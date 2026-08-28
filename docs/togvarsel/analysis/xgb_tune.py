"""Small xgboost tuning grid, so the LR-vs-trees verdict is not an artefact of one config."""
import sys, time, numpy as np, pandas as pd
from sklearn.metrics import roc_auc_score
S = sys.argv[1]
src = open(f"{S}/sweep.py").read()
exec(src.split("# ----------------------------------------------------------------------------- models")[0])   # data + SETS
from xgboost import XGBClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
COUNT_PREFIX = ("cat_", "catj_", "sx_", "et_cx", "et_n", "early_cx")
def prep_lr(X):
    X = X.copy()
    for c in X.columns:
        if c.startswith(COUNT_PREFIX): X[c] = np.log1p(X[c].clip(lower=0))
    return X
GRID = {
  "base_d3_lr05_n200":      dict(max_depth=3, learning_rate=0.05, n_estimators=200, min_child_weight=5, subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0),
  "stumps_d1_lr05_n300":    dict(max_depth=1, learning_rate=0.05, n_estimators=300, min_child_weight=5, subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0),
  "d2_lr03_n400":           dict(max_depth=2, learning_rate=0.03, n_estimators=400, min_child_weight=5, subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0),
  "d2_lr05_n100":           dict(max_depth=2, learning_rate=0.05, n_estimators=100, min_child_weight=5, subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0),
  "d2_lr02_n600_mcw10_l5":  dict(max_depth=2, learning_rate=0.02, n_estimators=600, min_child_weight=10, subsample=0.8, colsample_bytree=0.6, reg_lambda=5.0),
  "d3_lr05_n200_mcw10":     dict(max_depth=3, learning_rate=0.05, n_estimators=200, min_child_weight=10, subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0),
  "d4_lr02_n500_col06":     dict(max_depth=4, learning_rate=0.02, n_estimators=500, min_child_weight=5, subsample=0.6, colsample_bytree=0.6, reg_lambda=1.0),
  "d2_lr05_n200_col05_l3":  dict(max_depth=2, learning_rate=0.05, n_estimators=200, min_child_weight=5, subsample=0.8, colsample_bytree=0.5, reg_lambda=3.0),
}
def splits(d, proto):
    if proto == "A": yield d < "2026-04-01", d >= "2026-04-01"
    elif proto == "B": yield d >= "2025-10-01", d < "2025-10-01"
    else:
        per = d.dt.to_period("M")
        for m in sorted(set(per[d >= "2025-10-01"])): yield d < m.to_timestamp(), per == m
def oos(fit, X, y, d, proto):
    p = pd.Series(np.nan, index=X.index)
    for tr, te in splits(d, proto): p[te] = fit(X[tr].values, y[tr].values, X[te].values)
    return p
def xgb_fit(params):
    def f(Xtr, ytr, Xte):
        if len(np.unique(ytr)) < 2: return np.full(len(Xte), float(ytr.mean()))
        ps = []
        for s in range(5):
            m = XGBClassifier(random_state=s, verbosity=0, n_jobs=4, **params); m.fit(Xtr, ytr); ps.append(m.predict_proba(Xte)[:, 1])
        return np.mean(ps, axis=0)
    return f
def lr_fit(C):
    def f(Xtr, ytr, Xte):
        m = make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(), LogisticRegression(C=C, max_iter=5000)); m.fit(Xtr, ytr); return m.predict_proba(Xte)[:, 1]
    return f
rows = []
for target, setname in [("am5", "F15"), ("am10", "F15+CAT"), ("amrest5", "AM0700"), ("amcx", "F15+CAT"), ("am10", "F15+EXTRA")]:
    X = df[SETS[setname]].astype(float); y = df["y_" + target]
    for name, params in GRID.items():
        for proto in ("A", "B", "ROLL"):
            p = oos(xgb_fit(params), X, y, df.d, proto); m = p.notna()
            rows.append(dict(target=target, set=setname, model="xgb:" + name, proto=proto, auc=round(roc_auc_score(y[m], p[m]), 3)))
    Xl = prep_lr(X)
    for C in (0.1, 0.3, 1.0):
        for proto in ("A", "B", "ROLL"):
            p = oos(lr_fit(C), Xl, y, df.d, proto); m = p.notna()
            rows.append(dict(target=target, set=setname, model=f"lr:C={C}", proto=proto, auc=round(roc_auc_score(y[m], p[m]), 3)))
    print("done", target, setname, flush=True)
r = pd.DataFrame(rows); r.to_csv(f"{S}/out_sweep/xgb_tune.csv", index=False)
print(r.pivot_table(index=["target", "set", "model"], columns="proto", values="auc").to_string())
