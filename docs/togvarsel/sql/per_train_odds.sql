WITH jd AS (
  SELECT j.operating_date d,
    EXTRACT(HOUR FROM j.first_oslo_aimed_local)+EXTRACT(MINUTE FROM j.first_oslo_aimed_local)/60 h,
    (j.n_oslo_cancelled > 0 OR j.oslo_max_delay >= 5) trouble_any, (j.n_oslo_cancelled > 0) cx_any, (j.oslo_mean_delay >= 5) late_mean5
  FROM `ent-kixavvik-dev.disruptions.togvarsel_journeys` j WHERE j.n_oslo_calls > 0 AND NOT j.extra_journey
),
per_day AS (
  SELECT d,
    SAFE_DIVIDE(COUNTIF(h BETWEEN 6.5 AND 9 AND trouble_any), COUNTIF(h BETWEEN 6.5 AND 9)) am_train_trouble,
    SAFE_DIVIDE(COUNTIF(h BETWEEN 6.5 AND 9 AND cx_any), COUNTIF(h BETWEEN 6.5 AND 9)) am_train_cx,
    SAFE_DIVIDE(COUNTIF(h BETWEEN 15 AND 18 AND trouble_any), COUNTIF(h BETWEEN 15 AND 18)) pm_train_trouble,
    SAFE_DIVIDE(COUNTIF(h BETWEEN 15 AND 18 AND cx_any), COUNTIF(h BETWEEN 15 AND 18)) pm_train_cx,
    COUNTIF(h BETWEEN 6.5 AND 9) am_trains, COUNTIF(h BETWEEN 15 AND 18) pm_trains
  FROM jd GROUP BY d
),
pp AS (
  SELECT 'AM@06:30' fc, d, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_am_0630_A` UNION ALL SELECT 'AM@06:30', d, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_am_0630_B`
  UNION ALL SELECT 'PM@14:00', d, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_pm_1400_A` UNION ALL SELECT 'PM@14:00', d, p FROM `ent-kixavvik-dev.disruptions.togvarsel_pred_lr_pm_1400_B`
)
SELECT fc, CASE WHEN p < 0.10 THEN '1 green' WHEN p < 0.30 THEN '2 yellow' ELSE '3 red' END level, COUNT(*) n_days,
  ROUND(AVG(am_trains)) am_trains, ROUND(AVG(IF(fc LIKE 'AM%', am_train_trouble, pm_train_trouble)),3) share_trains_trouble,
  ROUND(AVG(IF(fc LIKE 'AM%', am_train_cx, pm_train_cx)),3) share_trains_cancelled,
  ROUND(APPROX_QUANTILES(IF(fc LIKE 'AM%', am_train_trouble, pm_train_trouble), 10)[OFFSET(9)],3) p90_share_trouble
FROM pp JOIN per_day USING (d) GROUP BY 1,2 ORDER BY 1,2
