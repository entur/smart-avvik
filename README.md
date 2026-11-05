# smart-avvik

smart-avvik predicts how public-transport disruptions in Norway will affect a journey, and warns
travellers before the disruption reaches them ("proaktiv avvikshåndtering"). It reads SIRI-SX
situation messages and SIRI-ET real-time data, scores every upcoming call with BigQuery ML models,
and serves the result as alerts through an API, Web Push and a set of web pages.

It is run by Team Ruter & Reiseplanlegger at [Entur](https://entur.no). It started as kix-avvik,
an experiment by Entur's Team KI-eksperimentering, and keeps that history.

## What is here

| Path | What it is |
| --- | --- |
| [`disruptions/api/`](disruptions/api/) | The FastAPI service: the poller that rebuilds predictions, the notification API, Web Push, the webhook worker, and the APIs behind the web pages. |
| [`demo/`](demo/) | The web front-ends (React and Vite), built into the image. Each has its own README. |
| [`data/queries/`](data/queries/) | BigQuery SQL and scripts for training, calibration and evaluation. |
| [`contracts/`](contracts/) | The notification API's OpenAPI document, event schemas and webhook signing vectors. |
| [`integrations/`](integrations/) | TypeScript packages for a client that receives the webhooks. |
| [`helm/smart-avvik/`](helm/smart-avvik/) | The Helm chart and its defaults. |
| [`eval/`](eval/) | Model evaluation notebooks, rendered to `/eval`. |
| [`docs/`](docs/) | Findings, plans, operations and integration guides. |

The per-environment Helm values, Terraform and deploy workflows are in a private Entur repository.

## Running it

You need Python 3.13 with [uv](https://docs.astral.sh/uv/), and Node 24 for the front-ends.

```sh
uv sync
```

**Tests.** The suites are hermetic: BigQuery, Vertex and the network are stubbed, so they need no
credentials. Run each suite as its own process, the way CI does, because some set environment
variables when imported:

```sh
cd disruptions/api
for suite in test_*.py; do uv run python "$suite"; done
```

The notification store's Firestore checks run against the emulator (Java 21 and `firebase-tools`):

```sh
cd disruptions/api
firebase emulators:exec --project demo-smartavvik --only firestore --config ../../firebase.json \
  'uv run python test_notification_store.py'
```

Checks that compare the code with the private deployment files skip here and say so at the end of
the run (see [`disruptions/api/deploy_files.py`](disruptions/api/deploy_files.py)).

**The service.** Running the API needs Google Cloud credentials with access to Entur's BigQuery
datasets, so it works for Entur developers only:

```sh
cd disruptions/api
uv run uvicorn main:app --reload --port 8000
```

**The image.** The [`Dockerfile`](Dockerfile) builds the front-ends and the API into one image,
from the repository root:

```sh
docker build -t smart-avvik .
```

## Documentation

- [`docs/operations/RUNBOOK.md`](docs/operations/RUNBOOK.md): start here to run the service:
  environments, deploy and rollback, alerts, logs, and the traps that have cost the most time.
- [`docs/operations/RETRAIN.md`](docs/operations/RETRAIN.md): retraining the models and promoting
  them to prd.
- [`CLAUDE.md`](CLAUDE.md): the working guide to the repository. It covers the models, the
  BigQuery tables, deployment, every API surface and the conventions. The notes for each surface
  are in [`.claude/rules/`](.claude/rules/).
- [`docs/operations/`](docs/operations/): how the service is operated.
- [`docs/`](docs/): findings, design reviews and plans, one directory per investigation.

## Integrating with the notification API

A client's backend can read a route's alerts from the public `journey-alerts` endpoint, or receive
them as signed HTTPS webhooks. Start here:

- [`docs/integrations/notification-api.md`](docs/integrations/notification-api.md): the API and
  event contract, signing, delivery semantics and retention.
- [`docs/integrations/firebase-bff.md`](docs/integrations/firebase-bff.md): building the receiving
  side with the two TypeScript packages under [`integrations/`](integrations/).
- [`docs/operations/notification-webhooks.md`](docs/operations/notification-webhooks.md): the
  operating contract, with flags, rollout order, logs, and the conditions a client is enabled
  under.

## Licence

[EUPL-1.2](LICENSE).
