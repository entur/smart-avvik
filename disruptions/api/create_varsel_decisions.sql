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
  et_cancelled          BOOL      OPTIONS(description="ET had ALREADY declared this boarding call cancelled, so the two probabilities below are 1.0 by construction and this decision is a certainty rather than a prediction. Exclude these rows before quoting model precision."),
  impact_probability    FLOAT64   OPTIONS(description="Calibrated P(cancelled OR >= 10 min late) — the alerting score; exactly 1.0 when et_cancelled."),
  cancel_probability    FLOAT64   OPTIONS(description="Calibrated P(the train is cancelled); exactly 1.0 when et_cancelled."),
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

  -- The faster alternative offered, if any. Flat here, in full inside message_input.
  has_alternative       BOOL      OPTIONS(description="Whether the Journey Planner lookup produced a faster way to to_stop."),
  alt_minutes_saved     INT64     OPTIONS(description="How much earlier it arrives than the disrupted train's predicted arrival."),
  alt_departure_time    TIMESTAMP OPTIONS(description="Expected departure of the alternative (UTC)."),
  alt_arrival_time      TIMESTAMP OPTIONS(description="Expected arrival of the alternative (UTC)."),
  alt_lines             STRING    OPTIONS(description="Its transit legs as shown to the traveller, e.g. 'R11, 100'; walking legs omitted."),

  -- What the traveller was told.
  message               STRING    OPTIONS(description="The message body sent (or that would have been sent)."),
  message_source        STRING    OPTIONS(description="'llm' when written by the model, 'deterministic' for the template fallback."),
  message_status        STRING    OPTIONS(description="Why that source: ok | ok_cached | not_alertworthy | not_selected | push_disabled | llm_disabled | capped | attempts_exhausted | rejected | ungrounded | error | unavailable. Without it every failure mode looks like the same deterministic row."),
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
  snapshot_refreshed_at TIMESTAMP OPTIONS(description="When the per-call prediction cache behind this decision was last rebuilt (UTC) — how stale the numbers were.")
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
  ADD COLUMN IF NOT EXISTS has_alternative    BOOL      OPTIONS(description="Whether the Journey Planner lookup produced a faster way to to_stop."),
  ADD COLUMN IF NOT EXISTS alt_minutes_saved  INT64     OPTIONS(description="How much earlier it arrives than the disrupted train's predicted arrival."),
  ADD COLUMN IF NOT EXISTS alt_departure_time TIMESTAMP OPTIONS(description="Expected departure of the alternative (UTC)."),
  ADD COLUMN IF NOT EXISTS alt_arrival_time   TIMESTAMP OPTIONS(description="Expected arrival of the alternative (UTC)."),
  ADD COLUMN IF NOT EXISTS alt_lines          STRING    OPTIONS(description="Its transit legs as shown to the traveller, e.g. 'R11, 100'; walking legs omitted."),
  ADD COLUMN IF NOT EXISTS message_status     STRING    OPTIONS(description="Why the message has the source it does: ok | ok_cached | not_alertworthy | not_selected | push_disabled | llm_disabled | capped | attempts_exhausted | rejected | ungrounded | error | unavailable."),
  ADD COLUMN IF NOT EXISTS llm_provider       STRING    OPTIONS(description="Backend that wrote the message: 'claude' | 'gemini'. Null when none did."),
  ADD COLUMN IF NOT EXISTS llm_model          STRING    OPTIONS(description="Model id behind that backend, e.g. 'claude-sonnet-5'."),
  ADD COLUMN IF NOT EXISTS prompt_version     STRING    OPTIONS(description="SHA-256[:8] of the system prompt in force."),
  ADD COLUMN IF NOT EXISTS message_input      STRING    OPTIONS(description="Exact JSON every message about this decision was written from (the model's prompt; the template's source when no model wrote one)."),
  ADD COLUMN IF NOT EXISTS et_cancelled       BOOL      OPTIONS(description="ET had ALREADY declared this boarding call cancelled, so impact_probability/cancel_probability are 1.0 by construction and this decision is a certainty rather than a prediction. Exclude these rows before quoting model precision.");


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
  message_source         STRING  OPTIONS(description="'llm' or 'deterministic'."),

  actual_cancelled       BOOL    OPTIONS(description="ET declared the boarding call cancelled."),
  actual_dep_delay_min   INT64   OPTIONS(description="Realized departure delay in minutes; null if the call was never operated."),
  actual_trouble         BOOL    OPTIONS(description="cancelled OR >= 10 min late. NULL = unknown (no realized call found), NOT 'no trouble'."),
  verdict                STRING  OPTIONS(description="'hit' | 'false_alarm' | 'miss' | 'correct_reject'; NULL when the outcome is unknown."),
  labelled_at            TIMESTAMP
)
PARTITION BY call_date
CLUSTER BY operator, verdict
OPTIONS(description="Avviksvarsel alert decisions joined to realized SIRI-ET outcomes. Built nightly by data/queries/label_varsel_decisions.sql. Never copied dev->prd — each environment labels its own decisions.");


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
