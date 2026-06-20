-- Trains the three per-call models from disruptions.training_calls (full-population, "any
-- situation" family). Run EACH statement as its own bq job: --maximum_bytes_billed is
-- cumulative across a multi-statement script and will kill the later CREATE MODELs.
--
-- Split: train on is_eval = FALSE (onset < 2026-04-01); the temporal holdout (is_eval = TRUE)
-- is the TEST set, evaluated separately with ML.EVALUATE. Probabilities are left uncalibrated
-- by class weighting (no auto_class_weights) because the product surfaces P(cancelled) directly.
--
-- Features = everything except the keys, the three labels, and the split flag. The two delay
-- columns MUST be excepted from the classifier: dep_delay_pos/arr_delay_pos are NULL exactly
-- when call_cancelled is TRUE, i.e. a perfect leak of the cancellation label.

-- 1. Cancellation classifier --------------------------------------------------------------
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['call_cancelled'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, dep_delay_pos, arr_delay_pos, is_eval)
FROM `disruptions.training_calls`
WHERE is_eval = FALSE;

-- 2. Departure-delay regressor (boarding side; non-cancelled calls with a departure) -------
-- The delay target is heavily right-skewed (median 2 min, mean 5), so a squared-error
-- regressor on raw minutes chases the mean and loses to "predict the median" on MAE. Train on
-- LN(1 + delay) instead -> the prediction back-transforms (EXP(pred) - 1) to ~the conditional
-- median = the "typical delay" the product wants. Serving must apply that back-transform.
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_delay_reg`
OPTIONS(
  model_type        = 'BOOSTED_TREE_REGRESSOR',
  input_label_cols  = ['dep_log'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval),
       LN(1 + dep_delay_pos) AS dep_log
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;

-- 3. Arrival-delay regressor (destination side; non-cancelled calls with an arrival) -------
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_delay_reg`
OPTIONS(
  model_type        = 'BOOSTED_TREE_REGRESSOR',
  input_label_cols  = ['arr_log'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval),
       LN(1 + arr_delay_pos) AS arr_log
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;
