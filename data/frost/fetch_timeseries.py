# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Fetch the availableTimeSeries manifest for every near-quay station, limited to
the download window, and estimate the resulting observation volume by resolution.

Writes timeseries_in_window.json: a flat list of series dicts (one per
source+element+resolution+level+timeOffset) that have data in the window. This is
the manifest the downloader iterates over.

Usage: uv run fetch_timeseries.py
"""
import base64
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
WINDOW = "2024-06-16/2026-06-16"          # last 2 years (today = 2026-06-16)
BATCH = 50                                 # sources per availableTimeSeries call

# Approx number of timesteps in the 2-year (731-day) window, per ISO-8601 resolution.
WINDOW_DAYS = 731
STEPS = {
    "PT1M": WINDOW_DAYS * 1440, "PT10M": WINDOW_DAYS * 144, "PT30M": WINDOW_DAYS * 48,
    "PT1H": WINDOW_DAYS * 24, "PT3H": WINDOW_DAYS * 8, "PT6H": WINDOW_DAYS * 4,
    "PT12H": WINDOW_DAYS * 2, "P1D": WINDOW_DAYS, "P1M": 24, "P3M": 8, "P6M": 4,
    "P1Y": 2,
}


def load_cid():
    with open(HERE / "auth.txt") as fh:
        for line in fh:
            if line.startswith("client_id:"):
                return line.split(":", 1)[1].strip()
    raise SystemExit("no client_id in auth.txt")


def get(url, cid, tries=5):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + base64.b64encode((cid + ":").encode()).decode())
    for attempt in range(tries):
        try:
            return json.loads(urllib.request.urlopen(req, timeout=120).read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"data": []}        # no series for this batch/window
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise


def main():
    cid = load_cid()
    with open(HERE / "near_stations.json") as fh:
        ids = [s["id"] for s in json.load(fh)]
    print(f"near stations: {len(ids)}", file=sys.stderr)

    all_series = []
    for i in range(0, len(ids), BATCH):
        batch = ids[i:i + BATCH]
        url = "https://frost.met.no/observations/availableTimeSeries/v0.jsonld?" + \
            urllib.parse.urlencode({"sources": ",".join(batch), "referencetime": WINDOW})
        d = get(url, cid)
        all_series.extend(d.get("data", []))
        print(f"  batch {i//BATCH+1}/{-(-len(ids)//BATCH)}: "
              f"+{len(d.get('data',[]))} series (total {len(all_series)})", file=sys.stderr)
        time.sleep(0.2)

    with open(HERE / "timeseries_in_window.json", "w") as fh:
        json.dump(all_series, fh, ensure_ascii=False)
    print(f"\nwrote timeseries_in_window.json: {len(all_series)} series", file=sys.stderr)

    # Volume estimate (upper bound: assumes every timestep present).
    import collections
    by_res_series = collections.Counter()
    by_res_steps = collections.Counter()
    stations = set()
    for s in all_series:
        r = s.get("timeResolution", "?")
        by_res_series[r] += 1
        by_res_steps[r] += STEPS.get(r, 0)
        stations.add(s.get("sourceId", "").split(":")[0])

    print(f"\ndistinct stations with data in window: {len(stations)}", file=sys.stderr)
    print("\nresolution    series   est.max obs", file=sys.stderr)
    tot = 0
    for r, n in sorted(by_res_series.items(), key=lambda kv: -by_res_steps[kv[0]]):
        tot += by_res_steps[r]
        print(f"  {r:<8} {n:>8} {by_res_steps[r]:>15,}", file=sys.stderr)
    print(f"  {'TOTAL':<8} {sum(by_res_series.values()):>8} {tot:>15,}", file=sys.stderr)
    print(f"\n~ upper-bound observations: {tot:,}  (~{tot*60/1e9:.1f} GB raw json, "
          f"~{tot*25/1e9:.1f} GB flattened csv)", file=sys.stderr)


if __name__ == "__main__":
    main()
