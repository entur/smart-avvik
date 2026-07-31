#!/usr/bin/env bash
#
# Label Avviksvarsel's alert decisions with realized outcomes, by hand.
#
# Normally unattended: the same SQL runs as the "varsel-label-daily" BigQuery scheduled query at
# 04:00 UTC. Run this to backfill after the schedule has been down, to label a fresh environment
# for the first time, or to see the verdicts immediately after a day of testing rather than
# waiting for the night.
#
# Safe to re-run and safe to run twice in a row: the INSERT skips decision subjects that already
# have a row, so nothing is duplicated. Idempotence is the reason the window is 10 days rather
# than 1 — a week of missed runs catches up on its own.
#
# Cost ~$0.08/run (an 11-day partition-pruned slice of last_recorded).
#
# PROJECT matters: dev and prd each hold their own decisions and label them separately. This table
# is deliberately absent from copy_runtime_to_prd.sh — copying it would overwrite one
# environment's real history with the other's.
set -euo pipefail

PROJECT="${1:-ent-kixavvik-dev}"

job=$(bq query --nouse_legacy_sql --nosynchronous_mode --project_id="$PROJECT" \
  < "$(dirname "$0")/label_varsel_decisions.sql" | sed -n 's/^Successfully started query //p')
if [ -z "$job" ]; then
  echo "Failed to submit the labelling job" >&2
  exit 1
fi
echo "Submitted label_varsel_decisions.sql as $job — waiting …"
bq wait --project_id="$PROJECT" "${job#*:}"

# Verify: the verdict mix, and how much of it is still unknown. A large unknown share is the
# signal that the ET join is missing — an operator naming mismatch or a stop_order convention
# drift shows up here as "everything is NULL", not as an error.
bq query --nouse_legacy_sql --project_id="$PROJECT" --format=pretty '
SELECT
  COALESCE(verdict, "ukjent utfall") AS verdict,
  COUNT(*) AS n,
  ROUND(100 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
FROM `disruptions.varsel_decision_outcomes`
WHERE call_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
GROUP BY verdict
ORDER BY n DESC'
