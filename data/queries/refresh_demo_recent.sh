#!/usr/bin/env bash
#
# Refresh the /demo dataset with recent journeys (yesterday, and today's completed ones).
# Run before a partner demo. Safe to re-run; each run rebuilds everything with onset in
# the last ~25 days, so runs up to ~3 weeks apart keep the window gap-free.
#
# Runs both recent-layer scripts, in this order:
#   extend_demo_recent_daily.sql  ALL operators from last_recorded (~$0.2; through yesterday).
#                                 Also runs unattended on the daily "demo-recent-daily"
#                                 BigQuery scheduled query, so this is usually a no-op top-up.
#   extend_demo_recent.sql        NSB from anshar ET/SX (~$2.5): full-fidelity rail extraction
#                                 INCLUDING today's completed journeys. Runs LAST so its
#                                 richer NSB rows win for the demo (the nightly schedule will
#                                 replace them with the thinner last_recorded version later —
#                                 fine once the demo is over).
#
# The API picks up the new window within ~30 min (meta cache TTL) — no redeploy needed.
set -euo pipefail

PROJECT="ent-kixavvik-dev"

for sql in extend_demo_recent_daily.sql extend_demo_recent.sql; do
  job=$(bq query --nouse_legacy_sql --nosynchronous_mode --project_id="$PROJECT" \
    < "$(dirname "$0")/$sql" | sed -n 's/^Successfully started query //p')
  if [ -z "$job" ]; then
    echo "Failed to submit BigQuery job for $sql" >&2
    exit 1
  fi
  echo "Submitted $sql as $job — waiting …"
  bq wait --project_id="$PROJECT" "${job#*:}"
done

bq query --nouse_legacy_sql --project_id="$PROJECT" --format=pretty '
SELECT operator, MAX(call_date) AS newest_call, COUNT(DISTINCT situationNumber) AS situations,
       COUNT(*) AS prediction_rows
FROM `ent-kixavvik-dev.disruptions.demo_predictions`
WHERE call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
GROUP BY operator ORDER BY prediction_rows DESC'
