-- disruptions.togvarsel_features: one row per day — ET state at the 06:30 / 09:00 / 12:00 /
-- 14:00 cutoffs, the previous evening, calendar flags, plus the realized window outcomes the
-- labels come from. An ET cutoff at HH:MM uses calls AIMED before HH-10min (the 06:30 forecast
-- sees trains scheduled up to 06:20), with rates over resolved calls only, so a partially
-- ingested morning changes the denominator, not the meaning.
--
-- Holiday and low-season flags come from disruptions.togvarsel_calendar (00_calendar.sql — the
-- single source shared with the serving query). A training day outside the calendar's maintained
-- range ERRORs the rebuild, so a retrain can never silently treat an unmaintained holiday as an
-- ordinary day.
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
cal AS (
  SELECT d, holiday, low_season FROM `disruptions.togvarsel_calendar`
)
SELECT dd.d, dd.dow, dd.weekday, EXTRACT(MONTH FROM dd.d) month, EXTRACT(ISOWEEK FROM dd.d) isoweek,
  IF(cal.d IS NULL,
     ERROR(FORMAT('togvarsel_calendar mangler %t — utvid data/queries/togvarsel/00_calendar.sql', dd.d)),
     cal.holiday) AS holiday,
  cal.low_season,
  -- realized outcomes (label sources)
  dd.am_t5, dd.pm_t5, dd.am_t10, dd.pm_t10, dd.am_cancel, dd.pm_cancel, dd.mid_t5, dd.eve_t5, dd.am_n, dd.pm_n,
  -- previous evening
  dd.prev_eve_t5, dd.prev_eve_cancel, dd.prev_pm_t5, dd.night_t5,
  -- the day's plan against the standing timetable (03b_plan.sql via 04_days.sql; 04-cuts):
  -- planned Oslo calls cut from the plan, all / first seen today. Known at 02:06, linear in the
  -- models (deliberately outside the log1p prefix rule -- the experiment kept them linear).
  dd.cut_am, dd.cut_am_new, dd.cut_amrest, dd.cut_amrest_new, dd.cut_pm, dd.cut_pm_new,
  -- et at cutoffs
  et.* EXCEPT(d),
  -- sx
  sx.* EXCEPT(d)
FROM `disruptions.togvarsel_days` dd
LEFT JOIN et ON et.d = dd.d
LEFT JOIN `disruptions.togvarsel_sx_day` sx ON sx.d = dd.d
LEFT JOIN cal ON cal.d = dd.d
