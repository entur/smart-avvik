"""End-to-end checks for the /varsel-api surface, hermetic (no BigQuery, no real push).

Sets a throwaway VAPID keypair + a temp state file BEFORE importing varsel, injects a synthetic
prediction snapshot, stubs the push sender, and drives the router through FastAPI's TestClient.
Covers config, stops, journey CRUD + validation, live-match annotation, notify + dedup, the test
push, and state persistence.

Run: uv run --with pywebpush --with httpx python test_varsel_api.py  (or under pytest)
"""

import base64
import os
import tempfile

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02

# --- Configure the environment BEFORE importing varsel (it reads env at import). ---
_v = Vapid02()
_v.generate_keys()
_PRIV = base64.urlsafe_b64encode(_v.private_key.private_numbers().private_value.to_bytes(32, "big")).rstrip(b"=").decode()
_PUB = base64.urlsafe_b64encode(
    _v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
).rstrip(b"=").decode()
_STATE_FD, _STATE_PATH = tempfile.mkstemp(prefix="varsel_state_", suffix=".json")
os.close(_STATE_FD)
os.environ.update(
    VAPID_PRIVATE_KEY=_PRIV,
    VAPID_PUBLIC_KEY=_PUB,
    VAPID_SUBJECT="mailto:test@entur.org",
    VARSEL_STATE_PATH=_STATE_PATH,
)

import json
from datetime import datetime, timedelta

import alternatives
import varsel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_varsel import (  # reuse the synthetic call shape + the stubbed main
    _OSLO,
    FakeCall,
    main,
)

DEVICE = "dev-abc12345"
SUBSCRIPTION = {"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k", "auth": "a"}}

# Oslo stops A (order 1) -> B (order 5) on a train flagged by situation S1. The endpoints match
# against the real clock (departed trains are filtered out), so the departure must lie in the
# future: two hours from now, matched by all-day/all-days journey windows below.
DEP = datetime.now(tz=_OSLO) + timedelta(hours=2)
SNAPSHOT = {
    "S1": [
        FakeCall("t1", "A", 1, DEP, p_cancel=0.82, p_impact_dep=0.9),
        FakeCall("t1", "B", 5, None, aimed_arr=DEP + timedelta(minutes=40)),
    ]
}
# Same shape, but the train left hours ago — must never produce an alert.
SNAPSHOT_DEPARTED = {
    "S1": [
        FakeCall("t1", "A", 1, DEP - timedelta(hours=4), p_cancel=0.82, p_impact_dep=0.9),
        FakeCall("t1", "B", 5, None, aimed_arr=DEP - timedelta(hours=4) + timedelta(minutes=40)),
    ]
}
STOPS = [
    {"stop_place": "A", "stop_name": "Oslo S", "mode": "Tog/Buss", "n_calls": 900},
    {"stop_place": "B", "stop_name": "Lillehammer", "mode": "Tog", "n_calls": 120},
]

# Point varsel at main's real /impact provider over a snapshot the tests can swap. Driving the real
# provider (rather than faking impact responses) means these e2e checks exercise the same matcher
# and severity order the deployed app does.
_active_snapshot = {"value": SNAPSHOT}


def _use_snapshot(snapshot):
    _active_snapshot["value"] = snapshot


def _impact_provider(*, from_stop, to_stop, window_start, window_end, catchable_after=None,
                     with_alternative=False):
    main._call_pred_by_situation = _active_snapshot["value"]
    return main._impact_for_trip(
        from_stop=from_stop,
        to_stop=to_stop,
        window_start=window_start,
        window_end=window_end,
        select="worst",
        catchable_after=catchable_after,
        # Passed THROUGH, not hard-coded: hard-coding False made the suite blind to a Journey Planner
        # call reappearing on the request path. alternatives.find_faster is stubbed in the tests that
        # care, so nothing here reaches the network either way.
        with_alternative=with_alternative,
    )


varsel.configure(impact_provider=_impact_provider, stops_provider=lambda: STOPS)

# Suite-wide: this provider calls the REAL _impact_for_trip, which calls alternatives.find_faster
# whenever with_alternative=True. Individual tests stubbing find_faster is not enough — any test that
# exercises the poller path without stubbing it would issue a live HTTPS request to api.entur.io from
# CI and eat the 3 s timeout, failing open so nobody would notice. Make it structurally impossible.
main.alternatives.find_faster = lambda *a, **k: None

app = FastAPI()
app.include_router(varsel.router)
client = TestClient(app)


def _reset_state():
    with varsel._state_lock:
        varsel._devices.clear()
        varsel._journeys.clear()
        varsel._notified.clear()
        varsel._messages.clear()
        varsel._alternatives.clear()
        varsel._message_attempts.clear()
    _use_snapshot(SNAPSHOT)


def test_config_reports_push_enabled():
    r = client.get("/varsel-api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["push_enabled"] is True
    assert body["vapid_public_key"] == _PUB


def test_stops():
    r = client.get("/varsel-api/stops")
    assert r.status_code == 200
    body = r.json()
    assert {s["stop_place"] for s in body} == {"A", "B"}
    assert {s["mode"] for s in body} == {"Tog/Buss", "Tog"}
    assert all(s["n_calls"] > 0 for s in body)


def test_journey_crud_and_live_match():
    _reset_state()
    payload = {
        "device_id": DEVICE,
        "from_stop": "A",
        "from_name": "Oslo S",
        "to_stop": "B",
        "to_name": "Lillehammer",
        "begin": "00:00",
        "end": "23:59",
        "days": [0, 1, 2, 3, 4, 5, 6],
    }
    r = client.post("/varsel-api/journeys", json=payload)
    assert r.status_code == 200, r.text
    created = r.json()
    jid = created["journey_id"]
    # The synthetic in-two-hours situation should show up as a live match on this journey.
    assert len(created["alerts"]) == 1
    assert created["alerts"][0]["situation_number"] == "S1"
    assert created["alerts"][0]["alertworthy"] is True

    # Validation: identical stops rejected.
    bad = client.post("/varsel-api/journeys", json={**payload, "to_stop": "A"})
    assert bad.status_code == 422

    # Validation: begin >= end rejected.
    bad2 = client.post("/varsel-api/journeys", json={**payload, "begin": "10:00", "end": "09:00"})
    assert bad2.status_code == 422

    listing = client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()
    assert [j["journey_id"] for j in listing["journeys"]] == [jid]
    assert listing["journeys"][0]["alerts"][0]["cancel_probability"] == 0.82

    # Delete requires the owning device.
    assert client.delete(f"/varsel-api/journeys/{jid}?device_id=someone-else").status_code == 404
    assert client.delete(f"/varsel-api/journeys/{jid}?device_id={DEVICE}").status_code == 200
    assert client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()["journeys"] == []


def test_notify_pushes_once_then_dedups(monkeypatch):
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    client.post(
        "/varsel-api/journeys",
        json={
            "device_id": DEVICE,
            "from_stop": "A",
            "from_name": "Oslo S",
            "to_stop": "B",
            "to_name": "Lillehammer",
            "begin": "00:00",
            "end": "23:59",
            "days": [0, 1, 2, 3, 4, 5, 6],
        },
    )
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    assert sent[0]["title"] == "Avvik: Oslo S → Lillehammer"
    assert sent[0]["url"] == "/varsel/"
    # The body leads with the alerting score (p_impact_dep 0.9), and this match is cancellation-
    # driven (p_cancel 0.82), so it says so rather than quoting a delay.
    assert "90 % sjanse for innstilling eller ≥ 10 min forsinkelse" in sent[0]["body"]
    assert "mest sannsynlig innstilt" in sent[0]["body"]
    # Second pass: already notified -> no new push.
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    # Situation ends (empty snapshot) -> notified pruned; it re-alerts if it returns.
    varsel.check_and_notify({})
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 2


def _register_matching_journey():
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    client.post(
        "/varsel-api/journeys",
        json={
            "device_id": DEVICE, "from_stop": "A", "from_name": "Oslo S",
            "to_stop": "B", "to_name": "Lillehammer",
            "begin": "00:00", "end": "23:59", "days": [0, 1, 2, 3, 4, 5, 6],
        },
    )


JOURNEY_BODY = {
    "from_stop": "A", "from_name": "Oslo S", "to_stop": "B", "to_name": "Lillehammer",
    "begin": "00:00", "end": "23:59", "days": [0, 1, 2, 3, 4, 5, 6],
}


def _register(device_id, **overrides):
    client.post(
        "/varsel-api/devices", json={"device_id": device_id, "subscription": SUBSCRIPTION}
    )
    r = client.post(
        "/varsel-api/journeys", json={"device_id": device_id, **JOURNEY_BODY, **overrides}
    )
    assert r.status_code == 200, r.text
    return r.json()["journey_id"]


def _count_provider_calls(monkeypatch):
    """Wrap the impact provider so a test can count how many times it is consulted."""
    calls = []
    real = varsel._impact_provider

    def counting(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setattr(varsel, "_impact_provider", counting)
    return calls


def test_same_journey_on_two_devices_shares_one_impact_call_and_one_message(monkeypatch):
    """R6: two devices watching the same trip get identical text, computed once."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    written = []

    def fake_write(journey, alert, *, journey_key):
        written.append(journey_key)
        return "Signalfeil ved Asker. Ta R10 kl 08:14 i stedet."

    monkeypatch.setattr(varsel, "_write_message", fake_write)
    provider_calls = _count_provider_calls(monkeypatch)

    _register("dev-first-0001")
    _register("dev-second-002")
    provider_calls.clear()  # registration itself renders each journey; count only the notify pass

    varsel.check_and_notify(SNAPSHOT)

    assert len(sent) == 2, "both devices must be notified"
    assert sent[0]["body"] == sent[1]["body"] == "Signalfeil ved Asker. Ta R10 kl 08:14 i stedet."
    assert len(written) == 1, "one written message per journey definition, not per device"
    # No window is scored twice. (A journey can legitimately span more than one window -- today's
    # and tomorrow's -- so the claim is "no duplicated work", not "exactly one call".)
    distinct = {tuple(sorted(kw.items(), key=str)) for kw in provider_calls}
    assert len(provider_calls) == len(distinct), (
        f"{len(provider_calls)} impact calls for {len(distinct)} distinct windows -- "
        "the second device re-drove work the first had already done"
    )


def test_different_journeys_are_computed_separately(monkeypatch):
    """The compute cache keys on the journey definition — different trips must not share it."""
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: None)
    provider_calls = _count_provider_calls(monkeypatch)

    _register("dev-first-0001")  # 00:00-23:59
    _register("dev-second-002", begin="06:00", end="10:00")
    provider_calls.clear()

    varsel.check_and_notify(SNAPSHOT)
    starts = {kw["window_start"].astimezone(_OSLO).strftime("%H:%M") for kw in provider_calls}
    assert "00:00" in starts and "06:00" in starts, (
        f"both journey definitions must be scored; saw window starts {sorted(starts)}"
    )


def test_dedup_is_per_device_not_per_journey_definition(monkeypatch):
    """The KTD7 trap: sharing the dedup key would let the first device silently suppress the rest."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: None)

    first = _register("dev-first-0001")
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    # A second device registers the identical journey after the first was already notified.
    _register("dev-second-002")
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 2, "the second device must still get its own first push"
    # ...and neither is pushed twice.
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 2
    assert any(k.startswith(first) for k in varsel._notified)


def test_message_failure_falls_back_to_the_deterministic_body(monkeypatch):
    """A failing message writer costs wording, never the notification."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: None)

    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    assert "90 % sjanse for innstilling eller ≥ 10 min forsinkelse" in sent[0]["body"]


def test_message_writer_exception_is_contained(monkeypatch):
    """_write_message swallows everything, but assert the notify path survives a raiser too."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")

    def boom(journey, alert, *, journey_key):
        raise RuntimeError("Vertex exploded")

    monkeypatch.setattr(varsel, "_write_message", boom)
    _register(DEVICE)
    try:
        varsel.check_and_notify(SNAPSHOT)
    except RuntimeError:
        raise AssertionError("a message-writer failure must not break the notify pass") from None
    assert len(sent) == 1, "the push still goes out with the deterministic wording"


def test_llm_call_cap_is_respected(monkeypatch):
    """With the cap at 1 and two alerting journeys, one gets written text and both are pushed."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(varsel, "_MAX_LLM_CALLS_PER_CYCLE", 1)
    written = []
    monkeypatch.setattr(
        varsel,
        "_write_message",
        lambda journey, alert, *, journey_key: written.append(journey_key) or "Skrevet melding.",
    )

    _register("dev-first-0001")
    _register("dev-second-002", begin="06:00", end="23:00")
    varsel.check_and_notify(SNAPSHOT)

    assert len(written) == 1, "the cap must bound written messages per cycle"
    assert len(sent) == 2, "both journeys are still pushed"
    bodies = sorted(p["body"] for p in sent)
    assert bodies[0] != bodies[1], "one written, one deterministic"


def test_a_message_that_cannot_be_written_is_not_retried_forever(monkeypatch):
    """A write that fails for a payload-dependent reason fails the same way every tick. Without a
    give-up point the poller re-issues it every 60 s for the situation's whole life (~720 calls),
    each one burning a slot from the per-cycle cap that a journey which WOULD have succeeded cannot
    then use — and none of it can change anything, because the push already went out with the
    deterministic wording on the first tick.
    """
    _reset_state()
    sent = []
    monkeypatch.setattr(
        varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent"
    )
    attempts = []
    monkeypatch.setattr(
        varsel, "_write_message", lambda *a, **k: attempts.append(1) or None
    )
    _register(DEVICE)

    for _ in range(10):  # ten poller cycles on the same live situation
        varsel.check_and_notify(SNAPSHOT)

    assert len(attempts) == varsel._MAX_MESSAGE_ATTEMPTS, (
        f"expected the write to be abandoned after {varsel._MAX_MESSAGE_ATTEMPTS} attempts, "
        f"got {len(attempts)}"
    )
    assert len(sent) == 1, "the push still went out once, with the deterministic wording"


def test_a_written_message_clears_the_failure_count(monkeypatch):
    """The give-up counter must not leak across a recovery: a transient failure followed by a
    success has to leave the journey able to be re-written for the next situation.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        return None if len(calls) == 1 else "Skrevet melding."

    monkeypatch.setattr(varsel, "_write_message", flaky)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)  # fails
    varsel.check_and_notify(SNAPSHOT)  # succeeds
    assert not varsel._message_attempts, "a success must clear the count, not leave it at 1"

    varsel.check_and_notify(SNAPSHOT)  # cached now -- no third call
    assert len(calls) == 2


def test_journey_listing_carries_the_pushed_message(monkeypatch):
    """The in-app list says exactly what the push said — read from cache, not re-written."""
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(
        varsel, "_write_message", lambda *a, **k: "Toget er mest sannsynlig innstilt."
    )
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    r = client.get("/varsel-api/journeys", params={"device_id": DEVICE})
    assert r.status_code == 200
    alerts = r.json()["journeys"][0]["alerts"]
    assert alerts and alerts[0]["message"] == "Toget er mest sannsynlig innstilt."


def test_journey_listing_omits_the_internal_impact_reference():
    """Alert.impact is a working reference for the message writer, not part of the contract."""
    _reset_state()
    _register(DEVICE)
    r = client.get("/varsel-api/journeys", params={"device_id": DEVICE})
    alerts = r.json()["journeys"][0]["alerts"]
    assert alerts, "the synthetic snapshot should produce a live match"
    assert "impact" not in alerts[0]


def test_journey_handlers_are_not_coroutines():
    """They reach the impact provider, which can make a blocking Journey Planner call, so FastAPI
    must run them in the anyio threadpool. As `async def` they would block the single-worker event
    loop and freeze /health (review 2026-07-29; rule documented at reisevalg.py:28)."""
    import inspect

    assert not inspect.iscoroutinefunction(varsel.list_journeys)
    assert not inspect.iscoroutinefunction(varsel.create_journey)


def test_request_path_does_not_call_the_journey_planner(monkeypatch):
    """The poller opts into the alternative lookup; the request path must not, or a device with 20
    journeys x 2 windows would fan out ~40 blocking external calls on one GET."""
    _reset_state()
    import alternatives

    calls = []
    monkeypatch.setattr(alternatives, "find_faster", lambda *a, **k: calls.append(1))
    _register(DEVICE)
    calls.clear()
    r = client.get("/varsel-api/journeys", params={"device_id": DEVICE})
    assert r.status_code == 200
    assert calls == [], "GET /varsel-api/journeys must not consult the Journey Planner"


def test_listing_surfaces_the_alternative_the_poller_found(monkeypatch):
    """The field is populated from the poller's cache. It used to be structurally null here, because
    the request path skips the lookup and nothing carried the result across (review 2026-07-29)."""
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: "Ta R10 i stedet.")
    fake_alt = alternatives.AlternativeJourney(
        departure_time=DEP, arrival_time=DEP + timedelta(minutes=30),
        duration_min=30, minutes_saved=12, legs=[],
    )
    real_match = varsel.match_journey

    def with_alt(journey, now=None, *, with_alternative=False):
        alerts = real_match(journey, now, with_alternative=with_alternative)
        if with_alternative:  # emulate the poller's JP hit
            for a in alerts:
                a.alternative_journey = fake_alt
        return alerts

    monkeypatch.setattr(varsel, "match_journey", with_alt)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    r = client.get("/varsel-api/journeys", params={"device_id": DEVICE})
    alerts = r.json()["journeys"][0]["alerts"]
    assert alerts and alerts[0]["alternative_journey"] is not None
    assert alerts[0]["alternative_journey"]["minutes_saved"] == 12


def test_departed_train_produces_no_alerts(monkeypatch):
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    _use_snapshot(SNAPSHOT_DEPARTED)
    _register_matching_journey()
    # The situation is still open, but its train left hours ago: no live match, no push.
    varsel.check_and_notify(SNAPSHOT_DEPARTED)
    assert sent == []
    listing = client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()
    assert listing["journeys"][0]["alerts"] == []


def test_client_log():
    r = client.post(
        "/varsel-api/client-log",
        json={
            "context": "fetch",
            "message": "GET /varsel-api/journeys: TypeError: Failed to fetch",
            "device_id": DEVICE,
            "url": "/varsel/",
        },
    )
    assert r.status_code == 200 and r.json()["status"] == "ok"
    # Validation: an empty message is rejected.
    assert client.post("/varsel-api/client-log", json={"context": "fetch", "message": ""}).status_code == 422


def test_transient_410_keeps_fresh_device(monkeypatch):
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "gone")
    _register_matching_journey()
    # A 404/410 on a freshly-created subscription is treated as transient -> device kept.
    varsel.check_and_notify(SNAPSHOT)
    assert DEVICE in varsel._devices
    # Age it past the grace window -> a 410 now drops it.
    with varsel._state_lock:
        varsel._devices[DEVICE]["created_ts"] = "2000-01-01T00:00:00+00:00"
    varsel.check_and_notify(SNAPSHOT)
    assert DEVICE not in varsel._devices


def test_test_push_fresh_410_is_425_not_dropped(monkeypatch):
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "gone")
    r = client.post("/varsel-api/test", json={"device_id": DEVICE})
    assert r.status_code == 425  # "try again shortly", subscription still propagating
    assert DEVICE in varsel._devices  # not dropped


def test_test_push(monkeypatch):
    _reset_state()
    # No device yet -> 404.
    assert client.post("/varsel-api/test", json={"device_id": DEVICE}).status_code == 404
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    r = client.post("/varsel-api/test", json={"device_id": DEVICE})
    assert r.status_code == 200 and r.json()["status"] == "sent"


def test_state_persisted_to_file():
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    with open(_STATE_PATH) as fh:
        data = json.load(fh)
    assert DEVICE in data["devices"]


class _MonkeyPatch:
    """Minimal monkeypatch shim so the tests run standalone (without pytest)."""

    def __init__(self):
        self._undo = []

    def setattr(self, obj, name, value):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)


if __name__ == "__main__":
    import inspect

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        if "monkeypatch" in inspect.signature(t).parameters:
            mp = _MonkeyPatch()
            try:
                t(mp)
            finally:
                mp.undo()
        else:
            t()
        print(f"  ok  {t.__name__}")
    os.unlink(_STATE_PATH)
    print(f"\n{len(tests)} /varsel-api e2e checks passed.")
