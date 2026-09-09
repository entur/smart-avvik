"""Checks for notification_events.py: the alert lifecycle, its ordering and its two bounds.

Hermetic. The Firestore fake below runs ``notification_store``'s REAL transaction bodies and applies
their writes, so every assertion here is about the committed documents rather than about a mock's
call log — which is the only way to check a revision, a cap exemption or a superseded delivery at
all.

**The fake is a third copy**, after ``test_notification_store``'s and ``test_notification_api``'s, and that is
this directory's convention rather than an oversight: CI runs every suite as its own process
(`python test_x.py`, never pytest), so a shared helper module here would be a new import-time
surface for all thirty of them, and a shared VAPID/pepper environment stub is exactly the leak that
convention exists to prevent. Each copy is trimmed to the queries its own module runs.

What is deliberately NOT asserted here: atomicity. The bodies converge on one committed outcome
under this fake's single-threaded application of their writes, which is the same code path
production runs, and that is a statement about shape and idempotency rather than about contention.
``test_notification_store`` says the same thing about the emulator.
"""

from __future__ import annotations

import inspect
import logging
import pathlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import notification_events
import notification_store

NOW = datetime(2026, 9, 8, 6, 30, tzinfo=UTC)
SID = "sub-1"
CLIENT = "client-a"
DEST = "dest-a"
MODULE_SOURCE = pathlib.Path(notification_events.__file__).read_text()


# ---- A fake client that runs the real bodies ---------------------------------------------------


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
        # `timeout` is accepted and ignored: every read in `notification_store` now carries its own
        # Firestore deadline (KTD6 -- an abandoned worker thread with no call deadline delays
        # pod termination via `concurrent.futures`' atexit join). `test_notification_store` is where
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
        self.transactions = 0
        self.queries: list[str] = []
        self.fail_with: BaseException | None = None
        self.delay_s = 0.0
        # Keyed by spec NAME, because a blanket query failure would also break
        # `_count_destination_deliveries` -- which runs inside `commit_event` -- and the point of
        # the pre-read tests is that the emit still commits when only the pre-read failed.
        self.query_fail_with: dict[str, BaseException] = {}

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)

    def put(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self.docs[f"{collection}/{doc_id}"] = dict(data)

    def doc(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        stored = self.docs.get(f"{collection}/{doc_id}")
        return dict(stored) if stored is not None else None

    def in_collection(self, collection: str) -> list[dict[str, Any]]:
        return [
            dict(data)
            for path, data in self.docs.items()
            if path.split("/", 1)[0] == collection
        ]


_saved: dict[str, Any] = {}
_pools: list[ThreadPoolExecutor] = []
_DRAIN_TIMEOUT_S = 5.0


class _RecordingPool(ThreadPoolExecutor):
    """Every pool `emit` creates, kept so a test can drain the workers it ABANDONED.

    `emit` calls `shutdown(wait=False)` on purpose -- a thread parked inside a Firestore call
    cannot be killed, and joining it would defeat the bounded wait entirely. Right in production,
    and in a suite it means the hung-store test's worker runs on into whatever executes next,
    writing through a fake that has already been uninstalled. `test_webhook_delivery` had the same
    shape and it cost one failing run in nine there; this one had not surfaced yet, which is the
    only difference between them.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _pools.append(self)


def _drain_pools() -> None:
    """Join every worker the step walked away from, so none of them outlives its test."""
    stragglers: list[str] = []
    for pool in _pools:
        # `_threads` is private; a test double is where reaching for it is honest, since the public
        # `shutdown(wait=True)` takes no timeout and a wedged worker would hang the suite rather
        # than report a leak.
        for thread in list(getattr(pool, "_threads", ())):
            thread.join(timeout=_DRAIN_TIMEOUT_S)
            if thread.is_alive():
                stragglers.append(thread.name)
    _pools.clear()
    if stragglers:
        raise AssertionError(f"worker thread(s) outlived the test that abandoned them: {stragglers}")


def _sortable(value: Any) -> Any:
    return value if isinstance(value, datetime) else datetime.min.replace(tzinfo=UTC)


def _install() -> _Fake:
    client = _Fake()
    _saved.update(
        {
            "client": notification_store._firestore_client,
            "run": notification_store._run_transaction,
            "query": notification_store._query,
            "pool": notification_events.ThreadPoolExecutor,
        }
    )
    notification_events.ThreadPoolExecutor = _RecordingPool  # type: ignore[misc]
    _pools.clear()
    notification_store._firestore_client = lambda: client  # type: ignore[assignment]

    def _run(body: Any) -> Any:
        if client.delay_s:
            time.sleep(client.delay_s)
        if client.fail_with is not None:
            raise client.fail_with
        client.transactions += 1
        txn = _Txn()
        result = body(txn)
        for kind, path, data in txn.writes:
            if kind == "set":
                client.docs[path] = dict(data or {})
            elif kind == "update":
                client.docs.setdefault(path, {}).update(data or {})
            else:
                client.docs.pop(path, None)
        return result

    def _query(
        name: str,
        *,
        equality: Any = None,
        inequality: Any = None,
        limit: int,
        # Accepted and unused: only the listing route pages, and no test here reaches it.
        start_after: Any = None,
    ) -> _Page:
        if name in client.query_fail_with:
            raise client.query_fail_with[name]
        client.queries.append(name)
        spec = notification_store._SPECS_BY_NAME[name]
        rows: list[tuple[str, dict[str, Any]]] = []
        for path, data in client.docs.items():
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

    notification_store._run_transaction = _run  # type: ignore[assignment]
    notification_store._query = _query  # type: ignore[assignment]
    notification_store.configure(client=client, clock=lambda: NOW)
    return client


def _uninstall() -> None:
    _drain_pools()
    if _saved:
        notification_events.ThreadPoolExecutor = _saved["pool"]  # type: ignore[misc]
        notification_store._firestore_client = _saved["client"]  # type: ignore[assignment]
        notification_store._run_transaction = _saved["run"]  # type: ignore[assignment]
        notification_store._query = _saved["query"]  # type: ignore[assignment]
        _saved.clear()
    notification_store.reset_for_tests()


def _spec(**over: Any) -> notification_store.SubscriptionSpec:
    base: dict[str, Any] = {
        "subscription_id": SID,
        "client_id": CLIENT,
        "destination_id": DEST,
        "from_stop": "NSR:StopPlace:337",
        "to_stop": "NSR:StopPlace:418",
        "begin": "06:00",
        "end": "09:00",
        "days": (0, 1, 2, 3, 4, 5, 6),
        "external_ref": "commuter-42",
        "test": False,
    }
    base.update(over)
    return notification_store.SubscriptionSpec(**base)


def _live(client: _Fake, **over: Any) -> None:
    """A stored subscription written by the REAL writer, never a hand-made dict.

    A fixture that does not come through `_new_subscription_doc` hides exactly the class of defect
    these checks exist to catch — the /rushhistorikk render stub modelled a non-pushing baseline as
    null where the code wrote "green", and the marker bug it hid shipped.
    """
    spec = _spec(**over)
    client.put(
        notification_store.SUBSCRIPTIONS, spec.subscription_id, notification_store._new_subscription_doc(spec, NOW)
    )


def _subject(**over: Any) -> notification_events.WebhookSubject:
    base: dict[str, Any] = {
        "subscription_id": SID,
        "client_id": CLIENT,
        "destination_id": DEST,
        "subscription_version": 1,
        "external_ref": "commuter-42",
        "test": False,
        "alertworthy": True,
        "cancelled": False,
        "situations": ("ENT:SituationNumber:1",),
        "operator": "VYG",
        "line": "VYG:Line:L1",
        "journey_ref": "DatedServiceJourney:1234",
        "stop_place": "NSR:StopPlace:337",
        "stop_order": 3,
        "departure_time": NOW + timedelta(minutes=20),
        "title": "Tog L1 08:50 kan bli innstilt",
        "body": "Oslo S → Ski\nSignalfeil",
        "payload": {"notification": {"title": "t", "body": "b"}},
    }
    base.update(over)
    return notification_events.WebhookSubject(**base)


# See `test_webhook_delivery`'s copy for the full argument: every fault this suite injects is
# logged by the code under test with `exc_info=True`, and with no handler in the hierarchy
# `logging.lastResort` printed the record and its traceback to stderr -- so a passing run ended in
# a stack trace, and a standing traceback is how a real one stops being read. A collecting sink
# rather than a `NullHandler`, so a fault can be ASSERTED rather than merely hidden.
class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


_SINK = _Records()
for _name in ("notification_events", "notification_store"):
    _sink_logger = logging.getLogger(_name)
    _sink_logger.setLevel(logging.INFO)
    _sink_logger.addHandler(_SINK)


def _capture() -> _Records:
    handler = _Records()
    logger = logging.getLogger("notification_events")
    # The level matters: the root logger defaults to WARNING, so without this the one INFO summary
    # per cycle -- the record an operator reads the cycle's counts off -- is dropped before any
    # handler sees it, and an assertion about it fails for a reason having nothing to do with the
    # code under test.
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    return handler


def _reattach(records: list[logging.LogRecord]) -> _Records:
    """Wrap already-captured records so `_events` can be reused after the handler came off."""
    handler = _Records()
    handler.records = records
    return handler


def _events(handler: _Records, name: str) -> list[dict]:
    out = []
    for record in handler.records:
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict) and fields.get("event") == name:
            out.append(fields)
    logging.getLogger("notification_events").removeHandler(handler)
    return out


# ---- Scenario 1 / AE2 + AE4: one train, one subject, and the confirmation ----------------------


def test_an_alertworthy_train_mints_one_event_and_the_next_cycle_mints_nothing():
    """Scenario 1's first half, and the whole point of reading the store rather than re-deriving.

    A re-derivation would mint a fresh revision — and therefore a fresh `event_id` — on every one of
    the ~300 cycles in a day, which the receiver cannot recognise as a duplicate and would turn into
    a second notification about the same train.
    """
    client = _install()
    try:
        _live(client)
        first = notification_events.emit([_subject()], now=NOW)
        events = client.in_collection(notification_store.EVENTS)
        assert first.created == 1 and len(events) == 1
        assert events[0]["type"] == notification_store.EVENT_CREATED
        assert events[0]["phase"] == notification_store.PHASE_PREDICTED
        event_id, revision = events[0]["event_id"], events[0]["revision"]

        # A LATER cycle, with a different probability in the payload: the facts moved, the lifecycle
        # did not. Nothing is minted, at any size of change -- there is no materiality threshold.
        second = notification_events.emit(
            [_subject(payload={"probabilities": {"impact": 0.99}})], now=NOW + timedelta(minutes=5)
        )
        assert second.created == 0 and second.unchanged == 1
        events = client.in_collection(notification_store.EVENTS)
        assert len(events) == 1 and events[0]["event_id"] == event_id
        assert events[0]["revision"] == revision
    finally:
        _uninstall()


def test_a_confirmed_cancellation_mints_an_updated_revision_and_supersedes_the_forecast():
    """AE4: ET declares a cancellation for a train an earlier event forecast.

    Three things in one, because they are one transaction: the same `alert_id`, a strictly greater
    revision, and `type = journey.alert.updated` with `phase = confirmed`.

    The cap exemption is asserted here too, and it is exempt only because the forecast was
    DELIVERED first. `cap_exempt` is decided at MINT and reads whether a revision about this train
    has already reached the traveller — see the next test for the other side of that, which is also
    where the supersede rule is checked: a delivery already `sent` is left exactly as it is (there
    is nothing to withdraw from a phone), while a still-PENDING one must be superseded or a
    predicted and a confirmed about one train both go out.
    """
    client = _install()
    try:
        _live(client)
        notification_events.emit([_subject()], now=NOW)
        first = client.in_collection(notification_store.EVENTS)[0]
        notification_store.mark_delivery_sent(first["event_id"], now=NOW + timedelta(seconds=5))

        notification_events.emit([_subject(cancelled=True)], now=NOW + timedelta(minutes=1))
        events = sorted(client.in_collection(notification_store.EVENTS), key=lambda e: e["revision"])
        assert len(events) == 2
        confirmed = events[-1]
        assert confirmed["alert_id"] == first["alert_id"], "the same subject, per (subscription, train)"
        assert confirmed["revision"] > first["revision"]
        assert confirmed["type"] == notification_store.EVENT_UPDATED
        assert confirmed["phase"] == notification_store.PHASE_CONFIRMED
        assert client.doc(notification_store.DELIVERIES, first["event_id"])["state"] == "sent", (
            "a delivery already on a phone is not superseded: there is nothing to withdraw"
        )
        assert client.doc(notification_store.DELIVERIES, confirmed["event_id"])["cap_exempt"] is True
    finally:
        _uninstall()


def test_a_confirmation_whose_forecast_was_never_delivered_is_not_cap_exempt():
    """The half of R11's exemption that is easy to get backwards, and the store's own rule.

    `cap_exempt` reads `delivered_revision`, not "is this a confirmation": a `confirmed` minted
    before its `predicted` ever reached the traveller is the FIRST notification about that train and
    must spend a slot. Exempting it would let a subscription exceed four notifications a day by
    having its forecasts fail to deliver, which is the opposite of what the exemption is for.

    And the supersede rule's other side: the forecast's delivery is still PENDING here, so it must be
    marked `superseded`. Without that a predicted and a confirmed about the same train both go out,
    and the receiver's revision rule saves the webhook-only if they happen to arrive in order.
    """
    client = _install()
    try:
        _live(client)
        notification_events.emit([_subject()], now=NOW)
        first = client.in_collection(notification_store.EVENTS)[0]
        notification_events.emit([_subject(cancelled=True)], now=NOW + timedelta(minutes=1))
        events = sorted(client.in_collection(notification_store.EVENTS), key=lambda e: e["revision"])
        assert client.doc(notification_store.DELIVERIES, events[-1]["event_id"])["cap_exempt"] is False
        assert client.doc(notification_store.DELIVERIES, first["event_id"])["state"] == "superseded"
    finally:
        _uninstall()


def test_thirteen_departures_produce_thirteen_events_soonest_first():
    """Scenario 2's emit half / AE3: a line suspension inside one subscription's window.

    Thirteen cancelled departures are thirteen SUBJECTS — the grain is (subscription, train) — so
    thirteen events, each with its own `alert_id`. The cap is not applied here at all: it is spent
    when a delivery SUCCEEDS (`test_webhook_delivery`), because a dead destination must not burn a
    subscription's four for the Oslo day with nothing delivered.

    The ordering assertion is about the EMIT queue, which is soonest-departure-first exactly as
    /varsel spends its own budget: when the budget binds it must be spent on the trains a traveller
    can still act on.
    """
    client = _install()
    try:
        _live(client)
        subjects = [
            _subject(
                journey_ref=f"DatedServiceJourney:{2000 + i}",
                departure_time=NOW + timedelta(minutes=30 * (13 - i)),
                cancelled=True,
            )
            for i in range(13)
        ]
        report = notification_events.emit(subjects, now=NOW)
        assert report.created == 13
        events = client.in_collection(notification_store.EVENTS)
        assert len({e["alert_id"] for e in events}) == 13
        # Minted in departure order, which the revision (clamped to `stored + 1` per subject, seeded
        # from one instant) cannot show -- so read the order off the queue itself.
        order = [s.journey_ref for s in sorted(subjects, key=notification_events._sort_key)]
        assert order == [f"DatedServiceJourney:{2000 + i}" for i in range(12, -1, -1)]
    finally:
        _uninstall()


# ---- Scenario 7: withdrawal, noise, and a restart ---------------------------------------------


def test_two_below_threshold_cycles_withdraw_and_one_does_not():
    """Scenario 7: two FRESH below-threshold cycles produce one `withdrawn`; one produces none.

    A single cycle's dip below the gate is noise, and a withdrawal is itself a notification. The
    consecutive count lives on the alert subject — Firestore, not this process — which is why the
    third emit below is preceded by a full re-install: a fresh process must reach the withdrawal on
    the same cycle a long-running one would.
    """
    client = _install()
    try:
        _live(client)
        notification_events.emit([_subject()], now=NOW)
        one = notification_events.emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=1))
        assert one.counting == 1 and one.withdrawn == 0, "one dip is noise"
        assert len(client.in_collection(notification_store.EVENTS)) == 1

        # A RESTART: the module holds nothing, the store holds the count.
        docs = dict(client.docs)
        _uninstall()
        client = _install()
        client.docs.update(docs)
        two = notification_events.emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=2))
        assert two.withdrawn == 1, "the counter survived the restart"
        events = sorted(client.in_collection(notification_store.EVENTS), key=lambda e: e["revision"])
        assert events[-1]["type"] == notification_store.EVENT_WITHDRAWN
        assert events[-1]["phase"] == notification_store.PHASE_WITHDRAWN
    finally:
        _uninstall()


def test_a_recovered_cycle_breaks_the_run_so_a_later_dip_does_not_withdraw():
    """Scenario 7's exact half: the count is CONSECUTIVE, not "two dips inside fifteen minutes".

    The store cannot see this on its own. An alert-worthy cycle that mints nothing reaches no store
    call at all — `skip_reason` skips it to save a transaction per standing subject per cycle — so
    `_withdraw_body` closes runs by TIME, and dip / recover / dip inside `WITHDRAWAL_GAP_S` still
    withdrew. That is not cosmetic: a wrong withdrawal is a real «varselet er trukket tilbake», it
    is cap-exempt so nothing suppresses it, and the next cycle re-alerts — notification flapping.

    So the recovery cycle now closes the run from here, where the information is, and the second dip
    is a FIRST dip again. All three cycles are inside the gap, which is what makes this the case the
    time window gets wrong.
    """
    client = _install()
    try:
        _live(client)
        notification_events.emit([_subject()], now=NOW)
        dip = notification_events.emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=1))
        assert dip.counting == 1, dip

        was = client.in_collection(notification_store.SUBJECTS)[0]
        recovered = notification_events.emit([_subject()], now=NOW + timedelta(minutes=2))
        assert recovered.unchanged == 1, "the same phase again: nothing is minted"
        assert recovered.run_cleared == 1, f"and the open run is closed; got {recovered}"
        subject = client.in_collection(notification_store.SUBJECTS)[0]
        assert subject["below_threshold_cycles"] == 0 and subject["below_threshold_at"] is None
        # A recovery is not a lifecycle transition: it must move neither, or it would mint an event
        # and put a second notification about one train on a traveller's phone.
        assert subject["phase"] == was["phase"] == notification_store.PHASE_PREDICTED
        assert subject["revision"] == was["revision"]

        # Well inside `WITHDRAWAL_GAP_S` of the first dip, so the time window alone would withdraw.
        assert timedelta(minutes=3).total_seconds() < notification_store.WITHDRAWAL_GAP_S
        second = notification_events.emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=3))
        assert second.withdrawn == 0 and second.counting == 1, (
            f"the recovery made this a FIRST dip, not the second of a run; got {second}"
        )
        assert len(client.in_collection(notification_store.EVENTS)) == 1, "no withdrawal was minted"
    finally:
        _uninstall()


def test_an_ordinary_standing_cycle_still_writes_nothing():
    """The cost contract: the reset must not undo the optimisation it was built on top of.

    A standing alert-worthy subject with NO open run is the overwhelming majority of cycles, and it
    must still reach no store call whatever — `open_runs` is consulted first and is empty for it.
    The measurement this protects is the 200-subject emit at ~12 ms, which a write per standing
    subject per cycle would turn into a transaction per subject.
    """
    client = _install()
    try:
        _live(client)
        notification_events.emit([_subject()], now=NOW)
        before = client.transactions
        report = notification_events.emit([_subject()], now=NOW + timedelta(minutes=1))
        assert report.unchanged == 1 and client.transactions == before, (
            f"an ordinary standing cycle writes nothing; "
            f"{client.transactions - before} transaction(s) happened"
        )
        assert report.run_cleared == 0 and report.run_stale == 0
    finally:
        _uninstall()


def test_a_run_already_past_the_gap_is_left_for_the_store_to_reset():
    """A write that would buy nothing is not made: the store starts a fresh run for it anyway.

    This is why `open_below_threshold` is a mapping rather than a set — the timestamp is what lets
    the caller tell an open run from a stale one without a read.
    """
    client = _install()
    try:
        _live(client)
        notification_events.emit([_subject()], now=NOW)
        notification_events.emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=1))
        later = NOW + timedelta(seconds=notification_store.WITHDRAWAL_GAP_S + 120)
        before = client.transactions
        report = notification_events.emit([_subject()], now=later)
        assert report.run_stale == 1 and report.run_cleared == 0, report
        assert client.transactions == before, "and it costs no transaction"
    finally:
        _uninstall()


def test_a_failed_pre_read_loses_the_reset_and_never_reads_per_subject():
    """R20, applied to the reset: an outage costs the optimisation's exactness, never a read.

    A failed `client_alerts` leaves `open_runs` empty, so no run is closed this cycle and the
    store's time window is what protects the traveller. What must NOT happen is a per-subject read
    to recover the information — that is the cost the whole pre-read exists to avoid.
    """
    client = _install()
    try:
        _live(client)
        notification_events.emit([_subject()], now=NOW)
        notification_events.emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=1))
        client.query_fail_with["client_alerts"] = RuntimeError("Firestore is unreachable")
        mark = len(_SINK.records)
        report = notification_events.emit([_subject()], now=NOW + timedelta(minutes=2))
        assert report.run_cleared == 0 and report.run_stale == 0, report
        assert report.unchanged == 1, "the store still answered the subject itself"
        assert not any("get_subject" in str(r.msg) for r in _SINK.records[mark:])
        # The run is still open, so the store's time window governs — the documented fallback.
        subject = client.in_collection(notification_store.SUBJECTS)[0]
        assert subject["below_threshold_at"] is not None
    finally:
        client.query_fail_with.pop("client_alerts", None)
        _uninstall()


def test_a_below_threshold_train_with_nothing_standing_costs_no_transaction():
    """The pre-read's whole purpose, and the cheap half of scenario 7.

    ~83 % of matched alerts are sub-threshold. A `record_below_threshold` for a train nobody was
    ever warned about writes nothing (`nothing_standing`), so paying a Firestore transaction to
    discover that on every cycle for every such train is the cost the one client-scoped query
    exists to avoid.
    """
    client = _install()
    try:
        _live(client)
        before = client.transactions
        report = notification_events.emit([_subject(alertworthy=False)], now=NOW)
        assert report.nothing_standing == 1
        assert client.transactions == before, "no transaction was spent"
        assert client.queries.count("client_alerts") == 1, "one query for the whole cycle"
    finally:
        _uninstall()


def test_an_unchanged_alertworthy_train_costs_no_transaction_either():
    """The other half of the pre-read: `_transition` would return None, so nothing is asked."""
    client = _install()
    try:
        _live(client)
        notification_events.emit([_subject()], now=NOW)
        before = client.transactions
        report = notification_events.emit([_subject()], now=NOW + timedelta(minutes=1))
        assert report.unchanged == 1 and client.transactions == before
    finally:
        _uninstall()


def test_a_truncated_or_over_full_pre_read_falls_back_to_asking_the_store():
    """The pre-read's one honest limit, handled rather than assumed.

    `client_alerts` COUNTS every live subject and enumerates at most `MAX_ALERTS_SERVED` of them,
    so a subscription holding more than that reports a count this module cannot resolve to phases.
    Treating the subset as complete would skip a withdrawal that was owed and — worse — skip a
    `confirmed` mint for a train ET has cancelled, silently, for exactly the subscriptions a line
    suspension has hit hardest.
    """
    client = _install()
    try:
        _live(client)
        served = notification_store.MAX_ALERTS_SERVED
        subjects = [
            _subject(journey_ref=f"DatedServiceJourney:{3000 + i}") for i in range(served + 2)
        ]
        notification_events.emit(subjects, now=NOW)
        assert len(client.in_collection(notification_store.SUBJECTS)) == served + 2

        before = client.transactions
        report = notification_events.emit(subjects, now=NOW + timedelta(minutes=1))
        assert report.unchanged == len(subjects), "every one still resolves to `no transition`"
        assert client.transactions > before, (
            "and it cost transactions, because the pre-read could not enumerate them: an "
            "incomplete answer must ask the store rather than guess"
        )
    finally:
        _uninstall()


def test_a_failed_pre_read_costs_transactions_and_never_events():
    """R20: a store outage suppresses the OPTIMISATION, never the correctness."""
    client = _install()
    try:
        _live(client)
        client.query_fail_with["client_alerts"] = RuntimeError("Firestore is unreachable")
        handler = _capture()
        report = notification_events.emit([_subject()], now=NOW)
        assert report.created == 1, "the event was still committed"
        assert any(e["result"] == "standing_error" for e in _events(handler, "notification_emit"))
    finally:
        _uninstall()


# ---- Scenario 10: the revision holds across a DST fall-back and a clock step -------------------


def test_the_revision_survives_the_oslo_dst_fall_back_and_a_clock_step_backwards():
    """Scenario 10, and the two failures it names are different.

    The DST half: at the autumn fall-back, Oslo wall-clock 02:30 happens twice, so a revision
    formatted from the Oslo decision timestamp goes BACKWARDS for an hour and a receiver ordering on
    `revision` discards every event for those subjects — a confirmed cancellation included. The seed
    is UTC, so the two instants below are an hour apart and the seed moves forward.

    The clock-step half: monotonicity cannot rest on the clock at all, because an NTP correction can
    move it backwards by an unbounded amount. So the seed is only a seed and the transaction clamps
    to `max(stored + 1, seed)` — asserted with a second emit at an EARLIER instant than the first.
    """
    oslo = notification_store.OSLO
    # 2026-10-25 03:00 CEST == 2026-10-25 02:00 CET: the repeated hour.
    before = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)  # 02:30 CEST
    after = datetime(2026, 10, 25, 1, 30, tzinfo=UTC)  # 02:30 CET
    assert before.astimezone(oslo).strftime("%H:%M") == after.astimezone(oslo).strftime("%H:%M")
    assert notification_store.revision_seed(after) > notification_store.revision_seed(before), (
        "the seed is UTC: the same Oslo wall-clock time an hour apart must still move forward"
    )

    client = _install()
    try:
        _live(client)
        notification_events.emit([_subject(departure_time=before + timedelta(minutes=20))], now=before)
        first = client.in_collection(notification_store.EVENTS)[0]["revision"]
        # A step BACKWARDS, further than the fall-back: the clamp is what holds now.
        notification_events.emit(
            [_subject(cancelled=True, departure_time=before + timedelta(minutes=20))],
            now=before - timedelta(days=2),
        )
        revisions = sorted(e["revision"] for e in client.in_collection(notification_store.EVENTS))
        assert revisions[-1] == first + 1, f"strictly increasing, got {revisions}"
    finally:
        _uninstall()


def test_the_instant_is_captured_once_outside_the_transaction_and_in_utc():
    """The structural half of scenario 10, read off the source rather than inferred.

    Three separate silent failures ride on this one line. A clock read INSIDE a transaction body
    makes `event_id` irreproducible, and the body "might run more than once" (Firebase's own words),
    so a retry would mint a second event the receiver cannot recognise as the same one. A naive
    local instant makes the seed Oslo-formatted, which the test above covers. And two emits in one
    cycle reading the clock separately would order two subjects by a few microseconds of scheduling
    rather than by departure.
    """
    source = inspect.getsource(notification_events.emit)
    assert "astimezone(UTC)" in source, "the instant is normalised to UTC before anything sees it"
    body = inspect.getsource(notification_events._run)
    assert "datetime.now" not in body, (
        "the emit loop must not read a wall clock: `now` arrives as an argument, captured once"
    )
    assert "datetime.now" not in inspect.getsource(notification_events._emit_one)


# ---- The transition rule is the store's, called and not restated ------------------------------


def test_the_skip_predicate_is_the_stores_own_transition_rule():
    """`skip_reason` may never be LOOSER than `notification_store._transition`, over every phase pair.

    A client-side skip is what turns a cheap cycle into a correct one, and a restatement that
    drifted would fail silently in the worst direction: minting nothing for a train ET has just
    cancelled, or minting a fresh revision on a changed probability. So this asserts the two agree
    across the whole cross-product rather than trusting a comment, and a grep refuses a restatement
    of the table.
    """
    phases = (None, notification_store.PHASE_PREDICTED, notification_store.PHASE_CONFIRMED, notification_store.PHASE_WITHDRAWN)
    for stored in phases:
        for cancelled in (False, True):
            subject = _subject(alertworthy=True, cancelled=cancelled)
            want = notification_store._transition(stored, notification_events.requested_phase(subject))
            got = notification_events.skip_reason(stored, subject)
            assert (got is None) == (want is not None), (stored, cancelled, got, want)
    # A below-threshold observation writes nothing unless something stands.
    for stored, expect in (
        (None, "nothing_standing"),
        (notification_store.PHASE_WITHDRAWN, "nothing_standing"),
        (notification_store.PHASE_PREDICTED, None),
        (notification_store.PHASE_CONFIRMED, None),
    ):
        assert notification_events.skip_reason(stored, _subject(alertworthy=False)) == expect
    assert "_transition(" in MODULE_SOURCE, "called, not restated"
    code = "\n".join(
        line for line in MODULE_SOURCE.splitlines() if not line.strip().startswith("#")
    )
    assert 'PHASE_CONFIRMED and' not in code, (
        "a restated transition table is what this module must not hold: one statement of the rule"
    )


def test_requested_phase_is_confirmed_only_for_a_declared_cancellation():
    assert notification_events.requested_phase(_subject()) == notification_store.PHASE_PREDICTED
    assert (
        notification_events.requested_phase(_subject(cancelled=True)) == notification_store.PHASE_CONFIRMED
    )


# ---- Identity: what cannot be a subject -------------------------------------------------------


def test_an_observation_that_cannot_name_a_train_is_never_a_subject():
    """`alert_id` is derived from six fields and is the only key the receiver orders on.

    Missing any of them, two DIFFERENT trains would share an id, and the receiver's own "drop a
    revision not strictly newer" rule would suppress the second commuter's notification entirely.
    `departure_time` is in that set because `call_date` is derived from it — the operating day the
    whole ET join key rests on.
    """
    for missing in ("operator", "journey_ref", "stop_place", "stop_order", "departure_time"):
        assert notification_events.train_ref(_subject(**{missing: None})) is None, missing
    # A line nobody published is a DISPLAY gap, not an identity one: `journey_ref` already names the
    # dated train, so this must still be a subject.
    ref = notification_events.train_ref(_subject(line=None))
    assert ref is not None and ref.line == ""

    client = _install()
    try:
        _live(client)
        before = client.transactions
        report = notification_events.emit([_subject(stop_order=None)], now=NOW)
        assert report.unidentified == 1 and client.transactions == before
    finally:
        _uninstall()


def test_the_call_date_is_the_oslo_operating_day():
    """The one derived field, and the timezone every call table keys on."""
    ref = notification_events.train_ref(
        _subject(departure_time=datetime(2026, 9, 8, 22, 30, tzinfo=UTC))
    )
    assert ref is not None and ref.call_date == "2026-09-09", "00:30 Oslo is the next day"


# ---- Bounds: the reservation and the bounded wait ---------------------------------------------


def test_the_reservation_stops_new_transactions_before_the_deadline_rather_than_after():
    """KTD6's budget, in `varsel._WriteBudget`'s shape, and the reason it is a RESERVATION.

    `monotonic() >= deadline` bounds when the last transaction STARTS, not when the step ENDS: a
    call admitted with a millisecond to spare still runs its full worst case afterwards, and that
    shape overshot a documented 30 s ceiling by 16 s (`varsel._WriteBudget`'s own note). Here the
    store is made slow enough that the second transaction cannot fit, and the assertion is that the
    step ended inside the deadline it was given.
    """
    client = _install()
    try:
        _live(client)
        client.delay_s = 0.05
        subjects = [
            _subject(journey_ref=f"DatedServiceJourney:{4000 + i}") for i in range(6)
        ]
        started = time.monotonic()
        report = notification_events.emit(
            subjects, now=NOW, deadline_s=0.2, wait_s=2.0, worst_case_s=0.12
        )
        elapsed = time.monotonic() - started
        assert report.capped, f"the budget must bind; got {report}"
        assert report.created + report.capped == len(subjects)
        assert elapsed < 0.5, f"the step must end inside its deadline; took {elapsed:.2f}s"
    finally:
        _uninstall()


def test_a_hung_store_cannot_extend_the_step_past_its_bounded_wait():
    """Scenario 3's slow-store half, on the emit side.

    The reservation cannot see this failure: it reserves a worst case the call then exceeds without
    limit, because `notification_store` plumbs no `timeout=` into the Firestore client. What bounds it is
    the outer wait on a worker thread that is ABANDONED rather than joined — `shutdown(wait=True)`
    would block the poller until the hung call finally returned and defeat the bound completely.
    """
    client = _install()
    release = threading.Event()
    try:
        _live(client)

        def hanging(_body: Any) -> Any:
            release.wait(30)  # bounded so a failing assertion cannot wedge the suite
            return notification_store.EventCommit(status="unchanged", alert_id="x")

        notification_store._run_transaction = hanging  # type: ignore[assignment]
        handler = _capture()
        started = time.monotonic()
        report = notification_events.emit([_subject()], now=NOW, wait_s=0.1)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"a hung store must not hold the poller thread; took {elapsed:.1f}s"
        assert report.timed_out is True
        assert {e["result"] for e in _events(handler, "notification_emit")} == {"timeout"}
    finally:
        release.set()
        _uninstall()


def test_a_destination_at_its_pending_ceiling_is_skipped_for_the_rest_of_the_step():
    """R21's ceiling, and why the skip is PASS-local.

    A destination at its pending-event ceiling refuses every further emit with the same answer, so
    asking again for each of a hundred subjects is a hundred wasted aggregation queries. Remembered
    ACROSS cycles it would keep refusing a destination that has since drained, which is
    indistinguishable from the intended 410 pause — so the set is built per call and dropped with it,
    which the second emit below asserts by succeeding.
    """
    client = _install()
    try:
        _live(client)
        real = notification_store._count_destination_deliveries
        notification_store._count_destination_deliveries = (  # type: ignore[assignment]
            lambda destination_id, *, state: notification_store.MAX_PENDING_EVENTS_PER_DESTINATION
        )
        subjects = [
            _subject(journey_ref=f"DatedServiceJourney:{5000 + i}") for i in range(4)
        ]
        report = notification_events.emit(subjects, now=NOW)
        assert report.quota == 4 and report.created == 0
        assert client.transactions == 0, "and only ONE of them paid for the discovery"

        notification_store._count_destination_deliveries = real  # type: ignore[assignment]
        assert notification_events.emit(subjects, now=NOW).created == 4, (
            "the skip is pass-local: a drained destination is attempted again next cycle"
        )
    finally:
        _uninstall()


def test_an_exhausted_transaction_is_classified_retryable_not_terminal():
    """The one classification a natural implementation gets wrong.

    The Firestore client retries a contended body five times and then raises a plain `ValueError`,
    not a `google.api_core` error — so a handler keyed on API exceptions calls it terminal, marks the
    subject failed, and never delivers a train nobody could commit an event for. `is_retryable`
    knows; this asserts the emit path asks IT rather than the exception's module.
    """
    client = _install()
    try:
        _live(client)
        prefix = notification_store._exhausted_transaction_prefix()
        client.fail_with = ValueError(f"{prefix} 5 attempts.")
        handler = _capture()
        report = notification_events.emit([_subject()], now=NOW)
        assert report.retryable == 1 and report.failed == 0
        assert any(e["result"] == "retryable" for e in _events(handler, "notification_emit"))

        client.fail_with = ValueError("something else entirely")
        mark = len(_SINK.records)
        report = notification_events.emit([_subject()], now=NOW)
        assert report.failed == 1 and report.retryable == 0
        # Asserted rather than printed: `_events` above removed the per-test handler, so this
        # second injection's record fell through to `lastResort` and put a traceback on stderr on
        # every run of a green suite.
        [record] = [r for r in _SINK.records[mark:] if r.exc_info]
        assert isinstance(record.exc_info[1], ValueError)
    finally:
        _uninstall()


def test_an_obsolete_subscription_emits_nothing_and_says_which_kind():
    """R13, enforced at the moment of the WRITE and not only at the read that produced the match.

    The pass matched against a snapshot taken before pass 1a, and a delete or an amendment can land
    in between. The version on the subject is the LOAD snapshot's for exactly this reason: compared
    against a fresh read it would always be equal, and an alert about a train matched for a route the
    client has since amended would go out stamped with the new version.
    """
    client = _install()
    try:
        _live(client)
        assert notification_events.emit([_subject(subscription_version=2)], now=NOW).obsolete == 1
        assert client.in_collection(notification_store.EVENTS) == []

        client.docs.clear()
        assert notification_events.emit([_subject()], now=NOW).obsolete == 1, "deleted subscription"
    finally:
        _uninstall()


# ---- R16: what never reaches a log line -------------------------------------------------------


def test_no_external_reference_route_or_prose_reaches_a_log_line():
    """R16 on this module's own records: identifiers yes, the traveller's trip no.

    `external_ref` is the client's own handle on a traveller and is carried for the envelope only.
    The route, the wording and the payload are what a routine log line must never hold — and the
    check is over the rendered message AND every structured field, because a field is exactly as
    published as the message.
    """
    client = _install()
    try:
        _live(client)
        handler = _capture()
        notification_events.emit(
            [
                _subject(
                    external_ref="commuter-42",
                    title="Tog L1 08:50 kan bli innstilt",
                    body="Oslo S → Ski\nSignalfeil",
                )
            ],
            now=NOW,
        )
        records = handler.records
        logging.getLogger("notification_events").removeHandler(handler)
        rendered = " ".join(
            [r.getMessage() for r in records] + [str(getattr(r, "fields", "")) for r in records]
        )
        for forbidden in ("commuter-42", "Oslo S", "Signalfeil", "innstilt", "NSR:StopPlace"):
            assert forbidden not in rendered, f"{forbidden!r} reached a log line: {rendered}"
        # And what an operator actually diagnoses with IS there: the cycle's counts. Not the
        # client id -- one summary line covers every client in the cycle, so a single id field
        # would be wrong rather than private. The per-failure records carry it (see
        # `test_a_failed_pre_read_...`), which is where a question about one client starts.
        summary = next(e for e in _events(_reattach(records), "notification_emit") if e["result"] == "ok")
        assert summary["created"] == 1 and summary["n"] == 1
    finally:
        _uninstall()


def test_the_two_ceilings_leave_the_phase_under_ktd6s_reversal_trigger():
    """KTD6's trigger is a number, so the ceilings that feed it have to add up to less than it.

    "Move delivery out when the webhook phase exceeds 30 s of measured phase time on any cycle in a
    week." Emit and deliver run in ONE `main._phase`, so their two outer bounds add: a pair that
    summed to 30 s or more would make the trigger fire on a bad morning by construction rather than
    because anything was wrong.
    """
    import webhook_delivery

    assert notification_events._EMIT_WAIT_S + webhook_delivery._DELIVERY_WAIT_S < 30.0
    assert notification_events._EMIT_DEADLINE_S < notification_events._EMIT_WAIT_S, (
        "the reservation must bind before the backstop, or the backstop is the only bound"
    )


def test_the_module_imports_neither_varsel_nor_main():
    """The dependency runs one way: `varsel` hands this module a value, never the reverse.

    Which is also what keeps this suite light — importing `varsel` would drag its import-time VAPID
    environment stubs into a process CI runs on its own, the leak the one-suite-per-process
    convention exists to prevent.
    """
    imports = set(re.findall(r"^import (\w+)", MODULE_SOURCE, re.MULTILINE))
    assert "varsel" not in imports and "main" not in imports and "impact" not in imports
    # An EXACT set, never a containment check: the point is that nothing heavy can be added without
    # a reader of this line deciding it is light. `reserved_budget` is the shared reserve-don't-check
    # pass budget, stdlib-only (`threading`, `time.monotonic`), and it is why `threading` is no
    # longer imported here directly -- the lock moved with the class that owns it.
    assert imports == {"logging", "notification_store", "reserved_budget"}, imports


if __name__ == "__main__":
    _ran = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            _ran += 1
            print(f"ok  {name}")
    # The COUNT, in every other suite's shape: a runner that silently collects nothing still exits
    # 0, so "it passed" is not the check -- "the number moved" is.
    print(f"\n{_ran} notification event checks passed.")
