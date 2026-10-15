-- KIX-39 zero-shot gate, per ask horizon: the eval_sjn_zero_shot.sql comparison re-anchored at the
-- leads leadtime_eval.sql uses (30 / 60 / 120 / 240 min, and -1 = 'plan', no observable train or
-- line state). The SJN rows are built here with leadtime_eval.sql's own re-anchoring, reading
-- sjn_calls where it reads rail_calls; the Vy rows are the no-SX rows of the existing
-- leadtime_eval_calls. Each lead is calibrated with ITS no-SX Platt bucket, as serving does.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.sjn_horizon_scores` AS
WITH
joined AS (
  SELECT t.*, COALESCE(c.aimed_dep, c.aimed_arr) AS call_ts
  FROM `ent-kixavvik-dev.disruptions.training_calls_sjn` AS t
  JOIN `ent-kixavvik-dev.disruptions.sjn_calls` AS c USING (jkey, stop_order, call_date)
  WHERE t.is_eval
),
expanded AS (
  SELECT j.*, lead_min FROM joined AS j CROSS JOIN UNNEST([30, 60, 120, 240]) AS lead_min
),
linehour AS (
  SELECT lineRef, TIMESTAMP_TRUNC(aimed_dep, HOUR) AS h, COUNT(*) AS n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS d,
         ROUND(AVG(CAST(COALESCE(call_cancelled, FALSE) AS INT64)), 3) AS c
  FROM `ent-kixavvik-dev.disruptions.sjn_calls`
  WHERE aimed_dep IS NOT NULL
  GROUP BY 1, 2
),
own AS (
  SELECT e.jkey, e.stop_order, e.call_date, e.lead_min,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)] AS own_prev
  FROM (SELECT DISTINCT jkey, stop_order, call_ts, call_date, lead_min FROM expanded) AS e
  JOIN `ent-kixavvik-dev.disruptions.sjn_calls` AS r2
    ON r2.jkey = e.jkey
   AND r2.call_date BETWEEN DATE_SUB(e.call_date, INTERVAL 1 DAY) AND e.call_date
   AND r2.stop_order < e.stop_order
   AND COALESCE(r2.aimed_dep, r2.aimed_arr) <= TIMESTAMP_SUB(e.call_ts, INTERVAL e.lead_min MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT COALESCE(r2.call_cancelled, FALSE)
  GROUP BY 1, 2, 3, 4
),
sjn AS (
  SELECT 'SJN' AS pop,
    e.* EXCEPT(call_ts, line_now_n, line_now_delay, line_now_cancel, own_prev_delay),
    IFNULL(lh.n, 0) AS line_now_n, IFNULL(lh.d, -1) AS line_now_delay,
    IFNULL(lh.c, -1) AS line_now_cancel, IFNULL(o.own_prev, -1) AS own_prev_delay
  FROM expanded AS e
  LEFT JOIN linehour AS lh
    ON lh.lineRef = e.lineRef
   AND lh.h = TIMESTAMP_TRUNC(TIMESTAMP_SUB(e.call_ts, INTERVAL (e.lead_min + 30) MINUTE), HOUR)
  LEFT JOIN own AS o USING (jkey, stop_order, call_date, lead_min)
  UNION ALL
  SELECT 'SJN' AS pop,
    j.* EXCEPT(call_ts, line_now_n, line_now_delay, line_now_cancel, own_prev_delay),
    -1 AS lead_min, 0 AS line_now_n, -1.0 AS line_now_delay, -1.0 AS line_now_cancel, -1 AS own_prev_delay
  FROM joined AS j
),
input AS (
  SELECT * FROM sjn
  UNION ALL
  SELECT 'VYG' AS pop, * FROM `ent-kixavvik-dev.disruptions.leadtime_eval_calls` WHERE NOT has_situation
),
-- deterministic key: every ML.PREDICT re-evaluates `keyed` (see eval_sjn_zero_shot.sql)
keyed AS (
  SELECT FARM_FINGERPRINT(CONCAT(pop, '|', jkey, '|', CAST(call_date AS STRING), '|',
                                 CAST(stop_order AS STRING), '|', CAST(lead_min AS STRING))) AS rid,
         IF(lead_min = -1, 'plan', CAST(lead_min AS STRING)) AS lead_bucket, *
  FROM input
),
c AS (
  SELECT rid, (SELECT p.prob FROM UNNEST(predicted_call_cancelled_probs) p WHERE p.label) AS pc
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`, (SELECT * FROM keyed))
),
d10 AS (
  SELECT rid, (SELECT p.prob FROM UNNEST(predicted_y_ge_probs) p WHERE p.label) AS pd
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`, (SELECT * FROM keyed))
),
w AS (SELECT head, lead_bucket, b, a FROM `ent-kixavvik-dev.disruptions.nosx_platt_weights`)
SELECT
  k.pop, k.lead_bucket, k.jkey, k.stop_order, k.lineRef, k.stop_place, k.call_date, k.is_terminal,
  k.call_cancelled, k.dep_delay_pos,
  1/(1+EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(c.pc,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.pc,1-1e-6),1e-6)))))) AS p_cancel,
  1/(1+EXP(-(wd.b + wd.a * LN(GREATEST(LEAST(d10.pd,1-1e-6),1e-6)/(1-GREATEST(LEAST(d10.pd,1-1e-6),1e-6)))))) AS p_dep10
FROM keyed AS k
JOIN c USING (rid) JOIN d10 USING (rid)
JOIN w AS wc ON wc.head = 'cancel'   AND wc.lead_bucket = k.lead_bucket
JOIN w AS wd ON wd.head = 'dep_ge10' AND wd.lead_bucket = k.lead_bucket;
