-- Strike-worded SX messages (description or summary contains «streik») about journeys dated @DATESUF.
WITH rows_ AS (
  SELECT situationNumber, TIMESTAMP(versionedAtTime) vt,
    (SELECT s.value FROM UNNEST(summaries) s WHERE s.language IS NULL OR UPPER(s.language)='NO' LIMIT 1) summary,
    (SELECT dd.value FROM UNNEST(descriptions) dd WHERE dd.language IS NULL OR UPPER(dd.language)='NO' LIMIT 1) descr, v
  FROM `ent-anshar-prd.realtime.siri_realtime_sx_production`, UNNEST(affects.vehicleJourneys) v
  WHERE _PARTITIONTIME BETWEEN TIMESTAMP '@P0' AND TIMESTAMP '@P1'
)
SELECT ref, situationNumber, MIN(vt) first_versioned,
       ANY_VALUE(summary) summary, ANY_VALUE(descr) descr, COUNT(*) n
FROM rows_, UNNEST(ARRAY_CONCAT(v.datedVehicleJourneyRefs, v.vehicleJourneyRefs,
     IF(v.framedVehicleJourneyRef.datedVehicleJourneyRef IS NULL, [], [v.framedVehicleJourneyRef.datedVehicleJourneyRef]))) ref
WHERE ref LIKE '%_@DATESUF'
  AND (REGEXP_CONTAINS(LOWER(IFNULL(descr,'')), r'streik') OR REGEXP_CONTAINS(LOWER(IFNULL(summary,'')), r'streik'))
GROUP BY 1,2 ORDER BY 3
