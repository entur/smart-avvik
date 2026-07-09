#!/usr/bin/env bash
#
# Build the Avviksvarsel PWA into disruptions/api/varsel_static/, where the API serves it at
# /varsel (same generated-directory pattern as demo/reisedemo -> demo_static/).
# Requires node >= 20. Run from anywhere for local dev; the image build (root
# Dockerfile's node stage) does the same build for deploys.
set -euo pipefail
cd "$(dirname "$0")"

npm install --no-fund --no-audit
npm run build
