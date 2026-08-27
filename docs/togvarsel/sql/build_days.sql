CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.togvarsel_days` AS
WITH p AS (
  SELECT d,
    MAX(IF(win='night',  trouble5, NULL)) night_t5,
    MAX(IF(win='early',  trouble5, NULL)) early_t5,
    MAX(IF(win='am_rush',trouble5, NULL)) am_t5,
    MAX(IF(win='midday', trouble5, NULL)) mid_t5,
    MAX(IF(win='pm_rush',trouble5, NULL)) pm_t5,
    MAX(IF(win='evening',trouble5, NULL)) eve_t5,
    MAX(IF(win='early',  trouble10, NULL)) early_t10,
    MAX(IF(win='am_rush',trouble10, NULL)) am_t10,
    MAX(IF(win='pm_rush',trouble10, NULL)) pm_t10,
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
  FROM `ent-kixavvik-dev.disruptions.togvarsel_day_windows` WHERE corridor='all' GROUP BY d
)
SELECT *,
  EXTRACT(DAYOFWEEK FROM d) dow,
  EXTRACT(DAYOFWEEK FROM d) BETWEEN 2 AND 6 AS weekday,
  LAG(eve_t5) OVER (ORDER BY d) prev_eve_t5,
  LAG(pm_t5) OVER (ORDER BY d) prev_pm_t5,
  LAG(eve_cancel) OVER (ORDER BY d) prev_eve_cancel,
  LAG(d) OVER (ORDER BY d) prev_d
FROM p
