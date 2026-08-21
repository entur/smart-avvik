"""Checks for /tilbakeblikk-api — a window of past alert decisions, replayed through today's prompt.

Hermetic, exactly like test_historikk.py: BigQuery is replaced with a fake that records the SQL, the
parameters and the job config it was handed and replays canned rows, so nothing here touches
BigQuery or the network.

Three families of check carry most of the weight, and each of them guards a defect that is silent
rather than loud:

* **The window.** Both times are quantized to 15-minute steps and the range is half-open, because
  every parameter lands in the response-cache key on a public unauthenticated endpoint. Two
  free-form second-resolution times are on the order of a billion keys per day, each one a cache
  entry and a BigQuery query. The checks assert the bound rather than assuming it.
* **The redaction.** No decision row may carry `journey_digest`, `from_stop`, `to_stop` or
  `message_input`, and the last of those is specific to this surface — it embeds all three of
  origin, destination and departure time in one blob.
* **The SQL text.** Nothing in this repo executes SQL and CI has no GCP credentials, so a defect in
  a .sql file ships green. The grep-level checks at the bottom pin the properties whose failure mode
  is a silently wrong answer rather than an error: the null-unsafe array test, the partition bounds,
  and the single-aggregate replay pick.

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery \
     --with google-cloud-storage --with pywebpush python test_tilbakeblikk.py   (or under pytest)
"""

import inspect
import json
import logging
import os
import re
import time as _time
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# --- Stub the BigQuery client BEFORE importing the module (main builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import llm_message
import tilbakeblikk
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

OSLO = ZoneInfo("Europe/Oslo")
DEP = datetime(2026, 7, 30, 6, 15, tzinfo=UTC)

# A day safely inside the lookback, so a check does not start failing when the clock rolls over.
PAST_DAY = datetime.now(OSLO).date() - timedelta(days=3)


def _row(**overrides) -> dict:
    """A decision row as tilbakeblikk_decisions.sql returns it; override per test."""
    base = {
        "situation_number": "S1",
        "summary": "Signalfeil ved Asker",
        "description": "Det er meldt om signalfeil.",
        "operator": "VYG",
        "line": "VYG:Line:R11",
        "mode": "Tog",
        "call_date": date(2026, 7, 30),
        "departure_time": DEP,
        "n_decisions": 4,
        "first_decided_at": DEP - timedelta(minutes=90),
        "last_decided_at": DEP - timedelta(minutes=5),
        "ever_alerted": True,
        "first_alert_at": DEP - timedelta(minutes=48),
        "alert_lead_min": 48,
        "max_impact_probability": 0.93,
        "final_impact_probability": 0.93,
        "final_cancel_probability": 0.81,
        "final_delay_p50_min": 15,
        "final_delay_p90_min": 40,
        "alert_threshold": 0.65,
        "et_cancelled": False,
        "pushed_any": True,
        "n_devices": 2,
        "would_send_at": DEP - timedelta(minutes=48),
        "first_in_window": True,
        "prompt_version": "a1b2c3d4",
        "actual_cancelled": True,
        "actual_dep_delay_min": None,
        "actual_trouble": True,
        "verdict": "hit",
        "outcome_state": "avklart",
    }
    base.update(overrides)
    return base


STORED_INPUT = {
    "linje": "R11",
    "reise": {"fra": "Oslo S", "til": "Drammen", "avgang": "06:15"},
    "innstilt": True,
    "situasjoner": [{"sammendrag": "Signalfeil ved Asker"}],
}
# Serialized by llm_message's OWN serializer, which is what makes the round-trip check meaningful:
# `message_input_json(json.loads(s)) == s` is the property that lets a replayed prompt be the
# recorded prompt rather than something that merely resembles it.
INPUT_JSON = llm_message.message_input_json(STORED_INPUT)


def _message_row(**overrides) -> dict:
    """A replay row as tilbakeblikk_messages.sql returns it — subject key, input, cycle.

    Carries `journey_digest` and `message_input`, which is exactly why nothing built from it may
    reach the response: this is the server-side half of the privacy boundary.
    """
    base = {
        "journey_digest": "abc123def456",
        "operator": "VYG",
        "jkey": "VYG:DatedServiceJourney:1",
        "call_date": date(2026, 7, 30),
        "stop_order": 3,
        "situation_number": "S1",
        "line": "VYG:Line:R11",
        "mode": "Tog",
        "departure_time": DEP,
        "message_input": INPUT_JSON,
        "would_send_at": DEP - timedelta(minutes=48),
        "prompt_version": "a1b2c3d4",
    }
    base.update(overrides)
    return base


def _subjects(n: int) -> list[dict]:
    """`n` distinct subjects, each with its own input, so a captured prompt names which one it was."""
    return [
        _message_row(
            situation_number=f"S{i}",
            message_input=llm_message.message_input_json({**STORED_INPUT, "sak": f"S{i}"}),
        )
        for i in range(n)
    ]


class _FakeBQ:
    """Records what it was asked — SQL, parameters and job config — and replays canned rows."""

    def __init__(self):
        self.rows: list[dict] = []
        self.message_rows: list[dict] = []
        self.filter_rows: list[dict] = []
        self.registry_rows: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.job_configs: list = []
        self.error: Exception | None = None
        # Fail ONE query while the others answer normally — how a dependency in another project
        # (the stop registry) actually goes away.
        self.error_on: str | None = None

    def query(self, sql, job_config=None):
        # ArrayQueryParameter carries `.values`, ScalarQueryParameter `.value`.
        params = {
            p.name: list(p.values) if hasattr(p, "values") else p.value
            for p in (job_config.query_parameters if job_config else [])
        }
        self.calls.append((sql, params))
        self.job_configs.append(job_config)
        if self.error is not None:
            raise self.error
        if self.error_on is not None and sql == self.error_on:
            raise RuntimeError("403 Access Denied: national_stop_registry")
        # Three distinct shapes come off the same client: decision rows, the /filters vocabulary
        # (four arrays, one row) and the NSR registry lookup behind the from/to grouping. The fake
        # answers per QUERY rather than per call — /filters and the registry both sit on the
        # VALIDATION path of /decisions and /summary, so a fake that replayed one list would hand a
        # filtered request decision rows as its allow-list and reject everything.
        if sql == tilbakeblikk._FILTERS_QUERY:
            rows = self.filter_rows
        elif sql == tilbakeblikk._STOP_PLACES_QUERY:
            rows = self.registry_rows
        elif sql == tilbakeblikk._MESSAGES_QUERY:
            rows = self.message_rows
        else:
            rows = self.rows

        class _Job:
            def result(self):
                return list(rows)

        return _Job()


fake = _FakeBQ()

# Every payload the template writer was handed, so a check can assert the two arms were built from
# ONE input rather than two that happen to agree.
deterministic_inputs: list[dict] = []


def _fake_deterministic(payload: dict) -> str:
    """Stands in for varsel._deterministic_body — a pure function of the stored input, injected.

    A stub rather than the real function on purpose: importing varsel here would pull the poller's
    module (and its mutable state) into a suite about a read path, which is the same reason the
    module takes it by injection in the first place.
    """
    deterministic_inputs.append(payload)
    return f"Mal for {payload['linje']}: avviket kan påvirke reisen din."


tilbakeblikk.configure(bq_client=fake, deterministic_writer=_fake_deterministic)

app = FastAPI()
app.include_router(tilbakeblikk.router)
client = TestClient(app)


def _filter_row(**overrides) -> dict:
    """The vocabulary /tilbakeblikk-api/filters returns, matching the line and places _row() names."""
    base = {
        "operators": ["VYG"],
        "lines": ["VYG:Line:R11"],
        "from_places": [{"ref": "NSR:StopPlace:337", "n": 5}],
        "to_places": [{"ref": "NSR:StopPlace:548", "n": 5}],
    }
    base.update(overrides)
    return base


def _place(ref, name, place_id=None) -> dict:
    """One National Stop Register row: the name, and the parent that says which refs are co-located."""
    return {"id": ref, "name": name, "place_id": place_id or ref}


def _reset(rows=None, error=None, filter_rows=None, registry=None, message_rows=None):
    fake.rows = list(rows or [])
    fake.message_rows = list(message_rows or [])
    fake.filter_rows = [_filter_row()] if filter_rows is None else list(filter_rows)
    fake.registry_rows = list(
        registry
        if registry is not None
        else [_place("NSR:StopPlace:337", "Oslo S"), _place("NSR:StopPlace:548", "Asker stasjon")]
    )
    fake.calls.clear()
    fake.job_configs.clear()
    fake.error = error
    fake.error_on = None
    tilbakeblikk._cache.clear()  # the TTL cache would otherwise serve a previous test's rows
    # The replay state, all four pieces of it. Leaving any one behind makes a later check pass for
    # the wrong reason: a warm wording cache hides a missing backend call, a spent per-minute tally
    # makes a budget check "bind" before it should, and a latched replay holder reports `disabled`
    # where a test expects an answer.
    tilbakeblikk._wordings.clear()
    for spent in tilbakeblikk._replay_calls.values():
        spent.clear()
    tilbakeblikk._replay_state = None
    tilbakeblikk._warned_no_writer = False
    deterministic_inputs.clear()
    # llm_message's own globals: this suite must never depend on the ambient environment of whoever
    # runs it (VARSEL_LLM_PROVIDER, GOOGLE_CLOUD_PROJECT), and the module-global latch is what the
    # isolation checks assert has NOT moved.
    llm_message._PROVIDER = "auto"
    llm_message._PROJECT = "ent-kixavvik-test"
    llm_message._ENABLED = True
    llm_message._STATE = llm_message._LatchState()


def _decision_calls():
    return [c for c in fake.calls if c[0] == tilbakeblikk._DECISIONS_QUERY]


def _never_called(body):
    """A backend that must not be reached. The default stub for the second backend.

    An unstubbed backend in this suite is a real Vertex client construction and a real network call,
    and it fails OPEN — `write_message_detailed` swallows it into `status="error"` — so the
    assertion under it would still pass while the test quietly talked to Google.
    """
    raise AssertionError("this backend must not be called")


@contextmanager
def _backends(claude, gemini=_never_called):
    """Swap both LLM backends for the body, restoring the real ones afterwards.

    Both, always: stubbing only the first leaves the fallback path pointed at the real Vertex client
    (see `_never_called`), and the fallback path is exactly what several checks below exercise.
    """
    original = dict(llm_message._BACKENDS)
    llm_message._BACKENDS.update(claude=claude, gemini=gemini)
    try:
        yield
    finally:
        llm_message._BACKENDS.update(original)


@contextmanager
def _captured(level=logging.INFO):
    """Collect this module's log records for the body."""
    records: list[logging.LogRecord] = []

    class _Sink(logging.Handler):
        def emit(self, record):
            records.append(record)

    module_logger = logging.getLogger("tilbakeblikk")
    sink = _Sink(level)
    module_logger.addHandler(sink)
    before = module_logger.level
    module_logger.setLevel(level)
    try:
        yield records
    finally:
        module_logger.removeHandler(sink)
        module_logger.setLevel(before)


def _reason(records, reason):
    """The bounded-out log records naming `reason`."""
    return [r for r in records if getattr(r, "fields", {}).get("reason") == reason]


def _oslo_midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=OSLO)


# --------------------------------------------------------------------------------------------
# The window: defaults, quantization, bounds
# --------------------------------------------------------------------------------------------


@contextmanager
def _clock_at(instant: datetime):
    """Run the body with the module's wall clock frozen at `instant`.

    Needed because the obvious check — "the default day equals today" — computes `today` the same way
    the code does, so it agrees with a UTC-resolved default for 22 hours out of 24 and only
    discriminates between midnight and 02:00 CEST. A mutation test caught exactly that: swapping
    `datetime.now(_OSLO).date()` for `datetime.utcnow().date()` left the suite green.

    The module touches only `datetime.now` and `datetime.combine`, and its annotations were resolved
    at import, so a shim exposing those two is enough.
    """
    real = tilbakeblikk.datetime

    class _Clock:
        @staticmethod
        def now(tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

        combine = staticmethod(real.combine)

    tilbakeblikk.datetime = _Clock
    try:
        yield
    finally:
        tilbakeblikk.datetime = real


def test_the_day_defaults_to_the_oslo_calendar_day_not_the_utc_one():
    """The page asks about a stretch of a LOCAL day, and for two hours every night the two calendars
    disagree: at 22:30 UTC it is already the 21st in Oslo. A UTC-resolved default would open the page
    on yesterday — showing an empty or half-finished day to whoever is looking at it late in the
    evening, which is when a disruption surface is most likely to be open."""
    _reset([_row()])
    with _clock_at(datetime(2026, 8, 20, 22, 30, tzinfo=UTC)):
        body = client.get("/tilbakeblikk-api/decisions").json()
    assert body["day"] == "2026-08-21", "the default day must be the Oslo calendar day"
    # ...and the window it resolves to is that Oslo day, not a UTC one: 22:00 UTC to 22:00 UTC.
    _, params = _decision_calls()[0]
    assert params["from_ts"] == _oslo_midnight(date(2026, 8, 21))
    assert params["to_ts"] == _oslo_midnight(date(2026, 8, 22))


def test_the_day_defaults_to_today_in_oslo():
    """The ordinary path, against the ambient clock — the frozen-clock check above is what actually
    discriminates the timezone."""
    _reset([_row()])
    body = client.get("/tilbakeblikk-api/decisions").json()
    assert body["day"] == datetime.now(OSLO).date().isoformat()


def test_the_time_range_defaults_to_the_whole_day_as_a_half_open_range():
    """00:00 to the FOLLOWING midnight, and both reach the query as real timestamps rather than
    NULLs.

    23:59:59.999999 is not on a 15-minute grid, so a default expressed that way would either have to
    escape the quantization or be rounded down — silently dropping the last quarter-hour of every
    default request, which is the busiest part of an evening window.
    """
    _reset([_row()])
    body = client.get("/tilbakeblikk-api/decisions").json()
    today = datetime.now(OSLO).date()
    assert body["from_time"] == "00:00:00"
    # 00:00 as an END means the following midnight; it cannot mean anything else, since a literal
    # 00:00 end would be at or before the start and is rejected.
    assert body["to_time"] == "00:00:00"

    _, params = _decision_calls()[0]
    assert params["from_ts"] == _oslo_midnight(today)
    assert params["to_ts"] == _oslo_midnight(today + timedelta(days=1))
    # Asserted against the next Oslo midnight rather than "+24 h": two days a year are 23 or 25
    # hours long, and a default that drifted by an hour on those days would be invisible.
    assert params["to_ts"] is not None and params["from_ts"] is not None


def test_from_time_rounds_down_and_to_time_rounds_up():
    """A narrowed range must never drop a minute the caller asked for. 14:07-14:22 therefore queries
    14:00-14:30, not 14:15-14:15."""
    _reset([_row()])
    client.get(
        "/tilbakeblikk-api/decisions",
        params={"day": PAST_DAY.isoformat(), "from_time": "14:07", "to_time": "14:22"},
    )
    _, params = _decision_calls()[0]
    assert params["from_ts"] == _oslo_midnight(PAST_DAY) + timedelta(hours=14)
    assert params["to_ts"] == _oslo_midnight(PAST_DAY) + timedelta(hours=14, minutes=30)

    # A boundary-aligned end stays where it is: snapping 15:00 forward to 15:15 would widen every
    # picker-produced request by a quarter-hour for nothing, since the picker offers exactly these
    # boundaries.
    _reset([_row()])
    client.get(
        "/tilbakeblikk-api/decisions",
        params={"day": PAST_DAY.isoformat(), "from_time": "14:00", "to_time": "15:00"},
    )
    _, params = _decision_calls()[0]
    assert params["to_ts"] == _oslo_midnight(PAST_DAY) + timedelta(hours=15)


def test_two_times_in_one_step_share_a_cache_entry_and_a_query():
    """The key-space bound, asserted rather than assumed.

    This is the whole reason the times are quantized: the endpoint is public and unauthenticated and
    both times are in the cache key, so second-resolution values would let a caller mint an unbounded
    number of cache entries — and BigQuery queries — out of one window.
    """
    _reset([_row()])
    for minute in ("14:01", "14:07", "14:14:59"):
        client.get(
            "/tilbakeblikk-api/decisions",
            params={"day": PAST_DAY.isoformat(), "from_time": minute, "to_time": "15:00"},
        )
    assert len(_decision_calls()) == 1, "three times inside one 15-minute step must share one query"
    assert len(tilbakeblikk._cache) == 1


def test_a_reversed_or_empty_time_range_is_rejected_without_a_query():
    """400 rather than an empty page, and no BigQuery call spent on it.

    Equal endpoints are rejected too, not only inverted ones: after quantization an equal pair is an
    empty range exactly when it lands on a step boundary and a non-empty one otherwise, and a rule
    whose behaviour depends on where the clock hands happen to be is worse than a refusal.
    """
    for from_time, to_time in (("14:00", "13:00"), ("14:00", "14:00")):
        _reset([_row()])
        bad = client.get(
            "/tilbakeblikk-api/decisions",
            params={"day": PAST_DAY.isoformat(), "from_time": from_time, "to_time": to_time},
        )
        assert bad.status_code == 400, (from_time, to_time)
        assert "to_time" in bad.json()["detail"]
        assert fake.calls == [], "a rejected time range must not reach BigQuery"


def test_a_future_day_and_a_day_beyond_the_lookback_are_rejected_without_a_query():
    """Both bounds exist for the same reason as the quantization: the day is in the cache key, so an
    unbounded range of days is an unbounded number of cache entries on a public endpoint. A future
    day has no decisions in it at all, so it can only ever be key-space."""
    import historikk

    tomorrow = datetime.now(OSLO).date() + timedelta(days=1)
    _reset([_row()])
    bad = client.get("/tilbakeblikk-api/decisions", params={"day": tomorrow.isoformat()})
    assert bad.status_code == 400
    assert "future" in bad.json()["detail"]
    assert fake.calls == []

    _reset([_row()])
    ancient = datetime.now(OSLO).date() - timedelta(days=historikk._MAX_WINDOW_DAYS + 1)
    bad = client.get("/tilbakeblikk-api/decisions", params={"day": ancient.isoformat()})
    assert bad.status_code == 400
    assert "too far back" in bad.json()["detail"]
    assert fake.calls == []


def test_the_window_is_echoed_in_full_on_every_data_response():
    """Four fields for one range: two the picker needs, two the contract needs. A client that had to
    re-derive the instants from a day and a time would have to re-implement Europe/Oslo and its two
    irregular days, and would get the exclusive end wrong."""
    _reset([_row()])
    body = client.get(
        "/tilbakeblikk-api/decisions",
        params={"day": PAST_DAY.isoformat(), "from_time": "06:00", "to_time": "09:00"},
    ).json()
    assert body["day"] == PAST_DAY.isoformat()
    assert body["from_time"] == "06:00:00"
    assert body["to_time"] == "09:00:00"
    assert datetime.fromisoformat(body["window_start"]) == _oslo_midnight(PAST_DAY) + timedelta(hours=6)
    assert datetime.fromisoformat(body["window_end"]) == _oslo_midnight(PAST_DAY) + timedelta(hours=9)


# --------------------------------------------------------------------------------------------
# The cache key
# --------------------------------------------------------------------------------------------


def test_every_parameter_makes_its_own_cache_entry():
    """The key is a positional tuple, so a parameter added to the params list and forgotten in the
    key fails SILENTLY: for two minutes, whichever request landed first decides what both callers
    see. Every distinct request below must therefore cost its own query."""
    variants = [
        {},
        {"day": (PAST_DAY - timedelta(days=1)).isoformat()},
        {"from_time": "06:00"},
        {"to_time": "18:00"},
        {"operator": "VYG"},
        {"line": "VYG:Line:R11"},
        {"from_place": "NSR:StopPlace:337"},
        {"to_place": "NSR:StopPlace:548"},
        {"verdict": "hit"},
        {"alerted_only": "true"},
        {"resolved_only": "true"},
        {"sx_only": "true"},
        {"limit": "25"},
    ]
    _reset([_row()])
    for extra in variants:
        client.get(
            "/tilbakeblikk-api/decisions", params={"day": PAST_DAY.isoformat(), **extra}
        )
    assert len(_decision_calls()) == len(variants)


def test_identical_requests_are_served_from_cache():
    _reset([_row()])
    client.get("/tilbakeblikk-api/decisions")
    client.get("/tilbakeblikk-api/decisions")
    assert len(_decision_calls()) == 1, "the second identical request should be served from cache"


def test_cache_is_bounded():
    """A public endpoint whose cache key carries caller-chosen values needs a cap. This pod never
    restarts, and its memory limit is the binding constraint — an OOMKill is zero capacity for the
    length of a cold start, because uvicorn binds only after lifespan's eager BigQuery loads."""
    _reset([_row()])
    for i in range(tilbakeblikk._CACHE_MAX_ENTRIES + 50):
        day = datetime.now(OSLO).date() - timedelta(days=i % 90)
        client.get("/tilbakeblikk-api/decisions", params={"day": day.isoformat()})
    assert len(tilbakeblikk._cache) <= tilbakeblikk._CACHE_MAX_ENTRIES


# --------------------------------------------------------------------------------------------
# The allow-list
# --------------------------------------------------------------------------------------------


def test_unknown_operator_is_rejected_without_a_query():
    """An unserved codespace must not reach BigQuery or mint a cache entry: the endpoint is public
    and `operator` is part of the cache key, so an arbitrary string would buy one cached query per
    distinct value."""
    _reset([_row()])
    bad = client.get("/tilbakeblikk-api/decisions", params={"operator": "NOT-AN-OPERATOR"})
    assert bad.status_code == 400
    assert "operator must be one of" in bad.json()["detail"]
    assert fake.calls == [], "a rejected operator must not reach BigQuery"


def test_a_value_no_decision_in_the_window_carries_is_rejected():
    """A 400 rather than an empty list, because "this line was not decided on in this window" and
    "it was, and your other filters excluded everything" are different answers — and a client that
    cannot tell them apart will show the second when it means the first."""
    for param, expected in (
        ({"line": "VYG:Line:R99"}, "Unknown line"),
        ({"from_place": "NSR:StopPlace:999"}, "Unknown from_place"),
        ({"to_place": "NSR:StopPlace:999"}, "Unknown to_place"),
    ):
        _reset([_row()])
        bad = client.get("/tilbakeblikk-api/decisions", params=param)
        assert bad.status_code == 400, param
        assert expected in bad.json()["detail"]
        assert _decision_calls() == [], "a rejected filter must not reach the decisions query"


def test_verdict_filter_is_validated():
    _reset([_row()])
    assert client.get("/tilbakeblikk-api/decisions", params={"verdict": "hit"}).status_code == 200
    _reset([_row()])
    bad = client.get("/tilbakeblikk-api/decisions", params={"verdict": "definitely-not"})
    assert bad.status_code == 400
    assert "verdict must be one of" in bad.json()["detail"]
    assert fake.calls == []


def test_an_unfiltered_request_spends_no_query_on_the_allow_list():
    """The default load is the common one and needs no vocabulary to validate. Checking it
    unconditionally would double every page load's BigQuery cost for nothing."""
    _reset([_row()])
    client.get("/tilbakeblikk-api/decisions")
    assert [c[0] for c in fake.calls] == [tilbakeblikk._DECISIONS_QUERY]


def test_the_allow_list_itself_is_keyed_on_the_window_alone():
    """So every filter combination on the page shares one vocabulary lookup — which is what makes it
    affordable to run the check on the validation path of /decisions and /summary at all."""
    _reset([_row()], filter_rows=[
        _filter_row(from_places=[
            {"ref": "NSR:StopPlace:337", "n": 9},
            {"ref": "NSR:StopPlace:548", "n": 4},
        ])
    ])
    client.get("/tilbakeblikk-api/decisions", params={"from_place": "NSR:StopPlace:337"})
    client.get("/tilbakeblikk-api/decisions", params={"from_place": "NSR:StopPlace:548"})
    assert len(_decision_calls()) == 2
    assert len([c for c in fake.calls if c[0] == tilbakeblikk._FILTERS_QUERY]) == 1


def test_a_chosen_place_expands_to_every_ref_standing_for_it():
    """The expansion is the whole point of grouping, not a detail of it. A station's rail and bus
    sides are separate NSR refs and each feed records its calls on its own, so matching only the ref
    the dropdown nominated hid 659 of Asker stasjon's 842 decisions on /historikk (dev, 2026-08-21).
    """
    _reset(
        [_row()],
        filter_rows=[_filter_row(from_places=[
            {"ref": "NSR:StopPlace:418", "n": 659},
            {"ref": "NSR:StopPlace:3247", "n": 183},
        ])],
        registry=[
            _place("NSR:StopPlace:418", "Asker stasjon", "NSR:StopPlace:59616"),
            _place("NSR:StopPlace:3247", "Asker stasjon", "NSR:StopPlace:59616"),
        ],
    )
    body = client.get("/tilbakeblikk-api/filters").json()
    assert body["from_places"] == [
        {"ref": "NSR:StopPlace:418", "name": "Asker stasjon", "n_refs": 2}
    ], "one row per physical place, nominating the busiest ref"

    client.get("/tilbakeblikk-api/decisions", params={"from_place": "NSR:StopPlace:418"})
    _, params = _decision_calls()[0]
    assert params["from_place_refs"] == ["NSR:StopPlace:3247", "NSR:StopPlace:418"]


def test_filters_lists_the_windows_vocabulary_and_echoes_the_window():
    _reset([_row()])
    r = client.get(
        "/tilbakeblikk-api/filters",
        params={"day": PAST_DAY.isoformat(), "from_time": "06:07", "to_time": "09:02"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["operators"] == ["VYG"]
    assert body["lines"] == ["VYG:Line:R11"]
    assert body["from_places"] == [{"ref": "NSR:StopPlace:337", "name": "Oslo S", "n_refs": 1}]
    # The vocabulary is scoped to the SAME quantized window as the data, or the dropdowns would offer
    # values the list beside them cannot show.
    assert (body["from_time"], body["to_time"]) == ("06:00:00", "09:15:00")


def test_an_unreadable_stop_registry_leaves_the_filters_usable():
    """The registry lives in another project behind an allAuthenticatedUsers grant, so it is the one
    dependency here that can go away on its own. It must cost names and grouping, never the page."""
    _reset([_row()])
    fake.error_on = tilbakeblikk._STOP_PLACES_QUERY
    try:
        r = client.get("/tilbakeblikk-api/filters")
    finally:
        fake.error_on = None
    assert r.status_code == 200, r.text
    assert r.json()["from_places"] == [{"ref": "NSR:StopPlace:337", "name": None, "n_refs": 1}]


def test_limit_above_the_cap_is_fastapis_422_not_this_modules_400():
    """Two different refusals, and a client has to be able to tell them apart: 422 means the request
    was malformed against the schema, 400 means it was well-formed and named something that does not
    exist in this window."""
    _reset([_row()])
    assert client.get("/tilbakeblikk-api/decisions", params={"limit": 10_000}).status_code == 422


# --------------------------------------------------------------------------------------------
# /summary
# --------------------------------------------------------------------------------------------


def test_summary_refuses_the_outcome_filters_and_accepts_the_population_ones():
    """verdict, alerted_only and resolved_only narrow by OUTCOME: filtering to hits would make
    precision read 100 %. The population filters narrow WHICH decisions, so the rates have to
    describe the same slice the list beside them shows.

    Asserted on the route signature as well as on the query parameters — an accepted-but-ignored
    parameter reads as "it did something" from the caller's side.
    """
    accepted = set(inspect.signature(tilbakeblikk.summary).parameters)
    assert not {"verdict", "alerted_only", "resolved_only", "sx_only"} & accepted
    assert {"day", "from_time", "to_time", "operator", "line", "from_place", "to_place"} <= accepted

    _reset([{"n_hit": 1}])
    r = client.get("/tilbakeblikk-api/summary", params={"line": "VYG:Line:R11"})
    assert r.status_code == 200, r.text
    _, params = next(c for c in fake.calls if c[0] == tilbakeblikk._SUMMARY_QUERY)
    assert params["line"] == "VYG:Line:R11"
    assert "verdict" not in params and "alerted_only" not in params
    # Empty array, never NULL: BigQuery has no NULL array parameter, so emptiness is how the SQL
    # spells "no filter" -- and a NULL here would make the predicate match nothing at all.
    assert params["from_place_refs"] == []


def test_summary_reports_the_labelled_split_alongside_the_rates():
    """R20, and it is not a diagnostic. Verdicts arrive from the 04:00 UTC job the following night,
    so on this page's default view — today, all day — the rates are computed over a handful of rows
    while everything else is still `venter`. Without the denominator a reader takes a precision of
    1.00 over three decisions for a claim about the day."""
    _reset([{
        "n_subjects": 412, "n_labelled": 3,
        "n_hit": 3, "n_false_alarm": 0, "n_miss": 0, "n_correct_reject": 0,
        "n_unknown": 1, "n_pending": 408, "precision": 1.0,
    }])
    s = client.get("/tilbakeblikk-api/summary").json()
    assert s["n_subjects"] == 412
    assert s["n_labelled"] == 3
    assert s["n_labelled"] + s["n_unknown"] + s["n_pending"] == s["n_subjects"]
    assert s["precision"] == 1.0


def test_summary_splits_provenance_and_keeps_unsegmented_its_own_bucket():
    """The blended precision is a mixture and the page has to be able to say of what. A decision on a
    call ET had already declared cancelled is served at probability 1.0 by construction: a real hit
    for the traveller, a gimme for the models."""
    _reset([{
        "n_hit": 10, "n_false_alarm": 2, "n_miss": 3, "precision": 10 / 12,
        "n_hit_predicted": 6, "n_false_alarm_predicted": 2, "precision_predicted": 0.75,
        "n_hit_relayed": 4, "n_false_alarm_relayed": 0, "precision_relayed": 1.0,
        "n_hit_no_sx": 0, "precision_no_sx": None,
        "n_unsegmented": 7,
    }])
    s = client.get("/tilbakeblikk-api/summary").json()
    assert s["predicted"]["precision"] < s["precision"] < s["relayed"]["precision"]
    assert s["predicted"]["n_hit"] + s["relayed"]["n_hit"] == s["n_hit"]
    # None, not 0.0: a rate over no decisions is unknown, and 0 % would read as "we got them wrong".
    assert s["no_sx"]["precision"] is None
    # Rows written before the flag existed belong to neither segment, and are reported rather than
    # assigned — defaulting them to `predicted` would be right for most and would bury the ones
    # worth finding.
    assert s["n_unsegmented"] == 7


def test_summary_survives_an_empty_window():
    """A window with no decisions in it is zeroes, not a 500."""
    _reset([])
    s = client.get("/tilbakeblikk-api/summary").json()
    assert s["n_subjects"] == 0 and s["n_labelled"] == 0
    assert s["precision"] is None and s["recall"] is None
    assert s["predicted"]["n_hit"] == 0 and s["predicted"]["precision"] is None


# --------------------------------------------------------------------------------------------
# The response shape and the redaction
# --------------------------------------------------------------------------------------------


def test_decisions_returns_rows_with_the_replay_fields():
    _reset([_row()])
    r = client.get("/tilbakeblikk-api/decisions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    d = body["decisions"][0]
    assert d["verdict"] == "hit"
    assert d["would_send_at"] is not None
    assert d["first_in_window"] is True
    assert d["prompt_version"] == "a1b2c3d4"


def test_a_subject_with_no_replayable_cycle_is_its_own_state():
    """R7. No alert-worthy cycle inside the window recorded a writer's input, so there is nothing to
    replay — which is NOT "no message" and not an error. The row still appears, with the three replay
    fields empty and first_in_window false."""
    _reset([_row(would_send_at=None, prompt_version=None, first_in_window=False)])
    d = client.get("/tilbakeblikk-api/decisions").json()["decisions"][0]
    assert d["would_send_at"] is None
    assert d["prompt_version"] is None
    assert d["first_in_window"] is False


def test_response_carries_no_traveller_journey_identifiers_and_no_writer_input():
    """The public response describes a train, never whose trip it was.

    from_stop/to_stop/journey_digest next to departure_time and n_devices would publish "somebody
    commutes A->B at 07:15". message_input is the fourth and is specific to this surface: it embeds
    all three of origin, destination and departure time in one blob.

    This asserts the WHOLE key set, so a future field addition fails here rather than shipping.
    Note what is absent besides the redacted four: `message` and `message_source`. The stored wording
    is deliberately not on this card (R12) — showing the pushed text beside one regenerated from a
    possibly different cycle would reintroduce exactly the mismatch the 2026-08-21 fix removed.
    """
    _reset([_row()])
    d = client.get("/tilbakeblikk-api/decisions").json()["decisions"][0]
    assert not {"journey_digest", "from_stop", "to_stop", "stop_place", "message_input"} & set(d)
    assert not {"message", "message_source"} & set(d)
    assert set(d) == {
        "situation_number", "summary", "description", "operator", "line", "mode", "call_date",
        "departure_time", "n_decisions", "first_decided_at", "last_decided_at",
        "ever_alerted", "first_alert_at", "alert_lead_min",
        "max_impact_probability", "final_impact_probability", "final_cancel_probability",
        "final_delay_p50_min", "final_delay_p90_min", "alert_threshold", "et_cancelled",
        "pushed_any", "n_devices", "would_send_at", "first_in_window", "prompt_version",
        "actual_cancelled", "actual_dep_delay_min", "actual_trouble", "verdict", "outcome_state",
    }


def test_the_decision_model_refuses_a_redacted_column():
    """`extra="forbid"` is what keeps the SELECT honest: it makes the SQL and this model agree column
    for column, so a future query that picks up a redacted one fails loudly here instead of quietly
    widening what an unauthenticated public host serves."""
    for redacted in ("journey_digest", "from_stop", "to_stop", "message_input"):
        try:
            tilbakeblikk.Decision(**_row(), **{redacted: "x"})
            raise AssertionError(f"Decision must reject {redacted} — extra='forbid' is the guard")
        except ValidationError:
            pass


def test_the_job_config_caps_what_one_request_can_bill():
    """Nothing else in this repo sets maximum_bytes_billed, and KTD3's cost argument is a pruning
    claim no test can check — the suites are hermetic and no .sql here is ever executed. The cap is
    the backstop: it bounds the blast radius of a query that starts reading something big, on a
    public unauthenticated endpoint that shares its project quota with the poller."""
    _reset([_row()])
    client.get("/tilbakeblikk-api/decisions")
    assert fake.job_configs, "no query was made"
    for job_config in fake.job_configs:
        assert job_config.maximum_bytes_billed == tilbakeblikk._MAX_BYTES_BILLED


# --------------------------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------------------------


def test_missing_tables_report_unavailable_not_a_traceback():
    _reset([], error=RuntimeError("404 Not found: Table disruptions.varsel_decisions"))
    r = client.get("/tilbakeblikk-api/decisions")
    assert r.status_code == 503
    assert "decision log" in r.json()["detail"].lower()


@contextmanager
def _deployed_as(project: str | None):
    """Run the body as if the app were deployed in `project` (ambient env restored afterwards)."""
    before = {k: os.environ.get(k) for k in ("GOOGLE_CLOUD_PROJECT", "KIX_ENVIRONMENT")}
    os.environ.pop("KIX_ENVIRONMENT", None)  # a local override would mask the project
    if project is None:
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
    else:
        os.environ["GOOGLE_CLOUD_PROJECT"] = project
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_config_survives_an_unreadable_decision_log():
    """Why it is its own endpoint and not a field on /summary: when the log cannot be read, knowing
    which environment you are on is what tells a broken deployment from a fresh one."""
    _reset([], error=RuntimeError("404 Not found: Table disruptions.varsel_decisions"))
    with _deployed_as("ent-kixavvik-dev"):
        assert client.get("/tilbakeblikk-api/summary").status_code == 503
        queries_so_far = len(fake.calls)
        r = client.get("/tilbakeblikk-api/config")
    assert r.status_code == 200
    assert r.json()["environment"] == "dev"
    assert len(fake.calls) == queries_so_far, "/config must not query BigQuery"

    with _deployed_as("ent-kixavvik-prd"):
        assert client.get("/tilbakeblikk-api/config").json()["environment"] is None


# --------------------------------------------------------------------------------------------
# /messages — the bounded replay service
#
# Every backend here is a stub (see `_backends` and `_never_called`): an unstubbed one would build a
# real Vertex client and fail OPEN into status="error", so the assertion under it would still pass
# while the suite quietly talked to Google.
# --------------------------------------------------------------------------------------------

OK_REPLY = '{"melding": "Toget er innstilt. Ta neste avgang."}'
# A figure that is not in the payload. `_is_grounded` rejects it, `write_message_detailed` continues
# to the next backend, and the subject therefore costs TWO calls — which is the whole reason the
# per-request cap counts calls rather than subjects.
UNGROUNDED_REPLY = '{"melding": "Toget er 42 minutter forsinket."}'


def _messages(**params):
    """GET /tilbakeblikk-api/messages, returning the parsed body and asserting it answered."""
    r = client.get("/tilbakeblikk-api/messages", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _statuses(body):
    return [m["status"] for m in body["messages"]]


def test_a_stored_input_reaches_the_backend_verbatim():
    """The prompt the model replays under must be the prompt the decision was written under, or the
    page is comparing two things at once. That holds because `message_input` was serialized by
    llm_message's own serializer and is handed back through it: json.loads then dumps is a round
    trip, byte for byte, not an approximation of one."""
    _reset(message_rows=[_message_row()])
    seen = []
    with _backends(lambda body: seen.append(body) or OK_REPLY):
        body = _messages()
    assert seen == [INPUT_JSON], "the stored JSON must reach the backend unchanged"
    entry = body["messages"][0]
    assert entry["status"] == "ok"
    assert entry["message"] == "Toget er innstilt. Ta neste avgang."
    assert entry["provider"] == "claude"
    assert entry["current_prompt_version"] == llm_message.PROMPT_VERSION
    assert entry["prompt_version"] == "a1b2c3d4", "the row's own prompt version, for R9's marker"
    # Same blast-radius cap as every other query on this surface -- it is a public endpoint sharing
    # its project quota with the poller.
    assert all(jc.maximum_bytes_billed == tilbakeblikk._MAX_BYTES_BILLED for jc in fake.job_configs)


def test_a_repeated_request_is_served_from_the_wording_cache():
    """A page refresh must not re-spend the Vertex budget on wordings it already has — that is what
    leaves the budget free to reach subjects further down the list."""
    _reset(message_rows=[_message_row()])
    calls = []
    with _backends(lambda body: calls.append(body) or OK_REPLY):
        first = _messages()
        second = _messages()
    assert len(calls) == 1, "the second request must be served from the wording cache"
    assert first["messages"][0]["message"] == second["messages"][0]["message"]


def test_a_new_prompt_version_invalidates_the_cached_wording():
    """The prompt version is IN the cache key, which is what makes this page answer the question it
    exists for: after a prompt edit the cards must show what the NEW brief writes, not yesterday's
    answer under today's version number."""
    _reset(message_rows=[_message_row()])
    calls = []
    with _backends(lambda body: calls.append(body) or OK_REPLY):
        _messages()
        assert len(calls) == 1
        before = llm_message.PROMPT_VERSION
        llm_message.PROMPT_VERSION = "0000feed"
        try:
            body = _messages()
        finally:
            llm_message.PROMPT_VERSION = before
    assert len(calls) == 2, "a changed prompt version must miss the cache"
    assert body["messages"][0]["current_prompt_version"] == "0000feed"


def test_a_narrower_window_picks_a_different_cycle_and_misses_the_cache():
    """The replayed `decided_at` is in the key too. Narrowing the window selects a different cycle,
    whose input and figures differ — serving the wider window's wording under the narrower one's
    time is the mismatch the 2026-08-21 fix removed from /historikk one layer down."""
    _reset(message_rows=[_message_row()])
    calls = []
    with _backends(lambda body: calls.append(body) or OK_REPLY):
        _messages(day=PAST_DAY.isoformat())
        fake.message_rows = [_message_row(would_send_at=DEP - timedelta(minutes=20))]
        tilbakeblikk._cache.clear()  # so the narrower window really re-reads
        _messages(day=PAST_DAY.isoformat(), from_time="06:00", to_time="07:00")
    assert len(calls) == 2, "a different replayed cycle is a different wording"


def test_the_per_request_cap_counts_backend_calls_not_subjects():
    """Under `auto` one subject can cost two calls: an ungrounded reply continues to the next
    backend, and a prompt edit that trips the grounding check trips it for EVERY subject at once —
    which is exactly the situation somebody has this page open in. A cap counted in subjects would
    then let one request spend twice its stated ceiling of Vertex calls, and the resource that
    ceiling protects is the quota the poller's push wordings share."""
    _reset(message_rows=_subjects(12))
    calls = []
    with _backends(
        lambda body: calls.append(body) or UNGROUNDED_REPLY,
        gemini=lambda body: calls.append(body) or "",
    ):
        body = _messages()
    cap = tilbakeblikk._MAX_CALLS_PER_REQUEST
    assert len(calls) == cap, f"the cap is {cap} CALLS; {len(calls)} were made"
    statuses = _statuses(body)
    assert statuses.count("ungrounded") == cap // tilbakeblikk._CALLS_PER_SUBJECT == 6
    assert statuses.count("ungrounded") < 12, "fewer subjects than the cap: each cost two calls"
    assert statuses.count("capped") == 6, "the rest are reported, not silently absent"


def test_the_next_request_generates_the_next_subjects_and_serves_the_rest_free():
    """Successive requests fill the page progressively. The cache is consulted BEFORE the budget, so
    a wording already held costs nothing and the budget lands on a subject that has none — without
    that order the first twelve would be re-served forever and a day's worth of subjects would be
    unreachable.

    The property asserted is disjointness and coverage, not exact positions: with a four-wide pool
    the winners are the first six subjects the pool REACHES, which is the first six give or take a
    position. What must hold — and what the cache-before-budget order is for — is that the second
    request spends its budget on the six it does not already hold, and on nothing else.
    """
    _reset(message_rows=_subjects(12))
    seen = []

    def claude(body):
        seen.append(json.loads(body)["sak"])
        return OK_REPLY

    with _backends(claude):
        first = _messages()
        second = _messages()
    assert len(seen) == 12 and len(set(seen)) == 12, (
        "each subject generated exactly once across the two requests: none re-served, none skipped"
    )
    assert not set(seen[:6]) & set(seen[6:]), "the second request must generate the OTHER six"
    assert _statuses(first).count("ok") == 6 and _statuses(first).count("capped") == 6
    assert _statuses(second) == ["ok"] * 12, "the first six now come free from the cache"


def test_the_per_minute_budget_binds_across_requests_and_is_logged_once():
    """One request's cap does not bound a caller who refreshes; the per-minute budget does. It is
    sized at one full request's worth inside the wall-clock deadline, so it binds on a second
    concurrent reader rather than never."""
    _reset(message_rows=_subjects(18))
    calls = []
    with _backends(lambda body: calls.append(body) or OK_REPLY):
        _messages()
        _messages()
        with _captured(logging.WARNING) as records:
            third = _messages()
    assert _statuses(third).count("rate_limited") == 6, "the subjects it could not reach say so"
    assert _statuses(third).count("ok") == 12, "the twelve it already holds are served free"
    assert len(calls) == 12, "the third request must spend no backend call at all"
    spent = tilbakeblikk._replay_calls[tilbakeblikk._REPLAY_PATH]
    assert len(spent) == tilbakeblikk._MAX_CALLS_PER_MIN == 24
    assert len(_reason(records, "rate_limited")) == 1, "logged once per request, not once per subject"


def test_the_budget_is_tallied_against_replays_own_allowance_not_the_pollers():
    """The tally is keyed by caller, in the shape of `alternatives._fetch_calls`. Replay draws on its
    own number, so it can neither starve another caller's share nor be starved by one.

    Be clear about what this buys here: the poller does not call this module, so its slot stays empty
    by construction. The keying is what makes that structural rather than a fact somebody has to keep
    true — and the coupling it defends against is one layer out, at Vertex, where the two really do
    share a quota."""
    _reset(message_rows=_subjects(2))
    now = _time.monotonic()
    tilbakeblikk._replay_calls[tilbakeblikk._POLLER_PATH][:] = [now] * 1000
    calls = []
    with _backends(lambda body: calls.append(body) or OK_REPLY):
        body = _messages()
    assert _statuses(body) == ["ok", "ok"], "another caller's spend must not bind replay"
    assert len(calls) == 2
    assert tilbakeblikk._replay_calls[tilbakeblikk._POLLER_PATH] == [now] * 1000, (
        "replay must never draw down another caller's allowance"
    )


def test_generation_disabled_reports_disabled_and_calls_nothing():
    """`disabled` means no backend could be ASKED — VARSEL_LLM_PROVIDER=off, no project, or every
    backend latched off. It has never meant "this deployment sends the template"."""
    _reset(message_rows=_subjects(3))
    llm_message._PROVIDER = "off"
    with _backends(_never_called, gemini=_never_called):
        body = _messages()
    assert _statuses(body) == ["disabled"] * 3
    assert [m["message"] for m in body["messages"]] == [None] * 3
    # The template arm is still built: it needs no backend, and a card showing a bound with no text
    # at all would be strictly less useful than one showing the wording that would have been sent.
    assert all(m["deterministic_message"] for m in body["messages"])


def test_the_delivery_gate_is_never_consulted():
    """The assertion that separates this surface from varsel, and the reason prd is worth pointing it
    at. prd runs generation ON and delivery OFF — it writes the model's wording and sends the
    template — so a page that asked `delivery_enabled()` would be blank in exactly the deployment
    whose journeys are real."""
    _reset(message_rows=[_message_row()])
    llm_message._ENABLED = False  # travellers get the template; this page must not care
    with _backends(lambda body: OK_REPLY):
        body = _messages()
    assert body["messages"][0]["message"] == "Toget er innstilt. Ta neste avgang."
    assert llm_message.delivery_enabled() is False, "the gate really was off"

    # And structurally, so the behaviour above cannot start passing for the wrong reason: neither
    # gate name is *referenced* anywhere in the module. Parsed rather than grepped, because both
    # names appear in its prose — explaining why they are not consulted is half the docstring.
    import ast

    tree = ast.parse(Path(tilbakeblikk.__file__).read_text())
    referenced = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    referenced |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "delivery_enabled" not in referenced
    assert "_deliverable_message" not in referenced
    assert "generation_enabled" in referenced, "the gate it DOES ask must still be asked"


def test_a_subject_with_no_stored_input_is_its_own_state_and_costs_nothing():
    """R7. No alert-worthy cycle in the window recorded a writer's input, so there is nothing to
    replay. Not "no message", not an error, and — the part that matters for the page filling up — not
    charged against the cap, since no call was made for it."""
    _reset(message_rows=[_message_row(situation_number="S0", message_input=None), _message_row()])
    calls = []
    with _backends(lambda body: calls.append(body) or OK_REPLY):
        body = _messages()
    first, second = body["messages"]
    assert first["status"] == "no_input"
    assert first["message"] is None and first["deterministic_message"] is None
    assert second["status"] == "ok"
    assert len(calls) == 1, "the empty subject must not have been sent to a backend"
    # Not merely "no call was made" — no budget was CHARGED either. A subject charged for a call it
    # never made would eat the allowance of subjects further down the list, and on a window where
    # most subjects never crossed the alert gate that is most of the page.
    assert len(tilbakeblikk._replay_calls[tilbakeblikk._REPLAY_PATH]) == (
        tilbakeblikk._CALLS_PER_SUBJECT
    ), "exactly one subject's worth of budget was spent"


def test_a_configuration_error_latches_only_the_replay_holder():
    """R15, and the failure it prevents is silent on the traveller-facing side. A replay burst that
    drives both backends into a configuration error would, on a shared latch, make
    `varsel.check_and_notify` fall back to the template for every real push for the next 30 minutes,
    and spend a message attempt per subject on each None."""
    _reset(message_rows=_subjects(2))

    def dead(body):
        raise llm_message._ConfigurationError("404 publisher model not found")

    with _backends(dead, gemini=dead):
        body = _messages()
    assert set(_statuses(body)) == {"unavailable"}
    assert set(tilbakeblikk._replay_state.latched_off) == {"claude", "gemini"}
    assert llm_message._STATE.latched_off == {}, "the live push path's latch must be untouched"
    assert llm_message.generation_enabled() is True, "the poller can still write a wording"


def test_a_transient_backend_failure_is_reported_per_subject():
    """One subject's Vertex problem must cost one card, never the page. A transient failure is also
    the one that does NOT try the other backend — `write_message_detailed` returns immediately — so
    it is a single call, and the subjects after it still get theirs."""
    _reset(message_rows=_subjects(3))

    def flaky(body):
        if json.loads(body)["sak"] == "S1":
            raise RuntimeError("deadline exceeded")
        return OK_REPLY

    with _backends(flaky):
        body = _messages()
    assert _statuses(body) == ["ok", "error", "ok"]
    assert body["messages"][1]["message"] is None
    assert body["messages"][1]["deterministic_message"], "the template arm survives the failure"


def test_the_deadline_reserves_the_worst_case_rather_than_measuring_elapsed_time():
    """A bound that checked elapsed time would bound when the last call STARTS, not when the pass
    ends: a call started one millisecond inside the deadline can still run for the full worst case.
    Reserving it is what makes the 30 s a promise. And the check has to run inside the worker as its
    task starts — submitted all at once, every task queues before the deadline is consulted and the
    bound guarantees nothing at all.

    Nothing downstream cuts a hang off: the chart sets no ingress timeout. So the contract is 200
    with what was answered, never a hang and never a 5xx."""
    _reset(message_rows=_subjects(8))
    deadline_s, worst_case = tilbakeblikk._REQUEST_DEADLINE_S, tilbakeblikk._WORST_CASE_CALL_S
    tilbakeblikk._REQUEST_DEADLINE_S = 0.6
    tilbakeblikk._WORST_CASE_CALL_S = 0.4
    calls = []

    def slow(body):
        calls.append(body)
        _time.sleep(0.3)
        return OK_REPLY

    try:
        with _backends(slow):
            r = client.get("/tilbakeblikk-api/messages")
    finally:
        tilbakeblikk._REQUEST_DEADLINE_S = deadline_s
        tilbakeblikk._WORST_CASE_CALL_S = worst_case

    assert r.status_code == 200, "a bounded pass answers; it never 5xxs and never hangs"
    statuses = _statuses(r.json())
    assert len(calls) == tilbakeblikk._REPLAY_WORKERS, (
        "only the first round of workers may start; the next round must find the deadline reserved"
    )
    assert statuses.count("ok") == len(calls)
    assert statuses.count("deadline") == 8 - len(calls)
    assert "capped" not in statuses, "the deadline bound here, not the per-request cap"


def test_the_route_is_a_coroutine_so_it_cannot_hold_an_anyio_worker_slot():
    """Every other route in this process is a plain `def` and is served on the shared anyio worker
    threadpool — /impact, the /varsel-api device and journey writes, the /historikk routes. A
    handler that can hold one of those slots for its full 30 s deadline would starve
    traveller-facing registration from a public unauthenticated endpoint, and with no ingress
    timeout nothing reclaims the slot. Declared `async`, this one only awaits: both the BigQuery read
    and the generation fan-out run on the request's own pool."""
    assert inspect.iscoroutinefunction(tilbakeblikk.messages)
    assert not inspect.iscoroutinefunction(tilbakeblikk.decisions), (
        "the comparison the check is about: /decisions IS on the shared pool"
    )


def test_the_two_arms_are_built_from_one_input():
    """The page compares two writers on the same facts. Two rebuilds of "the same" input would make
    it compare the rebuilds as well, which is the defect the 2026-08-05 message_input change removed
    from the decision log one layer down."""
    _reset(message_rows=[_message_row()])
    with _backends(lambda body: OK_REPLY):
        body = _messages()
    entry = body["messages"][0]
    assert deterministic_inputs == [json.loads(INPUT_JSON)], (
        "the template writer must be handed the SAME parsed document the prompt was built from"
    )
    assert entry["deterministic_message"] == "Mal for R11: avviket kan påvirke reisen din."
    assert entry["message"] != entry["deterministic_message"], "two arms, not one"


def test_an_uninjected_template_writer_costs_the_arm_not_the_page():
    """Fail-soft, because the template is the comparison arm and not the answer."""
    _reset(message_rows=[_message_row()])
    tilbakeblikk._deterministic_writer = None
    try:
        with _backends(lambda body: OK_REPLY):
            body = _messages()
    finally:
        tilbakeblikk._deterministic_writer = _fake_deterministic
    entry = body["messages"][0]
    assert entry["status"] == "ok" and entry["message"]
    assert entry["deterministic_message"] is None


def test_an_ungrounded_reply_returns_what_the_model_actually_wrote():
    """`ungrounded` and `rejected` are the two statuses a prompt edit most plausibly produces, and
    this page exists to iterate on the prompt. A status badge with no text would hide precisely the
    thing being worked on. It is never delivered and never stored on a decision row — the guards that
    refused it have not moved."""
    _reset(message_rows=[_message_row()])
    with _backends(lambda body: UNGROUNDED_REPLY, gemini=lambda body: ""):
        body = _messages()
    entry = body["messages"][0]
    assert entry["status"] == "ungrounded"
    assert entry["message"] is None, "a wording quoting a figure nobody measured is not served"
    assert "42" in entry["discarded"], "but a developer must be able to read what it wrote"


def test_the_messages_response_carries_no_input_and_no_journey_identifier():
    """KTD10, inherited verbatim and extended by one column. The row this entry is built from carries
    `journey_digest` and `message_input`; neither may cross the boundary. What leaves the process is
    the wording generated FROM the input, never the input.

    The whole key set is asserted, so a future field addition fails here rather than shipping."""
    _reset(message_rows=[_message_row()])
    with _backends(lambda body: OK_REPLY):
        body = _messages()
    entry = body["messages"][0]
    assert not {"journey_digest", "message_input", "from_stop", "to_stop", "jkey"} & set(entry)
    assert set(entry) == {
        "situation_number", "line", "mode", "departure_time", "would_send_at",
        "status", "message", "deterministic_message", "discarded", "provider", "model",
        "prompt_version", "current_prompt_version",
    }


def test_the_message_model_refuses_a_redacted_column():
    """Second line of defence, the same one /decisions has: `extra="forbid"` makes a future field
    fail loudly here instead of quietly widening what an unauthenticated public host serves."""
    ok = {
        "situation_number": "S1", "line": "VYG:Line:R11", "mode": "Tog", "departure_time": DEP,
        "would_send_at": DEP, "status": "ok", "message": "x", "deterministic_message": "y",
        "discarded": None, "provider": "claude", "model": "gemini-3.7-flash",
        "prompt_version": "a1b2c3d4", "current_prompt_version": "b2c3d4e5",
    }
    tilbakeblikk.ReplayedMessage(**ok)  # the shape itself must be valid
    for redacted in ("journey_digest", "message_input", "from_stop", "to_stop"):
        try:
            tilbakeblikk.ReplayedMessage(**ok, **{redacted: "x"})
            raise AssertionError(f"ReplayedMessage must reject {redacted}")
        except ValidationError:
            pass


def test_messages_takes_the_same_allow_list_as_decisions():
    """It never accepts a caller-supplied row key: what may be regenerated is decided by this
    module's own query over a vocabulary /filters published. An arbitrary string would otherwise mint
    one cache entry, one BigQuery query and a fan of Vertex calls apiece, on a public endpoint."""
    accepted = set(inspect.signature(tilbakeblikk.messages).parameters)
    assert accepted == set(inspect.signature(tilbakeblikk.decisions).parameters), (
        "the two lists are aligned by position, so they must be narrowed identically"
    )
    _reset(message_rows=[_message_row()])
    bad = client.get("/tilbakeblikk-api/messages", params={"operator": "NOT-AN-OPERATOR"})
    assert bad.status_code == 400
    assert fake.calls == [], "a rejected filter must not reach BigQuery"


def test_the_wording_cache_is_bounded():
    """2 000 entries, each a wording of at most 300 characters plus its metadata. Memory is the
    binding constraint on this pod — no CPU limit, but always a memory one (1228 Mi in dev),
    forceReplicas 1, and uvicorn binds only after lifespan's eager BigQuery loads, so an OOMKill is
    zero capacity for a whole cold start."""
    _reset()
    wording = tilbakeblikk._Wording("x", "y", "ok", None, "claude", "m")
    for i in range(tilbakeblikk._WORDING_MAX_ENTRIES + 100):
        tilbakeblikk._remember_wording((i,), wording)
    assert len(tilbakeblikk._wordings) <= tilbakeblikk._WORDING_MAX_ENTRIES


# --------------------------------------------------------------------------------------------
# The SQL text
#
# Nothing in this repo executes SQL and CI has no GCP credentials, so a defect in a .sql file ships
# green and only surfaces on a page load. These pin the properties whose failure mode is a silently
# WRONG answer rather than an error — the ones a dry run cannot catch either.
# --------------------------------------------------------------------------------------------

_QUERIES = {
    "decisions": tilbakeblikk._DECISIONS_QUERY,
    "summary": tilbakeblikk._SUMMARY_QUERY,
    "filters": tilbakeblikk._FILTERS_QUERY,
    "messages": tilbakeblikk._MESSAGES_QUERY,
}
# The three that feed the public card model and its rates. `messages` is server-side only.
_CARD_FACING = ("decisions", "summary", "filters")


def _uncommented(sql: str) -> str:
    """The SQL with its `--` comments stripped. Every check below runs on this.

    These files discuss the columns they must not project at length -- that is the point of them --
    so a grep over the raw text would be measuring the prose.
    """
    return "\n".join(ln for ln in sql.splitlines() if not ln.strip().startswith("--"))


def _projections(sql: str) -> list[str]:
    """Every SELECT list in the file, i.e. the text between a SELECT and its FROM."""
    found = re.findall(r"\bSELECT\b(.*?)\bFROM\b", _uncommented(sql), re.DOTALL)
    assert found, "could not find a SELECT list to check"
    return found


def test_the_no_filter_array_test_is_null_safe():
    """A text check, because nothing in this repo executes SQL -- and this defect is invisible
    without executing it.

    An EMPTY array parameter from the Python client serializes as {'arrayValues': []}, and BigQuery
    evaluates ARRAY_LENGTH of that as NULL, not 0. So `ARRAY_LENGTH(@refs) = 0` -- the obvious way to
    spell "no filter" -- is NULL on every unfiltered request, the ANDed predicate drops every row,
    and the whole page reads zero decisions. It shipped that way on /historikk for one commit on
    2026-08-21. A dry run validates it and `bq --parameter=refs:ARRAY<STRING>:[]` returns 0, because
    the CLI encodes an empty array differently from the client library, so neither cheap check
    catches it.
    """
    for name in ("decisions", "summary", "messages"):
        sql = _uncommented(_QUERIES[name])
        bare = re.findall(r"\(ARRAY_LENGTH\(@\w+\)\s*= 0 OR", sql)
        assert not bare, f"{name}: {len(bare)} null-unsafe emptiness test(s); use IFNULL(..., 0)"
        assert "IFNULL(ARRAY_LENGTH(" in sql, f"{name}: no array emptiness test found at all"


def test_every_query_prunes_the_decision_log_on_its_partition_column():
    """`varsel_decisions` is PARTITION BY DATE(decided_at), and the exact window predicate is a
    TIMESTAMP range, which does not reliably prune on its own. The explicit DATE() bound is what
    turns a whole-table scan into three partitions — the one cost claim this whole surface rests on,
    and one no test can otherwise check."""
    for name, sql in _QUERIES.items():
        body = _uncommented(sql)
        assert "DATE(decided_at) BETWEEN" in body, f"{name}: no partition bound on decided_at"


def test_the_outcomes_join_carries_its_own_partition_bound():
    """`varsel_decision_outcomes` is PARTITION BY call_date, which a `decided_at` predicate does NOT
    prune — so without this the outcomes read would be strictly more expensive than /historikk's,
    which does prune it, while the query overall claims to be cheaper.

    The width is measured, not reasoned to: dev 2026-08-21, MAX |call_date - Oslo(decided_at)| = 1
    day over 59 694 rows. Too narrow a bound drops outcome rows SILENTLY — the subjects just read as
    unresolved — and the byte counts validate_varsel_sql.sh prints would report that loss as a
    cheaper query, i.e. as success.
    """
    for name in ("decisions", "summary", "messages"):
        body = _uncommented(_QUERIES[name])
        assert "varsel_decision_outcomes" in body, f"{name}: expected an outcomes join"
        assert "call_date BETWEEN" in body, f"{name}: outcomes side is not bounded on its partition"


def test_the_replay_pick_is_one_aggregate_carrying_its_fields_together():
    """Two aggregates "picking the same row" is a promise ORDER BY cannot keep, and its failure here
    is a card quoting one cycle's wording under another cycle's time — the exact defect measured at
    41 % of /historikk's alerted subjects on 2026-08-21, one layer down.

    So `message_input`, `would_send_at` and `prompt_version` must come out of ONE
    ARRAY_AGG(... STRUCT ...), never out of two that happen to share an ORDER BY.
    """
    body = _uncommented(_QUERIES["messages"])
    segments = body.split("ARRAY_AGG(")[1:]
    carrying = [s for s in segments if "message_input" in s]
    assert len(carrying) == 1, (
        f"the replay input must come from exactly one aggregate, found {len(carrying)}"
    )
    assert "STRUCT(" in carrying[0], "the aggregate must carry its fields together in a STRUCT"
    assert "IGNORE NULLS ORDER BY decided_at LIMIT 1" in carrying[0]
    # Ascending: the FIRST alert-worthy cycle, which is the moment a message could first have gone
    # out. DESC would be the last one, which the 2026-08-21 measurement showed is a different row.
    assert "ORDER BY decided_at DESC" not in carrying[0]


def test_the_writers_input_is_named_by_the_messages_query_alone():
    """The split between the two files is a PRIVACY BOUNDARY, not a convenience (KTD10):
    `message_input` embeds origin, destination and departure time together, and a query that does not
    NAME a column cannot leak it however the model above it drifts.

    The card-facing decisions query does reference the column once, and deliberately: only as
    `message_input IS NOT NULL`, reduced to a BOOL (`AS has_input`) that the rest of the file works
    from. That guard is what makes its replay pick land on the SAME cycle the messages query fetches
    the input for -- without it the card would state a send time for a wording the page cannot
    produce. Note the shape is strictly stronger than "do not put it in the outer SELECT": the value
    is never carried through a CTE at all, only a boolean derived from it.

    So the property asserted is "every occurrence is that null test", not "the string is absent",
    which is what a first draft of this check said and what the file legitimately fails.
    """
    body = _uncommented(_QUERIES["decisions"])
    stray = re.findall(r"message_input(?!\s+IS NOT NULL)", body)
    assert not stray, f"decisions: message_input used other than as a null test ({len(stray)}x)"
    assert "message_input IS NOT NULL AS has_input" in body, (
        "the null test must be reduced to a BOOL, so no CTE ever carries the input text"
    )
    for name in ("summary", "filters"):
        assert "message_input" not in _uncommented(_QUERIES[name]), f"{name}: names message_input"
    assert any("message_input" in p for p in _projections(_QUERIES["messages"])), (
        "the messages query must project the input; it is the only file allowed to"
    )
    # And the public model refuses it even if a query ever started returning it — second line of
    # defence, asserted separately in test_the_decision_model_refuses_a_redacted_column.
    for name in _CARD_FACING:
        for projection in _projections(_QUERIES[name]):
            leaks = re.findall(r"message_input(?!\s+IS NOT NULL)", projection)
            assert not leaks, f"{name}: message_input reached a SELECT list as a value"


def test_the_registered_endpoints_are_filterable_but_never_projected():
    """The 2026-08-21 reversal on /historikk, inherited here with its remaining limit intact.

    from_place/to_place are FILTERS and /filters publishes the vocabulary, so a pair can be chosen
    and the matching departures read off. What did NOT change is the per-row redaction: no decision
    carries from_stop or to_stop, so a card still names a train rather than a trip. The filters query
    is the deliberate exemption — it must project both as the dropdown vocabulary (R13).
    """
    for name in ("decisions", "summary"):
        for projection in _projections(_QUERIES[name]):
            assert "from_stop" not in projection and "to_stop" not in projection, (
                f"{name}: a registered endpoint reached a SELECT list"
            )
    assert any("from_stop" in p for p in _projections(_QUERIES["filters"])), (
        "the filters query must project from_stop; it is the dropdown vocabulary"
    )


def test_the_two_lists_are_ordered_identically():
    """/decisions and /messages are aligned POSITION BY POSITION by the caller — the page keys its
    list on position, since nothing served identifies a decision uniquely (the real grain includes
    journey_digest, which is redacted). Any divergence in the ordering silently attaches one train's
    wording to another train's card, and departure_time alone leaves real ties: two registrations on
    the same train and the same situation differ only in journey_digest."""
    def order_by(sql: str) -> str:
        body = _uncommented(sql)
        clause = body[body.rindex("ORDER BY"):]
        return re.sub(r"\s+", " ", clause).strip()

    assert order_by(_QUERIES["decisions"]) == order_by(_QUERIES["messages"])
    assert "journey_digest" in order_by(_QUERIES["decisions"]), (
        "the tie-break must reach the full subject key, or two rows can swap between the two queries"
    )


def test_the_new_query_files_are_registered_in_the_validator():
    """R19, and the check is on the PARAMETERS rather than only the filenames.

    A parameter a file uses and the validator's list omits is not a soft warning -- bq refuses to
    parse the query at all ("Query parameter 'line' not found"), so the check reports FAILED and the
    file goes UNVALIDATED until somebody notices. That is exactly how /historikk's filter parameters
    slipped through on 2026-08-21.
    """
    script = (Path(__file__).parents[2] / "data/queries/validate_varsel_sql.sh").read_text()
    for name, sql in _QUERIES.items():
        filename = f"tilbakeblikk_{name}.sql"
        assert filename in script, f"{filename} is not dry-run by validate_varsel_sql.sh"
        for param in sorted(set(re.findall(r"@(\w+)", _uncommented(sql)))):
            assert f"parameter={param}:" in script or f"parameter={param}<" in script, (
                f"{filename} uses @{param}, which the validator does not supply — the dry run would "
                f"fail to parse and the file would go unvalidated"
            )


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} tilbakeblikk checks passed.")


if __name__ == "__main__":
    _run()
