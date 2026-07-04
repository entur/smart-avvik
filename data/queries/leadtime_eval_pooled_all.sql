-- Lead-time eval table for the ALL-OPERATOR pooled family: how early can the per-call
-- impact stack alert for each operator (rail NSB/GOA + the bus/local codespaces)?
--
-- Port of leadtime_eval.sql to disruptions.training_calls_pooled_all. Takes the temporal
-- holdout (is_eval, onset >= 2026-04-01) and re-anchors the two ASK-TIME features at
-- several leads L before the call's aimed time:
--   own_prev_delay(L) -- vehicle's own delay at latest earlier stop aimed <= call - L min
--   line_now_*(L)     -- hourly line state at TIMESTAMP_TRUNC(call - (L+30) min, HOUR)
-- L = 30 reproduces the training anchoring exactly (create_training_calls_pooled_all.sql
-- uses a 30-min own cutoff and the hour bucket of call - 60 min) and serves as the
-- pipeline sanity check. lead_min = -1 is the plan-ahead variant: ask at SX onset with no
-- observable vehicle/line state (sentinels 0 / -1).
--
-- Inherits the three bus join deltas from the training build (see its header):
--   1. call keys recovered via the 4-date equi-join + first-instance QUALIFY
--      (ServiceJourney jkeys repeat daily; rail no-op),
--   2. linehour is already an hour-bucket equi-join in the template,
--   3. own_prev_delay pins the journey instance via the operating_date sentinel.
--
-- All other feature columns are carried over frozen from training_calls_pooled_all, so
-- scoring this table with the *_pooled_all models isolates information decay alone.
-- Analysis lives in eval/pooled-model-bus-eval.py (marimo).
--
-- CAVEAT (inherited from training): the line_now hour bucket can contain calls aimed up
-- to ~30 min AFTER the nominal ask time, and realized delays stand in for live ET
-- estimates, so quality at each lead reads slightly optimistic. Consistent across leads.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.leadtime_eval_pooled_all` AS
WITH all_calls AS (
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
    d.affected_journeys
  FROM `disruptions.disruptions` AS d
  WHERE d.participantRef IN ('NSB', 'GOA', 'GCO', 'MOR', 'NOR', 'TRO', 'FIN', 'RUT')
    AND SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
    AND DATE(SAFE.TIMESTAMP(d.creationTime)) BETWEEN DATE '2026-04-01' AND DATE '2026-06-19'
),
aff AS (
  SELECT
    s.situationNumber, s.onset_ts,
    IF(s.participant IN ('NSB', 'GOA'),
       s.participant,
       REGEXP_EXTRACT(j, r'^([A-Za-z]+):')) AS operator,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
  WHERE s.participant IN ('NSB', 'GOA')
     OR REGEXP_EXTRACT(j, r'^([A-Za-z]+):') IN
        ('KOL','SOF','NOR','SKY','ATB','OST','FIN','MOR','RUT','TRO','GCO')
),
aff_dated AS (
  SELECT a.*, cd
  FROM aff AS a,
       UNNEST(GENERATE_DATE_ARRAY(DATE_SUB(DATE(a.onset_ts), INTERVAL 1 DAY),
                                  DATE_ADD(DATE(a.onset_ts), INTERVAL 2 DAY))) AS cd
),
-- recover call_ts + operating_date for the holdout rows (training drops both); the join
-- and QUALIFY mirror the training build's base CTE exactly
callkeys AS (
  SELECT a.operator, a.situationNumber, a.jkey, r.stop_order,
         COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts,
         r.operating_date
  FROM aff_dated AS a
  JOIN all_calls AS r
    ON r.operator  = a.operator
   AND r.jkey      = a.jkey
   AND r.call_date = a.cd
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min IS NOT NULL OR r.arr_delay_min IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY a.situationNumber, a.operator, a.jkey, r.stop_order
    ORDER BY COALESCE(r.aimed_dep, r.aimed_arr) ASC
  ) = 1
),
joined AS (
  SELECT t.*, c.call_ts, c.operating_date
  FROM `disruptions.training_calls_pooled_all` AS t
  JOIN callkeys AS c USING (operator, situationNumber, jkey, stop_order)
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
  FROM all_calls
  WHERE aimed_dep IS NOT NULL
  GROUP BY 1, 2
),
own AS (
  SELECT e.situationNumber, e.operator, e.jkey, e.stop_order, e.lead_min,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)] AS own_prev
  FROM (SELECT DISTINCT operator, situationNumber, jkey, stop_order,
               call_ts, call_date, operating_date, lead_min
        FROM expanded) AS e
  JOIN all_calls AS r2
    ON r2.operator = e.operator
   AND r2.jkey = e.jkey
   AND COALESCE(r2.operating_date, DATE '1000-01-01') = COALESCE(e.operating_date, DATE '1000-01-01')
   AND r2.call_date BETWEEN DATE_SUB(e.call_date, INTERVAL 1 DAY) AND e.call_date
   AND r2.stop_order < e.stop_order
   AND COALESCE(r2.aimed_dep, r2.aimed_arr) <= TIMESTAMP_SUB(e.call_ts, INTERVAL e.lead_min MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT COALESCE(r2.call_cancelled, FALSE)
  GROUP BY 1, 2, 3, 4, 5
)
SELECT
  e.* EXCEPT(call_ts, operating_date, line_now_n, line_now_delay, line_now_cancel, own_prev_delay),
  IFNULL(lh.n, 0)        AS line_now_n,
  IFNULL(lh.d, -1)       AS line_now_delay,
  IFNULL(lh.c, -1)       AS line_now_cancel,
  IFNULL(o.own_prev, -1) AS own_prev_delay
FROM expanded AS e
LEFT JOIN linehour AS lh
  ON lh.lineRef = e.lineRef
 AND lh.h = TIMESTAMP_TRUNC(TIMESTAMP_SUB(e.call_ts, INTERVAL (e.lead_min + 30) MINUTE), HOUR)
LEFT JOIN own AS o USING (situationNumber, operator, jkey, stop_order, lead_min)

UNION ALL

SELECT
  j.* EXCEPT(call_ts, operating_date, line_now_n, line_now_delay, line_now_cancel, own_prev_delay),
  -1   AS lead_min,
  0    AS line_now_n,
  -1.0 AS line_now_delay,
  -1.0 AS line_now_cancel,
  -1   AS own_prev_delay
FROM joined AS j;
