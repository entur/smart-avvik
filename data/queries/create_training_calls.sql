-- Per-call training table for the full-population, "any situation" model family.
--
-- GRAIN: one row per (situation x affected call) for NSB situations, where an "affected call"
-- is a stop on an affected journey whose SCHEDULED time falls within 12h (0..720 min) after the
-- situation's onset (creationTime). This is the prediction unit the product needs: a passenger
-- looks up their boarding stop and their alighting stop on the affected train and reads off
-- this call's outcome. Partial ("turn-short") cancellations are represented naturally because
-- call_cancelled is per stop in rail_calls.
--
-- THREE LABELS (realized, from the rebuilt rail_calls -- EXCLUDED from features at train time):
--   call_cancelled  -- will this stop be served? (the cancellation classifier's target)
--   dep_delay_pos   -- GREATEST(dep_delay,0) for non-cancelled calls  (boarding-side delay)
--   arr_delay_pos   -- GREATEST(arr_delay,0) for non-cancelled calls  (destination-side delay)
-- A passenger's boarding stop reads call_cancelled + dep_delay_pos; the alighting stop reads
-- call_cancelled + arr_delay_pos. We COALESCE(aimed_dep, aimed_arr) for the window so terminal
-- arrival-only stops (the destinations) are KEPT -- category_predictiveness.sql dropped them.
--
-- LEAKAGE: features are onset-time / planned-timetable / pre-call-observable only. Deliberately
-- EXCLUDED as leaky:
--   - duration_minutes, severity  -> from the LATEST snapshot in disruptions (resolved state)
--   - summary/description         -> latest text ("Toget kjører igjen" leaks the outcome)
--   - actual_dep/arr, call_source -> realized operation (these ARE the labels)
-- Kept (leak-free): status (onset-derived regex), planned, affected-counts, onset time parts,
-- per-call PLANNED attributes (line, stop, stop_order, scheduled time, position), and the
-- 2026-07-02 additions below.
--
-- FEATURES ADDED 2026-07-02 (exceedance-stack session, docs/model-hardening/LEARNINGS.md):
--   category        -- AI.CLASSIFY category joined LEAK-FREE on the situation's ONSET text
--                      (onset_summary/onset_description), 99.99% coverage; 'UNKNOWN' fallback.
--   stated_min      -- delay minutes stated in the onset text ("ca. 30 min forsinket"), -1 if none.
--   line_pre_*      -- realized line congestion in the 60 min BEFORE ONSET (same line): call count,
--                      mean positive dep delay, cancel share. Pre-onset => observable at onset.
--   net_pre_*       -- network-wide (all NSB rail) version of the same pre-onset window.
--   line_now_*      -- line state in the HOUR bucket containing (call time - 60 min): a fresher,
--                      ask-time-anchored congestion signal for near-term predictions. At serving
--                      this is only fully observable for calls <= ~1h ahead; the live path
--                      now-casts it from the most recent observed window (see predict_calls_today).
--   own_prev_delay  -- the train's OWN positive dep delay at its latest earlier stop whose aimed
--                      time is >= 30 min before this call (-1 if no such stop). The single
--                      strongest predictor found (corr 0.88 with the arrival-delay outcome);
--                      semantically "-1 = no upstream observation yet", which matches the live
--                      serving case of a train that has not started.
-- All *_delay/_cancel features use -1 sentinels for "window empty / not observable" (BQML boosted
-- trees split on the sentinel), *_n counts use 0.
--
-- SPLIT: temporal, keyed on onset date -> a whole situation (all its calls) lands on one side,
-- so there is no within-situation grouping leak. Train = onset < 2026-04-01, eval = on/after.
-- Window is restricted to onset in [2024-06-01, 2026-06-19] so rail_calls fully covers the 12h
-- outcome window (table runs 2024-06-01 .. 2026-06-20); later onsets would be right-censored.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.training_calls`
PARTITION BY call_date
CLUSTER BY situationNumber AS
WITH cat AS (
  -- categories has a handful of duplicate (summary, description) pairs; dedup or the situation
  -- join below fans out
  SELECT summary, description, ANY_VALUE(category) AS category
  FROM `disruptions.categories`
  GROUP BY summary, description
),
sit AS (
  SELECT
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
  WHERE d.participantRef = 'NSB'
    AND SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
    AND DATE(SAFE.TIMESTAMP(d.creationTime)) BETWEEN DATE '2024-06-01' AND DATE '2026-06-19'
),
aff AS (
  SELECT
    s.situationNumber, s.onset_ts, s.status, s.planned,
    s.num_journeys, s.num_stops, s.num_lines, s.category, s.stated_min,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
),
base AS (
  SELECT
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
  JOIN `disruptions.rail_calls` AS r USING (jkey)
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min IS NOT NULL OR r.arr_delay_min IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
),
-- pre-onset line congestion, per (situation x line): the 60 min before onset
sitline AS (
  SELECT DISTINCT situationNumber, lineRef, onset_ts FROM base
),
linepre AS (
  SELECT p.situationNumber, p.lineRef,
         COUNT(*) AS line_pre_n,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS line_pre_delay,
         ROUND(AVG(CAST(COALESCE(r.call_cancelled, FALSE) AS INT64)), 3) AS line_pre_cancel
  FROM sitline AS p
  JOIN `disruptions.rail_calls` AS r
    ON r.lineRef = p.lineRef
   AND r.call_date BETWEEN DATE_SUB(DATE(p.onset_ts, 'Europe/Oslo'), INTERVAL 1 DAY)
                       AND DATE(p.onset_ts, 'Europe/Oslo')
   AND r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1, 2
),
-- pre-onset network-wide state, per situation: same window, all NSB rail traffic
netpre AS (
  SELECT p.situationNumber,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS net_pre_delay,
         ROUND(AVG(CAST(COALESCE(r.call_cancelled, FALSE) AS INT64)), 3) AS net_pre_cancel
  FROM (SELECT DISTINCT situationNumber, onset_ts FROM sitline) AS p
  JOIN `disruptions.rail_calls` AS r
    ON r.call_date BETWEEN DATE_SUB(DATE(p.onset_ts, 'Europe/Oslo'), INTERVAL 1 DAY)
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
  FROM `disruptions.rail_calls`
  WHERE aimed_dep IS NOT NULL
  GROUP BY 1, 2
),
-- the train's own delay at its latest stop aimed >=30 min before this call
own AS (
  SELECT t.situationNumber, t.jkey, t.stop_order,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)]
           AS own_prev_delay
  FROM base AS t
  JOIN `disruptions.rail_calls` AS r2
    ON r2.jkey = t.jkey
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
