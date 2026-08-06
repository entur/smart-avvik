"""Checks for the Journey Planner alternative lookup — recorded payloads, no network.

The JP document itself was verified live with curl 2026-07-29 (Oslo S -> Drammen / Stavanger) and
again 2026-08-06 when it grew the aimed times, the estimated calls and both stop-place refs; these
tests pin the parser, the selection rules and the fail-open behaviour around them.

The parser is checked against `RECORDED_JP_REPLY` at the bottom of this file — a payload captured
from that 2026-08-06 curl, not a hand-written fixture. The distinction is the point: a fixture
written from the parser's own assumptions would agree with the parser no matter what either of
them did, and the JP document is hand-built GraphQL whose sibling in reisevalg.py already cost a
fix commit to a brace slip (PR #22).

Run: uv run --with pydantic python test_alternatives.py  (or under pytest)
"""

import json
from datetime import UTC, datetime, timedelta

import alternatives
from alternatives import (
    _TRIP_QUERY,
    AlternativeCandidate,
    AlternativeLeg,
    _KeyedTTLCache,
    fetch_candidates,
    parse_candidates,
    select_best,
    split_codespace,
)

REF = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)  # the disrupted train's predicted arrival
FROM = "NSR:StopPlace:337"  # Oslo S — the lookup requires real NSR ref shapes (see the guard test)
TO = "NSR:StopPlace:418"  # Asker stasjon


# ---- select_best: the selection rule ------------------------------------------------------------
# Every scenario below sits on one clock, so the numbers can be read off the offsets:
#
#   NOW      +0    the instant the selection runs
#   original +5    the disrupted train's departure — still catchable, or it would not be in the pool
#   WINDOW_END +30 the end of the traveller's registered window; + 90 min catch-up grace
#   DEADLINE +60   the disrupted train's AIMED arrival: the bar for "still in time"
#   (last offerable departure: +120)
#
# The train's PREDICTED arrival is stated per test and is a different anchor from DEADLINE — it is
# aimed + a non-negative delay, which is exactly why it can never sit in the in-time tier.
NOW = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)
DEADLINE = NOW + timedelta(minutes=60)
WINDOW_END = NOW + timedelta(minutes=30)
# The disrupted train's own (operator, jkey) — the pair the caller passes as `affected_refs`.
AFFECTED = ("VYG", "DatedServiceJourney:81613_DRM-DAL_26-08-06")
AFFECTED_BUS = ("RUT", "ServiceJourney:fe362750909c4c05aec47f2891ee3a5d")


def _cand(dep_min, arr_min, *, ordinal=0, risky=False, cancelled=False, refs=(),
          is_original=False, line="VYG:Line:R11"):
    """One candidate departing at NOW+`dep_min` and arriving at NOW+`arr_min`."""
    return AlternativeCandidate(
        departure_time=NOW + timedelta(minutes=dep_min),
        arrival_time=NOW + timedelta(minutes=arr_min),
        duration_min=arr_min - dep_min,
        legs=[
            AlternativeLeg(mode="rail", line=line, public_code="R11", from_name="Oslo S",
                           to_name="Asker stasjon")
        ],
        transit_refs=list(refs),
        lines=[line],
        cancelled=cancelled,
        ordinal=ordinal,
        is_original=is_original,
        risky=risky,
    )


def _original(*, predicted_min, dep_min=5, cancelled=False, risky=False):
    """The disrupted train as the caller synthesizes it: `is_original`, and riding an affected
    journey by construction — which is the whole reason the affected-refs drop has to exempt it."""
    return _cand(dep_min, predicted_min, refs=[AFFECTED], is_original=True,
                 cancelled=cancelled, risky=risky)


def _best(candidates, original, *, predicted=None, deadline=DEADLINE, now=NOW,
          window_end=WINDOW_END, affected=(AFFECTED, AFFECTED_BUS)):
    """`select_best` with the scenario's anchors. `original_predicted_arrival` defaults to the
    original's own arrival, which is the invariant the caller maintains."""
    if predicted is None:
        predicted = original.arrival_time if original is not None else deadline
    return select_best(
        candidates,
        original=original,
        original_arrival_time=deadline,
        original_predicted_arrival=predicted,
        now=now,
        window_end=window_end,
        affected_refs=set(affected),
    )


def test_the_latest_departing_journey_that_still_arrives_in_time_wins():
    """R3. NOT the earliest arrival: a traveller who makes their appointment either way would
    rather leave later than run for the first train out."""
    got = _best(
        [_cand(5, 40, ordinal=0), _cand(15, 50, ordinal=1), _cand(10, 45, ordinal=2)],
        _original(predicted_min=90),
    )
    assert got is not None
    assert got.departure_time == NOW + timedelta(minutes=15)
    assert got.arrival_time == NOW + timedelta(minutes=50), "the +40 arrival must NOT win"
    assert got.minutes_saved == 40


def test_when_nothing_arrives_in_time_the_earliest_arrival_wins():
    """R4. The deadline is already lost, so the only remaining question is how late."""
    got = _best(
        [_cand(5, 80, ordinal=0), _cand(20, 70, ordinal=1), _cand(30, 75, ordinal=2)],
        _original(predicted_min=100),
    )
    assert got is not None
    assert got.arrival_time == NOW + timedelta(minutes=70)
    assert got.departure_time == NOW + timedelta(minutes=20), "latest-departure is tier 1's rule"
    assert got.minutes_saved == 30
    # It arrives after the DEADLINE (+60) but before the disrupted train's predicted arrival
    # (+100). The flag is about the train the traveller has, not about their appointment — the two
    # anchors are different times and reading it off the wrong one inverts it in exactly this case.
    assert got.arrives_before_original is True


def test_a_replacement_arriving_later_than_the_original_would_is_still_offered():
    """The fallback tier's whole point. A replacement for a cancelled train legitimately arrives
    after the train it replaces — `minutes_saved` is signed, and this is the sign that matters."""
    got = _best([_cand(10, 75)], _original(predicted_min=60, cancelled=True))
    assert got is not None
    assert got.minutes_saved == -15
    assert got.arrives_before_original is False


def test_a_cancelled_candidate_is_never_chosen():
    """R5. The one that would win tier 1 outright is the one that is not running."""
    got = _best(
        [_cand(20, 55, cancelled=True, ordinal=0), _cand(10, 45, ordinal=1)],
        _original(predicted_min=90),
    )
    assert got is not None
    assert got.departure_time == NOW + timedelta(minutes=10)


def test_a_cancelled_original_cannot_suppress_the_alternative():
    """Today's headline failure: the old rule demanded a candidate beat the disrupted train's
    predicted arrival by 3 minutes, so a cancelled train — whose replacement is always later —
    got no alternative at all, which is precisely when the traveller needs one."""
    got = _best(
        [_cand(10, 70, ordinal=0), _cand(25, 85, ordinal=1)],
        _original(predicted_min=60, cancelled=True),
    )
    assert got is not None, "a later, slower journey is the honest answer here"
    assert got.arrival_time == NOW + timedelta(minutes=70)
    assert got.minutes_saved == -10


def test_the_disrupted_trains_own_pattern_is_not_offered_back():
    """JP's reply contains the disrupted train itself — the caller synthesizes `original` from our
    own scoring rather than reading it out of JP, so that pattern is in the list as
    `is_original=False` and would win outright. Both ref forms have to catch it: rail jkeys are
    DatedServiceJourney refs, bus jkeys plain ServiceJourney ones.
    """
    for label, ref in (("rail", AFFECTED), ("bus", AFFECTED_BUS)):
        got = _best(
            [_cand(20, 55, refs=[ref], ordinal=0), _cand(10, 45, ordinal=1)],
            _original(predicted_min=90),
        )
        assert got is not None
        assert got.departure_time == NOW + timedelta(minutes=10), f"the {label} ref must be dropped"

    # Matched on the (operator, jkey) PAIR, not the jkey alone: bus ServiceJourney refs are not
    # date-scoped, so one jkey can name two operators' journeys (the reason U2 kept both halves).
    other_operator = _cand(20, 55, refs=[("GOA", AFFECTED_BUS[1])], ordinal=0)
    got = _best([other_operator, _cand(10, 45, ordinal=1)], _original(predicted_min=90))
    assert got.departure_time == NOW + timedelta(minutes=20), "a different operator is not affected"


def test_the_synthesized_original_survives_that_same_exclusion():
    """It rides an affected journey by construction, so applying the drop to it would make "stay
    put" unreachable and no alternative could ever be suppressed. A/B on the one thing under test:
    same pool, same rules, only the original's arrival moves."""
    rival = _cand(10, 80, risky=True)  # risky, so R6 cannot hand it the tier before ranking
    stays = _best([rival], _original(predicted_min=60, risky=True))
    switches = _best([rival], _original(predicted_min=120, risky=True))
    assert stays is None, "the original arrives first, so it is in the pool and it won"
    assert switches is not None, "and when it arrives later the very same candidate is offered"


def test_a_candidate_departing_past_the_window_grace_is_excluded():
    """The traveller registered a window; a suggestion 20 minutes beyond its end plus the catch-up
    grace is not the trip they signed up for, however well it would score."""
    got = _best(
        [_cand(140, 170, ordinal=0), _cand(60, 150, ordinal=1)],
        _original(predicted_min=200),
        deadline=NOW + timedelta(minutes=180),
    )
    assert got is not None
    assert got.departure_time == NOW + timedelta(minutes=60), "+140 is past +30 window +90 grace"


def test_a_safe_journey_is_preferred_to_a_risky_one_in_the_same_tier():
    """R6 as a preference: both arrive in time, so take the one not heading for trouble itself —
    even though the risky one departs later and would win tier 1 on its own."""
    got = _best(
        [_cand(20, 55, risky=True, ordinal=0), _cand(10, 50, ordinal=1)],
        _original(predicted_min=90),
    )
    assert got is not None
    assert got.departure_time == NOW + timedelta(minutes=10)
    assert got.at_risk is False


def test_a_risky_journey_beats_a_safe_one_that_arrives_far_too_late():
    """R6 is a preference, not a veto — and arrival-feasibility is the OUTER loop. Nested the other
    way, a safe journey arriving an hour and a half late would beat a slightly risky one that still
    gets the traveller there on time."""
    got = _best(
        [_cand(20, 55, risky=True, ordinal=0), _cand(40, 150, ordinal=1)],
        _original(predicted_min=90),
    )
    assert got is not None
    assert got.arrival_time == NOW + timedelta(minutes=55)
    assert got.at_risk is True, "and the caller is told what it is offering"


def test_the_original_winning_means_there_is_nothing_to_offer():
    """R7. Delayed, but still the first arrival on the board."""
    assert _best(
        [_cand(10, 80, ordinal=0), _cand(20, 90, ordinal=1)], _original(predicted_min=70)
    ) is None
    # And with the materiality gate out of the way — an alert-worthy original, where any way
    # through would be worth offering — R7 is what stops us recommending the traveller's own train
    # back to them. Both candidates are risky so R6 cannot hand the tier to one of them first.
    assert _best(
        [_cand(10, 80, risky=True, ordinal=0), _cand(20, 90, risky=True, ordinal=1)],
        _original(predicted_min=70, risky=True),
    ) is None


def test_a_marginal_gain_over_a_slightly_late_train_is_not_advice():
    """The materiality gate. The deadline is the original's AIMED arrival while its own arrival is
    aimed + delay, so it can never sit in the in-time tier and any earlier train beats it — without
    this gate a two-minute delay would produce "abandon your train and run for an earlier one",
    which for delay-driven rail alerts would be the systematic advice."""
    assert _best([_cand(10, 61)], _original(predicted_min=62)) is None
    got = _best([_cand(10, 61)], _original(predicted_min=100))
    assert got is not None, "40 minutes late is worth escaping; two minutes is not"
    assert got.minutes_saved == 39


def test_the_materiality_gate_is_bypassed_when_the_original_is_in_real_trouble():
    """Cancelled or over its own alert gate: any way through is worth offering, however slim."""
    cancelled = _best([_cand(10, 61)], _original(predicted_min=62, cancelled=True))
    assert cancelled is not None and cancelled.minutes_saved == 1
    alertworthy = _best([_cand(10, 61)], _original(predicted_min=62, risky=True))
    assert alertworthy is not None and alertworthy.minutes_saved == 1


def test_a_tie_is_broken_deterministically():
    """This runs again on every refresh (~5 min). An unstable winner would flip both the card's
    wording and which situation the written message is cached under — the same stability concern
    `varsel._alert_rank` documents for tied cancellations."""
    tied_original = _original(predicted_min=70, risky=True)
    tied = _cand(10, 70, ordinal=3, risky=True)
    assert _best([tied], tied_original) is None
    assert _best([tied], tied_original) is None, "twice, identically"

    # Between two non-originals tying on arrival, JP's reply order decides — and decides the same
    # way whatever order the caller happened to hand them over in.
    a, b = _cand(10, 70, ordinal=1), _cand(12, 70, ordinal=0)
    for pool in ([a, b], [b, a]):
        got = _best(pool, _original(predicted_min=90))
        assert got.departure_time == NOW + timedelta(minutes=12), "ordinal 0 wins, in either order"


def test_a_candidate_departing_exactly_now_is_not_offered():
    """`> now`, not `>=`: a departure the traveller is standing at is not a plan."""
    assert _best([_cand(0, 20)], _original(predicted_min=90)) is None
    got = _best([_cand(1, 20)], _original(predicted_min=90))
    assert got is not None and got.departure_time == NOW + timedelta(minutes=1)


def test_an_empty_candidate_list_yields_no_alternative():
    assert _best([], _original(predicted_min=90)) is None
    assert _best([], _original(predicted_min=90, cancelled=True)) is None, "and no pool at all"
    assert _best([], None) is None


def test_a_candidate_that_cannot_be_ranked_is_dropped_not_fatal():
    """Nothing downstream can rank an arrival that is not there, and a departure we cannot read may
    already have gone. One unusable candidate costs that one suggestion."""
    no_arrival = _cand(10, 40).model_copy(update={"arrival_time": None})
    no_departure = _cand(10, 40).model_copy(update={"departure_time": None})
    for broken in (no_arrival, no_departure):
        got = _best([broken, _cand(12, 45, ordinal=1)], _original(predicted_min=90))
        assert got is not None
        assert got.departure_time == NOW + timedelta(minutes=12)
        assert _best([broken], _original(predicted_min=90)) is None


def test_the_chosen_journey_carries_what_the_traveller_has_to_recognise():
    got = _best([_cand(10, 45)], _original(predicted_min=90))
    assert [(leg.mode, leg.public_code, leg.from_name, leg.to_name) for leg in got.legs] == [
        ("rail", "R11", "Oslo S", "Asker stasjon")
    ]
    assert got.duration_min == 35
    assert got.arrives_before_original is True
    assert got.at_risk is False


# ---- cache / fail-open ------------------------------------------------------------------------
def test_cache_serves_within_ttl_and_issues_one_fetch():
    cache = _KeyedTTLCache(timedelta(minutes=2), timedelta(minutes=2))
    calls = []

    def fetch():
        calls.append(1)
        return ["value"]

    now = REF
    assert cache.get("k", fetch, now) == ["value"]
    assert cache.get("k", fetch, now + timedelta(seconds=30)) == ["value"]
    assert len(calls) == 1, "a second call inside the TTL must not re-fetch"


def test_cache_failure_serves_the_previous_value_not_a_fresh_miss():
    cache = _KeyedTTLCache(timedelta(seconds=0), timedelta(minutes=2))
    now = REF
    assert cache.get("k", lambda: ["good"], now) == ["good"]

    def boom():
        raise RuntimeError("JP down")

    assert cache.get("k", boom, now + timedelta(minutes=1)) == ["good"]


def test_cache_failure_cooldown_suppresses_further_fetches():
    cache = _KeyedTTLCache(timedelta(seconds=0), timedelta(minutes=5))
    attempts = []

    def boom():
        attempts.append(1)
        raise RuntimeError("JP down")

    now = REF
    cache.get("k", boom, now)
    cache.get("k", boom, now + timedelta(minutes=1))
    assert len(attempts) == 1, "the cooldown must suppress the second attempt"


HOSTILE_STOP_REFS = [
    'NSR:StopPlace:1"} ) { id } } evil { ',  # closes the literal and opens a new selection
    "NSR:StopPlace:1\nfoo",
    "A",
    "",
    "NSR:Line:337",  # a real ref shape, but not a stop
]


def test_a_stop_ref_that_could_escape_the_graphql_literal_is_refused_before_any_fetch():
    """Both refs are spliced into the GraphQL document as string literals and both originate in
    unauthenticated input. Nothing malformed reaches either entry point today, but only because an
    unrelated code path filters it — so the guard belongs here, where the assumption is actually
    made.

    `fetch_candidates` is the only entry point now; it carried the guard from the day it was
    written, and the second copy that used to sit on the lookup it replaced went with that function.
    """
    original_trip = alternatives._fetch_trip_patterns
    attempts = []
    alternatives._fetch_trip_patterns = lambda *a, **k: attempts.append(1) or _recorded_patterns()
    alternatives._cache = _KeyedTTLCache(timedelta(minutes=2), timedelta(minutes=2))
    try:
        for ref in HOSTILE_STOP_REFS:
            for pair in ((ref, TO), (FROM, ref)):
                assert (
                    fetch_candidates(
                        pair[0], pair[1], arrive_by=False, date_time=REF, now=REF
                    )
                    == []
                ), f"{ref!r} must not reach the Journey Planner via fetch_candidates"
        assert not attempts, "the guard must refuse before the fetch, not after"
        # And the shapes that are legitimate still work.
        assert fetch_candidates(
            FROM, "NSR:Quay:11274", arrive_by=False, date_time=REF, now=REF
        ), "a legitimate ref pair must still be fetched"
    finally:
        alternatives._fetch_trip_patterns = original_trip


def test_failure_marks_do_not_accumulate_past_their_cooldown():
    """`_entries` was capped but `_failed_at` was not, and it only shed a key on a later SUCCESS for
    that same key. The key embeds a 5-minute wall-clock bucket, so it grew with time rather than
    traffic — and during a total JP outage the success path that pruned it never ran at all.
    """
    cache = _KeyedTTLCache(timedelta(seconds=0), timedelta(minutes=5))

    def boom():
        raise RuntimeError("JP down")

    now = REF
    for i in range(50):  # 50 distinct keys, as a moving departure bucket would mint
        cache.get(("A", "B", i), boom, now)
    assert len(cache._failed_at) == 50, "inside the cooldown the marks are still load-bearing"

    cache.get(("A", "B", 999), boom, now + timedelta(minutes=6))
    assert len(cache._failed_at) == 1, "expired marks cannot suppress a fetch, so they are garbage"


# ---- the GraphQL document ---------------------------------------------------------------------
def _render(arrive_by: bool) -> str:
    return _TRIP_QUERY % {
        "from_stop": CAND_FROM,
        "to_stop": CAND_TO,
        "date_time": "2026-08-06T11:00:00+02:00",
        "arrive_by": "true" if arrive_by else "false",
        "num": 10,
    }


def test_both_anchors_render_a_syntactically_valid_document():
    """`arriveBy` is the whole reason there are two queries, and the document is hand-built GraphQL
    — the failure mode is a brace slip that only shows up against the live endpoint (PR #22).
    Checked against the recorded reply's shape rather than the network.
    """
    # Every field the recorded reply came back with must actually be selected by the document.
    keys: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                keys.add(k)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(RECORDED_JP_REPLY)["data"])

    for arrive_by in (False, True):
        doc = _render(arrive_by)
        assert f"arriveBy: {'true' if arrive_by else 'false'}" in doc
        assert doc.count("{") == doc.count("}"), "unbalanced braces"
        assert "%" not in doc, "an unsubstituted placeholder would be sent verbatim"
        assert "numTripPatterns: 10" in doc
        for key in sorted(keys):
            assert key in doc, f"{key!r} is in the reply but not selected by the document"


def test_the_document_selects_every_field_the_parser_reads_at_the_right_level():
    """Per level, not over the whole string: `aimedStartTime` is selected on both the pattern and
    the leg, so a substring check over the document stays green when one of the two is dropped.
    The parser would then read null forever, the recorded fixture would keep agreeing with it
    (it still has the field), and nothing would fail until a traveller saw a blank departure time.
    """
    doc = _render(False)
    pattern_block, sep, leg_block = doc.partition("legs {")
    assert sep, "the document must have a legs selection"
    for field in ("expectedStartTime", "expectedEndTime", "aimedStartTime", "aimedEndTime",
                  "duration"):
        assert field in pattern_block, f"{field!r} is not selected on the trip pattern"
    for field in ("mode", "realtime", "expectedStartTime", "expectedEndTime", "aimedStartTime",
                  "aimedEndTime", "line", "publicCode", "serviceJourney", "datedServiceJourney",
                  "fromPlace", "toPlace", "quay", "stopPlace", "fromEstimatedCall",
                  "toEstimatedCall", "cancellation", "aimedDepartureTime", "expectedDepartureTime",
                  "aimedArrivalTime", "expectedArrivalTime"):
        assert field in leg_block, f"{field!r} is not selected on the leg"


def test_the_document_asks_for_neither_forbidden_call_list():
    """`estimatedCalls` / `intermediateEstimatedCalls` are one word away and would multiply the
    payload by ~two orders of magnitude — a 30-call Bergen train times 10 patterns, on the poller
    thread behind a 3 s timeout. What a leg needs is its two endpoints, which the estimated *call*
    fields already give.
    """
    assert "estimatedCalls" not in _TRIP_QUERY
    assert "intermediateEstimatedCalls" not in _TRIP_QUERY
    assert "fromEstimatedCall" in _TRIP_QUERY and "toEstimatedCall" in _TRIP_QUERY


def test_the_document_does_not_clamp_the_search_window():
    """Measured 2026-08-06 (Oslo S -> Rosenholm, 11:00): unset -> 7 patterns spanning 11:16-14:16,
    searchWindow 120 -> 4, searchWindow 60 -> 2. Setting one SHRINKS the pool below what the
    unclamped query already returns — reach is governed by the window and OTP2's dynamic default,
    not by the pattern count. This reversed an earlier design decision; the constant must not
    quietly come back.
    """
    assert "searchWindow" not in _TRIP_QUERY


# ---- candidate parsing (pinned against the recorded reply) --------------------------------------
CAND_FROM = "NSR:StopPlace:337"  # Oslo S
CAND_TO = "NSR:StopPlace:5576"  # Drøbak torg
# 11:00 in Oslo on the day the reply was captured — the instant the whole recorded scenario sits on.
CAND_NOW = datetime(2026, 8, 6, 9, 0, tzinfo=UTC)


def _recorded_patterns():
    return json.loads(RECORDED_JP_REPLY)["data"]["trip"]["tripPatterns"]


def test_the_recorded_reply_is_the_one_that_was_captured():
    """Byte-pinned. A field added to `_TRIP_QUERY` changes what JP returns, and this makes that
    visible in review rather than letting the parser keep being checked against a payload the
    document no longer produces."""
    assert len(RECORDED_JP_REPLY.encode("utf-8")) == RECORDED_JP_REPLY_BYTES
    assert len(_recorded_patterns()) == 4


def test_the_recorded_reply_parses_keeping_aimed_and_expected_apart():
    """Collapsing the two loses the delay — aimed is the trip a traveller recognises from the
    timetable, expected is when it will actually happen."""
    cands = parse_candidates(_recorded_patterns())
    assert len(cands) == 4
    first = cands[0]
    assert first.departure_time.isoformat() == "2026-08-06T11:14:43+02:00"
    assert first.aimed_departure_time.isoformat() == "2026-08-06T11:14:00+02:00"
    assert first.arrival_time.isoformat() == "2026-08-06T12:15:19+02:00"
    assert first.aimed_arrival_time.isoformat() == "2026-08-06T12:15:19+02:00"
    assert first.departure_time != first.aimed_departure_time, "this pattern is running 43 s late"
    assert first.duration_min == 61  # 3636 s
    assert first.cancelled is False


def test_ordinal_records_the_reply_order():
    """JP returns patterns in its own preference order; a tie-break that reads `ordinal` is
    deterministic, one that reads dict iteration is not."""
    cands = parse_candidates(_recorded_patterns())
    assert [c.ordinal for c in cands] == [0, 1, 2, 3]
    assert [c.departure_time.strftime("%H:%M") for c in cands] == [
        "11:14", "11:26", "11:56", "12:26",
    ]


def test_board_and_alight_stop_places_come_from_the_transit_legs():
    """Not from the pattern's endpoints: patterns 1-3 open with a foot leg from Oslo S to a bus
    stop, and the stop a traveller actually boards at is the one the prediction cache can score.
    """
    cands = parse_candidates(_recorded_patterns())
    # Pattern 0: rail from Oslo S, transfer at Ski, bus, then walk to Drøbak torg.
    assert cands[0].board_stop_place == "NSR:StopPlace:337"
    assert cands[0].alight_stop_place == "NSR:StopPlace:5553"  # Seiersten skole, then a walk
    # Pattern 1: walk out of Oslo S first, so boarding is NOT the pattern's origin.
    assert cands[1].board_stop_place == "NSR:StopPlace:3983"
    assert cands[1].alight_stop_place == "NSR:StopPlace:5576"
    assert cands[1].legs[0].mode == "foot", "the first leg really is a walk"


def test_foot_legs_are_kept_in_legs_but_excluded_from_transit_refs():
    """A walk is part of the itinerary a traveller follows and must survive into `legs`; it has no
    service journey, so nothing can score it and it has no place in `transit_refs`."""
    cands = parse_candidates(_recorded_patterns())
    first = cands[0]
    assert [leg.mode for leg in first.legs] == ["rail", "foot", "bus", "foot"]
    assert len(first.transit_refs) == 4, "two transit legs x two ref forms; the two walks add none"
    assert first.lines == ["VYG:Line:RE20", "RUT:Line:510"]
    assert [leg.public_code for leg in first.legs] == ["RE20", None, "510", None]


def test_transit_refs_carry_both_halves_and_both_ref_forms():
    """The prediction cache is keyed on (operator, jkey). Bus ServiceJourney refs are not
    date-scoped, so one jkey can name two operators' journeys and keying on the jkey alone merges
    them. And JP gives a leg BOTH a DatedServiceJourney and a ServiceJourney id — rail jkeys are
    the dated form, bus jkeys are the plain one — so preferring either form would silently miss a
    whole family.
    """
    first = parse_candidates(_recorded_patterns())[0]
    assert first.transit_refs == [
        ("VYG", "DatedServiceJourney:113_OSL-HLD_26-08-06"),
        ("VYG", "ServiceJourney:113_552907-R"),
        ("RUT", "DatedServiceJourney:djj-6de174ed300e8d2cecb4b8d7f78a329e"),
        ("RUT", "ServiceJourney:fe362750909c4c05aec47f2891ee3a5d"),
    ]
    assert all(op is not None for op, _ in first.transit_refs)


def test_split_codespace_returns_both_halves():
    assert split_codespace("VYG:DatedServiceJourney:65_OSL-BRG_26-08-06") == (
        "VYG", "DatedServiceJourney:65_OSL-BRG_26-08-06",
    )
    assert split_codespace("RUT:ServiceJourney:fe362750909c4c05aec47f2891ee3a5d") == (
        "RUT", "ServiceJourney:fe362750909c4c05aec47f2891ee3a5d",
    )
    # A ref that is ALREADY a bare jkey has no codespace to read — and must not yield an operator
    # of "DatedServiceJourney", which a naive split on the first colon would produce.
    assert split_codespace("DatedServiceJourney:65_X") == (None, "DatedServiceJourney:65_X")
    assert split_codespace("ServiceJourney:sj-1") == (None, "ServiceJourney:sj-1")
    assert split_codespace(None) is None
    assert split_codespace("") is None


def _with_cancellation(which, value=True):
    """The recorded pattern 0, with one estimated call's `cancellation` flipped."""
    pattern = json.loads(RECORDED_JP_REPLY)["data"]["trip"]["tripPatterns"][0]
    for leg in pattern["legs"]:
        if leg.get(which):
            leg[which]["cancellation"] = value
            break
    return pattern


def test_a_cancelled_estimated_call_on_either_end_marks_the_candidate():
    """Belt-and-braces (JP's default already drops cancelled departures from the search — verified
    by A/B curl 2026-08-06), but if the flag ever surfaces it must not be ignored."""
    assert parse_candidates([_with_cancellation("fromEstimatedCall")])[0].cancelled is True
    assert parse_candidates([_with_cancellation("toEstimatedCall")])[0].cancelled is True
    assert parse_candidates(_recorded_patterns())[0].cancelled is False


def test_a_leg_without_a_quay_yields_none_stop_places_rather_than_raising():
    """A leg starting at a street address or a coordinate has no quay at all, and JP nests the ref
    two levels down — every level is a null waiting to happen."""
    pattern = json.loads(RECORDED_JP_REPLY)["data"]["trip"]["tripPatterns"][0]
    for leg in pattern["legs"]:
        leg["fromPlace"] = {"name": "Somewhere"}  # no quay key at all
        leg["toPlace"] = {"name": "Elsewhere", "quay": None}
    got = parse_candidates([pattern])
    assert len(got) == 1, "a missing quay is a null field, not a dropped candidate"
    assert got[0].board_stop_place is None
    assert got[0].alight_stop_place is None
    assert got[0].transit_refs, "the service-journey refs are unaffected"
    # And the deeper null: quay present, stopPlace absent.
    pattern2 = json.loads(RECORDED_JP_REPLY)["data"]["trip"]["tripPatterns"][0]
    for leg in pattern2["legs"]:
        leg["fromPlace"] = {"name": "X", "quay": {"stopPlace": None}}
    assert parse_candidates([pattern2])[0].board_stop_place is None


def test_a_malformed_pattern_is_skipped_not_fatal():
    """One bad pattern must cost that one suggestion. The 2026-07-29 incident is what happens
    otherwise: a raise here went through main._impact_for_trip into varsel.check_and_notify, where
    the per-journey guard turned it into every alert for that journey being dropped for the cycle.
    """
    good = _recorded_patterns()[0]
    naive = {  # JP always sends an offset; a naive timestamp cannot be compared to anything
        "expectedStartTime": "2026-08-06T11:00:00",
        "expectedEndTime": "2026-08-06T11:30:00",
        "duration": 1800,
        "legs": [],
    }
    aware_end = "2026-08-06T11:30:00+02:00"
    bad = [
        None,
        "not a pattern",
        5,
        {},
        naive,
        {**naive, "expectedEndTime": aware_end, "legs": ["nope", 5, None]},
        {**naive, "expectedEndTime": aware_end, "legs": 5},
        {**naive, "expectedEndTime": aware_end, "legs": None},
        {**naive, "expectedEndTime": aware_end, "duration": "1800"},
        {"expectedStartTime": naive["expectedStartTime"], "expectedEndTime": aware_end},  # no
        # duration, no legs at all
        # A node where JP puts an object. `"oops".get(...)` is an AttributeError, and it used to
        # cost the whole pattern rather than the one field.
        {**naive, "expectedEndTime": aware_end,
         "legs": [{"mode": "rail", "fromPlace": "oops", "line": "oops", "toPlace": 5,
                   "fromEstimatedCall": "oops", "serviceJourney": "oops"}]},
    ]
    for pattern in bad:
        got = parse_candidates([pattern, good])  # must not raise
        assert got[-1].ordinal == 1, "the good pattern keeps its position in the reply"
    survivors = parse_candidates(bad)
    # Only the five with no comparable arrival are dropped; a broken FIELD degrades to null.
    assert len(survivors) == 6, [c.ordinal for c in survivors]
    assert survivors[-1].legs[0].from_name is None
    assert survivors[-1].transit_refs == []
    assert survivors[-1].duration_min is None or survivors[-1].duration_min == 30


# ---- fetch_candidates -------------------------------------------------------------------------
def _install_fetch(fake):
    """Swap the network call and reset the cache; returns the original for the finally block."""
    original = alternatives._fetch_trip_patterns
    alternatives._fetch_trip_patterns = fake
    alternatives._cache = _KeyedTTLCache(timedelta(minutes=2), timedelta(minutes=2))
    return original


def test_fetch_candidates_passes_the_callers_stops_through_unchanged():
    """Canonicalisation belongs to the caller (varsel already collapses stop refs to one row per
    physical place). This layer must not second-guess it — quietly rewriting a ref here would move
    the 2026-08-03 silent no-match down a layer instead of fixing it.
    """
    seen = []

    def fake(from_stop, to_stop, date_time, *, arrive_by):
        seen.append((from_stop, to_stop, date_time, arrive_by))
        return _recorded_patterns()

    original = _install_fetch(fake)
    try:
        when = CAND_NOW + timedelta(minutes=30)
        got = fetch_candidates(CAND_FROM, CAND_TO, arrive_by=False, date_time=when, now=CAND_NOW)
        assert len(got) == 4
        assert seen == [(CAND_FROM, CAND_TO, when, False)]
    finally:
        alternatives._fetch_trip_patterns = original


def test_both_anchors_are_fetched_separately_and_cached_apart():
    """A depart-after search cannot contain a journey that leaves earlier and still gets there in
    time, so the two anchors are different questions — and must not share a cache entry."""
    seen = []

    def fake(from_stop, to_stop, date_time, *, arrive_by):
        seen.append(arrive_by)
        return _recorded_patterns()

    original = _install_fetch(fake)
    try:
        when = CAND_NOW + timedelta(minutes=30)
        fetch_candidates(CAND_FROM, CAND_TO, arrive_by=False, date_time=when, now=CAND_NOW)
        fetch_candidates(CAND_FROM, CAND_TO, arrive_by=True, date_time=when, now=CAND_NOW)
        assert seen == [False, True], "the arrive-by answer must not be served from the other key"
        fetch_candidates(CAND_FROM, CAND_TO, arrive_by=False, date_time=when, now=CAND_NOW)
        assert seen == [False, True], "and a repeat inside the TTL must not re-fetch"
    finally:
        alternatives._fetch_trip_patterns = original


def test_a_lookup_further_ahead_than_the_horizon_is_skipped_entirely():
    """A registration for tomorrow morning is re-examined on every poller cycle from the moment it
    is created, and this is the most expensive call on that path. A lookup that far ahead cannot
    contain a train in tomorrow's window, so it buys nothing at any price.
    """
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return _recorded_patterns()

    original = _install_fetch(fake)
    try:
        far = fetch_candidates(
            CAND_FROM, CAND_TO, arrive_by=False,
            date_time=CAND_NOW + timedelta(hours=7), now=CAND_NOW,
        )
        assert far == []
        assert not calls, "7 hours out must not reach the Journey Planner at all"

        near = fetch_candidates(
            CAND_FROM, CAND_TO, arrive_by=False,
            date_time=CAND_NOW + timedelta(hours=5), now=CAND_NOW,
        )
        assert len(near) == 4
        assert len(calls) == 1, "5 hours out is inside the horizon"
    finally:
        alternatives._fetch_trip_patterns = original


def test_fetch_candidates_takes_the_callers_now_and_never_samples_its_own():
    """`impact.for_trip` samples `now` once precisely so `as_of` and the observation cut cannot
    straddle two instants; this lookup has to sit on that same instant. A `date_time` 7 hours after
    the CALLER's `now` is out of horizon even when it is minutes away from the wall clock.
    """
    calls = []
    original = _install_fetch(lambda *a, **k: calls.append(1) or _recorded_patterns())
    try:
        stale_now = datetime(2020, 1, 1, tzinfo=UTC)
        assert fetch_candidates(
            CAND_FROM, CAND_TO, arrive_by=False,
            date_time=stale_now + timedelta(hours=7), now=stale_now,
        ) == []
        assert not calls
    finally:
        alternatives._fetch_trip_patterns = original


def test_fetch_candidates_fails_open_on_every_failure_path():
    """The whole point: a JP problem must never be why a push does not go out."""
    when = CAND_NOW + timedelta(minutes=30)

    def raising(*a, **k):
        raise RuntimeError("JP down")

    def graphql_errors(*a, **k):
        # _post_json turns an `errors` envelope into a raise so it lands on the cache's failure
        # path rather than being parsed into "no alternative" and cached as a fresh success.
        raise RuntimeError("JP error response: [{'message': 'Validation error'}]")

    cases = {
        "the fetch raises": raising,
        "a GraphQL errors envelope": graphql_errors,
        "an empty tripPatterns": lambda *a, **k: [],
        "a null tripPatterns": lambda *a, **k: None,
        "patterns are not a list": lambda *a, **k: {"tripPatterns": "nope"},
    }
    for label, fake in cases.items():
        original = _install_fetch(fake)
        try:
            got = fetch_candidates(
                CAND_FROM, CAND_TO, arrive_by=False, date_time=when, now=CAND_NOW
            )
            assert got == [], f"{label} must yield no candidates"
        finally:
            alternatives._fetch_trip_patterns = original


def test_fetch_candidates_contains_a_parsing_failure():
    """Fail open covers parsing, not just fetching — and the guard has to sit around the parse of
    the whole reply, not only around each pattern."""
    class Exploding(list):
        def __iter__(self):
            raise TypeError("boom")

    original = _install_fetch(lambda *a, **k: Exploding([1]))
    try:
        assert fetch_candidates(
            CAND_FROM, CAND_TO, arrive_by=False,
            date_time=CAND_NOW + timedelta(minutes=30), now=CAND_NOW,
        ) == []
    finally:
        alternatives._fetch_trip_patterns = original


def test_candidates_carry_the_caller_set_flags_defaulted_off():
    """`is_original` and `risky` are decisions this layer does not make — it reports what JP said.
    They must exist for the selector to set, and must start false."""
    cand = parse_candidates(_recorded_patterns())[0]
    assert cand.is_original is False
    assert cand.risky is False


def test_alternative_prediction_defaults_to_not_scored_with_null_probabilities():
    """A null probability means the models never scored this call — no live situation touches the
    journey — and never "zero risk". `scored` exists so a reader never has to infer that from the
    absence of a number.
    """
    pred = alternatives.AlternativePrediction()
    assert pred.scored is False
    assert pred.cancel_probability is None and pred.impact_probability is None
    assert pred.cancelled is False and pred.delay_min is None
    # And it lives HERE, not in impact: impact imports alternatives, and AlternativeJourney (this
    # module) will carry a field of this type. The reverse import is an ImportError at startup,
    # which `from __future__ import annotations` does not defer — pydantic resolves field
    # annotations when the class is built.
    assert alternatives.AlternativePrediction.__module__ == "alternatives"


# ---- Recorded Journey Planner reply -----------------------------------------------------------
# Captured verbatim with curl 2026-08-06 against
# https://api.entur.io/journey-planner/v3/graphql (ET-Client-Name: entur-kixavvik), using the exact
# `alternatives._TRIP_QUERY` document: Oslo S (NSR:StopPlace:337) -> Drøbak torg
# (NSR:StopPlace:5576), dateTime 2026-08-06T11:00:00+02:00, arriveBy false. Truncated to the first
# 4 of 7 tripPatterns; nothing inside a pattern was edited.
#
# This corridor was chosen over a plain Oslo S -> Drammen rail reply because it exercises the
# awkward shapes in one payload: pattern 0 is rail + foot + bus + foot (a transfer, two operators),
# patterns 1-3 open with a foot leg whose estimated calls are null, and every bus leg carries BOTH
# a DatedServiceJourney and a ServiceJourney id -- the case `transit_refs` exists for.
#
# A hand-written fixture would have agreed with whatever the parser happened to do. This does not.
RECORDED_JP_REPLY = (
    "{\"data\":{\"trip\":{\"tripPatterns\":[{\"expectedStartTime\":\"2026-08-06T11:14:43+02:00\",\"expec"
    "tedEndTime\":\"2026-08-06T12:15:19+02:00\",\"aimedStartTime\":\"2026-08-06T11:14:00+02:00\",\"ai"
    "medEndTime\":\"2026-08-06T12:15:19+02:00\",\"duration\":3636,\"legs\":[{\"mode\":\"rail\",\"realtime"
    "\":true,\"expectedStartTime\":\"2026-08-06T11:14:43+02:00\",\"expectedEndTime\":\"2026-08-06T11:"
    "25:26+02:00\",\"aimedStartTime\":\"2026-08-06T11:14:00+02:00\",\"aimedEndTime\":\"2026-08-06T11:"
    "25:00+02:00\",\"line\":{\"id\":\"VYG:Line:RE20\",\"publicCode\":\"RE20\"},\"serviceJourney\":{\"id\":\"V"
    "YG:ServiceJourney:113_552907-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServiceJourney:11"
    "3_OSL-HLD_26-08-06\"},\"fromPlace\":{\"name\":\"Oslo S\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPla"
    "ce:337\"}}},\"toPlace\":{\"name\":\"Ski stasjon\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:127\""
    "}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":true,\"aimedDepartureTime\":\"2026"
    "-08-06T11:14:00+02:00\",\"expectedDepartureTime\":\"2026-08-06T11:14:43+02:00\"},\"toEstimated"
    "Call\":{\"cancellation\":false,\"aimedArrivalTime\":\"2026-08-06T11:25:00+02:00\",\"expectedArri"
    "valTime\":\"2026-08-06T11:25:26+02:00\"}},{\"mode\":\"foot\",\"realtime\":false,\"expectedStartTim"
    "e\":\"2026-08-06T11:25:26+02:00\",\"expectedEndTime\":\"2026-08-06T11:30:07+02:00\",\"aimedStart"
    "Time\":\"2026-08-06T11:25:26+02:00\",\"aimedEndTime\":\"2026-08-06T11:30:07+02:00\",\"line\":null"
    ",\"serviceJourney\":null,\"datedServiceJourney\":null,\"fromPlace\":{\"name\":\"Ski stasjon\",\"qua"
    "y\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:127\"}}},\"toPlace\":{\"name\":\"Ski stasjon Jernbaneveie"
    "n\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:4992\"}}},\"fromEstimatedCall\":null,\"toEstimat"
    "edCall\":null},{\"mode\":\"bus\",\"realtime\":true,\"expectedStartTime\":\"2026-08-06T11:32:18+02:"
    "00\",\"expectedEndTime\":\"2026-08-06T12:03:36+02:00\",\"aimedStartTime\":\"2026-08-06T11:32:00+"
    "02:00\",\"aimedEndTime\":\"2026-08-06T12:03:00+02:00\",\"line\":{\"id\":\"RUT:Line:510\",\"publicCod"
    "e\":\"510\"},\"serviceJourney\":{\"id\":\"RUT:ServiceJourney:fe362750909c4c05aec47f2891ee3a5d\"},"
    "\"datedServiceJourney\":{\"id\":\"RUT:DatedServiceJourney:djj-6de174ed300e8d2cecb4b8d7f78a329"
    "e\"},\"fromPlace\":{\"name\":\"Ski stasjon Jernbaneveien\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopP"
    "lace:4992\"}}},\"toPlace\":{\"name\":\"Seiersten skole\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPla"
    "ce:5553\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":true,\"aimedDepartureTim"
    "e\":\"2026-08-06T11:32:00+02:00\",\"expectedDepartureTime\":\"2026-08-06T11:32:18+02:00\"},\"toE"
    "stimatedCall\":{\"cancellation\":false,\"aimedArrivalTime\":\"2026-08-06T12:03:00+02:00\",\"expe"
    "ctedArrivalTime\":\"2026-08-06T12:03:36+02:00\"}},{\"mode\":\"foot\",\"realtime\":false,\"expected"
    "StartTime\":\"2026-08-06T12:03:36+02:00\",\"expectedEndTime\":\"2026-08-06T12:15:19+02:00\",\"ai"
    "medStartTime\":\"2026-08-06T12:03:36+02:00\",\"aimedEndTime\":\"2026-08-06T12:15:19+02:00\",\"li"
    "ne\":null,\"serviceJourney\":null,\"datedServiceJourney\":null,\"fromPlace\":{\"name\":\"Seiersten"
    " skole\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5553\"}}},\"toPlace\":{\"name\":\"Drøbak torg"
    "\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5576\"}}},\"fromEstimatedCall\":null,\"toEstimate"
    "dCall\":null}]},{\"expectedStartTime\":\"2026-08-06T11:26:12+02:00\",\"expectedEndTime\":\"2026-"
    "08-06T12:19:53+02:00\",\"aimedStartTime\":\"2026-08-06T11:26:12+02:00\",\"aimedEndTime\":\"2026-"
    "08-06T12:16:00+02:00\",\"duration\":3221,\"legs\":[{\"mode\":\"foot\",\"realtime\":false,\"expectedS"
    "tartTime\":\"2026-08-06T11:26:12+02:00\",\"expectedEndTime\":\"2026-08-06T11:32:00+02:00\",\"aim"
    "edStartTime\":\"2026-08-06T11:26:12+02:00\",\"aimedEndTime\":\"2026-08-06T11:32:00+02:00\",\"lin"
    "e\":null,\"serviceJourney\":null,\"datedServiceJourney\":null,\"fromPlace\":{\"name\":\"Oslo S\",\"q"
    "uay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}}},\"toPlace\":{\"name\":\"Bjørvika\",\"quay\":{\"sto"
    "pPlace\":{\"id\":\"NSR:StopPlace:3983\"}}},\"fromEstimatedCall\":null,\"toEstimatedCall\":null},{"
    "\"mode\":\"bus\",\"realtime\":true,\"expectedStartTime\":\"2026-08-06T11:32:00+02:00\",\"expectedEn"
    "dTime\":\"2026-08-06T12:19:53+02:00\",\"aimedStartTime\":\"2026-08-06T11:32:00+02:00\",\"aimedEn"
    "dTime\":\"2026-08-06T12:16:00+02:00\",\"line\":{\"id\":\"RUT:Line:500\",\"publicCode\":\"500\"},\"serv"
    "iceJourney\":{\"id\":\"RUT:ServiceJourney:08730a7b534af6c55706fa8a2dee8139\"},\"datedServiceJo"
    "urney\":{\"id\":\"RUT:DatedServiceJourney:djj-1afc909de99d618a229603d1f86a03fd\"},\"fromPlace\""
    ":{\"name\":\"Bjørvika\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:3983\"}}},\"toPlace\":{\"name\":"
    "\"Drøbak torg\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5576\"}}},\"fromEstimatedCall\":{\"ca"
    "ncellation\":false,\"realtime\":true,\"aimedDepartureTime\":\"2026-08-06T11:32:00+02:00\",\"expe"
    "ctedDepartureTime\":\"2026-08-06T11:32:00+02:00\"},\"toEstimatedCall\":{\"cancellation\":false,"
    "\"aimedArrivalTime\":\"2026-08-06T12:16:00+02:00\",\"expectedArrivalTime\":\"2026-08-06T12:19:5"
    "3+02:00\"}}]},{\"expectedStartTime\":\"2026-08-06T11:56:12+02:00\",\"expectedEndTime\":\"2026-08"
    "-06T12:46:00+02:00\",\"aimedStartTime\":\"2026-08-06T11:56:12+02:00\",\"aimedEndTime\":\"2026-08"
    "-06T12:46:00+02:00\",\"duration\":2988,\"legs\":[{\"mode\":\"foot\",\"realtime\":false,\"expectedSta"
    "rtTime\":\"2026-08-06T11:56:12+02:00\",\"expectedEndTime\":\"2026-08-06T12:02:00+02:00\",\"aimed"
    "StartTime\":\"2026-08-06T11:56:12+02:00\",\"aimedEndTime\":\"2026-08-06T12:02:00+02:00\",\"line\""
    ":null,\"serviceJourney\":null,\"datedServiceJourney\":null,\"fromPlace\":{\"name\":\"Oslo S\",\"qua"
    "y\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}}},\"toPlace\":{\"name\":\"Bjørvika\",\"quay\":{\"stopP"
    "lace\":{\"id\":\"NSR:StopPlace:3983\"}}},\"fromEstimatedCall\":null,\"toEstimatedCall\":null},{\"m"
    "ode\":\"bus\",\"realtime\":true,\"expectedStartTime\":\"2026-08-06T12:02:00+02:00\",\"expectedEndT"
    "ime\":\"2026-08-06T12:46:00+02:00\",\"aimedStartTime\":\"2026-08-06T12:02:00+02:00\",\"aimedEndT"
    "ime\":\"2026-08-06T12:46:00+02:00\",\"line\":{\"id\":\"RUT:Line:500\",\"publicCode\":\"500\"},\"servic"
    "eJourney\":{\"id\":\"RUT:ServiceJourney:b1f1e52129f27edb4dcebfc49c8839eb\"},\"datedServiceJour"
    "ney\":{\"id\":\"RUT:DatedServiceJourney:djj-9e434aeee5ee7ae32047a0a2bff2aee0\"},\"fromPlace\":{"
    "\"name\":\"Bjørvika\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:3983\"}}},\"toPlace\":{\"name\":\"D"
    "røbak torg\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5576\"}}},\"fromEstimatedCall\":{\"canc"
    "ellation\":false,\"realtime\":true,\"aimedDepartureTime\":\"2026-08-06T12:02:00+02:00\",\"expect"
    "edDepartureTime\":\"2026-08-06T12:02:00+02:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"a"
    "imedArrivalTime\":\"2026-08-06T12:46:00+02:00\",\"expectedArrivalTime\":\"2026-08-06T12:46:00+"
    "02:00\"}}]},{\"expectedStartTime\":\"2026-08-06T12:26:12+02:00\",\"expectedEndTime\":\"2026-08-0"
    "6T13:16:00+02:00\",\"aimedStartTime\":\"2026-08-06T12:26:12+02:00\",\"aimedEndTime\":\"2026-08-0"
    "6T13:16:00+02:00\",\"duration\":2988,\"legs\":[{\"mode\":\"foot\",\"realtime\":false,\"expectedStart"
    "Time\":\"2026-08-06T12:26:12+02:00\",\"expectedEndTime\":\"2026-08-06T12:32:00+02:00\",\"aimedSt"
    "artTime\":\"2026-08-06T12:26:12+02:00\",\"aimedEndTime\":\"2026-08-06T12:32:00+02:00\",\"line\":n"
    "ull,\"serviceJourney\":null,\"datedServiceJourney\":null,\"fromPlace\":{\"name\":\"Oslo S\",\"quay\""
    ":{\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}}},\"toPlace\":{\"name\":\"Bjørvika\",\"quay\":{\"stopPla"
    "ce\":{\"id\":\"NSR:StopPlace:3983\"}}},\"fromEstimatedCall\":null,\"toEstimatedCall\":null},{\"mod"
    "e\":\"bus\",\"realtime\":false,\"expectedStartTime\":\"2026-08-06T12:32:00+02:00\",\"expectedEndTi"
    "me\":\"2026-08-06T13:16:00+02:00\",\"aimedStartTime\":\"2026-08-06T12:32:00+02:00\",\"aimedEndTi"
    "me\":\"2026-08-06T13:16:00+02:00\",\"line\":{\"id\":\"RUT:Line:500\",\"publicCode\":\"500\"},\"service"
    "Journey\":{\"id\":\"RUT:ServiceJourney:4f700bd5bbb7e43032a4d50f986fba7d\"},\"datedServiceJourn"
    "ey\":{\"id\":\"RUT:DatedServiceJourney:djj-2d7c008e012b3d7618a9b5ad13488a32\"},\"fromPlace\":{\""
    "name\":\"Bjørvika\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:3983\"}}},\"toPlace\":{\"name\":\"Dr"
    "øbak torg\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5576\"}}},\"fromEstimatedCall\":{\"cance"
    "llation\":false,\"realtime\":false,\"aimedDepartureTime\":\"2026-08-06T12:32:00+02:00\",\"expect"
    "edDepartureTime\":\"2026-08-06T12:32:00+02:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"a"
    "imedArrivalTime\":\"2026-08-06T13:16:00+02:00\",\"expectedArrivalTime\":\"2026-08-06T13:16:00+"
    "02:00\"}}]}]}}}"
)
# Pinned so a field added to `_TRIP_QUERY` cannot land without this payload being re-captured --
# the reply is the only check that the document and the parser still describe the same thing.
RECORDED_JP_REPLY_BYTES = 7768


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} alternative checks passed.")


if __name__ == "__main__":
    _run()
