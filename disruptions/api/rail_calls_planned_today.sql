-- PLANNED-timetable call source for live per-call serving: every UPCOMING call of every Vy/NSB
-- journey currently in the ET feed, with its scheduled (aimed) departure/arrival, stop and order.
--
-- This is the forward-looking sibling of rail_calls_today.sql (disruptions/api/). That table is
-- for realized actuals (recordedCalls only, departure-only) so it cannot answer "will this train
-- -- which has NOT run yet -- be cancelled / late?". Here we take ALL calls of each journey's last
-- snapshot, from recordedCalls (past stops) AND estimatedCalls (the upcoming stops, which
-- create_rail_calls.sql skips unless cancelled), keeping the AIMED times. The future stops come
-- from estimatedCalls; that's the planned timetable a freshly-reported situation needs.
--
-- DROP-IN: exposes the columns predict_calls.sql reads from rail_calls (jkey, lineRef, stop_place,
-- stop_name, stop_order, aimed_dep, aimed_arr) -- for live serving, point predict_calls' call
-- source at this table instead of rail_calls. call_cancelled here is what ET ALREADY declares
-- (not the model's prediction); call_source distinguishes recorded vs estimated stops.
--
-- WHETHER A PASSENGER MAY USE A CALL is a property of the call, not of the stop, and it is the one
-- thing a stop list cannot say (2026-09-01). SIRI's two boarding activities carry it, and Vy uses
-- them to close the local stretch of a long-distance run: the 16:23 Oslo S - Bergen calls Sandvika,
-- Asker, Drammen and Hokksund with arrivalBoardingActivity = NO_ALIGHTING -- board for Bergen, but
-- nobody gets off before Hønefoss. Without these two columns the matcher happily offered that train
-- to an Oslo - Drammen registration: one cancellation reached five registered journeys and eight
-- devices, none of whom could have been on that train. Measured on VYG the same day: 56 mid-journey NO_ALIGHTING calls on F4
-- (24 of its 28 journeys) and 417 mid-journey NO_BOARDING calls across F4, R31 and RE30.
--
-- The reading is fail-open and the rule lives HERE, in one place per source table, so nothing
-- downstream has to remember the vocabulary: only an explicit prohibition prohibits. Most operators
-- leave both fields NULL on most calls (RUT, KOL, TRO, SKY, OST: the large majority), and a NULL
-- means "not stated", never "not allowed" -- reading it as a prohibition would silently stop
-- alerting for whole operators. PASS_THRU is a call the vehicle does not serve at all, so it bars
-- both sides. An origin has no arrival and a terminal no departure, and the feed marks them
-- NO_ALIGHTING / NO_BOARDING accordingly; that costs nothing, since the matcher already requires an
-- aimed time on the side it is using.
--
-- HORIZON: ET only carries journeys it currently knows (active + near-future within the operating
-- day), so the forward horizon is bounded by how far ahead ET emits -- not a full multi-day plan.
-- A NeTEx planned-timetable feed would extend it; ET is the pragmatic source already wired here.
-- CURRENT_DATE() partition prune keeps it a tiny scan; re-run as often as you want fresh.
--
-- WHY ET AND NOT GTFS AS THE JOURNEY SOURCE: the GTFS timetable
-- (ent-data-sharing-ext-prd.timetable_gtfs.gtfs_last_recorded_ent_v1) is a richer schedule, but
-- its trip_id is a NeTEx ServiceJourney ref -- it does NOT match SX affected_journeys, which are
-- DatedServiceJourney refs (0 native matches). A parsed train-number+date bridge finds only ~57%
-- of SX-named trains, because GTFS is the BASE schedule and SX skews toward the replacement /
-- renumbered / extra runs that only ET carries. ET's datedVehicleJourneyRef shares SX's namespace,
-- so it is the correct journey source. GTFS is used ONLY to resolve stop_name below (by StopPlace,
-- not by journey), since the NSR quays join returns the ref but a NULL name.
CREATE OR REPLACE TABLE `disruptions.rail_calls_planned_today` AS
WITH last_snap AS (
  SELECT
    REGEXP_REPLACE(datedVehicleJourneyRef, r'^[A-Za-z]+:', '') AS jkey,
    lineRef,
    cancellation AS journey_cancelled,
    recordedCalls,
    estimatedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  -- YESTERDAY'S partition too (2026-09-21). ET publishes a journey ~22 h ahead, and Vy cancels
  -- some of them the day before and never sends them again: on 2026-09-21 nine L1 morning trains
  -- were stop-cancelled at 07:00-10:00 UTC the day before, had no row in today's partition, and
  -- so were absent from this table while their healthy neighbours were in it -- /impact told a
  -- Lillestrøm-Asker 07:10 registration "ingen aktive avvik". The partition is ingestion time,
  -- so "today's journeys" is a property of the CALLS (today_journeys below), not of the partition.
  -- The same rebuild at 16:00 also gained 264 journeys due LATER TODAY that had no snapshot
  -- since their publication yesterday -- a quarter of the table -- so an evening registration
  -- was scored against no trains at all until ET touched them again.
  -- Cost: about one more day's rail partition per rebuild (measured 14.1 -> 22.2 GB at 16:00).
  -- togvarsel_features_today.sql has read both partitions since it existed, for the same reason.
  -- `OR _PARTITIONTIME IS NULL` reads the streaming buffer too (see last_ingestion_time.sql).
  WHERE (DATE(_PARTITIONTIME) BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY) AND CURRENT_DATE()
         OR _PARTITIONTIME IS NULL)
    AND lineRef LIKE 'VYG:%'
    AND datedVehicleJourneyRef IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY datedVehicleJourneyRef ORDER BY SAFE.TIMESTAMP(recordedAtTime) DESC) = 1
),
calls AS (
  -- recorded (operated / past) stops -- planned times + the REALIZED departure delay, which the
  -- 2026-07-02 propagation features (line_pre_*/net_pre_*/line_now_*/own_prev_delay in
  -- predict_calls*.sql) read as the live-observable state. Same delay definition as
  -- create_rail_calls.sql: actual vs aimed departure, minutes.
  SELECT
    s.jkey, s.lineRef, rc.stopPointRef AS quay, rc.order AS stop_order, 'recorded' AS call_source,
    (rc.cancellation OR s.journey_cancelled) AS call_cancelled,
    (IFNULL(rc.departureBoardingActivity, 'BOARDING')  NOT IN ('NO_BOARDING',  'PASS_THRU')) AS boarding_allowed,
    (IFNULL(rc.arrivalBoardingActivity,   'ALIGHTING') NOT IN ('NO_ALIGHTING', 'PASS_THRU')) AS alighting_allowed,
    SAFE.TIMESTAMP(rc.aimedDepartureTime) AS aimed_dep,
    SAFE.TIMESTAMP(rc.aimedArrivalTime)   AS aimed_arr,
    TIMESTAMP_DIFF(SAFE.TIMESTAMP(rc.actualDepartureTime),
                   SAFE.TIMESTAMP(rc.aimedDepartureTime), MINUTE) AS dep_delay_min
  FROM last_snap AS s, UNNEST(s.recordedCalls) AS rc
  UNION ALL
  -- estimated (upcoming / future) stops -- ALL of them (the forward schedule), planned times
  SELECT
    s.jkey, s.lineRef, ec.stopPointRef, ec.order, 'estimated',
    (ec.cancellation OR s.journey_cancelled),
    (IFNULL(ec.departureBoardingActivity, 'BOARDING')  NOT IN ('NO_BOARDING',  'PASS_THRU')),
    (IFNULL(ec.arrivalBoardingActivity,   'ALIGHTING') NOT IN ('NO_ALIGHTING', 'PASS_THRU')),
    SAFE.TIMESTAMP(ec.aimedDepartureTime),
    SAFE.TIMESTAMP(ec.aimedArrivalTime),
    CAST(NULL AS INT64)
  FROM last_snap AS s, UNNEST(s.estimatedCalls) AS ec
),
-- Yesterday's partition also holds yesterday's finished journeys. Keep a journey only if one of
-- its calls is aimed today or later (Oslo) -- which is what the today-only partition delivered in
-- practice, since a snapshot ingested after 00:00 UTC is a call at or after 02:00 Oslo.
today_journeys AS (
  SELECT DISTINCT jkey
  FROM calls
  WHERE DATE(COALESCE(aimed_dep, aimed_arr), 'Europe/Oslo') >= CURRENT_DATE('Europe/Oslo')
),
dedup AS (
  -- a stop can appear in both lists across the journey's life; keep one (prefer recorded)
  SELECT c.*
  FROM calls AS c
  JOIN today_journeys USING (jkey)
  WHERE COALESCE(aimed_dep, aimed_arr) IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY jkey, stop_order ORDER BY (call_source = 'recorded') DESC) = 1
),
-- StopPlace -> human name from GTFS (the NSR quays join yields stopPlaceRef but a NULL name).
-- A small recent window of GTFS partitions covers every currently-served stop; names are stable.
gtfs_names AS (
  SELECT parent_station AS stop_place, ANY_VALUE(stop_name) AS stop_name
  FROM `ent-data-sharing-ext-prd.timetable_gtfs.gtfs_last_recorded_ent_v1`
  WHERE agency_id LIKE 'VYG:%'
    AND operating_date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
                           AND DATE_ADD(CURRENT_DATE(), INTERVAL 2 DAY)
    AND parent_station IS NOT NULL
  GROUP BY parent_station
)
SELECT
  d.jkey, d.lineRef, d.quay,
  q.stopPlaceRef            AS stop_place,
  COALESCE(n.stop_name, q.name) AS stop_name,
  d.stop_order, d.aimed_dep, d.aimed_arr, d.dep_delay_min,
  DATE(COALESCE(d.aimed_dep, d.aimed_arr), 'Europe/Oslo') AS call_date,
  d.call_cancelled, d.call_source, d.boarding_allowed, d.alighting_allowed
FROM dedup AS d
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
  ON d.quay = q.id
LEFT JOIN gtfs_names AS n
  ON q.stopPlaceRef = n.stop_place;
