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

**Severity:** medium · `helm/kix-avvik/env/values-kub-ent-prd.yaml` · **Not applied: no mechanical fix exists; must be settled before tracing is flipped on in prd.**

`OTEL_TRACES_SAMPLER: parentbased_traceidratio` honours a remote parent's sampled flag before the ratio applies, and the ASGI instrumentation extracts trace context from request headers. So any caller can send `traceparent: 00-<id>-<id>-01` on every request to `kix-avvik.entur.io` and force 100 % sampling with trace ids they chose — span volume, ingestion cost, and poisoned trace search. An ingress that always stamps `-00` would silently reduce sampling to zero while the values file claims 10 %.

**The obvious fix does not work.** Swapping to a plain `traceidratio` — or to a `ParentBased` whose remote arms are the ratio, which is what the review suggested — closes the always-`-00` case and leaves the attacker case fully open: `TraceIdRatioBased` is deterministic on the trace id, and the trace id is the attacker's. They pick ids that hash into the sampled range.

The real fix is to not accept inbound trace context at the public edge — strip the header in a middleware outside the OTel one, or settle it at the ingress — which is a design decision about whether kix-avvik is ever a child of an upstream Entur trace. Mitigated for now by shipping `TRACING_ENABLED: "false"` in tst and prd (commit `9399f3b`); the prd values file and the rollout checklist both name this as a precondition for flipping it on.

## 3. The three `cd.yml` smoke jobs are byte-identical copies

**Severity:** low · `.github/workflows/cd.yml:144` · **Not applied: collides with a `session-settled:` decision.**

`smoke-dev` / `smoke-tst` / `smoke-prd` repeat the same three steps verbatim (~110 lines), differing only in `HOST` and `ENV_LABEL`, and each carries the full three-way `case` of which two arms are dead. A reusable workflow taking `host`/`env_label` inputs would state the checks once while keeping the per-environment `needs` gating.

The duplication is deliberate and the plan's KTD records why: the jobs are gated by `needs` alone, and the header argues that case at length (copying the deploy jobs' `if:` guards would reference `needs.build` outputs these jobs cannot see, and smoke would be silently skipped on every push deploy). Worth revisiting on its own terms — the reviewer's risk is real, that a smoke fix lands in one job and prd keeps the old check with a green tick — but not as a side effect of a review-fix pass.

## 4. `_send_push_result` is a production shim for test-stub shapes

**Severity:** low · `disruptions/api/varsel.py:2015` · **Not applied: collides with the simplify pass's settled decision, but the argument against it got sharper.**

The shim tolerates suites that stub `_send_push` with a bare result string; production never takes the fallback arm, and the docstring says so. The simplify pass introduced it citing the `alternatives.fetch_candidates` precedent.

The reviewer's counter is worth recording because it is better than the earlier lens's version of the same finding: `fetch_candidates` is about an out-parameter added to a *real* signature, not about a return shape that changed — and a suite still stubbing the pre-change shape now passes while asserting nothing about `error_detail`, so the regression the shim exists to tolerate becomes undetectable. `test_varsel_api` already got the 5-tuple treatment; the handful of remaining stubs could get the same, and the shim would go.

Deferred rather than dismissed: it is a settled structure pin, and the severity-restoration work in `9399f3b` now has tests that do assert on `error_detail`, which narrows the reviewer's third point.
