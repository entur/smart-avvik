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
--   * exceedance stacks arr_ge{5,10,20,40}_clf + dep_ge{5,10,20,40}_clf with their *_platt
--     layers -> calibrated P(delay >= t) per side, monotonicity-enforced survival curves ->
--     arr_delay_p50/p90_stack_min + dep_delay_p50/p90_stack_min intervals and the alerts
--     p_impact (arrival) / p_impact_dep (departure) = p_cancel + (1-p_cancel) * P(>=10).
--   * 2026-07-03 ALL CALIBRATION IS ASK-HORIZON-AWARE: every platt layer is fit at the
--     training anchoring (~30 min before the call), so earlier asks are mis-calibrated
--     (alerts overconfident, the ge5 rungs under-predicted -> intervals too optimistic).
--     Each call gets a lead_bucket from minutes-until-call at scoring time (<45 min -> the
--     standard *_platt layers; then *_platt_lead{60,120,240} / *_platt_plan from
--     create_leadtime_calibration.sql; bucket boundaries at 1.5x each horizon, 'plan' beyond
--     6 h), and every layer -- p_cancel, both alerts, the ge-stack columns and the intervals
--     -- is picked from that bucket. The alerts compose per-horizon cancel + PLAIN per-horizon
--     ge10 (no cross-threshold cascade): the dep side validated out-of-sample in
--     eval/impact_eval.py, the arr side + remaining heads A/B-checked the same way.
--     p_arr_ge10_h / p_dep_ge10_h expose the alert ge10 terms; platt_lead_bucket the bucket.
--
-- PLUS, since 2026-08-19, one row per call ET has ALREADY DECLARED CANCELLED that no situation
-- named. Those consult no model -- a declared cancellation is a fact about the timetable, not a
-- forecast -- and are the reason varsel stops being blind to a train it can already see is gone.
-- See the `et_cancelled` / `covered` / `et_only` CTEs and the UNION ALL that closes the file.
-- Since 2026-09-23 the same branch relays the Vy trains taken OUT of today's plan
-- (rail_calls_removed_today, KIX:PlanRemoved:<jkey>), which ET never carries at all. That table
-- reaches this file there and nowhere else: the feature CTEs read rail_calls_planned_today only.
--
-- Cheap: reads the small materialized rail_calls_planned_today + today's situations + ML.PREDICT
-- (no ET scan here -- that cost is in rail_calls_planned_today.sql, rebuilt on a TTL by main.py).
-- Keep the feature block + scoring tail in sync with data/queries/predict_calls_today.sql.
-- The ET-cancellation relay below is deliberately NOT part of that sync: it is a serving
-- concern (it exists so varsel and /impact see a train ET already knows is gone), and its
-- rows carry a flat 1.0 rather than a model output. In an evaluation query they would be
-- free correct answers nobody predicted -- the same reason /historikk segments them out of
-- model skill.
CREATE OR REPLACE TABLE `disruptions.call_predictions_today` AS
WITH
cat AS (
  SELECT summary, description, ANY_VALUE(category) AS category
  FROM `disruptions.categories`
  GROUP BY summary, description
),
sit AS (
  SELECT
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime) AS onset_ts,
    d.summary,
    -- Served text is the situation's LATEST version (disruptions_today.sql), the first version
    -- where the latest carries none. Features keep reading the first-version `d.summary` /
    -- `d.description` below; only these served columns move.
    COALESCE(d.latest_summary, d.summary) AS served_summary,
    -- Carried for serving only (never a feature): /impact returns the operator's own explanation
    -- alongside the summary. Joined back at the final SELECT rather than threaded through `base`,
    -- so this free text never widens the ML.PREDICT inputs.
    COALESCE(d.latest_description, d.description) AS description,
    -- The operator's English, served only and joined back through `sd` like the description.
    COALESCE(d.latest_summary_en, d.summary_en) AS summary_en,
    COALESCE(d.latest_description_en, d.description_en) AS description_en,
    -- SIRI-SX `reportType` as the operator published it NOW (INCIDENT / GENERAL), served only.
    COALESCE(d.latest_report_type, d.reportType) AS report_type,
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
  FROM `disruptions.disruptions_initial_today` AS d
  LEFT JOIN cat AS c
    ON IFNULL(d.summary,'')     = IFNULL(c.summary,'')
   AND IFNULL(d.description,'') = IFNULL(c.description,'')
  WHERE SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
    -- Vy has declared it over (see `all_clear` in disruptions_today.sql), so it alerts nobody. Its
    -- calls fall through to the no-SX and ET-relay branches like any call no situation names.
    AND NOT IFNULL(d.all_clear, FALSE)
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
    -- OBSERVED state, not predicted: the realized departure delay on a call ET has already
    -- operated, and whether this call came from recordedCalls ('recorded' -> observed) or
    -- estimatedCalls ('estimated' -> still in the future). /impact reads these as
    -- `latest_observation`; the propagation features already read the same two columns.
    r.dep_delay_min AS et_dep_delay_min,
    r.call_source,
    -- Whether a passenger may board / alight HERE (rail_calls_planned_today.sql owns the reading).
    -- Carried, like every serving-only column here, into the ML.PREDICT input: a model ignores an
    -- input column it does not know, and only the reverse -- a contract naming a column the input
    -- lacks -- fails the scoring job. `impact._trip_cells` is the reader: without these two, a
    -- long-distance train whose local calls are boarding-only was offered as somebody's trip.
    r.boarding_allowed, r.alighting_allowed,
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
-- ---- CALLS NO SITUATION NAMED ----------------------------------------------------------------
-- The other 3/4 of the timetable. Everything above needs an operator to have published something;
-- these rows are what a traveller's train looks like when nobody has. The models can read them
-- because training_calls carries the same population under the same flag since 2026-08-19 -- the
-- fillers below are that file's, value for value, and they have to be: a serving row that encodes
-- "no situation" differently from a training row is the train/serve skew this whole change removed.
--
-- WHAT PARTITIONS THE THREE BRANCHES OF THIS FILE, so no call is scored twice or missed:
--   base     a situation named it
--   nosx     no situation named it and ET has NOT declared it cancelled  -> scored, here
--   et_only  no situation named it and ET HAS declared it cancelled      -> relayed, at the bottom
-- The cancelled ones are deliberately not scored: `impact._cancelled_prediction` would overwrite
-- every figure with the certainty anyway, so a model score for them is work whose only possible
-- effect is to be discarded.
--
-- THE WINDOW IS THE CALL'S OWN CLOCK, not a situation's. `base` measures from an onset that may be
-- hours old; here there is nothing to measure from, so the same 12 h reach forward and 1 h back is
-- taken from now -- matching et_only, which faces exactly the same absence.
nosx_base AS (
  SELECT
    CONCAT('KIX:NoSituation:', r.jkey) AS situationNumber,
    CAST(NULL AS TIMESTAMP) AS onset_ts,
    -- The summary a traveller reads on the card when no wording has been written for this
    -- alert. Deliberately just the headline: the absence of an operator message is what the
    -- `description` below is for and what `has_situation` carries, and spelling it out here
    -- put a caveat longer than the warning itself in front of somebody with a train to catch.
    'Mulig avvik' AS summary,
    r.jkey, r.lineRef, r.stop_place, r.stop_name, r.aimed_dep, r.aimed_arr,
    r.call_cancelled AS et_declared_cancelled,
    r.dep_delay_min AS et_dep_delay_min,
    r.call_source,
    r.boarding_allowed, r.alighting_allowed,
    COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts,
    'none' AS status,
    FALSE  AS planned,
    -1 AS num_journeys, -1 AS num_stops, -1 AS num_lines,
    -1 AS onset_hour, -1 AS onset_dow, -1 AS onset_month,
    r.stop_order,
    (r.aimed_dep IS NULL) AS is_terminal,
    (r.aimed_arr IS NULL) AS is_origin,
    -1 AS min_from_onset,
    EXTRACT(HOUR      FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_hour,
    EXTRACT(DAYOFWEEK FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_dow,
    'none' AS category, -1 AS stated_min
  FROM `disruptions.rail_calls_planned_today` AS r
  LEFT JOIN (SELECT DISTINCT jkey, stop_order FROM base) AS c
    ON c.jkey = r.jkey AND c.stop_order = r.stop_order
  WHERE c.jkey IS NULL
    AND NOT COALESCE(r.call_cancelled, FALSE)
    AND (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), CURRENT_TIMESTAMP(), MINUTE)
        BETWEEN -60 AND 720
),
-- One population column, two branches, and the column order is the contract (UNION ALL is
-- positional). has_situation is set HERE rather than in `calls` so each branch states its own.
allbase AS (
  SELECT *, TRUE  AS has_situation FROM base
  UNION ALL
  SELECT *, FALSE AS has_situation FROM nosx_base
),
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
-- The moment `observed` was read: when rail_calls_planned_today was built, not now. That table is rebuilt on a
-- TTL (main._PLANNED_TTL, 60 min), so measuring the last hour and the ask horizon from CURRENT_TIMESTAMP() read every
-- stop passed since the build as unobserved. own_prev_delay = -1 at a mid-route stop is what a
-- train whose earlier calls were cancelled looks like in training, so the model scored running
-- trains as likely cancellations (2026-10-13: live p_cancel 0.75 where the same calls scored 0.48
-- through the training pipeline and 40 % were cancelled). Anchored here, line_now_* and
-- lead_bucket describe what was known at the build, which is what leadtime_eval.sql fit the
-- per-horizon Platt layers on. COALESCE covers an empty table.
asof AS (
  SELECT COALESCE(MAX(built_at), CURRENT_TIMESTAMP()) AS t FROM `disruptions.rail_calls_planned_today`
),
linenow AS (
  SELECT lineRef,
         COUNT(*) AS line_now_n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS line_now_delay,
         ROUND(AVG(CAST(call_cancelled AS INT64)), 3) AS line_now_cancel
  FROM observed
  WHERE aimed_dep >= TIMESTAMP_SUB((SELECT t FROM asof), INTERVAL 60 MINUTE)
    AND aimed_dep <  (SELECT t FROM asof)
  GROUP BY 1
),
-- Keyed per CALL, not per (situation, call): the join reads only the train's own earlier stops,
-- so the situation never changed the answer -- and a no-SX row has no situationNumber to join on.
-- Same re-keying, for the same reason, as create_training_calls.sql. Dropping it would not error;
-- it would just hand every no-SX row own_prev_delay = -1, silently discarding the single feature
-- that carries most of the signal when there is no message (attribution 0.46).
own AS (
  SELECT t.jkey, t.stop_order,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)]
           AS own_prev_delay
  FROM (SELECT DISTINCT jkey, stop_order, call_ts FROM allbase) AS t
  JOIN observed AS r2
    ON r2.jkey = t.jkey
   AND r2.stop_order < t.stop_order
   AND r2.aimed_dep <= TIMESTAMP_SUB(t.call_ts, INTERVAL 30 MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT r2.call_cancelled
  GROUP BY 1, 2
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
    -- has_situation is NOT listed here: it already arrives through `b.*` from allbase, and naming
    -- it again makes ML.PREDICT reject the whole input ("Duplicate columns are not allowed in the
    -- input data to PREDICT"). It is a real feature of every head, not a passthrough -- the models
    -- split on it first (attribution #1, one split per iteration), which is what lets one model
    -- serve both populations.
    -- ask-horizon bucket: which platt layer calibrates this call's scores (see header)
    CASE
      WHEN TIMESTAMP_DIFF(b.call_ts, (SELECT t FROM asof), MINUTE) < 45  THEN '30'
      WHEN TIMESTAMP_DIFF(b.call_ts, (SELECT t FROM asof), MINUTE) < 90  THEN '60'
      WHEN TIMESTAMP_DIFF(b.call_ts, (SELECT t FROM asof), MINUTE) < 180 THEN '120'
      WHEN TIMESTAMP_DIFF(b.call_ts, (SELECT t FROM asof), MINUTE) < 360 THEN '240'
      ELSE 'plan'
    END AS lead_bucket
  FROM allbase AS b
  LEFT JOIN linepre AS lp ON lp.situationNumber = b.situationNumber AND lp.lineRef = b.lineRef
  LEFT JOIN netpre  AS np ON np.situationNumber = b.situationNumber
  LEFT JOIN linenow AS ln ON ln.lineRef = b.lineRef
  LEFT JOIN own AS o
    ON o.jkey = b.jkey AND o.stop_order = b.stop_order
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
-- POPULATION-AWARE SINCE 2026-08-19. The inline blocks below are the FLAGGED population's layers,
-- read live from ML.WEIGHTS exactly as before; the no-SX rows take theirs from the materialized
-- `nosx_platt_weights` (gen_nosx_platt_weights.py). One Platt layer has one intercept, and these
-- two populations sit ~12x apart on the base rate the intercept sets -- serving both through one
-- layer is the shared-layer mistake the calibration files exist to prevent, and it would land here
-- as under-firing alerts rather than as an error. The join below carries has_situation for that
-- reason; drop it and the query still runs and still returns a number for every row.
wch AS (
  SELECT lead_bucket, b, a, TRUE AS has_situation FROM (
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
  )
  UNION ALL
  SELECT lead_bucket, b, a, FALSE
  FROM `disruptions.nosx_platt_weights` WHERE head = 'cancel'
),
wdh AS (
  SELECT lead_bucket, b, a, TRUE AS has_situation FROM (
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
  )
  UNION ALL
  SELECT lead_bucket, b, a, FALSE
  FROM `disruptions.nosx_platt_weights` WHERE head = 'dep_ge10'
),
w5h AS (
  SELECT lead_bucket, b, a, TRUE AS has_situation FROM (
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
  )
  UNION ALL
  SELECT lead_bucket, b, a, FALSE
  FROM `disruptions.nosx_platt_weights` WHERE head = 'arr_ge5'
),
w10h AS (
  SELECT lead_bucket, b, a, TRUE AS has_situation FROM (
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
  )
  UNION ALL
  SELECT lead_bucket, b, a, FALSE
  FROM `disruptions.nosx_platt_weights` WHERE head = 'arr_ge10'
),
w20h AS (
  SELECT lead_bucket, b, a, TRUE AS has_situation FROM (
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
  )
  UNION ALL
  SELECT lead_bucket, b, a, FALSE
  FROM `disruptions.nosx_platt_weights` WHERE head = 'arr_ge20'
),
w40h AS (
  SELECT lead_bucket, b, a, TRUE AS has_situation FROM (
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
  )
  UNION ALL
  SELECT lead_bucket, b, a, FALSE
  FROM `disruptions.nosx_platt_weights` WHERE head = 'arr_ge40'
),
wd5h AS (
  SELECT lead_bucket, b, a, TRUE AS has_situation FROM (
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
  )
  UNION ALL
  SELECT lead_bucket, b, a, FALSE
  FROM `disruptions.nosx_platt_weights` WHERE head = 'dep_ge5'
),
wd20h AS (
  SELECT lead_bucket, b, a, TRUE AS has_situation FROM (
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
  )
  UNION ALL
  SELECT lead_bucket, b, a, FALSE
  FROM `disruptions.nosx_platt_weights` WHERE head = 'dep_ge20'
),
wd40h AS (
  SELECT lead_bucket, b, a, TRUE AS has_situation FROM (
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
  )
  UNION ALL
  SELECT lead_bucket, b, a, FALSE
  FROM `disruptions.nosx_platt_weights` WHERE head = 'dep_ge40'
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
  JOIN w5h   ON w5h.lead_bucket   = c.lead_bucket AND w5h.has_situation = c.has_situation
  JOIN w10h  ON w10h.lead_bucket  = c.lead_bucket AND w10h.has_situation = c.has_situation
  JOIN w20h  ON w20h.lead_bucket  = c.lead_bucket AND w20h.has_situation = c.has_situation
  JOIN w40h  ON w40h.lead_bucket  = c.lead_bucket AND w40h.has_situation = c.has_situation
  JOIN wd5h  ON wd5h.lead_bucket  = c.lead_bucket AND wd5h.has_situation = c.has_situation
  JOIN wdh   ON wdh.lead_bucket   = c.lead_bucket AND wdh.has_situation = c.has_situation
  JOIN wd20h ON wd20h.lead_bucket = c.lead_bucket AND wd20h.has_situation = c.has_situation
  JOIN wd40h ON wd40h.lead_bucket = c.lead_bucket AND wd40h.has_situation = c.has_situation
),
-- ---- ET-DECLARED CANCELLATIONS THAT NO SITUATION NAMED ----------------------------------------
-- Everything above scores calls an SX situation pointed at. A cancellation ET has ALREADY DECLARED
-- needs neither a situation nor a model: `impact._cancelled_prediction` replaces every score with
-- the certainty (p_cancel 1.0, alerting score 1.0, every delay figure dropped -- each one meant "if
-- it runs") the moment `et_declared_cancelled` is set, and `_call_severity`/`_alert_score` restate
-- it. So these rows carry that certainty directly and consult no model at all: the row RELAYS a
-- fact instead of predicting one, which is also how `decisions.et_cancelled` segments it on
-- /historikk. Without them varsel is blind to a train it can already see is gone -- measured in dev
-- on 2026-08-19: 90 declared-cancelled rail calls, 70 named by a situation and 20 not.
--
-- ANTI-JOINED PER CALL, never per journey. A situation's reach is 12 h from ITS onset, so one
-- journey can have some calls named and others not; and a second row for a call the models already
-- scored would put two boxes about one train on the card. `varsel.one_alert_per_train` would
-- collapse those, but leaning on it here would be wrong -- it deliberately does not reach the
-- decision log, where every situation keeps its own row.
--
-- ONE SYNTHETIC SITUATION PER JOURNEY, because `situationNumber` is the identity every consumer
-- keys on: /impact renders one card per situation, `impact._alternatives_by_situation` spends one
-- replacement lookup per situation, varsel dedups pushes on (journey, situationNumber) and
-- `varsel_decisions` CLUSTERs on it. Per journey is the one grain that makes all four right. The
-- `KIX:` prefix keeps it clear of the publisher namespaces (`ENT:`, `NSB:`) so it can never collide
-- with a real SX ref; the cost is that GET /situation/{id} has nothing to render for one, which is
-- the honest answer -- there is no SX message behind it.
--
-- AND THE TRAINS VY TOOK OUT OF TODAY'S PLAN (rail_calls_removed_today, plan 2026-09-23-001):
-- standing trains absent from today's GTFS and from ET, every call `call_cancelled` and
-- `call_source = 'removed'`. The same certainty, under their own prefix and wording in the SELECT
-- at the bottom. UNIONed HERE and nowhere above: the line and network state features average
-- `call_cancelled` over the planned table, and a removed train there would move every real
-- train's forecast on exactly the cut days, from inputs training never saw. None of them is in the
-- ET timetable, so the three-branch partition below still holds.
et_cancelled AS (
  SELECT
    r.jkey, r.lineRef, r.stop_place, r.stop_name, r.stop_order,
    r.aimed_dep, r.aimed_arr, r.dep_delay_min, r.call_source,
    r.boarding_allowed, r.alighting_allowed, r.bus_for_train,
    -- The cancelled stretch, for the summary. Computed over EVERY cancelled call of the journey
    -- rather than over what the anti-join below happens to leave, so the headline describes the
    -- disruption and not the leftovers. IGNORE NULLS because stop_name arrives through a LEFT JOIN
    -- in the planned source and one missing name should not blank the headline.
    FIRST_VALUE(r.stop_name IGNORE NULLS) OVER seg AS seg_from,
    LAST_VALUE(r.stop_name IGNORE NULLS)  OVER seg AS seg_to
  FROM (
    SELECT jkey, lineRef, stop_place, stop_name, stop_order, aimed_dep, aimed_arr, dep_delay_min,
           call_source, boarding_allowed, alighting_allowed, FALSE AS bus_for_train
    FROM `disruptions.rail_calls_planned_today`
    WHERE call_cancelled
    UNION ALL
    SELECT jkey, lineRef, stop_place, stop_name, stop_order, aimed_dep, aimed_arr, dep_delay_min,
           call_source, boarding_allowed, alighting_allowed, bus_for_train
    FROM `disruptions.rail_calls_removed_today`
  ) AS r
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
  WINDOW seg AS (
    PARTITION BY r.jkey ORDER BY r.stop_order
    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
  )
),
covered AS (
  SELECT DISTINCT jkey, stop_order FROM base
),
et_only AS (
  SELECT e.*
  FROM et_cancelled AS e
  LEFT JOIN covered AS c ON c.jkey = e.jkey AND c.stop_order = e.stop_order
  WHERE c.jkey IS NULL
    -- The same 12 h reach forward that a situation's window has, and one hour back so a call whose
    -- time has only just passed still answers /impact for somebody on the platform and can still be
    -- the trip's `latest_observation` (impact._is_observed admits a declared cancellation only once
    -- its own time has passed). The SX path already carries past calls -- its window opens at an
    -- onset that is itself in the past -- so this is a narrower lookback than what is served today,
    -- not a new kind of row.
    AND TIMESTAMP_DIFF(COALESCE(e.aimed_dep, e.aimed_arr), CURRENT_TIMESTAMP(), MINUTE)
        BETWEEN -60 AND 720
)
SELECT
  s.situationNumber,
  -- `s.summary` is the first version (it rode through the feature CTEs); serve the latest.
  IF(s.has_situation, sd.served_summary, s.summary) AS summary,
  -- A no-SX row has no operator text to serve, and NULL here would read downstream as "the
  -- operator published a message with no description". Say what the row actually is instead.
  IF(s.has_situation, sd.description,
     'Ingen situasjonsmelding er publisert for denne avgangen. Vurderingen bygger på '
     || 'sanntidsdata: togets egen forsinkelse oppstrøms og tilstanden på linjen.') AS description,
  s.onset_ts,
  s.jkey, s.lineRef, s.stop_place, s.stop_name, s.stop_order,
  s.aimed_dep, s.aimed_arr, s.is_terminal, s.is_origin, s.min_from_onset,
  s.et_declared_cancelled, s.et_dep_delay_min, s.call_source,
  s.boarding_allowed, s.alighting_allowed,
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
  s.lead_bucket AS platt_lead_bucket,
  -- Which population this row is from, served rather than re-derived downstream. The alert gate,
  -- the wording and /historikk's segmentation all need it, and inferring it from a
  -- `situationNumber LIKE 'KIX:%'` prefix would make a naming convention load-bearing for
  -- alerting policy.
  s.has_situation,
  -- The operator's English where one was published. A no-SX row's Norwegian is this file's own
  -- text (`nosx`'s 'Mulig avvik' and the description above), so its English is written here too.
  IF(s.has_situation, sd.summary_en, 'Possible disruption') AS summary_en,
  IF(s.has_situation, sd.description_en,
     'No situation message has been published for this departure. The assessment is based on '
     || "real-time data: the train's own delay upstream and the state of the line.") AS description_en,
  -- NULL on a no-SX row: no `sit` row carries its synthetic number, so `sd` is unmatched.
  sd.report_type
FROM scored AS s
-- Situation description for serving. GROUP BY guarantees one row per situationNumber, so this
-- join can never fan out the scored calls even if the source ever carries a duplicate.
LEFT JOIN (
  SELECT situationNumber, ANY_VALUE(served_summary) AS served_summary,
         ANY_VALUE(description) AS description,
         ANY_VALUE(summary_en) AS summary_en, ANY_VALUE(description_en) AS description_en,
         ANY_VALUE(report_type) AS report_type
  FROM sit GROUP BY situationNumber
) AS sd ON sd.situationNumber = s.situationNumber
JOIN wch  ON wch.lead_bucket  = s.lead_bucket AND wch.has_situation = s.has_situation
JOIN wdh  ON wdh.lead_bucket  = s.lead_bucket AND wdh.has_situation = s.has_situation
JOIN w10h ON w10h.lead_bucket = s.lead_bucket AND w10h.has_situation = s.has_situation
LEFT JOIN bounds AS bd ON bd.side = 'dep'
LEFT JOIN `disruptions.delay_p90_calib` AS dpq
  ON dpq.side = 'dep' AND dpq.plog_bucket = LEAST(GREATEST(ROUND(s.dep_plog,1), bd.lo), bd.hi)
LEFT JOIN bounds AS ba ON ba.side = 'arr'
LEFT JOIN `disruptions.delay_p90_calib` AS apq
  ON apq.side = 'arr' AND apq.plog_bucket = LEAST(GREATEST(ROUND(s.arr_plog,1), ba.lo), ba.hi)

UNION ALL
-- The ET-declared cancellations no situation named (see `et_only` above). Certainty in place of
-- every model score, and NULL for every figure whose meaning was "if it runs" -- the exact shape
-- `impact._cancelled_prediction` would impose on a scored row anyway, written here so nothing has
-- to be un-predicted downstream. Column order is the contract: this list must stay in step with
-- the SELECT above it, which is what a UNION ALL checks by name-independent position.
-- A removed train is not «Innstilt»: Vy has not cancelled it, and a traveller who checks entur.no
-- sees no cancellation, only a timetable without it. Its prefix is what varsel keeps out of the
-- per-train pushes and wordings, and what /historikk segments on: its `removed` segment is tested
-- ahead of `relayed`, since these rows carry et_declared_cancelled (plan 2026-09-23-001, U5).
SELECT
  IF(e.call_source = 'removed',
     CONCAT('KIX:PlanRemoved:', e.jkey),
     CONCAT('KIX:ETCancellation:', e.jkey)) AS situationNumber,
  CASE
    WHEN e.call_source = 'removed'              THEN 'Tatt ut av dagens rutetabell'
    WHEN e.seg_from IS NULL OR e.seg_to IS NULL THEN 'Innstilt avgang'
    WHEN e.seg_from = e.seg_to                  THEN CONCAT('Innstilt fra ', e.seg_from)
    ELSE CONCAT('Innstilt ', e.seg_from, '–', e.seg_to)
  END AS summary,
  CASE
    WHEN e.call_source = 'removed' THEN
      'Toget går vanligvis på denne ukedagen, men finnes ikke i dagens rutetabell.'
      || IF(e.bus_for_train, ' Vy har satt opp buss for tog.', '')
    ELSE 'Avgangen er markert innstilt i operatørens sanntidsdata.'
  END AS description,
  -- No SX message means no creationTime, so there is no onset to state and nothing to measure from.
  -- NULL rather than CURRENT_TIMESTAMP(), which would be the build time -- not when anybody declared
  -- anything, and different on every rebuild. Nothing reads either column off this table (checked
  -- 2026-08-19); they are here for parity with the situation-derived rows above.
  CAST(NULL AS TIMESTAMP) AS onset_ts,
  e.jkey, e.lineRef, e.stop_place, e.stop_name, e.stop_order,
  e.aimed_dep, e.aimed_arr,
  (e.aimed_dep IS NULL) AS is_terminal,
  (e.aimed_arr IS NULL) AS is_origin,
  CAST(NULL AS INT64)   AS min_from_onset,
  TRUE                  AS et_declared_cancelled,
  e.dep_delay_min       AS et_dep_delay_min,
  e.call_source,
  e.boarding_allowed, e.alighting_allowed,
  1.0                   AS p_cancel,
  CAST(NULL AS INT64)   AS dep_delay_typ_min,
  CAST(NULL AS INT64)   AS dep_delay_p90_min,
  CAST(NULL AS INT64)   AS arr_delay_typ_min,
  CAST(NULL AS INT64)   AS arr_delay_p90_min,
  CAST(NULL AS FLOAT64) AS p_arr_ge5,
  CAST(NULL AS FLOAT64) AS p_arr_ge10,
  CAST(NULL AS FLOAT64) AS p_arr_ge20,
  CAST(NULL AS FLOAT64) AS p_arr_ge40,
  CAST(NULL AS INT64)   AS arr_delay_p50_stack_min,
  CAST(NULL AS INT64)   AS arr_delay_p90_stack_min,
  -- The alerting scores, and the only two figures that are not NULL: 1.0, on the side that exists.
  -- An origin has no arrival and a terminal has no departure, so the per-side NULLing is the same
  -- rule the scored branch applies -- `impact._alert_score` reads both sides and takes the max.
  IF(e.aimed_arr IS NULL, NULL, 1.0) AS p_impact,
  CAST(NULL AS FLOAT64) AS p_dep_ge5,
  CAST(NULL AS FLOAT64) AS p_dep_ge10,
  CAST(NULL AS FLOAT64) AS p_dep_ge20,
  CAST(NULL AS FLOAT64) AS p_dep_ge40,
  CAST(NULL AS INT64)   AS dep_delay_p50_stack_min,
  CAST(NULL AS INT64)   AS dep_delay_p90_stack_min,
  IF(e.aimed_dep IS NULL, NULL, 1.0) AS p_impact_dep,
  CAST(NULL AS FLOAT64) AS p_arr_ge10_h,
  CAST(NULL AS FLOAT64) AS p_dep_ge10_h,
  -- No model was scored, so no platt layer calibrated anything. Naming a bucket here would claim a
  -- calibration that never ran.
  CAST(NULL AS STRING)  AS platt_lead_bucket,
  -- A relayed cancellation is by definition a call no situation named, so it belongs to the same
  -- population as the scored no-SX rows even though it consulted no model. /historikk still tells
  -- the two apart on the situationNumber prefix, which is what segments "relayed a fact" from
  -- "predicted an outcome"; this column answers the different question of whether an SX message
  -- existed at all.
  FALSE                 AS has_situation,
  -- English beside the Norwegian, written here for the same reason the Norwegian is: no operator
  -- published either. Stop names stay as ET gives them.
  CASE
    WHEN e.call_source = 'removed'              THEN "Removed from today's timetable"
    WHEN e.seg_from IS NULL OR e.seg_to IS NULL THEN 'Cancelled departure'
    WHEN e.seg_from = e.seg_to                  THEN CONCAT('Cancelled from ', e.seg_from)
    ELSE CONCAT('Cancelled ', e.seg_from, '–', e.seg_to)
  END AS summary_en,
  CASE
    WHEN e.call_source = 'removed' THEN
      "The train usually runs on this weekday, but is not in today's timetable."
      || IF(e.bus_for_train, ' Vy has set up rail replacement buses.', '')
    ELSE "The departure is marked as cancelled in the operator's real-time data."
  END AS description_en,
  -- No operator message, so no report type.
  CAST(NULL AS STRING)  AS report_type
FROM et_only AS e;
