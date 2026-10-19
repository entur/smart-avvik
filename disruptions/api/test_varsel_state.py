"""Checks for `varsel_state` — the seam a `/varsel` registration lives behind, and its blob backend.

Hermetic and standalone: the state path is a temp file, nothing here touches GCS, Firestore or the
network, and `varsel` is imported only to assert what it does NOT do.

Two kinds of check, and the second is the one this file exists for. The first is ordinary behaviour
of the blob backend — the tombstone that carries a signup across a lapse, the erasure that restores
everything when the durable write fails, the ledger a replacement push must not charge. The second
is the PROPERTIES THE SEAM RESTS ON, each of which fails silently: that the implementation still
answers the protocol it claims (a method renamed on one side and not the other is a `TypeError` on
the cycle's thread, hours later), that reads hand out copies rather than live records (an in-place
edit works on the blob and vanishes on the store), and that `apply_state` mutates in place rather
than rebinding (rebinding orphans `varsel`'s aliases and every suite that reads through them keeps
asserting against the dict the load discarded).

Run: uv run --with fastapi --with pydantic --with google-cloud-bigquery python test_varsel_state.py
"""

import inspect
import os
import tempfile
from datetime import UTC, datetime, timedelta
from typing import Any

_STATE_FD, _STATE_PATH = tempfile.mkstemp(prefix="varsel_state_seam_", suffix=".json")
os.close(_STATE_FD)
os.environ["VARSEL_STATE_PATH"] = _STATE_PATH

import notification_store
import varsel_state

NOW = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
DEVICE = "dev-abcdef12"
OTHER = "dev-99999999"
SUB = {"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k", "auth": "a"}}


#: Deliberately tiny, so the ceilings are reachable in a test at all. `/varsel`'s own are 5 000 and
#: 20 000, which no suite can drive, and a ceiling nothing exercises is a ceiling nobody knows works.
MAX_DEVICES = 3
MAX_JOURNEYS = 5


def _reset() -> varsel_state.BlobRegistrations:
    with varsel_state.lock:
        varsel_state._devices.clear()
        varsel_state._journeys.clear()
        varsel_state._notified.clear()
        varsel_state._device_tombstones.clear()
        varsel_state._pushed_trains.clear()
        varsel_state._daily_pushes.clear()
        varsel_state._togvarsel_rounds.clear()
        varsel_state._state_extra.clear()
    # The markers' durable home is module state, so a suite that moved it has to move it back or
    # the next test writes a Firestore document from a blob-backend check.
    varsel_state.configure_rounds(in_store=False)
    return varsel_state.BlobRegistrations(max_devices=MAX_DEVICES, max_journeys=MAX_JOURNEYS)


def _journey(device_id: str = DEVICE, **over) -> dict:
    rec = {
        "device_id": device_id,
        "from_stop": "NSR:StopPlace:1",
        "to_stop": "NSR:StopPlace:2",
        "begin": "07:00",
        "end": "09:00",
        "days": [1, 2, 3, 4, 5],
        "created_ts": NOW.isoformat(),
    }
    rec.update(over)
    return rec


# ---------------------------------------------------------------- the seam's own properties
def test_the_blob_backend_answers_every_method_the_protocol_declares(_mp):
    """A protocol nothing checks is a comment.

    `RegistrationStore` is a `typing.Protocol`, so nothing at runtime compares it against an
    implementation: a method renamed on one side, or a keyword-only argument added to one, type-checks
    in the editor and fails on the poller's thread hours later — the store backend will be a SECOND
    implementation of this, and the failure would then be per backend rather than per deployment.
    So both halves are asserted here: every declared name exists, and its signature matches
    parameter for parameter.
    """
    declared = {
        name: value
        for name, value in vars(varsel_state.RegistrationStore).items()
        if not name.startswith("_") and callable(value)
    }
    assert len(declared) >= 18, f"the protocol shrank to {len(declared)} methods; is this right?"
    assert "device_count" not in declared and "journey_count" not in declared, (
        "a count the caller compares against a constant is back on the protocol; the ceiling is the "
        "backend's, raised as CapacityExceeded -- see that class"
    )
    for name, proto in declared.items():
        impl = getattr(varsel_state.BlobRegistrations, name, None)
        assert impl is not None, f"BlobRegistrations does not answer {name}"
        assert inspect.signature(impl) == inspect.signature(proto), (
            f"{name} has drifted: {inspect.signature(impl)} against {inspect.signature(proto)}"
        )


def test_varsel_state_imports_neither_varsel_nor_main(_mp):
    """The package rule, both ways. `varsel` imports this module; this module importing `varsel`
    back would be a cycle, and importing `main` would put the whole app behind a state read. Checked
    off the SOURCE rather than off `sys.modules`, because by the time this suite runs `varsel` may
    well be imported by something else."""
    import re

    source = "\n".join(
        line
        for line in inspect.getsource(varsel_state).split("\n")
        if not line.lstrip().startswith("#")
    )
    for forbidden in (r"^import varsel\b", r"^import main\b", r"^from varsel\b", r"^from main\b"):
        # Anchored and word-bounded rather than a literal search: `import varsel as v` and a
        # trailing comment both slip past `"import varsel\n" in source`, and an import this module
        # must not have is precisely the thing a plain substring test would wave through.
        assert not re.search(forbidden, source, re.MULTILINE), f"varsel_state matches `{forbidden}`"


def test_the_seam_emits_no_varsel_log_event(_mp):
    """Every structured event a reader greps for is `varsel`'s vocabulary (`varsel_device_gone`,
    `varsel_delete_all`, …), and it must not change when the backend does. So the backend logs
    nothing at all with an `event` field: what a log query matches on stays a property of the
    caller, whichever store answers underneath it."""
    source = inspect.getsource(varsel_state)
    assert '"event"' not in source, "varsel_state emits a structured event; the vocabulary is varsel's"


def test_a_read_hands_out_a_copy_so_an_in_place_edit_cannot_silently_work(_mp):
    """The blob CAN hand out its live records and the store cannot, so a caller that edited one in
    place would work on one backend and lose the edit on the other — silently, and only in
    production. Copies make that structural: the two edits `varsel` genuinely makes have their own
    methods (`set_canonical_stops`, `stamp_service_check`), and this is what keeps a third from
    being written as a mutation."""
    store = _reset()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())

    device = store.get_device(DEVICE)
    device["subscription"] = {"endpoint": "https://evil.example.com"}
    assert store.get_device(DEVICE)["subscription"] == SUB, "a device read handed out a live record"

    _jid, record = store.device_journeys(DEVICE)[0]
    record["from_stop"] = "NSR:StopPlace:999"
    assert store.device_journeys(DEVICE)[0][1]["from_stop"] == "NSR:StopPlace:1"
    assert store.all_journeys()[0][1]["from_stop"] == "NSR:StopPlace:1"
    store.get_journey("j1")["to_stop"] = "NSR:StopPlace:999"
    assert store.get_journey("j1")["to_stop"] == "NSR:StopPlace:2"


def test_a_load_mutates_the_state_in_place_rather_than_rebinding_it(_mp):
    """`varsel` aliases these objects, so a rebind here orphans them.

    It rebound while it was its own only reader, and the failure the alias creates is the worst
    shape there is: `load_state` would leave `varsel._journeys` pointing at the dict it discarded,
    so the module would serve pre-load state while every suite reading through the alias asserted
    happily against the same stale dict. Identity is what has to hold, and identity is what is
    checked.
    """
    store = _reset()
    devices, journeys, notified = (
        varsel_state._devices,
        varsel_state._journeys,
        varsel_state._notified,
    )
    store.create_journey("j1", _journey())
    varsel_state.apply_state('{"devices": {}, "journeys": {}, "notified": ["j9|S1"]}')
    assert varsel_state._devices is devices
    assert varsel_state._journeys is journeys
    assert varsel_state._notified is notified
    assert journeys == {} and notified == {"j9|S1"}, "the load did not reach the aliased objects"


def test_a_load_reads_the_local_file_back_into_the_state_it_came_from(_mp):
    """The startup path, end to end against the real file, because nothing else in the repo drives
    it: `varsel.load_state()` runs once in `main.lifespan` and no caller suite reaches it, so
    without this the blob backend's only durable READ would ship unexercised. Bucket-less is the
    documented local-only mode and the one this suite runs in.
    """
    store = _reset()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    store.mark_notified("j1", "S1")
    assert store.save() is True, "bucket-less means there is no durable copy to be stale"

    _reset()
    assert store.device_journeys(DEVICE) == []
    store.load()
    assert store.get_device(DEVICE)["subscription"] == SUB
    assert [jid for jid, _rec in store.device_journeys(DEVICE)] == ["j1"]
    assert store.push_ledger("j1").notified == {"S1"}


def test_a_corrupt_blob_starts_empty_rather_than_refusing_to_serve(_mp):
    """Tolerant on purpose: a first-ever run, a fresh bucket and a truncated write all have to leave
    the app serving. What is lost is registrations, which the traveller can make again; what a raise
    would cost is the whole deployment."""
    store = _reset()
    with open(_STATE_PATH, "w") as fh:
        fh.write("{not json at all")
    store.load()
    assert store.all_journeys() == [] and store.get_device(DEVICE) is None


def test_an_unknown_top_level_key_survives_a_load_and_a_serialize(_mp):
    """Rollback insurance: a key written by a future build must survive THIS build rewriting the
    blob, or a rollback-and-forward silently erases that feature's state."""
    import json

    _reset()
    varsel_state.apply_state('{"journeys": {}, "a_future_feature": {"x": 1}}')
    assert json.loads(varsel_state.serialize_state())["a_future_feature"] == {"x": 1}


# ---------------------------------------------------------------- devices and their tombstones
def test_a_device_coming_back_after_a_lapse_keeps_its_original_signup(_mp):
    """The tombstone carries the signup across «Skru av» and across an expired subscription, and
    `put_device` is where it comes back. Without it a lapse reads as a brand-new device and moves
    that traveller out of every cohort the device report counts them in."""
    store = _reset()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    signup = store.get_device(DEVICE)["created_ts"]

    record, kept = store.forget_device(DEVICE, reason="turned_off")
    assert record is not None and kept == 1
    assert varsel_state._device_tombstones[DEVICE]["reason"] == "turned_off"

    store.put_device(DEVICE, subscription=SUB, brand="none")
    assert store.get_device(DEVICE)["created_ts"] == signup, "the lapse read as a new signup"
    assert store.get_device(DEVICE)["brand"] == "none", "the brand must follow the newest register"
    assert DEVICE not in varsel_state._device_tombstones, "the tombstone outlived the return"


def test_a_device_with_no_journeys_left_behind_leaves_no_tombstone(_mp):
    """A tombstone explains journeys that outlived a record. With none there is nothing to explain,
    and keeping one would have the device report describe a device nobody can be registered
    through. The stale-marker case is the same rule the other way: a device that lost its record
    while journeys stood, then lost the journeys, must not keep the marker."""
    store = _reset()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    record, kept = store.forget_device(DEVICE, reason="expired")
    assert record is not None and kept == 0
    assert varsel_state._device_tombstones == {}

    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    store.forget_device(DEVICE, reason="expired")
    assert DEVICE in varsel_state._device_tombstones
    store.delete_journey("j1")
    assert DEVICE not in varsel_state._device_tombstones, "the last journey left the marker behind"


def test_the_two_global_ceilings_refuse_through_the_backend_and_never_refuse_a_returning_device(_mp):
    """The ceiling moved from a caller's read-then-compare into the backend (U13's store half).

    Two properties, and the second is the one a pre-check got wrong for free: a ceiling must refuse
    a NEW device and must never refuse one that already exists, or a deployment sitting at its
    ceiling stops accepting the subscription refreshes that keep its own registrations alive -- and
    the traveller whose push endpoint rotated is silently un-notifiable from then on.
    """
    store = _reset()
    for i in range(MAX_DEVICES):
        store.put_device(f"dev-fill{i:04d}", subscription=SUB, brand="entur")
    try:
        store.put_device("dev-oneMore1", subscription=SUB, brand="entur")
    except varsel_state.CapacityExceeded as exc:
        assert exc.kind == "devices"
    else:
        raise AssertionError("the device ceiling did not refuse")
    # A device already stored is refreshed, not refused.
    store.put_device("dev-fill0000", subscription=SUB, brand="none")
    assert store.get_device("dev-fill0000")["brand"] == "none"

    for i in range(MAX_JOURNEYS):
        store.create_journey(f"j{i}", _journey())
    try:
        store.create_journey("one-more", _journey())
    except varsel_state.CapacityExceeded as exc:
        assert exc.kind == "journeys"
    else:
        raise AssertionError("the journey ceiling did not refuse")


def test_several_devices_come_back_from_one_ask(_mp):
    """`devices_for` is one call because the store answers it with one multi-get. Unknown ids are
    absent rather than an error: a journey registered by a browser that never turned notifications
    on is the ordinary case, not a failure."""
    store = _reset()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.put_device(OTHER, subscription=SUB, brand="none")
    found = store.devices_for([DEVICE, OTHER, "dev-nobody1", DEVICE])
    assert set(found) == {DEVICE, OTHER}
    assert found[OTHER]["brand"] == "none"
    assert store.devices_for([]) == {}


def test_forgetting_an_unknown_device_reports_nothing_rather_than_writing_a_marker(_mp):
    store = _reset()
    assert store.forget_device("dev-nothing1", reason="expired") == (None, 0)
    assert varsel_state._device_tombstones == {}


# ---------------------------------------------------------------- journeys
def test_one_devices_journeys_are_its_own_and_come_back_in_creation_order(_mp):
    """R15's read: the request path asks about ONE device. Order is asserted because the store
    backend answers this through a `created_at ASC` query and the blob through insertion order --
    the same order, and a listing that silently reordered would move a traveller's cards about."""
    store = _reset()
    for jid in ("j1", "j2", "j3"):
        store.create_journey(jid, _journey())
    store.create_journey("other", _journey(device_id=OTHER))
    assert [jid for jid, _rec in store.device_journeys(DEVICE)] == ["j1", "j2", "j3"]
    assert [jid for jid, _rec in store.device_journeys(OTHER)] == ["other"]
    assert len(store.all_journeys()) == 4 and store.get_device(DEVICE) is None


def test_a_stamp_or_a_canonicalization_of_a_deleted_journey_reports_it_rather_than_resurrecting(_mp):
    """Both run on the cycle, both against a list read before the pass, and a delete can land in
    between. Writing the record back would resurrect a registration the traveller deleted; the
    caller counts a stamp only when it landed."""
    store = _reset()
    store.create_journey("j1", _journey())
    store.delete_journey("j1")
    assert store.get_journey("j1") is None
    assert store.set_canonical_stops("j1", from_stop="A", to_stop="B") is False
    assert store.stamp_service_check("j1", checked_ts="t", cleared=True, reverse_only=False) is False
    assert store.all_journeys() == []


def test_an_established_service_answer_moves_the_stamp_whether_or_not_it_clears(_mp):
    """It used to write only when something cleared, which left every stamp of an unchanged `false`
    in memory until some other writer happened to persist -- so a restart re-asked them all."""
    store = _reset()
    store.create_journey("j1", _journey(no_direct_service=True, service_reverse_only=False))
    assert store.stamp_service_check("j1", checked_ts="t1", cleared=False, reverse_only=True)
    rec = dict(store.all_journeys())["j1"]
    assert rec["service_checked_ts"] == "t1"
    assert rec["no_direct_service"] is True and rec["service_reverse_only"] is True

    assert store.stamp_service_check("j1", checked_ts="t2", cleared=True, reverse_only=False)
    rec = dict(store.all_journeys())["j1"]
    assert rec["service_checked_ts"] == "t2"
    assert "no_direct_service" not in rec and "service_reverse_only" not in rec


# ---------------------------------------------------------------- the erasure
def test_an_unknown_id_is_erased_with_no_write_at_all(_mp):
    """The endpoint is unauthenticated, so a blob upload per random UUID is free write
    amplification -- under the lock the poller and every registration need."""
    store = _reset()
    saves: list[int] = []
    _mp.setattr(varsel_state, "save_state", lambda: saves.append(1) or True)
    result = store.erase_device("dev-nobody12")
    assert result == varsel_state.Erasure(found=False, journeys_deleted=0, durable=True)
    assert saves == []


def test_a_lone_tombstone_is_something_to_erase(_mp):
    """`found` is not "a device record existed": a marker with no record and no journeys is still
    data about a person, and the caller logs the erasure for it."""
    store = _reset()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    store.forget_device(DEVICE, reason="expired")
    store.delete_journey("j1")
    varsel_state._device_tombstones[DEVICE] = {"reason": "expired", "ts": "t", "journeys": 1}
    result = store.erase_device(DEVICE)
    assert result.found and result.journeys_deleted == 0 and result.durable
    assert varsel_state._device_tombstones == {}


def test_a_failed_durable_write_puts_everything_back(_mp):
    """Restoring is what makes the RETRY correct, not tidiness: without it the second attempt finds
    nothing in memory, takes the zero-I/O unknown-id path, and answers 200 over a blob that is still
    full -- an art. 17 erasure quietly undone."""
    store = _reset()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    store.create_journey("j2", _journey())
    store.mark_notified("j1", "S1")
    store.forget_device(DEVICE, reason="expired")
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    before = (
        dict(varsel_state._devices),
        dict(varsel_state._journeys),
        set(varsel_state._notified),
    )

    _mp.setattr(varsel_state, "save_state", lambda: False)
    result = store.erase_device(DEVICE)
    assert result.found and result.journeys_deleted == 2 and result.durable is False
    assert (
        dict(varsel_state._devices),
        dict(varsel_state._journeys),
        set(varsel_state._notified),
    ) == before, "the rollback did not restore everything"


def test_an_erasure_takes_the_marks_of_the_journeys_it_deletes_and_no_others(_mp):
    store = _reset()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    store.create_journey("other", _journey(device_id=OTHER))
    store.mark_notified("j1", "S1")
    store.mark_notified("other", "S1")
    result = store.erase_device(DEVICE)
    assert result.found and result.journeys_deleted == 1 and result.durable
    assert varsel_state._notified == {"other|S1"}
    assert store.get_device(DEVICE) is None


# ---------------------------------------------------------------- the push ledger
def test_one_call_marks_several_situations(_mp):
    """The morning digest's shape: every train it covers is marked in one call."""
    store = _reset()
    store.mark_notified("j1", "S1", "S2")
    assert store.push_ledger("j1").notified == {"S1", "S2"}


def test_one_registrations_ledger_carries_its_own_marks_and_no_neighbours(_mp):
    """Three questions per push, answered in ONE read because the store answers them from one
    journey document. A ledger that leaked a sibling registration's marks would suppress a push the
    other traveller has not had."""
    store = _reset()
    store.mark_notified("j1", "S1")
    store.mark_notified("j2", "S2")
    store.record_push("j1", tag="j1|L1|07:00", departure=NOW, oslo_day="2026-09-13", charge_cap=True)
    store.record_push("j2", tag="j2|L1|07:00", departure=NOW, oslo_day="2026-09-13", charge_cap=True)

    ledger = store.push_ledger("j1")
    assert ledger.notified == {"S1"}
    assert ledger.pushed_trains == {"j1|L1|07:00"}
    assert (ledger.day, ledger.sent_today) == ("2026-09-13", 1)
    assert store.push_ledger("j3") == varsel_state.PushLedger(
        notified=frozenset(), pushed_trains=frozenset(), day=None, sent_today=0
    )


def test_a_replacement_push_records_the_train_without_spending_the_day(_mp):
    """A push whose tag is already on the lock screen REPLACES the earlier notification rather than
    adding to the pile, so it must not spend one of the registration's four for the day -- the
    decision is the caller's, passed in, because re-deriving it here would read the very ledger the
    call is about to write."""
    store = _reset()
    tag = "j1|L1|07:00"
    store.record_push("j1", tag=tag, departure=NOW, oslo_day="2026-09-13", charge_cap=True)
    store.record_push("j1", tag=tag, departure=NOW, oslo_day="2026-09-13", charge_cap=False)
    assert store.push_ledger("j1").sent_today == 1
    assert store.push_ledger("j1").pushed_trains == {tag}


def test_a_count_carried_over_from_another_oslo_day_is_reset_rather_than_continued(_mp):
    store = _reset()
    store.record_push("j1", tag=None, departure=None, oslo_day="2026-09-12", charge_cap=True)
    store.record_push("j1", tag=None, departure=None, oslo_day="2026-09-12", charge_cap=True)
    store.record_push("j1", tag=None, departure=None, oslo_day="2026-09-13", charge_cap=True)
    assert store.push_ledger("j1") == varsel_state.PushLedger(
        notified=frozenset(), pushed_trains=frozenset(), day="2026-09-13", sent_today=1
    )


def test_an_alert_that_cannot_name_its_train_records_no_train(_mp):
    """`tag` is None when the alert cannot say which departure it is about; the service worker then
    falls back to the situation number, which is what it always did."""
    store = _reset()
    store.record_push("j1", tag=None, departure=None, oslo_day="2026-09-13", charge_cap=True)
    assert store.push_ledger("j1").pushed_trains == frozenset()
    assert store.push_ledger("j1").sent_today == 1


def test_the_pass_snapshot_is_asked_about_the_registrations_it_names(_mp):
    """`notified_snapshot` is pass 1b's question and takes the pair's own registrations. Asked for
    everything it would be a per-cycle read of every mark in the process -- which is exactly what
    the store backend cannot afford, and the reason the argument exists at all."""
    store = _reset()
    store.mark_notified("j1", "S1")
    store.mark_notified("j2", "S1")
    assert store.notified_snapshot(["j1"]) == {"j1|S1"}
    assert store.notified_snapshot(["j1", "j2"]) == {"j1|S1", "j2|S1"}
    assert store.notified_snapshot([]) == set()


# ---------------------------------------------------------------- the stale sweep
def test_the_sweep_reports_only_what_it_dropped_from_the_persisted_half(_mp):
    """The caller saves on a non-zero answer. The two in-memory ledgers are rebuilt by the next push
    and never reach the blob, so a sweep that only pruned those must NOT cost a blob upload -- and
    one that dropped a mark must."""
    store = _reset()
    store.mark_notified("j1", "S_gone")
    store.mark_notified("j1", "S_live")
    store.record_push(
        "j1", tag="j1|old", departure=NOW - timedelta(hours=9), oslo_day="2026-09-12", charge_cap=True
    )

    dropped = store.prune(
        live_situations={"S_live", "S_gone"}, trains_gone_by=NOW, oslo_today="2026-09-13"
    )
    assert dropped == 0, "nothing persisted went, so nothing is owed a save"
    assert store.push_ledger("j1").pushed_trains == frozenset(), "a departed train was kept"
    assert store.push_ledger("j1").sent_today == 0, "yesterday's count was kept"

    assert store.prune(live_situations={"S_live"}, trains_gone_by=NOW, oslo_today="2026-09-13") == 1
    assert varsel_state._notified == {"j1|S_live"}


# ---------------------------------------------------------------- the round markers
def test_the_round_markers_keep_exactly_one_date_and_are_handed_out_detached(_mp):
    """Markers dedup rounds and seed follow-up baselines for ONE day, so the first write after
    midnight is what forgets yesterday. Detached copies because togvarsel_notify mutates its working
    map freely and hands it back whole."""
    _reset()
    varsel_state.store_rounds("2026-09-12", {"0630": {"level": "yellow", "pushed": True}})
    varsel_state.store_rounds("2026-09-13", {"0630": {"level": "green", "pushed": False}})
    assert varsel_state.rounds_for("2026-09-12") == {}
    markers = varsel_state.rounds_for("2026-09-13")
    assert markers == {"0630": {"level": "green", "pushed": False}}
    markers["0630"]["level"] = "red"
    assert varsel_state.rounds_for("2026-09-13")["0630"]["level"] == "green"


def test_the_markers_are_deliberately_off_the_registration_protocol(_mp):
    """A rush round marker is not a registration, so it does not belong on a seam whose every
    method is a question about one — `erase_device`, `create_journey`, `mark_notified`. It IS
    durable under both backends (the store keeps one document for it), and that is the distinction
    this pins: off the protocol never meant off disk. On the protocol, both implementations would
    carry a method neither of them answers as a registration question."""
    declared = set(vars(varsel_state.RegistrationStore))
    assert not {n for n in declared if "round" in n}, "a round marker reached the registration seam"


# ---------------------------------------------------------------- the store backend
# Driven over `test_notification_store`'s OWN fake client rather than a second one written here.
# That suite's fake runs the real transaction bodies, applies their writes, enforces the read
# deadlines and resolves the server timestamp against a clock deliberately ahead of the caller's --
# every property this backend's correctness rests on. A private fake would be a second set of those
# properties to keep true, and the first thing to rot would be the one that matters most: that a
# body's writes are what a later read sees.
import test_notification_store as store_suite

STORE_CLIENT = varsel_state.BROWSER_CLIENT_ID


def _stage(client: Any) -> None:
    """Re-stage the fake's query pages from whatever it currently holds.

    The fake serves a query from a page a test put there, and applies the query's EQUALITY over it
    -- so staging every document of a collection and letting the filter run is the closest thing to
    a real collection this suite can have, and it is what makes `device_journeys`' own filter
    (client, channel, device) part of what is under test rather than something the fixture faked.
    """
    subscriptions = [
        doc
        for path, doc in client.docs.items()
        if path.startswith(f"{notification_store.SUBSCRIPTIONS}/")
    ]
    devices = [
        doc for path, doc in client.docs.items() if path.startswith(f"{notification_store.DEVICES}/")
    ]
    client.page("device_journeys", notification_store.SUBSCRIPTIONS, subscriptions)
    client.page("subscription_count", notification_store.SUBSCRIPTIONS, subscriptions)
    client.page("device_count", notification_store.DEVICES, devices)


def _store() -> tuple[varsel_state.StoreRegistrations, Any]:
    """A store-backed seam over a fresh fake, with the ceilings this suite can actually reach.

    Every query re-stages first, so a read always sees what the writes before it did. Staged once
    per test instead, the fixture would answer the question the test asked at setup time rather than
    the one it is asking now -- which is the shape that let a fake show an erase loop terminating
    when it could not.
    """
    client = store_suite._FakeClient()
    store_suite._install(client)
    notification_store.subscription_mirror().reset()
    staged_query = notification_store._query

    def _restaging_query(name: str, **kwargs: Any) -> Any:
        _stage(client)
        return staged_query(name, **kwargs)

    notification_store._query = _restaging_query  # type: ignore[assignment]
    return (
        varsel_state.StoreRegistrations(
            client_id=STORE_CLIENT, quotas=lambda: (MAX_DEVICES, MAX_JOURNEYS)
        ),
        client,
    )


def _seeded_mirror(client: Any) -> None:
    """Seed the subscription mirror from whatever the fake currently holds.

    The cycle reads the mirror and the request path reads the store, so a test about the cycle has
    to seed one -- and seeding it from the fake's own documents is what keeps the two halves
    describing the same state rather than two hand-built ones.
    """
    rows = [
        dict(doc)
        for path, doc in client.docs.items()
        if path.startswith(f"{notification_store.SUBSCRIPTIONS}/")
    ]
    store_suite._mirror_page(client, "mirror_seed", rows)
    notification_store.subscription_mirror().seed(now=datetime.now(UTC))


def test_the_store_backend_answers_the_same_protocol_as_the_blob(_mp):
    """Both backends, method for method and signature for signature.

    Two implementations is where a protocol earns its keep and where it silently stops being one: a
    keyword renamed on the blob is caught by every `/varsel` test, while the same rename on the
    store backend is caught by nothing until a cycle runs in an environment that has it switched on.
    """
    declared = {
        name: value
        for name, value in vars(varsel_state.RegistrationStore).items()
        if not name.startswith("_") and callable(value)
    }
    for name, proto in declared.items():
        impl = getattr(varsel_state.StoreRegistrations, name, None)
        assert impl is not None, f"StoreRegistrations does not answer {name}"
        assert inspect.signature(impl) == inspect.signature(proto), (
            f"{name} has drifted: {inspect.signature(impl)} against {inspect.signature(proto)}"
        )


def test_a_registration_created_on_the_request_path_is_listed_before_any_cycle(_mp):
    """R15, and the reason the request path does not read the mirror: the mirror is at best as fresh
    as the sync at the start of this cycle, and before the poller has seeded it, empty. A traveller
    who has just registered a journey must see it in the next `list_journeys` regardless."""
    store, _client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    assert notification_store.subscription_mirror().ready is False, "the mirror is deliberately cold"
    listed = store.device_journeys(DEVICE)
    assert [jid for jid, _rec in listed] == ["j1"]
    record = listed[0][1]
    assert record["device_id"] == DEVICE
    assert (record["from_stop"], record["to_stop"]) == ("NSR:StopPlace:1", "NSR:StopPlace:2")
    assert record["days"] == [1, 2, 3, 4, 5]
    assert store.get_device(DEVICE)["subscription"] == SUB


def test_every_field_varsels_own_record_carries_survives_the_round_trip(_mp):
    """The shape is the contract: `varsel` reads ONE record type from `device_journeys` and from
    `all_journeys`, renders the card from it and words a push from it. A field lost here does not
    error -- the card renders a blank name, the caveat disappears, the consent version reads as
    never given -- so every one of them is asserted by name."""
    store, client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey(
        "j1",
        _journey(
            from_name="Oslo S",
            to_name="Lillestrøm stasjon",
            consent_text_version="2026-09-02.1",
            no_direct_service=True,
            service_reverse_only=True,
            service_checked_ts=NOW.isoformat(),
        ),
    )
    (_jid, record) = store.device_journeys(DEVICE)[0]
    assert record["from_name"] == "Oslo S" and record["to_name"] == "Lillestrøm stasjon"
    assert record["consent_text_version"] == "2026-09-02.1"
    assert record["begin"] == "07:00" and record["end"] == "09:00"
    assert record["no_direct_service"] is True, "the caveat did not survive"
    assert record["service_reverse_only"] is True, "the swap qualifier did not survive"
    assert record["service_checked_ts"], "the caveat has no age, so it can never be judged"
    assert record["created_ts"], "the signup instant did not survive"
    # And the cycle's copy is the same record MINUS exactly one field, which is what lets one
    # reader serve both. `consent_text_version` is the difference and it is deliberate: the mirror
    # carries what the PASS reads and nothing else -- `_journey_model` is the only reader of that
    # field and it runs on the request path, where `device_journeys` answers. Asserted as a set
    # difference rather than glossed, so a SECOND field going missing from the cycle's view fails
    # here instead of rendering a blank name on somebody's card.
    _seeded_mirror(client)
    cycle = dict(store.all_journeys())["j1"]
    assert set(record) - set(cycle) == {"consent_text_version"}
    assert not set(cycle) - set(record), "the cycle's record carries a field the request path's does not"
    assert {k: v for k, v in record.items() if k != "consent_text_version"} == cycle


def test_a_servable_pair_carries_no_caveat_rather_than_a_false_one(_mp):
    """`/varsel` says "not established" by the caveat's ABSENCE, and `_service_warning` reads it
    that way. A `no_direct_service: False` would render as an all-clear the check never gave."""
    store, _client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey(service_checked_ts=NOW.isoformat()))
    (_jid, record) = store.device_journeys(DEVICE)[0]
    assert "no_direct_service" not in record and "service_reverse_only" not in record
    assert record["service_checked_ts"], "an established answer still has to carry its age"


def test_a_push_mark_is_on_the_document_and_the_next_cycle_sees_it(_mp):
    """The plan's second scenario. The write is the store's and the READ is the mirror's, which is
    the reversal of what U12 recorded: the mark bodies apply their own commit, so the mirror's copy
    is exact rather than a cycle behind -- and the alternative is a document read per registration
    per cycle."""
    store, client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    _seeded_mirror(client)

    store.mark_notified("j1", "S1")
    store.record_push("j1", tag="j1|L1|07:18", departure=NOW, oslo_day="2026-09-13", charge_cap=True)

    stored = client.doc(notification_store.SUBSCRIPTIONS, "j1")
    assert set(stored["notified"]) == {"S1"}, "the mark is not on the document"
    ledger = store.push_ledger("j1")
    assert ledger.notified == {"S1"}, "the cycle cannot see a mark this process just wrote"
    assert ledger.pushed_trains == {"j1|L1|07:18"}
    assert ledger.sent_today == 1
    assert store.notified_snapshot(["j1"]) == {"j1|S1"}


def test_a_push_that_cannot_name_its_train_charges_the_day_and_marks_no_train(_mp):
    """The `tag is None` branch, on BOTH backends, because they used to answer it differently.

    An alert that cannot say which departure it is about gets no lock-screen tag -- the worker falls
    back to the situation number. The store wrote a `"<jid>|unidentified"` placeholder for it: a
    mark no later push can ever match, taking a slot in a map `_prune_marks` bounds by age. The
    day's count is still spent, because the cap is per PUSH and not per train.
    """
    store, client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    _seeded_mirror(client)
    store.record_push("j1", tag=None, departure=None, oslo_day="2026-09-13", charge_cap=True)

    stored = client.doc(notification_store.SUBSCRIPTIONS, "j1")
    assert not stored["pushed_trains"], "an unnamed train left a mark nothing can match"
    assert stored["daily_pushes"], "the day's count was not charged"
    assert store.push_ledger("j1").sent_today == 1

    blob = _reset()  # clears the module state this half writes, and `_reset()` again below
    blob.create_journey("j1", _journey())
    blob.record_push("j1", tag=None, departure=None, oslo_day="2026-09-13", charge_cap=True)
    assert blob.push_ledger("j1").pushed_trains == frozenset(), "the two backends disagree"
    assert blob.push_ledger("j1").sent_today == 1
    _reset()  # the blob's state is MODULE state; left behind it reaches whatever runs next


def test_a_mark_for_a_journey_erased_mid_push_is_dropped_rather_than_raised(_mp):
    """The plan's fourth scenario. A push can be in flight past `_prepare_alert`'s re-check when the
    traveller presses «Slett alt»: the push went out, and the only thing this call could add is a
    mark on a document that no longer exists. Raising would take the whole delivery loop's result
    handling with it, for a journey nobody is registered on any more."""
    store, _client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    erasure = store.erase_device(DEVICE)
    assert erasure.found and erasure.journeys_deleted == 1 and erasure.durable

    store.mark_notified("j1", "S1")  # must not raise
    store.record_push("j1", tag=None, departure=None, oslo_day="2026-09-13", charge_cap=True)
    assert store.get_device(DEVICE) is None
    assert store.device_journeys(DEVICE) == []


def test_the_cycle_reads_nothing_at_all_from_an_unseeded_mirror(_mp):
    """`recipient_documents`' rule, restated for this reader: a partial population is worse than
    none, because the pass cannot tell "nobody is registered" from "the read has not finished" and
    would silently retire whatever the seed had not reached yet."""
    store, client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    assert store.all_journeys() == [], "a cold mirror answered for somebody"
    _seeded_mirror(client)
    assert [jid for jid, _rec in store.all_journeys()] == ["j1"]


def test_a_deleted_journey_is_gone_from_the_cycles_view_at_once(_mp):
    """The liveness re-check the delivery loop makes twice per push, and what the commit-apply is
    for: a watermark would carry this delete at the start of the NEXT cycle, by which time the push
    has gone out to a traveller who deleted the journey."""
    store, client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    _seeded_mirror(client)
    assert store.get_journey("j1") is not None

    store.delete_journey("j1")
    assert store.get_journey("j1") is None, "the cycle can still see a journey this request deleted"
    assert store.all_journeys() == []


def test_a_per_journey_read_is_keyed_and_never_a_scan_of_the_mirror(_mp):
    """The cost the store backend exists to remove, pinned where a benchmark cannot reach it.

    `recipients` copies the whole entry list and tests liveness on every element. The delivery loop
    asks `get_journey` three times per push and `push_ledger` once per candidate, so answering
    either by scanning makes a thousand pushes ~4 000 walks of a hundred thousand entries -- and a
    suite running at n=2 sees nothing at all. So the mechanism is asserted rather than the timing:
    with `recipients` refusing to answer, every per-journey read still does. `all_journeys` is
    deliberately not in this test -- it wants all of them, which is exactly what `recipients` is.
    """
    store, client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    _seeded_mirror(client)
    store.mark_notified("j1", "S1")

    def _refuse(self, *a, **k):
        raise AssertionError("a per-journey read walked the whole mirror")

    _mp.setattr(notification_store.SubscriptionMirror, "recipients", _refuse)
    assert store.get_journey("j1") is not None
    assert store.push_ledger("j1").notified == {"S1"}
    assert store.notified_snapshot(["j1"]) == {"j1|S1"}
    assert store.get_journey("nosuch") is None, "an unknown id must answer without a scan too"


def _foreign_write(client: Any, journey_id: str, **fields: Any) -> None:
    """Another pod's commit: on the document, and never applied to THIS process's mirror."""
    doc = client.doc(notification_store.SUBSCRIPTIONS, journey_id)
    client.put(notification_store.SUBSCRIPTIONS, journey_id, {**doc, **fields})


def test_the_request_paths_read_of_one_journey_is_the_stores_even_beside_a_seeded_mirror(_mp):
    """Zero-downtime U5 (R8, KTD7): a journey registered on another pod is in the store and not yet
    in the leader's mirror, so an ownership check that preferred the mirror would refuse the
    traveller's own delete until the next sync. The cycle's `get_journey` keeps the mirror."""
    store, client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    store_suite._mirror_page(client, "mirror_seed", [])
    notification_store.subscription_mirror().seed(now=datetime.now(UTC))

    assert store.get_journey("j1") is None, "the cycle's view should not have this journey yet"
    record = store.stored_journey("j1")
    assert record is not None and record["device_id"] == DEVICE
    assert store.stored_journey("nosuch") is None

    blob = _reset()
    blob.create_journey("j1", _journey())
    assert blob.stored_journey("j1") == blob.get_journey("j1")
    _reset()


def test_the_pre_send_read_refuses_a_journey_gone_or_already_marked_in_the_store(_mp):
    """Zero-downtime U5 (AE6): the leader's mirror is one cycle late for another pod's writes, so
    the last look before a send reads the document. Gone, or every situation already marked:
    refused. A train already marked under ANOTHER situation is not refused: that is a replacement
    (an ET cancellation confirming the interim leader's forecast) the stale mirror did not
    recognise, and refusing it would withhold the confirmation until the six-hourly re-seed."""
    store, client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    _seeded_mirror(client)
    tag = "j1|VYG|t1|A|1"

    assert store.may_send("j1", situations=("S1",))
    later = datetime.now(UTC) + timedelta(days=1)
    _foreign_write(client, "j1", notified={"S1": later}, pushed_trains={tag: later})
    assert not store.may_send("j1", situations=("S1",))
    assert store.may_send("j1", situations=("S1", "S2")), (
        "a digest naming a train nobody has been told about is still owed"
    )
    assert store.may_send("j1", situations=("S2",)), "the confirmation of a pushed train is owed"
    _foreign_write(client, "j1", status="deleted")
    assert not store.may_send("j1", situations=("S2",))
    assert store.get_journey("j1") is not None, "the mirror was meant to be the stale one here"

    blob = _reset()
    blob.create_journey("j1", _journey())
    blob.mark_notified("j1", "S1")
    assert blob.may_send("j1", situations=("S1",)), (
        "the blob backend never refuses here: its behaviour is today's, byte for byte"
    )
    _reset()


def test_the_ceilings_are_the_clients_quotas_under_the_store(_mp):
    """`_MAX_DEVICES` and `_MAX_JOURNEYS` become the reserved client's quotas, resolved per write so
    an operator's `set-quotas` takes effect without a restart. The refusal is the same
    `CapacityExceeded` the blob raises, because the route maps one exception to one 503."""
    store, _client = _store()
    for i in range(MAX_DEVICES):
        store.put_device(f"dev-fill{i:04d}", subscription=SUB, brand="entur")
    try:
        store.put_device("dev-oneMore1", subscription=SUB, brand="entur")
    except varsel_state.CapacityExceeded as exc:
        assert exc.kind == "devices"
    else:
        raise AssertionError("the device ceiling did not refuse")


def test_a_tombstoned_device_is_no_device_at_all(_mp):
    """`/varsel` POPS the record when a push endpoint dies and keeps the journeys, so "the record is
    gone" is exactly what the caller means. A husk with no endpoint would be handed to the push loop
    as a device, and every send against it would fail."""
    store, _client = _store()
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    record, kept = store.forget_device(DEVICE, reason="expired")
    assert record is not None and kept == 1
    assert store.get_device(DEVICE) is None
    assert store.devices_for([DEVICE]) == {}
    assert [jid for jid, _rec in store.device_journeys(DEVICE)] == ["j1"], "the journeys went too"


def test_the_store_backend_has_nothing_to_flush_and_nothing_to_sweep(_mp):
    """The protocol's one asymmetry, from this side. Each mutator is its own durable document write,
    so `save()` is True with nothing to do -- which is what lets the caller's "mutate, then save"
    sequence be correct on both backends and cost a blob upload on only one. `prune` returns 0 for
    the same reason: the marks are pruned at write time, so no save is ever owed."""
    store, _client = _store()
    assert store.save() is True
    assert store.prune(live_situations=set(), trains_gone_by=NOW, oslo_today="2026-09-13") == 0
    store.load()  # a no-op: the mirror seeds on the poller thread


def test_a_registration_read_every_week_outlives_the_horizon_it_was_created_under(_mp):
    """U14's whole point, walked over 200 days of a 180-day horizon.

    U12 anchored the browser channel's horizon on `created_at` and nothing could move it: the
    client never touches, an amendment keeps `created_at`, and the device upsert writes the device
    document alone. So a commuter who opened the app every morning lost the journey on day 181 --
    and not visibly, because an expired registration stops matching, drops out of this very list
    and is TTL-deleted within about a day. The device record did not fail that way, since its TTL
    is restamped on every upsert, so the steady state was: notifications on, device healthy,
    journey list quietly shorter, alerts stopped.

    The walk is the assertion because the defect is only reachable in time. Every check inside the
    loop passes under the old rule too, for 180 days.

    The second half is what keeps this an inactivity horizon rather than no horizon at all: a
    traveller who stops opening the app does age out, which is the whole storage-limitation reason
    the field exists.
    """
    store, client = _store()
    clock = {"now": NOW}
    _mp.setattr(notification_store, "_clock", lambda: clock["now"])
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    store.create_journey("j1", _journey())
    last_read = NOW
    for day in range(0, 201, 7):
        clock["now"] = NOW + timedelta(days=day)
        listed = [jid for jid, _rec in store.device_journeys(DEVICE)]
        assert listed == ["j1"], f"the registration was gone on day {day}"
        last_read = clock["now"]
    # The device document went with the journeys rather than expiring under them -- the population
    # that needs this most is the traveller who never turned notifications on, whose device is
    # never upserted and so has no other way to be renewed.
    device_doc = client.doc(notification_store.DEVICES, DEVICE) or {}
    assert device_doc[notification_store.TTL_FIELD] >= last_read, "the device document was not renewed"
    # And a traveller who stops opening the app still ages out, one horizon after their last visit.
    clock["now"] = last_read + timedelta(
        days=notification_store.WEB_PUSH_INACTIVITY_DAYS, seconds=1
    )
    assert store.device_journeys(DEVICE) == [], "an abandoned registration must not live forever"


def test_a_journey_list_renews_at_most_once_a_week_however_often_the_page_polls(_mp):
    """The cut, from the caller's side, with the arithmetic the constant is sized on.

    `list_journeys` is polled every 60 s by an open PWA, so a renewal on every read would be one
    write per registration per minute per open app -- and at a hundred thousand commuters those
    writes all land in the mirror's watermark query as well. What the week buys is that the poll
    costs nothing: no write, and no read beyond the one the list already made.
    """
    store, client = _store()
    clock = {"now": NOW}
    _mp.setattr(notification_store, "_clock", lambda: clock["now"])
    store.put_device(DEVICE, subscription=SUB, brand="entur")
    for i in range(3):
        store.create_journey(f"j{i}", _journey())
    settled = len(client.transactions)

    for minute in range(10):
        clock["now"] = NOW + timedelta(minutes=minute)
        assert len(store.device_journeys(DEVICE)) == 3
    assert len(client.transactions) == settled, "a 60 s poll wrote to the store"

    clock["now"] = NOW + timedelta(days=notification_store.WEB_PUSH_RENEW_AFTER_DAYS, seconds=1)
    store.device_journeys(DEVICE)
    assert len(client.transactions) - settled == 4, "three registrations and one device document"
    store.device_journeys(DEVICE)
    assert len(client.transactions) - settled == 4, "the renewal repeated on the very next read"


# ---------------------------------------------------------------- togvarsel round markers
def test_the_round_markers_are_written_to_one_store_document_nested_by_area(_mp):
    """Under the store backend a marker write must NOT go through `save_state()`.

    That is the trap, and it is invisible: `save_state` serializes the blob GLOBALS, which under
    this backend are empty because every mutator writes Firestore instead — so a marker write
    through it would replace the export's rollback artefact with an empty registration set. The
    assertion that nothing touched the blob is therefore the point of this test, not a detail of it.

    The body is nested by AREA with one area in it. Rushvarsel is one rush geography today and is
    planned to become several; nested from the start, that split is a data change here.
    """
    _reset()
    _mp.setattr(varsel_state, "_rounds_in_store", False)
    varsel_state.configure_rounds(in_store=True)
    written: list[dict] = []
    saved: list[int] = []
    _mp.setattr(notification_store, "write_rounds", lambda data, **kw: written.append(dict(data)))
    _mp.setattr(varsel_state, "save_state", lambda: (saved.append(1), True)[1])

    varsel_state.store_rounds("2026-09-14", {"0630": {"level": "yellow", "pushed": True}})
    assert varsel_state.save_rounds() is True
    assert written == [
        {
            "date": "2026-09-14",
            "areas": {varsel_state.ROUND_AREA: {"0630": {"level": "yellow", "pushed": True}}},
        }
    ]
    assert not saved, "a marker write reached the state blob, which under `store` is empty"


def test_the_round_markers_load_back_from_the_store_and_ignore_another_areas_key(_mp):
    """A build that knows one area reads its own key and leaves the rest alone — which is what
    makes a rollback ACROSS the rush-area split safe, rather than one that merges a neighbouring
    area's rounds into this one's dedup."""
    _reset()
    _mp.setattr(varsel_state, "_rounds_in_store", False)
    varsel_state.configure_rounds(in_store=True)
    body = {
        "date": "2026-09-14",
        "areas": {
            varsel_state.ROUND_AREA: {"0630": {"level": "red", "pushed": True}},
            "somewhere-else": {"0630": {"level": "green", "pushed": False}},
        },
    }
    _mp.setattr(notification_store, "read_rounds", lambda: dict(body))

    varsel_state.load_rounds()
    assert varsel_state.rounds_for("2026-09-14") == {"0630": {"level": "red", "pushed": True}}
    assert varsel_state.rounds_for("2026-09-13") == {}, "only the stored date"
    assert varsel_state.all_rounds() == {"2026-09-14": {"0630": {"level": "red", "pushed": True}}}


def test_the_round_markers_go_through_the_blob_when_the_backend_is_gcs(_mp):
    """The default and what every deployment did before the store backend existed: the markers are
    one key of the state object and `save_state()` writes them with the rest of it. The store must
    not be reached at all — including on load, where the blob's own `load_state` has applied them."""
    _reset()
    # Poisoned first, so the assertions below actually depend on `configure_rounds` doing
    # something: started from the module default they would pass with the call deleted.
    _mp.setattr(varsel_state, "_rounds_in_store", True)
    varsel_state.configure_rounds(in_store=False)
    calls: list[int] = []
    _mp.setattr(varsel_state, "save_state", lambda: (calls.append(1), True)[1])

    def _refuse(*_a, **_k):
        raise AssertionError("the store was touched under the blob backend")

    _mp.setattr(notification_store, "write_rounds", _refuse)
    _mp.setattr(notification_store, "read_rounds", _refuse)

    varsel_state.store_rounds("2026-09-14", {"0630": {"level": "yellow", "pushed": True}})
    assert varsel_state.save_rounds() is True
    varsel_state.load_rounds()
    assert calls == [1]
    assert varsel_state._togvarsel_rounds == {
        "2026-09-14": {"0630": {"level": "yellow", "pushed": True}}
    }


def test_a_round_marker_store_failure_is_logged_rather_than_raised(_mp):
    """Best-effort on both backends. A raise on the write would reach `togvarsel_notify`'s round
    loop and cost the round its decision; a raise on the read would fail the lifespan for a feature
    that is off in every environment. What an empty map costs is that rounds still inside their
    timeliness window are decided again — the crash window that module already documents."""
    _reset()
    _mp.setattr(varsel_state, "_rounds_in_store", False)
    varsel_state.configure_rounds(in_store=True)

    def _boom(*_a, **_k):
        raise RuntimeError("firestore is having a moment")

    _mp.setattr(notification_store, "write_rounds", _boom)
    _mp.setattr(notification_store, "read_rounds", _boom)

    varsel_state.store_rounds("2026-09-14", {"0630": {"level": "yellow", "pushed": True}})
    assert varsel_state.save_rounds() is False, "a failed write must SAY so"
    assert varsel_state.rounds_for("2026-09-14") == {"0630": {"level": "yellow", "pushed": True}}, (
        "the in-memory markers still dedup this process"
    )
    varsel_state.load_rounds()  # must not raise


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
        try:
            t(mp)
        finally:
            mp.undo()
        print(f"  ok  {t.__name__}")
    os.unlink(_STATE_PATH)
    print(f"\n{len(tests)} registration-seam checks passed.")
