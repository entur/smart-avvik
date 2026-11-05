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


def population(ratio: str, definitions: int, *, pairs: int | None = None) -> dict[str, int]:
    """How many pairs, windows and journeys `definitions` definitions are spread over.

    `pairs` overrides the ratio's own pair count and leaves everything else alone. Phase A's gate
    names one shape the two ratios do not produce on their own -- 10 000 definitions over 2 000
    pairs -- and deriving it by moving `--definitions` would change the denominator R1 is read
    against at the same time.
    """
    if ratio == "plan":
        pairs = min(4000, definitions) if pairs is None else pairs
        windows = 50
    elif ratio == "prd":
        pairs = max(1, round(definitions / PRD_DEFINITIONS_PER_PAIR)) if pairs is None else pairs
        # Definitions on one pair differ by WINDOW, so the window vocabulary must be at least the
        # per-pair count or the pairs cannot carry that many distinct definitions.
        windows = max(2, round(PRD_DEFINITIONS_PER_PAIR) + 1)
    else:  # pragma: no cover - argparse constrains this
        raise ValueError(f"unknown ratio: {ratio!r}")
    pairs = max(1, min(pairs, definitions))
    # Same rule as the `prd` branch's, applied after any override: fewer pairs than definitions
    # means several definitions share one, and they can only differ by window.
    windows = max(windows, -(-definitions // pairs) + 1)
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
    pairs: int | None = None,
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
    os.environ.setdefault("SMART_AVVIK_ALLOW_JP_NETWORK", "0")
    os.environ.setdefault("SMART_AVVIK_ALLOW_WEBHOOK_NETWORK", "0")
    os.environ["VARSEL_LLM_PROVIDER"] = "off"  # the kill switch: no backend is even asked

    import alternatives
    import decisions
    import impact
    import test_varsel_api as suite  # type: ignore[import-not-found]
    import varsel
    from bench_cycle import NEVER_CALLED, profile_report, row_counting_record

    suite._reset_state()
    varsel._send_push = lambda subscription, payload: "sent"
    alternatives.fetch_candidates = lambda *a, **k: []
    # **The decision-row build is inside R1's budget, so it has to be inside the measurement.**
    # R1 says in as many words that "matching and rows must fit the remainder", and
    # `_record_decisions` returns at its FIRST line unless `decisions.enabled()` -- which is
    # `_bq_client is not None`. A harness that configures no client therefore measured the match and
    # the wording passes and skipped every row, silently, while reporting itself as the pass's
    # end-to-end cost. The sentinel client is never called: `record` is the only entry point that
    # touches it and it is stubbed, so this stays hermetic. What now runs for real is
    # `decisions.build_row`, `decisions.rescored`, `varsel._thinning_reason` and the KTD15 ledger --
    # the per-definition work U4 added, which is exactly what the gate has to see.
    #
    # The stub is written to the CALL's arity, `record(rows, *, beat=...)`, and not to the
    # pre-U4 `record(rows)` the two harnesses carried: at exact arity an added kwarg raises
    # `TypeError` at the call, which here would have surfaced as the whole benchmark failing -- and
    # did not, only because the early return above meant the stub was never reached at all. Both
    # halves of that are worth keeping shut, so the row count is REPORTED: a zero in
    # `decision_rows` means the pass built no rows and the figure has quietly become a matcher
    # figure again.
    rows_written: list[int] = []
    decisions._bq_client = NEVER_CALLED
    decisions.record = row_counting_record(rows_written)

    snapshot = synthetic_snapshot(trips=trips, situations=situations)
    suite._use_snapshot(snapshot)
    impact.invalidate_generation()
    shape = population(ratio, definitions, pairs=pairs)
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
        # both figures come out of one process and one cache. No destinations: `_trip_cells` then
        # reads them off the scope, which is the whole snapshot here.
        impact._pruned_scope = lambda by_situation, refreshed_at, **kw: (
            {kw["situation"]: by_situation[kw["situation"]]}
            if kw.get("situation") is not None and kw["situation"] in by_situation
            else ({} if kw.get("situation") is not None else by_situation),
            None,
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
        # The unprofiled pass's rows -- the one the ms/definition above comes from. Zero means
        # `_record_decisions` returned early and the figure covers matching and wording only.
        decision_rows=rows_written[0] if rows_written else 0,
    )
    return report


# ---- The public read's own measurement (U15) -----------------------------------------------------

#: The cycle period the free-interpreter share is derived against: prd's measured p50 (2 144 cycles,
#: 2026-09-11). The pull read's sustainable rate is a SHARE of one interpreter, so it can only be
#: stated against how much of that interpreter the pass is already using.
CYCLE_PERIOD_S = 295.0


def run_pull_read(
    ratio: str,
    definitions: int,
    *,
    samples: int = 200,
    trips: int = PEAK_TRIPS,
    situations: int = PEAK_SITUATIONS,
    pairs: int | None = None,
) -> dict:
    """What `GET /journey-alerts` costs the one interpreter, and what the pass's warm costs it (U15).

    Three figures, over the same synthetic peak cache and the same seeded population `run` uses, so
    they are comparable with the ms-per-definition R1 is read against rather than with each other
    only.

    * **miss** — `_compute_figures` for a definition nothing warmed: the matcher on the request pool.
      This is the number `MAX_PER_MIN` has to be sized against, because it is the only one that is
      not a dict lookup, and it is what U2's index moved.
    * **hit** — the assembly a warmed or already-computed definition costs: the cut, the cache reads
      and the payload build, with no matcher at all.
    * **warm** — what pass 1a pays to publish its own match for every registered definition, as the
      DELTA between two whole cycles, one with the seam wired and one without. A delta of two
      timings is noisy by construction; it is reported as a share of the pass rather than to three
      decimals, and the pass's own figure is printed beside it so nobody reads it out of scale.

    From the miss figure and the pass's cost it derives the sustainable miss rate: the share of one
    interpreter the pass leaves, divided by the time a miss takes. That is the number to publish
    beside `MAX_PER_MIN`, because the ceiling is not a throughput -- it bounds abuse, and this
    bounds what the process can actually serve.

    Hermetic like `run`: no BigQuery, no Journey Planner, no Vertex, no push, no Firestore.
    """
    import os
    import time

    api = pathlib.Path(__file__).resolve().parents[1] / "disruptions" / "api"
    if str(api) not in sys.path:
        sys.path.insert(0, str(api))
    os.environ.setdefault("SMART_AVVIK_ALLOW_JP_NETWORK", "0")
    os.environ.setdefault("SMART_AVVIK_ALLOW_WEBHOOK_NETWORK", "0")
    os.environ["VARSEL_LLM_PROVIDER"] = "off"

    import alternatives
    import decisions
    import impact
    import notification_pull
    import test_varsel_api as suite  # type: ignore[import-not-found]
    import varsel
    from bench_cycle import NEVER_CALLED, row_counting_record

    suite._reset_state()
    varsel._send_push = lambda subscription, payload: "sent"
    alternatives.fetch_candidates = lambda *a, **k: []
    rows_written: list[int] = []
    decisions._bq_client = NEVER_CALLED
    decisions.record = row_counting_record(rows_written)

    snapshot = synthetic_snapshot(trips=trips, situations=situations)
    shape = population(ratio, definitions, pairs=pairs)

    def reseed():
        suite._reset_state()
        notification_pull.reset_state()
        suite._use_snapshot(snapshot)
        impact.invalidate_generation()
        return seed_registrations(varsel, shape)

    refreshed_at = datetime.now(UTC)
    notification_pull.configure(refreshed_at=lambda: refreshed_at)

    # 1. The pass WITHOUT the seam -- the baseline the warm's delta is taken against.
    reseed()
    stashed = varsel._figures_warmer
    varsel._figures_warmer = None
    started = time.process_time()
    varsel.check_and_notify(snapshot)
    cold_pass_s = time.process_time() - started

    # 2. The pass WITH it. Same cache, same population, freshly reset state.
    reseed()
    varsel._figures_warmer = notification_pull.warm_figures
    try:
        started = time.process_time()
        varsel.check_and_notify(snapshot)
        warm_pass_s = time.process_time() - started
        warmed = len(notification_pull._figures)
    finally:
        varsel._figures_warmer = stashed

    # 3. A MISS, over a sample of the seeded definitions. The figures cache is emptied first so
    #    every one of them runs the matcher; `_compute_figures` is called directly rather than
    #    through the route, because a route timing here would measure Starlette and the test client.
    registered = list(varsel._journeys.values())[:samples]
    now = datetime.now(UTC)
    with notification_pull._figures_lock:
        notification_pull._figures.clear()
    started = time.process_time()
    for journey in registered:
        notification_pull._compute_figures(journey, now, journey_key=varsel.journey_key(journey))
    miss_s = time.process_time() - started

    # 4. A HIT: the assembly, from figures that are already there.
    for journey in registered:
        key = notification_pull._cache_key(varsel.journey_key(journey), refreshed_at, now)
        notification_pull._remember_figures(
            key, refreshed_at, notification_pull._compute_figures(journey, now, journey_key=key[0])
        )
    started = time.process_time()
    for journey in registered:
        key = notification_pull._cache_key(varsel.journey_key(journey), refreshed_at, now)
        figures = notification_pull._cached_figures(key, refreshed_at)
        alive = notification_pull._alive(figures.entries, now)[: notification_pull.DEFAULT_LIMIT]
        states = notification_pull._listed_states(
            journey, key[0], (journey["from_stop"], journey["to_stop"]), alive, now, enabled=False
        )
        notification_pull._assemble(journey, alive, states, notification_pull.DEFAULT_LIMIT)
        notification_pull._coverage(journey)
    hit_s = time.process_time() - started

    # 5. What the warmed cache RETAINS. Its own pass, untimed, because `tracemalloc` roughly
    #    doubles allocation cost and would report the harness in the warm delta above. Measured as
    #    "free it and see how much comes back", which is retained size rather than peak: snapshot
    #    with the cache full, drop it, collect, snapshot again. An `_Entry` holds the winner and
    #    every uncollapsed alert about its train, and those carry per-alert `impact` COPIES, so the
    #    figure is set by how many definitions are alert-worthy -- which is the bad morning's shape,
    #    not an ordinary evening's, where almost every `_Figures` is empty.
    import gc
    import tracemalloc

    reseed()
    varsel._figures_warmer = notification_pull.warm_figures
    tracemalloc.start()
    try:
        varsel.check_and_notify(snapshot)
        entries = sum(len(f.entries) for f in notification_pull._figures.values())
        with_cache = tracemalloc.take_snapshot()
        with notification_pull._figures_lock:
            notification_pull._figures.clear()
        gc.collect()
        without_cache = tracemalloc.take_snapshot()
        retained = sum(s.size for s in with_cache.statistics("filename")) - sum(
            s.size for s in without_cache.statistics("filename")
        )
    finally:
        tracemalloc.stop()
        varsel._figures_warmer = stashed

    n = len(registered) or 1
    miss_ms = miss_s * 1000 / n
    free_share = max(0.0, 1.0 - warm_pass_s / CYCLE_PERIOD_S)
    return {
        "ratio": ratio,
        "definitions": definitions,
        "population": shape,
        "snapshot": snapshot_shape(snapshot),
        "samples": n,
        "decision_rows": rows_written[0] if rows_written else 0,
        "miss_ms": round(miss_ms, 3),
        "hit_ms": round(hit_s * 1000 / n, 3),
        "cold_pass_s": round(cold_pass_s, 3),
        "warm_pass_s": round(warm_pass_s, 3),
        "warm_delta_s": round(warm_pass_s - cold_pass_s, 3),
        "warm_share_of_pass": round((warm_pass_s - cold_pass_s) / cold_pass_s, 4) if cold_pass_s else None,
        "definitions_warmed": warmed,
        "figures_entries": entries,
        "figures_bytes": max(0, retained),
        "figures_mb": round(max(0, retained) / 1_048_576, 1),
        "figures_bytes_per_definition": round(max(0, retained) / definitions) if definitions else None,
        "cycle_period_s": CYCLE_PERIOD_S,
        "free_interpreter_share": round(free_share, 4),
        # What the process can serve if EVERY read misses: the honest floor, and the number R6's
        # 50 rps has to be checked against.
        "sustainable_misses_per_s": round(free_share / (miss_ms / 1000), 1) if miss_ms else None,
        "r6_target_per_s": 50,
    }


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
        "--pairs",
        type=int,
        default=None,
        help="Override the ratio's pair count (Phase A's gate names 10 000 definitions / 2 000 pairs).",
    )
    parser.add_argument(
        "--unpruned",
        action="store_true",
        help="Bypass the inverted index — the pass as it was before U2, for the comparison.",
    )
    parser.add_argument(
        "--pull-read",
        action="store_true",
        help="Measure the public read instead (U15): per-miss and per-hit interpreter time, what "
        "the pass's warm costs, and the sustainable miss rate that follows from them.",
    )
    parser.add_argument(
        "--samples", type=int, default=200, help="Definitions sampled for the per-read figures."
    )
    parser.add_argument("--json", action="store_true", help="Print the reports as JSON only.")
    return parser


def render(reports: Sequence[dict]) -> str:
    lines = ["", "bench_matcher — interpreter ms per definition", ""]
    head = (
        f"{'ratio':<6} {'index':<9} {'defs':>7} {'pairs':>7} {'rows':>7} "
        f"{'total s':>9} {'ms/def':>9}"
    )
    lines += [head, "-" * len(head)]
    for r in reports:
        lines.append(
            f"{r['ratio']:<6} {'pruned' if r['pruned'] else 'unpruned':<9} "
            f"{r['definitions']:>7} {r['population']['pairs']:>7} {r['decision_rows']:>7} "
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


def render_pull_read(reports: Sequence[dict]) -> str:
    lines = ["", "bench_matcher --pull-read — what GET /journey-alerts costs the one interpreter", ""]
    head = (
        f"{'ratio':<6} {'defs':>7} {'warmed':>7} {'entries':>8} {'cache MB':>9} "
        f"{'miss ms':>8} {'hit ms':>8} {'pass s':>8} {'warm s':>8} {'free':>6} {'miss/s':>8}"
    )
    lines += [head, "-" * len(head)]
    for r in reports:
        lines.append(
            f"{r['ratio']:<6} {r['definitions']:>7} {r['definitions_warmed']:>7} "
            f"{r.get('figures_entries', 0):>8} {r.get('figures_mb', 0):>9.1f} "
            f"{r['miss_ms']:>8.3f} {r['hit_ms']:>8.3f} {r['cold_pass_s']:>8.2f} "
            f"{r['warm_delta_s']:>8.2f} {r['free_interpreter_share']:>6.2f} "
            f"{r['sustainable_misses_per_s']:>8.1f}"
        )
    lines += [
        "",
        "miss/s is the sustainable rate if EVERY read misses: the share of one interpreter the pass",
        f"leaves at a {CYCLE_PERIOD_S:.0f} s period, divided by the per-miss time. R6 asks for 50/s.",
        "A read for a definition the pass warmed costs the hit column instead, and warming is what",
        "moves reads from the first column to the second — it does not make a miss cheaper.",
        "",
        "cache MB is what the warmed figures RETAIN, against the pod's 2 048 MiB. It is set by the",
        "ENTRIES, not the definitions: almost every _Figures is empty on an ordinary evening, and a",
        "bad morning is the shape that fills them (~13 % of definitions alert-worthy in prd).",
        "",
        "This is a synthetic cache in one process. The figure R6 is settled against is a 50 rps run",
        "against the real dev process during pass 1a, in the evening shape.",
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ratios = RATIOS if args.ratio == "both" else (args.ratio,)
    if args.pull_read:
        reports = [
            run_pull_read(
                ratio,
                args.definitions,
                samples=args.samples,
                trips=args.trips,
                situations=args.situations,
                pairs=args.pairs,
            )
            for ratio in ratios
        ]
        print(json.dumps(reports, indent=2) if args.json else render_pull_read(reports))
        return 0
    reports = [
        run(
            ratio,
            args.definitions,
            top=args.top,
            unpruned=args.unpruned,
            trips=args.trips,
            situations=args.situations,
            pairs=args.pairs,
        )
        for ratio in ratios
    ]
    print(json.dumps(reports, indent=2) if args.json else render(reports))
    return 0


if __name__ == "__main__":
    sys.exit(main())
