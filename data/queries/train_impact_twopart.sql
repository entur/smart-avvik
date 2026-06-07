-- Two-part (hurdle) impact model. The impact label is floored at reopen_minutes,
-- so ~half the rows have impact == reopen (no recovery tail) and the rest have a
-- positive tail. A single regressor straddles that point mass badly; splitting it
-- models each part cleanly:
--
--   dur_init_tail_clf   classifier: P(tail) = P(impact_minutes > reopen_minutes)
--   dur_init_tail_size  regressor : LN(impact_minutes - reopen_minutes), trained
--                                   ONLY on tail rows (impact > reopen)
--
-- Deployable combined prediction (builds on the reopen MODEL, not the unknown true
-- reopen time):
--   impact_pred = reopen_pred + P(tail) * EXP(tail_size_ln_pred)
-- which is >= reopen_pred by construction (coherent with the dual message).
--
-- Both parts share the initial-version features and EXCEPT all keys/labels.

DECLARE impact_median FLOAT64;

-- Part 1: does a recovery tail exist? ------------------------------------------
CREATE OR REPLACE MODEL `disruptions.dur_init_tail_clf`
OPTIONS(
  model_type='BOOSTED_TREE_CLASSIFIER', input_label_cols=['has_tail'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes),
       CAST(impact_minutes > reopen_minutes AS INT64) AS has_tail
FROM `disruptions.training_initial`;

-- Part 2: how big is the tail, given there is one? -----------------------------
CREATE OR REPLACE MODEL `disruptions.dur_init_tail_size`
OPTIONS(
  model_type='BOOSTED_TREE_REGRESSOR', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes),
       LN(impact_minutes - reopen_minutes) AS label
FROM `disruptions.training_initial`
WHERE impact_minutes > reopen_minutes;

-- Evaluate: two-part vs the single impact model, on held-out eval rows ----------
SET impact_median = (SELECT APPROX_QUANTILES(impact_minutes, 2)[OFFSET(1)]
                     FROM `disruptions.training_initial` WHERE NOT is_eval);

WITH ev AS (SELECT * FROM `disruptions.training_initial` WHERE is_eval),
rp AS (
  SELECT situationNumber, impact_minutes AS actual, EXP(predicted_label) AS reopen_pred
  FROM ML.PREDICT(MODEL `disruptions.dur_init_reopen`, (SELECT * FROM ev))
),
cp AS (
  SELECT situationNumber,
         (SELECT prob FROM UNNEST(predicted_has_tail_probs) WHERE label = 1) AS p_tail
  FROM ML.PREDICT(MODEL `disruptions.dur_init_tail_clf`, (SELECT * FROM ev))
),
tp AS (
  SELECT situationNumber, EXP(predicted_label) AS tail_pred
  FROM ML.PREDICT(MODEL `disruptions.dur_init_tail_size`, (SELECT * FROM ev))
),
sp AS (
  SELECT situationNumber, impact_minutes AS actual, EXP(predicted_label) AS single_pred
  FROM ML.PREDICT(MODEL `disruptions.dur_init_impact`, (SELECT * FROM ev))
),
scored AS (
  SELECT rp.actual,
         rp.reopen_pred + cp.p_tail * tp.tail_pred AS twopart_pred,
         sp.single_pred,
         (SELECT reopen_minutes FROM ev e WHERE e.situationNumber = rp.situationNumber) AS reopen_actual
  FROM rp JOIN cp USING (situationNumber) JOIN tp USING (situationNumber) JOIN sp USING (situationNumber)
),
preds AS (
  SELECT 'twopart'        AS model, actual, twopart_pred AS pred FROM scored
  UNION ALL SELECT 'single',          actual, single_pred         FROM scored
  UNION ALL SELECT 'predict_reopen',  actual, CAST(reopen_actual AS FLOAT64) FROM scored
  UNION ALL SELECT 'naive_median',    actual, impact_median       FROM scored
)
SELECT
  model,
  COUNT(*)                                                       AS n,
  ROUND(AVG(ABS(pred - actual)), 1)                              AS mae_all,
  ROUND(AVG(IF(actual <= 240, ABS(pred - actual), NULL)), 1)     AS mae_le240,
  ROUND(APPROX_QUANTILES(ABS(pred - actual), 2)[OFFSET(1)], 1)   AS median_ae,
  ROUND(SQRT(AVG(POW(pred - actual, 2))), 1)                     AS rmse_all
FROM preds
GROUP BY model
ORDER BY mae_le240;
