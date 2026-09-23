#!/usr/bin/env bash
#
# Replay disruptions/api/rail_calls_removed_today.sql over past days: U1's gate in
# docs/plans/2026-09-23-001-feat-removed-trains-plan.md, run before VARSEL_PLAN_REMOVED goes on
# anywhere a push can follow from it.
#
# The live file answers for TODAY only in one place: its `et_trains` CTE reads
# rail_calls_planned_today, which is rebuilt every hour and holds no past date. This script swaps
# that one CTE for ET's `last_recorded` by operating date and leaves every other line as it is, so
# the replay is the live rule and not a second copy of it. The `restricted` CTE still reads today's
# planned table -- which lines close boarding mid-journey is a property of how Vy sells the line,
# and last_recorded carries no activities to ask a past day with.
#
# Writes one scratch table per day, `disruptions.exp_removed_replay_<yyyymmdd>`, then prints per day
# what the table would have put in front of the registrations prd had that day, counted the way
# the strike reports count «Fjernet fra planen» (the registration's own first..last matched
# departure as its window; a train it was matched against that day does not count).
#
# Cost: ~0.3 GB per day for the build, plus one read of prd's varsel_decisions (~0.5 GB for two
# weeks). Measured 2026-09-23.
#
# Usage: data/queries/replay_removed_trains.sh FIRST_DAY LAST_DAY [project]   (default ent-kixavvik-dev)
set -euo pipefail

FIRST="$1"; LAST="$2"; PROJECT="${3:-ent-kixavvik-dev}"
API="$(cd "$(dirname "$0")/../../disruptions/api" && pwd)"

replay_sql() {
  python3 - "$API/rail_calls_removed_today.sql" "$1" <<'PY'
import sys

sql, day = open(sys.argv[1]).read(), sys.argv[2]
live_et = sql[sql.index("et_trains AS (") : sql.index("\n),", sql.index("et_trains AS (")) + 1]
replay_et = """et_trains AS (
  SELECT DISTINCT n AS trainno
  FROM `ent-data-sharing-ext-prd.realtime_siri_et.realtime_siri_et_last_recorded`,
    UNNEST([REGEXP_EXTRACT(datedServiceJourneyId, r'ServiceJourney:(\\d+)[-_]'),
            REGEXP_EXTRACT(datedServiceJourneyId, r'ServiceJourney:\\d+-(\\d+)_')]) AS n
  WHERE operatingDate = @d AND dataSource = 'VYG' AND n IS NOT NULL
"""
header = "CREATE OR REPLACE TABLE `disruptions.rail_calls_removed_today`"
assert sql.count(header) == 1 and sql.count(live_et) == 1
table = f"CREATE OR REPLACE TABLE `disruptions.exp_removed_replay_{day.replace('-', '')}`"
print(sql.replace(header, table).replace(live_et, replay_et))
PY
}

d="$FIRST"
while [[ "$d" < "$LAST" || "$d" == "$LAST" ]]; do
  printf 'build %s ... ' "$d"
  replay_sql "$d" | bq query --use_legacy_sql=false --project_id="$PROJECT" --quiet \
    --parameter=d:DATE:"$d" --parameter=enabled:BOOL:true >/dev/null
  echo ok
  d=$(python3 -c "import datetime,sys; print(datetime.date.fromisoformat(sys.argv[1]) + datetime.timedelta(days=1))" "$d")
done

bq query --use_legacy_sql=false --project_id="$PROJECT" --format=pretty --quiet \
  --parameter=first:DATE:"$FIRST" --parameter=last:DATE:"$LAST" <<'SQL'
WITH regs AS (
  SELECT call_date AS d, journey_digest, ANY_VALUE(from_stop) AS fs, ANY_VALUE(to_stop) AS ts,
         MIN(departure_time) AS w0, MAX(departure_time) AS w1,
         ARRAY_AGG(DISTINCT REGEXP_EXTRACT(jkey, r'ServiceJourney:(\d+)[-_]') IGNORE NULLS) AS seen
  FROM `ent-kixavvik-prd.disruptions.varsel_decisions`
  WHERE call_date BETWEEN @first AND @last
    AND DATE(decided_at) BETWEEN DATE_SUB(@first, INTERVAL 1 DAY) AND DATE_ADD(@last, INTERVAL 1 DAY)
    AND operator = 'VYG'
  GROUP BY 1, 2
),
removed AS (
  SELECT PARSE_DATE('%Y%m%d', _TABLE_SUFFIX) AS d, *
  FROM `disruptions.exp_removed_replay_*`
  WHERE PARSE_DATE('%Y%m%d', _TABLE_SUFFIX) BETWEEN @first AND @last
),
serves AS (
  SELECT r.d, r.journey_digest, a.lineRef, a.trainno
  FROM regs AS r
  JOIN removed AS a ON a.d = r.d AND a.stop_place = r.fs
  JOIN removed AS b ON b.d = a.d AND b.jkey = a.jkey AND b.stop_place = r.ts AND b.stop_order > a.stop_order
  WHERE a.aimed_dep BETWEEN r.w0 AND r.w1 AND a.trainno NOT IN UNNEST(r.seen)
)
SELECT
  x.d,
  (SELECT COUNT(DISTINCT jkey) FROM removed WHERE removed.d = x.d) AS table_trains,
  COUNT(DISTINCT s.trainno) AS trains_on_registrations,
  COUNT(DISTINCT s.journey_digest) AS registrations,
  COUNT(s.trainno) AS pairs
FROM (SELECT DISTINCT d FROM removed) AS x
LEFT JOIN serves AS s ON s.d = x.d
GROUP BY x.d
ORDER BY x.d
SQL
