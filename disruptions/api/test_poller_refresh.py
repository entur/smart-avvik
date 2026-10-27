"""Checks for the poller's rebuild cycle: the TTL-gated actuals scan, and the per-step timing.

Hermetic: the BigQuery client is stubbed before main is imported, and the app's lifespan is never
entered — so no poller starts and nothing here touches BigQuery or the network. The stub records
every query it is handed, which is the whole point: what is under test is *whether* the scan runs,
not what it returns.

rail_calls_today is the most expensive query the poller issues (a CREATE OR REPLACE over the whole
day-so-far ET partition, 3.4 GB by late evening), so the gate that keeps it from running on every
SX ingestion change is worth pinning down. The date-rollover case especially: the table holds one
UTC day, so "fresh" cannot be a pure elapsed-time question.

The second half covers `main._phase` / the `poller_cycle` summary — see the comment above those
checks for why the properties worth pinning are the exception-handling ones, not the durations.

Run: uv run --with fastapi --with httpx --with pydantic python test_poller_refresh.py
"""

import os
import textwrap
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import main
import notification_servability


class _RecordingClient:
    """Stands in for bq_client: records each query and returns an object with .result()."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sql: str, *args, **kwargs):
        self.queries.append(sql)

        class _Job:
            def result(self_inner, timeout=None):  # poller-path calls pass a timeout (U1)
                return []

        return _Job()


class _FrozenDatetime(datetime):
    """datetime with a pinned now(); main.py calls datetime.now(UTC) inside the gate."""

    _pinned = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):
        return cls._pinned


def _run(refreshed_at, pinned):
    """Invoke the gate with a given last-refresh stamp and wall clock; return queries issued."""
    recorder = _RecordingClient()
    before_client, before_stamp, before_dt = (
        main.bq_client,
        main._rail_calls_refreshed_at,
        main.datetime,
    )
    main.bq_client = recorder
    main._rail_calls_refreshed_at = refreshed_at
    _FrozenDatetime._pinned = pinned
    main.datetime = _FrozenDatetime
    try:
        main.maybe_refresh_rail_calls()
        return recorder.queries, main._rail_calls_refreshed_at
    finally:
        main.bq_client = before_client
        main._rail_calls_refreshed_at = before_stamp
        main.datetime = before_dt


NOON = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def test_line_modes_load_once_a_day_and_a_failed_load_keeps_the_last_map():
    """KIX-23. One Journey Planner query per `line_modes.TTL`; a failure, or an answer with no
    lines, raises into the phase's own try and leaves yesterday's map -- a JP outage must not turn
    every bus alert's `mode` null -- and the next tick asks again."""
    import alternatives
    import line_modes

    replies = [{"data": {"lines": [{"id": "RUT:Line:5", "transportMode": "metro"}]}}]
    asked = []

    def post(url, payload, headers):
        asked.append(payload["query"])
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    real = alternatives._post_json
    alternatives._post_json = post
    line_modes.reset()
    try:
        main.refresh_line_modes()
        main.refresh_line_modes()  # inside the TTL: no second query
        assert len(asked) == 1 and line_modes.transport_mode("RUT", "RUT:Line:5") == "metro"
        line_modes._fetched_at -= line_modes.TTL
        for failure in (RuntimeError("JP down"), {"data": {"lines": []}}):
            replies.append(failure)
            try:
                main.refresh_line_modes()
            except RuntimeError:
                pass
            else:
                raise AssertionError("a failed load must raise into the phase's try")
            assert line_modes.transport_mode("RUT", "RUT:Line:5") == "metro", "yesterday's map stays"
        assert len(asked) == 3, "a failure leaves it due, so the next tick asks again"
    finally:
        alternatives._post_json = real
        line_modes.reset()


def test_first_call_always_rebuilds():
    """No stamp yet — the table may be missing or left over from yesterday, so never skip."""
    queries, stamp = _run(None, NOON)
    assert queries == [main._RAIL_CALLS_QUERY]
    assert stamp == NOON


def test_within_ttl_is_skipped():
    """The whole point of the gate: a tick 5 minutes after a rebuild must not rescan."""
    queries, _ = _run(NOON - timedelta(minutes=5), NOON)
    assert queries == []


def test_past_ttl_rebuilds():
    queries, stamp = _run(NOON - timedelta(minutes=16), NOON)
    assert queries == [main._RAIL_CALLS_QUERY]
    assert stamp == NOON


def test_ttl_boundary_is_exclusive():
    """Exactly _RAIL_CALLS_TTL old is stale — `now - stamp < TTL` is the freshness test."""
    queries, _ = _run(NOON - main._RAIL_CALLS_TTL, NOON)
    assert queries == [main._RAIL_CALLS_QUERY]


def test_date_rollover_rebuilds_even_inside_ttl():
    """23:58 -> 00:03 is 5 minutes, but the table now holds *yesterday*. Must rebuild.

    Without the date clause the 00:03 tick would be served a stale day for the rest of the TTL —
    and just after midnight is the cheapest moment of the day to run this scan anyway.
    """
    just_after_midnight = datetime(2026, 8, 9, 0, 3, tzinfo=UTC)
    queries, stamp = _run(datetime(2026, 8, 8, 23, 58, tzinfo=UTC), just_after_midnight)
    assert queries == [main._RAIL_CALLS_QUERY]
    assert stamp == just_after_midnight


def test_same_day_inside_ttl_still_skipped():
    """Guard against the date clause over-firing: same UTC day, inside TTL -> still a skip."""
    queries, _ = _run(
        datetime(2026, 8, 8, 23, 50, tzinfo=UTC), datetime(2026, 8, 8, 23, 55, tzinfo=UTC)
    )
    assert queries == []


# --- main._phase: the cycle's per-step timing (added 2026-08-21) --------------------------------
#
# What these pin is not the timing itself (a duration assertion would be a flake) but the two
# properties that decide whether adding instrumentation was safe. The rebuild's isolation model is
# built entirely out of `try/except` around individual steps -- each one decides what a failure
# there degrades -- and `_phase` is now wrapped INSIDE several of those. A context manager that
# swallowed would silently convert "the bus predictions failed, serve NSB-only" into "the bus
# predictions succeeded", which no other check in this repo would catch.


class _Recorder:
    """Collects the `extra={"fields": ...}` payloads main.logger is handed."""

    def __init__(self) -> None:
        self.fields: list[dict] = []

    def info(self, *args, **kwargs) -> None:
        extra = kwargs.get("extra") or {}
        self.fields.append(extra.get("fields", {}))

    def __getattr__(self, _name):  # exception/warning/etc. are not under test here
        return lambda *a, **k: None


def _with_recorder(fn):
    """Run fn() with main.logger stubbed; return (recorded field dicts, raised exception or None)."""
    recorder = _Recorder()
    before = main.logger
    main.logger = recorder
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the test decides what to do with it
        return recorder.fields, exc
    finally:
        main.logger = before
    return recorder.fields, None


def test_phase_records_a_successful_step():
    timings: dict[str, int] = {}

    def run():
        with main._phase("some_step", timings):
            pass

    fields, exc = _with_recorder(run)
    assert exc is None
    assert "some_step" in timings
    assert next(f for f in fields if f.get("event") == "poller_phase")["result"] == "ok"


def test_phase_does_not_swallow_the_exception():
    """The whole isolation model of the rebuild depends on this."""
    timings: dict[str, int] = {}

    def run():
        with main._phase("failing_step", timings):
            raise RuntimeError("boom")

    _, exc = _with_recorder(run)
    assert isinstance(exc, RuntimeError), "a failing step must still reach its own except block"


def test_phase_times_a_step_that_failed():
    """A step that fails SLOWLY is the most interesting reading; it must not go unrecorded."""
    timings: dict[str, int] = {}

    def run():
        try:
            with main._phase("failing_step", timings):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

    fields, _ = _with_recorder(run)
    assert "failing_step" in timings, "a failed step must still record its duration"
    phase = next(f for f in fields if f.get("event") == "poller_phase")
    assert phase["result"] == "error"
    assert phase["phase"] == "failing_step"


def test_the_cycle_summary_is_logged_even_when_the_rebuild_dies():
    """`poll_for_changes` catches whatever escapes _on_ingestion_change, so this line is the only
    record that the cycle ran at all -- and a partial `phases` breakdown is itself the diagnosis."""
    before = main._rebuild_all

    def exploding_rebuild(timings):
        timings["reached_step"] = 42
        raise RuntimeError("rebuild died")

    main._rebuild_all = exploding_rebuild
    try:
        fields, exc = _with_recorder(lambda: main._on_ingestion_change(None))
    finally:
        main._rebuild_all = before

    assert isinstance(exc, RuntimeError), "the failure must still propagate to poll_for_changes"
    cycle = [f for f in fields if f.get("event") == "poller_cycle"]
    assert cycle, "no poller_cycle line was logged for a rebuild that died"
    assert cycle[0]["phases"] == {"reached_step": 42}


# --- The wall-clock tick: togvarsel refresh + notify wiring (U4) --------------------------------
#
# togvarsel_notify.evaluate() rides the EVERY-TICK wall-clock path, after togvarsel.refresh() so
# the tick that issues a round is the tick that can notify it -- and never the SX-ingestion
# rebuild, whose cadence is ingestion changes, not the clock. Its failure isolation matters the
# same way refresh's does: a notify bug must cost notifications, never the feed or the tick.


class _Ticks:
    """Duck-types the stop_event: poll_for_changes runs exactly `n` loop iterations."""

    def __init__(self, n: int) -> None:
        self.left = n

    def wait(self, interval: float) -> bool:
        self.left -= 1
        return self.left <= 0


def test_the_wall_clock_tick_runs_refresh_then_evaluate_and_isolates_evaluate():
    calls: list[str] = []

    def fake_evaluate():
        calls.append("evaluate")
        raise RuntimeError("notify blew up")  # must be logged, never kill the tick or the loop

    before = (main.get_last_ingestion_time, main.togvarsel.refresh, main.togvarsel_notify.evaluate)
    main.get_last_ingestion_time = lambda: datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
    main.togvarsel.refresh = lambda: calls.append("refresh")
    main.togvarsel_notify.evaluate = fake_evaluate
    try:
        _fields, exc = _with_recorder(
            lambda: main.poll_for_changes(lambda ts: calls.append("on_change"), _Ticks(2))
        )
    finally:
        (
            main.get_last_ingestion_time,
            main.togvarsel.refresh,
            main.togvarsel_notify.evaluate,
        ) = before
    assert exc is None, "an evaluate failure escaped the tick"
    # tick 1: the first-ever ingestion value fires on_change, then refresh, then evaluate;
    # evaluate's failure reaches neither the loop nor tick 2 (unchanged value -> no on_change).
    assert calls == ["on_change", "refresh", "evaluate", "refresh", "evaluate"]


def test_the_outcome_pass_runs_last_and_is_isolated_like_the_two_before_it():
    """The third wall-clock step, and it must fail exactly as harmlessly as the other two.

    Order matters in one direction only: it runs AFTER notify because it reads what the rounds
    wrote, and it must never run before `togvarsel.refresh()` either. Isolation is what actually
    bites — this pass makes blocking BigQuery calls (a ~32.5 MB SELECT plus three MERGEs per day of
    the backlog) on the SOLE poller thread, so a BigQuery outage inside it would otherwise take the
    disruption feed and every push down with /rushhistorikk's verdicts.
    """
    calls: list[str] = []
    before = (
        main.get_last_ingestion_time,
        main.togvarsel.refresh,
        main.togvarsel_notify.evaluate,
        main.togvarsel_outcomes.refresh,
    )
    main.get_last_ingestion_time = lambda: datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
    main.togvarsel.refresh = lambda: calls.append("refresh")
    main.togvarsel_notify.evaluate = lambda: calls.append("evaluate")

    def fake_outcomes():
        calls.append("outcomes")
        raise RuntimeError("BigQuery is having a day")

    main.togvarsel_outcomes.refresh = fake_outcomes
    try:
        _fields, exc = _with_recorder(
            lambda: main.poll_for_changes(lambda ts: calls.append("on_change"), _Ticks(2))
        )
    finally:
        (
            main.get_last_ingestion_time,
            main.togvarsel.refresh,
            main.togvarsel_notify.evaluate,
            main.togvarsel_outcomes.refresh,
        ) = before
    assert exc is None, "an outcome-pass failure escaped the tick"
    assert calls == [
        "on_change", "refresh", "evaluate", "outcomes",
        "refresh", "evaluate", "outcomes",
    ]


def test_the_outcome_pass_never_rides_the_sx_rebuild():
    """Wall-clock only, and last of the three. `_run_settled`'s own docstring describes where it
    sits relative to the other two, so this is also what keeps that comment honest."""
    import inspect

    assert "togvarsel_outcomes" not in inspect.getsource(main._rebuild_all)
    assert "togvarsel_outcomes" not in inspect.getsource(main._on_ingestion_change)
    poll = inspect.getsource(main.poll_for_changes)
    assert poll.index("togvarsel_notify.evaluate()") < poll.index("togvarsel_outcomes.refresh()")


def test_evaluate_never_rides_the_sx_rebuild():
    """The notify step is wall-clock-driven only: never from the SX-ingestion rebuild, and on the
    wall-clock tick always AFTER togvarsel.refresh() (a just-issued round is seen this tick)."""
    import inspect

    assert "togvarsel_notify" not in inspect.getsource(main._rebuild_all)
    assert "togvarsel_notify" not in inspect.getsource(main._on_ingestion_change)
    poll = inspect.getsource(main.poll_for_changes)
    assert poll.index("togvarsel.refresh()") < poll.index("togvarsel_notify.evaluate()")


@contextmanager
def _env(**values):
    """Set env vars for the body and restore whatever was there (including absence) after."""
    import os

    before = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_togvarsel_notify_flag_is_an_allow_list_with_unset_off():
    """R9: only true/1/yes/on enable (the delivery-flag convention), and UNSET is OFF — the code
    default matches the closed gate, deliberately unlike VARSEL_LLM_ENABLED's default-"true".

    Read with the master gate held OPEN, since 2026-09-08: the two flags are ANDed, and this check
    is about the delivery flag's own parse. What the AND does is the next check.
    """
    import os

    with _env(SHOW_RUSHVARSEL="true", TOGVARSEL_NOTIFY_ENABLED=None):
        assert main._togvarsel_notify_enabled() is False  # unset -> off, no helm value needed
        for value, want in (
            ("true", True), ("1", True), ("yes", True), ("on", True), ("TRUE", True),
            (" true ", True), ("false", False), ("", False), ("off", False), ("disabled", False),
            ("0", False),
        ):
            os.environ["TOGVARSEL_NOTIFY_ENABLED"] = value
            assert main._togvarsel_notify_enabled() is want, value


def test_the_master_gate_overrides_the_delivery_flag_in_both_directions():
    """SHOW_RUSHVARSEL off means no rush-forecast push, whatever TOGVARSEL_NOTIFY_ENABLED says.

    The AND is what lets dev's and prd's TOGVARSEL_NOTIFY_ENABLED="true" stay in place while the
    product is dark, so re-enabling is one value per environment. Both directions are checked
    because only one of them is the failure that would ship silently: an override that read the
    master gate but not the delivery flag would turn pushes ON in tst the moment the product was
    un-hidden, in the one environment that has never notified.

    This one function is the seam for all three ways rushvarsel reaches a traveller — the push
    round's `enabled` gate, /varsel-api/config's `togvarsel_notify` (the «Nytt: Rushvarsel»
    notice) and rush_outlook()'s gate (the panel at the top of /varsel) — so the four cases here
    are the whole matrix for every one of them.
    """
    for show, notify, want in (
        ("true", "true", True),     # the product on, this environment delivering
        ("true", "false", False),   # the product on, this environment silent (tst)
        ("false", "true", False),   # dark: dev's and prd's delivery values stay, and do nothing
        ("false", "false", False),
        (None, "true", False),      # unset master gate is off, and needs no helm value to be
    ):
        with _env(SHOW_RUSHVARSEL=show, TOGVARSEL_NOTIFY_ENABLED=notify):
            assert main._togvarsel_notify_enabled() is want, (show, notify)


def test_neither_the_page_nor_the_api_answers_while_the_master_gate_is_off():
    """With SHOW_RUSHVARSEL off, nothing under /rushvarsel answers on main's own app.

    Both halves are gated at IMPORT time, unlike every other read of this flag, and for two
    different reasons. The page: a Mount cannot be conditioned per request without first
    answering the bare /rushvarsel with a 307 to the trailing slash, so it is gated by not being
    mounted. The API: a router that is registered but 404s would still publish its endpoint names
    on /docs (FastAPI's default docs_url), and a listing is itself a mention of a product we are
    not showing — hence the schema assertion, which is the only one of these that /docs reflects.

    Probed by ASKING rather than by scanning app.routes: this FastAPI version keeps an included
    router as one opaque `_IncludedRouter` whose `path` is None, so a scan finds no
    /rushvarsel-api route even when the router IS registered and would pass identically either
    way (test_meldinger.py:2029 documents the same trap). /config endpoints need neither BigQuery
    nor the lifespan, which is what makes 404-vs-200 a clean read on registration alone.

    This suite imports main with SHOW_RUSHVARSEL unset, the base helm posture (tst's; dev and prd
    turn it on since 2026-09-28), so what is asserted here is the shipped closed default; the first
    line is what keeps that true if an ambient value ever appears.
    """
    import os

    from fastapi.testclient import TestClient

    assert os.environ.get("SHOW_RUSHVARSEL", "").strip().lower() not in ("true", "1", "yes", "on")
    client = TestClient(main.app)
    # The API: every route, not just the payload one — /config answering 200 would tell a reader
    # the page exists and is merely empty.
    assert client.get("/rushvarsel-api/today").status_code == 404
    assert client.get("/rushvarsel-api/config").status_code == 404
    assert client.get("/rushvarsel-api/outlook", params={"device_id": "d-1"}).status_code == 404
    # The page, on both spellings: a per-request gate inside a mounted StaticFiles would answer
    # the first of these with a 307 to the second, which is why the mount is skipped instead.
    assert client.get("/rushvarsel", follow_redirects=False).status_code == 404
    assert client.get("/rushvarsel/").status_code == 404
    # The old path's redirect too: it would hand a reader the new name.
    assert client.get("/togmelding", follow_redirects=False).status_code == 404
    # And off /docs: nothing under /rushvarsel is in the published schema.
    schema = main.app.openapi()["paths"]
    assert not [p for p in schema if p.startswith(("/rushvarsel", "/togmelding"))], [
        p for p in schema if p.startswith(("/rushvarsel", "/togmelding"))
    ]
    # The gate is about ONE product: the sibling surfaces still answer and are still published.
    assert client.get("/varsel-api/config").status_code == 200
    assert "/varsel-api/config" in schema
    # The page's directory constant is untouched, so flipping the flag needs no other change.
    assert main._TOGMELDING_STATIC_DIR.name == "togmelding_static"


# --- The webhook channel's gate and its (non-)effect on the cycle -------------------


def test_the_notification_webhooks_flag_is_an_allow_list_with_unset_off():
    """U2 scenario 5, the flag half: only true/1/yes/on enable, and UNSET is OFF.

    ONE flag, unlike `_togvarsel_notify_enabled`'s two — rushvarsel has a master product gate over
    three surfaces and the webhook channel has one. What is copied is the parse and the failure
    direction, which is what actually matters: a typo reads as off, and the code default matches
    the closed gate rather than relying on a helm value being present. Read at CALL time, so this
    can set the environment per case without an import dance.
    """
    import os

    with _env(NOTIFICATION_WEBHOOKS_ENABLED=None):
        assert main._notification_webhooks_enabled() is False, "unset must be off"
        for value, want in (
            ("true", True), ("1", True), ("yes", True), ("on", True), ("TRUE", True),
            (" true ", True), ("false", False), ("", False), ("off", False), ("disabled", False),
            ("0", False),
        ):
            os.environ["NOTIFICATION_WEBHOOKS_ENABLED"] = value
            assert main._notification_webhooks_enabled() is want, value


def test_the_delivery_worker_flag_is_an_allow_list_with_unset_off():
    """U11, and its polarity is the interesting half. Same parse as every other flag here -- only
    true/1/yes/on enable, a typo reads as off -- but read in the opposite direction: on means this
    process does LESS. So "off" is a deployment that keeps delivering exactly as it did, which is
    what makes a worker release that never becomes Ready cost nothing.
    """
    import os

    with _env(NOTIFICATION_DELIVERY_WORKER_ENABLED=None):
        assert main.delivery_worker_enabled() is False, "unset must be off, i.e. the poller delivers"
        for value, want in (
            ("true", True), ("1", True), ("yes", True), ("on", True), ("TRUE", True),
            (" true ", True), ("false", False), ("", False), ("off", False), ("disabled", False),
            ("0", False),
        ):
            os.environ["NOTIFICATION_DELIVERY_WORKER_ENABLED"] = value
            assert main.delivery_worker_enabled() is want, value


def test_the_flag_moves_delivery_and_leaves_emit_and_servability_where_they_are():
    """What U11 moves is the POST and the terminal write beside it, and nothing else.

    Emit stays because it is KTD6's one writer of a subject's phase and revision; servability stays
    because it is a BigQuery question this process already holds the client for, and because the
    worker has no BigQuery client at all. Asserted off the source rather than by running a cycle:
    the branch is one `if` inside a phase whose other steps need the whole rebuild around them, and
    a behavioural test would have to build that to observe one call site.
    """
    import ast
    import inspect

    source = inspect.getsource(main._rebuild_all)
    tree = ast.parse(textwrap.dedent(source))
    guarded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and "delivery_worker_enabled" in ast.dump(node.test)
        and "deliver_pending" in ast.dump(node)
    ]
    assert len(guarded) == 1, "delivery is gated exactly once"
    # It is a NOT: the flag says somebody else delivers, so this process skips.
    assert isinstance(guarded[0].test, ast.UnaryOp) and isinstance(guarded[0].test.op, ast.Not)
    # And nothing else in the cycle is behind it -- emit and the servability sweep included.
    body = ast.dump(ast.Module(body=guarded[0].body, type_ignores=[]))
    for stays in ("notification_events", "notification_servability", "sync_mirrors"):
        assert stays not in body, f"{stays} must not move with delivery"


def test_the_webhook_channel_adds_exactly_three_rebuild_phases_around_the_alerting_step():
    """U6: the cycle gains `notification_webhooks`, and AE13's second clause adds `notification_servability`.

    Two halves of this were settled by U2 and by U6 respectively, and the difference matters.

    The client **load** rides inside the existing `varsel_notify` phase, because KTD6 puts it
    immediately before pass 1a — that is what the subscriptions are matched from — and the seam has
    `varsel` calling the store rather than `main` loading it and passing it down; a `main._phase`
    would have to time a call it does not make. Its cost is one `event="varsel_webhook_load"`
    record per cycle.

    The client **emit and deliver** steps are their own phase, after `varsel_notify`. That is
    strictly stronger than KTD6's literal "after `_record_decisions` and `_save_state`": running
    after `check_and_notify` has RETURNED means a raise cannot unwind either of them at all. It
    also gives the Verification Contract's cycle-budget row a real `poller_phase` reading, which is
    the same number KTD6's reversal trigger is stated in.

    The **mirror sync** (U6) is its own phase AHEAD of `varsel_notify`, and both halves of that
    matter. Its own, because the pass reads the mirror from memory: a slow store now costs the
    mirror its freshness rather than costing the pass its cycle, and those are different failures
    with different responses that one phase's `ms` could not tell apart. Ahead, because a mirror
    synced after the pass would serve every cycle the population of the one before.

    The **servability re-check** (R6, AE13's second clause) is a FOURTH phase and not a fold into
    either neighbour. Not into `varsel_recheck_service`, whose analogue it is, because a Firestore
    read inside the browser phase blurs both the timing attribution and KTD6's ordering guarantee.
    And not into `notification_webhooks`, because that phase's `ms` is what KTD6's reversal trigger
    reads: its two steps reserve 28 s against a 30 s trigger, so a third reservation inside it would
    fire the trigger on a healthy cycle.

    Pinned as an exact tuple rather than a containment check, in both directions: a phase silently
    added here is a cycle-timing change nobody asked for, and a phase silently REMOVED is how a
    step stops being timed while `poller_cycle`'s `phases` breakdown still looks complete.
    """
    import inspect
    import re

    source = inspect.getsource(main._rebuild_all)
    phases = tuple(re.findall(r'_phase\(\s*"([a-z_]+)"', source))
    assert phases == (
        "rail_calls",
        # The strip's recorded calls, right after the rebuild that produced them (plan 2026-09-22-001).
        "rail_actuals_cache",
        "disruptions_sql",
        "categories",
        "predict_sql",
        "disruptions_cache",
        "impact_lookup",
        "svv_baselines",
        # Each line's JP transport mode (KIX-23), above `varsel_stops` for the orphan window's reason.
        "line_modes",
        "varsel_stops",
        "rail_planned_calls",
        # The trains Vy took out of today's plan (plan 2026-09-23-001): after the table it
        # anti-joins, before the spine load that reads it.
        "rail_removed_calls",
        # The «I dag» strip's timetable (plan 2026-09-22-001), loaded right after the rebuild that
        # produced it and BEFORE the prediction swap, so it adds nothing to the orphan window below.
        "rail_planned_cache",
        "predict_calls_rail",
        "disruptions_bus_sql",
        "bus_planned_calls",
        "predict_calls_bus",
        "call_predictions_cache",
        "varsel_recheck_service",
        "notification_mirror",
        "varsel_notify",
        # What the pass changed in the /varsel card caches, written for every other pod (U7).
        "varsel_card_cache",
        "notification_webhooks",
        "notification_servability",
        "varsel_state_export",
    ), phases
    # LAST, and the position is the assertion: the export is the one step of the cycle nobody is
    # waiting for. A slow or failing rewrite of the rollback artefact costs the artefact its
    # freshness; anywhere earlier it would cost the alerting pass its cycle.
    assert phases[-1] == "varsel_state_export", phases
    # `varsel_stops` is BEFORE `call_predictions_cache`, and the order is the assertion (2026-09-22).
    # That step stamps `_call_pred_refreshed_at`, which `notification_wording.wording_key` carries,
    # so every cached wording is orphaned the instant it moves and everything between the swap and
    # `varsel_notify` writing the new ones is a window where the in-app card and the journey-alert
    # read serve the deterministic line in place of the model's. Measured on prd over 12 cycles,
    # that window was ~20 s of a ~296 s period and `varsel_stops` was ~8 s of it -- for a step that
    # reads `{rail,bus}_calls_planned_today` straight from BigQuery and touches the prediction
    # snapshot in neither direction. Anything added between these two phases lands in that window,
    # so add it above `varsel_stops` unless it genuinely needs the fresh snapshot.
    assert phases.index("varsel_stops") < phases.index("call_predictions_cache"), phases
    # `notification_mirror` is BEFORE `varsel_notify`, and the order is the assertion. The pass
    # reads the store's mirror, so a mirror synced after it would serve every cycle the population
    # of the one before -- a whole cycle's lag on every create, replace and revocation, with
    # nothing anywhere that looks wrong.
    assert phases.index("notification_mirror") < phases.index("varsel_notify"), phases
    # And the alerting step is still isolated exactly as it was: its own try/except, so a client
    # store outage inside it degrades alerting alone and the feed rebuild above it is untouched.
    # `_phase` re-raises by design (test_phase_does_not_swallow_the_exception), so the try/except
    # around the `with` is the only thing that contains it.
    notify = source[source.index('_phase("varsel_notify"') :]
    assert "except Exception:" in notify and "Avviksvarsel notify failed" in notify
    assert "No-op when VAPID is unconfigured" not in source, (
        "the comment was false before this change and is more so now: scoring, recording and -- "
        "where any channel can deliver -- wording all run without a keypair"
    )


def test_the_webhook_phase_is_gated_guarded_twice_and_fed_by_a_return_value():
    """U6 scenario 8, at the wiring: what contains a client failure, and what feeds the phase.

    Four assertions, and each is about a failure that is silent if it is missing.

    * The phase is gated on `varsel.webhook_channel_enabled()`. Without it every environment with
      no named Firestore database — tst today, and every deployment before the flag is set — logs a
      `NotConfigured` traceback on every cycle from `pending_deliveries`, which is noise that trains
      an operator to ignore this phase.
    * Emit and deliver are guarded SEPARATELY (R20): an emit failure must not cost the deliveries
      already committed on an earlier cycle.
    * Delivery does not depend on `outcome`, so a cycle in which `check_and_notify` raised still
      drains the pending set — which is Firestore's, not this cycle's.
    * The subjects arrive as a RETURN VALUE. A module-level snapshot in `varsel` would need
      registering in `check_and_notify`'s stale sweep and in `test_varsel_api._reset_state`, and a
      leaked half-cycle snapshot reads exactly like the give-up bug those sweeps exist to prevent.
    """
    import inspect

    source = inspect.getsource(main._rebuild_all)
    # Bounded at the NEXT phase, not run to the end of the function. `notification_servability` follows
    # this one and carries a guard of its own, so an unbounded slice would count a fourth
    # `except Exception:` and pass only if the count assertion below were weakened -- which would
    # retire the thing it checks, that the phase's three steps are guarded separately.
    phase = source[
        source.index('_phase("notification_webhooks"') : source.index('_phase("notification_servability"')
    ]
    # The flag is read ONCE for the cycle and both phases gate on that local. Read twice, a broken
    # injected reader wrote the same warning and the same traceback twice per cycle.
    assert source.count("varsel.webhook_channel_enabled()") == 1, source
    assert source.count("if webhook_channel:") == 2, (
        "both notification phases gate on the one read, and neither may run ungated"
    )
    assert source.index("varsel.webhook_channel_enabled()") < source.index(
        '_phase("notification_webhooks"'
    ), "the gate must be outside the phase, or an ungated cycle still times a step it skipped"
    assert source.index("if webhook_channel:") < source.index('_phase("notification_webhooks"'), (
        "same, for the first phase's own gate"
    )
    assert source.rindex("if webhook_channel:") < source.index('_phase("notification_servability"'), (
        "same, for the second phase's own gate"
    )
    assert "outcome = varsel.check_and_notify(" in source, (
        "the notification phase is fed by check_and_notify's return value, not by module state"
    )
    # THREE steps, three guards, since U16 put the mint marks in this phase (KTD4): emit, the
    # decision-log rows saying which subjects were notified, and delivery. Each failure has to cost
    # only its own step -- an event that was committed must not be un-notified by a bookkeeping row,
    # and a decision-log problem must not be reported on a cycle where every event went out.
    assert phase.count("except Exception:") == 3, phase
    assert "Notification event emission failed" in phase
    assert "Recording this cycle's mint marks failed" in phase
    assert "Webhook delivery failed" in phase
    # The mint marks read the emit REPORT, so they are downstream of it and inside its `if outcome
    # is not None` -- a mint cannot exist for a pass that did not run.
    assert phase.index("notification_events.emit(") < phase.index(
        "varsel.record_minted_decisions("
    ), "the mint marks describe what emit committed and cannot precede it"
    deliver = phase[phase.index("webhook_delivery.deliver_pending(") :]
    assert "outcome" not in deliver, (
        "delivery must run on a cycle whose notification pass raised: the pending set is the "
        "store's committed delivery records, not this cycle's decisions"
    )


def test_the_notification_store_is_now_injected_into_varsel_as_the_recipient_provider():
    """U6's seam, asserted at the wiring rather than in prose.

    The mirror of U2's marker test, which asserted the provider was deliberately ABSENT while the
    store was a later unit. Both halves are wired now, and `webhook_channel_enabled()` still
    requires both — so a deployment missing either is the pass that ran before the channel existed.

    Checked over `lifespan`'s source because entering the lifespan starts the poller thread, which
    this suite must never do (it spends real BigQuery money per cycle).
    """
    import inspect

    source = inspect.getsource(main.lifespan)
    configure = source[source.index("varsel.configure(") :]
    configure = configure[: configure.index("llm_message.configure(")]
    assert "webhook_enabled=_notification_webhooks_enabled" in configure
    assert "webhook_recipients_provider=notification_store.recipient_documents" in configure


def test_the_servability_phase_is_gated_guarded_and_last():
    """AE13's second clause at the wiring: what gates it, what contains it, and where it sits.

    Four assertions, each about a failure that is silent without it.

    * It is GATED on `varsel.webhook_channel_enabled()`. Ungated, every deployment that has
      onboarded nobody -- and tst, which has no named Firestore database -- would read the store on
      every cycle and log a `NotConfigured` traceback for an empty population, which is the same
      noise the phase above is gated to avoid. The cost of gating is stated rather than hidden: a
      client onboarded before the flag is set reads the create-time answer with
      `servable_checked_at` publishing its age.
    * It is GUARDED, so a store outage inside it cannot reach the cycle. `_phase` re-raises by
      design, so the try/except around the `with` is the only thing that contains it.
    * It is LAST, after `notification_webhooks`. Ahead of it, a servability re-check that ran long would
      eat the delivery phase's reserved clock; behind it, nothing client-side can unwind the
      browser cycle's durable writes.
    * It names no `outcome`. This phase is fed by the store, not by the notification pass, and
      taking a dependency on the pass's return value would make a cycle whose alerting raised skip
      a re-check that has nothing to do with it.
    """
    import inspect

    source = inspect.getsource(main._rebuild_all)
    phase = source[source.index('_phase("notification_servability"') :]
    assert source.index('_phase("notification_webhooks"') < source.index('_phase("notification_servability"')
    # Two gates over ONE read: the flag is read once for the cycle (a broken injected reader logged
    # its warning and its traceback twice when it was read per phase), and each phase still carries
    # its own `if`, so neither can end up running inside the other's guard.
    gates = [i for i in range(len(source)) if source.startswith("if webhook_channel:", i)]
    assert len(gates) == 2, "each notification phase carries its own gate"
    assert source.count("varsel.webhook_channel_enabled()") == 1, "read once per cycle, not per phase"
    assert gates[1] < source.index('_phase("notification_servability"'), (
        "the gate must be outside the phase, or an ungated cycle still times a step it skipped"
    )
    assert phase.count("except Exception:") == 1, phase
    assert "notification_servability.recheck()" in phase
    assert "Client servability re-check failed" in phase
    assert "outcome" not in phase, (
        "the re-check is fed by the store, not by this cycle's notification pass"
    )


def test_the_servability_seam_is_the_same_one_the_notification_api_answers_from():
    """One seam object for both halves of R6, and one number for the worst case.

    `notification_api._record_servability` answers at create/replace and `notification_servability.recheck`
    re-answers on the cadence; wiring them to two different callables would let the caveat a
    client is shown and the re-check that clears it disagree about the same pair.

    The second half is a parity pin `notification_servability` cannot make itself: its reservation budget
    reserves the seam's own worst case, and the seam's timeout lives in `main`, which a poller-side
    module cannot import. Under-state it and the reservation stops being a ceiling on when the phase
    ends -- which is the whole reason it reserves rather than measures.

    Since U10 the sweep's half is the BATCH form -- one BigQuery job for every pending stop pair
    rather than one per record -- and it names the POLLER budget, which is what keeps it out of the
    traveller-facing 30/min tally (`main._POLLER_CONNECTIVITY_MAX_PER_MIN`). The notification API has
    no connectivity seam at all any more: a create and a replace answer `servable: null` and this
    sweep establishes it.

    Checked over `lifespan`'s source because entering the lifespan starts the poller thread, which
    this suite must never do (it spends real BigQuery money per cycle).
    """
    import inspect

    import notification_api
    import varsel

    source = inspect.getsource(main.lifespan)
    assert "notification_servability.configure(verdicts=varsel._service_verdicts)" in source, (
        "the sweep must take the BATCH seam"
    )
    bound = (
        "connectivity_batch_provider=functools.partial(\n"
        "            _varsel_connectivity_batch, path=_CONNECTIVITY_POLLER_PATH\n"
        "        ),"
    )
    assert bound in source, (
        "both sweeps must spend the POLLER budget, not the traveller-facing one; it is bound at "
        "wiring time because neither of them owns the vocabulary"
    )
    # The signature rather than the call text: the comment beside the call explains why the seam
    # is gone, so matching the WORD would match the explanation.
    assert "connectivity" not in inspect.signature(notification_api.configure).parameters, (
        "a create or replace must not ask BigQuery: `servable` is the sweep's answer now"
    )
    assert notification_servability._WORST_CASE_CHECK_S == main._CONNECTIVITY_TIMEOUT_S
    assert varsel._WORST_CASE_SERVICE_CHECK_S == main._CONNECTIVITY_TIMEOUT_S
    assert notification_servability._DEADLINE_S > notification_servability._WORST_CASE_CHECK_S, (
        "a deadline equal to the worst case admits exactly one check, making the count ceiling "
        "decoration"
    )
    # The two ends of the same bound. A sweep that admitted more records than one job may carry
    # would have its overflow answered as unknown by a job it had already decided to spend -- and
    # those records would stay due with nothing in the logs to say why.
    assert (
        notification_servability._MAX_WRITES_PER_CYCLE <= main._CONNECTIVITY_MAX_PAIRS_PER_JOB
    ), "a cycle may not admit more subscriptions than one connectivity job can answer about"


# ---- The inverted prediction index, built in the ingest loop (U2) --------------------------------


class _PredRow:
    """One `call_predictions_today` row, with every column `_call_pred_from_row` reads."""

    def __init__(self, jkey, situation, stop_place, stop_order, dep):
        self.jkey = jkey
        self.situationNumber = situation  # the BigQuery column's own name
        self.summary = "Signalfeil"
        self.description = None
        self.lineRef = "VYG:Line:R10"  # ditto
        self.stop_place = stop_place
        self.stop_name = None
        self.stop_order = stop_order
        self.aimed_dep = dep
        self.aimed_arr = dep
        self.et_declared_cancelled = False
        self.et_dep_delay_min = None
        self.call_source = "estimated"
        self.p_cancel = 0.2
        for name in (
            "dep_delay_typ_min", "dep_delay_p90_min", "arr_delay_typ_min", "arr_delay_p90_min",
            "p_arr_ge10", "p_arr_ge20", "arr_delay_p50_stack_min", "arr_delay_p90_stack_min",
            "p_impact", "p_dep_ge10", "p_dep_ge20", "dep_delay_p50_stack_min",
            "dep_delay_p90_stack_min", "p_impact_dep",
        ):
            setattr(self, name, None)


class _PredictionClient:
    """Rows for the rail prediction query, nothing for the bus one."""

    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []

    def query(self, sql, *args, **kwargs):
        self.queries.append(sql)
        rows = self.rows if len(self.queries) == 1 else []

        class _Job:
            def result(self_inner, timeout=None):
                return rows

        return _Job()


def _refresh_predictions(rows):
    """Run the real `refresh_call_predictions_cache` over `rows`, restoring every cache after."""
    before = (
        main.bq_client, main._call_pred_by_key, main._call_pred_by_jkey_index,
        main._call_pred_by_situation, main._call_pred_by_stop_ref, main._call_pred_refreshed_at,
    )
    main.bq_client = _PredictionClient(rows)
    try:
        main.refresh_call_predictions_cache()
        return (
            main._impact_snapshot(),
            main._impact_stop_ref_index(),
        )
    finally:
        (
            main.bq_client, main._call_pred_by_key, main._call_pred_by_jkey_index,
            main._call_pred_by_situation, main._call_pred_by_stop_ref,
            main._call_pred_refreshed_at,
        ) = before


def test_the_ingest_loop_indexes_every_call_by_the_stop_it_calls_at():
    """The fourth index (U2), which is what bounds the matcher's scan by the stop pair.

    It has to cover EVERY call, not the ones some filter kept: the prune asks this index whether a
    pair can possibly emit, so a call missing from it is a train the matcher can no longer see —
    and the symptom is the card reading "ingen aktive avvik akkurat nå" with the situation sitting
    in the cache, which is indistinguishable from a calm day.
    """
    now = datetime(2026, 8, 8, 7, 0, tzinfo=UTC)
    rows = [
        _PredRow("j1", "S1", "NSR:StopPlace:1", 1, now),
        _PredRow("j1", "S1", "NSR:StopPlace:2", 2, now + timedelta(minutes=20)),
        _PredRow("j2", "S1", "NSR:StopPlace:1", 1, now + timedelta(minutes=30)),
        _PredRow("j3", "S2", "NSR:StopPlace:3", 1, now),
    ]
    (snapshot, stamp), (index, index_stamp) = _refresh_predictions(rows)
    indexed = [(sit, c.jkey, c.stop_order) for entries in index.values() for sit, c in entries]
    assert sorted(indexed) == sorted(
        (sit, c.jkey, c.stop_order) for sit, calls in snapshot.items() for c in calls
    ), "the index does not cover the snapshot call for call"
    assert set(index) == {"NSR:StopPlace:1", "NSR:StopPlace:2", "NSR:StopPlace:3"}
    assert [c.jkey for _sit, c in index["NSR:StopPlace:1"]] == ["j1", "j2"]
    # The SITUATION travels in the entry, because the snapshot's key is what scopes a scan.
    assert {sit for sit, _c in index["NSR:StopPlace:1"]} == {"S1"}
    assert index_stamp == stamp, "the index and the snapshot must name one generation"


def test_the_index_and_the_snapshot_are_published_together():
    """A second rebuild replaces BOTH under the one lock, so no reader can straddle two loads.

    `impact._stop_ref_index_for` compares the two stamps and falls back to indexing the snapshot
    itself when they differ — a cheap safety net, and this is what keeps it from being the ordinary
    case.
    """
    now = datetime(2026, 8, 8, 7, 0, tzinfo=UTC)
    first = _refresh_predictions([_PredRow("j1", "S1", "NSR:StopPlace:1", 1, now)])
    second = _refresh_predictions([_PredRow("j9", "S9", "NSR:StopPlace:9", 1, now)])
    (snap_a, stamp_a), (index_a, _) = first
    (snap_b, stamp_b), (index_b, stamp_index_b) = second
    assert stamp_b >= stamp_a
    assert set(snap_a) == {"S1"} and set(snap_b) == {"S9"}
    assert set(index_a) == {"NSR:StopPlace:1"} and set(index_b) == {"NSR:StopPlace:9"}
    assert stamp_index_b == stamp_b


def test_the_engine_is_wired_to_the_pollers_index_and_varsel_to_the_heartbeat():
    """Two seams U2 adds at the wiring, each silent if it is dropped.

    Without `stop_ref_index` the engine still works — it indexes the snapshot itself — but the
    O(calls) pass moves off the poller thread and onto whichever request thread arrives first after
    a rebuild, which is the cost this wiring exists to place deliberately.

    Without `beat` the whole match-and-write pass beats `/health` exactly once, at its end, and a
    slow pass reads as a wedged poller. Neither failure shows up in any behaviour test, so both are
    checked at the call site, over `lifespan`'s source — entering the lifespan would start the
    poller thread, which spends real BigQuery money per cycle.
    """
    import inspect

    source = inspect.getsource(main.lifespan)
    engine = source[source.index("impact_engine.configure(") :]
    engine = engine[: engine.index("varsel.configure(")]
    assert "stop_ref_index=_impact_stop_ref_index" in engine, engine
    configure = source[source.index("varsel.configure(") :]
    configure = configure[: configure.index("llm_message.configure(")]
    assert "beat=_beat" in configure, configure


# ---- The journey-status spine (plan 2026-09-22-001, U2) ----------------------------------------


class _Row:
    """A BigQuery row as the spine loader reads it: attributes, and AttributeError for a column the
    table does not carry yet -- which is what `getattr(r, "arr_delay_min", None)` is for."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _planned_row(jkey="j1", order=1, stop="NSR:StopPlace:337"):
    return _Row(jkey=jkey, lineRef="VYG:Line:L1", stop_place=stop, stop_order=order,
                aimed_dep=NOON, aimed_arr=None, dep_delay_min=None, call_cancelled=False,
                call_source="estimated", boarding_allowed=None, alighting_allowed=False)


@contextmanager
def _spine_client(rows=None, *, raises=False):
    import journey_status

    class _Client:
        def __init__(self):
            self.queries: list[str] = []

        def query(self, sql, *a, **k):
            self.queries.append(sql)

            class _Job:
                def result(self_inner, timeout=None):
                    if raises:
                        raise RuntimeError("bigquery is down")
                    return list(rows or [])

            return _Job()

    before = main.bq_client, main._planned_refreshed_at
    client = _Client()
    main.bq_client = client
    try:
        yield client
    finally:
        main.bq_client, main._planned_refreshed_at = before
        journey_status.reset_state()


def test_the_spine_loads_once_per_planned_rebuild_and_is_stamped_with_it():
    import journey_status

    with _spine_client([_planned_row(), _planned_row(order=2, stop="NSR:StopPlace:418")]) as client:
        main._planned_refreshed_at = None
        main.refresh_planned_spine()
        assert client.queries == [], "no rebuild has finished: nothing to load"
        main._planned_refreshed_at = NOON
        main.refresh_planned_spine()
        main.refresh_planned_spine()
        assert client.queries == [main._JOURNEY_STATUS_SPINE_QUERY], "a second tick is a no-op"
        assert journey_status.spine_refreshed_at() == NOON
        (call, _) = journey_status._spine.by_key[("VYG", "j1")]
        # The operator is the codespace, as the prediction cache derives it; NULL activity is
        # permission and FALSE is not; a column the table lacks yet reads None.
        assert (call.operator, call.boarding_allowed, call.alighting_allowed) == ("VYG", True, False)
        assert call.arr_delay_min is None
        main._planned_refreshed_at = NOON + timedelta(hours=1)
        main.refresh_planned_spine()
        assert len(client.queries) == 2, "the next rebuild is loaded"


def test_a_removed_train_loads_into_the_spine_from_the_same_job():
    """Plan 2026-09-23-001: one query reads both tables, so the strip never sees one without the
    other, and a removed row keeps its source, its cancellation and its bus."""
    import journey_status

    removed = _Row(jkey="RemovedJourney:2105_2026-08-08", lineRef="VYG:Line:L1",
                   stop_place="NSR:StopPlace:337", stop_order=1, aimed_dep=NOON, aimed_arr=None,
                   dep_delay_min=None, call_cancelled=True, call_source="removed",
                   boarding_allowed=True, alighting_allowed=True, bus_for_train=True)
    with _spine_client([_planned_row(), removed]) as client:
        main._planned_refreshed_at = NOON
        main.refresh_planned_spine()
        assert client.queries == [main._JOURNEY_STATUS_SPINE_QUERY]
        assert "rail_calls_removed_today" in main._JOURNEY_STATUS_SPINE_QUERY
        (call,) = journey_status._spine.by_key[("VYG", "RemovedJourney:2105_2026-08-08")]
        assert (call.call_source, call.call_cancelled, call.bus_for_train) == ("removed", True, True)
        (planned,) = journey_status._spine.by_key[("VYG", "j1")]
        assert planned.bus_for_train is False


def test_a_failed_spine_load_keeps_the_previous_copy_and_retries_next_tick():
    import journey_status

    with _spine_client([_planned_row()]):
        main._planned_refreshed_at = NOON
        main.refresh_planned_spine()
    with _spine_client(raises=True) as client:
        journey_status.set_spine(journey_status.CallTable([], NOON))
        main._planned_refreshed_at = NOON + timedelta(hours=1)
        try:
            main.refresh_planned_spine()
        except RuntimeError:
            pass
        else:
            raise AssertionError("the loader must raise for _phase to record it")
        assert journey_status.spine_refreshed_at() == NOON, "the old copy is still served"
        try:
            main.refresh_planned_spine()
        except RuntimeError:
            pass
        assert len(client.queries) == 2, "a stamp that did not move means the next tick retries"


def test_the_actuals_load_once_per_actuals_rebuild_every_row_tagged_recorded():
    import journey_status

    rows = [_Row(jkey="j1", lineRef="VYG:Line:L1", stop_place="NSR:StopPlace:337", stop_order=1,
                 aimed_dep=NOON, dep_delay_min=4, call_cancelled=False)]
    before = main._rail_calls_refreshed_at
    try:
        with _spine_client(rows) as client:
            main._rail_calls_refreshed_at = None
            main.refresh_rail_actuals()
            assert client.queries == []
            main._rail_calls_refreshed_at = NOON
            main.refresh_rail_actuals()
            main.refresh_rail_actuals()
            assert client.queries == [main._JOURNEY_STATUS_ACTUALS_QUERY]
            assert journey_status.actuals_refreshed_at() == NOON
            (call,) = journey_status._actuals.by_key[("VYG", "j1")]
            assert (call.call_source, call.dep_delay_min, call.aimed_arr) == ("recorded", 4, None)
        with _spine_client(raises=True):
            journey_status.set_actuals(journey_status.CallTable([], NOON))
            main._rail_calls_refreshed_at = NOON + timedelta(minutes=15)
            try:
                main.refresh_rail_actuals()
            except RuntimeError:
                pass
            assert journey_status.actuals_refreshed_at() == NOON, "the old copy is still served"
    finally:
        main._rail_calls_refreshed_at = before


def test_the_actuals_phase_cannot_abort_the_rebuild():
    import inspect

    source = inspect.getsource(main._rebuild_all)
    block = source[source.index('_phase("rail_calls"') : source.index('_phase("disruptions_sql"')]
    assert '_phase("rail_actuals_cache"' in block and "except Exception:" in block, block

def test_the_spine_phase_is_isolated_from_the_predictions_it_sits_among():
    import inspect

    source = inspect.getsource(main._rebuild_all)
    block = source[source.index('_phase("rail_planned_cache"') - 200 : source.index('_phase("predict_calls_rail"')]
    assert "try:" in block and "except Exception:" in block, block


# ---- The removed-trains table (plan 2026-09-23-001, U1) -----------------------------------------


@contextmanager
def _removed_client(*, raises=False, table_exists=True):
    """bq_client for the removed build: records each query with its parameters by name."""

    class _Client:
        def __init__(self):
            self.builds: list[dict] = []

        def query(self, sql, *a, job_config=None, **k):
            assert sql == main._RAIL_CALLS_REMOVED_QUERY, sql[:60]
            self.builds.append({p.name: p.value for p in job_config.query_parameters})

            class _Job:
                def result(self_inner, timeout=None):
                    if raises:
                        raise RuntimeError("rail_calls_removed_today refused: 900 of 1036")
                    return []

            return _Job()

        def get_table(self, ref):
            assert ref == "disruptions.rail_calls_removed_today", ref
            if not table_exists:
                raise main.NotFound("not there")
            return object()

    before = main.bq_client, main._planned_refreshed_at, main._removed_built_for
    env = os.environ.get("VARSEL_PLAN_REMOVED")
    client = _Client()
    main.bq_client = client
    try:
        yield client
    finally:
        main.bq_client, main._planned_refreshed_at, main._removed_built_for = before
        if env is None:
            os.environ.pop("VARSEL_PLAN_REMOVED", None)
        else:
            os.environ["VARSEL_PLAN_REMOVED"] = env


def test_the_removed_table_is_built_once_per_planned_rebuild_for_the_oslo_day():
    with _removed_client() as client:
        os.environ["VARSEL_PLAN_REMOVED"] = "true"
        main._removed_built_for = None
        main._planned_refreshed_at = None
        main.maybe_refresh_removed_calls()
        assert client.builds == [], "no planned table yet: nothing to anti-join against"
        main._planned_refreshed_at = NOON
        main.maybe_refresh_removed_calls()
        main.maybe_refresh_removed_calls()
        assert len(client.builds) == 1, "a second tick on the same planned table is a no-op"
        assert client.builds[0] == {"d": datetime.now(main._OSLO).date(), "enabled": True}
        main._planned_refreshed_at = NOON + timedelta(hours=1)
        main.maybe_refresh_removed_calls()
        assert len(client.builds) == 2, "the next planned rebuild is followed"


def test_off_builds_the_removed_table_empty_once_and_then_stops():
    with _removed_client() as client:
        os.environ.pop("VARSEL_PLAN_REMOVED", None)
        main._removed_built_for = None
        main._planned_refreshed_at = NOON
        main.maybe_refresh_removed_calls()
        assert client.builds == [{"d": datetime.now(main._OSLO).date(), "enabled": False}]
        main._planned_refreshed_at = NOON + timedelta(hours=1)
        main.maybe_refresh_removed_calls()
        assert len(client.builds) == 1, "the environment cannot change under a running process"


def test_a_refused_removed_build_waits_for_the_next_planned_rebuild():
    with _removed_client(raises=True) as client:
        os.environ["VARSEL_PLAN_REMOVED"] = "true"
        main._removed_built_for = None
        main._planned_refreshed_at = NOON
        try:
            main.maybe_refresh_removed_calls()
        except RuntimeError:
            pass
        else:
            raise AssertionError("the build must raise for _phase to record it")
        # Refused, then emptied: the table the refusal kept was anti-joined against an older ET.
        assert [b["enabled"] for b in client.builds] == [True, False]
        main.maybe_refresh_removed_calls()
        assert len(client.builds) == 2, "a refusal is not retried every tick"
        main._planned_refreshed_at = NOON + timedelta(hours=1)
        try:
            main.maybe_refresh_removed_calls()
        except RuntimeError:
            pass
        assert len(client.builds) == 4


def test_any_other_removed_build_failure_is_retried_next_tick():
    """The table may not exist yet, and predict_calls.sql reads it: a transient failure waiting an
    hour would cost every rail and bus prediction for that hour."""

    class _Transient(Exception):
        pass

    with _removed_client() as client:
        os.environ["VARSEL_PLAN_REMOVED"] = "true"
        main._removed_built_for = None
        main._planned_refreshed_at = NOON
        real = main._build_removed_calls

        def flaky(*, enabled):
            client.builds.append({"enabled": enabled})
            raise _Transient("503 backend error")

        main._build_removed_calls = flaky
        try:
            try:
                main.maybe_refresh_removed_calls()
            except _Transient:
                pass
            assert main._removed_built_for is None, "not stamped: the next tick retries"
        finally:
            main._build_removed_calls = real
        main.maybe_refresh_removed_calls()
        assert main._removed_built_for == NOON and len(client.builds) == 2


def test_the_refusal_the_poller_recognises_is_the_one_the_sql_raises():
    assert main._REMOVED_REFUSAL in main._RAIL_CALLS_REMOVED_QUERY
    assert main._REMOVED_REFUSAL in "rail_calls_removed_today refused: 900 of 1036"  # the stub's


def test_the_removed_build_cannot_abort_the_predictions_after_it():
    import inspect

    source = inspect.getsource(main._rebuild_all)
    block = source[source.index('_phase("rail_planned_calls"') : source.index('_phase("rail_planned_cache"')]
    assert '_phase("rail_removed_calls"' in block and "except Exception:" in block, block


def test_the_removed_table_is_created_empty_only_when_it_is_absent():
    with _removed_client(table_exists=True) as client:
        os.environ["VARSEL_PLAN_REMOVED"] = "true"
        main.ensure_removed_calls_table()
        assert client.builds == []
    with _removed_client(table_exists=False) as client:
        os.environ["VARSEL_PLAN_REMOVED"] = "true"
        main.ensure_removed_calls_table()
        assert [b["enabled"] for b in client.builds] == [False], "empty, whatever the flag says"
    with _removed_client(table_exists=False, raises=True) as client:
        main.ensure_removed_calls_table()  # logged, never raised: startup must not block on it
        assert len(client.builds) == 1


def test_the_removed_sql_takes_the_parameters_the_poller_passes():
    import re

    sql = main._RAIL_CALLS_REMOVED_QUERY
    assert set(re.findall(r"@([a-z_]+)", sql)) == {"d", "enabled"}
    # The holiday guard's source, which copy_runtime_to_prd.sh carries to prd.
    assert "`disruptions.togvarsel_calendar`" in sql
    # The replay swaps the ET CTE and the table name; both must stay findable exactly once.
    assert sql.count("et_trains AS (") == 1
    assert sql.count("CREATE OR REPLACE TABLE `disruptions.rail_calls_removed_today`") == 1


def test_sjn_removals_are_guarded_apart_from_vy():
    # KIX-43: SJ Norge rides the same relay, but its guards are its own. A half-written SJN plan
    # must not ERROR the build (that keeps Vy's previous table too), and F6/F7 close boarding
    # mid-journey, which GTFS cannot say, so they are left out as F4 and RX11 are.
    import re

    sql = re.sub(r"--.*$", "", main._RAIL_CALLS_REMOVED_QUERY, flags=re.MULTILINE)
    assert "FROM UNNEST(['VYG', 'SJN']) AS op" in sql, "one guard row per operator"
    assert "JOIN guard AS g ON g.operator = c.operator" in sql
    assert "AND c.operator = 'VYG' THEN ERROR(" in sql, "only Vy's refusal errors"
    assert "('SJN', 'F6'), ('SJN', 'F7')" in sql
    assert "CONCAT(c.operator, ':Line:', c.line) AS lineRef" in sql


class _Ticks:
    """Duck-types the stop_event: poll_for_changes runs exactly `n` loop iterations."""

    def __init__(self, n: int) -> None:
        self.left = n

    def wait(self, interval: float) -> bool:
        self.left -= 1
        return self.left <= 0


@contextmanager
def _quiet_ticks(script):
    """Drive poll_for_changes hermetically. `script` is one (ingestion_value, monotonic_seconds)
    per tick: the probe stub returns the value and moves the fake clock to that time, and every
    monotonic() read in the loop — the spacing check, the rebuild stamp, `_beat` — sees the same
    frozen `now`, so the test does not depend on how many times the loop consults the clock. The
    per-tick side jobs (togvarsel, outcomes, labelling freshness) are stubbed to no-ops."""
    ticks = iter(script)
    now = [0.0]

    def _probe():
        value, now[0] = next(ticks)
        return value

    before = (
        main.get_last_ingestion_time,
        main.monotonic,
        main.togvarsel.refresh,
        main.togvarsel_notify.evaluate,
        main.togvarsel_outcomes.refresh,
        main.decisions.check_labelling_freshness,
        main._poller_heartbeat_monotonic,
    )
    main.get_last_ingestion_time = _probe
    main.monotonic = lambda: now[0]
    main.togvarsel.refresh = lambda: None
    main.togvarsel_notify.evaluate = lambda: None
    main.togvarsel_outcomes.refresh = lambda: None
    main.decisions.check_labelling_freshness = lambda: None
    try:
        yield
    finally:
        (
            main.get_last_ingestion_time,
            main.monotonic,
            main.togvarsel.refresh,
            main.togvarsel_notify.evaluate,
            main.togvarsel_outcomes.refresh,
            main.decisions.check_labelling_freshness,
            main._poller_heartbeat_monotonic,
        ) = before


def test_a_change_inside_the_rebuild_spacing_is_held_not_lost():
    """Since the probe reads the streaming buffer, the ingestion timestamp moves on almost every
    tick; the floor keeps that from becoming a rebuild per tick. What it must NOT do is drop the
    change: a held tick leaves `last_seen` alone, so the next tick past the floor rebuilds with the
    value it held. Three ticks: t=0 first observation (rebuilds), t=30 a new value inside the
    120 s floor (held), t=200 the same value (rebuilds)."""
    calls: list[str] = []
    with _quiet_ticks([("a", 0.0), ("b", 30.0), ("b", 200.0)]), _spacing(120):
        main.poll_for_changes(calls.append, _Ticks(3))
    assert calls == ["a", "b"], calls


def test_a_zero_spacing_rebuilds_on_every_change():
    calls: list[str] = []
    with _quiet_ticks([("a", 0.0), ("b", 1.0), ("c", 2.0)]), _spacing(0):
        main.poll_for_changes(calls.append, _Ticks(3))
    assert calls == ["a", "b", "c"], calls


def test_an_unchanged_value_never_rebuilds_whatever_the_spacing():
    calls: list[str] = []
    with _quiet_ticks([("a", 0.0), ("a", 1.0), ("a", 2.0)]), _spacing(0):
        main.poll_for_changes(calls.append, _Ticks(3))
    assert calls == ["a"], calls


@contextmanager
def _spacing(seconds: int):
    before = main._MIN_REBUILD_SPACING_S
    main._MIN_REBUILD_SPACING_S = seconds
    try:
        yield
    finally:
        main._MIN_REBUILD_SPACING_S = before


# ---- Following the last build: the startup load and a follower's tick (zero-downtime U3) --------


@contextmanager
def _warm_client(ages, rows=None, *, raises=(), not_found=(), lookups=None):
    """bq_client for `follow_last_build`: `ages` maps a table to how long ago it was built (absent:
    NotFound) and is read at CALL time, so a test moves a table by editing the dict; `rows` maps a
    query to its rows, a query in `raises` fails and one in `not_found` reads a missing table.
    `metadata` records every `get_table` and `timeouts` the timeout each was given.

    The per-pod lookups are replaced by `lookups` (none by default) -- the real ones would load the
    stop picker into `varsel` -- and line modes are held fresh, so nothing here reaches Journey
    Planner."""
    import journey_status
    import line_modes

    now = datetime.now(UTC)

    class _Table:
        def __init__(self, modified):
            self.modified = modified

    class _Client:
        def __init__(self):
            self.queries: list[str] = []
            self.metadata: list[str] = []
            self.timeouts: list[float | None] = []

        def get_table(self, ref, timeout=None):
            self.metadata.append(ref)
            self.timeouts.append(timeout)
            if ref not in ages:
                raise main.NotFound("not there")
            return _Table(now - ages[ref])

        def query(self, sql, *a, **k):
            self.queries.append(sql)

            class _Job:
                def result(self_inner, timeout=None):
                    if sql in not_found:
                        raise main.NotFound("Not found: Table")
                    if sql in raises:
                        raise RuntimeError("Unrecognized name: a column the old table lacks")
                    return list((rows or {}).get(sql, []))

            return _Job()

    names = (
        "bq_client", "_disruptions_cache", "_disruptions_refreshed_at", "_call_pred_by_key",
        "_call_pred_by_jkey_index", "_call_pred_by_situation", "_call_pred_by_stop_ref",
        "_call_pred_refreshed_at", "_planned_refreshed_at", "_rail_calls_refreshed_at",
        "_FOLLOWED_LOOKUPS", "_lookups_built",
    )
    saved = {name: getattr(main, name) for name in names}
    saved_modes = line_modes._fetched_at
    main.bq_client = client = _Client()
    main._disruptions_refreshed_at = main._call_pred_refreshed_at = None
    main._planned_refreshed_at = main._rail_calls_refreshed_at = None
    main._FOLLOWED_LOOKUPS = lookups or {}
    main._lookups_built = {}
    line_modes._fetched_at = now
    journey_status.reset_state()
    try:
        yield client, now
    finally:
        for name, value in saved.items():
            setattr(main, name, value)
        line_modes._fetched_at = saved_modes
        journey_status.reset_state()


_TWO_MIN = timedelta(minutes=2)
_ALL_FRESH = {
    "disruptions.predictions_today": _TWO_MIN,
    "disruptions.call_predictions_today": _TWO_MIN,
    "disruptions.rail_calls_planned_today": _TWO_MIN,
    "disruptions.rail_calls_today": _TWO_MIN,
}
# Every table a follower tick asks the modified time of, once per tick and nothing more.
_FOLLOWED_TABLES = {
    "disruptions.predictions_today",
    "disruptions.call_predictions_today",
    "disruptions.call_predictions_bus_today",
    "disruptions.rail_calls_planned_today",
    "disruptions.rail_calls_removed_today",
    "disruptions.rail_calls_today",
}


def test_a_new_process_serves_the_tables_the_last_one_built_stamped_with_their_age():
    """The deploy gap: without this a new pod answered journey-alerts 503 for ~95 s, until its
    first cycle rebuilt tables that already existed. The stamp is the TABLE's time, so `as_of` and
    `refreshed_at` never claim a snapshot is newer than it is."""
    import journey_status

    rows = {
        main._CALL_PREDICTIONS_QUERY: [_PredRow("j1", "S1", "NSR:StopPlace:1", 1, NOON)],
        main._JOURNEY_STATUS_SPINE_QUERY: [_planned_row()],
        main._JOURNEY_STATUS_ACTUALS_QUERY: [
            _Row(jkey="j1", lineRef="VYG:Line:L1", stop_place="NSR:StopPlace:337", stop_order=1,
                 aimed_dep=NOON, dep_delay_min=4, call_cancelled=False)
        ],
    }
    with _warm_client(_ALL_FRESH, rows) as (_client, now):
        main.follow_last_build()
        built = now - _TWO_MIN
        assert main._varsel_refreshed_at() == built, "the pull read answers, stamped with the table"
        assert set(main._impact_snapshot()[0]) == {"S1"}
        assert main._disruptions_refreshed_at == built
        assert journey_status.spine_refreshed_at() == built
        assert journey_status.actuals_refreshed_at() == built
        # The ET rebuild gates stay unarmed: the first cycle must still rebuild both tables, since
        # the deploy may have changed their SQL -- and then load them over the warm copies.
        assert main._planned_refreshed_at is None and main._rail_calls_refreshed_at is None
        main._planned_refreshed_at = now
        main.refresh_planned_spine()
        assert journey_status.spine_refreshed_at() == now


def test_a_missing_or_unreadable_table_leaves_only_its_own_cache_cold_and_age_is_no_bar():
    """Each load stands alone, and none can fail the lifespan (which would fail readiness and roll
    the release back). And no age bound (KTD6): a live leader on a quiet night leaves tables older
    than ten minutes, and a follower that refused them would serve 503 beside a healthy leader."""
    import journey_status

    ages = {
        # predictions_today absent: a first deploy.
        "disruptions.call_predictions_today": timedelta(minutes=11),
        "disruptions.rail_calls_planned_today": timedelta(minutes=50),
        "disruptions.rail_calls_today": _TWO_MIN,
    }
    with _warm_client(
        ages, {main._JOURNEY_STATUS_SPINE_QUERY: [_planned_row()]},
        raises={main._JOURNEY_STATUS_ACTUALS_QUERY},
    ) as (client, now):
        result = main.follow_last_build()
        assert main._disruptions_refreshed_at is None
        assert main._varsel_refreshed_at() == now - timedelta(minutes=11), "loaded, and says how old"
        assert journey_status.spine_refreshed_at() == now - timedelta(minutes=50)
        assert journey_status.actuals_refreshed_at() is None
        assert client.queries == [
            main._CALL_PREDICTIONS_QUERY, main._CALL_PREDICTIONS_BUS_QUERY,
            main._JOURNEY_STATUS_SPINE_QUERY, main._JOURNEY_STATUS_ACTUALS_QUERY,
        ]
        assert result == {
            "disruptions": "missing", "call_predictions": "loaded", "spine": "loaded",
            "actuals": "error",
        }, result


def test_a_follower_tick_with_nothing_moved_reads_metadata_and_runs_no_query():
    """R13: a follower's BigQuery cost is the metadata reads, until a table actually moves."""
    with _warm_client(_ALL_FRESH) as (client, _now):
        main.follow_last_build()
        loaded_queries = len(client.queries)
        client.metadata.clear()
        result = main.follow_last_build()
        assert len(client.queries) == loaded_queries, client.queries[loaded_queries:]
        assert sorted(client.metadata) == sorted(_FOLLOWED_TABLES), client.metadata
        assert set(result.values()) == {"unchanged"}, result
        # Bounded: one hung metadata read must not hold the tick past what the lease assumes.
        assert set(client.timeouts) == {main._POLLER_METADATA_TIMEOUT_S}, client.timeouts


def test_a_failed_bus_load_serves_nsb_only_and_retries_rather_than_stamping_the_build():
    """A bus table that exists but cannot be read: the load serves what it could (NSB-only, the
    leader's long-standing isolation) and reports "error", so the next tick asks again. Its stamp
    sits strictly between the last full load's and this build's: a new stamp for new contents, and
    never the build's own, which the same-build guard would take for a load already done."""
    ages = dict(_ALL_FRESH)
    ages["disruptions.call_predictions_today"] = timedelta(minutes=6)
    ages["disruptions.call_predictions_bus_today"] = timedelta(minutes=5)
    rows = {main._CALL_PREDICTIONS_QUERY: [_PredRow("j1", "S1", "NSR:StopPlace:1", 1, NOON)]}
    raises: set[str] = set()
    with _warm_client(ages, rows, raises=raises) as (client, now):
        main.follow_last_build()
        full = main._call_pred_refreshed_at
        assert full == now - timedelta(minutes=5)
        ages["disruptions.call_predictions_today"] = timedelta(minutes=1)
        built = now - timedelta(minutes=1)
        raises.add(main._CALL_PREDICTIONS_BUS_QUERY)
        result = main.follow_last_build()
        assert result["call_predictions"] == "error", result
        assert set(main._impact_snapshot()[0]) == {"S1"}, "NSB is still served"
        partial = main._call_pred_refreshed_at
        assert full < partial < built, (full, partial, built)
        # The next tick retries the same build: failing again keeps the same stamp (same tables,
        # same contents), and succeeding stamps the build, after which the guard holds.
        before = len(client.queries)
        assert main.follow_last_build()["call_predictions"] == "error"
        assert client.queries[before:] == [
            main._CALL_PREDICTIONS_QUERY, main._CALL_PREDICTIONS_BUS_QUERY
        ], "retried, not skipped as unchanged"
        assert main._call_pred_refreshed_at == partial
        raises.clear()
        assert main.follow_last_build()["call_predictions"] == "loaded"
        assert main._call_pred_refreshed_at == built
        assert main.follow_last_build()["call_predictions"] == "unchanged"


def test_a_missing_bus_table_is_a_complete_load():
    """Before the first bus predict the bus table does not exist, and NSB-only is the whole build:
    stamped with it, not retried on every tick."""
    with _warm_client(_ALL_FRESH, not_found={main._CALL_PREDICTIONS_BUS_QUERY}) as (_c, now):
        assert main.follow_last_build()["call_predictions"] == "loaded"
        assert main._call_pred_refreshed_at == now - _TWO_MIN


def test_only_the_bus_table_moving_reloads_call_predictions_stamped_with_it():
    """A cache built from two tables is stamped with the newer and reloads when EITHER moved."""
    ages = dict(_ALL_FRESH)
    ages["disruptions.call_predictions_today"] = timedelta(minutes=3)
    ages["disruptions.call_predictions_bus_today"] = timedelta(minutes=5)
    with _warm_client(ages) as (client, now):
        main.follow_last_build()
        assert main._call_pred_refreshed_at == now - timedelta(minutes=3)
        before = len(client.queries)
        ages["disruptions.call_predictions_bus_today"] = timedelta(minutes=1)
        result = main.follow_last_build()
        assert client.queries[before:] == [
            main._CALL_PREDICTIONS_QUERY, main._CALL_PREDICTIONS_BUS_QUERY
        ], "only the call-prediction cache reloads"
        assert main._call_pred_refreshed_at == now - timedelta(minutes=1), "the bus table's time"
        assert result["call_predictions"] == "loaded" and result["spine"] == "unchanged", result


def test_two_loads_whose_source_tables_differ_never_carry_the_same_stamp():
    """The invariant `impact`'s index guard, the figures cache and the wording key all rest on: a
    stamp names ONE set of contents. The spine is read from two tables; each move of either is a new
    load with a strictly newer stamp, and a tick on which neither moved loads nothing."""
    import journey_status

    ages = {
        "disruptions.rail_calls_planned_today": timedelta(minutes=10),
        "disruptions.rail_calls_removed_today": timedelta(minutes=8),
    }
    with _warm_client(ages) as (client, now):
        stamps = []
        for table, age in (
            (None, None),
            ("disruptions.rail_calls_planned_today", timedelta(minutes=6)),
            ("disruptions.rail_calls_removed_today", timedelta(minutes=4)),
            (None, None),
        ):
            if table:
                ages[table] = age
            main.follow_last_build()
            stamps.append(journey_status.spine_refreshed_at())
        loads = client.queries.count(main._JOURNEY_STATUS_SPINE_QUERY)
        assert loads == 3, loads
        assert stamps == [now - timedelta(minutes=m) for m in (8, 6, 4, 4)], stamps


def test_the_leader_stamps_with_the_newer_tables_modified_time_and_translates():
    """The leader loads through the same function as a follower, so two pods holding one table
    generation report one `refreshed_at` (R7) -- the load instant the leader used to stamp would
    differ from every follower's and move backwards under round-robin. Only the leader's load asks
    for translations (KTD12); a follower still swaps in the operator's own English."""
    import inspect

    import situation_english

    ages = {
        "disruptions.call_predictions_today": timedelta(minutes=3),
        "disruptions.call_predictions_bus_today": timedelta(minutes=1),
    }
    asked = []
    real = situation_english.update
    situation_english.update = lambda texts, *, translate=True: asked.append(translate)
    try:
        with _warm_client(ages) as (_client, now):
            main.follow_last_build()
            assert asked == [False], "a follower's load translates nothing"
            ages["disruptions.call_predictions_today"] = timedelta(seconds=10)
            assert main._load_from_last_build("call_predictions", leader=True) == "loaded"
            assert main._call_pred_refreshed_at == now - timedelta(seconds=10)
            assert asked == [False, True]
    finally:
        situation_english.update = real
    source = inspect.getsource(main._rebuild_all)
    for name in ("disruptions", "call_predictions", "spine", "actuals"):
        assert f'_load_from_last_build("{name}", leader=True)' in source, name
    for bare in ("refresh_disruptions_cache()", "refresh_call_predictions_cache()",
                 "refresh_planned_spine()", "refresh_rail_actuals()"):
        assert bare not in source, f"the leader still stamps {bare} with the load instant"


def test_a_per_pod_lookup_reloads_only_when_its_table_moved():
    ages = {"disruptions.some_lookup": timedelta(hours=5)}
    loads = []
    lookups = {"some_lookup": (("disruptions.some_lookup",), lambda: loads.append(1) or True)}
    with _warm_client(ages, lookups=lookups):
        main.follow_last_build()
        main.follow_last_build()
        assert len(loads) == 1
        ages["disruptions.some_lookup"] = timedelta(minutes=1)
        assert main.follow_last_build()["some_lookup"] == "loaded"
        assert len(loads) == 2


def test_a_failed_lookup_load_is_retried_next_tick_and_stamped_only_once_it_succeeds():
    """The stop list's table moves once a day; a follower whose load failed (a sub-query down, the
    registry unreadable) must not wait for it. A loader that swallows its failure says so by
    returning False, and a raise counts the same."""
    ages = {"disruptions.some_lookup": timedelta(hours=5)}
    answers = [False, RuntimeError("down"), True]
    calls = []

    def load():
        calls.append(1)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with _warm_client(ages, lookups={"some_lookup": (("disruptions.some_lookup",), load)}):
        outcomes = [main.follow_last_build()["some_lookup"] for _ in range(4)]
        assert outcomes == ["error", "error", "loaded", "unchanged"], outcomes
        assert len(calls) == 3, "retried every tick until it succeeded, then not again"
        assert "some_lookup" in main._lookups_built


def test_the_follower_lookups_are_the_per_pod_load_set():
    """KTD6's list; the rush hydrate is `_rush_hydrate`'s (day-keyed, below). Line modes have no
    table and keep their own 24 h TTL, so they are asked on every tick and not listed here."""
    import inspect

    assert set(main._FOLLOWED_LOOKUPS) == {"impact_lookup", "svv_baselines", "varsel_stops"}
    # Every call, un-gated by any table: main hands the hook over and the mechanism calls it.
    import poller_follow

    assert "refresh_line_modes=refresh_line_modes" in inspect.getsource(main.follow_last_build)
    assert "refresh_line_modes()" in inspect.getsource(poller_follow.follow_last_build)
    # A follower's stop list publishes the aliases and rewrites no stored registration.
    asked = []
    real = main.refresh_varsel_stops
    main.refresh_varsel_stops = lambda **kw: asked.append(kw) or True
    try:
        main._FOLLOWED_LOOKUPS["varsel_stops"][1]()
    finally:
        main.refresh_varsel_stops = real
    assert asked == [{"heal_registrations": False}], asked
    assert "refresh_varsel_stops()" in inspect.getsource(main._rebuild_all), "the leader heals"


def test_line_modes_refresh_on_a_follower_after_the_ttl_and_not_before():
    import alternatives
    import line_modes

    asked = []

    def post(url, payload, headers):
        asked.append(payload["query"])
        return {"data": {"lines": [{"id": "RUT:Line:5", "transportMode": "metro"}]}}

    real = alternatives._post_json
    alternatives._post_json = post
    try:
        with _warm_client({}) as (_client, now):
            line_modes._fetched_at = now - line_modes.TTL + timedelta(minutes=1)
            main.follow_last_build()
            assert asked == [], "inside the TTL"
            line_modes._fetched_at = now - line_modes.TTL - timedelta(seconds=1)
            main.follow_last_build()
            assert len(asked) == 1, "past the TTL"
    finally:
        alternatives._post_json = real
        line_modes.reset()


def test_the_lifespan_runs_the_follower_load_set_before_the_poller_starts():
    """After it, the first cycle's loads could land first and be overwritten by an older table; and
    a pod must answer every route, not only the two `/ready` checks, before it is Ready (KTD6). The
    lookups are no longer called on their own in the lifespan: `follow_last_build` is the one place,
    or the first follower tick would load each of them a second time."""
    import inspect

    source = inspect.getsource(main.lifespan)
    assert source.index("follow_last_build()") < source.index("thread.start()")
    assert source.index("varsel.load_state()") < source.index("follow_last_build()")
    assert source.index("reisevalg.configure(") < source.index("follow_last_build()")
    for direct in ("refresh_impact_lookup()", "refresh_varsel_stops()", "refresh_baselines()"):
        assert direct not in source, direct


def test_follow_last_build_follows_the_stored_translations_and_returns_the_load_outcomes():
    """Every follower tick and the lifespan read the leader's translations through here; deleting
    the call would leave a follower serving only what it translated itself, and pass silently."""
    import poller_follow
    import situation_english

    sentinel = {"call_predictions": "loaded"}
    follows = []
    saved = (poller_follow.follow_last_build, situation_english.follow, main.varsel.follow_card_caches)
    poller_follow.follow_last_build = lambda *a, **k: sentinel
    situation_english.follow = lambda **k: follows.append(("translations", k)) or 0
    # The /varsel card caches (U7) likewise: without it a follower's card shows the SX summary.
    main.varsel.follow_card_caches = lambda **k: follows.append(("cards", k))
    try:
        assert main.follow_last_build() is sentinel
    finally:
        poller_follow.follow_last_build, situation_english.follow, main.varsel.follow_card_caches = saved
    assert follows == [("translations", {}), ("cards", {})], follows


def test_the_lifespan_configures_the_translation_store_before_the_first_follow():
    """The store exists only behind the `/varsel` store backend (tst has none), and the lifespan's
    `follow_last_build()` is the first follow: configured after it, a new pod starts with nothing."""
    import inspect

    source = inspect.getsource(main.lifespan)
    configure = "situation_english.configure(store=notification_store if _varsel_store is not None else None)"
    assert configure in source
    assert source.index(configure) < source.index("follow_last_build()")
    cards = "varsel.configure_card_store(notification_store if _varsel_store is not None else None)"
    assert cards in source
    assert source.index(cards) < source.index("follow_last_build()")


def test_the_card_caches_are_written_after_the_pass_and_only_by_a_leader():
    """U7: the write follows `varsel_notify` (it writes what the pass changed), in its own phase,
    and behind the lease -- a leader demoted mid-pass must not write over its successor's."""
    import inspect

    source = inspect.getsource(main._rebuild_all)
    notify = source.index('_phase("varsel_notify"')
    gate = source.index("if _still_leading():", notify)
    phase = source.index('_phase("varsel_card_cache"')
    assert notify < gate < phase < source.index('_phase("notification_webhooks"')
    assert "varsel.flush_card_writes()" in source[phase:phase + 200]


class _StrictRow:
    """A row carrying ONLY the given columns (all None). Anything else raises AttributeError, as a
    BigQuery row from an older leader's table would."""

    def __init__(self, columns):
        self._columns = set(columns)

    def __getattr__(self, name):
        if name not in self.__dict__.get("_columns", ()):
            raise AttributeError(name)


# Read as `r.<col>` (strictly) by the row readers as of 2026-10-13. A column added to a prediction
# table after that date must be read with getattr and a default (KTD13), because a new follower can
# load a table the old leader built; adding it to these lists instead is the mistake to avoid.
_CALL_PRED_COLUMNS_2026_10_13 = (
    "situationNumber", "summary", "description", "jkey", "lineRef", "stop_place", "stop_name",
    "stop_order", "aimed_dep", "aimed_arr", "et_declared_cancelled", "et_dep_delay_min",
    "call_source", "p_cancel", "dep_delay_typ_min", "dep_delay_p90_min", "arr_delay_typ_min",
    "arr_delay_p90_min", "p_arr_ge10", "p_arr_ge20", "arr_delay_p50_stack_min",
    "arr_delay_p90_stack_min", "p_impact", "p_dep_ge10", "p_dep_ge20", "dep_delay_p50_stack_min",
    "dep_delay_p90_stack_min", "p_impact_dep",
)
_DISRUPTION_COLUMNS_2026_10_13 = (
    "situationNumber", "summary", "description", "category", "onset_ts", "reopen_ts",
    "num_affected_stops", "declared_window_min", "pred_reopen_min", "reopen_p50_min",
    "reopen_p80_min", "reopen_p90_min", "pred_impact_min", "impact_p50_min", "impact_p80_min",
    "impact_p90_min",
)


def test_a_prediction_row_from_an_older_leader_loads_with_defaults():
    """KTD13's rollout contract on the reading side. A guard, not a red: no column has been added
    since 2026-10-13, so this passes today and fails the day a reader adds `r.new_column`."""
    pred = main._call_pred_from_row(_StrictRow(_CALL_PRED_COLUMNS_2026_10_13), "VYG")
    assert pred.has_situation is True and pred.report_type is None and pred.boarding_allowed is True
    cached = main._row_to_cached(_StrictRow(_DISRUPTION_COLUMNS_2026_10_13))
    assert cached.num_affected_stops == 0



# ---- The lease gates the loop (zero-downtime U2, KTD2/KTD4) ---------------------------------------
#
# Pinned off the SOURCE before the loop body moved (the plan's execution note): a later edit that
# slid a writer above its check, or the probe above the lease tick, would still pass every
# behavioural test that happens not to lose the lease at exactly that line.


def test_the_loop_ticks_the_lease_before_it_reads_anything_and_branches_on_it():
    import inspect

    source = inspect.getsource(main.poll_for_changes)
    poll = source[source.index("while True:") :]  # the body; the docstring names the probe too
    tick = poll.index("_poller_lease.tick()")
    held = poll.index("_poller_lease.held()")
    assert tick < held < poll.index("get_last_ingestion_time()") < poll.index("on_change(current)")
    # The follower branch is decided by the same answer and leaves the iteration before the probe.
    follow = poll.index("follow_last_build()")
    assert held < follow < poll.index("continue", follow) < poll.index("get_last_ingestion_time()")


def test_every_tick_writer_is_behind_its_own_lease_check():
    """KTD2: the four tick writers are leader-only, each checked at its own boundary, so a leader
    demoted between two of them runs none of the rest."""
    import inspect

    source = inspect.getsource(main.poll_for_changes)
    poll = source[source.index("while True:") :]
    writers = (
        "togvarsel.refresh()",
        "togvarsel_notify.evaluate()",
        "togvarsel_outcomes.refresh()",
        "decisions.check_labelling_freshness()",
    )
    previous = poll.index("on_change(current)")
    for writer in writers:
        at = poll.index(writer)
        check = poll.rindex("_fence(", 0, at)
        assert previous < check < at, f"{writer} has no lease check of its own before it"
        previous = at


def test_the_phase_checks_the_lease_before_it_times_anything():
    """One call at the top of `_phase`, so a phase added later cannot forget it, and before the
    clock starts, so a skipped phase emits no record, no metric and no beat."""
    import inspect

    source = inspect.getsource(main._phase)
    assert source.index("_fence(name)") < source.index("started = monotonic()")
    assert source.index("_fence(name)") < source.index("try:")


def test_a_fenced_cycle_is_caught_once_before_it_stamps_freshness():
    import inspect

    source = inspect.getsource(main._on_ingestion_change)
    assert source.index("except _PollerFenced") < source.index("metrics.POLLER_LAST_CYCLE.set(")
    assert issubclass(main._PollerFenced, BaseException)
    assert not issubclass(main._PollerFenced, Exception), "a per-step except would swallow it"

# ---- the rush hydrate (zero-downtime U4, KTD8) ------------------------------------------------


@contextmanager
def _rush(ages, *, notify=False, load=None):
    """`_rush_hydrate` against `_warm_client`'s tables, with `togvarsel.load_issued` recorded (or
    replaced by `load`) and the station fetch reaching a counting fake through the REAL
    `station_set`. `evaluate` raises if anything reaches it: the hydrate is read-only."""
    loaded: list = []
    station_queries: list[str] = []

    class _StationsBQ:
        def query(self, sql, *a, **k):
            station_queries.append(sql)

            class _Job:
                def result(self_inner, timeout=None):
                    return [{"stop_place": "NSR:StopPlace:337"}]

            return _Job()

    def evaluate(*a, **k):
        raise AssertionError("the hydrate must never evaluate a round")

    tv, tvn = main.togvarsel, main.togvarsel_notify
    saved = (tv.load_issued, tvn.evaluate, tvn._bq_client, dict(tvn._station_state),
             main._rush_loaded, main._togvarsel_notify_enabled, main.varsel.push_enabled)
    tv.load_issued = load or (lambda d: loaded.append(d) or True)
    tvn.evaluate = evaluate
    tvn._bq_client = _StationsBQ()
    tvn._station_state.clear()
    main._rush_loaded = None
    main._togvarsel_notify_enabled = lambda: notify
    main.varsel.push_enabled = lambda: notify
    try:
        with _warm_client(ages):
            yield loaded, station_queries
    finally:
        (tv.load_issued, tvn.evaluate, tvn._bq_client, state, main._rush_loaded,
         main._togvarsel_notify_enabled, main.varsel.push_enabled) = saved
        tvn._station_state.clear()
        tvn._station_state.update(state)


def _oslo(day: int, hour: int = 7):
    from zoneinfo import ZoneInfo

    return datetime(2026, 8, day, hour, 10, tzinfo=ZoneInfo("Europe/Oslo"))


def test_the_rush_hydrate_reloads_when_a_table_moves_or_the_oslo_day_turns():
    """A follower's tick costs a metadata read while nothing moved (R13). The day turning is a
    reload on its own: `load_issued` is per Oslo day, and the issued table need not move at
    midnight for yesterday's forecasts to stop being today's."""
    from datetime import date

    ages = {"disruptions.togvarsel_issued": timedelta(hours=2)}
    with _rush(ages) as (loaded, _q):
        main._rush_hydrate(now=_oslo(30))
        main._rush_hydrate(now=_oslo(30))
        assert loaded == [date(2026, 8, 30)], loaded
        ages["disruptions.togvarsel_issued"] = timedelta(minutes=1)  # the leader recorded
        main._rush_hydrate(now=_oslo(30))
        assert len(loaded) == 2
        main._rush_hydrate(now=_oslo(31, hour=0))
        assert loaded[-1] == date(2026, 8, 31) and len(loaded) == 3
        main._rush_hydrate(now=_oslo(31), force=True)  # on_acquire: whatever the stamp says
        assert len(loaded) == 4


def test_a_failed_rush_load_is_retried_next_tick_and_never_raises():
    """`_on_lease_acquired` calls this too, and an acquisition must not fail on it: a new leader
    with stale rush state still reads the table back after its first recorded issue."""
    answers = [RuntimeError("bigquery down"), True]

    def load(d):
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    with _rush({"disruptions.togvarsel_issued": timedelta(hours=2)}, load=load):
        main._rush_hydrate(now=_oslo(31))
        assert main._rush_loaded is None, "unstamped after a failure"
        main._rush_hydrate(now=_oslo(31))
        assert main._rush_loaded is not None and answers == []


def test_no_rush_table_means_no_rush_query():
    with _rush({}) as (loaded, _q):
        main._rush_hydrate(now=_oslo(31))
    assert loaded == []


def test_the_station_set_is_read_once_per_oslo_day_on_any_pod_and_only_behind_the_gates():
    """The rush outlook reads the station cache and never queries, so every pod warms it — once
    per Oslo day — behind the same two gates `_evaluate` passes before it fetches (the delivery
    flag and VAPID), and without ever evaluating a round."""
    import togvarsel_notify

    with _rush({}, notify=False) as (_loaded, queries):
        main._rush_hydrate(now=_oslo(31))
        assert queries == [] and togvarsel_notify.cached_station_set(_oslo(31)) is None
    with _rush({}, notify=True) as (_loaded, queries):
        for hour in (7, 8, 9):
            main._rush_hydrate(now=_oslo(31, hour=hour))
        assert len(queries) == 1
        assert togvarsel_notify.cached_station_set(_oslo(31)) == {"NSR:StopPlace:337"}
        main._rush_hydrate(now=_oslo(31, hour=23).replace(day=31) + timedelta(hours=2))
        assert len(queries) == 2, "the next Oslo day fetches again"


def test_the_lifespan_hydrates_and_the_notify_chain_is_wired_to_the_store_and_the_lease():
    import inspect

    lifespan = inspect.getsource(main.lifespan)
    follow = lifespan.index("    follow_last_build()")
    assert lifespan.index("    _rush_hydrate()", follow) > follow, "before the port binds"
    assert "should_continue=_still_leading" in lifespan
    assert "device_journeys=varsel.device_journeys" in lifespan



# ---- Live features read the planned table's clock, not the wall clock ----------------------------


def _cte(sql: str, name: str) -> str:
    start = sql.index(f"\n{name} AS (")
    return sql[start : sql.index("\n),", start)]


def test_live_features_are_measured_from_when_the_planned_table_was_built():
    """The planned tables are rebuilt on a TTL (an hour for rail), so "the last hour" and the ask
    horizon measured from CURRENT_TIMESTAMP() read every stop passed since the build as unobserved.
    The model learned an unobserved earlier stop as "its earlier calls were cancelled", and live
    p_cancel ran far above what happened (2026-10-13). Both live files must anchor on `built_at`."""
    from pathlib import Path

    here = Path(main.__file__).parent
    for predict, planned in (
        ("predict_calls.sql", "rail_calls_planned_today"),
        ("predict_calls_bus.sql", "bus_calls_planned_today"),
    ):
        sql = (here / predict).read_text()
        assert "MAX(built_at)" in _cte(sql, "asof") and planned in _cte(sql, "asof"), predict
        assert "CURRENT_TIMESTAMP()" not in _cte(sql, "linenow"), predict
        assert "(SELECT t FROM asof)" in _cte(sql, "linenow"), predict
        bucket = sql[sql.index("CASE", sql.index("\ncalls AS (")) : sql.index("AS lead_bucket")]
        assert "CURRENT_TIMESTAMP()" not in bucket and bucket.count("(SELECT t FROM asof)") == 4, predict
        assert "CURRENT_TIMESTAMP() AS built_at" in (here / f"{planned}.sql").read_text(), planned

if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nAll poller refresh checks passed.")
