-- Live togvarsel features: rebuild disruptions.togvarsel_features_today (ONE row — today's
-- feature vector for the togmelding models) and disruptions.togvarsel_sx_today (today's SX
-- messages, for the deterministic "kjente avvik" list and the forecast's reason text).
--
-- This is the SERVING twin of the training chain in data/queries/togvarsel/ — same windows,
-- same class/cause regexes, same category groups, same zero-for-missing-count convention —
-- computed from the LIVE feeds (realtime ET + SX, yesterday's and today's partitions) instead
-- of the daily-batch tables. Any semantic change must land in BOTH places. Column names match
-- disruptions.togvarsel_training exactly; the models' TRANSFORM does the log1p, so everything
-- here stays raw.
--
-- An ET cutoff column (et_*_0630 etc.) uses calls AIMED before the cutoff minus 10 min, with
-- rates over resolved calls (cancelled or an actual time) — so running this a few minutes after
-- the cutoff gives the training-time semantics even though late-arriving updates for the last
-- pre-cutoff trains are still trickling in: they widen the denominator later, they don't change
-- what the columns mean. Run it AT OR AFTER the cutoff you are about to predict for, never
-- before.
--
-- KNOWN, ACCEPTED SKEW vs training: (1) training reads the end-of-day last_recorded snapshot,
-- so pre-cutoff calls whose updates arrived late are complete there and partial here;
-- (2) sx_amj_*/sx_pmj_* count affected journeys against the journeys ET currently knows —
-- at 06:30 the feed may not yet carry the whole 07–09 (and certainly not the whole 15–18) plan,
-- so those counts can read low early in the day. Both are bounded and documented in
-- docs/togvarsel/03-implementasjon.md.
--
-- Cost: two ET day-partitions (VYG) + two SX day-partitions — the same order as the poller's
-- rail_calls_today refresh, run only a handful of times per weekday.
DECLARE today DATE DEFAULT CURRENT_DATE('Europe/Oslo');
DECLARE yday DATE DEFAULT DATE_SUB(CURRENT_DATE('Europe/Oslo'), INTERVAL 1 DAY);
-- the previous weekday (Monday looks back to Friday), for the cut CTE's persistence test
DECLARE prevwd DATE DEFAULT IF(EXTRACT(DAYOFWEEK FROM CURRENT_DATE('Europe/Oslo')) = 2,
                               DATE_SUB(CURRENT_DATE('Europe/Oslo'), INTERVAL 3 DAY),
                               DATE_SUB(CURRENT_DATE('Europe/Oslo'), INTERVAL 1 DAY));

-- ---------------------------------------------------------------------------- ET calls (2 days)
CREATE TEMP TABLE _tv_calls AS
WITH last_snap AS (
  SELECT
    datedVehicleJourneyRef AS ref,
    IFNULL(extraJourney, FALSE) AS extra_journey,
    cancellation AS journey_cancelled,
    recordedCalls, estimatedCalls
  FROM `ent-anshar-prd.realtime.siri_realtime_et_production`
  WHERE DATE(_PARTITIONTIME) BETWEEN DATE_SUB(CURRENT_DATE('Europe/Oslo'), INTERVAL 1 DAY)
                                 AND CURRENT_DATE('Europe/Oslo')
    AND lineRef LIKE 'VYG:%'
    AND datedVehicleJourneyRef IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY datedVehicleJourneyRef ORDER BY SAFE.TIMESTAMP(recordedAtTime) DESC) = 1
),
calls AS (
  -- recorded (operated) stops: aimed + actual times
  SELECT
    s.ref, s.extra_journey, rc.stopPointRef AS quay,
    (IFNULL(rc.cancellation, FALSE) OR IFNULL(s.journey_cancelled, FALSE)) AS cancelled,
    SAFE.TIMESTAMP(rc.aimedArrivalTime) AS aimed_arr,
    SAFE.TIMESTAMP(rc.aimedDepartureTime) AS aimed_dep,
    SAFE.TIMESTAMP(rc.actualArrivalTime) AS actual_arr,
    SAFE.TIMESTAMP(rc.actualDepartureTime) AS actual_dep
  FROM last_snap s, UNNEST(s.recordedCalls) rc
  UNION ALL
  -- estimated (upcoming) stops: cancellations are declared here before the train ever runs
  SELECT
    s.ref, s.extra_journey, ec.stopPointRef,
    (IFNULL(ec.cancellation, FALSE) OR IFNULL(s.journey_cancelled, FALSE)),
    SAFE.TIMESTAMP(ec.aimedArrivalTime), SAFE.TIMESTAMP(ec.aimedDepartureTime),
    NULL, NULL
  FROM last_snap s, UNNEST(s.estimatedCalls) ec
)
SELECT
  c.ref, c.extra_journey, c.cancelled,
  q.stopPlaceRef AS stop_place,
  s.stop_place IS NOT NULL AS is_oslo,
  DATETIME(COALESCE(c.aimed_dep, c.aimed_arr), 'Europe/Oslo') AS aimed_local,
  DATE(DATETIME(COALESCE(c.aimed_dep, c.aimed_arr), 'Europe/Oslo')) AS d,
  EXTRACT(HOUR FROM DATETIME(COALESCE(c.aimed_dep, c.aimed_arr), 'Europe/Oslo'))
    + EXTRACT(MINUTE FROM DATETIME(COALESCE(c.aimed_dep, c.aimed_arr), 'Europe/Oslo'))/60.0 AS h,
  COALESCE(TIMESTAMP_DIFF(c.actual_arr, c.aimed_arr, SECOND)/60.0,
           TIMESTAMP_DIFF(c.actual_dep, c.aimed_dep, SECOND)/60.0) AS delay_min,
  (c.cancelled OR c.actual_arr IS NOT NULL OR c.actual_dep IS NOT NULL) AS resolved
FROM calls c
LEFT JOIN `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version` q ON c.quay = q.id
LEFT JOIN `disruptions.togvarsel_oslo_stations` s ON q.stopPlaceRef = s.stop_place
WHERE COALESCE(c.aimed_dep, c.aimed_arr) IS NOT NULL;

-- Journey Oslo footprint, for resolving SX affected_journeys (the training-side twin is
-- disruptions.togvarsel_journeys; refs match exactly — both are full dated refs like
-- "VYG:DatedServiceJourney:2137_LLS-ASR_26-08-24").
CREATE TEMP TABLE _tv_journeys AS
SELECT ref,
  DATE(MIN(aimed_local)) AS operating_date,
  COUNTIF(is_oslo) AS n_oslo_calls,
  MIN(IF(is_oslo, aimed_local, NULL)) AS first_oslo_aimed_local
FROM _tv_calls
GROUP BY ref;

-- ---------------------------------------------------------------------------- SX events (2 days)
-- Mirrors data/queries/create_disruptions.sql (onset = first snapshot's Norwegian text, span =
-- latest snapshot's collapsed validity, same NO_IMPACT/1..10080 filters) restricted to the
-- messages that belong to TODAY's forecast: created between yesterday 18:00 and now (fh is
-- hours relative to today's midnight; a previous-evening message has fh < 0).
CREATE TEMP TABLE _tv_sx AS
WITH scored AS (
  SELECT
    participantRef, situationNumber, version, creationTime, ingestionTime, versionedAtTime,
    severity, planned, validityPeriods, affects,
    COALESCE(SAFE.TIMESTAMP(versionedAtTime), ingestionTime) AS event_ts,
    (SELECT x.value FROM UNNEST(summaries) AS x
     WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
     LIMIT 1) AS no_summary,
    (SELECT x.value FROM UNNEST(descriptions) AS x
     WHERE x.value IS NOT NULL AND (x.language IS NULL OR UPPER(x.language) = 'NO')
     LIMIT 1) AS no_description
  FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
  WHERE DATE(_PARTITIONTIME) BETWEEN DATE_SUB(CURRENT_DATE('Europe/Oslo'), INTERVAL 1 DAY)
                                 AND CURRENT_DATE('Europe/Oslo')
    AND participantRef IN ('NSB', 'VYG')
),
latest AS (
  SELECT *,
    FIRST_VALUE(no_summary)     OVER w_onset AS onset_summary,
    FIRST_VALUE(no_description) OVER w_onset AS onset_description
  FROM scored
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY participantRef, situationNumber
    ORDER BY SAFE.TIMESTAMP(versionedAtTime) DESC, version DESC, ingestionTime DESC) = 1
  WINDOW w_onset AS (
    PARTITION BY participantRef, situationNumber
    ORDER BY event_ts ASC, version ASC, creationTime ASC
    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)
),
spans AS (
  SELECT *,
    TIMESTAMP_DIFF(
      (SELECT MAX(SAFE.TIMESTAMP(v.endTime)) FROM UNNEST(validityPeriods) v),
      (SELECT MIN(SAFE.TIMESTAMP(v.startTime)) FROM UNNEST(validityPeriods) v),
      MINUTE) AS dur,
    DATETIME(SAFE.TIMESTAMP(creationTime), 'Europe/Oslo') AS created_local
  FROM latest
),
base AS (
  SELECT *,
    LOWER(CONCAT(IFNULL(onset_summary,''), ' ', IFNULL(onset_description,''))) AS txt,
    EXTRACT(HOUR FROM created_local) + EXTRACT(MINUTE FROM created_local)/60.0
      + IF(DATE(created_local) = DATE_SUB(CURRENT_DATE('Europe/Oslo'), INTERVAL 1 DAY), -24, 0) AS fh
  FROM spans
  WHERE (severity IS NULL OR severity != 'NO_IMPACT')
    AND dur BETWEEN 1 AND 10080
    -- belongs to TODAY's forecast: created today before 18:00, or yesterday from 18:00 on
    AND ((DATE(created_local) = CURRENT_DATE('Europe/Oslo')
          AND EXTRACT(HOUR FROM created_local) < 18)
      OR (DATE(created_local) = DATE_SUB(CURRENT_DATE('Europe/Oslo'), INTERVAL 1 DAY)
          AND EXTRACT(HOUR FROM created_local) >= 18))
),
cls AS (
  SELECT *,
    CASE
      WHEN txt LIKE 'færre vogner%' THEN 'faerre_vogner'
      -- the training side goes via create_disruptions.sql's regex-derived status; same regex
      WHEN REGEXP_CONTAINS(txt, r'innstil|kansel') THEN 'innstilt'
      WHEN txt LIKE 'toget står%' THEN 'toget_staar'
      WHEN txt LIKE 'forsinkelse forventes%' THEN 'forsinkelse_forventes'
      WHEN txt LIKE 'forsinket%' THEN 'forsinket'
      WHEN REGEXP_CONTAINS(txt, r'^(buss for tog|planlegger buss|buss i tillegg|planlegger alternativ|ta ruters|vy kjører taxi|taxi)') THEN 'buss_for_tog'
      WHEN txt LIKE 'stengt%' THEN 'stengt'
      WHEN txt LIKE 'ta neste tog%' OR txt LIKE 'ta andre tog%' THEN 'ta_neste_tog'
      WHEN txt LIKE 'ny kjørerute%' OR txt LIKE 'bruk de forreste%' THEN 'ny_kjoererute'
      WHEN txt LIKE 'færre avganger%' OR txt LIKE 'redusert%' THEN 'faerre_avganger'
      ELSE 'other' END AS cls,
    CASE
      WHEN txt LIKE '%mangler et togsett%' OR txt LIKE '%mangler togsett%' THEN 'mangler_togsett'
      WHEN txt LIKE '%teknisk feil med toget%' OR txt LIKE '%feil på toget%' OR txt LIKE '%feil med toget%' THEN 'feil_tog'
      WHEN txt LIKE '%signalfeil%' OR txt LIKE '%signalanlegg%' THEN 'signalfeil'
      WHEN txt LIKE '%feil på infrastruktur%' OR txt LIKE '%infrastruktur%' OR txt LIKE '%sporveksel%' OR txt LIKE '%feil på sporet%' OR txt LIKE '%skinnebrudd%' OR txt LIKE '%kjøreledning%' THEN 'infrastruktur'
      WHEN txt LIKE '%strøm%' THEN 'stroem'
      WHEN txt LIKE '%personell%' OR txt LIKE '%personale%' OR txt LIKE '%mangler lokfører%' OR txt LIKE '%konduktør%' THEN 'personell'
      WHEN txt LIKE '%vedlikehold%' OR txt LIKE '%arbeid på%' OR txt LIKE '%planlagt arbeid%' OR txt LIKE '%anleggsarbeid%' THEN 'vedlikehold'
      WHEN txt LIKE '%tidligere stans%' OR txt LIKE '%andre tog%forsinket%' OR txt LIKE '%kø%' OR txt LIKE '%tett trafikk%' THEN 'foelgeforsinkelse'
      WHEN txt LIKE '%dyr i sporet%' OR txt LIKE '%påkjørsel%' OR txt LIKE '%person i sporet%' OR txt LIKE '%uvedkommende%' THEN 'sporet'
      WHEN txt LIKE '%vær%' OR txt LIKE '%snø%' OR txt LIKE '%flom%' OR txt LIKE '%uvær%' OR txt LIKE '%is %' OR txt LIKE '%varme%' OR txt LIKE '%kulde%' THEN 'vaer'
      WHEN txt LIKE '%politi%' OR txt LIKE '%brann%' OR txt LIKE '%ulykke%' OR txt LIKE '%syk%' THEN 'hendelse'
      ELSE 'ukjent' END AS cause,
    (SELECT ARRAY_AGG(journey_ref IGNORE NULLS)
     FROM (
       SELECT j.framedVehicleJourneyRef.datedVehicleJourneyRef AS journey_ref
       FROM UNNEST(affects.vehicleJourneys) j
       UNION DISTINCT
       SELECT dvjr FROM UNNEST(affects.vehicleJourneys) j, UNNEST(j.datedVehicleJourneyRefs) dvjr
       UNION DISTINCT
       SELECT vjr FROM UNNEST(affects.vehicleJourneys) j, UNNEST(j.vehicleJourneyRefs) vjr
     )) AS affected_journeys
  FROM base
),
cat AS (
  SELECT s.situationNumber,
    CASE
      WHEN c.category IN ('ytre/vær/vinter','ytre/vær/nedbør','ytre/vær/vind') THEN 'vaer'
      WHEN c.category IS NULL OR c.category IN ('ytre/dyr','infrastruktur/tunnel','ytre/person','materiell/dør','materiell/bremse','materiell/motor') THEN 'other'
      ELSE REPLACE(REPLACE(REPLACE(c.category,'/','_'),'ø','o'),'æ','ae') END AS cat
  FROM (SELECT DISTINCT situationNumber, onset_summary, onset_description FROM cls) s
  LEFT JOIN `disruptions.categories` c
    ON IFNULL(s.onset_summary,'__NULL__')=IFNULL(c.summary,'__NULL__')
   AND IFNULL(s.onset_description,'__NULL__')=IFNULL(c.description,'__NULL__')
)
SELECT
  e.situationNumber AS sn, e.participantRef, e.created_local, e.fh, e.dur,
  IFNULL(e.planned, FALSE) AS planned, e.cls, e.cause, cat.cat,
  e.onset_summary, e.onset_description,
  j_ref,
  IFNULL(j.n_oslo_calls > 0, FALSE) AS oslo,
  j.operating_date,
  EXTRACT(HOUR FROM j.first_oslo_aimed_local)
    + EXTRACT(MINUTE FROM j.first_oslo_aimed_local)/60.0 AS amh
FROM cls e
JOIN cat USING (situationNumber)
LEFT JOIN UNNEST(e.affected_journeys) j_ref
LEFT JOIN _tv_journeys j ON j.ref = j_ref;

-- ---------------------------------------------------------------------------- the feature row
CREATE OR REPLACE TABLE `disruptions.togvarsel_features_today` AS
WITH oc AS (
  SELECT * FROM _tv_calls
  WHERE is_oslo AND NOT extra_journey AND d = CURRENT_DATE('Europe/Oslo')
),
et AS (
  SELECT
    -- cutoff 06:30 (aimed 04:00–06:20)
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.33 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<6.33 AND resolved)) et_t5_0630,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.33 AND (cancelled OR delay_min>=10)), COUNTIF(h>=4 AND h<6.33 AND resolved)) et_t10_0630,
    COUNTIF(h>=4 AND h<6.33 AND cancelled) et_cx_0630,
    COUNT(DISTINCT IF(h>=4 AND h<6.33 AND cancelled, ref, NULL)) et_cxj_0630,
    AVG(IF(h>=4 AND h<6.33 AND NOT cancelled, delay_min, NULL)) et_md_0630,
    MAX(IF(h>=4 AND h<6.33 AND NOT cancelled, delay_min, NULL)) et_max_0630,
    SAFE_DIVIDE(COUNTIF(h>=5.83 AND h<6.33 AND (cancelled OR delay_min>=5)), COUNTIF(h>=5.83 AND h<6.33 AND resolved)) et_t5_last_0630,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.33 AND cancelled), COUNTIF(h>=4 AND h<6.33)) et_cxr_0630,
    -- cutoff 07:00 (aimed 04:00–06:50)
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<6.83 AND resolved)) et_t5_0700,
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<6.83 AND (cancelled OR delay_min>=10)), COUNTIF(h>=4 AND h<6.83 AND resolved)) et_t10_0700,
    COUNTIF(h>=4 AND h<6.83 AND cancelled) et_cx_0700,
    COUNT(DISTINCT IF(h>=4 AND h<6.83 AND cancelled, ref, NULL)) et_cxj_0700,
    AVG(IF(h>=4 AND h<6.83 AND NOT cancelled, delay_min, NULL)) et_md_0700,
    MAX(IF(h>=4 AND h<6.83 AND NOT cancelled, delay_min, NULL)) et_max_0700,
    SAFE_DIVIDE(COUNTIF(h>=6.33 AND h<6.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=6.33 AND h<6.83 AND resolved)) et_t5_last_0700,
    -- cutoff 12:00 (aimed 04:00–11:50)
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<11.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<11.83 AND resolved)) et_t5_1200,
    COUNTIF(h>=4 AND h<11.83 AND cancelled) et_cx_1200,
    SAFE_DIVIDE(COUNTIF(h>=10.5 AND h<11.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=10.5 AND h<11.83 AND resolved)) et_t5_last_1200,
    -- cutoff 14:00 (aimed 04:00–13:50)
    SAFE_DIVIDE(COUNTIF(h>=4 AND h<13.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=4 AND h<13.83 AND resolved)) et_t5_1400,
    COUNTIF(h>=4 AND h<13.83 AND cancelled) et_cx_1400,
    SAFE_DIVIDE(COUNTIF(h>=12.5 AND h<13.83 AND (cancelled OR delay_min>=5)), COUNTIF(h>=12.5 AND h<13.83 AND resolved)) et_t5_last_1400,
    -- night (00:00–04:30, day_windows' ELSE bucket)
    SAFE_DIVIDE(COUNTIF(h<4.5 AND (cancelled OR delay_min>=5)), COUNTIF(h<4.5 AND resolved)) night_t5
  FROM oc
),
prev AS (
  SELECT
    SAFE_DIVIDE(COUNTIF(h>=18 AND h<24 AND (cancelled OR delay_min>=5)), COUNTIF(h>=18 AND h<24 AND resolved)) prev_eve_t5,
    SAFE_DIVIDE(COUNTIF(h>=18 AND h<24 AND cancelled), COUNTIF(h>=18 AND h<24)) prev_eve_cancel,
    SAFE_DIVIDE(COUNTIF(h>=15 AND h<18 AND (cancelled OR delay_min>=5)), COUNTIF(h>=15 AND h<18 AND resolved)) prev_pm_t5
  FROM _tv_calls
  WHERE is_oslo AND NOT extra_journey AND d = DATE_SUB(CURRENT_DATE('Europe/Oslo'), INTERVAL 1 DAY)
),
sx AS (
  SELECT
    -- 06:30
    COUNT(DISTINCT IF(fh < 6.5, sn, NULL)) sx_n_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND cls='faerre_vogner', sn, NULL)) sx_fv_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cause='mangler_togsett', sn, NULL)) sx_mt_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND cls='innstilt', sn, NULL)) sx_inn_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND cls IN ('toget_staar','forsinket','forsinkelse_forventes'), sn, NULL)) sx_delay_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cause IN ('signalfeil','infrastruktur','stroem'), sn, NULL)) sx_infra_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cause='personell', sn, NULL)) sx_pers_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cause='vedlikehold', sn, NULL)) sx_vedl_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND dur >= 600, sn, NULL)) sx_long_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND planned, sn, NULL)) sx_planned_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND cls='innstilt' AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_inn_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 7 AND 9, j_ref, NULL)) sx_amj_rest_0630,
    COUNT(DISTINCT IF(fh < 0, sn, NULL)) sx_preveve,
    COUNT(DISTINCT IF(fh < 0 AND oslo AND cls='innstilt', sn, NULL)) sx_preveve_inn,
    -- 07:00
    COUNT(DISTINCT IF(fh < 7.0, sn, NULL)) sx_n_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND oslo AND cls='innstilt', sn, NULL)) sx_inn_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND oslo AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND planned, sn, NULL)) sx_planned_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND oslo AND cls='innstilt' AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) sx_amj_inn_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 7 AND 9, j_ref, NULL)) sx_amj_rest_0700,
    -- 12:00
    COUNT(DISTINCT IF(fh < 12, sn, NULL)) sx_n_1200,
    COUNT(DISTINCT IF(fh < 12 AND oslo AND cls='innstilt', sn, NULL)) sx_inn_1200,
    COUNT(DISTINCT IF(fh < 12 AND oslo AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_1200,
    COUNT(DISTINCT IF(fh < 12 AND cause IN ('signalfeil','infrastruktur','stroem'), sn, NULL)) sx_infra_1200,
    COUNT(DISTINCT IF(fh < 12 AND planned, sn, NULL)) sx_planned_1200,
    COUNT(DISTINCT IF(fh < 12 AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_1200,
    COUNT(DISTINCT IF(fh < 12 AND oslo AND cls='innstilt' AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_inn_1200,
    -- 14:00
    COUNT(DISTINCT IF(fh < 14, sn, NULL)) sx_n_1400,
    COUNT(DISTINCT IF(fh < 14 AND oslo AND cls='innstilt', sn, NULL)) sx_inn_1400,
    COUNT(DISTINCT IF(fh < 14 AND oslo AND cls IN ('buss_for_tog','stengt','faerre_avganger'), sn, NULL)) sx_big_1400,
    COUNT(DISTINCT IF(fh < 14 AND planned, sn, NULL)) sx_planned_1400,
    COUNT(DISTINCT IF(fh < 14 AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_1400,
    COUNT(DISTINCT IF(fh < 14 AND oslo AND cls='innstilt' AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 15 AND 18, j_ref, NULL)) sx_pmj_inn_1400
  FROM _tv_sx
),
cats AS (
  SELECT
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_ruteendring', sn, NULL)) cat_drift_ruteendring_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_kapasitet', sn, NULL)) cat_drift_kapasitet_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='materiell_teknisk', sn, NULL)) cat_materiell_teknisk_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_signal', sn, NULL)) cat_infrastruktur_signal_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_spor', sn, NULL)) cat_infrastruktur_spor_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_strom', sn, NULL)) cat_infrastruktur_strom_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_sporveksel', sn, NULL)) cat_infrastruktur_sporveksel_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_personale', sn, NULL)) cat_drift_personale_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='ytre_tredjepart', sn, NULL)) cat_ytre_tredjepart_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='drift_planlagt', sn, NULL)) cat_drift_planlagt_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='infrastruktur_stasjon', sn, NULL)) cat_infrastruktur_stasjon_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='vaer', sn, NULL)) cat_vaer_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='other', sn, NULL)) cat_other_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat LIKE 'infrastruktur%' AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_infra_am_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat='materiell_teknisk' AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_mat_am_0630,
    COUNT(DISTINCT IF(fh < 6.5 AND cat LIKE 'drift%' AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_drift_am_0630,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='drift_ruteendring', sn, NULL)) cat_drift_ruteendring_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='drift_kapasitet', sn, NULL)) cat_drift_kapasitet_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='materiell_teknisk', sn, NULL)) cat_materiell_teknisk_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='infrastruktur_signal', sn, NULL)) cat_infrastruktur_signal_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='infrastruktur_spor', sn, NULL)) cat_infrastruktur_spor_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='infrastruktur_strom', sn, NULL)) cat_infrastruktur_strom_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='infrastruktur_sporveksel', sn, NULL)) cat_infrastruktur_sporveksel_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='drift_personale', sn, NULL)) cat_drift_personale_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='ytre_tredjepart', sn, NULL)) cat_ytre_tredjepart_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='drift_planlagt', sn, NULL)) cat_drift_planlagt_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='infrastruktur_stasjon', sn, NULL)) cat_infrastruktur_stasjon_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='vaer', sn, NULL)) cat_vaer_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='other', sn, NULL)) cat_other_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat LIKE 'infrastruktur%' AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_infra_am_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat='materiell_teknisk' AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_mat_am_0700,
    COUNT(DISTINCT IF(fh < 7.0 AND cat LIKE 'drift%' AND oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) catj_drift_am_0700
  FROM _tv_sx
),
cut AS (
  -- The day's plan against the standing timetable (docs/rushvarsel/04-cuts; the training twin is
  -- data/queries/togvarsel/03b_plan.sql and the six names must match 10_train_models.sql). A
  -- (line, train number) is STANDING if the GTFS plan carried it on >= 3 of the 4 previous
  -- same-weekday dates; a standing train absent from today's plan is a cut, counted in planned
  -- Oslo-area calls per label window and netted against trains new today; *_new is the part not
  -- already cut on the previous weekday. The GTFS partition for a day is written ~02:06 that
  -- day, so all four issues see it. RE20 is excluded (its numbering does not join). ~0.4 GB.
  WITH g AS (
    SELECT g.operating_date d, g.route_short_name line, REGEXP_EXTRACT(g.trip_id, r'ServiceJourney:(\d+)[-_]') trainno,
      EXTRACT(HOUR FROM DATETIME(COALESCE(g.arrival_time, g.departure_time), 'Europe/Oslo'))
        + EXTRACT(MINUTE FROM DATETIME(COALESCE(g.arrival_time, g.departure_time), 'Europe/Oslo'))/60.0 h
    FROM `ent-data-sharing-ext-prd.timetable_gtfs.gtfs_last_recorded_ent_v1` g
    JOIN `disruptions.togvarsel_oslo_stations` s ON s.stop_place = g.parent_station
    -- spelled out: the table requires a partition filter and a subquery does not count as one
    WHERE g.operating_date IN (today, DATE_SUB(today, INTERVAL 1 WEEK), DATE_SUB(today, INTERVAL 2 WEEK),
                               DATE_SUB(today, INTERVAL 3 WEEK), DATE_SUB(today, INTERVAL 4 WEEK),
                               prevwd, DATE_SUB(prevwd, INTERVAL 1 WEEK), DATE_SUB(prevwd, INTERVAL 2 WEEK),
                               DATE_SUB(prevwd, INTERVAL 3 WEEK), DATE_SUB(prevwd, INTERVAL 4 WEEK))
      AND g.agency_id LIKE 'VYG%' AND g.route_type = 'Train' AND g.route_short_name != 'RE20'
  ),
  trips AS (SELECT DISTINCT d, line, trainno FROM g WHERE trainno IS NOT NULL),
  standing AS (
    SELECT ref, p.line, p.trainno, MAX(p.d) last_seen
    FROM UNNEST([today, prevwd]) ref
    JOIN trips p ON p.d IN (DATE_SUB(ref, INTERVAL 1 WEEK), DATE_SUB(ref, INTERVAL 2 WEEK), DATE_SUB(ref, INTERVAL 3 WEEK), DATE_SUB(ref, INTERVAL 4 WEEK))
    GROUP BY 1,2,3 HAVING COUNT(*) >= 3
  ),
  missing AS (
    SELECT s.ref, s.line, s.trainno, s.last_seen
    FROM standing s LEFT JOIN trips t ON t.d = s.ref AND t.line = s.line AND t.trainno = s.trainno
    WHERE t.trainno IS NULL
  ),
  miss_today AS (
    -- a LEFT JOIN, not EXISTS: BigQuery refuses the correlated form inside this script
    SELECT c.h, (q.trainno IS NOT NULL) persisted
    FROM missing m
    JOIN g c ON c.d = m.last_seen AND c.line = m.line AND c.trainno = m.trainno
    LEFT JOIN missing q ON q.ref = prevwd AND q.line = m.line AND q.trainno = m.trainno
    WHERE m.ref = today
  ),
  added AS (
    SELECT c.h FROM trips t
    LEFT JOIN standing s ON s.ref = today AND s.line = t.line AND s.trainno = t.trainno
    JOIN g c ON c.d = t.d AND c.line = t.line AND c.trainno = t.trainno
    WHERE t.d = today AND s.trainno IS NULL
  )
  SELECT
    GREATEST((SELECT COUNTIF(h>=6.5 AND h<9) FROM miss_today) - (SELECT COUNTIF(h>=6.5 AND h<9) FROM added), 0) cut_am,
    GREATEST((SELECT COUNTIF(NOT persisted AND h>=6.5 AND h<9) FROM miss_today) - (SELECT COUNTIF(h>=6.5 AND h<9) FROM added), 0) cut_am_new,
    GREATEST((SELECT COUNTIF(h>=7 AND h<9) FROM miss_today) - (SELECT COUNTIF(h>=7 AND h<9) FROM added), 0) cut_amrest,
    GREATEST((SELECT COUNTIF(NOT persisted AND h>=7 AND h<9) FROM miss_today) - (SELECT COUNTIF(h>=7 AND h<9) FROM added), 0) cut_amrest_new,
    GREATEST((SELECT COUNTIF(h>=15 AND h<18) FROM miss_today) - (SELECT COUNTIF(h>=15 AND h<18) FROM added), 0) cut_pm,
    GREATEST((SELECT COUNTIF(NOT persisted AND h>=15 AND h<18) FROM miss_today) - (SELECT COUNTIF(h>=15 AND h<18) FROM added), 0) cut_pm_new
),
cal AS (
  -- Today's row from the shared calendar (data/queries/togvarsel/00_calendar.sql, copied to prd
  -- by copy_runtime_to_prd.sh). Zero rows means the calendar has EXPIRED: fail loudly rather
  -- than silently forecasting on an unmaintained holiday.
  SELECT
    IF(COUNT(*) = 0,
       ERROR('disruptions.togvarsel_calendar dekker ikke dagens dato — utvid data/queries/togvarsel/00_calendar.sql og kjør copy_runtime_to_prd.sh'),
       LOGICAL_OR(holiday)) AS holiday,
    LOGICAL_OR(low_season) AS low_season
  FROM `disruptions.togvarsel_calendar`
  WHERE d = CURRENT_DATE('Europe/Oslo')
)
SELECT
  CURRENT_DATE('Europe/Oslo') AS d,
  CURRENT_TIMESTAMP() AS computed_at,
  cal.holiday,
  cal.low_season,
  IF(EXTRACT(DAYOFWEEK FROM CURRENT_DATE('Europe/Oslo'))=2,1,0) dow_2,
  IF(EXTRACT(DAYOFWEEK FROM CURRENT_DATE('Europe/Oslo'))=3,1,0) dow_3,
  IF(EXTRACT(DAYOFWEEK FROM CURRENT_DATE('Europe/Oslo'))=4,1,0) dow_4,
  IF(EXTRACT(DAYOFWEEK FROM CURRENT_DATE('Europe/Oslo'))=5,1,0) dow_5,
  IF(EXTRACT(DAYOFWEEK FROM CURRENT_DATE('Europe/Oslo'))=6,1,0) dow_6,
  et.*, prev.*, sx.*, cats.*, cut.*
FROM et CROSS JOIN prev CROSS JOIN sx CROSS JOIN cats CROSS JOIN cal CROSS JOIN cut;

-- ---------------------------------------------------------------------------- today's SX list
-- One row per situation belonging to today's forecast: the deterministic "kjente avvik" list
-- and the raw material for the forecast's reason text.
CREATE OR REPLACE TABLE `disruptions.togvarsel_sx_today` AS
SELECT
  sn, ANY_VALUE(participantRef) AS participant, ANY_VALUE(created_local) AS created_local,
  ANY_VALUE(fh) AS fh, ANY_VALUE(dur) AS dur, ANY_VALUE(planned) AS planned,
  ANY_VALUE(cls) AS cls, ANY_VALUE(cause) AS cause, ANY_VALUE(cat) AS cat,
  ANY_VALUE(onset_summary) AS summary, ANY_VALUE(onset_description) AS description,
  LOGICAL_OR(oslo) AS oslo,
  COUNT(DISTINCT IF(oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 6.5 AND 9, j_ref, NULL)) AS n_amj,
  COUNT(DISTINCT IF(oslo AND operating_date=CURRENT_DATE('Europe/Oslo') AND amh BETWEEN 15 AND 18, j_ref, NULL)) AS n_pmj
FROM _tv_sx
GROUP BY sn;
