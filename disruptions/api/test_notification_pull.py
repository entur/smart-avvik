"""The journey-keyed public read on `/notification-api/v1` (plan 2026-09-10-001, U3 + U4), hermetic.

Rides `test_varsel_api`'s harness -- importing it sets the VAPID keypair, the state file and the
environment label, stubs BigQuery through `test_varsel`, points `varsel` at main's REAL /impact
provider over a swappable synthetic snapshot, and disarms the Journey Planner -- and then re-points
the two stop providers at NSR-shaped rows, because the read refuses a ref that is not
`NSR:StopPlace:<digits>` before it reaches the matcher, so that harness's `"A"`/`"B"` prove nothing
here. The router is mounted on its own `FastAPI()` app, never `main.app` (its lifespan starts the
poller).

The enrichment tests (U4, the last section) are threaded and deterministic: every fake at a seam
answers at once or blocks on an `Event` the test releases, a task is joined through the in-flight
map (`_join`) rather than slept on, and no read ever waits the default `MAX_WAIT_S` -- `_get`
defaults `wait` to two seconds and the parked-task tests pass `wait=0`.

Run: KIX_ENVIRONMENT=prd uv run --with fastapi --with httpx --with pydantic --with google-cloud-bigquery \
  --with google-cloud-storage --with pywebpush --with google-cloud-firestore python test_notification_pull.py
"""

import test_varsel_api  # noqa: F401  (import side effects: env, BigQuery stubs, the /impact provider)

# isort: split
# The harness import stays FIRST: `varsel` reads the VAPID keypair from the environment at import,
# and `test_varsel_api` is what sets it.
import json
import logging
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import alternatives
import llm_message
import notification_pull
import notification_wording
import varsel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_varsel import _OSLO, FakeCall
from test_varsel_api import (
    DEVICE,
    WEBHOOK_DOC_ROUTED,
    _capture_writes,
    _impact_provider,
    _MonkeyPatch,
    _register,
    _reset_state,
    _use_notification_store,
    _use_snapshot,
)

NSR_A = "NSR:StopPlace:337"
NSR_B = "NSR:StopPlace:418"
PULL_STOPS = [
    {"stop_place": NSR_A, "stop_name": "Asker stasjon", "mode": "Tog", "n_calls": 264},
    {"stop_place": NSR_B, "stop_name": "Oslo S", "mode": "Tog/Buss", "n_calls": 900},
]
PATH = "/notification-api/v1/journey-alerts"

varsel.configure(impact_provider=_impact_provider, stops_provider=lambda: PULL_STOPS)
# The stop REGISTRY the message input resolves `reise.fra`/`reise.til` from. test_varsel_api wires
# it to its own A/B rows; left there, every route line here would read as a bare ref, which looks
# exactly like a wording bug and is not one.
llm_message.configure(stop_name_provider=lambda: PULL_STOPS)

app = FastAPI()
app.include_router(notification_pull.router)
client = TestClient(app)

LOADED_AT = datetime(2026, 9, 10, 10, 0, tzinfo=UTC)


def _train(dep, *, jkey="t1", situation="S1", **board_kw):
    """A boarding call at NSR_A and a later alighting call at NSR_B on one train."""
    return [
        FakeCall(jkey=jkey, stop_place=NSR_A, stop_order=1, aimed_dep=dep, situation_number=situation,
                 **board_kw),
        FakeCall(jkey=jkey, stop_place=NSR_B, stop_order=5, aimed_dep=None, situation_number=situation,
                 aimed_arr=dep + timedelta(minutes=40)),
    ]


ALL_DAYS = [0, 1, 2, 3, 4, 5, 6]
#: The five fields of a registration on the pair, verbatim -- what R3 means by "a registered
#: definition's five fields": `journey_key` over these must equal the key `_register` writes.
REGISTERED = {"begin": "00:00", "end": "23:59", "days": ALL_DAYS}


def _reset(*, refreshed_at=LOADED_AT, now=None):
    """Both harness resets, then the read's own seams: a loaded cache and a frozen clock.

    The one reset seam for the read's enrichment state (KTD5): `test_varsel_api._reset_state` is
    left alone, because that suite never calls the read, so no pull task can exist there to
    pollute it -- replacing an executor 153 times would buy nothing.

    The webhook delivery switch is pinned OFF here, which makes the REAL `write_wording` the
    default: it answers `not_delivered` before touching a backend, so a test that does not care
    about wording gets a terminal `disabled` for free. Two seams for one switch, and the
    difference bites: `notification_wording.PULL.deliver` captured the FUNCTION at import, so
    monkeypatching `notification_wording.webhook_delivery_enabled` reaches the task's own once-
    per-task read (and `varsel`'s) but not the real `write_wording`'s gate, which reads
    `_WEBHOOK_DELIVERY_ENABLED`. A test that wants the real writer to reach a stubbed backend sets
    the CONSTANT (`_real_backend`); one that fakes `write_wording` only needs the attribute.
    """
    _reset_state()
    notification_pull.reset_state()
    notification_pull.configure(refreshed_at=lambda: refreshed_at)
    notification_pull._now = (lambda: now) if now is not None else notification_pull._wall_clock
    notification_wording._WEBHOOK_DELIVERY_ENABLED = False


def _get(**params):
    """A read on the pair. `wait` defaults to a SMALL positive budget: with immediate fakes a
    patient read returns the moment the task resolves, and a hung fake then costs two seconds
    rather than `MAX_WAIT_S`."""
    query = {"from_stop": NSR_A, "to_stop": NSR_B, "wait": 2, **params}
    return client.get(PATH, params=query)


def _join(**definition):
    """Block on the enrichment task for a definition through the in-flight map, never a sleep.

    Takes `_get`'s own parameters (no `days` means the route's default, today and tomorrow), so a
    test joins exactly the task the read it just made submitted.

    The map keeps a finished Future until the next submission for its key replaces it, which is
    what lets a test join a task that an immediate fake completed before the request returned.
    """
    now = notification_pull._now()
    journey = notification_pull._definition(
        NSR_A, NSR_B, definition.get("begin"), definition.get("end"), definition.get("days"), now
    )
    key = notification_pull._inflight_key(varsel.journey_key(journey), now)
    with notification_pull._tasks_lock:
        future = notification_pull._inflight[key]
    return future.result(timeout=5)


# ---- The answer ------------------------------------------------------------------------------



def _subjects(outcome: object) -> list:
    """Every (recipient, train) the pass observed, materialised as `notification_events` does.

    The pass hands over one observation per (definition, train) with its recipients since U9 --
    1.3 million dataclasses on a 100 000-subscription corridor is why -- so a test that wants to
    read one recipient's subject asks for it the same way the emit loop does.
    """
    return [
        observation.subject(recipient)
        for observation in outcome.webhook_observations
        for recipient in observation.recipients
    ]

def test_two_alert_worthy_trains_answer_two_entries_soonest_first():
    """R2: one entry per alert-worthy train ahead, soonest expected departure first, the rank
    carried so a client can re-sort worst first; `limit` cuts the list and says so."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({
        # The later train is the WORSE one, so soonest-first and worst-first disagree on purpose.
        "S1": _train(now + timedelta(hours=1), p_cancel=0.3, p_impact_dep=0.70),
        "S2": _train(now + timedelta(hours=2), jkey="t2", situation="S2", p_cancel=0.9,
                     p_impact_dep=0.95),
    })
    r = _get()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["n_alerts"] == 2 and body["truncated"] is False and body["complete"] is True
    assert [a["train"]["journey_ref"] for a in body["alerts"]] == ["t1", "t2"]
    assert [a["severity"] for a in body["alerts"]] == [2, 1], "1 is the worst of the answer's trains"

    r = _get(limit=1)
    body = r.json()
    assert len(body["alerts"]) == 1 and body["n_alerts"] == 2 and body["truncated"] is True
    assert body["alerts"][0]["train"]["journey_ref"] == "t1"


def test_the_selection_is_the_collapse_then_the_gate_and_nothing_the_card_adds():
    """R2/KTD7. Three things absent that other surfaces show: a published situation UNDER the gate
    (the card shows every published one through `has_situation`); the U2 collapse-then-gate case
    -- a sub-gate no-SX reading (0.80 < 0.85) outranking an over-gate published one (0.70 > 0.65)
    on the same train, so the collapse keeps the sub-gate winner and the train is not
    alert-worthy; and nothing at all for a train no situation touches."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    dep = now + timedelta(hours=1)
    _use_snapshot({
        "S_under": _train(dep, jkey="t9", situation="S_under", p_impact_dep=0.50),
        "KIX:NoSituation:VYG:t1": _train(dep, situation="KIX:NoSituation:VYG:t1", has_situation=False,
                                         summary="Mulig avvik", p_impact_dep=0.80, p_cancel=0.3),
        "S_t1_published": _train(dep, situation="S_t1_published", p_impact_dep=0.70),
        "S_t2": _train(dep + timedelta(minutes=30), jkey="t2", situation="S_t2", p_impact_dep=0.95),
    })
    alerts = varsel.match_journey(
        {"from_stop": NSR_A, "to_stop": NSR_B, "begin": "00:00", "end": "23:59", "days": list(range(7))},
        now,
    )
    assert {a.situation_number for a in alerts} >= {"S_under", "KIX:NoSituation:VYG:t1", "S_t1_published"}, (
        "control: the matcher sees all three, and the card would show the published two"
    )
    body = _get().json()
    assert [a["train"]["journey_ref"] for a in body["alerts"]] == ["t2"], body["alerts"]
    assert body["n_alerts"] == 1


def test_two_situations_about_one_train_collapse_to_one_entry_naming_both():
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    dep = now + timedelta(hours=1)
    _use_snapshot({
        "S_a": _train(dep, situation="S_a", p_impact_dep=0.90, summary="Ta neste tog"),
        "S_b": _train(dep, situation="S_b", p_impact_dep=0.85, summary="Innstilt Drammen-Skøyen"),
    })
    body = _get().json()
    assert body["n_alerts"] == 1 and len(body["alerts"]) == 1
    entry = body["alerts"][0]
    assert sorted(entry["situations"]) == ["S_a", "S_b"]
    assert sorted(s["situation_number"] for s in entry["payload"]["situations"]) == ["S_a", "S_b"]
    assert entry["situations"] == [s["situation_number"] for s in entry["payload"]["situations"]]


def test_a_declared_cancellation_is_confirmed_certain_and_says_innstilt():
    """R3: `phase: confirmed` iff `alert.cancelled`; probabilities 1.0 and delay null are
    `impact._cancelled_prediction`'s rule carried by `varsel._webhook_payload`, and the title is the
    template's «er innstilt»."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    dep = now + timedelta(hours=1)
    _use_snapshot({"S1": _train(dep, et_declared_cancelled=True, p_impact_dep=0.6, p_cancel=0.4)})
    entry = _get().json()["alerts"][0]
    assert entry["phase"] == "confirmed"
    assert entry["payload"]["probabilities"]["impact"] == 1.0
    assert entry["payload"]["probabilities"]["cancellation"] == 1.0
    assert entry["payload"]["delay"] is None and entry["payload"]["effect"] == "cancelled"
    assert "innstilt" in entry["payload"]["notification"]["title"], entry["payload"]["notification"]
    assert entry["expected_departure_time"] == entry["aimed_departure_time"]
    # This harness pins the webhook delivery switch off, so the real writer answers `not_delivered`
    # before any backend -- `disabled`, terminal -- and the Journey Planner stub's empty reply is a
    # completed lookup.
    assert entry["wording"] == {"source": "deterministic", "status": "disabled"}
    assert entry["alternative"] == {"status": "none_found"}


def test_a_train_taken_out_of_the_plan_is_listed_confirmed_under_its_own_situation():
    """R4 of plan 2026-09-23-001: the read lists a removed train as it lists a declared
    cancellation (the relay's rows carry the same certainty), and the situation names its source.
    Only the webhook channel skips it."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    dep = now + timedelta(hours=1)
    jkey = "RemovedJourney:2105_2026-09-10"
    situation = f"KIX:PlanRemoved:{jkey}"
    _use_snapshot({situation: _train(
        dep, jkey=jkey, situation=situation, et_declared_cancelled=True, p_impact_dep=1.0,
        p_cancel=1.0, call_source="removed", has_situation=False,
        summary="Tatt ut av dagens rutetabell",
    )})
    (entry,) = _get().json()["alerts"]
    assert entry["phase"] == "confirmed" and entry["situations"] == [situation]
    assert entry["train"]["journey_ref"] == jkey
    assert entry["payload"]["probabilities"]["cancellation"] == 1.0


def test_a_removed_train_is_never_worded_and_takes_no_place_under_the_wording_cap(monkeypatch):
    """Dev, 2026-09-23: on a cut day the removed trains sat soonest-first ahead of the forecasts,
    the read worded every one of them on every cycle (the snapshot is in the wording key), and the
    forecasts behind them read `not_worded`. A removed train is `disabled` and spends no call, as
    in the pass, and the cap goes to the trains after it."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    cap = notification_pull.MAX_WORDINGS_PER_READ
    snapshot = {}
    for i in range(cap + 1):
        jkey = f"RemovedJourney:{2100 + i}_2026-09-10"
        situation = f"KIX:PlanRemoved:{jkey}"
        snapshot[situation] = _train(
            now + timedelta(minutes=20 + i), jkey=jkey, situation=situation, et_declared_cancelled=True,
            p_impact_dep=1.0, p_cancel=1.0, call_source="removed", has_situation=False,
            summary="Tatt ut av dagens rutetabell",
        )
    for i in range(cap):
        snapshot[f"S{i}"] = _train(now + timedelta(minutes=60 + 10 * i), jkey=f"t{i}", situation=f"S{i}", p_impact_dep=0.9)
    _use_snapshot(snapshot)
    words = _fake_wording(monkeypatch)
    body = _get(limit=20).json()
    assert [(a["situations"][0].startswith("KIX:PlanRemoved:"), a["wording"]) for a in body["alerts"]] == (
        [(True, {"source": "deterministic", "status": "disabled"})] * (cap + 1)
        + [(False, {"source": "llm", "status": "ok"})] * cap
    )
    assert sorted(c[1] for c in words) == [f"S{i}" for i in range(cap)]
    assert body["complete"] is True


def test_the_route_line_names_the_registry_not_the_ref():
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    entry = _get().json()["alerts"][0]
    route, _, wording = entry["payload"]["notification"]["body"].partition("\n")
    assert route == "Asker stasjon → Oslo S", route
    assert wording and "NSR:" not in wording


def test_a_departure_that_passed_after_the_figures_were_cached_is_cut_at_assembly():
    """KTD3: `catchable_after` is match time; a cached figure can outlive its departure by a cycle,
    so the assembly re-applies the cut against THIS request's clock -- on the cached entries,
    without running the matcher again."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    dep = now + timedelta(hours=1)
    _use_snapshot({
        "S1": _train(dep, p_impact_dep=0.9),
        "S2": _train(dep + timedelta(hours=3), jkey="t2", situation="S2", p_impact_dep=0.9),
    })
    assert _get().json()["n_alerts"] == 2
    calls = _count_matcher_calls()
    # Ninety minutes on: t1's expected departure (aimed + 3 min typical) has passed, t2's has not.
    notification_pull._now = lambda: now + timedelta(minutes=90)
    body = _get().json()
    assert [a["train"]["journey_ref"] for a in body["alerts"]] == ["t2"]
    assert body["n_alerts"] == 1 and body["truncated"] is False
    assert calls() == 0, "the cut is per request, not a matcher re-run"


# ---- Validation (KTD2) -----------------------------------------------------------------------


def test_every_malformed_input_is_a_422():
    _reset()
    cases = [
        ({"from_stop": "A"}, "a ref off the published shape"),
        ({"to_stop": "NSR:Quay:1"}, "a quay is not a stop place"),
        ({"begin": "09:00", "end": "07:00"}, "begin must be before end"),
        ({"begin": "07:00", "end": "07:00"}, "an empty period"),
        ({"begin": "07:00"}, "begin without end"),
        ({"end": "09:00"}, "end without begin"),
        ({"begin": "7:00", "end": "09:00"}, "not HH:MM"),
        ({"days": [7]}, "a weekday outside 0..6"),
        ({"days": [-1]}, "a negative weekday"),
        ({"days": ["0,1"]}, "a comma-separated list is not the repeated form"),
        ({"wait": notification_pull.MAX_WAIT_S + 1}, "wait over its ceiling"),
        ({"wait": -1}, "a negative wait"),
        ({"limit": 0}, "limit under one"),
        ({"limit": notification_pull.MAX_LIMIT + 1}, "limit over its ceiling"),
    ]
    for params, why in cases:
        r = _get(**params)
        assert r.status_code == 422, (why, r.status_code, r.text)
    # Equal refs AFTER canonicalization: the registry says these two are one place.
    varsel.set_stop_aliases({NSR_B: NSR_A})
    r = _get()
    assert r.status_code == 422 and "must differ" in r.json()["detail"], r.text
    assert NSR_A not in r.text and NSR_B not in r.text, "a refusal echoes no ref"


def test_the_refusals_use_varsels_own_wording():
    """The window and weekday verdicts are `varsel._parse_webhook_recipient`'s, reused rather than
    restated -- the same messages `notification_api` pins against `JourneyIn`."""
    _reset()
    assert _get(begin="09:00", end="07:00").json()["detail"] == (
        "begin must be before end (overnight windows are not supported)"
    )
    assert _get(days=[7]).json()["detail"] == "days must be a non-empty subset of 0..6 (0=Monday)"
    assert _get(begin="7:00", end="09:00").json()["detail"] == "begin/end must be HH:MM (24h)"


def test_the_canonical_definition_and_the_utc_windows_are_echoed():
    """A collapsed ref is canonicalized as /varsel canonicalizes a submitted one, and the answer
    says which definition it is about: the canonical refs, the defaults filled, the windows as UTC
    instants."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)  # a Thursday
    _reset(now=now)
    varsel.set_stop_aliases({"NSR:StopPlace:999": NSR_B})
    body = _get(to_stop="NSR:StopPlace:999").json()
    assert body["journey"] == {
        "from_stop": NSR_A, "to_stop": NSR_B, "begin": "00:00", "end": "23:59", "days": [3, 4],
    }
    definition = {**body["journey"]}
    expected = [
        {"start": s.isoformat().replace("+00:00", "Z"), "end": e.isoformat().replace("+00:00", "Z")}
        for s, e in varsel.journey_windows(definition, now)
    ]
    assert len(expected) == 2
    assert body["windows"] == expected, (body["windows"], expected)
    assert body["as_of"] == now.astimezone(UTC).isoformat().replace("+00:00", "Z")

    body = _get(begin="07:00", end="09:00", days=[0, 1]).json()
    assert body["journey"]["begin"] == "07:00" and body["journey"]["days"] == [0, 1]
    assert body["windows"] == [], "a Thursday read about Monday and Tuesday has no window ahead"


def test_coverage_names_the_served_operators_and_reads_the_stop_cache_over_every_ref():
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    coverage = _get().json()["coverage"]
    assert {"VYG", "GOA", "RUT"} <= set(coverage["operators"])
    assert coverage["operators"] == sorted(coverage["operators"])
    assert coverage["from_stop_in_timetable_today"] is True
    assert coverage["to_stop_in_timetable_today"] is True

    stashed = varsel._stops_provider
    try:
        # NSR_B is not in today's timetable.
        varsel._stops_provider = lambda: [PULL_STOPS[0]]
        coverage = _get().json()["coverage"]
        assert coverage == {
            "operators": list(notification_pull.SERVED_OPERATORS),
            "from_stop_in_timetable_today": True,
            "to_stop_in_timetable_today": False,
        }
        # Only a COLLAPSED ref of NSR_B's place is in the cache: the place is served.
        varsel.set_stop_aliases({"NSR:StopPlace:999": NSR_B})
        varsel._stops_provider = lambda: [
            PULL_STOPS[0], {"stop_place": "NSR:StopPlace:999", "stop_name": "Oslo S", "mode": "Tog", "n_calls": 1},
        ]
        assert _get().json()["coverage"]["to_stop_in_timetable_today"] is True
        # An empty cache fails OPEN.
        varsel._stops_provider = list
        coverage = _get().json()["coverage"]
        assert coverage["from_stop_in_timetable_today"] is True
        assert coverage["to_stop_in_timetable_today"] is True
    finally:
        varsel._stops_provider = stashed


def test_a_late_evening_read_lists_tomorrows_train_and_says_so():
    """No period means the whole day, and the cache carries tomorrow: at 22:00 there are two
    windows, and a 00:30 departure is listed with `departs_tomorrow` on its call (finding 48)."""
    now = datetime(2026, 9, 10, 22, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=2, minutes=30), p_impact_dep=0.9)})
    body = _get().json()
    assert len(body["windows"]) == 2
    assert body["n_alerts"] == 1
    entry = body["alerts"][0]
    assert entry["payload"]["call"]["departs_tomorrow"] is True
    assert " i morgen " in entry["payload"]["notification"]["title"], entry["payload"]["notification"]


# ---- Freshness and failure (KTD8, R6) --------------------------------------------------------


def test_a_cache_the_poller_has_not_loaded_is_a_503_never_an_empty_list():
    _reset(refreshed_at=None)
    r = _get()
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "predictions_not_loaded"
    assert r.headers["Retry-After"] == "60"


def test_a_loaded_cache_is_served_whatever_its_age_with_its_refreshed_at():
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    stale = now.astimezone(UTC) - timedelta(hours=1)
    _reset(refreshed_at=stale, now=now)
    r = _get()
    assert r.status_code == 200
    assert r.json()["refreshed_at"] == stale.isoformat().replace("+00:00", "Z")


def test_a_matcher_failure_is_a_503_and_the_warning_names_no_ref():
    """A `KeyError("NSR:StopPlace:337")` carries the ref in its text, so the record carries the
    exception's TYPE and the traceback goes to DEBUG."""
    _reset()
    stashed = varsel.match_journey

    def failing(*a, **k):
        raise KeyError(NSR_A)

    varsel.match_journey = failing
    try:
        with _captured() as records:
            r = _get()
    finally:
        varsel.match_journey = stashed
    assert r.status_code == 503 and r.json()["detail"] == "matcher_failed"
    assert r.headers["Retry-After"] == "60"
    warnings = [x for x in records if x.levelno >= logging.WARNING]
    assert warnings, "the failure is logged at WARNING"
    for record in warnings:
        assert NSR_A not in record.getMessage() and "KeyError" in record.getMessage()
        assert NSR_A not in str(getattr(record, "fields", {}))
        assert record.exc_info is None


# ---- The limiter (KTD11, R7) -----------------------------------------------------------------


def test_the_global_ceiling_answers_429_with_retry_after():
    _reset()
    stashed = notification_pull.MAX_PER_MIN
    notification_pull.MAX_PER_MIN = 3
    try:
        assert [_get().status_code for _ in range(3)] == [200, 200, 200]
        r = _get()
        assert r.status_code == 429 and r.json()["detail"] == "rate_limited"
        assert r.headers["Retry-After"] == "60"
    finally:
        notification_pull.MAX_PER_MIN = stashed


def test_the_per_pair_ceiling_is_per_pair_and_every_period_shares_it():
    """Per PAIR, not per definition: a definition is free to mint, so thirty distinct periods on
    one pair spend one bucket and the thirty-first read is refused, while another pair is
    untouched."""
    _reset()
    for i in range(notification_pull.PER_PAIR_PER_MIN):
        r = _get(begin=f"{i % 24:02d}:{i:02d}", end="23:59")
        assert r.status_code == 200, (i, r.text)
    r = _get(begin="00:00", end="12:00")
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"
    assert _get(from_stop=NSR_B, to_stop=NSR_A).status_code == 200, "the reverse pair is another bucket"


def test_the_pair_map_is_bounded_by_dropping_the_insertion_oldest():
    _reset()
    stashed = notification_pull.LIMITER_KEYS_MAX
    notification_pull.LIMITER_KEYS_MAX = 2
    try:
        for n in (1, 2, 3):
            assert _get(from_stop=f"NSR:StopPlace:{n}", to_stop=NSR_B).status_code == 200
        assert list(notification_pull._pair_buckets) == [
            ("NSR:StopPlace:2", NSR_B), ("NSR:StopPlace:3", NSR_B),
        ]
    finally:
        notification_pull.LIMITER_KEYS_MAX = stashed


def test_a_refused_read_still_logs_its_result():
    _reset()
    stashed = notification_pull.MAX_PER_MIN
    notification_pull.MAX_PER_MIN = 1
    try:
        _get()
        with _captured() as records:
            _get()
    finally:
        notification_pull.MAX_PER_MIN = stashed
    assert [r.fields["result"] for r in records] == ["rate_limited"]


# ---- The figures cache (KTD3, R8) ------------------------------------------------------------


def _count_matcher_calls():
    """Wrap `varsel.match_journey` -- by attribute, which is how the module reads it -- and return
    a reader of the count since the wrap. Always wraps the ORIGINAL, so a wrapper left behind by an
    earlier test counts nothing twice; the wrapper itself is harmless to leave (it delegates)."""
    original = _ORIGINAL_MATCH
    calls = [0]

    def counting(*a, **k):
        calls[0] += 1
        return original(*a, **k)

    varsel.match_journey = counting
    return lambda: calls[0]


_ORIGINAL_MATCH = varsel.match_journey


def test_a_warm_read_within_the_cycle_runs_no_matcher():
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    calls = _count_matcher_calls()
    assert _get().json()["n_alerts"] == 1
    # Two on a cold definition: the figures run, and the enrichment task's run with alternatives
    # on the pull path (U4) -- which a patient read has joined by the time it returns.
    assert calls() == 2
    with _captured() as records:
        assert _get().json()["n_alerts"] == 1
    assert calls() == 2, "the second read within the cycle ran the matcher"
    assert [r.fields["cache"] for r in records] == ["hit"]
    # A different definition on the same pair is its own figures key -- one more run -- but its
    # one train is the train just enriched, per (pair, train), so no task is owed for it.
    _get(begin="05:00", end="09:00")
    assert calls() == 3


def test_a_moved_refreshed_at_runs_the_matcher_once_and_evicts_the_old_generation():
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    calls = _count_matcher_calls()
    _get()
    old_keys = list(notification_pull._figures)
    assert len(old_keys) == 1 and old_keys[0][1] == LOADED_AT
    notification_pull.configure(refreshed_at=lambda: LOADED_AT + timedelta(minutes=5))
    _get()
    assert calls() == 2
    keys = list(notification_pull._figures)
    assert len(keys) == 1 and keys[0][1] == LOADED_AT + timedelta(minutes=5), keys
    _get()
    assert calls() == 2


def test_another_oslo_day_is_another_key():
    now = datetime(2026, 9, 10, 23, 30, tzinfo=_OSLO)
    _reset(now=now)
    calls = _count_matcher_calls()
    _get()
    notification_pull._now = lambda: now + timedelta(hours=1)  # 00:30 the next Oslo day
    _get()
    assert calls() == 2
    assert len(notification_pull._figures) == 2


def test_the_figures_cache_is_bounded():
    _reset()
    stashed = notification_pull.FIGURES_CACHE_MAX
    notification_pull.FIGURES_CACHE_MAX = 2
    try:
        for hour in ("01", "02", "03"):
            _get(begin=f"{hour}:00", end="23:59")
        assert len(notification_pull._figures) == 2
        assert all(key[0].split("|")[2] in ("02:00", "03:00") for key in notification_pull._figures)
    finally:
        notification_pull.FIGURES_CACHE_MAX = stashed


# ---- Headers and logs (R9, R11) --------------------------------------------------------------


def test_a_complete_answer_carries_the_public_cache_control_header():
    _reset()
    r = _get()
    assert r.status_code == 200 and r.json()["complete"] is True
    assert r.headers["Cache-Control"] == "public, max-age=60"


@contextmanager
def _captured():
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector()
    log = logging.getLogger("notification_pull")
    before = log.level
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        log.removeHandler(handler)
        log.setLevel(before)


ALLOWED_FIELDS = {"event", "result", "cache", "n_alerts", "wait_s", "ms", "pair_digest", "client_name"}


def test_no_log_record_carries_a_ref_a_name_a_departure_or_prose():
    """R11 over a full read: a cold miss, a warm hit, and a refusal. Every `notification_pull`
    record's field set is within the allowed eight, the digest is `alternatives.pair_digest`'s,
    and no message or field carries a ref, a stop name, a departure time or the wording."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    with _captured() as records:
        body = _get(wait=5).json()
        _get(wait=5)
    assert body["n_alerts"] == 1
    pulls = [r for r in records if getattr(r, "fields", {}).get("event") == "notification_pull"]
    assert [r.fields["cache"] for r in pulls] == ["miss", "hit"]
    assert [r.fields["n_alerts"] for r in pulls] == [1, 1]
    assert [r.fields["wait_s"] for r in pulls] == [5, 5]
    assert all(set(r.fields) <= ALLOWED_FIELDS for r in pulls), [set(r.fields) for r in pulls]
    import alternatives

    assert {r.fields["pair_digest"] for r in pulls} == {alternatives.pair_digest(NSR_A, NSR_B)}
    wording = body["alerts"][0]["payload"]["notification"]["body"]
    forbidden = ("NSR:", "Asker", "Oslo S", "07:", "08:", wording.split("\n")[-1][:12])
    for record in records:
        text = record.getMessage() + " " + str(getattr(record, "fields", {}))
        assert not any(token in text for token in forbidden), text


def test_both_reads_record_the_callers_et_client_name_and_nothing_else_the_header_says():
    """Entur asks every API consumer for `ET-Client-Name`, and the two reads record it so a caller
    can be found -- dev's 2026-09-23 poller sent `User-Agent: node` and could not be. A well-formed
    label is kept, an absent one is null, and anything else (a stop ref, an empty value) is
    `invalid`: the header is no way around R11."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    sent = ["entur-kix-reisefolge-prototype", None, NSR_A, ""]
    expected = ["entur-kix-reisefolge-prototype", None, "invalid", "invalid"]
    query = {"from_stop": NSR_A, "to_stop": NSR_B}
    with _captured() as records:
        for name in sent:
            headers = {} if name is None else {"ET-Client-Name": name}
            assert client.get(PATH, params={**query, "wait": 0}, headers=headers).status_code == 200
    pulls = [r.fields["client_name"] for r in records if getattr(r, "fields", {}).get("event") == "notification_pull"]
    assert pulls == expected
    _status_reset(_status_train("a", STATUS_NOW + timedelta(minutes=6)))
    with _captured() as records:
        for name in sent:
            headers = {} if name is None else {"ET-Client-Name": name}
            assert client.get(STATUS_PATH, params=query, headers=headers).status_code == 200
    status = [r.fields["client_name"] for r in records if getattr(r, "fields", {}).get("event") == "notification_status"]
    assert status == expected
    for record in records:
        assert "NSR:" not in record.getMessage() + json.dumps(getattr(record, "fields", {})), record.getMessage()


def test_an_unparseable_ref_logs_nothing():
    _reset()
    with _captured() as records:
        assert _get(from_stop="Oslo S").status_code == 422
        assert _get(from_stop="NSR:StopPlace:x").status_code == 422
    assert records == []


# ---- The access log (KTD13) -------------------------------------------------------------------


def _access_record(path: str) -> logging.LogRecord:
    """A uvicorn access record exactly as uvicorn logs it: on the `uvicorn.access` logger, with the
    five positional args its formatter reads and the full path (query string included) third."""
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "h11_impl.py", 0,
        '%s - "%s %s HTTP/%s" %d', ("10.0.0.1:1234", "GET", path, "1.1", 200), None,
    )


def test_the_access_log_redacts_this_paths_query_string_and_no_other():
    import logging_setup
    import notification_api

    assert notification_api.PREFIX + "/journey-alerts" in logging_setup.REDACTED_QUERY_PATHS
    assert notification_api.PREFIX + "/journey-status" in logging_setup.REDACTED_QUERY_PATHS
    formatter = logging_setup.JsonFormatter()
    lines: list[str] = []

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(formatter.format(record))

    access = logging.getLogger("uvicorn.access")
    sink = _Sink()
    access.addHandler(sink)
    try:
        access.handle(_access_record(f"{PATH}?from_stop={NSR_A}&to_stop={NSR_B}&begin=07:00&end=09:00"))
        access.handle(_access_record(f"/impact?from_stop={NSR_A}&to_stop={NSR_B}"))
        access.handle(_access_record(PATH))
    finally:
        access.removeHandler(sink)
    assert len(lines) == 3
    messages = [json.loads(line)["message"] for line in lines]
    assert NSR_A not in lines[0] and "07:00" not in lines[0], lines[0]
    assert messages[0] == f'10.0.0.1:1234 - "GET {PATH}?<redacted> HTTP/1.1" 200', messages[0]
    assert f"/impact?from_stop={NSR_A}" in messages[1], "an unrelated path's query string is untouched"
    assert messages[2] == f'10.0.0.1:1234 - "GET {PATH} HTTP/1.1" 200'


def test_the_filter_leaves_any_other_record_shape_alone():
    import logging_setup

    redaction = next(
        f for f in logging.getLogger("uvicorn.access").filters
        if isinstance(f, logging_setup._AccessLogRedaction)
    )
    plain = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "started %s", (PATH + "?x=1",), None)
    assert redaction.filter(plain) is True and plain.args == (PATH + "?x=1",)
    none = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "no args", None, None)
    assert redaction.filter(none) is True and none.args is None
    assert sum(isinstance(f, logging_setup._AccessLogRedaction) for f in logging.getLogger("uvicorn.access").filters) == 1, (
        "setup() must not install the filter twice"
    )


# ---- The router (KTD1) -----------------------------------------------------------------------


def test_the_pull_router_has_no_dependency_and_exactly_two_routes_which_are_coroutines():
    """Pinned here rather than beside `notification_api`'s router pins. U5 (2026-09-10) widened
    `test_notification_api.test_the_openapi_paths_are_exactly_the_routers_routes` and
    `..._the_openapi_tags_are_the_routers_own` to the UNION of both routers (seven routes, four
    tags) and added `..._the_public_read_overrides_the_global_security_per_operation`; those three
    read the published document, this one reads the router, and the two claims below -- no
    dependency, exactly these paths -- are deliberately not repeated there. The second path is the
    journey-status read (plan 2026-09-22-001)."""
    import inspect

    import notification_api

    assert notification_pull.router.dependencies == []
    routes = {
        (route.path[len(notification_api.PREFIX):], method.lower())
        for route in notification_pull.router.routes
        for method in route.methods
        if method not in ("HEAD", "OPTIONS")
    }
    assert routes == {("/journey-alerts", "get"), ("/journey-status", "get")}
    for route in notification_pull.router.routes:
        assert inspect.iscoroutinefunction(route.endpoint), route.path
        assert route.dependencies == [], route.path


def test_the_response_models_forbid_unknown_fields():
    for model in (
        notification_pull.JourneyAlertsResponse,
        notification_pull.JourneyAlertOut,
        notification_pull.JourneyOut,
        notification_pull.WindowOut,
        notification_pull.CoverageOut,
        notification_pull.TrainOut,
        notification_pull.WordingOut,
        notification_pull.AlternativeOut,
    ):
        assert model.model_config.get("extra") == "forbid", model.__name__


def test_the_read_is_public_and_the_sso_tuple_does_not_name_it():
    import sso

    assert not any(p.startswith("/notification") for p in sso.PROTECTED_PREFIXES)


# ---- The enrichment (U4): wording and alternative on request -----------------------------------


def _webhook_doc():
    """`WEBHOOK_DOC_ROUTED` on this suite's NSR pair, with the registration's five fields."""
    return {**WEBHOOK_DOC_ROUTED, "from_stop": NSR_A, "to_stop": NSR_B, **REGISTERED}


def _register_here(monkeypatch):
    """A browser registration on the NSR pair whose typed names equal the registry's, so the push
    and the event agree byte for byte (the byte-identity test's own precondition)."""
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    return _register(
        DEVICE, from_stop=NSR_A, from_name="Asker stasjon", to_stop=NSR_B, to_name="Oslo S", **REGISTERED
    )


def _poller_caches():
    with varsel._state_lock:
        return dict(varsel._messages), dict(varsel._alternatives), set(varsel._notified)


def test_a_registered_definition_serves_the_pollers_wording_byte_identical_to_the_event(monkeypatch):
    """R3/KTD6, mirroring `test_a_webhook_subject_carries_byte_identical_wording_to_the_web_push`:
    the poller writes one wording for a definition a browser and a webhook subscription share; a
    read naming the same five fields serves THAT wording, source `llm`, and its
    `payload.notification` is the event's byte for byte -- with no pull wording, no pull fetch and
    no write to the poller's caches (R10)."""
    import webhook_delivery

    _reset()  # the wall clock: `check_and_notify` reads its own, and the two must not straddle
    dep = datetime.now(tz=_OSLO) + timedelta(hours=2)
    snapshot = {"S1": _train(dep, p_cancel=0.82, p_impact_dep=0.9)}
    _use_snapshot(snapshot)
    monkeypatch.setattr(notification_wording, "webhook_delivery_enabled", lambda: True)
    written = _capture_writes(monkeypatch)
    _use_notification_store(monkeypatch, [_webhook_doc()])
    _register_here(monkeypatch)

    outcome = varsel.check_and_notify(snapshot)
    assert len(_subjects(outcome)) == 1 and len(written) == 1, (written, outcome)
    subject = _subjects(outcome)[0]
    before = _poller_caches()
    assert before[0], "control: the poller cached a wording for the definition"

    r = _get(**REGISTERED)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["complete"] is True and body["n_alerts"] == 1
    entry = body["alerts"][0]
    assert entry["wording"] == {"source": "llm", "status": "ok"}
    assert webhook_delivery.event_body(entry["payload"]["notification"]) == webhook_delivery.event_body(
        subject.payload["notification"]
    ), (entry["payload"]["notification"], subject.payload["notification"])
    assert entry["payload"]["notification"]["body"].endswith("\nToget er innstilt. Ta neste tog.")
    assert len(written) == 1, "no pull wording was asked for a train the poller already worded"
    assert alternatives._fetch_calls["pull"] == []
    assert notification_wording.spent("pull") == (0, 0)
    assert _poller_caches() == before, "the read never writes the poller's caches"


def _fake_wording(monkeypatch, *, text="Toget er innstilt. Ta neste tog.", status="ok", hold=None):
    """Fake `notification_wording.write_wording` at the module seam: records every
    `(subject_id, situation_number, policy name)` it is asked for, answers at once -- or after
    `hold` (an Event) is set -- with the model's text under `status`, or the template for any other
    status. The template half is built by the real helpers, so a fake `error` still carries a
    usable body exactly as the real writer would."""
    calls = []

    def fake(route, facts, *, policy, subject_id="", now=None, budget=None, message_input=None):
        calls.append((subject_id, facts.situation_number, policy.name))
        if hold is not None:
            hold.wait(5)
        assert message_input is not None, "the pull passes the message input it built after the alternative"
        template = notification_wording.deterministic_body(message_input)
        title = notification_wording.notification_title(message_input, "A", "B")
        if status == "ok":
            return notification_wording.Wording(title, text, "llm", "ok", message_input)
        return notification_wording.Wording(title, template, "deterministic", status, message_input)

    monkeypatch.setattr(notification_wording, "write_wording", fake)
    return calls


def _real_backend(monkeypatch, reply='{"melding": "Toget er innstilt."}'):
    """The REAL `write_wording` down to a stubbed backend, the way `test_notification_wording`
    drives it: the pull's delivery gate on (the constant, see `_reset`), one provider so a wording
    is one call, and Claude answering `reply` (a callable is called with the request body)."""
    calls = []

    def backend(body):
        calls.append(body)
        return reply(body) if callable(reply) else reply

    notification_wording._WEBHOOK_DELIVERY_ENABLED = True
    monkeypatch.setattr(llm_message, "_PROVIDER", "claude")
    monkeypatch.setattr(llm_message, "_BACKENDS", {**llm_message._BACKENDS, "claude": backend})
    return calls


def _hold_matcher(monkeypatch):
    """Park every enrichment task at its matcher call -- the pull path's `with_alternative=True`
    run, never the figures run -- on an Event the test releases. Returns (event, calls)."""
    event = threading.Event()
    calls = []
    original = _ORIGINAL_MATCH

    def held(journey, now=None, *, with_alternative=False, path="poller"):
        if with_alternative:
            calls.append(path)
            event.wait(5)
        return original(journey, now, with_alternative=with_alternative, path=path)

    monkeypatch.setattr(varsel, "match_journey", held)
    return event, calls


def _expire_markers():
    """`FAILURE_TTL_S` elapsing, without waiting for it: every cached record's expiry is moved into
    the past, so the next read finds nothing standing and resubmits. A TTL of zero would not do --
    a marker that expires the instant it is written reads `pending` on the very read that wrote
    it, and the claim under test is what happens on the read AFTER the marker's life."""
    with notification_pull._enrichment_lock:
        for cache in (notification_pull._wordings, notification_pull._alternative_outcomes):
            for key, (_, record) in list(cache.items()):
                cache[key] = (0.0, record)


def _two_trains(now):
    return {
        "S1": _train(now + timedelta(hours=1), p_impact_dep=0.9),
        "S2": _train(now + timedelta(hours=2), jkey="t2", situation="S2", p_impact_dep=0.95),
    }


def test_a_patient_read_on_a_cold_definition_words_each_train_once_under_the_pull_allowance(monkeypatch):
    """AE1/R4: the model's wording and the alternative are fetched on request, one wording per
    train, charged to the PULL allowance and to nothing else."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot(_two_trains(now))
    backend = _real_backend(monkeypatch)
    r = _get()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["complete"] is True and r.headers["Cache-Control"] == "public, max-age=60"
    assert [a["wording"] for a in body["alerts"]] == [{"source": "llm", "status": "ok"}] * 2
    assert all(a["payload"]["notification"]["body"].endswith("\nToget er innstilt.") for a in body["alerts"])
    # The Journey Planner stub answers every lookup with an empty reply, which is a completed lookup.
    assert [a["alternative"]["status"] for a in body["alerts"]] == ["none_found", "none_found"]
    assert len(backend) == 2, "one call per train"
    assert notification_wording.spent("pull") == (2, 2)
    assert notification_wording.spent("webhook") == (0, 0)
    assert notification_wording.spent("browser") == (0, 0)
    assert alternatives._fetch_calls["poller"] == [] and alternatives._fetch_calls["request"] == []


def test_a_zero_wait_read_answers_pending_and_no_store_and_the_next_read_finds_the_answer(monkeypatch):
    """AE2/R5/R9: nothing ready is served as the template and no alternative under `pending`,
    `complete` false and `no-store`; the task runs on, and the next read assembles from the caches
    without asking anybody again."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot(_two_trains(now))
    release, matched = _hold_matcher(monkeypatch)
    words = _fake_wording(monkeypatch)
    r = _get(wait=0)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    body = r.json()
    assert body["complete"] is False and body["n_alerts"] == 2
    for entry in body["alerts"]:
        assert entry["wording"] == {"source": "deterministic", "status": "pending"}
        assert entry["alternative"] == {"status": "pending"}
        assert entry["payload"]["alternative_journey"] is None
    release.set()
    _join()
    assert matched == ["pull"] and len(words) == 2
    r = _get(wait=0)
    body = r.json()
    assert body["complete"] is True and r.headers["Cache-Control"] == "public, max-age=60"
    assert [a["wording"]["status"] for a in body["alerts"]] == ["ok", "ok"]
    assert matched == ["pull"] and len(words) == 2, "a warm read asks nobody"


def test_neither_a_registered_nor_an_ad_hoc_read_writes_the_pollers_state_the_log_or_the_store(monkeypatch):
    """R10, the boundary: `_messages`, `_alternatives`, `_notified`, the decision writer and the
    notification store are untouched by a read of a registered definition and by one of an ad-hoc
    definition on the same pair."""
    _reset()
    # The pass runs on the real wall clock and drops departed trains, so the departure has to be
    # ahead of REAL now -- which puts it on tomorrow's Oslo day late in the evening. The ad hoc
    # definition therefore differs from REGISTERED in `days` (the train's own weekday) and not in
    # the time of day: a narrower period fell outside a departure past its `end` every evening
    # after 21:00 Oslo, and a window of 00:00-23:59 on one weekday contains the train at any hour.
    dep = datetime.now(tz=_OSLO) + timedelta(hours=2)
    ad_hoc_definition = {"begin": "00:00", "end": "23:59", "days": [dep.weekday()]}
    assert ad_hoc_definition != REGISTERED
    snapshot = {"S1": _train(dep, p_cancel=0.82, p_impact_dep=0.9)}
    _use_snapshot(snapshot)
    monkeypatch.setattr(notification_wording, "webhook_delivery_enabled", lambda: True)
    written = _capture_writes(monkeypatch)
    store_calls = _use_notification_store(monkeypatch, [_webhook_doc()])
    _register_here(monkeypatch)
    varsel.check_and_notify(snapshot)
    decisions_written = []
    original_record = varsel._record_decisions
    monkeypatch.setattr(
        varsel, "_record_decisions", lambda *a, **k: decisions_written.append(1) or original_record(*a, **k)
    )
    before = (_poller_caches(), len(store_calls), len(decisions_written), len(written))
    assert _get(**REGISTERED).json()["alerts"][0]["wording"]["status"] == "ok"
    ad_hoc = _get(**ad_hoc_definition).json()
    assert ad_hoc["n_alerts"] == 1
    # SAME PAIR, different period -- and since KTD9 (2026-09-12) the poller's wording is keyed on
    # the pair, so this read is served the registered definition's text too. It therefore submits
    # NO enrichment task at all, which is why joining one would KeyError rather than block: an
    # entry the poller already answered carries no `wording_key`.
    assert ad_hoc["alerts"][0]["wording"] == {"source": "llm", "status": "ok"}
    assert (_poller_caches(), len(store_calls), len(decisions_written), len(written)) == before


def test_a_burst_past_the_pull_allowance_is_rate_limited_and_charges_no_other_surface(monkeypatch):
    """AE5: the PULL allowance binds on its own ledger; the webhook's, the browser's and the
    poller's Journey Planner budget are untouched, and the refusal is transient."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot(_two_trains(now))
    backend = _real_backend(monkeypatch)
    notification_wording._calls[notification_wording._PULL_PATH].minute.extend(
        [notification_wording.monotonic()] * notification_wording.WORDINGS_PER_MIN
    )
    with _captured() as records:
        r = _get()
    body = r.json()
    assert [a["wording"] for a in body["alerts"]] == [{"source": "deterministic", "status": "rate_limited"}] * 2
    assert body["complete"] is False and r.headers["Cache-Control"] == "no-store"
    assert backend == [], "no backend was reached"
    assert notification_wording.spent("webhook") == (0, 0)
    assert notification_wording.spent("browser") == (0, 0)
    assert alternatives._fetch_calls["poller"] == []
    bounded = [x.fields for x in records if x.fields.get("event") == "notification_pull_bounded"]
    assert [(b["reason"], b["n"]) for b in bounded] == [("wording_allowance", 2)]


def test_two_periods_sharing_a_train_word_it_once_and_share_the_pair_ceiling(monkeypatch):
    """AE9: a narrow and a wide period on one pair are each enriched for their own trains, the
    train they share is worded ONCE between them, and every period on the pair spends one limiter
    bucket (U3 pins the limiter; one assertion here)."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({
        "S1": _train(now + timedelta(hours=1), p_impact_dep=0.9),
        "S2": _train(now + timedelta(hours=12), jkey="t2", situation="S2", p_impact_dep=0.9),
    })
    words = _fake_wording(monkeypatch)
    narrow = _get(begin="06:00", end="08:00", days=ALL_DAYS).json()
    wide = _get(begin="06:00", end="19:00", days=ALL_DAYS).json()
    assert [a["train"]["journey_ref"] for a in narrow["alerts"]] == ["t1"]
    assert [a["train"]["journey_ref"] for a in wide["alerts"]] == ["t1", "t2"]
    assert narrow["complete"] and wide["complete"]
    assert [a["wording"]["status"] for a in wide["alerts"]] == ["ok", "ok"]
    assert sorted(c[1] for c in words) == ["S1", "S2"], words
    for i in range(notification_pull.PER_PAIR_PER_MIN - 2):
        assert _get(begin=f"{i % 24:02d}:{i:02d}", end="23:59", wait=0).status_code == 200, i
    assert _get(begin="00:00", end="12:00", wait=0).status_code == 429


def test_a_backend_error_is_transient_and_the_read_resubmits_after_the_failure_ttl(monkeypatch):
    """R5: `error` is served with the template and expires; so do `rate_limited` and a queue-full
    `capped` (the latter in the queue test below)."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    words = _fake_wording(monkeypatch, status="error")
    r = _get()
    entry = r.json()["alerts"][0]
    assert entry["wording"] == {"source": "deterministic", "status": "error"}
    assert r.headers["Cache-Control"] == "no-store" and len(words) == 1
    assert _get().json()["alerts"][0]["wording"]["status"] == "error" and len(words) == 1, "the marker stands"
    _expire_markers()
    words = _fake_wording(monkeypatch, status="rate_limited")
    entry = _get().json()["alerts"][0]
    assert entry["wording"]["status"] == "rate_limited" and len(words) == 1, "the expired marker was resubmitted"
    _expire_markers()
    words = _fake_wording(monkeypatch)
    r = _get()
    assert r.json()["alerts"][0]["wording"] == {"source": "llm", "status": "ok"} and len(words) == 1
    assert r.headers["Cache-Control"] == "public, max-age=60"
    assert _get().json()["alerts"][0]["wording"]["status"] == "ok" and len(words) == 1, "ok is terminal"


def test_a_throttled_journey_planner_is_unanswered_never_none_found(monkeypatch):
    """A lookup that obtained no reply is a transient absence: `unanswered`, resubmitted on expiry,
    and never cached as the terminal `none_found` an answered-but-empty reply earns."""
    import impact

    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    replied = {"value": False}

    def throttled(*a, outcome=None, **k):
        if outcome is not None:
            outcome["answered"] = replied["value"]
        return []

    monkeypatch.setattr(impact.alternatives, "fetch_candidates", throttled)
    _fake_wording(monkeypatch)
    r = _get()
    entry = r.json()["alerts"][0]
    assert entry["alternative"] == {"status": "unanswered"} and entry["wording"]["status"] == "ok"
    assert r.json()["complete"] is False
    replied["value"] = True
    assert _get().json()["alerts"][0]["alternative"] == {"status": "unanswered"}, "the marker stands"
    _expire_markers()
    r = _get()
    assert r.json()["alerts"][0]["alternative"] == {"status": "none_found"} and r.json()["complete"] is True


def test_a_full_queue_answers_capped_at_once_with_the_figures_and_resubmits_on_expiry(monkeypatch):
    """AE8/KTD5: with `WORKERS + QUEUE_MAX` tasks unfinished a new definition's read is `capped`
    (transient) with its figures served, `complete` false and `no-store`; once the queue drains and
    the marker expires the next read resubmits."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    release, matched = _hold_matcher(monkeypatch)
    _fake_wording(monkeypatch)
    n = notification_pull.WORKERS + notification_pull.QUEUE_MAX
    periods = [(f"00:{i:02d}", "23:59") for i in range(n)]  # every one holds the 07:00 train
    for begin, end in periods:
        assert _get(begin=begin, end=end, days=ALL_DAYS, wait=0).json()["alerts"][0]["wording"]["status"] == "pending"
    with notification_pull._tasks_lock:
        assert sum(not f.done() for f in notification_pull._inflight.values()) == n
    with _captured() as records:
        r = _get(begin="05:30", end="23:59", days=ALL_DAYS, wait=0)
    body = r.json()
    assert r.status_code == 200 and body["n_alerts"] == 1, "the figures are served"
    assert body["alerts"][0]["wording"] == {"source": "deterministic", "status": "capped"}
    assert body["alerts"][0]["alternative"] == {"status": "capped"}
    assert body["complete"] is False and r.headers["Cache-Control"] == "no-store"
    bounded = [x.fields for x in records if x.fields.get("event") == "notification_pull_bounded"]
    assert [(b["reason"], b["n"]) for b in bounded] == [("queue_full", 1)]
    assert set(bounded[0]) <= {"event", "reason", "n", "pair_digest"}
    # Within the marker's life nothing is submitted again; past it the read resubmits.
    assert _get(begin="05:30", end="23:59", days=ALL_DAYS, wait=0).json()["alerts"][0]["wording"]["status"] == "capped"
    release.set()
    for begin, end in periods:
        _join(begin=begin, end=end, days=ALL_DAYS)
    assert len(matched) == n
    _expire_markers()
    body = _get(begin="05:30", end="23:59", days=ALL_DAYS).json()
    assert body["complete"] is True and body["alerts"][0]["wording"]["status"] == "ok"
    assert len(matched) == n + 1


def test_without_a_waiter_slot_a_positive_wait_read_answers_at_once_with_200(monkeypatch):
    """KTD9: past `MAX_CONCURRENT_WAITERS` a read behaves as `wait=0` -- never a 503."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    release, _ = _hold_matcher(monkeypatch)
    _fake_wording(monkeypatch)
    monkeypatch.setattr(notification_pull, "MAX_CONCURRENT_WAITERS", 0)
    started = notification_pull.monotonic()
    with _captured() as records:
        r = _get(wait=notification_pull.MAX_WAIT_S)
    assert r.status_code == 200 and r.json()["alerts"][0]["wording"]["status"] == "pending"
    assert notification_pull.monotonic() - started < 1.0, "it did not wait"
    assert [x.fields["reason"] for x in records if x.fields.get("event") == "notification_pull_bounded"] == ["waiters"]
    release.set()
    _join()
    assert notification_pull._waiters == 0


def test_beyond_the_two_caps_entries_read_not_worded_and_not_looked_up_and_count_as_terminal(monkeypatch):
    """KTD7: the wording cap is spent soonest-first and the rest carry `not_worded`; a train past
    the matcher's lookup cap reads `not_looked_up`; both are terminal, so the answer is complete."""
    import impact

    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({
        f"S{i}": _train(now + timedelta(minutes=30 + 10 * i), jkey=f"t{i}", situation=f"S{i}", p_impact_dep=0.9)
        for i in range(6)
    })
    words = _fake_wording(monkeypatch)
    with _captured() as records:
        r = _get()
    body = r.json()
    assert body["complete"] is True and r.headers["Cache-Control"] == "public, max-age=60"
    cap = notification_pull.MAX_WORDINGS_PER_READ
    assert [a["wording"]["status"] for a in body["alerts"]] == ["ok"] * cap + ["not_worded"] * (6 - cap)
    assert sorted(c[1] for c in words) == [f"S{i}" for i in range(cap)]
    lookups = impact._MAX_ALTERNATIVE_LOOKUPS
    assert [a["alternative"]["status"] for a in body["alerts"]] == ["none_found"] * lookups + ["not_looked_up"] * (6 - lookups)
    bounded = sorted((x.fields["reason"], x.fields["n"]) for x in records if x.fields.get("event") == "notification_pull_bounded")
    assert bounded == [("alternative_lookups", 6 - lookups), ("wordings_per_read", 6 - cap)]


def test_a_cancelled_train_refuses_the_pollers_forecast_wording_and_is_worded_fresh(monkeypatch):
    """AE6: the poller's cache holds a wording written while the train was merely at risk; once ET
    declares it cancelled the read refuses it (`cached_wording_and_alternative`'s rule) and words the
    train fresh as cancelled."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    dep = now + timedelta(hours=1)
    _use_snapshot({"S1": _train(dep, et_declared_cancelled=True, p_impact_dep=0.6, p_cancel=0.4)})
    monkeypatch.setattr(notification_wording, "webhook_delivery_enabled", lambda: True)
    _register_here(monkeypatch)
    key = varsel.journey_key({"from_stop": NSR_A, "to_stop": NSR_B, **REGISTERED})
    with varsel._state_lock:
        varsel._messages[(key, "S1", dep.astimezone(UTC).isoformat())] = "Toget kan bli 5 min forsinket."
    words = _fake_wording(monkeypatch, text="Toget er innstilt.")
    entry = _get(**REGISTERED).json()["alerts"][0]
    assert entry["phase"] == "confirmed"
    assert entry["wording"] == {"source": "llm", "status": "ok"}
    assert entry["payload"]["notification"]["body"].endswith("\nToget er innstilt.")
    assert "innstilt" in entry["payload"]["notification"]["title"]
    assert len(words) == 1, "worded fresh, not served from the poller's cache"


def test_a_wording_for_tomorrows_train_is_not_served_for_a_read_of_todays_train(monkeypatch):
    """`i_morgen` is in the wording key: written at 23:00 for the 00:30, the text says «i morgen»,
    and a read after midnight of the same train under the same situation re-words."""
    now = datetime(2026, 9, 10, 23, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1, minutes=30), p_impact_dep=0.9)})
    words = _fake_wording(monkeypatch)
    entry = _get(days=ALL_DAYS).json()["alerts"][0]
    assert entry["payload"]["call"]["departs_tomorrow"] is True and len(words) == 1
    notification_pull._now = lambda: now + timedelta(hours=1, minutes=10)  # 00:10 the next day
    entry = _get(days=ALL_DAYS).json()["alerts"][0]
    assert entry["payload"]["call"]["departs_tomorrow"] is False
    assert entry["wording"]["status"] == "ok" and len(words) == 2


def _alternative(departs_at, minutes_saved=10):
    return alternatives.AlternativeJourney(
        departure_time=departs_at, arrival_time=departs_at + timedelta(minutes=30),
        duration_min=30, minutes_saved=minutes_saved, arrives_before_original=True, at_risk=False, legs=[],
    )


def test_a_changed_alternative_re_words_once_and_a_dip_under_the_gate_re_words_on_the_refresh(monkeypatch):
    """KTD3's identity term and the third forget rule: the same alternative reuses the wording, a
    different one re-words once, and a train that dips under the gate and re-crosses within
    `WORDING_TTL_S` is re-worded because the figures refresh dropped its wording."""
    from test_varsel_api import _selection_outcome

    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    dep = now + timedelta(hours=1)
    alert = {"S1": _train(dep, p_impact_dep=0.9)}
    _use_snapshot(alert)
    first = _alternative(now + timedelta(minutes=50))
    chosen = _selection_outcome(monkeypatch, (first, False, ("VYG", "t1")))
    words = _fake_wording(monkeypatch)
    entry = _get().json()["alerts"][0]
    assert entry["alternative"] == {"status": "offered"} and len(words) == 1
    assert entry["payload"]["alternative_journey"]["departure_time"] is not None

    def forget_outcome():
        """The Journey Planner cache's life elapsing, without waiting `alternatives._TTL`."""
        with notification_pull._enrichment_lock:
            notification_pull._alternative_outcomes.clear()

    forget_outcome()
    assert _get().json()["alerts"][0]["wording"]["status"] == "ok" and len(words) == 1, "same advice, same wording"
    chosen["value"] = (_alternative(now + timedelta(minutes=55)), False, ("VYG", "t1"))
    forget_outcome()
    entry = _get().json()["alerts"][0]
    assert entry["wording"]["status"] == "ok" and len(words) == 2, "different advice, one re-wording"
    forget_outcome()
    assert _get().json()["alerts"][0]["wording"]["status"] == "ok" and len(words) == 2

    # The dip: a refresh in which t1 is under the gate lists nothing, and the one after re-lists it.
    _use_snapshot({"S1": _train(dep, p_impact_dep=0.30)})
    notification_pull.configure(refreshed_at=lambda: LOADED_AT + timedelta(minutes=5))
    assert _get().json()["n_alerts"] == 0
    _use_snapshot(alert)
    notification_pull.configure(refreshed_at=lambda: LOADED_AT + timedelta(minutes=10))
    entry = _get().json()["alerts"][0]
    assert entry["wording"]["status"] == "ok" and len(words) == 3, "re-crossing the gate re-words"


def test_two_concurrent_cold_reads_start_one_task_and_a_timed_out_waiter_does_not_cancel_it(monkeypatch):
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    release, matched = _hold_matcher(monkeypatch)
    words = _fake_wording(monkeypatch)
    answers = []

    def read():
        answers.append(_get(wait=1).json())

    threads = [threading.Thread(target=read) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert len(answers) == 2 and all(a["alerts"][0]["wording"]["status"] == "pending" for a in answers)
    assert matched == ["pull"], "one task between the two reads"
    with notification_pull._tasks_lock:
        assert len(notification_pull._inflight) == 1
        assert not next(iter(notification_pull._inflight.values())).cancelled()
    release.set()
    _join()
    assert len(words) == 1
    assert _get(wait=0).json()["alerts"][0]["wording"]["status"] == "ok", "the timed-out waiter left the task running"


def test_a_task_from_before_a_reset_cannot_write_into_the_next_tests_caches(monkeypatch):
    """KTD5's epoch: park a task, reset, release, join the OLD future -- the caches stay empty."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    release, _ = _hold_matcher(monkeypatch)
    words = _fake_wording(monkeypatch)
    assert _get(wait=0).json()["alerts"][0]["wording"]["status"] == "pending"
    with notification_pull._tasks_lock:
        (old,) = notification_pull._inflight.values()
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})
    release.set()
    old.result(timeout=5)
    assert len(words) == 1, "the old task ran to completion"
    assert notification_pull._wordings == {} and notification_pull._alternative_outcomes == {}
    assert notification_pull._inflight == {}


def test_a_pull_configuration_error_latches_the_pull_holder_only(monkeypatch):
    """R4, the latch half, end to end through the read: a backend the project cannot serve latches
    `PULL.state()` and neither the poller's holder nor the webhook channel's."""
    from llm_message import _ConfigurationError

    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({"S1": _train(now + timedelta(hours=1), p_impact_dep=0.9)})

    def unserved(_body):
        raise _ConfigurationError("model not enabled in this project")

    _real_backend(monkeypatch, reply=unserved)
    entry = _get().json()["alerts"][0]
    assert entry["wording"]["source"] == "deterministic"
    assert llm_message.generation_enabled(notification_wording.PULL.state()) is False
    assert llm_message.generation_enabled() is True, "the poller's holder is untouched"
    assert llm_message.generation_enabled(notification_wording.WEBHOOK.state()) is True


def test_the_module_shuts_its_pool_down_without_waiting():
    """`main.lifespan` calls this on exit; a parked task must not hold the process open."""
    _reset()
    notification_pull.shutdown()
    assert notification_pull._executor._shutdown is True
    _reset()  # a fresh executor for whatever runs after this test
    assert notification_pull._executor._shutdown is False


def _six_trains(now):
    """Six alert-worthy trains at ten-minute intervals from 06:30 -- two past the wording cap."""
    return {
        f"S{i}": _train(now + timedelta(minutes=30 + 10 * i), jkey=f"t{i}", situation=f"S{i}", p_impact_dep=0.9)
        for i in range(6)
    }


def test_not_worded_is_a_position_in_this_reads_order_not_a_marker_another_definition_can_read(monkeypatch):
    """Finding #4 (2026-09-10): `not_worded` is POSITIONAL -- past the cap in the order of the
    definition being read -- and never written to the cache. Persisted under KTD3's key, which
    carries no definition term, a wide definition's fifth train poisoned every narrower definition
    on the pair whose FIRST train it was: that read found a terminal record, submitted no task,
    and served the template as a complete, publicly cacheable answer for twelve hours."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot(_six_trains(now))
    words = _fake_wording(monkeypatch)
    cap = notification_pull.MAX_WORDINGS_PER_READ
    wide = _get().json()
    assert [a["wording"]["status"] for a in wide["alerts"]] == ["ok"] * cap + ["not_worded"] * 2
    assert len(words) == cap

    # A period covering only the fifth train (07:10): for THIS definition it is position 0.
    narrow = {"begin": "07:05", "end": "07:15", "days": ALL_DAYS}
    r = _get(wait=0, **narrow)
    body = r.json()
    assert [a["train"]["journey_ref"] for a in body["alerts"]] == ["t4"]
    assert body["alerts"][0]["wording"] == {"source": "deterministic", "status": "pending"}, body["alerts"][0]
    assert body["complete"] is False and r.headers["Cache-Control"] == "no-store"
    _join(**narrow)
    assert sorted(c[1] for c in words) == [f"S{i}" for i in range(cap + 1)], "worded once, for the narrow read"
    body = _get(wait=0, **narrow).json()
    assert body["alerts"][0]["wording"] == {"source": "llm", "status": "ok"} and body["complete"] is True

    # Back on the wide definition: a live record wins over position, so its fifth train now reads
    # the wording the narrow read paid for; the sixth is still past ITS cap, and no task is owed.
    wide = _get(wait=0).json()
    assert [a["wording"]["status"] for a in wide["alerts"]] == ["ok"] * (cap + 1) + ["not_worded"]
    assert wide["complete"] is True and len(words) == cap + 1


def test_a_train_past_the_cap_is_worded_once_the_trains_ahead_of_it_have_departed(monkeypatch):
    """The same finding on one definition over time: the fifth train of a whole-day read is
    `not_worded` at 06:00 and, once the four ahead of it have left, position 0 -- so the next read
    finds it `pending` and resubmits the task, which words it. Same Oslo day, so the figures cache
    hits and the departure cut is the assembly's (`_alive`), never a matcher re-run."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot({k: v for k, v in _six_trains(now).items() if k != "S5"})
    words = _fake_wording(monkeypatch)
    cap = notification_pull.MAX_WORDINGS_PER_READ
    body = _get().json()
    assert [a["wording"]["status"] for a in body["alerts"]] == ["ok"] * cap + ["not_worded"]
    assert len(words) == cap
    calls = _count_matcher_calls()
    # 07:05: the 07:00 train's expected departure (aimed + 3 min typical) has passed; the 07:10 has not.
    notification_pull._now = lambda: now + timedelta(minutes=65)
    r = _get(wait=0)
    body = r.json()
    assert [a["train"]["journey_ref"] for a in body["alerts"]] == ["t4"]
    assert body["alerts"][0]["wording"] == {"source": "deterministic", "status": "pending"}, body["alerts"][0]
    assert body["complete"] is False
    _join()
    assert sorted(c[1] for c in words) == [f"S{i}" for i in range(cap + 1)], "the resubmitted task worded it"
    body = _get(wait=0).json()
    assert body["alerts"][0]["wording"] == {"source": "llm", "status": "ok"} and body["complete"] is True
    assert calls() == 1, "the figures were a hit; only the resubmitted task ran the matcher"


def _failing_enrichment_matcher(monkeypatch):
    """`varsel.match_journey` as the figures phase sees it, and a `RuntimeError` on the enrichment
    task's own run (`with_alternative=True`) -- the branch `_hold_matcher` can park but never fail.
    Returns the count of enrichment runs attempted."""
    calls = []
    original = _ORIGINAL_MATCH

    def failing(journey, now=None, *, with_alternative=False, path="poller"):
        if with_alternative:
            calls.append(path)
            raise RuntimeError("boom")
        return original(journey, now, with_alternative=with_alternative, path=path)

    monkeypatch.setattr(varsel, "match_journey", failing)
    return calls


def test_a_matcher_failure_inside_the_task_marks_every_entry_transient_and_the_read_resubmits_on_expiry(monkeypatch):
    """Finding #3 (2026-09-10), the enrichment half of `test_a_matcher_failure_is_a_503_...`: the
    figures phase succeeds and the task's own matcher run raises. Every listed entry then carries
    `wording error` / `alternative unanswered` -- both transient -- `complete` is false and the
    answer is `no-store`; the WARNING names the exception's TYPE and nothing of the definition;
    and once the markers expire the next read resubmits the task."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot(_two_trains(now))
    runs = _failing_enrichment_matcher(monkeypatch)
    words = _fake_wording(monkeypatch)
    with _captured() as records:
        assert _get(wait=0).json()["alerts"][0]["wording"]["status"] == "pending"
        _join()
        r = _get(wait=0)
    body = r.json()
    assert runs == ["pull"] and words == []
    assert body["complete"] is False and r.headers["Cache-Control"] == "no-store"
    for entry in body["alerts"]:
        assert entry["wording"] == {"source": "deterministic", "status": "error"}, entry["wording"]
        assert entry["alternative"] == {"status": "unanswered"}
    warnings = [x for x in records if x.levelno >= logging.WARNING]
    assert len(warnings) == 1, [(x.getMessage(), getattr(x, "fields", {})) for x in warnings]
    (record,) = warnings
    assert record.fields["event"] == "notification_pull" and record.fields["result"] == "enrich_failed"
    assert record.fields["pair_digest"] == alternatives.pair_digest(NSR_A, NSR_B)
    assert "RuntimeError" in record.getMessage() and record.exc_info is None
    text = record.getMessage() + " " + str(record.fields)
    assert not any(token in text for token in ("NSR:", "Asker", "Oslo S", "boom")), text
    # Within the markers' life nothing is submitted again; past it the read resubmits.
    assert _get(wait=0).json()["alerts"][0]["wording"]["status"] == "error" and runs == ["pull"]
    _expire_markers()
    assert _get(wait=0).json()["alerts"][0]["wording"]["status"] == "pending"
    _join()
    assert runs == ["pull", "pull"], "the expired markers were resubmitted"


def test_a_raising_wording_writer_is_one_entrys_transient_error_and_is_retried_after_the_failure_ttl(monkeypatch):
    """Finding #3 (2026-09-10), the wording half: `notification_wording.write_wording` RAISES
    rather than returning an `error` wording (which `test_a_backend_error_is_transient_...` covers).
    `_word_one`'s own fallback records `error` for that entry alone, logs it under
    `notification_wording` with no ref, and the marker expires like any transient one."""
    now = datetime(2026, 9, 10, 6, 0, tzinfo=_OSLO)
    _reset(now=now)
    _use_snapshot(_two_trains(now))
    raised = []

    def raising(route, facts, *, policy, subject_id="", now=None, budget=None, message_input=None):
        raised.append(facts.situation_number)
        raise RuntimeError("boom")

    monkeypatch.setattr(notification_wording, "write_wording", raising)
    with _captured() as records:
        r = _get()
        _join()
    body = r.json()
    assert sorted(raised) == ["S1", "S2"]
    assert [a["wording"] for a in body["alerts"]] == [{"source": "deterministic", "status": "error"}] * 2
    assert body["complete"] is False and r.headers["Cache-Control"] == "no-store"
    warnings = [x for x in records if x.levelno >= logging.WARNING]
    assert len(warnings) == 2, [(x.getMessage(), getattr(x, "fields", {})) for x in warnings]
    for record in warnings:
        assert record.fields == {"event": "notification_wording", "result": "error", "surface": "pull"}
        text = record.getMessage() + " " + str(record.fields)
        assert not any(token in text for token in ("NSR:", "Asker", "Oslo S", "boom")), text
    assert _get().json()["alerts"][0]["wording"]["status"] == "error" and len(raised) == 2, "the marker stands"
    _expire_markers()
    words = _fake_wording(monkeypatch)
    r = _get()
    assert [a["wording"] for a in r.json()["alerts"]] == [{"source": "llm", "status": "ok"}] * 2
    assert sorted(c[1] for c in words) == ["S1", "S2"] and len(raised) == 2, "retried once each after the TTL"
    assert r.headers["Cache-Control"] == "public, max-age=60"


# ---- The pass's warm (U15) ---------------------------------------------------------------------
#
# Two of U15's stated scenarios need no test of their own and are named here so nobody adds a slow
# duplicate: "the 3 001st request in a minute is refused" and "the 20 001st definition evicts the
# oldest figure" are `test_the_global_ceiling_refuses...` and `test_the_figures_cache_is_bounded`
# above, both of which set the constant small and so assert the RULE rather than the number. The
# number itself is pinned against the published-numbers table by `test_notification_contract`.


@contextmanager
def _warming():
    """Wire the pass's warm seam, as `main.lifespan` does, and put it back.

    Off in every other test in this suite and in `test_varsel_api`, which is the point: absent, pass
    1a must be byte-for-byte the pass that ran before U15, and 250-odd checks either side of this
    one assert that by never noticing it exists.
    """
    stashed = varsel._figures_warmer
    varsel._figures_warmer = notification_pull.warm_figures
    try:
        yield
    finally:
        varsel._figures_warmer = stashed


def test_the_pass_warms_a_registered_definition_and_the_read_then_computes_nothing(monkeypatch):
    """U15's first scenario: after pass 1a a read naming the registration's five fields is a
    figures HIT, for the whole cycle and for every caller.

    Falsified by making the matcher RAISE before the read rather than by counting calls: a read
    that still answers, with the trains the pass found, cannot have run it. The same read without
    the seam wired is the control, and it 503s -- which is what makes the assertion about warming
    and not about some other cache.
    """
    _reset()
    dep = datetime.now(tz=_OSLO) + timedelta(hours=2)
    snapshot = {"S1": _train(dep, p_cancel=0.82, p_impact_dep=0.9)}
    _use_snapshot(snapshot)
    _register_here(monkeypatch)

    with _warming():
        varsel.check_and_notify(snapshot)

    def _refuse(*a, **kw):
        raise AssertionError("the read ran the matcher for a definition the pass warmed")

    monkeypatch.setattr(varsel, "match_journey", _refuse)
    r = _get(**REGISTERED, wait=0)
    assert r.status_code == 200, r.text
    assert r.json()["n_alerts"] == 1, r.json()

    # The control, and the falsification: a definition the pass did NOT warm still computes, so the
    # stubbed matcher is reached and the read fails the way it should.
    assert _get(begin="05:00", end="06:00", wait=0).status_code == 503


def test_a_warm_on_a_fresh_process_establishes_the_generation_it_stores_under():
    """`_remember_figures` refuses a store whose generation marker does not match, and the marker
    was only ever SET by a read (`_cached_figures`). The pass warms BEFORE any read of a fresh
    process, so without `_enter_generation` on the warm path every warmed figure is discarded on
    the way in, every read still misses, and nothing anywhere says so -- a warm pass that spends
    the pass's time and buys nothing.

    Falsify by dropping the `_enter_generation` call from `warm_figures`: `_figures` stays empty.
    """
    _reset()
    assert notification_pull._figures_generation is None, "control: nothing has read yet"
    now = notification_pull._now()
    definition = notification_pull._definition(NSR_A, NSR_B, None, None, None, now)
    notification_pull.warm_figures(varsel.journey_key(definition), definition, [], now)
    assert notification_pull._figures_generation == LOADED_AT
    assert len(notification_pull._figures) == 1, notification_pull._figures


def test_a_warm_leaves_a_figure_a_read_already_computed_alone():
    """A read that beat the pass to the key already built the same figures AND already applied the
    third forget rule; applying it twice is not free, and re-storing buys nothing."""
    _reset()
    _get(wait=0)
    before = dict(notification_pull._figures)
    assert len(before) == 1
    now = notification_pull._now()
    definition = notification_pull._definition(NSR_A, NSR_B, None, None, None, now)
    notification_pull.warm_figures(varsel.journey_key(definition), definition, [], now)
    after = dict(notification_pull._figures)
    assert list(after) == list(before)
    assert all(after[k] is before[k] for k in before), "the read's own figures object still stands"


def test_a_warm_that_raises_costs_a_cache_entry_and_never_the_journeys_alerts(monkeypatch):
    """The warm sits in its OWN try inside pass 1a, outside the matcher's. The matcher's answers an
    exception with empty alerts for that journey -- right for a match that failed, catastrophic for
    a warm that did, because it would drop a registered traveller's alerts for the whole cycle to
    save a cache entry. The read is a READER of the pass, never a step of it.
    """
    _reset()
    dep = datetime.now(tz=_OSLO) + timedelta(hours=2)
    snapshot = {"S1": _train(dep, p_cancel=0.82, p_impact_dep=0.9)}
    _use_snapshot(snapshot)
    pushed = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: pushed.append(payload) or "sent")
    _register(DEVICE, from_stop=NSR_A, from_name="Asker stasjon", to_stop=NSR_B, to_name="Oslo S", **REGISTERED)

    def _boom(*a, **kw):
        raise RuntimeError("the read's cache is not the pass's problem")

    stashed = varsel._figures_warmer
    varsel._figures_warmer = _boom
    try:
        varsel.check_and_notify(snapshot)
    finally:
        varsel._figures_warmer = stashed
    assert pushed, "the traveller was still pushed for the train the pass matched"
    assert not notification_pull._figures, "and nothing was cached, which is the whole cost"


def test_an_alternative_the_pass_found_never_reaches_a_read_that_established_none():
    """The one field that makes a WARMED answer differ from a cold one, and it is invisible to any
    test that stubs the matcher.

    `llm_message.build_message_input` reads `alert.alternative_journey` **or**, failing that,
    `impact.alternative_journey` -- the worst train's answer, kept as a fallback for callers that
    hold only the response. Every alert the poller's pass produces carries the second
    (`with_alternative=True`), and U15 warms a registered definition's figures from exactly those
    alerts. Left alone, such a winner would put an `alternativ_reise` into the message input -- and
    so into the wording key and the payload -- that the same definition's answer on a cold cycle
    does not have. `_enriched` clears it, which is a no-op for every winner this module computed
    itself.

    Falsify by dropping the `impact` half of `_enriched`'s update: `alternativ_reise` appears.
    """
    _reset()
    now = notification_pull._now()
    definition = notification_pull._definition(NSR_A, NSR_B, None, None, None, now)
    dep = now.astimezone(_OSLO) + timedelta(hours=2)
    _use_snapshot({"S1": _train(dep, p_cancel=0.82, p_impact_dep=0.9)})
    figures = notification_pull._compute_figures(definition, now, journey_key="k")
    winner = figures.entries[0].winner
    assert winner.impact is not None and winner.alternative_journey is None, "control: a cold winner"
    # BOTH halves of the control. If a cold match could attach a response-level alternative -- from
    # `alternatives._cache`'s TTL'd answer for a pair the poller looked up minutes ago -- then reads
    # were already serving `alternativ_reise` through the fallback before U15, and `_enriched`'s
    # clear would be a behaviour change on the existing path rather than a no-op on it. It cannot:
    # `_compute_figures` calls `match_journey` without `with_alternative`, so no lookup is consulted.
    assert winner.impact.alternative_journey is None, (
        "a cold read's winner must carry no response-level alternative either, or clearing it in "
        "`_enriched` changes what every pre-U15 read served"
    )

    journey = varsel.AlternativeJourney(
        legs=[], departure_time=dep + timedelta(minutes=20), arrival_time=dep + timedelta(minutes=70),
        minutes_saved=-30, arrives_before_original=False, at_risk=False, scored=False,
    )
    # A winner as pass 1a produces one: the response copy carries the alternative, the alert itself
    # does not, because the cache had nothing to say about THIS train.
    as_the_pass_has_it = winner.model_copy(
        update={"impact": winner.impact.model_copy(update={"alternative_journey": journey})}
    )
    assert notification_pull._message_input(definition, as_the_pass_has_it, now).get("alternativ_reise"), (
        "control: the fallback is real -- an un-enriched winner carries it into the message input"
    )
    enriched = notification_pull._enriched(as_the_pass_has_it, None)
    assert "alternativ_reise" not in notification_pull._message_input(definition, enriched, now)
    assert notification_pull._enriched(as_the_pass_has_it, journey).alternative_journey is journey


def test_the_forget_rule_drops_a_trains_wording_by_train_and_not_by_scanning():
    """The third forget rule pops by TRAIN; `_wordings` is keyed by KTD9's wording key, which does
    not begin with one. The scan that answered it was affordable on a read miss and is not once U15
    runs it per registered definition per cycle, so the index answers it instead.

    Both directions: the gone train's wording goes, and a live train's on the same pair stays --
    the second is what an index that drops too much would fail.
    """
    _reset()
    pair = (NSR_A, NSR_B)
    keys = {}
    for name, train in (("gone", ("VYG", "t1", 1)), ("stays", ("VYG", "t2", 1))):
        keys[name] = (pair, name)
        notification_pull._remember_wording(
            keys[name], notification_pull._WordingRecord("tekst", "ok", train), notification_pull._epoch
        )
        notification_pull._listed_trains.setdefault("k", frozenset())
    assert set(notification_pull._wording_keys_by_train) == {(pair, ("VYG", "t1", 1)), (pair, ("VYG", "t2", 1))}

    # "k" listed both trains last time and lists only the second now.
    notification_pull._listed_trains["k"] = frozenset({("VYG", "t1", 1), ("VYG", "t2", 1)})
    entry = notification_pull._Entry(
        winner=None, contributing=(), train=None, train_key=("VYG", "t2", 1),
        expected_departure=notification_pull._now(), situations=(),
    )
    notification_pull._forget_unlisted("k", pair, [entry])
    assert keys["gone"] not in notification_pull._wordings, "the train that left lost its wording"
    assert keys["stays"] in notification_pull._wordings, "and the one still listed kept its own"
    assert (pair, ("VYG", "t1", 1)) not in notification_pull._wording_keys_by_train


def test_the_wording_index_is_rebuilt_rather_than_evicted_when_it_outgrows_the_cache():
    """Evicting the index alone would retire the forget rule for whichever train went, which is a
    wrong answer rather than a miss. A rebuild drops exactly what eviction and expiry left behind,
    and costs one pass over a bounded cache once per `WORDING_CACHE_MAX` writes."""
    _reset()
    stashed = notification_pull.WORDING_CACHE_MAX
    notification_pull.WORDING_CACHE_MAX = 3
    try:
        for n in range(6):
            notification_pull._remember_wording(
                ((NSR_A, NSR_B), n), notification_pull._WordingRecord("t", "ok", ("VYG", f"t{n}", 1)),
                notification_pull._epoch,
            )
        assert len(notification_pull._wordings) <= 3
        # Every index entry names a key the cache still holds, and every held key is indexed.
        indexed = {k for keys in notification_pull._wording_keys_by_train.values() for k in keys}
        assert indexed == set(notification_pull._wordings), (indexed, set(notification_pull._wordings))
    finally:
        notification_pull.WORDING_CACHE_MAX = stashed


def test_the_coverage_set_is_built_once_per_stop_cache_refresh_and_not_per_request():
    """U15's third constant-sized cost. The set is over the poller's WHOLE stop cache, and at
    `MAX_PER_MIN` that is thousands of rebuilds a minute of a list that changes at most daily.

    Keyed on the ROWS rather than on the prediction-cache generation, which U15's plan proposed:
    coverage is a function of the stop cache, and the two refresh on different TTLs -- keyed on the
    generation the set would rebuild every cycle for a list that had not moved AND still report a
    newly-served stop place as absent until the next cycle. `test_coverage_names_the_served_
    operators_and_reads_the_stop_cache_over_every_ref` is what pins the freshness half: it swaps
    the provider between reads inside one generation and the answer follows.
    """
    _reset()
    builds = []
    stashed = varsel._stops_provider
    rows = list(PULL_STOPS)
    try:
        varsel._stops_provider = lambda: (builds.append(1), rows)[1]
        _get(wait=0)
        _get(wait=0)
        assert len(builds) == 2, "the provider is still consulted per request -- that part is O(1)"
        assert notification_pull._coverage_rows is rows
        present = notification_pull._coverage_present
        _get(wait=0)
        assert notification_pull._coverage_present is present, "and the SET was not rebuilt"
    finally:
        varsel._stops_provider = stashed


# ---- The journey-status read (plan 2026-09-22-001, U2) ----------------------------------------

STATUS_PATH = "/notification-api/v1/journey-status"
STATUS_NOW = datetime(2026, 9, 22, 7, 42, tzinfo=_OSLO)  # a Tuesday


def _status_train(jkey, dep, **alight):
    """Two planned calls on one Vy train: board at NSR_A, alight at NSR_B 40 minutes later."""
    import journey_status

    common = {"operator": "VYG", "jkey": jkey, "line": "VYG:Line:L1", "dep_delay_min": None,
              "arr_delay_min": None, "call_cancelled": False, "call_source": "estimated"}
    return [
        journey_status.PlannedCall(stop_place=NSR_A, stop_order=1, aimed_dep=dep, aimed_arr=None, **common),
        journey_status.PlannedCall(**{**common, **alight}, stop_place=NSR_B, stop_order=5,
                                   aimed_dep=None, aimed_arr=dep + timedelta(minutes=40)),
    ]


def _status_reset(rows=None, *, now=STATUS_NOW):
    import journey_status

    _reset(now=now)
    journey_status.reset_state()
    journey_status.configure(predictions_refreshed_at=lambda: LOADED_AT, place_refs=varsel.place_refs)
    if rows is not None:
        journey_status.set_spine(journey_status.CallTable(rows, now - timedelta(minutes=30)))


def _status(**params):
    return client.get(STATUS_PATH, params={"from_stop": NSR_A, "to_stop": NSR_B, **params})


def test_journey_status_answers_every_train_in_the_window_from_memory():
    dep = STATUS_NOW + timedelta(minutes=6)
    _status_reset(_status_train("a", dep) + _status_train("late", dep + timedelta(hours=3)))
    r = _status(begin="07:00", end="09:00", days=[1])
    assert r.status_code == 200, r.text
    assert r.headers["Cache-Control"] == "public, max-age=60"
    body = r.json()
    assert [t["jkey"] for t in body["trains"]] == ["a"], "the 10:48 is outside 07:00-09:00"
    assert body["trains"][0]["state"] == "unknown", "no prediction for it: grey, never green"
    assert body["coverage"]["operators"] == ["VYG"]
    assert body["journey"] == {"from_stop": NSR_A, "to_stop": NSR_B, "begin": "07:00", "end": "09:00", "days": [1]}
    assert body["as_of"]["predictions_refreshed_at"] is not None
    assert body["reason"] is None and body["window"] is not None


def test_journey_status_is_503_while_the_timetable_is_not_loaded():
    _status_reset()
    r = _status()
    assert r.status_code == 503 and r.json()["detail"] == "plan_not_loaded"
    assert r.headers["Retry-After"] == "60"


def test_journey_status_on_a_day_not_travelled_is_empty_with_a_reason():
    _status_reset(_status_train("a", STATUS_NOW + timedelta(minutes=6)))
    body = _status(days=[5, 6]).json()
    assert body["trains"] == [] and body["reason"] == "not_a_travel_day"


def test_journey_status_moves_a_cancelled_train_from_ring_to_fill_between_two_reads():
    """Nothing time-dependent is cached (KTD5): two reads straddling the aimed departure of a
    declared-cancelled train answer `predicted` then `confirmed`, and `now` moves between them."""
    dep = STATUS_NOW + timedelta(minutes=1)
    rows = _status_train("a", dep, call_cancelled=True)
    _status_reset(rows)
    first = _status(begin="07:00", end="09:00").json()
    notification_pull._now = lambda: dep + timedelta(seconds=30)
    second = _status(begin="07:00", end="09:00").json()
    assert (first["trains"][0]["state"], first["trains"][0]["cancelled"]) == ("predicted", True)
    assert second["trains"][0]["state"] == "confirmed"
    assert first["now"] != second["now"]


def test_journey_status_spends_the_same_per_pair_bucket_as_journey_alerts():
    _status_reset(_status_train("a", STATUS_NOW + timedelta(minutes=6)))
    for i in range(notification_pull.PER_PAIR_PER_MIN):
        r = _status() if i % 2 else _get(wait=0)
        assert r.status_code == 200, (i, r.text)
    assert _status().status_code == 429
    assert _get(wait=0).status_code == 429


def test_journey_status_refuses_a_malformed_definition_with_the_pull_reads_422():
    _status_reset([])
    assert _status(from_stop="Oslo S").status_code == 422
    assert _status(begin="07:00").status_code == 422
    assert _status(begin="09:00", end="07:00").status_code == 422


def test_journey_status_logs_one_record_with_nothing_identifying():
    _status_reset(_status_train("a", STATUS_NOW + timedelta(minutes=6)))
    with _captured() as records:
        _status()
    status = [r for r in records if getattr(r, "fields", {}).get("event") == "notification_status"]
    assert len(status) == 1
    assert set(status[0].fields) == {"event", "result", "n_trains", "ms", "pair_digest", "client_name"}
    assert status[0].fields["n_trains"] == 1
    assert status[0].fields["pair_digest"] == alternatives.pair_digest(NSR_A, NSR_B)
    text = status[0].getMessage() + json.dumps(status[0].fields)
    assert "NSR:" not in text and "07:" not in text


def test_journey_status_answers_503_and_one_record_when_grading_raises(monkeypatch):
    import journey_status

    _status_reset(_status_train("a", STATUS_NOW + timedelta(minutes=6)))

    def boom(definition, now):
        raise KeyError(NSR_A)  # an exception whose TEXT names a ref

    monkeypatch.setattr(journey_status, "status_for", boom)
    with _captured() as records:
        r = _status()
    assert r.status_code == 503 and r.json()["detail"] == "status_failed"
    assert r.headers["Retry-After"] == "60"
    status = [x for x in records if getattr(x, "fields", {}).get("event") == "notification_status"]
    assert [x.fields["result"] for x in status] == ["status_failed"]
    shipped = [x for x in records if x.levelno >= logging.INFO]
    assert all(NSR_A not in x.getMessage() for x in shipped), "the exception's text never reaches INFO+"

def test_journey_status_response_models_forbid_unknown_fields():
    import journey_status

    for model in (notification_pull.JourneyStatusResponse, journey_status.TrainStatus,
                  journey_status.AsOf, journey_status.WindowSpan):
        assert model.model_config.get("extra") == "forbid", model.__name__


def _run():
    """Mirrors `test_varsel_api`'s runner: a test naming `monkeypatch` gets the shim, undone after."""
    import inspect

    logging.getLogger("notification_pull").setLevel(logging.CRITICAL)
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        if "monkeypatch" in inspect.signature(fn).parameters:
            mp = _MonkeyPatch()
            try:
                fn(mp)
            finally:
                mp.undo()
        else:
            fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} journey-alert pull read checks passed.")


if __name__ == "__main__":
    _run()
