#!/usr/bin/env bash
# Pull everything the strike-week reports are built from, then build them.
#   docs/reports/streik-2026/generator/run.sh [DATA_DIR]
# Needs bq and gcloud under an ADC that reads ent-kixavvik-prd, ent-anshar-prd,
# ent-data-sharing-ext-prd and the prd cluster logs (ent-kub-prd). About 3 GB scanned, a few cents.
# The pulled CSVs carry per-registration journey digests, so DATA_DIR stays out of git.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DATA="${1:-$HERE/data}"
mkdir -p "$DATA"
q() {  # q <sql file> <out csv> [sed expression]
  sed "${3:-}" "$HERE/sql/$1" | bq query --use_legacy_sql=false --format=csv --max_rows=50000 > "$DATA/$2"
}
DAYS="2026-09-18 2026-09-19 2026-09-20 2026-09-21 2026-09-22 2026-09-23"
for d in $DAYS; do
  p0=$(date -j -v-2d -f %Y-%m-%d "$d" +%Y-%m-%d 2>/dev/null || date -d "$d -2 day" +%Y-%m-%d)
  q alerts.sql "alerts_$d.csv" "s/@DAY/$d/g" &
  q strike_sx.sql "strike_sx_$d.csv" "s/@P0/$p0/; s/@P1/$d/; s/@DATESUF/${d:2:2}-${d:5:2}-${d:8:2}/" &
  q cap_spend.sql "cap_spend_$d.csv" "s/@DAY/$d/g" &
done
for f in days gaps removed plan_cuts lines_plan sx_situations; do q "$f.sql" "$f.csv" & done
gcloud logging read 'resource.labels.namespace_name="kix-avvik" AND jsonPayload.event="push_capped" AND timestamp>="2026-09-17T22:00:00Z"' \
  --project=ent-kub-prd --format='value(timestamp,jsonPayload.withheld,jsonPayload.registrations)' --limit=20000 \
  > "$DATA/push_capped.tsv" &
wait
TZ=Europe/Oslo date '+%d.%m kl. %H:%M' > "$DATA/pulled.txt"
python3 "$HERE/gen.py" "$DATA"
