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
-- harmless — every consumer below aggregates.
--
-- The two place arrays carry a COUNT per ref, and it is not decoration. Refs are grouped into
-- physical places in the API (historikk._place_groups), and the group has to nominate ONE of them
-- as the value the client sends back. That pick is "the busiest ref, ties broken on the ref itself"
-- — the same rule main.refresh_varsel_stops uses for the picker, and for the same reason: a
-- nomination that flipped between refreshes would invalidate the page's selection under it.
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
  ARRAY(SELECT DISTINCT operator FROM universe WHERE operator IS NOT NULL ORDER BY operator) AS operators,
  ARRAY(SELECT DISTINCT line     FROM universe WHERE line     IS NOT NULL ORDER BY line)     AS lines,
  -- Refs, not names: the ref is what the decision log stores and what the filter predicate compares
  -- against, so it is the stable identity. The API resolves each to its National Stop Register name
  -- for display and groups co-located refs into one option — see historikk._place_groups.
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
