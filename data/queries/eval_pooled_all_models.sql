-- Per-operator eval of the ALL-OPERATOR pooled per-call family (rail + bus codespaces)
-- vs the prod (NSB-only) family. Same metric pipeline as eval_pooled_models.sql
-- (mirrors eval/impact_eval.py / predict_calls.sql):
--   p_impact     = p_cancel + (1 - p_cancel) * P(arr >= 10)   (alighting stop)
--   p_impact_dep = p_cancel + (1 - p_cancel) * P(dep >= 10)   (boarding stop)
--
-- TWO FAMILIES:
--   pooled_all -- _pooled_all models + operator-aware _pooled_all platts on the
--                 training_calls_pooled_all holdout, BY OPERATOR (13 slices).
--   prod       -- prod models + prod platts on training_calls holdout (NSB): the parity
--                 baseline, recomputed on this exact pipeline.
-- (Zero-shot was only informative for the rail experiment -- prod rail models were never
-- a serving option for bus, and the pooling-vs-zeroshot lesson is already documented.)
--
-- The operator-aware platt closed form is sigmoid(b + a*raw_logit + w[operator]), with
-- w from ML.WEIGHTS category_weights (LEFT JOIN + IFNULL 0 guards unseen operators).
-- AUC = Mann-Whitney rank statistic with average-rank tie handling. thr 0.65 = serving
-- alert threshold. Holdout = is_eval (onset >= 2026-04-01). Dry-run before running.

WITH
-- ====================== pooled_all family, pooled_all holdout ======================
pooled_ev AS (
  SELECT * FROM `ent-smartavvik-dev.disruptions.training_calls_pooled_all` WHERE is_eval
),
pl_clf AS (
  SELECT operator, situationNumber, jkey, stop_order, call_cancelled,
         is_origin, is_terminal, dep_delay_pos, arr_delay_pos,
         (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.call_cancel_clf_pooled_all`,
                  (SELECT * FROM pooled_ev))
),
pl_a10 AS (
  SELECT operator, situationNumber, jkey, stop_order,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.arr_ge10_clf_pooled_all`,
                  (SELECT * FROM pooled_ev))
),
pl_d10 AS (
  SELECT operator, situationNumber, jkey, stop_order,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.dep_ge10_clf_pooled_all`,
                  (SELECT * FROM pooled_ev))
),
-- operator-aware platt weights: scalar (b, a) + per-operator intercept shifts
pl_wc AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input='raw_logit', weight, NULL))     AS a
  FROM ML.WEIGHTS(MODEL `ent-smartavvik-dev.disruptions.cancel_platt_pooled_all`)
),
pl_wc_op AS (
  SELECT cw.category AS operator, cw.weight AS w
  FROM ML.WEIGHTS(MODEL `ent-smartavvik-dev.disruptions.cancel_platt_pooled_all`),
       UNNEST(category_weights) AS cw
  WHERE processed_input = 'operator'
),
pl_wa AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input='raw_logit', weight, NULL))     AS a
  FROM ML.WEIGHTS(MODEL `ent-smartavvik-dev.disruptions.arr_ge10_platt_pooled_all`)
),
pl_wa_op AS (
  SELECT cw.category AS operator, cw.weight AS w
  FROM ML.WEIGHTS(MODEL `ent-smartavvik-dev.disruptions.arr_ge10_platt_pooled_all`),
       UNNEST(category_weights) AS cw
  WHERE processed_input = 'operator'
),
pl_wd AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input='raw_logit', weight, NULL))     AS a
  FROM ML.WEIGHTS(MODEL `ent-smartavvik-dev.disruptions.dep_ge10_platt_pooled_all`)
),
pl_wd_op AS (
  SELECT cw.category AS operator, cw.weight AS w
  FROM ML.WEIGHTS(MODEL `ent-smartavvik-dev.disruptions.dep_ge10_platt_pooled_all`),
       UNNEST(category_weights) AS cw
  WHERE processed_input = 'operator'
),
pooled_scored AS (
  SELECT
    'pooled_all' AS family,
    c.operator,
    c.call_cancelled,
    (NOT c.is_origin   AND (c.call_cancelled OR c.arr_delay_pos IS NOT NULL)) AS eligible_arr,
    (NOT c.is_terminal AND (c.call_cancelled OR c.dep_delay_pos IS NOT NULL)) AS eligible_dep,
    (c.call_cancelled OR COALESCE(c.arr_delay_pos, 0) >= 10) AS label_arr,
    (c.call_cancelled OR COALESCE(c.dep_delay_pos, 0) >= 10) AS label_dep,
    1/(1+EXP(-(wc.b + IFNULL(oc.w, 0) + wc.a * LN(GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)))))) AS p_cancel,
    1/(1+EXP(-(wa.b + IFNULL(oa.w, 0) + wa.a * LN(GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)))))) AS p_a10,
    1/(1+EXP(-(wd.b + IFNULL(od.w, 0) + wd.a * LN(GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)))))) AS p_d10
  FROM pl_clf AS c
  JOIN pl_a10 AS a USING (operator, situationNumber, jkey, stop_order)
  JOIN pl_d10 AS d USING (operator, situationNumber, jkey, stop_order)
  CROSS JOIN pl_wc AS wc CROSS JOIN pl_wa AS wa CROSS JOIN pl_wd AS wd
  LEFT JOIN pl_wc_op AS oc ON oc.operator = c.operator
  LEFT JOIN pl_wa_op AS oa ON oa.operator = c.operator
  LEFT JOIN pl_wd_op AS od ON od.operator = c.operator
),
-- ====================== prod family, prod holdout (NSB baseline) ======================
prod_ev AS (
  SELECT * FROM `ent-smartavvik-dev.disruptions.training_calls` WHERE is_eval
),
pr_clf AS (
  SELECT situationNumber, jkey, stop_order, call_cancelled,
         is_origin, is_terminal, dep_delay_pos, arr_delay_pos,
         (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.call_cancel_clf`,
                  (SELECT * FROM prod_ev))
),
pr_a10 AS (
  SELECT situationNumber, jkey, stop_order,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.arr_ge10_clf`,
                  (SELECT * FROM prod_ev))
),
pr_d10 AS (
  SELECT situationNumber, jkey, stop_order,
         (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.dep_ge10_clf`,
                  (SELECT * FROM prod_ev))
),
pr_wc AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-smartavvik-dev.disruptions.cancel_platt`)
),
pr_wa AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-smartavvik-dev.disruptions.arr_ge10_platt`)
),
pr_wd AS (
  SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-smartavvik-dev.disruptions.dep_ge10_platt`)
),
prod_scored AS (
  SELECT
    'prod' AS family,
    'NSB'  AS operator,
    c.call_cancelled,
    (NOT c.is_origin   AND (c.call_cancelled OR c.arr_delay_pos IS NOT NULL)) AS eligible_arr,
    (NOT c.is_terminal AND (c.call_cancelled OR c.dep_delay_pos IS NOT NULL)) AS eligible_dep,
    (c.call_cancelled OR COALESCE(c.arr_delay_pos, 0) >= 10) AS label_arr,
    (c.call_cancelled OR COALESCE(c.dep_delay_pos, 0) >= 10) AS label_dep,
    1/(1+EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)))))) AS p_cancel,
    1/(1+EXP(-(wa.b + wa.a * LN(GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)))))) AS p_a10,
    1/(1+EXP(-(wd.b + wd.a * LN(GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)))))) AS p_d10
  FROM pr_clf AS c
  JOIN pr_a10 AS a USING (situationNumber, jkey, stop_order)
  JOIN pr_d10 AS d USING (situationNumber, jkey, stop_order)
  CROSS JOIN pr_wc AS wc CROSS JOIN pr_wa AS wa CROSS JOIN pr_wd AS wd
),
-- ====================== long format + rank-statistic AUC ======================
all_scored AS (
  SELECT * FROM pooled_scored
  UNION ALL SELECT * FROM prod_scored
),
long AS (
  SELECT family, operator, 'cancel' AS metric, call_cancelled AS y, p_cancel AS p
  FROM all_scored
  UNION ALL
  SELECT family, operator, 'impact_arr', label_arr, p_cancel + (1 - p_cancel) * p_a10
  FROM all_scored WHERE eligible_arr
  UNION ALL
  SELECT family, operator, 'impact_dep', label_dep, p_cancel + (1 - p_cancel) * p_d10
  FROM all_scored WHERE eligible_dep
),
ranked AS (
  -- average rank with tie handling, without PARTITION BY p (FLOAT64 partitioning is
  -- disallowed): avg rank = (rank_asc + rank_desc mirrored) / 2
  SELECT
    family, operator, metric, y, p,
    (RANK() OVER (PARTITION BY family, operator, metric ORDER BY p ASC)
     + COUNT(*) OVER (PARTITION BY family, operator, metric) + 1
     - RANK() OVER (PARTITION BY family, operator, metric ORDER BY p DESC)) / 2.0 AS r_avg
  FROM long
)
SELECT
  family, operator, metric,
  COUNT(*)   AS n,
  COUNTIF(y) AS n_pos,
  ROUND(SAFE_DIVIDE(COUNTIF(y), COUNT(*)), 4) AS base_rate,
  ROUND(SAFE_DIVIDE(
          SUM(IF(y, r_avg, 0)) - COUNTIF(y) * (COUNTIF(y) + 1) / 2.0,
          COUNTIF(y) * COUNTIF(NOT y)), 4)    AS auc,
  COUNTIF(p >= 0.65) AS n_alert_065,
  ROUND(SAFE_DIVIDE(COUNTIF(y AND p >= 0.65), COUNTIF(p >= 0.65)), 3) AS precision_065,
  ROUND(SAFE_DIVIDE(COUNTIF(y AND p >= 0.65), COUNTIF(y)), 3)         AS recall_065
FROM ranked
GROUP BY family, operator, metric
ORDER BY metric, operator, family;
