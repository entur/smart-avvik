-- Lookup table for passenger alerting: realized NSB impact by
-- (category x line x hour-of-day), conditional on the journey being flagged by an
-- SX situation of that category. "Given a category-X disruption flag on line L at
-- hour H, how likely is real trouble, and how bad?" -- exactly the alert decision.
--
-- SOURCE / COST: built entirely off materialized tables (disruptions, categories,
-- rail_calls) -- no ET scan. ~1 GB, a few cents. rail_calls already paid the 2y ET
-- scan; rebuild this table whenever rail_calls/categories are refreshed.
--
-- GRAIN + ROLLUPS: GROUP BY CUBE(category, line, hour) emits the full cube, so the
-- consumer can look up the most specific cell and BACK OFF when it is too sparse:
--   (C,L,H) -> (C,L) -> (C,H) -> (C) -> (L,H) -> (L) -> (H) -> overall
-- The `grain` column ('CLH','CL-','C--', ...) and sentinels mark each level:
--   category/line = '*'  and  hour = -1  mean "aggregated over this dimension".
-- Sparse most-granular cells are dropped (HAVING n_boardings >= 10); rollups remain,
-- so a back-off always resolves. Filter on n_boardings / n_situations to gate trust.
--
-- METRICS: cancel_rate and trouble_rate (cancelled OR dep_delay >= 10 min) are the
-- headline alert signals. A NULL cancellation flag is read as "not cancelled" (SIRI
-- convention; see the COALESCE note on the flagged CTE) so the denominators are ALL
-- boardings, not just those with a determinate flag. Delay percentiles/mean/std are over
-- NON-cancelled boardings only (clamped >= 0; cancellations are the separate cancel_rate).
-- Each row also carries the UNCONDITIONAL baseline (all NSB calls at the same line+hour)
-- and the lift = conditional / baseline, so a consumer can rank by "how much the flag adds".
--
-- RANKING (alert_score + alert_tier): a single rankable score so the consumer can
-- order cells directly instead of juggling raw metrics.
--   exp_disruption_min = expected lateness per flagged boarding, magnitude-aware:
--     (1 - cancel_rate) * delay_mean  +  cancel_rate * 60   (a cancellation counted
--     as 60 passenger-minutes; tune CANCEL_EQUIV below).
--   confidence = n_situations / (n_situations + 30)  -- shrink by sample size, with the
--     SITUATION (not boarding) as the independent unit, so a sparse cell can't rank high
--     on noise.
--   alert_score = exp_disruption_min * confidence.
--   alert_tier  = 'suppress' when the flag is no better than that line+hour's own
--     baseline (trouble_lift < 1.3) or too few situations (< 10); else high/medium/low by
--     score (>=12 / >=5 / else). All thresholds are tunable presentation choices.
--
-- CAVEATS: conditional on the SX flag (not P(disruption)); a journey flagged by
-- several situations is counted under each (category, situation); categories are
-- AI.CLASSIFY-derived from Norwegian summary+description. Scope: participantRef='NSB'.

CREATE OR REPLACE TABLE `ent-smartavvik-dev.disruptions.category_line_hour_impact` AS
WITH
  aff AS (
    SELECT
      d.situationNumber,
      SAFE.TIMESTAMP(d.creationTime)        AS onset_ts,
      IFNULL(c.category, 'UNKNOWN')         AS category,
      REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
    FROM `disruptions.disruptions` AS d
    -- NULL-safe join: many NSB situations have a NULL summary (and/or description), and
    -- plain `=` yields NULL (never TRUE) on a NULL side, so those situations would never
    -- match a category row and fall through to 'UNKNOWN' regardless of the categories
    -- table's coverage. Use IFNULL-sentinel equality: NULL-safe AND hash-joinable. (IS NOT
    -- DISTINCT FROM is also NULL-safe but not hash-joinable and blew the on-demand CPU limit
    -- -- see huskehjul_backtest.sql.)
    LEFT JOIN `disruptions.categories` AS c
      ON  IFNULL(d.summary, '__NULL__')     = IFNULL(c.summary, '__NULL__')
     AND IFNULL(d.description, '__NULL__') = IFNULL(c.description, '__NULL__'),
    UNNEST(d.affected_journeys) AS j
    WHERE d.participantRef = 'NSB'
  ),

  -- One row per flagged boarding (situation x affected journey x boardable stop).
  -- COALESCE(call_cancelled, FALSE): in SIRI ET the cancellation flag is only set when a
  -- trip is actually cancelled, so NULL means "not cancelled". Without the COALESCE,
  -- is_trouble is NULL for an on-time NULL-flag train, and AVG(CAST(... AS INT64)) below
  -- silently drops those rows from the denominator -- which over-states cancel/trouble
  -- rates on lines where the flag is often NULL (e.g. long-distance F4/RE).
  flagged AS (
    SELECT
      a.category,
      a.situationNumber,
      REPLACE(r.lineRef, 'VYG:Line:', '')                       AS line,
      EXTRACT(HOUR FROM r.aimed_dep AT TIME ZONE 'Europe/Oslo')  AS hour,
      COALESCE(r.call_cancelled, FALSE)                          AS call_cancelled,
      IF(NOT COALESCE(r.call_cancelled, FALSE), GREATEST(r.dep_delay_min, 0), NULL) AS delay_min,
      (COALESCE(r.call_cancelled, FALSE) OR r.dep_delay_min >= 10) AS is_trouble
    FROM aff AS a
    JOIN `disruptions.rail_calls` AS r USING (jkey)
    WHERE r.aimed_dep IS NOT NULL
      AND (r.call_cancelled OR r.dep_delay_min IS NOT NULL)
      AND TIMESTAMP_DIFF(r.aimed_dep, a.onset_ts, MINUTE) BETWEEN 0 AND 720
  ),

  flag_agg AS (
    SELECT
      GROUPING(category) AS g_cat,
      GROUPING(line)     AS g_line,
      GROUPING(hour)     AS g_hour,
      category, line, hour,
      COUNT(*)                                       AS n_boardings,
      COUNT(DISTINCT situationNumber)                AS n_situations,
      ROUND(AVG(CAST(call_cancelled AS INT64)), 4)   AS cancel_rate,
      ROUND(AVG(CAST(is_trouble AS INT64)), 4)       AS trouble_rate,
      COUNTIF(delay_min IS NOT NULL)                 AS n_noncancel,
      ROUND(AVG(delay_min), 1)                       AS delay_mean,
      ROUND(STDDEV(delay_min), 1)                    AS delay_std,
      APPROX_QUANTILES(delay_min, 100)[OFFSET(50)]   AS delay_p50,
      APPROX_QUANTILES(delay_min, 100)[OFFSET(90)]   AS delay_p90,
      APPROX_QUANTILES(delay_min, 100)[OFFSET(95)]   AS delay_p95
    FROM flagged
    GROUP BY CUBE(category, line, hour)
    HAVING COUNT(*) >= 10
  ),

  -- Unconditional baseline (ALL NSB calls), cubed over (line, hour) only.
  -- Same COALESCE(call_cancelled, FALSE) as the flagged set, so baseline and conditional
  -- rates (and the lift between them) treat a NULL cancellation flag identically.
  base AS (
    SELECT
      REPLACE(lineRef, 'VYG:Line:', '')                        AS line,
      EXTRACT(HOUR FROM aimed_dep AT TIME ZONE 'Europe/Oslo')  AS hour,
      COALESCE(call_cancelled, FALSE) AS call_cancelled, dep_delay_min
    FROM `disruptions.rail_calls`
    WHERE call_cancelled OR dep_delay_min IS NOT NULL
  ),
  base_agg AS (
    SELECT
      GROUPING(line) AS g_line,
      GROUPING(hour) AS g_hour,
      line, hour,
      COUNT(*)                                                            AS base_n,
      ROUND(AVG(CAST(call_cancelled AS INT64)), 4)                        AS base_cancel_rate,
      ROUND(AVG(CAST((call_cancelled OR dep_delay_min >= 10) AS INT64)),4) AS base_trouble_rate
    FROM base
    GROUP BY CUBE(line, hour)
  ),

  joined AS (
    SELECT
      CONCAT(IF(f.g_cat = 0, 'C', '-'),
             IF(f.g_line = 0, 'L', '-'),
             IF(f.g_hour = 0, 'H', '-'))          AS grain,
      IFNULL(f.category, '*')                     AS category,
      IFNULL(f.line, '*')                         AS line,
      IF(f.g_hour = 0, f.hour, -1)                AS hour,
      f.n_boardings,
      f.n_situations,
      f.cancel_rate,
      f.trouble_rate,
      f.n_noncancel,
      f.delay_mean,
      f.delay_std,
      f.delay_p50,
      f.delay_p90,
      f.delay_p95,
      b.base_cancel_rate,
      b.base_trouble_rate,
      ROUND(SAFE_DIVIDE(f.cancel_rate,  b.base_cancel_rate), 2)  AS cancel_lift,
      ROUND(SAFE_DIVIDE(f.trouble_rate, b.base_trouble_rate), 2) AS trouble_lift,
      ROUND((1 - f.cancel_rate) * IFNULL(f.delay_mean, 0) + f.cancel_rate * 60, 1) AS exp_disruption_min,
      ROUND(f.n_situations / (f.n_situations + 30), 2)          AS confidence
    FROM flag_agg AS f
    LEFT JOIN base_agg AS b
      ON f.g_line = b.g_line
     AND f.g_hour = b.g_hour
     AND (f.g_line = 1 OR f.line = b.line)
     AND (f.g_hour = 1 OR f.hour = b.hour)
  )

SELECT
  j.*,
  ROUND(j.exp_disruption_min * j.confidence, 1) AS alert_score,
  CASE
    WHEN j.n_situations < 10 OR IFNULL(j.trouble_lift, 0) < 1.3        THEN 'suppress'
    WHEN ROUND(j.exp_disruption_min * j.confidence, 1) >= 12           THEN 'high'
    WHEN ROUND(j.exp_disruption_min * j.confidence, 1) >= 5            THEN 'medium'
    ELSE 'low'
  END AS alert_tier
FROM joined AS j;
