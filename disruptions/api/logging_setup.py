"""Golden-path structured logging: one JSON object per line on stdout.

Entur's logging standard (entur-ai/guides/reference/logging.md) requires every service to emit
structured JSON to **stdout** with at least ``timestamp``, ``severity`` and ``message``. Plain
``logging.basicConfig`` writes text to stderr, which GKE's log agent ingests with
``severity: ERROR`` for every line (INFO noise shows up as errors) and splits tracebacks into one
entry per line. This module fixes both.

Importing it configures the root logger — the side effect is deliberate so that ``import
logging_setup`` can precede ``import varsel`` in main.py (varsel logs its VAPID status at import
time) without tripping E402 on a call between imports.

Loggers can attach extra structured fields to an entry via ``extra={"fields": {...}}``; they are
merged into the top-level JSON object (so they land as queryable ``jsonPayload.*`` keys in Cloud
Logging), e.g.::

    logger.info("Push sent", extra={"fields": {"event": "push", "device_id": dev}})
"""

import json
import logging
import os
import sys
import threading
from datetime import UTC, datetime

# Keys a `fields` dict may not override (the entry's own schema).
_RESERVED = {"timestamp", "severity", "message", "logger"}

#: The opentelemetry.trace module once imported, or False after a failed import (R17). Lazy and
#: cached: this module must import before everything else in main and must not require the OTel
#: API to be installed, but re-raising ImportError on every record would be paid per log line.
_OTEL_TRACE = None


def _span_correlation() -> dict:
    """Cloud Logging's trace-correlation keys for the current span; {} outside any valid span.

    Inside a valid span context — sampled or not, Cloud Logging's `trace_sampled` says which —
    the entry gains `logging.googleapis.com/spanId` and `.../trace_sampled`, plus `.../trace` in
    the `projects/<project>/traces/<32-hex>` form ONLY when GOOGLE_CLOUD_PROJECT is set: a
    `projects/None/...` key correlates with nothing and must never be emitted. Wrapped so it can
    NEVER raise — the formatter runs on every record, and a tracing problem taking logging down
    with it would be the observability stack eating itself.
    """
    global _OTEL_TRACE
    if _OTEL_TRACE is False:
        return {}
    try:
        if _OTEL_TRACE is None:
            try:
                from opentelemetry import trace
            except ImportError:
                _OTEL_TRACE = False
                return {}
            _OTEL_TRACE = trace
        ctx = _OTEL_TRACE.get_current_span().get_span_context()
        if not ctx.is_valid:
            return {}
        keys = {
            "logging.googleapis.com/spanId": _OTEL_TRACE.format_span_id(ctx.span_id),
            "logging.googleapis.com/trace_sampled": bool(ctx.trace_flags.sampled),
        }
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if project:
            keys["logging.googleapis.com/trace"] = (
                f"projects/{project}/traces/{_OTEL_TRACE.format_trace_id(ctx.trace_id)}"
            )
        return keys
    except Exception:  # noqa: BLE001 - see docstring: the formatter must never raise
        return {}

# The names LOG_LEVEL accepts (case-insensitive): the stdlib's own name→level mapping, minus
# NOTSET — on the root logger that means "fall back to WARNING", which nobody setting a level
# intends, so it resolves to INFO like any other unrecognised value.
_LEVEL_NAMES = {k: v for k, v in logging.getLevelNamesMapping().items() if k != "NOTSET"}

#: Third-party loggers pinned at INFO whenever LOG_LEVEL asks for DEBUG. LOG_LEVEL lowers the
#: ROOT logger, so it lowers these too — and at DEBUG they publish exactly what the rest of this
#: codebase goes out of its way not to:
#:
#: * ``urllib3.connectionpool`` logs one line per request including the PATH, so every Web Push
#:   becomes ``fcm.googleapis.com "POST /wp/<capability-token>"`` in Cloud Logging — the same
#:   token ``tracing_setup`` refuses to put in a span, and enough to push to that device.
#: * ``anthropic`` / ``httpx`` log request options and bodies, which on the LLM path is the
#:   traveller's origin, destination and departure time — the trio VARSEL_LLM_LOG_PROMPTS exists
#:   to gate, arriving here without anyone setting it.
#: * ``google`` / ``grpc`` add credential-adjacent auth noise at DEBUG.
#:
#: So the privacy interlock in :func:`_resolve_level` guards our OWN prompt record, and this
#: guards the libraries' — without it the interlock closes one door and leaves two open. Debugging
#: an HTTP-level problem in one of these is a deliberate act: raise it by name in a debugger or a
#: throwaway branch, never as a side effect of "let's see the debug logs".
_THIRD_PARTY_DEBUG_MUTED = ("urllib3", "httpx", "httpcore", "anthropic", "google", "grpc")


def _resolve_level() -> tuple[int, bool]:
    """The root level LOG_LEVEL asks for, and whether the privacy interlock refused DEBUG.

    LOG_LEVEL is a standard level name, case-insensitive; unset or unrecognised means INFO.
    ONE interlock (KTD12): DEBUG is refused while VARSEL_LLM_LOG_PROMPTS is truthy — at DEBUG
    that flag writes the full LLM prompt, which carries the traveller's origin, destination and
    departure time, to Cloud Logging (llm_message._LOG_PROMPTS). Turning on traveller-prompt
    logging must require deliberately touching the privacy flag in the same change as the
    verbosity, never arrive as a side effect of "let's see the debug logs". The truthy set is
    llm_message's own parse of that flag, kept identical so the two reads cannot disagree.
    """
    raw = os.environ.get("LOG_LEVEL", "").strip().upper()
    level = _LEVEL_NAMES.get(raw, logging.INFO)
    prompts = os.environ.get("VARSEL_LLM_LOG_PROMPTS", "").lower() in ("1", "true", "yes")
    if level <= logging.DEBUG and prompts:
        return logging.INFO, True
    return level, False


class JsonFormatter(logging.Formatter):
    """Formats a record as a single-line JSON object with GCP-recognised keys."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "severity": record.levelname,
            "message": message,
            "logger": record.name,
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update({k: v for k, v in fields.items() if k not in _RESERVED})
        # Applied AFTER the fields merge, so the true span identity wins over anything a fields
        # dict happens to spell with these keys.
        entry.update(_span_correlation())
        return json.dumps(entry, ensure_ascii=False, default=str)


#: Paths whose QUERY STRING is redacted from uvicorn's access log (2026-09-10). The journey-keyed
#: public read takes a traveller's origin, destination and period as query parameters, and the
#: access line — `'GET /path?query HTTP/1.1' 200` — would otherwise put exactly the trio
#: `/historikk-api` refuses to serve into Cloud Logging on every request. A literal here rather
#: than an import from `notification_pull`: this module is imported before every other one in
#: `main`, and the suite pins the two against each other instead.
REDACTED_QUERY_PATHS = ("/notification-api/v1/journey-alerts",)
_REDACTED_QUERY = "?<redacted>"


def _access_request_path(record: logging.LogRecord) -> str | None:
    """The full request path of a uvicorn access record, or None for any other record shape.

    uvicorn logs `'%s - "%s %s HTTP/%s" %d'` on the `uvicorn.access` logger with
    `args = (client_addr, method, full_path, http_version, status_code)`, so the path with its
    query string is `args[2]`. Only a record of exactly that shape — five args, a str third —
    qualifies; both access-log filters below share this one statement of the contract.
    """
    args = record.args
    if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
        return args[2]
    return None


class _AccessLogRedaction(logging.Filter):
    """Rewrites the full path of a uvicorn access record for :data:`REDACTED_QUERY_PATHS`.

    Only a record matching :func:`_access_request_path`'s contract is touched, and always by
    building a NEW tuple; anything else passes through untouched. Always returns True: this is
    a rewrite, never a drop.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        full_path = _access_request_path(record)
        if full_path is not None:
            path, sep, _query = full_path.partition("?")
            if sep and any(path == p or path.startswith(p + "/") for p in REDACTED_QUERY_PATHS):
                record.args = (*record.args[:2], path + _REDACTED_QUERY, *record.args[3:])
        return True


class _HealthAccessMute(logging.Filter):
    """Drops uvicorn access records for the /health probe (R9).

    GKE's liveness and readiness probes hit /health every few seconds, so every real request in
    the access log sits between probe lines — noise that costs ingestion and buries the lines
    worth reading. Only a record matching :func:`_access_request_path`'s contract is examined;
    anything else passes. The path must be EXACTLY ``/health`` (query string ignored) — never a
    prefix match, which would silently hide a future ``/healthz`` from the log.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        full_path = _access_request_path(record)
        return not (full_path is not None and full_path.partition("?")[0] == "/health")


def _log_thread_exception(args: threading.ExceptHookArgs) -> None:
    """``threading.excepthook`` replacement: one structured ERROR per dying thread.

    The interpreter's default prints the traceback raw to stderr, which GKE ingests split into
    one entry per line — for the poller thread that is the only record of why every push and
    cache refresh stopped, and it is unfindable. SystemExit is ignored exactly as threading's own
    default ignores it (a thread asking to stop is not a death). Never re-raises: the thread is
    already gone, and an exception from a hook would land on the stderr path this module exists
    to avoid.
    """
    if args.exc_type is SystemExit:
        return
    thread_name = args.thread.name if args.thread is not None else "<unknown>"
    logging.getLogger(__name__).error(
        "Thread %r died from an uncaught exception",
        thread_name,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        extra={"fields": {"event": "thread_died", "thread": thread_name}},
    )


def setup() -> None:
    """Route ALL logging — including uvicorn's — through one JSON handler on stdout."""
    # LOG_LEVEL (R12): standard names, case-insensitive, default INFO. DEBUG is refused while
    # VARSEL_LLM_LOG_PROMPTS is truthy — the privacy interlock _resolve_level documents — and
    # the refusal is logged as a structured ERROR below, once the JSON handler is in place.
    level, conflict = _resolve_level()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Lowering the root lowers every library with it — see _THIRD_PARTY_DEBUG_MUTED for what they
    # publish at DEBUG. Pinned only when DEBUG was actually asked for, so the ordinary INFO
    # deployment keeps whatever these loggers would do by default.
    if level <= logging.DEBUG:
        for name in _THIRD_PARTY_DEBUG_MUTED:
            logging.getLogger(name).setLevel(logging.INFO)
    # uvicorn installs its own non-propagating text handlers (error log on stderr!) before it
    # imports the app; strip them so its records — access log included — propagate to root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers[:] = []
        uv_logger.propagate = True
    # On the access logger itself, not on root: a logger's filters see only records logged ON it,
    # and uvicorn logs access lines on this one directly. Installed once, however often setup runs.
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _AccessLogRedaction) for f in access.filters):
        access.addFilter(_AccessLogRedaction())
    if not any(isinstance(f, _HealthAccessMute) for f in access.filters):
        access.addFilter(_HealthAccessMute())
    # A dying thread leaves one structured ERROR (event="thread_died") instead of a raw stderr
    # traceback. Assigning the module-level function is idempotent across setup() re-runs.
    threading.excepthook = _log_thread_exception
    if conflict:
        logging.getLogger(__name__).error(
            "LOG_LEVEL=DEBUG refused: VARSEL_LLM_LOG_PROMPTS is enabled, and DEBUG would put "
            "travellers' prompts (origin, destination, departure time) into Cloud Logging; "
            "running at INFO. Unset VARSEL_LLM_LOG_PROMPTS in the same change that lowers "
            "LOG_LEVEL.",
            extra={
                "fields": {
                    "event": "log_level_conflict",
                    "log_level": os.environ.get("LOG_LEVEL"),
                    "varsel_llm_log_prompts": os.environ.get("VARSEL_LLM_LOG_PROMPTS"),
                }
            },
        )


setup()
