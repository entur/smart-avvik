-- LIVE per-call serving: predict_calls.sql pointed at the PLANNED call source
-- (rail_calls_planned_today) instead of the realized rail_calls, over RECENT situations -- so it
-- scores trains that have NOT run yet. Identical model/calibration logic and feature construction
-- to predict_calls.sql (keep the two in sync); only the call source and situation window differ.
--
-- Refresh order for the API: rail_calls_planned_today.sql -> (disruptions refresh) -> this ->
-- cache a slice. Situation window here is "recent" as a stand-in for an active-situations feed
-- (e.g. active_disruptions.sql); narrow it to taste.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.call_predictions_today` AS
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
    AND DATE(SAFE.TIMESTAMP(creationTime)) >= DATE_SUB(CURRENT_DATE(), INTERVAL 4 DAY)
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
    a.situationNumber, a.onset_ts, a.summary,
    a.jkey, r.lineRef, r.stop_place, r.stop_name, r.aimed_dep, r.aimed_arr,
    r.call_cancelled AS et_declared_cancelled,
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
  JOIN `disruptions.rail_calls_planned_today` AS r USING (jkey)
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
),
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
  c.et_declared_cancelled,
  ROUND(1/(1+EXP(-(w.b + w.a *
    LN(GREATEST(LEAST(clf.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(clf.p_raw,1-1e-6),1e-6)))))), 4) AS p_cancel,
  IF(c.is_terminal, NULL, GREATEST(CAST(ROUND(EXP(dep.dep_plog)-1) AS INT64), 0)) AS dep_delay_typ_min,
  IF(c.is_terminal, NULL, dpq.p90_min)                                            AS dep_delay_p90_min,
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
