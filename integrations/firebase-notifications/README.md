# Entur journey notification webhooks — Firebase receiver

A Firestore-backed inbox, alert ledger, Cloud Functions receiver, security rules and recovery sweep
for Entur's client journey-notification channel. It builds on
[`@entur/smart-avvik-notifications-client`](../notifications-client/README.md), which holds the typed
envelope, the signature verifier and the suppression policy and has no Firebase dependency.

Integration should be **configuring an adapter, not implementing a protocol**. Signature
verification, replay-window handling, duplicate suppression and revision ordering are supplied. What
you write is recipient lookup, eligibility and presentation.

---

## What you get

| Piece | What it does |
|---|---|
| `createNotificationReceiver` | The HTTPS handler. Size, signature, envelope, inbox write, `200`. It cannot dispatch a notification. |
| `createInboxWorker` | A **retry-enabled** `onDocumentCreated` trigger over the inbox. Policy, state, eligibility, dispatch. |
| `createFirestoreInbox` | One document per accepted delivery, keyed by `event_id`, created and never overwritten. |
| `createFirestoreLedger` | The three suppression rules and the standing state per `alert_id`, in one transaction. |
| `createFirestoreSuppressionLog` | Every drop, with its rule and reason, so a missing notification has an explanation. |
| `runRecoverySweep` | The net under the trigger: pending records nothing finished. |
| `firestore.rules` | Denies every client SDK on all four collections. **Not optional — see below.** |
| `firestore.indexes.json` | The composite index the sweep's query needs in production. |

## Install

The package is **vendored** for the pilot rather than published. Copy
`integrations/notifications-client/` and `integrations/firebase-notifications/` into your repository,
keeping them as siblings — the Firebase package resolves the client through a `file:../` dependency.

```sh
npm --prefix notifications-client ci
npm --prefix firebase-notifications ci
npm --prefix firebase-notifications run build     # builds both, in the declared shape
```

### 1. Wire the functions

```ts
import { getFirestore } from "firebase-admin/firestore";
import { getMessaging } from "firebase-admin/messaging";
import { initializeApp } from "firebase-admin/app";
import { onRequest } from "firebase-functions/v2/https";
import { defineSecret } from "firebase-functions/params";
import {
  createFirestoreInbox,
  createFirestoreLedger,
  createFirestoreSuppressionLog,
  createInboxWorker,
  createNotificationReceiver,
} from "./firebase-notifications/dist/index.js";

const app = initializeApp();
const db = getFirestore(app);

// The verification secret. It lives in the FUNCTIONS RUNTIME configuration and nowhere else --
// never in your mobile bundle, never in a client-readable Remote Config value. A client that holds
// it can forge an event for any commuter you serve, and a secret shipped inside an app cannot be
// rotated without an app release.
const WEBHOOK_SECRET = defineSecret("ENTUR_WEBHOOK_SECRET");
// During a rotation Entur signs with two generations at once and either must be accepted, so this is
// an array. Add the new secret before the cutover, drop the old one after it: dropping a generation
// IS the revocation.
const PREVIOUS_SECRET = defineSecret("ENTUR_WEBHOOK_SECRET_PREVIOUS");

const adapters = {
  inbox: createFirestoreInbox(db),
  ledger: createFirestoreLedger(db),
  suppressions: createFirestoreSuppressionLog(db),

  // Yours. Never called for a test event.
  async recipients(event) {
    const snapshot = await db
      .collection("commuters")
      .where("enturSubscriptionId", "==", event.subscription_id)
      .get();
    return snapshot.docs.flatMap((doc) => doc.data().fcmTokens ?? []);
  },

  // Optional, and opting in is deliberate: without it every `test: true` event is suppressed
  // before native dispatch. Point it at an isolated map -- your own devices, never real commuters.
  async testRecipients() {
    return process.env.ENTUR_TEST_DEVICE_TOKENS?.split(",") ?? [];
  },

  // Rechecked immediately before dispatch. Entur rechecks the subscription on its side too, but a
  // request already in flight may complete, so the last word is yours.
  async eligibility(event) {
    const doc = await db.collection("commuters").doc(event.external_ref).get();
    if (!doc.exists || doc.data().unsubscribed) {
      return { ok: false, reason: "commuter has unsubscribed" };
    }
    return { ok: true };
  },

  async dispatch({ event, display, recipients }) {
    await getMessaging(app).sendEachForMulticast({
      tokens: recipients,
      notification:
        display === "alert"
          ? { title: event.payload.notification.title, body: event.payload.notification.body }
          : undefined,
      data: { alertId: event.alert_id, phase: event.phase },
      // One tag per train per subscription, so a confirmation REPLACES the forecast it supersedes
      // on the lock screen rather than stacking beneath it.
      android: { collapseKey: event.alert_id },
      apns: { headers: { "apns-collapse-id": event.alert_id.slice(0, 64) } },
    });
  },
};

export const enturNotifications = onRequest(
  { secrets: [WEBHOOK_SECRET, PREVIOUS_SECRET], region: "europe-west1" },
  createNotificationReceiver({
    secrets: () => [WEBHOOK_SECRET.value(), PREVIOUS_SECRET.value()].filter(Boolean),
    adapters,
  }),
);

export const enturNotificationWorker = createInboxWorker(adapters, { region: "europe-west1" });
```

Give Entur the deployed URL of `enturNotifications`. Nothing else about your project is exposed.

### 2. Install the security rules — this step is not optional

Merge `firestore.rules` into your project's rules and deploy them. Without it **any signed-in user of
your own app** can:

- read every event — the Norwegian prose of every commuter's disruption alert, with their origin,
  destination and departure time;
- forge a `notification_decisions` document marking a real event as already settled, which
  **suppresses a genuine cancellation notification** before it is ever dispatched;
- forge a `notification_inbox` document, which fires the trigger and **injects a notification**.

The last two are why this is a write path into what a traveller is told, not only a disclosure risk.

**Firestore evaluates rules as a union: access is granted if _any_ matching rule allows it, and a
deny cannot override an allow.** So copying these four `match` blocks into a ruleset that also
carries something like `match /{document=**} { allow read, write: if request.auth != null; }` leaves
the collections wide open. The blocks are necessary and not sufficient; what makes them sufficient is
that no broader rule in your ruleset allows these paths. Check for one — the emulator suite here
demonstrates the failure (`a permissive rule elsewhere in the ruleset DEFEATS these denials`).

The file ends with a catch-all `match /{document=**}` denying everything. That is right for a project
dedicated to this receiver and wrong for one that also serves your app: **drop the catch-all when
merging, keep the four blocks above it.**

### 3. Deploy the composite index — also not optional

```sh
firebase deploy --only firestore:rules,firestore:indexes
```

The recovery sweep queries `status == "pending"` with an inequality on `receivedAt`, which **requires
a composite index in production**. The Firestore emulator tracks no composite indexes at all, so
every test in this package — and every test you write — passes without it, and the first sweep on
your deployed project throws `FAILED_PRECONDITION`. Deploying the rules without the indexes leaves
that trap armed.

`test/ci.test.mjs` checks the index file against the module's own `QUERY_SPECS` in both directions:
a query with no index fails the build, and an index no query needs fails it too (a stale index is
paid for on every write to that collection, forever).

### 4. Schedule the recovery sweep

```ts
import { onSchedule } from "firebase-functions/v2/scheduler";
import { runRecoverySweep } from "./firebase-notifications/dist/index.js";

export const enturNotificationSweep = onSchedule("every 15 minutes", async () => {
  await runRecoverySweep(adapters);
});
```

Two independent ways a record is left `pending` and nothing says so: the trigger never fired (an
Eventarc incident, a redeploy mid-flight, a worker that exhausted the 24-hour retry window against a
store that was down all night), or the worker died between the decision and the dispatch. The sweep
picks both up; it skips anything younger than five minutes, which the trigger is probably still
working on.

---

## How the receiver decides

```
request ─▶ size ─▶ signature ─▶ envelope ─▶ inbox ─▶ 200
                                                      │
                                                      ▼   (retry-enabled trigger)
   staleness ─▶ duplicate? ─▶ newer revision? ─▶ confirmed already? ─▶ eligible? ─▶ dispatch
```

**The request handler never dispatches anything.** A Cloud Function that has returned its response
cannot access the CPU; work left unawaited afterwards may not run at all, or may resume inside a
later request where it would see that request's context. Persisting first and letting a retry-enabled
trigger do the work is the only shape that both acknowledges promptly and does the work reliably.

**The three suppression rules** — supplied, keyed on `alert_id`:

1. drop a duplicate `event_id`;
2. drop a revision not **strictly** newer than the highest applied for that `alert_id`;
3. never dispatch a `predicted` revision for a train already `confirmed`, **whatever its revision
   number** — rule 2 already catches a lower-numbered forecast, so rule 3 exists for the one rule 2
   admits.

`alert_id`'s grain is **(subscription, train)**, not the train. You serve many commuters through one
receiver, and a per-train key would make rule 2 suppress the second commuter's notification about the
same disrupted train.

**A confirmation defaults to `silent` display.** The traveller was already alerted by the forecast it
replaces; presenting it as a second alarm charges them twice for one disruption. Override with
`adapters.display` if your app wants otherwise.

**Delivery is at-least-once with no ordering guarantee**, and this package makes one *decision* per
event rather than one *dispatch*: a crash between the dispatch and the settle re-dispatches. That is
deliberate — the alternative trades a duplicate for a lost notification — and a per-train collapse
key on the device absorbs it.

## Status codes this receiver returns

Entur retries `408`, `429` and `5xx`; every other `4xx` is terminal for that event; `410` pauses your
destination until an operator resumes it.

| Status | When |
|---|---|
| `200` | Accepted, or a duplicate of an event already accepted. Both are success. |
| `400` | The body is not an envelope this build knows: a `schema_version` it does not recognise, a field the schema does not declare, a missing required field. |
| `401` | The signature does not verify, the timestamp is outside the five-minute replay window, or no secret is configured for this destination. Deliberately one status for all three, so the endpoint is not an oracle for which destinations exist. |
| `413` | Larger than the 64 KiB ceiling. Nothing is read, verified or stored. |
| `503` | The store was unavailable. **Retry this one** — the event is still Entur's to deliver. |

## Development

```sh
npm test              # node --test over the .ts sources, no build step
npm run test:emulator # the Firestore emulator suite, including the rules deny assertions
npm run verify:dist   # build the declared shape and load it as a client would
npm run typecheck
```

`npm test` runs the sources directly through Node's type stripping. **That is a test convenience and
not the shape you deploy**: what a Cloud Functions runtime loads is compiled CommonJS plus type
declarations on **Node 24**, which is what `npm run build` produces and `npm run verify:dist`
exercises.

### Emulator ports, and what separating them does and does not buy

This package's `firebase.json` pins **three** ports, and two of them are invisible in the
smart-avvik repository's root config:

| | root | here | why it is easy to miss |
|---|---|---|---|
| `firestore.port` | 8571 | **8572** | the only one either file names |
| `hub.port` | 4400 | **4401** | the root file does not name it at all; 4400 is the CLI default |
| `firestore.websocketPort` | 9150 | **9151** | named in neither. The CLI allocates it separately from `firestore.port`, defaults it to 9150 for every project, and shifts it only by probing for a free port at start-up — a race |

The emulator also runs under its **own demo project id** (`demo-smartavvikclient`). The CLI keys its hub
locator file on the project id and warns that two instances of one project "may result in unexpected
behavior"; a distinct id answers that warning rather than tolerating it.

**What that buys, measured.** A sequential run — which is what CI does — is clean even when the
previous step's emulator has not finished tearing down; a Firestore emulator was observed holding
its port for minutes after its runner exited, and the next run then failed with `Port 8572 is not
open`. And you can run this suite while a repo-root emulator is already up: 6 of 6 runs clean, exit
0, 23/23, with the root suite live throughout.

**What it does not buy.** Two emulator suites started at the *same instant* still fail — 3 of 3
rounds measured. The Firebase CLI prints its generic `Error: An unexpected error has occurred.`
before running the wrapped script, `emulators:exec` exits 2, and its error-path teardown can kill
the script mid-run; a probe found the hub listening on 4401 for 6 samples out of 40. The contention
is inside firebase-tools' own hub startup, not in a port this config can name. **Start one, wait for
it to listen, then start the other.**

Two related properties this suite relies on, both learned the hard way:

- **The two emulator test files run serially** (`--test-concurrency=1`). `node --test` parallelises
  test *files* by default, and the emulator runs in `--single_project_mode`, under which a distinct
  project id does **not** give a distinct database — both files write into one data space, so one
  file's truncation races the other's writes. The symptom is a lie: a test asserting one decision
  document finds four, which reads as a ledger bug rather than a fixture bug.
- **The rules suite deletes `FIREBASE_EMULATOR_HUB` before initialising.**
  `@firebase/rules-unit-testing`'s `initializeTestEnvironment` calls `discoverEmulators(hub)`
  *unconditionally* whenever that variable is set — the explicit `firestore: { host, port }` is
  consulted only afterwards — so a suite that needs nothing from the hub fails outright when the hub
  is unavailable, with `connect ECONNREFUSED` out of `before()` and every rules assertion marked
  failed for a reason having nothing to do with rules.

The emulator suite runs against the project id `demo-smartavvikclient`. A `demo-` prefix is what makes it
credential-free: the SDKs skip Application Default Credentials entirely and the project can reach no
real GCP service. Never point it at a real project id.
