-- p90 ("bad case") delay quantile for the ALL-OPERATOR pooled family, as a calibration layer over
-- the _pooled_all dep/arr point regressors. Port of create_delay_p90_calib.sql with ONE change:
-- the bucket key is OPERATOR-AWARE -- (side, operator, ROUND(predicted_log,1)) -- because the delay
-- tail is heteroscedastic AND differs by operator (a bus network's late-delay tail is not a rail
-- tail). A `*` global row per (side, plog_bucket) is also emitted as a fallback for sparse
-- operator/bucket cells; predict_calls_bus.sql prefers the operator row and falls back to `*`.
--
-- Bucket key = ROUND(predicted_log, 1): deterministic from the point prediction, so serving just
-- recomputes it. Built on TRAIN only (is_eval = FALSE). `operator` is a model feature, so it is
-- present in the ML.PREDICT output and can be grouped on directly.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.delay_p90_calib_pooled_all` AS
WITH dep AS (
  SELECT 'dep' AS side, operator, ROUND(predicted_dep_log, 1) AS plog_bucket, dep_delay_pos AS actual
  FROM ML.PREDICT(MODEL `disruptions.dep_delay_reg_pooled_all`,
    (SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, arr_delay_pos, is_eval)
     FROM `disruptions.training_calls_pooled_all` WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL))
),
arr AS (
  SELECT 'arr' AS side, operator, ROUND(predicted_arr_log, 1) AS plog_bucket, arr_delay_pos AS actual
  FROM ML.PREDICT(MODEL `disruptions.arr_delay_reg_pooled_all`,
    (SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, is_eval)
     FROM `disruptions.training_calls_pooled_all` WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL))
),
u AS (SELECT * FROM dep UNION ALL SELECT * FROM arr),
per_op AS (
  SELECT
    side, operator, plog_bucket,
    ROUND(EXP(plog_bucket) - 1, 2)                            AS pred_typ_min,
    COUNT(*)                                                  AS n,
    CAST(APPROX_QUANTILES(actual, 100)[OFFSET(50)] AS INT64) AS p50_min,
    CAST(APPROX_QUANTILES(actual, 100)[OFFSET(90)] AS INT64) AS p90_min,
    CAST(APPROX_QUANTILES(actual, 100)[OFFSET(95)] AS INT64) AS p95_min
  FROM u
  GROUP BY side, operator, plog_bucket
),
glob AS (
  SELECT
    side, '*' AS operator, plog_bucket,
    ROUND(EXP(plog_bucket) - 1, 2)                            AS pred_typ_min,
    COUNT(*)                                                  AS n,
    CAST(APPROX_QUANTILES(actual, 100)[OFFSET(50)] AS INT64) AS p50_min,
    CAST(APPROX_QUANTILES(actual, 100)[OFFSET(90)] AS INT64) AS p90_min,
    CAST(APPROX_QUANTILES(actual, 100)[OFFSET(95)] AS INT64) AS p95_min
  FROM u
  GROUP BY side, plog_bucket
)
SELECT * FROM per_op
UNION ALL
SELECT * FROM glob;
