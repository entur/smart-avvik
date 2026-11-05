-- The two wordings for one decision, side by side: what the model wrote, and what the template did.
--
-- Since 2026-08-07 `varsel_decisions` records BOTH wordings on every alert-worthy decision --
-- `message_llm` and `message_deterministic` -- while `VARSEL_LLM_ENABLED` decides only which one
-- reached the traveller. This file is what that recording exists for. Without it the columns are
-- inert: the change ships the machinery and never produces the answer.
--
-- Run it per environment (each records its own decisions; the tables are not copied dev->prd):
--   bq --project_id=ent-smartavvik-dev query --use_legacy_sql=false < message_wording_comparison.sql
--   bq --project_id=ent-smartavvik-prd query --use_legacy_sql=false < message_wording_comparison.sql
-- A few MB over a 30-day window, i.e. free. Dry-run each statement SEPARATELY when editing --
-- BigQuery validates a script lazily (`numChildJobs: 1`) and a whole-file dry run checks only the
-- first, which is how a broken second query ships green (the rule `validate_varsel_sql.sh` exists
-- for).
--
-- HANDLING. This reads the most sensitive table in the repo. The query deliberately selects none of
-- `journey_digest`, `from_stop`, `to_stop` or `message_input` -- the quasi-identifying set
-- `/historikk-api` redacts -- and joins on the digest without projecting it. The output still
-- contains `message_llm`, which is unconstrained prose the prompt instructs to advise on the
-- traveller's own route, so it routinely NAMES both ends of somebody's trip. Read it, do not paste
-- it into a PR body, a plan, a ticket or a shared page.
--
-- FIVE WAYS TO READ THE RESULT WRONGLY. Each is a real trap, not a caveat:
--
--   1. SUBJECTS, NOT ROWS. `varsel_decisions` holds one row per refresh cycle -- ~10 per subject,
--      deliberately, because the repeats are the lead-time series. Counting rows weights a
--      long-lived situation ten times heavier than a short one. The grain here comes from
--      `disruptions.varsel_decision_groups`, the same view `/historikk` and the nightly labelling
--      job group on, so subject counts here agree with the page instead of drifting from it.
--
--   2. `message_status = 'ok'` IS THE SAME-INPUT FILTER. Under `ok_cached` the model's text was
--      written from an EARLIER cycle's `message_input` while the deterministic text beside it is
--      this cycle's. Comparing those two measures staleness, not the writers.
--
--   3. `message_deterministic IS NOT NULL` DATES THE ROW. A NULL `message_llm` means either "no
--      model wording" or "written before the column existed", and the log holds many of the latter
--      (every row written before this change: measured 2026-08-07 in dev, 4010 of 4010 rows have a
--      NULL `message_deterministic`, because nothing has written the column yet). Pre-change rows
--      are reported as their own bucket below,
--      never folded into a generation-failure rate. Same third-bucket rule `/historikk` states for
--      `n_unsegmented`, and it has already been got backwards twice in this repo.
--
--   4. `attempts_exhausted` MEANS NO BACKEND WAS REACHABLE, not that the model was asked and
--      failed. `varsel._write_message` returns None without calling anything on several paths, and
--      the caller spends an attempt on any None -- so after `_MAX_MESSAGE_ATTEMPTS` cycles a
--      situation stops being offered to the writer at all. Measured in prd on 2026-08-07, where
--      generation had never run: 753 rows at `llm_disabled` and 693 at `attempts_exhausted` across
--      the same 11 subjects.
--
--   5. SEGMENT ON PROVENANCE, NEVER FILTER, AND TAKE `et_cancelled` FROM THE VIEW. A declared
--      cancellation strips every probability from the payload, so both writers have a much narrower
--      job and the wordings converge; blending those subjects with delay alerts flatters the
--      template. Critically, the segment is NOT `LOGICAL_OR(et_cancelled)` over the subject's
--      cycles: `varsel_decision_groups` resolves it at the cycle the verdict judges (the first
--      alerting cycle where we alerted, `LOGICAL_OR` where we did not), and the view's own header
--      records why a plain `LOGICAL_OR` is exactly backwards — a cancellation is routinely declared
--      AFTER we alerted, since beating the feed is the point, so it would file the stack's best wins
--      as gimmes. This query joins the view and reads `et_cancelled` from it, so the split here and
--      the one `/historikk` reports cannot drift. NULL stays its own row, never defaulted to FALSE.
--
--   6. THERE IS A THIRD PROVENANCE SINCE 2026-08-20, AND IT IS A DIFFERENT WRITING TASK. A call no
--      SX message named (`situation_number LIKE 'KIX:NoSituation:%'`) reaches both writers with
--      `ingen_melding: true` and an EMPTY `situasjoner` array: there is no operator text to quote,
--      so both write from the figures alone. Since 2026-08-20 neither one says the message is
--      missing either -- the template heads the body with "Mulig avvik" and the prompt asks for the
--      disruption alone -- so the two wordings converge for the same structural reason a declared
--      cancellation makes them converge (the brief is narrower), and pooling these subjects with
--      ordinary delay alerts flatters the template exactly as trap 5 describes. Rows written before
--      that date carry the older, longer wording on BOTH sides; `prompt_version` dates the prompt
--      half of the change.
--      The three-way split below uses the SAME CASE as `historikk_summary.sql`, `relayed` winning
--      where a no-SX call was later declared cancelled, so the segments here and the ones the page
--      reports are the same populations. Expect this segment to be thin or empty for a while: it
--      cuts at 0.85 rather than 0.65 (`alert_policy._NOSX_ALERT_THRESHOLD`), so it produces far
--      fewer alert-worthy decisions per day than its share of the timetable suggests.
--
--   7. A FOURTH PROVENANCE SINCE 2026-09-23: `removed`, a train Vy took out of the day's timetable
--      (`KIX:PlanRemoved:`). It carries `et_cancelled = TRUE` although ET declared nothing, so the
--      CASE tests the prefix FIRST, ahead of the flag -- the same order as `historikk_summary.sql`.
--      Expect it empty or near it: varsel keeps these trains out of the per-train pushes and the
--      writing pass (they reach the traveller in one morning digest), so few of them carry a pair.
--
--   8. A FIFTH SINCE multi-leg U11: `transfer` («med bytte»), a departure with a change. Its
--      `message_input` carries the trip facts and its wording comes from a second system prompt
--      with its own `prompt_version` (KTD12), so it is yet another writing task. The CASE tests the
--      legs snapshot (`g.trip_legs`) FIRST, ahead of `removed` -- the first leg of a trip can be any
--      of the other four.
--
-- Also worth pinning before drawing a conclusion: `prompt_version` (a prompt edit mid-window makes
-- rows incomparable -- that is what the column is for) and `llm_provider` (dev has been answering
-- on Gemini with Claude latched off, so a result there is not a verdict on Sonnet).

-- ---------------------------------------------------------------------------------------------
-- 1. Coverage: how many subjects actually have a comparable pair, and where the rest went.
--    Run this FIRST. A side-by-side over three subjects is an anecdote, and the second query
--    below cannot tell you that on its own.
-- ---------------------------------------------------------------------------------------------
WITH cycles AS (
  SELECT
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    message_status,
    message_llm,
    message_deterministic,
    message_source,
    prompt_version,
    llm_provider
  FROM `disruptions.varsel_decisions`
  WHERE decided_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
),
subjects AS (
  SELECT
    -- The subject grain, matching varsel_decision_groups. journey_digest is carried to group and
    -- join on; it is never projected past this CTE.
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    -- Post-change is decided on the deterministic column, per trap 3.
    LOGICAL_OR(message_deterministic IS NOT NULL)                 AS instrumented,
    LOGICAL_OR(message_status = 'ok' AND message_llm IS NOT NULL) AS has_same_input_pair,
    LOGICAL_OR(message_llm IS NOT NULL)                           AS has_any_llm_wording,
    LOGICAL_OR(message_source = 'llm')                            AS ever_delivered_llm,
    COUNT(*)                                                      AS n_cycles
  FROM cycles
  GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT
  -- `et_cancelled` comes from the VIEW, never re-derived here (trap 5); the prefix test is the
  -- same one historikk_summary.sql spells, in the same resolution order -- a trip first (trap 8),
  -- then `removed` (trap 7: its flag is TRUE with nothing declared), then NULL (unsegmented), then
  -- `relayed`, which wins
  -- over `no_sx` because a no-SX call declared cancelled between the scoring cycle and the judged
  -- cycle DID reach the traveller as a relayed fact (trap 6).
  CASE
    WHEN g.trip_legs IS NOT NULL                           THEN 'transfer'
    WHEN STARTS_WITH(situation_number, 'KIX:PlanRemoved:') THEN 'removed'
    WHEN g.et_cancelled IS NULL                            THEN NULL
    WHEN g.et_cancelled IS TRUE OR g.destination_cancelled IS TRUE THEN 'relayed'
    WHEN STARTS_WITH(situation_number, 'KIX:NoSituation:') THEN 'no_sx'
    ELSE 'predicted'
  END                                 AS provenance,
  COUNT(*)                            AS subjects,
  -- Reported beside the rates, never inside them: these predate the columns and carry no evidence
  -- either way.
  COUNTIF(NOT instrumented)           AS pre_change_subjects,
  COUNTIF(instrumented)               AS instrumented_subjects,
  COUNTIF(has_any_llm_wording)        AS with_llm_wording,
  -- The denominator that matters: a pair written from the SAME message_input.
  COUNTIF(has_same_input_pair)        AS comparable_pairs,
  COUNTIF(instrumented AND NOT has_any_llm_wording) AS instrumented_no_llm_wording,
  COUNTIF(ever_delivered_llm)         AS delivered_llm,
  -- Withheld by policy: a wording exists and the template went out instead. This WAS prd, and it is
  -- the population the rollout decision was judged on -- 590 pairs over the 30 days to 2026-08-23,
  -- when delivery was turned on there (VARSEL_LLM_ENABLED=true). It stops growing in prd from that
  -- date, so a window spanning it mixes two deployments; dev has delivered since the split.
  COUNTIF(has_any_llm_wording AND NOT ever_delivered_llm) AS written_but_withheld,
  ROUND(AVG(n_cycles), 1)             AS avg_cycles_per_subject
FROM subjects AS s
JOIN `disruptions.varsel_decision_groups` AS g
  USING (journey_digest, operator, jkey, call_date, stop_order, situation_number)
GROUP BY provenance
ORDER BY provenance;

-- ---------------------------------------------------------------------------------------------
-- 2. The side-by-side itself: one row per decision subject, both wordings, and the verdict where
--    the nightly job has produced one.
--
--    Only cycles with message_status='ok' contribute the wordings (trap 2), so the two texts on a
--    row were written from the same message_input. The subject grain and the outcome join both key
--    on (journey_digest, operator, jkey, call_date, stop_order, situation_number) -- the view's
--    grain, so these rows line up with what /historikk shows for the same train.
-- ---------------------------------------------------------------------------------------------
WITH paired AS (
  SELECT
    journey_digest, operator, jkey, call_date, stop_order, situation_number,
    -- The last cycle that produced a same-input pair: the closest look before departure that can
    -- still be compared like for like.
    ARRAY_AGG(message_llm           ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS wording_llm,
    ARRAY_AGG(message_deterministic ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS wording_template,
    ARRAY_AGG(message_source        ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS delivered,
    ARRAY_AGG(llm_provider          ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS provider,
    ARRAY_AGG(llm_model             ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS model,
    ARRAY_AGG(prompt_version        ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS prompt_version
  FROM `disruptions.varsel_decisions`
  WHERE decided_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
    AND message_status = 'ok'
    AND message_llm IS NOT NULL
    AND message_deterministic IS NOT NULL
  GROUP BY 1, 2, 3, 4, 5, 6
)
SELECT
  -- The decision is identified by its TRAIN, not by whose trip it was -- the same rule
  -- historikk_decisions.sql follows. journey_digest joins below but is not projected.
  g.call_date,
  g.operator,
  g.line,
  g.mode,
  g.departure_time,
  g.summary                       AS situation,
  g.ever_alerted,
  g.et_cancelled,
  -- Same CASE as query 1 and as historikk_summary.sql. Sort or filter the side-by-side on this
  -- before reading two texts against each other: a `no_sx` pair and a `predicted` pair were
  -- written from structurally different inputs (trap 6).
  CASE
    WHEN g.trip_legs IS NOT NULL                           THEN 'transfer'
    WHEN STARTS_WITH(situation_number, 'KIX:PlanRemoved:') THEN 'removed'
    WHEN g.et_cancelled IS NULL                            THEN NULL
    WHEN g.et_cancelled IS TRUE OR g.destination_cancelled IS TRUE THEN 'relayed'
    WHEN STARTS_WITH(situation_number, 'KIX:NoSituation:') THEN 'no_sx'
    ELSE 'predicted'
  END                             AS provenance,
  ROUND(g.max_impact_probability, 3) AS max_impact_probability,
  p.delivered                     AS wording_delivered,
  p.provider,
  p.model,
  p.prompt_version,
  p.wording_llm,
  p.wording_template,
  -- A crude but useful first cut: the model is briefed to be shorter and more specific, so a large
  -- length gap is where the two writers actually diverge. Not a quality measure -- read the text.
  LENGTH(p.wording_llm)           AS len_llm,
  LENGTH(p.wording_template)      AS len_template,
  o.verdict,
  o.actual_cancelled,
  o.actual_dep_delay_min
FROM paired AS p
JOIN `disruptions.varsel_decision_groups` AS g
  USING (journey_digest, operator, jkey, call_date, stop_order, situation_number)
-- LEFT: a subject too recent to have been labelled still belongs in the comparison. A NULL verdict
-- here means "not labelled yet or never resolvable", never "no trouble" -- the distinction
-- /historikk keeps as venter vs ukjent.
LEFT JOIN `disruptions.varsel_decision_outcomes` AS o
  USING (journey_digest, operator, jkey, call_date, stop_order, situation_number)
ORDER BY g.departure_time DESC;
