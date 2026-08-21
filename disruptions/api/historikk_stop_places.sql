-- NSR ref -> (name, physical place), for the /historikk-api/filters dropdowns.
--
-- Read straight from the National Stop Register rather than from main's picker cache
-- (`refresh_varsel_stops`), which is what this used to do and which was wrong in two ways at once.
-- That cache is built from TODAY's planned calls, so a stop the timetable does not serve today has
-- no entry — and, worse, it deliberately COLLAPSES co-located refs and keeps only the busiest, so
-- the absorbed ref is dropped from the list entirely. Measured on dev 2026-08-21: Asker stasjon is
-- NSR:StopPlace:418 (rail side, 264 calls) and NSR:StopPlace:3247 (bus side, 267), the bus side won
-- the collapse, and the rail side — carrying 659 of the station's 842 decisions — came back
-- nameless and was offered as a raw ref beside it.
--
-- The registry answers both questions for every ref that has ever existed: `name`, and the
-- `parentRef` that says two refs are one physical place. Same table, same grain and same
-- allAuthenticatedUsers grant main._varsel_stop_places already reads it under.
--
-- Cost: ~3 MB, bounded further by the ref list, and cached behind the same TTL as everything else
-- here. Deliberately its OWN query rather than a join inside historikk_filters.sql: a registry that
-- cannot be read must leave the filters working on bare refs (historikk._place_registry catches and
-- returns nothing), not 503 the page.
SELECT
  id,
  ANY_VALUE(name) AS name,
  COALESCE(ANY_VALUE(parentRef.ref), id) AS place_id
FROM `ent-data-sharing-ext-prd.national_stop_registry.stop_places_last_version`
WHERE id IN UNNEST(@refs)
GROUP BY id
