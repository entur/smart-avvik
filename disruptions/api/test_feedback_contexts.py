from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import feedback_contexts


class _Job:
    def result(self, timeout=None):
        return []


class FakeBQ:
    def __init__(self):
        self.rows = []

    def query(self, sql):
        assert "CREATE TABLE IF NOT EXISTS" in sql
        assert "partition_expiration_days=45" in sql
        return _Job()

    def insert_rows_json(self, table, rows, row_ids=None, timeout=None):
        assert timeout == 5
        # Stand in for what the real table enforces. insertAll reads a native dict against a JSON
        # column as a record and rejects the row, so a double that accepts any shape lets a write
        # path that fails against every real environment stay green (it did, until 2026-08-27).
        for row in rows:
            for column in feedback_contexts._JSON_COLUMNS:
                value = row.get(column)
                assert value is None or isinstance(value, str), f"{column} must be JSON text or None"
        self.rows.append((table, rows, row_ids))
        return []


class FakeStore:
    """`notification_store`'s four feedback-context calls over a dict, injected the way `main` does.

    A stand-in for the module rather than for Firestore: the bodies themselves run against the
    fake client in test_notification_store, and against the emulator there. What this suite checks
    is what `feedback_contexts` does with the answers -- which call it makes, when, and what it does
    when one raises.
    """

    def __init__(self):
        self.docs = {}
        self.writes = 0
        self.attempts = 0
        self.fail_writes = False
        self.fail_reads = False

    def write_feedback_context(self, context_id, *, snapshot, device_id, journey_id, expires_at):
        self.attempts += 1
        if self.fail_writes:
            raise RuntimeError("firestore unavailable")
        self.writes += 1
        self.docs[context_id] = {
            "snapshot": snapshot,
            "device_id": device_id,
            "journey_id": journey_id,
            "expires_at": expires_at,
            "claims": {},
        }

    def _live(self, context_id):
        doc = self.docs.get(context_id)
        return doc if doc is not None and doc["expires_at"] > datetime.now(UTC) else None

    def read_feedback_context(self, context_id):
        if self.fail_reads:
            raise RuntimeError("firestore unavailable")
        doc = self._live(context_id)
        return doc["snapshot"] if doc else None

    def claim_feedback_response(self, context_id, *, response_id):
        doc = self._live(context_id)
        if doc is None:
            return "missing"
        if response_id in doc["claims"]:
            return "duplicate"
        doc["claims"][response_id] = True
        return "claimed"

    def release_feedback_response(self, context_id, *, response_id):
        doc = self.docs.get(context_id)
        if doc is not None:
            doc["claims"].pop(response_id, None)

    def erase(self, device_id):
        """What `_erase_device_body` does to this collection: the device's documents, no others."""
        for context_id in [c for c, d in self.docs.items() if d["device_id"] == device_id]:
            del self.docs[context_id]


def _fresh_process(store, bq_client=None):
    """A second pod: no memory of any rendering, the same store."""
    feedback_contexts._reset_for_tests()
    feedback_contexts.configure(bq_client=bq_client, store=store)


_CARD = {
    "display_source": "summary",
    "displayed_text": "Forsinkelse",
    "journey_context": {"from_stop": "NSR:StopPlace:337", "from_name": "Lier"},
}


def setup_function():
    feedback_contexts._reset_for_tests()


def test_identical_snapshot_reuses_live_capability_and_changed_text_does_not():
    snapshot = {"display_source": "summary", "displayed_text": "Forsinkelse", "prediction": {"x": 1}}
    first = feedback_contexts.mint(snapshot)
    assert feedback_contexts.mint(dict(snapshot)) == first
    assert feedback_contexts.mint({**snapshot, "displayed_text": "Innstilt"}) != first


def test_persist_is_self_contained_and_uses_stable_insert_id():
    fake = FakeBQ()
    feedback_contexts.configure(bq_client=fake)
    assert feedback_contexts.ensure_table()
    context_id = feedback_contexts.mint({
        "display_source": "summary",
        "displayed_text": "Hei",
        "policy": {"alertworthy": False},
        "message_input": None,
    })
    assert feedback_contexts.persist(context_id) == (True, None)
    _, rows, ids = fake.rows[0]
    assert ids == [context_id]
    assert rows[0]["displayed_text"] == "Hei"
    assert rows[0]["context_id"] == context_id
    # The nested snapshot arrives as JSON text; an absent one stays SQL NULL rather than JSON null.
    assert json.loads(rows[0]["policy"]) == {"alertworthy": False}
    assert rows[0]["message_input"] is None
    assert feedback_contexts.persist(context_id) == (True, None)
    assert len(fake.rows) == 1


def test_unknown_context_never_infers_a_row():
    assert feedback_contexts.persist("0" * 32) == (False, "expired")


def test_a_context_minted_on_one_pod_resolves_on_another_without_the_device_id():
    store = FakeStore()
    _fresh_process(store)
    context_id = feedback_contexts.mint(_CARD, device_id="device-1")
    assert store.writes == 1
    _fresh_process(store)
    resolved = feedback_contexts.resolve(context_id)
    assert resolved == _CARD
    assert "device_id" not in resolved


def test_an_expired_or_erased_document_does_not_resolve_even_from_memory():
    store = FakeStore()
    _fresh_process(store)
    expired = feedback_contexts.mint(_CARD, device_id="device-1")
    store.docs[expired]["expires_at"] = datetime.now(UTC) - timedelta(seconds=1)
    assert feedback_contexts.resolve(expired) is None
    erased = feedback_contexts.mint({**_CARD, "displayed_text": "Innstilt"}, device_id="device-1")
    store.erase("device-1")
    # This pod's memory still holds the entry; the store is what an erasure on another pod reached.
    assert erased in feedback_contexts._by_id
    assert feedback_contexts.resolve(erased) is None


def test_reuse_is_per_device_and_costs_no_second_write():
    store = FakeStore()
    _fresh_process(store)
    first = feedback_contexts.mint(_CARD, device_id="device-1")
    assert feedback_contexts.mint(dict(_CARD), device_id="device-1") == first
    assert store.writes == 1
    other = feedback_contexts.mint(dict(_CARD), device_id="device-2")
    assert other != first, "two travellers must never share a context"
    assert store.writes == 2


def test_reuse_never_outlives_the_document_it_names():
    """Memory SLIDES a reused entry's lease; the document's `expires_at` does not move, so a slid
    entry would hand out an id every pod refuses as expired."""
    store = FakeStore()
    _fresh_process(store)
    first = feedback_contexts.mint(_CARD, device_id="device-1")
    expires, fingerprint, snap = feedback_contexts._by_id[first]
    # Past half the lease: what is left is too little to promise a tap on a reused id.
    feedback_contexts._by_id[first] = (expires - feedback_contexts._LEASE_S * 0.6, fingerprint, snap)
    second = feedback_contexts.mint(dict(_CARD), device_id="device-1")
    assert second != first and store.writes == 2


def test_a_rush_card_without_a_device_resolves_on_another_pod():
    store = FakeStore()
    _fresh_process(store)
    context_id = feedback_contexts.mint({"display_source": "togvarsel", "displayed_text": "Gult"})
    assert store.docs[context_id]["device_id"] is None
    _fresh_process(store)
    assert feedback_contexts.resolve(context_id)["display_source"] == "togvarsel"


def test_a_failed_store_write_still_renders_a_memory_only_context():
    store = FakeStore()
    store.fail_writes = True
    _fresh_process(store)
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    feedback_contexts.logger.addHandler(handler)
    try:
        context_id = feedback_contexts.mint(_CARD, device_id="device-1")
    finally:
        feedback_contexts.logger.removeHandler(handler)
    assert context_id is not None
    assert len(records) == 1, records
    # It resolves and claims on THIS pod, from memory, because the store never held it.
    assert feedback_contexts.resolve(context_id) == _CARD
    assert feedback_contexts.claim(context_id, "r-1") == "claimed"


def test_a_failed_store_write_spares_the_rest_of_the_render_its_deadline():
    """`/varsel-api/journeys` mints per alert, serially: once one write has failed, the next ones
    on this pod go memory-only at once rather than each waiting out the store's deadline."""
    store = FakeStore()
    store.fail_writes = True
    _fresh_process(store)
    first = feedback_contexts.mint(_CARD, device_id="device-1")
    second = feedback_contexts.mint({**_CARD, "displayed_text": "Innstilt"}, device_id="device-1")
    assert first and second and store.attempts == 1
    assert feedback_contexts.resolve(second) == _CARD | {"displayed_text": "Innstilt"}
    # Past the back-off the store is asked again.
    store.fail_writes = False
    feedback_contexts._store_down_until = 0.0
    third = feedback_contexts.mint({**_CARD, "displayed_text": "Gult"}, device_id="device-1")
    assert store.attempts == 2 and third in store.docs


def test_a_store_read_failure_is_not_an_expiry():
    """A blip is retryable; "expired" tells the traveller the card is gone, so they never retry."""
    store = FakeStore()
    fake = FakeBQ()
    _fresh_process(store, bq_client=fake)
    context_id = feedback_contexts.mint(_CARD, device_id="device-1")
    store.fail_reads = True
    try:
        feedback_contexts.resolve(context_id)
    except feedback_contexts.StoreUnavailable:
        pass
    else:
        raise AssertionError("a failed read resolved")
    assert feedback_contexts.persist(context_id) == (False, "downstream")
    assert fake.rows == []


def test_a_full_cache_still_mints_under_the_store():
    """Per-device fingerprints multiply live contexts. Under the store a full memory cache only
    costs reuse: the document is written and the widget still renders, served from the store."""
    store = FakeStore()
    _fresh_process(store)
    bound = feedback_contexts._MAX_CONTEXTS
    feedback_contexts._MAX_CONTEXTS = 1
    try:
        first = feedback_contexts.mint(_CARD, device_id="device-1")
        overflow = feedback_contexts.mint(_CARD, device_id="device-2")
        assert overflow is not None and overflow != first
        assert overflow in store.docs and overflow not in feedback_contexts._by_id
        assert feedback_contexts.resolve(overflow) == _CARD
        # With the store failing too there is nowhere to hold it, so no widget -- as before.
        store.fail_writes = True
        assert feedback_contexts.mint(_CARD, device_id="device-3") is None
    finally:
        feedback_contexts._MAX_CONTEXTS = bound


def test_a_full_cache_without_the_store_still_refuses():
    bound = feedback_contexts._MAX_CONTEXTS
    feedback_contexts._MAX_CONTEXTS = 1
    try:
        assert feedback_contexts.mint(_CARD, device_id="device-1") is not None
        assert feedback_contexts.mint(_CARD, device_id="device-2") is None
    finally:
        feedback_contexts._MAX_CONTEXTS = bound


def test_the_journey_a_card_belongs_to_reaches_the_store_and_never_the_snapshot():
    store = FakeStore()
    _fresh_process(store)
    context_id = feedback_contexts.mint(_CARD, device_id="device-1", journey_id="j1")
    assert store.docs[context_id]["journey_id"] == "j1"
    assert feedback_contexts.resolve(context_id) == _CARD
    # The same card on another journey of the same device is that journey's own context.
    assert feedback_contexts.mint(dict(_CARD), device_id="device-1", journey_id="j2") != context_id


def test_without_the_store_the_memory_path_is_todays():
    """Gate off (tst, local runs, every other suite): no store call, the lease slides, claims are
    free -- whatever database name the environment carries, because the gate is the injection."""
    assert feedback_contexts._store is None
    first = feedback_contexts.mint(_CARD, device_id="device-1")
    assert feedback_contexts.resolve(first) == _CARD
    assert feedback_contexts.claim(first, "r-1") == "claimed"
    assert feedback_contexts.claim(first, "r-1") == "claimed"


def test_persist_never_writes_the_device_id():
    store = FakeStore()
    fake = FakeBQ()
    _fresh_process(store)
    context_id = feedback_contexts.mint(_CARD, device_id="device-1")
    _fresh_process(store, bq_client=fake)
    assert feedback_contexts.persist(context_id) == (True, None)
    _, rows, _ = fake.rows[0]
    assert "device_id" not in rows[0]
    assert "device-1" not in json.dumps(rows[0])


def test_main_injects_the_store_on_the_lease_gate_never_on_a_named_database():
    """KTD5/KTD10: tst names a database it does not have, so `notification_store.enabled()` is
    true there. The store path follows `_varsel_store`, the registration backend the lease lives
    in -- and it is configured AFTER `_registration_store()` has had the chance to set it."""
    source = (Path(__file__).parent / "main.py").read_text()
    head, tail = source.split("feedback_contexts.configure(", 1)
    call = tail.split("\n    feedback_contexts.ensure_table()", 1)[0]
    # The code, not the comment beside it -- which names `enabled()` in order to refuse it.
    call = "\n".join(line for line in call.splitlines() if not line.strip().startswith("#"))
    assert "_varsel_store is not None" in call, call
    assert "enabled()" not in call, call
    assert "registrations=_registration_store()" in head


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        # pytest's hook, called here too: the store-backed checks leave a store configured, and a
        # memory-path check after them would otherwise run against it.
        setup_function()
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} feedback-context checks passed.")


if __name__ == "__main__":
    _run()
