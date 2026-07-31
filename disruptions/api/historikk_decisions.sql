-- /historikk-api/decisions: alert decisions, newest departure first, with their verdict.
--
-- UNION of two sources, because a decision becomes visible long before it can be judged:
--   * varsel_decision_outcomes — labelled by the nightly job, carries a verdict.
--   * varsel_decision_groups   — every decision, including today's. Rows already labelled are
--                                excluded so nothing appears twice.
-- Today's alerts therefore show up on the page within a poller cycle, with verdict NULL and
-- outcome_state 'venter' rather than being invisible until tomorrow morning.
--
-- outcome_state separates the three reasons a verdict can be missing, which the UI must not conflate:
--   'venter'  — too recent to label (the call has not run, or last_recorded has not caught up).
--   'ukjent'  — no realized call was found, or the decision carries no journey key to join on.
--               Permanent: last_recorded's rail slice runs 10-17 % thin, so some calls simply never
--               resolve, and a decision with a NULL jkey is excluded from labelling altogether
--               (label_varsel_decisions.sql). Counting either as a correct rejection is exactly the
--               inflation this column exists to prevent.
--
-- PRIVACY: this endpoint is served unauthenticated on a public host, so the traveller-identifying
-- triple (journey_digest, from_stop, to_stop) is deliberately NOT selected here. Those columns stay
-- in the access-controlled tables, where the decision log needs them to join and dedupe. Published
-- together with departure_time and n_devices they would say "somebody commutes A->B at 07:15",
-- which is precisely what llm_message.key_digest and VARSEL_LLM_LOG_PROMPTS=false already keep out
-- of the logs. The page identifies a decision by its train, not by whose trip it was.
--
-- Cost: the outcomes side prunes on its call_date partition. The groups side does NOT:
-- varsel_decisions is partitioned on DATE(decided_at) and the view aggregates before any filter
-- can reach it, so `call_date BETWEEN` is a post-aggregation filter over a full scan. Acceptable
-- while the log is small (a handful of rows per poller cycle), and the reason the response is
-- cached. If it grows, give the view a decided_at bound rather than widening this comment.
SELECT
  o.situation_number, o.summary, o.description,
  o.operator, o.line, o.mode, o.call_date, o.departure_time,
  o.n_decisions, o.first_decided_at, o.last_decided_at,
  o.ever_alerted, o.first_alert_at, o.alert_lead_min,
  o.max_impact_probability, o.final_impact_probability, o.final_cancel_probability,
  o.final_delay_p50_min, o.final_delay_p90_min, o.alert_threshold,
  o.pushed_any, o.n_devices, o.message, o.message_source,
  o.actual_cancelled, o.actual_dep_delay_min, o.actual_trouble, o.verdict,
  -- An unresolved row is only permanently unknown once it has aged out of the labelling job's
  -- 10-day retry window; inside it, the nightly run deletes and re-labels it, so a late ET row can
  -- still resolve it. Calling that 'ukjent' would report a retry in progress as a dead end.
  CASE
    WHEN o.actual_trouble IS NOT NULL THEN 'avklart'
    WHEN o.call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY) THEN 'venter'
    ELSE 'ukjent'
  END AS outcome_state
FROM `disruptions.varsel_decision_outcomes` AS o
WHERE o.call_date BETWEEN @from_date AND @to_date
  AND (@operator IS NULL OR o.operator = @operator)
  AND (@verdict  IS NULL OR o.verdict  = @verdict)
  AND (NOT @alerted_only OR o.ever_alerted)

UNION ALL

SELECT
  g.situation_number, g.summary, g.description,
  g.operator, g.line, g.mode, g.call_date, g.departure_time,
  g.n_decisions, g.first_decided_at, g.last_decided_at,
  g.ever_alerted, g.first_alert_at,
  TIMESTAMP_DIFF(g.departure_time, g.first_alert_at, MINUTE) AS alert_lead_min,
  g.max_impact_probability, g.final_impact_probability, g.final_cancel_probability,
  g.final_delay_p50_min, g.final_delay_p90_min, g.alert_threshold,
  g.pushed_any, g.n_devices, g.message, g.message_source,
  CAST(NULL AS BOOL)   AS actual_cancelled,
  CAST(NULL AS INT64)  AS actual_dep_delay_min,
  CAST(NULL AS BOOL)   AS actual_trouble,
  CAST(NULL AS STRING) AS verdict,
  -- A decision with no journey key can never be joined to a realized call, so the labelling job
  -- skips it permanently. Reporting it as 'venter' would promise a verdict that is never coming.
  IF(g.jkey IS NULL, 'ukjent', 'venter') AS outcome_state
FROM `disruptions.varsel_decision_groups` AS g
WHERE g.call_date BETWEEN @from_date AND @to_date
  AND (@operator IS NULL OR g.operator = @operator)
  -- Unlabelled rows carry no verdict, so a verdict filter must exclude them all rather than
  -- silently returning them as if they matched.
  AND @verdict IS NULL
  AND (NOT @alerted_only OR g.ever_alerted)
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

ORDER BY departure_time DESC
LIMIT @limit
