"""Hermetic checks for the reisevalg decision core and the /reisevalg-api endpoint.

No network, no BigQuery: fake road providers / board / car / volume fetchers are injected via
``reisevalg.configure()`` (the test_varsel_api.py pattern), time is frozen by monkeypatching
``reisevalg._now``, and the decision core is exercised directly as a pure function of
(observations, now) — like varsel's ``match_journey(now=...)``.

Covers the full precedence matrix (incl. road-normal + rail-degraded -> TRAIN_DEGRADED),
observation-based anti-flap, the arrival-side rail gate, the snapshot veto in both directions,
replacement-bus / RAIL_CLOSURE, per-source staleness (incl. the 08:30-with-07:00-observation
success case), the morning window, provider-exception isolation, DATEX-before-volume priority,
and the response contract for all five states.

Run: uv run --with httpx python disruptions/api/test_reisevalg.py  (or under pytest)
"""

import os

# The DATEX stub reads its env at import — make sure it reports unavailable in this run.
os.environ.pop("SVV_DATEX_USER", None)
os.environ.pop("SVV_DATEX_PASSWORD", None)

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import reisevalg
from fastapi import FastAPI
from fastapi.testclient import TestClient
from reisevalg import (
    Departure,
    HourVolume,
    RoadObservation,
    decide,
    parse_board,
    volume_hour_states,
    volume_judgment,
)
from reisevalg import (
    RecommendationReason as Reason,
)
from reisevalg import (
    RecommendationState as State,
)

_OSLO = reisevalg._OSLO

# The U2 backtest measured the volume signal's precision at 0.60 — below the 0.8 honesty bar —
# so the SHIPPED default is VOLUME_NUDGE_ENABLED=False (a volume-only "congested" never fires
# the nudge). The decision-matrix tests below exercise the full mechanism with the gate open;
# the shipped below-bar default is pinned in test_volume_precision_gate_blocks_nudge_by_default.
VOLUME_NUDGE_SHIPPED = reisevalg.VOLUME_NUDGE_ENABLED
reisevalg.VOLUME_NUDGE_ENABLED = True

app = FastAPI()
app.include_router(reisevalg.router)
client = TestClient(app)


def _oslo(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=_OSLO)


# Wednesday 2026-07-08 08:00 Europe/Oslo — inside the Mon-Fri 05:30-09:30 morning window.
NOW = _oslo(2026, 7, 8, 8, 0)
assert NOW.weekday() == 2
SATURDAY = _oslo(2026, 7, 11, 8, 0)
assert SATURDAY.weekday() == 5
WEEKDAY_NOON = _oslo(2026, 7, 8, 12, 0)


def _dep(
    minutes_from_now=15,
    *,
    now=NOW,
    cancelled=False,
    mode="rail",
    line="VYG:Line:R13",
    ref="VYG:DatedServiceJourney:81613_DRM-DAL_26-07-08",
    oslo_arr_min=35,
    arr_delay_min=0,
    realtime=True,
):
    """A corridor departure from Lier `minutes_from_now` out, arriving Oslo S `oslo_arr_min`
    later (aimed), with `arr_delay_min` of expected arrival delay at Oslo S."""
    aimed = now + timedelta(minutes=minutes_from_now)
    aimed_arr = aimed + timedelta(minutes=oslo_arr_min)
    return Departure(
        dated_journey_ref=ref,
        line=line,
        public_code=line.rsplit(":", 1)[-1],
        mode=mode,
        destination="Oslo S",
        realtime=realtime,
        cancelled=cancelled,
        aimed_dep=aimed,
        expected_dep=aimed,
        oslo_aimed_arr=aimed_arr,
        oslo_expected_arr=aimed_arr + timedelta(minutes=arr_delay_min),
    )


def _normal_board(now=NOW):
    """Three healthy corridor departures (10/25/40 min out)."""
    return [
        _dep(10, now=now, ref="VYG:DatedServiceJourney:1_A"),
        _dep(25, now=now, ref="VYG:DatedServiceJourney:2_B"),
        _dep(40, now=now, ref="VYG:DatedServiceJourney:3_C"),
    ]


def _road(state="congested", *, age_min=30, source="volume", travel=None, now=NOW):
    return RoadObservation(
        source=source,
        state=state,
        observed_at=now - timedelta(minutes=age_min),
        confidence="high" if source == "datex" else "low",
        travel_time_min=travel,
        backtested=source == "volume",
    )


@dataclass
class FakePred:
    """Duck-types the per-call prediction rows returned by main's snapshot lookup."""

    p_cancel: float = 0.0


def _wire(
    mp,
    *,
    now=NOW,
    providers=(),
    departures=(),
    board_error=False,
    car=36.0,
    snapshot=None,
    volume_fetcher=None,
):
    """Reset caches, freeze time and inject fakes everywhere (no network can be reached)."""
    reisevalg._board_cache.reset()
    reisevalg._car_cache.reset()
    reisevalg._volume_cache.reset()
    reisevalg._volume_run_start = None
    mp.setattr(reisevalg, "_now", lambda: now)

    def board():
        if board_error:
            raise RuntimeError("jp unreachable (test)")
        return list(departures)

    reisevalg.configure(
        snapshot_lookup=snapshot,
        road_providers=list(providers) if providers is not None else None,
        board_fetcher=board,
        car_fetcher=lambda: car,
        volume_fetcher=volume_fetcher,
    )


# ---- decide(): precedence matrix ---------------------------------------------------------------
def test_road_congested_rail_normal_recommends_train():
    d = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=_normal_board(),
        rail_observed_at=NOW,
    )
    assert d.state is State.TRAIN_RECOMMENDED
    assert d.reason is Reason.ROAD_CONGESTED_RAIL_OK
    assert d.road_source == "volume"
    # The volume proxy carries no travel time -> no minutes-saved claim.
    assert d.saved_min is None


def test_saved_min_only_from_travel_time_source_and_floored():
    # DATEX drive time 55 min; next train departs +10 and arrives +45 -> saves exactly 10 min.
    d = decide(
        now=NOW,
        road_observations=[_road("congested", source="datex", age_min=5, travel=55.0)],
        departures=_normal_board(),
        rail_observed_at=NOW,
        car_baseline_min=36.0,
    )
    assert d.state is State.TRAIN_RECOMMENDED
    assert d.saved_min == 10
    assert d.car_delay_min == 19  # 55 - 36 free-flow
    # Saving below the MIN_SAVED_MIN floor -> the number is omitted, the state stands.
    d2 = decide(
        now=NOW,
        road_observations=[_road("congested", source="datex", age_min=5, travel=50.0)],
        departures=_normal_board(),
        rail_observed_at=NOW,
        car_baseline_min=36.0,
    )
    assert d2.state is State.TRAIN_RECOMMENDED
    assert d2.saved_min is None


def test_road_normal_rail_normal_no_advantage():
    d = decide(
        now=NOW,
        road_observations=[_road("normal")],
        departures=_normal_board(),
        rail_observed_at=NOW,
    )
    assert (d.state, d.reason) == (State.NO_ADVANTAGE, Reason.ROAD_NORMAL)


def test_rail_degraded_wins_over_road_normal():
    # The honest "don't count on the train today" answer even when the road is fine.
    board = [_dep(10, cancelled=True)] + _normal_board()[1:]
    d = decide(
        now=NOW,
        road_observations=[_road("normal")],
        departures=board,
        rail_observed_at=NOW,
    )
    assert (d.state, d.reason) == (State.TRAIN_DEGRADED, Reason.RAIL_CANCELLATIONS)


def test_rail_degraded_wins_over_road_congested():
    board = [_dep(10, cancelled=True)] + _normal_board()[1:]
    d = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=board,
        rail_observed_at=NOW,
    )
    assert d.state is State.TRAIN_DEGRADED


def test_arrival_side_gate():
    # Departures leave Lier on time, but expected Oslo S arrival is 12 min late -> degraded.
    # The gate is arrival-side: a departure-side check would pass these trains.
    board = [_dep(10, arr_delay_min=12)] + _normal_board()[1:]
    d = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=board,
        rail_observed_at=NOW,
    )
    assert (d.state, d.reason) == (State.TRAIN_DEGRADED, Reason.RAIL_DELAYS)
    # At the threshold (<= 5 min) the train still counts as normal.
    board_ok = [_dep(10, arr_delay_min=5)] + _normal_board()[1:]
    d2 = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=board_ok,
        rail_observed_at=NOW,
    )
    assert d2.state is State.TRAIN_RECOMMENDED


def test_realtime_blind_board_yields_unknown():
    # All inspected departures are timetable-only (realtime=False, on time, not cancelled):
    # "not cancelled, on time" is vacuous during a SIRI->JP realtime outage -> UNKNOWN.
    blind = [
        _dep(10, realtime=False, ref="VYG:DatedServiceJourney:1_A"),
        _dep(25, realtime=False, ref="VYG:DatedServiceJourney:2_B"),
        _dep(40, realtime=False, ref="VYG:DatedServiceJourney:3_C"),
    ]
    d = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=blind,
        rail_observed_at=NOW,
    )
    assert (d.state, d.reason) == (State.UNKNOWN, Reason.NO_RAIL_DATA)
    # An explicit cancellation on a realtime=False row is still real signal: the degraded
    # checks run BEFORE the realtime requirement.
    d2 = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=[_dep(10, realtime=False, cancelled=True)] + blind[1:],
        rail_observed_at=NOW,
    )
    assert (d2.state, d2.reason) == (State.TRAIN_DEGRADED, Reason.RAIL_CANCELLATIONS)
    # One realtime departure among the inspected ones is evidence enough for the normal path.
    d3 = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=[_dep(10, realtime=True)] + blind[1:],
        rail_observed_at=NOW,
    )
    assert d3.state is State.TRAIN_RECOMMENDED


def test_snapshot_veto_demotes_and_never_promotes():
    # JP shows everything on time, but the prediction snapshot flags the first departure with
    # p_cancel above the bar -> the predictive layer vetoes (demote-only).
    flagged = "VYG:DatedServiceJourney:1_A"
    d = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=_normal_board(),
        rail_observed_at=NOW,
        snapshot_lookup=lambda ref: [FakePred(0.9)] if ref == flagged else None,
    )
    assert (d.state, d.reason) == (State.TRAIN_DEGRADED, Reason.RAIL_CANCELLATIONS)
    assert d.predicted_cancel[flagged] == 0.9
    # The inverse: snapshot silent (no active situation), JP shows a cancellation -> degraded.
    board = [_dep(10, cancelled=True)] + _normal_board()[1:]
    d2 = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=board,
        rail_observed_at=NOW,
        snapshot_lookup=lambda ref: None,
    )
    assert d2.state is State.TRAIN_DEGRADED
    # And an optimistic snapshot (p_cancel 0.0) cannot promote a JP-cancelled board back.
    d3 = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=board,
        rail_observed_at=NOW,
        snapshot_lookup=lambda ref: [FakePred(0.0)],
    )
    assert d3.state is State.TRAIN_DEGRADED


def test_snapshot_below_bar_does_not_demote():
    d = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=_normal_board(),
        rail_observed_at=NOW,
        snapshot_lookup=lambda ref: [FakePred(0.3)],
    )
    assert d.state is State.TRAIN_RECOMMENDED
    assert d.predicted_cancel["VYG:DatedServiceJourney:1_A"] == 0.3


def test_replacement_bus_is_not_a_rail_departure():
    # Zero rail-mode departures but replacement buses run -> planned closure, not OUTSIDE_WINDOW.
    board = [
        _dep(10, mode="bus", ref=None),
        _dep(40, mode="bus", ref=None),
    ]
    d = decide(
        now=NOW,
        road_observations=[_road("normal")],
        departures=board,
        rail_observed_at=NOW,
    )
    assert (d.state, d.reason) == (State.TRAIN_DEGRADED, Reason.RAIL_CLOSURE)
    # A replacement bus alongside healthy trains changes nothing (and never passes the rail gate).
    d2 = decide(
        now=NOW,
        road_observations=[_road("normal")],
        departures=[_dep(10, mode="bus", ref=None)] + _normal_board(),
        rail_observed_at=NOW,
    )
    assert d2.state is State.NO_ADVANTAGE
    # Rail rows exist but only beyond the lookahead; a bus within it does NOT substitute.
    d3 = decide(
        now=NOW,
        road_observations=[_road("normal")],
        departures=[_dep(10, mode="bus", ref=None), _dep(90)],
        rail_observed_at=NOW,
    )
    assert (d3.state, d3.reason) == (State.OUTSIDE_WINDOW, Reason.NO_UPCOMING_DEPARTURE)


def test_outside_window_short_circuits():
    for now in (SATURDAY, WEEKDAY_NOON):
        d = decide(
            now=now,
            road_observations=[_road("congested", now=now)],
            departures=_normal_board(now=now),
            rail_observed_at=now,
        )
        assert (d.state, d.reason) == (State.OUTSIDE_WINDOW, Reason.OUTSIDE_MORNING_WINDOW)
    # Saturday's verdict is valid until Monday's window opens.
    d = decide(now=SATURDAY, road_observations=[], departures=None, rail_observed_at=None)
    assert d.valid_until == _oslo(2026, 7, 13, 5, 30).astimezone(UTC)


def test_in_window_but_no_departure_within_hour():
    d = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=[_dep(90)],  # next train 90 min out
        rail_observed_at=NOW,
    )
    assert (d.state, d.reason) == (State.OUTSIDE_WINDOW, Reason.NO_UPCOMING_DEPARTURE)


def test_rail_degraded_reachable_with_zero_road_data():
    board = [_dep(10, cancelled=True)] + _normal_board()[1:]
    d = decide(now=NOW, road_observations=[], departures=board, rail_observed_at=NOW)
    assert d.state is State.TRAIN_DEGRADED
    assert d.road_source is None


# ---- decide(): per-source staleness -------------------------------------------------------------
def test_stale_road_observation_yields_unknown():
    # A successful fetch whose DATA is over the volume cutoff (280 min > 250) is stale-by-age.
    d = decide(
        now=NOW,
        road_observations=[_road("congested", age_min=280)],
        departures=_normal_board(),
        rail_observed_at=NOW,
    )
    assert (d.state, d.reason) == (State.UNKNOWN, Reason.ROAD_DATA_STALE)
    assert d.road_stale is True
    # No observation at all is a different reason.
    d2 = decide(now=NOW, road_observations=[], departures=_normal_board(), rail_observed_at=NOW)
    assert (d2.state, d2.reason) == (State.UNKNOWN, Reason.NO_ROAD_DATA)


def test_datex_staleness_cutoff_is_much_tighter():
    # 30 min old is fine for volumes but stale for DATEX (15 min cutoff).
    d = decide(
        now=NOW,
        road_observations=[_road("congested", source="datex", age_min=30, travel=60.0)],
        departures=_normal_board(),
        rail_observed_at=NOW,
    )
    assert (d.state, d.reason) == (State.UNKNOWN, Reason.ROAD_DATA_STALE)


def test_0830_with_0700_volume_observation_can_recommend():
    # The timing arithmetic the per-source cutoff exists for: at 08:30 an hourly-volume
    # observation stamped 07:00 (90 min old) is still usable -> the nudge CAN fire.
    now = _oslo(2026, 7, 8, 8, 30)
    obs = RoadObservation(
        source="volume",
        state="congested",
        observed_at=_oslo(2026, 7, 8, 7, 0),
        confidence="low",
        travel_time_min=None,
        backtested=True,
    )
    d = decide(
        now=now,
        road_observations=[obs],
        departures=_normal_board(now=now),
        rail_observed_at=now,
    )
    assert d.state is State.TRAIN_RECOMMENDED


def test_volume_precision_gate_blocks_nudge_by_default(monkeypatch):
    # The shipped default: U2 measured precision 0.60 < the 0.8 bar, so the gate is closed.
    assert VOLUME_NUDGE_SHIPPED is False
    assert reisevalg.VOLUME_PRECISION_MEASURED < reisevalg.VOLUME_PRECISION_BAR
    monkeypatch.setattr(reisevalg, "VOLUME_NUDGE_ENABLED", False)
    # Volume-congested + rail normal -> honest UNKNOWN with its own reason, never a nudge.
    d = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=_normal_board(),
        rail_observed_at=NOW,
    )
    assert (d.state, d.reason) == (State.UNKNOWN, Reason.VOLUME_BELOW_PRECISION_BAR)
    assert d.road_source == "volume"
    # DATEX (a measured travel time) is not gated.
    d2 = decide(
        now=NOW,
        road_observations=[_road("congested", source="datex", age_min=5, travel=60.0)],
        departures=_normal_board(),
        rail_observed_at=NOW,
    )
    assert d2.state is State.TRAIN_RECOMMENDED
    # Volume-normal is unaffected (a false "normal" cannot cause a false nudge).
    d3 = decide(
        now=NOW,
        road_observations=[_road("normal")],
        departures=_normal_board(),
        rail_observed_at=NOW,
    )
    assert (d3.state, d3.reason) == (State.NO_ADVANTAGE, Reason.ROAD_NORMAL)
    # Rail-degraded still wins with the gate closed (reachable without a valid road nudge).
    board = [_dep(10, cancelled=True)] + _normal_board()[1:]
    d4 = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=board,
        rail_observed_at=NOW,
    )
    assert d4.state is State.TRAIN_DEGRADED


def test_stale_rail_board_yields_unknown():
    d = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=_normal_board(),
        rail_observed_at=NOW - timedelta(minutes=30),
    )
    assert (d.state, d.reason) == (State.UNKNOWN, Reason.RAIL_DATA_STALE)
    d2 = decide(
        now=NOW, road_observations=[_road("congested")], departures=None, rail_observed_at=None
    )
    assert (d2.state, d2.reason) == (State.UNKNOWN, Reason.NO_RAIL_DATA)


def test_datex_priority_over_volume_never_blended():
    datex = _road("normal", source="datex", age_min=5, travel=38.0)
    volume = _road("congested", age_min=30)
    # Both fresh: DATEX wins outright — the congested volume reading is NOT blended in.
    d = decide(
        now=NOW,
        road_observations=[datex, volume],
        departures=_normal_board(),
        rail_observed_at=NOW,
    )
    assert (d.state, d.road_source) == (State.NO_ADVANTAGE, "datex")
    # DATEX stale: the fresh volume observation takes over.
    stale_datex = _road("normal", source="datex", age_min=30, travel=38.0)
    d2 = decide(
        now=NOW,
        road_observations=[stale_datex, volume],
        departures=_normal_board(),
        rail_observed_at=NOW,
    )
    assert (d2.state, d2.road_source) == (State.TRAIN_RECOMMENDED, "volume")


def test_road_state_since_threads_through():
    since = NOW - timedelta(hours=2)
    d = decide(
        now=NOW,
        road_observations=[_road("congested")],
        departures=_normal_board(),
        rail_observed_at=NOW,
        road_state_since=since,
    )
    assert d.state_since == since


def test_state_since_not_borrowed_by_winning_datex():
    # road_state_since is the VOLUME provider's run start; a winning DATEX observation must
    # use its own observed_at, never the volume pipeline's run start.
    datex = _road("congested", source="datex", age_min=5, travel=60.0)
    volume_run_start = NOW - timedelta(hours=3)
    d = decide(
        now=NOW,
        road_observations=[datex, _road("congested", age_min=30)],
        departures=_normal_board(),
        rail_observed_at=NOW,
        road_state_since=volume_run_start,
    )
    assert d.road_source == "datex"
    assert d.state_since == datex.observed_at
    assert d.state_since != volume_run_start


# ---- Volume provider: anti-flap + baselines ------------------------------------------------------
def _baseline_cells(point="97858V180819", direction="Oslo", median=1000, p10=700):
    """Baseline rows for every weekday hour so any test hour resolves."""
    return {
        (point, dow, hour): {"direction": direction, "median": median, "p10": p10}
        for dow in range(7)
        for hour in range(24)
    }


def _hour_row(point, hour_start, volume, heading="Oslo", coverage=100.0):
    return HourVolume(
        point_id=point,
        hour_start=hour_start,
        hour_end=hour_start + timedelta(hours=1),
        heading=heading,
        volume=volume,
        coverage_pct=coverage,
    )


def test_volume_query_is_brace_balanced():
    """Regression: the query builder used to emit one `}` too many per point, which closed the
    GraphQL document early. Trafikkdata then rejected the whole request ("extra tokens ...
    Offending token '}'" for one point, "'p1'" for more), so EVERY live volume fetch failed
    while the TTL cache logged one error per pod and served the previous value. Assert the
    document is balanced and never closes before the end, for one point and for many."""
    f = datetime(2026, 7, 8, 6, 0, tzinfo=UTC)
    t = datetime(2026, 7, 8, 8, 0, tzinfo=UTC)
    for n in (1, 2, 5):
        q = reisevalg._volume_query([f"P{i}" for i in range(n)], f, t)
        assert q.count("{") == q.count("}"), f"unbalanced braces for {n} point(s): {q}"
        depth = 0
        for pos, ch in enumerate(q):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                assert depth > 0 or pos == len(q) - 1, (
                    f"document closed at char {pos} of {len(q)} for {n} point(s); "
                    f"trailing garbage: {q[pos + 1:pos + 20]!r}"
                )
        assert depth == 0
        # Every point must be addressable in the response by its alias.
        for i in range(n):
            assert f"p{i}: trafficData(" in q


def test_volume_query_requests_every_point_once():
    """One aliased call covers the whole corridor -- not one request per point."""
    f = datetime(2026, 7, 8, 6, 0, tzinfo=UTC)
    t = datetime(2026, 7, 8, 8, 0, tzinfo=UTC)
    q = reisevalg._volume_query(["AAA", "BBB", "CCC"], f, t)
    assert q.count("trafficData(") == 3
    assert q.count("byHour(") == 3
    for pid in ("AAA", "BBB", "CCC"):
        assert f'trafficRegistrationPointId: "{pid}"' in q


def test_volume_hour_states_deficit_direction_and_coverage():
    base = _baseline_cells()
    h6, h7 = _oslo(2026, 7, 8, 6, 0), _oslo(2026, 7, 8, 7, 0)
    rows = [
        _hour_row("97858V180819", h6, 500),  # 50 % below median -> deficit
        _hour_row("97858V180819", h7, 990),  # near median -> normal
        _hour_row("97858V180819", h7, 100, heading="Drammen"),  # wrong direction: ignored
        _hour_row("97858V180819", h6, 100, coverage=10.0),  # low coverage: ignored
    ]
    states = volume_hour_states(rows, base)
    assert states == [
        (h6.astimezone(UTC) + timedelta(hours=1), "congested"),
        (h7.astimezone(UTC) + timedelta(hours=1), "normal"),
    ]


def test_volume_judgment_anti_flap_and_state_since():
    h = lambda i: _oslo(2026, 7, 8, i, 0).astimezone(UTC) + timedelta(hours=1)
    # Deficit only in the newest observation: persistence (N=2) unmet -> "normal", no nudge.
    obs, _run_start = volume_judgment([(h(6), "normal"), (h(7), "congested")])
    assert obs.state == "normal"
    assert obs.confidence == "low" and obs.travel_time_min is None
    # Deficit holds over the last N observations -> "congested"; state_since is the first
    # observation of the current run, and observed_at the end of the newest hour.
    obs2, run_start2 = volume_judgment(
        [(h(5), "normal"), (h(6), "congested"), (h(7), "congested")]
    )
    assert obs2.state == "congested"
    assert run_start2 == h(6)
    assert obs2.observed_at == h(7)


def test_volume_judgment_requires_adjacent_hours():
    # The backtested d15 rule (LAG(hour) = hour - 1) demands CONSECUTIVE congested hours:
    # congested at H-3 and H-1 with a gap (no usable hour between) does not make a run.
    h = lambda i: _oslo(2026, 7, 8, i, 0).astimezone(UTC) + timedelta(hours=1)
    obs, _ = volume_judgment([(h(4), "congested"), (h(6), "congested")])
    assert obs.state == "normal"
    # Consecutive congested hours -> congested, as before.
    obs2, _ = volume_judgment([(h(5), "congested"), (h(6), "congested")])
    assert obs2.state == "congested"


def test_volume_provider_unavailable_without_baselines(monkeypatch):
    monkeypatch.setattr(reisevalg, "_baselines", {})
    monkeypatch.setattr(reisevalg, "_baselines_built_at", None)
    reisevalg._volume_cache.reset()
    assert reisevalg._volume_provider() is None


def test_volume_provider_degrades_on_overage_baselines(monkeypatch):
    monkeypatch.setattr(reisevalg, "_baselines", _baseline_cells())
    monkeypatch.setattr(
        reisevalg, "_baselines_built_at", NOW - timedelta(days=400)
    )  # structural-drift guard: too old to trust
    monkeypatch.setattr(reisevalg, "_now", lambda: NOW)
    reisevalg._volume_cache.reset()
    assert reisevalg._volume_provider() is None


def test_refresh_baselines_tolerates_missing_table(monkeypatch):
    class _FailingBQ:
        def query(self, q):
            raise RuntimeError("Not found: Table disruptions.svv_baselines")

    monkeypatch.setattr(reisevalg, "_baselines", {})
    monkeypatch.setattr(reisevalg, "_bq_client", _FailingBQ())
    reisevalg.refresh_baselines()  # must not raise
    assert reisevalg._get_baselines()[0] == {}


def test_refresh_baselines_loads_rows(monkeypatch):
    built = NOW - timedelta(days=3)
    row = SimpleNamespace(
        point_id="97858V180819", direction="Oslo", dow=2, hour=7,
        median_volume=1000, p10_volume=700, built_at=built,
    )

    class _BQ:
        def query(self, q):
            return SimpleNamespace(result=lambda timeout=None: [row])

    monkeypatch.setattr(reisevalg, "_bq_client", _BQ())
    monkeypatch.setattr(reisevalg, "_baselines", {})
    monkeypatch.setattr(reisevalg, "_baselines_built_at", None)
    reisevalg.refresh_baselines()
    table, built_at = reisevalg._get_baselines()
    assert table[("97858V180819", 2, 7)]["median"] == 1000
    assert built_at == built
    monkeypatch.setattr(reisevalg, "_baselines", {})  # leave clean for other tests


def test_datex_stub_reports_unavailable(monkeypatch):
    # Env unset (popped before import) -> unavailable.
    assert reisevalg._datex_provider() is None
    # Even with credentials, the fetch/parse is deferred: the stub stays unavailable rather
    # than pretending to measure travel times.
    monkeypatch.setattr(reisevalg, "_SVV_DATEX_USER", "user")
    monkeypatch.setattr(reisevalg, "_SVV_DATEX_PASSWORD", "pass")
    assert reisevalg._datex_provider() is None


# ---- _TTLCache + HTTP fetchers ---------------------------------------------------------------------
def test_ttl_cache_single_flight_serves_previous_value():
    cache = reisevalg._TTLCache("test_single_flight", timedelta(seconds=90))
    earlier = NOW - timedelta(minutes=5)
    cache.get(lambda: "seeded", earlier)  # stale by NOW (TTL 90 s)
    cache._refreshing = True  # simulate another caller's in-flight refresh

    def must_not_fetch():
        raise AssertionError("fetch invoked despite an in-flight refresh")

    value, fetched_at = cache.get(must_not_fetch, NOW)
    assert (value, fetched_at) == ("seeded", earlier)


def test_ttl_cache_failure_cooldown_suppresses_retry():
    cache = reisevalg._TTLCache("test_cooldown", timedelta(seconds=90))
    earlier = NOW - timedelta(minutes=5)
    cache.get(lambda: "seeded", earlier)
    calls = {"n": 0}

    def failing():
        calls["n"] += 1
        raise RuntimeError("upstream down (test)")

    # Stale -> one fetch attempted; it fails, the previous value is served with OLD fetched_at.
    assert cache.get(failing, NOW) == ("seeded", earlier)
    assert calls["n"] == 1
    # Within the failure cooldown the fetcher is NOT invoked again (no retry/log storm).
    assert cache.get(failing, NOW + timedelta(seconds=5)) == ("seeded", earlier)
    assert calls["n"] == 1
    # After the cooldown the next get() retries (this also proves _refreshing was released).
    cache.get(failing, NOW + timedelta(seconds=45))
    assert calls["n"] == 2
    # A successful fetch clears the cooldown and swaps the value in.
    later = NOW + timedelta(seconds=90)
    assert cache.get(lambda: "recovered", later) == ("recovered", later)


def test_post_json_raises_on_graphql_errors(monkeypatch):
    import io
    import json as _json
    import urllib.request as _urllib_request

    class _FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def _fake_urlopen(body):
        return lambda req, timeout=None: _FakeResponse(_json.dumps(body).encode("utf-8"))

    good = {"data": {"stopPlace": {"estimatedCalls": []}}}
    monkeypatch.setattr(_urllib_request, "urlopen", _fake_urlopen(good))
    assert reisevalg._post_json("https://jp.test/", {}, {}) == good
    # 200-with-errors, missing data key, and null data must all raise (so the TTL caches take
    # the failure path and serve the previous value) instead of parsing as an empty board.
    for bad in (
        {"errors": [{"message": "boom"}], "data": None},
        {"no_data_envelope": 1},
        {"data": None},
    ):
        monkeypatch.setattr(_urllib_request, "urlopen", _fake_urlopen(bad))
        try:
            reisevalg._post_json("https://jp.test/", {}, {})
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"expected RuntimeError for body {bad}")


def test_car_fetcher_raises_on_empty_trip_patterns(monkeypatch):
    monkeypatch.setattr(
        reisevalg, "_post_json", lambda *a, **k: {"data": {"trip": {"tripPatterns": []}}}
    )
    try:
        reisevalg._default_car_fetcher()
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError on empty tripPatterns")
    # Through the cache, that failure preserves the previous good baseline (no None-for-6h).
    cache = reisevalg._TTLCache("test_car_baseline", timedelta(seconds=90))
    earlier = NOW - timedelta(minutes=5)
    cache.get(lambda: 36.0, earlier)
    assert cache.get(reisevalg._default_car_fetcher, NOW) == (36.0, earlier)


# ---- Board parsing --------------------------------------------------------------------------------
def _jp_call(
    *,
    line="VYG:Line:R13",
    mode="rail",
    dep="2026-07-08T08:10:00+02:00",
    cancelled=False,
    dsj="VYG:DatedServiceJourney:81613_DRM-DAL_26-07-08",
    oslo_arr="2026-07-08T08:45:00+02:00",
    oslo_expected=None,
    oslo_before_dep=False,
):
    """One estimatedCalls entry in the live JP shape (verified with curl 2026-07-11)."""
    calls = []
    if oslo_arr is not None:
        arr = "2026-07-08T07:40:00+02:00" if oslo_before_dep else oslo_arr
        calls.append(
            {
                "quay": {"stopPlace": {"id": "NSR:StopPlace:337"}},
                "aimedArrivalTime": arr,
                "expectedArrivalTime": oslo_expected or arr,
                "cancellation": False,
            }
        )
    return {
        "realtime": True,
        "cancellation": cancelled,
        "aimedDepartureTime": dep,
        "expectedDepartureTime": dep,
        "destinationDisplay": {"frontText": "Oslo S"},
        "serviceJourney": {"transportMode": mode, "line": {"id": line, "publicCode": line.rsplit(":", 1)[-1]}},
        "datedServiceJourney": {"id": dsj, "estimatedCalls": calls},
    }


def test_parse_board_direction_and_modes():
    data = {
        "data": {
            "stopPlace": {
                "estimatedCalls": [
                    _jp_call(),  # Oslo-bound corridor train
                    _jp_call(oslo_before_dep=True, dsj="VYG:DatedServiceJourney:drammen"),  # Drammen-bound: Oslo S call precedes Lier
                    _jp_call(oslo_arr=None, dsj="VYG:DatedServiceJourney:nooslo"),  # never reaches Oslo S
                    _jp_call(mode="bus"),  # replacement bus on the corridor line
                    _jp_call(line="BRA:Line:71", mode="bus", oslo_arr=None),  # local bus: dropped
                    _jp_call(cancelled=True, dsj="VYG:DatedServiceJourney:cxl", oslo_expected="2026-07-08T08:57:00+02:00"),
                ]
            }
        }
    }
    deps = parse_board(data)
    rails = [d for d in deps if d.is_corridor_rail]
    replacements = [d for d in deps if d.is_replacement]
    assert len(rails) == 2  # the healthy Oslo-bound train + the cancelled one
    assert len(replacements) == 1 and replacements[0].mode == "bus"
    cancelled = [d for d in rails if d.cancelled]
    assert len(cancelled) == 1
    assert round(cancelled[0].oslo_arrival_delay_min()) == 12
    healthy = next(d for d in rails if not d.cancelled)
    assert healthy.dated_journey_ref == "VYG:DatedServiceJourney:81613_DRM-DAL_26-07-08"
    assert healthy.aimed_dep == datetime(2026, 7, 8, 6, 10, tzinfo=UTC)


# ---- Endpoint: contract, isolation, end-to-end -----------------------------------------------------
def _iso_utc(value):
    return value is None or value.endswith(("Z", "+00:00"))


def _assert_contract(body):
    """The full response contract every state must satisfy."""
    assert body["state"] in {s.value for s in State}
    assert body["reason"] in {r.value for r in Reason}
    assert isinstance(body["copy"], str) and body["copy"]
    for key in ("as_of", "state_since", "valid_until"):
        assert _iso_utc(body[key]), (key, body[key])
    assert isinstance(body["road"], list)
    for entry in body["road"] + [body["rail"], body["car"]]:
        assert set(entry) >= {"name", "available", "observed_at", "confidence", "max_age_min", "stale"}
        assert _iso_utc(entry["observed_at"])
    assert isinstance(body["next_departures"], list)
    assert body["corridor"]["from_stop_place"] == "NSR:StopPlace:58835"
    assert body["corridor"]["to_stop_place"] == "NSR:StopPlace:337"
    assert "ikke" in body["parking"]["note"].lower()  # the "not live availability" caveat


def test_endpoint_recommended_full_contract(monkeypatch):
    flagged = "VYG:DatedServiceJourney:1_A"
    _wire(
        monkeypatch,
        providers=[lambda: _road("congested")],
        departures=_normal_board(),
        snapshot=lambda ref: [FakePred(0.3)] if ref == flagged else None,
    )
    r = client.get("/reisevalg-api/recommendation")
    assert r.status_code == 200
    body = r.json()
    _assert_contract(body)
    assert body["state"] == "TRAIN_RECOMMENDED"
    assert body["reason"] == "ROAD_CONGESTED_RAIL_OK"
    assert body["copy"] == "Bedre å ta toget i dag — kø på E18, togene går som normalt."
    assert body["road_source"] == "volume"
    assert body["saved_min"] is None  # volume proxy: no minutes claim
    assert body["car_baseline_min"] == 36
    assert len(body["next_departures"]) == 3
    first = body["next_departures"][0]
    assert first["dated_journey_ref"] == flagged
    assert first["predicted_cancel_probability"] == 0.3
    assert first["cancelled"] is False and first["mode"] == "rail"


def test_endpoint_all_five_states_serialize(monkeypatch):
    scenarios = [
        (
            {"providers": [lambda: _road("congested")], "departures": _normal_board()},
            "TRAIN_RECOMMENDED",
        ),
        (
            {"providers": [lambda: _road("normal")], "departures": _normal_board()},
            "NO_ADVANTAGE",
        ),
        (
            {
                "providers": [lambda: _road("normal")],
                "departures": [_dep(10, cancelled=True)] + _normal_board()[1:],
            },
            "TRAIN_DEGRADED",
        ),
        (
            {
                "now": SATURDAY,
                "providers": [lambda: _road("normal", now=SATURDAY)],
                "departures": _normal_board(now=SATURDAY),
            },
            "OUTSIDE_WINDOW",
        ),
        ({"providers": [], "departures": _normal_board()}, "UNKNOWN"),
    ]
    for kwargs, expected in scenarios:
        _wire(monkeypatch, **kwargs)
        body = client.get("/reisevalg-api/recommendation").json()
        _assert_contract(body)
        assert body["state"] == expected, (expected, body["reason"])


def test_endpoint_provider_exception_is_isolated(monkeypatch):
    def broken_provider():
        raise RuntimeError("svv unreachable (test)")

    _wire(monkeypatch, providers=[broken_provider], departures=_normal_board())
    r = client.get("/reisevalg-api/recommendation")
    assert r.status_code == 200
    body = r.json()
    # Road degraded to unavailable; the rest of the decision still computed honestly.
    assert (body["state"], body["reason"]) == ("UNKNOWN", "NO_ROAD_DATA")
    assert body["road"][0]["available"] is False
    assert body["rail"]["available"] is True


def test_endpoint_board_failure_yields_unknown_not_500(monkeypatch):
    _wire(monkeypatch, providers=[lambda: _road("congested")], board_error=True)
    r = client.get("/reisevalg-api/recommendation")
    assert r.status_code == 200
    body = r.json()
    assert (body["state"], body["reason"]) == ("UNKNOWN", "NO_RAIL_DATA")
    assert body["rail"]["available"] is False


def test_endpoint_stale_board_served_from_failing_refresh(monkeypatch):
    # Seed the board cache half an hour ago, then make the fetcher fail: the cache serves the
    # previous value with its OLD fetched_at, and the decision honestly reports RAIL_DATA_STALE.
    earlier = NOW - timedelta(minutes=30)
    _wire(monkeypatch, providers=[lambda: _road("congested")], board_error=True)
    reisevalg._board_cache.get(lambda: _normal_board(now=earlier), earlier)
    r = client.get("/reisevalg-api/recommendation")
    body = r.json()
    assert (body["state"], body["reason"]) == ("UNKNOWN", "RAIL_DATA_STALE")
    assert body["rail"]["stale"] is True


def test_endpoint_internal_error_returns_unknown_200(monkeypatch):
    _wire(monkeypatch, providers=[lambda: _road("congested")], departures=_normal_board())

    def boom(**kwargs):
        raise RuntimeError("decision blew up (test)")

    monkeypatch.setattr(reisevalg, "decide", boom)
    r = client.get("/reisevalg-api/recommendation")
    assert r.status_code == 200
    body = r.json()
    assert (body["state"], body["reason"]) == ("UNKNOWN", "INTERNAL_ERROR")
    _assert_contract(body)


def test_endpoint_datex_unset_falls_through_to_volume(monkeypatch):
    # Default provider order (DATEX stub first): with no credentials the stub reports
    # unavailable and the volume provider is still consulted — end-to-end through the real
    # volume pipeline (fake fetcher, injected baselines, persistence over the trailing window).
    monkeypatch.setattr(reisevalg, "_baselines", _baseline_cells())
    monkeypatch.setattr(reisevalg, "_baselines_built_at", NOW - timedelta(days=7))
    h6, h7 = _oslo(2026, 7, 8, 6, 0), _oslo(2026, 7, 8, 7, 0)

    def fake_volume_fetcher(point_ids, from_dt, to_dt):
        assert point_ids == ["97858V180819"]
        return [_hour_row("97858V180819", h6, 500), _hour_row("97858V180819", h7, 500)]

    _wire(
        monkeypatch,
        providers=None,  # default [datex stub, volume provider]
        departures=_normal_board(),
        volume_fetcher=fake_volume_fetcher,
    )
    body = client.get("/reisevalg-api/recommendation").json()
    assert body["state"] == "TRAIN_RECOMMENDED"
    assert body["road_source"] == "volume"
    # state_since = the first observation of the current congested run (end of hour 6-7).
    assert body["state_since"] == (
        (h6 + timedelta(hours=1)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    )
    names = [e["name"] for e in body["road"]]
    assert names == ["datex", "volume"]
    assert body["road"][0]["available"] is False  # the stub, honestly unavailable
    assert body["road"][1]["available"] is True


def test_endpoint_volume_anti_flap_end_to_end(monkeypatch):
    # Same pipeline, but the deficit appears only in the newest hour: persistence (N=2) unmet,
    # so the road reports "normal" -> NO_ADVANTAGE, never a flapping nudge.
    monkeypatch.setattr(reisevalg, "_baselines", _baseline_cells())
    monkeypatch.setattr(reisevalg, "_baselines_built_at", NOW - timedelta(days=7))
    h6, h7 = _oslo(2026, 7, 8, 6, 0), _oslo(2026, 7, 8, 7, 0)

    def fake_volume_fetcher(point_ids, from_dt, to_dt):
        return [_hour_row("97858V180819", h6, 990), _hour_row("97858V180819", h7, 500)]

    _wire(
        monkeypatch,
        providers=None,
        departures=_normal_board(),
        volume_fetcher=fake_volume_fetcher,
    )
    body = client.get("/reisevalg-api/recommendation").json()
    assert (body["state"], body["reason"]) == ("NO_ADVANTAGE", "ROAD_NORMAL")
    assert body["road_source"] == "volume"


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
        if "monkeypatch" in inspect.signature(t).parameters:
            mp = _MonkeyPatch()
            try:
                t(mp)
            finally:
                mp.undo()
        else:
            t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} reisevalg checks passed.")
