-- Score EVERY NSB disruption in disruptions_initial_today (today's situations) with the
-- duration models and set the predictions against what actually happened from SIRI-ET.
--
--   * reopen (dur_init_reopen)        -> "trains running again in ~X min"
--   * impact (impact_remaining_noet)  -> "timetable fully back to normal in ~Y min" (>= reopen)
--
-- The reopen model is the onset model (predicts time-to-reopen from the first SX
-- snapshot). The impact estimate now comes from the DURING-disruption model
-- impact_remaining_noet: it re-estimates the REMAINING impact each rebuild using how
-- long the disruption has already run plus the onset features, then we add elapsed_min
-- back to express it as minutes-from-onset. Held-out, re-estimating beats the old static
-- onset projection by ~18% MAE / ~29% median, and the gain grows the longer a disruption
-- runs (see docs/predict-duration/LEARNINGS.md).
--
-- Both models share `feat`, rebuilt once here: category lookup, od_corridors, the exact
-- stop_*/od_* multi-hot columns (names verbatim from ML.FEATURE_INFO), and the three
-- time-varying during features (elapsed_min, reopened_by_t, minutes_since_reopen). Each
-- model predicts LN(minutes); point estimate = EXP(predicted_label) (impact adds
-- elapsed_min), and the P50/P80/P90 range applies the calibrated LN-residual offsets from
-- duration_quantiles ('reopen' / 'remaining' targets) for "running again in ~X, up to ~Z min".
--
-- QUANTILES: each model uses its OWN calibrated LN-residual offsets from duration_quantiles
-- (target='reopen' for the reopen model, target='impact' for the impact model; see
-- create_quantiles.sql). Both join the per-category offset and COALESCE to the global
-- '__ALL__' row for sparse / UNKNOWN / previously-unseen categories.
--
-- NOTE on population: create_training_initial.sql dropped "order not received" rows
-- (description LIKE 'Bestilling ikke mottatt%' / 'Ingen bestilling mottatt%' /
-- 'Innstilt grunnet manglende bestilling%'). That filter is intentionally omitted here
-- so every incident is scored; add it to `src` to match the training slice.
--
-- ACTUALS ("what actually happened"): the actual_* columns come from rail_calls_today
-- (ET-derived realized cancellations + departure delays) measured within onset + 12h,
-- matched to each incident by affected STOP (stop_place IN affected_stops) OR by named
-- TRAIN (jkey IN affected_journeys). The train match matters because journey-only
-- situations carry NO affected_stops, so a stop-only join reads 0 even when the named
-- train was cancelled/delayed. actual_impact_min mirrors create_disruptions_initial's
-- impact label (impacted call = cancelled OR dep_delay >= 15 min; a >60-min quiet gap
-- ends the episode; 12h cap) — the apples-to-apples realized duration vs the predictions.
-- NOTE: rail_calls_today must cover each incident's onset + 12h or the actuals come back
-- 0/NULL (refresh it first with rail_calls_today.sql); a 0 there now means "no measured
-- impact at the stops OR on the named trains, OR no ET coverage yet".
--
-- actual_impact_min_lb is a SEPARATE variant that also looks back 60 min before onset,
-- to catch impact that began before the SX was posted (forward-only actual_impact_min
-- misses it). It equals actual_impact_min when there's no pre-onset impact; it is NOT a
-- redefinition of the impact label (which is strictly forward from onset).

CREATE OR REPLACE TABLE `ent-kixavvik-dev.disruptions.predictions_today` AS
WITH
src AS (
  SELECT
    d.*,
    ARRAY(
      SELECT DISTINCT
        (SELECT STRING_AGG(p, '-' ORDER BY p)
         FROM UNNEST(SPLIT(REGEXP_EXTRACT(j, r':\d+_([A-Za-z]+-[A-Za-z]+)'), '-')) AS p)
      FROM UNNEST(d.affected_journeys) AS j
      WHERE REGEXP_CONTAINS(j, r':\d+_([A-Za-z]+-[A-Za-z]+)')
    ) AS od_corridors
  FROM `disruptions.disruptions_initial_today` AS d
),
feat AS (
  SELECT
    -- pass-through (not features; ML.PREDICT carries them to the output)
    d.situationNumber,
    d.onset_ts,
    d.reopen_ts,   -- non-NULL once a reopening message was seen => disruption is over
    d.summary,
    d.description,
    d.declared_window_minutes AS declared_min,   -- operator's own estimate, for comparison
    -- scalar / categorical features
    IFNULL(c.category,   'UNKNOWN') AS category,
    IFNULL(d.severity,   'UNKNOWN') AS severity,
    IFNULL(d.reportType, 'UNKNOWN') AS reportType,
    d.priority,
    d.declared_window_minutes,
    ARRAY_LENGTH(d.affected_stops)    AS num_affected_stops,
    ARRAY_LENGTH(d.affected_journeys) AS num_affected_journeys,
    ARRAY_LENGTH(d.od_corridors)      AS num_corridors,
    d.month,
    d.dayofweek,
    d.hour,
    -- During-disruption features for impact_remaining_noet, as-of this predict run
    -- (CURRENT_TIMESTAMP is evaluated once per query). elapsed_min advances every
    -- rebuild, so the impact estimate re-anchors to how long the disruption has
    -- already run instead of being a fixed onset projection. reopen state is the
    -- observed reopening message (NULL while still shut). The onset reopen/impact
    -- models ignore these extra columns (ML.PREDICT matches features by name).
    TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), d.onset_ts, MINUTE)                      AS elapsed_min,
    (d.reopen_ts IS NOT NULL)                                                   AS reopened_by_t,
    IF(d.reopen_ts IS NOT NULL,
       GREATEST(TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), d.reopen_ts, MINUTE), 0), 0) AS minutes_since_reopen,
    -- top-50 stop multi-hot (exact column names the model was trained on)
    ('NSR:StopPlace:337' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_337,
    ('NSR:StopPlace:288' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_288,
    ('NSR:StopPlace:152' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_152,
    ('NSR:StopPlace:157' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_157,
    ('NSR:StopPlace:610' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_610,
    ('NSR:StopPlace:451' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_451,
    ('NSR:StopPlace:418' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_418,
    ('NSR:StopPlace:11'  IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_11,
    ('NSR:StopPlace:127' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_127,
    ('NSR:StopPlace:269' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_269,
    ('NSR:StopPlace:706' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_706,
    ('NSR:StopPlace:529' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_529,
    ('NSR:StopPlace:303' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_303,
    ('NSR:StopPlace:359' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_359,
    ('NSR:StopPlace:493' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_493,
    ('NSR:StopPlace:101' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_101,
    ('NSR:StopPlace:305' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_305,
    ('NSR:StopPlace:648' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_648,
    ('NSR:StopPlace:716' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_716,
    ('NSR:StopPlace:385' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_385,
    ('NSR:StopPlace:163' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_163,
    ('NSR:StopPlace:577' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_577,
    ('NSR:StopPlace:167' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_167,
    ('NSR:StopPlace:319' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_319,
    ('NSR:StopPlace:295' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_295,
    ('NSR:StopPlace:313' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_313,
    ('NSR:StopPlace:368' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_368,
    ('NSR:StopPlace:208' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_208,
    ('NSR:StopPlace:41'  IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_41,
    ('NSR:StopPlace:572' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_572,
    ('NSR:StopPlace:680' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_680,
    ('NSR:StopPlace:323' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_323,
    ('NSR:StopPlace:396' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_396,
    ('NSR:StopPlace:60'  IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_60,
    ('NSR:StopPlace:375' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_375,
    ('NSR:StopPlace:230' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_230,
    ('NSR:StopPlace:287' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_287,
    ('NSR:StopPlace:202' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_202,
    ('NSR:StopPlace:6'   IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_6,
    ('NSR:StopPlace:433' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_433,
    ('NSR:StopPlace:166' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_166,
    ('NSR:StopPlace:553' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_553,
    ('NSR:StopPlace:344' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_344,
    ('NSR:StopPlace:475' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_475,
    ('NSR:StopPlace:382' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_382,
    ('NSR:StopPlace:255' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_255,
    ('NSR:StopPlace:238' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_238,
    ('NSR:StopPlace:268' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_268,
    ('NSR:StopPlace:536' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_536,
    ('NSR:StopPlace:653' IN UNNEST(d.affected_stops)) AS stop_NSR_StopPlace_653,
    -- top-25 OD corridor multi-hot
    ('EVL-SKN'  IN UNNEST(d.od_corridors)) AS od_EVL_SKN,
    ('LLS-SPI'  IN UNNEST(d.od_corridors)) AS od_LLS_SPI,
    ('DAL-DRM'  IN UNNEST(d.od_corridors)) AS od_DAL_DRM,
    ('SKI-STB'  IN UNNEST(d.od_corridors)) AS od_SKI_STB,
    ('BRG-OSL'  IN UNNEST(d.od_corridors)) AS od_BRG_OSL,
    ('ASR-LLS'  IN UNNEST(d.od_corridors)) AS od_ASR_LLS,
    ('EVL-KBG'  IN UNNEST(d.od_corridors)) AS od_EVL_KBG,
    ('MOS-STB'  IN UNNEST(d.od_corridors)) AS od_MOS_STB,
    ('DRM-LHM'  IN UNNEST(d.od_corridors)) AS od_DRM_LHM,
    ('ASR-KVG'  IN UNNEST(d.od_corridors)) AS od_ASR_KVG,
    ('ASR-LHM'  IN UNNEST(d.od_corridors)) AS od_ASR_LHM,
    ('OSL-SKI'  IN UNNEST(d.od_corridors)) AS od_OSL_SKI,
    ('HLD-OSL'  IN UNNEST(d.od_corridors)) AS od_HLD_OSL,
    ('SKI-SKOe' IN UNNEST(d.od_corridors)) AS od_SKI_SKOe,
    ('GJOe-OSL' IN UNNEST(d.od_corridors)) AS od_GJOe_OSL,
    ('OSL-RST'  IN UNNEST(d.od_corridors)) AS od_OSL_RST,
    ('LYS-SKI'  IN UNNEST(d.od_corridors)) AS od_LYS_SKI,
    ('OSL-SKN'  IN UNNEST(d.od_corridors)) AS od_OSL_SKN,
    ('DAL-KBG'  IN UNNEST(d.od_corridors)) AS od_DAL_KBG,
    ('DRM-EVL'  IN UNNEST(d.od_corridors)) AS od_DRM_EVL,
    ('BRA-EVL'  IN UNNEST(d.od_corridors)) AS od_BRA_EVL,
    ('BRG-VOS'  IN UNNEST(d.od_corridors)) AS od_BRG_VOS,
    ('MYS-OSL'  IN UNNEST(d.od_corridors)) AS od_MYS_OSL,
    ('BRG-MYR'  IN UNNEST(d.od_corridors)) AS od_BRG_MYR,
    ('JAR-OSL'  IN UNNEST(d.od_corridors)) AS od_JAR_OSL
  FROM src AS d
  LEFT JOIN `disruptions.categories` AS c
    ON  IFNULL(d.summary, '__NULL__')     = IFNULL(c.summary, '__NULL__')
    AND IFNULL(d.description, '__NULL__') = IFNULL(c.description, '__NULL__')
),
-- ===== predictions: one ML.PREDICT per model, both fed the shared `feat` shape =====
-- reopen model: "trains running again in ~X min". Per-category 'reopen' offsets, with the
-- global '__ALL__' row as fallback for sparse / UNKNOWN / unseen categories.
reopen_pred AS (
  SELECT
    p.situationNumber,
    p.onset_ts,
    p.reopen_ts,
    p.category,
    p.num_affected_stops,
    p.declared_min,
    p.summary,
    p.description,
    ROUND(EXP(p.predicted_label))                                    AS pred_reopen_min,   -- point estimate
    ROUND(EXP(p.predicted_label + COALESCE(qc.off_p50, qg.off_p50)))  AS reopen_p50_min,
    ROUND(EXP(p.predicted_label + COALESCE(qc.off_p80, qg.off_p80)))  AS reopen_p80_min,
    ROUND(EXP(p.predicted_label + COALESCE(qc.off_p90, qg.off_p90)))  AS reopen_p90_min     -- "up to ~Z min"
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dur_init_reopen`,
                  (SELECT * FROM feat)) AS p
  LEFT JOIN (
    SELECT category, off_p50, off_p80, off_p90
    FROM `disruptions.duration_quantiles`
    WHERE target = 'reopen' AND category != '__ALL__'
  ) AS qc ON qc.category = p.category
  CROSS JOIN (
    SELECT off_p50, off_p80, off_p90
    FROM `disruptions.duration_quantiles`
    WHERE target = 'reopen' AND category = '__ALL__'
  ) AS qg
),
-- impact model: "timetable fully back to normal in ~Y min" (>= reopen). Uses the
-- DURING-disruption model impact_remaining_noet, which predicts the REMAINING impact
-- from now given how long the disruption has already run (elapsed_min) and whether the
-- line has reopened. We express it back as minutes-FROM-ONSET (= elapsed_min +
-- remaining) so the API's existing "minutes from now = est_from_onset - elapsed"
-- machinery is unchanged -- but because elapsed_min is re-read each rebuild, the
-- estimate re-anchors instead of drifting stale once a disruption outlasts its onset
-- projection. Per-category 'remaining' offsets bound it, global '__ALL__' as fallback.
impact_pred AS (
  SELECT
    p.situationNumber,
    ROUND(p.elapsed_min + EXP(p.predicted_label))                                    AS pred_impact_min,   -- point estimate
    ROUND(p.elapsed_min + EXP(p.predicted_label + COALESCE(qc.off_p50, qg.off_p50)))  AS impact_p50_min,
    ROUND(p.elapsed_min + EXP(p.predicted_label + COALESCE(qc.off_p80, qg.off_p80)))  AS impact_p80_min,
    ROUND(p.elapsed_min + EXP(p.predicted_label + COALESCE(qc.off_p90, qg.off_p90)))  AS impact_p90_min     -- "up to ~Z min"
  FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.impact_remaining_noet`,
                  (SELECT * FROM feat)) AS p
  LEFT JOIN (
    SELECT category, off_p50, off_p80, off_p90
    FROM `disruptions.duration_quantiles`
    WHERE target = 'remaining' AND category != '__ALL__'
  ) AS qc ON qc.category = p.category
  CROSS JOIN (
    SELECT off_p50, off_p80, off_p90
    FROM `disruptions.duration_quantiles`
    WHERE target = 'remaining' AND category = '__ALL__'
  ) AS qg
),
-- ===== realized outcome per incident (matched by affected stop OR named train) =====
inc AS (
  SELECT
    situationNumber,
    onset_ts,
    affected_stops,
    -- map SX journey refs ('VYG:DatedServiceJourney:1125_..') to rail_calls jkey form
    ARRAY(SELECT REGEXP_REPLACE(j, r'^[A-Za-z]+:', '') FROM UNNEST(affected_journeys) AS j)
      AS affected_jkeys
  FROM src
),
-- calls within onset + 12h that hit one of the incident's affected stops OR run on one
-- of its named trains. The OR is a single join predicate, so each call row matches an
-- incident at most once (no double counting). The train arm rescues journey-only
-- situations (empty affected_stops) that the stop arm alone would miss.
calls AS (
  SELECT
    i.situationNumber,
    i.onset_ts,
    r.aimed_dep,
    r.call_cancelled,
    r.dep_delay_min,
    (r.call_cancelled OR r.dep_delay_min >= 15) AS impacted   -- 15-min threshold (label default)
  FROM inc AS i
  JOIN `disruptions.rail_calls_today` AS r
    ON r.aimed_dep BETWEEN i.onset_ts AND TIMESTAMP_ADD(i.onset_ts, INTERVAL 12 HOUR)
   AND ( r.stop_place IN UNNEST(i.affected_stops)
      OR r.jkey       IN UNNEST(i.affected_jkeys) )
),
-- raw realized stats across affected stops
actual_stats AS (
  SELECT
    situationNumber,
    COUNT(*)                                              AS actual_n_calls,
    COUNTIF(call_cancelled)                               AS actual_n_cancelled,
    COUNTIF(NOT call_cancelled AND dep_delay_min >= 15)   AS actual_n_delayed15,
    MAX(dep_delay_min)                                    AS actual_max_delay_min
  FROM calls
  GROUP BY situationNumber
),
-- sessionize impacted calls (anchored at onset) -> realized impact duration,
-- mirroring create_disruptions_initial (a >60-min quiet gap ends an episode; 12h cap).
anchored AS (
  SELECT situationNumber, onset_ts, onset_ts AS ts FROM inc
  UNION ALL
  SELECT situationNumber, onset_ts, aimed_dep   FROM calls WHERE impacted
),
sessed AS (
  SELECT
    situationNumber, onset_ts, ts,
    SUM(CASE WHEN prev_ts IS NULL OR TIMESTAMP_DIFF(ts, prev_ts, MINUTE) > 60 THEN 1 ELSE 0 END)
      OVER (PARTITION BY situationNumber ORDER BY ts) AS session_id
  FROM (
    SELECT situationNumber, onset_ts, ts,
      LAG(ts) OVER (PARTITION BY situationNumber ORDER BY ts) AS prev_ts
    FROM anchored
  )
),
actual_impact AS (
  SELECT situationNumber, TIMESTAMP_DIFF(MAX(ts), MIN(onset_ts), MINUTE) AS actual_impact_min
  FROM sessed
  WHERE session_id = 1
  GROUP BY situationNumber
),
-- --- pre-onset look-back variant -------------------------------------------------
-- Same impact-episode logic, but also pulls IMPACTED calls from the 60 min BEFORE onset
-- (matched by stop or train). actual_impact_min_lb is the span of the episode that
-- CONTAINS onset, so it captures disruption that began before the SX was posted. It
-- equals actual_impact_min when there is no pre-onset impact. Change the INTERVAL to tune.
calls_pre AS (
  SELECT i.situationNumber, r.aimed_dep
  FROM inc AS i
  JOIN `disruptions.rail_calls_today` AS r
    ON r.aimed_dep >= TIMESTAMP_SUB(i.onset_ts, INTERVAL 60 MINUTE)
   AND r.aimed_dep <  i.onset_ts
   AND ( r.stop_place IN UNNEST(i.affected_stops)
      OR r.jkey       IN UNNEST(i.affected_jkeys) )
   AND ( r.call_cancelled OR r.dep_delay_min >= 15 )
),
anchored_lb AS (
  SELECT situationNumber, onset_ts AS ts, TRUE  AS is_onset FROM inc
  UNION ALL
  SELECT situationNumber, aimed_dep,      FALSE AS is_onset FROM calls WHERE impacted
  UNION ALL
  SELECT situationNumber, aimed_dep,      FALSE AS is_onset FROM calls_pre
),
sessed_lb AS (
  SELECT
    situationNumber, ts, is_onset,
    SUM(CASE WHEN prev_ts IS NULL OR TIMESTAMP_DIFF(ts, prev_ts, MINUTE) > 60 THEN 1 ELSE 0 END)
      OVER (PARTITION BY situationNumber ORDER BY ts) AS session_id
  FROM (
    SELECT situationNumber, ts, is_onset,
      LAG(ts) OVER (PARTITION BY situationNumber ORDER BY ts) AS prev_ts
    FROM anchored_lb
  )
),
-- the session that contains the onset seed row
onset_session_lb AS (
  SELECT situationNumber, ANY_VALUE(session_id) AS sid
  FROM sessed_lb WHERE is_onset GROUP BY situationNumber
),
actual_impact_lb AS (
  SELECT s.situationNumber,
         TIMESTAMP_DIFF(MAX(s.ts), MIN(s.ts), MINUTE) AS actual_impact_min_lb
  FROM sessed_lb AS s
  JOIN onset_session_lb AS o USING (situationNumber)
  WHERE s.session_id = o.sid
  GROUP BY s.situationNumber
)
SELECT
  r.situationNumber,
  r.onset_ts,
  r.reopen_ts,   -- NULL while ongoing; set to the reopening-message time once the line is back ("over")
  r.category,
  r.num_affected_stops,
  r.declared_min                                       AS declared_window_min,   -- operator's own estimate
  -- reopen model: "trains running again in ~X min"
  r.pred_reopen_min,
  r.reopen_p50_min,
  r.reopen_p80_min,
  r.reopen_p90_min,
  -- impact model: "timetable fully back to normal in ~Y min". Floored at the matching
  -- reopen estimate because impact_minutes = GREATEST(reopen_minutes, tail) by label
  -- definition, but the two independent regressors don't enforce it at predict time.
  GREATEST(r.pred_reopen_min, i.pred_impact_min) AS pred_impact_min,
  GREATEST(r.reopen_p50_min,  i.impact_p50_min)  AS impact_p50_min,
  GREATEST(r.reopen_p80_min,  i.impact_p80_min)  AS impact_p80_min,
  GREATEST(r.reopen_p90_min,  i.impact_p90_min)  AS impact_p90_min,
  -- what actually happened (ET-derived, matched by affected stop OR named train; 0/NULL until rail_calls_today covers onset+12h)
  IFNULL(s.actual_n_calls, 0)                          AS actual_n_calls,
  IFNULL(s.actual_n_cancelled, 0)                      AS actual_n_cancelled,
  IFNULL(s.actual_n_delayed15, 0)                      AS actual_n_delayed15,
  s.actual_max_delay_min,
  a.actual_impact_min,                                                       -- forward-only (onset -> onset+12h)
  b.actual_impact_min_lb,                                                    -- incl. 60-min pre-onset look-back
  r.summary,
  r.description
FROM reopen_pred AS r
JOIN impact_pred           AS i USING (situationNumber)
LEFT JOIN actual_stats     AS s USING (situationNumber)
LEFT JOIN actual_impact    AS a USING (situationNumber)
LEFT JOIN actual_impact_lb AS b USING (situationNumber)
ORDER BY r.pred_reopen_min DESC, r.onset_ts;
