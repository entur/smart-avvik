-- Calibration layer + action-threshold selection for the trip-outcome model.
--
-- The boosted-tree P(CANCELLED) is monotonic but over-confident at the top
-- (0.9 predicted -> ~0.77 actual). We fit a Platt scaler (1-feature logistic
-- regression on the logit) to recalibrate it, then sweep thresholds so the
-- product can pick a cutoff by the precision it needs.
--
-- HONEST SPLIT: the model trained on is_eval = FALSE. We split the untouched
-- eval rows by situationNumber into calib (fit the scaler) and test (report),
-- so neither the model nor the scaler has seen the test rows.

-- 1. Score the eval rows (raw class probabilities), tag calib/test ------------
CREATE OR REPLACE TABLE `disruptions.trip_scored` AS
SELECT
  outcome AS actual,
  (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'CANCELLED') AS p_cancelled,
  (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'DELAYED')   AS p_delayed,
  MOD(ABS(FARM_FINGERPRINT(situationNumber)), 2) AS half   -- 0 = calib, 1 = test
FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf`,
      (SELECT * FROM `disruptions.trip_outcomes` WHERE is_eval));

-- 2. Platt scaler for P(CANCELLED): logistic reg on the logit, calib half -----
CREATE OR REPLACE MODEL `disruptions.cal_cancelled`
OPTIONS(model_type = 'LOGISTIC_REG', input_label_cols = ['is_cancelled'],
        data_split_method = 'NO_SPLIT') AS
SELECT
  IF(actual = 'CANCELLED', 1, 0) AS is_cancelled,
  LOG(GREATEST(LEAST(p_cancelled, 0.999), 0.001)
      / (1 - GREATEST(LEAST(p_cancelled, 0.999), 0.001))) AS logit_p
FROM `disruptions.trip_scored`
WHERE half = 0;

-- 3. Apply the scaler to the test half ---------------------------------------
CREATE OR REPLACE TABLE `disruptions.trip_test_cal` AS
SELECT
  actual,
  p_cancelled                              AS p_cancelled_raw,
  p_delayed + p_cancelled                  AS p_disrupted,
  (SELECT prob FROM UNNEST(predicted_is_cancelled_probs) WHERE label = 1) AS p_cancelled_cal
FROM ML.PREDICT(MODEL `disruptions.cal_cancelled`,
      (SELECT actual, p_cancelled, p_delayed,
              LOG(GREATEST(LEAST(p_cancelled, 0.999), 0.001)
                  / (1 - GREATEST(LEAST(p_cancelled, 0.999), 0.001))) AS logit_p
       FROM `disruptions.trip_scored` WHERE half = 1));

-- 4. Reliability of the CALIBRATED probability on the test half ---------------
-- (compare actual_rate to the bucket: closer = better calibrated)
SELECT
  ROUND(p_cancelled_cal, 1) AS p_bucket,
  COUNT(*) AS n,
  ROUND(AVG(IF(actual = 'CANCELLED', 1, 0)), 3) AS actual_cancelled_rate
FROM `disruptions.trip_test_cal`
GROUP BY p_bucket ORDER BY p_bucket;

-- 5a. Threshold sweep for the "alternative / WFH" action on P(CANCELLED) ------
WITH thr AS (SELECT t FROM UNNEST(GENERATE_ARRAY(0.05, 0.95, 0.05)) AS t)
SELECT
  t AS threshold,
  ROUND(COUNTIF(p_cancelled_cal >= t) / COUNT(*), 3) AS coverage,
  ROUND(SAFE_DIVIDE(COUNTIF(p_cancelled_cal >= t AND actual = 'CANCELLED'),
                    COUNTIF(p_cancelled_cal >= t)), 3) AS precision,
  ROUND(SAFE_DIVIDE(COUNTIF(p_cancelled_cal >= t AND actual = 'CANCELLED'),
                    COUNTIF(actual = 'CANCELLED')), 3) AS recall
FROM `disruptions.trip_test_cal` CROSS JOIN thr
GROUP BY t ORDER BY t;

-- 5b. Threshold sweep for the "expect disruption / wait" action --------------
-- (P(not GO); raw -- calibrate the same way if you surface this number.)
WITH thr AS (SELECT t FROM UNNEST(GENERATE_ARRAY(0.05, 0.95, 0.05)) AS t)
SELECT
  t AS threshold,
  ROUND(COUNTIF(p_disrupted >= t) / COUNT(*), 3) AS coverage,
  ROUND(SAFE_DIVIDE(COUNTIF(p_disrupted >= t AND actual != 'GO'),
                    COUNTIF(p_disrupted >= t)), 3) AS precision,
  ROUND(SAFE_DIVIDE(COUNTIF(p_disrupted >= t AND actual != 'GO'),
                    COUNTIF(actual != 'GO')), 3) AS recall
FROM `disruptions.trip_test_cal` CROSS JOIN thr
GROUP BY t ORDER BY t;
