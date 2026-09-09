# Building the BFF side — the vendored TypeScript packages

Two packages ship from this repository so that integrating is *configuring an adapter* rather than
implementing a protocol. **The packages' own READMEs are the install guide**, and this page does not
repeat them:

| package | README | what it is |
|---|---|---|
| `integrations/notifications-client/` | [`README.md`](../../integrations/notifications-client/README.md) | the typed client, the signature verifier, the inbox contract, the three suppression rules, and a **generic `node:http` receiver with no Firebase dependency** |
| `integrations/firebase-notifications/` | [`README.md`](../../integrations/firebase-notifications/README.md) | the Firestore-backed inbox, ledger and suppression log, the Cloud Functions receiver, its security rules and a recovery sweep |

Start there. The wire contract those packages implement is `notification-api.md` beside this file;
the operator's side is `../operations/partner-notifications.md`.

This page carries the things a reader of either README would still not know: what the packages
deliberately do **not** guarantee, the two install steps that are required rather than advisory, and
three traps that were found by building this and are not visible from the outside.

---

## The packages are vendored, not published

Copy both directories into the partner's repository, **keeping them as siblings** — the Firebase
package resolves the client through a `file:../notifications-client` dependency. Publishing to a
registry is deferred work, so a partner upgrades by re-copying, and this repository is the source of
truth for both.

Declared consumption shape (KTD11): **compiled CommonJS plus `.d.ts`, on Node 24**
(`engines.node: ">=24"`). `npm run build` produces it and `npm run verify:dist` builds and then loads the
artifact through `require()` — checking the exported symbols, `main`/`types` resolution, the `.d.ts`
files, the CommonJS emit, and the README quickstart. It is a *packaging* check: the wire and policy
suites run against the `.ts` sources under `npm test` and are not re-run against `dist`. Testing
imports `.ts` directly under Node's type stripping, which is a test convenience and **not** the
partner's contract.

---

## What is guaranteed, and what is not

**One *decision*, at-least-once *dispatch*.** The ledger makes exactly one suppression decision per
event, atomically, and replays it verbatim on re-entry. It does **not** make dispatch exactly-once:
two genuinely concurrent workers on one record can both dispatch, and a crash between the dispatch
and the settle re-dispatches.

A lease was deliberately not added. It trades a duplicate for a *lost* notification whenever a
leaseholder dies, the schema already promises at-least-once, and the partner's per-train collapse key
absorbs the duplicate on the device — which is what `/varsel` itself does, one notification tag per
train per registration. Do not blur that distinction when quoting it to a partner: the acceptance
example says "one native decision", and that is exactly what is met.

**The 15-minute processing cutoff cannot tell a bug from an outage.** It bounds a *bug* — a dispatch
that throws on some payload would otherwise be redelivered by the trigger every few minutes for
twenty-four hours, each run a chance of a duplicate about a train that left hours ago. But a **store
outage longer than fifteen minutes expires every event accepted during it**, including ones with
forty minutes of `valid_until` left. It is a product choice rather than a plan requirement, it is
overridable per call (`processingCutoffMs`), and it is stated here so a partner does not discover it
during an incident.

**"413 before buffering" is literal in one receiver and not in the other.** The ceiling itself is
**64 KiB** (`DEFAULT_MAX_BODY_BYTES`) in both — an abuse bound, not a fit bound, since the largest
event the sender can produce is low single-digit kilobytes. What differs is where it can be enforced.
In the generic
`node:http` receiver the socket is destroyed mid-stream: the suite offers a 4 MiB body and asserts
that far less than a quarter of it is ever read off the wire, which is a claim about the socket and
not only about what the handler does with a buffer. In a
**managed Functions runtime the framework has already buffered the body before the handler is
entered**, so the honest claim there is narrower and still worth having: nothing this package does
touches the body, computes a MAC, or writes a document for an over-size request. State which claim
applies where; do not carry the generic receiver's wording into a Functions deployment.

---

## Two install steps that are required, not advisory

Both are in the Firebase README. They are repeated here only because each fails **silently in every
test and only in production**, which is the class of step a reader skims past.

### `firestore.rules` is necessary and *not sufficient*

**Firestore evaluates rules as a union: a deny cannot override an allow.** So merging the shipped
`match` blocks into a ruleset that also carries something like
`match /{document=**} { allow read, write: if request.auth != null; }` leaves the collections wide
open — and that is a *write* path into what a traveller is told, not only a disclosure risk: a forged
`notification_decisions` document suppresses a genuine cancellation notification, and a forged
`notification_inbox` document injects one.

The shipped file ends with its own catch-all denial. **Drop the catch-all when merging into a project
that also serves the partner's app; keep the four blocks above it.** An emulator test in the package
demonstrates the failure.

### The composite index must be deployed

The recovery sweep queries `status == "pending"` with an inequality on `receivedAt`, which **requires
a composite index in production**. The Firestore emulator tracks no composite indexes at all, so every
test passes without it and the partner's first sweep throws `FAILED_PRECONDITION`. Deploying rules
without indexes leaves the trap armed. This is the same trap `terraform/firestore.tf` closes on our
side, one boundary out.

---

## Three traps found by building this

**1. `firebase-functions@7.3.2`'s `retry` is reachable only through the options-object overload.**

```ts
// Correct: retry rides on the options object.
onDocumentCreated({ document: "notification_inbox/{eventId}", retry: true, region }, handler);

// The shape every quickstart shows. It produces __endpoint.eventTrigger.retry === false --
// not an absent field -- so it deploys, runs, and silently drops events on the first store blip.
onDocumentCreated("notification_inbox/{eventId}", handler);
```

The spelling has moved across major versions (`functions.runWith({ failurePolicy: true })` in v1),
and nothing in a type check, a lint or a deploy log distinguishes the two. `test/functionOptions.
test.mjs` asserts it off the generated endpoint, with the naive overload's `false` pinned beside it,
and also pins the installed package version — re-verify before widening that pin.

**2. The verification secret lives in the functions runtime configuration and never in a mobile
bundle.** A client that holds it can forge an event for any commuter the partner serves, and a secret
in a shipped app cannot be rotated. The receiver takes a `secrets(request)` **callback returning
every live generation** — `readonly string[]`, resolved per request, so a rotation needs no redeploy
and one receiver can serve several destinations by keying on the path. Several generations are live
at once because we sign under both during a rotation and a receiver must accept either, and
**revocation is removing one from what that callback returns** — which is why the contract is a list
and not a single string.

**3. `package.json` deliberately declares no `type` field, and `npm install` will add one if
allowed.** With none, Node's syntax detection loads a `.ts` source as ESM — what `node --test` needs —
while treating `dist/*.js` as CommonJS, which the artifact needs. An explicit `"commonjs"` kills the
`.ts` path; `"module"` kills the CommonJS emit. Both were observed. A test pins the omission; do not
"fix" it.

---

## Running the suites

```sh
npm --prefix integrations/notifications-client test          # node --test over the .ts sources
npm --prefix integrations/notifications-client run verify:dist
npm --prefix integrations/firebase-notifications test
npm --prefix integrations/firebase-notifications run verify:dist
npm --prefix integrations/firebase-notifications run test:emulator   # needs Java 21+ and firebase-tools
```

`test/endToEnd.test.mjs` in the Firebase package is the scenario a partner should be able to
reproduce first: a signed synthetic event becomes one native notification, with no real disruption and
no emulator. It runs in the plain `npm test` for that reason — behind `test:emulator` the first thing
a partner runs would need Java.

**The two emulator suites in this repository cannot be started at the same instant.** This package
pins its own Firestore port (8572), websocket port (9151), hub port (4401) and demo project id, which
is what makes the *sequential* case safe — CI runs them one after another, and a Firestore emulator
has been observed holding its port for minutes after its runner exited. It does not make two
simultaneous starts safe: the contention is inside `firebase-tools`' own hub startup. Start one, wait
for it to listen, then start the other.

**The emulator establishes shape and idempotency, not atomicity.** It uses "a simple locking
approach" and mirrors neither production concurrency nor its indexes. Atomicity is argued from the
transaction contract; index coverage is a static check against the **package's own**
`integrations/firebase-notifications/firestore.indexes.json` — one entry, for the sweep's query, and
a different file from the repo-root `firestore.indexes.json`, which declares our six `partner_*`
indexes. It runs in both directions: a query with no index fails, and an index no query needs fails
too, because a stale index is paid for on every write to that collection forever.
