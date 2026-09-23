-- Every Vy SX situation 17-23 Sept whose text mentions a strike or a reduced service. The strike
-- ones tag alerts as «SX (streik)»; the rest is the check that no reduced-service notice exists.
SELECT situationNumber, participantRef, MIN(TIMESTAMP(versionedAtTime)) first_v,
  ANY_VALUE((SELECT s.value FROM UNNEST(summaries) s WHERE s.language IS NULL OR UPPER(s.language)='NO' LIMIT 1)) summary,
  ANY_VALUE((SELECT d.value FROM UNNEST(descriptions) d WHERE d.language IS NULL OR UPPER(d.language)='NO' LIMIT 1)) descr,
  ANY_VALUE(ARRAY_LENGTH(affects.vehicleJourneys)) n_vj, ANY_VALUE(ARRAY_LENGTH(affects.networks)) n_net, ANY_VALUE(ARRAY_LENGTH(affects.stopPlaces)) n_sp
FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`
WHERE _PARTITIONTIME BETWEEN TIMESTAMP '2026-09-17' AND TIMESTAMP '2026-09-23' AND participantRef IN ('NSB', 'VYG')
  AND REGEXP_CONTAINS(LOWER(IFNULL((SELECT d.value FROM UNNEST(descriptions) d WHERE d.language IS NULL OR UPPER(d.language)='NO' LIMIT 1),''))
       || ' ' || LOWER(IFNULL((SELECT s.value FROM UNNEST(summaries) s WHERE s.language IS NULL OR UPPER(s.language)='NO' LIMIT 1),'')),
       r'redusert|færre avganger|færre tog|rutetabell|ruteplan|tilbud|endret rute|kjører ikke|halvtimes|timesavgang|streik')
GROUP BY 1,2 ORDER BY first_v
