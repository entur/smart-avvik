-- Per-ask-horizon Platt layers for early alerting -- ALL per-call heads.
--
-- WHY: every platt layer in create_cancel_calibration.sql / create_exceedance_calibration.sql
-- is fit at the training anchoring (~30 min before the call), so the served probabilities are
-- only calibrated for that regime. Asked earlier (60/120/240 min before the call, or at message
-- arrival with no observable train/line state) the raw heads lose signal and the 30-min layers
-- leave them OVERCONFIDENT: reliability sags below the diagonal and the threshold for 90 %
-- precision drifts 0.62 -> 0.86-0.88 across horizons. One logistic recentering per
-- (head, horizon) fixes it: honest probabilities at every ask time, so a single operating
-- threshold means the same thing at every horizon, and the survival-curve intervals stay
-- honest for far-ahead departures.
--
-- INPUT: disruptions.leadtime_eval_calls (built by leadtime_eval.sql) -- the temporal holdout
-- re-anchored at lead L in {30, 60, 120, 240} plus the plan-ahead variant (lead_min = -1,
-- no observable state). Statement 1 scores it once with all nine heads into leadtime_scored
-- so the platt fits below don't each re-run ML.PREDICT.
--
-- FIT POPULATIONS: horizon L uses only calls reachable at that horizon (min_from_onset >= L --
-- the SX must exist when we ask); the plan variant uses every call (scoreable the moment the
-- message arrives). Same per-head conventions as the 30-min files: cancel on all rows,
-- exceedance heads on rows where that side's delay is observed. No lead-30 models here: the
-- 30-min regime IS the training anchoring, already served by the standard *_platt layers
-- (the lead-30 rebuild reproduces training features exactly).
--
-- TRAILING WINDOW: fit ONLY on holdout data the heads did not train on (onset >= 2026-04-01).
-- The cancel + dep_ge10 pair was validated OUT-of-sample in eval/impact_eval.py (fit onset
-- 2026-04-01..2026-05-15, test 2026-05-16+): fixed threshold 0.65 holds >= 91 % precision at
-- every horizon (the shared 30-min layer gave 47-94 %). The remaining heads use the identical
-- recipe and were checked A/B the same way (calibration-in-the-large per head x horizon).
-- Re-run leadtime_eval.sql + this file after any head retrain (serving reads ML.WEIGHTS live;
-- a stale fit silently mis-calibrates).
--
-- SERVING: predict_calls*.sql buckets each call by minutes-until-call at scoring time
-- (<45 -> the standard 30-min layers, then 1.5x-horizon boundaries 90/180/360, beyond 6 h ->
-- plan) and picks every layer -- alerts, ge-stack columns and intervals -- from that bucket.
--
-- POPULATION: these 36 layers are the FLAGGED population's, and they stay that way without a
-- filter. training_calls gained unflagged rows on 2026-08-19 (has_situation, see
-- create_training_calls.sql), but leadtime_eval.sql reaches them through
-- `JOIN callkeys USING (situationNumber, ...)` and an unflagged row's situationNumber is NULL,
-- which never matches -- verified 2026-08-19: 262 647 flagged holdout rows in, zero unflagged.
-- So unlike create_cancel_calibration.sql and create_exceedance_calibration.sql, this file needed
-- no defensive predicate. Do not read that as "the split does not apply here":
--
-- A NO-SX SERVING PATH NEEDS ITS OWN 36. Every layer here exists so one fixed threshold means the
-- same thing at every ask horizon, and the unflagged population's probabilities decay with the
-- horizon for the same reason (own_prev_delay and line_now_* go stale) while sitting on a base
-- rate ~12x lower. Serving no-SX calls through these layers would be the shared-layer mistake one
-- file over, just per horizon. The build is mostly mechanical -- for an unflagged call "lead"
-- collapses to minutes-until-call, which predict_calls.sql already buckets on -- but
-- leadtime_eval.sql's re-anchoring is keyed on the SITUATION, so it needs a second branch that
-- recovers call_ts straight from rail_calls on (jkey, stop_order, call_date). Not built: nothing
-- serves no-SX scores yet, and 36 layers nobody reads would go stale before they were used.
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
a5 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge5_clf`, (SELECT * FROM inp))
),
a10 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf`, (SELECT * FROM inp))
),
a20 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge20_clf`, (SELECT * FROM inp))
),
a40 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge40_clf`, (SELECT * FROM inp))
),
d5 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge5_clf`, (SELECT * FROM inp))
),
d10 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`, (SELECT * FROM inp))
),
d20 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge20_clf`, (SELECT * FROM inp))
),
d40 AS (
  SELECT situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge40_clf`, (SELECT * FROM inp))
)
SELECT
  e.situationNumber, e.jkey, e.stop_order, e.lead_min,
  o.onset_date, e.call_date, e.min_from_onset,
  e.call_cancelled, e.dep_delay_pos, e.arr_delay_pos, e.is_terminal, e.is_origin,
  c.p_cancel_raw,
  a5.p_raw  AS p_arr5_raw,  a10.p_raw AS p_arr10_raw,
  a20.p_raw AS p_arr20_raw, a40.p_raw AS p_arr40_raw,
  d5.p_raw  AS p_dep5_raw,  d10.p_raw AS p_dep10_raw,
  d20.p_raw AS p_dep20_raw, d40.p_raw AS p_dep40_raw
FROM inp AS e
JOIN clf AS c  USING (situationNumber, jkey, stop_order, lead_min)
JOIN a5        USING (situationNumber, jkey, stop_order, lead_min)
JOIN a10       USING (situationNumber, jkey, stop_order, lead_min)
JOIN a20       USING (situationNumber, jkey, stop_order, lead_min)
JOIN a40       USING (situationNumber, jkey, stop_order, lead_min)
JOIN d5        USING (situationNumber, jkey, stop_order, lead_min)
JOIN d10       USING (situationNumber, jkey, stop_order, lead_min)
JOIN d20       USING (situationNumber, jkey, stop_order, lead_min)
JOIN d40       USING (situationNumber, jkey, stop_order, lead_min)
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


-- arr ge5 head --------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt_lead60`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 5 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 60 AND min_from_onset >= 60 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt_lead120`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 5 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 120 AND min_from_onset >= 120 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt_lead240`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 5 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 240 AND min_from_onset >= 240 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt_plan`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 5 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = -1 AND arr_delay_pos IS NOT NULL;


-- arr ge10 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_lead60`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 10 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 60 AND min_from_onset >= 60 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_lead120`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 10 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 120 AND min_from_onset >= 120 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_lead240`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 10 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 240 AND min_from_onset >= 240 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_plan`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 10 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = -1 AND arr_delay_pos IS NOT NULL;


-- arr ge20 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt_lead60`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 20 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 60 AND min_from_onset >= 60 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt_lead120`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 20 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 120 AND min_from_onset >= 120 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt_lead240`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 20 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 240 AND min_from_onset >= 240 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt_plan`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 20 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = -1 AND arr_delay_pos IS NOT NULL;


-- arr ge40 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt_lead60`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 40 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 60 AND min_from_onset >= 60 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt_lead120`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 40 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 120 AND min_from_onset >= 120 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt_lead240`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 40 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 240 AND min_from_onset >= 240 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt_plan`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  arr_delay_pos >= 40 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = -1 AND arr_delay_pos IS NOT NULL;


-- dep ge5 head --------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt_lead60`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 5 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 60 AND min_from_onset >= 60 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt_lead120`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 5 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 120 AND min_from_onset >= 120 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt_lead240`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 5 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 240 AND min_from_onset >= 240 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt_plan`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 5 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = -1 AND dep_delay_pos IS NOT NULL;


-- dep ge10 head -------------------------------------------------------------------------------

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


-- dep ge20 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt_lead60`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 20 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 60 AND min_from_onset >= 60 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt_lead120`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 20 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 120 AND min_from_onset >= 120 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt_lead240`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 20 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 240 AND min_from_onset >= 240 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt_plan`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 20 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = -1 AND dep_delay_pos IS NOT NULL;


-- dep ge40 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt_lead60`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 40 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 60 AND min_from_onset >= 60 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt_lead120`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 40 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 120 AND min_from_onset >= 120 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt_lead240`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 40 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = 240 AND min_from_onset >= 240 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt_plan`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  dep_delay_pos >= 40 AS y
FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
WHERE lead_min = -1 AND dep_delay_pos IS NOT NULL;
