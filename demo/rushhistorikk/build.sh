#!/usr/bin/env bash
#
# Build the Rushhistorikk UI into disruptions/api/rushhistorikk_static/ (gitignored — the image
# builds it in the Dockerfile's node stage; this is for local dev).
set -euo pipefail
cd "$(dirname "$0")"

npm install --no-fund --no-audit
npm run build
