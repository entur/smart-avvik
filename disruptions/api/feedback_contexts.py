"""Short-lived opaque handles for exact alert renderings and their durable BQ context."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "disruptions.varsel_feedback_contexts"
_DDL = (Path(__file__).parent / "create_varsel_feedback_contexts.sql").read_text()
_LEASE_S = 15 * 60
_MAX_CONTEXTS = 10_000
_bq_client: Any | None = None
_table_ready = False
_lock = threading.RLock()
_by_id: OrderedDict[str, tuple[float, str, dict[str, Any]]] = OrderedDict()
_by_fingerprint: dict[str, str] = {}


def configure(*, bq_client: Any | None) -> None:
    global _bq_client
    _bq_client = bq_client


def ensure_table() -> bool:
    global _table_ready
    if _bq_client is None:
        return False
    if _table_ready:
        return True
    try:
        _bq_client.query(_DDL).result(timeout=60)
        _table_ready = True
        return True
    except Exception:
        logger.warning("Could not ensure feedback context table", exc_info=True)
        return False


def enabled() -> bool:
    return _bq_client is not None and _table_ready


def _canonical(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _prune(now: float) -> None:
    for context_id, (expires, fingerprint, _) in list(_by_id.items()):
        if expires > now:
            continue
        _by_id.pop(context_id, None)
        if _by_fingerprint.get(fingerprint) == context_id:
            _by_fingerprint.pop(fingerprint, None)


def mint(snapshot: dict[str, Any]) -> str | None:
    """Reuse a capability only for a byte-equivalent live display snapshot."""
    now = monotonic()
    canonical = _canonical(snapshot)
    fingerprint = hashlib.sha256(canonical.encode()).hexdigest()
    with _lock:
        _prune(now)
        existing = _by_fingerprint.get(fingerprint)
        if existing and existing in _by_id:
            _, _, stored = _by_id[existing]
            _by_id[existing] = (now + _LEASE_S, fingerprint, stored)
            _by_id.move_to_end(existing)
            return existing
        if len(_by_id) >= _MAX_CONTEXTS:
            return None  # do not evict a still-live capability
        context_id = secrets.token_hex(16)
        _by_id[context_id] = (now + _LEASE_S, fingerprint, json.loads(canonical))
        _by_fingerprint[fingerprint] = context_id
        return context_id


def resolve(context_id: str) -> dict[str, Any] | None:
    now = monotonic()
    with _lock:
        _prune(now)
        entry = _by_id.get(context_id)
        return dict(entry[2]) if entry else None


def persist(context_id: str) -> tuple[bool, str | None]:
    """Persist the live snapshot before PostHog capture, idempotently by context id."""
    snapshot = resolve(context_id)
    if snapshot is None:
        return False, "expired"
    if not ensure_table():
        return False, "downstream"
    now = datetime.now(UTC)
    row = {
        "context_id": context_id,
        "context_date": now.date().isoformat(),
        "persisted_at": now.isoformat(),
        **snapshot,
    }
    try:
        errors = _bq_client.insert_rows_json(TABLE, [row], row_ids=[context_id])
    except Exception:
        logger.warning("Could not persist feedback context", exc_info=True)
        return False, "downstream"
    if errors:
        logger.warning("Feedback context rejected by BigQuery")
        return False, "downstream"
    return True, None


def _reset_for_tests() -> None:
    global _table_ready
    with _lock:
        _by_id.clear()
        _by_fingerprint.clear()
    _table_ready = False
