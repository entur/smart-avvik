-- Lead-time eval table: how early can the per-call impact stack alert?
--
-- Takes the temporal holdout of disruptions.training_calls (is_eval, onset >= 2026-04-01)
-- and re-anchors the two ASK-TIME features at several leads L before the call's aimed time:
--   own_prev_delay(L) -- train's own delay at latest earlier stop aimed <= call - L min
--   line_now_*(L)     -- hourly line state at TIMESTAMP_TRUNC(call - (L+30) min, HOUR)
-- L = 30 reproduces the training anchoring exactly (create_training_calls.sql uses a 30-min
-- own cutoff and the hour bucket of call - 60 min) and serves as the pipeline sanity check.
-- lead_min = -1 is the plan-ahead variant: ask at SX onset with no observable train/line
-- state (sentinels 0 / -1), the serving regime for calls far in the future.
--
-- All other feature columns are carried over frozen from training_calls, so scoring this
-- table with call_cancel_clf / dep_ge10_clf isolates the effect of information decay alone.
-- Analysis lives in eval/impact_eval.py (marimo). ~263k holdout rows x 5 variants = ~1.3M.
--
-- CAVEAT (inherited from training): the line_now hour bucket can contain calls aimed up to
-- ~30 min AFTER the nominal ask time, and realized delays stand in for live ET estimates,
-- so quality at each lead reads slightly optimistic. Consistent across leads.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.leadtime_eval_calls` AS
WITH sit AS (
  SELECT situationNumber, SAFE.TIMESTAMP(creationTime) AS onset_ts, affected_journeys
  FROM `ent-kixavvik-dev.disruptions.disruptions`
  WHERE participantRef = 'NSB'
    AND SAFE.TIMESTAMP(creationTime) IS NOT NULL
    AND DATE(SAFE.TIMESTAMP(creationTime)) BETWEEN DATE '2026-04-01' AND DATE '2026-06-19'
),
aff AS (
  SELECT s.situationNumber, s.onset_ts, REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
),
-- recover the exact call timestamps for the holdout rows (training_calls drops call_ts)
callkeys AS (
  SELECT a.situationNumber, a.jkey, r.stop_order,
         COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts
  FROM aff AS a
  JOIN `ent-kixavvik-dev.disruptions.rail_calls` AS r USING (jkey)
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min IS NOT NULL OR r.arr_delay_min IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
),
joined AS (
  SELECT t.*, c.call_ts
  FROM `ent-kixavvik-dev.disruptions.training_calls` AS t
  JOIN callkeys AS c USING (situationNumber, jkey, stop_order)
  WHERE t.is_eval
),
expanded AS (
  SELECT j.*, lead_min
  FROM joined AS j CROSS JOIN UNNEST([30, 60, 120, 240]) AS lead_min
),
linehour AS (
  SELECT lineRef, TIMESTAMP_TRUNC(aimed_dep, HOUR) AS h,
         COUNT(*) AS n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS d,
         ROUND(AVG(CAST(COALESCE(call_cancelled, FALSE) AS INT64)), 3) AS c
  FROM `ent-kixavvik-dev.disruptions.rail_calls`
  WHERE aimed_dep IS NOT NULL
  GROUP BY 1, 2
),
own AS (
  SELECT e.situationNumber, e.jkey, e.stop_order, e.lead_min,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)] AS own_prev
  FROM (SELECT situationNumber, jkey, stop_order, call_ts, call_date, lead_min FROM expanded) AS e
  JOIN `ent-kixavvik-dev.disruptions.rail_calls` AS r2
    ON r2.jkey = e.jkey
   AND r2.call_date BETWEEN DATE_SUB(e.call_date, INTERVAL 1 DAY) AND e.call_date
   AND r2.stop_order < e.stop_order
   AND COALESCE(r2.aimed_dep, r2.aimed_arr) <= TIMESTAMP_SUB(e.call_ts, INTERVAL e.lead_min MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT COALESCE(r2.call_cancelled, FALSE)
  GROUP BY 1, 2, 3, 4
)
SELECT
  e.* EXCEPT(call_ts, line_now_n, line_now_delay, line_now_cancel, own_prev_delay),
  IFNULL(lh.n, 0)        AS line_now_n,
  IFNULL(lh.d, -1)       AS line_now_delay,
  IFNULL(lh.c, -1)       AS line_now_cancel,
  IFNULL(o.own_prev, -1) AS own_prev_delay
FROM expanded AS e
LEFT JOIN linehour AS lh
  ON lh.lineRef = e.lineRef
 AND lh.h = TIMESTAMP_TRUNC(TIMESTAMP_SUB(e.call_ts, INTERVAL (e.lead_min + 30) MINUTE), HOUR)
LEFT JOIN own AS o USING (situationNumber, jkey, stop_order, lead_min)

UNION ALL

SELECT
  j.* EXCEPT(call_ts, line_now_n, line_now_delay, line_now_cancel, own_prev_delay),
  -1   AS lead_min,
  0    AS line_now_n,
  -1.0 AS line_now_delay,
  -1.0 AS line_now_cancel,
  -1   AS own_prev_delay
FROM joined AS j;
