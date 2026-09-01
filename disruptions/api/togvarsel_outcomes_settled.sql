-- The AUTHORITATIVE realized rush outcome, per (Oslo day, scope), for /rushhistorikk.
--
-- This is the half that must agree with the models. It reproduces the training chain's label
-- expressions rather than restating them, and there are TWO sources because there are two
-- expressions -- see the rounding note below. Keep this file, `data/queries/togvarsel/
-- 03_day_windows.sql` and `data/queries/togvarsel/08_features_cat.sql` in step: if the product bar
-- ever moves, all three move, and the pointer in each header is the only thing that says so.
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
-- ROUNDING IS PER SCOPE, and it is not cosmetic. 03_day_windows.sql emits
-- ROUND(SAFE_DIVIDE(...), 4), which 04_days.sql carries into am_t10/pm_t10; 08_features_cat.sql
-- emits a RAW SAFE_DIVIDE for am_t10_rest0700. One rule for all three breaks the identity the page
-- rests on -- that a non-green realized level coincides exactly with the training label being true
-- -- in one direction or the other. Worked example: 204 troubled of 2041 resolved is 0.0999510 raw.
-- Rounded it is 0.1000, so y_am10 is TRUE and the level must be yellow; unrounded it is below the
-- 0.10 bar and the level would read green, with the label still TRUE. Applying the ROUND to
-- `amrest` fails the mirror image, because y_amrest10 reads the raw value.
--
-- The rush windows are half-open on the AIMED time in Europe/Oslo, and the day key is the Oslo
-- date of that aimed time -- NOT operatingDate. That is 03_day_windows.sql's `local_date`, and it
-- is why the operatingDate prune below reaches a day either side: an after-midnight operating day
-- otherwise drops calls out of the window they belong to.
--
-- `last_recorded` is a once-daily batch (measured 2026-09-01: every partition shares one
-- last_modified_time of ~03:04 UTC, and the current day's partition held 115 k rows against
-- 1.81 M for the previous full day). So this query cannot answer for today, which is the whole
-- reason togvarsel_outcomes_provisional.sql exists.
--
-- Cost: 32 MB for one day, measured by dry run against ent-kixavvik-dev on 2026-09-01. A full
-- 17-month backfill is 7.55 GB (~$0.04). The caller also sets maximum_bytes_billed as a
-- blast-radius bound -- not a pruning check, since a too-narrow bound reads as a CHEAPER query.
SELECT
  d,
  scope,
  n_calls,
  n_resolved,
  n_calls - n_resolved AS n_unresolved,
  -- Per-scope rounding, per the header. The bar itself lives in Python (one dict, three scopes) so
  -- the level mapping and the eligibility test cannot drift apart from each other.
  IF(scope = 'amrest', trouble10_raw, ROUND(trouble10_raw, 4)) AS trouble10
FROM (
  SELECT
    d,
    scope,
    COUNT(*) AS n_calls,
    COUNTIF(cancelled OR has_actual) AS n_resolved,
    -- The product bar, verbatim from 03_day_windows.sql: cancelled OR >= 10 min late, over the
    -- calls with a KNOWN outcome. The denominator is resolved calls, never COUNT(*) -- a call that
    -- neither ran nor was cancelled says nothing about the rush and is counted separately.
    SAFE_DIVIDE(
      COUNTIF(cancelled OR delay_min >= 10),
      COUNTIF(cancelled OR has_actual)
    ) AS trouble10_raw
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
          DATETIME(
            COALESCE(e.aimedDepartureTime, e.aimedArrivalTime), 'Europe/Oslo'
          ) AS aimed_local,
          (IFNULL(e.journeyCancellation, FALSE) OR IFNULL(e.stopCancellation, FALSE)) AS cancelled,
          -- Arrival preferred, departure as fallback -- 03_day_windows.sql's own COALESCE order.
          -- Note this differs from label_varsel_decisions.sql, which is departure-only: same bar,
          -- different delay field, and togvarsel's is the one that governs here.
          COALESCE(
            TIMESTAMP_DIFF(e.arrivalTime, e.aimedArrivalTime, SECOND) / 60.0,
            TIMESTAMP_DIFF(e.departureTime, e.aimedDepartureTime, SECOND) / 60.0
          ) AS delay_min,
          (e.arrivalTime IS NOT NULL OR e.departureTime IS NOT NULL) AS has_actual
        FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded` AS e
        JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` AS q
          ON e.stopPointRef = q.id
        -- INNER join: togvarsel_oslo_stations DEFINES the product's geography (the 39 stations
        -- Asker - Oslo S - Lillestrøm and Oslo S - Ski). Never duplicate that list in SQL or
        -- Python -- 00_oslo_stations.sql is the one place it lives.
        JOIN `disruptions.togvarsel_oslo_stations` AS s
          ON q.stopPlaceRef = s.stop_place
        WHERE e.operatingDate
                BETWEEN DATE_SUB(@from_date, INTERVAL 1 DAY) AND DATE_ADD(@to_date, INTERVAL 1 DAY)
          AND e.dataSource = 'VYG'
          AND e.vehicleMode = 'rail'
          AND NOT IFNULL(e.extraJourney, FALSE)
      )
      -- The window predicate proper, on the Oslo aimed date. The operatingDate prune above is a
      -- COVER for it, deliberately one day wider.
      WHERE DATE(aimed_local) BETWEEN @from_date AND @to_date
    ) AS w
    CROSS JOIN UNNEST([
      STRUCT('am' AS scope, 6.5 AS lo, 9.0 AS hi),
      STRUCT('amrest', 7.0, 9.0),
      STRUCT('pm', 15.0, 18.0)
    ]) AS sc
    -- Half-open, matching 03_day_windows.sql's CASE bins. `amrest` deliberately overlaps `am`:
    -- they are two questions about one morning, asked by two models at two cutoffs.
    WHERE w.h >= sc.lo AND w.h < sc.hi
  )
  GROUP BY d, scope
)
