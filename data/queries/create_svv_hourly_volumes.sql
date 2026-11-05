-- Schema documentation for `ent-smartavvik-dev.disruptions.svv_hourly_volumes`
-- and `disruptions.svv_points` -- SVV (Statens vegvesen) hourly traffic volumes
-- for the curated E18 Lier -> Oslo corridor registration points, used by the
-- reisevalg-varsel feasibility analysis (road-congestion side of the
-- car-vs-train morning decision).
--
-- These tables are NOT created by running this file: the data comes from the
-- open Trafikkdata GraphQL API (https://trafikkdata-api.atlas.vegvesen.no/,
-- NLOD, no auth) via data/svv/download_volumes.py (stdlib-only, resumable,
-- ~176 pages/point at 100 hours/page), then `bq load`ed from NDJSON --
-- BigQuery cost: $0 (loads are free; the API is free open data).
-- The DDL below documents the exact schema and load commands used.
--
-- Grain of svv_hourly_volumes: one row per (point_id, hour, heading) -- the
-- byDirection rows are kept because summing directions destroys the rush-hour
-- signal (morning peak toward Oslo, afternoon peak away). total_volume is the
-- hour's all-direction total, repeated on each direction row. coverage_pct is
-- the API's own data-quality measure for the hour x direction; treat rows with
-- low coverage (or NULL volume) as missing, not as zero.
--
-- The API returns hours in Europe/Oslo local time; both hour_start_utc and the
-- naive Oslo-local hour_start_oslo are stored so DST days join cleanly with
-- rail_calls (which keys on Oslo-local call_date). Window matches rail_calls:
-- 2024-06-01 .. 2026-05-31 (Oslo local). Two points start later than the
-- window (data first published 2024-09; see svv_points / data/svv/points.json).
--
-- svv_points: the curated point registry from data/svv/points.json, in
-- corridor order (ascending EV18 road reference, Lier -> Oslo).
-- toward_oslo_heading is the byDirection.heading string that means "driving
-- toward Oslo" AT THAT POINT -- it varies per point (Liertoppen / Asker /
-- Oslo / Festning); the toward_oslo flag on the volume rows is derived from it
-- at download time.
--
-- Load commands (from repo root; volumes/*.ndjson.gz produced by the script):
--
--   first=1
--   for f in data/svv/volumes/*.ndjson.gz; do
--     if [ $first = 1 ]; then repl=--replace; first=0; else repl=--noreplace; fi
--     bq load --source_format=NEWLINE_DELIMITED_JSON $repl \
--       --time_partitioning_field=date_oslo --clustering_fields=point_id \
--       ent-smartavvik-dev:disruptions.svv_hourly_volumes "$f" \
--       data/svv/svv_hourly_volumes_schema.json
--   done
--
--   python3 -c "import json; [print(json.dumps({k: p.get(k) for k in \
--     ('id','name','road_ref','lat','lon','toward_oslo_heading','first_data','note')}, \
--     ensure_ascii=False)) for p in json.load(open('data/svv/points.json'))['points']]" \
--     > data/svv/points.ndjson
--   bq load --source_format=NEWLINE_DELIMITED_JSON --replace \
--     ent-smartavvik-dev:disruptions.svv_points data/svv/points.ndjson \
--     data/svv/svv_points_schema.json

CREATE TABLE IF NOT EXISTS `ent-smartavvik-dev.disruptions.svv_hourly_volumes` (
  point_id        STRING    NOT NULL OPTIONS(description="SVV traffic registration point id, e.g. 33438V444232; joins svv_points.id"),
  hour_start_utc  TIMESTAMP NOT NULL OPTIONS(description="Start of the measured hour, UTC"),
  hour_start_oslo DATETIME  NOT NULL OPTIONS(description="Start of the measured hour, naive Europe/Oslo local time (as published by the API)"),
  date_oslo       DATE      NOT NULL OPTIONS(description="Europe/Oslo date of hour_start; partition key; matches rail_calls.call_date semantics"),
  heading         STRING    NOT NULL OPTIONS(description="byDirection heading (travel direction), e.g. 'Oslo'/'Drammen'/'Asker'/'Festning' -- per-point vocabulary"),
  toward_oslo     BOOL      NOT NULL OPTIONS(description="TRUE when heading equals the point's toward_oslo_heading (from svv_points)"),
  volume          INT64              OPTIONS(description="Vehicles in this direction this hour; NULL = not measured (do not treat as 0)"),
  coverage_pct    FLOAT64            OPTIONS(description="API coverage.percentage for this hour x direction; filter low values as missing data"),
  total_volume    INT64              OPTIONS(description="All-direction total for the hour, repeated on each direction row")
)
PARTITION BY date_oslo
CLUSTER BY point_id;

CREATE TABLE IF NOT EXISTS `ent-smartavvik-dev.disruptions.svv_points` (
  id                  STRING  NOT NULL OPTIONS(description="SVV traffic registration point id"),
  name                STRING  NOT NULL OPTIONS(description="SVV point name, e.g. 'E18 Høvik'"),
  road_ref            STRING  NOT NULL OPTIONS(description="Road reference short form, e.g. 'EV18 S53D1 m3630'; ascending = Lier -> Oslo corridor order"),
  lat                 FLOAT64 NOT NULL,
  lon                 FLOAT64 NOT NULL,
  toward_oslo_heading STRING  NOT NULL OPTIONS(description="byDirection.heading string meaning 'driving toward Oslo' at this point"),
  first_data          STRING           OPTIONS(description="API dataTimeSpan.firstData; two points start later than the 2024-06 window"),
  note                STRING           OPTIONS(description="Caveats: one-way carriageways, the Bjørvika east-inflow proxy, late data start")
);
