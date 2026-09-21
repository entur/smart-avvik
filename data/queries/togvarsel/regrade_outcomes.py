"""Re-grade settled rush outcomes under the CURRENT label rule, one environment at a time.

Why this exists: the authoritative pass never rewrites a settled row (both MERGEs guard on
`NOT settled`, and the work set reaches past the 10-day retry window only for days with NO row),
so a change to the label rule -- 2026-09-21, docs/rushvarsel/04-cuts: planned trains that never
ran count as cancelled -- leaves every already-settled day graded on the old rule for good.
This script re-opens a date range and runs the module's OWN settle path over it, so there is one
definition of the rule and this file holds none of it.

    uv run python data/queries/togvarsel/regrade_outcomes.py --project ent-kixavvik-dev \
        --from 2025-08-01 --to 2026-09-20            # dry run: prints what would change
    uv run python data/queries/togvarsel/regrade_outcomes.py --project ent-kixavvik-dev \
        --from 2025-08-01 --to 2026-09-20 --apply    # un-settle the range, then re-settle it

What it touches: `trouble10`, `level`, `n_*`, `eligible`, `unresolvable`, `resolved_at` on the
rows in the range, exactly as the nightly pass writes them. The provisional_* trio is never
touched (the settled MERGE leaves it alone by design). Days with no issued forecast get no row,
as always: the work set is read from `togvarsel_issued`.

Cost: the settled query per 14-day chunk is ~2 GB (the GTFS side), so a 13-month range is
~30 chunks, about $0.40. Runs under your ADC; the deployed app is not involved and nothing needs
to be redeployed for this -- but run it AFTER the code carrying the rule is what you want the
page to show, or the next nightly pass on an un-settled day writes the old rule back.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "disruptions" / "api"))
import togvarsel_outcomes as outcomes  # noqa: E402


def _issued_days(client, start: date, end: date) -> list[date]:
    from google.cloud import bigquery

    rows = client.query(
        "SELECT DISTINCT d FROM `disruptions.togvarsel_issued` WHERE d BETWEEN @a AND @b ORDER BY d",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("a", "DATE", start),
                bigquery.ScalarQueryParameter("b", "DATE", end),
            ]
        ),
    ).result()
    return [r["d"] for r in rows]


def _levels(client, start: date, end: date) -> dict[tuple[date, str], tuple[float | None, str | None]]:
    from google.cloud import bigquery

    rows = client.query(
        "SELECT d, scope, trouble10, level FROM `disruptions.togvarsel_outcomes` WHERE d BETWEEN @a AND @b",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("a", "DATE", start),
                bigquery.ScalarQueryParameter("b", "DATE", end),
            ]
        ),
    ).result()
    return {(r["d"], r["scope"]): (r["trouble10"], r["level"]) for r in rows}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True)
    ap.add_argument("--from", dest="start", required=True, type=date.fromisoformat)
    ap.add_argument("--to", dest="end", required=True, type=date.fromisoformat)
    ap.add_argument("--apply", action="store_true", help="write; without it only the current rows are listed")
    args = ap.parse_args()
    if args.end >= date.today():
        ap.error("--to must be a past day: today is only ever provisional")

    from google.cloud import bigquery

    client = bigquery.Client(project=args.project)
    outcomes.configure(bq_client=client)
    days = _issued_days(client, args.start, args.end)
    before = _levels(client, args.start, args.end)
    print(f"{len(days)} issued days in {args.start}..{args.end}; {len(before)} outcome rows today")
    if not args.apply:
        print("dry run: pass --apply to un-settle and re-settle them")
        return

    client.query(
        "UPDATE `disruptions.togvarsel_outcomes` SET settled = FALSE, unresolvable = FALSE "
        "WHERE d BETWEEN @a AND @b",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("a", "DATE", args.start),
                bigquery.ScalarQueryParameter("b", "DATE", args.end),
            ]
        ),
    ).result()
    outcomes._run_settled(days, datetime.now(timezone.utc))
    after = _levels(client, args.start, args.end)
    changed = [(k, before.get(k), after.get(k)) for k in sorted(after) if before.get(k, (None, None))[1] != after[k][1]]
    print(f"re-settled {len(days)} days; level changed on {len(changed)} (day, scope) rows:")
    for (d, scope), old, new in changed:
        print(f"  {d} {scope}: {old[1] if old else None} {old[0] if old else None} -> {new[1]} {new[0]}")


if __name__ == "__main__":
    main()
