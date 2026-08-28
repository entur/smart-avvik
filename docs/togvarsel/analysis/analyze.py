#!/usr/bin/env python
"""Turn out_sweep/*.csv into markdown tables for the findings report."""
import os, sys
import numpy as np, pandas as pd
from sklearn.metrics import roc_auc_score
from scipy import stats

S = sys.argv[1] if len(sys.argv) > 1 else "."
O = f"{S}/out_sweep"
out = []
def H(t): out.append(f"\n### {t}\n")
def T(df, floatfmt=".3f"):
    out.append(df.to_markdown(index=False, floatfmt=floatfmt)); out.append("")

def fmt_auc(r): return f"{r.auc:.2f} [{r.lo:.2f}–{r.hi:.2f}]"

# ------------------------------------------------------------------ day-level results
res = pd.read_csv(f"{O}/results.csv")
res["auc_ci"] = res.apply(fmt_auc, axis=1)
def pivot(df, index=("target", "set", "model")):
    p = df.pivot_table(index=list(index), columns="proto", values="auc_ci", aggfunc="first").reset_index()
    n = df[df.proto == "ROLL"].groupby(list(index))[["n", "npos", "nfeat"]].first().reset_index()
    return p.merge(n, on=list(index))[list(index) + ["nfeat", "A", "B", "ROLL", "ROLLa", "ROLLb", "n", "npos"]]

for t in res.target.unique():
    H(f"Day-level results — target `{t}`  (AUC [95 % bootstrap CI]; A/B = time folds, ROLL = rolling monthly retrain Oct 2025–Aug 2026)")
    T(pivot(res[res.target == t]))

# LR vs XGB paired
pairs = pd.read_csv(f"{O}/pairs_lr_vs_xgb.csv")
H("XGB minus LR, paired bootstrap over days (positive = trees better)")
pv = pairs.assign(s=lambda d: d.apply(lambda r: f"{r.xgb_minus_lr:+.3f} [{r.lo:+.2f},{r.hi:+.2f}]", axis=1)).pivot_table(index=["target", "set"], columns="proto", values="s", aggfunc="first").reset_index()
T(pv)
sig = pairs[(pairs.lo > 0) | (pairs.hi < 0)]
out.append(f"Paired differences whose 95 % CI excludes zero: {len(sig)} of {len(pairs)}  (XGB better: {(sig.xgb_minus_lr>0).sum()}, LR better: {(sig.xgb_minus_lr<0).sum()})\n")

# placebo
pl = pd.read_csv(f"{O}/placebo.csv")
H("Placebo arms — does adding the columns beat dealing them to the wrong rows?")
out.append("`net` = AUC(real) − mean AUC(placebo); `z` = net / sd(placebo); `share≥` = share of placebo runs that scored at least as well as the real columns.\n")
plv = pl[["target", "base", "plus", "model", "proto", "auc_base", "auc_real", "placebo_mean", "placebo_sd", "delta_real", "net", "z", "share_placebo_ge_real"]]
T(plv)

# bins & calibration
bins = pd.read_csv(f"{O}/bins.csv")
H("Three-level bins on pooled ROLL out-of-sample predictions (green < 10 %, yellow 10–30 %, red ≥ 30 %)")
b = bins.copy(); b["share"] = (b.share * 100).round(0); b["bad"] = (b.bad * 100).round(0); b["trouble"] = (b.trouble * 100).round(0); b["cancelled"] = (b.cancelled * 100).round(1)
T(b[["cfg", "level", "days", "share", "bad", "trouble", "cancelled"]], floatfmt=".0f")
cal = pd.read_csv(f"{O}/calibration.csv")
H("Calibration (5 quantile bins of predicted probability; pred vs observed bad-day rate)")
c = cal.copy(); c["pred"] = (c.pred * 100).round(0); c["obs"] = (c.obs * 100).round(0)
cp = c.pivot_table(index="cfg", columns="bin", values=["pred", "obs"], aggfunc="first")
cp.columns = [f"{a}{b}" for a, b in cp.columns]; T(cp.reset_index(), floatfmt=".0f")

# importance
imp = pd.read_csv(f"{O}/importance.csv")
H("Feature importance (xgb gain share, fit on all 340 days) — top 12 per config")
for (t, s), g in imp[imp.model == "xgb"].groupby(["target", "set"]):
    g = g.sort_values("importance", ascending=False).head(12)
    out.append(f"**{t} / {s}**: " + ", ".join(f"{r.feature} {r.importance:.2f}" for r in g.itertuples()) + "\n")
H("Logistic regression standardized coefficients (fit on all days) — largest |coef|, top 12")
for (t, s), g in imp[imp.model == "lr_stdcoef"].groupby(["target", "set"]):
    g = g.reindex(g.importance.abs().sort_values(ascending=False).index).head(12)
    out.append(f"**{t} / {s}**: " + ", ".join(f"{r.feature} {r.importance:+.2f}" for r in g.itertuples()) + "\n")

# label sensitivity + rank correlation with the continuous share, from headline preds
ph = pd.read_csv(f"{O}/preds_headline.csv")
H("Label sensitivity: AUC of the same ROLL predictions against alternative bad-day definitions")
rows = []
for col in [c for c in ph.columns if c.startswith("p_am5_") and c.endswith("_ROLL")] + [c for c in ph.columns if c.startswith("p_am10_") and c.endswith("_ROLL")]:
    p = ph[col]; m = p.notna()
    r = dict(pred=col.replace("p_", "").replace("_ROLL", ""))
    for lab, y in [("t5≥.20", ph.am_t5 >= .20), ("t5≥.25", ph.am_t5 >= .25), ("t5≥.30", ph.am_t5 >= .30), ("t10≥.10", ph.am_t10 >= .10), ("t10≥.15", ph.am_t10 >= .15), ("cx≥.05", ph.am_cancel >= .05), ("cx≥.10", ph.am_cancel >= .10)]:
        r[lab] = round(roc_auc_score(y[m], p[m]), 3)
    r["spearman_t5"] = round(stats.spearmanr(p[m], ph.am_t5[m]).correlation, 3)
    r["spearman_cx"] = round(stats.spearmanr(p[m], ph.am_cancel[m]).correlation, 3)
    rows.append(r)
T(pd.DataFrame(rows))

# ------------------------------------------------------------------ corridor
if os.path.exists(f"{O}/corr_results.csv"):
    cr = pd.read_csv(f"{O}/corr_results.csv"); cr["auc_ci"] = cr.apply(fmt_auc, axis=1)
    for t in cr.target.unique():
        H(f"Corridor-level results — target `{t}` (AUC [95 % day-cluster bootstrap CI])")
        d = cr[cr.target == t]
        p = d.pivot_table(index=["set", "model"], columns="proto", values="auc_ci", aggfunc="first").reset_index()
        pc = d[d.proto == "ROLL"].set_index(["set", "model"])[["auc_west", "auc_northeast", "auc_south", "nfeat"]].reset_index()
        T(p.merge(pc, on=["set", "model"])[["set", "model", "nfeat", "A", "B", "ROLL", "ROLLa", "ROLLb", "auc_west", "auc_northeast", "auc_south"]])
    cpl = pd.read_csv(f"{O}/corr_placebo.csv")
    H("Corridor placebo arms (extra columns permuted within corridor)")
    T(cpl[["target", "base", "plus", "model", "proto", "auc_base", "auc_real", "placebo_mean", "placebo_sd", "delta_real", "net", "z", "share_placebo_ge_real"]])
    cb = pd.read_csv(f"{O}/corr_bins.csv")
    H("Corridor three-level bins (ROLL pooled)")
    b = cb.copy(); b["share"] = (b.share * 100).round(0); b["bad"] = (b.bad * 100).round(0); b["trouble"] = (b.trouble * 100).round(0); b["cancelled"] = (b.cancelled * 100).round(1)
    T(b[["cfg", "corridor", "level", "days", "share", "bad", "trouble", "cancelled"]], floatfmt=".0f")
    ci = pd.read_csv(f"{O}/corr_importance.csv")
    H("Corridor xgb importance (FULL set) — top 12")
    for (t, s), g in ci.groupby(["target", "set"]):
        g = g.sort_values("importance", ascending=False).head(12)
        out.append(f"**{t} / {s}**: " + ", ".join(f"{r.feature} {r.importance:.2f}" for r in g.itertuples()) + "\n")

open(f"{O}/summary.md", "w").write("\n".join(out))
print("\n".join(out))
