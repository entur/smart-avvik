-- Platt recalibration of the delay exceedance stacks: arrival (arr_ge{5,10,20,40}_clf) and
-- departure (dep_ge{5,10,20,40}_clf, the boarding-stop twins).
--
-- WHY: the exceedance classifiers train on data through 2026-03; delay levels shift seasonally
-- (the >=10 base rate is 14.7% in the train period vs 9.7% in spring 2026 on the arrival side;
-- 15.1% vs 9.9% on the departure side), which shows up as low-end over-prediction on raw scores.
-- Same recipe as cancel_platt: a logistic fit of the realized exceedance on the raw logit, per
-- threshold, over the post-training holdout.
-- (The v4-feature ge10 model was near-calibrated raw on the holdout; the Platt layer is cheap
-- insurance that also re-centers the weaker-signal thresholds, and refitting it on a trailing
-- window keeps the whole stack tracking the current regime without retraining trees.)
--
-- TRAILING WINDOW: fit ONLY on data the classifiers did not train on (is_eval = TRUE, onset >=
-- 2026-04-01). Re-run periodically -- and re-run AFTER any exceedance-model retrain (the Platt
-- coefficients are read live from ML.WEIGHTS at serving, so a stale fit silently mis-calibrates).
--
-- SERVING: predict_calls*.sql applies the closed form p_cal = sigmoid(b + a*logit(p_raw)) with
-- (a,b) from ML.WEIGHTS(arr_geN_platt / dep_geN_platt), then enforces cross-threshold
-- monotonicity (GREATEST cascade -- independent per-threshold fits can cross) before deriving
-- each side's interval and impact probability.
--
-- Run EACH statement as its own bq job (cumulative --maximum_bytes_billed kills later ones).

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT arr_delay_pos >= 5 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge5_clf`,
    (SELECT * FROM `disruptions.training_calls`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT arr_delay_pos >= 10 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge10_clf`,
    (SELECT * FROM `disruptions.training_calls`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT arr_delay_pos >= 20 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge20_clf`,
    (SELECT * FROM `disruptions.training_calls`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT arr_delay_pos >= 40 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.arr_ge40_clf`,
    (SELECT * FROM `disruptions.training_calls`
     WHERE is_eval = TRUE AND arr_delay_pos IS NOT NULL))
);

-- Departure side (boarding stop) ------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT dep_delay_pos >= 5 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge5_clf`,
    (SELECT * FROM `disruptions.training_calls`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT dep_delay_pos >= 10 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge10_clf`,
    (SELECT * FROM `disruptions.training_calls`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT dep_delay_pos >= 20 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge20_clf`,
    (SELECT * FROM `disruptions.training_calls`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  y
FROM (
  SELECT dep_delay_pos >= 40 AS y,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.dep_ge40_clf`,
    (SELECT * FROM `disruptions.training_calls`
     WHERE is_eval = TRUE AND dep_delay_pos IS NOT NULL))
);
