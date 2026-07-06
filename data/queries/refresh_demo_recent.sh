#!/usr/bin/env bash
#
# Refresh the /demo dataset with recent journeys (yesterday, and today's completed ones).
# Run before a partner demo. Safe to re-run; each run rebuilds everything with onset in
# the last ~25 days, so runs up to ~3 weeks apart keep the window gap-free.
#
# Runs data/queries/extend_demo_recent.sql (~$2 per run: ET/SX partition scans + scoring
# the nine heads). The API picks up the new window within ~30 min (meta cache TTL) — no
# redeploy needed.
set -euo pipefail

PROJECT="ent-kixavvik-dev"
SQL="$(dirname "$0")/extend_demo_recent.sql"

job=$(bq query --nouse_legacy_sql --nosynchronous_mode --project_id="$PROJECT" < "$SQL" \
  | sed -n 's/^Successfully started query //p')
if [ -z "$job" ]; then
  echo "Failed to submit BigQuery job" >&2
  exit 1
fi
echo "Submitted $job — waiting …"
bq wait --project_id="$PROJECT" "${job#*:}"

bq query --nouse_legacy_sql --project_id="$PROJECT" --format=pretty '
SELECT MAX(call_date) AS newest_call, COUNT(DISTINCT situationNumber) AS situations,
       COUNT(*) AS prediction_rows
FROM `ent-kixavvik-dev.disruptions.demo_predictions`
WHERE call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)'
