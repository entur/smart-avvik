-- The alert-decision log: what Avviksvarsel concluded about a registered journey, and when.
--
-- Run at startup with CREATE TABLE IF NOT EXISTS (decisions.ensure_table), not CREATE OR REPLACE.
-- Every other table the API builds is a derived snapshot the poller can rebuild from BigQuery at
-- will; this one is the only place its own history lives. Replacing it would destroy the record.
-- It is created from the app rather than by a script because prd is an isolated project fed by
-- free copies (copy_runtime_to_prd.sh) and nobody runs build scripts against it -- an
-- operator-provisioned table would simply be missing there, and every write would fail.
--
-- Deliberately NOT in copy_runtime_to_prd.sh: dev and prd each generate their own decisions, and
-- that script uses `bq cp -f`, so listing this table would overwrite prd's real history with dev's.
--
-- Append-only. Rows are never updated: the nightly labelling job
-- (data/queries/label_varsel_decisions.sql) reads this table and writes a separate outcomes table.
-- "What we decided at 07:14" is a historical fact and should not be editable by a later job.
--
-- Keyed on (journey definition x situation x departure) -- including decisions that did NOT alert,
-- which is what makes a miss observable rather than invisible. Repeats across cycles are expected
-- and intended: they are the lead-time series.
--
-- NOT one row per cycle since 2026-09-12 (KTD15). It was, from the day this table existed, and the
-- reasoning above still holds -- what stopped holding is the arithmetic. Measured in prd
-- 2026-09-11: ~400 rows per definition per day at 226 definitions, 88-95 % of them sub-threshold
-- no-SX subjects re-scored every ~295 s with nothing having moved; at the 35 000-80 000 definitions
-- this stack is being taken to that is 14-33 GB a DAY, and a 14-day /historikk window over it is
-- 70-150 GB per request. varsel._thinning_reason now writes a standing subject at most once per
-- quarter hour, PLUS every row the figures in varsel_decision_groups are made of: the first
-- sighting, every alert-worthy/sub-threshold transition, every new maximum impact probability,
-- every push, and a closing row carrying the subject's last observation, dated at the cycle it
-- describes. So first_alert_at, max_impact_probability, the final_* block and the quoted wording
-- are unchanged by construction; n_decisions counts rows written and says so in its description,
-- and no per-cycle rate may be computed across that date.
--
-- Privacy: keyed on the journey DEFINITION (journey_digest, a SHA-256 prefix of the journey key)
-- plus a device count. No device_id and no journey_id, so this is a record of what was said about
-- a train, not of where a person commutes. from_stop/to_stop are public StopPlace refs; the
-- traveller's time window and weekday pattern are not stored.
--
-- message_input is the exception that proves the rule: it embeds the same origin, destination and
-- departure time in one blob, which is why /historikk-api serves neither it nor from_stop/to_stop.
-- The table is the audit record; the public page is not.
--
-- Cost: negligible. Streaming inserts of a handful of rows per poller cycle.
CREATE TABLE IF NOT EXISTS `disruptions.varsel_decisions`
(
  decided_at            TIMESTAMP NOT NULL OPTIONS(description="When this match was evaluated (UTC). NOT every cycle since 2026-09-12: a standing subject nothing has changed about is written at most once per quarter hour (varsel._thinning_reason), plus every cycle a figure is computed from -- first sighting, each alert-worthy/sub-threshold transition, each new maximum impact probability, each push, and a closing row carrying the subject's LAST observation, dated at the cycle it describes. So consecutive rows for one subject can be minutes or a quarter hour apart, and the gap is not a gap in the matching. Before that date every cycle wrote, which is why no per-cycle rate may be computed across it."),
  journey_digest        STRING    OPTIONS(description="SHA-256[:12] of the journey key; joins to jsonPayload.journey_digest on event='llm_message' log records."),
  from_stop             STRING    OPTIONS(description="Boarding StopPlace ref the traveller registered."),
  to_stop               STRING    OPTIONS(description="Alighting StopPlace ref the traveller registered."),

  situation_number      STRING NOT NULL OPTIONS(description="SIRI-SX situationNumber."),
  summary               STRING    OPTIONS(description="Short Norwegian summary of the situation."),
  description           STRING    OPTIONS(description="The operator's fuller explanation."),

  -- The ET join key. (operator, jkey, call_date, stop_order) is the grain that works for both
  -- families: rail refs are date-scoped and unique alone, bus ServiceJourney jkeys repeat daily.
  operator              STRING    OPTIONS(description="Ref codespace: VYG, GOA, RUT, ..."),
  line                  STRING    OPTIONS(description="Line ref of the matched train."),
  mode                  STRING    OPTIONS(description="Vehicle noun shown to the traveller: 'Tog', 'Buss', 'T-bane', 'Trikk' or 'Båt' (the last three since 2026-09-29)."),
  jkey                  STRING    OPTIONS(description="SIRI-ET datedVehicleJourneyRef minus its codespace prefix."),
  call_date             DATE      OPTIONS(description="Operating day of the boarding call (Europe/Oslo)."),
  stop_place            STRING    OPTIONS(description="StopPlace ref of the boarding call."),
  stop_order            INT64     OPTIONS(description="Position of the boarding call on the journey."),
  departure_time        TIMESTAMP OPTIONS(description="Aimed departure from the boarding stop (UTC)."),
  minutes_to_departure  INT64     OPTIONS(description="departure_time - decided_at, in minutes: the lead time this decision was made at."),

  -- The prediction the decision was made on.
  et_cancelled          BOOL      OPTIONS(description="ET had ALREADY declared this boarding call cancelled when we decided, so impact_probability/cancel_probability are 1.0 by construction: the app RELAYED a known fact rather than predicting one. Segment on this rather than dropping the rows — the log answers two questions and no single figure serves both. SERVICE quality (was the traveller told the truth?) includes them, and should: relaying what ET already knows is most of the value, since nobody carries the ET feed on their phone. MODEL skill (did we see it coming?) excludes them: they are gimmes. Distrust either figure quoted alone, because the MIX of the two populations moves it with nothing about the app having changed."),
  impact_probability    FLOAT64   OPTIONS(description="Calibrated P(cancelled OR >= 10 min late) — the alerting score; exactly 1.0 when et_cancelled, where it is a relayed fact and not a prediction."),
  cancel_probability    FLOAT64   OPTIONS(description="Calibrated P(the train is cancelled); exactly 1.0 when et_cancelled, where it is a relayed fact and not a prediction."),
  ge10_probability      FLOAT64   OPTIONS(description="Calibrated P(>= 10 min late | it runs)."),
  delay_p50_min         INT64     OPTIONS(description="Median departure delay (min) from the exceedance stack."),
  delay_p90_min         INT64     OPTIONS(description="Bad-case (p90) departure delay (min)."),
  alert_threshold       FLOAT64   OPTIONS(description="The operator's threshold applied to impact_probability here."),
  alertworthy           BOOL      OPTIONS(description="Whether the score crossed the threshold."),
  pushed                BOOL      OPTIONS(description="Whether a notification was ISSUED for THIS decision -- this journey definition, this situation, this departure -- in the cycle the row was written: a Web Push the push service accepted, or, since 2026-09-13 (KTD4), a webhook event this cycle MINTED. The mint and not the delivery: a delivered mark lags the mint by up to two cycles, so a subject retired in between would read as never notified. Read `channel` beside it -- a 'both' row can say pushed with no Web Push behind it. Read rows before 2026-09-12 with care: the delivery loop marked pushes per (definition, situation) while rows are per (definition, situation, departure), so a push about one train could mark another departure under the same situation."),
  n_devices             INT64     OPTIONS(description="How many /varsel registrations share this journey definition -- the BROWSER side only, and it stays that way now that webhook routes are recorded too (2026-09-13, KTD4). A webhook-only row therefore reads 0 here beside a non-zero n_subscriptions, and neither column counts the other's recipients."),

  -- What had actually been OBSERVED on the line when we decided (as opposed to predicted). The
  -- system prompt tells the model to quote this with its timestamp, so it is the factual claim a
  -- traveller-facing message makes; obs_observed_at is its age (the source has a 60/120 min TTL).
  obs_cancelled         BOOL      OPTIONS(description="ET had declared the latest observed call on this line cancelled."),
  obs_stop_name         STRING    OPTIONS(description="Where that observation was made (name, falling back to the StopPlace ref)."),
  obs_delayed_min       INT64     OPTIONS(description="Minutes late at that stop; null if not recorded."),
  obs_observed_at       TIMESTAMP OPTIONS(description="Aimed time of the observed call (UTC) — how old the observation was when quoted."),

  -- The alternative journey offered, if any. Flat here, in full inside message_input. NOT
  -- necessarily a faster one since 2026-08-06: when nothing arrives before the disrupted train's
  -- aimed arrival, the fallback tier offers the earliest arrival there is, which for a cancelled
  -- train legitimately lands later than the timetable promised. alt_status (last in this table) is
  -- what makes the absence of an offer readable at all.
  has_alternative       BOOL      OPTIONS(description="Whether a replacement journey was offered to the traveller. NOT 'a faster way': the fallback tier legitimately arrives later than the disrupted train was due (see alt_arrives_before_original). FALSE covers three unlike states — a lookup that found nothing, a selection that kept the traveller on their own train, and no lookup at all — which is what alt_status separates."),
  alt_minutes_saved     INT64     OPTIONS(description="SIGNED minutes earlier this arrives than the disrupted train's PREDICTED arrival. Negative is a legitimate answer, not a bad row: a replacement for a cancelled train is worth offering even when it gets the traveller there later. Informational — nothing is dropped for being too slow."),
  alt_departure_time    TIMESTAMP OPTIONS(description="Expected departure of the alternative (UTC)."),
  alt_arrival_time      TIMESTAMP OPTIONS(description="Expected arrival of the alternative (UTC)."),
  alt_lines             STRING    OPTIONS(description="Its transit legs as shown to the traveller, e.g. 'R11, 100'; walking legs omitted."),

  -- What the traveller was told. Since 2026-08-07 these two describe DELIVERY, not availability:
  -- both candidate wordings are written for every alert-worthy decision and both are recorded last
  -- in this table (message_llm / message_deterministic), and VARSEL_LLM_ENABLED picks which one is
  -- sent. Read `message` as the text that reached the phone and `message_source` as which wording
  -- that was -- never as "did a model write one", which is `message_llm IS NOT NULL`.
  message               STRING    OPTIONS(description="The message body sent (or that would have been sent)."),
  message_source        STRING    OPTIONS(description="Which wording was SENT: 'llm' when the model's text went out, 'deterministic' for the template. Since 2026-08-07 it says nothing about which wordings EXIST — both are written for every alert-worthy decision and recorded in message_llm/message_deterministic, and VARSEL_LLM_ENABLED selects the one delivered. So 'deterministic' now covers two unlike states: no model wording exists, and one exists and was withheld by policy (prd's setting today). Separate them with message_llm IS NOT NULL, never with this column alone."),
  message_status        STRING    OPTIONS(description="Why that source: ok | ok_cached | not_alertworthy | not_selected | push_disabled | llm_disabled | capped | attempts_exhausted | transient_exhausted | rejected | ungrounded | error | unavailable. Without it every failure mode looks like one deterministic row. TWO give-up statuses since 2026-08-22, judging different things: attempts_exhausted the PAYLOAD (asked, and refused/over-ran/invented a figure), transient_exhausted the ENVIRONMENT (backend reached and failed repeatedly). One counter served both before, charged on EVERY None including paths that asked no backend -- so read a pre-2026-08-22 attempts_exhausted as transient unless proven otherwise (in prd: 290 preceding errors, zero rejected/ungrounded), and compute no rate across the change. llm_disabled also changed meaning, on 2026-08-07: it means no backend could be ASKED (VARSEL_LLM_PROVIDER=off, no project, all latched off), never that the deployment sends the template -- that is message_source='deterministic' with a non-null message_llm, status ok."),
  llm_provider          STRING    OPTIONS(description="Backend that wrote the message: 'claude' | 'gemini'. Null when none did."),
  llm_model             STRING    OPTIONS(description="Model id behind that backend, e.g. 'claude-sonnet-5'."),
  prompt_version        STRING    OPTIONS(description="SHA-256[:8] of the system prompt in force — a prompt edit invalidates comparisons across rows, and nothing else records it."),
  -- The writer's INPUT, verbatim: llm_message.build_message_input serialized by
  -- llm_message.message_input_json, the same bytes the model was sent -- and, when no model wrote
  -- anything, the same document the deterministic wording beside it was assembled from. Written for
  -- every decision, including the sub-threshold ones no message was ever written for. Contains stop
  -- NAMES and the departure time, which is the quasi-identifying trio /historikk-api deliberately
  -- redacts -- it must stay out of the API's SELECT (Decision is extra="forbid", which fails loudly
  -- if a future edit adds it).
  message_input         STRING    OPTIONS(description="Exact JSON every message about this decision was written from (the model's prompt; the template's source when no model wrote one)."),
  snapshot_refreshed_at TIMESTAMP OPTIONS(description="When the per-call prediction cache behind this decision was last rebuilt (UTC) — how stale the numbers were."),
  -- New columns go LAST, matching where ALTER ADD COLUMN puts them on an existing table (the rule
  -- stated at the outcomes table below, and it applies to this one just as much): otherwise a fresh
  -- environment and an upgraded one hold the same columns in different orders, and anything
  -- positional writes each environment's data into a different column.
  alt_status            STRING    OPTIONS(description="What became of the alternative-journey lookup: offered | none_found | suppressed (the selection ran and kept the traveller on their own train) | not_worth_lookup (no lookup was issued — the impact gate, or a reading taken from a non-canonical stop ref). Reserved but not yet written, because impact.for_trip does not record them and they are indistinguishable from 'no lookup' downstream: beyond_horizon | error. Recorded on every WRITTEN decision: until 2026-09-12 that was every decision, sub-threshold included; since the thinning rule (varsel._thinning_reason) it is every decision the log KEEPS. Thinning is blind to this column, so it drops the flat middle of standing sub-threshold subjects and the survivors over-represent transitions, peaks and pushes — NO offer rate over it is comparable across that date. It also gives the 2026-08-06 selection change a denominator, that change moving the numerator sharply. NULL = written before the column."),
  alt_arrives_before_original BOOL OPTIONS(description="The offered journey arrives strictly before the disrupted train's SCHEDULED arrival -- the traveller's own deadline, not the delayed train's expected arrival. FALSE is the fallback tier: nothing got there in time, and the traveller is better off late than stranded. Deliberately a different anchor from alt_minutes_saved, which measures against the PREDICTED arrival: one answers 'do I still make it', the other 'is this better than what I have'. Read them together, never either alone."),
  alt_at_risk           BOOL      OPTIONS(description="The offered journey is itself predicted to be in trouble (over its operator's alert gate, or on a line a live situation touches). Offered anyway because nothing safer reached the destination in time — these are the rows where a wording that promises a clean escape would be wrong."),
  alt_scored            BOOL      OPTIONS(description="The prediction cache held the offered journey's boarding call, so its probabilities are model output. FALSE means nothing scored it — the ordinary case, and NOT 'no risk'."),
  -- The recommendation's own ET join key, so the advice can be scored the way every other decision
  -- in this table already is. (operator, jkey, call_date, stop_order) is the grain
  -- label_varsel_decisions.sql joins last_recorded on; the columns above carry it for the DISRUPTED
  -- train, and until these existed "take this instead" was the one traveller-facing claim nothing
  -- could ever label. The operating day is not a column of its own -- it is
  -- DATE(alt_departure_time, "Europe/Oslo"), exact except where an access walk straddles midnight.
  alt_operator          STRING    OPTIONS(description="Ref codespace of the offered journey's boarding call (VYG, GOA, RUT, ...), from the prediction cache and never from the Journey Planner's line codespace — a JP codespace would fill the column and join to nothing. NULL exactly when alt_scored is FALSE."),
  alt_jkey              STRING    OPTIONS(description="Journey ref of the offered journey's boarding call, minus its codespace (dated for rail, plain ServiceJourney for bus). NULL exactly when alt_scored is FALSE."),
  alt_stop_order        INT64     OPTIONS(description="Position of that boarding call along the offered journey (1 = origin). Completes the join key with alt_operator, alt_jkey and DATE(alt_departure_time, 'Europe/Oslo'). NULL exactly when alt_scored is FALSE."),
  -- Both wordings, side by side, since 2026-08-07. VARSEL_LLM_ENABLED used to decide whether a
  -- written wording EXISTED, so the deployment whose pushes reach real travellers (prd, with it
  -- false) produced none and there was nothing to compare its own text against. It is now a
  -- send-time selector: both wordings are produced for every alert-worthy decision, `message` above
  -- is whichever one went out, and these two are what it was chosen between -- so the two can be
  -- read against each other on the traffic of the environment that actually pushes.
  message_llm           STRING    OPTIONS(description="The wording the model wrote for this decision, whether or not it was the one sent. NULL means no model wording exists for this row — below the alert gate, push off, generation off, capped, retries exhausted, refused, ungrounded (message_status says which) — OR the row predates this column, which message_deterministic is what distinguishes. 'Written but withheld by policy' is exactly message_source='deterministic' AND message_llm IS NOT NULL. Caveat when message_status='ok_cached': this text was written from an EARLIER cycle's message_input while message_deterministic beside it is THIS cycle's, so any same-input comparison of the two must filter on message_status='ok'."),
  message_deterministic STRING    OPTIONS(description="The template wording (varsel._deterministic_body) for this decision, computed on EVERY decision from 2026-08-07 — including the ~83 % sub-threshold rows nothing is ever sent for — whether or not it was the one delivered. Its presence is what DATES a row as post-change: message_llm cannot, since a NULL there means either 'no model wording' or 'written before the column existed', and at deploy time every existing row is the latter. NULL therefore reads as 'not recorded then', never 'no wording' — the same third-bucket rule /historikk states for n_unsegmented. Written from the same message_input the model was prompted with, or from the alert's own figures when that could not be built, which is why this can be non-null beside a NULL message_input. Under message_status='ok_cached' it describes a different cycle's input than message_llm does."),
  -- The webhook channel, recorded here for the first time (U16, 2026-09-13). KTD10 kept client
  -- routes out of this table until /historikk and /meldinger were behind authentication; they are
  -- (Entur SSO, 2026-09-10), and at a hundred thousand commuters "unscored" would have been the
  -- whole product. What did NOT move is the grain: a row is still one journey DEFINITION's view of
  -- one situation on one train, and a definition is a route, a window and a weekday set. No
  -- subscription id, client id, destination or external reference is recorded here or anywhere
  -- downstream of it -- test_notification_contract greps this file for exactly those.
  channel               STRING    OPTIONS(description="Which channel(s) watched this journey definition when the row was written: 'browser' (a /varsel registration), 'webhook' (a client subscription only) or 'both'. Added 2026-09-13 (KTD4) -- webhook routes were not recorded at all before it, so NULL means the row predates the column, never 'no channel'. It is aggregated onto the subject and is NOT in varsel_decision_groups' GROUP BY: a definition both channels share is ONE subject with one matching decision, which is the premise of the shared match and the shared wording. Three things follow. `pushed` means a notification was ISSUED on any channel, so a 'both' row can say pushed with no Web Push behind it. `n_devices` stays browser-only and `n_subscriptions` counts the client side, so neither is a count of the other's recipients. And the two LLM delivery switches are separate, so where a rollout has them disagreeing, `message` on a 'both' row is the browser's wording and the client received the other one."),
  n_subscriptions       INT64     OPTIONS(description="How many live webhook subscriptions shared this journey definition when the row was written -- the client side's count, beside n_devices' browser side. 0 on a browser-only row; NULL means the row predates the column (2026-09-13). No client identity is recorded anywhere in this table (KTD10): the grain is the journey DEFINITION, which is a route and a window and names nobody.")
)
PARTITION BY DATE(decided_at)
CLUSTER BY operator, situation_number
OPTIONS(
  description="Append-only log of Avviksvarsel alert decisions. Labelled with realized outcomes by data/queries/label_varsel_decisions.sql. Never CREATE OR REPLACE; never copied dev->prd.",
  -- A YEAR, and this DELETES data: a partition older than this is dropped by BigQuery and is gone.
  -- Deliberate, and sized against what reads the table rather than against sentiment. /historikk
  -- and /meldinger bound every window at historikk._MAX_WINDOW_DAYS (180), the labelling job at 10
  -- days, and the two data/queries side-by-sides at 30, so a year is more than twice the longest
  -- question anything asks. Without it the table is unbounded, and R12's bytes cap on a public page
  -- has to be sized against a table that only ever grows: 1.5 GB at 226 definitions (prd,
  -- 2026-09-11) and 14-33 GB a DAY at 35 000-80 000 before thinning.
  --
  -- Reaches a FRESH environment from here and an existing one only through the ALTER in the
  -- best-effort section (CREATE TABLE IF NOT EXISTS does nothing to a table that exists). It is in
  -- the forgiven half on purpose: a table-metadata rate limit must never leave `ensure_table`
  -- reporting "not ready", which is the 2026-08-07 mode where the decision log records nothing at
  -- all. A retention rule that lands a quarter of an hour late costs nothing.
  partition_expiration_days=365
);


-- llm_payload -> message_input, for tables written before the object had three consumers.
--
-- It was named for the LLM prompt when that was all it fed; it is now also what the deterministic
-- template words the push from, so the column name says input rather than payload. RENAME preserves
-- the rows -- the alternative, adding a second column, would leave the recorded inputs split across
-- two names with no way to tell which decisions used which.
--
-- Guarded by INFORMATION_SCHEMA rather than by `RENAME COLUMN IF EXISTS`, which is NOT idempotent
-- here: measured 2026-08-05, once the rename has happened the same statement fails with "Column
-- already exists: message_input" -- BigQuery rejects the TARGET name before it ever considers the
-- IF EXISTS on the source. This file runs on every startup, and decisions.ensure_table treats any
-- failure in it as "table not ready" and stops recording decisions entirely, so a statement that
-- works exactly once is a decision log that dies on the second deploy.
--
-- MUST stay ahead of the ADD COLUMN block below: adding message_input first would leave the rename
-- with nowhere to go and split the recorded inputs across two columns. The EXECUTE IMMEDIATE keeps
-- it one statement with no inner semicolon, which is what validate_varsel_sql.sh's splitter (and
-- therefore its per-statement dry run) needs. Verified against a table with an active streaming
-- buffer -- the live table always has one.
EXECUTE IMMEDIATE IF(
  EXISTS (SELECT 1 FROM `disruptions.INFORMATION_SCHEMA.COLUMNS`
          WHERE table_name = "varsel_decisions" AND column_name = "llm_payload"),
  "ALTER TABLE `disruptions.varsel_decisions` RENAME COLUMN llm_payload TO message_input",
  "SELECT 1"
);


-- Schema additions for tables created before those columns existed.
--
-- Necessary because the statement above is IF NOT EXISTS: where the table already exists it does
-- nothing at all, so a new column in that definition would silently never appear -- and the write
-- path would then log "N of M rows rejected by BigQuery" on every poller cycle. ADD COLUMN IF NOT
-- EXISTS is idempotent and free, so this runs unremarkably at every startup, in both environments,
-- and needs no operator step. Adding a nullable column never rewrites existing rows: rows written
-- before this landed keep NULLs, which read correctly as "not recorded then", not "no payload".
ALTER TABLE `disruptions.varsel_decisions`
  ADD COLUMN IF NOT EXISTS obs_cancelled      BOOL      OPTIONS(description="ET had declared the latest observed call on this line cancelled."),
  ADD COLUMN IF NOT EXISTS obs_stop_name      STRING    OPTIONS(description="Where that observation was made (name, falling back to the StopPlace ref)."),
  ADD COLUMN IF NOT EXISTS obs_delayed_min    INT64     OPTIONS(description="Minutes late at that stop; null if not recorded."),
  ADD COLUMN IF NOT EXISTS obs_observed_at    TIMESTAMP OPTIONS(description="Aimed time of the observed call (UTC) — how old the observation was when quoted."),
  ADD COLUMN IF NOT EXISTS has_alternative    BOOL      OPTIONS(description="Whether a replacement journey was offered to the traveller. NOT 'a faster way': the fallback tier legitimately arrives later than the disrupted train was due (see alt_arrives_before_original). FALSE covers three unlike states — a lookup that found nothing, a selection that kept the traveller on their own train, and no lookup at all — which is what alt_status separates."),
  ADD COLUMN IF NOT EXISTS alt_minutes_saved  INT64     OPTIONS(description="SIGNED minutes earlier this arrives than the disrupted train's PREDICTED arrival. Negative is a legitimate answer, not a bad row: a replacement for a cancelled train is worth offering even when it gets the traveller there later. Informational — nothing is dropped for being too slow."),
  ADD COLUMN IF NOT EXISTS alt_departure_time TIMESTAMP OPTIONS(description="Expected departure of the alternative (UTC)."),
  ADD COLUMN IF NOT EXISTS alt_arrival_time   TIMESTAMP OPTIONS(description="Expected arrival of the alternative (UTC)."),
  ADD COLUMN IF NOT EXISTS alt_lines          STRING    OPTIONS(description="Its transit legs as shown to the traveller, e.g. 'R11, 100'; walking legs omitted."),
  ADD COLUMN IF NOT EXISTS message_status     STRING    OPTIONS(description="Why that source: ok | ok_cached | not_alertworthy | not_selected | push_disabled | llm_disabled | capped | attempts_exhausted | transient_exhausted | rejected | ungrounded | error | unavailable. Without it every failure mode looks like one deterministic row. TWO give-up statuses since 2026-08-22, judging different things: attempts_exhausted the PAYLOAD (asked, and refused/over-ran/invented a figure), transient_exhausted the ENVIRONMENT (backend reached and failed repeatedly). One counter served both before, charged on EVERY None including paths that asked no backend -- so read a pre-2026-08-22 attempts_exhausted as transient unless proven otherwise (in prd: 290 preceding errors, zero rejected/ungrounded), and compute no rate across the change. llm_disabled also changed meaning, on 2026-08-07: it means no backend could be ASKED (VARSEL_LLM_PROVIDER=off, no project, all latched off), never that the deployment sends the template -- that is message_source='deterministic' with a non-null message_llm, status ok."),
  ADD COLUMN IF NOT EXISTS llm_provider       STRING    OPTIONS(description="Backend that wrote the message: 'claude' | 'gemini'. Null when none did."),
  ADD COLUMN IF NOT EXISTS llm_model          STRING    OPTIONS(description="Model id behind that backend, e.g. 'claude-sonnet-5'."),
  ADD COLUMN IF NOT EXISTS prompt_version     STRING    OPTIONS(description="SHA-256[:8] of the system prompt in force."),
  ADD COLUMN IF NOT EXISTS message_input      STRING    OPTIONS(description="Exact JSON every message about this decision was written from (the model's prompt; the template's source when no model wrote one)."),
  ADD COLUMN IF NOT EXISTS et_cancelled       BOOL      OPTIONS(description="ET had ALREADY declared this boarding call cancelled when we decided, so impact_probability/cancel_probability are 1.0 by construction: the app RELAYED a known fact rather than predicting one. Segment on this rather than dropping the rows — the log answers two questions and no single figure serves both. SERVICE quality (was the traveller told the truth?) includes them, and should: relaying what ET already knows is most of the value, since nobody carries the ET feed on their phone. MODEL skill (did we see it coming?) excludes them: they are gimmes. Distrust either figure quoted alone, because the MIX of the two populations moves it with nothing about the app having changed."),
  ADD COLUMN IF NOT EXISTS alt_status         STRING    OPTIONS(description="What became of the alternative-journey lookup, recorded on EVERY decision including the ~83 % that are sub-threshold: offered | none_found | suppressed (the selection ran and kept the traveller on their own train) | not_worth_lookup (no lookup was issued — the impact gate, or a reading taken from a non-canonical stop ref). Reserved but not yet written, because impact.for_trip does not record them and they are indistinguishable from 'no lookup' downstream: beyond_horizon | error. Without this an offer rate has no denominator across the 2026-08-06 selection change, which moves the numerator sharply — the same trap et_cancelled exists to avoid. NULL = written before the column."),
  ADD COLUMN IF NOT EXISTS alt_arrives_before_original BOOL OPTIONS(description="The offered journey arrives strictly before the disrupted train's SCHEDULED arrival -- the traveller's own deadline, not the delayed train's expected arrival. FALSE is the fallback tier: nothing got there in time, and the traveller is better off late than stranded. Deliberately a different anchor from alt_minutes_saved, which measures against the PREDICTED arrival: one answers 'do I still make it', the other 'is this better than what I have'. Read them together, never either alone."),
  ADD COLUMN IF NOT EXISTS alt_at_risk        BOOL      OPTIONS(description="The offered journey is itself predicted to be in trouble (over its operator's alert gate, or on a line a live situation touches). Offered anyway because nothing safer reached the destination in time — these are the rows where a wording that promises a clean escape would be wrong."),
  ADD COLUMN IF NOT EXISTS alt_scored         BOOL      OPTIONS(description="The prediction cache held the offered journey's boarding call, so its probabilities are model output. FALSE means nothing scored it — the ordinary case, and NOT 'no risk'."),
  ADD COLUMN IF NOT EXISTS alt_operator       STRING    OPTIONS(description="Ref codespace of the offered journey's boarding call (VYG, GOA, RUT, ...), from the prediction cache and never from the Journey Planner's line codespace — a JP codespace would fill the column and join to nothing. NULL exactly when alt_scored is FALSE."),
  ADD COLUMN IF NOT EXISTS alt_jkey           STRING    OPTIONS(description="Journey ref of the offered journey's boarding call, minus its codespace (dated for rail, plain ServiceJourney for bus). NULL exactly when alt_scored is FALSE."),
  ADD COLUMN IF NOT EXISTS alt_stop_order     INT64     OPTIONS(description="Position of that boarding call along the offered journey (1 = origin). Completes the join key with alt_operator, alt_jkey and DATE(alt_departure_time, 'Europe/Oslo'). NULL exactly when alt_scored is FALSE."),
  ADD COLUMN IF NOT EXISTS message_llm        STRING    OPTIONS(description="The wording the model wrote for this decision, whether or not it was the one sent. NULL means no model wording exists for this row — below the alert gate, push off, generation off, capped, retries exhausted, refused, ungrounded (message_status says which) — OR the row predates this column, which message_deterministic is what distinguishes. 'Written but withheld by policy' is exactly message_source='deterministic' AND message_llm IS NOT NULL. Caveat when message_status='ok_cached': this text was written from an EARLIER cycle's message_input while message_deterministic beside it is THIS cycle's, so any same-input comparison of the two must filter on message_status='ok'."),
  ADD COLUMN IF NOT EXISTS message_deterministic STRING OPTIONS(description="The template wording (varsel._deterministic_body) for this decision, computed on EVERY decision from 2026-08-07 — including the ~83 % sub-threshold rows nothing is ever sent for — whether or not it was the one delivered. Its presence is what DATES a row as post-change: message_llm cannot, since a NULL there means either 'no model wording' or 'written before the column existed', and at deploy time every existing row is the latter. NULL therefore reads as 'not recorded then', never 'no wording' — the same third-bucket rule /historikk states for n_unsegmented. Written from the same message_input the model was prompted with, or from the alert's own figures when that could not be built, which is why this can be non-null beside a NULL message_input. Under message_status='ok_cached' it describes a different cycle's input than message_llm does."),
  ADD COLUMN IF NOT EXISTS channel            STRING    OPTIONS(description="Which channel(s) watched this journey definition when the row was written: 'browser' (a /varsel registration), 'webhook' (a client subscription only) or 'both'. Added 2026-09-13 (KTD4) -- webhook routes were not recorded at all before it, so NULL means the row predates the column, never 'no channel'. It is aggregated onto the subject and is NOT in varsel_decision_groups' GROUP BY: a definition both channels share is ONE subject with one matching decision, which is the premise of the shared match and the shared wording. Three things follow. `pushed` means a notification was ISSUED on any channel, so a 'both' row can say pushed with no Web Push behind it. `n_devices` stays browser-only and `n_subscriptions` counts the client side, so neither is a count of the other's recipients. And the two LLM delivery switches are separate, so where a rollout has them disagreeing, `message` on a 'both' row is the browser's wording and the client received the other one."),
  ADD COLUMN IF NOT EXISTS n_subscriptions    INT64     OPTIONS(description="How many live webhook subscriptions shared this journey definition when the row was written -- the client side's count, beside n_devices' browser side. 0 on a browser-only row; NULL means the row predates the column (2026-09-13). No client identity is recorded anywhere in this table (KTD10): the grain is the journey DEFINITION, which is a route and a window and names nobody.");



-- The labelled counterpart: one row per decision subject, joined to what actually happened.
-- Written nightly by data/queries/label_varsel_decisions.sql; created HERE, at startup, because
-- /historikk queries it on every request and would 503 until the first nightly run otherwise.
-- IF NOT EXISTS, never CREATE OR REPLACE: a verdict is as unrebuildable as the decision it judges.
CREATE TABLE IF NOT EXISTS `disruptions.varsel_decision_outcomes`
(
  journey_digest         STRING,
  from_stop              STRING,
  to_stop                STRING,
  situation_number       STRING,
  summary                STRING,
  description            STRING,
  operator               STRING,
  line                   STRING,
  mode                   STRING,
  jkey                   STRING,
  call_date              DATE,
  stop_place             STRING,
  stop_order             INT64,
  departure_time         TIMESTAMP,

  n_decisions            INT64   OPTIONS(description="How many ROWS the decision log holds for this journey x situation x departure -- not how many refresh cycles scored it, and the two stopped being the same thing on 2026-09-12. A standing subject nothing has changed about is now written at most once per quarter hour (varsel._thinning_reason); every row the figures beside this are computed from is still written, which is why first_alert_at, max_impact_probability, the final_* block and the quoted wording are unchanged. Compute no per-cycle rate across that date."),
  first_decided_at       TIMESTAMP,
  last_decided_at        TIMESTAMP,
  ever_alerted           BOOL    OPTIONS(description="Whether ANY cycle crossed the threshold — the decision the verdict judges."),
  first_alert_at         TIMESTAMP OPTIONS(description="When we first crossed the threshold; null if we never did."),
  alert_lead_min         INT64   OPTIONS(description="Minutes between first_alert_at and departure — how much warning the traveller got."),
  max_impact_probability FLOAT64 OPTIONS(description="Highest alerting score reached across the cycles."),
  final_impact_probability FLOAT64,
  final_cancel_probability FLOAT64,
  final_delay_p50_min    INT64,
  final_delay_p90_min    INT64,
  alert_threshold        FLOAT64,
  pushed_any             BOOL    OPTIONS(description="Whether a notification was ISSUED on any channel for this decision: a Web Push the push service accepted, or a webhook event that was minted (2026-09-13, KTD4). Web Push alone before that date. Read `channel` beside it -- a 'both' subject can say TRUE with no Web Push behind it."),
  n_devices              INT64,
  message                STRING  OPTIONS(description="The wording the traveller was actually PUSHED for this decision, copied from varsel_decision_groups; when none was ever pushed (pushed_any = FALSE) it is the last wording produced instead, which is what /historikk marks 'ikke levert'. Deliberately not 'the last text produced', which it was until 2026-08-21: a wording is cached only while the situation stays above the alert gate (varsel._reconcile_caches), so a subject that eased off records the template on every later cycle and the card quoted text the traveller never received -- 41 % of dev's alerted subjects when it was measured, a tenth of them under the wrong writer's name."),
  message_source         STRING  OPTIONS(description="Which wording was SENT: 'llm' or 'deterministic'. Since 2026-08-07 'deterministic' covers two unlike states — no model wording existed, and one existed and was withheld by policy (prd's setting). This table cannot separate them: varsel_decisions.message_llm is what does, and it is not copied here."),

  actual_cancelled       BOOL    OPTIONS(description="ET declared the boarding call cancelled."),
  actual_dep_delay_min   INT64   OPTIONS(description="Realized departure delay in minutes; null if the call was never operated."),
  actual_trouble         BOOL    OPTIONS(description="cancelled OR >= 10 min late. NULL = unknown (no realized call found), NOT 'no trouble'."),
  verdict                STRING  OPTIONS(description="'hit' | 'false_alarm' | 'miss' | 'correct_reject'; NULL when the outcome is unknown."),
  labelled_at            TIMESTAMP,
  -- New columns go LAST here, matching where ALTER ADD COLUMN puts them on an existing table.
  -- Otherwise a fresh environment and an upgraded one hold the same columns in different orders,
  -- and anything positional -- label_varsel_decisions.sql's INSERT was exactly that until it grew
  -- an explicit column list -- writes each environment's data into a different column.
  et_cancelled           BOOL    OPTIONS(description="ET had already declared this boarding call cancelled at the point this verdict judges (the first alerting cycle if we alerted, any cycle if we did not): we RELAYED a known fact rather than predicting one. Segment on it, do not filter: service quality includes these and should, model skill excludes them as gimmes. NULL = the decision predates the column and belongs to neither segment."),
  -- The webhook channel's two columns, frozen onto the verdict exactly as n_devices is (U16,
  -- 2026-09-13). Adding them is what forces the scheduled query to be re-registered in BOTH
  -- environments in the same operation as this DDL -- the transfer config holds a COPY of the SQL
  -- and its INSERT names its columns, so the old text inserts one column short and the nightly job
  -- fails outright until it is updated.
  channel                STRING  OPTIONS(description="Which channel(s) watched this journey definition, aggregated over the subject's rows: 'browser', 'webhook' or 'both' -- 'both' when any row said so, or when browser rows and webhook rows both occur. NOT part of the grain (KTD4): a definition both channels share is one subject with one verdict. NULL = every row predates the column (2026-09-13)."),
  n_subscriptions        INT64   OPTIONS(description="The highest webhook-subscription count any of this subject's rows carried, frozen here like n_devices beside it. 0 on a browser-only subject; NULL = the rows predate the column.")
)
PARTITION BY call_date
CLUSTER BY operator, verdict
OPTIONS(description="Avviksvarsel alert decisions joined to realized SIRI-ET outcomes. Built nightly by data/queries/label_varsel_decisions.sql. Never copied dev->prd — each environment labels its own decisions.");


-- Same reason as the ADD COLUMN block above the decision log: the CREATE is IF NOT EXISTS, so a
-- column added to that definition never reaches an environment whose outcomes table already exists.
-- Here the symptom would be the nightly job failing outright rather than dropping rows, since its
-- INSERT names the column.
ALTER TABLE `disruptions.varsel_decision_outcomes`
  ADD COLUMN IF NOT EXISTS et_cancelled BOOL OPTIONS(description="ET had already declared this boarding call cancelled at the point this verdict judges (the first alerting cycle if we alerted, any cycle if we did not): we RELAYED a known fact rather than predicting one. Segment on it, do not filter: service quality includes these and should, model skill excludes them as gimmes. NULL = the decision predates the column and belongs to neither segment."),
  ADD COLUMN IF NOT EXISTS channel         STRING OPTIONS(description="Which channel(s) watched this journey definition, aggregated over the subject's rows: 'browser', 'webhook' or 'both' -- 'both' when any row said so, or when browser rows and webhook rows both occur. NOT part of the grain (KTD4): a definition both channels share is one subject with one verdict. NULL = every row predates the column (2026-09-13)."),
  ADD COLUMN IF NOT EXISTS n_subscriptions INT64  OPTIONS(description="The highest webhook-subscription count any of this subject's rows carried, frozen here like n_devices beside it. 0 on a browser-only subject; NULL = the rows predate the column.");


-- One row per decision SUBJECT: a journey definition's view of one situation on one train.
-- The per-cycle rows above collapse here into the decision that was actually made — did we ever
-- alert, how early, and what did we say.
--
-- A TABLE FUNCTION since 2026-09-12, with the view below it as a windowless wrapper — one body, two
-- ways in. CREATE OR REPLACE rather than IF NOT EXISTS for both, for two reasons. Neither holds
-- data, so replacing them is free and neither can drift from the definition in this file. And this
-- grouping has SIX consumers — the three /historikk queries, the nightly labelling job,
-- backfill_outcome_pushed_message.sql and message_wording_comparison.sql — which MUST group
-- identically: the labelling job writes a verdict keyed on this grain, and the API looks decisions
-- up by it. Two hand-maintained copies of a GROUP BY would eventually disagree, and the symptom
-- would be rows that never match their own verdict.
--
-- WHY THE PARAMETERS (R12). `varsel_decisions` is PARTITION BY DATE(decided_at) and every consumer
-- bounds `call_date` instead, which cannot prune a partition. A VIEW cannot take the window --
-- views take no parameters, and the windowed readers ask for different ones -- so the body lives
-- here, in a table function the caller passes its own partition cover to.
--
-- WHAT IT IS MEASURED TO BUY, and it is less than the paragraph above implies, so read this before
-- quoting a saving. Real runs against dev on 2026-09-12 (804 235 rows / 854 MB, 14-day window,
-- cache off), view vs function: `historikk_filters.sql` 158.9 MB -> 104.4 MB (-34 %),
-- `historikk_summary.sql` 88.7 MB -> 78.8 MB (-11 %), `historikk_decisions.sql` UNCHANGED. In
-- isolation the mechanism is worth far more -- `SELECT COUNT(*)` through the view is 116.2 MB and
-- through a four-day cover 12.5 MB (-89 %) -- and the gap between those two readings is the point:
-- BigQuery prunes BLOCKS on any column from its own columnar metadata, rows land here in
-- `decided_at` order, and `call_date` tracks `decided_at` to within a day, so the `call_date`
-- predicate the readers already carried was doing most of this work by accident. That accident is a
-- property of the DATA, not a guarantee: it weakens as the log grows, and the thinning rule
-- (KTD15) changes the write pattern it rests on. The partition cover makes the bound explicit and
-- survives both. Do not remove it on the strength of the -0 % reading.
--
-- THE COVER IS DERIVED, and derived covers fail silently and cheaply (the lesson
-- meldinger_decisions.sql states at length: a too-narrow one drops cycles out of the aggregates,
-- which reads as a subject that was never alerted on, and the byte counts a validator prints report
-- the loss as a CHEAPER query). Bound, measured in dev 2026-08-22 over 117 448 rows: a decision is
-- made from 720 min before its departure to 54 min after (`catchable_after` keeps a delayed train
-- catchable), `call_date` is the Oslo date of the departure, and Oslo is UTC+1/+2. So
-- DATE(decided_at) lies in [call_date - 1, call_date], and the caller's `call_date` range widened
-- by a day either side covers it with the +1 side as pure margin. RE-MEASURE BEFORE WIDENING THE
-- 12 H HORIZON: at 24 h the lower bound stops covering the span.
--
-- Adding a column to the TABLE above is free; adding one HERE is not, and the asymmetry is easy to
-- get backwards. label_varsel_decisions.sql does `SELECT *` over the view, and the registered
-- varsel-label-daily scheduled query holds a COPY of that SQL in each environment — so a column
-- added here changes the shape that copy sees, and the fix is an operator step in two projects
-- rather than an edit. Because this SELECT names every column explicitly, the four alt_* columns
-- added to varsel_decisions on 2026-08-06 reach the log without touching the nightly job at all.
-- That is the whole reason they were added there and not projected here. The move to a table
-- function deliberately changed NEITHER the column set nor the GROUP BY, for the same reason: the
-- nightly job's copy would otherwise need re-registering in two projects.
CREATE OR REPLACE TABLE FUNCTION `disruptions.varsel_decision_groups_between`(
  d_start DATE, d_end DATE
) AS (
-- Wrapped in an outer SELECT for one reason: `message` and `message_source` must come from ONE row
-- (see `delivered` below), and two separate aggregates picking "the same" row is a promise the
-- ordering cannot keep. `* EXCEPT` rather than a re-listed projection so a column added to the
-- inner SELECT still reaches consumers untouched -- it does move message/message_source to the end
-- of the column order, which is safe because every reader names its columns
-- (label_varsel_decisions.sql's INSERT, historikk_decisions.sql, historikk_summary.sql) and none
-- of them is positional.
SELECT
  * EXCEPT (delivered),
  delivered.text   AS message,
  delivered.source AS message_source
FROM (
SELECT
  journey_digest,
  operator,
  jkey,
  call_date,
  stop_order,
  situation_number,
  ANY_VALUE(from_stop)       AS from_stop,
  ANY_VALUE(to_stop)         AS to_stop,
  ANY_VALUE(summary)         AS summary,
  ANY_VALUE(description)     AS description,
  ANY_VALUE(line)            AS line,
  ANY_VALUE(mode)            AS mode,
  ANY_VALUE(stop_place)      AS stop_place,
  ANY_VALUE(departure_time)  AS departure_time,
  -- ROWS WRITTEN, not cycles scored, and the two stopped being the same thing on 2026-09-12.
  -- `varsel._record_decisions` thins a standing subject to one row per quarter hour (KTD15) plus
  -- every row a figure here is computed from: the first sighting, each alert-worthy/sub-threshold
  -- transition, each new maximum probability, each push, and a closing row carrying the last look.
  -- So this counts how often the subject was worth writing down, and a rate computed across that
  -- date is measuring two different things. The other figures below are unchanged BY CONSTRUCTION,
  -- which is what the rule is built around -- see the trigger list on `varsel._thinning_reason`.
  COUNT(*)                   AS n_decisions,
  MIN(decided_at)            AS first_decided_at,
  -- The closing row is dated at the cycle it DESCRIBES (the subject's last observation), not at the
  -- cycle that noticed the subject was gone, so this still reads as the last look we got.
  MAX(decided_at)            AS last_decided_at,
  LOGICAL_OR(alertworthy)    AS ever_alerted,
  MIN(IF(alertworthy, decided_at, NULL)) AS first_alert_at,
  -- Was this a relayed fact or a prediction? The split /historikk reports beside its blended
  -- precision, and the reason the per-cycle column exists at all.
  --
  -- Taken AT THE POINT THE VERDICT JUDGES: the first alerting cycle where we alerted, any cycle
  -- where we did not. Emphatically NOT a plain LOGICAL_OR over the subject, which would be
  -- backwards -- a cancellation is routinely declared AFTER we alerted, since alerting before ET
  -- publishes is the whole point of the app, so LOGICAL_OR would file the stack's best wins
  -- (predicted 10:00, ET confirms 14:17, train cancelled) as gimmes and hand the model's credit to
  -- the feed. The fallback covers the other side: a subject we stayed silent on has no alerting
  -- cycle, and there "ET declared it at some point and we said nothing anyway" is exactly the miss
  -- worth separating out.
  --
  -- NULL where no cycle recorded it -- rows written before 2026-08-05, which is all of them today.
  -- The API keeps those out of BOTH segments rather than defaulting them to FALSE: most were
  -- predictions, but the ones that were not are precisely the misses this column was added to find.
  COALESCE(
    ARRAY_AGG(IF(alertworthy, et_cancelled, NULL) IGNORE NULLS ORDER BY decided_at LIMIT 1)[SAFE_OFFSET(0)],
    LOGICAL_OR(et_cancelled)
  ) AS et_cancelled,
  MAX(impact_probability)    AS max_impact_probability,
  LOGICAL_OR(pushed)         AS pushed_any,
  MAX(n_devices)             AS n_devices,
  -- The channel, AGGREGATED onto the subject and deliberately not in the GROUP BY below (KTD4).
  -- Grouping on it would split a definition both channels share into two subjects -- two verdicts,
  -- two cards, two rows in every rate -- when what happened is one match, one wording and one
  -- decision. So it is a property of the subject, and 'both' is what a subject that was ever
  -- shared reads: either a single row already said so, or the subject has browser rows and webhook
  -- rows from different cycles (a client subscribed to a corridor a traveller was already
  -- watching, or a registration lapsed under a client's).
  --
  -- NULL survives as its own answer, the n_unsegmented rule one column over: every row written
  -- before 2026-09-13 carries no channel, and reading those as 'browser' would be a guess that
  -- happens to be right today and would silently mislabel any environment backfilled later.
  CASE
    WHEN LOGICAL_OR(channel = 'both')
      OR (LOGICAL_OR(channel = 'browser') AND LOGICAL_OR(channel = 'webhook')) THEN 'both'
    WHEN LOGICAL_OR(channel = 'webhook') THEN 'webhook'
    WHEN LOGICAL_OR(channel = 'browser') THEN 'browser'
  END                        AS channel,
  MAX(n_subscriptions)       AS n_subscriptions,
  ANY_VALUE(alert_threshold) AS alert_threshold,
  -- "Final" = the last cycle that scored it: the closest look we got before departure.
  ARRAY_AGG(impact_probability ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS final_impact_probability,
  ARRAY_AGG(cancel_probability ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS final_cancel_probability,
  ARRAY_AGG(delay_p50_min      ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS final_delay_p50_min,
  ARRAY_AGG(delay_p90_min      ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS final_delay_p90_min,
  -- THE WORDING THAT WAS PUSHED, falling back to the last one produced when none ever was.
  --
  -- The card quotes this as what the traveller was sent, so it has to be that row and not merely
  -- the most recent one. Those differ routinely, because a wording lives in `_messages` only while
  -- the situation stays above the gate: `varsel._reconcile_caches` calls `_forget_message` the tick
  -- a subject eases off below it, so every later cycle records the template again and a plain
  -- "last cycle with text" showed the template for a subject the model had written and the push had
  -- carried. Measured on dev 2026-08-21 over 1 790 alerted subjects: 52 quoted the template under
  -- "Skrevet av modellen" though the push carried the model's prose, 85 quoted the reverse, and 603
  -- more quoted the right KIND of wording from the wrong cycle -- different percentages, a different
  -- replacement train. 740 in all: 41 % of the page was text nobody received.
  --
  -- Ordered ASC among pushed rows -- the FIRST delivery, which is the one `first_alert_at` and
  -- `alert_lead_min` beside it describe, so the quoted text and the quoted warning time belong to
  -- the same cycle. (More than one pushed row is possible: `_notified` is keyed per journey_id, so
  -- a second device registering the same journey later pushes again.)
  --
  -- Both halves skip rows with no text at all, which is what the old IGNORE NULLS was for: a
  -- pushed row whose message column is NULL (rows written before it was populated) must fall
  -- through to the second half rather than blank the card.
  --
  -- One aggregate, not two: `message` and `message_source` are a pair, and a row showing one
  -- cycle's text under another cycle's attribution is exactly the defect this fixes.
  COALESCE(
    ARRAY_AGG(IF(pushed AND message IS NOT NULL, STRUCT(message AS text, message_source AS source), NULL)
              IGNORE NULLS ORDER BY decided_at LIMIT 1)[SAFE_OFFSET(0)],
    ARRAY_AGG(IF(message IS NOT NULL, STRUCT(message AS text, message_source AS source), NULL)
              IGNORE NULLS ORDER BY decided_at DESC LIMIT 1)[SAFE_OFFSET(0)]
  ) AS delivered
FROM `disruptions.varsel_decisions`
-- THE PARTITION PREDICATE, and the only difference between this body and the one that ran until
-- 2026-09-12. It is on DATE(decided_at), the partitioning column, which is what makes a request
-- read its own window instead of the whole table.
WHERE DATE(decided_at) BETWEEN d_start AND d_end
GROUP BY journey_digest, operator, jkey, call_date, stop_order, situation_number
)
);


-- The windowless way in, for the readers that have no window of their own to pass.
--
-- Three of the six consumers are here: the nightly labelling job (whose registered copy does
-- `SELECT *` over this NAME and must keep seeing a view with exactly these columns — changing it is
-- an operator step in two projects, not an edit), backfill_outcome_pushed_message.sql and
-- message_wording_comparison.sql. All three are cheap or one-off. The three /historikk queries call
-- the table function directly with their own cover.
--
-- The range is a constant pair wide enough to be "everything": the log starts in 2026 and the table
-- drops partitions past a year, so nothing outside it can exist. Deliberately NOT a rolling
-- CURRENT_DATE window, which would prune more and would also silently change what "the view" means
-- from one day to the next — a reader asking for 180 days would get a different answer depending on
-- when the question was asked.
CREATE OR REPLACE VIEW `disruptions.varsel_decision_groups` AS
SELECT * FROM `disruptions.varsel_decision_groups_between`(DATE '2020-01-01', DATE '2999-12-31');


-- ==============================================================================================
-- BEST-EFFORT SECTION -- everything below this marker is documentation, not structure.
--
-- decisions.ensure_table() splits this file HERE and runs the two halves as separate jobs. The
-- half above must succeed for the decision log to be usable; a failure below is logged and the
-- table is still marked ready. That split is not tidiness, it is the fix for an outage this file
-- caused on 2026-08-07: BigQuery caps table-metadata updates at 5 per 10 seconds per table, and
-- the SET OPTIONS block below had grown to 7 statements which, with the ADD COLUMN block above,
-- reliably exceeded it ("Exceeded rate limits: too many table update operations for this table").
-- ensure_table treats ANY failure in this file as "table not ready" and returns False, so
-- decisions.record() then writes nothing at all -- a cosmetic description update would have
-- silently taken down the entire alert-decision log on the next deploy.
--
-- The block only ever grows: every reworded description adds a permanent statement, because a
-- fresh environment gets its text from the CREATE bodies above and only an EXISTING table needs
-- carrying. Statements here whose text has demonstrably reached both dev and prd are dead weight
-- and can be pruned; until someone verifies that, the split is what keeps the cost bounded. That
-- verification was done on 2026-09-12 (eight statements to four) and again on 2026-09-13, when
-- U16 needed two of the slots back -- see the note below.
-- ==============================================================================================

-- Descriptions for columns that ALREADY exist.
--
-- Neither statement above can do this. CREATE TABLE IF NOT EXISTS does nothing at all where the
-- table exists, and ADD COLUMN IF NOT EXISTS does nothing where the column exists -- so an edited
-- OPTIONS(description=...) reaches a fresh environment and NO existing one. Measured 2026-08-05:
-- dev's `impact_probability` still carried its pre-et_cancelled text hours after the deploy that
-- reworded it, and `et_cancelled` itself was frozen with the description it was born with. The
-- file and the table then disagree, and the table is what anyone reads in the console or in
-- `bq show --schema` -- which for the columns below is the difference between reading `pushed`
-- as a Web Push and reading it as a notification on either channel.
--
-- SET OPTIONS is idempotent and free (metadata only, no rewrite), so it runs at every startup like
-- the block above. One statement per column: BigQuery allows a single ALTER COLUMN action per
-- ALTER TABLE. Descriptions are duplicated from the definitions above; keep them in step.
-- NEWEST FIRST in this block, which is the opposite of the "new things go last" rule the CREATE and
-- ADD COLUMN blocks above follow -- and the inversion is deliberate. BigQuery caps table-metadata
-- updates at 5 per 10 s per table; when the cap bites it truncates the TAIL, so ordering newest-last
-- would drop exactly the statements that have not reached an environment yet while re-applying ones
-- that already landed -- silent and perfectly counterproductive. Newest-first means a truncated run
-- loses only re-applications. The block stood at 7 statements against that cap of 5, i.e. it could
-- not complete at all; after the 2026-09-13 prune it is 3 on varsel_decisions and 1 on the outcomes
-- table, which is inside the cap on both. Prune again before adding a fifth to either.
--
-- The real fix is to prune: a statement here exists solely to carry reworded text to a table that
-- already exists, so once it has reached dev AND prd it is dead weight (a fresh environment reads
-- its descriptions from the CREATE body). Verify with `bq show --schema` in both projects, then
-- delete rather than letting the block grow past the cap again.

-- PRUNED TWICE: eight statements on 2026-09-12 (U4 step 5) and four more on 2026-09-13 (U16).
--
-- Verified rather than assumed both times, which is what the paragraph above asks for. The second
-- prune compared each statement's text against `bq show --schema` in BOTH ent-kixavvik-dev and
-- ent-kixavvik-prd programmatically, byte for byte: varsel_decisions' alt_status, decided_at and
-- pushed and varsel_decision_outcomes' n_decisions were identical in both projects, so all four
-- were carrying text that had already arrived. ent-kixavvik-tst holds no such table at all, so a
-- statement here can only ever serve dev and prd. The first prune's eight were varsel_decisions'
-- message_status, message_source, et_cancelled, impact_probability, cancel_probability,
-- has_alternative and alt_minutes_saved plus varsel_decision_outcomes' message.
--
-- What that buys is the room this unit needs: `pushed` and `n_devices` are reworded below because
-- KTD4 changes what they MEAN, and without the prune the block would stand at six against a cap of
-- five and complete for nobody.

-- Retention (U4, 2026-09-12). Carries the CREATE body's partition_expiration_days to the tables
-- that already exist — the whole point of this block, and the one statement here that is not a
-- description. It goes FIRST because this block is newest-first and a truncated run must lose only
-- re-applications, and it is below the marker rather than above it because a table-metadata rate
-- limit must never make `ensure_table` report "not ready": that is the 2026-08-07 mode, where the
-- decision log records nothing at all. A retention rule that lands one poller tick late costs
-- nothing. IT DELETES DATA once it lands: partitions older than a year go, and are gone.
--
-- Kept rather than pruned although it HAS landed in both projects (31 536 000 000 ms in each,
-- checked 2026-09-13): it is not a description, so a fresh environment reads it from the CREATE
-- body and an environment restored from a backup taken before U4 would not. One slot against that
-- is worth paying.
ALTER TABLE `disruptions.varsel_decisions`
  SET OPTIONS(partition_expiration_days=365);

-- The two columns KTD4 changed the MEANING of (U16, 2026-09-13). Neither changed type; what
-- changed is that `pushed` now answers for two channels and `n_devices` answers for one of them.
-- Both are read by somebody looking at the schema in the console to decide what a rate is over, so
-- a table still saying "Web Push" while the column counts minted webhook events is the 2026-08-05
-- failure shape: the file and the table disagree and the table is what gets read.
ALTER TABLE `disruptions.varsel_decisions`
  ALTER COLUMN pushed SET OPTIONS(description="Whether a notification was ISSUED for THIS decision -- this journey definition, this situation, this departure -- in the cycle the row was written: a Web Push the push service accepted, or, since 2026-09-13 (KTD4), a webhook event this cycle MINTED. The mint and not the delivery: a delivered mark lags the mint by up to two cycles, so a subject retired in between would read as never notified. Read `channel` beside it -- a 'both' row can say pushed with no Web Push behind it. Read rows before 2026-09-12 with care: the delivery loop marked pushes per (definition, situation) while rows are per (definition, situation, departure), so a push about one train could mark another departure under the same situation.");

ALTER TABLE `disruptions.varsel_decisions`
  ALTER COLUMN n_devices SET OPTIONS(description="How many /varsel registrations share this journey definition -- the BROWSER side only, and it stays that way now that webhook routes are recorded too (2026-09-13, KTD4). A webhook-only row therefore reads 0 here beside a non-zero n_subscriptions, and neither column counts the other's recipients.");

-- The outcomes table's own budget: BigQuery's 5-updates-per-10s cap is PER TABLE, and this is the
-- only statement against this one.
ALTER TABLE `disruptions.varsel_decision_outcomes`
  ALTER COLUMN pushed_any SET OPTIONS(description="Whether a notification was ISSUED on any channel for this decision: a Web Push the push service accepted, or a webhook event that was minted (2026-09-13, KTD4). Web Push alone before that date. Read `channel` beside it -- a 'both' subject can say TRUE with no Web Push behind it.");
