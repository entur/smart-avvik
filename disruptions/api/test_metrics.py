"""Checks for the Prometheus metrics module: registry, HTTP middleware, log-level counting.

Hermetic: the BigQuery client is stubbed before main is imported, and the app's lifespan is never
entered — so `metrics.start()` is never called by the app and no exporter port is bound at import
(the one test that binds a port asks for an ephemeral one and shuts it down again). All metric
objects work without the server because they register with prometheus_client's default REGISTRY
at import time.

Registry rules these tests live by: assert counter DELTAS, never absolutes (the registry is
process-global and other tests move it), and never `importlib.reload(metrics)` (re-registering a
metric name in the default REGISTRY raises).

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery \
      --with google-cloud-storage --with pywebpush --with google-cloud-firestore \
      --with prometheus-client python test_metrics.py  (or under pytest)
"""

import asyncio
import logging
import os
import urllib.request

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

# The suite asserts what an SSO-less request counts as, so a developer's exported SSO_REQUIRED
# must not leak in (the same reason the /varsel-api tests pin KIX_ENVIRONMENT at import). The one
# test about SSO rejection sets it explicitly and restores the popped state.
os.environ.pop("SSO_REQUIRED", None)

import logging_setup
import main
import metrics
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

# Not entered as a context manager on purpose: that would run the lifespan hook, which starts the
# background poller, loads BigQuery lookups — and calls metrics.start() on a real port.
client = TestClient(main.app)


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Current value of one sample, 0.0 when the labelset has never been touched."""
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def test_request_is_counted_with_method_path_and_status_class():
    """One request → +1 on the counter and +1 observation on the duration histogram, with the
    path collapsed to its first segment and the status to its class."""
    count_labels = {"method": "GET", "path": "demo-api", "status": "2xx"}
    duration_labels = {"method": "GET", "path": "demo-api", "long_poll": "false"}
    count_before = _sample("kix_http_requests_total", count_labels)
    duration_before = _sample("kix_http_request_duration_seconds_count", duration_labels)
    r = client.get("/demo-api/config")
    assert r.status_code == 200, r.text
    assert _sample("kix_http_requests_total", count_labels) == count_before + 1
    assert _sample("kix_http_request_duration_seconds_count", duration_labels) == duration_before + 1


def test_unknown_path_labels_as_other_and_known_mounts_by_segment():
    """The path label is an allowlist, not the raw path: a scanner probing random URLs on the
    public port must collapse to one series, or every probe mints a time series held forever."""
    other = {"method": "GET", "path": "other", "status": "4xx"}
    before = _sample("kix_http_requests_total", other)
    r = client.get("/no-such-page-xyzzy/deep/path")
    assert r.status_code == 404
    assert _sample("kix_http_requests_total", other) == before + 1
    # A known mount keeps its segment — /health answers 200 in a lifespan-less process.
    health = {"method": "GET", "path": "health", "status": "2xx"}
    before = _sample("kix_http_requests_total", health)
    assert client.get("/health").status_code == 200
    assert _sample("kix_http_requests_total", health) == before + 1


def test_log_records_are_counted_by_level_and_emit_never_raises():
    metrics.configure()
    logger = logging.getLogger("test-metrics")
    before_error = _sample("kix_log_records_total", {"level": "ERROR"})
    logger.error("counted")
    assert _sample("kix_log_records_total", {"level": "ERROR"}) == before_error + 1
    # Exotic records: an arbitrary object as arg, and a record whose %-formatting FAILS (too few
    # args) — the JSON stdout handler's format() raises internally and logging routes that to
    # handleError; the counting handler must still count and must never raise. raiseExceptions is
    # silenced so the expected handleError traceback does not spray the test output.
    before_info = _sample("kix_log_records_total", {"level": "INFO"})
    raise_before = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        logger.info("exotic %s", object())
        logger.info("mismatched %s %s", "only-one-arg")  # noqa: PLE1206 - failing format is the point
    finally:
        logging.raiseExceptions = raise_before
    assert _sample("kix_log_records_total", {"level": "INFO"}) == before_info + 2


def test_setup_rerun_needs_reconfigure_and_installs_exactly_one_handler():
    """logging_setup.setup() wipes root handlers — the counting handler with them — so a re-setup
    needs a re-configure; configure() is idempotent so calling it twice installs one handler."""
    logging_setup.setup()
    root = logging.getLogger()
    assert not any(isinstance(h, metrics._CountingHandler) for h in root.handlers)
    metrics.configure()
    metrics.configure()
    assert sum(isinstance(h, metrics._CountingHandler) for h in root.handlers) == 1
    before = _sample("kix_log_records_total", {"level": "ERROR"})
    logging.getLogger("test-metrics").error("still counted after re-setup")
    assert _sample("kix_log_records_total", {"level": "ERROR"}) == before + 1


def test_sso_rejected_request_is_still_counted():
    """The metrics middleware is registered AFTER RequireSSO, so it runs OUTSIDE it — a request
    the gate rejects is a served request and must be counted with its status class. Required but
    unconfigured fails closed as 503, which is the class asserted here."""
    keys = ("SSO_REQUIRED", "SSO_AUTH0_DOMAIN", "SSO_AUTH0_CLIENT_ID", "SSO_AUTH0_CLIENT_SECRET")
    before_env = {k: os.environ.get(k) for k in keys}
    os.environ["SSO_REQUIRED"] = "true"
    for key in keys[1:]:
        os.environ.pop(key, None)
    labels = {"method": "GET", "path": "historikk-api", "status": "5xx"}
    before = _sample("kix_http_requests_total", labels)
    try:
        r = client.get("/historikk-api/config")
        assert r.status_code == 503, r.text
        assert _sample("kix_http_requests_total", labels) == before + 1
    finally:
        for key, value in before_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_start_serves_text_exposition_on_its_own_ephemeral_port():
    """start() binds its own port (in the app, METRICS_PORT — never the public 8080) and serves
    the default REGISTRY as text exposition. Port 0 here so the test never collides with a real
    exporter or another suite."""
    client.get("/health")  # ensure at least one HTTP sample exists before scraping
    server, thread = metrics.start(port=0)
    try:
        port = server.server_port
        assert port != 0
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
            body = response.read().decode()
        assert "kix_http_requests_total" in body
        assert "kix_poller_last_tick_timestamp_seconds" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_middleware_passes_non_http_scopes_untouched():
    """A lifespan scope must reach the inner app with the ORIGINAL send — no status-noting
    wrapper — and count nothing; wrapping it would break startup/shutdown message passing."""
    seen = {}

    async def inner(scope, receive, send):
        seen["scope"], seen["receive"], seen["send"] = scope, receive, send

    async def receive():  # pragma: no cover - never awaited by the middleware
        return {"type": "lifespan.startup"}

    async def send(message):  # pragma: no cover - never awaited by the middleware
        pass

    scope = {"type": "lifespan"}
    asyncio.run(metrics.HTTPMetrics(inner)(scope, receive, send))
    assert seen["scope"] is scope
    assert seen["receive"] is receive
    assert seen["send"] is send


def test_domain_metrics_are_declared_with_the_agreed_names_and_labels():
    """U3/U4 only import and increment (KTD11) — so every agreed name and labelset is exercised
    once here, and a rename or label change fails now rather than when those units land."""
    metrics.POLLER_LAST_TICK.set(1_234.5)
    assert _sample("kix_poller_last_tick_timestamp_seconds") == 1_234.5
    metrics.POLLER_LAST_CYCLE.set(2_345.5)
    assert _sample("kix_poller_last_cycle_timestamp_seconds") == 2_345.5
    metrics.POLLER_LEADER.set(1)
    assert _sample("kix_poller_leader") == 1
    metrics.POLLER_LEADER.set(0)
    assert _sample("kix_poller_leader") == 0
    metrics.LABELLING_LAST_SUCCESS.set(3_456.5)
    assert _sample("kix_labelling_last_success_timestamp_seconds") == 3_456.5

    before = _sample("kix_poller_cycle_duration_seconds_count")
    metrics.POLLER_CYCLE_DURATION.observe(42.0)
    assert _sample("kix_poller_cycle_duration_seconds_count") == before + 1

    phase = {"phase": "test_phase"}
    before = _sample("kix_poller_phase_duration_seconds_count", phase)
    metrics.POLLER_PHASE_DURATION.labels(**phase).observe(1.5)
    assert _sample("kix_poller_phase_duration_seconds_count", phase) == before + 1

    before = _sample("kix_poller_phase_cpu_seconds_count", phase)
    metrics.POLLER_PHASE_CPU.labels(**phase).observe(0.75)
    assert _sample("kix_poller_phase_cpu_seconds_count", phase) == before + 1

    for counter, name, labels in (
        (metrics.POLLER_PHASE_TOTAL, "kix_poller_phase_total", {"phase": "test_phase", "result": "ok"}),
        (metrics.PUSH_TOTAL, "kix_push_total", {"result": "sent"}),
        (
            metrics.LLM_MESSAGE_TOTAL,
            "kix_llm_message_total",
            {"provider": "gemini", "result": "ok", "surface": "live"},
        ),
        (metrics.DECISION_WRITE_TOTAL, "kix_decision_write_total", {"result": "ok"}),
    ):
        before = _sample(name, labels)
        counter.labels(**labels).inc()
        assert _sample(name, labels) == before + 1, name


def test_journey_alerts_that_may_wait_are_timed_apart_as_long_polls():
    """A journey-alerts read with `wait` > 0 -- including no `wait` at all, which defaults to the
    longest -- is timed under long_poll="true", so rule 11 can leave the wait the caller asked for
    out of p99. `wait=0` answers at once and stays ordinary latency. Without a lifespan the read
    answers 503 not_loaded, which is after the handler has marked the request."""
    base = "/notification-api/v1/journey-alerts?from_stop=NSR:StopPlace:1&to_stop=NSR:StopPlace:2"

    def count(long_poll: str) -> float:
        labels = {"method": "GET", "path": "notification-api", "long_poll": long_poll}
        return _sample("kix_http_request_duration_seconds_count", labels)

    for query, label in (("", "true"), ("&wait=5", "true"), ("&wait=0", "false")):
        marked, unmarked = count(label), count("false" if label == "true" else "true")
        r = client.get(base + query)
        assert r.status_code == 503, (query, r.status_code, r.text)
        assert count(label) == marked + 1, query
        assert count("false" if label == "true" else "true") == unmarked, query


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} metrics checks passed.")


if __name__ == "__main__":
    _run()
