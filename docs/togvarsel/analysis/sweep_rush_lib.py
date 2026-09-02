"""Rolling-origin sweep: production feature lists vs candidate additions, LR as in production
(log1p on count columns, standardized, strong L2), with permutation-placebo arms and paired
bootstrap CIs. Prints AUC / log-loss and precision at the production yellow cut and at fixed
push rates, pooled over the out-of-sample months Oct 2025 .. Aug 2026."""
import re, sys, warnings, time
import numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.metrics import roc_auc_score, log_loss, brier_score_loss
from sklearn.ensemble import HistGradientBoostingClassifier
warnings.filterwarnings("ignore")
RNG = np.random.default_rng(7)
T0 = time.time()

tr = pd.read_csv("training.csv", parse_dates=["d"])
f3 = pd.read_csv("features3.csv", parse_dates=["d"])
df = tr.merge(f3, on="d", how="left").sort_values("d").reset_index(drop=True)
for c in df.columns:
    if df[c].dtype == object and set(df[c].dropna().unique()) <= {"true", "false"}:
        df[c] = df[c].map({"true": 1.0, "false": 0.0})
df = df[(df.eligible == 1) & df.y_am10.notna() & df.y_amrest10.notna() & df.y_pm10.notna()].reset_index(drop=True)
for c in ("y_am10","y_amrest10","y_pm10"): df[c] = df[c].astype(int)
print(f"eligible days {len(df)} {df.d.min().date()}..{df.d.max().date()}")

CAL = ['dow_2', 'dow_3', 'dow_4', 'dow_5', 'dow_6', 'low_season']
CAT = lambda s: [f'cat_{k}_{s}' for k in ['drift_ruteendring','drift_kapasitet','materiell_teknisk','infrastruktur_signal','infrastruktur_spor','infrastruktur_strom','infrastruktur_sporveksel','drift_personale','ytre_tredjepart','drift_planlagt','infrastruktur_stasjon','vaer','other']] + [f'catj_{k}_am_{s}' for k in ['infra','mat','drift']]
BASE = {
 "am0630": ['et_t5_0630','et_cx_0630','et_cxj_0630','et_md_0630','prev_eve_t5','prev_eve_cancel','prev_pm_t5','sx_inn_0630','sx_infra_0630','sx_amj_0630','sx_amj_inn_0630','sx_mt_0630','sx_big_0630','sx_n_0630','et_t10_0630','et_max_0630','et_t5_last_0630','et_cxr_0630','sx_planned_0630','sx_amj_rest_0630','sx_delay_0630','sx_fv_0630','sx_long_0630','sx_pers_0630','sx_vedl_0630','sx_preveve','sx_preveve_inn','night_t5'] + CAL + CAT('0630'),
 "am0700": ['et_t5_0700','et_t10_0700','et_cx_0700','et_cxj_0700','et_md_0700','et_max_0700','et_t5_last_0700','prev_eve_t5','prev_eve_cancel','prev_pm_t5','sx_inn_0700','sx_big_0700','sx_n_0700','sx_amj_0700','sx_amj_inn_0700','sx_amj_rest_0700','sx_planned_0700'] + CAL + CAT('0700'),
 "pm1200": ['et_t5_1200','et_cx_1200','et_t5_last_1200','et_t5_0630','prev_eve_t5','prev_pm_t5','sx_inn_1200','sx_big_1200','sx_n_1200','sx_infra_1200','sx_pmj_1200','sx_pmj_inn_1200','sx_planned_1200'] + CAL,
 "pm1400": ['et_t5_1400','et_cx_1400','et_t5_last_1400','et_t5_1200','et_t5_0630','prev_eve_t5','prev_pm_t5','sx_inn_1400','sx_big_1400','sx_n_1400','sx_pmj_1400','sx_pmj_inn_1400','sx_planned_1400'] + CAL,
}
LABEL = {"am0630": "y_am10", "am0700": "y_amrest10", "pm1200": "y_pm10", "pm1400": "y_pm10"}
L2 = {"am0630": 3.0, "am0700": 10.0, "pm1200": 3.0, "pm1400": 10.0}
ADD = {
 "am0630": {
   "recency": ['trend_0630','et_t5_last15_0630','et_t10_last_0630','et_cx_last_0630'],
   "breadth": ['nlines_bad_0630','nlines_any_0630','ncorr_bad_0630','corr_max_t5_0630'],
   "corridors": ['west_t5_0630','ne_t5_0630','south_t5_0630','central_t5_0630'],
   "yesterday": ['prev_am_t10_cal'],
 },
 "am0700": {
   "recency": ['trend_0700','et_t5_last15_0700','et_t10_last_0700','et_cx_last_0700','et_t10_rush_0700'],
   "breadth": ['nlines_bad_0700','nlines_any_0700','ncorr_bad_0700','corr_max_t5_0700'],
   "yesterday": ['prev_am_t10_cal'],
 },
 "pm1200": {
   "midday": ['et_t5_mid_1200','et_t10_mid_1200','et_cx_mid_1200','et_cxj_1200','trend_1200'],
   "morning": ['am_t10_known','am_cancel_known'],
   "breadth": ['nlines_bad_1200'],
   "yesterday": ['prev_pm_t10_cal'],
 },
 "pm1400": {
   "midday": ['et_t5_mid_1400','et_t10_mid_1400','et_cx_mid_1400','et_t5_1200_1350','et_cx_1200_1350','et_cxj_1400','trend_1400'],
   "morning": ['am_t10_known','am_cancel_known'],
   "breadth": ['nlines_bad_1400'],
   "yesterday": ['prev_pm_t10_cal'],
 },
}
COUNT_RE = re.compile(r'^(cat_|catj_|sx_|et_cx|et_n|early_cx|nlines_|ncorr_)')
FOLDS = pd.period_range("2025-10", "2026-08", freq="M")

def X_of(d, cols):
    X = d[cols].astype(float).copy()
    for c in cols:
        if COUNT_RE.match(c):
            X[c] = np.log1p(np.clip(X[c].fillna(0), 0, None))
    return X.values

def lr(C):
    return make_pipeline(SimpleImputer(strategy="mean"), StandardScaler(), LogisticRegression(C=C, max_iter=2000))

def roll(cols, label, model_fn, data=None):
    """Rolling-origin OOS probabilities for every eligible day from Oct 2025; NaN before."""
    data = df if data is None else data
    y = data[label].values
    p = np.full(len(data), np.nan)
    for per in FOLDS:
        tr_m = data.d < per.start_time
        te_m = (data.d >= per.start_time) & (data.d <= per.end_time)
        if te_m.sum() == 0: continue
        m = model_fn().fit(X_of(data[tr_m], cols), y[tr_m])
        p[te_m.values] = m.predict_proba(X_of(data[te_m], cols))[:, 1]
    return p

def metrics(p, y):
    m = ~np.isnan(p)
    p, y = p[m], y[m].astype(int)
    out = dict(n=len(y), auc=roc_auc_score(y, p), ll=log_loss(y, p), brier=brier_score_loss(y, p))
    push = p >= 0.10
    out["prec@0.10"] = y[push].mean() if push.any() else np.nan; out["push@0.10"] = push.mean()
    red = p >= 0.30
    out["prec@0.30"] = y[red].mean() if red.any() else np.nan; out["push@0.30"] = red.mean()
    for q in (0.2, 0.4):
        k = max(1, int(round(q * len(p)))); idx = np.argsort(-p)[:k]
        out[f"prec_top{int(q*100)}"] = y[idx].mean(); out[f"rec_top{int(q*100)}"] = y[idx].sum() / max(1, y.sum())
    return out

def paired_boot(pa, pb, y, B=1000):
    m = ~np.isnan(pa) & ~np.isnan(pb); pa, pb, y = pa[m], pb[m], y[m].astype(int)
    idx_all = np.arange(len(y)); diffs = []
    for _ in range(B):
        idx = RNG.choice(idx_all, len(y), replace=True)
        if y[idx].sum() in (0, len(idx)): continue
        diffs.append(roc_auc_score(y[idx], pb[idx]) - roc_auc_score(y[idx], pa[idx]))
    d = np.array(diffs); return np.percentile(d, 2.5), np.percentile(d, 97.5)

def placebo(base_cols, add_cols, label, C, n_perm=20):
    aucs = []
    for _ in range(n_perm):
        d2 = df.copy()
        perm = RNG.permutation(len(d2))
        for c in add_cols: d2[c] = d2[c].values[perm]
        p = roll(base_cols + add_cols, label, lambda: lr(C), data=d2)
        aucs.append(metrics(p, d2[label].values)["auc"])
    return np.mean(aucs), np.std(aucs)

