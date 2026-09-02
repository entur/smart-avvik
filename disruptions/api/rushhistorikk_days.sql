-- /rushhistorikk-api/days — one row per (Oslo day, issue): the FACTS, and nothing derived.
--
-- Three records join here and each answers a different question:
--   togvarsel_issued    what we forecast, and whether the forecast was late
--   togvarsel_rounds    what we communicated, and why we did not when we did not
--   togvarsel_outcomes  what the rush actually did, per scope
--
-- The verdict is NOT computed here. It is a fourteen-branch table over three named populations
-- (rushvarsel_verdicts.py), and expressing it a second time in SQL is how two definitions of
-- "traff" get shipped. This file returns facts; Python classifies and aggregates. That also keeps
-- the taxonomy unit-testable without BigQuery, which is where its real risk lives.
--
-- togvarsel_issued is the SPINE, and the joins are LEFT for a reason: an issue with no round record
-- must still produce a row, or the page silently loses the very state it needs to show — "ikke
-- vurdert", which is every issue predating this feature, every issue in an environment where the
-- notify gate is closed (permanently true in tst), and every issue whose best-effort round write
-- failed.
--
-- THE FIVE SUBSCRIBER-DERIVED COLUMNS ON togvarsel_rounds ARE NEVER NAMED HERE.
-- matched_journeys, matched_devices, n_sent, n_error and n_gone are day-grained counts over a
-- subscriber population currently numbering a handful, and /rushhistorikk-api is unauthenticated on
-- a public host (SSO_REQUIRED is "false" in every environment). A query that does not name a column
-- cannot leak it however the model above it drifts — meldinger_decisions.sql's rule, and
-- test_rushhistorikk pins it by grepping this text.
--
-- No partition-pruning predicate to get right: all three tables are tiny (four issue rows per
-- weekday, three outcome rows per day), so a 365-day window is bytes, not kilobytes. Measured 305 bytes by dry run
-- against ent-kixavvik-dev on 2026-09-01. The response is cached anyway, on a four-value window
-- allow-list, so the reachable key space is four.
WITH outcomes AS (
  -- One row per day, the three scopes pivoted. Each scope carries its own level because each
  -- issue is scored against the window ITS model was trained on: 0630 against am (06:30-09:00),
  -- 0700 against amrest (07:00-09:00), 1200/1400 against pm. Giving 0700 the whole morning would
  -- hand it thirty minutes that had already happened when it was issued.
  SELECT
    d,
    MAX(IF(scope = 'am', level, NULL)) AS am_level,
    MAX(IF(scope = 'am', trouble10, NULL)) AS am_trouble10,
    MAX(IF(scope = 'amrest', level, NULL)) AS amrest_level,
    MAX(IF(scope = 'amrest', trouble10, NULL)) AS amrest_trouble10,
    MAX(IF(scope = 'pm', level, NULL)) AS pm_level,
    MAX(IF(scope = 'pm', trouble10, NULL)) AS pm_trouble10,
    -- What the page showed at 09:20 / 18:20, kept beside the settled value so a reader can see the
    -- two disagree rather than wondering why a verdict changed overnight.
    MAX(IF(scope = 'am', provisional_level, NULL)) AS am_provisional_level,
    MAX(IF(scope = 'pm', provisional_level, NULL)) AS pm_provisional_level,
    -- Eligibility is a property of the DAY (09_training.sql thresholds am_n and pm_n), so
    -- LOGICAL_AND over the scopes that carry it, and NULL while only provisional rows exist.
    -- NULL when no scope has answered yet, rather than the TRUE that IFNULL alone would produce:
    -- the provisional pass writes eligible = NULL on purpose (its call counts are not the training
    -- chain's COUNT(*)), and a reader -- including api.ts's `eligible: boolean | null` -- tests for
    -- that NULL.
    IF(COUNTIF(eligible IS NOT NULL) = 0, NULL, LOGICAL_AND(IFNULL(eligible, TRUE))) AS eligible,
    -- The whole day is settled only when every scope is. A half-settled day is still `venter` for
    -- the issues whose own scope has not landed, which is why the per-scope flags travel too.
    -- LOGICAL_OR over "is this THE scope, and is it settled", never LOGICAL_AND over an IF whose
    -- else-branch is TRUE. That form reports a scope as SETTLED when the day has no row for it at
    -- all -- the else-branch answers TRUE for every other scope's row and the AND passes -- so
    -- today's 12:00/14:00 issues read `ukjent` instead of `venter` every weekday between 12:00 and
    -- 18:20, which is exactly the window a reader looks at. Verified against BigQuery: the old form
    -- returns true and this one false for a day carrying only am + amrest rows.
    LOGICAL_OR(scope = 'am' AND IFNULL(settled, FALSE)) AS am_settled,
    LOGICAL_OR(scope = 'amrest' AND IFNULL(settled, FALSE)) AS amrest_settled,
    LOGICAL_OR(scope = 'pm' AND IFNULL(settled, FALSE)) AS pm_settled,
    LOGICAL_OR(IFNULL(unresolvable, FALSE)) AS any_unresolvable,
    MAX(IF(scope = 'am', IFNULL(unresolvable, FALSE), FALSE)) AS am_unresolvable,
    MAX(IF(scope = 'amrest', IFNULL(unresolvable, FALSE), FALSE)) AS amrest_unresolvable,
    MAX(IF(scope = 'pm', IFNULL(unresolvable, FALSE), FALSE)) AS pm_unresolvable
  FROM `disruptions.togvarsel_outcomes`
  WHERE d >= @from_date
  GROUP BY d
),
recording_started AS (
  -- The earliest round ever recorded. An issue BEFORE this instant is `ikke vurdert` because the
  -- recording did not exist yet — the expected pre-launch backlog. One at or after it can only mean
  -- a lost best-effort write or a closed gate, and the page counts those separately: without the
  -- split, a write regression is indistinguishable from the backlog during exactly the weeks the
  -- backlog is large.
  SELECT MIN(d) AS first_recorded_day FROM `disruptions.togvarsel_rounds`
)
SELECT
  i.d,
  i.issue,
  i.p,
  i.level AS forecast_level,
  i.late,
  i.computed_at,
  -- The round record. `sent` is the POLICY decision (a push was issued), never delivery: a round
  -- can fan out to zero matched devices and still be the notification the traveller would have got.
  r.sent,
  r.level AS round_level,
  r.baseline_level,
  r.kind,
  r.reason,
  r.decided_at,
  (r.d IS NOT NULL) AS has_round,
  rs.first_recorded_day,
  o.am_level,
  o.am_trouble10,
  o.amrest_level,
  o.amrest_trouble10,
  o.pm_level,
  o.pm_trouble10,
  o.am_provisional_level,
  o.pm_provisional_level,
  o.eligible,
  o.am_settled,
  o.amrest_settled,
  o.pm_settled,
  o.am_unresolvable,
  o.amrest_unresolvable,
  o.pm_unresolvable,
  o.any_unresolvable
FROM `disruptions.togvarsel_issued` AS i
LEFT JOIN `disruptions.togvarsel_rounds` AS r
  ON i.d = r.d AND i.issue = r.issue
LEFT JOIN outcomes AS o
  ON i.d = o.d
CROSS JOIN recording_started AS rs
WHERE i.d >= @from_date
ORDER BY i.d DESC, i.issue
