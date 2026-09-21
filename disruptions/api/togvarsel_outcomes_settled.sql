-- The AUTHORITATIVE realized rush outcome, per (Oslo day, scope), for /rushhistorikk.
--
-- This is the half that must agree with the models. It reproduces the training chain's label
-- expressions rather than restating them, and there are TWO sources because there are two
-- expressions -- see the rounding note below. Keep this file, `data/queries/togvarsel/
-- 03_day_windows.sql`, `04_days.sql`, `08_features_cat.sql` and `03b_plan.sql` in step: if the
-- product bar or the label rule ever moves, all of them move, and the pointer in each header is
-- the only thing that says so.
--
-- Scope -> model -> label, from `data/queries/togvarsel/10_train_models.sql`:
--   am      06:30-09:00  togvarsel_m_am0630  y_am10      am_t10 >= 0.10
--   amrest  07:00-09:00  togvarsel_m_am0700  y_amrest10  am_t10_rest0700 >= 0.10
--   pm      15:00-18:00  togvarsel_m_pm1200  y_pm10      pm_t10 >= 0.15
--                        togvarsel_m_pm1400  y_pm10      (the same label; two ask times)
-- `amrest` is NOT a window `03_day_windows.sql` has -- its bins are early/am_rush/midday/pm_rush/
-- evening/night -- so a file claiming to reproduce 03 alone would have nothing to reproduce for
-- the 0700 issue. It comes from 08_features_cat.sql.
--
-- THE LABEL RULE SINCE 2026-09-21 (docs/rushvarsel/02-removed-journeys, 04-cuts). Vy sometimes
-- takes a train out instead of cancelling it -- pulled from ET with no Cancellation mid-day, or
-- dropped from the day's plan the evening before -- and the product owner ruled the day-before cut
-- a bad rush. The ET history cannot see either, so three terms join the bar's own two:
--   * an ESTIMATED time is not an observation: last_recorded fills the unrun tail of a journey that
--     went silent with its last expected times, which read as punctual under the old rule;
--   * an estimated call whose journey went silent more than 10 min before the call was due never
--     ran -- counted as cancelled;
--   * the planned Oslo-area calls of trains that never ran are added to BOTH numerator and
--     denominator: GTFS trains absent from ET that day on a line that did run (the intraday pull),
--     and STANDING trains (in the plan on >= 3 of the 4 previous same-weekday dates) cut from the
--     day's plan, netted against trains new that day and not already cut on the previous weekday.
-- That is 03b_plan.sql's `*_miss + *_cut_new`, computed here for the span rather than read from
-- the monthly-rebuilt table, because this pass settles a day the morning after. RE20's numbering
-- does not join and is excluded, as there.
--
-- ROUNDING IS PER SCOPE, and it is not cosmetic. 03_day_windows.sql / 04_days.sql emit
-- ROUND(SAFE_DIVIDE(...), 4) for am_t10/pm_t10; 08_features_cat.sql emits a RAW SAFE_DIVIDE for
-- am_t10_rest0700. One rule for all three breaks the identity the page rests on -- that a non-green
-- realized level coincides exactly with the training label being true -- in one direction or the
-- other. Worked example: 204 troubled of 2041 resolved is 0.0999510 raw. Rounded it is 0.1000, so
-- y_am10 is TRUE and the level must be yellow; unrounded it is below the 0.10 bar and the level
-- would read green, with the label still TRUE. Applying the ROUND to `amrest` fails the mirror
-- image, because y_amrest10 reads the raw value.
--
-- The rush windows are half-open on the AIMED time in Europe/Oslo, and the day key is the Oslo
-- date of that aimed time -- NOT operatingDate. That is 03_day_windows.sql's `local_date`, and it
-- is why the operatingDate prune below reaches a day either side: an after-midnight operating day
-- otherwise drops calls out of the window they belong to. The plan terms are keyed on the
-- operating date, as 03b_plan.sql keys them; inside 06:30-18:00 the two dates agree.
--
-- `last_recorded` is a once-daily batch (measured 2026-09-01: every partition shares one
-- last_modified_time of ~03:04 UTC, and the current day's partition held 115 k rows against
-- 1.81 M for the previous full day). So this query cannot answer for today, which is the whole
-- reason togvarsel_outcomes_provisional.sql exists.
--
-- Cost: the plan side reads the span plus 35 days of VYG GTFS (~0.04 GB a partition), so a
-- 14-day chunk is ~2 GB where the old query was 32 MB a day -- which is why the caller's
-- maximum_bytes_billed is 6 GiB. Still a blast-radius bound, not a pruning check: a too-narrow
-- bound reads as a CHEAPER query.
WITH
et AS (
  SELECT
    e.operatingDate AS od,
    REGEXP_REPLACE(e.lineRef, r'^VYG:Line:', '') AS line,
    REGEXP_EXTRACT(e.datedServiceJourneyId, r'DatedServiceJourney:(\d+)[-_]') AS trainno,
    e.stopPointRef AS quay,
    DATETIME(COALESCE(e.aimedDepartureTime, e.aimedArrivalTime), 'Europe/Oslo') AS aimed_local,
    (IFNULL(e.journeyCancellation, FALSE) OR IFNULL(e.stopCancellation, FALSE)) AS cancelled,
    -- Arrival preferred, departure as fallback -- 03_day_windows.sql's own COALESCE order.
    -- Note this differs from label_varsel_decisions.sql, which is departure-only: same bar,
    -- different delay field, and togvarsel's is the one that governs here.
    COALESCE(
      TIMESTAMP_DIFF(e.arrivalTime, e.aimedArrivalTime, SECOND) / 60.0,
      TIMESTAMP_DIFF(e.departureTime, e.aimedDepartureTime, SECOND) / 60.0
    ) AS delay_min,
    (e.arrivalTime IS NOT NULL OR e.departureTime IS NOT NULL) AND NOT IFNULL(e.estimated, FALSE) AS observed,
    (IFNULL(e.estimated, FALSE)
      AND (e.arrivalTime IS NOT NULL OR e.departureTime IS NOT NULL)
      AND NOT (IFNULL(e.journeyCancellation, FALSE) OR IFNULL(e.stopCancellation, FALSE))
      AND e.recordedAtTime < TIMESTAMP_SUB(COALESCE(e.aimedDepartureTime, e.aimedArrivalTime), INTERVAL 10 MINUTE)) AS silent
  FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded` AS e
  WHERE e.operatingDate BETWEEN DATE_SUB(@from_date, INTERVAL 1 DAY) AND DATE_ADD(@to_date, INTERVAL 1 DAY)
    AND e.dataSource = 'VYG'
    AND e.vehicleMode = 'rail'
    AND NOT IFNULL(e.extraJourney, FALSE)
),
-- Half-open, matching 03_day_windows.sql's CASE bins. `amrest` deliberately overlaps `am`: they
-- are two questions about one morning, asked by two models at two cutoffs.
scopes AS (
  SELECT * FROM UNNEST([
    STRUCT('am' AS scope, 6.5 AS lo, 9.0 AS hi),
    STRUCT('amrest', 7.0, 9.0),
    STRUCT('pm', 15.0, 18.0)
  ])
),
calls AS (
  SELECT
    DATE(et.aimed_local) AS d,
    EXTRACT(HOUR FROM et.aimed_local) + EXTRACT(MINUTE FROM et.aimed_local) / 60.0 AS h,
    et.cancelled, et.delay_min, et.observed, et.silent
  FROM et
  JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
    ON et.quay = q.id
  -- INNER join: togvarsel_oslo_stations DEFINES the product's geography (the 39 stations
  -- Asker - Oslo S - Lillestrøm and Oslo S - Ski). Never duplicate that list in SQL or
  -- Python -- 00_oslo_stations.sql is the one place it lives.
  JOIN `disruptions.togvarsel_oslo_stations` AS s
    ON q.stopPlaceRef = s.stop_place
  -- The window predicate proper, on the Oslo aimed date. The operatingDate prune above is a
  -- COVER for it, deliberately one day wider.
  WHERE DATE(et.aimed_local) BETWEEN @from_date AND @to_date
),
realized AS (
  SELECT
    c.d, sc.scope,
    COUNT(*) AS n_calls,
    -- The denominator is calls with a KNOWN outcome, never COUNT(*) -- a call that neither ran nor
    -- was cancelled says nothing about the rush and is counted separately.
    COUNTIF(c.cancelled OR c.silent OR c.observed) AS n_res,
    COUNTIF(c.cancelled OR c.silent OR (c.observed AND c.delay_min >= 10)) AS n_t
  FROM calls AS c CROSS JOIN scopes AS sc
  WHERE c.h >= sc.lo AND c.h < sc.hi
  GROUP BY c.d, sc.scope
),
-- ---- the plan side: the trains that never ran -------------------------------------------------
gc AS (
  SELECT
    g.operating_date AS d, g.route_short_name AS line,
    REGEXP_EXTRACT(g.trip_id, r'ServiceJourney:(\d+)[-_]') AS trainno,
    EXTRACT(HOUR FROM DATETIME(COALESCE(g.arrival_time, g.departure_time), 'Europe/Oslo'))
      + EXTRACT(MINUTE FROM DATETIME(COALESCE(g.arrival_time, g.departure_time), 'Europe/Oslo')) / 60.0 AS h
  FROM `ent-data-sharing-ext-prd.timetable_gtfs.gtfs_last_recorded_ent_v1` AS g
  JOIN `disruptions.togvarsel_oslo_stations` AS s
    ON s.stop_place = g.parent_station
  -- four reference weeks plus the previous-weekday chain before the span; the table requires a
  -- partition filter and a parameter expression counts as one
  WHERE g.operating_date BETWEEN DATE_SUB(@from_date, INTERVAL 35 DAY) AND @to_date
    AND g.agency_id LIKE 'VYG%' AND g.route_type = 'Train' AND g.route_short_name != 'RE20'
),
trips AS (SELECT DISTINCT d, line, trainno FROM gc WHERE trainno IS NOT NULL),
refs AS (
  SELECT DISTINCT d FROM trips WHERE d BETWEEN DATE_SUB(@from_date, INTERVAL 3 DAY) AND @to_date
),
standing AS (
  SELECT x.d, p.line, p.trainno, MAX(p.d) AS last_seen
  FROM refs AS x
  JOIN trips AS p
    ON p.d IN (DATE_SUB(x.d, INTERVAL 7 DAY), DATE_SUB(x.d, INTERVAL 14 DAY),
               DATE_SUB(x.d, INTERVAL 21 DAY), DATE_SUB(x.d, INTERVAL 28 DAY))
  GROUP BY x.d, p.line, p.trainno
  HAVING COUNT(*) >= 3
),
missing AS (
  SELECT s.d, s.line, s.trainno, s.last_seen
  FROM standing AS s LEFT JOIN trips AS t USING (d, line, trainno)
  WHERE t.trainno IS NULL
),
missing_calls AS (
  -- a standing train cut from the day's plan, its calls from its last appearance; persisted = it
  -- was already missing on the previous weekday (Monday looks back to Friday)
  SELECT m.d, g.h, (q.trainno IS NOT NULL) AS persisted
  FROM missing AS m
  JOIN gc AS g ON g.d = m.last_seen AND g.line = m.line AND g.trainno = m.trainno
  LEFT JOIN missing AS q
    ON q.d = IF(EXTRACT(DAYOFWEEK FROM m.d) = 2, DATE_SUB(m.d, INTERVAL 3 DAY), DATE_SUB(m.d, INTERVAL 1 DAY))
   AND q.line = m.line AND q.trainno = m.trainno
  WHERE m.d BETWEEN @from_date AND @to_date
),
added AS (
  -- trains in the day's plan that are not standing: a retiming's other half
  SELECT t.d, g.h
  FROM trips AS t
  LEFT JOIN standing AS s ON s.d = t.d AND s.line = t.line AND s.trainno = t.trainno
  JOIN gc AS g ON g.d = t.d AND g.line = t.line AND g.trainno = t.trainno
  WHERE s.trainno IS NULL AND t.d BETWEEN @from_date AND @to_date
),
et_trips AS (SELECT DISTINCT od AS d, line, trainno FROM et WHERE trainno IS NOT NULL),
miss_calls AS (
  -- planned trains that never appeared in ET at all, on a line that did run that day
  SELECT g.d, g.h
  FROM gc AS g
  JOIN (SELECT DISTINCT d, line FROM et_trips) AS running USING (d, line)
  LEFT JOIN et_trips AS e USING (d, line, trainno)
  WHERE g.trainno IS NOT NULL AND e.trainno IS NULL AND g.d BETWEEN @from_date AND @to_date
),
miss_w AS (
  SELECT mc.d, sc.scope, COUNT(*) AS n
  FROM miss_calls AS mc CROSS JOIN scopes AS sc WHERE mc.h >= sc.lo AND mc.h < sc.hi GROUP BY 1, 2
),
cut_w AS (
  SELECT m.d, sc.scope, COUNTIF(NOT m.persisted) AS n_new
  FROM missing_calls AS m CROSS JOIN scopes AS sc WHERE m.h >= sc.lo AND m.h < sc.hi GROUP BY 1, 2
),
add_w AS (
  SELECT a.d, sc.scope, COUNT(*) AS n
  FROM added AS a CROSS JOIN scopes AS sc WHERE a.h >= sc.lo AND a.h < sc.hi GROUP BY 1, 2
),
extra AS (
  SELECT k.d, k.scope,
    IFNULL(miss_w.n, 0) + GREATEST(IFNULL(cut_w.n_new, 0) - IFNULL(add_w.n, 0), 0) AS n
  FROM (SELECT d, scope FROM miss_w UNION DISTINCT SELECT d, scope FROM cut_w) AS k
  LEFT JOIN miss_w USING (d, scope)
  LEFT JOIN cut_w USING (d, scope)
  LEFT JOIN add_w USING (d, scope)
)
SELECT
  r.d,
  r.scope,
  -- the planned calls that never ran are IN the counts the page shows, as they are in the label
  r.n_calls + IFNULL(x.n, 0) AS n_calls,
  r.n_res + IFNULL(x.n, 0) AS n_resolved,
  r.n_calls - r.n_res AS n_unresolved,
  -- Per-scope rounding, per the header. The bar itself lives in Python (one dict, three scopes) so
  -- the level mapping and the eligibility test cannot drift apart from each other.
  IF(r.scope = 'amrest',
     SAFE_DIVIDE(r.n_t + IFNULL(x.n, 0), r.n_res + IFNULL(x.n, 0)),
     ROUND(SAFE_DIVIDE(r.n_t + IFNULL(x.n, 0), r.n_res + IFNULL(x.n, 0)), 4)) AS trouble10
FROM realized AS r
LEFT JOIN extra AS x USING (d, scope)
