#!/usr/bin/env bash
#
# Deploy the /disruptions API to Cloud Run.
#
# Deploys from source (Cloud Build builds the Dockerfile in this directory).
# Run from disruptions/api/ — the script cd's there itself so it works from anywhere.
#
# Flags worth knowing (see CLAUDE.md for the full rationale):
#   --min/--max-instances 1  one background poller thread + in-process cache => single always-on instance
#   --no-cpu-throttling      keeps CPU allocated between requests so the poller keeps running
#
set -euo pipefail

SERVICE="kix-avvik-disruptions"
PROJECT="ent-kixavvik-dev"
REGION="europe-west1"
SERVICE_ACCOUNT="application@ent-kixavvik-dev.iam.gserviceaccount.com"

# Run from the directory this script lives in (disruptions/api/).
cd "$(dirname "$0")"

# Render the eval notebooks to static HTML (runs their BigQuery cells under your ADC)
# so the fresh snapshots ship in the deploy source and get served at /eval. Set
# SKIP_EVAL_EXPORT=1 to reuse the existing eval_static/ (e.g. a code-only redeploy).
if [ "${SKIP_EVAL_EXPORT:-}" != "1" ]; then
  ./export_eval.sh
fi

gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --service-account "$SERVICE_ACCOUNT" \
  --min-instances 1 \
  --max-instances 1 \
  --no-cpu-throttling \
  "$@"
