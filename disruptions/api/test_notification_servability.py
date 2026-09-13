"""Checks for notification_servability.py: AE13's second clause, and the four rules it borrows.

Hermetic and store-free. Every other client suite runs `notification_store`'s real transaction bodies
against a Firestore fake, because what they assert is a committed document; nothing here is about a
document. What is under test is the SELECTION (which subscriptions are due, and in which order),
the WRITE RULE (an unestablished answer is never written, an established one always moves the
stamp) and the two BOUNDS -- so the store is two recorded callables and the assertions are about
what was asked and what was written.

The suite runs as its own process (`python test_notification_servability.py`, never pytest), like every
suite in this directory, and its runner is at the END of the file: a runner placed mid-file
silently skips every test below it while still printing a passing count.
"""

from __future__ import annotations

import ast
import inspect
import logging
import pathlib
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import notification_api
import notification_servability
import notification_store
import varsel

NOW = datetime(2026, 9, 9, 6, 30, tzinfo=UTC)
MODULE_SOURCE = pathlib.Path(notification_servability.__file__).read_text()


# ---- A recording store ------------------------------------------------------------------------


class _NotReady(Exception):
    """Marks a fake store as an UNSEEDED mirror rather than a failing one.

    `_load` treats the two the same way -- no re-check, every caveat stands -- and the tests should
    be able to drive both, because they arrive for different reasons: a restart window versus a
    store outage.
    """


class _Store:
    """Records what the pass asked the store for, and what it wrote."""

    def __init__(
        self,
        subscriptions: list[notification_store.Subscription],
        *,
        truncated: bool = False,
        load_error: BaseException | None = None,
        write_errors: dict[str, BaseException] | None = None,
    ) -> None:
        self.subscriptions = subscriptions
        self.truncated = truncated
        self.load_error = load_error
        self.write_errors = write_errors or {}
        self.loads = 0
        self.writes: list[tuple[str, bool | None]] = []
        self.versions: list[tuple[str, int]] = []

    # Stands in for the subscription MIRROR since U6: `_load` falls back to it rather than to a
    # bounded query, because reading the collection here would reintroduce per cycle the one full
    # read the mirror exists to remove -- on the path least able to justify it, since a healthy
    # list has nothing due.
    @property
    def ready(self) -> bool:
        return not self.load_error or not isinstance(self.load_error, _NotReady)

    def recipients(self, now: datetime, **_: Any) -> list[notification_store.Subscription]:
        self.loads += 1
        if self.load_error is not None:
            raise self.load_error
        return list(self.subscriptions)

    def size(self) -> int:
        return len(self.subscriptions)

    def set_servability(
        self,
        subscription_id: str,
        *,
        servable: bool | None,
        expected_version: int,
        now: datetime | None = None,
    ) -> None:
        del now
        # Recorded, so "the verdict is written for the version it was asked about" is an assertion
        # about a value rather than about the absence of a crash.
        self.writes.append((subscription_id, servable))
        self.versions.append((subscription_id, expected_version))
        error = self.write_errors.get(subscription_id)
        if error is not None:
            raise error


def _sub(
    subscription_id: str,
    *,
    servable: bool | None = False,
    checked_at: datetime | None = None,
    from_stop: str = "NSR:StopPlace:337",
    to_stop: str = "NSR:StopPlace:418",
) -> notification_store.Subscription:
    return notification_store.Subscription(
        subscription_id=subscription_id,
        client_id="client-a",
        destination_id="dest-a",
        from_stop=from_stop,
        to_stop=to_stop,
        begin="07:00",
        end="09:00",
        days=(0, 1, 2, 3, 4),
        external_ref="ref-1",
        test=False,
        version=1,
        status="active",
        created_at=NOW - timedelta(days=1),
        updated_at=NOW - timedelta(days=1),
        last_seen_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=30),
        lifetime_expires_at=NOW + timedelta(days=30),
        inactivity_expires_at=NOW + timedelta(days=30),
        servable=servable,
        servable_checked_at=checked_at,
    )


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# A collecting sink rather than a `NullHandler`, for `test_webhook_delivery`'s reason: the faults
# this suite injects are logged with `exc_info=True`, and with no handler in the hierarchy
# `logging.lastResort` prints every one of them -- so a green run would end in stack traces, which
# is how a real one stops being read.
_SINK = _Records()
for _name in ("notification_servability", "notification_store"):
    _sink_logger = logging.getLogger(_name)
    _sink_logger.setLevel(logging.DEBUG)
    _sink_logger.addHandler(_SINK)


def _run(
    store: _Store,
    *,
    verdicts: Any = (True, False),
    now: datetime = NOW,
    monotonic: Any = None,
    max_writes: int | None = None,
    write_wait: float | None = None,
) -> list[dict]:
    """Run one pass with `store` installed and a seam that answers `verdicts`.

    `verdicts` is either one answer for every question or a callable taking (from_stop, to_stop).
    Returns the structured `notification_servability` log fields the pass emitted.
    """
    asked: list[tuple[str, str]] = []

    def seam(pairs):
        # ONE call per pass since U10, however many pairs are pending, and the answers are
        # POSITIONAL. `asked` still collects pairs, so every assertion below about "what was asked"
        # is unchanged in meaning: it is the distinct stop pairs, not the subscriptions.
        asked.extend(pairs)
        return [verdicts(f, t) if callable(verdicts) else verdicts for f, t in pairs]

    mark = len(_SINK.records)
    before = (
        notification_store.subscription_mirror,
        notification_store.set_servability,
        notification_servability.monotonic,
        notification_servability._MAX_WRITES_PER_CYCLE,
        notification_servability._WRITE_WAIT_S,
    )
    notification_store.subscription_mirror = lambda: store  # type: ignore[assignment]
    # Read through the store object on every call, so a test that swaps its own writer in after
    # construction is still the one the pass reaches.
    notification_store.set_servability = lambda *a, **k: store.set_servability(*a, **k)  # type: ignore[assignment]
    if monotonic is not None:
        notification_servability.monotonic = monotonic
    if max_writes is not None:
        notification_servability._MAX_WRITES_PER_CYCLE = max_writes
    if write_wait is not None:
        notification_servability._WRITE_WAIT_S = write_wait
    notification_servability.configure(verdicts=seam)
    try:
        notification_servability.recheck(now=now)
    finally:
        (
            notification_store.subscription_mirror,
            notification_store.set_servability,
            notification_servability.monotonic,
            notification_servability._MAX_WRITES_PER_CYCLE,
            notification_servability._WRITE_WAIT_S,
        ) = before
        notification_servability.reset_for_tests()
    store.asked = asked  # type: ignore[attr-defined]
    kept = [
        record
        for record in _SINK.records[mark:]
        if isinstance(getattr(record, "fields", None), dict)
        and record.fields.get("event") == "notification_servability"
    ]
    # Levels beside the fields, because two of the assertions below are about the LEVEL a record
    # went out at rather than its content: an ordinary outcome logged as a warning is how a real
    # warning stops being read.
    store.levels = {record.fields["result"]: record.levelno for record in kept}  # type: ignore[attr-defined]
    return [record.fields for record in kept]


# ---- Rule 1: an unestablished answer is never written -------------------------------------------


def test_the_re_check_reuses_the_notification_passs_read_and_reads_for_itself_when_stale():
    """One read of `subscriptions` per cycle, not two. The pass reads the collection for matching
    (`notification_store.recipient_documents`) and this re-check walked it again for a question
    due on a handful of documents every six hours. The snapshot is reused inside `_LOAD_REUSE_S`
    and not past it -- and a re-check with nothing to reuse (a pass that raised, a cycle the flag
    skipped the pass on) still reads for itself, which is what keeps the poller wiring free of a
    dependency on the pass's return value."""
    store = _Store([_sub("sub-1", servable=False, checked_at=None)])
    before = (notification_store.subscription_mirror, notification_store.set_servability)
    notification_store.subscription_mirror = lambda: store  # type: ignore[assignment]
    notification_store.set_servability = store.set_servability  # type: ignore[assignment]
    notification_store.configure(clock=lambda: NOW)
    notification_servability.configure(verdicts=lambda pairs: [(True, False)] * len(pairs))
    try:
        notification_store.recipient_documents()
        assert store.loads == 1
        notification_servability.recheck(now=NOW + timedelta(seconds=45))
        assert store.loads == 1, "the same cycle's read is reused"
        assert [w for w in store.writes] == [("sub-1", True)], store.writes
        late = NOW + timedelta(seconds=notification_servability._LOAD_REUSE_S + 1)
        notification_servability.recheck(now=late)
        assert store.loads == 2, "past the reuse window the re-check reads for itself"
        notification_store.forget_recent_load()
        notification_servability.recheck(now=NOW + timedelta(seconds=45))
        assert store.loads == 3, "nothing to reuse: read for itself"
    finally:
        notification_store.subscription_mirror, notification_store.set_servability = before  # type: ignore[assignment]
        notification_servability.reset_for_tests()
        notification_store.reset_for_tests()


def test_a_verdict_that_could_not_be_established_writes_nothing_at_all():
    """The rule with the sharpest edge here. `set_servability` ACCEPTS None and would stamp it, so
    calling it on a provider failure would convert "we do not know" into a freshly-dated
    non-answer -- and one BigQuery blip into a caveat the contract says must never be rendered."""
    store = _Store([_sub("s1", servable=False)])
    _run(store, verdicts=None)
    assert store.asked == [("NSR:StopPlace:337", "NSR:StopPlace:418")]
    assert store.writes == [], "a None verdict must leave the stored value AND its stamp alone"


def test_a_seam_that_raises_is_not_an_answer_either():
    store = _Store([_sub("s1")])

    def boom(*_a: Any) -> Any:
        raise RuntimeError("bigquery is having a day")

    fields = _run(store, verdicts=boom)
    assert store.writes == []
    assert any(f["result"] == "error" for f in fields)


def test_none_is_never_passed_to_the_store():
    """Belt to the braces above: the module must not contain a call that could stamp a non-answer,
    however the control flow is later rearranged."""
    assert "servable=None" not in MODULE_SOURCE
    body = inspect.getsource(notification_servability._sweep)
    # The batch form states it as a filter rather than a `continue`: a pair with no established
    # answer never reaches `established`, so no subscription on it is ever handed to a worker.
    assert "if answer is not None" in body
    assert "servable=servable" in inspect.getsource(notification_servability._record)


def test_the_create_path_states_the_same_rule():
    """`notification_api._record_servability` is the other half of R6 and holds rule 1 independently.
    Pinned here because the two are only one rule if both say it: a create that stamped a None
    would leave a record this pass reads as freshly answered and skips for six hours."""
    create = inspect.getsource(notification_api._record_servability)
    assert "if verdict is None:" in create and "return None" in create


# ---- Rule 2: per record, not per cycle ----------------------------------------------------------


def test_a_fresh_answer_is_not_re_asked():
    store = _Store([_sub("s1", checked_at=NOW - timedelta(hours=1))])
    _run(store)
    assert store.loads == 1, "the store is read once per cycle regardless"
    assert store.asked == [], "an answer inside the interval costs no question"
    assert store.writes == []


def test_the_interval_boundary_is_inclusive():
    """`now - stamp >= interval`, `varsel._service_check_due`'s own comparison."""
    just_inside = _sub("s1", checked_at=NOW - notification_servability._RECHECK_INTERVAL)
    just_outside = _sub("s2", checked_at=NOW - notification_servability._RECHECK_INTERVAL)
    assert notification_servability.check_due(just_inside, NOW) is True
    assert (
        notification_servability.check_due(
            just_outside, NOW - timedelta(seconds=1)
        )
        is False
    )


def test_the_interval_is_varsels_own():
    """Two channels, one meaning of "stale". Restated rather than imported (a poller-side client
    module holds no import of varsel), so the equality is pinned instead."""
    assert notification_servability._RECHECK_INTERVAL == varsel._SERVICE_RECHECK


def test_a_missing_or_unusable_stamp_is_due_now():
    assert notification_servability.check_due(_sub("s1", checked_at=None), NOW) is True
    # A naive datetime -- a document written by something that is not this binary. Subtracting it
    # from an aware `now` raises TypeError, and a record whose freshness cannot be read must not
    # read as fresh.
    naive = _sub("s2", checked_at=NOW.replace(tzinfo=None))
    assert notification_servability.check_due(naive, NOW) is True


def test_a_servable_subscription_is_never_re_asked():
    """The one asymmetry, and it is deliberate: a stale `true` renders no caveat and publishes its
    own age, while re-asking every servable pair is O(all subscriptions) BigQuery questions."""
    ancient = _sub("s1", servable=True, checked_at=NOW - timedelta(days=90))
    assert notification_servability.check_due(ancient, NOW) is False
    store = _Store([ancient])
    _run(store)
    assert store.asked == [] and store.writes == []


def test_a_never_established_answer_is_due():
    """A `null` is R6's other unresolved state: leaving it unresolved forever has the same shape as
    the never-clearing caveat this pass exists to fix."""
    assert notification_servability.check_due(_sub("s1", servable=None, checked_at=None), NOW) is True
    stale_null = _sub("s2", servable=None, checked_at=NOW - timedelta(hours=7))
    assert notification_servability.check_due(stale_null, NOW) is True


# ---- Rule 3: the stamp moves on every established answer ----------------------------------------


def test_service_that_appeared_clears_the_caveat():
    store = _Store([_sub("s1", checked_at=NOW - timedelta(hours=7))])
    fields = _run(store, verdicts=(True, True))
    assert store.writes == [("s1", True)]
    summary = [f for f in fields if f["result"] == "swept"]
    assert summary and summary[0]["cleared"] == 1 and summary[0]["asked"] == 1


def test_an_unchanged_answer_still_moves_the_stamp():
    """R6's freshness half -- and the thing that takes a still-unservable record out of the due set
    for six hours instead of leaving it at the head of the queue on every cycle."""
    store = _Store([_sub("s1", checked_at=NOW - timedelta(hours=7))])
    fields = _run(store, verdicts=(False, False))
    assert store.writes == [("s1", False)]
    summary = [f for f in fields if f["result"] == "swept"]
    assert summary and summary[0]["cleared"] == 0 and summary[0]["unservable"] == 1


def test_only_the_forward_direction_decides_the_caveat():
    """The reverse reading is what /varsel words a likely from/to swap from; the client record has
    one tri-state and nowhere to carry it, and inventing a field would publish an uncontracted hint."""
    store = _Store([_sub("s1", checked_at=NOW - timedelta(hours=7))])
    _run(store, verdicts=(False, True))
    assert store.writes == [("s1", False)]


# ---- Rule 4: per-record isolation ---------------------------------------------------------------


def test_a_subscription_deleted_while_we_were_asking_is_skipped_not_resurrected():
    # Stamps chosen so the documented order (oldest first, a missing stamp being oldest) puts the
    # vanished record first: what is under test is that the pass carries on past it.
    store = _Store(
        [_sub("s1", checked_at=None), _sub("s2", checked_at=NOW - timedelta(hours=7))],
        write_errors={"s1": notification_store.NotFound("no such subscription")},
    )
    fields = _run(store, verdicts=(True, True))
    assert sorted(w[0] for w in store.writes) == ["s1", "s2"], "the second record is still processed"
    summary = [f for f in fields if f["result"] == "swept"]
    assert summary and summary[0]["cleared"] == 1, "a vanished record is not a cleared one"
    # An ORDINARY outcome, named as itself and not as a failed write: `set_servability` reads before
    # it writes, so a tombstone refusing the stamp is the store working. COUNTED rather than logged
    # since U10 -- two thousand records a cycle is two thousand lines, and the pass writes no
    # warning at all for this one, which is how a real warning stays readable.
    assert summary[0]["vanished"] == 1 and summary[0]["write_failed"] == 0, summary
    assert "write_failed" not in store.levels, store.levels


def test_a_route_replaced_mid_check_is_superseded_rather_than_stamped():
    """The verdict is about a ROUTE, and the question outlives the definition it was asked about.

    A check is a ~20 s BigQuery question and a replace is one client request, so they interleave.
    Without the version guard this pass would stamp the CURRENT record with a verdict about the
    route it has just stopped watching, and date it now -- `servable` reading as an answer beside a
    fresh `servable_checked_at` is the exact pair R6 publishes so a caveat's age can be judged.

    Named as its own outcome, below WARNING, for `vanished`'s reason: the replace retired the answer
    and its stamp deliberately, and the record comes back round on the next cadence with nothing
    established -- so there is nothing to repair and nothing to warn about.
    """
    store = _Store(
        [_sub("s1", checked_at=None), _sub("s2", checked_at=NOW - timedelta(hours=7))],
        write_errors={"s1": notification_store.Conflict("version is 2, not 1")},
    )
    fields = _run(store, verdicts=(True, True))
    assert sorted(w[0] for w in store.writes) == ["s1", "s2"], "the second record is still processed"
    # The version travels with the question, so the store can refuse a writer that is behind.
    assert sorted(store.versions) == [("s1", 1), ("s2", 1)], store.versions
    summary = [f for f in fields if f["result"] == "swept"]
    assert summary and summary[0]["cleared"] == 1, "a superseded record is not a cleared one"
    assert summary[0]["superseded"] == 1 and summary[0]["write_failed"] == 0, summary
    assert "write_failed" not in store.levels, store.levels


def test_one_failing_write_does_not_end_the_sweep():
    store = _Store(
        [_sub("s1", checked_at=None), _sub("s2", checked_at=None)],
        write_errors={"s1": RuntimeError("firestore is unhappy")},
    )
    fields = _run(store, verdicts=(True, True))
    assert sorted(w[0] for w in store.writes) == ["s1", "s2"]
    assert any(f["result"] == "write_failed" for f in fields)
    summary = [f for f in fields if f["result"] == "swept"]
    assert summary and summary[0]["write_failed"] == 1 and summary[0]["cleared"] == 1, summary


def test_an_unreadable_store_leaves_every_caveat_standing():
    store = _Store([_sub("s1")], load_error=RuntimeError("firestore is unreachable"))
    fields = _run(store)
    assert store.asked == [] and store.writes == []
    assert [f["result"] for f in fields] == ["load_failed"]


def test_no_seam_means_no_store_read():
    """An unwired deployment must not spend a store read to discover it has no seam."""
    store = _Store([_sub("s1")])
    before = notification_store.subscription_mirror
    notification_store.subscription_mirror = lambda: store  # type: ignore[assignment]
    notification_servability.reset_for_tests()
    try:
        notification_servability.recheck(now=NOW)
    finally:
        notification_store.subscription_mirror = before  # type: ignore[assignment]
    assert store.loads == 0


# ---- The bounds ---------------------------------------------------------------------------------


def test_the_ceiling_binds_on_WRITES_and_says_so():
    """A ceiling nobody can see binding is not a ceiling anybody can size.

    It bounds the STORE since U10 -- the BigQuery question is one job however many pairs are
    pending, so bounding questions would bound nothing -- and it is patched down here because the
    real number is two thousand and the check is about the policy.
    """
    bound = 3
    n = bound + 6
    store = _Store([_sub(f"s{i}", checked_at=None, to_stop=f"NSR:StopPlace:{i}") for i in range(n)])
    fields = _run(store, verdicts=(False, False), max_writes=bound)
    assert len(store.writes) == bound
    capped = [f for f in fields if f["result"] == "capped"]
    assert capped and capped[0]["due"] == n and capped[0]["capped"] == n - bound


def test_a_pair_no_admitted_subscription_carries_is_never_asked_about():
    """The pairs are derived from the ADMITTED subscriptions, not from the whole due set.

    Asking about a pair whose records will not be written this cycle spends an answer on nothing:
    the ceiling stops before them, they stay due, and the next cycle asks again anyway.
    """
    bound = 2
    store = _Store([_sub(f"s{i}", checked_at=None, to_stop=f"NSR:StopPlace:{i}") for i in range(5)])
    _run(store, verdicts=(False, False), max_writes=bound)
    assert len(store.asked) == bound, store.asked


def test_two_subscriptions_on_one_corridor_are_one_question(monkeypatch=None):
    """A pair is asked about once and the answer fans back out to every record on it.

    This is what makes the bound above a bound on RECORDS rather than on jobs: a hundred thousand
    commuters on one corridor are one place in one query.
    """
    del monkeypatch
    store = _Store([_sub("s1", checked_at=None), _sub("s2", checked_at=None)])
    _run(store, verdicts=(True, False))
    assert store.asked == [("NSR:StopPlace:337", "NSR:StopPlace:418")], store.asked
    assert sorted(store.writes) == [("s1", True), ("s2", True)]


def test_the_ceiling_is_spent_on_the_caveat_carrying_ones_oldest_first():
    """The order when it binds. A `false` is a caveat a client is being shown right now; a `null`
    was never established and is worth asking, but not ahead of one -- and within a group the
    oldest answer goes first, with a missing stamp counting as the oldest there is."""
    subs = [
        _sub("null-recent", servable=None, checked_at=NOW - timedelta(hours=7)),
        _sub("false-recent", servable=False, checked_at=NOW - timedelta(hours=7)),
        _sub("null-never", servable=None, checked_at=None),
        _sub("false-old", servable=False, checked_at=NOW - timedelta(days=3)),
    ]
    order = [s.subscription_id for s in notification_servability.due_subscriptions(subs, NOW)]
    assert order == ["false-old", "false-recent", "null-never", "null-recent"]


def test_the_deadline_reserves_the_question_rather_than_measuring_elapsed_time():
    """A ceiling on when the step ENDS, not on when its question starts.

    The clock is driven rather than slept on: the load is made to consume all but a moment of the
    budget, so the reservation refuses to start a question that could not finish inside it. A
    measured-elapsed check would start it anyway and the phase would end one whole worst case past
    the deadline -- with the poller's cycle behind it.
    """
    clock = {"t": 0.0}
    store = _Store([_sub(f"s{i}", checked_at=None) for i in range(3)])
    original = store.recipients

    def slow_load(*args: Any, **kwargs: Any):
        clock["t"] += notification_servability._DEADLINE_S - 1.0
        return original(*args, **kwargs)

    store.recipients = slow_load  # type: ignore[assignment]
    fields = _run(store, verdicts=(False, False), monotonic=lambda: clock["t"])
    assert store.asked == [], "no question may start that cannot finish inside the deadline"
    assert store.writes == []
    assert [f["result"] for f in fields] == ["deadline"], fields
    assert clock["t"] <= notification_servability._DEADLINE_S


def test_the_clock_starts_before_the_store_read():
    """The deadline bounds the PHASE, and the summary's `ms` reports the phase.

    Started after the load instead, both would describe the loop while `poller_phase` timed
    something longer -- and a slow store read would spend none of the budget it is actually
    spending. Pinned by source order because the alternative is timing a real Firestore call.
    """
    body = inspect.getsource(notification_servability.recheck)
    assert body.index("started = monotonic()") < body.index("_load(")


def test_the_write_pass_is_collected_under_a_bound_and_stragglers_are_counted():
    """A transactional COMMIT carries no client-side deadline, so an unbounded wait here would park
    the poller's thread on a hung store -- which is the one failure this phase must not cause.

    The abandoned write is not lost work and not a wrong answer: its record simply stays due, which
    is the state it was already in, and the next cycle picks it up.
    """
    release = threading.Event()
    store = _Store([_sub("slow", checked_at=None), _sub("quick", checked_at=None, to_stop="NSR:StopPlace:9")])
    original = store.set_servability

    def blocking(subscription_id: str, **kwargs: Any):
        if subscription_id == "slow":
            release.wait(5)
        return original(subscription_id, **kwargs)

    store.set_servability = blocking  # type: ignore[assignment]
    try:
        fields = _run(store, verdicts=(True, True), write_wait=0.05)
    finally:
        release.set()
    summary = [f for f in fields if f["result"] == "abandoned"]
    assert summary and summary[0]["abandoned"] == 1, fields
    assert summary[0]["cleared"] == 1, "the record that did finish is still counted"


def test_the_writes_of_one_pass_overlap_rather_than_queueing():
    """U9's arithmetic one module over: one transaction at a time is ~4 writes a second however
    many are waiting, so a bound of two thousand with a serial writer is decoration.

    A barrier is the falsification: it can only be crossed if the writes are genuinely concurrent,
    and a serial pass deadlocks on it rather than merely running slowly.
    """
    width = 8
    barrier = threading.Barrier(width, timeout=5)
    store = _Store(
        [_sub(f"s{i}", checked_at=None, to_stop=f"NSR:StopPlace:{i}") for i in range(width)]
    )
    original = store.set_servability

    def synchronised(subscription_id: str, **kwargs: Any):
        barrier.wait()
        return original(subscription_id, **kwargs)

    store.set_servability = synchronised  # type: ignore[assignment]
    fields = _run(store, verdicts=(True, True))
    summary = [f for f in fields if f["result"] == "swept"]
    assert summary and summary[0]["cleared"] == width, fields


def test_the_write_bound_is_expressed_as_a_transaction_and_not_as_a_firestore_batch():
    """The answer is about a ROUTE, and only the version compare says so.

    A Firestore `WriteBatch` has exactly two preconditions, `exists` and `last_update_time`, and
    neither can express "and the route has not changed". `last_update_time` read off the mirror is
    as old as the last sync, and `_sent_body` deliberately does not stamp -- so the subscriptions a
    delivery has written since would fail the precondition every cycle, forever. The version travels
    with the question instead, which is what `set_servability` already enforces inside a
    transaction.
    """
    # Over the PARSED module, not over its text: the prose above explains why a batch is refused,
    # so a grep would match the argument for the rule as though it were the rule being broken --
    # and a guard that cannot tell those apart is one nobody can write a comment near.
    forbidden = {"WriteBatch", "BulkWriter", "batch", "bulk_writer", "last_update_time"}
    used = set()
    for node in ast.walk(ast.parse(MODULE_SOURCE)):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            used.add(node.arg)
    assert not (used & forbidden), used & forbidden
    assert "expected_version=subscription.version" in inspect.getsource(
        notification_servability._record
    )


def test_the_bounds_are_sized_against_each_other_and_against_the_cadence():
    assert notification_servability._DEADLINE_S > (
        notification_servability._WORST_CASE_CHECK_S + notification_servability._WRITE_WAIT_S
    ), "the phase ceiling must cover the question AND the write pass it no longer leaves outside it"
    # Four checks a day hold the six-hour interval for one record, so the per-cycle write ceiling
    # times a day of ~296 s cycles is the population this pass can carry at full cadence.
    per_day = notification_servability._MAX_WRITES_PER_CYCLE * (24 * 3600 / 296)
    carried = per_day / (timedelta(days=1) / notification_servability._RECHECK_INTERVAL)
    assert carried > 100_000, carried
    # Serial, the write bound would be unreachable: the pool is what makes it a bound rather than
    # an aspiration.
    serial_seconds = notification_servability._MAX_WRITES_PER_CYCLE * 0.25
    assert serial_seconds > notification_servability._WRITE_WAIT_S
    assert (
        serial_seconds / notification_servability._WRITE_WORKERS
        <= notification_servability._WRITE_WAIT_S
    )


# ---- Quiet on a healthy list, and R16 -----------------------------------------------------------


def test_nothing_due_costs_no_question_and_no_summary():
    store = _Store([_sub("s1", servable=True), _sub("s2", checked_at=NOW - timedelta(minutes=5))])
    fields = _run(store)
    assert store.loads == 1 and store.asked == [] and store.writes == []
    assert fields == [], "a healthy list must not write a record on every cycle"


def test_the_mirror_backed_read_is_never_truncated_and_the_summary_says_so():
    """U6. `truncated` on this record used to mean R21's per-cycle read ceiling had bound.

    There is no per-cycle read left to bound -- the mirror holds the whole collection -- so the
    field is now always False here, and that is worth PINNING rather than leaving as a flag nobody
    can make fire. A truncation claim that can never be true is indistinguishable from one that is
    never checked, which is the failure mode this repo has been bitten by before.

    The field itself stays on the record: `LoadedSubscriptions` is shared with readers that can
    still truncate, and a reader supplying a truncated load must still be able to say so.
    """
    store = _Store([_sub("s1", checked_at=None)], truncated=True)
    fields = _run(store, verdicts=(True, True))
    summary = [f for f in fields if f["result"] == "swept"]
    assert summary, fields
    assert summary[0]["truncated"] is False, (
        "the mirror served a truncated load; it holds the whole collection by construction"
    )
    assert all("Webhook subscription load bounded" not in str(f) for f in fields)


def test_an_unseeded_mirror_skips_the_re_check_and_leaves_every_caveat_standing():
    """U6. A restart window and a store outage reach here for different reasons and the same way.

    Re-checking against a half-loaded mirror would resolve a caveat from a population that is not
    the population -- and R6's whole rule is that a stale answer must never read as an all-clear.
    """
    store = _Store([_sub("s1", checked_at=None)], load_error=_NotReady("still loading"))
    fields = _run(store, verdicts=(True, True))
    assert store.writes == [], "nothing may be written from an incomplete population"
    assert [f["result"] for f in fields] == ["mirror_not_ready"], fields


def test_no_log_record_carries_a_route_a_window_or_prose():
    """R16. The ids are the two `notification_store` already logs (`notification_subscription`); a stop ref,
    a boarding window or the client's own opaque reference are not published anywhere here."""
    store = _Store(
        [_sub("s1", checked_at=None), _sub("s2", checked_at=None)],
        write_errors={"s2": RuntimeError("nope")},
    )
    fields = _run(store, verdicts=(True, True))
    assert fields
    forbidden = {"NSR:StopPlace:337", "NSR:StopPlace:418", "07:00", "09:00", "ref-1"}
    for record in fields:
        for value in record.values():
            assert str(value) not in forbidden, record
        assert set(record) <= {
            "event",
            "result",
            "client_id",
            "subscription_id",
            "due",
            "asked",
            "answers",
            "pairs",
            "established",
            "cleared",
            "unservable",
            "vanished",
            "superseded",
            "write_failed",
            "abandoned",
            "capped",
            "truncated",
            "ms",
        }, record


def test_every_record_rides_the_event_name_the_create_path_already_uses():
    """One event name for both halves of R6, so a query written for either finds both -- the
    meldinger-rename lesson. A new name would silently drop these out of every existing query."""
    assert '"event": "notification_servability"' in MODULE_SOURCE
    assert MODULE_SOURCE.count('"event": "') == MODULE_SOURCE.count('"event": "notification_servability"')
    assert '"event": "notification_servability"' in pathlib.Path(notification_api.__file__).read_text()


if __name__ == "__main__":
    _ran = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            _ran += 1
            print(f"ok  {_name}")
    # The COUNT, in every other suite's shape: a runner that silently collects nothing still exits
    # 0, so "it passed" is not the check -- "the number moved" is.
    print(f"\n{_ran} client servability checks passed.")
