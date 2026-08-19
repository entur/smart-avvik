-- Per-call training table for the full-population, "any situation" model family.
--
-- GRAIN: one row per (situation x affected call) for NSB situations, where an "affected call"
-- is a stop on an affected journey whose SCHEDULED time falls within 12h (0..720 min) after the
-- situation's onset (creationTime). This is the prediction unit the product needs: a passenger
-- looks up their boarding stop and their alighting stop on the affected train and reads off
-- this call's outcome. Partial ("turn-short") cancellations are represented naturally because
-- call_cancelled is per stop in rail_calls.
--
-- PLUS, since 2026-08-19, one row per rail call NO situation named at all (has_situation =
-- FALSE) -- see the has_situation section below. Those rows have no situation to be crossed
-- with, so they appear exactly once each.
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
-- has_situation (ADDED 2026-08-19): THE POPULATION IS NOW TWO POPULATIONS.
-- Until now every row here was flagged by an SX message, so the models had never seen a call
-- with no situation -- and the product wants to score exactly those (a traveller's train can be
-- in trouble before, or without, anyone publishing about it). Measured 2026-08-19 in dev, that
-- gap is most of the network: 968 090 distinct flagged calls in this window against 5 919 146
-- unflagged ones, a 6:1 majority the trees had never been shown.
--
-- Serving them through the existing models means inventing values for the SX columns, and BOTH
-- available inventions are wrong (measured on 70 632 flagged calls scored through
-- call_cancel_clf, realized cancel rate 0.0952):
--   actual SX features   mean p 0.0904   AUC 0.9734
--   SX columns NULL      mean p 0.0363   AUC 0.8784   <- deflates 2.5x; training_calls has ZERO
--                                                        NULLs anywhere, so NULL is a branch no
--                                                        tree ever fit
--   SX columns sentinel  mean p 0.0847   AUC 0.8807   <- holds the level, but the level means
--                                                        "an average DISRUPTED call"
-- Neither is a statement the data supports, because "no situation" was never a state the model
-- could represent. The fix is not a better filler: it is to make the state explicit and let the
-- trees fit it, which is what has_situation is. With the flag present, the SX columns' filler
-- values below are IDENTIFIABLE rather than confusable -- a split on has_situation recovers the
-- population exactly, so the tree can learn one rule for flagged calls and another for the rest
-- instead of reading a sentinel as a feature value.
--
-- FILLERS on has_situation = FALSE rows, and why each is free of a real value:
--   status            'none'   -- distinct from 'other', which means "the regex matched no known
--                                 status IN SOME TEXT"; here there is no text
--   category          'none'   -- distinct from 'UNKNOWN', which means "AI.CLASSIFY declined";
--                                 here there was nothing to classify
--   planned           FALSE    -- the one filler that IS a real value; has_situation is what
--                                 disambiguates it, which is the whole point of carrying the flag
--   num_journeys/_stops/_lines  -1  -- ARRAY_LENGTH is never negative
--   stated_min        -1       -- already the "no minutes in the text" sentinel; here "no text"
--   onset_hour/_dow/_month     -1  -- real parts are 0..23 / 1..7 / 1..12
--   min_from_onset    -1       -- real values are 0..720 by construction of the window above
--   line_pre_*, net_pre_*      the existing empty-window sentinels (0 / -1), reached for free:
--                              those CTEs are onset-anchored, so the LEFT JOIN simply misses and
--                              the IFNULLs at the bottom of this file do the rest. There is no
--                              onset to anchor a pre-window on, and inventing a different window
--                              would silently give the two populations DIFFERENT features under
--                              one column name -- the failure this whole change exists to stop.
-- Everything else is genuinely shared and genuinely observable without SX: lineRef, stop_place,
-- stop_order, is_terminal, is_origin, call_hour, call_dow, line_now_* and own_prev_delay. Those
-- last two are the ones that carry the no-SX signal (own_prev_delay attribution 0.46,
-- line_now_delay 0.24), and they are call-anchored, so they mean the same thing in both
-- populations. That is the contract: a column is shared only if it is computed the same way.
--
-- CALIBRATION IS PER POPULATION, NOT SHARED. A single Platt layer over both would be fit mostly
-- on the 6:1 majority and would re-center the flagged calls to the unflagged base rate. The
-- unflagged holdout runs 0.19 % cancelled against the flagged holdout's 5.83 % -- a 30x gap that
-- no single intercept can serve. See create_cancel_calibration.sql / create_exceedance_calibration.sql.
--
-- own_prev_delay is now computed ONCE PER CALL rather than once per (situation, call). It never
-- depended on the situation -- the join is entirely on the call's own jkey/stop_order/time -- so
-- the values are unchanged for flagged rows; keying it per call is what lets the unflagged rows,
-- which have no situationNumber, read the same feature.

-- SPLIT: temporal, keyed on onset date -> a whole situation (all its calls) lands on one side,
-- so there is no within-situation grouping leak. Train = onset < 2026-04-01, eval = on/after.
-- Window is restricted to onset in [2024-06-01, 2026-06-19] so rail_calls fully covers the 12h
-- outcome window (table runs 2024-06-01 .. 2026-06-20); later onsets would be right-censored.
-- Unflagged rows have no onset, so their split (and their window) is keyed on call_date over the
-- same range. A situation with onset just before the boundary keeps its calls in TRAIN while an
-- unflagged call the same morning lands in EVAL; the fuzz is one 12 h window wide and does not
-- leak, since the two populations share no rows.

-- CLUSTER BY situationNumber is left ALONE even though the population split is now the filter
-- every new consumer applies, because situationNumber IS NULL is exactly NOT has_situation --
-- the unflagged rows sort together under NULL, so the existing key already separates them.
-- Leading with has_situation would read better and buy nothing, and it would cost a DROP: BigQuery
-- refuses to change an existing table's clustering through CREATE OR REPLACE ("clustering(...)
-- and existing spec is ..."), so this file would stop being runnable in place against every
-- warehouse that already has the table.
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
    TRUE AS has_situation,
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
-- every rail call NO situation named (has_situation = FALSE). The anti-join is PER CALL, not per
-- journey: a situation reaches 12 h from onset, so one train routinely has some calls inside a
-- situation's window and others outside it, and the ones outside are exactly as unflagged as a
-- call on a train nobody published about. Same validity filters as `base` -- a scheduled time and
-- a realized outcome -- so the two populations differ only in whether SX named them.
-- Every column below is either genuinely observable without a situation or one of the documented
-- fillers; the ORDER must match `base` above, since the UNION ALL is positional.
covered AS (
  SELECT DISTINCT jkey, stop_order, call_date FROM base
),
nosx AS (
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
    CAST(NULL AS TIMESTAMP) AS onset_ts,
    COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts,
    r.call_date >= DATE '2026-04-01' AS is_eval
  FROM `disruptions.rail_calls` AS r
  LEFT JOIN covered AS c
    ON c.jkey = r.jkey AND c.stop_order = r.stop_order AND c.call_date = r.call_date
  WHERE c.jkey IS NULL
    AND r.call_date BETWEEN DATE '2024-06-01' AND DATE '2026-06-19'
    AND (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min IS NOT NULL OR r.arr_delay_min IS NOT NULL)
),
allrows AS (
  SELECT * FROM base
  UNION ALL
  SELECT * FROM nosx
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
-- the train's own delay at its latest stop aimed >=30 min before this call.
-- Keyed per CALL (jkey, stop_order, call_date), not per (situation, call): the join below reads
-- only the call's own train and times, so a situation never changed the answer -- it just made
-- the same value be computed once per situation naming the call. Per-call keying is what lets
-- the has_situation = FALSE rows, which have no situationNumber to join on, read this feature.
own AS (
  SELECT t.jkey, t.stop_order, t.call_date,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)]
           AS own_prev_delay
  FROM (SELECT DISTINCT jkey, stop_order, call_date, call_ts FROM allrows) AS t
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
-- linepre/netpre are onset-anchored, so they simply MISS for has_situation = FALSE rows and the
-- IFNULLs above supply the documented empty-window sentinels. linehour and own are call-anchored
-- and join for both populations.
FROM allrows AS b
LEFT JOIN linepre AS lp ON lp.situationNumber = b.situationNumber AND lp.lineRef = b.lineRef
LEFT JOIN netpre  AS np ON np.situationNumber = b.situationNumber
LEFT JOIN linehour AS lh
  ON lh.lineRef = b.lineRef
 AND lh.h = TIMESTAMP_TRUNC(TIMESTAMP_SUB(b.call_ts, INTERVAL 60 MINUTE), HOUR)
LEFT JOIN own AS o
  ON o.jkey = b.jkey AND o.stop_order = b.stop_order AND o.call_date = b.call_date;
