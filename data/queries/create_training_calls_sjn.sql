-- SJ Norge (SJN) calls in the exact shape of training_calls' has_situation = FALSE rows (KIX-39).
--
-- One row per realized SJN call from disruptions.sjn_calls, with the no-SX fillers and the two
-- call-anchored feature blocks (line_now_*, own_prev_delay) computed exactly as
-- create_training_calls.sql computes them -- the only change is that they read sjn_calls instead
-- of rail_calls, which is the same thing for a call: both blocks only ever look at the call's own
-- line and own train. Keep the column ORDER identical to training_calls so the two can be
-- UNION ALLed.
--
-- All rows are no-SX on purpose. SJN's own SX (~25-30 a month) are per-departure notices that
-- announce an outcome already decided ("SJ Nord kjører buss for tog"), and no other publisher
-- names an SJN train; see docs/sjn/LEARNINGS.md.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.training_calls_sjn`
PARTITION BY call_date AS
WITH nosx AS (
  SELECT
    CAST(NULL AS STRING) AS situationNumber,
    r.jkey,
    r.call_date,
    COALESCE(r.call_cancelled, FALSE) AS call_cancelled,
    IF(NOT COALESCE(r.call_cancelled, FALSE), GREATEST(r.dep_delay_min, 0), NULL) AS dep_delay_pos,
    IF(NOT COALESCE(r.call_cancelled, FALSE), GREATEST(r.arr_delay_min, 0), NULL) AS arr_delay_pos,
    FALSE AS has_situation,
    'none' AS status,
    FALSE  AS planned,
    -1     AS num_journeys,
    -1     AS num_stops,
    -1     AS num_lines,
    -1     AS onset_hour,
    -1     AS onset_dow,
    -1     AS onset_month,
    r.lineRef,
    r.stop_place,
    r.stop_order,
    (r.aimed_dep IS NULL) AS is_terminal,
    (r.aimed_arr IS NULL) AS is_origin,
    -1 AS min_from_onset,
    EXTRACT(HOUR      FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_hour,
    EXTRACT(DAYOFWEEK FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_dow,
    'none' AS category,
    -1     AS stated_min,
    COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts,
    r.call_date >= DATE '2026-04-01' AS is_eval
  FROM `disruptions.sjn_calls` AS r
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min IS NOT NULL OR r.arr_delay_min IS NOT NULL)
),
linehour AS (
  SELECT lineRef, TIMESTAMP_TRUNC(aimed_dep, HOUR) AS h,
         COUNT(*) AS line_now_n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS line_now_delay,
         ROUND(AVG(CAST(COALESCE(call_cancelled, FALSE) AS INT64)), 3) AS line_now_cancel
  FROM `disruptions.sjn_calls`
  WHERE aimed_dep IS NOT NULL
  GROUP BY 1, 2
),
own AS (
  SELECT t.jkey, t.stop_order, t.call_date,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)]
           AS own_prev_delay
  FROM (SELECT DISTINCT jkey, stop_order, call_date, call_ts FROM nosx) AS t
  JOIN `disruptions.sjn_calls` AS r2
    ON r2.jkey = t.jkey
   AND r2.call_date BETWEEN DATE_SUB(t.call_date, INTERVAL 1 DAY) AND t.call_date
   AND r2.stop_order < t.stop_order
   AND COALESCE(r2.aimed_dep, r2.aimed_arr) <= TIMESTAMP_SUB(t.call_ts, INTERVAL 30 MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT COALESCE(r2.call_cancelled, FALSE)
  GROUP BY 1, 2, 3
)
SELECT
  b.* EXCEPT(call_ts),
  0  AS line_pre_n,
  -1.0 AS line_pre_delay,
  -1.0 AS line_pre_cancel,
  -1.0 AS net_pre_delay,
  -1.0 AS net_pre_cancel,
  IFNULL(lh.line_now_n, 0)        AS line_now_n,
  IFNULL(lh.line_now_delay, -1)   AS line_now_delay,
  IFNULL(lh.line_now_cancel, -1)  AS line_now_cancel,
  IFNULL(o.own_prev_delay, -1)    AS own_prev_delay
FROM nosx AS b
LEFT JOIN linehour AS lh
  ON lh.lineRef = b.lineRef
 AND lh.h = TIMESTAMP_TRUNC(TIMESTAMP_SUB(b.call_ts, INTERVAL 60 MINUTE), HOUR)
LEFT JOIN own AS o
  ON o.jkey = b.jkey AND o.stop_order = b.stop_order AND o.call_date = b.call_date;
