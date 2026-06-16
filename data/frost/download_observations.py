# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Download the last 2 years of frost.met.no observations for every near-quay
station, at hourly-and-coarser resolution, and save one gzipped CSV per station.

Driven by timeseries_in_window.json (produced by fetch_timeseries.py). For each
station it groups the available elements by time resolution, splits each request
to stay under frost's 100,000-observation-per-request cap (with adaptive time
bisection as a safety net), dedupes, and writes observations/<SNid>.csv.gz.

Resumable: a station whose final .csv.gz exists is skipped. Writes to a .tmp file
and renames on success so partial files are never mistaken for complete ones.

Usage:
  uv run download_observations.py                 # download all
  uv run download_observations.py --workers 6
  uv run download_observations.py --limit 5       # smoke-test first 5 stations
"""
import argparse
import base64
import csv
import gzip
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).parent
OUTDIR = HERE / "observations"

WINDOW_START = datetime(2024, 6, 16, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 6, 16, tzinfo=timezone.utc)
WINDOW_SECONDS = (WINDOW_END - WINDOW_START).total_seconds()
WINDOW_DAYS = WINDOW_SECONDS / 86400

# Resolutions to keep (hourly and coarser); everything else (PT1M/PT10M/PT30M/...) dropped.
KEEP = {"PT1H", "PT6H", "PT12H", "P1D", "P1M", "P3M", "P6M", "P1Y"}
STEPS = {  # approx timesteps in the window, used to size requests under the cap
    "PT1H": WINDOW_DAYS * 24, "PT6H": WINDOW_DAYS * 4, "PT12H": WINDOW_DAYS * 2,
    "P1D": WINDOW_DAYS, "P1M": 24, "P3M": 8, "P6M": 4, "P1Y": 2,
}
OBS_CAP = 90000          # stay safely under frost's 100k/request limit
ELEM_BATCH = 40          # max elements per request
ELEM_CHARS = 1800        # max joined-elements length per request (URL safety)
MIN_CHUNK_SECONDS = 3600 # don't bisect time windows below 1 hour

CSV_COLS = [
    "source_id", "reference_time", "element_id", "time_offset", "time_resolution",
    "level_type", "level_value", "level_unit", "value", "unit",
    "quality_code", "performance_category", "exposure_category",
]

_print_lock = threading.Lock()
_stats = {"rows": 0, "stations": 0, "requests": 0}


def log(msg):
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def load_cid():
    for line in open(HERE / "auth.txt"):
        if line.startswith("client_id:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("no client_id in auth.txt")


AUTH = None  # set in main


def _open(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + AUTH)
    return urllib.request.urlopen(req, timeout=180)


def fetch_obs(source, elements, res, t0, t1, depth=0):
    """Fetch observations for one element batch + resolution over [t0,t1).

    Returns a list of flattened row dicts. Recursively bisects the time window if
    frost reports the 100k cap would be exceeded. Retries transient errors.
    """
    rt = f"{t0.strftime('%Y-%m-%dT%H:%M:%SZ')}/{t1.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    url = "https://frost.met.no/observations/v0.jsonld?" + urllib.parse.urlencode({
        "sources": source, "elements": ",".join(elements),
        "referencetime": rt, "timeresolutions": res,
    })
    backoff = 2
    for attempt in range(6):
        try:
            with _print_lock:
                _stats["requests"] += 1
            raw = _open(url).read()
            d = json.loads(raw)
            return flatten(d.get("data", []))
        except urllib.error.HTTPError as e:
            body = e.read()
            if e.code in (404, 412):
                return []  # 404/412 = no data for this source/time/element slice
            if e.code == 403 and b"exceeds the maximum" in body:
                span = (t1 - t0).total_seconds()
                if span > MIN_CHUNK_SECONDS and len(elements) >= 1:
                    mid = t0 + (t1 - t0) / 2
                    return (fetch_obs(source, elements, res, t0, mid, depth + 1)
                            + fetch_obs(source, elements, res, mid, t1, depth + 1))
                if len(elements) > 1:  # can't shrink time further; split elements
                    h = len(elements) // 2
                    return (fetch_obs(source, elements[:h], res, t0, t1, depth + 1)
                            + fetch_obs(source, elements[h:], res, t0, t1, depth + 1))
                log(f"  ! giving up {source} {res} {elements} {rt}: {body[:120]}")
                return []
            if e.code in (429, 500, 502, 503, 504) and attempt < 5:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            log(f"  ! HTTP {e.code} {source} {res} {rt}: {body[:160]}")
            return []
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 5:
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            log(f"  ! neterr {source} {res} {rt}: {e}")
            return []
    return []


def flatten(data):
    rows = []
    for rec in data:
        src = rec.get("sourceId", "")
        rt = rec.get("referenceTime", "")
        for o in rec.get("observations", []):
            lvl = o.get("level") or {}
            rows.append({
                "source_id": src,
                "reference_time": rt,
                "element_id": o.get("elementId", ""),
                "time_offset": o.get("timeOffset", ""),
                "time_resolution": o.get("timeResolution", ""),
                "level_type": lvl.get("levelType", ""),
                "level_value": lvl.get("value", ""),
                "level_unit": lvl.get("unit", ""),
                "value": o.get("value", ""),
                "unit": o.get("unit", ""),
                "quality_code": o.get("qualityCode", ""),
                "performance_category": o.get("performanceCategory", ""),
                "exposure_category": o.get("exposureCategory", ""),
            })
    return rows


def batch_elements(elements):
    """Yield element batches bounded by count and joined-string length."""
    batch, length = [], 0
    for el in elements:
        add = len(el) + 1
        if batch and (len(batch) >= ELEM_BATCH or length + add > ELEM_CHARS):
            yield batch
            batch, length = [], 0
        batch.append(el)
        length += add
    if batch:
        yield batch


def time_chunks(n):
    """Split the window into n contiguous datetime sub-windows."""
    step = WINDOW_SECONDS / n
    bounds = [WINDOW_START + timedelta(seconds=step * i) for i in range(n)] + [WINDOW_END]
    return list(zip(bounds[:-1], bounds[1:]))


def process_station(source, by_res):
    """Download all kept resolutions for one station; write <source>.csv.gz."""
    final = OUTDIR / f"{source}.csv.gz"
    if final.exists():
        return source, -1  # already done
    tmp = OUTDIR / f"{source}.csv.gz.tmp"
    seen = set()
    nrows = 0
    with gzip.open(tmp, "wt", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS)
        w.writeheader()
        for res, elements in by_res.items():
            elements = sorted(elements)
            for batch in batch_elements(elements):
                est = len(batch) * STEPS.get(res, 1)
                n = max(1, -(-int(est) // OBS_CAP))  # ceil
                for t0, t1 in time_chunks(n):
                    for row in fetch_obs(source, batch, res, t0, t1):
                        key = (row["reference_time"], row["element_id"],
                               row["time_offset"], row["time_resolution"],
                               row["level_type"], row["level_value"])
                        if key in seen:
                            continue
                        seen.add(key)
                        w.writerow(row)
                        nrows += 1
    tmp.rename(final)
    with _print_lock:
        _stats["rows"] += nrows
        _stats["stations"] += 1
        done = _stats["stations"]
    log(f"[{done}] {source}: {nrows:,} rows  "
        f"(cum {_stats['rows']:,} rows, {_stats['requests']:,} reqs)")
    return source, nrows


def main():
    global AUTH
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=None, help="only first N stations")
    args = ap.parse_args()

    AUTH = base64.b64encode((load_cid() + ":").encode()).decode()
    OUTDIR.mkdir(exist_ok=True)

    # Build {station: {resolution: set(elementId)}} from the manifest, kept resolutions only.
    manifest = json.load(open(HERE / "timeseries_in_window.json"))
    stations = {}
    for s in manifest:
        res = s.get("timeResolution")
        if res not in KEEP:
            continue
        sid = s.get("sourceId", "").split(":")[0]
        el = s.get("elementId")
        if not sid or not el:
            continue
        stations.setdefault(sid, {}).setdefault(res, set()).add(el)

    ids = sorted(stations)
    if args.limit:
        ids = ids[:args.limit]
    todo = [i for i in ids if not (OUTDIR / f"{i}.csv.gz").exists()]
    log(f"stations with kept-resolution data: {len(ids)}  "
        f"(already done: {len(ids) - len(todo)}, to download: {len(todo)})")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(process_station, i, stations[i]) for i in todo]
        for _ in as_completed(futs):
            pass

    log(f"\nDONE: {_stats['stations']} stations, {_stats['rows']:,} rows, "
        f"{_stats['requests']:,} requests, {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
