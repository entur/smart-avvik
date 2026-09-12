#!/usr/bin/env python3
"""What the match pass costs per definition, at a synthetic late-evening prediction cache.

`bench_cycle.py profile` answers "which functions hold the interpreter" over the /varsel-api
suite's ONE-situation snapshot, and says so: its ms-per-definition is a floor. This answers the
question R1 is actually stated in — interpreter ms per DEFINITION at the peak cache and at the two
registration shapes the plan sizes against — by building the cache the pass would meet at 00:30
Oslo (~930 trips, ~1 100 situations) and running `varsel.check_and_notify` over 20 000 definitions.

Two ratios, because they stress different things and the plan quotes both:

* `plan` — 4 000 distinct place pairs over 50 distinct windows. This is the first draft's shape and
  the one the per-(pair, window) memo is most favourable to.
* `prd`  — the measured pilot's shape (2026-09-11: 275 journeys on 226 definitions over 160 pairs,
  i.e. 1.22 journeys per definition and 1.41 definitions per pair), scaled up. Definitions barely
  share a pair here, so it is the honest floor on what memoisation can buy and the number R1 should
  be read against.

`--unpruned` runs the same pass with `impact`'s inverted index bypassed — the code as it was before
U2 — so the two figures come out of one process, one snapshot and one profiler rather than out of
two runs someone has to argue are comparable.

Hermetic: no BigQuery, no Journey Planner, no Vertex, no push, no Firestore, nothing written.

    uv run python bench_matcher.py --ratio prd
    uv run python bench_matcher.py --ratio plan --definitions 2000 --unpruned
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# ---- The synthetic prediction cache -------------------------------------------------------------
#
# Measured in prd on 2026-09-10: 30-70 trips at 04:00-06:00, ~260-310 at 07:00-08:00, ~530 at 15:00
# and ~930 trips / ~1 100 situations at midnight, reset at 02:00 when the planned-today tables roll.
# The midnight figure is the one to size against: it is the largest cache the pass ever meets, it is
# set by the FEED and not by registrations, and it is the cycle where `varsel_notify` measures
# 17-20 s today at 226 definitions.

PEAK_TRIPS = 930
PEAK_SITUATIONS = 1100
#: Calls per trip. ~20 stops x 930 trips is the ~20 000 scored calls KTD8 sizes the index build at.
CALLS_PER_TRIP = 20
#: Distinct stop places in the synthetic network. Small enough that pairs collide the way a real
#: commuter population's do, large enough that a pair is not the whole cache.
STOP_PLACES = 400
#: How many of the situations are BIG ones — a single SX message naming many trains, the shape that
#: makes a per-situation grouping expensive. The rest are the synthetic per-train situations
#: (`KIX:NoSituation:`, `KIX:ETCancellation:`) that are 88-95 % of prd's decision rows. Without at
#: least one of these touching the benchmarked pairs the benchmark measures only the easy case.
BIG_SITUATION_TRAINS = (180, 90, 45, 20, 20)


@dataclass(frozen=True)
class BenchCall:
    """Duck-types `impact.CallPred` for every field the matcher reads.

    A local type rather than the real one so this module imports nothing from `disruptions/api` at
    module scope — the harness's own rule, and what lets its test suite run without the API's
    dependency group.
    """

    situation_number: str
    jkey: str
    operator: str
    line: str | None
    stop_place: str
    stop_order: int
    aimed_dep: datetime | None
    aimed_arr: datetime | None
    p_impact_dep: float | None
    summary: str | None = "Innstilte avganger"
    description: str | None = None
    stop_name: str | None = None
    p_cancel: float = 0.3
    dep_delay_typ_min: int | None = 4
    dep_delay_p90_min: int | None = 15
    dep_delay_p50_stack_min: int | None = 3
    dep_delay_p90_stack_min: int | None = 12
    arr_delay_typ_min: int | None = 5
    arr_delay_p90_min: int | None = 16
    arr_delay_p50_stack_min: int | None = 4
    arr_delay_p90_stack_min: int | None = 14
    p_arr_ge10: float | None = 0.4
    p_arr_ge20: float | None = 0.2
    p_dep_ge10: float | None = 0.4
    p_dep_ge20: float | None = 0.2
    p_impact: float | None = 0.5
    et_declared_cancelled: bool | None = False
    et_dep_delay_min: int | None = None
    call_source: str | None = "estimated"
    has_situation: bool = True
    boarding_allowed: bool = True
    alighting_allowed: bool = True


def stop_ref(i: int) -> str:
    return f"NSR:StopPlace:{i:05d}"


def synthetic_snapshot(
    *,
    trips: int = PEAK_TRIPS,
    situations: int = PEAK_SITUATIONS,
    calls_per_trip: int = CALLS_PER_TRIP,
    places: int = STOP_PLACES,
    seed: int = 20260912,
) -> dict[str, list[BenchCall]]:
    """`{situationNumber: [BenchCall]}` at the late-evening maximum.

    Trips are drawn over a HUB-weighted stop pool, because that is what a real network and a real
    commuter population both look like: prd's 160 pairs are concentrated on a handful of stations,
    so a benchmark over uniformly random stops would give every pair a near-empty posting list and
    flatter the index.

    Departure times are spread over the twelve hours ahead — the `-60/+720 min` window the no-SX
    branch scores — so a definition's window selects a slice rather than everything.
    """
    rng = random.Random(seed)
    now = datetime.now(UTC).replace(microsecond=0)
    # A hub-weighted pool: the first 10 places are drawn ~10x as often as the rest.
    pool = [stop_ref(i % 10) for i in range(300)] + [stop_ref(i) for i in range(places)]

    def build_trip(sit: str, index: int) -> list[BenchCall]:
        jkey = f"VYG:ServiceJourney:{index:06d}"
        line = f"VYG:Line:{rng.randrange(60)}"
        dep = now + timedelta(minutes=rng.randrange(-60, 720))
        stops = [rng.choice(pool) for _ in range(calls_per_trip)]
        severity = rng.random()
        return [
            BenchCall(
                situation_number=sit,
                jkey=jkey,
                operator="VYG",
                line=line,
                stop_place=stop,
                stop_order=order,
                aimed_dep=dep + timedelta(minutes=3 * order),
                aimed_arr=dep + timedelta(minutes=3 * order - 1) if order else None,
                p_impact_dep=severity,
                p_impact=severity,
                # The two populations the gate reads apart, at roughly prd's mix.
                has_situation=not sit.startswith("KIX:"),
                et_declared_cancelled=sit.startswith("KIX:ETCancellation:"),
                # A minority of calls close one activity, as the served operators do (0-6.3 % of
                # mid-journey bus calls, and every long-distance rail run's local stretch).
                boarding_allowed=rng.random() > 0.04,
                alighting_allowed=rng.random() > 0.04,
            )
            for order, stop in enumerate(stops)
        ]

    snapshot: dict[str, list[BenchCall]] = {}
    trip_index = 0
    # The big multi-train SX situations first, so they sit early in the snapshot's own order the way
    # an operator's message published before the night's cancellations does.
    for n, count in enumerate(BIG_SITUATION_TRAINS):
        sit = f"NSB:SituationNumber:{n:04d}"
        calls: list[BenchCall] = []
        for _ in range(count):
            calls.extend(build_trip(sit, trip_index))
            trip_index += 1
        snapshot[sit] = calls
    # Then the per-train situations the no-SX and ET-relay branches produce. There are MORE
    # situations than trips in prd (~1 100 over ~930) because several messages can name one train,
    # so once every trip has one the remainder are second situations about trips already present.
    n = 0
    while len(snapshot) < situations:
        kind = "KIX:ETCancellation" if n % 7 == 0 else "KIX:NoSituation"
        sit = f"{kind}:{n:06d}"
        snapshot[sit] = build_trip(sit, trip_index if trip_index < trips else n % trips)
        trip_index = min(trip_index + 1, trips)
        n += 1
    return snapshot


def snapshot_shape(snapshot: dict[str, list[BenchCall]]) -> dict[str, int]:
    """What the generated cache actually contains, printed beside the timing.

    A ms-per-definition figure means nothing without the cache it was measured over — that is the
    whole reason `bench_cycle.profile` has to call its own number a floor.
    """
    trips = {(c.operator, c.jkey) for calls in snapshot.values() for c in calls}
    return {
        "situations": len(snapshot),
        "trips": len(trips),
        "calls": sum(len(calls) for calls in snapshot.values()),
        "stop_places": len({c.stop_place for calls in snapshot.values() for c in calls}),
        "largest_situation_calls": max((len(c) for c in snapshot.values()), default=0),
    }


# ---- The registration population ----------------------------------------------------------------
#
# prd, 2026-09-11: 275 journeys on 226 definitions over 160 pairs. So 1.22 journeys per definition
# and 1.41 definitions per pair. Both ratios are held exactly; only the scale moves.

PRD_JOURNEYS_PER_DEFINITION = 1.22
PRD_DEFINITIONS_PER_PAIR = 1.41

RATIOS = ("plan", "prd")

#: R1's per-definition budget, at the two counts the requirement names: 120 s of non-BigQuery cycle
#: work divided by the measured definitions. A run at any other `--definitions` is compared against
#: THESE, not against 120 s over whatever count it happened to use — the budget is a property of the
#: pilot's size, not of the benchmark's.
R1_TARGETS = {"35000": 3.4, "80000": 1.5}


def population(ratio: str, definitions: int) -> dict[str, int]:
    """How many pairs, windows and journeys `definitions` definitions are spread over."""
    if ratio == "plan":
        pairs = min(4000, definitions)
        windows = 50
    elif ratio == "prd":
        pairs = max(1, round(definitions / PRD_DEFINITIONS_PER_PAIR))
        # Definitions on one pair differ by WINDOW, so the window vocabulary must be at least the
        # per-pair count or the pairs cannot carry that many distinct definitions.
        windows = max(2, round(PRD_DEFINITIONS_PER_PAIR) + 1)
    else:  # pragma: no cover - argparse constrains this
        raise ValueError(f"unknown ratio: {ratio!r}")
    return {
        "definitions": definitions,
        "pairs": pairs,
        "windows": windows,
        "journeys": round(definitions * PRD_JOURNEYS_PER_DEFINITION),
    }


def seed_registrations(varsel, shape: dict[str, int], *, seed: int = 7) -> dict[str, int]:
    """Write `shape` worth of registrations straight into `varsel`'s store.

    Straight in rather than through `POST /varsel-api/journeys`, which would spend a connectivity
    check and a card render per registration — neither of which is the pass being measured.

    Every definition gets its own (pair, begin) so `journey_key` separates them, and the extra
    journeys beyond `definitions` are second devices on definitions already present, which is what
    the 1.22 ratio means: two phones watching one trip.
    """
    rng = random.Random(seed)
    hubs = [stop_ref(i % 10) for i in range(30)] + [stop_ref(i) for i in range(STOP_PLACES)]
    # DISTINCT pairs, in draw order. A duplicate would collapse two definitions into one under
    # `journey_key`, so the population would silently be smaller than the count it reports — and
    # ms-per-definition would be wrong by exactly that factor with nothing saying so.
    seen: dict[tuple[str, str], None] = {}
    while len(seen) < shape["pairs"]:
        a, b = rng.choice(hubs), rng.choice(hubs)
        if a != b:
            seen[(a, b)] = None
    pairs = list(seen)
    begins = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 15, 30, 45)]
    definitions: list[dict] = []
    for i in range(shape["definitions"]):
        from_stop, to_stop = pairs[i % len(pairs)]
        definitions.append({
            "from_stop": from_stop,
            "to_stop": to_stop,
            "begin": begins[(i // len(pairs)) % shape["windows"] % len(begins)],
            "end": "23:59",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "created_ts": "2026-01-01T00:00:00+00:00",
        })
    for j in range(shape["journeys"]):
        device = f"bench-{j:06d}"
        varsel._devices[device] = {
            "subscription": None, "created_ts": "2026-01-01T00:00:00+00:00"
        }
        varsel._journeys[f"bj-{j:06d}"] = {
            "device_id": device, **definitions[j % len(definitions)]
        }
    return {
        "registered_journeys": len(varsel._journeys),
        "distinct_definitions": len({varsel.journey_key(j) for j in varsel._journeys.values()}),
    }


# ---- The measurement ----------------------------------------------------------------------------


def run(
    ratio: str,
    definitions: int,
    *,
    top: int = 8,
    unpruned: bool = False,
    trips: int = PEAK_TRIPS,
    situations: int = PEAK_SITUATIONS,
) -> dict:
    """Measure one `check_and_notify` over the synthetic peak cache. In-process, nothing written.

    TWO passes, because a profiled pass is not a timing. cProfile roughly doubles interpreter time,
    so the ms-per-definition R1 is stated in comes from an UNPROFILED pass measured with
    `time.process_time()` — process CPU, which is what KTD17's "interpreter time in one CPython
    process" means — and the profiler runs a second time, over freshly reset state, only for the
    self-time breakdown. Quoting the profiled figure would report the harness, not the pass.
    """
    import cProfile
    import os
    import pstats
    import time

    api = pathlib.Path(__file__).resolve().parents[1] / "disruptions" / "api"
    if str(api) not in sys.path:
        sys.path.insert(0, str(api))
    # The network fences, set BEFORE the modules are imported: `alternatives` and `webhook_sender`
    # decide "is this a test process?" from `sys.argv[0]`, which here is `bench_matcher.py`. Without
    # them a profile of the interpreter opens real Journey Planner and Vertex connections — measured
    # on `bench_cycle` before its own fences went in.
    os.environ.setdefault("KIX_ALLOW_JP_NETWORK", "0")
    os.environ.setdefault("KIX_ALLOW_WEBHOOK_NETWORK", "0")
    os.environ["VARSEL_LLM_PROVIDER"] = "off"  # the kill switch: no backend is even asked

    import alternatives
    import decisions
    import impact
    import test_varsel_api as suite  # type: ignore[import-not-found]
    import varsel
    from bench_cycle import profile_report

    suite._reset_state()
    varsel._send_push = lambda subscription, payload: "sent"
    decisions.record = lambda rows: 0
    alternatives.fetch_candidates = lambda *a, **k: []

    snapshot = synthetic_snapshot(trips=trips, situations=situations)
    suite._use_snapshot(snapshot)
    impact.invalidate_generation()
    shape = population(ratio, definitions)
    seeded = seed_registrations(varsel, shape)

    def reseed():
        suite._reset_state()
        suite._use_snapshot(snapshot)
        impact.invalidate_generation()
        return seed_registrations(varsel, shape)

    original = impact._pruned_scope
    if unpruned:
        # The pass as it was before U2: `_trip_cells` over the WHOLE snapshot, once per definition
        # per window per stop-ref pair. Patched here rather than measured from a git checkout so
        # both figures come out of one process and one cache.
        impact._pruned_scope = lambda by_situation, refreshed_at, **kw: (
            {kw["situation"]: by_situation[kw["situation"]]}
            if kw.get("situation") is not None and kw["situation"] in by_situation
            else ({} if kw.get("situation") is not None else by_situation)
        )
    profiler = cProfile.Profile()
    try:
        started = time.process_time()
        varsel.check_and_notify(snapshot)
        cpu_s = time.process_time() - started
        index = impact.index_stats()
        reseed()
        profiler.enable()
        varsel.check_and_notify(snapshot)
        profiler.disable()
    finally:
        impact._pruned_scope = original
    report = profile_report(pstats.Stats(profiler), definitions=definitions, top=top)
    profiled_ms = report.pop("ms_per_definition")
    profiled_s = report.pop("total_interpreter_s")
    report.update(
        ratio=ratio,
        pruned=not unpruned,
        population=shape,
        seeded=seeded,
        snapshot=snapshot_shape(snapshot),
        index=index,
        # The figure R1 is read against, from the unprofiled pass.
        interpreter_s=round(cpu_s, 3),
        ms_per_definition=round(cpu_s * 1000 / definitions, 4) if definitions else None,
        # What the PROFILED pass cost, kept beside it so the breakdown's scale is readable and so
        # nobody quotes the harness's own overhead as the pass's cost.
        profiled_interpreter_s=profiled_s,
        profiled_ms_per_definition=profiled_ms,
        r1_targets_ms_per_definition=R1_TARGETS,
    )
    return report


# ---- CLI -----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ratio", default="both", choices=(*RATIOS, "both"))
    parser.add_argument("--definitions", type=int, default=20000)
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--trips", type=int, default=PEAK_TRIPS)
    parser.add_argument("--situations", type=int, default=PEAK_SITUATIONS)
    parser.add_argument(
        "--unpruned",
        action="store_true",
        help="Bypass the inverted index — the pass as it was before U2, for the comparison.",
    )
    parser.add_argument("--json", action="store_true", help="Print the reports as JSON only.")
    return parser


def render(reports: Sequence[dict]) -> str:
    lines = ["", "bench_matcher — interpreter ms per definition", ""]
    head = f"{'ratio':<6} {'index':<9} {'defs':>7} {'pairs':>7} {'total s':>9} {'ms/def':>9}"
    lines += [head, "-" * len(head)]
    for r in reports:
        lines.append(
            f"{r['ratio']:<6} {'pruned' if r['pruned'] else 'unpruned':<9} "
            f"{r['definitions']:>7} {r['population']['pairs']:>7} "
            f"{r['interpreter_s']:>9} {r['ms_per_definition']:>9}"
        )
    lines += [
        "",
        "R1's budget: 3.4 ms per definition at 35 000, 1.5 ms at 80 000 (120 s / definitions).",
        f"Cache: {json.dumps(reports[0]['snapshot']) if reports else '{}'}",
        "",
        "Top self time (first report):",
    ]
    for row in (reports[0]["top_by_self_time"] if reports else []):
        lines.append(f"  {row['self_ms']:>9.1f} ms  {row['calls']:>9}x  {row['function']}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ratios = RATIOS if args.ratio == "both" else (args.ratio,)
    reports = [
        run(
            ratio,
            args.definitions,
            top=args.top,
            unpruned=args.unpruned,
            trips=args.trips,
            situations=args.situations,
        )
        for ratio in ratios
    ]
    print(json.dumps(reports, indent=2) if args.json else render(reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
