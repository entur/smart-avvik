-- /meldinger-api/filters: the values the page's dropdowns can actually be set to.
--
-- One row, four arrays: the operators, lines, boarding places and alighting places that occur on at
-- least one decision about a departure INSIDE the window. historikk_filters.sql on the
-- `departure_time` axis; see its header for why the page needs this as its own query rather than
-- deriving the options from the decisions it renders (that list is capped and ordered by departure,
-- so a line that only ran early in the window would silently drop out of its own filter, and once a
-- filter were applied the options would shrink to the selection with no way back).
--
-- ALL FOUR meldinger_*.sql files move to this axis together. A filters or summary query left on
-- `call_date` would offer a vocabulary, and rates, for a different population than the cards beside
-- them -- which is worse than either being wrong on its own, because nothing on the page would look
-- inconsistent.
--
-- This is also the API's ALLOW-LIST. A value not in these arrays is rejected with a 400 rather than
-- turned into a cache entry and a BigQuery query of its own: the endpoint is public and
-- unauthenticated and the value lands in the response-cache key, which is the same reasoning
-- meldinger._checked_operator applies to the codespace, with the vocabulary read from the data
-- instead of hard-coded. It follows that the page must fetch these BEFORE the data on every window
-- change and clear a selection the narrowed window no longer contains.
--
-- PRIVACY: from_stop/to_stop are the traveller's own registered endpoints, and this is the ONE place
-- they surface -- as the vocabulary a filter needs, attached to no particular decision. No decision
-- row carries them (see meldinger_decisions.sql's header and the Decision model's extra="forbid")
-- and a listing still does not pair an origin with a destination. That concession is inherited
-- verbatim from /historikk's 2026-08-21 reversal rather than newly made here.
--
-- Reads `varsel_decisions` directly and does NOT touch the outcomes table at all, unlike
-- historikk_filters.sql, which has to UNION the two because its population is defined by the
-- operating day. Here the population is "departing in the window", which lives in one table -- so
-- this is a single three-partition scan.
--
-- Deliberately NOT narrowed by the other filters: options derived from the current selection shrink
-- to it, and there is then no way back to the rest of the window.
WITH universe AS (
  SELECT operator, line, from_stop, to_stop
  FROM `disruptions.varsel_decisions`
  -- The DERIVED pruning bound and then the exact window predicate, exactly as in the other three
  -- files. These are now two DIFFERENT columns: the partition column is a UTC `decided_at` date,
  -- while the window is an Oslo day and time range on `departure_time`, resolved to two absolute
  -- timestamps by meldinger._resolve_window. Half-open [from, to). See
  -- meldinger_decisions.sql's header for why the pruning bound reaches a day either side and why
  -- a too-narrow one fails silently and cheaply.
  WHERE DATE(decided_at) BETWEEN DATE_SUB(@day, INTERVAL 1 DAY) AND DATE_ADD(@day, INTERVAL 1 DAY)
    AND departure_time >= @from_ts
    AND departure_time <  @to_ts
)
SELECT
  ARRAY(SELECT DISTINCT operator FROM universe WHERE operator IS NOT NULL ORDER BY operator)
    AS operators,
  ARRAY(SELECT DISTINCT line FROM universe WHERE line IS NOT NULL ORDER BY line)
    AS lines,
  -- Refs, not names: the ref is what the decision log stores and what the filter predicate compares
  -- against, so it is the stable identity. The API resolves each to its National Stop Register name
  -- for display and groups co-located refs into one option -- see meldinger._place_groups.
  --
  -- The COUNT per ref is not decoration. Refs are grouped into physical places in the API and the
  -- group has to nominate ONE of them as the value the client sends back; that pick is "the busiest
  -- ref, ties broken on the ref itself" -- main.refresh_varsel_stops' rule, and for the same reason:
  -- a nomination that flipped between refreshes would invalidate the page's selection under it.
  ARRAY(
    SELECT AS STRUCT from_stop AS ref, COUNT(*) AS n
    FROM universe WHERE from_stop IS NOT NULL
    GROUP BY from_stop ORDER BY from_stop
  ) AS from_places,
  ARRAY(
    SELECT AS STRUCT to_stop AS ref, COUNT(*) AS n
    FROM universe WHERE to_stop IS NOT NULL
    GROUP BY to_stop ORDER BY to_stop
  ) AS to_places
