-- The PROVISIONAL realized rush outcome for TODAY, per scope, for /rushhistorikk.
--
-- Why this file exists at all: the authoritative source is a once-daily batch. Measured
-- 2026-09-01, every `last_recorded` partition shares one last_modified_time of ~03:04 UTC and the
-- current day's held 115 k rows against 1.81 M for the previous full day, so
-- togvarsel_outcomes_settled.sql cannot answer for the morning rush at 09:20. The product asks for
-- a check after each rush, so this pass answers from tables the POLLER ALREADY BUILDS plus the
-- day's GTFS plan, and the nightly pass overwrites it with the value that agrees with the models.
--
-- FIVE KNOWN SKEWS against the training label, and naming them is the point. This value is
-- provisional because of them, `settled = FALSE` says so on the row, and /rushhistorikk excludes it
-- from every rate while still showing it on the day row:
--
--   1. DELAY FIELD. The label prefers ARRIVAL delay and falls back to departure
--      (03_day_windows.sql's COALESCE(arr, dep)). Neither source here carries arrival delay:
--      rail_calls_today has dep_delay_min only, and rail_calls_planned_today computes the same
--      departure figure. So this is a departure-delay reading of a bar defined on arrival.
--   2. STALENESS. Both sources are TTL-gated off the SX-ingestion rebuild, not rebuilt every tick
--      -- main._RAIL_CALLS_TTL is 15 min and main._PLANNED_TTL is 60 min -- and both run inside
--      _rebuild_all, which fires when the ingestion timestamp moves. A call late in the window can
--      therefore still be unresolved here; n_unresolved is what says how much of the window that
--      is, and it is why the due times are :20 rather than :05.
--   3. EXTRA JOURNEYS. The label excludes them (`NOT extraJourney`). Neither source carries the
--      flag, so a replacement or renumbered run is counted here and not there.
--   4. TODAY ONLY. Both sources are CREATE OR REPLACE for the current date, so this pass can never
--      resolve a past day. A missed rush waits for the nightly pass.
--   5. SILENT TAILS. The label counts an estimated call whose journey went quiet > 10 min before
--      it was due as cancelled (docs/rushvarsel/04-cuts). The planned table carries no snapshot
--      time, so here such a call is merely unresolved -- the direction that reads LOW.
--
-- WHAT IT DOES CARRY OF THE 2026-09-21 RULE: the planned Oslo-area calls of trains that never ran,
-- added to numerator and denominator exactly as the settled query and 03b_plan.sql do -- GTFS
-- trains absent from today's ET (the planned table's journeys with a call today, on a line that
-- did run) and STANDING trains cut from today's plan, netted against trains new today and not
-- already cut on the previous weekday. The day's GTFS partition is written ~02:06, so it is
-- there at 09:20. RE20 excluded, as there.
--
-- ONE ROW PER CALL, and that is a correctness property rather than tidiness.
-- `rail_calls_planned_today` is the call UNIVERSE: it unions ET's recordedCalls and estimatedCalls
-- and marks which in `call_source`, so it already carries the ET-declared cancellations that live
-- only in estimatedCalls -- the ones that drive trouble10 on a bad morning. `rail_calls_today` is
-- recorded-only and exists here purely for the FRESHER delay (15 min vs 60). They are therefore
-- LEFT JOINED, never unioned: a union would count every cancelled recorded call twice, in the
-- numerator and the denominator both, and ten duplicates among a hundred resolved calls moves
-- 0.10 to roughly 0.18 -- a different published level.
--
-- Both sources filter `lineRef LIKE 'VYG:%'`, which is the label's `dataSource = 'VYG'`.
WITH
scopes AS (
  SELECT * FROM UNNEST([
    STRUCT('am' AS scope, 6.5 AS lo, 9.0 AS hi),
    STRUCT('amrest', 7.0, 9.0),
    STRUCT('pm', 15.0, 18.0)
  ])
  -- Only the scopes the caller asked for: the morning pair at 09:20, `pm` at 18:20. Passing
  -- the list keeps the due-time policy in Python beside the rest of the schedule.
  WHERE scope IN UNNEST(@scopes)
),
w AS (
  SELECT
    DATE(aimed_local) AS d,
    EXTRACT(HOUR FROM aimed_local) + EXTRACT(MINUTE FROM aimed_local) / 60.0 AS h,
    cancelled, delay_min, has_actual
  FROM (
    SELECT
      DATETIME(COALESCE(p.aimed_dep, p.aimed_arr), 'Europe/Oslo') AS aimed_local,
      -- Either source may know a call is cancelled; planned_today is the one that sees an
      -- ET-declared cancellation on a train that has not run yet.
      (IFNULL(p.call_cancelled, FALSE) OR IFNULL(t.call_cancelled, FALSE)) AS cancelled,
      -- The fresher figure wins. Both are departure delay (skew 1).
      COALESCE(t.dep_delay_min, p.dep_delay_min) AS delay_min,
      -- A call has an outcome once it has been RECORDED. planned_today marks that in
      -- call_source; an estimated call is still a forecast, so it is unresolved unless it has
      -- been cancelled -- which the numerator below already treats as trouble.
      (p.call_source = 'recorded' OR t.jkey IS NOT NULL) AS has_actual
    FROM `disruptions.rail_calls_planned_today` AS p
    -- LEFT, on the full call key: planned_today is the universe, so every planned call appears
    -- exactly once whether or not it has run.
    LEFT JOIN `disruptions.rail_calls_today` AS t
      ON p.jkey = t.jkey AND p.stop_order = t.stop_order
    JOIN `disruptions.togvarsel_oslo_stations` AS s
      ON p.stop_place = s.stop_place
    WHERE COALESCE(p.aimed_dep, p.aimed_arr) IS NOT NULL
  )
  WHERE DATE(aimed_local) = @day
),
realized AS (
  SELECT
    w.d, sc.scope,
    COUNT(*) AS n_calls,
    COUNTIF(w.cancelled OR w.has_actual) AS n_res,
    COUNTIF(w.cancelled OR w.delay_min >= 10) AS n_t
  FROM w CROSS JOIN scopes AS sc
  WHERE w.h >= sc.lo AND w.h < sc.hi
  GROUP BY w.d, sc.scope
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
  -- today, the previous weekday, and four reference weeks behind each; the table requires a
  -- partition filter, spelled out as parameter expressions (a subquery does not count)
  WHERE g.operating_date IN (
      @day, DATE_SUB(@day, INTERVAL 7 DAY), DATE_SUB(@day, INTERVAL 14 DAY),
      DATE_SUB(@day, INTERVAL 21 DAY), DATE_SUB(@day, INTERVAL 28 DAY),
      DATE_SUB(@day, INTERVAL 1 DAY), DATE_SUB(@day, INTERVAL 8 DAY), DATE_SUB(@day, INTERVAL 15 DAY),
      DATE_SUB(@day, INTERVAL 22 DAY), DATE_SUB(@day, INTERVAL 29 DAY),
      DATE_SUB(@day, INTERVAL 3 DAY), DATE_SUB(@day, INTERVAL 10 DAY), DATE_SUB(@day, INTERVAL 17 DAY),
      DATE_SUB(@day, INTERVAL 24 DAY), DATE_SUB(@day, INTERVAL 31 DAY))
    AND g.agency_id LIKE 'VYG%' AND g.route_type = 'Train' AND g.route_short_name != 'RE20'
),
trips AS (SELECT DISTINCT d, line, trainno FROM gc WHERE trainno IS NOT NULL),
refs AS (
  SELECT * FROM UNNEST([
    @day,
    IF(EXTRACT(DAYOFWEEK FROM @day) = 2, DATE_SUB(@day, INTERVAL 3 DAY), DATE_SUB(@day, INTERVAL 1 DAY))
  ]) AS d
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
  SELECT g.h, (q.trainno IS NOT NULL) AS persisted
  FROM missing AS m
  JOIN gc AS g ON g.d = m.last_seen AND g.line = m.line AND g.trainno = m.trainno
  LEFT JOIN missing AS q ON q.d != @day AND q.line = m.line AND q.trainno = m.trainno
  WHERE m.d = @day
),
added AS (
  SELECT g.h
  FROM trips AS t
  LEFT JOIN standing AS s ON s.d = t.d AND s.line = t.line AND s.trainno = t.trainno
  JOIN gc AS g ON g.d = t.d AND g.line = t.line AND g.trainno = t.trainno
  WHERE s.trainno IS NULL AND t.d = @day
),
et_trips AS (
  -- today's journeys the feed carries, by train number and line
  SELECT DISTINCT
    REGEXP_REPLACE(lineRef, r'^VYG:Line:', '') AS line,
    REGEXP_EXTRACT(jkey, r'DatedServiceJourney:(\d+)[-_]') AS trainno
  FROM `disruptions.rail_calls_planned_today`
  WHERE call_date = @day
),
miss_calls AS (
  SELECT g.h
  FROM gc AS g
  JOIN (SELECT DISTINCT line FROM et_trips) AS running USING (line)
  LEFT JOIN et_trips AS e USING (line, trainno)
  WHERE g.d = @day AND g.trainno IS NOT NULL AND e.trainno IS NULL
),
miss_w AS (
  SELECT sc.scope, COUNT(*) AS n FROM miss_calls AS mc CROSS JOIN scopes AS sc WHERE mc.h >= sc.lo AND mc.h < sc.hi GROUP BY 1
),
cut_w AS (
  SELECT sc.scope, COUNTIF(NOT m.persisted) AS n_new FROM missing_calls AS m CROSS JOIN scopes AS sc WHERE m.h >= sc.lo AND m.h < sc.hi GROUP BY 1
),
add_w AS (
  SELECT sc.scope, COUNT(*) AS n FROM added AS a CROSS JOIN scopes AS sc WHERE a.h >= sc.lo AND a.h < sc.hi GROUP BY 1
),
extra AS (
  SELECT sc.scope, IFNULL(miss_w.n, 0) + GREATEST(IFNULL(cut_w.n_new, 0) - IFNULL(add_w.n, 0), 0) AS n
  FROM scopes AS sc
  LEFT JOIN miss_w USING (scope) LEFT JOIN cut_w USING (scope) LEFT JOIN add_w USING (scope)
)
SELECT
  r.d,
  r.scope,
  r.n_calls + IFNULL(x.n, 0) AS n_calls,
  r.n_res + IFNULL(x.n, 0) AS n_resolved,
  r.n_calls - r.n_res AS n_unresolved,
  -- Rounded as `am`/`pm` are; the caller writes this to provisional_trouble10 and never compares it
  -- to a training label, so the per-scope rounding split in the settled query does not apply.
  ROUND(SAFE_DIVIDE(r.n_t + IFNULL(x.n, 0), r.n_res + IFNULL(x.n, 0)), 4) AS trouble10
FROM realized AS r
LEFT JOIN extra AS x USING (scope)
