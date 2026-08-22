"""Checks for /meldinger-api — a window of past alert decisions, replayed through today's prompt.

Hermetic, exactly like test_historikk.py: BigQuery is replaced with a fake that records the SQL, the
parameters and the job config it was handed and replays canned rows, so nothing here touches
BigQuery or the network.

Three families of check carry most of the weight, and each of them guards a defect that is silent
rather than loud:

* **The window.** Both times are quantized to 15-minute steps and the range is half-open, because
  every parameter lands in the response-cache key on a public unauthenticated endpoint. Two
  free-form second-resolution times are on the order of a billion keys per day, each one a cache
  entry and a BigQuery query. The checks assert the bound rather than assuming it.
* **The redaction.** Decision rows carry display names for the registered endpoints, but never
  `journey_digest`, raw `from_stop`/`to_stop` refs, or `message_input`.
* **The SQL text.** Nothing in this repo executes SQL and CI has no GCP credentials, so a defect in
  a .sql file ships green. The grep-level checks at the bottom pin the properties whose failure mode
  is a silently wrong answer rather than an error: the null-unsafe array test, the partition bounds,
  and the single-aggregate replay pick.

Run: uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery \
     --with google-cloud-storage --with pywebpush python test_meldinger.py   (or under pytest)
"""

import asyncio
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time as _time
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# --- Stub the BigQuery client BEFORE importing the module (main builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import llm_message
import meldinger
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

OSLO = ZoneInfo("Europe/Oslo")
DEP = datetime(2026, 7, 30, 6, 15, tzinfo=UTC)

# A day safely inside the lookback, so a check does not start failing when the clock rolls over.
PAST_DAY = datetime.now(OSLO).date() - timedelta(days=3)


def _row(**overrides) -> dict:
    """A decision row as meldinger_decisions.sql returns it; override per test."""
    base = {
        "situation_number": "S1",
        "summary": "Signalfeil ved Asker",
        "description": "Det er meldt om signalfeil.",
        "operator": "VYG",
        "line": "VYG:Line:R11",
        "mode": "Tog",
        "from_stop": "NSR:StopPlace:337",
        "to_stop": "NSR:StopPlace:548",
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
        "send_state": "sendt",
        "other_pushed_on_train": 0,
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
    """A replay row as meldinger_messages.sql returns it — subject key, input, cycle.

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
        if sql == meldinger._FILTERS_QUERY:
            rows = self.filter_rows
        elif sql == meldinger._STOP_PLACES_QUERY:
            rows = self.registry_rows
        elif sql == meldinger._MESSAGES_QUERY:
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


meldinger.configure(bq_client=fake, deterministic_writer=_fake_deterministic)

app = FastAPI()
app.include_router(meldinger.router)
client = TestClient(app)


def _filter_row(**overrides) -> dict:
    """The vocabulary /meldinger-api/filters returns, matching the line and places _row() names."""
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
    meldinger._cache.clear()  # the TTL cache would otherwise serve a previous test's rows
    # The replay state, all four pieces of it. Leaving any one behind makes a later check pass for
    # the wrong reason: a warm wording cache hides a missing backend call, a spent replay tally
    # makes a budget check "bind" before it should, and a latched replay holder reports `disabled`
    # where a test expects an answer.
    meldinger._wordings.clear()
    meldinger._wording_inflight.clear()
    for spent in meldinger._replay_calls.values():
        spent.hour.clear()
        spent.day.clear()
    meldinger._replay_state = None
    meldinger._warned_no_writer = False
    deterministic_inputs.clear()
    # llm_message's own globals: this suite must never depend on the ambient environment of whoever
    # runs it (VARSEL_LLM_PROVIDER, GOOGLE_CLOUD_PROJECT), and the module-global latch is what the
    # isolation checks assert has NOT moved.
    llm_message._PROVIDER = "auto"
    llm_message._PROJECT = "ent-kixavvik-test"
    llm_message._ENABLED = True
    llm_message._STATE = llm_message._LatchState()


def _decision_calls():
    return [c for c in fake.calls if c[0] == meldinger._DECISIONS_QUERY]


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

    module_logger = logging.getLogger("meldinger")
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
    real = meldinger.datetime

    class _Clock:
        @staticmethod
        def now(tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

        combine = staticmethod(real.combine)

    meldinger.datetime = _Clock
    try:
        yield
    finally:
        meldinger.datetime = real


def test_the_day_defaults_to_the_oslo_calendar_day_not_the_utc_one():
    """The page asks about a stretch of a LOCAL day, and for two hours every night the two calendars
    disagree: at 22:30 UTC it is already the 21st in Oslo. A UTC-resolved default would open the page
    on yesterday — showing an empty or half-finished day to whoever is looking at it late in the
    evening, which is when a disruption surface is most likely to be open."""
    _reset([_row()])
    with _clock_at(datetime(2026, 8, 20, 22, 30, tzinfo=UTC)):
        body = client.get("/meldinger-api/decisions").json()
    assert body["day"] == "2026-08-21", "the default day must be the Oslo calendar day"
    # ...and the window it resolves to is that Oslo day, not a UTC one: 22:00 UTC to 22:00 UTC.
    _, params = _decision_calls()[0]
    assert params["from_ts"] == _oslo_midnight(date(2026, 8, 21))
    assert params["to_ts"] == _oslo_midnight(date(2026, 8, 22))


def test_the_day_defaults_to_today_in_oslo():
    """The ordinary path, against the ambient clock — the frozen-clock check above is what actually
    discriminates the timezone."""
    _reset([_row()])
    body = client.get("/meldinger-api/decisions").json()
    assert body["day"] == datetime.now(OSLO).date().isoformat()


def test_the_time_range_defaults_to_the_whole_day_as_a_half_open_range():
    """00:00 to the FOLLOWING midnight, and both reach the query as real timestamps rather than
    NULLs.

    23:59:59.999999 is not on a 15-minute grid, so a default expressed that way would either have to
    escape the quantization or be rounded down — silently dropping the last quarter-hour of every
    default request, which is the busiest part of an evening window.
    """
    _reset([_row()])
    body = client.get("/meldinger-api/decisions").json()
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
        "/meldinger-api/decisions",
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
        "/meldinger-api/decisions",
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
            "/meldinger-api/decisions",
            params={"day": PAST_DAY.isoformat(), "from_time": minute, "to_time": "15:00"},
        )
    assert len(_decision_calls()) == 1, "three times inside one 15-minute step must share one query"
    assert len(meldinger._cache) == 2  # decision list + shared stop-name lookup


def test_a_reversed_or_empty_time_range_is_rejected_without_a_query():
    """400 rather than an empty page, and no BigQuery call spent on it.

    Equal endpoints are rejected too, not only inverted ones: after quantization an equal pair is an
    empty range exactly when it lands on a step boundary and a non-empty one otherwise, and a rule
    whose behaviour depends on where the clock hands happen to be is worse than a refusal.
    """
    for from_time, to_time in (("14:00", "13:00"), ("14:00", "14:00")):
        _reset([_row()])
        bad = client.get(
            "/meldinger-api/decisions",
            params={"day": PAST_DAY.isoformat(), "from_time": from_time, "to_time": to_time},
        )
        assert bad.status_code == 400, (from_time, to_time)
        assert "to_time" in bad.json()["detail"]
        assert fake.calls == [], "a rejected time range must not reach BigQuery"


def test_a_bare_midnight_to_time_is_rejected_rather_than_silently_empty():
    """The bounds check must run on the RESOLVED minutes, not the raw times.

    The raw comparison can only fire when both times were supplied, so `to_time=00:00` with
    `from_time` omitted walked past it: the default start is minute 0 and midnight ceils to minute 0,
    giving the half-open [0, 0) the docstring promises is impossible — a 200 with a permanently empty
    window, on the one endpoint whose whole job is to describe a window. The shipped page never sends
    it (api.ts omits `to_time` when it equals midnight), but the API is public and documented.

    The `count == 0` assertion is the one that would have caught this: a request that reaches
    BigQuery and comes back empty is indistinguishable, from the client's side, from a quiet day.
    """
    for params in (
        {"to_time": "00:00"},                      # the hole: only `to_time`, at midnight
        {"to_time": "00:00:00"},                   # ...with explicit seconds, as FastAPI parses it
        {"from_time": "00:00", "to_time": "00:00"},  # the pair the raw check already refused
    ):
        _reset([_row()])
        bad = client.get(
            "/meldinger-api/decisions",
            params={"day": PAST_DAY.isoformat(), **params},
        )
        assert bad.status_code == 400, (params, bad.status_code, bad.json())
        assert "to_time" in bad.json()["detail"]
        assert fake.calls == [], "a rejected time range must not reach BigQuery"


def test_a_valid_range_still_survives_the_resolved_bounds_check():
    """The guard moved after quantization, so pin that it did not start refusing valid ranges.

    Quantization preserves order for anything already valid — `from < to` implies
    `floor(from) < ceil(to)` — and the sub-step range is the case that proves it: 14:00 to 14:01
    is empty before rounding and a full quarter-hour after it, which is exactly the asymmetry
    `_ceil_to_step` exists to create.

    Reaching BigQuery is the assertion, not the rendered times: a range ending at the FOLLOWING
    midnight serves `to_time` as "00:00", so ordering the two strings would fail on the widest
    legitimate window there is.
    """
    for from_time, to_time in (("14:00", "14:01"), ("00:00", "23:59"), ("14:00", "14:15")):
        _reset([_row()])
        ok = client.get(
            "/meldinger-api/decisions",
            params={"day": PAST_DAY.isoformat(), "from_time": from_time, "to_time": to_time},
        )
        assert ok.status_code == 200, (from_time, to_time, ok.json())
        assert fake.calls, f"{from_time}-{to_time} was refused; it is a valid range"


def test_tomorrow_is_allowed_because_the_axis_is_departure_and_not_decision():
    """A consequence of the departure axis, not a loosening of the bound.

    The poller scores about 12 h forward, so at 23:00 there are already real decisions about a 00:30
    departure tomorrow. On the old `decided_at` axis those cycles sat on today and tomorrow was
    genuinely empty; on this one they belong to tomorrow's window, and refusing it would hide them.
    Two days ahead still cannot hold a decision, so it stays key-space and stays refused.
    """
    tomorrow = datetime.now(OSLO).date() + timedelta(days=1)
    _reset([_row()])
    ok = client.get("/meldinger-api/decisions", params={"day": tomorrow.isoformat()})
    assert ok.status_code == 200, ok.text
    assert fake.calls, "tomorrow must reach BigQuery, not be refused"

    _reset([_row()])
    day_after = datetime.now(OSLO).date() + timedelta(days=2)
    bad = client.get("/meldinger-api/decisions", params={"day": day_after.isoformat()})
    assert bad.status_code == 400
    assert "future" in bad.json()["detail"]
    assert fake.calls == []


def test_a_day_beyond_the_lookback_is_rejected_without_a_query():
    """The day is in the cache key, so an unbounded range of days is an unbounded number of cache
    entries and BigQuery queries on a public unauthenticated endpoint."""
    import historikk

    _reset([_row()])
    ancient = datetime.now(OSLO).date() - timedelta(days=historikk._MAX_WINDOW_DAYS + 1)
    bad = client.get("/meldinger-api/decisions", params={"day": ancient.isoformat()})
    assert bad.status_code == 400
    assert "too far back" in bad.json()["detail"]
    assert fake.calls == []


def test_the_window_is_echoed_in_full_on_every_data_response():
    """Four fields for one range: two the picker needs, two the contract needs. A client that had to
    re-derive the instants from a day and a time would have to re-implement Europe/Oslo and its two
    irregular days, and would get the exclusive end wrong."""
    _reset([_row()])
    body = client.get(
        "/meldinger-api/decisions",
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
            "/meldinger-api/decisions", params={"day": PAST_DAY.isoformat(), **extra}
        )
    assert len(_decision_calls()) == len(variants)


def test_identical_requests_are_served_from_cache():
    _reset([_row()])
    client.get("/meldinger-api/decisions")
    client.get("/meldinger-api/decisions")
    assert len(_decision_calls()) == 1, "the second identical request should be served from cache"


def test_cache_is_bounded():
    """A public endpoint whose cache key carries caller-chosen values needs a cap. This pod never
    restarts, and its memory limit is the binding constraint — an OOMKill is zero capacity for the
    length of a cold start, because uvicorn binds only after lifespan's eager BigQuery loads."""
    _reset([_row()])
    for i in range(meldinger._CACHE_MAX_ENTRIES + 50):
        day = datetime.now(OSLO).date() - timedelta(days=i % 90)
        client.get("/meldinger-api/decisions", params={"day": day.isoformat()})
    assert len(meldinger._cache) <= meldinger._CACHE_MAX_ENTRIES


# --------------------------------------------------------------------------------------------
# The allow-list
# --------------------------------------------------------------------------------------------


def test_unknown_operator_is_rejected_without_a_query():
    """An unserved codespace must not reach BigQuery or mint a cache entry: the endpoint is public
    and `operator` is part of the cache key, so an arbitrary string would buy one cached query per
    distinct value."""
    _reset([_row()])
    bad = client.get("/meldinger-api/decisions", params={"operator": "NOT-AN-OPERATOR"})
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
        bad = client.get("/meldinger-api/decisions", params=param)
        assert bad.status_code == 400, param
        assert expected in bad.json()["detail"]
        assert _decision_calls() == [], "a rejected filter must not reach the decisions query"


def test_verdict_filter_is_validated():
    _reset([_row()])
    assert client.get("/meldinger-api/decisions", params={"verdict": "hit"}).status_code == 200
    _reset([_row()])
    bad = client.get("/meldinger-api/decisions", params={"verdict": "definitely-not"})
    assert bad.status_code == 400
    assert "verdict must be one of" in bad.json()["detail"]
    assert fake.calls == []


def test_an_unfiltered_request_skips_the_allow_list_but_resolves_route_names():
    """The default load is the common one and needs no vocabulary to validate. Checking it
    unconditionally would double every page load's BigQuery cost for nothing."""
    _reset([_row()])
    client.get("/meldinger-api/decisions")
    assert [c[0] for c in fake.calls] == [
        meldinger._DECISIONS_QUERY,
        meldinger._STOP_PLACES_QUERY,
    ]


def test_the_allow_list_itself_is_keyed_on_the_window_alone():
    """So every filter combination on the page shares one vocabulary lookup — which is what makes it
    affordable to run the check on the validation path of /decisions and /summary at all."""
    _reset([_row()], filter_rows=[
        _filter_row(from_places=[
            {"ref": "NSR:StopPlace:337", "n": 9},
            {"ref": "NSR:StopPlace:548", "n": 4},
        ])
    ])
    client.get("/meldinger-api/decisions", params={"from_place": "NSR:StopPlace:337"})
    client.get("/meldinger-api/decisions", params={"from_place": "NSR:StopPlace:548"})
    assert len(_decision_calls()) == 2
    assert len([c for c in fake.calls if c[0] == meldinger._FILTERS_QUERY]) == 1


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
    body = client.get("/meldinger-api/filters").json()
    assert body["from_places"] == [
        {"ref": "NSR:StopPlace:418", "name": "Asker stasjon", "n_refs": 2}
    ], "one row per physical place, nominating the busiest ref"

    client.get("/meldinger-api/decisions", params={"from_place": "NSR:StopPlace:418"})
    _, params = _decision_calls()[0]
    assert params["from_place_refs"] == ["NSR:StopPlace:3247", "NSR:StopPlace:418"]


def test_filters_lists_the_windows_vocabulary_and_echoes_the_window():
    _reset([_row()])
    r = client.get(
        "/meldinger-api/filters",
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
    fake.error_on = meldinger._STOP_PLACES_QUERY
    try:
        r = client.get("/meldinger-api/filters")
    finally:
        fake.error_on = None
    assert r.status_code == 200, r.text
    assert r.json()["from_places"] == [{"ref": "NSR:StopPlace:337", "name": None, "n_refs": 1}]


def test_limit_above_the_cap_is_fastapis_422_not_this_modules_400():
    """Two different refusals, and a client has to be able to tell them apart: 422 means the request
    was malformed against the schema, 400 means it was well-formed and named something that does not
    exist in this window."""
    _reset([_row()])
    assert client.get("/meldinger-api/decisions", params={"limit": 10_000}).status_code == 422


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
    accepted = set(inspect.signature(meldinger.summary).parameters)
    assert not {"verdict", "alerted_only", "resolved_only", "sx_only"} & accepted
    assert {"day", "from_time", "to_time", "operator", "line", "from_place", "to_place"} <= accepted

    _reset([{"n_hit": 1}])
    r = client.get("/meldinger-api/summary", params={"line": "VYG:Line:R11"})
    assert r.status_code == 200, r.text
    _, params = next(c for c in fake.calls if c[0] == meldinger._SUMMARY_QUERY)
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
    s = client.get("/meldinger-api/summary").json()
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
    s = client.get("/meldinger-api/summary").json()
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
    s = client.get("/meldinger-api/summary").json()
    assert s["n_subjects"] == 0 and s["n_labelled"] == 0
    assert s["precision"] is None and s["recall"] is None
    assert s["predicted"]["n_hit"] == 0 and s["predicted"]["precision"] is None


# --------------------------------------------------------------------------------------------
# The response shape and the redaction
# --------------------------------------------------------------------------------------------


def test_decisions_returns_rows_with_the_replay_fields():
    _reset([_row()])
    r = client.get("/meldinger-api/decisions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    d = body["decisions"][0]
    assert d["verdict"] == "hit"
    assert d["would_send_at"] is not None
    assert d["send_state"] == "sendt"
    assert d["prompt_version"] == "a1b2c3d4"


def test_a_subject_with_no_replayable_cycle_is_its_own_state():
    """R7. No delivered cycle recorded a writer's input, so there is nothing to replay — which is NOT
    "no message" and not an error. The row still appears, with the replay fields empty and
    `send_state` naming which of the several reasons applies."""
    _reset([_row(
        would_send_at=None, prompt_version=None, send_state="under_terskel",
        ever_alerted=False, pushed_any=False,
    )])
    d = client.get("/meldinger-api/decisions").json()["decisions"][0]
    assert d["would_send_at"] is None
    assert d["prompt_version"] is None
    assert d["send_state"] == "under_terskel"


def test_an_alert_worthy_subject_collapsed_into_a_sibling_says_so():
    """`send_state` is what separates "we stayed quiet about this train" from "we told them, under
    another situation number". The second is one_alert_per_train's collapse loser, and it is the
    common case: operators publish several SX messages about one disruption, and only the most
    severe survives into `_notified`. A card that reported both as "ikke sendt" would read as an
    alerting failure where the traveller was in fact notified."""
    _reset([_row(
        would_send_at=None, prompt_version=None, send_state="samlet",
        ever_alerted=True, pushed_any=False, other_pushed_on_train=1,
    )])
    d = client.get("/meldinger-api/decisions").json()["decisions"][0]
    assert d["send_state"] == "samlet"
    assert d["pushed_any"] is False
    assert d["other_pushed_on_train"] == 1


def test_response_carries_route_names_but_no_internal_identifiers_or_writer_input():
    """The public response names the route while keeping its internal keys and prompt input private.

    This asserts the WHOLE key set, so a future field addition fails here rather than shipping.
    Note what is absent besides the redacted four: `message` and `message_source`. The stored wording
    is deliberately not on this card (R12) — showing the pushed text beside one regenerated from a
    possibly different cycle would reintroduce exactly the mismatch the 2026-08-21 fix removed.
    """
    _reset([_row()])
    d = client.get("/meldinger-api/decisions").json()["decisions"][0]
    assert d["from_name"] == "Oslo S"
    assert d["to_name"] == "Asker stasjon"
    assert not {"journey_digest", "from_stop", "to_stop", "stop_place", "message_input"} & set(d)
    assert not {"message", "message_source"} & set(d)
    assert set(d) == {
        "situation_number", "summary", "description", "operator", "line", "mode", "from_name",
        "to_name", "call_date",
        "departure_time", "n_decisions", "first_decided_at", "last_decided_at",
        "ever_alerted", "first_alert_at", "alert_lead_min",
        "max_impact_probability", "final_impact_probability", "final_cancel_probability",
        "final_delay_p50_min", "final_delay_p90_min", "alert_threshold", "et_cancelled",
        "pushed_any", "n_devices", "other_pushed_on_train", "would_send_at", "send_state",
        "prompt_version",
        "actual_cancelled", "actual_dep_delay_min", "actual_trouble", "verdict", "outcome_state",
    }


def test_route_names_fall_back_to_refs_when_the_stop_registry_is_unavailable():
    _reset([_row()])
    fake.error_on = meldinger._STOP_PLACES_QUERY
    d = client.get("/meldinger-api/decisions").json()["decisions"][0]
    assert d["from_name"] == "NSR:StopPlace:337"
    assert d["to_name"] == "NSR:StopPlace:548"


def test_the_decision_model_refuses_a_redacted_column():
    """`extra="forbid"` is what keeps the SELECT honest: it makes the SQL and this model agree column
    for column, so a future query that picks up a redacted one fails loudly here instead of quietly
    widening what an unauthenticated public host serves."""
    public_row = _row()
    public_row["from_name"] = "Oslo S"
    public_row["to_name"] = "Asker stasjon"
    public_row.pop("from_stop")
    public_row.pop("to_stop")
    for redacted in ("journey_digest", "from_stop", "to_stop", "message_input"):
        try:
            meldinger.Decision(**public_row, **{redacted: "x"})
            raise AssertionError(f"Decision must reject {redacted} — extra='forbid' is the guard")
        except ValidationError:
            pass


def test_the_job_config_caps_what_one_request_can_bill():
    """Nothing else in this repo sets maximum_bytes_billed, and KTD3's cost argument is a pruning
    claim no test can check — the suites are hermetic and no .sql here is ever executed. The cap is
    the backstop: it bounds the blast radius of a query that starts reading something big, on a
    public unauthenticated endpoint that shares its project quota with the poller."""
    _reset([_row()])
    client.get("/meldinger-api/decisions")
    assert fake.job_configs, "no query was made"
    for job_config in fake.job_configs:
        assert job_config.maximum_bytes_billed == meldinger._MAX_BYTES_BILLED


# --------------------------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------------------------


def test_missing_tables_report_unavailable_not_a_traceback():
    _reset([], error=RuntimeError("404 Not found: Table disruptions.varsel_decisions"))
    r = client.get("/meldinger-api/decisions")
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
        assert client.get("/meldinger-api/summary").status_code == 503
        queries_so_far = len(fake.calls)
        r = client.get("/meldinger-api/config")
    assert r.status_code == 200
    assert r.json()["environment"] == "dev"
    assert len(fake.calls) == queries_so_far, "/config must not query BigQuery"

    with _deployed_as("ent-kixavvik-prd"):
        assert client.get("/meldinger-api/config").json()["environment"] is None


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
    """GET /meldinger-api/messages, returning the parsed body and asserting it answered."""
    r = client.get("/meldinger-api/messages", params=params)
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
    assert all(jc.maximum_bytes_billed == meldinger._MAX_BYTES_BILLED for jc in fake.job_configs)


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
        meldinger._cache.clear()  # so the narrower window really re-reads
        _messages(day=PAST_DAY.isoformat(), from_time="06:00", to_time="07:00")
    assert len(calls) == 2, "a different replayed cycle is a different wording"


def test_one_request_can_generate_every_subject_the_endpoint_may_return():
    """The hourly and daily ceilings own spend now. The request cap must not make a normal page
    require repeated reloads: the deployed Gemini-only provider makes one call per subject."""
    _reset(message_rows=_subjects(12))
    llm_message._PROVIDER = "gemini"
    calls = []
    with _backends(
        _never_called,
        gemini=lambda body: calls.append(body) or OK_REPLY,
    ):
        body = _messages()
    assert meldinger._CALLS_PER_ATTEMPT == 1
    assert meldinger._MAX_CALLS_PER_REQUEST >= meldinger._MAX_LIMIT
    assert len(calls) == 12, "Gemini is called exactly once for each subject"
    assert _statuses(body) == ["ok"] * 12


def test_a_second_request_serves_every_subject_from_cache():
    """Once a deployment has generated a subject, page reloads spend no further backend calls."""
    _reset(message_rows=_subjects(12))
    seen = []

    def claude(body):
        seen.append(json.loads(body)["sak"])
        return OK_REPLY

    with _backends(claude):
        first = _messages()
        second = _messages()
    assert len(seen) == 12 and len(set(seen)) == 12
    assert _statuses(first) == ["ok"] * 12
    assert _statuses(second) == ["ok"] * 12


def test_concurrent_cold_requests_share_one_generated_wording():
    """The two admitted requests must not spend twice or disagree on a stochastic first draw."""
    _reset()
    llm_message._PROVIDER = "gemini"
    entered = threading.Event()
    release = threading.Event()
    calls = []
    results = []

    def gemini(body):
        calls.append(body)
        entered.set()
        assert release.wait(2)
        return OK_REPLY

    def replay():
        results.append(
            meldinger._replay_one_cached(
                _message_row(),
                budget=meldinger._Budget(),
                deadline=_time.monotonic() + 30,
                generating=True,
            )
        )

    with _backends(_never_called, gemini=gemini):
        first = threading.Thread(target=replay)
        second = threading.Thread(target=replay)
        first.start()
        assert entered.wait(2)
        second.start()
        _time.sleep(0.05)
        release.set()
        first.join(2)
        second.join(2)
    assert len(calls) == 1
    assert results == [results[0], results[0]]


def test_the_hourly_budget_allows_1000_calls_then_binds_across_requests():
    _reset()
    now = _time.monotonic()
    assert meldinger._Budget().reserve(1000, now) is None
    with _captured(logging.WARNING) as records:
        assert meldinger._Budget().reserve(1, now) == "rate_limited"
    assert meldinger._MAX_CALLS_PER_HOUR == 1000
    assert len(_reason(records, "rate_limited")) == 1


def test_the_daily_budget_allows_5000_calls_then_binds_across_requests():
    _reset()
    now = _time.monotonic()
    spent = meldinger._replay_calls[meldinger._REPLAY_PATH]
    spent.day.extend([now - 2 * 60 * 60] * 4999)
    assert meldinger._Budget().reserve(1, now) is None
    assert meldinger._Budget().reserve(1, now) == "rate_limited"
    assert meldinger._MAX_CALLS_PER_DAY == 5000


def test_rolling_budgets_release_calls_at_each_window_boundary():
    _reset()
    now = _time.monotonic()
    spent = meldinger._replay_calls[meldinger._REPLAY_PATH]
    spent.hour.extend([now] * 1000)
    spent.day.extend([now - 2 * 60 * 60] * 4999)
    assert meldinger._Budget().reserve(1, now) == "rate_limited"
    assert meldinger._Budget().reserve(1, now + meldinger._HOUR_WINDOW_S) is None
    assert meldinger._Budget().reserve(1, now + meldinger._DAY_WINDOW_S) is None


def test_every_helm_environment_and_the_base_pin_gemini():
    root = Path(__file__).parents[2] / "helm/kix-avvik"
    values = [root / "values.yaml", *sorted((root / "env").glob("*.yaml"))]
    for path in values:
        assert "VARSEL_LLM_PROVIDER: gemini" in path.read_text(), path


def test_the_budget_is_tallied_against_replays_own_allowance_not_the_pollers():
    """The tally is keyed by caller, in the shape of `alternatives._fetch_calls`. Replay draws on its
    own number, so it can neither starve another caller's share nor be starved by one.

    Be clear about what this buys here: the poller does not call this module, so its slot stays empty
    by construction. The keying is what makes that structural rather than a fact somebody has to keep
    true — and the coupling it defends against is one layer out, at Vertex, where the two really do
    share a quota."""
    _reset(message_rows=_subjects(2))
    now = _time.monotonic()
    poller = meldinger._replay_calls[meldinger._POLLER_PATH]
    poller.hour.extend([now] * 1000)
    poller.day.extend([now] * 1000)
    calls = []
    with _backends(lambda body: calls.append(body) or OK_REPLY):
        body = _messages()
    assert _statuses(body) == ["ok", "ok"], "another caller's spend must not bind replay"
    assert len(calls) == 2
    assert list(poller.hour) == [now] * 1000 and list(poller.day) == [now] * 1000, (
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

    tree = ast.parse(Path(meldinger.__file__).read_text())
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
    assert len(meldinger._replay_calls[meldinger._REPLAY_PATH].hour) == (
        meldinger._CALLS_PER_ATTEMPT
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
    assert set(meldinger._replay_state.latched_off) == {"claude", "gemini"}
    assert llm_message._STATE.latched_off == {}, "the live push path's latch must be untouched"
    assert llm_message.generation_enabled() is True, "the poller can still write a wording"


def test_a_transient_backend_failure_is_retried_once_per_subject():
    """A momentary Vertex failure should recover inside the request without hiding other rows."""
    _reset(message_rows=_subjects(3))
    calls = []

    def flaky(body):
        subject = json.loads(body)["sak"]
        calls.append(subject)
        if subject == "S1" and calls.count(subject) == 1:
            raise RuntimeError("deadline exceeded")
        return OK_REPLY

    with _backends(flaky):
        body = _messages()
    assert _statuses(body) == ["ok", "ok", "ok"]
    assert calls.count("S1") == 2, "the transient failure should get exactly one retry"
    assert len(meldinger._replay_calls[meldinger._REPLAY_PATH].hour) == len(calls), (
        "the retry must spend the same hourly allowance as every other backend call"
    )


def test_a_persistent_backend_failure_stops_after_one_retry():
    """Automatic recovery must not turn an unavailable backend into an unbounded call loop."""
    _reset(message_rows=_subjects(1))
    calls = []

    def unavailable(body):
        calls.append(body)
        raise RuntimeError("service unavailable")

    with _backends(unavailable):
        body = _messages()
    assert _statuses(body) == ["error"]
    assert len(calls) == meldinger._MAX_BACKEND_ATTEMPTS == 2


def test_the_deadline_reserves_the_worst_case_rather_than_measuring_elapsed_time():
    """A bound that checked elapsed time would bound when the last call STARTS, not when the pass
    ends: a call started one millisecond inside the deadline can still run for the full worst case.
    Reserving it is what makes the 30 s a promise. And the check has to run inside the worker as its
    task starts — submitted all at once, every task queues before the deadline is consulted and the
    bound guarantees nothing at all.

    Nothing downstream cuts a hang off: the chart sets no ingress timeout. So the contract is 200
    with what was answered, never a hang and never a 5xx."""
    assert meldinger._WORST_CASE_CALL_S == llm_message._TIMEOUT_S
    _reset(message_rows=_subjects(8))
    deadline_s, worst_case = meldinger._REQUEST_DEADLINE_S, meldinger._WORST_CASE_CALL_S
    meldinger._REQUEST_DEADLINE_S = 0.6
    meldinger._WORST_CASE_CALL_S = 0.4
    calls = []

    def slow(body):
        calls.append(body)
        _time.sleep(0.3)
        return OK_REPLY

    try:
        with _backends(slow):
            r = client.get("/meldinger-api/messages")
    finally:
        meldinger._REQUEST_DEADLINE_S = deadline_s
        meldinger._WORST_CASE_CALL_S = worst_case

    assert r.status_code == 200, "a bounded pass answers; it never 5xxs and never hangs"
    statuses = _statuses(r.json())
    assert len(calls) == meldinger._REPLAY_WORKERS, (
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
    assert inspect.iscoroutinefunction(meldinger.messages)
    assert not inspect.iscoroutinefunction(meldinger.decisions), (
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
    meldinger._deterministic_writer = None
    try:
        with _backends(lambda body: OK_REPLY):
            body = _messages()
    finally:
        meldinger._deterministic_writer = _fake_deterministic
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
    meldinger.ReplayedMessage(**ok)  # the shape itself must be valid
    for redacted in ("journey_digest", "message_input", "from_stop", "to_stop"):
        try:
            meldinger.ReplayedMessage(**ok, **{redacted: "x"})
            raise AssertionError(f"ReplayedMessage must reject {redacted}")
        except ValidationError:
            pass


def test_a_replay_is_refused_when_no_slot_is_free_and_spends_nothing():
    """Admission control, and the thing it protects is not this page's latency.

    `messages` is `async def` precisely so it does NOT hold an anyio worker slot — that escape is
    what stops one replay starving traveller-facing registration. But the anyio pool is the only
    other thing bounding concurrency in this process, so stepping outside it means nothing counts
    these requests at all: N concurrent callers would mean N thread pools, N BigQuery jobs and 4N
    threads on a pod with one replica and a memory limit.

    The refusal must come BEFORE the pool and the query are allocated, which is what the
    `fake.calls` assertion pins — a 503 that has already spent a BigQuery job protects nothing.
    """
    _reset([_row()])
    real = meldinger._replay_slots
    meldinger._replay_slots = asyncio.Semaphore(0)   # both slots taken
    try:
        # Bounded, because the failure mode of deleting the pre-check is a BLOCK, not a wrong answer:
        # `async with` on an exhausted semaphore waits forever, so an unbounded call here would hang
        # CI instead of failing it, and a suite that hangs is one nobody can bisect.
        #
        # A raw DAEMON thread, not a ThreadPoolExecutor, and the reason is the whole point of this
        # guard. Pool threads are non-daemon and `concurrent.futures` joins them from an atexit
        # hook, so a permanently blocked call keeps the interpreter alive after the assertion has
        # already failed -- the timeout fires, the failure is reported, and the process still never
        # exits. Only a daemon thread lets this test fail and the suite finish.
        box: dict = {}
        worker = threading.Thread(
            target=lambda: box.update(r=client.get("/meldinger-api/messages")),
            daemon=True,
        )
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive(), (
            "the request blocked instead of refusing: with no slot free the route must return 503 "
            "immediately, not queue behind a deadline the caller cannot see"
        )
        r = box["r"]
        assert r.status_code == 503, r.text
        assert fake.calls == [], "a refused replay reached BigQuery before being refused"
    finally:
        meldinger._replay_slots = real


def test_the_replay_slot_is_released_so_requests_do_not_leak_capacity():
    """A gate that never gives the slot back is an outage on the third page load.

    Asserted over three sequential requests rather than two: with `_MAX_CONCURRENT_REPLAYS` = 2, a
    release bug that leaked one slot per request still lets the second through, and would pass a
    two-request version of this test while failing in production on the very next call.
    """
    for i in range(3):
        _reset([_row()])
        r = client.get("/meldinger-api/messages")
        assert r.status_code == 200, f"request {i + 1} was refused: {r.text}"
    assert not meldinger._replay_slots.locked(), "slots were not returned"


def test_the_concurrency_ceiling_remains_small_despite_the_larger_spend_budget():
    """A higher spend ceiling must not create unbounded pools, BigQuery jobs, or request threads."""
    assert meldinger._MAX_CONCURRENT_REPLAYS == 2


def test_messages_takes_the_same_allow_list_as_decisions():
    """It never accepts a caller-supplied row key: what may be regenerated is decided by this
    module's own query over a vocabulary /filters published. An arbitrary string would otherwise mint
    one cache entry, one BigQuery query and a fan of Vertex calls apiece, on a public endpoint."""
    accepted = set(inspect.signature(meldinger.messages).parameters)
    assert accepted == set(inspect.signature(meldinger.decisions).parameters), (
        "the two lists are aligned by position, so they must be narrowed identically"
    )
    _reset(message_rows=[_message_row()])
    bad = client.get("/meldinger-api/messages", params={"operator": "NOT-AN-OPERATOR"})
    assert bad.status_code == 400
    assert fake.calls == [], "a rejected filter must not reach BigQuery"


def test_the_wording_cache_expires_failures_but_keeps_answers_until_restart():
    """Transient failures recover quickly; successful and payload-determined results are canonical
    for this process and remain cached until eviction or restart."""
    _reset()
    clock = [1000.0]
    real = meldinger.monotonic
    meldinger.monotonic = lambda: clock[0]
    try:
        ok = meldinger._Wording("prose", "template", "ok", None, "claude", "m")
        err = meldinger._Wording(None, "template", "error", None, None, None)
        meldinger._remember_wording(("ok",), ok)
        meldinger._remember_wording(("err",), err)
        assert meldinger._cached_wording(("ok",)) == ok
        assert meldinger._cached_wording(("err",)) == err

        # Past the failure's life.
        clock[0] += meldinger._WORDING_ERROR_TTL_S + 1
        assert meldinger._cached_wording(("err",)) is None, "a failed draw outlived its short TTL"
        assert meldinger._cached_wording(("ok",)) == ok, "an answer expired on the failure's TTL"

        clock[0] += 365 * 24 * 60 * 60
        assert meldinger._cached_wording(("ok",)) == ok, "an answer expired before restart"
    finally:
        meldinger.monotonic = real


def test_a_refused_payload_is_cached_until_restart():
    """`rejected` and `ungrounded` are payload-determined, so they share the stable-result lifetime.

    The same stored `message_input` under the same PROMPT_VERSION will be refused again — re-asking
    spends a Vertex call to learn nothing. Only `error` says merely that the backend was unreachable
    a moment ago. This is the same cut `varsel` makes with `_message_attempts` vs `_message_errors`,
    expressed here as a cache lifetime rather than a counter, and it is why the two lifetimes must not be
    unified on the grounds that both are failures.
    """
    _reset()
    clock = [1000.0]
    real = meldinger.monotonic
    meldinger.monotonic = lambda: clock[0]
    try:
        for status in ("rejected", "ungrounded"):
            w = meldinger._Wording(None, "template", status, "the refused text", "claude", "m")
            meldinger._remember_wording((status,), w)
        clock[0] += meldinger._WORDING_ERROR_TTL_S + 1
        for status in ("rejected", "ungrounded"):
            assert meldinger._cached_wording((status,)) is not None, (
                f"{status} expired on the transient TTL; it is determined by the input, not the "
                "backend's mood, so re-asking spends a call to learn nothing"
            )
    finally:
        meldinger.monotonic = real


def test_a_token_truncated_reply_expires_on_the_transient_cache_ttl():
    """A fresh draw can fit even when the previous draw exhausted its output-token budget."""
    _reset()
    clock = [1000.0]
    real = meldinger.monotonic
    meldinger.monotonic = lambda: clock[0]
    try:
        wording = meldinger._Wording(
            message=None,
            deterministic="template",
            status="truncated",
            discarded=None,
            provider="gemini",
            model="gemini-3.7-flash",
        )
        meldinger._remember_wording(("truncated",), wording)
        clock[0] += meldinger._WORDING_ERROR_TTL_S + 1
        assert meldinger._cached_wording(("truncated",)) is None
    finally:
        meldinger.monotonic = real


def test_the_wording_cache_is_bounded():
    """10 000 entries, each a wording of at most 300 characters plus its metadata. Memory is the
    binding constraint on this pod — no CPU limit, but always a memory one (1228 Mi in dev),
    forceReplicas 1, and uvicorn binds only after lifespan's eager BigQuery loads, so an OOMKill is
    zero capacity for a whole cold start."""
    _reset()
    wording = meldinger._Wording("x", "y", "ok", None, "claude", "m")
    for i in range(meldinger._WORDING_MAX_ENTRIES + 100):
        meldinger._remember_wording((i,), wording)
    assert len(meldinger._wordings) <= meldinger._WORDING_MAX_ENTRIES


# --------------------------------------------------------------------------------------------
# The SQL text
#
# Nothing in this repo executes SQL and CI has no GCP credentials, so a defect in a .sql file ships
# green and only surfaces on a page load. These pin the properties whose failure mode is a silently
# WRONG answer rather than an error — the ones a dry run cannot catch either.
# --------------------------------------------------------------------------------------------

_QUERIES = {
    "decisions": meldinger._DECISIONS_QUERY,
    "summary": meldinger._SUMMARY_QUERY,
    "filters": meldinger._FILTERS_QUERY,
    "messages": meldinger._MESSAGES_QUERY,
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
    """`varsel_decisions` is PARTITION BY DATE(decided_at), which the window predicate no longer
    touches at all since the axis moved to `departure_time`. The DATE() bound is therefore a DERIVED
    cover for the window — every cycle that could have decided about a departure inside it — and it
    is load-bearing twice over: it is the one cost claim this surface rests on, and narrowing it
    drops cycles out of the per-subject aggregates rather than merely mislabelling a flag.

    Both failures are silent, and the byte counts validate_varsel_sql.sh prints would report the
    second as a CHEAPER query. Nothing here executes SQL, so this text check is the only guard.
    """
    for name, sql in _QUERIES.items():
        body = _uncommented(sql)
        assert "DATE(decided_at) BETWEEN" in body, f"{name}: no partition bound on decided_at"


def test_every_query_windows_on_departure_time_and_not_on_decided_at():
    """The axis is the page's whole claim: it answers "what would we have sent out for the trains
    leaving between 07:00 and 09:00". A file left windowing on `decided_at` would select a different
    population from the three beside it — and nothing on the page would look inconsistent, which is
    what makes it worth a test. /filters and /summary are the dangerous two: they feed the dropdown
    vocabulary and the rates, so a divergence there mislabels the cards rather than emptying them.
    """
    for name, sql in _QUERIES.items():
        body = _uncommented(sql)
        assert "departure_time >= @from_ts" in body, f"{name}: window is not on departure_time"
        assert "departure_time <" in body, f"{name}: window has no exclusive upper bound"
        assert "decided_at >= @from_ts" not in body, f"{name}: still windows on decided_at"


def test_the_replayed_cycle_is_the_one_a_push_went_out_on():
    """`pushed` is the RECORDED OUTPUT of varsel's four delivery filters — the operator threshold,
    the not-yet-departed cut, one_alert_per_train's collapse, and the `_notified` dedup keyed on
    (journey_id, situation) — written by `_record_decisions` after delivery.

    Picking `alertworthy` instead, which this did until 2026-08-22, names a cycle no message went out
    on: most alert-worthy cycles are repeats that `_notified` suppressed, and a collapse loser is
    alert-worthy on every cycle of its life while reaching nobody at all. The card would then state a
    send time for a push that never happened.

    It must also be the FIRST such cycle, because `_notified` makes the first delivery the only one
    for that situation's life — a later one is the in-app card being refreshed, not a second push.
    """
    for name in ("decisions", "messages"):
        body = _uncommented(_QUERIES[name])
        picks = [seg for seg in body.split("ARRAY_AGG(")[1:] if "would_send_at" in seg]
        assert len(picks) == 1, f"{name}: expected exactly one replay pick, found {len(picks)}"
        pick = picks[0]
        assert "pushed AND" in pick, f"{name}: the replay pick is not gated on `pushed`"
        assert "ORDER BY decided_at LIMIT 1" in pick, f"{name}: replay pick is not the FIRST cycle"
        assert "DESC" not in pick.split("LIMIT 1")[0], f"{name}: replay pick takes the LAST cycle"


def test_pushed_only_is_narrower_than_alerted_only_in_both_row_queries():
    """Two filters that look like one. An alert-worthy subject reaches nobody when another situation
    about its train wins one_alert_per_train's collapse, or when `_notified` already delivered for
    that situation on an earlier cycle — so `pushed_only` is a strict subset, and the two must both
    exist for the difference to be visible at all.

    Both row queries need it, and identically: they are aligned position by position, so a filter
    applied to one list and not the other attaches one train's wording to another train's card.
    """
    for name in ("decisions", "messages"):
        # Whitespace-collapsed: these two predicates are column-aligned in the files for
        # readability, and a reformat must not fail a test about which filters exist.
        body = re.sub(r"\s+", " ", _uncommented(_QUERIES[name]))
        assert "@pushed_only OR s.pushed_any" in body, f"{name}: no pushed_only filter"
        assert "@alerted_only OR s.ever_alerted" in body, f"{name}: lost the alerted_only filter"
    # ...and never on /summary, where narrowing to what we sent would make precision read 100 %.
    assert "@pushed_only" not in _uncommented(_QUERIES["summary"])


def test_the_outcomes_join_carries_its_own_partition_bound():
    """`varsel_decision_outcomes` is PARTITION BY call_date, which a `decided_at` predicate does NOT
    prune — so without this the outcomes read would be strictly more expensive than /historikk's,
    which does prune it, while the query overall claims to be cheaper.

    It stays +/-1 day rather than the exact `= @day` that the departure axis appears to allow, and
    the reason is `call_date` being the OPERATING day: an after-midnight service belongs to the
    previous timetable day, so the exact form would drop those subjects' verdicts. Too narrow a
    bound drops outcome rows SILENTLY — the subjects just read as unresolved, which the page renders
    perfectly happily — and the byte counts validate_varsel_sql.sh prints would report that loss as
    a cheaper query, i.e. as success. RE-MEASURE before widening the scoring horizon.
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


def test_the_registered_endpoints_are_projected_only_for_server_side_name_resolution():
    """Decisions may carry refs internally so the API can resolve card labels; summary may not."""
    assert any("from_stop" in p and "to_stop" in p for p in _projections(_QUERIES["decisions"]))
    for projection in _projections(_QUERIES["summary"]):
        assert "from_stop" not in projection and "to_stop" not in projection
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
        filename = f"meldinger_{name}.sql"
        assert filename in script, f"{filename} is not dry-run by validate_varsel_sql.sh"
        for param in sorted(set(re.findall(r"@(\w+)", _uncommented(sql)))):
            assert f"parameter={param}:" in script or f"parameter={param}<" in script, (
                f"{filename} uses @{param}, which the validator does not supply — the dry run would "
                f"fail to parse and the file would go unvalidated"
            )


# --- That the surface is served at all --------------------------------------------------------
#
# main.py registers the router and builds `app` at IMPORT, so this cannot be exercised by poking at
# the module afterwards — that would test the poke, not the registration. The check therefore
# imports main in a FRESH interpreter, the pattern test_llm_message.py uses for the same reason.
#
# /meldinger was gated on a TILBAKEBLIKK_ENABLED environment variable until the surface was
# renamed from /tilbakeblikk; the gate is gone and both halves are unconditional. What is worth
# pinning is that BOTH halves are there, since half a surface is worse than none: the page would
# load and every request behind it would 404.

_API_DIR = os.path.dirname(os.path.abspath(__file__))

# Printed as one JSON line so the parent asserts on structure rather than on grep.
#
# The API half is probed by ASKING for the endpoint rather than by reading app.routes: this FastAPI
# version keeps an included router as one opaque `_IncludedRouter` entry whose `path` is None, so a
# scan of app.routes finds no /meldinger-api route even when the router is registered — it would
# have "passed" identically with the router removed. /config needs neither BigQuery nor the
# lifespan, which is what makes 404-vs-200 a clean read on registration alone.
#
# The mount half stays structural, because the question there is not "does something answer" but
# which class answers: a plain StaticFiles would put index.html back under heuristic caching.
_SERVED_PROBE = """
import json

from google.cloud import bigquery
bigquery.Client = lambda *a, **k: None

import main
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles

mounts = {
    route.path: (
        type(route.app).__name__,
        isinstance(route.app, main._VersionedStaticFiles),
    )
    for route in main.app.routes
    if isinstance(getattr(route, "app", None), StaticFiles)
}
client = TestClient(main.app)
print(json.dumps({
    "config_status": client.get("/meldinger-api/config").status_code,
    "historikk_config_status": client.get("/historikk-api/config").status_code,
    "mounts": mounts,
}))
"""


@contextmanager
def _meldinger_static_present():
    """Make sure meldinger_static/ exists for the duration of one check.

    The mount is guarded on the directory existing, and that directory is generated at image-build
    time — absent in a fresh checkout and in CI. Without this the "mounted" assertion would pass by
    not running, on exactly the machine where nobody would look. Created only when missing and
    removed only when created, so a real local build is never touched.
    """
    path = Path(_API_DIR) / "meldinger_static"
    created = not path.exists()
    if created:
        path.mkdir()
    try:
        yield path
    finally:
        if created:
            path.rmdir()


def _probe() -> dict:
    """Import main in a child interpreter and report what it registered."""
    env = dict(os.environ)
    env["GOOGLE_CLOUD_PROJECT"] = "ent-kixavvik-test"
    result = subprocess.run(
        [sys.executable, "-c", _SERVED_PROBE],
        cwd=_API_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_both_halves_of_the_surface_are_served():
    """The router and the mount, together, and the mount as the caching subclass.

    test_static_cache.py already asserts the subclass for every mount that exists, but it is vacuous
    where the generated directories are absent — so this pins /meldinger's own mount with the
    directory guaranteed to be there.

    /historikk-api is asserted present in the same run as a control: it proves the probe sees the
    routers at all, so a failure here means withheld and not "import failed quietly".
    """
    with _meldinger_static_present():
        state = _probe()
    assert state["historikk_config_status"] == 200, "the probe did not see the other routers either"
    assert state["config_status"] == 200, "/meldinger-api/config is not registered"
    assert "/meldinger" in state["mounts"], "the UI is not mounted"
    type_name, is_versioned = state["mounts"]["/meldinger"]
    assert is_versioned, f"/meldinger is mounted with {type_name}, not _VersionedStaticFiles"


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} meldinger checks passed.")


if __name__ == "__main__":
    _run()
