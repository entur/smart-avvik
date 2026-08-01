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
DECISION_PARAMS=(
  --parameter=from_date:DATE:2026-07-01 --parameter=to_date:DATE:2026-07-31
  --parameter=operator:STRING:VYG --parameter=verdict:STRING:hit
  --parameter=alerted_only:BOOL:false --parameter=limit:INT64:200
)
SUMMARY_PARAMS=(
  --parameter=from_date:DATE:2026-07-01 --parameter=to_date:DATE:2026-07-31
  --parameter=operator:STRING:VYG
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

check "create_varsel_decisions.sql" "$API/create_varsel_decisions.sql"
check "label_varsel_decisions.sql"  "$ROOT/queries/label_varsel_decisions.sql"
check "historikk_decisions.sql"     "$API/historikk_decisions.sql" "${DECISION_PARAMS[@]}"
check "historikk_summary.sql"       "$API/historikk_summary.sql"   "${SUMMARY_PARAMS[@]}"

exit "$fail"
