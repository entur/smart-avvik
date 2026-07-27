# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy"]
# ///
"""Compute each frost weather station's distance to the nearest NSR quay.

Reads stations_all.json (frost sources) and quays_coords.csv (lat/lon of every
quay in ent-data-sharing-ext-prd.national_stop_registry.quays_last_version),
then writes stations_with_distance.csv (all stations, sorted by distance) and
prints the distribution so we can pick a sensible "close to a quay" threshold.

Usage:
  uv run filter_stations.py                 # report only
  uv run filter_stations.py --max-km 10     # also write near_stations.json
"""
import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).parent


def haversine_km(lat1, lon1, lat2, lon2):
    """Vectorised haversine. (lat1,lon1) scalars, (lat2,lon2) arrays."""
    r = 6371.0088
    p1 = np.radians(lat1)
    p2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlmb = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlmb / 2) ** 2
    return 2 * r * np.arcsin(np.sqrt(a))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-km", type=float, default=None,
                    help="If set, write near_stations.json with stations whose "
                         "nearest quay is within this many km.")
    args = ap.parse_args()

    with open(HERE / "stations_all.json") as fh:
        stations = json.load(fh)["data"]

    qlat, qlon = [], []
    with open(HERE / "quays_coords.csv") as f:
        for row in csv.DictReader(f):
            qlat.append(float(row["lat"]))
            qlon.append(float(row["lon"]))
    qlat = np.array(qlat)
    qlon = np.array(qlon)
    print(f"quays: {len(qlat):,}   stations: {len(stations):,}", file=sys.stderr)

    rows = []
    for s in stations:
        geo = s.get("geometry")
        if not geo or geo.get("@type") != "Point":
            continue
        lon, lat = geo["coordinates"][0], geo["coordinates"][1]
        d = haversine_km(lat, lon, qlat, qlon)
        rows.append({
            "id": s["id"],
            "name": s.get("name", ""),
            "lat": lat,
            "lon": lon,
            "county": s.get("county", ""),
            "nearest_quay_km": round(float(d.min()), 3),
        })

    rows.sort(key=lambda r: r["nearest_quay_km"])

    out = HERE / "stations_with_distance.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["id", "name", "lat", "lon", "county", "nearest_quay_km"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {out} ({len(rows)} stations with geometry)", file=sys.stderr)

    dists = np.array([r["nearest_quay_km"] for r in rows])
    print("\nNearest-quay distance distribution (km):", file=sys.stderr)
    for thr in [0.5, 1, 2, 5, 10, 15, 20, 30, 50, 100]:
        print(f"  <= {thr:>5} km : {int((dists <= thr).sum()):>5} stations", file=sys.stderr)
    print(f"  total          : {len(dists):>5} stations", file=sys.stderr)

    if args.max_km is not None:
        near = [r for r in rows if r["nearest_quay_km"] <= args.max_km]
        outj = HERE / "near_stations.json"
        with open(outj, "w") as fh:
            json.dump(near, fh, ensure_ascii=False, indent=2)
        print(f"\nwrote {outj}: {len(near)} stations within {args.max_km} km of a quay",
              file=sys.stderr)


if __name__ == "__main__":
    main()
