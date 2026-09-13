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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

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


class RegistrationStore(Protocol):
    """What `/varsel` needs of wherever registrations live.

    Every method is a question the request path or the cycle actually asks. Ordering matters in one
    place only and it is documented on :meth:`save`.
    """

    # ---- the request path
    def device_count(self) -> int: ...
    def get_device(self, device_id: str) -> dict | None: ...
    def put_device(self, device_id: str, *, subscription: dict, brand: str) -> None: ...
    def forget_device(self, device_id: str, *, reason: str) -> tuple[dict | None, int]: ...
    def journey_count(self) -> int: ...
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


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
    """

    # ---- the request path
    def device_count(self) -> int:
        with lock:
            return len(_devices)

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

    def journey_count(self) -> int:
        with lock:
            return len(_journeys)

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
        with lock:
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
