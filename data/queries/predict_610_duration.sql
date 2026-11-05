-- Predict reopening duration ("trains running again in ~X min") for every
-- post-training disruption in disruptions_initial_new that affects NSR:StopPlace:610.
--
-- dur_init_reopen was trained on training_initial's feature shape, so we rebuild
-- that shape here: category lookup, od_corridors, and the exact stop_*/od_* multi-hot
-- columns the model expects (names taken verbatim from ML.FEATURE_INFO). The model
-- predicts LN(reopen_minutes), so the point estimate in minutes = EXP(predicted_label);
-- the P50/P80/P90 range applies the calibrated LN-residual offsets from
-- duration_quantiles (target='reopen') for "running again in ~X, up to ~Z min" comms.
--
-- NOTE on population: create_training_initial.sql dropped "order not received" rows
-- (description LIKE 'Bestilling ikke mottatt%' / 'Ingen bestilling mottatt%' /
-- 'Innstilt grunnet manglende bestilling%'). That filter is intentionally omitted
-- here so every 610 incident is scored; add it to `src` to match the training slice.
--
-- Companion "traffic impacted for ~Y min" number: swap the model to dur_init_impact
-- and the quantile filter to target='impact' — the `feat` block is identical.
--
-- ACTUALS ("what actually happened" at 610): the actual_* columns come from
-- rail_calls_new (ET-derived: realized cancellations + departure delays at 610).
-- actual_impact_min mirrors create_disruptions_initial's impact label scoped to 610
-- (impacted call = cancelled OR dep_delay >= 15 min; a >60-min quiet gap ends the
-- episode; 12h cap) — the apples-to-apples realized duration to set against
-- pred_reopen_min. NOTE: rail_calls_new must cover each incident's onset + 12h or the
-- actuals come back 0/NULL (refresh it first with create_rail_calls_new.sql); and a
-- 0 there means "no measured impact OR no ET coverage yet".

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
  FROM `disruptions.disruptions_initial_new` AS d
  WHERE d.participantRef = 'NSB'
    AND 'NSR:StopPlace:610' IN UNNEST(d.affected_stops)
),
feat AS (
  SELECT
    -- pass-through (not features; ML.PREDICT carries them to the output)
    d.situationNumber,
    d.onset_ts,
    d.summary,
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
-- ===== realized outcome at NSR:StopPlace:610, from ET-derived rail_calls_new =====
inc AS (
  SELECT situationNumber, onset_ts FROM src
),
-- departures at 610 within each incident's onset + 12h window
calls610 AS (
  SELECT
    i.situationNumber,
    i.onset_ts,
    r.aimed_dep,
    r.call_cancelled,
    r.dep_delay_min,
    (r.call_cancelled OR r.dep_delay_min >= 15) AS impacted   -- 15-min threshold (label default)
  FROM inc AS i
  JOIN `disruptions.rail_calls_new` AS r
    ON r.stop_place = 'NSR:StopPlace:610'
   AND r.aimed_dep BETWEEN i.onset_ts AND TIMESTAMP_ADD(i.onset_ts, INTERVAL 12 HOUR)
),
-- raw realized stats at 610
actual_stats AS (
  SELECT
    situationNumber,
    COUNT(*)                                              AS actual_n_calls,
    COUNTIF(call_cancelled)                               AS actual_n_cancelled,
    COUNTIF(NOT call_cancelled AND dep_delay_min >= 15)   AS actual_n_delayed15,
    MAX(dep_delay_min)                                    AS actual_max_delay_min
  FROM calls610
  GROUP BY situationNumber
),
-- sessionize impacted calls (anchored at onset) -> realized impact duration at 610,
-- mirroring create_disruptions_initial (a >60-min quiet gap ends an episode; 12h cap).
anchored AS (
  SELECT situationNumber, onset_ts, onset_ts AS ts FROM inc
  UNION ALL
  SELECT situationNumber, onset_ts, aimed_dep   FROM calls610 WHERE impacted
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
)
SELECT
  p.situationNumber,
  p.onset_ts,
  p.category,
  p.declared_min                                       AS declared_window_min,
  ROUND(EXP(p.predicted_label))                                       AS pred_reopen_min,   -- point estimate
  ROUND(EXP(p.predicted_label + COALESCE(qc.off_p50, qg.off_p50)))     AS reopen_p50_min,
  ROUND(EXP(p.predicted_label + COALESCE(qc.off_p80, qg.off_p80)))     AS reopen_p80_min,
  ROUND(EXP(p.predicted_label + COALESCE(qc.off_p90, qg.off_p90)))     AS reopen_p90_min,    -- "up to ~Z min"
  -- what actually happened at 610 (ET-derived; 0/NULL until rail_calls_new covers onset+12h)
  IFNULL(s.actual_n_calls, 0)                          AS actual_n_calls,
  IFNULL(s.actual_n_cancelled, 0)                      AS actual_n_cancelled,
  IFNULL(s.actual_n_delayed15, 0)                      AS actual_n_delayed15,
  s.actual_max_delay_min,
  a.actual_impact_min,                                                       -- realized vs pred_reopen_min
  p.summary
FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.dur_init_reopen`,
                (SELECT * FROM feat)) AS p
-- per-category calibrated offsets, with the global '__ALL__' row as fallback for
-- sparse / UNKNOWN / unseen categories (see create_quantiles.sql)
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
LEFT JOIN actual_stats  AS s USING (situationNumber)
LEFT JOIN actual_impact AS a USING (situationNumber)
ORDER BY p.onset_ts;
