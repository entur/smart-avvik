"""Checks for the /demo-api endpoints that need no data.

Hermetic: the BigQuery client is stubbed before main is imported, and the app's lifespan is never
entered — so no poller starts and nothing here touches BigQuery or the network. That is also the
point of the one endpoint covered so far: /demo-api/config answers with no dataset behind it, which
is what lets the demo page name its environment even where the demo tables were never copied in.

Run: uv run --with fastapi --with httpx --with pydantic python test_demo_api.py  (or under pytest)
"""

import os
from contextlib import contextmanager

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import main
from fastapi.testclient import TestClient

# Not entered as a context manager on purpose: that would run the lifespan hook, which starts the
# background poller and loads BigQuery lookups.
client = TestClient(main.app)


@contextmanager
def _deployed_as(project: str | None):
    """Run the body as if the app were deployed in `project` (ambient env restored afterwards)."""
    before = {k: os.environ.get(k) for k in ("GOOGLE_CLOUD_PROJECT", "KIX_ENVIRONMENT")}
    os.environ.pop("KIX_ENVIRONMENT", None)  # a local override would mask the project
    if project is None:
        os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
    else:
        os.environ["GOOGLE_CLOUD_PROJECT"] = project
    try:
        yield
    finally:
        for key, value in before.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_config_names_the_non_prod_environment():
    """The page shows this behind its heading. /demo is shown to partners, and dev serves the same
    page from the same image as prd — the environment is the one thing the page cannot infer."""
    with _deployed_as("ent-smartavvik-dev"):
        r = client.get("/demo-api/config")
    assert r.status_code == 200, r.text
    assert r.json()["environment"] == "dev"


def test_config_leaves_production_unlabelled():
    with _deployed_as("ent-smartavvik-prd"):
        assert client.get("/demo-api/config").json()["environment"] is None
    with _deployed_as(None):
        assert client.get("/demo-api/config").json()["environment"] is None


def test_config_needs_no_demo_dataset():
    """Why it is its own endpoint rather than a field on /demo-api/meta: there is no BigQuery client
    here at all, which is the fresh-environment case where meta cannot answer and the masthead
    still renders."""
    assert main.bq_client is None
    with _deployed_as("ent-smartavvik-tst"):
        assert client.get("/demo-api/config").json()["environment"] == "tst"


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} /demo-api checks passed.")


if __name__ == "__main__":
    _run()
