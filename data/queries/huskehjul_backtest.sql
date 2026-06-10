-- Huskehjulet (E05) step 2: precedent backtest — is "sist gang tok det X min"
-- (median of earlier incidents, same stop + category) competitive with the trained
-- duration models?
--
-- Two jobs (one script breaks the on-demand CPU/bytes ratio; see notes below):
--   Job 1 materializes hh_backtest_base — every localized situation (<= 3 affected
--         stops) with its leave-one-out place precedent: median reopen/impact of
--         strictly EARLIER incidents sharing a stop AND category.
--   Job 2 scores four methods on identical rows (held-out is_eval, n_priors >= 3,
--         n = 321): place precedent, train-split category median, train-split
--         global median, and dur_init_reopen / dur_init_impact.
--
-- RESULTS (2026-06-10): everything is tied — model MAE 48.4 / precedent 50.3 /
-- category-median 50.3 (reopen); 68.9 / 74.6 / 71.9 (impact). Per family the
-- ordering holds: on infrastruktur rows (n=87, the long heavy-tailed clusters)
-- precedent 125.0 vs model 121.6 vs cat-median 127.8 (reopen MAE). Differences of
-- 2-5 min on n~100-300 are noise.
--   * H2 verdict: place adds ~nothing beyond category for POINT accuracy — but
--     precedent retrieval matches the trained model with a method that is fully
--     explainable ("4. gang her; sist tok det 76 min"). For the E05 demo that is
--     the result: the huskehjul message is as accurate as the ML model.
--
-- GOTCHAS that cost retries:
--   * bq query "$(...)": the query string must not START with an SQL comment —
--     bq's flag parser eats a leading "--".
--   * IS NOT DISTINCT FROM in join predicates blew the on-demand CPU limit
--     (21k CPU-s on 50MB scanned); IFNULL()-equality joins run in seconds.
--   * Expanding per-row medians via self-join (t.onset < p.onset over all 8k
--     situations) also blew the limit — train-split constants are the cheap,
--     established baseline anyway (train_dual_models.sql).

-- ===== Job 1: base table ========================================================
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.hh_backtest_base` AS
WITH sit AS (
  SELECT d.situationNumber, d.onset_ts, d.reopen_minutes, d.impact_minutes,
         c.category, d.affected_stops, ARRAY_LENGTH(d.affected_stops) AS n_stops
  FROM `ent-kixavvik-dev.disruptions.disruptions_initial` AS d
  LEFT JOIN `ent-kixavvik-dev.disruptions.categories` AS c
    ON  IFNULL(c.summary, '')     = IFNULL(d.summary, '')
    AND IFNULL(c.description, '') = IFNULL(d.description, '')
),
loc AS (SELECT * FROM sit WHERE n_stops <= 3),
pairs AS (
  SELECT s.situationNumber, s.onset_ts, s.category, s.reopen_minutes, s.impact_minutes, stop_ref
  FROM loc AS s CROSS JOIN UNNEST(s.affected_stops) AS stop_ref
),
matches AS (
  SELECT DISTINCT t.situationNumber AS sn,
         p.situationNumber AS prior_sn, p.reopen_minutes AS prior_reopen, p.impact_minutes AS prior_impact
  FROM pairs AS t
  JOIN pairs AS p
    ON  p.stop_ref = t.stop_ref
    AND IFNULL(p.category, '?') = IFNULL(t.category, '?')
    AND p.onset_ts < t.onset_ts
),
prec_place AS (
  SELECT sn, COUNT(*) AS n_priors,
         APPROX_QUANTILES(prior_reopen, 2)[OFFSET(1)] AS place_reopen,
         APPROX_QUANTILES(prior_impact, 2)[OFFSET(1)] AS place_impact
  FROM matches GROUP BY sn
)
SELECT l.situationNumber, l.onset_ts, l.category, l.reopen_minutes, l.impact_minutes,
       pp.n_priors, pp.place_reopen, pp.place_impact
FROM loc AS l
JOIN prec_place AS pp ON pp.sn = l.situationNumber;

-- ===== Job 2: metrics (run separately) ==========================================
WITH split AS (
  SELECT situationNumber, is_eval FROM `ent-kixavvik-dev.disruptions.training_initial`
),
sit AS (
  SELECT d.situationNumber, d.reopen_minutes, d.impact_minutes, c.category
  FROM `ent-kixavvik-dev.disruptions.disruptions_initial` AS d
  LEFT JOIN `ent-kixavvik-dev.disruptions.categories` AS c
    ON  IFNULL(c.summary, '')     = IFNULL(d.summary, '')
    AND IFNULL(c.description, '') = IFNULL(d.description, '')
),
cat_median AS (
  SELECT IFNULL(s.category, '?') AS category,
         APPROX_QUANTILES(s.reopen_minutes, 2)[OFFSET(1)] AS cat_reopen,
         APPROX_QUANTILES(s.impact_minutes, 2)[OFFSET(1)] AS cat_impact
  FROM sit AS s JOIN split USING (situationNumber)
  WHERE NOT split.is_eval
  GROUP BY 1
),
glob_median AS (
  SELECT APPROX_QUANTILES(s.reopen_minutes, 2)[OFFSET(1)] AS glob_reopen,
         APPROX_QUANTILES(s.impact_minutes, 2)[OFFSET(1)] AS glob_impact
  FROM sit AS s JOIN split USING (situationNumber)
  WHERE NOT split.is_eval
),
mp_reopen AS (
  SELECT situationNumber, EXP(predicted_label) AS model_reopen
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dur_init_reopen`,
                  (SELECT * FROM `ent-kixavvik-dev.disruptions.training_initial` WHERE is_eval))
),
mp_impact AS (
  SELECT situationNumber, EXP(predicted_label) AS model_impact
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dur_init_impact`,
                  (SELECT * FROM `ent-kixavvik-dev.disruptions.training_initial` WHERE is_eval))
),
evalset AS (
  SELECT b.*, cm.cat_reopen, cm.cat_impact, gm.glob_reopen, gm.glob_impact,
         mr.model_reopen, mi.model_impact,
         CASE
           WHEN b.category LIKE 'infrastruktur%' THEN 'infrastruktur'
           WHEN b.category = 'materiell/teknisk' THEN 'materiell'
           ELSE 'annet'
         END AS family
  FROM `ent-kixavvik-dev.disruptions.hh_backtest_base` AS b
  JOIN split ON split.situationNumber = b.situationNumber AND split.is_eval
  LEFT JOIN cat_median AS cm ON cm.category = IFNULL(b.category, '?')
  CROSS JOIN glob_median AS gm
  JOIN mp_reopen  AS mr ON mr.situationNumber = b.situationNumber
  JOIN mp_impact  AS mi ON mi.situationNumber = b.situationNumber
  WHERE b.n_priors >= 3
),
preds AS (
  SELECT family, 'reopen' AS target, 'precedent_place_cat' AS method, reopen_minutes AS actual, CAST(place_reopen AS FLOAT64) AS pred FROM evalset
  UNION ALL SELECT family, 'reopen', 'category_median', reopen_minutes, cat_reopen   FROM evalset
  UNION ALL SELECT family, 'reopen', 'global_median',   reopen_minutes, glob_reopen  FROM evalset
  UNION ALL SELECT family, 'reopen', 'model',           reopen_minutes, model_reopen FROM evalset
  UNION ALL SELECT family, 'impact', 'precedent_place_cat', impact_minutes, place_impact FROM evalset
  UNION ALL SELECT family, 'impact', 'category_median', impact_minutes, cat_impact   FROM evalset
  UNION ALL SELECT family, 'impact', 'global_median',   impact_minutes, glob_impact  FROM evalset
  UNION ALL SELECT family, 'impact', 'model',           impact_minutes, model_impact FROM evalset
)
SELECT target, method, COUNT(*) AS n,                -- add `family` to GROUP BY for the per-family view
       ROUND(AVG(ABS(pred - actual)), 1) AS mae_min,
       ROUND(APPROX_QUANTILES(ABS(pred - actual), 2)[OFFSET(1)], 1) AS median_ae_min
FROM preds
GROUP BY target, method
ORDER BY target, mae_min;
