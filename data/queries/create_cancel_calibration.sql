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
--
-- ONE LAYER PER POPULATION (2026-08-19). training_calls now carries unflagged calls too (see
-- has_situation in create_training_calls.sql), and the two populations are 47x apart on the
-- outcome this layer re-centers: the holdout runs 8.67 % cancelled on flagged calls against
-- 0.185 % on unflagged ones. A single logistic fit over both would be fit almost entirely on the
-- 6:1 unflagged majority and would drag the flagged probabilities down to a base rate that is
-- not theirs -- a Platt layer has exactly one intercept, and there is no value of it that serves
-- both. So cancel_platt keeps its meaning by gaining a filter, and cancel_platt_nosx is its twin.
--
-- THE FILTER ON THE EXISTING LAYER IS THE DANGEROUS PART OF THIS CHANGE. `WHERE is_eval = TRUE`
-- used to select the flagged holdout because the flagged holdout was all there was. It now
-- selects both populations, so WITHOUT the added `AND has_situation` this file silently
-- re-calibrates production alerting onto the unflagged base rate the next time anyone re-runs
-- it after a retrain -- no error, no schema change, just alerts that stop firing. Measured on
-- the same holdout, the flagged layer's own fit lands mean p 0.08661 against a 0.08669 base
-- rate; that is what a correct re-run reproduces.
--
-- What the split buys, out of sample (Platt fit on 2026-04-01..05-15, tested on the unseen
-- 05-16..06-19, cancel head, extended model): unflagged precision at the production 0.65 gate
-- goes 0.530 (old model, SX columns NULLed) -> 0.913, and the flagged side is unmoved. The
-- population does NOT want a lower threshold than 0.65 once it is encoded and calibrated
-- honestly -- see the has_situation notes in create_training_calls.sql.
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
     WHERE is_eval = TRUE AND has_situation))
);

-- The no-SX twin: same recipe, same holdout, the other population. Its rows reach the model with
-- has_situation = FALSE and the documented fillers in the SX columns, so this is a fit of the
-- realized outcome on what the model says about a call NOBODY published about -- which is a
-- different question from the one above, and gets its own answer.
CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.cancel_platt_nosx`
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
     WHERE is_eval = TRUE AND NOT has_situation))
);
