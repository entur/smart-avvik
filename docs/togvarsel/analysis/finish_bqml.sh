#!/bin/bash
# After `gcloud auth login`: retrain the one boosted tree that hit the token expiry, evaluate all 24 BQML models, fill §14j + artifact, tally cost.
set -e
S=/private/tmp/claude-501/-Users-thomas-Projects-entur-kix-labs/a515943d-e031-494c-8d4e-1ca0a404ad25/scratchpad
D=/Users/thomas/Projects/entur/kix-avvik/docs/togvarsel
n=exp_togvarsel_bt_am5_f15_B
if ! bq show --project_id=ent-kixavvik-dev "ent-kixavvik-dev:disruptions.pred_$n" >/dev/null 2>&1; then
  t0=$(date +%s); bq query --project_id=ent-kixavvik-dev --use_legacy_sql=false --nouse_cache < $S/sql/bqml/$n.sql > $S/out/bqml_$n.log 2>&1
  echo "$(date +%H:%M:%S) RERUN2 $n rc=$? $(( $(date +%s) - t0 ))s err=$(grep -c Error $S/out/bqml_$n.log)" >> $S/out/bqml_run.log
fi
$S/q.sh bqml_eval $S/sql/bqml_eval.sql
python3 -c "import json,csv,sys; r=json.load(open('$S/out/bqml_eval.json')); w=csv.DictWriter(open('$D/analysis/results/bqml_eval.csv','w'), fieldnames=list(r[0].keys())); w.writeheader(); w.writerows(r)"
cd $S && uv run --quiet --with pandas python fill_bqml.py $S $D/02-findings-2026-08-27.md $S/togmelding.html
bq ls -j --project_id=ent-kixavvik-dev -a --max_results=400 --min_creation_time=$(( ($(date +%s) - 12*3600) * 1000 )) --format=prettyjson 2>/dev/null | python3 -c "
import json,sys
jobs=json.load(sys.stdin); tot=0; n=0; bt=0
for j in jobs:
    st=j.get('statistics',{}); q=st.get('query',{}); cfg=j.get('configuration',{}).get('query',{}).get('query','') or ''
    if 'exp_togvarsel' not in cfg: continue
    b=int(q.get('totalBytesBilled') or 0); tot+=b; n+=1
    if 'BOOSTED_TREE' in cfg: bt+=b
print(f'BQML jobs {n}: {tot/1e9:.1f} GB ≈ \${tot/1e12*6.25:.2f} (boosted trees ≈ \${bt/1e12*6.25:.2f})')"
echo FINISH_DONE
