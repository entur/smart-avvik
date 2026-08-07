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
-- One row per (journey definition x situation x departure) per refresh cycle -- including
-- decisions that did NOT alert, which is what makes a miss observable rather than invisible.
-- Duplicates across cycles are expected and intended: they are the lead-time series.
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
  decided_at            TIMESTAMP NOT NULL OPTIONS(description="When this match was evaluated (UTC)."),
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
  mode                  STRING    OPTIONS(description="Vehicle noun shown to the traveller: 'Tog' or 'Buss'."),
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
  pushed                BOOL      OPTIONS(description="Whether a Web Push was actually delivered for this decision in this cycle."),
  n_devices             INT64     OPTIONS(description="How many registrations share this journey definition."),

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
  message_status        STRING    OPTIONS(description="Why that source: ok | ok_cached | not_alertworthy | not_selected | push_disabled | llm_disabled | capped | attempts_exhausted | rejected | ungrounded | error | unavailable. Without it every failure mode looks like the same deterministic row. NOTE llm_disabled changed meaning on 2026-08-07 when generation and delivery became separate switches: it now means no backend could be ASKED (VARSEL_LLM_PROVIDER=off, no GCP project, every backend latched off after a 404), never 'this deployment sends the template' — that case is message_source='deterministic' with a non-null message_llm, and it carries status ok. Rows written before that date use the old, conflated reading."),
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
  alt_status            STRING    OPTIONS(description="What became of the alternative-journey lookup, recorded on EVERY decision including the ~83 % that are sub-threshold: offered | none_found | suppressed (the selection ran and kept the traveller on their own train) | not_worth_lookup (no lookup was issued — the impact gate, or a reading taken from a non-canonical stop ref). Reserved but not yet written, because impact.for_trip does not record them and they are indistinguishable from 'no lookup' downstream: beyond_horizon | error. Without this an offer rate has no denominator across the 2026-08-06 selection change, which moves the numerator sharply — the same trap et_cancelled exists to avoid. NULL = written before the column."),
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
  message_deterministic STRING    OPTIONS(description="The template wording (varsel._deterministic_body) for this decision, computed on EVERY decision from 2026-08-07 — including the ~83 % sub-threshold rows nothing is ever sent for — whether or not it was the one delivered. Its presence is what DATES a row as post-change: message_llm cannot, since a NULL there means either 'no model wording' or 'written before the column existed', and at deploy time every existing row is the latter. NULL therefore reads as 'not recorded then', never 'no wording' — the same third-bucket rule /historikk states for n_unsegmented. Written from the same message_input the model was prompted with, or from the alert's own figures when that could not be built, which is why this can be non-null beside a NULL message_input. Under message_status='ok_cached' it describes a different cycle's input than message_llm does.")
)
PARTITION BY DATE(decided_at)
CLUSTER BY operator, situation_number
OPTIONS(description="Append-only log of Avviksvarsel alert decisions. Labelled with realized outcomes by data/queries/label_varsel_decisions.sql. Never CREATE OR REPLACE; never copied dev->prd.");


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
  ADD COLUMN IF NOT EXISTS message_status     STRING    OPTIONS(description="Why that source: ok | ok_cached | not_alertworthy | not_selected | push_disabled | llm_disabled | capped | attempts_exhausted | rejected | ungrounded | error | unavailable. Without it every failure mode looks like the same deterministic row. NOTE llm_disabled changed meaning on 2026-08-07 when generation and delivery became separate switches: it now means no backend could be ASKED (VARSEL_LLM_PROVIDER=off, no GCP project, every backend latched off after a 404), never 'this deployment sends the template' — that case is message_source='deterministic' with a non-null message_llm, and it carries status ok. Rows written before that date use the old, conflated reading."),
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
  ADD COLUMN IF NOT EXISTS message_deterministic STRING OPTIONS(description="The template wording (varsel._deterministic_body) for this decision, computed on EVERY decision from 2026-08-07 — including the ~83 % sub-threshold rows nothing is ever sent for — whether or not it was the one delivered. Its presence is what DATES a row as post-change: message_llm cannot, since a NULL there means either 'no model wording' or 'written before the column existed', and at deploy time every existing row is the latter. NULL therefore reads as 'not recorded then', never 'no wording' — the same third-bucket rule /historikk states for n_unsegmented. Written from the same message_input the model was prompted with, or from the alert's own figures when that could not be built, which is why this can be non-null beside a NULL message_input. Under message_status='ok_cached' it describes a different cycle's input than message_llm does.");



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

  n_decisions            INT64   OPTIONS(description="How many refresh cycles scored this journey x situation x departure."),
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
  pushed_any             BOOL    OPTIONS(description="Whether a Web Push was actually delivered for this decision."),
  n_devices              INT64,
  message                STRING  OPTIONS(description="The last message text produced for this decision."),
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
  et_cancelled           BOOL    OPTIONS(description="ET had already declared this boarding call cancelled at the point this verdict judges (the first alerting cycle if we alerted, any cycle if we did not): we RELAYED a known fact rather than predicting one. Segment on it, do not filter: service quality includes these and should, model skill excludes them as gimmes. NULL = the decision predates the column and belongs to neither segment.")
)
PARTITION BY call_date
CLUSTER BY operator, verdict
OPTIONS(description="Avviksvarsel alert decisions joined to realized SIRI-ET outcomes. Built nightly by data/queries/label_varsel_decisions.sql. Never copied dev->prd — each environment labels its own decisions.");


-- Same reason as the ADD COLUMN block above the decision log: the CREATE is IF NOT EXISTS, so a
-- column added to that definition never reaches an environment whose outcomes table already exists.
-- Here the symptom would be the nightly job failing outright rather than dropping rows, since its
-- INSERT names the column.
ALTER TABLE `disruptions.varsel_decision_outcomes`
  ADD COLUMN IF NOT EXISTS et_cancelled BOOL OPTIONS(description="ET had already declared this boarding call cancelled at the point this verdict judges (the first alerting cycle if we alerted, any cycle if we did not): we RELAYED a known fact rather than predicting one. Segment on it, do not filter: service quality includes these and should, model skill excludes them as gimmes. NULL = the decision predates the column and belongs to neither segment.");


-- One row per decision SUBJECT: a journey definition's view of one situation on one train.
-- The per-cycle rows above collapse here into the decision that was actually made — did we ever
-- alert, how early, and what did we say.
--
-- A view, and CREATE OR REPLACE rather than IF NOT EXISTS, for two reasons. It holds no data, so
-- replacing it is free and it can never drift from the definition in this file. And it has two
-- consumers — the nightly labelling job and /historikk-api — which MUST group identically: the
-- labelling job writes a verdict keyed on this grain, and the API looks decisions up by it. Two
-- hand-maintained copies of a GROUP BY would eventually disagree, and the symptom would be rows
-- that never match their own verdict.
--
-- Deliberately unfiltered by date: consumers add their own predicate, which pushes down into the
-- underlying partitioned table. Rows with a NULL jkey are kept — they can never be joined to a
-- realized outcome, but they are still decisions and belong on the page, marked unknown.
--
-- Adding a column to the TABLE above is free; adding one HERE is not, and the asymmetry is easy to
-- get backwards. label_varsel_decisions.sql does `SELECT *` over this view, and the registered
-- varsel-label-daily scheduled query holds a COPY of that SQL in each environment — so a column
-- added here changes the shape that copy sees, and the fix is an operator step in two projects
-- rather than an edit. Because this SELECT names every column explicitly, the four alt_* columns
-- added to varsel_decisions on 2026-08-06 reach the log without touching the nightly job at all.
-- That is the whole reason they were added there and not projected here.
CREATE OR REPLACE VIEW `disruptions.varsel_decision_groups` AS
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
  COUNT(*)                   AS n_decisions,
  MIN(decided_at)            AS first_decided_at,
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
  ANY_VALUE(alert_threshold) AS alert_threshold,
  -- "Final" = the last cycle that scored it: the closest look we got before departure.
  ARRAY_AGG(impact_probability ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS final_impact_probability,
  ARRAY_AGG(cancel_probability ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS final_cancel_probability,
  ARRAY_AGG(delay_p50_min      ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS final_delay_p50_min,
  ARRAY_AGG(delay_p90_min      ORDER BY decided_at DESC LIMIT 1)[OFFSET(0)] AS final_delay_p90_min,
  -- The last cycle that actually produced text, so a row shows what was said rather than NULL just
  -- because the final cycle re-used a cached message.
  ARRAY_AGG(message        IGNORE NULLS ORDER BY decided_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS message,
  ARRAY_AGG(message_source IGNORE NULLS ORDER BY decided_at DESC LIMIT 1)[SAFE_OFFSET(0)] AS message_source
FROM `disruptions.varsel_decisions`
GROUP BY journey_digest, operator, jkey, call_date, stop_order, situation_number;


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
-- and can be pruned; until someone verifies that, the split is what keeps the cost bounded.
-- ==============================================================================================

-- Descriptions for columns that ALREADY exist.
--
-- Neither statement above can do this. CREATE TABLE IF NOT EXISTS does nothing at all where the
-- table exists, and ADD COLUMN IF NOT EXISTS does nothing where the column exists -- so an edited
-- OPTIONS(description=...) reaches a fresh environment and NO existing one. Measured 2026-08-05:
-- dev's `impact_probability` still carried its pre-et_cancelled text hours after the deploy that
-- reworded it, and `et_cancelled` itself was frozen with the description it was born with. The
-- file and the table then disagree, and the table is what anyone reads in the console or in
-- `bq show --schema` -- which for THESE three columns is the difference between segmenting the
-- precision figure and quoting it whole.
--
-- SET OPTIONS is idempotent and free (metadata only, no rewrite), so it runs at every startup like
-- the block above. One statement per column: BigQuery allows a single ALTER COLUMN action per
-- ALTER TABLE. Descriptions are duplicated from the definitions above; keep them in step.
-- NEWEST FIRST in this block, which is the opposite of the "new things go last" rule the CREATE and
-- ADD COLUMN blocks above follow -- and the inversion is deliberate. BigQuery caps table-metadata
-- updates at 5 per 10 s per table and this block has 7 statements, so when the cap bites it truncates
-- the TAIL. Ordering newest-last would therefore drop exactly the descriptions that have not reached
-- an environment yet, while re-applying five that already landed: the failure would be silent and
-- perfectly counterproductive. Newest-first means a truncated run loses only re-applications.
--
-- The real fix is to prune: a statement here exists solely to carry reworded text to a table that
-- already exists, so once it has reached dev AND prd it is dead weight (a fresh environment reads
-- its descriptions from the CREATE body). Verify with `bq show --schema` in both projects, then
-- delete rather than letting the block grow past the cap again.

-- These two are the 2026-08-07 case: neither column changed, one value in
-- each did. Splitting VARSEL_LLM_ENABLED into a generation gate and a delivery selector left
-- message_source='deterministic' covering a second, new state — a model wording that exists and was
-- withheld — and moved llm_disabled from "the flag is false" to "no backend could be asked at all".
-- Both readings have to reach a dev and a prd table that already exist, and only SET OPTIONS can
-- carry them there. The values themselves are unchanged, so no row is rewritten and no consumer
-- (varsel_decision_groups, label_varsel_decisions.sql, /historikk-api) needs touching.
ALTER TABLE `disruptions.varsel_decisions`
  ALTER COLUMN message_source SET OPTIONS(description="Which wording was SENT: 'llm' when the model's text went out, 'deterministic' for the template. Since 2026-08-07 it says nothing about which wordings EXIST — both are written for every alert-worthy decision and recorded in message_llm/message_deterministic, and VARSEL_LLM_ENABLED selects the one delivered. So 'deterministic' now covers two unlike states: no model wording exists, and one exists and was withheld by policy (prd's setting today). Separate them with message_llm IS NOT NULL, never with this column alone.");

ALTER TABLE `disruptions.varsel_decisions`
  ALTER COLUMN message_status SET OPTIONS(description="Why that source: ok | ok_cached | not_alertworthy | not_selected | push_disabled | llm_disabled | capped | attempts_exhausted | rejected | ungrounded | error | unavailable. Without it every failure mode looks like the same deterministic row. NOTE llm_disabled changed meaning on 2026-08-07 when generation and delivery became separate switches: it now means no backend could be ASKED (VARSEL_LLM_PROVIDER=off, no GCP project, every backend latched off after a 404), never 'this deployment sends the template' — that case is message_source='deterministic' with a non-null message_llm, and it carries status ok. Rows written before that date use the old, conflated reading.");

ALTER TABLE `disruptions.varsel_decisions`
  ALTER COLUMN et_cancelled SET OPTIONS(description="ET had ALREADY declared this boarding call cancelled when we decided, so impact_probability/cancel_probability are 1.0 by construction: the app RELAYED a known fact rather than predicting one. Segment on this rather than dropping the rows — the log answers two questions and no single figure serves both. SERVICE quality (was the traveller told the truth?) includes them, and should: relaying what ET already knows is most of the value, since nobody carries the ET feed on their phone. MODEL skill (did we see it coming?) excludes them: they are gimmes. Distrust either figure quoted alone, because the MIX of the two populations moves it with nothing about the app having changed.");

ALTER TABLE `disruptions.varsel_decisions`
  ALTER COLUMN impact_probability SET OPTIONS(description="Calibrated P(cancelled OR >= 10 min late) — the alerting score; exactly 1.0 when et_cancelled, where it is a relayed fact and not a prediction.");

ALTER TABLE `disruptions.varsel_decisions`
  ALTER COLUMN cancel_probability SET OPTIONS(description="Calibrated P(the train is cancelled); exactly 1.0 when et_cancelled, where it is a relayed fact and not a prediction.");

-- These two are the 2026-08-06 case: the columns did not change, their MEANING did. The selection
-- rule no longer looks for a faster journey — it looks for the best remaining way there — so
-- "produced a faster way to to_stop" and "how much earlier it arrives" are both now false on the
-- face of them, and alt_minutes_saved is signed. Existing rows keep their values; what has to reach
-- dev and prd is the reading, and only these statements can carry it to a table that already exists.
ALTER TABLE `disruptions.varsel_decisions`
  ALTER COLUMN has_alternative SET OPTIONS(description="Whether a replacement journey was offered to the traveller. NOT 'a faster way': the fallback tier legitimately arrives later than the disrupted train was due (see alt_arrives_before_original). FALSE covers three unlike states — a lookup that found nothing, a selection that kept the traveller on their own train, and no lookup at all — which is what alt_status separates.");

ALTER TABLE `disruptions.varsel_decisions`
  ALTER COLUMN alt_minutes_saved SET OPTIONS(description="SIGNED minutes earlier this arrives than the disrupted train's PREDICTED arrival. Negative is a legitimate answer, not a bad row: a replacement for a cancelled train is worth offering even when it gets the traveller there later. Informational — nothing is dropped for being too slow.");
