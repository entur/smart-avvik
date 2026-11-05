# Entur journey notification webhooks — TypeScript client

The typed envelope, the signature verifier, the idempotent inbox contract and the three suppression
rules for Entur's client journey-notification channel. **No Firebase dependency**: it runs in a
Cloud Function, a container, or a `node:http` server, and the generic Node receiver in
`src/nodeReceiver.ts` is a shipped reference rather than an example.

On Firebase, add [`@entur/smart-avvik-firebase-notifications`](../firebase-notifications/README.md), which
supplies the Firestore-backed inbox and ledger, the Cloud Functions wiring and the security rules.

## Why the rules are supplied rather than left as homework

Delivery is **at-least-once with no ordering guarantee**. That is published rather than apologised
for — one attempt per notification pass while the event is valid, and the pass runs on SIRI ingestion
changes rather than a fixed schedule. Each of the three rules below is one whose absence produces a
*plausible-looking* notification about a train that is not in the state the notification claims, and
the third is the one nobody writes unprompted.

## Quickstart

```sh
npm install   # then: import from ./dist/index.js
```

```ts
import {
  createMemoryStores,
  process as processEvent,
  receive,
} from "@entur/smart-avvik-notifications-client";

const stores = createMemoryStores();          // replace with your own persistence

// 1. On the HTTP request: size, signature, envelope, persist, acknowledge. Nothing else.
const result = await receive(
  { secrets: [process.env.ENTUR_WEBHOOK_SECRET], inbox: stores.inbox },
  { headers: request.headers, body: rawBodyBytes, declaredBytes: contentLength },
);
response.status(result.status).json({ outcome: result.outcome });

// 2. Afterwards -- on a retry-enabled trigger, a queue, or a background task in a long-lived
//    process -- run the policy and dispatch.
const record = await stores.inbox.get(result.event.event_id);
await processEvent(
  {
    inbox: stores.inbox,
    ledger: stores.ledger,
    suppressions: stores.suppressions,
    recipients: async (event) => lookUpDevices(event.subscription_id),
    eligibility: async () => ({ ok: true }),
    dispatch: async ({ event, display, recipients }) =>
      sendPush(recipients, event.payload.notification, { silent: display === "silent" }),
  },
  record,
);
```

**Steps 1 and 2 must not be one step in a serverless runtime.** A function that has returned its
response cannot access the CPU, and work left unawaited afterwards may not run at all — or may resume
inside a later request. Persist, acknowledge, then let something retryable do the work.

## The three suppression rules

Keyed on `alert_id`, whose grain is **(subscription, train)** and not the train alone. One client
serves many commuters through one receiver, and a per-train key would make rule 2 suppress the
*second* commuter's notification about the same disrupted train.

1. **Drop a duplicate `event_id`.** It is deterministic over `(alert_id, revision)` on Entur's side,
   so a decision Entur re-derives mints the same id and your inbox recognises it.
2. **Drop a revision not _strictly_ newer** than the highest applied for that `alert_id`.
3. **Never dispatch a `predicted` revision for a train already `confirmed`, whatever its revision
   number.** Rule 2 catches a lower-numbered forecast; rule 3 exists for the one rule 2 admits — a
   forecast numbered *higher* than the confirmation. Entur never mints `confirmed → predicted`, so
   such an event is a bug or a forgery, and presenting it would tell a traveller that a train we know
   is cancelled might merely be late.

Rule 3 tracks the **standing** phase, not "was this ever confirmed". A cancelled train can be
reinstated, Entur mints a `withdrawn` for it, and a fresh forecast afterwards is a real transition;
reading rule 3 as "ever" would silence every alert about that train for the rest of the day.

`decideSuppression` is pure — no I/O, no clock, no Firebase — so the same rules run in a Firestore
transaction, in a generic receiver and in a unit test with a literal for prior state.

## Signatures

[Standard Webhooks](https://www.standardwebhooks.com/): `HMAC-SHA256` over
`webhook-id . webhook-timestamp . body`, base64, in a `webhook-signature` header carrying one
`v1,<sig>` entry **per live generation**, space-separated. Accept a match on any entry — that is what
makes each generation independently revocable, and revocation is removing a secret from your array.

Three things the verifier here does that a naive one does not:

- **It signs over bytes.** Never over a re-serialized object, never over anything measured in
  JavaScript string units. Entur's published `norwegian-body` vector disagrees about its own length
  between UTF-8 (400) and UTF-16 (386) precisely to catch this, and `.length` in JavaScript is the
  UTF-16 count.
- **The clock is injectable**, so a fixed vector can be verified. The `standardwebhooks` package's
  own `verify()` reads `Date.now()` and takes no clock, so it can only ever tell you that a recorded
  vector is stale.
- **A failure is a reason, not an exception.** A stale timestamp, a missing header and a wrong key
  are three different problems and an integrator needs to know which.

The five-minute replay window is why Entur re-signs every attempt under a fresh timestamp while
keeping the `webhook-id` stable: a retry reusing the first attempt's timestamp would land outside the
window, and a *correct* receiver would drop Entur's own retry as an attack.

## The envelope

`parseEvent` takes the **bytes**, refuses a `schema_version` it does not know, refuses any top-level
field the schema does not declare, requires every field the schema declares required, and checks
value kinds. The types in `src/contracts.generated.ts` are generated from
`contracts/event.schema.json` and `contracts/subscription.schema.json` — run `npm run generate` after
changing either, and `npm run generate:check` fails on drift.

Those two schemas plus `contracts/signing-vectors.json` are byte-identical copies of the authored
files in the smart-avvik repository, so the package is self-contained when vendored; a test asserts the
copies have not drifted when the repository is present.

The unknown-field refusal matters in both directions. Entur's store legitimately holds three fields
it must never put on the wire — `client_id` and `destination_id` (its own routing) and `expires_at`
(a retention decision that is *not* validity) — and its sender projects onto an allow-list for that
reason. Refusing them here is what turns a regression there into a test failure rather than a
production incident on every event.

## Status codes

Entur retries `408`, `429` and `5xx`; every other `4xx` is terminal for that event. `receive()`
returns the status to answer with:

| Status | Meaning |
|---|---|
| `200` | Accepted, or a duplicate already accepted. Both are success. |
| `400` | Not an envelope this build knows. |
| `401` | Bad signature, stale timestamp, or no secret for this destination. One status for all three, so the endpoint is not an oracle. |
| `413` | Over the 64 KiB ceiling — checked before the body is read, before any MAC, before any store call. |
| `503` | Your store was unavailable. **The only answer that asks Entur to retry.** |

## Validity and expiry

`valid_until` is the earlier of Entur's 60-minute horizon and the train's departure, and it bounds
*your* dispatch window: past it the event is expired rather than failed. The subscription's own
lifetime is **not** one of those terms — Entur enforces that at dispatch by re-reading the live
subscription — so an event may carry a `valid_until` that outlives its subscription by up to an hour.
Your `eligibility` hook is what closes that gap.

`process()` also applies a **processing cutoff** of 15 minutes from acceptance
(`DEFAULT_PROCESSING_CUTOFF_MS`, overridable per call). With a retry-enabled trigger a thrown error
is redelivered for up to 24 hours, which is right for a store outage and wrong for a bug; the cutoff
abandons a permanently failing record while it is still cheap.

**Know what that number costs.** It bounds a *bug*, and it does not distinguish a bug from an
outage: a store outage lasting longer than fifteen minutes expires every event accepted during it,
including ones still carrying forty minutes of `valid_until`. Fifteen minutes is a deliberate choice
rather than a requirement — `valid_until` already caps at sixty, which is itself 24x under the
trigger's window — so raise it if your store's realistic outage profile says so, and lower it if your
dispatch path is the thing you distrust.

## Test events

R15's `test` marker rides **inside the signed body**, so a receiver can refuse it before native
dispatch and no proxy can strip it. `process()` never calls `recipients` for a test event. Supplying
`testRecipients` is how you opt in; without it a test event is suppressed entirely. An *expired* test
event reaches neither map: the staleness cut runs at entry, ahead of the `test` marker.

## Development

```sh
npm test              # node --test over the .ts sources, no build step
npm run verify:dist   # build the declared shape and load it as a client would
npm run typecheck
npm run generate      # regenerate src/contracts.generated.ts from contracts/
```

`npm test` runs the sources directly through Node's type stripping. **That is a test convenience, not
the shape you consume**: the declared consumption shape is compiled **CommonJS plus type
declarations** on **Node 24**, which `npm run build` produces and `npm run verify:dist` loads and
exercises — including the quickstart above.

One package.json detail that looks like an omission and is not: there is **no `type` field**. Node's
syntax detection then loads a `.ts` source carrying `import` as ESM, which `node --test` needs, while
treating `dist/*.js` as CommonJS, which the published artifact needs. An explicit `"commonjs"` —
which `npm install` will add if you let it — disables the detection and every source import fails at
runtime; an explicit `"module"` breaks the CommonJS emit instead.
