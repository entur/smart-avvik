-- Trains a registered journey should have been matched against on day d but never was, and why:
--   plan_cut   in the standing timetable (>= 3 of the 4 previous same weekdays), absent from d's GTFS,
--              and no train NEW in d's plan serves the same boarding -> alighting within 15 min
--              (a retimed or renumbered train is not a removal)
--   no_et      in d's GTFS, never published to ET for d
--   et_cancel  in ET for d, cancelled at the boarding stop, yet no decision row (our side)
--   et_running in ET and not cancelled there, yet no decision row (matcher side; before the
--              two-partition fix reached prd on 2026-09-22 09:43, mostly trains ET only had from
--              the day before)
-- bus_cover: a bus-for-train trip in the day's plan serves the same stops within 30 min.
-- Window proxy: the registration's own first..last matched departure that day. A floor: a removed
-- train at the window's edge is not counted.
WITH days AS (SELECT d FROM UNNEST(GENERATE_DATE_ARRAY('2026-09-11', '2026-09-23')) d),
regs AS (
  SELECT call_date d, journey_digest, ANY_VALUE(from_stop) fs, ANY_VALUE(to_stop) ts,
         MIN(departure_time) w0, MAX(departure_time) w1, MAX(n_devices) dev,
         ARRAY_AGG(DISTINCT REGEXP_EXTRACT(jkey, r'ServiceJourney:(\d+)[-_]') IGNORE NULLS) seen
  FROM `ent-kixavvik-prd.disruptions.varsel_decisions`
  WHERE call_date BETWEEN '2026-09-11' AND '2026-09-23' AND DATE(decided_at) BETWEEN '2026-09-10' AND '2026-09-24'
    AND operator = 'VYG'
  GROUP BY 1,2),
g AS (
  SELECT operating_date od, route_short_name line,
         COALESCE(REGEXP_EXTRACT(trip_id, r'ServiceJourney:(\d+)[-_]'), REGEXP_EXTRACT(trip_id, r'ServiceJourney:(BUS-[^_]+_\d+)')) trainno,
         parent_station ps, stop_id quay, stop_sequence seq, departure_time dep
  FROM `ent-data-sharing-ext-prd.timetable_gtfs.gtfs_last_recorded_ent_v1`
  WHERE agency_id LIKE 'VYG%' AND route_type = 'Train' AND operating_date BETWEEN '2026-08-14' AND '2026-09-23'),
trips AS (SELECT DISTINCT od, line, trainno FROM g WHERE trainno IS NOT NULL),
standing AS (
  SELECT x.d, t.line, t.trainno, MAX(t.od) last_od FROM days x
  JOIN trips t ON t.od IN (DATE_SUB(x.d, INTERVAL 7 DAY), DATE_SUB(x.d, INTERVAL 14 DAY), DATE_SUB(x.d, INTERVAL 21 DAY), DATE_SUB(x.d, INTERVAL 28 DAY))
  GROUP BY 1,2,3 HAVING COUNT(*) >= 3),
cand AS (
  SELECT t.od d, t.line, t.trainno, IF(s.trainno IS NULL, 'added', 'planned') src, t.od src_od
  FROM trips t JOIN days ON days.d = t.od LEFT JOIN standing s ON s.d = t.od AND s.trainno = t.trainno
  UNION ALL
  SELECT s.d, s.line, s.trainno, 'plan_cut', s.last_od FROM standing s
  LEFT JOIN trips t ON t.od = s.d AND t.trainno = s.trainno WHERE t.trainno IS NULL),
cand_stops AS (
  SELECT c.d, c.line, c.trainno, c.src, g.ps, g.seq, TIMESTAMP_ADD(g.dep, INTERVAL DATE_DIFF(c.d, g.od, DAY) DAY) dep
  FROM cand c JOIN g ON g.trainno = c.trainno AND g.od = c.src_od),
serves AS (  -- every candidate train that boards at the registration's stop and later alights at its stop
  SELECT r.d, r.journey_digest, r.dev, r.fs, r.ts, r.w0, r.w1, r.seen, a.line, a.trainno, a.src, a.dep
  FROM regs r
  JOIN cand_stops a ON a.d = r.d AND a.ps = r.fs
  JOIN cand_stops b ON b.d = a.d AND b.trainno = a.trainno AND b.src = a.src AND b.ps = r.ts AND b.seq > a.seq),
replaced AS (  -- a cut train that a train new in the day's plan stands in for, within 15 min
  SELECT DISTINCT c.d, c.journey_digest, c.trainno FROM serves c JOIN serves n
    ON n.d = c.d AND n.journey_digest = c.journey_digest AND n.src = 'added'
   AND ABS(TIMESTAMP_DIFF(n.dep, c.dep, MINUTE)) <= 15 AND NOT STARTS_WITH(n.trainno, 'BUS')
  WHERE c.src = 'plan_cut'),
bus AS (  -- a cut train a bus-for-train trip in the day's plan covers, same stops, within 30 min
  SELECT DISTINCT c.d, c.journey_digest, c.trainno FROM serves c JOIN serves n
    ON n.d = c.d AND n.journey_digest = c.journey_digest AND n.src = 'added'
   AND ABS(TIMESTAMP_DIFF(n.dep, c.dep, MINUTE)) <= 30 AND STARTS_WITH(n.trainno, 'BUS')
  WHERE c.src = 'plan_cut'),
should AS (
  SELECT s.* FROM serves s
  LEFT JOIN replaced r ON r.d = s.d AND r.journey_digest = s.journey_digest AND r.trainno = s.trainno
  LEFT JOIN bus b ON b.d = s.d AND b.journey_digest = s.journey_digest AND b.trainno = s.trainno
  WHERE s.dep BETWEEN s.w0 AND s.w1 AND s.src != 'added' AND s.trainno NOT IN UNNEST(s.seen)
    AND r.trainno IS NULL),
quays AS (SELECT DISTINCT quay, ps FROM g WHERE od >= '2026-09-11'),
et AS (
  SELECT operatingDate d, REGEXP_EXTRACT(datedServiceJourneyId, r'ServiceJourney:(\d+)[-_]') trainno,
         q.ps, LOGICAL_OR(IFNULL(journeyCancellation, FALSE) OR IFNULL(stopCancellation, FALSE)) canc
  FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded` e
  LEFT JOIN quays q ON q.quay = e.stopPointRef
  WHERE operatingDate BETWEEN '2026-09-11' AND '2026-09-23' AND dataSource = 'VYG'
  GROUP BY 1,2,3),
et_j AS (SELECT DISTINCT d, trainno FROM et),
nsr AS (SELECT id, ANY_VALUE(name) name FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version` GROUP BY id)
SELECT (b.trainno IS NOT NULL) bus_cover, s.d, s.journey_digest, s.dev, s.fs, s.ts,
  COALESCE(na.name, s.fs) fra, COALESCE(nb.name, s.ts) til, s.line, s.trainno,
  FORMAT_TIMESTAMP('%H:%M', s.dep, 'Europe/Oslo') avgang,
  CASE WHEN s.src = 'plan_cut' THEN 'plan_cut'
       WHEN j.trainno IS NULL THEN 'no_et'
       WHEN e.canc THEN 'et_cancel'
       ELSE 'et_running' END why
FROM should s
LEFT JOIN bus b ON b.d = s.d AND b.journey_digest = s.journey_digest AND b.trainno = s.trainno
LEFT JOIN et_j j ON j.d = s.d AND j.trainno = s.trainno
LEFT JOIN et e ON e.d = s.d AND e.trainno = s.trainno AND e.ps = s.fs
LEFT JOIN nsr na ON na.id = s.fs LEFT JOIN nsr nb ON nb.id = s.ts
ORDER BY s.d, s.dep, s.journey_digest
