-- Pooled (NSB + GOA) per-call training table -- the multi-operator experiment of
-- docs/operator-census/LEARNINGS.md. Same grain, labels, leak rules, features and
-- temporal split as create_training_calls.sql (one row per situation x affected call,
-- outcomes from the operator's realized-call table, train onset < 2026-04-01), plus:
--
--   operator     -- the pooling feature ('NSB' | 'GOA' = SX participantRef). Lets one
--                   model family serve both operators; boosted trees can split on it
--                   wherever the operators' dynamics diverge.
--
-- CALLS SOURCE is per-operator: NSB situations join disruptions.rail_calls (VYG, anshar
-- build), GOA situations join disruptions.goa_calls (last_recorded build, calls from
-- 2025-02-25). The join is scoped ON (operator, jkey): DatedServiceJourney numeric ids
-- are not guaranteed unique across codespaces, and an NSB situation must never label
-- itself with a GOA call. GOA onsets start 2025-03-01 (when GOA appears in
-- last_recorded); NSB keeps the full 2024-06-01 window -- the pool is deliberately
-- unbalanced (~20:1) and the eval reads per-operator slices, not the blend.
--
-- PROPAGATION FEATURES are computed over the union of both calls tables:
--   line_pre_*/line_now_* key on lineRef -- VYG:%/GOA:% values are disjoint, so each
--     line's state only ever reflects its own operator;
--   net_pre_* is per (situation, operator) -- "network-wide" means the operator's own
--     network (all-GOA state for a GOA situation), NOT the union;
--   own_prev_delay keys on (operator, jkey) like the label join.
--
-- category joins the shared disruptions.categories text table -- verified 99.7% GOA
-- coverage (2026-07-03), no operator gap.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.training_calls_pooled`
PARTITION BY call_date
CLUSTER BY situationNumber AS
WITH cat AS (
  -- categories has a handful of duplicate (summary, description) pairs; dedup or the situation
  -- join below fans out
  SELECT summary, description, ANY_VALUE(category) AS category
  FROM `disruptions.categories`
  GROUP BY summary, description
),
all_calls AS (
  SELECT 'NSB' AS operator, * FROM `disruptions.rail_calls`
  UNION ALL
  SELECT 'GOA' AS operator, * FROM `disruptions.goa_calls`
),
sit AS (
  SELECT
    d.participantRef                  AS operator,
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime)    AS onset_ts,
    d.status,
    d.planned,
    ARRAY_LENGTH(d.affected_journeys) AS num_journeys,
    ARRAY_LENGTH(d.affected_stops)    AS num_stops,
    ARRAY_LENGTH(d.affected_lines)    AS num_lines,
    d.affected_journeys,
    -- leak-free category: join on the FIRST-version text, never the (drifting) latest text
    IFNULL(c.category, 'UNKNOWN')     AS category,
    -- delay magnitude stated in the onset text, if any ("forsinket ca. 30 minutter")
    IFNULL(SAFE_CAST(REGEXP_EXTRACT(
      LOWER(CONCAT(IFNULL(d.onset_summary,''), ' ', IFNULL(d.onset_description,''))),
      r'([0-9]+)\s*min') AS INT64), -1) AS stated_min
  FROM `disruptions.disruptions` AS d
  LEFT JOIN cat AS c
    ON IFNULL(d.onset_summary,'')     = IFNULL(c.summary,'')
   AND IFNULL(d.onset_description,'') = IFNULL(c.description,'')
  WHERE d.participantRef IN ('NSB', 'GOA')
    AND SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
    AND DATE(SAFE.TIMESTAMP(d.creationTime)) BETWEEN
          IF(d.participantRef = 'GOA', DATE '2025-03-01', DATE '2024-06-01')
          AND DATE '2026-06-19'
),
aff AS (
  SELECT
    s.operator, s.situationNumber, s.onset_ts, s.status, s.planned,
    s.num_journeys, s.num_stops, s.num_lines, s.category, s.stated_min,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
),
base AS (
  SELECT
    a.operator,
    a.situationNumber,
    a.jkey,
    r.call_date,
    COALESCE(r.call_cancelled, FALSE) AS call_cancelled,
    IF(NOT COALESCE(r.call_cancelled, FALSE), GREATEST(r.dep_delay_min, 0), NULL) AS dep_delay_pos,
    IF(NOT COALESCE(r.call_cancelled, FALSE), GREATEST(r.arr_delay_min, 0), NULL) AS arr_delay_pos,
    IFNULL(a.status, 'other') AS status,
    IFNULL(a.planned, FALSE)  AS planned,
    a.num_journeys,
    a.num_stops,
    a.num_lines,
    EXTRACT(HOUR      FROM a.onset_ts) AS onset_hour,
    EXTRACT(DAYOFWEEK FROM a.onset_ts) AS onset_dow,
    EXTRACT(MONTH     FROM a.onset_ts) AS onset_month,
    r.lineRef,
    r.stop_place,
    r.stop_order,
    (r.aimed_dep IS NULL) AS is_terminal,
    (r.aimed_arr IS NULL) AS is_origin,
    TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) AS min_from_onset,
    EXTRACT(HOUR      FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_hour,
    EXTRACT(DAYOFWEEK FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_dow,
    a.category,
    a.stated_min,
    a.onset_ts,
    COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts,
    DATE(a.onset_ts) >= DATE '2026-04-01' AS is_eval
  FROM aff AS a
  JOIN all_calls AS r USING (operator, jkey)
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min IS NOT NULL OR r.arr_delay_min IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
),
-- pre-onset line congestion, per (situation x line): the 60 min before onset
sitline AS (
  SELECT DISTINCT operator, situationNumber, lineRef, onset_ts FROM base
),
linepre AS (
  SELECT p.situationNumber, p.lineRef,
         COUNT(*) AS line_pre_n,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS line_pre_delay,
         ROUND(AVG(CAST(COALESCE(r.call_cancelled, FALSE) AS INT64)), 3) AS line_pre_cancel
  FROM sitline AS p
  JOIN all_calls AS r
    ON r.lineRef = p.lineRef
   AND r.call_date BETWEEN DATE_SUB(DATE(p.onset_ts, 'Europe/Oslo'), INTERVAL 1 DAY)
                       AND DATE(p.onset_ts, 'Europe/Oslo')
   AND r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1, 2
),
-- pre-onset network-wide state, per situation: same window, the OPERATOR's own network
netpre AS (
  SELECT p.situationNumber,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS net_pre_delay,
         ROUND(AVG(CAST(COALESCE(r.call_cancelled, FALSE) AS INT64)), 3) AS net_pre_cancel
  FROM (SELECT DISTINCT operator, situationNumber, onset_ts FROM sitline) AS p
  JOIN all_calls AS r
    ON r.operator = p.operator
   AND r.call_date BETWEEN DATE_SUB(DATE(p.onset_ts, 'Europe/Oslo'), INTERVAL 1 DAY)
                       AND DATE(p.onset_ts, 'Europe/Oslo')
   AND r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1
),
-- hourly line state (equi-join by hour bucket; a lineRef x time RANGE join blows up -- each line
-- would join its full 2-year history before the filter)
linehour AS (
  SELECT lineRef, TIMESTAMP_TRUNC(aimed_dep, HOUR) AS h,
         COUNT(*) AS line_now_n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS line_now_delay,
         ROUND(AVG(CAST(COALESCE(call_cancelled, FALSE) AS INT64)), 3) AS line_now_cancel
  FROM all_calls
  WHERE aimed_dep IS NOT NULL
  GROUP BY 1, 2
),
-- the train's own delay at its latest stop aimed >=30 min before this call
own AS (
  SELECT t.situationNumber, t.jkey, t.stop_order,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)]
           AS own_prev_delay
  FROM base AS t
  JOIN all_calls AS r2
    ON r2.operator = t.operator
   AND r2.jkey = t.jkey
   AND r2.call_date BETWEEN DATE_SUB(t.call_date, INTERVAL 1 DAY) AND t.call_date
   AND r2.stop_order < t.stop_order
   AND COALESCE(r2.aimed_dep, r2.aimed_arr) <= TIMESTAMP_SUB(t.call_ts, INTERVAL 30 MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT COALESCE(r2.call_cancelled, FALSE)
  GROUP BY 1, 2, 3
)
SELECT
  b.* EXCEPT(onset_ts, call_ts),
  IFNULL(lp.line_pre_n, 0)        AS line_pre_n,
  IFNULL(lp.line_pre_delay, -1)   AS line_pre_delay,
  IFNULL(lp.line_pre_cancel, -1)  AS line_pre_cancel,
  IFNULL(np.net_pre_delay, -1)    AS net_pre_delay,
  IFNULL(np.net_pre_cancel, -1)   AS net_pre_cancel,
  IFNULL(lh.line_now_n, 0)        AS line_now_n,
  IFNULL(lh.line_now_delay, -1)   AS line_now_delay,
  IFNULL(lh.line_now_cancel, -1)  AS line_now_cancel,
  IFNULL(o.own_prev_delay, -1)    AS own_prev_delay
FROM base AS b
LEFT JOIN linepre AS lp ON lp.situationNumber = b.situationNumber AND lp.lineRef = b.lineRef
LEFT JOIN netpre  AS np ON np.situationNumber = b.situationNumber
LEFT JOIN linehour AS lh
  ON lh.lineRef = b.lineRef
 AND lh.h = TIMESTAMP_TRUNC(TIMESTAMP_SUB(b.call_ts, INTERVAL 60 MINUTE), HOUR)
LEFT JOIN own AS o
  ON o.situationNumber = b.situationNumber AND o.jkey = b.jkey AND o.stop_order = b.stop_order;
