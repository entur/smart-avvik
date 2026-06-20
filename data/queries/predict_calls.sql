-- SERVING layer for the per-call family: one product-shaped row per (situation x affected call)
-- giving, for a passenger's specific train at a specific stop, the calibrated cancellation
-- probability and the typical + "bad case" (p90) delay. Boarding stop -> read p_cancel +
-- dep_delay_*; alighting stop -> read p_cancel + arr_delay_*.
--
-- Mirrors the per-situation serving path (predict.sql -> predictions_today -> API cache): a batch
-- CREATE OR REPLACE over a recent window of situations. Re-run on each SX ingestion like the
-- others. The API can cache a slice of disruptions.call_predictions and answer per-train queries.
--
-- FEATURE PARITY: the `calls` CTE reproduces create_training_calls.sql's feature construction
-- exactly (same names/types), so ML.PREDICT (which matches features by name) sees no train/serve
-- skew. Passthrough columns (summary, stop_name, aimed_*) are carried through untouched.
--
-- CALL SOURCE: scores against disruptions.rail_calls (the realized table) -> this is "what the
-- model would have predicted AT ONSET for each affected call" (all features are onset/planned,
-- so it is leak-free; only the call's existence is known post-hoc). For true forward serving,
-- swap rail_calls for a PLANNED-timetable source of the affected journeys' upcoming calls (one
-- that carries aimed_dep/aimed_arr/stop_order) -- see sx-et coverage notes in LEARNINGS.
--
-- CALIBRATION applied inline:
--   * P(cancel): Platt closed form p_cal = sigmoid(b + a*logit(p_raw)), (a,b) read live from
--     ML.WEIGHTS(cancel_platt) -> auto-tracks any refit.
--   * delay p90: lookup in delay_p90_calib keyed on ROUND(predicted_log,1), clamped per side.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.call_predictions` AS
WITH
sit AS (
  SELECT
    situationNumber,
    SAFE.TIMESTAMP(creationTime)    AS onset_ts,
    summary,
    status, planned,
    ARRAY_LENGTH(affected_journeys) AS num_journeys,
    ARRAY_LENGTH(affected_stops)    AS num_stops,
    ARRAY_LENGTH(affected_lines)    AS num_lines,
    affected_journeys
  FROM `disruptions.disruptions`
  WHERE participantRef = 'NSB'
    AND SAFE.TIMESTAMP(creationTime) IS NOT NULL
    AND DATE(SAFE.TIMESTAMP(creationTime)) >= DATE_SUB(DATE '2026-06-20', INTERVAL 30 DAY)
),
aff AS (
  SELECT
    s.situationNumber, s.onset_ts, s.summary, s.status, s.planned,
    s.num_journeys, s.num_stops, s.num_lines,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
),
calls AS (
  SELECT
    -- passthrough (not features; ML.PREDICT carries them to the output)
    a.situationNumber, a.onset_ts, a.summary,
    a.jkey, r.lineRef, r.stop_place, r.stop_name, r.aimed_dep, r.aimed_arr,
    -- features (names/types verbatim from create_training_calls.sql)
    IFNULL(a.status, 'other') AS status,
    IFNULL(a.planned, FALSE)  AS planned,
    a.num_journeys, a.num_stops, a.num_lines,
    EXTRACT(HOUR      FROM a.onset_ts) AS onset_hour,
    EXTRACT(DAYOFWEEK FROM a.onset_ts) AS onset_dow,
    EXTRACT(MONTH     FROM a.onset_ts) AS onset_month,
    r.stop_order,
    (r.aimed_dep IS NULL) AS is_terminal,
    (r.aimed_arr IS NULL) AS is_origin,
    TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) AS min_from_onset,
    EXTRACT(HOUR      FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_hour,
    EXTRACT(DAYOFWEEK FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_dow
  FROM aff AS a
  JOIN `disruptions.rail_calls` AS r USING (jkey)
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
),
-- one ML.PREDICT per model over the shared `calls` shape; rejoin on the call key
clf AS (
  SELECT situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.call_cancel_clf`, (SELECT * FROM calls))
),
dep AS (
  SELECT situationNumber, jkey, stop_order, predicted_dep_log AS dep_plog
  FROM ML.PREDICT(MODEL `disruptions.dep_delay_reg`, (SELECT * FROM calls))
),
arr AS (
  SELECT situationNumber, jkey, stop_order, predicted_arr_log AS arr_plog
  FROM ML.PREDICT(MODEL `disruptions.arr_delay_reg`, (SELECT * FROM calls))
),
-- Platt coefficients (live) and p90 bucket bounds per side
w AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt`)
),
bounds AS (
  SELECT side, MIN(plog_bucket) AS lo, MAX(plog_bucket) AS hi
  FROM `disruptions.delay_p90_calib` GROUP BY side
)
SELECT
  c.situationNumber, c.summary, c.onset_ts,
  c.jkey, c.lineRef, c.stop_place, c.stop_name, c.stop_order,
  c.aimed_dep, c.aimed_arr, c.is_terminal, c.is_origin, c.min_from_onset,
  -- calibrated P(this stop is cancelled / train doesn't serve it)
  ROUND(1/(1+EXP(-(w.b + w.a *
    LN(GREATEST(LEAST(clf.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(clf.p_raw,1-1e-6),1e-6)))))), 4) AS p_cancel,
  -- boarding-side delay (NULL at a terminal: no departure)
  IF(c.is_terminal, NULL, GREATEST(CAST(ROUND(EXP(dep.dep_plog)-1) AS INT64), 0)) AS dep_delay_typ_min,
  IF(c.is_terminal, NULL, dpq.p90_min)                                            AS dep_delay_p90_min,
  -- destination-side delay (NULL at an origin: no arrival)
  IF(c.is_origin,   NULL, GREATEST(CAST(ROUND(EXP(arr.arr_plog)-1) AS INT64), 0)) AS arr_delay_typ_min,
  IF(c.is_origin,   NULL, apq.p90_min)                                            AS arr_delay_p90_min
FROM calls AS c
JOIN clf USING (situationNumber, jkey, stop_order)
JOIN dep USING (situationNumber, jkey, stop_order)
JOIN arr USING (situationNumber, jkey, stop_order)
CROSS JOIN w
LEFT JOIN bounds AS bd ON bd.side = 'dep'
LEFT JOIN `disruptions.delay_p90_calib` AS dpq
  ON dpq.side = 'dep' AND dpq.plog_bucket = LEAST(GREATEST(ROUND(dep.dep_plog,1), bd.lo), bd.hi)
LEFT JOIN bounds AS ba ON ba.side = 'arr'
LEFT JOIN `disruptions.delay_p90_calib` AS apq
  ON apq.side = 'arr' AND apq.plog_bucket = LEAST(GREATEST(ROUND(arr.arr_plog,1), ba.lo), ba.hi);
