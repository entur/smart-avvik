# Operator census — can the per-call models generalize beyond NSB?

Session date: 2026-07-03. Question: what would it take to build the per-call
delay/cancellation models (the `training_calls` → 11-model family → platt/lead-time
calibration → `/disruptions` API stack) for participantRefs other than `NSB`?
Decision taken: **census first, no model build yet.** When a build happens, the agreed
direction is **pooled models** (one multi-operator training table, operator as a feature)
and **train + eval before any serving change**.

All numbers below were measured this session with cheap queries (every query dry-run
first; total census cost well under $1, no anshar full scans).

## TL;DR

- **GOA (Go-Ahead Norge) is a drop-in.** Same `DatedServiceJourney` refs on both sides,
  **96%** SX↔ET join rate, and very strong labels: affected calls are **48% cancelled vs
  0.3% baseline**. ~33k labeled call rows over ~17 months (≈5% of NSB's volume → pool, don't
  train standalone).
- **RUT (Ruter) is NOT journey-blind — we just don't extract its refs.**
  `create_disruptions.sql` reads `framedVehicleJourneyRef` + `datedVehicleJourneyRefs` but
  not the plain `affects.vehicleJourneys.vehicleJourneyRefs` field, where RUT puts
  `RUT:ServiceJourney:<hash>` refs on **77%** of its messages. With that one field added,
  RUT joins ET at **99.3%** and flagged calls show 8.7% cancellation vs 0.04% baseline
  (~100k labeled calls / 30 days). Caveat: RUT's journey-scoped messages are *reactive*
  per-departure notices, so the modeling value shifts toward downstream-call propagation.
- **`ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded` makes the
  historical build nearly free** (722 GB total, clustered by `dataSource`, one row per call,
  cancellation flags at 94% parity with `rail_calls`) — vs the ~10 TiB / $63 anshar scans.
  Training source only; live serving keeps the anshar TTL scan.
- **SJN is blocked** (ET refs unparseable since Dec 2021; a train-number+date join recovers
  only 65% of a tiny volume). **FLT** mostly publishes >7-day-validity messages that our
  duration cap drops. **GCO** — the biggest SX publisher at 122k situations/yr — turns out
  to be automated demand-responsive boat/ferry notices, not human disruption messages.

## Operator-by-operator verdicts

SX volumes from `disruptions.disruptions` (12 months to 2026-07-03); joins measured against
`last_recorded` unless noted. Names from `last_recorded.dataSourceName`.

| participantRef | Who | SX situations/yr | Journey refs | ET ref match | Join rate | Verdict |
|---|---|---|---|---|---|---|
| NSB | Vy (rail) | 49,285 | 89% (`VYG:DatedServiceJourney:`) | ✓ (May 2025→) | — (in prod) | Baseline |
| GOA | Go-Ahead Norge | 3,229 | 100% (`GOA:DatedServiceJourney:`) | ✓ (Feb/Mar 2025→) | **96%** | **Drop-in** |
| RUT | Ruter | 32,426 | 0% extracted; **77% in unread `vehicleJourneyRefs`** | ✓ (`ServiceJourney`+date) | **99.3%** | **Extraction fix, then joinable** (reactive-message caveat) |
| KOL | Kolumbus | 9,847 | 0% extracted; 73% in `vehicleJourneyRefs` | ✓ | not measured | Same extraction fix as RUT |
| VYG (SX) | Vygruppen (non-NSB) | 4,599 | 0% extracted; 100% in `vehicleJourneyRefs` | ✓ | not measured | Same extraction fix |
| GCO | unconfirmed (automated DRT/boat publisher) | 122,498 | 100% (`<CS>:ServiceJourney:` across KOL/SOF/SKY/NOR/ATB/OST/…) | ✓ | **98.4%** | Joinable, but content = auto "Bestillingsanløp"/"Ingen bestilling mottatt" notices — separate product question |
| MOR | FRAM (Møre og Romsdal) | 20,721 | 97% | ✓ | 87.8% | Joinable (bus project) |
| NOR | Reis Nordland | 2,429 | 96% | ✓ | 99.0% | Joinable (bus project) |
| TRO | Svipper (Troms) | 2,792 | 73% | ✓ | 98.3% | Joinable (bus project) |
| FIN | Snelandia (Finnmark) | 1,357 | 92% | ✓ | 98.3% | Joinable (bus project) |
| SKY | Skyss | 6,612 | 18% | ✓ | not measured | Partial refs only |
| SJN | SJ Nord | 235 | 100% (`SJN:DatedServiceJourney:`) | ✗ ET legacy `<trainnr>:<date>`; no parseable refs in `last_recorded` since 2021-12-22 | 65% via train-number+date (45/69, 60 d) | **Blocked** — custom join possible, volume too small to justify |
| FLT | Flytoget | 1 in table; **204/30 d in raw SX** | 100% | ✓ (`ServiceJourney`) | not measured | 184/204 situations exceed the 7-day duration cap in `create_disruptions.sql` (long-validity info messages); ~20/30 d usable — marginal, single line |

Label richness for the two viable candidates (affected/flagged calls vs operator baseline):

| Operator | Window | Labeled calls | Cancel rate (affected vs base) | p90 dep delay (affected vs base) |
|---|---|---|---|---|
| GOA | 2025-02 → now (~17 mo) | 32,967 (2,218 journeys) | **48.1% vs 0.3%** | 27 vs 6 min |
| RUT | 30 days | 100,120 (1,975 trips) | **8.7% vs 0.04%** | 26 vs 5 min |

RUT message character (90 d, from `disruptions.disruptions`): 3,534 per-departure delay
echoes ("Avgangen er ca. 15 minutter forsinket", no line/stop scoping — these carry the
unextracted journey refs), 2,297 stop-skip notices ("Avgangen stopper ikke på X"), 1,464
line-scoped incidents. The echoes describe already-materialized delays, so a RUT model is
less "predict the impact of an incident" and more "given this departure is flagged, forecast
its downstream calls" — which the existing propagation features
(`own_prev_delay`, `line_now_*`) already model.

## The `vehicleJourneyRefs` extraction gap

`create_disruptions.sql` builds `affected_journeys` from
`affects.vehicleJourneys.framedVehicleJourneyRef.datedVehicleJourneyRef` and
`affects.vehicleJourneys.datedVehicleJourneyRefs` — but never reads the plain repeated
`affects.vehicleJourneys.vehicleJourneyRefs` field. Raw SX, 30 days:

| participantRef | messages | refs in plain `vehicleJourneyRefs` | refs in extracted fields |
|---|---|---|---|
| RUT | 7,207 | **5,573** | 0 |
| KOL | 703 | **516** | 66 |
| VYG | 316 | **316** | 0 |
| NSB | 8,811 | 367 | 8,079 |

A one-field fix (third UNION branch in the `affected_journeys` subquery) unlocks RUT/KOL/VYG
journey scoping and adds ~4% more NSB refs. Note `ServiceJourney` refs are **not
date-scoped**, so downstream joins need `operatingDate` (from SX creation date / validity)
in the key — unlike the `DatedServiceJourney` refs the current jkey join relies on.

## `realtime_siri_et_last_recorded` — historical calls for ~cents

`ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded` (suggested by
Thomas) is a daily-refreshed, one-row-per-call flattening of the final recorded state of
every journey, all operators, Q1 2020 onward. 722 GB / 2.9 B rows, partitioned by
`operatingDate`, clustered by `dataSource`+`lineref` — the GOA rail slice is ~1–2 GB per
full-history pass, vs `rail_calls`' one-time ~10 TiB / $63 anshar scan. Verified 2026-07-03:

- **jkey-compatible**: `serviceJourneyId` carries whatever ref the feed used — for VYG/GOA
  rail that is the `DatedServiceJourney` ref (same jkey as SX after prefix-strip); for bus
  operators the `ServiceJourney` ref.
- **Cancellations captured**: May 2026 VYG rail = 3,592 cancelled calls vs 3,808 in
  `rail_calls` (**94%**); calls 406,794 vs 385,669; journeys 28,367 vs 31,350. The deltas
  look like replacement-bus/extra-journey handling — reconcile before trusting labels
  blindly, but it is not the estimatedCalls-cancellation hole `rail_calls` fixed.
- **Replacement buses separable**: the VYG uuid-`ServiceJourney` rows are
  `extraJourney=true, vehicleMode='bus'` (buss for tog). Filter `vehicleMode='rail'`.
- **No extra history**: pre-2025 `serviceJourneyId` is uuid-based — 0 of 21,173 autumn-2024
  NSB SX refs join. The SX↔ET boundary stands: labeled data from ~May 2025 (NSB) /
  ~Feb–Mar 2025 (GOA). GOA also has a hole Oct 2024–Jan 2025.
- **Limits**: daily refresh → **training only**, live serving keeps
  `rail_calls_planned_today`'s anshar TTL scan; `stopPointRef` is quay-level
  (`NSR:Quay:…`) → needs a quay→StopPlace mapping for `stop_place` feature parity;
  no per-message creationTime (only final `recordedAtTime`) — fine for outcomes, SX
  provides the onset clock. Feature parity otherwise checks out: `sequenceNr` → stop_order,
  NULL aimed times → terminal/origin, delays from `departureTime − aimedDepartureTime`.

## Where operator scope is hard-coded today

Every scoping is one of two literals: `participantRef = 'NSB'` (SX side) or
`lineRef LIKE 'VYG:%'` (ET side). No dataSource/codespace parameterization exists.

- **Calls tables (ET)**: `create_rail_calls.sql:46`, `create_rail_calls_new.sql:23`,
  `update_rail_calls.sql:46`, `update_rail_calls_new.sql:27`,
  `disruptions/api/rail_calls_today.sql:26`, `rail_calls_planned_today.sql:39,80`
  (both copies; :80 is the GTFS `agency_id LIKE 'VYG:%'` stop-name lookup).
- **Training**: `create_training_calls.sql:81` (the sole operator filter; models in
  `train_call_models.sql` and `eval/impact_eval.py` inherit from `training_calls`).
- **Calibration**: `create_leadtime_calibration.sql:46`, `leadtime_eval.sql:24`.
- **Serving**: `predict_calls.sql:55`, `predict_calls_today.sql:62`,
  `disruptions/api/disruptions_today.sql:34` (transitively scopes
  `api/predict_calls.sql`), `disruptions_as_of.sql:30`.
- **Lookups**: `create_category_line_hour_impact.sql:62` (+ `VYG:Line:` REPLACE at :75,:113),
  `create_trip_outcomes*.sql`, plus the duration-model family
  (`create_training.sql`, `create_disruptions_initial.sql:42`, …).
- **API**: `main.py:1393` hard-codes the `VYG:Line:` prefix; :793,:863,:1102 assume the
  prefix-strip convention. `create_disruptions.sql` is the only operator-agnostic stage.
- **jkey convention everywhere**: `REGEXP_REPLACE(ref, r'^[A-Za-z]+:', '')` — works for any
  codespace, but silently assumes date-scoped refs (true only for `DatedServiceJourney`).

## Recommended build plan (not executed)

1. **Pooled calls table** for rail from `last_recorded` (`dataSource IN ('VYG','GOA')`,
   `vehicleMode='rail'`), with `codespace` column and quay→StopPlace mapping. ~cents.
2. **Extend `create_disruptions.sql`** with the `vehicleJourneyRefs` UNION branch (also
   backfills NSB refs; one ~$0.01-class SX rebuild).
3. **Pooled `training_calls`** with `participantRef` as a feature; retrain the 11-model
   family (~$0.50–1 per family per `train_call_models.sql` header; global-explain retrain
   ≈ 5.4 h wall if promoting). Refit platt + lead-time layers **per operator** — pooled
   calibration would let NSB's base rates swamp GOA's.
4. **Per-operator eval** via `eval/impact_eval.py`, gated on **NSB parity** with current
   prod models before anything ships; GOA judged on its own precision/recall.
5. Only then: parameterize serving (`predict_calls*.sql`, `rail_calls_planned_today.sql`,
   `disruptions_today.sql`) and the API's line-prefix handling.
6. RUT/bus is a separate follow-on: same recipe but `ServiceJourney`+`operatingDate` join
   keys, ~30x call volume, urban-mode delay dynamics, and a product question about
   reactive-notice alerting value.

## Open questions

- **What is GCO?** Not in the public codespace registry; `dataSourceName` is NULL;
  behaviorally an automated publisher of per-sailing demand-responsive boat notices across
  county codespaces (KOL 8.6k, SOF 5.9k, SKY 4.1k situations/90 d, …). Ask colleagues —
  if some slice is real cancellations ("Innstilt"), boat alerting could be cheap to add.
- Should `rail_calls` itself migrate to `last_recorded` (kill the $63 rebuild scans) after
  reconciling the 6% cancellation-count gap?
- Is the 7-day duration cap in `create_disruptions.sql` right, given it drops ~90% of FLT
  and possibly other operators' long-validity situations?
- MOR's 87.8% join rate (vs 98–99% for peers) — dig into the missing 12% if FRAM ever
  matters.
