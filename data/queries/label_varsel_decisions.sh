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
# Cost ~$0.01/run (measured: 1.53 GB scanned, an 11-day partition- and cluster-pruned slice of
# last_recorded reading only the columns the join needs).
#
# PROJECT matters: dev and prd each hold their own decisions and label them separately. This table
# is deliberately absent from copy_runtime_to_prd.sh — copying it would overwrite one
# environment's real history with the other's.
#
# ---------------------------------------------------------------------------------------------
# REGISTERING THE SCHEDULE (one-off, per project — nothing in CI or Helm does this)
#
# ALREADY REGISTERED IN DEV as "varsel-label-daily" (2026-08-01, first run SUCCEEDED). prd is not,
# and should not be until its IAM is confirmed — see below.
#
# --service_account_name is REQUIRED, not optional polish. Without it `bq mk` tries to register the
# job against your personal credentials and blocks on an interactive OAuth consent prompt, which
# fails outright in any non-interactive shell. Running as the application SA also means the
# schedule does not break when the person who created it changes teams. demo-recent-daily is owned
# by the same SA.
#
#   bq mk --transfer_config \
#     --project_id=ent-kixavvik-dev \
#     --location=EU \
#     --data_source=scheduled_query \
#     --display_name="varsel-label-daily" \
#     --schedule="every day 04:00" \
#     --service_account_name="application@ent-kixavvik-dev.iam.gserviceaccount.com" \
#     --params="$(python3 -c 'import json;print(json.dumps({"query":open("data/queries/label_varsel_decisions.sql").read()}))')"
#
# Creating the config fires one run immediately, so a mistake surfaces at once rather than at 04:00.
#
# The config embeds a COPY of the SQL text, so editing the .sql in git changes nothing until the
# config is updated. That is the trap worth knowing about: the file here and the job actually
# running can silently diverge. Re-register after any edit
# (`bq ls --transfer_config --transfer_location=EU` gives the resource name):
#
#   bq update --transfer_config --params="$(python3 -c 'import json;print(json.dumps({"query":open("data/queries/label_varsel_decisions.sql").read()}))')" <config resource name>
#
# Validate the SQL BEFORE re-registering: data/queries/validate_varsel_sql.sh
#
# No --target_dataset / destination table: this is a multi-statement script that does its own
# DELETE + INSERT, like copy_demo_to_prd_daily.sql.
#
# Two ordering constraints, both of which bite prd rather than dev:
#   1. The app must have started at least once in that project first — the outcomes table is
#      created at startup (disruptions/api/create_varsel_decisions.sql), not by this job.
#   2. prd's SA reading ent-data-sharing-ext-prd is UNVERIFIED — prd is fed by free copies and has
#      never scanned SIRI history. Confirm it before registering there, or the job fails silently
#      every night. Same class as the 2026-07-09 AI.CLASSIFY gap: API enablement != IAM role.
#      (Dev is proven: its application SA already reads last_recorded for demo-recent-daily.)
#
# Force a run without waiting for 04:00, and check it:
#   bq mk --transfer_run --run_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)" <config resource name>
#   bq ls --transfer_run <config resource name>
# ---------------------------------------------------------------------------------------------
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
