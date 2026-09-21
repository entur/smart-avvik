"""Placebo arm for the area-specific feature block (docs/rushvarsel/01-omraader-2026-09-21.md §5).

For each area and issue, rolling-origin monthly (Oct 2025 .. Sep 2026) logistic regression on the
production recipe (log1p on count columns, standardised, strong L2) with three arms on the AREA's
own label and eligible days:
  L      production (area-wide) feature list
  S      production list + the area's own state (_c block)
  S-plc  S with the _c block dealt to the wrong days within the area (N_PERM permutations)
net = AUC(S) - mean AUC(S-plc); z = net / sd(placebo). Usage:
  uv run --with scikit-learn,pandas,numpy python placebo_area.py area_training.csv
"""
import re, sys, warnings
import numpy as np, pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.impute import SimpleImputer
from sklearn.pipeline import make_pipeline
from sklearn.metrics import roc_auc_score
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from gen_roll_area import G, A, LABEL_C, AREAS, cat
warnings.filterwarnings("ignore")
RNG = np.random.default_rng(11)
N_PERM = int(sys.argv[2]) if len(sys.argv) > 2 else 20
C = 0.1  # the study's local optimum (02-findings §14j); the same C in every arm

df = pd.read_csv(sys.argv[1], parse_dates=["d"])
for c in df.columns:
    if df[c].dtype == object and set(df[c].dropna().unique()) <= {"true", "false"}:
        df[c] = df[c].map({"true": 1.0, "false": 0.0})
FOLDS = pd.date_range("2025-10-01", "2026-09-01", freq="MS")
COUNT = re.compile(r'^(cat_|catj_|sx_|et_cx|et_n|early_cx)')

def X_of(d, cols):
    X = d[cols].astype(float).copy()
    for c in cols:
        if COUNT.match(c): X[c] = np.log1p(X[c].clip(lower=0))
    return X.values

def roll(d, cols, label):
    p = pd.Series(np.nan, index=d.index)
    for f in FOLDS:
        tr = d[d.d < f]; te = d[(d.d >= f) & (d.d < f + pd.offsets.MonthBegin(1))]
        if len(te) == 0 or tr[label].nunique() < 2: continue
        m = make_pipeline(SimpleImputer(strategy="mean"), StandardScaler(), LogisticRegression(C=C, max_iter=2000))
        m.fit(X_of(tr, cols), tr[label]); p.loc[te.index] = m.predict_proba(X_of(te, cols))[:, 1]
    return p

print("| area | issue | n OOS | bad | AUC L | AUC S | net vs placebo (z) | placebo sd | AUC SC | net (z) |")
print("|---|---|---|---|---|---|---|---|---|---|")
for slot in G:
    label = LABEL_C[slot]
    for area in AREAS:
        d = df[(df.area == area) & (df.eligible_c == 1) & df[label].notna()].sort_values("d").reset_index(drop=True)
        d[label] = d[label].astype(int)
        oos = d.d >= FOLDS[0]
        y = d.loc[oos, label].values
        pL = roll(d, G[slot], label)[oos].values
        pS = roll(d, G[slot] + A[slot], label)[oos].values
        aucL, aucS = roc_auc_score(y, pL), roc_auc_score(y, pS)
        plc = []
        for _ in range(N_PERM):
            dp = d.copy(); perm = RNG.permutation(len(d)); dp[A[slot]] = d[A[slot]].values[perm]
            plc.append(roc_auc_score(y, roll(dp, G[slot] + A[slot], label)[oos].values))
        plc = np.array(plc); net = aucS - plc.mean(); z = net / plc.std() if plc.std() > 0 else float("nan")
        sc = ""
        if slot.startswith("am"):
            cc = cat(slot[2:], "_c"); pSC = roll(d, G[slot] + A[slot] + cc, label)[oos].values; aucSC = roc_auc_score(y, pSC)
            plc2 = []
            for _ in range(N_PERM):
                dp = d.copy(); perm = RNG.permutation(len(d)); dp[cc] = d[cc].values[perm]
                plc2.append(roc_auc_score(y, roll(dp, G[slot] + A[slot] + cc, label)[oos].values))
            plc2 = np.array(plc2); sc = f"{aucSC:.3f} | {aucSC - plc2.mean():+.3f} ({(aucSC - plc2.mean()) / plc2.std():.1f})"
        else:
            sc = "– | –"
        print(f"| {area} | {slot} | {oos.sum()} | {y.mean():.0%} | {aucL:.3f} | {aucS:.3f} | {net:+.3f} ({z:.1f}) | {plc.std():.3f} | {sc} |", flush=True)
