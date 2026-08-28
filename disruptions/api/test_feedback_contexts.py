from __future__ import annotations

import json

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
