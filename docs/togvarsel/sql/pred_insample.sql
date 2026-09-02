WITH t AS (SELECT * FROM `disruptions.togvarsel_training` WHERE eligible),
p1 AS (SELECT d, (SELECT prob FROM UNNEST(predicted_label_probs) WHERE LOWER(CAST(label AS STRING))='true') p_0630
       FROM ML.PREDICT(MODEL `disruptions.togvarsel_m_am0630`, (SELECT * FROM t))),
p2 AS (SELECT d, (SELECT prob FROM UNNEST(predicted_label_probs) WHERE LOWER(CAST(label AS STRING))='true') p_0700
       FROM ML.PREDICT(MODEL `disruptions.togvarsel_m_am0700`, (SELECT * FROM t))),
p3 AS (SELECT d, (SELECT prob FROM UNNEST(predicted_label_probs) WHERE LOWER(CAST(label AS STRING))='true') p_1200
       FROM ML.PREDICT(MODEL `disruptions.togvarsel_m_pm1200`, (SELECT * FROM t))),
p4 AS (SELECT d, (SELECT prob FROM UNNEST(predicted_label_probs) WHERE LOWER(CAST(label AS STRING))='true') p_1400
       FROM ML.PREDICT(MODEL `disruptions.togvarsel_m_pm1400`, (SELECT * FROM t)))
SELECT t.d, t.dow, t.low_season, t.is_tune, t.am_t10, t.am_t10_rest0700, t.pm_t10,
       t.y_am10, t.y_amrest10, t.y_pm10, p_0630, p_0700, p_1200, p_1400
FROM t JOIN p1 USING (d) JOIN p2 USING (d) JOIN p3 USING (d) JOIN p4 USING (d)
ORDER BY d
