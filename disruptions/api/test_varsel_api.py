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

import varsel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_varsel import _OSLO, FakeCall  # reuse the synthetic call shape

DEVICE = "dev-abc12345"
SUBSCRIPTION = {"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k", "auth": "a"}}

# Oslo stops A (order 1) -> B (order 5) on a train flagged by situation S1. The endpoints match
# against the real clock (departed trains are filtered out), so the departure must lie in the
# future: two hours from now, matched by all-day/all-days journey windows below.
DEP = datetime.now(tz=_OSLO) + timedelta(hours=2)
SNAPSHOT = {
    "S1": [
        FakeCall("t1", "A", 1, DEP, p_cancel=0.82, p_impact_dep=0.9),
        FakeCall("t1", "B", 5, None),
    ]
}
# Same shape, but the train left hours ago — must never produce an alert.
SNAPSHOT_DEPARTED = {
    "S1": [
        FakeCall("t1", "A", 1, DEP - timedelta(hours=4), p_cancel=0.82, p_impact_dep=0.9),
        FakeCall("t1", "B", 5, None),
    ]
}
STOPS = [
    {"stop_place": "A", "stop_name": "Oslo S", "mode": "Tog/Buss", "n_calls": 900},
    {"stop_place": "B", "stop_name": "Lillehammer", "mode": "Tog", "n_calls": 120},
]

varsel.configure(snapshot_provider=lambda: SNAPSHOT, stops_provider=lambda: STOPS)

app = FastAPI()
app.include_router(varsel.router)
client = TestClient(app)


def _reset_state():
    with varsel._state_lock:
        varsel._devices.clear()
        varsel._journeys.clear()
        varsel._notified.clear()


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


def test_departed_train_produces_no_alerts(monkeypatch):
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    # The live listing reads the injected snapshot provider; point it at the departed train.
    monkeypatch.setattr(varsel, "_snapshot_provider", lambda: SNAPSHOT_DEPARTED)
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
