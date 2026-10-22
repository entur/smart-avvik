# The poller lease — runbook

**Date:** 2026-10-13 · **Plan:** `docs/plans/2026-10-13-001-feat-zero-downtime-deploy-autoscaling-plan.md`
(U1-U9; the KTD numbers below are that plan's) · **Alerts:** rules 1a and 1b in
`docs/observability/02-alert-rules-2026-09-11.md`.

Every API pod in dev and prd serves every route, and exactly one of them — the pod holding the
**poller lease** — runs the poller cycle and every tick-time writer. This file is how to read who
leads, what to do when nobody does or two do, and how an environment moves from one Recreate pod to
a rolling, autoscaled Deployment and back. tst has no lease and never will (see the last section).

## What the lease is

One Firestore document, **`poller_lease/current`** in the environment's `notifications` database,
with `holder`, `token`, `acquired_at`, `held_until` and `updated_at`. It is not in
`notification_store.COLLECTIONS` and has **no TTL field config**: `held_until` is a deadline the
store bodies compare, and a TTL could only delete the live copy (`togvarsel_rounds/current`'s
reason). The shape is additive-only across releases, since two binaries share it during every
rollout: every write keeps fields it does not know, every reader treats new ones as optional
(KTD13).

- **Acquire** is one transaction: read the document, refuse while another holder's `held_until` is
  in the future, else write this holder, a fresh token and `held_until`. Two followers reaching a
  lapsed lease on the same tick cannot both win. The same holder re-acquiring its own lease is
  allowed, so a lost acknowledgement cannot lock a pod out.
- **Renew** matches holder AND token, every **30 s** on its own timer thread (never on the
  heartbeat), only while the poller's heartbeat is younger than **250 s**, at most one in flight,
  each waited on for **20 s**. `held_until` is always **the last heartbeat plus 300 s**, never the
  renewal instant plus 300 s — so a wedged leader loses the lease exactly 300 s after it last
  finished a step, however many renewals its timer still makes.
- **The local deadline** is that heartbeat plus 300 s minus a **30 s** margin (clock skew plus the
  store's wait), on the monotonic clock. A leader stops believing it leads 30 s before a follower
  may take over.
- **Release** matches the holder alone (so it works after a lost acknowledgement), clears the
  holder and sets `held_until` to now. It **never deletes** the document, and writes nothing when the
  holder differs (`not_held`).
- **The holder id** is the pod name plus a per-process suffix (`<pod>-<6 hex>`), so a container
  restarted inside the same pod is a new holder and cannot renew its predecessor's tenure.

The numbers live in `disruptions/api/poller_lease.py` (`TTL_S`, `MARGIN_S`, `RENEW_EVERY_S`,
`RENEW_WHILE_FRESH_S`, `RENEW_WAIT_S`, `LONGEST_BEATLESS_WAIT_S`, `GRACE_S`, `PRESTOP_S`,
`SHUTDOWN_MARGIN_S`) and `notification_store.POLLER_LEASE_S`; `test_poller_lease` pins the
inequalities between them and `test_notification_contract` pins `GRACE_S`/`PRESTOP_S` to the Helm
values.

**What the lease gates — the whole cycle and every tick writer, not only the push** (KTD2):
`main._rebuild_all` (≈0.9 GB of `ML.PREDICT` and six `CREATE OR REPLACE` tables per rebuild),
`togvarsel.refresh`, `togvarsel_notify.evaluate`, `togvarsel_outcomes.refresh`,
`decisions.check_labelling_freshness`, the state export, and Gemini translation of operator text
(`situation_english`; every pod SERVES the leader's translations, read from Firestore's
`situation_translations` on each follower tick -- see "What every pod does"). The check (`main._fence`) runs before every `_phase`, between the two queries
of a phase that issues two, before every tick writer, before `deliver_to_devices` and per item inside
its fan-out, and before a rush round's delivery. A demoted or stopping leader stops at the next
boundary and logs one `poller_lease` record naming it.

**What every pod does, leader or not** (`main.follow_last_build`, once in the lifespan before the
port binds and then on every follower tick; the mechanism is `disruptions/api/poller_follow.py`, the
list of what is followed stays in `main`): load the prediction caches, the «I dag» timetable spine
and actuals, the impact lookup, the SVV baselines, the stop picker list and its aliases, line modes,
and the rush-forecast state and station set (`main._rush_hydrate`). Each cache is stamped with its
source tables' **modified time** — the leader's too — so `refreshed_at` and `as_of` agree across pods
for one table generation, and a follower's quiet tick costs only `get_table` metadata reads, which
BigQuery does not bill. There is **no age bound** on these loads (the 2026-09-25 warm start's
10-minute one is gone): a live leader on a quiet night legitimately leaves tables
hours old. Three things stay leader-only on purpose:

- **Stop aliases are published on a follower without healing stored registrations**
  (`refresh_varsel_stops(heal_registrations=False)`); only the leader's cycle rewrites a registration
  onto a canonical stop.
- **The mirrors** (subscription and standing-subject) are the leader's: seeded synchronously on
  acquisition, reset on loss, never seeded on a follower. Request paths read the store on every pod,
  the leader included (`stored_journey` for the delete route's ownership check, the lists), so a
  journey registered on another pod deletes cleanly on the leader.
- **Wording and alternatives** (`varsel._messages`, `_alternatives`) are per pod and not shared
  (KTD11): a follower's `/varsel` card shows the SX summary and no alternative; journey-alerts on a
  follower words through that pod's own PULL ledger.

**The leader's pre-send check** (`varsel_state.StoreRegistrations.may_send`): one store read per
push, outside `_state_lock`. It refuses a journey that is gone and a push whose situations are all
already marked notified — the two things the leader's one-cycle-late mirror cannot know about
another pod's writes or an interim leader's pushes. It does **not** refuse on the train tag alone
when that tag was marked under a DIFFERENT situation: that is a replacement the stale mirror did not
recognise (typically an ET cancellation confirming the interim leader's forecast), and refusing it
would withhold the confirmation until the six-hourly re-seed. Sending it costs the «Bekreftet:»
prefix and one slot of the day's cap.

## Acquisition, loss and shutdown (KTD14)

**On acquisition**, on the poller thread, before the first cycle of a tenure: reset and seed both
mirrors (beating between pages), reload the rush round markers, hydrate the rush state, force the
next iteration to rebuild whatever the ingestion stamp says, then set `kix_poller_leader` to 1 and
log `acquired`. If any step raises, the lease is released, the gauge cleared, and the pod stays a
follower for **5 ticks** before trying again (`acquire_failed`), so a partial store outage does not
spin every pod through acquire and release each tick. **A failed rush hydrate does not count**:
`_rush_hydrate` logs and never raises, deliberately — otherwise an unreadable `togvarsel_issued`
would block every election. The new leader then serves stale rush state until its own
`togvarsel.refresh` reads the record back. The first cycle is cold (the ET gates are not armed), as
after every start.

**On loss** (the renewal answered `lost`, or the local deadline passed): the next tick clears the
gauge, logs `lost`, waits for the emit pass to drain (bounded by the shutdown deadline) and resets
both mirrors.

**On SIGTERM**, one deadline of **255 s** (270 s grace − 5 s preStop − 10 s margin): set stop, let the
poller thread exit at its next boundary, join it up to the deadline, stop the renewal timer, wait for
the emit pass lock up to what is left, then **release the lease unless the thread is still alive**,
then clear the gauge. A thread still alive at the deadline is mid-phase — a BigQuery job may still
replace a table — so the lease is left to lapse (`release_skipped`) and the successor waits out the
TTL. A pass lock still held at the deadline is released anyway (`drain_timeout`): at most
`_EMIT_WORKERS` abandoned commits can land after it, and each is idempotent. A SIGKILL that lands
anyway leaves the lease unreleased: the successor pays the TTL plus a tick plus a cold cycle.

What the TTL cannot fence is covered elsewhere: browser dedup is store-exact through the pre-send
check; the successor's first cycle runs at least ~50 s of BigQuery before its own sends while the
old leader's in-flight sends end within one push timeout; webhook writes are fenced by their
transactional re-reads and the delivery claim tokens. **A rush round whose fan-out was already in
flight when the lease lapsed may be pushed again by the successor** — the round marker is persisted
after delivery, the same window a crash already has (KTD8).

## Reading who leads

Three sources, and they should agree.

1. **`/ready`'s body**, `{"status": "ready", "role": "leader"|"follower", "lease_age_s": …,
   "version": …}`. `version` is the commit the pod's image was built from (the Dockerfile's
   `GIT_SHA` build arg, which `ci.yml` sets; null in a build without it). `lease_age_s` is seconds
   since the heartbeat the lease was last written with. A renewal carries the latest heartbeat, so it is normally under a minute, can legitimately reach a few minutes
   inside one long BigQuery call (no beat for up to 240 s), and a pod stops answering `leader` at
   270; one that sits high between cycles means renewals are not landing. Through the public host it answers from
   whichever pod the load balancer picks; to see every pod, port-forward each one (the image is
   distroless — there is no shell or curl inside it):
   ```sh
   kubectl -n kix-avvik get pods      # the API pods, not kix-avvik-worker-*
   kubectl -n kix-avvik port-forward pod/<pod> 18080:8080   # then curl -s localhost:18080/ready
   ```
   503 with `reason: predictions_not_loaded` or `plan_not_loaded` is a pod still loading, not a
   lease problem. `/ready` and `/health` are both muted from the access log (`logging_setup`), so
   probing generates no log lines. The deploy smoke (`cd.yml`, dev and prd) waits up to 6 minutes
   for one answer naming `role: leader` AND the deployed build's `version` (the first 7 hex of the
   commit: a dispatched image's `-SHA<7>` tag suffix, else the run's own commit). "Any leader" is
   not enough from step B: the terminating old pod can still hold the lease and answer through the
   public host, which would pass a deploy whose new pod never acquires. Under step A (Recreate) no
   old pod exists, so the new pod acquires on its first tick, well inside the window. Dispatching
   an image built before the `version` field existed (a rollback past this change) fails this step
   by design; the deploy itself has already happened.
2. **The `event="poller_lease"` records**, in the **cluster** project (`ent-kub-dev`/`ent-kub-prd`,
   namespace `kix-avvik`), with `result` and `holder`:

   | `result` | Meaning |
   |---|---|
   | `acquired` | This pod leads from now; `on_acquire` succeeded |
   | `refused` | Another holder's lease is live (`held_by` names it); logged once per holder seen |
   | `lost` | The renewal answered `lost` or the local deadline passed; this pod follows |
   | `released` / `not_held` | Shutdown handed the lease back / it was no longer ours, nothing written |
   | `release_failed` | The release call raised; the successor waits out the TTL |
   | `release_skipped` | Shutdown deadline hit with the poller thread mid-phase; left to lapse |
   | `drain_timeout` | The emit pass was still running at the deadline; released or reset anyway |
   | `acquire_failed` | `on_acquire` raised; released, following for 5 ticks |
   | `error` | The acquire call itself raised (a store failure); following this tick |
   | `renew_error` | One renewal raised; the local deadline stands |
   | `lease_lost` / `stopping` | A fenced cycle or tick writer stopped at `phase` |

   A healthy rolling deploy reads, in order: the old pod's `released`, then the new pod's
   `acquired` within one tick. A Recreate deploy from a lease-unaware binary shows `acquired` with no
   `released` before it, since the old binary had no lease.
3. **`kix_poller_leader`**, 1 on the leader and 0 everywhere else — followers, the worker pod and
   every tst pod export it as 0 (an unset `prometheus_client` gauge is 0, not absent; the metric's
   HELP text says "absent" and is wrong in the same way `WORKER_LAST_TICK`'s was).
   `sum(kix_poller_leader{kubernetes_namespace="kix-avvik", prometheus_group="kub-ent-<env>-001"})`
   is the number of leaders; the dashboard draws `count()` beside it, the number of pods exporting.
   `kix_poller_last_cycle_timestamp_seconds` is leader-only by construction.

The document itself can be read with `notification_store.read_poller_lease()` from a developer
shell with ADC; never write it by hand.

## Operations

**Force a handoff: delete the leader pod.** `kubectl -n kix-avvik delete pod <leader>` sends SIGTERM,
the pod releases at the end of its shutdown sequence, and a follower acquires at its next tick. At
one replica (dev) the Deployment's replacement pod acquires once it is Ready, and nothing serves
until then (~45 s measured). Use `--grace-period=1` only to rehearse a crash: the lease is then
unreleased and the successor waits out the TTL from the dead leader's LAST HEARTBEAT plus a tick.
Measured in dev on 2026-10-20 that was 4 min 55 s, and rule 1a went Pending for exactly its 5m and
cleared without firing, so "fires once" is borderline rather than certain.

**Never delete or edit the lease document.** A deleted document is a free lease: a follower acquires
at its next tick while the old leader keeps believing it leads until its next renewal answers `lost`
— up to ~35 s (one renewal period plus the call) of two pods running phases. Deleting the pod is the
same handoff without that overlap. An expired lease needs no cleanup either: it is acquirable as it
stands.

**A Firestore outage stops the cycle in dev and prd.** No pod can renew, so the leader stops
believing at its last successful renewal's heartbeat plus 270 s and no follower can acquire. Every
pod keeps serving the last-built tables indefinitely, with `refreshed_at` ageing honestly; no
rebuild, no push, no emit, no rush round. Rule 1a fires after ~5 minutes. Pods that restart during
the outage cannot start at all (the store backend already fails the lifespan without the database),
so do not delete pods to "fix" it. Recovery is automatic once the store answers: the next tick
acquires. Before the lease this cost only the mirror sync; it is the price of having one writer.

**Rule 1b firing (two leaders).** Find the stalled pod: its own `kix_poller_leader` series (per pod,
without the `sum()`) reads 1 while its `/ready` answers `role: follower` — `/ready` asks the lease's
local deadline, which has passed, while the gauge is cleared only at that pod's next tick — and its
last `poller_lease` record predates the other pod's `acquired`. Its thread is stuck mid-phase. Run the duplicate check over `event="push"` for the window (at most one `result="sent"` per journey
and situation, grouped by pod). If the stall persists, delete that pod. Never touch the document.

**Expected alert shapes during a handoff.** Rule 1 stays quiet (every pod beats). Rule 2's `max()`
can dip to the followers' 0 or an older value until the new leader's forced first cycle completes —
inside its 15m pending window. Rule 1a fires only for a cold handoff (TTL plus a tick > 5m). Rule 5
may fire for an outgoing pod held mid-query for most of the 270 s grace while a Ready pod serves
beside it.

## Deploy contracts (KTD13)

Every rolling deploy runs two binaries side by side for a minute or more, so:

- **Additive columns only, for one release**, on every table a follower loads. A new follower loads
  tables the OLD leader built; a column added to a `*_today` table and read in the same release
  stalls the rollout (the new pod never becomes Ready) and `helm --wait` rolls it back. Ship the
  tolerant read one release ahead of the column.
- **The lease document is additive-only** (above).
- **A browser can fetch `index.html` from one pod and a hashed asset from another** during the
  rollout minute (no session affinity). A 404 on an asset resolves on reload; `_VersionedStaticFiles`
  already sends `no-cache` on `index.html`.
- **A rollout at two or three replicas may hop the lease through an old-template follower**, costing
  one cold cycle per hop (~20 GB of ET scans, ~$0.15, two to three minutes of no pushes). Accepted.

## Per-pod allowances (KTD12)

Every per-process allowance stays per process, so the replica ceiling (3) is the multiplier bound:
three pods are three times each.

- the PULL wording ledger for journey-alerts (`notification_wording.PULL`: 120 / 2 000 / 10 000
  wordings per minute, hour and day);
- the Journey Planner fetch caps (`alternatives`), the request limiters (journey-alerts'
  `MAX_PER_MIN` and `PER_PAIR_PER_MIN`, the other public limiters), the `/meldinger` replay budget;
- the feedback capture limits (60 per minute, 12 per context per minute, 2 000 per day — an
  effective **6 000 a day** at three pods; `data/feedback/README.md`).

Gemini translation of operator text is leader-only, so followers do not translate the same
situations; they read the leader's from `situation_translations` instead (the shared translation
store, 2026-10-22), seeded on startup and on taking the lease, synced on every follower tick. An ingress-wide budget would need the platform; nothing here provides one.

**`/rushvarsel-api/outlook` now reads the device's journeys from the store on every call** (it read
process memory before), so a public outlook read is one store query and can trigger the weekly
activity renewal write, as the `/varsel` journey list already does. Watch the Firestore read bill if
the panel's poll rate rises.

## Feedback contexts in the store

Under `VARSEL_STATE_BACKEND=store` (the lease's own gate, never `notification_store.enabled()`),
each card render's feedback snapshot is a document in `feedback_contexts` so a first tap resolves on
whichever pod receives it (KTD10):

- the snapshot is stored as **one canonical JSON string** (Firestore refuses nested arrays, and a
  string round-trips byte for byte); the document names its `device_id`, or none for the rush card;
- **expiry is fixed at mint, not sliding**: `expires_at` is 15 minutes after the render and doubles
  as the TTL field, so physical deletion follows within Firestore's ~24 h lag, and every read tests
  expiry itself. A memory-cached id is reused for the same fingerprint only while more than 7.5
  minutes remain, so **a rush card's context id rotates about every 7.5 minutes** under the store
  backend;
- «Slett alt» deletes the device's contexts inside the device's own erase transaction;
- a first tap is **claimed** on the document per (context, response id), so a submit retried
  against two pods captures once. A claim failure **fails open** (the tap is admitted: a store blip
  must not lose a rating), and a **release** gives the claim back when persisting or capturing fails,
  so the browser's retry is not refused;
- a store write that fails at mint falls back to memory on that pod only; another pod then answers
  that id as expired.

## Rollout: two deploys per environment (KTD15)

The pod a rolling deploy replaces is the one running the previous spec. If its binary has no lease
code it polls and pushes unconditionally beside the new leader; if its spec has no grace it is killed
after Kubernetes' 30 s default, mid-phase. So the switch is two deploys per environment.

**Step A** (the zero-downtime branch itself, `feat/zero-downtime-deploy`): the lease code, `/ready` as the
readiness path, `terminationGracePeriodSeconds: 270` in the base `values.yaml`, the 5 s preStop in
each of the **three API environment files** (not the base: the base is shared with the worker
release, which is behind no Service), and `deployment.forceReplicas: 1` **moved out of the base into
each environment file**. Every environment is still Recreate, one replica, no HPA. The base file
carries the rolling and HPA keys (`maxUnavailable: "0%"` as a string, HPA 1..3 on 80 % CPU, one pod
per 60 s up, 600 s stable before down), inert while an environment file forces one replica. prd's file
raises the floor to **2..3 with a 50 % PDB** (`deployment.minReplicas`, `deployment.minAvailable`
and `hpa.spec.minReplicas`, staged beside its `forceReplicas: 1` and equally inert until step B).

**Step B**, one values-only change per environment, made only after that environment's step A is
verified live, and its shape differs per environment:

- **dev** (done 2026-10-20): **the WHOLE `deployment:` block** was deleted (the key, its comment and
  `forceReplicas: 1`). Not the line alone: a `deployment:` key left with no keys is null, Helm reads
  a null override as "delete the key", the base file's whole `deployment` map goes with it, and the
  chart's startup-cpu-boost template fails the render on a nil pointer. dev rolls at **1..3 pods,
  PDB 0%** — the golden path's accepted single-pod dev.
- **prd** (on `main` 2026-10-22; live from the first prd dispatch after it): delete **the
  `forceReplicas: 1` line and its comment only**. prd's step A had been live since 2026-10-20 13:10
  (one Recreate pod, one `acquired`, no handoff) and its Terraform was applied on 2026-10-22, two
  days AFTER step A — the runbook's order (Terraform first) was missed on that dispatch, which left
  `feedback_contexts` without its TTL config in between. The block keeps
  `minReplicas: 2` and `minAvailable: "50%"`, so it is not null, and `hpa.spec.minReplicas: 2` sits
  beside it (the base `hpa.spec` states its own minimum of 1, which the chart's `deployment.minReplicas`
  does not reach). prd rolls at **2..3 pods, PDB 50 %** (one pod of two), which is what Entur's
  production hardening asks for (`entur/ai` architecture guide: more than one pod in prd, PDB 50 %).
  The reason is measured, not formal: at one pod a node drain or a cluster-autoscaler scale-down
  evicts the only pod before its replacement is Ready, and dev lost **41-52 s of 503/404 to each of
  three such evictions in 26 hours** (2026-10-19 12:32, 2026-10-20 13:49 and 14:33, the last a
  `ScaleDown` of the node it ran on). Rolling deploys never had that gap; evictions are what a second
  pod buys out. The cost is a permanent follower: one more pod at prd's 1.0 CPU and 2 GiB, and its
  follower loads (step 5's measurement).

`helm/kix-avvik/test/api_render.sh` (CI's `helm-render`, gating the image) renders each environment's
step B by deleting the `forceReplicas` line and asserts its floor (`ROLLING_FLOOR`: dev `1 0%`, prd
`2 50%`), so the shape is proven before anyone commits it — and a prd block that lost its floor
fails that render on the nil pointer above.

**A dispatch deploys the values at `main`'s HEAD**, so prd's `forceReplicas` line is deleted on
`main` only after prd's step A is verified live; dev's step B merging does not move prd until prd is
dispatched. If prd's line is ever deleted on `main` before prd's step A, recover with a dispatch from
a branch or tag at the step A commit.

### Pre-deploy audits (before each environment's step B)

- **Live strategy and grace**: `kubectl -n kix-avvik get deploy kix-avvik -o
  jsonpath='{.spec.strategy.type} {.spec.template.spec.terminationGracePeriodSeconds}'` reads
  `Recreate 270`. Anything else means step A is not what is running.
- **Rendered manifests**: `bash helm/kix-avvik/test/api_render.sh` passes on the commit being
  deployed.
- **The Firestore database and its TTL configs** exist for `feedback_contexts` (Terraform; prd's is
  dispatch-only — `gh workflow run terraform.yml -f environment=prd`, and confirm the apply, or prd's
  contexts are never deleted).
- **The reserved `browser` account** exists in the store (the lifespan fails without it).
- **The lease document's state**: one holder, `held_until` moving, matching the one pod's `/ready`.
- **No new loader columns** in this release on any table `follow_last_build` reads.
- **The `poller_cycle` baseline** for the environment (duration, phases, period) from the last day,
  to compare the first rolling cycles against.
- **Rules 1a and 1b exist** in that environment and passed their forced evaluation
  (`02-alert-rules-2026-09-11.md`, checklist step 9).

### Rollout checklist (U9)

1. **dev step A**: push to `main`. Confirm the single dev pod logs `poller_lease` `acquired` (no
   `released` before it — the old binary had no lease), `/ready` answers 200 with `role: leader`
   through a port-forward, one `poller_cycle` per period, and `sum(kix_poller_leader)` is 1. Create
   rules 1a/1b in dev and prove them (alert-rules checklist step 9).
2. **dev step B**: delete dev's `deployment:` block, merge, and watch the first rolling deploy while
   polling the journey-alerts/journey-status route matrix at one request per second **in total**,
   spread over three or more stop pairs (the per-pair limiter is 30 per minute per pod, so one pair
   at 1 rps measures 429s, not the rollout), counting 5xx only. Record the 5xx count (expect 0), the
   handoff time from the old pod's `released` to the new pod's `acquired`, and the first cycle's
   duration.
3. **Handoffs**: delete the leader pod gracefully and record time to `acquired`. Then kill it with
   `--grace-period=1` and record time to `acquired` and the absence of `released`. For both, run the
   duplicate check over `event="push"` (at most one `result="sent"` per journey and situation,
   grouped by pod). Repeat the forced case with a SIGTERM during a slow fan-out (a synthetic slow
   push endpoint in dev). Confirm against the deployed `/varsel` bundle that a retried feedback
   submit reuses its response id, which the claim rests on.
4. **Scale**: load journey-alerts at ~50 rps over three distinct definitions for ten minutes; record
   scale-out time, per-pod 200s and the scale-in order. With three pods, deploy once more and count
   `acquired` records for the rollout.
5. **Follower cost**: read `INFORMATION_SCHEMA.JOBS_BY_PROJECT` for one day — bytes billed by
   follower SELECTs against the leader's own load bytes. A follower reading more than the leader
   loads is a defect to fix before prd.
6. **Watch** `sum(kix_poller_leader)` in dev for a day.
7. **prd**, in the same two steps, after rules 1a/1b exist and are proven in prd:
   `gh workflow run terraform.yml -f environment=prd` and confirm the apply; dispatch `cd.yml` with
   the image in SHORT form (`kix-avvik:main.<date>-SHA<sha>`) and prd's `forceReplicas: 1` still in
   place; confirm `acquired` on the single pod; only then delete prd's `forceReplicas` line (the
   block and its 2-pod floor stay) and dispatch again. Expect the rollout to end with **two** pods,
   one `acquired` and one follower whose `/ready` answers `role: follower`, and
   `count(kix_poller_leader)` at 3 (two API pods plus the worker) beside `sum` at 1. Repeat steps 1,
   3 (graceful delete only: with a follower already Ready it should take one tick, with no serving
   gap) and 6 there.

**Stop and do not promote** if dev showed any `lease_lost` without a cause, two pods emitting
`poller_cycle` in one window, rule 1b firing, a rollout that needed `helm --wait`'s automatic
rollback, or a cycle-driven scale-out — dev's CPU request is 0.5, which puts the leader's peak
minute at the 80 % target; raise it to 1.0 first.

### Rollback

**`forceReplicas: 1` back first, the image second** (dev: the whole `deployment:` block back, as it
was; prd: the line back into its block) — the reverse of the rollout, for the same
reason: a lease-unaware image under RollingUpdate polls and pushes beside the leader. A stalled
rollout is left to `helm --wait`'s own rollback and **never cancelled**. The lease document needs no cleanup: an expired lease is
acquirable, and an older binary ignores it. **Keep the `feedback_contexts` TTL field config** until
a query shows the collection empty — rolling the feedback-context code back with its Terraform
would leave route-bearing documents with no expiry. Under Recreate the 270 s grace lengthens a
deploy's no-pod gap by up to one phase when the old pod is mid-query; that applies to step A and to
every rollback, and is accepted.

## tst has no lease

tst has no Firestore database and no store backend (`VARSEL_STATE_BACKEND` is not `store`), so the
process uses `poller_lease.SoleLeader`: always the leader, never touches a lease, starts no timer,
and keeps the old 5 s shutdown join. `/ready` answers exactly as `/health`, and tst's file states
`/health` as its readiness path anyway. tst keeps `deployment.forceReplicas: 1` **permanently** —
there is nothing to share and no leader to elect — and neither alert rule exists there (its gauge
is 0 forever). Local runs and every hermetic suite take the same path.

## Measurements

**dev, 2026-10-19..20** (U9; still owed: scale-out and scale-in timings, follower BigQuery bytes
against the leader's, the SIGTERM-during-slow-fan-out rehearsal, and the prd repeat):

| What | Measured |
|---|---|
| Step A, one Recreate pod (26 h, 5 handoffs) | `released` → `acquired` 42-49 s each; no `lease_lost`, no overlapping `poller_cycle`; cycle ~59 s mean, unchanged |
| Step B, first rolling deploy | 1 503 in 1 538 requests at 1 rps, at 12:49:06, ~2.5 min after the rollout ended and absent from the app's own logs (an edge 503, not attributable further: no Traefik access logs) |
| Step B handoff | old pod `refused` the new one, released 5 s after SIGTERM; new pod `acquired` 66 s later (its next tick); first cycle 76 s (cold) |
| Graceful delete, one pod | handoff 45 s; **46 s** of 503/404 (no surge on a pod delete) |
| Forced kill (`--grace-period=1`), one pod | no `released`; successor `refused`, then `acquired` 4 min 55 s after the kill; **41 s** of 503/404; rule 1a Pending 5m, did not fire |
| Cluster-autoscaler scale-down, one pod | leader fenced mid-cycle ("Poller stopped before rail_planned_calls"), `released`, successor `acquired` 47 s later; **52 s** of 503/404 |
| Duplicate pushes | none in 15 sent since step A — none of them near a handoff, so this proves little yet |
