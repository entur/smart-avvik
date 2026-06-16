-- Cancellation-focused variant: binary CANCELLED vs OTHER (GO + DELAYED).
-- Answers the high-value question directly -- "will this trip be cancelled?" --
-- and isolates the cancellation signal instead of diluting it into DISRUPTED.
--
-- WHY: in the 3-class model CANCELLED had the best recall (~0.59) and the
-- cleanest monotonic probability ranking; merging it with DELAYED (the binary
-- GO/DISRUPTED model) dropped AUC 0.734 -> 0.675 and gained no recall. This tests
-- whether training a dedicated boundary recovers / beats that signal.
--
-- All rows are kept so the eval population matches the other two models; the
-- label is remapped at training time via SELECT * REPLACE so trip_outcomes_initial
-- stays 3-class.
--
-- IMBALANCE: CANCELLED is only ~3.3% of rows -- auto_class_weights=TRUE stays on,
-- and the surfaced P(CANCELLED) will again be inflated (recalibrate before showing
-- a passenger a number). ACCURACY IS A TRAP -- judge on CANCELLED recall/precision,
-- on AUC (vs the 0.675 binary / 0.734 three-class), and on calibration.
--
-- is_eval is the leakage-safe split passed as DATA_SPLIT_COL so it is never a
-- feature; situationNumber and journey are identifiers and excluded.
--
-- NOTE: a script returns only the final statement's rows in `bq`; run steps 2-5
-- individually (or use the console) to see each result.

-- 1. Model -------------------------------------------------------------------
CREATE OR REPLACE MODEL `disruptions.trip_outcome_clf_initial_cancelled`
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
       REPLACE (IF(outcome = 'CANCELLED', 'CANCELLED', 'OTHER') AS outcome)
FROM `disruptions.trip_outcomes_initial`;

-- 2. Overall metrics on the held-out eval split -----------------------------
SELECT * FROM ML.EVALUATE(MODEL `disruptions.trip_outcome_clf_initial_cancelled`);

-- 3. Confusion matrix on the eval rows ---------------------------------------
SELECT *
FROM ML.CONFUSION_MATRIX(MODEL `disruptions.trip_outcome_clf_initial_cancelled`,
       (SELECT * EXCEPT(situationNumber, journey)
               REPLACE (IF(outcome = 'CANCELLED', 'CANCELLED', 'OTHER') AS outcome)
        FROM `disruptions.trip_outcomes_initial` WHERE is_eval));

-- 4. Per-class precision & recall (the metrics that matter) ------------------
WITH p AS (
  SELECT IF(outcome = 'CANCELLED', 'CANCELLED', 'OTHER') AS actual, predicted_outcome AS pred
  FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf_initial_cancelled`,
        (SELECT * EXCEPT(situationNumber, journey)
                REPLACE (IF(outcome = 'CANCELLED', 'CANCELLED', 'OTHER') AS outcome)
         FROM `disruptions.trip_outcomes_initial` WHERE is_eval))
),
cls AS (SELECT class FROM UNNEST(['OTHER','CANCELLED']) AS class)
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
    IF(outcome = 'CANCELLED', 'CANCELLED', 'OTHER') AS actual,
    (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'CANCELLED') AS p_cancelled
  FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf_initial_cancelled`,
        (SELECT * EXCEPT(situationNumber, journey)
                REPLACE (IF(outcome = 'CANCELLED', 'CANCELLED', 'OTHER') AS outcome)
         FROM `disruptions.trip_outcomes_initial` WHERE is_eval))
)
SELECT
  ROUND(p_cancelled, 1) AS p_bucket,
  COUNT(*) AS n,
  ROUND(AVG(IF(actual = 'CANCELLED', 1, 0)), 3) AS actual_cancelled_rate
FROM p
GROUP BY p_bucket
ORDER BY p_bucket;
