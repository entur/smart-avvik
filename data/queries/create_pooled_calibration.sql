-- Platt recalibration for the POOLED (NSB + GOA) model family: cancel_platt_pooled +
-- arr/dep_ge{5,10,20,40}_platt_pooled. Same recipe as create_cancel_calibration.sql /
-- create_exceedance_calibration.sql -- a logistic fit of the realized outcome on the raw
-- logit over the post-training holdout (is_eval = TRUE, onset >= 2026-04-01) -- but reading
-- training_calls_pooled and the _pooled models.
--
-- ONE SHARED FIT ACROSS OPERATORS: the holdout is ~95% NSB, so the platt coefficients are
-- effectively NSB-fit; the per-operator eval (docs/pooled-models) checks whether GOA rides
-- the same calibration acceptably. If GOA-only calibration is ever needed, fit per-operator
-- platts (WHERE operator = 'GOA') -- mirrors what the per-ask-horizon buckets do for lead time.
--
-- Fit-on-holdout caveat (same as prod): the eval reads precision/recall on the SAME holdout
-- the platt was fit on, so reliability reads slightly optimistic. Comparisons vs the prod
-- family are apples-to-apples -- prod platts are fit the same way.
--
-- Run EACH statement as its own bq job (cumulative --maximum_bytes_billed kills later ones).

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.cancel_platt_pooled`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['call_cancelled'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  call_cancelled
FROM (
  SELECT call_cancelled,
    (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.call_cancel_clf_pooled`,
    (SELECT * EXCEPT(situationNumber, jkey, call_date, dep_delay_pos, arr_delay_pos, is_eval)
     FROM `disruptions.training_calls_pooled`
     WHERE is_eval = TRUE))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge5_platt_pooled`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT arr_delay_pos >= 5 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge5_clf_pooled`,
    (SELECT * FROM `disruptions.training_calls_pooled`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge10_platt_pooled`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT arr_delay_pos >= 10 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge10_clf_pooled`,
    (SELECT * FROM `disruptions.training_calls_pooled`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge20_platt_pooled`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT arr_delay_pos >= 20 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge20_clf_pooled`,
    (SELECT * FROM `disruptions.training_calls_pooled`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge40_platt_pooled`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT arr_delay_pos >= 40 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge40_clf_pooled`,
    (SELECT * FROM `disruptions.training_calls_pooled`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

-- Departure side (boarding stop) ------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge5_platt_pooled`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT dep_delay_pos >= 5 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge5_clf_pooled`,
    (SELECT * FROM `disruptions.training_calls_pooled`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge10_platt_pooled`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT dep_delay_pos >= 10 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge10_clf_pooled`,
    (SELECT * FROM `disruptions.training_calls_pooled`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge20_platt_pooled`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT dep_delay_pos >= 20 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge20_clf_pooled`,
    (SELECT * FROM `disruptions.training_calls_pooled`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge40_platt_pooled`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT dep_delay_pos >= 40 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge40_clf_pooled`,
    (SELECT * FROM `disruptions.training_calls_pooled`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);
