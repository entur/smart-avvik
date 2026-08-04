"""Checks for main._varsel_connectivity — "does a planned trip run between these stops today?".

Hermetic: the BigQuery client is stubbed before main is imported and every query is answered from a
canned row, so nothing here touches BigQuery or the network.

This is the guard-rail behind the registration check, and the thing it must never do is claim "no
service" when it simply could not ask: that would refuse or warn about journeys that are fine. Every
failure path here therefore returns None, which the caller treats as "unknown", not as "no".

Run: uv run --with fastapi --with pydantic --with google-cloud-bigquery python test_varsel_service.py
"""

import os
import tempfile
from types import SimpleNamespace

# --- Stub BigQuery + pin the state file BEFORE importing main/varsel (both act at import). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]
_STATE_FD, _STATE_PATH = tempfile.mkstemp(prefix="varsel_service_state_", suffix=".json")
os.close(_STATE_FD)
os.environ["VARSEL_STATE_PATH"] = _STATE_PATH

import main


class _FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self, *args, **kwargs):
        if isinstance(self._rows, Exception):
            raise self._rows
        return list(self._rows)


class _FakeBQ:
    """Records every query it is asked, and answers with a canned row (or raises)."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[dict] = []

    def query(self, sql, job_config=None, *args, **kwargs):
        params = {p.name: list(p.values) for p in (job_config.query_parameters if job_config else [])}
        self.calls.append({"sql": sql, "params": params})
        return _FakeJob(self.rows)


def _row(forward, reverse):
    return [SimpleNamespace(forward=forward, reverse=reverse)]


def _install(fake, monkeypatch):
    monkeypatch.setattr(main, "bq_client", fake)
    main._connectivity_cache.clear()
    main._connectivity_calls.clear()


def test_both_directions_are_reported(monkeypatch):
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) == (True, False)
    assert fake.calls[0]["params"] == {"from_refs": ["A"], "to_refs": ["B"]}


def test_every_ref_of_a_place_is_passed_through(monkeypatch):
    """The caller hands over all refs of a merged stop; they must reach the query as arrays, or the
    check would report "no service" for a trip recorded on the other ref."""
    fake = _FakeBQ(_row(True, True))
    _install(fake, monkeypatch)
    main._varsel_connectivity(["A", "A2"], ["B", "B2"])
    assert fake.calls[0]["params"] == {"from_refs": ["A", "A2"], "to_refs": ["B", "B2"]}


def test_no_shared_trip_is_a_negative_not_an_unknown(monkeypatch):
    """An empty result set means the join found nothing — a real "these stops are not connected",
    unlike a failure, which must stay None."""
    fake = _FakeBQ([])
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) == (False, False)


def test_a_query_failure_is_unknown_not_no_service(monkeypatch):
    fake = _FakeBQ(RuntimeError("BigQuery is down"))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) is None


def test_a_repeated_pair_is_answered_from_cache(monkeypatch):
    """A client retrying a rejected registration must not cost a query per attempt."""
    fake = _FakeBQ(_row(False, True))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) == (False, True)
    assert main._varsel_connectivity(["A"], ["B"]) == (False, True)
    assert len(fake.calls) == 1


def test_the_cache_key_ignores_ref_order(monkeypatch):
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    main._varsel_connectivity(["A2", "A"], ["B"])
    main._varsel_connectivity(["A", "A2"], ["B"])
    assert len(fake.calls) == 1


def test_a_failure_is_not_cached(monkeypatch):
    """Caching "unknown" would freeze the check off for the whole TTL after a single hiccup."""
    fake = _FakeBQ(RuntimeError("transient"))
    _install(fake, monkeypatch)
    assert main._varsel_connectivity(["A"], ["B"]) is None
    fake.rows = _row(True, False)
    assert main._varsel_connectivity(["A"], ["B"]) == (True, False)


def test_distinct_pairs_are_rate_limited(monkeypatch):
    """/varsel-api is unauthenticated and the cache cannot bound a caller inventing pairs. Past the
    ceiling the check reports unknown — registrations still work, unvalidated."""
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    for i in range(main._CONNECTIVITY_MAX_PER_MIN):
        assert main._varsel_connectivity([f"A{i}"], ["B"]) == (True, False)
    assert main._varsel_connectivity(["A-over-the-limit"], ["B"]) is None
    assert len(fake.calls) == main._CONNECTIVITY_MAX_PER_MIN


def test_the_rate_limit_does_not_apply_to_cached_pairs(monkeypatch):
    """Otherwise a busy minute would start returning unknown for pairs already known."""
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    main._varsel_connectivity(["A"], ["B"])
    main._connectivity_calls[:] = [main._connectivity_calls[0]] * main._CONNECTIVITY_MAX_PER_MIN
    assert main._varsel_connectivity(["A"], ["B"]) == (True, False)
    assert main._varsel_connectivity(["C"], ["D"]) is None


def test_the_query_asks_both_call_sources(monkeypatch):
    """Rail and bus planned calls live in separate tables; a rail-only check would warn about every
    bus and ferry journey ever registered."""
    fake = _FakeBQ(_row(True, False))
    _install(fake, monkeypatch)
    main._varsel_connectivity(["A"], ["B"])
    sql = fake.calls[0]["sql"]
    assert "rail_calls_planned_today" in sql and "bus_calls_planned_today" in sql


class _MonkeyPatch:
    """Minimal monkeypatch shim so the tests run standalone (without pytest)."""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        mp = _MonkeyPatch()
        try:
            t(mp)
        finally:
            mp.undo()
        print(f"  ok  {t.__name__}")
    os.unlink(_STATE_PATH)
    print(f"\n{len(tests)} planned-service checks passed.")
