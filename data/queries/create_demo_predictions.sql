-- Demo dataset for the /demo journey-prediction page (demo/reisedemo + /demo-api endpoints).
--
-- Turns the lead-time holdout scores (disruptions.leadtime_scored, built by
-- create_leadtime_calibration.sql from leadtime_eval.sql) into a static, per-call table of
-- CALIBRATED predictions at three ask times, side by side with the realized outcome:
--
--   onset_*  lead_min = -1  "at SX message arrival" -- no observable train/line state
--                            (sentinels), calibrated with the *_platt_plan layers.
--   l60_*    lead_min = 60  asked 60 min before the call, *_platt_lead60 layers.
--                            NULL unless the SX existed by then (min_from_onset >= 60).
--   l30_*    lead_min = 30  asked 30 min before the call = the training anchoring,
--                            standard *_platt layers. NULL unless min_from_onset >= 30.
--
-- The platt recalibration, alert composition p_cancel + (1-p_cancel) * p_ge10_h, and the
-- survival-curve p50/p90 intervals mirror disruptions/api/predict_calls.sql VERBATIM (same
-- constants), just with the ask-horizon bucket coming from lead_min instead of
-- minutes-until-call. Reads ML.WEIGHTS live, so re-run after any platt refit.
--
-- Window is the temporal holdout (onset >= 2026-04-01): every number the demo shows is
-- out-of-sample. NSB rail only (the leadtime tables are NSB).
--
-- Also builds:
--   demo_calls      -- ALL rail calls in the window (affected or not), partition-pruned +
--                      clustered for the /demo-api/journeys from/to lookups.
--   demo_stops      -- stop picker: stops in the window with enough traffic to be useful.
--   demo_highlights -- one row per high-impact situation with a suggested from/to/window,
--                      so the demo has one-click "interesting scenarios".
--
-- Run each statement as its own bq job (file + --nosynchronous_mode + bq wait); the whole
-- script also works as one multi-statement job. Cost: reads leadtime_scored (~1.3M rows),
-- a 3-month slice of rail_calls, and ML.WEIGHTS -- well under a dollar.

CREATE TEMP FUNCTION platt(p FLOAT64, a FLOAT64, b FLOAT64) AS (
  1 / (1 + EXP(-(b + a * LN(GREATEST(LEAST(p, 1 - 1e-6), 1e-6)
                            / (1 - GREATEST(LEAST(p, 1 - 1e-6), 1e-6))))))
);

-- Delay quantile (minutes) read off the monotone survival curve s5..s40 = P(delay >= 5..40):
-- q = 0.5 gives the median, q = 0.1 the p90 (10 % exceedance). Same piecewise-linear
-- interpolation + sqrt tail cap as predict_calls.sql.
CREATE TEMP FUNCTION stack_q(s5 FLOAT64, s10 FLOAT64, s20 FLOAT64, s40 FLOAT64, q FLOAT64) AS (
  CAST(ROUND(CASE
    WHEN s5  <= q THEN (1 - q) / GREATEST(1 - s5, 0.001) * 5
    WHEN s10 <= q THEN 5  + (s5  - q) / GREATEST(s5  - s10, 0.001) * 5
    WHEN s20 <= q THEN 10 + (s10 - q) / GREATEST(s10 - s20, 0.001) * 10
    WHEN s40 <= q THEN 20 + (s20 - q) / GREATEST(s20 - s40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s40 / q), 90) END) AS INT64)
);

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.demo_predictions`
PARTITION BY call_date
CLUSTER BY stop_place AS
WITH
sc AS (
  SELECT *,
         CASE lead_min WHEN 30 THEN '30' WHEN 60 THEN '60' ELSE 'plan' END AS lead_bucket
  FROM `ent-kixavvik-dev.disruptions.leadtime_scored`
  WHERE lead_min IN (-1, 30, 60)
),
-- Per-ask-horizon platt weights, one row per bucket ('30' = the standard training-anchoring
-- layers; '60'/'plan' from create_leadtime_calibration.sql) -- same lookups as predict_calls.sql.
wch AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt_plan`)
),
w5h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge5_platt_plan`)
),
w10h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_plan`)
),
w20h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge20_platt_plan`)
),
w40h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge40_platt_plan`)
),
wd5h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge5_platt_plan`)
),
wd10h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_plan`)
),
wd20h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge20_platt_plan`)
),
wd40h AS (
  SELECT '30' AS lead_bucket,
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt`)
  UNION ALL
  SELECT '60',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt_lead60`)
  UNION ALL
  SELECT 'plan',
         MAX(IF(processed_input='__INTERCEPT__', weight, NULL)),
         MAX(IF(processed_input!='__INTERCEPT__', weight, NULL))
  FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge40_platt_plan`)
),
-- Calibrate every head at its ask-horizon; enforce survival-curve monotonicity by cascading
-- (GREATEST of the higher rungs), exactly as predict_calls.sql.
scored AS (
  SELECT
    sc.situationNumber, sc.jkey, sc.stop_order, sc.lead_min, sc.call_date,
    sc.min_from_onset, sc.is_origin, sc.is_terminal,
    platt(sc.p_cancel_raw, wch.a, wch.b) AS p_cancel,
    platt(sc.p_dep10_raw, wd10h.a, wd10h.b) AS p_dep_ge10,
    platt(sc.p_arr10_raw, w10h.a, w10h.b)   AS p_arr_ge10,
    GREATEST(platt(sc.p_arr5_raw,  w5h.a,  w5h.b),
             platt(sc.p_arr10_raw, w10h.a, w10h.b),
             platt(sc.p_arr20_raw, w20h.a, w20h.b),
             platt(sc.p_arr40_raw, w40h.a, w40h.b)) AS s5,
    GREATEST(platt(sc.p_arr10_raw, w10h.a, w10h.b),
             platt(sc.p_arr20_raw, w20h.a, w20h.b),
             platt(sc.p_arr40_raw, w40h.a, w40h.b)) AS s10,
    GREATEST(platt(sc.p_arr20_raw, w20h.a, w20h.b),
             platt(sc.p_arr40_raw, w40h.a, w40h.b)) AS s20,
    platt(sc.p_arr40_raw, w40h.a, w40h.b) AS s40,
    GREATEST(platt(sc.p_dep5_raw,  wd5h.a,  wd5h.b),
             platt(sc.p_dep10_raw, wd10h.a, wd10h.b),
             platt(sc.p_dep20_raw, wd20h.a, wd20h.b),
             platt(sc.p_dep40_raw, wd40h.a, wd40h.b)) AS d5,
    GREATEST(platt(sc.p_dep10_raw, wd10h.a, wd10h.b),
             platt(sc.p_dep20_raw, wd20h.a, wd20h.b),
             platt(sc.p_dep40_raw, wd40h.a, wd40h.b)) AS d10,
    GREATEST(platt(sc.p_dep20_raw, wd20h.a, wd20h.b),
             platt(sc.p_dep40_raw, wd40h.a, wd40h.b)) AS d20,
    platt(sc.p_dep40_raw, wd40h.a, wd40h.b) AS d40
  FROM sc
  JOIN wch   USING (lead_bucket)
  JOIN w5h   USING (lead_bucket)
  JOIN w10h  USING (lead_bucket)
  JOIN w20h  USING (lead_bucket)
  JOIN w40h  USING (lead_bucket)
  JOIN wd5h  USING (lead_bucket)
  JOIN wd10h USING (lead_bucket)
  JOIN wd20h USING (lead_bucket)
  JOIN wd40h USING (lead_bucket)
),
-- One row per (call, lead) with the demo's display columns; sides NULLed at origin/terminal.
per_lead AS (
  SELECT
    situationNumber, jkey, stop_order, lead_min, call_date, min_from_onset,
    ROUND(p_cancel, 4) AS p_cancel,
    IF(is_terminal, NULL, ROUND(p_dep_ge10, 4)) AS p_dep_ge10,
    IF(is_origin,   NULL, ROUND(p_arr_ge10, 4)) AS p_arr_ge10,
    IF(is_terminal, NULL, ROUND(p_cancel + (1 - p_cancel) * p_dep_ge10, 4)) AS p_impact_dep,
    IF(is_origin,   NULL, ROUND(p_cancel + (1 - p_cancel) * p_arr_ge10, 4)) AS p_impact_arr,
    IF(is_terminal, NULL, stack_q(d5, d10, d20, d40, 0.5)) AS dep_p50,
    IF(is_terminal, NULL, stack_q(d5, d10, d20, d40, 0.1)) AS dep_p90,
    IF(is_origin,   NULL, stack_q(s5, s10, s20, s40, 0.5)) AS arr_p50,
    IF(is_origin,   NULL, stack_q(s5, s10, s20, s40, 0.1)) AS arr_p90
  FROM scored
),
-- Pivot the three ask times wide. A lead-L prediction only exists if the SX message had
-- already arrived L minutes before the call (min_from_onset >= L); the at-onset prediction
-- always exists (the window is min_from_onset in [0, 720] by construction).
pivoted AS (
  SELECT
    situationNumber, jkey, stop_order,
    ANY_VALUE(call_date)       AS call_date,
    ANY_VALUE(min_from_onset)  AS min_from_onset,
    MAX(IF(lead_min = -1, p_cancel, NULL))      AS onset_p_cancel,
    MAX(IF(lead_min = -1, p_dep_ge10, NULL))    AS onset_p_dep_ge10,
    MAX(IF(lead_min = -1, p_arr_ge10, NULL))    AS onset_p_arr_ge10,
    MAX(IF(lead_min = -1, p_impact_dep, NULL))  AS onset_p_impact_dep,
    MAX(IF(lead_min = -1, p_impact_arr, NULL))  AS onset_p_impact_arr,
    MAX(IF(lead_min = -1, dep_p50, NULL))       AS onset_dep_p50,
    MAX(IF(lead_min = -1, dep_p90, NULL))       AS onset_dep_p90,
    MAX(IF(lead_min = -1, arr_p50, NULL))       AS onset_arr_p50,
    MAX(IF(lead_min = -1, arr_p90, NULL))       AS onset_arr_p90,
    MAX(IF(lead_min = 60 AND min_from_onset >= 60, p_cancel, NULL))     AS l60_p_cancel,
    MAX(IF(lead_min = 60 AND min_from_onset >= 60, p_dep_ge10, NULL))   AS l60_p_dep_ge10,
    MAX(IF(lead_min = 60 AND min_from_onset >= 60, p_arr_ge10, NULL))   AS l60_p_arr_ge10,
    MAX(IF(lead_min = 60 AND min_from_onset >= 60, p_impact_dep, NULL)) AS l60_p_impact_dep,
    MAX(IF(lead_min = 60 AND min_from_onset >= 60, p_impact_arr, NULL)) AS l60_p_impact_arr,
    MAX(IF(lead_min = 60 AND min_from_onset >= 60, dep_p50, NULL))      AS l60_dep_p50,
    MAX(IF(lead_min = 60 AND min_from_onset >= 60, dep_p90, NULL))      AS l60_dep_p90,
    MAX(IF(lead_min = 60 AND min_from_onset >= 60, arr_p50, NULL))      AS l60_arr_p50,
    MAX(IF(lead_min = 60 AND min_from_onset >= 60, arr_p90, NULL))      AS l60_arr_p90,
    MAX(IF(lead_min = 30 AND min_from_onset >= 30, p_cancel, NULL))     AS l30_p_cancel,
    MAX(IF(lead_min = 30 AND min_from_onset >= 30, p_dep_ge10, NULL))   AS l30_p_dep_ge10,
    MAX(IF(lead_min = 30 AND min_from_onset >= 30, p_arr_ge10, NULL))   AS l30_p_arr_ge10,
    MAX(IF(lead_min = 30 AND min_from_onset >= 30, p_impact_dep, NULL)) AS l30_p_impact_dep,
    MAX(IF(lead_min = 30 AND min_from_onset >= 30, p_impact_arr, NULL)) AS l30_p_impact_arr,
    MAX(IF(lead_min = 30 AND min_from_onset >= 30, dep_p50, NULL))      AS l30_dep_p50,
    MAX(IF(lead_min = 30 AND min_from_onset >= 30, dep_p90, NULL))      AS l30_dep_p90,
    MAX(IF(lead_min = 30 AND min_from_onset >= 30, arr_p50, NULL))      AS l30_arr_p50,
    MAX(IF(lead_min = 30 AND min_from_onset >= 30, arr_p90, NULL))      AS l30_arr_p90
  FROM per_lead
  GROUP BY situationNumber, jkey, stop_order
),
-- Stop names from the national stop registry (rail_calls.stop_name is NULL in this window).
nsr AS (
  SELECT id AS stop_place, ANY_VALUE(name) AS stop_name
  FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version`
  GROUP BY id
),
-- Situation metadata: onset + first-version text (what a passenger would have seen).
sit AS (
  SELECT situationNumber,
         MIN(SAFE.TIMESTAMP(creationTime)) AS onset_ts,
         ANY_VALUE(COALESCE(onset_summary, summary)) AS summary
  FROM `ent-kixavvik-dev.disruptions.disruptions`
  WHERE participantRef = 'NSB'
  GROUP BY situationNumber
),
cat AS (
  SELECT situationNumber, ANY_VALUE(category) AS category
  FROM `ent-kixavvik-dev.disruptions.leadtime_eval_calls`
  GROUP BY situationNumber
)
SELECT
  p.situationNumber, p.jkey, p.stop_order, p.call_date, p.min_from_onset,
  r.lineRef, r.stop_place, COALESCE(r.stop_name, n.stop_name) AS stop_name,
  r.aimed_dep, r.aimed_arr,
  s.onset_ts, s.summary, c.category,
  -- realized outcome at this call
  COALESCE(r.call_cancelled, FALSE) AS act_cancelled,
  r.dep_delay_min AS act_dep_delay_min,
  r.arr_delay_min AS act_arr_delay_min,
  p.* EXCEPT (situationNumber, jkey, stop_order, call_date, min_from_onset)
FROM pivoted AS p
JOIN `ent-kixavvik-dev.disruptions.rail_calls` AS r
  ON r.jkey = p.jkey AND r.stop_order = p.stop_order
 AND r.call_date BETWEEN DATE '2026-03-31' AND DATE '2026-06-21'
LEFT JOIN nsr AS n ON n.stop_place = r.stop_place
LEFT JOIN sit AS s USING (situationNumber)
LEFT JOIN cat AS c USING (situationNumber);

-- ALL rail calls in the demo window (affected by a situation or not), for the journey
-- search: the page shows every journey between the chosen stops, and journeys without an
-- SX flag simply have no prediction columns. Clustered by stop_place for the from/to lookups.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.demo_calls`
PARTITION BY call_date
CLUSTER BY stop_place AS
SELECT r.jkey, r.lineRef, r.stop_place,
       COALESCE(r.stop_name, n.name) AS stop_name,
       r.stop_order, r.call_date,
       r.aimed_dep, r.aimed_arr,
       COALESCE(r.call_cancelled, FALSE) AS call_cancelled,
       r.dep_delay_min, r.arr_delay_min
FROM `ent-kixavvik-dev.disruptions.rail_calls` AS r
LEFT JOIN (
  SELECT id, ANY_VALUE(name) AS name
  FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version`
  GROUP BY id
) AS n ON n.id = r.stop_place
WHERE r.call_date BETWEEN DATE '2026-04-01' AND DATE '2026-06-20';

-- Stop picker for the demo UI: stops with enough departures to be worth offering.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.demo_stops` AS
SELECT stop_place,
       ANY_VALUE(stop_name) AS stop_name,
       COUNT(*) AS n_calls
FROM `ent-kixavvik-dev.disruptions.demo_calls`
GROUP BY stop_place
HAVING COUNT(*) >= 200;

-- One-click demo scenarios: the highest-realized-impact situations, each with a suggested
-- from/to (first and last affected stop of the situation's worst-hit journey) and a time
-- window bracketing its affected departures.
CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.demo_highlights` AS
WITH impact AS (
  SELECT situationNumber, jkey,
         ANY_VALUE(summary)  AS summary,
         ANY_VALUE(category) AS category,
         ANY_VALUE(onset_ts) AS onset_ts,
         COUNTIF(act_cancelled) AS n_cancelled,
         COUNTIF(NOT act_cancelled AND act_dep_delay_min >= 10) AS n_delayed10,
         COUNT(*) AS n_calls,
         MIN(aimed_dep) AS first_dep,
         MAX(aimed_dep) AS last_dep
  FROM `ent-kixavvik-dev.disruptions.demo_predictions`
  GROUP BY situationNumber, jkey
),
per_sit AS (
  SELECT situationNumber,
         ANY_VALUE(summary)  AS summary,
         ANY_VALUE(category) AS category,
         ANY_VALUE(onset_ts) AS onset_ts,
         SUM(n_cancelled)  AS n_cancelled,
         SUM(n_delayed10)  AS n_delayed10,
         SUM(n_calls)      AS n_calls,
         MIN(first_dep)    AS window_start,
         MAX(last_dep)     AS window_end,
         -- worst-hit journey = exemplar for the from/to suggestion
         ARRAY_AGG(jkey ORDER BY n_cancelled + n_delayed10 DESC, n_calls DESC LIMIT 1)[OFFSET(0)] AS top_jkey
  FROM impact
  GROUP BY situationNumber
),
exemplar AS (
  SELECT s.situationNumber,
         ARRAY_AGG(d.stop_place ORDER BY d.stop_order ASC  LIMIT 1)[OFFSET(0)] AS from_stop,
         ARRAY_AGG(d.stop_name  ORDER BY d.stop_order ASC  LIMIT 1)[OFFSET(0)] AS from_name,
         ARRAY_AGG(d.stop_place ORDER BY d.stop_order DESC LIMIT 1)[OFFSET(0)] AS to_stop,
         ARRAY_AGG(d.stop_name  ORDER BY d.stop_order DESC LIMIT 1)[OFFSET(0)] AS to_name
  FROM per_sit AS s
  JOIN `ent-kixavvik-dev.disruptions.demo_predictions` AS d
    ON d.situationNumber = s.situationNumber AND d.jkey = s.top_jkey
  GROUP BY s.situationNumber
)
SELECT p.*, e.from_stop, e.from_name, e.to_stop, e.to_name
FROM per_sit AS p
JOIN exemplar AS e USING (situationNumber)
WHERE p.n_cancelled + p.n_delayed10 >= 5
ORDER BY p.n_cancelled + p.n_delayed10 DESC
LIMIT 40;
