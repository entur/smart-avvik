-- Calibration + two-tier policy for the v2 (unconditional) trip-outcome model.
--
-- v2 probabilities are auto_class_weight-inflated, so they must be recalibrated
-- before being surfaced or thresholded into a policy. Two Platt scalers:
--   cal_cancelled_v2  -> P(CANCELLED)  -> Tier 1 "take an alternative / WFH"
--   cal_disrupted_v2  -> P(not GO)     -> Tier 2 "expect delay / consider waiting"
--
-- HONEST SPLIT: model trained on is_eval = FALSE. Split the untouched eval rows
-- by situationNumber into calib (fit scalers) and test (report).

-- 1. Score v2 eval rows, tag calib/test, add id ------------------------------
CREATE OR REPLACE TABLE `disruptions.trip_scored_v2` AS
SELECT
  GENERATE_UUID() AS id,
  outcome AS actual,
  (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'CANCELLED') AS p_cancelled,
  (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'DELAYED')   AS p_delayed,
  MOD(ABS(FARM_FINGERPRINT(situationNumber)), 2) AS half
FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf_v2`,
      (SELECT * FROM `disruptions.trip_outcomes_v2` WHERE is_eval));

-- 2a/2b. Platt scalers on the calib half -------------------------------------
CREATE OR REPLACE MODEL `disruptions.cal_cancelled_v2`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['is_cancelled'], data_split_method='NO_SPLIT') AS
SELECT IF(actual='CANCELLED',1,0) AS is_cancelled,
  LOG(GREATEST(LEAST(p_cancelled,0.999),0.001)/(1-GREATEST(LEAST(p_cancelled,0.999),0.001))) AS logit_p
FROM `disruptions.trip_scored_v2` WHERE half=0;

CREATE OR REPLACE MODEL `disruptions.cal_disrupted_v2`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['is_disrupted'], data_split_method='NO_SPLIT') AS
SELECT IF(actual!='GO',1,0) AS is_disrupted,
  LOG(GREATEST(LEAST(p_delayed+p_cancelled,0.999),0.001)/(1-GREATEST(LEAST(p_delayed+p_cancelled,0.999),0.001))) AS logit_p
FROM `disruptions.trip_scored_v2` WHERE half=0;

-- 3. Apply both scalers to the test half -------------------------------------
CREATE OR REPLACE TABLE `disruptions.trip_test_cal_v2` AS
WITH base AS (
  SELECT id, actual,
    LOG(GREATEST(LEAST(p_cancelled,0.999),0.001)/(1-GREATEST(LEAST(p_cancelled,0.999),0.001))) AS logit_can,
    LOG(GREATEST(LEAST(p_delayed+p_cancelled,0.999),0.001)/(1-GREATEST(LEAST(p_delayed+p_cancelled,0.999),0.001))) AS logit_dis
  FROM `disruptions.trip_scored_v2` WHERE half=1
),
can AS (SELECT id, (SELECT prob FROM UNNEST(predicted_is_cancelled_probs) WHERE label=1) AS p_cancelled_cal
        FROM ML.PREDICT(MODEL `disruptions.cal_cancelled_v2`, (SELECT id, logit_can AS logit_p FROM base))),
dis AS (SELECT id, (SELECT prob FROM UNNEST(predicted_is_disrupted_probs) WHERE label=1) AS p_disrupted_cal
        FROM ML.PREDICT(MODEL `disruptions.cal_disrupted_v2`, (SELECT id, logit_dis AS logit_p FROM base)))
SELECT base.actual, can.p_cancelled_cal, dis.p_disrupted_cal
FROM base JOIN can USING (id) JOIN dis USING (id);

-- 4. Reliability of both calibrated probabilities ----------------------------
SELECT 'cancelled' AS prob, ROUND(p_cancelled_cal,1) AS p_bucket, COUNT(*) AS n,
       ROUND(AVG(IF(actual='CANCELLED',1,0)),3) AS actual_rate
FROM `disruptions.trip_test_cal_v2` GROUP BY p_bucket
UNION ALL
SELECT 'disrupted', ROUND(p_disrupted_cal,1), COUNT(*), ROUND(AVG(IF(actual!='GO',1,0)),3)
FROM `disruptions.trip_test_cal_v2` GROUP BY ROUND(p_disrupted_cal,1)
ORDER BY prob, p_bucket;

-- 5a/5b. Threshold sweeps on the calibrated probabilities --------------------
WITH thr AS (SELECT t FROM UNNEST(GENERATE_ARRAY(0.05,0.95,0.05)) AS t)
SELECT 'cancelled' AS tier, t AS threshold,
  ROUND(COUNTIF(p_cancelled_cal>=t)/COUNT(*),4) AS coverage,
  ROUND(SAFE_DIVIDE(COUNTIF(p_cancelled_cal>=t AND actual='CANCELLED'),COUNTIF(p_cancelled_cal>=t)),3) AS precision,
  ROUND(SAFE_DIVIDE(COUNTIF(p_cancelled_cal>=t AND actual='CANCELLED'),COUNTIF(actual='CANCELLED')),3) AS recall
FROM `disruptions.trip_test_cal_v2` CROSS JOIN thr GROUP BY t
UNION ALL
SELECT 'disrupted', t,
  ROUND(COUNTIF(p_disrupted_cal>=t)/COUNT(*),4),
  ROUND(SAFE_DIVIDE(COUNTIF(p_disrupted_cal>=t AND actual!='GO'),COUNTIF(p_disrupted_cal>=t)),3),
  ROUND(SAFE_DIVIDE(COUNTIF(p_disrupted_cal>=t AND actual!='GO'),COUNTIF(actual!='GO')),3)
FROM `disruptions.trip_test_cal_v2` CROSS JOIN thr GROUP BY t
ORDER BY tier, threshold;

-- 6. Two-tier policy cascade (chosen cutoffs) --------------------------------
-- Tier 1 P(cancelled_cal) >= 0.40  -> ~91% precision (take alternative / WFH)
-- Tier 2 P(notGO_cal)     >= 0.30  -> softer "expect delay" advisory
-- Cancellation is checked first so the severe message wins. Tune the cutoffs
-- to trade false-alarm rate vs. coverage.
WITH labeled AS (
  SELECT actual,
    CASE WHEN p_cancelled_cal >= 0.40 THEN '1_ALTERNATIVE_WFH'
         WHEN p_disrupted_cal >= 0.30 THEN '2_EXPECT_DELAY'
         ELSE '3_OK' END AS message
  FROM `disruptions.trip_test_cal_v2`
)
SELECT message,
  COUNT(*) AS n,
  ROUND(COUNT(*)/SUM(COUNT(*)) OVER (), 4) AS share,
  ROUND(COUNTIF(actual='CANCELLED')/COUNT(*), 3) AS cancelled_rate,
  ROUND(COUNTIF(actual='DELAYED')/COUNT(*), 3)   AS delayed_rate,
  ROUND(COUNTIF(actual='GO')/COUNT(*), 3)        AS go_rate
FROM labeled GROUP BY message ORDER BY message;
