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
    # Pin the environment label off (see deployment.environment_label): notification titles carry it on
    # non-prod, so a developer with GOOGLE_CLOUD_PROJECT=ent-kixavvik-dev exported -- routine in this
    # repo -- would otherwise fail the title assertions below. The tests that DO exercise labelling
    # set it themselves via _deployed_as.
    KIX_ENVIRONMENT="prd",
    # Pin the DELIVERY switch on, for the same class of reason (llm_message reads it at import).
    # Since 2026-08-07 it decides which of the two written wordings the push and the card carry, so
    # a developer with VARSEL_LLM_ENABLED=false exported -- prd's own setting, and exactly what
    # somebody reproducing prd behaviour locally would export -- would otherwise fail every
    # assertion that a written message reaches a traveller. The tests that exercise delivery-off
    # monkeypatch llm_message.delivery_enabled themselves, which also states the case they are about
    # instead of leaving it to the ambient environment.
    VARSEL_LLM_ENABLED="true",
    # And pin the GENERATION side on, which needs a project: `llm_message.generation_enabled()` is
    # False without one, and since 2026-08-07 `check_and_notify` checks it BEFORE reaching the
    # writer (so that a cycle asking no backend does not spend a retry attempt). Unpinned, every
    # test that stubs `_write_message` would silently never reach its stub and assert against a
    # deployment that cannot write at all. No client is ever constructed from this value here --
    # the backends are stubbed -- so a placeholder project is enough.
    GOOGLE_CLOUD_PROJECT=os.environ.get("GOOGLE_CLOUD_PROJECT", "ent-kixavvik-test"),
)

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import alternatives
import impact
import varsel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_varsel import (  # reuse the synthetic call shape + the stubbed main
    _OSLO,
    FakeCall,
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


# The engine reads its snapshot through an injected provider; the tests own it here so
# _use_snapshot swaps what the real matcher sees.
impact.configure(snapshot_provider=lambda: (_active_snapshot["value"], None))


def _impact_provider(*, from_stop, to_stop, window_start, window_end, catchable_after=None,
                     with_alternative=False, min_impact=0.0):
    return impact.for_trip(
        from_stop=from_stop,
        to_stop=to_stop,
        window_start=window_start,
        window_end=window_end,
        select="worst",
        catchable_after=catchable_after,
        # Passed THROUGH, not hard-coded: hard-coding False made the suite blind to a Journey Planner
        # call reappearing on the request path. alternatives.fetch_candidates is stubbed in the tests
        # that care, so nothing here reaches the network either way.
        with_alternative=with_alternative,
        min_impact=min_impact,
    )


varsel.configure(impact_provider=_impact_provider, stops_provider=lambda: STOPS)

# Suite-wide: this provider calls the REAL impact.for_trip, which calls
# alternatives.fetch_candidates whenever with_alternative=True. Individual tests stubbing it are not
# enough — any test that exercises the poller path without stubbing it would issue a live HTTPS
# request to api.entur.io from CI and eat the 3 s timeout, failing open so nobody would notice. Make
# it structurally impossible.
#
# This stub names a function, so re-pointing the call chain under it silently disarms it — which is
# exactly what happened when the single-call lookup became fetch -> score -> select. The second line
# of defence does not depend on anyone remembering: alternatives._post_json refuses to open a socket
# from a test process at all (test_a_forgotten_stub_cannot_reach_the_network pins it).
impact.alternatives.fetch_candidates = lambda *a, **k: []

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
        # Both retry counters. Forgetting this one leaks a partially-spent transient budget into the
        # next test, which then gives up early for a reason that has nothing to do with what it is
        # checking -- it reads as the give-up bug rather than as pollution.
        varsel._message_errors.clear()
        varsel._message_meta.clear()
    # llm_message's backend latch is PROCESS-global and survives a monkeypatch undo, so a test that
    # drives both backends into `_latched_off` (the 404/configuration path) silently disables
    # generation for every test that runs after it. That was invisible until 2026-08-07, when
    # `check_and_notify` began consulting `generation_enabled()` before reaching the writer -- at
    # which point the leak turned into "the writer is never called" in unrelated tests, which reads
    # exactly like the give-up bug those tests exist to pin. Clear it here, like test_llm_message's
    # own `_reset` does.
    import llm_message

    with llm_message._lock:
        llm_message._latched_off.clear()
        llm_message._active = None
        llm_message._active_at = None
    varsel.set_stop_aliases({})
    _use_snapshot(SNAPSHOT)


def test_config_reports_push_enabled():
    r = client.get("/varsel-api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["push_enabled"] is True
    assert body["vapid_public_key"] == _PUB


@contextmanager
def _deployed_as(project: str | None, override: str | None = None):
    """Run the body as if the app were deployed in `project` (ambient env, if any, restored after)."""
    before = {k: os.environ.get(k) for k in ("GOOGLE_CLOUD_PROJECT", "KIX_ENVIRONMENT")}
    for key, value in (("GOOGLE_CLOUD_PROJECT", project), ("KIX_ENVIRONMENT", override)):
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _environment():
    return client.get("/varsel-api/config").json()["environment"]


def test_config_names_the_non_prod_environment():
    """The PWA shows this behind its title: dev and prd sit side by side on one phone and are
    otherwise identical, down to the home-screen name (one image, one manifest, every environment)."""
    with _deployed_as("ent-kixavvik-dev"):
        assert _environment() == "dev"
    with _deployed_as("ent-kixavvik-tst"):
        assert _environment() == "tst"
    with _deployed_as("ent-kixavvik-prd", override="local"):
        assert _environment() == "local"  # KIX_ENVIRONMENT wins, for local runs


def test_config_leaves_production_unlabelled():
    """prd is the public, traveller-facing host, so it carries no environment token -- and neither
    does a deployment whose project we cannot read: guessing risks a stray label in prd."""
    with _deployed_as("ent-kixavvik-prd"):
        assert _environment() is None
    with _deployed_as(None):
        assert _environment() is None


def test_stops():
    r = client.get("/varsel-api/stops")
    assert r.status_code == 200
    body = r.json()
    assert {s["stop_place"] for s in body} == {"A", "B"}
    assert {s["mode"] for s in body} == {"Tog/Buss", "Tog"}
    assert all(s["n_calls"] > 0 for s in body)
    # A provider row without the grouping fields (an un-refreshed cache, or a deploy whose stop
    # registry read failed) still yields a usable place id: itself.
    assert all(s["place_id"] == s["stop_place"] and s["region"] is None for s in body)


def test_stops_pass_through_place_and_region(monkeypatch):
    """The picker appends the county only where a name belongs to more than one place, so both
    fields have to survive the hop."""
    monkeypatch.setattr(
        varsel,
        "_stops_provider",
        lambda: [{"stop_place": "A", "stop_name": "Sentrum", "mode": "Buss", "n_calls": 9,
                  "place_id": "parent-1", "region": "Trøndelag"}],
    )
    body = client.get("/varsel-api/stops").json()
    assert body[0]["place_id"] == "parent-1"
    assert body[0]["region"] == "Trøndelag"


def test_registration_is_canonicalized_onto_the_primary_stop():
    """A ref the server has collapsed must not be stored: the trip does not call there, and the
    journey would report "no active disruptions" for its whole life. The client may hold a stale
    stop list, and anything can POST to this API."""
    _reset_state()
    varsel.set_stop_aliases({"A2": "A"})
    r = client.post(
        "/varsel-api/journeys",
        json={"device_id": DEVICE, "from_stop": "A2", "from_name": "Oslo S", "to_stop": "B",
              "to_name": "Lillehammer", "begin": "00:00", "end": "23:59",
              "days": [0, 1, 2, 3, 4, 5, 6]},
    )
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["from_stop"] == "A", "stored on the primary ref, not the one that was posted"
    # And therefore it matches: this is the whole point of rewriting it.
    assert [a["situation_number"] for a in created["alerts"]] == ["S1"]


def test_existing_journeys_are_rewritten_when_the_registry_lands():
    """Registrations made before the grouping existed are on whichever twin the traveller picked.
    They are healed on the next stop refresh rather than left as permanent silence."""
    _reset_state()
    r = client.post(
        "/varsel-api/journeys",
        json={"device_id": DEVICE, "from_stop": "A2", "from_name": "Nyvoll", "to_stop": "B",
              "to_name": "Lillehammer", "begin": "00:00", "end": "23:59",
              "days": [0, 1, 2, 3, 4, 5, 6]},
    )
    jid = r.json()["journey_id"]
    assert r.json()["alerts"] == [], "the wrong-twin registration matches nothing (the bug)"

    varsel.set_stop_aliases({"A2": "A"})  # as refresh_varsel_stops does after reading the registry

    with varsel._state_lock:
        assert varsel._journeys[jid]["from_stop"] == "A"
    listed = client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()["journeys"]
    assert [a["situation_number"] for a in listed[0]["alerts"]] == ["S1"]


def _use_connectivity(verdict, seen=None):
    """Install a connectivity provider returning `verdict`, recording the refs it was asked about."""

    def provider(from_refs, to_refs):
        if seen is not None:
            seen.append((tuple(from_refs), tuple(to_refs)))
        return verdict

    varsel.configure(
        impact_provider=_impact_provider, stops_provider=lambda: STOPS,
        connectivity_provider=provider,
    )


def _no_connectivity():
    varsel.configure(impact_provider=_impact_provider, stops_provider=lambda: STOPS)


_JOURNEY = {"device_id": DEVICE, "from_stop": "A", "from_name": "Oslo S", "to_stop": "B",
            "to_name": "Lillehammer", "begin": "00:00", "end": "23:59",
            "days": [0, 1, 2, 3, 4, 5, 6]}


def test_a_pair_served_only_the_other_way_says_so_by_name():
    """Near-proof that from and to were swapped: the two stops ARE connected, just not in the
    direction asked for — so the caveat names both stops and asks the question outright, instead of
    the generic wording. Still not a refusal: a return leg can run on different days from the outward
    one, and an unregisterable journey would be worse than a registered one with a caveat."""
    _reset_state()
    _use_snapshot({})
    _use_connectivity((False, True))
    try:
        r = client.post("/varsel-api/journeys", json=_JOURNEY)
    finally:
        _no_connectivity()
        _use_snapshot(SNAPSHOT)
    assert r.status_code == 200, r.text
    warning = r.json()["warning"] or ""
    assert "motsatt vei" in warning
    assert "Oslo S" in warning and "Lillehammer" in warning
    with varsel._state_lock:
        assert len(varsel._journeys) == 1, "warned, not refused"


def test_a_pair_served_neither_way_registers_with_a_warning():
    """NOT refused: a weekend-only or seasonal route is legitimately absent from today's timetable,
    and refusing it would make the app wrong about a journey the traveller knows exists. The warning
    is what the old silence lacked."""
    _reset_state()
    _use_snapshot({})  # nothing matching, so the card would otherwise say "ingen aktive avvik"
    _use_connectivity((False, False))
    try:
        r = client.post("/varsel-api/journeys", json=_JOURNEY)
        assert r.status_code == 200, r.text
        assert "ingen direkteavganger" in (r.json()["warning"] or "")
        listed = client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()["journeys"]
    finally:
        _no_connectivity()
        _use_snapshot(SNAPSHOT)
    assert "ingen direkteavganger" in (listed[0]["warning"] or ""), "it persists on the card"


def test_a_served_pair_carries_no_warning():
    _reset_state()
    _use_connectivity((True, False))
    try:
        r = client.post("/varsel-api/journeys", json=_JOURNEY)
    finally:
        _no_connectivity()
    assert r.status_code == 200, r.text
    assert r.json()["warning"] is None
    with varsel._state_lock:
        assert "no_direct_service" not in next(iter(varsel._journeys.values()))


def test_the_check_asks_about_every_ref_of_both_places():
    """Same reason match_journey does: each feed records its calls on its own ref, so checking only
    the canonical one would report "no service" for a trip that plainly exists — the validation would
    then invent the very failure it exists to catch."""
    _reset_state()
    seen: list = []
    varsel.set_stop_aliases({"A2": "A"})
    _use_connectivity((True, False), seen)
    try:
        client.post("/varsel-api/journeys", json=_JOURNEY)
    finally:
        _no_connectivity()
        varsel.set_stop_aliases({})
    assert seen == [(("A", "A2"), ("B",))]


def test_an_unavailable_check_never_blocks_a_registration():
    """None means "could not establish", not "no service": a BigQuery outage, a timeout or the rate
    limit must not stop a traveller registering, nor put a warning on a healthy journey."""
    _reset_state()
    _use_connectivity(None)
    try:
        r = client.post("/varsel-api/journeys", json=_JOURNEY)
    finally:
        _no_connectivity()
    assert r.status_code == 200, r.text
    assert r.json()["warning"] is None
    with varsel._state_lock:
        rec = next(iter(varsel._journeys.values()))
    assert "no_direct_service" not in rec and "service_checked_ts" not in rec


def test_a_raising_check_never_blocks_a_registration():
    _reset_state()

    def boom(from_refs, to_refs):
        raise RuntimeError("BigQuery is down")

    varsel.configure(impact_provider=_impact_provider, stops_provider=lambda: STOPS,
                     connectivity_provider=boom)
    try:
        r = client.post("/varsel-api/journeys", json=_JOURNEY)
    finally:
        _no_connectivity()
    assert r.status_code == 200, r.text
    assert r.json()["warning"] is None


def test_the_warning_yields_to_an_actual_alert():
    """A card cannot say "we found no departures between these stops" directly above a disruption on
    one of them. Whatever the timetable said, a match is the stronger evidence."""
    _reset_state()
    _use_connectivity((False, False))
    try:
        r = client.post("/varsel-api/journeys", json=_JOURNEY)
    finally:
        _no_connectivity()
    assert len(r.json()["alerts"]) == 1, "the synthetic situation still matches"
    assert r.json()["warning"] is None


def test_service_that_appears_later_clears_the_warning():
    """The weekend route registered on a Friday. Re-checked on the poller's cycle, not left to carry a
    warning that has stopped being true."""
    _reset_state()
    _use_snapshot({})
    _use_connectivity((False, False))
    try:
        client.post("/varsel-api/journeys", json=_JOURNEY)
        with varsel._state_lock:
            jid = next(iter(varsel._journeys))
            assert varsel._journeys[jid]["no_direct_service"] is True
            # Backdate the stamp: the re-check is due once per _SERVICE_RECHECK, not every cycle.
            varsel._journeys[jid]["service_checked_ts"] = (
                datetime.now(UTC) - varsel._SERVICE_RECHECK - timedelta(minutes=1)
            ).isoformat()
        _use_connectivity((True, False))
        varsel.recheck_service()
        with varsel._state_lock:
            assert "no_direct_service" not in varsel._journeys[jid]
        listed = client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()["journeys"]
    finally:
        _no_connectivity()
        _use_snapshot(SNAPSHOT)
    assert listed[0]["warning"] is None


def test_a_fresh_verdict_is_not_re_asked_every_cycle():
    """The poller runs this on every refresh; without the interval it would issue a BigQuery query per
    flagged journey per cycle, forever."""
    _reset_state()
    seen: list = []
    _use_connectivity((False, False))
    try:
        client.post("/varsel-api/journeys", json=_JOURNEY)
        _use_connectivity((True, False), seen)
        varsel.recheck_service()
    finally:
        _no_connectivity()
    assert seen == [], "the verdict is minutes old; nothing to re-ask"
    with varsel._state_lock:
        assert next(iter(varsel._journeys.values()))["no_direct_service"] is True


def test_a_journey_whose_ends_are_one_place_is_refused():
    """Two refs for one physical place look like two stops in an older list; after canonicalization
    they are the same stop, which JourneyIn's from != to check ran too early to see."""
    _reset_state()
    varsel.set_stop_aliases({"B2": "A"})
    r = client.post(
        "/varsel-api/journeys",
        json={"device_id": DEVICE, "from_stop": "A", "from_name": "Oslo S", "to_stop": "B2",
              "to_name": "Oslo S", "begin": "00:00", "end": "23:59",
              "days": [0, 1, 2, 3, 4, 5, 6]},
    )
    assert r.status_code == 422, r.text
    with varsel._state_lock:
        assert varsel._journeys == {}


def test_a_stored_journey_is_not_collapsed_into_itself():
    """If both ends of an existing registration resolve to one place, rewriting it would store a
    record the API rejects. It is left alone and logged instead."""
    _reset_state()
    r = client.post(
        "/varsel-api/journeys",
        json={"device_id": DEVICE, "from_stop": "A", "from_name": "Oslo S", "to_stop": "B",
              "to_name": "Lillehammer", "begin": "00:00", "end": "23:59",
              "days": [0, 1, 2, 3, 4, 5, 6]},
    )
    jid = r.json()["journey_id"]
    varsel.set_stop_aliases({"B": "A"})
    with varsel._state_lock:
        assert (varsel._journeys[jid]["from_stop"], varsel._journeys[jid]["to_stop"]) == ("A", "B")


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
    assert sent[0]["title"] == "Oslo S → Lillehammer"
    assert sent[0]["url"] == "/varsel/"
    assert "82 % sjanse for innstilling" in sent[0]["body"]
    assert "min forsinket" not in sent[0]["body"]
    # Second pass: already notified -> no new push.
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    # Situation ends (empty snapshot) -> notified pruned; it re-alerts if it returns.
    varsel.check_and_notify({})
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 2


# ---- brand ------------------------------------------------------------------------------------
# ?brand=none renders /varsel unbranded. The PAGE reskins itself, but a NOTIFICATION is drawn by
# the OS from what the server sends, so the brand has to survive registration and come back out in
# the payload — otherwise an unbranded install shows the Entur mark in the tray and taps through to
# the Entur-styled page, which is the whole thing the variant exists to avoid.


def _register_journey(device: str) -> None:
    client.post(
        "/varsel-api/journeys",
        json={
            "device_id": device,
            "from_stop": "A",
            "from_name": "Oslo S",
            "to_stop": "B",
            "to_name": "Lillehammer",
            "begin": "00:00",
            "end": "23:59",
            "days": [0, 1, 2, 3, 4, 5, 6],
        },
    )


def test_an_unbranded_device_is_pushed_its_own_icon_and_url(monkeypatch):
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    client.post(
        "/varsel-api/devices",
        json={"device_id": DEVICE, "subscription": SUBSCRIPTION, "brand": "none"},
    )
    _register_journey(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    assert sent[0]["icon"] == "/varsel/icons/pwa-192-none.png"
    # Tapping through has to land back on the unbranded page, or the notification is neutral and
    # the app it opens is not.
    assert sent[0]["url"] == "/varsel/?brand=none"
    # The WORDING is brand-independent on purpose: it is what /historikk records, and text that
    # varied by variant would split every wording comparison along a second axis.
    assert sent[0]["title"] == "Oslo S → Lillehammer"
    assert "82 % sjanse for innstilling" in sent[0]["body"]


def test_a_device_that_never_named_a_brand_is_entur(monkeypatch):
    """The default is load-bearing, not cosmetic: every device registered before brands existed has
    a state record with no `brand` key at all, and prd's state blob survives the deploy that adds
    this. Those must keep getting the Entur icon rather than a missing one."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    # Simulate a record written before the field existed, which is what a restored blob looks like.
    with varsel._state_lock:
        varsel._devices[DEVICE].pop("brand", None)
    _register_journey(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    assert sent[0]["icon"] == "/varsel/icons/pwa-192.png"
    assert sent[0]["url"] == "/varsel/"


def test_a_brand_this_server_cannot_draw_falls_back_rather_than_404ing(monkeypatch):
    """A newer PWA build naming a brand this deployment has not shipped yet must still get a
    working notification: an unknown slug picked straight out of _BRAND_ASSETS would KeyError on
    the poller thread and take the whole cycle's alerting with it."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    res = client.post(
        "/varsel-api/devices",
        json={"device_id": DEVICE, "subscription": SUBSCRIPTION, "brand": "vy"},
    )
    assert res.status_code == 200
    _register_journey(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    assert sent[0]["icon"] == "/varsel/icons/pwa-192.png"


def test_reopening_under_another_brand_moves_the_devices_notifications(monkeypatch):
    """Registration is an upsert, and the brand rides along — a traveller who installed the Entur
    variant and later opens ?brand=none should not keep being notified in the old one."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    client.post(
        "/varsel-api/devices",
        json={"device_id": DEVICE, "subscription": SUBSCRIPTION, "brand": "entur"},
    )
    created = varsel._devices[DEVICE]["created_ts"]
    client.post(
        "/varsel-api/devices",
        json={"device_id": DEVICE, "subscription": SUBSCRIPTION, "brand": "none"},
    )
    assert varsel._devices[DEVICE]["brand"] == "none"
    assert varsel._devices[DEVICE]["created_ts"] == created, "an upsert must not reset the age"
    _register_journey(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert sent[0]["icon"] == "/varsel/icons/pwa-192-none.png"


def test_the_test_push_carries_the_brand_too(monkeypatch):
    """The test push is how a traveller confirms the install works, so it is the first notification
    they ever see — showing the Entur mark there would give the variant away immediately."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    client.post(
        "/varsel-api/devices",
        json={"device_id": DEVICE, "subscription": SUBSCRIPTION, "brand": "none"},
    )
    assert client.post("/varsel-api/test", json={"device_id": DEVICE}).status_code == 200
    assert sent[0]["icon"] == "/varsel/icons/pwa-192-none.png"
    assert sent[0]["url"] == "/varsel/?brand=none"


def test_the_brand_survives_a_state_reload():
    """State is persisted to a blob and restored after a redeploy; a brand that did not survive it
    would silently revert every unbranded install to Entur on the next deploy."""
    _reset_state()
    client.post(
        "/varsel-api/devices",
        json={"device_id": DEVICE, "subscription": SUBSCRIPTION, "brand": "none"},
    )
    blob = varsel._serialize_state()
    with varsel._state_lock:
        varsel._devices.clear()
    varsel._apply_state(blob)
    assert varsel._devices[DEVICE]["brand"] == "none"


def test_push_titles_carry_the_environment_on_non_prod(monkeypatch):
    """Both push kinds, alert and test: a dev push and a prd push arrive in the same tray under the
    same app name (one manifest, every environment), so the title is the only place that can say
    which deployment sent it. Prefixed, because phones truncate titles on the right."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    _register_matching_journey()
    with _deployed_as("ent-kixavvik-dev"):
        varsel.check_and_notify(SNAPSHOT)
        assert client.post("/varsel-api/test", json={"device_id": DEVICE}).status_code == 200
    assert [p["title"] for p in sent] == [
        "[dev] Oslo S → Lillehammer",
        "[dev] Varsler er aktivert",
    ]
    assert "[dev]" not in sent[0]["body"]  # the tag belongs in the title, not in the wording


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

    def fake_write(alert, *, journey_key, message_input, **_):
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
    assert "82 % sjanse for innstilling" in sent[0]["body"]


def test_message_writer_exception_is_contained(monkeypatch):
    """_write_message swallows everything, but assert the notify path survives a raiser too."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")

    def boom(alert, *, journey_key, message_input, **_):
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
        lambda alert, *, journey_key, message_input, **_: (
            written.append(journey_key) or "Skrevet melding."
        ),
    )

    _register("dev-first-0001")
    # A DIFFERENT journey definition (so it does not share the first one's message cache), but one
    # that still contains DEP. Hardcoding "06:00"-"23:00" here made the test fail whenever it ran
    # after ~21:00 Europe/Oslo, since DEP is now+2h: a latent CI landmine, not a real assertion.
    _register("dev-second-002", begin="00:01", end="23:59")
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

    def rejected(*a, outcome=None, **k):
        # The model WAS asked and what came back was unusable for this input. Since 2026-08-22 the
        # stub has to say so: a bare None now means "no backend was asked", which charges nothing.
        attempts.append(1)
        if outcome is not None:
            outcome["status"] = "rejected"

    monkeypatch.setattr(varsel, "_write_message", rejected)
    _register(DEVICE)

    for _ in range(10):  # ten poller cycles on the same live situation
        varsel.check_and_notify(SNAPSHOT)

    assert len(attempts) == varsel._MAX_MESSAGE_ATTEMPTS, (
        f"expected the write to be abandoned after {varsel._MAX_MESSAGE_ATTEMPTS} attempts, "
        f"got {len(attempts)}"
    )
    assert len(sent) == 1, "the push still went out once, with the deterministic wording"


def test_a_transient_failure_does_not_spend_the_payload_retry_budget(monkeypatch):
    """The 2026-08-22 fix, and the measurement that forced it.

    A timeout or a 429 says nothing about the payload, but every `None` used to charge the same
    counter — so three bad minutes at Vertex retired a subject's wording for the situation's whole
    12 h life. Measured in prd over the 14 days to 2026-08-21: of the subjects that ever reached
    `attempts_exhausted`, the statuses preceding it were **290 `error` and zero `rejected` or
    `ungrounded`**. The budget meant for hopeless payloads was, in practice, spent entirely on
    transient failures — while 55 % of subjects that hit an error went on to succeed later.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    calls = []

    def transient(*a, outcome=None, **k):
        calls.append(1)
        if outcome is not None:
            outcome["status"] = "error"

    monkeypatch.setattr(varsel, "_write_message", transient)
    _register(DEVICE)

    for _ in range(6):
        varsel.check_and_notify(SNAPSHOT)

    assert len(calls) == 6, (
        f"a transient failure spent the payload budget and stopped the retries after {len(calls)}"
    )


def test_a_transient_failure_is_still_bounded_by_its_own_budget(monkeypatch):
    """Not charging the payload counter must not mean retrying forever. Each transient failure is a
    real call and a real slot from `_MAX_LLM_CALLS_PER_CYCLE`, so a subject failing every cycle for
    12 h would spend ~240 of them — the starvation the give-up rule exists to prevent, just wearing
    a different hat."""
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    calls = []

    def transient(*a, outcome=None, **k):
        calls.append(1)
        if outcome is not None:
            outcome["status"] = "error"

    monkeypatch.setattr(varsel, "_write_message", transient)
    _register(DEVICE)

    for _ in range(varsel._MAX_TRANSIENT_ATTEMPTS + 5):
        varsel.check_and_notify(SNAPSHOT)

    assert len(calls) == varsel._MAX_TRANSIENT_ATTEMPTS, (
        f"expected {varsel._MAX_TRANSIENT_ATTEMPTS} transient attempts, got {len(calls)}"
    )
    statuses = {k[1]: v.get("status") for k, v in varsel._message_meta.items()}
    assert statuses.get("S1") == "transient_exhausted", (
        f"a transient give-up must not be filed as a verdict on the payload: {statuses}"
    )


def test_a_success_clears_the_transient_count(monkeypatch):
    """A subject that recovers must not still be retired by errors it has demonstrably outlived --
    which is the 55 % of prd's errored subjects that went on to succeed."""
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    fail = {"on": True}
    calls = []

    def flaky(*a, outcome=None, **k):
        calls.append(1)
        if fail["on"]:
            if outcome is not None:
                outcome["status"] = "error"
            return None
        if outcome is not None:
            outcome["status"] = "ok"
        return "Skrevet til slutt."

    monkeypatch.setattr(varsel, "_write_message", flaky)
    _register(DEVICE)

    for _ in range(varsel._MAX_TRANSIENT_ATTEMPTS - 1):
        varsel.check_and_notify(SNAPSHOT)
    fail["on"] = False
    varsel.check_and_notify(SNAPSHOT)  # the cycle that finally works

    assert not varsel._message_errors, "the error count survived a success"
    # And the subject is genuinely writable again rather than one failure from being retired.
    with varsel._state_lock:
        varsel._messages.clear()
    fail["on"] = True
    before = len(calls)
    for _ in range(3):
        varsel.check_and_notify(SNAPSHOT)
    assert len(calls) == before + 3, "the budget was not actually reset by the success"


def test_generation_being_off_does_not_spend_the_retry_budget(monkeypatch):
    """A cycle that asks no backend must not cost a retry.

    `_write_message` returns None on several paths that never reach a model — generation switched
    off, or every backend latched off after a 404 — and the caller spends an attempt on any None. So
    three such cycles used to exhaust a situation permanently: when `llm_message._LATCH_TTL` expired
    30 minutes later and a backend came back, the situation was already at `_MAX_MESSAGE_ATTEMPTS`
    and was never offered to the writer again for its whole life. In prd that silently thins the
    written-but-withheld population the rollout is judged on.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _register(DEVICE)

    monkeypatch.setattr(varsel, "_generation_enabled", lambda: False)
    for _ in range(5):  # five cycles with nothing to ask
        varsel.check_and_notify(SNAPSHOT)
    assert not varsel._message_attempts, "no backend was reached, so no attempt may be spent"

    # The backend comes back. The situation must still be offered to the writer.
    written = []
    monkeypatch.setattr(varsel, "_generation_enabled", lambda: True)
    monkeypatch.setattr(
        varsel, "_write_message", lambda *a, **k: written.append(1) or "Toget er innstilt."
    )
    varsel.check_and_notify(SNAPSHOT)
    assert written, "a recovered backend must still get to write this situation's wording"


def test_a_cached_wording_is_recorded_beside_a_fresh_template(monkeypatch):
    """The `ok_cached` trap, pinned: the two wordings on that row describe different cycles.

    `message_llm` is reused from the cycle that wrote it, while `message_deterministic` is rebuilt
    from THIS cycle's input every time. So on an `ok_cached` row the pair is not a same-input
    comparison, which is why message_wording_comparison.sql filters on `message_status = 'ok'`.
    Documented in three places and, until now, asserted in none.
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(
        llm_message,
        "write_message_detailed",
        lambda message_input, **kw: llm_message.MessageResult(
            "Skrevet i forrige syklus.", provider="gemini", model="g", status="ok"
        ),
    )
    _register(DEVICE)

    varsel.check_and_notify(SNAPSHOT)
    first = [r for r in log.rows if r["message_status"] == "ok"]
    assert first, "the first cycle writes the wording"

    varsel.check_and_notify(SNAPSHOT)  # same live situation, wording served from cache
    cached = [r for r in log.rows if r["message_status"] == "ok_cached"]
    assert cached, "the second cycle reuses it rather than re-asking"
    row = cached[-1]
    assert row["message_llm"] == "Skrevet i forrige syklus.", (
        "the cached row still records the model's wording"
    )
    assert row["message_deterministic"], "and a template wording rebuilt this cycle beside it"
    assert row["message_deterministic"] != row["message_llm"], (
        "the two describe different cycles' inputs -- the reason 'ok' is the same-input filter"
    )


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


def test_a_message_written_about_another_train_is_not_shown(monkeypatch):
    """Both caches outlive the train they describe: they are pruned when the SITUATION ends (up to
    its 12 h window), while the alert underneath is re-scored every tick and catchable_after walks
    departed trains out of the result. Keyed on (journey, situation) alone, a message written at
    06:05 about the 07:52 train still headed the card at 08:40, above a freshly computed line about
    the 09:12 one. The key carries the departure so that cannot happen.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: "Om det tidligere toget.")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert len(varsel._messages) == 1
    stale_key = next(iter(varsel._messages))

    # Same journey, same situation, a LATER train: the cached text must not be attached to it.
    later = stale_key[0], stale_key[1], "2099-01-01T06:00:00+00:00"
    varsel._messages[later] = varsel._messages.pop(stale_key)
    r = client.get("/varsel-api/journeys", params={"device_id": DEVICE})
    alerts = r.json()["journeys"][0]["alerts"]
    assert alerts and alerts[0]["message"] is None, (
        "text written about a different departure must not head this alert"
    )


def test_a_departed_alternative_is_not_offered(monkeypatch):
    """A stale alternative is worse than none: it advises catching a train that has already left."""
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    key = varsel._message_key(
        varsel.journey_key(next(iter(varsel._journeys.values()))),
        varsel.match_journey(next(iter(varsel._journeys.values())))[0],
    )
    gone = alternatives.AlternativeJourney(
        departure_time=datetime.now(UTC) - timedelta(hours=1),
        arrival_time=datetime.now(UTC) - timedelta(minutes=20),
        duration_min=40, minutes_saved=15, legs=[],
    )
    varsel._alternatives[key] = gone
    alerts = client.get("/varsel-api/journeys", params={"device_id": DEVICE}).json()[
        "journeys"][0]["alerts"]
    assert alerts and alerts[0]["alternative_journey"] is None


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


def test_the_journey_card_honours_the_delivery_switch(monkeypatch):
    """The card is the OTHER traveller-facing surface, and the one an implementer forgets.

    Since 2026-08-07 the wording is written whenever a backend can be tried, and VARSEL_LLM_ENABLED
    only decides which of the two already-written wordings the traveller gets. Gate the push and
    not this read and prd's card silently starts showing LLM prose the push it sits beside does not
    carry -- a difference nobody would see in a test, because the push assertions would all still
    hold. `message: null` is the state the PWA already renders (it is what "no message yet" has
    always looked like), so delivery-off needs no frontend change.
    """
    import llm_message

    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(
        varsel, "_write_message", lambda *a, **k: "Toget er mest sannsynlig innstilt."
    )
    monkeypatch.setattr(llm_message, "delivery_enabled", lambda: False)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    assert varsel._messages, (
        "generation does not depend on delivery any more: the wording is still written and cached, "
        "which is what U3 records beside the sent one"
    )
    alerts = client.get("/varsel-api/journeys", params={"device_id": DEVICE}).json()[
        "journeys"][0]["alerts"]
    assert alerts and alerts[0]["message"] is None, (
        "with delivery off the card must not show a wording the push did not carry"
    )


def test_journey_listing_omits_the_internal_impact_reference():
    """Alert.impact is a working reference for the message writer, not part of the contract."""
    _reset_state()
    _register(DEVICE)
    r = client.get("/varsel-api/journeys", params={"device_id": DEVICE})
    alerts = r.json()["journeys"][0]["alerts"]
    assert alerts, "the synthetic snapshot should produce a live match"
    assert "impact" not in alerts[0]


# ---- the alert-decision log -------------------------------------------------------------------
# A situation that matches the same journey but scores well under every operator's threshold, so a
# cycle produces one alerting and one non-alerting decision.
SNAPSHOT_MIXED = {
    "S1": SNAPSHOT["S1"],
    "S2": [
        FakeCall("t2", "A", 1, DEP + timedelta(minutes=10), situation_number="S2",
                 line="VYG:Line:R11", summary="Kort innstilling", p_cancel=0.02,
                 p_impact_dep=0.10),
        FakeCall("t2", "B", 5, None, situation_number="S2", line="VYG:Line:R11",
                 aimed_arr=DEP + timedelta(minutes=50)),
    ],
}


# TWO disrupted departures on one registered journey, each with its own SX message: the shape
# `one_alert_per_train` keeps as two cards and loop 2 delivers as two pushes. Both clear the rail
# gate, and t3 is deliberately the MILDER of the two so a test can tell "the writer served the worst
# train" apart from "the writer served the train that was about to be pushed".
SNAPSHOT_TWO_DISRUPTED = {
    "S1": SNAPSHOT["S1"],
    "S3": [
        FakeCall("t3", "A", 1, DEP + timedelta(minutes=20), situation_number="S3",
                 line="VYG:Line:R11", summary="Solnedgang i sporvekselen", p_cancel=0.40,
                 p_impact_dep=0.75),
        FakeCall("t3", "B", 5, None, situation_number="S3", line="VYG:Line:R11",
                 summary="Solnedgang i sporvekselen", aimed_arr=DEP + timedelta(minutes=60)),
    ],
}


# The 2026-08-05 report: two SX messages about ONE departure. Same journey (t1), same boarding call,
# scored independently under each situation -- which is exactly why the two readings differ (0.90 vs
# 0.87) and why the card showed "91 %" and "87 %" for one train, one box apart.
SNAPSHOT_TWO_NOTICES = {
    "S1": SNAPSHOT["S1"],
    "S2": [
        FakeCall("t1", "A", 1, DEP, situation_number="S2", summary="Innstilt Drammen–Skøyen",
                 p_cancel=0.78, p_impact_dep=0.87),
        FakeCall("t1", "B", 5, None, situation_number="S2", summary="Innstilt Drammen–Skøyen",
                 aimed_arr=DEP + timedelta(minutes=40)),
    ],
}


def test_two_notices_about_one_train_are_one_card():
    """One departure, one box. Both situations are real and both hit this trip, but the traveller
    has one train to catch -- and two boxes meant two different percentages for it."""
    _reset_state()
    _use_snapshot(SNAPSHOT_TWO_NOTICES)
    _register(DEVICE)

    alerts = client.get(
        "/varsel-api/journeys", params={"device_id": DEVICE}
    ).json()["journeys"][0]["alerts"]
    assert len(alerts) == 1
    # The survivor is the more severe reading, not whichever situation was iterated first.
    assert alerts[0]["situation_number"] == "S1"
    assert alerts[0]["impact_probability"] == 0.9


def test_two_notices_about_two_different_trains_stay_two_cards():
    """The collapse is per departure, not per journey: two trains is still two things to know."""
    _reset_state()
    _use_snapshot(SNAPSHOT_MIXED)  # t1 under S1, t2 under S2
    _register(DEVICE)

    alerts = client.get(
        "/varsel-api/journeys", params={"device_id": DEVICE}
    ).json()["journeys"][0]["alerts"]
    assert {a["situation_number"] for a in alerts} == {"S1", "S2"}


def test_two_notices_about_one_train_are_one_push(monkeypatch):
    """Same defect on the lock screen, one worse: two notifications a minute apart saying the same
    thing about one departure with different numbers."""
    _reset_state()
    _use_snapshot(SNAPSHOT_TWO_NOTICES)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT_TWO_NOTICES)

    assert len(sent) == 1


def test_every_train_a_cycle_pushes_about_gets_its_own_wording(monkeypatch):
    """One wording per TRAIN, because that is what loop 2 delivers (2026-08-21).

    The writing pass used to serve `next(a for a in alerts if a.alertworthy)` -- one train per
    journey -- while delivery pushes per train. So a second disrupted departure went to the lock
    screen with the template while the first carried the model's prose: 604 of dev's 844 pushes over
    14 days, 83 % of everything travellers actually received, filed under
    `message_status = 'not_selected'`.
    """
    _reset_state()
    _use_snapshot(SNAPSHOT_TWO_DISRUPTED)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")

    def fake_write(alert, *, journey_key, message_input, **_):
        return f"Skrevet om {alert.situation_number}."

    monkeypatch.setattr(varsel, "_write_message", fake_write)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT_TWO_DISRUPTED)

    assert len(sent) == 2, "two disrupted trains is two pushes"
    # The point of the fix: BOTH bodies are written prose. Before it the milder train's push carried
    # the deterministic wording, which names the train and quotes the numbers but is not this text.
    assert sorted(p["body"] for p in sent) == ["Skrevet om S1.", "Skrevet om S3."]


def test_the_writing_budget_follows_the_push_not_the_severity(monkeypatch):
    """Spend it where it can still change what somebody sees.

    Alert-worthy trains are re-scored every tick and run p50 3 / p90 12 per journey-cycle on dev,
    but `_notified` means each reaches a phone once, so PUSHES run p90 1 and 88 % of cycles push
    none at all. Writing for every alert-worthy train would therefore spend a bounded budget
    re-wording trains nobody will be told about again -- and starve the one that is about to be.
    """
    _reset_state()
    written = []

    def fake_write(alert, *, journey_key, message_input, **_):
        written.append(alert.situation_number)
        return f"Skrevet om {alert.situation_number}."

    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_write_message", fake_write)
    _register(DEVICE)

    # Cycle 1 knows only about the worst train, and notifies the device about it.
    _use_snapshot(SNAPSHOT)
    varsel.check_and_notify(SNAPSHOT)
    assert written == ["S1"]

    # Forget the cached wording, exactly as _reconcile_caches does when a subject eases off below
    # the gate. Without this the assertion below would hold for the wrong reason -- S1 would be
    # skipped as `ok_cached` whether or not the selection had improved.
    with varsel._state_lock:
        varsel._messages.clear()
    written.clear()

    # Cycle 2 adds a MILDER second train. S1 is more severe and would win any severity-ordered
    # pick, but the device has already been notified about it: no push is coming, and its wording
    # can no longer reach anybody. S3's can.
    _use_snapshot(SNAPSHOT_TWO_DISRUPTED)
    varsel.check_and_notify(SNAPSHOT_TWO_DISRUPTED)

    assert written == ["S3"], (
        "the budget went to the most severe train rather than the one about to be pushed"
    )


# Two disrupted trains where severity and departure DISAGREE: the sooner train is the milder one.
# Every other multi-train fixture here has the worst train leaving first, so it cannot tell an
# ordering by severity apart from an ordering by departure — which is the whole question below.
SNAPSHOT_SOONER_TRAIN_IS_MILDER = {
    "S3": SNAPSHOT_TWO_DISRUPTED["S3"],  # departs DEP + 20 min, p_impact 0.75
    "S5": [
        FakeCall("t5", "A", 1, DEP + timedelta(minutes=45), situation_number="S5",
                 line="VYG:Line:R12", summary="Signalfeil ved Lysaker", p_cancel=0.88,
                 p_impact_dep=0.95),
        FakeCall("t5", "B", 5, None, situation_number="S5", line="VYG:Line:R12",
                 summary="Signalfeil ved Lysaker", aimed_arr=DEP + timedelta(minutes=90)),
    ],
}


def test_the_write_budget_goes_to_the_soonest_departure_when_it_binds(monkeypatch):
    """Soonest first, ACROSS journeys — new on 2026-08-21, because until the budget could actually
    bind across journeys there was nothing to order.

    The queue used to be walked in `_journeys` insertion order, so a cap spent its slots on whichever
    journeys were registered earliest, every cycle, forever. Invisible while ~44 registrations rarely
    reach a cap of 8; indefensible at ten times that, where the same handful of travellers would take
    the whole budget every busy morning.

    Departure order and severity order disagree here on purpose. S5 is the worse train (0.95 vs
    0.75) and wins any severity-ordered pick — but it leaves 25 minutes later, so a wording written
    for it now can be written again on a later cycle, while S3's traveller has to move soon.
    """
    _reset_state()
    _use_snapshot(SNAPSHOT_SOONER_TRAIN_IS_MILDER)
    written = []

    def fake_write(alert, *, journey_key, message_input, **_):
        written.append(alert.situation_number)
        return f"Skrevet om {alert.situation_number}."

    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_write_message", fake_write)
    monkeypatch.setattr(varsel, "_MAX_LLM_CALLS_PER_CYCLE", 1)  # exactly one slot to give away
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT_SOONER_TRAIN_IS_MILDER)

    assert written == ["S3"], (
        "the single write slot went to the more severe train rather than the sooner one"
    )


def test_the_capped_train_is_still_recorded_as_capped(monkeypatch):
    """What the ceiling cost has to stay countable on the decision rows, not inferred from a log
    line — the same reason `alt_status` exists. With the calls concurrent, the refusal now happens
    on a worker thread, which is exactly where a status write is easy to drop."""
    _reset_state()
    _use_snapshot(SNAPSHOT_SOONER_TRAIN_IS_MILDER)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda alert, **kw: "Skrevet.")
    monkeypatch.setattr(varsel, "_MAX_LLM_CALLS_PER_CYCLE", 1)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT_SOONER_TRAIN_IS_MILDER)

    statuses = {k[1]: v.get("status") for k, v in varsel._message_meta.items()}
    assert statuses.get("S5") == "capped", f"expected S5 capped, got {statuses}"


def test_the_suppressed_notice_is_still_recorded(monkeypatch):
    """Collapsing is a presentation rule and must not reach the log: "every decision, not every
    push". A situation hidden on the card is still a decision that was made about it, and its row is
    part of the lead-time series the labelling job scores."""
    _reset_state()
    _use_snapshot(SNAPSHOT_TWO_NOTICES)
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT_TWO_NOTICES)

    assert {r["situation_number"] for r in log.rows} == {"S1", "S2"}


class _FakeBQ:
    """Enough of google.cloud.bigquery.Client for the decision log; records what was inserted."""

    def __init__(self, insert_error: Exception | None = None, query_error: Exception | None = None):
        self.rows: list[dict] = []
        self.insert_error = insert_error
        self.query_error = query_error
        self.queries = 0

    def query(self, sql, **kw):  # the CREATE TABLE IF NOT EXISTS
        self.queries += 1
        if self.query_error is not None:
            raise self.query_error

        class _Job:
            # `timeout=` is passed by decisions.ensure_table; the real client accepts it and this
            # fake must too, or the DDL "fails" here and the backoff silences every later test.
            def result(self, timeout=None):
                return None

        return _Job()

    def insert_rows_json(self, table, rows):
        if self.insert_error is not None:
            raise self.insert_error
        self.rows.extend(rows)
        return []


def _use_decision_log(monkeypatch, insert_error=None, query_error=None) -> _FakeBQ:
    import decisions

    fake = _FakeBQ(insert_error, query_error)
    monkeypatch.setattr(decisions, "_bq_client", fake)
    monkeypatch.setattr(decisions, "_table_ready", False)
    # The retry backoff is module state that would otherwise leak between tests.
    monkeypatch.setattr(decisions, "_ensure_failed_at", None)
    return fake


def test_decisions_record_non_alerting_matches_too(monkeypatch):
    """The recall half of the confusion matrix only exists if we write down what we did NOT alert on.

    Recording pushes alone can never produce a `miss`: a journey we stayed silent about leaves no
    trace, so every precision figure would be measured against a population we hand-picked.
    """
    _reset_state()
    _use_snapshot(SNAPSHOT_MIXED)
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT_MIXED)

    by_situation = {r["situation_number"]: r for r in log.rows}
    assert set(by_situation) == {"S1", "S2"}
    assert by_situation["S1"]["alertworthy"] is True
    assert by_situation["S2"]["alertworthy"] is False


def test_decision_row_carries_the_et_join_key(monkeypatch):
    """Without (operator, jkey, call_date, stop_order) the row can never be checked against ET."""
    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = log.rows[0]
    assert (row["operator"], row["jkey"], row["stop_order"]) == ("VYG", "t1", 1)
    assert row["call_date"] == DEP.astimezone(_OSLO).date().isoformat()
    assert row["departure_time"] is not None and row["decided_at"] is not None
    # Lead time is the axis alerting quality varies along most, so it must be recoverable.
    assert row["minutes_to_departure"] > 0


def test_decision_row_records_delivery_and_device_count(monkeypatch):
    _reset_state()
    _use_snapshot(SNAPSHOT_MIXED)
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _register(DEVICE)
    _register("dev-second12")  # same journey definition, second device
    varsel.check_and_notify(SNAPSHOT_MIXED)

    by_situation = {r["situation_number"]: r for r in log.rows}
    # One row per journey DEFINITION, not per device -- two devices on one trip is still one row.
    assert len(log.rows) == 2
    assert by_situation["S1"]["pushed"] is True
    assert by_situation["S2"]["pushed"] is False
    assert by_situation["S1"]["n_devices"] == 2


def test_decision_row_holds_no_device_or_journey_identifier(monkeypatch):
    """The log is a record of what was said about a train, not of where a person commutes.

    Keyed on the journey definition plus a count; the traveller's window and weekday pattern are
    deliberately absent, matching why llm_message logs a digest instead of the key.
    """
    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = log.rows[0]
    assert DEVICE not in json.dumps(row)
    assert not {"device_id", "journey_id", "begin", "end", "days"} & set(row)
    # The digest is what makes a row joinable to its event="llm_message" log line.
    import llm_message

    key = varsel.journey_key(next(iter(varsel._journeys.values())))
    assert row["journey_digest"] == llm_message.key_digest(key)


def test_decision_write_failure_never_blocks_a_push(monkeypatch):
    """A traveller's alert must not depend on the observability that watches it."""
    _reset_state()
    _use_decision_log(monkeypatch, insert_error=RuntimeError("BigQuery is down"))
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)  # must not raise
    assert len(sent) == 1


def test_decisions_are_not_recorded_without_a_client(monkeypatch):
    """No BigQuery client (tests, a failed build) disables recording rather than erroring."""
    import decisions

    _reset_state()
    monkeypatch.setattr(decisions, "_bq_client", None)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)  # must not raise
    assert decisions.enabled() is False


def test_decision_row_stores_the_text_that_was_actually_sent(monkeypatch):
    """prd runs with the LLM off, so a row must carry the deterministic body, not NULL.

    Storing only LLM output left every deterministic push with no message at all -- which is the
    normal case in prd (VARSEL_LLM_ENABLED=false), so /historikk there would have shown a decision
    log with nothing said in it.
    """
    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: None)  # LLM unavailable
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["message_source"] == "deterministic"
    # The same wording the push carried, not an empty column.
    assert "82 % sjanse for innstilling" in row["message"]
    # Nothing was written, so the LLM column is NULL and `message` is the template one -- the third
    # of the three delivery states, and the only one where the pair collapses to a single wording.
    assert row["message_llm"] is None
    assert row["message"] == row["message_deterministic"]


def test_decision_row_labels_a_written_message_as_llm(monkeypatch):
    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: "Toget er innstilt.")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert (row["message"], row["message_source"]) == ("Toget er innstilt.", "llm")
    # Delivery is on (the suite pins VARSEL_LLM_ENABLED=true), so the sent text IS the written one --
    # and the template wording is recorded anyway, which is what makes the two comparable on rows
    # where the model's version is the one that went out.
    assert row["message_llm"] == "Toget er innstilt."
    assert "82 % sjanse for innstilling" in row["message_deterministic"]


def test_decisions_are_recorded_even_when_push_is_disabled(monkeypatch):
    """"Every decision, not every push" has to hold when push itself is off.

    An early return on `push_enabled()` meant an environment without VAPID keys recorded nothing,
    so /historikk looked like a broken deployment rather than an un-provisioned one.
    """
    _reset_state()
    _register(DEVICE)  # register while push is still on, so a journey exists
    log = _use_decision_log(monkeypatch)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    varsel.check_and_notify(SNAPSHOT)

    assert sent == [], "push is disabled, so nothing may be delivered"
    assert [r["situation_number"] for r in log.rows] == ["S1"]
    assert log.rows[0]["pushed"] is False


def test_every_decision_records_the_message_input_even_below_the_threshold(monkeypatch):
    """The writer's input is logged for decisions no message was ever written for.

    Those are the majority of rows -- and the ones a miss is made of. Building the input only when
    the writer runs left "what did the model see?" unanswerable for exactly them.
    """
    _reset_state()
    _use_snapshot(
        {
            "S1": [
                FakeCall("t1", "A", 1, DEP, p_cancel=0.02, p_impact_dep=0.10),
                FakeCall("t1", "B", 5, None, aimed_arr=DEP + timedelta(minutes=40)),
            ]
        }
    )
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _register(DEVICE)
    varsel.check_and_notify(_active_snapshot["value"])

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["alertworthy"] is False, "0.10 must not cross the gate -- the point of this row"
    assert row["message_status"] == "not_alertworthy"
    payload = json.loads(row["message_input"])
    assert "reise" in payload and payload["sannsynlighet_prosent"] == 10
    assert row["prompt_version"], "the brief in force must be identifiable afterwards"
    # The wording columns follow the same rule as the input, and for the same reason. No model is
    # asked about a sub-threshold decision, so message_llm is NULL -- but message_deterministic is
    # written for every decision, which is what dates the row as post-2026-08-07. A NULL there means
    # "before the column existed", never "no wording", and these rows are ~83 % of the table.
    assert row["message_llm"] is None
    assert row["message_deterministic"] == row["message"]


def test_the_recorded_input_is_the_object_the_model_was_prompted_with(monkeypatch):
    """One build per decision, shared by the prompt and the row -- not two that resemble each other.

    The row used to hold an input rebuilt *after* the message was written: same code, but a second
    pass, so nothing tied the recorded document to the one the model actually saw. Assert identity,
    not equality -- equality is what a rebuild would also give you on a good day.
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    # The GENERATION gate: what decides whether the writer runs at all. Delivery is a separate
    # switch and does not belong here -- this test is about what the model was shown.
    monkeypatch.setattr(llm_message, "generation_enabled", lambda: True)

    built, prompted = [], []
    real_build = llm_message.build_message_input

    def counting(journey, impact, alert):
        built.append(real_build(journey, impact, alert))
        return built[-1]

    monkeypatch.setattr(llm_message, "build_message_input", counting)
    monkeypatch.setattr(
        llm_message,
        "write_message_detailed",
        lambda message_input, **kw: prompted.append(message_input)
        or llm_message.MessageResult("Toget er innstilt.", provider="gemini", model="g", status="ok"),
    )
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    assert len(built) == 1, "one build per journey definition x situation, not one per consumer"
    assert prompted[0] is built[0], "the model must be prompted with the object that was built"
    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["message_input"] == llm_message.message_input_json(built[0])


def test_the_deterministic_wording_is_written_from_the_message_input(monkeypatch):
    """The template reads the same document the model is prompted with.

    That is the point of routing it through the input: a figure added for the model is available to
    the fallback wording too, instead of the two drifting apart as one is enriched and the other
    keeps reaching past it to the Alert.
    """
    import llm_message

    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: None)  # no model wrote anything
    monkeypatch.setattr(
        llm_message,
        "build_message_input",
        lambda *a, **k: {
            "transportmiddel": "Buss",
            "avgang_kl": "09:30",
            "situasjoner": [{"sammendrag": "Veiarbeid"}],
            "forsinkelse_min": {"typisk": 4, "verstefall": 9},
        },
    )
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    # Every part of this body came out of the input above -- none of it off the (rail, 82 %-cancel)
    # alert the snapshot actually produced. The input carries no `linje`, so the train falls back to
    # "Buss kl 09:30": less than the reader wants, but nothing that is untrue.
    assert sent[0]["body"] == "Veiarbeid\nBuss kl 09:30: Typisk 4–9 min forsinket"


def test_the_push_names_the_train_by_its_line(monkeypatch):
    """"Tog L1 09:24", not "Tog kl 09:24" — the label a departure board would use.

    A registered journey matches whatever lines run between its two stops, so a time on its own
    leaves the reader working out which of them left at 09:24, on a lock screen, minutes before they
    would have to walk to the platform. The line is what makes the notification about *their* train.
    """
    import llm_message

    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: None)  # no model wrote anything
    monkeypatch.setattr(
        llm_message,
        "build_message_input",
        lambda *a, **k: {
            "transportmiddel": "Tog",
            "linje": "L1",
            "avgang_kl": "09:24",
            "situasjoner": [{"sammendrag": "Signalfeil"}],
            "forsinkelse_min": {"typisk": 4, "verstefall": 9},
        },
    )
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    assert sent[0]["body"] == "Signalfeil\nTog L1 09:24: Typisk 4–9 min forsinket"


def test_the_wording_falls_back_to_the_alerts_own_line_when_the_input_cannot_be_built():
    """`_message_input_from_alert` stands in for llm_message on the path where importing it failed,
    so it has to reduce the line ref itself. A push that loses the line to a payload problem is the
    failure that fallback exists to prevent, one field further in."""
    _reset_state()
    _register(DEVICE)
    alert = varsel.match_journey(next(iter(varsel._journeys.values())))[0]
    assert alert.line == "VYG:Line:R10", "the snapshot's own ref, unreduced on the Alert"
    assert "Tog R10 " in varsel._deterministic_body(varsel._message_input_from_alert(alert))
    # ...and it names no line rather than inventing one when the alert carries no ref either.
    unknown = varsel._message_input_from_alert(alert.model_copy(update={"line": None}))
    assert "linje" not in unknown
    assert "Tog kl " in varsel._deterministic_body(unknown)


def test_decision_row_records_which_backend_wrote_the_message(monkeypatch):
    """Cloud Logging has this at INFO for successes only, and only for its retention window; the row
    outlives both. Exercises the real _write_message so the wiring is covered, not a fake's opinion.
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(llm_message, "generation_enabled", lambda: True)
    monkeypatch.setattr(llm_message, "build_message_input", lambda *a, **k: {"reise": {"fra": "A"}})
    monkeypatch.setattr(
        llm_message,
        "write_message_detailed",
        lambda *a, **k: llm_message.MessageResult(
            "Toget er innstilt.", provider="gemini", model="gemini-3.5-flash", status="ok"
        ),
    )
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert (row["message"], row["message_source"]) == ("Toget er innstilt.", "llm")
    assert (row["llm_provider"], row["llm_model"]) == ("gemini", "gemini-3.5-flash")
    assert row["message_status"] == "ok"


def test_a_capped_cycle_is_distinguishable_from_a_disabled_llm(monkeypatch):
    """Both store the deterministic wording. One is a budget the poller imposed on itself and will
    lift next tick; the other is a deployment with no backend it can ask at all. Told apart only by
    message_status.
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(llm_message, "generation_enabled", lambda: True)
    monkeypatch.setattr(varsel, "_MAX_LLM_CALLS_PER_CYCLE", 0)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["message_source"] == "deterministic"
    assert row["message_status"] == "capped"


# ---- the two switches: writing a wording vs sending it ----------------------------------------
# Split on 2026-08-07. Until then one flag did both, so prd -- VARSEL_LLM_ENABLED=false -- never
# wrote a message and there was nothing to compare its own pushes against. The single old test
# below has two successors, one per gate, and both are needed: they now describe opposite halves of
# what used to be one setting, and keeping only one of them would leave a whole gate unexercised.


def test_a_deployment_that_can_ask_no_backend_says_so(monkeypatch):
    """VARSEL_LLM_PROVIDER=off, no GCP project, or every backend latched off after a 404.

    Same deterministic wording as a cap or a refusal, different cause -- and a separate test from
    the cap because the cap is checked FIRST, so one cycle cannot show both. The status value is
    still `llm_disabled` (rows written before the split use it), but what it now means is "nothing
    could be asked", never "this deployment sends the template".
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(llm_message, "generation_enabled", lambda: False)

    def never(*a, **k):
        raise AssertionError("no backend may be called when generation is off")

    monkeypatch.setattr(llm_message, "write_message_detailed", never)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    assert varsel._messages == {}, "nothing was written, so nothing may be cached"
    assert "82 % sjanse for innstilling" in sent[0]["body"], "the push carries the template wording"
    alerts = client.get("/varsel-api/journeys", params={"device_id": DEVICE}).json()[
        "journeys"][0]["alerts"]
    assert alerts[0]["message"] is None, "and the card has no written wording to show either"
    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["message_source"] == "deterministic"
    assert row["message_status"] == "llm_disabled"
    assert row["llm_provider"] is None


def test_delivery_off_still_writes_the_wording_but_pushes_the_template(monkeypatch):
    """prd's configuration: the model is asked, the traveller gets the deterministic wording.

    This is the whole point of the split. VARSEL_LLM_ENABLED=false used to mean the writer never
    ran, so prd's own traffic produced no LLM wording to compare its pushes against and the
    comparison could only ever be made on dev's handful of journeys. Now both exist for every
    alerting decision, and only the send is gated.

    The push body is asserted byte-identical to `_deterministic_body` over the very object the model
    was prompted with -- not merely "contains 82 %" -- because R5 is that no LLM outcome may change
    a single character of what a delivery-off deployment sends.
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(llm_message, "generation_enabled", lambda: True)
    monkeypatch.setattr(llm_message, "delivery_enabled", lambda: False)
    prompted = []
    monkeypatch.setattr(
        llm_message,
        "write_message_detailed",
        lambda message_input, **kw: prompted.append(message_input)
        or llm_message.MessageResult(
            "Toget ditt er innstilt. Ta R11 kl 08:24.", provider="gemini", model="g", status="ok"
        ),
    )
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    assert len(prompted) == 1, "the writer runs even though nothing it produces will be sent"
    assert list(varsel._messages.values()) == ["Toget ditt er innstilt. Ta R11 kl 08:24."]
    assert sent[0]["body"] == varsel._deterministic_body(prompted[0]), (
        "with delivery off the push must be the template wording, to the character"
    )
    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["message_status"] == "ok", (
        "the write succeeded; that a different wording was sent is not a message_status"
    )


def test_a_withheld_wording_is_recorded_beside_the_one_that_was_sent(monkeypatch):
    """Delivery off: both wordings are on the row, and `message`/`message_source` describe the SEND.

    The discriminator the two columns exist for. Until 2026-08-07 the row stored `written or
    deterministic` and labelled it `llm` whenever anything had been written -- which was harmless
    only because a wording was written exactly when it was sent. With generation and delivery split
    that is wrong three ways at once: /historikk would badge a template push "Skrevet av modellen",
    `label_varsel_decisions.sql` copies the pair into `varsel_decision_outcomes` where it is
    immutable once labelled, and "written but withheld by policy" -- `message_source =
    'deterministic' AND message_llm IS NOT NULL` -- could never match a single row, which is
    precisely the population prd's rollout is measured on.
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(llm_message, "generation_enabled", lambda: True)
    monkeypatch.setattr(llm_message, "delivery_enabled", lambda: False)
    monkeypatch.setattr(
        llm_message,
        "write_message_detailed",
        lambda *a, **k: llm_message.MessageResult(
            "Toget ditt er innstilt. Ta R11 kl 08:24.", provider="gemini", model="g", status="ok"
        ),
    )
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["message_llm"] == "Toget ditt er innstilt. Ta R11 kl 08:24.", (
        "the model's wording is recorded even though nobody received it"
    )
    assert row["message_source"] == "deterministic", (
        "message_source says what was SENT, not that a wording exists"
    )
    assert row["message"] == row["message_deterministic"] == sent[0]["body"], (
        "and `message` is the text the push actually carried, to the character"
    )
    # The write itself went fine, and the row says so: `message_status` describes the WRITER, so it
    # can never be the column that tells a withheld wording from a missing one. That distinction is
    # the message_llm/message_source pair above and nothing else.
    assert (row["message_status"], row["llm_provider"]) == ("ok", "gemini")


def test_delivery_off_survives_a_writer_that_raises(monkeypatch):
    """A backend failure must cost nothing at all when its output was never going to be sent.

    The write now happens on a path prd runs every cycle, so this is no longer a dev-only risk: a
    raise here would take the push with it for a wording nobody would have read.
    """
    import llm_message

    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(llm_message, "generation_enabled", lambda: True)
    monkeypatch.setattr(llm_message, "delivery_enabled", lambda: False)

    def boom(*a, **k):
        raise RuntimeError("Vertex exploded")

    monkeypatch.setattr(llm_message, "write_message_detailed", boom)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    assert len(sent) == 1, "a failed write must not cost the notification"
    assert "82 % sjanse for innstilling" in sent[0]["body"]


def test_the_write_budgets_are_unchanged_by_the_delivery_switch(monkeypatch):
    """The three things that suppress a write still suppress it when delivery is off.

    Turning the flag into a send-time selector widens WHEN the writer runs, and the risk is that it
    widens the budgets with it -- prd would then start spending Vertex calls it never spent before,
    on wordings it does not send. Cap, give-up counter and the "no message writing without push"
    rule are asserted here against a delivery-off deployment, since that is the one about to be
    enabled.
    """
    import llm_message

    monkeypatch.setattr(llm_message, "delivery_enabled", lambda: False)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    writes = []

    def failing_write(*a, outcome=None, **k):
        # `rejected` rather than a bare None: since 2026-08-22 the give-up counter this test is
        # about is the PAYLOAD one, and only a status in `_PAYLOAD_FAILURES` charges it. A bare None
        # now means "no backend was asked", which correctly charges nothing and would retry forever.
        writes.append(1)
        if outcome is not None:
            outcome["status"] = "rejected"

    monkeypatch.setattr(varsel, "_write_message", failing_write)

    _reset_state()
    monkeypatch.setattr(varsel, "_MAX_LLM_CALLS_PER_CYCLE", 0)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert writes == [], "the per-cycle cap still bounds writes"

    _reset_state()
    monkeypatch.setattr(varsel, "_MAX_LLM_CALLS_PER_CYCLE", 8)
    _register(DEVICE)
    for _ in range(10):  # ten poller ticks on one live situation, every write failing
        varsel.check_and_notify(SNAPSHOT)
    assert len(writes) == varsel._MAX_MESSAGE_ATTEMPTS, (
        f"the give-up counter still stops at {varsel._MAX_MESSAGE_ATTEMPTS}, got {len(writes)}"
    )

    writes.clear()
    _reset_state()
    _register(DEVICE)
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    varsel.check_and_notify(SNAPSHOT)
    assert writes == [], "no message writing without push -- text exists to be delivered"


def test_two_devices_share_one_write_with_delivery_off(monkeypatch):
    """R6 does not depend on which wording is sent: one write, and identical text on both devices.

    Worth pinning separately because the delivery gate sits inside the per-device delivery loop --
    the one place a per-device decision could creep back in and give two travellers on one trip two
    different texts.
    """
    import llm_message

    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(llm_message, "delivery_enabled", lambda: False)
    written = []
    monkeypatch.setattr(
        varsel,
        "_write_message",
        lambda alert, *, journey_key, message_input, **_: (
            written.append(journey_key) or "Skrevet melding."
        ),
    )
    _register("dev-first-0001")
    _register("dev-second-002")
    varsel.check_and_notify(SNAPSHOT)

    assert len(written) == 1, "one written message per journey definition, not per device"
    assert len(sent) == 2 and sent[0]["body"] == sent[1]["body"]
    assert sent[0]["body"] != "Skrevet melding.", "delivery is off; the template is what goes out"


def test_a_broken_message_input_costs_its_own_column_not_the_cycle(monkeypatch):
    """One journey's awkward input must not take the whole cycle's decision log with it -- the
    same rule alternatives.py learned when a malformed reply silently dropped every alert.

    The input now backs the wording too, so this pins both halves: the column is NULL (nothing was
    built, and the row must not claim otherwise) *and* the push still says something, from the
    alert's own figures.
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")

    def boom(*a, **k):
        raise ValueError("unserialisable")

    monkeypatch.setattr(llm_message, "build_message_input", boom)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["message_input"] is None
    assert row["message"], "the decision itself is still recorded in full"
    assert "82 % sjanse for innstilling" in sent[0]["body"], "and the push still carries a body"
    # The degraded wording is recorded as a WORDING and never as an input: it came from
    # `_message_input_from_alert`, which no writer was ever shown, so message_deterministic is
    # populated (it is what the push said) beside a NULL message_input (nothing was built).
    assert row["message_deterministic"] == sent[0]["body"]


def test_ensure_table_backs_off_after_a_failure(monkeypatch):
    """A permanently failing DDL must not re-issue a BigQuery job every 60 s poller tick."""
    import decisions

    _reset_state()
    log = _use_decision_log(monkeypatch, query_error=RuntimeError("permission denied"))
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    varsel.check_and_notify(SNAPSHOT)

    assert log.queries == 1, "the failed CREATE must not be retried on the very next cycle"
    assert decisions._ensure_failed_at is not None
    assert log.rows == [], "no rows are written while the table is not known to exist"


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
    monkeypatch.setattr(alternatives, "fetch_candidates", lambda *a, **k: calls.append(1) or [])
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


def test_an_alternative_found_on_the_canonical_ref_reaches_a_collapsed_refs_alert(monkeypatch):
    """The two records of "is there a faster way?" must not disagree — and only for the same train.

    varsel asks the Journey Planner on the CANONICAL stop ref only (match_journey's
    `pair_index == 0`), so when a situation's worst reading arrives from a collapsed ref -- a
    station's bus side, a ferry berth registered twice -- that alert's own /impact response was asked
    with with_alternative=False and carries none. Building the message input off the response
    recorded the decision with has_alternative=true and alt_lines filled in, beside an input with no
    `alternativ_reise`: the advice worth giving, withheld from the model and from the template both.

    What changed on 2026-08-12 is which alerts the answer may reach. An alternative is selected
    against ONE train's aimed arrival, so it transfers to a collapsed ref's reading of **that same
    train** -- same deadline, same answer -- and to nothing else. The second situation below is the
    other case, and it used to inherit too: a different train, recorded only on the collapsed ref,
    which is the reason that ref is asked about separately at all.
    """
    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: None)
    varsel.set_stop_aliases({"A2": "A"})  # place_refs("A") == ("A", "A2")

    fake_alt = alternatives.AlternativeJourney(
        departure_time=DEP, arrival_time=DEP + timedelta(minutes=25),
        duration_min=25, minutes_saved=17,
        legs=[alternatives.AlternativeLeg(mode="rail", line="VYG:Line:R10", public_code="R10",
                                          from_name="Oslo S", to_name="Lillehammer")],
    )
    # The selection rule has its own tests; what this one is about is the back-fill, so the whole
    # fetch -> score -> select chain is short-circuited to one known answer.
    monkeypatch.setattr(impact.alternatives, "fetch_candidates", lambda *a, **k: [])
    monkeypatch.setattr(impact.alternatives, "select_best", lambda *a, **k: fake_alt)

    # S1: ONE train, recorded on both refs of the place -- the ordinary shape of a station whose two
    # sides each publish their own call. The canonical ref carries the milder reading and spends the
    # Journey Planner call; the collapsed ref's reading is worse, so it is the one that wins and the
    # response the alert carries has no lookup on it. Same train, so the answer transfers.
    # S2: a train only the collapsed ref records. Nobody ever looked it up.
    _use_snapshot(
        {
            "S1": [
                FakeCall("t1", "A", 1, DEP, p_cancel=0.20, p_impact_dep=0.70),
                FakeCall("t1", "B", 5, None, aimed_arr=DEP + timedelta(minutes=40)),
                FakeCall("t1", "A2", 1, DEP, p_cancel=0.82, p_impact_dep=0.95),
            ],
            "S2": [
                FakeCall("stranger", "A2", 1, DEP, p_cancel=0.82, p_impact_dep=0.95,
                         situation_number="S2"),
                FakeCall("stranger", "B", 5, None, aimed_arr=DEP + timedelta(minutes=45),
                         situation_number="S2"),
            ],
        }
    )
    _register(DEVICE)
    varsel.check_and_notify(_active_snapshot["value"])

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["jkey"] == "t1"
    assert row["stop_place"] == "A2", "the collapsed ref's reading must be the one that won"
    assert row["has_alternative"] is True
    payload = json.loads(row["message_input"])
    assert payload["alternativ_reise"]["minutter_spart"] == 17, (
        "the flat columns and the input the message was written from must agree"
    )

    stranger = next(r for r in log.rows if r["situation_number"] == "S2")
    assert stranger["jkey"] == "stranger"
    assert stranger["has_alternative"] is False, (
        "a train nobody looked up must not inherit another train's escape route"
    )
    assert stranger["alt_status"] == "not_worth_lookup", (
        "and it is recorded as never asked, not as asked-and-empty"
    )


# ---- a superseded alternative stops being served ----------------------------------------------
# Both poller caches are pruned when the SITUATION ends, which can be 12 h after the tick that
# filled them, while the alerts underneath are re-scored every 60 s. Until 2026-08-06 nothing
# cleared an entry any earlier, because the old selection rule found an alternative "effectively
# never"; both answers are routine now, and one of them ("stay on your own train") contradicts what
# may already be cached and quoted in a written message.

# The train SNAPSHOT describes. A verdict must NAME it, or match_journey strips it as a claim about
# somebody else's train (an alert whose worst reading came from a collapsed ref).
_ALT_SUBJECT = ("VYG", "t1")

# Same train, same departure -- so the same message key -- but eased off well under every gate.
SNAPSHOT_EASED = {
    "S1": [
        FakeCall("t1", "A", 1, DEP, p_cancel=0.06, p_impact_dep=0.30),
        FakeCall("t1", "B", 5, None, aimed_arr=DEP + timedelta(minutes=40)),
    ]
}


def _selection_outcome(monkeypatch, value):
    """Drive /impact's alternative selection to a known `(journey, original_is_best, subject)`.

    Replaces fetch -> score -> select wholesale: the rule has its own hermetic suite, and what these
    checks are about is what the poller does with each of the three answers it can come back with.
    Returns a mutable holder so a test can change the answer between cycles.

    The tuple is widened to an `impact.AlternativeOutcome` here rather than at every call site: the
    three values travel together from the lookup all the way to the decision row, and the field order
    is the same one these tests have always written.
    """
    outcome = {"value": value}
    monkeypatch.setattr(
        impact,
        "_best_remaining_journey",
        lambda cells, **kw: impact.AlternativeOutcome(*outcome["value"]),
    )
    return outcome


def _numbered_messages(monkeypatch):
    """A writer that returns a different text each time, so a reused message is distinguishable from
    a re-written one (both are non-null, which is why counting calls alone is not enough)."""
    written = []
    monkeypatch.setattr(
        varsel,
        "_write_message",
        lambda *a, **k: written.append(1) or f"Melding {len(written)}.",
    )
    return written


def _listed_alert():
    r = client.get("/varsel-api/journeys", params={"device_id": DEVICE})
    assert r.status_code == 200, r.text
    alerts = r.json()["journeys"][0]["alerts"]
    assert alerts, "the synthetic snapshot should keep producing a match"
    return alerts[0]


def _some_alternative(minutes_out=5):
    return alternatives.AlternativeJourney(
        departure_time=DEP + timedelta(minutes=minutes_out),
        arrival_time=DEP + timedelta(minutes=minutes_out + 30),
        duration_min=30, minutes_saved=10, legs=[],
    )


def test_an_alternative_the_selection_has_decided_against_stops_being_offered(monkeypatch):
    """`original_is_best` is the answer that did not meaningfully exist before this change: the
    selection ran, over a real pool, and chose the traveller's own train. Nothing in the caching
    layer had ever been built to clear an alternative, so the card kept offering a superseded escape
    route -- now beside advice that says the opposite."""
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    written = _numbered_messages(monkeypatch)
    outcome = _selection_outcome(monkeypatch, (_some_alternative(), False, _ALT_SUBJECT))
    _register(DEVICE)

    varsel.check_and_notify(SNAPSHOT)
    alert = _listed_alert()
    assert alert["alternative_journey"] is not None
    assert alert["message"] == "Melding 1."

    outcome["value"] = (None, True, _ALT_SUBJECT)  # "bli sittende" -- your own train is still best
    varsel.check_and_notify(SNAPSHOT)
    alert = _listed_alert()
    assert alert["alternative_journey"] is None, (
        "the card must stop advising an alternative the selection has since decided against"
    )
    assert alert["message"] == "Melding 2.", (
        "the text was written from that alternative and names its departure time"
    )
    assert len(written) == 2


def test_a_match_that_eased_below_the_gate_carries_neither_cache(monkeypatch):
    """The commonest way this goes stale, and the one the old code could not reach: when nothing is
    alert-worthy any more, check_and_notify takes its early return -- which sat ABOVE every pop."""
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _numbered_messages(monkeypatch)
    _selection_outcome(monkeypatch, (_some_alternative(), False, _ALT_SUBJECT))
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert _listed_alert()["alternative_journey"] is not None

    _use_snapshot(SNAPSHOT_EASED)
    varsel.check_and_notify(SNAPSHOT_EASED)
    alert = _listed_alert()
    assert alert["alertworthy"] is False
    assert alert["alternative_journey"] is None
    assert alert["message"] is None, (
        "last hour's wording must not head a card showing this tick's mild numbers"
    )


def test_an_unchanged_alternative_does_not_re_write_the_message(monkeypatch):
    """Invalidation must not become a per-cycle LLM call: `ok_cached` is what keeps a 12 h situation
    inside _MAX_LLM_CALLS_PER_CYCLE. The alternative is re-selected every tick and the winner is
    stable by construction (select_best's tie-break), so an unchanged answer must reuse the text."""
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    written = _numbered_messages(monkeypatch)
    _selection_outcome(monkeypatch, (_some_alternative(), False, _ALT_SUBJECT))
    _register(DEVICE)

    varsel.check_and_notify(SNAPSHOT)
    varsel.check_and_notify(SNAPSHOT)
    assert len(written) == 1, "an unchanged alternative must not cost a second write"
    assert _listed_alert()["message"] == "Melding 1."
    key = next(iter(varsel._messages))
    assert varsel._message_meta[key]["status"] == "ok_cached"


def test_a_message_whose_alternative_has_departed_is_replaced(monkeypatch):
    """A cached text names a specific departure time and is reused for the situation's whole life.
    Once that train has gone the text is advice to catch something that has already left -- so the
    alternative and the wording built on it go together, and the next cycle writes a fresh one."""
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    written = _numbered_messages(monkeypatch)
    outcome = _selection_outcome(monkeypatch, (_some_alternative(), False, _ALT_SUBJECT))
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    # Time passes: the offered train leaves, and this cycle establishes nothing new (a Journey
    # Planner outage, the fetch budget, a collapsed ref -- all of them land here).
    key = next(iter(varsel._alternatives))
    varsel._alternatives[key] = alternatives.AlternativeJourney(
        departure_time=datetime.now(UTC) - timedelta(minutes=1),
        arrival_time=datetime.now(UTC) + timedelta(minutes=25),
        duration_min=26, minutes_saved=5, legs=[],
    )
    outcome["value"] = (None, False, _ALT_SUBJECT)
    varsel.check_and_notify(SNAPSHOT)

    assert key not in varsel._alternatives
    assert len(written) == 2, "the text naming the departed train must not be reused"
    assert _listed_alert()["message"] == "Melding 2."


def test_a_failing_cache_reconciliation_costs_the_cache_not_the_cycle(monkeypatch):
    """`_reconcile_caches` was called bare, outside the per-journey guard around `match_journey`.

    A raise there unwound out of check_and_notify entirely: the remaining journeys were never
    scored, loop 2 never ran (NO pushes at all for the cycle) and `_record_decisions` never ran (no
    rows either) -- a strictly larger blast radius than the 2026-07-29 incident this file's
    defence-in-depth exists to prevent. What it touches is duck-typed (a provider's impact response,
    a cached alternative, an aware/naive datetime comparison), so "it cannot raise" is an assumption
    about somebody else's object. Keeping one cycle's stale cache is the smaller failure by far.
    """
    _reset_state()
    log = _use_decision_log(monkeypatch)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")

    def boom(*a, **k):
        raise AttributeError("'ImpactResponse' object has no attribute 'original_is_best'")

    monkeypatch.setattr(varsel, "_reconcile_caches", boom)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)  # must not raise

    assert len(sent) == 1, "the traveller is still told"
    assert [r["situation_number"] for r in log.rows] == ["S1"], "and the decision is still recorded"


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
