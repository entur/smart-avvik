-- LIVE per-call serving: predict_calls.sql pointed at the PLANNED call source
-- (rail_calls_planned_today) instead of the realized rail_calls, over RECENT situations -- so it
-- scores trains that have NOT run yet. Identical model/calibration logic and feature construction
-- to predict_calls.sql (keep the two in sync); the call source, situation window, and the
-- LIVE-OBSERVABLE state features (below) are what differ.
--
-- STATE FEATURES UNDER THE LIVE INFORMATION CONSTRAINT (2026-07-02, see
-- docs/model-hardening/LEARNINGS.md): training computes line_pre_*/net_pre_*/line_now_*/
-- own_prev_delay from realized history. Live, they come from rail_calls_planned_today's
-- RECORDED calls (dep_delay_min = actual vs aimed), which is exactly what has been observed:
--   * line_pre_*/net_pre_* -- the 60 min before ONSET; for a live (recent-onset) situation that
--     window is in the past, so this matches training. Empty window (e.g. onset near the ET
--     snapshot's start-of-day boundary) -> -1 sentinel, which the models handle.
--   * own_prev_delay -- the train's own realized delay at its latest stop aimed >=30 min before
--     the call. For a train that has not started yet there is no such observation -> -1, the
--     SAME semantic as training ("no upstream observation").
--   * line_now_* -- training anchors this on the hour bucket ~30-90 min before EACH call, which
--     is only observable for calls <= ~1h ahead. Live we NOW-CAST it: one aggregate per line over
--     the last 60 min before CURRENT_TIMESTAMP(). For imminent calls this matches (slightly
--     fresher than) training; for far-future calls it is the best current estimate of that
--     feature, converging to training semantics as the 10-min refresh cycle approaches the call.
--
-- 2026-07-03 ALL CALIBRATION IS ASK-HORIZON-AWARE: every platt layer is fit at the training
-- anchoring (~30 min before the call), so earlier asks are mis-calibrated (alerts
-- overconfident, the ge5 rungs under-predicted -> intervals too optimistic). Each call gets a
-- lead_bucket from minutes-until-call at scoring time (<45 min -> the standard *_platt layers;
-- then *_platt_lead{60,120,240} / *_platt_plan from create_leadtime_calibration.sql; bucket
-- boundaries at 1.5x each horizon, 'plan' beyond 6 h), and every layer -- p_cancel, both
-- alerts, the ge-stack columns and the intervals -- is picked from that bucket. The alerts
-- compose per-horizon cancel + PLAIN per-horizon ge10 (no cross-threshold cascade): the dep
-- side validated out-of-sample in eval/impact_eval.py, the arr side + remaining heads
-- A/B-checked the same way. p_arr_ge10_h / p_dep_ge10_h expose the alert ge10 terms.
--
-- Refresh order for the API: rail_calls_planned_today.sql -> (disruptions refresh) -> this ->
-- cache a slice. Situation window here is "recent" as a stand-in for an active-situations feed
-- (e.g. active_disruptions.sql); narrow it to taste.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.call_predictions_today` AS
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
    AND DATE(SAFE.TIMESTAMP(d.creationTime)) >= DATE_SUB(CURRENT_DATE(), INTERVAL 4 DAY)
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
  JOIN `disruptions.rail_calls_planned_today` AS r USING (jkey)
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
),
-- realized outcomes observed so far today (mirror of rail_calls' "cancelled or operated" filter)
observed AS (
  SELECT lineRef, jkey, stop_order, aimed_dep, dep_delay_min, COALESCE(call_cancelled, FALSE) AS call_cancelled
  FROM `disruptions.rail_calls_planned_today`
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
-- now-cast line state: last 60 observed minutes per line (see header)
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
    IFNULL(o.own_prev_delay, -1)    AS own_prev_delay,
    -- ask-horizon bucket: which platt layer calibrates this call's scores (see header)
    CASE
      WHEN TIMESTAMP_DIFF(b.call_ts, CURRENT_TIMESTAMP(), MINUTE) < 45  THEN '30'
      WHEN TIMESTAMP_DIFF(b.call_ts, CURRENT_TIMESTAMP(), MINUTE) < 90  THEN '60'
      WHEN TIMESTAMP_DIFF(b.call_ts, CURRENT_TIMESTAMP(), MINUTE) < 180 THEN '120'
      WHEN TIMESTAMP_DIFF(b.call_ts, CURRENT_TIMESTAMP(), MINUTE) < 360 THEN '240'
      ELSE 'plan'
    END AS lead_bucket
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
-- per-ask-horizon platt lookups for the alert path (see header): '30' = the standard layers
wch AS (
  SELECT '30' AS lead_bucket, b, a FROM w
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt_lead60`)
  UNION ALL
  SELECT '120',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt_lead120`)
  UNION ALL
  SELECT '240',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt_lead240`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt_plan`)
),
wdh AS (
  SELECT '30' AS lead_bucket, b, a FROM wd10
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge10_platt_lead60`)
  UNION ALL
  SELECT '120',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge10_platt_lead120`)
  UNION ALL
  SELECT '240',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge10_platt_lead240`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge10_platt_plan`)
),
w5h AS (
  SELECT '30' AS lead_bucket, b, a FROM w5
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge5_platt_lead60`)
  UNION ALL
  SELECT '120',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge5_platt_lead120`)
  UNION ALL
  SELECT '240',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge5_platt_lead240`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge5_platt_plan`)
),
w10h AS (
  SELECT '30' AS lead_bucket, b, a FROM w10
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge10_platt_lead60`)
  UNION ALL
  SELECT '120',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge10_platt_lead120`)
  UNION ALL
  SELECT '240',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge10_platt_lead240`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge10_platt_plan`)
),
w20h AS (
  SELECT '30' AS lead_bucket, b, a FROM w20
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge20_platt_lead60`)
  UNION ALL
  SELECT '120',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge20_platt_lead120`)
  UNION ALL
  SELECT '240',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge20_platt_lead240`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge20_platt_plan`)
),
w40h AS (
  SELECT '30' AS lead_bucket, b, a FROM w40
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge40_platt_lead60`)
  UNION ALL
  SELECT '120',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge40_platt_lead120`)
  UNION ALL
  SELECT '240',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge40_platt_lead240`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.arr_ge40_platt_plan`)
),
wd5h AS (
  SELECT '30' AS lead_bucket, b, a FROM wd5
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge5_platt_lead60`)
  UNION ALL
  SELECT '120',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge5_platt_lead120`)
  UNION ALL
  SELECT '240',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge5_platt_lead240`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge5_platt_plan`)
),
wd20h AS (
  SELECT '30' AS lead_bucket, b, a FROM wd20
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge20_platt_lead60`)
  UNION ALL
  SELECT '120',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge20_platt_lead120`)
  UNION ALL
  SELECT '240',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge20_platt_lead240`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge20_platt_plan`)
),
wd40h AS (
  SELECT '30' AS lead_bucket, b, a FROM wd40
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge40_platt_lead60`)
  UNION ALL
  SELECT '120',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge40_platt_lead120`)
  UNION ALL
  SELECT '240',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge40_platt_lead240`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge40_platt_plan`)
),
bounds AS (
  SELECT side, MIN(plog_bucket) AS lo, MAX(plog_bucket) AS hi
  FROM `disruptions.delay_p90_calib` GROUP BY side
),
scored AS (
  SELECT
    c.*, clf.p_raw AS p_cancel_raw, dge10.p_raw AS p_dep10_raw, ge10.p_raw AS p_arr10_raw,
    dep.dep_plog, arr.arr_plog,
    GREATEST(
      1/(1+EXP(-(w5h.b + w5h.a * LN(GREATEST(LEAST(ge5.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge5.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w10h.b + w10h.a * LN(GREATEST(LEAST(ge10.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge10.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w20h.b + w20h.a * LN(GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w40h.b + w40h.a * LN(GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6))))))
    ) AS s5,
    GREATEST(
      1/(1+EXP(-(w10h.b + w10h.a * LN(GREATEST(LEAST(ge10.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge10.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w20h.b + w20h.a * LN(GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w40h.b + w40h.a * LN(GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6))))))
    ) AS s10,
    GREATEST(
      1/(1+EXP(-(w20h.b + w20h.a * LN(GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(w40h.b + w40h.a * LN(GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6))))))
    ) AS s20,
    1/(1+EXP(-(w40h.b + w40h.a * LN(GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)))))) AS s40,
    GREATEST(
      1/(1+EXP(-(wd5h.b + wd5h.a * LN(GREATEST(LEAST(dge5.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge5.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wdh.b + wdh.a * LN(GREATEST(LEAST(dge10.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge10.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd20h.b + wd20h.a * LN(GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd40h.b + wd40h.a * LN(GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6))))))
    ) AS d5,
    GREATEST(
      1/(1+EXP(-(wdh.b + wdh.a * LN(GREATEST(LEAST(dge10.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge10.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd20h.b + wd20h.a * LN(GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd40h.b + wd40h.a * LN(GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6))))))
    ) AS d10,
    GREATEST(
      1/(1+EXP(-(wd20h.b + wd20h.a * LN(GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd40h.b + wd40h.a * LN(GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6))))))
    ) AS d20,
    1/(1+EXP(-(wd40h.b + wd40h.a * LN(GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)))))) AS d40
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
  JOIN w5h   ON w5h.lead_bucket   = c.lead_bucket
  JOIN w10h  ON w10h.lead_bucket  = c.lead_bucket
  JOIN w20h  ON w20h.lead_bucket  = c.lead_bucket
  JOIN w40h  ON w40h.lead_bucket  = c.lead_bucket
  JOIN wd5h  ON wd5h.lead_bucket  = c.lead_bucket
  JOIN wdh   ON wdh.lead_bucket   = c.lead_bucket
  JOIN wd20h ON wd20h.lead_bucket = c.lead_bucket
  JOIN wd40h ON wd40h.lead_bucket = c.lead_bucket
)
SELECT
  s.situationNumber, s.summary, s.onset_ts,
  s.jkey, s.lineRef, s.stop_place, s.stop_name, s.stop_order,
  s.aimed_dep, s.aimed_arr, s.is_terminal, s.is_origin, s.min_from_onset,
  s.et_declared_cancelled,
  ROUND(1/(1+EXP(-(wch.b + wch.a *
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
  -- arrival alert: per-horizon cancel + PLAIN per-horizon arr ge10 (not the s.s10 cascade),
  -- mirroring the dep-side composition validated in eval/impact_eval.py
  IF(s.is_origin, NULL, ROUND(
    1/(1+EXP(-(wch.b + wch.a *
      LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6))))))
    + (1 - 1/(1+EXP(-(wch.b + wch.a *
      LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)))))))
      * 1/(1+EXP(-(w10h.b + w10h.a *
      LN(GREATEST(LEAST(s.p_arr10_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_arr10_raw,1-1e-6),1e-6))))))
  , 4)) AS p_impact,
  -- exceedance stack (departure side = the boarding stop; NULL at a terminal: no departure)
  IF(s.is_terminal, NULL, ROUND(s.d5, 4))  AS p_dep_ge5,
  IF(s.is_terminal, NULL, ROUND(s.d10, 4)) AS p_dep_ge10,
  IF(s.is_terminal, NULL, ROUND(s.d20, 4)) AS p_dep_ge20,
  IF(s.is_terminal, NULL, ROUND(s.d40, 4)) AS p_dep_ge40,
  IF(s.is_terminal, NULL, CAST(ROUND(CASE
    WHEN s.d5  <= 0.5 THEN (1 - 0.5) / GREATEST(1 - s.d5, 0.001) * 5
    WHEN s.d10 <= 0.5 THEN 5  + (s.d5  - 0.5) / GREATEST(s.d5  - s.d10, 0.001) * 5
    WHEN s.d20 <= 0.5 THEN 10 + (s.d10 - 0.5) / GREATEST(s.d10 - s.d20, 0.001) * 10
    WHEN s.d40 <= 0.5 THEN 20 + (s.d20 - 0.5) / GREATEST(s.d20 - s.d40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.d40 / 0.5), 90) END) AS INT64)) AS dep_delay_p50_stack_min,
  IF(s.is_terminal, NULL, CAST(ROUND(CASE
    WHEN s.d5  <= 0.1 THEN (1 - 0.1) / GREATEST(1 - s.d5, 0.001) * 5
    WHEN s.d10 <= 0.1 THEN 5  + (s.d5  - 0.1) / GREATEST(s.d5  - s.d10, 0.001) * 5
    WHEN s.d20 <= 0.1 THEN 10 + (s.d10 - 0.1) / GREATEST(s.d10 - s.d20, 0.001) * 10
    WHEN s.d40 <= 0.1 THEN 20 + (s.d20 - 0.1) / GREATEST(s.d20 - s.d40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.d40 / 0.1), 90) END) AS INT64)) AS dep_delay_p90_stack_min,
  -- boarding alert: P(cancelled OR departs >= 10 min late). Per-horizon cancel + PLAIN
  -- per-horizon ge10 (not the s.d10 cascade) -- the composition validated in eval/impact_eval.py.
  IF(s.is_terminal, NULL, ROUND(
    1/(1+EXP(-(wch.b + wch.a *
      LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6))))))
    + (1 - 1/(1+EXP(-(wch.b + wch.a *
      LN(GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_cancel_raw,1-1e-6),1e-6)))))))
      * 1/(1+EXP(-(wdh.b + wdh.a *
      LN(GREATEST(LEAST(s.p_dep10_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_dep10_raw,1-1e-6),1e-6))))))
  , 4)) AS p_impact_dep,
  -- the plain ge10 alert terms, exposed for transparency:
  -- p_impact(_dep) = p_cancel + (1-p_cancel) * p_arr_ge10_h (resp. p_dep_ge10_h)
  IF(s.is_origin, NULL, ROUND(
    1/(1+EXP(-(w10h.b + w10h.a *
      LN(GREATEST(LEAST(s.p_arr10_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_arr10_raw,1-1e-6),1e-6))))))
  , 4)) AS p_arr_ge10_h,
  IF(s.is_terminal, NULL, ROUND(
    1/(1+EXP(-(wdh.b + wdh.a *
      LN(GREATEST(LEAST(s.p_dep10_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(s.p_dep10_raw,1-1e-6),1e-6))))))
  , 4)) AS p_dep_ge10_h,
  s.lead_bucket AS platt_lead_bucket
FROM scored AS s
JOIN wch  ON wch.lead_bucket  = s.lead_bucket
JOIN wdh  ON wdh.lead_bucket  = s.lead_bucket
JOIN w10h ON w10h.lead_bucket = s.lead_bucket
LEFT JOIN bounds AS bd ON bd.side = 'dep'
LEFT JOIN `disruptions.delay_p90_calib` AS dpq
  ON dpq.side = 'dep' AND dpq.plog_bucket = LEAST(GREATEST(ROUND(s.dep_plog,1), bd.lo), bd.hi)
LEFT JOIN bounds AS ba ON ba.side = 'arr'
LEFT JOIN `disruptions.delay_p90_calib` AS apq
  ON apq.side = 'arr' AND apq.plog_bucket = LEAST(GREATEST(ROUND(s.arr_plog,1), ba.lo), ba.hi);
