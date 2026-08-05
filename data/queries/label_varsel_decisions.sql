-- Label Avviksvarsel's alert decisions with what actually happened to the train.
--
-- Registered as the bq scheduled query "varsel-label-daily" (every day 04:00 UTC, between
-- "demo-recent-daily" at 03:30 and "demo-copy-daily" at 04:30). Reads the append-only decision log
-- disruptions/api/create_varsel_decisions.sql writes, joins each decision to the realized call, and
-- appends a verdict row. Safe to re-run: rows already labelled are skipped (NOT EXISTS), so a
-- missed night backfills itself on the next run.
--
-- SOURCE: ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded — the
-- all-operator per-call final-state table. Extraction recipe is create_bus_calls.sql's: dedup on
-- the latest recordedAtTime, and null the "actuals" on rows still flagged estimated so an
-- un-departed call cannot masquerade as an on-time one.
--
-- COST: ~$0.01/run — measured, not estimated: `bq query --dry_run` reports 1.53 GB scanned
-- (2026-08-01, empty decision log). Nearly all of it is the ET side: an 11-day operatingDate
-- slice, partition-pruned, cluster-pruned on dataSource to the operators we serve, and reading
-- only the eight columns below. That column pruning is why this is an order of magnitude under
-- extend_demo_recent_daily.sql's ~$0.2 for 27 days, which carries far more of the row.
-- The ET side is fixed; the decision-log side grows with the log, so re-measure if it matters.
--
-- The decision-log side is a FULL scan, not a partitioned one: varsel_decisions is partitioned on
-- DATE(decided_at) but this query selects on call_date, and the grouping sits behind a view, so
-- there is no partition predicate to push down. That is a deliberate trade — a handful of rows per
-- poller cycle means the whole log is a few hundred MB after a year, well under a cent per run,
-- and sharing one grouping definition with /historikk-api is worth more than the pruning. If the
-- log ever grows enough to matter, add a decided_at bound to the view and pass it through.
--
-- WHY call_date <= yesterday: last_recorded is fresh THROUGH YESTERDAY, not same-day. Labelling a
-- call before its operating day is complete would read "not yet recorded" as "did not happen".
-- Each decision group therefore gets its one shot the day after it ran, with a 10-day window of
-- slack so a missed run still catches up.
--
-- OPERATOR TAGGING — the trap in this join. extend_demo_recent_daily.sql:61 remaps
-- dataSource='VYG' to 'NSB' because the demo tables and the SX publisher use NSB. This query must
-- NOT: varsel derives its operator from the line ref ('VYG:Line:R11' -> 'VYG', main.py:410), so
-- that is what the decision log holds. Remapping here would make every rail decision join zero
-- rows, and the result would look like "no outcome yet" rather than like a bug.
--
-- HONEST NULLS. A decision that finds no realized call gets actual_trouble = NULL and
-- verdict = NULL, never FALSE/'correct_reject'. The rail slice of last_recorded runs 10-17 %
-- thinner than the anshar extraction (calls the producer never confirmed are missing,
-- extend_demo_recent_daily.sql:14-16), so treating an unmatched join as "nothing went wrong" would
-- quietly manufacture correct rejections and inflate every precision figure computed downstream.

-- The outcomes table itself is created at app startup, alongside the decision log it is
-- derived from (disruptions/api/create_varsel_decisions.sql). One definition, in the one
-- place that always runs first: rows can only exist here because the app wrote decisions,
-- and /historikk queries this table on every request, so it has to exist before the first
-- nightly run rather than being conjured by it.


-- The delete/re-insert pair is ONE transaction. Each DML statement otherwise commits on its own,
-- so a DELETE that succeeded followed by an INSERT that failed would leave those subjects with no
-- outcome row at all -- self-healing on the next run only while they stay inside the 10-day window,
-- and permanently lost past it. ROLLBACK on error keeps yesterday's labels rather than half of them.
BEGIN TRANSACTION;

-- Clear out unresolved rows in the window before re-labelling them. A subject whose realized call
-- had not landed yet was written with actual_trouble = NULL; without this it would keep that NULL
-- forever, because the INSERT's NOT EXISTS guard would see a row and skip it. Deleting only the
-- unresolved ones keeps every settled verdict immutable while letting "unknown" become known.
DELETE FROM `disruptions.varsel_decision_outcomes`
WHERE actual_trouble IS NULL
  AND call_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY)
                    AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY);


-- The column list is explicit on purpose. Without it the INSERT maps POSITIONALLY onto whatever
-- order the outcomes table happens to hold -- and that order differs between an environment created
-- fresh (the CREATE TABLE's order) and one upgraded by ALTER ADD COLUMN (appended at the end). A
-- column added in the middle of the CREATE would therefore write correct rows in one environment
-- and silently shifted ones in the other, with no error anywhere. Keep it in step with
-- disruptions/api/create_varsel_decisions.sql; a name that does not exist there fails loudly, which
-- is the point.
INSERT INTO `disruptions.varsel_decision_outcomes` (
  journey_digest, from_stop, to_stop,
  situation_number, summary, description,
  operator, line, mode, jkey, call_date, stop_place, stop_order, departure_time,
  n_decisions, first_decided_at, last_decided_at,
  ever_alerted, first_alert_at, alert_lead_min,
  max_impact_probability, final_impact_probability, final_cancel_probability,
  final_delay_p50_min, final_delay_p90_min, alert_threshold,
  pushed_any, n_devices, message, message_source,
  actual_cancelled, actual_dep_delay_min, actual_trouble, verdict, labelled_at,
  et_cancelled
)
WITH
-- The decision subjects to label. The grouping itself lives in the
-- disruptions.varsel_decision_groups view (disruptions/api/create_varsel_decisions.sql) because
-- /historikk-api reads the same grain: a second copy of the GROUP BY here would eventually
-- disagree with it, and a verdict keyed on a grain the API does not share is a verdict nothing
-- can display.
grouped AS (
  SELECT *
  FROM `disruptions.varsel_decision_groups`
  WHERE call_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 10 DAY)
                      AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    -- No jkey means no join key, so no verdict is possible. Such rows stay in the view (the page
    -- shows them as unknown) but there is nothing for this job to resolve.
    AND jkey IS NOT NULL
),
-- Realized calls. operatingDate is bracketed one day wider than the call_date window on each side:
-- the operating day and the Oslo calendar date of the aimed departure diverge around midnight.
et AS (
  SELECT
    REGEXP_EXTRACT(serviceJourneyId, r'^([A-Za-z]+):')   AS operator,
    REGEXP_REPLACE(serviceJourneyId, r'^[A-Za-z]+:', '') AS jkey,
    sequenceNr                                           AS stop_order,
    DATE(COALESCE(aimedDepartureTime, aimedArrivalTime), 'Europe/Oslo') AS call_date,
    (COALESCE(journeyCancellation, FALSE) OR COALESCE(stopCancellation, FALSE)) AS call_cancelled,
    aimedDepartureTime AS aimed_dep,
    -- An "estimated" row is a prediction from the producer, not an observation. Nulling it is what
    -- keeps an un-departed call from being scored as punctual.
    IF(COALESCE(estimated, FALSE), NULL, departureTime) AS actual_dep
  FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded`
  WHERE operatingDate BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 11 DAY) AND CURRENT_DATE()
    AND dataSource IN ('VYG','GOA','KOL','SOF','NOR','SKY','ATB','OST','FIN','MOR','RUT','TRO','GCO')
    AND stopPointRef IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY serviceJourneyId, operatingDate, sequenceNr
    ORDER BY recordedAtTime DESC
  ) = 1
),
realized AS (
  SELECT
    operator, jkey, stop_order, call_date, call_cancelled,
    TIMESTAMP_DIFF(actual_dep, aimed_dep, MINUTE) AS dep_delay_min
  FROM et
  -- Realized outcomes only: cancelled, or actually operated. Anything else has not happened yet as
  -- far as this table is concerned, and must stay unknown rather than become "fine".
  WHERE call_cancelled OR actual_dep IS NOT NULL
),
labelled AS (
  SELECT
    g.*,
    r.call_cancelled AS actual_cancelled,
    IF(r.call_cancelled, NULL, r.dep_delay_min) AS actual_dep_delay_min,
    CASE
      WHEN r.operator IS NULL THEN NULL          -- no realized call found: unknown, not "no trouble"
      WHEN r.call_cancelled THEN TRUE
      -- No COALESCE to 0 here, deliberately. A matched call whose departure delay is still NULL
      -- (aimedDepartureTime absent, so the delay could not be computed) is UNKNOWN, not punctual.
      -- `NULL >= 10` is NULL in BigQuery, so such a row falls through as NULL exactly like the
      -- unmatched branch above. Defaulting it to 0 would have made it read as "no trouble" and
      -- manufactured a correct_reject out of missing data -- the one thing this table must never do.
      ELSE r.dep_delay_min >= 10                 -- same bar as varsel.TROUBLE_DELAY_MIN
    END AS actual_trouble
  FROM grouped AS g
  LEFT JOIN realized AS r
    ON  r.operator   = g.operator
    AND r.jkey       = g.jkey
    AND r.stop_order = g.stop_order
    -- Rail/GOA refs are DatedServiceJourney — date-scoped, so the jkey is unique on its own and the
    -- date is redundant. Bus ServiceJourney jkeys repeat every operating day, so there the date is
    -- load-bearing (demo_journeys.sql:11-15). Allowing rail to match without it means a boundary
    -- disagreement about which Oslo date a near-midnight departure belongs to costs nothing.
    AND (r.call_date = g.call_date OR STARTS_WITH(g.jkey, 'DatedServiceJourney:'))
)
SELECT
  journey_digest, from_stop, to_stop,
  situation_number, summary, description,
  operator, line, mode, jkey, call_date, stop_place, stop_order, departure_time,
  n_decisions, first_decided_at, last_decided_at,
  ever_alerted, first_alert_at,
  TIMESTAMP_DIFF(departure_time, first_alert_at, MINUTE) AS alert_lead_min,
  max_impact_probability, final_impact_probability, final_cancel_probability,
  final_delay_p50_min, final_delay_p90_min, alert_threshold,
  pushed_any, n_devices, message, message_source,
  actual_cancelled, actual_dep_delay_min, actual_trouble,
  CASE
    -- Both NULL guards are load-bearing, and neither may fall through to the ELSE. An unknown
    -- outcome is unknown; an all-NULL `ever_alerted` (no decision row carried a verdict-able flag)
    -- says nothing about whether we alerted. Letting either reach ELSE would label it
    -- 'correct_reject' -- inventing a correct decision out of absent data.
    WHEN actual_trouble IS NULL OR ever_alerted IS NULL THEN NULL
    WHEN ever_alerted AND actual_trouble       THEN 'hit'
    WHEN ever_alerted AND NOT actual_trouble   THEN 'false_alarm'
    WHEN NOT ever_alerted AND actual_trouble   THEN 'miss'
    ELSE 'correct_reject'
  END AS verdict,
  CURRENT_TIMESTAMP() AS labelled_at,
  -- Carried straight from the view, which decides what it means (relayed fact vs prediction, taken
  -- at the cycle the verdict judges). Resolving it here instead would put a second definition of
  -- the split next to the one /historikk-api reads, and the two would drift.
  et_cancelled
FROM labelled AS l
-- Idempotent, but only for subjects that actually RESOLVED. A subject labelled while ET had not yet
-- caught up carries actual_trouble = NULL; skipping it here too would freeze that "unknown" forever,
-- even once the realized call lands the next night. Those rows are deleted just above and rewritten
-- here, so an unknown is retried every run until it resolves or ages out of the window.
WHERE NOT EXISTS (
  SELECT 1
  FROM `disruptions.varsel_decision_outcomes` AS o
  WHERE o.journey_digest   = l.journey_digest
    AND o.operator         = l.operator
    AND o.jkey             = l.jkey
    AND o.call_date        = l.call_date
    AND o.stop_order       = l.stop_order
    AND o.situation_number = l.situation_number
    AND o.actual_trouble IS NOT NULL
);

COMMIT TRANSACTION;
