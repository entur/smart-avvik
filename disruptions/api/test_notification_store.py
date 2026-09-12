"""Checks for notification_store.py, in two halves.

**The emulator-free half always runs**, in ci.yml's ordinary `test_*.py` loop, which has neither
Java nor the firebase CLI. It exercises the transaction callables against a fake client that runs
the REAL bodies and applies their writes -- so the create, replace, delete, emit, claim and send
logic, the quota arithmetic, the expiry predicates, the tenant checks and the static index-coverage
and configuration guards are checked on every CI run.

**The emulator-backed half runs only when `FIRESTORE_EMULATOR_HOST` is set**, from ci.yml's second
step (`firebase emulators:exec --project demo-kix --only firestore`). When it is unset this file
prints a conspicuous SKIPPED line naming the count and the variable and exits 0. It is deliberately
loud: a suite in a directory no CI loop enters, or a runner placed mid-file, is this repo's most
expensive recurring bug -- it prints a passing count while running nothing -- so
`test_the_emulator_step_is_wired_in_ci` greps ci.yml for that step, and deleting it fails this suite
rather than quietly halving it.

**WHAT THE EMULATOR DOES NOT ESTABLISH.** Firestore's emulator "uses a simple locking approach and
doesn't attempt to" mirror the production concurrency modes, it does not enforce production time or
size limits, and it tracks NO composite indexes -- so a query missing its production index simply
succeeds there. Therefore:

* **Atomicity is argued from the transaction contract, never claimed from these tests.** The
  concurrency checks below are useful for shape and idempotency: they prove the bodies converge on
  one committed outcome under the emulator's locking, which is the same code path production runs.
  They are not evidence about contention.
* **Index coverage is a STATIC check** (`test_every_query_spec_has_a_checked_in_index` and its
  reverse against `terraform/firestore.tf`). No runtime test can stand in for it, in either
  direction: a query missing its index passes here and fails in production, and an index declared
  but never applied fails in exactly the same way.
"""

from __future__ import annotations

import inspect
import json
import os
import pathlib
import re
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import notification_store

REPO = pathlib.Path(__file__).resolve().parents[2]
MODULE_SOURCE = pathlib.Path(notification_store.__file__).read_text()
INDEXES = json.loads((REPO / "firestore.indexes.json").read_text())
FIRESTORE_TF = (REPO / "terraform" / "firestore.tf").read_text()
CI_YML = (REPO / ".github" / "workflows" / "ci.yml").read_text()
TERRAFORM_YML = (REPO / ".github" / "workflows" / "terraform.yml").read_text()
HELM_VALUES = (REPO / "helm" / "kix-avvik" / "values.yaml").read_text()
TF_VARIABLES = (REPO / "terraform" / "variables.tf").read_text()
VARSEL_SOURCE = (pathlib.Path(__file__).parent / "varsel.py").read_text()


def _uncommented(text: str, marker: str = "#") -> str:
    """The code half of a commented file, for the checks that must NOT match a comment.

    Every header in `terraform/firestore.tf` explains what it deliberately does not do -- it names
    `google-beta`, `index_config {}`, `google_project_iam_member` and `roles/datastore.user` in
    order to refuse them -- so a negative grep over the raw text matches the explanation and passes
    (or fails) for the wrong reason. Same rule `test_rushhistorikk` applies to its SQL greps.
    """
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith(marker)
    )


FIRESTORE_TF_CODE = _uncommented(FIRESTORE_TF)

EMULATOR_HOST = os.environ.get("FIRESTORE_EMULATOR_HOST")
EMULATOR_PROJECT = "demo-kix"

NOW = datetime(2026, 9, 8, 6, 30, tzinfo=UTC)
SID = "sub-1"
CLIENT = "client-a"
DEST = "dest-a"
TRAIN = notification_store.TrainRef(
    operator="VYG",
    line="L1",
    journey_ref="VYG:DatedServiceJourney:1234",
    call_date="2026-09-08",
    stop_place="NSR:StopPlace:337",
    stop_order=3,
)


# ---- Fixtures ---------------------------------------------------------------------------------


def _spec(**over: Any) -> notification_store.SubscriptionSpec:
    base: dict[str, Any] = {
        "subscription_id": SID,
        "client_id": CLIENT,
        "destination_id": DEST,
        "from_stop": "NSR:StopPlace:337",
        "to_stop": "NSR:StopPlace:418",
        "begin": "06:45",
        "end": "08:30",
        "days": (0, 1, 2, 3, 4),
        "external_ref": "commuter-42",
        "test": False,
    }
    base.update(over)
    return notification_store.SubscriptionSpec(**base)


def _live_doc(now: datetime = NOW, **over: Any) -> dict[str, Any]:
    """A stored subscription built by the REAL writer.

    Never a hand-written dict: a fixture that does not come through `_new_subscription_doc` hides
    exactly the class of defect these checks exist to catch -- the /rushhistorikk render stub
    modelled a non-pushing baseline as null where the code wrote "green", and the marker bug it hid
    shipped.
    """
    return notification_store._new_subscription_doc(_spec(**over), now)


def _emit(**over: Any) -> notification_store.EmitRequest:
    base: dict[str, Any] = {
        "subscription_id": SID,
        "client_id": CLIENT,
        "destination_id": DEST,
        "subscription_version": 1,
        "train": TRAIN,
        "phase": notification_store.PHASE_PREDICTED,
        "situations": ("ENT:SituationNumber:1",),
        "payload": {"notification": {"title": "Tog L1 07:18 er innstilt", "body": "Oslo S -> Ski"}},
        "valid_until": NOW + timedelta(minutes=60),
        "external_ref": "commuter-42",
        "test": False,
    }
    base.update(over)
    return notification_store.EmitRequest(**base)


# ---- A fake client that runs the real bodies ---------------------------------------------------


class _Snap:
    def __init__(self, path: str, data: dict[str, Any] | None, client: Any = None) -> None:
        self._path = path
        self._data = data
        self._client = client

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
        self.client = client
        self.path = path

    def get(self, transaction: Any = None, timeout: float | None = None) -> _Snap:
        # `timeout` is accepted and ASSERTED rather than ignored: every read in the module carries
        # its own Firestore deadline (KTD6), and a fake that silently swallowed the kwarg would let
        # a read lose its deadline with nothing failing -- which is how an abandoned worker thread
        # comes to delay pod termination.
        assert timeout == notification_store._CALL_DEADLINE_S, f"read without a deadline: {self.path}"
        if transaction is not None:
            transaction.ops.append(("read", self.path))
        return _Snap(self.path, self.client.docs.get(self.path), self.client)


class _Collection:
    def __init__(self, client: Any, name: str) -> None:
        self.client = client
        self.name = name

    def document(self, doc_id: str) -> _Ref:
        return _Ref(self.client, f"{self.name}/{doc_id}")


class _Batch:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._deletes: list[str] = []

    def delete(self, ref: _Ref) -> None:
        self._deletes.append(ref.path)

    def commit(self) -> None:
        for path in self._deletes:
            self.client.docs.pop(path, None)
        self.client.batch_deletes.extend(self._deletes)
        self._deletes = []


class _Page:
    """What the fake `_query` returns: a page of snapshots, iterable and streamable."""

    def __init__(self, client: Any, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self.client = client
        self.rows = rows

    def stream(self, timeout: float | None = None) -> list[_Snap]:
        assert timeout == notification_store._CALL_DEADLINE_S, "query stream without a deadline"
        return [_Snap(path, data, self.client) for path, data in self.rows]


class _Txn:
    """Records every operation in order, so reads-before-writes is checkable, not assumed."""

    def __init__(self) -> None:
        self.ops: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str, dict[str, Any] | None]] = []

    def set(self, ref: _Ref, data: dict[str, Any]) -> None:
        self.ops.append(("set", ref.path))
        self.writes.append(("set", ref.path, dict(data)))

    def update(self, ref: _Ref, data: dict[str, Any]) -> None:
        self.ops.append(("update", ref.path))
        self.writes.append(("update", ref.path, dict(data)))

    def delete(self, ref: _Ref) -> None:
        self.ops.append(("delete", ref.path))
        self.writes.append(("delete", ref.path, None))

    def get(self, query: Any, timeout: float | None = None) -> list[_Snap]:
        assert timeout == notification_store._CALL_DEADLINE_S, "in-transaction query without a deadline"
        self.ops.append(("read_query", getattr(query, "name", "page")))
        return list(query.stream(timeout=timeout))


class _FakeClient:
    def __init__(self, docs: dict[str, Any] | None = None) -> None:
        self.docs: dict[str, dict[str, Any]] = dict(docs or {})
        self.pages: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self.query_calls: list[tuple[str, dict[str, Any], dict[str, Any], int]] = []
        self.transactions: list[_Txn] = []
        self.batch_deletes: list[str] = []
        self.fail_with: BaseException | None = None

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)

    def batch(self) -> _Batch:
        return _Batch(self)

    def put(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self.docs[f"{collection}/{doc_id}"] = dict(data)

    def doc(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        stored = self.docs.get(f"{collection}/{doc_id}")
        return dict(stored) if stored is not None else None

    def page(self, spec_name: str, collection: str, rows: list[dict[str, Any]]) -> None:
        self.pages[spec_name] = [
            (f"{collection}/{row.get('subscription_id') or row.get('delivery_id') or i}", row)
            for i, row in enumerate(rows)
        ]


_fake: _FakeClient | None = None
_saved: dict[str, Any] = {}


def _install(client: _FakeClient) -> None:
    """Point notification_store at the fake, running its real bodies and applying their writes.

    `_run_transaction` runs each body ONCE here. The "a body may run more than once" property is a
    check of its own (`test_a_transaction_body_may_run_twice_...`) rather than something this helper
    simulates -- simulating it would make every other check pay for the retry and would still not
    be the client's retry.
    """
    global _fake
    _fake = client
    # ONLY on the first install, and this is not defensiveness -- it is a bug that shipped. A second
    # `_install` in one test (two `_emitted()` fixtures, say) used to overwrite `_saved` with the
    # ALREADY-PATCHED functions, so `_uninstall` then "restored" the fakes permanently and every
    # later test in the process ran against them. The hermetic half cannot detect that: those tests
    # want the fake. It surfaced only in the emulator half, as a fake transaction runner inside
    # `emulator_test_a_withdrawal_is_minted_...`, which is the argument for running both modes.
    if not _saved:
        _saved.update(
            {
                "client": notification_store._firestore_client,
                "run": notification_store._run_transaction,
                "query": notification_store._query,
            }
        )
    notification_store._firestore_client = lambda: client  # type: ignore[assignment]

    def _run(body: Any) -> Any:
        if client.fail_with is not None:
            raise client.fail_with
        txn = _Txn()
        client.transactions.append(txn)
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
        client.query_calls.append((name, dict(equality or {}), dict(inequality or {}), limit))
        rows = client.pages.get(name, [])
        # Positional, as Firestore's own cursor is: everything strictly after the snapshot in the
        # page's ordering. `pending_deliveries` pages past a throttled destination this way.
        if start_after is not None:
            paths = [path for path, _ in rows]
            if start_after._path in paths:
                rows = rows[paths.index(start_after._path) + 1 :]
        return _Page(client, rows[:limit])

    notification_store._run_transaction = _run  # type: ignore[assignment]
    notification_store._query = _query  # type: ignore[assignment]
    notification_store.configure(client=client, clock=lambda: NOW)


def _uninstall() -> None:
    global _fake
    if _saved:
        notification_store._firestore_client = _saved["client"]  # type: ignore[assignment]
        notification_store._run_transaction = _saved["run"]  # type: ignore[assignment]
        notification_store._query = _saved["query"]  # type: ignore[assignment]
        _saved.clear()
    notification_store.reset_for_tests()
    _fake = None


class _NoClockNoLog:
    """Make a clock read or a log call inside a transaction body raise, rather than merely differ.

    Comparing two runs of a body is not enough on its own: two `datetime.now()` calls can land in
    the same microsecond, and a log line does not change what is written at all. So the clock raises
    and every attribute of the logger raises, and the body has to be pure to get through.
    """

    def __enter__(self) -> Self:
        self._clock = notification_store._clock
        self._logger = notification_store.logger

        def _boom() -> datetime:
            raise AssertionError("a transaction body must not read the clock")

        class _Boom:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"a transaction body must not call logger.{name}")

        notification_store._clock = _boom  # type: ignore[assignment]
        notification_store.logger = _Boom()  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        notification_store._clock = self._clock  # type: ignore[assignment]
        notification_store.logger = self._logger  # type: ignore[assignment]


def _no_read_after_write(txn: _Txn) -> bool:
    seen_write = False
    for kind, _path in txn.ops:
        if kind in ("set", "update", "delete"):
            seen_write = True
        elif seen_write:
            return False
    return True


def setup_function() -> None:
    _uninstall()


def teardown_function() -> None:
    _uninstall()


# ---- Scenario 1: a transaction body may run more than once ------------------------------------


def test_a_transaction_body_may_run_twice_and_writes_the_same_thing():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    refs = notification_store._Refs(client)
    request = _emit()
    seed = notification_store.revision_seed(NOW)
    with _NoClockNoLog():
        first, second = _Txn(), _Txn()
        a = notification_store._emit_body(first, refs, request=request, now=NOW, seed=seed)
        b = notification_store._emit_body(second, refs, request=request, now=NOW, seed=seed)
    assert a == b, "the same reads must produce the same result"
    assert first.writes == second.writes, "the same reads must produce the same writes"
    assert a.status == "created"
    # The event id is what the receiver dedupes on; an irreproducible one turns a retried body into
    # a second notification about the same train.
    assert a.event_id == b.event_id


def test_every_transaction_body_reads_before_it_writes():
    """Every write path, because the rule has to hold in each of them and not on average."""
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    refs = notification_store._Refs(client)
    seed = notification_store.revision_seed(NOW)
    bodies = [
        lambda t: notification_store._create_body(
            t, refs, _Page(client, []), spec=_spec(subscription_id="sub-2"), now=NOW
        ),
        lambda t: notification_store._replace_body(
            t, refs, spec=_spec(begin="07:00"), expected_version=1, now=NOW
        ),
        lambda t: notification_store._emit_body(t, refs, request=_emit(), now=NOW, seed=seed),
        lambda t: notification_store._delete_body(
            t, refs, subscription_id=SID, client_id=CLIENT, now=NOW
        ),
        # The two the list did not name until 2026-09-09. `_touch_body` backs the client keep-alive
        # endpoint and `_servability_body` is called by the poller's own servability sweep on every
        # due subscription of every cycle -- so both are as reachable as anything above them, and
        # neither was covered by ANY of this suite's purity instruments.
        lambda t: notification_store._touch_body(
            t, refs, subscription_id=SID, client_id=CLIENT, now=NOW
        ),
        lambda t: notification_store._servability_body(
            t, refs, subscription_id=SID, servable=False, expected_version=1, now=NOW
        ),
    ]
    for body in bodies:
        txn = _Txn()
        with _NoClockNoLog():
            body(txn)
        assert txn.writes, "each of these bodies is expected to write"
        assert _no_read_after_write(txn), f"a read follows a write: {txn.ops}"


def test_no_body_that_increments_a_counter_is_retry_sensitive():
    """Every read-modify-write in this module reads the value it increments INSIDE the body.

    This is the second shape of the "a body may run more than once" defect, and it is quieter than a
    clock read: a counter taken from a value captured OUTSIDE the body, or accumulated into an outer
    variable, double-counts on a retry -- and a retry is exactly what contention produces. It would
    show up as an alert cap spending two of a traveller's four slots for one notification, or a
    revision jumping by two, neither of which fails anything.

    Ten bodies rewrite something derived (`version`, `attempts`, `revision`,
    `below_threshold_cycles`, `capped_count`, `cleanup.attempts`, the Oslo-day counter, and R5's two
    horizons). Each is run twice against one fake transaction with a raising clock and a raising
    logger; identical writes is the proof.

    The last two -- `_touch_body` and `_servability_body` -- increment nothing today, and that is
    why they were missing until 2026-09-09 rather than why they should be. This instrument is what
    catches the EDIT that adds a clock read or a counter to a body, so a body only inside it once it
    already has one is a check that arrives after the defect it exists to prevent.
    """
    seed = notification_store.revision_seed(NOW)
    for build in (
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._replace_body(
                t, r, spec=_spec(begin="07:00"), expected_version=1, now=NOW
            ),
        ),
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._delete_body(
                t, r, subscription_id=SID, client_id=CLIENT, now=NOW
            ),
        ),
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._withdraw_body(t, r, request=_emit(), now=NOW, seed=seed),
        ),
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._claim_body(t, r, delivery_id=_DELIVERY, now=NOW),
        ),
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._sent_body(t, r, delivery_id=_DELIVERY, now=NOW),
        ),
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._failed_body(
                t,
                r,
                delivery_id=_DELIVERY,
                error="reset",
                retryable=True,
                retry_after_s=30,
                terminal_state="failed",
                now=NOW,
            ),
        ),
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._terminal_body(
                t, r, delivery_id=_DELIVERY, state="expired", reason="validity", now=NOW
            ),
        ),
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._clear_below_threshold_body(
                t, r, alert_id=_emit().alert_id, now=NOW
            ),
        ),
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._finish_cleanup_body(
                t,
                r,
                subscription_id=SID,
                now=NOW,
                done=True,
                deleted={"events": 2, "deliveries": 2, "subjects": 1},
            ),
        ),
        # Neither of these increments anything TODAY -- both are deterministic patches over
        # `stored` and `now` -- and that is exactly why they belong here: the instrument is what
        # would catch the edit that adds a clock read or a counter to one of them, and until
        # 2026-09-09 the two were named by no purity check in this file at all. `_touch_body`
        # rewrites both of R5's horizons, which is the shape a counter takes here.
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._touch_body(
                t, r, subscription_id=SID, client_id=CLIENT, now=NOW
            ),
        ),
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._servability_body(
                t, r, subscription_id=SID, servable=True, expected_version=1, now=NOW
            ),
        ),
    ):
        client = _counting_fixture()
        refs, body = build(client)
        first, second = _Txn(), _Txn()
        with _NoClockNoLog():
            a = body(first, refs)
            b = body(second, refs)
        # Non-empty FIRST: a body that returns before writing passes an equality check vacuously,
        # and every one of these seven is set up to reach its write.
        assert first.writes, f"{body} wrote nothing -- the fixture does not reach its write path"
        assert first.writes == second.writes, f"retry-sensitive body: {first.writes}"
        assert a == b
        assert _no_read_after_write(first), f"a read follows a write: {first.ops}"


_DELIVERY = "delivery-1"


def _counting_fixture() -> _FakeClient:
    """A store where every counter is already non-zero, so a double increment is visible."""
    client = _FakeClient()
    request = _emit()
    client.put(
        notification_store.SUBSCRIPTIONS,
        SID,
        dict(
            _live_doc(),
            version=1,
            daily_deliveries={notification_store.oslo_day(NOW): 1},
            delivered={"ENT:SituationNumber:0": NOW + timedelta(hours=10)},
            cleanup={"state": "pending", "attempts": 1, "deleted_events": 1},
        ),
    )
    client.put(
        notification_store.SUBJECTS,
        request.alert_id,
        {
            "alert_id": request.alert_id,
            "subscription_id": SID,
            "client_id": CLIENT,
            "destination_id": DEST,
            "phase": notification_store.PHASE_PREDICTED,
            "revision": 41,
            "below_threshold_cycles": 1,
            "below_threshold_at": NOW - timedelta(seconds=60),
            "last_event_id": "old-event",
            "pending_delivery_id": _DELIVERY,
            "delivered_revision": None,
            notification_store.TTL_FIELD: NOW + timedelta(days=7),
        },
    )
    client.put(
        notification_store.DELIVERIES,
        _DELIVERY,
        {
            "delivery_id": _DELIVERY,
            "event_id": "old-event",
            "alert_id": request.alert_id,
            "subscription_id": SID,
            "subscription_version": 1,
            "client_id": CLIENT,
            "destination_id": DEST,
            "revision": 41,
            "state": "pending",
            "attempts": 2,
            "capped_count": 1,
            "retry_not_before": NOW - timedelta(seconds=1),
            "valid_until": NOW + timedelta(minutes=30),
            "situations": ["ENT:SituationNumber:1"],
            notification_store.TTL_FIELD: NOW + timedelta(days=7),
        },
    )
    return client


def test_the_send_and_claim_bodies_also_read_before_they_write():
    client, request = _emitted()
    delivery_id = client.docs[f"{notification_store.SUBJECTS}/{request.alert_id}"][
        "pending_delivery_id"
    ]
    refs = notification_store._Refs(client)
    for body in (
        lambda t: notification_store._claim_body(t, refs, delivery_id=delivery_id, now=NOW),
        lambda t: notification_store._sent_body(t, refs, delivery_id=delivery_id, now=NOW),
    ):
        txn = _Txn()
        with _NoClockNoLog():
            body(txn)
        assert _no_read_after_write(txn), f"a read follows a write: {txn.ops}"


def _emitted(**over: Any) -> tuple[_FakeClient, notification_store.EmitRequest]:
    """A fake store holding one live subscription and one committed pending event."""
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    request = _emit(**over)
    commit = notification_store.commit_event(request, now=NOW)
    assert commit.status == "created"
    return client, request


# ---- Scenario 9: ceilings, refused before any write --------------------------------------------


def test_a_client_at_its_ceiling_is_refused_before_any_write():
    client = _FakeClient()
    refs = notification_store._Refs(client)
    rows = [
        (f"{notification_store.SUBSCRIPTIONS}/s{i}", _live_doc(subscription_id=f"s{i}"))
        for i in range(notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT)
    ]
    txn = _Txn()
    try:
        with _NoClockNoLog():
            notification_store._create_body(txn, refs, _Page(client, rows), spec=_spec(), now=NOW)
    except notification_store.QuotaExceeded:
        pass
    else:
        raise AssertionError("a create past the ceiling must be refused")
    assert txn.writes == [], "the refusal must precede every write"


def test_the_ceiling_counts_only_live_subscriptions():
    """A client is not locked out by rows TTL has not caught up with.

    This is the whole reason the count is a query rather than a counter document: an expired or
    tombstoned subscription is still `status == "active"` (or still present) until TTL deletes it up
    to ~24 h later, and a counter would keep it in the total forever.
    """
    client = _FakeClient()
    refs = notification_store._Refs(client)
    dead = _live_doc(subscription_id="old", now=NOW - timedelta(days=400))
    stale = [
        (f"{notification_store.SUBSCRIPTIONS}/d{i}", dict(dead, subscription_id=f"d{i}"))
        for i in range(notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT)
    ]
    txn = _Txn()
    with _NoClockNoLog():
        outcome, _doc = notification_store._create_body(
            txn, refs, _Page(client, stale), spec=_spec(), now=NOW
        )
    assert outcome == "created"


def test_the_create_ceiling_query_is_bounded_by_the_ceiling():
    client = _FakeClient()
    _install(client)
    notification_store.create_subscription(_spec(), now=NOW)
    name, equality, _ineq, limit = client.query_calls[0]
    assert name == "subscription_count"
    assert equality == {"client_id": CLIENT, "status": "active"}
    assert limit == notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT


def test_the_create_count_sheds_dead_rows_before_live_ones():
    """The count is bounded AT the ceiling, so which rows the page holds IS the count.

    Liveness is tested in Python, so an expired-but-not-yet-deleted row is returned by the query
    and discarded after the fact. Counted through the listing's `created_at ASCENDING` ordering
    (as it was until 2026-09-10), the page filled with the OLDEST rows first -- exactly where the
    rows TTL has not caught up with sit -- so `ceiling` dead rows hid every live one, the count read
    zero, and a client past its ceiling was admitted another subscription (R21). Ordered
    `expires_at DESCENDING`, every live row precedes every dead one, so a page holding fewer than
    `ceiling` live rows has counted them all. The fake cannot order, so the direction is pinned on
    the spec the way `test_the_load_query_sheds_dead_rows_before_live_ones` pins its sibling.
    """
    spec = next(s for s in notification_store.QUERY_SPECS if s.name == "subscription_count")
    assert spec.order_by == (("expires_at", "DESCENDING"),)
    assert spec.equality == ("client_id", "status")
    listing = next(s for s in notification_store.QUERY_SPECS if s.name == "subscriptions")
    assert listing.order_by == (("created_at", "ASCENDING"),), "the published list order is unchanged"
    # And the page the body counts from really is the one the store asks for: a page of `ceiling`
    # rows, live ones first, refuses; the same rows dead-first would have admitted.
    client = _FakeClient()
    refs = notification_store._Refs(client)
    ceiling = notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT
    dead = _live_doc(subscription_id="old", now=NOW - timedelta(days=400))
    live_first = [
        (f"{notification_store.SUBSCRIPTIONS}/s{i}", _live_doc(subscription_id=f"s{i}"))
        for i in range(ceiling)
    ] + [(f"{notification_store.SUBSCRIPTIONS}/d0", dict(dead, subscription_id="d0"))]
    txn = _Txn()
    try:
        with _NoClockNoLog():
            notification_store._create_body(
                txn, refs, _Page(client, live_first[:ceiling]), spec=_spec(), now=NOW
            )
    except notification_store.QuotaExceeded:
        pass
    else:
        raise AssertionError("a page that is live rows up to the ceiling must refuse")


def test_the_per_cycle_load_is_bounded_by_the_ceiling_and_reports_truncation():
    client = _FakeClient()
    _install(client)
    rows = [_live_doc(subscription_id=f"s{i}") for i in range(5)]
    client.page("load_active_subscriptions", notification_store.SUBSCRIPTIONS, rows)
    loaded = notification_store.active_subscriptions(limit=3, now=NOW)
    assert len(loaded.subscriptions) == 3
    assert loaded.truncated is True
    _name, _eq, _ineq, limit = client.query_calls[-1]
    assert limit == 3
    # A request for more than R21's ceiling is clamped to it, not honoured.
    notification_store.active_subscriptions(limit=10**6, now=NOW)
    assert client.query_calls[-1][3] == notification_store.MAX_SUBSCRIPTIONS_PER_CYCLE


def test_the_cycle_ceiling_does_not_exceed_varsels_own():
    """The same read is bounded one layer out, and the two must not drift apart."""
    match = re.search(r"_MAX_WEBHOOK_RECIPIENTS\s*=\s*(\d+)", VARSEL_SOURCE)
    assert match, "varsel._MAX_WEBHOOK_RECIPIENTS not found -- was it renamed?"
    assert notification_store.MAX_SUBSCRIPTIONS_PER_CYCLE <= int(match.group(1))


def test_a_destination_past_the_pending_event_ceiling_cannot_emit():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    client.page(
        "destination_deliveries_in_state",
        notification_store.DELIVERIES,
        [
            {"delivery_id": f"d{i}", "state": "pending"}
            for i in range(notification_store.MAX_PENDING_EVENTS_PER_DESTINATION)
        ],
    )
    before = dict(client.docs)
    try:
        notification_store.commit_event(_emit(), now=NOW)
    except notification_store.QuotaExceeded:
        pass
    else:
        raise AssertionError("emitting past the pending ceiling must be refused")
    assert client.docs == before, "the refusal must precede every write"


def test_the_delivery_set_is_bounded_per_cycle_and_per_destination():
    client = _FakeClient()
    _install(client)
    rows = []
    for i in range(notification_store.MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE + 5):
        rows.append(
            {
                "delivery_id": f"d{i:03d}",
                "event_id": f"d{i:03d}",
                "alert_id": "a",
                "subscription_id": SID,
                "subscription_version": 1,
                "client_id": CLIENT,
                "destination_id": DEST,
                "state": "pending",
                "attempts": 0,
                "retry_not_before": NOW - timedelta(seconds=1),
                "valid_until": NOW + timedelta(minutes=30 + i),
                notification_store.TTL_FIELD: NOW + timedelta(days=7),
                "situations": ["ENT:1"],
            }
        )
    client.page("pending_deliveries", notification_store.DELIVERIES, rows)
    pending = notification_store.pending_deliveries(now=NOW)
    assert len(pending.ready) == notification_store.MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE
    assert pending.throttled == (DEST,)
    # Soonest departure first, across the whole set.
    assert [d.valid_until for d in pending.ready] == sorted(d.valid_until for d in pending.ready)


def test_a_stuck_destinations_backlog_cannot_starve_another_clients_deliveries():
    """Two destinations, one backlog. X holds more pending rows than a page, all sorting first by
    retry order because nothing ever advances them (paused after a 410, or skipped for the rest of
    each pass after one failure); Y holds three rows behind them. Until 2026-09-10 the read was one
    page of `MAX_DELIVERY_ATTEMPTS_PER_CYCLE`, so X owned the page, the per-destination ceiling
    kept 50 of X's rows, and Y's three never entered the set on any cycle. The read now pages past
    the throttled rows up to `MAX_DELIVERY_SCAN_PER_CYCLE`."""
    client = _FakeClient()
    _install(client)

    def row(i: int, destination: str) -> dict[str, Any]:
        return {
            "delivery_id": f"del-{destination}-{i:04d}",
            "event_id": f"evt-{destination}-{i:04d}",
            "alert_id": f"alert-{destination}-{i:04d}",
            # One commuter per row, as a real backlog is: many subscriptions, one destination.
            "subscription_id": f"sub-{destination}-{i:04d}",
            "subscription_version": 1,
            "client_id": f"client-{destination}",
            "destination_id": destination,
            "state": "pending",
            "attempts": 0,
            # X's rows are OLDER in retry order, so they fill the first page.
            "retry_not_before": NOW - timedelta(minutes=30 if destination == "X" else 1),
            "valid_until": NOW + timedelta(minutes=30 + (i % 20)),
            notification_store.TTL_FIELD: NOW + timedelta(days=7),
            "situations": ["ENT:1"],
        }

    backlog = notification_store.MAX_DELIVERY_ATTEMPTS_PER_CYCLE + 40
    rows = [row(i, "X") for i in range(backlog)] + [row(i, "Y") for i in range(3)]
    client.page("pending_deliveries", notification_store.DELIVERIES, rows)
    pending = notification_store.pending_deliveries(now=NOW)
    per_destination: dict[str, int] = {}
    for delivery in pending.ready:
        per_destination[delivery.destination_id] = per_destination.get(delivery.destination_id, 0) + 1
    assert per_destination == {
        "X": notification_store.MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE,
        "Y": 3,
    }, per_destination
    assert pending.throttled == ("X",)
    assert len({d.delivery_id for d in pending.ready}) == len(pending.ready), "no row twice"
    assert not pending.truncated, "the collection was drained, so nothing deliverable was left behind"
    # Two pages were read: the full first one and the short one that drained the collection.
    pages = [c for c in client.query_calls if c[0] == "pending_deliveries"]
    assert len(pages) == 2, pages
    # And the scan bound holds: a backlog past it leaves the read truncated, never unbounded.
    client.page(
        "pending_deliveries",
        notification_store.DELIVERIES,
        [row(i, "X") for i in range(notification_store.MAX_DELIVERY_SCAN_PER_CYCLE + 5)],
    )
    client.query_calls.clear()
    bounded = notification_store.pending_deliveries(now=NOW)
    assert bounded.truncated and bounded.throttled == ("X",)
    assert len([c for c in client.query_calls if c[0] == "pending_deliveries"]) == 5


# ---- Scenario 4: expired but not yet TTL-deleted ----------------------------------------------


def test_an_unreadable_or_missing_expiry_reads_as_expired():
    """The strict direction, because R19 publishes what the code does.

    A document that outlives its published window because its timestamp could not be parsed makes
    the published retention text false, and the failure is invisible. A dropped document is visible.
    """
    assert notification_store._not_expired({notification_store.TTL_FIELD: NOW + timedelta(days=1)}, NOW)
    assert not notification_store._not_expired({}, NOW)
    assert not notification_store._not_expired({notification_store.TTL_FIELD: None}, NOW)
    assert not notification_store._not_expired({notification_store.TTL_FIELD: "not a date"}, NOW)
    assert not notification_store._not_expired({notification_store.TTL_FIELD: NOW}, NOW)


def test_an_expired_subscription_is_neither_live_nor_loaded():
    old = _live_doc(now=NOW - timedelta(days=400))
    assert notification_store.subscription_is_live(old, NOW - timedelta(days=399)) is True
    assert notification_store.subscription_is_live(old, NOW) is False
    # Inactivity alone expires it even inside the maximum lifetime.
    idle = _live_doc(now=NOW - timedelta(days=100))
    assert notification_store.subscription_is_live(idle, NOW) is False
    # ... and a stale derived expires_at cannot rescue it: the horizons are tested from their own
    # fields, so a document written by another binary with a wrong minimum still expires.
    lying = dict(idle)
    lying[notification_store.TTL_FIELD] = NOW + timedelta(days=365)
    assert notification_store.subscription_is_live(lying, NOW) is False
    # A tombstone is never live, whatever its horizons say.
    assert notification_store.subscription_is_live(dict(_live_doc(), status="deleted"), NOW) is False

    client = _FakeClient()
    _install(client)
    client.page(
        "load_active_subscriptions",
        notification_store.SUBSCRIPTIONS,
        [_live_doc(subscription_id="live"), dict(idle, subscription_id="idle")],
    )
    loaded = notification_store.active_subscriptions(now=NOW)
    assert [s.subscription_id for s in loaded.subscriptions] == ["live"]
    assert loaded.expired == 1


def test_an_expired_subscription_cannot_be_replaced_touched_or_emitted_for():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc(now=NOW - timedelta(days=400)))
    _install(client)
    for call in (
        lambda: notification_store.replace_subscription(_spec(begin="07:00"), now=NOW),
        lambda: notification_store.touch_subscription(SID, client_id=CLIENT, now=NOW),
    ):
        try:
            call()
        except notification_store.NotFound:
            pass
        else:
            raise AssertionError("an expired subscription must not be writable")
    commit = notification_store.commit_event(_emit(), now=NOW)
    assert commit.status == "obsolete" and commit.reason == "expired"
    assert notification_store.get_subscription(SID, client_id=CLIENT, now=NOW) is None


def test_an_expired_alert_subject_is_read_as_absent():
    """A stale subject would clamp the revision to a stale value and read a stale PHASE.

    The second is the dangerous one: a `confirmed` from three days ago would suppress today's
    forecast about a same-numbered dated train, which is the one thing R14's third rule is for.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    request = _emit()
    client.put(
        notification_store.SUBJECTS,
        request.alert_id,
        {
            "alert_id": request.alert_id,
            "phase": notification_store.PHASE_CONFIRMED,
            "revision": 7,
            notification_store.TTL_FIELD: NOW - timedelta(days=1),
        },
    )
    _install(client)
    commit = notification_store.commit_event(request, now=NOW)
    assert commit.status == "created"
    assert commit.event_type == notification_store.EVENT_CREATED
    assert notification_store.get_subject(request.alert_id, now=NOW) is not None
    stale = dict(client.doc(notification_store.SUBJECTS, request.alert_id) or {})
    stale[notification_store.TTL_FIELD] = NOW - timedelta(days=1)
    client.put(notification_store.SUBJECTS, request.alert_id, stale)
    assert notification_store.get_subject(request.alert_id, now=NOW) is None


def test_an_expired_or_stale_delivery_is_neither_ready_nor_claimable():
    client = _FakeClient()
    _install(client)
    base = {
        "event_id": "d1",
        "alert_id": "a",
        "subscription_id": SID,
        "subscription_version": 1,
        "client_id": CLIENT,
        "destination_id": DEST,
        "state": "pending",
        "attempts": 0,
        "retry_not_before": NOW - timedelta(seconds=1),
        "valid_until": NOW + timedelta(minutes=30),
        notification_store.TTL_FIELD: NOW + timedelta(days=7),
        "situations": ["ENT:1"],
    }
    rows = [
        dict(base, delivery_id="ready"),
        # Past its validity: R9 says expired rather than failed, so it comes back as `stale` for
        # mark_delivery_expired instead of being dropped and left pending forever.
        dict(base, delivery_id="stale", valid_until=NOW - timedelta(minutes=1)),
        # Past its retention: TTL has not caught up, and every read path tests the field itself.
        dict(base, delivery_id="retired", **{notification_store.TTL_FIELD: NOW - timedelta(hours=1)}),
        # Backed off: excluded by the reader as well as by the query's range filter.
        dict(base, delivery_id="backedoff", retry_not_before=NOW + timedelta(minutes=2)),
    ]
    client.page("pending_deliveries", notification_store.DELIVERIES, rows)
    pending = notification_store.pending_deliveries(now=NOW)
    assert [d.delivery_id for d in pending.ready] == ["ready"]
    assert pending.stale == ("stale",)

    for row in rows:
        client.put(notification_store.DELIVERIES, row["delivery_id"], row)
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    assert notification_store.claim_delivery("retired", now=NOW).status == "gone"
    expired = notification_store.claim_delivery("stale", now=NOW)
    assert expired.status == "expired"
    assert client.doc(notification_store.DELIVERIES, "stale")["state"] == "expired"
    assert notification_store.claim_delivery("ready", now=NOW).status == "ready"


def test_the_delivery_query_itself_excludes_a_backed_off_record():
    """`Retry-After` has to survive a cycle boundary and a restart, so it is a QUERY filter."""
    client = _FakeClient()
    _install(client)
    notification_store.pending_deliveries(now=NOW)
    name, equality, inequality, _limit = client.query_calls[-1]
    assert name == "pending_deliveries"
    assert equality == {"state": "pending"}
    assert inequality == {"retry_not_before": NOW}
    spec = next(s for s in notification_store.QUERY_SPECS if s.name == "pending_deliveries")
    assert spec.inequality == (("retry_not_before", "<="),)


# ---- Scenario 3: a replace or delete racing an emission ---------------------------------------


def test_an_emission_against_an_obsolete_subscription_version_writes_nothing():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, dict(_live_doc(), version=2))
    _install(client)
    before = dict(client.docs)
    commit = notification_store.commit_event(_emit(subscription_version=1), now=NOW)
    assert commit.status == "obsolete" and commit.reason == "version"
    assert client.docs == before


def test_an_emission_after_a_tombstone_writes_nothing():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW)
    before = dict(client.docs)
    commit = notification_store.commit_event(_emit(), now=NOW)
    assert commit.status == "obsolete" and commit.reason == "deleted"
    assert client.docs == before
    # The version also moved, so an emission holding the pre-delete version is doubly refused.
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["version"] == 2


def test_a_replace_refuses_the_version_it_was_not_conditioned_on():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, dict(_live_doc(), version=3))
    _install(client)
    try:
        notification_store.replace_subscription(_spec(begin="07:00"), expected_version=1, now=NOW)
    except notification_store.Conflict:
        pass
    else:
        raise AssertionError("an amendment against a stale version must be refused")
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["begin"] == "06:45"
    updated = notification_store.replace_subscription(
        _spec(begin="07:00"), expected_version=3, now=NOW
    )
    assert updated.version == 4 and updated.begin == "07:00"


def test_a_claim_finds_a_delete_that_landed_after_the_pending_set_was_read():
    """R13, at the moment of dispatch rather than at the read that produced the match."""
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW)
    claim = notification_store.claim_delivery(delivery_id, now=NOW)
    assert claim.status == "cancelled" and claim.reason == "deleted"
    assert client.doc(notification_store.DELIVERIES, delivery_id)["state"] == "cancelled"


# ---- Scenario 5: tenant isolation --------------------------------------------------------------


def test_one_client_cannot_read_or_affect_anothers_records():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    other = "client-b"
    for call in (
        lambda: notification_store.get_subscription(SID, client_id=other, now=NOW),
        lambda: notification_store.replace_subscription(_spec(client_id=other), now=NOW),
        lambda: notification_store.delete_subscription(SID, client_id=other, now=NOW),
        lambda: notification_store.touch_subscription(SID, client_id=other, now=NOW),
        lambda: notification_store.create_subscription(_spec(client_id=other), now=NOW),
    ):
        try:
            call()
        except notification_store.Forbidden:
            pass
        else:
            raise AssertionError("a foreign client must not reach this record")
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["client_id"] == CLIENT
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["version"] == 1
    # An emission is an ordinary pass outcome rather than an error, so it reports `obsolete`.
    commit = notification_store.commit_event(_emit(client_id=other), now=NOW)
    assert commit.status == "obsolete" and commit.reason == "foreign"


def test_the_client_listing_is_filtered_by_the_authenticated_client():
    client = _FakeClient()
    _install(client)
    notification_store.list_subscriptions(client_id=CLIENT, now=NOW)
    _name, equality, _ineq, _limit = client.query_calls[-1]
    assert equality["client_id"] == CLIENT
    spec = next(s for s in notification_store.QUERY_SPECS if s.name == "subscriptions")
    assert "client_id" in spec.equality


def test_no_project_is_named_anywhere_in_the_module():
    """A dev credential cannot reach prd data, and the reason is structural.

    Each environment is its own GCP project with its own runtime identity, and the client takes its
    project from the ambient environment (`GOOGLE_CLOUD_PROJECT` / ADC) rather than from an argument.
    So there is no code path that can name a project at all -- which is a stronger claim than any
    runtime test could make, and the only one an emulator with a `demo-` project could not check.
    """
    assert "ent-kixavvik" not in MODULE_SOURCE
    assert "firestore.Client(database=_database)" in MODULE_SOURCE
    assert not re.search(r"firestore\.Client\([^)]*project=", MODULE_SOURCE)


# ---- Scenario 6: a store outage ----------------------------------------------------------------


def test_a_store_outage_leaves_no_partial_subscription():
    client = _FakeClient()
    _install(client)
    from google.api_core import exceptions as api_exceptions

    client.fail_with = api_exceptions.ServiceUnavailable("504 upstream")
    try:
        notification_store.create_subscription(_spec(), now=NOW)
    except api_exceptions.ServiceUnavailable as exc:
        assert notification_store.is_retryable(exc), "an outage is retryable, not terminal"
    else:
        raise AssertionError("a store outage must surface as a failure")
    assert client.docs == {}, "no partial subscription"


def test_the_store_has_no_import_time_side_effect_and_no_reverse_dependency():
    """Importing it starts no client, and it imports nothing the notification pass owns.

    The dependency runs one way on purpose: a notification event may not reach the device registry, the
    VAPID keypair or the decision-log writer (R16), and the poller reaches this module through an
    injected provider. `varsel._load_webhook_recipients` is the guard that turns a raise here into
    "this cycle has no webhook recipients" -- checked by name, because a store outage must not be
    able to unwind out of `check_and_notify` and cost the cycle its decision rows and its state save.
    """
    notification_store.reset_for_tests()
    assert notification_store.enabled() is False
    assert notification_store._firestore_client_obj is None
    for forbidden in ("import varsel", "import main", "import impact", "import decisions"):
        assert forbidden not in MODULE_SOURCE
    assert "def _load_webhook_recipients" in VARSEL_SOURCE
    guard = VARSEL_SOURCE.split("def _load_webhook_recipients", 1)[1].split("\ndef ", 1)[0]
    assert "except Exception:" in guard and "return []" in guard


def test_an_unreadable_document_is_skipped_rather_than_failing_its_collection():
    client = _FakeClient()
    _install(client)
    client.page(
        "load_active_subscriptions",
        notification_store.SUBSCRIPTIONS,
        [
            _live_doc(subscription_id="good"),
            # A shape this binary cannot read: no `days`. R20 wants it skipped, not fatal.
            {
                "subscription_id": "bad",
                "client_id": CLIENT,
                "destination_id": DEST,
                "from_stop": "NSR:StopPlace:1",
                "to_stop": "NSR:StopPlace:2",
                "begin": "06:00",
                "end": "07:00",
                "status": "active",
                "version": 1,
                "created_at": NOW,
                "lifetime_expires_at": NOW + timedelta(days=1),
                "inactivity_expires_at": NOW + timedelta(days=1),
                notification_store.TTL_FIELD: NOW + timedelta(days=1),
            },
        ],
    )
    loaded = notification_store.active_subscriptions(now=NOW)
    assert [s.subscription_id for s in loaded.subscriptions] == ["good"]
    assert loaded.skipped == 1


# ---- Scenario 7: an exhausted transaction is retryable ----------------------------------------


def test_an_exhausted_transaction_is_classified_retryable():
    from google.cloud.firestore_v1.base_transaction import (
        _EXCEED_ATTEMPTS_TEMPLATE,
        MAX_ATTEMPTS,
    )

    exhausted = ValueError(_EXCEED_ATTEMPTS_TEMPLATE.format(MAX_ATTEMPTS))
    assert notification_store.is_retryable(exhausted), (
        "a contended subject would be marked failed and never delivered"
    )


def test_the_exhausted_transaction_message_matches_the_installed_client():
    """A client upgrade that rewords it must fail a test rather than a client's delivery."""
    from google.cloud.firestore_v1.base_transaction import (
        _EXCEED_ATTEMPTS_TEMPLATE,
        MAX_ATTEMPTS,
    )

    assert _EXCEED_ATTEMPTS_TEMPLATE.startswith(notification_store._EXHAUSTED_TRANSACTION_PREFIX)
    assert notification_store._exhausted_transaction_prefix() == (
        notification_store._EXHAUSTED_TRANSACTION_PREFIX
    )
    assert MAX_ATTEMPTS == 5, "the plan and the contract both quote five attempts"


def test_our_own_errors_are_terminal_and_transport_errors_are_not():
    from google.api_core import exceptions as api_exceptions

    for terminal in (
        ValueError("days must be a non-empty subset of 0..6"),
        notification_store.Conflict("version mismatch"),
        notification_store.QuotaExceeded("ceiling"),
        notification_store.Forbidden("другой client"),
        notification_store.NotFound("gone"),
        notification_store.NotConfigured("no database"),
        api_exceptions.PermissionDenied("no datastore.user"),
        api_exceptions.InvalidArgument("bad field path"),
    ):
        assert not notification_store.is_retryable(terminal), terminal
    for retryable in (
        api_exceptions.Aborted("Too much contention"),
        api_exceptions.DeadlineExceeded("deadline"),
        api_exceptions.ServiceUnavailable("unavailable"),
        api_exceptions.ResourceExhausted("quota"),
        api_exceptions.TooManyRequests("429"),
        api_exceptions.InternalServerError("500"),
    ):
        assert notification_store.is_retryable(retryable), retryable


# ---- Current alert state: the read the notification API serves ---------------------------------------


def _alert_page(client: _FakeClient, docs: list[dict[str, Any]]) -> None:
    """Put subject documents where BOTH new query specs will find them."""
    for doc in docs:
        client.put(notification_store.SUBJECTS, doc["alert_id"], doc)
    rows = [(f"{notification_store.SUBJECTS}/{d['alert_id']}", d) for d in docs]
    client.pages["subscription_alerts"] = rows
    client.pages["client_alerts"] = rows


def _state(alert_id: str, phase: str, *, updated: datetime = NOW, **over: Any) -> dict[str, Any]:
    doc = {
        "alert_id": alert_id,
        "subscription_id": SID,
        "client_id": CLIENT,
        "destination_id": DEST,
        "revision": 5,
        "phase": phase,
        "delivered_revision": None,
        "operator": TRAIN.operator,
        "line": TRAIN.line,
        "journey_ref": TRAIN.journey_ref,
        "call_date": TRAIN.call_date,
        "stop_place": TRAIN.stop_place,
        "stop_order": TRAIN.stop_order,
        "created_at": NOW,
        "updated_at": updated,
        notification_store.TTL_FIELD: NOW + timedelta(days=notification_store.HISTORY_DAYS),
    }
    doc.update(over)
    return doc


def test_the_subject_document_the_emit_path_writes_is_readable_as_alert_state():
    """The two halves have to agree, and the writer is the authority.

    `commit_event` decides which fields a subject carries; this reads one back through the public
    accessor rather than through a fixture, so a field the writer renames cannot pass here.
    """
    client = _FakeClient()
    _install(client)
    try:
        client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
        commit = notification_store.commit_event(_emit())
        assert commit.status == "created"
        stored = client.doc(notification_store.SUBJECTS, commit.alert_id)
        _alert_page(client, [stored])
        snapshot = notification_store.subscription_alerts(SID, client_id=CLIENT)
        assert snapshot.n_active == 1
        assert snapshot.n_confirmed == 0
        assert snapshot.truncated is False
        state = snapshot.subjects[0]
        assert state.alert_id == commit.alert_id
        assert state.phase == notification_store.PHASE_PREDICTED
        assert state.revision == commit.revision
        assert state.delivered_revision is None
        # The train comes back as the same TrainRef the caller emitted, which is what makes an API
        # card and an event describe one train in one vocabulary.
        assert state.train == TRAIN
    finally:
        _uninstall()


def test_the_alert_state_read_consults_no_model_and_no_matcher():
    """A read of what is already committed, not a re-evaluation.

    Structural, because that is the claim: nothing in this module can reach the matcher, `/impact`
    or a prediction cache -- it imports none of them -- and the two functions issue exactly one
    query each through the spec helper.
    """
    for forbidden in ("import varsel", "import impact", "import main", "ML.PREDICT"):
        assert forbidden not in MODULE_SOURCE, forbidden
    client = _FakeClient()
    _install(client)
    try:
        _alert_page(client, [_state("a", notification_store.PHASE_PREDICTED)])
        client.query_calls.clear()
        notification_store.subscription_alerts(SID)
        assert [c[0] for c in client.query_calls] == ["subscription_alerts"]
        client.query_calls.clear()
        notification_store.client_alerts(CLIENT)
        assert [c[0] for c in client.query_calls] == ["client_alerts"]
    finally:
        _uninstall()


def test_a_withdrawn_subject_does_not_stand_and_is_never_served():
    """The question is what alerts STAND. A withdrawal is the retraction of one, the client's
    receiver already has the withdrawal event, and re-offering it here would invite a card for a
    train we have said is fine."""
    client = _FakeClient()
    _install(client)
    try:
        _alert_page(
            client,
            [
                _state("a", notification_store.PHASE_WITHDRAWN),
                _state("b", notification_store.PHASE_PREDICTED),
            ],
        )
        snapshot = notification_store.subscription_alerts(SID)
        assert [s.alert_id for s in snapshot.subjects] == ["b"]
        assert snapshot.n_active == 1
    finally:
        _uninstall()


def test_the_worst_alert_survives_the_cap_and_the_order_cannot_flip():
    """Phase first, then recency, then the id.

    Phase first is the one ordering mistake that would matter: a declared cancellation displaced by
    a forecast that happened to change more recently is exactly the case where the cap would hide
    the thing worth seeing. The id tie-break is what keeps two requests about unchanged data from
    returning two different lists.
    """
    client = _FakeClient()
    _install(client)
    try:
        docs = [
            _state(f"p{i}", notification_store.PHASE_PREDICTED, updated=NOW - timedelta(minutes=i))
            for i in range(notification_store.MAX_ALERTS_SERVED + 3)
        ]
        # The confirmed one is the STALEST, so only a phase-first ranking keeps it.
        docs.append(
            _state("confirmed", notification_store.PHASE_CONFIRMED, updated=NOW - timedelta(hours=6))
        )
        _alert_page(client, docs)
        snapshot = notification_store.subscription_alerts(SID)
        assert len(snapshot.subjects) == notification_store.MAX_ALERTS_SERVED
        assert snapshot.subjects[0].alert_id == "confirmed"
        # The counts are over EVERYTHING read, so the cap hides nothing -- it only stops enumerating.
        assert snapshot.n_active == len(docs)
        assert snapshot.n_confirmed == 1
        assert snapshot.truncated is False
        again = notification_store.subscription_alerts(SID)
        assert [s.alert_id for s in again.subjects] == [s.alert_id for s in snapshot.subjects]
    finally:
        _uninstall()


def test_an_expired_subject_is_not_a_standing_alert():
    """Every read path tests the expiry field itself, and here it is not hygiene.

    A subject document outlives the dated train it describes by up to HISTORY_DAYS, and TTL runs up
    to ~24 h late -- so trusting TTL would show last Tuesday's cancellation as a standing alert.
    """
    client = _FakeClient()
    _install(client)
    try:
        _alert_page(
            client,
            [
                _state("stale", notification_store.PHASE_CONFIRMED,
                       **{notification_store.TTL_FIELD: NOW - timedelta(seconds=1)}),
                _state("live", notification_store.PHASE_PREDICTED),
                # An unreadable expiry reads as expired, the direction a retention promise must
                # fail in.
                _state("unreadable", notification_store.PHASE_PREDICTED,
                       **{notification_store.TTL_FIELD: "not a timestamp"}),
            ],
        )
        snapshot = notification_store.subscription_alerts(SID)
        assert [s.alert_id for s in snapshot.subjects] == ["live"]
        assert snapshot.n_active == 1
    finally:
        _uninstall()


def test_one_client_cannot_read_anothers_alert_state():
    """Tenancy is checked here rather than left to the caller, like every other
    subscription-scoped method in this module."""
    client = _FakeClient()
    _install(client)
    try:
        _alert_page(client, [_state("a", notification_store.PHASE_PREDICTED)])
        assert notification_store.subscription_alerts(SID, client_id=CLIENT).n_active == 1
        forbidden = False
        try:
            notification_store.subscription_alerts(SID, client_id="someone-else")
        except notification_store.Forbidden:
            forbidden = True
        assert forbidden
    finally:
        _uninstall()


def test_the_client_wide_read_is_one_query_grouped_by_subscription():
    """R21's shape at the request layer: one query per REQUEST, never one per row.

    A list page can hold hundreds of watches, and a per-row read would be hundreds of queries plus
    up to MAX_SUBJECTS_PER_READ document reads apiece.
    """
    client = _FakeClient()
    _install(client)
    try:
        docs = [
            _state("a", notification_store.PHASE_PREDICTED),
            _state("b", notification_store.PHASE_CONFIRMED, subscription_id="sub-2"),
            _state("c", notification_store.PHASE_PREDICTED, subscription_id="sub-2"),
        ]
        _alert_page(client, docs)
        client.query_calls.clear()
        loaded = notification_store.client_alerts(CLIENT)
        assert len(client.query_calls) == 1
        assert loaded.truncated is False
        assert set(loaded.snapshots) == {SID, "sub-2"}
        assert loaded.snapshots[SID].n_active == 1
        assert loaded.snapshots["sub-2"].n_active == 2
        assert loaded.snapshots["sub-2"].n_confirmed == 1
    finally:
        _uninstall()


def test_a_bounded_read_marks_every_snapshot_and_the_wrapper():
    """The truncation flag is the honest half, and it must survive an EMPTY mapping.

    A subscription whose subjects fell outside the page is absent from `snapshots`, so without the
    flag on the wrapper the caller could not tell "no standing alert" from "crowded out" -- and
    rendering the second as the first is exactly the all-clear R6 forbids.
    """
    client = _FakeClient()
    _install(client)
    try:
        docs = [
            _state(f"a{i}", notification_store.PHASE_PREDICTED) for i in range(4)
        ]
        _alert_page(client, docs)
        loaded = notification_store.client_alerts(CLIENT, limit=2)
        assert loaded.truncated is True
        assert all(s.truncated for s in loaded.snapshots.values())
        one = notification_store.subscription_alerts(SID, limit=2)
        assert one.truncated is True
        # And an empty page still carries the flag on the wrapper.
        client.pages["client_alerts"] = []
        empty = notification_store.client_alerts(CLIENT, limit=2)
        assert empty.snapshots == {} and empty.truncated is False
    finally:
        _uninstall()


def test_an_empty_snapshot_is_a_claim_and_a_bounded_one_is_not():
    """The two must not be expressible with the same value: one says "no standing alert", the other
    says "we do not know", and a client that cannot tell them apart shows the second as the
    first."""
    empty = notification_store.empty_alert_snapshot(SID, now=NOW)
    assert empty.n_active == 0 and empty.n_confirmed == 0
    assert empty.truncated is False
    assert empty.subjects == ()
    assert empty.as_of == NOW


def test_an_unreadable_subject_is_skipped_rather_than_failing_its_collection():
    """R20's per-document guard, the same one `active_subscriptions` applies."""
    client = _FakeClient()
    _install(client)
    try:
        broken = _state("broken", notification_store.PHASE_PREDICTED)
        broken.pop("alert_id")  # the one field `_alert_state_from_doc` cannot do without
        # Registered by hand: `_alert_page` keys on alert_id, which this row deliberately lacks.
        client.pages["subscription_alerts"] = [
            (f"{notification_store.SUBJECTS}/broken", broken),
            (f"{notification_store.SUBJECTS}/fine", _state("fine", notification_store.PHASE_PREDICTED)),
        ]
        snapshot = notification_store.subscription_alerts(SID)
        assert [s.alert_id for s in snapshot.subjects] == ["fine"]
    finally:
        _uninstall()


def test_the_read_ceiling_and_the_served_cap_are_separate_numbers():
    """They answer different questions: what one request may READ, and what one row may SHOW.

    The served cap is the daily notification allowance on purpose -- a fifth subject is not
    something anybody can be told about today.
    """
    assert notification_store.MAX_ALERTS_SERVED == notification_store.MAX_ALERT_DELIVERIES_PER_DAY
    assert notification_store.MAX_SUBJECTS_PER_READ > notification_store.MAX_ALERTS_SERVED


def test_the_identity_plane_shares_this_modules_one_client():
    """Two gRPC channels to one database in a single-replica process is a cost with no gain, and
    `notification_auth` reads through the accessor below rather than opening a second one."""
    import notification_auth

    client = _FakeClient()
    _install(client)
    try:
        assert notification_store.firestore_client() is client
        # `notification_auth` names no database of its own, so there is one place that decides.
        assert "database" not in inspect.signature(notification_auth.configure).parameters
        assert "notification_store.firestore_client()" in pathlib.Path(
            pathlib.Path(notification_auth.__file__)
        ).read_text()
        notification_auth.configure()
        assert notification_auth.store_ready() is True
    finally:
        _uninstall()
        notification_auth.reset_for_tests()


# ---- Scenario 8: index coverage, both directions -----------------------------------------------


def _declared_indexes() -> set[tuple[str, tuple[tuple[str, str], ...]]]:
    return {
        (
            index["collectionGroup"],
            tuple((f["fieldPath"], f["order"]) for f in index["fields"]),
        )
        for index in INDEXES["indexes"]
    }


def test_every_query_in_the_module_has_a_checked_in_index():
    declared = _declared_indexes()
    for spec in notification_store.QUERY_SPECS:
        if not spec.index_fields:
            continue  # one field: Firestore's automatic single-field index serves it
        assert (spec.collection, spec.index_fields) in declared, (
            f"{spec.name} needs an index on {spec.index_fields} in firestore.indexes.json"
        )


def test_every_checked_in_index_belongs_to_a_query_and_a_collection_the_module_uses():
    """The reverse direction. An index declared but never applied fails the same way as a missing
    one -- and one declared for a query that no longer exists is paid for on every write."""
    needed = {
        (spec.collection, spec.index_fields)
        for spec in notification_store.QUERY_SPECS
        if spec.index_fields
    }
    for index in INDEXES["indexes"]:
        assert index["collectionGroup"] in notification_store.COLLECTIONS
        key = (
            index["collectionGroup"],
            tuple((f["fieldPath"], f["order"]) for f in index["fields"]),
        )
        assert key in needed, f"{key} is declared but no query in the module needs it"


def test_every_checked_in_index_is_materialised_by_terraform():
    assert 'jsondecode(file("${path.module}/../firestore.indexes.json")).indexes' in FIRESTORE_TF
    assert 'resource "google_firestore_index" "notification"' in FIRESTORE_TF
    assert "for_each = local.firestore_indexes_by_key" in FIRESTORE_TF
    # Keyed on the index's own CONTENT -- collection group plus the ordered field paths and
    # directions -- never on its position, or reordering the file destroys and recreates every index.
    assert 'for index in local.firestore_indexes :' in FIRESTORE_TF
    assert "index.collectionGroup" in FIRESTORE_TF
    assert "f.fieldPath" in FIRESTORE_TF and "f.order" in FIRESTORE_TF
    assert "count.index" not in FIRESTORE_TF_CODE
    # And the trigger without which the whole plan-time-drift property is false.
    assert TERRAFORM_YML.count("- 'firestore.indexes.json'") == 2


def test_every_index_entry_has_the_fields_the_for_each_reads():
    for index in INDEXES["indexes"]:
        assert set(index) <= {"collectionGroup", "queryScope", "fields"}
        assert index.get("queryScope", "COLLECTION") == "COLLECTION"
        assert index["fields"], "an index with no fields is not an index"
        for spec_field in index["fields"]:
            assert set(spec_field) == {"fieldPath", "order"}
            assert spec_field["order"] in ("ASCENDING", "DESCENDING")
            # Known provider drift: an explicit __name__ field causes recreate loops.
            assert spec_field["fieldPath"] != "__name__"


def test_the_load_query_sheds_dead_rows_before_live_ones():
    """`expires_at` DESCENDING is load-bearing, not cosmetic.

    Expiry is tested in Python, so an expired-but-not-deleted document is still returned by this
    query. Ordered ascending, a page truncated at the per-cycle ceiling would keep exactly the rows
    closest to expiry -- the dead ones -- and starve live subscriptions.
    """
    spec = next(s for s in notification_store.QUERY_SPECS if s.name == "load_active_subscriptions")
    assert spec.order_by == (("expires_at", "DESCENDING"),)
    assert (
        notification_store.SUBSCRIPTIONS,
        (("status", "ASCENDING"), ("expires_at", "DESCENDING")),
    ) in _declared_indexes()


def test_every_collection_group_has_exactly_one_ttl_field():
    ttl_block = FIRESTORE_TF.split("firestore_ttl_collections = [", 1)[1].split("]", 1)[0]
    for collection in notification_store.COLLECTIONS:
        assert f'"{collection}"' in ttl_block, f"{collection} has no TTL field in Terraform"
    assert notification_store.TTL_FIELD == "expires_at"
    assert f'field      = "{notification_store.TTL_FIELD}"' in FIRESTORE_TF
    assert FIRESTORE_TF_CODE.count("ttl_config {}") == 1  # one resource, for_each over four groups
    # An empty index_config block DISABLES indexing on the field; deliberately not used.
    assert "index_config {}" not in FIRESTORE_TF_CODE


def test_field_overrides_stay_empty_so_ttl_has_one_source_of_truth():
    assert INDEXES.get("fieldOverrides") == [], (
        "the CLI format can express a TTL, but Terraform owns them here -- two writable sources "
        "for a retention setting is how one of them quietly goes stale"
    )


def test_every_filter_goes_through_the_query_helper():
    helper = inspect.getsource(notification_store._query)
    for token in (".where(", ".order_by(", ".limit("):
        assert MODULE_SOURCE.count(token) == helper.count(token) > 0, (
            f"{token} appears outside _query -- a query that dodges the spec table can ship with "
            "no index declaration"
        )


def test_a_range_filter_must_be_the_first_ordering():
    """Firestore's own rule, enforced where the spec is declared rather than discovered at runtime."""
    try:
        notification_store.QuerySpec(
            name="bad",
            collection=notification_store.DELIVERIES,
            inequality=(("retry_not_before", "<="),),
            order_by=(("valid_until", "ASCENDING"),),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("a range filter not ordered first must be refused")


# ---- The event contract: revisions, transitions, supersession ----------------------------------


def test_a_revision_is_seeded_from_utc_and_never_goes_backwards():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    request = _emit()
    client.put(
        notification_store.SUBJECTS,
        request.alert_id,
        {
            "alert_id": request.alert_id,
            "subscription_id": SID,
            "phase": notification_store.PHASE_PREDICTED,
            "revision": 10**9,
            notification_store.TTL_FIELD: NOW + timedelta(days=1),
        },
    )
    _install(client)
    commit = notification_store.commit_event(_emit(phase=notification_store.PHASE_CONFIRMED), now=NOW)
    # The clock's seed is far below the stored revision, so the clamp is what supplies monotonicity.
    assert notification_store.revision_seed(NOW) < 10**9
    assert commit.revision == 10**9 + 1
    # A clock that goes backwards -- DST, an NTP step -- cannot move it back either.
    earlier = NOW - timedelta(hours=2)
    assert notification_store.revision_seed(earlier) < notification_store.revision_seed(NOW)
    assert notification_store.revision_seed(NOW) > 0


def test_a_fresh_subject_does_not_restart_at_zero():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    commit = notification_store.commit_event(_emit(), now=NOW)
    assert commit.revision == notification_store.revision_seed(NOW) > 1


def test_only_a_lifecycle_transition_mints_a_revision():
    table = {
        (None, "predicted"): notification_store.EVENT_CREATED,
        (None, "confirmed"): notification_store.EVENT_CREATED,
        ("predicted", "predicted"): None,
        ("predicted", "confirmed"): notification_store.EVENT_UPDATED,
        ("confirmed", "confirmed"): None,
        # R14's third rule, one layer earlier: the event never exists.
        ("confirmed", "predicted"): None,
        ("withdrawn", "predicted"): notification_store.EVENT_UPDATED,
        ("withdrawn", "confirmed"): notification_store.EVENT_UPDATED,
    }
    for (stored, requested), expected in table.items():
        assert notification_store._transition(stored, requested) == expected, (stored, requested)


def test_a_changed_payload_mints_nothing():
    """No materiality threshold, at any size, and the store owns the decision so nothing can pass
    it a per-cycle digest that would mint every tick."""
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    first = notification_store.commit_event(_emit(), now=NOW)
    again = notification_store.commit_event(
        _emit(payload={"notification": {"title": "helt annen tekst", "body": "94 %"}}),
        now=NOW + timedelta(minutes=5),
    )
    assert again.status == "unchanged"
    assert again.event_id == first.event_id and again.revision == first.revision
    assert "facts_digest" not in MODULE_SOURCE, (
        "a caller-supplied payload digest would mint a revision every cycle"
    )


def test_minting_a_revision_supersedes_the_pending_delivery_of_the_previous_one():
    client, request = _emitted()
    first_delivery = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    commit = notification_store.commit_event(
        _emit(phase=notification_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=3)
    )
    assert commit.status == "created"
    assert commit.superseded_delivery_id == first_delivery
    assert client.doc(notification_store.DELIVERIES, first_delivery)["state"] == "superseded"
    assert client.doc(notification_store.DELIVERIES, first_delivery)["superseded_by"] == commit.event_id
    assert client.doc(notification_store.DELIVERIES, commit.delivery_id)["state"] == "pending"


def test_a_sent_delivery_is_never_superseded_after_the_fact():
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    notification_store.mark_delivery_sent(delivery_id, now=NOW)
    commit = notification_store.commit_event(
        _emit(phase=notification_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=3)
    )
    assert commit.superseded_delivery_id is None
    assert client.doc(notification_store.DELIVERIES, delivery_id)["state"] == "sent"


def test_a_late_sent_on_a_superseded_delivery_writes_nothing():
    """An attempt takes seconds, and a newer revision can be minted inside them.

    The rule, which changed when `_sent_body` gained its `pending` guard and is worth stating
    because the guard makes it rather than a separate decision: a delivery that is no longer
    ``pending`` when its attempt finally returns records NOTHING. It spends no cap slot, writes no
    delivered mark, and leaves the subject alone.

    That is deliberate and not merely a side effect. A superseded delivery is one we have already
    decided is obsolete and minted a replacement for, and the replacement will spend its own slot
    when it lands -- so counting both would spend TWO of a subscription's four for one train in one
    cycle, which is worse than counting one. The cost is that a notification the client genuinely
    received goes unrecorded, and under-counting R11's cap is the safe direction: failing open costs
    at most an extra notification, while failing closed suppresses a confirmed cancellation.

    The `pending_delivery_id` assertion below is now defensive rather than load-bearing -- minting a
    revision marks the previous delivery superseded in the SAME transaction, so a `pending` delivery
    and a newer pending delivery cannot coexist for one subject -- and it is kept because the
    clobber it guards against is silent: it would drop the subject's pointer to the newer pending
    delivery, and the next mint would then fail to supersede it, putting two boxes about one train on
    a lock screen (the 2026-08-05 defect, one channel over).
    """
    client, request = _emitted()
    first = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    newer = notification_store.commit_event(
        _emit(phase=notification_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=2)
    )
    assert newer.delivery_id != first
    assert client.doc(notification_store.DELIVERIES, first)["state"] == "superseded"
    before = dict(client.docs)
    # The old attempt returns AFTER the new revision was minted.
    late = notification_store.mark_delivery_sent(first, now=NOW + timedelta(minutes=3))
    assert late.already_resolved is True and late.slot_spent is False
    assert late.state == "superseded", "it must report the state it actually holds"
    assert client.docs == before, "a resolved delivery is not re-resolved"
    subject = client.doc(notification_store.SUBJECTS, request.alert_id)
    assert subject["pending_delivery_id"] == newer.delivery_id
    assert subject["delivered_revision"] is None
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["daily_deliveries"] == {}

    # The replacement lands normally and is the one that spends the slot.
    sent = notification_store.mark_delivery_sent(newer.delivery_id, now=NOW + timedelta(minutes=4))
    assert sent.already_resolved is False and sent.slot_spent is True and sent.state == "sent"
    subject = client.doc(notification_store.SUBJECTS, request.alert_id)
    assert subject["delivered_revision"] == newer.revision
    assert subject["pending_delivery_id"] is None


def test_a_late_sent_cannot_double_spend_the_daily_cap():
    """The defect U6 hit live, as a stray `NotFound` from an abandoned attempt thread.

    The sequence is ordinary: the delivery pass abandons an attempt's thread when its bounded wait
    expires -- it cannot kill a thread parked in a socket read -- the next cycle legitimately
    re-sends the same still-`pending` event, and the abandoned thread then returns and marks the
    same delivery sent. Unguarded, `attempts` double-increments and R11's Oslo-day counter is spent
    TWICE for one notification, so a traveller silently loses one of their four. R11's grain is the
    whole of this plan's parity claim with /varsel.
    """
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    first = notification_store.mark_delivery_sent(delivery_id, now=NOW)
    assert first.slot_spent is True and first.attempts == 1 and first.already_resolved is False
    after_first = dict(client.docs)

    second = notification_store.mark_delivery_sent(delivery_id, now=NOW + timedelta(minutes=5))
    assert second.already_resolved is True, "a second sent must not read as a fresh success"
    assert second.slot_spent is False and second.state == "sent"
    assert second.attempts == 1, "attempts must not double-increment"
    assert client.docs == after_first, "the second write must touch nothing at all"
    counters = client.doc(notification_store.SUBSCRIPTIONS, SID)["daily_deliveries"]
    assert counters[notification_store.oslo_day(NOW)] == 1, "one notification spends one slot"
    assert notification_store.cap_remaining(
        client.doc(notification_store.SUBSCRIPTIONS, SID), NOW
    ) == notification_store.MAX_ALERT_DELIVERIES_PER_DAY - 1


def test_a_late_failure_cannot_resurrect_a_delivered_event():
    """The same race on the other write, and the worse of the two.

    An abandoned attempt thread from the previous cycle, returning after this cycle has marked the
    delivery `sent`, would put the state back to `pending` with a `retry_not_before` -- resurrecting
    a delivery the client has already received, so the next cycle sends it again. The receiver's
    inbox suppresses the duplicate (R12 is at-least-once), which is exactly why this would never
    have been noticed. `_terminal_body` has had this guard all along; its absence in `_sent_body`
    and `_failed_body` was the oversight.
    """
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    notification_store.mark_delivery_sent(delivery_id, now=NOW)
    after_sent = dict(client.docs)
    late = notification_store.mark_delivery_failed(
        delivery_id, error="connection reset", retryable=True, retry_after_s=30, now=NOW
    )
    assert late.already_resolved is True and late.state == "sent"
    assert client.docs == after_sent
    assert client.doc(notification_store.DELIVERIES, delivery_id)["state"] == "sent"
    # And it is not re-offered, which is a property of the QUERY rather than of the reader -- so it
    # is asserted on the spec. The fake's `_query` does not implement filters (it serves a
    # registered page verbatim), so feeding it a `sent` row and watching the reader drop it would
    # test nothing that runs in production.
    spec = next(s for s in notification_store.QUERY_SPECS if s.name == "pending_deliveries")
    assert spec.equality == ("state",)
    notification_store.pending_deliveries(now=NOW)
    assert client.query_calls[-1][1] == {"state": "pending"}


def test_the_pending_event_ceiling_is_counted_with_an_aggregation():
    """One count per emit, and pending deliveries accumulate for a full 60 minutes of validity.

    Streamed, a hundred subjects each reading a hundred pending documents is ten thousand reads per
    cycle and several million a day. The stream survives as a fallback -- the same answer at a worse
    price -- so a build without aggregation support degrades in cost rather than refusing to emit.
    """
    assert "query.count(alias=" in MODULE_SOURCE
    assert "sum(1 for _ in query.stream(" in MODULE_SOURCE, "the fallback is deliberate"
    calls: list[int] = []

    class _Agg:
        def get(self, timeout: float | None = None) -> list[list[Any]]:
            assert timeout == notification_store._CALL_DEADLINE_S, "aggregation without a deadline"
            calls.append(1)

            class _Value:
                value = 0

            return [[_Value()]]

    class _CountingPage(_Page):
        def count(self, alias: str) -> _Agg:
            assert alias == "n"
            return _Agg()

        def stream(self, timeout: float | None = None) -> list[_Snap]:
            raise AssertionError("the aggregation path must be preferred over a stream")

    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    real_query = notification_store._query

    def _counting_query(name: str, **kwargs: Any) -> Any:
        page = real_query(name, **kwargs)
        return _CountingPage(client, page.rows) if name.startswith("destination_") else page

    notification_store._query = _counting_query  # type: ignore[assignment]
    assert notification_store.commit_event(_emit(), now=NOW).status == "created"
    assert calls == [1], "exactly one aggregation per emit"


_WITHDRAWAL = _emit(payload={"notification": {"title": "Varselet er trukket", "body": "x"}})
# One pass period, measured at ~296 s on 2026-09-08. Used rather than a round number so these
# checks describe the cadence the code is sized against.
_PERIOD = timedelta(seconds=296)


def test_a_withdrawal_needs_two_below_threshold_cycles_in_one_run():
    client, request = _emitted()
    first = notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + _PERIOD)
    assert first.status == "counting" and first.below_threshold_cycles == 1
    final = notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + 2 * _PERIOD)
    assert final.status == "created" and final.event_type == notification_store.EVENT_WITHDRAWN
    subject = client.doc(notification_store.SUBJECTS, request.alert_id)
    assert subject["phase"] == notification_store.PHASE_WITHDRAWN
    # A mint closes the run: BOTH fields, or the next dip continues a run that ended here.
    assert subject["below_threshold_cycles"] == 0
    assert subject["below_threshold_at"] is None


def test_a_dip_a_recovery_and_a_second_dip_withdraw_nothing():
    """The defect: the count was a lifetime TOTAL while the docstring said consecutive.

    An alert-worthy cycle that mints nothing reaches no store call at all -- `notification_events.
    skip_reason` skips it to save a transaction per standing subject per cycle -- so nothing ever
    reset the counter, and a dip, an hour of recovery and a second dip withdrew the alert. That is a
    single-dip withdrawal in effect, which is exactly what `WITHDRAWAL_CYCLES` exists to prevent.
    """
    client, request = _emitted()
    first = notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + _PERIOD)
    assert first.status == "counting" and first.below_threshold_cycles == 1
    # An hour of recovered, alert-worthy cycles. Each one mints nothing and -- this is the whole
    # point -- reaches the store not at all, so nothing here writes.
    recovered = NOW + timedelta(hours=1)
    before = dict(client.docs)
    assert notification_store.commit_event(_emit(), now=recovered).status == "unchanged"
    assert client.docs == before, "an unchanged cycle writes nothing; the fix cannot rely on one"
    # The next dip must START A NEW RUN rather than complete the old one.
    again = notification_store.record_below_threshold(_WITHDRAWAL, now=recovered + _PERIOD)
    assert again.status == "counting", "a dip an hour after a dip is not a run"
    assert again.below_threshold_cycles == 1
    subject = client.doc(notification_store.SUBJECTS, request.alert_id)
    assert subject["phase"] == notification_store.PHASE_PREDICTED, "nothing was withdrawn"
    # ... and the new run withdraws on ITS second observation, so the rule still fires.
    final = notification_store.record_below_threshold(_WITHDRAWAL, now=recovered + 2 * _PERIOD)
    assert final.status == "created" and final.event_type == notification_store.EVENT_WITHDRAWN


def test_the_run_is_closed_by_the_gap_and_the_boundary_is_the_constant():
    _emitted()
    notification_store.record_below_threshold(_WITHDRAWAL, now=NOW)
    # Exactly at the boundary is still one run (`<=`); one second past it is not.
    at_edge = NOW + timedelta(seconds=notification_store.WITHDRAWAL_GAP_S)
    assert notification_store.record_below_threshold(_WITHDRAWAL, now=at_edge).status == "created"

    _emitted()
    notification_store.record_below_threshold(_WITHDRAWAL, now=NOW)
    past_edge = NOW + timedelta(seconds=notification_store.WITHDRAWAL_GAP_S + 1)
    restarted = notification_store.record_below_threshold(_WITHDRAWAL, now=past_edge)
    assert restarted.status == "counting" and restarted.below_threshold_cycles == 1
    # The window is ~3 pass periods, so a run survives two skipped or slow cycles.
    assert notification_store.WITHDRAWAL_GAP_S >= 3 * _PERIOD.total_seconds()


def test_a_subject_with_no_run_anchor_starts_a_fresh_run():
    """Rows written before `below_threshold_at` existed must restart, never withdraw early."""
    client, request = _emitted()
    stored = dict(client.doc(notification_store.SUBJECTS, request.alert_id))
    stored["below_threshold_cycles"] = notification_store.WITHDRAWAL_CYCLES - 1
    stored.pop("below_threshold_at", None)
    client.put(notification_store.SUBJECTS, request.alert_id, stored)
    outcome = notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + _PERIOD)
    assert outcome.status == "counting" and outcome.below_threshold_cycles == 1


def test_open_runs_are_served_uncapped_and_before_the_display_cap():
    """AE3's thirteen-subject line suspension is the case a field on `AlertState` would miss.

    `client_alerts` enumerates at most `MAX_ALERTS_SERVED` (4) subjects per subscription for
    DISPLAY, so nine of those thirteen would fall through to the time window -- the very case
    `_withdraw_body`'s own comment cites. `open_below_threshold` is therefore derived from the whole
    live page and capped only by `MAX_SUBJECTS_PER_READ`.
    """
    client = _FakeClient()
    _install(client)
    rows = []
    for i in range(13):
        rows.append(
            {
                "alert_id": f"a{i:02d}",
                "subscription_id": SID,
                "client_id": CLIENT,
                "destination_id": DEST,
                "phase": notification_store.PHASE_PREDICTED,
                "revision": 1,
                "updated_at": NOW,
                notification_store.TTL_FIELD: NOW + timedelta(days=7),
                # Every other one has a run open.
                **({"below_threshold_at": NOW - timedelta(seconds=60)} if i % 2 == 0 else {}),
            }
        )
    client.page("client_alerts", notification_store.SUBJECTS, rows)
    alerts = notification_store.client_alerts(CLIENT, now=NOW)
    # The DISPLAY list is capped ...
    assert len(alerts.snapshots[SID].subjects) == notification_store.MAX_ALERTS_SERVED
    assert alerts.snapshots[SID].n_active == 13, "the counts are over everything read"
    # ... and the open-run mapping is not.
    assert set(alerts.open_below_threshold) == {f"a{i:02d}" for i in range(0, 13, 2)}
    assert len(alerts.open_below_threshold) == 7 > notification_store.MAX_ALERTS_SERVED
    # A mapping, not a set: the caller can skip a clear whose run the store would restart anyway.
    stale = alerts.open_below_threshold["a00"]
    assert isinstance(stale, datetime)
    assert (NOW - stale).total_seconds() < notification_store.WITHDRAWAL_GAP_S

    # An expired subject contributes nothing: every read path tests the expiry field itself.
    client.page(
        "client_alerts",
        notification_store.SUBJECTS,
        [dict(rows[0], **{notification_store.TTL_FIELD: NOW - timedelta(hours=1)})],
    )
    assert notification_store.client_alerts(CLIENT, now=NOW).open_below_threshold == {}


def test_clearing_a_run_writes_once_and_touches_no_phase_or_revision():
    client, request = _emitted()
    before = dict(client.doc(notification_store.SUBJECTS, request.alert_id))
    # Nothing open: no write at all. This is what makes "zero writes on an ordinary standing
    # cycle" affordable for the caller.
    assert notification_store.clear_below_threshold(request.alert_id, now=NOW) is False
    assert client.doc(notification_store.SUBJECTS, request.alert_id) == before
    assert client.transactions[-1].writes == []

    # A run open: exactly one write, and only the two run fields plus the timestamp.
    notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + _PERIOD)
    opened = dict(client.doc(notification_store.SUBJECTS, request.alert_id))
    assert opened["below_threshold_cycles"] == 1 and opened["below_threshold_at"] is not None
    assert notification_store.clear_below_threshold(request.alert_id, now=NOW + 2 * _PERIOD) is True
    writes = client.transactions[-1].writes
    assert len(writes) == 1 and writes[0][0] == "update"
    assert set(writes[0][2]) == {"below_threshold_cycles", "below_threshold_at", "updated_at"}
    cleared = client.doc(notification_store.SUBJECTS, request.alert_id)
    assert cleared["below_threshold_cycles"] == 0 and cleared["below_threshold_at"] is None
    # No lifecycle movement: a recovery is not a transition and must mint nothing.
    assert cleared["phase"] == opened["phase"] == notification_store.PHASE_PREDICTED
    assert cleared["revision"] == opened["revision"]
    assert cleared["last_event_id"] == opened["last_event_id"]
    assert len([p for p in client.docs if p.startswith(notification_store.EVENTS)]) == 1

    # Idempotent: a second clear writes nothing.
    assert notification_store.clear_below_threshold(request.alert_id, now=NOW + 3 * _PERIOD) is False
    assert client.transactions[-1].writes == []


def test_clearing_never_creates_a_subject_and_answers_false_for_a_missing_one():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    before = dict(client.docs)
    assert notification_store.clear_below_threshold("no-such-alert", now=NOW) is False
    assert notification_store.clear_below_threshold("", now=NOW) is False
    assert client.docs == before, "a clear must never mint a phase-less subject document"
    # An expired subject is a missing one, here as everywhere else.
    request = _emit()
    client.put(
        notification_store.SUBJECTS,
        request.alert_id,
        {
            "alert_id": request.alert_id,
            "phase": notification_store.PHASE_PREDICTED,
            "below_threshold_at": NOW - timedelta(seconds=60),
            notification_store.TTL_FIELD: NOW - timedelta(hours=1),
        },
    )
    assert notification_store.clear_below_threshold(request.alert_id, now=NOW) is False


def test_a_cleared_run_makes_the_next_dip_a_first_dip():
    """The two new pieces together remove the time window's one failure mode.

    Dip, one recovered cycle, dip inside `WITHDRAWAL_GAP_S`: the window alone withdraws, which
    sends a real «varselet er trukket tilbake» and then re-alerts. With the recovery cleared, the
    second dip is a first dip.
    """
    client, request = _emitted()
    assert notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + _PERIOD).status == "counting"
    # One recovered cycle, inside the window -- the case the window gets wrong.
    recovered = NOW + 2 * _PERIOD
    assert recovered - (NOW + _PERIOD) < timedelta(seconds=notification_store.WITHDRAWAL_GAP_S)
    assert notification_store.clear_below_threshold(request.alert_id, now=recovered) is True
    again = notification_store.record_below_threshold(_WITHDRAWAL, now=recovered + _PERIOD)
    assert again.status == "counting" and again.below_threshold_cycles == 1
    assert client.doc(notification_store.SUBJECTS, request.alert_id)["phase"] == (
        notification_store.PHASE_PREDICTED
    )


def test_the_withdrawal_docstring_states_what_the_code_does():
    """The repo's own recurring defect: prose beside the code stating the opposite rule.

    This one had it -- "the consecutive count ... is reset by any commit_event that mints a phase"
    beside a counter that no unchanged cycle could ever reset. So the docstring must now name the
    approximation rather than the ideal, and must not claim consecutiveness.
    """
    doc = notification_store.record_below_threshold.__doc__ or ""
    assert "RUN" in doc and "WITHDRAWAL_GAP_S" in doc
    assert "approximation of" in doc
    body = inspect.getsource(notification_store._withdraw_body)
    assert "WITHDRAWAL_GAP_S" in body and "below_threshold_at" in body
    # The exact-but-costlier alternatives are recorded where the next reader will look.
    assert "skip_reason" in body and "CALLER change" in body


def test_a_below_threshold_observation_about_an_unwarned_train_writes_nothing():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    before = dict(client.docs)
    outcome = notification_store.record_below_threshold(_emit(), now=NOW)
    assert outcome.status == "unchanged" and outcome.reason == "nothing_standing"
    assert client.docs == before


def test_the_alert_id_grain_is_per_subscription_and_the_event_id_is_per_revision():
    a = notification_store.alert_subject_id("sub-1", TRAIN)
    b = notification_store.alert_subject_id("sub-2", TRAIN)
    assert a != b, (
        "a per-train-only alert_id would make the receiver's own revision rule suppress the "
        "SECOND commuter's notification about the same disrupted train"
    )
    assert a == notification_store.alert_subject_id("sub-1", TRAIN), "must be deterministic"
    other_stop = notification_store.TrainRef(**{**TRAIN.__dict__, "stop_order": 4})
    assert notification_store.alert_subject_id("sub-1", other_stop) != a
    assert notification_store.event_id_for(a, 5) == notification_store.event_id_for(a, 5)
    assert notification_store.event_id_for(a, 5) != notification_store.event_id_for(a, 6)
    assert notification_store.event_id_for(a, 5) != notification_store.event_id_for(b, 5)


def test_event_validity_is_the_earliest_of_three_things():
    horizon = NOW + timedelta(minutes=notification_store.EVENT_VALIDITY_MINUTES)
    assert (
        notification_store.event_validity(
            occurred_at=NOW, departure_at=None, subscription_expires_at=None
        )
        == horizon
    )
    departure = NOW + timedelta(minutes=12)
    assert (
        notification_store.event_validity(
            occurred_at=NOW, departure_at=departure, subscription_expires_at=None
        )
        == departure
    )
    lifetime = NOW + timedelta(minutes=3)
    assert (
        notification_store.event_validity(
            occurred_at=NOW, departure_at=departure, subscription_expires_at=lifetime
        )
        == lifetime
    )
    assert notification_store.EVENT_VALIDITY_MINUTES == 60, "U6, U8, AE12 and the docs quote this number"


def test_the_six_delivery_states_are_the_ones_the_contract_names():
    assert notification_store.DELIVERY_STATES == (
        "pending",
        "sent",
        "expired",
        "superseded",
        "cancelled",
        "failed",
    )
    assert notification_store.EVENT_TYPES == (
        "journey.alert.created",
        "journey.alert.updated",
        "journey.alert.withdrawn",
    )


# ---- R11: the daily cap, spent on success ------------------------------------------------------


def test_the_daily_cap_is_spent_on_success_and_a_replacement_is_exempt():
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    outcome = notification_store.mark_delivery_sent(delivery_id, now=NOW)
    assert outcome.state == "sent" and outcome.slot_spent is True
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert stored["daily_deliveries"][notification_store.oslo_day(NOW)] == 1
    assert notification_store.cap_remaining(stored, NOW) == (
        notification_store.MAX_ALERT_DELIVERIES_PER_DAY - 1
    )
    # The confirmation about the same train replaces a forecast the traveller already received, so
    # it is exempt -- otherwise a confirmed cancellation can be the notification the cap eats.
    confirmed = notification_store.commit_event(
        _emit(phase=notification_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=4)
    )
    assert confirmed.cap_exempt is True
    second = notification_store.mark_delivery_sent(confirmed.delivery_id, now=NOW + timedelta(minutes=4))
    assert second.slot_spent is False
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["daily_deliveries"][
        notification_store.oslo_day(NOW)
    ] == 1


def test_a_failed_attempt_spends_no_slot_and_leaves_no_mark():
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    outcome = notification_store.mark_delivery_failed(
        delivery_id, error="connection reset", retryable=True, now=NOW
    )
    assert outcome.state == "pending" and outcome.attempts == 1
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert stored["daily_deliveries"] == {}, "a dead destination must not burn the day's four"
    assert stored["delivered"] == {}, "dedup is on success, exactly as /varsel's is"


def test_a_capped_delivery_is_recorded_and_left_pending():
    client, request = _emitted()
    spent = dict(
        _live_doc(),
        daily_deliveries={notification_store.oslo_day(NOW): notification_store.MAX_ALERT_DELIVERIES_PER_DAY},
    )
    client.put(notification_store.SUBSCRIPTIONS, SID, spent)
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    claim = notification_store.claim_delivery(delivery_id, now=NOW)
    assert claim.status == "capped" and claim.reason == "daily_cap"
    stored = client.doc(notification_store.DELIVERIES, delivery_id)
    assert stored["state"] == "pending", "a suppressed alert is re-offered next cycle"
    assert stored["capped_count"] == 1, "and it is recorded rather than silently dropped"


def _many_pending(client: _FakeClient, n: int) -> list[str]:
    """`n` pending deliveries for one subscription, each its own train, all cap-eligible."""
    ids: list[str] = []
    for i in range(n):
        train = notification_store.TrainRef(**{**TRAIN.__dict__, "stop_order": 10 + i})
        commit = notification_store.commit_event(_emit(train=train), now=NOW)
        assert commit.delivery_id is not None
        ids.append(commit.delivery_id)
    return ids


def test_a_claim_reserves_the_slot_so_a_concurrent_claim_cannot_read_it_free():
    """R11 could be OVERSPENT, masked by a coincidence.

    The allowance was READ by `claim_delivery` and SPENT by `mark_delivery_sent`, so with a delivery
    pool wider than the cap the first wave of claims all read "slots left" before any of them spent
    one: up to `MAX_ALERT_DELIVERIES_PER_DAY + workers - 1` notifications for one subscription in one
    Oslo day. It did not bite only because `webhook_delivery._WORKERS` (4) happens to equal the cap
    (4). `varsel._WriteBudget.claim` is the precedent for the fix, and its docstring records the
    same read-then-act shape overshooting a 30 s ceiling by 16 s.

    Claiming without ever reporting back is what a concurrent pool does: every worker claims, then
    sends. So claiming five times in a row -- no sends at all -- must be refused on the fifth.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    ids = _many_pending(client, notification_store.MAX_ALERT_DELIVERIES_PER_DAY + 3)

    statuses = [notification_store.claim_delivery(d, now=NOW).status for d in ids]
    assert statuses[: notification_store.MAX_ALERT_DELIVERIES_PER_DAY] == ["ready"] * 4
    assert set(statuses[notification_store.MAX_ALERT_DELIVERIES_PER_DAY :]) == {"capped"}, statuses
    # Nothing has been SENT, so the confirmed count is still zero -- the refusal comes entirely
    # from the reservations, which is the property the old code lacked.
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert stored["daily_deliveries"] == {}
    assert len(stored["daily_reservations"]) == notification_store.MAX_ALERT_DELIVERIES_PER_DAY
    assert notification_store.cap_remaining(stored, NOW) == 0
    # And each reservation is keyed by ITS delivery id, so re-claiming one is idempotent rather
    # than consuming a second slot.
    assert set(stored["daily_reservations"]) == set(ids[:4])
    notification_store.claim_delivery(ids[0], now=NOW)
    assert len(client.doc(notification_store.SUBSCRIPTIONS, SID)["daily_reservations"]) == 4


def test_a_reserved_slot_is_confirmed_by_a_send_and_released_by_every_other_ending():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    ids = _many_pending(client, 4)

    def _reservations() -> dict[str, Any]:
        return client.doc(notification_store.SUBSCRIPTIONS, SID)["daily_reservations"]

    def _confirmed() -> int:
        return client.doc(notification_store.SUBSCRIPTIONS, SID)["daily_deliveries"].get(
            notification_store.oslo_day(NOW), 0
        )

    # CONFIRM: the reservation becomes a confirmed send, and the two are disjoint so the slot is
    # charged exactly once.
    assert notification_store.claim_delivery(ids[0], now=NOW).status == "ready"
    assert set(_reservations()) == {ids[0]}
    assert notification_store.mark_delivery_sent(ids[0], now=NOW).slot_spent is True
    assert _reservations() == {} and _confirmed() == 1

    # RELEASE on a retryable failure: the delivery stays pending for the next cycle and must not
    # hold the allowance while it waits.
    assert notification_store.claim_delivery(ids[1], now=NOW).status == "ready"
    notification_store.mark_delivery_failed(ids[1], error="reset", retryable=True, now=NOW)
    assert _reservations() == {} and _confirmed() == 1

    # RELEASE on expiry and on cancellation.
    assert notification_store.claim_delivery(ids[2], now=NOW).status == "ready"
    notification_store.mark_delivery_expired(ids[2], now=NOW)
    assert _reservations() == {}
    assert notification_store.claim_delivery(ids[3], now=NOW).status == "ready"
    notification_store.mark_delivery_cancelled(ids[3], reason="deleted", now=NOW)
    assert _reservations() == {}
    # One send, three endings that delivered nothing: one slot spent.
    assert _confirmed() == 1
    assert notification_store.cap_remaining(client.doc(notification_store.SUBSCRIPTIONS, SID), NOW) == 3


def test_a_reservation_nobody_reports_on_is_reclaimed_on_the_next_cycle():
    """The crash case, and the reason the reservation carries an expiry at all.

    A reservation nobody releases is WORSE than the overspend it prevents: it would burn one of a
    traveller's four silently and permanently, and the plan is explicit that the slot is spent on
    success ("otherwise a dead destination burns a subscription's four for the Oslo day with nothing
    delivered"). So a lapsed reservation is ignored by `cap_remaining` and pruned by the next write
    -- no sweep, no repair job, and bounded at ONE cycle's worth of allowance because
    `RESERVATION_TTL_S` (120 s) is well under the ~296 s pass period.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    ids = _many_pending(client, 4)
    for delivery_id in ids:
        assert notification_store.claim_delivery(delivery_id, now=NOW).status == "ready"
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert notification_store.cap_remaining(stored, NOW) == 0
    # THE PROCESS DIES HERE. Nothing confirms, nothing releases; four reservations stand.
    assert len(stored["daily_reservations"]) == 4

    # Still held one attempt later -- the reservation has to outlive a slow attempt, or it would be
    # no admission control at all.
    soon = NOW + timedelta(seconds=notification_store.RESERVATION_TTL_S - 1)
    assert notification_store.cap_remaining(stored, soon) == 0
    # Reclaimed past the TTL, which the next cycle reaches.
    next_cycle = NOW + timedelta(seconds=296)
    assert next_cycle - NOW > timedelta(seconds=notification_store.RESERVATION_TTL_S), (
        "the TTL must be shorter than the pass period, or a leak costs more than one cycle"
    )
    assert notification_store.cap_remaining(stored, next_cycle) == (
        notification_store.MAX_ALERT_DELIVERIES_PER_DAY
    )
    # And the next write prunes them rather than letting them accumulate.
    assert notification_store.claim_delivery(ids[0], now=next_cycle).status == "ready"
    assert set(client.doc(notification_store.SUBSCRIPTIONS, SID)["daily_reservations"]) == {ids[0]}


def test_a_lapsed_reservation_does_not_stop_a_success_spending_its_slot():
    """A slot is spent on SUCCESS, reservation or no reservation.

    The reservation is admission control on top, never the authority for the charge -- otherwise a
    slow attempt whose reservation lapsed would deliver a notification the cap never counted, and
    the traveller could receive five.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    delivery_id = _many_pending(client, 1)[0]
    assert notification_store.claim_delivery(delivery_id, now=NOW).status == "ready"
    late = NOW + timedelta(seconds=notification_store.RESERVATION_TTL_S + 60)
    sent = notification_store.mark_delivery_sent(delivery_id, now=late)
    assert sent.slot_spent is True
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert stored["daily_deliveries"][notification_store.oslo_day(late)] == 1
    assert stored["daily_reservations"] == {}, "the lapsed entry is pruned, not left behind"


def test_a_cap_exempt_delivery_reserves_nothing():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    forecast = notification_store.commit_event(_emit(), now=NOW)
    notification_store.mark_delivery_sent(forecast.delivery_id, now=NOW)
    confirmed = notification_store.commit_event(
        _emit(phase=notification_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=1)
    )
    assert confirmed.cap_exempt is True
    claim = notification_store.claim_delivery(confirmed.delivery_id, now=NOW + timedelta(minutes=1))
    assert claim.status == "ready"
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert stored["daily_reservations"] == {}, "an exempt claim takes no slot to reserve"
    # And it is admitted even with the whole day's allowance already spent.
    client.put(
        notification_store.SUBSCRIPTIONS,
        SID,
        dict(
            stored,
            daily_deliveries={
                notification_store.oslo_day(NOW): notification_store.MAX_ALERT_DELIVERIES_PER_DAY
            },
        ),
    )
    assert (
        notification_store.claim_delivery(
            confirmed.delivery_id, now=NOW + timedelta(minutes=1)
        ).status
        == "ready"
    )


def test_an_unreadable_cap_counter_grants_no_allowance():
    assert notification_store.cap_remaining({"daily_deliveries": {}}, NOW) == (
        notification_store.MAX_ALERT_DELIVERIES_PER_DAY
    )
    assert (
        notification_store.cap_remaining(
            {"daily_deliveries": {notification_store.oslo_day(NOW): "three"}}, NOW
        )
        == 0
    )


def test_the_cap_is_keyed_on_the_oslo_day_not_utc():
    late = datetime(2026, 7, 1, 22, 30, tzinfo=UTC)  # 00:30 the next day in Oslo (CEST)
    assert notification_store.oslo_day(late) == "2026-07-02"
    assert notification_store.oslo_day(datetime(2026, 1, 1, 22, 30, tzinfo=UTC)) == "2026-01-01"
    assert notification_store.MAX_ALERT_DELIVERIES_PER_DAY == 4, "/varsel's own per-registration cap"


def test_the_delivered_marks_are_per_train_the_grain_the_pass_admits_at():
    """One mark per delivered (subscription, train) -- the `alert_id` -- never one per situation.

    Events are minted per train, so `varsel._pending_pushes`' question of these marks is a per-train
    question. Keyed per situation (as they were until 2026-09-10), a second departure under an
    already-delivered situation was refused the wording pass while its event still went out.
    """
    client, request = _emitted(situations=("ENT:SituationNumber:1", "NSB:SituationNumber:9"))
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    notification_store.mark_delivery_sent(delivery_id, now=NOW)
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert set(stored["delivered"]) == {request.alert_id}, stored["delivered"]
    assert "ENT:SituationNumber:1" not in stored["delivered"], "a situation number is not a train"
    document = notification_store._subscription_from_doc(stored).as_recipient_document(NOW)
    assert document["delivered"] == [request.alert_id]
    # And a mark whose window has passed drops out rather than suppressing forever.
    later = NOW + timedelta(hours=notification_store.DELIVERED_MARK_TTL_H + 1)
    assert notification_store._subscription_from_doc(stored).as_recipient_document(later)[
        "delivered"
    ] == []


# ---- The /varsel seam --------------------------------------------------------------------------


def test_the_recipient_document_carries_every_key_varsel_reads():
    """Parity with `varsel._parse_webhook_recipient`, read out of its source rather than assumed.

    That function validates the fields the pass reads and is indifferent to the rest -- which is
    what lets this document carry the ids U6 needs -- so the check is one-directional: every key it
    reads must be present.
    """
    parser = VARSEL_SOURCE.split("def _parse_webhook_recipient", 1)[1].split("\ndef ", 1)[0]
    keys = set(re.findall(r'doc\[["\']([a-z_]+)["\']\]', parser))
    keys |= set(re.findall(r'doc\.get\(["\']([a-z_]+)["\']', parser))
    assert keys, "no document keys found -- did _parse_webhook_recipient change shape?"
    document = notification_store._subscription_from_doc(_live_doc()).as_recipient_document(NOW)
    missing = keys - set(document)
    assert not missing, f"varsel reads {sorted(missing)} and the store does not supply it"
    assert document["recipient_id"] == SID and document["days"] == [0, 1, 2, 3, 4]


def test_the_window_rules_are_varsels_own():
    for bad in (
        {"begin": "08:00", "end": "06:00"},  # overnight: refused there, refused here
        {"begin": "8:00"},
        {"end": "24:00"},
        {"days": ()},
        {"days": (7,)},
        {"to_stop": "NSR:StopPlace:337"},  # same as from_stop
    ):
        try:
            _spec(**bad).validate()
        except notification_store.Conflict:
            pass
        else:
            raise AssertionError(f"{bad} must be refused")
    _spec().validate()
    # And the restatement must not be looser than /varsel's: both refuse an overnight window.
    parser = VARSEL_SOURCE.split("def _parse_webhook_recipient", 1)[1].split("\ndef ", 1)[0]
    assert "overnight windows are not supported" in parser


def test_the_load_provider_is_the_shape_varsel_declares():
    client = _FakeClient()
    _install(client)
    client.page(
        "load_active_subscriptions",
        notification_store.SUBSCRIPTIONS,
        [_live_doc(subscription_id="s1"), _live_doc(subscription_id="s2")],
    )
    documents = notification_store.recipient_documents()
    assert [d["recipient_id"] for d in documents] == ["s1", "s2"]
    assert all(isinstance(d, dict) for d in documents)
    assert "def __call__(self) -> Iterable[Mapping[str, Any]]: ..." in VARSEL_SOURCE


# ---- Retry, expiry and the terminal states ----------------------------------------------------


def test_retry_after_is_capped_at_the_lesser_of_three_things():
    client, request = _emitted(valid_until=NOW + timedelta(seconds=90))
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    outcome = notification_store.mark_delivery_failed(
        delivery_id, error="429", retryable=True, retry_after_s=3600, now=NOW
    )
    # The header asked for an hour; the cap and then the departure both bind.
    assert outcome.retry_not_before == NOW + timedelta(seconds=90)
    assert notification_store.RETRY_AFTER_CAP_S == 300
    client.put(
        notification_store.DELIVERIES,
        delivery_id,
        dict(client.doc(notification_store.DELIVERIES, delivery_id), valid_until=NOW + timedelta(hours=2)),
    )
    outcome = notification_store.mark_delivery_failed(
        delivery_id, error="429", retryable=True, retry_after_s=3600, now=NOW
    )
    assert outcome.retry_not_before == NOW + timedelta(seconds=notification_store.RETRY_AFTER_CAP_S)


def test_a_terminal_failure_parks_the_delivery_and_a_410_cancels_it():
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    outcome = notification_store.mark_delivery_failed(
        delivery_id, error="404 no such route", retryable=False, now=NOW
    )
    assert outcome.state == "failed"
    client.put(
        notification_store.DELIVERIES,
        delivery_id,
        dict(client.doc(notification_store.DELIVERIES, delivery_id), state="pending"),
    )
    gone = notification_store.mark_delivery_failed(
        delivery_id, error="410 gone", retryable=False, terminal_state="cancelled", now=NOW
    )
    assert gone.state == "cancelled"
    try:
        notification_store.mark_delivery_failed(
            delivery_id, error="x", retryable=False, terminal_state="dead", now=NOW
        )
    except notification_store.Conflict:
        pass
    else:
        raise AssertionError("an unknown terminal state must be refused")


def test_a_delivery_past_its_validity_is_expired_rather_than_failed():
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    outcome = notification_store.mark_delivery_expired(delivery_id, now=NOW + timedelta(hours=2))
    assert outcome.state == "expired"
    assert client.doc(notification_store.DELIVERIES, delivery_id)["expired_reason"] == "validity"
    # Terminal states are not overwritten by a later sweep.
    again = notification_store.mark_delivery_expired(delivery_id, now=NOW + timedelta(hours=3))
    assert again.state == "expired"


def test_a_retried_create_writes_nothing_and_a_reused_id_is_refused():
    client = _FakeClient()
    _install(client)
    first = notification_store.create_subscription(_spec(), now=NOW)
    writes_before = len(client.transactions[-1].writes)
    assert writes_before == 1
    again = notification_store.create_subscription(_spec(), now=NOW + timedelta(minutes=5))
    assert again == first, "a safe retry returns the stored record unchanged"
    assert client.transactions[-1].writes == [], "and writes nothing at all"
    try:
        notification_store.create_subscription(_spec(begin="07:00"), now=NOW)
    except notification_store.Conflict:
        pass
    else:
        raise AssertionError("the same id with a different definition must be refused")
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["begin"] == "06:45"


def test_a_create_against_a_tombstone_is_refused_rather_than_reported_unchanged():
    """The idempotency check must test LIVENESS before the definition digest.

    `_delete_body` does not clear `definition_digest` -- only the cleanup's redaction does -- so a
    delete followed by a same-definition re-POST looked like a safe retry and handed the tombstone
    back through a 2xx with `status: "deleted"` on it. The window is exactly between the tombstone
    and the cleanup completing, which is where nobody is looking.
    """
    client = _FakeClient()
    _install(client)
    notification_store.create_subscription(_spec(), now=NOW)
    notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW)
    try:
        notification_store.create_subscription(_spec(), now=NOW + timedelta(minutes=1))
    except notification_store.Conflict:
        pass
    else:
        raise AssertionError("re-creating a tombstoned id must be refused, not reported unchanged")
    assert client.transactions[-1].writes == []
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["status"] == "deleted"
    # An EXPIRED-but-not-deleted record is refused for the same reason: the id is spent.
    client.put(notification_store.SUBSCRIPTIONS, "old", _live_doc(subscription_id="old", now=NOW - timedelta(days=400)))
    try:
        notification_store.create_subscription(_spec(subscription_id="old"), now=NOW)
    except notification_store.Conflict:
        pass
    else:
        raise AssertionError("re-creating an expired id must be refused")


def test_an_amendment_does_not_cancel_the_pending_alert():
    """R13 is about deletion or expiry stopping later claims, and NOT about amendment.

    A version compare at claim time silently loses the alert: emit at v1 leaves D1 pending, the
    client amends to v2, the next cycle's emission at v2 finds no lifecycle transition and mints
    nothing, and the claim would then cancel D1 -- so the traveller is never told about a train that
    is still cancelled, and nothing logs as wrong. The version check belongs at emit.
    """
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    notification_store.replace_subscription(_spec(begin="07:00"), expected_version=1, now=NOW)
    # The next cycle's observation mints nothing -- no lifecycle transition -- so D1 is still the
    # only delivery there will ever be for this train.
    assert notification_store.commit_event(_emit(subscription_version=2), now=NOW).status == "unchanged"
    claim = notification_store.claim_delivery(delivery_id, now=NOW)
    assert claim.status == "ready", "an amendment must not lose the pending alert"


def test_an_amendment_that_moves_the_callback_does_cancel_the_pending_alert():
    """What an amendment CAN invalidate: a delivery addressed to a destination the client moved."""
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    notification_store.replace_subscription(
        _spec(destination_id="dest-b"), expected_version=1, now=NOW
    )
    claim = notification_store.claim_delivery(delivery_id, now=NOW)
    assert claim.status == "cancelled" and claim.reason == "destination"
    assert client.doc(notification_store.DELIVERIES, delivery_id)["state"] == "cancelled"


def test_a_delete_after_a_completed_cleanup_is_still_idempotent():
    """The redaction blanks the weekdays, so a strict parse turned the second DELETE into a 500."""
    client = _FakeClient()
    _install(client)
    notification_store.create_subscription(_spec(), now=NOW)
    notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW)
    assert notification_store.cleanup_subscription(SID, batch=100, now=NOW).done is True
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["days"] == []
    again = notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW + timedelta(days=1))
    assert again is not None and again.status == "deleted" and again.days == ()
    assert client.transactions[-1].writes == []


def test_the_test_marker_is_immutable():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc(test=True))
    _install(client)
    try:
        notification_store.replace_subscription(_spec(test=False), now=NOW)
    except notification_store.Conflict:
        pass
    else:
        raise AssertionError("flipping the test marker must require delete and re-create")
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["test"] is True


def test_an_amendment_resets_servability_but_not_the_cap_or_the_dedup_marks():
    client = _FakeClient()
    stored = dict(
        _live_doc(),
        servable=True,
        servable_checked_at=NOW,
        daily_deliveries={notification_store.oslo_day(NOW): 2},
        delivered={"ENT:1": NOW + timedelta(hours=10)},
    )
    client.put(notification_store.SUBSCRIPTIONS, SID, stored)
    _install(client)
    updated = notification_store.replace_subscription(_spec(to_stop="NSR:StopPlace:999"), now=NOW)
    # A changed route is a different servability question, so the stored answer AND its freshness
    # are both retired: "we do not know" with no timestamp, rather than a stale True with a fresh
    # one, which is precisely the shape R6 forbids reading as an all-clear.
    assert updated.servable is None and updated.servable_checked_at is None
    after = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert after["daily_deliveries"] == {notification_store.oslo_day(NOW): 2}, (
        "letting an amendment clear the cap would be a bypass anyone could drive from the API"
    )
    assert set(after["delivered"]) == {"ENT:1"}


def test_servability_never_refuses_and_records_its_freshness():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    unservable = notification_store.set_servability(SID, servable=False, expected_version=1, now=NOW)
    assert unservable.servable is False and unservable.status == "active"
    unknown = notification_store.set_servability(
        SID, servable=None, expected_version=1, now=NOW + timedelta(hours=6)
    )
    assert unknown.servable is None, "could not be established must never read as an all-clear"
    assert unknown.servable_checked_at == NOW + timedelta(hours=6)


def test_a_servability_verdict_for_a_replaced_route_is_refused_not_stamped():
    """The check is a ~20 s BigQuery question and a replace is one request; they can interleave.

    Ask about Oslo S -> Drammen, the client replaces the watch with Oslo S -> Ski while the
    question is in flight, the answer comes back and is written. Nothing in the record then says it
    is about the OLD route: `servable` reads as an answer and `servable_checked_at` reads as fresh,
    which is exactly the pairing R6 publishes so a client can judge a caveat's age. The client is
    told their new route has no direct service, with a timestamp from a second ago.

    `version` is the guard rather than the definition digest because a replace bumps it and a
    `touch` does not -- so a keep-alive during a slow check is correctly NOT a conflict, while every
    route change is. Enforced INSIDE the transaction, since a read-then-write outside it is the same
    race one layer up, and `expected_version` is REQUIRED rather than optional-with-a-default: an
    unguarded write must not be reachable by omission.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    notification_store.replace_subscription(_spec(to_stop="NSR:StopPlace:999"), now=NOW)
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["version"] == 2
    try:
        notification_store.set_servability(SID, servable=False, expected_version=1, now=NOW)
    except notification_store.Conflict:
        pass
    else:
        raise AssertionError("a verdict about a route that has since been replaced must not land")
    after = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert after["servable"] is None and after["servable_checked_at"] is None, (
        "the replace retired the answer AND its stamp; the late verdict must not restore either"
    )
    # And the current version still answers, so the guard refuses a stale writer and nothing else.
    assert notification_store.set_servability(SID, servable=True, expected_version=2, now=NOW).servable


def test_a_tombstone_stops_routing_and_a_repeated_delete_writes_nothing():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    tombstoned = notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW)
    assert tombstoned is not None and tombstoned.status == "deleted"
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["cleanup"]["state"] == "pending"
    again = notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW + timedelta(hours=1))
    assert again is not None and again.status == "deleted"
    assert client.transactions[-1].writes == [], "a retried DELETE must not bump a version forever"
    assert notification_store.delete_subscription("never-existed", client_id=CLIENT, now=NOW) is None


def test_cleanup_runs_after_the_tombstone_and_never_before_it():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    try:
        notification_store.cleanup_subscription(SID, now=NOW)
    except notification_store.Conflict:
        pass
    else:
        raise AssertionError(
            "reversed, a partial delete leaves a ROUTABLE subscription whose events are gone"
        )


def test_cleanup_deletes_in_batches_and_redacts_only_once_complete():
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, dict(_live_doc(), status="deleted", cleanup={}))
    _install(client)
    client.page(
        "cleanup_events",
        notification_store.EVENTS,
        [{"subscription_id": SID, "event_id": f"e{i}"} for i in range(2)],
    )
    result = notification_store.cleanup_subscription(SID, batch=2, now=NOW)
    # A full page means there may be more, so the task is retryable and NOT yet complete.
    assert result.done is False and result.deleted_events == 2
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert stored["cleanup"]["state"] == "pending" and stored["from_stop"] != ""
    client.pages["cleanup_events"] = []
    final = notification_store.cleanup_subscription(SID, batch=2, now=NOW + timedelta(minutes=1))
    assert final.done is True and final.attempts == 2
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert stored["cleanup"]["state"] == "done" and stored["cleanup"]["deleted_events"] == 2
    assert stored["from_stop"] == "" and stored["to_stop"] == "" and stored["days"] == []
    assert stored["external_ref"] == "" and stored["delivered"] == {}
    assert stored["subscription_id"] == SID and stored["client_id"] == CLIENT


def test_cleanup_reaches_all_three_related_collections():
    """Top-level collections carrying a subscription id, so all three need deleting explicitly.

    As subcollections they would be orphaned by the tombstone -- a document delete does not remove
    subcollections and a TTL field does not cascade into them -- and the delivery query would keep
    returning them.
    """
    names = {spec.name for spec in notification_store.QUERY_SPECS if spec.name.startswith("cleanup_")}
    assert names == {"cleanup_events", "cleanup_deliveries", "cleanup_subjects"}
    for spec in notification_store.QUERY_SPECS:
        if spec.name.startswith("cleanup_"):
            assert spec.equality == ("subscription_id",)
            assert spec.index_fields == (), "a single equality needs no composite index"


# ---- Retention and the published contract ------------------------------------------------------


def test_the_published_retention_numbers_are_the_ones_the_code_uses():
    """R19: published retention and deletion behavior matches what the code does."""
    subscription_schema = json.loads(
        (REPO / "contracts" / "notifications" / "v1" / "subscription.schema.json").read_text()
    )
    expires = subscription_schema["properties"]["expires_at"]["description"]
    assert f"{notification_store.SUBSCRIPTION_MAX_LIFETIME_DAYS}-day" in expires
    assert f"{notification_store.SUBSCRIPTION_INACTIVITY_DAYS}-day" in expires
    assert f"~{notification_store.TTL_RESIDUAL_HOURS} hours" in expires
    event_schema = json.loads(
        (REPO / "contracts" / "notifications" / "v1" / "event.schema.json").read_text()
    )
    validity = event_schema["properties"]["valid_until"]["description"]
    assert f"{notification_store.EVENT_VALIDITY_MINUTES}-minute" in validity
    assert notification_store.HISTORY_DAYS == 7
    assert f"{notification_store.HISTORY_DAYS}-day" in MODULE_SOURCE or "seven-day" in MODULE_SOURCE


def test_the_event_history_window_is_shorter_than_the_labelling_retry_window():
    """A published constraint rather than an oversight: retrospectively scoring client alerts is
    foreclosed by this retention choice, and lengthening it later is a contract change."""
    assert notification_store.HISTORY_DAYS < 10
    assert "ten-day retry window" in MODULE_SOURCE


def test_both_contract_schemas_parse_and_agree_with_the_module():
    for name in ("subscription", "event"):
        schema = json.loads(
            (REPO / "contracts" / "notifications" / "v1" / f"{name}.schema.json").read_text()
        )
        assert schema["$schema"].startswith("https://json-schema.org/draft/2020-12")
        assert schema["type"] == "object"
        assert schema["required"]
    event_schema = json.loads(
        (REPO / "contracts" / "notifications" / "v1" / "event.schema.json").read_text()
    )
    assert event_schema["properties"]["schema_version"]["const"] == notification_store.SCHEMA_VERSION
    assert set(event_schema["properties"]["type"]["enum"]) == set(notification_store.EVENT_TYPES)
    assert set(event_schema["properties"]["phase"]["enum"]) == {
        notification_store.PHASE_PREDICTED,
        notification_store.PHASE_CONFIRMED,
        notification_store.PHASE_WITHDRAWN,
    }
    # The grain the plan does not state, recorded where U6 and U7 will read it.
    assert "(subscription, train)" in event_schema["properties"]["alert_id"]["description"]


def test_the_stored_event_carries_the_envelope_the_contract_names():
    client, request = _emitted()
    event = notification_store.get_event(
        client.doc(notification_store.SUBJECTS, request.alert_id)["last_event_id"], now=NOW
    )
    assert event is not None
    schema = json.loads(
        (REPO / "contracts" / "notifications" / "v1" / "event.schema.json").read_text()
    )
    for key in schema["required"]:
        if key == "valid_until":
            assert event["valid_until"] == request.valid_until
            continue
        assert key in event, f"the contract requires {key}"
    assert event["payload"] == dict(request.payload)
    assert event["situations"] == list(request.situations)


# ---- Configuration guards ----------------------------------------------------------------------


def test_installing_the_fake_twice_still_restores_the_real_functions():
    """A guard on this suite's own harness, after it leaked once.

    Two `_install` calls in one test overwrote `_saved` with the already-patched functions, so
    `_uninstall` restored the FAKES and every later test in the process ran against them -- and the
    hermetic half cannot notice, because those tests want the fake. It showed up only as a fake
    transaction runner inside an emulator test. A leaked harness reads exactly like a real bug in
    the module, which is the trap this repo has written up for module-level ledgers.
    """
    real_run = notification_store._run_transaction
    real_query = notification_store._query
    real_client = notification_store._firestore_client
    _install(_FakeClient())
    _install(_FakeClient())
    _uninstall()
    assert notification_store._run_transaction is real_run
    assert notification_store._query is real_query
    assert notification_store._firestore_client is real_client


def test_every_firestore_call_carries_its_own_deadline():
    """KTD6 requires a per-CALL deadline, and the poller's wait is not one.

    ``varsel._WEBHOOK_LOAD_TIMEOUT_S`` bounds how long the ingestion thread WAITS on a worker; a
    worker parked past it is abandoned, not killed, because Python cannot cancel a thread inside a
    socket read. An abandoned thread is not free -- ``concurrent.futures.thread`` registers an atexit
    hook that JOINS its workers, so a store call with no deadline of its own delays pod termination
    by however long it hangs. That is what this deadline stops.

    Checked two ways, because either alone is weak. Statically: no read in the module reaches the
    client without ``timeout=``. Dynamically: the fake's ``get``/``stream``/``count`` all ASSERT the
    kwarg, so every path the suite exercises would fail rather than silently lose its deadline --
    which is why this is also the reason those fakes assert instead of accepting ``**kwargs``.
    """
    assert 0 < notification_store._CALL_DEADLINE_S < 5.0, (
        "it must sit under varsel._WEBHOOK_LOAD_TIMEOUT_S so a hung call surfaces as this "
        "module's own retryable DeadlineExceeded rather than as the caller's timeout"
    )
    varsel_wait = re.search(r"_WEBHOOK_LOAD_TIMEOUT_S\s*=\s*([0-9.]+)", VARSEL_SOURCE)
    assert varsel_wait, "varsel._WEBHOOK_LOAD_TIMEOUT_S not found -- was it renamed?"
    assert notification_store._CALL_DEADLINE_S < float(varsel_wait.group(1))
    # Every read site. `.get(` also matches the aggregation's and the dataclass helpers', so the
    # test is over the READ calls the module makes on a ref, a query or an aggregation.
    reads = re.findall(
        r"(?:\.stream\(|\.count\(alias=\"n\"\)\.get\(|ref\.get\(|transaction\.get\(|"
        r"\.(?:subscription|subject|delivery|event)\([a-z_]+\)\.get\()([^)]*)",
        MODULE_SOURCE,
    )
    assert len(reads) >= 12, f"only found {len(reads)} read sites -- did the shape change?"
    for args in reads:
        assert "_CALL_DEADLINE_S" in args, f"read without a deadline: {args!r}"


def test_an_ordinary_ttl_lag_is_not_logged_as_a_warning():
    """`expired > 0` is an ORDINARY condition and warning on it made every cycle noisy.

    TTL deletes an expired document "typically within 24 hours", so during that documented lag a
    perfectly healthy deployment warned on every cycle -- once per client per cycle for the
    standing-alert read, which the notification pass calls. `terraform/firestore.tf`'s own header
    makes the argument: a standing warning is how a real one stops being read.

    What must still warn is the pair that changes someone's behaviour: `truncated` (R21's ceiling
    binding -- and for the alert-state read it also switches the caller to its slow per-subject
    path) and `skipped` (a document this binary cannot read).
    """
    records: list[tuple[str, dict[str, Any]]] = []

    class _Recorder:
        def __getattr__(self, level: str):
            def _log(_msg: str, *_args: Any, **kwargs: Any) -> None:
                records.append((level, dict((kwargs.get("extra") or {}).get("fields") or {})))

            return _log

    saved = notification_store.logger
    notification_store.logger = _Recorder()  # type: ignore[assignment]
    try:
        client = _FakeClient()
        _install(client)
        # Expired rows only: the ordinary TTL lag.
        client.page(
            "load_active_subscriptions",
            notification_store.SUBSCRIPTIONS,
            [
                _live_doc(subscription_id="live"),
                _live_doc(subscription_id="idle", now=NOW - timedelta(days=120)),
            ],
        )
        notification_store.recipient_documents()
        # Only the records carrying structured fields are the load summary; a skipped document also
        # emits a bare DEBUG traceback with no `extra`, which is not what this test is about.
        summaries = [(level, fields) for level, fields in records if fields]
        assert [level for level, _ in summaries] == ["debug"], records
        assert summaries[0][1]["expired"] == 1
        assert summaries[0][1]["event"] == "notification_store_load", "still queryable under one name"

        # A document this binary cannot read still WARNS. It has to be LIVE but unparseable: the
        # liveness test runs before the parse, so a doc missing its horizons is counted as expired
        # and never reaches the parser at all -- which is how the first version of this fixture
        # quietly tested the wrong branch.
        records.clear()
        unreadable = _live_doc(subscription_id="bad")
        del unreadable["days"]
        client.page(
            "load_active_subscriptions",
            notification_store.SUBSCRIPTIONS,
            [_live_doc(subscription_id="good"), unreadable],
        )
        notification_store.recipient_documents()
        summaries = [(level, fields) for level, fields in records if fields]
        assert [level for level, _ in summaries] == ["warning"], records
        assert summaries[0][1]["skipped"] == 1 and summaries[0][1]["expired"] == 0

        # And so does R21's ceiling binding, on its own.
        records.clear()
        client.page(
            "load_active_subscriptions",
            notification_store.SUBSCRIPTIONS,
            [_live_doc(subscription_id=f"s{i}") for i in range(3)],
        )
        notification_store.active_subscriptions(limit=2, now=NOW)
        records.clear()
        client.pages["load_active_subscriptions"] = client.pages["load_active_subscriptions"][:1]
        saved_ceiling = notification_store.MAX_SUBSCRIPTIONS_PER_CYCLE
        try:
            notification_store.MAX_SUBSCRIPTIONS_PER_CYCLE = 1
            notification_store.recipient_documents()
        finally:
            notification_store.MAX_SUBSCRIPTIONS_PER_CYCLE = saved_ceiling
        summaries = [(level, fields) for level, fields in records if fields]
        assert [level for level, _ in summaries] == ["warning"], records
        assert summaries[0][1]["truncated"] is True
    finally:
        notification_store.logger = saved  # type: ignore[assignment]


def test_the_published_validity_wording_matches_where_each_term_is_enforced():
    """R19's rule applied to the event contract: the schema must not promise a term the value omits.

    Validity is "the earlier of a 60-minute horizon, the departure, and the subscription's
    lifetime", and at emit only the first two are applied -- the notification pass does not hold the
    subscription's own expiry. The third is enforced DURABLY and more tightly instead:
    `claim_delivery` re-reads the LIVE subscription immediately before dispatch and cancels a
    delivery whose subscription has ended, which a value computed at emit could never do (it would
    be a snapshot taken when the pass loaded its recipients, already stale by the length of the
    pass). So the published wording says where each term is enforced rather than implying all three
    land on the wire.
    """
    schema = json.loads(
        (REPO / "contracts" / "notifications" / "v1" / "event.schema.json").read_text()
    )
    text = schema["properties"]["valid_until"]["description"]
    assert "60-minute horizon" in text and "departure" in text
    assert "WHERE EACH TERM IS ENFORCED" in text
    assert "enforced at dispatch" in text, "the lifetime term must be attributed, not implied"
    assert "outlives it by up to an hour" in text, "the residual has to be stated, not hidden"
    # And the function still takes the term, for the callers that hold a live subscription.
    horizon = notification_store.event_validity(
        occurred_at=NOW, departure_at=None, subscription_expires_at=None
    )
    assert horizon == NOW + timedelta(minutes=notification_store.EVENT_VALIDITY_MINUTES)
    lifetime = NOW + timedelta(minutes=5)
    assert (
        notification_store.event_validity(
            occurred_at=NOW, departure_at=None, subscription_expires_at=lifetime
        )
        == lifetime
    )
    doc = notification_store.event_validity.__doc__ or ""
    assert "claim_delivery" in doc and "snapshot" in doc


def test_a_delivery_whose_subscription_expired_is_cancelled_at_dispatch():
    """The durable half of the validity contract, which is what makes the wording above true."""
    client, request = _emitted()
    delivery_id = client.doc(notification_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    # The subscription passes its inactivity horizon while the event is still valid.
    client.put(
        notification_store.SUBSCRIPTIONS, SID, _live_doc(now=NOW - timedelta(days=400))
    )
    claim = notification_store.claim_delivery(delivery_id, now=NOW)
    assert claim.status == "cancelled" and claim.reason == "expired"
    assert client.doc(notification_store.DELIVERIES, delivery_id)["state"] == "cancelled"


def test_the_emulator_step_is_wired_in_ci():
    """Without this, the loud skip below quietly becomes permanent.

    The same shape of guard `test_meldinger` uses over `validate_varsel_sql.sh`: a check that only
    ever runs by hand is a check nobody runs.

    The `actions/setup-java` grep is deliberately UNPINNED, and the trade is worth stating because
    it cost a review round. This suite first shipped `@v5` by analogy with the `checkout@v5`,
    `setup-python@v6` and `setup-node@v6` already in this job -- and setup-java has no v5: its
    majors are v2, v3, v4, v6. A grep for `actions/setup-java@v6` would have caught that; it would
    also fail on every legitimate bump, and this check exists to prove the STEP is present rather
    than to review its pins. Note the failure mode it cannot catch is loud anyway -- an unresolvable
    action fails the job immediately -- unlike the silent skip this test does catch.
    """
    assert "emulators:exec" in CI_YML
    assert "--project demo-" in CI_YML, "a demo- project is what makes the run credential-free"
    assert "--only firestore" in CI_YML
    assert "actions/setup-java" in CI_YML, "the emulator is a Java process"
    assert "test_notification_store.py" in CI_YML
    assert "--config ../../firebase.json" in CI_YML
    # And the ordinary loop must be able to import the module at all.
    assert "--with google-cloud-firestore" in CI_YML


def test_the_dependency_is_declared_in_all_three_places():
    """The image installs from one file, `uv sync` reads another, and CI's loop names a third."""
    requirements = (pathlib.Path(__file__).parent / "requirements.txt").read_text()
    pyproject = (REPO / "pyproject.toml").read_text()
    assert "google-cloud-firestore" in requirements
    # Split on a closing bracket at the START of a line: `anthropic[vertex]` carries one mid-line.
    api_group = pyproject.split("api = [", 1)[1].split("\n]", 1)[0]
    assert "google-cloud-firestore" in api_group
    assert CI_YML.count("--with google-cloud-firestore") >= 1
    lock = (REPO / "uv.lock").read_text()
    assert 'name = "google-cloud-firestore"' in lock, "uv sync was not re-run"


def test_the_database_id_is_the_same_literal_in_terraform_and_helm():
    """The one thing Terraform and the runtime cannot share.

    A mismatch is invisible in the worst way: the poller's webhook load is guarded, so it logs one
    warning per cycle and the deployment reads exactly like a healthy one with nothing registered.
    """
    tf_default = re.search(
        r'variable "notification_firestore_database"[\s\S]*?default\s*=\s*"([a-z0-9-]+)"', TF_VARIABLES
    )
    assert tf_default, "the terraform variable or its default was renamed"
    helm = re.search(r"NOTIFICATION_FIRESTORE_DATABASE:\s*([a-z0-9-]+)", HELM_VALUES)
    assert helm, "NOTIFICATION_FIRESTORE_DATABASE is not in the base helm values"
    assert tf_default.group(1) == helm.group(1)
    assert 'name        = var.notification_firestore_database' in FIRESTORE_TF


def test_the_notification_secrets_are_their_own_group_and_carry_no_dash():
    """Never appended to `varsel`: an ExternalSecret is all-or-nothing under LatestOrFail and the
    chart's envFrom secretRef is not optional, so one unresolvable name deletes the group's Secret
    and leaves the pod in CreateContainerConfigError -- which in the `varsel` group means Web Push
    off for real travellers over a feature that is switched off.

    The names are checked whether they are live or commented, because they land commented until
    their values exist in Secret Manager (which this unit cannot create). `live_secret_envs` is the
    admission rule: an environment goes in only after `gcloud secrets versions list` has shown a
    version of BOTH secrets in that project -- dev and prd on 2026-09-10 (version 1 of each).
    """
    live_secret_envs = {"dev", "prd"}
    for env in ("dev", "tst", "prd"):
        values = (REPO / "helm" / "kix-avvik" / "env" / f"values-kub-ent-{env}.yaml").read_text()
        live = _uncommented(values)
        for name in ("WEBHOOK_SIGNING_ROOT", "NOTIFICATION_KEY_PEPPER"):
            assert name in values, f"{env} does not name {name} at all"
            # The prose above the block names both secrets in order to explain them, so the
            # "not in the varsel group" half has to read the LIVE yaml.
            if env in live_secret_envs:
                assert name in live, f"{env} has the secret in Secret Manager but does not mount it"
            else:
                assert name not in live, (
                    f"{env} names {name} in a live secrets group -- the value does not exist in "
                    "Secret Manager yet, and an unresolvable name fails the whole ExternalSecret"
                )
            assert "-" not in name  # the common chart fails the template on a dashed secret name
        # Its own group under `secrets:`, as one contiguous block, live or commented: appended to
        # varsel's list instead it would take the VAPID keys down with it.
        assert re.search(
            r"^\s*#?\s*notifications:\s*\n\s*#?\s*-\s*WEBHOOK_SIGNING_ROOT\s*\n\s*#?\s*-\s*"
            r"NOTIFICATION_KEY_PEPPER\s*$",
            values,
            re.MULTILINE,
        ), f"{env} has no `notifications:` secrets group naming both secrets, live or commented"
        assert "VAPID_PRIVATE_KEY" in values or env == "tst", "tst alone has no keypair"


def test_the_webhook_gate_is_off_where_an_operator_looks():
    assert 'NOTIFICATION_WEBHOOKS_ENABLED: "false"' in HELM_VALUES
    main_source = (pathlib.Path(__file__).parent / "main.py").read_text()
    assert 'os.environ.get("NOTIFICATION_WEBHOOKS_ENABLED", "")' in main_source, (
        "unset must read as off -- the code default has to match the closed gate"
    )


def test_the_iam_grant_is_in_the_manifest_and_not_in_terraform():
    manifest = (REPO / ".entur" / "application.yaml").read_text()
    assert "roles/datastore.user" in manifest
    for tf in (REPO / "terraform").glob("*.tf"):
        assert "roles/datastore" not in _uncommented(tf.read_text()), (
            f"{tf.name} grants roles/datastore -- an IAM grant in terraform/ meets the "
            "only_assignable_roles condition, which carries no roles/datastore.* at all"
        )
    assert "google_project_iam_member" not in FIRESTORE_TF_CODE
    assert "google_bigquery_dataset_iam_member" not in FIRESTORE_TF_CODE


def test_the_database_is_named_and_abandoned_on_destroy():
    assert 'deletion_policy         = "ABANDON"' in FIRESTORE_TF, (
        "a destroy must not take the subscriptions with it -- nothing can rebuild them"
    )
    assert 'type        = "FIRESTORE_NATIVE"' in FIRESTORE_TF
    assert 'location_id = "europe-west1"' in FIRESTORE_TF
    assert 'app_engine_integration_mode = "DISABLED"' in FIRESTORE_TF
    assert 'var.environment == "prd" ? "DELETE_PROTECTION_ENABLED"' in FIRESTORE_TF
    assert 'var.environment == "prd" ? 1 : 0' in FIRESTORE_TF, "backups are prd only"
    assert 'retention = "3024000s"' in FIRESTORE_TF
    assert "daily_recurrence {}" in FIRESTORE_TF
    assert 'disable_on_destroy = false' in FIRESTORE_TF
    assert "google-beta" not in FIRESTORE_TF_CODE, "the pinned provider has all four resources GA"


def test_the_deny_all_rules_file_exists_and_denies():
    rules = _uncommented((REPO / "firestore.rules").read_text(), marker="//")
    assert "allow read, write: if false;" in rules
    # The header quotes the default Firebase template in order to refuse it, so this has to read
    # the code half -- the same reason `_uncommented` exists for the Terraform checks.
    assert "request.time <" not in rules, "the default Firebase template opens the database"
    assert "if true" not in rules
    firebase = json.loads((REPO / "firebase.json").read_text())
    assert firebase["firestore"]["rules"] == "firestore.rules"
    assert firebase["firestore"]["indexes"] == "firestore.indexes.json"


def test_the_deny_all_rules_are_deliberately_not_released_by_terraform():
    """The rules file is the emulator's and the tests' statement, NOT a Terraform-managed release --
    and that is a decision with a date, not an omission. For one day (2026-09-09/10) it was
    released as the named database's live ruleset; the refresh of that ruleset needs
    `firebaserules.rulesets.get`, which the PULL-REQUEST plan identity lacks while the push APPLY
    identity has it, so every Terraform PR after the apply failed its plan. Rules govern client-SDK
    access only, no Firebase app exists in these projects, and a database with no release denies
    every client anyway -- so the guard guarded nothing and cost every plan.

    Pinned in the shape that would otherwise rot: no `resource` of either kind (the `removed`
    blocks that forget them are fine, and go once every environment that held them has applied),
    the emulator still reads the file, and terraform.yml no longer triggers on it -- a workflow that
    plans on a rules edit while nothing consumes the edit is the same silent gap as the reverse.
    Re-adding the release is the right move THE DAY a Firebase app is registered here, together
    with the Team Plattform ask for the plan-reader permission -- see terraform/firestore.tf."""
    assert 'resource "google_firebaserules_ruleset"' not in FIRESTORE_TF_CODE
    assert 'resource "google_firebaserules_release"' not in FIRESTORE_TF_CODE
    assert 'resource "google_project_service" "firebaserules"' not in FIRESTORE_TF_CODE
    firebase = json.loads((REPO / "firebase.json").read_text())
    assert firebase["firestore"]["rules"] == "firestore.rules"
    assert "- 'firestore.rules'" not in TERRAFORM_YML, (
        "terraform.yml must not trigger on firestore.rules while nothing in terraform/ consumes it"
    )
    assert TERRAFORM_YML.count("- 'firestore.indexes.json'") == 2, (
        "both of terraform.yml's paths: lists must still carry firestore.indexes.json"
    )


# ---- Emulator-backed ---------------------------------------------------------------------------

_EMULATOR_DB: str | None = None


def _emulator_client() -> Any:
    """A credential-free client against the emulator, on the NAMED database where possible.

    Tries the same named database production uses, so the code path under test is the deployed one.
    Falls back to `(default)` if this emulator build does not route a named id, and records which
    was used -- the fallback is worth knowing about, because a named database is what production
    opens and `configure(database=...)` is the only difference.
    """
    global _EMULATOR_DB
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore

    refused: list[str] = []
    for database in ("notifications", "(default)"):
        client = firestore.Client(
            project=EMULATOR_PROJECT, database=database, credentials=AnonymousCredentials()
        )
        smoke = client.collection("_smoke").document("_smoke")
        try:
            smoke.set({"ok": True})
            smoke.delete()
        except Exception as exc:  # noqa: BLE001 - reported in the assertion below
            refused.append(f"{database}: {type(exc).__name__}: {exc}")
            continue
        _EMULATOR_DB = database
        return client
    raise AssertionError(
        "the Firestore emulator accepted a write on neither database id: " + "; ".join(refused)
    )


def _clear_emulator() -> None:
    database = _EMULATOR_DB or "(default)"
    url = (
        f"http://{EMULATOR_HOST}/emulator/v1/projects/{EMULATOR_PROJECT}"
        f"/databases/{database}/documents"
    )
    request = urllib.request.Request(url, method="DELETE")
    try:
        urllib.request.urlopen(request, timeout=10).read()
    except urllib.error.HTTPError as exc:  # pragma: no cover - surfaces as a test failure below
        raise AssertionError(f"could not clear the emulator: {exc}") from exc


def _emulator_setup() -> Any:
    client = _emulator_client()
    _clear_emulator()
    notification_store.reset_for_tests()
    notification_store.configure(client=client, clock=lambda: NOW)
    return client


def emulator_test_the_subscription_round_trip_survives_the_real_client():
    client = _emulator_setup()
    created = notification_store.create_subscription(_spec(), now=NOW)
    assert created.version == 1 and created.status == "active"
    assert created.expires_at is not None and created.expires_at > NOW
    fetched = notification_store.get_subscription(SID, client_id=CLIENT, now=NOW)
    assert fetched is not None and fetched.days == (0, 1, 2, 3, 4)
    # Idempotent retry against a real store: same record, and the version does not move.
    assert notification_store.create_subscription(_spec(), now=NOW).version == 1
    amended = notification_store.replace_subscription(_spec(begin="07:00"), expected_version=1, now=NOW)
    assert amended.version == 2 and amended.begin == "07:00"
    touched = notification_store.touch_subscription(SID, client_id=CLIENT, now=NOW + timedelta(days=1))
    assert touched.inactivity_expires_at is not None
    assert touched.inactivity_expires_at > created.inactivity_expires_at
    tombstoned = notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW)
    assert tombstoned is not None and tombstoned.status == "deleted"
    assert notification_store.get_subscription(SID, client_id=CLIENT, now=NOW) is None
    assert client is not None


def emulator_test_the_load_query_returns_only_live_subscriptions():
    _emulator_setup()
    notification_store.create_subscription(_spec(subscription_id="live"), now=NOW)
    notification_store.create_subscription(
        _spec(subscription_id="idle"), now=NOW - timedelta(days=120)
    )
    notification_store.create_subscription(_spec(subscription_id="gone"), now=NOW)
    notification_store.delete_subscription("gone", client_id=CLIENT, now=NOW)
    loaded = notification_store.active_subscriptions(now=NOW)
    assert [s.subscription_id for s in loaded.subscriptions] == ["live"]
    # The idle one was returned by the query and dropped by the reader: TTL has not run.
    assert loaded.expired == 1
    documents = notification_store.recipient_documents()
    assert [d["recipient_id"] for d in documents] == ["live"]


def emulator_test_concurrent_emissions_converge_on_one_event():
    """Four writers on one subject: every permitted outcome, and no second event under any of them.

    **This cannot happen in production, which is exactly why the assertion has to be the CONTRACT
    and not the scheduler's outcome.** KTD6's single writer is nameable rather than hoped for: one
    poller thread calls the pass, no route invokes it, and the deployment is one replica under the
    Recreate strategy, so two cycles cannot overlap and contention on one subject document is
    structurally impossible. The check exists to pin what the store does if that ever stops being
    true.

    THE FIRST VERSION OF THIS TEST WAS FLAKY, and a flaky check is worse than no check -- it teaches
    people to re-run red builds. It asserted that all four callers returned. Under the emulator's
    simple locking -- and under production Standard edition's pessimistic concurrency, which is the
    same shape -- four callers contending on one document LEGITIMATELY exhaust the client's five
    attempts, and three of four did. Fewer threads or a sleep would only make the flake rarer.

    So a caller's permitted outcomes are exactly the two the store already classifies: commit, and
    return the same ``event_id`` every other success returns; or raise something
    :func:`notification_store.is_retryable` calls True, which the next poller cycle retries, as
    everything in this repo does. What holds whatever the scheduler did: every thread finished, at
    most one distinct event exists, every failure is retryable, and the store's event and delivery
    counts agree with the number of commits. Then the next cycle's retries -- one per squeezed-out
    caller, plus one -- converge on exactly one event and one delivery, with no new revision, which
    is the original point of the check.

    TWO WIDTHS, and the measurement behind that is worth stating precisely, because it is stronger
    than "the outcome varies by machine". Run five times against this emulator on 2026-09-08, the
    two widths between them produced ALL THREE regimes -- every caller committing, every caller
    exhausting, and a mix (1 committed / 7 retryable) -- and the regime changed from run to run on
    one machine with nothing else altered. So there is no width at which any outcome can be
    asserted, and the earlier reading that four threads always succeed here was luck rather than a
    property. Both widths are run and the same invariants asserted over both, including that a
    store holding NO event at all is a permitted, consistent outcome which the next cycle repairs.

    The suite header's statement stands: this is shape and idempotency, not evidence about
    contention, and atomicity is argued from the transaction contract.
    """
    for width in (4, 8):
        _one_contended_round(width)


def _one_contended_round(width: int) -> None:
    client = _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    request = _emit()
    # Keyed by thread index rather than appended, so a failure names WHICH caller it came from.
    results: dict[int, notification_store.EventCommit] = {}
    errors: dict[int, BaseException] = {}

    def _emit_once(index: int) -> None:
        try:
            results[index] = notification_store.commit_event(request, now=NOW)
        except BaseException as exc:  # noqa: BLE001 - classified below
            errors[index] = exc

    threads = [threading.Thread(target=_emit_once, args=(i,)) for i in range(width)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    # ---- invariants that hold whatever the scheduler decided -----------------------------------
    assert len(results) + len(errors) == width, "a thread neither returned nor raised"
    for index, exc in errors.items():
        # The one classification that matters: an exhausted transaction raises a plain ValueError,
        # so a handler keyed on google.api_core exceptions would mark a contended subject `failed`
        # and never deliver it.
        assert notification_store.is_retryable(exc), f"thread {index} failed terminally: {exc!r}"
    committed = {r.event_id for r in results.values() if r.event_id}
    assert len(committed) <= 1, f"two different events for one subject: {committed}"
    assert len({r.revision for r in results.values()}) <= 1, "one revision across all callers"
    assert not {r.status for r in results.values()} - {"created", "unchanged"}
    assert len([r for r in results.values() if r.status == "created"]) == len(committed)
    events = list(client.collection(notification_store.EVENTS).stream())
    deliveries = list(client.collection(notification_store.DELIVERIES).stream())
    assert len(events) == len(committed), (len(events), committed)
    assert len(deliveries) == len(events)
    # Printed rather than inferred: when this ever fails, the regime it failed in is the first
    # thing anyone will want, and it is not recoverable from the assertion message.
    print(
        f"      (width {width}: {len(results)} committed, {len(errors)} retryable, "
        f"{len(events)} event(s) stored)"
    )

    # ---- the next cycle is the retry, for however many callers were squeezed out ----------------
    # `+ 1` so the all-squeezed-out regime -- no event in the store at all -- also gets the cycle
    # that creates one, and the mixed regime gets one cycle more than it needs, which must be a
    # no-op. Both are the poller's own retry model: one attempt per pass, the next pass is the retry.
    for _ in range(len(errors) + 1):
        retried = notification_store.commit_event(request, now=NOW + timedelta(minutes=1))
        assert retried.status in ("created", "unchanged"), retried.status
        assert retried.event_id
    final_events = list(client.collection(notification_store.EVENTS).stream())
    assert len(final_events) == 1, "no retry may mint a second event about one train"
    assert len(list(client.collection(notification_store.DELIVERIES).stream())) == 1
    event_id = final_events[0].to_dict()["event_id"]
    for index, result in results.items():
        assert result.event_id == event_id, f"thread {index} saw a different event"
    # And it stays converged: a further cycle mints nothing at all.
    settled = notification_store.commit_event(request, now=NOW + timedelta(minutes=2))
    assert settled.status == "unchanged" and settled.event_id == event_id
    assert len(list(client.collection(notification_store.EVENTS).stream())) == 1


def emulator_test_a_delete_racing_an_emission_rejects_the_obsolete_version():
    client = _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW)
    commit = notification_store.commit_event(_emit(subscription_version=1), now=NOW)
    assert commit.status == "obsolete" and commit.reason == "deleted"
    assert list(client.collection(notification_store.EVENTS).stream()) == []
    # And an amendment that moved the version rejects an emission holding the old one.
    notification_store.create_subscription(_spec(subscription_id="s2"), now=NOW)
    notification_store.replace_subscription(
        _spec(subscription_id="s2", begin="07:00"), expected_version=1, now=NOW
    )
    stale = notification_store.commit_event(
        _emit(subscription_id="s2", subscription_version=1), now=NOW
    )
    assert stale.status == "obsolete" and stale.reason == "version"
    assert list(client.collection(notification_store.EVENTS).stream()) == []


def emulator_test_the_full_delivery_lifecycle_spends_the_cap_once():
    client = _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    commit = notification_store.commit_event(_emit(), now=NOW)
    pending = notification_store.pending_deliveries(now=NOW)
    assert [d.delivery_id for d in pending.ready] == [commit.delivery_id]
    claim = notification_store.claim_delivery(commit.delivery_id, now=NOW)
    # The claim RESERVED a slot, so the allowance it reports is what is left after it: 3, not 4.
    assert claim.status == "ready" and claim.cap_remaining == 3
    failure = notification_store.mark_delivery_failed(
        commit.delivery_id, error="timeout", retryable=True, retry_after_s=30, now=NOW
    )
    assert failure.state == "pending" and failure.attempts == 1
    # ... and the failure RELEASED it, so nothing is held while the delivery waits for its retry.
    held = notification_store.get_subscription(SID, client_id=CLIENT, now=NOW)
    assert held is not None and notification_store.cap_remaining(
        {"daily_deliveries": dict(held.daily_deliveries),
         "daily_reservations": dict(held.daily_reservations)},
        NOW,
    ) == 4
    # The backoff is a QUERY filter, so it survives this cycle boundary.
    assert notification_store.pending_deliveries(now=NOW).ready == ()
    later = NOW + timedelta(seconds=31)
    assert [d.delivery_id for d in notification_store.pending_deliveries(now=later).ready] == [
        commit.delivery_id
    ]
    sent = notification_store.mark_delivery_sent(commit.delivery_id, now=later)
    assert sent.state == "sent" and sent.slot_spent is True and sent.attempts == 2
    subscription = notification_store.get_subscription(SID, client_id=CLIENT, now=later)
    assert subscription is not None
    assert notification_store.cap_remaining(
        {"daily_deliveries": dict(subscription.daily_deliveries)}, later
    ) == 3
    assert list(subscription.delivered) == [commit.alert_id]
    assert notification_store.pending_deliveries(now=later).ready == ()
    assert client is not None


def emulator_test_concurrent_claims_cannot_overspend_the_daily_cap():
    """The defect's own shape, against a real store: claims wider than the cap, all at once.

    This is the check the fake cannot make. The reservation works because two claims for one
    subscription both WRITE that document, so Standard edition's pessimistic concurrency serialises
    them and the second reads the first's reservation instead of the same free slot -- a property of
    the transaction, not of the code, and only a real client exercises it.

    Asserted as the CONTRACT rather than an outcome, for the reason
    `emulator_test_concurrent_emissions_converge_on_one_event` records at length: a contended
    transaction legitimately exhausts the client's five attempts, so a caller either commits or
    raises something retryable. What holds unconditionally is the bound -- **never more than the cap
    is admitted** -- which is the thing that was broken.
    """
    client = _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    ids: list[str] = []
    for i in range(notification_store.MAX_ALERT_DELIVERIES_PER_DAY + 4):
        train = notification_store.TrainRef(**{**TRAIN.__dict__, "stop_order": 20 + i})
        commit = notification_store.commit_event(_emit(train=train), now=NOW)
        assert commit.delivery_id is not None
        ids.append(commit.delivery_id)

    ready: dict[str, str] = {}
    errors: dict[str, BaseException] = {}

    def _claim(delivery_id: str) -> None:
        try:
            ready[delivery_id] = notification_store.claim_delivery(delivery_id, now=NOW).status
        except BaseException as exc:  # noqa: BLE001 - classified below
            errors[delivery_id] = exc

    threads = [threading.Thread(target=_claim, args=(d,)) for d in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)

    assert len(ready) + len(errors) == len(ids), "a thread neither returned nor raised"
    for delivery_id, exc in errors.items():
        assert notification_store.is_retryable(exc), f"{delivery_id}: {exc!r}"
    admitted = [d for d, status in ready.items() if status == "ready"]
    assert set(ready.values()) <= {"ready", "capped"}, ready
    # THE BOUND. Before the reservation this could reach `cap + workers - 1`.
    assert len(admitted) <= notification_store.MAX_ALERT_DELIVERIES_PER_DAY, (
        f"{len(admitted)} claims admitted against a cap of "
        f"{notification_store.MAX_ALERT_DELIVERIES_PER_DAY}"
    )
    stored = client.collection(notification_store.SUBSCRIPTIONS).document(SID).get().to_dict()
    assert len(stored.get("daily_reservations") or {}) == len(admitted)
    assert notification_store.cap_remaining(stored, NOW) == (
        notification_store.MAX_ALERT_DELIVERIES_PER_DAY - len(admitted)
    ), "the allowance must agree with what was admitted, whatever the scheduler admitted"
    print(f"      ({len(admitted)} admitted, {len(ready) - len(admitted)} capped, "
          f"{len(errors)} retryable)")

    # Every admitted claim now sends, and the confirmed count matches exactly -- no double charge.
    # `.get(day, 0)` and `or {}`, because `len(admitted)` can legitimately be ZERO: every claim
    # contending on one subscription document can exhaust the client's five attempts, and an
    # assertion that indexed the day's key outright failed on exactly that regime the first time
    # this ran. Same lesson as the emission check above -- assert the contract, never the outcome
    # the scheduler picked.
    for delivery_id in admitted:
        assert notification_store.mark_delivery_sent(delivery_id, now=NOW).slot_spent is True
    settled = client.collection(notification_store.SUBSCRIPTIONS).document(SID).get().to_dict()
    assert (settled.get("daily_deliveries") or {}).get(notification_store.oslo_day(NOW), 0) == len(
        admitted
    )
    assert (settled.get("daily_reservations") or {}) == {}


def emulator_test_an_unreported_claim_is_reclaimed_next_cycle():
    """The crash case against a real store: claim, never report, then the next cycle."""
    client = _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    commit = notification_store.commit_event(_emit(), now=NOW)
    assert notification_store.claim_delivery(commit.delivery_id, now=NOW).status == "ready"
    stored = client.collection(notification_store.SUBSCRIPTIONS).document(SID).get().to_dict()
    assert len(stored["daily_reservations"]) == 1
    assert notification_store.cap_remaining(stored, NOW) == (
        notification_store.MAX_ALERT_DELIVERIES_PER_DAY - 1
    )
    # Nothing confirms and nothing releases. One cycle later the slot is free again, and the next
    # claim's own write prunes the lapsed entry.
    next_cycle = NOW + timedelta(seconds=296)
    assert notification_store.cap_remaining(stored, next_cycle) == (
        notification_store.MAX_ALERT_DELIVERIES_PER_DAY
    )
    assert notification_store.claim_delivery(commit.delivery_id, now=next_cycle).status == "ready"
    reclaimed = client.collection(notification_store.SUBSCRIPTIONS).document(SID).get().to_dict()
    assert set(reclaimed["daily_reservations"]) == {commit.delivery_id}


def emulator_test_clearing_a_run_survives_the_real_client():
    _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    notification_store.commit_event(_emit(), now=NOW)
    alert_id = _emit().alert_id
    assert notification_store.clear_below_threshold(alert_id, now=NOW) is False
    assert notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + _PERIOD).status == "counting"
    alerts = notification_store.client_alerts(CLIENT, now=NOW + _PERIOD)
    assert set(alerts.open_below_threshold) == {alert_id}
    assert notification_store.clear_below_threshold(alert_id, now=NOW + 2 * _PERIOD) is True
    assert notification_store.client_alerts(CLIENT, now=NOW + 2 * _PERIOD).open_below_threshold == {}
    subject = notification_store.get_subject(alert_id, now=NOW + 2 * _PERIOD)
    assert subject is not None
    assert subject.below_threshold_cycles == 0 and subject.below_threshold_at is None
    assert subject.phase == notification_store.PHASE_PREDICTED, "a recovery is not a transition"
    # So the next dip is a FIRST dip, inside the window the approximation would have got wrong.
    again = notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + 3 * _PERIOD)
    assert again.status == "counting" and again.below_threshold_cycles == 1


def emulator_test_a_confirmation_supersedes_the_forecast_and_is_cap_exempt():
    _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    forecast = notification_store.commit_event(_emit(), now=NOW)
    notification_store.mark_delivery_sent(forecast.delivery_id, now=NOW)
    confirmed = notification_store.commit_event(
        _emit(phase=notification_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=5)
    )
    assert confirmed.status == "created" and confirmed.event_type == notification_store.EVENT_UPDATED
    assert confirmed.revision > forecast.revision
    assert confirmed.cap_exempt is True
    sent = notification_store.mark_delivery_sent(confirmed.delivery_id, now=NOW + timedelta(minutes=5))
    assert sent.slot_spent is False
    # A predicted observation after a confirmation mints nothing at all.
    assert notification_store.commit_event(_emit(), now=NOW + timedelta(minutes=6)).status == "unchanged"


def emulator_test_the_ceiling_binds_and_counts_only_live_rows():
    _emulator_setup()
    notification_store.create_subscription(_spec(subscription_id="a"), now=NOW)
    saved = notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT
    try:
        notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT = 1
        try:
            notification_store.create_subscription(_spec(subscription_id="b"), now=NOW)
        except notification_store.QuotaExceeded:
            pass
        else:
            raise AssertionError("the ceiling must bind against a real store")
        # A tombstoned row does not count, so the client is not locked out by dead records.
        notification_store.delete_subscription("a", client_id=CLIENT, now=NOW)
        notification_store.create_subscription(_spec(subscription_id="b"), now=NOW)
    finally:
        notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT = saved
    assert notification_store.get_subscription("b", client_id=CLIENT, now=NOW) is not None


def emulator_test_one_client_cannot_list_or_touch_anothers_subscriptions():
    _emulator_setup()
    notification_store.create_subscription(_spec(subscription_id="mine"), now=NOW)
    notification_store.create_subscription(
        _spec(subscription_id="theirs", client_id="client-b"), now=NOW
    )
    mine = notification_store.list_subscriptions(client_id=CLIENT, now=NOW)
    assert [s.subscription_id for s in mine] == ["mine"]
    theirs = notification_store.list_subscriptions(client_id="client-b", now=NOW)
    assert [s.subscription_id for s in theirs] == ["theirs"]
    try:
        notification_store.delete_subscription("theirs", client_id=CLIENT, now=NOW)
    except notification_store.Forbidden:
        pass
    else:
        raise AssertionError("a foreign client must not tombstone this record")
    assert notification_store.get_subscription("theirs", client_id="client-b", now=NOW) is not None


def emulator_test_the_list_cursor_pages_against_the_real_client():
    """`start_after` is the one Firestore API this module gained, and only a real client can say.

    Everything else in the paged read is checkable against a fake, but the cursor's SEMANTICS are
    not: the hermetic double models it as "everything after this path in my own sorted rows", which
    is a claim about Firestore rather than a check of it. The three rows below are created at ONE
    instant on purpose -- `created_at` is the only declared ordering, so the page boundary falls on
    a tie, and a cursor built from field values alone could not cut it in one place. A snapshot
    cursor carries `__name__` too, which is what makes the tie deterministic and the page
    non-overlapping.

    The route reaches this with `_requested_id`, so `after` is the stored DOCUMENT id -- which in the
    store is the spec's own `subscription_id`; `notification_api` is where the `client~ref` namespacing
    happens.
    """
    _emulator_setup()
    for reference in ("a", "b", "c"):
        notification_store.create_subscription(_spec(subscription_id=reference), now=NOW)

    first = notification_store.list_subscriptions(client_id=CLIENT, limit=1, now=NOW)
    assert [s.subscription_id for s in first] == ["a"], first
    second = notification_store.list_subscriptions(client_id=CLIENT, limit=1, after="a", now=NOW)
    assert [s.subscription_id for s in second] == ["b"], second
    # `limit + 1` is what the route asks for, so check the shape it actually uses.
    rest = notification_store.list_subscriptions(client_id=CLIENT, limit=2, after="a", now=NOW)
    assert [s.subscription_id for s in rest] == ["b", "c"], rest
    assert notification_store.list_subscriptions(client_id=CLIENT, limit=2, after="c", now=NOW) == []

    # A cursor that names nothing this client can page from is `NotFound`, which the route turns
    # into a 400: "that watch is gone" and "nothing matched" are different answers.
    for missing in ("never-existed", "a-tombstoned-one"):
        try:
            notification_store.list_subscriptions(client_id=CLIENT, limit=1, after=missing, now=NOW)
        except notification_store.NotFound:
            pass
        else:
            raise AssertionError(f"paging from {missing!r} must not silently return page one")
    notification_store.delete_subscription("b", client_id=CLIENT, now=NOW)
    try:
        notification_store.list_subscriptions(client_id=CLIENT, limit=1, after="b", now=NOW)
    except notification_store.NotFound:
        pass
    else:
        raise AssertionError("a tombstoned cursor is gone, not the start of the listing")


def emulator_test_cleanup_deletes_every_related_document_and_redacts_the_tombstone():
    client = _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    for order in (3, 4, 5):
        train = notification_store.TrainRef(**{**TRAIN.__dict__, "stop_order": order})
        notification_store.commit_event(_emit(train=train), now=NOW)
    assert len(list(client.collection(notification_store.EVENTS).stream())) == 3
    notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW)
    result = notification_store.cleanup_subscription(SID, batch=100, now=NOW)
    assert result.done is True
    assert result.deleted_events == 3 and result.deleted_deliveries == 3
    assert result.deleted_subjects == 3
    for collection in (notification_store.EVENTS, notification_store.DELIVERIES, notification_store.SUBJECTS):
        assert list(client.collection(collection).stream()) == [], collection
    tombstone = client.collection(notification_store.SUBSCRIPTIONS).document(SID).get().to_dict()
    assert tombstone["cleanup"]["state"] == "done"
    assert tombstone["from_stop"] == "" and tombstone["external_ref"] == ""
    assert tombstone["subscription_id"] == SID


def emulator_test_a_withdrawal_needs_a_run_and_a_recovery_breaks_it():
    """Both halves of the run rule against the real client, not just the counting half.

    The dip/recover/dip case is the behaviour that CHANGED, so it belongs here as well as in the
    hermetic half: the count was a lifetime total, and a recovery in the middle of it withdrew an
    alert on what was effectively a single dip.
    """
    _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    notification_store.commit_event(_emit(), now=NOW)
    alert_id = _emit().alert_id

    # A run of two: withdrawn on the second.
    assert notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + _PERIOD).status == "counting"
    final = notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + 2 * _PERIOD)
    assert final.status == "created" and final.event_type == notification_store.EVENT_WITHDRAWN
    subject = notification_store.get_subject(alert_id, now=NOW + 2 * _PERIOD)
    assert subject is not None and subject.phase == notification_store.PHASE_WITHDRAWN
    assert subject.below_threshold_cycles == 0 and subject.below_threshold_at is None

    # A fresh alert, then a dip, an hour of recovery, and a dip: nothing is withdrawn.
    _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    notification_store.commit_event(_emit(), now=NOW)
    assert notification_store.record_below_threshold(_WITHDRAWAL, now=NOW + _PERIOD).status == "counting"
    recovered = NOW + timedelta(hours=1)
    assert notification_store.commit_event(_emit(), now=recovered).status == "unchanged"
    again = notification_store.record_below_threshold(_WITHDRAWAL, now=recovered + _PERIOD)
    assert again.status == "counting" and again.below_threshold_cycles == 1
    subject = notification_store.get_subject(alert_id, now=recovered + _PERIOD)
    assert subject is not None and subject.phase == notification_store.PHASE_PREDICTED


# ---- Runner ------------------------------------------------------------------------------------



# ---- Per-client ceilings and the channel field (2026-09-09) -----------------------------------


def test_the_active_subscription_ceiling_is_the_callers_when_given():
    """`notification_auth.Quotas.max_active_subscriptions` is what the API passes; the module
    constant is only the default, resolved at call time."""
    client = _FakeClient()
    refs = notification_store._Refs(client)
    rows = [
        (f"{notification_store.SUBSCRIPTIONS}/s{i}", _live_doc(subscription_id=f"s{i}"))
        for i in range(2)
    ]
    txn = _Txn()
    try:
        with _NoClockNoLog():
            notification_store._create_body(
                txn, refs, _Page(client, rows), spec=_spec(), now=NOW, max_active=2
            )
    except notification_store.QuotaExceeded as exc:
        assert "(ceiling 2)" in str(exc), str(exc)
    else:
        raise AssertionError("two live subscriptions against a ceiling of two must refuse a third")
    assert txn.writes == [], "the refusal must precede every write"
    with _NoClockNoLog():
        outcome, doc = notification_store._create_body(
            txn, refs, _Page(client, rows), spec=_spec(), now=NOW, max_active=3, daily_cap=9
        )
    assert outcome == "created"
    assert doc["daily_cap"] == 9 and doc["channel"] == notification_store.CHANNEL_WEBHOOK


def test_the_create_ceiling_query_limit_is_the_callers_ceiling():
    client = _FakeClient()
    _install(client)
    notification_store.create_subscription(_spec(), now=NOW, max_active=7)
    _name, _equality, _ineq, limit = client.query_calls[0]
    assert limit == 7, "R21: the count read is bounded by the ceiling it enforces"


def test_the_daily_cap_is_written_read_and_refreshed_on_every_client_write():
    client = _FakeClient()
    _install(client)
    created = notification_store.create_subscription(_spec(), now=NOW, daily_cap=6)
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert stored["daily_cap"] == 6 and created.daily_cap == 6
    assert notification_store.cap_remaining(stored, NOW) == 6
    # Replace and touch carry the client's cap of the day; neither touches it when not given.
    replaced = notification_store.replace_subscription(_spec(end="09:00"), now=NOW, daily_cap=2)
    assert replaced.daily_cap == 2
    assert client.doc(notification_store.SUBSCRIPTIONS, SID)["daily_cap"] == 2
    touched = notification_store.touch_subscription(SID, client_id=CLIENT, now=NOW, daily_cap=3)
    assert touched.daily_cap == 3
    untouched = notification_store.touch_subscription(SID, client_id=CLIENT, now=NOW)
    assert untouched.daily_cap == 3
    # A document written before the field existed keeps /varsel's four -- never zero.
    legacy = {k: v for k, v in stored.items() if k != "daily_cap"}
    default = notification_store.MAX_ALERT_DELIVERIES_PER_DAY
    assert notification_store.daily_cap_of(legacy) == default
    assert notification_store.cap_remaining(legacy, NOW) == default
    assert notification_store._subscription_from_doc(legacy).daily_cap == default
    assert notification_store.daily_cap_of({"daily_cap": "seven"}) == default
    assert notification_store.daily_cap_of({"daily_cap": 0}) == default
    assert notification_store.daily_cap_of({"daily_cap": True}) == default
    # An unreadable counter still fails closed -- against the document's own cap.
    assert notification_store.cap_remaining({"daily_cap": 6, "daily_deliveries": {notification_store.oslo_day(NOW): "x"}}, NOW) == 0


def test_a_subscription_document_names_its_channel_and_an_old_one_reads_as_webhook():
    doc = _live_doc()
    assert doc["channel"] == notification_store.CHANNEL_WEBHOOK == "webhook"
    assert notification_store._subscription_from_doc(doc).channel == "webhook"
    legacy = {k: v for k, v in doc.items() if k != "channel"}
    assert notification_store._subscription_from_doc(legacy).channel == "webhook"
    assert notification_store.CHANNELS == ("webhook", "web_push")
    # The recipient document the poller reads is unchanged: `varsel` pins its exact key set.
    recipient = notification_store._subscription_from_doc(doc).as_recipient_document(NOW)
    assert "channel" not in recipient and "daily_cap" not in recipient


def test_the_served_list_grows_with_the_clients_cap_and_never_shrinks_below_the_default():
    client = _FakeClient()
    _install(client)
    _alert_page(client, [_state(f"a{i}", notification_store.PHASE_PREDICTED) for i in range(6)])
    default = len(notification_store.subscription_alerts(SID, client_id=CLIENT, now=NOW).subjects)
    assert default == notification_store.MAX_ALERTS_SERVED == 4
    wider = notification_store.subscription_alerts(SID, client_id=CLIENT, served=6, now=NOW)
    assert len(wider.subjects) == 6 and wider.n_active == 6
    narrower = notification_store.subscription_alerts(SID, client_id=CLIENT, served=1, now=NOW)
    assert len(narrower.subjects) == 4, "a cap below the default never hides more than the default"
    grouped = notification_store.client_alerts(CLIENT, served=6, now=NOW)
    assert len(grouped.snapshots[SID].subjects) == 6

# ---- Store spans: one per Firestore call, carrying its interpreter cost (R16, KTD2) ------------


class _SpanRecorder:
    """Stands in for `tracing_setup.span`, recording what the store asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, name: str, attributes=None, **kwargs):
        self.calls.append((name, dict(attributes or {}), dict(kwargs)))
        from contextlib import nullcontext

        return nullcontext()


def test_a_transaction_opens_one_measured_span_that_refuses_to_record_its_exception():
    """KTD2 sizes the widened emit pass in INTERPRETER ms per transaction, and there is nowhere
    else to read that: a Firestore transaction is not pure socket wait — the body, the protobuf
    and the commit are all bytecode under KTD17, and at 3 ms each 20 000 transitions are 60 s of
    the one interpreter. So the span measures itself (`measure=True`) rather than carrying a count.

    ONE span covers all five of the client's internal retries, deliberately: what the pass is
    charged is what the call cost it, and a per-attempt span would report the cheapest attempt.

    `record_exception=False` is the redaction clause. A Firestore error message names the document
    path and a `subscription_id` embeds the client's own reference, so neither the span event nor
    the status description may carry it — and the two flags move together because both do.
    """
    from google.cloud import firestore

    class _ClientWithTransaction(_FakeClient):
        def transaction(self):
            return _Txn()

    recorder = _SpanRecorder()
    notification_store.configure(
        client=_ClientWithTransaction(), database=None, span=recorder
    )
    before = firestore.transactional
    firestore.transactional = lambda fn: fn  # the retry wrapper needs a real Transaction
    try:
        assert notification_store._run_transaction(lambda txn: "committed") == "committed"
    finally:
        firestore.transactional = before
        notification_store.reset_for_tests()
    assert [name for name, _, _ in recorder.calls] == ["firestore_transaction"]
    _, attributes, kwargs = recorder.calls[0]
    assert attributes == {}, "the cost is measured at close, never guessed at open"
    assert kwargs == {"measure": True, "record_exception": False}, kwargs


def test_a_streamed_page_opens_its_own_span_and_returns_the_page_unchanged():
    """The other half of "one span per Firestore call". The streams are the reads the mirror,
    the delivery scan and the servability re-check are made of, and they were invisible: a dev
    trace showed `notification_webhooks` with nothing under it at all.

    The helper must be transparent — same list, same order, same timeout — because it replaced
    eight bare `list(query.stream(timeout=_CALL_DEADLINE_S))` call sites.
    """

    class _Query:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def stream(self, timeout=None):
            self.timeouts.append(timeout)
            return iter(["a", "b", "c"])

    recorder = _SpanRecorder()
    query = _Query()
    notification_store.configure(client=_FakeClient(), database=None, span=recorder)
    try:
        assert notification_store._stream(query) == ["a", "b", "c"]
    finally:
        notification_store.reset_for_tests()
    assert query.timeouts == [notification_store._CALL_DEADLINE_S]
    assert [name for name, _, _ in recorder.calls] == ["firestore_stream"]


def test_without_an_injected_span_the_store_behaves_exactly_as_before():
    """The seam defaults off, and the store must not know the difference — the same rule every
    other injected capability in this repo follows, and the one that keeps `notification_store`
    free of an import of `tracing_setup` (which would run that module's import-time setup())."""

    class _Query:
        def stream(self, timeout=None):
            return iter([1, 2])

    notification_store.reset_for_tests()
    assert notification_store._span_provider is None
    assert notification_store._stream(_Query()) == [1, 2]


def test_every_streamed_read_sits_under_a_span():
    """A new bare `query.stream(...)` would be an untraced Firestore call, and nothing about it
    would look wrong — the store would simply stop accounting for one of its own reads, and the
    capacity number it feeds would be quietly low.

    The rule is checked where it can be checked cheaply: every line that streams under the call
    deadline must sit within a few lines of a `firestore_stream` span. Two such lines exist by
    design — `_stream`'s own body, and `_count`'s fallback, which counts without materialising the
    page and so cannot go through the helper — and both open that span themselves. Comment lines
    and the helper's docstring, which quotes the shape it replaced, are excluded.
    """
    lines = MODULE_SOURCE.split("\n")
    unguarded = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if ".stream(timeout=_CALL_DEADLINE_S)" not in stripped:
            continue
        if stripped.startswith("#") or "``" in stripped:
            continue  # a comment or the docstring quoting the replaced shape
        window = lines[max(0, i - 4) : i]
        if not any('_span("firestore_stream")' in w for w in window):
            unguarded.append(f"line {i + 1}: {stripped}")
    assert not unguarded, (
        "streamed reads with no firestore_stream span above them — route them through "
        "_stream():\n" + "\n".join(unguarded)
    )


def _run() -> None:
    plain = sorted(
        (name, fn)
        for name, fn in globals().items()
        if name.startswith("test_") and callable(fn)
    )
    emulator = sorted(
        (name, fn)
        for name, fn in globals().items()
        if name.startswith("emulator_test_") and callable(fn)
    )
    for name, fn in plain:
        setup_function()
        try:
            fn()
        finally:
            teardown_function()
        print(f"  ok  {name}")

    if EMULATOR_HOST:
        print(f"\n  -- emulator at {EMULATOR_HOST} --")
        for name, fn in emulator:
            notification_store.reset_for_tests()
            try:
                fn()
            finally:
                notification_store.reset_for_tests()
            print(f"  ok  {name}")
        print(f"    (database: {_EMULATOR_DB})")
        print(f"\n{len(plain) + len(emulator)} notification store checks passed.")
    else:
        print("\n" + "!" * 78)
        print(
            f"!! SKIPPED {len(emulator)} EMULATOR-BACKED CHECKS: FIRESTORE_EMULATOR_HOST is unset."
        )
        print("!! They run in ci.yml's own step, via:")
        print("!!   firebase emulators:exec --project demo-kix --only firestore \\")
        print("!!     --config ../../firebase.json '<this suite>'")
        print("!! Run them locally the same way (needs Java 21+ and firebase-tools).")
        print("!" * 78)
        print(f"\n{len(plain)} notification store checks passed, {len(emulator)} skipped.")


if __name__ == "__main__":
    _run()
