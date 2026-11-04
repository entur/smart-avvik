"""Checks for the Journey Planner trip-plan fetch — recorded payloads, no network.

The plan document's arguments were verified live against JP on 2026-10-27 (see the
`trip_plans` docstring); these tests pin the parser, the paging and the fail-open behaviour
around them. Every parser check reads a reply captured verbatim from that session (the
`RECORDED_*` constants at the bottom of this file), never a hand-written one: a fixture written
from the parser's own assumptions agrees with the parser whatever either of them does.

Run: uv run --with pydantic python test_trip_plans.py  (or under pytest)
"""

import copy
import json
import logging
import os
import re
import urllib.request
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import get_args
from zoneinfo import ZoneInfo

import alternatives
import trip_plans
import trips
from trips import DirectDeparture, PlanLeg, PlanTrip

OSLO = ZoneInfo("Europe/Oslo")
LIER = "NSR:StopPlace:426"  # a CHILD stop place: JP answers in children (parent 58835)
OSLO_S = "NSR:StopPlace:337"
DRAMMEN = "NSR:StopPlace:11"
DROBAK = "NSR:StopPlace:5576"
ASKER = "NSR:StopPlace:59616"
KOLBOTN = "NSR:StopPlace:572"
SKOYEN = "NSR:StopPlace:152"


def _at(day: date, hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=OSLO)


DAY = date(2026, 10, 28)


def _body(recorded: str) -> dict:
    return json.loads(recorded)


def _patterns(recorded: str) -> list[dict]:
    return _body(recorded)["data"]["trip"]["tripPatterns"]


def _page(patterns, *, cursor=None, next_date_time=None) -> dict:
    """A reply envelope around recorded patterns, for the paging scenarios."""
    metadata = {"nextDateTime": next_date_time} if next_date_time else None
    return {"data": {"trip": {"nextPageCursor": cursor, "metadata": metadata,
                              "tripPatterns": list(patterns)}}}


_CURSOR = re.compile(r'pageCursor: "([^"]*)"')


def _cursor_of(payload: dict) -> str | None:
    match = _CURSOR.search(payload["query"])
    return match[1] if match else None


def _install_post(fake):
    """Swap the network call and reset the fetch budget; returns the original.

    Same reason as `test_alternatives._install_fetch`: the budget counts per trailing minute, and
    without the reset every fetch in the file accumulates into one minute and a later test is
    throttled by the ones before it.
    """
    original = alternatives._post_json
    alternatives._post_json = fake
    alternatives._fetch_calls = {path: [] for path in alternatives._FETCH_CEILINGS}
    alternatives._refusals = {}
    return original


def _fetch(fake, from_stop, to_stop, start, end, places, **kwargs):
    original = _install_post(fake)
    try:
        return trip_plans.fetch_range(
            from_stop, to_stop, start, end, from_places=frozenset(places), **kwargs
        )
    finally:
        alternatives._post_json = original


def _serve(*bodies):
    """A fake `_post_json` answering page N with `bodies[N]`, recording every payload it got."""
    seen: list[dict] = []

    def fake(url, payload, headers):
        seen.append(payload)
        body = bodies[min(len(seen), len(bodies)) - 1]
        if isinstance(body, BaseException):
            raise body
        return copy.deepcopy(body)

    fake.seen = seen
    return fake


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _capture_logs():
    handler = _LogCapture()
    trip_plans.logger.addHandler(handler)
    trip_plans.logger.setLevel(logging.DEBUG)
    return handler


def _fields(record) -> dict:
    return getattr(record, "fields", {}) or {}


def _fetch_records(handler) -> list[dict]:
    return [_fields(r) for r in handler.records if _fields(r).get("event") == "trip_plans_fetch"]


# ---- the recorded replies and the document ---------------------------------------------------
def test_the_recorded_replies_are_the_ones_that_were_captured():
    """Byte-pinned, so a fixture cannot drift from what JP sent without this saying so."""
    for recorded, size, n in (
        (RECORDED_LIER_P1, RECORDED_LIER_P1_BYTES, 4),
        (RECORDED_LIER_P2, RECORDED_LIER_P2_BYTES, 4),
        (RECORDED_DROBAK, RECORDED_DROBAK_BYTES, 2),
        (RECORDED_KOLBOTN, RECORDED_KOLBOTN_BYTES, 5),
    ):
        assert len(recorded.encode("utf-8")) == size
        assert len(_patterns(recorded)) == n


def _render(cursor: str | None = None) -> str:
    return trip_plans._render(LIER, OSLO_S, _at(DAY, "07:00"), cursor)


def test_the_document_asks_for_the_planned_structure_and_nothing_heavier():
    """The arguments are the verified ones (module docstring): `ignoreRealtimeUpdates` keeps a
    cancelled departure, `timetableView` and `searchWindow` stay unset because setting either cut
    patterns, and the call lists stay forbidden for the reason `alternatives._TRIP_QUERY` gives.
    `realtime` is not selected: under `ignoreRealtimeUpdates` it reads false on every leg.
    """
    doc = _render()
    assert "ignoreRealtimeUpdates: true" in doc
    for absent in ("timetableView", "searchWindow", "estimatedCalls", "intermediateEstimatedCalls",
                   "realtime", "includeRealtimeCancellations", "pageCursor", "%"):
        assert absent not in doc.replace("ignoreRealtimeUpdates", ""), absent
    assert doc.count("{") == doc.count("}"), "unbalanced braces"
    assert f'from: {{place: "{LIER}"}}' in doc and f'to: {{place: "{OSLO_S}"}}' in doc
    assert 'dateTime: "2026-10-28T07:00:00+01:00"' in doc

    # Every field the document selects came back in the recorded replies, so the parser is checked
    # against JP's real answer for each one (the replies carry more: see their comments).
    keys: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                keys.add(key)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for recorded in (RECORDED_LIER_P1, RECORDED_DROBAK):
        walk(_body(recorded)["data"])
    selection = doc.split(") {", 1)[1]
    for field in re.findall(r"[A-Za-z]+", selection):
        assert field in keys, f"{field!r} is selected but no recorded reply carries it"


def test_a_cursor_page_is_the_same_request_plus_the_cursor():
    cursor = _body(RECORDED_LIER_P1)["data"]["trip"]["nextPageCursor"]
    paged = _render(cursor)
    assert f'pageCursor: "{cursor}"' in paged
    assert paged.replace(f'\n    pageCursor: "{cursor}"', "") == _render()


# ---- parsing ---------------------------------------------------------------------------------
def _lier(**kwargs):
    return _fetch(_serve(_body(RECORDED_LIER_P1)), LIER, OSLO_S, _at(DAY, "07:00"),
                  _at(DAY, "08:20"), {LIER}, **kwargs)


def test_lier_to_oslo_s_parses_one_trip_with_a_change_at_drammen():
    got = _lier()
    assert got.result == "complete" and got.pages == 1
    assert len(got.trips) == 1
    trip = got.trips[0]
    first, second = trip.legs
    assert (first.board_stop_place, first.alight_stop_place) == (LIER, DRAMMEN)
    assert (second.board_stop_place, second.alight_stop_place) == (DRAMMEN, OSLO_S)
    assert (first.public_code, second.public_code) == ("R13", "R12")
    assert first.line == "VYG:Line:R13" and first.mode == "rail"
    assert first.walk_after_s == 126, "07:24:00 -> 07:26:06 on foot at Drammen"
    assert second.walk_after_s == 0, "nothing after the last leg"
    assert first.refs == (
        ("VYG", "DatedServiceJourney:1606_DAL-DRM_26-10-28"),
        ("VYG", "ServiceJourney:1606_443223-R"),
    ), "both forms, dated first: rail's cache keys on the dated one"
    assert second.refs[0] == ("VYG", "DatedServiceJourney:552_DRM-EVL_26-10-28")
    # JP's own id, kept whole for the real-time read (U4): `refs` drop the codespace from the key.
    assert first.dated_ref == "VYG:DatedServiceJourney:1606_DAL-DRM_26-10-28"
    assert first.operator == second.operator == "VYG"
    assert (first.board_quay, first.board_position) == ("NSR:Quay:712", 15)
    assert (first.alight_quay, first.alight_position) == ("NSR:Quay:18", 17)
    assert second.board_position == 0, "JP counts from 0; ET's stop_order is this + 1 for VYG"
    assert first.service_date == DAY
    # JP's own stop names, which a leg our cache does not score has no other source for.
    assert (first.board_name, first.alight_name) == ("Lier stasjon", "Drammen stasjon")
    assert second.alight_name == "Oslo S"
    assert first.cancelled is False and second.cancelled is False
    assert first.aimed_departure == _at(DAY, "07:17") and first.aimed_arrival == _at(DAY, "07:24")
    assert trip.aimed_departure == _at(DAY, "07:17")
    assert trip.aimed_arrival == _at(DAY, "08:05")
    assert trip.dropped is False


def test_lier_direct_departures_keep_their_journey_departure_and_arrival():
    directs = _lier().directs
    assert [(d.aimed_departure, d.aimed_arrival) for d in directs] == [
        (_at(DAY, "07:11"), _at(DAY, "07:41")),
        (_at(DAY, "07:41"), _at(DAY, "08:11")),
        (_at(DAY, "08:11"), _at(DAY, "08:41")),
    ]
    first = directs[0]
    assert first.operator == "VYG" and first.board_stop_place == LIER
    assert first.refs == (
        ("VYG", "DatedServiceJourney:1611_DRM-DAL_26-10-28"),
        ("VYG", "ServiceJourney:1611-SUD_766511-R"),
    )


def test_the_bus_leg_keeps_the_plain_service_journey_the_bus_cache_keys_on():
    """RUT's cache keys on the plain `ServiceJourney:<hash>`, rail's on the dated ref: both are kept
    so U3 can try each. The two patterns share the 07:07 bus 500 and continue two ways (KTD19's
    siblings), and both survive as separate trips."""
    got = _fetch(_serve(_body(RECORDED_DROBAK)), DROBAK, ASKER, _at(DAY, "07:00"),
                 _at(DAY, "07:30"), {DROBAK})
    assert got.result == "complete" and got.directs == ()
    assert len(got.trips) == 2
    bus = got.trips[0].legs[0]
    assert bus.operator == "RUT" and bus.mode == "bus" and bus.public_code == "500"
    assert ("RUT", "ServiceJourney:abca14c8fae00bc75a7766a018b3a528") in bus.refs
    assert bus.refs[0] == ("RUT", "DatedServiceJourney:djj-788b507489c0740b5e252cd34c39f14c")
    assert bus.dated_ref == "RUT:DatedServiceJourney:djj-788b507489c0740b5e252cd34c39f14c"
    assert got.trips[0].legs[2].dated_ref == "VYG:DatedServiceJourney:507_EVL-KBG_26-10-28"
    assert got.trips[1].legs[0].refs == bus.refs, "one first train"
    assert [len(t.legs) for t in got.trips] == [3, 2]
    assert got.trips[0].legs[1].walk_after_s == 100, "bus 81 -> the R12 at Lysaker"
    assert got.trips[0].legs[0].walk_after_s == 0, "bus to bus at one stop, no foot leg"
    assert got.trips[1].legs[0].walk_after_s == 464
    assert {t.aimed_arrival for t in got.trips} == {_at(DAY, "08:29")}
    assert got.trips[0].legs[-1].operator == "VYG"


def test_walks_before_the_first_and_after_the_last_leg_are_no_transfer():
    """JP may walk the traveller to the first stop or from the last one; only a walk BETWEEN two
    ridden legs is a transfer's walk."""
    pattern = _patterns(RECORDED_LIER_P1)[1]
    foot = pattern["legs"][1]
    pattern["legs"] = [copy.deepcopy(foot), *pattern["legs"], copy.deepcopy(foot)]
    got = _fetch(_serve(_page([pattern])), LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "08:20"),
                 {LIER})
    assert len(got.trips) == 1
    assert [leg.walk_after_s for leg in got.trips[0].legs] == [126, 0]


def test_direct_and_walk_only_patterns_are_not_trips():
    recorded = _patterns(RECORDED_LIER_P1)
    foot = recorded[1]["legs"][1]
    walk_only = {"aimedStartTime": foot["aimedStartTime"], "legs": [foot]}
    got = _fetch(_serve(_page([recorded[0], walk_only])), LIER, OSLO_S, _at(DAY, "07:00"),
                 _at(DAY, "08:20"), {LIER})
    assert got.result == "complete" and got.trips == ()
    assert len(got.directs) == 1 and got.directs[0].aimed_departure == _at(DAY, "07:11")


def test_a_pattern_boarding_outside_the_from_place_is_neither():
    """JP answers in child stop places; a registered PARENT (Lier 58835) is not one of them, so the
    caller passes the place's refs. Here they exclude Lier, so nothing JP offered boards there."""
    got = _lier_with_places({"NSR:StopPlace:58835"})
    assert got.result == "complete" and got.trips == () and got.directs == ()


def _lier_with_places(places):
    return _fetch(_serve(_body(RECORDED_LIER_P1)), LIER, OSLO_S, _at(DAY, "07:00"),
                  _at(DAY, "08:20"), places)


def test_patterns_departing_outside_the_range_are_not_kept():
    got = _fetch(_serve(_body(RECORDED_LIER_P1)), LIER, OSLO_S, _at(DAY, "07:12"),
                 _at(DAY, "07:41"), {LIER})
    assert [t.aimed_departure for t in got.trips] == [_at(DAY, "07:17")]
    assert [d.aimed_departure for d in got.directs] == [_at(DAY, "07:41")], "both ends inclusive"


def test_the_cancelled_l2_departure_is_kept_by_the_planned_structure():
    """VYG 2868 was cancelled at every call on 2026-10-27. Without `ignoreRealtimeUpdates` JP left
    the 21:10 out; with it the departure is there, carrying no cancellation flag (that state comes
    from the prediction cache and U4's real-time fetch, never from the plan)."""
    day = date(2026, 10, 27)
    got = _fetch(_serve(_body(RECORDED_KOLBOTN)), KOLBOTN, SKOYEN, _at(day, "21:05"),
                 _at(day, "22:00"), {KOLBOTN})
    assert got.result == "complete"
    first = got.directs[0]
    assert first.aimed_departure == _at(day, "21:10")
    assert first.refs[0] == ("VYG", "DatedServiceJourney:2868_SKI-SKOe_26-10-27")
    assert [d.aimed_departure for d in got.directs] == [
        _at(day, "21:10"), _at(day, "21:25"), _at(day, "21:55")]
    assert [t.aimed_departure for t in got.trips] == [_at(day, "21:40")]
    assert not any(leg.cancelled for t in got.trips for leg in t.legs)


def _malformed_variants() -> dict[str, object]:
    """Recorded Lier patterns with one leg broken, by what is broken."""
    recorded = _patterns(RECORDED_LIER_P1)
    no_time = copy.deepcopy(recorded[1])
    no_time["legs"][2]["aimedEndTime"] = None
    no_refs = copy.deepcopy(recorded[3])
    no_refs["legs"][0]["serviceJourney"] = no_refs["legs"][0]["datedServiceJourney"] = None
    no_board = copy.deepcopy(recorded[1])
    no_board["legs"][2]["fromPlace"] = None
    no_alight = copy.deepcopy(recorded[0])
    no_alight["legs"][0]["toPlace"] = {"name": "Oslo S", "quay": None}
    return {"no aimed time": no_time, "no journey ref": no_refs,
            "no boarding stop place": no_board, "no alighting stop place": no_alight,
            "not a pattern": "junk"}


def test_a_malformed_leg_fails_its_page():
    """Only a `complete` fetch is merged, and a merge marks every stored trip it lacks `dropped`
    (KTD1). Were a malformed pattern merely skipped, a JP change breaking every leg would merge as
    a pair with no trips, invisibly; so it fails the page, and the good patterns beside it go too."""
    recorded = _patterns(RECORDED_LIER_P1)
    handler = _capture_logs()
    try:
        for label, broken in _malformed_variants().items():
            got = _fetch(_serve(_page([recorded[0], broken, recorded[2]])), LIER, OSLO_S,
                         _at(DAY, "07:00"), _at(DAY, "08:20"), {LIER})
            assert got.result == "failed", label
            assert got.trips == () and got.directs == () and got.pages == 0, label
            assert _fetch_records(handler)[-1]["reason"] == "malformed", label
    finally:
        trip_plans.logger.removeHandler(handler)


def test_a_malformed_leg_on_a_later_page_is_partial():
    broken = _malformed_variants()["no journey ref"]
    got = _fetch(_serve(_body(RECORDED_LIER_P1), _page([broken], cursor="x")), LIER, OSLO_S,
                 _at(DAY, "07:00"), _at(DAY, "09:00"), {LIER})
    assert got.result == "partial" and got.pages == 1
    assert len(got.trips) == 1 and len(got.directs) == 3, "page 1 is kept; page 2 adds nothing"


def test_walk_only_and_foreign_boarding_patterns_are_ordinary_drops():
    """Neither is malformed: a page holding them beside good patterns is still `complete`."""
    recorded = _patterns(RECORDED_LIER_P1)
    walk_only = {"aimedStartTime": recorded[1]["aimedStartTime"], "legs": [recorded[1]["legs"][1]]}
    elsewhere = copy.deepcopy(recorded[2])
    elsewhere["legs"][0]["fromPlace"] = recorded[1]["legs"][2]["fromPlace"]  # boards at Drammen
    got = _fetch(_serve(_page([recorded[0], walk_only, elsewhere])), LIER, OSLO_S,
                 _at(DAY, "07:00"), _at(DAY, "08:20"), {LIER})
    assert got.result == "complete"
    assert [d.aimed_departure for d in got.directs] == [_at(DAY, "07:11")]


# ---- paging ----------------------------------------------------------------------------------
def test_two_pages_follow_the_cursor_and_complete():
    fake = _serve(_body(RECORDED_LIER_P1), _body(RECORDED_LIER_P2))
    got = _fetch(fake, LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "09:00"), {LIER})
    assert got.result == "complete" and got.pages == 2
    assert [_cursor_of(p) for p in fake.seen] == [
        None, _body(RECORDED_LIER_P1)["data"]["trip"]["nextPageCursor"]]
    assert [d.aimed_departure for d in got.directs] == [
        _at(DAY, "07:11"), _at(DAY, "07:41"), _at(DAY, "08:11"), _at(DAY, "08:41")]
    assert len(got.trips) == 1


def test_paging_stops_once_the_next_window_starts_past_the_range():
    fake = _serve(_body(RECORDED_LIER_P1), _body(RECORDED_LIER_P2))
    got = _fetch(fake, LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "08:29"), {LIER})
    assert got.result == "complete" and got.pages == 1, "page 1's next window starts at 08:30"
    no_metadata = _body(RECORDED_LIER_P1)
    no_metadata["data"]["trip"]["metadata"] = None
    got = _fetch(_serve(no_metadata, _body(RECORDED_LIER_P2)), LIER, OSLO_S, _at(DAY, "07:00"),
                 _at(DAY, "08:10"), {LIER})
    assert got.pages == 1, "without metadata the last pattern's start (08:11) decides"
    no_cursor = _body(RECORDED_LIER_P1)
    no_cursor["data"]["trip"]["nextPageCursor"] = None
    got = _fetch(_serve(no_cursor), LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "12:00"), {LIER})
    assert got.result == "complete" and got.pages == 1, "no cursor: JP has nothing further"


def test_a_boundary_pattern_repeated_on_the_next_page_is_kept_once():
    p1, p2 = _body(RECORDED_LIER_P1), _body(RECORDED_LIER_P2)
    first, second = p1["data"]["trip"]["tripPatterns"], p2["data"]["trip"]["tripPatterns"]
    second.insert(0, copy.deepcopy(first[-1]))  # the 08:11 again, across the page boundary
    first.insert(2, copy.deepcopy(first[1]))  # and the 07:17 trip twice on one page
    got = _fetch(_serve(p1, p2), LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "09:00"), {LIER})
    assert [d.aimed_departure for d in got.directs] == [
        _at(DAY, "07:11"), _at(DAY, "07:41"), _at(DAY, "08:11"), _at(DAY, "08:41")]
    assert len(got.trips) == 1


def test_the_page_bound_ends_a_fetch_as_partial():
    """A page that keeps promising more inside the range: the bound stops it, and the answer says
    the range was not covered."""
    fake = _serve(_body(RECORDED_LIER_P1))  # every page: a cursor, next window 08:30
    start, end = _at(DAY, "07:00"), _at(DAY, "09:00")
    got = _fetch(fake, LIER, OSLO_S, start, end, {LIER})
    assert trip_plans._page_bound(start, end) == 6
    assert got.result == "partial" and got.pages == 6 == len(fake.seen)
    assert len(got.trips) == 1 and len(got.directs) == 3, "the pages that landed are kept, once"


def test_the_page_bound_covers_a_day_and_is_capped():
    start = _at(DAY, "00:00")
    assert trip_plans._page_bound(start, start + timedelta(minutes=30)) == 3
    assert trip_plans._page_bound(start, start + timedelta(minutes=31)) == 4
    assert trip_plans._page_bound(start, start + timedelta(hours=24)) == trip_plans._MAX_PAGES
    assert trip_plans._page_bound(start, start + timedelta(hours=72)) == trip_plans._MAX_PAGES
    assert trip_plans._MAX_PAGES * 36 >= 24 * 60, "a full day at the densest measured page"


# ---- failure ---------------------------------------------------------------------------------
def test_every_failure_on_the_first_page_fails_open():
    cases = {
        "a GraphQL errors envelope": RuntimeError("JP error response: [{'message': 'bad'}]"),
        "a timeout": TimeoutError("timed out"),
        "an unparseable reply": json.JSONDecodeError("Expecting value", "<html>", 0),
        "no trip object": {"data": {"trip": None}},
        "patterns that are not a list": {"data": {"trip": {"tripPatterns": "nope"}}},
        # Iterates as nothing at all, so only the type check stops it reading as an empty answer.
        "patterns that are an empty object": {"data": {"trip": {"tripPatterns": {}}}},
    }
    handler = _capture_logs()
    try:
        for label, answer in cases.items():
            got = _fetch(_serve(answer), LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "09:00"),
                         {LIER})
            assert got.result == "failed", label
            assert got.trips == () and got.directs == () and got.pages == 0, label
            record = _fetch_records(handler)[-1]
            assert record["result"] == "failed" and record["reason"], label
    finally:
        trip_plans.logger.removeHandler(handler)


def test_a_failure_on_a_later_page_is_partial_never_complete():
    for failure in (TimeoutError("timed out"), {"data": {"trip": None}}):
        got = _fetch(_serve(_body(RECORDED_LIER_P1), failure), LIER, OSLO_S, _at(DAY, "07:00"),
                     _at(DAY, "09:00"), {LIER})
        assert got.result == "partial" and got.pages == 1
        assert len(got.trips) == 1, "page 1's trips are kept; the caller decides what to merge"


def test_the_request_goes_through_the_shared_guarded_post():
    """Through `alternatives._post_json`, so the client name and the `errors` envelope are handled
    in one place: the real function runs here against a faked socket."""
    sent = []

    class _Reply:
        def __init__(self, body):
            self.body = json.dumps(body).encode("utf-8")

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    replies = [{"errors": [{"message": "Validation error"}], "data": None}]
    alternatives._fetch_calls = {path: [] for path in alternatives._FETCH_CEILINGS}
    real_urlopen = urllib.request.urlopen
    before = os.environ.get(alternatives._ALLOW_NETWORK_ENV)
    os.environ[alternatives._ALLOW_NETWORK_ENV] = "1"
    urllib.request.urlopen = lambda req, timeout: sent.append(req) or _Reply(replies.pop(0))
    try:
        got = trip_plans.fetch_range(LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "08:20"),
                                     from_places=frozenset({LIER}))
        assert got.result == "failed", "an errors envelope is a failure, not an empty answer"
        replies.append(_body(RECORDED_LIER_P1))
        got = trip_plans.fetch_range(LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "08:20"),
                                     from_places=frozenset({LIER}))
        assert got.result == "complete" and len(got.trips) == 1
        assert sent[-1].full_url == alternatives._JP_URL
        assert sent[-1].get_header("Et-client-name") == alternatives.ET_CLIENT_NAME
    finally:
        urllib.request.urlopen = real_urlopen
        if before is None:
            os.environ.pop(alternatives._ALLOW_NETWORK_ENV, None)
        else:
            os.environ[alternatives._ALLOW_NETWORK_ENV] = before


def test_a_test_process_never_reaches_the_network():
    assert alternatives._network_allowed() is False, "a test process is not allowed to dial out"
    raised = []
    real = alternatives._post_json

    def spy(*args, **kwargs):
        try:
            return real(*args, **kwargs)
        except RuntimeError as exc:
            raised.append(exc)
            raise

    got = _fetch(spy, LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "08:20"), {LIER})
    assert got.result == "failed"
    assert raised and "test process" in str(raised[0]), "the guard refused before any socket"


def test_a_stop_ref_that_could_escape_the_document_is_refused_before_any_fetch():
    fake = _serve(_body(RECORDED_LIER_P1))
    for bad in ('NSR:StopPlace:426"} x', "NSR:StopPlace:426\n", "Lier"):
        got = _fetch(fake, bad, OSLO_S, _at(DAY, "07:00"), _at(DAY, "08:20"), {LIER})
        assert got.result == "failed"
        got = _fetch(fake, LIER, bad, _at(DAY, "07:00"), _at(DAY, "08:20"), {LIER})
        assert got.result == "failed"
    assert fake.seen == []


# ---- budget ----------------------------------------------------------------------------------
def test_the_fetch_paths_and_their_ceilings_agree():
    assert set(get_args(alternatives.FetchPath)) == set(alternatives._FETCH_CEILINGS)
    assert alternatives._FETCH_CEILINGS["plans"] == 60
    assert alternatives._FETCH_CEILINGS["plans_pull"] == 20
    assert alternatives._FETCH_CEILINGS["realtime"] == 20


def test_plans_refuses_past_its_ceiling_and_spends_only_its_own_share():
    def lier(path="plans"):
        alternatives._post_json = _serve(_body(RECORDED_LIER_P1), _body(RECORDED_LIER_P2))
        return trip_plans.fetch_range(LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "09:00"),
                                      from_places=frozenset({LIER}), path=path)

    original = _install_post(None)
    try:
        assert lier().pages == 2 and len(alternatives._fetch_calls["plans"]) == 2
        for other in ("request", "poller", "pull", "plans_pull", "realtime"):
            assert alternatives._fetch_calls[other] == [], other

        now = datetime.now(UTC)
        alternatives._fetch_calls["plans"] = [now] * alternatives._FETCH_CEILINGS["plans"]
        refused = lier()
        assert refused.result == "failed" and refused.pages == 0
        assert alternatives._post_json.seen == [], "no request past the ceiling"
        assert "plans" in alternatives._refusals, "the refusal is counted on its own path"
        assert alternatives._fetch_calls["poller"] == []
        assert lier("plans_pull").result == "complete", "plans_pull has its own share"

        # A refusal after a page has landed leaves the range uncovered: partial.
        alternatives._fetch_calls["plans"] = [now] * (alternatives._FETCH_CEILINGS["plans"] - 1)
        cut = lier()
        assert cut.result == "partial" and cut.pages == 1
    finally:
        alternatives._post_json = original
        alternatives._fetch_calls = {path: [] for path in alternatives._FETCH_CEILINGS}
        alternatives._refusals = {}


# ---- logging ---------------------------------------------------------------------------------
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")


def test_no_log_record_carries_a_stop_ref_a_journey_ref_or_a_time():
    """A stop pair beside a time is a registered traveller's commute (R17): the fetch is labelled
    by `pair_digest` only. The failing stub's own message carries a ref and a time, so a record that
    echoed the exception would fail here."""
    leaky = RuntimeError("JP error response: NSR:StopPlace:426 07:17 VYG:ServiceJourney:1606_x")
    no_refs = copy.deepcopy(_patterns(RECORDED_LIER_P1)[1])
    no_refs["legs"][0]["serviceJourney"] = no_refs["legs"][0]["datedServiceJourney"] = None
    handler = _capture_logs()
    try:
        _fetch(_serve(_body(RECORDED_LIER_P1)), LIER, OSLO_S, _at(DAY, "07:00"),
               _at(DAY, "08:20"), {LIER})
        _fetch(_serve(_body(RECORDED_LIER_P1), leaky), LIER, OSLO_S, _at(DAY, "07:00"),
               _at(DAY, "09:00"), {LIER})
        _fetch(_serve(leaky), LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "09:00"), {LIER})
        _fetch(_serve(_page([no_refs])), LIER, OSLO_S, _at(DAY, "07:00"), _at(DAY, "09:00"),
               {LIER})
        _fetch(_serve(_body(RECORDED_LIER_P1)), LIER, OSLO_S, _at(DAY, "07:00"),
               _at(DAY, "09:00"), {LIER})
        _fetch(_serve(_body(RECORDED_LIER_P1)), "NSR:StopPlace:426\n", OSLO_S,
               _at(DAY, "07:00"), _at(DAY, "09:00"), {LIER})
        seen = {(f.get("result"), f.get("reason")) for f in _fetch_records(handler)}
        for expected in (("complete", None), ("partial", "fetch_error"), ("failed", "fetch_error"),
                         ("partial", "page_bound"), ("failed", "bad_stop_ref"),
                         ("failed", "malformed")):
            assert expected in seen, f"{expected} was never logged, so it was never checked"
        for record in handler.records:
            text = f"{record.getMessage()} {_fields(record)!r}"
            if record.exc_info:
                text += logging.Formatter().formatException(record.exc_info)
            assert "NSR:" not in text, text
            assert "ServiceJourney" not in text, text
            assert not _TIME.search(text), text
            assert _fields(record)["pair_digest"], "every record is labelled, by the digest only"
    finally:
        trip_plans.logger.removeHandler(handler)


# ---- the records -----------------------------------------------------------------------------
def test_a_plan_trip_round_trips_through_json():
    """What the plan store does: dump to JSON-safe values, read back, get the same record."""
    got = _fetch(_serve(_body(RECORDED_DROBAK)), DROBAK, ASKER, _at(DAY, "07:00"),
                 _at(DAY, "07:30"), {DROBAK})
    for trip in got.trips:
        dumped = json.loads(json.dumps(trip.model_dump(mode="json")))
        again = PlanTrip.model_validate(dumped)
        assert again == trip and hash(again) == hash(trip)
        assert again.legs[0].refs == trip.legs[0].refs and isinstance(again.legs[0].refs, tuple)
        # A plan stored before the leg kept JP's id still reads, with no id (U4: never fetched).
        older = dumped["legs"][0]
        del older["dated_ref"]
        assert PlanLeg.model_validate(older).dated_ref is None
    direct = _lier().directs[0]
    assert DirectDeparture.model_validate(json.loads(json.dumps(direct.model_dump(mode="json")))) \
        == direct
    try:
        PlanTrip(legs=(got.trips[0].legs[0],))
    except ValueError:
        pass
    else:
        raise AssertionError("a trip with one leg is a direct departure, never a trip")
    try:
        got.trips[0].legs[0].walk_after_s = 5  # type: ignore[misc]
    except ValueError:
        pass
    else:
        raise AssertionError("plan records are frozen")
    assert isinstance(got.trips[0].legs[0], PlanLeg)


# ---- U2: the day plan, the merge, the due gate, the store seam and the follow -----------------
#
# Hand-built records, unlike the parser checks above: what is under test here is set arithmetic
# over trips the parser already produced, so each identity is spelled out to read as the scenario.

T0 = datetime(2026, 10, 27, 21, 0, tzinfo=OSLO)  # the evening before DAY
REFRESH = timedelta(seconds=trip_plans.PLAN_REFRESH_S)
LIER_PARENT = "NSR:StopPlace:58835"


def _leg(board, alight, departure, minutes, *, journey, day=DAY, walk=0, quay=None) -> PlanLeg:
    return PlanLeg(
        operator="VYG",
        refs=(("VYG", f"DatedServiceJourney:{journey}"),),
        line="VYG:Line:R13",
        public_code="R13",
        mode="rail",
        board_stop_place=board,
        alight_stop_place=alight,
        board_quay=None,
        alight_quay=quay,
        board_position=None,
        alight_position=None,
        aimed_departure=departure,
        aimed_arrival=departure + timedelta(minutes=minutes),
        service_date=day,
        walk_after_s=walk,
    )


def _trip(hhmm, *, first="1606", second="1206", change_after=15, day=DAY, quay=None) -> PlanTrip:
    """Lier -> Drammen -> Oslo S leaving at `hhmm`. Two trips sharing `first` and `hhmm` with a
    different `second` are KTD19 siblings: one first train, two continuations."""
    departure = _at(day, hhmm)
    return PlanTrip(
        legs=(
            _leg(LIER, DRAMMEN, departure, 10, journey=first, day=day, walk=120),
            _leg(DRAMMEN, OSLO_S, departure + timedelta(minutes=change_after), 35,
                 journey=second, day=day, quay=quay),
        )
    )


def _direct(hhmm, *, journey="1611", day=DAY) -> DirectDeparture:
    departure = _at(day, hhmm)
    return DirectDeparture(operator="VYG", refs=(("VYG", f"DatedServiceJourney:{journey}"),),
                           board_stop_place=LIER, aimed_departure=departure,
                           aimed_arrival=departure + timedelta(minutes=30))


def _complete(*found, directs=()) -> trip_plans.FetchResult:
    return trip_plans.FetchResult(result="complete", trips=tuple(found), directs=tuple(directs),
                                  pages=1)


def _span(a, b, day=DAY):
    return _at(day, a), _at(day, b)


def _dropped(plan) -> dict[datetime, bool]:
    return {trip.aimed_departure: trip.dropped for trip in plan.trips}


def test_a_day_plan_covers_a_window_only_inside_the_union_of_its_ranges():
    def covered(a, b):
        return trips.CoveredRange(start=_at(DAY, a), end=_at(DAY, b), fetched_at=T0)

    plan = trips.DayPlan(covered=(covered("08:00", "09:00"), covered("07:00", "08:00"),
                                  covered("16:00", "17:00")))
    assert plan.covers(*_span("07:00", "09:00")), "two adjacent ranges cover their union"
    assert plan.covers(*_span("07:30", "07:45"))
    assert plan.covers(*_span("16:00", "16:00")), "a point inside a range"
    assert not plan.covers(*_span("08:30", "16:30")), "a gap between two ranges"
    assert not plan.covers(*_span("06:59", "07:30"))
    assert not plan.covers(*_span("16:30", "17:01"))
    assert not trips.DayPlan().covers(*_span("07:00", "07:00")), "no range covers nothing"
    # Instants, not wall-clock strings: `journey_windows` hands over UTC.
    start, end = _span("07:00", "09:00")
    assert plan.covers(start.astimezone(UTC), end.astimezone(UTC))


def test_the_flag_is_an_allow_list_read_per_call_and_off_by_default():
    saved = os.environ.pop("TRIP_PLANS_ENABLED", None)
    try:
        assert trips.enabled() is False, "unset is off"
        for value, expected in (("1", True), ("true", True), (" Yes ", True), ("ON", True),
                                ("0", False), ("false", False), ("enabled", False), ("", False)):
            os.environ["TRIP_PLANS_ENABLED"] = value
            assert trips.enabled() is expected, value
    finally:
        os.environ.pop("TRIP_PLANS_ENABLED", None)
        if saved is not None:
            os.environ["TRIP_PLANS_ENABLED"] = saved


def test_a_refresh_that_omits_a_trip_marks_it_dropped_and_adds_the_new_one():
    """AE10, KTD11: a complete refresh never removes a trip. One JP no longer returns is marked
    `dropped` (and still judged until its first leg departs); one it newly returns is added."""
    morning = _span("07:00", "08:00")
    plan = trip_plans.merge(None, _complete(_trip("07:17"), _trip("07:47", first="1610")),
                            *morning, T0)
    later = T0 + REFRESH
    plan = trip_plans.merge(plan, _complete(_trip("07:47", first="1610"),
                                            _trip("07:32", first="1608")), *morning, later)
    assert _dropped(plan) == {_at(DAY, "07:17"): True, _at(DAY, "07:32"): False,
                              _at(DAY, "07:47"): False}
    assert [(r.start, r.end, r.fetched_at) for r in plan.covered] == [(*morning, later)]


def test_two_writes_with_different_ranges_keep_both_ranges_both_trips_and_every_dropped_mark():
    morning, evening = _span("07:00", "08:00"), _span("16:00", "17:00")
    plan = trip_plans.merge(None, _complete(_trip("07:17"), _trip("07:47", first="1610")),
                            *morning, T0)
    plan = trip_plans.merge(plan, _complete(_trip("07:47", first="1610")), *morning,
                            T0 + timedelta(minutes=5))
    plan = trip_plans.merge(plan, _complete(_trip("16:17", first="1650"),
                                            directs=[_direct("16:11")]), *evening,
                            T0 + timedelta(minutes=10))
    assert _dropped(plan) == {_at(DAY, "07:17"): True, _at(DAY, "07:47"): False,
                              _at(DAY, "16:17"): False}
    assert plan.directs == (_direct("16:11"),)
    assert plan.covers(*morning) and plan.covers(*evening)
    assert not plan.covers(morning[0], evening[1]), "the hours between were never fetched"


def test_only_trips_inside_the_refreshed_range_are_marked_and_a_mark_is_sticky():
    morning = _span("07:00", "08:00")
    plan = trip_plans.merge(None, _complete(_trip("07:00"), _trip("08:00", first="1612"),
                                            _trip("08:01", first="1614")),
                            _at(DAY, "06:00"), _at(DAY, "09:00"), T0)
    plan = trip_plans.merge(plan, _complete(), *morning, T0 + REFRESH)
    # Both ends of the range are inside it, as `fetch_range` keeps them; 08:01 was not refreshed.
    assert _dropped(plan) == {_at(DAY, "07:00"): True, _at(DAY, "08:00"): True,
                              _at(DAY, "08:01"): False}
    # A trip JP returns again keeps its mark: KTD2 keeps every `dropped` mark.
    plan = trip_plans.merge(plan, _complete(_trip("07:00")), *morning, T0 + 2 * REFRESH)
    assert _dropped(plan)[_at(DAY, "07:00")] is True
    assert len(plan.trips) == 3, "nothing is ever removed"


def test_the_newer_fetch_decides_a_returned_trips_fields_and_siblings_stay_distinct():
    morning = _span("07:00", "08:00")
    plan = trip_plans.merge(None, _complete(_trip("07:17", quay="NSR:Quay:1")), *morning, T0)
    sibling = _trip("07:17", second="1208", change_after=25)
    plan = trip_plans.merge(plan, _complete(_trip("07:17", quay="NSR:Quay:2"), sibling), *morning,
                            T0 + REFRESH)
    assert len(plan.trips) == 2, "one first train, two continuations: two trips (KTD19)"
    original = next(t for t in plan.trips if t.legs[1].refs[0][1].endswith(":1206"))
    assert original.legs[1].alight_quay == "NSR:Quay:2" and not original.dropped
    assert sibling in plan.trips


def test_a_refreshed_range_takes_the_new_fetch_time_and_the_rest_keep_theirs():
    plan = trip_plans.merge(None, _complete(), *_span("06:00", "09:00"), T0)
    later = T0 + REFRESH
    plan = trip_plans.merge(plan, _complete(), *_span("07:00", "08:00"), later)
    assert [(r.start, r.end, r.fetched_at) for r in plan.covered] == [
        (*_span("06:00", "07:00"), T0), (*_span("07:00", "08:00"), later),
        (*_span("08:00", "09:00"), T0),
    ]
    # A range inside the refreshed one is replaced, never kept beside it: the list stays bounded.
    plan = trip_plans.merge(plan, _complete(), *_span("06:00", "09:00"), later + REFRESH)
    assert [(r.start, r.end) for r in plan.covered] == [_span("06:00", "09:00")]


def test_windows_become_fetch_ranges_per_oslo_date_they_begin_on():
    """`journey_windows` yields UTC instants; a window belongs to the OSLO date it begins on, and
    that date is both the plan's key and the due gate's (memory `oslo-utc-gate-and-latch`)."""
    tomorrow = DAY + timedelta(days=1)
    windows = [
        _span("07:00", "08:00"),
        _span("07:30", "09:00"),  # overlaps the one above: one range
        _span("16:00", "17:00"),
        (_at(DAY, "22:00"), _at(tomorrow, "01:00")),  # overnight: DAY's
        # 00:30 Oslo is 23:30 UTC the day BEFORE: a UTC date would file it under DAY.
        _span("00:30", "01:30", tomorrow),
    ]
    got = trip_plans.fetch_ranges([(s.astimezone(UTC), e.astimezone(UTC)) for s, e in windows])
    assert got == [
        (DAY, *_span("07:00", "09:00")),
        (DAY, *_span("16:00", "17:00")),
        (DAY, _at(DAY, "22:00"), _at(tomorrow, "01:00")),
        (tomorrow, *_span("00:30", "01:30", tomorrow)),
    ], got
    assert all(start.tzinfo == OSLO for _day, start, _end in got), "the fetch range is Oslo time"


class _PlanStore:
    """The store seam `trip_plans` calls -- `notification_store`'s three functions -- in memory: one
    JSON document per (from, to, Oslo date) and a stamp per write, as the real one keeps them. The
    stamp is an int here; `trip_plans` treats the watermark as opaque."""

    def __init__(self) -> None:
        self.docs: dict[tuple, tuple[str, int]] = {}
        self.stamp = 0
        self.fail: BaseException | None = None
        self.reads: list[object] = []

    def merge_trip_plan(self, from_stop, to_stop, oslo_date, apply, *, now=None):
        if self.fail is not None:
            raise self.fail
        key = (from_stop, to_stop, oslo_date)
        plan = apply(self.docs[key][0] if key in self.docs else None)
        self.stamp += 1
        self.docs[key] = (plan, self.stamp)
        return plan

    def _entries(self, since):
        self.reads.append(since)
        if self.fail is not None:
            raise self.fail
        rows = sorted((stamp, key, plan) for key, (plan, stamp) in self.docs.items()
                      if since is None or stamp > since)
        entries = [SimpleNamespace(from_stop=k[0], to_stop=k[1], oslo_date=k[2], plan=p)
                   for _stamp, k, p in rows]
        return entries, max((stamp for stamp, _k, _p in rows), default=since)

    def seed_trip_plans(self, *, now=None, beat=None):
        return self._entries(None)

    def sync_trip_plans(self, *, since, now=None, beat=None):
        return self._entries(since)


def _fetcher(*results):
    """A stand-in for `fetch_range` answering call N with `results[N]` (the last one repeats)."""
    calls: list[tuple] = []

    def fetch(from_stop, to_stop, start, end, *, from_places, path="plans"):
        calls.append((from_stop, to_stop, start, end, from_places))
        return results[min(len(calls), len(results)) - 1]

    fetch.calls = calls
    return fetch


@contextmanager
def _plans(store=None, fetch=None, *, enabled=True):
    """Fresh module state for one scenario, restored after it."""
    saved = (dict(trip_plans._plans), trip_plans._store, trip_plans._watermark,
             trip_plans.fetch_range, os.environ.get("TRIP_PLANS_ENABLED"))
    trip_plans._plans.clear()
    trip_plans.configure(store=store)
    if fetch is not None:
        trip_plans.fetch_range = fetch
    os.environ["TRIP_PLANS_ENABLED"] = "1" if enabled else "0"
    try:
        yield
    finally:
        trip_plans._plans.clear()
        trip_plans._plans.update(saved[0])
        trip_plans._store, trip_plans._watermark, trip_plans.fetch_range = saved[1:4]
        os.environ.pop("TRIP_PLANS_ENABLED", None)
        if saved[4] is not None:
            os.environ["TRIP_PLANS_ENABLED"] = saved[4]


def _places(ref):
    return (ref, LIER_PARENT)


def _refresh(windows, now):
    return trip_plans.refresh(windows, places=_places, now=now)


def test_a_due_range_is_fetched_once_written_through_the_store_and_held_until_stale():
    store, fetch = _PlanStore(), _fetcher(_complete(_trip("07:17")))
    window = _span("07:00", "08:00")
    with _plans(store, fetch):
        _refresh({(LIER, OSLO_S): [window]}, T0)
        assert len(fetch.calls) == 1
        assert fetch.calls[0][:4] == (LIER, OSLO_S, *window)
        assert fetch.calls[0][4] == frozenset({LIER, LIER_PARENT}), "the from-place's refs"
        assert list(store.docs) == [(LIER, OSLO_S, DAY)]
        plan = trip_plans.day_plan(LIER, OSLO_S, DAY)
        assert plan.covers(*window) and plan.trips == (_trip("07:17"),)
        assert plan == trips.DayPlan.model_validate_json(store.docs[(LIER, OSLO_S, DAY)][0]), (
            "memory holds what the store holds"
        )
        _refresh({(LIER, OSLO_S): [window]}, T0 + REFRESH - timedelta(seconds=1))
        assert len(fetch.calls) == 1, "fresh: not again before PLAN_REFRESH_S"
        _refresh({(LIER, OSLO_S): [window]}, T0 + REFRESH)
        assert len(fetch.calls) == 2, "stale at PLAN_REFRESH_S"


def test_two_definitions_on_one_pair_share_one_plan_covering_both_windows():
    store = _PlanStore()
    fetch = _fetcher(_complete(_trip("07:17")), _complete(_trip("16:17", first="1650")))
    morning, evening = _span("07:00", "08:00"), _span("16:00", "17:00")
    with _plans(store, fetch):
        _refresh({(LIER, OSLO_S): [evening, morning]}, T0)
        assert [call[2:4] for call in fetch.calls] == [morning, evening], "soonest first"
        assert list(store.docs) == [(LIER, OSLO_S, DAY)], "one plan per pair and Oslo date"
        plan = trip_plans.day_plan(LIER, OSLO_S, DAY)
        assert plan.covers(*morning) and plan.covers(*evening) and len(plan.trips) == 2


def test_an_overnight_window_is_one_range_across_midnight_under_the_date_it_begins():
    store, fetch = _PlanStore(), _fetcher(_complete())
    tomorrow = DAY + timedelta(days=1)
    window = (_at(DAY, "22:00").astimezone(UTC), _at(tomorrow, "01:00").astimezone(UTC))
    with _plans(store, fetch):
        _refresh({(LIER, OSLO_S): [window]}, _at(DAY, "21:45"))
        assert [call[2:4] for call in fetch.calls] == [window], "one fetch across midnight"
        assert list(store.docs) == [(LIER, OSLO_S, DAY)]
        # At 00:30, 2 h 45 min on, `journey_windows` still offers the window (as yesterday's):
        # the same key, so not due.
        _refresh({(LIER, OSLO_S): [window]}, _at(tomorrow, "00:30"))
        assert len(fetch.calls) == 1
        assert trip_plans.day_plan(LIER, OSLO_S, tomorrow) is None


def test_a_window_outside_the_stored_ranges_is_uncovered_and_due_on_the_next_cycle():
    """A definition registered mid-day: its window is a miss for readers (`covers` is False, which
    U3's verdict reads as `not_evaluated`) until the next cycle fetches exactly that range."""
    store = _PlanStore()
    fetch = _fetcher(_complete(_trip("07:17")), _complete(_trip("16:17", first="1650")))
    morning, evening = _span("07:00", "08:00"), _span("16:00", "17:00")
    with _plans(store, fetch):
        _refresh({(LIER, OSLO_S): [morning]}, T0)
        assert not trip_plans.day_plan(LIER, OSLO_S, DAY).covers(*evening)
        _refresh({(LIER, OSLO_S): [morning, evening]}, T0 + timedelta(minutes=2))
        assert [call[2:4] for call in fetch.calls] == [morning, evening]
        assert trip_plans.day_plan(LIER, OSLO_S, DAY).covers(*evening)


def test_a_partial_or_failed_fetch_changes_nothing_and_leaves_the_range_due():
    morning = _span("07:00", "08:00")
    for kind in ("partial", "failed"):
        store = _PlanStore()
        broken = trip_plans.FetchResult(result=kind, trips=(_trip("07:32", first="1608"),),
                                        directs=(), pages=1)
        fetch = _fetcher(_complete(_trip("07:17")), broken)
        with _plans(store, fetch):
            _refresh({(LIER, OSLO_S): [morning]}, T0)
            before, written = trip_plans.day_plan(LIER, OSLO_S, DAY), dict(store.docs)
            _refresh({(LIER, OSLO_S): [morning]}, T0 + REFRESH)
            assert trip_plans.day_plan(LIER, OSLO_S, DAY) == before, kind
            assert store.docs == written, f"a {kind} fetch writes nothing"
            assert not any(trip.dropped for trip in before.trips)
            _refresh({(LIER, OSLO_S): [morning]}, T0 + REFRESH + timedelta(minutes=2))
            assert len(fetch.calls) == 3, f"a {kind} range is due again on the next cycle"
    # And a pair with no plan at all keeps none, so its readers have nothing to evaluate.
    with _plans(_PlanStore(), _fetcher(trip_plans.FetchResult("failed", (), (), 0))):
        _refresh({(LIER, OSLO_S): [morning]}, T0)
        assert trip_plans.day_plan(LIER, OSLO_S, DAY) is None


def test_a_pair_partial_on_every_cycle_stays_uncovered_and_logs_partial_each_time():
    """Through the real fetch: page one lands, the page its cursor names fails, every cycle."""
    calls = []

    def post(url, payload, headers):
        calls.append(payload)
        if len(calls) % 2 == 0:
            raise TimeoutError("page two")
        return _body(RECORDED_LIER_P1)

    handler = _capture_logs()
    store = _PlanStore()
    original = _install_post(post)
    try:
        with _plans(store):
            for cycle in range(3):
                _refresh({(LIER, OSLO_S): [_span("07:00", "09:00")]}, T0 + cycle * REFRESH)
            assert trip_plans.day_plan(LIER, OSLO_S, DAY) is None and store.docs == {}
    finally:
        alternatives._post_json = original
        trip_plans.logger.removeHandler(handler)
    assert [r["result"] for r in _fetch_records(handler)] == ["partial"] * 3
    assert len(calls) == 6


def test_the_soonest_range_goes_first_and_the_deadline_leaves_the_rest_due():
    clock = [0.0]
    store = _PlanStore()
    inner = _fetcher(_complete())

    def slow(*args, **kwargs):
        clock[0] += 15.0
        return inner(*args, **kwargs)

    saved = trip_plans.monotonic
    trip_plans.monotonic = lambda: clock[0]
    tomorrow = DAY + timedelta(days=1)
    windows = {
        ("A", "B"): [_span("16:00", "17:00")],
        ("C", "D"): [_span("07:00", "08:00")],
        ("E", "F"): [_span("07:00", "08:00", tomorrow)],
    }
    try:
        with _plans(store, slow):
            assert trip_plans._PHASE_DEADLINE_S == 20
            _refresh(windows, T0)
            # 0 s: C (07:00), 15 s: A (16:00), 30 s: past the deadline, E stays due.
            assert [call[:2] for call in inner.calls] == [("C", "D"), ("A", "B")]
            _refresh(windows, T0 + timedelta(minutes=2))
            assert [call[:2] for call in inner.calls][2:] == [("E", "F")]
    finally:
        trip_plans.monotonic = saved


def test_a_store_failure_merges_in_memory_and_never_raises():
    store, fetch = _PlanStore(), _fetcher(_complete(_trip("07:17")))
    store.fail = RuntimeError("store down")
    handler = _capture_logs()
    try:
        with _plans(store, fetch):
            _refresh({(LIER, OSLO_S): [_span("07:00", "08:00")]}, T0)
            assert trip_plans.day_plan(LIER, OSLO_S, DAY).trips == (_trip("07:17"),)
    finally:
        trip_plans.logger.removeHandler(handler)
    errors = [_fields(r) for r in handler.records if _fields(r).get("result") == "store_error"]
    assert len(errors) == 1 and errors[0]["pair_digest"], errors
    assert not any("NSR:" in r.getMessage() for r in handler.records)


def test_without_a_store_a_pod_keeps_its_plans_in_memory():
    """tst has no store: its single pod fetches and holds the plans itself, and follows nothing."""
    fetch = _fetcher(_complete(_trip("07:17")))
    with _plans(None, fetch):
        _refresh({(LIER, OSLO_S): [_span("07:00", "08:00")]}, T0)
        assert trip_plans.day_plan(LIER, OSLO_S, DAY).trips == (_trip("07:17"),)
        trip_plans.follow()
        assert trip_plans.day_plan(LIER, OSLO_S, DAY) is not None


def test_plans_older_than_yesterday_are_pruned():
    with _plans(None, _fetcher(_complete())):
        for back in (0, 1, 2):
            trip_plans._plans[(LIER, OSLO_S, DAY - timedelta(days=back))] = trips.DayPlan()
        _refresh({}, _at(DAY, "08:00"))
        assert sorted(key[2] for key in trip_plans._plans) == [DAY - timedelta(days=1), DAY]


def test_the_write_hands_the_store_one_pure_merge_and_never_replaces_what_it_cannot_read():
    """The store runs `apply` inside a transaction body, which may run more than once."""
    seen = {}

    class _Recording(_PlanStore):
        def merge_trip_plan(self, from_stop, to_stop, oslo_date, apply, *, now=None):
            seen["apply"] = apply
            return super().merge_trip_plan(from_stop, to_stop, oslo_date, apply, now=now)

    morning = _span("07:00", "08:00")
    with _plans(_Recording(), _fetcher(_complete(_trip("07:17")))):
        _refresh({(LIER, OSLO_S): [morning]}, T0)
    apply = seen["apply"]
    stored = trip_plans.merge(None, _complete(_trip("07:47", first="1610")), *morning,
                              T0 - REFRESH).model_dump_json()
    assert apply(stored) == apply(stored), "pure: the same reads write the same thing"
    assert _dropped(trips.DayPlan.model_validate_json(apply(stored))) == {
        _at(DAY, "07:17"): False, _at(DAY, "07:47"): True,
    }
    try:
        apply("not a plan")
    except ValueError:
        pass
    else:
        raise AssertionError("an unreadable stored plan must fail the write, never be replaced")


@contextmanager
def _clock_at(at: datetime):
    """trip_plans' wall clock pinned to `at`. `follow()` prunes on `datetime.now`, so a test on the
    fixed DAY broke for good two days after DAY (2026-10-30) unless the clock is held there."""
    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):
            return at.astimezone(tz)

    trip_plans.datetime = _Pinned
    try:
        yield
    finally:
        trip_plans.datetime = datetime


def test_a_follower_seeds_then_syncs_the_leaders_plans():
    store = _PlanStore()
    with _plans(store, _fetcher(_complete(_trip("07:17")))):
        _refresh({(LIER, OSLO_S): [_span("07:00", "08:00")]}, T0)
        leader = trip_plans.day_plan(LIER, OSLO_S, DAY)
        # A follower: its own memory, the same store, on the leader's evening.
        with _clock_at(T0):
            trip_plans._plans.clear()
            trip_plans.configure(store=store)
            trip_plans.follow()
            assert store.reads == [None], "the first follow is a seed"
            assert trip_plans.day_plan(LIER, OSLO_S, DAY) == leader
            # The leader writes another pair; the follower's next tick syncs past its watermark.
            store.merge_trip_plan(DRAMMEN, OSLO_S, DAY, lambda _stored: trips.DayPlan(
                trips=(_trip("08:17"),)).model_dump_json())
            trip_plans.follow()
            assert store.reads == [None, 1]
            assert trip_plans.day_plan(DRAMMEN, OSLO_S, DAY).trips == (_trip("08:17"),)
            assert trip_plans.day_plan(LIER, OSLO_S, DAY) == leader


def test_a_follower_forgets_plans_for_dates_before_yesterday():
    """Only the leader's refresh used to prune, so a follower kept every day's plans for its whole
    life. Its follow prunes on the wall clock, as the refresh does: yesterday's stays for an
    overnight window, anything older goes."""
    today = datetime.now(UTC).astimezone(trip_plans.OSLO).date()
    old, kept = (LIER, OSLO_S, today - timedelta(days=2)), (LIER, OSLO_S, today - timedelta(days=1))
    with _plans(_PlanStore()):
        trip_plans._plans[old] = trips.DayPlan(trips=(_trip("07:17"),))
        trip_plans._plans[kept] = trips.DayPlan(trips=(_trip("07:17"),))
        trip_plans.follow()
        assert old not in trip_plans._plans
        assert kept in trip_plans._plans


def test_a_store_failure_on_follow_keeps_memory_and_never_raises():
    store = _PlanStore()
    store.docs[(LIER, OSLO_S, DAY)] = (trips.DayPlan(trips=(_trip("07:17"),)).model_dump_json(), 1)
    store.docs[(DRAMMEN, OSLO_S, DAY)] = ("not a plan", 2)
    handler = _capture_logs()
    try:
        with _plans(store), _clock_at(T0):
            trip_plans.follow()
            assert trip_plans.day_plan(LIER, OSLO_S, DAY).trips == (_trip("07:17"),)
            assert trip_plans.day_plan(DRAMMEN, OSLO_S, DAY) is None, "an unreadable one is skipped"
            store.fail = RuntimeError("store down")
            trip_plans.follow()
            assert trip_plans.day_plan(LIER, OSLO_S, DAY).trips == (_trip("07:17"),)
            assert trip_plans._watermark == 2, "a failed sync keeps the watermark"
    finally:
        trip_plans.logger.removeHandler(handler)
    results = [_fields(r).get("result") for r in handler.records]
    assert "follow_error" in results, results


def test_the_flag_off_follow_reads_nothing():
    """AE6: with the flag off no pod reads the collection, in the lifespan or on a tick."""
    store = _PlanStore()
    with _plans(store, enabled=False):
        trip_plans.follow()
        trip_plans.follow(beat=lambda: None)
    assert store.reads == []


# ---- U8: a plan fetched for a public read that missed (KTD1, KTD18) -----------------------------


def _land(*ranges, places=_places):
    """Start the read-miss flights for `ranges` (Oslo date, start, end) and wait them out."""
    flights = trip_plans.fetch_for_read(LIER, OSLO_S, list(ranges), places=places)
    for flight in flights:
        flight.join(5)
    return flights


@contextmanager
def _public():
    trip_plans.reset_public()
    try:
        yield
    finally:
        trip_plans.reset_public()


def test_a_read_miss_fetches_on_plans_pull_into_the_pods_cache_and_never_the_store():
    """KTD1 and KTD18: the read's fetch pages on `plans_pull` -- never `plans` (the leader's) or
    `poller` -- and a `complete` answer lands in this pod's cache, which `read_plan` serves and the
    store never sees; the leader's own plans (`day_plan`, its due gate) do not hold it."""
    store = _PlanStore()
    window = (_at(DAY, "07:00"), _at(DAY, "08:20"))
    original = _install_post(_serve(_body(RECORDED_LIER_P1)))
    try:
        with _plans(store), _public():
            assert len(_land((DAY, *window), places=lambda ref: (ref,))) == 1
            plan = trip_plans.read_plan(LIER, OSLO_S, DAY)
            assert plan is not None and plan.covers(*window) and plan.trips
            assert trip_plans.public_plans() == [plan]
            assert store.docs == {} and trip_plans.day_plan(LIER, OSLO_S, DAY) is None
            assert len(alternatives._fetch_calls["plans_pull"]) == 1
            for other in ("plans", "poller", "pull", "request", "realtime"):
                assert alternatives._fetch_calls[other] == [], other
    finally:
        alternatives._post_json = original
        alternatives._fetch_calls = {path: [] for path in alternatives._FETCH_CEILINGS}


def test_only_a_complete_fetch_is_kept_and_a_miss_asks_again():
    """A `partial` or `failed` fetch keeps nothing, so the read stays `not_evaluated` and its next
    miss starts another flight; a second range of the same date unions into the cached plan."""
    seen_paths: list[str] = []
    results = iter([
        trip_plans.FetchResult(result="partial", trips=(_trip("07:17"),), directs=(), pages=1),
        _complete(_trip("07:17")),
        _complete(_trip("09:17")),
    ])

    def fetch(from_stop, to_stop, start, end, *, from_places, path="plans"):
        seen_paths.append(path)
        return next(results)

    with _plans(fetch=fetch), _public():
        _land((DAY, *_span("07:00", "08:00")))
        assert trip_plans.read_plan(LIER, OSLO_S, DAY) is None, "a partial fetch is never cached"
        _land((DAY, *_span("07:00", "08:00")))
        _land((DAY, *_span("09:00", "10:00")))
        plan = trip_plans.read_plan(LIER, OSLO_S, DAY)
        assert plan.covers(*_span("07:00", "08:00")) and plan.covers(*_span("09:00", "10:00"))
        assert [trip.aimed_departure for trip in plan.trips] == [_at(DAY, "07:17"), _at(DAY, "09:17")]
    assert seen_paths == ["plans_pull"] * 3


def test_one_flight_per_pair_and_date_and_a_bounded_number_at_once():
    """One flight per (pair, Oslo date): a second miss while it is in the air starts nothing, and
    past `PUBLIC_FLIGHTS_MAX` flights a miss starts nothing either -- a later read asks again."""
    import threading

    release = threading.Event()

    def fetch(from_stop, to_stop, start, end, *, from_places, path="plans"):
        release.wait(5)
        return _complete()

    with _plans(fetch=fetch), _public():
        try:
            first = trip_plans.fetch_for_read(LIER, OSLO_S, [(DAY, *_span("07:00", "08:00"))],
                                              places=_places)
            again = trip_plans.fetch_for_read(LIER, OSLO_S, [(DAY, *_span("09:00", "10:00"))],
                                              places=_places)
            assert len(first) == 1 and again == [], "the same pair and date is one flight"
            others = [
                trip_plans.fetch_for_read(LIER, f"NSR:StopPlace:{n}", [(DAY, *_span("07:00", "08:00"))],
                                          places=_places)
                for n in range(trip_plans.PUBLIC_FLIGHTS_MAX)
            ]
            assert sum(len(started) for started in others) == trip_plans.PUBLIC_FLIGHTS_MAX - 1
        finally:
            release.set()
            for flight in first + [f for started in others for f in started]:
                flight.join(5)
        assert len(_land((DAY, *_span("07:00", "08:00")))) == 1, "landed, so a later miss flies"


def test_the_public_cache_is_count_bounded_least_recently_read_first_out():
    def fetch(from_stop, to_stop, start, end, *, from_places, path="plans"):
        return _complete(_trip("07:17"))

    saved = trip_plans.PUBLIC_PLANS_MAX
    trip_plans.PUBLIC_PLANS_MAX = 2
    try:
        with _plans(fetch=fetch), _public():
            for day in (DAY, DAY + timedelta(days=1)):
                _land((day, *_span("07:00", "08:00", day)))
            assert trip_plans.read_plan(LIER, OSLO_S, DAY) is not None  # read: now the newest
            _land((DAY + timedelta(days=2), *_span("07:00", "08:00", DAY + timedelta(days=2))))
            assert len(trip_plans.public_plans()) == 2
            assert trip_plans.read_plan(LIER, OSLO_S, DAY + timedelta(days=1)) is None, "evicted"
            assert trip_plans.read_plan(LIER, OSLO_S, DAY) is not None
    finally:
        trip_plans.PUBLIC_PLANS_MAX = saved


def test_read_plan_is_the_leaders_plan_beside_the_pods_own_and_the_due_gate_reads_the_leaders():
    """`read_plan` serves the stored plan, the public one, or both together -- every trip either
    holds, the leader's version (its `dropped` mark) winning, both sets of ranges -- while the
    leader's due gate reads its own plans alone: a public fetch covering a registered pair's range
    must not stop the leader fetching it for the store, or no follower would ever see it."""
    stored_trip, public_trip = _trip("07:17"), _trip("07:47")
    stored = trips.DayPlan(trips=(stored_trip.model_copy(update={"dropped": True}),),
                           covered=(trips.CoveredRange(start=_at(DAY, "07:00"), end=_at(DAY, "07:30"),
                                                       fetched_at=T0),))

    def fetch(from_stop, to_stop, start, end, *, from_places, path="plans"):
        return _complete(stored_trip, public_trip)

    store = _PlanStore()
    with _plans(store, fetch=fetch), _public():
        _land((DAY, *_span("07:00", "08:00")))
        assert trip_plans.read_plan(LIER, OSLO_S, DAY).trips == (
            stored_trip, public_trip,
        ), "the public plan alone"
        trip_plans._plans[(LIER, OSLO_S, DAY)] = stored
        both = trip_plans.read_plan(LIER, OSLO_S, DAY)
        assert [t.dropped for t in both.trips] == [True, False], "the leader's version wins"
        assert both.covers(*_span("07:00", "08:00")) and trip_plans.day_plan(LIER, OSLO_S, DAY) == stored
        _refresh({(LIER, OSLO_S): [_span("07:00", "08:00")]}, T0)
        assert list(store.docs) == [(LIER, OSLO_S, DAY)], "the leader still fetched it for the store"


def test_a_merged_read_is_built_once_while_both_plans_are_held_and_a_follow_keeps_an_unchanged_plan():
    """`read_plan` merges the leader's plan and the public one once, while both are the ones held,
    and every read -- the cached one too -- still marks the public plan read (the bound's order). A
    follow re-reading an unchanged document keeps its plan object, which is what keeps it cached."""
    stored = trips.DayPlan(trips=(_trip("07:17"),),
                           covered=(trips.CoveredRange(start=_at(DAY, "07:00"), end=_at(DAY, "07:30"),
                                                       fetched_at=T0),))
    tomorrow = DAY + timedelta(days=1)

    def fetch(from_stop, to_stop, start, end, *, from_places, path="plans"):
        return _complete(_trip("07:47"))

    store = _PlanStore()
    with _plans(store, fetch=fetch), _public():
        _land((DAY, *_span("07:00", "08:00")))
        _land((tomorrow, *_span("07:00", "08:00", tomorrow)))
        trip_plans._plans[(LIER, OSLO_S, DAY)] = stored
        first = trip_plans.read_plan(LIER, OSLO_S, DAY)
        assert trip_plans.read_plan(LIER, OSLO_S, tomorrow) is not None
        assert list(trip_plans._public)[-1] == (LIER, OSLO_S, tomorrow)
        assert trip_plans.read_plan(LIER, OSLO_S, DAY) is first, "built once"
        assert list(trip_plans._public)[-1] == (LIER, OSLO_S, DAY), "the cached read marks it read"
        trip_plans._plans[(LIER, OSLO_S, DAY)] = stored.model_copy()
        again = trip_plans.read_plan(LIER, OSLO_S, DAY)
        assert again is not first and again == first, "a replaced plan is merged anew"

        store.docs[(DRAMMEN, OSLO_S, DAY)] = (stored.model_dump_json(), 1)
        with _clock_at(T0):
            trip_plans.follow()
            held = trip_plans.day_plan(DRAMMEN, OSLO_S, DAY)
            trip_plans.configure(store=store)  # a seed again: the same document re-read
            trip_plans.follow()
            assert trip_plans.day_plan(DRAMMEN, OSLO_S, DAY) is held, "an unchanged document keeps it"
            store.docs[(DRAMMEN, OSLO_S, DAY)] = (
                trips.DayPlan(trips=(_trip("08:17"),)).model_dump_json(), 2)
            trip_plans.follow()
            assert trip_plans.day_plan(DRAMMEN, OSLO_S, DAY).trips == (_trip("08:17"),)


def test_a_flight_that_raises_is_logged_by_class_and_frees_its_key():
    """A flight is a daemon thread: unguarded, a raise would reach stderr and nothing else. It is
    logged as a failed `plans_pull` fetch, labelled by digest, and a later miss flies again."""
    def fetch(from_stop, to_stop, start, end, *, from_places, path="plans"):
        raise KeyError(LIER)

    handler = _capture_logs()
    try:
        with _plans(fetch=fetch), _public():
            assert len(_land((DAY, *_span("07:00", "08:00")))) == 1
            assert len(_land((DAY, *_span("07:00", "08:00")))) == 1, "the key was freed"
    finally:
        trip_plans.logger.removeHandler(handler)
    [record, _again] = _fetch_records(handler)
    assert (record["result"], record["reason"], record["error"]) == ("failed", "flight_error", "KeyError")
    assert LIER not in json.dumps(record) and trip_plans.read_plan(LIER, OSLO_S, DAY) is None


def test_with_the_flag_off_a_read_miss_starts_nothing():
    calls = []
    with _plans(fetch=lambda *a, **kw: calls.append(a) or _complete(), enabled=False), _public():
        assert trip_plans.fetch_for_read(LIER, OSLO_S, [(DAY, *_span("07:00", "08:00"))],
                                         places=_places) == []
    assert calls == []


# ---- U4: Journey Planner real-time for unscored legs -------------------------------------------
#
# RECORDED_REALTIME answers the batched document for exactly the two journeys RECORDED_DROBAK's trips
# ride (the RUT 500 bus and the VYG R12 507) plus an id JP does not know, so the parse, the fetch,
# the reader and the verdict below run on what JP sent. Hand-built trips only where the scenario is
# a count (the batch bound, the selection), and edits to the recorded calls are made on copies.

RT_NOW = _at(DAY, "06:30")  # 37 minutes before the 07:07 bus 500
BUS_500 = "RUT:DatedServiceJourney:djj-788b507489c0740b5e252cd34c39f14c"
BUS_81 = "RUT:DatedServiceJourney:djj-8423077639bdf4ebcc01dea73f9a62a9"
R12_507 = "VYG:DatedServiceJourney:507_EVL-KBG_26-10-28"
MAX_AGE = timedelta(seconds=trip_plans.REALTIME_MAX_AGE_S)
_ALIAS = re.compile(r'(l\d+): datedServiceJourney\(id: ("(?:[^"\\]|\\.)*")\)')


def _drobak() -> trips.DayPlan:
    """RECORDED_DROBAK's two trips: the 07:07 bus 500 on to the R12 via Lysaker (bus 81) or via
    Oslo S (on foot), both at Asker 08:29. The second is the planned continuation (fewer legs)."""
    got = _fetch(_serve(_body(RECORDED_DROBAK)), DROBAK, ASKER, _at(DAY, "07:00"),
                 _at(DAY, "07:30"), {DROBAK})
    return trips.DayPlan(trips=got.trips)


def _journeys(edit=None) -> dict[str, dict]:
    """The recorded reply's answers by journey id; `edit(id, calls)` changes them (fresh copies)."""
    answers = {a["id"]: a for a in _body(RECORDED_REALTIME)["data"].values() if a is not None}
    if edit is not None:
        for ref, answer in answers.items():
            edit(ref, answer["estimatedCalls"])
    return answers


def _on_time(leg: trips.PlanLeg) -> dict:
    """A JP answer reading `leg` on time at both its calls, for a journey the recording has none for."""
    def call(stop, field, at):
        return {"realtime": True, "cancellation": False, "quay": {"stopPlace": {"id": stop}},
                f"aimed{field}Time": at.isoformat(), f"expected{field}Time": at.isoformat()}

    return {"id": leg.dated_ref, "estimatedCalls": [
        call(leg.board_stop_place, "Departure", leg.aimed_departure),
        call(leg.alight_stop_place, "Arrival", leg.aimed_arrival),
    ]}


def _asked(payload: dict) -> list[str]:
    return [json.loads(ref) for _alias, ref in _ALIAS.findall(payload["query"])]


def _jp_realtime(journeys: dict[str, dict], *, fail: dict[int, BaseException] | None = None):
    """A fake `_post_json` answering each alias of the batched document from `journeys`, and null
    for an id it does not hold, as JP does. Request N (from 0) raises `fail[N]`."""
    seen: list[dict] = []

    def fake(url, payload, headers):
        seen.append(payload)
        if fail and len(seen) - 1 in fail:
            raise fail[len(seen) - 1]
        return {"data": {alias: copy.deepcopy(journeys.get(json.loads(ref)))
                         for alias, ref in _ALIAS.findall(payload["query"])}}

    fake.seen = seen
    return fake


@contextmanager
def _rt(registered=(), *, post=None, calls=None, enabled=True):
    """Fresh real-time state for one scenario: `registered` as this pod's held plans, a call lookup
    over `calls` ((operator, jkey) -> rows; none by default, so every leg is unscored), `post` as
    the network, the flag; all restored after."""
    saved = (dict(trip_plans._plans), dict(trip_plans._answers), trips._call_lookup,
             trips._realtime, trips._places, alternatives._post_json,
             os.environ.get("TRIP_PLANS_ENABLED"))
    trip_plans._plans.clear()
    trip_plans._plans.update(
        {(f"NSR:StopPlace:{i}", ASKER, DAY): plan for i, plan in enumerate(registered)}
    )
    trip_plans._answers.clear()
    trips.configure(call_lookup=lambda operator, jkey: (calls or {}).get((operator, jkey)))
    _install_post(post)
    os.environ["TRIP_PLANS_ENABLED"] = "1" if enabled else "0"
    try:
        yield
    finally:
        trip_plans._plans.clear()
        trip_plans._plans.update(saved[0])
        trip_plans._answers.clear()
        trip_plans._answers.update(saved[1])
        trips._call_lookup, trips._realtime, trips._places = saved[2:5]
        alternatives._post_json = saved[5]
        alternatives._fetch_calls = {path: [] for path in alternatives._FETCH_CEILINGS}
        alternatives._refusals = {}
        os.environ.pop("TRIP_PLANS_ENABLED", None)
        if saved[6] is not None:
            os.environ["TRIP_PLANS_ENABLED"] = saved[6]


def _read_at(now):
    """Wire the real reader into the verdict at a fixed instant, and no scored call."""
    trips.configure(call_lookup=lambda operator, jkey: None,
                    realtime=lambda leg: trip_plans.realtime_for(leg, now=now))


def _judge_drobak(plan):
    return trips.judge(plan, to_stop=ASKER, window_start=_at(DAY, "07:00"),
                       window_end=_at(DAY, "07:30"))


def test_the_recorded_realtime_reply_is_the_one_that_was_captured():
    """Byte-pinned, and it answers the recorded plan's own journeys: the reply's two ids are two of
    the three RECORDED_DROBAK rides (bus 81 is not asked in it), the third alias an invented id."""
    assert len(RECORDED_REALTIME.encode("utf-8")) == RECORDED_REALTIME_BYTES
    data = _body(RECORDED_REALTIME)["data"]
    assert (data["l0"]["id"], data["l1"]["id"], data["l2"]) == (BUS_500, R12_507, None)
    assert [len(data[alias]["estimatedCalls"]) for alias in ("l0", "l1")] == [27, 17]
    rides = {leg.dated_ref for trip in _drobak().trips for leg in trip.legs}
    assert rides == {BUS_500, BUS_81, R12_507}


def test_the_realtime_document_asks_each_journey_by_its_full_id_and_nothing_heavier():
    doc = trip_plans._render_realtime([BUS_500, R12_507])
    assert f"l0: datedServiceJourney(id: {json.dumps(BUS_500)})" in doc
    assert f"l1: datedServiceJourney(id: {json.dumps(R12_507)})" in doc
    assert doc.count("datedServiceJourney(") == 2
    # The verified selection, field for field; `estimatedCalls` takes no arguments here.
    assert doc.count(
        "estimatedCalls { stopPositionInPattern realtime cancellation aimedArrivalTime "
        "expectedArrivalTime aimedDepartureTime expectedDepartureTime quay { stopPlace { id } } }"
    ) == 2
    for heavier in ("estimatedCalls(", "situations", "serviceJourney {", "notices", "trip("):
        assert heavier not in doc, heavier
    # A ref is spliced as a JSON string, so it cannot close the literal it sits in.
    bad = 'RUT:x") { id } evil: trip(from: {place: "a'
    assert _asked({"query": trip_plans._render_realtime([bad])}) == [bad]


def test_the_reader_reads_each_legs_own_calls_off_the_recorded_answer():
    plan = _drobak()
    via_lysaker, via_oslo_s = plan.trips
    with _rt([plan], post=_jp_realtime(_journeys())):
        counts = trip_plans.refresh_realtime(RT_NOW)
        [payload] = alternatives._post_json.seen
        assert _asked(payload) == [BUS_500, BUS_81, R12_507], "each journey once, the shared bus too"
        assert (counts["wanted"], counts["asked"], counts["answered"]) == (3, 3, 2)

        def read(leg):
            return trip_plans.realtime_for(leg, now=RT_NOW)

        # The R12 reads `realtime: true`: each leg finds its own calls in the one answer.
        assert read(via_lysaker.legs[2]) == trips.LegRealtime(
            expected_departure=_at(DAY, "08:19"), expected_arrival=_at(DAY, "08:29"))
        assert read(via_oslo_s.legs[1]) == trips.LegRealtime(
            expected_departure=_at(DAY, "08:09"), expected_arrival=_at(DAY, "08:29"))
        # The bus reads `realtime: false` on every call (JP copied the aimed times): unknown, never
        # on time. Bus 81 came back null: absent. A leg without JP's id is never asked.
        assert read(via_lysaker.legs[0]) is None and read(via_oslo_s.legs[0]) is None
        assert read(via_lysaker.legs[1]) is None
        assert read(via_lysaker.legs[2].model_copy(update={"dated_ref": None})) is None
        # A leg whose calls are not in its journey's answer (another day's run) is absent too.
        assert read(via_lysaker.legs[2].model_copy(
            update={"aimed_departure": _at(DAY, "09:19")})) is None


def test_an_answer_is_used_within_the_age_bound_and_unknown_past_it():
    """One failed refresh does not blank a leg; a second does (KTD8). A failed tick keeps the
    answer, and the first tick past the bound drops it."""
    plan = _drobak()
    r12 = plan.trips[1].legs[1]
    timeout = TimeoutError("timed out")
    with _rt([plan], post=_jp_realtime(_journeys(), fail={1: timeout, 2: timeout})):
        trip_plans.refresh_realtime(RT_NOW)
        assert trip_plans.realtime_for(r12, now=RT_NOW + MAX_AGE) is not None
        assert trip_plans.realtime_for(r12, now=RT_NOW + MAX_AGE + timedelta(seconds=1)) is None
        trip_plans.refresh_realtime(RT_NOW + timedelta(minutes=3))
        assert trip_plans._answers[R12_507][0] == RT_NOW, "a failed request leaves it as it was"
        trip_plans.refresh_realtime(RT_NOW + MAX_AGE + timedelta(seconds=1))
        assert trip_plans._answers == {}
        # Unwired from a test's clock, the reader ages an answer on the wall clock.
        trip_plans._answers[R12_507] = (datetime.now(UTC),
                                        trip_plans._calls_of(_journeys()[R12_507]))
        assert trip_plans.realtime_for(r12) is not None
        trip_plans._answers[R12_507] = (datetime.now(UTC) - MAX_AGE - timedelta(seconds=5),
                                        trip_plans._calls_of(_journeys()[R12_507]))
        assert trip_plans.realtime_for(r12) is None


def test_a_journey_jp_answers_null_forgets_its_last_answer():
    """An answered request answers every leg it asked: null is "JP has no such journey", not "no
    news", so an older answer is not kept for it."""
    plan = _drobak()
    later = RT_NOW + timedelta(minutes=2)
    with _rt([plan], post=_jp_realtime(_journeys())):
        trip_plans.refresh_realtime(RT_NOW)
        assert set(trip_plans._answers) == {BUS_500, R12_507}
        alternatives._post_json = _jp_realtime({})
        trip_plans.refresh_realtime(later)
        assert trip_plans._answers == {}
        assert trip_plans.realtime_for(plan.trips[1].legs[1], now=later) is None


def test_ae4_a_bus_jp_expects_12_minutes_late_against_6_minutes_slack_misses_and_a_cancelled_bus_is_confirmed():
    """AE4 on the recorded plan and reply: the 07:07 bus 500 reaches Oslo bussterminal at 07:55,
    7 min 44 s on foot from the 08:09 R12 at Oslo S: 6 minutes to spare. JP expecting it 12 minutes
    late is a predicted miss at 1.0 (KTD8); JP cancelling it is confirmed. Both continuations are one
    subject (they share the bus and arrive together, KTD19), and both miss."""
    plan = _drobak()

    def late(ref, calls):
        if ref == BUS_500:
            for call in calls:
                call["realtime"] = True
                for key in ("expectedArrivalTime", "expectedDepartureTime"):
                    moved = alternatives._parse_ts(call[key]) + timedelta(minutes=12)
                    call[key] = moved.isoformat()

    def cancelled_at(stop):
        def edit(ref, calls):
            if ref == BUS_500:
                for call in calls:
                    call["realtime"] = True
                    call["cancellation"] = call["quay"]["stopPlace"]["id"] == stop
        return edit

    with _rt([plan], post=_jp_realtime(_journeys(late))):
        trip_plans.refresh_realtime(RT_NOW)
        _read_at(RT_NOW)
        [verdict] = _judge_drobak(plan)
    assert verdict.alertworthy and not verdict.confirmed
    assert verdict.trip.legs[0].alight_stop_place == "NSR:StopPlace:6505", "via Oslo S, on foot"
    assert verdict.facts.transfers[0].slack_min == 6
    assert (verdict.deciding.kind, verdict.deciding.index, verdict.deciding.probability) == (
        "missed_connection", 0, 1.0)
    assert verdict.reason.situation_number == (
        "KIX:Trip:RUT:ServiceJourney:abca14c8fae00bc75a7766a018b3a528:2026-10-28"
    ), "a JP reason attaches to the arriving bus's synthetic situation"
    assert not verdict.complete, "bus 81 came back null, and its continuation is in the cushion"

    # Cancelled where the traveller boards: the shared reason, confirmed.
    with _rt([plan], post=_jp_realtime(_journeys(cancelled_at(DROBAK)))):
        trip_plans.refresh_realtime(RT_NOW)
        _read_at(RT_NOW)
        [verdict] = _judge_drobak(plan)
    assert verdict.alertworthy and verdict.confirmed
    assert (verdict.deciding.kind, verdict.deciding.index, verdict.deciding.probability) == (
        "leg_cancelled", 0, 1.0)
    assert verdict.facts.legs[0].cancelled and verdict.facts.expected_arrival is None

    # Cancelled only at Oslo bussterminal: the planned route's bus cannot be left there, but the
    # same bus still reaches Bjørvika for the continuation via Lysaker, so KTD19 holds the alert --
    # once bus 81 answers. While it comes back null, that continuation is no reading that it gets
    # there (KTD8), so the cancellation alerts, predicted.
    bus_81 = plan.trips[0].legs[1]
    for answers, alerts in ((_journeys(cancelled_at("NSR:StopPlace:6505")), True),
                            ({**_journeys(cancelled_at("NSR:StopPlace:6505")),
                              BUS_81: _on_time(bus_81)}, False)):
        with _rt([plan], post=_jp_realtime(answers)):
            trip_plans.refresh_realtime(RT_NOW)
            _read_at(RT_NOW)
            [verdict] = _judge_drobak(plan)
        assert verdict.facts.legs[0].cancelled and verdict.alertworthy is alerts, alerts
        assert not verdict.confirmed and verdict.complete is not alerts, alerts


def test_with_no_usable_answer_every_unscored_leg_is_unknown_and_the_trip_is_no_subject():
    """A new leader's first cycle with an empty cache, or the reply as JP sent it (the bus read
    `realtime: false`): nothing is read as on time, and with no figure at all there is no subject --
    nothing to alert on and nothing that counts toward a withdrawal."""
    # U5 pins the withdrawal itself
    plan = _drobak()
    with _rt([plan], post=_jp_realtime(_journeys())):
        _read_at(RT_NOW)
        assert {trips.leg_state(leg).state for trip in plan.trips for leg in trip.legs} == {
            "unknown"}
        assert _judge_drobak(plan) == []
        trip_plans.refresh_realtime(RT_NOW)
        _read_at(RT_NOW)
        assert [trips.leg_state(leg).state for leg in plan.trips[1].legs] == ["unknown", "realtime"]
        assert _judge_drobak(plan) == [], "the bus's copied aimed times are no on-time answer"


def _dated(trip: PlanTrip) -> PlanTrip:
    """`trip` with each leg's JP id, as `trip_plans._leg` parses it."""
    return trip.model_copy(update={"legs": tuple(
        leg.model_copy(update={"dated_ref": f"VYG:{leg.refs[0][1]}"}) for leg in trip.legs
    )})


def _many(prefix: str, n: int) -> trips.DayPlan:
    """`n` trips from 07:00 a minute apart, two legs each, every leg its own journey."""
    return trips.DayPlan(trips=tuple(
        _dated(_trip(f"07:{i:02d}", first=f"{prefix}{i}a", second=f"{prefix}{i}b"))
        for i in range(n)
    ))


def test_the_batch_bound_fetches_registered_pairs_legs_first_and_public_reads_wait():
    assert (trip_plans._REALTIME_BATCH, trip_plans._REALTIME_MAX_REQUESTS) == (10, 3)
    with _rt([_many("reg", 15)], post=_jp_realtime({})):
        counts = trip_plans.refresh_realtime(RT_NOW, public=[_many("pub", 5)])
        seen = alternatives._post_json.seen
    assert [len(_asked(payload)) for payload in seen] == [10, 10, 10]
    asked = [ref for payload in seen for ref in _asked(payload)]
    assert len(set(asked)) == 30 and all(":reg" in ref for ref in asked), "public reads wait"
    assert (counts["wanted"], counts["asked"], counts["requests"]) == (40, 30, 3)
    # With room left, a public read's legs fill it, after every registered one.
    with _rt([_many("reg", 2)], post=_jp_realtime({})):
        trip_plans.refresh_realtime(RT_NOW, public=[_many("pub", 20)])
        asked = [ref for payload in alternatives._post_json.seen for ref in _asked(payload)]
    assert len(asked) == 30
    assert all(":reg" in ref for ref in asked[:4]) and all(":pub" in ref for ref in asked[4:])


def test_only_legs_of_watched_trips_boarding_soon_not_yet_arrived_and_not_scored_are_asked():
    """Scored at both calls is our figures' (KTD8); scored at one call still asks, because the
    other end is unknown until JP answers (`trips.LegState.unknown`). A trip whose first leg has
    left but which has not arrived still asks; one leaving more than the look-ahead from now, or
    already arrived, or with no Vy or SJ leg (R3), does not. A journey shared by two legs (one
    first train, two continuations) is asked once."""
    now = _at(DAY, "07:00")
    horizon = timedelta(seconds=trip_plans.REALTIME_LOOKAHEAD_S)
    soon = _dated(_trip("07:30", first="soon", second="soon2"))
    sibling = _dated(_trip("07:30", first="soon", second="other"))
    running = _dated(_trip("06:30", first="run", second="run2"))  # left 06:30, arrives 07:20
    arrived = _dated(_trip("06:00", first="done", second="done2"))  # arrived 06:50
    beyond = _dated(_trip((now + horizon + timedelta(minutes=1)).strftime("%H:%M"),
                          first="late", second="late2"))
    undated = _trip("07:35", first="nodate", second="nodate2")
    buses = PlanTrip(legs=tuple(
        leg.model_copy(update={"operator": "RUT", "refs": (("RUT", leg.refs[0][1]),),
                               "dated_ref": f"RUT:{leg.refs[0][1]}"})
        for leg in _trip("07:40", first="bus", second="bus2").legs
    ))
    soon2, run2 = soon.legs[1], running.legs[1]

    def row(stop, *, dep=None, arr=None):
        return SimpleNamespace(situation_number="SX:1", stop_place=stop, aimed_dep=dep,
                               aimed_arr=arr)

    calls = {
        ("VYG", "DatedServiceJourney:soon2"): [
            row(DRAMMEN, dep=soon2.aimed_departure), row(OSLO_S, arr=soon2.aimed_arrival)],
        ("VYG", "DatedServiceJourney:run2"): [row(DRAMMEN, dep=run2.aimed_departure)],
    }
    plan = trips.DayPlan(trips=(running, soon, sibling, arrived, beyond, undated, buses))
    with _rt([plan], post=_jp_realtime({}), calls=calls):
        counts = trip_plans.refresh_realtime(now)
        [payload] = alternatives._post_json.seen
    # A trip not yet departed is asked before one already under way, whose answer can no longer
    # start an alert (R10): with the bound reached, a departed trip's legs are the ones that wait.
    assert _asked(payload) == [f"VYG:DatedServiceJourney:{j}" for j in ("soon", "other", "run",
                                                                        "run2")]
    assert counts["wanted"] == 4


def test_the_realtime_path_refuses_past_its_ceiling_and_a_refusal_keeps_the_last_answer():
    """The `realtime` row of KTD18's fetch paths, on its own share: never the plans' or the
    alternatives'."""
    plan = _drobak()
    later = RT_NOW + timedelta(minutes=2)
    with _rt([plan], post=_jp_realtime(_journeys())):
        trip_plans.refresh_realtime(RT_NOW)
        assert len(alternatives._fetch_calls["realtime"]) == 1
        for other in ("request", "poller", "pull", "plans", "plans_pull"):
            assert alternatives._fetch_calls[other] == [], other
        alternatives._fetch_calls["realtime"] = [later] * alternatives._FETCH_CEILINGS["realtime"]
        counts = trip_plans.refresh_realtime(later)
        assert len(alternatives._post_json.seen) == 1, "no request past the ceiling"
        assert (counts["requests"], counts["refused"]) == (0, 1)
        assert "realtime" in alternatives._refusals and alternatives._fetch_calls["plans"] == []
        assert trip_plans.realtime_for(plan.trips[1].legs[1], now=later) is not None


def test_a_failed_request_leaves_its_legs_as_they_were_and_the_next_request_still_goes():
    plan = _many("reg", 6)  # 12 legs: two requests a tick
    answers = {leg.dated_ref: {"id": leg.dated_ref, "estimatedCalls": []}
               for trip in plan.trips for leg in trip.legs}
    later = RT_NOW + timedelta(minutes=2)
    with _rt([plan], post=_jp_realtime(answers, fail={2: TimeoutError("timed out")})):
        trip_plans.refresh_realtime(RT_NOW)
        counts = trip_plans.refresh_realtime(later)
        failed, landed = alternatives._post_json.seen[2:]
        assert {trip_plans._answers[ref][0] for ref in _asked(failed)} == {RT_NOW}
        assert {trip_plans._answers[ref][0] for ref in _asked(landed)} == {later}
    assert (counts["requests"], counts["failed"], counts["answered"]) == (2, 1, 2)


def test_with_the_flag_off_nothing_is_fetched():
    """AE6: no real-time fetch on any pod with `TRIP_PLANS_ENABLED` off."""
    with _rt([_drobak()], post=_jp_realtime(_journeys()), enabled=False):
        assert trip_plans.refresh_realtime(RT_NOW) == {}
        assert alternatives._post_json.seen == [] and trip_plans._answers == {}


def test_one_realtime_record_per_tick_carries_counts_and_never_a_ref_a_stop_or_a_time():
    """R17: the record says how the tick went and never which journey, stop or time. The failing
    stub's message carries all three, so a record that echoed it would fail here."""
    leaky = RuntimeError("JP error response: NSR:StopPlace:5576 07:07 RUT:DatedServiceJourney:x")
    handler = _capture_logs()
    try:
        with _rt([_drobak()], post=_jp_realtime(_journeys(), fail={1: leaky})):
            trip_plans.refresh_realtime(RT_NOW)
            trip_plans.refresh_realtime(RT_NOW + timedelta(minutes=1))
            alternatives._fetch_calls["realtime"] = [RT_NOW + timedelta(minutes=2)] * 20
            trip_plans.refresh_realtime(RT_NOW + timedelta(minutes=2))
            trip_plans.refresh_realtime(_at(DAY, "12:00"))  # every trip has arrived
    finally:
        trip_plans.logger.removeHandler(handler)
    records = [r for r in handler.records if _fields(r).get("event") == "trip_realtime_fetch"]
    fields = [_fields(r) for r in records]
    assert [f["result"] for f in fields] == ["complete", "failed", "failed", "none"], fields
    assert [(f["failed"], f["refused"]) for f in fields] == [(0, 0), (1, 0), (0, 1), (0, 0)]
    assert fields[1]["error"] == "RuntimeError" and fields[0]["error"] is None
    assert fields[0]["held"] == 2 and fields[3]["held"] == 0, "an answer past the bound is dropped"
    assert all(isinstance(f["ms"], int) for f in fields)
    for record in records:
        text = f"{record.getMessage()} {_fields(record)!r}"
        assert "NSR:" not in text and "ServiceJourney" not in text, text
        assert not _TIME.search(text), text


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} trip-plan checks passed.")


# ---- Recorded Journey Planner replies --------------------------------------------------------
# Captured verbatim with curl 2026-10-27 against https://api.entur.io/journey-planner/v3/graphql
# (ET-Client-Name: entur-kixavvik) with `ignoreRealtimeUpdates: true` and the verification
# document, which selected everything `trip_plans._PLAN_QUERY` does plus `realtime`, the expected
# times, `authority`, `operatingDay`, `searchWindowUsed` and the place names. The extra fields are
# unread here; nothing inside a pattern was edited.
#
# Lier (NSR:StopPlace:426) -> Oslo S (NSR:StopPlace:337), travel date 2026-10-28, dateTime 07:00:
# page 1 (four patterns, three direct R13s and the 07:17 R13 -> Drammen -> R12) and the page its
# `nextPageCursor` returned.
RECORDED_LIER_P1 = (
    "{\"data\":{\"trip\":{\"nextPageCursor\":\"MnxORVhUX1BBR0V8MjAyNi0xMC0yOFQwNzozMDowMFp8fDFoN"
    "TBtfFNUUkVFVF9BTkRfQVJSSVZBTF9USU1FfHx8fHx8fA==\",\"metadata\":{\"searchWindowUsed\":90,\""
    "nextDateTime\":\"2026-10-28T08:30:00+01:00\"},\"tripPatterns\":[{\"aimedStartTime\":\"2026-1"
    "0-28T07:11:00+01:00\",\"aimedEndTime\":\"2026-10-28T07:41:00+01:00\",\"legs\":[{\"mode\":\"rai"
    "l\",\"realtime\":false,\"serviceDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T07:11:00"
    "+01:00\",\"aimedEndTime\":\"2026-10-28T07:41:00+01:00\",\"expectedStartTime\":\"2026-10-28T0"
    "7:11:00+01:00\",\"expectedEndTime\":\"2026-10-28T07:41:00+01:00\",\"line\":{\"id\":\"VYG:Line:"
    "R13\",\"publicCode\":\"R13\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id"
    "\":\"VYG:ServiceJourney:1611-SUD_766511-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServ"
    "iceJourney:1611_DRM-DAL_26-10-28\",\"operatingDay\":\"2026-10-28\"},\"fromPlace\":{\"name\":\""
    "Lier stasjon\",\"quay\":{\"id\":\"NSR:Quay:711\",\"stopPlace\":{\"id\":\"NSR:StopPlace:426\"}}},\""
    "toPlace\":{\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:571\",\"stopPlace\":{\"id\":\"NSR:StopPla"
    "ce:337\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPositionI"
    "nPattern\":2,\"aimedDepartureTime\":\"2026-10-28T07:11:00+01:00\",\"expectedDepartureTime\""
    ":\"2026-10-28T07:11:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"realtime\":fal"
    "se,\"stopPositionInPattern\":8,\"aimedArrivalTime\":\"2026-10-28T07:41:00+01:00\",\"expecte"
    "dArrivalTime\":\"2026-10-28T07:41:00+01:00\"}}]},{\"aimedStartTime\":\"2026-10-28T07:17:00"
    "+01:00\",\"aimedEndTime\":\"2026-10-28T08:05:00+01:00\",\"legs\":[{\"mode\":\"rail\",\"realtime\""
    ":false,\"serviceDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T07:17:00+01:00\",\"aime"
    "dEndTime\":\"2026-10-28T07:24:00+01:00\",\"expectedStartTime\":\"2026-10-28T07:17:00+01:00"
    "\",\"expectedEndTime\":\"2026-10-28T07:24:00+01:00\",\"line\":{\"id\":\"VYG:Line:R13\",\"publicC"
    "ode\":\"R13\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id\":\"VYG:Servic"
    "eJourney:1606_443223-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServiceJourney:1606_D"
    "AL-DRM_26-10-28\",\"operatingDay\":\"2026-10-28\"},\"fromPlace\":{\"name\":\"Lier stasjon\",\"qu"
    "ay\":{\"id\":\"NSR:Quay:712\",\"stopPlace\":{\"id\":\"NSR:StopPlace:426\"}}},\"toPlace\":{\"name\":"
    "\"Drammen stasjon\",\"quay\":{\"id\":\"NSR:Quay:18\",\"stopPlace\":{\"id\":\"NSR:StopPlace:11\"}}}"
    ",\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPositionInPattern\":"
    "15,\"aimedDepartureTime\":\"2026-10-28T07:17:00+01:00\",\"expectedDepartureTime\":\"2026-10"
    "-28T07:17:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopP"
    "ositionInPattern\":17,\"aimedArrivalTime\":\"2026-10-28T07:24:00+01:00\",\"expectedArrival"
    "Time\":\"2026-10-28T07:24:00+01:00\"}},{\"mode\":\"foot\",\"realtime\":false,\"serviceDate\":nu"
    "ll,\"aimedStartTime\":\"2026-10-28T07:24:00+01:00\",\"aimedEndTime\":\"2026-10-28T07:26:06+"
    "01:00\",\"expectedStartTime\":\"2026-10-28T07:24:00+01:00\",\"expectedEndTime\":\"2026-10-28"
    "T07:26:06+01:00\",\"line\":null,\"authority\":null,\"serviceJourney\":null,\"datedServiceJou"
    "rney\":null,\"fromPlace\":{\"name\":\"Drammen stasjon\",\"quay\":{\"id\":\"NSR:Quay:18\",\"stopPla"
    "ce\":{\"id\":\"NSR:StopPlace:11\"}}},\"toPlace\":{\"name\":\"Drammen stasjon\",\"quay\":{\"id\":\"NS"
    "R:Quay:26\",\"stopPlace\":{\"id\":\"NSR:StopPlace:11\"}}},\"fromEstimatedCall\":null,\"toEstim"
    "atedCall\":null},{\"mode\":\"rail\",\"realtime\":false,\"serviceDate\":\"2026-10-28\",\"aimedSta"
    "rtTime\":\"2026-10-28T07:30:00+01:00\",\"aimedEndTime\":\"2026-10-28T08:05:00+01:00\",\"expe"
    "ctedStartTime\":\"2026-10-28T07:30:00+01:00\",\"expectedEndTime\":\"2026-10-28T08:05:00+01"
    ":00\",\"line\":{\"id\":\"VYG:Line:R12\",\"publicCode\":\"R12\"},\"authority\":{\"id\":\"VYG:Authorit"
    "y:VY\"},\"serviceJourney\":{\"id\":\"VYG:ServiceJourney:552_906318-R\"},\"datedServiceJourne"
    "y\":{\"id\":\"VYG:DatedServiceJourney:552_DRM-EVL_26-10-28\",\"operatingDay\":\"2026-10-28\"}"
    ",\"fromPlace\":{\"name\":\"Drammen stasjon\",\"quay\":{\"id\":\"NSR:Quay:26\",\"stopPlace\":{\"id\":"
    "\"NSR:StopPlace:11\"}}},\"toPlace\":{\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:568\",\"stopPl"
    "ace\":{\"id\":\"NSR:StopPlace:337\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtim"
    "e\":false,\"stopPositionInPattern\":0,\"aimedDepartureTime\":\"2026-10-28T07:30:00+01:00\","
    "\"expectedDepartureTime\":\"2026-10-28T07:30:00+01:00\"},\"toEstimatedCall\":{\"cancellatio"
    "n\":false,\"realtime\":false,\"stopPositionInPattern\":6,\"aimedArrivalTime\":\"2026-10-28T0"
    "8:05:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T08:05:00+01:00\"}}]},{\"aimedStartTim"
    "e\":\"2026-10-28T07:41:00+01:00\",\"aimedEndTime\":\"2026-10-28T08:11:00+01:00\",\"legs\":[{\""
    "mode\":\"rail\",\"realtime\":false,\"serviceDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-2"
    "8T07:41:00+01:00\",\"aimedEndTime\":\"2026-10-28T08:11:00+01:00\",\"expectedStartTime\":\"20"
    "26-10-28T07:41:00+01:00\",\"expectedEndTime\":\"2026-10-28T08:11:00+01:00\",\"line\":{\"id\":"
    "\"VYG:Line:R13\",\"publicCode\":\"R13\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJou"
    "rney\":{\"id\":\"VYG:ServiceJourney:1613_902634-R\"},\"datedServiceJourney\":{\"id\":\"VYG:Dat"
    "edServiceJourney:1613_KBG-DAL_26-10-28\",\"operatingDay\":\"2026-10-28\"},\"fromPlace\":{\"n"
    "ame\":\"Lier stasjon\",\"quay\":{\"id\":\"NSR:Quay:711\",\"stopPlace\":{\"id\":\"NSR:StopPlace:426"
    "\"}}},\"toPlace\":{\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:571\",\"stopPlace\":{\"id\":\"NSR:S"
    "topPlace:337\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPos"
    "itionInPattern\":9,\"aimedDepartureTime\":\"2026-10-28T07:41:00+01:00\",\"expectedDepartur"
    "eTime\":\"2026-10-28T07:41:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"realtim"
    "e\":false,\"stopPositionInPattern\":15,\"aimedArrivalTime\":\"2026-10-28T08:11:00+01:00\",\""
    "expectedArrivalTime\":\"2026-10-28T08:11:00+01:00\"}}]},{\"aimedStartTime\":\"2026-10-28T0"
    "8:11:00+01:00\",\"aimedEndTime\":\"2026-10-28T08:41:00+01:00\",\"legs\":[{\"mode\":\"rail\",\"re"
    "altime\":false,\"serviceDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T08:11:00+01:00"
    "\",\"aimedEndTime\":\"2026-10-28T08:41:00+01:00\",\"expectedStartTime\":\"2026-10-28T08:11:0"
    "0+01:00\",\"expectedEndTime\":\"2026-10-28T08:41:00+01:00\",\"line\":{\"id\":\"VYG:Line:R13\",\""
    "publicCode\":\"R13\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id\":\"VYG"
    ":ServiceJourney:1615_766521-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServiceJourney"
    ":1615_DRM-DAL_26-10-28\",\"operatingDay\":\"2026-10-28\"},\"fromPlace\":{\"name\":\"Lier stasj"
    "on\",\"quay\":{\"id\":\"NSR:Quay:711\",\"stopPlace\":{\"id\":\"NSR:StopPlace:426\"}}},\"toPlace\":{"
    "\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:571\",\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}}}"
    ",\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPositionInPattern\":"
    "2,\"aimedDepartureTime\":\"2026-10-28T08:11:00+01:00\",\"expectedDepartureTime\":\"2026-10-"
    "28T08:11:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPo"
    "sitionInPattern\":8,\"aimedArrivalTime\":\"2026-10-28T08:41:00+01:00\",\"expectedArrivalTi"
    "me\":\"2026-10-28T08:41:00+01:00\"}}]}]}}}"
)
RECORDED_LIER_P1_BYTES = 6507

RECORDED_LIER_P2 = (
    "{\"data\":{\"trip\":{\"nextPageCursor\":\"MnxORVhUX1BBR0V8MjAyNi0xMC0yOFQwOToyMDowMFp8fDJoM"
    "TBtfFNUUkVFVF9BTkRfQVJSSVZBTF9USU1FfHx8fHx8fA==\",\"metadata\":{\"searchWindowUsed\":110,"
    "\"nextDateTime\":\"2026-10-28T10:20:00+01:00\"},\"tripPatterns\":[{\"aimedStartTime\":\"2026-"
    "10-28T08:41:00+01:00\",\"aimedEndTime\":\"2026-10-28T09:11:00+01:00\",\"legs\":[{\"mode\":\"ra"
    "il\",\"realtime\":false,\"serviceDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T08:41:0"
    "0+01:00\",\"aimedEndTime\":\"2026-10-28T09:11:00+01:00\",\"expectedStartTime\":\"2026-10-28T"
    "08:41:00+01:00\",\"expectedEndTime\":\"2026-10-28T09:11:00+01:00\",\"line\":{\"id\":\"VYG:Line"
    ":R13\",\"publicCode\":\"R13\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"i"
    "d\":\"VYG:ServiceJourney:1617_766524-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedService"
    "Journey:1617_DRM-DAL_26-10-28\",\"operatingDay\":\"2026-10-28\"},\"fromPlace\":{\"name\":\"Lie"
    "r stasjon\",\"quay\":{\"id\":\"NSR:Quay:711\",\"stopPlace\":{\"id\":\"NSR:StopPlace:426\"}}},\"toP"
    "lace\":{\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:571\",\"stopPlace\":{\"id\":\"NSR:StopPlace:"
    "337\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPositionInPa"
    "ttern\":2,\"aimedDepartureTime\":\"2026-10-28T08:41:00+01:00\",\"expectedDepartureTime\":\"2"
    "026-10-28T08:41:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"realtime\":false,"
    "\"stopPositionInPattern\":8,\"aimedArrivalTime\":\"2026-10-28T09:11:00+01:00\",\"expectedAr"
    "rivalTime\":\"2026-10-28T09:11:00+01:00\"}}]},{\"aimedStartTime\":\"2026-10-28T09:11:00+01"
    ":00\",\"aimedEndTime\":\"2026-10-28T09:41:00+01:00\",\"legs\":[{\"mode\":\"rail\",\"realtime\":fa"
    "lse,\"serviceDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T09:11:00+01:00\",\"aimedEn"
    "dTime\":\"2026-10-28T09:41:00+01:00\",\"expectedStartTime\":\"2026-10-28T09:11:00+01:00\",\""
    "expectedEndTime\":\"2026-10-28T09:41:00+01:00\",\"line\":{\"id\":\"VYG:Line:R13\",\"publicCode"
    "\":\"R13\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id\":\"VYG:ServiceJo"
    "urney:1619_909760-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServiceJourney:1619_DRM-"
    "DAL_26-10-28\",\"operatingDay\":\"2026-10-28\"},\"fromPlace\":{\"name\":\"Lier stasjon\",\"quay\""
    ":{\"id\":\"NSR:Quay:711\",\"stopPlace\":{\"id\":\"NSR:StopPlace:426\"}}},\"toPlace\":{\"name\":\"Os"
    "lo S\",\"quay\":{\"id\":\"NSR:Quay:571\",\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}}},\"fromEsti"
    "matedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPositionInPattern\":2,\"aimedDe"
    "partureTime\":\"2026-10-28T09:11:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T09:11:0"
    "0+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPositionInPa"
    "ttern\":8,\"aimedArrivalTime\":\"2026-10-28T09:41:00+01:00\",\"expectedArrivalTime\":\"2026-"
    "10-28T09:41:00+01:00\"}}]},{\"aimedStartTime\":\"2026-10-28T09:41:00+01:00\",\"aimedEndTim"
    "e\":\"2026-10-28T10:11:00+01:00\",\"legs\":[{\"mode\":\"rail\",\"realtime\":false,\"serviceDate\""
    ":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T09:41:00+01:00\",\"aimedEndTime\":\"2026-10-2"
    "8T10:11:00+01:00\",\"expectedStartTime\":\"2026-10-28T09:41:00+01:00\",\"expectedEndTime\":"
    "\"2026-10-28T10:11:00+01:00\",\"line\":{\"id\":\"VYG:Line:R13\",\"publicCode\":\"R13\"},\"authori"
    "ty\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id\":\"VYG:ServiceJourney:1621_913868"
    "-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServiceJourney:1621_DRM-DAL_26-10-28\",\"op"
    "eratingDay\":\"2026-10-28\"},\"fromPlace\":{\"name\":\"Lier stasjon\",\"quay\":{\"id\":\"NSR:Quay:"
    "711\",\"stopPlace\":{\"id\":\"NSR:StopPlace:426\"}}},\"toPlace\":{\"name\":\"Oslo S\",\"quay\":{\"id"
    "\":\"NSR:Quay:571\",\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}}},\"fromEstimatedCall\":{\"canc"
    "ellation\":false,\"realtime\":false,\"stopPositionInPattern\":2,\"aimedDepartureTime\":\"202"
    "6-10-28T09:41:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T09:41:00+01:00\"},\"toEsti"
    "matedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPositionInPattern\":8,\"aimedAr"
    "rivalTime\":\"2026-10-28T10:11:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T10:11:00+01"
    ":00\"}}]},{\"aimedStartTime\":\"2026-10-28T10:11:00+01:00\",\"aimedEndTime\":\"2026-10-28T10"
    ":41:00+01:00\",\"legs\":[{\"mode\":\"rail\",\"realtime\":false,\"serviceDate\":\"2026-10-28\",\"ai"
    "medStartTime\":\"2026-10-28T10:11:00+01:00\",\"aimedEndTime\":\"2026-10-28T10:41:00+01:00\""
    ",\"expectedStartTime\":\"2026-10-28T10:11:00+01:00\",\"expectedEndTime\":\"2026-10-28T10:41"
    ":00+01:00\",\"line\":{\"id\":\"VYG:Line:R13\",\"publicCode\":\"R13\"},\"authority\":{\"id\":\"VYG:Au"
    "thority:VY\"},\"serviceJourney\":{\"id\":\"VYG:ServiceJourney:1623_766534-R\"},\"datedServic"
    "eJourney\":{\"id\":\"VYG:DatedServiceJourney:1623_DRM-DAL_26-10-28\",\"operatingDay\":\"2026"
    "-10-28\"},\"fromPlace\":{\"name\":\"Lier stasjon\",\"quay\":{\"id\":\"NSR:Quay:711\",\"stopPlace\":"
    "{\"id\":\"NSR:StopPlace:426\"}}},\"toPlace\":{\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:571\","
    "\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\""
    "realtime\":false,\"stopPositionInPattern\":2,\"aimedDepartureTime\":\"2026-10-28T10:11:00+"
    "01:00\",\"expectedDepartureTime\":\"2026-10-28T10:11:00+01:00\"},\"toEstimatedCall\":{\"canc"
    "ellation\":false,\"realtime\":false,\"stopPositionInPattern\":8,\"aimedArrivalTime\":\"2026-"
    "10-28T10:41:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T10:41:00+01:00\"}}]}]}}}"
)
RECORDED_LIER_P2_BYTES = 4867

# Drøbak torg (NSR:StopPlace:5576) -> Asker (NSR:StopPlace:59616), travel date 2026-10-28: RUT
# bus 500 then VYG rail. Truncated to the first 2 of 12 tripPatterns, the two continuations of the
# 07:07 bus (via bus 81 and Lysaker, and on foot via Oslo S); the cursor and metadata are page 1's
# as sent.
RECORDED_DROBAK = (
    "{\"data\":{\"trip\":{\"nextPageCursor\":\"MnxORVhUX1BBR0V8MjAyNi0xMC0yOFQwNjo1NzowMFp8fDFof"
    "FNUUkVFVF9BTkRfQVJSSVZBTF9USU1FfGZhbHNlfDIwMjYtMTAtMjhUMDY6NTc6MDBafDIwMjYtMTAtMjhUM"
    "Dg6MTk6MDBafDJ8NzIyOXx8\",\"metadata\":{\"searchWindowUsed\":100,\"nextDateTime\":\"2026-10-"
    "28T07:58:00+01:00\"},\"tripPatterns\":[{\"aimedStartTime\":\"2026-10-28T07:07:00+01:00\",\"a"
    "imedEndTime\":\"2026-10-28T08:29:00+01:00\",\"legs\":[{\"mode\":\"bus\",\"realtime\":false,\"ser"
    "viceDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T07:07:00+01:00\",\"aimedEndTime\":\""
    "2026-10-28T07:53:00+01:00\",\"expectedStartTime\":\"2026-10-28T07:07:00+01:00\",\"expected"
    "EndTime\":\"2026-10-28T07:53:00+01:00\",\"line\":{\"id\":\"RUT:Line:500\",\"publicCode\":\"500\"}"
    ",\"authority\":{\"id\":\"RUT:Authority:RUT\"},\"serviceJourney\":{\"id\":\"RUT:ServiceJourney:a"
    "bca14c8fae00bc75a7766a018b3a528\"},\"datedServiceJourney\":{\"id\":\"RUT:DatedServiceJourn"
    "ey:djj-788b507489c0740b5e252cd34c39f14c\",\"operatingDay\":\"2026-10-28\"},\"fromPlace\":{\""
    "name\":\"Drøbak torg\",\"quay\":{\"id\":\"NSR:Quay:10207\",\"stopPlace\":{\"id\":\"NSR:StopPlace:5"
    "576\"}}},\"toPlace\":{\"name\":\"Bjørvika\",\"quay\":{\"id\":\"NSR:Quay:7170\",\"stopPlace\":{\"id\":"
    "\"NSR:StopPlace:3983\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\""
    "stopPositionInPattern\":2,\"aimedDepartureTime\":\"2026-10-28T07:07:00+01:00\",\"expectedD"
    "epartureTime\":\"2026-10-28T07:07:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\""
    "realtime\":false,\"stopPositionInPattern\":25,\"aimedArrivalTime\":\"2026-10-28T07:53:00+0"
    "1:00\",\"expectedArrivalTime\":\"2026-10-28T07:53:00+01:00\"}},{\"mode\":\"bus\",\"realtime\":f"
    "alse,\"serviceDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T07:55:00+01:00\",\"aimedE"
    "ndTime\":\"2026-10-28T08:15:00+01:00\",\"expectedStartTime\":\"2026-10-28T07:55:00+01:00\","
    "\"expectedEndTime\":\"2026-10-28T08:15:00+01:00\",\"line\":{\"id\":\"RUT:Line:81\",\"publicCode"
    "\":\"81\"},\"authority\":{\"id\":\"RUT:Authority:RUT\"},\"serviceJourney\":{\"id\":\"RUT:ServiceJo"
    "urney:0744f4f78e16a93b93f12fde886fae17\"},\"datedServiceJourney\":{\"id\":\"RUT:DatedServi"
    "ceJourney:djj-8423077639bdf4ebcc01dea73f9a62a9\",\"operatingDay\":\"2026-10-28\"},\"fromPl"
    "ace\":{\"name\":\"Bjørvika\",\"quay\":{\"id\":\"NSR:Quay:7170\",\"stopPlace\":{\"id\":\"NSR:StopPlac"
    "e:3983\"}}},\"toPlace\":{\"name\":\"Lysaker stasjon\",\"quay\":{\"id\":\"NSR:Quay:7897\",\"stopPla"
    "ce\":{\"id\":\"NSR:StopPlace:4377\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtim"
    "e\":false,\"stopPositionInPattern\":30,\"aimedDepartureTime\":\"2026-10-28T07:55:00+01:00\""
    ",\"expectedDepartureTime\":\"2026-10-28T07:55:00+01:00\"},\"toEstimatedCall\":{\"cancellati"
    "on\":false,\"realtime\":false,\"stopPositionInPattern\":38,\"aimedArrivalTime\":\"2026-10-28"
    "T08:15:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T08:15:00+01:00\"}},{\"mode\":\"foot\","
    "\"realtime\":false,\"serviceDate\":null,\"aimedStartTime\":\"2026-10-28T08:15:00+01:00\",\"ai"
    "medEndTime\":\"2026-10-28T08:16:40+01:00\",\"expectedStartTime\":\"2026-10-28T08:15:00+01:"
    "00\",\"expectedEndTime\":\"2026-10-28T08:16:40+01:00\",\"line\":null,\"authority\":null,\"serv"
    "iceJourney\":null,\"datedServiceJourney\":null,\"fromPlace\":{\"name\":\"Lysaker stasjon\",\"q"
    "uay\":{\"id\":\"NSR:Quay:7897\",\"stopPlace\":{\"id\":\"NSR:StopPlace:4377\"}}},\"toPlace\":{\"nam"
    "e\":\"Lysaker stasjon\",\"quay\":{\"id\":\"NSR:Quay:248\",\"stopPlace\":{\"id\":\"NSR:StopPlace:15"
    "7\"}}},\"fromEstimatedCall\":null,\"toEstimatedCall\":null},{\"mode\":\"rail\",\"realtime\":fal"
    "se,\"serviceDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T08:19:00+01:00\",\"aimedEnd"
    "Time\":\"2026-10-28T08:29:00+01:00\",\"expectedStartTime\":\"2026-10-28T08:19:00+01:00\",\"e"
    "xpectedEndTime\":\"2026-10-28T08:29:00+01:00\",\"line\":{\"id\":\"VYG:Line:R12\",\"publicCode\""
    ":\"R12\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id\":\"VYG:ServiceJou"
    "rney:507_906236-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServiceJourney:507_EVL-KBG"
    "_26-10-28\",\"operatingDay\":\"2026-10-28\"},\"fromPlace\":{\"name\":\"Lysaker stasjon\",\"quay\""
    ":{\"id\":\"NSR:Quay:248\",\"stopPlace\":{\"id\":\"NSR:StopPlace:157\"}}},\"toPlace\":{\"name\":\"As"
    "ker stasjon\",\"quay\":{\"id\":\"NSR:Quay:698\",\"stopPlace\":{\"id\":\"NSR:StopPlace:418\"}}},\"f"
    "romEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPositionInPattern\":7,\""
    "aimedDepartureTime\":\"2026-10-28T08:19:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T"
    "08:19:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPosit"
    "ionInPattern\":9,\"aimedArrivalTime\":\"2026-10-28T08:29:00+01:00\",\"expectedArrivalTime\""
    ":\"2026-10-28T08:29:00+01:00\"}}]},{\"aimedStartTime\":\"2026-10-28T07:07:00+01:00\",\"aime"
    "dEndTime\":\"2026-10-28T08:29:00+01:00\",\"legs\":[{\"mode\":\"bus\",\"realtime\":false,\"servic"
    "eDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T07:07:00+01:00\",\"aimedEndTime\":\"202"
    "6-10-28T07:55:00+01:00\",\"expectedStartTime\":\"2026-10-28T07:07:00+01:00\",\"expectedEnd"
    "Time\":\"2026-10-28T07:55:00+01:00\",\"line\":{\"id\":\"RUT:Line:500\",\"publicCode\":\"500\"},\"a"
    "uthority\":{\"id\":\"RUT:Authority:RUT\"},\"serviceJourney\":{\"id\":\"RUT:ServiceJourney:abca"
    "14c8fae00bc75a7766a018b3a528\"},\"datedServiceJourney\":{\"id\":\"RUT:DatedServiceJourney:"
    "djj-788b507489c0740b5e252cd34c39f14c\",\"operatingDay\":\"2026-10-28\"},\"fromPlace\":{\"nam"
    "e\":\"Drøbak torg\",\"quay\":{\"id\":\"NSR:Quay:10207\",\"stopPlace\":{\"id\":\"NSR:StopPlace:5576"
    "\"}}},\"toPlace\":{\"name\":\"Oslo bussterminal\",\"quay\":{\"id\":\"NSR:Quay:11971\",\"stopPlace\""
    ":{\"id\":\"NSR:StopPlace:6505\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":"
    "false,\"stopPositionInPattern\":2,\"aimedDepartureTime\":\"2026-10-28T07:07:00+01:00\",\"ex"
    "pectedDepartureTime\":\"2026-10-28T07:07:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":"
    "false,\"realtime\":false,\"stopPositionInPattern\":26,\"aimedArrivalTime\":\"2026-10-28T07:"
    "55:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:55:00+01:00\"}},{\"mode\":\"foot\",\"rea"
    "ltime\":false,\"serviceDate\":null,\"aimedStartTime\":\"2026-10-28T07:55:00+01:00\",\"aimedE"
    "ndTime\":\"2026-10-28T08:02:44+01:00\",\"expectedStartTime\":\"2026-10-28T07:55:00+01:00\","
    "\"expectedEndTime\":\"2026-10-28T08:02:44+01:00\",\"line\":null,\"authority\":null,\"serviceJ"
    "ourney\":null,\"datedServiceJourney\":null,\"fromPlace\":{\"name\":\"Oslo bussterminal\",\"qua"
    "y\":{\"id\":\"NSR:Quay:11971\",\"stopPlace\":{\"id\":\"NSR:StopPlace:6505\"}}},\"toPlace\":{\"name"
    "\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:559\",\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}}},\"fro"
    "mEstimatedCall\":null,\"toEstimatedCall\":null},{\"mode\":\"rail\",\"realtime\":false,\"servic"
    "eDate\":\"2026-10-28\",\"aimedStartTime\":\"2026-10-28T08:09:00+01:00\",\"aimedEndTime\":\"202"
    "6-10-28T08:29:00+01:00\",\"expectedStartTime\":\"2026-10-28T08:09:00+01:00\",\"expectedEnd"
    "Time\":\"2026-10-28T08:29:00+01:00\",\"line\":{\"id\":\"VYG:Line:R12\",\"publicCode\":\"R12\"},\"a"
    "uthority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id\":\"VYG:ServiceJourney:507_9"
    "06236-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServiceJourney:507_EVL-KBG_26-10-28\""
    ",\"operatingDay\":\"2026-10-28\"},\"fromPlace\":{\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:55"
    "9\",\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}}},\"toPlace\":{\"name\":\"Asker stasjon\",\"quay\""
    ":{\"id\":\"NSR:Quay:698\",\"stopPlace\":{\"id\":\"NSR:StopPlace:418\"}}},\"fromEstimatedCall\":{"
    "\"cancellation\":false,\"realtime\":false,\"stopPositionInPattern\":4,\"aimedDepartureTime\""
    ":\"2026-10-28T08:09:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T08:09:00+01:00\"},\"t"
    "oEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPositionInPattern\":9,\"ai"
    "medArrivalTime\":\"2026-10-28T08:29:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T08:29:"
    "00+01:00\"}}]}]}}}"
)
RECORDED_DROBAK_BYTES = 7077

# Kolbotn (NSR:StopPlace:572) -> Skøyen (NSR:StopPlace:152), 2026-10-27, dateTime 21:05, all five
# patterns: VYG:ServiceJourney:2868-SKI_660212-R (the 21:10 L2) was cancelled at every call that
# evening and is still here, with `cancellation: false`.
RECORDED_KOLBOTN = (
    "{\"data\":{\"trip\":{\"nextPageCursor\":\"MnxORVhUX1BBR0V8MjAyNi0xMC0yN1QyMToyNTowMFp8fDFoM"
    "zBtfFNUUkVFVF9BTkRfQVJSSVZBTF9USU1FfHx8fHx8fA==\",\"metadata\":{\"searchWindowUsed\":80,\""
    "nextDateTime\":\"2026-10-27T22:25:00+01:00\"},\"tripPatterns\":[{\"aimedStartTime\":\"2026-1"
    "0-27T21:10:00+01:00\",\"aimedEndTime\":\"2026-10-27T21:36:00+01:00\",\"legs\":[{\"mode\":\"rai"
    "l\",\"realtime\":false,\"serviceDate\":\"2026-10-27\",\"aimedStartTime\":\"2026-10-27T21:10:00"
    "+01:00\",\"aimedEndTime\":\"2026-10-27T21:36:00+01:00\",\"expectedStartTime\":\"2026-10-27T2"
    "1:10:00+01:00\",\"expectedEndTime\":\"2026-10-27T21:36:00+01:00\",\"line\":{\"id\":\"VYG:Line:"
    "L2\",\"publicCode\":\"L2\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id\":"
    "\"VYG:ServiceJourney:2868-SKI_660212-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServic"
    "eJourney:2868_SKI-SKOe_26-10-27\",\"operatingDay\":\"2026-10-27\"},\"fromPlace\":{\"name\":\"K"
    "olbotn stasjon\",\"quay\":{\"id\":\"NSR:Quay:935\",\"stopPlace\":{\"id\":\"NSR:StopPlace:572\"}}}"
    ",\"toPlace\":{\"name\":\"Skøyen stasjon\",\"quay\":{\"id\":\"NSR:Quay:237\",\"stopPlace\":{\"id\":\"N"
    "SR:StopPlace:152\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"sto"
    "pPositionInPattern\":7,\"aimedDepartureTime\":\"2026-10-27T21:10:00+01:00\",\"expectedDepa"
    "rtureTime\":\"2026-10-27T21:10:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"rea"
    "ltime\":false,\"stopPositionInPattern\":15,\"aimedArrivalTime\":\"2026-10-27T21:36:00+01:0"
    "0\",\"expectedArrivalTime\":\"2026-10-27T21:36:00+01:00\"}}]},{\"aimedStartTime\":\"2026-10-"
    "27T21:25:00+01:00\",\"aimedEndTime\":\"2026-10-27T21:52:00+01:00\",\"legs\":[{\"mode\":\"rail\""
    ",\"realtime\":false,\"serviceDate\":\"2026-10-27\",\"aimedStartTime\":\"2026-10-27T21:25:00+0"
    "1:00\",\"aimedEndTime\":\"2026-10-27T21:52:00+01:00\",\"expectedStartTime\":\"2026-10-27T21:"
    "25:00+01:00\",\"expectedEndTime\":\"2026-10-27T21:52:00+01:00\",\"line\":{\"id\":\"VYG:Line:L2"
    "\",\"publicCode\":\"L2\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id\":\"V"
    "YG:ServiceJourney:2770-SKI_443990-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServiceJ"
    "ourney:2770_SKI-STB_26-10-27\",\"operatingDay\":\"2026-10-27\"},\"fromPlace\":{\"name\":\"Kolb"
    "otn stasjon\",\"quay\":{\"id\":\"NSR:Quay:935\",\"stopPlace\":{\"id\":\"NSR:StopPlace:572\"}}},\"t"
    "oPlace\":{\"name\":\"Skøyen stasjon\",\"quay\":{\"id\":\"NSR:Quay:237\",\"stopPlace\":{\"id\":\"NSR:"
    "StopPlace:152\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPo"
    "sitionInPattern\":7,\"aimedDepartureTime\":\"2026-10-27T21:25:00+01:00\",\"expectedDepartu"
    "reTime\":\"2026-10-27T21:25:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"realti"
    "me\":false,\"stopPositionInPattern\":15,\"aimedArrivalTime\":\"2026-10-27T21:52:00+01:00\","
    "\"expectedArrivalTime\":\"2026-10-27T21:52:00+01:00\"}}]},{\"aimedStartTime\":\"2026-10-27T"
    "21:40:00+01:00\",\"aimedEndTime\":\"2026-10-27T22:12:00+01:00\",\"legs\":[{\"mode\":\"rail\",\"r"
    "ealtime\":false,\"serviceDate\":\"2026-10-27\",\"aimedStartTime\":\"2026-10-27T21:40:00+01:0"
    "0\",\"aimedEndTime\":\"2026-10-27T21:59:00+01:00\",\"expectedStartTime\":\"2026-10-27T21:40:"
    "00+01:00\",\"expectedEndTime\":\"2026-10-27T21:59:00+01:00\",\"line\":{\"id\":\"VYG:Line:L2\",\""
    "publicCode\":\"L2\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id\":\"VYG:"
    "ServiceJourney:2870_444114-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServiceJourney:"
    "2870_SKI-OSL_26-10-27\",\"operatingDay\":\"2026-10-27\"},\"fromPlace\":{\"name\":\"Kolbotn sta"
    "sjon\",\"quay\":{\"id\":\"NSR:Quay:935\",\"stopPlace\":{\"id\":\"NSR:StopPlace:572\"}}},\"toPlace\""
    ":{\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:556\",\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}"
    "}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stopPositionInPattern"
    "\":7,\"aimedDepartureTime\":\"2026-10-27T21:40:00+01:00\",\"expectedDepartureTime\":\"2026-1"
    "0-27T21:40:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"stop"
    "PositionInPattern\":13,\"aimedArrivalTime\":\"2026-10-27T21:59:00+01:00\",\"expectedArriva"
    "lTime\":\"2026-10-27T21:59:00+01:00\"}},{\"mode\":\"foot\",\"realtime\":false,\"serviceDate\":n"
    "ull,\"aimedStartTime\":\"2026-10-27T21:59:00+01:00\",\"aimedEndTime\":\"2026-10-27T22:01:28"
    "+01:00\",\"expectedStartTime\":\"2026-10-27T21:59:00+01:00\",\"expectedEndTime\":\"2026-10-2"
    "7T22:01:28+01:00\",\"line\":null,\"authority\":null,\"serviceJourney\":null,\"datedServiceJo"
    "urney\":null,\"fromPlace\":{\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:556\",\"stopPlace\":{\"i"
    "d\":\"NSR:StopPlace:337\"}}},\"toPlace\":{\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:561\",\"st"
    "opPlace\":{\"id\":\"NSR:StopPlace:337\"}}},\"fromEstimatedCall\":null,\"toEstimatedCall\":nul"
    "l},{\"mode\":\"rail\",\"realtime\":false,\"serviceDate\":\"2026-10-27\",\"aimedStartTime\":\"2026"
    "-10-27T22:07:00+01:00\",\"aimedEndTime\":\"2026-10-27T22:12:00+01:00\",\"expectedStartTime"
    "\":\"2026-10-27T22:07:00+01:00\",\"expectedEndTime\":\"2026-10-27T22:12:00+01:00\",\"line\":{"
    "\"id\":\"VYG:Line:R21\",\"publicCode\":\"R21\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"servi"
    "ceJourney\":{\"id\":\"VYG:ServiceJourney:1170-MOS_443066-R\"},\"datedServiceJourney\":{\"id\""
    ":\"VYG:DatedServiceJourney:1170_MOS-STB_26-10-27\",\"operatingDay\":\"2026-10-27\"},\"fromP"
    "lace\":{\"name\":\"Oslo S\",\"quay\":{\"id\":\"NSR:Quay:561\",\"stopPlace\":{\"id\":\"NSR:StopPlace:"
    "337\"}}},\"toPlace\":{\"name\":\"Skøyen stasjon\",\"quay\":{\"id\":\"NSR:Quay:237\",\"stopPlace\":{"
    "\"id\":\"NSR:StopPlace:152\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":fal"
    "se,\"stopPositionInPattern\":6,\"aimedDepartureTime\":\"2026-10-27T22:07:00+01:00\",\"expec"
    "tedDepartureTime\":\"2026-10-27T22:07:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":fal"
    "se,\"realtime\":false,\"stopPositionInPattern\":8,\"aimedArrivalTime\":\"2026-10-27T22:12:0"
    "0+01:00\",\"expectedArrivalTime\":\"2026-10-27T22:12:00+01:00\"}}]},{\"aimedStartTime\":\"20"
    "26-10-27T21:55:00+01:00\",\"aimedEndTime\":\"2026-10-27T22:22:00+01:00\",\"legs\":[{\"mode\":"
    "\"rail\",\"realtime\":false,\"serviceDate\":\"2026-10-27\",\"aimedStartTime\":\"2026-10-27T21:5"
    "5:00+01:00\",\"aimedEndTime\":\"2026-10-27T22:22:00+01:00\",\"expectedStartTime\":\"2026-10-"
    "27T21:55:00+01:00\",\"expectedEndTime\":\"2026-10-27T22:22:00+01:00\",\"line\":{\"id\":\"VYG:L"
    "ine:L2\",\"publicCode\":\"L2\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\""
    "id\":\"VYG:ServiceJourney:2772-SKI_443993-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedSe"
    "rviceJourney:2772_SKI-STB_26-10-27\",\"operatingDay\":\"2026-10-27\"},\"fromPlace\":{\"name\""
    ":\"Kolbotn stasjon\",\"quay\":{\"id\":\"NSR:Quay:935\",\"stopPlace\":{\"id\":\"NSR:StopPlace:572\""
    "}}},\"toPlace\":{\"name\":\"Skøyen stasjon\",\"quay\":{\"id\":\"NSR:Quay:237\",\"stopPlace\":{\"id\""
    ":\"NSR:StopPlace:152\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\""
    "stopPositionInPattern\":7,\"aimedDepartureTime\":\"2026-10-27T21:55:00+01:00\",\"expectedD"
    "epartureTime\":\"2026-10-27T21:55:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\""
    "realtime\":false,\"stopPositionInPattern\":15,\"aimedArrivalTime\":\"2026-10-27T22:22:00+0"
    "1:00\",\"expectedArrivalTime\":\"2026-10-27T22:22:00+01:00\"}}]},{\"aimedStartTime\":\"2026-"
    "10-27T22:10:00+01:00\",\"aimedEndTime\":\"2026-10-27T22:36:00+01:00\",\"legs\":[{\"mode\":\"ra"
    "il\",\"realtime\":false,\"serviceDate\":\"2026-10-27\",\"aimedStartTime\":\"2026-10-27T22:10:0"
    "0+01:00\",\"aimedEndTime\":\"2026-10-27T22:36:00+01:00\",\"expectedStartTime\":\"2026-10-27T"
    "22:10:00+01:00\",\"expectedEndTime\":\"2026-10-27T22:36:00+01:00\",\"line\":{\"id\":\"VYG:Line"
    ":L2\",\"publicCode\":\"L2\"},\"authority\":{\"id\":\"VYG:Authority:VY\"},\"serviceJourney\":{\"id\""
    ":\"VYG:ServiceJourney:2872-SKI_660214-R\"},\"datedServiceJourney\":{\"id\":\"VYG:DatedServi"
    "ceJourney:2872_SKI-SKOe_26-10-27\",\"operatingDay\":\"2026-10-27\"},\"fromPlace\":{\"name\":\""
    "Kolbotn stasjon\",\"quay\":{\"id\":\"NSR:Quay:935\",\"stopPlace\":{\"id\":\"NSR:StopPlace:572\"}}"
    "},\"toPlace\":{\"name\":\"Skøyen stasjon\",\"quay\":{\"id\":\"NSR:Quay:237\",\"stopPlace\":{\"id\":\""
    "NSR:StopPlace:152\"}}},\"fromEstimatedCall\":{\"cancellation\":false,\"realtime\":false,\"st"
    "opPositionInPattern\":7,\"aimedDepartureTime\":\"2026-10-27T22:10:00+01:00\",\"expectedDep"
    "artureTime\":\"2026-10-27T22:10:00+01:00\"},\"toEstimatedCall\":{\"cancellation\":false,\"re"
    "altime\":false,\"stopPositionInPattern\":15,\"aimedArrivalTime\":\"2026-10-27T22:36:00+01:"
    "00\",\"expectedArrivalTime\":\"2026-10-27T22:36:00+01:00\"}}]}]}}}"
)
RECORDED_KOLBOTN_BYTES = 7710

# The batched real-time document (U4), captured verbatim with curl 2026-10-27 23:25 against the
# same endpoint and client name: `datedServiceJourney` by JP's full id under aliases, the selection
# `trip_plans._REALTIME_LEG` sends. l0 is RECORDED_DROBAK's bus 500 (27 calls, `realtime: false` on
# every one: JP copied the aimed times for the next morning's bus), l1 its R12 507 (17 calls,
# `realtime: true`), l2 an id JP does not know (asked for `id` only), which came back null with no
# `errors`. Not trimmed.
RECORDED_REALTIME = (
    "{\"data\":{\"l0\":{\"id\":\"RUT:DatedServiceJourney:djj-788b507489c0740b5e252cd34c39f14c\",\"oper"
    "atingDay\":\"2026-10-28\",\"estimatedCalls\":[{\"stopPositionInPattern\":0,\"realtime\":false,\"ca"
    "ncellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:05:00+01:00\",\"expectedArrivalTime\":\""
    "2026-10-28T07:05:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:05:00+01:00\",\"expectedDep"
    "artureTime\":\"2026-10-28T07:05:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5566\"}}"
    "},{\"stopPositionInPattern\":1,\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2"
    "026-10-28T07:06:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:06:00+01:00\",\"aimedDepart"
    "ureTime\":\"2026-10-28T07:06:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:06:00+01:00\""
    ",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5578\"}}},{\"stopPositionInPattern\":2,\"realtime\""
    ":false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:07:00+01:00\",\"expectedArri"
    "valTime\":\"2026-10-28T07:07:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:07:00+01:00\",\"e"
    "xpectedDepartureTime\":\"2026-10-28T07:07:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPla"
    "ce:5576\"}}},{\"stopPositionInPattern\":3,\"realtime\":false,\"cancellation\":false,\"aimedArriv"
    "alTime\":\"2026-10-28T07:09:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:09:00+01:00\",\"a"
    "imedDepartureTime\":\"2026-10-28T07:09:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:09"
    ":00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5561\"}}},{\"stopPositionInPattern\":4,"
    "\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:10:00+01:00\",\"ex"
    "pectedArrivalTime\":\"2026-10-28T07:10:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:10:00"
    "+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:10:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"N"
    "SR:StopPlace:5398\"}}},{\"stopPositionInPattern\":5,\"realtime\":false,\"cancellation\":false,\""
    "aimedArrivalTime\":\"2026-10-28T07:11:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:11:00"
    "+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:11:00+01:00\",\"expectedDepartureTime\":\"2026-1"
    "0-28T07:11:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5564\"}}},{\"stopPositionInP"
    "attern\":6,\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:12:00+"
    "01:00\",\"expectedArrivalTime\":\"2026-10-28T07:12:00+01:00\",\"aimedDepartureTime\":\"2026-10-2"
    "8T07:12:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:12:00+01:00\",\"quay\":{\"stopPlace"
    "\":{\"id\":\"NSR:StopPlace:5556\"}}},{\"stopPositionInPattern\":7,\"realtime\":false,\"cancellatio"
    "n\":false,\"aimedArrivalTime\":\"2026-10-28T07:14:00+01:00\",\"expectedArrivalTime\":\"2026-10-2"
    "8T07:14:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:14:00+01:00\",\"expectedDepartureTim"
    "e\":\"2026-10-28T07:14:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5551\"}}},{\"stopP"
    "ositionInPattern\":8,\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28"
    "T07:15:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:15:00+01:00\",\"aimedDepartureTime\":"
    "\"2026-10-28T07:15:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:15:00+01:00\",\"quay\":{"
    "\"stopPlace\":{\"id\":\"NSR:StopPlace:5496\"}}},{\"stopPositionInPattern\":9,\"realtime\":false,\"c"
    "ancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:16:00+01:00\",\"expectedArrivalTime\":"
    "\"2026-10-28T07:16:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:16:00+01:00\",\"expectedDe"
    "partureTime\":\"2026-10-28T07:16:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5546\"}"
    "}},{\"stopPositionInPattern\":10,\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":"
    "\"2026-10-28T07:18:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:18:00+01:00\",\"aimedDepa"
    "rtureTime\":\"2026-10-28T07:18:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:18:00+01:0"
    "0\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5532\"}}},{\"stopPositionInPattern\":11,\"realti"
    "me\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:19:00+01:00\",\"expectedA"
    "rrivalTime\":\"2026-10-28T07:19:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:19:00+01:00\""
    ",\"expectedDepartureTime\":\"2026-10-28T07:19:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:Stop"
    "Place:5530\"}}},{\"stopPositionInPattern\":12,\"realtime\":false,\"cancellation\":false,\"aimedA"
    "rrivalTime\":\"2026-10-28T07:19:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:19:00+01:00"
    "\",\"aimedDepartureTime\":\"2026-10-28T07:19:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T0"
    "7:19:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5516\"}}},{\"stopPositionInPattern"
    "\":13,\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:22:00+01:00"
    "\",\"expectedArrivalTime\":\"2026-10-28T07:22:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:"
    "22:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:22:00+01:00\",\"quay\":{\"stopPlace\":{\"i"
    "d\":\"NSR:StopPlace:5513\"}}},{\"stopPositionInPattern\":14,\"realtime\":false,\"cancellation\":f"
    "alse,\"aimedArrivalTime\":\"2026-10-28T07:24:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07"
    ":24:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:24:00+01:00\",\"expectedDepartureTime\":\""
    "2026-10-28T07:24:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:5441\"}}},{\"stopPosit"
    "ionInPattern\":15,\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07"
    ":41:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:41:00+01:00\",\"aimedDepartureTime\":\"20"
    "26-10-28T07:41:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:41:00+01:00\",\"quay\":{\"st"
    "opPlace\":{\"id\":\"NSR:StopPlace:6666\"}}},{\"stopPositionInPattern\":16,\"realtime\":false,\"can"
    "cellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:42:00+01:00\",\"expectedArrivalTime\":\"2"
    "026-10-28T07:42:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:42:00+01:00\",\"expectedDepa"
    "rtureTime\":\"2026-10-28T07:42:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:6664\"}}}"
    ",{\"stopPositionInPattern\":17,\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2"
    "026-10-28T07:42:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:42:00+01:00\",\"aimedDepart"
    "ureTime\":\"2026-10-28T07:42:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:42:00+01:00\""
    ",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:6663\"}}},{\"stopPositionInPattern\":18,\"realtime"
    "\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:43:00+01:00\",\"expectedArr"
    "ivalTime\":\"2026-10-28T07:43:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:43:00+01:00\",\""
    "expectedDepartureTime\":\"2026-10-28T07:43:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPl"
    "ace:6662\"}}},{\"stopPositionInPattern\":19,\"realtime\":false,\"cancellation\":false,\"aimedArr"
    "ivalTime\":\"2026-10-28T07:44:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:44:00+01:00\","
    "\"aimedDepartureTime\":\"2026-10-28T07:44:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:"
    "44:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:6660\"}}},{\"stopPositionInPattern\":"
    "20,\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:45:00+01:00\","
    "\"expectedArrivalTime\":\"2026-10-28T07:45:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:45"
    ":00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:45:00+01:00\",\"quay\":{\"stopPlace\":{\"id\""
    ":\"NSR:StopPlace:6659\"}}},{\"stopPositionInPattern\":21,\"realtime\":false,\"cancellation\":fal"
    "se,\"aimedArrivalTime\":\"2026-10-28T07:46:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:4"
    "6:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:46:00+01:00\",\"expectedDepartureTime\":\"20"
    "26-10-28T07:46:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:6610\"}}},{\"stopPositio"
    "nInPattern\":22,\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:4"
    "7:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:47:00+01:00\",\"aimedDepartureTime\":\"2026"
    "-10-28T07:47:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:47:00+01:00\",\"quay\":{\"stop"
    "Place\":{\"id\":\"NSR:StopPlace:6606\"}}},{\"stopPositionInPattern\":23,\"realtime\":false,\"cance"
    "llation\":false,\"aimedArrivalTime\":\"2026-10-28T07:48:00+01:00\",\"expectedArrivalTime\":\"202"
    "6-10-28T07:48:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:48:00+01:00\",\"expectedDepart"
    "ureTime\":\"2026-10-28T07:48:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:6603\"}}},{"
    "\"stopPositionInPattern\":24,\"realtime\":false,\"cancellation\":false,\"aimedArrivalTime\":\"202"
    "6-10-28T07:49:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:49:00+01:00\",\"aimedDepartur"
    "eTime\":\"2026-10-28T07:49:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:49:00+01:00\",\""
    "quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:6578\"}}},{\"stopPositionInPattern\":25,\"realtime\":"
    "false,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:53:00+01:00\",\"expectedArriv"
    "alTime\":\"2026-10-28T07:53:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:53:00+01:00\",\"ex"
    "pectedDepartureTime\":\"2026-10-28T07:53:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlac"
    "e:3983\"}}},{\"stopPositionInPattern\":26,\"realtime\":false,\"cancellation\":false,\"aimedArriv"
    "alTime\":\"2026-10-28T07:55:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:55:00+01:00\",\"a"
    "imedDepartureTime\":\"2026-10-28T07:55:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:55"
    ":00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:6505\"}}}]},\"l1\":{\"id\":\"VYG:DatedServ"
    "iceJourney:507_EVL-KBG_26-10-28\",\"operatingDay\":\"2026-10-28\",\"estimatedCalls\":[{\"stopPos"
    "itionInPattern\":0,\"realtime\":true,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07"
    ":31:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:31:00+01:00\",\"aimedDepartureTime\":\"20"
    "26-10-28T07:31:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:31:00+01:00\",\"quay\":{\"st"
    "opPlace\":{\"id\":\"NSR:StopPlace:159\"}}},{\"stopPositionInPattern\":1,\"realtime\":true,\"cancel"
    "lation\":false,\"aimedArrivalTime\":\"2026-10-28T07:35:00+01:00\",\"expectedArrivalTime\":\"2026"
    "-10-28T07:35:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:35:00+01:00\",\"expectedDepartu"
    "reTime\":\"2026-10-28T07:35:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:166\"}}},{\"s"
    "topPositionInPattern\":2,\"realtime\":true,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10"
    "-28T07:41:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T07:41:00+01:00\",\"aimedDepartureTim"
    "e\":\"2026-10-28T07:43:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T07:43:00+01:00\",\"quay"
    "\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:269\"}}},{\"stopPositionInPattern\":3,\"realtime\":true,\""
    "cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T07:55:00+01:00\",\"expectedArrivalTime\""
    ":\"2026-10-28T07:55:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T07:56:00+01:00\",\"expectedD"
    "epartureTime\":\"2026-10-28T07:56:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:451\"}"
    "}},{\"stopPositionInPattern\":4,\"realtime\":true,\"cancellation\":false,\"aimedArrivalTime\":\"2"
    "026-10-28T08:06:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T08:06:00+01:00\",\"aimedDepart"
    "ureTime\":\"2026-10-28T08:09:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T08:09:00+01:00\""
    ",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:337\"}}},{\"stopPositionInPattern\":5,\"realtime\":"
    "true,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T08:10:00+01:00\",\"expectedArriva"
    "lTime\":\"2026-10-28T08:10:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T08:11:00+01:00\",\"exp"
    "ectedDepartureTime\":\"2026-10-28T08:11:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace"
    ":288\"}}},{\"stopPositionInPattern\":6,\"realtime\":true,\"cancellation\":false,\"aimedArrivalTi"
    "me\":\"2026-10-28T08:14:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T08:14:00+01:00\",\"aimed"
    "DepartureTime\":\"2026-10-28T08:15:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T08:15:00+"
    "01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:152\"}}},{\"stopPositionInPattern\":7,\"real"
    "time\":true,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T08:18:00+01:00\",\"expected"
    "ArrivalTime\":\"2026-10-28T08:18:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T08:19:00+01:00"
    "\",\"expectedDepartureTime\":\"2026-10-28T08:19:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:Sto"
    "pPlace:157\"}}},{\"stopPositionInPattern\":8,\"realtime\":true,\"cancellation\":false,\"aimedArr"
    "ivalTime\":\"2026-10-28T08:23:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T08:23:00+01:00\","
    "\"aimedDepartureTime\":\"2026-10-28T08:24:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T08:"
    "24:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:610\"}}},{\"stopPositionInPattern\":9"
    ",\"realtime\":true,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T08:29:00+01:00\",\"ex"
    "pectedArrivalTime\":\"2026-10-28T08:29:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T08:30:00"
    "+01:00\",\"expectedDepartureTime\":\"2026-10-28T08:30:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"N"
    "SR:StopPlace:418\"}}},{\"stopPositionInPattern\":10,\"realtime\":true,\"cancellation\":false,\"a"
    "imedArrivalTime\":\"2026-10-28T08:41:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T08:41:00+"
    "01:00\",\"aimedDepartureTime\":\"2026-10-28T08:43:00+01:00\",\"expectedDepartureTime\":\"2026-10"
    "-28T08:43:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:11\"}}},{\"stopPositionInPatt"
    "ern\":11,\"realtime\":true,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T08:46:00+01:"
    "00\",\"expectedArrivalTime\":\"2026-10-28T08:46:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T0"
    "8:47:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T08:47:00+01:00\",\"quay\":{\"stopPlace\":{"
    "\"id\":\"NSR:StopPlace:18\"}}},{\"stopPositionInPattern\":12,\"realtime\":true,\"cancellation\":fa"
    "lse,\"aimedArrivalTime\":\"2026-10-28T08:53:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T08:"
    "53:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T08:53:00+01:00\",\"expectedDepartureTime\":\"2"
    "026-10-28T08:53:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:187\"}}},{\"stopPositio"
    "nInPattern\":13,\"realtime\":true,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T08:59"
    ":00+01:00\",\"expectedArrivalTime\":\"2026-10-28T08:59:00+01:00\",\"aimedDepartureTime\":\"2026-"
    "10-28T09:01:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T09:01:00+01:00\",\"quay\":{\"stopP"
    "lace\":{\"id\":\"NSR:StopPlace:245\"}}},{\"stopPositionInPattern\":14,\"realtime\":true,\"cancella"
    "tion\":false,\"aimedArrivalTime\":\"2026-10-28T09:06:00+01:00\",\"expectedArrivalTime\":\"2026-1"
    "0-28T09:06:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T09:06:00+01:00\",\"expectedDeparture"
    "Time\":\"2026-10-28T09:06:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:401\"}}},{\"sto"
    "pPositionInPattern\":15,\"realtime\":true,\"cancellation\":false,\"aimedArrivalTime\":\"2026-10-"
    "28T09:11:00+01:00\",\"expectedArrivalTime\":\"2026-10-28T09:11:00+01:00\",\"aimedDepartureTime"
    "\":\"2026-10-28T09:11:00+01:00\",\"expectedDepartureTime\":\"2026-10-28T09:11:00+01:00\",\"quay\""
    ":{\"stopPlace\":{\"id\":\"NSR:StopPlace:628\"}}},{\"stopPositionInPattern\":16,\"realtime\":true,\""
    "cancellation\":false,\"aimedArrivalTime\":\"2026-10-28T09:25:00+01:00\",\"expectedArrivalTime\""
    ":\"2026-10-28T09:25:00+01:00\",\"aimedDepartureTime\":\"2026-10-28T09:25:00+01:00\",\"expectedD"
    "epartureTime\":\"2026-10-28T09:25:00+01:00\",\"quay\":{\"stopPlace\":{\"id\":\"NSR:StopPlace:519\"}"
    "}}]},\"l2\":null}}"
)
RECORDED_REALTIME_BYTES = 14008


if __name__ == "__main__":
    _run()
