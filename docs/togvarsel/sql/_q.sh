#!/bin/bash
# Usage: q.sh <name> [--dry] < sql   OR   q.sh <name> file.sql [--dry]
# Runs a BigQuery query with cost logging into $S/cost.log. Uses project ent-kixavvik-dev.
S=/private/tmp/claude-501/-Users-thomas-Projects-entur-kix-labs/a515943d-e031-494c-8d4e-1ca0a404ad25/scratchpad
NAME=$1; shift
DRY=""
SQLFILE=""
for a in "$@"; do case $a in --dry) DRY="--dry_run";; *) SQLFILE=$a;; esac; done
if [ -n "$SQLFILE" ]; then SQL=$(cat "$SQLFILE"); else SQL=$(cat); fi
echo "$SQL" > $S/sql/$NAME.sql
if [ -n "$DRY" ]; then
  bq query --project_id=ent-kixavvik-dev --use_legacy_sql=false --dry_run --format=prettyjson < $S/sql/$NAME.sql 2>&1 | python3 -c "
import sys,json
t=sys.stdin.read()
try:
  d=json.loads(t); b=int(d['statistics']['totalBytesProcessed'])
  print(f'DRY {sys.argv[1]}: {b/1e9:.2f} GB  ≈ \${b/1e12*6.25:.3f}')
except Exception as e:
  print(t)
" $NAME
  exit 0
fi
JOBID="kix_${NAME}_$(date +%s)"
bq query --project_id=ent-kixavvik-dev --use_legacy_sql=false --job_id=$JOBID --format=prettyjson --max_rows=1000 < $S/sql/$NAME.sql > $S/out/$NAME.json 2> $S/out/$NAME.err
RC=$?
STATS=$(bq show --format=prettyjson -j $JOBID 2>/dev/null | python3 -c "
import sys,json
d=json.load(sys.stdin)
st=d.get('statistics',{})
b=int(st.get('query',{}).get('totalBytesBilled') or st.get('totalBytesProcessed') or 0)
ms=int(st.get('endTime',0))-int(st.get('startTime',0))
print(f'{b/1e9:.3f} GB \${b/1e12*6.25:.4f} {ms/1000:.0f}s')
" 2>/dev/null)
echo "$(date +%H:%M:%S) $NAME rc=$RC $STATS" >> $S/cost.log
if [ $RC -ne 0 ]; then echo "ERROR ($NAME):"; cat $S/out/$NAME.err $S/out/$NAME.json | head -30; exit $RC; fi
echo "[$NAME] $STATS"
python3 - $S/out/$NAME.json <<'PY'
import sys,json
t=open(sys.argv[1]).read().strip()
if not t or not t.startswith('['): print(t[:500] or "(no output)"); sys.exit()
rows=json.loads(t)
if not rows: print("(no rows)"); sys.exit()
cols=list(rows[0].keys())
def fmt(v):
    if isinstance(v,dict): return json.dumps(v)
    if isinstance(v,list): return json.dumps(v)[:60]
    return str(v)
print(" | ".join(cols))
for r in rows[:200]:
    print(" | ".join(fmt(r.get(c)) for c in cols))
if len(rows)>200: print(f"... ({len(rows)} rows)")
PY
