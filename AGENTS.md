# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project

`kix-avvik` is an experiment from Entur's Team KI-eksperimentering aimed at proactive disruption handling (Norwegian: "proaktiv avvikshåndtering"). It predicts the duration of public-transport disruptions from SIRI-SX situation messages using BigQuery ML.

## Tooling

- Use uv for Python operations
- Use bq for bigquery operations. For instance, use bq show --schema --format=prettyjson ent-anshar-prd:realtime.siri_realtime_et_production

## Important BigQuery tables

- ent-anshar-prd:realtime.siri_realtime_sx_production: SIRI-SX (Situation Exchange), textual incident and disruption messages
- ent-anshar-prd:realtime.siri_realtime_et_production: SIRI-ET (Estimated Timetable), real-time updates to the planned timetable

## Deployment

The `/disruptions` API (`disruptions/api/main.py`) runs on **Kubernetes** (Entur GKE golden
path; hosts `kix-avvik.dev.entur.io` / `.staging.entur.io` (tst) / `.entur.io` (prd); see the
Kubernetes memory note). It is built from the multi-stage `Dockerfile` at the **repo root** — its
node stage builds the `/demo`, `/varsel`, `/historikk` and `/meldinger` front-ends (from
`demo/reisedemo` + `demo/avviksvarsel` + `demo/varselhistorikk` + `demo/meldinger`, which is
why the context is the
repo root, not `disruptions/api`), and its python stage assembles the API. CI builds and deploys it via `.github/workflows/{ci,cd}.yml`:

```sh
git push origin main                         # builds the image (ci.yml) and deploys to dev (cd.yml)
gh workflow run cd.yml -f environment=prd    # promote to prd (tst/prd are dispatch-only)
```

- **Single always-on instance** (`deployment.forceReplicas: 1` in the Helm chart): the app runs
  one background poller thread and an in-process cache, so more replicas mean duplicate pollers
  and unshared caches.
- Public, read-only service; the SA needs SIRI read access (granted via the team group).
- **Front-ends are built into the image**, not committed: the Dockerfile's node stage runs each
  app's `npm ci && npm run build`, and Vite writes into
  `disruptions/api/{demo,varsel,historikk,meldinger}_static/` (all gitignored). `demo/*/build.sh` do the same locally for dev. **`/eval` is the exception**:
  its marimo snapshots run BigQuery under a developer's ADC, so they can't be built in the
  image/CI — they are rendered offline by `disruptions/api/export_eval.sh` and **committed** under
  `disruptions/api/eval_static/`, shipped as-is. Refresh them by running that script, then commit
  and push.
- The partner demo UI at `/demo` (predictions vs realized outcomes for a chosen trip) lives in
  `demo/reisedemo/` (React + Vite). Its data is served by the `/demo-api` endpoints
  from static tables built by `data/queries/create_demo_predictions.sql` — re-run that query
  after model/calibration retrains. `data/queries/refresh_demo_recent.sh` extends the tables
  with the last ~25 days straight from SIRI-ET/SX (run before a partner demo; ~$2).
- The **Avviksvarsel** PWA at `/varsel` (register a journey → Web Push when a live disruption is
  predicted to hit it; rail + the trained bus operators, per-operator alert thresholds) lives in
  `demo/avviksvarsel/` (React + Vite + Entur Linje). Its
  API is `/varsel-api` (see `disruptions/api/varsel.py`), which runs in the poller's refresh cycle.
  **It does not match on its own — it calls `/impact`** through an injected in-process provider
  (`main._varsel_impact`, structurally typed as `varsel.ImpactProvider`), so there is one matcher and
  one severity order behind both surfaces; varsel keeps only its own policy on top (drop trains that
  can no longer be caught, plus the per-operator alert gate). The matcher itself lives in
  `disruptions/api/impact.py` — extracted from `main.py` so `varsel` can *type* that contract rather
  than hold it as `Callable[..., Any]`; it broke silently once and took all push alerting with it
  while the test suite stayed green. `main` owns the prediction cache and injects a reader via
  `impact.configure()`. Note `main.py` imports it as `impact_engine`: the `/impact` route function is
  itself named `impact`, and a bare `impact.` inside that function would resolve to the route.
  Work is shared per journey *definition* (`varsel.journey_key`): two
  devices watching the same trip drive one `/impact` call and one written message and see identical
  text, while push dedup stays keyed on `journey_id` so each device is still notified once.
  **`to_stop` narrows `/impact`**: only trains that actually reach it are returned, so a caller
  passing it sees fewer situations than one that does not.
  Install steps for phones are in `demo/avviksvarsel/README.md`. Env vars beyond the base deploy:
  - `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` (raw base64url) + `VAPID_SUBJECT` (`mailto:`) — the
    Web Push keypair. When unset, push is disabled gracefully (`/varsel-api/config` reports
    `push_enabled=false`); the journey list still works. Regenerate with
    `uv run --with pywebpush python disruptions/api/gen_vapid.py`.
  - `VARSEL_STATE_BUCKET` — a GCS bucket holding `varsel_state.json` (devices/journeys/notified)
    so registrations survive redeploys (the container fs is ephemeral). The runtime SA needs
    `roles/storage.objectAdmin` on it. Without the bucket, state is local-only (lost on redeploy).
  - `VARSEL_LLM_ENABLED` / `VARSEL_LLM_PROVIDER` / `VERTEX_LOCATION` /
    `VERTEX_CLAUDE_MODEL` / `VERTEX_GEMINI_MODEL` — the push text is written by an LLM on Vertex AI
    (`disruptions/api/llm_message.py`) from the enriched `/impact` payload. No API keys: both
    backends use the runtime SA's ADC (`roles/aiplatform.user`, already granted for `AI.CLASSIFY`).
    Every Helm environment pins `VARSEL_LLM_PROVIDER=gemini`; `auto` remains a code capability but
    is not deployed. Check `jsonPayload.provider` on `event="llm_message"` when diagnosing calls.
    Every failure path (unset,
    unavailable model, timeout, empty or over-long reply) falls back to the previous deterministic
    wording, so a push is never lost to an LLM problem.
    **Writing a wording and sending it are two switches** (2026-08-07): `VARSEL_LLM_PROVIDER=off`
    stops a model being *asked*; `VARSEL_LLM_ENABLED=false` still asks and records the answer but
    *sends* the deterministic wording. prd ran them apart — `gemini` with `ENABLED=false` — from
    2026-08-07 to **2026-08-23**, writing a `message_llm` on every alert-worthy decision while
    delivering the template; that period produced the "written but withheld" pairs the rollout was
    judged on (590 same-input pairs over the trailing 30 days, measured 2026-08-23) and prd now runs
    `ENABLED=true`, so a real traveller's push carries the model's prose. Reverting is one value and
    which one depends on the fault: bad **text** → `VARSEL_LLM_ENABLED=false` (travellers get the
    template, the model keeps writing and recording); **egress** must stop →
    `VARSEL_LLM_PROVIDER=off`. Delivery never changed what Vertex receives — generation did, on
    2026-08-07. Know what that sends: the traveller's origin, destination and departure time, which
    is not what the `AI.CLASSIFY` IAM precedent covers. Prompts and replies are logged under `jsonPayload.event="llm_message"`.
    **There are three location shapes, not two** (2026-08-07): `global`, a single region, and the
    **EU multi-region** endpoint (`aiplatform.eu.rep.googleapis.com`, `location="eu"`) — and only the
    third both serves the model and confines processing, which is why `VERTEX_GEMINI_LOCATION` is
    **`eu` in every environment and in the code default**. Vertex documents multi-region endpoints as
    keeping ML processing "within a specific jurisdictional boundary" and `global` as the opposite:
    "you can't control or know which region your ML processing requests are sent to" — which also
    leaves Google's abuse-monitoring retention ("the same region or multi-region selected by the
    customer") bound to nothing. A single region is **not** the fallback it looks like: measured
    2026-08-07 in both projects, `gemini-3.5-flash` 404s at `europe-west1`, `europe-north1` and
    `europe-west4` alike, so pinning a region costs a model generation while `eu` costs nothing
    (median 1.51 s vs 1.38 s, from inside the dev cluster; Private Google Access is not in that path,
    which the Vertex docs warn would break multi-region endpoints). `VERTEX_LOCATION` still defaults
    to `global` and governs Claude, unavailable in these projects; an explicit `VERTEX_LOCATION`
    moves both. Not settled by this: whether a commute pattern is "Fortrolig" under Entur's
    classification — a DPIA question, with `VARSEL_LLM_PROVIDER=off` as the kill switch.
    The backend latch expires
    after 30 min (`llm_message._LATCH_TTL`), so enabling Model Garden takes effect without a
    redeploy. The prompt is **not** logged by default (it carries the traveller's origin,
    destination and departure time); records carry a hashed `journey_digest`, and
    `VARSEL_LLM_LOG_PROMPTS=true` puts the full prompt/reply at DEBUG.
  - `/impact` also returns `alternative_journey` when `to_stop` is given — the **best remaining way
    there**, not a faster one (`disruptions/api/alternatives.py`, TTL-cached, fail-open, no
    credentials). Prefer the latest departure still arriving before the disrupted train's aimed
    arrival; failing that, the earliest arrival among trains not yet departed — which for a
    cancelled train legitimately lands later than the timetable promised, so `minutes_saved` is
    signed and `arrives_before_original` says whether the traveller still makes it. The traveller's
    own train competes: when it wins, the field is null with `original_is_best` true.

- The **Varselhistorikk** page at `/historikk` (`demo/varselhistorikk/`, React + Vite + Entur
  Linje) is the feedback loop for alerting: every decision Avviksvarsel took about a registered
  journey, the message that was sent, and — once the journey has run — a hit / false alarm / miss /
  correct-reject verdict. Its API is `/historikk-api` (`disruptions/api/historikk.py`).
  - **Every decision is logged, not every push.** `varsel.check_and_notify` appends one row per
    (journey definition x situation x departure) per refresh cycle to `disruptions.varsel_decisions`
    via `disruptions/api/decisions.py`, *including* matches that stayed below the alert threshold.
    Without those, a `miss` leaves no trace and only precision is measurable. Repeats across cycles
    are intentional — they are the lead-time series. Writing fails open: a BigQuery problem never
    blocks a push.
  - Both tables and the `varsel_decision_groups` view are created by the app at startup
    (`create_varsel_decisions.sql`, `CREATE TABLE IF NOT EXISTS` — **never** `CREATE OR REPLACE`;
    their contents cannot be rebuilt). The outcomes table is created there too, not by the nightly
    job: `/historikk` queries it on every request and would 503 until the first nightly run
    otherwise. The view holds the grouping shared by the nightly job and the API, so the two cannot
    drift apart.
  - Outcomes come from `data/queries/label_varsel_decisions.sql` (scheduled query
    `varsel-label-daily`, 04:00 UTC, ~$0.01/run measured; **registered in dev, not prd** —
    register it per environment with the `bq mk --transfer_config` recipe in
    `label_varsel_decisions.sh`, which needs `--service_account_name` or it blocks on an
    interactive OAuth prompt), joining `last_recorded` on
    `(operator, jkey, call_date, stop_order)`. It must **not** remap `dataSource='VYG'` to `'NSB'`
    the way `extend_demo_recent_daily.sql` does — varsel records the codespace from the line ref
    (`VYG`), so remapping would silently join zero rail rows. Backfill by hand with
    `data/queries/label_varsel_decisions.sh [project]`.
  - **An unmatched join is `NULL`, never "no trouble".** `last_recorded`'s rail slice runs 10-17 %
    thin, so some calls never resolve; counting them as correct rejections would manufacture
    precision. The API keeps `venter` (too recent to label) distinct from `ukjent` (labelled, no
    call found) for the same reason.
  - Recall on this page is recall **among SX-flagged calls on registered journeys** — the models
    never score a call no SX message flagged — and the UI says so next to the number.
  - Privacy, in two layers. The *table* is keyed on the journey **definition**
    (`journey_digest`, the same `llm_message.key_digest` used in the LLM audit log, so the two
    join) plus a device count — no `device_id`, no `journey_id`, no time window or weekday pattern.
    The *API* redacts further: `journey_digest`, `from_stop` and `to_stop` are **not** served,
    because `/historikk-api` is unauthenticated on a public host and those three next to
    `departure_time` and `n_devices` would publish "somebody commutes A→B at 07:15". `Decision` is
    `extra="forbid"` so a future SELECT that re-adds a redacted column fails loudly instead of
    quietly publishing it. The page identifies a decision by its **train**, not by whose trip it was.
  - `venter` vs `ukjent` tracks the labelling job's 10-day retry window, on both surfaces: an
    unresolved row inside the window is still being retried (`venter`); past it, the attempts have
    given up (`ukjent`). Keep `historikk_decisions.sql` and `historikk_summary.sql` in step — the
    page shows the per-row state next to the count.
  - Neither table is in `copy_runtime_to_prd.sh`: each environment records its own decisions and
    `bq cp -f` overwrites. Both environments record BOTH wordings and differ in what was sent:
    dev delivers the model's (`message_source='llm'`), prd withholds it
    (`message_source='deterministic'` beside a non-null `message_llm`) — the population prd's
    rollout decision is judged on.

- The **Meldinger** page at `/meldinger` (`demo/meldinger/`, React + Vite + Entur Linje)
  replays a window of past alert decisions through **today's** prompt, from the `message_input`
  already stored on every decision row. `/historikk` answers "what did we send"; this answers "what
  would today's prompt say", and nothing is sent to anybody. API `/meldinger-api`
  (`disruptions/api/meldinger.py`) over `meldinger_{decisions,summary,filters,messages}.sql`;
  `messages` is the only route that calls Vertex, kept separate so a Vertex outage empties the
  wordings and leaves every card and statistic intact. Its two lists align **by position**, which is
  why the messages query repeats the decisions query's WHERE and ORDER BY verbatim.
  - **It was `/tilbakeblikk` until 2026-08-23.** The rename covers route, API prefix, module and
    SQL filenames, the static dir and the Vite env prefix. What does not travel with it is **Cloud
    Logging**: the events are now `meldinger_replay`, `meldinger_replay_retry` and
    `meldinger_bounded`, so an old saved query returns nothing rather than erroring. The `.tb-` CSS
    prefix and the dated plan document keep their old names on purpose.
  - **The window is `decided_at`, not `call_date`** — one Oslo day plus a from/to time of day,
    quantized to 15-minute half-open steps (unbounded times would mint one cache entry and one query
    apiece on a public endpoint). It is also the partition key of `varsel_decisions`, so these prune
    where /historikk's cannot: measured 2026-08-21, the filters query dry-runs at **0 bytes** against
    `historikk_filters.sql`'s **21 MB**. The outcomes join is bounded **separately** on `call_date`
    (that table's partition key) at **±1 day, measured** — max 1, p99.9 = 1 over 59 694 dev rows —
    because a too-narrow bound drops outcome rows *silently* and the byte counts would report the
    loss as a cheaper query. Cost is not a correctness check.
  - **Generation is the gate, delivery is not.** It asks `llm_message.generation_enabled()` and never
    `delivery_enabled()` or `varsel._deliverable_message`. prd generates *and*, since 2026-08-23,
    delivers — but `VARSEL_LLM_ENABLED=false` is the documented revert for bad text, so the
    generate-without-deliver state is one deploy away at any time and a page that consulted the
    delivery gate would go blank for a reason having nothing to do with the feature, precisely while
    a prompt is being fixed.
  - **Four bounds on regeneration**: 1 000 backend CALLS per request and trailing hour, 5 000 per
    trailing day, a configurable request deadline that reserves the 8 s single-provider worst case inside each worker, and a cache
    keyed on subject + replayed cycle + `PROMPT_VERSION`. Replay has its own hourly/daily ledger;
    all environments pin Gemini, so one subject reserves one call. Cached hits spend no allowance.
    The code default remains 30 s, while every Helm environment sets
    `MELDINGER_REQUEST_DEADLINE_S=60` so a cold-cache page can fill more rows per request.
    Successful, `rejected`, and `ungrounded` results remain in memory
    until eviction (10 000 entries) or process restart, while transient failures expire after 60 s.
    The route stays `async def` and admits only two concurrent replay requests, independently of the
    larger spend allowance, so it cannot create unbounded pools beside `/impact` and `/varsel-api`.
  - **The latch is isolated.** `llm_message`'s `_active`/`_active_at`/`_latched_off` and their lock
    now live in a `_LatchState` holder; the module keeps one global instance (every existing caller
    unchanged) and replay takes its own via `new_replay_state()`, seeded one-way. Without it a replay
    burst that drove both backends into a configuration error would strip model wordings from real
    traveller pushes for the whole 30 min TTL — silently, on the side nobody watches.
  - **The privacy split is structural.** `meldinger_decisions.sql` carries `message_input` only as
    a null test — no CTE in it holds the text — while `meldinger_messages.sql` holds it and only
    the server reads it. Decision rows project `from_stop`/`to_stop` only for server-side NSR name
    resolution; the public model returns `from_name`/`to_name` and still forbids the raw refs,
    `journey_digest`, and `message_input` with `extra="forbid"`.
  - **The surface is served unconditionally, in every environment.** A `TILBAKEBLIKK_ENABLED`
    env var gated it (router and static mount together) until the **2026-08-23** rename to
    `/meldinger` removed the flag; by then all three environments set it `"true"`, so it was a chart
    value nobody varied. **Q1 is answered, and on one fact rather than on the measurement**: the
    prose names both ends of a trip, each card headline names them too, and `trafficType: public` is
    inherited by every environment — measured 2026-08-22 over 14 days of prd, 21 of 31 from/to pairs
    (68 %) isolate exactly one journey definition. Accepted because prd's registrations are **test
    users, not members of the public** (team decision, 2026-08-22). Re-run that measurement if prd
    ever takes real registrations; the fix is then access control in front of `/meldinger`, not a
    shorter SELECT list — the exposure is in the generated sentence, not in a column.
  - Two card constraints: `first_in_window` says whether the replayed cycle is the subject's first
    alerting opportunity or merely the earliest one this window contains (narrowing the picker moves
    it), and the per-cycle aggregates are window-scoped while the verdict comes whole from the
    nightly job — so `ever_alerted: false` beside `verdict: 'hit'` is correct, not a bug.
  - A replay measures a **prompt** change, not a `build_message_input` change (the stored input was
    built by that day's builder), and each wording is a single draw from a non-deterministic decoder
    cached per row and prompt version — so a difference between two rows or two prompt versions is
    confounded with sampling variance. The page says so.
  - `data/queries/validate_varsel_sql.sh` covers the four new files. A same-day window is mostly
    `venter` (verdicts arrive from the 04:00 UTC job the following night), so precision/recall/lead
    on the default view describe a small labelled subset — `/summary` states that as counts.

- The **Reisevalg** endpoint `GET /reisevalg-api/recommendation` (car vs train for the
  Lier/Drammen → Oslo S corridor; feasibility prototype — see `docs/bil-avvik/LEARNINGS.md`)
  lives in `disruptions/api/reisevalg.py`. Road signal: SVV Trafikkdata volumes gated to
  UNKNOWN (measured precision 0.60 < 0.8 bar) + a DATEX II stub behind optional
  `SVV_DATEX_USER`/`SVV_DATEX_PASSWORD` (unset = provider unavailable; when provisioning,
  use Secret Manager like the VAPID keys). Its `disruptions.svv_baselines` lookup loads in
  the poller/lifespan and is deliberately NOT in `copy_runtime_to_prd.sh` — prd serves the
  road side as UNKNOWN until a road provider is validated. Rebuild baselines/backtest with
  `data/queries/reisevalg_*.sql`; raw volume backfill via `data/svv/download_volumes.py`.

- **Rushvarsel** at `/togmelding` (`/togmelding-api/today`, `disruptions/api/togvarsel.py`) is
  the rush-hour forecast from `docs/togvarsel/` (§8 of the findings as revised by §14k; the
  implementation is `docs/togvarsel/03-implementasjon.md`). Four weekday issues (06:30, 07:00 for
  the AM rush; 12:00, 14:00 for the PM rush), each a tuned `LOGISTIC_REG` probability on the
  product bar (cancelled OR ≥ 10 min) mapped to green/yellow/red at fixed cuts (0.10/0.30), with
  rule-based reasons and a deterministic "kjente avvik" list. `togvarsel.refresh()` runs on
  every poll tick (wall-clock — an issue is due at its cutoff whether or not SX moved) and
  computes due issues (one rebuild of `togvarsel_features_today` + one `ML.PREDICT` per issue —
  a handful of BQ jobs per weekday; failures back off per issue and give up after 8 tries/day);
  weekends/holidays get no forecast, and issued forecasts land in `disruptions.togvarsel_issued`
  (MERGE, first write per (d, issue) wins; `late` marks back-fills — per-environment, not copied
  to prd). Retrain monthly with `data/queries/togvarsel/retrain.sh` (≈ $1.5; trains candidates,
  promotes all four `togvarsel_m_*` only on full success), then `copy_runtime_to_prd.sh` copies
  models + tables to prd. Holidays/low season live in ONE place: `disruptions.togvarsel_calendar`
  (`data/queries/togvarsel/00_calendar.sql`) — extend yearly; serving ERRORs loudly past its
  horizon and retrain.sh refuses under 60 days of headroom. The page is a React + Vite + Entur
  Linje bundle (`demo/togmelding/`, built into the image like the other four front-ends;
  `disruptions/api/togmelding_static/` is generated and gitignored since 2026-09-01, when the page
  moved off one committed HTML file to match `/varsel`). Level colours are Linje validation
  tokens, never hex; `GET /togmelding-api/config` labels non-prod in the heading and title.
  The product was renamed from «Togmeldingen» to «Rushvarsel» on 2026-09-01, DISPLAY-ONLY: the
  route, `/togmelding-api`, the modules, `demo/togmelding/`, the `disruptions.togvarsel_*` tables,
  `TOGVARSEL_NOTIFY_ENABLED`, the `togvarsel_*` log events and the two data discriminators
  (`display_source='togvarsel'`, `KIX:Togvarsel:<date>:<issue>`) all keep their names — the route
  because it is already in pushes on lock screens, the discriminators because a new name drops the
  rows out of every existing export query. The
  published privacy texts stay duplicated in `demo/togmelding/src/personvern.ts` (the app cannot
  import /varsel's module: its Docker stage copies only its own directory), guarded by
  `disruptions/api/test_personvern_texts.py`, which compares both modules' resolved constants.
  `GET /togmelding-api/outlook?device_id=…` (`togvarsel_notify.rush_outlook`) is the same forecast
  narrowed to a device, for the status panel at the top of `/varsel` — the push policy read-only,
  bounded by the rush's end rather than the registration's, and reading the poller's station cache
  without ever querying.

## Conventions

- All SQL targets BigQuery Standard SQL; legacy SQL is not used.
- Text fields are filtered to Norwegian (`language IS NULL OR UPPER(language) = 'NO'`) — preserve this when adding queries against SX data.
- The two-year partition window (`_PARTITIONTIME >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR)`) is the standard lookback for training queries.
- Consider cost before running BigQuery operations. Ask for permission before running anything more expensive than $10.
- In docs/kix-docs, references to R1, R2 etc refers to the documents docs/kix-docs/docs/Context/r1-entur-rolle-og-mandat.md, docs/kix-docs/docs/Context/r2-entur-teknisk-deep-dive.md etc.
