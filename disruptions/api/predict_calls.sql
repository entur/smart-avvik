-- LIVE per-call predictions for the API -> disruptions.call_predictions_today.
--
-- Scores today's NSB situations' affected calls through the per-call models + calibrators:
--   * situations from disruptions_initial_today (today's first-version SX). `status` is derived
--     inline from the onset summary/description with the SAME regex as create_disruptions.sql
--     (disruptions_initial_today carries the first-version text but not the status column).
--     `category` joins disruptions.categories on that same first-version text (leak-free, the
--     training join), 'UNKNOWN' fallback; `stated_min` is regexed from it.
--   * planned upcoming calls from rail_calls_planned_today (ET aimed times incl. future stops,
--     plus dep_delay_min on recorded calls -- the live-observable state).
--   * onset + time features come from SAFE.TIMESTAMP(creationTime) in UTC -- VERBATIM parity with
--     create_training_calls.sql (which keyed everything off creationTime, no tz shift). Any drift
--     here (Oslo vs UTC, event_ts vs creationTime) would be train/serve skew, so keep it identical.
--   * 2026-07-02 state features (line_pre_*/net_pre_*/line_now_*/own_prev_delay) computed from
--     rail_calls_planned_today's OBSERVED outcomes only -- see data/queries/predict_calls_today.sql
--     header for the live-information-constraint rationale (line_now_* is a per-line NOW-CAST of
--     the last observed 60 min).
--   * exceedance stack arr_ge{5,10,20,40}_clf + arr_ge*_platt -> calibrated P(arr >= t),
--     monotonicity-enforced survival curve -> arr_delay_p50/p90_stack_min interval and
--     p_impact = p_cancel + (1-p_cancel) * P(>=10).
--
-- Cheap: reads the small materialized rail_calls_planned_today + today's situations + ML.PREDICT
-- (no ET scan here -- that cost is in rail_calls_planned_today.sql, rebuilt on a TTL by main.py).
-- Keep the feature block + scoring tail in sync with data/queries/predict_calls_today.sql.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.call_predictions_today` AS
WITH
cat AS (
  SELECT summary, description, ANY_VALUE(category) AS category
  FROM `ent-kixavvik-dev.disruptions.categories`
  GROUP BY summary, description
),
sit AS (
  SELECT
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime) AS onset_ts,
    d.summary,
    CASE
      WHEN REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(d.summary,''),' ',IFNULL(d.description,''))),
                           r'innstil|kansel')                        THEN 'innstilt'
      WHEN REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(d.summary,''),' ',IFNULL(d.description,''))),
                           r'forsink|toget står')                    THEN 'forsinket'
      WHEN REGEXP_CONTAINS(LOWER(IFNULL(d.summary,'')),
                           r'færre vogner|forreste vogn|fremste vogn|bruk de') THEN 'kapasitet'
      ELSE 'other'
    END AS status,
    d.planned,
    ARRAY_LENGTH(d.affected_journeys) AS num_journeys,
    ARRAY_LENGTH(d.affected_stops)    AS num_stops,
    ARRAY_LENGTH(d.affected_lines)    AS num_lines,
    d.affected_journeys,
    IFNULL(c.category, 'UNKNOWN')     AS category,
    IFNULL(SAFE_CAST(REGEXP_EXTRACT(
      LOWER(CONCAT(IFNULL(d.summary,''), ' ', IFNULL(d.description,''))),
      r'([0-9]+)\s*min') AS INT64), -1) AS stated_min
  FROM `ent-kixavvik-dev.disruptions.disruptions_initial_today` AS d
  LEFT JOIN cat AS c
    ON IFNULL(d.summary,'')     = IFNULL(c.summary,'')
   AND IFNULL(d.description,'') = IFNULL(c.description,'')
  WHERE SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
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
    a.situationNumber, a.onset_ts, a.summary,
    a.jkey, r.lineRef, r.stop_place, r.stop_name, r.aimed_dep, r.aimed_arr,
    r.call_cancelled AS et_declared_cancelled,
    COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts,
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
  JOIN `ent-kixavvik-dev.disruptions.rail_calls_planned_today` AS r USING (jkey)
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
),
observed AS (
  SELECT lineRef, jkey, stop_order, aimed_dep, dep_delay_min, COALESCE(call_cancelled, FALSE) AS call_cancelled
  FROM `ent-kixavvik-dev.disruptions.rail_calls_planned_today`
  WHERE COALESCE(call_cancelled, FALSE) OR dep_delay_min IS NOT NULL
),
sitline AS (
  SELECT DISTINCT situationNumber, lineRef, onset_ts FROM base
),
linepre AS (
  SELECT p.situationNumber, p.lineRef,
         COUNT(*) AS line_pre_n,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS line_pre_delay,
         ROUND(AVG(CAST(r.call_cancelled AS INT64)), 3) AS line_pre_cancel
  FROM sitline AS p
  JOIN observed AS r
    ON r.lineRef = p.lineRef
   AND r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1, 2
),
netpre AS (
  SELECT p.situationNumber,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS net_pre_delay,
         ROUND(AVG(CAST(r.call_cancelled AS INT64)), 3) AS net_pre_cancel
  FROM (SELECT DISTINCT situationNumber, onset_ts FROM sitline) AS p
  JOIN observed AS r
    ON r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1
),
linenow AS (
  SELECT lineRef,
         COUNT(*) AS line_now_n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS line_now_delay,
         ROUND(AVG(CAST(call_cancelled AS INT64)), 3) AS line_now_cancel
  FROM observed
  WHERE aimed_dep >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 60 MINUTE)
    AND aimed_dep <  CURRENT_TIMESTAMP()
  GROUP BY 1
),
own AS (
  SELECT t.situationNumber, t.jkey, t.stop_order,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)]
           AS own_prev_delay
  FROM base AS t
  JOIN observed AS r2
    ON r2.jkey = t.jkey
   AND r2.stop_order < t.stop_order
   AND r2.aimed_dep <= TIMESTAMP_SUB(t.call_ts, INTERVAL 30 MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT r2.call_cancelled
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
    IFNULL(ln.line_now_n, 0)        AS line_now_n,
    IFNULL(ln.line_now_delay, -1)   AS line_now_delay,
    IFNULL(ln.line_now_cancel, -1)  AS line_now_cancel,
    IFNULL(o.own_prev_delay, -1)    AS own_prev_delay
  FROM base AS b
  LEFT JOIN linepre AS lp ON lp.situationNumber = b.situationNumber AND lp.lineRef = b.lineRef
  LEFT JOIN netpre  AS np ON np.situationNumber = b.situationNumber
  LEFT JOIN linenow AS ln ON ln.lineRef = b.lineRef
  LEFT JOIN own AS o
    ON o.situationNumber = b.situationNumber AND o.jkey = b.jkey AND o.stop_order = b.stop_order
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
bounds AS (
  SELECT side, MIN(plog_bucket) AS lo, MAX(plog_bucket) AS hi
  FROM `disruptions.delay_p90_calib` GROUP BY side
),
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
    1/(1+EXP(-(w40.b + w40.a * LN(GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)))))) AS s40
  FROM calls AS c
  JOIN clf  USING (situationNumber, jkey, stop_order)
  JOIN dep  USING (situationNumber, jkey, stop_order)
  JOIN arr  USING (situationNumber, jkey, stop_order)
  JOIN ge5  USING (situationNumber, jkey, stop_order)
  JOIN ge10 USING (situationNumber, jkey, stop_order)
  JOIN ge20 USING (situationNumber, jkey, stop_order)
  JOIN ge40 USING (situationNumber, jkey, stop_order)
  CROSS JOIN w5 CROSS JOIN w10 CROSS JOIN w20 CROSS JOIN w40
)
SELECT
  s.situationNumber, s.summary, s.onset_ts,
  s.jkey, s.lineRef, s.stop_place, s.stop_name, s.stop_order,
  s.aimed_dep, s.aimed_arr, s.is_terminal, s.is_origin, s.min_from_onset,
  s.et_declared_cancelled,
  ROUND(1/(1+EXP(-(w.b + w.a *
    LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)))))), 4) AS p_cancel,
  IF(s.is_terminal, NULL, GREATEST(CAST(ROUND(EXP(s.dep_plog)-1) AS INT64), 0)) AS dep_delay_typ_min,
  IF(s.is_terminal, NULL, dpq.p90_min)                                          AS dep_delay_p90_min,
  IF(s.is_origin,   NULL, GREATEST(CAST(ROUND(EXP(s.arr_plog)-1) AS INT64), 0)) AS arr_delay_typ_min,
  IF(s.is_origin,   NULL, apq.p90_min)                                          AS arr_delay_p90_min,
  IF(s.is_origin, NULL, ROUND(s.s5, 4))  AS p_arr_ge5,
  IF(s.is_origin, NULL, ROUND(s.s10, 4)) AS p_arr_ge10,
  IF(s.is_origin, NULL, ROUND(s.s20, 4)) AS p_arr_ge20,
  IF(s.is_origin, NULL, ROUND(s.s40, 4)) AS p_arr_ge40,
  IF(s.is_origin, NULL, CAST(ROUND(CASE
    WHEN s.s5  <= 0.5 THEN (1 - 0.5) / GREATEST(1 - s.s5, 0.001) * 5
    WHEN s.s10 <= 0.5 THEN 5  + (s.s5  - 0.5) / GREATEST(s.s5  - s.s10, 0.001) * 5
    WHEN s.s20 <= 0.5 THEN 10 + (s.s10 - 0.5) / GREATEST(s.s10 - s.s20, 0.001) * 10
    WHEN s.s40 <= 0.5 THEN 20 + (s.s20 - 0.5) / GREATEST(s.s20 - s.s40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.s40 / 0.5), 90) END) AS INT64)) AS arr_delay_p50_stack_min,
  IF(s.is_origin, NULL, CAST(ROUND(CASE
    WHEN s.s5  <= 0.1 THEN (1 - 0.1) / GREATEST(1 - s.s5, 0.001) * 5
    WHEN s.s10 <= 0.1 THEN 5  + (s.s5  - 0.1) / GREATEST(s.s5  - s.s10, 0.001) * 5
    WHEN s.s20 <= 0.1 THEN 10 + (s.s10 - 0.1) / GREATEST(s.s10 - s.s20, 0.001) * 10
    WHEN s.s40 <= 0.1 THEN 20 + (s.s20 - 0.1) / GREATEST(s.s20 - s.s40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.s40 / 0.1), 90) END) AS INT64)) AS arr_delay_p90_stack_min,
  IF(s.is_origin, NULL, ROUND(
    1/(1+EXP(-(w.b + w.a *
      LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6))))))
    + (1 - 1/(1+EXP(-(w.b + w.a *
      LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6))))))) * s.s10
  , 4)) AS p_impact
FROM scored AS s
CROSS JOIN w
LEFT JOIN bounds AS bd ON bd.side = 'dep'
LEFT JOIN `disruptions.delay_p90_calib` AS dpq
  ON dpq.side = 'dep' AND dpq.plog_bucket = LEAST(GREATEST(ROUND(s.dep_plog,1), bd.lo), bd.hi)
LEFT JOIN bounds AS ba ON ba.side = 'arr'
LEFT JOIN `disruptions.delay_p90_calib` AS apq
  ON apq.side = 'arr' AND apq.plog_bucket = LEAST(GREATEST(ROUND(s.arr_plog,1), ba.lo), ba.hi);
