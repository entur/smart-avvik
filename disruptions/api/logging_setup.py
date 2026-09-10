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
import sys
from datetime import UTC, datetime

# Keys a `fields` dict may not override (the entry's own schema).
_RESERVED = {"timestamp", "severity", "message", "logger"}


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
        return json.dumps(entry, ensure_ascii=False, default=str)


#: Paths whose QUERY STRING is redacted from uvicorn's access log (2026-09-10). The journey-keyed
#: public read takes a traveller's origin, destination and period as query parameters, and the
#: access line — `'GET /path?query HTTP/1.1' 200` — would otherwise put exactly the trio
#: `/historikk-api` refuses to serve into Cloud Logging on every request. A literal here rather
#: than an import from `notification_pull`: this module is imported before every other one in
#: `main`, and the suite pins the two against each other instead.
REDACTED_QUERY_PATHS = ("/notification-api/v1/journey-alerts",)
_REDACTED_QUERY = "?<redacted>"


class _AccessLogRedaction(logging.Filter):
    """Rewrites the full path of a uvicorn access record for :data:`REDACTED_QUERY_PATHS`.

    uvicorn logs `'%s - "%s %s HTTP/%s" %d'` on the `uvicorn.access` logger with
    `args = (client_addr, method, full_path, http_version, status_code)`, so the path with its
    query string is `args[2]`. Only a record of exactly that shape — five args, a str third — is
    touched, and always by building a NEW tuple; anything else passes through untouched. Always
    returns True: this is a rewrite, never a drop.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path, sep, _query = args[2].partition("?")
            if sep and any(path == p or path.startswith(p + "/") for p in REDACTED_QUERY_PATHS):
                record.args = (*args[:2], path + _REDACTED_QUERY, *args[3:])
        return True


def setup() -> None:
    """Route ALL logging — including uvicorn's — through one JSON handler on stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
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


setup()
