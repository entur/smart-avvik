# Journey notification webhooks — notification API

`/notification-api/v1` lets a client register the same kind of watch a traveller registers in
`/varsel` — origin, destination, weekdays and a boarding window — and receive one signed HTTPS
webhook per disrupted train, carrying the ready-to-display Norwegian title and body `/varsel` would
have pushed to its own browsers.

This document is the contract. `firebase-bff.md` beside it is the integration guide for the two
vendored TypeScript packages, which is where a client should start; the operating contract — flags,
rollout order, retention clauses checked against the code, and what an operator has to do — is
`../operations/notification-webhooks.md`.

The machine-readable contracts are checked in and are the authority where this page and they
disagree:

| file | what it is |
|---|---|
| `contracts/notifications/v1/openapi.json` | the seven routes, hand-authored — six credentialed, and the public journey-keyed read `GET /journey-alerts` (`security: []` on that one operation). Its tags — `notification-subscriptions`, `notification-destinations`, `notification-test`, `notification-journey-alerts` — are the two routers' own, pinned as one vocabulary by a test; a bare `subscriptions` would say nothing among this app's other paths |
| `contracts/notifications/v1/subscription.schema.json` | the subscription request and response |
| `contracts/notifications/v1/event.schema.json` | the event envelope (`additionalProperties: false`) |
| `contracts/notifications/v1/signing-vectors.json` | seven cross-language signing vectors, self-describing |
| `contracts/notifications/v1/test-event-sample.json` | one synthetic event, **generated** from `_synthetic_event` rather than authored — the worked example of a real payload, and what the vendored receiver's end-to-end scenario reads. Regenerate it; do not edit it |

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
registration also holds; a webhook-only route produces no decision row at all and is therefore
**unscored, deliberately** — and so is any definition read through [`GET /journey-alerts`](#the-journey-keyed-read)
that nobody registered. `../operations/notification-webhooks.md` states the five limitations that
follow (KTD12) and the enablement condition they impose.

---

## Quickstart

1. **Ask an operator to provision you.** There is no self-service. Provisioning creates the client
   account, one callback destination per environment, and one API credential; every action lands in
   an append-only audit record. See `scripts/manage_notification_client.py` and the operations doc.
   You get back a `key-id` and a secret **shown once** (from `issue-key`), and a `destination_id`.
   **You also need the verification secret your receiver checks signatures with**, and the operator
   prints it for you with `show-signing-secret` — once per live generation, and again after each
   `rotate-signing`. **No route serves it and none ever will**, which is the interesting half: a
   secret fetchable over your own API credential is a secret that credential can be traded for, so
   `GET /destinations/current` gives you the generation *numbers* and never calls the derivation at
   all. The value is derived from the environment's signing root over the destination and
   generation, so nothing stores it and one client's leaked receiver secret forges nothing for
   another. It is a live credential: take it the way you would take a password, not in a ticket or a
   chat message. Environments are separate GCP projects: a dev credential is worthless in prd.
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
    "external_ref": "commuter-4711", "test": true}
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

**Several credentials are live at once** (up to 8 active per client), so a rotation is: issue a new
one, cut over, revoke the old. Revocation takes effect on your next request. A client-level
suspension takes effect within 60 s.

Every failure — missing header, wrong scheme, unknown `key-id`, revoked credential, wrong secret,
expired credential — answers the same **401 `{"detail": "unauthorized"}`** with
`WWW-Authenticate: Bearer`. The reason is logged and never returned: "unknown key id" and "wrong
secret" must not be distinguishable over the wire. A suspended client gets **403 `client_suspended`**.

**Reads are authenticated and throttled exactly like writes.** A GET is a Firestore query like any
other.

**One route is public.** [`GET /journey-alerts`](#the-journey-keyed-read) takes no credential at all:
it is keyed on a stop pair and a period, holds nothing personal, and reads the service's in-process
caches rather than Firestore. It has its own limiter instead, shared by every anonymous caller, and
its contract is its own section below. Everything else on the prefix is authenticated as described
here.

---

## Rate limits and quotas

**The per-client values are pilot defaults, set per client.** Your operator can raise or lower three
of them on your account without a deploy: active subscriptions, requests per minute, and the daily
notification cap per subscription (4 by default, the same as `/varsel`'s per registration). Ask for the
numbers your integration needs when you are provisioned. Two things about them worth knowing: a
subscription ceiling above 10 000 needs work on our side first (the create transaction counts your
live subscriptions by reading them, and past that the count has to become an aggregation), and your
daily cap is copied onto each subscription when you write it, so a raised cap reaches an existing
subscription on your next `POST` of it — the idempotent re-registration is enough.

| limit | value | breach |
|---|---|---|
| global pre-lookup, process-wide, 60 s sliding | 300 req/min | `429 rate_limited`, `Retry-After: 60` |
| per client, after authentication, 60 s sliding | 120 req/min (default; per client) | `429 rate_limited`, `Retry-After: 60` |
| `POST /test-events`, per client, on top of the above | 6 req/min | `429 test_events_rate_limited`, `Retry-After: 60` |
| active subscriptions per client | 500 (default; per client, at most 10 000) | `429 quota_exceeded`, `Retry-After: 300` |
| alert deliveries per subscription per Oslo day | 4 (default; per client, at most 1 000) | the next is suppressed for the day, not queued |
| active credentials per client | 8 | operator-side `Conflict` |
| active destinations per client | 4 | operator-side `Conflict` |
| list page size | default 100, max 500 | `422` outside 1–500 |
| `GET /journey-alerts`, process-wide, every anonymous caller, 60 s sliding | 300 req/min | `429 rate_limited`, `Retry-After: 60` |
| `GET /journey-alerts`, per canonical stop pair, 60 s sliding | 30 req/min | `429 rate_limited`, `Retry-After: 60` |
| `GET /journey-alerts` wordings, every anonymous caller together | 30 / min, 500 / h, 2 000 / day | the entry carries the template with `wording.status: rate_limited`; never a 429 |
| `GET /journey-alerts` Journey Planner lookups, every anonymous caller together | 30 / min | the entry carries no alternative with `alternative.status: unanswered`; never a 429 |
| `GET /journey-alerts` wordings per read | 4, soonest departure first | the rest carry the template with `wording.status: not_worded`, re-evaluated on every read as the trains ahead depart |
| `GET /journey-alerts` `wait` / `limit` | 0–20 s (default 20) / 1–20 (default 10) | `422` outside those |

The two limiter tiers exist because *verifying a credential is itself store work*: a repeated bad
credential is refused by the global tier and a short-TTL negative cache before any Firestore read, so
it cannot spend a real client's budget. An anonymous flood can still exhaust the global tier — the
same accepted trade `/impact`'s limiter makes.

**The public read's limiter and allowances are a separate ledger, and a SHARED one.** They are not
the credentialed tiers above and never touch a client's own budget — but they are also not per
caller: the app's BFF has no reserved slice, so one anonymous source at the ceiling holds every
caller at 429, and one at the daily wording allowance holds every caller template-only for the
trailing day. Treat both as ordinary states and serve your cached answer.

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
  `{"detail": "channel_not_configured"}`** with `Retry-After: 60` on every **credentialed** route —
  never 401. The public read is the exception: it never touches the store or the pepper, so it is
  never `channel_not_configured` — it serves in every environment, tst included, with the one 503
  of its own below.
  **Every environment answers this today**: the database name ships in the base chart and is
  inherited everywhere, so the pepper is the only discriminator and it exists in none of the three
  projects. Any other store fault answers **503 `store_unavailable`**.
- **The public read has a 503 of its own, and you will meet it after every deploy**: **503
  `{"detail": "predictions_not_loaded"}`** with `Retry-After: 60` for about a minute after the
  process starts, until the poller's first load. It is deliberately never an empty list — an empty
  list off an empty cache would report an empty process as a calm day. Your own cache carries the
  panel across it. A failed match answers **503 `matcher_failed`**, likewise retryable.

`NOTIFICATION_WEBHOOKS_ENABLED` gates the *notification pass* — whether the poller matches
subscriptions and delivers events — not the API. With it off you can register watches and read them
back; no event is minted and nothing is delivered.

---

## Routes

All seven sit under `/notification-api/v1`. On the six credentialed ones both the authentication and
the throttle dependency run before the handler; the seventh, `GET /journey-alerts`, is public and has
[its own section](#the-journey-keyed-read).

### `POST /subscriptions` — create or replace a watch

There is no PUT and no PATCH. Your own `subscription_id` is the idempotency key; the stored document
id is `<client_id>~<your reference>`, so two clients may use the same reference and neither can
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
| `external_ref` | string | no | ≤ 256 characters, opaque to us, echoed on every event |
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
bare reference), plus `client_id`, `version`, `status`, `servable`, `servable_checked_at`,
`expires_at`, `created_at`, `updated_at`, and `alerts` — which is **always `null` on this route**.

### `GET /subscriptions` — list

`limit` (1–500, default 100) and `after` (a previous page's `next_cursor`). A cursor the current set
does not contain is **400 `unknown_cursor`**, deliberately rather than an empty page. Alert state for
the whole page comes from one client-scoped query; see [Alert state](#alert-state).

Response: `count`, `limit`, `next_cursor` (`null` on the last page), `subscriptions`.

### `GET /subscriptions/{id}` — one watch

Accepts the bare reference or the namespaced `<client_id>~<reference>` form an event carries.
Missing, tombstoned, expired and another client's are all **404 `not_found`** — the difference
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

`{"subscription_id": "<your reference>"}` — and, like the two path routes, the namespaced
`<client_id>~<reference>` form works too. That matters more than it sounds: the id you have to hand
is usually the one a `GET` response or an event your receiver just handled gave you, and that is the
namespaced form, so accepting only the bare reference meant a 422 from a value this API had itself
just served. Refused with **409 `not_a_test_subscription`** unless the watch was created
`test: true`.

`TestEventRequest.subscription_id` is therefore the one property in `openapi.json` stated inline
rather than `$ref`-ed into `subscription.schema.json`: that file's definition is the **create-side**
constraint (128 characters, no `~`) and is correctly strict there, since a create body must never
name the namespaced form. Until 2026-09-09 this request body referenced it, so a client generating
or validating against the published schema refused the namespaced id client-side — the same bug the
server had already been fixed for, one layer out. **Do not widen the shared definition to reconcile
them**; they are two different constraints.

Sent synchronously and **nothing is stored** — no event, no delivery, no alert subject, no cap slot.
That is not convenience: the pending-event ceiling is exact rather than best-effort *because the emit
path has exactly one writer*, and a route that minted an event would falsify every quota check in the
store.

Answers **200 with the outcome** even when your endpoint rejected it —
`{event_id, subscription_id, destination_id, delivered, status, error, signed_generations}` — because
this is a diagnostic endpoint and the classification is the diagnosis. A refusal on *our* side (not a
test watch, paused destination, signing not configured) is a 4xx/503 as usual.

**A test event is a faithful sample, and it is meant to be the thing you write your receiver
against** — it is the only body you can see before a real disruption. Its `payload` carries the same
keys a real event's does, pinned in both directions by
`test_the_synthetic_payload_has_the_same_shape_as_a_real_one`, and the body is projected through the
delivery pass's own `wire_event` allow-list and serialized by its own `event_body` — the same
allow-list and the same serializer a real delivery goes through, so key order, UTF-8 and timestamp
rendering are identical too.

It is a coherent **confirmed cancellation** rather than an arbitrary sample, because the schema's own
rule is that a `confirmed` event carries probabilities of 1.0 and null delay figures; a synthetic
event stating 1.0 under `predicted` would teach a combination no real event produces. Its
`has_situation` is `false` and truthfully so — no SIRI-SX message named this train, and its
`alert_threshold` is therefore the **no-SX cut** rather than the published one (see
[the payload](#payload)).

Two things still differ, both deliberately: `alert_id` and `event_id` are **random** here, where a
real event derives them from the subject and revision — otherwise the supplied receiver's duplicate
rule would give you exactly one notification however many test events you asked for — and
`journey_ref` is a `KIX:Test:` reference that can never collide with a real subject.

What does *not* change is that `payload` is an **open** object: the schema declares
`additionalProperties: false` on the envelope only, and its sub-objects carry no property list. So a
differently named key inside `payload` would be schema-valid and wrong, which is exactly why that
shape is pinned by a test rather than by the schema.

### `GET /journey-alerts` — the public read

No credential. The alert-worthy trains ahead on a journey definition nobody registered, each carrying
the event's own `payload`. Its parameters, statuses, wait budget, caching rule and the BFF guidance
are [The journey-keyed read](#the-journey-keyed-read) below.

### Errors

Every error this module raises itself is `{"detail": "<short stable string>"}`:

| status | `detail` |
|---|---|
| 400 | `if_match_must_be_a_version_integer`, `unknown_cursor` |
| 401 | `unauthorized` |
| 403 | `client_suspended` |
| 404 | `not_found`, `no_destination` |
| 409 | `version_conflict`, `not_a_test_subscription`, `destination_missing`, `destination_paused` |
| 422 | `unknown_destination` |
| 429 | `rate_limited`, `quota_exceeded`, `test_events_rate_limited` — and `rate_limited` from the public read's own limiter |
| 503 | `channel_not_configured`, `store_unavailable`, `signing_not_configured` — and, on the public read only, `predictions_not_loaded`, `matcher_failed` |

Validation refusals carry prose rather than a code, and it is the prose `/varsel`'s own rules produce:
`"begin must be before end (overnight windows are not supported)"`,
`"days must be a non-empty subset of 0..6 (0=Monday)"`, `"begin/end must be HH:MM (24h)"`,
`"from_stop and to_stop must differ"`, `"the test marker is immutable; delete and re-create instead"`,
`"subscription id already exists with a different definition"`, `f"version is {n}, not {m}"`.
The public read adds two of its own, and neither echoes your value:
`"from_stop and to_stop must be NSR:StopPlace:<digits>"`, `"begin and end must be given together"`.

**One shape this page cannot flatten:** a schema-level 422 — an unknown body field under
`extra="forbid"`, a `subscription_id` failing its pattern, `limit` out of range, a path segment over
its 257-character bound — is FastAPI's default, where `detail` is an **array of objects**
(`type`/`loc`/`msg`/`input`, plus `ctx` where there is a constraint to quote), not a string. Parse
`detail` defensively: branch on its type.

The OpenAPI document publishes both shapes rather than promising only the string one, which it did
until 2026-09-09: `ValidationError` is the array shape, `Error` the string, and every documented 422
is one or the other — `oneOf` on `POST /subscriptions` and on `GET /journey-alerts`, the two routes
with a refusal of their own at that status, and `ValidationError` alone on the four routes where the
framework is the only thing that can return it (both subscription `GET`s, the `DELETE`, and
`POST /test-events`). `GET /destinations/current`
documents no 422 because it constrains nothing: no body, no query, no path parameter. Flattening the
array into `Error` would need a `RequestValidationError` handler, and FastAPI registers those per
application rather than per router — this API shares a process with `/varsel-api`, `/historikk-api`
and `/meldinger-api`, whose validation bodies would silently change shape with it. Two honest shapes,
not one uniform shape bought at that price.

---

## Servability

`servable` and `servable_checked_at` answer "does any planned direct trip run between these two
places today?", from `/varsel`'s own connectivity check.

- **A pair no direct trip serves is stored, never refused.** Weekend-only and seasonal routes are why
  a refusal would be wrong.
- **`null` means the answer could not be established** — an outage, a timeout, the connectivity
  endpoint's own rate limit. It is never an all-clear, and it is never written as `false`.
- The answer is retired and re-asked when you amend the definition.

**It is re-asked, and the cadence is `/varsel`'s own: every six hours per record.** A weekend-only
ferry registered on a Friday evening, a seasonal route registered out of season and a pair whose
timetable had not been published yet all answer "no direct service" at registration and start running
later, so the caveat clears by itself. Three properties of that sweep are worth knowing:

- **Only `false` and `null` are re-asked.** A `true` is never re-checked, deliberately: the harm is
  one-directional — a stale `false` shows you a caveat about a watch that is working, while a stale
  `true` renders nothing and publishes its own age in `servable_checked_at`, which is what R6 asks
  for instead of a silent all-clear.
- **The stamp moves on every established answer, including an unchanged `false`.** So
  `servable_checked_at` is the age of the last *answer*, not of the last *change*.
- **A check that could not be established writes nothing at all** — neither the value nor the stamp.
  One BigQuery blip must not become a freshly-stamped non-answer.

**One window where it does not run**, and it is worth knowing during onboarding: the sweep is gated
on the same flag as delivery, so until the webhook channel is enabled in that environment nothing
refreshes a caveat. Re-`POST`ing the *identical* definition does not re-ask either — that path is a
liveness touch, and only a **changed** definition re-asks. So in that window you are looking at the
create-time answer, with `servable_checked_at` telling you how old it is.

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

- **At most your daily cap's worth of subjects are enumerated** (4 unless your client's cap is
  higher, never fewer than 4), deliberately: a subject past the cap is one it has already suppressed
  today, so listing it would be reporting what cannot be told to anybody. The **counts are over
  everything read**, so nothing is hidden, only unenumerated.
- **At most 500 subject documents are read per request.** Past that, `truncated: true` and the counts
  are lower bounds. On a list page the flag is set on **every** row when that page's single query was
  bounded, and it fires at 500 live subjects **across the whole client**, not per subscription. Ask
  about one watch individually to scope the read to it.

`truncated: true` also covers a failed read. Beside it, `n_active: 0` means *we could not establish
this* — never all-clear.

Ordering is `confirmed` before `predicted`, then most recently changed, then `alert_id`: a declared
cancellation is never displaced by a freshly-changed forecast, and the order cannot flip between two
requests about unchanged data.

---

## The journey-keyed read

`GET /notification-api/v1/journey-alerts` answers "what would Avviksvarsel be warning about on this
trip right now?" for a definition **nobody registered**, with **no credential**. It exists for the
Entur app's favourites panel: one read per saved route when a user opens the app. The selection is
the notification pass's own — the same trains, the same per-operator bar, the same collapse of
several situations about one train into one entry — and every entry's `payload` is built by the same
assembly the webhook event goes through, so a receiver that renders an event renders this.

### Parameters

| parameter | required | rule | default |
|---|---|---|---|
| `from_stop` | yes | `^NSR:StopPlace:[0-9]+$`, canonicalised as `/varsel` canonicalises a submitted ref | — |
| `to_stop` | yes | same, and a different physical place after canonicalisation | — |
| `begin` | with `end` | `HH:MM`, Europe/Oslo | `00:00` |
| `end` | with `begin` | `HH:MM`, strictly after `begin` — an overnight period is refused, ask as two | `23:59` |
| `days` | no | **repeated** parameter, `days=0&days=1`, integers 0–6, 0 = Monday. A comma-separated list is a 422 | today's and tomorrow's Oslo weekday |
| `wait` | no | 0–20 seconds to wait for the wording and the alternative | 20 |
| `limit` | no | 1–20 entries | 10 |

`begin`/`end` come together or not at all; no period is the whole day. No `days` is today and
tomorrow, which is every window the prediction cache can carry — a read at 22:00 with no period
lists a 00:30 departure with `departs_tomorrow: true`.

### The answer

```json
{
  "as_of": "2026-09-10T05:12:04+00:00",
  "refreshed_at": "2026-09-10T05:09:31+00:00",
  "complete": true,
  "n_alerts": 1,
  "truncated": false,
  "journey": {"from_stop": "NSR:StopPlace:337", "to_stop": "NSR:StopPlace:59872",
              "begin": "06:30", "end": "09:00", "days": [2, 3]},
  "windows": [{"start": "2026-09-10T04:30:00+00:00", "end": "2026-09-10T07:00:00+00:00"},
              {"start": "2026-09-11T04:30:00+00:00", "end": "2026-09-11T07:00:00+00:00"}],
  "coverage": {"operators": ["ATB", "FIN", "GOA", "KOL", "MOR", "NOR", "RUT", "TRO", "VYG"],
               "from_stop_in_timetable_today": true, "to_stop_in_timetable_today": true},
  "alerts": [
    {
      "severity": 1,
      "phase": "confirmed",
      "train": {"operator": "VYG", "line": "VYG:Line:L1", "journey_ref": "VYG:ServiceJourney:L1-2143-20260910",
                "call_date": "2026-09-10", "stop_place": "NSR:StopPlace:337", "stop_order": 4},
      "aimed_departure_time": "2026-09-10T05:45:00+00:00",
      "expected_departure_time": "2026-09-10T05:45:00+00:00",
      "situations": ["KIX:ETCancellation:VYG:ServiceJourney:L1-2143-20260910"],
      "payload": {
        "notification": {"title": "Tog L1 07:45 er innstilt",
                         "body": "Oslo S → Lillestrøm\nToget er innstilt. Neste tog L1 går 07:55 og er fremme 08:16.",
                         "action": null},
        "call": {"stop_place": "NSR:StopPlace:337", "stop_order": 4,
                 "aimed_departure_time": "2026-09-10T05:45:00+00:00", "departs_tomorrow": false},
        "probabilities": {"impact": 1.0, "cancellation": 1.0, "delay_ge10": null, "alert_threshold": 0.85},
        "delay": null,
        "effect": "cancelled",
        "situations": [{"situation_number": "KIX:ETCancellation:VYG:ServiceJourney:L1-2143-20260910",
                        "summary": "Innstilt", "description": null, "has_situation": false}],
        "alternative_journey": {"...": "the best remaining way there, see the event's payload"}
      },
      "wording": {"source": "llm", "status": "ok"},
      "alternative": {"status": "offered"}
    }
  ]
}
```

The values above are illustrative; `payload` is documented under [the event](#payload) and is the
same object, so it is not restated here. What the envelope around it means:

- **`alerts` is soonest expected departure first**, at most `limit` entries; `n_alerts` is the
  total before the cut and `truncated` says whether one was made — the soonest trains are kept.
  **`severity` is a 1-based rank in worst-first order** (the pass's own order: severity, then
  situation number) over every train that survived, assigned before the cut, so a client can re-sort
  worst first and a truncated answer may lack some ranks. Soonest-first is the list order because a
  panel opened on the platform needs the next train first, and on a line suspension where every
  cancellation ties, worst-first would cut a commuter's next train.
- **`phase` is `predicted` or `confirmed`** and there is no `withdrawn` entry: a train no longer over
  the gate, or whose expected departure has passed, is simply absent.
- **`train`** is the event's own six identity fields; an entry and an event about one train join on
  them without translation. `expected_departure_time` is the aimed time plus the typical delay
  predicted for it — the figure that decides whether the train can still be caught, and the one the
  list is ordered on; equal to the aimed time for a declared cancellation.
- **`journey` and `windows` are what the answer is about** — the canonical refs, the defaults filled
  in, and the boarding windows as UTC instants (today's remaining one and tomorrow's, on the
  definition's weekdays). **Key your cache on `journey` plus `refreshed_at`**, never on the query
  string you sent: two spellings of one place are one answer.
- **`refreshed_at`** is when the prediction cache the figures come from was loaded. It rebuilds only
  when upstream real-time ingestion moves, so a quiet feed leaves a *correct* cache old; the age is
  served rather than refused, and you judge.

### `coverage`, and what an empty list means

`coverage.operators` is the set of operator codespaces the service scores at all — Vy rail and the
trained bus operators. The two `*_in_timetable_today` flags say whether each canonical stop appears in
today's planned timetable, checked over every ref of the physical place; they **fail open** (an
unloaded timetable reads `true`), because "we do not know" must never render as "not served".

There is deliberately **no servability caveat** on this read: the question costs a BigQuery query per
anonymous pair and writes both refs into a log line, neither of which belongs on a public
unauthenticated path. The `coverage` block is what you render an unknown favourite from — **render a
favourite whose stop reads `false` as unverified, not as calm.**

And read an empty `alerts` for what it is: **no alert-worthy train among the trains the service
scores** — direct trips by the operators in `coverage.operators` — and never all-clear. A trip with a
transfer never alerts; nothing here says anything about any other operator; and nothing here is
evidence about alert quality, because a definition nobody registered produces no decision row and is
**unscored, deliberately** (the fifth KTD12 limitation in the operations document).

### The statuses, and which are final

Each entry carries `wording {source, status}` and `alternative {status}`:

| | terminal (a wording is held for the situation's life, up to 12 h; an alternative outcome for the Journey Planner cache's 10 min) | transient (expire after ~60 s and are retried on a later read) |
|---|---|---|
| `wording.status` | `ok` (source `llm`), `rejected`, `ungrounded` (the model answered and the answer was unusable for this input — re-asking would learn nothing), `disabled` (this deployment does not deliver the model's prose on this channel, or no backend may be asked), `not_worded` (past the four-per-read wording cap in this read's soonest-first order; re-evaluated on every read as the trains ahead depart, never cached — terminal for this read only) | `pending`, `capped` (the enrichment queue was full), `rate_limited` (the read's wording allowance), `error` |
| `alternative.status` | `offered`, `suppressed` (the selection kept the traveller on their own train), `none_found`, `not_worth_lookup` (none was issued), `not_looked_up` (past the per-read lookup cap) | `pending`, `capped`, `unanswered` (the Journey Planner did not reply: throttled, an outage, its cooldown) |

`wording.source` is `llm` or `deterministic` and nothing else. **The template is the floor: render
it.** A `deterministic` entry is a complete, correct notification in Norwegian — it is what `/varsel`
sends whenever the model cannot be asked — and every non-`ok` status above is the reason the model's
text is not there instead.

### The wait budget and `complete`

The read is two halves. The **figures** — which trains, which numbers — are served at once from the
service's in-process cache. The **wording and the alternative journey** are fetched on request, in
the background, under the read's own allowances; the request waits up to `wait` seconds for them and
answers with whatever is ready. What is not ready is served as the template and no alternative, with
the statuses above saying why. **The work continues after the request returns**, so the next read of
the same definition finds it.

`complete` is `true` only when every *listed* entry's two statuses are terminal, and it decides the
caching header:

| `complete` | `Cache-Control` |
|---|---|
| `true` | `public, max-age=60` — safe for an intermediary and for your BFF to hold for a minute |
| `false` | `no-store` — the incomplete entries change on the next read |

A warm read within one prediction cycle runs no matcher, no model call and no Journey Planner call.
A cold definition with one alert-worthy train typically completes within one `wait` (a few seconds:
one match with lookups, then the wording), and about three seconds more per further train up to the
four-per-read wording cap.

### Byte-identity with the event, and its exact boundary

When the five fields you pass are **verbatim a registration's** — a browser's or a webhook
subscription's — and the notification pass has already worded that train, the entry serves the
pass's own wording and `payload.notification` is **byte-identical to the event's**, with no pull
wording spent. The boundary is worth stating precisely, because it is narrower than "the same as the
event":

- it is **exact-key**: `begin`, `end` and `days` are part of the key, so a favourite that differs
  from the registration by a minute is a fresh draw;
- it is **the notification only**: the figures are live where the event's were its mint-time
  snapshot (a revision is minted only on a lifecycle transition, so the event's numbers may be a
  cycle old), and the alternative can differ in one case the operating contract publishes;
- it is **one channel's**: identity is with the webhook event, and the browser push can differ while
  the two prose switches disagree (see the operations document);
- everywhere else the wording is **the read's own draw from a non-deterministic model**, or the
  template — so two definitions on one pair, or two reads across a re-wording, can carry different
  prose about one train, and that is sampling, not a change in the forecast.

The entry says whether its text is the model's (`wording.source`); it **never says whether the
definition is registered**.

### Three accepted exposures, published rather than closed

1. **The completeness and latency of a first read reveal whether a registration with exactly those
   five fields exists and is alert-worthy right now** — a registered definition answers complete at
   once where a cold one waits. The mitigation is that no status ever says so, and that a definition
   is a stop pair with a period, not a person.
2. **The global ceiling and the allowances are shared by every anonymous caller**, the app's BFF
   included, with no reserved slice — one source at the ceiling holds every caller at 429; one at
   the daily wording allowance holds every caller template-only for the trailing day. Treat both as
   ordinary states.
3. **The definition rides in a query string.** The service redacts it from its own access log and no
   record it writes carries a ref, a name, a departure time or any prose — but any log outside the
   process (an ingress, a proxy, your own client) may keep the request line, and that is yours to
   govern.

### BFF guidance

- **Serve favourite routes only.** A favourite departure and a favourite stop place carry no
  destination and have nothing to be keyed on here.
- **Collapse identical canonical definitions before calling.** Two users with the same route and
  period are one read; the per-pair ceiling is 30 a minute and period churn on one pair does not buy
  a fresh bucket.
- **Key your cache on the echoed `journey` plus `refreshed_at`**, not on what you sent.
- **From the panel, send `wait=0`** and read again about five seconds later if `complete` was false;
  render the template meanwhile. A patient tool sends the default `wait` and takes the complete
  answer in one request.
- **Render the template on `complete: false`** — it is a real notification, not a placeholder — and
  never a spinner in its place.
- **Render an uncovered or unknown favourite as unverified**, from `coverage`, never as calm; and
  never render an empty list as all-clear.
- **Treat 429, 503 `predictions_not_loaded` and a `rate_limited` status as ordinary states** and
  serve your cached answer. They are the read's bounds binding, not a fault to alarm on.
- **Whether an entry's wording is the model's follows a deployment switch**
  (`NOTIFICATION_WEBHOOK_LLM_ENABLED`, the webhook event's own; on in dev, off elsewhere today), so
  in an environment where it is off every entry reads `disabled` with the template — correct, and
  complete.

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
| `subscription_id` | string | yes | the **namespaced** form, `<client_id>~<your reference>` |
| `subscription_version` | int ≥ 1 | yes | the version this event was authorised by |
| `external_ref` | string | no | your opaque reference, echoed unchanged |
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
`probabilities` (`impact`, `cancellation`, `delay_ge10`, and `alert_threshold` — the bar this alert
actually cleared, carried so the reason is visible rather than inferred). **`alert_threshold` varies
along two axes, not one.** It is per operator *and* per population: a train **no SIRI-SX message
named** is scored from real-time data alone and is cut at a higher bar than a published disruption —
0.85 against 0.65 for VYG, a difference of 0.20 — because without a message the only signal is the
last hour of observation and the probability decays with the ask horizon far faster. Read the value
off the event; do not assume the published per-operator cut. A `situations` entry whose
`has_situation` is `false` is how you can tell which population an alert came from. Then `delay`
(`{typical_min, worst_case_min}` or `null`), `effect`, `situations` (objects: `situation_number`,
`summary`, `description`, `has_situation`) and `alternative_journey` (`object|null`).

**`payload.call` carries an aimed time and never an expected one, and no boarding or alighting
flags — both by design, and the schema now says so.** The real-time estimate for this call is what
the probabilities and the delay figures beside it are *derived from*; restating it here as a second
time would invite a receiver to display a figure the forecast has already superseded. And the two
SIRI boarding/alighting activities gate the **match** upstream — calling at a stop is not the same as
being usable from it, since long-distance runs are routinely sold boarding-only over their local
stretch — so a call you receive is already one the traveller may board and alight at, and repeating
the flags would publish a rule instead of its result. Putting either on the wire would be a schema
**addition**, not a description change.

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
  `destination_id || generation`, so we store no signing material and one client's leaked
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

**Validity is the earlier of a 60-minute horizon from `occurred_at`, the train's expected departure,
and the subscription's lifetime** — and the three terms are not enforced in the same place, which the
contract states rather than implies:

- the **60-minute horizon** and the **expected departure** are computed at emit and are what
  `valid_until` carries. Expected, not aimed: the aimed time plus the delay we predict for the train,
  which is the same figure that decides whether it can still be caught, so a train running
  25 minutes late is still valid — and still notified — after its aimed time. An expected departure
  less than two minutes ahead of `occurred_at` when the event is minted (already past included) is
  floored to two minutes ahead, so an observation made moments before the train leaves is attempted
  once rather than expiring unsent — `valid_until` is never earlier than `occurred_at` + 2 min;
- the **subscription's lifetime** is enforced at dispatch, on a live read immediately before the
  attempt. So a published `valid_until` can outlive the subscription by up to an hour, while nothing
  is ever *sent* past its expiry. This is strictly tighter than a snapshot taken at emit would be.

Past validity the event is marked **expired, not failed**, and is not retried.

One attempt per pending event per notification pass; the next pass is the retry. Response handling:

| your answer | ours |
|---|---|
| 2xx | success. The daily cap slot is spent here, on success — a dead endpoint must not burn your day's allowance |
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

**At most your daily cap's worth of alert notifications per subscription per Oslo day** (four by default), spent soonest-departure-first —
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
naming the function behind each one, is in `../operations/notification-webhooks.md`.

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
   alert-worthy. They hold prose, never a client id, a callback URL or your reference.
2. **Client and event identifiers in cluster logs.** They have their own retention and access
   policy, which the operations doc states. No log line on this path carries an origin, a
   destination, a departure time, a full callback URL or any notification prose. Three things do
   reach a log line and are named rather than glossed: your `external_ref` never does, but your
   **`subscription_id`** does (as `<client_id>~<your reference>`, on `notification_subscription` and
   `notification_cleanup`) and is hashed into `alert_id`; a **refused destination's hostname and its
   resolved IP addresses** do, on `webhook_delivery_refused`; and an operator's own email address
   does, on `notification_admin`.

**The seven-day event retention is shorter than the ten-day window the nightly outcome-labelling job
uses**, so retrospectively scoring client alerts is foreclosed by this choice. That is a published
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
dashboard for clients; scored outcomes for webhook-only route definitions **and for definitions read
through `GET /journey-alerts`**; destination-ownership verification; additional languages;
self-service onboarding; new model or operator coverage.

And for the public read specifically: a favourite **departure** (a line at a stop) and a favourite
**stop place** carry no destination and are not served — the read is keyed on a pair and serves
favourite **routes** only; a trip with a **transfer** never alerts, so a definition across one reads
as calm however bad the day is; the per-subscription pull with an unguessable token, for a client
that wants the full event envelope rather than the payload; and a pull helper in the vendored
package.
