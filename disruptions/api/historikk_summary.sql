-- /historikk-api/summary: how the alerting has actually performed over a window.
--
-- Counts are over LABELLED decisions only. `unknown` and `pending` are reported alongside rather
-- than folded into a verdict bucket, so a reader can see how much of the window the numbers
-- actually cover — a precision figure computed over 20 % of the decisions is a different claim
-- from one computed over 95 %, and the page must be able to say which it is showing.
--
-- precision = hit / (hit + false_alarm)   — of the alerts we sent, how many were right.
-- recall    = hit / (hit + miss)          — of the trouble we could see, how much we caught.
--
-- What recall here does NOT mean: true recall over all disruptions. The models only score calls an
-- SX message flagged, and only on journeys somebody registered, so trouble that never produced an
-- SX message is invisible to this table. It is recall among SX-flagged calls on registered
-- journeys. The UI states this next to the number.
WITH labelled AS (
  SELECT
    verdict, alert_lead_min, actual_trouble,
    -- Same split as historikk_decisions.sql: an unresolved row inside the labelling job's 10-day
    -- retry window is still waiting, not permanently unknown. Keeping the two in step matters --
    -- the page shows this count next to the per-row state, and they must tell the same story.
    actual_trouble IS NULL
      AND call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY) AS still_retrying
  FROM `disruptions.varsel_decision_outcomes`
  WHERE call_date BETWEEN @from_date AND @to_date
    AND (@operator IS NULL OR operator = @operator)
),
-- Unlabelled groups, split by whether a verdict is still possible. A decision with no journey key
-- can never be joined to a realized call -- the labelling job skips it permanently -- so it is
-- already unknown, not merely waiting. Counting it as pending would understate how much of the
-- window can never be resolved.
unlabelled AS (
  SELECT COUNTIF(g.jkey IS NOT NULL) AS n_pending,
         COUNTIF(g.jkey IS NULL)     AS n_unjoinable
  FROM `disruptions.varsel_decision_groups` AS g
  WHERE g.call_date BETWEEN @from_date AND @to_date
    AND (@operator IS NULL OR g.operator = @operator)
    AND NOT EXISTS (
      SELECT 1
      FROM `disruptions.varsel_decision_outcomes` AS o
      WHERE o.journey_digest   = g.journey_digest
        AND o.operator         = g.operator
        AND o.jkey             = g.jkey
        AND o.call_date        = g.call_date
        AND o.stop_order       = g.stop_order
        AND o.situation_number = g.situation_number
    )
)
SELECT
  COUNTIF(verdict = 'hit')            AS n_hit,
  COUNTIF(verdict = 'false_alarm')    AS n_false_alarm,
  COUNTIF(verdict = 'miss')           AS n_miss,
  COUNTIF(verdict = 'correct_reject') AS n_correct_reject,
  -- Permanently unknown: labelled rows past the retry window, plus unlabelled rows with no join key.
  COUNTIF(verdict IS NULL AND NOT still_retrying)
    + (SELECT n_unjoinable FROM unlabelled) AS n_unknown,
  -- Still resolvable: never labelled yet, plus labelled-but-unresolved inside the retry window.
  COUNTIF(verdict IS NULL AND still_retrying)
    + (SELECT n_pending FROM unlabelled)    AS n_pending,
  SAFE_DIVIDE(
    COUNTIF(verdict = 'hit'),
    COUNTIF(verdict = 'hit') + COUNTIF(verdict = 'false_alarm')
  ) AS precision,
  SAFE_DIVIDE(
    COUNTIF(verdict = 'hit'),
    COUNTIF(verdict = 'hit') + COUNTIF(verdict = 'miss')
  ) AS recall,
  -- Lead time on the alerts that were right: how much warning a traveller who needed it actually
  -- got. Median rather than mean — a single alert fired days ahead would otherwise flatter it.
  -- IGNORE NULLS goes after the quantile count, not after the expression: BigQuery's signature is
  -- APPROX_QUANTILES(expression, number [{IGNORE|RESPECT} NULLS]), and the other order does not
  -- parse -- which would have made this whole endpoint a permanent 503.
  APPROX_QUANTILES(IF(verdict = 'hit', alert_lead_min, NULL), 2 IGNORE NULLS)[SAFE_OFFSET(1)]
    AS median_lead_min_on_hits,
  COUNTIF(verdict = 'hit' AND alert_lead_min >= 30) AS n_hit_with_30min_notice
FROM labelled
