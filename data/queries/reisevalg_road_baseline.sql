-- Road-side baselines + per-morning anomaly table for the reisevalg-varsel
-- feasibility backtest (plan docs/plans/2026-07-11-001-..., U2). Two tables:
--
-- 1) disruptions.svv_baselines -- read by the LIVE module (disruptions/api/
--    reisevalg.py refresh_baselines); its schema is a serving contract:
--      point_id STRING, direction STRING, dow INT64, hour INT64,
--      median_volume FLOAT64, p10_volume FLOAT64, built_at TIMESTAMP
--    KEY CONVENTION (must match reisevalg.py volume_hour_states):
--      * dow  = PYTHON weekday(): 0=Monday .. 6=Sunday (the module keys the lookup
--        with `hour_start.astimezone(Oslo).weekday()`). In BigQuery that is
--        MOD(EXTRACT(DAYOFWEEK FROM date) + 5, 7). Weekday rows only (0..4):
--        weekend hours simply miss the lookup and are ignored by the provider,
--        which is correct -- the recommendation window is weekday mornings, and
--        the trailing fetch window only crosses midnight into the same weekday
--        convention on Tue-Fri (Mon early hours fall on Sunday=absent -> dropped).
--      * hour = Oslo-local hour of the hour START (module: `local.hour`).
--      * direction = the Trafikkdata byDirection heading string meaning
--        "toward Oslo" AT THAT POINT (varies: Liertoppen/Asker/Oslo/Festning);
--        the module drops fetched rows whose heading != direction.
--    Built from non-holiday weekday history with coverage >= 50, excluding
--    outage/closure days (a point-day whose toward-Oslo daily total is < 50% of
--    that point's same-weekday median daily total -- sensor gaps and full road
--    closures would otherwise poison p10). Each point uses its own available
--    window (Kjellstad/Tranby only start 2024-09). Drift check 2026-07-11:
--    year1-vs-year2 rush medians shift at most +4.1% (Holmen) -- no E18 roadwork
--    drift visible, so the full 2024-06..2026-05 window is used per point.
--
-- 2) disruptions.reisevalg_road_mornings -- one row per weekday morning
--    (2024-06-02..2026-05-29): rush-window (06:00-09:30) volume deficit vs
--    baseline and anomaly flags at several thresholds, built with the SAME
--    per-hour corridor rule as the live provider (a point-hour is deficient when
--    volume < (1-d) x same-point/dow/hour median; an hour is corridor-congested
--    when >= 50% of reporting points are deficient; a morning is anomalous when
--    two CONSECUTIVE congested hours touch the rush window). Hours 04-09 feed the
--    hourly rule (pre-rush congestion is the only live-detectable case given the
--    ~150 min publication lag); the volume-ratio flags use hours 06-08 (fully
--    inside the window) and are hindsight-only.
--    Detection timing: detect_ts_* = end of the 2nd consecutive congested hour
--    (hindsight); avail_ts_* = detect_ts + 150 min publication lag (U1 measured
--    134-194 min); avail_by_* compares against 07/08/09:00 Oslo.
--
-- Keep the holiday literals in sync with reisevalg_rail_mornings.sql (same
-- derivation comment there).
--
-- COST: disruptions.svv_hourly_volumes is ~25 MB; several passes ~ free.

-- ---------------------------------------------------------------------------
-- (a) serving baselines
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE `disruptions.svv_baselines` AS
WITH
holiday_dates AS (
  SELECT DATE(d) AS d FROM UNNEST([
    '2024-12-25','2024-12-26','2025-01-01','2025-04-17','2025-04-18','2025-04-21',
    '2025-05-01','2025-05-29','2025-06-09','2025-12-25','2025-12-26','2026-01-01',
    '2026-04-02','2026-04-03','2026-04-06','2026-05-01','2026-05-14','2026-05-25',
    '2024-12-23','2024-12-24','2024-12-27','2024-12-30','2024-12-31',
    '2025-04-14','2025-04-15','2025-04-16','2025-05-02','2025-05-30',
    '2025-12-22','2025-12-23','2025-12-24','2025-12-29','2025-12-30','2025-12-31',
    '2026-01-02','2026-05-15','2026-03-30','2026-03-31','2026-04-01'
  ]) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2024-07-08','2024-08-04')) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2025-07-07','2025-08-03')) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2024-09-30','2024-10-04')) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2025-02-17','2025-02-21')) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2025-09-29','2025-10-03')) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2026-02-16','2026-02-20')) d
),
usable AS (  -- toward-Oslo, usable-coverage weekday hours outside holidays
  SELECT v.point_id, v.heading, v.date_oslo,
         MOD(EXTRACT(DAYOFWEEK FROM v.date_oslo) + 5, 7) AS dow,  -- 0=Mon..6=Sun
         EXTRACT(HOUR FROM v.hour_start_oslo) AS hour,
         v.volume
  FROM `disruptions.svv_hourly_volumes` v
  LEFT JOIN (SELECT DISTINCT d FROM holiday_dates) h ON h.d = v.date_oslo
  WHERE v.toward_oslo AND v.volume IS NOT NULL AND v.coverage_pct >= 50
    AND MOD(EXTRACT(DAYOFWEEK FROM v.date_oslo) + 5, 7) < 5
    AND h.d IS NULL
),
daily AS (  -- point-day totals, for the outage/closure-day exclusion
  SELECT point_id, date_oslo, dow, SUM(volume) AS day_vol
  FROM usable GROUP BY point_id, date_oslo, dow
),
day_median AS (
  SELECT point_id, dow,
         APPROX_QUANTILES(day_vol, 100)[OFFSET(50)] AS med_day_vol
  FROM daily GROUP BY point_id, dow
),
good_days AS (  -- drop days below 50% of the point's same-weekday median total
  SELECT d.point_id, d.date_oslo
  FROM daily d JOIN day_median m USING (point_id, dow)
  WHERE d.day_vol >= 0.5 * m.med_day_vol
)
SELECT
  u.point_id,
  u.heading AS direction,
  u.dow,
  u.hour,
  CAST(APPROX_QUANTILES(u.volume, 100)[OFFSET(50)] AS FLOAT64) AS median_volume,
  CAST(APPROX_QUANTILES(u.volume, 100)[OFFSET(10)] AS FLOAT64) AS p10_volume,
  CURRENT_TIMESTAMP() AS built_at
FROM usable u
JOIN good_days g ON g.point_id = u.point_id AND g.date_oslo = u.date_oslo
GROUP BY u.point_id, u.heading, u.dow, u.hour;

-- ---------------------------------------------------------------------------
-- (b) per-morning road anomaly table (backtest side)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE `disruptions.reisevalg_road_mornings` AS
WITH
holiday_dates AS (
  SELECT DATE(d) AS d FROM UNNEST([
    '2024-12-25','2024-12-26','2025-01-01','2025-04-17','2025-04-18','2025-04-21',
    '2025-05-01','2025-05-29','2025-06-09','2025-12-25','2025-12-26','2026-01-01',
    '2026-04-02','2026-04-03','2026-04-06','2026-05-01','2026-05-14','2026-05-25',
    '2024-12-23','2024-12-24','2024-12-27','2024-12-30','2024-12-31',
    '2025-04-14','2025-04-15','2025-04-16','2025-05-02','2025-05-30',
    '2025-12-22','2025-12-23','2025-12-24','2025-12-29','2025-12-30','2025-12-31',
    '2026-01-02','2026-05-15','2026-03-30','2026-03-31','2026-04-01'
  ]) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2024-07-08','2024-08-04')) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2025-07-07','2025-08-03')) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2024-09-30','2024-10-04')) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2025-02-17','2025-02-21')) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2025-09-29','2025-10-03')) d
  UNION ALL SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2026-02-16','2026-02-20')) d
),
obs AS (  -- toward-Oslo usable observations, hours 04-09, joined to baselines
  SELECT v.date_oslo, v.point_id,
         EXTRACT(HOUR FROM v.hour_start_oslo) AS hour,
         v.volume, b.median_volume, b.p10_volume
  FROM `disruptions.svv_hourly_volumes` v
  JOIN `disruptions.svv_baselines` b
    ON b.point_id = v.point_id AND b.direction = v.heading
   AND b.dow = MOD(EXTRACT(DAYOFWEEK FROM v.date_oslo) + 5, 7)
   AND b.hour = EXTRACT(HOUR FROM v.hour_start_oslo)
  WHERE v.toward_oslo AND v.volume IS NOT NULL AND v.coverage_pct >= 50
    AND MOD(EXTRACT(DAYOFWEEK FROM v.date_oslo) + 5, 7) < 5
    AND EXTRACT(HOUR FROM v.hour_start_oslo) BETWEEN 4 AND 9
    AND b.median_volume > 0
),
hour_states AS (  -- the live provider's per-hour corridor rule, several deficits
  SELECT date_oslo, hour,
         COUNT(*) AS pts,
         AVG(IF(volume < 0.85 * median_volume, 1, 0)) >= 0.5 AS cong_d15,
         AVG(IF(volume < 0.80 * median_volume, 1, 0)) >= 0.5 AS cong_d20,
         AVG(IF(volume < 0.75 * median_volume, 1, 0)) >= 0.5 AS cong_d25,
         AVG(IF(volume < 0.70 * median_volume, 1, 0)) >= 0.5 AS cong_d30,
         AVG(IF(volume < p10_volume, 1, 0)) >= 0.5 AS cong_p10
  FROM obs GROUP BY date_oslo, hour
),
detect AS (  -- 2 consecutive congested hours, 2nd hour touching rush (06-09)
  SELECT date_oslo,
    MIN(IF(cong_d15 AND prev_d15 AND hour BETWEEN 6 AND 9, hour, NULL)) AS det_h_d15,
    MIN(IF(cong_d20 AND prev_d20 AND hour BETWEEN 6 AND 9, hour, NULL)) AS det_h_d20,
    MIN(IF(cong_d25 AND prev_d25 AND hour BETWEEN 6 AND 9, hour, NULL)) AS det_h_d25,
    MIN(IF(cong_d30 AND prev_d30 AND hour BETWEEN 6 AND 9, hour, NULL)) AS det_h_d30,
    MIN(IF(cong_p10 AND prev_p10 AND hour BETWEEN 6 AND 9, hour, NULL)) AS det_h_p10,
    -- earliest live-AVAILABLE detection: any adjacent congested pair (04-09);
    -- availability = end of 2nd hour + 150 min lag
    MIN(IF(cong_d25 AND prev_d25, hour, NULL)) AS det_h_d25_any
  FROM (
    SELECT *,
      LAG(cong_d15) OVER w AND LAG(hour) OVER w = hour - 1 AS prev_d15,
      LAG(cong_d20) OVER w AND LAG(hour) OVER w = hour - 1 AS prev_d20,
      LAG(cong_d25) OVER w AND LAG(hour) OVER w = hour - 1 AS prev_d25,
      LAG(cong_d30) OVER w AND LAG(hour) OVER w = hour - 1 AS prev_d30,
      LAG(cong_p10) OVER w AND LAG(hour) OVER w = hour - 1 AS prev_p10
    FROM hour_states
    WINDOW w AS (PARTITION BY date_oslo ORDER BY hour)
  )
  GROUP BY date_oslo
),
ratio AS (  -- hindsight rush-volume ratio, hours 06-08 (fully inside the window)
  SELECT date_oslo,
         COUNT(DISTINCT point_id) AS n_points,
         SUM(volume) AS rush_vol,
         SUM(median_volume) AS rush_exp,
         SAFE_DIVIDE(SUM(volume), SUM(median_volume)) AS rush_vol_ratio
  FROM obs WHERE hour BETWEEN 6 AND 8
  GROUP BY date_oslo
),
spine AS (
  SELECT d AS date_oslo
  FROM UNNEST(GENERATE_DATE_ARRAY('2024-06-01', '2026-05-31')) d
  WHERE MOD(EXTRACT(DAYOFWEEK FROM d) + 5, 7) < 5
)
SELECT
  s.date_oslo,
  h.d IS NOT NULL AS is_holiday,
  r.n_points, r.rush_vol, r.rush_exp,
  ROUND(r.rush_vol_ratio, 4) AS rush_vol_ratio,
  -- live-analog anomaly flags (>= 2 consecutive congested hours touching rush):
  d.det_h_d15 IS NOT NULL AS anom_d15,
  d.det_h_d20 IS NOT NULL AS anom_d20,
  d.det_h_d25 IS NOT NULL AS anom_d25,  -- the live placeholder constants (0.25/0.5/N=2)
  d.det_h_d30 IS NOT NULL AS anom_d30,
  d.det_h_p10 IS NOT NULL AS anom_p10,
  -- hindsight volume-ratio flags:
  r.rush_vol_ratio < 0.90 AS anom_vol90,
  r.rush_vol_ratio < 0.85 AS anom_vol85,
  r.rush_vol_ratio < 0.75 AS anom_vol75,
  -- timing (Oslo local): hindsight detection = end of 2nd consecutive congested
  -- hour; live availability adds the 150 min publication lag.
  d.det_h_d25, d.det_h_d20,
  DATETIME(s.date_oslo, TIME(d.det_h_d25 + 1, 0, 0)) AS detect_ts_d25,
  DATETIME_ADD(DATETIME(s.date_oslo, TIME(d.det_h_d25_any + 1, 0, 0)), INTERVAL 150 MINUTE) AS avail_ts_d25,
  DATETIME_ADD(DATETIME(s.date_oslo, TIME(d.det_h_d25_any + 1, 0, 0)), INTERVAL 150 MINUTE)
    <= DATETIME(s.date_oslo, TIME(9, 0, 0)) AS avail_by_0900_d25,
  DATETIME_ADD(DATETIME(s.date_oslo, TIME(d.det_h_d25_any + 1, 0, 0)), INTERVAL 150 MINUTE)
    <= DATETIME(s.date_oslo, TIME(8, 0, 0)) AS avail_by_0800_d25,
  DATETIME_ADD(DATETIME(s.date_oslo, TIME(d.det_h_d25_any + 1, 0, 0)), INTERVAL 150 MINUTE)
    <= DATETIME(s.date_oslo, TIME(7, 0, 0)) AS avail_by_0700_d25
FROM spine s
LEFT JOIN (SELECT DISTINCT d FROM holiday_dates) h ON h.d = s.date_oslo
LEFT JOIN ratio r ON r.date_oslo = s.date_oslo
LEFT JOIN detect d ON d.date_oslo = s.date_oslo;
