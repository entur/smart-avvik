-- Does any planned trip today run from one place to another, and does any run the other way —
-- asked about MANY stop pairs in one job.
--
-- One question per pair used to be one BigQuery job per pair. That is affordable when a job is a
-- traveller registering a journey and unaffordable as a sweep: the servability re-check is a
-- per-record chore (`notification_servability`, `varsel.recheck_service`), so at a pilot's scale it
-- was bounded to four records per cycle by the cost of asking, and four per ~296 s cycle is ~1 200
-- records a day against a population meant to reach 100 000. Batched, the sweep's BigQuery cost
-- stops scaling with the number of records at all: one job answers every pending pair.
--
-- It is asked when a journey is registered because varsel can only ever alert on a DIRECT trip
-- (`to_stop` narrowing keeps only trains that reach the alighting stop), so a pair no single trip
-- connects is a registration that cannot produce an alert however bad the day gets. Answering "no"
-- out loud is the point: the failure it replaces was a card reading "ingen aktive avvik akkurat nå"
-- for the rest of the journey's life.
--
-- MIN and MAX per side rather than a plain join on stop_order: a circular route calls at the same
-- stop twice, and "is there a boarding call before an alighting call" is then a question about the
-- earliest boarding and the latest alighting — with the mirror image for the other direction.
--
-- A missing `boarding_allowed` column (a planned table the poller has not rebuilt since 2026-09-01)
-- makes this query error, which `main._varsel_connectivity_batch` already turns into "could not
-- establish" for every pair in the job — no warning, registration proceeds. That is the right
-- failure and it self-heals on the next rebuild; the alternative, guessing the column away, would
-- be a silent wrong answer.
--
-- @pairs  ARRAY<STRUCT<pair_id INT64, from_refs ARRAY<STRING>, to_refs ARRAY<STRING>>>
--
-- `pair_id` is the caller's index into its own list and is never stored: the answer is about a stop
-- pair, and the caller fans it back out to every registration on that pair.
WITH pairs AS (
    SELECT * FROM UNNEST(@pairs)
),
-- Flattened to (pair_id, ref) so every join below is an EQUALITY on `stop_place`. The obvious
-- alternative — joining `calls` to `pairs` on `stop_place IN UNNEST(p.from_refs)` — is a cross
-- join with a filter, so its cost is pairs x calls rather than a hash join's.
from_refs AS (
    SELECT pair_id, ref FROM pairs, UNNEST(from_refs) AS ref
),
to_refs AS (
    SELECT pair_id, ref FROM pairs, UNNEST(to_refs) AS ref
),
-- Every ref any pair named, so the two scans below read only the stops this job is about. Both
-- tables are partitioned by nothing useful here, so this predicate is what keeps a 2 000-pair job
-- the same cost as a one-pair one.
wanted AS (
    SELECT ref FROM from_refs
    UNION DISTINCT
    SELECT ref FROM to_refs
),
calls AS (
    SELECT jkey, stop_place, stop_order, boarding_allowed, alighting_allowed
    FROM `disruptions.rail_calls_planned_today`
    WHERE stop_place IN (SELECT ref FROM wanted)
    UNION ALL
    SELECT jkey, stop_place, stop_order, boarding_allowed, alighting_allowed
    FROM `disruptions.bus_calls_planned_today`
    WHERE stop_place IN (SELECT ref FROM wanted)
),
-- Boarding and alighting, not merely "calls here": a trip nobody may get on or off is not service,
-- and the question has to be asked in exactly the shape `impact._trip_cells` matches in — otherwise
-- the caveat and the alerting disagree, and a pair served only by boarding-only long-distance
-- trains is told service exists and then never hears about one of them.
--
-- FOUR groupings, NOT TWO, because the two directions need opposite ends of the same restriction:
-- travelling to->from, the traveller BOARDS at the to-place and ALIGHTS at the from-place. One pair
-- of "calls at from" / "calls at to" sets filtered by one activity each would answer the reverse
-- direction with the forward direction's permissions.
board_from AS (
    SELECT f.pair_id, c.jkey, MIN(c.stop_order) AS ord
    FROM from_refs AS f JOIN calls AS c ON c.stop_place = f.ref
    WHERE c.boarding_allowed GROUP BY 1, 2
),
alight_to AS (
    SELECT t.pair_id, c.jkey, MAX(c.stop_order) AS ord
    FROM to_refs AS t JOIN calls AS c ON c.stop_place = t.ref
    WHERE c.alighting_allowed GROUP BY 1, 2
),
board_to AS (
    SELECT t.pair_id, c.jkey, MIN(c.stop_order) AS ord
    FROM to_refs AS t JOIN calls AS c ON c.stop_place = t.ref
    WHERE c.boarding_allowed GROUP BY 1, 2
),
alight_from AS (
    SELECT f.pair_id, c.jkey, MAX(c.stop_order) AS ord
    FROM from_refs AS f JOIN calls AS c ON c.stop_place = f.ref
    WHERE c.alighting_allowed GROUP BY 1, 2
),
-- Joined on (pair_id, jkey): one trip has to board and alight for the SAME pair, and the
-- `pair_id` term is what stops one pair's boarding call pairing with another pair's alighting one.
forward AS (
    SELECT DISTINCT b.pair_id
    FROM board_from AS b JOIN alight_to AS a USING (pair_id, jkey)
    WHERE b.ord < a.ord
),
reverse AS (
    SELECT DISTINCT b.pair_id
    FROM board_to AS b JOIN alight_from AS a USING (pair_id, jkey)
    WHERE b.ord < a.ord
)
-- LEFT JOINed from `pairs`, so a pair no trip serves — and a pair whose refs appear in neither
-- table — still gets a row. A caller that saw no row for a pair reads it as "could not establish"
-- rather than "no service", which cannot happen here and is the safe way round if it ever does.
SELECT
    p.pair_id,
    f.pair_id IS NOT NULL AS forward,
    r.pair_id IS NOT NULL AS reverse
FROM pairs AS p
LEFT JOIN forward AS f ON f.pair_id = p.pair_id
LEFT JOIN reverse AS r ON r.pair_id = p.pair_id
