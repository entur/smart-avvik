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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nAll poller refresh checks passed.")
