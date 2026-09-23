---
title: Trains Removed From the Plan - Plan
type: feat
date: 2026-09-23
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
product_contract_source: ce-plan-bootstrap
execution: code
---

# Trains Removed From the Plan - Plan

Vy has been taking trains out of the day's timetable instead of cancelling them since Saturday
19 September. Such a train never enters SIRI-ET, so nothing in this codebase can see it. The strip
shows no circle for it, the card says nothing, and nobody gets a push. This plan makes a removed
train a known cancellation, delivered as one push per journey per day.

## Goal Capsule

- **Objective:** A commuter whose usual train has been removed from today's plan is told, once, in
  the morning, which of their trains are not running. The «I dag» strip shows each one as a red
  cancellation instead of leaving it out.
- **Means:** A small table of today's removed Vy trains, built from the GTFS timetable against its
  own standing plan (the rule the strike reports and the Rushvarsel labels already use). It is
  relayed into the prediction cache like an ET-declared cancellation, loaded into the strip's
  spine, and delivered to browser registrations as one digest push per registration per day, not
  before 05:00 Oslo.
- **Authority:** The user's request (2026-09-23), with two choices made in-session: one digest per
  registration per day, and no push before 05:00. The product owner's 2026-09-21 ruling that a
  timetable cut announced the day before counts as a bad rush. The journey-status plan
  (`2026-09-22-001`) left «the trains Vy cut from it» as an open question; this plan answers it
  for today. `CLAUDE.md` and `.claude/rules/{varsel,notification-webhooks,historikk}.md` govern code,
  tests and deployment.
- **Execution profile:** Five units. U1 builds the table behind a flag and is gated on a replay
  against the strike reports. U2 relays removed trains into the cache with per-train push held
  off. U3 puts them on the strip. U4 adds the digest, built on U3's spine. U5 labels them for
  `/historikk`. Each unit lands in dev and is read there before the next.
- **Stop conditions:** Never push a removed train one push per train. Never let a removed row reach
  the model's feature CTEs or Rushvarsel's outcome passes. Never build the table while today's GTFS
  partition is missing: every standing train would read as removed. Never mint a jkey in the
  `DatedServiceJourney:` namespace. Never turn the flag on in prd before the replay passes.
- **Tail ownership:** This repository. The prd flag and the prd deploy are the user's call.

---

## Product Contract

### Summary

Each morning, from 05:00, a registration whose window today contains trains Vy removed from the
plan gets one push: «3 tog er tatt ut av rutetabellen i dag», with the route on the first line and
the trains listed after it. The strip shows each removed train as a red cancellation, and so do the
card and the public journey-alerts read. It is one push, whatever the count, and it uses one of
the registration's four daily pushes.

### Problem Frame

Three things measured on 23 September (`docs/reports/streik-2026/rapport-2026-09-23.html`, fetched
10:39):

- 147 trains that run on an ordinary Wednesday were missing from the day's timetable. They would
  have served 92 registered journeys. By comparison, 5 trains were properly cancelled.
- The count has grown every day since the cut began: 40, 68, 88, 109, 147 removed trains on
  registered journeys from 19 to 23 September; 125-199 a day across Vy's whole plan. On an ordinary
  week (14-18 September) it was 2 a day, both on F4.
- None of them could be alerted. The prediction cache, the strip's spine and every push are built
  from `rail_calls_planned_today`, which is built from ET, and a removed train never enters ET. No
  SX message announces the reduced plan.

Per-train pushes would not work at this volume. 35 of the 92 journeys lost more than 4 trains (up
to 38, Oslo S → Ski). One push per train is 601 pushes that day; the cap of 4 cuts that to 237, and
those 35 journeys would spend their whole day's allowance on removed trains before 06:00.

### Key Decisions

- **One digest per registration per Oslo day.** Every removed train in the registration's window
  is listed in one push. It counts once against `_MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY`, leaving
  three for real cancellations and forecasts. A later digest the same day (a registration created
  at 10:00, or a set that grows) replaces the first on the lock screen under the same tag and is
  exempt as a replacement. Governs R6-R8. (session-settled: chosen by the user over per-train pushes
  on the 601/237/35-of-92 figures above.)
- **Not before 05:00 Oslo.** The day's GTFS lands ~02:06 and the first planned rebuild after it
  runs by ~03:06, so the first digest could go out around 03:00. It waits until 05:00. A removed
  train that leaves before 05:00 is on the strip and the card but not in the push. Governs R7.
  (session-settled: chosen by the user.)
- **Removed means the report's rule.** A train is removed on day d when its (line, train number)
  was in the GTFS plan on at least 3 of the 4 previous same weekdays, is not in d's GTFS, is not in
  d's ET under the same train number, and no train new in d's plan stands in for it within 15
  minutes. It is the same baseline as the strike reports' «Fjernet fra planen» and Rushvarsel's
  `standing` CTE. Governs R1-R3.
- **A removed train is a cancellation to the traveller, and its source is visible.** Everything
  traveller-facing reads it as cancelled (red, struck, `cancelled: true`). The digest and the strip
  say «tatt ut av rutetabellen» instead of «innstilt», because Vy has not cancelled it and a
  traveller who checks entur.no will see no cancellation. The card and the pull read keep saying
  «Innstilt» in v1: their cancelled wording is `varsel._deterministic_body`'s and the model's, and
  the situation's summary carries the source. Governs R4, R7, R9.
- **Vy rail only, today only**, as the strip. Governs R10.

### Actors

- A1. **The commuter** on `/varsel`: gets the morning digest, sees the red circles.
- A2. **The Entur app's BFF**: sees removed trains on `journey-alerts` and `journey-status`.
- A3. **The operator**: reads `/historikk`'s new segment, the build's log line, the replay.

### Requirements

**The removed set**

- R1. Once today's GTFS partition exists, `disruptions.rail_calls_removed_today` holds one row per
  call of every Vy train removed today (Key Decisions' rule), in the call shape of
  `rail_calls_planned_today`: `jkey`, `lineRef`, `stop_place`, `stop_name`, `stop_order`, `aimed_dep`,
  `aimed_arr`, `call_date`, `call_cancelled = TRUE`, `call_source = 'removed'`,
  `boarding_allowed`, `alighting_allowed`, plus `trainno`. Before today's partition exists it
  holds no rows.
- R2. A removed train's calls are its own calls on the last date it ran, moved to today in Oslo
  local time. A train re-added to ET later in the day leaves the table at the next planned rebuild
  (≤ 60 min).
- R3. A line on which today's ET plan closes boarding or alighting anywhere (F4, RX11, R31 and RE30
  when last measured) is left out. GTFS carries no pickup or drop-off types, so it cannot say which
  calls a removed train's passengers may use. Guessing «allowed» is the 2026-09-01 F4 incident: a
  Bergen train pushed to Oslo S → Drammen registrations nobody could have been aboard.

**Every surface that already shows a cancellation**

- R4. Removed calls are relayed into the prediction cache as certainties, exactly as
  `predict_calls.sql`'s `et_only` rows are: `p_cancel` 1.0, `p_impact`/`p_impact_dep` 1.0 on the
  side that exists, every delay figure NULL, `has_situation = FALSE`. One synthetic situation per
  train, `KIX:PlanRemoved:<jkey>`, with summary «Tatt ut av dagens rutetabell» and a description
  saying the train usually runs on this weekday, is not in today's timetable, and no cancellation
  has been published. The card, `/impact` and `GET /notification-api/v1/journey-alerts` show
  removed trains with no code of their own (`phase: confirmed` on the pull read; «Innstilt» in the
  card's and the pull read's wording, as for any cancellation).
- R5. Webhook events are not emitted for removed trains in v1. `notification_events` skips them
  with its own skip reason, and the contract says so. The subscription cap of 4 would spend itself
  at 03:00 exactly as the browser cap would.

**The push**

- R6. A browser registration never gets a per-train push about a removed train, and its removed
  trains are never queued for an LLM wording.
- R7. From 05:00 Oslo, a registration with one or more removed trains still to depart in today's
  window, not yet notified, gets one digest push. The set is the day's, from the strip's spine,
  not the prediction cache's 12-hour horizon. Title: «N tog er tatt ut av rutetabellen i dag» (the `[dev]` prefix as
  usual). Body: the route line, then the trains soonest first, «L1 07:10, L1 07:40 og R13 08:11 går
  ikke i dag.», at most six named and «og K til» for the rest, then «De går vanligvis på
  <ukedag>er, men finnes ikke i dagens rutetabell.» Deterministic text, no model.
- R8. After a successful send, every train the digest covers, named or counted in «og K til», is
  marked notified for that registration, and its decision rows record `pushed = TRUE` with the
  digest text as their message.
  A failed send re-offers the whole set next cycle. The digest takes one slot of the daily cap;
  a replacement digest under the same tag takes none.

**The strip**

- R9. The strip's spine holds removed trains beside ET's. A removed train is `cancelled: true`,
  `level: red`, a ring until its aimed departure and filled after, as a declared cancellation is.
  `TrainStatus` gains `removed_from_plan: boolean`; the circle's label reads «tatt ut av
  rutetabellen» instead of «meldt innstilt» / «innstilt».

**Scope and hygiene**

- R10. Vy rail, today. `VARSEL_PLAN_REMOVED=on|off` (default `off`); `off` rebuilds the table empty,
  and every surface is then exactly as it is now.
- R11. Removed rows reach no model feature and no Rushvarsel outcome. The line and network state
  features in `predict_calls.sql` (`line_pre_*`, `net_pre_*`, `line_now_*`) and
  `togvarsel_outcomes_provisional.sql` read `rail_calls_planned_today` only and stay that way.
  Rushvarsel already counts cut trains through its own `standing` term.

### Acceptance Examples

- Wednesday 30 September, Oslo S → Ski 07:00-09:00, three removed trains in the window (R22 08:00,
  R23 08:06, R23 08:36). 05:00: one push, «[dev] 3 tog er tatt ut av rutetabellen i dag» / «Oslo S
  → Ski» / «R22 08:00, R23 08:06 og R23 08:36 går ikke i dag. De går vanligvis på onsdager, men
  finnes ikke i dagens rutetabell.» At 07:42 the strip shows those three as struck red rings among
  the other trains. At 09:30 they are filled.
- The same registration, R21 08:22 then cancelled in ET at 07:30: a normal per-train push, and the
  cap has 3 left for it.
- A registration with 38 removed trains: one push naming six, «og 32 til».
- Vy re-adds R23 08:06 to ET at 06:40: at the next planned rebuild it leaves the removed table, and
  the strip shows the ET train in its place. The digest already sent is not corrected (Open
  Questions).
- Wednesday 30 September, an F4 Oslo S → Drammen registration: nothing. F4 closes alighting at
  Drammen, so the line is out of v1.
- 02:30, today's GTFS not yet written: the table has no rows; nothing is shown anywhere.

### Scope Boundaries

- **Not the intraday pull** (shape B in `docs/rushvarsel/02-removed-journeys-2026-09-21.md`: a
  journey that goes silent in ET mid-day). It needs its own staleness rule and false-positive
  replay.
- **Not tomorrow.** The GTFS partition for d is written ~02:06 on d; an evening warning about
  tomorrow would need ET-ahead detection.
- **Not the webhook channel** (R5), and not bus or GOA.
- **Not the lines with boarding restrictions** (R3). Admitting them needs each train's own
  activities from its last ET appearance.
- **Not a collapsed card.** A registration with 38 removed trains lists 38 alert boxes, as a heavy
  ET-cancellation day already does; the strip is the overview.
- **Not a correction push** when a removed train comes back.

### Success Criteria

- **Replay before any flag is turned on (U1's gate):** over 14-18 September the table yields at most
  2 removed trains a day on registered prd journeys (the reports' ordinary-week figure, 0 expected
  once F4 is left out). Over 19-23 September it reproduces the reports' per-registration counts
  (40/68/88/109/147, less F4's) within 10 %. Any day whose count jumps with no cut behind it (a
  timetable change, an engineering week) is examined before the flag goes on.
- **In dev on the first weekday after U4:** every registration with removed trains in its window
  gets exactly one digest, at or after 05:00. `push_capped` counts none of them. The strip shows
  each one red.
- **After U5's first nightly run:** at least 90 % of the day's `KIX:PlanRemoved:` rows carry a
  verdict.
- **Cost:** the table's hourly rebuild bills under 0.5 GB (5 GTFS partitions at ~0.04 GB plus the
  planned table's keys), confirmed by dry run.

---

## Planning Contract

### Key Technical Decisions

- **KTD1. Its own table, never rows in `rail_calls_planned_today`.** The planned table has readers
  that must not see a removed train, and each would need its own opt-out. The model's state
  features take `AVG(call_cancelled)` over it (`predict_calls.sql` ~191-224, `line_pre_cancel`,
  `net_pre_cancel`, `line_now_cancel`), so 147 extra cancelled rows would move every real train's
  forecast on exactly the cut days, from inputs training never saw. That is the train/serve skew
  `CLAUDE.md`'s `has_situation` paragraph warns about. Rushvarsel's provisional outcome pass reads
  the planned table as its call universe and adds the standing-plan cut separately, so it would
  count them twice. `varsel_connectivity.sql` would call a pair served only by removed trains
  «served». A separate table makes the two readers that want it opt in: the relay in
  `predict_calls.sql` and the strip's spine loader. A forgotten opt-in only hides a train; a
  forgotten opt-out silently changes the models.
- **KTD2. Built hourly, straight after the planned rebuild, in the same TTL gate.** The ET
  anti-join (R2) needs a fresh planned table, and the GTFS side is five partitions
  (`operating_date IN (d, d-7, d-14, d-21, d-28)`, ~0.2 GB). Own `_phase("rail_removed_calls")` with
  its own `try`. A failure keeps the previous table, which is harmless: every row carries its own
  date and every reader windows on aimed time. The lifespan runs the flag-off build (the same SQL
  under a zero-row predicate) when the table is absent, so `predict_calls.sql` never meets a
  missing table, and its positional UNION has one schema source. A missing table would fail
  `predict_calls.sql`, and with it every prediction.
- **KTD3. The SQL, rule by rule.** Restate `removed3.sql` (the strike reports' query, session
  c486abb0) and the `standing` CTE of `togvarsel_outcomes_settled.sql`, not a new rule:
  - train number: GTFS `REGEXP_EXTRACT(trip_id, r'ServiceJourney:(\d+)[-_]')`; ET the same
    pattern on `jkey` (`DatedServiceJourney:2105_LLS-ASR_26-09-23` → `2105`, checked in dev
    2026-09-23);
  - standing: (line, trainno) on ≥ 3 of d-7/d-14/d-21/d-28; removed: standing, not in d's GTFS;
  - guards: no rows unless d's partition has at least one VYG train trip (R1); and no rows, with a
    logged refusal, when the removed set exceeds 50 % of the standing set. A half-written
    partition would otherwise read as a network-wide cut and push to every registration at 05:00;
    the worst real day so far was ~13-20 %. Confirm whether the partition lands atomically, and
    drop the ratio if it does;
  - netting, at train level: drop R when a train new in d's plan (not standing), same line and
    `direction_id`, calls R's origin `parent_station` within 15 min of R. The reports net per
    registration (boarding → alighting); the table cannot, and the replay measures the gap;
  - reinstatement: drop R when `rail_calls_planned_today` carries (line, trainno). RE20's GTFS and
    ET numbers do not join (`togvarsel_outcomes_settled.sql` says so), so RE20 is detected but its
    reinstatement never is; the replay says whether that matters;
  - calls: R's rows on `last_seen`, with `stop_place = parent_station`, `stop_order =
    stop_sequence`, `stop_name` from GTFS as the planned table resolves it, `lineRef =
    'VYG:Line:' || route_short_name`. Times move by whole days in Oslo local datetime
    (`DATETIME_ADD(DATETIME(t, 'Europe/Oslo'), INTERVAL DATE_DIFF(d, last_seen, DAY) DAY)` back
    to a timestamp in `Europe/Oslo`), never `TIMESTAMP_ADD` of 24 h multiples: DST ends 25 October,
    inside the first four weeks this runs;
  - restricted lines: drop every line with a `NOT boarding_allowed OR NOT alighting_allowed` call
    in today's `rail_calls_planned_today` (R3); both flags TRUE on what remains;
  - jkey `RemovedJourney:<trainno>_<yyyy-mm-dd>`: unique per train per day, stable across
    rebuilds, outside `DatedServiceJourney:` (the labeller joins that namespace without a date).
- **KTD4. The relay reuses `et_only`, with a CASE on `call_source`.** In `predict_calls.sql` the
  `et_cancelled` CTE reads `rail_calls_planned_today WHERE call_cancelled UNION ALL
  rail_calls_removed_today`, and the final `et_only` SELECT picks the prefix, summary and
  description on `call_source = 'removed'`. The `covered` anti-join and the −60/+720 min window
  apply unchanged. Without the CASE every removed train would mint `KIX:ETCancellation:` with
  «Innstilt Oslo S–Ski», which is wrong both on the card and on `/historikk`. The three-branch
  partition of the ET timetable still holds: removed calls are not in it.
- **KTD5. The digest is a delivery step, not a new alert, and its set comes from the spine.**
  Matching, decision rows and the prediction cache stay per train. The prediction cache only
  reaches −60/+720 min from its build, so at 05:00 it holds nothing after ~17:00. A digest built
  from it would be followed by replacements at 06:00, 07:00 and later as a long window's trains
  crossed the horizon. The set is therefore `journey_status.status_for(record, now)`'s trains with
  `removed_from_plan` and a departure after `now`. `varsel` already imports `journey_status`, and
  the strip and the digest agree by construction. Only registrations whose from-place carries a
  removed call today are walked (the spine's by-stop index), so a day without a cut costs nothing.
  In `check_and_notify`'s collect loop (`varsel.py` ~5120-5170), removed alerts from the cache are
  skipped (R6), and a registration with removed trains not in `ledger.notified`
  (`KIX:PlanRemoved:<jkey>`) gets one pending item with its own tag
  (`plan-removed:<journey_id>:<oslo_date>`). It is cap-checked once, and it is a replacement when
  that tag is already in `ledger.pushed_trains`. Loop 2 marks every covered situation (R8), so
  the same set never re-sends. The title and body live in
  `notification_wording` (the leaf module that owns titles), `removed_digest(...)`. The 05:00 gate
  is an Oslo-time check in the same loop; the day key is `oslo_today` as the cap's is
  (gate and latch in one timezone, `oslo-utc-gate-and-latch`).
- **KTD6. Removed trains never enter the write queue.** The pass that picks trains to word (1b →
  1c) skips `KIX:PlanRemoved:` alerts for browser recipients. Otherwise they spend
  `_MAX_LLM_CALLS_PER_CYCLE` and the pair's `_MAX_MESSAGES_PER_JOURNEY` on wordings no push will
  carry. The pull read words its own four soonest as it does today (input `innstilt` and
  `ingen_melding`, so the model says «innstilt»; noted, not changed).
- **KTD7. U2 is safe on its own.** It ships R6's exclusion with the relay, so between U2 and U4
  removed trains show on the card, the strip and the pull read, and push nothing.
- **KTD8. The strip loads the same table.** `main.refresh_planned_spine` runs a second `SELECT` over
  `rail_calls_removed_today` and builds its rows as `PlannedCall(call_cancelled=True,
  call_source='removed')` into the same `CallTable`. `journey_status._trains` finds them unchanged;
  `_status` sets `removed_from_plan` from `call_source`. The eligibility oracle
  (`test_the_train_set_agrees_with_the_push_matchers_eligibility`) gains a removed train.
- **KTD9. Labelled by train number, and only where the line ran.** `label_varsel_decisions.sql`
  gains a branch for `KIX:PlanRemoved:` rows, joined on (VYG, line, train number from the jkey,
  boarding stop place), never on `stop_order`: a removed row's order is GTFS `stop_sequence`, ET's is
  `sequenceNr`, and `last_recorded.stopPointRef` is a quay, so it goes to its parent through
  `national_stop_registry.quays_last_version` as `rail_calls_today.sql` does. Trouble TRUE when
  `last_recorded` has the line running on that operating date and no journey with that train number;
  FALSE when the train ran, not cancelled, departing the boarding stop under 10 min late; NULL
  otherwise. «Absent from ET» is
  only evidence where ET had the line at all: its rail slice runs 10-17 % thin, and a hit made of
  missing data is the reverse of the error the NULL rule exists to prevent. The SQL changes, the
  columns do not, so no ALTER. But the registered scheduled query embeds a copy of the SQL, so
  it is `bq update --transfer_config` in dev and prd in the same sitting.
- **KTD10. `/historikk` segments it, it does not blend it.** A fourth segment, `removed`, is
  discriminated on the prefix ahead of `relayed` (removed rows carry `et_cancelled = TRUE` because
  that is what makes `impact._cancelled_prediction` serve the certainty). Prefix CASEs:
  `historikk_summary.sql`, `historikk_decisions.sql`, `meldinger_summary.sql`,
  `meldinger_filters.sql`, `data/queries/message_wording_comparison.sql`. `historikk.py`'s
  `Segment` fields and both `SummaryStrip.tsx` gain the segment.

### Bounds

- Build: ≤ 0.5 GB per hourly rebuild; ~24 rebuilds a day per environment.
- Table: ~200 trains × ~15 calls ≈ 3 000 rows on a bad day.
- Digest: 1 push per registration per day, plus a replacement only when the set changes; ≤ 6
  trains named.
- Decision rows: one per (registration, removed train) per cycle it is matched in, as for an ET
  cancellation; 601 such pairs on 23 September. Read the growth in dev (U2).

### High-Level Technical Design

```
poller cycle (hourly TTL)                             consumers
--------------------------                            ---------
rail_planned_calls ─► rail_calls_planned_today ─┬───► predict_calls.sql features (unchanged)
                                                │     varsel_connectivity, togvarsel (unchanged)
GTFS d, d-7..d-28 ─┐                            │
                   ├─► rail_removed_calls ──────┴─► rail_calls_removed_today
ET trainnos ───────┘                                   │                │
                                                       ▼                ▼
                               predict_calls.sql et_only (KIX:PlanRemoved:)   rail_planned_cache
                                                       │                     (journey_status spine)
                                   prediction cache ───┤                        │
                              ┌────────────┬───────────┼──────────┐              ▼
                              ▼            ▼           ▼          ▼        «I dag» strip
                           /impact     card       journey-alerts  check_and_notify
                                                  (pull, confirmed) ├─ per-train: skip removed
                                                                   └─ digest ≥ 05:00, 1 per reg/day
                     notification_events: skip removed (R5)
                     label_varsel_decisions.sql: by train number (U5)
```

### Integration constraints and risks

- **False removals from a timetable change.** The standing rule reads a new timetable period as
  removals for up to two weeks per weekday wherever netting misses: a renumbering, or a retiming
  of more than 15 min. Mid-December is the next change. The replay has to include the last
  timetable change in GTFS history (find it by the day-over-day trip-count jump), and the flag is
  the rollback.
- **A persisted cut stops being «removed» after two weeks**, when the train falls below 3 of 4
  reference weekdays. A cut that lasts is the new plan; the pushes stop by construction.
- **Bus for train.** A removed train covered by a replacement bus is still removed; the digest does
  not mention the bus (the card's alternative lookup may offer it through Journey Planner).
- **prd.** `#217`/`#218` (the strip) are in dev only. The push path (U2, U4) reaches prd with the first
  prd deploy after they merge, strip or no strip, which is why the flag defaults to `off`.
- **`data/queries/rail_calls_planned_today.sql` is a stale twin** (no two-partition fix, no
  readers). Leave it alone; it is not the live SQL.
- **Exact-arity stubs** (`exact-arity-test-stubs`): the spine loader's BigQuery stub and any
  `notification_wording` stub change shape. Grep and widen them in the same commit.

---

## Implementation Units

### U1. The removed-trains table, behind a flag, and the replay

- **Goal:** `rail_calls_removed_today` is built hourly in dev with the flag on, and the replay
  against the strike reports has passed.
- **Requirements:** R1-R3, R10, R11. Implements KTD1-KTD3.
- **Files:** `disruptions/api/rail_calls_removed_today.sql` (new), `disruptions/api/main.py`
  (phase `rail_removed_calls`, `VARSEL_PLAN_REMOVED`, empty-table bootstrap in `lifespan`),
  `disruptions/api/test_poller_refresh.py` (phase tuple, flag off → empty rebuild, failure keeps
  the cycle), `data/queries/validate_varsel_sql.sh` (register the file), `helm/kix-avvik/env/values-kub-ent-{dev,tst,prd}.yaml`
  (`VARSEL_PLAN_REMOVED`: `on` in dev, `off` in tst/prd), a replay script under
  `data/queries/` parameterised by date.
- **Approach:**
  1. Write the SQL per KTD3, parameterised by `@d` (default `CURRENT_DATE('Europe/Oslo')`) so the
     replay and the live build are one file.
  2. Dry-run it (bytes), then run the replay for 2026-09-11..23 into a scratch table in dev, and
     compare per-registration counts with `removed3.sql`'s output for the same days (the table
     joined to prd's `varsel_decisions` registrations the way `removed3.sql` does).
  3. Run it for the dates around the last timetable change; examine any jump.
  4. Wire the phase after `rail_planned_calls` inside the same TTL gate; bootstrap the empty table.
- **Test scenarios:** phase present and ordered; flag off rebuilds empty; a raise in the step is
  logged and the cycle's later phases run; bootstrap creates the table only when absent.
- **Verification:** replay criteria met (Success Criteria), and no replayed removed (line, train
  number) appears in that day's ET (the reinstatement anti-join, checked where it lives); in dev, `poller_phase` for
  `rail_removed_calls` is logged hourly with its `ms`; the table's row count after 03:10 matches
  the replay for that date.

### U2. Relay into the prediction cache, with no push yet

- **Goal:** removed trains appear on the card, `/impact` and the pull read as cancellations under
  `KIX:PlanRemoved:`; no browser push and no webhook event carries one.
- **Requirements:** R4-R6. Implements KTD4, KTD6, KTD7.
- **Files:** `disruptions/api/predict_calls.sql`, `disruptions/api/varsel.py` (collect-loop and
  write-queue exclusions), `disruptions/api/notification_events.py` (skip reason),
  `contracts/notifications/v1/openapi.json` and `event.schema.json` (the `phase: confirmed` and
  `journey_ref` descriptions; journey-alerts' description gains the one exception),
  `docs/integrations/notification-api.md`, `disruptions/api/test_varsel_api.py`,
  `disruptions/api/test_notification_events.py`, `disruptions/api/test_notification_contract.py`
  if it pins those texts.
- **Approach:** the UNION and CASE in `predict_calls.sql`; one constant,
  `PLAN_REMOVED_PREFIX = "KIX:PlanRemoved:"`, in `notification_wording` (a leaf; `varsel` already
  imports it, and `notification_events` may, once the import pins in `test_notification_*` are
  checked).
- **Test scenarios:** a removed alert is alert-worthy on the card and absent from `per_device`; it
  is not queued for a wording; `notification_events` skips it with the new reason; an ordinary
  ET cancellation beside it still pushes; the SQL's output for a removed call carries 1.0/NULL
  and the prefix (dry run plus one dev read).
- **Verification:** in dev after 03:10, `GET /varsel-api/journeys` for a registration on a cut line
  shows the boxes; `push_capped` and the push log carry none; decision rows appear under the prefix.

### U3. Removed trains on the strip

- **Goal:** the strip shows removed trains as red cancellations labelled «tatt ut av
  rutetabellen».
- **Requirements:** R9. Implements KTD8.
- **Files:** `disruptions/api/main.py` (`refresh_planned_spine`), `disruptions/api/journey_status.py`
  (`TrainStatus.removed_from_plan`), `disruptions/api/test_journey_status.py`,
  `disruptions/api/test_poller_refresh.py`, `contracts/notifications/v1/openapi.json`,
  `integrations/notifications-client/test/wireContract.test.mjs`, `demo/avviksvarsel/src/api.ts`,
  `demo/avviksvarsel/src/components/dagensReiseModel.ts`,
  `demo/avviksvarsel/test/dagensReise.test.mjs`.
- **Test scenarios:** a removed train in the window is red, cancelled, `removed_from_plan`, ring
  before and filled after its aimed departure; `removed_from_plan` false on every ET train; the
  label.
- **Verification:** in dev, the strip for a cut registration agrees with the digest's list.

### U4. The morning digest

- **Goal:** one push per registration per day naming its removed trains, from 05:00.
- **Requirements:** R7, R8. Implements KTD5. Depends on U3 (the digest's set is the spine's).
- **Files:** `disruptions/api/notification_wording.py` (`removed_digest`),
  `disruptions/api/varsel.py`, `disruptions/api/test_varsel_api.py`,
  `disruptions/api/test_notification_wording.py`.
- **Approach:** per KTD5. Weekday name from the Oslo date. Six named at most.
- **Test scenarios:** 04:59 nothing, 05:00 one push; three removed trains → one push naming three,
  cap spent by one; 38 → six named, «og 32 til»; a second cycle sends nothing; a registration
  created later gets its own digest; a grown set replaces under the same tag at no cap cost; a
  failed send re-offers all; the digest plus four real alerts → the fourth real one is capped;
  departed removed trains are not named; `[dev]` prefix; the body's first line is the route.
- **Verification:** in dev on the first weekday, the digest count equals the number of
  registrations with removed trains in their window, each at or after 05:00, read from the push
  log and `varsel_decisions`.

### U5. Labels and the `/historikk` segment

- **Goal:** removed-train decisions get verdicts and their own segment.
- **Requirements:** Success Criteria (labelling). Implements KTD9, KTD10.
- **Files:** `data/queries/label_varsel_decisions.sql`, the five prefix CASEs in KTD10,
  `disruptions/api/historikk.py`, `disruptions/api/meldinger.py` if its segment model mirrors
  it, `demo/varselhistorikk/src/{api.ts,components/SummaryStrip.tsx}`,
  `demo/meldinger/src/...` likewise, their tests; `.claude/rules/historikk.md`.
- **Approach:** read `.claude/rules/historikk.md`'s labelling section before editing. Dry-run the
  labeller; update the transfer config in dev, then prd, in the same sitting.
- **Verification:** the first nightly run after the update writes verdicts for ≥ 90 % of the
  previous day's removed rows; `/historikk` shows the `removed` segment beside the others.

---

## Verification Contract

- Every changed Python suite run as CI runs it (`uv run python <suite>.py`, count checked, not
  just exit code), plus the `demo/avviksvarsel` and `integrations/` Node suites for U2 and U3.
- Every changed SQL dry-run by hand before merge, and `validate_varsel_sql.sh` run; nothing in CI
  executes SQL.
- U1's replay, U2's and U4's dev readings and U5's first nightly are recorded in the PR that
  follows each.

## Definition of Done

- U1-U5 merged and deployed to dev, each read there as its Verification says.
- `.claude/rules/varsel.md` (relay, digest, strip), `.claude/rules/notification-webhooks.md` (the
  skip) and `.claude/rules/historikk.md` (segment, labeller) updated. `CLAUDE.md`'s
  `predict_calls.sql` paragraph notes the relay's second source.
- The prd flag decision put to the user with the replay and one dev weekday in hand.

## Open Questions

- **Webhook events for partners.** Per-train `confirmed` events would spend a subscription's cap at
  03:00; a digest event is a contract change. Default: none in v1, the pull read serves them.
- **A correction when a removed train comes back.** Default: none; the strip and card show the
  truth within the hour, and U5 scores the digest's claim a false alarm.
- **The lines with boarding restrictions.** Admitting F4, RX11, R31 and RE30 needs each removed
  train's own activities from its last ET appearance (an anshar partition read per day, ~10-20 GB).
- **Word choice.** «tatt ut av rutetabellen» (this plan) or «innstilt». The owner may prefer one
  word everywhere; the pull read's model wording already says «innstilt».
- **Tomorrow's removals.** Only from ET's ~22 h horizon, which is noisier than GTFS.

## System-Wide Impact

- Prediction cache: more certainty rows on cut days (~3 000 calls at 23 September's rate), no model
  work.
- `varsel_decisions`: one row per (registration, removed train) per matched cycle.
- The daily cap: one slot per registration on cut days.
- BigQuery: ≤ 0.5 GB an hour per environment, plus U5's labeller reading nothing new.
- Rushvarsel, the no-SX layer and the models' features: untouched by construction (KTD1).

## Sources and Research

- `docs/reports/streik-2026/rapport-2026-09-23.html` and the generator's `removed3.sql`
  (session c486abb0 scratchpad): the rule, the 147/92 figures, the per-journey distribution.
- `docs/rushvarsel/02-removed-journeys-2026-09-21.md`, `04-cuts`: the three shapes; the owner's
  2026-09-21 ruling.
- `disruptions/api/togvarsel_outcomes_settled.sql`: the production `standing` CTE and the RE20
  caveat.
- `disruptions/api/predict_calls.sql`: `et_cancelled` / `et_only`, the feature CTEs KTD1 protects.
- `disruptions/api/varsel.py` ~5120-5170: the cap and the collect loop.
- `contracts/notifications/v1/{openapi,event.schema}.json`: `phase`, `journey_ref`.
- GTFS `gtfs_last_recorded_ent_v1` schema (no pickup/drop-off types) and `last_recorded` ET schema
  (no boarding activities), read 2026-09-23.
