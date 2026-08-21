-- /tilbakeblikk-api/decisions: the alert decisions taken inside one day-and-time window.
--
-- A near-clone of historikk_decisions.sql on a DIFFERENT WINDOW AXIS, and the axis is the whole
-- point. /historikk windows on `call_date` -- the operating day of the train -- and answers "what
-- did we decide about the trains that ran that day". /tilbakeblikk windows on `decided_at` -- when
-- the decision was taken -- and answers "what were we saying between 07:00 and 09:00 this morning".
-- The second question is what a prompt-iteration surface needs: a stretch of real cycles whose
-- recorded inputs can be replayed through today's prompt.
--
-- THE FORK IS DELIBERATE AND HAS A COST. /historikk is not modified (the plan's scope boundary), so
-- this file, tilbakeblikk_summary.sql, tilbakeblikk_filters.sql and tilbakeblikk_messages.sql are a
-- second copy of that logic on a second axis: a fix to one side must be mirrored to the other. The
-- 2026-08-21 pushed-wording fix is exactly the shape of change that has to be. Only
-- historikk_stop_places.sql is genuinely shared, because it takes only @refs and has no window.
--
-- WHY THIS READS `varsel_decisions` AND NOT `varsel_decision_groups` (KTD4): the view carries
-- neither `message_input` nor any `decided_at` bound, and it aggregates before a filter can reach
-- it. Reading the table directly is the only way to get the replay input, and it is also the reason
-- the pruning below is available at all.
--
-- COST: `varsel_decisions` is PARTITION BY DATE(decided_at), so this is the first query in the
-- stack that actually prunes its main input. The partition column is a UTC date while the window is
-- an Oslo day, so the pruning predicate spans @day-1 to @day+1 and the exact window predicate
-- (@from_ts <= decided_at < @to_ts, both resolved from the Oslo day and the quantized times in
-- tilbakeblikk._resolve_window) is applied on top. Three partitions instead of one whole table.
--
-- THE OUTCOMES SIDE IS BOUNDED TOO, and it has to be bounded SEPARATELY: that table is
-- PARTITION BY call_date, which a `decided_at` predicate does not prune, so without an explicit
-- `call_date BETWEEN` this read would be strictly MORE expensive than /historikk's -- which does
-- prune it -- while the query overall claims to be cheaper.
--
-- The width is MEASURED, not reasoned to. Run in dev on 2026-08-21:
--
--   SELECT MAX(ABS(DATE_DIFF(call_date, DATE(decided_at, 'Europe/Oslo'), DAY)))
--   FROM `disruptions.varsel_decisions`
--   WHERE DATE(decided_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
--
-- -> max_days = 1 over 59 694 rows, p99.9 = 1, and no NULL call_date. So the bound is +/-1 day.
-- That agrees with the structure: a situation is scored over a ~12 h forward window, so the furthest
-- a call can sit from the Oslo day it was decided on is the following day. RE-MEASURE before
-- widening the scoring horizon -- a too-narrow bound drops outcome rows SILENTLY (the subjects just
-- read as unresolved, which is a state the page renders perfectly happily), and the byte counts
-- validate_varsel_sql.sh prints would report that loss as a CHEAPER query, i.e. as success. Cost is
-- not a correctness check here.
--
-- PRIVACY (KTD10, inherited verbatim from /historikk): the traveller-identifying triple
-- (journey_digest, from_stop, to_stop) is never projected. journey_digest and the two stop refs are
-- used as JOIN and ORDER BY and WHERE terms only. `message_input` is the fourth redaction and the
-- one specific to this surface: it embeds origin, destination and departure time in one blob.
--
-- Note what that means for the one reference to `message_input` below. It appears in a PREDICATE,
-- never in a projection, and it must: the replay cycle this file names in `would_send_at` has to be
-- the same cycle tilbakeblikk_messages.sql fetches the input for, or the card would state a send
-- time for a wording the page cannot produce. The redaction that matters is the projection, and
-- test_tilbakeblikk.py asserts exactly that -- SELECT lists of this file and the summary carry no
-- message_input, from_stop or to_stop, while the messages file does carry the input.
WITH scanned AS (
  -- Every decision cycle in the +/-1 day span, flagged for whether it is inside the requested
  -- window. One scan serves both: the window-scoped card figures come from the flagged rows, and
  -- `first_in_window` needs the span-wide view of the same subject (see below).
  SELECT
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    summary, description, line, mode, stop_place, departure_time,
    decided_at, alertworthy, et_cancelled, impact_probability, cancel_probability,
    delay_p50_min, delay_p90_min, alert_threshold, pushed, n_devices, prompt_version,
    -- Predicate only; see the header note. TRUE when this cycle recorded an input that can be
    -- replayed through today's prompt.
    message_input IS NOT NULL AS has_input,
    -- Half-open [from, to). The endpoint quantizes both ends to 15-minute steps -- from down, to up
    -- -- so a narrowed range never drops a minute the caller asked for, and the whole-day default is
    -- midnight to the FOLLOWING midnight rather than 23:59:59.999999, which is not on the grid.
    decided_at >= @from_ts AND decided_at < @to_ts AS in_window
  FROM `disruptions.varsel_decisions`
  WHERE DATE(decided_at) BETWEEN DATE_SUB(@day, INTERVAL 1 DAY) AND DATE_ADD(@day, INTERVAL 1 DAY)
    AND (@operator IS NULL OR operator = @operator)
    AND (@line     IS NULL OR line     = @line)
    -- The traveller's own registered endpoints: filtered on, never selected. An ARRAY of EVERY ref
    -- standing for the chosen place, not the one the dropdown nominated -- a station's rail and bus
    -- sides are separate NSR refs and each feed records its calls on its own, so matching a single
    -- ref hid 659 of Asker stasjon's 842 decisions on /historikk (measured on dev, 2026-08-21).
    --
    -- IFNULL, and it is load-bearing: an EMPTY array parameter from the Python client serializes as
    -- {'arrayValues': []} and BigQuery evaluates ARRAY_LENGTH of that as NULL rather than 0, so the
    -- bare `ARRAY_LENGTH(@refs) = 0` is NULL on every unfiltered request, the ANDed predicate drops
    -- every row, and the page goes to zeros. It shipped that way on /historikk for one commit. A dry
    -- run validates it and `bq --parameter=refs:ARRAY<STRING>:[]` returns 0, because the CLI encodes
    -- an empty array differently from the client library, so neither cheap check catches it.
    AND (IFNULL(ARRAY_LENGTH(@from_place_refs), 0) = 0 OR from_stop IN UNNEST(@from_place_refs))
    AND (IFNULL(ARRAY_LENGTH(@to_place_refs),   0) = 0 OR to_stop   IN UNNEST(@to_place_refs))
    -- "Only what an operator published about" -- cut on the NAMESPACE, not on a list of the
    -- synthetic kinds, so a future `KIX:` kind is covered without an edit here.
    AND (NOT @sx_only OR NOT STARTS_WITH(situation_number, 'KIX:'))
),
-- One row per decision SUBJECT, at /historikk's grain (KTD5) -- the grain varsel_decision_groups
-- groups at and the nightly labelling job writes a verdict for. Keeping it means the cards and the
-- rates stay comparable with /historikk's; a per-cycle grain would multiply the cards roughly
-- tenfold and describe a different population from the one /summary reports on.
subjects AS (
  SELECT
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    ANY_VALUE(summary)     AS summary,
    ANY_VALUE(description) AS description,
    ANY_VALUE(line)        AS line,
    ANY_VALUE(mode)        AS mode,
    ANY_VALUE(stop_place)  AS stop_place,
    -- MAX rather than ANY_VALUE, and only here: this column decides the ORDER BY, and
    -- tilbakeblikk_messages.sql has to resolve the SAME ordered list so the two responses align
    -- position by position. ANY_VALUE is free to pick differently in two queries over the same rows.
    MAX(departure_time)    AS departure_time,

    -- ---- WINDOW-SCOPED per-cycle figures --------------------------------------------------------
    -- Everything below is computed over the cycles INSIDE the window, while the verdict further
    -- down comes whole from the nightly job. That is deliberate (KTD5) and it is visible on the
    -- page: a subject can legitimately read ever_alerted = FALSE beside verdict = 'hit', because it
    -- alerted outside the window the reader chose. The API's field descriptions say so, and the UI
    -- labels these figures as window-scoped rather than letting them read as /historikk's.
    COUNTIF(in_window)                              AS n_decisions,
    MIN(IF(in_window, decided_at, NULL))            AS first_decided_at,
    MAX(IF(in_window, decided_at, NULL))            AS last_decided_at,
    LOGICAL_OR(in_window AND alertworthy)           AS ever_alerted,
    MIN(IF(in_window AND alertworthy, decided_at, NULL)) AS first_alert_at,
    MAX(IF(in_window, impact_probability, NULL))    AS max_impact_probability,
    ANY_VALUE(IF(in_window, alert_threshold, NULL)) AS alert_threshold,
    LOGICAL_OR(in_window AND pushed)                AS pushed_any,
    MAX(IF(in_window, n_devices, NULL))             AS n_devices,
    -- The four "final" figures in ONE aggregate, not four. Two aggregates "picking the same row" is
    -- a promise ORDER BY cannot keep, and its failure mode here would be a card mixing one cycle's
    -- probability with another cycle's delay. (varsel_decision_groups still spells these as four
    -- separate ARRAY_AGGs; that is the older form, and the `delivered` aggregate beside them is the
    -- one this copies.) The STRUCT is NULL for an out-of-window row and IGNORE NULLS drops it, so
    -- this is "the last IN-WINDOW cycle" -- including when that cycle's own figures are NULL.
    ARRAY_AGG(IF(in_window, STRUCT(
                impact_probability AS final_impact_probability,
                cancel_probability AS final_cancel_probability,
                delay_p50_min      AS final_delay_p50_min,
                delay_p90_min      AS final_delay_p90_min), NULL)
              IGNORE NULLS ORDER BY decided_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS final,
    -- Was this a relayed fact or a prediction, as at the point a verdict would judge? Same rule as
    -- varsel_decision_groups: the first ALERTING cycle where we alerted, any cycle where we did not.
    -- Emphatically not a plain LOGICAL_OR, which would be backwards -- a cancellation is routinely
    -- declared AFTER we alerted, since alerting before ET publishes is the whole point of the app.
    -- Used only as the fallback below, for a subject the nightly job has not labelled yet.
    COALESCE(
      ARRAY_AGG(IF(in_window AND alertworthy, et_cancelled, NULL)
                IGNORE NULLS ORDER BY decided_at LIMIT 1)[SAFE_OFFSET(0)],
      LOGICAL_OR(IF(in_window, et_cancelled, NULL))
    ) AS et_cancelled_window,

    -- ---- THE REPLAY PICK (KTD5) -----------------------------------------------------------------
    -- The FIRST alert-worthy cycle in the window that recorded an input: the moment a message could
    -- first have gone out, which is what "Varsel kl. HH:MM" means on a surface where nothing was
    -- sent. Not the LAST cycle -- the 2026-08-21 fix measured those as a different row for 41 % of
    -- /historikk's alerted subjects.
    --
    -- ONE aggregate carrying its fields together, for the same reason as `final` above and the
    -- `delivered` aggregate in create_varsel_decisions.sql: `would_send_at` and `prompt_version`
    -- describe one cycle, and the messages file resolves `message_input` from the same expression.
    -- Split into two aggregates they could name two different cycles, and the symptom would be a
    -- card quoting one cycle's wording under another cycle's time -- exactly the defect the view was
    -- fixed for.
    --
    -- The `has_input` guard is what makes this "the first replayable cycle" rather than "the first
    -- alert-worthy cycle, possibly with nothing to replay": a subject none of whose alerting cycles
    -- recorded an input has NO replayable cycle at all, which the API reports as its own state (R7)
    -- instead of as "no message". message_input is NULL on rows written before 2026-08-05 and on
    -- rows where the builder failed; both are that same third bucket, and neither is a default.
    ARRAY_AGG(IF(in_window AND alertworthy AND has_input, STRUCT(
                decided_at     AS would_send_at,
                prompt_version AS prompt_version), NULL)
              IGNORE NULLS ORDER BY decided_at LIMIT 1)[SAFE_OFFSET(0)] AS replay,

    -- The subject's earliest alert-worthy cycle over the WHOLE SPAN, not just the window. This is
    -- what keeps "Varsel kl." from claiming a first opportunity the window cannot support: for a
    -- subject that was already alerting before the window opened, the pick above is merely the
    -- earliest cycle the window happens to contain, and narrowing the picker would silently move the
    -- claimed send time LATER. It costs no extra partition -- the span is already scanned.
    --
    -- What bounds the claim: the span reaches back at least 24 h before the window opens, against a
    -- scoring horizon of ~12 h, so a subject's cycles cannot begin before it in practice. If the
    -- horizon ever grows past a day, this comparison starts reading TRUE where it should read FALSE.
    MIN(IF(alertworthy, decided_at, NULL)) AS span_first_alert_at
  FROM scanned
  GROUP BY journey_digest, operator, jkey, call_date, stop_order, situation_number
  -- A subject is on the page because it was DECIDED in the window. The span-wide rows above exist
  -- only to qualify that; they never put a subject on the page by themselves.
  HAVING COUNTIF(in_window) > 0
)
SELECT
  s.situation_number, s.summary, s.description,
  s.operator, s.line, s.mode, s.call_date, s.departure_time,
  s.n_decisions, s.first_decided_at, s.last_decided_at,
  s.ever_alerted, s.first_alert_at,
  -- Computed from the WINDOW's first alert, deliberately not copied from o.alert_lead_min. The
  -- outcomes column describes the subject's whole life, so serving it beside a window-scoped
  -- first_alert_at would put two figures on one card that disagree about which cycle they mean.
  TIMESTAMP_DIFF(s.departure_time, s.first_alert_at, MINUTE) AS alert_lead_min,
  s.max_impact_probability,
  s.final.final_impact_probability,
  s.final.final_cancel_probability,
  s.final.final_delay_p50_min,
  s.final.final_delay_p90_min,
  s.alert_threshold,
  -- Provenance, from the nightly job where it has run and from the window otherwise -- the same two
  -- arms /historikk serves it from, in one expression because this query is a join rather than a
  -- UNION. Taking it from the outcomes row where one exists is what keeps a card's tag agreeing with
  -- the segment /summary counted it in; that file reads the same column.
  COALESCE(o.et_cancelled, s.et_cancelled_window) AS et_cancelled,
  s.pushed_any, s.n_devices,

  -- ---- the replay, projected -------------------------------------------------------------------
  s.replay.would_send_at,
  -- TRUE only when the replayed cycle IS the subject's first alert-worthy cycle anywhere in the
  -- span. FALSE covers two unlike-looking cases that the UI labels differently from a first
  -- opportunity: the subject was already alerting before the window opened, and the subject had no
  -- replayable cycle at all. COALESCE so this is a BOOL and never NULL -- the API types it strictly.
  --
  -- Note the comparison is against the earliest alert-worthy cycle FULL STOP, not the earliest one
  -- that recorded an input. A subject whose very first alerting cycle wrote no input therefore reads
  -- FALSE, which is the conservative direction: it declines to claim a first opportunity.
  COALESCE(s.replay.would_send_at = s.span_first_alert_at, FALSE) AS first_in_window,
  -- The prompt in force when the ROW was written, against llm_message.PROMPT_VERSION today. The page
  -- marks the difference; it does not claim to separate a prompt change from sampling variance,
  -- since each wording is a single draw from a non-deterministic decoder.
  s.replay.prompt_version,

  o.actual_cancelled, o.actual_dep_delay_min, o.actual_trouble, o.verdict,
  -- Three reasons a verdict can be missing, which the UI must not conflate. An unresolved row is
  -- only permanently unknown once it has aged out of the labelling job's 10-day retry window; inside
  -- it, the nightly run deletes and re-labels it, so a late ET row can still resolve it. And a
  -- decision with no journey key can never be joined to a realized call at all -- the labelling job
  -- skips it permanently -- so reporting it as 'venter' would promise a verdict that is never
  -- coming. NULL is never 'correct_reject': last_recorded's rail slice runs 10-17 % thin, and
  -- counting an unmatched join as "nothing went wrong" manufactures precision out of missing data.
  --
  -- A same-day window -- this page's default -- is almost entirely 'venter', because verdicts arrive
  -- from the 04:00 UTC job the following night. /summary reports the split beside the rates for
  -- exactly that reason.
  CASE
    WHEN o.actual_trouble IS NOT NULL THEN 'avklart'
    WHEN s.jkey IS NULL               THEN 'ukjent'
    WHEN s.call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY) THEN 'venter'
    ELSE 'ukjent'
  END AS outcome_state
FROM subjects AS s
LEFT JOIN `disruptions.varsel_decision_outcomes` AS o
  -- The partition bound FIRST, and inside the ON rather than in the WHERE: in the WHERE it would
  -- turn this into an inner join and drop every unlabelled subject -- which on a same-day window is
  -- nearly all of them. See the header for how the +/-1 day was measured.
  ON  o.call_date BETWEEN DATE_SUB(@day, INTERVAL 1 DAY) AND DATE_ADD(@day, INTERVAL 1 DAY)
  -- The subject key, plain equality. A NULL jkey therefore never matches, which is correct: the
  -- labelling job excludes those decisions entirely, and the CASE above reports them as 'ukjent'.
  -- One row per subject on the outcomes side by construction (label_varsel_decisions.sql deletes
  -- and re-inserts at this grain), so this cannot fan out.
  AND o.journey_digest   = s.journey_digest
  AND o.operator         = s.operator
  AND o.jkey             = s.jkey
  AND o.call_date        = s.call_date
  AND o.stop_order       = s.stop_order
  AND o.situation_number = s.situation_number
-- Verdict narrows to LABELLED subjects by construction: an unlabelled subject has a NULL o.verdict,
-- so the equality is never TRUE for it.
WHERE (@verdict IS NULL OR o.verdict = @verdict)
  AND (NOT @alerted_only OR s.ever_alerted)
  -- "With ground truth" is the OUTCOME being known, deliberately not `verdict IS NOT NULL`: an
  -- outcome that resolved while ever_alerted was NULL is judged by nobody yet plainly has a realized
  -- call behind it, and filtering on the verdict would leave those reachable from no filter setting.
  AND (NOT @resolved_only OR o.actual_trouble IS NOT NULL)
-- Fully deterministic, and it has to be: tilbakeblikk_messages.sql repeats this ORDER BY verbatim so
-- the two responses line up position by position, and the page keys its list on position because
-- nothing served identifies a decision uniquely (the real grain includes journey_digest, which is
-- redacted). departure_time alone leaves ties -- two registrations on the same train and the same
-- situation differ only in journey_digest -- so the whole subject key breaks them. Ordering by a
-- redacted column is not projecting it.
ORDER BY s.departure_time DESC,
         s.situation_number, s.operator, s.jkey, s.call_date, s.stop_order, s.journey_digest
LIMIT @limit
