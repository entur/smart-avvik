-- Huskehjulet (E05) step 3: weather coupling — do incident types cluster in
-- weather bands, as the E05 storyboard claims ("sporvekselfeil ved lav temperatur
-- + fuktig luft")?
--
-- Population: localized (<= 3 affected stops) NSB incidents within 40 km of Oslo S,
-- daily weather from GHCN-D station OSLO BLINDERN (NOM00001492, 4 km from Oslo S).
-- rate_ratio (RR) = incident rate per day in bucket / category's overall daily rate.
--
-- REGION GOTCHA: bigquery-public-data.ghcn_d is in US; project + NSR tables are in
-- EU — one query cannot join them ("Access Denied"/"not found" for whichever side
-- is off-region). Fix: extract the ~700-row Blindern daily pivot with a US query,
-- then `bq load` it into disruptions.hh_weather_blindern (EU):
--
--   SELECT date,
--     MAX(IF(element = 'TMIN', value / 10, NULL)) AS tmin_c,
--     MAX(IF(element = 'TMAX', value / 10, NULL)) AS tmax_c,
--     MAX(IF(element = 'PRCP', value / 10, NULL)) AS prcp_mm,
--     MAX(IF(element = 'SNWD', value,      NULL)) AS snwd_mm
--   FROM (year tables ghcnd_2024 UNION ALL ghcnd_2025 UNION ALL ghcnd_2026)
--   WHERE id = 'NOM00001492' AND (qflag IS NULL OR TRIM(qflag) = '')
--   GROUP BY date
--   HAVING date >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR) AND date < CURRENT_DATE();
--
--   bq load --replace --source_format=CSV --skip_leading_rows=1 \
--     ent-kixavvik-dev:disruptions.hh_weather_blindern blindern.csv \
--     date:DATE,tmin_c:FLOAT,tmax_c:FLOAT,prcp_mm:FLOAT,snwd_mm:FLOAT
--
-- GHCN-D is daily and has NO humidity element — testing the actual RH/kondens
-- claim needs MET Frost (frost.met.no, hourly obs incl. RH) as the upgrade path.
--
-- RESULTS (2026-06-10, 712 days, Oslo area):
--   * sporveksel is a DEEP-COLD failure: tmin < -5C -> RR 2.51 (54 inc/100 days vs
--     ~21 overall); snow on ground RR 1.64; and it skews DRY (RR 1.23) — cold
--     high-pressure days, not humid ones. The storyboard's "fuktig luft" detail is
--     not supported at daily granularity; deep cold + snow is the fellesnevner.
--   * materiell/teknisk is broadly winter-coupled: RR 1.7 (<-5C), 2.0 (-5..0C),
--     snow RR 2.2 — rolling stock suffers in winter generally.
--   * signal is NOT weather-driven (RR 0.8-1.6, no monotone pattern) — also
--     contrary to the storyboard's signalfeil example.
--   * drift/personale: snow RR 2.67 — likely cascade/seasonal confound.
--   * Durations do NOT lengthen in cold for sporveksel (median 59 min at <-5C vs
--     84-86 mild) — the weather effect is on FREQUENCY, not duration.
-- Caveats: incidents (not incident-days) are counted, so cascade days inflate RR;
-- season is a confound for anything winter-correlated — sporveksel's deep-cold RR
-- should be read relative to materiell's (2.51 vs 1.70).

WITH banded AS (
  SELECT date,
    CASE WHEN tmin_c < -5 THEN '1: tmin < -5C'
         WHEN tmin_c <  0 THEN '2: tmin -5..0C'
         WHEN tmin_c <  5 THEN '3: tmin 0..5C'
         ELSE                  '4: tmin > 5C' END AS temp_band,
    IF(prcp_mm > 0, 'wet', 'dry')             AS wet,
    IF(snwd_mm > 0, 'snow_on_ground', 'bare') AS snow
  FROM `ent-kixavvik-dev.disruptions.hh_weather_blindern`
  WHERE tmin_c IS NOT NULL
),
oslo_stops AS (
  SELECT id FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version`
  WHERE ST_DWITHIN(ST_GEOGPOINT(location_longitude, location_latitude),
                   ST_GEOGPOINT(10.753, 59.911), 40000)
),
inc AS (
  SELECT d.situationNumber,
         DATE(d.onset_ts, 'Europe/Oslo') AS date,
         c.category, d.reopen_minutes
  FROM `ent-kixavvik-dev.disruptions.disruptions_initial` AS d
  LEFT JOIN `ent-kixavvik-dev.disruptions.categories` AS c
    ON  IFNULL(c.summary, '')     = IFNULL(d.summary, '')
    AND IFNULL(c.description, '') = IFNULL(d.description, '')
  WHERE ARRAY_LENGTH(d.affected_stops) <= 3
    AND EXISTS (SELECT 1 FROM UNNEST(d.affected_stops) AS sr JOIN oslo_stops AS o ON o.id = sr)
    AND c.category IN ('infrastruktur/sporveksel', 'infrastruktur/signal',
                       'infrastruktur/spor', 'materiell/teknisk', 'drift/personale')
),
joined AS (
  SELECT i.category, i.reopen_minutes, b.temp_band, b.wet, b.snow
  FROM inc AS i JOIN banded AS b USING (date)
),
dims AS (
  SELECT category, 'tmin' AS dim, temp_band AS bucket, reopen_minutes FROM joined
  UNION ALL SELECT category, 'precip', wet,  reopen_minutes FROM joined
  UNION ALL SELECT category, 'snowcover', snow, reopen_minutes FROM joined
),
day_dims AS (
  SELECT 'tmin' AS dim, temp_band AS bucket, COUNT(*) AS n_days FROM banded GROUP BY 2
  UNION ALL SELECT 'precip', wet,  COUNT(*) FROM banded GROUP BY 2
  UNION ALL SELECT 'snowcover', snow, COUNT(*) FROM banded GROUP BY 2
),
agg AS (
  SELECT d.category, d.dim, d.bucket,
         COUNT(*) AS n_inc,
         APPROX_QUANTILES(d.reopen_minutes, 2)[OFFSET(1)] AS med_reopen_min
  FROM dims AS d
  GROUP BY 1, 2, 3
)
SELECT a.category, a.dim, a.bucket, a.n_inc, dd.n_days,
       ROUND(a.n_inc * 100.0 / dd.n_days, 1) AS inc_per_100_days,
       ROUND( (a.n_inc / dd.n_days)
            / (SUM(a.n_inc) OVER (PARTITION BY a.category, a.dim)
               / SUM(dd.n_days) OVER (PARTITION BY a.category, a.dim)), 2) AS rate_ratio,
       a.med_reopen_min
FROM agg AS a
JOIN day_dims AS dd USING (dim, bucket)
ORDER BY a.category, a.dim, a.bucket;
