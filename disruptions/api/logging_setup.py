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
from datetime import datetime, timezone

# Keys a `fields` dict may not override (the entry's own schema).
_RESERVED = {"timestamp", "severity", "message", "logger"}


class JsonFormatter(logging.Formatter):
    """Formats a record as a single-line JSON object with GCP-recognised keys."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
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


setup()
