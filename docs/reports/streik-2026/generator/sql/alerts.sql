-- Every push about a train on operating day @DAY: first push per registration x situation x departure.
WITH d AS (
  SELECT * FROM `ent-kixavvik-prd.disruptions.varsel_decisions`
  WHERE call_date = DATE '@DAY' AND DATE(decided_at) BETWEEN DATE_SUB(DATE '@DAY', INTERVAL 1 DAY) AND DATE_ADD(DATE '@DAY', INTERVAL 1 DAY)
    AND pushed
),
first_push AS (
  SELECT journey_digest, situation_number, departure_time, line, jkey,
         MIN(decided_at) AS notified_at,
         ANY_VALUE(from_stop) from_stop, ANY_VALUE(to_stop) to_stop,
         ANY_VALUE(summary) summary, ANY_VALUE(description) descr,
         ARRAY_AGG(STRUCT(message, message_source, n_devices, n_subscriptions, channel, et_cancelled)
                   ORDER BY decided_at LIMIT 1)[OFFSET(0)] m
  FROM d GROUP BY 1,2,3,4,5
),
nsr AS (
  SELECT id, ANY_VALUE(name) name
  FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version` GROUP BY id
)
SELECT f.line, f.jkey, f.situation_number,
       COALESCE(a.name, f.from_stop) AS fra, COALESCE(b.name, f.to_stop) AS til,
       FORMAT_TIMESTAMP('%H:%M', f.departure_time, 'Europe/Oslo') AS avgang,
       FORMAT_TIMESTAMP('%H:%M:%S', f.notified_at, 'Europe/Oslo') AS varslet,
       CAST(ROUND(TIMESTAMP_DIFF(f.departure_time, f.notified_at, SECOND)/60) AS INT64) AS lead_min,
       f.m.n_devices, f.m.n_subscriptions, f.m.channel, f.m.message_source, f.m.et_cancelled,
       f.summary, f.descr, f.m.message
FROM first_push f LEFT JOIN nsr a ON a.id=f.from_stop LEFT JOIN nsr b ON b.id=f.to_stop
ORDER BY f.departure_time, fra
