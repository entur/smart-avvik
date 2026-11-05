-- Leakage-safe pre-onset WEATHER features, one row per situationNumber, from
-- frost.met.no observations (`disruptions.frost_weather`). Tests the hypothesis
-- that ambient weather at onset (snow, wind, frost, heat, rain) predicts how long
-- a disruption runs -- the LEARNINGS "add weather features" next step.
--
-- GEOGRAPHIC LINK (the crux). Disruptions reference NSR StopPlaces
-- (`affected_stops`), but `frost_weather` is keyed by met.no station. We map each
-- affected StopPlace to its nearest <=3 weather stations within 25 km (NSR
-- stop-place coords vs station coords), then read weather AREALLY across all
-- stations near a situation's stops -- this fills the per-station element gaps
-- (a precip-only gauge nearby is complemented by a neighbour reporting temp/wind)
-- and represents "conditions in the area", which is the right granularity for the
-- regional weather (snow/cold/storm) that matters to rail. Coverage: 192/210
-- affected stops get a station within 25 km; 95% of situations get some weather,
-- 82% temperature, 94% precipitation. The ~25 km / top-3 knobs trade coverage
-- against locality.
--
-- LEAKAGE CONTROL. Unlike ET (whose final delays leak future knowledge), weather
-- observations are ground-truth measurements of the PAST and are never revised
-- with future info, so any window ending at onset is leakage-safe. Every window
-- below ends at `onset_ts` (ts <= onset, ts > onset-24h). (Production caveat: an
-- hourly obs for the hour containing onset may publish a few minutes after onset;
-- immaterial offline, and a live poller would use the latest available obs.)
--
-- PREREQUISITES (built once, cheap):
--   * `disruptions.frost_stations` -- station id/name/lat/lon, loaded from
--     data/frost/stations_with_distance.csv (1568 rows):
--       bq load --replace --source_format=CSV --skip_leading_rows=1 \
--         --schema 'id:STRING,name:STRING,lat:FLOAT,lon:FLOAT,county:STRING,nearest_quay_km:FLOAT' \
--         disruptions.frost_stations data/frost/stations_with_distance.csv

-- 1. StopPlace -> nearest <=3 stations within 25 km (areal weather sources).
CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.stopplace_station` AS
WITH affected AS (
  SELECT DISTINCT st AS stop_place
  FROM `disruptions.disruptions_initial`, UNNEST(affected_stops) AS st
),
sp AS (
  SELECT id AS stop_place, name AS stop_name,
         location_latitude AS lat, location_longitude AS lon
  FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version`
  WHERE location_latitude IS NOT NULL AND location_longitude IS NOT NULL
),
joined AS (SELECT a.stop_place, sp.stop_name, sp.lat, sp.lon FROM affected a JOIN sp USING (stop_place)),
ranked AS (
  SELECT j.stop_place, j.stop_name, s.id AS station_id, s.name AS station_name,
         ST_DISTANCE(ST_GEOGPOINT(j.lon,j.lat), ST_GEOGPOINT(s.lon,s.lat))/1000.0 AS dist_km,
         ROW_NUMBER() OVER (PARTITION BY j.stop_place
           ORDER BY ST_DISTANCE(ST_GEOGPOINT(j.lon,j.lat), ST_GEOGPOINT(s.lon,s.lat))) AS rk
  FROM joined j, `disruptions.frost_stations` s
)
SELECT stop_place, stop_name, station_id, station_name, dist_km, rk
FROM ranked WHERE rk <= 3 AND dist_km <= 25;

-- 2. Compact hourly weather for just the mapped stations + the 5 relevant elements.
--    `value` is STRING in frost_weather -> SAFE_CAST. Multiple levels per element
--    (e.g. temp at 2 m / 10 m) are averaged. Precip winsorized at 80 mm/h (one bad
--    gauge reported 160); snow depth clamped >=0 (MET codes -1 = snow-free).
--    ~9 GB scan of the 16 GB clustered table (~$0.05).
CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.station_weather_hourly` AS
WITH raw AS (
  SELECT
    SPLIT(source_id, ':')[OFFSET(0)] AS station_id,
    reference_time AS ts,
    element_id,
    SAFE_CAST(value AS FLOAT64) AS v
  FROM `disruptions.frost_weather`
  WHERE time_resolution = 'PT1H'
    AND element_id IN ('air_temperature','wind_speed','max(wind_speed_of_gust PT1H)',
                       'sum(precipitation_amount PT1H)','surface_snow_thickness')
    AND value IS NOT NULL
)
SELECT
  station_id, ts,
  AVG(IF(element_id='air_temperature', v, NULL))                AS air_temp,
  AVG(IF(element_id='wind_speed', v, NULL))                     AS wind_speed,
  AVG(IF(element_id='max(wind_speed_of_gust PT1H)', v, NULL))   AS gust,
  LEAST(AVG(IF(element_id='sum(precipitation_amount PT1H)', v, NULL)), 80.0) AS precip_1h,
  GREATEST(AVG(IF(element_id='surface_snow_thickness', v, NULL)), 0.0)       AS snow_depth
FROM raw
WHERE station_id IN (SELECT DISTINCT station_id FROM `disruptions.stopplace_station`)
GROUP BY station_id, ts;

-- 3. Per-situation onset weather features. Two-level aggregation: per (situation,
--    station) window stats, then areally across the situation's nearby stations
--    (worst-case MAX for precip/wind/snow, MIN/MAX for temp extremes, AVG for the
--    "now" temperature). NULLs kept (BOOSTED_TREE handles missing natively) + has-flags.
CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.weather_features` AS
WITH sit AS (
  SELECT situationNumber, onset_ts, affected_stops FROM `disruptions.disruptions_initial`
),
sit_station AS (
  SELECT DISTINCT s.situationNumber, s.onset_ts, ss.station_id
  FROM sit s
  CROSS JOIN UNNEST(s.affected_stops) AS st
  JOIN `disruptions.stopplace_station` ss ON ss.stop_place = st
),
sw AS (   -- per (situation, station) window aggregates, all windows ending at onset
  SELECT
    ss.situationNumber, ss.station_id,
    SUM(IF(w.ts >= TIMESTAMP_SUB(ss.onset_ts, INTERVAL 6  HOUR), w.precip_1h, NULL)) AS st_precip_6h,
    SUM(w.precip_1h)                                                                 AS st_precip_24h,
    AVG(IF(w.ts >= TIMESTAMP_SUB(ss.onset_ts, INTERVAL 3  HOUR), w.air_temp, NULL))  AS st_temp_now,
    MIN(w.air_temp)                                                                  AS st_temp_min24,
    MAX(w.air_temp)                                                                  AS st_temp_max24,
    MAX(IF(w.ts >= TIMESTAMP_SUB(ss.onset_ts, INTERVAL 6  HOUR), w.wind_speed, NULL))AS st_wind_max6,
    MAX(IF(w.ts >= TIMESTAMP_SUB(ss.onset_ts, INTERVAL 6  HOUR), w.gust, NULL))      AS st_gust_max6,
    MAX(w.snow_depth)                                                                AS st_snow
  FROM sit_station ss
  JOIN `disruptions.station_weather_hourly` w
    ON w.station_id = ss.station_id
   AND w.ts >  TIMESTAMP_SUB(ss.onset_ts, INTERVAL 24 HOUR)
   AND w.ts <= ss.onset_ts
  GROUP BY ss.situationNumber, ss.station_id
)
SELECT
  s.situationNumber,
  MAX(sw.st_precip_6h)  AS wx_precip_6h,    -- worst 6 h rain accumulation among nearby stations (mm)
  MAX(sw.st_precip_24h) AS wx_precip_24h,   -- worst 24 h rain accumulation (mm)
  AVG(sw.st_temp_now)   AS wx_temp_now,     -- areal mean temp in last 3 h (degC)
  MIN(sw.st_temp_min24) AS wx_temp_min24,   -- coldest in last 24 h (frost/ice proxy)
  MAX(sw.st_temp_max24) AS wx_temp_max24,   -- hottest in last 24 h (heat/sun-kink proxy)
  MAX(sw.st_wind_max6)  AS wx_wind_max6,    -- peak mean wind in last 6 h (m/s)
  MAX(sw.st_gust_max6)  AS wx_gust_max6,    -- peak gust in last 6 h (m/s)
  MAX(sw.st_snow)       AS wx_snow,         -- max snow depth on ground (cm)
  LOGICAL_OR(sw.station_id IS NOT NULL)   AS wx_has_any,
  LOGICAL_OR(sw.st_temp_now IS NOT NULL)  AS wx_has_temp,
  LOGICAL_OR(sw.st_precip_6h IS NOT NULL) AS wx_has_precip
FROM sit s
LEFT JOIN sw USING (situationNumber)
GROUP BY s.situationNumber;
