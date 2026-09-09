# Journey notification webhooks — partner API

`/notification-api/v1` lets a partner register the same kind of watch a traveller registers in
`/varsel` — origin, destination, weekdays and a boarding window — and receive one signed HTTPS
webhook per disrupted train, carrying the ready-to-display Norwegian title and body `/varsel` would
have pushed to its own browsers.

This document is the contract. `firebase-bff.md` beside it is the integration guide for the two
vendored TypeScript packages, which is where a partner should start; the operating contract — flags,
rollout order, retention clauses checked against the code, and what an operator has to do — is
`../operations/partner-notifications.md`.

The machine-readable contracts are checked in and are the authority where this page and they
disagree:

| file | what it is |
|---|---|
| `contracts/notifications/v1/openapi.json` | the six routes, hand-authored |
| `contracts/notifications/v1/subscription.schema.json` | the subscription request and response |
| `contracts/notifications/v1/event.schema.json` | the event envelope (`additionalProperties: false`) |
| `contracts/notifications/v1/signing-vectors.json` | seven cross-language signing vectors, self-describing |

---

## What this is not

**We do not know whether a notification reached a phone.** A 2xx from your BFF is a durable receipt
at your endpoint and nothing more. Everything after it — recipient mapping, consent, FCM/APNs,
presentation — is yours, and no measurement on our side can stand in for it.

**Delivery is at-least-once with no ordering guarantee.** The published contract is *one attempt per
notification pass while the event is valid*. The pass runs on SIRI ingestion changes rather than a
schedule, so the retry **interval** is a measurement (~296 s, 2026-09-08) and not a constant; only
the validity **horizon** is a number. See [Delivery semantics](#delivery-semantics).

**The receiver's guarantee is one *decision*, at-least-once *dispatch*.** The supplied packages make
exactly one suppression decision per event and replay it verbatim; they do not make dispatch
exactly-once, and deliberately so. See [What the packages guarantee](#what-the-packages-guarantee).

**Nothing here is a claim about alert quality**, and nothing anywhere is a claim about any operator
but **Vy rail**. `/historikk` scores `/varsel`'s decision logic on the journey definitions a browser
registration also holds; a partner-only route produces no decision row at all and is therefore
**unscored, deliberately**. `../operations/partner-notifications.md` states the four limitations that
follow (KTD12) and the enablement condition they impose.

---

## Quickstart

1. **Ask an operator to provision you.** There is no self-service. Provisioning creates the partner
   account, one callback destination per environment, and one API credential; every action lands in
   an append-only audit record. See `scripts/manage_notification_partner.py` and the operations doc.
   You get back a `key-id` and a secret **shown once** (from `issue-key`), and a `destination_id`.
   **You also need the verification secret your receiver checks signatures with, and no shipped
   command prints it.** It is `HMAC-SHA256` of the environment's signing root over the destination
   and generation, so an operator holding the root can derive it with
   `webhook_signing.derive_secret(destination_id, generation)`; there is no subcommand and no route
   that does — `GET /destinations/current` serves generation *numbers* only, deliberately, so the API
   has nothing to leak. Ask the operator for it explicitly, once per live generation. Environments are
   separate GCP projects: a dev credential is worthless in prd.
2. **Stand up a receiver.** `firebase-bff.md`. The vendored packages do the signature verification,
   the idempotent inbox, revision ordering and duplicate suppression; you supply recipient lookup,
   an eligibility recheck and native dispatch.
3. **Register a test-only watch.**

   ```http
   POST /notification-api/v1/subscriptions
   Authorization: Bearer kixn_0123456789abcdef0123.<secret>
   Content-Type: application/json

   {"subscription_id": "smoke-1", "destination_id": "acme-prod",
    "from_stop": "NSR:StopPlace:337", "to_stop": "NSR:StopPlace:59872",
    "begin": "07:00", "end": "09:00", "days": [0,1,2,3,4],
    "partner_ref": "commuter-4711", "test": true}
   ```

   `test: true` is **immutable** — it cannot be set or cleared later, only deleted and re-created.
4. **Fire a synthetic event.** `POST /notification-api/v1/test-events` with
   `{"subscription_id": "smoke-1"}`. One signed event goes to your callback, nothing is stored on
   our side, and the daily cap is untouched. Six per minute.
5. **Register the real watches** with `test` omitted, and watch for events.

---

## Authentication

```
Authorization: Bearer <key-id>.<secret>
```

- `key-id` is `kixn_` + 20 hex characters (`^kixn_[0-9a-f]{20}$`, 25 characters).
- `secret` is 256 bits, url-safe base64 without padding (43 characters). It is shown **once**, at
  issue, and stored only as an HMAC-SHA256 hash under a per-environment pepper.
- The separator is `.`, which occurs in neither half.
- A **bearer header, never a cookie**: this app carries one global `CORSMiddleware` with
  `allow_origins=["*"]`, so every endpoint is reachable from every origin and a cookie would be sent
  from all of them.

**Several credentials are live at once** (up to 8 active per partner), so a rotation is: issue a new
one, cut over, revoke the old. Revocation takes effect on your next request. A partner-level
suspension takes effect within 60 s.

Every failure — missing header, wrong scheme, unknown `key-id`, revoked credential, wrong secret,
expired credential — answers the same **401 `{"detail": "unauthorized"}`** with
`WWW-Authenticate: Bearer`. The reason is logged and never returned: "unknown key id" and "wrong
secret" must not be distinguishable over the wire. A suspended partner gets **403 `partner_suspended`**.

**Reads are authenticated and throttled exactly like writes.** A GET is a Firestore query like any
other.

---

## Rate limits and quotas

| limit | value | breach |
|---|---|---|
| global pre-lookup, process-wide, 60 s sliding | 300 req/min | `429 rate_limited`, `Retry-After: 60` |
| per partner, after authentication, 60 s sliding | 120 req/min | `429 rate_limited`, `Retry-After: 60` |
| `POST /test-events`, per partner, on top of the above | 6 req/min | `429 test_events_rate_limited`, `Retry-After: 60` |
| active subscriptions per partner | 500 | `429 quota_exceeded`, `Retry-After: 300` |
| active credentials per partner | 8 | operator-side `Conflict` |
| active destinations per partner | 4 | operator-side `Conflict` |
| list page size | default 100, max 500 | `422` outside 1–500 |

The two limiter tiers exist because *verifying a credential is itself store work*: a repeated bad
credential is refused by the global tier and a short-TTL negative cache before any Firestore read, so
it cannot spend a real partner's budget. An anonymous flood can still exhaust the global tier — the
same accepted trade `/impact`'s limiter makes.

`Retry-After` on a quota breach is deliberately 300 s and not 60: a subscription-count ceiling does
not clear when a limiter window rolls over, so telling you to retry in a minute would invite exactly
the retry loop the ceiling exists to stop.

---

## Availability

The router is **mounted unconditionally in every environment**, independent of the channel flag —
gating onboarding on the flag that needs onboarding is circular. Two consequences:

- `/notification-api/v1` appears in the app's public `/docs` (FastAPI's default `docs_url`). Accepted
  rather than overlooked: this surface is publicly reachable by design, with the credential, the
  two-tier limiter and the quotas in front of it.
- A deployment whose store or key pepper is not configured answers **503
  `{"detail": "channel_not_configured"}`** with `Retry-After: 60` on every route — never 401.
  **Every environment answers this today**: the database name ships in the base chart and is
  inherited everywhere, so the pepper is the only discriminator and it exists in none of the three
  projects. Any other store fault answers **503 `store_unavailable`**.

`PARTNER_NOTIFICATIONS_ENABLED` gates the *notification pass* — whether the poller matches
subscriptions and delivers events — not the API. With it off you can register watches and read them
back; no event is minted and nothing is delivered.

---

## Routes

All six sit under `/notification-api/v1`. Both the authentication and the throttle dependency run on
every one of them, before the handler.

### `POST /subscriptions` — create or replace a watch

There is no PUT and no PATCH. Your own `subscription_id` is the idempotency key; the stored document
id is `<partner_id>~<your reference>`, so two partners may use the same reference and neither can
address the other's record.

| field | type | required | rule |
|---|---|---|---|
| `subscription_id` | string | yes | 1–128 of `[A-Za-z0-9._:-]` |
| `destination_id` | string | yes | 1–128 of `[A-Za-z0-9._:-]`; must be yours and active |
| `from_stop` | string | yes | `^NSR:StopPlace:[0-9]+$` |
| `to_stop` | string | yes | `^NSR:StopPlace:[0-9]+$`, and different from `from_stop` **after canonicalisation** |
| `begin` | string | yes | `HH:MM`, 24 h |
| `end` | string | yes | `HH:MM`, strictly after `begin` — **overnight windows are refused**, as they are in `/varsel` |
| `days` | array of int | yes | non-empty subset of 0–6, 0 = Monday. Order and duplicates are normalised |
| `partner_ref` | string | no | ≤ 256 characters, opaque to us, echoed on every event |
| `test` | boolean | no | default `false`. **Immutable after creation** |

`If-Match: <version>` is optional optimistic concurrency. A mismatch, or an `If-Match` on a reference
that does not exist, is **409 `version_conflict`**.

Outcomes: **201** on create; **200** when the definition is byte-identical (no store write at all, so
a retry cannot bump the version, reset a horizon or clear a spent cap slot — it only refreshes the
inactivity horizon, because re-POSTing *is* the "still interested" signal); **200** with a bumped
`version` on an amendment.

**A tombstoned reference is never resurrected.** The id is spent and its record is the audit trail:
`409 "subscription id was deleted or has passed one of its horizons; use a new id"`.

The response is the stored subscription: the nine submitted fields (with `subscription_id` as your
bare reference), plus `partner_id`, `version`, `status`, `servable`, `servable_checked_at`,
`expires_at`, `created_at`, `updated_at`, and `alerts` — which is **always `null` on this route**.

### `GET /subscriptions` — list

`limit` (1–500, default 100) and `after` (a previous page's `next_cursor`). A cursor the current set
does not contain is **400 `unknown_cursor`**, deliberately rather than an empty page. Alert state for
the whole page comes from one partner-scoped query; see [Alert state](#alert-state).

Response: `count`, `limit`, `next_cursor` (`null` on the last page), `subscriptions`.

### `GET /subscriptions/{id}` — one watch

Accepts the bare reference or the namespaced `<partner_id>~<reference>` form an event carries.
Missing, tombstoned, expired and another partner's are all **404 `not_found`** — the difference
between 403 and 404 would be an enumeration oracle.

### `DELETE /subscriptions/{id}` — stop a watch

Idempotent, and never 404s. See [Retention and deletion](#retention-and-deletion) for what it does and
what `cleanup.state: "pending"` obliges you to do.

Response: `subscription_id`, `status` (always `"deleted"`), `existed`, and `cleanup` — `null` when
`existed: false`, otherwise `{state, passes, deleted_events, deleted_deliveries, deleted_subjects,
residual_hours}`.

### `GET /destinations/current` — your callback and its signing generations

`destination_id`, `callback_url`, `status`, `paused`, `paused_at`, `paused_reason`, `ready`, and
`signing_generations: {active, retiring}` — **generation numbers only**. A signing secret is derived
from a root over `destination_id || generation` and is stored nowhere, so this endpoint has nothing
to leak, and it never calls the derivation either.

`ready` is `active AND not paused AND signing configured in this deployment`.

### `POST /test-events` — one signed synthetic event

`{"subscription_id": "<your reference>"}`. Refused with **409 `not_a_test_subscription`** unless the
watch was created `test: true`.

Sent synchronously and **nothing is stored** — no event, no delivery, no alert subject, no cap slot.
That is not convenience: the pending-event ceiling is exact rather than best-effort *because the emit
path has exactly one writer*, and a route that minted an event would falsify every quota check in the
store.

Answers **200 with the outcome** even when your endpoint rejected it —
`{event_id, subscription_id, destination_id, delivered, status, error, signed_generations}` — because
this is a diagnostic endpoint and the classification is the diagnosis. A refusal on *our* side (not a
test watch, paused destination, signing not configured) is a 4xx/503 as usual.

**The synthetic event's `payload` is a simplified stand-in, and it is not shaped like a real one.**
The *envelope* is identical, and that is what the receiver's protocol work runs on. But
`payload.probabilities` is `{"impact", "cancel"}` here against a real event's
`{"impact", "cancellation", "delay_ge10", "alert_threshold"}`; `payload.call` is
`{"operator", "line", "stop_place", "aimed_departure"}` against
`{"stop_place", "stop_order", "aimed_departure_time", "departs_tomorrow"}`; and `delay`, `effect` and
`situations` are always null or empty. **Do not type a client against a test event's payload** — read
the real shapes below, and treat `payload` as open (the schema declares
`additionalProperties: false` on the envelope only).

### Errors

Every error this module raises itself is `{"detail": "<short stable string>"}`:

| status | `detail` |
|---|---|
| 400 | `if_match_must_be_a_version_integer`, `unknown_cursor` |
| 401 | `unauthorized` |
| 403 | `partner_suspended` |
| 404 | `not_found`, `no_destination` |
| 409 | `version_conflict`, `not_a_test_subscription`, `destination_missing`, `destination_paused` |
| 422 | `unknown_destination` |
| 429 | `rate_limited`, `quota_exceeded`, `test_events_rate_limited` |
| 503 | `channel_not_configured`, `store_unavailable`, `signing_not_configured` |

Validation refusals carry prose rather than a code, and it is the prose `/varsel`'s own rules produce:
`"begin must be before end (overnight windows are not supported)"`,
`"days must be a non-empty subset of 0..6 (0=Monday)"`, `"begin/end must be HH:MM (24h)"`,
`"from_stop and to_stop must differ"`, `"the test marker is immutable; delete and re-create instead"`,
`"subscription id already exists with a different definition"`, `f"version is {n}, not {m}"`.

**One shape this page cannot flatten:** a schema-level 422 — an unknown body field under
`extra="forbid"`, a `subscription_id` failing its pattern, `limit` out of range — is FastAPI's default,
where `detail` is an **array of objects** (`type`/`loc`/`msg`/`input`), not a string. Parse `detail`
defensively.

---

## Servability

`servable` and `servable_checked_at` answer "does any planned direct trip run between these two
places today?", from `/varsel`'s own connectivity check.

- **A pair no direct trip serves is stored, never refused.** Weekend-only and seasonal routes are why
  a refusal would be wrong.
- **`null` means the answer could not be established** — an outage, a timeout, the connectivity
  endpoint's own rate limit. It is never an all-clear, and it is never written as `false`.
- The answer is retired and re-asked when you amend the definition.

**Known gap, published rather than implied.** There is no periodic re-check for a partner
subscription. `/varsel` re-asks every 6 h for its own registrations and clears the caveat when service
appears; the partner path sets the verdict on create and replace only, so a weekend-only route reads
`servable: false` until you re-POST it. Re-POSTing the identical definition is the workaround and it
costs nothing. Tracked as a follow-up.

Related: `/varsel` rewrites stored registrations onto canonical stop refs when the stop registry
rebuilds, and that rewrite reaches its own state blob and **not** Firestore. Your refs are
canonicalised on the way in; re-POSTing re-canonicalises them.

---

## Alert state

The two GET routes carry `alerts` — what we currently hold about this watch, read from committed
events. It is **always `null`** on the create/replace response, so `null` means "not included in this
response", never "nothing standing" (that is `n_active: 0` with `truncated: false`).

```json
{"as_of": "...", "n_active": 6, "n_confirmed": 2, "truncated": false, "subjects": [ ... ]}
```

Each subject: `alert_id`, `phase` (`predicted` or `confirmed` — a withdrawn alert is never listed),
`revision`, `delivered_revision` (`null` = an alert we hold and you have not been sent),
`operator`, `line`, `journey_ref`, `call_date`, `stop_place`, `stop_order`, `updated_at`.

Two bounds, and they answer different questions:

- **At most 4 subjects are enumerated**, deliberately the daily notification cap: a fifth is one the
  cap has already suppressed today, so listing it would be reporting what cannot be told to anybody.
  The **counts are over everything read**, so nothing is hidden, only unenumerated.
- **At most 500 subject documents are read per request.** Past that, `truncated: true` and the counts
  are lower bounds. On a list page the flag is set on **every** row when that page's single query was
  bounded, and it fires at 500 live subjects **across the whole partner**, not per subscription. Ask
  about one watch individually to scope the read to it.

`truncated: true` also covers a failed read. Beside it, `n_active: 0` means *we could not establish
this* — never all-clear.

Ordering is `confirmed` before `predicted`, then most recently changed, then `alert_id`: a declared
cancellation is never displaced by a freshly-changed forecast, and the order cannot flip between two
requests about unchanged data.

---

## The event

`contracts/notifications/v1/event.schema.json` is the authority. `additionalProperties: false` **on
the envelope**; the nested objects under `payload` are deliberately open.

### Envelope

| field | type | required | notes |
|---|---|---|---|
| `schema_version` | `"1"` | yes | refuse an unknown version rather than mis-parse it |
| `event_id` | string | yes | immutable, deterministic over `(alert_id, revision)`. **Not a capability**: never a lookup key or a bearer credential |
| `type` | enum | yes | `journey.alert.created`, `journey.alert.updated`, `journey.alert.withdrawn` |
| `phase` | enum | yes | `predicted`, `confirmed`, `withdrawn` |
| `alert_id` | string | yes | the per-train subject key — grain **(subscription, train)** |
| `revision` | int ≥ 1 | yes | strictly increasing per `alert_id` |
| `occurred_at` | date-time | yes | when we decided this revision, UTC |
| `valid_until` | date-time | yes | see [Delivery semantics](#delivery-semantics) |
| `subscription_id` | string | yes | the **namespaced** form, `<partner_id>~<your reference>` |
| `subscription_version` | int ≥ 1 | yes | the version this event was authorised by |
| `partner_ref` | string | no | your opaque reference, echoed unchanged |
| `test` | boolean | yes | inside the signed body, not a header |
| `operator` | string | yes | codespace, e.g. `VYG` |
| `line` | string | yes | |
| `journey_ref` | string | yes | the dated service journey reference |
| `call_date` | date | yes | |
| `stop_place` | string | yes | the **boarding** stop place |
| `stop_order` | int ≥ 0 | yes | |
| `situations` | array of string | no | contributing SIRI-SX situation numbers |
| `payload` | object | yes | facts plus the Norwegian prose |

**`alert_id`'s grain is `(subscription, train)` and not the train alone**, and this matters to your
suppression: a per-train key would make "drop a revision not strictly newer" suppress the *second*
commuter's notification about the same cancelled train. The train's own fields ride along for display
and for your joins; they are not the identity.

**A revision is minted only on a lifecycle transition** — none → phase, `predicted` → `confirmed`,
`withdrawn` → `{predicted, confirmed}`. A changed probability, delay figure or wording mints **nothing,
at any size**. There is no materiality threshold to tune, because minting on any changed figure would
turn every poller cycle into a fresh revision and a second notification about the same train.
`confirmed` → `predicted` mints nothing either — enforced a layer before you, so such an event should
not exist.

`withdrawn` means **the forecast changed, not that normal service is guaranteed**, and the wording is
deterministic rather than written by a model, because there is nothing to retract *with*: a retraction
led by "91 % sjanse for innstilling" would say the opposite of what it means. The title is the train
label (or, where no departure is named, the route) followed by «varselet er trukket tilbake», and the
body is the route on its own line then «Vi varsler ikke lenger om denne avgangen. Det betyr ikke at
den er garantert i rute.» — the second sentence being the one a traveller could most easily read as an
all-clear, so it is stated rather than left out.

A `situations` entry with a `KIX:` prefix means **no operator message is behind this alert**:
`KIX:ETCancellation:` is a cancellation the real-time feed declared with no SIRI-SX message naming it,
`KIX:NoSituation:` is a disruption scored from real-time data alone. There is nothing to fetch for
either.

### Payload

`payload.notification` is required and carries `title`, `body` and `action` (`object|null`). **They
are ready to display** — R7's "normal receipt needs no second request to us" is a property of what
this carries. The prose is the same text `/varsel` would have pushed for the same journey, written by
the same assembly, and it is deliberately brand-independent.

Beside it: `call` (`stop_place`, `stop_order`, `aimed_departure_time`, `departs_tomorrow`),
`probabilities` (`impact`, `cancellation`, `delay_ge10`, and `alert_threshold` — the operator's own
alerting bar, so the reason this cleared it is visible rather than inferred), `delay`
(`{typical_min, worst_case_min}` or `null`), `effect`, `situations` (objects: `situation_number`,
`summary`, `description`, `has_situation`) and `alternative_journey` (`object|null`).

**`payload.call`'s schema description over-promises and is being left as the description rather than
quietly implemented.** It says the call carries "aimed and expected times, and whether boarding and
alighting are permitted there". Only `aimed_departure_time` is emitted; there is no expected time and
there are no boarding/alighting flags on the wire. The matcher itself does honour the two SIRI
activities — a train that calls at the destination with alighting closed is never offered — so the
*selection* is right and only the reported field set is short. Do not write a client that reads them.

**`effect`'s vocabulary is `"cancelled"`, `"delayed"` or `null`** — the schema declares the field and
enumerates no values, so this list is the code's and is stated here rather than left to be discovered.
`cancelled` means the feed declared it; `delayed` means a delay figure was established; `null` means
neither was.

`alternative_journey` is the **best remaining way there**, not necessarily a faster one. Read
`arrives_before_original` rather than assuming a saving; `minutes_saved` is signed.

A `confirmed` event carries probabilities of 1.0 and **null delay figures** — every delay figure meant
"if it runs".

---

## Signing

Standard Webhooks. Three headers, on every attempt:

```
webhook-id: 3f9c1e...          the delivery id — STABLE across attempts
webhook-timestamp: 1789000000  Unix seconds — FRESH per attempt
webhook-signature: v1,<b64> v1,<b64>
```

The MAC is HMAC-SHA256 over `webhook-id.webhook-timestamp.<raw body>`, base64-encoded. **Verify over
the raw bytes, before parsing.** The `webhook-id` is the delivery id, which equals the `event_id`:
40 lowercase hex characters, no prefix and no structure — treat it as opaque and do not match on a
shape. The body is serialised once and signed unmodified; re-serialising it
signs a different document as soon as anything reorders a key.

- **`webhook-id` is stable, the timestamp and signature are not.** A retry that reused the first
  attempt's timestamp would land outside your five-minute replay window, and a *correct* receiver
  would drop our own retry as an attack.
- **Several `v1,` entries may be present** — one per live signing generation. Accept if **any** of
  them verifies. That is what makes a rotation a cutover rather than a flag day, and revocation is
  removing one generation. The supplied receivers take a `secrets(request)` **callback** returning
  the live generations, not a fixed array, so a rotation needs no redeploy and one receiver can
  serve several destinations.
- **Five minutes is the reference replay tolerance.**
- Each destination's secret is derived as HMAC-SHA256 of a per-environment root over
  `destination_id || generation`, so we store no signing material and one partner's leaked
  verification secret forges nothing for another.
- **The verification secret lives in your functions runtime configuration and never in a mobile
  bundle.** A client that holds it can forge an event for any commuter you serve, and a secret in a
  shipped app cannot be rotated.

`contracts/notifications/v1/signing-vectors.json` carries seven vectors and is self-describing.
Its `how_to_consume.typescript_standardwebhooks` gives the **clock-free** check, because the npm
package's `verify()` reads `Date.now()` and cannot accept a fixed timestamp. The `norwegian-body`
vector disagrees about length between the two languages on purpose (utf8 400, utf16 386): **sign over
bytes, never over string length.**

---

## Delivery semantics

**Validity is the earlier of a 60-minute horizon from `occurred_at`, the train's departure, and the
subscription's lifetime** — and the three terms are not enforced in the same place, which the contract
states rather than implies:

- the **60-minute horizon** and the **departure** are computed at emit and are what `valid_until`
  carries;
- the **subscription's lifetime** is enforced at dispatch, on a live read immediately before the
  attempt. So a published `valid_until` can outlive the subscription by up to an hour, while nothing
  is ever *sent* past its expiry. This is strictly tighter than a snapshot taken at emit would be.

Past validity the event is marked **expired, not failed**, and is not retried.

One attempt per pending event per notification pass; the next pass is the retry. Response handling:

| your answer | ours |
|---|---|
| 2xx | success. The daily cap slot is spent here, on success — a dead endpoint must not burn your four |
| **410 Gone** | terminal for the event, and the **destination is paused until an operator resumes it** |
| 408, 429, 5xx | retryable. `Retry-After` is honoured, capped at the lesser of the header, the time to departure, and 300 s, and persisted so the backoff survives a pass boundary and a restart |
| other 4xx | terminal for that event only; other events and other destinations are unaffected |
| 3xx | terminal. A redirect is a configuration fault the next attempt would hit again, and following one is what host pinning prevents |
| network error, timeout | retryable |

Per attempt: **5 s total, 2 s connect.** A destination whose cold start exceeds that is **never
delivered to at all** — not merely a cycle late: the first failure skips it for the rest of the cycle,
there is one attempt per cycle, the next cycle repeats the cold start, and the events end as
`expired`, which reads like ordinary expiry rather than a stuck destination. `minInstances: 1` on your
receiver is the fix, and measuring that latency before enablement is a required pre-flight check.

**At most four alert notifications per subscription per Oslo day**, spent soonest-departure-first —
`/varsel`'s own cap at `/varsel`'s own grain. A commuter who registers a morning and an evening watch
has an allowance for each; bounding a traveller's total across their watches is yours to add if you
want one. A suppressed alert is recorded and re-offered on the next pass.

Two honest caveats on that cap:

- **The exemption is decided when the revision is minted, not when it is sent.** A `confirmed` minted
  before its `predicted` was ever delivered is **not** exempt — it is the first notification about
  that train and spends a slot.
- **The exemption is generalised beyond "a confirmation"**: *any* replacement of a revision already
  delivered about that train is exempt, which includes a **withdrawal**. The reasoning is that a
  retraction the cap suppressed leaves the traveller holding a warning we have withdrawn — the same
  argument Rushvarsel's friskmelding rests on. It is wider than the requirement's own wording says,
  and it is flagged as an open product question rather than presented as settled.

**Two SIRI-SX messages about one cancelled departure produce one event**, from the most severe alert,
with every contributing situation number on it. The winner is tie-broken deterministically so it
cannot flip between passes.

---

## What the packages guarantee

The two vendored TypeScript packages implement, so you do not:

1. signature verification over raw bytes, with multiple generations;
2. an idempotent inbox keyed on `event_id`;
3. the three suppression rules, in order — drop a duplicate `event_id`; drop a revision not strictly
   newer than the highest applied for that `alert_id`; never dispatch a `predicted` revision for a
   train already `confirmed`, whatever its revision;
4. a recovery sweep for records a trigger never picked up.

You supply recipient lookup, an eligibility recheck immediately before dispatch, and native dispatch.

**The guarantee is one *decision*, at-least-once *dispatch*.** The ledger makes exactly one
suppression decision per event, atomically, and replays it verbatim on re-entry; it does **not** make
dispatch exactly-once. Two genuinely concurrent workers on one record can both dispatch, and a crash
between your dispatch and the settle re-dispatches. A lease was deliberately not added, because it
trades a duplicate for a *lost* notification whenever a leaseholder dies. Absorb the duplicate with a
per-train collapse key on the device — which is what `/varsel` itself does, one notification tag per
train per registration.

**Both a `confirmed` and a `withdrawn` event default to `silent`**, and only a `predicted` one to
`alert` (`defaultDisplay`). A confirmation replaces a forecast the traveller has already been shown —
presenting it as a second alarm charges them twice for one disruption — and a withdrawal retracts a
warning rather than raising one. Override with the `display` resolver if the app wants otherwise.

Rule 3 tracks the **standing phase**, not "ever confirmed". A cancelled train can be reinstated, we
mint `withdrawn`, and a fresh forecast after that is a real transition; the "ever" reading would
silence every alert about that train for the rest of the day.

Two numbers of the receiver's own, neither of them in the sender's contract:

- **A 64 KiB inbound ceiling.** An abuse bound, not a fit bound — the largest event we can produce is
  low single-digit kilobytes. What matters is *where* it is enforced.
- **A 15-minute processing cutoff, measured from acceptance.** The Functions trigger retries a thrown
  error for up to 24 hours, which is right for a store outage and wrong for a bug: a dispatch that
  throws on some payload would otherwise re-run every few minutes for a day, each run a chance of a
  duplicate about a train that left hours ago. It **cannot tell a bug from an outage** — a store
  outage longer than fifteen minutes expires every event accepted during it, including ones with
  forty minutes of `valid_until` left. It is a product choice, it is overridable per call
  (`processingCutoffMs`), and it is published here rather than left to be discovered.

---

## Retention and deletion

The clauses below are checked against the code that implements them; the clause-by-clause table,
naming the function behind each one, is in `../operations/partner-notifications.md`.

| what | kept | how |
|---|---|---|
| events, deliveries, alert subjects | **7 days** from creation | a TTL field, plus an explicit expiry test on every read |
| a subscription | the **earlier** of 180 days from creation and 60 days from your last interaction | both horizons stored and tested on read, not TTL alone |
| a deletion tombstone | 7 days | redacted the moment cleanup completes |
| the operator audit record | **indefinitely** | append-only; nothing in this repo updates or deletes a row |

**Deletion is ordered and durable.** `DELETE` writes a tombstone first, in one transactional write
that stops routing, and only then deletes the events, deliveries and alert subjects behind it —
reversed, a partial failure would leave a routable subscription whose events are gone. Cleanup runs in
bounded batches (three passes of 200 documents per request) with an auditable completion state.

**If the response says `cleanup.state: "pending"`, deletion is not finished and there is no background
job that will finish it.** Re-issue the `DELETE` — it is idempotent and resumes — or the remaining
documents fall to TTL. `residual_hours: 24` on that response is the TTL *lag*; the full worst case,
if you never re-issue, is the document's own 7-day retention plus that lag, so up to about **8 days**.

**Two things deletion does not reach, and both are published rather than implied.**

1. **The in-process wording caches.** They key on the *journey definition*, which is exactly what your
   subscription shares with any browser registration on the same route — so removing one
   subscription's wording would remove the other's. Those entries expire with the SIRI-SX situation
   they belong to (up to 12 h from its onset) and are dropped the first pass a subject stops being
   alert-worthy. They hold prose, never a partner id, a callback URL or your reference.
2. **Partner and event identifiers in cluster logs.** They have their own retention and access
   policy, which the operations doc states. No log line on this path carries an origin, a
   destination, a departure time, a full callback URL or any notification prose. Three things do
   reach a log line and are named rather than glossed: your `partner_ref` never does, but your
   **`subscription_id`** does (as `<partner_id>~<your reference>`, on `partner_subscription` and
   `partner_cleanup`) and is hashed into `alert_id`; a **refused destination's hostname and its
   resolved IP addresses** do, on `partner_delivery_refused`; and an operator's own email address
   does, on `partner_admin`.

**The seven-day event retention is shorter than the ten-day window the nightly outcome-labelling job
uses**, so retrospectively scoring partner alerts is foreclosed by this choice. That is a published
contract, so lengthening it later is a contract change rather than a tuning edit.

---

## Environments

| environment | base URL |
|---|---|
| dev | `https://kix-avvik.dev.entur.io/notification-api/v1` |
| tst | `https://kix-avvik.staging.entur.io/notification-api/v1` |
| prd | `https://kix-avvik.entur.io/notification-api/v1` |

Each is a separate GCP project with its own runtime identity, its own Firestore database, its own
signing root and its own key pepper. Credentials, destinations, subscriptions and events do not cross
between them — that isolation is what the project boundary enforces, not an application check.

---

## Not in scope

Deferred, and named so nobody plans around a promise that was not made: exact dated itinerary
subscriptions and a dated-departure identity contract; cross-source arbitration against the existing
Ukur cancellation feed; the companion BFF integration and native enrollment work — **you own the
surface a commuter registers a journey on**, and the subscription shape assumes it captures origin,
destination, weekdays and hours as `/varsel`'s does; notifications tied to a specific ticket rather
than a registered journey; publishing the helper packages to a registry; a delivery-status API or
dashboard for partners; scored outcomes for partner-only route definitions; destination-ownership
verification; additional languages; self-service onboarding; new model or operator coverage.
