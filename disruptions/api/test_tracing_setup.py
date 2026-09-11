"""Checks for the OpenTelemetry tracing module: gating, fail-open, spans, sanitization, log keys.

Hermetic — NO ADC and NO network, ever. The real OTLP/gRPC exporter is never built: every
enabled-path test injects an in-memory exporter through ``setup(exporter_factory=...)``, which is
the seam that exists precisely so tests never touch ``google.auth`` or ``grpc`` (KTD3: those are
lazy imports inside the gated setup, and this suite asserts they stay un-imported). ``main`` is
deliberately NOT imported: the module under test is self-contained, and importing main would drag
``google.cloud.bigquery`` — and with it ``grpc`` — into ``sys.modules``, retiring that assertion.

Global-state discipline: the OTel API allows ``set_tracer_provider`` once per process, so every
test that sets it goes through ``_reset_tracing()`` first (the ``trace._TRACER_PROVIDER`` /
``Once`` reset the API leaves as the only door) and shuts its provider down after. The requests
instrumentation is likewise global — tests that install it uninstrument in ``finally`` — which is
what keeps the tests order-independent.

Run: uv run --with fastapi --with httpx --with pydantic --with opentelemetry-api \
      --with opentelemetry-sdk --with opentelemetry-instrumentation-fastapi \
      --with opentelemetry-instrumentation-requests python test_tracing_setup.py  (or under pytest)
"""

import json
import logging
import os
import sys

# The flag must not leak in from a developer's shell: the import-time setup() call below must see
# the default-off posture (same pattern as test_metrics.py popping SSO_REQUIRED).
for _var in ("TRACING_ENABLED", "OTEL_TRACES_SAMPLER", "OTEL_TRACES_SAMPLER_ARG"):
    os.environ.pop(_var, None)

import logging_setup
import tracing_setup
from opentelemetry import trace
from opentelemetry.util._once import Once


def _reset_tracing() -> None:
    """Return the process to the pre-setup state: no global provider, module flag off.

    ``trace._TRACER_PROVIDER`` / ``_TRACER_PROVIDER_SET_ONCE`` are private API, used here because
    the public API is set-once by design and tests must be order-independent. A provider that was
    set is shut down first so its BatchSpanProcessor worker thread does not leak.
    """
    provider = trace._TRACER_PROVIDER
    if provider is not None and hasattr(provider, "shutdown"):
        provider.shutdown()
    trace._TRACER_PROVIDER = None
    trace._TRACER_PROVIDER_SET_ONCE = Once()
    tracing_setup._enabled = False
    for var in ("TRACING_ENABLED", "OTEL_TRACES_SAMPLER", "OTEL_TRACES_SAMPLER_ARG"):
        os.environ.pop(var, None)
    try:  # uninstrument requests if an enabled test left it installed
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        instrumentor = RequestsInstrumentor()
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
    except ImportError:
        pass


def _enable_with_memory_exporter():
    """Run the real setup() with the flag on and an injected in-memory exporter; return it."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    os.environ["TRACING_ENABLED"] = "true"
    tracing_setup.setup(exporter_factory=lambda: exporter)
    assert tracing_setup._enabled, "setup() with the flag on and a healthy factory must enable"
    return exporter


def _flush() -> None:
    trace.get_tracer_provider().force_flush()


def test_disabled_by_default_is_a_no_op_and_imports_no_exporter_machinery():
    """With TRACING_ENABLED unset, setup() configures nothing: no global provider, no lazy SDK
    machinery imported (KTD3 — an exporter ImportError must not be able to crash `import main`,
    which only holds if the gated path never imports it), and span() is a usable no-op."""
    _reset_tracing()
    tracing_setup.setup()
    assert trace._TRACER_PROVIDER is None, "flag off must not set a tracer provider"
    # The gated lazy imports must not have happened. `grpc` is installed in the dev venv (it rides
    # with google-cloud-bigquery) but this suite never imports main, so its presence in
    # sys.modules could only come from tracing_setup's gated path.
    for module in ("grpc", "google.auth.transport.grpc", "opentelemetry.exporter.otlp"):
        assert module not in sys.modules, f"{module} was imported on the disabled path"
    with tracing_setup.span("x") as s:
        assert not s.is_recording(), "with no provider, span() must yield a non-recording span"


def test_disabled_instrument_app_leaves_the_app_untouched():
    from fastapi import FastAPI

    _reset_tracing()
    app = FastAPI()
    before = len(app.user_middleware)
    tracing_setup.instrument_app(app)
    assert len(app.user_middleware) == before, "disabled instrument_app must add no middleware"


def test_cycle_and_phase_spans_nest_as_parent_and_child():
    """`span("poller_cycle")` containing `span("rail_calls")` exports a parent/child pair — the
    shape KTD4 promises: one trace per rebuild cycle with per-phase children."""
    _reset_tracing()
    try:
        exporter = _enable_with_memory_exporter()
        # One combined with: contexts enter in order, so rail_calls opens inside poller_cycle —
        # the same nesting main's _phase gets from the context.
        with tracing_setup.span("poller_cycle"), tracing_setup.span("rail_calls"):
            pass
        _flush()
        spans = {s.name: s for s in exporter.get_finished_spans()}
        assert set(spans) == {"poller_cycle", "rail_calls"}, sorted(spans)
        child, parent = spans["rail_calls"], spans["poller_cycle"]
        assert child.parent is not None and child.parent.span_id == parent.context.span_id
        assert parent.parent is None, "the cycle span must be a root span"
    finally:
        _reset_tracing()


def test_the_resource_names_the_service_project_and_environment():
    _reset_tracing()
    os.environ["GOOGLE_CLOUD_PROJECT"] = "ent-kixavvik-dev"
    try:
        exporter = _enable_with_memory_exporter()
        with tracing_setup.span("x"):
            pass
        _flush()
        (span,) = exporter.get_finished_spans()
        attrs = dict(span.resource.attributes)
        assert attrs["service.name"] == "kix-avvik"
        assert attrs["gcp.project_id"] == "ent-kixavvik-dev"
        # Derived from the project suffix, NOT deployment.environment_label(): that helper
        # returns None in prd by design, and a trace resource must never carry a null.
        assert attrs["environment"] == "dev"
    finally:
        _reset_tracing()
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)


def test_instrumented_app_traces_routes_but_never_health():
    """/health is excluded by an ANCHORED full-URL regex (excluded_urls matches the whole URL,
    so an unanchored `/health` would also exclude /varsel-api/health-adjacent paths)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _reset_tracing()
    try:
        exporter = _enable_with_memory_exporter()
        app = FastAPI()

        @app.get("/health")
        def health():
            return {"status": "ok"}

        @app.get("/disruptions")
        def disruptions():
            return []

        tracing_setup.instrument_app(app)
        client = TestClient(app)
        assert client.get("/health").status_code == 200
        _flush()
        assert exporter.get_finished_spans() == (), "/health must produce no server span"
        assert client.get("/disruptions").status_code == 200
        _flush()
        server_spans = [
            s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.SERVER
        ]
        assert len(server_spans) == 1, [s.name for s in exporter.get_finished_spans()]
    finally:
        _reset_tracing()


def test_no_exported_server_span_attribute_carries_the_query_string():
    """R15b: device_id is a capability token on /varsel-api/journeys — a traced request carrying
    `?device_id=SECRET` must export NO attribute value containing it, on any span."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _reset_tracing()
    try:
        exporter = _enable_with_memory_exporter()
        app = FastAPI()

        @app.get("/varsel-api/journeys")
        def journeys(device_id: str = ""):
            return []

        tracing_setup.instrument_app(app)
        client = TestClient(app)
        assert client.get("/varsel-api/journeys?device_id=SECRET").status_code == 200
        _flush()
        spans = exporter.get_finished_spans()
        assert spans, "the traced request must export at least one span"
        for span in spans:
            for key, value in (span.attributes or {}).items():
                assert "SECRET" not in str(value), f"{span.name}.{key} leaked the query: {value!r}"
    finally:
        _reset_tracing()


def test_outbound_push_delivery_spans_carry_neither_query_nor_token_path():
    """R15b's outbound half: a Web Push endpoint URL is a capability token in the PATH (Chrome's
    ride on fcm.googleapis.com, so a bare 'is it a Google API?' carve-out would leak them). The
    client span must keep scheme+host — still diagnosable — and lose path and query."""
    import requests
    import requests.adapters

    _reset_tracing()
    real_send = requests.adapters.HTTPAdapter.send
    try:
        exporter = _enable_with_memory_exporter()

        def fake_send(self, request, **kwargs):  # below Session.send, so instrumentation runs
            response = requests.Response()
            response.status_code = 201
            response.request = request
            response.url = request.url
            return response

        requests.adapters.HTTPAdapter.send = fake_send
        for url in (
            "https://fcm.googleapis.com/wp/SECRETTOKEN123?auth=SECRETQUERY",
            "https://updates.push.services.mozilla.com/wpush/v2/SECRETTOKEN123",
        ):
            assert requests.post(url, timeout=1).status_code == 201
        _flush()
        client_spans = [
            s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.CLIENT
        ]
        assert len(client_spans) == 2, [s.name for s in exporter.get_finished_spans()]
        hosts_seen = set()
        for span in client_spans:
            for key, value in (span.attributes or {}).items():
                assert "SECRETTOKEN123" not in str(value), f"{key} leaked the token: {value!r}"
                assert "SECRETQUERY" not in str(value), f"{key} leaked the query: {value!r}"
                if isinstance(value, str) and value.startswith("https://"):
                    hosts_seen.add(value)
        assert any("fcm.googleapis.com" in h for h in hosts_seen), (
            f"the sanitized URL must keep scheme+host for diagnosis, got {hosts_seen}"
        )
    finally:
        requests.adapters.HTTPAdapter.send = real_send
        _reset_tracing()


def test_a_google_api_client_span_keeps_its_path_but_not_its_query():
    """The path carve-out that makes BigQuery/Vertex spans diagnosable: a non-push Google API
    call keeps its path (which names the method) while the query is still stripped."""
    import requests
    import requests.adapters

    _reset_tracing()
    real_send = requests.adapters.HTTPAdapter.send
    try:
        exporter = _enable_with_memory_exporter()

        def fake_send(self, request, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response.request = request
            response.url = request.url
            return response

        requests.adapters.HTTPAdapter.send = fake_send
        requests.get(
            "https://bigquery.googleapis.com/bigquery/v2/projects/p/jobs?key=SECRETQUERY",
            timeout=1,
        )
        _flush()
        (span,) = [s for s in exporter.get_finished_spans() if s.kind == trace.SpanKind.CLIENT]
        url_values = [
            str(v)
            for v in (span.attributes or {}).values()
            if isinstance(v, str) and v.startswith("https://")
        ]
        assert any("/bigquery/v2/projects/p/jobs" in v for v in url_values), url_values
        assert all("SECRETQUERY" not in v for v in url_values), url_values
    finally:
        requests.adapters.HTTPAdapter.send = real_send
        _reset_tracing()


def test_formatter_emits_the_three_correlation_keys_inside_a_sampled_span():
    """R17: a log line inside a sampled span carries trace/spanId/trace_sampled, with the trace
    key in Cloud Logging's `projects/<proj>/traces/<32-hex>` form."""
    from opentelemetry.sdk.trace import TracerProvider

    os.environ["GOOGLE_CLOUD_PROJECT"] = "ent-kixavvik-dev"
    try:
        # A LOCAL provider, never set globally: start_as_current_span attaches to the context
        # regardless, which is what lets this test leave the set-once global alone entirely.
        tracer = TracerProvider().get_tracer("test")
        formatter = logging_setup.JsonFormatter()
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        with tracer.start_as_current_span("s") as span:
            entry = json.loads(formatter.format(record))
            ctx = span.get_span_context()
        assert entry["logging.googleapis.com/trace"] == (
            f"projects/ent-kixavvik-dev/traces/{trace.format_trace_id(ctx.trace_id)}"
        )
        assert entry["logging.googleapis.com/spanId"] == trace.format_span_id(ctx.span_id)
        assert entry["logging.googleapis.com/trace_sampled"] is True
    finally:
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)


def test_formatter_emits_no_correlation_keys_outside_a_span():
    formatter = logging_setup.JsonFormatter()
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    entry = json.loads(formatter.format(record))
    assert not any(k.startswith("logging.googleapis.com/") for k in entry), entry


def test_formatter_without_a_project_omits_the_trace_key_but_keeps_the_span_keys():
    """`projects/None/traces/...` must never be emitted — the trace key needs the project id,
    while spanId and trace_sampled are project-free and stay."""
    from opentelemetry.sdk.trace import TracerProvider

    os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
    tracer = TracerProvider().get_tracer("test")
    formatter = logging_setup.JsonFormatter()
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    with tracer.start_as_current_span("s"):
        entry = json.loads(formatter.format(record))
    assert "logging.googleapis.com/trace" not in entry, entry
    assert "logging.googleapis.com/spanId" in entry
    assert "logging.googleapis.com/trace_sampled" in entry


def test_a_poisoned_span_accessor_never_breaks_the_formatter():
    """The formatter runs on EVERY record — a tracing problem must never take logging with it."""

    class _Poisoned:
        def get_current_span(self):
            raise RuntimeError("poisoned")

        def __getattr__(self, name):
            raise RuntimeError("poisoned")

    before = logging_setup._OTEL_TRACE
    logging_setup._OTEL_TRACE = _Poisoned()
    try:
        formatter = logging_setup.JsonFormatter()
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
        entry = json.loads(formatter.format(record))
        assert entry["message"] == "hello"
        assert not any(k.startswith("logging.googleapis.com/") for k in entry), entry
    finally:
        logging_setup._OTEL_TRACE = before


def test_a_raising_exporter_factory_fails_open_with_one_structured_warning():
    """R13: any init error logs event="tracing_disabled" and the app serves with no-op spans —
    and the global provider stays unset, so nothing half-configured survives the failure."""
    _reset_tracing()
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    tracing_logger = logging.getLogger("tracing_setup")
    tracing_logger.addHandler(handler)
    os.environ["TRACING_ENABLED"] = "true"
    try:

        def exploding_factory():
            raise RuntimeError("no exporter today")

        tracing_setup.setup(exporter_factory=exploding_factory)
        warnings = [
            r
            for r in records
            if r.levelno == logging.WARNING
            and getattr(r, "fields", {}).get("event") == "tracing_disabled"
        ]
        assert len(warnings) == 1, [r.getMessage() for r in records]
        assert "no exporter today" in str(warnings[0].fields.get("reason"))
        assert tracing_setup._enabled is False
        assert trace._TRACER_PROVIDER is None, "a failed setup must not set a global provider"
        with tracing_setup.span("x") as s:  # still usable, still a no-op
            assert not s.is_recording()
    finally:
        tracing_logger.removeHandler(handler)
        _reset_tracing()


def test_a_device_id_never_reaches_a_span_in_the_request_path():
    """R15b: the capability token in /varsel-api/devices/<id> is redacted, not just the query.

    The first version of the inbound hook stripped the query string only, which missed the sharper
    leak entirely: the device id is a PATH segment, and it is the same secret `/varsel-api/devices`
    accepts as proof of ownership — the one this module already refuses to export for
    fcm.googleapis.com. Exported, anyone with Cloud Trace read in the project could replay it.
    """
    cases = [
        ("/varsel-api/devices/abc123", "/varsel-api/devices/<redacted>"),
        ("/varsel-api/devices/abc123/data", "/varsel-api/devices/<redacted>/data"),
        ("/varsel-api/journeys/j-77", "/varsel-api/journeys/<redacted>"),
        ("/notification-api/v1/subscriptions/s-9", "/notification-api/v1/subscriptions/<redacted>"),
        # Untouched: no identifier, and a bare collection route has nothing to redact.
        ("/varsel-api/devices", "/varsel-api/devices"),
        ("/varsel-api/stops", "/varsel-api/stops"),
        ("/health", "/health"),
    ]
    for raw, expected in cases:
        assert tracing_setup._safe_path(raw) == expected, raw

    class _Span:
        def __init__(self):
            self.attributes = {"http.url": "https://x.entur.io/varsel-api/devices/abc123?y=1"}

        def set_attribute(self, key, value):
            self.attributes[key] = value

    span = _Span()
    tracing_setup._server_request_hook(span, {"path": "/varsel-api/devices/abc123"})
    exported = span.attributes["http.url"]
    assert "abc123" not in exported, exported
    assert "y=1" not in exported, exported
    assert exported == "https://x.entur.io/varsel-api/devices/<redacted>"


def test_every_path_parameter_route_sits_behind_a_redacted_segment():
    """The redaction is a whitelist of PRECEDING segments, so a new id-bearing route must join it.

    Walks the app's own routes rather than restating a list: an "is this a uuid?" heuristic would
    stop matching the day an id format changes, and the failure is invisible — a leaked token
    looks exactly like a working span. A new `/…/widgets/{widget_id}` fails here until `widgets`
    is added to _ID_BEARING_SEGMENTS or deliberately judged non-identifying.
    """
    import re

    # This suite is otherwise main-free, so it carries no module-top BigQuery stub — and main
    # builds a client at import scope. CI has no ADC, so an unstubbed import raises
    # DefaultCredentialsError there while passing on any laptop that has run `gcloud auth login`.
    # Same stub every main-importing suite applies at its module top, just applied here.
    from google.cloud import bigquery as _bq_for_main

    _bq_for_main.Client = lambda *a, **k: None  # type: ignore[assignment]
    import main

    unguarded = []
    for route in main.app.routes:
        path = getattr(route, "path", "")
        for match in re.finditer(r"\{[^}]+\}", path):
            prefix = path[: match.start()].rstrip("/")
            preceding = prefix.rsplit("/", 1)[-1].lower()
            if preceding not in tracing_setup._ID_BEARING_SEGMENTS:
                unguarded.append(f"{path} (segment before the parameter: {preceding!r})")
    assert not unguarded, (
        "path-parameter routes whose id would be exported to Cloud Trace verbatim:\n"
        + "\n".join(unguarded)
    )


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} tracing checks passed.")


if __name__ == "__main__":
    _run()
