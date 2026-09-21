-- disruptions.togvarsel_days: one row per day with the window outcomes pivoted to columns
-- (am_t10 is the AM-rush label source; prev_* are the previous calendar day's evening state,
-- weekends included — the serving query reads "yesterday" the same way).
--
-- am_t10 / pm_t10 are the CORRECTED labels since 2026-09-21 (docs/rushvarsel/04-cuts): the
-- share is taken over 03_day_windows.sql's n_t10_lab / n_res_lab (an estimated time is not an
-- observation; a journey that went silent never ran) with the planned trains that never ran
-- (togvarsel_plan_cut: absent from ET, or cut from the day's plan and not already cut the day
-- before) added to BOTH numerator and denominator. am_t10_raw / pm_t10_raw keep the old rule so
-- the two can be compared; every other window column is unchanged. The cut itself rides along as
-- cut_* for 07_features.sql.
CREATE OR REPLACE TABLE `disruptions.togvarsel_days` AS
WITH p AS (
  SELECT d,
    MAX(IF(win='night',  trouble5, NULL)) night_t5,
    MAX(IF(win='early',  trouble5, NULL)) early_t5,
    MAX(IF(win='am_rush',trouble5, NULL)) am_t5,
    MAX(IF(win='midday', trouble5, NULL)) mid_t5,
    MAX(IF(win='pm_rush',trouble5, NULL)) pm_t5,
    MAX(IF(win='evening',trouble5, NULL)) eve_t5,
    MAX(IF(win='early',  trouble10, NULL)) early_t10,
    MAX(IF(win='am_rush',trouble10, NULL)) am_t10_raw,
    MAX(IF(win='pm_rush',trouble10, NULL)) pm_t10_raw,
    MAX(IF(win='am_rush',n_t10_lab, NULL)) am_t10_n,
    MAX(IF(win='am_rush',n_res_lab, NULL)) am_res_n,
    MAX(IF(win='pm_rush',n_t10_lab, NULL)) pm_t10_n,
    MAX(IF(win='pm_rush',n_res_lab, NULL)) pm_res_n,
    MAX(IF(win='early',  cancel_rate, NULL)) early_cancel,
    MAX(IF(win='am_rush',cancel_rate, NULL)) am_cancel,
    MAX(IF(win='midday', cancel_rate, NULL)) mid_cancel,
    MAX(IF(win='pm_rush',cancel_rate, NULL)) pm_cancel,
    MAX(IF(win='evening',cancel_rate, NULL)) eve_cancel,
    MAX(IF(win='early',  mean_delay, NULL)) early_mean_delay,
    MAX(IF(win='am_rush',mean_delay, NULL)) am_mean_delay,
    MAX(IF(win='pm_rush',mean_delay, NULL)) pm_mean_delay,
    MAX(IF(win='early',  n_calls, NULL)) early_n,
    MAX(IF(win='am_rush',n_calls, NULL)) am_n,
    MAX(IF(win='pm_rush',n_calls, NULL)) pm_n
  FROM `disruptions.togvarsel_day_windows` WHERE corridor='all' GROUP BY d
),
k AS (
  SELECT d, am_miss, pm_miss, am_cut, am_cut_new, amrest_cut, amrest_cut_new, pm_cut, pm_cut_new
  FROM `disruptions.togvarsel_plan_cut`
),
j AS (
  SELECT p.*,
    -- same rounding as 03_day_windows.sql's trouble10 (rushhistorikk's per-scope rule)
    ROUND(SAFE_DIVIDE(am_t10_n + IFNULL(k.am_miss, 0) + IFNULL(k.am_cut_new, 0),
                      am_res_n + IFNULL(k.am_miss, 0) + IFNULL(k.am_cut_new, 0)), 4) am_t10,
    ROUND(SAFE_DIVIDE(pm_t10_n + IFNULL(k.pm_miss, 0) + IFNULL(k.pm_cut_new, 0),
                      pm_res_n + IFNULL(k.pm_miss, 0) + IFNULL(k.pm_cut_new, 0)), 4) pm_t10,
    IFNULL(k.am_cut, 0) cut_am, IFNULL(k.am_cut_new, 0) cut_am_new,
    IFNULL(k.amrest_cut, 0) cut_amrest, IFNULL(k.amrest_cut_new, 0) cut_amrest_new,
    IFNULL(k.pm_cut, 0) cut_pm, IFNULL(k.pm_cut_new, 0) cut_pm_new
  FROM p LEFT JOIN k USING (d)
)
SELECT *,
  EXTRACT(DAYOFWEEK FROM d) dow,
  EXTRACT(DAYOFWEEK FROM d) BETWEEN 2 AND 6 AS weekday,
  LAG(eve_t5) OVER (ORDER BY d) prev_eve_t5,
  LAG(pm_t5) OVER (ORDER BY d) prev_pm_t5,
  LAG(eve_cancel) OVER (ORDER BY d) prev_eve_cancel,
  LAG(d) OVER (ORDER BY d) prev_d
FROM j
