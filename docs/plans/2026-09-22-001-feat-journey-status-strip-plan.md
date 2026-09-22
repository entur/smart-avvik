---
title: Journey Status Strip - Plan
type: feat
date: 2026-09-22
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Journey Status Strip - Plan

Design: the «Pendleroversikt» canvas (three artboards: the `/varsel` card with the strip, the strip
through a day, the anatomy of the circles). This plan is the code behind it.

## Goal Capsule

- **Objective:** A traveller opening `/varsel` sees, on each journey card, one circle per train in
  today's registered window, left to right: filled where the outcome is known (ran on time, ran
  late, cancelled), a coloured ring where it is a forecast from the model behind
  `/notification-api`, grey where nothing can be said. The same answer is readable by the Entur app
  as a public journey-keyed endpoint.
- **Means:** A leaf module that grades every train in a window from three in-memory sources already
  built by the poller (the day's planned timetable, the per-call prediction cache, today's recorded
  calls), served twice: embedded on `/varsel-api/journeys` and as
  `GET /notification-api/v1/journey-status`. A React component on the journey card renders it.
- **Authority:** The user request and the design canvas govern scope. `CLAUDE.md` governs code, test
  and deployment conventions. The pull-read plan's R11 (no client route in routine logs) and KTD10
  (never `varsel_decisions`) bind the public read. The 2026-09-01 design review's level vocabulary
  binds the colours' names.
- **Execution profile:** Four units. U1 and U2 ship a working strip whose confirmed states lag up to
  an hour; U3 puts it on the page; U4 takes the lag to fifteen minutes. Each unit lands in dev on
  its own and is read there before the next.
- **Stop conditions:** Never run BigQuery on a request. Never write `varsel_decisions` from either
  route. Never import `notification_pull` from `varsel`, nor `varsel` from the new module. Never
  answer an empty list for a cold cache as though it meant «alt i rute». No `SHOW_RUSHVARSEL`
  coupling: this is a `/varsel` feature, not a Rushvarsel one.
- **Tail ownership:** This repository. The Entur app's BFF integrates the public read on its own.

---

## Product Contract

### Summary

Each `/varsel` journey card gains a «I dag» strip: the trains in the journey's window today, in
departure order, each a 38 px circle carrying its departure time. Colour is the level (green under
3 min, yellow 3–9 min, red 10 min or more or cancelled, grey unknown); fill says whether the level
is a fact or a forecast. A «nå» marker separates the two. The strip is a read of state the poller
already holds and costs nothing per request beyond memory.

### Problem Frame

The card today shows alerts: trains over the push gate, and nothing else. A commuter who opens the
app at 07:40 wants a different question answered: how does my morning look, all of it. That needs
every train in the window with a figure, including the ones that already ran, and the matcher
deliberately hides both: it filters to alert-worthy trains and drops departures that can no longer
be caught. The figures exist. The prediction cache scores every Vy rail call inside a −60 min to
+12 h window of the last build, no-SX trains included, and the poller rebuilds today's planned and
recorded calls on TTLs. Nothing assembles them per journey, and nothing keeps a train that has left
the prediction cache.

### Key Decisions

- **Level from the alert gate, not a second threshold.** A forecast is red exactly when
  `alert_policy.gate` passes for that train, so the strip and the push can never disagree about
  which trains are bad. Yellow is a typical arrival delay of 3 min or more; green is the rest; grey
  is no figure. Governs R3. (session-settled: the user's «>10 min» is read as the repo's «≥ 10»
  product bar, which is what the gate and the labels use.)
- **Filled means the alighting call is recorded, or the train is declared cancelled and its time
  has passed.** A train under way is a ring: the model's arrival forecast already carries what has
  been observed. Governs R2. (session-settled: mirrors `impact._is_observed`, which admits a
  declared cancellation as an observation only once its own time has passed.)
- **The day's planned timetable is the spine.** Every circle comes from `rail_calls_planned_today`
  held in memory; predictions and actuals colour it. Governs R1, R5. (Chosen over the prediction
  cache alone, which loses a train an hour after its time — including one cancelled before it ran,
  the circle that matters most.)
- **Served twice from one function, embedded on `/varsel-api/journeys` and public on
  `/notification-api/v1`.** Governs R6, R7. (Chosen over the page calling the public read: one
  fetch per poll instead of one per card, no rate-limit interplay between a device's own page and
  anonymous callers, and the route data never leaves the device-scoped API.)
- **Vy rail only in v1.** Both call tables are `lineRef LIKE 'VYG:%'`. A journey on a corridor GOA
  also serves gets Vy's circles only, and the answer says so. Governs R8.
- **Today only.** Tomorrow's plan, and the trains Vy cut from it, are an Open Question.

### Actors

- A1. **The commuter** on `/varsel`: glances at the strip, taps a circle for the line and the figure.
- A2. **The Entur app's BFF**: reads `journey-status` for a favourite route.
- A3. **The operator**: reads the two new log events and the cycle's two new phases.

### Requirements

**The answer**

- R1. For a journey definition (from, to, `begin`, `end`, `days`) and an instant, the answer lists
  every planned Vy rail train that boards at the from-place and alights at the to-place with an
  aimed departure inside today's window, in departure order. Boarding and alighting follow the
  matcher's rule (`boarding_allowed` at from, `alighting_allowed` at to, `stop_order` increasing).
  A day not in `days` answers an empty list with `reason: not_a_travel_day`.
- R2. Each train carries `state`: `confirmed` when its alighting call is recorded or it is declared
  cancelled with its aimed time at the boarding stop passed; `predicted` when the prediction cache
  holds a figure for it; `unknown` otherwise.
- R3. Each train carries `level`: `red` when cancelled (declared, or confirmed) or the confirmed
  arrival delay is 10 min or more, or (predicted) the alert gate passes; `yellow` when the
  confirmed arrival delay, or the typical arrival delay forecast, is 3–9 min; `green` below;
  `grey` when `state` is `unknown`. Cancelled additionally sets `cancelled: true`. **A trip is
  cancelled when its boarding call OR its alighting call is cancelled**: Vy cancels partial legs
  (Sandvika onward), and a train that leaves Oslo S but never reaches Asker is cancelled for this
  traveller.
- R4. Each train carries the identity and figures a client needs to render and explain it:
  `departure_time` (aimed, UTC), `line`, `operator`, `jkey`, `delay_min` (confirmed arrival delay
  where recorded), `expected_departure_time` (aimed plus typical departure delay, predicted),
  `delay_p50_min`, `impact_probability`, `alert_worthy`, `observed_at`.
- R5. The answer says what it is built from: `as_of` per source (`plan_refreshed_at`,
  `predictions_refreshed_at`, `actuals_refreshed_at`, each nullable), and `complete: false` with
  `reason` when the spine is not loaded (`plan_not_loaded`). A loaded spine with no predictions is
  complete with every future train `unknown`.

**The two surfaces**

- R6. `GET /varsel-api/journeys` carries `today` on every journey: the answer above for the
  device's own definition, computed on the request from memory. `null` where the spine is not
  loaded, so the card renders the old way.
- R7. `GET /notification-api/v1/journey-status` takes the pull read's five definition parameters
  (`from_stop`, `to_stop`, optional `begin`/`end`, optional `days`) with the pull read's validation
  and canonicalization, no credential, and answers the same body with `coverage` and `Cache-Control:
  public, max-age=60`. A cold spine is 503 `plan_not_loaded` with `Retry-After`, never an empty
  list. It shares the pull read's global and per-pair limiter.
- R8. The answer carries `coverage.operators` (`["VYG"]` in v1) so a client can say «kun Vy-tog».

**The page**

- R9. The journey card renders the strip between the meta chips and the alerts: a horizontal row
  that scrolls when the trains outnumber the width, a «nå» marker between past and future, a
  legend line (filled = bekreftet, ring = prognose, count). Each circle is a `<button>` with an
  `aria-label` naming line, time and state; tapping shows one detail line under the row.
- R10. Colours are Linje tokens only (no hex), so `?brand=none` reskins the strip as it reskins
  the Rushvarsel badges. Yellow carries dark ink. The confirmed cancelled circle strikes the time.
- R11. Nothing on the page fetches the public read. With `today` null the card is byte-for-byte
  what it is now.

**Bounds and hygiene**

- R12. No BigQuery on any request path. The two new in-memory indexes are loaded in the poller's
  cycle, after the rebuild that produces each table, as their own `_phase`.
- R13. The public read logs one `notification_status` record per request with `result`, `cache`,
  `n_trains`, `ms` and `pair_digest`, nothing identifying; its path is in `REDACTED_QUERY_PATHS`.
  The embed logs nothing per request.

### Acceptance Examples

- At 07:42 on a weekday, Oslo S → Asker 07:00–09:00: 07:03 filled green (arrived +1), 07:18 filled
  red struck (declared cancelled yesterday, time passed), 07:33 filled yellow (+5), 07:48 red ring
  (gate passes at 0.71), 08:03 green ring, 08:11 grey ring (no figure), 08:18 yellow ring (p50 4),
  08:33 and 08:48 green rings. «nå» between 07:33 and 07:48.
- At 06:10 the same journey: every circle a ring; 07:18 a red ring struck (cancelled, not yet
  due); trains after ~08:10 grey.
- At 09:30: every circle filled; a train that ran but whose alighting call was never recorded is a
  filled grey circle.
- Saturday, a Mon–Fri journey: no strip, `reason: not_a_travel_day`.
- A journey Ski → Oslo S: L2 and R-trains shown, RE20 shown, nothing from GOA (none runs there);
  Oslo S → Asker shows Vy's trains and `coverage.operators == ["VYG"]`.
- Poller restarted a minute ago: `today` is null on `/varsel-api/journeys`, the card is unchanged;
  the public read answers 503 with `Retry-After`.

### Scope Boundaries

- Not tomorrow, not the GTFS cut. The evening artboard's «I morgen» state needs tomorrow's plan
  and the standing-timetable comparison; both are Open Questions.
- Not bus, not GOA. Both call tables are Vy rail; widening them is its own change with its own
  scan cost.
- Not a wording. The strip carries figures, the alert boxes below it carry prose.
- Not a push. Nothing here notifies; the daily cap and `_notified` are untouched.
- No decision rows. The strip is unscored on `/historikk` (KTD10's rule; the pushes it sits beside
  are scored as before).

### Success Criteria

- A `/varsel-api/journeys` request with ten registrations completes in under 50 ms more than
  today's, measured in dev with the request harness.
- The two new phases together add under 3 s to the cycle at today's table sizes (~22 000 planned
  rows, ~9 000 recorded rows in dev at midday), read from `poller_cycle.phases`.
- Read in dev on the first weekday after U3: the strip agrees with `/historikk`'s verdicts for the
  day's pushed trains (a pushed train is a red ring before departure and, after its run, filled at
  the level the label gives it).

---

## Planning Contract

### Key Technical Decisions

- **KTD1. A leaf module, `journey_status.py`.** It imports `impact` and `alert_policy`, never
  `varsel` or `notification_pull`. `varsel.list_journeys` and the pull router both call it. This
  is what lets one function serve both surfaces under the existing rule that `varsel` never
  imports `notification_pull` (pinned both ways today, with `warm_figures` injected for exactly
  this reason). Providers are injected with `configure()`: the spine, the actuals, the prediction
  generation, the stop cache.
- **KTD2. Per-train enumeration lives in `impact`, at the ELIGIBILITY step, not at the cell
  step.** `_trip_cells` builds, per situation, an `eligible` list of (boarding call, alighting
  call) per `(operator, jkey)` and then keeps ONE train per line per situation («Per line, keep one
  eligible train»). A real Vy SX names 07:48, 08:03 and 08:18 together, so enumerating through the
  cells would keep one of the three and lose the other two from the strip. The eligibility block
  (boarding at `from_stop` inside the window, `boarding_allowed`, alighting at `to_stop` with
  `alighting_allowed` and a higher `stop_order`) is extracted into `impact._eligible_trains(...)`,
  which `_trip_cells` calls unchanged and `impact.trains_for_trip(from_stop, to_stop,
  window_start, window_end)` calls with `catchable_after=None`, over the pruned scope, then
  collapses across situations to one entry per `(operator, jkey)` keeping the worst call by
  `_call_severity` (what `select="worst"` ranks on; `varsel._alert_rank` is its restatement one
  layer up and `impact` cannot import it). Byte-identity with the matcher's eligibility holds by
  construction because there is one function; the test oracle is a brute-force walk of the
  snapshot's calls at `from_stop`, never `_trip_cells` itself, whose per-line pick is the very
  thing being skipped.
- **KTD3. Three sources, one precedence.** Declared cancellation (spine `call_cancelled`, or
  prediction `et_declared_cancelled`) > recorded alighting call (actuals index in U4, spine
  `call_source='recorded'` before it) > prediction (cache) > unknown. The spine decides WHICH trains
  exist; the other two only colour them. A prediction for a train the spine lacks is dropped
  (it cannot happen within one build, and across builds the spine is the older of the two).
- **KTD4. The spine and the actuals are in-memory copies of two tables the poller already
  rebuilds**, loaded by one `SELECT` each right after the rebuild that produced them
  (`rail_planned_calls` → new phase `rail_planned_cache`; `rail_calls` → new phase
  `rail_actuals_cache` in U4). Shape: `{(operator, jkey): tuple[PlannedCall, ...]}` sorted by
  `stop_order`, plus a by-stop-place posting list like `_call_pred_by_stop_ref`, so a window
  question is O(calls at the from-place). Swapped whole under a lock with a `refreshed_at`, never
  mutated. A failed load keeps the previous copy and logs; the request path reads whatever is
  there. Memory: ~22 000 rows as a slotted dataclass is a few MB against 1 GiB.
- **KTD5. Cache the enumerated FIGURES per generation, assemble per request.** Three things in
  the body depend on the request instant: the cancelled ring-to-fill boundary (KTD6), the no-SX
  grey horizon (KTD7) and the `now` the client splits on (KTD8). A cached answer would hold a
  declared-cancelled train as a ring for a whole cycle after its time. So the public read caches
  the time-independent half — the enumerated trains with their spine, prediction and actuals
  figures — per `(journey_key, plan_refreshed_at, predictions_refreshed_at, actuals_refreshed_at,
  Oslo day)` in the pull read's bounded single-flight shape, and `status_for` applies R2, R3 and
  KTD7 at assembly on every request. The embed does not cache at all: one memory walk per
  registration per poll, on a request that already pays for the matcher.
- **KTD6. Confirmed delay is the ARRIVAL at the alighting stop.** Neither SQL carries it today
  (`dep_delay_min` only). U4 adds `arr_delay_min` (`actualArrivalTime − aimedArrivalTime`) to both
  `rail_calls_today.sql` and `rail_calls_planned_today.sql`. Until U4, U1 grades a recorded
  alighting call by its departure delay and a terminus alighting call (no departure) by the last
  recorded call before it, and says so in `observed_at`. The prediction side already scores the
  arrival (`arr_delay_typ_min`, `p_arr_*`), so the two halves grade the same end from U4 on.
- **KTD7. Grey is honest, not green.** A no-SX train more than ~2 h out has a score that collapses
  to the base rate (CLAUDE.md's no-SX paragraph), so a figure with `has_situation=False` and
  `minutes_to_departure > NOSX_HORIZON_MIN` (120) is served `unknown`, not `green`. An SX-named
  train is served at any horizon, as the push is.
- **KTD8. The «nå» boundary is the SERVER's `now`.** The answer carries `now`; the client draws the
  marker between the last train whose aimed departure is before it and the first after. State,
  not position, decides fill (R2), so a train two minutes past due with no recording is a ring
  left of the marker for one cycle, which is true.
- **KTD9. Route on the pull router.** `journey-status` sits beside `journey-alerts` on
  `notification_pull.router` (no router dependency, `async def`, not in `sso.PROTECTED_PREFIXES`),
  reusing `_definition`, `_claim`, the coverage block and the 503 shape. The route-set pin and the
  `(route,) = router.routes` unpack in `test_notification_pull` move in the same commit.
- **KTD10. The embed is a field, the client is a pure component.** `varsel.Journey.today:
  JourneyStatus | None`; `api.ts`'s `Journey` gains the same; `components/DagensReise.tsx` takes
  `today` and `now` and renders; a pure `dagensReise.ts` maps `(state, level, cancelled)` to
  class names and the aria label, tested in `test/dagensReise.test.mjs`.

### Bounds

- `NOSX_HORIZON_MIN = 120` (KTD7).
- Spine load: one `SELECT` over `rail_calls_planned_today` per rebuild (hourly), ~22 000 rows,
  under 5 MB; actuals load one per 15 min, ~9 000 rows growing to ~20 000 by evening.
- Request path: memory only; a window of two hours on one corridor is under 40 trains.
- Public read: the pull read's `MAX_PER_MIN` and `PER_PAIR_PER_MIN` buckets, shared, not doubled.
- Answer cache: `STATUS_CACHE_MAX = 20_000` entries, evicted on any generation move.
- Phase time budget: under 3 s for both loads together (Success Criteria); measured, not assumed.

### High-Level Technical Design

```
poller cycle                                   request path
------------                                   ------------
rail_planned_calls  ─┐
                     ├─► rail_planned_cache ──► main._planned_spine ─┐
predict_calls_rail ──► call_predictions_cache ► impact generation ───┼─► journey_status.status_for(definition, now)
rail_calls ─────────► rail_actuals_cache (U4) ► main._rail_actuals ──┘        │
                                                                  ┌───────────┴───────────┐
                                                                  ▼                       ▼
                                                    varsel.list_journeys            notification_pull
                                                    (Journey.today)                 GET /journey-status
                                                          │
                                                          ▼
                                              demo/avviksvarsel DagensReise.tsx
```

`status_for` builds the window from the definition (`varsel.journey_windows`' Oslo rule, restated
in the leaf module without importing `varsel`: today's `[begin, end]` if today is in `days`),
enumerates trains from the spine's posting list at the from-place, checks alighting at the
to-place with increasing `stop_order`, then for each train reads the prediction cache through
`impact.trains_for_trip` and the recorded calls, and applies KTD3 and R3.

### Integration constraints and risks

- **The spine is up to 60 min stale**, so a train added or cancelled inside the hour is missing or
  wrong until the next planned rebuild. The prediction cache (five-minute cycle, but built FROM
  the planned table) has the same lag for cancellations. Tightening `_PLANNED_TTL` is the lever and
  costs linearly (~22 GB per rail rebuild since 2026-09-21). Accepted for v1 and stated in
  `as_of`.
- **Recorded calls in the spine are also 60 min stale**; U4's actuals index is what brings
  «Oppdatert» to 15 min, and U4 is not optional for the product to feel live.
- **A per-request matcher call per registration already runs in `list_journeys`**; the embed adds
  a spine walk per registration. At the device grain (a handful of journeys) this is nothing; the
  public read is where volume lands, and it is cached and limited.
- **`rail_calls_today` is a CREATE OR REPLACE on the whole day**; adding a column is safe for its
  readers (`disruptions_today.sql` joins by name) and the validator does not cover either SQL
  today. Dry-run both by hand before merging U4.
- **The prediction table's horizon** (−60/+720 min from build) means a morning window viewed at
  05:00 has trains beyond +12 h only for very long windows; they read grey. Fine.

---

## Implementation Units

### U1. Per-train enumeration and the pure status module

- **Goal:** `impact.trains_for_trip` and `journey_status.status_for` exist, tested hermetically,
  with a spine and actuals injected as plain dicts.
- **Requirements:** R1–R5, R8. Implements KTD1, KTD2, KTD3, KTD6 (pre-U4 rule), KTD7.
- **Files:** `disruptions/api/impact.py`, `disruptions/api/journey_status.py` (new),
  `disruptions/api/test_journey_status.py` (new), `disruptions/api/test_impact.py`.
- **Approach:**
  1. Extract `impact._eligible_trains(calls, from_stop, to_stop, window_start, window_end,
     catchable_after, better)` out of `_trip_cells` (a pure move; the existing `impact` and
     `varsel` suites are its characterization coverage), then `impact.trains_for_trip(...) ->
     list[TrainFigures]`: over the pruned scope, per situation, the eligible pairs with
     `catchable_after=None`; collapse across situations per `(operator, jkey)` keeping the worst
     boarding call by `_call_severity`; carry the boarding and alighting `StopPrediction`s,
     `has_situation`, `et_declared_cancelled` for BOTH calls, `call_source`, `et_dep_delay_min`
     and the winning situation number.
  2. `journey_status.py`: `PlannedCall` (slotted), `Spine` (by-key and by-stop-place posting
     list, `refreshed_at`), `TrainStatus` and `JourneyStatus` pydantic models (the wire shape),
     `configure(spine=..., actuals=..., predictions_refreshed_at=..., stop_rows=...)`,
     `window_today(definition, now)` and `status_for(definition, now)`.
  3. Precedence and levels exactly as KTD3 and R3, as small named functions (`_level_confirmed`,
     `_level_predicted`) so the tests can hit each branch by name.
- **Test scenarios (each must be able to fail):**
  - Every acceptance example above as a table-driven test over a synthetic spine and cache.
  - A train in the cache but not in the spine is absent (KTD3).
  - A declared-cancelled train reads `predicted`/red ring before its time and `confirmed`/red
    after (KTD6 boundary at the aimed departure).
  - A no-SX figure at 150 min out reads `unknown`; the same at 90 min reads its level; an SX
    figure at 150 min reads its level (KTD7).
  - Gate red follows the operator threshold and the no-SX cut (a 0.70 no-SX figure is not red;
    a 0.70 SX figure is).
  - A recorded terminus alighting call with no departure is graded by the previous recorded call.
  - Alighting call cancelled, boarding call not: `cancelled: true`, red (R3).
  - One SX situation naming three trains in the window: all three are enumerated, each with the
    SX figures, and `_trip_cells` on the same snapshot returns one (the case KTD2 exists for).
  - Differential: over randomized snapshots, `trains_for_trip`'s train set equals a brute-force
    walk of every call at `from_stop` inside the window with a usable alighting call, and each
    train's figures equal its most severe such call's. `_trip_cells`'s cell set is unchanged by
    the extraction (its own differential test is re-run).
  - The module's import set is exactly `{impact, alert_policy, pydantic, stdlib}` (source pin).
- **Verification:** both suites pass under `uv run python <suite>.py`; `ruff check .` clean.

### U2. The spine in memory and the public read

- **Goal:** the poller holds the planned timetable in memory; `GET /notification-api/v1/journey-
  status` answers from it.
- **Requirements:** R5, R7, R8, R12, R13. Implements KTD4, KTD5, KTD9.
- **Dependencies:** U1.
- **Files:** `disruptions/api/main.py`, `disruptions/api/notification_pull.py`,
  `disruptions/api/logging_setup.py`, `disruptions/api/test_notification_pull.py`,
  `disruptions/api/test_notification_contract.py`, `disruptions/api/test_poller_refresh.py`,
  `docs/operations/notification-webhooks.md`, `docs/integrations/notification-api.md`.
- **Approach:**
  1. `main.refresh_planned_spine()`: `SELECT jkey, lineRef, stop_place, stop_order, aimed_dep,
     aimed_arr, dep_delay_min, call_cancelled, call_source, boarding_allowed, alighting_allowed FROM
     disruptions.rail_calls_planned_today`, built into a `Spine`, swapped under a lock; called
     inside a new `_phase("rail_planned_cache")` right after `rail_planned_calls`, guarded by the
     planned table's `refreshed_at` so it runs only when the table was rebuilt. Failure logs and
     keeps the old spine (`test_poller_refresh` pins that a raise here does not fail the cycle).
  2. `journey_status.configure(...)` wired in `lifespan` beside `notification_pull.configure`.
  3. The route: parse with `_definition`, limiter with `_claim`, cache per KTD5 with the figures
     cache's single-flight helpers generalized or duplicated minimally, 503 `plan_not_loaded` on a
     `None` spine, `coverage` from the existing block plus `operators`, `Cache-Control` per R7.
  4. `REDACTED_QUERY_PATHS` gains the path; `notification_status` log record; row in the
     operations document's log table, which also states that the per-pair bucket is SHARED with
     `journey-alerts` (a BFF reading both routes for one favourite spends the published 30/min
     twice as fast); `docs/integrations/notification-api.md` gains a `GET /journey-status`
     section with the body, the statuses, the two staleness figures and the shared bucket.
  5. Move the route-set pin to `{("/journey-alerts","get"), ("/journey-status","get")}` and the
     unpack to a loop.
- **Test scenarios:**
  - A loaded spine and cache answer the 07:42 acceptance example through the route, `complete`
    true, `Cache-Control` public.
  - Spine `None` answers 503 with `Retry-After`; spine loaded and predictions `None` answers 200
    with every future train `unknown`.
  - A second read within the same generations runs no enumeration; a spine swap evicts.
  - Two reads inside one generation straddling a declared-cancelled train's aimed departure
    answer `predicted` (ring) then `confirmed` (filled), and `now` moves between them (KTD5).
  - The per-pair bucket is shared with `journey-alerts`: thirty reads split across the two routes
    on one pair trip the limit together.
  - 422 cases are the pull read's, exercised once through this route.
  - No log record carries a ref, name or time; the access log for the path is redacted; the
    contract test finds the event row and the module in its lists.
  - A raise inside `refresh_planned_spine` leaves the previous spine and the cycle green.
- **Execution note:** land in dev, read `poller_phase` for `rail_planned_cache` and the
  `notification_status` volume for a day before U3.

### U3. Embed on `/varsel-api/journeys` and the strip on the card

- **Goal:** the card shows the strip.
- **Requirements:** R6, R9, R10, R11. Implements KTD8, KTD10.
- **Dependencies:** U2 deployed in dev.
- **Files:** `disruptions/api/varsel.py`, `disruptions/api/test_varsel_api.py`,
  `demo/avviksvarsel/src/api.ts`, `demo/avviksvarsel/src/components/JourneyCard.tsx`,
  `demo/avviksvarsel/src/components/DagensReise.tsx` (new),
  `demo/avviksvarsel/src/components/dagensReise.ts` (new),
  `demo/avviksvarsel/test/dagensReise.test.mjs` (new), `demo/avviksvarsel/src/styles.css`,
  `demo/avviksvarsel/src/brands/none.css` (only if a token the generator cannot see leaks).
- **Approach:**
  1. `Journey.today: JourneyStatus | None`; `list_journeys` calls `journey_status.status_for` per
     journey under its own `try`, so a raise here degrades that journey's `today` to `null` and
     nothing else. (The matcher call in that loop has no such guard today; do not widen the new
     one to cover it.)
  2. `DagensReise.tsx`: header row («I dag», «Oppdatert HH:MM» from the newest non-null `as_of`),
     the row (`overflow-x: auto`, `scroll-snap-type: x proximity`, connector line, «nå» marker
     placed between the last past and first future train by `now`, right-edge fade), legend, one
     detail line for the tapped train. On mount, scroll so the marker sits in the left third;
     `prefers-reduced-motion` disables smooth scroll. Renders nothing when `today` is null or
     `reason` is `not_a_travel_day`; renders «Vurderes fra HH:MM» when every train is `unknown`
     and the window is ahead.
  3. `dagensReise.ts`: `circleClass(train)`, `circleLabel(train)` (Norwegian: «L1 07:18,
     innstilt», «R13 08:11, ikke vurdert», «L1 07:48, ventet over 10 min forsinket»),
     `splitAtNow(trains, now)`. **The strip's four-value level vocabulary lives here, never in
     `levels.ts`**: that file is a byte-identical duplicate across two apps pinned against the
     server's `_LEVEL_TEXT`, and a fourth level there fails `test_togvarsel`.
  4. CSS on Linje validation tokens (`--fill-success-*`, `--fill-warning-*`, `--fill-negative-*`,
     `--fill-neutral-*` for grey) and text tokens; yellow fill takes `--text-color-default`; the
     ring is a 3 px border in the `--stroke-*` step; hit area 44 px via padding on a 38 px face.
- **Test scenarios:**
  - `test_varsel_api`: a device with one journey and a loaded spine gets `today` with the trains;
    spine `None` gets `today: null` and the response is otherwise unchanged (byte compare against
    the pre-change fixture); a `status_for` raise nulls that journey only.
  - Node: `circleClass` for all twelve `(state, level)` combinations plus cancelled; `circleLabel`
    wording per state; `splitAtNow` with an empty list, all past, all future, a tie on `now`.
  - Personvern guard unaffected (no new published text beyond UI labels; run the suite).
- **Execution note:** view with the stubbed `:8000` and `npm run dev` per the local-front-end
  note; check `?brand=none` once in the browser, since the icon-colour trap is invisible to tests.

### U4. Actuals at fifteen minutes and the arrival delay

- **Goal:** «Oppdatert» moves from ~60 min to ~15, and the confirmed level grades the arrival.
- **Requirements:** R2, R3 at the design's freshness. Implements KTD4 (actuals half), KTD6.
- **Dependencies:** U3.
- **Files:** `disruptions/api/rail_calls_today.sql`, `disruptions/api/rail_calls_planned_today.sql`,
  `disruptions/api/main.py`, `disruptions/api/journey_status.py`,
  `disruptions/api/test_journey_status.py`, `disruptions/api/test_poller_refresh.py`,
  `data/queries/validate_varsel_sql.sh`, `CLAUDE.md`.
- **Approach:**
  1. Both SQLs gain `arr_delay_min` = `TIMESTAMP_DIFF(SAFE.TIMESTAMP(rc.actualArrivalTime),
     SAFE.TIMESTAMP(rc.aimedArrivalTime), MINUTE)` on recorded calls (`NULL` on estimated); both
     registered in `validate_varsel_sql.sh` and dry-run.
  2. `main.refresh_rail_actuals()`: `SELECT jkey, lineRef, stop_place, stop_order, aimed_dep,
     aimed_arr, dep_delay_min, arr_delay_min, call_cancelled FROM disruptions.rail_calls_today`
     after `maybe_refresh_rail_calls` in a `_phase("rail_actuals_cache")`, guarded by
     `_rail_calls_refreshed_at`; same swap-and-keep-on-failure rule.
  3. `status_for` prefers the actuals index over the spine's recorded calls; the confirmed level
     reads `arr_delay_min` at the alighting call, falling back to the pre-U4 rule where it is
     `NULL`.
  4. CLAUDE.md's Avviksvarsel section gains the strip paragraph: the three sources, the
     precedence, the two staleness figures, and that `rail_calls_today` now has a live reader
     (its header comment says nothing live reads it; U4 changes that sentence too).
- **Test scenarios:**
  - An alighting call with `arr_delay_min` 12 and `dep_delay_min` 3 reads red (arrival wins).
  - Actuals index newer than the spine overrides a spine `estimated` call to `confirmed`.
  - A raise in `refresh_rail_actuals` keeps the previous index and the cycle green.
  - `as_of.actuals_refreshed_at` is the index's, not the spine's.
- **Verification:** dry runs of both SQL files against dev print the same byte counts as before
  within 5 %; both suites green; a weekday's `poller_cycle.phases` shows both loads under 3 s.

---

## Verification Contract

- Every suite under `disruptions/api/test_*.py` runs as `uv run python <suite>.py` and its
  assertion COUNT moves for the suites this plan touches (the `__main__` runner trap).
- `demo/avviksvarsel`: `npm test` green including the new node test; `npm run build` green.
- `uv run ruff check .` clean.
- Dev: after U2, `poller_phase` for `rail_planned_cache` under 3 s; after U3, the strip on a real
  registration agrees with `/impact` for the same window (same red trains); after U4, «Oppdatert»
  is within 20 min of wall clock on a weekday afternoon.

## Definition of Done

- U1–U4 merged and deployed to dev; U4 read there for one weekday before prd.
- `docs/integrations/notification-api.md` documents `GET /journey-status`; the operations
  document's log table has `notification_status`; CLAUDE.md carries the strip paragraph.
- This plan is the design record; the «Pendleroversikt» canvas is a private claude.ai artifact
  and is not linked from the client-facing document. If the app team needs the visual, export the
  three artboards as PNGs under `docs/plans/assets/` and link those.

## Open Questions

1. **Tomorrow.** The design's evening state («I morgen», with Vy's cut trains dashed) needs
   tomorrow's planned trips (the planned table reads yesterday's and today's ET partitions, not
   tomorrow's) and the standing-timetable comparison from `03b_plan.sql` / `togvarsel_features_
   today.sql`'s `cut` CTE. Both are GTFS reads; neither is on the request path. Worth its own
   plan once the strip has been used for a week.
2. **GOA and other rail.** Widening both call tables from `VYG:%` to all rail is a scan-cost
   question (`rail_calls_planned_today` is ~22 GB per rebuild for Vy alone since 2026-09-21).
3. **Should the public read carry `expected_departure_time` for a confirmed train too?** It
   would let the app show «gikk 07:21» beside the aimed 07:18. Cheap (`dep_delay_min` at the
   boarding call is in the spine), left out to keep v1's body small.
4. **Do we want the embed cached per device?** Not needed at today's registration counts; noted
   because `list_journeys` is a public unauthenticated route with a per-device key.

## System-Wide Impact

- Two new poller phases, both memory loads; no new BigQuery table, one new column on two existing
  ones (U4).
- `varsel.Journey` gains a nullable field; every existing client keeps working (`api.ts` is the
  only client and is changed in the same unit).
- `notification_pull.router` gains a route; the OpenAPI schema lists it; nothing in `sso`.
- `rail_calls_today` acquires its first live reader; its header comment's cost argument (nothing
  live reads it, so 15 min is fine) still holds because the reader tolerates the lag.

## Sources and Research

- The «Pendleroversikt» design canvas (2026-09-22).
- `notification_pull.py`, `impact._trip_cells`, `varsel.match_journey`, `predict_calls.sql` (the
  three-branch partition and the −60/+720 horizon), `rail_calls_planned_today.sql`,
  `rail_calls_today.sql`, `main.maybe_refresh_rail_calls`.
- Row counts in dev at 2026-09-22 14:30 Oslo: planned 21 842, recorded 8 587, scored 13 219.
- The pull-read plan (`2026-09-10-001`) for the route shape, the limiter, the redaction and the
  cache pattern.
