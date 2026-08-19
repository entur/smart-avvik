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
-- stated_min, line_pre_*, net_pre_*, line_now_*, own_prev_delay). The delay regressors' EXCEPT
-- lists below spell those columns out so a verbatim retrain reproduces the validated models --
-- do NOT silently absorb new columns into an old model's contract. The cancellation classifier
-- (1.) and the exceedance classifiers (4., 5.) are validated WITH the new features (measured
-- A/Bs, 2026-07-02); the regressors (2., 3.) remain on the frozen pre-propagation contract.
--
-- The two delay columns MUST be excepted from every classifier: dep_delay_pos/arr_delay_pos are
-- NULL exactly when call_cancelled is TRUE, i.e. a perfect leak of the cancellation label.
--
-- has_situation IS A FEATURE OF EVERY HEAD (2026-08-19), including the two regressors whose
-- contract is otherwise frozen. It is excepted NOWHERE, deliberately: training_calls now carries
-- the ~5.9 M rail calls no SX message named (86 % of the table), and the flag is the only thing
-- that tells a head which population a row is from. Take it out and the SX columns' fillers
-- become indistinguishable from real values -- which is the exact defect this change exists to
-- remove, reintroduced silently. A head trained without it will still build and still score.
--
-- THE DILUTION GATE PASSED, WHICH IS NOT WHAT THE BUS PRECEDENT PREDICTED. Pooling 63 % foreign
-- rows into the NSB models cost 0.005 AUC and forced the two-family split
-- (docs/pooled-models-bus/LEARNINGS.md); here the foreign share is 86 % and the cost is zero --
-- flagged-holdout AUC 0.9603 before and 0.9603 after, precision at the 0.65 gate 0.944 -> 0.941.
-- The difference is that this pool arrives WITH a column naming it, so the trees spend one split
-- and then fit each population separately, instead of averaging two regimes into one. Read that
-- as the rule: pool a population you can name, split one you cannot.
--
-- A DEDICATED NO-SX FAMILY WAS MEASURED AND IS NOT WORTH IT -- don't re-propose it. Trained on
-- the unflagged calls alone with only the 11 features that exist without a situation, it scored
-- AUC 0.8902 on the unflagged holdout against the pooled model's 0.8875: inside the noise on
-- 1 590 positives, in exchange for a second model family, a second feature contract and a second
-- thing to retrain. The pooled head wins on everything that is not a rounding error.
--
-- SIZE, at 7.24 M rows (5.5x the pre-2026-08-19 table). Measured on the promotion run: the
-- cancellation classifier below billed 285 GB (~$1.78) and took 81 min; the ten 50-iteration
-- heads are ~40 GB / ~15 min each.
--
-- enable_global_explain IS NOW FALSE EVERYWHERE (2026-08-19). It was TRUE, and at the old size
-- that already made a retrain a 5.4 h job with the explain phase -- not the training -- as the
-- 5-hour part; at 5.5x the rows it would make one an overnight job nobody schedules, which in
-- practice means the heads stop being retrained at all. ML.FEATURE_IMPORTANCE needs none of it:
-- it reads gain and split counts straight off a boosted-tree model however it was trained, and
-- it is what produced the attributions quoted in this file. Turn the option back on only for a
-- run that needs Shapley-style per-row explanations specifically, and expect to leave it
-- overnight.

-- 1. Cancellation classifier --------------------------------------------------------------
-- Contract: ALL feature columns incl. the 2026-07-02 category/text/propagation set. Measured
-- A/B vs the frozen 16-feature contract (holdout, onset >= 2026-04-01): AUC 0.8855 -> 0.9504,
-- log-loss 0.1965 -> 0.1484. Top new features: line_now_cancel (attribution 0.43, the #1
-- feature overall -- observed cancellations on the line raise P(cancel) for its coming calls)
-- and own_prev_delay (0.23). The no-line-state slice is a statistical tie, so the plan-ahead
-- regime does not regress. REFIT cancel_platt after any retrain of this model.
--
-- HYPERPARAMETERS TUNED 2026-07-02 (step-4 headroom check, docs/model-hardening/LEARNINGS.md):
-- local XGBoost/LightGBM showed +1.3pt AUC over the BQML defaults; this config (found by local
-- tuning, validated in BQML as exp_cancel_clf_tuned) recovers ~80% of it in-warehouse:
-- holdout AUC 0.9504 -> 0.9606, log-loss 0.1484 -> 0.1275. The residual ~0.3pt is library-level
-- (native categorical splits), unreachable in BQML. NOTE: BQML bills boosted-tree training per
-- iteration -- 300 iterations scans ~66 GB (~$0.41) vs ~$0.07 for the 50-iteration models below.
-- WALL-TIME WARNING: with enable_global_explain the explain phase dominates at this size --
-- the 2026-07-03 promotion run took 5.4 h (training itself ~22 min; billed only ~$0.46, the
-- explain phase is slot-invisible). Plan retrains overnight, or drop the option if attribution
-- inspection isn't needed.
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`
OPTIONS(
  model_type        = 'BOOSTED_TREE_CLASSIFIER',
  input_label_cols  = ['call_cancelled'],
  max_iterations    = 300,
  learn_rate        = 0.05,
  max_tree_depth    = 8,
  subsample         = 0.8,
  colsample_bytree  = 0.8,
  early_stop        = FALSE,
  data_split_method = 'NO_SPLIT',
  enable_global_explain = FALSE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, dep_delay_pos, arr_delay_pos, is_eval)
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
  enable_global_explain = FALSE
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
  enable_global_explain = FALSE
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
  enable_global_explain = FALSE
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
  enable_global_explain = FALSE
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
  enable_global_explain = FALSE
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
  enable_global_explain = FALSE
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
  enable_global_explain = FALSE
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
  enable_global_explain = FALSE
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
  enable_global_explain = FALSE
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
  enable_global_explain = FALSE
) AS
SELECT * EXCEPT(situationNumber, jkey, call_date, call_cancelled, dep_delay_pos, arr_delay_pos, is_eval,
                planned, num_lines, is_terminal, is_origin, stop_place),
       dep_delay_pos >= 40 AS y_ge
FROM `disruptions.training_calls`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;
