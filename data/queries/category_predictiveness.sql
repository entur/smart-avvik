-- How predictive is each disruption category of realized NSB impact (delay + cancellation)?
--
-- Joins every NSB situation (disruptions) to its category (categories) and to the realized
-- outcomes of its affected journeys' calls (rail_calls), within 0..720 min of onset -- the
-- same flagged-boarding join as create_category_line_hour_impact.sql. Produces a per-category
-- scorecard so weak/noisy categories (candidates to MERGE or DROP) and large heterogeneous
-- ones (candidates to SPLIT) stand out, plus a single taxonomy-level predictiveness number.
-- Use it to evaluate / revise category_taxonomy. Cheap: materialized tables only, no ET scan.
--
-- UNITS: predictiveness stats are at the SITUATION level (the independent unit; calls within a
-- situation are correlated). Magnitudes (cancel_rate, delay_*) are at the boarding/call level.
-- A NULL ET cancellation flag is read as "not cancelled" (SIRI convention).
--
-- SEVERITY (exp_min): per affected call, cancellation = CANCEL_EQUIV passenger-minutes, else
-- max(dep_delay,0); a situation's severity = mean over its calls. eta2 = share of the
-- between-situation severity variance explained by category -- the single "how predictive is
-- the taxonomy" number (0..1, higher = more predictive). eta2_contrib = each category's share
-- of it. effect_size_d = (category mean - grand mean) / grand sd (how far this category sits
-- from average). exp_min_sd = within-category spread (low = homogeneous = predictable).

DECLARE CANCEL_EQUIV INT64 DEFAULT 60;

-- One row per (situation x affected call) within the impact window.
CREATE TEMP TABLE per_call AS
WITH cats AS (   -- one category per (summary, description), NULL-safe keys
  SELECT IFNULL(summary, '__NULL__') AS s_key,
         IFNULL(description, '__NULL__') AS d_key,
         ANY_VALUE(category) AS category
  FROM `disruptions.categories`
  GROUP BY s_key, d_key
),
aff AS (
  SELECT
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime)        AS onset_ts,
    IFNULL(c.category, 'UNKNOWN')         AS category,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM `disruptions.disruptions` AS d
  LEFT JOIN cats AS c
    ON  IFNULL(d.summary, '__NULL__')     = c.s_key
    AND IFNULL(d.description, '__NULL__') = c.d_key,
  UNNEST(d.affected_journeys) AS j
  WHERE d.participantRef = 'NSB'
)
SELECT
  a.situationNumber,
  a.category,
  COALESCE(r.call_cancelled, FALSE)                                          AS cancelled,
  IF(NOT COALESCE(r.call_cancelled, FALSE), GREATEST(r.dep_delay_min, 0), NULL) AS delay_min,
  (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min >= 10)               AS is_trouble,
  IF(COALESCE(r.call_cancelled, FALSE), CANCEL_EQUIV, GREATEST(r.dep_delay_min, 0)) AS exp_min
FROM aff AS a
JOIN `disruptions.rail_calls` AS r USING (jkey)
WHERE r.aimed_dep IS NOT NULL
  AND (r.call_cancelled OR r.dep_delay_min IS NOT NULL)
  AND TIMESTAMP_DIFF(r.aimed_dep, a.onset_ts, MINUTE) BETWEEN 0 AND 720;

-- Collapse to one row per situation (the independent unit).
CREATE TEMP TABLE per_sit AS
SELECT
  situationNumber,
  ANY_VALUE(category)              AS category,
  COUNT(*)                         AS n_calls,
  AVG(exp_min)                     AS sev_exp,
  AVG(CAST(cancelled AS INT64))    AS sit_cancel,
  AVG(CAST(is_trouble AS INT64))   AS sit_trouble,
  AVG(delay_min)                   AS sit_delay_mean
FROM per_call
GROUP BY situationNumber;

-- Per-category scorecard, ranked by share of severity variance explained.
WITH
g_sit AS (
  SELECT COUNT(*) AS N, AVG(sev_exp) AS mean_exp,
         VAR_POP(sev_exp) AS var_exp, STDDEV_POP(sev_exp) AS sd_exp
  FROM per_sit
),
g_call AS (
  SELECT AVG(CAST(cancelled AS INT64))  AS base_cancel_rate,
         AVG(CAST(is_trouble AS INT64)) AS base_trouble_rate
  FROM per_call
),
cat_sit AS (
  SELECT category, COUNT(*) AS n_situations, SUM(n_calls) AS n_boardings,
         AVG(sev_exp) AS exp_min_mean, STDDEV(sev_exp) AS exp_min_sd
  FROM per_sit GROUP BY category
),
cat_call AS (
  SELECT category,
         AVG(CAST(cancelled AS INT64))  AS cancel_rate,
         AVG(CAST(is_trouble AS INT64)) AS trouble_rate,
         AVG(delay_min)                 AS delay_mean,
         APPROX_QUANTILES(delay_min, 100)[OFFSET(90)] AS delay_p90
  FROM per_call GROUP BY category
)
SELECT
  cs.category,
  cs.n_situations,
  cs.n_boardings,
  ROUND(cc.cancel_rate, 4)                                            AS cancel_rate,
  ROUND(cc.trouble_rate, 4)                                           AS trouble_rate,
  ROUND(cc.delay_mean, 1)                                             AS delay_mean,
  cc.delay_p90,
  ROUND(cs.exp_min_mean, 1)                                           AS exp_min_mean,
  ROUND(cs.exp_min_sd, 1)                                             AS exp_min_sd,
  ROUND(SAFE_DIVIDE(cc.cancel_rate,  gc.base_cancel_rate), 2)         AS cancel_lift,
  ROUND(SAFE_DIVIDE(cc.trouble_rate, gc.base_trouble_rate), 2)        AS trouble_lift,
  ROUND(SAFE_DIVIDE(cs.exp_min_mean - gs.mean_exp, NULLIF(gs.sd_exp,0)), 2)            AS effect_size_d,
  ROUND(SAFE_DIVIDE(cs.n_situations * POW(cs.exp_min_mean - gs.mean_exp, 2),
                    gs.N * gs.var_exp), 4)                            AS eta2_contrib
FROM cat_sit AS cs
JOIN cat_call AS cc USING (category)
CROSS JOIN g_sit AS gs
CROSS JOIN g_call AS gc
ORDER BY eta2_contrib DESC;

-- Taxonomy-level predictiveness: eta2 = between-situation variance explained by category.
WITH
g AS (
  SELECT COUNT(*) AS N,
         AVG(sev_exp) AS m, VAR_POP(sev_exp) AS v,
         AVG(sit_cancel) AS mc, VAR_POP(sit_cancel) AS vc,
         AVG(sit_trouble) AS mt, VAR_POP(sit_trouble) AS vt
  FROM per_sit
),
c AS (
  SELECT category, COUNT(*) AS n,
         AVG(sev_exp) AS m, AVG(sit_cancel) AS mc, AVG(sit_trouble) AS mt
  FROM per_sit GROUP BY category
)
SELECT
  g.N AS n_situations,
  COUNT(*) AS n_categories,
  ROUND(SUM(c.n * POW(c.m  - g.m,  2)) / NULLIF(g.N * g.v,  0), 4) AS eta2_severity,
  ROUND(SUM(c.n * POW(c.mc - g.mc, 2)) / NULLIF(g.N * g.vc, 0), 4) AS eta2_cancel,
  ROUND(SUM(c.n * POW(c.mt - g.mt, 2)) / NULLIF(g.N * g.vt, 0), 4) AS eta2_trouble
FROM c CROSS JOIN g
GROUP BY g.N, g.v, g.vc, g.vt;
