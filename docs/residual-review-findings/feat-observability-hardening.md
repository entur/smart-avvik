# Residual review findings — `feat/observability-hardening`

Review: `code-review mode:agent plan:docs/plans/2026-09-11-001-feat-observability-hardening-plan.md base:main high`, 2026-09-11, against `330fb95`. Fifteen findings; twelve applied in `9399f3b`. These three were not, and each says why.

No tracker sink is configured for this repo, so the findings are inlined verbatim rather than linked.

## 1. A timed-out BigQuery job is abandoned, not cancelled

**Severity:** medium · `disruptions/api/main.py:469` · **Not applied: blast radius beyond this branch, and it reopens a settled decision.**

`result(timeout=…)` stops the client waiting; it does not stop the job. A `maybe_refresh_planned_calls` that times out at 240 s leaves the ~8 GB rail (or ~12 GB bus) CTAS running to completion in BigQuery — billed — while `_planned_refreshed_at` is never written, so the next cycle issues an identical scan on top of it. Under on-demand slots that is a positive feedback loop: the second scan adds contention to the slowdown that caused the first timeout.

The same shape has a subtler cost. Before this branch a slow-but-successful 250 s `predict_sql` finished and refreshed the caches; now it raises at 240 s and the whole cycle is abandoned. A timeout that turns a slow cycle into *no* cycle is not obviously better than the wedge it guards against — the crash-loop guard is still right, but the number wants a measurement it has not had.

The reviewer's root-cause fix is a client-level default:

```python
bigquery.Client(default_query_job_config=QueryJobConfig(job_timeout_ms=…))
```

which makes BigQuery itself cancel the job, and would also have covered the two call sites `330fb95` missed and the three per-module constants that now exist (`togvarsel._READ_TIMEOUT_S`, `_REBUILD_TIMEOUT_S`, `togvarsel_notify._STATIONS_TIMEOUT_S`).

**Why not now:** `main.bq_client` is shared with the request path, where `/demo-api` and `fetch_disruptions_as_of` have different latency expectations and an unbounded wait holds an anyio slot rather than the tick. A global job timeout is a change to every query in the app, and per-call-site bounds are a `session-settled:` KTD1 decision. It wants its own change, with the prd `poller_cycle` measurement KTD1 already asks for.

## 2. Inbound `traceparent` decides prd's sampling on a public unauthenticated host

**Severity:** medium · `helm/kix-avvik/env/values-kub-ent-prd.yaml` · **SETTLED 2026-09-11: risk accepted by the product owner; referred to Team Plattform as a shared-ingress concern. No longer blocks flipping tracing on in prd.**

`OTEL_TRACES_SAMPLER: parentbased_traceidratio` honours a remote parent's sampled flag before the ratio applies, and the ASGI instrumentation extracts trace context from request headers. So any caller can send `traceparent: 00-<id>-<id>-01` on every request to `kix-avvik.entur.io` and force 100 % sampling with trace ids they chose — span volume, ingestion cost, and poisoned trace search. An ingress that always stamps `-00` would silently reduce sampling to zero while the values file claims 10 %.

**The obvious fix does not work.** Swapping to a plain `traceidratio` — or to a `ParentBased` whose remote arms are the ratio, which is what the review suggested — closes the always-`-00` case and leaves the attacker case fully open: `TraceIdRatioBased` is deterministic on the trace id, and the trace id is the attacker's. They pick ids that hash into the sampled range.

The real fix is to not accept inbound trace context at the public edge — strip the header in a middleware outside the OTel one, or settle it at the ingress — which is a design decision about whether kix-avvik is ever a child of an upstream Entur trace.

### The decision (2026-09-11)

**Accepted, and referred outward.** The product owner's call: take the risk in this app, and leave it to Team Plattform to fix at the shared ingress if they judge it worth fixing. Stripping `traceparent` there closes it for every Entur service at once, which is the right layer for a header the public can set — and doing it in this app alone would fix one host while the same exposure sits on every other public one.

Both directions were on the table when it was accepted, and the record should be explicit that the *mundane* one is the likelier:

- **`-01` (adversarial):** a caller forces 100 % sampling with trace ids of their choosing — unbounded billed span volume, and trace search populated with ids they picked. Measured against the real SDK: 100.0 % vs the configured 9.6 %.
- **`-00` (accidental, and more likely):** any middlebox, CDN or client library that stamps an unsampled parent takes prd's tracing to **zero** while this file says 10 % and the alert rules assume traces exist. Nothing errors. You would believe you had tracing and have none.

Also recorded, because it was measured while settling this and is easy to assume otherwise: **dev has the same `-00` exposure.** `parentbased_always_on` defaults `remote_parent_not_sampled` to `ALWAYS_OFF`, so a caller can suppress dev's *request* spans too. Poller-cycle traces are immune in every environment — they have no inbound parent — which is why they are the sound basis for a coverage claim and request spans are not.

**Detection, since there is no prevention here:** if prd request traces thin out or vanish while poller-cycle traces keep arriving, suspect an upstream stamping `-00` before suspecting the exporter or the IAM. That asymmetry — poller traces present, request traces absent — is the signature, and it is the one reading that distinguishes this from every other tracing failure.

Consequently `TRACING_ENABLED` in prd is now gated only on the rollout checklist's IAM step, which passed for all three projects on 2026-09-11.

## 3. The three `cd.yml` smoke jobs are byte-identical copies

**Severity:** low · `.github/workflows/cd.yml:144` · **Not applied: collides with a `session-settled:` decision.**

`smoke-dev` / `smoke-tst` / `smoke-prd` repeat the same three steps verbatim (~110 lines), differing only in `HOST` and `ENV_LABEL`, and each carries the full three-way `case` of which two arms are dead. A reusable workflow taking `host`/`env_label` inputs would state the checks once while keeping the per-environment `needs` gating.

The duplication is deliberate and the plan's KTD records why: the jobs are gated by `needs` alone, and the header argues that case at length (copying the deploy jobs' `if:` guards would reference `needs.build` outputs these jobs cannot see, and smoke would be silently skipped on every push deploy). Worth revisiting on its own terms — the reviewer's risk is real, that a smoke fix lands in one job and prd keeps the old check with a green tick — but not as a side effect of a review-fix pass.

## 4. `_send_push_result` is a production shim for test-stub shapes

**Severity:** low · `disruptions/api/varsel.py:2015` · **Not applied: collides with the simplify pass's settled decision, but the argument against it got sharper.**

The shim tolerates suites that stub `_send_push` with a bare result string; production never takes the fallback arm, and the docstring says so. The simplify pass introduced it citing the `alternatives.fetch_candidates` precedent.

The reviewer's counter is worth recording because it is better than the earlier lens's version of the same finding: `fetch_candidates` is about an out-parameter added to a *real* signature, not about a return shape that changed — and a suite still stubbing the pre-change shape now passes while asserting nothing about `error_detail`, so the regression the shim exists to tolerate becomes undetectable. `test_varsel_api` already got the 5-tuple treatment; the handful of remaining stubs could get the same, and the shim would go.

Deferred rather than dismissed: it is a settled structure pin, and the severity-restoration work in `9399f3b` now has tests that do assert on `error_detail`, which narrows the reviewer's third point.
