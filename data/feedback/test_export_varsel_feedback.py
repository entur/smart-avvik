from __future__ import annotations

import importlib.util
import json
import os
import uuid
from datetime import date
from pathlib import Path
from urllib.error import HTTPError

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
    outside["context_date"] = "2026-08-24"
    rows, errors = export.collapse_events([outside, {"_malformed": "bad row"}], environment="dev", start=date(2026, 8, 26), end=date(2026, 8, 27))
    assert rows == []
    assert len(errors) == 2


def test_previous_day_context_is_valid_for_revision_ingested_after_midnight():
    previous = event(revision=2)
    previous["context_date"] = "2026-08-25"
    rows, errors = export.collapse_events(
        [previous], environment="dev", start=date(2026, 8, 26), end=date(2026, 8, 27)
    )
    assert rows == [previous]
    assert errors == []


def test_bigquery_project_identifier_is_validated_before_query():
    for project in ("bad`project", "bad project", "x;select", "UPPERCASE"):
        try:
            export.fetch_contexts(
                project=project,
                context_ids=["a" * 32],
                start=date(2026, 8, 26),
                end=date(2026, 8, 27),
                client=object(),
            )
        except ValueError as exc:
            assert "project ID" in str(exc)
        else:
            raise AssertionError("unsafe project identifier accepted")


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
    assert "event IN ('varsel_alert_feedback', 'rush_alert_feedback')" in query
    assert "properties.environment = 'prd'" in query
    assert "timestamp >=" in query and "timestamp <" in query
    assert f"LIMIT {export.MAX_EVENTS + 1}" in query


def test_hogql_uses_hogql_functions_and_not_clickhouse_ones():
    # Nothing offline can validate HogQL, so this pins the constructs a real query already refused
    # rather than pretending to check the dialect. toInt64 is ClickHouse's; HogQL rejects the whole
    # query with a 400 ("Perhaps you meant 'toInt(...)'?"), which cost a release-gate run on
    # 2026-08-28 because the only assertions here were about bounds.
    query = export._hogql("dev", date(2026, 8, 1), date(2026, 8, 3))
    assert "toInt64" not in query
    assert "toInt(properties.revision)" in query


def test_hogql_pins_every_timestamp_to_utc_and_the_context_date_to_a_day():
    # PostHog types a date-ish property as DateTime in the PROJECT timezone, so an unwrapped
    # projection returns "2026-08-27T00:00:00+02:00" for a stored "2026-08-27" — which
    # date.fromisoformat rejects, dropping every response as one malformed_event. toDate truncates
    # in the coercion's own zone and recovers the stored day; the instants are pinned to UTC so an
    # exported document does not change value when somebody edits a PostHog project setting.
    query = export._hogql("dev", date(2026, 8, 1), date(2026, 8, 3))
    assert "toString(toDate(properties.context_date))" in query
    assert "toString(toTimeZone(timestamp, 'UTC'))" in query
    assert "toString(toTimeZone(properties.submitted_at, 'UTC'))" in query
    # A bare projection of either instant would reintroduce the project-timezone dependency.
    assert "\ntimestamp," not in query
    assert "properties.submitted_at\n" not in query


def test_the_query_is_never_served_from_the_api_cache():
    # PostHog caches a result per query text (measured: is_cached=true on a repeat). A reviewer
    # re-running a window after more feedback arrived would otherwise get the earlier answer and read
    # it as "nothing new"; and during the ~50 s ingestion delay one early empty read would be cached
    # and repeated by every retry.
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"results": []}'

    def opener(request, timeout=None):
        seen["body"] = json.loads(request.data.decode())
        return Response()

    export.fetch_posthog(project_id=1, environment="dev", start=date(2026, 8, 1),
                         end=date(2026, 8, 2), token="phx_x", opener=opener)
    assert seen["body"]["refresh"] == "force_blocking"


def test_a_refused_query_carries_the_api_reason_not_just_the_status():
    # urllib hides the body behind "HTTP Error 400: Bad Request", and the body is where PostHog puts
    # the reason — the entire diagnosis, in the one place a traceback does not reach.
    class Refused(HTTPError):
        def __init__(self):
            super().__init__("https://eu.posthog.com", 400, "Bad Request", {}, None)

        def read(self):
            return b'{"code":"hogql_query_error","detail":"Unsupported function call'

    def opener(request, timeout=None):
        raise Refused()

    try:
        export.fetch_posthog(project_id=1, environment="dev", start=date(2026, 8, 1),
                             end=date(2026, 8, 2), token="phx_x", opener=opener)
    except RuntimeError as exc:
        assert "hogql_query_error" in str(exc)
        assert "400" in str(exc)
    else:
        raise AssertionError("a refused query must not pass silently")


def stat_mode(path):
    return os.stat(path).st_mode & 0o777


def _run():
    # CI executes each suite as `python <suite>.py`, not under pytest, so without this the file
    # imports cleanly, collects nothing and exits 0 -- a green tick over an unrun suite. A suite
    # that prints no "N checks passed" line did not run.
    import inspect
    import tempfile

    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        if "tmp_path" in inspect.signature(fn).parameters:
            # A fresh directory per test, as pytest's fixture gives: two of these write the same
            # file name and assert that a second write is refused.
            with tempfile.TemporaryDirectory() as directory:
                fn(Path(directory))
        else:
            fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} feedback-export checks passed.")


if __name__ == "__main__":
    _run()
