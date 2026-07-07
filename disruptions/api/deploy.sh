#!/usr/bin/env bash
#
# Deploy the /disruptions API to Cloud Run.
#
# Deploys from the REPO ROOT (Cloud Build builds the root Dockerfile). That
# Dockerfile is multi-stage: it builds the /demo and /varsel front-ends itself, so
# — unlike before — this script no longer runs their build.sh scripts. It is the
# same image CI builds and deploys to Kubernetes.
#
# The repo-root context is what lets the Dockerfile reach demo/reisedemo and
# demo/avviksvarsel; .gcloudignore (at the repo root) keeps the upload small.
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

# Repo root (this script lives in disruptions/api/).
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# The /eval snapshots (disruptions/api/eval_static/) are committed and ship as-is,
# so a normal deploy does NOT re-render them. Set RUN_EVAL_EXPORT=1 to refresh them
# first — this runs the marimo notebooks' BigQuery cells under your ADC. Do it (and
# commit the result) after a model/calibration retrain; see CLAUDE.md.
if [ "${RUN_EVAL_EXPORT:-}" = "1" ]; then
  "$(dirname "$0")/export_eval.sh"
fi

cd "$REPO_ROOT"

gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT" \
  --region "$REGION" \
  --service-account "$SERVICE_ACCOUNT" \
  --min-instances 1 \
  --max-instances 1 \
  --no-cpu-throttling \
  "$@"
