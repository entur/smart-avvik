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
node stage builds the `/demo` and `/varsel` front-ends (from `demo/reisedemo` +
`demo/avviksvarsel`, which is why the context is the repo root, not `disruptions/api`), and its
python stage assembles the API. CI builds and deploys it via `.github/workflows/{ci,cd}.yml`:

```sh
git push origin main                         # builds the image (ci.yml) and deploys to dev (cd.yml)
gh workflow run cd.yml -f environment=prd    # promote to prd (tst/prd are dispatch-only)
```

- **Single always-on instance** (`deployment.forceReplicas: 1` in the Helm chart): the app runs
  one background poller thread and an in-process cache, so more replicas mean duplicate pollers
  and unshared caches.
- Public, read-only service; the SA needs SIRI read access (granted via the team group).
- **Front-ends are built into the image**, not committed: the Dockerfile's node stage runs each
  app's `npm ci && npm run build`, and Vite writes into `disruptions/api/{demo,varsel}_static/`
  (both gitignored). `demo/*/build.sh` do the same locally for dev. **`/eval` is the exception**:
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
  API is `/varsel-api` (see `disruptions/api/varsel.py`); the matcher runs in the poller's refresh
  cycle. Install steps for phones are in `demo/avviksvarsel/README.md`. Two extra env vars beyond
  the base deploy:
  - `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` (raw base64url) + `VAPID_SUBJECT` (`mailto:`) — the
    Web Push keypair. When unset, push is disabled gracefully (`/varsel-api/config` reports
    `push_enabled=false`); the journey list still works. Regenerate with
    `uv run --with pywebpush python disruptions/api/gen_vapid.py`.
  - `VARSEL_STATE_BUCKET` — a GCS bucket holding `varsel_state.json` (devices/journeys/notified)
    so registrations survive redeploys (the container fs is ephemeral). The runtime SA needs
    `roles/storage.objectAdmin` on it. Without the bucket, state is local-only (lost on redeploy).

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