-- Platt recalibration of the per-call cancellation classifier (call_cancel_clf).
--
-- WHY: call_cancel_clf trains on data through 2026-03 (call cancel rate ~13.9%) but recent
-- conditions run lower (~10% in spring 2026 -- a seasonal shift). Its raw P(cancel) therefore
-- over-predicts a little at the low end and under-predicts at the high end. A logistic fit of
-- the realized outcome on the raw score (Platt scaling) re-centers and re-shapes the curve.
--
-- TRAILING WINDOW: fit ONLY on data the classifier did not train on -- the post-training holdout
-- (is_eval = TRUE, i.e. onset >= 2026-04-01, ~the last 80 days as of 2026-06-20). Re-run this
-- periodically so the calibration tracks the current base rate; each run uses the most recent
-- ~quarter of labeled calls. Validated by fitting on 2026-04..05 and testing on unseen 2026-06:
-- log-loss 0.164 -> 0.157, mean P(cancel) 0.113 -> 0.103 vs actual 0.095, with the high-
-- confidence deciles markedly closer to the realized rate.
--
-- SERVING (two steps):
--   p_raw = P(cancel) from ML.PREDICT(call_cancel_clf, <features>)
--   p_cal = ML.PREDICT(cancel_platt, raw_logit => LN(p_raw/(1-p_raw)))   -- clamp p_raw to (0,1)
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.cancel_platt`
OPTIONS(
  model_type       = 'LOGISTIC_REG',
  input_label_cols = ['call_cancelled'],
  data_split_method = 'NO_SPLIT'
) AS
SELECT
  LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))) AS raw_logit,
  call_cancelled
FROM (
  SELECT call_cancelled,
    (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p
  FROM ML.PREDICT(MODEL `disruptions.call_cancel_clf`,
    (SELECT * EXCEPT(situationNumber, jkey, call_date, dep_delay_pos, arr_delay_pos, is_eval)
     FROM `disruptions.training_calls`
     WHERE is_eval = TRUE))
);
