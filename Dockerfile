# Image for the /disruptions API plus its two front-ends (/demo and /varsel).
#
# Build context is the REPO ROOT (not disruptions/api) because the front-end
# sources live in demo/reisedemo and demo/avviksvarsel, outside the API dir. The
# CI build (.github/workflows/ci.yml) and the Cloud Run deploy (disruptions/api/
# deploy.sh) both build this file from the root.
#
# Why multi-stage: the /demo and /varsel UIs are Vite bundles generated from
# source — the generated demo_static/ and varsel_static/ are gitignored, so they
# are NOT in the checkout and must be built here. (The /eval snapshots are
# different: they run BigQuery under a developer's ADC, so they are rendered
# offline and committed under disruptions/api/eval_static/ — see deploy.sh.)

# ---- Stage 1: build the two Vite front-ends -------------------------------
# Vite 7 needs Node >=20.19 / >=22.12; node:22-slim satisfies that.
FROM node:22-slim AS frontends

# Each app's vite.config.ts writes its bundle to ../../disruptions/api/<x>_static,
# i.e. /build/disruptions/api/{demo,varsel}_static from the app dirs below. Create
# that parent up front so Vite's emptyOutDir always has somewhere to write.
RUN mkdir -p /build/disruptions/api

# Deps first (cache-friendly: only re-runs when a lockfile changes), then sources,
# then build. npm ci is reproducible from the committed lockfiles.
WORKDIR /build/demo/reisedemo
COPY demo/reisedemo/package.json demo/reisedemo/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY demo/reisedemo/ ./
RUN npm run build

WORKDIR /build/demo/avviksvarsel
COPY demo/avviksvarsel/package.json demo/avviksvarsel/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY demo/avviksvarsel/ ./
RUN npm run build

# ---- Stage 2: the Python API image ----------------------------------------
# Deliberately slim: FastAPI + the BigQuery client, none of the repo's
# notebook/plotting deps. main.py loads its sibling *.sql files (and eval_static/)
# via Path(__file__).parent, so they must sit next to main.py in the image.
FROM python:3.13-slim

# uv for fast installs (project convention; see CLAUDE.md).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY disruptions/api/requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

# App code + *.sql + the committed eval_static/ snapshots. demo_static/ and
# varsel_static/ are gitignored (and .dockerignore'd), so they are absent here.
COPY disruptions/api/ .

# The freshly built /demo and /varsel bundles from stage 1. Copied last so they
# always win over any stale locally-built copy that slipped into the context.
COPY --from=frontends /build/disruptions/api/demo_static ./demo_static
COPY --from=frontends /build/disruptions/api/varsel_static ./varsel_static

# Cloud Run / the chart route traffic to $PORT (default 8080). Single worker on
# purpose: the app keeps an in-process cache and one background poller thread, so
# multiple workers would mean duplicate pollers and unshared caches.
ENV PORT=8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
