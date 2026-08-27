#!/usr/bin/env python3
"""Export personless varsel feedback enriched with its exact BigQuery context."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

EVENT = "varsel_alert_feedback"
QUERY_HOST = "https://eu.posthog.com"
TOKEN_ENV = "POSTHOG_PERSONAL_API_KEY"
MAX_EVENTS = 10_000
ENVIRONMENTS = ("dev", "tst", "prd")
_HEX_ID = re.compile(r"^[0-9a-f]{32}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)
_PROJECT_ID = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
EVENT_COLUMNS = (
    "event_timestamp", "environment", "context_id", "context_date", "response_id",
    "revision", "event_id", "rating", "comment", "submitted_at",
)


def _iso_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from exc


def _hogql(environment: str, start: date, end: date) -> str:
    """Build from parsed/allow-listed values only; no raw user string reaches HogQL."""
    start_ts = datetime.combine(start, time.min, tzinfo=UTC).isoformat()
    end_ts = datetime.combine(end, time.min, tzinfo=UTC).isoformat()
    return f"""SELECT
timestamp, properties.environment, properties.context_id, properties.context_date,
properties.response_id, toInt64(properties.revision), properties.event_id,
properties.rating, properties.comment, properties.submitted_at
FROM events
WHERE event = '{EVENT}'
  AND timestamp >= toDateTime('{start_ts}')
  AND timestamp < toDateTime('{end_ts}')
  AND properties.environment = '{environment}'
ORDER BY timestamp, uuid
LIMIT {MAX_EVENTS + 1}"""


def fetch_posthog(
    *, project_id: int, environment: str, start: date, end: date, token: str,
    opener: Callable[..., Any] = urlopen,
) -> list[dict[str, Any]]:
    payload = json.dumps({"query": {"kind": "HogQLQuery", "query": _hogql(environment, start, end)}}).encode()
    request = Request(
        f"{QUERY_HOST}/api/projects/{project_id}/query/",
        data=payload,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with opener(request, timeout=30) as response:
        result = json.load(response)
    rows = result.get("results")
    if not isinstance(rows, list):
        raise TypeError("PostHog Query API returned no result rows")
    if len(rows) > MAX_EVENTS:
        raise RuntimeError(f"window exceeds the {MAX_EVENTS}-event completeness limit; use a narrower window")
    events: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(EVENT_COLUMNS):
            events.append({"_malformed": "unexpected PostHog row shape", "raw": row})
        else:
            events.append(dict(zip(EVENT_COLUMNS, row, strict=True)))
    return events


def collapse_events(
    events: Iterable[dict[str, Any]], *, environment: str, start: date, end: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        try:
            if event.get("_malformed"):
                raise ValueError(str(event["_malformed"]))
            context_id, response_id, event_id = (str(event[k]) for k in ("context_id", "response_id", "event_id"))
            revision = event["revision"]
            context_date = date.fromisoformat(str(event["context_date"]))
            if event.get("environment") != environment:
                raise ValueError("environment mismatch")
            if not _HEX_ID.fullmatch(context_id) or not _UUID.fullmatch(response_id) or not _UUID.fullmatch(event_id):
                raise ValueError("invalid identifier")
            if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
                raise ValueError("invalid revision")
            if event.get("rating") not in ("up", "down"):
                raise ValueError("invalid rating")
            if not start - timedelta(days=1) <= context_date < end:
                raise ValueError("context date outside requested horizon")
            key = (environment, context_id, response_id)
            rank = (revision, event_id)
            previous = latest.get(key)
            if previous is None or rank > (previous["revision"], previous["event_id"]):
                latest[key] = event
        except (KeyError, TypeError, ValueError) as exc:
            errors.append({"kind": "malformed_event", "index": index, "reason": str(exc)})
    return sorted(latest.values(), key=lambda e: (e["context_date"], e["context_id"], e["response_id"])), errors


def fetch_contexts(
    *, project: str, context_ids: list[str], start: date, end: date, client: Any | None = None,
) -> list[dict[str, Any]]:
    if not _PROJECT_ID.fullmatch(project):
        raise ValueError("invalid Google Cloud project ID")
    if not context_ids:
        return []
    if client is None:
        from google.cloud import bigquery
        client = bigquery.Client(project=project)
    else:
        bigquery = __import__("google.cloud.bigquery", fromlist=["bigquery"])
    query = f"""SELECT * FROM `{project}.disruptions.varsel_feedback_contexts`
WHERE context_date >= @from_date AND context_date < @to_date
AND context_id IN UNNEST(@context_ids)"""
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("from_date", "DATE", start),
        bigquery.ScalarQueryParameter("to_date", "DATE", end),
        bigquery.ArrayQueryParameter("context_ids", "STRING", context_ids),
    ])
    return [dict(row.items()) for row in client.query(query, job_config=config).result()]


def enrich(
    events: list[dict[str, Any]], contexts: Iterable[dict[str, Any]], errors: list[dict[str, Any]],
    *, retained_since: date | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in contexts:
        grouped[str(row.get("context_id"))].append(row)
    resolved: dict[str, dict[str, Any]] = {}
    conflicting_context_ids: set[str] = set()
    for context_id, rows in grouped.items():
        # Retry-created duplicates may legitimately differ only in their insertion timestamp.
        canonical = {
            json.dumps({key: value for key, value in row.items() if key != "persisted_at"},
                       sort_keys=True, default=str, ensure_ascii=False)
            for row in rows
        }
        if len(canonical) == 1:
            resolved[context_id] = rows[0]
            if len(rows) > 1:
                errors.append({"kind": "identical_duplicate_context", "context_id": context_id, "count": len(rows)})
        else:
            conflicting_context_ids.add(context_id)
            errors.append({"kind": "conflicting_duplicate_context", "context_id": context_id, "count": len(rows)})
    output = []
    for event in events:
        context_id = event["context_id"]
        context = resolved.get(context_id)
        if context is None:
            if context_id not in conflicting_context_ids:
                kind = "expired_context" if retained_since and date.fromisoformat(event["context_date"]) < retained_since else "missing_context"
                errors.append({"kind": kind, "context_id": context_id, "response_id": event["response_id"]})
            output.append({"feedback": event, "context": None})
        else:
            output.append({"feedback": event, "context": context})
    return output, errors


def write_json(path: Path, document: dict[str, Any]) -> None:
    if str(path) == "-":
        raise ValueError("stdout output is not allowed")
    if path.is_symlink() or path.exists():
        raise FileExistsError(f"refusing to overwrite or follow {path}")
    if not path.parent.is_dir():
        raise FileNotFoundError(f"output directory does not exist: {path.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=True, indent=2, default=str)
            handle.write("\n")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--project", required=True, help="GCP project containing the context table")
    result.add_argument("--environment", required=True, choices=ENVIRONMENTS)
    result.add_argument("--from-date", required=True, type=_iso_day, help="inclusive UTC date")
    result.add_argument("--to-date", required=True, type=_iso_day, help="exclusive UTC date")
    result.add_argument("--posthog-project-id", required=True, type=int)
    result.add_argument("--output", required=True, type=Path)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.from_date >= args.to_date or (args.to_date - args.from_date).days > 31:
        raise SystemExit("date window must be 1-31 days (to-date is exclusive)")
    token = os.environ.get(TOKEN_ENV)
    if not token:
        raise SystemExit(f"{TOKEN_ENV} must contain a read-only PostHog personal API key")
    events, errors = collapse_events(
        fetch_posthog(project_id=args.posthog_project_id, environment=args.environment,
                      start=args.from_date, end=args.to_date, token=token),
        environment=args.environment, start=args.from_date, end=args.to_date,
    )
    context_ids = sorted({event["context_id"] for event in events})
    contexts = fetch_contexts(project=args.project, context_ids=context_ids,
                              start=args.from_date - timedelta(days=1), end=args.to_date)
    rows, errors = enrich(events, contexts, errors, retained_since=datetime.now(UTC).date() - timedelta(days=45))
    write_json(args.output, {
        "metadata": {"environment": args.environment, "from_date": args.from_date,
                     "to_date_exclusive": args.to_date, "response_count": len(rows),
                     "issue_count": len(errors)},
        "responses": rows,
        "issues": errors,
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
