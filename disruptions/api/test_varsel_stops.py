"""Checks for the stop picker list: one row per physical place, county labels, fail-open grouping.

Hermetic: the BigQuery client is stubbed before main is imported and every query is answered from
canned rows, so nothing here touches BigQuery or the network.

Why this file exists: two StopPlaces named "Nyvoll ferjekai" were offered as two identical picker
rows, and the one a traveller happened to pick was not the quay their ferry called at. `to_stop`
narrowing then dropped every situation and the journey card reported "ingen aktive avvik akkurat nå"
— a silent, permanent no-op that looked like calm weather. The registry knows those refs are one
place (a shared `parentRef`); this is the test that we use that knowledge.

Run: uv run --with fastapi --with httpx --with pydantic python test_varsel_stops.py  (or pytest)
"""

import os
import tempfile
from types import SimpleNamespace

# --- Stub BigQuery + pin the state file BEFORE importing main/varsel (both act at import). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]
_STATE_FD, _STATE_PATH = tempfile.mkstemp(prefix="varsel_stops_state_", suffix=".json")
os.close(_STATE_FD)
os.environ["VARSEL_STATE_PATH"] = _STATE_PATH

import main
import varsel


class _FakeQueryJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self, *args, **kwargs):
        if isinstance(self._rows, Exception):
            raise self._rows
        return list(self._rows)


class _FakeBQ:
    """Answers each of refresh_varsel_stops' three sub-queries from canned rows.

    Dispatch is on a fragment of the query text rather than call order: the order the sub-queries run
    in is an implementation detail, and keying on it would make this suite pass for the wrong reason
    if that changed.
    """

    def __init__(self, rail=(), bus=(), registry=()):
        self.by_source = {"rail_calls_planned_today": rail,
                          "bus_calls_planned_today": bus,
                          "national_stop_registry": registry}

    def query(self, sql, *args, **kwargs):
        for marker, rows in self.by_source.items():
            if marker in sql:
                return _FakeQueryJob(rows)
        raise AssertionError(f"unexpected query: {sql[:80]}")


def _rail(stop_place, stop_name, n_calls):
    return SimpleNamespace(stop_place=stop_place, stop_name=stop_name, n_calls=n_calls,
                           has_rail=True, has_bus=False)


def _bus(stop_place, stop_name, n_calls, operator_is_goa=False):
    return SimpleNamespace(stop_place=stop_place, stop_name=stop_name, n_calls=n_calls,
                           has_rail=operator_is_goa, has_bus=not operator_is_goa)


def _nsr(ref, place_id, county_no=None):
    return SimpleNamespace(id=ref, place_id=place_id, county_no=county_no)


def _refresh(fake, monkeypatch):
    monkeypatch.setattr(main, "bq_client", fake)
    main.refresh_varsel_stops()
    return {s["stop_place"]: s for s in main._varsel_stops()}


def test_colocated_same_name_refs_collapse_to_the_busiest(monkeypatch):
    """The reported bug: 57120 (12 calls) and 57070 (2 calls) are one ferry berth in the registry."""
    fake = _FakeBQ(
        bus=[_bus("57120", "Nyvoll ferjekai", 12), _bus("57070", "Nyvoll ferjekai", 2)],
        registry=[_nsr("57120", "parent-1", "56"), _nsr("57070", "parent-1", "56")],
    )
    stops = _refresh(fake, monkeypatch)
    assert set(stops) == {"57120"}, "the quiet twin must not be offered as a second row"
    # Calls are summed: the row stands for the whole place, and the total is what ranks it.
    assert stops["57120"]["n_calls"] == 14
    assert varsel.canonical_stop("57070") == "57120"
    assert varsel.canonical_stop("57120") == "57120"


def test_rail_and_bus_sides_of_one_station_become_one_row(monkeypatch):
    """Lysaker stasjon is two refs (rail 157, bus 4377) under one parent — one row, both modes."""
    fake = _FakeBQ(
        rail=[_rail("157", "Lysaker stasjon", 366)],
        bus=[_bus("4377", "Lysaker stasjon", 726)],
        registry=[_nsr("157", "parent-lysaker", "32"), _nsr("4377", "parent-lysaker", "32")],
    )
    stops = _refresh(fake, monkeypatch)
    assert set(stops) == {"4377"}, "the busier ref wins the row"
    assert stops["4377"]["mode"] == "Tog/Buss", "the merged row serves both families"
    assert stops["4377"]["n_calls"] == 1092


def test_one_parent_with_different_names_is_not_merged(monkeypatch):
    """A parent place can hold genuinely different stops. Merging them would answer a search for
    Jernbanetorget with Oslo S."""
    fake = _FakeBQ(
        rail=[_rail("337", "Oslo S", 900)],
        bus=[_bus("58366", "Jernbanetorget", 400)],
        registry=[_nsr("337", "parent-oslo", "03"), _nsr("58366", "parent-oslo", "03")],
    )
    stops = _refresh(fake, monkeypatch)
    assert set(stops) == {"337", "58366"}
    assert varsel.canonical_stop("58366") == "58366"


def test_county_comes_from_the_municipality_prefix(monkeypatch):
    """Both numbering generations resolve (Kambo is still 0104), and a non-Norwegian codespace is
    left unlabelled instead of being run through a table that does not describe it."""
    fake = _FakeBQ(
        rail=[_rail("513", "Kambo", 20), _rail("57123", "Korsfjord ferjekai", 12),
              _rail("99", "Åre station", 4)],
        registry=[_nsr("513", "513", "01"), _nsr("57123", "57123", "56"), _nsr("99", "99", None)],
    )
    stops = _refresh(fake, monkeypatch)
    assert stops["513"]["region"] == "Østfold"
    assert stops["57123"]["region"] == "Finnmark"
    assert stops["99"]["region"] is None


def test_an_unreadable_registry_leaves_the_list_as_it_was(monkeypatch):
    """Fail-open, like every other sub-query here: no registry means no grouping and no counties —
    not an empty picker."""
    fake = _FakeBQ(
        bus=[_bus("57120", "Nyvoll ferjekai", 12), _bus("57070", "Nyvoll ferjekai", 2)],
        registry=RuntimeError("registry unavailable"),
    )
    stops = _refresh(fake, monkeypatch)
    assert set(stops) == {"57120", "57070"}
    assert all(s["region"] is None for s in stops.values())
    assert all(s["place_id"] == s["stop_place"] for s in stops.values())
    assert varsel.canonical_stop("57070") == "57070"


def test_a_failing_call_source_still_yields_the_other(monkeypatch):
    fake = _FakeBQ(
        rail=RuntimeError("rail table missing"),
        bus=[_bus("57120", "Nyvoll ferjekai", 12)],
        registry=[_nsr("57120", "parent-1", "56")],
    )
    stops = _refresh(fake, monkeypatch)
    assert set(stops) == {"57120"}


def test_the_primary_does_not_flip_between_refreshes(monkeypatch):
    """Ties break on the ref, not on dict order: a primary that flipped would rewrite the same
    registrations back and forth on every refresh."""
    fake = _FakeBQ(
        bus=[_bus("b-ref", "Kaia", 5), _bus("a-ref", "Kaia", 5)],
        registry=[_nsr("b-ref", "p", "50"), _nsr("a-ref", "p", "50")],
    )
    first = _refresh(fake, monkeypatch)
    fake.by_source["bus_calls_planned_today"] = [_bus("a-ref", "Kaia", 5), _bus("b-ref", "Kaia", 5)]
    second = _refresh(fake, monkeypatch)
    assert set(first) == set(second) == {"a-ref"}


def test_an_unnamed_stop_is_never_merged(monkeypatch):
    """Its name cannot be shown to tell it apart either, so hiding it behind a neighbour's row would
    remove it from the picker with nothing to explain the loss."""
    fake = _FakeBQ(
        bus=[_bus("x1", None, 3), _bus("x2", None, 9)],
        registry=[_nsr("x1", "p", "50"), _nsr("x2", "p", "50")],
    )
    stops = _refresh(fake, monkeypatch)
    assert set(stops) == {"x1", "x2"}


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
    import inspect

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        mp = _MonkeyPatch()
        try:
            if "monkeypatch" in inspect.signature(t).parameters:
                t(mp)
            else:
                t()
        finally:
            mp.undo()
            varsel.set_stop_aliases({})
        print(f"  ok  {t.__name__}")
    os.unlink(_STATE_PATH)
    print(f"\n{len(tests)} stop-picker checks passed.")
