-- /meldinger-api/filters: the values the page's dropdowns can actually be set to.
--
-- One row, five arrays: the operators, lines, boarding places, alighting places and SIRI-SX
-- summaries that occur on at least one decision about a departure INSIDE the window. Everything
-- except the last is historikk_filters.sql on the `departure_time` axis; see its header for why the
-- page needs this as its own query rather than deriving the options from the decisions it renders
-- (that list is capped and ordered by departure, so a line that only ran early in the window would
-- silently drop out of its own filter, and once a filter were applied the options would shrink to
-- the selection with no way back). `sx_summaries` is this surface's own and has no /historikk twin.
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
-- WHAT THE `summary` COLUMN COSTS, measured by dry run against dev on 2026-08-23 over a whole-day
-- window: 2.97 MB -> 3.52 MB here, a flat +557 KB, which is the column across the three partitions.
-- The same +557 KB lands on meldinger_summary.sql (7.71 -> 8.26 MB) and meldinger_messages.sql
-- (23.38 -> 23.94 MB), and NOTHING on meldinger_decisions.sql (34.38 MB either way), which already
-- selected it. So ~1.7 MB added per page load, against a 120 s response cache that every filter
-- combination on the window shares. Note this is one of the few cost figures on this surface a dry
-- run can actually establish -- the DATE(decided_at) and call_date bounds cannot be checked this
-- way, because narrowing them wrongly reports as a CHEAPER query.
--
-- Deliberately NOT narrowed by the other filters: options derived from the current selection shrink
-- to it, and there is then no way back to the rest of the window.
--
-- ONE OF THE FIVE VOCABULARIES IS TRUNCATED, and only one: `sx_summaries` is capped at the 30 most
-- common SIRI-SX summaries. See the comment on that array for what the cap costs.
WITH universe AS (
  SELECT operator, line, from_stop, to_stop, summary, situation_number
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
),
-- One row per stored summary, with the fact that decides which OPTION it belongs to. Its own CTE
-- rather than an inline subquery: referenced from the GROUP BY of a query that selects from it,
-- BigQuery resolves `no_sx` to the LOGICAL_OR above rather than to this CTE's output column and
-- refuses the query ("Column no_sx contains an aggregation function, which is not allowed in
-- GROUP BY"). Naming the step makes the reference unambiguous.
sx_rows AS (
  SELECT
    summary,
    -- Is this text ONLY ever written on rows no operator published anything about? LOGICAL_OR
    -- rather than an equality, so a text an operator also happens to publish is not quietly
    -- reclassified as ours.
    LOGICAL_OR(STARTS_WITH(situation_number, 'KIX:NoSituation:')) AS row_no_sx,
    COUNT(*) AS n
  FROM universe
  -- TRIM, so a summary of nothing but whitespace is excluded too: it would otherwise be an
  -- unreadable blank row in the dropdown that filters to a population nobody can identify.
  WHERE summary IS NOT NULL AND TRIM(summary) != ''
  GROUP BY summary
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
  ) AS to_places,
  -- The SIRI-SX summaries the window's decisions carry, as OPTIONS rather than raw strings: one
  -- entry per thing a reader can pick, MOST COMMON FIRST, capped at 30. Same shape and same reason
  -- as from_places/to_places above -- an option nominates one stored value as its identity and
  -- carries every value it stands for, and the API expands the nomination back to all of them when
  -- filtering. It is `_place_groups`/`_place_refs` over summaries instead of stop refs.
  --
  -- WHAT GETS COLLAPSED, AND WHY IT IS NOT A TEXT MATCH. predict_calls.sql mints
  -- `KIX:NoSituation:<jkey>` for a call NO operator published anything about, and writes a
  -- placeholder summary on it. Those rows are not SIRI-SX at all, so under a control labelled
  -- SIRI-SX they belong together as one option ("no message") rather than as one row per
  -- placeholder wording. And there IS more than one wording: the text changed on 2026-08-20
  -- (ff67e58) about thirteen hours after the layer shipped, and `varsel_decisions` keeps what it
  -- recorded -- 60 288 rows carry the current text and 5 055 the original, measured in dev on
  -- 2026-08-23. Grouping on the situationNumber NAMESPACE rather than on the placeholder strings is
  -- what makes this survive the next such edit: a third wording joins the same option for free,
  -- where a hard-coded pair would silently start showing it as though an operator wrote it.
  --
  -- The API labels this option "Ingen"; the SQL deliberately does not, because `no_sx` is a fact
  -- about the data and the wording is a UI decision (format.ts holds every other such label).
  --
  -- THE CAP COUNTS OPTIONS, not stored values, which is why the grouping happens here and not in
  -- the API: capping raw strings first would spend two of the thirty slots on one option. It is
  -- still the only TRUNCATED vocabulary here -- a summary outside the top 30 sits on cards the page
  -- shows and is a 400 if asked for, which is the price of allow-listing free-form text on a public
  -- endpoint.
  --
  -- The nomination is `ARRAY_AGG(... ORDER BY n DESC, summary LIMIT 1)`, never ANY_VALUE: it is the
  -- busiest member with ties broken on the text, so it CANNOT flip between two refreshes of the
  -- same window and invalidate the selection the page is holding. Same rule, and same reason, as
  -- the busiest-ref nomination on the places above. The ORDER BY tie-break exists for the same
  -- reason one slot down: ranks 30 and 31 routinely tie on count.
  ARRAY(
    SELECT AS STRUCT
      ARRAY_AGG(summary ORDER BY n DESC, summary LIMIT 1)[OFFSET(0)] AS value,
      ARRAY_AGG(summary ORDER BY summary)                            AS members,
      -- The grouping key itself, not an aggregate over it -- every member of the group shares it by
      -- construction. The CTE column is `row_no_sx` rather than `no_sx` because an output alias of
      -- the same name shadows the input column in GROUP BY, and BigQuery then resolves the key to
      -- the aggregate and refuses the query.
      row_no_sx                                                      AS no_sx
    FROM sx_rows
    -- The collapse: every no-SX placeholder shares one group (second key NULL), while every other
    -- text stays its own.
    GROUP BY row_no_sx, IF(row_no_sx, NULL, summary)
    ORDER BY SUM(n) DESC, ARRAY_AGG(summary ORDER BY n DESC, summary LIMIT 1)[OFFSET(0)]
    LIMIT 30
  ) AS sx_summaries
