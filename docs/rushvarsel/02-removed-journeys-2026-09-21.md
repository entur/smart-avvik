# When Vy removes a train instead of cancelling it (2026-09-21)

Question from the product owner, 2026-09-21: Vy sometimes removes journeys from the timetable
instead of cancelling them. Was that the case for L1 and L2 today and on Friday 18 September,
and how can the models and the notifications (Rushvarsel and the journey alerts on
`/notification-api`) take it into account? From a passenger's point of view a removed train is a
cancelled train.

Everything below is measured against `realtime_siri_et_last_recorded` (the daily-batch history
the training chain reads), the live `siri_realtime_et_production` partitions, the GTFS timetable
(`timetable_gtfs.gtfs_last_recorded_ent_v1`) and `disruptions.disruptions` (Vy's SIRI-SX).
Cost of the investigation: 100 jobs, 156 GB, **$0.97**.

## Short answer

- **Friday 18 September: no.** Both lines ran their full weekday set (L1 152, L2 153 journeys, the
  same as GTFS). The afternoon's trouble on L2 was a strike, and every affected train was
  cancelled properly in both SX («Innstilt Høvik–Ski. Dette skyldes streik», 15:37) and ET
  (504 stop-cancelled calls on L1, 150 on L2). The models saw it.
- **Today, Monday 21 September: yes, in a different form.** The GTFS plan for today (version
  2026-09-20, published the evening before) carries **125 L1 journeys against the standing 152**.
  Ten of the 46 dropped departures were also published to ET as cancelled (yesterday morning,
  stop-level, never re-sent today). The other **36 daytime and evening L1 departures were never
  published to ET at all**, eight of them in the PM rush (14:10, 14:16, 14:40, 15:25, 15:46,
  16:16, 16:48, 17:55). No SX message mentions a reduced service. L2 lost 14 late-evening
  departures and gained 6 retimed ones, which looks like a timetable change rather than a cut.
- **Friday 11 September is the clean case you describe.** 27 afternoon journeys (11 L1 between
  13:46 and 15:48, 16 L2 between 14:24 and 16:24) were published the day before and then, at
  **15:04 Oslo**, the feed simply stopped updating them. None was ever cancelled. The 12 that had
  not departed vanished from the history table; the 15 that were under way stayed, with their
  unrun tail filled in with the last *estimated* times. Vy's SX that afternoon named individual
  trains («Innstilt Asker–Lillestrøm … mangler et togsett», 13:16) but nothing about the
  removal itself. If what you remember seeing was on a Friday, this is probably the day.

## The three shapes, and what each is invisible to

| shape | example | in `last_recorded` | in today's ET partition | in SX | who misses it |
|---|---|---|---|---|---|
| **A. Plan cut the evening before** | today: 36 L1 departures | absent | absent | no | training labels (denominator shrinks), live features, both Rushvarsel outcome passes, `/impact` (the train does not exist) |
| **B. Intraday pull** | 11 Sept 15:04: 27 journeys | undeparted: absent; departed: present with an estimated tail read as punctual | present, stale last snapshot | no | training labels, outcome passes; `/impact` keeps serving the stale snapshot as a normal train |
| **C. Cancelled the day before, not re-sent** | today: 9 L1 morning trains | present, cancelled | **absent** (only in yesterday's partition) | per-train «Innstilt» | `rail_calls_today`, `rail_calls_planned_today` and everything on them: `/impact`, `/varsel`, `/notification-api`, the provisional Rushvarsel outcome pass |

Shape C is **our bug, not Vy's**. Both tables filter `DATE(_PARTITIONTIME) = CURRENT_DATE()`, so a
cancellation declared yesterday for a train today is not in them. Verified at 14:57 today: the
four half-cancelled L1 trains (05:04, 07:10, 07:18, 08:40 from Lillestrøm/Asker) are in neither
table while their healthy neighbours are in both. `togvarsel_features_today.sql` already reads
yesterday's partition too, so the forecast features saw them; the 06:30 model issued red (0.487)
this morning, correctly.

Two data-quality facts found on the way, both worth their own line:

- **An estimated time is not an observation.** For a journey that goes silent mid-run,
  `last_recorded` carries the last *expected* time in `arrivalTime`/`departureTime` on calls
  marked `estimated = TRUE` (L1 2140 on 11 Sept: Bryn to Lillestrøm, recorded 15:04:21, times
  8-9 min "late", never run). `03_day_windows.sql` defines `has_actual` from the time columns
  alone, so those calls count as resolved and, at 0-9 min, as punctual. Over the training window
  this is 14 807 Oslo-area calls (0.4 %), concentrated on bad days: 1 641 on 2025-11-12, 1 457 on
  2025-11-05, 943 on 2025-06-17, 235 on 2026-09-11.
- **The bare `L1` line ref.** The journeys pulled mid-run on 11 Sept sit in `last_recorded` with
  `lineRef = 'L1'` rather than `'VYG:Line:L1'` (7 L1 and 5 L2 journeys). Any `lineRef IN (…)`
  filter misses them; the chain's `REGEXP_REPLACE(lineRef, '^VYG:Line:', '')` is fine.

## What it did to Rushvarsel

**11 September, afternoon.** Issued 12:00 yellow (0.205), 14:00 yellow (0.240). Recorded outcome
`pm` = 0.085, green, over 1 037 resolved calls, so the day reads as two unjustified warnings. The
27 removed journeys had 408 planned calls at the 39 Oslo-area stations between 15:04 and 18:00
(GTFS). Counting them as cancelled gives roughly (88 + 408) / (1 037 + 408) ≈ **0.34, red**. That
is an estimate that mixes planned with observed calls, but the direction is not in doubt: the
yellow was right and the page says it was wrong.

**11 September, morning.** Also in the numbers: `am` resolved 1 217 calls; today's provisional
`am` resolved 1 072 with trouble 0.037, green, against a red forecast. The 145 missing calls are
the nine cancelled and three removed morning trains (shape C and A), which are exactly the
troubled ones. Counted, the morning is around 0.15, yellow. **Today's PM outcome will read the
same way**: computed on a denominator missing eight PM-rush L1 departures, it will land low
against the 14:00 yellow. That is checkable on /rushhistorikk tomorrow.

**Label bias in training.** Shape A and the undeparted half of shape B shrink the denominator
and remove precisely the calls that would have been troubled; the departed half of B adds
phantom punctual calls. All three bias `am_t10`/`pm_t10` *down* on bad days, which is the tail
the models are trained to find. How often: a same-weekday journey-count baseline over the
training window (`disruptions.exp_rush_missing_days`, dev) shows ≥ 5 journeys missing on 132 of
360 eligible weekdays, but that figure is dominated by planned engineering weeks (L1 absent for
a fortnight in April and October 2025, L2 in March 2026) and cannot separate a plan cut from
track work without a planned-work calendar. The GTFS-versus-ET diff by train number (below)
over July-September 2026 is much quieter: a baseline of ~5 unmatched trains a day, then
**2026-08-12 (L1: 58 missing)**, 2026-09-11 (L1 4 + L2 2 undeparted) and 2026-09-20 (L1 19).

## How to incorporate it — ranked by certainty and cost

1. **Read two ET partitions in `rail_calls_today.sql` and `rail_calls_planned_today.sql`**
   (`DATE(_PARTITIONTIME) BETWEEN CURRENT_DATE() - 1 AND CURRENT_DATE()`, keep the aimed-date
   filter). Fixes shape C for `/impact`, `/varsel`, `/notification-api` and the provisional
   Rushvarsel pass. No policy question. Cost: roughly doubles the rail scan, about +$1.2 a day
   per environment at the 60-minute TTL. `togvarsel_features_today.sql` is the precedent.
2. **`has_actual` must require `NOT estimated`** in `03_day_windows.sql` and in
   `togvarsel_outcomes_settled.sql`. Phantom tails become unresolved instead of punctual. One
   line each, then a retrain. Does not by itself count them as trouble; that is point 4.
3. **Staleness → synthetic cancellation for the journey alerts** (shape B, the departed half).
   `rail_calls_planned_today` needs the journey's `recordedAtTime`; `predict_calls.sql` gets a
   branch beside `et_only`: a journey not cancelled, whose last snapshot is older than N minutes
   while other journeys on the same line have fresh ones, and whose next aimed call is more than
   M minutes past, is served as `et_declared_cancelled` from that call on, under
   `KIX:ETRemoved:<jkey>` so `/historikk` can segment it like `KIX:ETCancellation` and
   `KIX:NoSituation`. Wording «Innstilt», per your premise. Before shipping, replay the rule over
   a few normal days' partitions: the last calls of every journey look stale at the end of its
   run, and N, M have to be set where that does not fire. On 11 Sept it would have fired ~15:15,
   forty minutes before the first affected traveller's train was due.
4. **GTFS plan versus ET, by train number, in the label chain and both outcome passes** (shape
   B, the undeparted half; also any journey the plan had that ET never published). GTFS
   `trip_id` and the ET `DatedServiceJourney` share the train number (152 of 152 L1 matched on a
   normal Monday); GTFS history reaches back to 2023 at 0.28 GB a day, so the whole training
   window is ~150 GB, about $1. Planned calls of an unmatched trip at Oslo-area stations become
   cancelled calls. It is immune to engineering weeks because GTFS already carries the reduced
   plan. Exactly for that reason **it cannot see shape A**.
5. **Shape A is a product decision before it is a feature.** A plan cut published the evening
   before is not in GTFS-versus-ET, not in SX and not in ET. The only baseline is the standing
   timetable (same-weekday median of planned trips, or the previous GTFS version), and that
   baseline is confounded with planned closures. The question for the product owner: does an
   announced 30 % reduction of L1 count as a bad rush? If yes, the cleanest signal is a per-line
   «redusert rutetilbud i dag: 36 færre avganger på L1» reason on the Rushvarsel card and a
   line-level notice on the `/varsel` cards, not 36 synthetic per-train cancellations for trains
   the journey planner no longer shows either.
6. **Report upstream.** SIRI-ET has `Cancellation`; pulling 27 journeys without it (Vy, 11 Sept
   15:04) and filling estimated tails into the observed-time columns (anshar's `last_recorded`)
   are both data-quality defects for the realtime team and Vy, not things a consumer should
   have to infer. Nothing here replaces that report.

Points 1 and 2 are safe to do now. Points 3 and 4 are a few days each, and 4 is the one that
moves the training labels. Point 5 is the one that needs an answer from you.

## What happened next

Points 2 and 4 were tried on the labels the same afternoon: see
[03-labels](03-labels-2026-09-21.md). 15 of 358 eligible days flip from green to bad, the PM
models gain one to two AUC points on retraining (not significant), the AM models nothing.

**Point 1 is done for rail, in the working tree** (`disruptions/api/rail_calls_planned_today.sql`,
uncommitted): the snapshot CTE reads yesterday's and today's partition, and a journey is kept if
one of its calls is aimed today or later (Oslo). Measured against the table the app was serving
from at 16:00: 1 088 → 1 427 journeys, none lost. Of the 339 gained, 264 were trains due
**later today** with no snapshot since yesterday's publication, 49 were running or finished on a
stale snapshot, 14 cancelled in advance, 6 partly cancelled (the L1 morning trains above) and 6
past and never recorded. So the today-only filter was hiding a quarter of the day's trains from
`/impact` and the pull read, not only the pre-cancelled ones. Dry run 14.1 → 22.2 GB per
rebuild. `rail_calls_today.sql` was left alone: it reads recorded calls only, so a train
cancelled before it ran was never in it under either filter, and the provisional outcome pass
takes its universe from the planned table. `bus_calls_planned_today.sql` has the same shape and
was not measured.

## Where everything is

- Scratch queries and exports: `hist_l1l2.sql`, `hist_j.sql`, `live_j.sql`, `live_0911.sql`,
  `gtfs.sql`, `gtfs_diff.sql`, `removed_pm.sql`, `prev.sql`, `sx.sql` and their CSVs in the
  session scratchpad (not in the repo).
- `disruptions.exp_rush_missing_days` (dev): per-day journeys missing against the same-weekday
  median, 2025-04-10 to 2026-09-20, for the 15 Vy Oslo lines. Scratch; drop when done.
- Nothing in the code was changed.
