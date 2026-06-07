-- Trains the two passenger-message models from training_initial and evaluates both
-- against naive baselines on the held-out eval rows.
--
--   dur_init_reopen  -- predicts reopen_minutes ("trains running again in ~X min")
--   dur_init_impact  -- predicts impact_minutes ("traffic impacted for ~Y min")
--
-- Both: BOOSTED_TREE_REGRESSOR on LN(label) (right-skewed target), custom is_eval
-- split, the SAME features — and crucially EXCEPT(reopen_minutes, impact_minutes)
-- so neither post-hoc label leaks into the other model.
--
-- Baselines per target: train-set median, the operator's declared window, and (for
-- impact) "just reuse the predicted reopen time" — the bar the impact model must
-- clear to justify being a second model at all.

DECLARE reopen_median FLOAT64;
DECLARE impact_median FLOAT64;

SET reopen_median = (SELECT APPROX_QUANTILES(reopen_minutes, 2)[OFFSET(1)]
                     FROM `disruptions.training_initial` WHERE NOT is_eval);
SET impact_median = (SELECT APPROX_QUANTILES(impact_minutes, 2)[OFFSET(1)]
                     FROM `disruptions.training_initial` WHERE NOT is_eval);

-- Model A: time to reopening ---------------------------------------------------
CREATE OR REPLACE MODEL `disruptions.dur_init_reopen`
OPTIONS(
  model_type='BOOSTED_TREE_REGRESSOR', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes), LN(reopen_minutes) AS label
FROM `disruptions.training_initial`;

-- Model B: time until impact subsides ------------------------------------------
CREATE OR REPLACE MODEL `disruptions.dur_init_impact`
OPTIONS(
  model_type='BOOSTED_TREE_REGRESSOR', input_label_cols=['label'],
  data_split_method='CUSTOM', data_split_col='is_eval',
  max_iterations=50, early_stop=TRUE, enable_global_explain=TRUE) AS
SELECT * EXCEPT(situationNumber, reopen_minutes, impact_minutes), LN(impact_minutes) AS label
FROM `disruptions.training_initial`;

-- Feature importance (run each on its own; a script returns only the last result):
-- SELECT * FROM ML.GLOBAL_EXPLAIN(MODEL `disruptions.dur_init_reopen`) ORDER BY attribution DESC;
-- SELECT * FROM ML.GLOBAL_EXPLAIN(MODEL `disruptions.dur_init_impact`) ORDER BY attribution DESC;

-- Evaluation on held-out eval rows, in real minutes ----------------------------
WITH ev AS (SELECT * FROM `disruptions.training_initial` WHERE is_eval),
preds AS (
  -- reopen target
  SELECT 'reopen' AS target, 'model' AS model, reopen_minutes AS actual, EXP(predicted_label) AS pred
  FROM ML.PREDICT(MODEL `disruptions.dur_init_reopen`, (SELECT * FROM ev))
  UNION ALL
  SELECT 'reopen', 'naive_median', reopen_minutes, reopen_median FROM ev
  UNION ALL
  SELECT 'reopen', 'naive_declared', reopen_minutes, COALESCE(declared_window_minutes, reopen_median) FROM ev
  -- impact target
  UNION ALL
  SELECT 'impact', 'model', impact_minutes, EXP(predicted_label)
  FROM ML.PREDICT(MODEL `disruptions.dur_init_impact`, (SELECT * FROM ev))
  UNION ALL
  SELECT 'impact', 'naive_median', impact_minutes, impact_median FROM ev
  UNION ALL
  SELECT 'impact', 'predict_reopen', impact_minutes, CAST(reopen_minutes AS FLOAT64) FROM ev
)
SELECT
  target, model,
  COUNT(*)                                                     AS n_eval,
  ROUND(AVG(ABS(pred - actual)), 1)                            AS mae_min,
  ROUND(APPROX_QUANTILES(ABS(pred - actual), 2)[OFFSET(1)], 1) AS median_ae_min,
  ROUND(SQRT(AVG(POW(pred - actual, 2))), 1)                   AS rmse_min,
  ROUND(AVG(ABS(pred - actual) / actual), 3)                   AS mape
FROM preds
GROUP BY target, model
ORDER BY target, mae_min;
