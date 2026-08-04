# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
node stage builds the `/demo`, `/varsel` and `/historikk` front-ends (from
`demo/reisedemo` + `demo/avviksvarsel` + `demo/varselhistorikk`, which is why the context is the
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
  `disruptions/api/{demo,varsel,historikk}_static/` (all gitignored). `demo/*/build.sh` do the same locally for dev. **`/eval` is the exception**:
  its marimo snapshots run BigQuery under a developer's ADC, so they can't be built in the
  image/CI — they are rendered offline by `disruptions/api/export_eval.sh` and **committed** under
  `disruptions/api/eval_static/`, shipped as-is. Refresh them by running that script, then commit
  and push.
- The partner demo UI at `/demo` (predictions vs realized outcomes for a chosen trip) lives in
  `demo/reisedemo/` (React + Vite). Its data is served by the `/demo-api` endpoints
  from static tables built by `data/queries/create_demo_predictions.sql` — re-run that query
  after model/calibration retrains. `data/queries/refresh_demo_recent.sh` extends the tables
  with the last ~25 days straight from SIRI-ET/SX (run before a partner demo; ~$2).
  Non-prod deployments label the masthead and tab title (`Reiseprediksjon under avvik (dev)`) from
  `GET /demo-api/config` — a partner shown dev numbers as if they were prd is the failure it
  prevents. All three front-ends do this from the same `deployment.environment_label()`, each
  through its own `*-api/config` endpoint, kept off the data endpoints because those can fail while
  the page shell still renders. `disruptions/api/test_demo_api.py` covers the `/demo` one.
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
  **One picker row per physical place** (2026-08-03): `/varsel-api/stops` collapses stop refs that
  the National Stop Register groups under the same `parentRef` *and* that share a name — a station's
  rail and bus sides, a ferry berth registered twice. Before that the picker offered two identical
  rows ("Nyvoll ferjekai" 57120 / 57070) and picking the wrong one made `to_stop` narrowing drop
  every situation, so the journey card said "ingen aktive avvik akkurat nå" forever. Three parts,
  and all three are needed: the row keeps the busiest ref, `varsel.create_journey` **canonicalizes**
  submitted refs (a stale client can still name a collapsed one), and `match_journey` asks `/impact`
  about **every** ref of the place (`varsel.place_refs`) because each feed records its calls on its
  own ref — matching only the canonical one would just move the silent miss down a layer. Names that
  belong to several *different* places (~1 300 of them: "Sentrum", "Kirken") get a county suffix from
  `topographicPlaceRef`, `KVE:` codespace only. The registry read fails open: no registry means no
  grouping and no counties, exactly as before it existed. It is readable from both environments (the
  dataset grants `allAuthenticatedUsers` READER) and costs ~3 MB per refresh.
  Install steps for phones are in `demo/avviksvarsel/README.md`. Env vars beyond the base deploy:
  - `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` (raw base64url) + `VAPID_SUBJECT` (`mailto:`) — the
    Web Push keypair. When unset, push is disabled gracefully (`/varsel-api/config` reports
    `push_enabled=false`); the journey list still works. Regenerate with
    `uv run --with pywebpush python disruptions/api/gen_vapid.py`.
  - `VARSEL_STATE_BUCKET` — a GCS bucket holding `varsel_state.json` (devices/journeys/notified)
    so registrations survive redeploys (the container fs is ephemeral). The runtime SA needs
    `roles/storage.objectAdmin` on it. Without the bucket, state is local-only (lost on redeploy).
  - Non-prod deployments label themselves in the PWA header and tab title —
    `Avviksvarsel (dev)` — and in the **push title** (`[dev] Avvik: …`, prefixed because phones
    truncate on the right), because dev and prd installs are otherwise identical on a phone and in
    the notification tray (the manifest name and icons are baked into the one image every
    environment shares). The tag goes in the title only, never the body, so the wording
    `/historikk` records is the same text in every environment.
    `/varsel-api/config` serves the name. The one derivation lives in
    `disruptions/api/deployment.py` (`environment_label()`), shared with `/historikk`: it reads the
    `GOOGLE_CLOUD_PROJECT` suffix, so no cosmetic per-environment variable has to be remembered —
    that one is already mandatory everywhere, since the unqualified `disruptions.*` refs resolve
    through it. `KIX_ENVIRONMENT` overrides it locally. `prd` — and an unreadable project — stay
    unlabelled: a stray `(dev)` on the traveller-facing host is worse than none on a deployment we
    control. Note the `/varsel-api` tests pin `KIX_ENVIRONMENT=prd` at import, or a developer with
    `GOOGLE_CLOUD_PROJECT=ent-kixavvik-dev` exported fails the push-title assertions.
  - `VARSEL_LLM_ENABLED` / `VARSEL_LLM_PROVIDER` / `VERTEX_LOCATION` /
    `VERTEX_CLAUDE_MODEL` / `VERTEX_GEMINI_MODEL` — the push text is written by an LLM on Vertex AI
    (`disruptions/api/llm_message.py`) from the enriched `/impact` payload. No API keys: both
    backends use the runtime SA's ADC (`roles/aiplatform.user`, already granted for `AI.CLASSIFY`).
    `VARSEL_LLM_PROVIDER=auto` tries **Claude Sonnet 5** and latches to **Gemini** for the process
    lifetime if the project cannot serve Anthropic publisher models — those need Vertex **Model
    Garden enablement on top of the IAM role**, so check `jsonPayload.provider` on
    `event="llm_message"` rather than assuming which one answered. Every failure path (unset,
    unavailable model, timeout, empty or over-long reply) falls back to the previous deterministic
    wording, so a push is never lost to an LLM problem. **Off in prd** (`VARSEL_LLM_ENABLED=false`)
    until validated in dev. Prompts and replies are logged under `jsonPayload.event="llm_message"`.
    `VERTEX_LOCATION` defaults to **`global`**, not to the stack's `europe-west1`: measured
    2026-07-29, `gemini-3.5-flash` serves at `global` and 404s regionally, and Sonnet 5 is not
    available in `ent-kixavvik-dev` at all. Override per model with `VERTEX_CLAUDE_LOCATION` /
    `VERTEX_GEMINI_LOCATION` when their regional availability diverges. The backend latch expires
    after 30 min (`llm_message._LATCH_TTL`), so enabling Model Garden takes effect without a
    redeploy. The prompt is **not** logged by default (it carries the traveller's origin,
    destination and departure time); records carry a hashed `journey_digest`, and
    `VARSEL_LLM_LOG_PROMPTS=true` puts the full prompt/reply at DEBUG.
  - `/impact` also returns `alternative_journey` when `to_stop` is given — a faster way there from
    the Journey Planner (`disruptions/api/alternatives.py`, TTL-cached, fail-open, no credentials).

- The **Varselhistorikk** page at `/historikk` (`demo/varselhistorikk/`, React + Vite + Entur
  Linje) is the feedback loop for alerting: every decision Avviksvarsel took about a registered
  journey, the message that was sent, and — once the journey has run — a hit / false alarm / miss /
  correct-reject verdict. Its API is `/historikk-api` (`disruptions/api/historikk.py`).
  - Non-prod deployments label the heading and tab title (`Varselhistorikk (dev)`) from
    `GET /historikk-api/config`, sharing `deployment.environment_label()` with `/varsel`. It is its
    own endpoint, not a field on `/summary`, because both data endpoints 503 when the decision log
    cannot be read — precisely when "is this dev or prd?" matters — and it touches no BigQuery.
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
    `bq cp -f` overwrites. Note `VARSEL_LLM_ENABLED=false` in prd, so prd rows carry the
    deterministic wording; dev is where the LLM-written messages are.

- The **Reisevalg** endpoint `GET /reisevalg-api/recommendation` (car vs train for the
  Lier/Drammen → Oslo S corridor; feasibility prototype — see `docs/bil-avvik/LEARNINGS.md`)
  lives in `disruptions/api/reisevalg.py`. Road signal: SVV Trafikkdata volumes gated to
  UNKNOWN (measured precision 0.60 < 0.8 bar) + a DATEX II stub behind optional
  `SVV_DATEX_USER`/`SVV_DATEX_PASSWORD` (unset = provider unavailable; when provisioning,
  use Secret Manager like the VAPID keys). Its `disruptions.svv_baselines` lookup loads in
  the poller/lifespan and is deliberately NOT in `copy_runtime_to_prd.sh` — prd serves the
  road side as UNKNOWN until a road provider is validated. Rebuild baselines/backtest with
  `data/queries/reisevalg_*.sql`; raw volume backfill via `data/svv/download_volumes.py`.

## Conventions

- All SQL targets BigQuery Standard SQL; legacy SQL is not used.
- Text fields are filtered to Norwegian (`language IS NULL OR UPPER(language) = 'NO'`) — preserve this when adding queries against SX data.
- The two-year partition window (`_PARTITIONTIME >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR)`) is the standard lookback for training queries.
- Consider cost before running BigQuery operations. Ask for permission before running anything more expensive than $10.
- In docs/kix-docs, references to R1, R2 etc refers to the documents docs/kix-docs/docs/Context/r1-entur-rolle-og-mandat.md, docs/kix-docs/docs/Context/r2-entur-teknisk-deep-dive.md etc.