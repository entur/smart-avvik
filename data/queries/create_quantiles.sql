-- Quantile / prediction-interval output for the duration models, so comms can say
-- "trains running again in ~X min, up to ~Z min" instead of a bare point estimate.
--
-- BQML boosted trees train with squared loss only — there is no native
-- pinball/quantile objective. So we calibrate empirically: both models predict in
-- LN space, and the LN-residual  e = LN(actual) - predicted_label  is approximately
-- homoscedastic in log space. Its quantiles are additive offsets in LN space, i.e.
-- multiplicative factors in minutes:
--
--   P-quantile prediction = EXP(predicted_label + offset_pP)
--
-- Offsets are computed on TRAIN rows only (leakage-safe) and validated on the eval
-- rows below — cov_pP should land near 0.50 / 0.80 / 0.90. If a target is poorly
-- calibrated, recompute offsets within buckets (e.g. category) instead of globally.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.duration_quantiles` AS
WITH resid AS (
  SELECT 'reopen' AS target, LN(reopen_minutes) - predicted_label AS e
  FROM ML.PREDICT(MODEL `disruptions.dur_init_reopen`,
       (SELECT * FROM `disruptions.training_initial` WHERE NOT is_eval))
  UNION ALL
  SELECT 'impact' AS target, LN(impact_minutes) - predicted_label AS e
  FROM ML.PREDICT(MODEL `disruptions.dur_init_impact`,
       (SELECT * FROM `disruptions.training_initial` WHERE NOT is_eval))
),
q AS (SELECT target, APPROX_QUANTILES(e, 100) AS qs FROM resid GROUP BY target)
SELECT target,
  qs[OFFSET(50)] AS off_p50,
  qs[OFFSET(80)] AS off_p80,
  qs[OFFSET(90)] AS off_p90
FROM q;

-- Validate coverage + show typical interval widths on the held-out eval rows.
WITH ev AS (SELECT * FROM `disruptions.training_initial` WHERE is_eval),
preds AS (
  SELECT 'reopen' AS target, reopen_minutes AS actual, predicted_label AS pred_ln
  FROM ML.PREDICT(MODEL `disruptions.dur_init_reopen`, (SELECT * FROM ev))
  UNION ALL
  SELECT 'impact' AS target, impact_minutes AS actual, predicted_label AS pred_ln
  FROM ML.PREDICT(MODEL `disruptions.dur_init_impact`, (SELECT * FROM ev))
)
SELECT
  p.target,
  COUNT(*) AS n,
  ROUND(AVG(IF(actual <= EXP(pred_ln + q.off_p50), 1, 0)), 3) AS cov_p50,
  ROUND(AVG(IF(actual <= EXP(pred_ln + q.off_p80), 1, 0)), 3) AS cov_p80,
  ROUND(AVG(IF(actual <= EXP(pred_ln + q.off_p90), 1, 0)), 3) AS cov_p90,
  ROUND(APPROX_QUANTILES(EXP(pred_ln + q.off_p50), 2)[OFFSET(1)], 0) AS med_p50_min,
  ROUND(APPROX_QUANTILES(EXP(pred_ln + q.off_p80), 2)[OFFSET(1)], 0) AS med_p80_min,
  ROUND(APPROX_QUANTILES(EXP(pred_ln + q.off_p90), 2)[OFFSET(1)], 0) AS med_p90_min
FROM preds p
JOIN `disruptions.duration_quantiles` q USING (target)
GROUP BY p.target
ORDER BY p.target;
