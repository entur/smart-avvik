"""Generate one BigQuery ML rolling-origin script per (variant, slot, area) for the rush-area
experiment, and a run list. Each script: for every month M in Oct 2025 .. Sep 2026, train a
LOGISTIC_REG (production recipe: log1p TRANSFORM on count columns, production l2_reg) on the
eligible rows before M and score M, appending (variant, mname, area, fold, d, label, p) to
disruptions.exp_rush_area_pred. Scripts are independent so they can run concurrently.

Variants (feature set / label / rows):
  base  production lists, area-wide label, area-wide eligibility   -> today's forecast
  L     production lists, the AREA's label, the area's eligibility -> "is it the label?"
  S     production lists + the area's own state (_c), area label   -> the corridor model
  SC    S + the area's own category counters (AM slots only)
  P     pooled over the three directions, S columns + area dummies, one model for all areas
Second wave (argv[2] == "wave2"), after P matched B everywhere in the first:
  P4     P pooled over all four areas (central included, its own dummy)
  Pl2_x  P with l2_reg x in {1, 10, 30} instead of the production value (3x the rows moves the
         effective penalty)
  PMplus P for the PM issues + the morning rush as realised (am_t10 area-wide and am_t10_known_c
         for the area), the 04-presisjon §5 lead
Sixth wave (argv[2] == "wave6"): the pooled area model on corrected area labels, with the area cut as a feature.
Fifth wave (argv[2] == "wave5"): F4a/F4b (+C) = cut labels and the cut as a feature (04-cuts); BC = old label + cut feature.
Fourth wave (argv[2] == "wave4"): F2/F3 = base recipe on corrected labels (see 02-removed-journeys).
Third wave (argv[2] == "wave3"): PT = BOOSTED_TREE_CLASSIFIER on P's rows and columns (the study's
tuned tree configuration), because the tree verdict was made on a third of these rows.
"""
import sys, pathlib
TABLE = "disruptions.exp_rush_area_training"
PRED = "disruptions.exp_rush_area_pred"
MONTHS = "GENERATE_DATE_ARRAY('2025-10-01', '2026-09-01', INTERVAL 1 MONTH)"
CAL = ['dow_2','dow_3','dow_4','dow_5','dow_6','low_season']
CATS = ['drift_ruteendring','drift_kapasitet','materiell_teknisk','infrastruktur_signal','infrastruktur_spor','infrastruktur_strom','infrastruktur_sporveksel','drift_personale','ytre_tredjepart','drift_planlagt','infrastruktur_stasjon','vaer','other']
def cat(s, suf=""): return [f'cat_{k}_{s}{suf}' for k in CATS] + [f'catj_{k}_am_{s}{suf}' for k in ['infra','mat','drift']]
G = {  # production lists, 10_train_models.sql verbatim
 "am0630": ['et_t5_0630','et_cx_0630','et_cxj_0630','et_md_0630','prev_eve_t5','prev_eve_cancel','prev_pm_t5','sx_inn_0630','sx_infra_0630','sx_amj_0630','sx_amj_inn_0630','sx_mt_0630','sx_big_0630','sx_n_0630','et_t10_0630','et_max_0630','et_t5_last_0630','et_cxr_0630','sx_planned_0630','sx_amj_rest_0630','sx_delay_0630','sx_fv_0630','sx_long_0630','sx_pers_0630','sx_vedl_0630','sx_preveve','sx_preveve_inn','night_t5'] + CAL + cat('0630'),
 "am0700": ['et_t5_0700','et_t10_0700','et_cx_0700','et_cxj_0700','et_md_0700','et_max_0700','et_t5_last_0700','prev_eve_t5','prev_eve_cancel','prev_pm_t5','sx_inn_0700','sx_big_0700','sx_n_0700','sx_amj_0700','sx_amj_inn_0700','sx_amj_rest_0700','sx_planned_0700'] + CAL + cat('0700'),
 "pm1200": ['et_t5_1200','et_cx_1200','et_t5_last_1200','et_t5_0630','prev_eve_t5','prev_pm_t5','sx_inn_1200','sx_big_1200','sx_n_1200','sx_infra_1200','sx_pmj_1200','sx_pmj_inn_1200','sx_planned_1200'] + CAL,
 "pm1400": ['et_t5_1400','et_cx_1400','et_t5_last_1400','et_t5_1200','et_t5_0630','prev_eve_t5','prev_pm_t5','sx_inn_1400','sx_big_1400','sx_n_1400','sx_pmj_1400','sx_pmj_inn_1400','sx_planned_1400'] + CAL,
}
A = {  # the area's own state, same shapes with the _c suffix
 "am0630": ['et_t5_0630_c','et_t10_0630_c','et_cx_0630_c','et_cxj_0630_c','et_md_0630_c','et_max_0630_c','et_t5_last_0630_c','et_cxr_0630_c','prev_eve_t5_c','prev_eve_cancel_c','prev_pm_t5_c','night_t5_c','sx_n_0630_c','sx_inn_0630_c','sx_big_0630_c','sx_infra_0630_c','sx_planned_0630_c','sx_amj_0630_c','sx_amj_inn_0630_c','sx_amj_rest_0630_c','sx_preveve_c','sx_preveve_inn_c'],
 "am0700": ['et_t5_0700_c','et_t10_0700_c','et_cx_0700_c','et_cxj_0700_c','et_md_0700_c','et_max_0700_c','et_t5_last_0700_c','prev_eve_t5_c','prev_eve_cancel_c','prev_pm_t5_c','sx_n_0700_c','sx_inn_0700_c','sx_big_0700_c','sx_planned_0700_c','sx_amj_0700_c','sx_amj_inn_0700_c','sx_amj_rest_0700_c'],
 "pm1200": ['et_t5_1200_c','et_cx_1200_c','et_t5_last_1200_c','et_t5_0630_c','prev_eve_t5_c','prev_pm_t5_c','sx_n_1200_c','sx_inn_1200_c','sx_big_1200_c','sx_infra_1200_c','sx_planned_1200_c','sx_pmj_1200_c','sx_pmj_inn_1200_c'],
 "pm1400": ['et_t5_1400_c','et_cx_1400_c','et_t5_last_1400_c','et_t5_1200_c','et_t5_0630_c','prev_eve_t5_c','prev_pm_t5_c','sx_n_1400_c','sx_inn_1400_c','sx_big_1400_c','sx_planned_1400_c','sx_pmj_1400_c','sx_pmj_inn_1400_c'],
}
LABEL = {"am0630": "y_am10", "am0700": "y_amrest10", "pm1200": "y_pm10", "pm1400": "y_pm10"}
LABEL_C = {"am0630": "y_am10_c", "am0700": "y_amrest10_c", "pm1200": "y_pm10_c", "pm1400": "y_pm10_c"}
L2 = {"am0630": 3.0, "am0700": 10.0, "pm1200": 3.0, "pm1400": 10.0}
AREAS = ['west', 'northeast', 'south', 'central']
DIRS = ['west', 'northeast', 'south']
DUMMIES = ['area_west', 'area_northeast', 'area_south']

def xf(cols):
    import re
    return ",\n    ".join(f"LN(1 + GREATEST({c}, 0)) AS {c}" if re.match(r'^(cat_|catj_|sx_|et_cx|et_n|early_cx)', c) else c for c in cols)

TEMPLATE = """-- generated by gen_roll_area.py: variant={variant} slot={slot} area={area}
DECLARE months ARRAY<DATE> DEFAULT {months};
CREATE TABLE IF NOT EXISTS `{pred}` (
  variant STRING, mname STRING, area STRING, fold DATE, d DATE, label BOOL, p FLOAT64, trained_at TIMESTAMP);
DELETE FROM `{pred}` WHERE variant = '{variant}' AND mname = '{slot}' AND area = '{area}';
FOR f IN (SELECT m0 FROM UNNEST(months) m0) DO
  EXECUTE IMMEDIATE FORMAT('''
    CREATE OR REPLACE MODEL `disruptions.exp_ra_{variant}_{slot}_{area}`
    TRANSFORM(
    {xf},
    label)
    OPTIONS({options})
    AS SELECT {cols}, {labelcol} AS label
       FROM `{table}`
       WHERE {rows} AND {labelcol} IS NOT NULL AND d < '%t' ''', f.m0);
  EXECUTE IMMEDIATE FORMAT('''
    INSERT INTO `{pred}`
    SELECT '{variant}', '{slot}', {areaexpr}, DATE '%t', d, label,
      (SELECT prob FROM UNNEST(predicted_label_probs) WHERE LOWER(CAST(label AS STRING)) = 'true'),
      CURRENT_TIMESTAMP()
    FROM ML.PREDICT(MODEL `disruptions.exp_ra_{variant}_{slot}_{area}`,
      (SELECT *, {labelcol} AS label FROM `{table}`
       WHERE {rows} AND {labelcol} IS NOT NULL AND d >= '%t' AND d < DATE_ADD('%t', INTERVAL 1 MONTH)))''',
    f.m0, f.m0, f.m0);
END FOR;
DROP MODEL IF EXISTS `disruptions.exp_ra_{variant}_{slot}_{area}`;
"""

LR_OPTIONS = ("model_type='LOGISTIC_REG', input_label_cols=['label'], data_split_method='NO_SPLIT', "
              "l2_reg={l2}, max_iterations=50, early_stop=TRUE")
# the study's tuned tree (02-findings §14j): depth 3, 200 trees, lr 0.05, subsample/colsample 0.8, min child weight 10
BT_OPTIONS = ("model_type='BOOSTED_TREE_CLASSIFIER', input_label_cols=['label'], data_split_method='NO_SPLIT', "
              "max_tree_depth=3, num_parallel_tree=1, max_iterations=200, learn_rate=0.05, subsample=0.8, "
              "colsample_bytree=0.8, min_tree_child_weight=10, l2_reg=1.0, early_stop=FALSE")

def emit(variant, slot, area, cols, labelcol, rows, areaexpr, options=None, table=TABLE):
    OUT = pathlib.Path(sys.argv[1])
    options = (options or LR_OPTIONS).format(l2=L2[slot])
    sql = TEMPLATE.format(variant=variant, slot=slot, area=area, months=MONTHS, pred=PRED, table=table,
                          xf=xf(cols), cols=", ".join(cols), labelcol=labelcol, rows=rows, areaexpr=areaexpr, options=options)
    p = OUT / f"roll_{variant}_{slot}_{area}.sql"; p.write_text(sql); return p

if __name__ == "__main__":
  OUT = pathlib.Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
  files = []
  for slot in G:
      # base: the production recipe on area-wide rows (area='west' carries identical global columns)
      files.append(emit("base", slot, "all", G[slot], LABEL[slot], "area = 'west' AND eligible", "'all'"))
      for area in AREAS:
          rows = f"area = '{area}' AND eligible_c"
          files.append(emit("L", slot, area, G[slot], LABEL_C[slot], rows, f"'{area}'"))
          files.append(emit("S", slot, area, G[slot] + A[slot], LABEL_C[slot], rows, f"'{area}'"))
          if slot.startswith("am"):
              files.append(emit("SC", slot, area, G[slot] + A[slot] + cat(slot[2:], "_c"), LABEL_C[slot], rows, f"'{area}'"))
      files.append(emit("P", slot, "dirs", G[slot] + A[slot] + DUMMIES, LABEL_C[slot],
                        "area IN ('west','northeast','south') AND eligible_c", "area"))
  if len(sys.argv) > 2 and sys.argv[2] == "wave2":
      files = []
      D4 = DUMMIES + ['area_central']
      for slot in G:
          files.append(emit("P4", slot, "all4", G[slot] + A[slot] + D4, LABEL_C[slot], "eligible_c", "area"))
          for l2 in (1.0, 10.0, 30.0):
              L2[slot] = l2
              files.append(emit(f"Pl2_{l2:g}", slot, "dirs", G[slot] + A[slot] + DUMMIES, LABEL_C[slot],
                                "area IN ('west','northeast','south') AND eligible_c", "area"))
          L2[slot] = {"am0630": 3.0, "am0700": 10.0, "pm1200": 3.0, "pm1400": 10.0}[slot]
          if slot.startswith("pm"):
              files.append(emit("PMplus", slot, "dirs", G[slot] + A[slot] + DUMMIES + ['am_t10', 'am_cancel', 'am_t10_known_c', 'am_cancel_c'], LABEL_C[slot],
                                "area IN ('west','northeast','south') AND eligible_c", "area"))
  if len(sys.argv) > 2 and sys.argv[2] == "wave3":
      # PT: boosted trees on the pooled table — the study's tree verdict (§14c) was on ~300 rows; P has 3x
      files = [emit("PT", slot, "dirs", G[slot] + A[slot] + DUMMIES, LABEL_C[slot],
                    "area IN ('west','northeast','south') AND eligible_c", "area", options=BT_OPTIONS) for slot in G]
  if len(sys.argv) > 2 and sys.argv[2] == "wave4":
      # F*: the production recipe (base rows and columns) trained on CORRECTED labels, from
      # 02-removed-journeys: _f2 = observed-only actuals + silent tails as cancelled, _f3 = _f2 +
      # GTFS-planned trains absent from ET as cancelled. Scored against their own label; eval_area
      # re-scores base and F* against every label set so the label effect and the model effect separate.
      files = [emit(v, slot, "all", G[slot], LABEL[slot] + suf, "area = 'west' AND eligible", "'all'",
                    table="disruptions.exp_rush_area_training_fix")
               for slot in G for v, suf in (("F2", "_f2"), ("F3", "_f3"))]
  if len(sys.argv) > 2 and sys.argv[2] == "wave5":
      # F4a/F4b: labels that count a standing-timetable cut as trouble (04-cuts): _f4a = every cut,
      # _f4b = only a cut not already in force the previous weekday. *C adds the cut itself as a
      # feature, known at 02:06 from the day's GTFS. cut_* are counts, so they take the log1p TRANSFORM
      # through the et_cx prefix rule? No -- they are named cut_*, so xf() leaves them linear; add them
      # to the prefix regex if a later wave wants log1p.
      CUT = {"am0630": ['cut_am', 'cut_am_new'], "am0700": ['cut_amrest', 'cut_amrest_new'],
             "pm1200": ['cut_pm', 'cut_pm_new'], "pm1400": ['cut_pm', 'cut_pm_new']}
      files = []
      for slot in G:
          for v, suf in (("F4a", "_f4a"), ("F4b", "_f4b")):
              files.append(emit(v, slot, "all", G[slot], LABEL[slot] + suf, "area = 'west' AND eligible", "'all'",
                                table="disruptions.exp_rush_area_training_fix"))
              files.append(emit(v + "C", slot, "all", G[slot] + CUT[slot], LABEL[slot] + suf, "area = 'west' AND eligible", "'all'",
                                table="disruptions.exp_rush_area_training_fix"))
          files.append(emit("BC", slot, "all", G[slot] + CUT[slot], LABEL[slot], "area = 'west' AND eligible", "'all'",
                            table="disruptions.exp_rush_area_training_fix"))
  if len(sys.argv) > 2 and sys.argv[2] == "wave6":
      # The pooled area model (P: three directions, own state, area dummies) on corrected AREA labels,
      # with and without the area's own cut as a feature (04-cuts). Pf3 = label _c_f3 (removed trains),
      # Pf4b = label _c_f4b (+ first-day cuts), Pf4bC = Pf4b + cut_*_c, PC = old area label + cut_*_c.
      CUTC = {"am0630": ['cut_am_c', 'cut_am_new_c'], "am0700": ['cut_amrest_c', 'cut_amrest_new_c'],
              "pm1200": ['cut_pm_c', 'cut_pm_new_c'], "pm1400": ['cut_pm_c', 'cut_pm_new_c']}
      ROWS = "area IN ('west','northeast','south') AND eligible_c"
      T = "disruptions.exp_rush_area_training_fix"
      files = []
      for slot in G:
          cols = G[slot] + A[slot] + DUMMIES
          files.append(emit("Pf3", slot, "dirs", cols, LABEL_C[slot] + "_f3", ROWS, "area", table=T))
          files.append(emit("Pf4b", slot, "dirs", cols, LABEL_C[slot] + "_f4b", ROWS, "area", table=T))
          files.append(emit("Pf4bC", slot, "dirs", cols + CUTC[slot], LABEL_C[slot] + "_f4b", ROWS, "area", table=T))
          files.append(emit("PC", slot, "dirs", cols + CUTC[slot], LABEL_C[slot], ROWS, "area", table=T))
  (OUT / "runlist.txt").write_text("\n".join(str(f) for f in files) + "\n")
  print(len(files), "scripts ->", OUT)
