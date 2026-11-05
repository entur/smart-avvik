-- Trains the ALL-OPERATOR pooled per-call model family (rail: NSB, GOA; bus/local:
-- KOL, SOF, NOR, SKY, ATB, OST, FIN, MOR, RUT, TRO, GCO) from
-- disruptions.training_calls_pooled_all. Experiment stage of the multi-operator plan
-- (docs/operator-census/LEARNINGS.md): these models serve NOTHING until the per-operator
-- eval gates on NSB parity vs the prod family.
--
-- Identical to train_call_models.sql except:
--   * every model reads training_calls_pooled_all and gains the `operator` feature
--     -- the pooling column is deliberately absent from every EXCEPT list, including the
--     otherwise-frozen delay regressors' (a pooled model without the operator column could
--     not separate the operators where their dynamics diverge);
--   * model names carry the _pooled_all suffix;
--   * enable_global_explain = FALSE -- the explain phase dominated the 2026-07-03 promotion
--     run (5.4 h wall vs ~22 min training); attribution inspection isn't needed to gate the
--     experiment. Re-enable if a pooled model is ever promoted and attributions are wanted.
--
-- Run EACH statement as its own bq job: --maximum_bytes_billed is cumulative across a
-- multi-statement script and will kill the later CREATE MODELs.
--
-- Split: train on is_eval = FALSE (onset < 2026-04-01); the temporal holdout (is_eval = TRUE)
-- is the TEST set. Probabilities are left uncalibrated; the _pooled_all platt layers
-- (create_pooled_all_calibration.sql) re-center them on the holdout. The two delay columns MUST
-- be excepted from every classifier: dep_delay_pos/arr_delay_pos are NULL exactly when
-- call_cancelled is TRUE, i.e. a perfect leak of the cancellation label.

-- 1. Cancellation classifier (tuned 2026-07-02 hyperparameters, see train_call_models.sql) --
CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.call_cancel_clf_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE;

-- 2. Departure-delay regressor (LN(1+delay) target; frozen 11-feature contract + operator) --
CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_delay_reg_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;

-- 3. Arrival-delay regressor ----------------------------------------------------------------
CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_delay_reg_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;

-- 4. Arrival-delay EXCEEDANCE stack: P(arr_delay >= t), t in {5, 10, 20, 40} -----------------
CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge5_clf_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge10_clf_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge20_clf_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge40_clf_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE AND arr_delay_pos IS NOT NULL;

-- 5. Departure-delay EXCEEDANCE stack: P(dep_delay >= t), t in {5, 10, 20, 40} ---------------
CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge5_clf_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge10_clf_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge20_clf_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge40_clf_pooled_all`
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
FROM `disruptions.training_calls_pooled_all`
WHERE is_eval = FALSE AND dep_delay_pos IS NOT NULL;
