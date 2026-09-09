# kix-avvik
Agent for proaktiv avvikshåndtering, fra Team KI-eksperimentering

## Documentation

- [`CLAUDE.md`](CLAUDE.md) — the working guide to this repository: the project, the tooling, the
  BigQuery tables, deployment, every front-end and API surface, and the conventions.
- [`docs/`](docs/) — findings, design reviews and plans, one directory per investigation.

## Integrating with the partner notification webhooks

An external partner's BFF can receive the alerts `/varsel` already sends its own browsers, as signed
HTTPS webhooks. Start here:

- [`docs/integrations/notification-api.md`](docs/integrations/notification-api.md) — the API and event
  contract, signing, delivery semantics and retention.
- [`docs/integrations/firebase-bff.md`](docs/integrations/firebase-bff.md) — building the receiving
  side with the two vendored TypeScript packages under [`integrations/`](integrations/).
- [`docs/operations/partner-notifications.md`](docs/operations/partner-notifications.md) — the
  operating contract: flags, rollout order, logs, and the conditions a partner is enabled under.
