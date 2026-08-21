# Image for the /disruptions API plus its front-ends (/demo, /varsel, /historikk,
# /tilbakeblikk).
#
# Build context is the REPO ROOT (not disruptions/api) because the front-end
# sources live in demo/reisedemo, demo/avviksvarsel, demo/varselhistorikk and
# demo/tilbakeblikk, outside the API dir. The CI build
# (.github/workflows/ci.yml) builds this file from the root.
#
# Why multi-stage: those UIs are Vite bundles generated from source — the
# generated demo_static/, varsel_static/, historikk_static/ and
# tilbakeblikk_static/ are gitignored, so they are NOT in the checkout and must
# be built here. (The /eval snapshots are different: they run BigQuery under a
# developer's ADC, so they are rendered offline and committed under
# disruptions/api/eval_static/ — see export_eval.sh.)
#
# Note the /tilbakeblikk bundle is built into every image regardless of
# environment: whether the surface is SERVED is decided at runtime by
# TILBAKEBLIKK_ENABLED (see main.py), not by what the image contains.
#
# Runtime base is distroless (Entur Golden Path for Python; see entur/ai
# guides/reference/docker.md). It ships only glibc + the CPython 3.13 runtime —
# no shell, apt, perl, tar, coreutils or util-linux — which removes the bulk of
# the base-image CVEs that a full Debian userland (python:3.13-slim) drags in,
# and it runs as non-root by default. Python deps are therefore installed into a
# venv in a slim builder stage and copied in; the distroless interpreter runs
# them via PYTHONPATH. Reference: entur/epimetheus uses the same pattern.

# ---- Stage 1: build the Vite front-ends -----------------------------------
# Vite 8 needs Node >=20.19 / >=22.12; node:24-slim (the current LTS line) satisfies that.
FROM node:24-slim AS frontends

# Each app's vite.config.ts writes its bundle to ../../disruptions/api/<x>_static,
# i.e. /build/disruptions/api/{demo,varsel,historikk,tilbakeblikk}_static from the app dirs below.
# Create that parent up front so Vite's emptyOutDir always has somewhere to write.
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

WORKDIR /build/demo/varselhistorikk
COPY demo/varselhistorikk/package.json demo/varselhistorikk/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY demo/varselhistorikk/ ./
RUN npm run build

WORKDIR /build/demo/tilbakeblikk
COPY demo/tilbakeblikk/package.json demo/tilbakeblikk/package-lock.json ./
RUN npm ci --no-fund --no-audit
COPY demo/tilbakeblikk/ ./
RUN npm run build

# ---- Stage 2: install the Python deps into a venv -------------------------
# Built on python:3.13-slim (Debian trixie, same CPython 3.13 ABI as the
# distroless runtime below) so the wheels copied across are binary-compatible.
# The venv is self-contained under /opt/venv; the runtime stage puts its
# site-packages on PYTHONPATH rather than using the venv's own interpreter.
FROM python:3.13-slim AS pybuild

# uv for fast installs (project convention; see CLAUDE.md). only-system pins uv to
# the image's CPython so the venv is 3.13 (never a uv-downloaded interpreter).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

WORKDIR /app
COPY disruptions/api/requirements.txt .
RUN uv venv /opt/venv \
 && uv pip install --python /opt/venv/bin/python --no-cache -r requirements.txt

# ---- Stage 3: the distroless runtime image --------------------------------
# main.py loads its sibling *.sql files (and eval_static/) via Path(__file__).parent,
# so they must sit next to main.py in the image (WORKDIR /app below).
FROM gcr.io/distroless/python3-debian13:nonroot

# Run the copied venv's packages on the image's own CPython 3.13. /app is on the
# path too so `uvicorn main:app` can import main.py.
ENV PYTHONPATH=/opt/venv/lib/python3.13/site-packages:/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Python deps (installed in stage 2).
COPY --from=pybuild /opt/venv /opt/venv

# App code + *.sql + the committed eval_static/ snapshots. demo_static/,
# varsel_static/, historikk_static/ and tilbakeblikk_static/ are gitignored (and
# .dockerignore'd), so they are absent here.
COPY disruptions/api/ .

# The freshly built /demo, /varsel, /historikk and /tilbakeblikk bundles from
# stage 1. Copied last so they always win over any stale locally-built copy that
# slipped into the context.
COPY --from=frontends /build/disruptions/api/demo_static ./demo_static
COPY --from=frontends /build/disruptions/api/varsel_static ./varsel_static
COPY --from=frontends /build/disruptions/api/historikk_static ./historikk_static
COPY --from=frontends /build/disruptions/api/tilbakeblikk_static ./tilbakeblikk_static

# The Helm chart routes traffic to 8080. Single worker on purpose: the app keeps
# an in-process cache and one background poller thread, so multiple workers would
# mean duplicate pollers and unshared caches.
#
# Exec form (distroless has no shell): the image's ENTRYPOINT is the CPython
# interpreter, so this runs `python -m uvicorn ...`. The port is fixed to 8080
# rather than expanding $PORT (no shell to expand it); nothing in the deploy
# overrides it.
EXPOSE 8080
CMD ["-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
