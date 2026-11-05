# frost.met.no weather observations

Weather observations from the Norwegian Meteorological Institute's [Frost API](https://frost.met.no),
for weather stations close to public-transport quays. Intended for joining weather
against SIRI disruption data in this project.

**License:** observation data is © Meteorologisk institutt, licensed
[CC BY 3.0 NO](https://creativecommons.org/licenses/by/3.0/no/). Attribute MET Norway.

## What was downloaded

- **Stations:** the 1,487 Norwegian `SensorSystem` stations whose nearest quay (in
  `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version`, 111,528 quays)
  is within **10 km**. This keeps all land stations near transit and drops offshore
  platforms / remote ocean & Svalbard sites. 1,448 of them actually have data in the
  window at the kept resolutions.
- **Window:** the last 2 years — `2024-06-16 / 2026-06-16` (the project's standard
  2-year lookback).
- **Resolutions:** hourly and coarser — `PT1H, PT6H, PT12H, P1D, P1M, P3M, P6M, P1Y`.
  Sub-hourly series (`PT1M, PT10M, PT30M`, …) were **excluded** — they were ~82% of the
  raw volume (~30 GB) and finer than needed for disruption-duration modelling.
- **Elements:** every element available per station at the kept resolutions (raw
  observations plus MET's derived/aggregate series, e.g. `mean(air_temperature P1D)`).

## Files

| File | What |
|---|---|
| `observations/<SNid>.csv.gz` | One gzipped, flattened CSV per station (the data) |
| `stations_all.json` | Raw Frost `/sources` response (1,592 Norwegian SensorSystems) |
| `stations_with_distance.csv` | Every station + its nearest-quay distance (km), sorted |
| `near_stations.json` | The 1,487 stations within 10 km of a quay (download set) |
| `quays_coords.csv` | lat/lon of all 111,528 quays (from BigQuery) |
| `timeseries_in_window.json` | availableTimeSeries manifest driving the download |
| `auth.txt` | Frost client_id / client_secret (not committed) |

### `observations/<SNid>.csv.gz` columns

`source_id, reference_time, element_id, time_offset, time_resolution, level_type,
level_value, level_unit, value, unit, quality_code, performance_category, exposure_category`

`reference_time` is UTC ISO-8601. Rows are unique on
`(reference_time, element_id, time_offset, time_resolution, level_type, level_value)`.
`quality_code` is MET's QC flag (0 = best). A station may report the same element at
multiple `level`s (e.g. air temperature at 2 m and 10 m).

`value` is **text**, not numeric: it is numeric for almost all elements, but a small
set of elements (`unit = 'Date'`, e.g. `time_of_maximum_*`) report a timestamp as their
value. Use `SAFE_CAST(value AS FLOAT64)` for numeric elements.

## In BigQuery

Loaded into **`ent-smartavvik-dev.disruptions.frost_weather`** (EU): 155,274,639 rows,
1,357 stations, 253 elements, ~16 GB. Partitioned by `DATE(reference_time)` (DAY) and
clustered by `source_id, element_id, time_resolution`, so filtering by station/element
and a time range is cheap. Column types match `frost_weather_schema.json` (note `value`
is STRING — see above).

Reload (from a GCS staging bucket co-located with the EU dataset):

```sh
gcloud storage buckets create gs://<staging-bucket> --location=EU --project=ent-smartavvik-dev
gcloud storage cp 'observations/*.csv.gz' gs://<staging-bucket>/observations/ --project=ent-smartavvik-dev
bq load --project_id=ent-smartavvik-dev --location=EU --replace \
  --source_format=CSV --skip_leading_rows=1 --max_bad_records=0 \
  --time_partitioning_field=reference_time --time_partitioning_type=DAY \
  --clustering_fields=source_id,element_id,time_resolution \
  disruptions.frost_weather 'gs://<staging-bucket>/observations/*.csv.gz' frost_weather_schema.json
```

`skip_leading_rows=1` drops the per-file header from each file in the wildcard load.
`scan_types.py` re-checks column types across all CSVs before a load.

## Reproduce / extend

Scripts use [uv](https://docs.astral.sh/uv/) with inline dependencies; run from this dir.

```sh
# 1. station list (Frost /sources) -> stations_all.json
curl -s -u "$CLIENT_ID:" \
  "https://frost.met.no/sources/v0.jsonld?country=Norge&types=SensorSystem" \
  -o stations_all.json

# 2. quay coords from BigQuery -> quays_coords.csv
bq query --use_legacy_sql=false --max_rows=200000 --format=csv \
  'SELECT location_latitude AS lat, location_longitude AS lon
   FROM `ent-data-sharing-ext-prd.national_stop_registry.quays_last_version`
   WHERE location_latitude IS NOT NULL AND location_longitude IS NOT NULL' \
  > quays_coords.csv

# 3. nearest-quay distance + select stations within 10 km -> near_stations.json
uv run filter_stations.py --max-km 10

# 4. availableTimeSeries manifest for the window -> timeseries_in_window.json
uv run fetch_timeseries.py

# 5. download observations -> observations/*.csv.gz  (resumable; skips finished stations)
uv run download_observations.py --workers 6
```

### Notes

- Frost caps each `/observations` request at **100,000 observations**. The downloader
  sizes each request from the element count × resolution, and bisects the time window
  (then the element list) if a request still exceeds the cap.
- HTTP 404/412 from `/observations` means "no data for this slice" and is treated as empty.
- To change the resolution set or distance threshold, edit `KEEP` in
  `download_observations.py` / pass `--max-km` to `filter_stations.py`, then re-run.
