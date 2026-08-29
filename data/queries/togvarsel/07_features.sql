-- disruptions.togvarsel_features: one row per day — ET state at the 06:30 / 09:00 / 12:00 /
-- 14:00 cutoffs, the previous evening, calendar flags, plus the realized window outcomes the
-- labels come from. An ET cutoff at HH:MM uses calls AIMED before HH-10min (the 06:30 forecast
-- sees trains scheduled up to 06:20), with rates over resolved calls only, so a partially
-- ingested morning changes the denominator, not the meaning.
--
-- HOLIDAYS AND SEASONS ARE HARDCODED and must be extended once a year (a known wart, §13 of the
-- findings doc). The serving query (disruptions/api/togvarsel_features_today.sql) carries the
-- SAME lists — extend both together, and keep them covering at least a year past today.
CREATE OR REPLACE TABLE `disruptions.togvarsel_features` AS
WITH oc AS (
  SELECT DATE(c.aimed_local) d, c.dsj, c.cancelled,
    EXTRACT(HOUR FROM c.aimed_local) + EXTRACT(MINUTE FROM c.aimed_local)/60.0 h,
    COALESCE(c.arr_delay_min, c.dep_delay_min) delay_min,
    (c.cancelled OR c.actual_arr IS NOT NULL OR c.actual_dep IS NOT NULL) resolved
  FROM `disruptions.togvarsel_calls` c JOIN `disruptions.togvarsel_oslo_stations` USING (stop_place)
  WHERE NOT IFNULL(c.extra_journey,FALSE)
),
et AS (
  SELECT d,
    -- cutoff 06:30 (calls aimed 04:00-06:20)
    COUNTIF(h>=4 AND h<6.33) et_n_0630,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.33 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<6.33 AND resolved)) et_t5_0630,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.33 AND (cancelled OR delay_min>=10)), COUNTIF(h>=4 AND h<6.33 AND resolved)) et_t10_0630,
    COUNTIF(h>=4 AND h<6.33 AND cancelled) et_cx_0630,
    COUNT(DISTINCT IF(h>=4 AND h<6.33 AND cancelled, dsj, NULL)) et_cxj_0630,
    AVG(IF(h>=4 AND h<6.33 AND NOT cancelled, delay_min, NULL)) et_md_0630,
    MAX(IF(h>=4 AND h<6.33 AND NOT cancelled, delay_min, NULL)) et_max_0630,
    SAFE_DIVIDE(COUNTIF(h>=5.83 AND h<6.33 AND (cancelled OR delay_min>=5)), COUNTIF(h>=5.83 AND h<6.33 AND resolved)) et_t5_last_0630,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.33 AND cancelled), COUNTIF(h>=4 AND h<6.33)) et_cxr_0630,
    -- cutoff 09:00 (04:00-08:50)
    COUNTIF(h>=4 AND h<8.83) et_n_0900,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<8.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<8.83 AND resolved)) et_t5_0900,
    COUNTIF(h>=4 AND h<8.83 AND cancelled) et_cx_0900,
    AVG(IF(h>=4 AND h<8.83 AND NOT cancelled, delay_min, NULL)) et_md_0900,
    SAFE_DIVIDE(COUNTIF(h>=7.5 AND h<8.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=7.5 AND h<8.83 AND resolved)) et_t5_last_0900,
    -- cutoff 12:00 (04:00-11:50)
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<11.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<11.83 AND resolved)) et_t5_1200,
    COUNTIF(h>=4 AND h<11.83 AND cancelled) et_cx_1200,
    SAFE_DIVIDE(COUNTIF(h>=10.5 AND h<11.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=10.5 AND h<11.83 AND resolved)) et_t5_last_1200,
    -- cutoff 14:00 (04:00-13:50)
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<13.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<13.83 AND resolved)) et_t5_1400,
    SAFE_DIVIDE(COUNTIF(h>=12.5 AND h<13.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=12.5 AND h<13.83 AND resolved)) et_t5_last_1400,
    COUNTIF(h>=4 AND h<13.83 AND cancelled) et_cx_1400
  FROM oc GROUP BY d
),
hol AS (
  -- Norwegian public holidays + the rail-quiet 24/12 and 31/12, on weekdays in the data window.
  -- EXTEND YEARLY (together with the serving query's copy).
  SELECT d FROM UNNEST([DATE '2025-04-17', '2025-04-18', '2025-04-21', '2025-05-01', '2025-05-17', '2025-05-29', '2025-06-09', '2025-12-24', '2025-12-25', '2025-12-26', '2025-12-31', '2026-01-01',
    '2026-04-02', '2026-04-03', '2026-04-06', '2026-05-01', '2026-05-14', '2026-05-25',
    '2026-12-24', '2026-12-25', '2026-12-26', '2026-12-31', '2027-01-01',
    '2027-03-25', '2027-03-26', '2027-03-29', '2027-05-01', '2027-05-06', '2027-05-17']) d
)
SELECT dd.d, dd.dow, dd.weekday, EXTRACT(MONTH FROM dd.d) month, EXTRACT(ISOWEEK FROM dd.d) isoweek,
  (hol.d IS NOT NULL) AS holiday,
  -- Low season: July + Christmas + Easter weeks. EXTEND YEARLY (with the serving query's copy).
  (EXTRACT(MONTH FROM dd.d)=7 OR dd.d BETWEEN '2025-12-22' AND '2026-01-02' OR dd.d BETWEEN '2025-04-14' AND '2025-04-21' OR dd.d BETWEEN '2026-03-30' AND '2026-04-06'
   OR dd.d BETWEEN '2026-12-21' AND '2027-01-03' OR dd.d BETWEEN '2027-03-22' AND '2027-03-29') AS low_season,
  -- realized outcomes (label sources)
  dd.am_t5, dd.pm_t5, dd.am_t10, dd.pm_t10, dd.am_cancel, dd.pm_cancel, dd.mid_t5, dd.eve_t5, dd.am_n, dd.pm_n,
  -- previous evening
  dd.prev_eve_t5, dd.prev_eve_cancel, dd.prev_pm_t5, dd.night_t5,
  -- et at cutoffs
  et.* EXCEPT(d),
  -- sx
  sx.* EXCEPT(d)
FROM `disruptions.togvarsel_days` dd
LEFT JOIN et ON et.d = dd.d
LEFT JOIN `disruptions.togvarsel_sx_day` sx ON sx.d = dd.d
LEFT JOIN hol ON hol.d = dd.d
