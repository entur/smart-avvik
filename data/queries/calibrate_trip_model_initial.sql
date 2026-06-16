-- Calibration + two-tier policy for the INITIAL (first-SX-message) trip-outcome
-- model `disruptions.trip_outcome_clf_initial`.
--
-- The boosted-tree probabilities are monotonic but auto_class_weight-inflated
-- (P(CANCELLED) ~10x too high), so they must be recalibrated before being
-- surfaced or thresholded. Two Platt scalers (1-feature logistic reg on the logit):
--   cal_cancelled_initial -> P(CANCELLED) -> Tier 1 "take an alternative / WFH"
--   cal_disrupted_initial -> P(not GO)    -> Tier 2 "expect delay / consider waiting"
-- then threshold sweeps so the product can pick a cutoff by the precision it needs.
--
-- HONEST SPLIT: the model trained on is_eval = FALSE. We split the untouched eval
-- rows by situationNumber into calib (fit the scalers) and test (report), so
-- neither the model nor the scalers have seen the test rows.
--
-- NOTE: a script returns only the final statement's rows in `bq`; run the SELECT
-- steps (4, 5, 6) individually (or use the console) to see each result.

-- 1. Score the eval rows (raw class probabilities), tag calib/test, add id ----
CREATE OR REPLACE TABLE `disruptions.trip_scored_initial` AS
SELECT
  GENERATE_UUID() AS id,
  outcome AS actual,
  (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'CANCELLED') AS p_cancelled,
  (SELECT prob FROM UNNEST(predicted_outcome_probs) WHERE label = 'DELAYED')   AS p_delayed,
  MOD(ABS(FARM_FINGERPRINT(situationNumber)), 2) AS half   -- 0 = calib, 1 = test
FROM ML.PREDICT(MODEL `disruptions.trip_outcome_clf_initial`,
      (SELECT * FROM `disruptions.trip_outcomes_initial` WHERE is_eval));

-- 2a. Platt scaler for P(CANCELLED) on the calib half ------------------------
CREATE OR REPLACE MODEL `disruptions.cal_cancelled_initial`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['is_cancelled'], data_split_method='NO_SPLIT') AS
SELECT IF(actual='CANCELLED',1,0) AS is_cancelled,
  LOG(GREATEST(LEAST(p_cancelled,0.999),0.001)/(1-GREATEST(LEAST(p_cancelled,0.999),0.001))) AS logit_p
FROM `disruptions.trip_scored_initial` WHERE half=0;

-- 2b. Platt scaler for P(not GO) = P(DELAYED)+P(CANCELLED) on the calib half --
CREATE OR REPLACE MODEL `disruptions.cal_disrupted_initial`
OPTIONS(model_type='LOGISTIC_REG', input_label_cols=['is_disrupted'], data_split_method='NO_SPLIT') AS
SELECT IF(actual!='GO',1,0) AS is_disrupted,
  LOG(GREATEST(LEAST(p_delayed+p_cancelled,0.999),0.001)/(1-GREATEST(LEAST(p_delayed+p_cancelled,0.999),0.001))) AS logit_p
FROM `disruptions.trip_scored_initial` WHERE half=0;

-- 3. Apply both scalers to the test half (single source for the sweeps) ------
CREATE OR REPLACE TABLE `disruptions.trip_test_cal_initial` AS
WITH base AS (
  SELECT id, actual,
    p_cancelled             AS p_cancelled_raw,
    p_delayed + p_cancelled AS p_disrupted_raw,
    LOG(GREATEST(LEAST(p_cancelled,0.999),0.001)/(1-GREATEST(LEAST(p_cancelled,0.999),0.001))) AS logit_can,
    LOG(GREATEST(LEAST(p_delayed+p_cancelled,0.999),0.001)/(1-GREATEST(LEAST(p_delayed+p_cancelled,0.999),0.001))) AS logit_dis
  FROM `disruptions.trip_scored_initial` WHERE half=1
),
can AS (SELECT id, (SELECT prob FROM UNNEST(predicted_is_cancelled_probs) WHERE label=1) AS p_cancelled_cal
        FROM ML.PREDICT(MODEL `disruptions.cal_cancelled_initial`, (SELECT id, logit_can AS logit_p FROM base))),
dis AS (SELECT id, (SELECT prob FROM UNNEST(predicted_is_disrupted_probs) WHERE label=1) AS p_disrupted_cal
        FROM ML.PREDICT(MODEL `disruptions.cal_disrupted_initial`, (SELECT id, logit_dis AS logit_p FROM base)))
SELECT base.actual, base.p_cancelled_raw, base.p_disrupted_raw,
       can.p_cancelled_cal, dis.p_disrupted_cal
FROM base JOIN can USING (id) JOIN dis USING (id);

-- 4. Reliability of both CALIBRATED probabilities on the test half -----------
SELECT 'cancelled' AS prob, ROUND(p_cancelled_cal,1) AS p_bucket, COUNT(*) AS n,
       ROUND(AVG(IF(actual='CANCELLED',1,0)),3) AS actual_rate
FROM `disruptions.trip_test_cal_initial` GROUP BY p_bucket
UNION ALL
SELECT 'disrupted', ROUND(p_disrupted_cal,1), COUNT(*), ROUND(AVG(IF(actual!='GO',1,0)),3)
FROM `disruptions.trip_test_cal_initial` GROUP BY ROUND(p_disrupted_cal,1)
ORDER BY prob, p_bucket;

-- 5. Threshold sweeps on the calibrated probabilities ------------------------
WITH thr AS (SELECT t FROM UNNEST(GENERATE_ARRAY(0.05,0.95,0.05)) AS t)
SELECT 'cancelled' AS tier, t AS threshold,
  ROUND(COUNTIF(p_cancelled_cal>=t)/COUNT(*),4) AS coverage,
  ROUND(SAFE_DIVIDE(COUNTIF(p_cancelled_cal>=t AND actual='CANCELLED'),COUNTIF(p_cancelled_cal>=t)),3) AS precision,
  ROUND(SAFE_DIVIDE(COUNTIF(p_cancelled_cal>=t AND actual='CANCELLED'),COUNTIF(actual='CANCELLED')),3) AS recall
FROM `disruptions.trip_test_cal_initial` CROSS JOIN thr GROUP BY t
UNION ALL
SELECT 'disrupted', t,
  ROUND(COUNTIF(p_disrupted_cal>=t)/COUNT(*),4),
  ROUND(SAFE_DIVIDE(COUNTIF(p_disrupted_cal>=t AND actual!='GO'),COUNTIF(p_disrupted_cal>=t)),3),
  ROUND(SAFE_DIVIDE(COUNTIF(p_disrupted_cal>=t AND actual!='GO'),COUNTIF(actual!='GO')),3)
FROM `disruptions.trip_test_cal_initial` CROSS JOIN thr GROUP BY t
ORDER BY tier, threshold;

-- 6. Two-tier policy cascade (cutoffs chosen from the step-5 sweep) ----------
-- Cancellation is checked first so the more severe message wins. Tune the
-- cutoffs to trade false-alarm rate vs. coverage. NOTE: unlike v2, the initial
-- model is far weaker on cancellation -- calibrated P(CANCELLED) tops out ~0.4,
-- so even the highest usable cutoff only buys ~40% precision. Cutoffs below:
--   Tier 1 P(cancelled_cal) >= 0.30  -> ~40% precision, ~6% recall, ~0.5% of trips
--                                       (frame as "elevated cancellation risk",
--                                        NOT a confident "cancelled" claim)
--   Tier 2 P(notGO_cal)     >= 0.30  -> ~44% precision, ~43% recall, ~20% of trips
WITH labeled AS (
  SELECT actual,
    CASE WHEN p_cancelled_cal >= 0.30 THEN '1_ALTERNATIVE_WFH'
         WHEN p_disrupted_cal >= 0.30 THEN '2_EXPECT_DELAY'
         ELSE '3_OK' END AS message
  FROM `disruptions.trip_test_cal_initial`
)
SELECT message,
  COUNT(*) AS n,
  ROUND(COUNT(*)/SUM(COUNT(*)) OVER (), 4) AS share,
  ROUND(COUNTIF(actual='CANCELLED')/COUNT(*), 3) AS cancelled_rate,
  ROUND(COUNTIF(actual='DELAYED')/COUNT(*), 3)   AS delayed_rate,
  ROUND(COUNTIF(actual='GO')/COUNT(*), 3)        AS go_rate
FROM labeled GROUP BY message ORDER BY message;
