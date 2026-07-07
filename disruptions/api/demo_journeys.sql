-- Journey search for the /demo-api/journeys endpoint.
--
-- Parameters: @from_stop, @to_stop (NSR StopPlace refs), @start_ts, @end_ts (UTC timestamps).
-- Returns every journey in the demo window that departs @from_stop within [@start_ts, @end_ts]
-- and later calls at @to_stop, with:
--   * the realized outcome at both ends (cancelled / delay minutes), and
--   * when an SX situation flagged the journey, the calibrated predictions made at three ask
--     times (at message arrival / 60 min before / 30 min before the call) for both ends,
--     from disruptions.demo_predictions (see data/queries/create_demo_predictions.sql).
--
-- Multi-operator keying: a journey is (operator, jkey, instance). Rail/GOA jkeys are
-- date-scoped (DatedServiceJourney -- one instance ever), but bus ServiceJourney jkeys repeat
-- every operating day, so boarding and alighting calls are paired on the operating_date
-- instance pin (sentinel for rail, where operating_date is NULL), and prediction rows -- which
-- carry the call's own call_date -- are joined per (operator, jkey, call_date, stop_order).
--
-- When several situations touch a journey the most severe one (highest at-onset impact
-- probability across either end) is chosen, and BOTH ends read from that same situation so
-- the card tells one coherent story -- same convention as /journey.
WITH
f AS (
  SELECT operator, jkey,
         COALESCE(operating_date, DATE '1000-01-01') AS inst,
         lineRef, stop_place, stop_name, stop_order, call_date, aimed_dep,
         call_cancelled, dep_delay_min
  FROM `ent-kixavvik-dev.disruptions.demo_calls`
  WHERE stop_place = @from_stop
    AND call_date BETWEEN DATE(@start_ts) AND DATE(@end_ts)
    AND aimed_dep BETWEEN @start_ts AND @end_ts
),
t AS (
  SELECT operator, jkey,
         COALESCE(operating_date, DATE '1000-01-01') AS inst,
         stop_place, stop_name, stop_order, call_date, aimed_arr,
         call_cancelled, arr_delay_min
  FROM `ent-kixavvik-dev.disruptions.demo_calls`
  WHERE stop_place = @to_stop
    AND call_date BETWEEN DATE(@start_ts) AND DATE_ADD(DATE(@end_ts), INTERVAL 1 DAY)
    AND aimed_arr IS NOT NULL
),
-- One row per journey instance: first boarding at from_stop, first alighting after it.
j AS (
  SELECT
    f.operator, f.jkey, f.lineRef,
    f.stop_place AS from_stop,  f.stop_name AS from_name,
    t.stop_place AS to_stop,    t.stop_name AS to_name,
    f.stop_order AS from_order, t.stop_order AS to_order,
    f.call_date  AS from_call_date, t.call_date AS to_call_date,
    f.aimed_dep  AS from_aimed_dep, t.aimed_arr AS to_aimed_arr,
    f.call_cancelled AS act_dep_cancelled, f.dep_delay_min AS act_dep_delay_min,
    t.call_cancelled AS act_arr_cancelled, t.arr_delay_min AS act_arr_delay_min
  FROM f
  JOIN t USING (operator, jkey, inst)
  WHERE t.stop_order > f.stop_order
  QUALIFY ROW_NUMBER() OVER (PARTITION BY f.operator, f.jkey, f.inst
                             ORDER BY f.stop_order, t.stop_order) = 1
),
pf AS (
  SELECT * FROM `ent-kixavvik-dev.disruptions.demo_predictions`
  WHERE stop_place = @from_stop
    AND call_date BETWEEN DATE(@start_ts) AND DATE(@end_ts)
),
pt AS (
  SELECT * FROM `ent-kixavvik-dev.disruptions.demo_predictions`
  WHERE stop_place = @to_stop
    AND call_date BETWEEN DATE(@start_ts) AND DATE_ADD(DATE(@end_ts), INTERVAL 1 DAY)
),
-- Most severe situation per journey instance, judged at message arrival across either end.
cand AS (
  SELECT operator, jkey, from_call_date, situationNumber,
         MAX(sev) AS sev
  FROM (
    SELECT j.operator, j.jkey, j.from_call_date, p.situationNumber,
           COALESCE(p.onset_p_impact_dep, p.onset_p_impact_arr, p.onset_p_cancel) AS sev
    FROM pf AS p
    JOIN j ON j.operator = p.operator AND j.jkey = p.jkey
          AND p.stop_order = j.from_order AND p.call_date = j.from_call_date
    UNION ALL
    SELECT j.operator, j.jkey, j.from_call_date, p.situationNumber,
           COALESCE(p.onset_p_impact_arr, p.onset_p_impact_dep, p.onset_p_cancel) AS sev
    FROM pt AS p
    JOIN j ON j.operator = p.operator AND j.jkey = p.jkey
          AND p.stop_order = j.to_order AND p.call_date = j.to_call_date
  )
  GROUP BY operator, jkey, from_call_date, situationNumber
),
best AS (
  SELECT operator, jkey, from_call_date,
         ARRAY_AGG(situationNumber ORDER BY sev DESC LIMIT 1)[OFFSET(0)] AS situationNumber
  FROM cand
  GROUP BY operator, jkey, from_call_date
)
SELECT
  j.*,
  b.situationNumber,
  COALESCE(pf.summary, pt.summary)   AS summary,
  COALESCE(pf.category, pt.category) AS category,
  COALESCE(pf.onset_ts, pt.onset_ts) AS onset_ts,
  -- departure side: predictions for the boarding call
  pf.min_from_onset      AS dep_min_from_onset,
  pf.onset_p_cancel      AS dep_onset_p_cancel,
  pf.onset_p_dep_ge10    AS dep_onset_p_ge10,
  pf.onset_p_impact_dep  AS dep_onset_p_impact,
  pf.onset_dep_p50       AS dep_onset_p50,
  pf.onset_dep_p90       AS dep_onset_p90,
  pf.l60_p_cancel        AS dep_l60_p_cancel,
  pf.l60_p_dep_ge10      AS dep_l60_p_ge10,
  pf.l60_p_impact_dep    AS dep_l60_p_impact,
  pf.l60_dep_p50         AS dep_l60_p50,
  pf.l60_dep_p90         AS dep_l60_p90,
  pf.l30_p_cancel        AS dep_l30_p_cancel,
  pf.l30_p_dep_ge10      AS dep_l30_p_ge10,
  pf.l30_p_impact_dep    AS dep_l30_p_impact,
  pf.l30_dep_p50         AS dep_l30_p50,
  pf.l30_dep_p90         AS dep_l30_p90,
  -- arrival side: predictions for the alighting call
  pt.min_from_onset      AS arr_min_from_onset,
  pt.onset_p_cancel      AS arr_onset_p_cancel,
  pt.onset_p_arr_ge10    AS arr_onset_p_ge10,
  pt.onset_p_impact_arr  AS arr_onset_p_impact,
  pt.onset_arr_p50       AS arr_onset_p50,
  pt.onset_arr_p90       AS arr_onset_p90,
  pt.l60_p_cancel        AS arr_l60_p_cancel,
  pt.l60_p_arr_ge10      AS arr_l60_p_ge10,
  pt.l60_p_impact_arr    AS arr_l60_p_impact,
  pt.l60_arr_p50         AS arr_l60_p50,
  pt.l60_arr_p90         AS arr_l60_p90,
  pt.l30_p_cancel        AS arr_l30_p_cancel,
  pt.l30_p_arr_ge10      AS arr_l30_p_ge10,
  pt.l30_p_impact_arr    AS arr_l30_p_impact,
  pt.l30_arr_p50         AS arr_l30_p50,
  pt.l30_arr_p90         AS arr_l30_p90
FROM j
LEFT JOIN best AS b USING (operator, jkey, from_call_date)
LEFT JOIN pf ON pf.operator = j.operator AND pf.jkey = j.jkey
            AND pf.stop_order = j.from_order AND pf.call_date = j.from_call_date
            AND pf.situationNumber = b.situationNumber
LEFT JOIN pt ON pt.operator = j.operator AND pt.jkey = j.jkey
            AND pt.stop_order = j.to_order AND pt.call_date = j.to_call_date
            AND pt.situationNumber = b.situationNumber
ORDER BY j.from_aimed_dep
LIMIT 300
