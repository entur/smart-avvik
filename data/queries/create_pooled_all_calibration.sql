-- Platt recalibration for the ALL-OPERATOR pooled model family: cancel_platt_pooled_all +
-- arr/dep_ge{5,10,20,40}_platt_pooled_all. Same recipe as create_pooled_calibration.sql --
-- a logistic fit of the realized outcome on the raw logit over the post-training holdout
-- (is_eval = TRUE, onset >= 2026-04-01) -- with ONE addition:
--
-- OPERATOR-AWARE FIT: `operator` joins raw_logit as a second (categorical) input, giving
-- each operator its own intercept at a shared slope. The rail-only pooled family could get
-- away with one shared scalar fit (holdout ~95% NSB); here the bus slices are the MAJORITY
-- of the holdout with base rates 3-5x NSB's, and a shared intercept would drag NSB's
-- calibration off and mis-center every bus operator at the fixed 0.65 alert threshold.
-- Serving closed form becomes: p_cal = sigmoid(b + a*raw_logit + w[operator]) with w from
-- ML.WEIGHTS category_weights.
--
-- Fit-on-holdout caveat (same as prod): the eval reads precision/recall on the SAME holdout
-- the platt was fit on, so reliability reads slightly optimistic. Comparisons vs the prod
-- family are apples-to-apples -- prod platts are fit the same way.
--
-- Run EACH statement as its own bq job (cumulative --maximum_bytes_billed kills later ones).

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.cancel_platt_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['call_cancelled'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator,
  call_cancelled
FROM (
  SELECT operator, call_cancelled,
    (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.call_cancel_clf_pooled_all`,
    (SELECT * EXCEPT(situationNumber, jkey, call_date, dep_delay_pos, arr_delay_pos, is_eval)
     FROM `disruptions.training_calls_pooled_all`
     WHERE is_eval = TRUE))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge5_platt_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator,
  y
FROM (
  SELECT operator, arr_delay_pos >= 5 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge5_clf_pooled_all`,
    (SELECT * FROM `disruptions.training_calls_pooled_all`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge10_platt_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator,
  y
FROM (
  SELECT operator, arr_delay_pos >= 10 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge10_clf_pooled_all`,
    (SELECT * FROM `disruptions.training_calls_pooled_all`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge20_platt_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator,
  y
FROM (
  SELECT operator, arr_delay_pos >= 20 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge20_clf_pooled_all`,
    (SELECT * FROM `disruptions.training_calls_pooled_all`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge40_platt_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator,
  y
FROM (
  SELECT operator, arr_delay_pos >= 40 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge40_clf_pooled_all`,
    (SELECT * FROM `disruptions.training_calls_pooled_all`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

-- Departure side (boarding stop) ------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge5_platt_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator,
  y
FROM (
  SELECT operator, dep_delay_pos >= 5 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge5_clf_pooled_all`,
    (SELECT * FROM `disruptions.training_calls_pooled_all`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge10_platt_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator,
  y
FROM (
  SELECT operator, dep_delay_pos >= 10 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge10_clf_pooled_all`,
    (SELECT * FROM `disruptions.training_calls_pooled_all`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge20_platt_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator,
  y
FROM (
  SELECT operator, dep_delay_pos >= 20 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge20_clf_pooled_all`,
    (SELECT * FROM `disruptions.training_calls_pooled_all`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge40_platt_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator,
  y
FROM (
  SELECT operator, dep_delay_pos >= 40 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge40_clf_pooled_all`,
    (SELECT * FROM `disruptions.training_calls_pooled_all`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);
