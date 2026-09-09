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

import inspect
import logging
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import notification_api
import notification_servability
import notification_store
import varsel

NOW = datetime(2026, 9, 9, 6, 30, tzinfo=UTC)
MODULE_SOURCE = pathlib.Path(notification_servability.__file__).read_text()


# ---- A recording store ------------------------------------------------------------------------


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

    def active_subscriptions(self, **_: Any) -> notification_store.LoadedSubscriptions:
        self.loads += 1
        if self.load_error is not None:
            raise self.load_error
        return notification_store.LoadedSubscriptions(
            subscriptions=tuple(self.subscriptions),
            truncated=self.truncated,
            skipped=0,
            expired=0,
        )

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
) -> list[dict]:
    """Run one pass with `store` installed and a seam that answers `verdicts`.

    `verdicts` is either one answer for every question or a callable taking (from_stop, to_stop).
    Returns the structured `notification_servability` log fields the pass emitted.
    """
    asked: list[tuple[str, str]] = []

    def seam(from_stop: str, to_stop: str):
        asked.append((from_stop, to_stop))
        return verdicts(from_stop, to_stop) if callable(verdicts) else verdicts

    mark = len(_SINK.records)
    before = (
        notification_store.active_subscriptions,
        notification_store.set_servability,
        notification_servability.monotonic,
    )
    notification_store.active_subscriptions = store.active_subscriptions  # type: ignore[assignment]
    notification_store.set_servability = store.set_servability  # type: ignore[assignment]
    if monotonic is not None:
        notification_servability.monotonic = monotonic
    notification_servability.configure(connectivity=seam)
    try:
        notification_servability.recheck(now=now)
    finally:
        (
            notification_store.active_subscriptions,
            notification_store.set_servability,
            notification_servability.monotonic,
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
    before = (notification_store.active_subscriptions, notification_store.set_servability)
    notification_store.active_subscriptions = store.active_subscriptions  # type: ignore[assignment]
    notification_store.set_servability = store.set_servability  # type: ignore[assignment]
    notification_store.configure(clock=lambda: NOW)
    notification_servability.configure(connectivity=lambda _f, _t: (True, False))
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
        notification_store.active_subscriptions, notification_store.set_servability = before  # type: ignore[assignment]
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
    assert "if verdict is None:" in body and "continue" in body


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
    assert summary and summary[0]["cleared"] == 1 and summary[0]["checked"] == 1


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
    assert [w[0] for w in store.writes] == ["s1", "s2"], "the second record is still processed"
    summary = [f for f in fields if f["result"] == "swept"]
    assert summary and summary[0]["cleared"] == 1, "a vanished record is not a cleared one"
    # An ORDINARY outcome, named as itself and not as a failed write: `set_servability` reads before
    # it writes, so a tombstone refusing the stamp is the store working. Logged below WARNING for
    # the reason every standing warning in this repo is argued down -- one on a routine deletion is
    # how a real one stops being read.
    assert "vanished" in store.levels and "write_failed" not in store.levels, store.levels
    assert store.levels["vanished"] < logging.WARNING


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
    assert [w[0] for w in store.writes] == ["s1", "s2"], "the second record is still processed"
    # The version travels with the question, so the store can refuse a writer that is behind.
    assert store.versions == [("s1", 1), ("s2", 1)], store.versions
    summary = [f for f in fields if f["result"] == "swept"]
    assert summary and summary[0]["cleared"] == 1, "a superseded record is not a cleared one"
    assert "superseded" in store.levels and "write_failed" not in store.levels, store.levels
    assert store.levels["superseded"] < logging.WARNING


def test_one_failing_write_does_not_end_the_sweep():
    store = _Store(
        [_sub("s1", checked_at=None), _sub("s2", checked_at=None)],
        write_errors={"s1": RuntimeError("firestore is unhappy")},
    )
    fields = _run(store, verdicts=(True, True))
    assert [w[0] for w in store.writes] == ["s1", "s2"]
    assert any(f["result"] == "write_failed" for f in fields)


def test_an_unreadable_store_leaves_every_caveat_standing():
    store = _Store([_sub("s1")], load_error=RuntimeError("firestore is unreachable"))
    fields = _run(store)
    assert store.asked == [] and store.writes == []
    assert [f["result"] for f in fields] == ["load_failed"]


def test_no_seam_means_no_store_read():
    """An unwired deployment must not spend a store read to discover it has no seam."""
    store = _Store([_sub("s1")])
    before = notification_store.active_subscriptions
    notification_store.active_subscriptions = store.active_subscriptions  # type: ignore[assignment]
    notification_servability.reset_for_tests()
    try:
        notification_servability.recheck(now=NOW)
    finally:
        notification_store.active_subscriptions = before  # type: ignore[assignment]
    assert store.loads == 0


# ---- The bounds ---------------------------------------------------------------------------------


def test_the_ceiling_binds_and_says_so():
    """A ceiling nobody can see binding is not a ceiling anybody can size."""
    n = notification_servability._MAX_CHECKS_PER_CYCLE + 6
    store = _Store([_sub(f"s{i}", checked_at=None) for i in range(n)])
    fields = _run(store, verdicts=(False, False))
    assert len(store.asked) == notification_servability._MAX_CHECKS_PER_CYCLE
    capped = [f for f in fields if f["result"] == "capped"]
    assert capped and capped[0]["due"] == n
    assert capped[0]["checked"] == notification_servability._MAX_CHECKS_PER_CYCLE


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


def test_the_deadline_reserves_the_worst_case_rather_than_measuring_elapsed_time():
    """A ceiling on when the step ENDS, not on when its last question starts.

    The clock is driven rather than slept on. Each question costs half the seam's worst case, so a
    measured-elapsed check would keep starting questions until the deadline had passed and the pass
    would end one whole worst case beyond it.
    """
    clock = {"t": 0.0}
    step = notification_servability._WORST_CASE_CHECK_S / 2

    def fake_monotonic() -> float:
        return clock["t"]

    def slow(*_a: Any):
        clock["t"] += step
        return (False, False)

    store = _Store([_sub(f"s{i}", checked_at=None) for i in range(20)])
    fields = _run(store, verdicts=slow, monotonic=fake_monotonic)
    assert clock["t"] <= notification_servability._DEADLINE_S, (
        "the pass must END inside the deadline, not merely stop starting questions at it"
    )
    bound = [f for f in fields if f["result"] in {"capped", "deadline"}]
    assert bound, fields
    # With a worst case of 20 s reserved against a 30 s deadline, questions may start only while
    # under 10 s have elapsed, so the deadline is what binds here rather than the count ceiling.
    assert bound[0]["result"] == "deadline", bound


def test_the_clock_starts_before_the_store_read():
    """The deadline bounds the PHASE, and the summary's `ms` reports the phase.

    Started after the load instead, both would describe the loop while `poller_phase` timed
    something longer -- and a slow store read would spend none of the budget it is actually
    spending. Pinned by source order because the alternative is timing a real Firestore call.
    """
    body = inspect.getsource(notification_servability.recheck)
    assert body.index("started = monotonic()") < body.index("_load(")


def test_the_deadline_does_not_claim_to_reserve_the_write():
    """The ceiling is `_DEADLINE_S` plus one `set_servability`, and the module says so.

    `notification_store._run_transaction` retries a contended body five times at
    `notification_store._CALL_DEADLINE_S` per read, and the commit carries no deadline at all -- so the
    write tail cannot be reserved into a true ceiling from here, only bounded by an outer wait this
    step does not run. What the comment must not do is state a ceiling the code does not hold.
    """
    reservation = inspect.getsource(notification_servability._sweep)
    assert "monotonic() + _WORST_CASE_CHECK_S > deadline" in reservation
    assert "_WORST_CASE_WRITE" not in MODULE_SOURCE, (
        "if a write allowance is ever reserved, the docstring's residual has to go with it"
    )


def test_the_bounds_are_sized_against_each_other_and_against_the_cadence():
    assert notification_servability._DEADLINE_S > notification_servability._WORST_CASE_CHECK_S, (
        "a deadline at the worst case admits exactly one question, making the count a decoration"
    )
    # Four questions a day hold the six-hour interval for one subscription, so the per-cycle
    # ceiling times a day of ~296 s cycles is the population this pass can carry at full cadence.
    per_day = notification_servability._MAX_CHECKS_PER_CYCLE * (24 * 3600 / 296)
    carried = per_day / (timedelta(days=1) / notification_servability._RECHECK_INTERVAL)
    assert carried > 100, carried


# ---- Quiet on a healthy list, and R16 -----------------------------------------------------------


def test_nothing_due_costs_no_question_and_no_summary():
    store = _Store([_sub("s1", servable=True), _sub("s2", checked_at=NOW - timedelta(minutes=5))])
    fields = _run(store)
    assert store.loads == 1 and store.asked == [] and store.writes == []
    assert fields == [], "a healthy list must not write a record on every cycle"


def test_truncation_is_carried_on_the_summary_rather_than_warned_about_twice():
    store = _Store([_sub("s1", checked_at=None)], truncated=True)
    fields = _run(store, verdicts=(True, True))
    summary = [f for f in fields if f["result"] == "swept"]
    assert summary and summary[0]["truncated"] is True
    assert all("Webhook subscription load bounded" not in str(f) for f in fields)


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
            "checked",
            "cleared",
            "unservable",
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
