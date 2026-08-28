#!/usr/bin/env python
"""Day-level togvarsel model sweep.

Logistic regression vs gradient-boosted trees (xgboost), several feature sets, three evaluation
protocols (time-folds A and B as in the report, plus ROLL = rolling-origin monthly retrain),
permutation-placebo arms for every "does adding X help" claim, bootstrap CIs, calibration.

Usage: uv run --with xgboost --with scikit-learn --with pandas --with numpy python sweep.py <scratchpad>
"""
import os, sys, json, warnings, time
import numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression, LogisticRegressionCV
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.metrics import roc_auc_score, brier_score_loss
from xgboost import XGBClassifier

warnings.filterwarnings("ignore")
S = sys.argv[1] if len(sys.argv) > 1 else "."
OUT = f"{S}/out_sweep"
os.makedirs(OUT, exist_ok=True)
RNG = np.random.default_rng(42)
T0 = time.time()

def log(*a):
    print(f"[{time.time()-T0:6.0f}s]", *a, flush=True)

# ----------------------------------------------------------------------------- data
def tobool(s):
    if s.dtype == object:
        return s.map({"true": 1, "false": 0, "True": 1, "False": 0}).astype(float)
    return s.astype(float)

f = pd.read_csv(f"{S}/data/features.csv")
f2 = pd.read_csv(f"{S}/data/features2.csv")
dup = [c for c in f2.columns if c in f.columns and c != "d"]
f2 = f2.drop(columns=dup)
df = f.merge(f2, on="d", how="left")
for c in ["weekday", "holiday", "low_season", "am_bad", "pm_bad", "am_bad20", "pm_bad25"]:
    df[c] = tobool(df[c])
df["d"] = pd.to_datetime(df["d"])
df = df.sort_values("d").reset_index(drop=True)
el = (df.weekday == 1) & (df.holiday == 0) & (df.am_n > 300) & (df.pm_n > 300)
df = df[el].copy().reset_index(drop=True)
log(f"eligible weekdays: {len(df)}  ({df.d.min().date()} .. {df.d.max().date()})")

# lags over the eligible-weekday sequence
df["prev_am_t5"] = df.am_t5.shift(1)
df["prev_mid_t5"] = df.mid_t5.shift(1)
df["roll5_am_t5"] = df.am_t5.shift(1).rolling(5, min_periods=3).mean()
df["roll5_pm_t5"] = df.pm_t5.shift(1).rolling(5, min_periods=3).mean()
df["roll5_am_cancel"] = df.am_cancel.shift(1).rolling(5, min_periods=3).mean()
for k in range(2, 7):
    df[f"dow_{k}"] = (df.dow == k).astype(int)

# labels
LABELS = {
    "am5": (df.am_t5 >= 0.25), "am10": (df.am_t10 >= 0.10), "amcx": (df.am_cancel >= 0.05),
    "pm5": (df.pm_t5 >= 0.30), "pm10": (df.pm_t10 >= 0.15), "pmcx": (df.pm_cancel >= 0.05),
    "amrest5": (df.am_t5_rest0700 >= 0.25), "amrest10": (df.am_t10_rest0700 >= 0.10),
}
for k, v in LABELS.items():
    df["y_" + k] = v.astype(int)
log("label base rates:", {k: f"{df['y_'+k].sum()}/{len(df)}" for k in LABELS})

# ----------------------------------------------------------------------------- feature sets
CAL = ["dow_2", "dow_3", "dow_4", "dow_5", "dow_6", "low_season"]
PERSIST = CAL + ["prev_eve_t5", "prev_eve_cancel", "prev_pm_t5", "prev_am_t5", "roll5_am_t5", "roll5_pm_t5"]
F6 = ["et_t5_0630", "et_cxj_0630", "prev_eve_t5", "sx_inn_0630", "sx_amj_inn_0630", "low_season"]
F15 = ["et_t5_0630", "et_cx_0630", "et_cxj_0630", "et_md_0630", "prev_eve_t5", "prev_eve_cancel", "prev_pm_t5",
       "sx_inn_0630", "sx_infra_0630", "sx_amj_0630", "sx_amj_inn_0630", "sx_mt_0630", "sx_big_0630", "sx_n_0630"] + CAL
def cats(suffix, side):
    return [c for c in df.columns if c.startswith("cat_") and c.endswith("_" + suffix)] + \
           [f"catj_{g}_{side}_{suffix}" for g in ("infra", "mat", "drift")]
CAT0630 = cats("0630", "am")
WX = ["wx_tmin", "wx_tmin_night", "wx_snow", "wx_precip_night", "wx_wind"]
ROLL = ["prev_am_t5", "roll5_am_t5", "roll5_pm_t5", "roll5_am_cancel"]
EXTRA = ["et_t10_0630", "et_max_0630", "et_t5_last_0630", "et_cxr_0630", "sx_planned_0630", "sx_amj_rest_0630",
         "sx_delay_0630", "sx_fv_0630", "sx_long_0630", "sx_pers_0630", "sx_vedl_0630", "sx_preveve", "sx_preveve_inn", "night_t5"]
AM0700 = ["et_t5_0700", "et_t10_0700", "et_cx_0700", "et_cxj_0700", "et_md_0700", "et_max_0700", "et_t5_last_0700",
          "prev_eve_t5", "prev_eve_cancel", "prev_pm_t5", "sx_inn_0700", "sx_big_0700", "sx_n_0700", "sx_amj_0700",
          "sx_amj_inn_0700", "sx_amj_rest_0700", "sx_planned_0700"] + CAL
CAT0700 = cats("0700", "am")
EVE = ["prev_eve_t5", "prev_eve_cancel", "prev_pm_t5", "prev_am_t5", "prev_mid_t5", "roll5_am_t5", "roll5_pm_t5",
       "sx_n_2200p", "sx_inn_2200p", "sx_big_2200p", "sx_planned_2200p", "sx_amj_2200p", "sx_amj_inn_2200p", "sx_amj_rest_2200p"] + CAL
CAT2200 = [c for c in df.columns if c.startswith("cat_") and c.endswith("_2200p")]
PM0630 = F15 + ["sx_pmj_0630", "sx_pmj_inn_0630"]
PM1200 = ["et_t5_1200", "et_cx_1200", "et_t5_last_1200", "et_t5_0630", "prev_eve_t5", "prev_pm_t5",
          "sx_inn_1200", "sx_big_1200", "sx_n_1200", "sx_infra_1200", "sx_pmj_1200", "sx_pmj_inn_1200", "sx_planned_1200"] + CAL
CAT1200 = cats("1200", "pm")
PM1400 = ["et_t5_1400", "et_cx_1400", "et_t5_last_1400", "et_t5_1200", "et_t5_0630", "prev_eve_t5", "prev_pm_t5",
          "sx_inn_1400", "sx_big_1400", "sx_n_1400", "sx_pmj_1400", "sx_pmj_inn_1400", "sx_planned_1400"] + CAL
CAT1400 = cats("1400", "pm")

SETS = {
    "CAL": CAL, "PERSIST": PERSIST, "F6": F6, "F15": F15,
    "F15+CAT": F15 + CAT0630, "F15+WX": F15 + WX, "F15+ROLL": F15 + ROLL, "F15+EXTRA": F15 + EXTRA,
    "ALL": F15 + CAT0630 + WX + ROLL + EXTRA,
    "AM0700": AM0700, "AM0700+CAT": AM0700 + CAT0700,
    "EVE": EVE, "EVE+CAT": EVE + CAT2200, "EVE+CAT+WX": EVE + CAT2200 + WX,
    "PM0630": PM0630, "PM0630+CAT": PM0630 + CAT0630,
    "PM1200": PM1200, "PM1200+CAT": PM1200 + CAT1200,
    "PM1400": PM1400, "PM1400+CAT": PM1400 + CAT1400,
}
missing = {k: [c for c in v if c not in df.columns] for k, v in SETS.items()}
missing = {k: v for k, v in missing.items() if v}
assert not missing, f"missing columns: {missing}"

# ----------------------------------------------------------------------------- models
def make_lr():
    return make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(),
                         LogisticRegressionCV(Cs=[0.03, 0.1, 0.3, 1.0, 3.0], cv=5, scoring='neg_log_loss', max_iter=5000))

def make_xgb(seed):
    return XGBClassifier(n_estimators=200, max_depth=3, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                         min_child_weight=5, reg_lambda=1.0, random_state=seed, verbosity=0, n_jobs=4)

COUNT_PREFIX = ("cat_", "catj_", "sx_", "et_cx", "et_n", "early_cx")
def prep_lr(X):
    X = X.copy()
    for c in X.columns:
        if c.startswith(COUNT_PREFIX): X[c] = np.log1p(X[c].clip(lower=0))
    return X

def fit_pred(kind, Xtr, ytr, Xte):
    if len(np.unique(ytr)) < 2:
        return np.full(len(Xte), float(ytr.mean()))
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
    if proto == "A":
        yield d < "2026-04-01", d >= "2026-04-01"
    elif proto == "B":
        yield d >= "2025-10-01", d < "2025-10-01"
    elif proto == "ROLL":
        per = d.dt.to_period("M")
        for m in sorted(set(per[d >= "2025-10-01"])):
            yield d < m.to_timestamp(), per == m

def oos(kind, data, feats, target, proto):
    p = pd.Series(np.nan, index=data.index)
    X = data[feats].astype(float); y = data[target]
    if kind == "lr": X = prep_lr(X)
    for tr, te in splits(data.d, proto):
        p[te] = fit_pred(kind, X[tr].values, y[tr].values, X[te].values)
    return p

# ----------------------------------------------------------------------------- metrics
def auc_ci(y, p, n=1000, seed=0):
    y = np.asarray(y); p = np.asarray(p, dtype=float); m = ~np.isnan(p); y = y[m]; p = p[m]
    a = roc_auc_score(y, p); r = np.random.default_rng(seed); bs = []; idx = np.arange(len(y))
    for _ in range(n):
        i = r.choice(idx, len(idx))
        if y[i].min() == y[i].max(): continue
        bs.append(roc_auc_score(y[i], p[i]))
    return a, float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5)), int(len(y)), int(y.sum()), brier_score_loss(y, p)

def paired_diff(y, p1, p2, n=1000, seed=0):
    y = np.asarray(y); p1 = np.asarray(p1, dtype=float); p2 = np.asarray(p2, dtype=float)
    m = ~np.isnan(p1) & ~np.isnan(p2); y = y[m]; p1 = p1[m]; p2 = p2[m]
    d0 = roc_auc_score(y, p2) - roc_auc_score(y, p1); r = np.random.default_rng(seed); bs = []; idx = np.arange(len(y))
    for _ in range(n):
        i = r.choice(idx, len(idx))
        if y[i].min() == y[i].max(): continue
        bs.append(roc_auc_score(y[i], p2[i]) - roc_auc_score(y[i], p1[i]))
    return d0, float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))

# ----------------------------------------------------------------------------- sweep
CACHE = {}
ROWS = []
PROTOS = ["A", "B", "ROLL"]

def run(target, setname, kind, proto):
    key = (target, setname, kind, proto)
    if key in CACHE: return CACHE[key]
    p = oos(kind, df, SETS[setname], "y_" + target, proto)
    CACHE[key] = p
    a, lo, hi, n, npos, brier = auc_ci(df["y_" + target], p)
    ROWS.append(dict(target=target, set=setname, model=kind, proto=proto, auc=round(a, 3), lo=round(lo, 3), hi=round(hi, 3),
                     n=n, npos=npos, brier=round(brier, 4), nfeat=len(SETS[setname])))
    if proto == "ROLL":
        for sub, msk in (("ROLLa", df.d >= "2026-04-01"), ("ROLLb", df.d < "2026-04-01")):
            pp = p.where(msk); a, lo, hi, n, npos, brier = auc_ci(df["y_" + target], pp)
            ROWS.append(dict(target=target, set=setname, model=kind, proto=sub, auc=round(a, 3), lo=round(lo, 3), hi=round(hi, 3),
                             n=n, npos=npos, brier=round(brier, 4), nfeat=len(SETS[setname])))
    return p

PLAN = {
    "am5":      ["CAL", "PERSIST", "F6", "F15", "F15+CAT", "F15+WX", "F15+ROLL", "F15+EXTRA", "ALL", "EVE", "EVE+CAT", "EVE+CAT+WX"],
    "am10":     ["CAL", "PERSIST", "F6", "F15", "F15+CAT", "F15+WX", "F15+ROLL", "F15+EXTRA", "ALL", "EVE", "EVE+CAT"],
    "amcx":     ["CAL", "PERSIST", "F15", "F15+CAT", "ALL", "EVE+CAT"],
    "amrest5":  ["F15", "F15+CAT", "AM0700", "AM0700+CAT"],
    "amrest10": ["F15", "F15+CAT", "AM0700", "AM0700+CAT"],
    "pm5":      ["CAL", "PERSIST", "PM0630", "PM0630+CAT", "PM1200", "PM1200+CAT", "PM1400", "PM1400+CAT"],
    "pm10":     ["CAL", "PERSIST", "PM0630", "PM0630+CAT", "PM1200", "PM1200+CAT", "PM1400", "PM1400+CAT"],
    "pmcx":     ["PM0630+CAT", "PM1200+CAT", "PM1400+CAT"],
}
for target, sets in PLAN.items():
    for setname in sets:
        for kind in ("lr", "xgb"):
            for proto in PROTOS:
                run(target, setname, kind, proto)
    log(f"done target {target}")
res = pd.DataFrame(ROWS)
res.to_csv(f"{OUT}/results.csv", index=False)

# ----------------------------------------------------------------------------- paired LR vs XGB
PAIRS = []
for target, sets in PLAN.items():
    for setname in sets:
        for proto in PROTOS:
            p1 = CACHE[(target, setname, "lr", proto)]; p2 = CACHE[(target, setname, "xgb", proto)]
            d0, lo, hi = paired_diff(df["y_" + target], p1, p2)
            PAIRS.append(dict(target=target, set=setname, proto=proto, xgb_minus_lr=round(d0, 3), lo=round(lo, 3), hi=round(hi, 3)))
pd.DataFrame(PAIRS).to_csv(f"{OUT}/pairs_lr_vs_xgb.csv", index=False)
log("paired LR vs XGB done")

# ----------------------------------------------------------------------------- placebo arms
def placebo(target, base_name, extra_name, kind, proto, nperm):
    """AUC of base+extra where the extra columns are dealt to the wrong rows (joint permutation)."""
    base = SETS[base_name]; full = SETS[extra_name]
    extra = [c for c in full if c not in base]
    y = df["y_" + target]
    p_real = CACHE[(target, extra_name, kind, proto)]; msk = p_real.notna().values
    a_base = roc_auc_score(y[msk], CACHE[(target, base_name, kind, proto)][msk])
    a_real = roc_auc_score(y[msk], p_real[msk])
    aucs = []
    for k in range(nperm):
        dfp = df.copy(); perm = RNG.permutation(len(dfp))
        dfp[extra] = dfp[extra].values[perm]
        p = oos(kind, dfp, base + extra, "y_" + target, proto)
        aucs.append(roc_auc_score(y[msk], p[msk]))
    aucs = np.array(aucs)
    z = (a_real - aucs.mean()) / (aucs.std() + 1e-9)
    return dict(target=target, base=base_name, plus=extra_name, model=kind, proto=proto, n_extra=len(extra), nperm=nperm,
                auc_base=round(a_base, 3), auc_real=round(a_real, 3), placebo_mean=round(aucs.mean(), 3), placebo_sd=round(aucs.std(), 3),
                placebo_max=round(aucs.max(), 3), delta_real=round(a_real - a_base, 3), delta_placebo=round(aucs.mean() - a_base, 3),
                net=round(a_real - aucs.mean(), 3), z=round(z, 2), share_placebo_ge_real=round(float((aucs >= a_real).mean()), 2))

PLACEBO = []
COMPARISONS = [("am5", "F6", "F15"), ("am5", "F15", "F15+CAT"), ("am5", "F15", "F15+WX"), ("am5", "F15", "F15+ROLL"), ("am5", "F15", "F15+EXTRA"), ("am5", "F15", "ALL"),
               ("am10", "F6", "F15"), ("am10", "F15", "F15+CAT"), ("am10", "F15", "ALL"),
               ("amrest5", "F15", "AM0700"), ("am5", "EVE", "EVE+CAT"), ("am5", "EVE+CAT", "EVE+CAT+WX"),
               ("pm5", "PM1400", "PM1400+CAT"), ("pm5", "PM0630", "PM0630+CAT"), ("pm10", "PM1400", "PM1400+CAT")]
for target, base, plus in COMPARISONS:
    for kind in ("lr", "xgb"):
        for proto in PROTOS:
            nperm = 20 if kind == "lr" else 10
            PLACEBO.append(placebo(target, base, plus, kind, proto, nperm))
    log(f"placebo {target} {base} -> {plus}")
pd.DataFrame(PLACEBO).to_csv(f"{OUT}/placebo.csv", index=False)

# ----------------------------------------------------------------------------- 3-level bins + calibration (ROLL pooled OOS)
def bins_table(target, setname, kind, proto="ROLL"):
    p = CACHE[(target, setname, kind, proto)]; y = df["y_" + target]; m = p.notna()
    side = "am" if target.startswith("am") else "pm"
    tmp = pd.DataFrame(dict(p=p[m], y=y[m], t5=df.loc[m, f"{side}_t5"], cx=df.loc[m, f"{side}_cancel"]))
    tmp["level"] = pd.cut(tmp.p, [-1, 0.10, 0.30, 2], labels=["green", "yellow", "red"])
    g = tmp.groupby("level", observed=False).agg(days=("y", "size"), bad=("y", "mean"), trouble=("t5", "mean"), cancelled=("cx", "mean"))
    g["share"] = g.days / g.days.sum()
    g.insert(0, "cfg", f"{target}/{setname}/{kind}/{proto}")
    return g.reset_index()

def calib_table(target, setname, kind, proto="ROLL", nb=5):
    p = CACHE[(target, setname, kind, proto)]; y = df["y_" + target]; m = p.notna()
    tmp = pd.DataFrame(dict(p=p[m], y=y[m]))
    tmp["bin"] = pd.qcut(tmp.p.rank(method="first"), nb, labels=False)
    g = tmp.groupby("bin").agg(n=("y", "size"), pred=("p", "mean"), obs=("y", "mean"))
    g.insert(0, "cfg", f"{target}/{setname}/{kind}/{proto}")
    return g.reset_index()

HEAD = [("am5", "F15", "lr"), ("am5", "F15", "xgb"), ("am5", "F15+CAT", "lr"), ("am5", "F15+CAT", "xgb"), ("am5", "ALL", "xgb"),
        ("am10", "F15", "lr"), ("am10", "F15+CAT", "lr"), ("am10", "F15+CAT", "xgb"), ("am10", "ALL", "xgb"),
        ("amcx", "F15+CAT", "lr"), ("amcx", "F15+CAT", "xgb"),
        ("pm5", "PM0630+CAT", "lr"), ("pm5", "PM1400+CAT", "lr"), ("pm5", "PM1400+CAT", "xgb"),
        ("pm10", "PM1400+CAT", "lr"), ("pm10", "PM1400+CAT", "xgb"),
        ("am5", "EVE+CAT", "lr"), ("am5", "EVE+CAT", "xgb"), ("amrest5", "AM0700+CAT", "lr"), ("amrest5", "AM0700+CAT", "xgb")]
pd.concat([bins_table(*h) for h in HEAD]).to_csv(f"{OUT}/bins.csv", index=False)
pd.concat([calib_table(*h) for h in HEAD]).to_csv(f"{OUT}/calibration.csv", index=False)

# ----------------------------------------------------------------------------- pooled OOS predictions for headline configs
pred_out = df[["d", "am_t5", "am_t10", "am_cancel", "pm_t5", "pm_t10", "pm_cancel", "y_am5", "y_am10", "y_pm5", "y_pm10"]].copy()
for (t, s, k) in HEAD:
    for proto in PROTOS:
        pred_out[f"p_{t}_{s}_{k}_{proto}"] = CACHE[(t, s, k, proto)].values
pred_out.to_csv(f"{OUT}/preds_headline.csv", index=False)

# ----------------------------------------------------------------------------- feature importance (fit on all eligible days)
imp_rows = []
for target, setname in [("am5", "ALL"), ("am5", "F15+CAT"), ("am10", "ALL"), ("pm5", "PM1400+CAT"), ("am5", "EVE+CAT")]:
    X = df[SETS[setname]].astype(float); y = df["y_" + target]
    gains = np.zeros(X.shape[1])
    for s in range(5):
        m = make_xgb(s); m.fit(X.values, y.values)
        b = m.get_booster(); sc = b.get_score(importance_type="gain")
        for i in range(X.shape[1]):
            gains[i] += sc.get(f"f{i}", 0.0)
    gains /= gains.sum() + 1e-12
    for i, c in enumerate(X.columns):
        imp_rows.append(dict(target=target, set=setname, model="xgb", feature=c, importance=round(float(gains[i]), 4)))
    lr = make_lr(); lr.fit(X.values, y.values)
    coef = lr[-1].coef_[0][: X.shape[1]]
    for i, c in enumerate(X.columns):
        imp_rows.append(dict(target=target, set=setname, model="lr_stdcoef", feature=c, importance=round(float(coef[i]), 3)))
pd.DataFrame(imp_rows).to_csv(f"{OUT}/importance.csv", index=False)
log("all done")
