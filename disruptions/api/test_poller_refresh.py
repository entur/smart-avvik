"""Checks for the poller's TTL-gated actuals rebuild (main.maybe_refresh_rail_calls).

Hermetic: the BigQuery client is stubbed before main is imported, and the app's lifespan is never
entered — so no poller starts and nothing here touches BigQuery or the network. The stub records
every query it is handed, which is the whole point: what is under test is *whether* the scan runs,
not what it returns.

rail_calls_today is the most expensive query the poller issues (a CREATE OR REPLACE over the whole
day-so-far ET partition, 3.4 GB by late evening), so the gate that keeps it from running on every
SX ingestion change is worth pinning down. The date-rollover case especially: the table holds one
UTC day, so "fresh" cannot be a pure elapsed-time question.

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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nAll poller refresh checks passed.")
