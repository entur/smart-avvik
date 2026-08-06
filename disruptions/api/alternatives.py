"""Faster-alternative lookup for /impact — "is there a better way to get there than this train?"

When ``/impact`` is asked about a trip (``from_stop`` → ``to_stop``) and a situation is predicted to
hit the train the traveller depends on, the useful next question is not another probability: it is
whether some *other* departure gets them to ``to_stop`` sooner. This module answers that from the
Entur Journey Planner (JP) v3 GraphQL API.

Design notes
------------
* **Fetching and choosing are separate.** :func:`fetch_candidates` asks JP and parses the reply into
  :class:`AlternativeCandidate` records — everything JP offered, in JP's order, ranked by nobody. It
  takes both anchors: `arrive_by=False` reads `date_time` as "leave no earlier than",
  `arrive_by=True` as "be there by", and the two are separate requests because a depart-after search
  cannot contain a journey that leaves earlier and still gets there in time. Selection reads
  candidates; it never re-parses patterns.
* **The bar is the traveller's appointment, not the fastest possible trip.** :func:`select_best`
  keeps the **latest-departing** journey that still arrives before the disrupted train's *aimed*
  arrival: somebody who will make their appointment either way would rather leave later than run for
  an earlier train. Only when nothing arrives in time does it fall back to the earliest arrival —
  which is the case where a replacement legitimately arrives *later* than the train it replaces, so
  ``minutes_saved`` is signed and may be zero or negative. The rule this replaced (earliest arrival,
  at least three minutes faster) measurably almost never fired.
* **The disrupted train competes under the same rules, and usually wins.** It is passed in as
  ``original`` and ranked alongside the candidates; when it wins, there is no alternative to offer.
  Because its arrival is aimed **plus** a non-negative predicted delay while the deadline is that
  same aimed arrival, it can never sit in the in-time tier — which is why a *materiality* gate sits
  on top (see ``_MIN_MATERIAL_MINUTES``), or a two-minute delay would produce "abandon your train".
* **Candidates that ride the disruption are excluded, the original is not.** A candidate is dropped
  when any of its ``transit_refs`` is one of the affected ``(operator, jkey)`` pairs — JP's reply
  contains the disrupted train's own pattern, and it would win outright. The synthesized
  ``original`` rides an affected journey *by construction*, so it is exempt; dropping it would make
  the "stay put" answer unreachable. Riding an affected *line* is no longer a hard exclusion: it
  feeds ``AlternativeCandidate.risky``, which the selector treats as a preference (a safe journey
  arriving two hours late must not beat a slightly risky one that gets the traveller there on time).
* **Fail open, always.** Every failure path — timeout, GraphQL ``errors``, an empty
  ``tripPatterns``, a malformed payload — yields ``None``. This sits on the poller thread behind a
  push notification; it must never be the reason an alert does not go out.
* **Keyed TTL cache with single-flight and a failure cooldown**, mirroring
  ``reisevalg._TTLCache``'s semantics (fetch outside the lock, swap under it, a failed fetch serves
  the previous value rather than caching a miss as fresh) but keyed per ``(from, to, departure
  bucket)`` rather than holding one value. Air legs are excluded by restricting
  ``transportModes``; ``directMode: foot`` keeps JP from proposing a two-hour walk.
"""

import json
import logging
import re
import threading
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_JP_URL = "https://api.entur.io/journey-planner/v3/graphql"
_ET_CLIENT_NAME = "entur-kixavvik"
_HTTP_TIMEOUT_S = 3.0

# How long a (from, to, departure-bucket) answer is reused. Short: the alternatives a traveller has
# do shift as the timetable moves, but not second to second, and this call sits on the poller tick.
_TTL = timedelta(minutes=2)
# After a failure, suppress new fetches for this long (still serving any previous value) so a
# degraded JP is not hammered once per poller cycle.
_FAILURE_COOLDOWN = timedelta(minutes=2)
# Departure-time cache bucket: requests within the same bucket share one JP answer.
_DEPART_BUCKET = timedelta(minutes=5)
# The materiality gate on "stay put". Inherited from the deleted `_MIN_MINUTES_SAVED`, which used to
# be a filter over every candidate; it is now applied at exactly ONE place — to the winner, and only
# when the disrupted train is neither cancelled nor itself alert-worthy.
#
# Why the gate has to exist at all. The R3 deadline is the original's *aimed* arrival, while the
# original's own arrival is that aimed time PLUS a non-negative predicted delay. So the original can
# never sit in the in-time tier, and any earlier-arriving train beats it there — meaning "the
# traveller should stay on the train they have" would be unreachable for every delay-driven alert.
# Rail alerts are mostly delay-driven, so without this gate the systematic advice becomes "abandon
# your train and run for an earlier one" — including when the delay is two minutes. Do not
# "simplify" it away: it is not a duplicate of the tier logic, it is the reason the tier logic is
# safe to state so simply.
_MIN_MATERIAL_MINUTES = 3
# Mirrors `varsel._WINDOW_CATCH_UP` (90 min): how long past a registered window's end a train from
# that window can still be worth offering, sized above the p90 delays the exceedance stack produces.
# It is duplicated rather than imported because `varsel` imports THIS module (`from alternatives
# import AlternativeJourney`), so importing back is a cycle. `select_best` takes it as a parameter so
# a caller that can see both may pass the real constant and keep the two from drifting.
_CATCH_UP_GRACE = timedelta(minutes=90)
# Legacy: the filter `select_best` replaced, still read by `_legacy_to_alternative` on `find_faster`'s
# path. Deleted together with `find_faster` when its callers are re-pointed at `select_best`.
_LEGACY_MIN_MINUTES_SAVED = 3
# How far ahead of `now` a requested travel time may sit before the lookup is skipped entirely.
# Stated in hours rather than as a ratio of anything: a registration for tomorrow morning refreshes
# on every poller cycle from the moment it is created, and the Journey Planner query it would issue
# is the single most expensive call on that path. A tomorrow-window lookup cannot contain a train in
# tomorrow's window anyway — by the time the traveller could act on it, the answer has been
# recomputed many times over.
_MAX_LOOKAHEAD_HOURS = 6
# An NSR stop reference, e.g. NSR:StopPlace:337 or NSR:Quay:11274. Deliberately excludes the quote
# and brace characters that would let a value escape the GraphQL string literal it is spliced into.
_STOP_REF = re.compile(r"^[A-Za-z]{2,10}:(StopPlace|Quay):[A-Za-z0-9_-]{1,40}$")


def _is_stop_ref(value: str) -> bool:
    """True when `value` is shaped like an NSR stop reference."""
    return bool(_STOP_REF.match(value or ""))
# How many patterns to ask JP for. A post-filter, NOT a reach control -- see the measurement table
# below. JP's own default is 12; asking for 10 leaves room to drop patterns (wrong line, rides the
# disruption, cancelled) and still have something to offer.
_NUM_TRIP_PATTERNS = 10

# Verified live with curl 2026-08-06 (Oslo S -> Rosenholm, Oslo S -> Drammen, Oslo S -> Drøbak
# torg, both `arriveBy` values), following the same in-file convention as reisevalg.py's two JP
# queries -- a brace slip in one of those cost a fix commit (PR #22), so the document is checked
# against the real endpoint before the parser is written, and the date says when. The parser is
# additionally pinned in test_alternatives.py against a payload recorded from that same curl.
#
# Reach is governed by `searchWindow` and OTP2's dynamic default, NOT by the pattern count.
# Measured 2026-08-06, Oslo S (NSR:StopPlace:337) -> Rosenholm stasjon (NSR:StopPlace:60),
# dateTime 11:00 local:
#
#   | query                                     | patterns | departure span |
#   |-------------------------------------------|----------|----------------|
#   | numTripPatterns: 10, searchWindow unset   |    7     | 11:16-14:16    |
#   | numTripPatterns: 10, searchWindow: 120    |    4     | 11:16-12:46    |
#   | numTripPatterns: 10, searchWindow: 60     |    2     | 11:16-11:46    |
#   | numTripPatterns:  3, searchWindow: 120    |    3     | 11:16-12:16    |
#
# So clamping `searchWindow` SHRINKS the candidate pool below what the unclamped query already
# gets, and the count only binds once it drops below what the window returned anyway (row 4). This
# reversed an earlier design decision -- setting a searchWindow looked like the way to widen the
# search and does the opposite -- so it is recorded here rather than re-derived. The schema is
# explicit about the count too: numTripPatterns "reduce[s] the number of trip patterns AFTER the
# OTP travel search is done in a post-filtering process". `searchWindow` is deliberately UNSET.
# (An earlier reading the same day gave 5 / 11:16-13:16 for row 1: the dynamic window is
# density-aware and recomputed per request, so the counts are not constants. The ordering is.)
#
# `includeRealtimeCancellations` stays at its schema default (false). Determined by A/B curl
# 2026-08-06 against a real partial cancellation (TRO:ServiceJourney:250_260324131500087_1032 had
# its Rødskjer call cancelled; routing Rødskjer -> Harstad Byterminal over it):
#   * default        -> 0 tripPatterns. The cancelled departure is excluded from the search.
#   * flag set true  -> 1 tripPattern, fromEstimatedCall.cancellation == true.
# So JP's own `cancellation: true` does NOT surface by default -- the default already excludes
# cancelled departures. Our own ET cache is therefore the working guard against recommending a
# train that is not running; reading `cancellation` off the estimated calls below is
# belt-and-braces, and earns its keep only if this flag is ever flipped on or OTP's realtime
# picture lags our ET one at the margin. Do not read `candidate.cancelled` as the primary signal.
#
# Modes a traveller on a disrupted public-transport trip could actually switch to. Air is
# deliberately absent (JP will happily route Oslo -> Stavanger by plane otherwise), and every
# access/egress/direct mode is foot so the fallback is never "drive" or "cycle for two hours".
#
# `estimatedCalls` and `intermediateEstimatedCalls` are FORBIDDEN in this document. They are a
# one-word edit away and would be a two-order-of-magnitude change in payload size: a 30-call Bergen
# train times 10 patterns is ~300 call objects per reply, on the poller thread, behind a 3 s
# timeout. What this module needs from a leg is its two endpoints, and `fromEstimatedCall` /
# `toEstimatedCall` give exactly those.
#
# `realtime` (on the leg and on `fromEstimatedCall`) is selected but not mapped onto a model field
# here: it says whether `expectedDepartureTime` is a real prediction or just a copy of the aimed
# time, which is what tells the scoring layer whether an "on time" reading means anything.
_TRIP_QUERY = """
{
  trip(
    from: {place: "%(from_stop)s"}
    to: {place: "%(to_stop)s"}
    dateTime: "%(date_time)s"
    arriveBy: %(arrive_by)s
    numTripPatterns: %(num)d
    modes: {
      accessMode: foot
      egressMode: foot
      directMode: foot
      transportModes: [
        {transportMode: rail}
        {transportMode: bus}
        {transportMode: coach}
        {transportMode: tram}
        {transportMode: metro}
        {transportMode: water}
      ]
    }
  ) {
    tripPatterns {
      expectedStartTime
      expectedEndTime
      aimedStartTime
      aimedEndTime
      duration
      legs {
        mode
        realtime
        expectedStartTime
        expectedEndTime
        aimedStartTime
        aimedEndTime
        line { id publicCode }
        serviceJourney { id }
        datedServiceJourney { id }
        fromPlace { name quay { stopPlace { id } } }
        toPlace { name quay { stopPlace { id } } }
        fromEstimatedCall { cancellation realtime aimedDepartureTime expectedDepartureTime }
        toEstimatedCall { cancellation aimedArrivalTime expectedArrivalTime }
      }
    }
  }
}
"""


class AlternativeLeg(BaseModel):
    """One leg of a suggested alternative, trimmed to what a traveller needs to recognise it."""

    mode: str | None = Field(description='Transport mode: "rail", "bus", "foot", ...')
    line: str | None = Field(default=None, description="Line ref, e.g. 'VYG:Line:R11'.")
    public_code: str | None = Field(
        default=None, description="The line number as shown to passengers, e.g. 'R11'."
    )
    from_name: str | None = Field(default=None, description="Where this leg starts.")
    to_name: str | None = Field(default=None, description="Where this leg ends.")
    start_time: datetime | None = Field(default=None, description="Expected start of the leg.")
    end_time: datetime | None = Field(default=None, description="Expected end of the leg.")


class AlternativeJourney(BaseModel):
    """The journey :func:`select_best` chose over the disrupted train.

    Sourced from the Journey Planner, so its times reflect the *published* real-time picture.

    It is **not** necessarily faster than the train it replaces: when nothing arrives before the
    disrupted train's aimed arrival, the fallback tier offers the earliest arrival there is, and a
    replacement for a cancelled train legitimately gets the traveller there later than the timetable
    promised. `minutes_saved` is signed for exactly that reason, and `arrives_before_original` says
    which side of zero it fell on without a reader having to know the sign convention.
    """

    departure_time: datetime | None = Field(description="Expected departure from `from_stop`.")
    arrival_time: datetime | None = Field(description="Expected arrival at `to_stop`.")
    duration_min: int | None = Field(default=None, description="Total journey time in minutes.")
    minutes_saved: int = Field(
        description="SIGNED minutes earlier this arrives than the disrupted train's predicted "
        "arrival. Zero or negative is a legitimate answer in the fallback tier — a replacement for "
        "a cancelled train is still worth offering when it arrives later than the train would have.",
    )
    arrives_before_original: bool = Field(
        default=False,
        description="This arrives strictly before the disrupted train's predicted arrival. False "
        "means the traveller gets there later than the disrupted train was expected to — which "
        "only happens when that train is cancelled or in enough trouble to be worth escaping.",
    )
    at_risk: bool = Field(
        default=False,
        description="This journey is itself predicted to be in trouble (over its operator's alert "
        "gate, or running on a line a live situation touches). It is offered anyway because "
        "nothing safer reached the destination in time — a wording that presents it as a clean "
        "escape would be wrong.",
    )
    legs: list[AlternativeLeg] = Field(
        default_factory=list, description="The legs to travel, in order."
    )


class AlternativePrediction(BaseModel):
    """What we know and what we forecast about the alternative's own boarding call.

    Two different questions live side by side here and must not be blended:

    * ``delay_min`` (with ``scheduled_time`` / ``expected_time`` / ``cancelled``) is the Journey
      Planner's **observed** realtime picture — what the feed already says about this departure.
    * ``cancel_probability`` / ``impact_probability`` are **our** forecast, from the same per-call
      models that scored the disrupted train. They are the answer to "is the trip we are about to
      recommend heading for trouble itself?", not to "is it late right now".

    A null probability means **the models never scored this call** — no live situation touches this
    journey, so it is absent from the prediction cache. It never means "zero risk". A wording that
    renders a null as reassurance ("ingen risiko") is wrong; the honest reading is "nothing flagged
    it", which is the ordinary case for the great majority of departures.

    Lives in this module rather than in ``impact``, even though ``impact.score_candidates`` is its
    only constructor: ``impact`` imports ``alternatives`` (``from alternatives import
    AlternativeJourney``), and ``AlternativeJourney`` carries a ``departure_prediction`` of this
    type. Defining it in ``impact`` would need the reverse import. ``from __future__ import
    annotations`` does not rescue that — pydantic resolves field annotations when the class is
    built, so the cycle is an ImportError at startup, not a deferred one.
    """

    stop_place: str | None = Field(
        default=None, description="NSR StopPlace ref of the alternative's boarding stop."
    )
    stop_name: str | None = Field(default=None, description="Human-readable boarding stop name.")
    scheduled_time: datetime | None = Field(
        default=None, description="Aimed (timetabled) departure from the boarding stop."
    )
    expected_time: datetime | None = Field(
        default=None, description="Expected departure per the Journey Planner's realtime picture."
    )
    delay_min: int | None = Field(
        default=None,
        description="OBSERVED delay in minutes (expected minus aimed), clamped at 0 — a vehicle "
        "running early is not 'delayed -2 min'. This is JP's figure, never a forecast.",
    )
    cancelled: bool = Field(
        default=False,
        description="The Journey Planner's estimated call for this boarding is flagged cancelled.",
    )
    cancel_probability: float | None = Field(
        default=None,
        description="OUR calibrated P(this boarding is cancelled), 0-1. Null = not scored (no live "
        "situation touches this journey), NOT zero risk.",
    )
    impact_probability: float | None = Field(
        default=None,
        description="OUR calibrated P(cancelled OR materially late) for this boarding, 0-1. "
        "Null = not scored, NOT zero risk.",
    )
    scored: bool = Field(
        default=False,
        description="True when the prediction cache had this call, so the probabilities above are "
        "model output. False means they are null because nothing scored it — the flag exists so a "
        "reader never has to infer 'not scored' from 'no number'.",
    )


class AlternativeCandidate(BaseModel):
    """One parsed `tripPattern` from a Journey Planner reply, before any selection has happened.

    Deliberately a *record of what JP said*, not a recommendation: every field is either read
    straight off the pattern or derived from it, and the two caller-set flags at the bottom are the
    only places anything else writes. Selection reads these; it does not re-parse patterns.

    Aimed and expected are both kept because they answer different questions — aimed is the trip a
    traveller would recognise from the timetable, expected is when it will actually happen — and
    collapsing them loses the delay.
    """

    departure_time: datetime | None = Field(
        default=None, description="EXPECTED departure from the origin (realtime)."
    )
    arrival_time: datetime | None = Field(
        default=None, description="EXPECTED arrival at the destination (realtime)."
    )
    aimed_departure_time: datetime | None = Field(
        default=None, description="AIMED (timetabled) departure from the origin."
    )
    aimed_arrival_time: datetime | None = Field(
        default=None, description="AIMED (timetabled) arrival at the destination."
    )
    duration_min: int | None = Field(default=None, description="Total journey time in minutes.")
    legs: list[AlternativeLeg] = Field(
        default_factory=list, description="The legs to travel, in order (foot legs included)."
    )
    cancelled: bool = Field(
        default=False,
        description="Any transit leg's boarding or alighting estimated call is flagged cancelled "
        "by JP. Belt-and-braces only — the trip search excludes cancelled departures by default "
        "(see the note on `includeRealtimeCancellations`), so our ET cache is the real guard.",
    )
    transit_refs: list[tuple[str | None, str]] = Field(
        default_factory=list,
        description="Ordered (operator, jkey) pairs for the transit legs — foot legs excluded. "
        "BOTH halves of each ref are kept: the prediction cache is keyed on (operator, jkey), and "
        "bus ServiceJourney refs are not date-scoped, so a jkey alone can collide across "
        "operators. A leg contributes BOTH its dated and its plain service-journey ref where JP "
        "gives both, because which form the cache holds depends on the family — rail jkeys are "
        "DatedServiceJourney refs, bus jkeys are plain ServiceJourney refs — so a lookup has to be "
        "able to try either. Deduplicated, first occurrence wins.",
    )
    lines: list[str] = Field(
        default_factory=list, description="Ordered line refs of the transit legs, e.g. 'VYG:Line:R11'."
    )
    board_stop_place: str | None = Field(
        default=None,
        description="NSR StopPlace where the FIRST transit leg is boarded — not where the walk "
        "from the origin starts. Null when no leg exposes a quay.",
    )
    alight_stop_place: str | None = Field(
        default=None, description="NSR StopPlace where the LAST transit leg is left."
    )
    ordinal: int = Field(
        default=0,
        description="Position of this pattern in the Journey Planner's reply (0-based). JP returns "
        "patterns in its own preference order; keeping it makes a tie-break deterministic instead "
        "of dependent on dict iteration.",
    )
    is_original: bool = Field(
        default=False,
        description="Caller-set: this candidate IS the disrupted journey the traveller already has.",
    )
    risky: bool = Field(
        default=False,
        description="Caller-set: this candidate is itself predicted to be in trouble.",
    )


# ---- Keyed TTL cache -------------------------------------------------------------------------
class _KeyedTTLCache:
    """Per-key TTL cache with single-flight refresh and a post-failure cooldown.

    Same contract as ``reisevalg._TTLCache`` — the fetch runs outside the lock, the swap happens
    under it, and a failing fetch serves the previous value (with its old timestamp) instead of
    caching the miss as a fresh success — but keyed, because there is one answer per stop pair
    rather than one global value.
    """

    def __init__(self, ttl: timedelta, failure_cooldown: timedelta):
        self._ttl = ttl
        self._failure_cooldown = failure_cooldown
        self._lock = threading.Lock()
        self._entries: dict[Any, tuple[Any, datetime]] = {}
        self._refreshing: set[Any] = set()
        self._failed_at: dict[Any, datetime] = {}

    def get(self, key: Any, fetch: Callable[[], Any], now: datetime) -> Any:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and now - entry[1] < self._ttl:
                return entry[0]
            if key in self._refreshing:  # another caller is already fetching this key
                return entry[0] if entry else None
            failed_at = self._failed_at.get(key)
            if failed_at is not None and now - failed_at < self._failure_cooldown:
                return entry[0] if entry else None
            self._refreshing.add(key)
        try:
            value = fetch()
        except Exception:
            logger.warning(
                "Journey Planner alternative lookup failed for %s; serving previous value",
                key,
                exc_info=True,
                extra={"fields": {"event": "alternatives_error"}},
            )
            with self._lock:
                self._failed_at[key] = now
                self._refreshing.discard(key)
                self._expire_failures(now)
                entry = self._entries.get(key)
            return entry[0] if entry else None
        with self._lock:
            self._entries[key] = (value, now)
            self._failed_at.pop(key, None)
            self._refreshing.discard(key)
            self._expire_failures(now)
            # Keep the cache bounded: stop pairs are user-chosen, so this is unbounded in principle.
            if len(self._entries) > 500:
                oldest = sorted(self._entries.items(), key=lambda kv: kv[1][1])[:100]
                for k, _ in oldest:
                    self._entries.pop(k, None)
        return value

    def _expire_failures(self, now: datetime) -> None:
        """Drop failure marks that can no longer suppress a fetch. Caller holds the lock.

        `_entries` is capped but `_failed_at` was not, and it only ever shed a key on a later
        *success* for that same key. The key embeds a 5-minute wall-clock bucket, so it grows with
        time rather than with traffic: a stop pair mints up to 288 keys a day whether anyone asks
        again or not, and on a long-lived single pod that never comes back. Runs on the failure path
        too — during a total Journey Planner outage the success path never executes.
        """
        stale = [k for k, t in self._failed_at.items() if now - t >= self._failure_cooldown]
        for k in stale:
            self._failed_at.pop(k, None)


_cache = _KeyedTTLCache(_TTL, _FAILURE_COOLDOWN)


# ---- HTTP ------------------------------------------------------------------------------------
def _post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    """POST JSON and parse the reply, treating a GraphQL error envelope as a failure.

    JP answers HTTP 200 with an ``errors`` array on a bad query, so raising here routes those
    responses through the cache's failure path instead of parsing them into "no alternative" and
    caching that as a fresh success. Same guard as ``reisevalg._post_json``.
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
        raise RuntimeError(f"JP error response: {str(body.get('errors', 'no data key'))[:200]}")
    return body


def _fetch_trip_patterns(
    from_stop: str, to_stop: str, date_time: datetime, *, arrive_by: bool
) -> list[dict]:
    """Ask JP for trip patterns anchored either at departure or at arrival.

    `arrive_by=False` reads `date_time` as "leave no earlier than"; `arrive_by=True` reads it as
    "be there by". The two anchors answer different questions and are separate requests — a single
    depart-after search cannot contain a journey that leaves earlier and still gets there in time.
    """
    query = _TRIP_QUERY % {
        "from_stop": from_stop,
        "to_stop": to_stop,
        "date_time": date_time.isoformat(),
        "arrive_by": "true" if arrive_by else "false",
        "num": _NUM_TRIP_PATTERNS,
    }
    data = _post_json(_JP_URL, {"query": query}, headers={"ET-Client-Name": _ET_CLIENT_NAME})
    trip = (data.get("data") or {}).get("trip") or {}
    return trip.get("tripPatterns") or []


def _fetch_patterns(from_stop: str, to_stop: str, depart_after: datetime) -> list[dict]:
    return _fetch_trip_patterns(from_stop, to_stop, depart_after, arrive_by=False)


# ---- Parsing / selection ---------------------------------------------------------------------
def _parse_ts(value: str | None) -> datetime | None:
    """Parse a JP timestamp, rejecting anything we cannot safely compare.

    A *naive* timestamp is treated as malformed rather than coerced: JP always returns an offset, and
    silently assuming one would make the comparison against `reference_arrival` (which is UTC-aware)
    either raise or be wrong by up to two hours. Verified 2026-07-29 that a naive value used to raise
    TypeError all the way out of find_faster."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _strip_codespace(ref: str | None) -> str | None:
    """'VYG:DatedServiceJourney:81613_X' -> 'DatedServiceJourney:81613_X' (our jkey form)."""
    if not ref:
        return None
    _, sep, rest = ref.partition(":")
    return rest if sep else ref


def split_codespace(ref: str | None) -> tuple[str | None, str] | None:
    """'VYG:DatedServiceJourney:81613_X' -> ('VYG', 'DatedServiceJourney:81613_X').

    The codespace-preserving sibling of :func:`_strip_codespace`, which throws the prefix away.
    Both halves are needed because the prediction cache is keyed on ``(operator, jkey)``: bus
    ``ServiceJourney`` refs carry no date, so one jkey can name two operators' journeys and keying
    on the jkey alone silently merges them. (``main`` does this same split inline for ``/journey``'s
    path parameter; this is the reusable form, placed where ``transit_refs`` is built.)

    A NeTEx ref is ``<Codespace>:<Type>:<Value>``, so the codespace is only real when what follows
    the first colon still contains one — ``'DatedServiceJourney:x'`` has no codespace and must not
    yield an operator of ``'DatedServiceJourney'``. Returns ``None`` for an empty or non-string ref
    and ``(None, ref)`` when no codespace can be read, so a caller never has to distinguish
    "absent" from "unprefixed" by string inspection.
    """
    if not ref or not isinstance(ref, str):
        return None
    head, sep, rest = ref.partition(":")
    if sep and ":" in rest:
        return (head, rest)
    return (None, ref)


# ---- Legacy selection (dies with `find_faster`) -----------------------------------------------
# Everything down to `find_faster` is the rule `select_best` replaces: the earliest-arriving pattern
# that beat the disrupted train's predicted arrival by at least three minutes. It is kept, private
# and renamed, only because `impact.for_trip` still calls `find_faster`; deleting it in the same
# change would silently stop every alternative being offered until those callers are re-pointed.
# When `find_faster` goes, these three go with it.
def _legacy_rides_the_disruption(
    pattern: dict, excluded_jkeys: set[str], excluded_lines: set[str]
) -> bool:
    legs = pattern.get("legs")
    for leg in legs if isinstance(legs, list) else ():
        if not isinstance(leg, dict):
            continue
        line_id = (leg.get("line") or {}).get("id")
        if line_id and line_id in excluded_lines:
            return True
        for key in ("datedServiceJourney", "serviceJourney"):
            ref = _strip_codespace((leg.get(key) or {}).get("id"))
            if ref and ref in excluded_jkeys:
                return True
    return False


def _legacy_to_alternative(pattern: dict, reference_arrival: datetime) -> AlternativeJourney | None:
    arrival = _parse_ts(pattern.get("expectedEndTime"))
    if arrival is None:
        return None
    saved = round((reference_arrival - arrival).total_seconds() / 60)
    if saved < _LEGACY_MIN_MINUTES_SAVED:
        return None
    duration = pattern.get("duration")
    if not isinstance(duration, int | float):
        duration = None  # JP sends seconds; anything else is malformed, not a reason to crash
    legs = pattern.get("legs")
    if not isinstance(legs, list):
        legs = []
    return AlternativeJourney(
        departure_time=_parse_ts(pattern.get("expectedStartTime")),
        arrival_time=arrival,
        duration_min=round(duration / 60) if duration else None,
        minutes_saved=saved,
        arrives_before_original=True,  # the legacy filter admitted nothing else
        legs=[
            AlternativeLeg(
                mode=leg.get("mode"),
                line=(leg.get("line") or {}).get("id"),
                public_code=(leg.get("line") or {}).get("publicCode"),
                from_name=(leg.get("fromPlace") or {}).get("name"),
                to_name=(leg.get("toPlace") or {}).get("name"),
                start_time=_parse_ts(leg.get("expectedStartTime")),
                end_time=_parse_ts(leg.get("expectedEndTime")),
            )
            for leg in legs
            if isinstance(leg, dict)
        ],
    )


def _legacy_select_faster(
    patterns: Iterable[dict],
    *,
    reference_arrival: datetime,
    excluded_jkeys: set[str],
    excluded_lines: set[str],
) -> AlternativeJourney | None:
    """The earliest-arriving pattern that beats `reference_arrival` and avoids the disruption.

    Superseded by :func:`select_best`; still reached from :func:`find_faster`, and deleted with it.
    """
    best: AlternativeJourney | None = None
    for pattern in patterns:
        if not isinstance(pattern, dict):
            continue
        if _legacy_rides_the_disruption(pattern, excluded_jkeys, excluded_lines):
            continue
        candidate = _legacy_to_alternative(pattern, reference_arrival)
        if candidate is None:
            continue
        if best is None or (
            candidate.arrival_time is not None
            and best.arrival_time is not None
            and candidate.arrival_time < best.arrival_time
        ):
            best = candidate
    return best


def find_faster(
    from_stop: str,
    to_stop: str,
    *,
    reference_arrival: datetime,
    depart_after: datetime,
    excluded_jkeys: set[str],
    excluded_lines: set[str],
    now: datetime | None = None,
) -> AlternativeJourney | None:
    """A faster way from `from_stop` to `to_stop`, or None.

    `reference_arrival` is the bar to beat — the disrupted train's predicted arrival at `to_stop`.
    `depart_after` is when the traveller can realistically leave. Returns None on any failure, on
    an empty result, and when nothing saves at least `_LEGACY_MIN_MINUTES_SAVED` minutes.

    Superseded by `fetch_candidates` + `select_best`; still the live path until its callers are
    re-pointed, at which point this and the three `_legacy_*` helpers are deleted together.
    """
    if not (_is_stop_ref(from_stop) and _is_stop_ref(to_stop)):
        # Both refs are spliced into the GraphQL document as string literals, and both originate in
        # unauthenticated input (/impact's query params, JourneyIn's fields) validated only as short
        # strings. Nothing reaches here today with a quote in it -- find_faster is only called once
        # the refs have matched real stop_places in the prediction cache -- but that containment is
        # an accident of an unrelated code path, and the sibling reisevalg module was already bitten
        # once by hand-built GraphQL (PR #22). Refuse the shape here, where the assumption lives.
        logger.warning(
            "Refusing a Journey Planner lookup for a non-NSR stop ref",
            extra={"fields": {"event": "alternatives_error", "reason": "bad_stop_ref"}},
        )
        return None
    now = now or datetime.now(depart_after.tzinfo)
    bucket = int(depart_after.timestamp() // _DEPART_BUCKET.total_seconds())
    key = (from_stop, to_stop, bucket)
    patterns = _cache.get(key, lambda: _fetch_patterns(from_stop, to_stop, depart_after), now)
    if not patterns:
        return None
    # "Fail open, ALWAYS" has to cover parsing, not just fetching. Until 2026-07-29 only the fetch
    # was guarded, so one malformed pattern raised out of here, through main._impact_for_trip, into
    # varsel.check_and_notify -- where the per-journey guard turned it into every alert for that
    # journey being silently dropped for the cycle. A bad alternative must cost the alternative only.
    try:
        return _legacy_select_faster(
            patterns,
            reference_arrival=reference_arrival,
            excluded_jkeys=excluded_jkeys,
            excluded_lines=excluded_lines,
        )
    except Exception:
        logger.warning(
            "Could not interpret the Journey Planner reply for %s; offering no alternative",
            key,
            exc_info=True,
            extra={"fields": {"event": "alternatives_error"}},
        )
        return None


# ---- Candidates ------------------------------------------------------------------------------
def _as_dict(value: Any) -> dict:
    """`value` if it is a dict, else an empty one.

    `(node.get("x") or {}).get("y")` is the idiom this module already used, and it is safe only
    while every non-null value really is an object. It is not: a malformed reply can put a string
    where JP puts a node, and `"oops".get` is an AttributeError that costs the whole pattern. The
    parser's contract is that a broken *field* degrades to null; only a broken *arrival time* drops
    the candidate.
    """
    return value if isinstance(value, dict) else {}


def _stop_place_ref(place: Any) -> str | None:
    """`fromPlace`/`toPlace` -> its NSR StopPlace ref, or None.

    JP nests it two levels down (`quay { stopPlace { id } }`) and any level can be null — a leg
    that starts at a street address or a coordinate has no quay at all, and every foot leg in a
    real reply is a candidate for that. A missing level is None here, never an exception.
    """
    if not isinstance(place, dict):
        return None
    ref = _as_dict(_as_dict(place.get("quay")).get("stopPlace")).get("id")
    return ref if isinstance(ref, str) and ref else None


def _leg_is_cancelled(leg: dict) -> bool:
    """True when JP flags either end of this leg's estimated calls as cancelled."""
    return any(
        _as_dict(leg.get(key)).get("cancellation") is True
        for key in ("fromEstimatedCall", "toEstimatedCall")
    )


def _to_candidate(pattern: dict, ordinal: int) -> AlternativeCandidate | None:
    """Parse one `tripPattern` into an `AlternativeCandidate`, or None if it is unusable.

    "Unusable" is narrow on purpose: only a pattern with no comparable expected arrival is dropped,
    because nothing downstream can rank it. Everything else degrades to a null field. A pattern is
    a suggestion, and a suggestion with a missing line ref is still a suggestion.
    """
    if not isinstance(pattern, dict):
        return None
    arrival = _parse_ts(pattern.get("expectedEndTime"))
    if arrival is None:
        return None
    duration = pattern.get("duration")
    if not isinstance(duration, int | float) or isinstance(duration, bool):
        duration = None  # JP sends seconds; anything else is malformed, not a reason to crash
    raw_legs = pattern.get("legs")
    if not isinstance(raw_legs, list):
        raw_legs = []
    legs = [leg for leg in raw_legs if isinstance(leg, dict)]

    transit_refs: list[tuple[str | None, str]] = []
    lines: list[str] = []
    board_stop_place: str | None = None
    alight_stop_place: str | None = None
    cancelled = False
    seen_refs: set[tuple[str | None, str]] = set()
    for leg in legs:
        # A foot leg has no service journey and no estimated calls; it is part of the itinerary a
        # traveller walks, never something the prediction cache can score or a situation can hit.
        # Both ref forms are collected, not the first that is present: JP gives a bus leg BOTH a
        # DatedServiceJourney and a ServiceJourney id, and our bus cache keys on the plain one
        # while rail keys on the dated one. Preferring either form would silently miss one family.
        refs = [
            _as_dict(leg.get(key)).get("id")
            for key in ("datedServiceJourney", "serviceJourney")
        ]
        refs = [ref for ref in refs if isinstance(ref, str) and ref]
        if not refs:
            continue
        for ref in refs:
            split = split_codespace(ref)
            if split is not None and split not in seen_refs:
                seen_refs.add(split)
                transit_refs.append(split)
        line_id = _as_dict(leg.get("line")).get("id")
        if isinstance(line_id, str) and line_id:
            lines.append(line_id)
        if board_stop_place is None:
            board_stop_place = _stop_place_ref(leg.get("fromPlace"))
        alight = _stop_place_ref(leg.get("toPlace"))
        if alight is not None:
            alight_stop_place = alight
        cancelled = cancelled or _leg_is_cancelled(leg)

    return AlternativeCandidate(
        departure_time=_parse_ts(pattern.get("expectedStartTime")),
        arrival_time=arrival,
        aimed_departure_time=_parse_ts(pattern.get("aimedStartTime")),
        aimed_arrival_time=_parse_ts(pattern.get("aimedEndTime")),
        duration_min=round(duration / 60) if duration else None,
        legs=[
            AlternativeLeg(
                mode=leg.get("mode") if isinstance(leg.get("mode"), str) else None,
                line=_as_dict(leg.get("line")).get("id"),
                public_code=_as_dict(leg.get("line")).get("publicCode"),
                from_name=_as_dict(leg.get("fromPlace")).get("name"),
                to_name=_as_dict(leg.get("toPlace")).get("name"),
                start_time=_parse_ts(leg.get("expectedStartTime")),
                end_time=_parse_ts(leg.get("expectedEndTime")),
            )
            for leg in legs
        ],
        cancelled=cancelled,
        transit_refs=transit_refs,
        lines=lines,
        board_stop_place=board_stop_place,
        alight_stop_place=alight_stop_place,
        ordinal=ordinal,
    )


def parse_candidates(patterns: Iterable[Any]) -> list[AlternativeCandidate]:
    """Parse a JP `tripPatterns` list into candidates, in reply order.

    Pure — no I/O — so the parser can be pinned against a recorded payload. A pattern that cannot be
    parsed is skipped individually rather than failing the batch: one malformed entry must cost that
    one suggestion, never the whole reply.
    """
    out: list[AlternativeCandidate] = []
    for ordinal, pattern in enumerate(patterns):
        try:
            candidate = _to_candidate(pattern, ordinal)
        except Exception:
            logger.warning(
                "Skipping an uninterpretable Journey Planner trip pattern (#%d)",
                ordinal,
                exc_info=True,
                extra={"fields": {"event": "alternatives_error", "reason": "bad_pattern"}},
            )
            continue
        if candidate is not None:
            out.append(candidate)
    return out


def fetch_candidates(
    from_stop: str,
    to_stop: str,
    *,
    arrive_by: bool,
    date_time: datetime,
    now: datetime,
) -> list[AlternativeCandidate]:
    """Journey Planner candidates from `from_stop` to `to_stop`, in JP's reply order.

    `date_time` is read as "leave no earlier than" when `arrive_by` is False and as "be there by"
    when it is True. `now` is passed in and never defaulted internally, so the caller's single
    sampled instant governs the horizon cut and the cache alike — `impact.for_trip` already samples
    `now` once precisely so `as_of` and the observation cut cannot straddle two instants, and this
    lookup must sit on that same instant.

    Selection is somebody else's job. This returns everything JP offered, parsed; it does not rank,
    filter by usefulness, or decide anything.

    Fail open, ALWAYS: a refused stop ref, a lookup too far ahead, an outage, a GraphQL `errors`
    envelope, an empty reply, or an uninterpretable one all yield `[]`. This sits on the poller
    thread behind a push notification and must never be the reason an alert does not go out.
    """
    if not (_is_stop_ref(from_stop) and _is_stop_ref(to_stop)):
        # Both refs are spliced into the GraphQL document as string literals, and both originate in
        # unauthenticated input (/impact's query params, JourneyIn's fields) validated only as short
        # strings. Nothing reaches here today with a quote in it -- the refs have matched real
        # stop_places in the prediction cache first -- but that containment is an accident of an
        # unrelated code path, and the sibling reisevalg module was already bitten once by
        # hand-built GraphQL (PR #22). Refuse the shape here, where the assumption lives, and
        # BEFORE any fetch.
        logger.warning(
            "Refusing a Journey Planner lookup for a non-NSR stop ref",
            extra={"fields": {"event": "alternatives_error", "reason": "bad_stop_ref"}},
        )
        return []
    if date_time - now > timedelta(hours=_MAX_LOOKAHEAD_HOURS):
        # A registration for tomorrow morning is re-examined on every poller cycle from the moment
        # it is created, and this is the most expensive call on that path. A lookup that far ahead
        # cannot contain a train in tomorrow's window, so it buys nothing at any price.
        return []
    bucket = int(date_time.timestamp() // _DEPART_BUCKET.total_seconds())
    key = (from_stop, to_stop, arrive_by, bucket)
    patterns = _cache.get(
        key,
        lambda: _fetch_trip_patterns(from_stop, to_stop, date_time, arrive_by=arrive_by),
        now,
    )
    if not patterns:
        return []
    # "Fail open, ALWAYS" has to cover parsing, not just fetching. Until 2026-07-29 only the fetch
    # was guarded, so one malformed pattern raised out of here, through main._impact_for_trip, into
    # varsel.check_and_notify -- where the per-journey guard turned it into every alert for that
    # journey being silently dropped for the cycle. A bad alternative must cost the alternative only.
    try:
        return parse_candidates(patterns)
    except Exception:
        logger.warning(
            "Could not interpret the Journey Planner reply for %s; offering no candidates",
            key,
            exc_info=True,
            extra={"fields": {"event": "alternatives_error"}},
        )
        return []


# ---- Selection -------------------------------------------------------------------------------
def select_best(
    candidates: Iterable[AlternativeCandidate],
    *,
    original: AlternativeCandidate | None,
    original_arrival_time: datetime,
    original_predicted_arrival: datetime,
    now: datetime,
    window_end: datetime,
    affected_refs: set[tuple[str | None, str]],
    catch_up_grace: timedelta = _CATCH_UP_GRACE,
) -> AlternativeJourney | None:
    """What the traveller should take instead of the disrupted train, or None to stay put.

    Pure — no I/O — so the rule is exercised against recorded payloads rather than the network.

    The two anchors are different times and must not be collapsed. `original_arrival_time` is the
    disrupted train's **aimed** arrival: the traveller's deadline, and the bar an alternative has to
    clear to count as "still in time". `original_predicted_arrival` is what we actually expect of
    that train (aimed + predicted delay): the bar `minutes_saved` and the materiality gate measure
    against. Passing the predicted time as the deadline would let a delayed train move the
    traveller's appointment.

    The rule, in order:

    1. **Pool.** Drop anything cancelled; anything whose departure has passed (`> now`, strictly, so
       a departure exactly at `now` is not offered) or falls beyond `window_end + catch_up_grace`;
       and any NON-original candidate riding one of `affected_refs`. That last exclusion carries the
       reply's copy of the disrupted train itself — the caller synthesizes `original` from our own
       scoring rather than reading it out of JP, so JP's own pattern for the same train is still in
       the list as `is_original=False` and would win outright. The original is exempt precisely
       because it rides an affected journey by construction; dropping it would make "stay put"
       unreachable.
    2. **Tiers, arrival-feasibility outside risk-avoidance.** Within each tier the safe candidates
       are consulted before the full pool. Nested the other way, a *safe* journey arriving two hours
       late would beat a slightly risky one that still gets the traveller there on time.
    3. **Ties** break on earlier arrival, then on `ordinal` (JP's own reply order), then in favour of
       the original. Not cosmetic: this runs again on every refresh, and an unstable winner would
       flip both the card's wording and which situation the written message is cached under.
    4. **The original competes.** If it wins, there is nothing to offer — plus the materiality gate
       described on `_MIN_MATERIAL_MINUTES`, without which "stay put" is unreachable for every
       delay-driven alert.

    `window_end` and `affected_refs` are required, not optional: without the first there is no upper
    bound on how far past the traveller's own window a suggestion may reach, and without the second
    the disrupted train wins its own replacement contest.
    """
    affected = set(affected_refs or ())

    def is_original(cand: AlternativeCandidate) -> bool:
        # Identity as well as the flag: the caller sets `is_original`, and a caller that forgets
        # would silently lose R7 — "stay put" would stop being an answer this function can give.
        return cand.is_original or cand is original

    pool_source = list(candidates)
    if original is not None and not any(cand is original for cand in pool_source):
        pool_source.append(original)

    last_departure = window_end + catch_up_grace
    pool: list[AlternativeCandidate] = []
    for cand in pool_source:
        if cand.cancelled:
            continue
        if cand.arrival_time is None:
            continue  # nothing downstream can rank it, and every comparison below reads it
        if cand.departure_time is not None:
            if cand.departure_time <= now or cand.departure_time > last_departure:
                continue
        elif not is_original(cand):
            # A suggestion whose departure we cannot read may already have left. The original is
            # exempt: its catchability was established upstream (`catchable_after`), and it is the
            # baseline rather than a suggestion.
            continue
        if not is_original(cand) and any(ref in affected for ref in cand.transit_refs):
            continue
        pool.append(cand)
    if not pool:
        return None

    def in_time(group: list[AlternativeCandidate]) -> list[AlternativeCandidate]:
        return [cand for cand in group if cand.arrival_time < original_arrival_time]

    def whenever(group: list[AlternativeCandidate]) -> list[AlternativeCandidate]:
        return group

    def latest_departing(group: list[AlternativeCandidate]) -> AlternativeCandidate:
        return min(
            group,
            key=lambda cand: (
                -cand.departure_time.timestamp() if cand.departure_time else float("inf"),
                cand.arrival_time.timestamp(),
                cand.ordinal,
                0 if is_original(cand) else 1,
            ),
        )

    def earliest_arriving(group: list[AlternativeCandidate]) -> AlternativeCandidate:
        return min(
            group,
            key=lambda cand: (
                cand.arrival_time.timestamp(),
                cand.ordinal,
                0 if is_original(cand) else 1,
            ),
        )

    safe = [cand for cand in pool if not cand.risky]
    winner: AlternativeCandidate | None = None
    for tier, pick in ((in_time, latest_departing), (whenever, earliest_arriving)):
        for group in (safe, pool):
            subset = tier(group)
            if subset:
                winner = pick(subset)
                break
        if winner is not None:
            break
    if winner is None or is_original(winner):
        return None

    saved = round((original_predicted_arrival - winner.arrival_time).total_seconds() / 60)
    # The materiality gate. When the disrupted train is neither cancelled nor over its own alert
    # gate, staying on it is a perfectly good outcome and switching has to earn itself — see
    # `_MIN_MATERIAL_MINUTES`, without which a two-minute delay reads as "run for the earlier
    # train". It reads the same rounded figure the wording quotes, so an offer and the minutes it
    # advertises always agree. An absent original is treated as "not in trouble": nothing
    # established that it is, and inventing trouble is how noise gets sent.
    in_real_trouble = original is not None and (original.cancelled or original.risky)
    if not in_real_trouble and saved < _MIN_MATERIAL_MINUTES:
        return None
    return AlternativeJourney(
        departure_time=winner.departure_time,
        arrival_time=winner.arrival_time,
        duration_min=winner.duration_min,
        minutes_saved=saved,
        arrives_before_original=winner.arrival_time < original_predicted_arrival,
        at_risk=winner.risky,
        legs=list(winner.legs),
    )
