"""Where a `/varsel` registration lives, behind one seam with two backends.

`varsel` held its registrations in five module dicts and persisted them as ONE GCS blob, rewritten
whole on every device upsert, journey create and journey delete. That is the right shape for the
~200 registrations prd carries and the wrong one for the 100 000 the plan sizes for: the blob is
read once at startup and written whole on the request path, so a write is O(registrations) and two
concurrent writes are a lost update. U12 put the durable half in Firestore, one document per device
and per journey; this module is the seam that lets `varsel` be written against either.

**Two backends, one call sequence.** :class:`BlobRegistrations` is today's dicts and today's blob,
moved here unchanged. The store backend arrives in U13's second PR. The protocol below is what both
answer, and it is deliberately shaped as the CALLER's question rather than as either backend's
primitives: `erase_device(device_id)` is a method, while "pop the device, pop its journeys, pop
their marks, and put them all back if the write failed" is not -- the blob does that under one lock
and the store does it as a paged transaction, and a seam that named the steps would be the blob's
implementation wearing a protocol's clothes.

**`save()` is the one asymmetry, and it is stated rather than hidden.** The blob BATCHES: its
mutators change memory and `save()` is the single write that makes them durable. The store does
not: each mutator is its own document write and `save()` has nothing to flush, so it returns True.
A caller therefore mutates and then calls `save()`, and that one sequence is correct on both --
costing a blob upload on one and nothing at all on the other. It is the reason the request path
stops writing the blob at the cutover without a single call site moving.

**This module must not import `varsel`** (pinned by a source-inspection test in
`test_notification_contract.py`, both ways). `varsel` imports it, hands its own policy -- the
ceilings, the wording, the cap -- around it, and does its own logging: nothing here emits a
`varsel_*` event, because the event vocabulary is the caller's and a backend swap must not change
one line of it.

**What deliberately did NOT move**: `_messages`, `_alternatives`, `_message_attempts`,
`_message_errors`, `_message_meta`, `_stop_aliases`, `_decision_ledger`. Those are per-process
caches of work done about a registration, not the registration -- they are rebuilt every cycle, a
restart is meant to forget them, and putting them behind a durable seam would be paying Firestore
for a snapshot whose whole value is being thrown away.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import notification_store  # the store backend's collections (U12); never `varsel`

logger = logging.getLogger("kix.varsel_state")


# ---- What a caller reads about one registration's pushes --------------------------------------
@dataclass(frozen=True)
class PushLedger:
    """One registration's push marks, read TOGETHER because the delivery loop asks about all three.

    Three questions per (registration, train): has this situation already been pushed, has this
    TRAIN already been pushed (so the new notification replaces it on the lock screen rather than
    adding to the pile), and how many alert pushes this Oslo day has this registration spent. Under
    the blob they are three dicts and the grouping costs nothing; under the store they are three
    fields of ONE journey document, so asking them separately would be three reads per push where
    U12's `_web_push_journey` returns all of it in one. That is why this is a value object rather
    than three accessors -- the shape is the store's, chosen before the store exists so the second
    PR does not have to re-cut the protocol it was supposed to settle.

    `day` and `sent_today` describe the ledger AS STORED: a count from a previous Oslo day reads as
    that day with its own count, and it is the caller that decides a stale day means zero spent.
    Deciding it here would need this module to know which day "now" is, which is the caller's clock.

    **The blob answers this by scanning, and that cost is accepted rather than hidden.** Its marks
    are flat maps keyed `"<journey_id>|..."`, so one ledger is a pass over all of them where the
    old loop's three membership tests were O(1) -- a real regression in the shape, bounded by three
    things that make it not one in practice. The caller reads a ledger only for a registration that
    has an alert-worthy train this cycle, which on an ordinary cycle is almost none of them; prd
    carries ~200 registrations against a few hundred marks; and the blob backend retires at U14,
    where the store answers the same call with one document read and the scan does not exist. If
    the blob is still here when either number grows, the fix is a bulk `push_ledgers(journey_ids)`
    -- one grouping per pass, which is what the old loop effectively had -- and NOT a per-journey
    index, which would be a second copy of the marks to keep true.
    """

    notified: frozenset[str]
    pushed_trains: frozenset[str]
    day: str | None
    sent_today: int


@dataclass(frozen=True)
class Erasure:
    """What «Slett alt» did: whether there was ANYTHING to erase, how many journeys went, and
    whether the removal is DURABLE.

    `found` is not "a device record existed": a lone tombstone with no record and no journeys is
    something to erase too, and the caller logs the erasure for it. False means the id was unknown
    in every one of the three places, which is the path that does no I/O at all.

    `durable` is the one the route reads -- an art. 17 erasure answering 200 while the durable copy
    still holds the rows has them resurrected by the next restart, and by then the client has
    cleared its device id, so what came back is unreachable by the person it belongs to."""

    found: bool
    journeys_deleted: int
    durable: bool


class CapacityExceeded(Exception):
    """A ceiling refused this registration. `kind` is `"devices"` or `"journeys"`.

    RAISED by the backend rather than pre-checked by the caller, which is U13's one change to the
    protocol step 1 shipped. `device_count()` and `journey_count()` were on it, and a count the
    caller compares against a constant is two things the store cannot do: the ceiling is the
    CLIENT's quota there, enforced inside the create transaction where U7 put it precisely so that
    "bounded" and "affordable" stop being the same argument -- and a read-then-compare outside the
    transaction is a race the blob got away with because it held a lock across both halves.

    `kind` is carried because `/varsel` logs `varsel_capacity` with it and an operator selects on
    it; the two ceilings are different numbers with different causes.
    """

    def __init__(self, kind: str) -> None:
        super().__init__(f"the {kind} ceiling refused this registration")
        self.kind = kind


class RegistrationStore(Protocol):
    """What `/varsel` needs of wherever registrations live.

    Every method is a question the request path or the cycle actually asks. Ordering matters in one
    place only and it is documented on :meth:`save`.
    """

    # ---- the request path
    def get_device(self, device_id: str) -> dict | None: ...
    def devices_for(self, device_ids: Iterable[str]) -> dict[str, dict]: ...
    def put_device(self, device_id: str, *, subscription: dict, brand: str) -> None: ...
    def forget_device(self, device_id: str, *, reason: str) -> tuple[dict | None, int]: ...
    def device_journeys(self, device_id: str) -> list[tuple[str, dict]]: ...
    def create_journey(self, journey_id: str, record: dict) -> None: ...
    def delete_journey(self, journey_id: str) -> dict | None: ...
    def erase_device(self, device_id: str) -> Erasure: ...

    # ---- the cycle
    def all_journeys(self) -> list[tuple[str, dict]]: ...
    def get_journey(self, journey_id: str) -> dict | None: ...
    def set_canonical_stops(self, journey_id: str, *, from_stop: str, to_stop: str) -> bool: ...
    def stamp_service_check(
        self, journey_id: str, *, checked_ts: str, cleared: bool, reverse_only: bool
    ) -> bool: ...

    # ---- the push side
    def notified_snapshot(self, journey_ids: Iterable[str]) -> set[str]: ...
    def push_ledger(self, journey_id: str) -> PushLedger: ...
    def mark_notified(self, journey_id: str, situation_number: str) -> None: ...
    def record_push(
        self,
        journey_id: str,
        *,
        tag: str | None,
        departure: datetime | None,
        oslo_day: str,
        charge_cap: bool,
    ) -> None: ...
    def prune(
        self, *, live_situations: set[str], trains_gone_by: datetime, oslo_today: str
    ) -> int: ...

    # ---- persistence
    def save(self) -> bool: ...
    def load(self) -> None: ...


# ---- The blob backend's state ------------------------------------------------------------------
# Module level, not instance attributes, and that is load-bearing three times over: there is one
# blob per process, `varsel` aliases these names so every existing test keeps reading the state it
# always read, and a suite that constructs a second `BlobRegistrations` must get the SAME state
# rather than a private fork of it. The class is a thin façade over these; it holds no data.
#
# Re-entrant lock: mutations call other mutations while already holding it.
lock = threading.RLock()
_devices: dict[
    str, dict
] = {}  # device_id -> {"subscription": {...}, "created_ts": iso}
_journeys: dict[str, dict] = {}  # journey_id -> journey record (see POST /journeys)
_notified: set[str] = (
    set()
)  # {"journey_id|situationNumber"} — dedup, pruned when a situation ends
# Why a device record went away while its journeys stayed (2026-09-04):
# device_id -> {"reason": "expired" | "turned_off", "ts": iso, "created_ts": <the record's original
# signup>, "journeys": <count left behind>}. Two writers, `_drop_device` (the push service answered
# 404/410) and `delete_device` («Skru av»), and it is what tells those two apart from the third and
# — measured in prd on 2026-09-04 — much larger population: a journey registered by a device that
# never turned notifications on at all, which has no record to lose and so no tombstone. Nothing
# durable held that distinction before, so the device report could only guess. Written only when
# journeys remain (a record with none leaves nothing to explain), removed when the device
# re-registers (`put_device`, which also restores the original `created_ts` so a lapse does not
# read as a new signup), when its last journey is deleted, and by «Slett alt» — an erasure keeps no
# marker of what it erased. Persisted in the state blob beside the journeys it annotates.
_device_tombstones: dict[str, dict[str, Any]] = {}
# Trains a push has already reached each REGISTRATION about, {"<journey_id>|<train key>": departure
# (UTC)} -- design review 2026-09-01, findings 14 and 15. Two readers. `_notification_title` prefixes
# a later push about the same train with «Bekreftet:» (the 65 % forecast and «er innstilt» used to
# arrive as two unrelated notifications about one train), and the delivery loop exempts such a push
# from the daily cap, because it REPLACES the earlier one on the lock screen (same notification tag)
# rather than adding to the pile. In memory only: a restart forgets it, which costs at most one
# missing prefix and one extra counted push, while persisting it would grow the state blob by a key
# per train. Pruned by departure time -- a train is done being pushed about once it has left.
_pushed_trains: dict[str, datetime] = {}
# Alert pushes sent per REGISTRATION on the current Oslo day, journey_id -> ("YYYY-MM-DD", count):
# the ledger behind `varsel._MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY`. In memory for the same reason as
# the dict above -- a restart resets the count, which errs towards sending.
_daily_pushes: dict[str, tuple[str, int]] = {}
# Togvarsel push round markers, {"<YYYY-MM-DD Oslo>": {"<issue>": {"level": str, "pushed": bool}}} —
# togvarsel_notify's single state primitive (dedup + the follow-up baseline in one). Persisted in
# the state blob so a completed round never re-fires across restarts. Exactly one date is kept:
# `store_rounds` prunes to the date it is given, which is how day rollover forgets yesterday.
# Deliberately NOT on the protocol: a rush-hour round marker is not a registration, and forcing the
# store backend to grow a `togvarsel_rounds` document to satisfy this interface would be inventing a
# reader for it. It rides in the blob half, and `varsel`'s injected accessor pair is what
# togvarsel_notify sees either way.
_togvarsel_rounds: dict[str, dict[str, Any]] = {}
# Unknown top-level keys in the loaded blob, carried through apply→serialize verbatim (rollback
# insurance): a key written by a future feature must survive THIS build rewriting the blob, so a
# rollback-and-forward does not silently erase that feature's state.
_state_extra: dict[str, Any] = {}
_STATE_KEYS = frozenset({"devices", "journeys", "notified", "togvarsel_rounds", "device_tombstones"})

_STATE_PATH = Path(os.environ.get("VARSEL_STATE_PATH", "/tmp/varsel_state.json"))
_STATE_BUCKET = os.environ.get(
    "VARSEL_STATE_BUCKET"
)  # GCS bucket for durable state (optional)
_STATE_BLOB = os.environ.get("VARSEL_STATE_BLOB", "varsel_state.json")


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat()


def _iso_to_dt(value: str) -> datetime | None:
    """An ISO-8601 stamp back to an aware instant, or None when it cannot be read.

    `/varsel` passes its service-check stamp as the string its own record carries; the store takes
    an instant. A value this cannot parse means the caller has no clock to offer, and the store's
    own `now` is the right answer then -- never an invented one.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def serialize_state() -> str:
    with lock:
        return json.dumps(
            {
                **_state_extra,  # unknown keys ride along verbatim (rollback insurance)
                "devices": _devices,
                "journeys": _journeys,
                "notified": sorted(_notified),
                "togvarsel_rounds": _togvarsel_rounds,
                "device_tombstones": _device_tombstones,
            }
        )


def apply_state(raw: str) -> None:
    """Replace the in-memory state with a loaded blob's, IN PLACE.

    In place rather than by rebinding, which is what this did while it was the only reader of its
    own globals: `varsel` now aliases these objects, so a rebind here would leave that module — and
    every test that reads through it — pointing at the dict this function had just orphaned. The
    aliasing is temporary (it retires with the blob backend); mutating in place is correct either
    way, and rebinding was never anything but incidental.
    """
    data = json.loads(raw)
    with lock:
        _devices.clear()
        _devices.update(data.get("devices", {}))
        _journeys.clear()
        _journeys.update(data.get("journeys", {}))
        _notified.clear()
        _notified.update(data.get("notified", []))
        _togvarsel_rounds.clear()
        _togvarsel_rounds.update(data.get("togvarsel_rounds", {}))
        _device_tombstones.clear()
        _device_tombstones.update(data.get("device_tombstones", {}))
        _state_extra.clear()
        _state_extra.update({k: v for k, v in data.items() if k not in _STATE_KEYS})


_gcs_client = None
_gcs_client_lock = threading.Lock()


def _gcs_blob():
    """The state blob handle when a bucket is configured, else None (import is lazy).

    The client is built once and reused. Constructing a fresh `storage.Client()` per call meant every
    device registration and every journey delete paid an ADC resolution plus a metadata-server token
    fetch before the upload — on the request path, under the state lock.
    """
    global _gcs_client
    if not _STATE_BUCKET:
        return None
    from google.cloud import storage

    with _gcs_client_lock:
        if _gcs_client is None:
            _gcs_client = storage.Client()
    return _gcs_client.bucket(_STATE_BUCKET).blob(_STATE_BLOB)


def load_state() -> None:
    """Load persisted state at startup — from GCS when configured, else the local file.

    Tolerant: a missing/corrupt store just starts empty (logged), so a first-ever run or a fresh
    bucket doesn't block serving."""
    try:
        blob = _gcs_blob()
        if blob is not None:
            if blob.exists():
                apply_state(blob.download_as_text())
                logger.info(
                    "Loaded varsel state from gs://%s/%s: %d devices, %d journeys",
                    _STATE_BUCKET,
                    _STATE_BLOB,
                    len(_devices),
                    len(_journeys),
                )
            else:
                logger.info(
                    "No varsel state blob yet at gs://%s/%s", _STATE_BUCKET, _STATE_BLOB
                )
            return
        if _STATE_PATH.exists():
            apply_state(_STATE_PATH.read_text())
            logger.info(
                "Loaded varsel state from %s: %d devices, %d journeys",
                _STATE_PATH,
                len(_devices),
                len(_journeys),
            )
    except Exception:
        logger.exception("Could not load varsel state; starting empty")


def read_blob_text() -> str | None:
    """The persisted state exactly as stored, or None when there is none — STRICT about the
    difference between "no blob" and "could not read one".

    :func:`load_state` is tolerant on purpose: it swallows everything and starts empty, because
    serving without state beats not serving at all. That is exactly the wrong rule for the cutover
    import, which writes a one-shot marker afterwards — reading a transient GCS failure as "there
    was nothing to import" would mark the migration complete over a blob full of registrations
    nobody ever read. So this one raises, and the caller fails the lifespan.
    """
    blob = _gcs_blob()
    if blob is not None:
        return blob.download_as_text() if blob.exists() else None
    return _STATE_PATH.read_text() if _STATE_PATH.exists() else None


def write_blob_text(payload: str) -> None:
    """Replace the persisted state with this exact text. U14's export, and nothing else.

    Separate from :func:`save_state`, which serialises the blob backend's own module dicts: under
    the store backend those are empty, so `save_state` would write an empty blob over the rollback
    artefact -- which is the one failure an export must not have.
    """
    blob = _gcs_blob()
    if blob is not None:
        blob.upload_from_string(payload, content_type="application/json")
        return
    _STATE_PATH.write_text(payload)


def save_state() -> bool:
    """Write state through to the local file and (when configured) the GCS blob. Best-effort:
    a persistence failure is logged, not raised — the in-memory state is still authoritative.

    Returns whether the DURABLE copy is up to date: True when the GCS upload succeeded, and True
    when no bucket is configured — there is then no durable copy to be stale, which is the
    documented local-only dev mode (`VARSEL_STATE_BUCKET` unset). False only when a *configured*
    upload actually failed, `_gcs_blob` itself blowing up on ADC included.

    The return value is backward compatible: nothing raises, the local-file write keeps its own
    swallow-and-log, and every caller but the erasure ignores it. That one is the single place where
    "logged, not raised" is wrong — a GDPR art. 17 erasure that answers 200 while the blob still
    holds the data has it resurrected by the next restart's :func:`load_state`, and by then the
    client has cleared its device id, so the resurrected rows are unreachable by the person they
    belong to.

    **This is the one function a suite patches to make persistence fail or to count writes.** It was
    `varsel._save_state` until the seam existed, and there were then two ways to reach the blob —
    that name and the backend's own call — so a patch on one left the other writing. Everything
    persists through here now, `BlobRegistrations.save` included, which is what keeps such a patch
    total rather than merely plausible.
    """
    payload = serialize_state()
    try:
        _STATE_PATH.write_text(payload)
    except Exception:
        logger.exception("Could not write varsel state to %s", _STATE_PATH)
    try:
        blob = _gcs_blob()
        if blob is None:
            return True
        blob.upload_from_string(payload, content_type="application/json")
    except Exception:
        logger.exception(
            "Could not upload varsel state to gs://%s/%s", _STATE_BUCKET, _STATE_BLOB
        )
        return False
    return True


# ---- Togvarsel round markers (off the protocol; see `_togvarsel_rounds`) ------------------------
# **They are not persisted under `VARSEL_STATE_BACKEND=store`, and that is U14's to settle.** These
# two functions are module state either way, but the durable write behind them is the caller's
# `_save_state()` -- which is `BlobRegistrations.save()` on the blob and a no-op that returns True
# on the store, whose `load()` is a no-op too. So under the store backend a restart re-fires the
# accepted crash window for whatever rush round is still inside its timeliness window, rather than
# only for one mid-round crash. It costs nothing today: `SHOW_RUSHVARSEL` is off in every
# environment and no environment sets this backend, so nothing evaluates a round at all. The two
# shapes, neither decided here: keep the markers on the blob regardless of backend (this module
# calls `save_state()` itself and `main` always calls `load_state()`), or move them to one
# Firestore document. What must not happen is the cutover discovering it.
def rounds_for(date_str: str) -> dict[str, Any]:
    """A copy of the togvarsel round markers for one Oslo date ({} when none exist)."""
    with lock:
        return {k: dict(v) for k, v in _togvarsel_rounds.get(str(date_str), {}).items()}


def store_rounds(date_str: str, markers: Mapping[str, Any]) -> None:
    """Replace the round markers for `date_str` — the current Oslo date — in place.

    Every OTHER date is pruned: markers dedup rounds and seed follow-up baselines for one day
    only, so the first write after midnight is what forgets yesterday. Persisting is the CALLER's,
    because the caller is `varsel`'s injected accessor and its best-effort write-through is what
    togvarsel_notify's documented crash window is stated against.
    """
    with lock:
        _togvarsel_rounds.clear()
        _togvarsel_rounds[str(date_str)] = {k: dict(v) for k, v in markers.items()}


class BlobRegistrations:
    """The registrations `/varsel` has always had: five module dicts and one GCS blob.

    A façade over this module's globals rather than a holder of its own, so constructing a second
    instance is free and shares one state — see the comment on the block above. Every method takes
    the module lock, and the lock is re-entrant because several of them call each other.

    The two global ceilings are this backend's OWN, taken at construction so a suite can move them,
    and they are checked under the same lock acquisition as the write they guard -- which is what
    the read-then-compare they replaced could not promise.
    """

    def __init__(self, *, max_devices: int, max_journeys: int) -> None:
        self._max_devices = max_devices
        self._max_journeys = max_journeys

    # ---- the request path
    def get_device(self, device_id: str) -> dict | None:
        """A COPY of the device record, or None.

        A copy, because the store backend cannot hand out a live document and a caller that edited
        one in place would work on the blob and silently lose the edit on the store. The two
        in-place edits `varsel` used to make to JOURNEY records are :meth:`set_canonical_stops` and
        :meth:`stamp_service_check`, which exist for exactly this reason.
        """
        with lock:
            record = _devices.get(device_id)
            return dict(record) if record is not None else None

    def put_device(self, device_id: str, *, subscription: dict, brand: str) -> None:
        """Store the browser's push subscription for this device, KEEPING its original signup.

        Raises :class:`CapacityExceeded` past the global device ceiling, and only for a device that
        is NEW: an existing install re-registering must never be refused, or a deployment at its
        ceiling stops accepting the subscription refreshes that keep its own registrations alive.

        The signup instant is resolved here rather than by the caller because it is a property of
        the record's history and not of the request: an existing record keeps its own, a device
        coming back after «Skru av» or an expired subscription takes it off the tombstone it
        clears, and only a genuinely new device is stamped now. Without that, a lapse reads as a
        brand-new device and moves it out of every cohort it belongs to.

        `brand` is refreshed on every registration, so a device that reopens the app under a
        different `?brand=` has its notifications follow it rather than keeping the first one.
        """
        with lock:
            existing = _devices.get(device_id)
            if existing is None and len(_devices) >= self._max_devices:
                raise CapacityExceeded("devices")
            tombstone = _device_tombstones.pop(device_id, None)
            if existing is not None:
                created_ts = existing["created_ts"]
            else:
                created_ts = (tombstone or {}).get("created_ts") or _now_iso()
            _devices[device_id] = {
                "subscription": subscription,
                "created_ts": created_ts,
                "brand": brand,
            }

    def forget_device(self, device_id: str, *, reason: str) -> tuple[dict | None, int]:
        """Remove the device record, keeping its journeys; returns (the record, journeys kept).

        Writes the tombstone when journeys remain and clears it when none do — a record with no
        journeys left behind explains nothing to a later reader. The LOG line is the caller's: this
        module emits no `varsel_*` event, so a backend swap cannot change one.
        """
        with lock:
            record = _devices.pop(device_id, None)
            if record is None:
                return None, 0
            journeys = sum(
                1 for rec in _journeys.values() if rec["device_id"] == device_id
            )
            if journeys:
                _device_tombstones[device_id] = {
                    "reason": reason,
                    "ts": _now_iso(),
                    "created_ts": record.get("created_ts"),
                    "journeys": journeys,
                }
            else:
                _device_tombstones.pop(device_id, None)
            return record, journeys

    def devices_for(self, device_ids: Iterable[str]) -> dict[str, dict]:
        """Several devices at once, keyed by id; ids with no pushable device are simply absent.

        One call because the store answers it with one multi-get: the delivery pass needs an
        endpoint and a brand for every registration it is about to push to, and on a disrupted
        corridor that is thousands of serial round trips asked one at a time.
        """
        with lock:
            found = {}
            for device_id in dict.fromkeys(device_ids):
                record = _devices.get(device_id)
                if record is not None:
                    found[device_id] = dict(record)
            return found

    def device_journeys(self, device_id: str) -> list[tuple[str, dict]]:
        """One device's journeys as (id, copy) pairs, in creation order.

        R15's read: the request path asks about ONE device, never about all of them. Under the blob
        that is a scan of every registration and the ordering is the dict's insertion order; under
        the store it is the `device_journeys` query U12 added, ordered by `created_at` — which is
        the same order, stated rather than inherited.
        """
        with lock:
            return [
                (jid, dict(rec))
                for jid, rec in _journeys.items()
                if rec["device_id"] == device_id
            ]

    def create_journey(self, journey_id: str, record: dict) -> None:
        """Store one registration, refusing past the global journey ceiling.

        The per-DEVICE cap is the caller's: it is a product rule about one traveller's list, where
        this is the deployment refusing to take more registrations at all.
        """
        with lock:
            if len(_journeys) >= self._max_journeys:
                raise CapacityExceeded("journeys")
            _journeys[journey_id] = dict(record)

    def delete_journey(self, journey_id: str) -> dict | None:
        """Remove one journey and return it, dropping its device's tombstone with the last one.

        The tombstone explains journeys that outlived a device record; with none left there is
        nothing to explain, and leaving it would have the device report describe a device nobody can
        still be registered through.
        """
        with lock:
            record = _journeys.pop(journey_id, None)
            if record is None:
                return None
            device_id = record["device_id"]
            if device_id in _device_tombstones and not any(
                rec["device_id"] == device_id for rec in _journeys.values()
            ):
                del _device_tombstones[device_id]
            return record

    def erase_device(self, device_id: str) -> Erasure:
        """«Slett alt»: the device, its journeys, its marks and its tombstone, or none of it.

        One method rather than the four pops the route used to make, because the two backends make
        it atomic by different means and neither can be expressed in the other's steps. Here it is
        one take of the lock plus a full restore when the durable write fails; under the store it is
        U12's paged `erase_device` transaction. The route reads `durable` and answers 503 on False.

        Zero I/O for an unknown device, which is not an optimisation: the endpoint is
        unauthenticated, so saving the blob for every random UUID would be free write amplification
        against a GCS upload held under this lock.
        """
        with lock:
            device = _devices.pop(device_id, None)
            # The tombstone goes too: an art. 17 erasure keeps no record of what it erased.
            tombstone = _device_tombstones.pop(device_id, None)
            deleted = [
                jid for jid, rec in _journeys.items() if rec["device_id"] == device_id
            ]
            if device is None and tombstone is None and not deleted:
                return Erasure(found=False, journeys_deleted=0, durable=True)
            removed_journeys = {jid: _journeys.pop(jid) for jid in deleted}
            gone = set(deleted)
            removed_notified = {key for key in _notified if key.split("|", 1)[0] in gone}
            _notified.difference_update(removed_notified)
            if save_state():
                return Erasure(found=True, journeys_deleted=len(deleted), durable=True)
            # The durable copy still holds everything, so memory must too, or the two disagree and
            # the retry the caller is about to be told to make would erase only what is left.
            if device is not None:
                _devices[device_id] = device
            if tombstone is not None:
                _device_tombstones[device_id] = tombstone
            _journeys.update(removed_journeys)
            _notified.update(removed_notified)
            return Erasure(found=True, journeys_deleted=len(deleted), durable=False)

    # ---- the cycle
    def all_journeys(self) -> list[tuple[str, dict]]:
        """Every registration as (id, copy) pairs.

        The cycle's read, and the blob backend's only way to answer it. The store backend does not
        implement it this way at all — the cycle reads the subscription mirror there, which is the
        whole point of U6 — so nothing outside the blob may come to depend on the ordering or on the
        records being live.
        """
        with lock:
            return [(jid, dict(rec)) for jid, rec in _journeys.items()]

    def get_journey(self, journey_id: str) -> dict | None:
        """A COPY of one registration, or None.

        Asked by the delivery loop as a liveness re-check and by the delete route as the ownership
        check. By ID and never by a scan, because under the store that is one document read where
        "is this journey one of that device's?" would be a query -- and it is asked twice per push.
        """
        with lock:
            record = _journeys.get(journey_id)
            return dict(record) if record is not None else None

    def set_canonical_stops(
        self, journey_id: str, *, from_stop: str, to_stop: str
    ) -> bool:
        """Rewrite one registration onto canonical stop refs; False when it is gone."""
        with lock:
            record = _journeys.get(journey_id)
            if record is None:
                return False
            record["from_stop"] = from_stop
            record["to_stop"] = to_stop
            return True

    def stamp_service_check(
        self, journey_id: str, *, checked_ts: str, cleared: bool, reverse_only: bool
    ) -> bool:
        """Record a planned-service re-check on one registration; False when it is gone.

        `cleared` drops the caveat and its qualifier together; otherwise the qualifier is written
        beside the stamp. The stamp moves on EVERY established answer, cleared or not — it used to
        be written only when something cleared, which left every stamp of an unchanged `false` in
        memory until some other writer happened to persist, so a restart re-asked them all.
        """
        with lock:
            record = _journeys.get(journey_id)
            if record is None:
                return False
            record["service_checked_ts"] = checked_ts
            if cleared:
                record.pop("no_direct_service", None)
                record.pop("service_reverse_only", None)
            else:
                record["service_reverse_only"] = reverse_only
            return True

    # ---- the push side
    def notified_snapshot(self, journey_ids: Iterable[str]) -> set[str]:
        """The `"<journey_id>|<situation>"` marks among these registrations.

        Asked once per pair in pass 1b to decide which trains are still worth a WORDING — not which
        are safe to push, which is :meth:`push_ledger`'s exact per-registration answer read at
        delivery. The distinction is what lets the store backend serve this from the cycle's own
        mirror view: `_pending_pushes` is already "deliberately approximate in one direction only",
        and a mark the mirror has not caught up with costs at worst one wording written for a train
        already delivered. A stale mark must never reach the delivery loop, and does not.
        """
        wanted = set(journey_ids)
        with lock:
            return {key for key in _notified if key.split("|", 1)[0] in wanted}

    def push_ledger(self, journey_id: str) -> PushLedger:
        with lock:
            day, count = _daily_pushes.get(journey_id, (None, 0))
            prefix = f"{journey_id}|"
            return PushLedger(
                notified=frozenset(
                    key.split("|", 1)[1] for key in _notified if key.startswith(prefix)
                ),
                pushed_trains=frozenset(tag for tag in _pushed_trains if tag.startswith(prefix)),
                day=day,
                sent_today=count,
            )

    def mark_notified(self, journey_id: str, situation_number: str) -> None:
        with lock:
            _notified.add(f"{journey_id}|{situation_number}")

    def record_push(
        self,
        journey_id: str,
        *,
        tag: str | None,
        departure: datetime | None,
        oslo_day: str,
        charge_cap: bool,
    ) -> None:
        """Record a delivered alert push: the train it was about, and the day's count.

        `tag` is None when the alert cannot say which train it is about, and then nothing is
        recorded for the train — the lock screen falls back to the situation number, which is what
        it always did.

        `charge_cap` is the CALLER's answer to "is this a replacement?", passed rather than
        re-derived: a push whose tag was already in the ledger takes the earlier notification's
        place on the lock screen instead of adding to the pile, so it must not spend one of the
        registration's four for the day. Re-deriving it here would read the very ledger this call
        is about to write.

        A count carried over from a previous Oslo day is reset rather than incremented — the same
        reading :meth:`push_ledger`'s caller applies, restated at the write because the two are
        separated by the whole delivery fan-out.
        """
        with lock:
            if tag is not None and departure is not None:
                _pushed_trains[tag] = departure
            if not charge_cap:
                return
            day, count = _daily_pushes.get(journey_id, (oslo_day, 0))
            _daily_pushes[journey_id] = (
                oslo_day,
                (count if day == oslo_day else 0) + 1,
            )

    def prune(
        self, *, live_situations: set[str], trains_gone_by: datetime, oslo_today: str
    ) -> int:
        """The stale sweep: marks for situations that ended, trains that have left, other days.

        None of it is correctness — every reader tests the thing it cares about — and all of it is
        memory: a mark kept for a situation nobody can be alerted about again is a key per
        (registration, situation) for as long as the process lives.

        Returns how many notified marks went, which is the only part of this that is PERSISTED: the
        caller saves on a non-zero answer and skips the blob upload otherwise. The two in-memory
        ledgers are rebuilt by the next push and never reach the blob at all.
        """
        with lock:
            stale = {k for k in _notified if k.split("|", 1)[1] not in live_situations}
            _notified.difference_update(stale)
            for tag in [t for t, departed in _pushed_trains.items() if departed < trains_gone_by]:
                _pushed_trains.pop(tag, None)
            for jid in [j for j, (day, _count) in _daily_pushes.items() if day != oslo_today]:
                _daily_pushes.pop(jid, None)
            return len(stale)

    # ---- persistence
    def save(self) -> bool:
        """Make every mutation since the last call durable; see :func:`save_state`.

        Resolved through the module rather than bound at class definition, so a suite patching
        `varsel_state.save_state` reaches this too. That totality is the point — see that function.
        """
        return save_state()

    def load(self) -> None:
        load_state()


# ---- The store backend (U13, second half) -------------------------------------------------------
#: The client id every `/varsel` registration is stored under. A reserved name rather than one more
#: onboarded client, because the traveller behind these documents is not somebody's integration:
#: nothing may issue a credential for it (`scripts/manage_notification_client.py` refuses the name),
#: and its `clients` document exists only to carry the two quotas that were `varsel._MAX_DEVICES`
#: and `varsel._MAX_JOURNEYS`. Hard-coded here and not configurable: it is a join key in four
#: collections, so a deployment that changed it would orphan every registration it already had.
BROWSER_CLIENT_ID = "browser"


def _device_record(device: Any) -> dict[str, Any]:
    """One stored device as `/varsel`'s own record shape.

    The push subscription is rebuilt into the browser's `{endpoint, keys}` form because that is what
    `pywebpush` is handed verbatim; the store holds the three parts flat, which is what lets a
    tombstone clear them individually.
    """
    return {
        "subscription": {
            "endpoint": device.endpoint,
            "keys": {"p256dh": device.p256dh, "auth": device.auth},
        },
        "created_ts": device.created_at.isoformat() if device.created_at else "",
        "brand": device.brand,
    }


def _journey_record(record: Any) -> dict[str, Any]:
    """One stored registration as `/varsel`'s own journey record.

    Takes anything carrying the fields, so a `Subscription` from the request path and a
    `MirrorEntry` from the cycle produce the SAME shape -- which is the property the whole backend
    rests on, because `varsel` reads one record type from both and a second builder here would be
    two shapes the day one of them gained a field.

    The three servability fields are written only when there is an answer, because `/varsel`'s
    record says "not established" by their ABSENCE and `_service_warning` reads it that way: a
    `no_direct_service: False` would render as an all-clear the check never gave.
    """
    out: dict[str, Any] = {
        "device_id": record.device_id,
        "from_stop": record.from_stop,
        "from_name": record.from_name,
        "to_stop": record.to_stop,
        "to_name": record.to_name,
        "begin": record.begin,
        "end": record.end,
        "days": list(record.days),
    }
    created = getattr(record, "created_at", None)
    if created is not None:
        out["created_ts"] = created.isoformat()
    consent = getattr(record, "consent_version", "")
    if consent:
        out["consent_text_version"] = consent
    checked = getattr(record, "servable_checked_at", None)
    if checked is not None:
        out["service_checked_ts"] = checked.isoformat()
    if record.servable is False:
        out["no_direct_service"] = True
        out["service_reverse_only"] = bool(getattr(record, "servable_reverse", False))
    return out


class StoreRegistrations:
    """`/varsel`'s registrations in Firestore: U12's collections, behind the same seam.

    **Which read goes where is the design, and it is R15 plus one.** The REQUEST path reads the
    store: a traveller who has just created a journey must see it in the next `list_journeys`, and
    the mirror is at best as fresh as the sync at the start of this cycle -- and, before the poller
    has seeded it, empty. The CYCLE reads the mirror: at a hundred thousand registrations a
    per-cycle read of the collection is the cycle. The one method that answers both, `get_journey`,
    prefers the mirror and falls back to the store while it is not ready, which is exact in both
    cases because every commit this process makes is applied to the mirror from its own result
    (`notification_store.SUBSCRIPTION_MIRRORING_BODIES`).

    **The ceilings are the client's, resolved by the CALLER.** This module must not import
    `notification_auth` -- `notification_store` does not either, and U7's pattern is that a quota
    arrives as a callable from whoever already reads the account. `quotas` yields
    `(max_devices, max_active_subscriptions)` and is called per write rather than cached, so an
    operator's `set-quotas` takes effect without a restart.

    **`save()` has nothing to flush and says so**, which is the asymmetry the protocol publishes:
    each mutator here is its own durable document write, so the caller's "mutate, then save"
    sequence costs a blob upload on one backend and nothing at all on this one.
    """

    def __init__(self, *, client_id: str, quotas: Callable[[], tuple[int, int]]) -> None:
        self._client_id = client_id
        self._quotas = quotas

    # ---- the request path
    def get_device(self, device_id: str) -> dict | None:
        device = notification_store.get_device(device_id)
        if device is None or device.client_id != self._client_id or not device.pushable:
            # A tombstoned device is NOT a device here. `/varsel` pops the record when a push
            # endpoint dies and keeps the journeys, so "the record is gone" is exactly what the
            # caller means, and a husk with no endpoint would be offered to the push loop.
            return None
        return _device_record(device)

    def devices_for(self, device_ids: Iterable[str]) -> dict[str, dict]:
        wanted = list(dict.fromkeys(device_ids))
        found = notification_store.get_devices(wanted)
        return {
            device_id: _device_record(device)
            for device_id, device in found.items()
            if device.client_id == self._client_id and device.pushable
        }

    def put_device(self, device_id: str, *, subscription: dict, brand: str) -> None:
        keys = subscription.get("keys") or {}
        try:
            notification_store.upsert_device(
                device_id=device_id,
                client_id=self._client_id,
                endpoint=str(subscription.get("endpoint") or ""),
                p256dh=str(keys.get("p256dh") or ""),
                auth=str(keys.get("auth") or ""),
                brand=brand,
                max_devices=self._quotas()[0],
            )
        except notification_store.QuotaExceeded as exc:
            raise CapacityExceeded("devices") from exc

    def forget_device(self, device_id: str, *, reason: str) -> tuple[dict | None, int]:
        device = notification_store.tombstone_device(
            device_id=device_id, client_id=self._client_id, reason=reason
        )
        if device is None:
            return None, 0
        return _device_record(device), len(self.device_journeys(device_id))

    def device_journeys(self, device_id: str) -> list[tuple[str, dict]]:
        """The device's registrations — and the read that RENEWS them, which the blob has no
        equivalent of because a blob record has no expiry at all.

        `web_push`'s horizon is 180 days of inactivity (U14), and this is the one call that means
        "the traveller is here": it runs on every app open, and — unlike the device upsert — it
        runs for the traveller who never turned notifications on, which was 79 of prd's 186
        journeys when the device report last counted. Hooking the renewal to the upsert instead
        would have expired exactly that population while it was using the page every day.

        Renewing inside a read rather than behind a method of its own, and the seam is the reason:
        `RegistrationStore` is a list of questions the CALLER asks, and "the traveller was here" is
        not one /varsel has any reason to ask — it is this backend's own storage-limitation rule,
        so a protocol method would be a no-op the blob has to implement for a concept it does not
        have. The cost is that a read writes, which is why it is named here rather than buried:
        `notification_store.note_web_push_activity` bounds it to one write per journey per week and
        swallows its own failures.

        `forget_device` reads through here too, so «Skru av» also renews. That is correct rather
        than tolerated — somebody pressing a button in the app is present by definition.
        """
        subs = notification_store.device_journeys(
            device_id=device_id, client_id=self._client_id
        )
        notification_store.note_web_push_activity(
            device_id, client_id=self._client_id, subscriptions=subs
        )
        return [(sub.subscription_id, _journey_record(sub)) for sub in subs]

    def create_journey(self, journey_id: str, record: dict) -> None:
        spec = notification_store.SubscriptionSpec(
            subscription_id=journey_id,
            client_id=self._client_id,
            destination_id="",
            channel=notification_store.CHANNEL_WEB_PUSH,
            device_id=str(record["device_id"]),
            from_stop=str(record["from_stop"]),
            to_stop=str(record["to_stop"]),
            from_name=str(record.get("from_name") or ""),
            to_name=str(record.get("to_name") or ""),
            begin=str(record["begin"]),
            end=str(record["end"]),
            days=tuple(int(d) for d in record["days"]),
            consent_version=str(record.get("consent_text_version") or ""),
        )
        try:
            notification_store.create_subscription(
                spec,
                max_active=self._quotas()[1],
                # The record's own signup instant, which on the request path is this instant and on
                # U14's cutover import is the day the traveller actually registered. Passed always
                # rather than only by the import, so `created_ts` on the record and `created_at` on
                # the document agree by construction instead of by happening to be written in the
                # same millisecond -- `_journey_record` hands the document's value back out, so a
                # divergence would be visible to the traveller as a re-ordered list.
                created_at=_parse_ts(record.get("created_ts")),
            )
        except notification_store.QuotaExceeded as exc:
            raise CapacityExceeded("journeys") from exc
        # The servability answer the create path already has, stamped as its own write rather than
        # carried on the spec: `SubscriptionSpec` is what a CLIENT submits and a verdict is not a
        # client's to submit -- `set_servability` is the one writer, and it is what R6's freshness
        # stamp comes from.
        if "no_direct_service" in record or "service_checked_ts" in record:
            notification_store.set_servability(
                journey_id,
                servable=not record.get("no_direct_service"),
                servable_reverse=bool(record.get("service_reverse_only")),
                expected_version=1,
            )

    def delete_journey(self, journey_id: str) -> dict | None:
        record = notification_store.delete_subscription(
            journey_id, client_id=self._client_id
        )
        return None if record is None else _journey_record(record)

    def erase_device(self, device_id: str) -> Erasure:
        """«Slett alt» through U12's paged transaction.

        `durable` is `complete`: each pass is atomic and the device document goes in the pass that
        drains the last journey, so an incomplete run has left some journeys tombstoned and the
        traveller is owed the 503 that makes their client retry. At `varsel._MAX_JOURNEYS_PER_DEVICE`
        (20) against `MAX_ERASE_WRITES_PER_TRANSACTION` (400) it is always one pass, which is what
        makes "nothing partially deleted" true rather than hopeful -- if that per-device cap ever
        rises past the transaction bound, this sentence stops holding and the 503 becomes a retry
        over a partially erased device.
        """
        result = notification_store.erase_device(device_id=device_id, client_id=self._client_id)
        return Erasure(
            found=bool(result.device_deleted or result.journeys_tombstoned),
            journeys_deleted=result.journeys_tombstoned,
            durable=result.complete,
        )

    # ---- the cycle
    def all_journeys(self) -> list[tuple[str, dict]]:
        """Every live registration, from the MIRROR and never from the collection.

        This is the read U6 exists for: at a hundred thousand registrations a per-cycle query over
        the collection is the cycle, and the mirror answers it from memory. An unseeded mirror
        yields nothing rather than a partial set, `recipient_documents`' rule -- the pass cannot
        tell "nobody is registered" from "the read has not finished", and half a population is the
        worse answer because it silently retires the other half.
        """
        mirror = notification_store.subscription_mirror()
        if not mirror.ready:
            return []
        return [
            (entry.subscription_id, _journey_record(entry))
            for entry in mirror.recipients(
                _now(), channel=notification_store.CHANNEL_WEB_PUSH
            )
        ]

    def get_journey(self, journey_id: str) -> dict | None:
        """One registration, from the mirror when it is ready and from the store when it is not.

        The mirror is EXACT for this process rather than merely fresh -- every commit is applied
        from its own result -- so the delivery loop's two liveness re-checks, which exist to catch a
        delete that landed mid-cycle, cost no read. The fallback is what makes the request path
        correct before the poller has seeded: `/varsel-api` serves from the instant the port binds,
        and the seed runs on the poller thread minutes later.
        """
        mirror = notification_store.subscription_mirror()
        if mirror.ready:
            entry = mirror.entry(
                journey_id, _now(), channel=notification_store.CHANNEL_WEB_PUSH
            )
            return None if entry is None else _journey_record(entry)
        record = notification_store.get_subscription(journey_id, client_id=self._client_id)
        return None if record is None else _journey_record(record)

    def set_canonical_stops(self, journey_id: str, *, from_stop: str, to_stop: str) -> bool:
        return notification_store.canonicalize_stops(
            subscription_id=journey_id,
            client_id=self._client_id,
            from_stop=from_stop,
            to_stop=to_stop,
        )

    def stamp_service_check(
        self, journey_id: str, *, checked_ts: str, cleared: bool, reverse_only: bool
    ) -> bool:
        """R6's answer and its freshness. False when the registration is gone or was replaced.

        `expected_version` is read from the record rather than passed by the caller, and the window
        it guards is smaller here than on the webhook channel: a `/varsel` registration is never
        replaced, so the only way the version moves under a check in flight is a canonicalization.
        A stale stamp would put a verdict about the old pair on the new one, with nothing in the
        document saying so -- which is exactly the case `set_servability` refuses.
        """
        record = notification_store.get_subscription(journey_id, client_id=self._client_id)
        if record is None:
            return False
        try:
            notification_store.set_servability(
                journey_id,
                servable=cleared,
                servable_reverse=reverse_only,
                expected_version=record.version,
                now=_iso_to_dt(checked_ts),
            )
        except (notification_store.NotFound, notification_store.Conflict):
            return False
        return True

    # ---- the push side
    def notified_snapshot(self, journey_ids: Iterable[str]) -> set[str]:
        wanted = set(journey_ids)
        marks: set[str] = set()
        for journey_id, ledger in self._ledgers(wanted).items():
            marks.update(f"{journey_id}|{situation}" for situation in ledger.notified)
        return marks

    def push_ledger(self, journey_id: str) -> PushLedger:
        return self._ledgers({journey_id}).get(
            journey_id, PushLedger(frozenset(), frozenset(), None, 0)
        )

    def _ledgers(self, journey_ids: set[str]) -> dict[str, PushLedger]:
        """The push marks of these registrations, from the mirror.

        From the mirror and not the store, which reverses what U12 recorded and is the reason the
        mark bodies now apply their own commits: the alternative is a document read per registration
        per cycle, and the mirror's copy is exact rather than approximate because this process is
        the only writer of it. See `notification_store.SUBSCRIPTION_MIRRORING_BODIES`.

        Keyed through :meth:`SubscriptionMirror.entry`, never `recipients` -- the pass asks this
        per candidate, and a scan per question is the cost that read exists to remove.
        """
        mirror = notification_store.subscription_mirror()
        if not mirror.ready or not journey_ids:
            return {}
        now = _now()
        out: dict[str, PushLedger] = {}
        for journey_id in journey_ids:
            entry = mirror.entry(
                journey_id, now, channel=notification_store.CHANNEL_WEB_PUSH
            )
            if entry is None:
                continue
            day, sent = (
                max(entry.daily_pushes.items()) if entry.daily_pushes else (None, 0)
            )
            out[entry.subscription_id] = PushLedger(
                notified=frozenset(entry.notified),
                pushed_trains=frozenset(entry.pushed_trains),
                day=day,
                sent_today=sent,
            )
        return out

    def mark_notified(self, journey_id: str, situation_number: str) -> None:
        try:
            notification_store.mark_notified(
                subscription_id=journey_id, situation_number=situation_number
            )
        except notification_store.NotFound:
            # The journey was erased between the delivery loop's re-check and this write. Nothing
            # to resurrect and nothing to report: the push went out, and a mark on a document that
            # no longer exists would be the only thing this call could add.
            logger.info("Journey %s was erased before its push mark landed", journey_id)

    def record_push(
        self,
        journey_id: str,
        *,
        tag: str | None,
        departure: datetime | None,
        oslo_day: str,
        charge_cap: bool,
    ) -> None:
        # `tag` None records nothing for the train and still charges the day, which is the blob's
        # own reading one layer down (`_record_push_body`) rather than a second rule here: the cap
        # is spent per PUSH, and the lock screen falls back to the situation number.
        try:
            notification_store.record_push(
                subscription_id=journey_id, train_ref=tag, charge_cap=charge_cap
            )
        except notification_store.NotFound:
            logger.info("Journey %s was erased before its push was recorded", journey_id)

    def prune(
        self, *, live_situations: set[str], trains_gone_by: datetime, oslo_today: str
    ) -> int:
        """Nothing to sweep: the marks are pruned at write time and the rest is Firestore's TTL.

        `_mark_notified_body` and `_record_push_body` drop their own expired entries on every write,
        which is what bounds the maps without a sweep anywhere -- so there is no in-memory ledger to
        forget and no blob to save. Returns 0, which is what tells the caller no save is owed.
        """
        return 0

    # ---- persistence
    def save(self) -> bool:
        """Nothing to flush: every mutator above is its own durable document write."""
        return True

    def load(self) -> None:
        """Nothing to load: the mirror seeds on the poller thread, after the port binds."""


# ---- The cutover import (U14) ------------------------------------------------------------------
#: The one-shot marker's document id in `notification_store.MIGRATIONS`.
IMPORT_MARKER = "varsel_blob_to_store"


class ImportFailed(RuntimeError):
    """The cutover import did not complete, or completed and its invariants did not hold.

    Raised out of the lifespan so the port never binds: `helm --wait` then rolls the release back
    to the binary that was still reading the blob, which is intact — the import creates and never
    deletes, so a failed one leaves the rollback artefact untouched.
    """


@dataclass(frozen=True)
class ImportReport:
    """What one cutover import did, for the log line and for the operator.

    Every field is a count somebody acts on. `stray_marks` is the one that is expected to be
    non-zero and harmless: a push mark whose journey has since been deleted has nothing to attach
    to, and the blob keeps such marks until `varsel`'s own sweep drops them.
    """

    #: `imported`, `skipped` (the marker was already there) or `empty` (there was no blob). The
    #: caller logs it: this module emits no structured event, so which vocabulary a reader greps
    #: for stays a property of the caller rather than of whichever backend answered.
    result: str = "imported"
    devices: int = 0
    devices_synthesised: int = 0
    tombstones: int = 0
    journeys: int = 0
    marks: int = 0
    stray_marks: int = 0
    seconds: float = 0.0
    problems: tuple[str, ...] = ()

    def as_fields(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "devices": self.devices,
            "devices_synthesised": self.devices_synthesised,
            "tombstones": self.tombstones,
            "journeys": self.journeys,
            "marks": self.marks,
            "stray_marks": self.stray_marks,
            "seconds": round(self.seconds, 3),
            "problems": list(self.problems),
        }


def _parse_ts(raw: Any) -> datetime | None:
    """One ISO timestamp out of the blob, or None. Never raises: a record with an unreadable
    signup instant is still a registration, and the fallback is the import instant."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def import_blob_into_store(
    store: StoreRegistrations, *, now: datetime | None = None
) -> ImportReport:
    """Move the GCS blob's registrations into the store, ONCE. U14's cutover (KTD13).

    Runs as a startup step of the store-backed binary, inside `lifespan` and before the port binds,
    so `/varsel` never serves a request against a half-imported store (R15). Under the Recreate
    strategy the blob's writer has already stopped, so what it reads cannot move underneath it.

    **One-shot behind a store marker, and that is not caution — it is the only thing standing
    between this and a privacy defect.** The import creates by document id and never deletes, so a
    second run against a store somebody has since erased themselves from would put every one of
    their registrations back. It would not be rare: the import runs at startup, and dev restarts on
    every push. So the marker is read FIRST — before the blob is opened at all — and written LAST,
    only once the invariants below have held. Re-importing after a rollback is deliberately not
    something a restart can do; it is an operator deleting that document on purpose.

    **What it writes, and through what.** Every document goes through the same functions the
    request path uses — `create_subscription`, `upsert_device`, `mark_notified`, `tombstone_device`
    — rather than through hand-built documents. A second builder here would be a second definition
    of what a registration IS, and it would drift the first time `_new_subscription_doc` gained a
    field: the import would keep writing last quarter's shape, and the only symptom would be a
    document that reads as slightly wrong months later. The cost is one transaction per document
    instead of a batch: ~360 of them for prd's blob, ~20 s in-cluster, well inside the chart's
    300 s startup probe beside the BigQuery loads. The duration is logged so a blob that grows
    towards that bound is noticed before it reaches it.

    **Two instants, two reasons.** `created_at` is the blob's own `created_ts` — the signup cohort
    the device report groups on, and what `device_journeys` orders by, so restamping it would make
    every registration look made on cutover day. `last_seen_at` is the import instant, because the
    blob records no per-journey last-seen and there is nothing else to take; under U14's inactivity
    horizon that is not a cliff but a fresh 180 days, and the only registration it ever expires is
    one whose traveller does not open the app in the six months after the cutover.

    **What it deliberately does not carry.** The togvarsel round markers: they are off the protocol
    and ride in the blob half, which is the gap the operations document's cutover section names.
    Importing them here would be inventing a home for them that nothing reads.
    """
    started = time.monotonic()
    instant = now or _now()
    marker = notification_store.read_migration(IMPORT_MARKER)
    if marker:
        return ImportReport(result="skipped", seconds=time.monotonic() - started)

    raw = read_blob_text()
    if raw is None:
        # No blob at all is a legitimate first run of a fresh environment, and it is DONE rather
        # than pending: marking it keeps a blob that appears later -- a restored backup, a rollback
        # that wrote one -- from being imported by a pod restart nobody asked to migrate anything.
        # A blob that could not be READ raises out of `read_blob_text` instead and never gets here.
        notification_store.write_migration(
            IMPORT_MARKER, {"result": "empty", "source": _blob_source()}, now=instant
        )
        return ImportReport(result="empty", seconds=time.monotonic() - started)

    data = json.loads(raw)
    devices: dict[str, Any] = dict(data.get("devices") or {})
    journeys: dict[str, Any] = dict(data.get("journeys") or {})
    tombstones: dict[str, Any] = dict(data.get("device_tombstones") or {})
    marks = [str(mark) for mark in (data.get("notified") or [])]
    max_devices, max_journeys = store._quotas()
    client_id = store._client_id

    written_devices = 0
    for device_id, record in devices.items():
        subscription = dict(record.get("subscription") or {})
        keys = dict(subscription.get("keys") or {})
        notification_store.upsert_device(
            device_id=device_id,
            client_id=client_id,
            endpoint=str(subscription.get("endpoint") or ""),
            p256dh=str(keys.get("p256dh") or ""),
            auth=str(keys.get("auth") or ""),
            brand=str(record.get("brand") or "entur"),
            now=instant,
            created_at=_parse_ts(record.get("created_ts")),
            max_devices=max_devices,
        )
        written_devices += 1

    # A journey whose device record is gone gets a device document with no push subscription. That
    # is a state the product supports rather than a repair: 79 of prd's 186 journeys sat on such
    # devices when the device report last counted, and the journey list works as a dashboard for
    # them. Without the document the traveller's own «Slett alt» would have nothing to delete, and
    # `device_journeys` would answer about a device that, as far as the store knows, never existed.
    synthesised = 0
    for journey_id, record in journeys.items():
        device_id = str(record.get("device_id") or "")
        if not device_id or device_id in devices:
            continue
        devices[device_id] = {}
        notification_store.upsert_device(
            device_id=device_id,
            client_id=client_id,
            endpoint="",
            p256dh="",
            auth="",
            brand="entur",
            now=instant,
            created_at=_parse_ts((tombstones.get(device_id) or {}).get("created_ts"))
            or _parse_ts(record.get("created_ts")),
            max_devices=max_devices,
        )
        synthesised += 1

    written_journeys = 0
    for journey_id, record in journeys.items():
        if written_journeys >= max_journeys:
            raise ImportFailed(
                f"the blob holds more journeys than the client's ceiling ({max_journeys})"
            )
        store.create_journey(journey_id, record)
        written_journeys += 1

    # After the devices exist, because a tombstone is a patch to a device document and there is no
    # tombstone without one. The ORIGINAL instant is preserved where the blob recorded one: the
    # device report renders it, and stamping the import over it would make every lapse look like it
    # happened at the cutover.
    written_tombstones = 0
    for device_id, tombstone in tombstones.items():
        if device_id not in devices:
            continue
        notification_store.tombstone_device(
            device_id=device_id,
            client_id=client_id,
            reason=str(tombstone.get("reason") or "expired"),
            now=_parse_ts(tombstone.get("ts")) or instant,
        )
        written_tombstones += 1

    written_marks = 0
    stray = 0
    for mark in marks:
        journey_id, _, situation = mark.partition("|")
        if journey_id not in journeys:
            # A mark whose journey is gone has nothing to attach to. Counted rather than dropped
            # silently: it is the one number here that is legitimately non-zero, and a reader who
            # does not know that would read it as data loss.
            stray += 1
            continue
        store.mark_notified(journey_id, situation)
        written_marks += 1

    problems = _import_problems(store, devices=devices, journeys=journeys, marks=marks)
    report = ImportReport(
        devices=written_devices,
        devices_synthesised=synthesised,
        tombstones=written_tombstones,
        journeys=written_journeys,
        marks=written_marks,
        stray_marks=stray,
        seconds=time.monotonic() - started,
        problems=tuple(problems),
    )
    if problems:
        # The marker is NOT written, so the next start tries again -- which is right: nothing has
        # been deleted, every create is idempotent on (id, definition), and the alternative is a
        # half-imported store that no restart will ever finish.
        raise ImportFailed("; ".join(problems))
    notification_store.write_migration(
        IMPORT_MARKER,
        {"result": "imported", "source": _blob_source(), **report.as_fields()},
        now=instant,
    )
    return report


def _blob_source() -> str:
    """Where the import read from, for the marker document. Not a credential and not personal
    data — a bucket name and an object name, which is what a later operator needs to know which
    artefact this store was built from."""
    return f"gs://{_STATE_BUCKET}/{_STATE_BLOB}" if _STATE_BUCKET else str(_STATE_PATH)


def _import_problems(
    store: StoreRegistrations,
    *,
    devices: Mapping[str, Any],
    journeys: Mapping[str, Any],
    marks: Iterable[str],
) -> list[str]:
    """KTD13's invariants, read back OUT of the store rather than counted on the way in.

    Counting what the import wrote would check the loop against itself: every invariant here is
    about what a later reader will find, so every one is answered by the same read the request path
    makes. `device_journeys` is that read, and it carries four of the invariants at once — the
    query filters `client_id` and `channel`, and `subscription_is_live` filters `status` and the
    horizons, so a journey that comes back has all four and a journey that is missing one does not
    come back at all.
    """
    problems: list[str] = []
    expected: dict[str, set[str]] = {}
    for journey_id, record in journeys.items():
        expected.setdefault(str(record.get("device_id") or ""), set()).add(journey_id)

    # `notification_store.device_journeys` and NOT the seam's, which is the same query and also
    # RENEWS what it reads (U14's inactivity horizon). Nothing would go wrong today -- the import
    # has just stamped every one of these, so the renewal's staleness cut skips them all -- but a
    # verification pass that writes is one whose result depends on what it did, and this one exists
    # to answer whether the import's own writes landed.
    found: dict[str, set[str]] = {}
    stored_marks: set[str] = set()
    for device_id in devices:
        subscriptions = notification_store.device_journeys(
            device_id=device_id, client_id=store._client_id
        )
        found[device_id] = {sub.subscription_id for sub in subscriptions}
        for sub in subscriptions:
            stored_marks.update(f"{sub.subscription_id}|{sit}" for sit in sub.notified)

    for device_id, wanted in expected.items():
        got = found.get(device_id, set())
        if got != wanted:
            missing = sorted(wanted - got)
            problems.append(
                f"device {device_id} should hold {len(wanted)} journeys and the store returns "
                f"{len(got)}; missing {len(missing)}"
            )
    total = sum(len(ids) for ids in found.values())
    if total != len(journeys):
        problems.append(f"the blob holds {len(journeys)} journeys and the store returns {total}")

    wanted_marks = {mark for mark in marks if str(mark).partition("|")[0] in journeys}
    if stored_marks != wanted_marks:
        problems.append(
            f"the blob holds {len(wanted_marks)} live marks and the store returns "
            f"{len(stored_marks)}"
        )
    return problems


# ---- The rollback artefact (U14) ---------------------------------------------------------------
@dataclass(frozen=True)
class ExportReport:
    """What one export wrote back into the blob. Counts only; no identifiers."""

    devices: int = 0
    tombstones: int = 0
    journeys: int = 0
    marks: int = 0
    seconds: float = 0.0
    bytes_written: int = 0

    def as_fields(self) -> dict[str, Any]:
        return {
            "devices": self.devices,
            "tombstones": self.tombstones,
            "journeys": self.journeys,
            "marks": self.marks,
            "seconds": round(self.seconds, 3),
            "bytes_written": self.bytes_written,
        }


def export_store_to_blob(
    store: StoreRegistrations,
    *,
    now: datetime | None = None,
    beat: Callable[[], None] | None = None,
) -> ExportReport:
    """Write the store's registrations back into the state blob: the rollback artefact (U14).

    **What this exists for is erasure, not backup.** The import creates and never deletes, so the
    blob still holds everything that was in it at the cutover — including the journeys of a
    traveller who has since pressed «Slett alt». Rolling back to `gcs` without an export would
    therefore resurrect somebody who asked to be forgotten. The read behind it applies liveness, so
    a tombstoned registration is ABSENT from what is written rather than written as deleted, and
    the interval between exports is the window in which that is still true of the artefact. The
    runbook states that interval as the rollback loss window.

    **It writes the blob backend's own shape, verbatim**, so the artefact is something
    `apply_state` can load rather than something a future reader has to convert: the same five
    keys, built through `_journey_record` and `_device_record`, which are what both backends
    already answer `/varsel` with.

    **Keys this backend does not hold ride through from the blob that is already there** — the
    togvarsel round markers and anything under `_state_extra`. That is not tidiness: the round
    markers have no home in the store (the cutover section names it), so an export that rebuilt the
    document from the store alone would silently delete them from the only place they exist.

    A device with no live journeys IS exported, which is why the device read is a paged scan rather
    than a multi-get over the ids the journeys name: that record is a push endpoint, a brand and
    the signup instant `_upsert_device_body` restores, and dropping it would make that traveller
    read as a new install on their next visit.
    """
    started = time.monotonic()
    instant = now or _now()
    subscriptions = notification_store.all_web_push_subscriptions(
        client_id=store._client_id, now=instant, beat=beat
    )
    devices = notification_store.all_devices(client_id=store._client_id, beat=beat)

    journeys: dict[str, Any] = {}
    marks: list[str] = []
    per_device: dict[str, int] = {}
    for sub in subscriptions:
        journeys[sub.subscription_id] = _journey_record(sub)
        marks.extend(f"{sub.subscription_id}|{situation}" for situation in sub.notified)
        per_device[sub.device_id] = per_device.get(sub.device_id, 0) + 1

    records: dict[str, Any] = {}
    tombstones: dict[str, Any] = {}
    for device_id, device in devices.items():
        if device.pushable:
            records[device_id] = _device_record(device)
            continue
        # The blob's own shape for a device that has journeys and no push subscription: the record
        # is ABSENT and the tombstone explains why, which is what `forget_device` writes.
        stored = dict(device.tombstone or {})
        tombstones[device_id] = {
            "reason": str(stored.get("reason") or "expired"),
            "ts": _as_iso(stored.get("at")) or instant.isoformat(),
            "created_ts": _as_iso(stored.get("original_created_at"))
            or _as_iso(device.created_at)
            or "",
            "journeys": per_device.get(device_id, 0),
        }

    existing: dict[str, Any] = {}
    raw = read_blob_text()
    if raw:
        try:
            existing = json.loads(raw)
        except ValueError:
            # A corrupt artefact is replaced rather than preserved: what it would carry through is
            # unreadable anyway, and refusing would leave the erasure unreached.
            logger.warning("The existing varsel state blob is unreadable; exporting without it")
    carried = {key: value for key, value in existing.items() if key not in _STATE_KEYS}
    payload = json.dumps(
        {
            **carried,
            # The one key the store has no home for, carried through from whatever is already
            # there rather than rebuilt or dropped.
            "togvarsel_rounds": existing.get("togvarsel_rounds", {}),
            "devices": records,
            "journeys": journeys,
            "notified": sorted(marks),
            "device_tombstones": tombstones,
        }
    )
    write_blob_text(payload)
    return ExportReport(
        devices=len(records),
        tombstones=len(tombstones),
        journeys=len(journeys),
        marks=len(marks),
        seconds=time.monotonic() - started,
        bytes_written=len(payload.encode()),
    )


def _as_iso(value: Any) -> str:
    """An ISO string from whatever the store handed back, or "". Never raises: an unreadable
    instant in a tombstone is a field, not a reason to lose the export."""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else ""
