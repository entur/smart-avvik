# smart-avvik runbook

**Written:** 2026-11-05 for kix-avvik's handover to team-ror, adapted 2026-11-06 to smart-avvik's
two repositories. **Retraining:** [`RETRAIN.md`](RETRAIN.md).

smart-avvik is split in two. This repository is public and holds the code: it builds and tests the
image and deploys nothing. The private
[`entur/smart-avvik-deployment-config`](https://github.com/entur/smart-avvik-deployment-config)
holds the Helm chart and the values per environment, Terraform, the orchestrator manifests and the
deploy workflow. Its `docs/handover.md` records how the service moved from kix-avvik.

This is the short version for people. The long notes are in `CLAUDE.md` and `.claude/rules/*.md`.
They were written for Claude Code, so they are dense and full of history. Read them when you work on
a surface, not to run the service. This file links into them where the detail matters.

## What the service does

smart-avvik predicts how a public-transport disruption will affect a train or bus, and tells
travellers before it happens. It reads SIRI-SX (operator disruption messages) and SIRI-ET (real-time
timetable updates) from Anshar's BigQuery tables, scores every rail call with BigQuery ML (BQML)
models, and pushes alerts.

```
SIRI-SX, SIRI-ET, GTFS, stop registry (BigQuery, other teams' projects)
  -> poller cycle on the leader pod: rebuild *_today tables, run ML.PREDICT   (every 120-185 s)
  -> every API pod serves the built tables
  -> Web Push (/varsel, Rushvarsel), signed webhooks, internal pages behind SSO
```

- **One pod writes.** Every API pod serves every route. Only the pod holding the Firestore lease
  `poller_lease/current` runs the poller cycle and sends pushes. The details, and what to do when
  zero or two pods lead, are in [`poller-lease.md`](poller-lease.md).
- **BigQuery is the database and the bill.** The poller rebuilds the `disruptions.*_today` tables
  every cycle. Each rebuild bills about 0.9 GB of `ML.PREDICT`, so `POLLER_MIN_REBUILD_SPACING_S`
  (120 s) is the cost knob.
- **prd never trains.** Models and lookup tables are trained in dev and copied to prd. See
  [`RETRAIN.md`](RETRAIN.md).
- **Firestore holds state**: registrations, the lease, webhook subscriptions, translations.

Public surfaces: `/varsel` (Smarte reisevarsler), `/togmelding` (Rushvarsel),
`/notification-api/v1` (webhooks), the journey-alerts and journey-status reads. Internal, behind
Entur SSO: `/demo`, `/historikk`, `/meldinger`, `/rushhistorikk`. `/eval` serves committed notebook
snapshots.

## Environments

| | dev | prd |
|---|---|---|
| Host | `smart-avvik.dev.entur.io` | `smart-avvik.entur.io` |
| GCP project | `ent-smartavvik-dev` | `ent-smartavvik-prd` |
| Cluster project (logs, kubectl) | `ent-kub-dev` | `ent-kub-prd` |
| kube context | `gke_ent-kub-dev_europe-west1_kub-ent-dev-001` | `gke_ent-kub-prd_europe-west1_kub-ent-prd-001` |
| Pods | 1-3, rolling | 2-3, rolling |
| Deploys | on every push to `main` | after approval |

There is no tst: kix-avvik never deployed one either. The project `ent-smartavvik-tst` exists,
unused.

Namespace is `smart-avvik` everywhere. Two Helm releases share the chart and image: `smart-avvik`
(API and poller) and `smart-avvik-worker` (webhook delivery, one replica). The chart and the values
per environment are in deployment-config's `helm/smart-avvik/`. Feature flags are the
`configmap.data` keys in `helm/smart-avvik/env/values-kub-ent-<env>.yaml` there.

## Where to look

- **Logs** are in the *cluster* project, not the app project: `ent-kub-prd`, namespace
  `smart-avvik`. Querying `ent-smartavvik-prd` returns an empty list and no error.
  ```sh
  gcloud logging read --project=ent-kub-prd --freshness=1h \
    'resource.labels.namespace_name="smart-avvik" AND jsonPayload.event="poller_cycle"'
  ```
  Logs are structured. Useful `event` values: `poller_cycle` and `poller_phase` (cycle timings),
  `poller_lease` (who leads), `push`, `notification_*`. The ERROR level is kept almost silent on
  purpose, so any ERROR is worth reading.
- **Metrics** are `smart_avvik_*` Prometheus metrics, read in Grafana (grafana.entur.org) through the
  `prometheus-thanos` data source. They are not in Google Managed Prometheus or Cloud Monitoring.
  Pick the environment with `prometheus_group="kub-ent-prd-001"`. The label for the namespace
  differs by metric source, and a wrong label returns no data with no error: see the ground rules
  in [`../observability/02-alert-rules-2026-09-11.md`](../observability/02-alert-rules-2026-09-11.md).
- **Alerts:** a GCP uptime check on `/health` per environment (deployment-config's
  `terraform/monitoring.tf`). prd posts to Slack `#team-ror-prod-alerts` and mails the address in
  `terraform/env/prd.tfvars`; dev only mails. The Grafana rules below were kix-avvik's (folder
  `team-ki-eksperimentering`, Slack `#alerts-kix`) and are **not recreated yet** for the
  `smart_avvik_*` metrics. Nothing pages anyone, at any hour. "critical" means "look at this
  first".
- **Traces** go to Cloud Trace in the app project (10 % sampled in prd).
- **Alert decisions** are logged to BigQuery `disruptions.varsel_decisions`. A nightly scheduled
  query labels them into `varsel_decision_outcomes`, which `/historikk` reads.
- **Who leads:** `kubectl -n smart-avvik port-forward pod/<pod> 18080:8080`, then
  `curl -s localhost:18080/ready`. The body says `role: leader` or `follower`. The image is
  distroless, so there is no shell inside the pod.
- **Secrets** are in Secret Manager in each app project, with copies in team-ror's 1Password,
  mounted as three ExternalSecret groups (the `secrets:` block in deployment-config's
  `helm/smart-avvik/env/values-kub-ent-<env>.yaml`): `varsel` (the VAPID
  key pair, the PostHog token, the feedback receipt secret), `notifications` (the webhook signing
  root and the API-key pepper) and `sso` (the Auth0 client secret and the session secret). A
  changed secret needs an ExternalSecret re-sync (hourly) and a pod restart. **Rotating the VAPID
  keys invalidates every existing push subscription.** The pepper cannot be rotated without
  reissuing every webhook client's credential, and the signing root has its own procedure: see
  [`notification-webhooks.md`](notification-webhooks.md).

## Deploy

A push to `main` here starts the chain:

1. `build.yml` runs `ci.yml`: lint, the test suites, the image build and the scan. The image stays
   as the run's `smart-avvik` artifact.
2. If CI passed and the repository variable `DEPLOY_ON_MAIN` is `true`, its `trigger-deploy` job
   dispatches deployment-config's `cd.yml` with the run id.
3. `cd.yml` takes the image over with `entur/ror-gha-workflows`' `handover-image.yml` (check the run,
   scan, push to the registry as `smart-avvik:main.<date>-SHA<commit>`), runs its `checks.yml`
   against that commit, deploys the API and the worker to dev, and smoke-tests dev.
4. prd waits for approval in the `Approve` environment, then deploys and smoke-tests.

```sh
gh run list --repo entur/smart-avvik --workflow build.yml --limit 3
gh run list --repo entur/smart-avvik-deployment-config --workflow cd.yml --limit 3
```

Check after every merge that the dev deploy ran: `trigger-deploy` only runs after CI passed, so a
red test on `main` means the merge never reaches dev, and the PR shows nothing wrong.

To deploy by hand from deployment-config: `-f run_id=<build run id>` hands over and deploys a build
of `main`, `-f image_tag=main.<date>-SHA<commit>` redeploys an image already in the registry, and no
input deploys the image of the latest build of `main` (only once it is in the registry).

```sh
gh workflow run cd.yml --repo entur/smart-avvik-deployment-config -f run_id=<build run id>
```

`cd.yml` waits up to 6 minutes for a pod that answers `/ready` with `role: leader` and the deployed
commit as `version`. After a deploy, `/health`, `/varsel/` and `/historikk/` should answer 200.

**Never cancel a running Helm deploy job.** Cancelling a run that waits at the `Approve` gate is
fine. Helm rolls itself back if the rollout times out (about 13
minutes). A cancelled run leaves the release in `pending-upgrade`, and every later deploy then fails
with "another operation is in progress". If that has happened:

```sh
helm --kube-context <context> -n smart-avvik history smart-avvik
helm --kube-context <context> -n smart-avvik rollback smart-avvik <last deployed revision>
```

**A change to a configmap value alone does not restart the pods** when you run `helm upgrade` by
hand: the image tag is unchanged, so Kubernetes sees no new pod template, and the app keeps the old
values it read at startup. Follow with `kubectl -n smart-avvik rollout restart deploy/smart-avvik`.
Deploys through `cd.yml` re-render the pod template and are not affected.

**Additive schema changes only, for one release.** During a rolling deploy the new pods load
tables the old leader built. A column read in the same release that adds it stops the new pod from
becoming ready. Ship the tolerant read one release before the column. More in
[`poller-lease.md`](poller-lease.md#deploy-contracts-ktd13).

## Roll back

Deploy an earlier image by dispatching deployment-config's `cd.yml` with its tag. It goes to dev
first, then to prd after approval:

```sh
gcloud container images list-tags eu.gcr.io/entur-system-1287/smart-avvik --limit=10 --sort-by=~timestamp
gh workflow run cd.yml --repo entur/smart-avvik-deployment-config -f image_tag=main.<date>-SHA<sha>
```

`image_tag` is the tag alone, never `smart-avvik:...` or a full `eu.gcr.io/...` reference: the
workflow adds the registry and the name, and refuses anything else. (In kix-avvik a full reference
became a doubled path that took dev down for 5.5 hours on 2026-07-28.)

A rollback runs `checks.yml` against the older commit. Its tests are the older commit's, run against
today's deployment files, and can fail if those changed since. See deployment-config's
`docs/handover.md`.

**Rolling back past 2026-10-19** (kix-avvik PR 312, the lease) is different: an image without the
lease would run the poller on every pod. Put `deployment.forceReplicas: 1` back into the
environment's values file in deployment-config first, then deploy the image. See [`poller-lease.md`](poller-lease.md#rollback). The deploy's
smoke step then fails, because an old image's `/ready` has no `version`. The deploy itself has
already happened at that point.

**If GitHub Actions is down**, deploy from a laptop, in a checkout of deployment-config (the chart
and the values are there). Here the image reference must be **fully qualified**, the opposite of
the dispatch input:

```sh
helm --kube-context gke_ent-kub-prd_europe-west1_kub-ent-prd-001 \
  upgrade smart-avvik ./helm/smart-avvik -f helm/smart-avvik/env/values-kub-ent-prd.yaml \
  --namespace smart-avvik --install --wait --timeout 13m0s \
  --set common.container.image=eu.gcr.io/entur-system-1287/smart-avvik:<tag>
```

Run it with `--dry-run` first. The bare command has no automatic rollback. If `helm dependency
build` complains about a missing repository, ignore it: the chart dependency is vendored.

## When an alert fires

Every rule's query, threshold and intended response is in
[`../observability/02-alert-rules-2026-09-11.md`](../observability/02-alert-rules-2026-09-11.md).
The common cases:

| Alert | What it means | First response |
|---|---|---|
| 1 Poller stopped | No API pod is alive and beating | `kubectl -n smart-avvik get pods`; read the pod's events and last logs. A restart is the usual fix. |
| 1a No poller leader | Pods serve, but nobody rebuilds or pushes | Fires once and clears by itself after a crashed leader (~6 min). If it stays: read `poller_lease` logs; `acquire_failed` or `error` means Firestore is failing. Do **not** delete pods then: a pod cannot start without Firestore, and recovery is automatic when it answers. |
| 1b Two poller leaders | Two pods each believe they lead | Find the stalled pod (gauge 1, `/ready` says follower) and delete it. Never edit or delete the lease document. |
| 2 Rebuild stale | No cycle finished for an hour | Can be a quiet night. In the daytime, read `poller_phase` logs for the phase that hangs or fails, often BigQuery, and check that new SIRI-SX rows arrive. |
| 5 Pod not ready | A pod is out of rotation | During a deploy this is often the outgoing pod. Check rule 10 before acting. |
| 6 ERROR logged | Something logged at ERROR | Read the record in `ent-kub-<env>`. The first ERROR after a restart does not trigger this rule. |
| 9 Labelling stale | The nightly labelling query has not succeeded for 48 h | Check the `varsel-label-daily` scheduled query in the environment's project. Unlabelled decisions are lost after 10 days. |
| 10 5xx rate | Requests are failing | Check pod readiness and recent deploys. |

If a deploy looks healthy but a fix is missing in the browser, compare the `<script src>` in the
page with what the server serves before debugging anything else. An `index.html` cached before the
deploy can still point at the old bundle.

## Scheduled queries

The BigQuery scheduled queries run as the service account
`application@ent-smartavvik-<env>.iam.gserviceaccount.com`, not as a person:

- `varsel-label-daily` labels alert decisions with what actually happened. Recipe and history:
  `data/queries/label_varsel_decisions.sh`.
- kix-avvik also ran a demo refresh (`demo-recent-daily` in dev, `demo-copy-daily` in prd). It is
  not registered for smart-avvik: `/demo` had no requests in the four weeks before the move. The
  dev `demo_*` tables were copied once; `data/queries/refresh_demo_recent.sh` refreshes them.

A scheduled query holds **a copy** of its SQL. Editing the `.sql` file in git changes nothing until
the config is updated with `bq update --transfer_config`. Adding a column to
`varsel_decision_outcomes` breaks the labelling job until that is done: the `ALTER TABLE` and the
config update are one operation. Creating a config with `bq mk --transfer_config` needs
`--service_account_name`, and creating one also starts one run immediately.

```sh
bq ls --transfer_config --transfer_location=EU --project_id=ent-smartavvik-prd
```

## Gotchas

These are the traps that have cost the most time. Until 2026-11-05 most of them lived only in one
developer's private notes.

**Authentication and GCP**

- A `bq` "Access Denied" or "missing `bigquery.jobs.create`" on either project is almost always an
  expired local login. Run `gcloud auth login` before suspecting IAM.
- `gcloud container clusters list --project ent-smartavvik-prd` gives 403. The clusters live in the
  shared `ent-kub-*` projects. That is not a permissions problem.
- A GCP API being enabled and a role being granted are separate. When an AI function fails in
  BigQuery, check that the Vertex AI API is enabled in the project, not only the IAM role.
- A "Dataplex" line on the bill is BigQuery Data Lineage, captured for every poller rebuild. On
  2026-08-10 it was enabled in dev only and was about 24 % of the project bill. Check with
  `gcloud services list --enabled --project=ent-smartavvik-dev | grep datalineage`; nothing in the
  repo uses it, so it can be disabled.

**CI**

- CI runs each Python test file as `python <file>.py`, not with pytest. A test file without a
  `__main__` runner at the end runs nothing and passes. A runner placed mid-file skips every test
  below it. When you add tests, check that the reported count went up.
- The runner stops at the first failing test file. After fixing one red suite, run every later
  suite before pushing.
- CI also runs Node suites: `demo/avviksvarsel` and the two packages under `integrations/`. A change
  to `contracts/notifications/v1/` is only verified by those.
- **No SQL runs in CI.** CI has no GCP credentials. A syntax error in a `.sql` file passes CI and
  first fails in production. Dry-run every changed query: `bq query --dry_run < file.sql`. A dry run
  does not evaluate parameters, so an empty `ARRAY` parameter (which BigQuery turns into NULL) is
  only caught by running the query.
- Some files under `disruptions/api/*.sql` have a runnable twin under `data/queries/`. Check for
  one before editing.
- The checks that compare the code with the deployment files (`disruptions/api/deploy_files.py`)
  skip here and run in deployment-config's `checks.yml`, against this repository's `main`. A change
  to those tests must merge here first, or deployment-config's next PR fails.
- The Helm lint and render checks are in deployment-config too. A `helm-lint` job reported as `fail`
  can be a cancelled analytics step after a successful lint. Check which step failed, then
  `gh run rerun <run-id> --failed`.
- A job with no steps and no log ("log not found", `BlobNotFound`) never ran. Rerun it.
- Test fixtures seeded from the wall clock have caused most of the flaky tests. A date literal next
  to code that prunes by the real clock fails permanently from some date on (seen 2026-10-30).
- A PR title must be a valid conventional commit (`feat`, `fix`, `docs`, `chore` ...), and so must
  the commit's subject when the PR has one commit. Renaming the PR title is not enough then. `ops` is
  not accepted.

**BigQuery CLI**

- Submit long statements from a file: `bq query --nosynchronous_mode < file.sql`. Long inline
  statements have hung client-side, and a file starting with a `--` comment is parsed as a flag
  when passed as an argument.
- `--parameter=name:STRING:` sends an empty string, not NULL.
- Never chain `bq query --dry_run ... && bq query ...`. It once billed 152 GB that the dry run was
  supposed to prevent.

**Terraform**

- PR plans run as a read-only identity, applies as a stronger one. A resource the apply can create
  but the plan cannot read breaks every later PR plan. The way out is a `removed { ...
  lifecycle { destroy = false } }` block, which skips the refresh.
- Terraform is in deployment-config. Grant dataset access to a **group** in its
  `terraform/env/<env>.tfvars`, never a person. Keep the
  resource type `google_bigquery_dataset_iam_member`: the `_binding` and `_policy` forms would
  remove the prd service account's access. Details in `terraform/bigquery_iam.tf`.

**Monitoring**

- A Grafana rule that cannot fire looks exactly like a healthy one. A wrong label selector does
  it (no data, no error). So did the rule importer: from 2026-09-18 to 09-23, five imported rules
  compared a constant instead of their query. The only check is to force the query past the
  threshold and watch the rule fire. The rules doc describes how.
- An unset `prometheus_client` gauge exports `0`, not nothing, and both the API and the worker
  export every metric. That is why the rules wrap gauges in `max()`.

**Time zones**

- The product runs on Oslo time (rush hours, `call_date`, push cut-offs). The infrastructure runs
  on UTC (BigQuery batch rewrites, scheduled queries). Scheduled queries move one hour against Oslo
  twice a year. A daily job and its "already ran today" flag must use the same calendar.
- `realtime_siri_et_last_recorded` is rewritten daily around 05:03 Oslo time. A job that reads it
  earlier reads yesterday's batch.

## Further reading

- [`poller-lease.md`](poller-lease.md): the leader lease, handoffs, outages, rollback.
- [`../observability/02-alert-rules-2026-09-11.md`](../observability/02-alert-rules-2026-09-11.md):
  every alert rule and how they were created.
- [`notification-webhooks.md`](notification-webhooks.md): the webhook channel's operating contract.
- [`gemini-tak-og-kostnad.md`](gemini-tak-og-kostnad.md): LLM caps and cost.
- `CLAUDE.md` and `.claude/rules/<surface>.md`: per-surface detail.
- deployment-config's `docs/handover.md`: how the service moved from kix-avvik, and what is left.
