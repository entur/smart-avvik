"""OpenTelemetry tracing to Google Cloud Trace, gated on ``TRACING_ENABLED`` and failing open.

Importing it runs :func:`setup` — the side effect is deliberate, the same idiom as
``logging_setup`` (which main imports first, alphabetically, so a fail-open WARNING from here is
already JSON). With the flag off — the default, and every test suite, laptop and CI run — setup
returns immediately and :func:`span` yields the OTel API's non-recording spans, which cost nothing.

**Module-top imports are the OTel API only** (KTD3). The SDK, the OTLP/gRPC exporter, ``grpc``,
``google.auth`` and both instrumentors import lazily inside the flag-gated functions: they are
five distributions plus grpcio, an exporter ImportError outside the fail-open guard would crash
``import main``, and keeping them out of the module top is what lets most suites run with only
``opentelemetry-api`` installed.

Sampling is NOT configured here: the SDK's default-constructed sampler reads the standard
``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG`` env vars natively, which Helm sets per
environment (R18) — dev samples everything, tst/prd head-sample 10 %.

**Span sanitization (R15b)**: hooks on both instrumentations overwrite every URL-carrying span
attribute before export — no query string ever leaves the process (``device_id`` on
``/varsel-api`` is a capability token), and an outbound span to a non-allowlisted host keeps only
scheme+host (a Web Push endpoint's PATH is the capability token; Chrome's ride on
``fcm.googleapis.com``, so the Google-API carve-out explicitly excludes that host).

BigQuery needs no instrumentor: its client emits spans on its own once the OTel API is importable
and a global provider is set, which is why main imports this module before it builds
``bq_client`` (the alphabetical import block guarantees it).
"""

import logging
import os
import time
from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit

from opentelemetry import trace

logger = logging.getLogger(__name__)

# deployment.py:43's parse, repeated rather than imported for the same reason deployment repeats
# llm_message's: this module must import nothing heavier than the OTel API.
_TRUTHY = ("true", "1", "yes", "on")

# Google's documented OTLP path for Cloud Trace (KTD9). The gRPC channel ignores the URL path,
# but this is the endpoint string the migration guide shows, kept verbatim.
_ENDPOINT = "https://telemetry.googleapis.com:443/v1/traces"

#: Anchored FULL-URL regex: `excluded_urls` is re.search'd against the whole request URL
#: (scheme://host/path), so an unanchored "/health" would also exclude any path that merely
#: contains it. Only exactly /health — the probe path _HealthAccessMute already mutes — matches.
_EXCLUDED_URLS = r"^https?://[^/]+/health$"

#: Hosts on googleapis.com whose PATH is a per-user capability token, not an API method: Chrome's
#: Web Push endpoints (https://fcm.googleapis.com/wp/<token>). The Google-API path carve-out in
#: :func:`_path_safe_host` must not cover them, or every push delivery span would publish the
#: token that lets anyone push to that device.
_PUSH_HOSTS_ON_GOOGLE = frozenset({"fcm.googleapis.com"})

#: Path segments whose NEXT segment is an identifier minted for one subscriber, never a method
#: name. `/varsel-api/devices/<device_id>` is the sharp one: that id is the capability token
#: `/varsel-api/devices` accepts as proof of ownership — the same secret this module already
#: refuses to export for `fcm.googleapis.com` — so exporting it in an INBOUND span's path would
#: hand anyone with Cloud Trace read in the project everything they need to replay the call.
#: `journeys` and `subscriptions` are the other two identifier-bearing routes in the app.
#: Redacting the segment after these keeps the span's shape (the route is what makes it
#: diagnosable) while dropping the only part of it that identifies a person (R15b).
_ID_BEARING_SEGMENTS = frozenset({"devices", "journeys", "subscriptions"})
_REDACTED_SEGMENT = "<redacted>"

#: The CLOSED vocabulary of span-attribute names, and the whole of the span-attribute rule.
#:
#: The repo's existing redaction requirement covers LOG LINES. A span is not a log line and is not
#: covered by it, while it is exported to Cloud Trace — where project IAM is the only gate — and is
#: opened in exactly the places the journey data lives: `varsel`'s sub-passes and the store's
#: Firestore calls. So the rule `metrics.py` states for label values is restated here and applies
#: to both halves of an attribute: **the NAME must be in this set and the VALUE must be a number
#: or a bool**. Counts and timings, nothing else — never a stop ref, a place name, a departure
#: time, a callback URL, a client or subscription id, or any prose.
#:
#: A bounded string is not an exception to make: the span's NAME is already a literal from a closed
#: set at each call site, which is where "which pass is this" belongs. Adding a string-valued
#: attribute here would also mint a Cloud Trace filter dimension per distinct value, which is the
#: unbounded-cardinality half of the same rule.
SPAN_ATTRIBUTE_NAMES = frozenset(
    {
        # varsel's sub-passes (R16): how many of each thing the pass carried.
        "definitions",
        "matched",
        "queued",
        "recipients",
        "registrations",
        "workers",
        # Set by `measure=True` at close, never by a caller: what the span cost the one
        # interpreter (KTD2 sizes the widened emit pass in interpreter ms per transaction).
        "cpu_ms",
        "wall_ms",
    }
)

_enabled = False


def _safe_path(path: str) -> str:
    """``path`` with every subscriber identifier replaced by a placeholder.

    A whitelist of preceding segments rather than a guess at what an id looks like: an
    "is this a uuid?" heuristic silently stops matching the day an id format changes, and the
    failure is invisible — a leaked token looks exactly like a working span. Every path-parameter
    route in the app sits behind one of :data:`_ID_BEARING_SEGMENTS`, and a test walks the app's
    own routes to keep that true.
    """
    segments = path.split("/")
    for i in range(1, len(segments)):
        if segments[i - 1].lower() in _ID_BEARING_SEGMENTS and segments[i]:
            segments[i] = _REDACTED_SEGMENT
    return "/".join(segments)


def _path_safe_host(host: str) -> bool:
    """Whether an outbound span to ``host`` may keep its URL path.

    Google API hosts keep it — the path names the method (``/bigquery/v2/...``), which is what
    makes the span diagnosable — EXCEPT the push hosts above. Everything else (Mozilla/Windows
    push services, and any host nobody thought about) loses its path: the safe default, because a
    Web Push endpoint is a capability URL and new push hosts appear without warning.
    """
    host = host.lower()
    if host in _PUSH_HOSTS_ON_GOOGLE:
        return False
    return host == "googleapis.com" or host.endswith(".googleapis.com")


def _overwrite_url_attributes(span, safe_url: str, safe_target: str) -> None:
    """Overwrite every URL-carrying attribute PRESENT on ``span`` with its sanitized value.

    Both old ("http.*") and new ("url.*") semconv keys are covered, so a contrib upgrade that
    flips the default convention cannot silently reopen the leak. Only keys the instrumentation
    actually set are touched — a hook must not invent attributes the span never had. Reads the
    SDK span's ``attributes`` mapping; an API-level (non-recording) span has none and exports
    nothing, so it is left alone.
    """
    attributes = getattr(span, "attributes", None)
    if not attributes:
        return
    for key in ("http.url", "url.full"):
        if key in attributes:
            span.set_attribute(key, safe_url)
    for key in ("http.target", "url.path"):
        if key in attributes:
            span.set_attribute(key, safe_target)
    if "url.query" in attributes:
        span.set_attribute("url.query", "")


def _server_request_hook(span, scope) -> None:
    """Strip the query string AND every subscriber id from an inbound server span's URL (R15b).

    The query string was the only thing stripped here at first, which left the sharper leak wide
    open: the id in `/varsel-api/devices/<device_id>` is a path segment, not a parameter, and it
    is a capability token. :func:`_safe_path` handles it. The route template would be the tidier
    source, but `http.route` is not set yet when this hook fires — the ASGI instrumentation opens
    the span before Starlette has matched a route — so the redaction works on the concrete path.

    Never raises: a hook failure inside the middleware would fail the request it observes.
    """
    try:
        attributes = getattr(span, "attributes", None)
        if not attributes:
            return
        full = attributes.get("http.url") or attributes.get("url.full") or ""
        parts = urlsplit(str(full))
        path = _safe_path(parts.path or (scope or {}).get("path", ""))
        safe_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        _overwrite_url_attributes(span, safe_url or path, path)
    except Exception:  # noqa: BLE001, S110 - sanitizing must never fail the request it rides on
        pass


def _requests_request_hook(span, request) -> None:
    """Sanitize an outbound `requests` client span: query always stripped, path only kept for
    hosts where it is an API method rather than a capability token (R15b).

    Never raises: the hook runs inside the instrumented ``Session.send``, where an exception
    would fail the traveller-facing push delivery it is observing.
    """
    try:
        parts = urlsplit(str(getattr(request, "url", "") or ""))
        keep_path = _path_safe_host(parts.hostname or "")
        path = parts.path if keep_path else ""
        safe_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        _overwrite_url_attributes(span, safe_url, path or "/")
    except Exception:  # noqa: BLE001, S110 - sanitizing must never fail the push it rides on
        pass


def _default_exporter():
    """The OTLP/gRPC exporter for telemetry.googleapis.com, authenticated with ADC (KTD9).

    Hand-built channel credentials per Google's migration guide — the dedicated
    ``opentelemetry-exporter-gcp-trace`` package is deprecated in favor of exactly this shape.
    Every import here is lazy on purpose: this function only runs flag-on, inside setup()'s
    fail-open guard.
    """
    import google.auth
    import google.auth.transport.grpc
    import google.auth.transport.requests
    import grpc
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

    credentials, _ = google.auth.default()
    auth_plugin = google.auth.transport.grpc.AuthMetadataPlugin(
        credentials, google.auth.transport.requests.Request()
    )
    channel_creds = grpc.composite_channel_credentials(
        grpc.ssl_channel_credentials(), grpc.metadata_call_credentials(auth_plugin)
    )
    return OTLPSpanExporter(credentials=channel_creds, endpoint=_ENDPOINT)


def _threading_instrumented() -> bool:
    """Is the thread-pool context propagation installed? False when the distribution is absent."""
    try:
        from opentelemetry.instrumentation.threading import ThreadingInstrumentor
    except ImportError:
        return False
    return bool(ThreadingInstrumentor().is_instrumented_by_opentelemetry)


def _instrument_threading() -> None:
    """Carry the current OTel context into ``Thread`` and ``ThreadPoolExecutor`` workers.

    Why it is needed: `varsel.check_and_notify` runs three pools (16 matchers, 16 wording writers,
    32 push senders) and a ThreadPoolExecutor propagates no contextvars, which is where the OTel
    context lives. Without this, every span created in a worker — the Journey Planner and Vertex
    calls the `requests` instrumentation opens, and varsel's own sub-pass children — is an
    independently sampled ROOT span. The trace then shows the pass and nothing underneath it, which
    is precisely the question a capacity run is asking.

    Its OWN nested guard, not folded into :func:`setup`'s one try/except, and the failure direction
    is the reason: this is an optional extra, and an ImportError caught by that outer handler would
    flip ``_enabled`` to False and cost the deployment every span it already had, to lose an
    optimisation. Anything that goes wrong here logs once and leaves tracing on.

    Note what it does: a process-wide monkeypatch of ``threading.Thread.start`` and
    ``ThreadPoolExecutor.submit``. It runs only inside the TRACING_ENABLED gate.
    """
    try:
        from opentelemetry.instrumentation.threading import ThreadingInstrumentor

        ThreadingInstrumentor().instrument()
    except Exception as exc:  # noqa: BLE001 - an optional extra must never disable tracing
        logger.warning(
            "Tracing: thread-pool context propagation unavailable (%s); worker spans will be "
            "separate traces",
            exc,
            extra={"fields": {"event": "tracing_threading_unavailable", "reason": str(exc)}},
        )


def setup(exporter_factory=None) -> None:
    """Install the SDK tracer provider when ``TRACING_ENABLED`` is truthy; otherwise do nothing.

    Fails OPEN (R13): any exception — a missing distribution, no ADC, a bad env value — logs one
    WARNING (``event="tracing_disabled"``) and leaves tracing off while the app serves normally.
    The global provider is set LAST, so a failure part-way through configures nothing at all.

    "Fails open" is about SETUP, and it is worth being precise because the env files used to
    overclaim it: once setup succeeds, a failing EXPORT is not quiet. The OTLP exporter logs
    "Failed to export" at ERROR for every batch it cannot deliver — roughly one every 5 s under
    the default BatchSpanProcessor schedule — and a non-retryable code (PERMISSION_DENIED from a
    missing trace-write role, UNAVAILABLE from a disabled service) never stops recurring. The app
    is unaffected; the ERROR channel is not. That is why the flag ships off in an environment
    whose IAM has not been verified, rather than relying on the failure being harmless.

    ``exporter_factory`` is the test seam: injecting an in-memory exporter is what lets the suite
    exercise the whole enabled path without ever building channel credentials.
    """
    global _enabled
    if os.environ.get("TRACING_ENABLED", "").strip().lower() not in _TRUTHY:
        return
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        attributes = {"service.name": "smart-avvik"}
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
        if project:
            attributes["gcp.project_id"] = project
            # The project suffix, NOT deployment.environment_label(): that helper returns None
            # in prd by design (an unlabelled traveller-facing host), and a resource attribute
            # must always carry a value.
            attributes["environment"] = project.rsplit("-", 1)[-1]
        exporter = (exporter_factory or _default_exporter)()
        # No sampler is constructed: TracerProvider's default reads OTEL_TRACES_SAMPLER /
        # OTEL_TRACES_SAMPLER_ARG natively, which is where Helm sets the per-env policy (R18).
        provider = TracerProvider(resource=Resource.create(attributes))
        # BatchSpanProcessor with its DEFAULT shutdown/flush caps: the known failure mode is a
        # shutdown hang against an unreachable endpoint, and the default 30 s cap bounds it
        # (KTD9) — the pod's termination grace period covers that.
        provider.add_span_processor(BatchSpanProcessor(exporter))
        from opentelemetry.instrumentation.requests import RequestsInstrumentor

        RequestsInstrumentor().instrument(request_hook=_requests_request_hook)
        _instrument_threading()
        # LAST, after the final fallible step, which is what makes the docstring's promise true.
        # Set before the instrumentor — as it was — a contrib/SDK mismatch that made `instrument`
        # raise left the provider installed and its BatchSpanProcessor exporting poller and
        # BigQuery spans, while `_enabled` was False, `instrument_app` no-opped, and the operator
        # read a warning saying tracing was off. Instrumenting first is safe: the API hands out a
        # ProxyTracer that resolves to whatever provider is installed when a span is first created.
        trace.set_tracer_provider(provider)
        _enabled = True
        logger.info(
            "Tracing enabled: OTLP export to %s",
            _ENDPOINT,
            extra={"fields": {"event": "tracing_enabled", "endpoint": _ENDPOINT}},
        )
    except Exception as exc:  # noqa: BLE001 - R13: tracing must never take the app down
        _enabled = False
        # BatchSpanProcessor starts its worker thread in __init__, so a failure after the
        # provider was built leaves one running with nothing to flush. Shutting it down is
        # belt-and-braces beside the ordering above — and must not itself raise here.
        try:
            provider.shutdown()
        except Exception:  # noqa: BLE001, S110 - the provider may not exist yet; nothing to do
            pass
        logger.warning(
            "Tracing disabled: setup failed (%s); the app serves normally with no-op spans",
            exc,
            extra={"fields": {"event": "tracing_disabled", "reason": str(exc)}},
        )


def safe_attributes(attributes) -> dict:
    """``attributes`` reduced to what :data:`SPAN_ATTRIBUTE_NAMES` allows — a FILTER, never a raise.

    The collapse-to-``other`` shape `metrics.HTTPMetrics` uses, for the same reason: this runs on
    the poller thread inside the alerting pass, and an instrumentation exception there would take
    a cycle's alerting with it. So an unknown name or a non-numeric value is dropped silently.

    Silent dropping is the right failure and useless as feedback — an attribute that never arrives
    looks exactly like a span nobody instrumented — which is why `test_tracing_setup` reads the two
    instrumented modules' source and fails on a name this set does not carry.

    ``bool`` passes the ``int`` check (it is a subclass) and is deliberately kept: a flag is a
    bounded value.
    """
    if not attributes:
        return {}
    return {
        key: value
        for key, value in attributes.items()
        if key in SPAN_ATTRIBUTE_NAMES and isinstance(value, (int, float))
    }


@contextmanager
def _measured(cm):
    """``cm``, with the span's own wall and interpreter cost set on it as it closes.

    The cost is only knowable at CLOSE, and the attribute rule is enforced at OPEN — so it is
    applied here, through :func:`safe_attributes`, rather than by letting a caller reach past the
    filter with ``set_attribute``. ``process_time`` is process-wide, so a span whose body fans out
    over a pool can legitimately report more CPU than wall.

    In ``finally`` and inside the ``with``, so a call that fails slowly — the reading worth having —
    still carries its cost, and so the span is still open when the attributes are set.
    """
    started, started_cpu = time.monotonic(), time.process_time()
    with cm as current:
        try:
            yield current
        finally:
            cost = {
                "wall_ms": round((time.monotonic() - started) * 1000),
                "cpu_ms": round((time.process_time() - started_cpu) * 1000),
            }
            for key, value in safe_attributes(cost).items():
                current.set_attribute(key, value)


def span(name: str, attributes=None, *, record_exception: bool = True, measure: bool = False):
    """A context manager for one named span, safe to use whether or not tracing is enabled.

    With no SDK provider installed the OTel API hands out non-recording spans — a few attribute
    lookups and no allocation of note — so callers (the poller's cycle and phase wrappers) never
    need to ask whether tracing is on. The tracer is fetched per call on purpose: it follows a
    provider set or reset after this module imported, and the call is nowhere near hot (one per
    poller phase, ~20 per multi-minute cycle).

    ``attributes`` passes through :func:`safe_attributes`, which is where the span-attribute rule
    is enforced rather than documented.

    ``record_exception=False`` is the store's setting, and the two OTel flags move TOGETHER
    because both carry ``str(exception)``: ``record_exception`` writes it into a span event and
    ``set_status_on_exception`` writes it into the span's status description, so suppressing one
    alone suppresses nothing. A Firestore error message names the document path, and a
    subscription id embeds the client's own reference — the identifier the redaction rule keeps
    out of the record. What it costs is that such a span is not marked failed; the caller's own
    handling logs the failure, and the enclosing phase span still records anything that propagates.

    ``measure=True`` adds ``wall_ms`` and ``cpu_ms`` at close — the store's setting, because KTD2
    sizes the emit pass in interpreter ms per transaction and there is nowhere else to read it.
    Left False the call returns the OTel context manager untouched, so the ~20 phase spans a cycle
    already opens pay nothing for a feature they do not use.
    """
    cm = trace.get_tracer(__name__).start_as_current_span(
        name,
        attributes=safe_attributes(attributes),
        record_exception=record_exception,
        set_status_on_exception=record_exception,
    )
    return _measured(cm) if measure else cm


def instrument_app(app) -> None:
    """Instrument the FastAPI app for inbound request tracing; a no-op while tracing is off.

    Called from main AFTER ``app.add_middleware(metrics.HTTPMetrics)`` — Starlette runs the
    last-added middleware outermost, so the final stack is OTel → metrics → RequireSSO → CORS
    (KTD10) and a request the SSO gate rejects is still traced. Fails open like :func:`setup`:
    an instrumentation error logs and leaves the app serving untraced.
    """
    if not _enabled:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls=_EXCLUDED_URLS,
            server_request_hook=_server_request_hook,
        )
    except Exception as exc:  # noqa: BLE001 - R13 again: never take the app down
        logger.warning(
            "Tracing: FastAPI instrumentation failed (%s); requests serve untraced",
            exc,
            extra={"fields": {"event": "tracing_disabled", "reason": str(exc)}},
        )


setup()
