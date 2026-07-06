#!/usr/bin/env bash
#
# Build the journey-prediction demo UI into disruptions/api/demo_static/, where the
# API serves it at /demo (same generated-directory pattern as eval_static/).
# Requires node >= 20. Run from anywhere; deploy.sh calls this before deploying.
set -euo pipefail
cd "$(dirname "$0")"

npm install --no-fund --no-audit
npm run build
