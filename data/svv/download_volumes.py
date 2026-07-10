# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Download 2 years of hourly per-direction traffic volumes for the curated E18
corridor registration points (points.json) from the SVV Trafikkdata GraphQL API
(https://trafikkdata-api.atlas.vegvesen.no/, open data / NLOD, no auth), and
save one gzipped NDJSON file per point under volumes/.

One output row per (point, hour, direction):
  point_id, hour_start_utc, hour_start_oslo, date_oslo, heading,
  toward_oslo (from points.json), volume, coverage_pct, total_volume.
byDirection rows are kept (summing directions destroys the rush signal);
total_volume is the hour's all-direction total, repeated on each direction row.
Hours come back in Europe/Oslo local time with offset -- both UTC and the naive
Oslo-local hour are stored so DST days join cleanly with rail_calls.

Window matches rail_calls: 2024-06-01 .. 2026-06-01 (Europe/Oslo), i.e.
17,520 hours per point, paginated 100 hours per request (~176 pages/point).

Resumable: a point whose final .ndjson.gz exists is skipped. Writes to a .tmp
file and renames on success so partial files are never mistaken for complete
ones. Polite client: small sleep between pages, exponential backoff on errors,
contact User-Agent on every request.

Usage:
  uv run download_volumes.py                # download all points
  uv run download_volumes.py --workers 3
  uv run download_volumes.py --limit 1      # smoke-test first point
"""
import argparse
import gzip
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).parent
OUTDIR = HERE / "volumes"

API = "https://trafikkdata-api.atlas.vegvesen.no/"
USER_AGENT = "entur-kixavvik-feasibility thomas.oldervoll@gmail.com"

# Europe/Oslo local window, matching the rail_calls window (2024-06-01..2026-05-31).
WINDOW_FROM = "2024-06-01T00:00:00+02:00"
WINDOW_TO = "2026-06-01T00:00:00+02:00"

PAGE_SIZE = 100          # hours per page (API max)
PAGE_SLEEP = 0.2         # polite pause between requests, seconds

QUERY = """
query ($pid: String!, $from: ZonedDateTime!, $to: ZonedDateTime!, $first: PositiveInt!, $after: String) {
  trafficData(trafficRegistrationPointId: $pid) {
    volume {
      byHour(from: $from, to: $to, first: $first, after: $after) {
        edges {
          node {
            from
            total { volumeNumbers { volume } coverage { percentage } }
            byDirection {
              heading
              total { volumeNumbers { volume } coverage { percentage } }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

_print_lock = threading.Lock()
_stats = {"rows": 0, "points": 0, "requests": 0}


def log(msg):
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def gql(variables):
    """POST one GraphQL request; exponential backoff on transient errors."""
    body = json.dumps({"query": QUERY, "variables": variables}).encode()
    backoff = 2
    for attempt in range(6):
        try:
            with _print_lock:
                _stats["requests"] += 1
            req = urllib.request.Request(API, data=body, headers={
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            })
            d = json.loads(urllib.request.urlopen(req, timeout=60).read())
            if d.get("errors"):
                raise RuntimeError(f"GraphQL errors: {d['errors'][:1]}")
            return d["data"]
        except (urllib.error.URLError, TimeoutError, RuntimeError) as e:
            code = getattr(e, "code", None)
            if isinstance(e, urllib.error.HTTPError) and code not in (429, 500, 502, 503, 504):
                raise
            if attempt == 5:
                raise
            log(f"  ! retry ({e}); sleeping {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    raise RuntimeError("unreachable")


def rows_for_node(point_id, node, toward_heading):
    """Flatten one byHour node into one row per direction."""
    local = datetime.fromisoformat(node["from"])  # Oslo local with offset
    utc = local.astimezone(timezone.utc)
    tot = node["total"] or {}
    tot_vn = tot.get("volumeNumbers") or {}
    total_volume = tot_vn.get("volume")
    for d in node["byDirection"] or []:
        dt = d.get("total") or {}
        vn = dt.get("volumeNumbers") or {}
        cov = dt.get("coverage") or {}
        yield {
            "point_id": point_id,
            "hour_start_utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hour_start_oslo": local.replace(tzinfo=None).isoformat(),
            "date_oslo": local.date().isoformat(),
            "heading": d["heading"],
            "toward_oslo": d["heading"] == toward_heading,
            "volume": vn.get("volume"),
            "coverage_pct": cov.get("percentage"),
            "total_volume": total_volume,
        }


def process_point(point):
    """Download the full window for one point; write volumes/<id>.ndjson.gz."""
    pid = point["id"]
    final = OUTDIR / f"{pid}.ndjson.gz"
    if final.exists():
        return pid, -1  # already done
    tmp = OUTDIR / f"{pid}.ndjson.gz.tmp"
    toward = point["toward_oslo_heading"]
    nrows = npages = 0
    after = None
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        while True:
            data = gql({"pid": pid, "from": WINDOW_FROM, "to": WINDOW_TO,
                        "first": PAGE_SIZE, "after": after})
            conn = data["trafficData"]["volume"]["byHour"]
            for edge in conn["edges"]:
                for row in rows_for_node(pid, edge["node"], toward):
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    nrows += 1
            npages += 1
            pi = conn["pageInfo"]
            if not pi["hasNextPage"]:
                break
            after = pi["endCursor"]
            time.sleep(PAGE_SLEEP)
    tmp.rename(final)
    with _print_lock:
        _stats["rows"] += nrows
        _stats["points"] += 1
        done = _stats["points"]
    log(f"[{done}] {pid} ({point['name']}): {nrows:,} rows, {npages} pages  "
        f"(cum {_stats['rows']:,} rows, {_stats['requests']:,} reqs)")
    return pid, nrows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=int, default=None, help="only first N points")
    args = ap.parse_args()

    OUTDIR.mkdir(exist_ok=True)
    points = json.load(open(HERE / "points.json"))["points"]
    if args.limit:
        points = points[: args.limit]
    todo = [p for p in points if not (OUTDIR / f"{p['id']}.ndjson.gz").exists()]
    log(f"points: {len(points)}  (already done: {len(points) - len(todo)}, "
        f"to download: {len(todo)})")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(process_point, p) for p in todo]
        for f in as_completed(futs):
            f.result()  # propagate failures

    log(f"\nDONE: {_stats['points']} points, {_stats['rows']:,} rows, "
        f"{_stats['requests']:,} requests, {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
