-- How often does Avviksvarsel actually offer the traveller a way out?
--
-- The one question the unit tests cannot answer. Every gate in the alternative-journey plan is a
-- test over payloads the implementer wrote, so all eleven requirements can pass while the offer
-- rate in production stays at zero. This is the measurement that would notice.
--
-- Run it per environment (each records its own decisions; the tables are not copied):
--   bq --project_id=ent-smartavvik-dev query --use_legacy_sql=false < alternative_offer_rate.sql
--   bq --project_id=ent-smartavvik-prd query --use_legacy_sql=false < alternative_offer_rate.sql
-- ~30 KB scanned over a 30-day window, i.e. free.
--
-- THE DENOMINATOR IS SUBJECTS, NOT ROWS. `varsel_decisions` holds one row per
-- (journey definition x situation x departure) per refresh cycle -- deliberately, because the
-- repeats are the lead-time series -- so a subject appears ~10 times and counting rows weights
-- long-lived situations ten times heavier than short ones. `LOGICAL_OR(has_alternative)` asks the
-- question that matters to the traveller: was an alternative offered at any point while this was
-- live?
--
-- SEGMENT ON et_cancelled, never filter. A declared cancellation is the case the 2026-08-06
-- selection rule exists for -- under the old rule nothing could arrive "earlier" than a train that
-- is not running -- so blending it with delay alerts hides the only population that tests the
-- change. It is also, in dev, almost empty: 2 subjects as of 2026-08-06, which is why the headline
-- figure below cannot speak for it.
--
-- WHY THIS FILE EXISTS AT ALL: the "effectively never" baseline lived in a docstring, was quoted by
-- seven other places, and went stale within two days without anybody noticing (see
-- `decisions._alternative_fields`). A number worth citing is worth being able to re-derive.
--
-- Once this branch has deployed and `alt_status` is populated, prefer the second query below: it
-- separates "no alternative was found" from "the selection kept the traveller on their own train"
-- from "no lookup was issued at all", which `has_alternative = FALSE` collapses into one bucket.

-- ---------------------------------------------------------------------------------------------
-- 1. The offer rate, split by era and by whether ET had already declared the train cancelled.
--    Works on any version of the table -- reads only columns that predate 2026-08-06.
-- ---------------------------------------------------------------------------------------------
-- NULL IS A THIRD BUCKET, NEVER A DEFAULT. `has_alternative` has only been WRITTEN since
-- 2026-08-05 (`decisions._alternative_fields`, commit fa27085); every row before that carries NULL.
-- `LOGICAL_OR(NULL)` is NULL and `COUNTIF(NULL)` does not count, so a naive version of this query
-- renders "never instrumented" as "never offered" -- which is exactly how a 0 % baseline that had
-- never been measured came to be quoted in seven places. `n_uninstrumented` is reported beside the
-- rate rather than folded into it, and the rate's denominator excludes those rows. This is the same
-- rule `/historikk` already applies to `n_unsegmented`, and the same one `alt_status` exists to
-- enforce; it is easy to get backwards twice.
WITH subjects AS (
  SELECT
    DATE(decided_at) AS decided_on,
    journey_digest,
    situation_number,
    departure_time,
    LOGICAL_OR(has_alternative)                  AS ever_offered,   -- NULL when uninstrumented
    LOGICAL_OR(COALESCE(et_cancelled, FALSE))    AS was_cancelled,
    COUNT(*)                                     AS n_cycles
  FROM `disruptions.varsel_decisions`
  WHERE decided_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
  GROUP BY 1, 2, 3, 4
)
SELECT
  decided_on,
  was_cancelled,
  COUNT(*)                                             AS subjects,
  COUNTIF(ever_offered IS NULL)                        AS uninstrumented,
  COUNTIF(ever_offered IS NOT NULL)                    AS measurable,
  COUNTIF(ever_offered)                                AS offered,
  -- Denominator is the MEASURABLE subjects only. NULL when nothing was measurable, which is the
  -- honest answer and reads differently from 0.0.
  ROUND(100 * SAFE_DIVIDE(COUNTIF(ever_offered), COUNTIF(ever_offered IS NOT NULL)), 1)
                                                       AS pct_offered,
  COUNT(DISTINCT journey_digest)                       AS journeys,
  ROUND(AVG(n_cycles), 1)                              AS avg_cycles_per_subject
FROM subjects
GROUP BY decided_on, was_cancelled
ORDER BY decided_on, was_cancelled;

-- ---------------------------------------------------------------------------------------------
-- 2. AFTER the 2026-08-06 columns are deployed: why each subject has the outcome it has.
--    `has_alternative = FALSE` means three unlike things, and only alt_status tells them apart --
--    a lookup that found nothing, a selection that kept the traveller on their own train, and no
--    lookup at all. An offer rate without that split has no denominator across the change.
--
--    Commented out because `alt_status` does not exist until this branch deploys; running it
--    against an older table fails with "Unrecognized name: alt_status" rather than silently.
-- ---------------------------------------------------------------------------------------------
-- WITH subjects AS (
--   SELECT
--     journey_digest, situation_number, departure_time,
--     LOGICAL_OR(COALESCE(et_cancelled, FALSE))                        AS was_cancelled,
--     -- Worst-case reading per subject: "offered" if it was ever offered, else the most
--     -- informative non-offer seen. ANY_VALUE would pick an arbitrary cycle.
--     MIN(CASE alt_status WHEN 'offered'          THEN 1
--                         WHEN 'suppressed'       THEN 2
--                         WHEN 'none_found'       THEN 3
--                         WHEN 'not_worth_lookup' THEN 4 ELSE 5 END)   AS status_rank,
--     LOGICAL_OR(COALESCE(alt_arrives_before_original, FALSE))         AS ever_in_time,
--     LOGICAL_OR(COALESCE(alt_at_risk, FALSE))                         AS ever_at_risk,
--     LOGICAL_OR(COALESCE(alt_scored, FALSE))                          AS ever_scored
--   FROM `disruptions.varsel_decisions`
--   WHERE decided_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
--   GROUP BY 1, 2, 3
-- )
-- SELECT
--   was_cancelled,
--   CASE status_rank WHEN 1 THEN 'offered' WHEN 2 THEN 'suppressed'
--                    WHEN 3 THEN 'none_found' WHEN 4 THEN 'not_worth_lookup'
--                    ELSE 'unrecorded' END                             AS outcome,
--   COUNT(*)                                                           AS subjects,
--   -- Of the offers: how many still got the traveller there on time, how many we flagged as
--   -- themselves at risk, and how many the prediction cache could score at all.
--   COUNTIF(ever_in_time)                                              AS arrived_in_time,
--   COUNTIF(ever_at_risk)                                              AS offered_at_risk,
--   COUNTIF(ever_scored)                                               AS scored
-- FROM subjects
-- GROUP BY was_cancelled, outcome
-- ORDER BY was_cancelled, subjects DESC;
