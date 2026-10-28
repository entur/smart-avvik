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
  keeps the journey whose arrival is **closest to the disrupted train's *aimed* arrival**, with a
  minute late weighted twice a minute early (2026-10-08). An in-time journey is not preferred at any
  price: leaving two hours early is a worse answer than arriving half an hour late. A replacement
  may therefore legitimately arrive *later* than the train it replaces, so ``minutes_saved`` is
  signed and may be zero or negative. The rule before 2026-10-08 kept the latest-departing in-time
  journey and fell back to the earliest arrival only when nothing arrived in time; the one before
  that (earliest arrival, at least three minutes faster) fired on 26 % of alerting subjects when
  measured on 2026-08-06.
  Re-derive with ``data/queries/alternative_offer_rate.sql``; see
  :func:`decisions._alternative_fields` for the reading and its caveats.
* **The disrupted train competes under the same rules, and usually wins.** It is passed in as
  ``original`` and ranked alongside the candidates; when it wins, there is no alternative to offer.
  Because its arrival is aimed **plus** a non-negative predicted delay while the deadline is that
  same aimed arrival, any rival arriving nearer the deadline beats it — which is why a *materiality* gate sits
  on top (see ``_MIN_MATERIAL_MINUTES``), or a two-minute delay would produce "abandon your train".
* **Candidates that ride the disruption are excluded, the original is not.** A candidate is dropped
  when any of its ``transit_refs`` is one of the affected ``(operator, jkey)`` pairs — JP's reply
  contains the disrupted train's own pattern, and it would win outright. The synthesized
  ``original`` rides an affected journey *by construction*, so it is exempt; dropping it would make
  the "stay put" answer unreachable. That exemption covers **every** rival-cut — the departure
  window and the risk partition too, both of which excluded the baseline in practice; see
  :func:`select_best`. Riding an affected *line* is no longer a hard exclusion: it
  feeds ``AlternativeCandidate.risky``, which the selector treats as a preference (a safe journey
  arriving two hours late must not beat a slightly risky one that gets the traveller there on time).
* **Fail open, always.** Every failure path — timeout, GraphQL ``errors``, an empty
  ``tripPatterns``, a malformed payload — yields ``None``. This sits on the poller thread behind a
  push notification; it must never be the reason an alert does not go out.
* **Keyed TTL cache with single-flight and a failure cooldown**, mirroring
  ``reisevalg._TTLCache``'s semantics (fetch outside the lock, swap under it, a failed fetch serves
  the previous value rather than caching a miss as fresh) but keyed per
  ``(from, to, arrive_by, quantized date_time)`` rather than holding one value. Air legs are
  excluded by restricting ``transportModes``; ``directMode: foot`` keeps JP from proposing a
  two-hour walk.
* **The key is cycle-stable, and that is what makes the TTL worth anything.** This is a blocking
  3 s HTTPS call on the ingestion thread that also rebuilds every BigQuery cache, in a service
  pinned to one replica, so a cold fetch per cycle delays ``/disruptions``, ``/journey``, ``/demo``
  and ``/reisevalg`` alike. The caller anchors one of its two queries on a moving ``now``; with the
  old five-minute bucket two cycles ~4.8 min apart shared a key about 4 % of the time, so **every**
  poller lookup was a cold fetch no matter how long the TTL was. See :data:`_DATE_TIME_QUANTUM` —
  the quantum comes first, the TTL second.
* **Bounded, per path.** ``/impact`` is unauthenticated on a public host and turns one inbound
  request into one outbound JP call, so :data:`_FETCH_MAX_PER_MIN` caps how many *distinct keys*
  may be fetched per minute — counted separately for the request path, the poller path and the
  journey-alert pull read's path, so request traffic can never eat the poller's share and the read
  can eat neither. Past the ceiling this module yields no
  candidates (``alternative_journey: null``) rather than the pod degrading. There is deliberately
  **no global circuit breaker**: one module-global cache serves both paths, so a global breaker
  would let request-path failures — or a caller tripping it on purpose — suppress the alternatives
  in push alerts. The per-key cooldown is the whole of the back-off.
* **Observable without publishing the traveller's trip.** Every lookup logs
  ``event="alternatives_fetch"`` (duration, bytes, pattern count, anchor, cache hit/miss) and every
  failure logs ``event="alternatives_error"``, both carrying a ``pair_digest`` and **never** the
  stop refs: on the poller path those two refs are a registered traveller's own journey, and a
  per-cycle success log of origin + destination + departure time is exactly the trio
  ``/historikk-api`` refuses to serve.
"""

import json
import logging
import os
import re
import secrets
import sys
import threading
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from time import perf_counter
from typing import Any, Literal, NamedTuple

from entur_api import ET_CLIENT_NAME
from llm_message import key_digest
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_JP_URL = "https://api.entur.io/journey-planner/v3/graphql"
_HTTP_TIMEOUT_S = 3.0

# How long a (from, to, arrive_by, date-time quantum) answer is reused. Sized ABOVE the ingestion
# cadence, not below it: measured in prd 2026-08-06 (08:45-10:45 CEST) the poller's rebuild fires
# every ~4.8 minutes, so at the old two minutes the TTL had always expired by the next cycle and
# every poller lookup was a cold 3 s HTTPS call on the thread that also rebuilds every BigQuery
# cache. Ten minutes clears the cadence with room to spare without being tuned to it — the cadence
# is set by how often an upstream SIRI-SX ingestion timestamp changes, a property of somebody else's
# write rate, so nothing here may depend on it more tightly than an order of magnitude.
_TTL = timedelta(minutes=10)
# After a failure, suppress new fetches for this long (still serving any previous value) so a
# degraded JP is not hammered once per poller cycle. It has to clear the same cadence for the same
# reason: at two minutes this cooldown had ALWAYS expired before the next cycle asked again, so the
# sentence above described a suppression that never once happened.
_FAILURE_COOLDOWN = timedelta(minutes=10)
# Cache-key quantum for `date_time`: lookups whose travel time falls in the same half hour share one
# JP answer. COARSE on purpose, and this is the half of the fix that is easy to leave out — raising
# the TTL alone changes nothing. `impact._best_remaining_journey`'s fallback query is anchored on a
# moving `now`, so with the old five-minute bucket two cycles ~4.8 minutes apart landed in the same
# bucket about 4 % of the time: the key, not the TTL, was what made every poller lookup a cold
# fetch. At 30 minutes consecutive cycles share a key ~84 % of the time.
#
# Thirty minutes is safe because the fetch anchor is not what decides feasibility — `select_best`
# is. A shared answer was computed for some other instant inside the same half hour: for
# `arrive_by=False` it may contain departures already gone (dropped, `departure_time <= now`) and
# still contains the later ones, since OTP's dynamic search window reaches ~3 h past the anchor; for
# `arrive_by=True` it may contain arrivals too late for this caller's deadline (dropped, the in-time
# tier compares against `original_arrival_time`). Both directions cost the selector candidates it
# re-checks anyway; neither can smuggle in one it would not have checked.
_DATE_TIME_QUANTUM = timedelta(minutes=30)
# A ceiling on how many DISTINCT keys may be fetched per minute, per path. Modelled on
# `main._CONNECTIVITY_MAX_PER_MIN`, which exists for the identical problem: /impact is public and
# unauthenticated, has no rate limit of its own, runs `plain def` in the ~40-slot anyio threadpool,
# and turns one inbound request into a blocking 3 s outbound call — and the cache cannot bound the
# cost of a caller that keeps inventing `at` values, because every one of them is a new key. Past
# the ceiling `fetch_candidates` yields no candidates, so /impact degrades to
# `alternative_journey: null` instead of the pod degrading and taking /journey, /demo-api and
# /reisevalg-api down with it.
_FETCH_MAX_PER_MIN = 30
# ...and the poller gets its own, larger ceiling, because the paragraph above is an argument about
# the REQUEST path only — a public unauthenticated endpoint turning one inbound call into a blocking
# outbound one. None of it describes the poller, which issues a bounded number of lookups on its own
# schedule for journeys somebody deliberately registered. The two shared a constant by convenience,
# and that convenience was silently setting the alerting budget from an abuse-control number.
#
# 90, against a measured cycle (2026-08-21): the rate window is one minute and a cycle's lookups all
# fire within seconds of each other, ~177 s apart, so this is in practice "per cycle" rather than
# "per minute" — at 10x today's registrations, p90 is ~80 alerting journeys in a cycle. What makes 90
# affordable where 30 was the ceiling is that the lookups are no longer sequential: at
# `varsel._MATCH_WORKERS` = 16 and ~3 s each, 90 of them cost ~17 s of wall clock instead of ~270 s.
# Raise the two together or not at all — widening this alone just makes the poller thread block
# longer.
_POLLER_FETCH_MAX_PER_MIN = 90
# ...and the journey-alert pull read (2026-09-10) gets a THIRD, separate allowance. It fetches the
# alternative on request, like the public endpoint, but it is neither of the two callers above: it
# is not the browser's cycle, so charging it to the poller would let a burst of reads starve the
# push cycle's reserved share; and it is not the anonymous endpoint, so charging it to `request`
# would let an /impact flood empty the read's allowance — and a read that is throttled serves
# `unanswered`, which is exactly the outcome the split exists to keep rare. The public number, on
# the public reasoning: the read is reachable without credentials and a caller can invent keys.
_PULL_FETCH_MAX_PER_MIN = 30
# ...and the trip plans (multi-leg journeys, KTD18) get three more, so plan traffic can never eat
# the alternative lookups' share. Each counts PAGES, one request each: `plans` for the leader's
# phase, which pages through a registered pair's range a few times a day; `plans_pull` for a pod
# fetching a plan for a public read that missed; `realtime` for the batched per-leg JP real-time
# reads (U4).
_PLANS_FETCH_MAX_PER_MIN = 60
_PLANS_PULL_FETCH_MAX_PER_MIN = 20
_REALTIME_FETCH_MAX_PER_MIN = 20
_RATE_WINDOW = timedelta(minutes=1)
# How long past `_HTTP_TIMEOUT_S` a caller waiting on somebody else's in-flight fetch keeps waiting.
# Covers the gap between claiming the key and actually issuing the request; past it the waiter gives
# up with whatever it had, which is exactly the old behaviour. A backstop, not a tuning knob — the
# fetching thread sets its Event on every exit path (see `_release`), so this should never elapse.
_REFRESH_WAIT_MARGIN_S = 1.0
# The materiality gate on "stay put". Inherited from the deleted `_MIN_MINUTES_SAVED`, which used to
# be a filter over every candidate; it is now applied at exactly ONE place — to the winner, and only
# when the disrupted train is neither cancelled nor itself alert-worthy.
#
# Why the gate has to exist at all. The R3 deadline is the original's *aimed* arrival, while the
# original's own arrival is that aimed time PLUS a non-negative predicted delay. So any rival arriving
# nearer the deadline costs less and beats it — meaning "the
# traveller should stay on the train they have" would be unreachable for every delay-driven alert.
# Rail alerts are mostly delay-driven, so without this gate the systematic advice becomes "abandon
# your train and run for an earlier one" — including when the delay is two minutes. Do not
# "simplify" it away: it is not a duplicate of the ranking, it is the reason the ranking is safe to
# state so simply.
_MIN_MATERIAL_MINUTES = 3
# Mirrors `varsel._WINDOW_CATCH_UP` (90 min): how long past a registered window's end a train from
# that window can still be worth offering, sized above the p90 delays the exceedance stack produces.
# It is duplicated rather than imported because `varsel` imports THIS module (`from alternatives
# import AlternativeJourney`), so importing back is a cycle. `select_best` takes it as a parameter so
# a caller that can see both may pass the real constant and keep the two from drifting.
_CATCH_UP_GRACE = timedelta(minutes=90)
# How much worse a minute LATE is than a minute EARLY when ranking alternatives. Early costs the
# traveller waiting time; late can cost an appointment or a connection. Without the asymmetry an
# hourly line ties exactly (one train 60 min early, the next 60 min late). A policy knob, not a
# measured figure (2026-10-08).
_LATE_WEIGHT = 2.0
# How far ahead of `now` a requested travel time may sit before the lookup is skipped entirely.
# Stated in hours rather than as a ratio of anything: a registration for tomorrow morning refreshes
# on every poller cycle from the moment it is created, and the Journey Planner query it would issue
# is the single most expensive call on that path. A tomorrow-window lookup cannot contain a train in
# tomorrow's window anyway — by the time the traveller could act on it, the answer has been
# recomputed many times over.
_MAX_LOOKAHEAD_HOURS = 6
# An NSR stop reference, e.g. NSR:StopPlace:337 or NSR:Quay:11274. Deliberately excludes the quote
# and brace characters that would let a value escape the GraphQL string literal it is spliced into.
# Matched with `fullmatch` — see `_is_stop_ref` for why `$` alone is not enough.
_STOP_REF = re.compile(r"^[A-Za-z]{2,10}:(StopPlace|Quay):[A-Za-z0-9_-]{1,40}$")
# Salt for `pair_digest`, minted per process. See that function: without it the digest is a
# reversible pseudonym over a small public keyspace, and the logs it labels are per-cycle.
_PAIR_SALT = secrets.token_hex(16)


def _is_stop_ref(value: str) -> bool:
    """True when `value` is shaped like an NSR stop reference.

    `fullmatch`, not `match`: Python's `$` also matches immediately BEFORE a single trailing
    newline, so `"NSR:StopPlace:337\\n"` satisfied the anchored pattern above. Nothing exploitable
    followed today — the ref matches no cached call, and a raw newline inside a GraphQL string
    literal fails closed at the endpoint — but this guard exists precisely so the next reader does
    not have to re-derive that, and `fullmatch` closes it for free.
    """
    return bool(_STOP_REF.fullmatch(value or ""))


def within_lookahead(date_time: datetime, now: datetime) -> bool:
    """Whether a travel time is near enough that asking the Journey Planner about it buys anything.

    Public because the caller has to make the same cut *before* it decides to ask a second time.
    :func:`fetch_candidates` refuses a too-distant `date_time` by returning `[]`, which is
    indistinguishable from "JP had nothing" — so a caller whose fallback query is anchored at `now`
    would sail past the horizon guard and issue exactly the expensive call the horizon exists to
    prevent, for a trip nobody can act on for hours. See :data:`_MAX_LOOKAHEAD_HOURS`.
    """
    return date_time - now <= timedelta(hours=_MAX_LOOKAHEAD_HOURS)
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
        fromEstimatedCall {
          cancellation realtime aimedDepartureTime expectedDepartureTime
          destinationDisplay { frontText }
        }
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
    destination: str | None = Field(
        default=None, description="Where the vehicle terminates, its front text: 'Hamar'."
    )
    start_time: datetime | None = Field(default=None, description="Expected start of the leg.")
    end_time: datetime | None = Field(default=None, description="Expected end of the leg.")


# ---- The one trip shape (multi-leg KTD13) ------------------------------------------------------
# A departure with a change, as `payload.trip`, journey-status's `TrainStatus.trip` and the
# `/varsel-api` models all serialise it. Plain objects only: the contract's schema generator
# handles no `oneOf` and no `$defs`.

#: Why a trip with a change is in trouble, one vocabulary for the verdict, the wording, the payload
#: and the decision log. `departure`: the first leg's boarding figure. `leg_cancelled`: a ridden
#: leg's boarding or alighting call declared cancelled (ET or Journey Planner). `leg_departure`: a
#: later leg's boarding figure. `missed_connection`: a change our figures (or JP's expected times)
#: say is missed. `leg_removed`: a ridden leg on a train taken out of today's plan.
TripReasonKind = Literal[
    "departure", "leg_cancelled", "leg_departure", "missed_connection", "leg_removed"
]
#: What a leg was judged from this cycle (multi-leg KTD8's three states): our own figures
#: (`prediction`, the prediction cache scores both its ends, or its train is out of the plan),
#: Journey Planner real-time (`jp_realtime`), or nothing (`none`: «mangler sanntid»).
TripLegSource = Literal["prediction", "jp_realtime", "none"]


class TripLeg(AlternativeLeg):
    """One ridden leg of a trip with a change: :class:`AlternativeLeg` widened, additively.

    KTD13 says to widen `AlternativeLeg` itself. It is a subclass instead, deliberately:
    `AlternativeLeg` is serialised in today's `/impact`, `/varsel-api` and webhook payloads, and with
    the flag off nothing on those wires may change beyond R16's two fields (AE6). The trip shape
    gets the widened leg; every existing payload keeps the leg it has.
    """

    board_stop_place: str | None = Field(
        default=None, description="NSR StopPlace ref where this leg is boarded."
    )
    alight_stop_place: str | None = Field(
        default=None, description="NSR StopPlace ref where this leg is left."
    )
    aimed_departure: datetime | None = Field(
        default=None, description="Aimed (timetabled) departure from the boarding stop."
    )
    aimed_arrival: datetime | None = Field(
        default=None, description="Aimed (timetabled) arrival at the alighting stop."
    )
    cancelled: bool = Field(
        default=False,
        description="The boarding or alighting call of this leg is declared cancelled (ET or the "
        "Journey Planner's real-time data).",
    )
    data_source: TripLegSource | None = Field(
        default=None,
        description="What the leg was judged from: `prediction` (our own figures), `jp_realtime` "
        "(the Journey Planner's real-time data) or `none` (nothing: no live data for this leg). "
        "Null where the leg was not judged.",
    )


class TripTransfer(BaseModel):
    """A change between two legs: transfer `i` sits between legs `i` and `i + 1`."""

    stop_place: str | None = Field(description="NSR StopPlace ref where the arriving leg is left.")
    stop_name: str | None = Field(default=None, description="Its name (null if unresolved).")
    walk_min: int = Field(description="Minutes on foot to the next leg's boarding stop.")
    slack_min: int = Field(
        description="Planned slack in minutes: the next leg's aimed departure, minus the arriving "
        "leg's aimed arrival, minus the walk. Negative when the plan is already too tight.",
    )


class TripProblem(BaseModel):
    """What breaks the planned ride, and the figure the alert gate decided on (KTD6)."""

    kind: TripReasonKind = Field(description="The reason, from the published vocabulary.")
    index: int = Field(
        description="The leg index (0-based) for a leg reason, the transfer index for "
        "`missed_connection`."
    )
    probability: float = Field(
        description="The deciding figure, 0-1; 1.0 for a certainty. It may come from another "
        "continuation of the same first train, the one that bound the verdict (KTD19).",
    )
    threshold: float = Field(description="The bar the alert gate applied to `probability`.")


class TripFacts(BaseModel):
    """A departure with a change, judged end to end: its legs, its changes, when it gets there,
    and what breaks it."""

    legs: list[TripLeg] = Field(description="The ridden legs, in travel order, from 0.")
    transfers: list[TripTransfer] = Field(
        description="The changes, from 0: transfer `i` sits between legs `i` and `i + 1`."
    )
    expected_arrival: datetime | None = Field(
        default=None,
        description="When the traveller is expected at the destination: on the planned route, or "
        "after a broken change on the earliest planned continuation of the same first train that "
        "still holds. Null when nothing in the plan gets them there.",
    )
    arrives_via: str | None = Field(
        default=None,
        description="Where the continuation `expected_arrival` belongs to leaves the planned route "
        "-- its stop name, or its NSR StopPlace ref when the service has no name for it -- set only "
        "when a planned change breaks and another continuation of the same first train arriving "
        "less than 10 minutes later holds (KTD19). Null on the planned route.",
    )
    problem: TripProblem | None = Field(
        default=None, description="Why the departure alerts; null when it does not."
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

    ``operator`` / ``jkey`` / ``stop_order`` are the call's **identity**, and they are here for one
    reason: without them "take this instead" is the only traveller-facing claim in this repo that
    nothing can ever score. Every alert decision gets a nightly hit / false-alarm / miss verdict from
    ``data/queries/label_varsel_decisions.sql``, which joins realized ET outcomes on
    ``(operator, jkey, call_date, stop_order)`` — the same grain the decision log
    (``decisions.build_row``) and ``varsel_decision_groups`` already agree on. The decision row
    carried that key for the *disrupted* train and not for the recommended one, so the advice could
    not be labelled: ``impact._score_candidate`` resolved the boarding :class:`impact.CallPred`,
    which has all three, and then kept only its numbers. These three carry it through.

    They are ``None`` **exactly when** ``scored`` is False, because an unscored candidate resolved no
    call — and the identity comes off the resolved call, never off the Journey Planner. Two thirds of
    a join key joins to nothing, and JP's line codespace is not the operator the ET tables key on
    (which is precisely why ``impact._score_candidate`` refuses to gate ``risky`` on it either).

    Lives in this module rather than in ``impact``, even though ``impact.score_candidates`` is its
    only constructor: ``impact`` imports ``alternatives`` (``from alternatives import
    AlternativeJourney``), and ``AlternativeJourney`` carries a ``departure_prediction`` of this
    type. Defining it in ``impact`` would need the reverse import. ``from __future__ import
    annotations`` does not rescue that — pydantic resolves field annotations when the class is
    built, so the cycle is an ImportError at startup, not a deferred one. It is declared ABOVE
    :class:`AlternativeJourney` for the same reason, one file down: that class annotates two fields
    with this type, and pydantic needs the name bound by the time the class body is evaluated.
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
    # The join key back to realized outcomes. Set from the resolved CallPred and from nothing else;
    # null whenever `scored` is False. See the class docstring.
    operator: str | None = Field(
        default=None,
        description="Ref codespace of the resolved call's journey (VYG, GOA, RUT, ...) — the ET "
        "join key's first part, taken from the prediction cache and NEVER from the Journey "
        "Planner's line codespace. Null exactly when `scored` is False.",
    )
    jkey: str | None = Field(
        default=None,
        description="The resolved call's journey ref minus its codespace — dated for rail, plain "
        "ServiceJourney for bus, whichever form the cache holds. Null exactly when `scored` is "
        "False.",
    )
    stop_order: int | None = Field(
        default=None,
        description="Position of the resolved call along its journey (1 = origin). Completes "
        "(operator, jkey, call_date, stop_order), the grain label_varsel_decisions.sql joins "
        "last_recorded on. Null exactly when `scored` is False.",
    )


class AlternativeJourney(BaseModel):
    """The journey :func:`select_best` chose over the disrupted train.

    Sourced from the Journey Planner, so its times reflect the *published* real-time picture.

    It is **not** necessarily faster than the train it replaces: when nothing arrives before the
    disrupted train's aimed arrival, the fallback tier offers the earliest arrival there is, and a
    replacement for a cancelled train legitimately gets the traveller there later than the timetable
    promised. `minutes_saved` is signed for exactly that reason, and `arrives_before_original`
    answers the traveller's other question — do I still get there when I planned to?

    The two are measured against DIFFERENT anchors and that is deliberate: `minutes_saved` against
    the disrupted train's predicted arrival ("is this better than what I have?"),
    `arrives_before_original` against its aimed one ("do I still make my appointment?"). Collapsing
    them onto one anchor silences the caveat in the case that needs it most.
    """

    departure_time: datetime | None = Field(description="Expected departure from `from_stop`.")
    arrival_time: datetime | None = Field(description="Expected arrival at `to_stop`.")
    duration_min: int | None = Field(default=None, description="Total journey time in minutes.")
    minutes_saved: int = Field(
        description="SIGNED, and INFORMATIONAL — never a filter. Minutes earlier this arrives than "
        "the disrupted train's predicted arrival; zero or negative is a legitimate answer in the "
        "fallback tier, where a replacement for a cancelled train is worth offering even though it "
        "gets the traveller there later than the timetable promised. Nothing is dropped for being "
        "'not fast enough' (the rule that did is gone; measured 2026-08-06 it offered on 26 % of "
        "alerting subjects, see `data/queries/alternative_offer_rate.sql`); the one "
        "gate this figure feeds is the materiality check on a train that is in no real trouble.",
    )
    arrives_before_original: bool = Field(
        default=False,
        description="This arrives strictly before the traveller's PLANNED arrival — the disrupted "
        "train's aimed time, i.e. the appointment they registered around. False means they get "
        "there later than the timetable promised, which is a legitimate answer for a cancelled or "
        "badly delayed train and is what every wording built on this flag says out loud. NOT "
        "measured against the disrupted train's predicted arrival: that is `minutes_saved`'s "
        "anchor, and reading this one off it made a 25-minutes-late arrival report as in time.",
    )
    at_risk: bool = Field(
        default=False,
        description="This journey is itself predicted to be in trouble (over its operator's alert "
        "gate, or running on a line a live situation touches). It is offered anyway because "
        "nothing safer reached the destination in time — a wording that presents it as a clean "
        "escape would be wrong.",
    )
    minutes_after_original: int | None = Field(
        default=None,
        description="How many minutes AFTER the traveller's planned arrival this gets them there — "
        "the figure behind `arrives_before_original = false`, so a wording can say «34 min senere "
        "enn planlagt» instead of leaving the reader to subtract two clock times on a lock screen "
        "(design review 2026-09-01, finding 16). Same anchor as `arrives_before_original` (the "
        "disrupted train's AIMED arrival), never `minutes_saved`'s predicted one. Null when the "
        "alternative is in time, or when the selector had no aimed arrival to measure against.",
    )
    legs: list[AlternativeLeg] = Field(
        default_factory=list, description="The legs to travel, in order."
    )
    departure_prediction: AlternativePrediction | None = Field(
        default=None,
        description="What JP observed and what our models forecast about boarding THIS journey — "
        "the same two-questions-side-by-side shape /impact publishes for the disrupted train. Null "
        "when the winner carried no scored boarding call (nothing resolved a stop to attach it to).",
    )
    arrival_prediction: AlternativePrediction | None = Field(
        default=None,
        description="The same for the alighting call. Null far more often than its departure "
        "sibling: a multi-leg suggestion's last leg is frequently absent from the prediction cache, "
        "and this is never borrowed from the departure — 'when do I get there' answered with the "
        "departure's delay would be a fabricated figure.",
    )


class CandidateLeg(NamedTuple):
    """One ridden leg of a candidate, as the check on its change reads it (multi-leg U6, KTD10):
    the leg's refs in the cache's key form (both, as `transit_refs` collects them), its two stops
    and aimed times, and the seconds walked to the next leg (0 after the last: an egress walk is no
    change). Internal only: `AlternativeLeg` is the wire shape and stays as it is."""

    refs: tuple[tuple[str | None, str], ...]
    board_stop_place: str | None
    alight_stop_place: str | None
    aimed_departure: datetime | None
    aimed_arrival: datetime | None
    walk_after_s: int = 0


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
    transit_legs: list[CandidateLeg] = Field(
        default_factory=list,
        description="The transit legs one by one, what `impact`'s check on a change reads (KTD7). "
        "Empty for a candidate built by hand, which then has no change to check.",
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
    likely_cancelled: bool = Field(
        default=False,
        description="Caller-set: our cancellation probability for a boarding or alighting call is "
        "at or over the operator's alert gate. Dropped from the pool like a declared cancellation.",
    )
    predicted_arrival_time: datetime | None = Field(
        default=None,
        description="Caller-set: the arrival our models expect (aimed + p50 delay), when later than "
        "`arrival_time`. What the ranking reads; `arrival_time` stays JP's, which is what is shown.",
    )
    departure_prediction: AlternativePrediction | None = Field(
        default=None,
        description="Caller-set by `impact.score_candidates`: what JP observed and what our models "
        "forecast about this candidate's boarding call. It rides on the candidate, and not only on "
        "the `CandidateScore` the scorer returns, because :func:`select_best` answers with an "
        "`AlternativeJourney` rather than with the candidate that won — so the only join a caller "
        "could use to attach the winner's own numbers afterwards is identity, which the journey "
        "does not carry. Annotating in place is the same convention `risky` and `cancelled` use.",
    )
    arrival_prediction: AlternativePrediction | None = Field(
        default=None, description="Caller-set: the same for this candidate's alighting call."
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
        # key -> an Event the fetching thread sets when it is done. A dict of Events rather than the
        # plain set this was until 2026-08-21, so a concurrent caller for the SAME key can wait for
        # the answer instead of being told there isn't one. See `wait_for_refresh` in `get`.
        self._refreshing: dict[Any, threading.Event] = {}
        self._failed_at: dict[Any, datetime] = {}

    def get(
        self,
        key: Any,
        fetch: Callable[[], Any],
        now: datetime,
        *,
        label: str | None = None,
        gate: Callable[[], bool] | None = None,
        outcome: dict[str, Any] | None = None,
        wait_for_refresh: bool = False,
    ) -> Any:
        """The cached value for `key`, refreshing it through `fetch` when it is stale.

        `label` is what the failure log names this key by. The key itself is NEVER logged: it
        carries the traveller's origin and destination, and the only caller passes the pair digest
        here for exactly that reason.

        `outcome`, when passed, is filled with `{"served": "hit" | "fetch" | "stale"}` — WHERE the
        returned value came from, which the value alone cannot tell you. `"stale"` covers every path
        that wanted a refresh and could not get one (single-flight, cooldown, a refused gate, a
        raising fetch) yet still had a previous value to hand back. The caller needs that apart from
        the value because a served-stale reply is NOT an answer: `fetch_candidates` reported one as
        answered, so during a Journey Planner outage `impact._best_remaining_journey` believed the
        first query had replied and bought a SECOND blocking call on the ingestion thread — a
        degraded Journey Planner costing twice what a healthy one does, exactly when it could least
        afford it.

        `gate` is consulted at the one instant this class knows a fetch is about to be issued —
        after the cache, the single-flight check and the cooldown have all declined to answer, and
        before anything is claimed. That placement is the whole point: a cache hit must not spend
        the caller's fetch budget, and a refusal must NOT mark the key failed, or a throttled key
        would then be suppressed for the entire cooldown on top of the throttle. A refusal serves
        the previous value if there is one, exactly like the cooldown does. It runs under this
        lock, so the claim and the decision to fetch cannot straddle another thread's claim.

        `wait_for_refresh` decides what a caller who finds this key already in flight does: give up
        with whatever it had (the default, and what the request path wants), or **block until the
        thread that claimed it answers, then read the fresh value** (what the poller wants, and since
        2026-09-10 the journey-alert pull read too — see `_WAITING_PATHS`).

        That distinction did not exist while the poller's lookups were sequential, because they
        could not collide: journey A's fetch had completed and populated the cache before journey B
        was asked at all, so B took a cache hit. Fanning the matcher out (2026-08-21) makes them
        genuinely concurrent, and Oslo-corridor registrations share stop pairs heavily — so without
        this, "another caller is already fetching this key" would become the common case and every
        journey but the first would be told there is no alternative, having spent no fetch and
        learned nothing. That is worse than the sequential behaviour it replaced, which is the kind
        of regression a fan-out is expected to cause and easy to ship unnoticed: nothing errors, the
        cards just quietly stop offering a replacement train.

        The wait is bounded by the fetch's own timeout plus a margin, so a waiter can never block
        longer than doing the fetch itself would have, and a fetching thread that dies without
        setting its Event costs one timeout rather than a hung poller.
        """
        def served(kind: str) -> None:
            if outcome is not None:
                outcome["served"] = kind

        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and now - entry[1] < self._ttl:
                # A live entry IS an answer — the reply is inside its TTL, which is what the TTL
                # means. Only a value handed back because a refresh could not happen is "stale".
                served("hit")
                return entry[0]
            in_flight = self._refreshing.get(key)  # another caller is already fetching this key
            if in_flight is not None and not wait_for_refresh:
                served("stale")
                return entry[0] if entry else None
            if in_flight is None:
                failed_at = self._failed_at.get(key)
                if failed_at is not None and now - failed_at < self._failure_cooldown:
                    served("stale")
                    return entry[0] if entry else None
                if gate is not None and not gate():
                    served("stale")
                    return entry[0] if entry else None
                self._refreshing[key] = threading.Event()

        if in_flight is not None:
            # Somebody else claimed this key; wait for their answer rather than duplicating the
            # fetch or reporting a non-answer. Outside the lock, obviously — the thread we are
            # waiting on needs it to publish the value. `_expire_failures` runs on both of its exit
            # paths, so a waiter that wakes to a failure sees the cooldown, not a phantom claim.
            in_flight.wait(_HTTP_TIMEOUT_S + _REFRESH_WAIT_MARGIN_S)
            with self._lock:
                entry = self._entries.get(key)
            if entry is not None and now - entry[1] < self._ttl:
                # Their reply, inside its TTL. Note `entry[1]` may be marginally LATER than our own
                # `now` (they sampled theirs after we sampled ours), which makes the difference
                # negative and the test true — correct, and worth stating: a value fetched after we
                # asked is fresher than one fetched before, not staler.
                served("hit")
                return entry[0]
            served("stale")
            return entry[0] if entry else None

        try:
            value = fetch()
        except Exception:
            logger.warning(
                "Journey Planner alternative lookup failed for %s; serving previous value",
                label or "an unlabelled key",
                exc_info=True,
                extra={"fields": {"event": "alternatives_error", "pair_digest": label or ""}},
            )
            with self._lock:
                self._failed_at[key] = now
                self._release(key)
                self._expire_failures(now)
                entry = self._entries.get(key)
            served("stale")
            return entry[0] if entry else None
        with self._lock:
            self._entries[key] = (value, now)
            self._failed_at.pop(key, None)
            self._release(key)
            self._expire_failures(now)
            # Keep the cache bounded. The reasoning changed with the key, so it is restated rather
            # than inherited: the old key embedded a five-minute WALL-CLOCK bucket, so a single
            # stop pair minted up to 288 entries a day whether anyone asked again or not, and the
            # cap was mostly holding back the clock. The key now grows with traffic — 48 half-hour
            # quanta a day per (pair, anchor) — and stop pairs are still user-chosen, so it is
            # still unbounded in principle. What bounds the hostile case is `_FETCH_MAX_PER_MIN`
            # above: entries can only be minted as fast as fetches are allowed. Eviction is
            # oldest-written-first, so a caller churning pairs can evict the poller's entries; the
            # cost of that is one refetch inside the poller's own reserved budget, never an alert.
            if len(self._entries) > 500:
                oldest = sorted(self._entries.items(), key=lambda kv: kv[1][1])[:100]
                for k, _ in oldest:
                    self._entries.pop(k, None)
        served("fetch")
        return value

    def _release(self, key: Any) -> None:
        """Drop this key's in-flight claim and wake anyone waiting on it. Caller holds the lock.

        Both exit paths of the fetch go through here, success and failure alike — a waiter that is
        never woken burns its whole timeout, and during a Journey Planner outage the failure path is
        the ONLY one that runs. `Event.set()` does not block, so doing it under the lock is fine.
        """
        event = self._refreshing.pop(key, None)
        if event is not None:
            event.set()

    def _expire_failures(self, now: datetime) -> None:
        """Drop failure marks that can no longer suppress a fetch. Caller holds the lock.

        `_entries` is capped but `_failed_at` was not, and it only ever shed a key on a later
        *success* for that same key. When this was written the key embedded a 5-minute wall-clock
        bucket, so it grew with time rather than with traffic — a stop pair minted up to 288 keys a
        day whether anyone asked again or not, and on a long-lived single pod that never came back.
        The quantum is now 30 minutes and no longer moves with `now` on its own, which slows that
        down but does not stop it: a caller inventing stop pairs still mints a mark per refused
        fetch. Runs on the failure path too — during a total Journey Planner outage the success path
        never executes.
        """
        stale = [k for k, t in self._failed_at.items() if now - t >= self._failure_cooldown]
        for k in stale:
            self._failed_at.pop(k, None)


_cache = _KeyedTTLCache(_TTL, _FAILURE_COOLDOWN)

# ---- Fetch budget ----------------------------------------------------------------------------
# One tally per caller, never one shared. The poller's share is RESERVED: request traffic spends
# only the request path's budget, so an anonymous caller inventing `at` values cannot starve the
# alternatives out of every push notification, and the pull read (2026-09-10) spends only its own.
# That starvation is also why there is no global circuit breaker here — `_cache` is one module
# global shared by every path, so a breaker tripped by request-path failures (or tripped
# deliberately) would suppress the poller's lookups too. The per-key cooldown inside
# `_KeyedTTLCache` is the only back-off.
#
# The ceiling is a LOOKUP, not a conditional: a fourth path added to the Literal without a row here
# raises `KeyError` on its first fetch rather than silently sharing somebody else's number — which
# is how the request and poller paths came to share one constant until 2026-08-21.
_REQUEST_PATH = "request"
_POLLER_PATH = "poller"
_PULL_PATH = "pull"
FetchPath = Literal["request", "poller", "pull", "plans", "plans_pull", "realtime"]
_FETCH_CEILINGS: dict[str, int] = {
    _REQUEST_PATH: _FETCH_MAX_PER_MIN,
    _POLLER_PATH: _POLLER_FETCH_MAX_PER_MIN,
    _PULL_PATH: _PULL_FETCH_MAX_PER_MIN,
    "plans": _PLANS_FETCH_MAX_PER_MIN,
    "plans_pull": _PLANS_PULL_FETCH_MAX_PER_MIN,
    "realtime": _REALTIME_FETCH_MAX_PER_MIN,
}
# The paths whose caller blocks on somebody else's in-flight fetch for the same key — see
# `wait_for_refresh` in `_KeyedTTLCache.get`. Not the request path, and the reason is stated there.
_WAITING_PATHS = frozenset({_POLLER_PATH, _PULL_PATH})
_budget_lock = threading.Lock()
_fetch_calls: dict[str, list[datetime]] = {path: [] for path in _FETCH_CEILINGS}
#: Refusals counted per path, so a bound that binds is ONE record rather than one per refused
#: lookup (U10). The poller asks up to `impact._MAX_ALTERNATIVE_LOOKUPS` times per definition per
#: cycle, so at a hundred thousand definitions a morning where the budget binds would be hundreds of
#: thousands of WARNING lines a cycle -- and a warning at that volume is how a real warning stops
#: being read. The first refusal after a quiet window is still reported AT ONCE, because the
#: interesting moment is when the bound starts binding; the rest are counted and carried into one
#: summary per `_REFUSAL_SUMMARY_WINDOW`.
#:
#: It needs no stale sweep, unlike every budget in `varsel`: it holds one counter per path (six of
#: them, for the life of the process) and a leaked one costs a log line's accuracy rather than an
#: alert. The suites reset it for determinism, not for correctness.
_REFUSAL_SUMMARY_WINDOW = timedelta(minutes=1)
_refusals: dict[str, dict[str, Any]] = {}


def _count_refusal(path: str, now: datetime) -> dict[str, Any] | None:
    """Count one refused lookup; answer a summary when one is due, else ``None``.

    The caller holds :data:`_budget_lock` and logs OUTSIDE it -- this module already documents one
    lock order (`_KeyedTTLCache`'s, then this one) and a logging call made under a lock is a third
    party's code running inside it.
    """
    state = _refusals.get(path)
    if state is None:
        _refusals[path] = {"since": now, "refused": 0}
        return {"refused": 1, "window_s": 0.0}
    state["refused"] += 1
    if now - state["since"] < _REFUSAL_SUMMARY_WINDOW:
        return None
    summary = {"refused": state["refused"], "window_s": (now - state["since"]).total_seconds()}
    _refusals[path] = {"since": now, "refused": 0}
    return summary


def _reserve_fetch(path: str, now: datetime) -> bool:
    """Claim one of `path`'s fetches for the trailing minute, or refuse.

    Time is the caller's `now` rather than a clock read here, for the same reason every other
    decision in this module takes it: `impact.for_trip` samples one instant per response, and a
    ceiling that sampled its own would be untestable without sleeping.

    Called from inside `_KeyedTTLCache.get`'s lock (see its `gate` parameter). Nothing takes these
    two locks in the other order.

    An unknown `path` raises: see :data:`_FETCH_CEILINGS`.
    """
    ceiling = _FETCH_CEILINGS[path]
    summary: dict[str, Any] | None = None
    with _budget_lock:
        calls = _fetch_calls.setdefault(path, [])
        calls[:] = [t for t in calls if now - t < _RATE_WINDOW]
        admitted = len(calls) < ceiling
        if admitted:
            calls.append(now)
        else:
            summary = _count_refusal(path, now)
    if summary is not None:
        logger.warning(
            "Journey Planner lookups rate-limited on the %s path (%d/min); %d refused in the last "
            "%.0f s, each offering no alternative",
            path,
            ceiling,
            summary["refused"],
            summary["window_s"],
            extra={
                "fields": {
                    "event": "alternatives_error",
                    "reason": "rate_limited",
                    "path": path,
                    "refused": summary["refused"],
                    "window_s": summary["window_s"],
                }
            },
        )
    return admitted


def pair_digest(from_stop: str, to_stop: str) -> str:
    """A short label for a stop pair, for logs that must not carry the pair itself.

    On the poller path these two refs ARE a registered traveller's own journey, and a per-cycle
    success log of origin + destination beside a departure time is precisely the trio
    `/historikk-api` refuses to serve and `llm_message` keeps out of Cloud Logging by default.

    **Salted per process, and that is not optional here** (2026-08-06). An unsalted SHA-256 over two
    NSR refs is a reversible pseudonym, not an anonymisation: stop refs are public and enumerable
    (~60k StopPlaces, from a register this app reads on every refresh), so a rainbow table over
    plausible pairs is a few seconds' work — and `event="alternatives_fetch"` now fires on every
    lookup including cache hits, once or twice per registered journey per ~4.8 min cycle. Anyone
    with Cloud Logging read access could otherwise recover each traveller's origin and destination,
    and their travel window from the timestamps. A process-lifetime random preserves everything the
    observability actually needs — correlating records about one pair within one pod's life — and
    nothing else.

    Deliberately NOT joinable with the decision log's `journey_digest`, and it never was: every
    other :func:`llm_message.key_digest` call site hashes the full `varsel.journey_key`
    (`from|to|begin|end|days`), a different preimage entirely. The shared helper is an
    implementation detail, not a join key. Nothing reads this value except a human reading logs.
    """
    return key_digest(f"{_PAIR_SALT}|{from_stop}|{to_stop}")


# Response size is measured where the socket is read (`_post_json`) and reported where the fetch is
# accounted for (`fetch_candidates`), and the two are separated by `_fetch_trip_patterns` — which
# four other suites swap wholesale, so threading a stats argument through it would break a stub
# contract this module does not own. A thread-local carries it instead: the fetch runs on the
# calling thread (outside the cache lock), so the poller and each request thread read their own last
# measurement, and a stubbed fetch simply leaves it unset — logging `bytes: null` rather than a
# number nothing measured.
_response = threading.local()


# ---- HTTP ------------------------------------------------------------------------------------
# Egress guard. A test process must never reach api.entur.io, and the failure mode when it does is
# invisible: this whole module fails OPEN, so a live request from CI eats the 3 s timeout, logs a
# warning nobody reads, and the assertion under it still passes. That is not hypothetical — the
# /varsel-api suite carries a module-level stub of the lookup precisely because one unstubbed test
# would have done it, and the stub had to name the function, so re-pointing the call chain silently
# disarmed it. This guard does not depend on anyone remembering to stub anything.
#
# Opt-in, because "allowed" is the state that has to be asserted rather than assumed:
#   * KIX_ALLOW_JP_NETWORK truthy  -> always allowed (a deliberate live check against the real JP)
#   * KIX_ALLOW_JP_NETWORK falsy   -> always blocked
#   * unset -> allowed only outside a test process.
# The last line is what keeps the deployment safe: requiring the flag in prd would mean a forgotten
# Helm value silently costs every alternative, fail-open, forever — trading a loud test failure for
# a silent production one, which is the wrong direction. Detection covers both ways the suites run:
# `pytest` (CI runs each suite standalone, but a developer does not) and `python test_x.py`.
_ALLOW_NETWORK_ENV = "KIX_ALLOW_JP_NETWORK"


def _in_a_test_process() -> bool:
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return True
    argv0 = os.path.basename(sys.argv[0] if sys.argv else "")
    return argv0.startswith("test_") and argv0.endswith(".py")


def _network_allowed() -> bool:
    """Whether this process may dial the Journey Planner at all. See the note above."""
    flag = os.environ.get(_ALLOW_NETWORK_ENV)
    if flag is not None:
        return flag.strip().lower() in ("1", "true", "yes", "on")
    return not _in_a_test_process()


def _post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    """POST JSON and parse the reply, treating a GraphQL error envelope as a failure.

    JP answers HTTP 200 with an ``errors`` array on a bad query, so raising here routes those
    responses through the cache's failure path instead of parsing them into "no alternative" and
    caching that as a fresh success. Same guard as ``reisevalg._post_json``.

    Raises before opening a socket when :func:`_network_allowed` says no — see the note above it.
    """
    if not _network_allowed():
        raise RuntimeError(
            "Refusing a live Journey Planner request from a test process: stub "
            f"alternatives.fetch_candidates (or set {_ALLOW_NETWORK_ENV}=1 to allow it)"
        )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
        raw = resp.read()
    # Recorded before parsing, so a reply too broken to decode still reports how big it was.
    _response.bytes = len(raw)
    body = json.loads(raw.decode("utf-8"))
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
    data = _post_json(_JP_URL, {"query": query}, headers={"ET-Client-Name": ET_CLIENT_NAME})
    trip = (data.get("data") or {}).get("trip") or {}
    return trip.get("tripPatterns") or []


# ---- Parsing / selection ---------------------------------------------------------------------
def _parse_ts(value: str | None) -> datetime | None:
    """Parse a JP timestamp, rejecting anything we cannot safely compare.

    A *naive* timestamp is treated as malformed rather than coerced: JP always returns an offset, and
    silently assuming one would make the comparison against the disrupted train's arrival (which is
    UTC-aware) either raise or be wrong by up to two hours. Verified 2026-07-29 that a naive value
    used to raise TypeError all the way out of the lookup."""
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


# JP names an SJ Norge train 'SJN:DatedServiceJourney:49_OSL-TND_26-10-15', but its ET arrives through
# Bane NOR as '49:2026-10-15', and that is the jkey the prediction cache holds (KIX-39).
_SJN_DATED = re.compile(r"DatedServiceJourney:(\d+)_.*_(\d{2})-(\d{2})-(\d{2})")


def cache_key(ref: str | None) -> tuple[str | None, str] | None:
    """:func:`split_codespace`, with an SJN dated ref rewritten to the ET key the cache holds.

    Without it an SJN leg never resolves: its own train is not excluded from the rival pool, so a
    late SJ train could be offered as the alternative to itself, and no SJN rival is ever scored.
    """
    split = split_codespace(ref)
    if split is not None and split[0] == "SJN":
        m = _SJN_DATED.fullmatch(split[1])
        if m:
            return ("SJN", f"{m[1]}:20{m[2]}-{m[3]}-{m[4]}")
    return split


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
    # Each ridden leg's fields for `CandidateLeg`, the walk last so a foot leg can add to it.
    ridden: list[list[Any]] = []
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
            # A walk after a ridden leg is that change's walk, `trip_plans._parse_pattern`'s rule;
            # an unreadable time costs the walk, never the pattern.
            start, end = _parse_ts(leg.get("aimedStartTime")), _parse_ts(leg.get("aimedEndTime"))
            if ridden and leg.get("mode") == "foot" and start is not None and end is not None:
                ridden[-1][-1] += round((end - start).total_seconds())
            continue
        leg_refs = tuple(dict.fromkeys(filter(None, map(cache_key, refs))))
        ridden.append([
            leg_refs, _stop_place_ref(leg.get("fromPlace")), _stop_place_ref(leg.get("toPlace")),
            _parse_ts(leg.get("aimedStartTime")), _parse_ts(leg.get("aimedEndTime")), 0,
        ])
        for split in leg_refs:
            if split not in seen_refs:
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
    if ridden:
        ridden[-1][-1] = 0  # the walk after the last leg is the egress, not a change

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
                destination=_as_dict(
                    _as_dict(leg.get("fromEstimatedCall")).get("destinationDisplay")
                ).get("frontText"),
                start_time=_parse_ts(leg.get("expectedStartTime")),
                end_time=_parse_ts(leg.get("expectedEndTime")),
            )
            for leg in legs
        ],
        cancelled=cancelled,
        transit_refs=transit_refs,
        lines=lines,
        transit_legs=[CandidateLeg(*fields) for fields in ridden],
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


def _pattern_count(patterns: Any) -> int | None:
    """How many trip patterns came back, or None when that cannot be read off the value.

    Never raises and never iterates: a malformed reply can put a string or an object where JP puts
    a list, and this runs on the logging path, which must not be able to cost a lookup.
    """
    if patterns is None:
        return 0
    if isinstance(patterns, list | tuple):
        return len(patterns)
    return None


def _log_lookup(
    *,
    pair_digest: str,
    arrive_by: bool,
    path: str,
    fetched: bool,
    throttled: bool,
    duration_ms: int | None,
    response_bytes: int | None,
    n_patterns: int | None,
) -> None:
    """Record one lookup under `event="alternatives_fetch"`.

    This module logged only failures until 2026-08-06, and dev recorded none in 24 h — so a
    Journey Planner that is 2.9 s slow but perfectly healthy was completely invisible, and "watch
    the error volume" could not have detected a fetch rate this unit exists to change. Cache hits
    are logged too, or the hit rate the cache key was rewritten for would be unmeasurable.

    Carries the digest, never either stop ref — see :func:`pair_digest`.
    """
    logger.info(
        "Journey Planner alternative lookup (%s)",
        "fetch" if fetched else ("throttled" if throttled else "cache"),
        extra={
            "fields": {
                "event": "alternatives_fetch",
                "pair_digest": pair_digest,
                "arrive_by": arrive_by,
                "path": path,
                "cache_hit": not fetched and not throttled,
                "throttled": throttled,
                "duration_ms": duration_ms,
                "bytes": response_bytes,
                "n_patterns": n_patterns,
            }
        },
    )


def fetch_candidates(
    from_stop: str,
    to_stop: str,
    *,
    arrive_by: bool,
    date_time: datetime,
    now: datetime,
    path: FetchPath = _REQUEST_PATH,
    outcome: dict[str, Any] | None = None,
) -> list[AlternativeCandidate]:
    """Journey Planner candidates from `from_stop` to `to_stop`, in JP's reply order.

    `date_time` is read as "leave no earlier than" when `arrive_by` is False and as "be there by"
    when it is True. `now` is passed in and never defaulted internally, so the caller's single
    sampled instant governs the horizon cut, the cache and the fetch ceiling alike —
    `impact.for_trip` already samples `now` once precisely so `as_of` and the observation cut cannot
    straddle two instants, and this lookup must sit on that same instant.

    `path` says which side of the reserved budget this lookup spends: `"poller"` for the refresh
    cycle behind push alerting, `"pull"` for the journey-alert pull read, `"request"` for anything
    serving an inbound HTTP request. It defaults to `"request"`, the conservative reading — an
    unwired caller is then throttled by the public budget rather than silently spending the
    poller's. See :data:`_FETCH_MAX_PER_MIN`.

    `outcome`, when passed, is filled with `{"answered": bool}`: **was a reply obtained at all**, as
    distinct from "the reply was empty". Fail-open collapses timeout, GraphQL `errors`, cooldown,
    single-flight, throttle and refused ref into the same `[]` an answered-but-empty search
    produces, and a caller that reads that as "nothing available" pays for a SECOND blocking 3 s
    query on the ingestion thread — so a Journey Planner outage cost two calls per lookup instead of
    one (fixed 2026-08-06). The distinction already existed one layer down and was thrown away:
    `_KeyedTTLCache.get` returns `None` for "no reply obtained" and `[]` only for a genuinely empty
    one. An out-parameter rather than a richer return type on purpose: four suites stub this
    function wholesale, and an unstubbed or legacy caller must keep the plain-list contract.

    Selection is somebody else's job. This returns everything JP offered, parsed; it does not rank,
    filter by usefulness, or decide anything.

    Fail open, ALWAYS: a refused stop ref, a lookup too far ahead, a throttled path, an outage, a
    GraphQL `errors` envelope, an empty reply, or an uninterpretable one all yield `[]`. This sits
    on the poller thread behind a push notification and must never be the reason an alert does not
    go out.
    """
    def answered(value: bool) -> None:
        if outcome is not None:
            outcome["answered"] = value

    answered(False)  # nothing has replied yet; every early return below is a non-answer
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
    if not within_lookahead(date_time, now):
        # A registration for tomorrow morning is re-examined on every poller cycle from the moment
        # it is created, and this is the most expensive call on that path. A lookup that far ahead
        # cannot contain a train in tomorrow's window, so it buys nothing at any price.
        return []
    digest = pair_digest(from_stop, to_stop)
    if arrive_by:
        # The EXACT deadline, never a bucket (2026-08-06). An `arriveBy` reply is anchored on the
        # time it was asked about and structurally cannot contain a journey arriving after it, so
        # two travellers sharing a 30-minute bucket shared a reply that was a valid answer for only
        # the earlier of them: the later one was handed a pool anchored up to 29 minutes early,
        # which cannot hold the LATEST in-time departure the primary rule exists to find. The
        # non-empty pool then also suppressed the depart-after query that would have contained it.
        # Bucketing costs nothing here anyway — this anchor is the disrupted train's AIMED arrival,
        # already stable across the poller's refreshes, so the exact value hits the cache just as
        # often. The quantum below is for the fallback query, whose anchor is `now` and therefore
        # moves every cycle; that is the case :data:`_DATE_TIME_QUANTUM` was introduced for.
        anchor = int(date_time.timestamp())
    else:
        anchor = int(date_time.timestamp() // _DATE_TIME_QUANTUM.total_seconds())
    key = (from_stop, to_stop, arrive_by, anchor)
    # Set by the two closures below, which run at most once each and only inside `_cache.get`. A
    # dict rather than `nonlocal` so the closures stay readable next to what they record.
    state: dict[str, Any] = {"fetched": False, "throttled": False, "ms": None, "bytes": None}

    def gate() -> bool:
        if _reserve_fetch(path, now):
            return True
        state["throttled"] = True
        return False

    def fetch() -> list[dict]:
        state["fetched"] = True
        _response.bytes = None
        started = perf_counter()
        try:
            return _fetch_trip_patterns(from_stop, to_stop, date_time, arrive_by=arrive_by)
        finally:
            # In `finally` so a timeout — the reading this log exists to make visible — is timed
            # too, even though the cache turns it into a warning and a served-stale value.
            state["ms"] = round((perf_counter() - started) * 1000)
            state["bytes"] = getattr(_response, "bytes", None)

    cache_outcome: dict[str, Any] = {}
    patterns = _cache.get(
        key,
        fetch,
        now,
        label=digest,
        gate=gate,
        outcome=cache_outcome,
        # The poller waits on somebody else's in-flight fetch for this key. Its callers are
        # concurrent with each other and share stop pairs heavily, so waiting is what keeps a
        # fan-out from turning "one fetch, N answers" into "one fetch, one answer and N-1 shrugs".
        # The pull read waits too (2026-09-10): it runs on its own executor, not in the anyio
        # request pool, and the key it wants is often the one the poller's pass is fetching at that
        # instant. The request path keeps giving up immediately: an inbound HTTP request holding an
        # anyio threadpool slot to wait on an unrelated request's fetch is how a slow upstream turns
        # into a queue, and `/impact` already degrades to `alternative_journey: null` gracefully.
        wait_for_refresh=path in _WAITING_PATHS,
    )
    _log_lookup(
        pair_digest=digest,
        arrive_by=arrive_by,
        path=path,
        fetched=bool(state["fetched"]),
        throttled=bool(state["throttled"]),
        duration_ms=state["ms"],
        response_bytes=state["bytes"],
        n_patterns=_pattern_count(patterns),
    )
    # `None` is the cache saying "no reply was obtained" — an outage, the post-failure cooldown, or
    # another thread's in-flight refresh with nothing stored yet. A throttled lookup counts as
    # unanswered too even when a previous value is served: re-asking on another anchor would only be
    # refused by the same spent budget, one more log line poorer.
    #
    # `served == "stale"` is the same judgement for the case a non-None value hides (2026-08-06):
    # the refresh failed, was single-flighted, or was inside the post-failure cooldown, and the
    # PREVIOUS value came back. It is worth serving — stale advice beats none — but it is not the
    # Journey Planner having answered this question, and treating it as one is what let one outage
    # buy two blocking calls per journey. `not state["throttled"]` is kept beside it deliberately:
    # a test double for `_cache` that ignores `outcome` still reports a refused gate correctly.
    answered(
        patterns is not None
        and not state["throttled"]
        and cache_outcome.get("served") != "stale"
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
            digest,
            exc_info=True,
            extra={"fields": {"event": "alternatives_error", "pair_digest": digest}},
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
    outcome: dict[str, Any] | None = None,
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

    1. **Pool.** Drop anything cancelled or `likely_cancelled` and anything nothing can rank (no
       arrival time). Of the
       RIVALS additionally drop those whose departure has passed (`> now`, strictly, so a departure
       exactly at `now` is not offered), those beyond `window_end + catch_up_grace`, those with no
       readable departure at all, and any riding one of `affected_refs`. That last exclusion carries
       the reply's copy of the disrupted train itself — the caller synthesizes `original` from our
       own scoring rather than reading it out of JP, so JP's own pattern for the same train is still
       in the list as `is_original=False` and would win outright.

       **Every one of those cuts is about rivals, and the original is exempt from all of them**
       (2026-08-06). It rides an affected journey by construction, and its `departure_time` is the
       AIMED departure while its catchability was settled upstream against the EXPECTED one
       (`impact._original_candidate`, `catchable_after`) — so a train aimed 08:00, predicted 30 min
       late and still perfectly catchable at 08:05 was being deleted from its own replacement
       contest. Dropping the baseline does not make "stay put" less likely; it makes it
       *unreachable*, and hands the answer to whatever the pool happens to contain.
    2. **Closest arrival to the deadline, late weighted double** (2026-10-08). The cost of a
       candidate is its minutes early plus :data:`_LATE_WEIGHT` x its minutes late, against
       `original_arrival_time`. Until then an in-time candidate always won, however early: when
       Vy removed the R14 12:04, 13:04 and 14:04, all three were offered the SJ 11:46, which for
       the 14:04 meant leaving 2 h 18 min early rather than taking the 14:31 arriving 34 min late.

       Risk enters through our own predictions, not a separate penalty (2026-10-13). The cost
       reads `predicted_arrival_time` (aimed + p50 delay, when later than JP's expectation) and
       the original's own predicted arrival, so a train we expect to be late is ranked as late.
       A train whose cancellation probability is at or over the operator's alert gate is dropped
       in rule 1 (`likely_cancelled`), the original included: below the gate that probability is
       overconfident (Vy, 1 Sep - 12 Oct 2026: predicted 0.30-0.65, cancelled 2 % without an SX
       message and 9-14 % with one; at 0.65 and over, 30 % and 55 %). `risky` no longer ranks
       anything; it is published as `at_risk` on the offer.
    3. **Ties** break on earlier arrival, then on `ordinal` (JP's own reply order), then in favour of
       the original. Not cosmetic: this runs again on every refresh, and an unstable winner would
       flip both the card's wording and which situation the written message is cached under.
    4. **The original competes.** If it wins, there is nothing to offer — plus the materiality gate
       described on `_MIN_MATERIAL_MINUTES`, without which "stay put" is unreachable for every
       delay-driven alert.

    `outcome`, when passed, is filled with `{"original_is_best": bool}` — the VERDICT behind
    `ImpactResponse.original_is_best`, published here rather than inferred by the caller. It is True
    only when the original was in this function's own pool, at least one rival was there with it,
    and the comparison still came out in the original's favour (it won, or the materiality gate kept
    the traveller on it). A `None` return with a False verdict means "nothing was established" —
    silence, not endorsement — and the two must not be worded alike: `varsel._reconcile_caches`
    evicts a cached alternative on the verdict, so a caller deriving it from its own pre-filtered
    pool would evict on a comparison that never happened.

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

    if outcome is not None:
        outcome["original_is_best"] = False  # no comparison has happened yet

    last_departure = window_end + catch_up_grace
    pool: list[AlternativeCandidate] = []
    for cand in pool_source:
        if cand.cancelled or cand.likely_cancelled:
            continue
        if cand.arrival_time is None:
            continue  # nothing downstream can rank it, and every comparison below reads it
        if is_original(cand):
            # The baseline, not a suggestion: exempt from every remaining cut. See rule 1 — its
            # departure is the AIMED one and its catchability was settled upstream, so a delayed
            # train the traveller can still board must not be deleted from its own contest.
            pool.append(cand)
            continue
        if cand.departure_time is None or cand.departure_time <= now:
            continue  # a suggestion whose departure we cannot read may already have left
        if cand.departure_time > last_departure:
            continue
        if any(ref in affected for ref in cand.transit_refs):
            continue
        pool.append(cand)
    if not pool:
        return None
    originals = [cand for cand in pool if is_original(cand)]
    rivals = [cand for cand in pool if not is_original(cand)]

    def cost(cand: AlternativeCandidate) -> float:
        """Minutes early plus LATE_WEIGHT x minutes late against the traveller's deadline, read off
        the arrival our models predict (the original's `arrival_time` already is one)."""
        arrival = cand.predicted_arrival_time or cand.arrival_time
        diff = (arrival - original_arrival_time).total_seconds() / 60
        return -diff if diff <= 0 else _LATE_WEIGHT * diff

    def closest(group: list[AlternativeCandidate]) -> AlternativeCandidate:
        return min(
            group,
            key=lambda cand: (
                cost(cand),
                cand.arrival_time.timestamp(),
                cand.ordinal,
                0 if is_original(cand) else 1,
            ),
        )

    def record_stay_put() -> None:
        """Record "the traveller should keep the train they have" — a verdict, when one was earned.

        Claimed only when the original was actually in the pool AND had a rival to beat. A pool of
        one is not a comparison: an unanswered Journey Planner leaves the original alone in it, and
        reporting that as "we chose your train" would evict a cached alternative downstream on
        strength of a contest that never took place.
        """
        if outcome is not None:
            outcome["original_is_best"] = bool(originals) and bool(rivals)

    winner = closest(pool)
    if winner is None or is_original(winner):
        record_stay_put()
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
        record_stay_put()
        return None
    return AlternativeJourney(
        departure_time=winner.departure_time,
        arrival_time=winner.arrival_time,
        duration_min=winner.duration_min,
        minutes_saved=saved,
        # Against the traveller's DEADLINE (the aimed arrival), not against the delayed train's
        # predicted one — `minutes_saved` above is the other anchor, deliberately. Every consumer of
        # this flag talks about the scheduled arrival: `ankommer_i_tide` in the message input, the
        # template's " – senere enn planlagt ankomst", and the prompt rule that forbids claiming an
        # on-time arrival. Read off the predicted arrival (until 2026-08-06) it said True for an
        # alternative arriving 25 minutes after the traveller was due, whenever their own train was
        # predicted later still — so the push said nothing and the model was free to write "du
        # rekker fram i tide" to somebody who does not.
        arrives_before_original=winner.arrival_time < original_arrival_time,
        # The same anchor as the flag above, as a number: how late against the PLAN. Only carried
        # when it is positive, i.e. exactly when the flag is False, so the two can never disagree.
        minutes_after_original=(
            round((winner.arrival_time - original_arrival_time).total_seconds() / 60)
            if winner.arrival_time >= original_arrival_time
            else None
        ),
        at_risk=winner.risky,
        legs=list(winner.legs),
        # Carried off the winning candidate, where `impact.score_candidates` annotated them. This
        # function answers with a journey rather than with the candidate that won, so a caller
        # cannot attach the winner's own numbers afterwards — there is no join back.
        departure_prediction=winner.departure_prediction,
        arrival_prediction=winner.arrival_prediction,
    )
