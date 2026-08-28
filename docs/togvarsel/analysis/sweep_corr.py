#!/usr/bin/env python
"""Corridor-level togvarsel sweep (west / northeast / south rows, AM rush at 06:30).

Tests, with placebo arms and day-cluster bootstrap CIs:
  - area-level features vs corridor-specific features on corridor labels
  - cross-corridor early state (does west's early trouble help forecast northeast?)
  - SX category counts (disruptions.categories) at 06:30
  - LR vs XGB
Usage: uv run --with xgboost --with scikit-learn --with pandas --with numpy python sweep_corr.py <scratchpad>
"""
import os, sys, warnings, time
import numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.metrics import roc_auc_score, brier_score_loss
from xgboost import XGBClassifier

warnings.filterwarnings("ignore")
S = sys.argv[1] if len(sys.argv) > 1 else "."
OUT = f"{S}/out_sweep"; os.makedirs(OUT, exist_ok=True)
RNG = np.random.default_rng(7); T0 = time.time()
def log(*a): print(f"[{time.time()-T0:6.0f}s]", *a, flush=True)
def tobool(s):
    return s.map({"true": 1, "false": 0, "True": 1, "False": 0}).astype(float) if s.dtype == object else s.astype(float)

# ----------------------------------------------------------------------------- data
fc = pd.read_csv(f"{S}/data/features_corridor.csv")
f = pd.read_csv(f"{S}/data/features.csv"); f2 = pd.read_csv(f"{S}/data/features2.csv")
dw = pd.read_csv(f"{S}/data/day_windows.csv")
for c in ["weekday", "holiday", "low_season"]: fc[c] = tobool(fc[c])
fc["d"] = pd.to_datetime(fc["d"])
f["d"] = pd.to_datetime(f["d"]); f2["d"] = pd.to_datetime(f2["d"]); dw["d"] = pd.to_datetime(dw["d"])
# corridor AM labels on the 10-min bar + cancellation share, from day_windows
am = dw[(dw.win == "am_rush") & dw.corridor.isin(["west", "northeast", "south"])][["d", "corridor", "trouble10", "cancel_rate"]]
am = am.rename(columns={"trouble10": "am_t10_c", "cancel_rate": "am_cx_c"})
fc = fc.merge(am, on=["d", "corridor"], how="left")
# cross-corridor early state
piv = fc.pivot_table(index="d", columns="corridor", values=["early_t5", "early_cx", "sx_inn_0630_c", "sx_amj_inn_0630_c"])
piv.columns = [f"{a}__{b}" for a, b in piv.columns]
fc = fc.merge(piv.reset_index(), on="d", how="left")
# day-level features (area F15 + categories + weather)
dup = [c for c in f2.columns if c in f.columns and c != "d"]
day = f.merge(f2.drop(columns=dup), on="d", how="left")
daycols = ["et_t5_0630", "et_cx_0630", "et_cxj_0630", "et_md_0630", "prev_eve_cancel", "prev_pm_t5", "sx_inn_0630", "sx_amj_inn_0630",
           "sx_mt_0630", "sx_big_0630", "sx_n_0630", "am_t5", "am_t10", "wx_tmin", "wx_tmin_night", "wx_snow", "wx_precip_night", "wx_wind"] + \
          [c for c in day.columns if c.startswith("cat_") and c.endswith("_0630")] + ["catj_infra_am_0630", "catj_mat_am_0630", "catj_drift_am_0630"]
day = day[["d"] + daycols].rename(columns={"am_t5": "am_t5_area", "am_t10": "am_t10_area"})
fc = fc.merge(day, on="d", how="left")
el = (fc.weekday == 1) & (fc.holiday == 0) & (fc.am_n > 80) & (fc.pm_n > 80)
fc = fc[el].sort_values(["d", "corridor"]).reset_index(drop=True)
for k in range(2, 7): fc[f"dow_{k}"] = (fc.dow == k).astype(int)
for c in ["west", "northeast", "south"]: fc[f"is_{c}"] = (fc.corridor == c).astype(int)
fc["y_am5"] = (fc.am_t5 >= 0.25).astype(int); fc["y_am10"] = (fc.am_t10_c >= 0.10).astype(int); fc["y_amcx"] = (fc.am_cx_c >= 0.05).astype(int)
log(f"corridor rows: {len(fc)}  days: {fc.d.nunique()}  base rates:",
    {t: f"{fc[t].sum()}/{len(fc)}" for t in ["y_am5", "y_am10", "y_amcx"]},
    "per corridor y_am5:", fc.groupby("corridor").y_am5.mean().round(3).to_dict())

CAL = ["dow_2", "dow_3", "dow_4", "dow_5", "dow_6", "low_season"]
ID = ["is_west", "is_northeast", "is_south"]
AREA = ["et_t5_0630", "et_cx_0630", "et_cxj_0630", "et_md_0630", "prev_eve_t5", "prev_eve_cancel", "prev_pm_t5",
        "sx_inn_0630", "sx_infra_0630", "sx_amj_0630", "sx_amj_inn_0630", "sx_mt_0630", "sx_big_0630", "sx_n_0630"] + CAL
CSPEC = ["early_t5", "early_cx", "prev_pm_t5_c", "sx_inn_0630_c", "sx_amj_inn_0630_c", "sx_amj_0630_c", "sx_big_0630_c", "sx_infra_0630_c"]
CORR = ID + ["early_t5", "early_cx", "et_t5_0630_all", "prev_eve_t5", "prev_pm_t5_c", "sx_inn_0630_c", "sx_amj_inn_0630_c",
             "sx_amj_0630_c", "sx_big_0630_c", "sx_infra_0630_c", "sx_infra_0630"] + CAL
X = [c for c in fc.columns if "__" in c]
CAT = [c for c in fc.columns if c.startswith("cat_") and c.endswith("_0630")] + ["catj_infra_am_0630", "catj_mat_am_0630", "catj_drift_am_0630"]
WX = ["wx_tmin", "wx_tmin_night", "wx_snow", "wx_precip_night", "wx_wind"]
SETS = {"AREA": AREA, "AREA+ID": AREA + ID, "AREA+ID+CSPEC": AREA + ID + CSPEC, "CORR": CORR, "CORR+X": CORR + X,
        "CORR+CAT": CORR + CAT, "CORR+X+CAT": CORR + X + CAT, "CORR+X+CAT+WX": CORR + X + CAT + WX, "FULL": AREA + ID + CSPEC + X + CAT}
missing = {k: [c for c in v if c not in fc.columns] for k, v in SETS.items()}; missing = {k: v for k, v in missing.items() if v}
assert not missing, missing

# ----------------------------------------------------------------------------- models / protocols
def make_lr(): return make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(), LogisticRegressionCV(Cs=[0.03, 0.1, 0.3, 1.0, 3.0], cv=5, scoring='neg_log_loss', max_iter=5000))
def make_xgb(seed): return XGBClassifier(n_estimators=200, max_depth=3, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8, min_child_weight=5, reg_lambda=1.0, random_state=seed, verbosity=0, n_jobs=4)
COUNT_PREFIX = ("cat_", "catj_", "sx_", "et_cx", "et_n", "early_cx")
def prep_lr(X):
    X = X.copy()
    for c in X.columns:
        if c.startswith(COUNT_PREFIX): X[c] = np.log1p(X[c].clip(lower=0))
    return X

def fit_pred(kind, Xtr, ytr, Xte):
    if len(np.unique(ytr)) < 2: return np.full(len(Xte), float(ytr.mean()))
    if kind == "lr":
        try:
            m = make_lr(); m.fit(Xtr, ytr)
        except Exception:
            m = make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(), LogisticRegression(C=0.3, max_iter=5000)); m.fit(Xtr, ytr)
        return m.predict_proba(Xte)[:, 1]
    ps = []
    for s in range(3):
        m = make_xgb(s); m.fit(Xtr, ytr); ps.append(m.predict_proba(Xte)[:, 1])
    return np.mean(ps, axis=0)
def splits(d, proto):
    if proto == "A": yield d < "2026-04-01", d >= "2026-04-01"
    elif proto == "B": yield d >= "2025-10-01", d < "2025-10-01"
    elif proto == "ROLL":
        per = d.dt.to_period("M")
        for m in sorted(set(per[d >= "2025-10-01"])): yield d < m.to_timestamp(), per == m
def oos(kind, data, feats, target, proto):
    p = pd.Series(np.nan, index=data.index); Xm = data[feats].astype(float); y = data[target]
    if kind == "lr": Xm = prep_lr(Xm)
    for tr, te in splits(data.d, proto): p[te] = fit_pred(kind, Xm[tr].values, y[tr].values, Xm[te].values)
    return p
def auc_ci_cluster(y, p, days, n=1000, seed=0):
    """bootstrap over DAYS (corridor rows of one day are correlated)."""
    y = np.asarray(y); p = np.asarray(p, float); days = np.asarray(days); m = ~np.isnan(p); y, p, days = y[m], p[m], days[m]
    a = roc_auc_score(y, p); ud = np.unique(days); r = np.random.default_rng(seed); bs = []
    idx_by_day = {d: np.where(days == d)[0] for d in ud}
    for _ in range(n):
        pick = r.choice(ud, len(ud)); i = np.concatenate([idx_by_day[d] for d in pick])
        if y[i].min() == y[i].max(): continue
        bs.append(roc_auc_score(y[i], p[i]))
    return a, float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)), int(len(y)), int(y.sum()), brier_score_loss(y, p)

CACHE = {}; ROWS = []; PROTOS = ["A", "B", "ROLL"]
def run(target, setname, kind, proto):
    key = (target, setname, kind, proto)
    if key in CACHE: return CACHE[key]
    p = oos(kind, fc, SETS[setname], "y_" + target, proto); CACHE[key] = p
    a, lo, hi, n, npos, brier = auc_ci_cluster(fc["y_" + target], p, fc.d)
    row = dict(target=target, set=setname, model=kind, proto=proto, auc=round(a, 3), lo=round(lo, 3), hi=round(hi, 3), n=n, npos=npos, brier=round(brier, 4), nfeat=len(SETS[setname]))
    for c in ["west", "northeast", "south"]:
        m = (fc.corridor == c) & p.notna()
        row["auc_" + c] = round(roc_auc_score(fc.loc[m, "y_" + target], p[m]), 3) if fc.loc[m, "y_" + target].nunique() == 2 else np.nan
    ROWS.append(row)
    if proto == "ROLL":
        for sub, msk in (("ROLLa", fc.d >= "2026-04-01"), ("ROLLb", fc.d < "2026-04-01")):
            pp = p.where(msk); a, lo, hi, n, npos, brier = auc_ci_cluster(fc["y_" + target], pp, fc.d)
            ROWS.append(dict(target=target, set=setname, model=kind, proto=sub, auc=round(a, 3), lo=round(lo, 3), hi=round(hi, 3), n=n, npos=npos, brier=round(brier, 4), nfeat=len(SETS[setname])))
    return p

# the report's comparison: DAY-level model (area labels) evaluated on corridor labels
def area_model_on_corridor(kind, proto, target):
    dd = day.merge(f[["d", "weekday", "holiday", "am_n", "pm_n", "dow", "low_season", "prev_eve_t5", "sx_infra_0630", "sx_amj_0630"]], on="d")
    for c in ["weekday", "holiday", "low_season"]: dd[c] = tobool(dd[c])
    dd = dd[(dd.weekday == 1) & (dd.holiday == 0) & (dd.am_n > 300) & (dd.pm_n > 300)].sort_values("d").reset_index(drop=True)
    for k in range(2, 7): dd[f"dow_{k}"] = (dd.dow == k).astype(int)
    dd["y"] = ((dd.am_t5_area >= 0.25) if target == "am5" else (dd.am_t10_area >= 0.10)).astype(int)
    p = oos(kind, dd, AREA, "y", proto)
    return fc[["d"]].merge(pd.DataFrame(dict(d=dd.d, p=p.values)), on="d", how="left").p

PLAN = {"am5": list(SETS), "am10": ["AREA", "AREA+ID", "CORR", "CORR+X", "CORR+CAT", "CORR+X+CAT", "FULL"], "amcx": ["CORR", "CORR+X+CAT", "FULL"]}
for target, sets in PLAN.items():
    for setname in sets:
        for kind in ("lr", "xgb"):
            for proto in PROTOS: run(target, setname, kind, proto)
    for kind in ("lr", "xgb"):
        for proto in PROTOS:
            if target == "amcx": continue
            p = area_model_on_corridor(kind, proto, target); CACHE[(target, "DAYMODEL", kind, proto)] = p
            a, lo, hi, n, npos, brier = auc_ci_cluster(fc["y_" + target], p, fc.d)
            row = dict(target=target, set="DAYMODEL", model=kind, proto=proto, auc=round(a, 3), lo=round(lo, 3), hi=round(hi, 3), n=n, npos=npos, brier=round(brier, 4), nfeat=len(AREA))
            for c in ["west", "northeast", "south"]:
                m = (fc.corridor == c) & p.notna(); row["auc_" + c] = round(roc_auc_score(fc.loc[m, "y_" + target], p[m]), 3)
            ROWS.append(row)
    log("done", target)
pd.DataFrame(ROWS).to_csv(f"{OUT}/corr_results.csv", index=False)

# ----------------------------------------------------------------------------- placebo (permute extra columns WITHIN corridor)
def placebo(target, base_name, plus_name, kind, proto, nperm):
    base = SETS[base_name]; extra = [c for c in SETS[plus_name] if c not in base]; y = fc["y_" + target]
    p_real = CACHE[(target, plus_name, kind, proto)]; msk = p_real.notna().values
    a_base = roc_auc_score(y[msk], CACHE[(target, base_name, kind, proto)][msk]); a_real = roc_auc_score(y[msk], p_real[msk])
    aucs = []
    for k in range(nperm):
        dfp = fc.copy()
        for c in ["west", "northeast", "south"]:
            idx = np.where(dfp.corridor == c)[0]; dfp.loc[idx, extra] = dfp.loc[idx, extra].values[RNG.permutation(len(idx))]
        aucs.append(roc_auc_score(y[msk], oos(kind, dfp, base + extra, "y_" + target, proto)[msk]))
    aucs = np.array(aucs)
    return dict(target=target, base=base_name, plus=plus_name, model=kind, proto=proto, n_extra=len(extra), nperm=nperm, auc_base=round(a_base, 3), auc_real=round(a_real, 3),
                placebo_mean=round(aucs.mean(), 3), placebo_sd=round(aucs.std(), 3), placebo_max=round(aucs.max(), 3), delta_real=round(a_real - a_base, 3),
                delta_placebo=round(aucs.mean() - a_base, 3), net=round(a_real - aucs.mean(), 3), z=round((a_real - aucs.mean()) / (aucs.std() + 1e-9), 2),
                share_placebo_ge_real=round(float((aucs >= a_real).mean()), 2))
PL = []
for target, base, plus in [("am5", "AREA+ID", "AREA+ID+CSPEC"), ("am5", "CORR", "CORR+X"), ("am5", "CORR", "CORR+CAT"), ("am5", "CORR+X", "CORR+X+CAT"),
                           ("am10", "AREA+ID", "CORR"), ("am10", "CORR", "CORR+X"), ("am10", "CORR", "CORR+CAT")]:
    for kind in ("lr", "xgb"):
        for proto in PROTOS: PL.append(placebo(target, base, plus, kind, proto, 20 if kind == "lr" else 10))
    log("placebo", target, base, "->", plus)
pd.DataFrame(PL).to_csv(f"{OUT}/corr_placebo.csv", index=False)

# ----------------------------------------------------------------------------- bins per corridor (ROLL)
B = []
for (t, s, k) in [("am5", "CORR", "lr"), ("am5", "CORR+CAT", "lr"), ("am5", "CORR+X+CAT", "xgb"), ("am5", "FULL", "xgb"), ("am10", "CORR+CAT", "lr"), ("am10", "CORR+X+CAT", "xgb")]:
    p = CACHE[(t, s, k, "ROLL")]; m = p.notna()
    tmp = pd.DataFrame(dict(p=p[m], y=fc.loc[m, "y_" + t], t5=fc.loc[m, "am_t5"], cx=fc.loc[m, "am_cx_c"], corridor=fc.loc[m, "corridor"]))
    tmp["level"] = pd.cut(tmp.p, [-1, 0.10, 0.30, 2], labels=["green", "yellow", "red"])
    for c, g in tmp.groupby("corridor"):
        gg = g.groupby("level", observed=False).agg(days=("y", "size"), bad=("y", "mean"), trouble=("t5", "mean"), cancelled=("cx", "mean")); gg["share"] = gg.days / gg.days.sum()
        gg.insert(0, "corridor", c); gg.insert(0, "cfg", f"{t}/{s}/{k}/ROLL"); B.append(gg.reset_index())
pd.concat(B).to_csv(f"{OUT}/corr_bins.csv", index=False)
# pooled predictions
po = fc[["d", "corridor", "am_t5", "am_t10_c", "am_cx_c", "y_am5", "y_am10"]].copy()
for (t, s, k) in [("am5", "CORR", "lr"), ("am5", "CORR+CAT", "lr"), ("am5", "CORR+X+CAT", "xgb"), ("am5", "FULL", "xgb"), ("am10", "CORR+CAT", "lr"), ("am10", "CORR+X+CAT", "xgb"), ("am5", "DAYMODEL", "lr")]:
    for proto in PROTOS: po[f"p_{t}_{s}_{k}_{proto}"] = CACHE[(t, s, k, proto)].values
po.to_csv(f"{OUT}/corr_preds_headline.csv", index=False)
# importance
imp = []
for target, setname in [("am5", "FULL"), ("am10", "FULL")]:
    Xm = fc[SETS[setname]].astype(float); y = fc["y_" + target]; gains = np.zeros(Xm.shape[1])
    for s in range(5):
        m = make_xgb(s); m.fit(Xm.values, y.values); sc = m.get_booster().get_score(importance_type="gain")
        for i in range(Xm.shape[1]): gains[i] += sc.get(f"f{i}", 0.0)
    gains /= gains.sum() + 1e-12
    for i, c in enumerate(Xm.columns): imp.append(dict(target=target, set=setname, feature=c, importance=round(float(gains[i]), 4)))
pd.DataFrame(imp).to_csv(f"{OUT}/corr_importance.csv", index=False)
log("all done")
