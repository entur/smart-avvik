"""Evaluate the rush-area rolling-origin predictions (docs/rushvarsel/01-omraader-2026-09-21.md §4, §6).

Inputs: area_pred.csv (SELECT * FROM disruptions.exp_rush_area_pred) and area_training.csv
(SELECT * FROM disruptions.exp_rush_area_training). For every issue and area, on the SAME days,
compares variant B (the production forecast re-scored against the area's label) with L, S, SC and P:
AUC with a paired bootstrap CI, log-loss, the bad-rate inside the production level bins, and the
page's rates replayed with docs/togvarsel/analysis/rushsim.py.
  uv run --with scikit-learn,pandas,numpy python eval_area.py area_pred.csv area_training.csv
"""
import sys, numpy as np, pandas as pd
from sklearn.metrics import roc_auc_score, log_loss
sys.path.insert(0, __file__.rsplit("/", 1)[0] + "/../../togvarsel/analysis")
import rushsim as R
RNG = np.random.default_rng(3)
pred = pd.read_csv(sys.argv[1], parse_dates=["d", "fold"]); pred["label"] = pred.label.astype(str).str.lower().eq("true")
tr = pd.read_csv(sys.argv[2], parse_dates=["d"])
for c in tr.columns:
    if tr[c].dtype == object and set(tr[c].dropna().unique()) <= {"true", "false"}: tr[c] = tr[c].map({"true": True, "false": False})
SLOTS = ["am0630", "am0700", "pm1200", "pm1400"]
AREAS = ["west", "northeast", "south", "central"]
VARIANTS = ["B", "L", "S", "SC", "P", "E"]  # E = mean of B and S probabilities (a zero-cost ensemble, computed here)
base = pred[pred.variant == "base"][["mname", "d", "p"]].rename(columns={"p": "p_base"})

def series(variant, slot, area):
    """(d, p, label) for one variant on one area's label; B = base p joined to the area's own label."""
    if variant == "B":
        lab = tr[(tr.area == area) & (tr.eligible_c == True)][["d", {"am0630": "y_am10_c", "am0700": "y_amrest10_c"}.get(slot, "y_pm10_c")]]
        lab.columns = ["d", "label"]
        x = base[base.mname == slot].merge(lab, on="d").rename(columns={"p_base": "p"})
    elif variant in ("E", "EP"):
        b, s = series("B", slot, area), series("S" if variant == "E" else "P", slot, area)
        common = b.index.intersection(s.index)
        return pd.DataFrame({"p": (b.loc[common].p.values + s.loc[common].p.values) / 2, "label": s.loc[common].label.values.astype(int)}, index=common)
    else:
        x = pred[(pred.variant == variant) & (pred.mname == slot) & (pred.area == area)][["d", "p", "label"]]
    x = x.dropna().set_index("d").sort_index(); x["label"] = x.label.astype(bool).astype(int); return x

def boot(pa, pb, y, B=1000):
    out = []
    for _ in range(B):
        i = RNG.choice(len(y), len(y), replace=True)
        if y[i].sum() in (0, len(i)): continue
        out.append(roc_auc_score(y[i], pb[i]) - roc_auc_score(y[i], pa[i]))
    return np.percentile(out, 2.5), np.percentile(out, 97.5)

def bins(x):
    out = []
    for lo, hi in [(0, .1), (.1, .3), (.3, 1.01)]:
        s = x[(x.p >= lo) & (x.p < hi)]; out.append(f"{len(s)}→{s.label.mean():.0%}" if len(s) else "0")
    return " / ".join(out)

print("## 4a. Today's forecast on its own label (reference)")
for slot in SLOTS:
    lab = tr[(tr.area == "west") & (tr.eligible == True)][["d", {"am0630": "y_am10", "am0700": "y_amrest10"}.get(slot, "y_pm10")]]; lab.columns = ["d", "label"]
    x = base[base.mname == slot].merge(lab, on="d").rename(columns={"p_base": "p"}).dropna(); x["label"] = x.label.astype(bool).astype(int)
    if len(x) == 0: print(f"{slot}: no predictions"); continue
    print(f"{slot}: n={len(x)} bad={x.label.mean():.0%} AUC={roc_auc_score(x.label, x.p):.3f} logloss={log_loss(x.label, x.p):.3f} bins {bins(x)}")

print("\n## 4b. Per area and issue, same days: AUC (ΔAUC vs B, 95 % CI), log-loss, green/yellow/red n→bad")
print("| issue | area | n | bad | B | L | S | SC | P | E |")
print("|---|---|---|---|---|---|---|---|---|---|")
best = {}
for slot in SLOTS:
    for area in AREAS:
        S = {v: series(v, slot, area) for v in VARIANTS}
        S = {v: s for v, s in S.items() if len(s)}
        if "B" not in S: continue
        common = S["B"].index
        for v in S: common = common.intersection(S[v].index)
        y = S["B"].loc[common].label.values.astype(int); pB = S["B"].loc[common].p.values
        cells = []
        for v in VARIANTS:
            if v not in S: cells.append("–"); continue
            p = S[v].loc[common].p.values
            auc = roc_auc_score(y, p); ll = log_loss(y, p)
            if v == "B": cells.append(f"**{auc:.3f}** ll {ll:.3f}<br>{bins(S[v].loc[common])}")
            else:
                lo, hi = boot(pB, p, y); cells.append(f"{auc:.3f} ({auc - roc_auc_score(y, pB):+.3f} [{lo:+.3f}, {hi:+.3f}]) ll {ll:.3f}<br>{bins(S[v].loc[common])}")
            best[(slot, area, v)] = (auc, ll, len(common))
        print(f"| {slot} | {area} | {len(common)} | {y.mean():.0%} | " + " | ".join(cells) + " |")

print("\n## 4c. Pooled over the three directions (day×area rows), same days")
print("| issue | n rows | bad | B | L | S | SC | P | E |")
print("|---|---|---|---|---|---|---|---|---|")
for slot in SLOTS:
    parts = {v: [] for v in VARIANTS}
    for area in ["west", "northeast", "south"]:
        S = {v: series(v, slot, area) for v in VARIANTS}; S = {v: s for v, s in S.items() if len(s)}
        if "B" not in S: continue
        common = S["B"].index
        for v in S: common = common.intersection(S[v].index)
        for v in S: parts[v].append(S[v].loc[common].assign(area=area))
    cells = []; n = None
    for v in VARIANTS:
        if not parts[v]: cells.append("–"); continue
        x = pd.concat(parts[v]); n = len(x); y = x.label.values.astype(int)
        if len(set(y)) < 2: cells.append("–"); continue
        cells.append(f"{roc_auc_score(y, x.p):.3f} ll {log_loss(y, x.p):.3f}")
    if n: print(f"| {slot} | {n} | {y.mean():.0%} | " + " | ".join(cells) + " |")

print("\n## 6. Page rates per area (rushsim: Presisjon / Dekning / bad rushes warned / pushes per day / expected Presisjon), production cuts")
print("| area | variant | n days | Presisjon | warnings only | Dekning | bad rushes warned | pushes/day | expected |")
print("|---|---|---|---|---|---|---|---|---|")
for area in AREAS:
    for variant in ["B", "L", "S", "P", "E"]:
        if variant == "P" and area == "central": continue
        w = tr[(tr.area == area) & (tr.eligible_c == True)][["d", "am_t10_c", "am_t10_rest_c", "pm_t10_c", "y_am10_c", "y_amrest10_c", "y_pm10_c"]].copy()
        w.columns = ["d", "am_t10", "am_t10_rest0700", "pm_t10", "y_am10", "y_amrest10", "y_pm10"]
        for slot in SLOTS:
            s = series(variant, slot, area)[["p"]].rename(columns={"p": f"p_{slot[2:]}"}).rename_axis("d").reset_index()
            w = w.merge(s, on="d", how="inner")
        if len(w) == 0: continue
        for c in ["y_am10", "y_amrest10", "y_pm10"]: w[c] = w[c].astype(bool)
        sim = R.simulate(w.sort_values("d").reset_index(drop=True)); t = R.stats(sim); warn = sim[sim.sent & (sim.level != "green")]
        print(f"| {area} | {variant} | {len(w)} | {t['presisjon']:.0%} ({t['pres']}) | {warn.justified.mean():.0%} ({int(warn.justified.sum())}/{len(warn)}) | {t['dekning']:.0%} | {t['recall_bad']:.0%} ({t['rec']}) | {t['pushes_per_day']:.2f} | {t['exp_presisjon']:.0%} |")

W2 = ["P", "P4", "Pl2_1", "Pl2_10", "Pl2_30", "PMplus", "EP"]  # EP = mean of B and P
if pred.variant.isin(W2[1:]).any():
    print("\n## 4d. Second wave: pooled variants against B, same days (AUC, ΔAUC vs B [95 % CI], log-loss)")
    print("| issue | area | n | B | " + " | ".join(W2) + " |")
    print("|---|---|---|---|" + "---|" * len(W2))
    for slot in SLOTS:
        for area in ["west", "northeast", "south", "central"]:
            S = {v: series(v, slot, area) for v in ["B"] + W2}; S = {v: x for v, x in S.items() if len(x)}
            if "B" not in S or len(S) < 2: continue
            common = S["B"].index
            for v in S: common = common.intersection(S[v].index)
            y = S["B"].loc[common].label.values.astype(int); pB = S["B"].loc[common].p.values
            cells = [f"{roc_auc_score(y, pB):.3f} ll {log_loss(y, pB):.3f}"]
            for v in W2:
                if v not in S: cells.append("–"); continue
                p_ = S[v].loc[common].p.values; lo, hi = boot(pB, p_, y)
                cells.append(f"{roc_auc_score(y, p_):.3f} ({roc_auc_score(y, p_) - roc_auc_score(y, pB):+.3f} [{lo:+.3f}, {hi:+.3f}]) ll {log_loss(y, p_):.3f}<br>{bins(S[v].loc[common])}")
            print(f"| {slot} | {area} | {len(common)} | " + " | ".join(cells) + " |")
    print("\n## 4e. Second wave pooled over the three directions")
    print("| issue | n rows | B | " + " | ".join(W2) + " |")
    print("|---|---|---|" + "---|" * len(W2))
    for slot in SLOTS:
        parts = {v: [] for v in ["B"] + W2}
        for area in ["west", "northeast", "south"]:
            S = {v: series(v, slot, area) for v in ["B"] + W2}; S = {v: x for v, x in S.items() if len(x)}
            common = S["B"].index
            for v in S: common = common.intersection(S[v].index)
            for v in S: parts[v].append(S[v].loc[common])
        cells = []; n = 0
        for v in ["B"] + W2:
            if not parts[v]: cells.append("–"); continue
            x = pd.concat(parts[v]); n = len(x); y = x.label.values.astype(int)
            cells.append(f"{roc_auc_score(y, x.p):.3f} ll {log_loss(y, x.p):.3f}")
        print(f"| {slot} | {n} | " + " | ".join(cells) + " |")

if (pred.variant == "PT").any():
    print("\n## 4f. Boosted trees on the pooled rows (PT) — PARTIAL: the folds that finished before the run was stopped on cost; B and P on the same days")
    print("| issue | area | n | folds | B | P | PT (ΔAUC vs B [95 % CI]) |")
    print("|---|---|---|---|---|---|---|")
    for slot in SLOTS:
        for area in ["west", "northeast", "south"]:
            t = series("PT", slot, area); b = series("B", slot, area); pp = series("P", slot, area)
            common = t.index.intersection(b.index).intersection(pp.index)
            if len(common) < 20: continue
            y = b.loc[common].label.values.astype(int); pB = b.loc[common].p.values; pT = t.loc[common].p.values
            if len(set(y)) < 2: continue
            nf = pred[(pred.variant == "PT") & (pred.mname == slot)].fold.nunique()
            lo, hi = boot(pB, pT, y)
            print(f"| {slot} | {area} | {len(common)} | {nf} | {roc_auc_score(y, pB):.3f} | {roc_auc_score(y, pp.loc[common].p.values):.3f} | {roc_auc_score(y, pT):.3f} ({roc_auc_score(y, pT) - roc_auc_score(y, pB):+.3f} [{lo:+.3f}, {hi:+.3f}]) ll {log_loss(y, pT):.3f} |")
