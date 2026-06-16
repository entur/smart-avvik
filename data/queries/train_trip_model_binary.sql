-- Binary variant of train_trip_model_initial.sql: collapses DELAYED + CANCELLED
-- into a single DISRUPTED class, leaving GO untouched. Predicts "will this trip
-- run normally, or not?" for the passenger wait / alternative / WFH recommendation.
--
-- WHY: in the 3-class model DELAYED was the weakest class (precision ~0.36,
-- recall ~0.47) because it sits on an ambiguous GO<->DELAYED boundary. Merging
-- it into DISRUPTED removes that boundary; the positive class is larger and
-- less ambiguous, so positive-class recall/precision should improve.
--
-- The label is remapped here at training time via SELECT * REPLACE -- the
-- trip_outcomes_initial table is left as 3-class so the two models can coexist.
--
-- IMBALANCE: GO is still ~80% of rows, so auto_class_weights=TRUE stays on and
-- ACCURACY IS STILL A TRAP -- judge on positive-class (DISRUPTED) recall/precision
-- and on the calibration of P(DISRUPTED).
--
-- is_eval is the leakage-safe split (all rows of a situationNumber on one side),
-- passed as DATA_SPLIT_COL so it is never a feature. situationNumber and journey
-- are identifiers and excluded; boarding_stop and lineRef stay as features.
--
-- NOTE: a script returns only the final statement's rows in `bq`; run steps 2-5
-- individually (or use the console) to see each result.

-- 1. Model -------------------------------------------------------------------
CREATE OR REPLACE MODEL `disruptions.trip_outcome_clf_initial_binary`
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
       REPLACE (IF(outcome = 'GO', 'GO', 'DISRUPTED') AS outcome)
FROM `disruptions.trip_outcomes_initial`;

-- 2. Overall (macro) metrics on the held-out eval split ----------------------
SELECT * FROM ML.EVALUATE(MODEL `disruptions.trip_outcome_clf_initial_binary`);

-- 3. Confusion matrix on the eval rows ---------------------------------------
SELECT *
FROM ML.CONFUSION_MATRIX(MODEL `disruptions.trip_outcome_clf_initial_binary`,
       (SELECT * EXCEPT(situationNumber, journey)
               REPLACE (IF(outcome = 'GO', 'GO', 'DISRUPTED') AS outcome)
        FROM `disruptions.trip_outcomes_initial` WHERE is_eval));

-- 4. Per-class precision & recall (the metrics that matter) ------------------
WITH p AS (
  SELECT IF(outcome = 'GO', 'GO', 'DISRUPTED') AS actual, predicted_outcome AS pred
  FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf_initial_binary`,
        (SELECT * EXCEPT(situationNumber, journey)
                REPLACE (IF(outcome = 'GO', 'GO', 'DISRUPTED') AS outcome)
         FROM `disruptions.trip_outcomes_initial` WHERE is_eval))
),
cls AS (SELECT class FROM UNNEST(['GO','DISRUPTED']) AS class)
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

-- 5. Calibration of the probability you surface: P(DISRUPTED) vs actual rate --
WITH p AS (
  SELECT
    IF(outcome = 'GO', 'GO', 'DISRUPTED') AS actual,
    (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'DISRUPTED') AS p_disrupted
  FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf_initial_binary`,
        (SELECT * EXCEPT(situationNumber, journey)
                REPLACE (IF(outcome = 'GO', 'GO', 'DISRUPTED') AS outcome)
         FROM `disruptions.trip_outcomes_initial` WHERE is_eval))
)
SELECT
  ROUND(p_disrupted, 1) AS p_bucket,
  COUNT(*) AS n,
  ROUND(AVG(IF(actual = 'DISRUPTED', 1, 0)), 3) AS actual_disrupted_rate
FROM p
GROUP BY p_bucket
ORDER BY p_bucket;
