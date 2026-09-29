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
import textwrap
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
    `google-beta`, `google_project_iam_member` and `roles/datastore.user` in order to refuse them,
    and since U9 it names `index_config {}` twice, once to refuse it on the TTL field and once to
    require it on the six exemptions -- so a negative grep over the raw text matches the
    explanation and passes (or fails) for the wrong reason. Same rule `test_rushhistorikk` applies
    to its SQL greps.
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

    def set(self, data: dict[str, Any]) -> None:
        """A write OUTSIDE a transaction, which exactly one caller makes: the migration marker,
        whose single writer is a startup step under Recreate."""
        self.client.docs[self.path] = dict(data)
        self.client.direct_sets.append(self.path)


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


class _AggregationResult:
    def __init__(self, value: int) -> None:
        self.value = value


class _Aggregation:
    """What `_Page.count()` returns: the page's own length, read under the same deadline.

    Counting the REGISTERED ROWS and applying no predicate of its own is the honest fake here,
    because the real aggregation applies its predicates in the INDEX -- `status`, `channel` and
    `expires_at > now` are the query's, not the reader's. So a hermetic test that registers dead
    rows and expects them not to be counted would be testing this class rather than the store; that
    half of the ceiling lives in the emulator, where the filters are real.
    """

    def __init__(self, page: _Page) -> None:
        self._page = page

    def get(self, transaction: Any = None, timeout: float | None = None) -> list[list[Any]]:
        assert timeout == notification_store._CALL_DEADLINE_S, "aggregation without a deadline"
        if transaction is not None:
            # Recorded as a READ, in order, so `test_every_transaction_body_reads_before_it_writes`
            # sees the count for what it is. Filed as anything else -- or not recorded at all -- a
            # body that counted after writing would pass that check.
            transaction.ops.append(("read_count", "count"))
        return [[_AggregationResult(len(self._page.rows))]]


class _Page:
    """What the fake `_query` returns: a page of snapshots, iterable, streamable and countable."""

    def __init__(self, client: Any, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self.client = client
        self.rows = rows

    def stream(self, timeout: float | None = None) -> list[_Snap]:
        assert timeout == notification_store._CALL_DEADLINE_S, "query stream without a deadline"
        return [_Snap(path, data, self.client) for path, data in self.rows]

    def count(self, alias: str) -> _Aggregation:
        assert alias == "n"
        return _Aggregation(self)


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
        #: One entry per `get_all`, holding the ids asked for IN ORDER. What a multi-get is for is
        #: that there is one of them, so the count of calls is the assertion, not the contents.
        self.get_all_calls: list[list[str]] = []
        self.transactions: list[_Txn] = []
        #: Paths written by `_Ref.set`, i.e. outside a transaction. Recorded rather than ignored so
        #: a body that started writing this way is visible.
        self.direct_sets: list[str] = []
        self.batch_deletes: list[str] = []
        self.fail_with: BaseException | None = None
        # The STORE's clock, which a server stamp resolves against. Deliberately ahead of `NOW` by
        # a visible margin: the bodies are handed `NOW` for `updated_at`, and a fake where the two
        # agree could not tell a correct implementation from one that stamped `now` instead.
        self.server_now: datetime = NOW + timedelta(seconds=37)

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)

    def get_all(self, refs: list[Any], timeout: float | None = None) -> list[Any]:
        """Firestore's multi-get: snapshots for the refs that exist, in no guaranteed order.

        Returns nothing at all for a missing id rather than a snapshot whose `exists` is False,
        which is what the real client does for `get_all` and is the behaviour the caller's
        "unknown ids are simply absent" rule rests on.
        """
        assert timeout == notification_store._CALL_DEADLINE_S, "multi-get without a deadline"
        self.get_all_calls.append([ref.path.split("/", 1)[1] for ref in refs])
        return [
            ref.get(timeout=notification_store._CALL_DEADLINE_S)
            for ref in refs
            if ref.path in self.docs
        ]

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


def _resolve_server_timestamps(
    data: dict[str, Any] | None, server_now: datetime
) -> dict[str, Any] | None:
    """What Firestore does at commit: replace every ``SERVER_TIMESTAMP`` sentinel with a real time.

    The fake applied writes verbatim, which left `MIRROR_FIELD` holding a sentinel object -- and a
    watermark comparison against a sentinel is not a comparison at all, so every mirror check would
    have passed or failed for a reason having nothing to do with the code. `server_now` is the
    CLIENT's clock and deliberately separate from `NOW`, the clock the bodies are handed: the whole
    point of a server stamp is that the two can differ, and a fake in which they cannot could not
    show the drift the field exists to survive.
    """
    if not data:
        return data
    from google.cloud import firestore

    sentinel = firestore.SERVER_TIMESTAMP
    return {k: (server_now if v is sentinel else v) for k, v in data.items()}


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
            resolved = _resolve_server_timestamps(data, client.server_now)
            if kind == "set":
                client.docs[path] = dict(resolved or {})
            elif kind == "update":
                client.docs.setdefault(path, {}).update(resolved or {})
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
        # EQUALITY IS APPLIED, on the fields a row actually carries. The fake returned its page
        # verbatim, which was harmless while every page was built for one query -- and became a
        # fake that cannot show termination once `_erase_device_body` started CLEARING `device_id`
        # as part of the tombstone. That clearing is what drops a journey out of `device_journeys`
        # and therefore what ends the erase loop; a page that ignored the filter re-served the same
        # rows forever. A row that lacks the field is left in rather than excluded, which is looser
        # than Firestore (an equality excludes a document missing the field) and keeps every page
        # built before this behaving as it did.
        for field, wanted in (equality or {}).items():
            rows = [(path, row) for path, row in rows if row.get(field, wanted) == wanted]
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
    # The mirrors are module state and leak between tests exactly as `_recent_load` does -- a
    # half-seeded mirror from a previous test reads as a healthy one here.
    notification_store.reset_mirrors_for_tests()


def _uninstall() -> None:
    global _fake
    if _saved:
        notification_store._firestore_client = _saved["client"]  # type: ignore[assignment]
        notification_store._run_transaction = _saved["run"]  # type: ignore[assignment]
        notification_store._query = _saved["query"]  # type: ignore[assignment]
        _saved.clear()
    notification_store.reset_for_tests()
    notification_store.reset_mirrors_for_tests()
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
            lambda t, r: notification_store._claim_body(
                t, r, delivery_id=_DELIVERY, token="tok", lease_s=20.0, now=NOW
            ),
        ),
        lambda c: (
            notification_store._Refs(c),
            lambda t, r: notification_store._sent_body(t, r, delivery_id=_DELIVERY, token=None, now=NOW),
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
                token=None,
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
        lambda t: notification_store._claim_body(
            t, refs, delivery_id=delivery_id, token="tok", lease_s=20.0, now=NOW
        ),
        lambda t: notification_store._sent_body(t, refs, delivery_id=delivery_id, token=None, now=NOW),
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

    **Asserted on the QUERY since U7, not on the reader.** Liveness used to be tested in Python over
    the rows the page returned, so a hermetic test could register dead rows and watch the count skip
    them. The count is now a `count()` aggregation, which applies its predicates in the index and
    returns a number -- there are no rows to inspect. The two halves of that move live in two
    places: the predicates are pinned here, and that they really exclude a dead document is
    `emulator_test_the_create_ceiling_counts_through_the_index`, where the filters are Firestore's.
    """
    spec = next(s for s in notification_store.QUERY_SPECS if s.name == "subscription_count")
    assert "status" in spec.equality, "a tombstone must not count against a live ceiling"
    assert spec.inequality == (("expires_at", ">"),), "an expired row must not count either"


def test_the_counted_ceiling_can_only_ever_read_HIGH():
    """R21's property, argued rather than sampled: the count is a superset of live, never a subset.

    `subscription_is_live` tests four things and the query tests three of them plus
    `expires_at > now`. That field is written as the MINIMUM of the two horizons (`_horizons`), so a
    document inside both is necessarily inside it -- every live row is counted. The converse fails
    only for a document whose stored `expires_at` is somehow ahead of a horizon it should have been
    derived from, and such a row is counted although the reader would reject it.

    So the count can be too high and never too low, and too high REFUSES a create a stricter count
    would have admitted. That is the direction a ceiling must fail in; R21 forbids the other one.
    Pinned off `_horizons` rather than restated, so a change that stopped deriving the field from
    the minimum fails here instead of quietly inverting the argument.
    """
    created = NOW
    last_seen = NOW + timedelta(days=3)
    horizons = notification_store._horizons(created, last_seen)
    assert horizons[notification_store.TTL_FIELD] == min(
        horizons["lifetime_expires_at"], horizons["inactivity_expires_at"]
    )


def test_the_create_ceiling_query_is_bounded_by_the_ceiling():
    client = _FakeClient()
    _install(client)
    notification_store.create_subscription(_spec(), now=NOW)
    name, equality, inequality, limit = client.query_calls[0]
    assert name == "subscription_count"
    # The channel is the spec's, not a constant: U12 creates `web_push` subscriptions through this
    # same path, and each channel's ceiling is counted on its own documents.
    assert equality == {"client_id": CLIENT, "status": "active", "channel": "webhook"}
    # The instant is the CALLER's, taken once outside the transaction. A query built inside the body
    # would take a different `now` on a retry, which is the same defect as a clock read in a body.
    assert inequality == {"expires_at": NOW}
    assert limit == notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT


def test_the_create_ceiling_is_counted_by_an_aggregation_inside_the_transaction():
    """Not by reading the rows, which is what bounded `max_active_subscriptions` at 10 000.

    The read cost up to `max_active` document read locks under Standard edition's pessimistic
    concurrency, paid on every create the client makes -- fine at 500, and the stated reason the
    bound could not be raised. A `count()` bills roughly one read per thousand index entries and
    takes no per-document lock.

    Two things are pinned rather than one. That the aggregation is the path taken: the page's
    `stream` raises, so a reader that fell back to counting rows fails here rather than passing
    slowly. And that it carries the TRANSACTION -- an aggregation read outside it would not see the
    body's own consistent snapshot, so two concurrent creates could each count the other out and
    both be admitted past the ceiling.
    """
    seen: list[Any] = []

    class _StreamlessPage(_Page):
        def stream(self, timeout: float | None = None) -> list[_Snap]:
            raise AssertionError("the aggregation path must be preferred over a stream")

        def count(self, alias: str) -> Any:
            aggregation = super().count(alias)
            real_get = aggregation.get

            def _get(transaction: Any = None, timeout: float | None = None) -> Any:
                seen.append(transaction)
                return real_get(transaction=transaction, timeout=timeout)

            aggregation.get = _get  # type: ignore[method-assign]
            return aggregation

    client = _FakeClient()
    refs = notification_store._Refs(client)
    txn = _Txn()
    with _NoClockNoLog():
        outcome, _stored = notification_store._create_body(
            txn, refs, _StreamlessPage(client, []), spec=_spec(), now=NOW
        )
    assert outcome == "created"
    assert seen == [txn], "the count must be read inside the caller's own transaction"


def test_the_ceiling_count_needs_no_new_index():
    """The `expires_at` inequality is free: the range field was already the first ordering.

    Firestore's classic rule is that a range filter's field must lead the ordering, and
    `subscription_count` has ordered by `expires_at DESCENDING` since 2026-09-10 -- so the index the
    aggregation needs is the one already checked in, and the terraform plan for this unit is empty.
    Pinned as an equality between the spec's `index_fields` and the fields the checked-in index
    declares, because "it needs no index" asserted as a comment is how a `FailedPrecondition` on
    every create ships green.
    """
    spec = next(s for s in notification_store.QUERY_SPECS if s.name == "subscription_count")
    assert spec.index_fields == (
        ("client_id", "ASCENDING"),
        ("status", "ASCENDING"),
        ("channel", "ASCENDING"),
        ("expires_at", "DESCENDING"),
    )
    declared = INDEXES["indexes"]
    wanted = [{"fieldPath": f, "order": o} for f, o in spec.index_fields]
    assert any(
        index["collectionGroup"] == notification_store.SUBSCRIPTIONS
        and index["fields"] == wanted
        for index in declared
    ), "the aggregation's index must already be checked in"


def test_neither_two_thousand_ceiling_survives_u6():
    """R21's per-cycle read ceiling and varsel's count ceiling both go with the full read.

    They bounded the same read from two layers and had to be edited together, which is why a test
    parsed one constant out of the other module's source. The mirror reads the collection once, at
    startup, paged with a cursor -- so there is no per-cycle read to bound, and a ceiling left
    behind would silently cap the mirror at 2 000 subscriptions instead.

    Asserted as ABSENCE from both modules, not as a changed value: a ceiling that survives as a
    number nobody applies is the shape this repo has been bitten by before.
    """
    assert not hasattr(notification_store, "MAX_SUBSCRIPTIONS_PER_CYCLE")
    assert not hasattr(notification_store, "active_subscriptions")
    assert "_MAX_WEBHOOK_RECIPIENTS" not in VARSEL_SOURCE
    # And the query they bounded is gone too, with its index: an index paid for on every write with
    # no reader is what `test_every_checked_in_index_belongs_to_a_query...` exists to refuse.
    assert not any(spec.name == "load_active_subscriptions" for spec in notification_store.QUERY_SPECS)


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


def test_the_per_destination_attempt_ceiling_is_the_clients_own():
    """U7: a client at a hundred thousand subscriptions is not bounded by the pilot's 50.

    The counter stays per DESTINATION -- two destinations of one client each spend their own
    allowance, which is what stops one bad endpoint eating the other's -- while the CEILING is read
    from the client's account, because a quota is a property of an account and not of a callback
    URL. Asked once per client per pass: a page can hold hundreds of one client's deliveries and the
    lookup behind it is a cached account read rather than a free one.
    """
    client = _FakeClient()
    _install(client)
    rows = [
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
        for i in range(notification_store.MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE + 5)
    ]
    client.page("pending_deliveries", notification_store.DELIVERIES, rows)
    asked: list[str] = []
    raised = notification_store.MAX_DELIVERY_ATTEMPTS_PER_DESTINATION_PER_CYCLE + 3
    pending = notification_store.pending_deliveries(
        per_destination_limit=lambda client_id: (asked.append(client_id), raised)[1], now=NOW
    )
    assert len(pending.ready) == raised, (
        "the ceiling must be the client's, not the module constant"
    )
    assert pending.throttled == (DEST,), "and it must still bind above it"
    assert asked == [CLIENT], "asked once per client, not once per delivery"


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
    _seed_mirror(client, [_live_doc(subscription_id="live"), dict(idle, subscription_id="idle")])
    live = notification_store.subscription_mirror().recipients(NOW)
    assert [e.subscription_id for e in live] == ["live"]


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


def test_the_listing_scan_is_capped_independently_of_the_clients_ceiling():
    """A listing must not become a 200 000-document read when a client's ceiling is raised.

    The listing tests liveness in Python -- a query cannot express both of R5's horizons -- so a
    page of `limit` documents can come back with fewer live rows and it reads again. What bounded
    those rounds was the client's own ceiling, which was the same number as the bound on
    `max_active_subscriptions` and therefore at worst 10 000. U7 raises that bound to 200 000 for
    the create count, and the scan deliberately does NOT follow it: a client whose collection is
    dominated by rows TTL has not caught up with would otherwise turn an ordinary request on this
    endpoint into the most expensive read in the module.

    The arrangement is the one that makes the loop run to its bound -- a page source holding only
    dead rows, more of them than the cap -- so what is measured is where it STOPS rather than that
    it finished. Given a client ceiling of 200 000 the uncapped loop reads every registered row.
    """
    # Imported HERE rather than at module scope: this suite pins that the store has no reverse
    # dependency on `notification_auth`, and a top-level import would read as one at a glance.
    import notification_auth

    assert (
        notification_store.MAX_SUBSCRIPTION_LIST_SCAN
        < notification_auth.QUOTA_BOUNDS["max_active_subscriptions"][1]
    ), "the cap only means something while it is below the quota's own bound"
    client = _FakeClient()
    dead = _live_doc(subscription_id="old", now=NOW - timedelta(days=400))
    page = notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT
    registered = notification_store.MAX_SUBSCRIPTION_LIST_SCAN + 2 * page
    client.pages["subscriptions"] = [
        (f"{notification_store.SUBSCRIPTIONS}/d{i}", dict(dead, subscription_id=f"d{i}"))
        for i in range(registered)
    ]
    _install(client)
    out = notification_store.list_subscriptions(
        client_id=CLIENT, limit=page, max_active=200_000, now=NOW
    )
    assert out == [], "every registered row is dead, so the page is empty"
    scanned = sum(call[3] for call in client.query_calls if call[0] == "subscriptions")
    assert scanned == notification_store.MAX_SUBSCRIPTION_LIST_SCAN, (
        f"the scan must stop at the cap, not at the client's raised ceiling: read {scanned}"
    )


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
    _seed_mirror(client, client.pages["load_active_subscriptions"] and [
        row for _path, row in client.pages["load_active_subscriptions"]
    ])
    live = notification_store.subscription_mirror().recipients(NOW)
    assert [e.subscription_id for e in live] == ["good"]


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


def test_every_collection_group_has_exactly_one_ttl_field():
    ttl_block = FIRESTORE_TF.split("firestore_ttl_collections = [", 1)[1].split("]", 1)[0]
    # COMMENTS STRIPPED, because the block explains itself: the reason `migrations` is absent is
    # written inside the list, and a naive substring search reads that prose as an entry -- which
    # is the shape of vacuous check this repo has been bitten by before, in both directions (a
    # collection named only in a comment would also read as covered).
    ttl_block = "\n".join(
        line for line in ttl_block.splitlines() if not line.strip().startswith("#")
    )
    for collection in notification_store.COLLECTIONS:
        assert f'"{collection}"' in ttl_block, f"{collection} has no TTL field in Terraform"
    # And the one collection that must NOT have one, pinned because the obvious edit is to add it
    # for symmetry. A migration marker is the record that a one-shot has already run; U14's cutover
    # import creates by document id and never deletes, so an expired marker re-runs it at the next
    # restart -- dev restarts on every push -- and resurrects every registration a traveller erased
    # since. The collection holds one document per migration and no personal data at all.
    assert f'"{notification_store.MIGRATIONS}"' not in ttl_block, (
        "the migrations collection must have NO TTL field: an expired one-shot marker re-runs the "
        "migration it guards"
    )
    # And the second one, absent for its own reason rather than by analogy: it holds exactly ONE
    # document, rewritten in place, so a TTL could only delete the live copy -- after which a
    # restart re-decides every rush round still inside its timeliness window.
    assert f'"{notification_store.ROUNDS}"' not in ttl_block, (
        "the togvarsel rounds collection must have NO TTL field: its one document is the live one"
    )
    assert notification_store.TTL_FIELD == "expires_at"
    assert f'field      = "{notification_store.TTL_FIELD}"' in FIRESTORE_TF
    assert FIRESTORE_TF_CODE.count("ttl_config {}") == 1  # one resource, for_each over four groups
    # An empty `index_config` block DISABLES indexing on the field, which is right for the six
    # exemptions below and wrong for `expires_at` -- the poller's per-cycle read orders by it. The
    # TTL resource must therefore OMIT the block, and the count is what says only the exemption
    # resource carries one.
    ttl_resource = FIRESTORE_TF_CODE.split('resource "google_firestore_field" "notification_ttl"', 1)[1]
    ttl_resource = ttl_resource.split("\nresource ", 1)[0]
    assert "index_config" not in ttl_resource, (
        "the TTL field must keep Firestore's automatic single-field indexes: an empty "
        "`index_config {}` disables them, and the poller's per-cycle read orders by `expires_at`"
    )
    assert FIRESTORE_TF_CODE.count("index_config {}") == 1  # the exemption resource, for_each


def test_every_exempted_field_is_named_by_no_query_and_declared_in_both_places():
    """U9 step 4. An exemption is silent in both directions, so it is pinned in both.

    Firestore indexes every field of every document automatically -- ascending and descending, and
    every element of an array separately -- so each index below was paid for on every write by a
    query that does not exist. What makes the removal safe is the SPEC TABLE: a field no query
    filters or orders on cannot lose anything by not being indexed, and that is checked here
    against `QUERY_SPECS` rather than by reading the module.

    The failure the other way is worse and is why this is bidirectional: exempting a field a query
    DOES name makes the query return nothing at all, with no error -- `retry_not_before` is the
    live example, a range filter in `pending_deliveries`, and `expires_at` is the TTL field on all
    four collections.
    """
    block = FIRESTORE_TF.split("firestore_index_exemptions = [", 1)[1].split("]", 1)[0]
    declared = {
        (line.split('collection = "', 1)[1].split('"', 1)[0],
         line.split('field = "', 1)[1].split('"', 1)[0])
        for line in block.splitlines()
        if "collection = " in line and "field = " in line
    }
    assert declared, "the exemption list is empty; this check has stopped checking anything"

    mirrored = {
        (entry["collectionGroup"], entry["fieldPath"])
        for entry in INDEXES.get("fieldOverrides", [])
    }
    assert declared == mirrored, (
        f"Terraform exempts {sorted(declared)} and firestore.indexes.json {sorted(mirrored)}. The "
        "two must agree: the JSON is what the emulator and a `firebase deploy` read, Terraform is "
        "what applies, and a plan-time drift check that reads one of them is worth nothing."
    )
    for entry in INDEXES["fieldOverrides"]:
        assert set(entry) == {"collectionGroup", "fieldPath", "indexes"}
        assert entry["indexes"] == [], "an exemption is an EMPTY index list, not a missing key"

    queried = {
        (spec.collection, field)
        for spec in notification_store.QUERY_SPECS
        for field in (
            *spec.equality,
            *(path for path, _ in spec.inequality),
            *(path for path, _ in spec.order_by),
        )
    }
    collisions = declared & queried
    assert not collisions, (
        f"{sorted(collisions)} is both exempted from indexing and named by a query spec. The query "
        "would return NOTHING, with no error and nothing in the logs."
    )
    for collection in notification_store.COLLECTIONS:
        assert (collection, notification_store.TTL_FIELD) not in declared, (
            f"{collection}.{notification_store.TTL_FIELD} is the TTL field; exempting it stops "
            "expired documents being found at all"
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


def test_a_live_lease_refuses_a_second_claim_and_lapses_on_its_own():
    """U8, KTD14: what makes a second delivery worker safe.

    `claimed_at` was written and read by nothing, so two workers reading the same pending page both
    claimed, both POSTed, and the client got the event twice -- which its own `event_id` inbox drops,
    which is exactly why it would never have been noticed. The lease is the hold; the token is what
    proves who has it.

    Three instants, because a hold that never lapses is a delivery nobody can ever retry: inside the
    lease it is refused, past it a second claim succeeds, and the token it gets is a DIFFERENT one.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    delivery_id = _many_pending(client, 1)[0]
    first = notification_store.claim_delivery(delivery_id, now=NOW)
    assert first.status == "ready" and first.token
    stored = client.doc(notification_store.DELIVERIES, delivery_id)
    assert stored["claimed_until"] == NOW + timedelta(seconds=notification_store.DELIVERY_LEASE_S)

    inside = notification_store.claim_delivery(delivery_id, now=NOW + timedelta(seconds=5))
    assert inside.status == "leased" and inside.reason == "lease"
    assert inside.token is None, "a refused claim holds nothing"
    # NOTHING was written: the live holder is mid-attempt and this reader has no opinion about it.
    assert client.doc(notification_store.DELIVERIES, delivery_id) == stored

    past = NOW + timedelta(seconds=notification_store.DELIVERY_LEASE_S + 1)
    second = notification_store.claim_delivery(delivery_id, now=past)
    assert second.status == "ready" and second.token and second.token != first.token
    assert client.doc(notification_store.DELIVERIES, delivery_id)["claim_token"] == second.token


def test_a_lapsed_holder_cannot_resolve_a_delivery_that_was_re_claimed():
    """The other half of the lease, and the half that protects R11's cap.

    A worker whose lease lapsed is not necessarily dead -- it may be slow. If it returns and writes
    `sent`, it spends one of the traveller's four for an attempt whose outcome nobody knows, and it
    clears a pending delivery the live holder is still POSTing. The token compare refuses it, and
    the refusal is its OWN outcome: the delivery is not resolved (so it is not `already_resolved`)
    and nothing failed (so it is not an error).

    Both terminal writes, because the failure half is the worse one: unguarded it would set a
    re-claimed delivery back to `pending` with a `retry_not_before` and release the live claim's cap
    slot.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    delivery_id = _many_pending(client, 1)[0]
    lapsed = notification_store.claim_delivery(delivery_id, now=NOW)
    past = NOW + timedelta(seconds=notification_store.DELIVERY_LEASE_S + 1)
    live = notification_store.claim_delivery(delivery_id, now=past)
    assert live.status == "ready" and live.token != lapsed.token

    refused = notification_store.mark_delivery_sent(delivery_id, token=lapsed.token, now=past)
    assert refused.lease_lost is True and refused.already_resolved is False
    assert refused.slot_spent is False, "and no slot was spent on an outcome nobody knows"
    assert client.doc(notification_store.DELIVERIES, delivery_id)["state"] == "pending"
    assert client.doc(notification_store.SUBSCRIPTIONS, SID).get("daily_deliveries", {}) == {}

    failed = notification_store.mark_delivery_failed(
        delivery_id, error="boom", retryable=True, token=lapsed.token, now=past
    )
    assert failed.lease_lost is True
    assert client.doc(notification_store.DELIVERIES, delivery_id)["claim_token"] == live.token

    # And the LIVE holder resolves it normally, clearing the hold with the state.
    done = notification_store.mark_delivery_sent(delivery_id, token=live.token, now=past)
    assert done.lease_lost is False and done.state == "sent" and done.slot_spent is True
    resolved = client.doc(notification_store.DELIVERIES, delivery_id)
    assert resolved["claimed_until"] is None and resolved["claim_token"] is None


def test_a_leased_delivery_is_not_even_offered_by_the_pending_read():
    """Filtered in PYTHON on the page, never in the query.

    A second inequality would need its own composite index AND would exclude every document that
    lacks the field -- which on the deploy that introduces it is all of them, so the whole backlog
    would become invisible. Filtering here costs a leased row its slot in the scan; filtering there
    costs the backlog.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    held, free = _many_pending(client, 2)
    notification_store.claim_delivery(held, now=NOW)
    client.page(
        "pending_deliveries",
        notification_store.DELIVERIES,
        [client.doc(notification_store.DELIVERIES, d) for d in (held, free)],
    )
    offered = notification_store.pending_deliveries(now=NOW + timedelta(seconds=1))
    assert [d.delivery_id for d in offered.ready] == [free]
    # And it comes back once the lease lapses, which is what stops a dead holder parking it forever.
    later = notification_store.pending_deliveries(
        now=NOW + timedelta(seconds=notification_store.DELIVERY_LEASE_S + 1)
    )
    assert sorted(d.delivery_id for d in later.ready) == sorted([held, free])


def test_the_lease_is_longer_than_one_attempts_worst_case():
    """The arithmetic, pinned off the constants rather than restated as a number.

    After the claim commits an attempt still reads the event (one Firestore call), POSTs it, and
    writes the terminal state (another Firestore call). A lease shorter than that sum would lapse
    mid-attempt on a healthy pass, and every slow delivery would be re-claimed and sent twice.
    """
    # Imported here rather than at module scope: this suite is the STORE's, and a top-level import
    # of a delivery module would put its import-time surface in every run of it.
    import webhook_sender

    worst_case = (
        notification_store._CALL_DEADLINE_S  # the event read
        + webhook_sender.DEFAULT_TIMEOUT_S  # the POST, connect included
        + notification_store._CALL_DEADLINE_S  # the terminal write
    )
    assert notification_store.DELIVERY_LEASE_S > worst_case, (
        f"the lease ({notification_store.DELIVERY_LEASE_S}s) must outlast one attempt "
        f"({worst_case}s), or a healthy pass re-claims its own in-flight deliveries"
    )
    # And it is not so long that a dead holder parks a delivery past its own usefulness: an event is
    # valid for an hour and a pass runs every few minutes.
    assert notification_store.DELIVERY_LEASE_S < notification_store.RESERVATION_TTL_S


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
    # And it is admitted even with the whole day's allowance already spent. PAST THE LEASE the
    # claim above took (U8): re-claiming the same delivery at the same instant is now refused as
    # `leased`, which is the lease doing exactly its job -- so the second claim is made after it
    # lapses, which is also the only shape a real second worker could have.
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
    past_lease = NOW + timedelta(minutes=1, seconds=notification_store.DELIVERY_LEASE_S + 1)
    assert notification_store.claim_delivery(confirmed.delivery_id, now=past_lease).status == "ready"


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
        {"begin": "08:00", "end": "08:00"},  # empty: refused there, refused here
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
    # A window over midnight is accepted here exactly as there (KIX-22), and the restatement must
    # not be looser or stricter than /varsel's: both refuse only an empty window, in one sentence.
    _spec(begin="22:00", end="06:00").validate()
    parser = VARSEL_SOURCE.split("def _parse_webhook_recipient", 1)[1].split("\ndef ", 1)[0]
    assert "begin and end must differ" in parser


def test_the_load_provider_is_the_shape_varsel_declares():
    client = _FakeClient()
    _install(client)
    _seed_mirror(client, [_live_doc(subscription_id="s1"), _live_doc(subscription_id="s2")])
    documents = notification_store.recipient_documents()
    assert [d["recipient_id"] for d in documents] == ["s1", "s2"]
    assert all(isinstance(d, dict) for d in documents)
    assert "def __call__(self) -> Iterable[Mapping[str, Any]]: ..." in VARSEL_SOURCE


def test_an_unseeded_mirror_yields_no_recipients_and_says_which_it_is():
    """U6. A partial answer would be worse than none.

    The pass cannot tell "this client has no live subscriptions" from "the mirror has not finished
    loading", so a half-loaded mirror would silently retire whatever it had not reached. It is a
    WARNING because it is the one state where the channel is deliberately serving nobody, and it
    names itself so a restart window is not mistaken for an empty collection.
    """
    client = _FakeClient()
    _install(client)
    _mirror_page(client, "mirror_seed", [_live_doc(subscription_id="s1")])
    records: list[tuple[str, dict]] = []
    saved = notification_store.logger
    try:
        notification_store.logger = _RecordingLogger(records)  # type: ignore[assignment]
        assert notification_store.recipient_documents() == []
        assert [level for level, _ in records] == ["warning"], records
        assert records[0][1]["result"] == "mirror_not_ready"

        records.clear()
        notification_store.subscription_mirror().seed(now=NOW)
        assert [d["recipient_id"] for d in notification_store.recipient_documents()] == ["s1"]
        # An ordinary serve is DEBUG: a line on every cycle at WARNING is how a real warning stops
        # being read, which is the argument `recipient_documents` already made about TTL lag.
        assert [level for level, _ in records] == ["debug"], records
        assert records[0][1]["result"] == "mirror"
    finally:
        notification_store.logger = saved  # type: ignore[assignment]


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
    """KTD6 requires a per-CALL deadline, and it is now the ONLY bound on this module's reads.

    Until U6 there was a second one: `varsel._WEBHOOK_LOAD_TIMEOUT_S` bounded how long the ingestion
    thread waited on a worker wrapping the per-cycle load, with the worker abandoned rather than
    killed past it. That whole arrangement went with the per-cycle read -- the pass reads an
    in-memory mirror now, so there is no socket in its path -- which makes the per-call deadline
    load-bearing on its own rather than as the inner of two.

    Checked two ways, because either alone is weak. Statically: no read in the module reaches the
    client without ``timeout=``. Dynamically: the fake's ``get``/``stream``/``count`` all ASSERT the
    kwarg, so every path the suite exercises would fail rather than silently lose its deadline --
    which is why this is also the reason those fakes assert instead of accepting ``**kwargs``.
    """
    assert 0 < notification_store._CALL_DEADLINE_S < 5.0
    assert "_WEBHOOK_LOAD_TIMEOUT_S" not in VARSEL_SOURCE, (
        "the pass's wait outlived the read it bounded; a wait around an in-memory call is a "
        "deadline that can never fire, which reads as a bound and is not one"
    )
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
        # **The subscription half of this test moved to the mirror with U6.** The per-cycle load
        # query is gone, and with it R21's ceiling and the `truncated` warning that announced it;
        # an expired document is now dropped by the mirror's read-time liveness and an unreadable
        # one by its seed, neither of which logs per cycle. What is left here is the alert-state
        # read, which still queries and still owes the same distinction.
        stale = _state("a-expired", notification_store.PHASE_PREDICTED)
        stale[notification_store.TTL_FIELD] = NOW - timedelta(minutes=1)
        _alert_page(client, [_state("a1", notification_store.PHASE_PREDICTED), stale])
        notification_store.client_alerts(CLIENT, now=NOW)
        summaries = [(level, fields) for level, fields in records if fields]
        assert [level for level, _ in summaries] == ["debug"], records
        assert summaries[0][1]["event"] == "notification_quota"
        assert summaries[0][1]["expired"] == 1 and summaries[0][1]["truncated"] is False

        # And the ceiling binding, which changes the CALLER's behaviour, still warns.
        records.clear()
        saved_served = notification_store.MAX_SUBJECTS_PER_READ
        try:
            notification_store.MAX_SUBJECTS_PER_READ = 1
            notification_store.client_alerts(CLIENT, now=NOW)
        finally:
            notification_store.MAX_SUBJECTS_PER_READ = saved_served
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


# ---- The togvarsel round markers' one document (U14) -------------------------------------------


def test_the_round_markers_are_one_document_that_a_write_replaces_whole():
    """One document, fixed id, in a collection of its own with no TTL.

    A `set` and never an `update`, and that is the property worth pinning: the whole marker map is
    the unit, so a merge would leave an issue marked from a date the writer has already rolled
    past -- which is the dedup this document exists for, refusing a round that ought to fire.
    """
    client = _FakeClient()
    _install(client)
    assert notification_store.read_rounds() is None, "absent until something writes it"

    notification_store.write_rounds(
        {"date": "2026-09-14", "areas": {"oslo": {"0630": {"level": "yellow", "pushed": True}}}},
        now=NOW,
    )
    body = notification_store.read_rounds()
    assert body is not None
    assert body["date"] == "2026-09-14"
    assert body["areas"] == {"oslo": {"0630": {"level": "yellow", "pushed": True}}}
    assert body["updated_at"] == NOW
    assert notification_store.TTL_FIELD not in body, "no horizon: the one document is the live one"

    # The next day replaces it rather than merging into it.
    notification_store.write_rounds(
        {"date": "2026-09-15", "areas": {"oslo": {}}}, now=NOW
    )
    body = notification_store.read_rounds()
    assert body is not None and body["areas"] == {"oslo": {}}
    assert body["date"] == "2026-09-15", "yesterday must not survive the write"

    # One document, in its own collection, outside the four the TTL policy covers.
    paths = [p for p in client.docs if p.startswith(f"{notification_store.ROUNDS}/")]
    assert paths == [f"{notification_store.ROUNDS}/{notification_store.ROUNDS_DOC}"]
    assert notification_store.ROUNDS not in notification_store.COLLECTIONS


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


def emulator_test_a_migration_marker_round_trips_and_is_absent_until_it_is_written():
    """U14's one-shot marker against the real client, because it is the one write here that is not
    a transaction.

    `write_migration` is a plain `set` -- there is one writer, a startup step under Recreate, and
    what the document has to survive is a restart rather than a concurrent writer. The hermetic
    fake grew a `set` on its document ref to serve it, so this is the check that the real client
    agrees; and `read_migration` answering None for a collection that does not exist yet is what
    the very first cutover depends on.
    """
    client = _emulator_setup()
    assert notification_store.read_migration("varsel_blob_to_store") is None
    notification_store.write_migration(
        "varsel_blob_to_store", {"result": "imported", "journeys": 3}, now=NOW
    )
    marker = notification_store.read_migration("varsel_blob_to_store")
    assert marker is not None
    assert marker["result"] == "imported" and marker["journeys"] == 3
    assert marker["name"] == "varsel_blob_to_store" and marker["recorded_at"] == NOW
    # No TTL field on it, ever: an expired marker re-runs the import it guards.
    assert notification_store.TTL_FIELD not in marker
    assert client is not None


def emulator_test_the_round_marker_document_round_trips_through_the_real_client():
    """The markers' one document against the real client, for the migration marker's reason: it is
    a plain `set` outside a transaction, and the hermetic fake grew that method to serve it.

    What only this can say is that a nested map comes back nested -- the body is keyed by rush area
    so the planned split into several of them is a data change rather than a migration, and a
    client that flattened or dropped the inner map would make that claim false in production only.
    """
    client = _emulator_setup()
    assert notification_store.read_rounds() is None
    notification_store.write_rounds(
        {
            "date": "2026-09-14",
            "areas": {"oslo": {"0630": {"level": "yellow", "pushed": True}}},
        },
        now=NOW,
    )
    body = notification_store.read_rounds()
    assert body is not None
    assert body["date"] == "2026-09-14"
    assert body["areas"]["oslo"]["0630"] == {"level": "yellow", "pushed": True}
    assert body["updated_at"] == NOW
    assert notification_store.TTL_FIELD not in body
    assert client is not None


def emulator_test_a_renewal_moves_the_browser_horizon_through_the_real_client():
    """U14's renewal against a real transaction, because the hermetic fake runs a body once.

    Two things only the real client can say. The renewal reads the document and then writes it, and
    Firestore raises `ReadAfterWriteError` on a read placed below a write in a transaction -- the
    rule that broke U12's erasure on every device that had anything to erase, which the fake
    records but does not enforce. And the TTL field the emulator stores is the one Firestore's
    policy deletes on, so a horizon written under the wrong channel's rule would be a real deletion
    date rather than a dict key.
    """
    client = _emulator_setup()
    notification_store.create_subscription(_web_push_spec(), now=NOW)
    notification_store.upsert_device(
        device_id="device-1",
        client_id=CLIENT,
        endpoint="https://push.example.com/x",
        p256dh="k",
        auth="a",
        brand="entur",
        now=NOW,
    )
    later = NOW + timedelta(days=notification_store.WEB_PUSH_RENEW_AFTER_DAYS + 1)
    subs = notification_store.device_journeys(device_id="device-1", client_id=CLIENT, now=later)
    assert len(subs) == 1, subs
    assert (
        notification_store.note_web_push_activity(
            "device-1", client_id=CLIENT, subscriptions=subs, now=later
        )
        == 1
    )
    renewed = notification_store.get_subscription(SID, client_id=CLIENT, now=later)
    assert renewed is not None
    assert renewed.inactivity_expires_at == later + timedelta(
        days=notification_store.WEB_PUSH_INACTIVITY_DAYS
    )
    assert renewed.lifetime_expires_at is None, "the retired horizon came back through the store"
    assert renewed.expires_at == renewed.inactivity_expires_at
    device = notification_store.get_device("device-1")
    assert device is not None and device.last_seen_at == later
    assert client is not None


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


def emulator_test_the_mirror_seed_holds_only_live_subscriptions():
    """U6, against a real client. The seed reads EVERYTHING and liveness is applied on the read.

    The three cases that have to come out differently, and none of which the fake can produce: a
    live subscription, one past its inactivity horizon that TTL has not deleted yet, and a
    tombstone. The seed's query filters none of them -- it cannot, because a tombstone written
    during the seed has to be learned about -- so what separates them is entirely Python.
    """
    _emulator_setup()
    notification_store.create_subscription(_spec(subscription_id="live"), now=NOW)
    notification_store.create_subscription(
        _spec(subscription_id="idle"), now=NOW - timedelta(days=120)
    )
    notification_store.create_subscription(_spec(subscription_id="gone"), now=NOW)
    notification_store.delete_subscription("gone", client_id=CLIENT, now=NOW)

    mirror = notification_store.subscription_mirror()
    mirror.seed(now=NOW)
    assert mirror.size() == 3, "the seed reads every document, live or not"
    assert [e.subscription_id for e in mirror.recipients(NOW)] == ["live"]
    assert [d["recipient_id"] for d in notification_store.recipient_documents()] == ["live"]


def emulator_test_a_committed_subject_reaches_the_standing_set_through_the_real_client():
    """U9 step 2, against a real client, because the hermetic fake cannot check the ORDERING.

    `_mirror_subject` runs on what `_run_transaction` RETURNS, never from inside a body, and the
    reason is the real client's retry: a body may run more than once, so an apply from inside would
    record a state a later attempt overwrote. The suite's fake runs each body exactly once, which
    is precisely the property that makes it unable to see the difference.
    """
    _emulator_setup()
    notification_store.create_subscription(_spec(), now=NOW)
    mirror = notification_store.subject_mirror()
    mirror.reset()
    mirror.seed(now=NOW)
    assert mirror.phases_for(SID, NOW) == {}

    created = notification_store.commit_event(_emit(), now=NOW)
    assert created.status == "created"
    assert mirror.phases_for(SID, NOW) == {created.alert_id: notification_store.PHASE_PREDICTED}

    later = NOW + timedelta(minutes=5)
    counting = notification_store.record_below_threshold(_emit(), now=later)
    assert counting.status == "counting"
    assert mirror.open_runs(later) == {created.alert_id: later}
    assert notification_store.clear_below_threshold(created.alert_id, now=later) is True
    assert mirror.open_runs(later) == {}

    # And a re-seed off the real documents agrees with what the commits put there, which is what
    # says the in-memory shortcut and the durable state have not drifted.
    mirror.reset()
    mirror.seed(now=later)
    assert mirror.phases_for(SID, later) == {created.alert_id: notification_store.PHASE_PREDICTED}
    assert mirror.open_runs(later) == {}


def emulator_test_a_web_push_subscription_is_invisible_to_the_webhook_load_and_ceiling():
    """U5's claim, against a real client, on U6's path.

    The channel filter moved from the load QUERY to the mirror's Python when the query went, and
    the claim did not change: a `web_push` document is not handed to the poller as a webhook
    recipient, and it does not spend the client's webhook ceiling. The ceiling half still goes
    through a real query with a real equality, which is the half a static check cannot reach.
    """
    _emulator_setup()
    notification_store.create_subscription(_spec(subscription_id="client-sub"), now=NOW)
    # A REAL `web_push` spec since U12: `destination_id` is refused on this channel and `device_id`
    # is required, so `_spec(channel=...)` alone is no longer a valid registration.
    notification_store.create_subscription(_web_push_spec(subscription_id="browser-sub"), now=NOW)
    mirror = notification_store.subscription_mirror()
    mirror.seed(now=NOW)
    assert mirror.size() == 2, "one mirror holds both channels -- which is what U12 needs"
    assert [e.subscription_id for e in mirror.recipients(NOW)] == ["client-sub"]
    assert [d["recipient_id"] for d in notification_store.recipient_documents()] == ["client-sub"]

    # The same mirror, asked the other way: the browser document is there and readable.
    on_browser = mirror.recipients(NOW, channel=notification_store.CHANNEL_WEB_PUSH)
    assert [e.subscription_id for e in on_browser] == ["browser-sub"]

    # And the ceiling, with the one arrangement that DISCRIMINATES. The client holds two documents
    # and a ceiling of two, so an unfiltered count refuses the next create; counted per channel it
    # holds one webhook, and the create is admitted. At a ceiling of one both channels would refuse
    # and the check would pass whether or not the filter was there at all.
    notification_store.create_subscription(
        _spec(subscription_id="client-sub-2"), max_active=2, now=NOW
    )
    # ... and the webhook ceiling still binds on webhooks alone, now that there are two of them.
    try:
        notification_store.create_subscription(
            _spec(subscription_id="client-sub-3"), max_active=2, now=NOW
        )
    except notification_store.QuotaExceeded:
        pass
    else:
        raise AssertionError("the webhook ceiling did not bind")


def emulator_test_one_devices_journeys_and_its_erasure_against_a_real_client():
    """U12's two store paths on a real Firestore: the device-scoped query and «Slett alt».

    What this reaches that the hermetic half cannot. The QUERY runs with three equalities and an
    ordering against a real index resolver -- though read the emulator's limit honestly: it tracks
    no composite indexes, so a missing one raises `FailedPrecondition` in production and nothing
    here. What this does prove is that the filter selects the right documents, which is the half a
    hand-written page cannot show. And the ERASURE runs its bounded loop against a real query whose
    rows change under it: the tombstone clears `device_id`, that is what drops a journey out of the
    query, and that is what ends the loop -- a property the fake could only show after it learned
    to apply equality at all.
    """
    _emulator_setup()
    notification_store.upsert_device(
        device_id="device-1",
        client_id=CLIENT,
        endpoint="https://push.example.com/x/abc",
        p256dh="BPk",
        auth="auth",
        now=NOW,
    )
    for sid in ("j1", "j2", "j3"):
        notification_store.create_subscription(_web_push_spec(subscription_id=sid), now=NOW)
    # A second device's journey and a webhook subscription, which the query must not return.
    notification_store.create_subscription(
        _web_push_spec(subscription_id="other", device_id="device-2"), now=NOW
    )
    notification_store.create_subscription(_spec(subscription_id="client-sub"), now=NOW)

    mine = notification_store.device_journeys(device_id="device-1", client_id=CLIENT, now=NOW)
    assert sorted(s.subscription_id for s in mine) == ["j1", "j2", "j3"], [s.subscription_id for s in mine]

    bound = notification_store.MAX_ERASE_WRITES_PER_TRANSACTION
    notification_store.MAX_ERASE_WRITES_PER_TRANSACTION = 2
    try:
        result = notification_store.erase_device(device_id="device-1", client_id=CLIENT, now=NOW)
    finally:
        notification_store.MAX_ERASE_WRITES_PER_TRANSACTION = bound
    assert result.complete and result.device_deleted and result.journeys_tombstoned == 3, result
    assert result.passes >= 2, "the bound was two; three journeys cannot be one pass"
    assert notification_store.get_device("device-1") is None
    assert notification_store.device_journeys(device_id="device-1", client_id=CLIENT, now=NOW) == []
    # The neighbours are untouched -- the erasure is scoped by device AND by channel AND by client.
    assert len(notification_store.device_journeys(device_id="device-2", client_id=CLIENT, now=NOW)) == 1
    assert notification_store.get_subscription("client-sub", client_id=CLIENT, now=NOW) is not None
    # And the mirror learns, which is the whole reason the journeys are tombstoned rather than
    # deleted: a seed after the erasure holds the husks and serves none of them.
    mirror = notification_store.subscription_mirror()
    mirror.seed(now=NOW)
    live = mirror.recipients(NOW, channel=notification_store.CHANNEL_WEB_PUSH)
    assert [e.subscription_id for e in live] == ["other"], [e.subscription_id for e in live]


def emulator_test_a_journey_created_mid_erasure_leaves_no_orphan():
    """A journey created BETWEEN two erase passes is caught by the next one, and the device waits.

    This is the sequential half of the plan's interleaving scenario, and it is the half that can be
    driven. `erase_device` rebuilds its query on every pass rather than hoisting it -- a create that
    lands after pass one is therefore in pass two's page -- and the device is hard-deleted only on a
    pass that did not fill, so the deletion cannot outrun a journey still arriving. Both are
    properties of the loop, and both are what this drives: the bound is shrunk to 2, pass one is run
    by hand, `j4` is created, and the public loop finishes the erasure.

    **The concurrent half is NOT tested here and cannot be, and the reason is a property of
    Firestore rather than of this suite.** Read-write transactions take pessimistic locks on their
    read set under Standard edition -- `_create_body`'s own docstring says so, which is why U7
    replaced its read with an aggregation -- so a competing create committed from INSIDE the erase
    transaction's body deadlocks: measured against the emulator, the inner `create_subscription`
    exhausts `_run_transaction`'s five attempts while the outer transaction waits on it. Driven from
    another THREAD it does not deadlock and it proves nothing either: the create blocks on the
    erase's locks and commits after it, so what the assertion would be pinning is the emulator's
    lock ordering.

    What that leaves un-eliminated is worth stating rather than implying, because it is real and it
    is U13's to close: a create whose transaction commits AFTER an erasure leaves a live journey
    whose device no longer exists. `_create_body` reads no device document at all, so nothing in the
    store refuses it. It is bounded -- no device means no endpoint, so nothing is ever pushed to it,
    and its own TTL removes it -- but it is returned by `device_journeys` and counted by the ceiling
    until then. The place to refuse it is the request path U13 puts over this store, not a fifth
    read inside the create transaction.
    """
    _emulator_setup()
    notification_store.upsert_device(
        device_id="device-1",
        client_id=CLIENT,
        endpoint="https://push.example.com/x/abc",
        p256dh="BPk",
        auth="auth",
        now=NOW,
    )
    for sid in ("j1", "j2", "j3"):
        notification_store.create_subscription(_web_push_spec(subscription_id=sid), now=NOW)

    bound = notification_store.MAX_ERASE_WRITES_PER_TRANSACTION
    notification_store.MAX_ERASE_WRITES_PER_TRANSACTION = 2
    refs = notification_store._Refs(notification_store._firestore_client())
    try:
        # Pass one, by hand: two journeys tombstoned, not complete, device still there.
        query = notification_store._query(
            "device_journeys",
            equality={
                "client_id": CLIENT,
                "channel": notification_store.CHANNEL_WEB_PUSH,
                "device_id": "device-1",
            },
            limit=2,
        )
        tombstoned, deleted, complete = notification_store._run_transaction(
            lambda txn: notification_store._erase_device_body(
                txn, refs, query, device_id="device-1", client_id=CLIENT, now=NOW
            )
        )
        assert tombstoned == ["j1", "j2"] and not deleted and not complete
        # The interleaved create, committed between the passes.
        notification_store.create_subscription(_web_push_spec(subscription_id="j4"), now=NOW)
        # The rest of the erasure, through the public loop.
        notification_store.erase_device(device_id="device-1", client_id=CLIENT, now=NOW)
    finally:
        notification_store.MAX_ERASE_WRITES_PER_TRANSACTION = bound

    assert notification_store.get_device("device-1") is None
    left = notification_store.device_journeys(device_id="device-1", client_id=CLIENT, now=NOW)
    assert left == [], f"an orphaned live journey survived the erasure: {[s.subscription_id for s in left]}"


def emulator_test_the_create_ceiling_counts_through_the_index():
    """U7's half a hermetic test cannot reach: a `count()` against a real Firestore.

    Three things are settled here and nowhere else. That the aggregation runs INSIDE a transaction
    at all -- the Python client does not accept one through `Transaction.get`, so the call shape is
    the aggregation's own `get(transaction=...)`, and a build that refused it would fail every
    create. That a tombstoned document does not count: `status` is an equality, and the whole
    reason the count is a query rather than a counter is that a dead row must not lock a client
    out. And that an expired one does not either: `expires_at > now` is the inequality, applied by
    the index over rows the reader never sees.

    The arrangement DISCRIMINATES rather than merely passing. The client holds three documents, of
    which one is live, against a ceiling of two: counted as written, three refuses; counted through
    the filters, one is admitted. Strip either predicate from the spec and this fails.
    """
    _emulator_setup()
    notification_store.create_subscription(_spec(subscription_id="live-sub"), now=NOW)
    notification_store.create_subscription(_spec(subscription_id="gone-sub"), now=NOW)
    notification_store.delete_subscription("gone-sub", client_id=CLIENT, now=NOW)
    # An expired document, written directly: `create_subscription` derives its own horizons, so the
    # only way to have one already past them is to put it there.
    stale = dict(_live_doc(subscription_id="stale-sub", now=NOW - timedelta(days=400)))
    _emulator_client().collection(notification_store.SUBSCRIPTIONS).document("stale-sub").set(stale)
    assert stale["expires_at"] < NOW, "the fixture must really be expired"

    created = notification_store.create_subscription(
        _spec(subscription_id="next-sub"), max_active=2, now=NOW
    )
    assert created.version == 1, "a tombstone and an expired row must not spend the ceiling"

    # And the ceiling still binds on what is actually live: two now, against a ceiling of two.
    try:
        notification_store.create_subscription(
            _spec(subscription_id="one-too-many"), max_active=2, now=NOW
        )
    except notification_store.QuotaExceeded:
        pass
    else:
        raise AssertionError("the counted ceiling did not bind")


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
    # The recipient document CARRIES the channel since U5, and `daily_cap` still does not: the
    # reader is what decides whether it can route a document (R20), and from U12 this collection
    # holds a `web_push` document per /varsel registration. `daily_cap` stays out because the pass
    # never reads it -- the cap is spent inside the store's own transaction.
    recipient = notification_store._subscription_from_doc(doc).as_recipient_document(NOW)
    assert recipient["channel"] == "webhook"
    assert "daily_cap" not in recipient
    legacy_recipient = notification_store._subscription_from_doc(legacy).as_recipient_document(NOW)
    assert legacy_recipient["channel"] == "webhook", legacy_recipient


def test_exactly_the_mirror_consuming_bodies_stamp_the_watermark_field():
    """U6/KTD5. Which bodies stamp is the mechanism, so it is pinned both ways.

    The rule is "every body that changes a field the mirror CONSUMES", which is not the same as
    "every body that touches a subscription", and both mistakes are silent:

    * Leaving `_touch_body` OUT -- which the unit's own list did -- means the mirror keeps a stale
      `expires_at` and drops a live subscription the client has just re-PUT. It is the write a
      client makes most often.
    * Leaving `_sent_body` IN defeats the watermark entirely: every delivery would make its
      subscription due for re-read, so the busiest documents would sync on every cycle.

    Read off the SOURCE rather than driven, because what has to hold is a property of the set of
    bodies -- a body added next year is the case that matters, and no behavioural check over
    today's five would see it.
    """
    source = pathlib.Path(notification_store.__file__).read_text()
    bodies = {}
    for chunk in source.split("\ndef ")[1:]:
        name = "_" + chunk.split("(", 1)[0].lstrip("_")
        if name.endswith("_body"):
            bodies[name] = chunk
    assert len(bodies) >= 13, f"only found {len(bodies)} bodies; did the naming change?"

    # Matched on the CALL, not on one spelling of the assignment: `_replace_body` builds its patch
    # and extends it, so a dict-literal-only pattern would read it as not stamping.
    stamping = {name for name, body in bodies.items() if "_mirror_stamp()" in body}
    # `_create_body` stamps through `_new_subscription_doc`, which is not a body itself.
    builder = source.split("\ndef _new_subscription_doc", 1)[1].split("\ndef ", 1)[0]
    if "_mirror_stamp()" in builder:
        stamping.add("_create_body")
    assert stamping == set(notification_store.MIRROR_STAMPING_BODIES), (
        f"stamping bodies are {sorted(stamping)}, the declared set is "
        f"{sorted(notification_store.MIRROR_STAMPING_BODIES)}. A new body that changes a field the "
        "mirror reads must stamp AND be declared; one that does not must do neither."
    )
    for never in (
        "_sent_body",
        "_claim_body",
        "_failed_body",
        "_terminal_body",
        # U12's two push-side ledgers, out for `_sent_body`'s reason one channel over: they are
        # written once per push, so stamping them would put one push on a hundred-thousand-strong
        # corridor through the watermark query as a hundred thousand documents. **The consequence
        # is a constraint on U13 and is recorded at the declaration: the cycle cannot read
        # `notified` or `pushed_trains` from the mirror**, because a stale mark re-offers a train
        # already pushed. Those reads go to the store, per push.
        "_mark_notified_body",
        "_record_push_body",
    ):
        assert never in bodies, f"{never} is gone; this check has stopped checking anything"
        assert never not in stamping, (
            f"{never} stamps the watermark: every delivery would then make its subscription due "
            "for re-read and the watermark would sync the busiest documents on every cycle"
        )
    # And the one whose OMISSION would be a privacy defect rather than a freshness one (U12). A
    # hard-deleted document matches no watermark query, so an erasure that did not stamp would
    # leave the mirror matching, wording and logging decisions about a traveller who asked to be
    # forgotten, for up to MIRROR_MAX_AGE_S. That is why the erasure tombstones rather than deletes.
    assert "_erase_device_body" in stamping


def test_the_watermark_field_is_the_stores_clock_and_advances_on_each_write():
    """The stamp is the SERVER's, so a body must never write its own `now` into it.

    The fake resolves the sentinel against a clock 37 s ahead of the one the bodies are handed, so
    an implementation that wrote `now` reads as 37 s behind and fails here. Without that gap the
    check would pass against either.
    """
    client = _FakeClient()
    _install(client)
    notification_store.create_subscription(_spec(), now=NOW)
    stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
    assert stored is not None
    created_stamp = stored[notification_store.MIRROR_FIELD]
    assert created_stamp == client.server_now, "the stamp is not the store's clock"
    assert created_stamp != stored["created_at"], "the stamp is the body's own `now`"

    # Each mirror-consuming write moves it forward. The horizons a touch extends are exactly what
    # the mirror tests liveness against, which is why the touch is in this list at all.
    for step, call in (
        ("touch", lambda: notification_store.touch_subscription(SID, client_id=CLIENT, now=NOW)),
        (
            "servability",
            lambda: notification_store.set_servability(
                SID, servable=True, expected_version=1, now=NOW
            ),
        ),
        ("delete", lambda: notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW)),
    ):
        client.server_now = client.server_now + timedelta(seconds=11)
        call()
        stored = client.doc(notification_store.SUBSCRIPTIONS, SID)
        assert stored is not None
        assert stored[notification_store.MIRROR_FIELD] == client.server_now, (
            f"{step} did not restamp the watermark field"
        )


def _web_push_spec(**over: Any) -> notification_store.SubscriptionSpec:
    """A `web_push` spec. `destination_id` is cleared because validation REFUSES it on this channel;
    `_spec`'s default is the webhook one.
    """
    base: dict[str, Any] = {
        "channel": notification_store.CHANNEL_WEB_PUSH,
        "destination_id": "",
        "device_id": "device-1",
        "from_name": "Oslo S",
        "to_name": "Ski stasjon",
        "consent_version": "2026-09-02.1",
    }
    base.update(over)
    return _spec(**base)


def test_the_two_channel_fields_are_required_and_refused_in_both_directions():
    """U12. Requiring is the obvious half; REFUSING is the one that matters.

    A `web_push` document carrying a `destination_id` would be offered to the delivery pass by any
    future query that forgets its channel filter -- which is a traveller's commute POSTed to a
    client's callback. A `webhook` document carrying a `device_id` is the mirror image: it would be
    returned by the `device_journeys` query as somebody's registration.
    """
    _web_push_spec().validate()
    _spec().validate()
    for over, expected in (
        (
            {
                "channel": notification_store.CHANNEL_WEB_PUSH,
                "destination_id": "dest-a",
                "device_id": "device-1",
            },
            "destination_id is not",
        ),
        ({"channel": notification_store.CHANNEL_WEB_PUSH, "destination_id": "", "device_id": ""}, "device_id is required"),
        ({"device_id": "device-1"}, "device_id is not"),
        ({"destination_id": ""}, "destination_id is required"),
    ):
        try:
            _spec(**over).validate()
        except notification_store.Conflict as exc:
            assert expected in str(exc), (over, str(exc))
        else:
            raise AssertionError(f"{over} was accepted")


def test_the_webhook_definition_digest_did_not_move_when_device_id_arrived():
    """The digest is a STORED value, so extending its material is a migration.

    `device_id` shares `destination_id`'s position through an `or` rather than taking one of its
    own: exactly one of the two is non-empty by construction, so a webhook spec's material is byte
    for byte what it was. A second position would add a separator, change every hash, and turn every
    client's idempotent re-POST into a Conflict on the deploy that introduced the field.

    Recomputed here from the literal pre-U12 material rather than from a frozen constant, so the
    check still means something if the surrounding fields ever move.
    """
    import hashlib

    spec = _spec()
    material = "|".join(
        (
            spec.client_id,
            spec.destination_id,
            spec.from_stop,
            spec.to_stop,
            spec.begin,
            spec.end,
            ",".join(str(d) for d in sorted(set(spec.days))),
            spec.external_ref,
            "test" if spec.test else "live",
        )
    )
    assert notification_store._definition_digest(spec) == hashlib.sha256(material.encode()).hexdigest()[:32]
    # And the two channels cannot collide: one carries a destination where the other carries a
    # device, in the same slot, so the digests differ as long as the two ids do.
    assert notification_store._definition_digest(_web_push_spec()) != notification_store._definition_digest(spec)


def test_a_web_push_registration_has_an_inactivity_horizon_and_no_lifetime_one():
    """U14's horizon rule, which INVERTS U12's, and the product change inside it.

    U12 gave this channel the lifetime horizon alone, reasoning that the 60-day inactivity one is
    about a client's forgotten integration and that a traveller not opening the PWA for two months
    is what a working alert service looks like. Both halves of that are right and the conclusion
    was wrong: what it left was a clock anchored on CREATION that nothing could reset, so a
    commuter opening the app every morning lost the journey on day 181 -- silently, since it stops
    matching, drops out of `device_journeys` and is TTL-deleted within ~24 h. A storage-limitation
    rule is for ageing out what nobody uses. So the channel keeps an inactivity horizon at its own
    longer number instead, and `note_web_push_activity` is what moves the anchor.

    It is still a change from today's GCS blob, which has NO expiry on a registration at all: 180
    days of not opening the app is a real ceiling somebody has to have chosen.

    The field is OMITTED rather than written far ahead: a field that lies about its meaning is worse
    than an absent one, and `subscription_is_live` reads the absence correctly.
    """
    doc = notification_store._new_subscription_doc(_web_push_spec(), NOW)
    assert "lifetime_expires_at" not in doc
    inactive = NOW + timedelta(days=notification_store.WEB_PUSH_INACTIVITY_DAYS)
    assert doc["inactivity_expires_at"] == inactive
    assert doc[notification_store.TTL_FIELD] == inactive
    assert notification_store.subscription_is_live(doc, NOW)
    assert not notification_store.subscription_is_live(doc, inactive + timedelta(seconds=1))
    # The horizon is the traveller's, not the client's: sixty days is the number that would have
    # expired a commuter over a summer holiday.
    assert notification_store.WEB_PUSH_INACTIVITY_DAYS > notification_store.SUBSCRIPTION_INACTIVITY_DAYS
    # A webhook document is untouched: both horizons, TTL the minimum.
    webhook = notification_store._new_subscription_doc(_spec(), NOW)
    assert webhook["inactivity_expires_at"] == NOW + timedelta(
        days=notification_store.SUBSCRIPTION_INACTIVITY_DAYS
    )
    assert webhook["lifetime_expires_at"] == NOW + timedelta(
        days=notification_store.SUBSCRIPTION_MAX_LIFETIME_DAYS
    )


def test_the_channel_only_fields_are_absent_one_way_and_an_empty_string_the_other():
    """The absence argument holds for the web_push set and NOT for `destination_id`. Pinned because
    three documents claimed otherwise until prd's cutover showed it.

    `_channel_fields` writes the web_push-only fields conditionally, so a webhook document LACKS
    `device_id` -- and a Firestore equality does not match a document that lacks a field, which is
    what makes `device_journeys` unable to return a client's subscription as a property of the DATA.
    `destination_id` is written by `_new_subscription_doc` itself, outside that helper, so a
    web_push document carries it as `""`. `.claude/rules/notification-webhooks.md`, the operations
    document and the helper's own docstring all generalised the first half into "each channel's
    fields are absent on the other"; dev's webhook documents happened to agree, because they predate
    the `device_id` field entirely, and prd's 275 imported registrations are what showed the other
    half was never true.

    The half that MATTERS is asserted here too, and it is the one the security argument rests on: a
    non-empty `destination_id` is refused on this channel, so no web_push document can name a real
    destination however this dict is built.

    Whoever makes the two symmetric must change `_subscription_from_doc` and
    `_delivery_subscription_from_doc` in the same commit -- both index `doc["destination_id"]`
    directly, so omitting the field KeyErrors every web_push read in production.
    """
    web_push = notification_store._new_subscription_doc(_web_push_spec(), NOW)
    webhook = notification_store._new_subscription_doc(_spec(), NOW)

    # The direction that holds, and the mechanism the queries rely on.
    assert "device_id" not in webhook, (
        "a webhook document must LACK device_id, or device_journeys stops being unable to return "
        "one as a property of the data"
    )
    assert web_push["device_id"], "the web_push document names its device"

    # The direction that does not. Asserted as it IS, so the day somebody makes it absent this
    # fails and sends them to the two direct-index readers before production does.
    assert "destination_id" in web_push and web_push["destination_id"] == "", (
        "destination_id is written outside _channel_fields and is an empty string here; if this "
        "is now absent, _subscription_from_doc and _delivery_subscription_from_doc must have "
        "stopped indexing it directly"
    )

    # What the refusal guarantees regardless: no web_push document names a real destination. The
    # refusal lives on `SubscriptionSpec.validate`, which `create_subscription` and
    # `replace_subscription` call -- NOT in `_new_subscription_doc`, which builds the body of an
    # already-validated spec. Asserted through the validator rather than the builder, or this
    # would pass for the wrong reason.
    try:
        _web_push_spec(destination_id=DEST).validate()
    except notification_store.Conflict:
        pass
    else:
        raise AssertionError("a non-empty destination_id must be refused on a web_push spec")


def test_a_document_with_no_channel_still_needs_both_horizons():
    """The fail-closed half survives the dispatch, and this is the assertion that says so.

    U5's convention is that a document without `channel` MEANS webhook, which is what every document
    written before that field existed is. So the dispatch cannot weaken them: they still need both
    horizons, and a `web_push` document missing its own one is still dead.

    That second half is what makes U14's inversion safe to deploy. The field a `web_push` document
    must be inside moved from `lifetime_expires_at` to `inactivity_expires_at`, so a document
    written under the old rule reads as expired rather than as immortal -- the direction a horizon
    change has to fail in. Nothing in any environment holds such a document (no deployment selects
    the store backend, and `varsel_state` is the only writer of the channel), which is what makes
    the inversion a free choice rather than a migration.
    """
    doc = notification_store._new_subscription_doc(_spec(), NOW)
    doc.pop("channel")
    assert notification_store.subscription_is_live(doc, NOW)
    doc.pop("inactivity_expires_at")
    assert not notification_store.subscription_is_live(doc, NOW), (
        "a channel-less document is a webhook document and still needs both horizons"
    )
    web_push = notification_store._new_subscription_doc(_web_push_spec(), NOW)
    web_push.pop("inactivity_expires_at")
    assert not notification_store.subscription_is_live(web_push, NOW)
    # A document written under U12's rule carries the other field and nothing else, and is dead.
    old_shape = notification_store._new_subscription_doc(_web_push_spec(), NOW)
    old_shape.pop("inactivity_expires_at")
    old_shape["lifetime_expires_at"] = NOW + timedelta(days=90)
    assert not notification_store.subscription_is_live(old_shape, NOW), (
        "a pre-U14 web_push document must read as expired, never as live on the retired field"
    )


def test_a_renewal_and_an_amendment_each_keep_their_own_channels_horizon_shape():
    """Both `_horizons` call sites that have to read the channel off the STORED document.

    `_create_body` always knew it -- the channel is on the spec. `_touch_body` and `_replace_body`
    take it off the document they just read, and until U14 neither passed it at all: both called
    `_horizons` with its `webhook` default. Unreachable while no web_push document existed
    anywhere, and the primary path the moment a renewal is what the browser channel's horizon
    rests on. Neither failure raises. A renewed registration would carry a 60-day inactivity
    horizon -- the number chosen for a client's forgotten integration -- and a
    `lifetime_expires_at` this channel is not supposed to have, and would then expire at whichever
    came first, with the document looking perfectly well-formed.
    """
    later = NOW + timedelta(days=30)
    expected = later + timedelta(days=notification_store.WEB_PUSH_INACTIVITY_DAYS)

    def written(body: str) -> dict[str, Any]:
        client = _FakeClient()
        refs = notification_store._Refs(client)
        client.put(
            notification_store.SUBSCRIPTIONS,
            SID,
            notification_store._new_subscription_doc(_web_push_spec(), NOW),
        )
        txn = _Txn()
        with _NoClockNoLog():
            if body == "touch":
                notification_store._touch_body(
                    txn, refs, subscription_id=SID, client_id=CLIENT, now=later
                )
            else:
                notification_store._replace_body(
                    txn, refs, spec=_web_push_spec(begin="06:30"), expected_version=1, now=later
                )
        assert len(txn.writes) == 1, txn.writes
        return txn.writes[0][2] or {}

    for body in ("touch", "replace"):
        patch = written(body)
        assert patch["inactivity_expires_at"] == expected, body
        assert patch[notification_store.TTL_FIELD] == expected, body
        assert "lifetime_expires_at" not in patch, (
            f"a {body} gave a web_push registration the horizon this channel does not have"
        )


def test_the_renewal_pass_writes_nothing_until_the_anchor_is_actually_stale():
    """The cut that makes a renewal affordable, and the arithmetic behind the constant.

    The PWA polls the journey list every 60 s, so a pass that renewed on every read would be one
    write per registration per minute per open app. The cut is a week against a 180-day horizon,
    so what it costs is that a registration expires 173-180 days after its last use rather than
    exactly 180 -- and what it buys is that all but one read a week is free.

    The device document rides on the same event and is asserted here rather than separately,
    because renewing the journeys while letting the device that owns them expire is the same
    defect one document along.
    """
    client = _FakeClient()
    _install(client)
    try:
        doc = notification_store._new_subscription_doc(_web_push_spec(), NOW)
        client.put(notification_store.SUBSCRIPTIONS, SID, doc)
        client.put(
            notification_store.DEVICES,
            "device-1",
            notification_store._new_device_doc(
                device_id="device-1",
                client_id=CLIENT,
                endpoint="https://push.example.com/x",
                p256dh="k",
                auth="a",
                brand="entur",
                now=NOW,
            ),
        )
        subs = [notification_store._subscription_from_doc(doc)]
        cut = notification_store.WEB_PUSH_RENEW_AFTER_DAYS

        fresh = notification_store.note_web_push_activity(
            "device-1", client_id=CLIENT, subscriptions=subs, now=NOW + timedelta(days=cut - 1)
        )
        assert fresh == 0 and client.transactions == [], "a fresh anchor was rewritten"

        at_the_cut = NOW + timedelta(days=cut)
        assert (
            notification_store.note_web_push_activity(
                "device-1", client_id=CLIENT, subscriptions=subs, now=at_the_cut
            )
            == 1
        )
        stored = client.doc(notification_store.SUBSCRIPTIONS, SID) or {}
        assert stored["inactivity_expires_at"] == at_the_cut + timedelta(
            days=notification_store.WEB_PUSH_INACTIVITY_DAYS
        )
        device = client.doc(notification_store.DEVICES, "device-1") or {}
        assert device[notification_store.TTL_FIELD] == at_the_cut + timedelta(
            days=notification_store.SUBSCRIPTION_MAX_LIFETIME_DAYS
        ), "the journeys were renewed and the device document they belong to was not"
    finally:
        _uninstall()


def test_a_renewal_cannot_recreate_a_device_document_an_erasure_removed():
    """«Slett alt» deletes the device document, and a renewal reads before it writes.

    The race is ordinary rather than exotic: the journey list and the erasure are two requests from
    the same phone, and the list's renewal can land after the delete. Writing unconditionally would
    put back a husk carrying the very id the traveller asked us to forget -- with a fresh 180-day
    TTL on it, so the resurrection would outlive the erasure by half a year.
    """
    client = _FakeClient()
    _install(client)
    try:
        assert notification_store.renew_device("gone-1", client_id=CLIENT, now=NOW) is None
        assert client.doc(notification_store.DEVICES, "gone-1") is None
        assert client.transactions, "the renewal must at least have looked"
        assert client.transactions[-1].writes == [], "a missing device must not be written"
    finally:
        _uninstall()


def test_the_channel_only_fields_are_absent_rather_than_empty_on_the_other_channel():
    """Absent, not `""`. A Firestore equality filter does not match a document that LACKS the field,
    so an absent `device_id` is what makes it impossible for the `device_journeys` query to return a
    webhook subscription -- a property of the DATA rather than of the filter. Empty strings would
    move that guarantee into whoever writes the query next.
    """
    webhook = notification_store._new_subscription_doc(_spec(), NOW)
    for field in ("device_id", "from_name", "to_name", "consent_version", "notified", "pushed_trains", "daily_pushes"):
        assert field not in webhook, field
    web_push = notification_store._new_subscription_doc(_web_push_spec(), NOW)
    assert web_push["device_id"] == "device-1"
    assert web_push["from_name"] == "Oslo S" and web_push["to_name"] == "Ski stasjon"
    assert web_push["consent_version"] == "2026-09-02.1"
    assert web_push["notified"] == {} and web_push["pushed_trains"] == {} and web_push["daily_pushes"] == {}


def test_the_liveness_map_carries_every_field_the_predicate_reads():
    """A structural pin, because the failure it catches is silent in the worst possible way.

    `MirrorEntry.liveness` is a hand-built map handed to `subscription_is_live`, and the predicate
    grew a `channel` read at U12. Without it every `web_push` entry reads as a webhook one, demands
    an inactivity horizon it does not have, and the mirror holds the entry, reports its size, and
    serves it to NOBODY. That is what this test found while it was being written.

    Asserted off the SOURCE of the predicate and its helper rather than by behaviour: what has to
    hold is a property of the field set, and the next field added to the predicate fails here in
    exactly the same way.
    """
    import ast
    import inspect

    read: set[str] = set()
    # `_channel_of` is in the walk because the predicate reads `channel` THROUGH it since U14: a
    # scan of the two obvious functions stopped seeing the field the moment the dispatch moved into
    # a helper, and the assertion below went green while the map could have lost it.
    for fn in (
        notification_store.subscription_is_live,
        notification_store._horizon_fields,
        notification_store._channel_of,
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                read.add(str(node.args[0].value))
        # The two horizon names arrive as a tuple the predicate iterates, not as `get` literals.
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and str(node.value).endswith("_expires_at"):
                read.add(str(node.value))
    read |= {notification_store.TTL_FIELD}
    assert {"status", "channel", "lifetime_expires_at", "inactivity_expires_at"} <= read, read
    entry = notification_store._mirror_entry(notification_store._new_subscription_doc(_spec(), NOW))
    missing = read - set(entry.liveness)
    assert not missing, (
        f"MirrorEntry.liveness does not carry {sorted(missing)}, which subscription_is_live reads. "
        "The mirror would hold those entries, report them in its size, and serve them to nobody."
    )


# ---- Devices and the erasure path (U12) --------------------------------------------------------


def _device_doc(client: _FakeClient, **over: Any) -> dict[str, Any]:
    """A stored device built by the REAL writer, never a hand-written dict (`_live_doc`'s rule)."""
    base: dict[str, Any] = {
        "device_id": "device-1",
        "client_id": CLIENT,
        "endpoint": "https://push.example.com/x/abc",
        "p256dh": "BPk-key",
        "auth": "auth-token",
        "brand": "entur",
        "now": NOW,
    }
    base.update(over)
    doc = notification_store._new_device_doc(**base)
    client.put(notification_store.DEVICES, doc["device_id"], doc)
    return doc


def _journey_rows(client: _FakeClient, ids: list[str], **over: Any) -> list[dict[str, Any]]:
    """`n` live `web_push` registrations on one device, stored AND paged for the erase query."""
    rows = []
    for sid in ids:
        doc = notification_store._new_subscription_doc(
            _web_push_spec(subscription_id=sid, **over), NOW
        )
        client.put(notification_store.SUBSCRIPTIONS, sid, doc)
        # The STORED dict, by reference, so the page reflects what earlier writes did to it. The
        # fake's `put` copies, and paging the copy would make a second erase pass re-read rows the
        # first one had already tombstoned -- a fake that cannot show idempotence.
        rows.append(client.docs[f"{notification_store.SUBSCRIPTIONS}/{sid}"])
    client.page("device_journeys", notification_store.SUBSCRIPTIONS, rows)
    return rows


def test_an_upsert_keeps_the_original_signup_and_clears_the_tombstone():
    """`/varsel`'s own rule (2026-09-04), not an invention here: a device whose push subscription
    lapsed and came back is the SAME device. Restamping its signup would make every lapse read as a
    new install in the device report, and the tombstone going with it is the same fact from the
    other side -- there is an endpoint again, so there is nothing left to explain.
    """
    client = _FakeClient()
    _install(client)
    try:
        _device_doc(client)
        notification_store.tombstone_device(device_id="device-1", client_id=CLIENT, reason="expired")
        stored = client.doc(notification_store.DEVICES, "device-1")
        assert stored["tombstone"]["reason"] == "expired"
        assert stored["endpoint"] == "" and stored["p256dh"] == "" and stored["auth"] == ""
        assert stored["tombstone"]["original_created_at"] == NOW

        later = NOW + timedelta(days=3)
        device = notification_store.upsert_device(
            device_id="device-1",
            client_id=CLIENT,
            endpoint="https://push.example.com/x/new",
            p256dh="BPk-new",
            auth="auth-new",
            now=later,
        )
        assert device.created_at == NOW, "an upsert is a refresh, not a new install"
        assert device.tombstone == {} and device.pushable
        assert client.doc(notification_store.DEVICES, "device-1")["last_seen_at"] == later
    finally:
        _uninstall()


def test_a_refresh_is_never_refused_by_the_device_ceiling():
    """Counted only for a device that does not exist yet. A client AT its ceiling that could not
    update an endpoint would have every expired subscription become permanent -- a traveller who
    silently stops being alerted, which is the failure the whole re-subscribe path exists to avoid.
    """
    client = _FakeClient()
    _install(client)
    try:
        _device_doc(client)
        client.page("device_count", notification_store.DEVICES, [{"device_id": "d"} for _ in range(9)])
        notification_store.upsert_device(
            device_id="device-1",
            client_id=CLIENT,
            endpoint="https://push.example.com/x/refreshed",
            p256dh="k",
            auth="a",
            max_devices=1,
        )
        try:
            notification_store.upsert_device(
                device_id="device-2",
                client_id=CLIENT,
                endpoint="https://push.example.com/x/other",
                p256dh="k",
                auth="a",
                max_devices=1,
            )
        except notification_store.QuotaExceeded:
            pass
        else:
            raise AssertionError("a NEW device past the ceiling must be refused")
        assert client.doc(notification_store.DEVICES, "device-2") is None, "refused before any write"
    finally:
        _uninstall()


def test_erasure_tombstones_the_journeys_so_the_mirror_learns_and_deletes_the_device():
    """The decision this unit turns on, and the reason is not visible in the transaction.

    The obvious «Slett alt» is `transaction.delete` on every document. A deleted document matches no
    query -- including `mirror_sync`'s watermark query -- so the poller's mirror would keep the
    traveller's journeys for up to MIRROR_MAX_AGE_S (6 h) and keep matching, wording and logging
    decisions about somebody who asked to be forgotten. A TOMBSTONE travels the path the mirror
    already has.

    What survives is a husk: id, status, TTL. The route, the window, the typed names, the device id
    and the three push ledgers are cleared in the SAME write, so nothing personal outlives the
    transaction -- unlike a client's revocation, which redacts only once the cleanup completes.
    """
    client = _FakeClient()
    _install(client)
    try:
        _device_doc(client)
        _journey_rows(client, ["j1", "j2"])
        result = notification_store.erase_device(device_id="device-1", client_id=CLIENT)
        assert result.complete and result.device_deleted and result.journeys_tombstoned == 2
        # READS BEFORE WRITES, and this one is not house style: Firestore raises
        # `ReadAfterWriteError` on a read that follows a write inside a transaction, so a body that
        # read the device BELOW the tombstone loop would raise on every erasure that has anything
        # to erase -- which is every real one. The emulator found exactly that while this was being
        # written; the fake records the op order, so this is where it is checked.
        for txn in client.transactions:
            assert _no_read_after_write(txn), f"a read follows a write: {txn.ops}"
        assert client.doc(notification_store.DEVICES, "device-1") is None, "the device is GONE"
        for sid in ("j1", "j2"):
            husk = client.doc(notification_store.SUBSCRIPTIONS, sid)
            assert husk is not None, "a tombstone, not a deletion -- the mirror has to see it"
            assert husk["status"] == "deleted"
            assert husk[notification_store.MIRROR_FIELD] == client.server_now, (
                "without the stamp the mirror never learns and keeps matching an erased traveller"
            )
            for cleared in ("from_stop", "to_stop", "from_name", "to_name", "device_id"):
                assert husk[cleared] == "", cleared
            for emptied in ("notified", "pushed_trains", "daily_pushes"):
                assert husk[emptied] == {}, emptied
            assert not notification_store.subscription_is_live(husk, NOW)
    finally:
        _uninstall()


def test_a_second_erasure_is_a_no_op():
    """Idempotent in both directions: no device, and no journey that is not already a tombstone.
    A traveller who taps twice, or a retried request, must not produce a second audit line's worth
    of writes -- and must not raise, because the answer to "is it gone" is yes.
    """
    client = _FakeClient()
    _install(client)
    try:
        _device_doc(client)
        _journey_rows(client, ["j1"])
        notification_store.erase_device(device_id="device-1", client_id=CLIENT)
        writes_before = sum(len(t.writes) for t in client.transactions)
        again = notification_store.erase_device(device_id="device-1", client_id=CLIENT)
        assert again.complete and again.journeys_tombstoned == 0 and not again.device_deleted
        assert sum(len(t.writes) for t in client.transactions) == writes_before, "no second write"
    finally:
        _uninstall()


def test_erasure_drains_across_transactions_rather_than_refusing_a_busy_device():
    """Firestore allows 500 writes per transaction. A device with more journeys than one pass can
    carry must not become a traveller who CANNOT exercise erasure because they registered a lot --
    so the pass is bounded and the caller loops, and the device document goes in the pass that
    drains the last journey so a retry after a partial one still finds the rest.
    """
    client = _FakeClient()
    _install(client)
    bound = notification_store.MAX_ERASE_WRITES_PER_TRANSACTION
    notification_store.MAX_ERASE_WRITES_PER_TRANSACTION = 2
    try:
        _device_doc(client)
        _journey_rows(client, ["j1", "j2", "j3"])
        result = notification_store.erase_device(device_id="device-1", client_id=CLIENT)
        assert result.passes >= 2, result
        assert result.complete and result.device_deleted and result.journeys_tombstoned == 3
    finally:
        notification_store.MAX_ERASE_WRITES_PER_TRANSACTION = bound
        _uninstall()


def test_a_mark_on_an_erased_journey_raises_and_creates_nothing():
    """The push that was in flight when «Slett alt» landed. `transaction.update` on a missing
    document raises in the client too -- but only after the write is attempted, and with a message
    about a path. Reading first is what makes the outcome legible AND what guarantees the erased
    registration is not resurrected by a `set`.
    """
    client = _FakeClient()
    _install(client)
    try:
        _journey_rows(client, ["j1"])
        notification_store.mark_notified(subscription_id="j1", situation_numbers=("ENT:S:1",))
        assert client.doc(notification_store.SUBSCRIPTIONS, "j1")["notified"]
        client.docs[f"{notification_store.SUBSCRIPTIONS}/j1"]["status"] = "deleted"
        for call in (
            lambda: notification_store.mark_notified(subscription_id="j1", situation_numbers=("ENT:S:2",)),
            lambda: notification_store.record_push(subscription_id="j1", train_ref="VYG:L1:07:18"),
            lambda: notification_store.mark_notified(subscription_id="gone", situation_numbers=("x",)),
        ):
            try:
                call()
            except notification_store.NotFound:
                pass
            else:
                raise AssertionError("an erased registration must not be written to")
        assert client.doc(notification_store.SUBSCRIPTIONS, "gone") is None
    finally:
        _uninstall()


def test_several_situations_are_marked_in_one_transaction():
    """The morning digest covers every removed train in a window at once (plan 2026-09-23-001, U4):
    one call, one transaction, every mark written."""
    client = _FakeClient()
    _install(client)
    try:
        _journey_rows(client, ["j1"])
        notification_store.mark_notified(subscription_id="j1", situation_numbers=("ENT:S:1",))
        notification_store.mark_notified(
            subscription_id="j1", situation_numbers=("KIX:PlanRemoved:a", "KIX:PlanRemoved:b")
        )
        marks = client.doc(notification_store.SUBSCRIPTIONS, "j1")["notified"]
        assert set(marks) == {"ENT:S:1", "KIX:PlanRemoved:a", "KIX:PlanRemoved:b"}, marks
    finally:
        _uninstall()


def test_the_two_mark_bodies_are_idempotent_under_one_clock():
    """The transaction-suite rule: a body may run more than once, so two runs with one clock must
    write the same document. `record_push` is the interesting one -- it INCREMENTS, so a naive
    implementation is the counter-example, and what makes it safe is that the increment is computed
    from the document the body just read rather than accumulated across runs.
    """
    client = _FakeClient()
    _install(client)
    try:
        _journey_rows(client, ["j1"])
        refs = notification_store._Refs(client)
        first = notification_store._mark_notified_body(
            _Txn(), refs, subscription_id="j1", situation_numbers=("ENT:S:1",), now=NOW
        )
        second = notification_store._mark_notified_body(
            _Txn(), refs, subscription_id="j1", situation_numbers=("ENT:S:1",), now=NOW
        )
        assert first["notified"] == second["notified"]
        a = notification_store._record_push_body(
            _Txn(), refs, subscription_id="j1", train_ref="T", now=NOW
        )
        b = notification_store._record_push_body(
            _Txn(), refs, subscription_id="j1", train_ref="T", now=NOW
        )
        assert a["daily_pushes"] == b["daily_pushes"] == {notification_store.oslo_day(NOW): 1}
        assert a["pushed_trains"] == b["pushed_trains"]
    finally:
        _uninstall()


def test_one_devices_journeys_are_read_from_the_store_and_liveness_is_applied_here():
    """R15: a traveller who has just created a journey sees it in the next `list_journeys`, so the
    request path reads the STORE and not the mirror -- which is at best as fresh as the sync at the
    start of this cycle. Liveness is applied in Python for `load_active_subscriptions`' reason: a
    second inequality would need its own index and would exclude every document lacking the field.
    """
    client = _FakeClient()
    _install(client)
    try:
        rows = _journey_rows(client, ["j1", "j2"])
        rows[1]["status"] = "deleted"
        client.page("device_journeys", notification_store.SUBSCRIPTIONS, rows)
        got = notification_store.device_journeys(device_id="device-1", client_id=CLIENT)
        assert [s.subscription_id for s in got] == ["j1"], "a tombstone is not a registration"
        _name, equality, _ineq, _limit = next(
            call for call in client.query_calls if call[0] == "device_journeys"
        )
        assert equality == {
            "client_id": CLIENT,
            "channel": notification_store.CHANNEL_WEB_PUSH,
            "device_id": "device-1",
        }, equality
    finally:
        _uninstall()


def test_the_create_ceiling_is_counted_per_channel():
    """U5's surviving half. U6 took the other one with the query it filtered.

    The load query is gone -- the mirror holds every channel and filters in Python -- so the only
    place a `channel` equality is still load-bearing is the client's create-time ceiling, which is
    its WEBHOOK ceiling and must not be spent by a traveller's own registration (U12-U14). Asserted
    on the spec rather than on a fake query because `_query` builds every filter from the specs;
    `test_every_filter_goes_through_the_query_helper` is what makes that true.
    """
    specs = {spec.name: spec for spec in notification_store.QUERY_SPECS}
    assert "channel" in specs["subscription_count"].equality
    # The listing is deliberately NOT in that set: a client lists what it created, and until U13
    # everything it can create is a webhook. Adding the equality here would need another index for
    # no question anybody asks.
    assert "channel" not in specs["subscriptions"].equality
    # And the mirror's own two queries filter nothing, which is what lets one mirror serve both
    # channels when U12 puts /varsel's registrations in this collection.
    assert specs["mirror_seed"].equality == () and specs["mirror_sync"].equality == ()


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


# ---- The subscription mirror (U6) ----------------------------------------------------------------


class _RecordingLogger:
    """Captures (level, structured fields) per log call. Promoted from a local class in the TTL-lag
    test when the mirror gave a second test the same need."""

    def __init__(self, records: list[tuple[str, dict[str, Any]]]) -> None:
        self._records = records

    def __getattr__(self, level: str):
        def _log(_msg: str, *_args: Any, **kwargs: Any) -> None:
            self._records.append((level, dict((kwargs.get("extra") or {}).get("fields") or {})))

        return _log


def _seed_mirror(client: _FakeClient, docs: list[dict[str, Any]], *, now: datetime = NOW) -> None:
    """Put documents in the mirror the pass now reads through, as the seed would.

    Since U6 `recipient_documents` serves the mirror rather than querying, so a test that stages a
    `load_active_subscriptions` page is staging a page nothing reads.
    """
    _mirror_page(client, "mirror_seed", docs)
    notification_store.subscription_mirror().seed(now=now)


def _mirror_page(client: _FakeClient, spec: str, docs: list[dict[str, Any]]) -> None:
    """Stage what one mirror query returns. The fake evaluates no inequality -- it serves what a
    test puts there -- so the QUERY's own shape is asserted separately off `query_calls`."""
    client.pages[spec] = [
        (f"{notification_store.SUBSCRIPTIONS}/{d['subscription_id']}", d) for d in docs
    ]


def _mirrored(**over: Any) -> dict[str, Any]:
    """A stored document as the real writer produces it, with the watermark already resolved.

    `mirror_at` is popped BEFORE the document is built: it is this helper's own argument, not one
    of `SubscriptionSpec`'s.
    """
    mirror_at = over.pop("mirror_at", NOW)
    doc = _live_doc(**over)
    doc[notification_store.MIRROR_FIELD] = mirror_at
    return doc


def test_the_mirror_seeds_from_the_whole_collection_and_pages_with_a_cursor():
    """Scenario 1. The startup read is the only full read the mirror ever does."""
    client = _FakeClient()
    _install(client)
    docs = [_mirrored(subscription_id=f"s{i}") for i in range(5)]
    _mirror_page(client, "mirror_seed", docs)

    beats: list[int] = []
    mirror = notification_store.SubscriptionMirror()
    page_size = notification_store.MIRROR_PAGE_SIZE
    notification_store.MIRROR_PAGE_SIZE = 2
    try:
        result = mirror.seed(now=NOW, beat=lambda: beats.append(1))
    finally:
        notification_store.MIRROR_PAGE_SIZE = page_size

    assert mirror.ready is True
    assert mirror.size() == 5, result
    assert result.pages == 3, "2 + 2 + 1: the short page is what ends the read"
    assert len(beats) == 2, "the heartbeat moves BETWEEN pages, not after the last one"
    assert {e.subscription_id for e in mirror.recipients(NOW)} == {f"s{i}" for i in range(5)}
    # Ordered by `created_at`, never by the watermark field -- a document written before that field
    # existed has to be in the read that seeds the mirror.
    spec = next(s for s in notification_store.QUERY_SPECS if s.name == "mirror_seed")
    assert spec.order_by == (("created_at", "ASCENDING"),), spec
    assert spec.equality == () and spec.inequality == (), "the seed filters nothing"


def test_the_seed_needs_no_composite_index_and_neither_does_the_sync():
    """Both mirror queries are single-field, which is why U6 adds no index and races no deploy."""
    for name in ("mirror_seed", "mirror_sync"):
        spec = next(s for s in notification_store.QUERY_SPECS if s.name == name)
        assert spec.index_fields == (), f"{name} would need a composite index"


def test_a_sync_applies_only_what_changed_and_asks_from_the_watermark_less_the_overlap():
    """Scenarios 2 and 3. The overlap is what stops a late commit being stepped over forever."""
    client = _FakeClient()
    _install(client)
    first = _mirrored(subscription_id="s1")
    _mirror_page(client, "mirror_seed", [first])
    mirror = notification_store.SubscriptionMirror()
    mirror.seed(now=NOW)
    assert mirror.size() == 1

    later = NOW + timedelta(minutes=5)
    changed = _mirrored(subscription_id="s2", mirror_at=later)
    _mirror_page(client, "mirror_sync", [changed])
    client.query_calls.clear()
    result = mirror.sync(now=later)

    assert result.synced == 1 and result.removed == 0, result
    assert mirror.size() == 2
    name, _eq, inequality, _limit = client.query_calls[0]
    assert name == "mirror_sync"
    asked = inequality[notification_store.MIRROR_FIELD]
    assert asked == NOW - timedelta(seconds=notification_store.MIRROR_SYNC_OVERLAP_S), asked

    # Re-reading the same document is a no-op, which is what makes the overlap safe.
    client.query_calls.clear()
    again = mirror.sync(now=later)
    assert mirror.size() == 2, "applying the overlap twice changed the mirror"
    assert again.synced == 1, "the overlap deliberately re-reads; it must simply re-apply"


def test_a_tombstone_removes_the_subscription_from_the_mirror():
    """Scenario 4. The one path that carries a revocation the instant it is written."""
    client = _FakeClient()
    _install(client)
    _mirror_page(client, "mirror_seed", [_mirrored(subscription_id="s1")])
    mirror = notification_store.SubscriptionMirror()
    mirror.seed(now=NOW)

    later = NOW + timedelta(minutes=1)
    tombstoned = _mirrored(subscription_id="s1")
    tombstoned["status"] = "deleted"
    tombstoned[notification_store.MIRROR_FIELD] = later
    _mirror_page(client, "mirror_sync", [tombstoned])
    result = mirror.sync(now=later)

    assert result.removed == 1 and result.synced == 0, result
    assert mirror.size() == 0
    assert mirror.recipients(later) == []


def test_a_subscription_whose_horizon_passes_is_dropped_on_the_read_with_no_write():
    """Scenario 5, and the reason liveness is applied on the READ.

    Nothing writes a document when its inactivity horizon passes at 03:00, so no sync can carry the
    news. Tested at read time, an expired subscription is never matched even by a mirror that has
    not synced since.
    """
    client = _FakeClient()
    _install(client)
    _mirror_page(client, "mirror_seed", [_mirrored(subscription_id="s1")])
    mirror = notification_store.SubscriptionMirror()
    mirror.seed(now=NOW)
    assert len(mirror.recipients(NOW)) == 1

    past_horizon = NOW + timedelta(days=notification_store.SUBSCRIPTION_INACTIVITY_DAYS + 1)
    assert mirror.recipients(past_horizon) == []
    assert mirror.size() == 1, "the entry is still held; it is the READ that refuses it"


def test_the_mirror_serves_one_channel_and_holds_them_all():
    """U5's filter, now in Python: the mirror is the store for every channel (U12 needs it)."""
    client = _FakeClient()
    _install(client)
    webhook_doc = _mirrored(subscription_id="client-sub")
    _mirror_page(
        client,
        "mirror_seed",
        [
            webhook_doc,
            _mirrored(subscription_id="browser-sub", channel=notification_store.CHANNEL_WEB_PUSH),
        ],
    )
    mirror = notification_store.SubscriptionMirror()
    mirror.seed(now=NOW)

    assert mirror.size() == 2, "both channels are held"
    assert [e.subscription_id for e in mirror.recipients(NOW)] == ["client-sub"]
    on_browser = mirror.recipients(NOW, channel=notification_store.CHANNEL_WEB_PUSH)
    assert [e.subscription_id for e in on_browser] == ["browser-sub"]
    # **The document the pass receives is byte-identical to the one the store's own reader builds.**
    # This is the assertion the whole unit rests on: U6 changes where the pass gets its documents,
    # not what they say, and a mirror that served a subtly different shape would change matching
    # behaviour with nothing in the diff that looks like a matching change.
    from_store = notification_store._subscription_from_doc(webhook_doc).as_recipient_document(NOW)
    assert mirror.documents(NOW) == [from_store]


def test_one_entry_is_answered_by_key_and_still_applies_liveness_and_the_channel():
    """`entry` exists so a per-journey read is not a scan (`varsel_state`'s cycle reads), and it has
    to be the same ANSWER `recipients` would have given for that id -- the two filters included.

    A bare dict get would pass every test built on fresh same-channel entries and diverge exactly
    where it matters: the delivery loop's liveness re-check would stop seeing expiry while
    `all_journeys` still filtered it out, so one pass would word a train for a registration the next
    read of the same mirror says is gone.
    """
    client = _FakeClient()
    _install(client)
    _mirror_page(
        client,
        "mirror_seed",
        [
            _mirrored(subscription_id="browser-sub", channel=notification_store.CHANNEL_WEB_PUSH),
            _mirrored(subscription_id="client-sub"),
        ],
    )
    mirror = notification_store.SubscriptionMirror()
    mirror.seed(now=NOW)
    browser = notification_store.CHANNEL_WEB_PUSH

    assert mirror.entry("browser-sub", NOW, channel=browser).subscription_id == "browser-sub"
    assert mirror.entry("nosuch", NOW, channel=browser) is None
    assert mirror.entry("client-sub", NOW, channel=browser) is None, "the other channel answered"
    assert mirror.entry("client-sub", NOW) is not None, "its own channel must still answer"
    # Expiry is the one state change nothing writes a document for, which is why `recipients` tests
    # it on the read -- and why this one has to as well.
    later = NOW + timedelta(days=notification_store.SUBSCRIPTION_MAX_LIFETIME_DAYS + 1)
    assert mirror.recipients(later, channel=browser) == []
    assert mirror.entry("browser-sub", later, channel=browser) is None


def test_a_sync_that_raises_keeps_the_previous_mirror():
    """Scenario 7. An empty mirror on a transient error reads as "every client unsubscribed"."""
    client = _FakeClient()
    _install(client)
    _mirror_page(client, "mirror_seed", [_mirrored(subscription_id="s1")])
    mirror = notification_store.SubscriptionMirror()
    mirror.seed(now=NOW)

    boom = RuntimeError("firestore is having a moment")
    original = notification_store._stream

    def _raises(query: Any) -> list[Any]:
        raise boom

    notification_store._stream = _raises  # type: ignore[assignment]
    try:
        try:
            mirror.sync(now=NOW + timedelta(minutes=1))
        except RuntimeError as exc:
            assert exc is boom
        else:
            raise AssertionError("the sync swallowed the error; the CALLER decides, not the mirror")
    finally:
        notification_store._stream = original  # type: ignore[assignment]

    assert mirror.size() == 1, "the previous mirror is still served"
    assert [e.subscription_id for e in mirror.recipients(NOW)] == ["s1"]


def test_a_watermark_older_than_the_bound_forces_a_re_seed():
    """The carried review's P1, and it is a CORRECTNESS bound rather than a freshness one.

    A revocation reaches the mirror as a tombstone, and that tombstone has a TTL of its own. If
    sync is broken for longer than the TTL, Firestore hard-deletes the tombstone before the mirror
    ever reads it: the removal is lost for good and no later sync can repair it, because there is
    nothing left to read. So the bound has to sit under the TTL, not merely under an alert
    threshold.
    """
    assert notification_store.MIRROR_MAX_AGE_S < 24 * 60 * 60, (
        "the bound must sit under the deletion marker's ~24 h TTL or it defends nothing"
    )
    client = _FakeClient()
    _install(client)
    _mirror_page(client, "mirror_seed", [_mirrored(subscription_id="s1")])
    mirror = notification_store.SubscriptionMirror()
    mirror.seed(now=NOW)

    # Just inside the bound: an ordinary incremental sync.
    _mirror_page(client, "mirror_sync", [])
    fresh = mirror.sync(now=NOW + timedelta(seconds=notification_store.MIRROR_MAX_AGE_S - 60))
    assert fresh.reseeded is False, fresh

    # Past it: the whole collection is read again, and the mirror follows the SEED's page.
    _mirror_page(client, "mirror_seed", [_mirrored(subscription_id="s2")])
    stale = mirror.sync(now=NOW + timedelta(seconds=notification_store.MIRROR_MAX_AGE_S + 60))
    assert stale.reseeded is True, stale
    assert [e.subscription_id for e in mirror.recipients(NOW)] == ["s2"], (
        "a re-seed replaces the mirror rather than merging into it -- which is the point: the "
        "entries it can no longer account for are exactly the ones a lost tombstone would leave"
    )


def test_an_unseeded_mirror_answers_for_nobody_and_seeds_on_first_sync():
    """`ready` is read, never inferred from emptiness: an empty collection is a steady state."""
    client = _FakeClient()
    _install(client)
    mirror = notification_store.SubscriptionMirror()
    assert mirror.ready is False
    assert mirror.recipients(NOW) == []

    _mirror_page(client, "mirror_seed", [])
    result = mirror.sync(now=NOW)
    assert result.reseeded is True and mirror.ready is True, result
    assert mirror.size() == 0
    assert mirror.ready is True, "an empty collection must not read as 'still loading'"


def test_one_unreadable_document_never_fails_the_mirror():
    """R20, the same per-document rule the load path applies."""
    client = _FakeClient()
    _install(client)
    _mirror_page(
        client,
        "mirror_seed",
        [{"subscription_id": "broken"}, _mirrored(subscription_id="s1")],
    )
    mirror = notification_store.SubscriptionMirror()
    mirror.seed(now=NOW)
    assert [e.subscription_id for e in mirror.recipients(NOW)] == ["s1"]


def _subject_seed_page(client: _FakeClient, docs: list[dict[str, Any]]) -> None:
    client.pages["mirror_seed_subjects"] = [
        (f"{notification_store.SUBJECTS}/{d['alert_id']}", d) for d in docs
    ]


def test_the_subject_mirror_seeds_the_standing_set_and_skips_expired_subjects():
    """U6 step 3. The complete standing set the emit pass cannot get from a bounded query.

    `client_alerts` enumerates at most `MAX_SUBJECTS_PER_READ`, and past that the answer is
    INCOMPLETE -- which is not a slightly worse answer: the pass falls back to a store transaction
    per subject, because treating a truncated read as complete skips a withdrawal that was owed and
    a `confirmed` mint for a train ET has cancelled. At one client with 100 000 subscriptions that
    fallback IS the steady state.
    """
    client = _FakeClient()
    _install(client)
    dead = _state("a-dead", notification_store.PHASE_PREDICTED)
    dead[notification_store.TTL_FIELD] = NOW - timedelta(minutes=1)
    _subject_seed_page(
        client,
        [
            _state("a1", notification_store.PHASE_PREDICTED),
            _state("a2", notification_store.PHASE_CONFIRMED),
            dead,
        ],
    )
    mirror = notification_store.SubjectMirror()
    assert mirror.ready is False
    result = mirror.seed(now=NOW)

    assert mirror.ready is True and mirror.size() == 2, result
    assert mirror.phases_for(SID, NOW) == {
        "a1": notification_store.PHASE_PREDICTED,
        "a2": notification_store.PHASE_CONFIRMED,
    }
    # TTL runs up to ~24 h late, so an expired subject is still returned by the query -- and nothing
    # writes a document when its own TTL passes, so the read is the only place it can be refused.
    assert "a-dead" not in mirror.phases_for(SID, NOW)


def test_the_subject_mirror_is_indexed_by_subscription_not_scanned():
    """The emit pass asks per subscription; a flat map would make each of 100 000 questions a scan.

    Asserted as a property rather than by timing: a subject belonging to another subscription is
    invisible to this one's lookup, which is only true of a real index.
    """
    client = _FakeClient()
    _install(client)
    other = _state("b1", notification_store.PHASE_PREDICTED)
    other["subscription_id"] = "sub-other"
    _subject_seed_page(client, [_state("a1", notification_store.PHASE_PREDICTED), other])
    mirror = notification_store.SubjectMirror()
    mirror.seed(now=NOW)

    assert mirror.phases_for(SID, NOW) == {"a1": notification_store.PHASE_PREDICTED}
    assert mirror.phases_for("sub-other", NOW) == {"b1": notification_store.PHASE_PREDICTED}
    assert mirror.phases_for("sub-nobody", NOW) == {}


def test_a_committed_subject_reaches_the_standing_set_without_a_re_read():
    """Kept current by the poller's own commits, not by a watermark.

    Subject documents have exactly ONE writer (KTD6: one poller thread, no route invokes the pass,
    one replica under Recreate), so every change passes through this process and applying it in
    memory is both cheaper and stronger than re-reading it.
    """
    client = _FakeClient()
    _install(client)
    _subject_seed_page(client, [_state("a1", notification_store.PHASE_PREDICTED)])
    mirror = notification_store.SubjectMirror()
    mirror.seed(now=NOW)
    assert mirror.phases_for(SID, NOW) == {"a1": notification_store.PHASE_PREDICTED}

    mirror.apply(_state("a1", notification_store.PHASE_CONFIRMED))
    assert mirror.phases_for(SID, NOW) == {"a1": notification_store.PHASE_CONFIRMED}
    mirror.apply(_state("a2", notification_store.PHASE_PREDICTED))
    assert set(mirror.phases_for(SID, NOW)) == {"a1", "a2"}

    # An unreadable document is ignored rather than raised on: the commit has already happened, and
    # raising here would unwind a pass over a mirror that is only ever an optimisation.
    mirror.apply({"alert_id": "broken"})
    assert set(mirror.phases_for(SID, NOW)) == {"a1", "a2"}


def test_dropping_a_subject_and_dropping_a_whole_subscription():
    """A revoked subscription takes its standing subjects with it, or the pass keeps answering for
    a recipient that no longer exists."""
    client = _FakeClient()
    _install(client)
    _subject_seed_page(
        client,
        [_state("a1", notification_store.PHASE_PREDICTED), _state("a2", notification_store.PHASE_PREDICTED)],
    )
    mirror = notification_store.SubjectMirror()
    mirror.seed(now=NOW)

    mirror.drop(SID, "a1")
    assert set(mirror.phases_for(SID, NOW)) == {"a2"}
    mirror.drop(SID, "a-never-existed")
    assert set(mirror.phases_for(SID, NOW)) == {"a2"}

    mirror.drop_subscription(SID)
    assert mirror.phases_for(SID, NOW) == {} and mirror.size() == 0


def test_the_open_withdrawal_runs_are_served_whole_environment():
    """`ClientAlerts.open_below_threshold`'s shape: the run is a property of the SUBJECT, and the
    caller looks up the one alert id it is about."""
    client = _FakeClient()
    _install(client)
    counting = _state("a1", notification_store.PHASE_PREDICTED)
    counting["below_threshold_at"] = NOW - timedelta(minutes=5)
    _subject_seed_page(client, [counting, _state("a2", notification_store.PHASE_PREDICTED)])
    mirror = notification_store.SubjectMirror()
    mirror.seed(now=NOW)

    runs = mirror.open_runs(NOW)
    assert set(runs) == {"a1"}, runs
    assert runs["a1"] == NOW - timedelta(minutes=5)
    # A mint closes the run -- both fields, which is what the emit body writes.
    mirror.apply(_state("a1", notification_store.PHASE_CONFIRMED, below_threshold_at=None))
    assert mirror.open_runs(NOW) == {}


def test_every_body_that_writes_a_subscription_is_declared_mirroring_or_declared_exempt():
    """U13's commit-apply, pinned exactly as U9's is: as a property of the SET.

    The rule is "every body whose write the CYCLE reads back must reach the mirror from its own
    commit", and both mistakes are silent. Left out, a `/varsel` registration created or deleted
    mid-cycle is invisible until the next sync -- and the delivery loop's two liveness re-checks
    exist for precisely that delete, so the push goes out to a traveller who erased the journey.
    Left in, a delivery body would put the webhook channel's busiest documents through an apply
    that has no reader, which is the economy `MIRROR_STAMPING_BODIES` protects.

    Read off the source, because the case that matters is a body added next year that no
    behavioural check over today's seven could see.
    """
    source = pathlib.Path(notification_store.__file__).read_text()
    bodies = {}
    for chunk in source.split("\ndef ")[1:]:
        name = "_" + chunk.split("(", 1)[0].lstrip("_")
        bodies[name] = chunk
    writers = {
        name
        for name, body in bodies.items()
        if name.endswith("_body")
        and (
            "transaction.set(refs.subscription(" in body
            or "transaction.update(refs.subscription(" in body
            or "transaction.update(\n            refs.subscription(" in body
        )
    }
    assert "_create_body" in writers and "_mark_notified_body" in writers, (
        "the scan stopped seeing the main writers; the spelling changed"
    )
    declared = set(notification_store.SUBSCRIPTION_MIRRORING_BODIES) | set(
        notification_store.SUBSCRIPTION_WRITERS_OUTSIDE_THE_MIRROR
    )
    # `_erase_device_body` is the declared exception and drops by ID rather than applying a
    # document, so it is in neither set; it is asserted separately below.
    writers.discard("_erase_device_body")
    assert writers == declared, (
        f"bodies writing a subscription document are {sorted(writers)}, the declared sets cover "
        f"{sorted(declared)}. A new one must either reach `_mirror_subscription` and be declared "
        "in SUBSCRIPTION_MIRRORING_BODIES, or be declared exempt with the reason."
    )
    assert notification_store.SUBSCRIPTION_MIRRORING_BODIES.isdisjoint(
        notification_store.SUBSCRIPTION_WRITERS_OUTSIDE_THE_MIRROR
    ), "a body cannot be both"
    # The erasure's own path, named so the set reads as complete: it must drop by id, because it
    # returns counts and ids rather than documents.
    assert "_subscription_mirror.drop_committed(" in bodies["_erase_device_body"] or (
        "drop_committed(ids)" in source
    ), "the erasure no longer drops its journeys from the mirror"


def test_the_declared_mirroring_bodies_are_the_ones_whose_commit_actually_applies():
    """The other direction: a body IN the set whose public caller forgot the apply.

    The set is a claim about what reaches `_mirror_subscription`, and a declaration with no call is
    the same silent failure as an undeclared body -- worse, because it reads as covered. Checked by
    walking each declared body back to the public function that runs it.
    """
    source = pathlib.Path(notification_store.__file__).read_text()
    callers = {
        "_create_body": "create_subscription",
        "_replace_body": "replace_subscription",
        "_delete_body": "delete_subscription",
        "_touch_body": "touch_subscription",
        "_servability_body": "set_servability",
        "_canonicalize_body": "canonicalize_stops",
        "_mark_notified_body": "mark_notified",
        "_record_push_body": "record_push",
    }
    assert set(callers) == set(notification_store.SUBSCRIPTION_MIRRORING_BODIES), (
        "a declared body has no caller listed here; add it with its public function"
    )
    for body, public in callers.items():
        chunk = source.split(f"\ndef {public}(", 1)[1].split("\ndef ", 1)[0]
        assert body in chunk, f"{public} no longer runs {body}"
        assert "_mirror_subscription(" in chunk, (
            f"{public} runs {body}, which is declared mirroring, and never applies the commit"
        )


def test_a_committed_document_reaches_the_mirror_without_waiting_for_a_sync():
    """The whole point of the commit-apply: a create is matchable in the cycle it happened in.

    A watermark would carry it at the start of the NEXT cycle. For a webhook client that is a
    freshness question; for a `/varsel` registration it is the difference between a traveller
    seeing their own journey alerted on and the delivery loop pushing about one they just deleted.
    """
    client = _FakeClient()
    _install(client)
    mirror = notification_store.SubscriptionMirror()
    _mirror_page(client, "mirror_seed", [])
    mirror.seed(now=NOW)
    assert mirror.size() == 0

    doc = _mirrored(subscription_id="sub-1")
    mirror.apply_committed(doc)
    assert mirror.size() == 1
    assert [e.subscription_id for e in mirror.recipients(NOW)] == ["sub-1"]

    # A tombstone is applied as a REMOVAL, exactly as the sync applies one.
    mirror.apply_committed({**doc, "status": "deleted"})
    assert mirror.size() == 0
    # And an unreadable document is skipped rather than raised: the transaction has already
    # committed, so a raise here would fail a call whose write has landed.
    mirror.apply_committed({"subscription_id": "broken"})
    assert mirror.size() == 0


def test_a_sync_does_not_overwrite_a_commit_this_process_made_while_it_was_reading():
    """The race the local apply creates, and the reason `_overtaken` exists.

    In order: the sync reads a page with `sub-1` in it; a request thread deletes `sub-1` and applies
    the tombstone; the sync then applies its stale copy. Without the guard `sub-1` is live in the
    mirror until the next sync -- and the delivery loop's re-check, which exists for exactly this
    delete, waves the push through. Driven by applying the tombstone BETWEEN the sync's read and its
    apply, which is the window itself.
    """
    client = _FakeClient()
    _install(client)
    mirror = notification_store.SubscriptionMirror()
    live = _mirrored(subscription_id="sub-1")
    _mirror_page(client, "mirror_seed", [live])
    mirror.seed(now=NOW)
    assert mirror.size() == 1

    real_stream = notification_store._stream
    interleaved: list[str] = []

    def _stream_then_delete(query: Any) -> Any:
        page = real_stream(query)
        if not interleaved:
            interleaved.append("sub-1")
            mirror.apply_committed({**live, "status": "deleted"})
        return page

    _mirror_page(client, "mirror_sync", [live])
    notification_store._stream = _stream_then_delete  # type: ignore[assignment]
    try:
        report = mirror.sync(now=NOW + timedelta(seconds=60))
    finally:
        notification_store._stream = real_stream  # type: ignore[assignment]

    assert interleaved == ["sub-1"], "the competing delete never ran; this test proved nothing"
    assert mirror.size() == 0, "a stale read resurrected a subscription this process had deleted"
    assert report.overtaken == 1, "the skip is not reported, so an operator cannot see it happen"
    assert report.watermark_age_s is not None, "the watermark still advanced past the document"


def test_an_erasure_drops_its_journeys_from_the_mirror_at_once():
    """«Slett alt» by id rather than by document: the body returns ids, not documents.

    The tombstone does stamp the mirror field, so a sync WOULD carry it -- one cycle later, and one
    cycle of matching, wording and writing decision rows about a traveller who asked to be forgotten
    is what this path exists to prevent.
    """
    client = _FakeClient()
    _install(client)
    _device_doc(client)
    rows = _journey_rows(client, ["j1", "j2"])
    mirror = notification_store.subscription_mirror()
    mirror.reset()
    _mirror_page(client, "mirror_seed", [dict(r) for r in rows])
    mirror.seed(now=NOW)
    assert mirror.size() == 2

    notification_store.erase_device(device_id="device-1", client_id=CLIENT, now=NOW)
    assert mirror.size() == 0, "the erased journeys are still matchable this cycle"
    mirror.reset()


def test_a_replacement_push_records_the_train_without_spending_the_day():
    """`charge_cap=False`: a notification that takes an earlier one's place on the lock screen under
    the same tag does not add to the pile, so it must not spend one of the registration's four.

    The train mark is written either way -- it is what the NEXT replacement is recognised by, and a
    replacement that did not refresh it would let the mark age out mid-disruption and turn the third
    notification about one train into a fourth charged push.
    """
    client = _FakeClient()
    _install(client)
    _journey_rows(client, ["j1"])
    notification_store.record_push(subscription_id="j1", train_ref="t1", now=NOW)
    notification_store.record_push(subscription_id="j1", train_ref="t1", charge_cap=False, now=NOW)
    stored = client.doc(notification_store.SUBSCRIPTIONS, "j1")
    assert stored["daily_pushes"] == {notification_store.oslo_day(NOW): 1}
    assert set(stored["pushed_trains"]) == {"t1"}


def test_a_web_push_documents_own_fields_are_parsed_back_and_mirrored():
    """U12 wrote all six and read none of them back, so `/varsel`'s registration could be stored and
    then not be read: the device it belongs to, the names the traveller typed and the three push
    ledgers were write-only. Both readers are checked, because the cycle uses the mirror's copy and
    the request path uses the parsed record, and two readers is two places to forget a field."""
    _install(_FakeClient())
    row = notification_store._new_subscription_doc(
        _web_push_spec(subscription_id="j1", from_name="Oslo S", to_name="Lillestrøm"), NOW
    )
    row["notified"] = {"S1": NOW + timedelta(days=7)}
    row["pushed_trains"] = {"t1": NOW + timedelta(days=7)}
    row["daily_pushes"] = {"2026-09-13": 2}

    parsed = notification_store._subscription_from_doc(row)
    assert parsed.device_id == "device-1"
    assert (parsed.from_name, parsed.to_name) == ("Oslo S", "Lillestrøm")
    assert parsed.consent_version == row["consent_version"]
    assert set(parsed.notified) == {"S1"}
    assert set(parsed.pushed_trains) == {"t1"}
    assert parsed.daily_pushes == {"2026-09-13": 2}

    entry = notification_store._mirror_entry(row)
    assert entry.device_id == "device-1"
    assert (entry.from_name, entry.to_name) == ("Oslo S", "Lillestrøm")
    assert set(entry.notified) == {"S1"}
    assert set(entry.pushed_trains) == {"t1"}
    assert entry.daily_pushes == {"2026-09-13": 2}
    # A webhook document carries none of them and must not start failing to parse.
    webhook = notification_store._mirror_entry(_live_doc())
    assert webhook.device_id == "" and webhook.notified == {}


def test_several_devices_come_back_in_one_round_trip():
    """The delivery pass needs an endpoint and a brand per registration it is about to push to, and
    on a disrupted corridor that is thousands in one cycle -- serially, on the poller's thread. One
    multi-get costs one round trip; a loop of `get_device` costs one each.

    Unknown ids are absent rather than an error, which is what lets the caller treat "no device" as
    the ordinary case it is: a journey registered by a browser that never turned notifications on.
    """
    client = _FakeClient()
    _install(client)
    _device_doc(client, device_id="device-1")
    _device_doc(client, device_id="device-2")
    devices = notification_store.get_devices(["device-1", "device-2", "device-gone", "device-1"])
    assert set(devices) == {"device-1", "device-2"}
    assert devices["device-1"].endpoint
    assert client.get_all_calls == [["device-1", "device-2", "device-gone"]], (
        "the ids were not asked for in ONE call, or the duplicate was not folded"
    )
    assert notification_store.get_devices([]) == {}, "an empty ask must cost no call at all"


def test_every_body_that_writes_a_subject_is_declared_mirroring_or_declared_exempt():
    """U9 step 2, pinned the way `MIRROR_STAMPING_BODIES` is: as a property of the SET.

    The rule is "every body that changes a field the standing set CONSUMES must reach the mirror",
    and both mistakes are silent. A mirroring body left out means the next cycle reads a phase
    Firestore no longer holds — a withdrawal never counted, or a `confirmed` mint skipped for a
    train ET has cancelled. `_sent_body` left IN would put the busiest documents in the hottest
    loop for no reader, which is exactly the economy that keeps it out of `MIRROR_STAMPING_BODIES`.

    Read off the source, because the case that matters is a body added next year and no
    behavioural check over today's four could see it. The test is what answers "does the mirror
    need a periodic re-seed?" with no: the failure mode is not a timing race, it is a forgotten
    writer, and this is what refuses one.
    """
    source = pathlib.Path(notification_store.__file__).read_text()
    bodies = {}
    for chunk in source.split("\ndef ")[1:]:
        name = "_" + chunk.split("(", 1)[0].lstrip("_")
        bodies[name] = chunk
    # `_mint` is not named `*_body` and is the most important writer of the four, which is why this
    # scans every function rather than only the bodies.
    writers = {
        name
        for name, body in bodies.items()
        if "refs.subject(" in body
        and ("transaction.set(refs.subject(" in body or "transaction.update(refs.subject(" in body)
    }
    assert "_mint" in writers, "the scan stopped seeing the main writer; the spelling changed"
    declared = set(notification_store.SUBJECT_MIRRORING_BODIES) | set(
        notification_store.SUBJECT_WRITERS_OUTSIDE_THE_MIRROR
    )
    assert writers == declared, (
        f"bodies writing a subject document are {sorted(writers)}, the declared sets cover "
        f"{sorted(declared)}. A new one must either reach `_mirror_subject` and be declared in "
        "SUBJECT_MIRRORING_BODIES, or be declared exempt with the reason."
    )
    assert notification_store.SUBJECT_MIRRORING_BODIES.isdisjoint(
        notification_store.SUBJECT_WRITERS_OUTSIDE_THE_MIRROR
    ), "a body cannot be both"
    # And the exemption is not a loophole: the one exempt body must not touch the phase, which is
    # what makes skipping the mirror safe rather than merely cheap.
    for exempt in notification_store.SUBJECT_WRITERS_OUTSIDE_THE_MIRROR:
        patch = bodies[exempt].split("transaction.update(refs.subject(", 1)[1]
        assert '"phase"' not in patch.split("return", 1)[0], (
            f"{exempt} writes the phase and is declared exempt from the mirror; the standing set "
            "would then answer with a phase this process itself has moved"
        )


def test_every_commit_that_moves_a_subject_reaches_the_standing_set():
    """U9 step 2, and it is a CORRECTNESS step rather than an economy one.

    U6 built this mirror and nothing outside its own tests ever wrote to it. Read from an
    unmaintained standing set the emit pass is worse than useless: a subject minted in cycle N is
    absent in cycle N+1, so `notification_events.skip_reason` answers `nothing_standing` for the
    below-threshold observation that follows and the withdrawal is never counted at all -- a
    traveller keeps a warning we have stopped believing, silently, forever.

    Drives the three PUBLIC functions rather than `apply`, because what has to hold is that no
    committing path can forget: `_mirror_subject` is the single statement and this is what pins it.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    mirror = notification_store.subject_mirror()

    created = notification_store.commit_event(_emit(), now=NOW)
    assert created.status == "created"
    assert mirror.phases_for(SID, NOW) == {created.alert_id: notification_store.PHASE_PREDICTED}
    # A mint closes any open run, and the mirror has to have heard that too.
    assert mirror.open_runs(NOW) == {}

    later = NOW + timedelta(minutes=5)
    counting = notification_store.record_below_threshold(_emit(), now=later)
    assert counting.status == "counting" and counting.below_threshold_cycles == 1
    assert mirror.open_runs(later) == {created.alert_id: later}
    # The phase does NOT move on a counting cycle: a below-threshold observation is not a
    # withdrawal until the run is long enough, and a mirror that moved the phase here would make
    # the next alert-worthy cycle mint a revision nobody is owed.
    assert mirror.phases_for(SID, later) == {created.alert_id: notification_store.PHASE_PREDICTED}

    assert notification_store.clear_below_threshold(created.alert_id, now=later) is True
    assert mirror.open_runs(later) == {}
    assert mirror.phases_for(SID, later) == {created.alert_id: notification_store.PHASE_PREDICTED}


def test_a_delivery_writes_the_subject_and_deliberately_does_not_mirror_it():
    """The omission `_mirror_subject` names, pinned so it stays a decision rather than an oversight.

    `_sent_body` writes `delivered_revision`, `delivered_at` and the pending pointer onto the
    subject. None of the three is in the standing set, so mirroring them would put the busiest
    documents in the hottest loop for no reader -- the same economy that keeps that body out of
    `MIRROR_STAMPING_BODIES`. The phase is untouched by construction, which is what makes the
    omission safe and is asserted here rather than assumed.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    mirror = notification_store.subject_mirror()
    created = notification_store.commit_event(_emit(), now=NOW)
    before = dict(mirror.phases_for(SID, NOW))

    claim = notification_store.claim_delivery(created.delivery_id, now=NOW)
    assert claim.status == "ready", claim
    notification_store.mark_delivery_sent(created.delivery_id, token=claim.token, now=NOW)

    stored = client.doc(notification_store.SUBJECTS, created.alert_id) or {}
    assert stored.get("delivered_revision") == created.revision, "the store did write the subject"
    assert mirror.phases_for(SID, NOW) == before
    assert stored.get("phase") == notification_store.PHASE_PREDICTED


def test_a_tombstone_and_its_cleanup_both_take_the_standing_subjects_with_them():
    """Nothing may stand for a subscription that can no longer be routed to (R13).

    Two calls and not one: the tombstone is when the subscription stops being routable, cleanup is
    the retryable path R19 requires -- and cleanup can run long after a restart that re-seeded the
    mirror from the very documents it is deleting.
    """
    client = _FakeClient()
    client.put(notification_store.SUBSCRIPTIONS, SID, _live_doc())
    _install(client)
    mirror = notification_store.subject_mirror()
    created = notification_store.commit_event(_emit(), now=NOW)
    assert mirror.phases_for(SID, NOW) != {}

    notification_store.delete_subscription(SID, client_id=CLIENT, now=NOW)
    assert mirror.phases_for(SID, NOW) == {}

    mirror.apply(_state(created.alert_id, notification_store.PHASE_PREDICTED))
    assert mirror.phases_for(SID, NOW) != {}
    notification_store.cleanup_subscription(SID, now=NOW)
    assert mirror.phases_for(SID, NOW) == {}


def test_the_standing_set_is_pruned_of_expired_subjects_rather_than_growing_for_a_week():
    """Memory, not correctness -- both readers already test expiry.

    A subject document lives `HISTORY_DAYS` (7), so an unpruned mirror holds every train every
    subscription was ever warned about for a week. At 100 000 subscriptions that is the one number
    this structure grows in, and the pod's memory limit equals its request.
    """
    client = _FakeClient()
    _install(client)
    dying = _state("a-old", notification_store.PHASE_PREDICTED)
    dying[notification_store.TTL_FIELD] = NOW + timedelta(minutes=1)
    _subject_seed_page(client, [_state("a1", notification_store.PHASE_PREDICTED), dying])
    mirror = notification_store.SubjectMirror()
    mirror.seed(now=NOW)
    assert mirror.size() == 2

    assert mirror.prune(NOW) == 0, "nothing has expired yet"
    assert mirror.prune(NOW + timedelta(minutes=2)) == 1
    assert mirror.size() == 1 and set(mirror.phases_for(SID, NOW)) == {"a1"}
    # An empty subscription leaves no bucket behind, or the index grows without holding anything.
    assert mirror.prune(NOW + timedelta(days=notification_store.HISTORY_DAYS + 1)) == 1
    assert mirror.size() == 0


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
