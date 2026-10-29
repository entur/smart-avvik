-- /meldinger-api/decisions: the alert decisions covering one day-and-time window of DEPARTURES.
--
-- A near-clone of historikk_decisions.sql on a THIRD window axis, and the axis is the whole point.
-- /historikk windows on `call_date` -- the operating day of the train -- and answers "what did we
-- decide about the trains that ran that day". /meldinger windows on `departure_time` -- the
-- aimed departure of the boarding call -- and answers "what would we have sent out for the trains
-- leaving between 07:00 and 09:00, worded by today's prompt".
--
-- WHY NOT `decided_at`, which this file used until 2026-08-22. That axis selects CYCLES, and the
-- page's unit is a MESSAGE. Two consequences made it the wrong axis:
--
--   * A subject's cycles straddle the window. Selecting 07:00-09:00 of decision time catches the
--     second-through-Nth cycle about a train first decided at 06:40, so every per-cycle figure had
--     to be computed over the window's slice of the subject and labelled WINDOW-SCOPED on the card,
--     and `first_in_window` had to exist to stop "Varsel kl." claiming a first opportunity that was
--     merely the earliest cycle the reader happened to select. On the departure axis a subject is
--     wholly in or wholly out, so all of that is gone: every figure below is whole-subject and
--     agrees with the nightly job's by construction rather than by a recomputation that had to be
--     kept from disagreeing with it.
--   * It cannot answer "what would we have sent for THIS departure", which is the question. A
--     departure has one send moment; a cycle window has to pick one, and the pick was a rule the
--     reader could not see.
--
-- WHAT THE OLD AXIS COULD DO AND THIS ONE CANNOT, stated because it is a real loss: only a
-- `decided_at` window reconstructs "what the app was saying at 07:15" as one coherent snapshot, and
-- only it let the reader sweep LEAD TIME by moving the window (the same train's 2-hour-lead cycle
-- and its 13-minute-lead cycle carry very different inputs and produce very different wordings).
-- Here the replayed cycle is always the send moment, which is what the page now claims to show.
--
-- THE FORK FROM /historikk IS DELIBERATE AND HAS A COST. /historikk is not modified (the plan's
-- scope boundary), so this file, meldinger_summary.sql, meldinger_filters.sql and
-- meldinger_messages.sql are a second copy of that logic on a second axis: a fix to one side must
-- be mirrored to the other. The 2026-08-21 pushed-wording fix is exactly the shape of change that
-- has to be. Only historikk_stop_places.sql is genuinely shared, because it takes only @refs and
-- has no window of its own.
--
-- WHY THIS READS `varsel_decisions` AND NOT `varsel_decision_groups` (KTD4): the view carries
-- neither `message_input` nor any date bound, and it aggregates before a filter can reach it.
-- Reading the table directly is the only way to get the replay input, and it is also the reason the
-- pruning below is available at all.
--
-- COST, AND A TRAP THAT GOT SHARPER RATHER THAN SOFTER. `varsel_decisions` is
-- PARTITION BY DATE(decided_at), which the window predicate no longer touches -- so the pruning
-- predicate is now a DERIVED bound: every cycle that decided about a departure inside the window.
--
-- Under the old axis a too-narrow span only mislabelled `first_in_window`. Now it DROPS CYCLES FROM
-- THE AGGREGATES -- `n_decisions` undercounts, `first_alert_at` moves later, `alert_lead_min`
-- shrinks, and a push whose cycle fell outside the span reads as "never sent". Every one of those
-- is silent, and the byte counts validate_varsel_sql.sh prints would report the loss as a CHEAPER
-- query, i.e. as success. Cost is not a correctness check here.
--
-- MEASURED in dev on 2026-08-22 over the last 14 days, 117 448 rows:
--
--   SELECT MIN(DATE_DIFF(DATE(departure_time,'Europe/Oslo'), DATE(decided_at), DAY)),   -- 0
--          MAX(DATE_DIFF(DATE(departure_time,'Europe/Oslo'), DATE(decided_at), DAY)),   -- 1
--          MIN(TIMESTAMP_DIFF(departure_time, decided_at, MINUTE)),                     -- -54
--          MAX(TIMESTAMP_DIFF(departure_time, decided_at, MINUTE)),                     -- 710
--          COUNTIF(departure_time IS NULL)                                              -- 0
--   FROM `disruptions.varsel_decisions`
--   WHERE DATE(decided_at) >= DATE_SUB(CURRENT_DATE(), INTERVAL 14 DAY)
--
-- So DATE(decided_at) lands on @day or @day-1, and the bound below covers both. Two things in that
-- reading are worth stating because the obvious version of each is wrong.
--
-- A decision is NOT always before its departure: the worst observed lead is MINUS 54 minutes. The
-- matcher's catchable_after=now cut drops a train that can no longer be CAUGHT, which is not the
-- same as one whose aimed departure has passed -- a delayed train is still catchable. That negative
-- tail is the only thing that could ever put a cycle on @day+1 (a late-evening Oslo departure
-- decided after UTC midnight), and at -54 min it cannot: a departure inside @day is at most 22:00
-- UTC on @day, so even the worst tail stays on @day. The @day+1 reach below is therefore MARGIN, and
-- margin against a specific future -- a looser departed-train cut, or a lead past about -70 minutes
-- -- rather than decoration. It costs nothing: three partitions either way.
--
-- The 710-minute maximum is the ~12 h scoring horizon, and it is what bounds @day-1. RE-MEASURE
-- before widening that horizon: at 24 h the bound below stops covering the span.
--
-- THE OUTCOMES SIDE IS BOUNDED SEPARATELY, on `call_date`, because that table is
-- PARTITION BY call_date and a `departure_time` predicate prunes nothing there. Measured on the
-- same rows, `call_date = DATE(departure_time,'Europe/Oslo')` held for ALL 117 448 of them -- zero
-- operating-day drift, so the exact `= @day` would have been correct on this data:
--
--   SELECT COUNTIF(call_date != DATE(departure_time,'Europe/Oslo'))                     -- 0
--
-- It stays +/-1 day anyway. Fourteen days of dev, whose registrations number about three, is thin
-- evidence for a claim about after-midnight operating days across every operator, and the failure
-- mode is the silent one above: a dropped verdict just reads as an unresolved subject, which the
-- page renders perfectly happily. Tighten it only against a measurement from prd.
--
-- The bound also lives inside the ON and never the WHERE: in the WHERE it turns the LEFT JOIN into
-- an inner one and drops every unlabelled subject, which on a same-day window is nearly all of them.
--
-- A ROW WITH NO `departure_time` CANNOT BE PLACED ON THIS AXIS and therefore drops out entirely.
-- That is the honest handling -- it has no departure to window on -- and it is a population change
-- against the old axis, where such a subject appeared with a NULL lead time and sorted last. The
-- measurement above says the population is currently empty (0 NULL departure_time in 14 days), so
-- this is a contract note rather than an observed loss.
--
-- PRIVACY: journey_digest and `message_input` never leave this query. The endpoint does project the
-- two stop refs so the server can resolve human-readable route names, then removes the refs before
-- response validation. `message_input` is the remaining surface-specific redaction: it embeds
-- origin, destination and departure time
-- in one blob. It appears below in a PREDICATE only, never in a projection, and it must: the replay
-- cycle this file names in `would_send_at` has to be the same cycle meldinger_messages.sql
-- fetches the input for, or the card would state a send time for a wording the page cannot produce.
-- test_meldinger.py asserts exactly that -- the SELECT lists of this file and the summary carry
-- no message_input, from_stop or to_stop, while the messages file does carry the input.
WITH scanned AS (
  -- Every cycle of every subject whose DEPARTURE falls inside the window. No in/out flag: the axis
  -- is a property of the subject, so a subject is wholly in or wholly out and each of its cycles
  -- comes with it.
  SELECT
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    summary AS cycle_summary, description, line, mode, stop_place, from_stop, to_stop, departure_time,
    decided_at, alertworthy, et_cancelled, impact_probability, cancel_probability,
    delay_p50_min, delay_p90_min, alert_threshold, pushed, n_devices, prompt_version,
    channel, n_subscriptions,
    -- Why no push went out is a question this page will provoke on every silent card, and this is
    -- the only column that separates "this deployment does not push at all" from "we chose not to".
    message_status = 'push_disabled' AS push_off,
    -- Predicate only; see the header note. TRUE when this cycle recorded an input that can be
    -- replayed through today's prompt.
    message_input IS NOT NULL AS has_input
  FROM `disruptions.varsel_decisions`
  -- The DERIVED partition bound. See the header: this is no longer the window predicate, and a
  -- too-narrow one now drops cycles from the aggregates rather than mislabelling one flag.
  WHERE DATE(decided_at) BETWEEN DATE_SUB(@day, INTERVAL 1 DAY) AND DATE_ADD(@day, INTERVAL 1 DAY)
    -- The window itself. Half-open [from, to). The endpoint quantizes both ends to 15-minute steps
    -- -- from down, to up -- so a narrowed range never drops a minute the caller asked for, and the
    -- whole-day default is midnight to the FOLLOWING midnight rather than 23:59:59.999999, which is
    -- not on the grid. A NULL departure_time fails this and drops out, as the header says.
    AND departure_time >= @from_ts AND departure_time < @to_ts
    AND (@operator IS NULL OR operator = @operator)
    AND (@line     IS NULL OR line     = @line)
    -- The channel this definition was watched on (U16, KTD4). A POPULATION filter, so it reaches
    -- meldinger_summary.sql and meldinger_messages.sql in the same shape. A named channel excludes
    -- rows whose own channel is NULL -- written before 2026-09-13 -- rather than folding them into
    -- 'browser', which is the n_unsegmented third-bucket rule one page over.
    AND (@channel  IS NULL OR channel  = @channel)
    -- ONE chosen SIRI-SX option is applied after subject aggregation below. It is expanded to EVERY
    -- stored summary it stands for -- the same shape as the place refs below, and for the same
    -- reason: an option can cover more than one stored value.
    -- Today that is the no-SX placeholders, whose wording changed on 2026-08-20 while the older rows
    -- kept the old text; matching only the value the dropdown nominated would return one of the two
    -- populations and silently hide the other, which is `varsel.place_refs`' lesson verbatim.
    --
    -- The values are allow-listed against meldinger_filters.sql's top-30 array before they ever
    -- reach here -- free-form prose from a public endpoint is otherwise one more response-cache
    -- entry and one more BigQuery query per distinct string. Exact equality per member, never a
    -- LIKE: the dropdown offers values that exist, so a prefix match would only add ways to select a
    -- population the reader cannot see the boundary of.
    --
    -- Applying this predicate here would discard the other cycles of a matching subject when its
    -- summary changed. That would corrupt whole-subject metrics and could discard the pushed cycle.
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
--
-- Note what the grain means for "one push per registration". `_notified` is keyed per journey_id,
-- so two devices watching the same trip are two pushes -- but the log is keyed on the journey
-- DEFINITION (journey_digest) with a device count beside it, deliberately, so that it cannot
-- identify a traveller. One row here is therefore one wording delivered to `n_devices` devices, and
-- the page says so rather than implying a single recipient.
subjects AS (
  SELECT
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    ANY_VALUE(cycle_summary) AS summary,
    ANY_VALUE(description) AS description,
    ANY_VALUE(line)        AS line,
    ANY_VALUE(mode)        AS mode,
    ANY_VALUE(stop_place)  AS stop_place,
    ANY_VALUE(from_stop)   AS from_stop,
    ANY_VALUE(to_stop)     AS to_stop,
    -- MAX rather than ANY_VALUE, and only here: this column decides the ORDER BY, and
    -- meldinger_messages.sql has to resolve the SAME ordered list so the two responses align
    -- position by position. ANY_VALUE is free to pick differently in two queries over the same rows.
    MAX(departure_time)    AS departure_time,

    -- ---- per-cycle figures, now WHOLE-SUBJECT ---------------------------------------------------
    -- Under the old axis every one of these was computed over the window's slice of the subject and
    -- carried a WINDOW-SCOPED caveat on the card, which is how `ever_alerted = FALSE` could sit
    -- beside `verdict = 'hit'` and be correct. On this axis the subject is wholly in the window, so
    -- these describe the whole subject and mean the same thing as /historikk's.
    COUNT(*)                                AS n_decisions,
    MIN(decided_at)                         AS first_decided_at,
    MAX(decided_at)                         AS last_decided_at,
    LOGICAL_OR(alertworthy)                 AS ever_alerted,
    MIN(IF(alertworthy, decided_at, NULL))  AS first_alert_at,
    MAX(impact_probability)                 AS max_impact_probability,
    ANY_VALUE(alert_threshold)              AS alert_threshold,
    MAX(n_devices)                          AS n_devices,
    -- The client side's count and the channel, aggregated exactly as varsel_decision_groups does
    -- it: the highest count any row carried, and 'both' where either a row said so or the subject
    -- has rows of each kind. Not in the GROUP BY -- a definition both channels share is ONE
    -- subject with one wording, which is the premise this page is built on.
    MAX(n_subscriptions)                    AS n_subscriptions,
    CASE
      WHEN LOGICAL_OR(channel = 'both')
        OR (LOGICAL_OR(channel = 'browser') AND LOGICAL_OR(channel = 'webhook')) THEN 'both'
      WHEN LOGICAL_OR(channel = 'webhook') THEN 'webhook'
      WHEN LOGICAL_OR(channel = 'browser') THEN 'browser'
    END                                     AS channel,
    -- `pushed` is written as a real bool on every row (decisions.py builds it with bool()), so this
    -- is never NULL in practice; COALESCE anyway, because the states below branch on it and a NULL
    -- would fall to the ELSE and report a delivered push as "never sent".
    COALESCE(LOGICAL_OR(pushed), FALSE)     AS pushed_any,
    COALESCE(LOGICAL_OR(push_off), FALSE)   AS push_off,
    -- The four "final" figures in ONE aggregate, not four. Two aggregates "picking the same row" is
    -- a promise ORDER BY cannot keep, and its failure mode here would be a card mixing one cycle's
    -- probability with another cycle's delay. (varsel_decision_groups still spells these as four
    -- separate ARRAY_AGGs; that is the older form, and the `delivered` aggregate beside them is the
    -- one this copies.) No IGNORE NULLS: the STRUCT itself is never NULL, so this is the last
    -- cycle's figures including when that cycle's own figures are NULL, which is the intent.
    ARRAY_AGG(STRUCT(
                impact_probability AS final_impact_probability,
                cancel_probability AS final_cancel_probability,
                delay_p50_min      AS final_delay_p50_min,
                delay_p90_min      AS final_delay_p90_min)
              ORDER BY decided_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS final,
    -- Was this a relayed fact or a prediction, as at the point a verdict would judge? Same rule as
    -- varsel_decision_groups: the first ALERTING cycle where we alerted, any cycle where we did not.
    -- Emphatically not a plain LOGICAL_OR, which would be backwards -- a cancellation is routinely
    -- declared AFTER we alerted, since alerting before ET publishes is the whole point of the app.
    -- Used only as the fallback below, for a subject the nightly job has not labelled yet.
    COALESCE(
      ARRAY_AGG(IF(alertworthy, et_cancelled, NULL)
                IGNORE NULLS ORDER BY decided_at LIMIT 1)[SAFE_OFFSET(0)],
      LOGICAL_OR(et_cancelled)
    ) AS et_cancelled_derived,

    -- ---- THE REPLAY PICK: the cycle a push actually went out on ---------------------------------
    -- `pushed` is the RECORDED OUTPUT of varsel's four delivery filters -- alertworthy against the
    -- operator's threshold, the train not yet departed, one_alert_per_train's collapse of several SX
    -- messages about one departure, and the `_notified` dedup keyed on (journey_id, situation) --
    -- written by _record_decisions AFTER delivery, so it reflects what reached a device rather than
    -- what was queued.
    --
    -- So this is looked up, never re-derived, and that is not a shortcut: a re-derivation cannot see
    -- `_notified` (which persists across cycles and across redeploys in the GCS state blob), cannot
    -- see whether a subscription was `gone` and the push landed a cycle later, and cannot see which
    -- situation won the collapse in a cycle it is not simulating. It would produce a plausible send
    -- time that is not the send time.
    --
    -- FIRST, not last: `_notified` makes the first delivery the only one for that situation's life,
    -- so later alerting cycles record pushed = FALSE and the in-app card, not a second push, is what
    -- reflects a worsening. This is the same aggregate shape as `delivered` in
    -- create_varsel_decisions.sql, which /historikk's card was fixed to use on 2026-08-21.
    --
    -- ONE aggregate carrying its fields together, for the same reason as `final` above:
    -- `would_send_at` and `prompt_version` describe one cycle, and the messages file resolves
    -- `message_input` from the same expression. Split into two aggregates they could name two
    -- different cycles, and the symptom would be a card quoting one cycle's wording under another
    -- cycle's time -- exactly the defect the view was fixed for.
    --
    -- The `has_input` guard makes this "the pushed cycle we can replay". A push whose cycle recorded
    -- no input has nothing to hand today's prompt; that is its own state below, not "no message".
    ARRAY_AGG(IF(pushed AND has_input, STRUCT(
                decided_at     AS would_send_at,
                prompt_version AS prompt_version), NULL)
              IGNORE NULLS ORDER BY decided_at LIMIT 1)[SAFE_OFFSET(0)] AS replay
  FROM scanned
  GROUP BY journey_digest, operator, jkey, call_date, stop_order, situation_number
  -- Eligibility is subject-scoped, while every matching subject keeps all of its cycles above.
  -- IFNULL is required because an empty array from the Python client has NULL ARRAY_LENGTH.
  HAVING IFNULL(ARRAY_LENGTH(@sx_summaries), 0) = 0
      OR COUNTIF(cycle_summary IN UNNEST(@sx_summaries)) > 0
),
-- One derived fact that needs the sibling subjects: did any OTHER situation about this same
-- registration-and-train deliver a push? It answers the question a silent card provokes -- an
-- alert-worthy subject that never pushed is usually one_alert_per_train's collapse loser, and
-- saying so is the difference between "we stayed quiet about this train" and "we told them, under
-- another situation number".
--
-- It also makes the page a detector for the open `_notified` gap: the dedup key is per SITUATION
-- while the collapse is per cycle, so if the winning situation for a train flips between cycles the
-- new winner's key is not in `_notified` and a SECOND push goes out for the same train. That shows
-- up here as two subjects on one train both reading `sendt`.
--
-- Partitioned on the registration too, not just the train: the collapse and the dedup are both
-- per-registration, so another traveller's push must not explain away this one's silence. A NULL
-- jkey identifies no train at all and would group every unkeyed subject of a digest together, so it
-- is forced to zero rather than allowed to claim a sibling it cannot have.
subjects_marked AS (
  SELECT
    *,
    IF(jkey IS NULL, 0,
       COUNTIF(pushed_any) OVER (
         PARTITION BY journey_digest, operator, jkey, call_date, stop_order
       ) - IF(pushed_any, 1, 0)) AS other_pushed_on_train
  FROM subjects
)
SELECT
  s.situation_number, s.summary, s.description,
  s.operator, s.line, s.mode, s.from_stop, s.to_stop, s.call_date, s.departure_time,
  s.n_decisions, s.first_decided_at, s.last_decided_at,
  s.ever_alerted, s.first_alert_at,
  -- Now agrees with the nightly job's alert_lead_min by construction rather than by a recomputation
  -- kept from disagreeing with it: both describe the subject's whole life. Still computed here
  -- rather than copied from o.alert_lead_min, so an unlabelled subject -- nearly all of them on a
  -- same-day window -- still gets a lead time.
  TIMESTAMP_DIFF(s.departure_time, s.first_alert_at, MINUTE) AS alert_lead_min,
  s.max_impact_probability,
  s.final.final_impact_probability,
  s.final.final_cancel_probability,
  s.final.final_delay_p50_min,
  s.final.final_delay_p90_min,
  s.alert_threshold,
  -- Provenance, from the nightly job where it has run and from the decisions otherwise -- the same
  -- two arms /historikk serves it from, in one expression because this query is a join rather than a
  -- UNION. Taking it from the outcomes row where one exists is what keeps a card's tag agreeing with
  -- the segment /summary counted it in; that file reads the same column.
  COALESCE(o.et_cancelled, s.et_cancelled_derived) AS et_cancelled,
  s.pushed_any, s.n_devices, s.channel, s.n_subscriptions, s.other_pushed_on_train,

  -- ---- the replay, projected -------------------------------------------------------------------
  s.replay.would_send_at,
  -- The prompt in force when the ROW was written, against llm_message.PROMPT_VERSION today. The page
  -- marks the difference; it does not claim to separate a prompt change from sampling variance,
  -- since each wording is a single draw from a non-deterministic decoder.
  s.replay.prompt_version,
  -- Why this subject does or does not have a message, in one field the card renders directly.
  -- Ordered most-specific-first, and the order carries meaning: a sub-threshold subject is reported
  -- as such even on a train where another situation pushed, because never reaching the gate is the
  -- reason it was silent and the sibling push is incidental to it.
  CASE
    WHEN s.pushed_any AND s.replay.would_send_at IS NOT NULL THEN 'sendt'
    -- Delivered, but that cycle recorded no message_input -- so there is nothing to hand today's
    -- prompt. Rows written before 2026-08-05 have no input at all, and a builder failure leaves the
    -- same gap; both are this state rather than "no message was sent".
    WHEN s.pushed_any                                        THEN 'sendt_uten_input'
    WHEN NOT s.ever_alerted                                  THEN 'under_terskel'
    WHEN s.other_pushed_on_train > 0                         THEN 'samlet'
    -- The deployment does not push at all (no VAPID keypair): every subject would otherwise read as
    -- a deliberate silence, and the page would look like an alerting failure rather than a
    -- configuration.
    WHEN s.push_off                                          THEN 'push_av'
    ELSE 'ikke_sendt'
  END AS send_state,

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
  -- from the 05:30 Oslo job the following morning. /summary reports the split beside the rates for
  -- exactly that reason.
  CASE
    WHEN o.actual_trouble IS NOT NULL THEN 'avklart'
    WHEN s.jkey IS NULL               THEN 'ukjent'
    WHEN s.call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY) THEN 'venter'
    ELSE 'ukjent'
  END AS outcome_state
FROM subjects_marked AS s
LEFT JOIN `disruptions.varsel_decision_outcomes` AS o
  -- The partition bound FIRST, and inside the ON rather than in the WHERE: in the WHERE it would
  -- turn this into an inner join and drop every unlabelled subject -- which on a same-day window is
  -- nearly all of them. See the header for why it stays +/-1 day rather than the exact `= @day` the
  -- departure axis appears to allow.
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
  -- "Only the ones a traveller was actually told about" -- a strictly narrower cut than
  -- @alerted_only, and the difference is the point: an alert-worthy subject that never pushed is a
  -- collapse loser or a repeat cycle, and it reached nobody.
  AND (NOT @pushed_only  OR s.pushed_any)
  -- "With ground truth" is the OUTCOME being known, deliberately not `verdict IS NOT NULL`: an
  -- outcome that resolved while ever_alerted was NULL is judged by nobody yet plainly has a realized
  -- call behind it, and filtering on the verdict would leave those reachable from no filter setting.
  AND (NOT @resolved_only OR o.actual_trouble IS NOT NULL)
-- Fully deterministic, and it has to be: meldinger_messages.sql repeats this ORDER BY verbatim so
-- the two responses line up position by position, and the page keys its list on position because
-- nothing served identifies a decision uniquely (the real grain includes journey_digest, which is
-- redacted). departure_time alone leaves ties -- two registrations on the same train and the same
-- situation differ only in journey_digest -- so the whole subject key breaks them. Ordering by a
-- redacted column is not projecting it.
--
-- ASCENDING since 2026-08-22, where the decided_at axis sorted DESC: the window now selects a
-- stretch of departures, and a reader walking 07:00 to 09:00 wants the 07:03 first.
ORDER BY s.departure_time ASC,
         s.situation_number, s.operator, s.jkey, s.call_date, s.stop_order, s.journey_digest
LIMIT @limit
