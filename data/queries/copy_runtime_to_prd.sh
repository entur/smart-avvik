#!/usr/bin/env bash
#
# Copy the disruptions API's runtime BigQuery objects dev -> prd.
#
# The API's SQL uses unqualified `disruptions.*` refs resolved via
# GOOGLE_CLOUD_PROJECT, so prd serves from ent-smartavvik-prd:disruptions. All
# models are trained and all lookup tables are built in dev; prd never trains
# or scans SIRI history itself — it only receives copies. Both datasets are in
# the EU multi-region, so every copy job below is free (0 bytes billed).
#
# Re-run after every retrain / calibration refresh in dev to promote the new
# models to prd (also re-run after rebuilding the demo_* tables or
# category_line_hour_impact). Safe to re-run any time: -f overwrites the prd
# copy in place.
#
# What is copied (the serving runtime set, derived from disruptions/api/*.sql
# + main.py — see docs in the repo memory / PR description):
#   - rail family (NSB+GOA prod models): 2 duration models, 2 delay regressors,
#     9 classifier heads, 45 platt calibrators (9 heads x base/lead60/120/240/plan)
#   - bus family (pooled_all): 2 delay regressors + 9 classifier heads
#     (bus platt calibration is the pooled_all_platt_weights TABLE, not models)
#   - lookup tables: category taxonomy/classification, alert-impact profiles,
#     duration + delay-p90 calibration, pooled platt weights, /demo-api tables
#
# NOT copied: the *_today tables (rail_calls_today, *_planned_today,
# disruptions_*_initial_today, predictions_today, call_predictions_*_today) —
# the poller CREATE OR REPLACEs those in prd within one refresh cycle.
#
# ALSO NOT copied, for the opposite reason — varsel_decisions and
# varsel_decision_outcomes (the Avviksvarsel alert-decision log and its nightly
# labels), plus the varsel_decision_groups view over them. Each environment
# records its OWN decisions, and `bq cp -f` overwrites: listing them here would
# replace prd's real alerting history with dev's. The app creates the table and
# view itself on startup (disruptions/api/create_varsel_decisions.sql), and
# data/queries/label_varsel_decisions.sh labels each environment separately.
# Same reasoning as svv_baselines, and as togvarsel_issued (each environment
# logs its own issued forecasts; togvarsel.py creates the table on first use).
set -euo pipefail

SRC_PROJECT="ent-smartavvik-dev"
DST_PROJECT="ent-smartavvik-prd"
DATASET="disruptions"

MODELS=(
  # initial-duration models (predict.sql / disruptions_as_of.sql)
  dur_init_reopen impact_remaining_noet
  # togmelding forecast models (togvarsel.py / togvarsel_features_today.sql); retrained by
  # data/queries/togvarsel/retrain.sh in dev, promoted here
  togvarsel_m_am0630 togvarsel_m_am0700 togvarsel_m_pm1200 togvarsel_m_pm1400
)
# rail family ("" suffix) + bus/pooled family (_pooled_all suffix)
for fam in "" "_pooled_all"; do
  MODELS+=("arr_delay_reg$fam" "dep_delay_reg$fam" "call_cancel_clf$fam")
  for side in arr dep; do
    for thr in 5 10 20 40; do MODELS+=("${side}_ge${thr}_clf$fam"); done
  done
done
# rail platt calibrators: 9 heads x {base, lead60, lead120, lead240, plan}, x TWO POPULATIONS
# since 2026-08-20. The _nosx half calibrates calls no SX message named (see the no-SX branch in
# predict_calls.sql).
#
# PROMOTING THIS TO PRD IS A THREE-STEP SEQUENCE, and both of the obvious two-step orders break it.
# The dependency runs both ways: the retrained HEADS require `has_situation` in their input, which
# only an image carrying PR #60 supplies; and the new QUERY joins `nosx_platt_weights`, which only
# this script creates. So copy-then-deploy leaves prd's old image feeding models a column it does
# not produce, and deploy-then-copy leaves prd's new query joining a table that does not exist.
# Either way the per-call scoring query dies inside main.py's try/except -- the /disruptions feed
# survives, but /journey and every varsel push serve stale or empty predictions until the other half
# lands. The window is avoidable, and this is how:
#
#   1. Create an EMPTY `nosx_platt_weights` in prd first. The no-SX rows then lose their INNER JOIN
#      and drop out, which is exactly the pre-2026-08-20 behaviour -- no alerts missed that were
#      being sent before, and the flagged path untouched.
#   2. Deploy the image. has_situation is now supplied (prd's old heads ignore an input column they
#      do not know), and the query's no-SX branch resolves against the empty table.
#   3. Run this script. New heads land into a serving path that already feeds them has_situation,
#      the 45 _nosx models arrive, and the real weights table replaces the empty one -- at which
#      point no-SX alerting switches on.
#
# Each step is independently safe, which is the property the two-step orders lack.
for head in cancel arr_ge5 arr_ge10 arr_ge20 arr_ge40 dep_ge5 dep_ge10 dep_ge20 dep_ge40; do
  for variant in "" "_lead60" "_lead120" "_lead240" "_plan"; do
    MODELS+=("${head}_platt${variant}")
    MODELS+=("${head}_platt_nosx${variant}")
  done
done

TABLES=(
  categories               # AI.CLASSIFY cache; INSERT INTO'd at runtime (seed copy)
  category_taxonomy
  category_line_hour_impact
  duration_quantiles
  delay_p90_calib
  delay_p90_calib_pooled_all
  pooled_all_platt_weights
  nosx_platt_weights       # flattened no-SX platt coefficients; predict_calls.sql joins it directly
  demo_stops demo_highlights demo_calls demo_predictions
  togvarsel_oslo_stations   # the togmelding geography; togvarsel_features_today.sql joins it
  togvarsel_calendar        # the togmelding holiday/low-season calendar (00_calendar.sql)
)

# Ensure the destination dataset exists (EU multi-region, same as dev, so
# copy jobs stay same-region and free).
if ! bq show --format=none "$DST_PROJECT:$DATASET" 2>/dev/null; then
  echo "Creating dataset $DST_PROJECT:$DATASET (location EU)"
  bq mk --dataset --location=EU \
    --description "disruptions API runtime objects, copied from $SRC_PROJECT by data/queries/copy_runtime_to_prd.sh" \
    "$DST_PROJECT:$DATASET"
fi

echo "Copying ${#MODELS[@]} models and ${#TABLES[@]} tables $SRC_PROJECT -> $DST_PROJECT"
fail=0
for obj in "${MODELS[@]}" "${TABLES[@]}"; do
  if bq cp -f "$SRC_PROJECT:$DATASET.$obj" "$DST_PROJECT:$DATASET.$obj" >/dev/null; then
    echo "  ok  $obj"
  else
    echo "  FAILED  $obj" >&2
    fail=1
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "Some copies failed — re-run to retry (copies are idempotent)." >&2
  exit 1
fi
echo "Done: $((${#MODELS[@]} + ${#TABLES[@]})) objects in $DST_PROJECT:$DATASET"
