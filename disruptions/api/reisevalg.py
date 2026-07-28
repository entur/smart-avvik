"""Reisevalg-varsel — car vs train recommendation for the Lier/Drammen → Oslo S corridor.

``GET /reisevalg-api/recommendation`` answers, for the fixed E18/Drammenbanen morning corridor,
the question in docs/bil-avvik: "is the train the better choice right now?". It combines

* a **road signal** behind a provider interface (:class:`RoadObservation`): a Trafikkdata
  hourly-volume anomaly provider that works today (inherently lagged — the lag lives in the
  observation's ``observed_at``, never hidden by a cache), and a DATEX II travel-time provider
  that ships as an interface-honoring stub until SVV credentials exist. Strict priority — DATEX
  when fresh, else volumes, never blended;
* a **rail signal** from the Journey Planner v3 departure board at Lier (`estimatedCalls` —
  NOT the `trip` query, which omits cancelled journeys and would make "not cancelled" vacuously
  true during a real stop), arrival-side gated at Oslo S, plus the in-process per-call
  prediction snapshot injected from main.py (the snapshot can only demote, never promote);
  "trains normal" additionally requires realtime evidence on at least one inspected departure —
  a timetable-only board (realtime outage) yields UNKNOWN, never a nudge;
* a **car baseline** from Journey Planner's car routing (free-flow; the minutes-saved number is
  only claimable when a road provider supplies an actual travel time).

Five states, in explicit precedence: ``OUTSIDE_WINDOW`` short-circuits (except a planned
closure — zero rail departures but replacement buses present — which is ``TRAIN_DEGRADED`` /
``RAIL_CLOSURE``); then ``TRAIN_DEGRADED`` > ``TRAIN_RECOMMENDED`` > ``NO_ADVANTAGE`` >
``UNKNOWN``. Rail-degraded wins over road-normal, and is reachable with zero road data. Stale
or missing input yields ``UNKNOWN``, never a false nudge.

Design notes
------------
* **The endpoint is plain ``def``, NOT ``async def``** — a deliberate deviation from the
  otherwise-mirrored varsel template: it does blocking ``urllib`` I/O, which must run in the
  anyio threadpool, never on the single-worker event loop (it would freeze ``/journey``,
  ``/varsel-api`` and the ``/health`` liveness probe).
* All external fetches are TTL-cached with per-cache locks and **single-flight** refresh
  (concurrent callers serve the previous value instead of stacking fetches), fetch outside the
  lock, swap under it. Every HTTP call has a timeout ≤ 3 s. No BigQuery on the request path:
  the ``disruptions.svv_baselines`` lookup is loaded by :func:`refresh_baselines` from the
  poller/lifespan (the ``refresh_impact_lookup`` precedent).
* The decision core (:func:`decide`) is a **pure function of (cached observations, now)** —
  recomputed per request, so "never serve a stale nudge" is automatic. Anti-flap is
  observation-based: "congested" requires the volume deficit to hold over the last
  ``PERSISTENCE_N`` hourly observations, stateless per request and restart-proof.
* Every subsystem failure is isolated (failed source → that source unavailable → honest
  ``UNKNOWN``); the endpoint always returns 200.

No import of main.py (avoids a cycle): main injects the per-call prediction snapshot lookup
and the BigQuery client via :func:`configure`; tests inject fake providers/fetchers the same
way. Decision constants below are literals-in-code (the ``varsel.threshold_for`` precedent);
the ones marked PLACEHOLDER are pending the U2 backtest and must be re-derived offline, never
fitted in the serving path.
"""

import json
import logging
import os
import re
import threading
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_OSLO = ZoneInfo("Europe/Oslo")

# ---- Corridor (fixed, per the concept PDF; a second corridor later is configuration work) ----
CORRIDOR_FROM_STOP = "NSR:StopPlace:58835"  # Lier stasjon (Journey Planner id)
CORRIDOR_FROM_NAME = "Lier stasjon"
CORRIDOR_TO_STOP = "NSR:StopPlace:337"  # Oslo S
CORRIDOR_TO_NAME = "Oslo S"
CORRIDOR_ROAD = "E18 retning Oslo"
CORRIDOR_LINES = frozenset(
    {"VYG:Line:L1", "VYG:Line:R12", "VYG:Line:R13", "VYG:Line:RE11"}
)
# Car origin for the baseline route: Lier (park-and-ride side of the corridor).
CORRIDOR_CAR_ORIGIN = (59.7889, 10.2456)

# ---- Decision constants (derived offline, applied online — see module docstring) -------------
# Per-hour volume deficit that counts as congestion: volume below (1 - deficit) x the
# same-point/weekday/hour median. Queues push volume BELOW baseline downstream of the
# bottleneck (throughput collapse). U2 backtest (2026-07-11): 0.15 was the best of 8 variants
# (precision 0.60 on 6/10 flagged mornings vs bus-probe/SX ground truth); 0.25 scored 0.40.
CONGESTION_VOLUME_DEFICIT = 0.15
# Fraction of reporting corridor points that must show the deficit for an hour to count as
# corridor-congested. U2 backtest (2026-07-11): 0.5, from the winning d15 variant.
CONGESTION_POINT_FRAC = 0.5
# "Trains normal" arrival-side gate: expected Oslo S arrival delay of each of the next
# RAIL_CHECK_N departures must be <= this (delay accumulates after Lier — a departure-side gate
# would pass trains that arrive 10-15 min late).
RAIL_NORMAL_MAX_ARR_DELAY_MIN = 5
# How many upcoming corridor departures the rail gate inspects.
RAIL_CHECK_N = 3
# Minutes-saved floor: below this the saved-minutes number is omitted (not worth switching for).
MIN_SAVED_MIN = 10
# Snapshot veto bar: a per-call predicted cancellation probability above this for one of the
# next departures demotes to TRAIN_DEGRADED. PLACEHOLDER — pending U2 backtest.
ALERT_P_CANCEL = 0.5
# Weekday-morning nudge window (Europe/Oslo), the PDF's commute use case.
# PLACEHOLDER — pending U2 backtest.
MORNING_WINDOW_BEGIN = time(5, 30)
MORNING_WINDOW_END = time(9, 30)
# A nudge requires an actual rail departure within this many minutes (the user is deciding now).
DEPARTURE_LOOKAHEAD_MIN = 60
# Anti-flap: "congested" requires the deficit to hold over the last N hourly observations.
# U2 backtest (2026-07-11): N=2 (part of the winning d15 variant).
PERSISTENCE_N = 2
# Per-source staleness cutoffs. Hourly volumes are inherently old when published — a single
# short cutoff would make the volume provider stale-by-rule at every fetch, hence the wide
# volume window (usable up to ~4 h old, always confidence "low"). U1 measured the current-hour
# publication lag at 134-194 min (night sample; worst case + 60 min ≈ 250). Even so, U2's
# timing simulation shows rush-onset detection lands after the morning window — the wide cutoff
# keeps the observation *visible*, the precision gate below keeps it from ever nudging.
VOLUME_MAX_AGE_MIN = 250
DATEX_MAX_AGE_MIN = 15
RAIL_MAX_AGE_MIN = 10
# R5's honesty bar: volume-only road data may map "congested" to TRAIN_RECOMMENDED only if the
# U2 backtest shows >= this precision on flagged rush mornings; below the bar the volume signal
# routes to UNKNOWN for nudging purposes (measured value recorded in docs/bil-avvik/LEARNINGS.md).
VOLUME_PRECISION_BAR = 0.8
# U2 backtest (2026-07-11): best variant (d15) precision = 0.60 (6/10 flagged mornings
# confirmed congested against bus-probe + SX ground truth) — BELOW the bar. The volume provider
# is therefore backtest-evidence only: its "congested" observation surfaces in the per-source
# breakdown but never fires the nudge. Re-derive by re-running data/queries/reisevalg_*.sql
# after baseline changes (or when a better road source lands) and update this literal.
VOLUME_PRECISION_MEASURED = 0.60
VOLUME_NUDGE_ENABLED = VOLUME_PRECISION_MEASURED >= VOLUME_PRECISION_BAR
# Structural-drift guard: baselines older than this are not trusted (E18 roadwork reshapes
# volumes on week scale; the offline re-derivation loop is enforced, not remembered).
BASELINES_MAX_AGE_DAYS = 180

# How long an UNKNOWN verdict is worth caching client-side before asking again.
_UNKNOWN_REVISIT = timedelta(minutes=5)
# Minimum Trafikkdata coverage percentage for an hour x point row to be usable.
_COVERAGE_MIN_PCT = 50.0
# Trailing window of hourly volumes fetched per refresh (>= PERSISTENCE_N + publication lag).
_VOLUME_WINDOW_H = 6

_HTTP_TIMEOUT_S = 3.0
_ET_CLIENT_NAME = "entur-kixavvik"
_JP_URL = "https://api.entur.io/journey-planner/v3/graphql"
_TRAFIKKDATA_URL = "https://trafikkdata-api.atlas.vegvesen.no/"
_TRAFIKKDATA_USER_AGENT = "entur-kixavvik thomas.oldervoll@gmail.com"

# Journey Planner departure-board query — verified live with curl 2026-07-11. The board (not
# `trip`) is deliberate: estimatedCalls carries cancellation state for cancelled and running
# trains alike, and datedServiceJourney.estimatedCalls exposes the whole call pattern so the
# expected ARRIVAL at Oslo S comes back in the same request (no per-train follow-up call).
_JP_BOARD_QUERY = """
{
  stopPlace(id: "%s") {
    estimatedCalls(
      numberOfDepartures: 10
      timeRange: 7200
      arrivalDeparture: departures
      whiteListedModes: [rail, bus, coach]
      includeCancelledTrips: true
    ) {
      realtime
      cancellation
      aimedDepartureTime
      expectedDepartureTime
      destinationDisplay { frontText }
      serviceJourney {
        transportMode
        line { id publicCode }
      }
      datedServiceJourney {
        id
        estimatedCalls {
          quay { stopPlace { id } }
          aimedArrivalTime
          expectedArrivalTime
          cancellation
        }
      }
    }
  }
}
""" % CORRIDOR_FROM_STOP  # noqa: UP031  (%-format: the GraphQL body is all braces, which .format() would need doubled)

# Journey Planner car-baseline query — verified live with curl 2026-07-11 (Lier → Oslo S,
# 2131 s / 40.9 km free-flow). directMode car with no transit modes = pure car routing.
_JP_CAR_QUERY = """
{
  trip(
    from: {coordinates: {latitude: %s, longitude: %s}}
    to: {place: "%s"}
    modes: {directMode: car, transportModes: []}
  ) {
    tripPatterns { duration }
  }
}
""" % (CORRIDOR_CAR_ORIGIN[0], CORRIDOR_CAR_ORIGIN[1], CORRIDOR_TO_STOP)  # noqa: UP031  (see above)

# svv_baselines lookup (loaded by refresh_baselines, never on the request path). Built offline
# by data/queries/reisevalg_road_baseline.sql (U2): per point x toward-Oslo direction x
# weekday x hour median/p10 volumes, with built_at for the structural-drift age gate.
_BASELINES_QUERY = """
    SELECT point_id, direction, dow, hour, median_volume, p10_volume, built_at
    FROM `disruptions.svv_baselines`
"""


def _now() -> datetime:
    """The current instant (UTC). Module-level so tests can freeze time."""
    return datetime.now(UTC)


def _parse_ts(value: str | None) -> datetime | None:
    """Parse a Journey Planner / Trafikkdata ISO timestamp ('+02:00' offsets) to aware UTC."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except ValueError:
        return None


def _utc(dt: datetime | None) -> datetime | None:
    """Normalize an aware datetime to UTC for the response (freshness fields are ISO-8601 UTC)."""
    return dt.astimezone(UTC) if dt is not None else None


# ---- States and reasons ----------------------------------------------------------------------
class RecommendationState(str, Enum):
    """The five corridor states, in decision precedence (OUTSIDE_WINDOW short-circuits)."""

    TRAIN_RECOMMENDED = "TRAIN_RECOMMENDED"
    NO_ADVANTAGE = "NO_ADVANTAGE"
    TRAIN_DEGRADED = "TRAIN_DEGRADED"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    UNKNOWN = "UNKNOWN"


class RecommendationReason(str, Enum):
    """Machine-readable reason behind the state."""

    ROAD_CONGESTED_RAIL_OK = "ROAD_CONGESTED_RAIL_OK"
    ROAD_NORMAL = "ROAD_NORMAL"
    ROAD_DATA_STALE = "ROAD_DATA_STALE"
    NO_ROAD_DATA = "NO_ROAD_DATA"
    RAIL_CANCELLATIONS = "RAIL_CANCELLATIONS"
    RAIL_DELAYS = "RAIL_DELAYS"
    RAIL_CLOSURE = "RAIL_CLOSURE"
    RAIL_DATA_STALE = "RAIL_DATA_STALE"
    NO_RAIL_DATA = "NO_RAIL_DATA"
    NO_UPCOMING_DEPARTURE = "NO_UPCOMING_DEPARTURE"
    OUTSIDE_MORNING_WINDOW = "OUTSIDE_MORNING_WINDOW"
    VOLUME_BELOW_PRECISION_BAR = "VOLUME_BELOW_PRECISION_BAR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_State = RecommendationState
_Reason = RecommendationReason

# Norwegian user-facing copy per (state, reason); state-level fallback below.
_COPY: dict[tuple[RecommendationState, RecommendationReason], str] = {
    (_State.TRAIN_RECOMMENDED, _Reason.ROAD_CONGESTED_RAIL_OK):
        "Bedre å ta toget i dag — kø på E18, togene går som normalt.",
    (_State.NO_ADVANTAGE, _Reason.ROAD_NORMAL):
        "Ingen kø-gevinst nå — normal trafikk på E18, og togene går som normalt.",
    (_State.TRAIN_DEGRADED, _Reason.RAIL_CANCELLATIONS):
        "Ikke regn med toget nå — innstillinger på strekningen mot Oslo S.",
    (_State.TRAIN_DEGRADED, _Reason.RAIL_DELAYS):
        "Ikke regn med toget nå — forsinkelser inn mot Oslo S.",
    (_State.TRAIN_DEGRADED, _Reason.RAIL_CLOSURE):
        "Toget går ikke nå — buss for tog på strekningen.",
    (_State.OUTSIDE_WINDOW, _Reason.OUTSIDE_MORNING_WINDOW):
        "Utenfor morgenrushet — anbefalingen gjelder hverdager 05:30–09:30.",
    (_State.OUTSIDE_WINDOW, _Reason.NO_UPCOMING_DEPARTURE):
        "Ingen togavgang fra Lier mot Oslo S den nærmeste timen.",
    (_State.UNKNOWN, _Reason.ROAD_DATA_STALE):
        "Usikkert nå — veidataene er for gamle til å vurdere køen på E18.",
    (_State.UNKNOWN, _Reason.NO_ROAD_DATA):
        "Usikkert nå — mangler ferske veidata for E18.",
    (_State.UNKNOWN, _Reason.VOLUME_BELOW_PRECISION_BAR):
        "Mulig kø på E18, men datagrunnlaget er ikke presist nok til å anbefale togbytte.",
    (_State.UNKNOWN, _Reason.RAIL_DATA_STALE):
        "Usikkert nå — sanntidsdataene for tog er for gamle.",
    (_State.UNKNOWN, _Reason.NO_RAIL_DATA):
        "Usikkert nå — mangler sanntidsdata for tog.",
    (_State.UNKNOWN, _Reason.INTERNAL_ERROR):
        "Usikkert nå — teknisk feil ved beregning av anbefalingen.",
}
_STATE_COPY: dict[RecommendationState, str] = {
    _State.TRAIN_RECOMMENDED: "Bedre å ta toget i dag.",
    _State.NO_ADVANTAGE: "Ingen fordel av å bytte i dag.",
    _State.TRAIN_DEGRADED: "Ikke regn med toget nå.",
    _State.OUTSIDE_WINDOW: "Utenfor morgenrushet.",
    _State.UNKNOWN: "Usikkert datagrunnlag — kan ikke gi noen anbefaling nå.",
}


def copy_for(state: RecommendationState, reason: RecommendationReason) -> str:
    """The Norwegian user-facing copy for a (state, reason) pair."""
    return _COPY.get((state, reason), _STATE_COPY[state])


# ---- Road observations and providers ---------------------------------------------------------
@dataclass(frozen=True)
class RoadObservation:
    """One road source's current judgment of the corridor.

    ``observed_at`` is the timestamp of the underlying DATA (for volumes: the end of the newest
    included hour) — deliberately distinct from any cache's fetch time, so the inherent
    publication lag of hourly data is visible to the staleness gate, never hidden.
    """

    source: str  # "datex" | "volume" (echoed as road_source in the response)
    state: str  # "congested" | "normal"
    observed_at: datetime  # UTC timestamp of the newest underlying data point
    confidence: str  # "low" (volume proxy) | "high" (real travel times)
    travel_time_min: float | None  # actual drive time when the source measures it (DATEX only)
    backtested: bool  # whether this signal family is validated by the offline backtest


# Per-source data-age cutoffs for the aggregation (first FRESH observation wins).
_ROAD_MAX_AGE_MIN: dict[str, int] = {"datex": DATEX_MAX_AGE_MIN, "volume": VOLUME_MAX_AGE_MIN}


def _road_max_age(source: str) -> int:
    """Freshness cutoff for a source; unknown sources get the strictest (DATEX) cutoff."""
    return _ROAD_MAX_AGE_MIN.get(source, DATEX_MAX_AGE_MIN)


# ---- DATEX II travel-time provider (interface-honoring STUB) ---------------------------------
# SVV's real travel-time feed (DATEX II GetTravelTimeData, ~5-min cadence, E18 Oslo–Aust-Agder
# covered) requires a registered account and has no historical archive. The access request is a
# day-1 parallel action (plan U1); until credentials exist this provider reports unavailable.
# Deferred implementation rules (must hold when the real fetch lands):
#   * Credentials are read once at import from SVV_DATEX_USER / SVV_DATEX_PASSWORD (the varsel
#     VAPID precedent) and sent ONLY as an `Authorization: Basic ...` header built from them —
#     never embedded in a URL.
#   * Failure logs carry the exception type and HTTP status only — never the request URL or
#     headers (either could leak the credentials).
#   * Observations: source "datex", confidence "high", travel_time_min from the feed,
#     backtested False — there is no DATEX archive, so the offline backtest can only ever
#     validate the volume family.
_SVV_DATEX_USER = os.environ.get("SVV_DATEX_USER")
_SVV_DATEX_PASSWORD = os.environ.get("SVV_DATEX_PASSWORD")


def _datex_provider() -> RoadObservation | None:
    """DATEX travel-time observation — stub: always unavailable until access is granted."""
    if not (_SVV_DATEX_USER and _SVV_DATEX_PASSWORD):
        return None  # not activated: no credentials configured
    # Credentials present, but the real fetch/parse is deferred until the feed can be
    # exercised against real data (plan scope boundary). Report unavailable, never pretend.
    return None


_datex_provider.road_source_name = "datex"


# ---- svv_baselines lookup (BigQuery, poller-loaded — never on the request path) ---------------
_baselines_lock = threading.Lock()
_baselines: dict[tuple[str, int, int], dict[str, Any]] = {}
_baselines_built_at: datetime | None = None
_baselines_refreshed_at: datetime | None = None

_bq_client: Any | None = None  # injected via configure() from main.py


def refresh_baselines() -> None:
    """Reload the (point_id, dow, hour) -> volume-baseline lookup from BigQuery.

    Mirrors main.refresh_impact_lookup (eager tolerant call in lifespan + isolated refresh in
    the poller tick). Tolerant by design: the table may not exist until the U2 backtest lands —
    any failure logs and keeps the previous lookup (initially empty), which makes the volume
    provider report the road side as unavailable rather than erroring.
    """
    global _baselines_built_at, _baselines_refreshed_at
    if _bq_client is None:
        logger.info("No BigQuery client configured; svv baselines not loaded")
        return
    try:
        rows = _bq_client.query(_BASELINES_QUERY).result()
        table: dict[tuple[str, int, int], dict[str, Any]] = {}
        built_at: datetime | None = None
        for r in rows:
            table[(r.point_id, r.dow, r.hour)] = {
                "direction": r.direction,
                "median": r.median_volume,
                "p10": r.p10_volume,
            }
            if r.built_at is not None and (built_at is None or r.built_at > built_at):
                built_at = r.built_at
    except Exception as exc:  # noqa: BLE001  (deliberate: any load failure degrades the road side to unavailable)
        logger.warning(
            "svv_baselines unavailable (%s: %s); reisevalg road volume side reports unavailable "
            "(expected until data/queries/reisevalg_road_baseline.sql has run)",
            type(exc).__name__,
            exc,
            extra={"fields": {"event": "reisevalg_baselines_unavailable"}},
        )
        return
    with _baselines_lock:
        global _baselines
        _baselines = table
        _baselines_built_at = built_at
        _baselines_refreshed_at = datetime.now(UTC)
    logger.info(
        "Refreshed svv baselines: %d cells (built_at=%s)",
        len(table),
        built_at,
        extra={"fields": {"event": "reisevalg_baselines_refreshed"}},
    )


def _get_baselines() -> tuple[dict[tuple[str, int, int], dict[str, Any]], datetime | None]:
    with _baselines_lock:
        return _baselines, _baselines_built_at


# ---- Single-flight TTL cache ------------------------------------------------------------------
# Failure cooldown: after a failed fetch, get() serves the previous value without re-attempting
# for this long — prevents per-request retry+log storms against JP/SVV during upstream
# degradation (code review 2026-07-11: single-flight stops concurrent stacking, not sequential
# per-request retries).
_FAILURE_COOLDOWN = timedelta(seconds=30)


class _TTLCache:
    """Module-level input cache: TTL + fetched_at, per-cache lock, single-flight refresh.

    ``get`` serves the cached value while fresh; when stale, exactly one caller refreshes
    (fetch OUTSIDE the lock, swap under it) while concurrent callers serve the previous value
    immediately. A failing fetch logs (``event=reisevalg_<name>_error``) and serves the previous
    value with its OLD fetched_at — staleness is judged by the caller against fetched_at / the
    data's own observed_at, never masked here. After a failure, ``_FAILURE_COOLDOWN`` suppresses
    new fetch attempts (still serving the previous value); a successful fetch clears it.
    """

    def __init__(self, name: str, ttl: timedelta):
        self._name = name
        self._ttl = ttl
        self._lock = threading.Lock()
        self._value: Any = None
        self._fetched_at: datetime | None = None
        self._refreshing = False
        self._last_failure_at: datetime | None = None

    def get(self, fetch: Callable[[], Any], now: datetime) -> tuple[Any, datetime | None]:
        with self._lock:
            if self._fetched_at is not None and now - self._fetched_at < self._ttl:
                return self._value, self._fetched_at
            if self._refreshing:  # single-flight: another caller is already fetching
                return self._value, self._fetched_at
            if (
                self._last_failure_at is not None
                and now - self._last_failure_at < _FAILURE_COOLDOWN
            ):  # failure cooldown: don't hammer a degraded upstream on every request
                return self._value, self._fetched_at
            self._refreshing = True
        try:
            try:
                value = fetch()
            except Exception:
                logger.exception(
                    "reisevalg %s fetch failed; serving previous value",
                    self._name,
                    extra={"fields": {"event": f"reisevalg_{self._name}_error"}},
                )
                with self._lock:
                    self._last_failure_at = now
                    return self._value, self._fetched_at
            with self._lock:
                self._value = value
                self._fetched_at = now
                self._last_failure_at = None
                return value, self._fetched_at
        finally:
            # Always release the single-flight marker — even on exceptions the inner handler
            # doesn't catch (BaseException, a raising log handler) — or every later get()
            # would serve the stale value forever.
            with self._lock:
                self._refreshing = False

    def reset(self) -> None:
        """Drop the cached value (used by tests between scenarios)."""
        with self._lock:
            self._value = None
            self._fetched_at = None
            self._refreshing = False
            self._last_failure_at = None


# TTLs: the board is realtime (short); volumes only change hourly (a ~15 min poll is plenty and
# stays far below any Trafikkdata rate concern); the car free-flow baseline is near-static.
_board_cache = _TTLCache("board", timedelta(seconds=90))
_volume_cache = _TTLCache("volume", timedelta(minutes=15))
_car_cache = _TTLCache("car_baseline", timedelta(hours=6))

# state_since support for the volume source: the start of the current same-state run, as
# computed from the trailing hourly observations at the last volume refresh.
_volume_run_start: datetime | None = None


# ---- HTTP -------------------------------------------------------------------------------------
def _post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    """POST a JSON body and parse the JSON response. Timeout <= 3 s on every call.

    GraphQL servers legitimately answer HTTP 200 with an ``errors`` array (or without a usable
    ``data`` envelope — both JP and Trafikkdata wrap results in ``{"data": ...}``). Raising here
    (mirroring ``data/svv/download_volumes.py``'s ``gql`` guard) routes those responses through
    the :class:`_TTLCache` failure path — serve the previous value — instead of parsing them
    into an empty board/None and caching that as fresh success.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if isinstance(body, dict) and (
        body.get("errors") or "data" not in body or body.get("data") is None
    ):
        raise RuntimeError(
            f"GraphQL error response: {str(body.get('errors', 'no data key'))[:200]}"
        )
    return body


# ---- Rail: Journey Planner departure board ----------------------------------------------------
@dataclass(frozen=True)
class Departure:
    """One board row at Lier, with its Oslo S arrival resolved from the same JP response."""

    dated_journey_ref: str | None  # "VYG:DatedServiceJourney:..." — joins the snapshot
    line: str | None  # "VYG:Line:R13"
    public_code: str | None  # "R13"
    mode: str | None  # "rail" | "bus" | "coach" | ...
    destination: str | None
    realtime: bool
    cancelled: bool
    aimed_dep: datetime | None  # UTC
    expected_dep: datetime | None  # UTC
    oslo_aimed_arr: datetime | None  # UTC; None when the journey never reaches Oslo S later
    oslo_expected_arr: datetime | None  # UTC

    @property
    def is_corridor_rail(self) -> bool:
        """A rail departure on a corridor line actually heading toward Oslo S (the Oslo S call
        exists later in the pattern — the board also lists the Drammen-bound direction)."""
        return (
            self.mode == "rail"
            and self.line in CORRIDOR_LINES
            and self.oslo_aimed_arr is not None
        )

    @property
    def is_replacement(self) -> bool:
        """A non-rail row standing in on the corridor (buss for tog keeps the rail line ref
        and/or calls at Oslo S later). Never counts as a rail departure."""
        return self.mode != "rail" and (
            self.line in CORRIDOR_LINES or self.oslo_aimed_arr is not None
        )

    @property
    def departure_at(self) -> datetime | None:
        return self.expected_dep or self.aimed_dep

    def oslo_arrival_delay_min(self) -> float | None:
        """Expected-vs-aimed arrival delay at Oslo S in minutes; None without realtime arrival."""
        if self.oslo_aimed_arr is None or self.oslo_expected_arr is None:
            return None
        return (self.oslo_expected_arr - self.oslo_aimed_arr).total_seconds() / 60.0


def parse_board(data: dict) -> list[Departure]:
    """Parse the JP board response into :class:`Departure` rows (corridor rows only)."""
    stop = (data.get("data") or {}).get("stopPlace") or {}
    out: list[Departure] = []
    for call in stop.get("estimatedCalls") or []:
        sj = call.get("serviceJourney") or {}
        line = (sj.get("line") or {}).get("id")
        aimed_dep = _parse_ts(call.get("aimedDepartureTime"))
        dsj = call.get("datedServiceJourney") or {}
        # The Oslo S call LATER than this departure is the direction discriminator: the board
        # also lists Drammen-bound trains, whose Oslo S call (if any) precedes Lier.
        oslo_aimed = oslo_expected = None
        oslo_cancelled = False
        for c in dsj.get("estimatedCalls") or []:
            sp = ((c.get("quay") or {}).get("stopPlace") or {}).get("id")
            if sp != CORRIDOR_TO_STOP:
                continue
            arr = _parse_ts(c.get("aimedArrivalTime"))
            if arr is None or (aimed_dep is not None and arr <= aimed_dep):
                continue  # Oslo S call before Lier: wrong direction
            oslo_aimed = arr
            oslo_expected = _parse_ts(c.get("expectedArrivalTime"))
            oslo_cancelled = bool(c.get("cancellation"))
            break
        dep = Departure(
            dated_journey_ref=dsj.get("id"),
            line=line,
            public_code=(sj.get("line") or {}).get("publicCode"),
            mode=sj.get("transportMode"),
            destination=(call.get("destinationDisplay") or {}).get("frontText"),
            realtime=bool(call.get("realtime")),
            # A cancelled Oslo S call also breaks the passenger's trip (partial cancellation).
            cancelled=bool(call.get("cancellation")) or oslo_cancelled,
            aimed_dep=aimed_dep,
            expected_dep=_parse_ts(call.get("expectedDepartureTime")),
            oslo_aimed_arr=oslo_aimed,
            oslo_expected_arr=oslo_expected,
        )
        if dep.is_corridor_rail or dep.is_replacement:
            out.append(dep)
    return out


def _default_board_fetcher() -> list[Departure]:
    data = _post_json(
        _JP_URL, {"query": _JP_BOARD_QUERY}, headers={"ET-Client-Name": _ET_CLIENT_NAME}
    )
    return parse_board(data)


def _default_car_fetcher() -> float:
    """Free-flow car minutes Lier -> Oslo S from JP car routing (near-static, cached hours)."""
    data = _post_json(
        _JP_URL, {"query": _JP_CAR_QUERY}, headers={"ET-Client-Name": _ET_CLIENT_NAME}
    )
    patterns = ((data.get("data") or {}).get("trip") or {}).get("tripPatterns") or []
    durations = [p.get("duration") for p in patterns if p.get("duration")]
    if not durations:
        # An empty/missing tripPatterns response is an upstream failure, not "no baseline":
        # raising routes it through the cache's failure path, which preserves the previous
        # good baseline instead of caching None as fresh success for the full 6 h TTL.
        raise RuntimeError("JP car routing returned no tripPatterns with a duration")
    return min(durations) / 60.0


# ---- Road: Trafikkdata volume-anomaly provider -------------------------------------------------
@dataclass(frozen=True)
class HourVolume:
    """One point x hour x direction volume row as fetched from Trafikkdata."""

    point_id: str
    hour_start: datetime  # UTC
    hour_end: datetime  # UTC
    heading: str | None
    volume: int | None
    coverage_pct: float | None


def _volume_query(point_ids: list[str], from_dt: datetime, to_dt: datetime) -> str:
    """One aliased Trafikkdata GraphQL query covering every corridor point, so the trailing
    window is a single call rather than one per point.

    Each `p<i>:` fragment must close exactly the nine blocks it opens (trafficData, volume,
    byHour, edges, node, byDirection, total, volumeNumbers, coverage) — the outer document
    braces are added by the return below. An earlier version emitted one `}` too many, which
    closed the document after `p0` and left `p1:` onward as trailing garbage; Trafikkdata
    rejected the whole query with "extra tokens ... Offending token 'p1'", so every
    multi-point fetch failed while a single-point one still parsed. test_volume_query_*
    pins the brace balance.
    """
    parts = []
    for i, pid in enumerate(point_ids):
        parts.append(
            f'p{i}: trafficData(trafficRegistrationPointId: "{pid}") {{ volume {{ '
            f'byHour(from: "{from_dt.isoformat()}", to: "{to_dt.isoformat()}") {{ '
            "edges { node { from to byDirection { heading "
            "total { volumeNumbers { volume } coverage { percentage } } } } } } } }"
        )
    return "{ " + " ".join(parts) + " }"


def _default_volume_fetcher(
    point_ids: list[str], from_dt: datetime, to_dt: datetime
) -> list[HourVolume]:
    """Fetch the trailing byHour window for all corridor points in one Trafikkdata call."""
    # Point ids come from our own BigQuery table, but sanitize anyway before string-building.
    safe_ids = [pid for pid in point_ids if re.fullmatch(r"[A-Za-z0-9]+", pid)]
    if not safe_ids:
        return []
    data = _post_json(
        _TRAFIKKDATA_URL,
        {"query": _volume_query(safe_ids, from_dt, to_dt)},
        headers={"User-Agent": _TRAFIKKDATA_USER_AGENT},
    )
    rows: list[HourVolume] = []
    payload = data.get("data") or {}
    for i, pid in enumerate(safe_ids):
        by_hour = (((payload.get(f"p{i}") or {}).get("volume") or {}).get("byHour")) or {}
        for edge in by_hour.get("edges") or []:
            node = edge.get("node") or {}
            hour_start = _parse_ts(node.get("from"))
            hour_end = _parse_ts(node.get("to"))
            if hour_start is None or hour_end is None:
                continue
            for d in node.get("byDirection") or []:
                total = d.get("total") or {}
                rows.append(
                    HourVolume(
                        point_id=pid,
                        hour_start=hour_start,
                        hour_end=hour_end,
                        heading=d.get("heading"),
                        volume=(total.get("volumeNumbers") or {}).get("volume"),
                        coverage_pct=(total.get("coverage") or {}).get("percentage"),
                    )
                )
    return rows


def volume_hour_states(
    rows: list[HourVolume], baselines: dict[tuple[str, int, int], dict[str, Any]]
) -> list[tuple[datetime, str]]:
    """Per-hour corridor congestion states from fetched volumes vs the offline baselines.

    Only rows in each point's toward-Oslo direction (the baseline row's ``direction``) with
    usable coverage count. An hour is corridor-"congested" when at least
    ``CONGESTION_POINT_FRAC`` of the reporting points show a volume deficit of
    ``CONGESTION_VOLUME_DEFICIT`` vs their same-weekday/hour median. Returns
    ``[(hour_end_utc, "congested"|"normal"), ...]`` sorted by hour; hours with no usable
    observation are omitted. Pure — hermetically testable with fake row lists.
    """
    by_hour: dict[datetime, list[bool]] = {}
    for r in rows:
        if r.volume is None:
            continue
        if r.coverage_pct is not None and r.coverage_pct < _COVERAGE_MIN_PCT:
            continue
        local = r.hour_start.astimezone(_OSLO)
        base = baselines.get((r.point_id, local.weekday(), local.hour))
        if base is None or base.get("direction") != r.heading:
            continue
        median = base.get("median")
        if not median or median <= 0:
            continue
        deficit = r.volume < median * (1.0 - CONGESTION_VOLUME_DEFICIT)
        by_hour.setdefault(_utc(r.hour_end), []).append(deficit)
    out: list[tuple[datetime, str]] = []
    for hour_end in sorted(by_hour):
        flags = by_hour[hour_end]
        congested = sum(flags) / len(flags) >= CONGESTION_POINT_FRAC
        out.append((hour_end, "congested" if congested else "normal"))
    return out


def volume_judgment(
    hour_states: list[tuple[datetime, str]],
) -> tuple[RoadObservation | None, datetime | None]:
    """Aggregate hourly states into one volume observation, with observation-based anti-flap.

    "Congested" requires the deficit to hold over the last ``PERSISTENCE_N`` observations AND
    those observations to be hour-adjacent; a deficit in only the newest hour reports "normal"
    (not yet persistent). ``observed_at`` is the end of the newest included hour — the data's
    own timestamp, so the publication lag is visible to the staleness gate. The second return
    value is the start of the current run of the reported state (feeds ``state_since``). Pure.
    """
    if not hour_states:
        return None, None
    states = sorted(hour_states)
    tail = states[-PERSISTENCE_N:]
    # Hour-adjacency: the backtested d15 variant (reisevalg_road_baseline.sql,
    # LAG(hour) = hour - 1) requires CONSECUTIVE congested hours — the measured 0.60 precision
    # only describes the adjacent-hours rule. A gap in usable hours breaks the run.
    adjacent = all(
        tail[i + 1][0] - tail[i][0] == timedelta(hours=1) for i in range(len(tail) - 1)
    )
    congested = (
        len(tail) >= PERSISTENCE_N
        and adjacent
        and all(s == "congested" for _, s in tail)
    )
    state = "congested" if congested else "normal"
    observed_at = states[-1][0]
    run_start: datetime | None = None
    for hour_end, s in reversed(states):
        if s == state:
            run_start = hour_end
        elif run_start is not None:
            break
    obs = RoadObservation(
        source="volume",
        state=state,
        observed_at=observed_at,
        confidence="low",  # hourly volumes are a queue *proxy*, never high confidence
        travel_time_min=None,  # volumes carry no drive time — no minutes-saved claim from here
        backtested=True,  # the volume family is what the U2 offline backtest validates
    )
    return obs, run_start or observed_at


def _compute_volume_observation(
    baselines: dict[tuple[str, int, int], dict[str, Any]], now: datetime
) -> tuple[RoadObservation | None, datetime | None]:
    """Fetch the trailing volume window and judge it (runs inside the volume cache refresh)."""
    point_ids = sorted({k[0] for k in baselines})
    local_now = now.astimezone(_OSLO)
    from_dt = (local_now - timedelta(hours=_VOLUME_WINDOW_H)).replace(
        minute=0, second=0, microsecond=0
    )
    rows = _volume_fetcher(point_ids, from_dt, local_now)
    return volume_judgment(volume_hour_states(rows, baselines))


def _volume_provider() -> RoadObservation | None:
    """Trafikkdata volume-anomaly observation, or None when the road side is unavailable
    (no baselines yet — the svv_baselines table lands with the U2 backtest — or over-age
    baselines, or no usable fetched hours)."""
    baselines, built_at = _get_baselines()
    if not baselines:
        return None
    now = _now()
    if built_at is not None and now - built_at > timedelta(days=BASELINES_MAX_AGE_DAYS):
        # Structural-drift guard: stale baselines (E18 roadwork moves the volume floor) must
        # degrade the road side to unavailable rather than keep firing (see KTDs).
        return None
    result, _fetched_at = _volume_cache.get(
        lambda: _compute_volume_observation(baselines, now), now
    )
    if result is None:
        return None
    obs, run_start = result
    global _volume_run_start
    _volume_run_start = run_start
    return obs


_volume_provider.road_source_name = "volume"


# ---- Injected/overridable collaborators (configure keeps main.py-free of cycles) --------------
_UNSET: Any = object()

# The per-call prediction snapshot lookup, injected from main.py: full dated journey ref
# ("VYG:DatedServiceJourney:...") -> per-call prediction rows (or None). Read-only.
_snapshot_lookup: Callable[[str], list | None] | None = None
_road_providers: list[Callable[[], RoadObservation | None]] = [_datex_provider, _volume_provider]
_board_fetcher: Callable[[], list[Departure] | None] = _default_board_fetcher
_car_fetcher: Callable[[], float | None] = _default_car_fetcher
_volume_fetcher: Callable[[list[str], datetime, datetime], list[HourVolume]] = (
    _default_volume_fetcher
)


def configure(
    *,
    snapshot_lookup: Callable[[str], list | None] | None = _UNSET,
    bq_client: Any | None = _UNSET,
    road_providers: list[Callable[[], RoadObservation | None]] | None = _UNSET,
    board_fetcher: Callable[[], list[Departure] | None] | None = _UNSET,
    car_fetcher: Callable[[], float | None] | None = _UNSET,
    volume_fetcher: Callable[[list[str], datetime, datetime], list[HourVolume]] | None = _UNSET,
) -> None:
    """Wire in collaborators. main.py injects ``snapshot_lookup`` + ``bq_client`` at startup;
    tests inject fake providers/fetchers. Omitted kwargs are left unchanged; passing ``None``
    for a fetcher/provider restores its default (for ``snapshot_lookup``/``bq_client``, ``None``
    means absent)."""
    global _snapshot_lookup, _bq_client, _road_providers
    global _board_fetcher, _car_fetcher, _volume_fetcher
    if snapshot_lookup is not _UNSET:
        _snapshot_lookup = snapshot_lookup
    if bq_client is not _UNSET:
        _bq_client = bq_client
    if road_providers is not _UNSET:
        _road_providers = (
            list(road_providers) if road_providers is not None
            else [_datex_provider, _volume_provider]
        )
    if board_fetcher is not _UNSET:
        _board_fetcher = board_fetcher if board_fetcher is not None else _default_board_fetcher
    if car_fetcher is not _UNSET:
        _car_fetcher = car_fetcher if car_fetcher is not None else _default_car_fetcher
    if volume_fetcher is not _UNSET:
        _volume_fetcher = (
            volume_fetcher if volume_fetcher is not None else _default_volume_fetcher
        )


# ---- Morning window ----------------------------------------------------------------------------
def in_morning_window(now: datetime) -> bool:
    """True on weekdays inside [MORNING_WINDOW_BEGIN, MORNING_WINDOW_END] Europe/Oslo."""
    local = now.astimezone(_OSLO)
    return local.weekday() < 5 and MORNING_WINDOW_BEGIN <= local.time() <= MORNING_WINDOW_END


def _next_window_start(now: datetime) -> datetime | None:
    """Start of the next weekday-morning window strictly after `now` (UTC)."""
    local = now.astimezone(_OSLO)
    for i in range(8):
        day = (local + timedelta(days=i)).date()
        if day.weekday() >= 5:
            continue
        start = datetime.combine(day, MORNING_WINDOW_BEGIN, tzinfo=_OSLO)
        if start > local:
            return _utc(start)
    return None


def _prev_window_end(now: datetime) -> datetime | None:
    """End of the most recent weekday-morning window at or before `now` (UTC)."""
    local = now.astimezone(_OSLO)
    for i in range(8):
        day = (local - timedelta(days=i)).date()
        if day.weekday() >= 5:
            continue
        end = datetime.combine(day, MORNING_WINDOW_END, tzinfo=_OSLO)
        if end <= local:
            return _utc(end)
    return None


def _window_end_today(now: datetime) -> datetime:
    """End of today's morning window (only meaningful when `now` is inside it), UTC."""
    local = now.astimezone(_OSLO)
    return _utc(datetime.combine(local.date(), MORNING_WINDOW_END, tzinfo=_OSLO))


# ---- Decision core (pure) -----------------------------------------------------------------------
@dataclass(frozen=True)
class Decision:
    """Everything :func:`decide` concluded — the endpoint only renders this."""

    state: RecommendationState
    reason: RecommendationReason
    saved_min: int | None = None
    car_delay_min: int | None = None
    state_since: datetime | None = None
    valid_until: datetime | None = None
    road_source: str | None = None
    road_stale: bool = False
    upcoming: tuple[Departure, ...] = ()
    predicted_cancel: dict[str, float] | None = None


def decide(
    *,
    now: datetime,
    road_observations: list[RoadObservation],
    departures: list[Departure] | None,
    rail_observed_at: datetime | None,
    car_baseline_min: float | None = None,
    snapshot_lookup: Callable[[str], list | None] | None = None,
    road_state_since: datetime | None = None,
) -> Decision:
    """The recommendation as a pure function of (cached observations, now).

    Precedence: OUTSIDE_WINDOW short-circuits (except planned closure -> TRAIN_DEGRADED /
    RAIL_CLOSURE); then TRAIN_DEGRADED > TRAIN_RECOMMENDED > NO_ADVANTAGE > UNKNOWN.
    Rail-degraded wins over road-normal, and is reachable with zero road data. The snapshot
    lookup can only demote (a predicted cancellation vetoes a JP-on-time board), never promote.
    """
    # Road aggregation: ordered providers, first FRESH observation wins (DATEX before volumes,
    # never blended). Staleness is per-source — hourly volumes are usable far longer than
    # 5-minute travel times.
    winning: RoadObservation | None = None
    road_stale = False
    for obs in road_observations:
        if now - obs.observed_at <= timedelta(minutes=_road_max_age(obs.source)):
            winning = obs
            break
        road_stale = True
    road_source = winning.source if winning is not None else None

    # car_delay is informational whenever a travel-time source is fresh (independent of state).
    car_delay_min = None
    if (
        winning is not None
        and winning.travel_time_min is not None
        and car_baseline_min is not None
    ):
        car_delay_min = max(0, round(winning.travel_time_min - car_baseline_min))

    # 1) Window short-circuit: outside the weekday-morning window nothing is recommended.
    if not in_morning_window(now):
        return Decision(
            state=_State.OUTSIDE_WINDOW,
            reason=_Reason.OUTSIDE_MORNING_WINDOW,
            car_delay_min=car_delay_min,
            state_since=_prev_window_end(now),
            valid_until=_next_window_start(now),
            road_source=road_source,
            road_stale=road_stale,
        )
    window_end = _window_end_today(now)

    # 2) Rail availability gate: without fresh board data no confident state exists — a stale
    # board must not produce a nudge NOR a degraded verdict.
    if departures is None or rail_observed_at is None:
        return Decision(
            state=_State.UNKNOWN,
            reason=_Reason.NO_RAIL_DATA,
            car_delay_min=car_delay_min,
            state_since=now,
            valid_until=now + _UNKNOWN_REVISIT,
            road_source=road_source,
            road_stale=road_stale,
        )
    if now - rail_observed_at > timedelta(minutes=RAIL_MAX_AGE_MIN):
        return Decision(
            state=_State.UNKNOWN,
            reason=_Reason.RAIL_DATA_STALE,
            car_delay_min=car_delay_min,
            state_since=now,
            valid_until=now + _UNKNOWN_REVISIT,
            road_source=road_source,
            road_stale=road_stale,
        )

    def _dep_key(d: Departure) -> datetime:
        return d.departure_at or now

    rail_up = sorted(
        (
            d
            for d in departures
            if d.is_corridor_rail and d.departure_at is not None and d.departure_at >= now
        ),
        key=_dep_key,
    )
    replacement_up = sorted(
        (
            d
            for d in departures
            if d.is_replacement and d.departure_at is not None and d.departure_at >= now
        ),
        key=_dep_key,
    )
    upcoming = tuple(sorted(rail_up + replacement_up, key=_dep_key)[:6])

    # 3) Departure-within-the-hour gate, with the planned-closure exception: zero rail-mode
    # departures but replacement services present is a closure (TRAIN_DEGRADED / RAIL_CLOSURE),
    # not OUTSIDE_WINDOW.
    within = [
        d
        for d in rail_up
        if d.departure_at <= now + timedelta(minutes=DEPARTURE_LOOKAHEAD_MIN)
    ]
    if not within:
        if not rail_up and replacement_up:
            return Decision(
                state=_State.TRAIN_DEGRADED,
                reason=_Reason.RAIL_CLOSURE,
                car_delay_min=car_delay_min,
                state_since=rail_observed_at,
                valid_until=min(
                    window_end, rail_observed_at + timedelta(minutes=RAIL_MAX_AGE_MIN)
                ),
                road_source=road_source,
                road_stale=road_stale,
                upcoming=upcoming,
            )
        return Decision(
            state=_State.OUTSIDE_WINDOW,
            reason=_Reason.NO_UPCOMING_DEPARTURE,
            car_delay_min=car_delay_min,
            state_since=now,
            valid_until=now + _UNKNOWN_REVISIT,
            road_source=road_source,
            road_stale=road_stale,
            upcoming=upcoming,
        )

    # 4) "Trains normal" over the next RAIL_CHECK_N departures — arrival-side gated at Oslo S,
    # with the snapshot veto (demote-only). All three checks per departure.
    next_deps = rail_up[:RAIL_CHECK_N]
    degraded_reason: RecommendationReason | None = None
    predicted_cancel: dict[str, float] = {}
    for d in next_deps:
        if degraded_reason is None and d.cancelled:
            degraded_reason = _Reason.RAIL_CANCELLATIONS
        delay = d.oslo_arrival_delay_min()
        if (
            degraded_reason is None
            and delay is not None
            and delay > RAIL_NORMAL_MAX_ARR_DELAY_MIN
        ):
            degraded_reason = _Reason.RAIL_DELAYS
        if snapshot_lookup is not None and d.dated_journey_ref:
            try:
                rows = snapshot_lookup(d.dated_journey_ref)
            except Exception:
                logger.exception(
                    "reisevalg snapshot lookup failed",
                    extra={"fields": {"event": "reisevalg_snapshot_error"}},
                )
                rows = None
            if rows:
                p = max((getattr(r, "p_cancel", 0.0) or 0.0) for r in rows)
                predicted_cancel[d.dated_journey_ref] = p
                if degraded_reason is None and p > ALERT_P_CANCEL:
                    # Predictive veto: the snapshot can only demote, never promote.
                    degraded_reason = _Reason.RAIL_CANCELLATIONS
    if degraded_reason is not None:
        return Decision(
            state=_State.TRAIN_DEGRADED,
            reason=degraded_reason,
            car_delay_min=car_delay_min,
            state_since=rail_observed_at,
            valid_until=min(window_end, rail_observed_at + timedelta(minutes=RAIL_MAX_AGE_MIN)),
            road_source=road_source,
            road_stale=road_stale,
            upcoming=upcoming,
            predicted_cancel=predicted_cancel,
        )

    # 4b) Realtime-evidence requirement: during a SIRI->JP realtime outage the board serves
    # pure timetable rows (realtime=false, aimed==expected, cancellations invisible) — "not
    # cancelled, on time" is then vacuous. Without realtime on any inspected departure,
    # "trains normal" is unknowable, never assertable. Explicit cancellations above still
    # demote first (a cancelled non-realtime row is real signal).
    if not any(d.realtime for d in next_deps):
        return Decision(
            state=_State.UNKNOWN,
            reason=_Reason.NO_RAIL_DATA,
            car_delay_min=car_delay_min,
            state_since=now,
            valid_until=now + _UNKNOWN_REVISIT,
            road_source=road_source,
            road_stale=road_stale,
            upcoming=upcoming,
            predicted_cancel=predicted_cancel,
        )

    # 5) Rail is normal — now the road side decides. No fresh road observation means UNKNOWN
    # (honesty rule: stale or missing input never yields a nudge).
    if winning is None:
        return Decision(
            state=_State.UNKNOWN,
            reason=_Reason.ROAD_DATA_STALE if road_stale else _Reason.NO_ROAD_DATA,
            car_delay_min=car_delay_min,
            state_since=now,
            valid_until=now + _UNKNOWN_REVISIT,
            road_stale=road_stale,
            upcoming=upcoming,
            predicted_cancel=predicted_cancel,
        )

    rail_valid = rail_observed_at + timedelta(minutes=RAIL_MAX_AGE_MIN)
    road_valid = winning.observed_at + timedelta(minutes=_road_max_age(winning.source))
    # road_state_since is the VOLUME provider's trailing-run start — apply it only when the
    # volume observation actually won; DATEX (or injected) observations use their own
    # observed_at (see the caller's comment in _build_recommendation).
    state_since = (
        road_state_since
        if winning.source == "volume" and road_state_since is not None
        else winning.observed_at
    )

    if winning.state == "congested":
        # R5 precision gate: a volume-only "congested" may never fire the nudge while the
        # measured backtest precision (VOLUME_PRECISION_MEASURED) is below the bar — it
        # surfaces honestly as UNKNOWN with its own reason instead of a low-confidence
        # recommendation. DATEX (a measured travel time) is not gated here.
        if winning.source == "volume" and not VOLUME_NUDGE_ENABLED:
            return Decision(
                state=_State.UNKNOWN,
                reason=_Reason.VOLUME_BELOW_PRECISION_BAR,
                car_delay_min=car_delay_min,
                state_since=state_since,
                valid_until=min(window_end, road_valid, rail_valid),
                road_source=road_source,
                road_stale=road_stale,
                upcoming=upcoming,
                predicted_cancel=predicted_cancel,
            )
        # Minutes saved is only claimable from a source that measures drive time (DATEX);
        # volumes never carry one. car_time_now − (wait + rail time) = car − (arrival − now).
        saved_min = None
        if winning.travel_time_min is not None:
            ref = next(
                (
                    d
                    for d in next_deps
                    if not d.cancelled
                    and (d.oslo_expected_arr or d.oslo_aimed_arr) is not None
                ),
                None,
            )
            if ref is not None:
                arr = ref.oslo_expected_arr or ref.oslo_aimed_arr
                rail_total_min = (arr - now).total_seconds() / 60.0
                saved = winning.travel_time_min - rail_total_min
                if saved >= MIN_SAVED_MIN:
                    saved_min = round(saved)
        return Decision(
            state=_State.TRAIN_RECOMMENDED,
            reason=_Reason.ROAD_CONGESTED_RAIL_OK,
            saved_min=saved_min,
            car_delay_min=car_delay_min,
            state_since=state_since,
            valid_until=min(window_end, road_valid, rail_valid),
            road_source=road_source,
            road_stale=road_stale,
            upcoming=upcoming,
            predicted_cancel=predicted_cancel,
        )
    return Decision(
        state=_State.NO_ADVANTAGE,
        reason=_Reason.ROAD_NORMAL,
        car_delay_min=car_delay_min,
        state_since=state_since,
        valid_until=min(window_end, road_valid, rail_valid),
        road_source=road_source,
        road_stale=road_stale,
        upcoming=upcoming,
        predicted_cancel=predicted_cancel,
    )


# ---- API models ---------------------------------------------------------------------------------
class SourceFreshness(BaseModel):
    """Freshness of one input source — every consumer must judge the data by its own age."""

    name: str = Field(description="Source name: 'datex', 'volume', 'journey-planner', 'journey-planner-car'.")
    available: bool = Field(description="Whether this source currently supplies an observation.")
    observed_at: datetime | None = Field(
        description="Timestamp of the underlying DATA (UTC) — for hourly volumes the end of the "
        "newest included hour, deliberately distinct from any cache fetch time.",
    )
    confidence: str | None = Field(
        description="'high' (measured travel times / realtime board) or 'low' (volume proxy); "
        "null when unavailable.",
    )
    max_age_min: int | None = Field(
        description="Per-source staleness cutoff in minutes; observations older than this are "
        "ignored by the decision. Null when no cutoff applies (near-static input).",
    )
    stale: bool = Field(
        description="True when an observation exists but is older than max_age_min (ignored).",
    )


class NextDeparture(BaseModel):
    """One upcoming corridor departure from Lier toward Oslo S (rail or rail-replacement)."""

    dated_journey_ref: str | None = Field(
        description="Dated journey ref (e.g. 'VYG:DatedServiceJourney:...'), joinable to /journey.",
    )
    line: str | None = Field(description="Line ref, e.g. 'VYG:Line:R13'.")
    public_code: str | None = Field(description="Public line code, e.g. 'R13'.")
    mode: str | None = Field(
        description="'rail' for trains; anything else (e.g. 'bus') is a replacement service and "
        "never counts as a rail departure.",
    )
    destination: str | None = Field(description="Front text shown on the departure board.")
    cancelled: bool = Field(description="Whether this departure (or its Oslo S call) is cancelled.")
    realtime: bool = Field(description="Whether the times carry realtime updates.")
    aimed_departure: datetime | None = Field(description="Aimed departure from Lier (UTC).")
    expected_departure: datetime | None = Field(description="Expected departure from Lier (UTC).")
    aimed_oslo_arrival: datetime | None = Field(description="Aimed arrival at Oslo S (UTC).")
    expected_oslo_arrival: datetime | None = Field(description="Expected arrival at Oslo S (UTC).")
    oslo_arrival_delay_min: int | None = Field(
        description="Expected-vs-aimed Oslo S arrival delay in whole minutes; the rail gate "
        "thresholds on this (arrival-side, not departure-side).",
    )
    predicted_cancel_probability: float | None = Field(
        description="Calibrated P(cancelled) from the per-call prediction snapshot, when a live "
        "situation covers this train; null otherwise. Can only demote the recommendation.",
    )


class CorridorInfo(BaseModel):
    """Self-description of the fixed corridor this recommendation covers."""

    from_stop_place: str = Field(description="Boarding StopPlace ref (Lier).")
    from_name: str = Field(description="Boarding stop name.")
    to_stop_place: str = Field(description="Alighting StopPlace ref (Oslo S).")
    to_name: str = Field(description="Alighting stop name.")
    road: str = Field(description="The parallel road stretch the road signal describes.")
    lines: list[str] = Field(description="Corridor rail lines the rail gate considers.")


class ParkingInfo(BaseModel):
    """Static park-and-ride note for the boarding stop."""

    name: str = Field(description="Facility name.")
    capacity: int | None = Field(
        description="STATIC capacity from the stop registry (NSR); may be null when not yet "
        "verified. This is NOT occupancy and must NOT be rendered as live availability — no "
        "open real-time occupancy feed exists (Bane NOR's is app-only).",
    )
    note: str = Field(description="Norwegian caveat text to show next to the capacity.")


class RecommendationResponse(BaseModel):
    """The corridor recommendation plus everything needed to judge how trustworthy it is."""

    state: RecommendationState = Field(
        description="TRAIN_RECOMMENDED | NO_ADVANTAGE | TRAIN_DEGRADED | OUTSIDE_WINDOW | UNKNOWN.",
    )
    reason: RecommendationReason = Field(
        description="Machine-readable reason behind the state (e.g. ROAD_DATA_STALE, RAIL_CLOSURE).",
    )
    copy_text: str = Field(
        serialization_alias="copy",
        description="Norwegian user-facing copy for the state, e.g. 'Bedre å ta toget i dag'. "
        "Serialized as `copy`.",
    )
    as_of: datetime = Field(description="Server time the recommendation was computed against (UTC).")
    state_since: datetime | None = Field(
        description="Since when the current state has held, derived from the observation history "
        "(for the volume source: the first observation of the current run), UTC.",
    )
    valid_until: datetime | None = Field(
        description="When this verdict expires at the latest (input staleness cutoff or window "
        "end) — re-ask after this, UTC.",
    )
    road_source: str | None = Field(
        description="Which road provider supplied the winning fresh observation ('datex' or "
        "'volume'); sources are prioritized, never blended. Null when no fresh road data.",
    )
    saved_min: int | None = Field(
        description="Estimated whole minutes saved by taking the train now: congested drive time "
        "minus (wait for the next departure + rail time to Oslo S). Only present when a "
        "travel-time road source (DATEX) is fresh AND the saving is at least MIN_SAVED_MIN; the "
        "volume proxy never yields this number.",
    )
    car_delay_min: int | None = Field(
        description="Extra drive minutes vs the free-flow car baseline, when a travel-time road "
        "source is fresh; null otherwise.",
    )
    car_baseline_min: int | None = Field(
        description="Free-flow car minutes Lier -> Oslo S (Journey Planner car routing; near-static).",
    )
    road: list[SourceFreshness] = Field(
        description="Per-provider road freshness, in priority order (DATEX first, then volumes).",
    )
    rail: SourceFreshness = Field(description="Journey Planner departure-board freshness.")
    car: SourceFreshness = Field(description="Car-baseline freshness (near-static, no cutoff).")
    next_departures: list[NextDeparture] = Field(
        description="Upcoming corridor departures (rail + any replacement services), soonest first.",
    )
    corridor: CorridorInfo = Field(description="Which corridor this recommendation describes.")
    parking: ParkingInfo = Field(
        description="Static park-and-ride capacity note — never live availability.",
    )


def _parking_info() -> ParkingInfo:
    # Static capacity only: NSR/NeTEx carries capacity for innfartsparkering, but no open
    # real-time occupancy feed exists (verified at planning time — Bane NOR's is app-only).
    # Capacity is left null until U5 verifies the NSR figure; the note is safe to render as-is.
    return ParkingInfo(
        name="Lier innfartsparkering",
        capacity=None,
        note="Innfartsparkering ved Lier stasjon (statisk kapasitet fra stoppestedsregisteret "
        "— sier ikke noe om ledige plasser nå).",
    )


def _corridor_info() -> CorridorInfo:
    return CorridorInfo(
        from_stop_place=CORRIDOR_FROM_STOP,
        from_name=CORRIDOR_FROM_NAME,
        to_stop_place=CORRIDOR_TO_STOP,
        to_name=CORRIDOR_TO_NAME,
        road=CORRIDOR_ROAD,
        lines=sorted(CORRIDOR_LINES),
    )


def _next_departure_model(d: Departure, predicted_cancel: dict[str, float]) -> NextDeparture:
    delay = d.oslo_arrival_delay_min()
    return NextDeparture(
        dated_journey_ref=d.dated_journey_ref,
        line=d.line,
        public_code=d.public_code,
        mode=d.mode,
        destination=d.destination,
        cancelled=d.cancelled,
        realtime=d.realtime,
        aimed_departure=_utc(d.aimed_dep),
        expected_departure=_utc(d.expected_dep),
        aimed_oslo_arrival=_utc(d.oslo_aimed_arr),
        expected_oslo_arrival=_utc(d.oslo_expected_arr),
        oslo_arrival_delay_min=round(delay) if delay is not None else None,
        predicted_cancel_probability=predicted_cancel.get(d.dated_journey_ref or ""),
    )


# ---- Router -------------------------------------------------------------------------------------
router = APIRouter(prefix="/reisevalg-api", tags=["reisevalg"])


def _build_recommendation(now: datetime) -> RecommendationResponse:
    # Gather cached inputs; every subsystem failure is isolated and only degrades itself.
    road_entries: list[tuple[str, RoadObservation | None]] = []
    for i, provider in enumerate(_road_providers):
        name = getattr(provider, "road_source_name", None)
        try:
            obs = provider()
        except Exception:
            logger.exception(
                "reisevalg road provider failed",
                extra={
                    "fields": {
                        "event": "reisevalg_road_provider_error",
                        "provider": name or repr(provider),
                    }
                },
            )
            obs = None
        if obs is not None and name is None:
            name = obs.source
        road_entries.append((name or f"provider{i + 1}", obs))
    observations = [obs for _, obs in road_entries if obs is not None]

    departures, rail_fetched_at = _board_cache.get(lambda: _board_fetcher(), now)
    car_min, car_fetched_at = _car_cache.get(lambda: _car_fetcher(), now)

    # state_since for the volume source comes from its trailing-run computation; injected or
    # DATEX observations fall back to their own observed_at inside decide().
    road_state_since = _volume_run_start

    decision = decide(
        now=now,
        road_observations=observations,
        departures=departures,
        rail_observed_at=rail_fetched_at,
        car_baseline_min=car_min,
        snapshot_lookup=_snapshot_lookup,
        road_state_since=road_state_since,
    )

    road_freshness = [
        SourceFreshness(
            name=name,
            available=obs is not None,
            observed_at=_utc(obs.observed_at) if obs is not None else None,
            confidence=obs.confidence if obs is not None else None,
            max_age_min=_road_max_age(obs.source if obs is not None else name),
            stale=(
                obs is not None
                and now - obs.observed_at > timedelta(minutes=_road_max_age(obs.source))
            ),
        )
        for name, obs in road_entries
    ]
    rail_freshness = SourceFreshness(
        name="journey-planner",
        available=departures is not None,
        observed_at=_utc(rail_fetched_at),
        confidence="high" if departures is not None else None,
        max_age_min=RAIL_MAX_AGE_MIN,
        stale=(
            rail_fetched_at is not None
            and now - rail_fetched_at > timedelta(minutes=RAIL_MAX_AGE_MIN)
        ),
    )
    car_freshness = SourceFreshness(
        name="journey-planner-car",
        available=car_min is not None,
        observed_at=_utc(car_fetched_at),
        confidence="high" if car_min is not None else None,
        max_age_min=None,  # free-flow baseline is near-static; no staleness cutoff
        stale=False,
    )

    predicted_cancel = decision.predicted_cancel or {}
    return RecommendationResponse(
        state=decision.state,
        reason=decision.reason,
        copy_text=copy_for(decision.state, decision.reason),
        as_of=_utc(now),
        state_since=_utc(decision.state_since),
        valid_until=_utc(decision.valid_until),
        road_source=decision.road_source,
        saved_min=decision.saved_min,
        car_delay_min=decision.car_delay_min,
        car_baseline_min=round(car_min) if car_min is not None else None,
        road=road_freshness,
        rail=rail_freshness,
        car=car_freshness,
        next_departures=[
            _next_departure_model(d, predicted_cancel) for d in decision.upcoming
        ],
        corridor=_corridor_info(),
        parking=_parking_info(),
    )


def _unknown_response(now: datetime) -> RecommendationResponse:
    """Last-resort response when the whole build fails: 200, UNKNOWN, honest reason."""
    return RecommendationResponse(
        state=_State.UNKNOWN,
        reason=_Reason.INTERNAL_ERROR,
        copy_text=copy_for(_State.UNKNOWN, _Reason.INTERNAL_ERROR),
        as_of=_utc(now),
        state_since=_utc(now),
        valid_until=_utc(now + _UNKNOWN_REVISIT),
        road_source=None,
        saved_min=None,
        car_delay_min=None,
        car_baseline_min=None,
        road=[],
        rail=SourceFreshness(
            name="journey-planner", available=False, observed_at=None, confidence=None,
            max_age_min=RAIL_MAX_AGE_MIN, stale=False,
        ),
        car=SourceFreshness(
            name="journey-planner-car", available=False, observed_at=None, confidence=None,
            max_age_min=None, stale=False,
        ),
        next_departures=[],
        corridor=_corridor_info(),
        parking=_parking_info(),
    )


@router.get(
    "/recommendation",
    summary="Car vs train recommendation for the Lier → Oslo S morning corridor",
    response_description="One of five states with per-source freshness and Norwegian copy.",
)
def recommendation() -> RecommendationResponse:
    """The live corridor recommendation, recomputed per request from cached inputs.

    Plain ``def`` (NOT ``async def``) on purpose: this handler performs blocking ``urllib``
    fetches on cache misses, which must run in FastAPI's threadpool — on the event loop they
    would freeze every other endpoint of this single-worker app, including the ``/health``
    liveness probe. Worst-case cold-cache latency is three sequential HTTP calls at a 3 s
    timeout each (~9 s); warm-cache requests do no I/O at all.

    Always returns 200: any internal failure degrades to ``UNKNOWN`` with a reason, never a 5xx.
    """
    now = _now()
    try:
        return _build_recommendation(now)
    except Exception:
        logger.exception(
            "reisevalg recommendation failed",
            extra={"fields": {"event": "reisevalg_internal_error"}},
        )
        return _unknown_response(now)
