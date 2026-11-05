-- KIX-39 zero-shot gate: do the production rail heads, unchanged, rank and calibrate SJ Norge's
-- calls as well as they do Vy's? Scores the no-SX holdout of BOTH populations (call_date >=
-- 2026-04-01, the models' temporal holdout) through the cancel / dep_ge10 / arr_ge10 heads and the
-- no-SX Platt layer at the training anchoring (lead_bucket '30'), then composes p_impact_dep the
-- way predict_calls.sql does. Writes disruptions.sjn_zero_shot_scores; the summary is the SELECT in
-- docs/sjn/LEARNINGS.md.
CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.sjn_zero_shot_scores` AS
WITH input AS (
  SELECT 'SJN' AS pop, * FROM `disruptions.training_calls_sjn` WHERE is_eval
  UNION ALL
  SELECT 'VYG' AS pop, * FROM `disruptions.training_calls` WHERE is_eval AND NOT has_situation
),
-- The row key must be DETERMINISTIC: each ML.PREDICT below re-evaluates `keyed`, and a
-- ROW_NUMBER() OVER () numbers the rows differently every time, which silently pairs one call's
-- cancel score with another call's delay score (it did: every AUC came out ~0.49).
keyed AS (
  SELECT FARM_FINGERPRINT(CONCAT(pop, '|', jkey, '|', CAST(call_date AS STRING), '|',
                                 CAST(stop_order AS STRING))) AS rid, *
  FROM input
),
c AS (
  SELECT rid, (SELECT p.prob FROM UNNEST(predicted_call_cancelled_probs) p WHERE p.label) AS p_cancel_raw
  FROM ML.PREDICT(MODEL `disruptions.call_cancel_clf`, (SELECT * FROM keyed))
),
d10 AS (
  SELECT rid, (SELECT p.prob FROM UNNEST(predicted_y_ge_probs) p WHERE p.label) AS p_dep10_raw
  FROM ML.PREDICT(MODEL `disruptions.dep_ge10_clf`, (SELECT * FROM keyed))
),
a10 AS (
  SELECT rid, (SELECT p.prob FROM UNNEST(predicted_y_ge_probs) p WHERE p.label) AS p_arr10_raw
  FROM ML.PREDICT(MODEL `disruptions.arr_ge10_clf`, (SELECT * FROM keyed))
),
w AS (
  SELECT head, b, a FROM `disruptions.nosx_platt_weights` WHERE lead_bucket = '30'
)
SELECT
  k.rid, k.pop, k.jkey, k.stop_order, k.lineRef, k.stop_place, k.call_date, k.call_hour, k.is_terminal, k.is_origin,
  k.own_prev_delay, k.line_now_n,
  k.call_cancelled, k.dep_delay_pos, k.arr_delay_pos,
  c.p_cancel_raw, d10.p_dep10_raw, a10.p_arr10_raw,
  1/(1+EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(c.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_cancel_raw,1-1e-6),1e-6)))))) AS p_cancel,
  1/(1+EXP(-(wd.b + wd.a * LN(GREATEST(LEAST(d10.p_dep10_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(d10.p_dep10_raw,1-1e-6),1e-6)))))) AS p_dep10,
  1/(1+EXP(-(wa.b + wa.a * LN(GREATEST(LEAST(a10.p_arr10_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(a10.p_arr10_raw,1-1e-6),1e-6)))))) AS p_arr10
FROM keyed AS k
JOIN c USING (rid) JOIN d10 USING (rid) JOIN a10 USING (rid)
CROSS JOIN (SELECT b, a FROM w WHERE head = 'cancel')   AS wc
CROSS JOIN (SELECT b, a FROM w WHERE head = 'dep_ge10') AS wd
CROSS JOIN (SELECT b, a FROM w WHERE head = 'arr_ge10') AS wa;
