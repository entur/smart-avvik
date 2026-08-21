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

exit "$fail"
