-- Trains the per-call models from disruptions.training_calls (full-population, "any
-- situation" family). Run EACH statement as its own bq job: --maximum_bytes_billed is
-- cumulative across a multi-statement script and will kill the later CREATE MODELs.
--
-- Split: train on is_eval = FALSE (onset < 2026-04-01); the temporal holdout (is_eval = TRUE)
-- is the TEST set, evaluated separately with ML.EVALUATE. Probabilities are left uncalibrated
-- by class weighting (no auto_class_weights); Platt layers fit on the holdout re-center them
-- (cancel_platt for the classifier, arr_ge*/dep_ge*_platt for the exceedance stacks).
--
-- FEATURE CONTRACTS ARE EXPLICIT: training_calls gained new columns on 2026-07-02 (category,
-- stated_min, line_pre_*, net_pre_*, line_now_*, own_prev_delay). The three original models'
-- EXCEPT lists below now spell those columns out so a verbatim retrain reproduces the validated
-- models -- do NOT silently absorb new columns into an old model's contract. The exceedance
-- classifiers (4., 5.) are the only models validated WITH the new features.
--
-- The two delay columns MUST be excepted from every classifier: dep_delay_pos/arr_delay_pos are
-- NULL exactly when call_cancelled is TRUE, i.e. a perfect leak of the cancellation label.

-- 1. Cancellation classifier --------------------------------------------------------------
-- Contract frozen at the original 16 features (2026-06-20). Retraining WITH the propagation
-- features is untested for this model -- likely a lift (see LEARNINGS next steps), but do it as
-- a measured A/B, not by accident.
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['call_cancelled'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, dep_delay_pos, arr_delay_pos, is_eval,
                category, stated_min, line_pre_n, line_pre_delay, line_pre_cancel,
                net_pre_delay, net_pre_cancel, line_now_n, line_now_delay, line_now_cancel,
                own_prev_delay)
FROM `disruptions.training_calls`
WHERE is_eval = FALSE;

-- 2. Departure-delay regressor (boarding side; non-cancelled calls with a departure) -------
-- The delay target is heavily right-skewed (median 2 min, mean 5), so a squared-error
-- regressor on raw minutes chases the mean and loses to "predict the median" on MAE. Train on
-- LN(1 + delay) instead -> the prediction back-transforms (EXP(pred) - 1) to ~the conditional
-- median = the "typical delay" the product wants. Serving must apply that back-transform.
-- Pruned feature set (11 features, validated 2026-07-02): planned, num_lines, is_terminal,
-- is_origin, stop_place had ~0 global-explain attribution. Contract frozen; the 2026-07-02
-- feature columns are excepted for the same reason as the classifier.
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_delay_reg`
OPTIONS(
  model_type        = 'BOOSTED_TREE_REGRESSOR',
  input_label_cols  = ['dep_log'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place,
                category, stated_min, line_pre_n, line_pre_delay, line_pre_cancel,
                net_pre_delay, net_pre_cancel, line_now_n, line_now_delay, line_now_cancel,
                own_prev_delay),
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
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place,
                category, stated_min, line_pre_n, line_pre_delay, line_pre_cancel,
                net_pre_delay, net_pre_cancel, line_now_n, line_now_delay, line_now_cancel,
                own_prev_delay),
       LN(1 + arr_delay_pos) AS arr_log
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;

-- 4. Arrival-delay EXCEEDANCE stack: P(arr_delay >= t), t in {5, 10, 20, 40} ---------------
-- The 2026-07-02 reframing (docs/model-hardening/LEARNINGS.md): "will the arrival be >= t min
-- late?" is a classification question, and it is far more learnable than delay magnitude
-- (>=10 holdout AUC 0.910 with the propagation features vs 0.781 for the regressor-as-ranker).
-- The four calibrated probabilities form a discrete survival curve per call, from which serving
-- derives a delay interval (p50/p90 crossings) and the alert probability
-- p_impact = p_cancel + (1-p_cancel) * P(>=10).
--
-- Feature set = the validated v4 experiment set minus the dead text flags (f_bus/f_slow/f_closed,
-- attribution <= 0.0006): the frozen 11 regressor features + category + stated_min +
-- line_pre_* + net_pre_* + line_now_* + own_prev_delay (attribution 0.47 -- the top feature).
-- Population mirrors arr_delay_reg: non-cancelled calls with an arrival. Cancellation is handled
-- by call_cancel_clf; these are P(>= t | train runs).
-- All four MUST share one feature contract so serving computes the feature block once.
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge5_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['y_ge'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place),
       arr_delay_pos >= 5 AS y_ge
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['y_ge'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place),
       arr_delay_pos >= 10 AS y_ge
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge20_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['y_ge'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place),
       arr_delay_pos >= 20 AS y_ge
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge40_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['y_ge'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place),
       arr_delay_pos >= 40 AS y_ge
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;

-- 5. Departure-delay EXCEEDANCE stack: P(dep_delay >= t), t in {5, 10, 20, 40} ---------------
-- Boarding-stop twins of the arrival stack (4.): "will my train LEAVE >= t min late?". Same
-- feature contract, same thresholds (dep base rates track arr almost exactly: holdout >=10 is
-- 9.9% vs arr 9.7%; >=40 is 0.61% -- thin, tail is extrapolation territory on both sides).
-- Population mirrors dep_delay_reg: non-cancelled calls with a departure (terminals excluded).
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge5_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['y_ge'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place),
       dep_delay_pos >= 5 AS y_ge
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['y_ge'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place),
       dep_delay_pos >= 10 AS y_ge
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge20_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['y_ge'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place),
       dep_delay_pos >= 20 AS y_ge
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge40_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['y_ge'],
  max_iterations    = 50,
  early_stop        = TRUE,
  enable_global_explain = TRUE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place),
       dep_delay_pos >= 40 AS y_ge
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;
