#!/usr/bin/env bash
#
# Label Avviksvarsel's alert decisions with realized outcomes, by hand.
#
# Normally unattended: the same SQL runs as the "varsel-label-daily" BigQuery scheduled query at
# 05:30 Oslo (03:30 and 04:30 UTC; the .sql header says why). Run this to backfill after the
# schedule has been down, to label a fresh environment for the first time, or to see the verdicts
# immediately after a day of testing rather than waiting for the morning.
#
# Safe to re-run and safe to run twice in a row: the INSERT skips decision subjects that already
# have a row, so nothing is duplicated. Idempotence is the reason the window is 10 days rather
# than 1 — a week of missed runs catches up on its own.
#
# Cost ~$0.015/run (measured 2026-09-23: 2.30 GB dry run and billed alike, an 11-day
# partition-pruned slice of last_recorded reading only the columns the joins need).
#
# PROJECT matters: dev and prd each hold their own decisions and label them separately. This table
# is deliberately absent from copy_runtime_to_prd.sh — copying it would overwrite one
# environment's real history with the other's.
#
# ---------------------------------------------------------------------------------------------
# REGISTERING THE SCHEDULE (one-off, per project — nothing in CI or Helm does this)
#
# ALREADY REGISTERED IN BOTH ENVIRONMENTS as "varsel-label-daily", first run SUCCEEDED in each:
#   dev  2026-08-01, projects/305411154596/locations/europe/transferConfigs/6a735413-0000-2192-b918-14223bba7f62
#   prd  2026-08-05, projects/925999097557/locations/europe/transferConfigs/6a766b6c-0000-294f-93f0-9898fbd2fc35
#
# prd was held back for four days on "can its SA read ent-data-sharing-ext-prd?" — prd is fed by
# free copies and had never scanned SIRI history. The dataset ACL answers it: realtime_siri_et
# grants READER to allAuthenticatedUsers, the same pattern as the NSR registry. Checking the ACL
# first would have unblocked it immediately; the AI.CLASSIFY lesson (API enablement != IAM role)
# says to confirm functionally anyway, and creating the config fires one run that does exactly that.
#
# --service_account_name is REQUIRED, not optional polish. Without it `bq mk` tries to register the
# job against your personal credentials and blocks on an interactive OAuth consent prompt, which
# fails outright in any non-interactive shell. Running as the application SA also means the
# schedule does not break when the person who created it changes teams. demo-recent-daily is owned
# by the same SA.
#
#   bq mk --transfer_config \
#     --project_id=ent-smartavvik-dev \
#     --location=EU \
#     --data_source=scheduled_query \
#     --display_name="varsel-label-daily" \
#     --schedule="every 1 hours from 03:30 to 04:45" \
#     --service_account_name="application@ent-smartavvik-dev.iam.gserviceaccount.com" \
#     --params="$(python3 -c 'import json;print(json.dumps({"query":open("data/queries/label_varsel_decisions.sql").read()}))')"
#
# Creating the config fires one run immediately, so a mistake surfaces at once rather than the next morning.
# An existing config takes the same schedule with
#   bq update --transfer_config --schedule="every 1 hours from 03:30 to 04:45" <config resource name>
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
# BOTH configs, always. Adding a column to varsel_decision_outcomes and updating only one leaves the
# other environment's job broken every night until someone notices — and a schema change is what
# makes the copies diverge in the first place. The ALTER and both updates are ONE operation.
#
# No --target_dataset / destination table: this is a multi-statement script that does its own
# DELETE + INSERT, like copy_demo_to_prd_daily.sql.
#
# One ordering constraint, for any NEW environment: the app must have started there at least once
# first — the outcomes table is created at startup (disruptions/api/create_varsel_decisions.sql),
# not by this job.
#
# And one clock. The window is 10 days, so a decision not labelled within 10 days of its operating
# day is unknown forever. Registering late costs exactly the decisions that aged out meanwhile.
#
# Force a run without waiting for the morning, and check it:
#   bq mk --transfer_run --run_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)" <config resource name>
#   bq ls --transfer_run <config resource name>
# ---------------------------------------------------------------------------------------------
set -euo pipefail

PROJECT="${1:-ent-smartavvik-dev}"

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
