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
-- LEAKAGE: features are onset-time / planned-timetable only. Deliberately EXCLUDED as leaky:
--   - duration_minutes, severity  -> from the LATEST snapshot in disruptions (resolved state)
--   - summary/description         -> latest text ("Toget kjører igjen" leaks the outcome)
--   - actual_dep/arr, call_source -> realized operation (these ARE the labels)
-- Kept (leak-free): status (onset-derived regex), planned, affected-counts, onset time parts,
-- and per-call PLANNED attributes (line, stop, stop_order, scheduled time, position).
--
-- SPLIT: temporal, keyed on onset date -> a whole situation (all its calls) lands on one side,
-- so there is no within-situation grouping leak. Train = onset < 2026-04-01, eval = on/after.
-- Window is restricted to onset in [2024-06-01, 2026-06-19] so rail_calls fully covers the 12h
-- outcome window (table runs 2024-06-01 .. 2026-06-20); later onsets would be right-censored.

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.training_calls`
PARTITION BY call_date
CLUSTER BY situationNumber AS
WITH sit AS (
  SELECT
    situationNumber,
    SAFE.TIMESTAMP(creationTime)    AS onset_ts,
    status,
    planned,
    ARRAY_LENGTH(affected_journeys) AS num_journeys,
    ARRAY_LENGTH(affected_stops)    AS num_stops,
    ARRAY_LENGTH(affected_lines)    AS num_lines,
    affected_journeys
  FROM `disruptions.disruptions`
  WHERE participantRef = 'NSB'
    AND SAFE.TIMESTAMP(creationTime) IS NOT NULL
    AND DATE(SAFE.TIMESTAMP(creationTime)) BETWEEN DATE '2024-06-01' AND DATE '2026-06-19'
),
aff AS (
  SELECT
    s.situationNumber, s.onset_ts, s.status, s.planned,
    s.num_journeys, s.num_stops, s.num_lines,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
)
SELECT
  -- keys (EXCEPT'd from features at train time)
  a.situationNumber,
  a.jkey,
  r.call_date,
  -- labels (realized; EXCEPT'd from features at train time). A NULL call_cancelled is read as
  -- FALSE (SIRI convention: absent cancellation = served) so the classifier label is non-NULL.
  COALESCE(r.call_cancelled, FALSE) AS call_cancelled,
  IF(NOT COALESCE(r.call_cancelled, FALSE), GREATEST(r.dep_delay_min, 0), NULL) AS dep_delay_pos,
  IF(NOT COALESCE(r.call_cancelled, FALSE), GREATEST(r.arr_delay_min, 0), NULL) AS arr_delay_pos,
  -- situation features (leak-free / onset)
  IFNULL(a.status, 'other') AS status,
  IFNULL(a.planned, FALSE)  AS planned,
  a.num_journeys,
  a.num_stops,
  a.num_lines,
  EXTRACT(HOUR      FROM a.onset_ts) AS onset_hour,
  EXTRACT(DAYOFWEEK FROM a.onset_ts) AS onset_dow,
  EXTRACT(MONTH     FROM a.onset_ts) AS onset_month,
  -- call features (planned attributes, leak-free)
  r.lineRef,
  r.stop_place,
  r.stop_order,
  (r.aimed_dep IS NULL) AS is_terminal,
  (r.aimed_arr IS NULL) AS is_origin,
  TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) AS min_from_onset,
  EXTRACT(HOUR      FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_hour,
  EXTRACT(DAYOFWEEK FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_dow,
  -- temporal split (EXCEPT'd from features)
  DATE(a.onset_ts) >= DATE '2026-04-01' AS is_eval
FROM aff AS a
JOIN `disruptions.rail_calls` AS r USING (jkey)
WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
  AND (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min IS NOT NULL OR r.arr_delay_min IS NOT NULL)
  AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720;
