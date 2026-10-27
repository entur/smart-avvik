-- KIX-42 gate: score SJ Norge's OWN SX situations through the flagged path before serving them.
--
-- SJN's calls named by an SJN situation, in create_training_calls.sql's `base` shape, scored at
-- the leads leadtime_eval.sql uses and calibrated with the FLAGGED Platt layers per lead, as
-- predict_calls.sql serves them. Vy's flagged holdout rows (leadtime_eval_calls, has_situation)
-- are scored beside them. SJN was in no training row and no Platt fit, so the whole SJN history
-- is out of sample; Vy's rows are its temporal holdout (>= 2026-04-01).
--
-- Differences from training, each matching serving rather than training:
--   * jkey: SJN refs are 'SJN:DatedServiceJourney:<nr>_..._<yy-mm-dd>' and ET's are '<nr>:<date>',
--     so the codespace strip does not bridge them; the train number + date does.
--   * category: no SJN text is in disruptions.categories, so they are classified here with the
--     same AI.CLASSIFY call update_categories.sql makes live, into a scratch table.
--   * net_pre_*: from rail_calls (Vy), because predict_calls.sql's netpre is Vy-only since KIX-39.
--     line_pre_*, line_now_* and own_prev_delay read the SJN train and line from sjn_calls.
--
-- Writes disruptions.sjn_eval_categories, disruptions.training_calls_sjn_flagged and
-- disruptions.sjn_flagged_scores; the summary query is in docs/sjn/LEARNINGS.md.

DECLARE cat_array STRING DEFAULT (
  SELECT STRING_AGG(FORMAT('(%T, %T)', category, description), ',\n      ' ORDER BY sort_order)
  FROM `disruptions.category_taxonomy`
);

EXECUTE IMMEDIATE FORMAT(r"""
CREATE OR REPLACE TABLE `disruptions.sjn_eval_categories` AS
WITH texts AS (
  SELECT onset_summary AS summary, onset_description AS description
  FROM `disruptions.disruptions`
  WHERE participantRef = 'SJN' AND (onset_summary IS NOT NULL OR onset_description IS NOT NULL)
  GROUP BY 1, 2
)
SELECT summary, description,
  AI.CLASSIFY(CONCAT(IFNULL(summary, ''), ' ', IFNULL(description, '')), categories => [
      %s
    ]) AS category
FROM texts
""", cat_array);

CREATE OR REPLACE TABLE `disruptions.training_calls_sjn_flagged` AS
WITH sit AS (
  SELECT
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime)    AS onset_ts,
    d.status, d.planned,
    -- SJ names one train under two refs, '..._STK-LMO_26-10-13' and '..._STK-LMO_M_26-10-13'; counted
    -- raw, num_journeys would double for exactly the trains it describes.
    (SELECT COUNT(DISTINCT REGEXP_REPLACE(j, r'_M_(\d{2}-\d{2}-\d{2})$', r'_\1'))
     FROM UNNEST(d.affected_journeys) AS j) AS num_journeys,
    ARRAY_LENGTH(d.affected_stops)    AS num_stops,
    ARRAY_LENGTH(d.affected_lines)    AS num_lines,
    d.affected_journeys,
    IFNULL(c.category, 'UNKNOWN')     AS category,
    IFNULL(SAFE_CAST(REGEXP_EXTRACT(
      LOWER(CONCAT(IFNULL(d.onset_summary,''), ' ', IFNULL(d.onset_description,''))),
      r'([0-9]+)\s*min') AS INT64), -1) AS stated_min
  FROM `disruptions.disruptions` AS d
  LEFT JOIN `disruptions.sjn_eval_categories` AS c
    ON IFNULL(d.onset_summary,'')     = IFNULL(c.summary,'')
   AND IFNULL(d.onset_description,'') = IFNULL(c.description,'')
  WHERE d.participantRef = 'SJN'
    AND SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
    AND DATE(SAFE.TIMESTAMP(d.creationTime)) BETWEEN DATE '2024-06-01' AND DATE '2026-10-11'
    -- serving drops these (disruptions_today.sql): SJ's unfilled template placeholders
    AND NOT REGEXP_CONTAINS(CONCAT(IFNULL(d.onset_summary,''), ' ', IFNULL(d.onset_description,'')),
                            r'\{[^}]+\}')
),
aff AS (
  -- DISTINCT for the same reason: both refs bridge to one jkey
  SELECT DISTINCT s.* EXCEPT(affected_journeys),
    CONCAT(REGEXP_EXTRACT(j, r'^SJN:DatedServiceJourney:(\d+)_'), ':',
           FORMAT_DATE('%Y-%m-%d', SAFE.PARSE_DATE('%y-%m-%d', REGEXP_EXTRACT(j, r'_(\d{2}-\d{2}-\d{2})$'))))
      AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
),
base AS (
  SELECT
    a.situationNumber, a.jkey, r.call_date,
    COALESCE(r.call_cancelled, FALSE) AS call_cancelled,
    IF(NOT COALESCE(r.call_cancelled, FALSE), GREATEST(r.dep_delay_min, 0), NULL) AS dep_delay_pos,
    IF(NOT COALESCE(r.call_cancelled, FALSE), GREATEST(r.arr_delay_min, 0), NULL) AS arr_delay_pos,
    TRUE AS has_situation,
    IFNULL(a.status, 'other') AS status,
    IFNULL(a.planned, FALSE)  AS planned,
    a.num_journeys, a.num_stops, a.num_lines,
    EXTRACT(HOUR      FROM a.onset_ts) AS onset_hour,
    EXTRACT(DAYOFWEEK FROM a.onset_ts) AS onset_dow,
    EXTRACT(MONTH     FROM a.onset_ts) AS onset_month,
    r.lineRef, r.stop_place, r.stop_order,
    (r.aimed_dep IS NULL) AS is_terminal,
    (r.aimed_arr IS NULL) AS is_origin,
    TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) AS min_from_onset,
    EXTRACT(HOUR      FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_hour,
    EXTRACT(DAYOFWEEK FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_dow,
    a.category, a.stated_min,
    a.onset_ts,
    COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts,
    TRUE AS is_eval
  FROM aff AS a
  JOIN `disruptions.sjn_calls` AS r USING (jkey)
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min IS NOT NULL OR r.arr_delay_min IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
),
sitline AS (SELECT DISTINCT situationNumber, lineRef, onset_ts FROM base),
linepre AS (
  SELECT p.situationNumber, p.lineRef,
         COUNT(*) AS line_pre_n,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS line_pre_delay,
         ROUND(AVG(CAST(COALESCE(r.call_cancelled, FALSE) AS INT64)), 3) AS line_pre_cancel
  FROM sitline AS p
  JOIN `disruptions.sjn_calls` AS r
    ON r.lineRef = p.lineRef
   AND r.call_date BETWEEN DATE_SUB(DATE(p.onset_ts, 'Europe/Oslo'), INTERVAL 1 DAY) AND DATE(p.onset_ts, 'Europe/Oslo')
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
    ON r.call_date BETWEEN DATE_SUB(DATE(p.onset_ts, 'Europe/Oslo'), INTERVAL 1 DAY) AND DATE(p.onset_ts, 'Europe/Oslo')
   AND r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1
),
linehour AS (
  SELECT lineRef, TIMESTAMP_TRUNC(aimed_dep, HOUR) AS h,
         COUNT(*) AS line_now_n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS line_now_delay,
         ROUND(AVG(CAST(COALESCE(call_cancelled, FALSE) AS INT64)), 3) AS line_now_cancel
  FROM `disruptions.sjn_calls` WHERE aimed_dep IS NOT NULL GROUP BY 1, 2
),
own AS (
  SELECT t.jkey, t.stop_order, t.call_date,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)] AS own_prev_delay
  FROM (SELECT DISTINCT jkey, stop_order, call_date, call_ts FROM base) AS t
  JOIN `disruptions.sjn_calls` AS r2
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
  ON o.jkey = b.jkey AND o.stop_order = b.stop_order AND o.call_date = b.call_date;

-- Score both populations at each lead (leadtime_eval.sql's re-anchoring), flagged layers per lead.
CREATE OR REPLACE TABLE `disruptions.sjn_flagged_scores` AS
WITH
joined AS (
  SELECT t.*, COALESCE(c.aimed_dep, c.aimed_arr) AS call_ts
  FROM `disruptions.training_calls_sjn_flagged` AS t
  JOIN `disruptions.sjn_calls` AS c USING (jkey, stop_order, call_date)
),
expanded AS (SELECT j.*, lead_min FROM joined AS j CROSS JOIN UNNEST([30, 60, 120, 240]) AS lead_min),
linehour AS (
  SELECT lineRef, TIMESTAMP_TRUNC(aimed_dep, HOUR) AS h, COUNT(*) AS n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS d,
         ROUND(AVG(CAST(COALESCE(call_cancelled, FALSE) AS INT64)), 3) AS c
  FROM `disruptions.sjn_calls` WHERE aimed_dep IS NOT NULL GROUP BY 1, 2
),
own AS (
  SELECT e.jkey, e.stop_order, e.call_date, e.lead_min,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)] AS own_prev
  FROM (SELECT DISTINCT jkey, stop_order, call_ts, call_date, lead_min FROM expanded) AS e
  JOIN `disruptions.sjn_calls` AS r2
    ON r2.jkey = e.jkey
   AND r2.call_date BETWEEN DATE_SUB(e.call_date, INTERVAL 1 DAY) AND e.call_date
   AND r2.stop_order < e.stop_order
   AND COALESCE(r2.aimed_dep, r2.aimed_arr) <= TIMESTAMP_SUB(e.call_ts, INTERVAL e.lead_min MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT COALESCE(r2.call_cancelled, FALSE)
  GROUP BY 1, 2, 3, 4
),
sjn AS (
  SELECT 'SJN' AS pop,
    e.* EXCEPT(call_ts, line_now_n, line_now_delay, line_now_cancel, own_prev_delay),
    IFNULL(lh.n, 0) AS line_now_n, IFNULL(lh.d, -1) AS line_now_delay,
    IFNULL(lh.c, -1) AS line_now_cancel, IFNULL(o.own_prev, -1) AS own_prev_delay
  FROM expanded AS e
  LEFT JOIN linehour AS lh
    ON lh.lineRef = e.lineRef
   AND lh.h = TIMESTAMP_TRUNC(TIMESTAMP_SUB(e.call_ts, INTERVAL (e.lead_min + 30) MINUTE), HOUR)
  LEFT JOIN own AS o USING (jkey, stop_order, call_date, lead_min)
  UNION ALL
  SELECT 'SJN' AS pop,
    j.* EXCEPT(call_ts, line_now_n, line_now_delay, line_now_cancel, own_prev_delay),
    -1 AS lead_min, 0 AS line_now_n, -1.0 AS line_now_delay, -1.0 AS line_now_cancel, -1 AS own_prev_delay
  FROM joined AS j
),
input AS (
  SELECT * FROM sjn
  UNION ALL
  SELECT 'VYG' AS pop, * FROM `disruptions.leadtime_eval_calls` WHERE has_situation
),
-- deterministic key: every ML.PREDICT re-evaluates `keyed` (see eval_sjn_zero_shot.sql)
keyed AS (
  SELECT FARM_FINGERPRINT(CONCAT(pop, '|', situationNumber, '|', jkey, '|', CAST(call_date AS STRING), '|',
                                 CAST(stop_order AS STRING), '|', CAST(lead_min AS STRING))) AS rid,
         IF(lead_min = -1, 'plan', CAST(lead_min AS STRING)) AS lead_bucket, *
  FROM input
),
c AS (
  SELECT rid, (SELECT p.prob FROM UNNEST(predicted_call_cancelled_probs) p WHERE p.label) AS pc
  FROM ML.PREDICT(MODEL `disruptions.call_cancel_clf`, (SELECT * FROM keyed))
),
d10 AS (
  SELECT rid, (SELECT p.prob FROM UNNEST(predicted_y_ge_probs) p WHERE p.label) AS pd
  FROM ML.PREDICT(MODEL `disruptions.dep_ge10_clf`, (SELECT * FROM keyed))
),
-- the flagged layers, '30' being the unsuffixed training-anchored one (predict_calls.sql `wch`/`wdh`)
w AS (
  SELECT 'cancel' AS head, '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt`)
  UNION ALL
  SELECT 'cancel' AS head, '60' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt_lead60`)
  UNION ALL
  SELECT 'cancel' AS head, '120' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt_lead120`)
  UNION ALL
  SELECT 'cancel' AS head, '240' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt_lead240`)
  UNION ALL
  SELECT 'cancel' AS head, 'plan' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.cancel_platt_plan`)
  UNION ALL
  SELECT 'dep_ge10' AS head, '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge10_platt`)
  UNION ALL
  SELECT 'dep_ge10' AS head, '60' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge10_platt_lead60`)
  UNION ALL
  SELECT 'dep_ge10' AS head, '120' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge10_platt_lead120`)
  UNION ALL
  SELECT 'dep_ge10' AS head, '240' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge10_platt_lead240`)
  UNION ALL
  SELECT 'dep_ge10' AS head, 'plan' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `disruptions.dep_ge10_platt_plan`)
)
SELECT
  k.pop, k.lead_bucket, k.situationNumber, k.jkey, k.stop_order, k.lineRef, k.stop_place, k.call_date,
  k.is_terminal, k.status, k.category, k.call_cancelled, k.dep_delay_pos,
  1/(1+EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(c.pc,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.pc,1-1e-6),1e-6)))))) AS p_cancel,
  1/(1+EXP(-(wd.b + wd.a * LN(GREATEST(LEAST(d10.pd,1-1e-6),1e-6)/(1-GREATEST(LEAST(d10.pd,1-1e-6),1e-6)))))) AS p_dep10
FROM keyed AS k
JOIN c USING (rid) JOIN d10 USING (rid)
JOIN w AS wc ON wc.head = 'cancel'   AND wc.lead_bucket = k.lead_bucket
JOIN w AS wd ON wd.head = 'dep_ge10' AND wd.lead_bucket = k.lead_bucket;
