# Retraining and promoting the models

**Written:** 2026-11-05. **Companion to:** [`RUNBOOK.md`](RUNBOOK.md).

All models are BigQuery ML (BQML) models in `ent-kixavvik-dev:disruptions`. They are trained in dev
and copied to prd. prd never trains. Retraining is manual: nothing is scheduled, and no test checks
that a retrain left serving consistent.

Most classifiers have a **Platt layer**: a small logistic regression fitted afterwards that turns
the model's raw score into a probability you can put a threshold on (0.65 for an alert).

**The rule that matters most:** serving reads some calibration from *tables*, not from models. A
table is a snapshot of the models it was built from. Retrain a model and skip the table rebuild, and
prd serves new models with old calibration. Nothing errors.

## What to retrain, and how often

| Family | Serves | Cadence | Procedure |
|---|---|---|---|
| Rushvarsel (`togvarsel_m_*`, 4 models) | `/togmelding` | Monthly | [Rushvarsel](#rushvarsel), one script |
| Rail call heads (11 models) + Platt calibration (90 models, 1 table) | `/varsel`, webhooks, journey-alerts | No fixed cadence; when the base rate drifts | [Rail heads](#rail-heads) |
| Bus and Go-Ahead (`*_pooled_all`) | Same, for buses | Frozen at June 2026 | Not routine, see [Frozen families](#frozen-families) |
| Duration (`dur_init_reopen`, `impact_remaining_noet`) | `/disruptions` | Frozen | Not routine |
| Trip miss constants (`trips._MISS_*`) | Multi-leg journeys | After an arrival-head retrain | Only matters once multi-leg is on in prd |

The LLM writers (Vertex Gemini) are not trained by us.

## Before you start

- `gcloud auth login`, and check that `bq ls ent-kixavvik-dev:disruptions` works. An "Access
  Denied" here means an expired login, not missing access.
- Every command below runs against **dev**. Most training files name `ent-kixavvik-dev` in full.
- Submit SQL from a file, never inline: `bq query --nouse_legacy_sql --project_id=ent-kixavvik-dev <
  file.sql`.
- Costs are on-demand BigQuery. The rail heads alone bill about $4 (step 2); the other steps state
  their cost in their file headers where it was measured. Dry-run a step you have no figure for:
  `bq query --dry_run < file.sql`, and do not chain the dry run and the real run with `&&`.

## Rushvarsel

```sh
data/queries/togvarsel/retrain.sh
```

About 45 minutes and $1.7. The script refreshes the training data, trains four candidate models,
and only replaces the live `togvarsel_m_*` models in dev when every step succeeded. dev's poller
picks them up on its next forecast, with no deploy.

- Check the tuning table the script prints last: holdout AUC should be between 0.7 and 0.9. A
  holdout in winter reads lower than one in summer.
- The script refuses to run when `togvarsel_calendar` ends less than 60 days ahead. The calendar
  ends 2027-05-17, so from about 2027-03-18 you must extend `00_calendar.sql` first. Serving fails
  after that date.
- To reach prd: [Promote to prd](#promote-to-prd).

## Rail heads

Run each step in order, in dev. Each step's file header explains the step in more detail.

0. **Refresh the source data.** `update_rail_calls.sql` adds the last two days of SIRI-ET to
   `rail_calls`. For a longer gap, widen the literal `INTERVAL 2 DAY` in the file. Never replace it
   with a variable: that turns a cheap read into a full scan. A full rebuild costs about $63.
1. **Move the training window.** `create_training_calls.sql` has a hand-set end date (lines 108-109,
   154 and 243, currently `2026-10-11`). Set it to two days before the end of `rail_calls`, so
   every call has its 12-hour outcome window. The train/eval split at `2026-04-01` (lines 107, 192,
   238) is a separate decision: the calibration below is fitted on the eval side.
   Then run `create_training_calls.sql`.
2. **Train the heads.** `train_call_models.sql` holds 11 `CREATE OR REPLACE MODEL` statements.
   **Run each one as its own job**: `--maximum_bytes_billed` counts the whole script, and later
   statements get killed. The cancellation classifier takes about 81 minutes and $1.78. Each of the
   other ten takes about 15 minutes.
3. **Refit the Platt calibration**: `create_cancel_calibration.sql`, then
   `create_exceedance_calibration.sql`.
4. **Refit the lead-time calibration**: `leadtime_eval.sql`, then `create_leadtime_calibration.sql`.
5. **Rebuild the no-SX snapshot**: `create_nosx_platt_weights.sql`. Free. Run the committed SQL; the
   generator `gen_nosx_platt_weights.py` is only re-run when the generator itself changes.
6. **If the delay regressors were retrained**: `create_delay_p90_calib.sql`. **Check before
   running.** The file reads `training_calls` with no `has_situation` filter, so a run today
   builds the table from both populations. It was written before the table had two. Decide
   whether that is intended, and add the filter if it is not.
7. **Rebuild the demo tables**: `create_demo_predictions.sql`, as one job, about $1. It reads the
   lead-time and Platt outputs, so it comes after steps 4 and 5. **Check before running.** It
   reads `leadtime_scored`, which has held both populations since 2026-08-19, with no
   `has_situation` filter, and applies the flagged calibration to every row.

Between step 2 and step 5, dev serves new heads with old calibration. Keep that window short.

**Do not remove `AND has_situation` from any calibration fit.** `training_calls` holds two
populations: calls an operator message named, and 5.9 M calls no message named. A fit without the
filter centres alerting on a base rate 47 times lower. Nothing errors; the alerts just stop
firing. See `CLAUDE.md`, "Conventions".

**Change a model's input columns only after serving has them.** `ML.PREDICT` fails the whole
scoring job when a model expects a column its input lacks. Deploy the serving SQL
(`disruptions/api/predict_calls.sql`) first, then train.

**Check the result.** There is no committed evaluation query for the rail heads. The reference
figures are in `train_call_models.sql`'s header: flagged holdout AUC 0.960, precision 0.941 at the
0.65 alert bar. `eval/impact_eval.py` (marimo) compares predictions with outcomes. After step 5,
read the slopes `gen_nosx_platt_weights.py` warns about.

## Promote to prd

```sh
data/queries/copy_runtime_to_prd.sh
```

Copies 118 models and 14 tables from dev to prd with `bq cp -f`. Free (both datasets are in the EU
multi-region) and safe to re-run. prd's poller uses the new objects on its next cycle; no deploy.

- **It copies every family at once.** A half-finished retrain of one family in dev goes to prd
  with it. Promote only when dev is in a consistent state.
- It copies one object at a time, so prd mixes old and new models for a few minutes.
- It does **not** copy the poller-built `*_today` tables, the decision log, `svv_baselines` or
  `togvarsel_issued`. Each environment keeps its own.
- It overwrites prd's `categories`, which the app appends to at runtime.
- The demo tables are also copied every morning by the prd scheduled query `demo-copy-daily`
  (04:30 UTC). dev's `demo-recent-daily` re-scores the last ~25 days with the current models, but
  nothing rebuilds the fixed April-June window except step 7.

## Frozen families

These were trained once and are not part of a routine retrain. Re-running their files unchanged
reproduces the same models.

- **Bus and Go-Ahead (`*_pooled_all`).** The training windows end 2026-06-19/20
  (`create_training_calls_pooled_all.sql`, `create_bus_calls.sql`, `create_goa_calls.sql`). To
  retrain, move those literals, then run in order: `create_training_calls_pooled_all.sql`,
  `train_call_models_pooled_all.sql`, `create_pooled_all_calibration.sql`,
  `leadtime_eval_pooled_all.sql`, `create_leadtime_calibration_pooled_all.sql`,
  `create_pooled_all_platt_weights.sql`, `create_delay_p90_calib_pooled_all.sql`. The table
  `pooled_all_platt_weights` is what serving reads, so the second-to-last step is not optional.
  Evaluate with `eval_pooled_all_models.sql`.
- **Duration.** `train_dual_models.sql` and `train_impact_remaining.sql`, with
  `create_quantiles.sql` for `duration_quantiles`.
- **`trips._MISS_*`** (`disruptions/api/trips.py`). Refit after the arrival heads change, with
  `data/queries/transfer_miss_backtest.py` (command in its header), then copy the printed constants
  into `trips.py` and move the pins in `test_trips.py`. Its input table `exp_etcmp6_fair_scored`
  is not built by any committed code. This only affects prd once `TRIP_PLANS_ENABLED` is on there;
  it is on in dev only.

## Known problems

- `copy_runtime_to_prd.sh` copies the 45 `*_nosx` Platt models, which prd never reads (serving
  reads `nosx_platt_weights`). Harmless. Its header still says 45 calibrators; there are 90.
- `togvarsel/retrain.sh` skipped `03b_plan.sql` from 2026-09-21, so models it retrained saw no
  timetable cuts after the last manual run of that file. Fixed by PR 386. Until that PR is merged,
  run `03b_plan.sql` by hand before the script. The first retrain after the merge is the first one
  with fresh cuts, so its holdout AUCs may move.
