-- disruptions.reisevalg_backtest -- the joint per-morning road x rail grid the
-- reisevalg-varsel feasibility verdict rests on (plan docs/plans/2026-07-11-001-...,
-- U2 deliverable; R3). One row per weekday morning 2024-06..2026-05, joining:
--   * rail state from disruptions.reisevalg_rail_mornings (arrival-side predicate,
--     in_rail_coverage marks the usable slice >= 2024-12-15 -- see that file);
--   * road anomaly flags from disruptions.reisevalg_road_mornings (live-analog
--     volume-deficit rule at several thresholds + hindsight volume-ratio flags,
--     plus detection timing under the measured ~150 min publication lag);
--   * ground truth: bus-probe run-time inflation (disruptions.reisevalg_bus_mornings,
--     built by reisevalg_bus_probe.sql from RUT:Line:160 Sandvika->Vika atrium and
--     RUT:Line:250 IKEA Slependen->Vika atrium realized run times in
--     ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded)
--     and corridor road-incident SX counts (disruptions.disruptions text filter).
--
-- GROUND TRUTH definition (gt_congested): bus p50 inflation >= 5 min OR bus p90
-- inflation >= 10 min OR >= 2 corridor traffic-incident SX situations created
-- 05:00-09:59 Oslo. The channels are weak individually (SX yield is tiny; buses
-- ride bus lanes on parts of the stretch, damping their exposure to car queues) --
-- precision numbers derived from this must be read as approximate.
--
-- The summary SELECT at the end emits the headline rates; break-even constants
-- (headway, rail run time) come from the rail_calls scan in the same statement.
--
-- COST: materialized U2 tables (tiny) + one 0.25 GB disruptions.disruptions scan
-- + one ~0.5 GB rail_calls scan for headway/run-time -> ~cents.

CREATE OR REPLACE TABLE `disruptions.reisevalg_backtest` AS
WITH sx AS (  -- corridor road-incident SX mornings (weak channel; see header)
  SELECT DATE(ts_oslo) AS date_oslo, COUNT(*) AS n_sx
  FROM (
    SELECT DATETIME(SAFE.TIMESTAMP(creationTime), 'Europe/Oslo') AS ts_oslo,
           IFNULL(summary,'') || ' ' || IFNULL(description,'') AS text
    FROM `disruptions.disruptions`
    WHERE participantRef IN ('RUT','BRA')
      AND SAFE.TIMESTAMP(creationTime) BETWEEN '2024-06-01' AND '2026-06-01'
  )
  WHERE EXTRACT(HOUR FROM ts_oslo) BETWEEN 5 AND 9
    AND REGEXP_CONTAINS(text, r'(?i)(\bkø\b|køer|kødannelse|trafikkulykke|ulykke|påkjørsel|kollisjon|glatt|trafikale forhold|stor trafikk)')
    AND REGEXP_CONTAINS(text, r'(?i)(Lier\b|Asker|Sandvika|Lysaker|Høvik|Billingstad|Slependen|Blommenholm|Gyssestad)')
  GROUP BY date_oslo
)
SELECT
  r.date_oslo,
  -- rail side
  r.in_rail_coverage, r.is_holiday, r.holiday_kind, r.is_closure,
  r.n_trains, r.n_cancelled, r.n_late_arr, r.p90_arr_delay,
  r.degraded_strict, r.degraded_loose,
  -- road side (volume anomaly)
  v.n_points, v.rush_vol_ratio,
  v.anom_d15, v.anom_d20, v.anom_d25, v.anom_d30, v.anom_p10,
  v.anom_vol90, v.anom_vol85, v.anom_vol75,
  v.detect_ts_d25, v.avail_ts_d25,
  v.avail_by_0700_d25, v.avail_by_0800_d25, v.avail_by_0900_d25,
  -- ground truth
  b.n_runs AS bus_runs, b.p50_infl AS bus_p50_infl, b.p90_infl AS bus_p90_infl,
  IFNULL(s.n_sx, 0) AS n_sx,
  (IFNULL(b.p50_infl, 0) >= 5 OR IFNULL(b.p90_infl, 0) >= 10 OR IFNULL(s.n_sx, 0) >= 2)
    AS gt_congested
FROM `disruptions.reisevalg_rail_mornings` r
LEFT JOIN `disruptions.reisevalg_road_mornings` v USING (date_oslo)
LEFT JOIN `disruptions.reisevalg_bus_mornings` b USING (date_oslo)
LEFT JOIN sx s USING (date_oslo);

-- Headway + run-time constants for the break-even computation (summary output;
-- not part of the per-morning table). Corridor trains as in
-- reisevalg_rail_mornings.sql; usable rail window only.
WITH corridor AS (
  SELECT jkey, stop_place, stop_order, aimed_dep, actual_dep, actual_arr, call_date
  FROM `disruptions.rail_calls`
  WHERE call_date BETWEEN '2024-12-15' AND '2026-05-31'
    AND lineRef IN ('VYG:Line:L1', 'VYG:Line:R12', 'VYG:Line:R13', 'VYG:Line:RE11')
    AND stop_place IN ('NSR:StopPlace:426', 'NSR:StopPlace:337')
),
lier AS (
  SELECT * FROM corridor WHERE stop_place = 'NSR:StopPlace:426'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY jkey ORDER BY stop_order) = 1
),
runs AS (
  SELECT l.call_date,
         DATETIME(l.aimed_dep, 'Europe/Oslo') AS dep_oslo,
         TIMESTAMP_DIFF(o.actual_arr, l.actual_dep, SECOND) / 60.0 AS run_min,
         LAG(l.aimed_dep) OVER (PARTITION BY l.call_date ORDER BY l.aimed_dep) AS prev_dep
  FROM lier l
  JOIN corridor o ON o.jkey = l.jkey AND o.stop_place = 'NSR:StopPlace:337'
                 AND o.stop_order > l.stop_order
  WHERE MOD(EXTRACT(DAYOFWEEK FROM l.call_date) + 5, 7) < 5
)
SELECT
  EXTRACT(HOUR FROM dep_oslo) AS dep_hour,
  COUNT(*) AS departures,
  ROUND(APPROX_QUANTILES(TIMESTAMP_DIFF(TIMESTAMP(dep_oslo), TIMESTAMP(DATETIME(prev_dep, 'Europe/Oslo')), SECOND) / 60.0, 100)[OFFSET(50)], 1) AS median_headway_min,
  ROUND(APPROX_QUANTILES(run_min, 100)[OFFSET(50)], 1) AS median_run_min,
  -- break-even queue delay: queue_delay > wait + rail_time - car_freeflow(36 min)
  ROUND(APPROX_QUANTILES(TIMESTAMP_DIFF(TIMESTAMP(dep_oslo), TIMESTAMP(DATETIME(prev_dep, 'Europe/Oslo')), SECOND) / 60.0, 100)[OFFSET(50)] / 2
        + APPROX_QUANTILES(run_min, 100)[OFFSET(50)] - 36, 1) AS breakeven_expected_wait,
  ROUND(APPROX_QUANTILES(TIMESTAMP_DIFF(TIMESTAMP(dep_oslo), TIMESTAMP(DATETIME(prev_dep, 'Europe/Oslo')), SECOND) / 60.0, 100)[OFFSET(50)]
        + APPROX_QUANTILES(run_min, 100)[OFFSET(50)] - 36, 1) AS breakeven_full_wait
FROM runs
WHERE TIME(dep_oslo) BETWEEN '06:00:00' AND '09:30:00'
GROUP BY dep_hour ORDER BY dep_hour;
