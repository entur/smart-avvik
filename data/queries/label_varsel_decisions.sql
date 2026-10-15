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
-- COST: ~$0.015/run — measured, not estimated: `bq query --dry_run` reports 2.30 GB in dev and in
-- prd alike (2026-09-23), and what is billed is the same figure (a read-only replay of this SELECT
-- billed 2.12 GB against a 2.12 GB dry run), so cluster pruning on dataSource saves next to nothing
-- here: the operators we serve are most of the table. 0.19 GB of it is the decision-log side; the
-- rest is the ET side, an 11-day operatingDate slice reading only the columns the `et` CTE names.
-- That column pruning is why this stays an order of magnitude under extend_demo_recent_daily.sql's
-- ~$0.2 for 27 days, which carries far more of the row. The removed-train branch added 0.26 GB
-- (2.04 -> 2.30 GB): lineRef and vehicleMode across the whole slice, plus the stop registry.
-- It was 1.53 GB on 2026-08-01 with an empty log; the ET slice drifts with the feed and the log
-- side grows with the log, so re-measure if it matters.
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
-- The same thinness decides the removed-train branch (`removed_subjects` below), where the error
-- runs the other way: a train missing from ET counts as a hit only where ET carried its line.

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
  et_cancelled,
  -- New names go LAST here, matching where ALTER ADD COLUMN puts them on a table that already
  -- exists. The two arrived on 2026-09-13 (U16), and adding them is why this file's registered
  -- COPY has to be updated in both environments in the same operation as the DDL: run the deploy
  -- first so the columns exist, then `bq update --transfer_config` in dev and prd. The old stored
  -- text keeps working until then -- it simply inserts two columns short, leaving them NULL -- and
  -- the job fails outright the moment the new text runs against a table without the columns.
  channel, n_subscriptions
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
    -- SJ Norge (KIX-39) reaches last_recorded through Bane NOR as '<train number>:<date>', a ref with
    -- no codespace; the WHERE below admits only its rows from dataSource BNR, so BNR means SJN.
    IF(dataSource = 'BNR', 'SJN', REGEXP_EXTRACT(serviceJourneyId, r'^([A-Za-z]+):')) AS operator,
    REGEXP_REPLACE(serviceJourneyId, r'^[A-Za-z]+:', '') AS jkey,
    sequenceNr                                           AS stop_order,
    DATE(COALESCE(aimedDepartureTime, aimedArrivalTime), 'Europe/Oslo') AS call_date,
    (COALESCE(journeyCancellation, FALSE) OR COALESCE(stopCancellation, FALSE)) AS call_cancelled,
    aimedDepartureTime AS aimed_dep,
    -- An "estimated" row is a prediction from the producer, not an observation. Nulling it is what
    -- keeps an un-departed call from being scored as punctual.
    IF(COALESCE(estimated, FALSE), NULL, departureTime) AS actual_dep,
    -- Read by the removed-train branch only (see `removed_et` below); the existing join
    -- never looks at them.
    operatingDate, lineRef, stopPointRef, vehicleMode
  FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded`
  WHERE operatingDate BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 11 DAY) AND CURRENT_DATE()
    AND (dataSource IN ('VYG','GOA','KOL','SOF','NOR','SKY','ATB','OST','FIN','MOR','RUT','TRO','GCO')
         OR (dataSource = 'BNR' AND lineRef LIKE 'SJN:%'))
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
    -- SJN's '<train number>:<date>' is date-scoped too, but matching it on the date as well costs
    -- nothing: both sides take call_date from the same aimed time.
    AND (r.call_date = g.call_date OR STARTS_WITH(g.jkey, 'DatedServiceJourney:'))
  -- Removed-train subjects are labelled by their own branch below. The COALESCE keeps a row with no
  -- situation number here, so the two branches partition `grouped` exactly.
  WHERE NOT STARTS_WITH(COALESCE(g.situation_number, ''), 'KIX:PlanRemoved:')
),
-- TRAINS VY TOOK OUT OF THE PLAN (KIX:PlanRemoved:<jkey>, disruptions/api/rail_calls_removed_today.sql).
-- None of the join keys above exist for them: the jkey is `RemovedJourney:<train number>_<operating
-- date>`, the stop_order is GTFS `stop_sequence` (ET's is `sequenceNr`, a different count), and the
-- train never entered ET under any jkey. So they are labelled by TRAIN NUMBER on the jkey's operating
-- date, at the boarding STOP PLACE:
--   * TRUE  when ET ran the subject's line that day and has no journey with that number at all --
--           the removal held (decided from the second night, see `labelled_removed`);
--   * TRUE  when the train ran after all and its boarding call was cancelled or left >= 10 min late
--           (the existing branch's rule, same bar as varsel.TROUBLE_DELAY_MIN);
--   * FALSE when it ran and left the boarding stop under 10 min late -- Vy put it back;
--   * NULL  otherwise, and above all when ET did not carry the line that day. «Absent from ET» is
--           evidence only where ET had the line at all: the rail slice runs 10-17 % thin (HONEST NULLS
--           in the header), and a hit made of missing data is the reverse of the error the NULL rule
--           exists to prevent.
-- The operating date comes from the jkey, not call_date: a call after midnight carries the next
-- Oslo date while its train still belongs to the day it was removed from.
removed_subjects AS (
  SELECT
    *,
    REGEXP_EXTRACT(jkey, r'^RemovedJourney:(\d+)_') AS r_trainno,
    SAFE.PARSE_DATE('%Y-%m-%d', REGEXP_EXTRACT(jkey, r'_(\d{4}-\d{2}-\d{2})$')) AS r_od,
    -- The line CODE: last_recorded files some Vy journeys under a bare `L1` instead of
    -- `VYG:Line:L1` (the intraday pull of 2026-09-11), so both sides are compared on the code.
    REGEXP_EXTRACT(line, r'([^:]+)$') AS r_line
  FROM grouped
  WHERE STARTS_WITH(situation_number, 'KIX:PlanRemoved:')
),
-- Vy's TRAIN calls, once per train number. A through train Vy numbers twice (RE20's `382-110`) is
-- listed under both, exactly as rail_calls_removed_today.sql matches it: a removed row names one
-- number, and the train ran if ET has either. `vehicleMode = 'rail'` because the number pattern also
-- matches replacement buses whose ServiceJourney id is a UUID starting with eight digits (19 of them
-- in 11 days, measured 2026-09-23), and a bus is not the line's trains running. So a line ET carried
-- as replacement buses only reads as absent, and its removed trains stay NULL rather than hits.
removed_et AS (
  SELECT
    e.operatingDate AS od,
    n AS trainno,
    REGEXP_EXTRACT(e.lineRef, r'([^:]+)$') AS line,
    -- ET's stopPointRef is a QUAY; the subject names the parent stop place. Same resolution as
    -- disruptions/api/rail_calls_today.sql. GTFS parent_station, which the removed row carries,
    -- equalled this parent on every one of 7 889 replayed calls (2026-09-19..22).
    q.stopPlaceRef AS stop_place,
    e.call_cancelled, e.aimed_dep, e.actual_dep
  FROM et AS e,
    UNNEST([REGEXP_EXTRACT(e.jkey, r'ServiceJourney:(\d+)[-_]'),
            REGEXP_EXTRACT(e.jkey, r'ServiceJourney:\d+-(\d+)_')]) AS n
  LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
    ON q.id = e.stopPointRef
  WHERE e.operator = 'VYG' AND e.vehicleMode = 'rail' AND n IS NOT NULL
),
-- The two presence lookups are one row per key, so their LEFT JOINs cannot duplicate a subject;
-- `removed_boarding` can, and `labelled_removed`'s QUALIFY takes it back to one. That matters
-- because the INSERT's NOT EXISTS sees only the table, never the rows the same statement is writing.
removed_lines_ran AS (
  SELECT DISTINCT od, line FROM removed_et
),
-- Keyed without the line, on purpose. A number that also ran on ANOTHER line that day finds no
-- boarding call and is not absent either, so the subject is NULL and retried: the conservative
-- direction. Keying on the line would make a train re-added under a different line code read as
-- absent, i.e. a false hit. ET's line code matched GTFS's for every shared number over 2026-09-19..22.
removed_trains_ran AS (
  SELECT DISTINCT od, trainno FROM removed_et
),
-- The boarding call, on the same realized rule as `realized` above: cancelled, or actually departed.
-- EVERY candidate is kept. One number can carry two journeys in a day (Vy files a partial re-run as
-- `<n>_..._ExtraJourney`: 3 of 942 trains on 2026-09-22), and which one is the subject's train is
-- decided in `labelled_removed` on the subject's own departure time. Picking one here, blind to the
-- subject, let a cancelled or late ExtraJourney label an on-time train (or the reverse), and the
-- NOT EXISTS guard then froze that verdict.
removed_boarding AS (
  SELECT
    od, trainno, line, stop_place, call_cancelled, aimed_dep,
    TIMESTAMP_DIFF(actual_dep, aimed_dep, MINUTE) AS dep_delay_min
  FROM removed_et
  WHERE call_cancelled OR actual_dep IS NOT NULL
),
labelled_removed AS (
  SELECT
    g.* EXCEPT (r_trainno, r_od, r_line),
    CASE
      WHEN b.trainno IS NOT NULL THEN b.call_cancelled
      -- Same two-day guard as the absent arm in actual_trouble below; the two must not disagree.
      WHEN lr.line IS NOT NULL AND tr.trainno IS NULL
       AND g.r_od <= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY) THEN TRUE
    END AS actual_cancelled,
    IF(b.call_cancelled, NULL, b.dep_delay_min) AS actual_dep_delay_min,
    CASE
      -- It ran after all: judged on its boarding call exactly as the existing branch judges one,
      -- including a NULL delay staying NULL rather than reading as punctual.
      WHEN b.trainno IS NOT NULL THEN b.call_cancelled OR b.dep_delay_min >= 10
      -- The line ran and this number did not: the removal held. Decided only once the operating
      -- date is two days old, because absence is a verdict the NOT EXISTS guard then freezes, while
      -- a train Vy put back can have its ET rows land after the 04:00 UTC run (0-37 VYG rail rows a
      -- day did on 2026-09-19..22). The first night therefore leaves it NULL, deleted and retried;
      -- the second decides. A boarding call seen is evidence already, so that arm does not wait.
      WHEN lr.line IS NOT NULL AND tr.trainno IS NULL
       AND g.r_od <= DATE_SUB(CURRENT_DATE(), INTERVAL 2 DAY) THEN TRUE
      -- The line absent from ET, or the train present with no realized call at the boarding stop
      -- (on another line, a boarding call only estimated, or not recorded at all): unknown.
    END AS actual_trouble
  FROM removed_subjects AS g
  LEFT JOIN removed_boarding AS b
    ON  b.od = g.r_od AND b.trainno = g.r_trainno AND b.line = g.r_line
    AND b.stop_place = g.stop_place
  LEFT JOIN removed_lines_ran AS lr ON lr.od = g.r_od AND lr.line = g.r_line
  LEFT JOIN removed_trains_ran AS tr ON tr.od = g.r_od AND tr.trainno = g.r_trainno
  -- One row per subject (the view's grain), keeping the boarding candidate whose aimed departure is
  -- nearest the one the subject was decided on: `departure_time` is the aimed departure from the
  -- boarding stop, and a partial re-run of the same number leaves at a different time. A subject
  -- with no candidate keeps its one NULL-joined row. `b.aimed_dep` settles a candidate as far
  -- before as another is after (the earlier wins, as the old earliest-first pick did), so a re-run
  -- repeats the choice.
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY g.journey_digest, g.operator, g.jkey, g.call_date, g.stop_order, g.situation_number
    ORDER BY ABS(TIMESTAMP_DIFF(b.aimed_dep, g.departure_time, SECOND)) NULLS LAST, b.aimed_dep
  ) = 1
),
labelled_all AS (
  SELECT * FROM labelled
  UNION ALL
  SELECT * FROM labelled_removed
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
  et_cancelled,
  -- Same rule, one unit later: the view aggregates `channel` onto the subject ('both' where either
  -- a row said so or browser rows and webhook rows both occur) and takes the highest
  -- n_subscriptions, exactly as it takes the highest n_devices. Restating either here would be a
  -- second definition of which channel a shared definition belongs to.
  channel, n_subscriptions
FROM labelled_all AS l
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
