"""Checks for partner_store.py, in two halves.

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

import partner_store

REPO = pathlib.Path(__file__).resolve().parents[2]
MODULE_SOURCE = pathlib.Path(partner_store.__file__).read_text()
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
PARTNER = "partner-a"
DEST = "dest-a"
TRAIN = partner_store.TrainRef(
    operator="VYG",
    line="L1",
    journey_ref="VYG:DatedServiceJourney:1234",
    call_date="2026-09-08",
    stop_place="NSR:StopPlace:337",
    stop_order=3,
)


# ---- Fixtures ---------------------------------------------------------------------------------


def _spec(**over: Any) -> partner_store.SubscriptionSpec:
    base: dict[str, Any] = {
        "subscription_id": SID,
        "partner_id": PARTNER,
        "destination_id": DEST,
        "from_stop": "NSR:StopPlace:337",
        "to_stop": "NSR:StopPlace:418",
        "begin": "06:45",
        "end": "08:30",
        "days": (0, 1, 2, 3, 4),
        "partner_ref": "commuter-42",
        "test": False,
    }
    base.update(over)
    return partner_store.SubscriptionSpec(**base)


def _live_doc(now: datetime = NOW, **over: Any) -> dict[str, Any]:
    """A stored subscription built by the REAL writer.

    Never a hand-written dict: a fixture that does not come through `_new_subscription_doc` hides
    exactly the class of defect these checks exist to catch -- the /rushhistorikk render stub
    modelled a non-pushing baseline as null where the code wrote "green", and the marker bug it hid
    shipped.
    """
    return partner_store._new_subscription_doc(_spec(**over), now)


def _emit(**over: Any) -> partner_store.EmitRequest:
    base: dict[str, Any] = {
        "subscription_id": SID,
        "partner_id": PARTNER,
        "destination_id": DEST,
        "subscription_version": 1,
        "train": TRAIN,
        "phase": partner_store.PHASE_PREDICTED,
        "situations": ("ENT:SituationNumber:1",),
        "payload": {"notification": {"title": "Tog L1 07:18 er innstilt", "body": "Oslo S -> Ski"}},
        "valid_until": NOW + timedelta(minutes=60),
        "partner_ref": "commuter-42",
        "test": False,
    }
    base.update(over)
    return partner_store.EmitRequest(**base)


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

    def get(self, transaction: Any = None) -> _Snap:
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

    def stream(self) -> list[_Snap]:
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

    def get(self, query: Any) -> list[_Snap]:
        self.ops.append(("read_query", getattr(query, "name", "page")))
        return list(query.stream())


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
    """Point partner_store at the fake, running its real bodies and applying their writes.

    `_run_transaction` runs each body ONCE here. The "a body may run more than once" property is a
    check of its own (`test_a_transaction_body_may_run_twice_...`) rather than something this helper
    simulates -- simulating it would make every other check pay for the retry and would still not
    be the client's retry.
    """
    global _fake
    _fake = client
    _saved.update(
        {
            "client": partner_store._client,
            "run": partner_store._run_transaction,
            "query": partner_store._query,
        }
    )
    partner_store._client = lambda: client  # type: ignore[assignment]

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

    def _query(name: str, *, equality: Any = None, inequality: Any = None, limit: int) -> _Page:
        client.query_calls.append((name, dict(equality or {}), dict(inequality or {}), limit))
        rows = client.pages.get(name, [])[:limit]
        return _Page(client, rows)

    partner_store._run_transaction = _run  # type: ignore[assignment]
    partner_store._query = _query  # type: ignore[assignment]
    partner_store.configure(client=client, clock=lambda: NOW)


def _uninstall() -> None:
    global _fake
    if _saved:
        partner_store._client = _saved["client"]  # type: ignore[assignment]
        partner_store._run_transaction = _saved["run"]  # type: ignore[assignment]
        partner_store._query = _saved["query"]  # type: ignore[assignment]
        _saved.clear()
    partner_store.reset_for_tests()
    _fake = None


class _NoClockNoLog:
    """Make a clock read or a log call inside a transaction body raise, rather than merely differ.

    Comparing two runs of a body is not enough on its own: two `datetime.now()` calls can land in
    the same microsecond, and a log line does not change what is written at all. So the clock raises
    and every attribute of the logger raises, and the body has to be pure to get through.
    """

    def __enter__(self) -> Self:
        self._clock = partner_store._clock
        self._logger = partner_store.logger

        def _boom() -> datetime:
            raise AssertionError("a transaction body must not read the clock")

        class _Boom:
            def __getattr__(self, name: str) -> Any:
                raise AssertionError(f"a transaction body must not call logger.{name}")

        partner_store._clock = _boom  # type: ignore[assignment]
        partner_store.logger = _Boom()  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: object) -> None:
        partner_store._clock = self._clock  # type: ignore[assignment]
        partner_store.logger = self._logger  # type: ignore[assignment]


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
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    refs = partner_store._Refs(client)
    request = _emit()
    seed = partner_store.revision_seed(NOW)
    with _NoClockNoLog():
        first, second = _Txn(), _Txn()
        a = partner_store._emit_body(first, refs, request=request, now=NOW, seed=seed)
        b = partner_store._emit_body(second, refs, request=request, now=NOW, seed=seed)
    assert a == b, "the same reads must produce the same result"
    assert first.writes == second.writes, "the same reads must produce the same writes"
    assert a.status == "created"
    # The event id is what the receiver dedupes on; an irreproducible one turns a retried body into
    # a second notification about the same train.
    assert a.event_id == b.event_id


def test_every_transaction_body_reads_before_it_writes():
    """All four write paths, because the rule has to hold in each of them and not on average."""
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    refs = partner_store._Refs(client)
    seed = partner_store.revision_seed(NOW)
    bodies = [
        lambda t: partner_store._create_body(
            t, refs, _Page(client, []), spec=_spec(subscription_id="sub-2"), now=NOW
        ),
        lambda t: partner_store._replace_body(
            t, refs, spec=_spec(begin="07:00"), expected_version=1, now=NOW
        ),
        lambda t: partner_store._emit_body(t, refs, request=_emit(), now=NOW, seed=seed),
        lambda t: partner_store._delete_body(
            t, refs, subscription_id=SID, partner_id=PARTNER, now=NOW
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

    Six bodies increment something (`version`, `attempts`, `revision`, `below_threshold_cycles`,
    `capped_count`, `cleanup.attempts`, the Oslo-day counter). Each is run twice against one fake
    transaction with a raising clock and a raising logger; identical writes is the proof.
    """
    seed = partner_store.revision_seed(NOW)
    for build in (
        lambda c: (
            partner_store._Refs(c),
            lambda t, r: partner_store._replace_body(
                t, r, spec=_spec(begin="07:00"), expected_version=1, now=NOW
            ),
        ),
        lambda c: (
            partner_store._Refs(c),
            lambda t, r: partner_store._delete_body(
                t, r, subscription_id=SID, partner_id=PARTNER, now=NOW
            ),
        ),
        lambda c: (
            partner_store._Refs(c),
            lambda t, r: partner_store._withdraw_body(t, r, request=_emit(), now=NOW, seed=seed),
        ),
        lambda c: (
            partner_store._Refs(c),
            lambda t, r: partner_store._claim_body(t, r, delivery_id=_DELIVERY, now=NOW),
        ),
        lambda c: (
            partner_store._Refs(c),
            lambda t, r: partner_store._sent_body(t, r, delivery_id=_DELIVERY, now=NOW),
        ),
        lambda c: (
            partner_store._Refs(c),
            lambda t, r: partner_store._failed_body(
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
            partner_store._Refs(c),
            lambda t, r: partner_store._finish_cleanup_body(
                t,
                r,
                subscription_id=SID,
                now=NOW,
                done=True,
                deleted={"events": 2, "deliveries": 2, "subjects": 1},
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
        partner_store.SUBSCRIPTIONS,
        SID,
        dict(
            _live_doc(),
            version=1,
            daily_deliveries={partner_store.oslo_day(NOW): 1},
            delivered={"ENT:SituationNumber:0": NOW + timedelta(hours=10)},
            cleanup={"state": "pending", "attempts": 1, "deleted_events": 1},
        ),
    )
    client.put(
        partner_store.SUBJECTS,
        request.alert_id,
        {
            "alert_id": request.alert_id,
            "subscription_id": SID,
            "partner_id": PARTNER,
            "destination_id": DEST,
            "phase": partner_store.PHASE_PREDICTED,
            "revision": 41,
            "below_threshold_cycles": 1,
            "last_event_id": "old-event",
            "pending_delivery_id": _DELIVERY,
            "delivered_revision": None,
            partner_store.TTL_FIELD: NOW + timedelta(days=7),
        },
    )
    client.put(
        partner_store.DELIVERIES,
        _DELIVERY,
        {
            "delivery_id": _DELIVERY,
            "event_id": "old-event",
            "alert_id": request.alert_id,
            "subscription_id": SID,
            "subscription_version": 1,
            "partner_id": PARTNER,
            "destination_id": DEST,
            "revision": 41,
            "state": "pending",
            "attempts": 2,
            "capped_count": 1,
            "retry_not_before": NOW - timedelta(seconds=1),
            "valid_until": NOW + timedelta(minutes=30),
            "situations": ["ENT:SituationNumber:1"],
            partner_store.TTL_FIELD: NOW + timedelta(days=7),
        },
    )
    return client


def test_the_send_and_claim_bodies_also_read_before_they_write():
    client, request = _emitted()
    delivery_id = client.docs[f"{partner_store.SUBJECTS}/{request.alert_id}"][
        "pending_delivery_id"
    ]
    refs = partner_store._Refs(client)
    for body in (
        lambda t: partner_store._claim_body(t, refs, delivery_id=delivery_id, now=NOW),
        lambda t: partner_store._sent_body(t, refs, delivery_id=delivery_id, now=NOW),
    ):
        txn = _Txn()
        with _NoClockNoLog():
            body(txn)
        assert _no_read_after_write(txn), f"a read follows a write: {txn.ops}"


def _emitted(**over: Any) -> tuple[_FakeClient, partner_store.EmitRequest]:
    """A fake store holding one live subscription and one committed pending event."""
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    request = _emit(**over)
    commit = partner_store.commit_event(request, now=NOW)
    assert commit.status == "created"
    return client, request


# ---- Scenario 9: ceilings, refused before any write --------------------------------------------


def test_a_partner_at_its_ceiling_is_refused_before_any_write():
    client = _FakeClient()
    refs = partner_store._Refs(client)
    rows = [
        (f"{partner_store.SUBSCRIPTIONS}/s{i}", _live_doc(subscription_id=f"s{i}"))
        for i in range(partner_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_PARTNER)
    ]
    txn = _Txn()
    try:
        with _NoClockNoLog():
            partner_store._create_body(txn, refs, _Page(client, rows), spec=_spec(), now=NOW)
    except partner_store.QuotaExceeded:
        pass
    else:
        raise AssertionError("a create past the ceiling must be refused")
    assert txn.writes == [], "the refusal must precede every write"


def test_the_ceiling_counts_only_live_subscriptions():
    """A partner is not locked out by rows TTL has not caught up with.

    This is the whole reason the count is a query rather than a counter document: an expired or
    tombstoned subscription is still `status == "active"` (or still present) until TTL deletes it up
    to ~24 h later, and a counter would keep it in the total forever.
    """
    client = _FakeClient()
    refs = partner_store._Refs(client)
    dead = _live_doc(subscription_id="old", now=NOW - timedelta(days=400))
    stale = [
        (f"{partner_store.SUBSCRIPTIONS}/d{i}", dict(dead, subscription_id=f"d{i}"))
        for i in range(partner_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_PARTNER)
    ]
    txn = _Txn()
    with _NoClockNoLog():
        outcome, _doc = partner_store._create_body(
            txn, refs, _Page(client, stale), spec=_spec(), now=NOW
        )
    assert outcome == "created"


def test_the_create_ceiling_query_is_bounded_by_the_ceiling():
    client = _FakeClient()
    _install(client)
    partner_store.create_subscription(_spec(), now=NOW)
    name, equality, _ineq, limit = client.query_calls[0]
    assert name == "partner_subscriptions"
    assert equality == {"partner_id": PARTNER, "status": "active"}
    assert limit == partner_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_PARTNER


def test_the_per_cycle_load_is_bounded_by_the_ceiling_and_reports_truncation():
    client = _FakeClient()
    _install(client)
    rows = [_live_doc(subscription_id=f"s{i}") for i in range(5)]
    client.page("load_active_subscriptions", partner_store.SUBSCRIPTIONS, rows)
    loaded = partner_store.active_subscriptions(limit=3, now=NOW)
    assert len(loaded.subscriptions) == 3
    assert loaded.truncated is True
    _name, _eq, _ineq, limit = client.query_calls[-1]
    assert limit == 3
    # A request for more than R21's ceiling is clamped to it, not honoured.
    partner_store.active_subscriptions(limit=10**6, now=NOW)
    assert client.query_calls[-1][3] == partner_store.MAX_SUBSCRIPTIONS_PER_CYCLE


def test_the_cycle_ceiling_does_not_exceed_varsels_own():
    """The same read is bounded one layer out, and the two must not drift apart."""
    match = re.search(r"_MAX_PARTNER_RECIPIENTS\s*=\s*(\d+)", VARSEL_SOURCE)
    assert match, "varsel._MAX_PARTNER_RECIPIENTS not found -- was it renamed?"
    assert partner_store.MAX_SUBSCRIPTIONS_PER_CYCLE <= int(match.group(1))


def test_a_destination_past_the_pending_event_ceiling_cannot_emit():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    client.page(
        "destination_deliveries_in_state",
        partner_store.DELIVERIES,
        [
            {"delivery_id": f"d{i}", "state": "pending"}
            for i in range(partner_store.MAX_PENDING_EVENTS_PER_DESTINATION)
        ],
    )
    before = dict(client.docs)
    try:
        partner_store.commit_event(_emit(), now=NOW)
    except partner_store.QuotaExceeded:
        pass
    else:
        raise AssertionError("emitting past the pending ceiling must be refused")
    assert client.docs == before, "the refusal must precede every write"


def test_the_delivery_set_is_bounded_per_cycle_and_per_destination():
    client = _FakeClient()
    _install(client)
    rows = []
    for i in range(partner_store.MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE + 5):
        rows.append(
            {
                "delivery_id": f"d{i:03d}",
                "event_id": f"d{i:03d}",
                "alert_id": "a",
                "subscription_id": SID,
                "subscription_version": 1,
                "partner_id": PARTNER,
                "destination_id": DEST,
                "state": "pending",
                "attempts": 0,
                "retry_not_before": NOW - timedelta(seconds=1),
                "valid_until": NOW + timedelta(minutes=30 + i),
                partner_store.TTL_FIELD: NOW + timedelta(days=7),
                "situations": ["ENT:1"],
            }
        )
    client.page("pending_deliveries", partner_store.DELIVERIES, rows)
    pending = partner_store.pending_deliveries(now=NOW)
    assert len(pending.ready) == partner_store.MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE
    assert pending.throttled == (DEST,)
    # Soonest departure first, across the whole set.
    assert [d.valid_until for d in pending.ready] == sorted(d.valid_until for d in pending.ready)


# ---- Scenario 4: expired but not yet TTL-deleted ----------------------------------------------


def test_an_unreadable_or_missing_expiry_reads_as_expired():
    """The strict direction, because R19 publishes what the code does.

    A document that outlives its published window because its timestamp could not be parsed makes
    the published retention text false, and the failure is invisible. A dropped document is visible.
    """
    assert partner_store._not_expired({partner_store.TTL_FIELD: NOW + timedelta(days=1)}, NOW)
    assert not partner_store._not_expired({}, NOW)
    assert not partner_store._not_expired({partner_store.TTL_FIELD: None}, NOW)
    assert not partner_store._not_expired({partner_store.TTL_FIELD: "not a date"}, NOW)
    assert not partner_store._not_expired({partner_store.TTL_FIELD: NOW}, NOW)


def test_an_expired_subscription_is_neither_live_nor_loaded():
    old = _live_doc(now=NOW - timedelta(days=400))
    assert partner_store.subscription_is_live(old, NOW - timedelta(days=399)) is True
    assert partner_store.subscription_is_live(old, NOW) is False
    # Inactivity alone expires it even inside the maximum lifetime.
    idle = _live_doc(now=NOW - timedelta(days=100))
    assert partner_store.subscription_is_live(idle, NOW) is False
    # ... and a stale derived expires_at cannot rescue it: the horizons are tested from their own
    # fields, so a document written by another binary with a wrong minimum still expires.
    lying = dict(idle)
    lying[partner_store.TTL_FIELD] = NOW + timedelta(days=365)
    assert partner_store.subscription_is_live(lying, NOW) is False
    # A tombstone is never live, whatever its horizons say.
    assert partner_store.subscription_is_live(dict(_live_doc(), status="deleted"), NOW) is False

    client = _FakeClient()
    _install(client)
    client.page(
        "load_active_subscriptions",
        partner_store.SUBSCRIPTIONS,
        [_live_doc(subscription_id="live"), dict(idle, subscription_id="idle")],
    )
    loaded = partner_store.active_subscriptions(now=NOW)
    assert [s.subscription_id for s in loaded.subscriptions] == ["live"]
    assert loaded.expired == 1


def test_an_expired_subscription_cannot_be_replaced_touched_or_emitted_for():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc(now=NOW - timedelta(days=400)))
    _install(client)
    for call in (
        lambda: partner_store.replace_subscription(_spec(begin="07:00"), now=NOW),
        lambda: partner_store.touch_subscription(SID, partner_id=PARTNER, now=NOW),
    ):
        try:
            call()
        except partner_store.NotFound:
            pass
        else:
            raise AssertionError("an expired subscription must not be writable")
    commit = partner_store.commit_event(_emit(), now=NOW)
    assert commit.status == "obsolete" and commit.reason == "expired"
    assert partner_store.get_subscription(SID, partner_id=PARTNER, now=NOW) is None


def test_an_expired_alert_subject_is_read_as_absent():
    """A stale subject would clamp the revision to a stale value and read a stale PHASE.

    The second is the dangerous one: a `confirmed` from three days ago would suppress today's
    forecast about a same-numbered dated train, which is the one thing R14's third rule is for.
    """
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    request = _emit()
    client.put(
        partner_store.SUBJECTS,
        request.alert_id,
        {
            "alert_id": request.alert_id,
            "phase": partner_store.PHASE_CONFIRMED,
            "revision": 7,
            partner_store.TTL_FIELD: NOW - timedelta(days=1),
        },
    )
    _install(client)
    commit = partner_store.commit_event(request, now=NOW)
    assert commit.status == "created"
    assert commit.event_type == partner_store.EVENT_CREATED
    assert partner_store.get_subject(request.alert_id, now=NOW) is not None
    stale = dict(client.doc(partner_store.SUBJECTS, request.alert_id) or {})
    stale[partner_store.TTL_FIELD] = NOW - timedelta(days=1)
    client.put(partner_store.SUBJECTS, request.alert_id, stale)
    assert partner_store.get_subject(request.alert_id, now=NOW) is None


def test_an_expired_or_stale_delivery_is_neither_ready_nor_claimable():
    client = _FakeClient()
    _install(client)
    base = {
        "event_id": "d1",
        "alert_id": "a",
        "subscription_id": SID,
        "subscription_version": 1,
        "partner_id": PARTNER,
        "destination_id": DEST,
        "state": "pending",
        "attempts": 0,
        "retry_not_before": NOW - timedelta(seconds=1),
        "valid_until": NOW + timedelta(minutes=30),
        partner_store.TTL_FIELD: NOW + timedelta(days=7),
        "situations": ["ENT:1"],
    }
    rows = [
        dict(base, delivery_id="ready"),
        # Past its validity: R9 says expired rather than failed, so it comes back as `stale` for
        # mark_delivery_expired instead of being dropped and left pending forever.
        dict(base, delivery_id="stale", valid_until=NOW - timedelta(minutes=1)),
        # Past its retention: TTL has not caught up, and every read path tests the field itself.
        dict(base, delivery_id="retired", **{partner_store.TTL_FIELD: NOW - timedelta(hours=1)}),
        # Backed off: excluded by the reader as well as by the query's range filter.
        dict(base, delivery_id="backedoff", retry_not_before=NOW + timedelta(minutes=2)),
    ]
    client.page("pending_deliveries", partner_store.DELIVERIES, rows)
    pending = partner_store.pending_deliveries(now=NOW)
    assert [d.delivery_id for d in pending.ready] == ["ready"]
    assert pending.stale == ("stale",)

    for row in rows:
        client.put(partner_store.DELIVERIES, row["delivery_id"], row)
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    assert partner_store.claim_delivery("retired", now=NOW).status == "gone"
    expired = partner_store.claim_delivery("stale", now=NOW)
    assert expired.status == "expired"
    assert client.doc(partner_store.DELIVERIES, "stale")["state"] == "expired"
    assert partner_store.claim_delivery("ready", now=NOW).status == "ready"


def test_the_delivery_query_itself_excludes_a_backed_off_record():
    """`Retry-After` has to survive a cycle boundary and a restart, so it is a QUERY filter."""
    client = _FakeClient()
    _install(client)
    partner_store.pending_deliveries(now=NOW)
    name, equality, inequality, _limit = client.query_calls[-1]
    assert name == "pending_deliveries"
    assert equality == {"state": "pending"}
    assert inequality == {"retry_not_before": NOW}
    spec = next(s for s in partner_store.QUERY_SPECS if s.name == "pending_deliveries")
    assert spec.inequality == (("retry_not_before", "<="),)


# ---- Scenario 3: a replace or delete racing an emission ---------------------------------------


def test_an_emission_against_an_obsolete_subscription_version_writes_nothing():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, dict(_live_doc(), version=2))
    _install(client)
    before = dict(client.docs)
    commit = partner_store.commit_event(_emit(subscription_version=1), now=NOW)
    assert commit.status == "obsolete" and commit.reason == "version"
    assert client.docs == before


def test_an_emission_after_a_tombstone_writes_nothing():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    partner_store.delete_subscription(SID, partner_id=PARTNER, now=NOW)
    before = dict(client.docs)
    commit = partner_store.commit_event(_emit(), now=NOW)
    assert commit.status == "obsolete" and commit.reason == "deleted"
    assert client.docs == before
    # The version also moved, so an emission holding the pre-delete version is doubly refused.
    assert client.doc(partner_store.SUBSCRIPTIONS, SID)["version"] == 2


def test_a_replace_refuses_the_version_it_was_not_conditioned_on():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, dict(_live_doc(), version=3))
    _install(client)
    try:
        partner_store.replace_subscription(_spec(begin="07:00"), expected_version=1, now=NOW)
    except partner_store.Conflict:
        pass
    else:
        raise AssertionError("an amendment against a stale version must be refused")
    assert client.doc(partner_store.SUBSCRIPTIONS, SID)["begin"] == "06:45"
    updated = partner_store.replace_subscription(
        _spec(begin="07:00"), expected_version=3, now=NOW
    )
    assert updated.version == 4 and updated.begin == "07:00"


def test_a_claim_finds_a_delete_that_landed_after_the_pending_set_was_read():
    """R13, at the moment of dispatch rather than at the read that produced the match."""
    client, request = _emitted()
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    partner_store.delete_subscription(SID, partner_id=PARTNER, now=NOW)
    claim = partner_store.claim_delivery(delivery_id, now=NOW)
    assert claim.status == "cancelled" and claim.reason == "deleted"
    assert client.doc(partner_store.DELIVERIES, delivery_id)["state"] == "cancelled"


# ---- Scenario 5: tenant isolation --------------------------------------------------------------


def test_one_partner_cannot_read_or_affect_anothers_records():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    other = "partner-b"
    for call in (
        lambda: partner_store.get_subscription(SID, partner_id=other, now=NOW),
        lambda: partner_store.replace_subscription(_spec(partner_id=other), now=NOW),
        lambda: partner_store.delete_subscription(SID, partner_id=other, now=NOW),
        lambda: partner_store.touch_subscription(SID, partner_id=other, now=NOW),
        lambda: partner_store.create_subscription(_spec(partner_id=other), now=NOW),
    ):
        try:
            call()
        except partner_store.Forbidden:
            pass
        else:
            raise AssertionError("a foreign partner must not reach this record")
    assert client.doc(partner_store.SUBSCRIPTIONS, SID)["partner_id"] == PARTNER
    assert client.doc(partner_store.SUBSCRIPTIONS, SID)["version"] == 1
    # An emission is an ordinary pass outcome rather than an error, so it reports `obsolete`.
    commit = partner_store.commit_event(_emit(partner_id=other), now=NOW)
    assert commit.status == "obsolete" and commit.reason == "foreign"


def test_the_partner_listing_is_filtered_by_the_authenticated_partner():
    client = _FakeClient()
    _install(client)
    partner_store.list_subscriptions(partner_id=PARTNER, now=NOW)
    _name, equality, _ineq, _limit = client.query_calls[-1]
    assert equality["partner_id"] == PARTNER
    spec = next(s for s in partner_store.QUERY_SPECS if s.name == "partner_subscriptions")
    assert "partner_id" in spec.equality


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
        partner_store.create_subscription(_spec(), now=NOW)
    except api_exceptions.ServiceUnavailable as exc:
        assert partner_store.is_retryable(exc), "an outage is retryable, not terminal"
    else:
        raise AssertionError("a store outage must surface as a failure")
    assert client.docs == {}, "no partial subscription"


def test_the_store_has_no_import_time_side_effect_and_no_reverse_dependency():
    """Importing it starts no client, and it imports nothing the notification pass owns.

    The dependency runs one way on purpose: a partner event may not reach the device registry, the
    VAPID keypair or the decision-log writer (R16), and the poller reaches this module through an
    injected provider. `varsel._load_partner_recipients` is the guard that turns a raise here into
    "this cycle has no partner recipients" -- checked by name, because a store outage must not be
    able to unwind out of `check_and_notify` and cost the cycle its decision rows and its state save.
    """
    partner_store.reset_for_tests()
    assert partner_store.enabled() is False
    assert partner_store._client_obj is None
    for forbidden in ("import varsel", "import main", "import impact", "import decisions"):
        assert forbidden not in MODULE_SOURCE
    assert "def _load_partner_recipients" in VARSEL_SOURCE
    guard = VARSEL_SOURCE.split("def _load_partner_recipients", 1)[1].split("\ndef ", 1)[0]
    assert "except Exception:" in guard and "return []" in guard


def test_an_unreadable_document_is_skipped_rather_than_failing_its_collection():
    client = _FakeClient()
    _install(client)
    client.page(
        "load_active_subscriptions",
        partner_store.SUBSCRIPTIONS,
        [
            _live_doc(subscription_id="good"),
            # A shape this binary cannot read: no `days`. R20 wants it skipped, not fatal.
            {
                "subscription_id": "bad",
                "partner_id": PARTNER,
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
                partner_store.TTL_FIELD: NOW + timedelta(days=1),
            },
        ],
    )
    loaded = partner_store.active_subscriptions(now=NOW)
    assert [s.subscription_id for s in loaded.subscriptions] == ["good"]
    assert loaded.skipped == 1


# ---- Scenario 7: an exhausted transaction is retryable ----------------------------------------


def test_an_exhausted_transaction_is_classified_retryable():
    from google.cloud.firestore_v1.base_transaction import (
        _EXCEED_ATTEMPTS_TEMPLATE,
        MAX_ATTEMPTS,
    )

    exhausted = ValueError(_EXCEED_ATTEMPTS_TEMPLATE.format(MAX_ATTEMPTS))
    assert partner_store.is_retryable(exhausted), (
        "a contended subject would be marked failed and never delivered"
    )


def test_the_exhausted_transaction_message_matches_the_installed_client():
    """A client upgrade that rewords it must fail a test rather than a partner's delivery."""
    from google.cloud.firestore_v1.base_transaction import (
        _EXCEED_ATTEMPTS_TEMPLATE,
        MAX_ATTEMPTS,
    )

    assert _EXCEED_ATTEMPTS_TEMPLATE.startswith(partner_store._EXHAUSTED_TRANSACTION_PREFIX)
    assert partner_store._exhausted_transaction_prefix() == (
        partner_store._EXHAUSTED_TRANSACTION_PREFIX
    )
    assert MAX_ATTEMPTS == 5, "the plan and the contract both quote five attempts"


def test_our_own_errors_are_terminal_and_transport_errors_are_not():
    from google.api_core import exceptions as api_exceptions

    for terminal in (
        ValueError("days must be a non-empty subset of 0..6"),
        partner_store.Conflict("version mismatch"),
        partner_store.QuotaExceeded("ceiling"),
        partner_store.Forbidden("другой partner"),
        partner_store.NotFound("gone"),
        partner_store.NotConfigured("no database"),
        api_exceptions.PermissionDenied("no datastore.user"),
        api_exceptions.InvalidArgument("bad field path"),
    ):
        assert not partner_store.is_retryable(terminal), terminal
    for retryable in (
        api_exceptions.Aborted("Too much contention"),
        api_exceptions.DeadlineExceeded("deadline"),
        api_exceptions.ServiceUnavailable("unavailable"),
        api_exceptions.ResourceExhausted("quota"),
        api_exceptions.TooManyRequests("429"),
        api_exceptions.InternalServerError("500"),
    ):
        assert partner_store.is_retryable(retryable), retryable


# ---- Current alert state: the read the partner API serves ---------------------------------------


def _alert_page(client: _FakeClient, docs: list[dict[str, Any]]) -> None:
    """Put subject documents where BOTH new query specs will find them."""
    for doc in docs:
        client.put(partner_store.SUBJECTS, doc["alert_id"], doc)
    rows = [(f"{partner_store.SUBJECTS}/{d['alert_id']}", d) for d in docs]
    client.pages["subscription_alerts"] = rows
    client.pages["partner_alerts"] = rows


def _state(alert_id: str, phase: str, *, updated: datetime = NOW, **over: Any) -> dict[str, Any]:
    doc = {
        "alert_id": alert_id,
        "subscription_id": SID,
        "partner_id": PARTNER,
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
        partner_store.TTL_FIELD: NOW + timedelta(days=partner_store.HISTORY_DAYS),
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
        client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
        commit = partner_store.commit_event(_emit())
        assert commit.status == "created"
        stored = client.doc(partner_store.SUBJECTS, commit.alert_id)
        _alert_page(client, [stored])
        snapshot = partner_store.subscription_alerts(SID, partner_id=PARTNER)
        assert snapshot.n_active == 1
        assert snapshot.n_confirmed == 0
        assert snapshot.truncated is False
        state = snapshot.subjects[0]
        assert state.alert_id == commit.alert_id
        assert state.phase == partner_store.PHASE_PREDICTED
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
        _alert_page(client, [_state("a", partner_store.PHASE_PREDICTED)])
        client.query_calls.clear()
        partner_store.subscription_alerts(SID)
        assert [c[0] for c in client.query_calls] == ["subscription_alerts"]
        client.query_calls.clear()
        partner_store.partner_alerts(PARTNER)
        assert [c[0] for c in client.query_calls] == ["partner_alerts"]
    finally:
        _uninstall()


def test_a_withdrawn_subject_does_not_stand_and_is_never_served():
    """The question is what alerts STAND. A withdrawal is the retraction of one, the partner's
    receiver already has the withdrawal event, and re-offering it here would invite a card for a
    train we have said is fine."""
    client = _FakeClient()
    _install(client)
    try:
        _alert_page(
            client,
            [
                _state("a", partner_store.PHASE_WITHDRAWN),
                _state("b", partner_store.PHASE_PREDICTED),
            ],
        )
        snapshot = partner_store.subscription_alerts(SID)
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
            _state(f"p{i}", partner_store.PHASE_PREDICTED, updated=NOW - timedelta(minutes=i))
            for i in range(partner_store.MAX_ALERTS_SERVED + 3)
        ]
        # The confirmed one is the STALEST, so only a phase-first ranking keeps it.
        docs.append(
            _state("confirmed", partner_store.PHASE_CONFIRMED, updated=NOW - timedelta(hours=6))
        )
        _alert_page(client, docs)
        snapshot = partner_store.subscription_alerts(SID)
        assert len(snapshot.subjects) == partner_store.MAX_ALERTS_SERVED
        assert snapshot.subjects[0].alert_id == "confirmed"
        # The counts are over EVERYTHING read, so the cap hides nothing -- it only stops enumerating.
        assert snapshot.n_active == len(docs)
        assert snapshot.n_confirmed == 1
        assert snapshot.truncated is False
        again = partner_store.subscription_alerts(SID)
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
                _state("stale", partner_store.PHASE_CONFIRMED,
                       **{partner_store.TTL_FIELD: NOW - timedelta(seconds=1)}),
                _state("live", partner_store.PHASE_PREDICTED),
                # An unreadable expiry reads as expired, the direction a retention promise must
                # fail in.
                _state("unreadable", partner_store.PHASE_PREDICTED,
                       **{partner_store.TTL_FIELD: "not a timestamp"}),
            ],
        )
        snapshot = partner_store.subscription_alerts(SID)
        assert [s.alert_id for s in snapshot.subjects] == ["live"]
        assert snapshot.n_active == 1
    finally:
        _uninstall()


def test_one_partner_cannot_read_anothers_alert_state():
    """Tenancy is checked here rather than left to the caller, like every other
    subscription-scoped method in this module."""
    client = _FakeClient()
    _install(client)
    try:
        _alert_page(client, [_state("a", partner_store.PHASE_PREDICTED)])
        assert partner_store.subscription_alerts(SID, partner_id=PARTNER).n_active == 1
        forbidden = False
        try:
            partner_store.subscription_alerts(SID, partner_id="someone-else")
        except partner_store.Forbidden:
            forbidden = True
        assert forbidden
    finally:
        _uninstall()


def test_the_partner_wide_read_is_one_query_grouped_by_subscription():
    """R21's shape at the request layer: one query per REQUEST, never one per row.

    A list page can hold hundreds of watches, and a per-row read would be hundreds of queries plus
    up to MAX_SUBJECTS_PER_READ document reads apiece.
    """
    client = _FakeClient()
    _install(client)
    try:
        docs = [
            _state("a", partner_store.PHASE_PREDICTED),
            _state("b", partner_store.PHASE_CONFIRMED, subscription_id="sub-2"),
            _state("c", partner_store.PHASE_PREDICTED, subscription_id="sub-2"),
        ]
        _alert_page(client, docs)
        client.query_calls.clear()
        loaded = partner_store.partner_alerts(PARTNER)
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
            _state(f"a{i}", partner_store.PHASE_PREDICTED) for i in range(4)
        ]
        _alert_page(client, docs)
        loaded = partner_store.partner_alerts(PARTNER, limit=2)
        assert loaded.truncated is True
        assert all(s.truncated for s in loaded.snapshots.values())
        one = partner_store.subscription_alerts(SID, limit=2)
        assert one.truncated is True
        # And an empty page still carries the flag on the wrapper.
        client.pages["partner_alerts"] = []
        empty = partner_store.partner_alerts(PARTNER, limit=2)
        assert empty.snapshots == {} and empty.truncated is False
    finally:
        _uninstall()


def test_an_empty_snapshot_is_a_claim_and_a_bounded_one_is_not():
    """The two must not be expressible with the same value: one says "no standing alert", the other
    says "we do not know", and a client that cannot tell them apart shows the second as the
    first."""
    empty = partner_store.empty_alert_snapshot(SID, now=NOW)
    assert empty.n_active == 0 and empty.n_confirmed == 0
    assert empty.truncated is False
    assert empty.subjects == ()
    assert empty.as_of == NOW


def test_an_unreadable_subject_is_skipped_rather_than_failing_its_collection():
    """R20's per-document guard, the same one `active_subscriptions` applies."""
    client = _FakeClient()
    _install(client)
    try:
        broken = _state("broken", partner_store.PHASE_PREDICTED)
        broken.pop("alert_id")  # the one field `_alert_state_from_doc` cannot do without
        # Registered by hand: `_alert_page` keys on alert_id, which this row deliberately lacks.
        client.pages["subscription_alerts"] = [
            (f"{partner_store.SUBJECTS}/broken", broken),
            (f"{partner_store.SUBJECTS}/fine", _state("fine", partner_store.PHASE_PREDICTED)),
        ]
        snapshot = partner_store.subscription_alerts(SID)
        assert [s.alert_id for s in snapshot.subjects] == ["fine"]
    finally:
        _uninstall()


def test_the_read_ceiling_and_the_served_cap_are_separate_numbers():
    """They answer different questions: what one request may READ, and what one row may SHOW.

    The served cap is the daily notification allowance on purpose -- a fifth subject is not
    something anybody can be told about today.
    """
    assert partner_store.MAX_ALERTS_SERVED == partner_store.MAX_ALERT_DELIVERIES_PER_DAY
    assert partner_store.MAX_SUBJECTS_PER_READ > partner_store.MAX_ALERTS_SERVED


def test_the_identity_plane_shares_this_modules_one_client():
    """Two gRPC channels to one database in a single-replica process is a cost with no gain, and
    `partner_auth` reads through the accessor below rather than opening a second one."""
    import partner_auth

    client = _FakeClient()
    _install(client)
    try:
        assert partner_store.client() is client
        # `partner_auth` names no database of its own, so there is one place that decides.
        assert "database" not in inspect.signature(partner_auth.configure).parameters
        assert "partner_store.client()" in pathlib.Path(
            pathlib.Path(partner_auth.__file__)
        ).read_text()
        partner_auth.configure()
        assert partner_auth.store_ready() is True
    finally:
        _uninstall()
        partner_auth.reset_for_tests()


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
    for spec in partner_store.QUERY_SPECS:
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
        for spec in partner_store.QUERY_SPECS
        if spec.index_fields
    }
    for index in INDEXES["indexes"]:
        assert index["collectionGroup"] in partner_store.COLLECTIONS
        key = (
            index["collectionGroup"],
            tuple((f["fieldPath"], f["order"]) for f in index["fields"]),
        )
        assert key in needed, f"{key} is declared but no query in the module needs it"


def test_every_checked_in_index_is_materialised_by_terraform():
    assert 'jsondecode(file("${path.module}/../firestore.indexes.json")).indexes' in FIRESTORE_TF
    assert 'resource "google_firestore_index" "partner"' in FIRESTORE_TF
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
    spec = next(s for s in partner_store.QUERY_SPECS if s.name == "load_active_subscriptions")
    assert spec.order_by == (("expires_at", "DESCENDING"),)
    assert (
        partner_store.SUBSCRIPTIONS,
        (("status", "ASCENDING"), ("expires_at", "DESCENDING")),
    ) in _declared_indexes()


def test_every_collection_group_has_exactly_one_ttl_field():
    ttl_block = FIRESTORE_TF.split("firestore_ttl_collections = [", 1)[1].split("]", 1)[0]
    for collection in partner_store.COLLECTIONS:
        assert f'"{collection}"' in ttl_block, f"{collection} has no TTL field in Terraform"
    assert partner_store.TTL_FIELD == "expires_at"
    assert f'field      = "{partner_store.TTL_FIELD}"' in FIRESTORE_TF
    assert FIRESTORE_TF_CODE.count("ttl_config {}") == 1  # one resource, for_each over four groups
    # An empty index_config block DISABLES indexing on the field; deliberately not used.
    assert "index_config {}" not in FIRESTORE_TF_CODE


def test_field_overrides_stay_empty_so_ttl_has_one_source_of_truth():
    assert INDEXES.get("fieldOverrides") == [], (
        "the CLI format can express a TTL, but Terraform owns them here -- two writable sources "
        "for a retention setting is how one of them quietly goes stale"
    )


def test_every_filter_goes_through_the_query_helper():
    helper = inspect.getsource(partner_store._query)
    for token in (".where(", ".order_by(", ".limit("):
        assert MODULE_SOURCE.count(token) == helper.count(token) > 0, (
            f"{token} appears outside _query -- a query that dodges the spec table can ship with "
            "no index declaration"
        )


def test_a_range_filter_must_be_the_first_ordering():
    """Firestore's own rule, enforced where the spec is declared rather than discovered at runtime."""
    try:
        partner_store.QuerySpec(
            name="bad",
            collection=partner_store.DELIVERIES,
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
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    request = _emit()
    client.put(
        partner_store.SUBJECTS,
        request.alert_id,
        {
            "alert_id": request.alert_id,
            "subscription_id": SID,
            "phase": partner_store.PHASE_PREDICTED,
            "revision": 10**9,
            partner_store.TTL_FIELD: NOW + timedelta(days=1),
        },
    )
    _install(client)
    commit = partner_store.commit_event(_emit(phase=partner_store.PHASE_CONFIRMED), now=NOW)
    # The clock's seed is far below the stored revision, so the clamp is what supplies monotonicity.
    assert partner_store.revision_seed(NOW) < 10**9
    assert commit.revision == 10**9 + 1
    # A clock that goes backwards -- DST, an NTP step -- cannot move it back either.
    earlier = NOW - timedelta(hours=2)
    assert partner_store.revision_seed(earlier) < partner_store.revision_seed(NOW)
    assert partner_store.revision_seed(NOW) > 0


def test_a_fresh_subject_does_not_restart_at_zero():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    commit = partner_store.commit_event(_emit(), now=NOW)
    assert commit.revision == partner_store.revision_seed(NOW) > 1


def test_only_a_lifecycle_transition_mints_a_revision():
    table = {
        (None, "predicted"): partner_store.EVENT_CREATED,
        (None, "confirmed"): partner_store.EVENT_CREATED,
        ("predicted", "predicted"): None,
        ("predicted", "confirmed"): partner_store.EVENT_UPDATED,
        ("confirmed", "confirmed"): None,
        # R14's third rule, one layer earlier: the event never exists.
        ("confirmed", "predicted"): None,
        ("withdrawn", "predicted"): partner_store.EVENT_UPDATED,
        ("withdrawn", "confirmed"): partner_store.EVENT_UPDATED,
    }
    for (stored, requested), expected in table.items():
        assert partner_store._transition(stored, requested) == expected, (stored, requested)


def test_a_changed_payload_mints_nothing():
    """No materiality threshold, at any size, and the store owns the decision so nothing can pass
    it a per-cycle digest that would mint every tick."""
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    first = partner_store.commit_event(_emit(), now=NOW)
    again = partner_store.commit_event(
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
    first_delivery = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    commit = partner_store.commit_event(
        _emit(phase=partner_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=3)
    )
    assert commit.status == "created"
    assert commit.superseded_delivery_id == first_delivery
    assert client.doc(partner_store.DELIVERIES, first_delivery)["state"] == "superseded"
    assert client.doc(partner_store.DELIVERIES, first_delivery)["superseded_by"] == commit.event_id
    assert client.doc(partner_store.DELIVERIES, commit.delivery_id)["state"] == "pending"


def test_a_sent_delivery_is_never_superseded_after_the_fact():
    client, request = _emitted()
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    partner_store.mark_delivery_sent(delivery_id, now=NOW)
    commit = partner_store.commit_event(
        _emit(phase=partner_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=3)
    )
    assert commit.superseded_delivery_id is None
    assert client.doc(partner_store.DELIVERIES, delivery_id)["state"] == "sent"


def test_a_late_sent_does_not_clobber_a_newer_pending_delivery():
    """An attempt takes seconds, and a newer revision can be minted inside them.

    Clearing `pending_delivery_id` unconditionally would drop the subject's pointer to the NEWER
    pending delivery, and the next mint would then fail to supersede it -- two boxes about one train
    on a lock screen, which is the 2026-08-05 defect one channel over.
    """
    client, request = _emitted()
    first = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    newer = partner_store.commit_event(
        _emit(phase=partner_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=2)
    )
    assert newer.delivery_id != first
    # The old attempt returns AFTER the new revision was minted.
    partner_store.mark_delivery_sent(first, now=NOW + timedelta(minutes=3))
    subject = client.doc(partner_store.SUBJECTS, request.alert_id)
    assert subject["pending_delivery_id"] == newer.delivery_id
    # And the recorded delivered revision is the one actually delivered, clamped forward only.
    assert subject["delivered_revision"] == client.doc(partner_store.DELIVERIES, first)["revision"]
    partner_store.mark_delivery_sent(newer.delivery_id, now=NOW + timedelta(minutes=4))
    subject = client.doc(partner_store.SUBJECTS, request.alert_id)
    assert subject["delivered_revision"] == newer.revision
    assert subject["pending_delivery_id"] is None


def test_the_pending_event_ceiling_is_counted_with_an_aggregation():
    """One count per emit, and pending deliveries accumulate for a full 60 minutes of validity.

    Streamed, a hundred subjects each reading a hundred pending documents is ten thousand reads per
    cycle and several million a day. The stream survives as a fallback -- the same answer at a worse
    price -- so a build without aggregation support degrades in cost rather than refusing to emit.
    """
    assert "query.count(alias=" in MODULE_SOURCE
    assert "sum(1 for _ in query.stream())" in MODULE_SOURCE, "the fallback is deliberate"
    calls: list[int] = []

    class _Agg:
        def get(self) -> list[list[Any]]:
            calls.append(1)

            class _Value:
                value = 0

            return [[_Value()]]

    class _CountingPage(_Page):
        def count(self, alias: str) -> _Agg:
            assert alias == "n"
            return _Agg()

        def stream(self) -> list[_Snap]:
            raise AssertionError("the aggregation path must be preferred over a stream")

    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    real_query = partner_store._query

    def _counting_query(name: str, **kwargs: Any) -> Any:
        page = real_query(name, **kwargs)
        return _CountingPage(client, page.rows) if name.startswith("destination_") else page

    partner_store._query = _counting_query  # type: ignore[assignment]
    assert partner_store.commit_event(_emit(), now=NOW).status == "created"
    assert calls == [1], "exactly one aggregation per emit"


def test_a_withdrawal_needs_consecutive_below_threshold_cycles():
    client, request = _emitted()
    withdrawal = _emit(payload={"notification": {"title": "Varselet er trukket", "body": "x"}})
    for cycle in range(1, partner_store.WITHDRAWAL_CYCLES):
        outcome = partner_store.record_below_threshold(withdrawal, now=NOW + timedelta(minutes=cycle))
        assert outcome.status == "counting" and outcome.below_threshold_cycles == cycle
    final = partner_store.record_below_threshold(withdrawal, now=NOW + timedelta(minutes=9))
    assert final.status == "created" and final.event_type == partner_store.EVENT_WITHDRAWN
    subject = client.doc(partner_store.SUBJECTS, request.alert_id)
    assert subject["phase"] == partner_store.PHASE_WITHDRAWN
    assert subject["below_threshold_cycles"] == 0
    # A dip that recovers is never withdrawn: an observation with a phase resets the count.
    partner_store.commit_event(_emit(), now=NOW + timedelta(minutes=10))
    assert client.doc(partner_store.SUBJECTS, request.alert_id)["below_threshold_cycles"] == 0
    assert (
        partner_store.record_below_threshold(withdrawal, now=NOW + timedelta(minutes=11)).status
        == "counting"
    )


def test_a_below_threshold_observation_about_an_unwarned_train_writes_nothing():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    before = dict(client.docs)
    outcome = partner_store.record_below_threshold(_emit(), now=NOW)
    assert outcome.status == "unchanged" and outcome.reason == "nothing_standing"
    assert client.docs == before


def test_the_alert_id_grain_is_per_subscription_and_the_event_id_is_per_revision():
    a = partner_store.alert_subject_id("sub-1", TRAIN)
    b = partner_store.alert_subject_id("sub-2", TRAIN)
    assert a != b, (
        "a per-train-only alert_id would make the receiver's own revision rule suppress the "
        "SECOND commuter's notification about the same disrupted train"
    )
    assert a == partner_store.alert_subject_id("sub-1", TRAIN), "must be deterministic"
    other_stop = partner_store.TrainRef(**{**TRAIN.__dict__, "stop_order": 4})
    assert partner_store.alert_subject_id("sub-1", other_stop) != a
    assert partner_store.event_id_for(a, 5) == partner_store.event_id_for(a, 5)
    assert partner_store.event_id_for(a, 5) != partner_store.event_id_for(a, 6)
    assert partner_store.event_id_for(a, 5) != partner_store.event_id_for(b, 5)


def test_event_validity_is_the_earliest_of_three_things():
    horizon = NOW + timedelta(minutes=partner_store.EVENT_VALIDITY_MINUTES)
    assert (
        partner_store.event_validity(
            occurred_at=NOW, departure_at=None, subscription_expires_at=None
        )
        == horizon
    )
    departure = NOW + timedelta(minutes=12)
    assert (
        partner_store.event_validity(
            occurred_at=NOW, departure_at=departure, subscription_expires_at=None
        )
        == departure
    )
    lifetime = NOW + timedelta(minutes=3)
    assert (
        partner_store.event_validity(
            occurred_at=NOW, departure_at=departure, subscription_expires_at=lifetime
        )
        == lifetime
    )
    assert partner_store.EVENT_VALIDITY_MINUTES == 60, "U6, U8, AE12 and the docs quote this number"


def test_the_six_delivery_states_are_the_ones_the_contract_names():
    assert partner_store.DELIVERY_STATES == (
        "pending",
        "sent",
        "expired",
        "superseded",
        "cancelled",
        "failed",
    )
    assert partner_store.EVENT_TYPES == (
        "journey.alert.created",
        "journey.alert.updated",
        "journey.alert.withdrawn",
    )


# ---- R11: the daily cap, spent on success ------------------------------------------------------


def test_the_daily_cap_is_spent_on_success_and_a_replacement_is_exempt():
    client, request = _emitted()
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    outcome = partner_store.mark_delivery_sent(delivery_id, now=NOW)
    assert outcome.state == "sent" and outcome.slot_spent is True
    stored = client.doc(partner_store.SUBSCRIPTIONS, SID)
    assert stored["daily_deliveries"][partner_store.oslo_day(NOW)] == 1
    assert partner_store.cap_remaining(stored, NOW) == (
        partner_store.MAX_ALERT_DELIVERIES_PER_DAY - 1
    )
    # The confirmation about the same train replaces a forecast the traveller already received, so
    # it is exempt -- otherwise a confirmed cancellation can be the notification the cap eats.
    confirmed = partner_store.commit_event(
        _emit(phase=partner_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=4)
    )
    assert confirmed.cap_exempt is True
    second = partner_store.mark_delivery_sent(confirmed.delivery_id, now=NOW + timedelta(minutes=4))
    assert second.slot_spent is False
    assert client.doc(partner_store.SUBSCRIPTIONS, SID)["daily_deliveries"][
        partner_store.oslo_day(NOW)
    ] == 1


def test_a_failed_attempt_spends_no_slot_and_leaves_no_mark():
    client, request = _emitted()
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    outcome = partner_store.mark_delivery_failed(
        delivery_id, error="connection reset", retryable=True, now=NOW
    )
    assert outcome.state == "pending" and outcome.attempts == 1
    stored = client.doc(partner_store.SUBSCRIPTIONS, SID)
    assert stored["daily_deliveries"] == {}, "a dead destination must not burn the day's four"
    assert stored["delivered"] == {}, "dedup is on success, exactly as /varsel's is"


def test_a_capped_delivery_is_recorded_and_left_pending():
    client, request = _emitted()
    spent = dict(
        _live_doc(),
        daily_deliveries={partner_store.oslo_day(NOW): partner_store.MAX_ALERT_DELIVERIES_PER_DAY},
    )
    client.put(partner_store.SUBSCRIPTIONS, SID, spent)
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    claim = partner_store.claim_delivery(delivery_id, now=NOW)
    assert claim.status == "capped" and claim.reason == "daily_cap"
    stored = client.doc(partner_store.DELIVERIES, delivery_id)
    assert stored["state"] == "pending", "a suppressed alert is re-offered next cycle"
    assert stored["capped_count"] == 1, "and it is recorded rather than silently dropped"


def test_an_unreadable_cap_counter_grants_no_allowance():
    assert partner_store.cap_remaining({"daily_deliveries": {}}, NOW) == (
        partner_store.MAX_ALERT_DELIVERIES_PER_DAY
    )
    assert (
        partner_store.cap_remaining(
            {"daily_deliveries": {partner_store.oslo_day(NOW): "three"}}, NOW
        )
        == 0
    )


def test_the_cap_is_keyed_on_the_oslo_day_not_utc():
    late = datetime(2026, 7, 1, 22, 30, tzinfo=UTC)  # 00:30 the next day in Oslo (CEST)
    assert partner_store.oslo_day(late) == "2026-07-02"
    assert partner_store.oslo_day(datetime(2026, 1, 1, 22, 30, tzinfo=UTC)) == "2026-01-01"
    assert partner_store.MAX_ALERT_DELIVERIES_PER_DAY == 4, "/varsel's own per-registration cap"


def test_the_delivered_marks_are_the_notified_grain_varsel_reads():
    client, request = _emitted(situations=("ENT:SituationNumber:1", "NSB:SituationNumber:9"))
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    partner_store.mark_delivery_sent(delivery_id, now=NOW)
    stored = client.doc(partner_store.SUBSCRIPTIONS, SID)
    assert set(stored["delivered"]) == {"ENT:SituationNumber:1", "NSB:SituationNumber:9"}
    document = partner_store._subscription_from_doc(stored).as_recipient_document(NOW)
    assert document["delivered"] == ["ENT:SituationNumber:1", "NSB:SituationNumber:9"]
    # And a mark whose window has passed drops out rather than suppressing forever.
    later = NOW + timedelta(hours=partner_store.DELIVERED_MARK_TTL_H + 1)
    assert partner_store._subscription_from_doc(stored).as_recipient_document(later)[
        "delivered"
    ] == []


# ---- The /varsel seam --------------------------------------------------------------------------


def test_the_recipient_document_carries_every_key_varsel_reads():
    """Parity with `varsel._parse_partner_recipient`, read out of its source rather than assumed.

    That function validates the fields the pass reads and is indifferent to the rest -- which is
    what lets this document carry the ids U6 needs -- so the check is one-directional: every key it
    reads must be present.
    """
    parser = VARSEL_SOURCE.split("def _parse_partner_recipient", 1)[1].split("\ndef ", 1)[0]
    keys = set(re.findall(r'doc\[["\']([a-z_]+)["\']\]', parser))
    keys |= set(re.findall(r'doc\.get\(["\']([a-z_]+)["\']', parser))
    assert keys, "no document keys found -- did _parse_partner_recipient change shape?"
    document = partner_store._subscription_from_doc(_live_doc()).as_recipient_document(NOW)
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
        except partner_store.Conflict:
            pass
        else:
            raise AssertionError(f"{bad} must be refused")
    _spec().validate()
    # And the restatement must not be looser than /varsel's: both refuse an overnight window.
    parser = VARSEL_SOURCE.split("def _parse_partner_recipient", 1)[1].split("\ndef ", 1)[0]
    assert "overnight windows are not supported" in parser


def test_the_load_provider_is_the_shape_varsel_declares():
    client = _FakeClient()
    _install(client)
    client.page(
        "load_active_subscriptions",
        partner_store.SUBSCRIPTIONS,
        [_live_doc(subscription_id="s1"), _live_doc(subscription_id="s2")],
    )
    documents = partner_store.recipient_documents()
    assert [d["recipient_id"] for d in documents] == ["s1", "s2"]
    assert all(isinstance(d, dict) for d in documents)
    assert "def __call__(self) -> Iterable[Mapping[str, Any]]: ..." in VARSEL_SOURCE


# ---- Retry, expiry and the terminal states ----------------------------------------------------


def test_retry_after_is_capped_at_the_lesser_of_three_things():
    client, request = _emitted(valid_until=NOW + timedelta(seconds=90))
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    outcome = partner_store.mark_delivery_failed(
        delivery_id, error="429", retryable=True, retry_after_s=3600, now=NOW
    )
    # The header asked for an hour; the cap and then the departure both bind.
    assert outcome.retry_not_before == NOW + timedelta(seconds=90)
    assert partner_store.RETRY_AFTER_CAP_S == 300
    client.put(
        partner_store.DELIVERIES,
        delivery_id,
        dict(client.doc(partner_store.DELIVERIES, delivery_id), valid_until=NOW + timedelta(hours=2)),
    )
    outcome = partner_store.mark_delivery_failed(
        delivery_id, error="429", retryable=True, retry_after_s=3600, now=NOW
    )
    assert outcome.retry_not_before == NOW + timedelta(seconds=partner_store.RETRY_AFTER_CAP_S)


def test_a_terminal_failure_parks_the_delivery_and_a_410_cancels_it():
    client, request = _emitted()
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    outcome = partner_store.mark_delivery_failed(
        delivery_id, error="404 no such route", retryable=False, now=NOW
    )
    assert outcome.state == "failed"
    client.put(
        partner_store.DELIVERIES,
        delivery_id,
        dict(client.doc(partner_store.DELIVERIES, delivery_id), state="pending"),
    )
    gone = partner_store.mark_delivery_failed(
        delivery_id, error="410 gone", retryable=False, terminal_state="cancelled", now=NOW
    )
    assert gone.state == "cancelled"
    try:
        partner_store.mark_delivery_failed(
            delivery_id, error="x", retryable=False, terminal_state="dead", now=NOW
        )
    except partner_store.Conflict:
        pass
    else:
        raise AssertionError("an unknown terminal state must be refused")


def test_a_delivery_past_its_validity_is_expired_rather_than_failed():
    client, request = _emitted()
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    outcome = partner_store.mark_delivery_expired(delivery_id, now=NOW + timedelta(hours=2))
    assert outcome.state == "expired"
    assert client.doc(partner_store.DELIVERIES, delivery_id)["expired_reason"] == "validity"
    # Terminal states are not overwritten by a later sweep.
    again = partner_store.mark_delivery_expired(delivery_id, now=NOW + timedelta(hours=3))
    assert again.state == "expired"


def test_a_retried_create_writes_nothing_and_a_reused_id_is_refused():
    client = _FakeClient()
    _install(client)
    first = partner_store.create_subscription(_spec(), now=NOW)
    writes_before = len(client.transactions[-1].writes)
    assert writes_before == 1
    again = partner_store.create_subscription(_spec(), now=NOW + timedelta(minutes=5))
    assert again == first, "a safe retry returns the stored record unchanged"
    assert client.transactions[-1].writes == [], "and writes nothing at all"
    try:
        partner_store.create_subscription(_spec(begin="07:00"), now=NOW)
    except partner_store.Conflict:
        pass
    else:
        raise AssertionError("the same id with a different definition must be refused")
    assert client.doc(partner_store.SUBSCRIPTIONS, SID)["begin"] == "06:45"


def test_a_create_against_a_tombstone_is_refused_rather_than_reported_unchanged():
    """The idempotency check must test LIVENESS before the definition digest.

    `_delete_body` does not clear `definition_digest` -- only the cleanup's redaction does -- so a
    delete followed by a same-definition re-POST looked like a safe retry and handed the tombstone
    back through a 2xx with `status: "deleted"` on it. The window is exactly between the tombstone
    and the cleanup completing, which is where nobody is looking.
    """
    client = _FakeClient()
    _install(client)
    partner_store.create_subscription(_spec(), now=NOW)
    partner_store.delete_subscription(SID, partner_id=PARTNER, now=NOW)
    try:
        partner_store.create_subscription(_spec(), now=NOW + timedelta(minutes=1))
    except partner_store.Conflict:
        pass
    else:
        raise AssertionError("re-creating a tombstoned id must be refused, not reported unchanged")
    assert client.transactions[-1].writes == []
    assert client.doc(partner_store.SUBSCRIPTIONS, SID)["status"] == "deleted"
    # An EXPIRED-but-not-deleted record is refused for the same reason: the id is spent.
    client.put(partner_store.SUBSCRIPTIONS, "old", _live_doc(subscription_id="old", now=NOW - timedelta(days=400)))
    try:
        partner_store.create_subscription(_spec(subscription_id="old"), now=NOW)
    except partner_store.Conflict:
        pass
    else:
        raise AssertionError("re-creating an expired id must be refused")


def test_an_amendment_does_not_cancel_the_pending_alert():
    """R13 is about deletion or expiry stopping later claims, and NOT about amendment.

    A version compare at claim time silently loses the alert: emit at v1 leaves D1 pending, the
    partner amends to v2, the next cycle's emission at v2 finds no lifecycle transition and mints
    nothing, and the claim would then cancel D1 -- so the traveller is never told about a train that
    is still cancelled, and nothing logs as wrong. The version check belongs at emit.
    """
    client, request = _emitted()
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    partner_store.replace_subscription(_spec(begin="07:00"), expected_version=1, now=NOW)
    # The next cycle's observation mints nothing -- no lifecycle transition -- so D1 is still the
    # only delivery there will ever be for this train.
    assert partner_store.commit_event(_emit(subscription_version=2), now=NOW).status == "unchanged"
    claim = partner_store.claim_delivery(delivery_id, now=NOW)
    assert claim.status == "ready", "an amendment must not lose the pending alert"


def test_an_amendment_that_moves_the_callback_does_cancel_the_pending_alert():
    """What an amendment CAN invalidate: a delivery addressed to a destination the partner moved."""
    client, request = _emitted()
    delivery_id = client.doc(partner_store.SUBJECTS, request.alert_id)["pending_delivery_id"]
    partner_store.replace_subscription(
        _spec(destination_id="dest-b"), expected_version=1, now=NOW
    )
    claim = partner_store.claim_delivery(delivery_id, now=NOW)
    assert claim.status == "cancelled" and claim.reason == "destination"
    assert client.doc(partner_store.DELIVERIES, delivery_id)["state"] == "cancelled"


def test_a_delete_after_a_completed_cleanup_is_still_idempotent():
    """The redaction blanks the weekdays, so a strict parse turned the second DELETE into a 500."""
    client = _FakeClient()
    _install(client)
    partner_store.create_subscription(_spec(), now=NOW)
    partner_store.delete_subscription(SID, partner_id=PARTNER, now=NOW)
    assert partner_store.cleanup_subscription(SID, batch=100, now=NOW).done is True
    assert client.doc(partner_store.SUBSCRIPTIONS, SID)["days"] == []
    again = partner_store.delete_subscription(SID, partner_id=PARTNER, now=NOW + timedelta(days=1))
    assert again is not None and again.status == "deleted" and again.days == ()
    assert client.transactions[-1].writes == []


def test_the_test_marker_is_immutable():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc(test=True))
    _install(client)
    try:
        partner_store.replace_subscription(_spec(test=False), now=NOW)
    except partner_store.Conflict:
        pass
    else:
        raise AssertionError("flipping the test marker must require delete and re-create")
    assert client.doc(partner_store.SUBSCRIPTIONS, SID)["test"] is True


def test_an_amendment_resets_servability_but_not_the_cap_or_the_dedup_marks():
    client = _FakeClient()
    stored = dict(
        _live_doc(),
        servable=True,
        servable_checked_at=NOW,
        daily_deliveries={partner_store.oslo_day(NOW): 2},
        delivered={"ENT:1": NOW + timedelta(hours=10)},
    )
    client.put(partner_store.SUBSCRIPTIONS, SID, stored)
    _install(client)
    updated = partner_store.replace_subscription(_spec(to_stop="NSR:StopPlace:999"), now=NOW)
    # A changed route is a different servability question, so the stored answer AND its freshness
    # are both retired: "we do not know" with no timestamp, rather than a stale True with a fresh
    # one, which is precisely the shape R6 forbids reading as an all-clear.
    assert updated.servable is None and updated.servable_checked_at is None
    after = client.doc(partner_store.SUBSCRIPTIONS, SID)
    assert after["daily_deliveries"] == {partner_store.oslo_day(NOW): 2}, (
        "letting an amendment clear the cap would be a bypass anyone could drive from the API"
    )
    assert set(after["delivered"]) == {"ENT:1"}


def test_servability_never_refuses_and_records_its_freshness():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    unservable = partner_store.set_servability(SID, servable=False, now=NOW)
    assert unservable.servable is False and unservable.status == "active"
    unknown = partner_store.set_servability(SID, servable=None, now=NOW + timedelta(hours=6))
    assert unknown.servable is None, "could not be established must never read as an all-clear"
    assert unknown.servable_checked_at == NOW + timedelta(hours=6)


def test_a_tombstone_stops_routing_and_a_repeated_delete_writes_nothing():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    tombstoned = partner_store.delete_subscription(SID, partner_id=PARTNER, now=NOW)
    assert tombstoned is not None and tombstoned.status == "deleted"
    assert client.doc(partner_store.SUBSCRIPTIONS, SID)["cleanup"]["state"] == "pending"
    again = partner_store.delete_subscription(SID, partner_id=PARTNER, now=NOW + timedelta(hours=1))
    assert again is not None and again.status == "deleted"
    assert client.transactions[-1].writes == [], "a retried DELETE must not bump a version forever"
    assert partner_store.delete_subscription("never-existed", partner_id=PARTNER, now=NOW) is None


def test_cleanup_runs_after_the_tombstone_and_never_before_it():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    try:
        partner_store.cleanup_subscription(SID, now=NOW)
    except partner_store.Conflict:
        pass
    else:
        raise AssertionError(
            "reversed, a partial delete leaves a ROUTABLE subscription whose events are gone"
        )


def test_cleanup_deletes_in_batches_and_redacts_only_once_complete():
    client = _FakeClient()
    client.put(partner_store.SUBSCRIPTIONS, SID, dict(_live_doc(), status="deleted", cleanup={}))
    _install(client)
    client.page(
        "cleanup_events",
        partner_store.EVENTS,
        [{"subscription_id": SID, "event_id": f"e{i}"} for i in range(2)],
    )
    result = partner_store.cleanup_subscription(SID, batch=2, now=NOW)
    # A full page means there may be more, so the task is retryable and NOT yet complete.
    assert result.done is False and result.deleted_events == 2
    stored = client.doc(partner_store.SUBSCRIPTIONS, SID)
    assert stored["cleanup"]["state"] == "pending" and stored["from_stop"] != ""
    client.pages["cleanup_events"] = []
    final = partner_store.cleanup_subscription(SID, batch=2, now=NOW + timedelta(minutes=1))
    assert final.done is True and final.attempts == 2
    stored = client.doc(partner_store.SUBSCRIPTIONS, SID)
    assert stored["cleanup"]["state"] == "done" and stored["cleanup"]["deleted_events"] == 2
    assert stored["from_stop"] == "" and stored["to_stop"] == "" and stored["days"] == []
    assert stored["partner_ref"] == "" and stored["delivered"] == {}
    assert stored["subscription_id"] == SID and stored["partner_id"] == PARTNER


def test_cleanup_reaches_all_three_related_collections():
    """Top-level collections carrying a subscription id, so all three need deleting explicitly.

    As subcollections they would be orphaned by the tombstone -- a document delete does not remove
    subcollections and a TTL field does not cascade into them -- and the delivery query would keep
    returning them.
    """
    names = {spec.name for spec in partner_store.QUERY_SPECS if spec.name.startswith("cleanup_")}
    assert names == {"cleanup_events", "cleanup_deliveries", "cleanup_subjects"}
    for spec in partner_store.QUERY_SPECS:
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
    assert f"{partner_store.SUBSCRIPTION_MAX_LIFETIME_DAYS}-day" in expires
    assert f"{partner_store.SUBSCRIPTION_INACTIVITY_DAYS}-day" in expires
    assert f"~{partner_store.TTL_RESIDUAL_HOURS} hours" in expires
    event_schema = json.loads(
        (REPO / "contracts" / "notifications" / "v1" / "event.schema.json").read_text()
    )
    validity = event_schema["properties"]["valid_until"]["description"]
    assert f"{partner_store.EVENT_VALIDITY_MINUTES}-minute" in validity
    assert partner_store.HISTORY_DAYS == 7
    assert f"{partner_store.HISTORY_DAYS}-day" in MODULE_SOURCE or "seven-day" in MODULE_SOURCE


def test_the_event_history_window_is_shorter_than_the_labelling_retry_window():
    """A published constraint rather than an oversight: retrospectively scoring partner alerts is
    foreclosed by this retention choice, and lengthening it later is a contract change."""
    assert partner_store.HISTORY_DAYS < 10
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
    assert event_schema["properties"]["schema_version"]["const"] == partner_store.SCHEMA_VERSION
    assert set(event_schema["properties"]["type"]["enum"]) == set(partner_store.EVENT_TYPES)
    assert set(event_schema["properties"]["phase"]["enum"]) == {
        partner_store.PHASE_PREDICTED,
        partner_store.PHASE_CONFIRMED,
        partner_store.PHASE_WITHDRAWN,
    }
    # The grain the plan does not state, recorded where U6 and U7 will read it.
    assert "(subscription, train)" in event_schema["properties"]["alert_id"]["description"]


def test_the_stored_event_carries_the_envelope_the_contract_names():
    client, request = _emitted()
    event = partner_store.get_event(
        client.doc(partner_store.SUBJECTS, request.alert_id)["last_event_id"], now=NOW
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
    assert "test_partner_store.py" in CI_YML
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

    A mismatch is invisible in the worst way: the poller's partner load is guarded, so it logs one
    warning per cycle and the deployment reads exactly like a healthy one with nothing registered.
    """
    tf_default = re.search(
        r'variable "partner_firestore_database"[\s\S]*?default\s*=\s*"([a-z0-9-]+)"', TF_VARIABLES
    )
    assert tf_default, "the terraform variable or its default was renamed"
    helm = re.search(r"PARTNER_FIRESTORE_DATABASE:\s*([a-z0-9-]+)", HELM_VALUES)
    assert helm, "PARTNER_FIRESTORE_DATABASE is not in the base helm values"
    assert tf_default.group(1) == helm.group(1)
    assert 'name        = var.partner_firestore_database' in FIRESTORE_TF


def test_the_partner_secrets_are_their_own_group_and_carry_no_dash():
    """Never appended to `varsel`: an ExternalSecret is all-or-nothing under LatestOrFail and the
    chart's envFrom secretRef is not optional, so one unresolvable name deletes the group's Secret
    and leaves the pod in CreateContainerConfigError -- which in the `varsel` group means Web Push
    off for real travellers over a feature that is switched off.

    The names are checked whether they are live or commented, because they land commented until
    their values exist in Secret Manager (which this unit cannot create).
    """
    for env in ("dev", "tst", "prd"):
        values = (REPO / "helm" / "kix-avvik" / "env" / f"values-kub-ent-{env}.yaml").read_text()
        live = _uncommented(values)
        for name in ("PARTNER_SIGNING_ROOT", "PARTNER_KEY_PEPPER"):
            assert name in values, f"{env} does not name {name} at all"
            # The prose above the block names both secrets in order to explain them, so the
            # "not in the varsel group" half has to read the LIVE yaml.
            assert name not in live, (
                f"{env} names {name} in a live secrets group -- the value does not exist in Secret "
                "Manager yet, and an unresolvable name fails the whole ExternalSecret"
            )
            assert "-" not in name  # the common chart fails the template on a dashed secret name
        # Its own group under `secrets:`, as one contiguous block, live or commented: appended to
        # varsel's list instead it would take the VAPID keys down with it.
        assert re.search(
            r"^\s*#?\s*partner:\s*\n\s*#?\s*-\s*PARTNER_SIGNING_ROOT\s*\n\s*#?\s*-\s*"
            r"PARTNER_KEY_PEPPER\s*$",
            values,
            re.MULTILINE,
        ), f"{env} has no `partner:` secrets group naming both secrets, live or commented"
        assert "VAPID_PRIVATE_KEY" in values or env == "tst", "tst alone has no keypair"


def test_the_partner_gate_is_off_where_an_operator_looks():
    assert 'PARTNER_NOTIFICATIONS_ENABLED: "false"' in HELM_VALUES
    main_source = (pathlib.Path(__file__).parent / "main.py").read_text()
    assert 'os.environ.get("PARTNER_NOTIFICATIONS_ENABLED", "")' in main_source, (
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
    for database in ("partner-notifications", "(default)"):
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
    partner_store.reset_for_tests()
    partner_store.configure(client=client, clock=lambda: NOW)
    return client


def emulator_test_the_subscription_round_trip_survives_the_real_client():
    client = _emulator_setup()
    created = partner_store.create_subscription(_spec(), now=NOW)
    assert created.version == 1 and created.status == "active"
    assert created.expires_at is not None and created.expires_at > NOW
    fetched = partner_store.get_subscription(SID, partner_id=PARTNER, now=NOW)
    assert fetched is not None and fetched.days == (0, 1, 2, 3, 4)
    # Idempotent retry against a real store: same record, and the version does not move.
    assert partner_store.create_subscription(_spec(), now=NOW).version == 1
    amended = partner_store.replace_subscription(_spec(begin="07:00"), expected_version=1, now=NOW)
    assert amended.version == 2 and amended.begin == "07:00"
    touched = partner_store.touch_subscription(SID, partner_id=PARTNER, now=NOW + timedelta(days=1))
    assert touched.inactivity_expires_at is not None
    assert touched.inactivity_expires_at > created.inactivity_expires_at
    tombstoned = partner_store.delete_subscription(SID, partner_id=PARTNER, now=NOW)
    assert tombstoned is not None and tombstoned.status == "deleted"
    assert partner_store.get_subscription(SID, partner_id=PARTNER, now=NOW) is None
    assert client is not None


def emulator_test_the_load_query_returns_only_live_subscriptions():
    _emulator_setup()
    partner_store.create_subscription(_spec(subscription_id="live"), now=NOW)
    partner_store.create_subscription(
        _spec(subscription_id="idle"), now=NOW - timedelta(days=120)
    )
    partner_store.create_subscription(_spec(subscription_id="gone"), now=NOW)
    partner_store.delete_subscription("gone", partner_id=PARTNER, now=NOW)
    loaded = partner_store.active_subscriptions(now=NOW)
    assert [s.subscription_id for s in loaded.subscriptions] == ["live"]
    # The idle one was returned by the query and dropped by the reader: TTL has not run.
    assert loaded.expired == 1
    documents = partner_store.recipient_documents()
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
    :func:`partner_store.is_retryable` calls True, which the next poller cycle retries, as
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
    partner_store.create_subscription(_spec(), now=NOW)
    request = _emit()
    # Keyed by thread index rather than appended, so a failure names WHICH caller it came from.
    results: dict[int, partner_store.EventCommit] = {}
    errors: dict[int, BaseException] = {}

    def _emit_once(index: int) -> None:
        try:
            results[index] = partner_store.commit_event(request, now=NOW)
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
        assert partner_store.is_retryable(exc), f"thread {index} failed terminally: {exc!r}"
    committed = {r.event_id for r in results.values() if r.event_id}
    assert len(committed) <= 1, f"two different events for one subject: {committed}"
    assert len({r.revision for r in results.values()}) <= 1, "one revision across all callers"
    assert not {r.status for r in results.values()} - {"created", "unchanged"}
    assert len([r for r in results.values() if r.status == "created"]) == len(committed)
    events = list(client.collection(partner_store.EVENTS).stream())
    deliveries = list(client.collection(partner_store.DELIVERIES).stream())
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
        retried = partner_store.commit_event(request, now=NOW + timedelta(minutes=1))
        assert retried.status in ("created", "unchanged"), retried.status
        assert retried.event_id
    final_events = list(client.collection(partner_store.EVENTS).stream())
    assert len(final_events) == 1, "no retry may mint a second event about one train"
    assert len(list(client.collection(partner_store.DELIVERIES).stream())) == 1
    event_id = final_events[0].to_dict()["event_id"]
    for index, result in results.items():
        assert result.event_id == event_id, f"thread {index} saw a different event"
    # And it stays converged: a further cycle mints nothing at all.
    settled = partner_store.commit_event(request, now=NOW + timedelta(minutes=2))
    assert settled.status == "unchanged" and settled.event_id == event_id
    assert len(list(client.collection(partner_store.EVENTS).stream())) == 1


def emulator_test_a_delete_racing_an_emission_rejects_the_obsolete_version():
    client = _emulator_setup()
    partner_store.create_subscription(_spec(), now=NOW)
    partner_store.delete_subscription(SID, partner_id=PARTNER, now=NOW)
    commit = partner_store.commit_event(_emit(subscription_version=1), now=NOW)
    assert commit.status == "obsolete" and commit.reason == "deleted"
    assert list(client.collection(partner_store.EVENTS).stream()) == []
    # And an amendment that moved the version rejects an emission holding the old one.
    partner_store.create_subscription(_spec(subscription_id="s2"), now=NOW)
    partner_store.replace_subscription(
        _spec(subscription_id="s2", begin="07:00"), expected_version=1, now=NOW
    )
    stale = partner_store.commit_event(
        _emit(subscription_id="s2", subscription_version=1), now=NOW
    )
    assert stale.status == "obsolete" and stale.reason == "version"
    assert list(client.collection(partner_store.EVENTS).stream()) == []


def emulator_test_the_full_delivery_lifecycle_spends_the_cap_once():
    client = _emulator_setup()
    partner_store.create_subscription(_spec(), now=NOW)
    commit = partner_store.commit_event(_emit(), now=NOW)
    pending = partner_store.pending_deliveries(now=NOW)
    assert [d.delivery_id for d in pending.ready] == [commit.delivery_id]
    claim = partner_store.claim_delivery(commit.delivery_id, now=NOW)
    assert claim.status == "ready" and claim.cap_remaining == 4
    failure = partner_store.mark_delivery_failed(
        commit.delivery_id, error="timeout", retryable=True, retry_after_s=30, now=NOW
    )
    assert failure.state == "pending" and failure.attempts == 1
    # The backoff is a QUERY filter, so it survives this cycle boundary.
    assert partner_store.pending_deliveries(now=NOW).ready == ()
    later = NOW + timedelta(seconds=31)
    assert [d.delivery_id for d in partner_store.pending_deliveries(now=later).ready] == [
        commit.delivery_id
    ]
    sent = partner_store.mark_delivery_sent(commit.delivery_id, now=later)
    assert sent.state == "sent" and sent.slot_spent is True and sent.attempts == 2
    subscription = partner_store.get_subscription(SID, partner_id=PARTNER, now=later)
    assert subscription is not None
    assert partner_store.cap_remaining(
        {"daily_deliveries": dict(subscription.daily_deliveries)}, later
    ) == 3
    assert list(subscription.delivered) == ["ENT:SituationNumber:1"]
    assert partner_store.pending_deliveries(now=later).ready == ()
    assert client is not None


def emulator_test_a_confirmation_supersedes_the_forecast_and_is_cap_exempt():
    _emulator_setup()
    partner_store.create_subscription(_spec(), now=NOW)
    forecast = partner_store.commit_event(_emit(), now=NOW)
    partner_store.mark_delivery_sent(forecast.delivery_id, now=NOW)
    confirmed = partner_store.commit_event(
        _emit(phase=partner_store.PHASE_CONFIRMED), now=NOW + timedelta(minutes=5)
    )
    assert confirmed.status == "created" and confirmed.event_type == partner_store.EVENT_UPDATED
    assert confirmed.revision > forecast.revision
    assert confirmed.cap_exempt is True
    sent = partner_store.mark_delivery_sent(confirmed.delivery_id, now=NOW + timedelta(minutes=5))
    assert sent.slot_spent is False
    # A predicted observation after a confirmation mints nothing at all.
    assert partner_store.commit_event(_emit(), now=NOW + timedelta(minutes=6)).status == "unchanged"


def emulator_test_the_ceiling_binds_and_counts_only_live_rows():
    _emulator_setup()
    partner_store.create_subscription(_spec(subscription_id="a"), now=NOW)
    saved = partner_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_PARTNER
    try:
        partner_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_PARTNER = 1
        try:
            partner_store.create_subscription(_spec(subscription_id="b"), now=NOW)
        except partner_store.QuotaExceeded:
            pass
        else:
            raise AssertionError("the ceiling must bind against a real store")
        # A tombstoned row does not count, so the partner is not locked out by dead records.
        partner_store.delete_subscription("a", partner_id=PARTNER, now=NOW)
        partner_store.create_subscription(_spec(subscription_id="b"), now=NOW)
    finally:
        partner_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_PARTNER = saved
    assert partner_store.get_subscription("b", partner_id=PARTNER, now=NOW) is not None


def emulator_test_one_partner_cannot_list_or_touch_anothers_subscriptions():
    _emulator_setup()
    partner_store.create_subscription(_spec(subscription_id="mine"), now=NOW)
    partner_store.create_subscription(
        _spec(subscription_id="theirs", partner_id="partner-b"), now=NOW
    )
    mine = partner_store.list_subscriptions(partner_id=PARTNER, now=NOW)
    assert [s.subscription_id for s in mine] == ["mine"]
    theirs = partner_store.list_subscriptions(partner_id="partner-b", now=NOW)
    assert [s.subscription_id for s in theirs] == ["theirs"]
    try:
        partner_store.delete_subscription("theirs", partner_id=PARTNER, now=NOW)
    except partner_store.Forbidden:
        pass
    else:
        raise AssertionError("a foreign partner must not tombstone this record")
    assert partner_store.get_subscription("theirs", partner_id="partner-b", now=NOW) is not None


def emulator_test_cleanup_deletes_every_related_document_and_redacts_the_tombstone():
    client = _emulator_setup()
    partner_store.create_subscription(_spec(), now=NOW)
    for order in (3, 4, 5):
        train = partner_store.TrainRef(**{**TRAIN.__dict__, "stop_order": order})
        partner_store.commit_event(_emit(train=train), now=NOW)
    assert len(list(client.collection(partner_store.EVENTS).stream())) == 3
    partner_store.delete_subscription(SID, partner_id=PARTNER, now=NOW)
    result = partner_store.cleanup_subscription(SID, batch=100, now=NOW)
    assert result.done is True
    assert result.deleted_events == 3 and result.deleted_deliveries == 3
    assert result.deleted_subjects == 3
    for collection in (partner_store.EVENTS, partner_store.DELIVERIES, partner_store.SUBJECTS):
        assert list(client.collection(collection).stream()) == [], collection
    tombstone = client.collection(partner_store.SUBSCRIPTIONS).document(SID).get().to_dict()
    assert tombstone["cleanup"]["state"] == "done"
    assert tombstone["from_stop"] == "" and tombstone["partner_ref"] == ""
    assert tombstone["subscription_id"] == SID


def emulator_test_a_withdrawal_is_minted_after_consecutive_below_threshold_cycles():
    _emulator_setup()
    partner_store.create_subscription(_spec(), now=NOW)
    partner_store.commit_event(_emit(), now=NOW)
    withdrawal = _emit(payload={"notification": {"title": "Varselet er trukket", "body": "x"}})
    for cycle in range(1, partner_store.WITHDRAWAL_CYCLES):
        assert (
            partner_store.record_below_threshold(withdrawal, now=NOW + timedelta(minutes=cycle)).status
            == "counting"
        )
    final = partner_store.record_below_threshold(withdrawal, now=NOW + timedelta(minutes=9))
    assert final.status == "created" and final.event_type == partner_store.EVENT_WITHDRAWN
    subject = partner_store.get_subject(_emit().alert_id, now=NOW + timedelta(minutes=9))
    assert subject is not None and subject.phase == partner_store.PHASE_WITHDRAWN


# ---- Runner ------------------------------------------------------------------------------------


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
            partner_store.reset_for_tests()
            try:
                fn()
            finally:
                partner_store.reset_for_tests()
            print(f"  ok  {name}")
        print(f"    (database: {_EMULATOR_DB})")
        print(f"\n{len(plain) + len(emulator)} partner-store checks passed.")
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
        print(f"\n{len(plain)} partner-store checks passed, {len(emulator)} skipped.")


if __name__ == "__main__":
    _run()
