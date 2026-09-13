"""Checks for U14's cutover: the one-shot import into the store, and the export back out of it.

Hermetic and standalone: the blob is a temp file, the store is `test_notification_store`'s fake
client running the REAL transaction bodies, and nothing here touches GCS, Firestore or the network.

What this file is for is not that the import copies rows. It is that the import cannot do the four
things whose failure nobody would notice until a traveller complained:

* run twice and resurrect registrations somebody erased between the two runs,
* mark itself complete over a blob it never managed to read,
* report success while the store holds fewer journeys or fewer marks than the blob did,
* restamp every traveller's signup instant with the cutover date.

Run: uv run --with fastapi --with pydantic --with google-cloud-bigquery python test_varsel_import.py
"""

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

_STATE_FD, _STATE_PATH = tempfile.mkstemp(prefix="varsel_import_", suffix=".json")
os.close(_STATE_FD)
os.environ["VARSEL_STATE_PATH"] = _STATE_PATH

import notification_store

# The fake client and its staging helpers, for `test_varsel_state`'s reason: the store backend's
# correctness rests on the real bodies, and a private fake would be a second set of properties to
# keep true.
import test_notification_store as store_suite
import varsel_state

NOW = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
SIGNUP = datetime(2026, 7, 8, 6, 30, tzinfo=UTC)
CLIENT = varsel_state.BROWSER_CLIENT_ID
MAX_DEVICES = 50
MAX_JOURNEYS = 50

DEV_A = "dev-aaaaaaaa"
DEV_B = "dev-bbbbbbbb"
DEV_GONE = "dev-cccccccc"


def _subscription(tag: str) -> dict:
    return {"endpoint": f"https://push.example.com/{tag}", "keys": {"p256dh": "k", "auth": "a"}}


def _journey(device_id: str, **over) -> dict:
    rec = {
        "device_id": device_id,
        "from_stop": "NSR:StopPlace:337",
        "to_stop": "NSR:StopPlace:418",
        "from_name": "Oslo S",
        "to_name": "Asker stasjon",
        "begin": "07:00",
        "end": "09:00",
        "days": [1, 2, 3, 4, 5],
        "created_ts": SIGNUP.isoformat(),
        "consent_text_version": "2026-09-02.1",
    }
    rec.update(over)
    return rec


def _blob(**over: Any) -> dict:
    """The plan's own fixture: two devices, three journeys (one whose device record is gone), four
    live marks, one stray, one tombstone, and round markers the import must leave alone."""
    state = {
        "devices": {
            DEV_A: {"subscription": _subscription("a"), "created_ts": SIGNUP.isoformat(), "brand": "entur"},
            DEV_B: {"subscription": _subscription("b"), "created_ts": SIGNUP.isoformat(), "brand": "none"},
        },
        "journeys": {
            "j-1": _journey(DEV_A),
            "j-2": _journey(DEV_A, begin="15:00", end="17:00"),
            "j-3": _journey(DEV_GONE, to_stop="NSR:StopPlace:591"),
        },
        "notified": [
            "j-1|ENT:SituationNumber:1",
            "j-1|ENT:SituationNumber:2",
            "j-2|ENT:SituationNumber:3",
            "j-3|KIX:ETCancellation:abc",
            "j-deleted|ENT:SituationNumber:9",
        ],
        "device_tombstones": {
            DEV_GONE: {
                "reason": "turned_off",
                "ts": (SIGNUP + timedelta(days=3)).isoformat(),
                "created_ts": SIGNUP.isoformat(),
                "journeys": 1,
            }
        },
        "togvarsel_rounds": {"2026-09-30": {"0630": {"level": "yellow", "pushed": True}}},
        "some_future_key": {"kept": True},
    }
    state.update(over)
    return state


def _write_blob(state: dict | None = None) -> dict:
    state = _blob() if state is None else state
    with open(_STATE_PATH, "w") as handle:
        json.dump(state, handle)
    return state


def _store(_mp: Any, now: datetime = NOW) -> tuple[varsel_state.StoreRegistrations, Any]:
    """A store-backed seam over a fresh fake, re-staging its query pages on every read.

    `test_varsel_state._store`'s arrangement, repeated rather than imported because that suite's
    copy carries its own tiny ceilings: the import has to be able to write more documents than a
    ceiling test wants to reach.

    The store's own clock is frozen here rather than in each test, and that is not only for
    determinism: the import passes its instant where it can and every document it writes through
    `create_subscription` takes `notification_store`'s clock instead. In production those are the
    same clock read seconds apart, so freezing it is what makes the suite's `now` mean what the
    import's `now` means.

    AFTER `_install`, which configures a clock of its own -- the store suite's `NOW`, two months
    before this one. Patched before it, every document would carry that instant instead and the
    horizon assertions would be checking the fixture against itself.
    """
    client = store_suite._FakeClient()
    store_suite._install(client)
    _mp.setattr(notification_store, "_clock", lambda: now)
    notification_store.subscription_mirror().reset()
    staged = notification_store._query

    def _restaging_query(name: str, **kwargs: Any) -> Any:
        subscriptions = [
            doc
            for path, doc in client.docs.items()
            if path.startswith(f"{notification_store.SUBSCRIPTIONS}/")
        ]
        devices = [
            doc
            for path, doc in client.docs.items()
            if path.startswith(f"{notification_store.DEVICES}/")
        ]
        client.page("device_journeys", notification_store.SUBSCRIPTIONS, subscriptions)
        # The export's own read, which has no filters at all: it pages the whole collection and
        # picks the client and the channel in Python, so the fixture stages every subscription.
        client.page("mirror_seed", notification_store.SUBSCRIPTIONS, subscriptions)
        client.page("subscription_count", notification_store.SUBSCRIPTIONS, subscriptions)
        client.page("device_count", notification_store.DEVICES, devices)
        client.page("device_listing", notification_store.DEVICES, devices)
        return staged(name, **kwargs)

    notification_store._query = _restaging_query  # type: ignore[assignment]
    return (
        varsel_state.StoreRegistrations(
            client_id=CLIENT, quotas=lambda: (MAX_DEVICES, MAX_JOURNEYS)
        ),
        client,
    )


def _docs(client: Any, collection: str) -> dict[str, dict]:
    prefix = f"{collection}/"
    return {
        path[len(prefix) :]: doc for path, doc in client.docs.items() if path.startswith(prefix)
    }


def test_the_import_writes_every_registration_and_counts_the_mark_it_cannot_place(_mp):
    """The plan's own fixture, document for document.

    The stray mark is the one number here that is legitimately non-zero: `j-deleted` is a mark for
    a journey the blob no longer holds, which has nothing in the store to attach to. Counted rather
    than dropped silently, because a reader who does not know that would read it as data loss.
    """
    _write_blob()
    store, client = _store(_mp)
    report = varsel_state.import_blob_into_store(store, now=NOW)

    assert report.result == "imported"
    assert report.journeys == 3
    assert report.devices == 2
    assert report.devices_synthesised == 1, "the journey whose device record is gone got no device"
    assert report.tombstones == 1
    assert report.marks == 4
    assert report.stray_marks == 1
    assert report.problems == ()

    assert set(_docs(client, notification_store.SUBSCRIPTIONS)) == {"j-1", "j-2", "j-3"}
    assert set(_docs(client, notification_store.DEVICES)) == {DEV_A, DEV_B, DEV_GONE}
    # The traveller who never turned notifications on: a device document with no push subscription,
    # which is a state the product supports rather than a repair.
    synthesised = _docs(client, notification_store.DEVICES)[DEV_GONE]
    assert synthesised["endpoint"] == "" and synthesised["p256dh"] == ""
    assert synthesised["tombstone"]["reason"] == "turned_off"
    assert synthesised["tombstone"]["at"] == SIGNUP + timedelta(days=3), (
        "the tombstone was restamped with the cutover instant, so every lapse reads as having "
        "happened on cutover day"
    )
    # The round markers are deliberately NOT carried: they are off the protocol and ride in the
    # blob half. That is the gap the operations document's cutover section names, not this
    # function's to invent a home for.
    assert "togvarsel_rounds" not in json.dumps(sorted(client.docs))


def test_the_import_keeps_the_signup_instant_and_starts_the_horizon_at_the_cutover(_mp):
    """Two instants, two reasons, and getting either backwards is invisible for months.

    `created_at` is the traveller's own signup: the device report cohorts on it and
    `device_journeys` orders by it, so stamping the import over it would make every registration
    look made on cutover day. `last_seen_at` is the import instant, because the blob records no
    per-journey last-seen and there is nothing else to take — and under U14's inactivity horizon
    that gives every imported registration a fresh 180 days rather than a cliff.
    """
    _write_blob()
    store, client = _store(_mp)
    varsel_state.import_blob_into_store(store, now=NOW)

    doc = _docs(client, notification_store.SUBSCRIPTIONS)["j-1"]
    assert doc["created_at"] == SIGNUP, "the traveller's signup instant was overwritten"
    assert doc["last_seen_at"] == NOW
    assert doc["inactivity_expires_at"] == NOW + timedelta(
        days=notification_store.WEB_PUSH_INACTIVITY_DAYS
    ), "the horizon was anchored on the signup rather than on the cutover"
    assert "lifetime_expires_at" not in doc
    assert _docs(client, notification_store.DEVICES)[DEV_A]["created_at"] == SIGNUP
    # And the record the traveller reads back carries the signup they made, not the import's.
    assert dict(store.device_journeys(DEV_A))["j-1"]["created_ts"] == SIGNUP.isoformat()


def test_every_imported_journey_is_one_the_cycle_and_the_mirror_can_both_see(_mp):
    """KTD13's document invariant, asserted through the two readers rather than by field-checking.

    A journey has to carry `channel`, `client_id`, `status` and its horizon. Rather than listing
    those, this asks the two things that actually consume them: `device_journeys`, whose query
    filters the first two and whose liveness test covers the other two, and one pass of the
    mirror's own liveness — which is the read the poller matches from, and the one that would
    silently hold an entry and serve it to nobody.
    """
    _write_blob()
    store, client = _store(_mp)
    varsel_state.import_blob_into_store(store, now=NOW)

    rows = [dict(doc) for doc in _docs(client, notification_store.SUBSCRIPTIONS).values()]
    store_suite._mirror_page(client, "mirror_seed", rows)
    notification_store.subscription_mirror().seed(now=NOW)
    held = notification_store.subscription_mirror().recipients(
        NOW, channel=notification_store.CHANNEL_WEB_PUSH
    )
    assert len(held) == 3, "an imported registration is in the store and invisible to the cycle"
    assert {entry.subscription_id for entry in held} == {"j-1", "j-2", "j-3"}
    assert {entry.device_id for entry in held} == {DEV_A, DEV_GONE}


def test_a_second_run_is_a_no_op_and_cannot_resurrect_what_a_traveller_erased(_mp):
    """The one-shot marker, and the reason it is not caution.

    The import creates by document id and never deletes. It runs at startup, and dev restarts on
    every push — so without the marker, a traveller who pressed «Slett alt» after the cutover would
    have every journey back at the next deploy, with the device record and the push subscription
    that go with them. The marker is read before the blob is so much as opened.
    """
    _write_blob()
    store, client = _store(_mp)
    varsel_state.import_blob_into_store(store, now=NOW)
    store.erase_device(DEV_A)
    assert set(_docs(client, notification_store.SUBSCRIPTIONS)) == {"j-1", "j-2", "j-3"}
    erased = _docs(client, notification_store.SUBSCRIPTIONS)["j-1"]
    assert erased["status"] == "deleted", "the erasure did not tombstone the journey"

    before = len(client.transactions)
    second = varsel_state.import_blob_into_store(store, now=NOW + timedelta(hours=1))
    assert second.result == "skipped"
    assert second.journeys == 0
    assert len(client.transactions) == before, "the second run wrote to the store"
    assert _docs(client, notification_store.SUBSCRIPTIONS)["j-1"]["status"] == "deleted", (
        "a restart put back a registration the traveller had erased"
    )
    assert DEV_A not in _docs(client, notification_store.DEVICES)


def test_a_blob_that_could_not_be_read_is_never_marked_as_imported(_mp):
    """The failure that would lose every registration in one quiet startup.

    `load_state` swallows everything and starts empty, because serving without state beats not
    serving. The import must read that same failure the other way: "could not reach GCS" is not
    "there was nothing to import", and treating it as such would write the one-shot marker over a
    blob full of registrations nobody ever read — after which no restart would ever try again.
    """
    _write_blob()
    store, _client = _store(_mp)

    def _unreachable() -> str | None:
        raise RuntimeError("503 while reading the state blob")

    _mp.setattr(varsel_state, "read_blob_text", _unreachable)
    try:
        varsel_state.import_blob_into_store(store, now=NOW)
    except RuntimeError as exc:
        assert "503" in str(exc)
    else:
        raise AssertionError("an unreadable blob must fail the import, not complete it")
    assert notification_store.read_migration(varsel_state.IMPORT_MARKER) is None, (
        "the import marked itself complete over a blob it never read"
    )


def test_a_missing_blob_is_complete_rather_than_pending(_mp):
    """A fresh environment has no blob, and that IS the migration, finished.

    Left pending, a blob appearing later — a restored backup, a rollback that wrote one — would be
    imported by whichever pod restarted next, which is a migration nobody asked for.
    """
    if os.path.exists(_STATE_PATH):
        os.unlink(_STATE_PATH)
    store, client = _store(_mp)
    report = varsel_state.import_blob_into_store(store, now=NOW)
    assert report.result == "empty" and report.journeys == 0
    marker = notification_store.read_migration(varsel_state.IMPORT_MARKER)
    assert marker is not None and marker["result"] == "empty"
    assert _docs(client, notification_store.SUBSCRIPTIONS) == {}


def test_an_import_that_lost_a_journey_refuses_to_serve_and_stays_unmarked(_mp):
    """KTD13's "refuses to serve", and the half that matters is the marker.

    The invariants are read back OUT of the store rather than counted on the way in, so a body that
    silently dropped a document is caught by the same read the request path makes. Raising takes
    the lifespan down and `helm --wait` rolls the release back to the binary still reading the
    blob — which is intact, because the import creates and never deletes. And the marker is NOT
    written, so the next start tries again rather than leaving a half-imported store no restart
    will ever finish.
    """
    _write_blob()
    store, _client = _store(_mp)
    real = store.create_journey
    calls = {"n": 0}

    def _drop_the_second(journey_id: str, record: dict) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            return
        real(journey_id, record)

    _mp.setattr(store, "create_journey", _drop_the_second)
    try:
        varsel_state.import_blob_into_store(store, now=NOW)
    except varsel_state.ImportFailed as exc:
        assert "journeys" in str(exc)
    else:
        raise AssertionError("an import that lost a journey must refuse to serve")
    assert notification_store.read_migration(varsel_state.IMPORT_MARKER) is None, (
        "a failed import marked itself complete; no restart will ever finish it"
    )


def test_an_import_that_lost_a_mark_refuses_too(_mp):
    """The second invariant, which nothing else here would catch.

    A missing mark is not a missing registration: every journey is present, the page renders, and
    the only symptom is a traveller pushed a second time about a train they were already told
    about — days later, and traceable to nothing.
    """
    _write_blob()
    store, _client = _store(_mp)
    _mp.setattr(store, "mark_notified", lambda journey_id, situation_number: None)
    try:
        varsel_state.import_blob_into_store(store, now=NOW)
    except varsel_state.ImportFailed as exc:
        assert "marks" in str(exc)
    else:
        raise AssertionError("an import that lost the push marks must refuse to serve")


# ---- The rollback artefact ---------------------------------------------------------------------


def _imported(_mp: Any) -> tuple[Any, Any]:
    _write_blob()
    store, client = _store(_mp)
    varsel_state.import_blob_into_store(store, now=NOW)
    return store, client


def test_the_export_writes_a_blob_the_blob_backend_can_read_straight_back(_mp):
    """Round trip: import, export, load the export with the blob backend, compare.

    The artefact is only worth having if a rollback can USE it, and a rollback is
    `VARSEL_STATE_BACKEND` back to `gcs` -- which is `apply_state` over whatever the blob holds. So
    the check is not "the file has the right keys" but "the other backend reads the same
    registrations out of it".
    """
    store, _client = _imported(_mp)
    report = varsel_state.export_store_to_blob(store, now=NOW)
    assert report.journeys == 3 and report.devices == 2 and report.tombstones == 1
    assert report.marks == 4 and report.bytes_written > 0

    varsel_state.apply_state(varsel_state.read_blob_text() or "{}")
    assert set(varsel_state._journeys) == {"j-1", "j-2", "j-3"}
    assert set(varsel_state._devices) == {DEV_A, DEV_B}, "a tombstoned device is not a record"
    assert set(varsel_state._device_tombstones) == {DEV_GONE}
    assert varsel_state._device_tombstones[DEV_GONE]["reason"] == "turned_off"
    assert varsel_state._device_tombstones[DEV_GONE]["journeys"] == 1
    # The four live marks; the stray one the import dropped does not come back.
    assert varsel_state._notified == {
        "j-1|ENT:SituationNumber:1",
        "j-1|ENT:SituationNumber:2",
        "j-2|ENT:SituationNumber:3",
        "j-3|KIX:ETCancellation:abc",
    }
    assert varsel_state._journeys["j-1"]["created_ts"] == SIGNUP.isoformat()


def test_a_traveller_who_erased_everything_is_absent_from_the_next_export(_mp):
    """The property the export exists for, and the only one whose absence is a privacy defect.

    The import creates and never deletes, so the blob still holds every registration it started
    with -- including those of somebody who has since pressed «Slett alt». Roll back without an
    export and they are all back. The read behind the export applies liveness, so a tombstoned
    registration is ABSENT rather than written as deleted.
    """
    store, _client = _imported(_mp)
    store.erase_device(DEV_A)
    varsel_state.export_store_to_blob(store, now=NOW)

    varsel_state.apply_state(varsel_state.read_blob_text() or "{}")
    assert set(varsel_state._journeys) == {"j-3"}, "an erased traveller's journeys were exported"
    assert DEV_A not in varsel_state._devices
    assert not [m for m in varsel_state._notified if m.startswith("j-1|")]


def test_the_export_carries_through_the_keys_the_store_has_no_home_for(_mp):
    """The togvarsel round markers and anything else the blob carries.

    They are off the protocol and the store holds none of them, so an export that rebuilt the
    document from the store alone would silently delete them from the only place they exist -- and
    the deletion would be invisible until a rush round re-fired after a restart.
    """
    store, _client = _imported(_mp)
    varsel_state.export_store_to_blob(store, now=NOW)
    written = json.loads(varsel_state.read_blob_text() or "{}")
    assert written["togvarsel_rounds"] == {"2026-09-30": {"0630": {"level": "yellow", "pushed": True}}}
    assert written["some_future_key"] == {"kept": True}


def test_the_export_never_writes_the_blob_backends_empty_state_over_the_artefact(_mp):
    """`save_state` serialises the blob backend's own module dicts, which are EMPTY under the store.

    So the one thing an export must not do is go through it. Asserted by driving the export with
    the module dicts deliberately full of something else: what lands in the blob is the store's
    registrations, never theirs.
    """
    store, _client = _imported(_mp)
    with varsel_state.lock:
        varsel_state._journeys.clear()
        varsel_state._journeys["ghost"] = _journey(DEV_B)
        varsel_state._devices.clear()
    varsel_state.export_store_to_blob(store, now=NOW)
    written = json.loads(varsel_state.read_blob_text() or "{}")
    assert "ghost" not in written["journeys"]
    assert set(written["journeys"]) == {"j-1", "j-2", "j-3"}


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
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        mp = _MonkeyPatch()
        store_suite._uninstall()
        notification_store.subscription_mirror().reset()
        try:
            t(mp)
        finally:
            mp.undo()
            store_suite._uninstall()
        print(f"  ok  {t.__name__}")
    if os.path.exists(_STATE_PATH):
        os.unlink(_STATE_PATH)
    print(f"\n{len(tests)} cutover-import checks passed.")
