"""Score the pooled area model and its corrected-label / cut-feature variants per direction (04-cuts).
Inputs:
  pred.csv    SELECT variant, mname, area, fold, d, label, p FROM disruptions.exp_rush_area_pred
              WHERE variant IN ('base','P','Pf3','Pf4b','Pf4bC','PC')
  labels.csv  the export of exp_rush_area_training_fix (d, area, eligible_c, y_*_c, y_*_c_f3, y_*_c_f4b, ...)
Run: uv run --with scikit-learn,pandas,numpy python eval_cuts_area.py pred.csv labels.csv
base (today's network forecast) is scored on each AREA's label, as in 01-omraader §4a.
"""
import sys
import numpy as np, pandas as pd
from sklearn.metrics import roc_auc_score, log_loss

pred = pd.read_csv(sys.argv[1], parse_dates=["d"])
lab = pd.read_csv(sys.argv[2], parse_dates=["d"])
LAB = {"am0630": "y_am10_c", "am0700": "y_amrest10_c", "pm1200": "y_pm10_c", "pm1400": "y_pm10_c"}
AREAS = ["west", "northeast", "south"]
rng = np.random.default_rng(7)

def boot(y, a, b, n=2000):
    y, a, b = np.asarray(y), np.asarray(a), np.asarray(b)
    out = []
    for _ in range(n):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() == y[i].max(): continue
        out.append(roc_auc_score(y[i], a[i]) - roc_auc_score(y[i], b[i]))
    return np.percentile(out, [2.5, 97.5])

def series(variant, slot, area, suf):
    if variant == "base":
        p = pred[(pred.variant == "base") & (pred.mname == slot)][["d", "p"]]
    else:
        p = pred[(pred.variant == variant) & (pred.mname == slot) & (pred.area == area)][["d", "p"]]
    col = LAB[slot] + suf
    y = lab[lab.area == area][["d", col, "eligible_c"]].rename(columns={col: "y"})
    m = p.merge(y, on="d"); m = m[m.eligible_c.astype(bool) & m.y.notna()]
    return m.d.values, m.y.astype(bool).astype(int).values, m.p.values

VARS = [v for v in ["base", "P", "Pf3", "Pf4b", "Pf4bC", "PC"] if (pred.variant == v).any()]
for suf, name in (("", "old area label"), ("_f3", "f3: removed trains counted"), ("_f4b", "f4b: + first-day cuts")):
    print(f"\n## AUC per direction on the {name}")
    print("| issue | area | n | bad | " + " | ".join(VARS) + " |")
    print("|---|---|---|---|" + "---|" * len(VARS))
    for slot in LAB:
        for area in AREAS:
            cells = []; n = bad = 0
            for v in VARS:
                d, y, p = series(v, slot, area, suf)
                if len(y) == 0 or y.min() == y.max(): cells.append("–"); continue
                n, bad = len(y), int(y.sum())
                cells.append(f"{roc_auc_score(y, p):.3f}")
            print(f"| {slot} | {area} | {n} | {bad} | " + " | ".join(cells) + " |")

print("\n## Paired ΔAUC on the f4b area label, pooled over the three directions, vs base and vs P")
print("| issue | model | vs base [95% CI] | vs P [95% CI] |")
print("|---|---|---|---|")
for slot in LAB:
    for v in [v for v in ["Pf3", "Pf4b", "Pf4bC", "PC"] if v in VARS]:
        rows = []
        for ref in ["base", "P"]:
            frames = []
            for area in AREAS:
                da, ya, pa = series(v, slot, area, "_f4b"); db, yb, pb = series(ref, slot, area, "_f4b")
                m = pd.DataFrame({"d": da, "y": ya, "pa": pa}).merge(pd.DataFrame({"d": db, "pb": pb}), on="d")
                frames.append(m)
            m = pd.concat(frames)
            if len(m) == 0 or m.y.min() == m.y.max(): rows.append("–"); continue
            lo, hi = boot(m.y, m.pa, m.pb)
            rows.append(f"{roc_auc_score(m.y, m.pa) - roc_auc_score(m.y, m.pb):+.3f} [{lo:+.3f}, {hi:+.3f}]")
        print(f"| {slot} | {v} | " + " | ".join(rows) + " |")

print("\n## Push rule (p >= 0.10) on the f4b area label, pooled over the three directions")
print("| issue | model | pushes | precision | recall |")
print("|---|---|---|---|---|")
for slot in LAB:
    for v in VARS:
        ys, ps = [], []
        for area in AREAS:
            d, y, p = series(v, slot, area, "_f4b"); ys.append(y); ps.append(p)
        y, p = np.concatenate(ys), np.concatenate(ps)
        if len(y) == 0: continue
        push = p >= 0.10
        prec = y[push].mean() if push.any() else float("nan"); rec = (y & push).sum() / y.sum() if y.sum() else float("nan")
        print(f"| {slot} | {v} | {int(push.sum())} | {prec:.2f} | {rec:.2f} |")
