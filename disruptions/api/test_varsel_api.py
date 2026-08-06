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
        varsel._message_meta.clear()
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
    assert sent[0]["title"] == "Avvik: Oslo S → Lillehammer"
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
        "[dev] Avvik: Oslo S → Lillehammer",
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

    def fake_write(alert, *, journey_key, message_input):
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

    def boom(alert, *, journey_key, message_input):
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
        lambda alert, *, journey_key, message_input: (
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


def test_decision_row_labels_a_written_message_as_llm(monkeypatch):
    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_write_message", lambda *a, **k: "Toget er innstilt.")
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert (row["message"], row["message_source"]) == ("Toget er innstilt.", "llm")


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
    monkeypatch.setattr(llm_message, "llm_enabled", lambda: True)

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
    # alert the snapshot actually produced.
    assert sent[0]["body"] == "Veiarbeid\nBuss kl 09:30: Typisk 4–9 min forsinket"


def test_decision_row_records_which_backend_wrote_the_message(monkeypatch):
    """Cloud Logging has this at INFO for successes only, and only for its retention window; the row
    outlives both. Exercises the real _write_message so the wiring is covered, not a fake's opinion.
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(llm_message, "llm_enabled", lambda: True)
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
    lift next tick; the other is how prd is configured. Told apart only by message_status.
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(llm_message, "llm_enabled", lambda: True)
    monkeypatch.setattr(varsel, "_MAX_LLM_CALLS_PER_CYCLE", 0)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["message_source"] == "deterministic"
    assert row["message_status"] == "capped"


def test_a_deployment_with_the_llm_off_says_so(monkeypatch):
    """The prd configuration. Same deterministic wording as a cap or a refusal, different cause --
    and a separate test from the cap because the cap is checked FIRST, so one cycle cannot show both.
    """
    import llm_message

    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(llm_message, "llm_enabled", lambda: False)
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["message_source"] == "deterministic"
    assert row["message_status"] == "llm_disabled"
    assert row["llm_provider"] is None


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
    """The two records of "is there a faster way?" must not disagree.

    varsel asks the Journey Planner once per window, on the CANONICAL stop ref (match_journey's
    `pair_index == 0`), and back-fills the answer onto every alert. So when a situation's worst
    reading arrives from a collapsed ref -- a station's bus side, a ferry berth registered twice --
    that alert's own /impact response was asked with with_alternative=False and carries none.
    Building the message input off the response recorded the decision with has_alternative=true and
    alt_lines filled in, beside an input with no `alternativ_reise`: the advice worth giving,
    withheld from the model and from the template both.
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

    # One situation, two trains: the canonical ref boards the milder one (so that pass matches, and
    # is the one that spends the Journey Planner call), the collapsed ref the worse one -- which is
    # the reading that wins, and therefore the response the alert carries.
    _use_snapshot(
        {
            "S1": [
                FakeCall("mild", "A", 1, DEP, p_cancel=0.20, p_impact_dep=0.70),
                FakeCall("mild", "B", 5, None, aimed_arr=DEP + timedelta(minutes=40)),
                FakeCall("worst", "A2", 1, DEP, p_cancel=0.82, p_impact_dep=0.95),
                FakeCall("worst", "B", 5, None, aimed_arr=DEP + timedelta(minutes=45)),
            ]
        }
    )
    _register(DEVICE)
    varsel.check_and_notify(_active_snapshot["value"])

    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["jkey"] == "worst", "the collapsed ref's train must be the one that won"
    assert row["has_alternative"] is True
    payload = json.loads(row["message_input"])
    assert payload["alternativ_reise"]["minutter_spart"] == 17, (
        "the flat columns and the input the message was written from must agree"
    )


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
