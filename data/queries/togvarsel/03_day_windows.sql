-- disruptions.togvarsel_day_windows: per (day, time window, corridor + an 'all' rollup) call
-- outcomes. trouble5/trouble10 are the shares of calls cancelled OR ≥5/≥10 min late, over the
-- calls with a known outcome (cancelled or an actual time) — the product bar is trouble10
-- (§13/§14 of docs/togvarsel/02-findings-2026-08-27.md). Labels for the togvarsel models come
-- from the 'all' rows via 04_days.sql.
CREATE OR REPLACE TABLE `disruptions.togvarsel_day_windows` AS
WITH c AS (
  SELECT c.operating_date, DATE(c.aimed_local) AS local_date, c.dsj, c.line, s.corridor, s.station,
    c.cancelled,
    COALESCE(c.arr_delay_min, c.dep_delay_min) AS delay_min,
    (c.actual_arr IS NOT NULL OR c.actual_dep IS NOT NULL) AS has_actual,
    -- Corrected-label terms (docs/rushvarsel/04-cuts): an ESTIMATED time is not an observation
    -- (last_recorded fills the unrun tail of a journey pulled mid-run with its last expected
    -- times), and an estimated call whose journey went silent > 10 min before it was due never
    -- ran. has_actual above is kept for the feature columns; the label columns use these two.
    (c.actual_arr IS NOT NULL OR c.actual_dep IS NOT NULL) AND NOT IFNULL(c.estimated, FALSE) AS observed,
    (IFNULL(c.estimated, FALSE) AND (c.actual_arr IS NOT NULL OR c.actual_dep IS NOT NULL) AND NOT c.cancelled
       AND c.recorded_at < TIMESTAMP_SUB(c.aimed_ts, INTERVAL 10 MINUTE)) AS silent,
    EXTRACT(HOUR FROM c.aimed_local) + EXTRACT(MINUTE FROM c.aimed_local)/60.0 AS h
  FROM `disruptions.togvarsel_calls` c
  JOIN `disruptions.togvarsel_oslo_stations` s USING (stop_place)
  WHERE NOT IFNULL(c.extra_journey, FALSE)
),
w AS (
  SELECT *, CASE
    WHEN h >= 4.5 AND h < 6.5 THEN 'early'
    WHEN h >= 6.5 AND h < 9 THEN 'am_rush'
    WHEN h >= 9 AND h < 15 THEN 'midday'
    WHEN h >= 15 AND h < 18 THEN 'pm_rush'
    WHEN h >= 18 AND h < 24 THEN 'evening'
    ELSE 'night' END AS win
  FROM c
)
SELECT local_date AS d, win, corridor,
  COUNT(*) n_calls, COUNT(DISTINCT dsj) n_journeys,
  COUNTIF(cancelled) n_cancelled,
  COUNTIF(NOT cancelled AND NOT has_actual) n_unresolved,
  COUNTIF(delay_min >= 3) n_ge3, COUNTIF(delay_min >= 5) n_ge5, COUNTIF(delay_min >= 10) n_ge10, COUNTIF(delay_min >= 20) n_ge20,
  ROUND(AVG(IF(NOT cancelled, delay_min, NULL)),2) mean_delay,
  ROUND(APPROX_QUANTILES(IF(NOT cancelled, delay_min, NULL), 20)[OFFSET(18)],1) p90_delay,
  ROUND(SAFE_DIVIDE(COUNTIF(cancelled OR delay_min >= 5), COUNTIF(cancelled OR has_actual)),4) trouble5,
  ROUND(SAFE_DIVIDE(COUNTIF(cancelled OR delay_min >= 10), COUNTIF(cancelled OR has_actual)),4) trouble10,
  ROUND(SAFE_DIVIDE(COUNTIF(cancelled), COUNT(*)),4) cancel_rate,
  -- label numerator / denominator under the corrected rule; 04_days.sql adds the planned trains
  -- that never ran (togvarsel_plan_cut) to both before taking the share
  COUNTIF(cancelled OR silent OR (observed AND delay_min >= 10)) n_t10_lab,
  COUNTIF(cancelled OR silent OR observed) n_res_lab
FROM w
GROUP BY 1,2,3
UNION ALL
SELECT local_date, win, 'all',
  COUNT(*), COUNT(DISTINCT dsj), COUNTIF(cancelled), COUNTIF(NOT cancelled AND NOT has_actual),
  COUNTIF(delay_min >= 3), COUNTIF(delay_min >= 5), COUNTIF(delay_min >= 10), COUNTIF(delay_min >= 20),
  ROUND(AVG(IF(NOT cancelled, delay_min, NULL)),2),
  ROUND(APPROX_QUANTILES(IF(NOT cancelled, delay_min, NULL), 20)[OFFSET(18)],1),
  ROUND(SAFE_DIVIDE(COUNTIF(cancelled OR delay_min >= 5), COUNTIF(cancelled OR has_actual)),4),
  ROUND(SAFE_DIVIDE(COUNTIF(cancelled OR delay_min >= 10), COUNTIF(cancelled OR has_actual)),4),
  ROUND(SAFE_DIVIDE(COUNTIF(cancelled), COUNT(*)),4),
  COUNTIF(cancelled OR silent OR (observed AND delay_min >= 10)),
  COUNTIF(cancelled OR silent OR observed)
FROM w GROUP BY 1,2
