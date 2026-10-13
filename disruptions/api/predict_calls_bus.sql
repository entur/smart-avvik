-- LIVE per-call predictions for the BUS/GOA (pooled_all) family -> disruptions.call_predictions_bus_today.
-- The bus/GOA sibling of predict_calls.sql (NSB/prod). Same output contract PLUS an `operator` column,
-- so main.py can merge both tables into one per-call cache keyed (operator, jkey).
--
-- FOUR deltas vs predict_calls.sql (all forced by the two-family + multi-operator design):
--   1. Situations from disruptions_bus_initial_today (bus/GOA publishers); planned calls from
--      bus_calls_planned_today. operator = the affected-journey ref codespace (GCO fans across
--      county codespaces -> operator is the NETWORK the journey runs in, not the publisher), and the
--      call join is on (operator, jkey) because bus ServiceJourney jkeys collide across operators.
--   2. Models are the _pooled_all heads; `operator` is a model feature, carried into ML.PREDICT.
--   3. Calibration is operator-aware AND ask-horizon-aware: p_cal = sigmoid(b + a*raw_logit +
--      w[operator]), with (b, a, w) looked up from disruptions.pooled_all_platt_weights by
--      (head, lead_bucket, operator) -- one JOIN per head, replacing predict_calls.sql's inline
--      ML.WEIGHTS blocks. lead_bucket is minutes-until-call at scoring time (same boundaries as rail).
--   4. p90 delay intervals read the operator-aware disruptions.delay_p90_calib_pooled_all
--      ((side, operator, plog_bucket)) with a global operator='*' fallback for sparse cells.
--
-- PLUS, since 2026-08-19, one row per call ET has ALREADY DECLARED CANCELLED that no situation
-- named -- consulting no model, exactly as in predict_calls.sql. See the `et_cancelled` /
-- `covered` / `et_only` CTEs and the UNION ALL that closes the file.
--
-- net_pre_* is scoped to the OPERATOR's own network (matching create_training_calls_pooled_all.sql);
-- line_pre_*/line_now_* key on lineRef (already operator-namespaced). line_now_* is a NOW-CAST of the
-- last observed 60 min (same live-information constraint as predict_calls.sql, not the training
-- per-call hour bucket). onset/time features come from SAFE.TIMESTAMP(creationTime) in UTC for
-- verbatim train/serve parity. Keep the feature block in sync with predict_calls.sql + the training
-- build (create_training_calls_pooled_all.sql).
CREATE OR REPLACE TABLE `disruptions.call_predictions_bus_today` AS
WITH
cat AS (
  SELECT summary, description, ANY_VALUE(category) AS category
  FROM `disruptions.categories`
  GROUP BY summary, description
),
sit AS (
  SELECT
    d.situationNumber,
    SAFE.TIMESTAMP(d.creationTime) AS onset_ts,
    d.summary,
    -- Served columns read the LATEST version, features the first -- see predict_calls.sql.
    COALESCE(d.latest_summary, d.summary) AS served_summary,
    -- Carried for serving only (never a feature) -- see the same column in predict_calls.sql.
    COALESCE(d.latest_description, d.description) AS description,
    -- The operator's English, served only -- see the same columns in predict_calls.sql.
    COALESCE(d.latest_summary_en, d.summary_en) AS summary_en,
    COALESCE(d.latest_description_en, d.description_en) AS description_en,
    -- SIRI-SX `reportType` as the operator published it NOW (INCIDENT / GENERAL), served only.
    COALESCE(d.latest_report_type, d.reportType) AS report_type,
    CASE
      WHEN REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(d.summary,''),' ',IFNULL(d.description,''))),
                           r'innstil|kansel')                        THEN 'innstilt'
      WHEN REGEXP_CONTAINS(LOWER(CONCAT(IFNULL(d.summary,''),' ',IFNULL(d.description,''))),
                           r'forsink|toget står')                    THEN 'forsinket'
      WHEN REGEXP_CONTAINS(LOWER(IFNULL(d.summary,'')),
                           r'færre vogner|forreste vogn|fremste vogn|bruk de') THEN 'kapasitet'
      ELSE 'other'
    END AS status,
    d.planned,
    ARRAY_LENGTH(d.affected_journeys) AS num_journeys,
    ARRAY_LENGTH(d.affected_stops)    AS num_stops,
    ARRAY_LENGTH(d.affected_lines)    AS num_lines,
    d.affected_journeys,
    IFNULL(c.category, 'UNKNOWN')     AS category,
    IFNULL(SAFE_CAST(REGEXP_EXTRACT(
      LOWER(CONCAT(IFNULL(d.summary,''), ' ', IFNULL(d.description,''))),
      r'([0-9]+)\s*min') AS INT64), -1) AS stated_min
  FROM `disruptions.disruptions_bus_initial_today` AS d
  LEFT JOIN cat AS c
    ON IFNULL(d.summary,'')     = IFNULL(c.summary,'')
   AND IFNULL(d.description,'') = IFNULL(c.description,'')
  WHERE SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
),
aff AS (
  -- operator = the ref codespace (the network the journey runs in); jkey = ref minus prefix.
  -- Restrict to the trained + live-servable operator set (matches bus_calls_planned_today).
  SELECT
    s.situationNumber, s.onset_ts, s.summary, s.status, s.planned,
    s.num_journeys, s.num_stops, s.num_lines, s.category, s.stated_min,
    REGEXP_EXTRACT(j, r'^([A-Za-z]+):') AS operator,
    REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') AS jkey
  FROM sit AS s, UNNEST(s.affected_journeys) AS j
  WHERE REGEXP_EXTRACT(j, r'^([A-Za-z]+):')
        IN ('GOA','RUT','KOL','SOF','NOR','SKY','ATB','OST','FIN','MOR','TRO','GCO')
),
base AS (
  SELECT
    a.operator, a.situationNumber, a.onset_ts, a.summary,
    a.jkey, r.lineRef, r.stop_place, r.stop_name, r.aimed_dep, r.aimed_arr,
    r.call_cancelled AS et_declared_cancelled,
    -- OBSERVED state, not predicted -- see the same two columns in predict_calls.sql.
    r.dep_delay_min AS et_dep_delay_min,
    r.call_source,
    -- Whether a passenger may board / alight HERE -- see predict_calls.sql for why these ride
    -- into the ML.PREDICT input rather than being joined back, and bus_calls_planned_today.sql
    -- for the reading itself.
    r.boarding_allowed, r.alighting_allowed,
    COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts,
    IFNULL(a.status, 'other') AS status,
    IFNULL(a.planned, FALSE)  AS planned,
    a.num_journeys, a.num_stops, a.num_lines,
    EXTRACT(HOUR      FROM a.onset_ts) AS onset_hour,
    EXTRACT(DAYOFWEEK FROM a.onset_ts) AS onset_dow,
    EXTRACT(MONTH     FROM a.onset_ts) AS onset_month,
    r.stop_order,
    (r.aimed_dep IS NULL) AS is_terminal,
    (r.aimed_arr IS NULL) AS is_origin,
    TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) AS min_from_onset,
    EXTRACT(HOUR      FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_hour,
    EXTRACT(DAYOFWEEK FROM COALESCE(r.aimed_dep, r.aimed_arr)) AS call_dow,
    a.category, a.stated_min
  FROM aff AS a
  JOIN `disruptions.bus_calls_planned_today` AS r
    ON r.operator = a.operator AND r.jkey = a.jkey
  WHERE (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
    AND TIMESTAMP_DIFF(COALESCE(r.aimed_dep, r.aimed_arr), a.onset_ts, MINUTE) BETWEEN 0 AND 720
  -- first instance after onset per (operator, jkey, stop_order): bus ServiceJourney jkeys are not
  -- date-scoped, so a ref could match several stop-order rows; keep the earliest upcoming one.
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY a.situationNumber, a.operator, a.jkey, r.stop_order
    ORDER BY COALESCE(r.aimed_dep, r.aimed_arr) ASC) = 1
),
observed AS (
  SELECT operator, lineRef, jkey, stop_order, aimed_dep, dep_delay_min,
         COALESCE(call_cancelled, FALSE) AS call_cancelled
  FROM `disruptions.bus_calls_planned_today`
  WHERE COALESCE(call_cancelled, FALSE) OR dep_delay_min IS NOT NULL
),
sitline AS (
  SELECT DISTINCT operator, situationNumber, lineRef, onset_ts FROM base
),
linepre AS (
  -- pre-onset line congestion (60 min before onset). lineRef is operator-namespaced.
  SELECT p.situationNumber, p.lineRef,
         COUNT(*) AS line_pre_n,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS line_pre_delay,
         ROUND(AVG(CAST(r.call_cancelled AS INT64)), 3) AS line_pre_cancel
  FROM sitline AS p
  JOIN observed AS r
    ON r.lineRef = p.lineRef
   AND r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1, 2
),
netpre AS (
  -- pre-onset network state, scoped to the OPERATOR's own network (matches training)
  SELECT p.situationNumber,
         ROUND(AVG(GREATEST(r.dep_delay_min, 0)), 2) AS net_pre_delay,
         ROUND(AVG(CAST(r.call_cancelled AS INT64)), 3) AS net_pre_cancel
  FROM (SELECT DISTINCT operator, situationNumber, onset_ts FROM sitline) AS p
  JOIN observed AS r
    ON r.operator = p.operator
   AND r.aimed_dep >= TIMESTAMP_SUB(p.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  p.onset_ts
  GROUP BY 1
),
-- The moment `observed` was read: when bus_calls_planned_today was built, not now. That table is rebuilt on a
-- TTL (main._BUS_PLANNED_TTL, 120 min), so measuring the last hour and the ask horizon from CURRENT_TIMESTAMP() read every
-- stop passed since the build as unobserved. own_prev_delay = -1 at a mid-route stop is what a
-- train whose earlier calls were cancelled looks like in training, so the model scored running
-- trains as likely cancellations (2026-10-13: live p_cancel 0.75 where the same calls scored 0.48
-- through the training pipeline and 40 % were cancelled). Anchored here, line_now_* and
-- lead_bucket describe what was known at the build, which is what leadtime_eval.sql fit the
-- per-horizon Platt layers on. COALESCE covers an empty table.
asof AS (
  SELECT COALESCE(MAX(built_at), CURRENT_TIMESTAMP()) AS t FROM `disruptions.bus_calls_planned_today`
),
linenow AS (
  -- NOW-CAST: per-line observed state in the last 60 min before the table was built (`asof`)
  SELECT lineRef,
         COUNT(*) AS line_now_n,
         ROUND(AVG(GREATEST(dep_delay_min, 0)), 2) AS line_now_delay,
         ROUND(AVG(CAST(call_cancelled AS INT64)), 3) AS line_now_cancel
  FROM observed
  WHERE aimed_dep >= TIMESTAMP_SUB((SELECT t FROM asof), INTERVAL 60 MINUTE)
    AND aimed_dep <  (SELECT t FROM asof)
  GROUP BY 1
),
own AS (
  -- the vehicle's own realized delay at its latest earlier stop aimed >=30 min before this call;
  -- keyed on (operator, jkey) -- bus jkeys collide across operators. planned_today is a single
  -- operating day so no operating_date pin is needed.
  SELECT t.operator, t.situationNumber, t.jkey, t.stop_order,
         ARRAY_AGG(GREATEST(r2.dep_delay_min, 0) ORDER BY r2.stop_order DESC LIMIT 1)[OFFSET(0)]
           AS own_prev_delay
  FROM base AS t
  JOIN observed AS r2
    ON r2.operator = t.operator
   AND r2.jkey = t.jkey
   AND r2.stop_order < t.stop_order
   AND r2.aimed_dep <= TIMESTAMP_SUB(t.call_ts, INTERVAL 30 MINUTE)
   AND r2.dep_delay_min IS NOT NULL
   AND NOT r2.call_cancelled
  GROUP BY 1, 2, 3, 4
),
calls AS (
  SELECT
    b.*,
    IFNULL(lp.line_pre_n, 0)        AS line_pre_n,
    IFNULL(lp.line_pre_delay, -1)   AS line_pre_delay,
    IFNULL(lp.line_pre_cancel, -1)  AS line_pre_cancel,
    IFNULL(np.net_pre_delay, -1)    AS net_pre_delay,
    IFNULL(np.net_pre_cancel, -1)   AS net_pre_cancel,
    IFNULL(ln.line_now_n, 0)        AS line_now_n,
    IFNULL(ln.line_now_delay, -1)   AS line_now_delay,
    IFNULL(ln.line_now_cancel, -1)  AS line_now_cancel,
    IFNULL(o.own_prev_delay, -1)    AS own_prev_delay,
    CASE
      WHEN TIMESTAMP_DIFF(b.call_ts, (SELECT t FROM asof), MINUTE) < 45  THEN '30'
      WHEN TIMESTAMP_DIFF(b.call_ts, (SELECT t FROM asof), MINUTE) < 90  THEN '60'
      WHEN TIMESTAMP_DIFF(b.call_ts, (SELECT t FROM asof), MINUTE) < 180 THEN '120'
      WHEN TIMESTAMP_DIFF(b.call_ts, (SELECT t FROM asof), MINUTE) < 360 THEN '240'
      ELSE 'plan'
    END AS lead_bucket
  FROM base AS b
  LEFT JOIN linepre AS lp ON lp.situationNumber = b.situationNumber AND lp.lineRef = b.lineRef
  LEFT JOIN netpre  AS np ON np.situationNumber = b.situationNumber
  LEFT JOIN linenow AS ln ON ln.lineRef = b.lineRef
  LEFT JOIN own AS o
    ON o.operator = b.operator AND o.situationNumber = b.situationNumber
   AND o.jkey = b.jkey AND o.stop_order = b.stop_order
),
clf AS (
  SELECT operator, situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.call_cancel_clf_pooled_all`, (SELECT * FROM calls))
),
dep AS (
  SELECT operator, situationNumber, jkey, stop_order, predicted_dep_log AS dep_plog
  FROM ML.PREDICT(MODEL `disruptions.dep_delay_reg_pooled_all`, (SELECT * FROM calls))
),
arr AS (
  SELECT operator, situationNumber, jkey, stop_order, predicted_arr_log AS arr_plog
  FROM ML.PREDICT(MODEL `disruptions.arr_delay_reg_pooled_all`, (SELECT * FROM calls))
),
ge5 AS (
  SELECT operator, situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.arr_ge5_clf_pooled_all`, (SELECT * FROM calls))
),
ge10 AS (
  SELECT operator, situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.arr_ge10_clf_pooled_all`, (SELECT * FROM calls))
),
ge20 AS (
  SELECT operator, situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.arr_ge20_clf_pooled_all`, (SELECT * FROM calls))
),
ge40 AS (
  SELECT operator, situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.arr_ge40_clf_pooled_all`, (SELECT * FROM calls))
),
dge5 AS (
  SELECT operator, situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.dep_ge5_clf_pooled_all`, (SELECT * FROM calls))
),
dge10 AS (
  SELECT operator, situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.dep_ge10_clf_pooled_all`, (SELECT * FROM calls))
),
dge20 AS (
  SELECT operator, situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.dep_ge20_clf_pooled_all`, (SELECT * FROM calls))
),
dge40 AS (
  SELECT operator, situationNumber, jkey, stop_order,
    (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
  FROM ML.PREDICT(MODEL `disruptions.dep_ge40_clf_pooled_all`, (SELECT * FROM calls))
),
-- operator-aware, ask-horizon-aware platt coefficients, one slice per head
pwt AS (SELECT * FROM `disruptions.pooled_all_platt_weights`),
scored AS (
  SELECT
    c.*,
    clf.p_raw AS p_cancel_raw, dge10.p_raw AS p_dep10_raw, ge10.p_raw AS p_arr10_raw,
    dep.dep_plog, arr.arr_plog,
    -- calibrated cancel + per-rung exceedance probs (sigmoid(b + a*logit + w[operator]))
    1/(1+EXP(-(pc.b  + pc.a  * LN(GREATEST(LEAST(clf.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(clf.p_raw,1-1e-6),1e-6)))  + IFNULL(pc.w,0))))  AS pc_cal,
    1/(1+EXP(-(pa5.b + pa5.a * LN(GREATEST(LEAST(ge5.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge5.p_raw,1-1e-6),1e-6)))  + IFNULL(pa5.w,0)))) AS ca5,
    1/(1+EXP(-(pa10.b+ pa10.a* LN(GREATEST(LEAST(ge10.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge10.p_raw,1-1e-6),1e-6)))+ IFNULL(pa10.w,0))))AS ca10,
    1/(1+EXP(-(pa20.b+ pa20.a* LN(GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge20.p_raw,1-1e-6),1e-6)))+ IFNULL(pa20.w,0))))AS ca20,
    1/(1+EXP(-(pa40.b+ pa40.a* LN(GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ge40.p_raw,1-1e-6),1e-6)))+ IFNULL(pa40.w,0))))AS ca40,
    1/(1+EXP(-(pd5.b + pd5.a * LN(GREATEST(LEAST(dge5.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge5.p_raw,1-1e-6),1e-6)))+ IFNULL(pd5.w,0)))) AS cd5,
    1/(1+EXP(-(pd10.b+ pd10.a* LN(GREATEST(LEAST(dge10.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge10.p_raw,1-1e-6),1e-6)))+IFNULL(pd10.w,0))))AS cd10,
    1/(1+EXP(-(pd20.b+ pd20.a* LN(GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge20.p_raw,1-1e-6),1e-6)))+IFNULL(pd20.w,0))))AS cd20,
    1/(1+EXP(-(pd40.b+ pd40.a* LN(GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(dge40.p_raw,1-1e-6),1e-6)))+IFNULL(pd40.w,0))))AS cd40
  FROM calls AS c
  JOIN clf  USING (operator, situationNumber, jkey, stop_order)
  JOIN dep  USING (operator, situationNumber, jkey, stop_order)
  JOIN arr  USING (operator, situationNumber, jkey, stop_order)
  JOIN ge5  USING (operator, situationNumber, jkey, stop_order)
  JOIN ge10 USING (operator, situationNumber, jkey, stop_order)
  JOIN ge20 USING (operator, situationNumber, jkey, stop_order)
  JOIN ge40 USING (operator, situationNumber, jkey, stop_order)
  JOIN dge5  USING (operator, situationNumber, jkey, stop_order)
  JOIN dge10 USING (operator, situationNumber, jkey, stop_order)
  JOIN dge20 USING (operator, situationNumber, jkey, stop_order)
  JOIN dge40 USING (operator, situationNumber, jkey, stop_order)
  JOIN pwt AS pc   ON pc.head  ='cancel'  AND pc.lead_bucket  =c.lead_bucket AND pc.operator  =c.operator
  JOIN pwt AS pa5  ON pa5.head ='arr_ge5' AND pa5.lead_bucket =c.lead_bucket AND pa5.operator =c.operator
  JOIN pwt AS pa10 ON pa10.head='arr_ge10'AND pa10.lead_bucket=c.lead_bucket AND pa10.operator=c.operator
  JOIN pwt AS pa20 ON pa20.head='arr_ge20'AND pa20.lead_bucket=c.lead_bucket AND pa20.operator=c.operator
  JOIN pwt AS pa40 ON pa40.head='arr_ge40'AND pa40.lead_bucket=c.lead_bucket AND pa40.operator=c.operator
  JOIN pwt AS pd5  ON pd5.head ='dep_ge5' AND pd5.lead_bucket =c.lead_bucket AND pd5.operator =c.operator
  JOIN pwt AS pd10 ON pd10.head='dep_ge10'AND pd10.lead_bucket=c.lead_bucket AND pd10.operator=c.operator
  JOIN pwt AS pd20 ON pd20.head='dep_ge20'AND pd20.lead_bucket=c.lead_bucket AND pd20.operator=c.operator
  JOIN pwt AS pd40 ON pd40.head='dep_ge40'AND pd40.lead_bucket=c.lead_bucket AND pd40.operator=c.operator
),
-- monotone survival curve per side (max over the >= rungs) + composed alerts
curve AS (
  SELECT
    s.*,
    GREATEST(s.ca5, s.ca10, s.ca20, s.ca40) AS s5,
    GREATEST(s.ca10, s.ca20, s.ca40)        AS s10,
    GREATEST(s.ca20, s.ca40)                AS s20,
    s.ca40                                  AS s40,
    GREATEST(s.cd5, s.cd10, s.cd20, s.cd40) AS d5,
    GREATEST(s.cd10, s.cd20, s.cd40)        AS d10,
    GREATEST(s.cd20, s.cd40)                AS d20,
    s.cd40                                  AS d40
  FROM scored AS s
),
-- per-(side, operator) p90 bucket bounds, plus the global operator='*' bounds for fallback
bnd_op AS (
  SELECT side, operator, MIN(plog_bucket) AS lo, MAX(plog_bucket) AS hi
  FROM `disruptions.delay_p90_calib_pooled_all` GROUP BY side, operator
),
-- ---- ET-DECLARED CANCELLATIONS THAT NO SITUATION NAMED ----------------------------------------
-- The bus/GOA sibling of the block by this name in predict_calls.sql, and the rationale is there in
-- full: a cancellation ET has ALREADY DECLARED is a fact about the timetable, so it needs no
-- situation and no model -- `impact._cancelled_prediction` replaces every score with the certainty
-- the moment `et_declared_cancelled` is set. Three bus-specific deltas:
--   1. The synthetic situationNumber carries operator AND call_date, because bus ServiceJourney
--      jkeys are not date-scoped and collide both across operators and across days.
--   2. The anti-join keys on the CALL's own timestamp as well, rather than on
--      (operator, jkey, stop_order): `base` keeps only the earliest instance per that triple
--      (its QUALIFY), so a jkey serving two days would otherwise have one day's cancellation
--      suppressed by the other day's being scored.
--   3. It is a much bigger population than rail's -- measured in dev on 2026-08-19, 3 602 uncovered
--      declared-cancelled calls across 706 journeys against rail's 20 across 3. Volume is not risk
--      here (a row only becomes a push if a REGISTERED journey touches it, and `_trip_cells` is
--      linear in the snapshot, which this grows by ~15 %), but it is the reason the two families
--      will look very different in the decision log's `relayed` segment.
et_cancelled AS (
  SELECT
    r.operator, r.jkey, r.lineRef, r.stop_place, r.stop_name, r.stop_order,
    r.aimed_dep, r.aimed_arr, r.dep_delay_min, r.call_source, r.call_date,
    r.boarding_allowed, r.alighting_allowed,
    COALESCE(r.aimed_dep, r.aimed_arr) AS call_ts,
    -- The cancelled stretch, for the summary -- over EVERY cancelled call of the journey, not what
    -- the anti-join leaves. IGNORE NULLS: stop_name arrives through a LEFT JOIN in the planned
    -- source and one missing name should not blank the headline.
    FIRST_VALUE(r.stop_name IGNORE NULLS) OVER seg AS seg_from,
    LAST_VALUE(r.stop_name IGNORE NULLS)  OVER seg AS seg_to
  FROM `disruptions.bus_calls_planned_today` AS r
  WHERE r.call_cancelled
    AND (r.aimed_dep IS NOT NULL OR r.aimed_arr IS NOT NULL)
  WINDOW seg AS (
    PARTITION BY r.operator, r.jkey, r.call_date ORDER BY r.stop_order
    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
  )
),
covered AS (
  SELECT DISTINCT operator, jkey, stop_order, call_ts FROM base
),
et_only AS (
  SELECT e.*
  FROM et_cancelled AS e
  LEFT JOIN covered AS c
    ON  c.operator   = e.operator
    AND c.jkey       = e.jkey
    AND c.stop_order = e.stop_order
    AND c.call_ts    = e.call_ts
  WHERE c.jkey IS NULL
    -- Same 12 h reach forward as a situation's window, one hour back so a call whose time has only
    -- just passed still answers /impact for somebody at the stop and can still be the trip's
    -- `latest_observation`. See predict_calls.sql for why the lookback is narrower than what the
    -- situation-derived rows already carry.
    AND TIMESTAMP_DIFF(e.call_ts, CURRENT_TIMESTAMP(), MINUTE) BETWEEN -60 AND 720
)
SELECT
  s.operator,
  s.situationNumber, sd.served_summary AS summary, sd.description, s.onset_ts,
  s.jkey, s.lineRef, s.stop_place, s.stop_name, s.stop_order,
  s.aimed_dep, s.aimed_arr, s.is_terminal, s.is_origin, s.min_from_onset,
  s.et_declared_cancelled, s.et_dep_delay_min, s.call_source,
  s.boarding_allowed, s.alighting_allowed,
  ROUND(s.pc_cal, 4) AS p_cancel,
  IF(s.is_terminal, NULL, GREATEST(CAST(ROUND(EXP(s.dep_plog)-1) AS INT64), 0)) AS dep_delay_typ_min,
  IF(s.is_terminal, NULL, COALESCE(dpq_dep.p90_min, dpg_dep.p90_min))            AS dep_delay_p90_min,
  IF(s.is_origin,   NULL, GREATEST(CAST(ROUND(EXP(s.arr_plog)-1) AS INT64), 0)) AS arr_delay_typ_min,
  IF(s.is_origin,   NULL, COALESCE(dpq_arr.p90_min, dpg_arr.p90_min))            AS arr_delay_p90_min,
  IF(s.is_origin, NULL, ROUND(s.s5, 4))  AS p_arr_ge5,
  IF(s.is_origin, NULL, ROUND(s.s10, 4)) AS p_arr_ge10,
  IF(s.is_origin, NULL, ROUND(s.s20, 4)) AS p_arr_ge20,
  IF(s.is_origin, NULL, ROUND(s.s40, 4)) AS p_arr_ge40,
  IF(s.is_origin, NULL, CAST(ROUND(CASE
    WHEN s.s5  <= 0.5 THEN (1 - 0.5) / GREATEST(1 - s.s5, 0.001) * 5
    WHEN s.s10 <= 0.5 THEN 5  + (s.s5  - 0.5) / GREATEST(s.s5  - s.s10, 0.001) * 5
    WHEN s.s20 <= 0.5 THEN 10 + (s.s10 - 0.5) / GREATEST(s.s10 - s.s20, 0.001) * 10
    WHEN s.s40 <= 0.5 THEN 20 + (s.s20 - 0.5) / GREATEST(s.s20 - s.s40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.s40 / 0.5), 90) END) AS INT64)) AS arr_delay_p50_stack_min,
  IF(s.is_origin, NULL, CAST(ROUND(CASE
    WHEN s.s5  <= 0.1 THEN (1 - 0.1) / GREATEST(1 - s.s5, 0.001) * 5
    WHEN s.s10 <= 0.1 THEN 5  + (s.s5  - 0.1) / GREATEST(s.s5  - s.s10, 0.001) * 5
    WHEN s.s20 <= 0.1 THEN 10 + (s.s10 - 0.1) / GREATEST(s.s10 - s.s20, 0.001) * 10
    WHEN s.s40 <= 0.1 THEN 20 + (s.s20 - 0.1) / GREATEST(s.s20 - s.s40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.s40 / 0.1), 90) END) AS INT64)) AS arr_delay_p90_stack_min,
  -- arrival alert: per-horizon cancel + PLAIN per-horizon arr ge10
  IF(s.is_origin, NULL, ROUND(s.pc_cal + (1 - s.pc_cal) * s.ca10, 4)) AS p_impact,
  IF(s.is_terminal, NULL, ROUND(s.d5, 4))  AS p_dep_ge5,
  IF(s.is_terminal, NULL, ROUND(s.d10, 4)) AS p_dep_ge10,
  IF(s.is_terminal, NULL, ROUND(s.d20, 4)) AS p_dep_ge20,
  IF(s.is_terminal, NULL, ROUND(s.d40, 4)) AS p_dep_ge40,
  IF(s.is_terminal, NULL, CAST(ROUND(CASE
    WHEN s.d5  <= 0.5 THEN (1 - 0.5) / GREATEST(1 - s.d5, 0.001) * 5
    WHEN s.d10 <= 0.5 THEN 5  + (s.d5  - 0.5) / GREATEST(s.d5  - s.d10, 0.001) * 5
    WHEN s.d20 <= 0.5 THEN 10 + (s.d10 - 0.5) / GREATEST(s.d10 - s.d20, 0.001) * 10
    WHEN s.d40 <= 0.5 THEN 20 + (s.d20 - 0.5) / GREATEST(s.d20 - s.d40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.d40 / 0.5), 90) END) AS INT64)) AS dep_delay_p50_stack_min,
  IF(s.is_terminal, NULL, CAST(ROUND(CASE
    WHEN s.d5  <= 0.1 THEN (1 - 0.1) / GREATEST(1 - s.d5, 0.001) * 5
    WHEN s.d10 <= 0.1 THEN 5  + (s.d5  - 0.1) / GREATEST(s.d5  - s.d10, 0.001) * 5
    WHEN s.d20 <= 0.1 THEN 10 + (s.d10 - 0.1) / GREATEST(s.d10 - s.d20, 0.001) * 10
    WHEN s.d40 <= 0.1 THEN 20 + (s.d20 - 0.1) / GREATEST(s.d20 - s.d40, 0.001) * 20
    ELSE LEAST(40 * SQRT(s.d40 / 0.1), 90) END) AS INT64)) AS dep_delay_p90_stack_min,
  -- boarding alert: per-horizon cancel + PLAIN per-horizon dep ge10
  IF(s.is_terminal, NULL, ROUND(s.pc_cal + (1 - s.pc_cal) * s.cd10, 4)) AS p_impact_dep,
  IF(s.is_origin,   NULL, ROUND(s.ca10, 4)) AS p_arr_ge10_h,
  IF(s.is_terminal, NULL, ROUND(s.cd10, 4)) AS p_dep_ge10_h,
  s.lead_bucket AS platt_lead_bucket,
  sd.summary_en,
  sd.description_en,
  sd.report_type
FROM curve AS s
-- Situation description for serving. GROUP BY guarantees one row per situationNumber, so this
-- join can never fan out the scored calls even if the source ever carries a duplicate.
LEFT JOIN (
  SELECT situationNumber, ANY_VALUE(served_summary) AS served_summary,
         ANY_VALUE(description) AS description,
         ANY_VALUE(summary_en) AS summary_en, ANY_VALUE(description_en) AS description_en,
         ANY_VALUE(report_type) AS report_type
  FROM sit GROUP BY situationNumber
) AS sd ON sd.situationNumber = s.situationNumber
-- dep p90: operator row (clamped to operator bounds), else global '*' row (clamped to '*' bounds)
LEFT JOIN bnd_op AS bo_dep ON bo_dep.side='dep' AND bo_dep.operator=s.operator
LEFT JOIN bnd_op AS bg_dep ON bg_dep.side='dep' AND bg_dep.operator='*'
LEFT JOIN `disruptions.delay_p90_calib_pooled_all` AS dpq_dep
  ON dpq_dep.side='dep' AND dpq_dep.operator=s.operator
 AND dpq_dep.plog_bucket = LEAST(GREATEST(ROUND(s.dep_plog,1), bo_dep.lo), bo_dep.hi)
LEFT JOIN `disruptions.delay_p90_calib_pooled_all` AS dpg_dep
  ON dpg_dep.side='dep' AND dpg_dep.operator='*'
 AND dpg_dep.plog_bucket = LEAST(GREATEST(ROUND(s.dep_plog,1), bg_dep.lo), bg_dep.hi)
-- arr p90: same, arrival side
LEFT JOIN bnd_op AS bo_arr ON bo_arr.side='arr' AND bo_arr.operator=s.operator
LEFT JOIN bnd_op AS bg_arr ON bg_arr.side='arr' AND bg_arr.operator='*'
LEFT JOIN `disruptions.delay_p90_calib_pooled_all` AS dpq_arr
  ON dpq_arr.side='arr' AND dpq_arr.operator=s.operator
 AND dpq_arr.plog_bucket = LEAST(GREATEST(ROUND(s.arr_plog,1), bo_arr.lo), bo_arr.hi)
LEFT JOIN `disruptions.delay_p90_calib_pooled_all` AS dpg_arr
  ON dpg_arr.side='arr' AND dpg_arr.operator='*'
 AND dpg_arr.plog_bucket = LEAST(GREATEST(ROUND(s.arr_plog,1), bg_arr.lo), bg_arr.hi)

UNION ALL
-- The ET-declared cancellations no situation named (see `et_only` above). Certainty in place of
-- every model score, NULL for every figure that meant "if it runs". Column order is the contract:
-- keep this list in step with the SELECT above it, starting with `operator`.
SELECT
  e.operator,
  CONCAT('KIX:ETCancellation:', e.operator, ':', e.jkey, ':', CAST(e.call_date AS STRING))
    AS situationNumber,
  CASE
    WHEN e.seg_from IS NULL OR e.seg_to IS NULL THEN 'Innstilt avgang'
    WHEN e.seg_from = e.seg_to                  THEN CONCAT('Innstilt fra ', e.seg_from)
    ELSE CONCAT('Innstilt ', e.seg_from, '–', e.seg_to)
  END AS summary,
  'Avgangen er markert innstilt i operatørens sanntidsdata.' AS description,
  -- No SX message means no creationTime: there is no onset to state and nothing to measure from.
  CAST(NULL AS TIMESTAMP) AS onset_ts,
  e.jkey, e.lineRef, e.stop_place, e.stop_name, e.stop_order,
  e.aimed_dep, e.aimed_arr,
  (e.aimed_dep IS NULL) AS is_terminal,
  (e.aimed_arr IS NULL) AS is_origin,
  CAST(NULL AS INT64)   AS min_from_onset,
  TRUE                  AS et_declared_cancelled,
  e.dep_delay_min       AS et_dep_delay_min,
  e.call_source,
  e.boarding_allowed, e.alighting_allowed,
  1.0                   AS p_cancel,
  CAST(NULL AS INT64)   AS dep_delay_typ_min,
  CAST(NULL AS INT64)   AS dep_delay_p90_min,
  CAST(NULL AS INT64)   AS arr_delay_typ_min,
  CAST(NULL AS INT64)   AS arr_delay_p90_min,
  CAST(NULL AS FLOAT64) AS p_arr_ge5,
  CAST(NULL AS FLOAT64) AS p_arr_ge10,
  CAST(NULL AS FLOAT64) AS p_arr_ge20,
  CAST(NULL AS FLOAT64) AS p_arr_ge40,
  CAST(NULL AS INT64)   AS arr_delay_p50_stack_min,
  CAST(NULL AS INT64)   AS arr_delay_p90_stack_min,
  -- The alerting scores, and the only two figures here that are not NULL: 1.0, on the side that
  -- exists. An origin has no arrival and a terminal has no departure, the same per-side NULLing the
  -- scored branch applies; `impact._alert_score` reads both sides and takes the max.
  IF(e.aimed_arr IS NULL, NULL, 1.0) AS p_impact,
  CAST(NULL AS FLOAT64) AS p_dep_ge5,
  CAST(NULL AS FLOAT64) AS p_dep_ge10,
  CAST(NULL AS FLOAT64) AS p_dep_ge20,
  CAST(NULL AS FLOAT64) AS p_dep_ge40,
  CAST(NULL AS INT64)   AS dep_delay_p50_stack_min,
  CAST(NULL AS INT64)   AS dep_delay_p90_stack_min,
  IF(e.aimed_dep IS NULL, NULL, 1.0) AS p_impact_dep,
  CAST(NULL AS FLOAT64) AS p_arr_ge10_h,
  CAST(NULL AS FLOAT64) AS p_dep_ge10_h,
  -- No model was scored, so no platt layer calibrated anything. Naming a bucket would claim a
  -- calibration that never ran.
  CAST(NULL AS STRING)  AS platt_lead_bucket,
  -- English beside the Norwegian, written here for the same reason the Norwegian is: no operator
  -- published either. Stop names stay as ET gives them.
  CASE
    WHEN e.seg_from IS NULL OR e.seg_to IS NULL THEN 'Cancelled departure'
    WHEN e.seg_from = e.seg_to                  THEN CONCAT('Cancelled from ', e.seg_from)
    ELSE CONCAT('Cancelled ', e.seg_from, '–', e.seg_to)
  END AS summary_en,
  "The departure is marked as cancelled in the operator's real-time data." AS description_en,
  CAST(NULL AS STRING)  AS report_type
FROM et_only AS e;
