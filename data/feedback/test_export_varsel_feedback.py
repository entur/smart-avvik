from __future__ import annotations

import importlib.util
import os
import uuid
from datetime import date
from pathlib import Path

MODULE = Path(__file__).with_name("export_varsel_feedback.py")
spec = importlib.util.spec_from_file_location("export_varsel_feedback", MODULE)
export = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(export)


def event(revision=1, *, context="a" * 32, response=None, event_id=None, comment="nyttig"):
    return {
        "event_timestamp": "2026-08-26T10:00:00Z", "environment": "dev",
        "context_id": context, "context_date": "2026-08-26",
        "response_id": response or str(uuid.uuid4()), "revision": revision,
        "event_id": event_id or str(uuid.uuid4()), "rating": "up", "comment": comment,
        "submitted_at": "2026-08-26T10:00:00Z",
    }


def test_out_of_order_history_collapses_to_highest_complete_revision():
    response = str(uuid.uuid4())
    rows = [event(3, response=response, comment="latest"), event(1, response=response, comment="old")]
    collapsed, errors = export.collapse_events(rows, environment="dev", start=date(2026, 8, 26), end=date(2026, 8, 27))
    assert errors == []
    assert len(collapsed) == 1
    assert collapsed[0]["comment"] == "latest"


def test_enrichment_never_guesses_missing_or_conflicting_context():
    missing, conflict = event(context="a" * 32), event(context="b" * 32)
    contexts = [
        {"context_id": "b" * 32, "displayed_text": "one"},
        {"context_id": "b" * 32, "displayed_text": "two"},
    ]
    rows, errors = export.enrich([missing, conflict], contexts, [])
    assert [row["context"] for row in rows] == [None, None]
    assert {error["kind"] for error in errors} == {"missing_context", "conflicting_duplicate_context"}


def test_identical_duplicate_is_resolved_but_reported():
    feedback = event()
    context = {"context_id": "a" * 32, "displayed_text": "exact"}
    rows, errors = export.enrich([feedback], [context, dict(context)], [])
    assert rows[0]["context"]["displayed_text"] == "exact"
    assert errors == [{"kind": "identical_duplicate_context", "context_id": "a" * 32, "count": 2}]


def test_retry_duplicate_may_differ_only_by_persistence_timestamp():
    feedback = event()
    first = {"context_id": "a" * 32, "displayed_text": "exact", "persisted_at": "one"}
    second = {**first, "persisted_at": "two"}
    rows, errors = export.enrich([feedback], [first, second], [])
    assert rows[0]["context"] == first
    assert errors[0]["kind"] == "identical_duplicate_context"


def test_malformed_and_out_of_horizon_events_are_reported():
    outside = event()
    outside["context_date"] = "2026-08-25"
    rows, errors = export.collapse_events([outside, {"_malformed": "bad row"}], environment="dev", start=date(2026, 8, 26), end=date(2026, 8, 27))
    assert rows == []
    assert len(errors) == 2


def test_json_output_is_owner_only_non_overwriting_and_escapes_controls(tmp_path):
    target = tmp_path / "feedback.json"
    export.write_json(target, {"comment": "line\n\u202eevil\u0000"})
    assert stat_mode(target) == 0o600
    raw = target.read_text()
    assert "\\n" in raw and "\\u202e" in raw and "\\u0000" in raw
    try:
        export.write_json(target, {})
    except FileExistsError:
        pass
    else:
        raise AssertionError("overwrote an export")


def test_json_output_refuses_stdout_and_symlink(tmp_path):
    real = tmp_path / "real"
    real.write_text("keep")
    link = tmp_path / "link"
    link.symlink_to(real)
    for target in (Path("-"), link):
        try:
            export.write_json(target, {})
        except (ValueError, FileExistsError):
            pass
        else:
            raise AssertionError("unsafe output accepted")


def test_hogql_is_bounded_to_event_environment_and_dates():
    query = export._hogql("prd", date(2026, 8, 1), date(2026, 8, 3))
    assert "event = 'varsel_alert_feedback'" in query
    assert "properties.environment = 'prd'" in query
    assert "timestamp >=" in query and "timestamp <" in query
    assert f"LIMIT {export.MAX_EVENTS + 1}" in query


def stat_mode(path):
    return os.stat(path).st_mode & 0o777
