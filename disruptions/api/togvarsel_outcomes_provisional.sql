-- The PROVISIONAL realized rush outcome for TODAY, per scope, for /rushhistorikk.
--
-- Why this file exists at all: the authoritative source is a once-daily batch. Measured
-- 2026-09-01, every `last_recorded` partition shares one last_modified_time of ~03:04 UTC and the
-- current day's held 115 k rows against 1.81 M for the previous full day, so
-- togvarsel_outcomes_settled.sql cannot answer for the morning rush at 09:20. The product asks for
-- a check after each rush, so this pass answers from tables the POLLER ALREADY BUILDS -- adding no
-- BigQuery scan of its own beyond reading two small materialized tables -- and the nightly pass
-- overwrites it with the value that agrees with the models.
--
-- FOUR KNOWN SKEWS against the training label, and naming them is the point. This value is
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
SELECT
  d,
  scope,
  n_calls,
  n_resolved,
  n_calls - n_resolved AS n_unresolved,
  -- Rounded as `am`/`pm` are; the caller writes this to provisional_trouble10 and never compares it
  -- to a training label, so the per-scope rounding split in the settled query does not apply.
  ROUND(
    SAFE_DIVIDE(n_trouble, n_resolved), 4
  ) AS trouble10
FROM (
  SELECT
    d,
    scope,
    COUNT(*) AS n_calls,
    COUNTIF(cancelled OR has_actual) AS n_resolved,
    COUNTIF(cancelled OR delay_min >= 10) AS n_trouble
  FROM (
    SELECT
      w.d,
      sc.scope,
      w.cancelled,
      w.delay_min,
      w.has_actual
    FROM (
      SELECT
        DATE(aimed_local) AS d,
        EXTRACT(HOUR FROM aimed_local) + EXTRACT(MINUTE FROM aimed_local) / 60.0 AS h,
        cancelled,
        delay_min,
        has_actual
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
          -- been cancelled -- which the numerator above already treats as trouble.
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
    ) AS w
    CROSS JOIN UNNEST([
      STRUCT('am' AS scope, 6.5 AS lo, 9.0 AS hi),
      STRUCT('amrest', 7.0, 9.0),
      STRUCT('pm', 15.0, 18.0)
    ]) AS sc
    WHERE w.h >= sc.lo AND w.h < sc.hi
      -- Only the scopes the caller asked for: the morning pair at 09:20, `pm` at 18:20. Passing
      -- the list keeps the due-time policy in Python beside the rest of the schedule.
      AND sc.scope IN UNNEST(@scopes)
  )
  GROUP BY d, scope
)
