-- Per-trip outcome predictions (GO / DELAYED / CANCELLED) from the INITIAL
-- (first-SX-message) conditional model `trip_outcome_clf_initial` for EVERY
-- post-training NSB disruption in disruptions_initial_new, set against what
-- actually happened from SIRI-ET.
--
-- This is the initial-model companion to predict_initial_new_trips.sql. The
-- candidate/feature pipeline is identical (src/feat CTEs mirror
-- create_trip_outcomes_initial.sql verbatim): the disruption's affected_journeys
-- joined to rail_calls_new by jkey -> one row per (situation x flagged journey x
-- boardable Quay), onset = SAFE.TIMESTAMP(creationTime), minutes_since_onset
-- clipped to the trained 0..720 range. boarding_stop is the ET Quay (stopPointRef).
--
-- CALIBRATION IS INLINED. Raw boosted-tree probabilities are auto_class_weights-
-- inflated, so they go through the two Platt scalers fitted in
-- calibrate_trip_model_initial.sql -- but instead of two extra ML.PREDICT calls
-- against the scaler models, we apply their extracted coefficients directly as
-- p_cal = sigmoid(b + a * logit(p_raw)). Coefficients (ML.WEIGHTS, validated to
-- 0.0 error vs ML.PREDICT on the calib test set):
--   cancelled: a =  1.4879012499527542  b = -2.2033347773362664   (on P(CANCELLED))
--   disrupted: a =  1.2223545795561150  b = -1.9467212377562177   (on P(DELAYED)+P(CANCELLED))
-- So the serving path needs ONLY the classifier; cal_cancelled_initial /
-- cal_disrupted_initial are not read here. If the model is retrained, refit the
-- scalers and refresh these constants from ML.WEIGHTS.
--
-- TWO-TIER POLICY (cutoffs from calibrate_trip_model_initial.sql step 6, the
-- INITIAL model's own sweep -- NOT v2's 0.40/0.30, which has ~0 coverage here):
--   Tier 1 p_cancelled_cal >= 0.30 -> "alternative / WFH" (~40% precision, frame as
--          ELEVATED CANCELLATION RISK -- the initial model can't assert a cancellation)
--   Tier 2 p_disrupted_cal >= 0.30 -> "expect delay" (~44% precision, ~43% recall)
--
-- ACTUALS: outcome realized from the same rail_calls_new row (cancelled / delay >=
-- 10 min / go -- the label definition). rail_calls_new must cover each incident's
-- onset + 12h (refresh with create_rail_calls_new.sql first). NOTE rail_calls_new
-- is VYG-only, so flagged journeys on other operators' lines are absent.

WITH
src AS (
  SELECT
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime)    AS onset_ts,
    IFNULL(c.category, 'UNKNOWN')     AS category,
    ARRAY_LENGTH(d.affected_stops)    AS num_affected_stops,
    ARRAY_LENGTH(d.affected_journeys) AS num_affected_journeys,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM `disruptions.disruptions_initial_new` AS d
  LEFT JOIN `disruptions.categories` AS c
    ON  IFNULL(d.summary, '__NULL__')     = IFNULL(c.summary, '__NULL__')
    AND IFNULL(d.description, '__NULL__') = IFNULL(c.description, '__NULL__'),
  UNNEST(d.affected_journeys) AS j
  WHERE d.participantRef = 'NSB'
    AND SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
),
feat AS (
  SELECT
    -- pass-through (not features; ML.PREDICT carries them to the output)
    s.situationNumber,
    r.jkey   AS journey,
    r.aimed_dep,
    -- features (exact trained shape)
    s.category,
    s.num_affected_stops,
    s.num_affected_journeys,
    r.quay   AS boarding_stop,
    r.lineRef,
    r.stop_order,
    EXTRACT(HOUR      FROM r.aimed_dep) AS dep_hour,
    EXTRACT(DAYOFWEEK FROM r.aimed_dep) AS dep_dow,
    TIMESTAMP_DIFF(r.aimed_dep, s.onset_ts, MINUTE) AS minutes_since_onset,
    -- realized outcome (label definition; NOT a feature)
    CASE
      WHEN r.call_cancelled       THEN 'CANCELLED'
      WHEN r.dep_delay_min >= 10  THEN 'DELAYED'
      ELSE 'GO'
    END AS actual_outcome
  FROM src AS s
  JOIN `disruptions.rail_calls_new` AS r USING (jkey)
  WHERE r.aimed_dep IS NOT NULL
    AND TIMESTAMP_DIFF(r.aimed_dep, s.onset_ts, MINUTE) BETWEEN 0 AND 720
),
scored AS (
  SELECT
    situationNumber, journey, boarding_stop, aimed_dep,
    category, lineRef, minutes_since_onset, actual_outcome,
    predicted_outcome,
    (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'CANCELLED') AS p_cancelled,
    (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'DELAYED')   AS p_delayed
  FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf_initial`, (SELECT * FROM feat))
),
-- inline Platt calibration: p_cal = sigmoid(b + a * logit(p_raw)) (no scaler models)
cal AS (
  SELECT
    *,
    1/(1+EXP(-(-2.2033347773362664 + 1.4879012499527542 *
        LOG(GREATEST(LEAST(p_cancelled, 0.999), 0.001)
            / (1 - GREATEST(LEAST(p_cancelled, 0.999), 0.001)))))) AS p_cancelled_cal,
    1/(1+EXP(-(-1.9467212377562177 + 1.222354579556115 *
        LOG(GREATEST(LEAST(p_delayed + p_cancelled, 0.999), 0.001)
            / (1 - GREATEST(LEAST(p_delayed + p_cancelled, 0.999), 0.001)))))) AS p_disrupted_cal
  FROM scored
)
SELECT
  situationNumber,
  journey,
  boarding_stop,
  aimed_dep,
  category,
  lineRef,
  minutes_since_onset,
  predicted_outcome,
  ROUND(p_cancelled_cal, 3) AS p_cancelled_cal,
  ROUND(p_disrupted_cal, 3) AS p_disrupted_cal,
  -- two-tier policy cascade: severe message wins (cutoffs: see header)
  CASE
    WHEN p_cancelled_cal >= 0.30 THEN '1_ALTERNATIVE_WFH'
    WHEN p_disrupted_cal >= 0.30 THEN '2_EXPECT_DELAY'
    ELSE '3_OK'
  END AS message,
  actual_outcome
FROM cal
ORDER BY p_cancelled_cal DESC, situationNumber, aimed_dep;
