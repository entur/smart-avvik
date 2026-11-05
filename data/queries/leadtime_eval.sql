-- Lead-time eval table: how early can the per-call impact stack alert?
--
-- Takes the temporal holdout of disruptions.training_calls (is_eval) and re-anchors the two
-- ASK-TIME features at several leads L before the call's aimed time:
--   own_prev_delay(L) -- train's own delay at latest earlier stop aimed <= call - L min
--   line_now_*(L)     -- hourly line state at TIMESTAMP_TRUNC(call - (L+30) min, HOUR)
-- L = 30 reproduces the training anchoring exactly (create_training_calls.sql uses a 30-min
-- own cutoff and the hour bucket of call - 60 min) and serves as the pipeline sanity check.
-- lead_min = -1 is the plan-ahead variant: ask at SX onset with no observable train/line
-- state (sentinels 0 / -1), the serving regime for calls far in the future.
--
-- All other feature columns are carried over frozen from training_calls, so scoring this
-- table with call_cancel_clf / dep_ge10_clf isolates the effect of information decay alone.
-- Analysis lives in eval/impact_eval.py (marimo).
--
-- BOTH POPULATIONS SINCE 2026-08-19. training_calls carries unflagged calls (has_situation, see
-- create_training_calls.sql) and this table now carries them too -- ~1.08 M holdout rows x 5
-- variants = ~5.4 M, up from ~1.3 M. It did not before, and not by design: call_ts was recovered
-- by re-joining SX to rail_calls, so an unflagged row's NULL situationNumber silently dropped it.
-- That accident was load-bearing for create_leadtime_calibration.sql, which had no population
-- predicate because it could not see the other population. IT DOES NOW, so every fit in that file
-- gained one in the same change -- read its header before touching either.
--
-- call_ts is recovered PER CALL (jkey, stop_order, call_date -- verified unique in rail_calls,
-- 6 893 205 rows / 6 893 205 keys) rather than through the situation, which is both what lets the
-- unflagged rows in and a simplification: the 0..720-min window the old reconstruction re-applied
-- was already baked into which rows training_calls holds. own_prev_delay is re-keyed the same way
-- for the same reason it was in create_training_calls.sql -- it never depended on the situation.
--
-- CAVEAT (inherited from training): the line_now hour bucket can contain calls aimed up to
-- ~30 min AFTER the nominal ask time, and realized delays stand in for live ET estimates,
-- so quality at each lead reads slightly optimistic. Consistent across leads.

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.leadtime_eval_calls` AS
WITH
-- recover the exact call timestamps for the holdout rows (training_calls drops call_ts)
callts AS (
  SELECT jkey, stop_order, call_date, COALESCE(aimed_dep, aimed_arr) AS call_ts
  FROM `ent-smartavvik-dev.disruptions.rail_calls`
  WHERE (aimed_dep IS NOT NULL OR aimed_arr IS NOT NULL)
    AND (COALESCE(call_cancelled, FALSE) OR dep_delay_min IS NOT NULL OR arr_delay_min IS NOT NULL)
),
joined AS (
  SELECT t.*, c.call_ts
  FROM `ent-smartavvik-dev.disruptions.training_calls` AS t
  JOIN callts AS c USING (jkey, stop_order, call_date)
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
  FROM `ent-smartavvik-dev.disruptions.rail_calls`
  WHERE aimed_dep IS NOT NULL
  GROUP BY 1, 2
),
own AS (
  SELECT e.jkey, e.stop_order, e.call_date, e.lead_min,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)] AS own_prev
  FROM (SELECT DISTINCT jkey, stop_order, call_ts, call_date, lead_min FROM expanded) AS e
  JOIN `ent-smartavvik-dev.disruptions.rail_calls` AS r2
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
LEFT JOIN own AS o USING (jkey, stop_order, call_date, lead_min)

UNION ALL

SELECT
  j.* EXCEPT(call_ts, line_now_n, line_now_delay, line_now_cancel, own_prev_delay),
  -1   AS lead_min,
  0    AS line_now_n,
  -1.0 AS line_now_delay,
  -1.0 AS line_now_cancel,
  -1   AS own_prev_delay
FROM joined AS j;
