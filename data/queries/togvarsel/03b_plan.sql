-- disruptions.togvarsel_plan_cut: one row per day with the PLANNED trains that never ran, in
-- Oslo-area calls per label window (docs/rushvarsel/02-removed-journeys, 04-cuts). Vy sometimes
-- takes a train out instead of cancelling it -- pulled from ET mid-day with no Cancellation, or
-- dropped from the day's plan the evening before -- and to a passenger that is a cancellation.
-- The ET history cannot see either, so the label chain reads the GTFS plan beside it.
--
--   *_miss     planned Oslo calls of GTFS trains absent from ET that day (line did run) -- the
--              intraday pull, and anything the plan had that ET never carried
--   *_cut      planned Oslo calls of STANDING trains absent from the day's plan, netted against
--              trains new that day (a retimed evening or a December change reads as zero)
--   *_cut_new  the part of *_cut not already in force on the previous weekday: the day-before cut
--              the product owner ruled a bad rush on 2026-09-21 (a persisting cut is planned work)
--
-- STANDING = the (line, train number) was in the plan on >= 3 of the 4 previous same-weekday
-- dates. Train numbers join GTFS trip_ids (ServiceJourney:2240_...) to ET journeys
-- (DatedServiceJourney:2240_...) at >= 99 % on every Vy Oslo line except RE20, which is excluded.
-- 04_days.sql adds *_miss + *_cut_new to both the numerator and the denominator of am_t10 /
-- pm_t10; 08_features_cat.sql does the same for the 07:00 label; 07_features.sql carries *_cut
-- and *_cut_new as features (known at 02:06, when the day's GTFS partition is written).
-- Cost: ~530 partitions x ~0.1 GB of VYG GTFS, about $0.5 per rebuild.
CREATE OR REPLACE TABLE `disruptions.togvarsel_plan_cut` AS
WITH gc AS (
  SELECT g.operating_date d, g.route_short_name line, REGEXP_EXTRACT(g.trip_id, r'ServiceJourney:(\d+)[-_]') trainno, g.trip_id,
    EXTRACT(HOUR FROM DATETIME(COALESCE(g.arrival_time, g.departure_time), 'Europe/Oslo'))
      + EXTRACT(MINUTE FROM DATETIME(COALESCE(g.arrival_time, g.departure_time), 'Europe/Oslo'))/60.0 h
  FROM `ent-data-sharing-ext-prd.timetable_gtfs.gtfs_last_recorded_ent_v1` g
  JOIN `disruptions.togvarsel_oslo_stations` s ON s.stop_place = g.parent_station
  -- four reference weeks before last_recorded's first VYG day (2025-04-10), up to yesterday
  WHERE g.operating_date BETWEEN '2025-03-13' AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    AND g.agency_id LIKE 'VYG%' AND g.route_type = 'Train' AND g.route_short_name != 'RE20'
),
trips AS (SELECT DISTINCT d, line, trainno FROM gc WHERE trainno IS NOT NULL),
days AS (SELECT DISTINCT d FROM trips),
standing AS (
  SELECT x.d, p.line, p.trainno, MAX(p.d) last_seen
  FROM days x JOIN trips p
    ON p.d IN (DATE_SUB(x.d, INTERVAL 7 DAY), DATE_SUB(x.d, INTERVAL 14 DAY), DATE_SUB(x.d, INTERVAL 21 DAY), DATE_SUB(x.d, INTERVAL 28 DAY))
  GROUP BY 1,2,3 HAVING COUNT(*) >= 3
),
-- a standing train absent from the day's plan, with its calls taken from its last appearance
missing AS (
  SELECT s.d, s.line, s.trainno, s.last_seen
  FROM standing s LEFT JOIN trips t USING (d, line, trainno)
  WHERE t.trainno IS NULL
),
missing_calls AS (
  SELECT m.d, m.line, m.trainno, g.h,
    -- persisted: the same train was already missing on the previous weekday (Monday looks back to Friday)
    EXISTS (SELECT 1 FROM missing q
            WHERE q.d = IF(EXTRACT(DAYOFWEEK FROM m.d) = 2, DATE_SUB(m.d, INTERVAL 3 DAY), DATE_SUB(m.d, INTERVAL 1 DAY))
              AND q.line = m.line AND q.trainno = m.trainno) persisted
  FROM missing m JOIN gc g ON g.d = m.last_seen AND g.line = m.line AND g.trainno = m.trainno
),
cut AS (
  SELECT d,
    COUNTIF(h>=6.5 AND h<9) am_cut_raw, COUNTIF(h>=7 AND h<9) amrest_cut_raw, COUNTIF(h>=15 AND h<18) pm_cut_raw,
    COUNTIF(NOT persisted AND h>=6.5 AND h<9) am_cut_new_raw, COUNTIF(NOT persisted AND h>=7 AND h<9) amrest_cut_new_raw,
    COUNTIF(NOT persisted AND h>=15 AND h<18) pm_cut_new_raw,
    COUNT(DISTINCT CONCAT(line, trainno)) missing_trips, COUNT(DISTINCT IF(NOT persisted, CONCAT(line, trainno), NULL)) missing_trips_new
  FROM missing_calls GROUP BY d
),
-- trains in the day's plan that are not standing: a retiming's other half
added AS (
  SELECT t.d, COUNTIF(g.h>=6.5 AND g.h<9) am_add, COUNTIF(g.h>=7 AND g.h<9) amrest_add, COUNTIF(g.h>=15 AND g.h<18) pm_add
  FROM trips t LEFT JOIN standing s ON s.d = t.d AND s.line = t.line AND s.trainno = t.trainno
  JOIN gc g ON g.d = t.d AND g.line = t.line AND g.trainno = t.trainno
  WHERE s.trainno IS NULL AND t.d >= DATE '2025-04-10'
  GROUP BY t.d
),
-- planned trains that never appeared in ET at all, on a line that did run that day
et AS (
  SELECT DISTINCT operating_date d, line, REGEXP_EXTRACT(dsj, r'DatedServiceJourney:(\d+)[-_]') trainno
  FROM `disruptions.togvarsel_calls` WHERE NOT IFNULL(extra_journey, FALSE)
),
miss AS (
  SELECT g.d, COUNTIF(h>=6.5 AND h<9) am_miss, COUNTIF(h>=7 AND h<9) amrest_miss, COUNTIF(h>=15 AND h<18) pm_miss
  FROM gc g
  JOIN (SELECT DISTINCT d, line FROM et) running USING (d, line)
  LEFT JOIN et USING (d, line, trainno)
  WHERE g.trainno IS NOT NULL AND et.trainno IS NULL
  GROUP BY g.d
)
SELECT x.d,
  IFNULL(m.am_miss, 0) am_miss, IFNULL(m.amrest_miss, 0) amrest_miss, IFNULL(m.pm_miss, 0) pm_miss,
  GREATEST(IFNULL(c.am_cut_raw, 0) - IFNULL(a.am_add, 0), 0) am_cut,
  GREATEST(IFNULL(c.amrest_cut_raw, 0) - IFNULL(a.amrest_add, 0), 0) amrest_cut,
  GREATEST(IFNULL(c.pm_cut_raw, 0) - IFNULL(a.pm_add, 0), 0) pm_cut,
  GREATEST(IFNULL(c.am_cut_new_raw, 0) - IFNULL(a.am_add, 0), 0) am_cut_new,
  GREATEST(IFNULL(c.amrest_cut_new_raw, 0) - IFNULL(a.amrest_add, 0), 0) amrest_cut_new,
  GREATEST(IFNULL(c.pm_cut_new_raw, 0) - IFNULL(a.pm_add, 0), 0) pm_cut_new,
  IFNULL(c.missing_trips, 0) missing_trips, IFNULL(c.missing_trips_new, 0) missing_trips_new
FROM days x
LEFT JOIN miss m USING (d) LEFT JOIN cut c USING (d) LEFT JOIN added a USING (d)
WHERE x.d >= DATE '2025-04-10'
