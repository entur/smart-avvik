"""Short-lived opaque handles for exact alert renderings and their durable BQ context."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any

logger = logging.getLogger(__name__)

TABLE = "disruptions.varsel_feedback_contexts"
# The JSON-typed columns of TABLE. insert_rows_json streams through the legacy insertAll API, which
# reads a native dict against a JSON column as a record and rejects the row ("This field: policy is
# not a record") — every context write failed this way until 2026-08-27, invisibly, because the
# failure path only logs and the traveller's rating still reaches PostHog. A None stays None: a
# JSON column holds SQL NULL that way, while json.dumps(None) would store a JSON `null` literal
# that `IS NOT NULL` matches and /meldinger-style "has input" tests would read as present.
_JSON_COLUMNS = ("journey_context", "message_input", "prediction", "policy", "delivery")
_DDL = (Path(__file__).parent / "create_varsel_feedback_contexts.sql").read_text()
_LEASE_S = 15 * 60
_MAX_CONTEXTS = 10_000
#: After a failed store write, how long this pod mints memory-only without asking. A card render
#: mints per alert, serially, and each write waits out the store's deadline when the store is down.
_STORE_BACKOFF_S = 10.0
_store_down_until = 0.0
_bq_client: Any | None = None
_table_ready = False
_lock = threading.RLock()
_by_id: OrderedDict[str, tuple[float, str, dict[str, Any]]] = OrderedDict()
_by_fingerprint: dict[str, str] = {}
_persisted: set[str] = set()
#: Where contexts live across pods (zero-downtime U6, KTD10): `notification_store`, injected by
#: `main` ONLY under `VARSEL_STATE_BACKEND=store` -- the lease's own gate (KTD5). Never on
#: `notification_store.enabled()`, which is true in tst where the named database does not exist.
#: None is today's single-process path, byte for byte: memory only, a sliding lease, no claims.
_store: Any | None = None
#: Ids minted while the store write failed. They live in this pod's memory only, so this pod
#: resolves and claims them from memory -- the store has never heard of them and would say expired.
_memory_only: set[str] = set()


def configure(*, bq_client: Any | None, store: Any | None = None) -> None:
    global _bq_client, _store
    _bq_client = bq_client
    _store = store


class StoreUnavailable(Exception):
    """The store could not be read. Retryable, so never answered as an expired card."""


def _from_memory(context_id: str) -> bool:
    return _store is None or context_id in _memory_only


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
    # Table readiness is retryable in persist(); configuration availability is what controls UI.
    return _bq_client is not None


def _canonical(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _prune(now: float) -> None:
    while _by_id:
        context_id, (expires, fingerprint, _) = next(iter(_by_id.items()))
        if expires > now:
            break
        _by_id.pop(context_id, None)
        _persisted.discard(context_id)
        _memory_only.discard(context_id)
        if _by_fingerprint.get(fingerprint) == context_id:
            _by_fingerprint.pop(fingerprint, None)


def mint(
    snapshot: dict[str, Any], *, device_id: str | None = None, journey_id: str | None = None
) -> str | None:
    """Reuse a capability only for a byte-equivalent live display snapshot from the same device.

    The device is in the fingerprint so two travellers never share a context: under the store
    backend «Slett alt» deletes a device's contexts, and a shared one would be either deleted from
    under the other traveller or left holding the erased one's route. The rush card mints with no
    device (it carries no route), so its context stays shared, and is deleted by expiry alone.
    The journey is in it too, because the store's claim refuses a context whose journey is gone.
    """
    global _store_down_until
    now = monotonic()
    canonical = _canonical(snapshot)
    material = canonical if device_id is None else f"{device_id}\0{journey_id}\0{canonical}"
    fingerprint = hashlib.sha256(material.encode()).hexdigest()
    with _lock:
        _prune(now)
        existing = _by_fingerprint.get(fingerprint)
        if existing and existing in _by_id:
            expires, _, stored = _by_id[existing]
            if _from_memory(existing):
                _by_id[existing] = (now + _LEASE_S, fingerprint, stored)
                _by_id.move_to_end(existing)
                return existing
            # The store's document does NOT slide -- refreshing it would be a write per render,
            # the cost the memory cache exists to save -- so reuse is bounded by its expiry, and
            # stops at half the lease so a reused id still promises a traveller 7.5 minutes to tap.
            # Past that, a fresh id below; the old entry is left for `_prune`.
            if expires - now > _LEASE_S / 2:
                return existing
        # Under the store a full cache costs only reuse: the document still serves the tap. In
        # memory alone the cache IS the capability, and a still-live one is never evicted.
        full = len(_by_id) >= _MAX_CONTEXTS
        if full:
            logger.warning(
                "Feedback context cache is full",
                extra={"fields": {"event": "varsel_feedback_context", "result": "full"}},
            )
            if _store is None:
                return None
        context_id = secrets.token_hex(16)
    memory_only = _store is not None and now < _store_down_until
    if _store is not None and not memory_only:
        # Outside the lock: a Firestore write is a network round trip, and every card render on
        # every request thread takes this lock.
        try:
            _store.write_feedback_context(
                context_id,
                snapshot=canonical,
                device_id=device_id,
                journey_id=journey_id,
                expires_at=datetime.now(UTC) + timedelta(seconds=_LEASE_S),
            )
        except Exception:
            # Never into a card render: the widget still works on this pod, from memory.
            logger.warning(
                "Could not store feedback context; serving it from this pod only",
                exc_info=True,
                extra={"fields": {"event": "varsel_feedback_context", "result": "store_failed"}},
            )
            memory_only = True
            _store_down_until = monotonic() + _STORE_BACKOFF_S
    if full:
        # Memory-only with a full cache: nowhere to hold it, so no widget -- as before.
        return None if memory_only else context_id
    with _lock:
        _by_id[context_id] = (now + _LEASE_S, fingerprint, json.loads(canonical))
        _by_fingerprint[fingerprint] = context_id
        if memory_only:
            _memory_only.add(context_id)
        return context_id


def resolve(context_id: str) -> dict[str, Any] | None:
    """The snapshot a first tap rates, or None. Under the store backend this reads the store on
    EVERY call, never memory first: an erasure on another pod is only visible there. A failed read
    raises :class:`StoreUnavailable` -- None would tell the traveller the card had expired."""
    now = monotonic()
    with _lock:
        _prune(now)
        if _from_memory(context_id):
            entry = _by_id.get(context_id)
            return dict(entry[2]) if entry else None
    try:
        raw = _store.read_feedback_context(context_id)
    except Exception:
        logger.warning(
            "Could not read feedback context",
            exc_info=True,
            extra={"fields": {"event": "varsel_feedback_context", "result": "read_failed"}},
        )
        raise StoreUnavailable(context_id) from None
    return json.loads(raw) if raw is not None else None


def claim(context_id: str, response_id: str) -> str:
    """Take the first tap of `response_id` on this context: ``claimed``, ``duplicate`` or
    ``missing``. Without the store it is always ``claimed`` -- today's behaviour, where the single
    pod's own limiter is the only guard. A store failure also admits: the BigQuery row is keyed by
    context id and PostHog dedupes on the event id, so failing open costs at worst a double count,
    where failing closed would lose the traveller's answer."""
    if _from_memory(context_id):
        return "claimed"
    try:
        return _store.claim_feedback_response(context_id, response_id=response_id)
    except Exception:
        logger.warning(
            "Could not claim feedback response; admitting it",
            exc_info=True,
            extra={"fields": {"event": "varsel_feedback_context", "result": "claim_failed"}},
        )
        return "claimed"


def release(context_id: str, response_id: str) -> None:
    """Undo :func:`claim` after a downstream failure, so the front-end's retry -- same response id
    -- is admitted rather than answered `throttled` for good."""
    if _from_memory(context_id):
        return
    try:
        _store.release_feedback_response(context_id, response_id=response_id)
    except Exception:
        logger.warning(
            "Could not release feedback claim",
            exc_info=True,
            extra={"fields": {"event": "varsel_feedback_context", "result": "release_failed"}},
        )


def persist(context_id: str, *, context_date: str | None = None) -> tuple[bool, str | None]:
    """Persist the live snapshot before PostHog capture, idempotently by context id."""
    try:
        snapshot = resolve(context_id)
    except StoreUnavailable:
        return False, "downstream"
    if snapshot is None:
        return False, "expired"
    with _lock:
        if context_id in _persisted:
            return True, None
    if not ensure_table():
        return False, "downstream"
    now = datetime.now(UTC)
    row = {
        "context_id": context_id,
        "context_date": context_date or now.date().isoformat(),
        "persisted_at": now.isoformat(),
        **snapshot,
    }
    for column in _JSON_COLUMNS:
        if row.get(column) is not None:
            row[column] = json.dumps(row[column], ensure_ascii=False, default=str)
    try:
        errors = _bq_client.insert_rows_json(TABLE, [row], row_ids=[context_id], timeout=5)
    except Exception:
        logger.warning("Could not persist feedback context", exc_info=True)
        return False, "downstream"
    if errors:
        logger.warning("Feedback context rejected by BigQuery")
        return False, "downstream"
    with _lock:
        _persisted.add(context_id)
    return True, None


def _reset_for_tests() -> None:
    global _table_ready, _store, _store_down_until
    with _lock:
        _by_id.clear()
        _by_fingerprint.clear()
        _persisted.clear()
        _memory_only.clear()
    _table_ready = False
    _store = None
    _store_down_until = 0.0
