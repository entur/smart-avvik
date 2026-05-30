-- Trains the per-trip outcome classifier (3 classes: GO / DELAYED / CANCELLED)
-- for the passenger wait / alternative / WFH recommendation, and evaluates it
-- the right way for an imbalanced problem.
--
-- IMBALANCE: GO is ~86% of rows; CANCELLED (~4%) and DELAYED (~10%) are the
-- actionable classes. auto_class_weights=TRUE keeps GO from swamping them.
-- ACCURACY IS A TRAP (all-GO scores ~86%) -- judge on per-class recall (esp.
-- CANCELLED) and on the calibration of the probabilities you surface.
--
-- is_eval is the leakage-safe split (all rows of a situationNumber on one side),
-- passed as DATA_SPLIT_COL so it is never a feature. situationNumber and journey
-- are identifiers and excluded; boarding_stop and lineRef stay as features.
--
-- NOTE: a script returns only the final statement's rows in `bq`; run steps 2-5
-- individually (or use the console) to see each result.

-- 1. Model -------------------------------------------------------------------
CREATE OR REPLACE MODEL `disruptions.trip_outcome_clf`
OPTIONS(
  model_type            = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols      = ['outcome'],
  auto_class_weights    = TRUE,
  data_split_method     = 'CUSTOM',
  data_split_col        = 'is_eval',
  max_iterations        = 50,
  early_stop            = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, journey)
FROM `disruptions.trip_outcomes`;

-- 2. Overall (macro) metrics on the held-out eval split ----------------------
SELECT * FROM ML.EVALUATE(MODEL `disruptions.trip_outcome_clf`);

-- 3. Confusion matrix on the eval rows ---------------------------------------
SELECT *
FROM ML.CONFUSION_MATRIX(MODEL `disruptions.trip_outcome_clf`,
       (SELECT * EXCEPT(situationNumber, journey)
        FROM `disruptions.trip_outcomes` WHERE is_eval));

-- 4. Per-class precision & recall (the metrics that matter) ------------------
WITH p AS (
  SELECT outcome AS actual, predicted_outcome AS pred
  FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf`,
        (SELECT * EXCEPT(situationNumber, journey)
         FROM `disruptions.trip_outcomes` WHERE is_eval))
),
cls AS (SELECT class FROM UNNEST(['GO','DELAYED','CANCELLED']) AS class)
SELECT
  c.class,
  COUNTIF(p.actual = c.class) AS support,
  ROUND(SAFE_DIVIDE(COUNTIF(p.pred = c.class AND p.actual = c.class),
                    COUNTIF(p.actual = c.class)), 3) AS recall,
  ROUND(SAFE_DIVIDE(COUNTIF(p.pred = c.class AND p.actual = c.class),
                    COUNTIF(p.pred = c.class)), 3)   AS precision
FROM cls AS c CROSS JOIN p
GROUP BY c.class
ORDER BY support DESC;

-- 5. Calibration of the probability you surface: P(CANCELLED) vs actual rate --
WITH p AS (
  SELECT
    outcome AS actual,
    (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'CANCELLED') AS p_cancelled
  FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf`,
        (SELECT * EXCEPT(situationNumber, journey)
         FROM `disruptions.trip_outcomes` WHERE is_eval))
)
SELECT
  ROUND(p_cancelled, 1) AS p_bucket,
  COUNT(*) AS n,
  ROUND(AVG(IF(actual = 'CANCELLED', 1, 0)), 3) AS actual_cancelled_rate
FROM p
GROUP BY p_bucket
ORDER BY p_bucket;
