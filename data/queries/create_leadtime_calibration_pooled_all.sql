-- Per-ask-horizon Platt layers for the ALL-OPERATOR pooled family -- ALL per-call heads.
--
-- Port of create_leadtime_calibration.sql to the _pooled_all family, with the operator-aware
-- fit from create_pooled_all_calibration.sql. WHY (same as rail): every base _pooled_all platt is
-- fit at the training anchoring (~30 min before the call), so probabilities asked earlier
-- (60/120/240 min before, or at message arrival with no observable train/line state) are
-- mis-calibrated. One logistic recentering per (head, horizon) restores honest probabilities so a
-- single 0.65 alert threshold means the same thing at every ask time and horizon.
--
-- TWO DELTAS vs the rail file:
--   1. Every model is a _pooled_all head, and every platt fit adds `operator` as a second
--      (categorical) input -> per-operator intercept at a shared slope. Serving closed form is
--      sigmoid(b + a*raw_logit + w[operator]) with w from ML.WEIGHTS category_weights, exactly as
--      the base _pooled_all platts (create_pooled_all_calibration.sql).
--   2. jkeys are NOT unique across operators (bus ServiceJourney refs are not date-scoped), so the
--      score-join carries `operator` in its key: (operator, situationNumber, jkey, stop_order,
--      lead_min). Rail's file could key on the 4 non-operator columns; here that would cross-join.
--
-- INPUT: disruptions.leadtime_eval_pooled_all (built by leadtime_eval_pooled_all.sql) -- the
-- pooled temporal holdout (is_eval, onset >= 2026-04-01) re-anchored at lead L in {30,60,120,240}
-- plus the plan-ahead variant (lead_min = -1, no observable state). It is holdout-only by
-- construction, so no onset-date trailing filter is needed here (unlike the rail file's `onset` CTE).
-- Statement 1 scores it once with all nine _pooled_all heads into leadtime_scored_pooled_all so the
-- platt fits below don't each re-run ML.PREDICT.
--
-- FIT POPULATIONS: horizon L uses only calls reachable at that horizon (min_from_onset >= L -- the
-- SX must exist when we ask); the plan variant uses every call. cancel on all rows, each exceedance
-- head on rows where that side's delay is observed. No lead-30 models: the 30-min regime IS the
-- training anchoring, already served by the base *_platt_pooled_all layers.
--
-- SERVING: predict_calls_bus.sql buckets each call by minutes-until-call at scoring time
-- (<45 -> the base 30-min layers, then 1.5x-horizon boundaries 90/180/360, beyond 6 h -> plan) and
-- picks every layer -- alerts, ge-stack columns and intervals -- from that bucket, applying the
-- per-operator w for that (head, bucket).
--
-- Run EACH statement as its own bq job (file + --nosynchronous_mode + bq wait). Re-run after any
-- _pooled_all head retrain (serving reads ML.WEIGHTS live; a stale fit silently mis-calibrates).

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all` AS
WITH inp AS (
  SELECT * FROM `ent-smartavvik-dev.disruptions.leadtime_eval_pooled_all`
),
clf AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_cancel_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.call_cancel_clf_pooled_all`, (SELECT * FROM inp))
),
a5 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.arr_ge5_clf_pooled_all`, (SELECT * FROM inp))
),
a10 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.arr_ge10_clf_pooled_all`, (SELECT * FROM inp))
),
a20 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.arr_ge20_clf_pooled_all`, (SELECT * FROM inp))
),
a40 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.arr_ge40_clf_pooled_all`, (SELECT * FROM inp))
),
d5 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.dep_ge5_clf_pooled_all`, (SELECT * FROM inp))
),
d10 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.dep_ge10_clf_pooled_all`, (SELECT * FROM inp))
),
d20 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.dep_ge20_clf_pooled_all`, (SELECT * FROM inp))
),
d40 AS (
  SELECT operator, situationNumber, jkey, stop_order, lead_min,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.dep_ge40_clf_pooled_all`, (SELECT * FROM inp))
)
SELECT
  e.operator, e.situationNumber, e.jkey, e.stop_order, e.lead_min,
  e.call_date, e.min_from_onset,
  e.call_cancelled, e.dep_delay_pos, e.arr_delay_pos, e.is_terminal, e.is_origin,
  c.p_cancel_raw,
  a5.p_raw  AS p_arr5_raw,  a10.p_raw AS p_arr10_raw,
  a20.p_raw AS p_arr20_raw, a40.p_raw AS p_arr40_raw,
  d5.p_raw  AS p_dep5_raw,  d10.p_raw AS p_dep10_raw,
  d20.p_raw AS p_dep20_raw, d40.p_raw AS p_dep40_raw
FROM inp AS e
JOIN clf AS c  USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN a5        USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN a10       USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN a20       USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN a40       USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN d5        USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN d10       USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN d20       USING (operator, situationNumber, jkey, stop_order, lead_min)
JOIN d40       USING (operator, situationNumber, jkey, stop_order, lead_min);

-- Cancellation head ---------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.cancel_platt_lead60_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['call_cancelled'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, call_cancelled
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 60 AND min_from_onset >= 60;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.cancel_platt_lead120_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['call_cancelled'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, call_cancelled
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 120 AND min_from_onset >= 120;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.cancel_platt_lead240_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['call_cancelled'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, call_cancelled
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 240 AND min_from_onset >= 240;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.cancel_platt_plan_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['call_cancelled'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_cancel_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, call_cancelled
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = -1;


-- arr ge5 head --------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge5_platt_lead60_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 5 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 60 AND min_from_onset >= 60 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge5_platt_lead120_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 5 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 120 AND min_from_onset >= 120 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge5_platt_lead240_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 5 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 240 AND min_from_onset >= 240 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge5_platt_plan_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 5 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = -1 AND arr_delay_pos IS NOT NULL;


-- arr ge10 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge10_platt_lead60_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 10 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 60 AND min_from_onset >= 60 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge10_platt_lead120_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 10 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 120 AND min_from_onset >= 120 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge10_platt_lead240_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 10 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 240 AND min_from_onset >= 240 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge10_platt_plan_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 10 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = -1 AND arr_delay_pos IS NOT NULL;


-- arr ge20 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge20_platt_lead60_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 20 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 60 AND min_from_onset >= 60 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge20_platt_lead120_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 20 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 120 AND min_from_onset >= 120 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge20_platt_lead240_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 20 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 240 AND min_from_onset >= 240 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge20_platt_plan_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 20 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = -1 AND arr_delay_pos IS NOT NULL;


-- arr ge40 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge40_platt_lead60_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 40 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 60 AND min_from_onset >= 60 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge40_platt_lead120_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 40 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 120 AND min_from_onset >= 120 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge40_platt_lead240_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 40 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 240 AND min_from_onset >= 240 AND arr_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.arr_ge40_platt_plan_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_arr40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, arr_delay_pos >= 40 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = -1 AND arr_delay_pos IS NOT NULL;


-- dep ge5 head --------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge5_platt_lead60_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 5 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 60 AND min_from_onset >= 60 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge5_platt_lead120_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 5 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 120 AND min_from_onset >= 120 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge5_platt_lead240_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 5 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 240 AND min_from_onset >= 240 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge5_platt_plan_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep5_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 5 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = -1 AND dep_delay_pos IS NOT NULL;


-- dep ge10 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge10_platt_lead60_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 10 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 60 AND min_from_onset >= 60 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge10_platt_lead120_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 10 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 120 AND min_from_onset >= 120 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge10_platt_lead240_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 10 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 240 AND min_from_onset >= 240 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge10_platt_plan_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep10_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 10 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = -1 AND dep_delay_pos IS NOT NULL;


-- dep ge20 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge20_platt_lead60_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 20 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 60 AND min_from_onset >= 60 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge20_platt_lead120_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 20 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 120 AND min_from_onset >= 120 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge20_platt_lead240_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 20 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 240 AND min_from_onset >= 240 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge20_platt_plan_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep20_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 20 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = -1 AND dep_delay_pos IS NOT NULL;


-- dep ge40 head -------------------------------------------------------------------------------

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge40_platt_lead60_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 40 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 60 AND min_from_onset >= 60 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge40_platt_lead120_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 40 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 120 AND min_from_onset >= 120 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge40_platt_lead240_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 40 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = 240 AND min_from_onset >= 240 AND dep_delay_pos IS NOT NULL;

CREATE OR REPLACE MODEL `ent-smartavvik-dev.disruptions.dep_ge40_platt_plan_pooled_all`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['y'], data_split_method='NO_SPLIT') AS
SELECT
  LN(GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6) / (1 - GREATEST(LEAST(p_dep40_raw, 1 - 1e-6), 1e-6))) AS raw_logit,
  operator, dep_delay_pos >= 40 AS y
FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
WHERE lead_min = -1 AND dep_delay_pos IS NOT NULL;
