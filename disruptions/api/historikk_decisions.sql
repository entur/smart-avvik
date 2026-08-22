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
-- PRIVACY: this endpoint is served unauthenticated on a public host, and the per-row redaction of
-- the traveller's endpoints is GONE as of 2026-08-22 -- from_stop and to_stop are selected here and
-- the API resolves them to NSR names, so a card now says "Oslo S -> Lillestrom stasjon" beside
-- departure_time and n_devices. That is the "somebody commutes A->B at 07:15" this file spent two
-- paragraphs refusing to publish, so read those paragraphs as history, not as a live guarantee.
--
-- Three things make it a smaller step than the deletion looks. The 2026-08-21 filter change already
-- made the pair CHOOSABLE -- historikk_filters.sql publishes the vocabulary of registered endpoints
-- and the matching departures could be read off a narrowed page -- so what goes now is the work of
-- narrowing, not the possibility. /meldinger already publishes exactly this, decided the same
-- way and for the same reason (its own header carries the k-anonymity measurement: 21 of 31 prd
-- pairs isolate a single registered journey). And prd's registrations are TEST USERS rather than
-- members of the public -- there is no real traveller behind a k=1 pair -- which is the whole of
-- what makes this acceptable. Team decision, 2026-08-22.
--
-- So the condition to re-check is not a threshold but a fact: IF /varsel EVER TAKES REGISTRATIONS
-- FROM THE PUBLIC, this projection publishes real commutes and must be reconsidered. The fix then
-- is access control on /historikk, not a smaller SELECT list -- the exposure would also be in
-- /meldinger's generated prose, which no projection controls. journey_digest stays absent from
-- both surfaces regardless: it is the join key to the LLM audit log, and no page needs it.
--
-- Cost: the outcomes side prunes on its call_date partition. The groups side does NOT:
-- varsel_decisions is partitioned on DATE(decided_at) and the view aggregates before any filter
-- can reach it, so `call_date BETWEEN` is a post-aggregation filter over a full scan. Acceptable
-- while the log is small (a handful of rows per poller cycle), and the reason the response is
-- cached. If it grows, give the view a decided_at bound rather than widening this comment.
SELECT
  o.situation_number, o.summary, o.description,
  o.operator, o.line, o.mode, o.from_stop, o.to_stop, o.call_date, o.departure_time,
  o.n_decisions, o.first_decided_at, o.last_decided_at,
  o.ever_alerted, o.first_alert_at, o.alert_lead_min,
  o.max_impact_probability, o.final_impact_probability, o.final_cancel_probability,
  o.final_delay_p50_min, o.final_delay_p90_min, o.alert_threshold, o.et_cancelled,
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
  AND (@line     IS NULL OR o.line     = @line)
  -- The traveller's own registered endpoints. Both filtered on AND selected since 2026-08-22 (see
  -- the PRIVACY note at the top); the refs go no further than this process, which pops them and
  -- serves resolved NSR names in their place, so extra="forbid" still guards the ref itself.
  -- NULL from_stop/to_stop rows are dropped by an active filter, which is right -- a decision that
  -- records no origin cannot be claimed to have started anywhere in particular.
  --
  -- An ARRAY of EVERY ref standing for the chosen place, not the one the dropdown nominated, and
  -- the difference is not cosmetic. A station's rail and bus sides are separate NSR refs and each
  -- feed records its calls on its own, so a registration made against either one lands here under
  -- that ref: matching a single ref hid 659 of Asker stasjon's 842 decisions behind the row that
  -- said "Asker stasjon" (measured on dev, 2026-08-21). This is varsel.place_refs' lesson one layer
  -- up -- collapsing refs into one row and then matching only the survivor moves the silent miss
  -- down a layer instead of removing it.
  --
  -- Empty means "no filter", since BigQuery has no NULL array parameter.
  -- IFNULL, and it is load-bearing: an EMPTY array parameter from the Python client serializes as
  -- {'arrayValues': []}, and BigQuery evaluates ARRAY_LENGTH of that as NULL rather than 0 (measured
  -- 2026-08-21). So the bare `ARRAY_LENGTH(@refs) = 0` this shipped with was NULL on every
  -- unfiltered request, the predicate excluded every row, and the whole page went to zeros -- while
  -- a dry run validated and `bq --parameter=...:ARRAY<STRING>:[]` returned 0, because the CLI
  -- encodes an empty array differently from the client library. No test caught it: nothing in this
  -- repo executes SQL.
  AND (IFNULL(ARRAY_LENGTH(@from_place_refs), 0) = 0 OR o.from_stop IN UNNEST(@from_place_refs))
  AND (IFNULL(ARRAY_LENGTH(@to_place_refs), 0) = 0 OR o.to_stop   IN UNNEST(@to_place_refs))
  AND (NOT @alerted_only OR o.ever_alerted)
  -- "With ground truth" is the OUTCOME being known, which is what outcome_state calls 'avklart' --
  -- deliberately not `verdict IS NOT NULL`. The two differ by one row shape: an outcome that
  -- resolved while `ever_alerted` was NULL is judged by nobody (label_varsel_decisions.sql refuses
  -- to guess) yet plainly has a realized call behind it. Filtering on the verdict would leave those
  -- reachable from no filter setting at all, which is how a population disappears.
  AND (NOT @resolved_only OR o.actual_trouble IS NOT NULL)
  -- "Only what an operator published about" -- cut on the NAMESPACE, not on a list of the synthetic
  -- kinds. Every situation number this codebase mints starts with `KIX:` precisely to stay clear of
  -- the publisher namespaces (`ENT:`, `NSB:`, ...), so this one predicate covers today's two
  -- (`KIX:NoSituation:` scored from real-time data, `KIX:ETCancellation:` relayed from the feed)
  -- and whatever a third turns out to be. Enumerating the prefixes instead would leave a future
  -- kind quietly included in a population the checkbox promises has operator text behind it.
  AND (NOT @sx_only OR NOT STARTS_WITH(o.situation_number, 'KIX:'))

UNION ALL

SELECT
  g.situation_number, g.summary, g.description,
  g.operator, g.line, g.mode, g.from_stop, g.to_stop, g.call_date, g.departure_time,
  g.n_decisions, g.first_decided_at, g.last_decided_at,
  g.ever_alerted, g.first_alert_at,
  TIMESTAMP_DIFF(g.departure_time, g.first_alert_at, MINUTE) AS alert_lead_min,
  g.max_impact_probability, g.final_impact_probability, g.final_cancel_probability,
  g.final_delay_p50_min, g.final_delay_p90_min, g.alert_threshold, g.et_cancelled,
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
  AND (@line     IS NULL OR g.line     = @line)
  -- Same three as the labelled side; see the note there for why they are filtered but not selected,
  -- and why a place is an array of refs rather than one.
  AND (IFNULL(ARRAY_LENGTH(@from_place_refs), 0) = 0 OR g.from_stop IN UNNEST(@from_place_refs))
  AND (IFNULL(ARRAY_LENGTH(@to_place_refs), 0) = 0 OR g.to_stop   IN UNNEST(@to_place_refs))
  -- Unlabelled rows carry no verdict, so a verdict filter must exclude them all rather than
  -- silently returning them as if they matched.
  AND @verdict IS NULL
  AND (NOT @alerted_only OR g.ever_alerted)
  -- Same reasoning one step further: every row on this side is unlabelled by construction — that is
  -- what makes it this side of the UNION — so none of them has ground truth yet.
  AND NOT @resolved_only
  -- Same cut as the labelled side; see the note there for why it is the namespace and not a list.
  AND (NOT @sx_only OR NOT STARTS_WITH(g.situation_number, 'KIX:'))
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
