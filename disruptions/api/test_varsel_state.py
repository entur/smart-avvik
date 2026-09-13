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

_STATE_FD, _STATE_PATH = tempfile.mkstemp(prefix="varsel_state_seam_", suffix=".json")
os.close(_STATE_FD)
os.environ["VARSEL_STATE_PATH"] = _STATE_PATH

import varsel_state

NOW = datetime(2026, 9, 13, 8, 0, tzinfo=UTC)
DEVICE = "dev-abcdef12"
OTHER = "dev-99999999"
SUB = {"endpoint": "https://push.example.com/x", "keys": {"p256dh": "k", "auth": "a"}}


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
    return varsel_state.BlobRegistrations()


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
    assert store.journey_count() == 0
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
    assert store.journey_count() == 0 and store.device_count() == 0


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
    assert store.journey_count() == 4 and store.device_count() == 0


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
    assert store.journey_count() == 0


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
    """A rush round marker is not a registration. On the protocol, the store backend would have to
    grow a `togvarsel_rounds` document to satisfy an interface nothing would read it through."""
    declared = set(vars(varsel_state.RegistrationStore))
    assert not {n for n in declared if "round" in n}, "a round marker reached the registration seam"


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
