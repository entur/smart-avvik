"""Checks for bench_matcher.py — the synthetic cache, the two ratios and the seeded population.

Hermetic and standalone (`uv run python test_bench_matcher.py`), like every suite in this repo: CI
runs each as its own process and never through pytest.

What these are for is narrow and worth stating, because a benchmark's tests can easily become a
benchmark of the tests. A measurement is only as honest as the load it was taken over, and the two
ways this harness could lie quietly are (1) generating a cache that omits the expensive shape — one
SX message naming hundreds of trains — so the pass looks cheap for a reason the real feed does not
share, and (2) drifting off the registration ratios the plan's budget is stated against. Both are
checked here as numbers.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import bench_matcher as bm

PLAN = (
    pathlib.Path(__file__).resolve().parents[1]
    / "docs" / "plans" / "2026-09-11-002-feat-scale-to-100k-commuters-plan.md"
)


class _FakeVarsel:
    """Just enough of `varsel` for `seed_registrations`, which takes the module as an argument.

    Injected rather than imported so this suite needs none of the API's dependencies — and so the
    seeding logic is checked against the real `journey_key` rule (join on the five fields) without
    importing the matcher that owns it.
    """

    def __init__(self) -> None:
        self._devices: dict = {}
        self._journeys: dict = {}

    @staticmethod
    def journey_key(journey: dict) -> str:
        return "|".join([
            journey["from_stop"], journey["to_stop"], journey["begin"], journey["end"],
            ",".join(str(d) for d in journey["days"]),
        ])


# ---- The synthetic cache ------------------------------------------------------------------------


def test_the_cache_reaches_the_measured_late_evening_peak():
    """~930 trips and ~1 100 situations, which is the largest cache the pass ever meets.

    Situations OUTNUMBER trips in prd (1 100 over 930) because several messages can name one train;
    a generator that stopped at one situation per trip would quietly measure a smaller cache than
    the one it claims.
    """
    snapshot = bm.synthetic_snapshot()
    shape = bm.snapshot_shape(snapshot)
    assert shape["situations"] == bm.PEAK_SITUATIONS, shape
    assert shape["trips"] == bm.PEAK_TRIPS, shape
    assert shape["calls"] >= 18_000, shape  # KTD8 sizes the index build at ~20 000 calls


def test_the_cache_contains_a_situation_naming_hundreds_of_trains():
    """The expensive shape, without which this benchmark measures only the easy case.

    88-95 % of prd's decision rows are synthetic per-train subjects, and those are trivial to group.
    What costs is a real SX message naming a whole line's night of cancellations: that situation's
    calls are grouped in full every time a scan reaches it, so a cache of only per-train situations
    would report a per-definition cost the real evening peak does not have.
    """
    snapshot = bm.synthetic_snapshot()
    big = max(snapshot.values(), key=len)
    trains = {(c.operator, c.jkey) for c in big}
    assert len(trains) >= 100, f"largest situation names {len(trains)} trains"
    assert max(bm.BIG_SITUATION_TRAINS) == len(trains), "the generator dropped a big situation"


def test_the_big_situations_call_at_the_hubs_the_benchmarked_pairs_are_drawn_from():
    """A big situation nothing asks about is decoration.

    Both the trips and the registration pairs are drawn from the same hub-weighted pool, so this
    asserts the overlap rather than assuming it: if the two pools ever drift apart, every pair's
    posting list goes near-empty and the harness reports a flattering number with nothing wrong on
    the face of it.
    """
    snapshot = bm.synthetic_snapshot()
    big = max(snapshot.values(), key=len)
    hubs = {bm.stop_ref(i) for i in range(10)}
    assert hubs <= {c.stop_place for c in big}, "the largest situation misses the hubs"


def test_every_generated_call_carries_its_own_situation_number():
    """The snapshot KEY and `CallPred.situation_number` agree, as `main` makes them agree.

    They are allowed to differ (the matcher scopes on the key and labels on the field), but the
    production cache never makes them, and a benchmark that did would be measuring a shape no
    deployment produces.
    """
    snapshot = bm.synthetic_snapshot(trips=40, situations=50)
    for sit, calls in snapshot.items():
        assert {c.situation_number for c in calls} == {sit}, sit


def test_the_generator_is_deterministic():
    """Same seed, same cache — or two runs of the harness are not comparable."""
    a = bm.snapshot_shape(bm.synthetic_snapshot(trips=60, situations=70))
    b = bm.snapshot_shape(bm.synthetic_snapshot(trips=60, situations=70))
    assert a == b, (a, b)


# ---- The registration population ----------------------------------------------------------------


def test_the_prd_ratio_is_the_measured_one():
    """1.22 journeys per definition and 1.41 definitions per pair (prd blob, 2026-09-11).

    Held as ratios rather than as the raw 275/226/160 so the shape survives the scale-up, which is
    the only thing about the pilot that is supposed to change.
    """
    assert bm.PRD_JOURNEYS_PER_DEFINITION == 275 / 226 or round(
        bm.PRD_JOURNEYS_PER_DEFINITION, 2
    ) == round(275 / 226, 2)
    assert round(bm.PRD_DEFINITIONS_PER_PAIR, 2) == round(226 / 160, 2)
    shape = bm.population("prd", 20000)
    assert shape["pairs"] == round(20000 / bm.PRD_DEFINITIONS_PER_PAIR), shape
    assert shape["journeys"] == round(20000 * bm.PRD_JOURNEYS_PER_DEFINITION), shape


def test_the_plan_ratio_is_four_thousand_pairs_over_fifty_windows():
    shape = bm.population("plan", 20000)
    assert shape["pairs"] == 4000, shape
    assert shape["windows"] == 50, shape


def test_the_seeded_population_really_holds_that_many_distinct_definitions():
    """The ratio has to survive `journey_key`, not just the arithmetic above it.

    Two registrations differing only by `device_id` are ONE definition — that is the whole point of
    the key — so a seeder that varied the wrong field would report 20 000 definitions and give the
    pass 14 000, and the ms-per-definition figure would be wrong by that factor with nothing saying
    so.
    """
    for ratio in bm.RATIOS:
        fake = _FakeVarsel()
        shape = bm.population(ratio, 600)
        seeded = bm.seed_registrations(fake, shape)
        assert seeded["registered_journeys"] == shape["journeys"], (ratio, seeded, shape)
        assert seeded["distinct_definitions"] == shape["definitions"], (ratio, seeded, shape)
        pairs = {(j["from_stop"], j["to_stop"]) for j in fake._journeys.values()}
        assert len(pairs) == shape["pairs"], (ratio, len(pairs), shape)


def test_a_seeded_journey_never_boards_and_alights_at_one_place():
    """A degenerate pair matches nothing, so it would measure the empty case as if it were a trip."""
    fake = _FakeVarsel()
    bm.seed_registrations(fake, bm.population("prd", 300))
    assert all(j["from_stop"] != j["to_stop"] for j in fake._journeys.values())


# ---- The report ---------------------------------------------------------------------------------


def test_the_budget_in_the_report_is_r1s_and_not_the_benchmarks_own_count():
    """3.4 ms at 35 000 definitions and 1.5 ms at 80 000, quoted from R1 itself.

    A run at 20 000 definitions must not be scored against 120 s / 20 000: the budget is a property
    of the pilot's size, not of whatever count the harness was invoked with, and dividing by the
    smaller number is how a pass that misses would read as passing.
    """
    assert bm.R1_TARGETS == {"35000": 3.4, "80000": 1.5}
    r1 = [line for line in PLAN.read_text().splitlines() if line.startswith("- R1.")]
    assert r1, "the plan has no R1 line"
    assert "3.4 ms at 35 000 definitions" in r1[0], r1[0]
    assert "1.5 ms at 80 000" in r1[0], r1[0]


def test_the_rendered_table_names_both_ratios_and_the_cache_it_measured():
    """A ms-per-definition figure with no cache beside it is unreadable six months later."""
    reports = [
        {
            "ratio": ratio, "pruned": True, "definitions": 20000,
            "population": {"pairs": 4000}, "interpreter_s": 9.0, "ms_per_definition": 0.45,
            "snapshot": {"situations": 1100, "trips": 930, "calls": 29000},
            "top_by_self_time": [{"self_ms": 1.0, "calls": 2, "function": "impact.py:1(f)"}],
        }
        for ratio in bm.RATIOS
    ]
    text = bm.render(reports)
    for ratio in bm.RATIOS:
        assert ratio in text, text
    assert "1100" in text and "930" in text, text
    assert "3.4 ms per definition at 35 000" in text, text


def test_the_unpruned_mode_names_the_symbol_it_replaces():
    """`--unpruned` swaps `impact._pruned_scope`; a rename there must fail loudly, not silently.

    Without this the harness would keep running after the production symbol moved, measure the
    pruned pass twice, and report a 1x speedup as if the index had stopped working.
    """
    sys.path.insert(
        0, str(pathlib.Path(__file__).resolve().parents[1] / "disruptions" / "api")
    )
    import impact

    assert hasattr(impact, "_pruned_scope"), "bench_matcher patches a symbol that no longer exists"
    source = pathlib.Path(bm.__file__).read_text()
    assert "impact._pruned_scope = " in source, "the unpruned shim is gone"


def _run() -> None:
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} bench_matcher checks passed.")


if __name__ == "__main__":
    _run()
