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

The `/disruptions` API (`disruptions/api/main.py`) runs on Cloud Run, deployed from source
(Cloud Build builds the Dockerfile in that directory). Redeploy from `disruptions/api/`:

```sh
gcloud run deploy kix-avvik-disruptions \
  --source . \
  --project ent-kixavvik-dev \
  --region europe-west1 \
  --service-account application@ent-kixavvik-dev.iam.gserviceaccount.com \
  --min-instances 1 \
  --max-instances 1 \
  --no-cpu-throttling
```

- `--min/--max-instances 1`: the app runs one background poller thread and an in-process
  cache, so it must be a single always-on instance — more instances mean duplicate pollers
  and unshared caches.
- `--no-cpu-throttling`: keeps CPU allocated between requests so the poller keeps running.
- Public, read-only service; the SA needs SIRI read access (granted via the team group).
- Service URL: https://kix-avvik-disruptions-305411154596.europe-west1.run.app
- The partner demo UI at `/demo` (predictions vs realized outcomes for a chosen trip) lives in
  `demo/reisedemo/` (React + Vite) and is built into `disruptions/api/demo_static/` by
  `deploy.sh` (`SKIP_DEMO_BUILD=1` to skip). Its data is served by the `/demo-api` endpoints
  from static tables built by `data/queries/create_demo_predictions.sql` — re-run that query
  after model/calibration retrains.

## Conventions

- All SQL targets BigQuery Standard SQL; legacy SQL is not used.
- Text fields are filtered to Norwegian (`language IS NULL OR UPPER(language) = 'NO'`) — preserve this when adding queries against SX data.
- The two-year partition window (`_PARTITIONTIME >= DATE_SUB(CURRENT_DATE(), INTERVAL 2 YEAR)`) is the standard lookback for training queries.
- Consider cost before running BigQuery operations. Ask for permission before running anything more expensive than $10.
- In docs/kix-docs, references to R1, R2 etc refers to the documents docs/kix-docs/docs/Context/r1-entur-rolle-og-mandat.md, docs/kix-docs/docs/Context/r2-entur-teknisk-deep-dive.md etc.