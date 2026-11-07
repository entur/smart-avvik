#!/usr/bin/env python3
"""Move a manifest's `move` rows from `disruptions` to `disruptions_obsolete`, or put some back.

Plan: docs/plans/2026-11-07-001-refactor-bigquery-obsolete-dataset-plan.md (U3, U4, U5).

    uv run python scripts/bq_obsolete_move.py --project ent-kixavvik-dev                  # dry run
    uv run python scripts/bq_obsolete_move.py --project ent-kixavvik-dev --only a b --apply
    uv run python scripts/bq_obsolete_move.py --project ent-kixavvik-dev --apply
    uv run python scripts/bq_obsolete_move.py --project ent-kixavvik-dev --restore --only a --apply

BigQuery has no move: each object is copied, the copy is checked, and only then is the source
deleted. A copy has to have the source's type, and a table's copy its row count and logical bytes.
The source is read again just before the delete and must be unchanged. A table with rows in its
streaming buffer is refused, because a copy leaves those rows out. Nothing is overwritten. A rerun
skips what is already moved and finishes an object that was copied but not deleted. One line per
object: `moved`, `skipped (<why>)` or `FAILED (<why>)`; the run keeps going after a failure, an
unexpected API error included, and exits 1 if there was one.

Refuses, before any copy or delete: a manifest whose file name names another environment than
`--project`, `--apply` while the manifest still has a `review` row, a `--only` name that is not a
`move` row, any row that is not a table or a model, and under `--apply` any row a keep root names
today (the inventory's roots are recomputed, because code merged after the manifest can name one).

A source modified after the manifest recorded it is skipped (`changed-since-manifest`). That includes
a restored object, whose modified time is its restore: moving it again needs a new inventory. When
both the source and the destination exist and the source was created later, something recreated it
under the old name, so it is skipped (`recreated-since-move`) rather than deleted.

`--restore` takes its names from `--only` and their types from `disruptions_obsolete`, not from the
manifest, so it works after a later inventory. It never deletes the obsolete copy while the name
exists in `disruptions`: that copy may be the only one.

Copies within the EU multi-region are free. Runs under your ADC.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime
from pathlib import Path

from bq_obsolete_inventory import DATASET, ENVS, MANIFEST_DIR

OBSOLETE = f"{DATASET}_obsolete"
MOVABLE = ("TABLE", "MODEL")


def load_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def manifest_env(path: Path) -> str | None:
    m = re.fullmatch(r"manifest-(\w+)\.csv", path.name)
    return m.group(1) if m else None


def _get(client, kind: str, ref: str):
    from google.api_core.exceptions import NotFound

    try:
        return client.get_model(ref) if kind == "MODEL" else client.get_table(ref)
    except NotFound:
        return None


def _mismatch(kind: str, a, b) -> str | None:
    """Why `b` is not a faithful copy of `a`, or None."""
    if kind == "MODEL":
        return None if a.model_type == b.model_type else f"model type {a.model_type} -> {b.model_type}"
    if b.table_type != "TABLE":
        return f"copy is a {b.table_type}"
    if (a.num_rows, a.num_bytes) != (b.num_rows, b.num_bytes):
        return f"rows/bytes {a.num_rows}/{a.num_bytes} -> {b.num_rows}/{b.num_bytes}"
    return None


def _delete(client, kind: str, ref: str) -> None:
    if kind == "MODEL":
        client.delete_model(ref)
    else:
        client.delete_table(ref)


def _kind_in(client, ref: str) -> str | None:
    """TABLE or MODEL, whichever `ref` is; None when it is neither."""
    return next((kind for kind in MOVABLE if _get(client, kind, ref) is not None), None)


def transfer(client, project: str, row: dict[str, str], *, restore: bool, apply: bool) -> str:
    """Move one object (or restore it); returns its report line. Raises on an unexpected API error."""
    from google.cloud import bigquery

    name = row["name"]
    kind = row.get("type") or _kind_in(client, f"{project}.{OBSOLETE}.{name}")
    if kind is None:
        if restore and _kind_in(client, f"{project}.{DATASET}.{name}"):
            return "skipped (already-restored)"
        return f"FAILED (not in {OBSOLETE})"
    frm, to = (OBSOLETE, DATASET) if restore else (DATASET, OBSOLETE)
    src_ref, dst_ref = f"{project}.{frm}.{name}", f"{project}.{to}.{name}"
    src, dst = _get(client, kind, src_ref), _get(client, kind, dst_ref)
    if src is None:
        return "skipped (already-moved)" if dst is not None else f"FAILED (not in {frm} or {to})"
    if kind == "TABLE" and src.table_type != "TABLE":
        return f"FAILED (source is a {src.table_type})"
    if kind == "TABLE" and src.streaming_buffer is not None:
        return "FAILED (rows in the streaming buffer; a copy would leave them out)"
    if restore and dst is not None:
        # Something holds the name in disruptions already: a recreated object, or a restore that
        # stopped before its delete. Either way the obsolete copy may be the only one, so keep it.
        return f"skipped (exists in {DATASET}; the {OBSOLETE} copy is kept)"
    if dst is not None and src.created > dst.created:
        return "skipped (recreated-since-move)"
    if not restore and int(src.modified.timestamp()) > int(datetime.fromisoformat(row["modified"]).timestamp()):
        return "skipped (changed-since-manifest)"
    if not apply and dst is not None:
        return "would finish"
    if not apply:
        return "would restore" if restore else "would move"
    if dst is None:
        config = bigquery.CopyJobConfig(write_disposition="WRITE_EMPTY")
        client.copy_table(src_ref, dst_ref, job_config=config).result()
        dst = _get(client, kind, dst_ref)
        if dst is None:
            return "FAILED (copy finished but the destination is missing)"
    if problem := _mismatch(kind, src, dst):
        return f"FAILED ({problem})"
    # Read the source again: a write that landed after the first read is not in the copy.
    now = _get(client, kind, src_ref)
    if now is None or now.modified != src.modified:
        return "FAILED (source changed during the move; it stays, and the copy stays too)"
    _delete(client, kind, src_ref)
    return "restored" if restore else "moved"


def ensure_dataset(client, project: str, manifest: Path, apply: bool) -> str:
    from google.api_core.exceptions import NotFound
    from google.cloud import bigquery

    ref = f"{project}.{OBSOLETE}"
    try:
        client.get_dataset(ref)
        return f"{ref} exists"
    except NotFound:
        if not apply:
            return f"{ref} would be created (EU, no expiry)"
    ds = bigquery.Dataset(ref)
    ds.location = "EU"
    ds.description = (
        f"Objects moved out of {DATASET} by scripts/bq_obsolete_move.py. The record is "
        f"docs/operations/bigquery-obsolete/{manifest.name}. No expiry: deleting them is a separate decision."
    )
    client.create_dataset(ds)
    return f"{ref} created (EU, no expiry)"


def select(rows: list[dict[str, str]], only: list[str] | None, apply: bool) -> list[dict[str, str]]:
    """The manifest rows a move acts on; raises ValueError for anything refused before a call."""
    if apply and any(r["verdict"] == "review" for r in rows):
        raise ValueError("the manifest still has review rows; decide each one before --apply")
    move = {r["name"]: r for r in rows if r["verdict"] == "move"}
    if only:
        unknown = [n for n in only if n not in move]
        if unknown:
            raise ValueError(f"not move rows in the manifest: {', '.join(unknown)}")
        chosen = [move[n] for n in only]
    else:
        chosen = list(move.values())
    refused = [f"{r['name']} ({r['type']})" for r in chosen if r["type"] not in MOVABLE]
    if refused:
        raise ValueError(f"only tables and models move; set these to keep: {', '.join(refused)}")
    return chosen


def rooted(project: str, names: list[str]) -> list[str]:
    """The names a keep root matches today. Code merged after the manifest can name a move row."""
    import bq_obsolete_inventory as inv

    roots = inv.roots_for(ENVS[project], inv.repo_roots(), inv.scheduled_queries(project))
    patterns = [p for tokens in roots.values() for t in tokens if (p := inv.pattern(t))]
    return [n for n in names if any(p.fullmatch(n) for p in patterns)]


def run(client, project: str, manifest: Path | None, rows: list[dict[str, str]], restore: bool, apply: bool) -> int:
    if not restore:
        print(ensure_dataset(client, project, manifest, apply))
    print(f"{'Restoring' if restore else 'Moving'} {len(rows)} objects{'' if apply else ' (dry run)'}")
    failed = 0
    for row in rows:
        try:
            line = transfer(client, project, row, restore=restore, apply=apply)
        except Exception as e:  # noqa: BLE001 - one object's failure must not stop the others
            line = f"FAILED ({type(e).__name__}: {str(e).splitlines()[0][:160]})"
        failed += line.startswith("FAILED")
        print(f"  {line}  {row['name']}")
    if failed:
        print(f"{failed} failed; nothing was deleted without a checked copy. Rerun to retry.")
    return 1 if failed else 0


def main(argv: list[str] | None = None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", required=True, choices=sorted(ENVS))
    ap.add_argument("--manifest", type=Path, help="default: the committed manifest for --project")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="act on these move rows only")
    ap.add_argument("--restore", action="store_true", help="copy back from disruptions_obsolete (needs --only)")
    ap.add_argument("--apply", action="store_true", help="make the changes; without it this is a dry run")
    args = ap.parse_args(argv)
    env = ENVS[args.project]
    manifest = None
    if args.restore:
        # Restore reads the names from --only and their type from disruptions_obsolete, so it still
        # works after a later inventory has rewritten the manifest without the moved rows.
        if not args.only:
            ap.error("--restore needs --only: name the objects to put back")
        rows = [{"name": n} for n in args.only]
    else:
        manifest = args.manifest or MANIFEST_DIR / f"manifest-{env}.csv"
        if manifest_env(manifest) != env:
            ap.error(f"{manifest.name} is not manifest-{env}.csv, the manifest for {args.project}")
        try:
            rows = select(load_manifest(manifest), args.only, args.apply)
        except ValueError as e:
            ap.error(str(e))
        if args.apply and (now_kept := rooted(args.project, [r["name"] for r in rows])):
            ap.error(f"a keep root names these now; re-run the inventory: {', '.join(now_kept)}")
    if client is None:
        from google.cloud import bigquery

        client = bigquery.Client(project=args.project)
    return run(client, args.project, manifest, rows, args.restore, args.apply)


if __name__ == "__main__":
    sys.exit(main())
