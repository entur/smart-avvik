-- Huskehjulet (E05) step 1: recurrence census — do disruptions recur at the same
-- place with the same cause, and with stable durations?
--
-- Grain: one row per (stop place, category) cluster with >= 4 incidents in 2 years,
-- over the reopening-labelled NSB slice (disruptions_initial, ~7.9k situations).
--
-- LESSON FROM THE FIRST RUN (no localized filter): raw counts are dominated by
-- smear — a line-wide situation is counted at every affected stop, so Oslo-area
-- hubs show 500–2300 "incidents" each and no cluster is majority-localized.
-- The localized filter (<= 3 affected stops) is what isolates the station-local
-- recurrences that are the actual huskehjul candidates.
--
-- FINDINGS (2026-06-10 run): two distinct families —
--   * materiell/teknisk "Toget står" clusters: frequent everywhere, fast and
--     predictable (median reopen 7–11 min) — place barely matters.
--   * infrastruktur/{signal,sporveksel,spor} clusters at specific hubs (Oslo S,
--     Lillestrøm, Drammen, Ski, Lysaker): 15–51 repeats in 2 years, long and
--     heavy-tailed (median reopen 25–82 min, p80 up to ~3.5h). These are the
--     "fjerde gang på seks måneder"-patterns from the E05 storyboard, real.

WITH situations AS (
  SELECT
    d.situationNumber, d.onset_ts, d.reopen_minutes, d.impact_minutes,
    d.summary, d.description, c.category, stop_ref
  FROM `ent-kixavvik-dev.disruptions.disruptions_initial` AS d
  LEFT JOIN `ent-kixavvik-dev.disruptions.categories` AS c
    -- IFNULL-sentinel equality: NULL-safe AND hash-joinable (IS NOT DISTINCT FROM blew the
    -- on-demand CPU limit -- see huskehjul_backtest.sql).
    ON  IFNULL(c.summary, '__NULL__')     = IFNULL(d.summary, '__NULL__')
    AND IFNULL(c.description, '__NULL__') = IFNULL(d.description, '__NULL__')
  CROSS JOIN UNNEST(d.affected_stops) AS stop_ref
  WHERE ARRAY_LENGTH(d.affected_stops) <= 3   -- localized incidents only (see header)
)
SELECT
  s.stop_ref,
  ANY_VALUE(nsr.name) AS stop_name,
  s.category,
  COUNT(DISTINCT s.situationNumber) AS n_incidents,
  COUNT(DISTINCT IF(s.onset_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 180 DAY),
                    s.situationNumber, NULL)) AS n_last_6mo,
  DATE_DIFF(MAX(DATE(s.onset_ts)), MIN(DATE(s.onset_ts)), DAY) AS span_days,
  APPROX_QUANTILES(s.reopen_minutes, 100)[OFFSET(50)] AS median_reopen_min,
  APPROX_QUANTILES(s.reopen_minutes, 100)[OFFSET(80)] AS p80_reopen_min,
  APPROX_QUANTILES(s.impact_minutes, 100)[OFFSET(50)] AS median_impact_min,
  APPROX_QUANTILES(s.impact_minutes, 100)[OFFSET(80)] AS p80_impact_min,
  ARRAY_TO_STRING(ARRAY_AGG(DISTINCT SUBSTR(s.description, 1, 80) IGNORE NULLS LIMIT 3),
                  ' | ') AS sample_descriptions
FROM situations AS s
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version` AS nsr
  ON nsr.id = s.stop_ref
GROUP BY s.stop_ref, s.category
HAVING n_incidents >= 4
ORDER BY n_incidents DESC
LIMIT 60;
