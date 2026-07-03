-- Per-ask-horizon Platt layers for early alerting on the departure side.
--
-- WHY: cancel_platt / dep_ge10_platt are fit at the training anchoring (~30 min before the
-- call), so the served p_impact_dep is only calibrated for that regime. Asked earlier (60/120/
-- 240 min before departure, or at message arrival with no observable train/line state) the
-- raw heads lose signal and the 30-min platt leaves them OVERCONFIDENT: reliability sags below
-- the diagonal and the threshold for 90 % precision drifts 0.62 -> 0.86-0.88 across horizons.
-- One logistic recentering per (head, horizon) fixes both: honest probabilities at every ask
-- time and a single operating threshold that means the same thing at every horizon.
--
-- INPUT: disruptions.leadtime_eval_calls (built by leadtime_eval.sql) -- the temporal holdout
-- re-anchored at lead L in {30, 60, 120, 240} plus the plan-ahead variant (lead_min = -1,
-- no observable state). Statement 1 scores it once with both heads into leadtime_scored so
-- the platt fits below don't each re-run ML.PREDICT.
--
-- FIT POPULATIONS: horizon L uses only calls reachable at that horizon (min_from_onset >= L --
-- the SX must exist when we ask); the plan variant uses every call (scoreable the moment the
-- message arrives). Same per-head conventions as create_cancel_calibration.sql /
-- create_exceedance_calibration.sql: cancel on all rows, dep_ge10 on dep_delay_pos IS NOT NULL.
-- No lead-30 platts here: the 30-min regime IS the training anchoring, already served by
-- cancel_platt / dep_ge10_platt (lead-30 rebuild reproduces training features exactly).
--
-- TRAILING WINDOW: fit ONLY on holdout data the heads did not train on (onset >= 2026-04-01).
-- Validated out-of-sample in eval/impact_eval.py: platts fit on onset 2026-04-01..2026-05-15,
-- tested on unseen onset >= 2026-05-16. Re-run leadtime_eval.sql + this file after any head
-- retrain (serving reads ML.WEIGHTS live; a stale fit silently mis-calibrates).
--
-- SERVING: pick the platt by minutes-until-departure at ask time (>= ~45 min -> lead60/120/240
-- bucket, call not yet reachable by realtime features -> plan), then the usual closed form
-- p_cal = sigmoid(b + a*logit(p_raw)) and composition p_impact_dep = p_cancel + (1-p_cancel)*p_dep10.
--
-- Run EACH statement as its own bq job (file + --nosynchronous_mode + bq wait).

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.leadtime_scored` AS
WITH inp AS (
  SELECT * FROM `ent-kixavvik-dev.disruptions.leadtime_eval_calls`
),
onset AS (
  SELECT situationNumber, MIN(DATE(SAFE.TIMESTAMP(creationTime))) AS onset_date
  FROM `ent-kixavvik-dev.disruptions.disruptions`
  WHERE participantRef = 'NSB'
  GROUP BY 1
),
clf AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_cancel_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`, (SELECT * FROM inp))
),
d10 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_dep10_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`, (SELECT * FROM inp))
)
SELECT
  e.situationNumber, e.jkey, e.stop_order, e.lead_min,
  o.onset_date, e.call_date, e.min_from_onset,
  e.call_cancelled, e.dep_delay_pos, e.is_terminal,
  c.p_cancel_raw, d.p_dep10_raw
FROM inp AS e
JOIN clf AS c USING (situationNumber, jkey, stop_order, lead_min)
JOIN d10 AS d USING (situationNumber, jkey, stop_order, lead_min)
LEFT JOIN onset AS o USING (situationNumber);

-- Cancellation head ---------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.cancel_platt_lead60`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['call_cancelled'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  call_cancelled
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 60 AND min_from_onset >= 60;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.cancel_platt_lead120`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['call_cancelled'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  call_cancelled
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 120 AND min_from_onset >= 120;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.cancel_platt_lead240`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['call_cancelled'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  call_cancelled
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 240 AND min_from_onset >= 240;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.cancel_platt_plan`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['call_cancelled'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  call_cancelled
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = -1;

-- Departure >=10 min head ---------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_lead60`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 10 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 60 AND min_from_onset >= 60 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_lead120`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 10 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 120 AND min_from_onset >= 120 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_lead240`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 10 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 240 AND min_from_onset >= 240 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_plan`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 10 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = -1 AND dep_delay_pos IS NOT NULL;
