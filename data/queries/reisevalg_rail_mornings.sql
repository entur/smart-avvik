-- disruptions.reisevalg_rail_mornings -- one row per weekday morning (Mon-Fri,
-- 06:00-09:30 Europe/Oslo, window 2024-06-01..2026-05-31): the rail side of the
-- reisevalg-varsel backtest (car vs train, Lier/Drammen -> Oslo S; plan
-- docs/plans/2026-07-11-001-feat-reisevalg-varsel-feasibility-plan.md, U2).
--
-- CORRIDOR TRAINS: rail_calls journeys on VYG:Line:{L1,R12,R13,RE11} with a call at
-- Lier (NSR:StopPlace:426) -- or Drammen (NSR:StopPlace:11) for trains that skip
-- Lier -- and an Oslo S (NSR:StopPlace:337) call LATER in stop_order within the same
-- jkey (rail_calls has no direction column; direction is derivable only this way).
-- A train is "bad" on the ARRIVAL-side predicate (definition parity with the live
-- module's rail gate in disruptions/api/reisevalg.py: what the passenger experiences
-- at Oslo S): cancelled anywhere on the origin->Oslo S leg, or Oslo S
-- arr_delay_min > 5. Residual instrument mismatch vs live: rail_calls actuals can be
-- missing (train ran but no recorded arrival -> counted NOT late), while the live
-- gate reads JP *expected* arrival; both are arrival-side <=5 min + cancellations.
--
-- CLOSURE FLAG (kept as a flag, rows NOT dropped): mornings where >50% of corridor
-- trains are cancelled, or with zero corridor trains at all (journeys removed from
-- plan), are mass-cancellation / planned-engineering mornings (Drammenbanen summer
-- works). Headline backtest numbers exclude them via the flag.
--
-- HOLIDAY FLAG: hardcoded literal date list (see holiday_dates CTE) so reruns
-- reproduce -- Norwegian public holidays in 2024-06..2026-05, inneklemte dager
-- (bridge days incl. romjul + easter-week Mon-Wed), fellesferie ISO weeks 28-31,
-- school breaks vinterferie uke 8 / hoestferie uke 40 (Oslo/Viken convention).
--
-- RAIL DATA COVERAGE (measured 2026-07-11): rail_calls has essentially no
-- passenger journeys before the 2024-12-15 timetable change (12 journeys/day at
-- Oslo S vs ~757 after; the corridor line IDs L1/R12/R13/RE11 also only exist from
-- that change). in_rail_coverage marks mornings >= 2024-12-15 -- headline backtest
-- numbers must be computed on that slice (~17.5 months, not the nominal 24).
--
-- COST: single scan of disruptions.rail_calls restricted to 4 corridor lines
-- (cluster key) and 8 columns; dry run 0.67 GB -> well under a cent.

CREATE OR REPLACE TABLE `disruptions.reisevalg_rail_mornings` AS
WITH
-- Norwegian holiday calendar for the window, as literals. Derivation:
--   public:     faste + bevegelige helligdager (easter 2025-04-20, 2026-04-05;
--               Kristi himmelfart 2025-05-29, 2026-05-14; 2. pinsedag 2025-06-09,
--               2026-05-25; 17. mai falls on Sat 2025 / Sun 2026 -> no weekday rows).
--   inneklemt:  julaften/romjul/nyttaarsaften weekdays, easter-week Mon-Wed,
--               Fri after 1 May 2025 / Kristi himmelfart / New Year 2026.
--   fellesferie: ISO weeks 28-31 (2024-07-08..2024-08-04, 2025-07-07..2025-08-03).
--   school:     vinterferie uke 8 (2025-02-17..21, 2026-02-16..20),
--               hoestferie uke 40 (2024-09-30..10-04, 2025-09-29..10-03).
holiday_dates AS (
  SELECT DATE(d) AS d, kind FROM UNNEST([
    STRUCT('2024-12-25' AS d, 'public' AS kind), ('2024-12-26', 'public'),
    ('2025-01-01', 'public'),
    ('2025-04-17', 'public'), ('2025-04-18', 'public'), ('2025-04-21', 'public'),
    ('2025-05-01', 'public'), ('2025-05-29', 'public'), ('2025-06-09', 'public'),
    ('2025-12-25', 'public'), ('2025-12-26', 'public'),
    ('2026-01-01', 'public'),
    ('2026-04-02', 'public'), ('2026-04-03', 'public'), ('2026-04-06', 'public'),
    ('2026-05-01', 'public'), ('2026-05-14', 'public'), ('2026-05-25', 'public'),
    ('2024-12-23', 'inneklemt'), ('2024-12-24', 'inneklemt'), ('2024-12-27', 'inneklemt'),
    ('2024-12-30', 'inneklemt'), ('2024-12-31', 'inneklemt'),
    ('2025-04-14', 'inneklemt'), ('2025-04-15', 'inneklemt'), ('2025-04-16', 'inneklemt'),
    ('2025-05-02', 'inneklemt'), ('2025-05-30', 'inneklemt'),
    ('2025-12-22', 'inneklemt'), ('2025-12-23', 'inneklemt'), ('2025-12-24', 'inneklemt'),
    ('2025-12-29', 'inneklemt'), ('2025-12-30', 'inneklemt'), ('2025-12-31', 'inneklemt'),
    ('2026-01-02', 'inneklemt'), ('2026-05-15', 'inneklemt'),
    ('2026-03-30', 'inneklemt'), ('2026-03-31', 'inneklemt'), ('2026-04-01', 'inneklemt')
  ])
  UNION ALL
  SELECT d, 'fellesferie' FROM UNNEST(GENERATE_DATE_ARRAY('2024-07-08', '2024-08-04')) d
  UNION ALL
  SELECT d, 'fellesferie' FROM UNNEST(GENERATE_DATE_ARRAY('2025-07-07', '2025-08-03')) d
  UNION ALL
  SELECT d, 'school' FROM UNNEST(GENERATE_DATE_ARRAY('2024-09-30', '2024-10-04')) d
  UNION ALL
  SELECT d, 'school' FROM UNNEST(GENERATE_DATE_ARRAY('2025-02-17', '2025-02-21')) d
  UNION ALL
  SELECT d, 'school' FROM UNNEST(GENERATE_DATE_ARRAY('2025-09-29', '2025-10-03')) d
  UNION ALL
  SELECT d, 'school' FROM UNNEST(GENERATE_DATE_ARRAY('2026-02-16', '2026-02-20')) d
),
holidays AS (  -- one row per date; public > inneklemt > fellesferie > school
  SELECT d,
         ARRAY_AGG(kind ORDER BY CASE kind WHEN 'public' THEN 0 WHEN 'inneklemt' THEN 1
                                           WHEN 'fellesferie' THEN 2 ELSE 3 END LIMIT 1)[OFFSET(0)] AS kind
  FROM holiday_dates GROUP BY d
),
corridor AS (  -- all corridor-line calls (needed for "cancelled anywhere on the leg")
  SELECT jkey, lineRef, stop_place, stop_order, aimed_dep, arr_delay_min,
         IFNULL(call_cancelled, FALSE) AS cancelled, call_date
  FROM `disruptions.rail_calls`
  WHERE call_date BETWEEN '2024-06-01' AND '2026-05-31'
    AND lineRef IN ('VYG:Line:L1', 'VYG:Line:R12', 'VYG:Line:R13', 'VYG:Line:RE11')
),
origins AS (  -- per journey: the Lier call, or the Drammen call for Lier-skipping trains
  SELECT jkey, lineRef, call_date, stop_place AS origin_stop, stop_order AS origin_order,
         aimed_dep AS origin_aimed_dep
  FROM corridor
  WHERE stop_place IN ('NSR:StopPlace:426', 'NSR:StopPlace:11')
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY jkey  -- jkey is date-scoped (SIRI-ET memory note) -> safe partition key
    ORDER BY IF(stop_place = 'NSR:StopPlace:426', 0, 1), stop_order
  ) = 1
),
legs AS (  -- corridor train = origin call with an Oslo S call later in stop_order
  SELECT o.*, s.stop_order AS oslo_order, s.arr_delay_min AS oslo_arr_delay_min
  FROM origins o
  JOIN corridor s
    ON s.jkey = o.jkey AND s.stop_place = 'NSR:StopPlace:337' AND s.stop_order > o.origin_order
  QUALIFY ROW_NUMBER() OVER (PARTITION BY o.jkey ORDER BY s.stop_order) = 1
),
trains AS (  -- morning filter + per-train badness on the arrival-side predicate
  SELECT
    l.call_date,
    l.jkey,
    LOGICAL_OR(c.cancelled) AS leg_cancelled,  -- cancelled anywhere origin..Oslo S
    ANY_VALUE(l.oslo_arr_delay_min) AS oslo_arr_delay_min
  FROM legs l
  JOIN corridor c
    ON c.jkey = l.jkey AND c.stop_order BETWEEN l.origin_order AND l.oslo_order
  WHERE MOD(EXTRACT(DAYOFWEEK FROM l.call_date) + 5, 7) < 5  -- Mon-Fri
    AND TIME(DATETIME(l.origin_aimed_dep, 'Europe/Oslo')) BETWEEN '06:00:00' AND '09:30:00'
  GROUP BY l.call_date, l.jkey
),
spine AS (  -- every weekday in the window, so zero-train (full closure) mornings appear
  SELECT d AS date_oslo
  FROM UNNEST(GENERATE_DATE_ARRAY('2024-06-01', '2026-05-31')) d
  WHERE MOD(EXTRACT(DAYOFWEEK FROM d) + 5, 7) < 5
),
per_morning AS (
  SELECT
    s.date_oslo,
    COUNT(t.jkey) AS n_trains,
    COUNTIF(t.leg_cancelled) AS n_cancelled,
    -- NULL arr delay (missing actuals, ~rare) counts as NOT late; see header.
    COUNTIF(NOT t.leg_cancelled AND t.oslo_arr_delay_min > 5) AS n_late_arr,
    COUNTIF(NOT t.leg_cancelled AND t.oslo_arr_delay_min > 10) AS n_late_arr10,
    COUNTIF(NOT t.leg_cancelled AND t.oslo_arr_delay_min IS NOT NULL) AS n_arr_known,
    APPROX_QUANTILES(IF(t.leg_cancelled, NULL, t.oslo_arr_delay_min), 100)[OFFSET(90)] AS p90_arr_delay
  FROM spine s
  LEFT JOIN trains t ON t.call_date = s.date_oslo
  GROUP BY s.date_oslo
)
SELECT
  p.*,
  p.date_oslo >= '2024-12-15' AS in_rail_coverage,  -- see header: ET gap before this
  h.d IS NOT NULL AS is_holiday,
  h.kind AS holiday_kind,
  -- Planned-closure detector: mass cancellation (>50%) or no corridor trains at all.
  (p.n_trains = 0 OR p.n_cancelled > 0.5 * p.n_trains) AS is_closure,
  -- Classification variants (zero-train mornings are degraded by definition):
  (p.n_trains = 0 OR p.n_cancelled > 0 OR p.n_late_arr >= 2) AS degraded_strict,
  (p.n_trains = 0 OR p.n_cancelled > 0 OR p.n_late_arr10 >= 1) AS degraded_loose
FROM per_morning p
LEFT JOIN holidays h ON h.d = p.date_oslo;
