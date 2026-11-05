-- ALL-OPERATOR pooled per-call training table: rail (NSB, GOA) + the bus/local-transport
-- codespaces (KOL, SOF, NOR, SKY, ATB, OST, FIN, MOR, RUT, TRO, GCO). Extends
-- create_training_calls_pooled.sql; same grain, labels, leak rules and temporal split
-- (train onset < 2026-04-01). The rail slices are intended to be row-identical to
-- training_calls_pooled -- every structural change below is a provable no-op for rail.
--
--   operator -- the pooling feature. For NSB/GOA situations it stays the participantRef
--     (rail behaviour, unchanged). For bus situations it is the REF's codespace, not the
--     publisher: GCO publishes per-departure notices for journeys across many county
--     networks, and propagation features must key on the network the journey runs in.
--     NOR carries two publishers (NOR + GCO) -- both fold into operator 'NOR'.
--
-- BUS JOIN SEMANTICS (the three deltas vs the rail-only pooled build):
--   1. ServiceJourney refs are NOT date-scoped -- the same jkey repeats every operating
--      day. The label join expands each ref to the 4 candidate call_dates around onset
--      and equi-joins (operator, jkey, call_date); the 0..720-min onset filter already
--      confines rail matches to that window, so rail rows are unchanged, while bus refs
--      stop fanning out to ~470 instances. A QUALIFY keeps only the FIRST instance after
--      onset per (situation, operator, jkey, stop_order) -- a notice is about the next
--      departure, not tomorrow's same-numbered one (rail keys are unique: verified 0
--      duplicate (jkey, stop_order) in rail_calls, so again a rail no-op).
--   2. line_pre_* / net_pre_* become hour-bucket equi-joins: the old range joins expand
--      to ~1e13 candidate pairs against 480M bus calls. A 60-min window spans at most 2
--      hour buckets, so joining on (key, hour) with the exact window filter afterwards
--      returns IDENTICAL values at a fraction of the cost.
--   3. own_prev_delay pins the journey instance via operating_date (NULL for rail, where
--      DatedServiceJourney jkeys are already date-scoped -- the sentinel keeps the rail
--      join exactly as before).
--
-- category still joins the shared disruptions.categories text table; bus texts that miss
-- fall to 'UNKNOWN' and the trees can split on operator/status instead.

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.training_calls_pooled_all`
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
  SELECT 'NSB' AS operator, CAST(NULL AS DATE) AS operating_date, * FROM `disruptions.rail_calls`
  UNION ALL
  SELECT 'GOA' AS operator, CAST(NULL AS DATE) AS operating_date, * FROM `disruptions.goa_calls`
  UNION ALL
  SELECT * FROM `disruptions.bus_calls`
),
sit AS (
  SELECT
    d.participantRef                  AS participant,
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
  WHERE d.participantRef IN ('NSB', 'GOA', 'GCO', 'MOR', 'NOR', 'TRO', 'FIN', 'RUT')
    AND SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
    AND DATE(SAFE.TIMESTAMP(d.creationTime)) BETWEEN
          IF(d.participantRef = 'NSB', DATE '2024-06-01', DATE '2025-03-01')
          AND DATE '2026-06-19'
),
aff AS (
  SELECT
    s.situationNumber, s.onset_ts, s.status, s.planned,
    s.num_journeys, s.num_stops, s.num_lines, s.category, s.stated_min,
    IF(s.participant IN ('NSB', 'GOA'),
       s.participant,
       REGEXP_EXTRACT(j, r'^([A-Za-z]+):')) AS operator,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
  WHERE s.participant IN ('NSB', 'GOA')
     OR REGEXP_EXTRACT(j, r'^([A-Za-z]+):') IN
        ('KOL','SOF','NOR','SKY','ATB','OST','FIN','MOR','RUT','TRO','GCO')
),
-- expand each ref to the candidate call_dates around onset (see header, delta 1)
aff_dated AS (
  SELECT a.*, cd
  FROM aff AS a,
       UNNEST(GENERATE_DATE_ARRAY(DATE_SUB(DATE(a.onset_ts), INTERVAL 1 DAY),
                                  DATE_ADD(DATE(a.onset_ts), INTERVAL 2 DAY))) AS cd
),
base AS (
  SELECT
    a.operator,
    a.situationNumber,
    a.jkey,
    r.call_date,
    r.operating_date,
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
  FROM aff_dated AS a
  JOIN all_calls AS r
    ON r.operator  = a.operator
   AND r.jkey      = a.jkey
   AND r.call_date = a.cd
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min IS NOT NULL OR r.arr_delay_min IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
  -- first instance after onset only (rail no-op; see header, delta 1)
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY a.situationNumber, a.operator, a.jkey, r.stop_order
    ORDER BY COALESCE(r.aimed_dep, r.aimed_arr) ASC
  ) = 1
),
-- pre-onset line congestion, per (situation x line): the 60 min before onset.
-- Hour-bucket equi-join + exact window filter (see header, delta 2): identical values to
-- the old lineRef + call_date range join -- a 60-min window touches at most these 2 buckets.
sitline AS (
  SELECT DISTINCT operator, situationNumber, lineRef, onset_ts FROM base
),
sitline_h AS (
  -- the two buckets are always distinct (exactly 60 min apart), so no dedup needed
  SELECT p.*, h
  FROM sitline AS p,
       UNNEST([TIMESTAMP_TRUNC(TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE), HOUR),
               TIMESTAMP_TRUNC(p.onset_ts, HOUR)]) AS h
),
linepre AS (
  SELECT p.situationNumber, p.lineRef,
         COUNT(*) AS line_pre_n,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS line_pre_delay,
         ROUND(AVG(CAST(COALESCE(r.call_cancelled, FALSE) AS INT64)), 3) AS line_pre_cancel
  FROM sitline_h AS p
  JOIN all_calls AS r
    ON r.lineRef = p.lineRef
   AND TIMESTAMP_TRUNC(r.aimed_dep, HOUR) = p.h
  WHERE r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
    AND r.aimed_dep <  p.onset_ts
  GROUP BY 1, 2
),
-- pre-onset network-wide state, per situation: same window, the OPERATOR's own network
sitnet_h AS (
  SELECT p.operator, p.situationNumber, p.onset_ts, h
  FROM (SELECT DISTINCT operator, situationNumber, onset_ts FROM sitline) AS p,
       UNNEST([TIMESTAMP_TRUNC(TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE), HOUR),
               TIMESTAMP_TRUNC(p.onset_ts, HOUR)]) AS h
),
netpre AS (
  SELECT p.situationNumber,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS net_pre_delay,
         ROUND(AVG(CAST(COALESCE(r.call_cancelled, FALSE) AS INT64)), 3) AS net_pre_cancel
  FROM sitnet_h AS p
  JOIN all_calls AS r
    ON r.operator = p.operator
   AND TIMESTAMP_TRUNC(r.aimed_dep, HOUR) = p.h
  WHERE r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
    AND r.aimed_dep <  p.onset_ts
  GROUP BY 1
),
-- hourly line state (equi-join by hour bucket; a lineRef x time RANGE join blows up -- each line
-- would join its full history before the filter)
linehour AS (
  SELECT lineRef, TIMESTAMP_TRUNC(aimed_dep, HOUR) AS h,
         COUNT(*) AS line_now_n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS line_now_delay,
         ROUND(AVG(CAST(COALESCE(call_cancelled, FALSE) AS INT64)), 3) AS line_now_cancel
  FROM all_calls
  WHERE aimed_dep IS NOT NULL
  GROUP BY 1, 2
),
-- the vehicle's own delay at its latest stop aimed >=30 min before this call, same
-- journey INSTANCE (operating_date sentinel keeps rail exactly as before; delta 3)
own AS (
  SELECT t.situationNumber, t.jkey, t.stop_order,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)]
           AS own_prev_delay
  FROM base AS t
  JOIN all_calls AS r2
    ON r2.operator = t.operator
   AND r2.jkey = t.jkey
   AND COALESCE(r2.operating_date, DATE '1000-01-01') = COALESCE(t.operating_date, DATE '1000-01-01')
   AND r2.call_date BETWEEN DATE_SUB(t.call_date, INTERVAL 1 DAY) AND t.call_date
   AND r2.stop_order < t.stop_order
   AND COALESCE(r2.aimed_dep, r2.aimed_arr) <= TIMESTAMP_SUB(t.call_ts, INTERVAL 30 MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT COALESCE(r2.call_cancelled, FALSE)
  GROUP BY 1, 2, 3
)
SELECT
  b.* EXCEPT(onset_ts, call_ts, operating_date),
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
