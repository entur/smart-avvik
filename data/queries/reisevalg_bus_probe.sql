-- Bus-probe ground truth for the reisevalg-varsel feasibility backtest (plan
-- docs/plans/2026-07-11-001-..., U2 deliverable; extracted from the 2026-07-11 U2
-- analysis). Entur's own RUT buses as probe vehicles on the E18 corridor: realized
-- run-time inflation between anchor stops is a direct travel-time congestion signal.
-- Three tables in dependency order:
--
--   1) disruptions.reisevalg_bus_probe_calls -- anchor-stop calls for the corridor
--      lines RUT:Line:250 (IKEA Slependen -> Vika atrium), RUT:Line:160 (Sandvika
--      bussterminal -> Vika atrium) and RUT:Line:130, from
--      ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded
--      (the all-operator per-call history table -- daily batch, training-only).
--   2) disruptions.reisevalg_bus_runs -- one row per (date, journey): west-anchor
--      departure to east-anchor arrival, realized vs aimed run time, inflation.
--   3) disruptions.reisevalg_bus_mornings -- one row per weekday morning: p50/p90/max
--      inflation over the rush window. reisevalg_backtest.sql LEFT JOINs this table
--      as its bus-probe ground-truth channel.
--
-- MONTHLY CHUNKING REQUIRED for (1): last_recorded's clustering does NOT reduce
-- billed bytes for the dataSource='RUT' filter -- every chunk bills the full
-- partition-pruned slice, ~1.58 GB per month, so the 2024-06..2026-05 window is
-- ~24 chunks = ~38 GB = ~$0.24 total. Run the first month as the CREATE OR REPLACE
-- below, then advance the operatingDate parameters month by month (editing them by
-- hand or via a shell loop) with `bq query --append_table` / INSERT INTO
-- `disruptions.reisevalg_bus_probe_calls` SELECT ... for the remaining months.
-- Do NOT widen the BETWEEN to the full window in one statement: same total bytes,
-- but a failed job forfeits the whole scan instead of one month.
--
-- COST: ~$0.24 for the full 24-month extraction; (2) and (3) read only the tiny
-- materialized tables (~free).

-- ---------------------------------------------------------------------------
-- (1) anchor-stop probe calls -- ONE MONTH PER RUN (see header)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE `disruptions.reisevalg_bus_probe_calls` AS
SELECT operatingDate, lineRef, serviceJourneyId, stopPointName, sequenceNr,
       aimedArrivalTime, arrivalTime, aimedDepartureTime, departureTime, estimated
FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded`
WHERE operatingDate BETWEEN '2024-06-01' AND '2024-06-30'  -- advance month by month to 2026-05-31
  AND dataSource = 'RUT'
  AND lineRef IN ('RUT:Line:250', 'RUT:Line:160', 'RUT:Line:130')
  AND stopPointName IN ('IKEA Slependen', 'Sandvika bussterminal',
                        'Vika atrium', 'Nationaltheatret');

-- ---------------------------------------------------------------------------
-- (2) per-journey corridor runs (west anchor -> east anchor)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE `disruptions.reisevalg_bus_runs` AS
WITH anchors AS (
  SELECT operatingDate, lineRef, serviceJourneyId, stopPointName, sequenceNr,
         aimedDepartureTime, departureTime, aimedArrivalTime, arrivalTime, estimated
  FROM `disruptions.reisevalg_bus_probe_calls`
),
west AS (
  SELECT * FROM anchors
  WHERE stopPointName IN ('IKEA Slependen','Sandvika bussterminal')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY operatingDate, serviceJourneyId ORDER BY sequenceNr) = 1
),
east AS (
  SELECT * FROM anchors
  WHERE stopPointName IN ('Vika atrium','Nationaltheatret')
  QUALIFY ROW_NUMBER() OVER (PARTITION BY operatingDate, serviceJourneyId
                             ORDER BY IF(stopPointName='Vika atrium',0,1), sequenceNr) = 1
)
SELECT
  w.operatingDate AS date_oslo,
  w.lineRef,
  w.serviceJourneyId,
  w.stopPointName AS west_stop, e.stopPointName AS east_stop,
  DATETIME(w.aimedDepartureTime, 'Europe/Oslo') AS aimed_dep_west_oslo,
  TIMESTAMP_DIFF(e.arrivalTime, w.departureTime, SECOND) / 60.0 AS run_min,
  TIMESTAMP_DIFF(e.aimedArrivalTime, w.aimedDepartureTime, SECOND) / 60.0 AS aimed_run_min,
  (TIMESTAMP_DIFF(e.arrivalTime, w.departureTime, SECOND)
   - TIMESTAMP_DIFF(e.aimedArrivalTime, w.aimedDepartureTime, SECOND)) / 60.0 AS inflation_min,
  w.estimated AS west_estimated, e.estimated AS east_estimated
FROM west w
JOIN east e
  ON e.operatingDate = w.operatingDate AND e.serviceJourneyId = w.serviceJourneyId
 AND e.sequenceNr > w.sequenceNr
WHERE w.departureTime IS NOT NULL AND e.arrivalTime IS NOT NULL
  AND w.aimedDepartureTime IS NOT NULL AND e.aimedArrivalTime IS NOT NULL;

-- ---------------------------------------------------------------------------
-- (3) per-morning inflation summary (the backtest's ground-truth channel)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE TABLE `disruptions.reisevalg_bus_mornings` AS
SELECT
  date_oslo,
  COUNT(*) AS n_runs,
  ROUND(APPROX_QUANTILES(inflation_min, 100)[OFFSET(50)], 2) AS p50_infl,
  ROUND(APPROX_QUANTILES(inflation_min, 100)[OFFSET(90)], 2) AS p90_infl,
  ROUND(MAX(inflation_min), 1) AS max_infl
FROM `disruptions.reisevalg_bus_runs`
WHERE MOD(EXTRACT(DAYOFWEEK FROM date_oslo)+5,7) < 5
  AND TIME(aimed_dep_west_oslo) BETWEEN '06:00:00' AND '09:30:00'
  AND inflation_min BETWEEN -30 AND 120
GROUP BY date_oslo
HAVING n_runs >= 10;
