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

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import main


class _RecordingClient:
    """Stands in for bq_client: records each query and returns an object with .result()."""

    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sql: str, *args, **kwargs):
        self.queries.append(sql)

        class _Job:
            def result(self_inner):
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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nAll poller refresh checks passed.")
