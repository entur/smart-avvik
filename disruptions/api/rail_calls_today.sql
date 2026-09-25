-- Refresh of disruptions.rail_calls_today: the most up-to-date ET snapshot for every Vy/NSB
-- journey from TODAY's ET partition. Same shape/logic as create_rail_calls_new.sql (last
-- snapshot per journey -> most up-to-date; Quay -> parent StopPlace via the National Stop
-- Register) — only the window differs (today here, vs 30 days in create_rail_calls_new.sql /
-- 2 years in create_rail_calls.sql).
--
-- CREATE OR REPLACE, not append: appending can't deliver "most up-to-date" for a journey already
-- in the table (a jkey dedup keeps the stale snapshot; no dedup duplicates it). Rebuilding
-- today's slice each run guarantees the freshest snapshot per journey AND drops anything not from
-- today. It costs the same scan as a merge would — both read only today's ET partition — so
-- there's no incremental-cost reason to do anything fancier. Run it as often as you want fresh
-- actuals.
--
-- The CURRENT_DATE() _PARTITIONTIME prune keeps this a tiny scan; no EXECUTE IMMEDIATE needed,
-- since CURRENT_DATE() in the partition filter prunes (same convention as create_rail_calls(_new).sql).
-- "Today" is by ET ingestion partition, so it captures journeys whose latest snapshot landed
-- today. The day is only as complete as ET has ingested so far (in-progress windows give partial
-- actuals); re-run as ET fills in.
--
-- ONE LIVE READER since 2026-09-22: `main.refresh_rail_actuals` loads it into memory after every
-- rebuild for the /varsel «I dag» strip (`journey_status`), which is what brings a recorded arrival
-- there from the planned table's 60 min to this table's 15. It tolerates the 15 min, so the cost
-- argument in `main._RAIL_CALLS_TTL`'s comment still holds. It grades a trip by the DEPARTURE delay
-- at the traveller's alighting call: an arrival delay was measured (2026-09-22) at +58 % on this
-- scan (4.2 GB at 20:50, from reading `aimedArrivalTime` and `actualArrivalTime`), which is why it
-- is not here. A call with no aimed departure (a terminus) is not in this table either; the strip
-- reads those off the planned table instead.

CREATE OR REPLACE TABLE `disruptions.rail_calls_today` AS
WITH last_snap AS (
  SELECT
    REGEXP_REPLACE(datedVehicleJourneyRef, r'^[A-Za-z]+:', '') AS jkey,
    lineRef, cancellation AS journey_cancelled, recordedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  -- `OR _PARTITIONTIME IS NULL` reads the streaming buffer too (rows land there for a few minutes
  -- before BigQuery assigns their partition); ~23 MB, and it is where the freshest calls are.
  WHERE (DATE(_PARTITIONTIME) = CURRENT_DATE() OR _PARTITIONTIME IS NULL) AND lineRef LIKE 'VYG:%'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY datedVehicleJourneyRef ORDER BY recordedAtTime DESC) = 1
),
calls AS (
  SELECT
    s.jkey, s.lineRef,
    rc.stopPointRef AS quay,
    rc.order AS stop_order,
    SAFE.TIMESTAMP(rc.aimedDepartureTime) AS aimed_dep,
    DATE(SAFE.TIMESTAMP(rc.aimedDepartureTime)) AS call_date,
    (rc.cancellation OR s.journey_cancelled) AS call_cancelled,
    TIMESTAMP_DIFF(SAFE.TIMESTAMP(rc.actualDepartureTime),
                   SAFE.TIMESTAMP(rc.aimedDepartureTime), MINUTE) AS dep_delay_min
  FROM last_snap AS s, UNNEST(s.recordedCalls) AS rc
  WHERE SAFE.TIMESTAMP(rc.aimedDepartureTime) IS NOT NULL
    AND (rc.cancellation OR s.journey_cancelled OR rc.actualDepartureTime IS NOT NULL)
)
SELECT
  c.jkey, c.lineRef, c.quay,
  q.stopPlaceRef AS stop_place,
  q.name         AS stop_name,
  c.stop_order, c.aimed_dep, c.call_date, c.call_cancelled, c.dep_delay_min
FROM calls AS c
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
  ON c.quay = q.id;
