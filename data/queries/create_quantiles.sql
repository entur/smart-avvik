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
-- Offsets are calibrated PER CATEGORY — the dominant driver of how long a disruption
-- runs and of how wide the uncertainty is (a signal fault spreads very differently
-- from a staffing gap). A category with at least `min_support` TRAIN residuals gets
-- its own offsets; everything sparser — and any UNKNOWN / previously-unseen category
-- at serve time — falls back to the global row, category = '__ALL__'. Offsets are
-- computed on TRAIN rows only (leakage-safe) and validated on the eval rows below;
-- cov_pP should land near 0.50 / 0.80 / 0.90 within each category.
--
-- Table shape: one row per (target, category) that clears `min_support`, plus one
-- (target, '__ALL__') global fallback row. Consumers (predict_*_duration.sql,
-- predict_initial_new_impact.sql) join offsets on category and COALESCE to '__ALL__'.
-- Keep them in sync with this schema — there are now MANY rows per target, so a bare
-- `WHERE target = 'reopen'` join would fan every prediction into N copies.

DECLARE min_support INT64 DEFAULT 100;  -- min TRAIN residuals for a category to earn its own offsets

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.duration_quantiles` AS
WITH resid AS (
  SELECT 'reopen' AS target, category, LN(reopen_minutes) - predicted_label AS e
  FROM ML.PREDICT(MODEL `disruptions.dur_init_reopen`,
       (SELECT * FROM `disruptions.training_initial` WHERE NOT is_eval))
  UNION ALL
  SELECT 'impact' AS target, category, LN(impact_minutes) - predicted_label AS e
  FROM ML.PREDICT(MODEL `disruptions.dur_init_impact`,
       (SELECT * FROM `disruptions.training_initial` WHERE NOT is_eval))
),
-- per-category offsets, kept only where there is enough support for a stable P90
by_cat AS (
  SELECT target, category, COUNT(*) AS n, APPROX_QUANTILES(e, 100) AS qs
  FROM resid
  GROUP BY target, category
  HAVING COUNT(*) >= min_support
),
-- global fallback per target (every TRAIN residual, regardless of category)
glob AS (
  SELECT target, '__ALL__' AS category, COUNT(*) AS n, APPROX_QUANTILES(e, 100) AS qs
  FROM resid
  GROUP BY target
)
SELECT
  target,
  category,
  n,
  qs[OFFSET(50)] AS off_p50,
  qs[OFFSET(80)] AS off_p80,
  qs[OFFSET(90)] AS off_p90
FROM (SELECT * FROM by_cat UNION ALL SELECT * FROM glob);

-- Validate coverage + typical interval widths on the held-out eval rows, PER category
-- and applying the same global fallback the consumers use. cov_pP should sit near
-- 0.50 / 0.80 / 0.90; `via` shows whether a category used its own offsets or '__ALL__'.
WITH ev AS (SELECT * FROM `disruptions.training_initial` WHERE is_eval),
preds AS (
  SELECT 'reopen' AS target, category, reopen_minutes AS actual, predicted_label AS pred_ln
  FROM ML.PREDICT(MODEL `disruptions.dur_init_reopen`, (SELECT * FROM ev))
  UNION ALL
  SELECT 'impact' AS target, category, impact_minutes AS actual, predicted_label AS pred_ln
  FROM ML.PREDICT(MODEL `disruptions.dur_init_impact`, (SELECT * FROM ev))
),
glob AS (
  SELECT target, off_p50, off_p80, off_p90
  FROM `disruptions.duration_quantiles` WHERE category = '__ALL__'
),
off AS (
  SELECT
    p.target,
    p.category,
    p.actual,
    p.pred_ln,
    IF(qc.category IS NULL, '__ALL__', 'own') AS via,
    COALESCE(qc.off_p50, qg.off_p50) AS off_p50,
    COALESCE(qc.off_p80, qg.off_p80) AS off_p80,
    COALESCE(qc.off_p90, qg.off_p90) AS off_p90
  FROM preds AS p
  LEFT JOIN `disruptions.duration_quantiles` AS qc
    ON qc.target = p.target AND qc.category = p.category   -- never matches the '__ALL__' row
  JOIN glob AS qg ON qg.target = p.target
)
SELECT
  target,
  category,
  ANY_VALUE(via) AS via,
  COUNT(*) AS n,
  ROUND(AVG(IF(actual <= EXP(pred_ln + off_p50), 1, 0)), 3) AS cov_p50,
  ROUND(AVG(IF(actual <= EXP(pred_ln + off_p80), 1, 0)), 3) AS cov_p80,
  ROUND(AVG(IF(actual <= EXP(pred_ln + off_p90), 1, 0)), 3) AS cov_p90,
  ROUND(APPROX_QUANTILES(EXP(pred_ln + off_p50), 2)[OFFSET(1)], 0) AS med_p50_min,
  ROUND(APPROX_QUANTILES(EXP(pred_ln + off_p80), 2)[OFFSET(1)], 0) AS med_p80_min,
  ROUND(APPROX_QUANTILES(EXP(pred_ln + off_p90), 2)[OFFSET(1)], 0) AS med_p90_min
FROM off
GROUP BY target, category
ORDER BY target, n DESC;
