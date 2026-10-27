-- Vy and SJ Norge trains REMOVED from the day's plan, one row per call, in the shape of
-- rail_calls_planned_today (plan docs/plans/2026-09-23-001-feat-removed-trains-plan.md).
--
-- SJ NORGE since KIX-43 (2026-10-24). SJ does the same thing on its planned closures: 137 of 2 224
-- standing SJN train-days of 2026-09-25..10-21 were taken out of the plan, all on the Dovrebanen
-- (F6, 09-26..29) and Raumabanen (R65, 09-30..10-07) closures and their neighbours. Every rule
-- below runs PER OPERATOR, so one operator's timetable change cannot silence the other's
-- removals and one operator's half-written plan cannot refuse the other's (`guard`). Two things
-- differ: SJN's ET jkey is Bane NOR's '<number>:<date>', and SJN's GTFS has no bus trips at all,
-- so `bus_for_train` is Vy's alone -- only SJ's own SX can say «buss for tog». F6 and F7 close
-- boarding or alighting mid-journey (7 of 12 F6 and 1 of 8 F7 trains on 2026-10-24) and are left
-- out like F4, which leaves 87 of the 137 train-days; covering them needs each call's activities,
-- which GTFS does not carry.
--
-- From Saturday 2026-09-19 Vy has been taking trains out of the day's timetable instead of
-- cancelling them: 147 trains an ordinary Wednesday runs were missing on 2026-09-23, against 5
-- proper cancellations. Such a train never enters SIRI-ET, so rail_calls_planned_today -- and
-- with it the prediction cache, the «I dag» strip and every push -- cannot see it. This table is
-- the one place that can: GTFS against its own standing plan.
--
-- THE RULE is the strike reports' «Fjernet fra planen» and Rushvarsel's `standing` CTE
-- (togvarsel_outcomes_settled.sql), not a new one. A train number is REMOVED on @d when
--   * it was in the GTFS plan on at least 3 of the 4 previous same weekdays (standing),
--   * it is not in @d's GTFS plan under any line,
--   * ET does not carry it for @d (a train Vy put back is not removed), and
--   * no train NEW in @d's plan -- same line and direction, calling the removed train's origin
--     within 15 min of it -- stands in for it (a retimed or renumbered train is not a removal).
-- Train numbers: GTFS `ServiceJourney:<n>-...` or `_...`, ET's jkey
-- `DatedServiceJourney:<n>_..._<yy-mm-dd>`.
-- Both sides are matched on the NUMBER alone, as the reports match them: a number that moved line
-- is not a removal. A through train Vy numbers twice (RE20's `382-110`: 382 from Göteborg, 110 in
-- Norway) is present if EITHER number is: on 2026-09-16 Vy cut 382-110 back to a Halden - Oslo
-- `110`, and matching the first number alone read the train Norwegian travellers still had as
-- removed -- one on every ordinary weekday of the replay.
--
-- A REMOVED TRAIN'S CALLS are its own calls on the last date it ran, moved to @d by whole days in
-- Oslo local time. Never TIMESTAMP_ADD of 24 h multiples: DST ends 2026-10-25, inside the four
-- weeks the standing rule looks back over, and a shifted 07:10 would read 06:10.
--
-- LINES THAT CLOSE BOARDING OR ALIGHTING ANYWHERE MID-JOURNEY ARE LEFT OUT: F4 and RX11, measured
-- on train journeys 2026-09-23 (R31's 53 and RE30's 31 trains that day had no such call; the
-- NO_BOARDING the 2026-09-01 count found on them was their replacement buses'). GTFS carries no pickup or drop-off types, so it cannot say which calls a
-- removed train's passengers may use, and guessing «allowed» is the 2026-09-01 F4 incident: a
-- Bergen train pushed to Oslo S -> Drammen registrations nobody could have been aboard. Mid-journey
-- only, because every origin is NO_ALIGHTING and every terminus NO_BOARDING in the feed.
--
-- BUS FOR TRAIN. Vy files a replacement bus under the number of the train it replaces
-- (`ServiceJourney:BUS-2281_...` for train 2281), in GTFS and in ET alike, so `bus_for_train` is a
-- number match and nothing looser: on 2026-09-01 all 66 removed Gjøvikbanen trains had one, on
-- 2026-09-23 13 of 188 did. A covered train is still removed -- the traveller's train does not run
-- -- and the morning digest says a bus is set up. A bus some other operator runs (the L2 closure
-- of 2026-08-17) is not in Vy's plan and is not seen.
--
-- FOUR GUARDS. (1) No rows unless @d's GTFS partition holds Vy trains: without it every standing
-- train reads as removed. The partition is written days ahead (2026-09-23 held 09-24..09-27 at
-- gtfs_version 09-22), so this is a floor rather than the usual case; `gtfs_version` rides on every
-- row so the loader can log which plan it read. (2) The build FAILS, keeping the previous table,
-- when more than half the standing trains read as removed. A half-written partition would
-- otherwise look like a network-wide cut and push to every registration at 05:00; the worst real
-- day so far was ~13 %. ERROR(), not an empty table, so the refusal is logged by the poller phase
-- that ran it.
-- (3) No rows on a public holiday, nor on a date the calendar does not cover
-- (`disruptions.togvarsel_calendar`, Rushvarsel's, which also marks 24/12 and 31/12). Vy runs a holiday
-- plan against a weekday standing plan: Ascension Day 2026-05-14 read 396 trains as removed, under
-- the 50 % refusal, so all of them would have been published.
-- (4) No rows when more than a quarter of today's train numbers are not in the standing plan: that
-- is a new timetable period, not a cut. A cut takes trains away and adds few (5-8 % new numbers on
-- 2026-09-19..23, 2 % on the 2026-09-01 closure); a new plan renumbers (48-66 % on Maundy Thursday,
-- the summer timetable's first weeks and its last, when every normal-plan train is "new" against a
-- summer standing plan). Quiet rather than ERROR(): it recurs for weeks at every timetable change.
-- What neither guard can tell from a cut is a SEASONAL REDUCTION that keeps the numbers (Vy dropped
-- 59-131 standing trains on 2026-06-22..25, before the summer plan): those are published as removed.
--
-- WHO READS IT, and who must not (KTD1). predict_calls.sql relays it as KIX:PlanRemoved:<jkey>
-- certainties and main.refresh_planned_spine loads it into the strip's spine. The model's line
-- and network state features, Rushvarsel's outcome passes and varsel_connectivity.sql read
-- rail_calls_planned_today only: a removed row there would move every real train's forecast on
-- exactly the cut days, count each cut twice, and call a pair only removed trains serve «served».
--
-- @enabled FALSE (VARSEL_PLAN_REMOVED=off) builds the table with no rows, so every reader is
-- exactly as it was before the table existed. Cost ~0.3 GB per build (five GTFS partitions; 0.29 GB dry-run 2026-09-23).
CREATE OR REPLACE TABLE `disruptions.rail_calls_removed_today` AS
WITH
gtfs AS (
  SELECT
    operating_date AS od,
    IF(agency_id = 'SJN:Authority:SJN', 'SJN', 'VYG') AS operator,
    route_short_name AS line,
    direction_id,
    trip_id,
    REGEXP_EXTRACT(trip_id, r'ServiceJourney:(\d+)[-_]') AS trainno,
    REGEXP_EXTRACT(trip_id, r'ServiceJourney:\d+-(\d+)_') AS trainno2,
    stop_id AS quay,
    parent_station,
    stop_name,
    stop_sequence,
    departure_time,
    arrival_time,
    gtfs_version
  FROM `ent-data-sharing-ext-prd.timetable_gtfs.gtfs_last_recorded_ent_v1`
  WHERE operating_date IN (@d,
                           DATE_SUB(@d, INTERVAL 7 DAY), DATE_SUB(@d, INTERVAL 14 DAY),
                           DATE_SUB(@d, INTERVAL 21 DAY), DATE_SUB(@d, INTERVAL 28 DAY))
    AND (agency_id LIKE 'VYG%' OR agency_id = 'SJN:Authority:SJN')
    AND route_type = 'Train'
),
trips AS (
  SELECT DISTINCT od, operator, line, trainno, trainno2 FROM gtfs WHERE trainno IS NOT NULL
),
-- Every number @d's plan knows a train by (see the header on through trains).
today AS (
  SELECT DISTINCT operator, n AS trainno
  FROM trips, UNNEST([trainno, trainno2]) AS n
  WHERE od = @d AND n IS NOT NULL
),
standing AS (
  SELECT operator, line, trainno, ANY_VALUE(trainno2) AS trainno2, MAX(od) AS last_od
  FROM trips
  WHERE od < @d
  GROUP BY operator, line, trainno
  HAVING COUNT(DISTINCT od) >= 3
),
-- The trains ET carries for @d, by the operating date in the jkey rather than by call date: a
-- night train of @d-1 has calls on @d under the same number a removed @d train would have.
-- data/queries/replay_removed_trains.sh swaps this CTE for last_recorded, which has past dates.
et_trains AS (
  SELECT DISTINCT 'VYG' AS operator, n AS trainno
  FROM `disruptions.rail_calls_planned_today`,
    UNNEST([REGEXP_EXTRACT(jkey, r'ServiceJourney:(\d+)[-_]'),
            REGEXP_EXTRACT(jkey, r'ServiceJourney:\d+-(\d+)_')]) AS n
  WHERE SAFE.PARSE_DATE('%y-%m-%d', REGEXP_EXTRACT(jkey, r'_(\d{2}-\d{2}-\d{2})$')) = @d
    AND n IS NOT NULL
  UNION DISTINCT
  -- SJN: '<number>:<yyyy-mm-dd>', the date being the operating day, as for Vy above
  SELECT DISTINCT 'SJN', REGEXP_EXTRACT(jkey, r'^(\d+):')
  FROM `disruptions.rail_calls_planned_today`
  WHERE lineRef LIKE 'SJN:%'
    AND SAFE.PARSE_DATE('%Y-%m-%d', REGEXP_EXTRACT(jkey, r':(\d{4}-\d{2}-\d{2})$')) = @d
    AND REGEXP_CONTAINS(jkey, r'^\d+:')
),
present AS (
  SELECT operator, trainno FROM today
  UNION DISTINCT
  SELECT operator, trainno FROM et_trains
),
-- The train numbers a Vy bus-for-train trip stands in for on @d (see the header).
-- Vy's bus-for-train trips are `route_type = 'Train'` in GTFS (all 3 480 of them, 2026-08-24..09-27),
-- so they are in `gtfs` already.
bus_numbers AS (
  SELECT DISTINCT 'VYG' AS operator, REGEXP_EXTRACT(trip_id, r'ServiceJourney:BUS-(\d+)_') AS trainno
  FROM gtfs
  WHERE od = @d AND operator = 'VYG' AND REGEXP_CONTAINS(trip_id, r'ServiceJourney:BUS-\d+_')
  UNION DISTINCT
  SELECT DISTINCT 'VYG', REGEXP_EXTRACT(jkey, r'ServiceJourney:BUS-(\d+)_')
  FROM `disruptions.rail_calls_planned_today`
  WHERE SAFE.PARSE_DATE('%y-%m-%d', REGEXP_EXTRACT(jkey, r'_(\d{2}-\d{2}-\d{2})$')) = @d
    AND REGEXP_CONTAINS(jkey, r'ServiceJourney:BUS-\d+_')
),
missing AS (
  SELECT s.*
  FROM standing AS s
  LEFT JOIN present AS p1 ON p1.operator = s.operator AND p1.trainno = s.trainno
  LEFT JOIN present AS p2 ON p2.operator = s.operator AND p2.trainno = s.trainno2
  WHERE p1.trainno IS NULL AND p2.trainno IS NULL
  -- A number is one train per operating day; if it stood on two lines, the latest wins.
  QUALIFY ROW_NUMBER() OVER (PARTITION BY s.operator, s.trainno ORDER BY s.last_od DESC, s.line) = 1
),
-- One trip per train on the date it last ran (a number can carry two trip variants).
last_trip AS (
  SELECT m.operator, m.line, m.trainno, m.last_od, MIN(g.trip_id) AS trip_id
  FROM missing AS m
  JOIN gtfs AS g
    ON g.od = m.last_od AND g.operator = m.operator AND g.trainno = m.trainno AND g.line = m.line
  GROUP BY m.operator, m.line, m.trainno, m.last_od
),
calls AS (
  SELECT
    lt.operator, lt.line, lt.trainno, g.direction_id, g.quay, g.parent_station, g.stop_name, g.stop_sequence,
    TIMESTAMP(DATETIME_ADD(DATETIME(g.departure_time, 'Europe/Oslo'),
                           INTERVAL DATE_DIFF(@d, lt.last_od, DAY) DAY), 'Europe/Oslo') AS dep,
    TIMESTAMP(DATETIME_ADD(DATETIME(g.arrival_time, 'Europe/Oslo'),
                           INTERVAL DATE_DIFF(@d, lt.last_od, DAY) DAY), 'Europe/Oslo') AS arr,
    g.stop_sequence = MIN(g.stop_sequence) OVER trip AS is_first,
    g.stop_sequence = MAX(g.stop_sequence) OVER trip AS is_last
  FROM last_trip AS lt
  JOIN gtfs AS g ON g.od = lt.last_od AND g.trip_id = lt.trip_id
  WINDOW trip AS (PARTITION BY lt.operator, lt.trainno)
),
-- Trains in @d's plan that are not standing: the retimed and renumbered stand-ins. TRAINS: a
-- bus-for-train trip has no train number (`BUS-2281_...`) and must not net the train it replaces
-- out of the table -- that train is removed, and `bus_for_train` is how the digest says so.
new_calls AS (
  SELECT g.operator, g.line, g.direction_id, g.parent_station, g.departure_time
  FROM gtfs AS g
  LEFT JOIN (SELECT DISTINCT operator, trainno FROM standing) AS s
    ON s.operator = g.operator AND s.trainno = g.trainno
  WHERE g.od = @d AND g.trainno IS NOT NULL AND s.trainno IS NULL
),
stood_in AS (
  SELECT DISTINCT c.operator, c.trainno
  FROM calls AS c
  JOIN new_calls AS n
    ON n.operator = c.operator
   AND n.line = c.line AND n.direction_id = c.direction_id AND n.parent_station = c.parent_station
   AND ABS(TIMESTAMP_DIFF(n.departure_time, c.dep, MINUTE)) <= 15
  WHERE c.is_first
),
-- Lines whose ET plan today closes boarding or alighting at a mid-journey call of a TRAIN (see
-- the header). Every origin is NO_ALIGHTING and every terminus NO_BOARDING, hence the stop_order
-- bounds. Trains only: the bus-for-train journeys Vy files under a train line (`BUS-2279_...`
-- under L1) carry their own activities, and on 2026-09-23 they alone would have taken L1, R12 and
-- R13 -- 73 removed trains -- out of this table.
-- The activity test stays in QUALIFY: in WHERE it would move each journey's first and last call.
--
-- PLUS THE LINES SEEN RESTRICTED BEFORE, because today's ET can only speak for a line that runs
-- today: close F4 for the whole day and no F4 train is in ET, so the detection finds nothing and
-- every removed F4 train comes out bookable at every call -- the 2026-09-01 incident exactly.
-- ponytail: a fixed list (Vy's two lines the detection found on 2026-09-23, SJN's two on
-- 2026-10-24, KIX-43; KIX-49 would replace it with per-stop rules); a line newly selling
-- boarding-only stretches is caught only on days it runs. Persist the detected set with a
-- last-seen date before the morning digest (plan U4) ships to prd.
restricted AS (
  SELECT DISTINCT REGEXP_EXTRACT(lineRef, r'^([^:]+):') AS operator,
                  REGEXP_EXTRACT(lineRef, r'([^:]+)$') AS line
  FROM `disruptions.rail_calls_planned_today`
  -- trains: Vy's numbered DatedServiceJourneys, SJN's '<number>:<date>'
  WHERE REGEXP_CONTAINS(jkey, r'ServiceJourney:\d+[-_]') OR REGEXP_CONTAINS(jkey, r'^\d+:')
  QUALIFY stop_order > MIN(stop_order) OVER (PARTITION BY jkey)
      AND stop_order < MAX(stop_order) OVER (PARTITION BY jkey)
      AND (NOT boarding_allowed OR NOT alighting_allowed)
  UNION DISTINCT
  SELECT operator, line FROM UNNEST([
    STRUCT('VYG' AS operator, 'F4' AS line), ('VYG', 'RX11'), ('SJN', 'F6'), ('SJN', 'F7')])
),
-- One row per operator: every count below is that operator's alone.
guard AS (
  SELECT
    op AS operator,
    (SELECT COUNT(*) FROM today WHERE operator = op) AS n_today,
    (SELECT COUNT(*) FROM standing WHERE operator = op) AS n_standing,
    (SELECT COUNT(*) FROM missing WHERE operator = op) AS n_missing,
    (SELECT MAX(gtfs_version) FROM gtfs WHERE od = @d AND operator = op) AS gtfs_version,
    -- NULL, not FALSE, when the calendar has no row for @d: an unmaintained calendar fails closed.
    (SELECT LOGICAL_OR(holiday) FROM `disruptions.togvarsel_calendar` WHERE d = @d) AS holiday,
    (SELECT COUNT(DISTINCT trainno) FROM trips WHERE od = @d AND operator = op) AS n_today_trains,
    (SELECT COUNT(DISTINCT t.trainno)
     FROM trips AS t
     LEFT JOIN (SELECT DISTINCT operator, trainno FROM standing) AS s
       ON s.operator = t.operator AND s.trainno = t.trainno
     WHERE t.od = @d AND t.operator = op AND s.trainno IS NULL) AS n_new
  FROM UNNEST(['VYG', 'SJN']) AS op
)
SELECT
  CONCAT('RemovedJourney:', c.trainno, '_', FORMAT_DATE('%Y-%m-%d', @d)) AS jkey,
  CONCAT(c.operator, ':Line:', c.line) AS lineRef,
  c.quay,
  c.parent_station AS stop_place,
  c.stop_name,
  c.stop_sequence AS stop_order,
  IF(c.is_last, NULL, c.dep) AS aimed_dep,
  IF(c.is_first, NULL, c.arr) AS aimed_arr,
  CAST(NULL AS INT64) AS dep_delay_min,
  DATE(IF(c.is_last, c.arr, c.dep), 'Europe/Oslo') AS call_date,
  TRUE AS call_cancelled,
  'removed' AS call_source,
  TRUE AS boarding_allowed,
  TRUE AS alighting_allowed,
  c.trainno,
  bn.trainno IS NOT NULL AS bus_for_train,
  g.gtfs_version
FROM calls AS c
JOIN guard AS g ON g.operator = c.operator
LEFT JOIN bus_numbers AS bn ON bn.operator = c.operator AND bn.trainno = c.trainno
LEFT JOIN stood_in AS si ON si.operator = c.operator AND si.trainno = c.trainno
LEFT JOIN restricted AS r ON r.operator = c.operator AND r.line = c.line
WHERE si.trainno IS NULL
  AND r.line IS NULL
  -- CASE, not AND: BigQuery does not promise to short-circuit AND, and ERROR() must be reached
  -- only when the build is on and the partition exists.
  AND CASE
    WHEN NOT @enabled OR g.n_today = 0 THEN FALSE
    WHEN g.holiday IS NOT FALSE THEN FALSE
    WHEN g.n_new > 0.25 * g.n_today_trains THEN FALSE
    -- Vy's refusal keeps the previous table (the poller logs the ERROR). SJN's is quiet: its
    -- ~80 trains a day must not be able to take Vy's rows down with it.
    WHEN g.n_missing > 0.5 * g.n_standing AND c.operator = 'VYG' THEN ERROR(FORMAT(
      'rail_calls_removed_today refused: %d of %d standing trains missing from the %t plan',
      g.n_missing, g.n_standing, @d))
    WHEN g.n_missing > 0.5 * g.n_standing THEN FALSE
    ELSE TRUE
  END;
