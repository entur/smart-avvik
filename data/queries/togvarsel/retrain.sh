#!/usr/bin/env bash
#
# Monthly togvarsel retrain (§14k: "retrained monthly"): rebuild the day-level training chain
# from the batch sources, then re-tune and re-train the four production models. Run it around
# the turn of the month; it is safe to re-run (every step is CREATE OR REPLACE).
#
#   create_disruptions.sql   ../create_disruptions.sql — refresh disruptions.disruptions
#                            (2-year SIRI-SX scan, ~$1; the SX-event source for the chain)
#   00..09_*.sql             stations → calls → journeys → windows → days → sx → features →
#                            training (01 scans last_recorded VYG rail, ~$0.5; the rest read
#                            tiny tables)
#   10_train_models.sql      l2_reg grid + final LOGISTIC_REG fits (~36 model jobs, ~25 min,
#                            costs cents — LOGISTIC_REG, not boosted trees; see the file header)
#
# Total ≈ $1.5 and ~40 min. The serving app picks the new models up on its next togvarsel issue
# (ML.PREDICT names the models; no redeploy needed). After a retrain, eyeball the tuning table
# printed at the end: holdout AUCs should sit in the neighbourhood of §14a/§14e (0.7–0.9 —
# and remember §14f: a holdout that lands in winter reads lower than one in summer).
set -euo pipefail

PROJECT="${PROJECT:-ent-kixavvik-dev}"
DIR="$(dirname "$0")"

run() {
  local sql="$1"
  echo "== $sql =="
  local t0=$SECONDS
  bq query --nouse_legacy_sql --format=none --project_id="$PROJECT" < "$sql"
  echo "   done in $((SECONDS - t0)) s"
}

run "$DIR/../create_disruptions.sql"
for f in "$DIR"/0[0-9]_*.sql; do run "$f"; done
run "$DIR/10_train_models.sql"

bq query --nouse_legacy_sql --project_id="$PROJECT" --format=pretty '
SELECT model_name, l2_reg, ROUND(holdout_log_loss,4) log_loss, ROUND(holdout_auc,3) auc,
       n_train, n_tune, chosen
FROM `disruptions.togvarsel_tuning`
ORDER BY model_name, l2_reg'
