# Cloud Run image for the /disruptions API (main.py in this directory).
#
# Deliberately slim: installs only FastAPI + the BigQuery client, none of the
# repo's notebook/plotting dependencies. The app loads its sibling *.sql files
# via Path(__file__).parent, so they must live next to main.py in the image —
# the build context is this directory, so `COPY . .` brings them along.
FROM python:3.13-slim

# uv for fast installs (project convention; see CLAUDE.md).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY . .

# Cloud Run routes traffic to $PORT (default 8080). Single worker on purpose:
# the app keeps an in-process cache and runs one background poller thread, so
# multiple workers would mean duplicate pollers and unshared caches.
ENV PORT=8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
