"""Checks for the poller lease: the state machine, the role loop, the fences and the shutdown (U2).

Hermetic: the BigQuery client is stubbed before main is imported, the app's lifespan is never
entered, and every store call is a fake with an injected clock -- nothing here touches Firestore,
BigQuery or the network, and no test sleeps for more than a fraction of a second.

What is pinned, in the plan's order (docs/plans/2026-10-13-001, U2):

* the state machine (`poller_lease`): the local deadline is anchored on the heartbeat the last
  successful store write carried and never on the live one, the renewal timer's cadence, freshness
  gate and single flight, the bounded wait on a hung renewal, `on_acquire`/`on_lose` and the
  five-tick back-off, and the arithmetic between the TTL, the margin and the longest wait;
* the role loop (`main.poll_for_changes`): a leader runs the cycle and the tick writers, a
  follower only follows the last build, and a forced rebuild after acquisition;
* the fences (`main._phase`, the two-query phases, the tick writers): a lost lease or a stop ends
  the cycle at the next boundary, with no record for the phase it stopped before;
* the shutdown (`poller_handover.shutdown_poller`, bound as `main._shutdown_poller`): release only
  after the thread has exited and the emit pass has drained, never while the thread is still alive,
  and a 5 s join under `SoleLeader`.

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery \
    --with google-cloud-storage --with pywebpush --with google-cloud-firestore \
    --with prometheus-client python test_poller_lease.py
"""

import ast
import logging
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import main
import metrics
import notification_events
import notification_store
import poller_lease
import varsel
from google.api_core.exceptions import NotFound

PL = notification_store.PollerLease


# ---- fakes ---------------------------------------------------------------------------------------


class _Clock:
    """One monotonic clock and a wall clock a fixed offset from it, plus the loop's heartbeat."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now
        self.beat: float | None = now

    def monotonic(self) -> float:
        return self.now

    def wall(self) -> float:
        return 1_760_000_000.0 + self.now


class _Store:
    """The three lease calls over one in-memory document, recording every call."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.holder: str | None = None
        self.token: str | None = None
        self.acquire_raises: Exception | None = None
        self.renew_status = "renewed"
        self.renew_gate: threading.Event | None = None
        self._n = 0

    def acquire(self, holder, *, heartbeat):
        self.calls.append(("acquire", holder, heartbeat))
        if self.acquire_raises is not None:
            raise self.acquire_raises
        if self.holder not in (None, holder):
            return PL("refused", self.holder, None)
        self._n += 1
        self.holder, self.token = holder, f"t{self._n}"
        return PL("acquired", holder, heartbeat, self.token)

    def renew(self, holder, token, *, heartbeat):
        self.calls.append(("renew", holder, token, heartbeat))
        if self.renew_gate is not None:
            self.renew_gate.wait(2.0)
        return PL(self.renew_status, self.holder, heartbeat, token)

    def release(self, holder):
        self.calls.append(("release", holder))
        if self.holder != holder:
            return PL("not_held", self.holder, None)
        self.holder = None
        return PL("released", None, None)

    def count(self, kind: str) -> int:
        return sum(1 for c in self.calls if c[0] == kind)


def _lease(clock=None, store=None, **kw):
    clock = clock or _Clock()
    store = store or _Store()
    events: list[str] = []
    lease = poller_lease.StoreLease(
        holder="pod-a-1",
        acquire=store.acquire,
        renew=store.renew,
        release=store.release,
        heartbeat=lambda: clock.beat,
        on_acquire=kw.pop("on_acquire", lambda: events.append("on_acquire")),
        on_lose=kw.pop("on_lose", lambda: events.append("on_lose")),
        monotonic=clock.monotonic,
        wall=clock.wall,
        renew_wait_s=kw.pop("renew_wait_s", 0.05),
    )
    return lease, clock, store, events


def _settle(lease, timeout=1.0):
    """Wait for an abandoned renewal thread to finish (sub-second; the fakes never really hang)."""
    end = time.monotonic() + timeout
    while lease._renewing and time.monotonic() < end:
        time.sleep(0.005)


# ---- the state machine ---------------------------------------------------------------------------


def test_poller_lease_imports_no_sibling_module():
    """KTD17: a stdlib leaf, so `varsel` never sees `main` through it and no cycle can appear."""
    here = Path(__file__).parent
    tree = ast.parse((here / "poller_lease.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    siblings = {p.stem for p in here.glob("*.py")}
    assert not names & siblings, names & siblings
    assert names <= {"__future__", "logging", "threading", "time", "collections", "datetime", "typing"}, names


def test_the_numbers_hold_each_other_up():
    """KTD4's arithmetic, and KTD9's. The last renewal (heartbeat age just under 250 s) still leaves
    the local deadline 30 s before the stored one; the local deadline outlasts the longest beat-less
    wait (one BigQuery call), so a healthy leader inside a slow query keeps the lease; and the
    post-stop work fits the shutdown deadline taken at SIGTERM."""
    assert poller_lease.TTL_S == notification_store.POLLER_LEASE_S
    assert poller_lease.RENEW_WHILE_FRESH_S + poller_lease.MARGIN_S <= poller_lease.TTL_S
    assert poller_lease.TTL_S - poller_lease.MARGIN_S > poller_lease.LONGEST_BEATLESS_WAIT_S
    assert poller_lease.LONGEST_BEATLESS_WAIT_S == main._POLLER_QUERY_TIMEOUT_S
    assert poller_lease.RENEW_WAIT_S < poller_lease.MARGIN_S, "a hung renewal must cost the margin"
    assert poller_lease.SHUTDOWN_DEADLINE_S == 255.0
    # The drain after stop: in-flight sends finish within one push timeout and are then marked (two
    # store writes); the thread's next boundary is at most one BigQuery call away.
    drain = varsel._PUSH_TIMEOUT_S + 2 * notification_store._CALL_DEADLINE_S
    assert drain < poller_lease.SHUTDOWN_DEADLINE_S
    assert main._POLLER_QUERY_TIMEOUT_S < poller_lease.SHUTDOWN_DEADLINE_S


def test_sole_leader_always_leads_and_touches_nothing():
    sole = poller_lease.SoleLeader()
    sole.tick()
    sole.start()
    assert sole.held() and sole.role() == ("leader", None) and sole.release() is None


def test_held_lapses_at_the_last_written_heartbeat_plus_ttl_minus_margin():
    """No store answer needed: the local deadline alone demotes. And a live heartbeat moving on
    without a renewal landing does not extend it -- that is the two-leader bug."""
    lease, clock, store, _ = _lease()
    lease.tick()
    assert lease.held() and store.count("acquire") == 1
    clock.now = 1_000 + 269.9
    clock.beat = clock.now  # the loop keeps beating; no renewal lands
    assert lease.held()
    clock.now = 1_000 + 270.0
    assert not lease.held(), "the deadline is the anchor heartbeat + 270, not the live heartbeat"


def test_a_renewal_moves_the_deadline_to_its_own_heartbeat_and_no_further():
    lease, clock, store, _ = _lease()
    lease.tick()
    clock.now = 1_240.0  # heartbeat age 240: still fresh, renews, but carries the OLD heartbeat
    assert lease.renew_if_due() == "renewed"
    clock.now = 1_270.0
    assert not lease.held(), "a renewal at heartbeat age 240 must not extend past beat + 270"
    # A fresh beat, renewed, does extend -- to that beat + 270.
    lease, clock, store, _ = _lease()
    lease.tick()
    clock.now = clock.beat = 1_200.0
    clock.now = 1_205.0  # a 5 s wait after the beat moves nothing either
    assert lease.renew_if_due() == "renewed"
    clock.now = 1_469.9
    assert lease.held()
    clock.now = 1_470.0
    assert not lease.held()
    assert store.calls[-1][3] == datetime.fromtimestamp(clock.wall() - 270.0, UTC), (
        "the store is told the heartbeat's wall instant, not the renewal's"
    )


def test_the_timer_renews_every_period_while_the_heartbeat_is_fresh_and_stops_past_250():
    lease, clock, store, _ = _lease()
    lease.tick()
    waits: list[float] = []

    class _Periods:
        """Stands in for the timer's stop event: `n` periods, advancing the clock 30 s each."""

        def __init__(self, n):
            self.left = n

        def wait(self, interval):
            waits.append(interval)
            self.left -= 1
            clock.now += interval
            return self.left < 0

    lease._stopped = _Periods(10)
    lease._run_timer()  # the heartbeat never moves: ages 30, 60, ... 300
    assert set(waits) == {poller_lease.RENEW_EVERY_S}
    # Renewed at ages 30..240 (8 periods); 270 and 300 are past the 250 s gate.
    assert store.count("renew") == 8, store.calls
    assert lease.renew_if_due() == "not_held" or not lease.held()


def test_a_renewal_in_flight_is_skipped_not_queued_and_concurrent_callers_make_one_call():
    lease, _clock, store, _ = _lease()
    lease.tick()
    store.renew_gate = threading.Event()
    answers: list[str] = []
    callers = [threading.Thread(target=lambda: answers.append(lease.renew_if_due())) for _ in range(2)]
    for t in callers:
        t.start()
    for t in callers:
        t.join(1.0)
    assert sorted(answers) == ["pending", "skipped"], answers
    assert lease.renew_if_due() == "skipped", "still in flight at the next period: skipped"
    assert store.count("renew") == 1, "nothing was queued behind the hung call"
    store.renew_gate.set()
    _settle(lease)
    assert lease.renew_if_due() == "renewed"
    assert store.count("renew") == 2


def test_a_hung_renewal_returns_within_the_bounded_wait_and_the_lease_lapses_on_time():
    lease, clock, store, _ = _lease()
    lease.tick()
    store.renew_gate = threading.Event()
    clock.now = clock.beat = 1_100.0
    started = time.monotonic()
    assert lease.renew_if_due() == "pending"
    assert time.monotonic() - started < 0.5, "the timer must walk away from a hung store call"
    clock.now = 1_000 + 270.0
    assert not lease.held(), "no renewal landed, so the anchor is still the acquisition's beat"
    store.renew_gate.set()
    _settle(lease)


def test_a_lost_renewal_demotes_at_once_and_on_lose_runs_on_the_next_tick():
    lease, _clock, store, events = _lease()
    lease.tick()
    store.renew_status = "lost"
    store.holder = "pod-b-2"  # somebody else took it
    assert lease.renew_if_due() == "lost"
    assert not lease.held(), "the fences see the loss before the loop's next tick"
    assert events == ["on_acquire"], "callbacks run on the poller thread, never the timer's"
    lease.tick()
    assert events == ["on_acquire", "on_lose"]
    assert lease.role() == ("follower", None)


def test_on_acquire_runs_on_every_acquisition_and_the_role_reports_the_lease_age():
    lease, clock, store, events = _lease()
    lease.tick()
    clock.now = 1_012.0
    assert lease.role() == ("leader", 12.0)
    clock.now = 1_300.0  # lapsed locally
    store.holder = None
    clock.beat = clock.now
    lease.tick()  # notices the loss, then acquires again in the same tick
    assert events == ["on_acquire", "on_lose", "on_acquire"]
    assert lease.held()


def test_a_tenure_lost_during_on_acquire_is_undone_by_on_lose_on_the_next_tick():
    events: list[str] = []
    holder: dict = {}

    def slow_seed():
        events.append("on_acquire")
        holder["store"].renew_status = "lost"
        assert holder["lease"].renew_if_due() == "lost"  # the timer fires mid-seed

    lease, _clock, store, _ = _lease(on_acquire=slow_seed, on_lose=lambda: events.append("on_lose"))
    holder.update(store=store, lease=lease)
    lease.tick()
    assert not lease.held(), "the loop follows this tick"
    store.holder = "pod-b-2"
    lease.tick()
    assert events == ["on_acquire", "on_lose"], "what on_acquire set up (the gauge) is undone"


def test_a_failed_on_acquire_releases_and_stays_a_follower_for_five_ticks():
    def boom():
        raise RuntimeError("seed failed")

    lease, _clock, store, _events = _lease(on_acquire=boom)
    lease.tick()
    assert not lease.held()
    assert store.count("release") == 1 and store.holder is None
    for _ in range(poller_lease.BACKOFF_TICKS):
        lease.tick()
    assert store.count("acquire") == 1, "no acquisition during the back-off"
    lease.tick()
    assert store.count("acquire") == 2


def test_an_acquire_that_raises_leaves_a_follower_and_does_not_raise():
    lease, _clock, store, _ = _lease()
    store.acquire_raises = RuntimeError("firestore unavailable")
    lease.tick()
    assert not lease.held() and lease.role() == ("follower", None)
    store.acquire_raises = None
    lease.tick()
    assert lease.held()


def test_release_matches_the_holder_and_ends_the_tenure_first():
    lease, _clock, store, _ = _lease()
    lease.tick()
    assert lease.release() == "released"
    assert store.calls[-1] == ("release", "pod-a-1") and not lease.held()
    assert lease.renew_if_due() == "not_held"


# ---- main: the role loop --------------------------------------------------------------------------


class _Ticks:
    """Duck-types the stop_event: poll_for_changes runs exactly `n` loop iterations."""

    def __init__(self, n: int) -> None:
        self.left = n

    def wait(self, interval: float) -> bool:
        self.left -= 1
        return self.left <= 0


class _ScriptedLease:
    """`held()` answers from a per-tick script; `tick()` moves to the next entry."""

    def __init__(self, script):
        self.script = list(script)
        self.at = -1

    def tick(self):
        self.at += 1

    def held(self):
        return self.script[self.at]

    def role(self):
        return ("leader" if self.held() else "follower"), None


@contextmanager
def _loop(lease, calls, values=("a", "b", "c", "d", "e")):
    """poll_for_changes with every side job recorded into `calls` and the lease swapped in."""
    probe = iter(values)
    saved = (
        main._poller_lease, main.get_last_ingestion_time, main.follow_last_build, main._rush_hydrate,
        main.togvarsel.refresh, main.togvarsel_notify.evaluate, main.togvarsel_outcomes.refresh,
        main.decisions.check_labelling_freshness, main._MIN_REBUILD_SPACING_S,
        main._poller_heartbeat_monotonic, main._force_rebuild,
    )
    main._poller_lease = lease
    main.get_last_ingestion_time = lambda: next(probe)
    main.follow_last_build = lambda: calls.append("follow") or {}
    main._rush_hydrate = lambda: calls.append("hydrate")
    main.togvarsel.refresh = lambda: calls.append("refresh")
    main.togvarsel_notify.evaluate = lambda: calls.append("evaluate")
    main.togvarsel_outcomes.refresh = lambda: calls.append("outcomes")
    main.decisions.check_labelling_freshness = lambda: calls.append("labelling")
    main._MIN_REBUILD_SPACING_S = 0
    try:
        yield
    finally:
        (
            main._poller_lease, main.get_last_ingestion_time, main.follow_last_build,
            main._rush_hydrate, main.togvarsel.refresh, main.togvarsel_notify.evaluate,
            main.togvarsel_outcomes.refresh, main.decisions.check_labelling_freshness,
            main._MIN_REBUILD_SPACING_S, main._poller_heartbeat_monotonic, main._force_rebuild,
        ) = saved


_LEADER_TICK = ["refresh", "evaluate", "outcomes", "labelling"]


def test_a_scripted_lease_runs_the_cycle_only_on_the_ticks_it_is_held():
    calls: list[str] = []
    with _loop(_ScriptedLease([True, True, False, True]), calls):
        main.poll_for_changes(lambda ts: calls.append(f"on_change:{ts}"), _Ticks(4))
    assert calls == (
        ["on_change:a", *_LEADER_TICK]
        + ["on_change:b", *_LEADER_TICK]
        + ["follow", "hydrate"]
        + ["on_change:c", *_LEADER_TICK]
    ), calls


def test_an_acquire_that_raises_runs_the_follower_body_and_the_loop_goes_on():
    calls: list[str] = []
    store = _Store()
    store.acquire_raises = RuntimeError("firestore unavailable")
    lease, _clock, _store, _ = _lease(store=store)
    with _loop(lease, calls):
        main.poll_for_changes(lambda ts: calls.append("on_change"), _Ticks(2))
    assert calls == ["follow", "hydrate", "follow", "hydrate"], calls
    assert store.count("acquire") == 2


def test_an_acquisition_forces_the_next_rebuild_past_the_stamp_and_the_spacing():
    """The first cycle after an acquisition is the new leader's: it must not wait for SIRI-SX to
    move, nor for a spacing floor measured from a rebuild somebody else ran."""
    calls: list[str] = []
    with _loop(_ScriptedLease([True, True, True]), calls, values=("a", "a", "a")):
        main._MIN_REBUILD_SPACING_S = 10_000

        def on_change(ts):
            calls.append("on_change")
            if calls.count("on_change") == 1:
                main._force_rebuild = True  # what on_acquire sets

        main.poll_for_changes(on_change, _Ticks(3))
    assert calls.count("on_change") == 2, calls


def test_a_follower_tick_beats_so_health_stays_200():
    calls: list[str] = []
    with _loop(_ScriptedLease([False]), calls):
        main._poller_heartbeat_monotonic = None
        main.poll_for_changes(lambda ts: None, _Ticks(1))
        assert main._poller_heartbeat_monotonic is not None
        before = main._poller_thread

        class _Alive:
            name = "poller"

            def is_alive(self):
                return True

        main._poller_thread = _Alive()
        try:
            assert main._poller_health() == (True, None)
        finally:
            main._poller_thread = before


def test_a_demoted_leader_runs_no_further_tick_writer():
    """Lost between two writers: the rest of the tick is skipped, logged once as `lease_lost`."""
    calls: list[str] = []
    lease = _ScriptedLease([True])

    def refresh():
        calls.append("refresh")
        lease.script[0] = False

    with _loop(lease, calls), _recorded() as fields:
        main.togvarsel.refresh = refresh
        main.poll_for_changes(lambda ts: calls.append("on_change"), _Ticks(1))
    assert calls == ["on_change", "refresh"], calls
    (record,) = [f for f in fields if f.get("event") == "poller_lease"]
    assert record["result"] == "lease_lost" and record["phase"] == "togvarsel_notify", record


def test_the_role_seam_reads_the_lease():
    saved = main._poller_lease
    main._poller_lease = _ScriptedLease([False])
    main._poller_lease.tick()
    try:
        assert main._poller_role() == ("follower", None)
    finally:
        main._poller_lease = saved
    assert main._poller_role() == ("leader", None), "the default is the sole leader"


def test_the_holder_is_the_pod_name_with_a_per_process_suffix():
    import os

    saved = os.environ.get("HOSTNAME")
    os.environ["HOSTNAME"] = "kix-avvik-7d9f-abcde"
    try:
        first, second = main._poller_holder_id(), main._poller_holder_id()
    finally:
        if saved is None:
            os.environ.pop("HOSTNAME", None)
        else:
            os.environ["HOSTNAME"] = saved
    assert first.startswith("kix-avvik-7d9f-abcde-") and second.startswith("kix-avvik-7d9f-abcde-")
    assert first != second, "two processes on one pod (a restart) are two holders"


# ---- main: the fences ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.fields: list[dict] = []

    def _record(self, *args, **kwargs) -> None:
        self.fields.append((kwargs.get("extra") or {}).get("fields", {}))

    info = warning = error = _record

    def __getattr__(self, _name):
        return lambda *a, **k: None


@contextmanager
def _recorded():
    """Every record `main` or `poller_handover` (the lease loss and the shutdown) writes inside the
    block; one recorder for both, so a test sees them in the order they were written."""
    import poller_handover

    recorder = _Recorder()
    saved = main.logger, poller_handover.logger
    main.logger = poller_handover.logger = recorder
    try:
        yield recorder.fields
    finally:
        main.logger, poller_handover.logger = saved


class _CycleClient:
    """bq_client for a cycle: records each query, every table is absent, and `on_query` sees each
    query as it is issued (which is where a test loses the lease)."""

    def __init__(self, on_query=lambda sql: None):
        self.queries: list[str] = []
        self.on_query = on_query

    def query(self, sql, *args, **kwargs):
        self.queries.append(sql)
        self.on_query(sql)

        class _Job:
            def result(self_inner, timeout=None):
                return []

        return _Job()

    def get_table(self, name, timeout=None):
        raise NotFound(name)


class _FlagLease:
    def __init__(self):
        self.lead = True

    def tick(self):
        pass

    def held(self):
        return self.lead

    def role(self):
        return "leader", None


@contextmanager
def _cycle(client, lease=None, stop=None):
    saved = (main.bq_client, main._poller_lease, main._poller_stop_event, main._rail_calls_refreshed_at)
    main.bq_client = client
    main._poller_lease = lease or _FlagLease()
    main._poller_stop_event = stop
    try:
        with _recorded() as fields:
            yield fields
    finally:
        (main.bq_client, main._poller_lease, main._poller_stop_event, main._rail_calls_refreshed_at) = saved


def _phases(fields):
    return [f["phase"] for f in fields if f.get("event") == "poller_phase"]


def _lost_at(query_sql, *, reason="lease_lost"):
    lease, stop = _FlagLease(), threading.Event()

    def on_query(sql):
        if sql == query_sql:
            if reason == "lease_lost":
                lease.lead = False
            else:
                stop.set()

    client = _CycleClient(on_query)
    with _cycle(client, lease, stop) as fields:
        main._on_ingestion_change(None)
    return client, fields


def test_a_lease_lost_mid_cycle_stops_before_the_next_phase_with_no_record_for_it():
    client, fields = _lost_at(main._DISRUPTIONS_QUERY)
    assert main._UPDATE_CATEGORIES_QUERY not in client.queries
    assert main._PREDICT_QUERY not in client.queries, "no CREATE OR REPLACE after the loss"
    assert _phases(fields)[-1] == "disruptions_sql", _phases(fields)
    assert not [f for f in fields if f.get("event") == "poller_phase" and f["result"] == "error"]
    (record,) = [f for f in fields if f.get("event") == "poller_lease"]
    # `categories` sits inside a `try/except Exception`: the fence is a BaseException, so it is
    # not swallowed there.
    assert (record["result"], record["phase"]) == ("lease_lost", "categories"), record
    (cycle,) = [f for f in fields if f.get("event") == "poller_cycle"]
    assert "categories" not in cycle["phases"]


def test_lost_during_categories_it_stops_before_predict_sql():
    client, fields = _lost_at(main._UPDATE_CATEGORIES_QUERY)
    assert main._PREDICT_QUERY not in client.queries
    (record,) = [f for f in fields if f.get("event") == "poller_lease"]
    assert (record["result"], record["phase"]) == ("lease_lost", "predict_sql"), record


def test_a_stop_between_phases_ends_the_cycle_the_same_way():
    client, fields = _lost_at(main._DISRUPTIONS_QUERY, reason="stopping")
    assert main._PREDICT_QUERY not in client.queries
    (record,) = [f for f in fields if f.get("event") == "poller_lease"]
    assert (record["result"], record["phase"]) == ("stopping", "categories"), record


def test_a_fenced_cycle_does_not_stamp_freshness():
    metrics.POLLER_LAST_CYCLE.set(123.0)
    _lost_at(main._DISRUPTIONS_QUERY)
    assert metrics.POLLER_LAST_CYCLE._value.get() == 123.0


class _TableClient(_CycleClient):
    def get_table(self, name, timeout=None):
        class _T:
            modified = datetime.now(UTC)

        return _T()


def test_call_predictions_check_and_beat_between_their_two_queries():
    saved = (main._call_pred_refreshed_at, main._beat)
    events: list[str] = []
    try:
        # Healthy: a beat between the rail and the bus query.
        client = _TableClient(lambda sql: events.append(sql))
        main._beat = lambda: events.append("beat")
        main._call_pred_refreshed_at = None
        with _cycle(client):
            main._load_from_last_build("call_predictions", leader=True)
        rail = events.index(main._CALL_PREDICTIONS_QUERY)
        assert "beat" in events[rail : events.index(main._CALL_PREDICTIONS_BUS_QUERY)]
        # Lost after the first query: the second is never issued.
        lease = _FlagLease()
        client = _TableClient(lambda sql: setattr(lease, "lead", False))
        main._call_pred_refreshed_at = None
        with _cycle(client, lease):
            try:
                main._load_from_last_build("call_predictions", leader=True)
            except main._PollerFenced as fenced:
                assert fenced.phase == "call_predictions_cache"
            else:
                raise AssertionError("the lease check between the two queries did not fire")
        assert client.queries == [main._CALL_PREDICTIONS_QUERY]
        # A follower's load has no lease to lose: both queries, no raise.
        client = _TableClient()
        main._call_pred_refreshed_at = None
        with _cycle(client, _FlagLease()) as _:
            main._poller_lease.lead = False
            main._load_from_last_build("call_predictions", leader=False)
        assert client.queries == [main._CALL_PREDICTIONS_QUERY, main._CALL_PREDICTIONS_BUS_QUERY]
    finally:
        main._call_pred_refreshed_at, main._beat = saved


def test_a_refused_removed_build_checks_the_lease_before_emptying_the_table():
    import inspect

    source = inspect.getsource(main.maybe_refresh_removed_calls)
    first = source.index("_build_removed_calls(enabled=enabled)")
    assert first < source.index('_fence("rail_removed_calls")') < source.index(
        "_build_removed_calls(enabled=False)"
    )
    assert source.index('_fence("rail_removed_calls")') < source.index("_removed_built_for = planned")


def test_the_alerting_pass_is_handed_the_lease():
    import inspect

    source = inspect.getsource(main._rebuild_all)
    assert "varsel.check_and_notify(_varsel_snapshot(), should_continue=_still_leading)" in source


# ---- main: acquisition, loss and shutdown --------------------------------------------------------


@contextmanager
def _hooks(calls, *, webhooks=True, fail=None):
    saved = (
        notification_store.reset_mirrors, notification_store.sync_mirrors,
        main.varsel_state.load_rounds, main._rush_hydrate, main._force_rebuild,
        main.varsel.webhook_channel_enabled, notification_events.wait_until_idle,
    )

    def step(name):
        def run(*a, **k):
            calls.append(name)
            if name == fail:
                raise RuntimeError(name)

        return run

    notification_store.reset_mirrors = step("reset_mirrors")
    notification_store.sync_mirrors = step("sync_mirrors")
    main.varsel_state.load_rounds = step("load_rounds")
    main._rush_hydrate = step("rush_hydrate")
    main.varsel.webhook_channel_enabled = lambda: webhooks
    notification_events.wait_until_idle = lambda timeout_s: calls.append("wait_until_idle") or True
    main._force_rebuild = False
    try:
        yield
    finally:
        (
            notification_store.reset_mirrors, notification_store.sync_mirrors,
            main.varsel_state.load_rounds, main._rush_hydrate, main._force_rebuild,
            main.varsel.webhook_channel_enabled, notification_events.wait_until_idle,
        ) = saved


def _gauge():
    return metrics.POLLER_LEADER._value.get()


def test_on_acquire_seeds_reloads_hydrates_forces_and_sets_the_gauge():
    calls: list[str] = []
    with _hooks(calls):
        main._on_lease_acquired()
        assert main._force_rebuild is True
    assert calls == ["reset_mirrors", "sync_mirrors", "load_rounds", "rush_hydrate"], calls
    assert _gauge() == 1
    calls.clear()
    with _hooks(calls, webhooks=False):
        main._on_lease_acquired()
    assert calls == ["reset_mirrors", "load_rounds", "rush_hydrate"], "the cycle's own gate"


def test_the_mirrors_are_seeded_under_the_store_backend_with_the_webhook_flag_off():
    """U5 step 1: under `VARSEL_STATE_BACKEND=store` the cycle reads `/varsel`'s registrations from
    the mirror, so a seed gated on the webhook flag alone left `all_journeys()` empty and the
    leader alerted nobody. Both gates -- the acquisition's and the cycle's phase -- follow it."""
    import inspect

    calls: list[str] = []
    saved = main._varsel_store
    main._varsel_store = object()
    try:
        with _hooks(calls, webhooks=False):
            main._on_lease_acquired()
        assert main._mirrors_wanted(False) is True
    finally:
        main._varsel_store = saved
    assert calls == ["reset_mirrors", "sync_mirrors", "load_rounds", "rush_hydrate"], calls
    assert main._mirrors_wanted(False) is False, "no backend, no flag: today's cycle, unchanged"
    assert main._mirrors_wanted(True) is True
    source = inspect.getsource(main._rebuild_all)
    gate = source.index("if _mirrors_wanted(webhook_channel):")
    assert gate < source.index('_phase("notification_mirror"'), "the phase must sit behind it"


def test_on_acquire_follows_the_stored_translations_and_survives_a_store_that_raises():
    """Translation store R5/AE2: a new leader reads what another leader translated before its first
    cycle, and a Firestore it cannot read costs it the translations, never the lease."""
    import situation_english

    class _Raising:
        def __init__(self):
            self.calls = []

        def seed_situation_translations(self, *, limit, beat=None):
            self.calls.append("seed")
            raise RuntimeError("firestore down")

    store = _Raising()
    calls: list[str] = []
    situation_english.configure(store=store)
    logging.getLogger("situation_english").setLevel(logging.CRITICAL)
    try:
        with _hooks(calls):
            main._on_lease_acquired()
            assert main._force_rebuild is True
        assert store.calls == ["seed"]
        assert _gauge() == 1
    finally:
        situation_english.configure(store=None)
        situation_english.reset_state()


def test_on_acquire_seeds_the_stored_translations_with_the_poller_beat():
    """The seed reads pages of up to CACHE_MAX entries before the first cycle; without the beat a
    slow read on a large store looks like a hung poller to the liveness probe."""
    import situation_english

    class _Recording:
        def __init__(self):
            self.beats = []

        def seed_situation_translations(self, *, limit, beat=None):
            self.beats.append(beat)
            return [], None

    store = _Recording()
    calls: list[str] = []
    situation_english.configure(store=store)
    metrics.POLLER_LEADER.set(0)
    try:
        with _hooks(calls):
            main._on_lease_acquired()
        assert store.beats == [main._beat], store.beats
        assert _gauge() == 1
    finally:
        situation_english.configure(store=None)
        situation_english.reset_state()


def test_on_acquire_follows_the_card_caches_with_the_beat_and_survives_a_store_that_raises():
    """Translation store U7, R10/R5: a new leader loads the last leader's /varsel card caches
    before its first cycle, beating between pages, and a store it cannot read costs it those
    caches, never the lease."""
    import varsel

    class _Raising:
        def __init__(self):
            self.beats = []

        def seed_card_entries(self, *, beat=None):
            self.beats.append(beat)
            raise RuntimeError("firestore down")

    store = _Raising()
    calls: list[str] = []
    varsel.configure_card_store(store)
    logging.getLogger("varsel").setLevel(logging.CRITICAL)
    metrics.POLLER_LEADER.set(0)
    try:
        with _hooks(calls):
            main._on_lease_acquired()
            assert main._force_rebuild is True
        assert store.beats == [main._beat], store.beats
        assert _gauge() == 1
    finally:
        logging.getLogger("varsel").setLevel(logging.NOTSET)
        varsel.configure_card_store(None)
        varsel.reset_card_store_state()


def test_a_failing_on_acquire_clears_the_gauge_and_raises_to_the_state_machine():
    calls: list[str] = []
    metrics.POLLER_LEADER.set(1)
    with _hooks(calls, fail="sync_mirrors"):
        try:
            main._on_lease_acquired()
        except RuntimeError:
            pass
        else:
            raise AssertionError("the state machine must see the failure to release and back off")
        assert main._force_rebuild is False
    assert _gauge() == 0


def test_on_lose_clears_the_gauge_waits_for_the_emit_pass_and_resets_the_mirrors():
    calls: list[str] = []
    metrics.POLLER_LEADER.set(1)
    with _hooks(calls):
        main._on_lease_lost()
    assert calls == ["wait_until_idle", "reset_mirrors"], calls
    assert _gauge() == 0


def test_on_lose_resets_the_mirrors_anyway_when_the_emit_pass_outlives_the_drain():
    calls: list[str] = []
    metrics.POLLER_LEADER.set(1)
    with _hooks(calls), _recorded() as fields:
        notification_events.wait_until_idle = lambda timeout_s: calls.append("wait_until_idle") or False
        main._on_lease_lost()
    assert calls == ["wait_until_idle", "reset_mirrors"], calls
    assert any(f.get("result") == "drain_timeout" for f in fields), fields
    assert _gauge() == 0


def test_on_lose_leaves_both_real_mirrors_not_ready():
    notification_store.subscription_mirror()._seeded = True
    notification_store.subject_mirror()._seeded = True
    saved = notification_events.wait_until_idle
    notification_events.wait_until_idle = lambda timeout_s: True
    try:
        main._on_lease_lost()
    finally:
        notification_events.wait_until_idle = saved
    assert not notification_store.subscription_mirror().ready
    assert not notification_store.subject_mirror().ready


class _Thread:
    def __init__(self, exits=True):
        self.exits = exits
        self.joined: list[float] = []

    def join(self, timeout=None):
        self.joined.append(timeout)

    def is_alive(self):
        return not self.exits


class _ReleaseLease(_FlagLease):
    def __init__(self, order):
        super().__init__()
        self.order = order
        self.stopped = False

    def release(self):
        self.order.append("release")
        return "released"

    def stop(self):
        self.stopped = True


def test_shutdown_releases_after_the_thread_exits_and_the_emit_pass_drains():
    order: list[str] = []
    thread, stop, lease = _Thread(), threading.Event(), _ReleaseLease(order)
    metrics.POLLER_LEADER.set(1)
    saved = notification_events.wait_until_idle
    notification_events.wait_until_idle = lambda timeout_s: order.append("drained") or True
    try:
        main._shutdown_poller(thread, stop, lease, deadline_s=0.5)
    finally:
        notification_events.wait_until_idle = saved
    assert stop.is_set() and thread.joined and 0 < thread.joined[0] <= 0.5
    assert order == ["drained", "release"], order
    assert lease.stopped and _gauge() == 0


def test_a_pass_lock_held_past_the_deadline_is_released_anyway_and_logged():
    order: list[str] = []
    lease = _ReleaseLease(order)
    held = threading.Event()
    done = threading.Event()

    def hold():
        with notification_events._pass_lock:
            held.set()
            done.wait(2.0)

    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(1.0)
    try:
        with _recorded() as fields:
            started = time.monotonic()
            main._shutdown_poller(_Thread(), threading.Event(), lease, deadline_s=0.1)
            assert time.monotonic() - started < 0.5
    finally:
        done.set()
        holder.join(1.0)
    assert order == ["release"]
    assert any(f.get("result") == "drain_timeout" for f in fields), fields


def test_a_thread_still_alive_at_the_deadline_leaves_the_lease_unreleased():
    order: list[str] = []
    lease = _ReleaseLease(order)
    with _recorded() as fields:
        main._shutdown_poller(_Thread(exits=False), threading.Event(), lease, deadline_s=0.05)
    assert order == [], "a phase is still running: the successor waits out the TTL instead"
    assert any(f.get("result") == "release_skipped" for f in fields), fields
    assert _gauge() == 0


def test_the_sole_leader_keeps_the_five_second_join():
    thread = _Thread()
    calls: list[str] = []
    saved = notification_events.wait_until_idle
    notification_events.wait_until_idle = lambda timeout_s: calls.append("drain") or True
    try:
        main._shutdown_poller(thread, threading.Event(), poller_lease.SoleLeader())
    finally:
        notification_events.wait_until_idle = saved
    assert thread.joined == [5] and calls == [], "no lease, no successor to protect"


def test_the_lifespan_builds_a_store_lease_only_under_the_store_backend():
    """KTD5: gated on `_varsel_store`, never on `notification_store.enabled()`, which is true in
    tst where no database exists. Checked over the source because entering the lifespan starts the
    poller."""
    import inspect

    source = inspect.getsource(main.lifespan)
    assert "if _varsel_store is not None:" in source
    gate = source.index("if _varsel_store is not None:")
    assert gate < source.index("poller_lease.StoreLease(") < source.index("thread.start()")
    assert "notification_store.enabled()" not in source[gate : source.index("thread.start()")]
    assert "_shutdown_poller(thread, stop_event, _poller_lease)" in source


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} poller lease checks passed.")


if __name__ == "__main__":
    _run()
