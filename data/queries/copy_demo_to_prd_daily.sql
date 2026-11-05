-- Daily prd refresh of the /demo tables.
--
-- Registered as the scheduled query "demo-copy-daily" in ent-smartavvik-prd
-- (every day 06:00 UTC, runs as application@ent-smartavvik-prd), one hour after
-- dev's "demo-recent-daily" (05:00 UTC, extend_demo_recent_daily.sql) has
-- extended the dev demo tables with yesterday's calls for all operators.
--
-- The prd /demo-api serves straight from these copies (30-min in-process meta
-- cache, no redeploy needed), so this keeps the prd demo at most a day behind
-- without touching the rest of the promotion flow (copy_runtime_to_prd.sh
-- stays the path for models/calibration after retrains).
--
-- CREATE TABLE ... COPY runs as a same-region copy job: free, preserves
-- partitioning/clustering. Access: the prd SA holds dataset-level
-- roles/bigquery.dataViewer on ent-smartavvik-dev:disruptions (granted
-- 2026-07-08); the unqualified targets resolve to ent-smartavvik-prd.
--
-- Re-register after editing:
--   bq update --transfer_config --params="$(python3 -c 'import json;print(json.dumps({"query":open("data/queries/copy_demo_to_prd_daily.sql").read()}))')" <config resource name>

CREATE OR REPLACE TABLE disruptions.demo_calls
COPY `ent-smartavvik-dev.disruptions.demo_calls`;

CREATE OR REPLACE TABLE disruptions.demo_predictions
COPY `ent-smartavvik-dev.disruptions.demo_predictions`;

CREATE OR REPLACE TABLE disruptions.demo_stops
COPY `ent-smartavvik-dev.disruptions.demo_stops`;

CREATE OR REPLACE TABLE disruptions.demo_highlights
COPY `ent-smartavvik-dev.disruptions.demo_highlights`;
