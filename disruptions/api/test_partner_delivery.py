"""Checks for partner_delivery.py: one bounded attempt per pending event, and what stops the pass.

Hermetic, and hermetic in two distinct ways that both matter.

The Firestore fake runs ``partner_store``'s REAL bodies and applies their writes, so every
assertion is about the committed delivery documents. It is the fourth copy of that fake in this
directory (after ``test_partner_store``, ``test_partner_api`` and ``test_partner_events``), which
is the convention rather than an oversight: CI runs every suite as its own process, so a shared
helper module here would be a new import-time surface for all thirty of them.

The SENDER is stubbed, and ``webhook_client.deliver`` raises ``NetworkBlocked`` from a test process
that forgets to — so a missing stub is a loud failure rather than a request to a partner-supplied
host from CI. Nothing here opens a socket.

**The two timeout scenarios are deliberately two tests, and they catch different bugs.** A hanging
DESTINATION is bounded by ``webhook_client``'s own per-attempt ceiling, so what has to hold is the
reservation budget plus the per-destination skip. A slow STORE has no ceiling of its own —
``partner_store`` plumbs no ``timeout=`` into the Firestore client — so what has to hold is the
outer bounded wait on a pool that is abandoned rather than joined. One test cannot cover both, and
a suite with only the first would ship a pass a hung transaction can hold forever.
"""

from __future__ import annotations

import inspect
import json
import logging
import pathlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import partner_auth
import partner_delivery
import partner_events
import partner_store
import webhook_client
import webhook_signing

NOW = datetime(2026, 9, 8, 6, 30, tzinfo=UTC)
SID = "sub-1"
PARTNER = "partner-a"
DEST = "dest-a"
URL = "https://receiver.example.com/hooks/kix"
PEPPER = "p" * 48
SIGNING_ROOT = "r" * 48
MODULE_SOURCE = pathlib.Path(partner_delivery.__file__).read_text()
SCHEMA_PATH = pathlib.Path("contracts/notifications/v1/event.schema.json")


def _code() -> str:
    """The module minus its own docstring and comment lines.

    Every header in `partner_delivery.py` explains what it deliberately does NOT do -- it names
    `varsel._state_lock`, `shutdown(wait=True)` and `decisions` in order to refuse them -- so a
    negative grep over the raw text matches the explanation and passes for the wrong reason.
    """
    body = MODULE_SOURCE.split('"""', 2)[-1]
    return "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )


# ---- Fakes -------------------------------------------------------------------------------------


class _Snap:
    def __init__(self, path: str, data: dict[str, Any] | None, client: Any) -> None:
        self._path, self._data, self._client = path, data, client

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None

    @property
    def reference(self) -> Any:
        return _Ref(self._client, self._path)


class _Ref:
    def __init__(self, client: Any, path: str) -> None:
        self.client, self.path = client, path

    def get(self, transaction: Any = None, timeout: float | None = None) -> _Snap:
        # `timeout` is accepted and ignored: every read in `partner_store` now carries its own
        # Firestore deadline (KTD6 -- an abandoned worker thread with no call deadline delays
        # pod termination via `concurrent.futures`' atexit join). `test_partner_store` is where
        # the deadline is asserted; this double only has to tolerate the kwarg.
        del timeout
        return _Snap(self.path, self.client.docs.get(self.path), self.client)


class _Collection:
    def __init__(self, client: Any, name: str) -> None:
        self.client, self.name = client, name

    def document(self, doc_id: str) -> _Ref:
        return _Ref(self.client, f"{self.name}/{doc_id}")


class _Page:
    def __init__(self, client: Any, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self.client, self.rows = client, rows

    def stream(self, timeout: float | None = None) -> list[_Snap]:
        del timeout
        return [_Snap(path, data, self.client) for path, data in self.rows]


class _Txn:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str, dict[str, Any] | None]] = []

    def set(self, ref: _Ref, data: dict[str, Any]) -> None:
        self.writes.append(("set", ref.path, dict(data)))

    def update(self, ref: _Ref, data: dict[str, Any]) -> None:
        self.writes.append(("update", ref.path, dict(data)))

    def delete(self, ref: _Ref) -> None:
        self.writes.append(("delete", ref.path, None))

    def get(self, query: Any, timeout: float | None = None) -> list[_Snap]:
        del timeout
        return list(query.stream())


class _Fake:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.transactions = 0
        self.delay_s = 0.0
        self.hang: threading.Event | None = None

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)

    def put(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self.docs[f"{collection}/{doc_id}"] = dict(data)

    def doc(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        stored = self.docs.get(f"{collection}/{doc_id}")
        return dict(stored) if stored is not None else None

    def in_collection(self, collection: str) -> list[dict[str, Any]]:
        return [
            dict(data) for path, data in self.docs.items() if path.split("/", 1)[0] == collection
        ]


class _MemoryDocs:
    """``partner_auth._Docs`` over a dict."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict[str, Any]] = {}

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        stored = self.data.get((collection, doc_id))
        return dict(stored) if stored is not None else None

    def set(self, collection: str, doc_id: str, doc: Any) -> None:
        self.data[(collection, doc_id)] = dict(doc)

    def update(self, collection: str, doc_id: str, patch: Any) -> None:
        self.data.setdefault((collection, doc_id), {}).update(dict(patch))

    def list_by(self, collection: str, field: str, value: Any, *, limit: int) -> list[dict]:
        return [
            dict(doc)
            for (coll, _), doc in self.data.items()
            if coll == collection and doc.get(field) == value
        ][:limit]


_saved: dict[str, Any] = {}
_sent: list[dict[str, Any]] = []
_result: dict[str, Any] = {}
_pools: list[ThreadPoolExecutor] = []
# How long a released worker gets to finish before the drain calls it a leak. Generous, because it
# only has a fake dict write and a stub sender left to do once the hang is released; a thread still
# alive after this is a real defect and failing on it beats polluting whatever runs next.
_DRAIN_TIMEOUT_S = 5.0


class _RecordingPool(ThreadPoolExecutor):
    """Every pool the module under test creates, kept so a test can drain what it ABANDONED.

    `deliver_pending` calls `shutdown(wait=False)` on purpose and must keep doing so: a thread
    parked in a socket read cannot be killed, and waiting for it would defeat the exact bound the
    pass exists to hold. Right in production, unacceptable in a suite -- the released worker ran on
    into the NEXT test. It appended to the module-global `_sent`, which made
    `test_a_store_failure_writes_nothing_and_re_offers_the_delivery`'s `not _sent` fail about one
    run in nine (measured: 2 of 18 under parallel load, 0 of 12 unloaded -- so a serial re-run
    "proving" it fixed proves nothing), and it wrote through a fake that `_uninstall` had already
    swapped out, printing a `NotFound` traceback on every single run.

    Draining costs the tests nothing they were asserting: every bound is asserted BEFORE
    `_uninstall` runs, so the property under test is still that the PASS returned inside its
    deadline while its workers were still parked.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _pools.append(self)


def _drain_pools(client: Any) -> None:
    """Release any injected hang, then join every worker the pass walked away from."""
    if client is not None and getattr(client, "hang", None) is not None:
        # Before the join, never after: the fake's own `hang.wait(30)` would otherwise be what
        # bounds the drain, and a 30 s pause per hang test reads as a hung suite.
        client.hang.set()
    if client is not None:
        client.delay_s = 0.0
    stragglers: list[str] = []
    for pool in _pools:
        # `_threads` is private, and a test double is where reaching for it is honest: the public
        # API offers only `shutdown(wait=True)`, which takes no timeout, so a worker that never
        # returns would wedge the suite instead of reporting a leak.
        for thread in list(getattr(pool, "_threads", ())):
            thread.join(timeout=_DRAIN_TIMEOUT_S)
            if thread.is_alive():
                stragglers.append(thread.name)
    _pools.clear()
    if stragglers:
        raise AssertionError(
            "worker thread(s) outlived the test that abandoned them: "
            f"{stragglers}. A straggler mutates whatever runs next, which is how this suite came "
            "to fail one run in nine with every check passing."
        )


def _sortable(value: Any) -> Any:
    return value if isinstance(value, datetime) else datetime.min.replace(tzinfo=UTC)


def _install() -> _Fake:
    import os

    client = _Fake()
    _saved.update(
        {
            "client": partner_store._client,
            "run": partner_store._run_transaction,
            "query": partner_store._query,
            "pool": partner_delivery.ThreadPoolExecutor,
            "emit_pool": partner_events.ThreadPoolExecutor,
            "fake": client,
        }
    )
    # BOTH pools, because `_pending()` reaches the real emit path and `partner_events.emit` makes
    # its own. No test here hangs the client before `_pending`, so an emit worker cannot straggle
    # today -- and closing it costs one line, where discovering it later costs another afternoon of
    # bisecting a suite that fails one run in nine.
    partner_delivery.ThreadPoolExecutor = _RecordingPool  # type: ignore[misc]
    partner_events.ThreadPoolExecutor = _RecordingPool  # type: ignore[misc]
    _pools.clear()
    partner_store._client = lambda: client  # type: ignore[assignment]

    def _run(body: Any) -> Any:
        if client.hang is not None:
            client.hang.wait(30)  # bounded so a failing assertion cannot wedge the suite
        if client.delay_s:
            time.sleep(client.delay_s)
        txn = _Txn()
        # Serialized, because the delivery pass is 4-wide and this fake's read-modify-write over a
        # plain dict is not atomic: without the lock a concurrency test would fail on the fake
        # rather than on the code.
        with client.lock:
            client.transactions += 1
            result = body(txn)
            for kind, path, data in txn.writes:
                if kind == "set":
                    client.docs[path] = dict(data or {})
                elif kind == "update":
                    client.docs.setdefault(path, {}).update(data or {})
                else:
                    client.docs.pop(path, None)
        return result

    def _query(name: str, *, equality: Any = None, inequality: Any = None, limit: int) -> _Page:
        spec = partner_store._SPECS_BY_NAME[name]
        rows: list[tuple[str, dict[str, Any]]] = []
        for path, data in list(client.docs.items()):
            if path.split("/", 1)[0] != spec.collection:
                continue
            if any(data.get(f) != (equality or {})[f] for f in spec.equality):
                continue
            keep = True
            for field_path, op in spec.inequality:
                value = data.get(field_path)
                bound = (inequality or {})[field_path]
                if op == "<=" and not (value is not None and value <= bound):
                    keep = False
            if keep:
                rows.append((path, dict(data)))
        for field_path, direction in reversed(spec.order_by):
            rows.sort(
                key=lambda row, f=field_path: _sortable(row[1].get(f)),
                reverse=direction == "DESCENDING",
            )
        return _Page(client, rows[:limit])

    partner_store._run_transaction = _run  # type: ignore[assignment]
    partner_store._query = _query  # type: ignore[assignment]
    partner_store.configure(client=client, clock=lambda: NOW)

    os.environ[partner_auth.PEPPER_ENV] = PEPPER
    os.environ[webhook_signing.SIGNING_ROOT_ENV] = SIGNING_ROOT
    partner_auth.configure(docs=_MemoryDocs(), clock=lambda: NOW)
    partner_auth.create_partner(PARTNER, name="Partner A", actor="op@entur.org")
    partner_auth.create_destination(DEST, partner_id=PARTNER, callback_url=URL, actor="op@entur.org")

    _sent.clear()
    _result.clear()
    _result.update({"ok": True, "status": 200})
    return client


def _uninstall() -> None:
    import os

    _drain_pools(_saved.get("fake"))
    if _saved:
        partner_delivery.ThreadPoolExecutor = _saved["pool"]  # type: ignore[misc]
        partner_events.ThreadPoolExecutor = _saved["emit_pool"]  # type: ignore[misc]
        partner_store._client = _saved["client"]  # type: ignore[assignment]
        partner_store._run_transaction = _saved["run"]  # type: ignore[assignment]
        partner_store._query = _saved["query"]  # type: ignore[assignment]
        _saved.clear()
    partner_store.reset_for_tests()
    partner_auth.reset_for_tests()
    os.environ.pop(partner_auth.PEPPER_ENV, None)
    os.environ.pop(webhook_signing.SIGNING_ROOT_ENV, None)


def _sender(**over: Any):
    """A stub in ``webhook_client.deliver``'s exact shape, recording what was sent."""
    hang: threading.Event | None = over.pop("hang", None)

    def send(url, *, body, headers, timeout_s, connect_timeout_s):
        if hang is not None:
            hang.wait(30)
        _sent.append({"url": url, "body": body, "headers": dict(headers)})
        return webhook_client.DeliveryAttempt(**{**_result, **over})

    return send


def _spec(**over: Any) -> partner_store.SubscriptionSpec:
    base: dict[str, Any] = {
        "subscription_id": SID,
        "partner_id": PARTNER,
        "destination_id": DEST,
        "from_stop": "NSR:StopPlace:337",
        "to_stop": "NSR:StopPlace:418",
        "begin": "05:00",
        "end": "12:00",
        "days": (0, 1, 2, 3, 4, 5, 6),
        "partner_ref": "commuter-42",
        "test": False,
    }
    base.update(over)
    return partner_store.SubscriptionSpec(**base)


def _live(client: _Fake, **over: Any) -> None:
    spec = _spec(**over)
    client.put(
        partner_store.SUBSCRIPTIONS,
        spec.subscription_id,
        partner_store._new_subscription_doc(spec, NOW),
    )


def _subject(**over: Any) -> partner_events.PartnerSubject:
    base: dict[str, Any] = {
        "subscription_id": SID,
        "partner_id": PARTNER,
        "destination_id": DEST,
        "subscription_version": 1,
        "partner_ref": "commuter-42",
        "alertworthy": True,
        "cancelled": True,
        "situations": ("ENT:SituationNumber:1",),
        "operator": "VYG",
        "line": "VYG:Line:L1",
        "journey_ref": "DatedServiceJourney:1234",
        "stop_place": "NSR:StopPlace:337",
        "stop_order": 3,
        "departure_time": NOW + timedelta(minutes=20),
        "title": "Tog L1 08:50 er innstilt",
        "body": "Oslo S → Ski\nInnstilt",
        "payload": {"notification": {"title": "Tog L1 08:50 er innstilt", "body": "Oslo S → Ski"}},
    }
    base.update(over)
    return partner_events.PartnerSubject(**base)


def _pending(client: _Fake, n: int = 1, **over: Any) -> list[dict[str, Any]]:
    """Emit `n` events through the REAL emit path, so the pending records are the real shape."""
    subjects = [
        _subject(
            **{
                "journey_ref": f"DatedServiceJourney:{1000 + i}",
                "departure_time": NOW + timedelta(minutes=10 + 5 * i),
                **over,
            }
        )
        for i in range(n)
    ]
    partner_events.emit(subjects, now=NOW)
    return sorted(
        client.in_collection(partner_store.DELIVERIES), key=lambda d: d["valid_until"]
    )


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# Every fault this suite injects is logged by the code under test with `exc_info=True`, which is
# correct of the code and was being PRINTED: with no handler anywhere in the hierarchy, `logging`
# falls back to `lastResort`, which writes the record and its traceback to stderr. So a green run
# ended in a stack trace -- and a standing traceback in the log is how a real one stops being read,
# which is the argument `_store_step` itself makes about swallowing store failures.
#
# A collecting sink, not a `NullHandler`: it satisfies `callHandlers` so `lastResort` never fires,
# AND it keeps the record, so the two tests that inject a fault assert it was logged rather than
# merely stopping it being seen. Attached to the loggers this suite drives -- both the module under
# test and the store it calls, since `partner_store` warns on its own paths too.
_SINK = _Records()
for _name in ("partner_delivery", "partner_store"):
    _sink_logger = logging.getLogger(_name)
    _sink_logger.setLevel(logging.INFO)
    _sink_logger.addHandler(_SINK)


def _sink_since(mark: int, name: str) -> list[dict]:
    """The structured fields of records the sink collected after `mark`."""
    out = []
    for record in _SINK.records[mark:]:
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields.get("event") == name:
            out.append(fields)
    return out


def _capture() -> _Records:
    handler = _Records()
    logger = logging.getLogger("partner_delivery")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler


def _fields(handler: _Records, name: str) -> list[dict]:
    out = []
    for record in handler.records:
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields.get("event") == name:
            out.append(fields)
    logging.getLogger("partner_delivery").removeHandler(handler)
    return out


# ---- The happy path, and what a success writes -------------------------------------------------


def test_a_pending_delivery_is_signed_sent_once_and_recorded_sent():
    """The whole path, and the two things a success has to write TOGETHER.

    `sent` on the delivery and the Oslo-day counter on the subscription are one transaction: split,
    they disagree after a restart, and the exemption failing open would suppress a confirmed
    cancellation. The delivered situation marks go with them — Firestore's copy of `_notified`, at
    the grain `_pending_pushes` asks its question at.

    The `webhook-id` IS the delivery id, and the body is signed over the exact bytes sent.
    """
    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client)
        report = partner_delivery.deliver_pending(now=NOW, sender=_sender())
        assert report.sent == 1 and report.ready == 1
        stored = client.doc(partner_store.DELIVERIES, delivery["delivery_id"])
        assert stored["state"] == "sent" and stored["attempts"] == 1
        subscription = client.doc(partner_store.SUBSCRIPTIONS, SID)
        assert subscription["daily_deliveries"] == {partner_store.oslo_day(NOW): 1}
        assert "ENT:SituationNumber:1" in subscription["delivered"]

        [attempt] = _sent
        assert attempt["url"] == URL
        assert attempt["headers"][webhook_signing.HEADER_ID] == delivery["delivery_id"]
        # Signed over what was SENT. Re-serializing between signing and sending is the canonical
        # webhook bug: the signature then covers a document the receiver did not get.
        destination = partner_auth.destination_for_delivery(DEST)
        secret = webhook_signing.derive_secret(DEST, destination.active_generation)
        assert webhook_signing.verify(
            secret=secret,
            webhook_id=delivery["delivery_id"],
            timestamp=attempt["headers"][webhook_signing.HEADER_TIMESTAMP],
            body=attempt["body"],
            header=attempt["headers"][webhook_signing.HEADER_SIGNATURE],
            now=int(attempt["headers"][webhook_signing.HEADER_TIMESTAMP]),
        ), "the bytes sent must verify under the destination's own derived secret"
        wire = json.loads(attempt["body"].decode())
        assert wire["event_id"] == delivery["event_id"] and wire["revision"] >= 1
        assert partner_store.TTL_FIELD not in wire, (
            "the TTL field is a RETENTION decision of ours; on the wire it would read as a "
            "deletion date the partner is entitled to rely on"
        )
        assert wire["payload"]["notification"]["title"] == "Tog L1 08:50 er innstilt"
    finally:
        _uninstall()


def test_nothing_is_attempted_twice_in_one_cycle():
    """One attempt per pending event per cycle: the next cycle is the retry."""
    client = _install()
    try:
        _live(client)
        _pending(client)
        partner_delivery.deliver_pending(now=NOW, sender=_sender())
        assert len(_sent) == 1
        # A second pass in the same cycle finds nothing: the record is `sent`, not `pending`.
        assert partner_delivery.deliver_pending(now=NOW, sender=_sender()).ready == 0
        assert len(_sent) == 1
    finally:
        _uninstall()


# ---- Scenario 2 / AE3: the daily cap and its recorded suppressions -----------------------------


def test_thirteen_pending_events_yield_four_notifications_and_nine_recorded_suppressions():
    """AE3's delivery half: at most four notifications per subscription per Oslo day.

    The nine others are RECORDED (`capped_at`/`capped_count`) and left `pending`, so the next cycle
    inside their validity offers them again — the cap is a per-day allowance, not a verdict on the
    event. Recording them is what makes the suppression visible rather than a delivery that quietly
    never happened, and KTD12 needs exactly those rows: this plan manufactures capped subjects by
    design, nine of thirteen here.

    **What "spent on the soonest" can and cannot be asserted as, because two things loosen it and
    both are real.** `pending_deliveries` sorts on `valid_until`, which IS the departure for a train
    inside the 60-minute horizon and clamps to the horizon beyond it, so among trains further out
    the order falls back to the delivery id — nothing on a delivery record carries the departure.
    And the cap is READ by `claim_delivery` and SPENT by `mark_delivery_sent`, so across a 4-wide
    pool up to `_WORKERS` claims can pass the check before any increment lands: measured on exactly
    this shape, the four delivered were the 1st, 2nd, 3rd and 6th soonest rather than the first
    four. The COUNT is exact — Firestore's counter is authoritative — and the loosening is bounded
    by the pool width.

    So this asserts what holds: exactly four notifications, nine recorded suppressions, a counter
    reading four, and the soonest departure among the four (it is submitted first, and the first
    round always has allowance). Tightening the *which* would mean claiming sequentially, and the
    claim is a store call — sequential claims for a two-hundred-deep backlog are what the pool
    exists to avoid.
    """
    client = _install()
    try:
        _live(client)
        deliveries = _pending(client, n=13)
        assert len(deliveries) == 13
        report = partner_delivery.deliver_pending(now=NOW, sender=_sender())
        # The unconditional invariant: every one of the thirteen was accounted for.
        assert report.sent + report.capped == 13, report
        assert report.sent == partner_store.MAX_ALERT_DELIVERIES_PER_DAY == 4
        assert report.capped == 9
        # WHY THOSE TWO ARE EXACT, because it is not unconditional and a reader who assumes it is
        # will mis-diagnose the day it moves. R11's cap is READ by `claim_delivery` and SPENT by
        # `mark_delivery_sent`, so concurrent claims can be admitted against the same last slot and
        # the general bound is `MAX + workers - 1`, not `MAX`. It is exact here because the budget
        # (4) and the pool width (4) are equal: the first wave of claims exhausts the allowance, and
        # every later claim reads nothing left. Change either number and this becomes a range —
        # and if it ever flakes, the claim/send window is the cause, not contamination. The
        # looseness itself is a reported finding against the store's cap enforcement, not something
        # to widen this assertion for: `<= 7` would quietly accept a traveller losing a slot.
        assert partner_delivery._WORKERS == partner_store.MAX_ALERT_DELIVERIES_PER_DAY, (
            "the two exact assertions above rest on these being equal; re-derive them, do not "
            "relax them, if this fires"
        )

        stored = {d["delivery_id"]: client.doc(partner_store.DELIVERIES, d["delivery_id"]) for d in deliveries}
        sent = [d for d in stored.values() if d["state"] == "sent"]
        capped = [d for d in stored.values() if d["state"] == "pending"]
        assert len(sent) == 4 and len(capped) == 9
        assert all(d.get("capped_count") == 1 for d in capped), "every suppression is recorded"
        soonest = deliveries[0]["valid_until"]
        assert soonest in [d["valid_until"] for d in sent], (
            "the soonest departure is always among the four: it is submitted first and the first "
            "round of claims always has allowance"
        )
        assert max(d["valid_until"] for d in sent) <= deliveries[
            partner_store.MAX_ALERT_DELIVERIES_PER_DAY + partner_delivery._WORKERS - 1
        ]["valid_until"], (
            "and the loosening is bounded by the pool width -- see the docstring: the cap is read "
            "at claim and spent at send, so up to `_WORKERS` claims pass before any increment lands"
        )
        assert client.doc(partner_store.SUBSCRIPTIONS, SID)["daily_deliveries"] == {
            partner_store.oslo_day(NOW): 4
        }
    finally:
        _uninstall()


def test_a_cap_exempt_confirmation_still_reaches_a_subscription_whose_four_are_spent():
    """Scenario 8's other half: the cap counter and its exemption both survive a restart.

    Both live on Firestore documents, so "survives a restart" is a property of where they are
    written rather than of anything this process holds — which is why the assertion below simply
    re-installs the module state and keeps the documents.
    """
    client = _install()
    try:
        _live(client)
        # Four FORECASTS, so the fifth event below is a real predicted -> confirmed transition.
        # Emitted as cancelled they would already be `confirmed`, and the confirmation would mint
        # nothing at all -- which would pass a weaker assertion for the wrong reason.
        _pending(client, n=4, cancelled=False)
        assert partner_delivery.deliver_pending(now=NOW, sender=_sender()).sent == 4

        # A confirmation about a train already delivered: exempt at MINT.
        subjects = [_subject(journey_ref="DatedServiceJourney:1000", cancelled=True)]
        partner_events.emit(subjects, now=NOW + timedelta(minutes=1))
        docs = dict(client.docs)
        _uninstall()
        client = _install()
        client.docs.clear()
        client.docs.update(docs)

        report = partner_delivery.deliver_pending(now=NOW + timedelta(minutes=2), sender=_sender())
        assert report.sent == 1 and report.capped == 0, (
            "a confirmation that replaces a delivered forecast is exempt from the cap (R11)"
        )
        assert client.doc(partner_store.SUBSCRIPTIONS, SID)["daily_deliveries"] == {
            partner_store.oslo_day(NOW): 4
        }, "and spends no slot"
    finally:
        _uninstall()


# ---- Scenario 3 / AE7 + AE14: the two ways a pass can be held ----------------------------------


def test_a_hanging_destination_is_skipped_and_the_pass_ends_inside_its_deadline():
    """AE7: the callback hangs and thirty events are pending for it.

    Two mechanisms, both required. The per-attempt ceiling bounds ONE attempt; the pass-local skip
    is what stops the rest being attempted at all after the first failure, which is the difference
    between at most one timeout per worker and thirty of them. Skipped rather than recorded: they stay pending and
    the next cycle asks again.

    The skip is deliberately PASS-local. Module-level and swept it would pause a destination
    permanently, which is indistinguishable from the intended 410 pause.
    """
    client = _install()
    try:
        _live(client)
        _pending(client, n=30)
        started = time.monotonic()
        report = partner_delivery.deliver_pending(
            now=NOW,
            sender=_sender(ok=False, retryable=True, error="TimeoutError"),
            deadline_s=1.0,
            wait_s=2.0,
            timeout_s=0.05,
            store_call_s=0.05,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"the pass must end inside its deadline; took {elapsed:.2f}s"
        # The INVARIANT is that every one of the thirty events was accounted for and that the
        # destination went out after its first failure — not the split between the two, which is
        # how many workers had already been dispatched when that first failure landed. Bounded
        # above by the pool width and never by one: measured with the real 5 s per-attempt ceiling
        # this pass makes FOUR attempts and skips 26, so the `== 1` this used to assert was reading
        # the unloaded interleaving of a narrow timeout as a property of the mechanism.
        assert 1 <= len(_sent) <= partner_delivery._WORKERS, (
            f"at least one attempt, at most one per worker; got {len(_sent)}"
        )
        assert report.retryable == len(_sent), report
        assert report.retryable + report.skipped == 30, report
        assert report.skipped_destinations == 1, report
        # And nothing terminal was written for the skipped ones.
        states = {d["state"] for d in client.in_collection(partner_store.DELIVERIES)}
        assert states == {"pending"}, states
    finally:
        _uninstall()


def test_a_slow_store_cannot_extend_the_pass_past_its_bounded_wait():
    """AE14's slow-store half, and the failure the reservation CANNOT see.

    `partner_store` plumbs no `timeout=` into the Firestore client, so a hung transaction has no
    ceiling of its own and the reservation reserves a worst case the call then exceeds without
    limit. What holds is the outer bounded wait on a pool that is ABANDONED — `shutdown(wait=True)`
    would block the poller until the hung call finally returned and defeat the bound completely.

    Deliberately a real clock and a real thread rather than a mocked one: the property being checked
    is that the ingestion thread comes back, and a mocked clock cannot fail that way.
    """
    client = _install()
    try:
        _live(client)
        _pending(client, n=4)
        client.hang = threading.Event()
        handler = _capture()
        started = time.monotonic()
        report = partner_delivery.deliver_pending(
            now=NOW,
            sender=_sender(),
            wait_s=0.15,
            # The reservation must NOT be what binds here, or the tasks never reach the store and
            # the test would pass without exercising the hang at all: a per-attempt worst case of
            # `timeout_s + 3 * store_call_s` against a small deadline refuses every claim.
            deadline_s=5.0,
            timeout_s=0.05,
            store_call_s=0.05,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"a hung store must not hold the poller thread; took {elapsed:.1f}s"
        assert report.timed_out is True and report.sent == 0
        assert not _sent, "nothing was dialled: the claim never returned"
        assert {f["result"] for f in _fields(handler, "partner_delivery_round")} == {"timeout"}
    finally:
        if client.hang is not None:
            client.hang.set()
        _uninstall()


def test_the_deadline_binds_before_the_backlog_does():
    """The reservation is a ceiling on when the pass ENDS, not on when the last attempt starts.

    Checking elapsed time would admit an attempt with a millisecond to spare and then run its full
    worst case — the shape that overshot a documented 30 s ceiling by 16 s
    (`varsel._WriteBudget`'s own measurement). With a per-attempt worst case reserved, a backlog
    larger than the deadline allows leaves the remainder `capped` and the pass inside its bound.
    """
    client = _install()
    try:
        _live(client, days=(0, 1, 2, 3, 4, 5, 6))
        _pending(client, n=20)
        started = time.monotonic()
        report = partner_delivery.deliver_pending(
            now=NOW,
            sender=_sender(),
            deadline_s=0.3,
            wait_s=3.0,
            timeout_s=0.1,
            store_call_s=0.1,
            workers=1,
        )
        elapsed = time.monotonic() - started
        assert report.capped, f"the reservation must bind on a backlog this size; got {report}"
        assert elapsed < 1.5, f"and the pass ends inside its deadline; took {elapsed:.2f}s"
        assert report.sent + report.capped + report.skipped == 20
    finally:
        _uninstall()


# ---- Scenario 4 / AE6 + AE12: the four terminal rules ------------------------------------------


def test_a_503_stays_pending_and_a_deletion_then_cancels_it():
    """AE6: the callback returns 503, then the subscription is deleted before the next cycle.

    The 503 is retryable, so the record stays `pending` and no cap slot is spent — dedup is on
    SUCCESS, exactly as /varsel's is, or a dead destination burns a subscription's four for the day
    with nothing delivered. The deletion is then caught by `claim_delivery`, which is called
    immediately before dispatch (R13's recheck) and writes the terminal state itself so nothing
    re-offers it.
    """
    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client)
        report = partner_delivery.deliver_pending(
            now=NOW, sender=_sender(ok=False, status=503, retryable=True, error="http_503")
        )
        assert report.retryable == 1
        stored = client.doc(partner_store.DELIVERIES, delivery["delivery_id"])
        assert stored["state"] == "pending" and stored["attempts"] == 1
        assert client.doc(partner_store.SUBSCRIPTIONS, SID).get("daily_deliveries") in (None, {})

        # The subscription is deleted (tombstoned) before the next cycle.
        client.docs[f"{partner_store.SUBSCRIPTIONS}/{SID}"]["status"] = "deleted"
        _sent.clear()
        report = partner_delivery.deliver_pending(now=NOW + timedelta(minutes=1), sender=_sender())
        assert report.cancelled == 1 and not _sent, "no further attempt for it"
        stored = client.doc(partner_store.DELIVERIES, delivery["delivery_id"])
        assert stored["state"] == "cancelled" and stored["cancelled_reason"] == "deleted"
    finally:
        _uninstall()


def test_a_departed_train_becomes_expired_rather_than_failed_and_is_not_retried():
    """AE12: the train departs while an event is still undelivered.

    `expired`, never `failed` — "we ran out of time to tell you" and "your endpoint rejected us" are
    different operator problems, and collapsing them would hide a dead destination inside ordinary
    expiry. And an event past the 60-minute horizon is the same state by a different route, because
    validity is the EARLIER of the horizon and the departure.
    """
    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client)
        after_departure = delivery["valid_until"] + timedelta(minutes=1)
        report = partner_delivery.deliver_pending(now=after_departure, sender=_sender())
        assert report.expired == 1 and not _sent, "not retried"
        assert client.doc(partner_store.DELIVERIES, delivery["delivery_id"])["state"] == "expired"

        # The horizon, for a departure far enough out that it is the binding term.
        client.docs.clear()
        _live(client)
        [far] = _pending(client, departure_time=NOW + timedelta(hours=6))
        horizon = NOW + timedelta(minutes=partner_store.EVENT_VALIDITY_MINUTES)
        assert far["valid_until"] == horizon, "60 minutes, not the departure"
        _sent.clear()
        report = partner_delivery.deliver_pending(now=horizon + timedelta(seconds=1), sender=_sender())
        assert report.expired == 1 and not _sent
    finally:
        _uninstall()


def test_a_410_pauses_the_destination_until_an_operator_resumes_it():
    """R12's pause, and the two records it touches.

    The DELIVERY is `cancelled` rather than `failed` (nothing was wrong with the event; the endpoint
    is gone), and the DESTINATION is paused — which is a field on a record `mark_delivery_failed`
    deliberately does not reach. The pause is what stops the next cycle re-attempting it, asserted
    by the second pass below rather than by the flag alone.
    """
    client = _install()
    try:
        _live(client)
        deliveries = _pending(client, n=2)
        report = partner_delivery.deliver_pending(
            now=NOW,
            sender=_sender(
                ok=False, status=410, retryable=False, terminal_state="cancelled",
                pause_destination=True, error="http_410",
            ),
            # ONE worker, so this pass is about R12's pause and not about how four of them
            # interleave. With the default width both events are dispatched at once and it is the
            # scheduler that decides whether the second is attempted or skipped — which leaves the
            # second delivery's state, and therefore the resume assertion at the end, riding on
            # timing. The race itself is covered where it belongs: by the invariant just below and
            # by `test_a_hanging_destination_is_skipped_and_the_pass_ends_inside_its_deadline`.
            workers=1,
        )
        # Both numbers are invariants of the WORK, not of how the four workers interleaved:
        # two pending events against one gone endpoint, one destination for an operator to resume.
        # `paused` counted pause CALLS until this was found flaky under load — the second worker's
        # skip-set check races the first worker's write — so it now counts distinct destinations off
        # a set, and the per-event outcome moved to `cancelled`.
        assert report.paused == 1, report
        # The invariant is the SUM: two pending events, each either attempted or skipped because
        # the destination went out. Asserted in that shape deliberately, because at the default
        # pool width the split is decided by whether the second worker reached the skip check
        # before or after the first wrote it — an artefact of scheduling, not a property of the
        # code, and `paused` itself used to be counted that way and flaked one run in six.
        assert report.cancelled + report.skipped == 2, report
        assert (report.cancelled, report.skipped) == (1, 1), report
        assert client.doc(partner_store.DELIVERIES, deliveries[0]["delivery_id"])["state"] == "cancelled"
        destination = partner_auth.destination_for_delivery(DEST)
        assert destination.paused is True and destination.deliverable is False

        _sent.clear()
        report = partner_delivery.deliver_pending(now=NOW + timedelta(minutes=1), sender=_sender())
        assert not _sent and report.not_deliverable >= 1, (
            "a paused destination is read BEFORE the attempt, not rediscovered by it"
        )
        # And the event is left pending rather than thrown away on an operator's behalf.
        assert client.doc(partner_store.DELIVERIES, deliveries[1]["delivery_id"])["state"] == "pending"

        partner_auth.resume_destination(DEST, actor="op@entur.org")
        assert partner_delivery.deliver_pending(now=NOW + timedelta(minutes=2), sender=_sender()).sent == 1
    finally:
        _uninstall()


def test_a_4xx_other_than_410_is_terminal_for_that_event_only():
    """The classification `webhook_client` owns, passed straight through to the store.

    Both `terminal_state` values are pinned as members of `partner_store.DELIVERY_STATES` by U5's
    suite, which is what makes passing them through safe rather than a coincidence.
    """
    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client)
        report = partner_delivery.deliver_pending(
            now=NOW,
            sender=_sender(ok=False, status=400, retryable=False, terminal_state="failed", error="http_400"),
        )
        assert report.failed == 1
        assert client.doc(partner_store.DELIVERIES, delivery["delivery_id"])["state"] == "failed"
        assert partner_auth.destination_for_delivery(DEST).paused is False, (
            "only a 410 pauses the destination; a 400 is this event's problem"
        )
    finally:
        _uninstall()


# ---- Scenario 6 / AE15: a restart between commit and delivery ----------------------------------


def test_a_restart_between_commit_and_delivery_resends_the_original_id_and_revision():
    """AE15, and it holds because the pending set is the STORE's.

    A re-derivation from a cycle's decisions would mint a fresh revision and therefore a fresh
    `event_id`, which the receiver cannot recognise as a duplicate and would turn into a second
    notification about the same train. Here the process forgets everything and the same delivery id
    and revision go out — with a FRESH timestamp and signature, because a retry reusing the first
    attempt's timestamp lands outside the receiver's five-minute replay window, where a correct
    receiver drops our own retry as an attack.
    """
    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client)
        partner_delivery.deliver_pending(
            now=NOW, sender=_sender(ok=False, retryable=True, error="TimeoutError")
        )
        first = dict(_sent[-1])

        docs = dict(client.docs)
        _uninstall()
        client = _install()
        client.docs.clear()
        client.docs.update(docs)
        later = NOW + timedelta(minutes=5)
        # A fresh timestamp needs a moved wall clock, which `webhook_signing.sign` reads directly.
        real_time = time.time
        try:
            time.time = lambda: real_time() + 600  # type: ignore[assignment]
            partner_delivery.deliver_pending(now=later, sender=_sender())
        finally:
            time.time = real_time  # type: ignore[assignment]

        second = _sent[-1]
        assert second["headers"][webhook_signing.HEADER_ID] == first["headers"][webhook_signing.HEADER_ID]
        assert json.loads(second["body"])["revision"] == json.loads(first["body"])["revision"]
        assert second["headers"][webhook_signing.HEADER_TIMESTAMP] != first["headers"][
            webhook_signing.HEADER_TIMESTAMP
        ], "a retry must re-mint its timestamp or a correct receiver drops it"
        assert second["headers"][webhook_signing.HEADER_SIGNATURE] != first["headers"][
            webhook_signing.HEADER_SIGNATURE
        ]
        assert client.doc(partner_store.DELIVERIES, delivery["delivery_id"])["attempts"] == 2
    finally:
        _uninstall()


def test_an_event_past_its_expiry_is_never_sent_even_on_a_first_attempt():
    """The other half of scenario 6: expiry is checked on the way out, not only on the way in."""
    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client)
        # Past the event's own `valid_until` but still inside its seven-day retention, which is the
        # window where a naive implementation would happily send it: `_not_expired` says the document
        # is alive, and only `event_is_valid` says the EVENT is not.
        report = partner_delivery.deliver_pending(
            now=delivery["valid_until"] + timedelta(seconds=1), sender=_sender()
        )
        assert not _sent and report.expired == 1
    finally:
        _uninstall()


# ---- Scenario 9: the next cycle, and a persisted Retry-After -----------------------------------


def test_a_destination_that_failed_in_one_cycle_is_attempted_again_in_the_next():
    """The skip is pass-local, which is what makes the next cycle a real retry.

    Made module-level and swept, one failure would pause a destination permanently — and a permanent
    pause with no operator action behind it is indistinguishable from the intended 410 pause, which
    is the diagnosis it would destroy.
    """
    client = _install()
    try:
        _live(client)
        _pending(client, n=2)
        first = partner_delivery.deliver_pending(
            now=NOW, sender=_sender(ok=False, retryable=True, error="TimeoutError")
        )
        # The sum, not the split: two events against one failing destination, and whether the
        # second was attempted or skipped depends on whether it reached the skip check before the
        # first worker wrote it. `skipped_destinations` is the invariant that matters here — the
        # destination went out — and the next pass proves the skip did not outlive the cycle.
        assert first.skipped_destinations == 1, first
        assert first.retryable + first.skipped == 2, first
        _sent.clear()
        second = partner_delivery.deliver_pending(now=NOW + timedelta(minutes=1), sender=_sender())
        assert second.sent == 2, f"both are attempted on the next cycle; got {second}"
    finally:
        _uninstall()


def test_a_429_retry_after_is_persisted_and_still_deferred_after_a_restart():
    """Scenario 9's second half: the backoff is a stored timestamp, not a pass-local sleep.

    `retry_not_before` is on the delivery record and the delivery QUERY filters on it, so the
    backoff survives a cycle boundary and a restart. Capped at the lesser of the header, the time to
    departure and five minutes — a partner can legally answer `Retry-After: 999999` and RFC 9110
    puts no ceiling on it.
    """
    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client, departure_time=NOW + timedelta(hours=3))
        partner_delivery.deliver_pending(
            now=NOW,
            sender=_sender(ok=False, status=429, retryable=True, retry_after_s=120.0, error="http_429"),
        )
        stored = client.doc(partner_store.DELIVERIES, delivery["delivery_id"])
        assert stored["state"] == "pending"
        assert stored["retry_not_before"] == NOW + timedelta(seconds=120)

        docs = dict(client.docs)
        _uninstall()
        client = _install()
        client.docs.clear()
        client.docs.update(docs)
        _sent.clear()
        report = partner_delivery.deliver_pending(now=NOW + timedelta(seconds=60), sender=_sender())
        assert report.ready == 0 and not _sent, "still deferred after a restart"
        report = partner_delivery.deliver_pending(now=NOW + timedelta(seconds=121), sender=_sender())
        assert report.sent == 1, "and attempted once the backoff has passed"
    finally:
        _uninstall()


def test_the_retry_after_cap_never_outlives_the_departure():
    """A backoff longer than the time to departure would defer the event past its own validity."""
    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client, departure_time=NOW + timedelta(minutes=11))
        partner_delivery.deliver_pending(
            now=NOW,
            sender=_sender(ok=False, status=429, retryable=True, retry_after_s=299.0, error="http_429"),
        )
        stored = client.doc(partner_store.DELIVERIES, delivery["delivery_id"])
        assert stored["retry_not_before"] <= delivery["valid_until"]
    finally:
        _uninstall()


# ---- Store failures, and what is NOT written ---------------------------------------------------


def test_a_store_failure_writes_nothing_and_re_offers_the_delivery():
    """A store outage costs a cycle, never a subscription.

    Including the case a natural implementation misses: an exhausted transaction surfaces as a plain
    `ValueError` after five attempts, not a Google API error, so a handler keyed on
    `google.api_core` exceptions would call it terminal, mark the subject failed, and never deliver
    it. `partner_store.is_retryable` is what this path asks.
    """
    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client)
        real = partner_store.claim_delivery
        prefix = partner_store._exhausted_transaction_prefix()

        def exhausted(*_a: Any, **_k: Any):
            raise ValueError(f"{prefix} 5 attempts.")

        partner_store.claim_delivery = exhausted  # type: ignore[assignment]
        mark = len(_SINK.records)
        try:
            handler = _capture()
            report = partner_delivery.deliver_pending(now=NOW, sender=_sender())
        finally:
            partner_store.claim_delivery = real  # type: ignore[assignment]
        # `not _sent` is the assertion a straggler from the previous test used to break: the
        # slow-store test abandons four workers, its `finally` releases the hang, and they dialled
        # the stub in here. `_drain_pools` is what makes this deterministic.
        assert report.store_errors == 1 and not _sent
        [record] = [r for r in _SINK.records[mark:] if r.exc_info]
        assert isinstance(record.exc_info[1], ValueError), "an exhausted transaction, not an API error"
        assert client.doc(partner_store.DELIVERIES, delivery["delivery_id"])["state"] == "pending"
        assert [f["step"] for f in _fields(handler, "partner_delivery")] == ["claim"]

        assert partner_delivery.deliver_pending(now=NOW + timedelta(minutes=1), sender=_sender()).sent == 1
    finally:
        _uninstall()


def test_an_unreadable_pending_set_attempts_nothing_and_raises_nothing():
    """R20: reading partner state is guarded, and a failure suppresses that step only."""
    client = _install()
    try:
        _live(client)
        real = partner_store.pending_deliveries

        def failing(*_a: Any, **_k: Any):
            raise RuntimeError("Firestore is unreachable")

        partner_store.pending_deliveries = failing  # type: ignore[assignment]
        mark = len(_SINK.records)
        try:
            report = partner_delivery.deliver_pending(now=NOW, sender=_sender())
        finally:
            partner_store.pending_deliveries = real  # type: ignore[assignment]
        assert report.store_errors == 1 and report.ready == 0 and not _sent
        # The fault is ASSERTED, not printed. It is logged with `exc_info=True` on purpose, so
        # without a handler it reached `lastResort` and put a traceback on stderr on every run of
        # a passing suite.
        [record] = [r for r in _SINK.records[mark:] if r.exc_info]
        assert isinstance(record.exc_info[1], RuntimeError)
        assert [f["result"] for f in _sink_since(mark, "partner_delivery")] == ["pending_error"]
    finally:
        _uninstall()


def test_an_unconfigured_signing_root_leaves_the_event_pending():
    """A missing environment variable is not a delivery outcome and must not be recorded as one.

    Every destination fails together on it, so marking each event `failed` would burn a
    subscription's attempts on a provisioning gap and lose the events when it was fixed.
    """
    import os

    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client)
        os.environ.pop(webhook_signing.SIGNING_ROOT_ENV, None)
        report = partner_delivery.deliver_pending(now=NOW, sender=_sender())
        assert not _sent and report.store_errors == 1
        assert client.doc(partner_store.DELIVERIES, delivery["delivery_id"])["state"] == "pending"
        assert partner_auth.destination_for_delivery(DEST).paused is False, (
            "an unsigned attempt is our fault; pausing is the partner telling us to stop"
        )
    finally:
        _uninstall()


# ---- R16 and the module's own shape -----------------------------------------------------------


def test_no_callback_url_partner_reference_or_prose_reaches_a_log_line():
    """R16 on this module's records. A callback URL is a partner's infrastructure, not ours to log."""
    client = _install()
    try:
        _live(client)
        _pending(client)
        handler = _capture()
        partner_delivery.deliver_pending(
            now=NOW, sender=_sender(ok=False, status=500, retryable=True, error="http_500")
        )
        records = handler.records
        logging.getLogger("partner_delivery").removeHandler(handler)
        rendered = " ".join(
            [r.getMessage() for r in records] + [str(getattr(r, "fields", "")) for r in records]
        )
        for forbidden in ("receiver.example.com", "commuter-42", "Oslo S", "Innstilt", "NSR:StopPlace"):
            assert forbidden not in rendered, f"{forbidden!r} reached a log line: {rendered}"
    finally:
        _uninstall()


def test_the_pool_is_four_wide_and_the_claim_happens_inside_the_worker():
    """KTD6's two structural rules about the pool, read off the source.

    Four is `/meldinger`'s width and the repo's narrowest, which is the right precedent for
    blocking HTTP on a path the poller shares — `varsel`'s own pools are 16/16/32 and run against
    endpoints Entur or a push service owns.

    And the claim must be taken INSIDE the worker as its task starts, never at submit time:
    submitting is what queues the work, so a claim there would admit every task before the deadline
    was consulted and the bound would guarantee nothing. That is `/meldinger`'s recorded lesson.
    """
    assert partner_delivery._WORKERS == 4
    body = inspect.getsource(partner_delivery._attempt)
    assert "budget.claim()" in body, "the claim is in the worker's own body"
    submit = inspect.getsource(partner_delivery.deliver_pending)
    assert "budget.claim" not in submit, "and never at submit time"
    assert "shutdown(wait=False)" in submit, (
        "the pool is abandoned, not joined: `wait=True` would block the poller until whatever is "
        "hung finally returned and defeat the bounded wait completely"
    )
    # Over the CODE, not the raw text: the module docstring names `varsel._state_lock` in order to
    # say it never takes it, so a grep over the whole file matches the explanation and passes (or
    # fails) for the wrong reason. Same rule `test_partner_store` applies to its Terraform greps.
    assert "_state_lock" not in _code(), (
        "no Firestore call under varsel's state lock -- this module never takes it at all"
    )


def test_the_pending_set_is_taken_from_the_store_and_never_re_sorted():
    """`pending_deliveries` already separates ready/stale/throttled and orders ready by validity.

    Re-sorting or re-deriving it here would be a second definition of what is pending, and the one
    that would win is whichever ran last. The `stale` set is resolved rather than dropped, because a
    pending document nothing ever resolves is a document the query keeps returning for its whole
    seven-day retention.
    """
    body = inspect.getsource(partner_delivery.deliver_pending)
    assert "pending.ready" in body and ".sort(" not in body and "sorted(" not in body
    assert "mark_delivery_expired" in inspect.getsource(partner_delivery._expire_stale)


def test_the_module_writes_nothing_to_the_decision_log():
    """KTD10, at the import boundary: partner state is Firestore's, never `varsel_decisions`.

    /historikk and /meldinger publish that table on public hosts — with resolved origin and
    destination names since 2026-08-22 — and partner-app travellers are members of the public. Its
    `pushed` column is also journey-DEFINITION grain and cannot tell a webhook from a Web Push, so a
    partner delivery recorded there would corrupt both pages' meaning as well as their privacy
    claim. A module that imports neither `decisions` nor `varsel` cannot do it however it drifts.
    """
    for forbidden in ("decisions", "varsel", "main", "historikk", "meldinger"):
        assert f"import {forbidden}" not in MODULE_SOURCE, forbidden
    assert "pushed_keys" not in MODULE_SOURCE


def test_the_wire_envelope_is_exactly_the_published_schemas_properties():
    """`additionalProperties: false`, so one stray field costs every event to every partner.

    The stored event carries three names the envelope does not: `partner_id` and `destination_id`
    are our own routing, recorded so a delivery can be traced back to the destination that
    authorised it, and the TTL field is a retention decision of ours. A receiver running the
    verifier U7 ships REFUSES a body carrying any of them, and it would refuse EVERY event rather
    than an unlucky one — the fields are on every mint. Nothing in this repo executes the schema
    against a real body, so this is the only place the bytes we sign are checked against the
    contract we publish.

    Pinned in BOTH directions deliberately. A field here the schema lacks is refused at the
    receiver; a schema property missing here is silently dropped from the body, and for one of the
    eighteen REQUIRED names that is the same refusal reached by the opposite route.
    """
    schema = json.loads(
        (pathlib.Path(partner_delivery.__file__).parents[2] / SCHEMA_PATH).read_text()
    )
    assert schema["additionalProperties"] is False, (
        "the allow-list projection exists BECAUSE the envelope is closed; if this is ever relaxed, "
        "re-read `_WIRE_FIELDS`' first reason before trusting it"
    )
    properties = set(schema["properties"])
    fields = partner_delivery._WIRE_FIELDS
    assert len(set(fields)) == len(fields), "a duplicate would hide a missing name"
    assert set(fields) == properties, (
        "the wire envelope and the published schema must name the same fields: "
        f"ours only {sorted(set(fields) - properties)}, schema only {sorted(properties - set(fields))}"
    )
    assert set(schema["required"]) <= set(fields)

    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client)
        stored = client.doc(partner_store.EVENTS, delivery["event_id"])
        for ours in ("partner_id", "destination_id", partner_store.TTL_FIELD):
            assert ours in stored, (
                f"{ours} is expected to be STORED — the test is worthless if the store stops "
                "writing the very fields the projection exists to withhold"
            )
        wire = partner_delivery._wire_event(stored)
        assert set(wire) <= properties, sorted(set(wire) - properties)
        assert set(schema["required"]) <= set(wire), sorted(set(schema["required"]) - set(wire))

        # And through the real path, on the bytes that are actually signed and sent.
        partner_delivery.deliver_pending(now=NOW, sender=_sender())
        [attempt] = _sent
        sent = json.loads(attempt["body"].decode())
        assert set(sent) <= properties, sorted(set(sent) - properties)
        assert set(schema["required"]) <= set(sent)
    finally:
        _uninstall()


def test_a_late_arrival_is_not_counted_as_a_fresh_delivery():
    """The abandoned-thread race, from this side: the write is refused and the COUNT must follow.

    `partner_store.mark_delivery_sent` now refuses a write to a delivery that is no longer
    `pending` and answers `already_resolved` with the state it actually holds, so R11's cap is no
    longer double-spent. That leaves the report as the thing that is wrong: `_attempt` returned
    "sent" whenever the write did not raise, so an attempt thread the previous cycle abandoned would
    report a second delivery of one notification. `partner_delivery_round` is published as
    operational telemetry, so `sent` has to mean a notification the partner did not already have.

    The state is flipped INSIDE the sender, which is where the race actually sits: a delivery that
    is already resolved when the pass starts is never offered by `pending_deliveries` at all, so
    setting it up front would test nothing. Here the record is pending when it is read and claimed,
    and another pass resolves it while this attempt is on the wire -- exactly the window an
    abandoned thread returns into.

    Both resolved cases, because they mean different things to a reader: an already-`sent` record is
    a duplicate the receiver's inbox drops, while a `superseded` one was replaced by a newer
    revision that spends its own slot.
    """
    client = _install()
    try:
        _live(client)
        [delivery] = _pending(client)
        path = f"{partner_store.DELIVERIES}/{delivery['delivery_id']}"

        def resolving(state: str):
            """A sender that succeeds, and while it is on the wire another pass resolves the row."""
            inner = _sender()

            def send(url, **kwargs):
                result = inner(url, **kwargs)
                client.docs[path]["state"] = state
                return result

            return send

        report = partner_delivery.deliver_pending(now=NOW, sender=resolving("sent"))
        assert report.sent == 0, "a late arrival is not a fresh delivery"
        assert report.already_resolved == 1 and report.superseded == 0, report
        assert len(_sent) == 1, "the HTTP call did happen; it is the ACCOUNTING that changed"
        subscription = client.doc(partner_store.SUBSCRIPTIONS, SID)
        assert subscription.get("daily_deliveries", {}) == {}, "and no slot was spent"
        assert client.doc(partner_store.DELIVERIES, delivery["delivery_id"])["attempts"] == 0, (
            "and NOTHING was written: the refused write leaves `attempts` alone too, which is the "
            "cosmetic half of the same double-count"
        )

        # A newer revision replaced it instead: still not a send, and counted apart because that
        # replacement spends R11's slot itself.
        client.docs[path]["state"] = "pending"
        client.docs[path]["retry_not_before"] = NOW
        third = partner_delivery.deliver_pending(now=NOW, sender=resolving("superseded"))
        assert third.superseded == 1 and third.sent == 0 and third.already_resolved == 0, third

        # And the guard must not turn an ordinary send into a late arrival.
        client.docs[path]["state"] = "pending"
        client.docs[path]["retry_not_before"] = NOW
        ordinary = partner_delivery.deliver_pending(now=NOW, sender=_sender())
        assert ordinary.sent == 1 and ordinary.already_resolved == 0 and ordinary.superseded == 0
    finally:
        _uninstall()


if __name__ == "__main__":
    _ran = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            _ran += 1
            print(f"ok  {name}")
    # The COUNT, in every other suite's shape: a runner that silently collects nothing still exits
    # 0, so "it passed" is not the check -- "the number moved" is.
    print(f"\n{_ran} partner delivery checks passed.")
