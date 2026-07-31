"""Checks for the Journey Planner alternative lookup — recorded payloads, no network.

The JP document itself was verified live with curl 2026-07-29 (Oslo S -> Drammen / Stavanger);
these tests pin the selection rules and the fail-open behaviour around it.

Run: uv run --with pydantic python test_alternatives.py  (or under pytest)
"""

from datetime import UTC, datetime, timedelta

import alternatives
from alternatives import _KeyedTTLCache, find_faster, select_faster

REF = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)  # the disrupted train's predicted arrival
FROM = "NSR:StopPlace:337"  # Oslo S — find_faster requires real NSR ref shapes (see the guard test)
TO = "NSR:StopPlace:418"  # Drammen


def _pattern(arrive_min, *, dep_min=0, line="VYG:Line:R11", dsj="VYG:DatedServiceJourney:X_26-07-29",
             mode="rail"):
    """One tripPattern arriving `arrive_min` minutes relative to REF (negative = earlier)."""
    start = REF + timedelta(minutes=dep_min - 40)
    end = REF + timedelta(minutes=arrive_min)
    return {
        "expectedStartTime": start.isoformat(),
        "expectedEndTime": end.isoformat(),
        "duration": (end - start).total_seconds(),
        "legs": [
            {
                "mode": mode,
                "expectedStartTime": start.isoformat(),
                "expectedEndTime": end.isoformat(),
                "line": {"id": line, "publicCode": "R11"},
                "serviceJourney": {"id": "VYG:ServiceJourney:sj-1"},
                "datedServiceJourney": {"id": dsj},
                "fromPlace": {"name": "Oslo S"},
                "toPlace": {"name": "Drammen"},
            }
        ],
    }


def _select(patterns, jkeys=frozenset(), lines=frozenset()):
    return select_faster(
        patterns, reference_arrival=REF, excluded_jkeys=set(jkeys), excluded_lines=set(lines)
    )


# ---- selection --------------------------------------------------------------------------------
def test_picks_the_earliest_arriving_pattern_that_beats_the_reference():
    got = _select([_pattern(-5), _pattern(-25), _pattern(-12)])
    assert got is not None
    assert got.arrival_time == REF - timedelta(minutes=25)
    assert got.minutes_saved == 25


def test_no_pattern_faster_than_the_reference_yields_none():
    """A journey slower than what the passenger already has is not an alternative."""
    assert _select([_pattern(5), _pattern(20)]) is None


def test_a_marginal_saving_is_not_offered():
    """Shaving a minute off is noise; quoting it as advice costs more trust than it buys."""
    assert _select([_pattern(-1)]) is None
    assert _select([_pattern(-2)]) is None
    assert _select([_pattern(-3)]) is not None


def test_pattern_riding_the_affected_journey_is_excluded():
    affected = "DatedServiceJourney:81613_DRM-DAL_26-07-29"
    patterns = [_pattern(-30, dsj=f"VYG:{affected}"), _pattern(-10)]
    got = _select(patterns, jkeys={affected})
    assert got is not None
    assert got.minutes_saved == 10, "the faster pattern rides the disruption and must be dropped"


def test_pattern_on_the_affected_line_is_excluded():
    patterns = [_pattern(-30, line="VYG:Line:R11"), _pattern(-10, line="VYG:Line:R10")]
    got = _select(patterns, lines={"VYG:Line:R11"})
    assert got is not None
    assert got.legs[0].line == "VYG:Line:R10"


def test_bus_service_journey_refs_also_match_the_exclusion():
    """Bus jkeys are plain ServiceJourney refs, so both JP ref fields must be checked."""
    pattern = _pattern(-30)
    pattern["legs"][0]["datedServiceJourney"] = None
    assert _select([pattern], jkeys={"ServiceJourney:sj-1"}) is None


def test_empty_and_malformed_patterns_are_survivable():
    assert _select([]) is None
    assert _select([{}]) is None
    assert _select([{"expectedEndTime": "not-a-timestamp", "legs": []}]) is None
    assert _select([None]) is None


def test_malformed_patterns_do_not_raise():
    """Regression (review 2026-07-29): each of these raised TypeError out of select_faster, through
    find_faster (which only guarded the FETCH), through main._impact_for_trip, and into
    varsel.check_and_notify — where the per-journey guard turned it into every alert for that journey
    being silently dropped for the cycle. One bad JP pattern must cost the alternative, nothing more.
    """
    naive = {  # JP always sends an offset; a naive timestamp cannot be compared to a UTC reference
        "expectedStartTime": "2026-07-29T08:00:00",
        "expectedEndTime": "2026-07-29T08:30:00",
        "duration": 1800,
        "legs": [],
    }
    aware_end = (REF - timedelta(minutes=20)).isoformat()
    cases = {
        "naive timestamps": naive,
        "non-numeric duration": {**naive, "expectedEndTime": aware_end, "duration": "1800"},
        "legs not a list": {**naive, "expectedEndTime": aware_end, "legs": 5},
        "leg not a dict": {**naive, "expectedEndTime": aware_end, "legs": ["nope"]},
        "null legs": {**naive, "expectedEndTime": aware_end, "legs": None},
    }
    for label, pattern in cases.items():
        got = _select([pattern])  # must not raise
        if label == "naive timestamps":
            assert got is None, "an uncomparable timestamp is not an alternative"


def test_find_faster_contains_a_parsing_failure():
    """The fail-open contract covers parsing, not just fetching."""
    original = alternatives._fetch_patterns
    alternatives._fetch_patterns = lambda *a, **k: [
        {"expectedStartTime": "2026-07-29T08:00:00", "expectedEndTime": "2026-07-29T08:30:00",
         "duration": 1800, "legs": []}
    ]
    alternatives._cache = _KeyedTTLCache(timedelta(minutes=2), timedelta(minutes=2))
    try:
        assert find_faster(FROM, TO, reference_arrival=REF,
                           depart_after=REF - timedelta(minutes=40),
                           excluded_jkeys=set(), excluded_lines=set(), now=REF) is None
    finally:
        alternatives._fetch_patterns = original


def test_legs_are_summarised_in_order():
    got = _select([_pattern(-20)])
    assert [(leg.mode, leg.public_code, leg.from_name, leg.to_name) for leg in got.legs] == [
        ("rail", "R11", "Oslo S", "Drammen")
    ]
    assert got.duration_min == 20


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


def test_find_faster_returns_none_when_the_fetch_raises(monkeypatch=None):
    """The whole point: a JP outage must never be why a push does not go out."""
    original = alternatives._fetch_patterns
    alternatives._fetch_patterns = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    alternatives._cache = _KeyedTTLCache(timedelta(minutes=2), timedelta(minutes=2))
    try:
        got = find_faster(
            "NSR:StopPlace:337",
            "NSR:StopPlace:418",
            reference_arrival=REF,
            depart_after=REF - timedelta(minutes=40),
            excluded_jkeys=set(),
            excluded_lines=set(),
            now=REF,
        )
        assert got is None
    finally:
        alternatives._fetch_patterns = original


def test_find_faster_returns_none_on_empty_patterns():
    original = alternatives._fetch_patterns
    alternatives._fetch_patterns = lambda *a, **k: []
    alternatives._cache = _KeyedTTLCache(timedelta(minutes=2), timedelta(minutes=2))
    try:
        assert (
            find_faster(
                FROM,
                TO,
                reference_arrival=REF,
                depart_after=REF - timedelta(minutes=40),
                excluded_jkeys=set(),
                excluded_lines=set(),
                now=REF,
            )
            is None
        )
    finally:
        alternatives._fetch_patterns = original


def test_a_stop_ref_that_could_escape_the_graphql_literal_is_refused_before_any_fetch():
    """Both refs are spliced into the GraphQL document as string literals and both originate in
    unauthenticated input. Nothing malformed reaches find_faster today, but only because an unrelated
    code path filters it — so the guard belongs here, where the assumption is actually made.
    """
    original = alternatives._fetch_patterns
    attempts = []
    alternatives._fetch_patterns = lambda *a, **k: attempts.append(1) or [_pattern(-20)]
    alternatives._cache = _KeyedTTLCache(timedelta(minutes=2), timedelta(minutes=2))
    hostile = [
        'NSR:StopPlace:1"} ) { id } } evil { ',  # closes the literal and opens a new selection
        "NSR:StopPlace:1\nfoo",
        "A",
        "",
        "NSR:Line:337",  # a real ref shape, but not a stop
    ]
    try:
        for ref in hostile:
            for pair in ((ref, TO), (FROM, ref)):
                assert (
                    find_faster(
                        pair[0], pair[1], reference_arrival=REF,
                        depart_after=REF - timedelta(minutes=40),
                        excluded_jkeys=set(), excluded_lines=set(), now=REF,
                    )
                    is None
                ), f"{ref!r} must not reach the Journey Planner"
        assert not attempts, "the guard must refuse before the fetch, not after"
        # And the shapes that are legitimate still work.
        assert find_faster(
            FROM, "NSR:Quay:11274", reference_arrival=REF,
            depart_after=REF - timedelta(minutes=40),
            excluded_jkeys=set(), excluded_lines=set(), now=REF,
        ) is not None
    finally:
        alternatives._fetch_patterns = original


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


def test_find_faster_buckets_the_departure_time_for_cache_reuse():
    original = alternatives._fetch_patterns
    calls = []

    def fake(from_stop, to_stop, depart_after):
        calls.append(depart_after)
        return [_pattern(-20)]

    alternatives._fetch_patterns = fake
    alternatives._cache = _KeyedTTLCache(timedelta(minutes=2), timedelta(minutes=2))
    try:
        base = REF - timedelta(minutes=40)
        for offset in (0, 1, 2):
            find_faster(
                FROM,
                TO,
                reference_arrival=REF,
                depart_after=base + timedelta(minutes=offset),
                excluded_jkeys=set(),
                excluded_lines=set(),
                now=REF,
            )
        assert len(calls) == 1, "departures inside one 5-minute bucket must share a JP answer"
    finally:
        alternatives._fetch_patterns = original


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
