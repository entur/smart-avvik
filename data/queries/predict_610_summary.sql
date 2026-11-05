WITH
disr AS (
  SELECT
    d.situationNumber, d.summary, d.description, d.creationTime,
    SAFE.TIMESTAMP(d.creationTime)        AS onset_ts,
    IFNULL(c.category, 'UNKNOWN')         AS category,
    ARRAY_LENGTH(d.affected_stops)        AS num_affected_stops,
    ARRAY_LENGTH(d.affected_journeys)     AS num_affected_journeys,
    cd                                    AS cand_date
  FROM `disruptions.disruptions_new` AS d
  LEFT JOIN `disruptions.categories` AS c
    ON  IFNULL(d.summary, '__NULL__')     = IFNULL(c.summary, '__NULL__')
    AND IFNULL(d.description, '__NULL__') = IFNULL(c.description, '__NULL__'),
  UNNEST([DATE(SAFE.TIMESTAMP(d.creationTime)),
          DATE_ADD(DATE(SAFE.TIMESTAMP(d.creationTime)), INTERVAL 1 DAY)]) AS cd
  WHERE 'NSR:StopPlace:610' IN UNNEST(d.affected_stops)
    AND d.participantRef = 'NSB'
    AND SAFE.TIMESTAMP(d.creationTime) IS NOT NULL
),
calls AS (
  SELECT
    r.jkey, r.quay AS boarding_stop, r.lineRef, r.stop_order, r.aimed_dep,
    r.call_date, r.call_cancelled, r.dep_delay_min,
    EXTRACT(HOUR FROM r.aimed_dep) AS dep_hour,
    EXTRACT(DAYOFWEEK FROM r.aimed_dep) AS dep_dow
  FROM `disruptions.rail_calls_new` AS r
  WHERE r.stop_place = 'NSR:StopPlace:610'
    AND r.quay IN ('NSR:Quay:990', 'NSR:Quay:993')
),
cand AS (
  SELECT
    c.boarding_stop, s.category, s.num_affected_stops, s.num_affected_journeys,
    c.lineRef, c.stop_order, c.dep_hour, c.dep_dow,
    TIMESTAMP_DIFF(c.aimed_dep, s.onset_ts, MINUTE) AS minutes_since_onset,
    c.jkey, c.aimed_dep, c.call_cancelled, c.dep_delay_min,
    s.summary, s.description, s.situationNumber, s.creationTime
  FROM disr AS s
  JOIN calls AS c ON c.call_date = s.cand_date
  WHERE TIMESTAMP_DIFF(c.aimed_dep, s.onset_ts, MINUTE) BETWEEN 0 AND 720
),
pred AS (
  SELECT
    p.predicted_outcome AS predicted,
    (SELECT prob FROM UNNEST(p.predicted_outcome_probs) WHERE label = 'CANCELLED') AS p_cancelled,
    CASE
      WHEN p.call_cancelled      THEN 'CANCELLED'
      WHEN p.dep_delay_min >= 10 THEN 'DELAYED'
      ELSE 'GO'
    END AS actual
  FROM ML.PREDICT(MODEL `ent-smartavvik-dev.disruptions.trip_outcome_clf`, (SELECT * FROM cand)) AS p
)
SELECT predicted, actual, COUNT(*) AS n, ROUND(AVG(p_cancelled), 3) AS avg_p_cancelled
FROM pred
GROUP BY predicted, actual
ORDER BY predicted, actual;
