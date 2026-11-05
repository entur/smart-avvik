-- p90 ("bad case") delay quantile, as a calibration layer over the dep/arr point regressors.
-- BQML boosted trees have no native quantile/pinball loss, so we estimate the conditional p90
-- empirically: bucket calls by the point model's prediction and read off the 90th percentile of
-- ACTUAL delay in each bucket. The delay tail is heteroscedastic (the log-residual p90 grows
-- with the predicted level), so a single global multiplier is wrong -- a per-bucket table is not.
--
-- Bucket key = ROUND(predicted_log, 1): deterministic from the point prediction, so serving just
-- recomputes it (no stored bin edges). Built on TRAIN only (is_eval = FALSE). Serving:
--   point model -> predicted_log -> ROUND(.,1) -> look up p90_min in this table
--   (clamp the bucket to [MIN,MAX] per side for predictions beyond the trained range).
CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.delay_p90_calib` AS
WITH dep AS (
  SELECT 'dep' AS side, ROUND(predicted_dep_log, 1) AS plog_bucket, dep_delay_pos AS actual
  FROM ML.PREDICT(MODEL `disruptions.dep_delay_reg`,
    (SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, arr_delay_pos, is_eval)
     FROM `disruptions.training_calls` WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL))
),
arr AS (
  SELECT 'arr' AS side, ROUND(predicted_arr_log, 1) AS plog_bucket, arr_delay_pos AS actual
  FROM ML.PREDICT(MODEL `disruptions.arr_delay_reg`,
    (SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, is_eval)
     FROM `disruptions.training_calls` WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL))
),
u AS (SELECT * FROM dep UNION ALL SELECT * FROM arr)
SELECT
  side,
  plog_bucket,
  ROUND(EXP(plog_bucket) - 1, 2)                              AS pred_typ_min,
  COUNT(*)                                                    AS n,
  CAST(APPROX_QUANTILES(actual, 100)[OFFSET(50)] AS INT64)   AS p50_min,
  CAST(APPROX_QUANTILES(actual, 100)[OFFSET(90)] AS INT64)   AS p90_min,
  CAST(APPROX_QUANTILES(actual, 100)[OFFSET(95)] AS INT64)   AS p95_min
FROM u
GROUP BY side, plog_bucket;
