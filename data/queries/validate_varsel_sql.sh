#!/usr/bin/env bash
#
# Dry-run every SQL file behind the decision log and /historikk, to catch what the test suite
# structurally cannot.
#
# No SQL in this repo is executed by any test: the suites are hermetic (the BigQuery client is
# stubbed, CI has no GCP credentials), so a syntax or type error in a .sql file ships green and
# only surfaces when the query first runs for real -- at 04:00 UTC, or on a user's page load.
# That is not hypothetical: review of this feature found `APPROX_QUANTILES(expr IGNORE NULLS, n)`
# with the clause order reversed, which is a hard syntax error and would have made
# /historikk-api/summary a permanent 503. A dry run catches that in a second.
#
# --dry_run validates syntax, types, table and column references against the real schemas without
# executing or scanning, so it is free. It does need the tables to exist: run
# disruptions/api/create_varsel_decisions.sql first (or let the app create them at startup).
#
# SINCE 2026-09-12 that prerequisite includes a ROUTINE, not only tables: the grouping body moved
# into the table function `disruptions.varsel_decision_groups_between(d_start, d_end)` so the three
# /historikk queries can pass their own partition cover (R12). Each statement of the DDL is dry-run
# on its own, so the view over that function -- and all three historikk files -- resolve it by name
# and report FAILED in a project where the app has never started. Same shape as the two togvarsel
# tables noted further down: start the app once against the project, then validate.
#
# Run this after editing any of these files, and before re-registering the scheduled query.
#
# Usage: data/queries/validate_varsel_sql.sh [project]      (default ent-kixavvik-dev)
set -euo pipefail

PROJECT="${1:-ent-kixavvik-dev}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"          # data/
API="$(cd "$ROOT/../disruptions/api" && pwd)"
fail=0

# The two /historikk queries are parameterised; the values are irrelevant to a dry run, only the
# types are, but they have to be supplied or the query will not parse.
#
# Keep these in step with the parameter lists in disruptions/api/historikk.py. A parameter the file
# uses and this list omits is not a soft warning -- bq refuses to parse the query at all
# ("Query parameter 'line' not found"), so the check reports FAILED and the file goes UNVALIDATED
# until somebody notices. That is how the /historikk filter parameters added on 2026-08-21 left
# both of these files unchecked; found on 2026-08-21 while validating the view change beside them.
DECISION_PARAMS=(
  --parameter=from_date:DATE:2026-07-01 --parameter=to_date:DATE:2026-07-31
  --parameter=operator:STRING:VYG --parameter=verdict:STRING:hit
  --parameter=line:STRING:R11
  '--parameter=from_place_refs:ARRAY<STRING>:[]' '--parameter=to_place_refs:ARRAY<STRING>:[]'
  --parameter=alerted_only:BOOL:false --parameter=resolved_only:BOOL:false
  --parameter=sx_only:BOOL:false --parameter=limit:INT64:200
)
SUMMARY_PARAMS=(
  --parameter=from_date:DATE:2026-07-01 --parameter=to_date:DATE:2026-07-31
  --parameter=operator:STRING:VYG --parameter=line:STRING:R11
  '--parameter=from_place_refs:ARRAY<STRING>:[]' '--parameter=to_place_refs:ARRAY<STRING>:[]'
)

check() {
  local label="$1" file="$2"; shift 2
  printf '%-34s ' "$label"
  if out=$(bq query --use_legacy_sql=false --dry_run --project_id="$PROJECT" "$@" < "$file" 2>&1); then
    # The byte count is worth seeing: it is the cost signal, and a sudden jump means a predicate
    # stopped pruning.
    echo "ok  ${out##*will process }"
  else
    echo "FAILED"; echo "$out" | sed 's/^/    /'; fail=1
  fi
}

# A multi-statement file needs every statement checked, one at a time.
#
# BigQuery dry-runs a SCRIPT lazily: it reports `numChildJobs: 1` and validates only the FIRST
# statement, because a later one may depend on what an earlier one creates. So the whole-file check
# below passes on the CREATE TABLE alone, and a broken ALTER or view definition after it dry-runs
# perfectly clean -- then fails at app startup, which is the one place this script exists to
# prevent. Measured 2026-08-05, while adding the llm_payload columns.
check_each_statement() {
  local label="$1" file="$2"
  local dir; dir="$(mktemp -d)"
  # Splitting on ";" needs a real tokenizer, not a sed: these files carry semicolons inside column
  # descriptions ("Minutes late at that stop; null if not recorded.") and apostrophes inside SQL
  # comments ("the traveller's time window"), each of which breaks the naive version differently.
  python3 - "$file" "$dir" <<'PY'
import pathlib, sys

sql = pathlib.Path(sys.argv[1]).read_text()
out = pathlib.Path(sys.argv[2])
statements, buf, quote, comment = [], [], None, False
i = 0
while i < len(sql):
    ch = sql[i]
    if comment:
        if ch == "\n":
            comment = False
    elif quote:
        if ch == quote:
            quote = None
    elif ch in "\"'`":
        quote = ch
    elif sql.startswith("--", i):
        comment = True
    elif ch == ";":
        statements.append("".join(buf) + ";")
        buf = []
        i += 1
        continue
    buf.append(ch)
    i += 1
if "".join(buf).strip():
    statements.append("".join(buf))
for n, stmt in enumerate(s for s in statements if s.strip().strip(";")):
    (out / f"{n:02d}.sql").write_text(stmt)
PY
  local n=0
  for stmt in "$dir"/*.sql; do
    n=$((n + 1))
    # Name the statement by its first non-comment line, so a failure says which one.
    local first; first=$(grep -m1 -vE '^\s*(--|$)' "$stmt" | cut -c1-46)
    check "$label [$n] ${first}" "$stmt"
  done
  rm -rf "$dir"
}

check_each_statement "create_varsel_decisions.sql" "$API/create_varsel_decisions.sql"
check "label_varsel_decisions.sql"  "$ROOT/queries/label_varsel_decisions.sql"
check "historikk_decisions.sql"     "$API/historikk_decisions.sql" "${DECISION_PARAMS[@]}"
check "historikk_summary.sql"       "$API/historikk_summary.sql"   "${SUMMARY_PARAMS[@]}"
# The filter vocabulary and its stop-place names: both feed /historikk-api and neither was checked
# here until 2026-08-21. historikk_filters.sql is also the API's allow-list, so a fault in it 400s
# every filtered request rather than merely emptying a dropdown.
check "historikk_filters.sql"       "$API/historikk_filters.sql" \
  --parameter=from_date:DATE:2026-07-01 --parameter=to_date:DATE:2026-07-31 \
  --parameter=limit:INT64:200
check "historikk_stop_places.sql"   "$API/historikk_stop_places.sql" \
  --parameter='refs:ARRAY<STRING>:["NSR:StopPlace:337"]'

# /meldinger's four, on the DEPARTURE-TIME axis since 2026-08-22: a day (which bounds the DERIVED
# DATE(decided_at) partition scan and the call_date bound on the outcomes side) plus the two absolute
# instants the API resolves the Oslo day and the quantized times into -- those two now compare
# against `departure_time`, not `decided_at`, so @day and @from_ts/@to_ts are predicates on two
# DIFFERENT columns and neither can be derived from the other.
#
# The same "keep these in step" warning applies, and harder: these files carry FOURTEEN parameters
# against /historikk's eleven, and one omitted here makes bq refuse to parse the query at all, so the
# check reports FAILED and the file goes UNVALIDATED. test_meldinger.py greps this script for
# every @param each file uses, which is the cheap half of the guard; the dry run below is the half
# that actually type-checks them.
#
# WHAT A CLEAN DRY RUN HERE DOES NOT PROVE, and the second item got WORSE on this axis. An empty
# ARRAY parameter from the CLI is encoded differently from the Python client (so the
# IFNULL(ARRAY_LENGTH(..)) rule dry-runs identically either way). And a too-narrow date bound drops
# rows SILENTLY -- which shows up here as a SMALLER byte count, i.e. as success. That now applies to
# the DATE(decided_at) bound as well as the call_date one: it is no longer the window predicate but
# a derived cover for it, so narrowing it drops cycles out of the per-subject aggregates rather than
# merely mislabelling a flag. Cost is not a correctness check.
MELDINGER_PARAMS=(
  --parameter=day:DATE:2026-07-15
  --parameter=from_ts:TIMESTAMP:'2026-07-15 06:00:00'
  --parameter=to_ts:TIMESTAMP:'2026-07-15 09:00:00'
)
MELDINGER_DECISION_PARAMS=(
  "${MELDINGER_PARAMS[@]}"
  --parameter=operator:STRING:VYG --parameter=verdict:STRING:hit
  --parameter=line:STRING:R11
  # The SIRI-SX filter. An ARRAY, not a scalar: the dropdown offers one option per ANSWER and the
  # API expands it to every stored summary it stands for -- the no-SX placeholder alone has two
  # wordings. Allow-listed against meldinger_filters.sql's top-30 array before it reaches BigQuery.
  '--parameter=sx_summaries:ARRAY<STRING>:[]'
  '--parameter=from_place_refs:ARRAY<STRING>:[]' '--parameter=to_place_refs:ARRAY<STRING>:[]'
  --parameter=alerted_only:BOOL:false --parameter=pushed_only:BOOL:false
  --parameter=resolved_only:BOOL:false
  --parameter=sx_only:BOOL:false --parameter=limit:INT64:200
)
check "meldinger_decisions.sql"     "$API/meldinger_decisions.sql" \
  "${MELDINGER_DECISION_PARAMS[@]}"
# Byte-identical filters, ordering and limit to the file above -- that is what aligns the two
# responses position by position -- so it takes the same parameter list.
check "meldinger_messages.sql"      "$API/meldinger_messages.sql" \
  "${MELDINGER_DECISION_PARAMS[@]}"
# Population filters only: verdict/alerted_only/pushed_only/resolved_only/sx_only narrow by OUTCOME
# or by what we chose to do, and never reach /summary -- filtering to hits would make precision read
# 100 %, and filtering to the subjects a push went out for would do the same from the other side.
check "meldinger_summary.sql"       "$API/meldinger_summary.sql" \
  "${MELDINGER_PARAMS[@]}" \
  --parameter=operator:STRING:VYG --parameter=line:STRING:R11 \
  '--parameter=sx_summaries:ARRAY<STRING>:[]' \
  '--parameter=from_place_refs:ARRAY<STRING>:[]' '--parameter=to_place_refs:ARRAY<STRING>:[]'
# The vocabulary AND the API's allow-list, so a fault here 400s every filtered request rather than
# merely emptying a dropdown. Deliberately not narrowed by the other filters, hence three parameters.
check "meldinger_filters.sql"       "$API/meldinger_filters.sql" "${MELDINGER_PARAMS[@]}"

# ---- /rushhistorikk: the Rushvarsel feedback loop -------------------------------------------
#
# The first togvarsel SQL this script covers. The twelve files under data/queries/togvarsel/ and
# disruptions/api/togvarsel_features_today.sql are still unvalidated -- a separate, overdue pass.
#
# PREREQUISITE, and it is not optional: disruptions.togvarsel_rounds and
# disruptions.togvarsel_outcomes must EXIST in the project. Both are created from Python at startup
# (togvarsel_notify.ensure_table / togvarsel_outcomes.ensure_table), and a dry run resolves table
# references -- so run this against an environment the app has started in, or the two rushhistorikk
# checks report FAILED and the files go UNVALIDATED, which is the exact failure this script exists
# to prevent.
#
# Measured 2026-09-01 against ent-kixavvik-dev: settled 32.5 MB for one day, provisional 3.4 MB,
# the day query 305 bytes. The usual caveat applies harder than the byte counts suggest -- a too-narrow
# date bound drops rows SILENTLY and shows up here as a CHEAPER query, i.e. as success.
check "togvarsel_outcomes_settled.sql" "$API/togvarsel_outcomes_settled.sql" \
  --parameter=from_date:DATE:2026-08-28 --parameter=to_date:DATE:2026-08-28
# @scopes is never empty -- the caller passes ("am","amrest") or ("pm") -- so this is a plain
# IN UNNEST and NOT the IFNULL(ARRAY_LENGTH(...), 0) = 0 idiom the /historikk filters need. That
# idiom exists for "an empty array means no filter"; there is no no-filter case here, and adding it
# would silently widen the pass to scopes whose rush has not ended.
check "togvarsel_outcomes_provisional.sql" "$API/togvarsel_outcomes_provisional.sql" \
  --parameter=day:DATE:2026-09-01 '--parameter=scopes:ARRAY<STRING>:["am","amrest"]'
check "rushhistorikk_days.sql"      "$API/rushhistorikk_days.sql" \
  --parameter=from_date:DATE:2026-08-01

exit "$fail"
