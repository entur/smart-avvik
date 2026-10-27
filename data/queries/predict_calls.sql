-- SERVING layer for the per-call family: one product-shaped row per (situation x affected call)
-- giving, for a passenger's specific train at a specific stop, the calibrated cancellation
-- probability, the typical + "bad case" (p90) delay, and (2026-07-02) the EXCEEDANCE STACKS on
-- BOTH sides: calibrated P(delay >= 5/10/20/40), a delay interval read off each survival curve,
-- and the alert probabilities p_impact (arrival) / p_impact_dep (departure), each
-- = p_cancel + (1-p_cancel)*P(>=10 on that side).
-- Boarding stop -> read p_cancel + dep_delay_* + p_dep_ge* + p_impact_dep; alighting stop ->
-- read p_cancel + arr_* + p_arr_ge* + p_impact.
--
-- Mirrors the per-situation serving path (predict.sql -> predictions_today -> API cache): a batch
-- CREATE OR REPLACE over a recent window of situations. Re-run on each SX ingestion like the
-- others. The API can cache a slice of disruptions.call_predictions and answer per-train queries.
--
-- FEATURE PARITY: the `calls` CTE + state CTEs reproduce create_training_calls.sql's feature
-- construction exactly (same names/types/sentinels), so ML.PREDICT (which matches features by
-- name) sees no train/serve skew. Passthrough columns are carried through untouched.
--
-- CALL SOURCE: scores against disruptions.rail_calls (the realized table) -> this is "what the
-- model would have predicted for each affected call" (features are onset/planned/pre-call-
-- observable, so it is leak-free; only the call's existence is known post-hoc). For true forward
-- serving see predict_calls_today.sql (planned source + live-observable state features).
--
-- CALIBRATION applied inline:
--   * P(cancel) and each P(arr>=t): Platt closed form p_cal = sigmoid(b + a*logit(p_raw)),
--     (a,b) read live from ML.WEIGHTS(<platt model>) -> auto-tracks any refit.
--   * delay p90 (regressor path): lookup in delay_p90_calib keyed on ROUND(predicted_log,1).
--   * interval (stack path): monotonicity-enforced survival curve (GREATEST cascade), then
--     linear interpolation of the q=0.5 (p50) and q=0.1 (p90) crossings; tail extrapolated
--     as 40*SQRT(s40/q), capped at 90 min.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.call_predictions` AS
WITH
cat AS (
  SELECT summary, description, ANY_VALUE(category) AS category
  FROM `disruptions.categories`
  GROUP BY summary, description
),
sit AS (
  SELECT
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime)    AS onset_ts,
    d.summary,
    d.status, d.planned,
    ARRAY_LENGTH(d.affected_journeys) AS num_journeys,
    ARRAY_LENGTH(d.affected_stops)    AS num_stops,
    ARRAY_LENGTH(d.affected_lines)    AS num_lines,
    d.affected_journeys,
    IFNULL(c.category, 'UNKNOWN')     AS category,
    IFNULL(SAFE_CAST(REGEXP_EXTRACT(
      LOWER(CONCAT(IFNULL(d.onset_summary,''), ' ', IFNULL(d.onset_description,''))),
      r'([0-9]+)\s*min') AS INT64), -1) AS stated_min
  FROM `disruptions.disruptions` AS d
  LEFT JOIN cat AS c
    ON IFNULL(d.onset_summary,'')     = IFNULL(c.summary,'')
   AND IFNULL(d.onset_description,'') = IFNULL(c.description,'')
  WHERE d.participantRef = 'NSB'
    AND SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
    AND DATE(SAFE.TIMESTAMP(d.creationTime)) >= DATE_SUB(DATE '2026-06-20', INTERVAL 30 DAY)
),
aff AS (
  SELECT
    s.situationNumber, s.onset_ts, s.summary, s.status, s.planned,
    s.num_journeys, s.num_stops, s.num_lines, s.category, s.stated_min,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
),
base AS (
  SELECT
    -- passthrough (not features; ML.PREDICT carries them to the output)
    a.situationNumber, a.onset_ts, a.summary,
    a.jkey, r.lineRef, r.stop_place, r.stop_name, r.aimed_dep, r.aimed_arr,
    r.call_date, COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts,
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
    EXTRACT(DAYOFWEEK FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_dow,
    a.category, a.stated_min
  FROM aff AS a
  JOIN `disruptions.rail_calls` AS r USING (jkey)
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
),
-- state features (parity with create_training_calls.sql, computed over rail_calls)
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
linehour AS (
  SELECT lineRef, TIMESTAMP_TRUNC(aimed_dep, HOUR) AS h,
         COUNT(*) AS line_now_n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS line_now_delay,
         ROUND(AVG(CAST(COALESCE(call_cancelled, FALSE) AS INT64)), 3) AS line_now_cancel
  FROM `disruptions.rail_calls`
  WHERE aimed_dep IS NOT NULL
    AND call_date >= DATE_SUB(DATE '2026-06-20', INTERVAL 32 DAY)
  GROUP BY 1, 2
),
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
),
calls AS (
  SELECT
    b.*,
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
    ON o.situationNumber = b.situationNumber AND o.jkey = b.jkey AND o.stop_order = b.stop_order
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
ge5 AS (
  SELECT situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.arr_ge5_clf`, (SELECT * FROM calls))
),
ge10 AS (
  SELECT situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.arr_ge10_clf`, (SELECT * FROM calls))
),
ge20 AS (
  SELECT situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.arr_ge20_clf`, (SELECT * FROM calls))
),
ge40 AS (
  SELECT situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.arr_ge40_clf`, (SELECT * FROM calls))
),
dge5 AS (
  SELECT situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.dep_ge5_clf`, (SELECT * FROM calls))
),
dge10 AS (
  SELECT situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.dep_ge10_clf`, (SELECT * FROM calls))
),
dge20 AS (
  SELECT situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.dep_ge20_clf`, (SELECT * FROM calls))
),
dge40 AS (
  SELECT situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.dep_ge40_clf`, (SELECT * FROM calls))
),
-- Platt coefficients (live) per calibrated model, and p90 bucket bounds per side
w AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt`)
),
w5 AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge5_platt`)
),
w10 AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge10_platt`)
),
w20 AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge20_platt`)
),
w40 AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge40_platt`)
),
wd5 AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge5_platt`)
),
wd10 AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge10_platt`)
),
wd20 AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge20_platt`)
),
wd40 AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge40_platt`)
),
bounds AS (
  SELECT side, MIN(plog_bucket) AS lo, MAX(plog_bucket) AS hi
  FROM `disruptions.delay_p90_calib` GROUP BY side
),
-- calibrate + monotonicity-enforce the survival curve (a per-threshold Platt refit can cross)
scored AS (
  SELECT
    c.*, clf.p_raw AS p_cancel_raw, dep.dep_plog, arr.arr_plog,
    GREATEST(
      1/(1+EXP(-(w5.b + w5.a * LN(GREATEST(LEAST(ge5.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge5.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w10.b + w10.a * LN(GREATEST(LEAST(ge10.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge10.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w20.b + w20.a * LN(GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w40.b + w40.a * LN(GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6))))))
    ) AS s5,
    GREATEST(
      1/(1+EXP(-(w10.b + w10.a * LN(GREATEST(LEAST(ge10.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge10.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w20.b + w20.a * LN(GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w40.b + w40.a * LN(GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6))))))
    ) AS s10,
    GREATEST(
      1/(1+EXP(-(w20.b + w20.a * LN(GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w40.b + w40.a * LN(GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6))))))
    ) AS s20,
    1/(1+EXP(-(w40.b + w40.a * LN(GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)))))) AS s40,
    GREATEST(
      1/(1+EXP(-(wd5.b + wd5.a * LN(GREATEST(LEAST(dge5.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge5.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd10.b + wd10.a * LN(GREATEST(LEAST(dge10.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge10.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd20.b + wd20.a * LN(GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd40.b + wd40.a * LN(GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6))))))
    ) AS d5,
    GREATEST(
      1/(1+EXP(-(wd10.b + wd10.a * LN(GREATEST(LEAST(dge10.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge10.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd20.b + wd20.a * LN(GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd40.b + wd40.a * LN(GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6))))))
    ) AS d10,
    GREATEST(
      1/(1+EXP(-(wd20.b + wd20.a * LN(GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd40.b + wd40.a * LN(GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6))))))
    ) AS d20,
    1/(1+EXP(-(wd40.b + wd40.a * LN(GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)))))) AS d40
  FROM calls AS c
  JOIN clf  USING (situationNumber, jkey, stop_order)
  JOIN dep  USING (situationNumber, jkey, stop_order)
  JOIN arr  USING (situationNumber, jkey, stop_order)
  JOIN ge5  USING (situationNumber, jkey, stop_order)
  JOIN ge10 USING (situationNumber, jkey, stop_order)
  JOIN ge20 USING (situationNumber, jkey, stop_order)
  JOIN ge40 USING (situationNumber, jkey, stop_order)
  JOIN dge5  USING (situationNumber, jkey, stop_order)
  JOIN dge10 USING (situationNumber, jkey, stop_order)
  JOIN dge20 USING (situationNumber, jkey, stop_order)
  JOIN dge40 USING (situationNumber, jkey, stop_order)
  CROSS JOIN w5 CROSS JOIN w10 CROSS JOIN w20 CROSS JOIN w40
  CROSS JOIN wd5 CROSS JOIN wd10 CROSS JOIN wd20 CROSS JOIN wd40
)
SELECT
  s.situationNumber, s.summary, s.onset_ts,
  s.jkey, s.lineRef, s.stop_place, s.stop_name, s.stop_order,
  s.aimed_dep, s.aimed_arr, s.is_terminal, s.is_origin, s.min_from_onset,
  -- calibrated P(this stop is cancelled / train doesn't serve it)
  ROUND(1/(1+EXP(-(w.b + w.a *
    LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)))))), 4) AS p_cancel,
  -- boarding-side delay (NULL at a terminal: no departure)
  IF(s.is_terminal, NULL, GREATEST(CAST(ROUND(EXP(s.dep_plog)-1) AS INT64), 0)) AS dep_delay_typ_min,
  IF(s.is_terminal, NULL, dpq.p90_min)                                          AS dep_delay_p90_min,
  -- destination-side delay (NULL at an origin: no arrival)
  IF(s.is_origin,   NULL, GREATEST(CAST(ROUND(EXP(s.arr_plog)-1) AS INT64), 0)) AS arr_delay_typ_min,
  IF(s.is_origin,   NULL, apq.p90_min)                                          AS arr_delay_p90_min,
  -- exceedance stack (arrival side; NULL at an origin). P(>= t | train runs), calibrated.
  IF(s.is_origin, NULL, ROUND(s.s5, 4))  AS p_arr_ge5,
  IF(s.is_origin, NULL, ROUND(s.s10, 4)) AS p_arr_ge10,
  IF(s.is_origin, NULL, ROUND(s.s20, 4)) AS p_arr_ge20,
  IF(s.is_origin, NULL, ROUND(s.s40, 4)) AS p_arr_ge40,
  -- delay interval from the survival curve: p50 and p90 crossings (log-linear and floored below
  -- 5 min, linear on the {5,10,20,40} grid, exponential-ish tail extrapolation capped at 90 min;
  -- disruptions/api/predict_calls.sql's arr_delay_p50_stack_min says why)
  IF(s.is_origin, NULL, CAST(ROUND(CASE
    WHEN s.s5  <= 0.5 THEN FLOOR(5 * LN(0.5) / LN(GREATEST(s.s5, 1e-6)))
    WHEN s.s10 <= 0.5 THEN 5  + (s.s5  - 0.5) / GREATEST(s.s5  - s.s10, 0.001) * 5
    WHEN s.s20 <= 0.5 THEN 10 + (s.s10 - 0.5) / GREATEST(s.s10 - s.s20, 0.001) * 10
    WHEN s.s40 <= 0.5 THEN 20 + (s.s20 - 0.5) / GREATEST(s.s20 - s.s40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.s40 / 0.5), 90) END) AS INT64)) AS arr_delay_p50_stack_min,
  IF(s.is_origin, NULL, CAST(ROUND(CASE
    WHEN s.s5  <= 0.1 THEN FLOOR(5 * LN(0.1) / LN(GREATEST(s.s5, 1e-6)))
    WHEN s.s10 <= 0.1 THEN 5  + (s.s5  - 0.1) / GREATEST(s.s5  - s.s10, 0.001) * 5
    WHEN s.s20 <= 0.1 THEN 10 + (s.s10 - 0.1) / GREATEST(s.s10 - s.s20, 0.001) * 10
    WHEN s.s40 <= 0.1 THEN 20 + (s.s20 - 0.1) / GREATEST(s.s20 - s.s40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.s40 / 0.1), 90) END) AS INT64)) AS arr_delay_p90_stack_min,
  -- the alert probability: P(cancelled OR >= 10 min late at this stop)
  IF(s.is_origin, NULL, ROUND(
    1/(1+EXP(-(w.b + w.a *
      LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6))))))
    + (1 - 1/(1+EXP(-(w.b + w.a *
      LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6))))))) * s.s10
  , 4)) AS p_impact,
  -- exceedance stack (departure side = the boarding stop; NULL at a terminal: no departure)
  IF(s.is_terminal, NULL, ROUND(s.d5, 4))  AS p_dep_ge5,
  IF(s.is_terminal, NULL, ROUND(s.d10, 4)) AS p_dep_ge10,
  IF(s.is_terminal, NULL, ROUND(s.d20, 4)) AS p_dep_ge20,
  IF(s.is_terminal, NULL, ROUND(s.d40, 4)) AS p_dep_ge40,
  IF(s.is_terminal, NULL, CAST(ROUND(CASE
    WHEN s.d5  <= 0.5 THEN FLOOR(5 * LN(0.5) / LN(GREATEST(s.d5, 1e-6)))
    WHEN s.d10 <= 0.5 THEN 5  + (s.d5  - 0.5) / GREATEST(s.d5  - s.d10, 0.001) * 5
    WHEN s.d20 <= 0.5 THEN 10 + (s.d10 - 0.5) / GREATEST(s.d10 - s.d20, 0.001) * 10
    WHEN s.d40 <= 0.5 THEN 20 + (s.d20 - 0.5) / GREATEST(s.d20 - s.d40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.d40 / 0.5), 90) END) AS INT64)) AS dep_delay_p50_stack_min,
  IF(s.is_terminal, NULL, CAST(ROUND(CASE
    WHEN s.d5  <= 0.1 THEN FLOOR(5 * LN(0.1) / LN(GREATEST(s.d5, 1e-6)))
    WHEN s.d10 <= 0.1 THEN 5  + (s.d5  - 0.1) / GREATEST(s.d5  - s.d10, 0.001) * 5
    WHEN s.d20 <= 0.1 THEN 10 + (s.d10 - 0.1) / GREATEST(s.d10 - s.d20, 0.001) * 10
    WHEN s.d40 <= 0.1 THEN 20 + (s.d20 - 0.1) / GREATEST(s.d20 - s.d40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.d40 / 0.1), 90) END) AS INT64)) AS dep_delay_p90_stack_min,
  -- boarding alert: P(cancelled OR departs >= 10 min late)
  IF(s.is_terminal, NULL, ROUND(
    1/(1+EXP(-(w.b + w.a *
      LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6))))))
    + (1 - 1/(1+EXP(-(w.b + w.a *
      LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6))))))) * s.d10
  , 4)) AS p_impact_dep
FROM scored AS s
CROSS JOIN w
LEFT JOIN bounds AS bd ON bd.side = 'dep'
LEFT JOIN `disruptions.delay_p90_calib` AS dpq
  ON dpq.side = 'dep' AND dpq.plog_bucket = LEAST(GREATEST(ROUND(s.dep_plog,1), bd.lo), bd.hi)
LEFT JOIN bounds AS ba ON ba.side = 'arr'
LEFT JOIN `disruptions.delay_p90_calib` AS apq
  ON apq.side = 'arr' AND apq.plog_bucket = LEAST(GREATEST(ROUND(s.arr_plog,1), ba.lo), ba.hi);
