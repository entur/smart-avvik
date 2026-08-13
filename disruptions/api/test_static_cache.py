"""Checks for how long a browser may keep the front-ends' static files.

Hermetic in a way that matters here: the suite builds its OWN static directory in a temp dir rather
than reading demo_static/ varsel_static/ historikk_static/. Those are generated at image-build time
and simply absent in a fresh checkout and in CI, so a test that read them would pass by not running
— on the one machine where nobody would notice.

What is actually at stake: index.html carries no hash in its name, and its <script src> is the only
thing naming the current bundle. Left heuristically cacheable it pins a returning browser to the
previous deploy's bundle — measured on prd 2026-08-11, 40 minutes after a deploy, and it presents
as "the fix did not ship".

Run: uv run --with fastapi --with httpx --with pydantic python test_static_cache.py  (or pytest)
"""

from pathlib import Path
from tempfile import TemporaryDirectory

# --- Stub the BigQuery client BEFORE importing main (it builds one at module scope). ---
from google.cloud import bigquery as _bq

_bq.Client = lambda *a, **k: None  # type: ignore[assignment]

import main
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"

_tmp = TemporaryDirectory()
_root = Path(_tmp.name)
(_root / "assets").mkdir()
# A front-end as Vite lays it out: an unhashed entry point, hashed payload beside it, plus the
# unhashed odds and ends the PWA ships at the root.
(_root / "index.html").write_text('<script src="/x/assets/index-B8Bt7o5h.js"></script>')
(_root / "assets" / "index-B8Bt7o5h.js").write_text("console.log(1)")
(_root / "assets" / "Entur-Nationale-Regular-BpXINGXk.woff").write_bytes(b"font")
(_root / "manifest.webmanifest").write_text("{}")
(_root / "sw.js").write_text("// service worker")

_app = FastAPI()
_app.mount("/x", main._VersionedStaticFiles(directory=_root, html=True), name="x")
client = TestClient(_app)


def test_hashed_assets_are_immutable():
    """The payload. Its URL names its own content, so it never needs revalidating."""
    for path in ("/x/assets/index-B8Bt7o5h.js", "/x/assets/Entur-Nationale-Regular-BpXINGXk.woff"):
        r = client.get(path)
        assert r.status_code == 200, r.text
        assert r.headers["cache-control"] == IMMUTABLE, path


def test_index_html_must_be_revalidated():
    """The entry point, and the whole reason this exists.

    index.html keeps its name across every deploy while the bundle it points at does not, so a
    browser allowed to keep it stays on the previous deploy's JavaScript.
    """
    r = client.get("/x/index.html")
    assert r.status_code == 200
    assert r.headers["cache-control"] == REVALIDATE


def test_the_directory_url_is_the_same_file_and_gets_the_same_rule():
    """html=True serves index.html for /x/ — the URL people actually visit.

    Worth its own check: it reaches the same file by a different route through StaticFiles, and a
    rule applied only to the explicit filename would miss every real visitor.
    """
    r = client.get("/x/")
    assert r.status_code == 200
    assert r.headers["cache-control"] == REVALIDATE


def test_unhashed_root_files_are_revalidated():
    """The PWA's manifest and service worker keep their names too.

    A cached service worker is the nastier version of the same bug: it outlives the page and can
    keep serving an old shell on its own.
    """
    for path in ("/x/manifest.webmanifest", "/x/sw.js"):
        assert client.get(path).headers["cache-control"] == REVALIDATE, path


def test_cache_control_survives_a_304():
    """The subtle one.

    Starlette decides "not modified" inside file_response and returns a NotModifiedResponse built
    from the headers it held at that moment. A 304 that answered without Cache-Control would put
    the file straight back under heuristic caching on every revalidation — undoing this on exactly
    the request that was supposed to enforce it.
    """
    first = client.get("/x/index.html")
    etag = first.headers["etag"]
    again = client.get("/x/index.html", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["cache-control"] == REVALIDATE

    asset = client.get("/x/assets/index-B8Bt7o5h.js")
    revalidated = client.get(
        "/x/assets/index-B8Bt7o5h.js", headers={"If-None-Match": asset.headers["etag"]}
    )
    assert revalidated.status_code == 304
    assert revalidated.headers["cache-control"] == IMMUTABLE


def test_immutability_is_claimed_only_for_files_under_assets():
    """`immutable` is a promise for a year and cannot be taken back.

    So it is granted by position — the assets/ directory Vite fills with hashed names — and a path
    that cannot be placed under the mount root gets the cautious answer instead.
    """
    mount = main._VersionedStaticFiles(directory=_root)
    assert mount._cache_control(_root / "assets" / "index-B8Bt7o5h.js") == IMMUTABLE
    assert mount._cache_control(_root / "index.html") == REVALIDATE
    # A same-named directory OUTSIDE the mount must not inherit the promise.
    assert mount._cache_control(Path("/somewhere/else/assets/index-abc.js")) == REVALIDATE


def test_every_real_static_mount_uses_the_caching_subclass():
    """A guard on the mounts themselves, so a fifth front-end cannot quietly opt out.

    Vacuous where the generated directories are absent (a fresh checkout, CI) — hence the checks
    above, which build their own. It earns its keep locally and in the image, where the mounts do
    exist.
    """
    static_mounts = [
        route for route in main.app.routes if isinstance(getattr(route, "app", None), StaticFiles)
    ]
    for route in static_mounts:
        assert isinstance(route.app, main._VersionedStaticFiles), (
            f"{route.path} is mounted with plain StaticFiles and would fall back to heuristic "
            "caching"
        )


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} static-cache checks passed.")


if __name__ == "__main__":
    _run()
