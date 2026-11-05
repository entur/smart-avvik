#!/usr/bin/env bash
#
# Monthly togvarsel retrain (§14k: "retrained monthly"): rebuild the day-level training chain
# from the batch sources, then re-tune and re-train the four production models. Run it around
# the turn of the month; it is safe to re-run (every step is CREATE OR REPLACE).
#
#   create_disruptions.sql   ../create_disruptions.sql — refresh disruptions.disruptions
#                            (2-year SIRI-SX scan, ~$1; the SX-event source for the chain)
#   00..09_*.sql             calendar + stations → calls → journeys → windows → plan cut → days →
#                            sx → features → training (01 scans last_recorded VYG rail, ~$0.5;
#                            03b scans ~21 GB of GTFS, ~$0.15; the rest read tiny tables)
#   10_train_models.sql      l2_reg grid + final LOGISTIC_REG fits into togvarsel_m_cand_*
#                            (~36 model jobs, ~25 min, costs cents)
#   promotion                bq cp the four candidates onto the live togvarsel_m_* names — only
#                            reached when ALL of the above succeeded, so a failed retrain leaves
#                            the live model family untouched instead of half-replaced
#
# Total ≈ $1.7 and ~45 min. SERVING MUST CARRY THE cut_* COLUMNS BEFORE THE MODELS ARE PROMOTED
# (2026-09-21): ML.PREDICT fails the whole scoring job on a model whose contract names a column
# its input lacks, so a deploy of togvarsel_features_today.sql precedes the first retrain that
# includes 10_train_models.sql's cut features. Run this script with the promotion loop commented
# out to train candidates only. The serving app picks the new models up on its next togvarsel issue
# (ML.PREDICT names the models; no redeploy needed). After a retrain, eyeball the tuning table
# printed at the end: holdout AUCs should sit in the neighbourhood of §14a/§14e (0.7–0.9 —
# and remember §14f: a holdout that lands in winter reads lower than one in summer).
set -euo pipefail

PROJECT="${PROJECT:-ent-smartavvik-dev}"
DIR="$(dirname "$0")"

run() {
  local sql="$1"
  echo "== $sql =="
  local t0=$SECONDS
  bq query --nouse_legacy_sql --format=none --project_id="$PROJECT" < "$sql"
  echo "   done in $((SECONDS - t0)) s"
}

run "$DIR/../create_disruptions.sql"
# 0[0-9]*, not 0[0-9]_*: the latter skips 03b_plan.sql, and 04_days.sql then reads every day after
# the last manual 03b run as zero timetable cuts.
for f in "$DIR"/0[0-9]*.sql; do run "$f"; done

# Refuse to retrain on a calendar about to expire — extend 00_calendar.sql first (§13).
bq query --nouse_legacy_sql --format=none --project_id="$PROJECT" '
SELECT IF(MAX(d) < DATE_ADD(CURRENT_DATE("Europe/Oslo"), INTERVAL 60 DAY),
          ERROR(FORMAT("togvarsel_calendar slutter %t — utvid data/queries/togvarsel/00_calendar.sql før retrening", MAX(d))),
          TRUE)
FROM `disruptions.togvarsel_calendar`'

run "$DIR/10_train_models.sql"

# All four candidates trained: promote them to the live names. Model copies are quick metadata
# jobs, so the window where the family is mixed shrinks from ~25 min of training to seconds
# (and a failed copy is re-runnable: just re-run this loop).
for m in am0630 am0700 pm1200 pm1400; do
  bq cp -f "$PROJECT:disruptions.togvarsel_m_cand_$m" "$PROJECT:disruptions.togvarsel_m_$m" >/dev/null
  echo "== promoted togvarsel_m_$m =="
done
for m in am0630 am0700 pm1200 pm1400; do
  bq rm -f -m "$PROJECT:disruptions.togvarsel_m_cand_$m"
done

bq query --nouse_legacy_sql --project_id="$PROJECT" --format=pretty '
SELECT model_name, l2_reg, ROUND(holdout_log_loss,4) log_loss, ROUND(holdout_auc,3) auc,
       n_train, n_tune, chosen
FROM `disruptions.togvarsel_tuning`
ORDER BY model_name, l2_reg'
