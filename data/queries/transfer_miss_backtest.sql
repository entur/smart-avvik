-- Historical Vy-to-Vy changes, for the backtest and the fit of multi-leg KTD7's P(miss)
-- (`trips.p_miss`). transfer_miss_backtest.py reads this table; its header carries the method and
-- the measured numbers.
--
-- WHAT A CHANGE IS. Journey Planner keeps no history, so the changes are rebuilt from rail_calls:
-- for each arriving call and each direction out of its station, the first departure aimed 2-20 min
-- after the aimed arrival -- the connection JP would build with no walk (its minimum change is
-- 120 s). Not one back the way the arriving train came, not one to the stop the arriving train
-- calls at next (nobody changes to go where they already ride), and not the same set continuing
-- under a new journey ref (it terminates and the other starts at the same quay). Both calls are
-- recorded and neither is cancelled: a cancelled call is `leg_cancelled`'s reason, not
-- `missed_connection`'s. Vy only: rail_calls has no SJ, Go-Ahead or bus.
--
-- NEIGHBOURS FROM THE CALL ORDER. rail_calls' own prev/next_stop_place are NULL on every row
-- appended since the 2026-06-20 rebuild (update_rail_calls.sql never fills them), so a filter on
-- them silently keeps April-June only. LAG/LEAD over the journey, before cancelled calls are
-- dropped, is the physical route.
--
-- THE WINDOW is the lead-time holdout's (leadtime_scored, exp_etcmp6_fair_scored): literal, so
-- BigQuery prunes partitions. Built 2026-10-28: 918 133 changes at 220 stations, 34 870 missed
-- with no walk (3.8 %), billed 0.33 GB.
--
--   bq query --use_legacy_sql=false < data/queries/transfer_miss_backtest.sql

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.exp_transfer_pairs` AS
WITH journey AS (
  SELECT *,
         LAG(stop_place) OVER w AS prev_place,
         LEAD(stop_place) OVER w AS next_place
  FROM `ent-kixavvik-dev.disruptions.rail_calls`
  WHERE call_date BETWEEN '2026-04-01' AND '2026-10-12'
  WINDOW w AS (PARTITION BY jkey ORDER BY stop_order)
),
rc AS (
  SELECT jkey, lineRef, quay, stop_place, prev_place, next_place, stop_order, call_date,
         aimed_arr, actual_arr, aimed_dep, actual_dep
  FROM journey
  WHERE call_source = 'recorded'
    AND NOT COALESCE(call_cancelled, FALSE)
),
cand AS (
  SELECT a.call_date, a.stop_place,
         a.jkey AS a_jkey, a.stop_order AS a_stop_order, a.lineRef AS a_line,
         a.aimed_arr, a.actual_arr,
         d.jkey AS b_jkey, d.stop_order AS b_stop_order, d.lineRef AS b_line,
         d.next_place AS b_next, d.aimed_dep, d.actual_dep
  FROM rc AS a
  JOIN rc AS d
    ON d.stop_place = a.stop_place
   AND d.call_date = a.call_date
  WHERE a.aimed_arr IS NOT NULL AND a.actual_arr IS NOT NULL
    AND d.aimed_dep IS NOT NULL AND d.actual_dep IS NOT NULL
    AND d.jkey != a.jkey
    AND d.aimed_dep BETWEEN TIMESTAMP_ADD(a.aimed_arr, INTERVAL 120 SECOND)
                        AND TIMESTAMP_ADD(a.aimed_arr, INTERVAL 20 MINUTE)
    AND d.next_place IS DISTINCT FROM a.prev_place  -- not back the way it came
    AND d.next_place IS DISTINCT FROM a.next_place  -- not where the arriving train goes
    AND NOT (a.next_place IS NULL AND d.prev_place IS NULL AND d.quay = a.quay)  -- the same set
)
SELECT * EXCEPT (rn)
FROM (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY call_date, a_jkey, a_stop_order, b_next ORDER BY aimed_dep, b_jkey) AS rn
  FROM cand
)
WHERE rn = 1;
