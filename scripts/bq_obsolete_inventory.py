#!/usr/bin/env python3
"""Classify every object in one project's `disruptions` dataset as keep, move or review.

Plan: docs/plans/2026-11-07-001-refactor-bigquery-obsolete-dataset-plan.md (U1, U2).

    uv run python scripts/bq_obsolete_inventory.py --project ent-kixavvik-prd --since 2026-11-07T12:54Z
    uv run python scripts/bq_obsolete_inventory.py --project ent-kixavvik-dev --since 2026-11-07T12:38Z
    uv run python scripts/bq_obsolete_inventory.py --dump-roots > scripts/fixtures/bq_obsolete_roots.json

Writes `docs/operations/bigquery-obsolete/manifest-<env>.csv` (or `--out`) and prints counts and GB
per verdict and reason. It reads BigQuery, the audit log and the scheduled-query configs, and
writes nothing but the manifest.

**How an object is classified** (the plan's KTD1):

1. `keep` when a root names it. A root is a file or SQL text whose name tokens keep the objects
   they name. A token is `disruptions.<name>`, or `{VAR}.<name>` / `$VAR.<name>` where `VAR` is
   assigned a literal ending in `disruptions` in the same file. A placeholder in a name (`{x}`,
   `$x`, `${x}`, `%s`) is a wildcard; a name with fewer than two literal letters or digits is
   dropped, because as a wildcard it would keep everything. The roots: deployed code, the project's scheduled queries, the prd
   copy list and the feedback export in both projects; the retrain files and `demo_*` in dev only.
2. `review` when the project's own service account read it after `--since` (`app-read`), or an
   external principal read it in the last 30 days (`external-read`). Logs never decide `keep`:
   retraining reads its inputs about once a quarter.
3. `move`, with reason `eval-only` when only an eval tool names it, else `unreferenced`.

**Readers are buckets, never names.** `app` is the project's service account. `team` is a member of
`sg-dig-team-ki-eksperimentering@entur.no`, asked of `gcloud` once per principal. `handover` is a
job in an `ent-smartavvik-*` project (team-ror's copy) and raises nothing. Everyone else is
`external`. The manifest holds buckets and dates only: no e-mail address, no query text.

Needs ADC for BigQuery, Logging and Data Transfer, and a live `gcloud auth login` for the group
check: a failed check reads as "not a member", so the script checks your own account first and
stops if that fails. Cost: one JOBS query and one `__TABLES__` query, about 10 MB each.
"""

from __future__ import annotations

import argparse
import csv
import fnmatch
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATASET = "disruptions"
ENVS = {"ent-kixavvik-prd": "prd", "ent-kixavvik-dev": "dev"}
TEAM_GROUP = "sg-dig-team-ki-eksperimentering@entur.no"
HANDOVER_PREFIX = "ent-smartavvik-"
EXTERNAL_WINDOW = timedelta(days=30)
MIN_APP_WINDOW = timedelta(hours=24)
MANIFEST_DIR = REPO / "docs" / "operations" / "bigquery-obsolete"
COLUMNS = ("name", "type", "size_gb", "modified", "verdict", "reason", "evidence")
BUCKETS = ("app", "team", "handover", "external")

#: Deployed code: what the image ships from disruptions/api, minus its test suites.
DEPLOYED = ("disruptions/api/*.py", "disruptions/api/*.sql")
OPERATIONAL = ("data/feedback/export_varsel_feedback.py",)
#: The retrain procedure in docs/operations/RETRAIN.md, file by file (KTD3). retrain.sh runs the
#: numbered togvarsel files by glob, so the glob is listed rather than each file.
RETRAIN = (
    "data/queries/togvarsel/retrain.sh",
    "data/queries/togvarsel/[0-9]*.sql",
    "data/queries/create_disruptions.sql",  # retrain.sh runs it before the numbered files
    "data/queries/update_rail_calls.sql",
    "data/queries/create_training_calls.sql",
    "data/queries/train_call_models.sql",
    "data/queries/create_cancel_calibration.sql",
    "data/queries/create_exceedance_calibration.sql",
    "data/queries/leadtime_eval.sql",
    "data/queries/create_leadtime_calibration.sql",
    "data/queries/create_nosx_platt_weights.sql",
    "data/queries/gen_nosx_platt_weights.py",
    "data/queries/create_delay_p90_calib.sql",
    "data/queries/create_demo_predictions.sql",
    "data/queries/create_training_calls_pooled_all.sql",
    "data/queries/create_bus_calls.sql",
    "data/queries/create_goa_calls.sql",
    "data/queries/train_call_models_pooled_all.sql",
    "data/queries/create_pooled_all_calibration.sql",
    "data/queries/leadtime_eval_pooled_all.sql",
    "data/queries/create_leadtime_calibration_pooled_all.sql",
    "data/queries/create_pooled_all_platt_weights.sql",
    "data/queries/create_delay_p90_calib_pooled_all.sql",
    "data/queries/eval_pooled_all_models.sql",
    # The multi-leg miss model (trips._MISS_*): a prd feature once TRIP_PLANS_ENABLED is on there.
    "data/queries/transfer_miss_backtest.py",
    "data/queries/transfer_miss_backtest.sql",
)
#: Files RETRAIN.md names that are not retrain inputs, and why. U1's test holds RETRAIN.md to
#: RETRAIN + this set, so a new step in the runbook fails the suite until it is classified.
NOT_RETRAIN_INPUTS = {
    "copy_runtime_to_prd.sh": "its own root, prd-copy",
    "predict_calls.sql": "deployed code",
    "trips.py": "deployed code",
    "test_trips.py": "a test suite",
    "impact_eval.py": "an eval tool: its reads are eval-only by design",
    "bq_obsolete_inventory.py": "this cleanup's own tooling",
    "test_bq_obsolete_inventory.py": "this cleanup's own tooling",
    "bq_obsolete_move.py": "this cleanup's own tooling",
}
DEMO_BUILDERS = ("data/queries/create_demo_predictions.sql", "data/queries/extend_demo_recent_daily.sql")
EVAL = ("eval/*.py", "data/queries/et_expected_eval.py")

#: A placeholder in a name: Python `{x}`, shell `$x` / `${x}`, or printf `%s`.
_PLACEHOLDER = r"\{[^{}\s]*\}|\$\{?\w+\}?|%s"
_NAME = r"((?:[A-Za-z0-9_]|" + _PLACEHOLDER + r")+)"
_PLAIN = re.compile(r"(?<![\w-])" + DATASET + r"\." + _NAME)
_ASSIGN = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_]\w*)\s*(?::[^=\n]*)?=\s*["']?([\w.:-]+)["']?""", re.MULTILINE)
_DATASET_LITERAL = re.compile(r"(?:^|[.:])" + DATASET + r"$")


@dataclass(frozen=True)
class Obj:
    name: str
    type: str
    size_gb: float
    modified: datetime


@dataclass(frozen=True)
class Read:
    name: str
    principal: str
    job_project: str
    at: datetime


# ---- Roots ----------------------------------------------------------------------------------------


def name_tokens(text: str) -> set[str]:
    """Every object name `text` writes as `disruptions.<name>` or through a dataset variable."""
    tokens = set(_PLAIN.findall(text))
    for var, value in _ASSIGN.findall(text):
        if _DATASET_LITERAL.search(value):
            ref = r"(?:\{" + re.escape(var) + r"\}|\$" + re.escape(var) + r"\b)\."
            tokens.update(re.findall(ref + _NAME, text))
    return tokens


def pattern(token: str) -> re.Pattern[str] | None:
    """The token as a full-match pattern, `{...}` as a wildcard; None when it would match anything."""
    parts = re.split(_PLACEHOLDER, token)
    if len(re.sub(r"[\W_]", "", "".join(parts))) < 2:
        return None
    return re.compile(".*".join(map(re.escape, parts)))


def tracked_files(globs: Iterable[str]) -> list[Path]:
    """Tracked files matching any glob (`*` crosses directories), test suites excluded."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.split()
    globs = tuple(globs)
    return [
        REPO / f
        for f in tracked
        if any(fnmatch.fnmatchcase(f, g) for g in globs) and not Path(f).name.startswith("test_")
    ]


def tokens_of(globs: Iterable[str]) -> set[str]:
    return set().union(*(name_tokens(p.read_text()) for p in tracked_files(globs)))


def copy_list() -> list[str]:
    out = subprocess.run(
        ["bash", str(REPO / "data" / "queries" / "copy_runtime_to_prd.sh"), "--list"],
        capture_output=True, text=True, check=True,
    ).stdout
    return out.split()


def repo_roots() -> dict[str, list[str]]:
    """Every repo-file root's tokens, for both environments; `--dump-roots` writes this."""
    roots = {
        "deployed": tokens_of(DEPLOYED),
        "prd-copy": set(copy_list()),
        "operational": tokens_of(OPERATIONAL),
        "retrain": tokens_of(RETRAIN),
        "demo": {"demo_{name}"} | tokens_of(DEMO_BUILDERS),
        "eval-only": tokens_of(EVAL),
    }
    return {k: sorted(v) for k, v in roots.items()}


#: Keep reasons in the order they are reported; the first that names an object is its reason.
KEEP_ORDER = ("deployed", "scheduled-query", "prd-copy", "operational", "retrain", "demo")
DEV_ONLY = {"retrain", "demo"}


def roots_for(env: str, repo: dict[str, list[str]], scheduled_sql: Iterable[str]) -> dict[str, set[str]]:
    roots = {k: set(v) for k, v in repo.items() if k in KEEP_ORDER and (env == "dev" or k not in DEV_ONLY)}
    roots["scheduled-query"] = set().union(*(name_tokens(q) for q in scheduled_sql))
    return roots


# ---- Classification -------------------------------------------------------------------------------


def _patterns(tokens: Iterable[str]) -> list[re.Pattern[str]]:
    return [p for t in tokens if (p := pattern(t))]


def bucket(read: Read, project: str, is_team: Callable[[str], bool]) -> str:
    if read.job_project.startswith(HANDOVER_PREFIX):
        return "handover"
    if read.principal == app_account(project):
        return "app"
    return "team" if is_team(read.principal) else "external"


def classify(
    objects: Iterable[Obj],
    roots: dict[str, set[str]],
    eval_tokens: Iterable[str],
    app_reads: dict[str, datetime],
    reads: Iterable[Read],
    project: str,
    is_team: Callable[[str], bool],
) -> list[dict[str, str]]:
    keep = [(r, _patterns(roots.get(r, ()))) for r in KEEP_ORDER]
    evals = _patterns(eval_tokens)
    seen: dict[str, dict[str, datetime]] = defaultdict(dict)
    for name, at in app_reads.items():
        seen[name]["app"] = at
    for r in reads:
        b = bucket(r, project, is_team)
        if r.at > seen[r.name].get(b, datetime.min.replace(tzinfo=UTC)):
            seen[r.name][b] = r.at
    rows = []
    for o in objects:
        ev = seen.get(o.name, {})
        reason = next((r for r, pats in keep if any(p.fullmatch(o.name) for p in pats)), None)
        if reason:
            verdict = "keep"
        elif o.name in app_reads:
            verdict, reason = "review", "app-read"
        elif "external" in ev:
            verdict, reason = "review", "external-read"
        else:
            verdict = "move"
            reason = "eval-only" if any(p.fullmatch(o.name) for p in evals) else "unreferenced"
        rows.append({
            "name": o.name,
            "type": o.type,
            "size_gb": f"{o.size_gb:.3f}",
            "modified": stamp(o.modified),
            "verdict": verdict,
            "reason": reason,
            "evidence": " ".join(f"{b}:{ev[b]:%Y-%m-%d}" for b in BUCKETS if b in ev),
        })
    return sorted(rows, key=lambda r: (r["verdict"], r["name"]))


def stamp(t: datetime) -> str:
    """UTC ISO 8601 to the second, the precision the mover compares at."""
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_manifest(rows: list[dict[str, str]], out) -> None:
    w = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)


def summary(rows: list[dict[str, str]]) -> str:
    n: Counter[tuple[str, str]] = Counter()
    gb: Counter[tuple[str, str]] = Counter()
    for r in rows:
        n[r["verdict"], r["reason"]] += 1
        gb[r["verdict"], r["reason"]] += float(r["size_gb"])
    lines = [f"{v:<7} {reason:<16} {n[v, reason]:>4} objects {gb[v, reason]:>9.2f} GB" for v, reason in sorted(n)]
    return "\n".join(lines)


# ---- BigQuery, Logging, Data Transfer, gcloud -----------------------------------------------------


def app_account(project: str) -> str:
    return f"application@{project}.iam.gserviceaccount.com"


def list_objects(client, project: str) -> list[Obj]:
    """Tables and views from `__TABLES__` (for size), models and routines from their own lists.

    `__TABLES__` lists models too, as type 4 (measured 2026-11-07: 310 in dev), so a name the model
    list returns is dropped from it. Any other type keeps its number, which the mover refuses.
    """
    types = {1: "TABLE", 2: "VIEW", 3: "EXTERNAL"}
    models = [Obj(m.model_id, "MODEL", 0.0, m.modified) for m in client.list_models(f"{project}.{DATASET}")]
    model_names = {m.name for m in models}
    rows = client.query(
        f"SELECT table_id, type, size_bytes, last_modified_time FROM `{project}.{DATASET}.__TABLES__`"
    ).result()
    tables = [
        Obj(r["table_id"], types.get(r["type"], str(r["type"])), (r["size_bytes"] or 0) / 1e9,
            datetime.fromtimestamp(r["last_modified_time"] / 1000, UTC))
        for r in rows
        if r["table_id"] not in model_names
    ]
    routines = [Obj(r.routine_id, "ROUTINE", 0.0, r.modified) for r in client.list_routines(f"{project}.{DATASET}")]
    return tables + models + routines


def app_reads_since(client, project: str, since: datetime) -> dict[str, datetime]:
    from google.cloud import bigquery

    rows = client.query(
        "SELECT t.table_id AS name, MAX(j.creation_time) AS last"
        " FROM `region-eu`.INFORMATION_SCHEMA.JOBS_BY_PROJECT AS j, UNNEST(j.referenced_tables) AS t"
        " WHERE j.creation_time >= @since AND j.user_email = @sa"
        f" AND t.project_id = @project AND t.dataset_id = '{DATASET}'"
        " GROUP BY name",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("since", "TIMESTAMP", since),
            bigquery.ScalarQueryParameter("sa", "STRING", app_account(project)),
            bigquery.ScalarQueryParameter("project", "STRING", project),
        ]),
    ).result()
    return {r["name"]: r["last"] for r in rows}


def audit_filter(project: str, start: datetime) -> str:
    return (
        f'logName="projects/{project}/logs/cloudaudit.googleapis.com%2Fdata_access"'
        f' AND timestamp>="{stamp(start)}"'
        f' AND protoPayload.resourceName:"/datasets/{DATASET}/"'
        f' AND protoPayload.authenticationInfo.principalEmail!="{app_account(project)}"'
        " AND (protoPayload.metadata.tableDataRead:* OR protoPayload.metadata.modelDataRead:*)"
    )


def parse_read(entry: dict) -> Read | None:
    """One audit entry (its API dict) as a Read; None when it is not a read of this dataset."""
    pp = entry.get("protoPayload", {})
    md = pp.get("metadata", {})
    op = md.get("tableDataRead") or md.get("modelDataRead")
    m = re.search(rf"/datasets/{DATASET}/(?:tables|models)/([^/]+)$", pp.get("resourceName", ""))
    if op is None or not m:
        return None
    auth = pp.get("authenticationInfo", {})
    job = re.match(r"projects/([^/]+)/", op.get("jobName", ""))
    return Read(
        name=m.group(1),
        principal=auth.get("principalEmail") or auth.get("principalSubject") or "unknown",
        job_project=job.group(1) if job else "",
        at=datetime.fromisoformat(entry["timestamp"]),
    )


def audit_reads(project: str, start: datetime) -> list[Read]:
    from google.cloud import logging

    entries = logging.Client(project=project).list_entries(filter_=audit_filter(project, start), page_size=1000)
    return [r for e in entries if (r := parse_read(e.to_api_repr()))]


def scheduled_queries(project: str) -> list[str]:
    from google.cloud import bigquery_datatransfer

    client = bigquery_datatransfer.DataTransferServiceClient()
    return [
        cfg.params["query"]
        for cfg in client.list_transfer_configs(parent=f"projects/{project}/locations/eu")
        if cfg.data_source_id == "scheduled_query"
    ]


def gcloud_is_member(email: str, group: str) -> bool:
    """A denial means "not a member": we can only read groups we are in."""
    r = subprocess.run(
        ["gcloud", "identity", "groups", "memberships", "check-transitive-membership",
         f"--group-email={group}", f"--member-email={email}", "--format=value(hasMembership)"],
        capture_output=True, text=True, check=False,
    )
    return r.returncode == 0 and r.stdout.strip().lower() == "true"


def gcloud_account() -> str:
    return subprocess.run(
        ["gcloud", "config", "get-value", "account"], capture_output=True, text=True, check=False
    ).stdout.strip()


def team_check(operator: str, is_member: Callable[[str], bool], group: str = TEAM_GROUP) -> Callable[[str], bool]:
    """A cached membership check, refused up front when the operator's own account fails it."""
    if not operator or not is_member(operator):
        raise SystemExit(
            f"Your own account ({operator or 'none'}) failed the membership check for {group}: either "
            "the gcloud login has expired (run `gcloud auth login`) or you are not a member (pass "
            "--team-group with your team's group). A failed check would read every reader as 'external'."
        )
    cache = {operator: True}

    def check(principal: str) -> bool:
        if principal not in cache:
            cache[principal] = is_member(principal)
        return cache[principal]

    return check


# ---- Main -----------------------------------------------------------------------------------------


def parse_since(s: str) -> datetime:
    return datetime.fromisoformat(s).astimezone(UTC)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", choices=sorted(ENVS))
    ap.add_argument("--since", type=parse_since, help="start of the app-read window, e.g. 2026-11-07T12:54Z")
    ap.add_argument("--out", type=Path, help="manifest path (default: the committed manifest)")
    ap.add_argument("--dump-roots", action="store_true", help="print the repo-file root tokens as JSON and exit")
    ap.add_argument("--team-group", default=TEAM_GROUP, help=f"the group whose reads raise nothing (default {TEAM_GROUP})")
    args = ap.parse_args(argv)
    if args.dump_roots:
        json.dump(repo_roots(), sys.stdout, indent=1)
        print()
        return
    if not args.project or not args.since:
        ap.error("--project and --since are required")
    now = datetime.now(UTC)
    if now - args.since < MIN_APP_WINDOW:
        ap.error("--since must be at least 24 hours ago, so every daily scheduled query ran inside the window")

    from google.cloud import bigquery

    env = ENVS[args.project]
    is_team = team_check(gcloud_account(), lambda p: gcloud_is_member(p, args.team_group), args.team_group)
    sql = scheduled_queries(args.project)
    if not sql:
        # Both projects have had two since 2026-09; none means a wrong location or a missing
        # permission, and the objects only a scheduled query names would turn into move rows.
        raise SystemExit(f"No scheduled queries found in {args.project}; refusing to classify without them.")
    client = bigquery.Client(project=args.project)
    repo = repo_roots()
    rows = classify(
        list_objects(client, args.project),
        roots_for(env, repo, sql),
        repo["eval-only"],
        app_reads_since(client, args.project, args.since),
        audit_reads(args.project, now - EXTERNAL_WINDOW),
        args.project,
        is_team,
    )
    out = args.out or MANIFEST_DIR / f"manifest-{env}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        write_manifest(rows, f)
    print(f"{len(rows)} objects -> {out.relative_to(REPO) if out.is_relative_to(REPO) else out}")
    print(summary(rows))


if __name__ == "__main__":
    main()
