"""Score the production recipe (variant base) and its corrected-label retrains (F2, F3) against
every label set, so the LABEL effect and the MODEL effect separate. Reads two CSV exports:
  pred.csv    SELECT variant, mname, fold, d, label, p FROM disruptions.exp_rush_area_pred WHERE area='all'
  labels.csv  SELECT d, eligible, y_*_old/_f2/_f3 FROM disruptions.exp_rush_area_training_fix WHERE area='west'
Run: uv run --with scikit-learn,pandas,numpy python eval_labels.py pred.csv labels.csv
"""
import sys
import numpy as np, pandas as pd
from sklearn.metrics import roc_auc_score, log_loss

pred = pd.read_csv(sys.argv[1], parse_dates=["d"])
lab = pd.read_csv(sys.argv[2], parse_dates=["d"])
LAB = {"am0630": "y_am10", "am0700": "y_amrest10", "pm1200": "y_pm10", "pm1400": "y_pm10"}
SETS = [s for s in ["old", "f2", "f3", "f4a", "f4b"] if f"y_am10_{s}" in lab.columns]
rng = np.random.default_rng(7)

def boot(y, a, b, n=2000):
    y, a, b = np.asarray(y), np.asarray(a), np.asarray(b)
    d = []
    for _ in range(n):
        i = rng.integers(0, len(y), len(y))
        if y[i].min() == y[i].max(): continue
        d.append(roc_auc_score(y[i], a[i]) - roc_auc_score(y[i], b[i]))
    return np.percentile(d, [2.5, 97.5])

def series(variant, slot, labelset):
    p = pred[(pred.variant == variant) & (pred.mname == slot)][["d", "p"]]
    y = lab[["d", f"{LAB[slot]}_{labelset}", "eligible"]].rename(columns={f"{LAB[slot]}_{labelset}": "y"})
    m = p.merge(y, on="d")
    m = m[m.eligible & m.y.notna()]
    return m.d.values, m.y.astype(bool).astype(int).values, m.p.values

print("## Label sets: bad days per slot (eligible, Oct 2025 .. Sep 2026 scored months)")
for slot in LAB:
    d0, y0, _ = series("base", slot, "old")
    row = [f"{slot}: n={len(y0)}"]
    for s in SETS:
        _, y, _ = series("base", slot, s); row.append(f"{s} bad={int(y.sum())}")
    print("  " + " | ".join(row))

print("\n## AUC / log-loss of each model scored on each label set (rows: model; columns: label set)")
print("| issue | model | " + " | ".join(SETS) + " |")
print("|---|---|" + "---|" * len(SETS))
for slot in LAB:
    for v in [v for v in ["base", "F2", "F3", "BC", "F4a", "F4aC", "F4b", "F4bC"] if (pred.variant == v).any()]:
        cells = []
        for s in SETS:
            d, y, p = series(v, slot, s)
            if len(y) == 0 or y.min() == y.max(): cells.append("–"); continue
            cells.append(f"{roc_auc_score(y, p):.3f} ll {log_loss(y, np.clip(p, 1e-6, 1-1e-6)):.3f}")
        print(f"| {slot} | {v} | " + " | ".join(cells) + " |")

print("\n## Paired ΔAUC, same label set, same days (F3 − base on f3 labels; F2 − base on f2 labels; base on f3 − base on old is the label effect)")
print("| issue | F3−base on f3 [95% CI] | F2−base on f2 [95% CI] | base: f3 − old |")
print("|---|---|---|---|")
for slot in LAB:
    out = []
    for v, s in (("F3", "f3"), ("F2", "f2")):
        da, ya, pa = series(v, slot, s); db, yb, pb = series("base", slot, s)
        m = pd.DataFrame({"d": da, "y": ya, "pa": pa}).merge(pd.DataFrame({"d": db, "pb": pb}), on="d")
        if len(m) == 0 or m.y.min() == m.y.max(): out.append("–"); continue
        lo, hi = boot(m.y, m.pa, m.pb)
        out.append(f"{roc_auc_score(m.y, m.pa) - roc_auc_score(m.y, m.pb):+.3f} [{lo:+.3f}, {hi:+.3f}]")
    _, y_old, p_old = series("base", slot, "old"); _, y_f3, p_f3 = series("base", slot, "f3")
    out.append(f"{roc_auc_score(y_f3, p_f3) - roc_auc_score(y_old, p_old):+.3f}")
    print(f"| {slot} | " + " | ".join(out) + " |")

print("\n## Level bins on f3 labels: bad-day rate per bin (green <0.10, yellow <0.30, red)")
for slot in LAB:
    for v in ["base", "F3"]:
        d, y, p = series(v, slot, "f3")
        if len(y) == 0: continue
        b = np.digitize(p, [0.10, 0.30])
        cells = [f"{['green','yellow','red'][k]} n={int((b==k).sum())} bad={y[b==k].mean():.2f}" if (b==k).any() else f"{['green','yellow','red'][k]} –" for k in range(3)]
        print(f"  {slot} {v}: " + " | ".join(cells))

print("\n## Push rule (p >= 0.10 -> yellow or red): precision and recall of a bad rush, per label set")
print("| issue | model | label | pushes | precision | recall |")
print("|---|---|---|---|---|---|")
for slot in LAB:
    for s in SETS:
        for v in [v for v in ["base", "BC", "F3", "F4a", "F4aC", "F4b", "F4bC"] if (pred.variant == v).any()]:
            d, y, p = series(v, slot, s)
            if len(y) == 0: continue
            push = p >= 0.10
            prec = y[push].mean() if push.any() else float("nan")
            rec = (y & push).sum() / y.sum() if y.sum() else float("nan")
            print(f"| {slot} | {v} | {s} | {int(push.sum())} | {prec:.2f} | {rec:.2f} |")

print("\n## Paired ΔAUC on f4a / f4b labels vs base")
print("| issue | label | model | ΔAUC vs base [95% CI] |")
print("|---|---|---|---|")
for slot in LAB:
    for s in [s for s in ["f4a", "f4b"] if s in SETS]:
        for v in [v for v in ["BC", "F4a", "F4aC", "F4b", "F4bC"] if (pred.variant == v).any()]:
            da, ya, pa = series(v, slot, s); db, yb, pb = series("base", slot, s)
            m = pd.DataFrame({"d": da, "y": ya, "pa": pa}).merge(pd.DataFrame({"d": db, "pb": pb}), on="d")
            if len(m) == 0 or m.y.min() == m.y.max(): continue
            lo, hi = boot(m.y, m.pa, m.pb)
            print(f"| {slot} | {s} | {v} | {roc_auc_score(m.y, m.pa) - roc_auc_score(m.y, m.pb):+.3f} [{lo:+.3f}, {hi:+.3f}] |")
