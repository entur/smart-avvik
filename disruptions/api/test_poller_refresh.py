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
    """With SHOW_RUSHVARSEL off, nothing under /togmelding answers on main's own app.

    Both halves are gated at IMPORT time, unlike every other read of this flag, and for two
    different reasons. The page: a Mount cannot be conditioned per request without first
    answering the bare /togmelding with a 307 to the trailing slash, so it is gated by not being
    mounted. The API: a router that is registered but 404s would still publish its endpoint names
    on /docs (FastAPI's default docs_url), and a listing is itself a mention of a product we are
    not showing — hence the schema assertion, which is the only one of these that /docs reflects.

    Probed by ASKING rather than by scanning app.routes: this FastAPI version keeps an included
    router as one opaque `_IncludedRouter` whose `path` is None, so a scan finds no
    /togmelding-api route even when the router IS registered and would pass identically either
    way (test_meldinger.py:2029 documents the same trap). /config endpoints need neither BigQuery
    nor the lifespan, which is what makes 404-vs-200 a clean read on registration alone.

    This suite imports main with SHOW_RUSHVARSEL unset, the posture every deployment is in today,
    so what is asserted here is the shipped behaviour; the first line is what keeps that true if
    an ambient value ever appears.
    """
    import os

    from fastapi.testclient import TestClient

    assert os.environ.get("SHOW_RUSHVARSEL", "").strip().lower() not in ("true", "1", "yes", "on")
    client = TestClient(main.app)
    # The API: every route, not just the payload one — /config answering 200 would tell a reader
    # the page exists and is merely empty.
    assert client.get("/togmelding-api/today").status_code == 404
    assert client.get("/togmelding-api/config").status_code == 404
    assert client.get("/togmelding-api/outlook", params={"device_id": "d-1"}).status_code == 404
    # The page, on both spellings: a per-request gate inside a mounted StaticFiles would answer
    # the first of these with a 307 to the second, which is why the mount is skipped instead.
    assert client.get("/togmelding", follow_redirects=False).status_code == 404
    assert client.get("/togmelding/").status_code == 404
    # And off /docs: nothing under /togmelding is in the published schema.
    schema = main.app.openapi()["paths"]
    assert not [p for p in schema if p.startswith("/togmelding")], [
        p for p in schema if p.startswith("/togmelding")
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
        "disruptions_sql",
        "categories",
        "predict_sql",
        "disruptions_cache",
        "impact_lookup",
        "svv_baselines",
        "varsel_stops",
        "rail_planned_calls",
        "predict_calls_rail",
        "disruptions_bus_sql",
        "bus_planned_calls",
        "predict_calls_bus",
        "call_predictions_cache",
        "varsel_recheck_service",
        "notification_mirror",
        "varsel_notify",
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nAll poller refresh checks passed.")
