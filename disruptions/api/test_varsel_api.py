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
import logging
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


def _wording(payload: dict) -> str:
    """The wording part of a push body (2026-09-02: the body leads with the route on its own line,
    so the title can carry the fact about the train). The wording is what the decision log records
    and what the two writers are compared on, so that is what these checks read."""
    route, _, wording = payload["body"].partition("\n")
    assert " → " in route, f"the body must lead with the route line, got {route!r}"
    return wording


def _expected_title(state: str = "kan bli innstilt", prefix: str = "") -> str:
    """The SNAPSHOT train's push title. DEP is clock-relative, so the time is formatted here rather
    than written out -- and run between 22:00 and 24:00 Oslo the train departs tomorrow, which the
    title then says (design review 2026-09-01, finding 48)."""
    tomorrow = " i morgen" if DEP.date() > datetime.now(tz=_OSLO).date() else ""
    return f"{prefix}Tog R10 {DEP:%H:%M}{tomorrow} {state}"


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
        # The per-train push memory and the daily cap ledger (2026-09-02). A leaked count reads
        # exactly like the cap firing for no reason.
        varsel._pushed_trains.clear()
        varsel._daily_pushes.clear()
        # Why a record went away (2026-09-04). A leaked tombstone makes a fresh registration in the
        # next test inherit an old `created_ts`, which reads like the restore logic misfiring.
        varsel._device_tombstones.clear()
    # llm_message's backend latch is PROCESS-global and survives a monkeypatch undo, so a test that
    # drives both backends into `_latched_off` (the 404/configuration path) silently disables
    # generation for every test that runs after it. That was invisible until 2026-08-07, when
    # `check_and_notify` began consulting `generation_enabled()` before reaching the writer -- at
    # which point the leak turned into "the writer is never called" in unrelated tests, which reads
    # exactly like the give-up bug those tests exist to pin. Clear it here, like test_llm_message's
    # own `_reset` does -- which since 2026-08-21 means replacing the HOLDER the latch lives in, not
    # rebinding module globals that nothing would read any more.
    import llm_message

    llm_message._STATE = llm_message._LatchState()
    # `notification_wording`'s per-surface allowance and latch holders, for exactly the same reason
    # one line up and with the same failure (U1, 2026-09-08). The module is not on the browser push
    # path -- its browser slot is empty by construction -- so nothing this suite drives spends it
    # today; it is registered here because the house rule is that every module-level budget a
    # notification surface owns is reset from this one function, and because a suite that leaves a
    # partly-spent allowance or a latched-off partner holder behind reads exactly like the give-up
    # bug the tests downstream of it exist to pin.
    import notification_wording

    notification_wording.reset_state()
    # The partner channel's two INJECTED SEAMS (U2, 2026-09-08). Not a ledger -- the pass holds no
    # partner state at module level, deliberately (KTD3: Firestore is the only dedup ledger for that
    # channel, and the snapshot `check_and_notify` matches from is pass-local) -- but a leaked stub
    # store is the same class of pollution as a leaked budget and worse to read: every later test
    # would quietly match one extra journey_key group, so a wording count or an /impact call count
    # would be off by one with nothing in that test mentioning partners. Reset to the off default,
    # which is what an un-wired deployment holds.
    varsel._partner_enabled = None
    varsel._partner_provider = None
    varsel.set_stop_aliases({})
    _use_snapshot(SNAPSHOT)


def test_the_shared_reset_clears_the_notification_wording_allowance_and_holders():
    """U1 scenario 7: one seam, called from this suite's own `_reset_state`.

    Both halves leak, and only one of them is obvious. A partly-spent trailing hour makes a later
    partner wording fall back to the template for a reason having nothing to do with what is being
    checked; a latch holder driven off by a `_ConfigurationError` stays off for
    `llm_message._LATCH_TTL` -- thirty minutes, i.e. the rest of the run -- and reports `disabled`
    with nothing saying why. Asserted through the holder the surface actually hands out rather than
    through the private field, so widening the seam cannot pass this while leaving a caller stale.
    """
    import notification_wording as nw

    _reset_state()
    nw._spend(nw._PARTNER_PATH)
    nw._spend(nw._PARTNER_PATH)
    before = nw.PARTNER.state()
    assert nw.spent(nw._PARTNER_PATH) == (2, 2)
    assert before is not None

    _reset_state()

    assert nw.spent(nw._PARTNER_PATH) == (0, 0)
    assert nw.spent(nw._BROWSER_PATH) == (0, 0)
    assert nw.PARTNER.state() is not before, "the latch holder must be rebuilt, not reused"


def test_config_reports_push_enabled():
    r = client.get("/varsel-api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["push_enabled"] is True
    assert body["vapid_public_key"] == _PUB


def test_config_reports_togvarsel_notify_only_where_wired_on():
    """The PWA's rush-forecast notice follows the delivery gate through /config: a deployment
    without the provider (this suite's configure call, an older image) reads as off, so the page
    can never announce a push class the environment's TOGVARSEL_NOTIFY_ENABLED gate withholds."""
    assert client.get("/varsel-api/config").json()["togvarsel_notify"] is False
    before = varsel._togvarsel_notify_provider
    varsel._togvarsel_notify_provider = lambda: True
    try:
        assert client.get("/varsel-api/config").json()["togvarsel_notify"] is True
    finally:
        varsel._togvarsel_notify_provider = before


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
    journey_response = client.post(
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
    journey_id = journey_response.json()["journey_id"]
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    assert sent[0]["title"] == _expected_title()
    assert sent[0]["body"].startswith("Oslo S → Lillehammer\n")
    assert sent[0]["url"] == (
        f"/varsel/?journey={journey_id}&situation=S1"
    )
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


def _register_journey(device: str) -> str:
    response = client.post(
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
    return response.json()["journey_id"]


def test_an_unbranded_device_is_pushed_its_own_icon_and_url(monkeypatch):
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    client.post(
        "/varsel-api/devices",
        json={"device_id": DEVICE, "subscription": SUBSCRIPTION, "brand": "none"},
    )
    journey_id = _register_journey(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    assert sent[0]["icon"] == "/varsel/icons/pwa-192-none.png"
    # Tapping through has to land back on the unbranded page, or the notification is neutral and
    # the app it opens is not.
    assert sent[0]["url"] == (
        f"/varsel/?brand=none&journey={journey_id}&situation=S1"
    )
    # The WORDING is brand-independent on purpose: it is what /historikk records, and text that
    # varied by variant would split every wording comparison along a second axis.
    assert sent[0]["title"] == _expected_title()
    assert sent[0]["body"].startswith("Oslo S → Lillehammer\n")
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
    journey_id = _register_journey(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert len(sent) == 1
    assert sent[0]["icon"] == "/varsel/icons/pwa-192.png"
    assert sent[0]["url"] == (
        f"/varsel/?journey={journey_id}&situation=S1"
    )


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
        _expected_title(prefix="[dev] "),
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
    assert _wording(sent[0]) == _wording(sent[1]) == "Signalfeil ved Asker. Ta R10 kl 08:14 i stedet."
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
    bodies = sorted(_wording(p) for p in sent)
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
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    for failure_status in ("error", "truncated"):
        _reset_state()
        calls = []

        def transient(
            *a, outcome=None, _calls=calls, _failure_status=failure_status, **k
        ):
            _calls.append(1)
            if outcome is not None:
                outcome["status"] = _failure_status

        monkeypatch.setattr(varsel, "_write_message", transient)
        _register(DEVICE)

        for _ in range(6):
            varsel.check_and_notify(SNAPSHOT)

        assert len(calls) == 6, (
            f"{failure_status} spent the payload budget and stopped after {len(calls)} retries"
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


# The two populations on one journey. `n1`/`n2` are calls NO SX message named (predict_calls.sql's
# `nosx` branch, 3/4 of the timetable), scored by the models alone and cut at 0.85; `S2` is an
# operator-published one that scores well under the rail gate of 0.65. Only the no-SX pair is
# gated on the card -- and only the sub-threshold half of it.
SNAPSHOT_NO_SX = {
    "S1": SNAPSHOT["S1"],
    "S2": SNAPSHOT_MIXED["S2"],
    "KIX:NoSituation:n1": [
        FakeCall("n1", "A", 1, DEP + timedelta(minutes=5), situation_number="KIX:NoSituation:n1",
                 line="VYG:Line:R12", summary="Mulig avvik", has_situation=False,
                 p_cancel=0.03, p_impact_dep=0.40),
        FakeCall("n1", "B", 5, None, situation_number="KIX:NoSituation:n1", line="VYG:Line:R12",
                 summary="Mulig avvik", has_situation=False,
                 aimed_arr=DEP + timedelta(minutes=45)),
    ],
    "KIX:NoSituation:n2": [
        FakeCall("n2", "A", 1, DEP + timedelta(minutes=15), situation_number="KIX:NoSituation:n2",
                 line="VYG:Line:R13", summary="Mulig avvik", has_situation=False,
                 p_cancel=0.55, p_impact_dep=0.92),
        FakeCall("n2", "B", 5, None, situation_number="KIX:NoSituation:n2", line="VYG:Line:R13",
                 summary="Mulig avvik", has_situation=False,
                 aimed_arr=DEP + timedelta(minutes=55)),
    ],
}


def test_a_mulig_avvik_card_needs_the_push_threshold_a_published_one_does_not():
    """A no-SX match reaches the card only once it would have been pushed.

    Every call a situation named produces a card whether or not it crosses the gate, because there
    is an operator message on it to read. A call NO situation named produces one only if it clears
    its own (higher) bar: with nothing published behind it the card is the model's numbers and
    nothing else, and every departure on the journey qualifies as a match -- so a quiet morning
    filled the page with "Mulig avvik" cards quoting a few minutes of typical delay and buried the
    disruption worth reading.
    """
    _reset_state()
    _use_snapshot(SNAPSHOT_NO_SX)
    _register(DEVICE)

    alerts = client.get(
        "/varsel-api/journeys", params={"device_id": DEVICE}
    ).json()["journeys"][0]["alerts"]
    shown = {a["situation_number"] for a in alerts}
    assert "KIX:NoSituation:n2" in shown, "0.92 clears the no-SX cut of 0.85"
    assert "KIX:NoSituation:n1" not in shown, "0.40 does not, and has no message to read"
    # Unchanged for anything an operator published: S2 is under the 0.65 rail gate and still shows.
    assert {"S1", "S2"} <= shown
    assert not next(a for a in alerts if a["situation_number"] == "S2")["alertworthy"]


def test_the_card_filter_never_reaches_the_decision_log_or_the_matcher():
    """Display only. `match_journey` still returns the sub-threshold no-SX match, because those
    rows are the lead-time series /historikk measures recall on -- a match that stayed under the
    gate is exactly what a `miss` is made of."""
    _reset_state()
    _use_snapshot(SNAPSHOT_NO_SX)
    _register(DEVICE)

    with varsel._state_lock:
        rec = next(iter(varsel._journeys.values()))
    matched = {a.situation_number for a in varsel.match_journey(rec)}
    assert "KIX:NoSituation:n1" in matched


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
    assert sorted(_wording(p) for p in sent) == ["Skrevet om S1.", "Skrevet om S3."]


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
    assert _wording(sent[0]) == "Veiarbeid\nBuss kl 09:30: Typisk 4–9 min forsinket"


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

    assert _wording(sent[0]) == "Signalfeil\nTog L1 09:24: Typisk 4–9 min forsinket"


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
    assert _wording(sent[0]) == varsel._deterministic_body(prompted[0]), (
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
    assert row["message"] == row["message_deterministic"] == _wording(sent[0]), (
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
    # Still true after KTD4 (2026-09-08), and the reason is now stated rather than implied: the
    # rule was never "no keypair, no writing" but "no reader, no writing", and with no partner
    # channel wired -- this suite's default, and every deployment's until the store lands -- a
    # missing keypair means there is no reader on any channel. The companion case, where a partner
    # subscription IS the reader, is
    # `test_a_partner_subscription_matches_and_is_written_for_without_a_keypair`.
    assert writes == [], "no deliverable recipient on any channel -- text exists to be delivered"
    assert varsel.partner_channel_enabled() is False


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
    assert len(sent) == 2 and _wording(sent[0]) == _wording(sent[1])
    assert _wording(sent[0]) != "Skrevet melding.", "delivery is off; the template is what goes out"


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
    assert row["message_deterministic"] == _wording(sent[0])


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


def test_deliver_to_devices_stops_a_device_at_gone_but_not_the_others(monkeypatch):
    """Direct pin of the extracted fan-out seam (shared with togvarsel_notify): sends stay
    sequential and ordered WITHIN a device, a "gone" stops that device's REMAINING items only,
    and results come back flattened in per-device order — the contract loop 2's dedup and drop
    bookkeeping is written against."""
    calls = []

    def _fake_send(sub, payload):
        calls.append((sub["endpoint"], payload["n"]))
        return "gone" if payload["n"] == "a2" else "sent"

    monkeypatch.setattr(varsel, "_send_push", _fake_send)
    per_device = {
        "dev-a": [("a1", {"n": "a1"}), ("a2", {"n": "a2"}), ("a3", {"n": "a3"})],
        "dev-b": [("b1", {"n": "b1"})],
    }
    results = varsel.deliver_to_devices(
        per_device, lambda item: ({"endpoint": f"https://p.example/{item[0]}"}, item[1])
    )
    # a3 was never attempted: its device died at a2. dev-b is untouched by dev-a's fate.
    assert [(d, i[0], r) for d, i, r, _p in results] == [
        ("dev-a", "a1", "sent"), ("dev-a", "a2", "gone"), ("dev-b", "b1", "sent"),
    ]
    assert sorted(c[1] for c in calls) == ["a1", "a2", "b1"]
    assert varsel.deliver_to_devices({}, lambda item: item) == []  # no pool spun up for nothing


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


# ---- delete-all («Slett alt») + consent version -------------------------------------------------
# The privacy plan's two backend halves (docs/plans/2026-08-31-001): a one-request deletion of
# everything keyed to a device (GDPR art. 17), and a provable record of which consent text a
# registration was shown (art. 7). The delete-all endpoint is deliberately NOT delete_device:
# that one is «Skru av» (forget the push subscription, keep the journeys) and gets its own pin
# below so the two contracts cannot quietly merge.

_PAYLOAD = {
    "device_id": DEVICE,
    "from_stop": "A",
    "from_name": "Oslo S",
    "to_stop": "B",
    "to_name": "Lillehammer",
    "begin": "00:00",
    "end": "23:59",
    "days": [0, 1, 2, 3, 4, 5, 6],
}


@contextmanager
def _capture_varsel_logs():
    """Collect varsel's log records so a test can assert on the structured `fields` payloads."""
    records: list[logging.LogRecord] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collector()
    before_level = varsel.logger.level
    varsel.logger.addHandler(handler)
    varsel.logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        varsel.logger.removeHandler(handler)
        varsel.logger.setLevel(before_level)


def _events(records, name: str) -> list[dict]:
    return [f for f in (getattr(r, "fields", {}) for r in records) if f.get("event") == name]


def test_delete_all_removes_device_journeys_and_notified_state():
    """The server half of «Slett alt»: one request removes the device record, every journey the
    device registered, and the journeys' _notified dedup keys — from module state AND from the
    persisted state file, which is what a redeploy would restore (R9)."""
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    jid1 = _register_journey(DEVICE)
    jid2 = _register_journey(DEVICE)
    with varsel._state_lock:
        varsel._notified.add(f"{jid1}|S1")
        varsel._notified.add(f"{jid2}|S1")
        varsel._save_state()  # the dedup keys must demonstrably reach the state file first
    with _capture_varsel_logs() as records:
        r = client.delete(f"/varsel-api/devices/{DEVICE}/data")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "journeys_deleted": 2}
    assert client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()["journeys"] == []
    assert DEVICE not in varsel._devices
    assert not any(k.split("|", 1)[0] in {jid1, jid2} for k in varsel._notified)
    with open(_STATE_PATH) as fh:
        data = json.load(fh)
    assert DEVICE not in data["devices"]
    assert jid1 not in data["journeys"] and jid2 not in data["journeys"]
    assert not any(k.split("|", 1)[0] in {jid1, jid2} for k in data["notified"])
    # The art. 17 execution record: exactly one structured line, with the count.
    events = _events(records, "varsel_delete_all")
    assert len(events) == 1
    assert events[0]["device_id"] == DEVICE and events[0]["journeys_deleted"] == 2


def test_delete_all_leaves_other_devices_untouched():
    _reset_state()
    other = "dev-other-98765"
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    client.post("/varsel-api/devices", json={"device_id": other, "subscription": SUBSCRIPTION})
    _register_journey(DEVICE)
    other_jid = _register_journey(other)
    with varsel._state_lock:
        varsel._notified.add(f"{other_jid}|S1")
    r = client.delete(f"/varsel-api/devices/{DEVICE}/data")
    assert r.status_code == 200 and r.json()["journeys_deleted"] == 1
    assert other in varsel._devices
    listing = client.get(f"/varsel-api/journeys?device_id={other}").json()["journeys"]
    assert [j["journey_id"] for j in listing] == [other_jid]
    assert f"{other_jid}|S1" in varsel._notified


def test_delete_all_unknown_id_is_idempotent_and_does_zero_io(monkeypatch):
    """An unknown device_id answers 200 with zero deletions, twice — and the no-op path must do
    ZERO I/O: every mutation holds _state_lock across a blocking GCS upload, so an unauthenticated
    endpoint that saved state on every random UUID would be free write amplification. No log
    either — nothing happened."""
    _reset_state()
    saves = []
    monkeypatch.setattr(varsel, "_save_state", lambda: saves.append(1))
    with _capture_varsel_logs() as records:
        for _ in range(2):
            r = client.delete("/varsel-api/devices/dev-nobody-1234/data")
            assert r.status_code == 200
            assert r.json() == {"status": "ok", "journeys_deleted": 0}
    assert saves == []
    assert _events(records, "varsel_delete_all") == []


def test_delete_all_rejects_out_of_bounds_device_ids():
    """The path param mirrors the body models' device_id bounds (8..100). No charset pattern:
    the legacy fallback shape dev-<ts>-<rand> must stay legal."""
    _reset_state()
    assert client.delete(f"/varsel-api/devices/{'x' * 101}/data").status_code == 422
    assert client.delete("/varsel-api/devices/short/data").status_code == 422


def test_delete_all_without_a_device_record_still_deletes_journeys():
    """Push never enabled → no device record, but the journeys are still the user's data."""
    _reset_state()
    _register_journey(DEVICE)
    r = client.delete(f"/varsel-api/devices/{DEVICE}/data")
    assert r.status_code == 200 and r.json()["journeys_deleted"] == 1
    assert client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()["journeys"] == []


def test_delete_device_keeps_journeys():
    """«Skru av» vs «Slett alt»: the existing DELETE /devices/{id} forgets only the push
    subscription — the registered journeys survive it. First-ever pin of that contract, added
    beside delete-all so the two endpoints' semantics cannot quietly merge."""
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    jid = _register_journey(DEVICE)
    assert client.delete(f"/varsel-api/devices/{DEVICE}").status_code == 200
    assert DEVICE not in varsel._devices
    listing = client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()["journeys"]
    assert [j["journey_id"] for j in listing] == [jid]


def _age_device(device: str, minutes: int) -> None:
    """Back-date a device record past `_SUBSCRIPTION_GRACE`, so a gone result actually drops it."""
    with varsel._state_lock:
        varsel._devices[device]["created_ts"] = (
            datetime.now(UTC) - timedelta(minutes=minutes)
        ).isoformat()


def test_turning_push_off_leaves_a_tombstone_that_explains_the_kept_journeys():
    """«Skru av» keeps the journeys (previous test) and, since 2026-09-04, records WHY the record
    is gone. Without it the device report cannot tell a traveller who turned notifications off
    from one who never turned them on — and prd's orphaned journeys are mostly the latter."""
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    signup = varsel._devices[DEVICE]["created_ts"]
    _register_journey(DEVICE)
    _register_journey(DEVICE)
    assert client.delete(f"/varsel-api/devices/{DEVICE}").status_code == 200
    tomb = varsel._device_tombstones[DEVICE]
    assert tomb["reason"] == "turned_off"
    assert tomb["created_ts"] == signup
    assert tomb["journeys"] == 2
    assert tomb["ts"] >= signup


def test_an_expired_subscription_leaves_a_tombstone_with_the_expired_reason():
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    _register_journey(DEVICE)
    # Inside the grace window a gone result is treated as propagation delay and drops nothing.
    assert varsel.handle_gone_device(DEVICE) is False
    assert DEVICE in varsel._devices and DEVICE not in varsel._device_tombstones
    _age_device(DEVICE, minutes=60)
    assert varsel.handle_gone_device(DEVICE) is True
    assert DEVICE not in varsel._devices
    assert varsel._device_tombstones[DEVICE]["reason"] == "expired"
    assert varsel._device_tombstones[DEVICE]["journeys"] == 1


def test_a_record_with_no_journeys_leaves_no_tombstone():
    """Nothing to explain, so nothing is kept — or the map would accumulate an id per device that
    ever turned notifications off, forever."""
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    assert client.delete(f"/varsel-api/devices/{DEVICE}").status_code == 200
    assert DEVICE not in varsel._device_tombstones


def test_re_registering_restores_the_original_signup_and_clears_the_tombstone():
    """A lapse — «Skru av» then «Slå på varsler», or an expired subscription the service worker
    renews — must not read as a new signup on the device report."""
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    _age_device(DEVICE, minutes=60)
    signup = varsel._devices[DEVICE]["created_ts"]
    _register_journey(DEVICE)
    client.delete(f"/varsel-api/devices/{DEVICE}")
    assert DEVICE in varsel._device_tombstones
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    assert varsel._devices[DEVICE]["created_ts"] == signup
    assert DEVICE not in varsel._device_tombstones


def test_the_tombstone_goes_with_the_last_journey():
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    first = _register_journey(DEVICE)
    second = _register_journey(DEVICE)
    client.delete(f"/varsel-api/devices/{DEVICE}")
    assert client.delete(f"/varsel-api/journeys/{first}?device_id={DEVICE}").status_code == 200
    assert DEVICE in varsel._device_tombstones, "one journey is still left to explain"
    assert client.delete(f"/varsel-api/journeys/{second}?device_id={DEVICE}").status_code == 200
    assert DEVICE not in varsel._device_tombstones


def test_delete_all_erases_the_tombstone_too():
    """An art. 17 erasure keeps no marker of what it erased — and a stale marker alone is still
    worth a save, so the empty-work shortcut must not skip it."""
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    _register_journey(DEVICE)
    client.delete(f"/varsel-api/devices/{DEVICE}")
    assert DEVICE in varsel._device_tombstones
    r = client.delete(f"/varsel-api/devices/{DEVICE}/data")
    assert r.status_code == 200 and r.json()["journeys_deleted"] == 1
    assert DEVICE not in varsel._device_tombstones
    # Erasing a device that still holds its record writes none either.
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    _register_journey(DEVICE)
    client.delete(f"/varsel-api/devices/{DEVICE}/data")
    assert DEVICE not in varsel._device_tombstones and DEVICE not in varsel._devices


def test_a_failed_erasure_restores_the_tombstone_with_everything_else(monkeypatch):
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    _register_journey(DEVICE)
    client.delete(f"/varsel-api/devices/{DEVICE}")
    before = dict(varsel._device_tombstones[DEVICE])
    monkeypatch.setattr(varsel, "_save_state", lambda: False)
    assert client.delete(f"/varsel-api/devices/{DEVICE}/data").status_code == 503
    assert varsel._device_tombstones[DEVICE] == before
    assert len([j for j in varsel._journeys.values() if j["device_id"] == DEVICE]) == 1


def test_tombstones_round_trip_the_state_blob():
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    _register_journey(DEVICE)
    client.delete(f"/varsel-api/devices/{DEVICE}")
    expected = dict(varsel._device_tombstones)
    blob = varsel._serialize_state()
    assert json.loads(blob)["device_tombstones"] == expected
    varsel._device_tombstones.clear()
    varsel._apply_state(blob)
    assert varsel._device_tombstones == expected
    # A blob written before the key existed loads as "no tombstones", not as an error.
    old = json.loads(blob)
    del old["device_tombstones"]
    varsel._apply_state(json.dumps(old))
    assert varsel._device_tombstones == {}


def test_consent_text_version_is_stored_echoed_and_survives_a_reload():
    _reset_state()
    r = client.post(
        "/varsel-api/journeys", json={**_PAYLOAD, "consent_text_version": "2026-08-31.1"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["consent_text_version"] == "2026-08-31.1"
    jid = r.json()["journey_id"]
    listing = client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()["journeys"]
    assert listing[0]["consent_text_version"] == "2026-08-31.1"
    # The journey record rides the state blob wholesale, so the version must round-trip it: what a
    # redeploy restores is what proves which text this registration was shown.
    blob = varsel._serialize_state()
    with varsel._state_lock:
        varsel._journeys.clear()
    varsel._apply_state(blob)
    assert varsel._journeys[jid]["consent_text_version"] == "2026-08-31.1"


def test_consent_text_version_is_optional_for_old_clients():
    _reset_state()
    r = client.post("/varsel-api/journeys", json=_PAYLOAD)
    assert r.status_code == 200, r.text
    assert r.json()["consent_text_version"] is None


def test_consent_text_version_is_constrained():
    """Every sibling string field on JourneyIn carries bounds; an unbounded client-controlled
    string would ride the state blob on every save and land in a structured log field."""
    _reset_state()
    overlong = "2026-08-31." + "9" * 35  # matches the pattern, blows max_length=40
    r = client.post("/varsel-api/journeys", json={**_PAYLOAD, "consent_text_version": overlong})
    assert r.status_code == 422
    r = client.post(
        "/varsel-api/journeys", json={**_PAYLOAD, "consent_text_version": "not-a-version"}
    )
    assert r.status_code == 422


def test_unknown_extra_fields_on_post_are_still_ignored():
    """Pydantic's default extra='ignore' is what makes the consent rollout safe in both
    directions (old API ignores the field; new API accepts old clients) — pin that no
    model_config tightened it."""
    _reset_state()
    r = client.post("/varsel-api/journeys", json={**_PAYLOAD, "favourite_colour": "red"})
    assert r.status_code == 200, r.text


def test_registration_logs_the_consent_version_but_never_the_route_or_the_digest():
    """The journey_registered event carries consent_text_version and journey_id — never
    from_stop/to_stop or the stop names, and never the journey DIGEST.

    The digest is the point of this test. event="push" already carries device_id + journey_id and
    event="llm_message" carries the digest alone; a record pairing device_id WITH the digest would
    be the first join between the two, and it would make every varsel_decisions row (keyed on that
    same digest) device-linkable through Cloud Logging — while the privacy page this work publishes
    states the decision log «identifiserer ikke enheten din». journey_id identifies the
    registration for consent provenance without minting the cross-log join."""
    _reset_state()
    with _capture_varsel_logs() as records:
        r = client.post(
            "/varsel-api/journeys", json={**_PAYLOAD, "consent_text_version": "2026-08-31.1"}
        )
    assert r.status_code == 200, r.text
    registered = [
        record for record in records
        if getattr(record, "fields", {}).get("event") == "journey_registered"
    ]
    assert len(registered) == 1
    fields = registered[0].fields
    assert fields["consent_text_version"] == "2026-08-31.1"
    assert fields["journey_id"] == r.json()["journey_id"]
    assert fields["device_id"] == DEVICE
    assert "journey_digest" not in fields
    assert "from_stop" not in fields and "to_stop" not in fields
    assert "from_name" not in fields and "to_name" not in fields
    assert all(
        name not in record.getMessage()
        for record in registered
        for name in ("Oslo S", "Lillehammer", '"A"', '"B"')
    )


def test_save_state_reports_the_durable_copy_up_to_date_without_a_bucket():
    """No VARSEL_STATE_BUCKET means there is no durable copy to be stale — the documented
    local-only dev mode — so _save_state reports success. delete_device_data reads this return
    value, and a False here would 503 every erasure in dev and in this suite."""
    _reset_state()
    assert not varsel._STATE_BUCKET, "the suite runs bucket-less; this test is about that mode"
    assert varsel._save_state() is True


def test_save_state_reports_failure_when_a_configured_upload_fails(monkeypatch):
    """The GCS branch keeps its swallow-and-log, but it now SIGNALS. Before this the only trace of
    a failed durable write was a log line nobody reads on the request path, so «Slett alt» could
    answer 200 over a blob that still held everything."""
    _reset_state()

    class _FailingBlob:
        def upload_from_string(self, *a, **k):
            raise RuntimeError("503 backend error")

    monkeypatch.setattr(varsel, "_gcs_blob", lambda: _FailingBlob())
    assert varsel._save_state() is False
    # The local file is still written — that half stays best-effort and is unaffected.
    with open(_STATE_PATH) as fh:
        json.load(fh)


def test_delete_all_rolls_back_and_503s_when_the_durable_write_fails(monkeypatch):
    """A GDPR art. 17 erasure must not report success over a durable blob that still holds the
    data: load_state would resurrect it on the next restart, and by then the client has cleared its
    device id, so the resurrected rows are unreachable by the person they belong to.

    The ROLLBACK is what makes the retry correct. Without it the second attempt finds nothing in
    memory, takes the zero-I/O unknown-id path, and answers 200 while the blob is still full."""
    _reset_state()
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    jid1 = _register_journey(DEVICE)
    jid2 = _register_journey(DEVICE)
    with varsel._state_lock:
        varsel._notified.add(f"{jid1}|S1")
        varsel._notified.add(f"{jid2}|S1")

    real_save = varsel._save_state
    durable = {"ok": False}
    monkeypatch.setattr(varsel, "_save_state", lambda: real_save() and durable["ok"])

    with _capture_varsel_logs() as records:
        r = client.delete(f"/varsel-api/devices/{DEVICE}/data")
    assert r.status_code == 503, r.text
    assert "Prøv igjen" in r.json()["detail"]
    # Distinct, alertable event — a silently-failed erasure has to be findable in the logs.
    failures = _events(records, "varsel_delete_all_persist_failed")
    assert len(failures) == 1 and failures[0]["device_id"] == DEVICE
    assert _events(records, "varsel_delete_all") == []
    # Everything is still there: memory and the blob agree, which is what lets the retry work.
    assert DEVICE in varsel._devices
    listing = client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()["journeys"]
    assert {j["journey_id"] for j in listing} == {jid1, jid2}
    assert {f"{jid1}|S1", f"{jid2}|S1"} <= varsel._notified

    # The retry, with the durable write healthy again, deletes everything.
    durable["ok"] = True
    r = client.delete(f"/varsel-api/devices/{DEVICE}/data")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "journeys_deleted": 2}
    assert DEVICE not in varsel._devices
    assert client.get(f"/varsel-api/journeys?device_id={DEVICE}").json()["journeys"] == []
    assert not any(k.split("|", 1)[0] in {jid1, jid2} for k in varsel._notified)


def test_a_journey_deleted_mid_cycle_is_not_pushed_to(monkeypatch):
    """«Slett alt» landing inside the notification fan-out. Loop 2 captures its work items —
    subscription included — before the pool starts, so without a last look just before the send a
    push still reaches the phone after the UI said everything was deleted, and the "sent" result
    writes a _notified key for a journey that no longer exists."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    jid = _register_journey(DEVICE)

    real_deliver = varsel.deliver_to_devices

    def _delete_all_then_deliver(per_device, prepare):
        # The window: the items are collected, the erasure lands, the sends have not gone out.
        assert per_device, "the fan-out must have had something queued for this to be a test"
        assert client.delete(f"/varsel-api/devices/{DEVICE}/data").status_code == 200
        return real_deliver(per_device, prepare)

    monkeypatch.setattr(varsel, "deliver_to_devices", _delete_all_then_deliver)
    varsel.check_and_notify(SNAPSHOT)
    assert sent == []
    assert not any(k.split("|", 1)[0] == jid for k in varsel._notified)


def test_a_single_journey_deleted_mid_cycle_is_not_pushed_to(monkeypatch):
    """The other half of the same window: the device survives («Skru av» was not pressed) but the
    one journey is gone, so the re-check has to look at _journeys and not only at _devices."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    client.post("/varsel-api/devices", json={"device_id": DEVICE, "subscription": SUBSCRIPTION})
    jid = _register_journey(DEVICE)

    real_deliver = varsel.deliver_to_devices

    def _delete_journey_then_deliver(per_device, prepare):
        assert client.delete(f"/varsel-api/journeys/{jid}?device_id={DEVICE}").status_code == 200
        return real_deliver(per_device, prepare)

    monkeypatch.setattr(varsel, "deliver_to_devices", _delete_journey_then_deliver)
    varsel.check_and_notify(SNAPSHOT)
    assert sent == []
    assert DEVICE in varsel._devices
    assert not any(k.split("|", 1)[0] == jid for k in varsel._notified)


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


# ---- design review 2026-09-01: one notification per train, and a daily cap --------------------


def _cancelled_notice(situation: str) -> list:
    """A second SX message about the SNAPSHOT train, t1, now DECLARED cancelled by ET."""
    return [
        FakeCall("t1", "A", 1, DEP, situation_number=situation, summary="Innstilt",
                 et_declared_cancelled=True, p_cancel=1.0, p_impact_dep=1.0),
        FakeCall("t1", "B", 5, None, situation_number=situation, summary="Innstilt",
                 aimed_arr=DEP + timedelta(minutes=40)),
    ]


def test_a_cancellation_confirming_an_earlier_forecast_replaces_it_on_the_lock_screen(monkeypatch):
    """Finding 14: the 65 % forecast and «er innstilt» about one train arrived as two unrelated
    notifications. Both pushes now carry the same per-train tag, so the second replaces the first
    (sw.js), its title says «Bekreftet:», and it does not count against the daily cap -- it adds
    nothing to the tray."""
    _reset_state()
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    jid = _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert [p["title"] for p in sent] == [_expected_title()]
    assert varsel._daily_pushes[jid][1] == 1

    _use_snapshot({"S1": SNAPSHOT["S1"], "S2": _cancelled_notice("S2")})
    varsel.check_and_notify(_active_snapshot["value"])
    assert len(sent) == 2, "a NEW situation about the same train still reaches the phone"
    assert sent[1]["title"] == _expected_title("er innstilt", prefix="Bekreftet: ")
    assert sent[1]["tag"] == sent[0]["tag"] == f"{jid}|VYG|t1|A|1"
    assert varsel._daily_pushes[jid][1] == 1, "a replacement is not a second notification"
    _use_snapshot(SNAPSHOT)


def test_alert_pushes_per_registration_are_capped_per_day_soonest_first(monkeypatch):
    """Finding 15: six notifications in one day for three journeys, before a single real user. A
    line suspension pushes once per cancelled train in the window; the cap bounds that per
    registration and per Oslo day, spends itself on the soonest departures, records the withheld
    alerts all the same, and forgets yesterday."""
    _reset_state()
    trains = {}
    for i in range(6):
        dep = DEP + timedelta(minutes=10 * i)
        trains[f"S{i + 1}"] = [
            FakeCall(f"t{i + 1}", "A", 1, dep, situation_number=f"S{i + 1}", p_cancel=0.82,
                     p_impact_dep=0.9),
            FakeCall(f"t{i + 1}", "B", 5, None, situation_number=f"S{i + 1}",
                     aimed_arr=dep + timedelta(minutes=40)),
        ]
    _use_snapshot(trains)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    log = _use_decision_log(monkeypatch)
    jid = _register(DEVICE)
    cap = varsel._MAX_ALERT_PUSHES_PER_JOURNEY_PER_DAY
    varsel.check_and_notify(trains)
    assert len(sent) == cap == 4
    assert [p["title"] for p in sent] == [
        f"Tog R10 {DEP + timedelta(minutes=10 * i):%H:%M}"
        f"{' i morgen' if (DEP + timedelta(minutes=10 * i)).date() > datetime.now(tz=_OSLO).date() else ''}"
        " kan bli innstilt"
        for i in range(cap)
    ], "the cap is spent on the soonest departures"
    # Every decision is still recorded; the two withheld ones simply did not push.
    pushed = {r["situation_number"] for r in log.rows if r["pushed"]}
    assert pushed == {"S1", "S2", "S3", "S4"}
    assert {r["situation_number"] for r in log.rows} >= {"S5", "S6"}
    # Re-offered next cycle, still capped today ...
    varsel.check_and_notify(trains)
    assert len(sent) == cap
    # ... and delivered once the ledger says it is a new day.
    with varsel._state_lock:
        varsel._daily_pushes[jid] = ("2000-01-01", cap)
    varsel.check_and_notify(trains)
    assert len(sent) == 6
    _use_snapshot(SNAPSHOT)


# ---- U2: a partner subscription is a registration with a webhook channel (KTD3/KTD4) ----------
#
# The partner store itself is a later unit, so everything here drives the SEAM with stubs: a list
# of raw documents, a provider that raises, a provider that hangs, and a document this binary
# cannot read. That is deliberate and not a shortcut -- R20's claim is about what the PASS does
# when the store misbehaves, and a stub is the only way to produce those four states on demand.

PARTNER_DOC = {
    "recipient_id": "sub-0001",
    "from_stop": "A",
    "to_stop": "B",
    "begin": "00:00",
    "end": "23:59",
    "days": [0, 1, 2, 3, 4, 5, 6],
}
# The same document as the store's `as_recipient_document` actually writes it: the four ROUTING
# fields as well as the five the pass matches on. `PARTNER_DOC` above keeps only the matching ones
# on purpose -- it is what a document written by a binary that predates the routing fields looks
# like, and `test_a_subscription_with_no_destination_is_matched_but_never_emitted_for` is the
# assertion that such a document is still matched.
PARTNER_DOC_ROUTED = {
    **PARTNER_DOC,
    "subscription_id": "sub-0001",
    "partner_id": "partner-a",
    "destination_id": "dest-a",
    "version": 1,
    "partner_ref": "commuter-42",
    "test": False,
}


def _use_partner_store(monkeypatch, docs, *, enabled=True):
    """Wire the partner channel's two seams and return the list its provider appends a call to.

    `docs` may be a list or a zero-argument callable, so a test can make the store raise, hang or
    page. Returns the call log rather than a count, so "the store was never consulted" is an
    assertion about a list and not about a number that could also be zero by accident.
    """
    calls = []

    def provider():
        calls.append(1)
        return docs() if callable(docs) else list(docs)

    monkeypatch.setattr(varsel, "_partner_enabled", lambda: enabled)
    monkeypatch.setattr(varsel, "_partner_provider", provider)
    return calls


def _capture_writes(monkeypatch, text="Toget er innstilt. Ta neste tog."):
    """Record every subject EITHER writer is asked about, as (journey_key, situation_number).

    Both, since U6 (2026-09-08): a definition a browser registration holds goes through
    `varsel._write_message` (the browser wording policy) and a **partner-only** definition goes
    through `notification_wording.write_wording` under `notification_wording.PARTNER`, whose prompt
    audit record is suppressed in code — R16 forbids the browser policy's record for partner data,
    and dev runs `VARSEL_LLM_LOG_PROMPTS=true`.

    Stubbing only one of the two would make every partner-only assertion here read "nothing was
    written for it", which is exactly the shape of the failure these tests exist to catch. Both
    stubs return prose, so the caller caches it as the model's — `write_wording`'s `source` is what
    decides that, so `"llm"` is the value a returned text has to carry.
    """
    import notification_wording

    written = []

    def fake_write(alert, *, journey_key, message_input, **_):
        written.append((journey_key, alert.situation_number))
        return text

    def fake_partner_write(route, facts, *, subject_id="", message_input=None, **_):
        written.append((subject_id, facts.situation_number))
        return notification_wording.Wording(
            title="Tog L1 07:18 er innstilt",
            body=text,
            source="llm",
            status="ok",
            message_input=message_input or {},
        )

    monkeypatch.setattr(varsel, "_write_message", fake_write)
    monkeypatch.setattr(notification_wording, "write_wording", fake_partner_write)
    return written


def test_a_registration_with_no_device_record_is_matched_and_written_for(monkeypatch):
    """CHARACTERIZATION, taken before U2 changed anything: this shape already exists at scale.

    79 of prd's 186 registrations sit on a device id with no push subscription behind it — an
    iPhone that opened the link in Safari and never turned notifications on. `_journeys` keys the
    per-recipient ledgers, not `_devices`, so the matcher already runs for such a journey, it is
    already reported pending by `_pending_pushes` (which deliberately does not read device state),
    a wording is already written for it, and only the delivery loop skips it.

    That is exactly the shape a partner subscription has, which is why KTD3 calls one a
    registration with a channel rather than a device. U2 leaves this behaviour untouched, and this
    test is what says so: if the gate change had been written as "a live browser subscription
    exists" rather than "push is enabled", every one of those 79 registrations would silently stop
    attracting a wording and the in-app card would decay to the template for all of them.

    Verified BOTH ways on 2026-09-08, which is the only thing that makes it characterization rather
    than a description of the new code: it passes against the pre-U2 `varsel`/`main` as well as
    against this one. A characterization test only run forwards proves the new behaviour and says
    nothing about whether it changed.
    """
    _reset_state()
    log = _use_decision_log(monkeypatch)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    written = _capture_writes(monkeypatch)
    provider_calls = _count_provider_calls(monkeypatch)
    jid = _register(DEVICE)
    with varsel._state_lock:
        varsel._devices.pop(DEVICE)  # the journey stays; the device record is gone

    varsel.check_and_notify(SNAPSHOT)

    assert provider_calls, "the matcher still runs for a journey with no device record"
    assert [s for _k, s in written] == ["S1"], "and a wording is still written for it"
    assert sent == [], "but nothing is delivered -- there is no subscription to deliver to"
    assert not varsel._notified, "so no dedup key is written either; the next cycle re-offers it"
    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["pushed"] is False
    assert row["message_llm"] == "Toget er innstilt. Ta neste tog.", (
        "the wording reached the decision log, so it demonstrably exists for the card to read"
    )
    assert jid in varsel._journeys and DEVICE not in varsel._devices


def test_a_partner_subscription_matches_and_is_written_for_without_a_keypair(monkeypatch):
    """U2 scenario 1 / AE10: no VAPID keypair, and the partner channel still works.

    `push_enabled()` False is tst's real posture (it holds no keypair) and a webhook-only
    deployment's by construction. Before KTD4 this cycle wrote nothing at all and recorded
    `message_status='push_disabled'` on every row, because the writing pass was gated on the
    keypair rather than on there being a reader.

    Driven with TWO SX messages about one departure, so the collapse is exercised rather than
    assumed: `one_alert_per_train` is what makes the count-of-one meaningful.

    The EVENT half of the scenario ("produces an event") lands in U6, which owns event emission —
    U2 has no event type and deliberately does not invent one. What this asserts is everything the
    unit reaches: the match, the per-train collapse, the written wording, and that no browser
    delivery was attempted.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    written = _capture_writes(monkeypatch)
    calls = _use_partner_store(monkeypatch, [PARTNER_DOC])
    _use_snapshot(SNAPSHOT_TWO_NOTICES)

    varsel.check_and_notify(SNAPSHOT_TWO_NOTICES)

    assert calls == [1], "the store is read exactly once per cycle, before pass 1a"
    assert len(written) == 1, f"one wording per TRAIN, not per SX message; got {written}"
    assert written[0][0] == "A|B|00:00|23:59|0,1,2,3,4,5,6", "keyed on the journey DEFINITION"
    assert varsel._messages, "and cached, which is what U6's event will carry"
    assert sent == [], "no keypair means no browser delivery was even attempted"


def test_a_browser_and_a_partner_registration_share_one_match_and_one_wording(monkeypatch):
    """U2 scenario 2 / AE1: one definition, two channels, ONE match and ONE wording.

    Asserted BY CALL COUNT, which is the only way to assert it: comparing the two channels' outputs
    would pass just as happily if the pass had matched twice and produced two identical answers, and
    the cost R3 buys is precisely that it does not. A partner subscription costs this pass no
    /impact call and no Vertex call of its own when a browser registration already covers its
    definition; what it costs is one more entry in a list.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    written = _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC])
    _register(DEVICE)
    # The counters go on AFTER registration, deliberately: `POST /journeys` annotates the created
    # journey by running the matcher on the request path, so counting from before it would charge
    # this cycle a call the cycle never made.
    matches = []
    real_match = varsel.match_journey

    def counting_match(journey, *a, **kw):
        matches.append(varsel.journey_key(journey))
        return real_match(journey, *a, **kw)

    monkeypatch.setattr(varsel, "match_journey", counting_match)
    provider_calls = _count_provider_calls(monkeypatch)

    varsel.check_and_notify(SNAPSHOT)

    key = "A|B|00:00|23:59|0,1,2,3,4,5,6"
    assert matches == [key], f"exactly one matcher call between the two channels; got {matches}"
    assert written == [(key, "S1")], f"and exactly one written wording; got {written}"
    # The verdicts are identical because there is only one set of them: both channels read the same
    # `alerts_by_key` entry, which is what makes "identical verdicts" structural rather than checked.
    assert len(varsel._messages) == 1
    assert len(provider_calls) == len(
        varsel.journey_windows(next(iter(varsel._journeys.values())), datetime.now(UTC))
    ), "one /impact call per boarding window of the ONE group, not two groups' worth"


def test_only_the_train_a_partner_still_needs_is_written_for_without_a_keypair(monkeypatch):
    """U2 scenario 3, and the half of KTD4 that is easy to miss and expensive to get wrong.

    Two disrupted departures on one definition, no keypair, and a partner subscription that has
    already been delivered S1. The wording budget must go to S3 and to nothing else.

    Both halves of the move are under test here. If the gate had been changed WITHOUT withholding
    the browser jids from `_pending_pushes`, the browser registration would report BOTH trains
    pending on every cycle — `_notified` only grows on a successful browser send, and there can
    never be one here — so this cycle would write for two trains it can never deliver, on every
    tick, competing with the one subject the partner is actually waiting for. And if the pending
    set had not been extended to partner recipients at all, S3 would fall through to the
    worst-train fallback, which picks S1 (the more severe) and hands the traveller the template for
    the train they still need to hear about.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    written = _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [{**PARTNER_DOC, "delivered": ["S1"]}])
    _register(DEVICE)  # a browser registration on the same definition, undeliverable here
    _use_snapshot(SNAPSHOT_TWO_DISRUPTED)

    varsel.check_and_notify(SNAPSHOT_TWO_DISRUPTED)

    assert [s for _k, s in written] == ["S3"], (
        f"only the train the partner is still owed; got {written}"
    )


def test_a_keypair_less_cycle_writes_nothing_once_the_partner_has_been_told(monkeypatch):
    """The other side of the same rule: nothing pending on any channel means no write.

    A browser registration is present and disrupted, and it would report pending forever on this
    deployment. It contributes nothing, because the browser channel cannot deliver at all here —
    which is what stops a keypair-less deployment spending its whole budget re-wording trains that
    reach nobody.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    written = _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [{**PARTNER_DOC, "delivered": ["S1"]}])
    _register(DEVICE)

    for _ in range(3):  # three poller ticks on one live situation
        varsel.check_and_notify(SNAPSHOT)

    assert written == [], f"no deliverable recipient on any channel; got {written}"


def test_no_deliverable_recipient_on_any_channel_spends_no_vertex_call(monkeypatch):
    """U2 scenario 4: a keypair-less deployment with the partner flag unset writes nothing.

    This is the case every deployment is in today, and the one the old `if not push_on: continue`
    got right. The gate had to keep it right while widening: the condition is now "a recipient on
    some channel can receive this", and with no keypair and no partner store there is none.

    `_generation_enabled` is asserted never to be consulted either, because that is where the spend
    would begin — a cycle that reaches the writer's gate has already decided to write.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    log = _use_decision_log(monkeypatch)
    written = _capture_writes(monkeypatch)
    asked = []
    monkeypatch.setattr(
        varsel, "_generation_enabled", lambda: asked.append(1) or True
    )
    _register(DEVICE)

    varsel.check_and_notify(SNAPSHOT)

    assert written == [] and asked == [], "no reader, no spend"
    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["message_status"] == "push_disabled", (
        "and `push_disabled` keeps its meaning for a browser subject -- every row this table gets "
        "is one (KTD10 keeps partner subjects out), so the value still says exactly why"
    )


def test_the_partner_flag_unset_never_touches_the_store(monkeypatch):
    """U2 scenario 5: with the flag unset the pass makes no store call at all.

    Enforced in `varsel`, not in whoever wrote the provider: the flag is injected as its own seam
    precisely so "the flag is off, so nothing was read" is a property of the pass. Both shapes of
    off are checked — the flag reading False, and a flag reader that raises, which must also read
    as off (the failure direction of a delivery switch is closed).
    """
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    # The WRITER too, and not because this test cares what it says: push is enabled here, so the
    # cycle reaches pass 1c, and nothing in this suite structurally fences `_write_message` the way
    # `alternatives._post_json` fences the Journey Planner. Left unstubbed it makes a real Vertex
    # call from CI -- two, one per backend, before the latch closes.
    _capture_writes(monkeypatch)
    _register(DEVICE)

    calls = _use_partner_store(monkeypatch, [PARTNER_DOC], enabled=False)
    varsel.check_and_notify(SNAPSHOT)
    assert calls == [], "the flag is off, so the store must not be consulted"
    assert varsel.partner_channel_enabled() is False

    def boom():
        raise RuntimeError("the flag could not be read")

    monkeypatch.setattr(varsel, "_partner_enabled", boom)
    assert varsel.partner_channel_enabled() is False, "a raising gate reads as OFF"
    varsel.check_and_notify(SNAPSHOT)
    assert calls == []
    # And with no provider wired at all -- every deployment until the store lands.
    monkeypatch.setattr(varsel, "_partner_enabled", lambda: True)
    monkeypatch.setattr(varsel, "_partner_provider", None)
    assert varsel.partner_channel_enabled() is False


def test_a_broken_partner_store_costs_the_partner_channel_and_nothing_else(monkeypatch):
    """U2 scenario 7 / AE14: the store raises, hangs, or hands over one unreadable document.

    Three shapes in one test because they share the whole assertion: browser matching, wording,
    delivery, decision recording and state persistence all complete, and only the partner channel's
    own contribution is lost. Called bare instead of guarded, a raise here unwinds out of
    `check_and_notify` entirely — loop 2 never runs, so no push at all goes out, and
    `_record_decisions` never runs, so the decision log gains a hole no later cycle backfills.
    """
    import threading
    from time import monotonic

    # --- 1. the store raises -------------------------------------------------------------------
    _reset_state()
    log = _use_decision_log(monkeypatch)
    sent = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    written = _capture_writes(monkeypatch)

    def raising():
        raise RuntimeError("Firestore is unreachable")

    calls = _use_partner_store(monkeypatch, raising)
    _register(DEVICE)
    with _capture_varsel_logs() as records:
        varsel.check_and_notify(SNAPSHOT)
    assert calls == [1], "it was tried"
    assert len(sent) == 1 and written and log.rows, "and the browser cycle completed regardless"
    assert varsel._notified, "including the state the save persists"
    assert [e["result"] for e in _events(records, "varsel_partner_load")] == ["error"]

    # --- 2. the store hangs --------------------------------------------------------------------
    # A real bound, not a mocked clock: the provider parks for `held` and the load is allowed 50 ms.
    # The cycle must come back long before the provider does, which is the property KTD6 asks for --
    # the poller thread is what ingestion for every other feature runs on.
    _reset_state()
    release = threading.Event()
    monkeypatch.setattr(varsel, "_PARTNER_LOAD_TIMEOUT_S", 0.05)
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    sent.clear()
    written = _capture_writes(monkeypatch)

    def hanging():
        release.wait(30)  # bounded so a failing assertion cannot wedge the suite
        return [PARTNER_DOC]

    _use_partner_store(monkeypatch, hanging)
    _register(DEVICE)
    started = monotonic()
    try:
        varsel.check_and_notify(SNAPSHOT)
    finally:
        release.set()  # let the abandoned worker finish rather than leaking it into the next test
    elapsed = monotonic() - started
    assert elapsed < 5.0, f"a hanging store must not hold the poller thread; took {elapsed:.1f}s"
    assert len(sent) == 1 and written, "and the browser cycle still completed"

    # --- 3. one unreadable document among readable ones ----------------------------------------
    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    written = _capture_writes(monkeypatch)
    _use_partner_store(
        monkeypatch,
        [
            {"recipient_id": "sub-broken"},  # no window, no stops: this binary cannot read it
            {**PARTNER_DOC, "recipient_id": "sub-good"},
        ],
    )
    with _capture_varsel_logs() as records:
        varsel.check_and_notify(SNAPSHOT)
    assert [s for _k, s in written] == ["S1"], (
        "the readable subscription was matched and written for: one bad document must never fail "
        "its collection (R20)"
    )
    loads = _events(records, "varsel_partner_load")
    assert next(e for e in loads if e["result"] == "skipped")["skipped"] == 1
    assert next(e for e in loads if e["result"] == "ok")["n"] == 1
    # R16: the count and the exception class are logged, the document never is.
    assert not any("sub-broken" in str(getattr(r, "msg", "")) for r in records)


def test_a_cycle_with_partner_recipients_writes_a_byte_identical_state_blob(monkeypatch):
    """U2 scenario 8, and KTD3's central invariant made testable rather than claimed.

    `_journeys`, `_notified` and the GCS state blob never carry a partner id: the partner view is
    merged at match time and thrown away with the cycle. That is what makes a rollback to
    pre-feature code safe — `_save_state` writes known keys, so `_state_extra`'s round-trip could
    not have protected partner state that had leaked into the blob, and a rolled-back binary would
    silently erase it on its first save.

    Both cycles start from the SAME serialized state (restored through `_apply_state`), because a
    fresh registration mints a new `journey_id` and the two blobs would then differ for a reason
    having nothing to do with partners.

    U2's scenario says "matched **and delivered**". Partner DELIVERY is U6's — this unit has no
    sender and deliberately invents none — so what is asserted here is a cycle in which the
    subscriptions matched, earned a wording and had the browser channel deliver and save beside
    them. U6 re-asserts the same equality across a cycle that also delivered a webhook, which is
    where a partner id would most plausibly be written into the blob (its dedup ledger is
    Firestore, and the temptation is to mirror it into `_notified`).
    """
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    _capture_writes(monkeypatch)
    _register(DEVICE)
    baseline = varsel._serialize_state()

    def one_cycle() -> str:
        varsel._apply_state(baseline)
        with varsel._state_lock:
            varsel._pushed_trains.clear()  # not in the blob, but it gates the daily cap
            varsel._daily_pushes.clear()
            varsel._messages.clear()
        varsel.check_and_notify(SNAPSHOT)
        return varsel._serialize_state()

    _use_partner_store(
        monkeypatch,
        [PARTNER_DOC, {**PARTNER_DOC, "recipient_id": "sub-0002", "delivered": ["S1"]}],
    )
    with_partners = one_cycle()
    assert "S1" in with_partners, "the cycle must really have delivered, or this proves nothing"

    monkeypatch.setattr(varsel, "_partner_provider", None)
    without_partners = one_cycle()

    assert with_partners == without_partners
    assert "sub-000" not in with_partners, "no partner id may reach the blob"
    assert varsel._STATE_KEYS == frozenset(
        {"devices", "journeys", "notified", "togvarsel_rounds", "device_tombstones"}
    ), "the blob's key set is unchanged: the partner channel persists nothing here"
    assert not any("sub-000" in json.dumps(rec) for rec in varsel._journeys.values())


def test_a_partner_only_route_records_no_decision_row(monkeypatch):
    """KTD10, and the limitation KTD12 records rather than hides.

    `varsel_decisions` is published on public hosts, with resolved origin and destination names
    since 2026-08-22 — accepted then because prd's registrations are the team's own test users.
    Partner-app travellers are members of the public, so partner subjects stay out of that table
    entirely, which `_record_decisions` gets for free by iterating the BROWSER grouping.

    The cost is real and is the reason this is a test rather than a comment: a partner-only route
    produces no row at all, so `/historikk` scores the definitions both channels share and nothing
    else. Scoring partner-only routes is deferred work; the enablement gate is conditional on
    `/historikk`'s per-operator verdicts with that gap written down.
    """
    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    written = _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC])  # and NO browser registration

    varsel.check_and_notify(SNAPSHOT)

    assert [s for _k, s in written] == ["S1"], "the subscription was matched and written for"
    assert log.rows == [], "and recorded nowhere in varsel_decisions"


# ---- U6: the pass outcome -- what the partner phase is handed --------------------------------


def test_two_situations_about_one_train_yield_one_partner_subject_carrying_both(monkeypatch):
    """U6 scenario 1 / AE2, on the varsel side: one TRAIN is one subject, not one per SX message.

    Operators routinely publish several situations about one disruption — Vy sent both "Ta neste
    tog" and "Innstilt Drammen–Skøyen" for the same cancelled 15:03, which is what put "91 %" and
    "87 %" for one train two boxes apart on a card in 2026-08. `one_alert_per_train` collapses them
    for every traveller-facing surface, and the partner channel is one.

    **Both situation numbers ride on the subject**, and that half is easy to miss and expensive:
    `partner_store._sent_body` writes a delivered mark per situation number, and
    `_pending_pushes` tests the collapsed WINNER's number against those marks. Carrying only the
    winner's would let a winner flip between cycles re-pend a train already delivered and re-spend
    the wording budget on it. R8 calls them "the contributing situations" for the same reason.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC_ROUTED])
    _use_snapshot(SNAPSHOT_TWO_NOTICES)

    outcome = varsel.check_and_notify(SNAPSHOT_TWO_NOTICES)

    assert len(outcome.partner_subjects) == 1, (
        f"one subject per TRAIN, not per SX message; got {outcome.partner_subjects}"
    )
    subject = outcome.partner_subjects[0]
    assert set(subject.situations) == {"S1", "S2"}, subject.situations
    assert [s["situation_number"] for s in subject.payload["situations"]] == list(subject.situations)
    # The subject carries the routing the emit step needs, from the LOAD snapshot's version.
    assert subject.subscription_id == "sub-0001" and subject.subscription_version == 1
    assert subject.alertworthy is True


def test_a_partner_subject_carries_byte_identical_wording_to_the_web_push(monkeypatch):
    """R7: "the Norwegian title and body /varsel would have pushed", asserted byte for byte.

    Both go through `varsel.notification_wording_for`, which is what makes this true by
    construction rather than by two f-strings that agree today: the route on the body's own first
    line, the environment tag on the title only, and the wording itself untouched.

    **Two documented divergences, and both are deliberate.** The ROUTE's names: a browser
    registration supplies `from_name`/`to_name`, and a partner-only route has nobody to have typed
    them, so the event falls back to the stop registry's. And the WORDING, while the two delivery
    switches disagree — the rollout turns the partner channel on with the template first
    (`NOTIFICATION_PARTNER_LLM_ENABLED` unset) while the PWA is already receiving the model's prose,
    so during that window the event carries the template and the push does not. Both switches are
    on below, which is the state R7's claim is about, and the second half of this test asserts the
    divergence rather than pretending it does not exist.
    """
    import notification_wording

    _reset_state()
    sent: list[dict] = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: sent.append(payload) or "sent")
    monkeypatch.setattr(notification_wording, "partner_delivery_enabled", lambda: True)
    _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC_ROUTED])
    _register(DEVICE)

    outcome = varsel.check_and_notify(SNAPSHOT)

    assert len(sent) == 1 and len(outcome.partner_subjects) == 1
    subject = outcome.partner_subjects[0]
    assert subject.title == sent[0]["title"], (subject.title, sent[0]["title"])
    assert subject.body == sent[0]["body"], (subject.body, sent[0]["body"])
    assert subject.body.startswith("Oslo S → Lillehammer\n"), subject.body
    assert subject.payload["notification"] == {
        "title": subject.title,
        "body": subject.body,
        "action": None,
    }

    # The other side: with the partner switch OFF the event carries the TEMPLATE while the push
    # carries the model's prose. Asserted so the rollout window is a checked property rather than a
    # surprise the first partner reports.
    _reset_state()
    sent.clear()
    monkeypatch.setattr(notification_wording, "partner_delivery_enabled", lambda: False)
    _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC_ROUTED])
    _register(DEVICE)
    outcome = varsel.check_and_notify(SNAPSHOT)
    assert "Toget er innstilt. Ta neste tog." in sent[0]["body"]
    assert "Toget er innstilt. Ta neste tog." not in outcome.partner_subjects[0].body
    assert outcome.partner_subjects[0].title == sent[0]["title"], "the TITLE never varies"


def test_a_below_threshold_train_carries_a_retraction_wording_not_the_alert_one(monkeypatch):
    """Scenario 7's wording half: a withdrawal must not lead with the disruption it retracts.

    "Withdrawal means the forecast changed, not that normal service is guaranteed" — the store's own
    `record_below_threshold` makes that the wording's job, and it is the one sentence a traveller
    could most easily read as an all-clear. A retraction worded from the alert template would open
    "82 % sjanse for innstilling", which is the opposite of what it means.

    The train is still named exactly as every other notification names it, because the retraction is
    about a specific departure and a partner has to be able to match it to the alert it replaces.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC_ROUTED])
    # A snapshot whose one call is well below the alert gate.
    mild = {
        "S1": [
            FakeCall("t1", "A", 1, DEP, p_cancel=0.02, p_impact_dep=0.05),
            FakeCall("t1", "B", 5, None, aimed_arr=DEP + timedelta(minutes=40)),
        ]
    }
    _use_snapshot(mild)

    outcome = varsel.check_and_notify(mild)

    assert len(outcome.partner_subjects) == 1
    subject = outcome.partner_subjects[0]
    assert subject.alertworthy is False
    assert varsel._PARTNER_WITHDRAWAL_TITLE in subject.title, subject.title
    # `A → B`, the raw refs, because a partner-ONLY route has no traveller-supplied name and this
    # suite's stop registry resolves neither. In prd the registry supplies both; the structure --
    # the route on its own first line, then the retraction -- is what has to hold either way.
    assert subject.body == f"A → B\n{varsel._PARTNER_WITHDRAWAL_DETAIL}", subject.body
    assert "innstilling" not in subject.body and "%" not in subject.body


def test_a_declared_cancellation_carries_no_delay_figures_in_its_payload(monkeypatch):
    """The three-way split every writer in this module makes, applied to the event payload.

    When ET has already declared the call cancelled the probabilities are 1.0 by construction
    (`impact._cancelled_prediction`) and every delay figure meant "if it runs" — so `delay` is
    null, exactly as `event.schema.json` states for `phase: confirmed` and exactly as
    `_deterministic_body` and the message input already do it.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC_ROUTED])
    cancelled = {"S1": _cancelled_notice("S1")}
    _use_snapshot(cancelled)

    outcome = varsel.check_and_notify(cancelled)

    [subject] = outcome.partner_subjects
    assert subject.cancelled is True
    assert subject.payload["delay"] is None
    assert subject.payload["probabilities"]["cancellation"] == 1.0
    assert subject.payload["effect"] == "cancelled"


def test_a_partner_delivery_can_never_enter_pushed_keys(monkeypatch):
    """U6 scenario 5 / KTD10's second leak, closed structurally rather than by care.

    `pushed_keys` is journey-DEFINITION grain, so a partner delivery recorded there would mark a
    browser registration's row `pushed` with no browser push behind it — and
    `varsel_decision_groups` picks the wording /historikk quotes off the FIRST pushed row, so the
    page would quote a wording nobody received. That is the 2026-08-21 defect, re-created from the
    other side.

    What makes it impossible is WHERE the delivery happens: `check_and_notify` returns before any
    partner event is sent, so there is no moment at which a partner outcome could reach that set.
    Asserted both ways — a shared definition with push off records `pushed=False` while the partner
    subject exists, and the source of the delivery module names neither symbol.
    """
    _reset_state()
    log = _use_decision_log(monkeypatch)
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC_ROUTED])
    _register(DEVICE)  # a browser registration on the SAME definition

    outcome = varsel.check_and_notify(SNAPSHOT)

    assert outcome.partner_subjects, "the partner subject exists"
    row = next(r for r in log.rows if r["situation_number"] == "S1")
    assert row["pushed"] is False, (
        "the browser row must not read `pushed` on the strength of a partner delivery"
    )
    import pathlib

    text = (pathlib.Path(varsel.__file__).parent / "partner_delivery.py").read_text()
    assert "pushed_keys" not in text and "import varsel" not in text


def test_the_decision_log_and_the_state_blob_are_written_before_the_outcome_is_built(monkeypatch):
    """U6 scenario 8: a failure in the partner work cannot cost the browser cycle's durable writes.

    KTD6 orders every partner step that writes or sends after `_record_decisions` and `_save_state`.
    Building the outcome LAST and handing it back is strictly stronger on KTD6's own stated reason:
    the emit and delivery steps run after this function has returned, so a raise in them cannot
    unwind either write at all. This asserts the weaker half that is still checkable here — a raise
    while BUILDING the subjects leaves both writes intact and the cycle reports no subjects.
    """
    _reset_state()
    log = _use_decision_log(monkeypatch)
    saves: list[int] = []
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    monkeypatch.setattr(varsel, "_save_state", lambda: saves.append(1) or True)
    _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC_ROUTED])
    _register(DEVICE)

    import pathlib

    def raising(*_a, **_k):
        raise RuntimeError("a duck-typed payload bit back")

    monkeypatch.setattr(varsel, "_partner_payload", raising)
    with _capture_varsel_logs() as records:
        outcome = varsel.check_and_notify(SNAPSHOT)

    assert outcome.partner_subjects == (), "no partner work this cycle"
    assert log.rows, "and the decision log was still written"
    assert saves, "and the state blob was still saved"
    # Caught PER DEFINITION, not by the outer backstop: one journey's awkward payload must cost
    # that journey's subjects rather than every other partner's events in the same cycle.
    results = [e["result"] for e in _events(records, "partner_emit")]
    assert results == ["subject_error", "subjects_skipped"], results
    assert _events(records, "partner_emit")[1]["n"] == 1
    # And the outer guard is still there as the backstop for a raise the loop itself cannot reach.
    assert '"result": "build_error"' in pathlib.Path(varsel.__file__).read_text()


def test_a_subscription_with_no_destination_is_matched_but_never_emitted_for(monkeypatch):
    """R20's per-document rule applied to the four ROUTING fields, one layer past the loader.

    The loader reads them permissively on purpose: the fields it REFUSES on are the ones the pass
    matches with, and a document missing a destination can still be matched — it still collapses
    onto its journey key and still shares the one /impact call and the one wording AE1 asserts.
    Refusing it would take a browser registration's own wording away over a partner field. So it is
    matched and simply not emitted for, and the COUNT is logged (never the document — R16).
    """
    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    written = _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC])  # no partner_id, no destination_id

    with _capture_varsel_logs() as records:
        outcome = varsel.check_and_notify(SNAPSHOT)

    assert [s for _k, s in written] == ["S1"], "matched and written for"
    assert outcome.partner_subjects == (), "and not emitted for"
    assert _events(records, "partner_emit")[0]["result"] == "unroutable"


def test_a_partner_only_definition_is_written_for_through_the_partner_policy(monkeypatch):
    """The R16 exposure U2 left open, and the half of it U6 closes.

    A `journey_key` reaching `varsel._write_message` reaches `llm_message.write_message_detailed`,
    which emits `event="llm_message"` — and under `VARSEL_LLM_LOG_PROMPTS=true`, which dev runs, the
    full prompt at DEBUG, carrying the traveller's origin, destination and departure time. R16
    forbids that for partner data, so a definition NO browser registration holds is routed through
    `notification_wording.PARTNER`, which suppresses that record in code rather than by an
    environment variable.

    The discriminator is `by_journey_key`, which U2 kept browser-only precisely so this question has
    a cheap answer. The shared case is unavoidable and by design — AE1 pins one match and one
    wording between the channels, so there is one call and it is the browser's — and this asserts
    both halves, because asserting only the partner-only one would pass just as happily if every
    subject went through PARTNER and AE1 were quietly false.
    """
    import notification_wording

    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    browser_calls: list[str] = []
    partner_calls: list[str] = []
    monkeypatch.setattr(
        varsel,
        "_write_message",
        lambda alert, *, journey_key, message_input, **_: browser_calls.append(journey_key)
        or "Skrevet av modellen.",
    )

    def partner_write(route, facts, *, subject_id="", message_input=None, **_):
        partner_calls.append(subject_id)
        return notification_wording.Wording(
            title="t", body="b", source="llm", status="ok", message_input=message_input or {}
        )

    monkeypatch.setattr(notification_wording, "write_wording", partner_write)

    # 1. partner-ONLY: the PARTNER policy, and the browser writer is never reached.
    _use_partner_store(monkeypatch, [PARTNER_DOC_ROUTED])
    varsel.check_and_notify(SNAPSHOT)
    assert partner_calls and not browser_calls, (partner_calls, browser_calls)

    # 2. SHARED: the browser writer, once, and the partner writer not at all.
    _reset_state()
    partner_calls.clear()
    browser_calls.clear()
    _use_partner_store(monkeypatch, [PARTNER_DOC_ROUTED])
    _register(DEVICE)
    varsel.check_and_notify(SNAPSHOT)
    assert len(browser_calls) == 1 and not partner_calls, (browser_calls, partner_calls)


def test_the_partner_wording_gate_is_the_partner_switch_not_varsel_llm_enabled(monkeypatch):
    """The two delivery switches stay separate, which is the rollout order's whole point.

    "Enable the partner flag in dev first, with deterministic wording, then model wording once
    matching and delivery qualify." Sharing `VARSEL_LLM_ENABLED` would flip partner prose on the
    moment it goes on for the PWA, in the same deploy — precisely the coupling the 2026-08-07
    generation/delivery split removed one layer down.

    Read ONCE per cycle, like `_record_decisions`' own two gate reads: a flag re-read per subject
    could write for one train and not the next inside one pass.
    """
    import notification_wording

    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC_ROUTED])
    _register(DEVICE)

    # Partner prose OFF (the default everywhere today): the event carries the template.
    monkeypatch.setattr(notification_wording, "partner_delivery_enabled", lambda: False)
    outcome = varsel.check_and_notify(SNAPSHOT)
    template_body = outcome.partner_subjects[0].body
    assert "Toget er innstilt. Ta neste tog." not in template_body

    # Partner prose ON: the same cached wording the browser card would show.
    monkeypatch.setattr(notification_wording, "partner_delivery_enabled", lambda: True)
    outcome = varsel.check_and_notify(SNAPSHOT)
    assert "Toget er innstilt. Ta neste tog." in outcome.partner_subjects[0].body

    # And a switch that raises reads as OFF -- the failure direction of a delivery gate is closed.
    def boom() -> bool:
        raise RuntimeError("unreadable")

    monkeypatch.setattr(notification_wording, "partner_delivery_enabled", boom)
    outcome = varsel.check_and_notify(SNAPSHOT)
    assert "Toget er innstilt. Ta neste tog." not in outcome.partner_subjects[0].body


def test_the_write_budgets_clock_is_shared_and_its_call_allowance_is_not(monkeypatch):
    """`notification_wording.PassBudget`'s stated split, at the one call site that has to honour it.

    The CLOCK is a property of the pass — two surfaces writing inside one pass cannot each have
    their own, and the reservation is what makes the deadline a ceiling on when the pass ENDS. The
    COUNT is a property of the surface: merged, a partner with a few hundred subscriptions would
    spend `_MAX_LLM_CALLS_PER_CYCLE` on partner wordings and silently retire the browser ones, which
    is that constant's own lesson read the other way round.
    """
    budget = varsel._WriteBudget(1, varsel.monotonic() + 3600)
    assert budget.claim_clock() is True and budget.claimed == 0, "the clock only"
    assert budget.claim() is True and budget.claimed == 1
    assert budget.claim() is False, "the count binds for the browser surface"
    assert budget.claim_clock() is True, "and not for the partner one"

    spent = varsel._WriteBudget(9, varsel.monotonic() - 1)
    assert spent.claim_clock() is False, "an expired clock binds for BOTH"
    assert spent.claim() is False


def test_the_locked_pass_budget_is_safe_across_the_write_pool(monkeypatch):
    """`PassBudget` documents itself as not thread-shared, and pass 1c is `_LLM_WORKERS`-wide.

    `self.left -= 1` is a read-modify-write and the GIL does not make it atomic, so unlocked the
    ceiling could be overspent by up to one call per worker. Driven from more threads than the limit
    allows, the number of successful takes must be exactly the limit.
    """
    import threading

    budget = varsel._LockedPassBudget(5)
    taken: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        got = budget.take()
        with lock:
            taken.append(got)

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(taken) == 5, f"exactly the limit, got {sum(taken)}"


def test_a_partner_subscription_is_invisible_on_every_varsel_surface(monkeypatch):
    """U2 scenario 6 for the /varsel side, and R16: nothing about a partner reaches the PWA.

    A partner subscription is a view merged inside `check_and_notify` and thrown away with the
    cycle (KTD3), so the journey list, the state file and the device registry must all read exactly
    as they would with no partner channel at all. Asserted after a cycle in which the subscription
    genuinely matched and earned a wording, since that is the only interesting moment: a view that
    contributed nothing could not leak anything either.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "push_enabled", lambda: False)
    written = _capture_writes(monkeypatch)
    _use_partner_store(monkeypatch, [PARTNER_DOC])

    varsel.check_and_notify(SNAPSHOT)

    assert written, "the subscription really was matched and written for"
    assert varsel._journeys == {} and varsel._devices == {}
    assert varsel._notified == set()
    listing = client.get("/varsel-api/journeys", params={"device_id": DEVICE})
    assert listing.status_code == 200 and listing.json()["journeys"] == []
    # /config is the PWA's only other read of module state, and it says nothing about channels.
    # What this is about, and not the exact key set: an unrelated future field would otherwise
    # fail this test with a message about partners, which is the worst kind of failing assertion.
    assert not any(
        "partner" in key for key in client.get("/varsel-api/config").json()
    ), "the PWA's config gains no partner field: the channel is not its business"


def test_a_partner_document_naming_a_collapsed_stop_ref_groups_with_the_browser_journey(
    monkeypatch,
):
    """The 2026-08-03 picker bug, one layer out — and it would have been silent.

    `_canonicalize_journeys` only ever walks `_journeys`, so nothing would have rewritten a partner
    document naming a ref the stop registry has since collapsed onto a primary. Left alone it
    produces a DIFFERENT `journey_key` for the identical physical trip: two groups, two /impact
    calls, two wordings, and AE1's claim quietly false with every assertion about a single channel
    still passing.
    """
    _reset_state()
    monkeypatch.setattr(varsel, "_send_push", lambda sub, payload: "sent")
    written = _capture_writes(monkeypatch)
    varsel.set_stop_aliases({"A2": "A"})  # A2 is the same physical place as A, collapsed onto A
    try:
        _use_partner_store(monkeypatch, [{**PARTNER_DOC, "from_stop": "A2"}])
        _register(DEVICE)
        varsel.check_and_notify(SNAPSHOT)
    finally:
        varsel.set_stop_aliases({})

    assert written == [("A|B|00:00|23:59|0,1,2,3,4,5,6", "S1")], (
        f"one group, one wording, keyed on the canonical refs; got {written}"
    )


def test_the_partner_loader_refuses_exactly_what_the_registration_api_refuses():
    """R2: a partner subscription is `/varsel`'s registration shape, including the refusals.

    The loader restates `JourneyIn`'s three window rules rather than importing the model — a
    partner subscription has no `device_id`, and a load step on the ingestion thread must be
    indifferent to fields a NEWER binary added, which a model with a growing required set is not.
    That restatement is exactly the kind of duplicate that drifts into being looser, so the two are
    pinned against each other here: the same shapes, the same verdicts.
    """
    import pydantic

    cases = [
        ({}, True),                                   # the valid baseline
        ({"begin": "24:00"}, False),
        ({"end": "7:5"}, False),
        ({"begin": "10:00", "end": "09:00"}, False),  # overnight windows are refused, not wrapped
        ({"begin": "10:00", "end": "10:00"}, False),
        ({"days": []}, False),
        ({"days": [7]}, False),
        ({"days": [-1]}, False),
        ({"to_stop": "A"}, False),                    # from == to
    ]
    for overrides, want_ok in cases:
        doc = {**PARTNER_DOC, **overrides}
        loader_ok = True
        try:
            varsel._parse_partner_recipient(doc)
        except (KeyError, TypeError, ValueError) as exc:
            # The three classes a malformed document can reach here; anything else is a defect in
            # the loader rather than a bad document, and this test must not paper over it.
            loader_ok = False
            assert str(exc), overrides
        api_ok = True
        try:
            varsel.JourneyIn(device_id="dev-abc12345", **{k: v for k, v in doc.items()
                                                          if k != "recipient_id"})
        except pydantic.ValidationError:
            api_ok = False
        assert loader_ok is want_ok, f"loader disagreed on {overrides}"
        assert api_ok is want_ok, f"JourneyIn disagreed on {overrides}"

    # A field the running binary does not know is IGNORED, not refused: the store will grow columns
    # (a destination, a quota, a test marker) and a load step that rejected unknown keys would stop
    # matching the whole collection on the deploy that adds one.
    kept = varsel._parse_partner_recipient({**PARTNER_DOC, "some_future_field": {"a": 1}})
    assert set(kept.journey) == {"from_stop", "to_stop", "begin", "end", "days"}
    assert kept.recipient_id == "sub-0001" and kept.delivered == frozenset()


def test_the_partner_read_is_bounded_by_a_document_ceiling():
    """R21: the per-cycle read cannot exceed the ceiling, and the ceiling binds on the PAGING.

    `islice` is applied inside the worker thread, so a lazy cursor stops fetching at the ceiling
    rather than being fetched whole and then trimmed — which is what makes this a bound on the read
    and not merely on the result. Anything approaching it in a pilot is a store bug or a quota
    breach, so truncating is the right failure and it is reported.
    """
    fetched = []

    def endless():
        for i in range(varsel._MAX_PARTNER_RECIPIENTS + 500):
            fetched.append(i)
            yield {**PARTNER_DOC, "recipient_id": f"sub-{i}"}

    before = (varsel._partner_enabled, varsel._partner_provider)
    varsel._partner_enabled, varsel._partner_provider = (lambda: True), endless
    try:
        loaded = varsel._load_partner_recipients()
    finally:
        varsel._partner_enabled, varsel._partner_provider = before
    assert len(loaded) == varsel._MAX_PARTNER_RECIPIENTS
    assert len(fetched) == varsel._MAX_PARTNER_RECIPIENTS, (
        "the cursor must stop at the ceiling, not be drained and then trimmed"
    )


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
