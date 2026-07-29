"""Faster-alternative lookup for /impact — "is there a better way to get there than this train?"

When ``/impact`` is asked about a trip (``from_stop`` → ``to_stop``) and a situation is predicted to
hit the train the traveller depends on, the useful next question is not another probability: it is
whether some *other* departure gets them to ``to_stop`` sooner. This module answers that from the
Entur Journey Planner (JP) v3 GraphQL API.

Design notes
------------
* **The bar is the disrupted train, not the timetable.** A pattern qualifies only when its expected
  arrival at ``to_stop`` beats ``reference_arrival`` — the matched train's *predicted* arrival
  (aimed + predicted delay). A pattern that is faster than the timetable but slower than what the
  passenger already has is not an alternative.
* **Patterns that ride the disruption are excluded.** A pattern is dropped when any transit leg runs
  the affected journey (matched on the service-journey ref, stripped of its codespace, which covers
  both the rail ``DatedServiceJourney`` refs and the bus ``ServiceJourney`` refs our cache keys on)
  or the affected line. Recommending the same disrupted line back to the traveller is worse than
  saying nothing.
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
# Only propose an alternative that saves at least this much — shaving a minute off is noise, and
# quoting it as advice would cost more trust than it buys.
_MIN_MINUTES_SAVED = 3
# How many patterns to ask JP for. Enough that a few can be filtered out (wrong line, too slow)
# and something still survives.
_NUM_TRIP_PATTERNS = 6

# Modes a traveller on a disrupted public-transport trip could actually switch to. Air is
# deliberately absent (JP will happily route Oslo -> Stavanger by plane otherwise), and every
# access/egress/direct mode is foot so the fallback is never "drive" or "cycle for two hours".
_TRIP_QUERY = """
{
  trip(
    from: {place: "%(from_stop)s"}
    to: {place: "%(to_stop)s"}
    dateTime: "%(date_time)s"
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
      duration
      legs {
        mode
        expectedStartTime
        expectedEndTime
        line { id publicCode }
        serviceJourney { id }
        datedServiceJourney { id }
        fromPlace { name }
        toPlace { name }
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
    """A journey from `from_stop` to `to_stop` that arrives before the disrupted train would.

    Sourced from the Journey Planner, so it reflects the *published* real-time picture — it is not
    scored by our per-call models and carries no cancellation or delay probability of its own.
    """

    departure_time: datetime | None = Field(description="Expected departure from `from_stop`.")
    arrival_time: datetime | None = Field(description="Expected arrival at `to_stop`.")
    duration_min: int | None = Field(default=None, description="Total journey time in minutes.")
    minutes_saved: int = Field(
        description="Minutes earlier this arrives than the disrupted train's predicted arrival.",
    )
    legs: list[AlternativeLeg] = Field(
        default_factory=list, description="The legs to travel, in order."
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
                entry = self._entries.get(key)
            return entry[0] if entry else None
        with self._lock:
            self._entries[key] = (value, now)
            self._failed_at.pop(key, None)
            self._refreshing.discard(key)
            # Keep the cache bounded: stop pairs are user-chosen, so this is unbounded in principle.
            if len(self._entries) > 500:
                oldest = sorted(self._entries.items(), key=lambda kv: kv[1][1])[:100]
                for k, _ in oldest:
                    self._entries.pop(k, None)
        return value


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


def _fetch_patterns(from_stop: str, to_stop: str, depart_after: datetime) -> list[dict]:
    query = _TRIP_QUERY % {
        "from_stop": from_stop,
        "to_stop": to_stop,
        "date_time": depart_after.isoformat(),
        "num": _NUM_TRIP_PATTERNS,
    }
    data = _post_json(_JP_URL, {"query": query}, headers={"ET-Client-Name": _ET_CLIENT_NAME})
    trip = (data.get("data") or {}).get("trip") or {}
    return trip.get("tripPatterns") or []


# ---- Parsing / selection ---------------------------------------------------------------------
def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _strip_codespace(ref: str | None) -> str | None:
    """'VYG:DatedServiceJourney:81613_X' -> 'DatedServiceJourney:81613_X' (our jkey form)."""
    if not ref:
        return None
    _, sep, rest = ref.partition(":")
    return rest if sep else ref


def _rides_the_disruption(
    pattern: dict, excluded_jkeys: set[str], excluded_lines: set[str]
) -> bool:
    for leg in pattern.get("legs") or []:
        line_id = (leg.get("line") or {}).get("id")
        if line_id and line_id in excluded_lines:
            return True
        for key in ("datedServiceJourney", "serviceJourney"):
            ref = _strip_codespace((leg.get(key) or {}).get("id"))
            if ref and ref in excluded_jkeys:
                return True
    return False


def _to_alternative(pattern: dict, reference_arrival: datetime) -> AlternativeJourney | None:
    arrival = _parse_ts(pattern.get("expectedEndTime"))
    if arrival is None:
        return None
    saved = round((reference_arrival - arrival).total_seconds() / 60)
    if saved < _MIN_MINUTES_SAVED:
        return None
    duration = pattern.get("duration")
    return AlternativeJourney(
        departure_time=_parse_ts(pattern.get("expectedStartTime")),
        arrival_time=arrival,
        duration_min=round(duration / 60) if duration else None,
        minutes_saved=saved,
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
            for leg in pattern.get("legs") or []
        ],
    )


def select_faster(
    patterns: Iterable[dict],
    *,
    reference_arrival: datetime,
    excluded_jkeys: set[str],
    excluded_lines: set[str],
) -> AlternativeJourney | None:
    """The earliest-arriving pattern that beats `reference_arrival` and avoids the disruption.

    Pure — no I/O — so the selection rules are testable against a recorded JP payload.
    """
    best: AlternativeJourney | None = None
    for pattern in patterns:
        if _rides_the_disruption(pattern, excluded_jkeys, excluded_lines):
            continue
        candidate = _to_alternative(pattern, reference_arrival)
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
    an empty result, and when nothing saves at least `_MIN_MINUTES_SAVED` minutes.
    """
    now = now or datetime.now(depart_after.tzinfo)
    bucket = int(depart_after.timestamp() // _DEPART_BUCKET.total_seconds())
    key = (from_stop, to_stop, bucket)
    patterns = _cache.get(key, lambda: _fetch_patterns(from_stop, to_stop, depart_after), now)
    if not patterns:
        return None
    return select_faster(
        patterns,
        reference_arrival=reference_arrival,
        excluded_jkeys=excluded_jkeys,
        excluded_lines=excluded_lines,
    )
