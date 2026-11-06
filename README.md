# kix-avvik
Agent for proaktiv avvikshåndtering, fra Team KI-eksperimentering

## Documentation

- [`docs/operations/RUNBOOK.md`](docs/operations/RUNBOOK.md) — start here to run the service:
  environments, deploy and rollback, alerts, logs, and the traps that have cost the most time.
- [`docs/operations/RETRAIN.md`](docs/operations/RETRAIN.md) — retraining the models and promoting
  them to prd.
- [`CLAUDE.md`](CLAUDE.md) — the detailed guide to this repository, written for Claude Code: the
  project, the tooling, the BigQuery tables, deployment, every front-end and API surface, and the
  conventions.
- [`docs/`](docs/) — findings, design reviews and plans, one directory per investigation.

## Integrating with the notification webhooks

An external client's BFF can receive the alerts `/varsel` already sends its own browsers, as signed
HTTPS webhooks. Start here:

- [`docs/integrations/notification-api.md`](docs/integrations/notification-api.md) — the API and event
  contract, signing, delivery semantics and retention.
- [`docs/integrations/firebase-bff.md`](docs/integrations/firebase-bff.md) — building the receiving
  side with the two vendored TypeScript packages under [`integrations/`](integrations/).
- [`docs/operations/notification-webhooks.md`](docs/operations/notification-webhooks.md) — the
  operating contract: flags, rollout order, logs, and the conditions a client is enabled under.
