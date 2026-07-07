-- Point-in-time variant of the disruptions_today.sql + predict.sql chain, parameterized by
-- @as_of (a TIMESTAMP). Returns the same servable columns as active_disruptions.sql, but
-- reconstructed AS OF @as_of instead of "now": GET /disruptions?at=... runs this directly
-- (bypassing the in-process cache, which only ever holds today's live snapshot).
--
-- WHAT "AS OF" MEANS HERE. Only SX versions with event_ts <= @as_of are considered, so a
-- situation's onset/feature snapshot is its first version at or before @as_of, its reopen state
-- reflects only reopening messages seen by then (a reopening that arrived later stays NULL =
-- "still shut at @as_of"), and the during-disruption features (elapsed_min, reopened_by_t,
-- minutes_since_reopen) are measured against @as_of. Nothing from after @as_of leaks in.
--
-- The 2-day partition window ([@as_of - 1 day, @as_of], UTC, like the live build's CURRENT_DATE()
-- scope but with one day of look-back for cross-midnight onset/ingestion lag) bounds cost; the
-- event_ts <= @as_of filter does the real point-in-time cut.
--
-- KEEP IN SYNC: the onset/reopen reconstruction (base) is copied from disruptions_today.sql, and
-- the feature shape (feat: the stop_*/od_* multi-hot lists, the categories/duration_quantiles
-- joins, the two ML.PREDICT models) is copied verbatim from predict.sql. Unlike the live chain
-- this does NOT build the impact label or ET-derived actuals (rail_calls), nor run
-- update_categories -- the endpoint serves only predictions, and a previously-unseen category
-- falls back to 'UNKNOWN' (handled by the model and the '__ALL__' quantile row).

WITH all_versions AS (
  SELECT
    participantRef, situationNumber, version, creationTime,
    COALESCE(SAFE.TIMESTAMP(versionedAtTime), ingestionTime) AS event_ts,
    severity, planned, reportType, priority, validityPeriods, summaries, descriptions, affects
  FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
  WHERE DATE(_PARTITIONTIME) BETWEEN DATE_SUB(DATE(@as_of), INTERVAL 1 DAY) AND DATE(@as_of)
    AND participantRef = 'NSB'
    AND COALESCE(SAFE.TIMESTAMP(versionedAtTime), ingestionTime) <= @as_of
),
-- First version (at or before @as_of) = feature snapshot AND clock start (onset).
first_version AS (
  SELECT *
  FROM all_versions
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY participantRef, situationNumber
    ORDER BY event_ts ASC, version ASC, creationTime ASC
  ) = 1
),
-- Observed reopening = earliest snapshot announcing the line is back, among versions <= @as_of.
end_times AS (
  SELECT participantRef, situationNumber, MIN(event_ts) AS reopen_ts
  FROM all_versions
  WHERE EXISTS (
    SELECT 1 FROM UNNEST(summaries) AS s
    WHERE (s.language IS NULL OR UPPER(s.language) = 'NO')
      AND (s.value = 'Toget kjører igjen' OR s.value LIKE 'Åpnet%')
  )
  GROUP BY participantRef, situationNumber
),
base AS (
  SELECT
    fv.participantRef,
    fv.situationNumber,
    fv.creationTime,
    fv.event_ts                                        AS onset_ts,
    et.reopen_ts,
    fv.severity,
    fv.planned,
    fv.reportType,
    fv.priority,
    (
      SELECT TIMESTAMP_DIFF(MAX(SAFE.TIMESTAMP(v.endTime)), fv.event_ts, MINUTE)
      FROM UNNEST(fv.validityPeriods) AS v
      WHERE SAFE.TIMESTAMP(v.endTime) > fv.event_ts
        AND SAFE.TIMESTAMP(v.endTime) < TIMESTAMP_ADD(fv.event_ts, INTERVAL 365 DAY)
    ) AS declared_window_minutes,
    (SELECT x.value FROM UNNEST(fv.summaries) AS x
     WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
     LIMIT 1) AS summary,
    (SELECT x.value FROM UNNEST(fv.descriptions) AS x
     WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
     LIMIT 1) AS description,
    (
      SELECT ARRAY_AGG(stop_ref ORDER BY stop_ref)
      FROM (
        SELECT stops.stopPointRef AS stop_ref
        FROM UNNEST(fv.affects.stopPoints) AS stops WHERE stops.stopPointRef IS NOT NULL
        UNION DISTINCT
        SELECT sp.stopPlaceRef
        FROM UNNEST(fv.affects.stopPlaces) AS sp WHERE sp.stopPlaceRef IS NOT NULL
        UNION DISTINCT
        SELECT sp.stopPointRef
        FROM UNNEST(fv.affects.networks) AS n, UNNEST(n.affectedLines) AS al,
             UNNEST(al.routes) AS r, UNNEST(r.stopPoints.stopPoints) AS sp
        WHERE sp.stopPointRef IS NOT NULL
        UNION DISTINCT
        SELECT sp.stopPointRef
        FROM UNNEST(fv.affects.vehicleJourneys) AS vj, UNNEST(vj.routes) AS r,
             UNNEST(r.stopPoints.stopPoints) AS sp
        WHERE sp.stopPointRef IS NOT NULL
      )
    ) AS affected_stops,
    (
      SELECT ARRAY_AGG(journey_ref ORDER BY journey_ref)
      FROM (
        SELECT journeys.framedVehicleJourneyRef.datedVehicleJourneyRef AS journey_ref
        FROM UNNEST(fv.affects.vehicleJourneys) AS journeys
        WHERE journeys.framedVehicleJourneyRef.datedVehicleJourneyRef IS NOT NULL
        UNION DISTINCT
        SELECT dvjr
        FROM UNNEST(fv.affects.vehicleJourneys) AS journeys, UNNEST(journeys.datedVehicleJourneyRefs) AS dvjr
        WHERE dvjr IS NOT NULL
        UNION DISTINCT
        -- plain vehicleJourneyRefs: ServiceJourney refs (RUT/KOL, replacement buses), NOT date-scoped
        SELECT vjr
        FROM UNNEST(fv.affects.vehicleJourneys) AS journeys, UNNEST(journeys.vehicleJourneyRefs) AS vjr
        WHERE vjr IS NOT NULL
      )
    ) AS affected_journeys,
    EXTRACT(MONTH     FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS month,
    EXTRACT(DAY       FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS day,
    EXTRACT(DAYOFWEEK FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS dayofweek,
    EXTRACT(HOUR      FROM fv.event_ts AT TIME ZONE 'Europe/Oslo') AS hour
  FROM first_version AS fv
  LEFT JOIN end_times AS et USING (participantRef, situationNumber)
  WHERE (fv.severity IS NULL OR fv.severity != 'NO_IMPACT')
),
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
  FROM base AS d
),
feat AS (
  SELECT
    -- pass-through (not features; ML.PREDICT carries them to the output)
    d.situationNumber,
    d.onset_ts,
    d.reopen_ts,   -- non-NULL once a reopening message was seen by @as_of => disruption was over
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
    -- During-disruption features for impact_remaining_noet, measured AS OF @as_of (the
    -- point-in-time analogue of predict.sql's CURRENT_TIMESTAMP() anchoring).
    TIMESTAMP_DIFF(@as_of, d.onset_ts, MINUTE)                      AS elapsed_min,
    (d.reopen_ts IS NOT NULL)                                       AS reopened_by_t,
    IF(d.reopen_ts IS NOT NULL,
       GREATEST(TIMESTAMP_DIFF(@as_of, d.reopen_ts, MINUTE), 0), 0) AS minutes_since_reopen,
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
    ROUND(EXP(p.predicted_label))                                    AS pred_reopen_min,
    ROUND(EXP(p.predicted_label + COALESCE(qc.off_p50, qg.off_p50)))  AS reopen_p50_min,
    ROUND(EXP(p.predicted_label + COALESCE(qc.off_p80, qg.off_p80)))  AS reopen_p80_min,
    ROUND(EXP(p.predicted_label + COALESCE(qc.off_p90, qg.off_p90)))  AS reopen_p90_min
  FROM ML.PREDICT(MODEL `disruptions.dur_init_reopen`,
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
impact_pred AS (
  SELECT
    p.situationNumber,
    ROUND(p.elapsed_min + EXP(p.predicted_label))                                    AS pred_impact_min,
    ROUND(p.elapsed_min + EXP(p.predicted_label + COALESCE(qc.off_p50, qg.off_p50)))  AS impact_p50_min,
    ROUND(p.elapsed_min + EXP(p.predicted_label + COALESCE(qc.off_p80, qg.off_p80)))  AS impact_p80_min,
    ROUND(p.elapsed_min + EXP(p.predicted_label + COALESCE(qc.off_p90, qg.off_p90)))  AS impact_p90_min
  FROM ML.PREDICT(MODEL `disruptions.impact_remaining_noet`,
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
)
SELECT
  r.situationNumber,
  r.summary,
  r.description,
  r.category,
  r.onset_ts,
  r.reopen_ts,                  -- NULL if not reopened as of @as_of; reopening-message time otherwise
  r.num_affected_stops,
  r.declared_min AS declared_window_min,
  -- reopen model: "trains running again in ~X min" (minutes from onset)
  r.pred_reopen_min,
  r.reopen_p50_min,
  r.reopen_p80_min,
  r.reopen_p90_min,
  -- impact model: floored at the matching reopen estimate, as in predict.sql
  GREATEST(r.pred_reopen_min, i.pred_impact_min) AS pred_impact_min,
  GREATEST(r.reopen_p50_min,  i.impact_p50_min)  AS impact_p50_min,
  GREATEST(r.reopen_p80_min,  i.impact_p80_min)  AS impact_p80_min,
  GREATEST(r.reopen_p90_min,  i.impact_p90_min)  AS impact_p90_min
FROM reopen_pred AS r
JOIN impact_pred AS i USING (situationNumber)
ORDER BY r.reopen_ts IS NOT NULL, r.onset_ts DESC   -- still-ongoing first, then reopened, newest onset first
