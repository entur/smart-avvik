"""Prometheus metric registry, HTTP-metrics middleware and log-level counting for the API.

One rule governs everything declared here: **label values must stay bounded and free of journey
data**. The exporter port (``METRICS_PORT``, default 9464) is readable cluster-wide — anything
that can reach the pod IP can read every series — so nothing that identifies a traveller, a
journey, a stop or a device may become a label value; and every distinct label value is a time
series held in process memory forever and shipped on every scrape, so nothing unbounded (raw URL
paths, situation numbers, error strings) may either. The HTTP middleware therefore collapses
paths to a first-segment allowlist (else ``other``), methods to the known verbs (else ``OTHER``)
and statuses to their class; the log handler collapses non-standard level names the same way.

All metric objects register with prometheus_client's default ``REGISTRY`` at import time, so
they work — and are assertable in tests — without :func:`start` ever running. :func:`start`
(called from main's lifespan, before the BigQuery loads — KTD2: the annotation scraper targets
the pod IP before Ready, so this port is the only window into the up-to-300 s startup) serves
that registry on a dedicated internal port, never the public app port 8080. :func:`configure`
appends the level-counting handler to the root logger; call it after ``logging_setup.setup()``,
and again after any re-setup — ``setup()`` wipes root handlers, this one with them.

This module declares EVERY domain metric (KTD11), so the units that instrument the poller, push
delivery, LLM writing and the decision log only import and increment — none of them mints a name.
"""

import logging
import threading
import time
from wsgiref.simple_server import WSGIServer

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# --- Domain metrics (declared here, incremented by later units) -----------------------------------

POLLER_LAST_TICK = Gauge(
    "kix_poller_last_tick_timestamp_seconds",
    "Epoch seconds of the poller loop's most recent tick, set on every iteration whether or not "
    "SIRI-SX moved. This is the LIVENESS series: alert on `time() - value`, which must stay small "
    "even on a quiet night.",
)
POLLER_LAST_CYCLE = Gauge(
    "kix_poller_last_cycle_timestamp_seconds",
    "Epoch seconds when a full rebuild cycle (main._rebuild_all) last RAN TO THE END. Moves only "
    "when the upstream SIRI-SX ingestion timestamp moves (~3 min p50), so it measures cache "
    "freshness, never liveness — an age alert on it would page on every quiet night. A cycle that "
    "died in one of the steps that propagate does not move it; a step with its own try/except "
    "failing every cycle DOES leave it moving, so pair it with "
    "kix_poller_phase_total{result=\"error\"} rather than reading it as 'every cache is fresh'.",
)
POLLER_CYCLE_DURATION = Histogram(
    "kix_poller_cycle_duration_seconds",
    "Wall-clock duration of one full rebuild cycle (the event=\"poller_cycle\" total, in "
    "seconds). Buckets are tuned for the measured 10-180 s range (prd baseline 2026-08-21: "
    "50-63 s against a ~177 s cycle period), with 600 and 900 added for the load harness: past "
    "300 s every overshoot used to read as a bare +Inf, and how far a cycle runs past the ~295 s "
    "period is exactly the question a capacity run is asking.",
    buckets=(10, 20, 30, 45, 60, 90, 120, 180, 300, 600, 900),
)
POLLER_PHASE_DURATION = Histogram(
    "kix_poller_phase_duration_seconds",
    "Wall-clock duration of one step of the rebuild cycle (the event=\"poller_phase\" ms, in "
    "seconds). `phase` is the step name main._phase was given — a small fixed vocabulary.",
    ["phase"],
    buckets=(0.5, 1, 2, 5, 10, 20, 40, 60, 120),
)
POLLER_PHASE_CPU = Histogram(
    "kix_poller_phase_cpu_seconds",
    "PROCESS CPU seconds burned during one step of the rebuild cycle (the event=\"poller_phase\" "
    "`cpu_ms`, in seconds). The twin of the duration histogram above and the only one that "
    "answers the question capacity work actually asks: a step that takes 9 s of wall clock is "
    "either one CPython thread executing bytecode — which neither a second core nor a wider pool "
    "makes faster, since the GIL serializes it — or a socket wait, which both do. `duration` "
    "cannot tell those apart and `cpu` can. Two readings to keep honest: it is `time.process_time` "
    "over the WHOLE process, so request-path work served concurrently with the phase is included "
    "(the poller shares one interpreter with the API, which is the point rather than a defect); "
    "and it counts every thread, so a 16-wide pool's CPU can exceed the phase's wall clock.",
    ["phase"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 40, 60, 120),
)
POLLER_PHASE_TOTAL = Counter(
    "kix_poller_phase_total",
    "Rebuild-cycle steps by outcome (`result` = ok/error). A failing step is isolated by the "
    "rebuild's per-step try/except, so `error` here means a degraded cache being served, not a "
    "dead poller — the liveness gauge above answers that.",
    ["phase", "result"],
)
PUSH_TOTAL = Counter(
    "kix_push_total",
    "Web Push delivery attempts by outcome (`result` = sent/gone/error), across every push kind "
    "— varsel alerts, togvarsel rounds, test pushes. The kind stays in the structured logs, not "
    "in a label, to keep the series count fixed.",
    ["result"],
)
LLM_MESSAGE_TOTAL = Counter(
    "kix_llm_message_total",
    "LLM wording attempts by backend (`provider`), outcome (`result` = the MessageResult status "
    "vocabulary) and calling surface (`surface` = live/replay/...). Alert rules should select "
    "surface=\"live\": replay bursts from /meldinger are expected during prompt work.",
    ["provider", "result", "surface"],
)
DECISION_WRITE_TOTAL = Counter(
    "kix_decision_write_total",
    "Appends to the disruptions.varsel_decisions log by outcome (`result` = ok/partial/error). "
    "The write fails open — a BigQuery problem never blocks a push — which is exactly why "
    "failures need a counter: the decision log going quiet is otherwise invisible.",
    ["result"],
)
MIRROR_AGE = Gauge(
    "kix_mirror_age_seconds",
    "How far behind the subscription mirror's watermark is, in seconds — the age of the newest "
    "change it has applied (U6, KTD5). This is the series that says whether the poller is matching "
    "the CURRENT population: the pass reads the mirror rather than the collection, so a mirror that "
    "stops syncing keeps alerting a revoked recipient and keeps missing a new one, with every cycle "
    "otherwise looking healthy. Two thresholds worth knowing apart. Warn above roughly three median "
    "cycle periods (~15 min), which is a sync that has failed a few times. The value that matters "
    "for CORRECTNESS is notification_store.MIRROR_MAX_AGE_S (6 h), past which the store re-seeds "
    "itself — because a revocation arrives as a tombstone whose own TTL deletes it after about a "
    "day, so a sync broken for longer than that loses the removal for good. Absent until the first "
    "sync completes, which is the restart window and not a fault.",
)
LABELLING_LAST_SUCCESS = Gauge(
    "kix_labelling_last_success_timestamp_seconds",
    "Epoch seconds when the varsel-label-daily outcome labelling was last observed fresh (set by "
    "the labelling-freshness detector). Absence or a large age means verdicts are not arriving; "
    "past the labelling job's 10-day retry window the loss is permanent.",
)
LOG_RECORDS_TOTAL = Counter(
    "kix_log_records_total",
    "Log records reaching the root logger, by level — counted by the handler configure() "
    "installs, so an ERROR-rate alert needs no log pipeline. Non-standard level names collapse "
    "to OTHER to keep the label bounded.",
    ["level"],
)

# --- HTTP metrics ---------------------------------------------------------------------------------

HTTP_REQUEST_DURATION = Histogram(
    "kix_http_request_duration_seconds",
    "Wall-clock duration of requests on the public app port, by method and first path segment "
    "(allowlisted below, else `other`).",
    ["method", "path"],
)
HTTP_REQUESTS_TOTAL = Counter(
    "kix_http_requests_total",
    "Requests on the public app port, by method, first path segment (allowlisted below, else "
    "`other`) and status class (`2xx`..`5xx`; a request that died before any response started "
    "counts as `5xx`, which is what the server answers for it).",
    ["method", "path", "status"],
)

#: First path segments allowed as a `path` label value: the app's actual routes, routers, static
#: mounts and FastAPI's own doc paths (main.py is the source of truth; /togmelding and /metrics
#: are listed although one is gated on SHOW_RUSHVARSEL and the other lives on the exporter port —
#: a probe for either on 8080 is still that path, and a constant entry costs nothing). Everything
#: else — scanners, typos, paths nobody added here — collapses to "other": on a public
#: unauthenticated port the label vocabulary must be a closed set, or every probe mints a series.
KNOWN_PATH_SEGMENTS = frozenset(
    {
        "alert_profile",
        "assets",
        "auth",
        "demo",
        "demo-api",
        "disruptions",
        "docs",
        "eval",
        "health",
        "historikk",
        "historikk-api",
        "impact",
        "journey",
        "last_ingestion_time",
        "meldinger",
        "meldinger-api",
        "metrics",
        "notification-api",
        "openapi.json",
        "redoc",
        "reisevalg-api",
        "rushhistorikk",
        "rushhistorikk-api",
        "situation",
        "togmelding",
        "togmelding-api",
        "varsel",
        "varsel-api",
    }
)

#: Same closed-set rule for the method label: the verbs the app could meaningfully answer.
#: Anything else a client invents collapses to OTHER.
_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"})


def _path_label(path: str) -> str:
    segment = path.lstrip("/").split("/", 1)[0]
    return segment if segment in KNOWN_PATH_SEGMENTS else "other"


class HTTPMetrics:
    """Pure-ASGI request counting and timing — same shape as sso.RequireSSO, for the same reason:
    no response buffering (BaseHTTPMiddleware wraps every body, which the static mounts and
    /impact should not pay per request).

    Registered in main AFTER RequireSSO — Starlette runs the last-added middleware outermost — so
    a request the SSO gate rejects (302/401/503) is still a served request and is counted with
    its status class. Non-http scopes (lifespan) pass through with the original callables
    untouched.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope["method"] if scope["method"] in _KNOWN_METHODS else "OTHER"
        path = _path_label(scope["path"])
        status_code: int | None = None

        async def send_and_note_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_and_note_status)
        finally:
            # From `finally`, so a handler that raises is counted rather than invisible. No
            # response start seen means exactly that case; the server answers 500 for it, so 5xx.
            elapsed = time.perf_counter() - started
            status = f"{status_code // 100}xx" if status_code is not None else "5xx"
            HTTP_REQUEST_DURATION.labels(method=method, path=path).observe(elapsed)
            HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status=status).inc()


# --- Log-level counting ---------------------------------------------------------------------------

_STANDARD_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


class _CountingHandler(logging.Handler):
    """Counts records by level into ``kix_log_records_total``. Never formats the record — the
    message and args are not touched, so a record whose %-formatting would fail still counts —
    and never raises: a logging call is the last thing that runs on many failure paths, and an
    exception from a handler would surface exactly there.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = record.levelname if record.levelname in _STANDARD_LEVELS else "OTHER"
            LOG_RECORDS_TOTAL.labels(level=level).inc()
        except Exception:  # noqa: BLE001, S110 - deliberately silent, and it cannot log: see docstring
            # Even Handler.handleError is unsafe here — it can itself write to a stream
            # mid-teardown, and logging from a logging handler recurses.
            pass


def configure() -> None:
    """Append the level-counting handler to the root logger. Idempotent — the same install idiom
    as logging_setup's _AccessLogRedaction: probe by type, so repeated calls keep exactly one.

    Call AFTER ``logging_setup.setup()``: setup() wipes root handlers
    (``root.handlers[:] = [handler]``), this one with them, so a re-setup needs a re-configure.
    """
    root = logging.getLogger()
    if not any(isinstance(h, _CountingHandler) for h in root.handlers):
        root.addHandler(_CountingHandler())


def start(port: int) -> tuple[WSGIServer, threading.Thread]:
    """Serve the default REGISTRY on ``port`` (all interfaces, prometheus_client's own daemon
    thread). Called from main's lifespan — never at import, so a suite that imports main without
    entering the lifespan binds nothing. Returns prometheus_client's ``(server, thread)`` pair,
    which is what lets a test bind port 0 and shut the server down again.
    """
    return start_http_server(port)
