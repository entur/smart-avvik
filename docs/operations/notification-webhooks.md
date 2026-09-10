# Notification webhooks — operating contract

The channel through which `/varsel`'s alerts reach an external client's BFF, as signed HTTPS
webhooks. This is the operator's document: what the flags are, in what order to turn them on, what the logs
say, what the retention text promises and which function keeps that promise, and what must be
recorded and signed off before a client is enabled.

The client-facing contract is `../integrations/notification-api.md`; the integration guide for the
two vendored TypeScript packages is `../integrations/firebase-bff.md`. The plan of record is
`docs/plans/2026-09-08-001-feat-partner-notification-webhooks-plan.md`.

**Nothing below is a claim about alert quality, and nothing anywhere in this feature is a claim about
any operator but Vy rail.** See [Enablement conditions](#enablement-conditions-ktd12).

---

## The four flags, and the order they go on in

The channel is on in dev and prd since 2026-09-10 and off in tst. Turning it on is four values and they are **not
interchangeable**; the first is a prerequisite for the third, and the third is the only one that
takes effect **without a restart** — every value here is configmap- or secret-sourced, so a pod
restart is needed anyway to change what the process sees, and what flag 3 buys is that it is re-read
on every cycle rather than latched at import.

### 1. `NOTIFICATION_FIRESTORE_DATABASE` — the named database

`helm/kix-avvik/values.yaml`, uncommented, `notifications` in all three environments.
Read once at startup by `main.lifespan` → `notification_store.configure(database=...)`.

`notification_store._firestore_client()` **raises `NotConfigured` when it is empty and never falls back to
`(default)`** — the safe direction, and the reason this base value must stay where it is: remove it
and a live flag flip becomes a no-op that logs one warning per cycle and reads exactly like a healthy
deployment with nothing registered.

The same literal is `terraform/variables.tf`'s `notification_firestore_database` default, and a test greps
both files, because a mismatch between the chart and Terraform fails the same silent way.

### 2. `NOTIFICATION_KEY_PEPPER` and `WEBHOOK_SIGNING_ROOT` — Secret Manager, per environment

**Both exist in dev and prd since 2026-09-10 and not in tst.** Both are read per call, both refuse a value
under 32 bytes, and both fail **closed**:

- no pepper → `notification_auth.available()` is false → every `/notification-api/v1` route answers
  **503 `channel_not_configured`**, never 401;
- no signing root → `webhook_delivery` logs `webhook_delivery` / `result="unsigned"` and leaves the
  event **pending**. Nothing is burned; the events expire on their own horizon.

Their Helm names ship **commented** in tst's environment file, in its own `# notifications:`
ExternalSecret group, and uncommented in dev's and prd's since 2026-09-10. Two rules, both written into those files:

- **Never append them to the `varsel` group.** An ExternalSecret is all-or-nothing under
  `secretVersionSelectionPolicy=LatestOrFail`, and the chart's `envFrom.secretRef` carries no
  `optional: true` — so one unresolvable name deletes the group's Secret and leaves the pod in
  `CreateContainerConfigError`. In the `varsel` group that takes `VAPID_PRIVATE_KEY` down and
  silently disables Web Push for real travellers, over a feature that is switched off.
- **Uncomment only in the change that turns the channel on, and only after
  `gcloud secrets versions list` shows a version in that project.** A name never lands in Helm ahead
  of its value.

Write them with `printf '%s'` and verify with `wc -c` and `tail -c1 | xxd -p`. A trailing `0x0a`
re-hashes every credential and re-derives every destination secret at once; both modules only strip
it and warn **once per process** (`notification_auth_config`, `webhook_signing_config`), so the warning is
easy to miss and the symptom differs per secret: a stripped **pepper** re-hashes every credential, so
every client 401s at once; a stripped **signing root** re-derives every destination secret, so
nothing 401s at all and every client's receiver silently rejects the signature instead.

Values must be distinct per environment. No dashes in either name — the common chart's template fails
on one.

**The pepper cannot be rotated without reissuing every client credential**, because the stored
hashes cannot be re-derived. The signing root can, generation by generation.

### 3. `NOTIFICATION_WEBHOOKS_ENABLED` — the channel gate

`helm/kix-avvik/values.yaml`, `"false"`; dev and prd override it to `"true"` since 2026-09-10 (with no client
onboarded — the population is empty until the operator tool creates one, and the KTD12 checklist
below still gates every client). Allow-list parse
(`true/1/yes/on`), **unset = off**, **read at call time** — so it takes effect without a restart,
*given* flag 1.

It gates the **notification pass**, not the API: with it off, the poller never loads a subscription,
never mints an event and never delivers, while `/notification-api/v1` still serves. That asymmetry is
deliberate — gating onboarding on the flag that needs onboarding is circular.

With it off **neither** notification phase enters — `notification_webhooks` nor `notification_servability` — so
`poller_cycle.phases` carries no key for either. An operator reading a cycle breakdown sees the keys
absent, not zero.

**That gating has one consequence to know during onboarding, and the reason for it is the interesting
part.** `notification_servability` is what re-asks a stale or never-established servability answer every
six hours and clears the caveat from a pair a later timetable connects. Gated, it does not run until
this flag is on in that environment — so a client who registers before enablement sees the
**create-time** answer, with `servable_checked_at` publishing its age. That is R6's "how fresh"
rather than an all-clear, and it is worth saying plainly because the obvious workaround does not
work: an identical re-`PUT` is a `touch_subscription`, **not** a re-ask, and only a *changed*
definition re-asks.

Gating it anyway is the right trade. Ungated, every deployment that has onboarded nobody — all three
today — and tst, which has no named database, would read Firestore and ask BigQuery on every cycle
for an empty population. And "the flag is unset, so no notification state was read" would stop being a
property of the poller, which is the sentence this document leans on everywhere else.

### 4. `NOTIFICATION_WEBHOOK_LLM_ENABLED` — webhook wording

**Read at IMPORT in `notification_wording`, so this one does need a restart.** Unset it and the
channel still emits and delivers, carrying the deterministic template wording; the model is not asked
and nothing is charged.

**It is set in TWO Helm values files — dev and prd, both since 2026-09-10 — and the reason is not
the webhook channel.** The public journey-keyed read (`GET /notification-api/v1/journey-alerts`, plan
2026-09-10-001) follows this same switch: whether an entry's `payload.notification` carries the
model's prose is `notification_wording.PULL`'s `deliver` gate, which is `webhook_delivery_enabled()`,
the same function the event's wording reads. The flag is import-time and defaults off, so without
the chart line the read would have shipped template-only in dev and defeated the decision behind its
enrichment — hence `helm/kix-avvik/env/values-kub-ent-dev.yaml` sets it to `"true"`, with the comment
naming the read as the reason and `NOTIFICATION_WEBHOOKS_ENABLED` (off in dev) as what keeps it
inert for events: no subscription is loaded and nothing is minted or delivered, so the only surface
the value reaches in dev is the read. prd set it later the same day, on the product owner's call,
for the same reason and with the same thing keeping it inert for events (`NOTIFICATION_WEBHOOKS_ENABLED`
is off in prd too), so a read on `kix-avvik.entur.io` carries the model's prose under the read's own
allowances. tst does not set it; there every entry the read serves reads `wording.status: disabled`
with the template, which is correct and complete. When the webhook channel itself is enabled in an
environment, this flag's value is the rollout order's second step below and a restart applies it.

**The bad-text revert is therefore TWO switches, and the one an operator already knows does not
reach every surface.** `VARSEL_LLM_ENABLED=false` returns the *browser push* to the template and
nothing else: the read and the webhook event serve the poller's cached model text under this gate,
not the browser's. So:

- bad text on the **push** → `VARSEL_LLM_ENABLED=false`, no restart;
- bad text on the **read or the event** → `NOTIFICATION_WEBHOOK_LLM_ENABLED=false` **plus a
  restart**, which returns both to the template and leaves the push alone;
- egress must stop everywhere → `VARSEL_LLM_PROVIDER=off`, the one switch that stops every surface
  asking.

Whether the read should have a switch of its own rather than following this one is an open question
for the product owner; the default this plan shipped is one switch.

**The read's Gemini egress is read as covered by the two existing decisions, and the owner if that
reading is contested is the DPO.** The 2026-08-07 egress decision and prd's classification of the
origin, destination and departure-time trio as «Intern» were taken for registered travellers. The
read's prompts carry only an anonymous definition — a stop pair, a period, weekdays — and no
identity, no device and no registration; a wording is written through `notification_wording.PULL`,
whose truthy `caller` suppresses the prompt/reply record in code, and the model-call record carries
the salted per-process pair digest rather than a definition digest. That is strictly less than what
the covered decisions already send.

A consequence worth knowing before anyone compares two wordings: with `VARSEL_LLM_ENABLED=true`
(prd's setting since 2026-08-23) beside `NOTIFICATION_WEBHOOK_LLM_ENABLED=false`, **the Web Push
carries the model's prose while the notification event carries the template**. The two channels are
byte-identical only where both switches agree, and the divergence is asserted explicitly by the test
suite rather than left to be discovered.

Note also that client prose inherits `/varsel`'s egress kill switch: `VARSEL_LLM_PROVIDER=off` stops
a model being asked on **both** surfaces.

### One more, and it must never appear in a chart

`KIX_ALLOW_WEBHOOK_NETWORK` is the outbound test fence in `webhook_sender`. Unset means "allowed
outside a test process", which is what a deployment wants. Setting it to a falsy value in a
deployment silently blocks every delivery.

---

## Enablement conditions (KTD12)

**No client is enabled for an operator `/historikk`'s own verdicts do not cover without that gap
being recorded and signed off.** That is a Definition-of-Done term, not advice, and it is the whole
of what replaced the origin plan's dated-leg replay gate. It survives as five limitations, of which
**only the first three are inherited — the fourth is created by this plan, and the fifth by the
public read that followed it.**

1. **`/historikk`'s rows are one per subject per cycle**, the repeats being the lead-time series,
   while a notification fires on the first threshold crossing. A rate over rows is not a rate over
   notifications.
2. **A subject nobody was notified about has no row at all**, so capped and missed subjects must be
   returned to the denominator by hand — and this channel *manufactures* capped subjects by design:
   nine of thirteen in the line-suspension case the daily cap is specified against. That limitation
   binds harder here than it did where it was written.
3. **21 308 of 21 331 replayed subjects were VYG**, so the evidence is a statement about **Vy rail**
   and nothing else.
4. **A webhook-only route produces no `varsel_decisions` row at all.** `_record_decisions` iterates
   the browser grouping, and KTD10 keeps webhook subjects out of it, so `/historikk` scores the
   *shared* definitions and nothing else. prd's own registrations are the team's test users, so a
   client's route mix overlaps them only by accident. **Webhook-only routes are not thinly
   evidenced; they are unscored, deliberately.** Scoring them is deferred work, and the seven-day
   event retention below is the constraint that work would first have to lift. **Deferred by
   decision on 2026-09-09** until `/historikk` and `/meldinger` are behind authentication, and to be
   done then — see [Open questions](#open-questions-and-product-decisions), item 4. **They are, since
   2026-09-10** (Entur SSO via Auth0's partner tenant, `disruptions/api/sso.py`, every environment),
   so the deferral has ended and the work is owed.
5. **A definition nobody registered produces no `varsel_decisions` row at all, and the public read
   serves exactly those** (plan 2026-09-10-001, origin KTD10). `GET /journey-alerts` runs the
   matcher for whatever five fields a caller names, records nothing — no decision row, no Firestore
   document — and so what it answers for an ad-hoc favourite is **unscored**: not thinly evidenced,
   unscored, and by the same decision as the fourth. The read's own contract says so, and says an
   empty list is never all-clear. Where the caller's five fields are verbatim a registration's the
   entry is the registration's own wording and that registration's row exists — but the read cannot
   tell the client which case it is in, deliberately, and no claim about the panel's alert quality
   follows from `/historikk` for any route a browser registration does not also hold.

Therefore: **no generalized alert-quality claim, no per-route claim, and no claim about any operator
but Vy rail.**

### Before a client is enabled — the checklist

| # | Item | Who |
|---|---|---|
| 1 | `/historikk`'s verdict counts recorded **for every operator the pilot's subscriptions reach**, with a go/no-go per operator | team |
| 2 | Any subscription set reaching an operator those verdicts do not cover **recorded as explicitly unmeasured and signed off** | product owner |
| 3 | The pilot destination's **cold-start latency measured against the 5 s per-attempt ceiling**. A receiver slower than the ceiling is never delivered to *at all* — see [Delivery](#delivery-and-the-cycle-budget) | team + client |
| 4 | The pass's share of the cycle measured under a hanging destination, a slow store and a backlog **in the target environment** | team |
| 5 | The controller question answered — see [Open questions](#open-questions-and-product-decisions) | team + DPO |

---

## Rollout order

1. **dev, deterministic wording.** `NOTIFICATION_WEBHOOKS_ENABLED=true` and, for this step only,
   `NOTIFICATION_WEBHOOK_LLM_ENABLED` set back to `"false"` — dev has carried `"true"` since
   2026-09-10 for the public read (section 4 above), so the template-only step is a deliberate
   flip down rather than the starting state. Register a test-only subscription against a receiver
   you control, send a `/test-events` event, and watch a real disruption produce a real event end
   to end.
2. **dev, model wording.** Return `NOTIFICATION_WEBHOOK_LLM_ENABLED` to `"true"` and restart.
   Compare a client event's `payload.notification` against the Web Push body for the same journey;
   they must agree — and against the public read's entry for the same five fields, which must be
   byte-identical to the event's.
3. **A test client in prd**, with the KTD12 checklist above completed and every subscription
   `test: true`.
4. **The pilot**, with real subscriptions.

**Apply `terraform/` before the code that reads the store deploys.** Merging to `main` applies dev;
tst and prd are `gh workflow run terraform.yml -f environment=<env>`, dispatch-only exactly as
`cd.yml` is.

`.entur/application.yaml` is different in kind: `entur apply` on that PR grants `roles/datastore.user`
in all three projects at once, before any of them has a database, which is harmless and is why it
need not be sequenced. **Verify the binding landed** — the platform's filter is a comprehension guard
that drops a non-matching role *silently*, with no error and no binding:

```sh
gcloud projects get-iam-policy ent-kixavvik-dev \
  --flatten='bindings[].members' \
  --filter='bindings.role:roles/datastore.user' --format='value(bindings.members)'
```

Credential rotation needs both an ExternalSecret re-sync (hourly) and a pod restart.

**Reverting.** Bad client *text* — on the webhook event **or on the public read**, which follow one
switch — → `NOTIFICATION_WEBHOOK_LLM_ENABLED=false` plus a restart; the template goes back out on both
and nothing else changes. Note the other direction: `VARSEL_LLM_ENABLED=false` reverts the *browser
push* only and reaches neither of these. Egress must stop → `VARSEL_LLM_PROVIDER=off`, which stops
every surface asking, the read included. The whole channel must stop →
`NOTIFICATION_WEBHOOKS_ENABLED=false`, which takes effect on the next cycle with no restart and
**preserves every subscription**: nothing is deleted, no obsolete event is replayed on resume (the
pending set is read from Firestore and every event carries its own validity horizon), and the first
cycle after resume is bounded by the same 28 s ceiling as any other.

---

## Infrastructure

### Terraform (`terraform/firestore.tf`)

The first non-IAM resource this directory creates.

| resource | notes |
|---|---|
| `google_project_service.firestore` | `firestore.googleapis.com`, `disable_on_destroy = false`, ordered first |
| `google_firestore_database.notifications` | `notifications`, `europe-west1` (**immutable**), `FIRESTORE_NATIVE`, delete protection and PITR in prd only, `deletion_policy = "ABANDON"` |
| `google_firestore_index.notification` | `for_each` over `firestore.indexes.json`, **keyed on the index's own content**, never its array position — reordering the file would otherwise destroy and recreate every index |
| `google_firestore_field.notification_ttl` | `expires_at` on all four notification collections. **No `index_config {}` block** — an empty one disables all indexes on the field, which is not what it looks like |
| `google_firestore_backup_schedule.webhook_daily` | prd only, `daily_recurrence` with `retention = "3024000s"` — five weeks, a chosen value rather than a ceiling. Firestore allows up to 14 weeks for both daily and weekly recurrences (checked against Google's documentation, 2026-09-09). Five is what PITR does not already cover: PITR gives the last seven days at minute granularity, so the backup buys the month beyond it |
| *(no Rules resources)* | `firestore.rules` is **deliberately not released by Terraform** since 2026-09-10. It was, for one day (`google_firebaserules_ruleset` + `_release`, `cloud.firestore/notifications`, added 2026-09-09), and the apply worked — but refreshing the ruleset on every later plan needs `firebaserules.rulesets.get`, which the pull-request plan identity (`gh-kix-avvik-f849-ci@`, `roles/viewer` + the org's `githubCiTerraformPlanReader`) lacks while the push-apply identity (`…-dev@`, `roles/firebase.admin`) holds it, so every Terraform PR after that apply failed its plan. Rules govern client-SDK access only, no Firebase app exists here, and a database with no release denies every client anyway. Two `removed { destroy = false }` blocks forget the pair (dev's live release stays deny-all, unmanaged); re-add them, with the Team Plattform ask for the plan-reader permission, the day a Firebase app is registered |

**Do not add an IAM grant to this file.** `roles/datastore.user` lives in `.entur/application.yaml`
because the two paths meet two different guards: a `google_project_iam_member` here runs under a
`projectIamAdmin` conditioned by `only_assignable_roles`, a CEL `hasOnly()` over a list carrying no
`roles/datastore.*`. It governs IAM grants only, which is exactly why it does not reach a database, an
API enablement or an index.

**That diagnosis is settled** (2026-09-01, by reading `entur/tf-gcp-apps`' `github_iam.tf`, which
attaches the condition; `terraform/firestore.tf` and `.entur/application.yaml` both carry it). The
parked `terraform/oauth_iam.tf` that read the same 403 as an org-level deny policy was retired on
2026-09-10 together with the Google-OAuth SSO route it served — the gate now uses Auth0's partner
tenant, which needs no project IAM at all (`disruptions/api/sso.py`).

**Plan-time index drift rests on one config line, and it is present:**
`.github/workflows/terraform.yml` lists `firestore.indexes.json` beside `terraform/**` in **both**
of its `paths:` lists (and, since 2026-09-10, deliberately not `firestore.rules` — nothing in
`terraform/` consumes it any more). Without it, a PR adding a query and its index entry would touch neither
`terraform/` nor the workflow, pass the static coverage test, merge, apply nothing, and land a runtime
`FailedPrecondition` in production. A missing composite index is never a deploy-time error and the
emulator tracks no indexes at all, so this is the earliest anything can catch it.

Expect the **first apply in each project to need a re-run** if `firestore.googleapis.com` has not
finished propagating by the time the database resource is created; the platform's own project module
sleeps 60 s after enabling APIs for that reason.

**The Rules release WAS accepted in a project Firebase has never touched** — the 2026-09-09 apply
created `cloud.firestore/notifications` from Terraform's client exactly as predicted, so that question
is answered. What nobody had checked was the *plan* side: the pull-request plan runs as a different,
read-only identity than the push apply, and that identity cannot `get` a ruleset. The pair was
therefore forgotten again on 2026-09-10 (see the table above); the confirmed release name and the
apply identity's `roles/firebase.admin` are what make re-adding it a small change when a Firebase app
ever exists here. Worth knowing for the next resource a plan cannot read: a `removed { destroy = false }`
block is **not refreshed** during plan (verified on that PR's own plan — no `Refreshing state…` line,
just "will no longer be managed by Terraform, but will not be destroyed"), so the pair left state on a
green plan; deleting the resource blocks outright would have planned a destroy, which refreshes first.

### Collections

Two planes, and only one of them has retention.

**Notification plane** — `subscriptions`, `alert_subjects`, `events`,
`deliveries`. Every subscription document carries `channel` (`webhook` today; `web_push` is the value
`/varsel`'s registrations take when they move into this store, named now so that move is a new value
and not a backfill) and `daily_cap` (the client's, copied on every client write). All four TTL on
`expires_at`. All four **top level**, carrying a
`subscription_id`, because a document delete does not remove subcollections and a TTL field does not
cascade into them — as subcollections they would be orphaned by a delete and the delivery query would
keep returning them forever.

**Identity plane** — `clients`, `credentials`, `destinations`,
`admin_audit`. `clients` carries the three quota fields (`max_active_subscriptions`,
`requests_per_minute`, `daily_cap`); a document without them reads as the defaults. **No TTL field
config, no index entry, no Terraform, no retention.**
`credentials` carries an `expires_at` that is a validity check in code, not a TTL.
`admin_audit` is append-only and unbounded, deliberately.

### Indexes (`firestore.indexes.json`, repo root)

Seven composite indexes, one file read by three consumers — the emulator, the static coverage test and
Terraform. The three cleanup queries filter on `subscription_id` alone and are served by automatic
single-field indexes; they are declared as query specs with empty index fields and are deliberately
absent from this file.

---

## The operator tool

```sh
uv run --with google-api-python-client python scripts/manage_notification_client.py \
  --environment dev <subcommand> ...
```

**The `--with google-api-python-client` is not optional.** The Cloud Identity client is not a repo
dependency; without it the authorizer cannot import, fails closed, and **refuses every subcommand**.
Check that first when somebody who *is* in the group is refused.

`--environment {dev,tst,prd}` is required and never inferred. `--project` defaults to ambient ADC;
`--database` defaults to `$NOTIFICATION_FIRESTORE_DATABASE` or `notifications`.

| subcommand | effect |
|---|---|
| `create-client` | the account. Idempotent on (id, name) |
| `suspend-client` / `resume-client` | credentials answer 403 while suspended; takes effect within 60 s |
| `show-client` | account, credentials, current destination — **prints the callback URL to stdout** |
| `set-quotas` | raises or lowers a client's three ceilings — `--max-active-subscriptions`, `--requests-per-minute`, `--daily-cap`; unnamed ones keep their value; bounded by `notification_auth.QUOTA_BOUNDS`; audited as `set_client_quotas` with the requested values in `reason`. `create-client` takes the same flags for a NEW account |
| `issue-key` | mints a credential and **prints the bearer secret once**. No reversible copy exists |
| `revoke-key` | terminal, effective on the caller's next request |
| `list-keys` | key ids and state, never a secret |
| `create-destination` / `amend-destination` | the URL is address-tested before the write |
| `pause-destination` / `resume-destination` | `resume` also lifts a pause a 410 caused |
| `show-signing-secret` | prints **every live generation's** signing secret for a destination, each with its number and its `active`/`retiring` role. **Prints a live credential** — see below |
| `rotate-signing` | mints the next generation; the current one retires, both live at once |
| `retire-signing` | drops the retiring generation after cutover |

### `show-signing-secret` — the one command that prints a live credential

It is the only path to a destination's verification secret, and there is deliberately no other:
`GET /destinations/current` serves generation *numbers* and never calls the derivation, because a
secret fetchable over the client's own API credential is a secret that credential can be traded for.
Nothing is stored or looked up — the value is **derived** from `WEBHOOK_SIGNING_ROOT` over
`destination_id || generation`, which is why the store holds no signing material and one client's
leaked receiver secret forges nothing for another.

**It needs that environment's `WEBHOOK_SIGNING_ROOT` exported**, like `issue-key` needs the pepper,
and there is an early pre-check that says so. That pre-check runs **before** the group check, on
purpose: without it a missing export would surface as `SigningNotConfigured`, whose message is about
Secret Manager and the chart's secret group — the wrong hint for a human at a terminal who has simply
not exported a variable — and worse, it would read as an authorization failure. A missing root exits
**2** with a message about the variable and writes no audit row; an authorization refusal exits **3**
and does.

**It prints every live generation**, each with its number and its `active`/`retiring` role, not just
the active one. During a rotation two verify at once and "which one do I install?" is the first
question a client asks: install the active one, keep accepting the retiring one until
`retire-signing` runs.

**Handling.** This is the one command whose output must not go into a ticket, a chat message or a
shell history file. Hand it over the way a password is handed over. If the value has been anywhere it
should not, `rotate-signing` the destination — that is what the two-generation overlap is for, and it
costs the client nothing but adding a string to the array their receiver already accepts. The read
is audited before the value is produced (`action="show_signing_secret"`, `result="disclosed"`, with
the generation numbers in `reason`), so a refusal partway through still leaves the row saying somebody
asked; a credential disclosure is precisely what an append-only audit exists for.

**Authorization** is a named group, checked transitively through Cloud Identity, with the actor taken
from ADC and never from an argument:

| environment | group |
|---|---|
| dev, tst, prd | `sg-dig-team-ki-eksperimentering@entur.no` |

**That is the team group, in every environment, and it is a pilot shortcut decided on 2026-09-10.**
R22 asked for a dedicated least-privilege group with production as its own
(`kix-avvik-notification-operators[-prd]`), and that remains the end state. It is not what ships
because a new group at Entur is not self-service: the `sg-dig-*` groups are Entra security groups
that Team Plattform creates through `tf-azure-ad` and grants through `tf-gcp-iam` (ask in
`#talk-utviklerplattform`), they are addressed `@entur.no` — never `@entur.org`, which the first
version of this table named — and nobody had requested them. The team group needs no
`roles/datastore.user` grant of its own: it already reads both `notifications` databases through the
roles it holds on the folder above the projects (`entur.devOps` among them; verified from a member's
ADC in dev and prd on 2026-09-10), and `checkTransitiveMembership` answers for it. What the shortcut
gives up is R22's separation — everyone on the team is a prd operator — which is acceptable while
prd's registrants are the team. Moving to the dedicated groups is `OPERATOR_GROUPS` in the tool
plus this table, once Team Plattform has created them and granted `roles/datastore.user` in each
project; nothing else changes, because project IAM, not this table, bounds what a member reaches.

**Refusals fail closed on every error**, including a permission error on the membership check itself
(reason `membership_check_failed: ...`). A refusal writes a JSON line to **stderr** and then
best-effort appends an audit row — best-effort because the same IAM that gates the action may gate
the audit write, which is exactly why the stderr line always goes out.

Exit codes: `0` success, `2` a missing secret or an auth-plane error, `3` refused. Two subcommands
carry an early pre-check on the environment variable they need — `issue-key` on
`NOTIFICATION_KEY_PEPPER`, `show-signing-secret` on `WEBHOOK_SIGNING_ROOT` — and **both run before the
group check**, so a missing export exits 2 with a message about the variable and writes **no audit
row at all**. That ordering is deliberate: reported after the group check, a missing export would
read as an authorization failure and send an operator to the wrong place entirely.

The audit record — `audit_id` (a sortable millisecond timestamp plus random bytes), `actor`, `action`,
`result`, `at`, `client_id`, `destination_id`, `key_id`, `reason` — is append-only; nothing in this
repo updates or deletes a row. Its own limitation is stated rather than hidden: it proves what was
attempted and by whom it *claimed* to be, not that the claim was verified.

---

## What the logs say

App logs live in the **cluster** project — `ent-kub-{dev,prd}`, namespace `kix-avvik` — not in
`ent-kixavvik-*`. A `gcloud logging read --project=ent-kixavvik-prd` for any of these returns nothing,
silently.

| event | where | carries |
|---|---|---|
| `varsel_webhook_load` | the pass's one read of notification state | `result` (`ok`/`error`/`skipped`/`flag_error`), `n`, `skipped`, `truncated`, `ms` |
| `notification_store_load` | the store side of the same read | `n`, `skipped`, `expired`, `truncated` |
| `notification_emit` | the emit step | `result` (`ok`, `timeout`, `retryable`, `failed`, `standing_error`, `run_clear_error`), `created`, `withdrawn`, `unchanged`, `obsolete`, `quota`, `capped`, `retryable`, `ms` |
| `notification_event` | one committed revision | `result` = the event type, `client_id`, `destination_id`, `alert_id`, `revision`, `superseded` |
| `webhook_delivery_round` | the delivery step | `sent`, `retryable`, `expired`, `cancelled`, `capped`, `deadline`, `paused`, `skipped`, `skipped_destinations`, `store_errors`, `throttled`, `ms` |
| `webhook_delivery` | one delivery fault | `result` (`unsigned`, `pause_failed`, `store_error`, `error`, `expire_error`, `pending_error`, `expire_timeout`), `destination_id`. **`expire_timeout`** means the stale-expiry sweep hit its bounded wait — the store is demonstrably hung, so the delivery pool is not created at all on that cycle rather than leaking four more threads to reach the same answer |
| `notification_quota` | an R21 ceiling binding | `result` (`pending_events`, `alert_state`, `delivery_attempts`, `subscriptions`), the count; `delivery_attempts` also carries `throttled` (destination ids), `truncated` (the scan bound was hit before the collection was drained) and `scanned` (pending documents read) |
| `notification_subscription` | a create, no-op or tombstone | `result`, `client_id`, `subscription_id` |
| `notification_cleanup` | a deletion pass | `result`, `subscription_id`, the three deleted counts |
| `notification_auth` | a refused credential | `reason`, `key_id`. **The secret is logged nowhere at any level** |
| `notification_admin` | an operator action | `action`, `result`, `actor`, `client_id`, `destination_id`, `key_id`. `action="show_signing_secret"` with `result="disclosed"` is a **credential read** — the audit document beside it carries the generation numbers in `reason`, never the secret |
| `notification_test_event` | a synthetic send | `client_id`, `key_id`, `destination_id`, `result` (`ok`, `failed`, `timeout`), `status`. **`result="timeout"`** — a WARNING, and the only one of the three without a `status`, because no response was received — means the bounded wait around the whole delivery elapsed — DNS is the reason it exists, since `socket.getaddrinfo` is reached by no deadline in `webhook_sender`. The route still answers **200** with `delivered: false`; a 504 would read as our failure when it is the destination's |
| `notification_servability` | the six-hourly re-check sweep, and its per-record faults | `result`: `swept` (a clean pass), `capped` / `deadline` (a bound bound — logged at WARNING, because a ceiling nobody can see binding is not a ceiling anybody can size), `load_failed` (the store could not be read; every caveat stands), `vanished` (deleted while we were asking), `superseded` (the route was replaced while the check ran, so the verdict describes a definition that no longer exists and is discarded rather than stamped — logged at DEBUG here on the poller's cadence and at INFO on the request path, where an operator may correlate it with a client's report), `error` (the seam raised or timed out) and `write_failed`. A sweep record also carries `due`, `checked`, `cleared`, `unservable`, `truncated`, `ms`. **`error` and `write_failed` now come from two call sites**: the create path logs neither id, the re-check logs `client_id` and `subscription_id` — both of which `notification_subscription` already logs, so this adds no new class of identifier |
| `notification_alert_state` | a failed alert-state read behind a subscription response | `result` (`error`) |
| `webhook_delivery_refused` | an outbound refusal | `reason` |
| `webhook_delivery_response_truncated` | a 2xx body over the response cap | `limit` |
| `notification_auth_config` / `webhook_signing_config` | once per process: a secret arrived with whitespace and was stripped | `variable` — the **name only**, never the value |
| `notification_wording` | a webhook wording fault | `result` (`error`, `rate_limited`), `surface` |
| `notification_pull` | the journey-keyed public read, ONE record per request (`GET /notification-api/v1/journey-alerts`, 2026-09-10) | `result` (`ok`, `not_loaded`, `matcher_failed` — a WARNING whose message names the exception's TYPE only, `rate_limited`, and since U4 `enrich_failed` — the same WARNING shape, for the background enrichment task's matcher run or the task itself), `cache` (`hit`/`miss`), `n_alerts` (what the answer lists, before `limit`), `wait_s`, `ms`, `pair_digest` (`alternatives.pair_digest`, the salted per-process digest of the canonical stop pair). **Nothing else**: no ref, no name, no departure time, no prose, and a 422 logs nothing at all. The process's HTTP access log redacts this path's query string too (`logging_setup._AccessLogRedaction`), because the refs ride in it |
| `notification_pull_bounded` | a bound binding on the read's enrichment (U4) | `reason` (`queue_full` — the task was refused and every listed entry marked `capped`; `wordings_per_read`; `alternative_lookups`; `wording_allowance` — the PULL allowance answered `rate_limited`; `waiters` — no waiter slot, the read behaved as `wait=0`), `n` (entries the bound touched), `pair_digest`. Nothing identifying. The read's own record also carries `result="enrich_failed"` at WARNING when the task's matcher run raised — the exception's TYPE in the message, never its text |
| `poller_phase` | the cycle breakdown | `phase="notification_webhooks"` or `"notification_servability"`, `ms`, `result` |

**What no `notification_*` or `webhook_*` log line carries:** an origin, a destination, a departure time, a full callback
URL, a credential secret, or any notification prose. Read the qualifier — it is about these events,
and the fourth entry below is why it needs one.

**What some do carry, named rather than glossed** — this is the honest half of the retention claim:

- **`subscription_id` is `<client_id>~<the client's own reference>`**, so a client-chosen reference
  reaches `notification_subscription` and `notification_cleanup`, and is hashed into `alert_id` on
  `notification_event`. `external_ref` — the client's handle on a *traveller* — reaches no log line.
- **`webhook_delivery_refused.reason` can carry a callback destination's hostname and its resolved IP
  addresses** (`'host' resolves to N address(es) that are not globally routable unicast: ...`). Not a
  path or query, but not nothing.
- **`notification_admin.actor` identifies a named human** — the operator's ADC identity, usually an email
  address, falling back to `$USER` and then to `"unknown"`.
- **A webhook subscription's origin and destination DO reach Cloud Logging — under `/varsel`'s event
  name, not a client one.** `main._varsel_connectivity` writes both stop refs into the *message* of
  an INFO record (`event="varsel_service_check"`, `result="ok"`) on every established verdict. Its
  structured `fields` carry only `event`, `result`, `forward`, `reverse` and — since the budget was
  split per caller — `path` (`request` or `notification_api`), which is why the claim
  above stays literally true — and a reader who stopped there would be misled, which this document's
  posture does not allow. The webhook channel reaches it at **create time** (it always did) and now
  again **every six hours per unservable subscription**, through `notification_servability`. It is
  **pre-existing and shared with `/varsel`'s own registrations**, so the honest framing is an
  accepted exposure this feature extends rather than a new leak; and if it is ever to be closed, the
  fix is `/varsel`'s log line, not this channel's. **The public read deliberately never reaches it**:
  no servability question is asked for an anonymous pair, which is one reason the read has a
  `coverage` block instead of a caveat.
- **The public read's own three accepted exposures, published beside this one rather than closed**
  (plan 2026-09-10-001). (1) **The completeness and latency of a FIRST read reveal whether a
  registration with exactly those five fields exists and is alert-worthy now**: a registered
  definition the pass has worded answers complete at once, a cold one waits or answers `pending`.
  The mitigation is that no status ever says so, and that a definition is a stop pair with a
  period, not a person. (2) **The global ceiling and the wording and Journey Planner allowances are
  shared by every anonymous caller**, the app's BFF included, with no reserved slice — one source
  at `MAX_PER_MIN` holds every caller at 429, one at `WORDINGS_PER_DAY` holds every caller
  template-only for the trailing day; the client contract tells the BFF to treat both as ordinary
  states. (3) **The definition rides in the query string.** The process redacts it from its own
  access log (`logging_setup._AccessLogRedaction`) and no `notification_pull*` record carries a
  ref, a name, a departure time or prose — only `pair_digest`, the salted per-process digest
  `alternatives` already logs — but any log outside the process (the ingress, a proxy, the caller's
  own client) may keep the request line, and nothing in this repository governs those.

Log retention and access are the cluster project's, not this repo's: nothing here configures a log
bucket or a sink, so what applies is whatever `ent-kub-{dev,prd}` sets. **Verify it against the
project rather than assuming the 30-day `_Default`** before quoting a number to anybody.

**There are no metrics on this path.** Everything an operator reads is a structured log record.

**What to watch on the public read** (`GET /journey-alerts`), since its bounds are shared by every
anonymous caller and bind silently as per-entry statuses rather than as errors: the **rate of
`notification_pull_bounded`** by `reason` — a steady `wording_allowance` means the pull wording
allowance is spent and every caller is getting the template, a steady `queue_full` means the
enrichment pool is saturated, `waiters` that a burst is parking more readers than
`MAX_CONCURRENT_WAITERS` — and **`notification_wording.spent("pull")` against the hour**, the
`(hour, day)` pair of wording calls charged to the pull path, read against `WORDINGS_PER_HOUR` (500)
and `WORDINGS_PER_DAY` (2 000). Both are the read's own ledgers: the poller's and the webhook
channel's are `spent("browser")` and `spent("webhook")`, and a read burst that moves either of those
is a bug. `notification_pull` with `result="rate_limited"` is the limiter itself binding; with
`cache="miss"` at a high rate it is the figures cost, which is the peak measurement the plan's
verification contract asks for before the panel goes live.

---

## Delivery and the cycle budget

The webhook work is one `main._rebuild_all` phase, `notification_webhooks`, entered only when the channel
flag is on and placed **after** `varsel.check_and_notify` has *returned* — so a raise inside it cannot
unwind the decision-log write or the state-blob save at all, rather than merely being sequenced after
them. Emit and deliver are guarded separately: an emit failure must not cost deliveries already
committed, and delivery runs even when the pass above raised, because the pending set is Firestore's
rather than this cycle's.

Every bound is a number:

| bound | value |
|---|---|
| attempts per pending event per cycle | 1 — the next cycle is the retry |
| delivery pool width | 4 (`/meldinger`'s width, the repo's narrowest) |
| per attempt | 5 s total, 2 s connect |
| delivery step deadline / bounded wait | 20 s / 20 s |
| emit step deadline / bounded wait | 6 s / 8 s |
| per Firestore call | 4 s, under `varsel`'s 5 s wait |
| attempts per cycle, total / per destination | 200 / 50 |

A destination is **skipped for the rest of the cycle after its first failure**, in pass-local state.
That is the documented exception to this repo's "every module-level ledger is swept" rule: made
module-level and swept, one failure would pause a destination permanently, which is
indistinguishable from the intended 410 pause.

**Worst cases under injected faults.** Two of these are *ceilings* — arithmetic over the constants
above, and pinned as such by a test — and two are *timings* recorded when the step was built. The
table says which, because "measured 28 s" and "the two waits sum to 28 s" are different claims and
only the second is checkable.

| injected fault | figure | kind |
|---|---|---|
| a hanging destination, 30 pending on one subscription | ~5.0 s | timing — one attempt at the 5 s per-attempt ceiling. Since claims were sharded per subscription (`7072268`) all 30 are one shard, so the first failure sets the skip and the rest never dial. The earlier figure of four attempts and 26 skipped is from before that change |
| a destination hanging forever | 20.0 s | **ceiling** — `webhook_delivery._DELIVERY_WAIT_S`, the outer bounded wait |
| a hung store | **28.0 s** | **ceiling** — `_EMIT_WAIT_S` (8) + `_DELIVERY_WAIT_S` (20), pinned under 30 s by `test_notification_events.py::test_the_two_ceilings_leave_the_phase_under_ktd6s_reversal_trigger` |
| a healthy backlog of 200 pending | 7 ms | timing, recorded in `e70d291` |
| emit of 200 subjects | 14 ms | timing, re-recorded in `7072268` after the sharding change |

Against the cycle period measured at **~296 s** (2026-09-08) the worst case is ~9.5 % of one cycle.
`notification_webhooks` is now the cycle's **last** phase — `check_and_notify` was, and this step is what
displaced it — so its tail delays nothing else in the rebuild.

**KTD6's reversal trigger is a number, not a judgement. Move delivery out of the pass when any one of
these is true:**

- `poller_phase{phase="notification_webhooks"}` exceeds **30 s** on any cycle in a week;
- a **second replica** appears (the single-writer argument the emit path's exactness rests on is one
  poller thread, no route invoking the pass, one replica under the Recreate strategy);
- **clients reach single digits.**

The 28 s ceiling is 2 s under that trigger and is pinned by a test rather than argued. Re-measure
before raising any ceiling; the numbers above are measurements from 2026-08-21 and 2026-09-08, not
constants.

**A slow client is the thing to watch, and its failure mode does not look like one.** A destination
whose cold start exceeds the 5 s per-attempt ceiling is **never delivered to at all**, not merely a
cycle late: the first failure skips it for the rest of the cycle, there is one attempt per cycle, the
next cycle repeats the cold start, and every event ends `expired` — which reads like ordinary expiry.
`minInstances: 1` on the receiver is the client-side fix. Measuring that latency is checklist item 3.

**What to measure during a pilot:** evaluation-to-callback latency; **source age reported separately
from delivery latency**, so a fast callback cannot disguise an old prediction; the webhook phase's
share of the cycle against the 30 s trigger; the oldest pending event; per-destination outcomes;
suppression reasons; quota breaches; request rejections. **Do not count a 2xx from the BFF as a
notification on a phone.**

---

## Scaling beyond the pilot

The contract and the store's document model carry a hundred thousand commuters as they are: the
event grain (`alert_id` per subscription and train), the revision rules, the signing scheme and the
eight collections need no change, and the nullable `servable` already tolerates an asynchronous
answer. What does **not** carry that far is the runtime topology, and it is pilot-grade on purpose:
every ceiling above is a constant sized for one poller thread and a few hundred subscriptions. These
are the moves, in the order they will bind, each behind a seam that already exists. None of them
touches the wire contract, which is why the pilot could ship without them.

1. **The matcher, once per place pair rather than once per definition.** `impact._trip_cells` scans
   every scored call of every situation per journey definition at ~40 ms each; ten thousand
   definitions is minutes per cycle, for both channels. Evaluate each (from, to) pair once against
   the situations, then apply the definitions' windows. Outside this channel's code and the first
   thing that binds — a single client at 500 subscriptions already costs ~20 s of matcher per cycle.
2. **A mirror instead of a full read.** `varsel._load_webhook_recipients` reads every active
   subscription every cycle through the provider callable, capped at `MAX_SUBSCRIPTIONS_PER_CYCLE`
   (2 000) because a full read is what it is. Past that the provider becomes an in-memory mirror
   synced on an `updated_at` watermark (or a listener), with a full read only at startup. The seam is
   the callable; `varsel` does not change.
3. **Emit and delivery as their own Deployment.** Both run on the poller thread today, bounded at
   `_MAX_EMITS_PER_CYCLE` (120) and `MAX_DELIVERY_ATTEMPTS_PER_CYCLE` (200) so a bad morning cannot
   stall the cycle. At scale a bad morning is tens of thousands of events an hour, which one thread
   cannot deliver at 5 s per attempt. `deliveries` is already a queue with transactional claims and a
   `RESERVATION_TTL_S` lease, so a second Deployment can drain it safely; the one thing that changes
   meaning is the pending-event ceiling, which is exact only because emit has one writer and becomes
   best-effort with two.
4. **Servability for every pending pair in one query per cycle.** Today a create asks BigQuery
   inline under a 15/min budget and `notification_servability` re-asks four subscriptions a cycle
   (~1 150 a day). Distinct stop pairs number in the thousands where subscriptions number in the
   hundreds of thousands, and the planned tables already answer all of them in one query at cents.
5. **A `count()` aggregation for the active-subscription ceiling.** `_create_body` counts a client's
   live subscriptions by reading them inside the create transaction, up to the ceiling, which is why
   `QUOTA_BOUNDS` caps `max_active_subscriptions` at 10 000. Above that the count has to be an
   aggregation over an `expires_at` inequality (a new composite index), so the quota a very large
   client needs is unlocked by this item, not by `set-quotas` alone.

Two costs that arrive with larger caps rather than with volume: `notification_events._standing`'s
client-wide pre-read enumerates at most `MAX_SUBJECTS_PER_READ` (500) subjects and falls back to one
transaction per subject when it is incomplete, which a client with many subscriptions and a high
`daily_cap` will hit more often (correct, slower); and the model wording is written per journey
definition under a per-cycle budget, so at scale most events carry the template unless the wording
is re-keyed on (place pair, train). Neither changes what a client receives, only how much prose and
how fast.

Two things that do NOT need to change for a hundred thousand commuters: the one-subscription-per-
commuter grain (a client that prefers one subscription per route can already use the API that way,
with `daily_cap` raised and its own per-user caps, and the receiver's dispatch hook already fans one
event out to many recipients), and the single API replica, whose request load at that scale is tens
of requests a second once the matcher is indexed.

---

## Retention and deletion, clause by clause (R19)

Every published clause below is stated beside the function that makes it true. Where the code did not
support the clause, the clause was changed.

| # | Published clause | Code that makes it true |
|---|---|---|
| 1 | Events, deliveries and alert subjects are kept **7 days** | `notification_store.HISTORY_DAYS = 7`; `_emit_body` writes `expires_at = now + 7 days` on the event, the delivery and the subject in one transaction |
| 2 | A subscription expires at the **earlier of 180 days** from creation **and 60 days** from the last client interaction | `SUBSCRIPTION_MAX_LIFETIME_DAYS = 180`, `SUBSCRIPTION_INACTIVITY_DAYS = 60`; `_horizons()` stores `lifetime_expires_at`, `inactivity_expires_at` and `expires_at = min(...)`; `touch_subscription` pushes the inactivity horizon out and reads first so it cannot resurrect a tombstone |
| 3 | Both horizons are **enforced on read**, not by TTL alone | `subscription_is_live()` tests `status == "active"` and **both** named horizons, not `expires_at` alone — that field is only the minimum, written by whichever binary created the row |
| 4 | Every collection's expiry is **tested on read**, because an expired document stays queryable until TTL deletes it | `_not_expired()`, called on every read path in all four collections. A **missing** `expires_at` reads as expired — the direction a retention promise has to fail |
| 5 | The **maximum residual is ~24 h** where TTL is the fallback for a document whose own expiry has passed | `TTL_RESIDUAL_HOURS = 24`, from Firestore's documented "typically deleted within 24 hours after expiration"; served on the DELETE response as `cleanup.residual_hours` |
| 6 | **Where cleanup has not completed, the full residual is up to about 8 days** — the document's own 7-day retention plus that lag | `HISTORY_DAYS` + `TTL_RESIDUAL_HOURS`. The tombstone is on the same 7-day window (`_delete_body` sets `expires_at = now + HISTORY_DAYS`) and keeps route, window, weekdays and `external_ref` until cleanup redacts them, so **8 days is the number to publish**, not 24 hours |
| 7 | Deletion is **ordered**: tombstone first, in one transactional write that stops routing | `_delete_body` — sets `status="deleted"`, both horizons to `now`, and `cleanup: {state: "pending"}`. Routing stops because the load query filters `status == "active"` (`QUERY_SPECS`, `load_active_subscriptions`). Reversed, a partial failure leaves a routable subscription whose events are gone |
| 8 | Deletion is **idempotent in both directions** | `_delete_body` returns `None` for an unknown id and the stored record with **no write** for an already-tombstoned one, so a retried DELETE cannot bump a version forever |
| 9 | Cleanup is **retryable with an auditable completion state**, batched | `cleanup_subscription()` — three bounded queries, `client.batch()` deletes, then `_finish_cleanup_body` writes `cleanup.state`, `.attempts`, `.last_attempt_at`, the three deleted counts and `.completed_at`. `done=False` when any page came back full |
| 10 | **A repeated DELETE resumes an incomplete cleanup; there is no background job that will finish it** | `notification_api.delete_subscription` runs at most `_CLEANUP_PASSES = 3` × `_CLEANUP_BATCH = 200` per request and returns `cleanup.state: "pending"` when it did not finish. Nothing else in the repo calls `cleanup_subscription`. A cleanup exception is logged and **never fails the DELETE** — the tombstone is already committed, so it is a retention question and not a routing one |
| 11 | The tombstone's **route, window, weekdays and client reference are redacted the moment cleanup completes** | `_finish_cleanup_body(done=True)` blanks `from_stop`, `to_stop`, `begin`, `end`, `days`, `external_ref`, `definition_digest`, `delivered`, `daily_deliveries`, `daily_reservations` and stamps `redacted_at`. Redaction rather than deletion, because the residual record is what keeps a repeated DELETE idempotent and the completion auditable |
| 12 | Deletion or expiry **stops later claims**, and a request in flight may complete | `_claim_body` re-reads the **live** subscription immediately before dispatch and cancels the delivery on `missing`, `deleted`, `expired` or a changed `destination_id`. Deliberately **not** on a version change: a version compare there would cancel a still-pending alert on every amendment |
| 13 | Events, deliveries and subjects are **top-level collections carrying a subscription id**, not subcollections | `COLLECTIONS` and `_Refs`. A document delete does not remove subcollections and a TTL field does not cascade into them |
| 14 | A delivered dedup mark lives **48 h** | `DELIVERED_MARK_TTL_H = 48`, pruned by `_prune_marks`. A bound rather than an exact rule, and generous on purpose: a mark that expires *early* re-notifies a train the traveller has already been told about |
| 15 | The **operator audit is append-only and never expires** | `notification_auth.ADMIN_AUDIT`; the `_Docs` seam has **no `delete`**, and no TTL field config exists for that collection in `terraform/firestore.tf`. Deleting a subscription never touches it. Its `reason` can hold a refused destination's host |
| 16 | The **7-day event retention is shorter than the 10-day outcome-labelling window**, so retrospectively scoring client alerts is foreclosed | `HISTORY_DAYS = 7` against the nightly job's retry window. Published, so lengthening it later is a contract change |

### Published numbers, and where they come from

Every number this feature publishes, beside the constant that defines it.
`disruptions/api/test_notification_contract.py` parses **this table** and compares each row against the
module, so a constant that moves without this document moving fails the build — which is what turns
R19's clause-by-clause check from a one-off reading into something that keeps holding.

Three of these are **defaults** rather than ceilings since 2026-09-09: `MAX_ALERT_DELIVERIES_PER_DAY`,
`MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT` and `PER_CLIENT_PER_MIN` seed a new client's own values
(`notification_auth.Quotas`, stored on the `clients` document, changed with `set-quotas`, bounded by
`notification_auth.QUOTA_BOUNDS` at 1 000 / 10 000 / 10 000). The table states the default; a client's
effective values are on its account. The daily cap is also copied onto each subscription document
(`daily_cap`) at create, replace and touch, which is where `cap_remaining` reads it.

| constant | value |
|---|---|
| `notification_store.HISTORY_DAYS` | 7 |
| `notification_store.SUBSCRIPTION_MAX_LIFETIME_DAYS` | 180 |
| `notification_store.SUBSCRIPTION_INACTIVITY_DAYS` | 60 |
| `notification_store.EVENT_VALIDITY_MINUTES` | 60 |
| `notification_store.TTL_RESIDUAL_HOURS` | 24 |
| `notification_store.DELIVERED_MARK_TTL_H` | 48 |
| `notification_store.MAX_ALERT_DELIVERIES_PER_DAY` | 4 |
| `notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT` | 500 |
| `notification_store.MAX_SUBSCRIPTIONS_PER_CYCLE` | 2000 |
| `notification_store.MAX_PENDING_EVENTS_PER_DESTINATION` | 500 |
| `notification_store.MAX_DELIVERY_ATTEMPTS_PER_CYCLE` | 200 |
| `notification_store.MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE` | 50 |
| `notification_store.MAX_DELIVERY_SCAN_PER_CYCLE` | 1000 |
| `notification_events.DEPARTURE_GRACE_S` | 120 |
| `notification_store.MAX_ALERTS_SERVED` | 4 |
| `notification_store.MAX_SUBJECTS_PER_READ` | 500 |
| `notification_store.WITHDRAWAL_CYCLES` | 2 |
| `notification_store.WITHDRAWAL_GAP_S` | 900 |
| `notification_store.RETRY_AFTER_CAP_S` | 300 |
| `notification_store.RESERVATION_TTL_S` | 120.0 |
| `notification_auth.MAX_CREDENTIALS_PER_CLIENT` | 8 |
| `notification_auth.MAX_DESTINATIONS_PER_CLIENT` | 4 |
| `notification_auth.MIN_PEPPER_BYTES` | 32 |
| `notification_auth.GLOBAL_PRELOOKUP_PER_MIN` | 300 |
| `notification_auth.PER_CLIENT_PER_MIN` | 120 |
| `notification_auth.RETRY_AFTER_S` | 60 |
| `webhook_signing.MIN_ROOT_BYTES` | 32 |
| `webhook_signing.TOLERANCE_S` | 300 |
| `webhook_sender.DEFAULT_TIMEOUT_S` | 5.0 |
| `webhook_sender.CONNECT_TIMEOUT_S` | 2.0 |
| `webhook_sender.MAX_RESPONSE_BYTES` | 65536 |
| `notification_api._LIST_DEFAULT` | 100 |
| `notification_api._LIST_MAX` | 500 |
| `notification_api._TEST_EVENTS_PER_MIN` | 6 |
| `notification_api._CLEANUP_PASSES` | 3 |
| `notification_api._CLEANUP_BATCH` | 200 |
| `notification_events._EMIT_DEADLINE_S` | 6.0 |
| `notification_events._EMIT_WAIT_S` | 8.0 |
| `notification_events._MAX_EMITS_PER_CYCLE` | 120 |
| `webhook_delivery._DELIVERY_DEADLINE_S` | 20.0 |
| `webhook_delivery._DELIVERY_WAIT_S` | 20.0 |
| `webhook_delivery._WORKERS` | 4 |
| `notification_pull.MAX_PER_MIN` | 300 |
| `notification_pull.PER_PAIR_PER_MIN` | 30 |
| `notification_pull.LIMITER_KEYS_MAX` | 10000 |
| `notification_pull.MAX_WAIT_S` | 20 |
| `notification_pull.DEFAULT_LIMIT` | 10 |
| `notification_pull.MAX_LIMIT` | 20 |
| `notification_pull.FIGURES_CACHE_MAX` | 512 |
| `notification_pull.MAX_WORDINGS_PER_READ` | 4 |
| `notification_pull.WORKERS` | 4 |
| `notification_pull.QUEUE_MAX` | 16 |
| `notification_pull.WORDING_WORKERS` | 4 |
| `notification_pull.MAX_CONCURRENT_WAITERS` | 64 |
| `notification_pull.WORDING_CACHE_MAX` | 10000 |
| `notification_pull.WORDING_TTL_S` | 43200 |
| `notification_pull.FAILURE_TTL_S` | 60 |
| `notification_wording.WORDINGS_PER_MIN` | 30 |
| `notification_wording.WORDINGS_PER_HOUR` | 500 |
| `notification_wording.WORDINGS_PER_DAY` | 2000 |
| `alternatives._PULL_FETCH_MAX_PER_MIN` | 30 |

**The journey-keyed pull read** (plan 2026-09-10-001): the `notification_pull.*` rows are the public
read's own numbers. The first seven are the read without enrichment (U3) — its limiter (a global
per-minute ceiling and a per-stop-pair one in a map bounded at `LIMITER_KEYS_MAX`, independent of the
credentialed tiers above), its `wait` and `limit` bounds, and the per-cycle figures cache. The next
eight are the enrichment (U4): the wordings one read may spend (`MAX_WORDINGS_PER_READ`, soonest
departure first; the rest carry the template under `not_worded` — a verdict derived from the
entry's position in the read's soonest-first order and never cached, so it is re-evaluated on
every read as the trains ahead depart or a narrower definition puts the train under the cap), the
background pool (`WORKERS`, a queue of `QUEUE_MAX` bounded by the module itself — past
`WORKERS + QUEUE_MAX` unfinished tasks a new definition's read is answered `capped` at once, with its
figures — and `WORDING_WORKERS` inside one task), the requests that may be parked on a task at once (`MAX_CONCURRENT_WAITERS`; without a slot a
read behaves as `wait=0`, never a 503), and the two cache lifetimes: a wording lives `WORDING_TTL_S`
(the situation's own 12 h outer bound, and shorter when the figures refresh no longer lists its
train) in a map of `WORDING_CACHE_MAX`, an alternative outcome lives `alternatives._TTL`, and a
transient outcome (`pending` aside: `error`, `rate_limited`, `unanswered`, a queue-full `capped`) is
a `FAILURE_TTL_S` marker resubmitted on expiry. The four rows after them are the allowances the read
spends and nothing else does (R4): its wording ceilings per minute, hour and day
(`notification_wording.WORDINGS_PER_*`, charged to the `pull` path under the `PULL` policy, with its
own latch holder) and its Journey Planner budget (`alternatives._PULL_FETCH_MAX_PER_MIN`). The
published contract (U5, 2026-09-10) added no row: every number the read publishes was already here,
and the client contract's rate-limit table quotes these.

Two numbers on the receiver's side are TypeScript's and are pinned against
`integrations/notifications-client/src/client.ts` by the same suite: the **64 KiB** inbound ceiling
(`DEFAULT_MAX_BODY_BYTES`) and the **15-minute** processing cutoff (`DEFAULT_PROCESSING_CUTOFF_MS`).

One more figure is a measurement rather than a constant and is therefore **not** in that table: the
pass period, ~296 s on 2026-09-08. It carries its date wherever it is quoted, because re-measuring is
what changing it means — and two constants above are sized against it (`WITHDRAWAL_GAP_S` at ~3
periods, `RESERVATION_TTL_S` comfortably under one), so they are one decision with it.

### The two things deletion does not reach

Both confirmed against the code, both published rather than implied.

**1. The in-process wording caches.** `varsel._messages` and `varsel._message_meta` (and
`varsel._alternatives` beside them) are keyed by what `varsel._message_key(key, alert)` returns — the
tuple (journey definition, situation number, departure). The first component is the **journey
definition**, which under KTD3 is exactly what a webhook subscription
shares with a browser registration on the same route. Removing one subscription's wording would remove
the other's, so cleanup deliberately does not try.

- **Residual window:** an entry is dropped by `varsel._reconcile_caches` → `_forget_message` on the
  first pass where the subject stops being alert-worthy, and the outer bound is the situation's own
  life — up to **12 h** from its onset. They are in-memory only: a redeploy clears them.
- **What they hold:** prose and its provenance. No client id, no callback URL, no `external_ref`.
- `notification_wording` holds no journey-keyed cache at all — its module state is a call-count budget
  of timestamps plus per-surface latch holders — so this claim is about `varsel` alone.
- `notification_pull` (U4) holds a SECOND in-process prose cache, keyed by canonical stop pair and
  train rather than by journey definition: a wording for up to `WORDING_TTL_S` (12 h, or until the
  figures refresh no longer lists its train) and an alternative outcome for `alternatives._TTL`.
  Written for ad-hoc definitions nobody registered as much as for registered ones, it carries no
  client id, no subscription id and no callback, and a subscription's deletion has nothing in it to
  reach — the same residual window as `varsel`'s, in memory only, cleared by a redeploy.

**2. Client and event identifiers in cluster logs.** They live in `ent-kub-{dev,prd}`, namespace
`kix-avvik`, under that project's retention and access policy and not this repo's — nothing here
configures a bucket or a sink. The exact fields are the table in [What the logs say](#what-the-logs-say);
the three that matter are `subscription_id` (which embeds the client's own reference), a refused
destination's hostname with its resolved addresses, and an operator's email address.

### R16, and the half of it that is not what it looks like

R16 keeps client records, credentials, callback URLs and generated prose out of `/historikk`,
`/meldinger`, `/rushhistorikk`, routine logs **and the LLM prompt audit record**. The last of those has
**two halves and only one is closed**, which is published here rather than asserted away:

- **A webhook-only route** — one no browser registration holds — is written for through
  `notification_wording.WEBHOOK`, selected in `varsel.check_and_notify` on the discriminator `key not
  in by_journey_key` (`by_journey_key` is browser-only by construction, per KTD10). That policy
  **suppresses the prompt/reply DEBUG record in code**, regardless of `VARSEL_LLM_LOG_PROMPTS`, and
  drops `message` from the INFO record. This is the half R16 requires and it is closed.
- **A route a browser registration also holds** stays on the browser policy, unavoidably and by
  design: AE1 pins **one match and one wording** between the two channels, so there is one call and it
  is the browser's. That call's `llm_message` record carries the traveller's origin, destination and
  departure time at DEBUG wherever `VARSEL_LLM_LOG_PROMPTS` is on — **which dev is** — and it would
  exist with no webhook channel at all, because a browser registration holds the same route.

**So R16's claim is about webhook-only routes plus every other client field. It is not "no client
route ever appears in the prompt log."**

**Accepted as it stands on 2026-09-09.** The alternative put forward was dropping R16 altogether once
`/historikk` and `/meldinger` are behind authentication. That retires the dashboard half of R16 and
not this one: the prompt/reply record lives in **Cloud Logging**, which project IAM gates and page
authentication never touches. And the code that closes the webhook-only half is one field —
`caller` on `notification_wording.WEBHOOK` — feeding a gate `llm_message` already has for
`/meldinger`'s replay, so removing it would be a guard removed rather than complexity saved.

---

## Acceptance evidence (AE1–AE18)

The Definition of Done asks for test or device evidence per acceptance example. Paths are relative to
the repository root; Python suites are `disruptions/api/`, TypeScript ones `integrations/`.

| AE | Evidence | Complete? |
|---|---|---|
| AE1 | `test_varsel_api.py::test_a_browser_and_a_webhook_registration_share_one_match_and_one_wording` (**asserted by call count** — one `/impact` call, one Vertex call), `::test_a_webhook_subject_carries_byte_identical_wording_to_the_web_push`, `test_notification_store.py::test_the_window_rules_are_varsels_own` | yes |
| AE2 | `test_varsel_api.py::test_two_situations_about_one_train_yield_one_webhook_subject_carrying_both`, `test_notification_events.py::test_an_alertworthy_train_mints_one_event_and_the_next_cycle_mints_nothing`; the "same winner every cycle" half is `/varsel`'s own pre-existing `test_varsel.py::test_the_survivor_of_a_tie_does_not_depend_on_input_order` | yes |
| AE3 | `test_notification_events.py::test_thirteen_departures_produce_thirteen_events_soonest_first`; `test_webhook_delivery.py::test_thirteen_pending_events_yield_four_notifications_and_nine_recorded_suppressions` (`sent == 4`, `capped == 9`) | yes |
| AE4 | `test_notification_events.py::test_a_confirmed_cancellation_mints_an_updated_revision_and_supersedes_the_forecast` and `::test_a_confirmation_whose_forecast_was_never_delivered_is_not_cap_exempt`; `test_webhook_delivery.py::test_a_cap_exempt_confirmation_still_reaches_a_subscription_whose_four_are_spent`; silent-by-default in `notifications-client/test/policy.test.mjs` | yes |
| AE5 | `notifications-client/test/receive.test.mjs` (one inbox record, concurrently and on redelivery), `test/process.test.mjs` (one native decision after a restart), and the same two through the real emulator in `firebase-notifications/test/emulator/store.test.mjs` | yes |
| AE6 | `test_webhook_delivery.py::test_a_503_stays_pending_and_a_deletion_then_cancels_it`; `test_notification_store.py::test_a_delivery_whose_subscription_expired_is_cancelled_at_dispatch` | **partial** — "other destinations unaffected" is carried by `test_webhook_delivery.py::test_a_4xx_other_than_410_is_terminal_for_that_event_only` and `report.skipped_destinations == 1` in the hanging-destination test, not by one test running two destinations through one pass |
| AE7 | `test_webhook_delivery.py::test_a_hanging_destination_is_skipped_and_the_pass_ends_inside_its_deadline` (**measured**: with production constants, four attempts and 26 skipped, 5.0 s) and `::test_the_deadline_binds_before_the_backlog_does`; pool width pinned by `::test_the_pool_is_four_wide_and_the_claim_happens_inside_the_worker`; poller containment by `test_poller_refresh.py::test_the_webhook_phase_is_gated_guarded_twice_and_fed_by_a_return_value` | yes |
| AE8 | `test_webhook_signing.py::test_two_attempts_share_the_webhook_id_and_differ_in_timestamp_and_signature` and `::test_a_stale_timestamp_is_refused_for_the_timestamp_and_not_for_the_mac`; receiver side in `notifications-client/test/signingVectors.test.mjs` | yes |
| AE9 | `test_webhook_sender.py` — the named ranges, NAT64, 6to4, multicast, both families, connect-time refusal, one resolution, the validated literal with the original SNI; provisioning gate and audit in `test_notification_auth.py::test_an_unsafe_callback_url_is_refused_at_provisioning_and_audited` | yes |
| AE10 | `test_varsel_api.py::test_a_webhook_subscription_matches_and_is_written_for_without_a_keypair` and the two budget tests beside it; `test_notification_wording.py::test_a_webhook_wording_needs_no_vapid_keypair_and_no_model`; the delivery half is structural and is now pinned by `test_notification_contract.py::test_the_store_and_delivery_modules_import_no_poller_state` | yes |
| AE11 | `test_notification_api.py::test_a_missing_or_wrong_credential_reads_nothing` and `::test_one_client_cannot_reach_anothers_records_and_the_body_names_nothing`; `test_notification_store.py` tenant isolation; `test_llm_message.py::test_a_webhook_call_keeps_prose_and_the_prompt_out_of_the_log`; `test_varsel_api.py::test_a_webhook_only_route_records_no_decision_row`; and whole-tree in `test_notification_contract.py` (no client field in any `data/queries` SQL, any front-end bundle, the decision-log DDL or any structured log field). **Shape note:** the `/historikk` and `/meldinger` half is a grep over the uncommented SQL — the repo's idiom, structurally stronger than a response assertion, but not a rendered-page check | yes |
| AE12 | `test_webhook_delivery.py::test_a_departed_train_becomes_expired_rather_than_failed_and_is_not_retried`; `test_notification_store.py::test_event_validity_is_the_earliest_of_three_things` | yes |
| AE13 | **Stored with the caveat:** `test_notification_api.py::test_an_unservable_pair_is_stored_and_carries_the_caveat`, `::test_a_provider_timeout_stores_no_caveat_rather_than_a_false_one`, `::test_a_changed_route_retires_the_old_servability_answer`. **Re-checked as service appears:** `test_notification_servability.py` (27 checks) — `::test_service_that_appeared_clears_the_caveat`, `::test_a_never_established_answer_is_due`, `::test_an_unchanged_answer_still_moves_the_stamp`, `::test_a_verdict_that_could_not_be_established_writes_nothing_at_all`, `::test_the_interval_is_varsels_own`; wired and gated in `test_poller_refresh.py` | yes |
| AE14 | `test_varsel_api.py::test_a_broken_notification_store_costs_the_webhook_channel_and_nothing_else` (raises, hangs and one unreadable document, in one test, asserting browser matching, wording, delivery, decision recording **and** state persistence all complete); `test_webhook_delivery.py::test_a_slow_store_cannot_extend_the_pass_past_its_bounded_wait`; `test_notification_events.py::test_a_hung_store_cannot_extend_the_step_past_its_bounded_wait` | yes |
| AE15 | `test_webhook_delivery.py::test_a_restart_between_commit_and_delivery_resends_the_original_id_and_revision`; `test_notification_store.py::test_only_a_lifecycle_transition_mints_a_revision` and `::test_a_changed_payload_mints_nothing`; the receiver's suppression in `notifications-client/test/process.test.mjs` | yes |
| AE16 | `notifications-client/test/policy.test.mjs`, `test/process.test.mjs`, `test/genericReceiver.test.mjs` (over a socket) and `firebase-notifications/test/emulator/store.test.mjs` (through the transaction) | yes |
| AE17 | `test_notification_store.py::test_the_daily_cap_is_spent_on_success_and_a_replacement_is_exempt` and `::test_the_cap_is_keyed_on_the_oslo_day_not_utc` (the counter is on the subscription document); receiver-side per-subscription ledger state in `notifications-client/test/policy.test.mjs` and `firebase-notifications/test/emulator/store.test.mjs` | **partial** — the grain is pinned structurally, but no sender-side test runs two disrupted subscriptions and asserts 4 + 4 in one Oslo day, and the TypeScript tests labelled AE17 prove per-subscription **ledger** state, which is a different claim |
| AE18 | `test_notification_api.py::test_a_synthetic_event_is_refused_for_a_live_subscription`, `::test_a_replace_cannot_clear_or_set_the_test_marker`, `::test_a_test_event_is_signed_over_the_exact_bytes_and_stores_nothing`; the receiver refuses before native dispatch in `notifications-client/test/process.test.mjs`, and end to end in `firebase-notifications/test/endToEnd.test.mjs` | yes |

**No AE is at zero coverage**, and none rests on a deferred manual check. Three rows remain partial,
each with a named uncovered clause, and all three are test gaps rather than behaviour gaps — AE13's
was the one behaviour gap and its second clause is now built.

Two things no test can establish and that stay on the pre-flight checklist above: the **pilot
destination's cold-start latency** against the 5 s per-attempt ceiling, and a **test-phone
notification** for a predicted and a confirmed event on the client's own runtime.

## Open questions and product decisions

Named here because they are decisions somebody has to make, not documentation gaps. **Six were put
to the product owner with the pull request and answered on 2026-09-09.** What was decided stands
beside each; the ones still open say who owns them.

1. **Who is the controller for client-path data, and is a processing agreement needed?** The client
   owns consent, which makes us a processor for the same origin/destination/window trio `/varsel`
   holds for its own users — and `/varsel`'s privacy texts name Entur AS because that registration is
   ours. No agreement, legal basis or published notice for the notification API path exists. **Open (2026-09-09):
   project management's to settle with the DPO, not the team's; the clients know the channel is
   experimental. It gates real-user enablement rather than implementation.**
2. **The daily cap's exemption is wider than the requirement's wording.** R11 exempts "a confirmation
   that replaces an earlier forecast about the same train". The store exempts **any replacement of a
   revision already delivered about that train**, which includes a **withdrawal**. The reasoning is
   that a retraction the cap suppressed leaves the traveller holding a warning we have withdrawn —
   Rushvarsel's friskmelding argument. **Decided 2026-09-09: the wider rule stands.**
3. **`NOTIFICATION_WEBHOOK_LLM_ENABLED` was in no chart until 2026-09-10.** Whether client prose is the model's is a
   product decision, and the name lands in Helm when that decision is made. **Partly overtaken
   (2026-09-10)**: dev sets it for the public read, which follows the same switch — see section 4
   — and prd followed later that day on the product owner's call; the webhook channel is off in
   both, so no event carries it yet. For tst it is still the rollout order's second step, and
   for the channel's own prose the decision is made when that step is. **Open for the
   product owner**: whether the read should have a prose switch of its own rather than sharing
   this one.
4. **Webhook-only routes are unscored** (the fourth KTD12 limitation above). **Decided 2026-09-09:
   they stay out of `varsel_decisions` until `/historikk` and `/meldinger` are behind
   authentication — planned, and expected before any real client data — and go in once they are.**
   **Unblocked 2026-09-10**: both pages and both APIs are behind Entur SSO in every environment
   (#142), so this is now owed work, not a deferral: the channel discriminator on `varsel_decisions`,
   the groups view, the labelling INSERT and the scheduled-query re-registration, in one operation.
   Including them now was offered if it simplified the channel; it does not, it adds: `pushed` is
   journey-definition grain and cannot tell a webhook from a Web Push (KTD10), so the row needs a
   channel discriminator, and that column reaches `varsel_decision_groups`, the nightly labelling
   INSERT and the scheduled-query re-registration in one operation.
5. **R16's prompt-record claim is half-closed** ([above](#r16-and-the-half-of-it-that-is-not-what-it-looks-like)).
   **Decided 2026-09-09: accepted as it stands**, and R16 is kept — dashboard authentication does
   not reach the Cloud Logging half, and the code behind it is one field.
6. **`/notification-api/v1` is discoverable in the public `/docs`.** **Decided 2026-09-09: by
   design — this is a public API.**
7. **`firestore.rules` asserted a protection nothing deployed.** **Decided 2026-09-09: wired** — see
   [Terraform](#terraform-terraformfirestoretf). **Un-wired 2026-09-10**: the release cost every
   Terraform PR its plan (the plan identity cannot read a ruleset) and guarded a Firebase app that
   does not exist; the file is back to being the emulator's and the tests' statement, and the
   release returns the day an app is registered.

---

## Manual steps — everything no code change can do

Nothing in this list is done. The channel cannot be enabled anywhere until the first four are.

1. **Create the two Secret Manager secrets in each environment** — `NOTIFICATION_KEY_PEPPER` and
   `WEBHOOK_SIGNING_ROOT`, ≥ 32 bytes, distinct per environment, written with `printf '%s'` and
   verified with `wc -c` and `tail -c1 | xxd -p`. Then, in the same change that sets
   `NOTIFICATION_WEBHOOKS_ENABLED`, uncomment the `# notifications:` group in that environment's Helm
   values file.
2. **`entur apply` the `.entur/application.yaml` change**, then **verify the binding landed** with
   the `get-iam-policy` recipe above. The platform's filter drops a non-matching role silently.
3. **Apply `terraform/` per environment** — dev on merge to `main`, tst and prd by
   `gh workflow run terraform.yml -f environment=<env>`. Expect the first apply in each project to
   need a re-run while `firestore.googleapis.com` propagates. On the **first plan**, confirm the CI
   account can plan the new file at all: its read side is `roles/viewer` plus the custom org role
   `entur.githubCiTerraformPlanReader`, whose contents an ordinary developer cannot describe, and it
   is the one permission in KTD5's chain that was never measured.
4. **The operator groups — DONE for the pilot by shortcut, 2026-09-10**, and still owed in the
   intended form. The tool gates every environment on the team group
   `sg-dig-team-ki-eksperimentering@entur.no` (see "The operator tool" above for why and what it
   gives up). The end state is still two dedicated groups, `kix-avvik-notification-operators` for
   dev and tst and `kix-avvik-notification-operators-prd` for prd, each holding
   `roles/datastore.user` in the projects it operates — a Team Plattform request
   (`tf-azure-ad` for the groups, `tf-gcp-iam` for the grants; `#talk-utviklerplattform`), since
   that grant exists nowhere in this repository: `terraform/` cannot make it and
   `.entur/application.yaml` grants only the runtime service account. When they exist, point
   `OPERATOR_GROUPS` at them and restore the prd-is-its-own assertion in `test_notification_auth`.
5. **Close the superseded PR unmerged.** KTD1 keeps `feat/journey-notification-webhooks` out of this
   branch — its dated-leg half is ~2 500 lines and +99.2 MB per bus prediction run that the next
   change would delete — and the wording seam was extracted by file rather than merged. The open PR
   is #136; close it rather than letting it rot open beside a branch that superseded it.
6. **Hand the client its verification secret out of band**, from `show-signing-secret` with that
   environment's `WEBHOOK_SIGNING_ROOT` exported (see the operator tool above — it prints a live
   credential, and the read is audited). Repeat it on every `rotate-signing`, and remember the
   receiver's `secrets` callback returns a **list**, so during a rotation the client holds both
   generations and revocation is dropping one from what that callback returns.
7. **Complete the KTD12 checklist** above before any client is enabled: the per-operator verdict
   counts and go/no-go, the sign-off on any unmeasured operator, the cold-start measurement, and the
   pass measurement in the target environment.
8. **Answer the controller question** with the DPO, and **decide the cap-exemption question** with the
   product owner. Both are in the section above.
9. **Record the store's measured monthly cost** once the pilot runs, and whether Firestore's no-cost
   quota reached the named database. Expected to be cents either way at pilot volume — a figure to
   know, not a gate.
10. **Run the client trial**: a test-phone notification for both a predicted and a confirmed event,
   plus a generic HTTPS receiver outside Firebase. No test can stand in for it, because a 2xx from a
   BFF is not a notification on a phone.

## Known limitations, recorded rather than smoothed over

- **`/notification-api/v1` appears in the app's public `/docs`**, because the router mounts
  unconditionally and `docs_url` is FastAPI's default. **By decision (product owner, 2026-09-09):
  this is a public API**, and the plan's own risk register already accepted that the surface is
  publicly reachable.
- **Ordering of a subscription's daily notifications (four by default) is soonest-departure-first and *exact only
  within a subscription*.** Across subscriptions it is approximate, and outside the 60-minute validity
  horizon a train can be missing from the ordering entirely.
- **`cap_exempt` is decided at mint, not at send.** A `confirmed` minted before its `predicted` was
  ever delivered is **not** exempt — it is the first notification about that train and spends a slot.
- **`valid_until` carries two of its three terms.** The 60-minute horizon and the departure are
  computed at emit; the subscription's lifetime is enforced at `claim_delivery` on a live read, which
  is strictly tighter than any snapshot. The published wording says where each term is enforced rather
  than implying all three land in the field. **The departure term is the EXPECTED departure** (aimed
  plus the predicted typical delay — the matcher's own catchability figure, carried on
  `WebhookSubject.expected_departure_time`), and never earlier than
  `notification_events.DEPARTURE_GRACE_S` (120 s) past the emit instant — a term less than that far
  ahead, already past included, is floored to it. Until 2026-09-10 it was the aimed time, so a delayed but
  still catchable train — kept by the matcher and pushed to browsers — minted an event that was stale
  before its first attempt.
- **The withdrawal run's "consecutive" count is closed by time**, not by a per-cycle observation: two
  below-threshold observations further apart than `WITHDRAWAL_GAP_S` (900 s, ~3 pass periods) start a
  fresh count. Sized from the measured ~296 s period; **re-measure the period before changing either
  number — they are one decision.**
- **The emulator does not mirror production concurrency** ("a simple locking approach") and tracks no
  composite indexes. Emulator tests establish shape and idempotency; atomicity is argued from the
  transaction contract, and index coverage from a static check.
