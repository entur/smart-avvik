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
        # `_count_destination_deliveries` -- which the pass runs on the coordinator since U9, once
        # per destination, and which failing takes that destination's observations with it
        # (`test_a_destinations_uncountable_ceiling_...`). A test that wants one query to fail must
        # be able to say which.
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
        if start_after is not None:
            # Positional, as Firestore's own cursor is: everything strictly after the snapshot in
            # THIS ordering. `pending_deliveries` pages past a throttled destination this way.
            paths = [path for path, _ in rows]
            if start_after._path in paths:
                rows = rows[paths.index(start_after._path) + 1 :]
        return _Page(client, rows[:limit])

    notification_store._run_transaction = _run  # type: ignore[assignment]
    notification_store._query = _query  # type: ignore[assignment]
    notification_store.configure(client=client, clock=lambda: NOW)
    # The standing set the pass reads, seeded over an empty store exactly as the poller's own
    # `sync_mirrors` step does before every cycle. It has to be READY or `_standing` answers
    # `_INCOMPLETE` for everything and the whole pre-read optimisation is switched off -- which is
    # the restart window's correct behaviour and would make every assertion here about the fallback
    # instead. From here it is kept current by the commits themselves (`_mirror_subject`), which is
    # what these tests are checking.
    _sync_mirror()
    return client


def _sync_mirror(now: datetime = NOW) -> None:
    """The poller's own `sync_mirrors` step: read the standing set out of whatever the store holds.

    A fresh process does exactly this before its first pass, which is what makes a withdrawal
    counter survive a restart — the count lives on the subject document, and the mirror is how this
    module gets to see it without a read per subject.
    """
    notification_store.reset_mirrors_for_tests()
    notification_store.subject_mirror().seed(now=now)


def _uninstall() -> None:
    _drain_pools()
    if _saved:
        notification_events.ThreadPoolExecutor = _saved["pool"]  # type: ignore[misc]
        notification_store._firestore_client = _saved["client"]  # type: ignore[assignment]
        notification_store._run_transaction = _saved["run"]  # type: ignore[assignment]
        notification_store._query = _saved["query"]  # type: ignore[assignment]
        _saved.clear()
    # Module state, and it leaks between tests exactly as the configured client does: a mirror left
    # holding the previous test's subjects reads here as a healthy one.
    notification_store.reset_mirrors_for_tests()
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


def _observed(subjects: list[notification_events.WebhookSubject]) -> list[Any]:
    """Group subjects into observations the way `varsel._observations_for_key` does.

    The pass hands `emit` one observation per (definition, train) with its recipients since U9;
    this suite's fixtures stay written in SUBJECTS because a subject is what the store sees and
    what every assertion here is about. The grouping key is the train plus the observation —
    exactly what `varsel` shares across the recipients on one definition — with `payload` left out
    because a dict is unhashable and, in this suite, identical wherever the rest of the key is.
    """
    grouped: dict[tuple, list[notification_events.WebhookSubject]] = {}
    for subject in subjects:
        grouped.setdefault(
            (
                subject.alertworthy,
                subject.cancelled,
                subject.situations,
                subject.operator,
                subject.line,
                subject.journey_ref,
                subject.stop_place,
                subject.stop_order,
                subject.departure_time,
                subject.title,
                subject.body,
                subject.expected_departure_time,
            ),
            [],
        ).append(subject)
    return [
        notification_events.WebhookObservation(
            recipients=tuple(
                notification_events.Recipient(
                    subscription_id=s.subscription_id,
                    client_id=s.client_id,
                    destination_id=s.destination_id,
                    subscription_version=s.subscription_version,
                    external_ref=s.external_ref,
                    test=s.test,
                )
                for s in group
            ),
            alertworthy=group[0].alertworthy,
            cancelled=group[0].cancelled,
            situations=group[0].situations,
            operator=group[0].operator,
            line=group[0].line,
            journey_ref=group[0].journey_ref,
            stop_place=group[0].stop_place,
            stop_order=group[0].stop_order,
            departure_time=group[0].departure_time,
            title=group[0].title,
            body=group[0].body,
            payload=group[0].payload,
            expected_departure_time=group[0].expected_departure_time,
            # U16's seam, in `varsel._decision_key`'s shape: (journey definition, situation,
            # departure). One per observation however many recipients it has, which is the property
            # `test_the_mints_are_also_reported_at_the_decision_logs_grain` is about.
            decision_key=(
                "jkey|from|to|window",
                group[0].situations[0] if group[0].situations else "",
                group[0].departure_time.isoformat() if group[0].departure_time else "",
            ),
        )
        for group in grouped.values()
    ]


def _emit(subjects: list[notification_events.WebhookSubject], **kwargs: Any) -> Any:
    """`notification_events.emit` over the observations those subjects belong to."""
    return notification_events.emit(_observed(subjects), **kwargs)


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
        first = _emit([_subject()], now=NOW)
        events = client.in_collection(notification_store.EVENTS)
        assert first.created == 1 and len(events) == 1
        assert events[0]["type"] == notification_store.EVENT_CREATED
        assert events[0]["phase"] == notification_store.PHASE_PREDICTED
        event_id, revision = events[0]["event_id"], events[0]["revision"]

        # A LATER cycle, with a different probability in the payload: the facts moved, the lifecycle
        # did not. Nothing is minted, at any size of change -- there is no materiality threshold.
        second = _emit(
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
        _emit([_subject()], now=NOW)
        first = client.in_collection(notification_store.EVENTS)[0]
        notification_store.mark_delivery_sent(first["event_id"], now=NOW + timedelta(seconds=5))

        _emit([_subject(cancelled=True)], now=NOW + timedelta(minutes=1))
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
        _emit([_subject()], now=NOW)
        first = client.in_collection(notification_store.EVENTS)[0]
        _emit([_subject(cancelled=True)], now=NOW + timedelta(minutes=1))
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
        report = _emit(subjects, now=NOW)
        assert report.created == 13
        events = client.in_collection(notification_store.EVENTS)
        assert len({e["alert_id"] for e in events}) == 13
        # Minted in departure order, which the revision (clamped to `stored + 1` per subject, seeded
        # from one instant) cannot show -- so read the order off the queue itself.
        order = [
            o.journey_ref
            for o in sorted(_observed(subjects), key=notification_events._sort_key)
        ]
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
        _emit([_subject()], now=NOW)
        one = _emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=1))
        assert one.counting == 1 and one.withdrawn == 0, "one dip is noise"
        assert len(client.in_collection(notification_store.EVENTS)) == 1

        # A RESTART: the module holds nothing, the store holds the count.
        docs = dict(client.docs)
        _uninstall()
        client = _install()
        client.docs.update(docs)
        # Seeded AFTER the documents are back, because that is the order a pod comes up in: the
        # standing set is read out of Firestore by the poller's own step, before the pass.
        _sync_mirror()
        two = _emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=2))
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
        _emit([_subject()], now=NOW)
        dip = _emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=1))
        assert dip.counting == 1, dip

        was = client.in_collection(notification_store.SUBJECTS)[0]
        recovered = _emit([_subject()], now=NOW + timedelta(minutes=2))
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
        second = _emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=3))
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
        _emit([_subject()], now=NOW)
        before = client.transactions
        report = _emit([_subject()], now=NOW + timedelta(minutes=1))
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
        _emit([_subject()], now=NOW)
        _emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=1))
        later = NOW + timedelta(seconds=notification_store.WITHDRAWAL_GAP_S + 120)
        before = client.transactions
        report = _emit([_subject()], now=later)
        assert report.run_stale == 1 and report.run_cleared == 0, report
        assert client.transactions == before, "and it costs no transaction"
    finally:
        _uninstall()


def test_an_unseeded_mirror_loses_the_reset_and_never_reads_per_subject():
    """R20, applied to the reset: the restart window costs the optimisation's exactness, never a read.

    The failure this guards used to be a failed `client_alerts` query; since U9 the standing set is
    in memory and the only state that cannot answer is a mirror that has not finished seeding — a
    pod coming up cold, which happens on every deploy. `open_runs` is then empty, so no run is
    closed this cycle and the store's time window is what protects the traveller. What must NOT
    happen is a per-subject read to recover the information, which is the cost the whole pre-read
    exists to avoid.
    """
    client = _install()
    try:
        _live(client)
        _emit([_subject()], now=NOW)
        _emit([_subject(alertworthy=False)], now=NOW + timedelta(minutes=1))
        notification_store.reset_mirrors_for_tests()
        assert notification_store.subject_mirror().ready is False
        mark = len(_SINK.records)
        report = _emit([_subject()], now=NOW + timedelta(minutes=2))
        assert report.run_cleared == 0 and report.run_stale == 0, report
        assert report.unchanged == 1, "the store still answered the subject itself"
        assert not any("get_subject" in str(r.msg) for r in _SINK.records[mark:])
        # The run is still open, so the store's time window governs — the documented fallback.
        subject = client.in_collection(notification_store.SUBJECTS)[0]
        assert subject["below_threshold_at"] is not None
    finally:
        _uninstall()


def test_a_below_threshold_train_with_nothing_standing_costs_no_transaction():
    """The pre-read's whole purpose, and the cheap half of scenario 7.

    ~83 % of matched alerts are sub-threshold. A `record_below_threshold` for a train nobody was
    ever warned about writes nothing (`nothing_standing`), so paying a Firestore transaction to
    discover that on every cycle for every such train is the cost the standing set exists to avoid.

    Since U9 it costs no QUERY either, which is the stronger claim and the one worth pinning: the
    answer is in memory, so a cycle over a quiet population reaches Firestore not at all.
    """
    client = _install()
    try:
        _live(client)
        before = client.transactions
        queries = len(client.queries)
        report = _emit([_subject(alertworthy=False)], now=NOW)
        assert report.nothing_standing == 1
        assert client.transactions == before, "no transaction was spent"
        assert client.queries[queries:] == [], "and no query either — the mirror answered it"
    finally:
        _uninstall()


def test_an_unchanged_alertworthy_train_costs_no_transaction_either():
    """The other half of the pre-read: `_transition` would return None, so nothing is asked."""
    client = _install()
    try:
        _live(client)
        _emit([_subject()], now=NOW)
        before = client.transactions
        report = _emit([_subject()], now=NOW + timedelta(minutes=1))
        assert report.unchanged == 1 and client.transactions == before
    finally:
        _uninstall()


def test_a_shared_observation_materialises_a_subject_only_where_a_store_call_happens():
    """U9 step 1, and the number it prevents is the reason it exists.

    The pass used to hand over one `WebhookSubject` per (recipient, train), built in `varsel`
    before this module saw any of them — so a corridor with 100 000 subscriptions and AE3's
    thirteen cancelled departures materialised 1.3 million dataclasses in one burst, on the
    ingestion thread, in a pod whose memory limit equals its request. The payload was already
    shared by reference; the objects around it were not.

    What is asserted is the property rather than a byte count: the observation is one object, and a
    subject is built only where the standing set says a store call is going to happen. For the ~83 %
    of matched alerts that are sub-threshold with nothing standing, that is never.
    """
    client = _install()
    try:
        recipients = []
        for i in range(5):
            sid = f"sub-{i}"
            _live(client, subscription_id=sid)
            recipients.append(
                notification_events.Recipient(
                    subscription_id=sid,
                    client_id=CLIENT,
                    destination_id=DEST,
                    subscription_version=1,
                )
            )
        shared = _observed([_subject(alertworthy=False)])[0]
        quiet = notification_events.WebhookObservation(
            **{**shared.__dict__, "recipients": tuple(recipients)}
        )

        built: list[str] = []
        original = notification_events.WebhookObservation.subject

        def counting(self: Any, recipient: Any) -> Any:
            built.append(recipient.subscription_id)
            return original(self, recipient)

        notification_events.WebhookObservation.subject = counting  # type: ignore[method-assign]
        try:
            quietly = notification_events.emit([quiet], now=NOW)
            assert quietly.nothing_standing == 5, quietly
            assert built == [], (
                f"a below-threshold observation nobody stands on built {len(built)} subject(s); "
                "the whole point of the shape is that it builds none"
            )

            # And where they DO stand, every recipient is materialised — once each.
            alert = notification_events.WebhookObservation(
                **{**quiet.__dict__, "alertworthy": True}
            )
            loud = notification_events.emit([alert], now=NOW)
            assert loud.created == 5, loud
            assert sorted(built) == [f"sub-{i}" for i in range(5)]
        finally:
            notification_events.WebhookObservation.subject = original  # type: ignore[method-assign]
    finally:
        _uninstall()


def test_the_train_identity_is_resolved_once_per_train_not_once_per_recipient():
    """The other half of step 1: `train_ref_of` and the requested phase are per OBSERVATION.

    At 100 000 subscriptions on one corridor the difference between per-train and per-recipient is
    four calls against four hundred thousand, every cycle — and the answer cannot differ between
    two recipients, because it is derived from the train alone.
    """
    client = _install()
    try:
        recipients = []
        for i in range(4):
            sid = f"sub-{i}"
            _live(client, subscription_id=sid)
            recipients.append(
                notification_events.Recipient(
                    subscription_id=sid,
                    client_id=CLIENT,
                    destination_id=DEST,
                    subscription_version=1,
                )
            )
        shared = _observed([_subject()])[0]
        observation = notification_events.WebhookObservation(
            **{**shared.__dict__, "recipients": tuple(recipients)}
        )

        calls = 0
        original = notification_events.train_ref_of

        def counting(**kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return original(**kwargs)

        notification_events.train_ref_of = counting  # type: ignore[assignment]
        try:
            report = notification_events.emit([observation], now=NOW)
        finally:
            notification_events.train_ref_of = original  # type: ignore[assignment]
        assert report.created == 4, report
        assert calls == 1, f"the train was identified {calls} times for one observation"
    finally:
        _uninstall()


def test_the_standing_set_is_asked_once_per_subscription_not_once_per_observation():
    """AE3's line suspension is thirteen departures on ONE subscription.

    `phases_for` is a lock, a list copy and a dict build; asked per recipient rather than per
    subscription, twelve of those thirteen answers would be spent on a dict comprehension that
    overwrites them. At 100 000 subscriptions on one corridor it is 100 000 lookups against 1.3
    million, every cycle — the same quadratic-in-a-linear-structure mistake `open_runs` is asked
    once to avoid.
    """
    client = _install()
    try:
        _live(client)
        subjects = [
            _subject(journey_ref=f"DatedServiceJourney:{4000 + i}", alertworthy=False)
            for i in range(13)
        ]
        mirror = notification_store.subject_mirror()
        asked: list[str] = []
        original = mirror.phases_for

        def counting(subscription_id: str, now: datetime) -> Any:
            asked.append(subscription_id)
            return original(subscription_id, now)

        mirror.phases_for = counting  # type: ignore[method-assign]
        try:
            report = _emit(subjects, now=NOW)
        finally:
            del mirror.phases_for
        assert report.nothing_standing == 13, report
        assert asked == [SID], f"the standing set was asked {len(asked)} times for one subscription"
    finally:
        _uninstall()


def test_the_pre_reads_own_ceiling_is_gone_and_a_line_suspension_costs_nothing():
    """U9 step 2, and this test is the whole reason the standing set moved into memory.

    The old pre-read was `client_alerts`, which COUNTS every live subject and enumerates at most
    `MAX_ALERTS_SERVED` (4) of them — so a subscription holding more than that reported a count the
    caller could not resolve to phases, and an incomplete answer means a store transaction PER
    SUBJECT. AE3's line suspension is thirteen cancelled departures inside one window, so the
    optimisation switched itself off in exactly the case it was written for, and at one client with
    100 000 subscriptions that fallback was the steady state.

    The mirror enumerates everything, so the same population now costs no transaction at all. The
    ceiling it replaces is asserted by name rather than by a magic number, so this keeps meaning
    what it says if `MAX_ALERTS_SERVED` ever moves.
    """
    client = _install()
    try:
        _live(client)
        served = notification_store.MAX_ALERTS_SERVED
        subjects = [
            _subject(journey_ref=f"DatedServiceJourney:{3000 + i}") for i in range(served + 2)
        ]
        _emit(subjects, now=NOW)
        assert len(client.in_collection(notification_store.SUBJECTS)) == served + 2

        before = client.transactions
        report = _emit(subjects, now=NOW + timedelta(minutes=1))
        assert report.unchanged == len(subjects), "every one still resolves to `no transition`"
        assert client.transactions == before, (
            f"a subscription past the old display ceiling ({served}) cost "
            f"{client.transactions - before} transaction(s); the mirror is complete by construction"
        )
    finally:
        _uninstall()


def test_an_unseeded_mirror_costs_transactions_and_never_events():
    """R20: the restart window suppresses the OPTIMISATION, never the correctness.

    `sync_mirrors` runs as its own poller step before the pass, so an unseeded mirror is the window
    between a pod starting and its first sync finishing. Every subject falls back to asking the
    store, which is the slow path and the only correct one — and no event is lost to it.
    """
    client = _install()
    try:
        _live(client)
        notification_store.reset_mirrors_for_tests()
        before = client.transactions
        report = _emit([_subject()], now=NOW)
        assert report.created == 1, "the event was still committed"
        assert client.transactions > before, "and it was the store that answered, per subject"
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
        _emit([_subject(departure_time=before + timedelta(minutes=20))], now=before)
        first = client.in_collection(notification_store.EVENTS)[0]["revision"]
        # A step BACKWARDS, further than the fall-back: the clamp is what holds now.
        _emit(
            [_subject(cancelled=True, departure_time=before + timedelta(minutes=20))],
            now=before - timedelta(days=2),
        )
        revisions = sorted(e["revision"] for e in client.in_collection(notification_store.EVENTS))
        assert revisions[-1] == first + 1, f"strictly increasing, got {revisions}"
    finally:
        _uninstall()


def test_validity_follows_the_expected_departure_and_is_floored_when_already_past():
    """R12's departure term is the EXPECTED departure, the matcher's own catchability figure.

    A train 25 minutes late is kept by the matcher past its aimed time (`catchable_after` compares
    the expected departure) and pushed to browsers -- and until 2026-09-10 its event's validity was
    clamped to the AIMED time, so it was stale at mint and `pending_deliveries` retired it before
    any attempt. The second half is the match-to-emit gap: an expected departure less than
    `DEPARTURE_GRACE_S` ahead at emit -- a few seconds behind, or a few seconds ahead -- is floored
    to it, so the observation is attempted once in this cycle rather than expiring before the
    delivery step classifies it, and the grace is under one pass period so it cannot carry a
    departed train into a retry.
    """
    client = _install()
    try:
        _live(client)
        late = _subject(
            journey_ref="DatedServiceJourney:late",
            departure_time=NOW - timedelta(minutes=5),
            expected_departure_time=NOW + timedelta(minutes=20),
        )
        gone = _subject(
            journey_ref="DatedServiceJourney:gone",
            departure_time=NOW - timedelta(minutes=10),
            expected_departure_time=NOW - timedelta(seconds=30),
        )
        imminent = _subject(
            journey_ref="DatedServiceJourney:imminent",
            departure_time=NOW - timedelta(minutes=10),
            expected_departure_time=NOW + timedelta(seconds=30),
        )
        aimed_only = _subject(
            journey_ref="DatedServiceJourney:aimed", departure_time=NOW + timedelta(minutes=12)
        )
        _emit([late, gone, imminent, aimed_only], now=NOW)
        events = {e["event_id"]: e for e in client.in_collection(notification_store.EVENTS)}
        by_ref = {
            events[d["event_id"]]["journey_ref"]: d
            for d in client.in_collection(notification_store.DELIVERIES)
        }
        assert by_ref["DatedServiceJourney:late"]["valid_until"] == NOW + timedelta(minutes=20)
        assert by_ref["DatedServiceJourney:gone"]["valid_until"] == NOW + timedelta(
            seconds=notification_events.DEPARTURE_GRACE_S
        )
        assert by_ref["DatedServiceJourney:imminent"]["valid_until"] == NOW + timedelta(
            seconds=notification_events.DEPARTURE_GRACE_S
        ), "a term still ahead but inside the grace is floored too, not only one already past"
        assert by_ref["DatedServiceJourney:aimed"]["valid_until"] == NOW + timedelta(minutes=12), (
            "no expected departure carried: the aimed time is the term, unchanged"
        )
        # And both are deliverable at the instant they were minted, which is the whole point.
        pending = notification_store.pending_deliveries(now=NOW)
        assert {d.valid_until for d in pending.ready} >= {
            NOW + timedelta(minutes=20),
            NOW + timedelta(seconds=notification_events.DEPARTURE_GRACE_S),
        }
        assert pending.stale == ()
        assert notification_events.DEPARTURE_GRACE_S < 296, "under one measured pass period"
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
        report = _emit([_subject(stop_order=None)], now=NOW)
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
        # ONE worker, because what this checks is the reservation's arithmetic and not the pool's:
        # at sixty-four the six slow transactions overlap and all six fit inside the deadline,
        # which is the pool working rather than the budget failing.
        report = _emit(
            subjects, now=NOW, deadline_s=0.2, wait_s=2.0, worst_case_s=0.12, workers=1
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
        report = _emit([_subject()], now=NOW, wait_s=0.1)
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
            # Written to the store's EXACT arity, `ceiling` included: a stub narrower than the
            # call site fails with a TypeError that reads like a bug in the code under test.
            lambda destination_id, *, state, ceiling: ceiling
        )
        subjects = [
            _subject(journey_ref=f"DatedServiceJourney:{5000 + i}") for i in range(4)
        ]
        report = _emit(subjects, now=NOW)
        assert report.quota == 4 and report.created == 0
        assert client.transactions == 0, "and only ONE of them paid for the discovery"

        notification_store._count_destination_deliveries = real  # type: ignore[assignment]
        assert _emit(subjects, now=NOW).created == 4, (
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
        report = _emit([_subject()], now=NOW)
        assert report.retryable == 1 and report.failed == 0
        assert any(e["result"] == "retryable" for e in _events(handler, "notification_emit"))

        client.fail_with = ValueError("something else entirely")
        mark = len(_SINK.records)
        report = _emit([_subject()], now=NOW)
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
        assert _emit([_subject(subscription_version=2)], now=NOW).obsolete == 1
        assert client.in_collection(notification_store.EVENTS) == []

        client.docs.clear()
        assert _emit([_subject()], now=NOW).obsolete == 1, "deleted subscription"
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
        _emit(
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


def test_the_transactions_of_one_pass_overlap_rather_than_queueing():
    """U9 step 3. One transaction at a time is ~5 emits a second however many are waiting.

    Asserted with a `Barrier`, not with a timing: four store calls must be in flight AT ONCE or the
    barrier times out and the test fails on its own clock rather than on a threshold somebody has
    to tune. Four different subscriptions, because that is what a corridor looks like — and because
    two emits about one train on one subscription would be one subject, not two.
    """
    client = _install()
    try:
        subjects = []
        for i in range(4):
            sid = f"sub-{i}"
            _live(client, subscription_id=sid)
            subjects.append(_subject(subscription_id=sid))
        barrier = threading.Barrier(4, timeout=10)
        met = threading.Event()
        real = notification_store.commit_event

        def meeting(request: Any, **kwargs: Any) -> Any:
            try:
                barrier.wait()
                met.set()
            except threading.BrokenBarrierError:
                pass
            return real(request, **kwargs)

        notification_store.commit_event = meeting  # type: ignore[assignment]
        try:
            report = _emit(subjects, now=NOW, workers=4)
        finally:
            notification_store.commit_event = real  # type: ignore[assignment]
        assert met.is_set(), "the four transactions never overlapped; the pass is still serial"
        assert report.created == 4, report
    finally:
        _uninstall()


def test_a_superseding_mint_gives_its_pending_slot_back():
    """The ledger is net-zero on a replacement, and a counter that only counts up is not.

    `_mint` flips the superseded delivery to `superseded` in the same transaction that creates the
    new one, so a `confirmed` replacing an undelivered `predicted` leaves the destination holding
    exactly as many pending events as before. A ledger that charged without refunding would refuse
    at HALF the ceiling on precisely the morning that matters — one where many mints are a
    confirmation — and the symptom is a quota refusal an operator cannot reconcile with the count
    Firestore holds.

    Both mints are in ONE pass, because the ledger is pass-local: across two passes it is recounted
    from the store and the refund has nothing to show. The counter reads the fake's own pending
    deliveries rather than a fixed number, so the ledger's arithmetic is checked against what
    Firestore would actually answer.
    """
    client = _install()
    try:
        _live(client)
        real = notification_store._count_destination_deliveries

        def live_count(destination_id: str, *, state: str, ceiling: int) -> int:
            return len(
                [
                    d
                    for d in client.in_collection(notification_store.DELIVERIES)
                    if d.get("destination_id") == destination_id and d.get("state") == state
                ]
            )

        notification_store._count_destination_deliveries = live_count  # type: ignore[assignment]
        try:
            ceiling = 2
            soon = _subject(
                journey_ref="DatedServiceJourney:7000",
                departure_time=NOW + timedelta(minutes=10),
            )
            first = _emit([soon], now=NOW, workers=1, pending_quota=lambda c: ceiling)
            assert first.created == 1, first

            # One pass, two trains, a destination holding ONE pending event and a ceiling of two.
            # The soonest is the same train declared cancelled — a `predicted` -> `confirmed`
            # transition, which supersedes rather than adds — and the later one is genuinely new.
            # Without the refund the replacement spends the second slot and the new train is
            # refused, which is the defect this exists to catch.
            report = _emit(
                [
                    _subject(
                        journey_ref="DatedServiceJourney:7000",
                        departure_time=NOW + timedelta(minutes=10),
                        cancelled=True,
                    ),
                    _subject(
                        journey_ref="DatedServiceJourney:7001",
                        departure_time=NOW + timedelta(minutes=40),
                    ),
                ],
                now=NOW + timedelta(minutes=1),
                workers=1,
                pending_quota=lambda c: ceiling,
            )
            assert report.created == 2 and report.quota == 0, (
                f"the superseded slot was never given back: {report}"
            )
            pending = [
                d
                for d in client.in_collection(notification_store.DELIVERIES)
                if d["state"] == "pending"
            ]
            assert len(pending) == 2, f"one replacement and one new train; got {pending}"
        finally:
            notification_store._count_destination_deliveries = real  # type: ignore[assignment]
    finally:
        _uninstall()


def test_the_mints_are_also_reported_at_the_decision_logs_grain_deduplicated():
    """U16 reads `minted_decisions`, and the two grains are not interchangeable.

    An alert id is per (subscription, train): a corridor with five hundred subscriptions watching
    one disrupted departure mints five hundred of them. A decision row is per (definition,
    situation, departure) — ONE, whatever the corridor's population — so the second list has to be
    deduplicated at the source rather than by whoever reads it, or the mint write would be
    O(subscriptions) on exactly the shape U9 emptied of per-recipient work.

    The key is carried on the observation rather than derived, because neither grain converts to
    the other: an alert id embeds a subscription and a decision key embeds a situation.
    """
    client = _install()
    try:
        _live(client)
        _live(client, subscription_id="sub-0002")
        # Two subscriptions on the SAME train: one observation, two recipients, two alert ids.
        shared = [_subject(), _subject(subscription_id="sub-0002")]
        # A second DEPARTURE under the same situation, which is what a decision key tells apart:
        # one situation reaches 12 h from its onset and names several trains.
        other = _subject(
            journey_ref="DatedServiceJourney:9999",
            departure_time=NOW + timedelta(minutes=40),
        )
        report = _emit(shared + [other], now=NOW, workers=1)

        assert report.created == 3, report
        assert len(report.minted) == 3, "three (subscription, train) subjects were minted"
        assert len(report.minted_decisions) == 2, (
            f"but only two decision subjects: {report.minted_decisions}"
        )
        assert len({k[2] for k in report.minted_decisions}) == 2, (
            "and they are told apart by the train, which is what the decision key carries"
        )
    finally:
        _uninstall()


def test_the_cycles_mints_are_reported_and_no_per_event_line_is_written():
    """U9 step 5. One record per cycle with counts, not one per committed revision.

    `commit_event` wrote an INFO line per mint carrying the client, the destination, the alert id
    and the revision. At the pilot's few hundred events a day that was the right grain; a widened
    pass writes 20 000 a cycle over ~300 cycles a day, which is a logging bill and a summary nobody
    can read. Nothing durable was lost — the events are Firestore documents, not log lines — and a
    question about one client still starts at the per-failure records, which carry `client_id`.

    What replaces it for a reader inside the process is the ledger on the report: what this pass
    minted, on the value the pass returns rather than on a module global, which is
    `varsel.PassOutcome`'s rule and for its reason.
    """
    client = _install()
    try:
        _live(client)
        handler = _capture()
        # The per-revision record was `notification_store`'s, so that logger has to be watched too
        # or this assertion could never see it come back.
        store_logger = logging.getLogger("notification_store")
        store_logger.setLevel(logging.INFO)
        store_logger.addHandler(handler)
        subjects = [
            _subject(journey_ref=f"DatedServiceJourney:{9600 + i}") for i in range(3)
        ]
        report = _emit(subjects, now=NOW, workers=1)
        records = list(handler.records)
        store_logger.removeHandler(handler)
        logging.getLogger("notification_events").removeHandler(handler)

        assert report.created == 3, report
        assert len(report.minted) == 3, report.minted
        assert set(report.minted) == {
            notification_store.alert_subject_id(SID, notification_events.train_ref(s))
            for s in subjects
        }
        names = {getattr(r, "fields", {}).get("event") for r in records}
        assert "notification_event" not in names, (
            f"a per-revision line is back; a cycle would write 20 000 of them. Saw {names}"
        )
    finally:
        _uninstall()


def test_a_destinations_uncountable_ceiling_costs_that_destination_and_not_the_cycle():
    """R20, applied to the two Firestore calls U9 moved onto the coordinator.

    The quota resolver and the ledger's first sight of a destination were both INSIDE the
    per-subject `try` before this unit — the resolver as an argument to `_emit_one`, the count
    inside `commit_event` — so a blip cost one subject a `retryable` and the pass carried on.
    Moved onto the coordinator and left unguarded, one raise leaves the loop, leaves `_run`, and
    reaches `emit`'s handler, which reports the whole cycle as a timeout and DISCARDS the counts of
    every transaction already committed. A 100 000-subscription morning is exactly when a 4 s
    aggregation deadline gets hit.
    """
    client = _install()
    try:
        _live(client, subscription_id="sub-bad")
        _live(client, subscription_id="sub-good")
        real = notification_store._count_destination_deliveries

        def flaky(destination_id: str, *, state: str, ceiling: int) -> int:
            if destination_id == "dest-bad":
                raise RuntimeError("Firestore is unreachable")
            return 0

        notification_store._count_destination_deliveries = flaky  # type: ignore[assignment]
        try:
            report = _emit(
                [
                    _subject(
                        subscription_id="sub-bad",
                        destination_id="dest-bad",
                        journey_ref="DatedServiceJourney:9500",
                    ),
                    _subject(
                        subscription_id="sub-good",
                        destination_id="dest-good",
                        journey_ref="DatedServiceJourney:9501",
                    ),
                ],
                now=NOW,
                workers=1,
            )
        finally:
            notification_store._count_destination_deliveries = real  # type: ignore[assignment]
        assert report.timed_out is False, f"one destination's blip failed the whole cycle: {report}"
        assert report.created == 1, report
        assert report.retryable == 1, report
    finally:
        _uninstall()


def test_a_raise_before_the_pool_exists_still_releases_the_pass_lock():
    """The lock is held for the life of the pod otherwise, and nothing says so.

    `_standing`, the ledger's construction and the pool's own all sit above the loop's `try`. A
    raise in any of them would skip a `finally` that released there, and every later cycle would
    answer `overlap_refused` with nothing actually draining — a pass that has stopped running and a
    signal that says it is still running are the same reading.
    """
    client = _install()
    try:
        _live(client)
        real = notification_events._standing
        notification_events._standing = lambda ids, now: (_ for _ in ()).throw(  # type: ignore[assignment]
            RuntimeError("the standing set exploded")
        )
        try:
            broken = _emit([_subject()], now=NOW, workers=1)
            assert broken.timed_out is True and broken.created == 0, broken
        finally:
            notification_events._standing = real  # type: ignore[assignment]

        after = _emit([_subject()], now=NOW, workers=1)
        assert after.overlap_refused is False, (
            "the pass lock survived a raise above the pool; every later cycle is now refused"
        )
        assert after.created == 1, after
    finally:
        _uninstall()


def test_a_second_pass_is_refused_while_the_first_is_still_draining():
    """Two overlapping passes each hold their own pending count and admit past a ceiling together.

    The emit path's exactness rests on there being one writer, and U9 makes that writer a pass
    rather than a thread — so what enforces it is this refusal plus the ledger's lock. The
    abandoned workers of a timed-out pass are exactly the ones still committing, which is why the
    lock is released by `_run`'s own `finally` and not by the thread that gave up waiting.
    """
    client = _install()
    try:
        _live(client)
        holding = threading.Event()
        release = threading.Event()
        real = notification_store.commit_event

        def slow(request: Any, **kwargs: Any) -> Any:
            holding.set()
            release.wait(timeout=10)
            return real(request, **kwargs)

        notification_store.commit_event = slow  # type: ignore[assignment]
        try:
            first: list[Any] = []
            runner = threading.Thread(
                target=lambda: first.append(
                    _emit([_subject()], now=NOW, workers=1, wait_s=0.2, deadline_s=5.0)
                )
            )
            runner.start()
            assert holding.wait(timeout=5), "the first pass never reached the store"
            runner.join(timeout=5)
            assert first and first[0].timed_out, first

            # The first pass's worker is still parked in the store, so the second must refuse.
            refused = _emit(
                [_subject(journey_ref="DatedServiceJourney:8000")], now=NOW, workers=1
            )
            assert refused.overlap_refused is True, refused
            assert refused.created == 0

            release.set()
            # Once it drains, the next cycle runs normally again — a refusal is one cycle, not a
            # latch.
            for _ in range(100):
                time.sleep(0.02)
                again = _emit(
                    [_subject(journey_ref="DatedServiceJourney:8001")], now=NOW, workers=1
                )
                if not again.overlap_refused:
                    break
            assert again.overlap_refused is False and again.created == 1, again
        finally:
            release.set()
            notification_store.commit_event = real  # type: ignore[assignment]
    finally:
        _uninstall()


def test_the_pass_beats_the_heartbeat_while_it_submits():
    """`/health`'s staleness rule means "no STEP has finished in 600 s", and this step may run 80 s.

    A long way inside the threshold today, and a long way further inside it than the 6 s deadline
    was — so the pass beats per chunk already rather than after the next widening. A seam that
    raises must not cost the pass, which is `varsel._heartbeat`'s rule restated.
    """
    client = _install()
    try:
        _live(client)
        beats = 0

        def boom() -> None:
            nonlocal beats
            beats += 1
            raise RuntimeError("heartbeat exploded")

        subjects = [
            _subject(journey_ref=f"DatedServiceJourney:{9000 + i}")
            for i in range(notification_events._HEARTBEAT_EVERY + 1)
        ]
        report = _emit(subjects, now=NOW, workers=4, beat=boom)
        assert report.created == len(subjects), report
        assert beats >= 2, f"beat {beats} time(s) over {len(subjects)} submissions"
    finally:
        _uninstall()


#: The poller cycle's measured period, 2026-09-08. A measurement, not a constant — it is set by how
#: often the upstream SIRI-SX ingestion timestamp moves — so the bound below is a SHARE of it rather
#: than a number, and re-measuring is what moves it.
MEASURED_CYCLE_PERIOD_S = 296.0
#: R2's target, which is what `_EMIT_DEADLINE_S` and `_MAX_EMITS_PER_CYCLE` are both derived from.
TARGET_EMITS_PER_SECOND = 250


def test_the_phase_ceiling_is_a_share_of_the_cycle_rather_than_ktd6s_thirty_seconds():
    """U9 step 6. KTD6's trigger was a number and the widened pass exceeds it BY DESIGN.

    It read "move delivery out when the webhook phase exceeds 30 s of measured phase time on any
    cycle in a week", and until U9 the two outer bounds summed to 28 s, pinned here at 2 s under
    it. That arithmetic was right for a one-wide loop at the pilot's few hundred events a day and
    is the wrong shape for R2: a bad morning's transitions have to be emitted inside ONE cycle, and
    one transaction at a time against a ~200 ms round trip is ~5 emits a second however many are
    waiting. So the deadline moved with the width, and the trigger is restated in the operations
    document as emit's share being exceeded — the pass ran to its deadline with work left over —
    rather than as a wall-clock ceiling.

    What still has to hold, and is checked here rather than argued:

    * the phase's ceiling stays a MINORITY of the cycle it runs in, or a slow store turns every
      cycle into the previous cycle's tail;
    * the reservation binds before the backstop, or the backstop is the only bound there is;
    * the two derived numbers stay derived: `_EMIT_DEADLINE_S` times R2's target rate IS
      `_MAX_EMITS_PER_CYCLE`, so a change to one that forgets the other is caught here rather than
      discovered as a pass that stops early with budget to spare.
    """
    import webhook_delivery

    phase_ceiling = notification_events._EMIT_WAIT_S + webhook_delivery._DELIVERY_WAIT_S
    assert phase_ceiling <= MEASURED_CYCLE_PERIOD_S / 2, (
        f"the webhook phase's ceiling is {phase_ceiling:.0f} s against a ~{MEASURED_CYCLE_PERIOD_S:.0f} s "
        "cycle period; past half of it a bad store makes every cycle the previous one's tail"
    )
    assert notification_events._EMIT_DEADLINE_S < notification_events._EMIT_WAIT_S, (
        "the reservation must bind before the backstop, or the backstop is the only bound"
    )
    assert (
        notification_events._MAX_EMITS_PER_CYCLE
        == notification_events._EMIT_DEADLINE_S * TARGET_EMITS_PER_SECOND
    ), (
        f"{notification_events._MAX_EMITS_PER_CYCLE} emits over "
        f"{notification_events._EMIT_DEADLINE_S:.0f} s is "
        f"{notification_events._MAX_EMITS_PER_CYCLE / notification_events._EMIT_DEADLINE_S:.0f}/s, "
        f"not R2's {TARGET_EMITS_PER_SECOND}/s. The count and the clock are one derivation."
    )


def test_the_pending_ceiling_is_the_clients_own_and_is_asked_once_per_client():
    """U7: one client at a hundred thousand subscriptions is not bounded by the pilot's 500.

    Two properties, and the second is what makes the first affordable. The ceiling the store
    compares against is whatever the resolver answered for that subject's client -- so a raised
    quota raises the ceiling, and a client's own backlog cannot be refused on another client's
    behalf. And the resolver is consulted once per CLIENT per pass rather than once per subject: a
    bad morning is tens of thousands of subjects across a handful of clients, and the answer behind
    it is a cached account read, which is cheap but not free.

    The resolver is a CALLABLE rather than an import for a reason this suite pins one test below:
    the answer lives in `notification_auth`, and the exact-import set is what keeps this module
    light enough for CI to run it in its own process.

    **U9 moved the COUNT the ceiling is compared against.** It was an aggregation query per mint --
    most of dev's first ~200-250 ms per emit, and at sixty-four workers sixty-four concurrent
    aggregations against one collection to re-derive a figure this process is the only writer of.
    It is now one count per destination per pass, kept in step by `_PendingLedger`, so the
    assertion below is that the store was asked ONCE and that the ledger refused the rest.
    """
    client = _install()
    try:
        _live(client)
        asked: list[str] = []
        seen: list[int] = []
        real = notification_store._count_destination_deliveries

        def _counted(destination_id: str, *, state: str, ceiling: int) -> int:
            seen.append(ceiling)
            # One under the ceiling, so the FIRST emit proceeds and the rest must not.
            return ceiling - 1

        notification_store._count_destination_deliveries = _counted  # type: ignore[assignment]
        subjects = [_subject(journey_ref=f"DatedServiceJourney:{6000 + i}") for i in range(3)]
        report = _emit(
            subjects,
            now=NOW,
            workers=1,
            pending_quota=lambda client_id: (asked.append(client_id), 9_999)[1],
        )
        assert seen == [9_999], (
            f"the destination was counted {len(seen)} times for three mints; U9 counts once per "
            "pass and keeps its own figure in step"
        )
        assert report.created == 1 and report.quota == 2, report
        assert asked == [subjects[0].client_id], "asked once per client, not once per subject"

        # And with no resolver the module constant stands, which is what every caller that does not
        # care about quotas gets.
        seen.clear()
        _emit([_subject(journey_ref="DatedServiceJourney:6100")], now=NOW, workers=1)
        assert seen == [notification_store.MAX_PENDING_EVENTS_PER_DESTINATION], (
            "with no resolver the pass resolves the store's own constant"
        )
    finally:
        notification_store._count_destination_deliveries = real  # type: ignore[assignment]
        _uninstall()


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
    # pass budget, stdlib-only (`threading`, `time.monotonic`). `threading` is back since U9: the
    # pass has its own locks now -- the outcome tally and the pending ledger the sixty-four workers
    # share, and the module lock that refuses a second pass over them.
    assert imports == {"logging", "notification_store", "reserved_budget", "threading"}, imports


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
