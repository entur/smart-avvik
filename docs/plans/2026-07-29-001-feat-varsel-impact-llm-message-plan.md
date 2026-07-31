---
title: "feat: /varsel-api on top of /impact, with observations, alternatives and an LLM-written message"
date: 2026-07-29
type: feat
depth: deep
origin: none (direct request)
---

# feat: /varsel-api on top of /impact, with observations, alternatives and an LLM-written message

## Summary

Today `/varsel-api` and `/impact` are two independent readers of the same in-process per-call
prediction snapshot: `varsel.match_journey` re-implements the situation × line × train matching that
`main._trip_cells` already does, and each has its own severity ordering and its own idea of what a
"trip" is. This plan makes `/impact` the single matching-and-shaping engine, enriches it with three
new pieces of information a traveller actually needs (the full situation text, what has been
*observed* on the line so far, and whether a faster alternative exists), and replaces the
hand-assembled Norwegian push text with a short message written by Claude Sonnet 5 on Vertex AI from
that enriched payload.

The `/impact` HTTP contract stays backward compatible: every change is an added field. The external
consumer (`togpuls`, `togpuls/clients/disruptions.py`) sends `from_stop` / `to_stop` / `at` and reads
only `situations[].situation_number` — verified against the current file on `main` — so additions
cannot break it.

## Problem Frame

Four problems, in the order they bite:

1. **Duplicated matching logic.** `varsel.match_journey` (varsel.py:341) and `main._trip_cells`
   (main.py:1530) both group a situation's scored calls by dated journey, find the boarding call at
   `from_stop`, check that the same train later calls at `to_stop`, and pick a worst case — with
   *different* severity keys (`varsel._severity` orders on `p_impact_dep`, `main._worst` orders on
   `p_cancel`). Two implementations of one concept, already drifted.
2. **The push text is assembled from probabilities, not from information.** The current payload is a
   template over `p_impact_dep`, `p_cancel` and a delay interval. It cannot say "your train is
   already 18 minutes late at Asker" or "there is a train 12 minutes later that gets you there
   sooner", because `/impact` does not carry either fact.
3. **Situations are truncated to `summary`.** The SX `description` — the operator's actual
   explanation — is already in the source tables and is thrown away before it reaches any surface.
4. **Work is duplicated per device.** `check_and_notify` iterates journeys, and two devices that
   registered the same trip each drive a full independent match. Once each journey costs an LLM call,
   that duplication becomes a real cost and a consistency bug: two travellers on the same train
   could receive differently-worded advice.

## Requirements

| ID | Requirement | Source |
|---|---|---|
| R1 | `/varsel-api` obtains its per-journey disruption picture by calling `/impact`, not by re-implementing the matcher. `/impact` may change as long as `togpuls/clients/disruptions.py` keeps working. | request 1 |
| R2 | Each situation carries `description` in addition to `summary`. | request 2 |
| R3 | Each line carries `latest_observation` with `cancelled` (bool), `at_stop` (the latest stop on that line we have information about) and `delayed_min` (minutes late at `at_stop`). | request 3 |
| R4 | When `to_stop` is given, consult the Entur Journey Planner for a faster journey `from_stop` → `to_stop`; if one exists, return it as `alternative_journey`. | request 4 |
| R5 | The complete `/impact` output (including descriptions, latest observations and any alternative journey) is sent to Claude Sonnet 5 on Vertex AI, and Claude's reply — a brief message with journey information and advice — becomes the pushed message. | request 5 |
| R6 | Multiple devices registered on the same journey receive identical output, produced by a single `/impact` call and a single Claude call per journey. | request 6 |
| R7 | The work ships on a branch with a pull request. | request 6 (second) |

### Success criteria

- `/impact` responses gain `description`, `operator`, `latest_observation` and `alternative_journey`
  and lose nothing; a request shaped exactly like `togpuls.fetch_impact`'s still parses.
- `varsel.match_journey` no longer contains matching logic of its own — it consumes impact cells.
- A push for a live rail disruption carries Claude-written Norwegian text; with Vertex unreachable
  the same push still goes out with the existing deterministic wording.
- Two devices on the same registered trip get byte-identical message text, with one `/impact` and
  one Claude call recorded per refresh cycle.

---

## Scope Boundaries

### In scope

- `disruptions/api/main.py` — trip-cell matcher, `/impact` models and endpoint, provider injection.
- `disruptions/api/varsel.py` — matcher removal, per-journey compute cache, notification body.
- `disruptions/api/predict_calls.sql` / `predict_calls_bus.sql` — two new carried-through columns
  plus `description`.
- New `disruptions/api/llm_message.py` (Vertex Claude client) and the Journey Planner alternative
  lookup (in `main.py` or a small new module — see U4).
- `demo/avviksvarsel/` — surface the message in the journey list.
- Tests, `requirements.txt`, `CLAUDE.md`, Helm/`.entur` config for the new env vars.

### Deferred to follow-up work

- **Lowering the planned-source TTL** to make `latest_observation` fresher than 60 min (rail) /
  120 min (bus). It costs real BigQuery money (`_PLANNED_TTL` at 60 min already scans ~190 GB/day);
  decide with a measurement, not in this PR. See Risk 2.
- **Alternative journeys for the bus families.** The Journey Planner call is mode-agnostic, but
  "faster" is only well-defined against a predicted arrival, and the bus lead-time findings
  (docs/pooled-models-bus/LEARNINGS.md) mean several operators publish the SX after departure. Ship
  rail + the trained bus operators; do not tune bus-specific alternative logic here.
- **Prompt/eval loop for the message text.** This PR establishes the call, the guardrails and the
  audit log. Systematically evaluating message quality (an eval set of situations → judged messages)
  is its own piece of work.
- **`togpuls`' hardcoded base URL.** `DISRUPTION_IMPACT_URL` in that repo still points at the
  decommissioned Cloud Run host (`kix-avvik-disruptions-…run.app`), so its `/impact` calls already
  fail today. Not ours to change in this PR; worth a heads-up to its author.

### Out of scope

- Retraining or recalibrating any model. No `*_platt` refit, no `copy_runtime_to_prd.sh` run beyond
  what new columns require.
- Changing the alerting threshold or the definition of "trouble" (`TROUBLE_DELAY_MIN`,
  `_OPERATOR_ALERT_THRESHOLD`). The LLM writes prose about the existing decision; it does not make
  the decision.
- `/demo`, `/eval`, `/reisevalg-api` behaviour.

---

## High-Level Technical Design

*Directional guidance for review — not implementation specification. The prose above and the
per-unit sections below are authoritative where they disagree.*

### Where the pieces sit

```mermaid
flowchart TB
    subgraph BQ["BigQuery (disruptions dataset)"]
        PLAN["rail_calls_planned_today<br/>bus_calls_planned_today<br/>(dep_delay_min, call_cancelled, call_source)"]
        PRED["call_predictions_today<br/>call_predictions_bus_today<br/>+ description, dep_delay_min, call_source"]
        PLAN --> PRED
    end

    PRED -->|"poller tick"| CACHE["_call_pred_by_situation<br/>(in-process, one instance)"]

    CACHE --> CELLS["_trip_cells<br/>window-based matcher"]
    CELLS --> IMPACT["/impact response builder<br/>situations[] + lines[]"]
    JP["Journey Planner v3<br/>(TTL-cached, fail-open)"] --> IMPACT

    IMPACT -->|"HTTP GET /impact"| EXT["togpuls (external)"]
    IMPACT -->|"in-process provider"| VARSEL["varsel: window per journey,<br/>alert gate, per-journey cache"]
    VARSEL --> LLM["Claude Sonnet 5 on Vertex<br/>(fail-open → template body)"]
    LLM --> PUSH["Web Push, fanned out per device"]
    VARSEL --> LIST["GET /varsel-api/journeys"]
```

### One refresh cycle

```mermaid
sequenceDiagram
    participant P as poller thread
    participant V as varsel.check_and_notify
    participant I as impact provider
    participant J as Journey Planner
    participant C as Claude (Vertex)
    participant D as devices

    P->>V: refreshed snapshot
    V->>V: group journeys by canonical journey key
    loop once per distinct journey key
        V->>I: impact(from, to, window)
        I-->>V: situations[] + lines[] (+ latest_observation)
        opt to_stop set and a faster pattern exists
            I->>J: trip(from → to)
            J-->>I: alternative_journey
        end
        V->>V: apply per-operator alert gate
        opt alert-worthy and not cached this cycle
            V->>C: enriched payload + prompt
            C-->>V: short Norwegian message
        end
    end
    loop per (device, journey) pair
        V->>D: push (shared message text, per-device dedup)
    end
```

The two loops are the crux of R6: the expensive work is keyed by *journey definition*, the delivery
and its dedup are keyed by *device + journey*.

---

## Key Technical Decisions

### KTD1 — `/varsel-api` calls `/impact` in-process, through an injected provider

`varsel` never imports `main` (that rule predates this plan and stays). `main.configure(...)` gains
an `impact_provider` alongside the existing `snapshot_provider` / `stops_provider`; varsel calls it
and receives the same `ImpactResponse` object the HTTP endpoint returns.

Not an HTTP loopback call: the app is a deliberately single always-on instance with an in-process
cache (`deployment.forceReplicas: 1`), the matcher runs inside the poller thread, and a self-call
would add a request round trip plus a base-URL configuration for no isolation benefit. "Calls
`/impact`" is satisfied by going through `/impact`'s code path — one implementation, one severity
order, one response shape.

### KTD2 — the window is the primitive; `at` stays the public parameter

`_trip_cells` currently takes a point `at` and matches boarding calls within
`_IMPACT_AT_TOLERANCE` (±90 min). A registered journey is a recurring weekday + `[begin, end]`
window, which is the more general shape. So: `_trip_cells` takes an explicit
`(window_start, window_end)`, and the `/impact` endpoint derives that window from `at ± tolerance`.

This keeps the HTTP semantics bit-identical (same `at`, same tolerance, same per-line
nearest-to-`at` pick) while letting varsel pass a journey's actual local window without a second
matcher. Per-line selection differs by caller — `/impact` keeps "nearest to `at`", varsel needs
"every qualifying train, worst wins" — so the selection strategy is a parameter of the cell builder,
not a fork of it.

### KTD3 — every `/impact` addition is additive; nothing is renamed or removed

`togpuls.fetch_impact` sends `from_stop` / `to_stop` / `at` and reads `situations` (list) and
`situation_number` per entry. New fields (`description`, `operator`, `latest_observation`,
`alternative_journey`) are added; `situations` stays a list of objects keyed the same way; no field
changes type or name. `Alert` in varsel likewise only grows — `demo/avviksvarsel/src/api.ts` reads
`situation_number`, `summary`, `cancel_probability`, `delay_p50_min`, `delay_p90_min`,
`impact_probability`, `alert_threshold`, `alertworthy`, all of which stay.

### KTD4 — `latest_observation` comes from data already in the cache, not a new scan

`rail_calls_planned_today` / `bus_calls_planned_today` already carry `dep_delay_min` (realised
departure delay on recorded calls), `call_cancelled` and `call_source` (`recorded` vs `estimated`) —
that is exactly what the propagation features read. `predict_calls*.sql` already surfaces
`call_cancelled` as `et_declared_cancelled` but drops `dep_delay_min` and `call_source`. Carrying
those two through costs nothing (no new scan; the predict step reads the materialised planned
table).

Consequence to state honestly rather than hide: the observation is only as fresh as the planned
source, i.e. up to `_PLANNED_TTL` (60 min rail) or `_BUS_PLANNED_TTL` (120 min bus) old. The
response therefore carries `observed_at` so a consumer — and Claude — can see the age instead of
assuming "latest" means "now". Making it fresher is deferred (see Scope Boundaries).

### KTD5 — Journey Planner alternatives reuse the `reisevalg` HTTP patterns, and fail open

`disruptions/api/reisevalg.py` already calls `https://api.entur.io/journey-planner/v3/graphql` with
`ET-Client-Name: entur-kixavvik`, a 3 s timeout, a `_post_json` helper that treats a GraphQL
`errors` array as a failure, and a `_TTLCache` whose failure path serves the previous good value.
Reuse those patterns rather than inventing a second HTTP style.

"Faster" is defined against the disrupted train, not against the timetable: a JP `tripPattern`
qualifies when its expected arrival at `to_stop` is earlier than the matched train's predicted
arrival (aimed arrival plus the predicted arrival delay), **and** it does not ride the affected
journey. A missing, slow or erroring JP means the field is absent — never a blocked push.

### KTD6 — Claude Sonnet 5 on Vertex AI, structured output, `low` effort

Model ID is the bare `claude-sonnet-5` (Vertex takes no provider prefix); client is
`AnthropicVertex(project_id=…, region=…)` from `anthropic[vertex]`; auth is GCP ADC — the runtime SA
already holds `roles/aiplatform.user` (`.entur/application.yaml`), granted for the `AI.CLASSIFY`
step. Region from env, defaulting to `europe-west1` to keep inference in the region the rest of the
stack runs in, with `global` as a documented alternative.

Request shape, per current Sonnet 5 semantics:

- Adaptive thinking is **on by default** on Sonnet 5; set `thinking={"type": "adaptive"}` explicitly
  with `output_config={"effort": "low"}`. A 300-character Norwegian notification does not need depth,
  and `low` keeps the poller tick fast. Do **not** reach for `thinking: {"type": "disabled"}` — low
  effort is the cheaper lever and avoids the disabled-thinking failure modes.
- No `temperature` / `top_p` / `top_k` — non-default sampling parameters are rejected on Sonnet 5.
- No assistant prefill — it returns 400. Shape the output with
  `output_config={"format": {"type": "json_schema", …}}` (supported on Vertex) returning a single
  `message` string, which also removes "Her er meldingen:" preambles.
- `max_tokens` sized to leave room for thinking plus a short answer (≈600); non-streaming is fine at
  that size.
- Batches, the Files API and server-side `fallbacks` are not available on Vertex — none are needed.

**Pre-flight, before writing code:** confirm `claude-sonnet-5` is actually callable in
`ent-kixavvik-dev`. Anthropic publisher models need Vertex Model Garden enablement in addition to
`roles/aiplatform.user`, and this project has already been bitten by exactly that class of gap
("API service enablement ≠ IAM role", 2026-07-09). If the model is not enabled and enabling it is
not self-serve, everything except U5/U6's LLM path still ships and the deterministic body remains in
use — say so in the PR rather than merging a silently dead feature.

### KTD7 — compute is cached per journey definition; dedup stays per device

The canonical journey key is `(from_stop, to_stop, tuple(sorted(days)), begin, end)`. `/impact` and
Claude run once per key per refresh cycle.

The `_notified` dedup key must **not** become the journey key. It is currently
`"{journey_id}|{situationNumber}"`, i.e. per registration; collapsing it to the journey key would
mean the first device to be notified suppresses the push for every other device on the same trip.
Compute is shared; delivery and its dedup are not. Existing `_notified` entries stay valid because
the key format is unchanged.

### KTD8 — the LLM is fenced on both sides

*Input:* SX free text is publisher-authored and reaches the prompt verbatim, so the payload is
wrapped in an explicit data delimiter and the system prompt states that the content is data
describing a disruption, never instructions. Numbers are pre-rounded and pre-selected by our code;
the prompt forbids inventing figures, times or line names not present in the payload.

*What it may quote:* the headline probability is the calibrated impact probability
(`P(innstilling eller ≥ 10 min forsinkelse)`), matching `/demo` and the current wording.
`dep_delay_typ_min` is **never** placed in the payload — for alert-worthy boardings its median is
2 min against the exceedance stack's 15 min, and quoting it is exactly what made a 93 %-impact alert
read as "1 % sjanse for innstilling, typisk 2 min forsinket" before the 2026-07-27 fix.

*Output:* length-capped (~300 chars) for Web Push, and logged with its prompt under
`jsonPayload.event = "llm_message"`, mirroring the existing `event = "push"` audit trail — what was
said to a traveller must remain reconstructible.

### KTD9 — every new dependency fails open

Push delivery must never depend on Vertex or JP being up. `VARSEL_LLM_ENABLED` gates the LLM path
(off when no credentials resolve); on any exception, timeout, empty reply or over-long reply, the
body falls back to today's `_notification_payload` text. A per-cycle cap on Claude calls bounds the
poller tick even when many journeys alert at once.

---

## Implementation Units

**Sequencing.** U1 → U2 → U3 is the spine. U4 is independent and only has to land *before* U3's
`alternative_journey` wiring, so it can be built in parallel (it is numbered after U3 because it is a
smaller, self-contained side quest, not because it comes later). U5 needs U3's payload shape; U6 needs
U2, U3 and U5; U7 needs U6; U8 closes out. The Vertex access pre-flight in KTD6 should be run first
of all, since a negative answer changes what U5/U6 can claim.

### U1. Carry `description` and the observed-state columns through to `_CallPred`

**Goal:** make the situation's full text and the line's realised state available in the in-process
snapshot, with no new BigQuery scan.

**Requirements:** R2, R3.

**Dependencies:** none.

**Files:**
- `disruptions/api/predict_calls.sql` (modify)
- `disruptions/api/predict_calls_bus.sql` (modify)
- `disruptions/api/main.py` (modify — `_CallPred`, `_call_pred_from_row`)
- `disruptions/api/test_varsel.py` (modify — `FakeCall` gains the new fields with defaults)

**Approach:**
- In both predict SQL files, add `d.description` to the `sit` CTE projection and carry it to the
  final `SELECT` alongside `s.summary`. Preserve the Norwegian text filter already applied upstream
  in the `disruptions_*_initial_today` sources — do not add a second, divergent filter here.
- Carry `r.dep_delay_min` and `r.call_source` from the planned-call join (the `calls` CTE already
  joins that table for `et_declared_cancelled`) through to the final `SELECT`.
- Add `description`, `dep_delay_min`, `call_source` to `_CallPred` and map them in
  `_call_pred_from_row`. `_CallPred` is a frozen dataclass with positional construction only in that
  one mapper, so this is a contained change.
- The bus file mirrors the rail file; keep the two projections in step (they already are).

**Patterns to follow:** the existing `et_declared_cancelled` column is the precedent for surfacing an
observed (not predicted) field through this pipeline — follow its placement and naming style. Column
comments in these files explain *why* a column exists; match that density.

**Test scenarios:**
- `FakeCall` with `description=None` still constructs and the matcher behaves as before (proves the
  fields are optional at the consumer boundary).
- `_call_pred_from_row` maps a row exposing `description` / `dep_delay_min` / `call_source` onto the
  matching `_CallPred` attributes; a row where `dep_delay_min` is `NULL` yields `None`, not `0`
  (an unobserved call must be distinguishable from an on-time one).
- SQL validation is a dry-run, not a unit test: `bq query --dry_run` both files and confirm the
  scan estimate has not grown (the planned table is already read).

**Verification:** both predict queries run and the resulting tables contain the three new columns;
the API starts and serves `/journey` unchanged.

---

### U2. Make `_trip_cells` window-based, and carry the operator on each cell

**Goal:** one matcher that serves both a point-in-time `/impact` query and a recurring journey
window, and that exposes the operator the alert gate needs.

**Requirements:** R1.

**Dependencies:** U1.

**Files:**
- `disruptions/api/main.py` (modify — `_TripCell`, `_trip_cells`, `_worst`, `_severity_key`)
- `disruptions/api/test_impact.py` (new)

**Approach:**
- `_trip_cells(scope, from_stop, to_stop, window_start, window_end, *, select)` — boarding calls
  qualify when `window_start <= aimed_dep <= window_end`. `select` chooses the per-line boarding
  call: `nearest(at)` for `/impact`, `worst` for varsel. Keep `_IMPACT_AT_TOLERANCE` where it is and
  let the endpoint derive `at ± tolerance`; the tolerance is an `/impact` concept, not a matcher one.
- Add `operator` and `jkey` to `_TripCell` (both already available on the boarding `_CallPred`) so
  downstream code can pick a per-operator threshold and identify the affected journey for the
  Journey Planner comparison.
- Reconcile the two severity orders on the shared path. `varsel._severity` orders by
  `p_impact_dep` — the quantity the alert gate actually fires on — and the 2026-07-27 note in
  varsel.py records why ordering by `p_cancel` is wrong (it can hand the alert's numbers to a train
  other than the one that crossed the threshold). Move `_worst` / `_severity_key` to impact
  probability first, then p90 delay, then `p_cancel` as a deterministic tie-break. **This changes
  `/impact`'s ordering** for situations affecting multiple lines: it is a deliberate correctness fix,
  not an accident, and it must be called out in the PR body.

**Execution note:** write the window-matcher tests first. This unit changes a function two surfaces
depend on, and the edge cases (window boundaries, direction, multi-line situations) are exactly what
a characterisation test should pin before the refactor.

**Test scenarios:**
- A boarding call exactly at `window_start` and one exactly at `window_end` both match; one a minute
  outside either edge does not.
- `select="nearest"` with two trains on one line 20 and 70 min from `at` picks the 20-min one;
  `select="worst"` picks the higher-`p_impact_dep` one regardless of distance from `at`.
- Direction: a train calling at `to_stop` with a *lower* `stop_order` than the boarding call does not
  produce a cell (wrong direction); with a higher `stop_order` it does.
- A situation affecting two lines produces two cells, and the rolled-up `SituationImpact` takes the
  numbers from the line with the higher `p_impact_dep` — not the higher `p_cancel`.
- `to_stop=None` produces cells with `arrival is None` and no crash.
- Bus-vs-rail jkey collision: two operators carrying the same `jkey` in one situation produce
  separate cells (the `(operator, jkey)` grouping must survive the refactor).
- `operator` on the cell equals the boarding call's operator, including for a pooled bus row.

**Verification:** `/impact` responses for a live situation are unchanged except for the documented
ordering fix; the new tests pass standalone and under pytest.

---

### U3. Add `description`, `operator`, `latest_observation` and `alternative_journey` to `/impact`

**Goal:** the `/impact` payload carries everything a message writer needs, additively.

**Requirements:** R2, R3, R4.

**Dependencies:** U1, U2, U4 (for the `alternative_journey` wiring).

**Files:**
- `disruptions/api/main.py` (modify — `SituationImpact`, `LineImpact`, new `LatestObservation`,
  new `AlternativeJourney`, `ImpactResponse`, the `impact` endpoint)
- `disruptions/api/test_impact.py` (modify)

**Approach:**
- `SituationImpact` gains `description: str | None`, taken from the group's boarding call.
- `LineImpact` gains `operator: str | None` and `latest_observation: LatestObservation | None`.
- `LatestObservation` carries `cancelled: bool`, `at_stop: str | None` (StopPlace ref),
  `at_stop_name: str | None`, `delayed_min: int | None` and `observed_at: datetime | None`. The
  requirement names three fields; the stop name and the observation time are added because a message
  writer needs a human stop name, and because the freshness bound in KTD4 must be visible rather
  than implied.
- Resolution rule for a line's latest observation: among that line's calls in the situation's scope,
  keep those with an observation (`call_source == 'recorded'`, or `et_declared_cancelled` true), and
  take the one with the greatest `aimed_dep`, tie-broken by `stop_order`. **Scope caveat to write
  into the docstring:** the snapshot only holds calls belonging to affected journeys, so this is "the
  latest stop on this line *that a live situation touches*", not a whole-line view. Prefer an
  observation on the traveller's own resolved train when one exists, since that is the most
  actionable; otherwise fall back to the line.
- `alternative_journey: AlternativeJourney | None` sits on `ImpactResponse` (one per trip, not per
  line — the traveller is choosing one way to get there), populated only when `to_stop` is given and
  U4's provider returns something faster than the worst affected line's predicted arrival.
- `ImpactResponse` docstring documents each addition and the freshness bound.

**Test scenarios:**
- A response built from cells where no call has an observation yields
  `latest_observation is None` — absent, never a zero-filled placeholder.
- A line with recorded calls at stop_order 1 (5 min late) and 3 (18 min late) reports
  `at_stop` = the stop_order-3 stop and `delayed_min == 18`.
- A call flagged `et_declared_cancelled` yields `cancelled: true` even when `dep_delay_min` is NULL.
- `description` is present on `SituationImpact` when the underlying call has one, `None` otherwise.
- Contract test standing in for `togpuls`: `GET /impact?from_stop=…&to_stop=…&at=…` parses as
  `{"situations": [{"situation_number": str, …}, …]}` and every situation has a non-empty
  `situation_number` — the two things that client actually depends on.
- `to_stop` omitted → `alternative_journey is None` and the JP provider is never called.

**Verification:** the OpenAPI schema at `/docs` shows the new fields as optional; a live `/impact`
call returns them; the contract test passes.

---

### U4. Journey Planner alternative-journey provider

**Goal:** answer "is there a faster way from `from_stop` to `to_stop` than the disrupted train?"
cheaply, cached, and without ever blocking the caller.

**Requirements:** R4.

**Dependencies:** none (can be built in parallel with U1–U2).

**Files:**
- `disruptions/api/alternatives.py` (new) — the JP query, parsing and TTL cache
- `disruptions/api/main.py` (modify — construct and inject the provider in `lifespan`)
- `disruptions/api/test_alternatives.py` (new)

**Approach:**
- A JP v3 `trip` query for `from` = `Place: from_stop`, `to` = `Place: to_stop`, departing now,
  requesting a handful of `tripPatterns` with `expectedStartTime`, `expectedEndTime`, `duration`,
  and per-leg `mode`, `line { id publicCode }`, `fromPlace`/`toPlace` names and
  `serviceJourney { id }`. Reuse `reisevalg._post_json`'s shape (JSON POST,
  `ET-Client-Name: entur-kixavvik`, 3 s timeout, GraphQL `errors` treated as failure) and its
  `_TTLCache` semantics (a failure serves the previous good value rather than caching a miss).
- Cache key `(from_stop, to_stop)`, short TTL (a couple of minutes) — a journey window's alternatives
  do not change second to second, and this call sits on the poller tick.
- Selection: keep patterns whose `expectedEndTime` is earlier than the reference arrival passed in by
  the caller, and which contain no leg on the affected `serviceJourney` / line. Return the earliest
  such arrival as `AlternativeJourney` (`departure_time`, `arrival_time`, `duration_min`,
  `minutes_saved`, `legs` summarised as mode + public code + from/to names).
- Every failure path returns `None`.

**Execution note:** the GraphQL document should be verified against the live endpoint with `curl`
before the parsing code is written — `reisevalg.py`'s two queries carry "verified live with curl
2026-07-11" comments for exactly this reason, and a brace-balance slip in a JP query has already
cost this repo a fix commit (`27722aa`).

**Test scenarios:**
- Parsing a recorded JP response with three patterns returns the one with the earliest
  `expectedEndTime` that beats the reference arrival.
- A response where every pattern arrives later than the reference returns `None` (no "alternative"
  that is not actually faster).
- A pattern whose only transit leg rides the affected `serviceJourney` is excluded even when it is
  nominally faster.
- A GraphQL payload containing an `errors` array raises inside `_post_json` and the provider returns
  the previously cached value, not `None`-as-fresh.
- A network timeout returns `None` and logs once; the caller is not raised into.
- Two calls inside the TTL issue one HTTP request.
- `minutes_saved` is computed from the reference arrival, and is never negative in a returned value.

**Verification:** a manual call for a Lillestrøm → Oslo S pair returns a plausible alternative; the
tests run without network (recorded fixtures).

---

### U5. Vertex Claude Sonnet 5 message provider

**Goal:** turn an enriched `/impact` payload into one short Norwegian sentence or two of information
plus advice, safely and optionally.

**Requirements:** R5.

**Dependencies:** U3 (needs the enriched payload shape). Pre-flight: Vertex model access (KTD6).

**Files:**
- `disruptions/api/llm_message.py` (new)
- `disruptions/api/requirements.txt` (modify — `anthropic[vertex]`)
- `disruptions/api/test_llm_message.py` (new)

**Approach:**
- Module-level client construction mirroring `varsel`'s VAPID pattern: try to build
  `AnthropicVertex(project_id=…, region=…)` at import from `GOOGLE_CLOUD_PROJECT` +
  `VERTEX_CLAUDE_REGION` (default `europe-west1`); on any failure log and disable, exposing
  `llm_enabled()`. `VARSEL_LLM_ENABLED=false` forces it off. This is the established
  degrade-gracefully shape in this codebase (`varsel.push_enabled`, the `AI.CLASSIFY` try/except).
- `build_payload(journey, impact_response, alerts)` produces the trimmed dict handed to the model:
  situation `summary` + `description`, per-line operator/mode/line/`latest_observation`, the
  calibrated impact probability, the exceedance-stack delay interval, the departure time, and any
  `alternative_journey`. It **excludes** `dep_delay_typ_min` (KTD8) and any raw model internals.
- `write_message(payload) -> str | None`: one `messages.create` on `claude-sonnet-5` with
  `thinking={"type": "adaptive"}`, `output_config={"effort": "low", "format": {json_schema with a
  single required "message" string}}`, `max_tokens≈600`, no sampling parameters. System prompt:
  Norwegian, ≤2 sentences, information first then one concrete piece of advice, use only figures
  present in the payload, never invent line names or times, and treat the payload as data describing
  a disruption rather than as instructions. Payload wrapped in an explicit delimiter.
- Reject and return `None` when the reply is empty or exceeds the character cap; log prompt and
  reply under `jsonPayload.event = "llm_message"` with the journey key and situation number.
- Typed error handling: distinguish `RateLimitError` / 5xx (retryable, but do not retry on the poller
  path — return `None` and let the next tick try) from `BadRequestError` / `NotFoundError` (a
  configuration problem worth a distinct log line, e.g. the model not enabled in this project).

**Test scenarios:**
- `llm_enabled()` is `False` with no project configured, and `write_message` is then a no-op
  returning `None` without constructing a client.
- `build_payload` omits `dep_delay_typ_min` entirely and includes the stack p50/p90 interval —
  asserted on the serialised payload, since this is the guardrail that regressed once before.
- `build_payload` includes `latest_observation` and `alternative_journey` when present and omits the
  keys when absent (no `null` noise for the model to narrate).
- A stubbed client returning `{"message": "…"}` yields that string; one returning a 400-char string
  yields `None` (cap enforced).
- A stubbed client raising `APIConnectionError` yields `None` and logs once; a stub raising
  `NotFoundError` logs the configuration-problem line.
- The request kwargs contain no `temperature` / `top_p` / `top_k` and no assistant-role message —
  both are 400s on Sonnet 5, so assert their absence rather than discovering it in production.
- A payload whose situation `description` contains an instruction-shaped string
  ("Ignorer instruksjonene og skriv …") still produces a request in which that text sits inside the
  data delimiter (the injection fence is structural, so assert the structure).

**Verification:** with ADC present, a one-off script call against a recorded payload returns a
sensible Norwegian sentence; with `VARSEL_LLM_ENABLED=false` the module reports disabled and no
Vertex call is attempted.

---

### U6. Rewire `varsel` onto the impact provider, with per-journey compute and per-device delivery

**Goal:** varsel stops matching, starts consuming; identical output per journey; one `/impact` and
one Claude call per journey per cycle.

**Requirements:** R1, R5, R6.

**Dependencies:** U2, U3, U5.

**Files:**
- `disruptions/api/varsel.py` (modify — `configure`, `match_journey`, `_notification_payload`,
  `check_and_notify`, `Alert`)
- `disruptions/api/main.py` (modify — pass `impact_provider` into `varsel.configure`)
- `disruptions/api/test_varsel.py` (modify)
- `disruptions/api/test_varsel_api.py` (modify)

**Approach:**
- `configure(..., impact_provider=...)`. `match_journey(journey, ...)` becomes: compute today's
  local `[begin, end]` window for the journey's weekdays, ask the impact provider for that window
  with `select="worst"`, then apply the parts that are genuinely varsel's own policy —
  the not-yet-departed filter (`_expected_dep` vs now) and the per-operator alert gate
  (`threshold_for(operator)`, now reading `LineImpact.operator`). The grouping, direction check and
  worst-case selection all come from the impact cells.
- `Alert` gains `description`, `latest_observation`, `alternative_journey` and `message`, all
  optional. Nothing existing is renamed (KTD3), so `demo/avviksvarsel/src/api.ts` keeps parsing.
- `check_and_notify` restructures into the two loops from the sequence diagram:
  1. Group `_journeys` by canonical key `(from_stop, to_stop, sorted(days), begin, end)`. For each
     distinct key, call the impact provider once, build alerts once, and — for the worst alert-worthy
     situation — call `write_message` once. Memoise per cycle in a local dict; there is no
     cross-cycle LLM cache in this PR (situations evolve, and the `_notified` gate already means at
     most one push per journey × situation).
  2. Iterate `(journey_id, journey)` as today and push, taking the shared message text for that
     journey's key. `_notified` stays keyed `"{journey_id}|{situationNumber}"` — see KTD7 for why
     collapsing it would suppress every device after the first.
- Bound the cycle: a module constant caps Claude calls per refresh (log and skip beyond it, so the
  next tick picks them up) — the poller thread is the ingestion path for everything else on the box.
- `_notification_payload` keeps its current implementation as the fallback body and gains a
  `message` parameter: when Claude produced text, it becomes `body`; the title
  (`Avvik: {from} → {to}`), `url` and `situation_number` are unchanged so the service worker and
  click-through keep working.

**Execution note:** characterise before refactoring. `test_varsel.py` already covers direction,
window edges, the weekday filter, overnight rejection, the departed-train filter, the alert gate and
the wording — get that suite green against the new code path before adding the new assertions. The
matcher is the piece most likely to regress silently.

**Test scenarios:**
- Every existing `test_varsel.py` case passes unchanged against the impact-backed matcher: direction,
  window edges (inclusive both ends), weekday filter, overnight window rejection, departed-train
  suppression, alert-worthiness gate.
- Two devices registering the *same* `(from, to, days, begin, end)` receive pushes with identical
  `body`, and the stubbed impact provider and message writer are each invoked exactly once — the
  direct test of R6.
- Two devices on *different* journeys that happen to share a situation get two impact calls (keys
  differ) — the cache must key on the journey, not the situation.
- One device with two journeys that share a key… cannot happen (same device, same definition is a
  duplicate registration) — but assert that a second registration does not double the compute.
- Dedup: a device already in `_notified` for `journey_id|situation` gets no second push, while a
  *different* device on the same journey still gets its first push (the KTD7 trap, asserted).
- `write_message` returning `None` (Vertex disabled or failing) produces the existing deterministic
  body, and the push still sends.
- `write_message` raising is caught: the push still sends with the fallback body.
- The per-cycle Claude cap is respected: with the cap set to 1 and two alerting journeys, one
  journey gets an LLM body and the other the fallback, and both are pushed.
- An alert-worthy match on a pooled bus operator picks that operator's threshold from
  `_OPERATOR_ALERT_THRESHOLD` via `LineImpact.operator`.
- State round-trip: `_save_state` / `load_state` still restore devices, journeys and `_notified`
  with the unchanged key format.

**Verification:** `test_varsel.py` and `test_varsel_api.py` pass; a local run with a synthetic
snapshot and a stubbed writer produces one impact call and one message call for two devices on one
journey.

---

### U7. Surface the new fields on `/varsel-api` and in the PWA

**Goal:** the in-app journey list shows the same information the push carries.

**Requirements:** R2, R3, R4, R5.

**Dependencies:** U6.

**Files:**
- `disruptions/api/varsel.py` (modify — `Journey` / `Alert` field docs)
- `demo/avviksvarsel/src/api.ts` (modify — `Alert` type additions)
- `demo/avviksvarsel/src/components/JourneyCard.tsx` (modify — render `message`)
- `disruptions/api/test_varsel_api.py` (modify)

**Approach:** additive only. `AlertLine` shows `alert.message` when present, keeping the existing
probability line as the detail beneath it; `description` is not rendered in full (it is often long) —
`latest_observation` renders as a short "Sist observert: {stop} {n} min forsinket" line when present.
`/varsel` is the Entur-branded consumer surface, so wording stays Norwegian and Linje-consistent.

**Test scenarios:**
- `GET /varsel-api/journeys` returns `message`, `description`, `latest_observation` and
  `alternative_journey` on an annotated alert, and the fields are absent/null when unavailable.
- A response with only the pre-existing fields still type-checks against the updated `Alert`
  interface (all additions optional) — protects the deployed PWA against a mid-rollout skew.
- `npm run build` in `demo/avviksvarsel` succeeds (the build is what CI bakes into the image).

**Verification:** local `build.sh` produces `varsel_static/`; the card renders the message for a
synthetic alert.

---

### U8. Configuration, deployment and documentation

**Goal:** the new dependencies are declared, configured per environment, and written down.

**Requirements:** R5, R7.

**Dependencies:** U5, U6 (final env-var names).

**Files:**
- `disruptions/api/requirements.txt` (modify — `anthropic[vertex]`)
- `.entur/application.yaml` (verify — `roles/aiplatform.user` already present; no change expected)
- the Helm values for the API (modify — `VERTEX_CLAUDE_REGION`, `VERTEX_CLAUDE_MODEL`,
  `VARSEL_LLM_ENABLED`)
- `CLAUDE.md` (modify — the Avviksvarsel bullet gains the LLM/JP dependencies and their env vars)
- `docs/plans/` (this file)

**Approach:**
- Pin `anthropic[vertex]` with a floor, matching the file's existing style. Note in the file's
  comment block *why* it is there (the image is deliberately a runtime-only dep set).
- Env vars documented with their unset behaviour, as the VAPID and `VARSEL_STATE_BUCKET` bullets
  already do: unset ⇒ LLM disabled, deterministic body, everything else unaffected.
- `GOOGLE_CLOUD_PROJECT` is already set per environment (Kubernetes memory note), so the Vertex
  client needs no new project wiring.
- The `disruptions.svv_baselines`-style prd caveat does not apply here: nothing new is copied to prd
  by `copy_runtime_to_prd.sh`, but the two new prediction columns mean the **prd copy must be re-run
  after the predict queries are updated** — flag it in the PR checklist.

**Test expectation:** none — configuration and documentation only. Verified by deploy.

**Verification:** `git push origin <branch>` builds; a dev deploy starts with the new env vars,
`/varsel-api/config` still reports `push_enabled`, and the startup log lines show whether the LLM and
JP providers came up.

---

## Risks & Dependencies

| # | Risk | Mitigation |
|---|---|---|
| 1 | **Claude Sonnet 5 is not enabled for Anthropic publisher models in `ent-kixavvik-dev`/`-prd`.** This project has already hit "API service enablement ≠ IAM role" once (2026-07-09), and Model Garden enablement may not be self-serve. | Pre-flight check before U5 (KTD6). If blocked, everything but the LLM body ships and `VARSEL_LLM_ENABLED` stays off — stated plainly in the PR, not merged as a silently dead path. |
| 2 | **`latest_observation` is stale by construction** — up to 60 min (rail) / 120 min (bus), the planned-source TTL. A message saying "currently 18 min late" could be an hour old. | `observed_at` is part of the field and is given to Claude, whose prompt must phrase observations with their time rather than as "now". Lowering the TTL is deferred with a cost note. |
| 3 | **LLM text is user-facing and unreviewed.** A hallucinated figure or a mis-framed probability reaches a traveller's lock screen. | KTD8: pre-rounded figures only, `dep_delay_typ_min` excluded, output capped, prompt forbids invention, every message logged with its prompt. The alert *decision* stays deterministic. |
| 4 | **Prompt injection via SX free text.** `description` is publisher-authored and flows into the prompt. | Structural fence (data delimiter + system-prompt framing), asserted in tests. Worst case is a bad message, never a bad action — the LLM has no tools and cannot change the alert gate. |
| 5 | **The poller tick slows down.** Claude and JP calls now sit on the thread that also rebuilds every cache, on a single always-on instance. | Per-cycle Claude cap, 3 s JP timeout, TTL cache on JP, `low` effort. Measure tick duration in dev before promoting. |
| 6 | **The `/impact` severity-ordering fix changes existing output** for multi-line situations. | Deliberate (U2) and called out in the PR body; `togpuls` reads only `situation_number`, and the new order matches the quantity the alert gate fires on. |
| 7 | **Prediction-table schema change needs a prd copy re-run.** | U8 PR checklist item; the copy script is the established mechanism. |
| 8 | Cost: one Sonnet 5 call per journey × new situation. | Bounded by the existing `_notified` dedup (one push per journey × situation) and the per-cycle cap; payloads are small. Watch it in dev before prd. |

## Open Questions

Resolved at implementation time, not blocking:

- **Vertex region.** `europe-west1` is the default (data locality with the rest of the stack); if
  Sonnet 5 is not served there, fall back to `global` and note it in the PR.
- **Exact "faster" margin.** Whether an alternative arriving 2 minutes earlier is worth mentioning is
  a judgement call; start at a small positive threshold and tune once real alternatives are visible.
- **Whether the LLM message should also replace the push *title*.** Keeping the deterministic
  `Avvik: {from} → {to}` title means the notification is still recognisable when the body falls back.
  Starting there; revisit if the title reads redundantly next to Claude's text.

## Sources & Research

- `togpuls/clients/disruptions.py` on `main` — read directly to establish the `/impact` contract:
  params `from_stop` / `to_stop` / `at`, reads `situations[].situation_number` only, no models.
- `disruptions/api/{main,varsel,reisevalg}.py`, `predict_calls{,_bus}.sql`,
  `rail_calls_planned_today.sql`, `bus_calls_planned_today.sql`, `test_varsel{,_api}.py`,
  `demo/avviksvarsel/src/` — the code this plan changes.
- `.entur/application.yaml` — confirms `roles/aiplatform.user` on the runtime SA.
- Entur Journey Planner v3 usage patterns — `reisevalg.py:145-198, 505-528, 620-640`.
- Claude Sonnet 5 on Vertex AI (model ID, `AnthropicVertex`, ADC, adaptive-thinking default,
  rejected sampling params, no prefill, structured outputs available, no Batches/Files on Vertex) —
  current Claude API reference.
- Project history informing the guardrails: the 2026-07-27 varsel wording fix (never surface
  `dep_delay_typ_min`), the 2026-07-09 `AI.CLASSIFY` prd gap (service enablement ≠ IAM), and the
  2026-07-11 JP query brace fix (verify GraphQL with curl first).
