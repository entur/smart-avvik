-- /historikk-api/filters: the values the page's dropdowns can actually be set to.
--
-- One row, four arrays: the operators, lines, boarding places and alighting places that occur on at
-- least one decision in the window. The page needs this as its own query rather than deriving the
-- options from the decisions it renders, because that list is capped (LIMIT @limit, 200 from the
-- UI) and ordered by departure — so a line that only ran early in the window would silently drop
-- out of its own filter. It is also what bounds the API's validation: a value not in these arrays
-- is rejected rather than turned into a cache entry and a BigQuery query of its own, the same
-- reasoning historikk._checked_operator already applies to the codespace.
--
-- UNION ALL of the same two sources /historikk-api/decisions reads, and for the same reason: a
-- decision is visible on the page long before the nightly job can label it, so options taken from
-- the labelled table alone would not offer today's trains at all. Duplicates across the two are
-- harmless here — every consumer below is a SELECT DISTINCT.
--
-- PRIVACY: from_stop/to_stop are the traveller's own registered endpoints, which this API withheld
-- entirely until the filters were added. They are still NOT served per decision (see the Decision
-- model and the header of historikk_decisions.sql) — this is the one place they surface, as the
-- vocabulary a filter needs, without being attached to any particular row.
--
-- Cost: the outcomes side prunes on its call_date partition; the groups side does not, exactly as
-- in historikk_decisions.sql — the view aggregates before a filter can reach the partitioned table
-- underneath. So this is one more full scan of a small table per window per cache TTL. Same
-- acceptability argument, same escape hatch if the log grows: bound the view on decided_at.
WITH universe AS (
  SELECT operator, line, from_stop, to_stop
  FROM `disruptions.varsel_decision_outcomes`
  WHERE call_date BETWEEN @from_date AND @to_date

  UNION ALL

  SELECT operator, line, from_stop, to_stop
  FROM `disruptions.varsel_decision_groups`
  WHERE call_date BETWEEN @from_date AND @to_date
)
SELECT
  ARRAY(SELECT DISTINCT operator  FROM universe WHERE operator  IS NOT NULL ORDER BY operator)  AS operators,
  ARRAY(SELECT DISTINCT line      FROM universe WHERE line      IS NOT NULL ORDER BY line)      AS lines,
  -- Refs, not names: the ref is what the decision log stores and what the filter predicate compares
  -- against, so it is the stable identity. The API resolves each to its registry name for display
  -- (historikk._stop_names), and falls back to the bare ref when the registry cannot place it —
  -- which is also what happens in every environment where the stop list has not loaded yet.
  ARRAY(SELECT DISTINCT from_stop FROM universe WHERE from_stop IS NOT NULL ORDER BY from_stop)  AS from_places,
  ARRAY(SELECT DISTINCT to_stop   FROM universe WHERE to_stop   IS NOT NULL ORDER BY to_stop)    AS to_places
