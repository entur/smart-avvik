-- Candidate features not in the production chain, per day, from the chain's own small tables.
-- Everything is computable at the same cutoffs the live query uses (calls AIMED before cutoff-10min).
CREATE OR REPLACE TABLE `disruptions.exp_rush_features3` AS
WITH oc AS (
  SELECT DATE(c.aimed_local) d, c.dsj, c.line, s.corridor, c.cancelled,
    EXTRACT(HOUR FROM c.aimed_local) + EXTRACT(MINUTE FROM c.aimed_local)/60.0 h,
    COALESCE(c.arr_delay_min, c.dep_delay_min) delay_min,
    (c.cancelled OR c.actual_arr IS NOT NULL OR c.actual_dep IS NOT NULL) resolved
  FROM `disruptions.togvarsel_calls` c JOIN `disruptions.togvarsel_oslo_stations` s USING (stop_place)
  WHERE NOT IFNULL(c.extra_journey,FALSE)
),
-- breadth: how many lines / corridors carry trouble before each cutoff
line_early AS (
  SELECT d, line,
    COUNTIF(h>=4 AND h<6.33 AND (cancelled OR delay_min>=5)) n_bad_0630,
    COUNTIF(h>=4 AND h<6.33 AND resolved) n_res_0630,
    COUNTIF(h>=4 AND h<6.83 AND (cancelled OR delay_min>=5)) n_bad_0700,
    COUNTIF(h>=4 AND h<6.83 AND resolved) n_res_0700,
    COUNTIF(h>=4 AND h<11.83 AND (cancelled OR delay_min>=5)) n_bad_1200,
    COUNTIF(h>=4 AND h<11.83 AND resolved) n_res_1200,
    COUNTIF(h>=4 AND h<13.83 AND (cancelled OR delay_min>=5)) n_bad_1400,
    COUNTIF(h>=4 AND h<13.83 AND resolved) n_res_1400
  FROM oc GROUP BY d, line
),
lines AS (
  SELECT d,
    COUNTIF(n_res_0630 >= 3 AND SAFE_DIVIDE(n_bad_0630, n_res_0630) >= 0.2) nlines_bad_0630,
    COUNTIF(n_res_0700 >= 3 AND SAFE_DIVIDE(n_bad_0700, n_res_0700) >= 0.2) nlines_bad_0700,
    COUNTIF(n_res_1200 >= 3 AND SAFE_DIVIDE(n_bad_1200, n_res_1200) >= 0.2) nlines_bad_1200,
    COUNTIF(n_res_1400 >= 3 AND SAFE_DIVIDE(n_bad_1400, n_res_1400) >= 0.2) nlines_bad_1400,
    COUNTIF(n_bad_0630 > 0) nlines_any_0630,
    COUNTIF(n_bad_0700 > 0) nlines_any_0700
  FROM line_early GROUP BY d
),
corr_early AS (
  SELECT d, corridor,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.33 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<6.33 AND resolved)) t5_0630,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<6.83 AND resolved)) t5_0700
  FROM oc GROUP BY d, corridor
),
corr AS (
  SELECT d,
    COUNTIF(t5_0630 >= 0.10) ncorr_bad_0630, MAX(t5_0630) corr_max_t5_0630,
    COUNTIF(t5_0700 >= 0.10) ncorr_bad_0700, MAX(t5_0700) corr_max_t5_0700,
    MAX(IF(corridor='west', t5_0630, NULL)) west_t5_0630,
    MAX(IF(corridor='northeast', t5_0630, NULL)) ne_t5_0630,
    MAX(IF(corridor='south', t5_0630, NULL)) south_t5_0630,
    MAX(IF(corridor='central', t5_0630, NULL)) central_t5_0630
  FROM corr_early GROUP BY d
),
win AS (
  SELECT d,
    -- finer recency at 06:30 / 07:00: the last 15 minutes and the ≥10 share of the last 30
    SAFE_DIVIDE(COUNTIF(h>=6.08 AND h<6.33 AND (cancelled OR delay_min>=5)), COUNTIF(h>=6.08 AND h<6.33 AND resolved)) et_t5_last15_0630,
    SAFE_DIVIDE(COUNTIF(h>=5.83 AND h<6.33 AND (cancelled OR delay_min>=10)), COUNTIF(h>=5.83 AND h<6.33 AND resolved)) et_t10_last_0630,
    COUNTIF(h>=5.83 AND h<6.33 AND cancelled) et_cx_last_0630,
    SAFE_DIVIDE(COUNTIF(h>=6.58 AND h<6.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=6.58 AND h<6.83 AND resolved)) et_t5_last15_0700,
    SAFE_DIVIDE(COUNTIF(h>=6.33 AND h<6.83 AND (cancelled OR delay_min>=10)), COUNTIF(h>=6.33 AND h<6.83 AND resolved)) et_t10_last_0700,
    COUNTIF(h>=6.33 AND h<6.83 AND cancelled) et_cx_last_0700,
    -- the first half hour of the rush itself is what the 07:00 issue has seen (06:30-06:50)
    SAFE_DIVIDE(COUNTIF(h>=6.5 AND h<6.83 AND (cancelled OR delay_min>=10)), COUNTIF(h>=6.5 AND h<6.83 AND resolved)) et_t10_rush_0700,
    -- midday-only windows for the PM issues (et_t5_1200/1400 are cumulative since 04:00 and dominated by the morning)
    SAFE_DIVIDE(COUNTIF(h>=9 AND h<11.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=9 AND h<11.83 AND resolved)) et_t5_mid_1200,
    SAFE_DIVIDE(COUNTIF(h>=9 AND h<11.83 AND (cancelled OR delay_min>=10)), COUNTIF(h>=9 AND h<11.83 AND resolved)) et_t10_mid_1200,
    COUNTIF(h>=9 AND h<11.83 AND cancelled) et_cx_mid_1200,
    SAFE_DIVIDE(COUNTIF(h>=9 AND h<13.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=9 AND h<13.83 AND resolved)) et_t5_mid_1400,
    SAFE_DIVIDE(COUNTIF(h>=9 AND h<13.83 AND (cancelled OR delay_min>=10)), COUNTIF(h>=9 AND h<13.83 AND resolved)) et_t10_mid_1400,
    COUNTIF(h>=9 AND h<13.83 AND cancelled) et_cx_mid_1400,
    SAFE_DIVIDE(COUNTIF(h>=12 AND h<13.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=12 AND h<13.83 AND resolved)) et_t5_1200_1350,
    COUNTIF(h>=12 AND h<13.83 AND cancelled) et_cx_1200_1350,
    -- the morning rush's own outcome, known by 12:00 (the ripple the study measured directly)
    SAFE_DIVIDE(COUNTIF(h>=6.5 AND h<9 AND (cancelled OR delay_min>=10)), COUNTIF(h>=6.5 AND h<9 AND resolved)) am_t10_known,
    SAFE_DIVIDE(COUNTIF(h>=6.5 AND h<9 AND cancelled), COUNTIF(h>=6.5 AND h<9)) am_cancel_known,
    -- how many distinct journeys are cancelled before each cutoff (journey-level, not call-level)
    COUNT(DISTINCT IF(h>=4 AND h<11.83 AND cancelled, dsj, NULL)) et_cxj_1200,
    COUNT(DISTINCT IF(h>=4 AND h<13.83 AND cancelled, dsj, NULL)) et_cxj_1400
  FROM oc GROUP BY d
),
days AS (SELECT d, am_t10, pm_t10 FROM `disruptions.togvarsel_days`),
lagged AS (
  SELECT d,
    LAG(am_t10) OVER (ORDER BY d) prev_am_t10_cal,
    LAG(pm_t10) OVER (ORDER BY d) prev_pm_t10_cal
  FROM days
)
SELECT dd.d,
  lines.* EXCEPT(d), corr.* EXCEPT(d), win.* EXCEPT(d), lagged.* EXCEPT(d),
  -- trend: is the last half hour worse than the early window as a whole?
  f.et_t5_last_0630 - f.et_t5_0630 AS trend_0630,
  c.et_t5_last_0700 - c.et_t5_0700 AS trend_0700,
  f.et_t5_last_1200 - f.et_t5_1200 AS trend_1200,
  f.et_t5_last_1400 - f.et_t5_1400 AS trend_1400
FROM `disruptions.togvarsel_days` dd
LEFT JOIN lines USING (d) LEFT JOIN corr USING (d) LEFT JOIN win USING (d) LEFT JOIN lagged USING (d)
LEFT JOIN `disruptions.togvarsel_features` f USING (d)
LEFT JOIN `disruptions.togvarsel_features_cat` c USING (d)
