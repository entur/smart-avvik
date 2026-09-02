import sys, numpy as np, pandas as pd
import rushsim as R
src = sys.argv[1]; variant = sys.argv[2] if len(sys.argv) > 2 else "base"
df = pd.read_csv(src, parse_dates=["d"])
for s in R.SLOTS:
    col = f"p_{s}__{variant}" if f"p_{s}__{variant}" in df.columns else f"p_{s}"
    df[f"p_{s}"] = df[col]
df = df.dropna(subset=[f"p_{s}" for s in R.SLOTS]).sort_values("d").reset_index(drop=True)
for c in ("y_am10","y_amrest10","y_pm10"): df[c] = df[c].astype(str).str.lower().isin(["true","1","1.0"])
print(f"OOS days {df.d.min().date()}..{df.d.max().date()} n={len(df)}  variant={variant}")
print("\n=== production cuts (green<0.10, red>=0.30) ===")
print(R.fmt(R.table(R.simulate(df))))
print("\nmatrix total:"); print(R.matrix(R.simulate(df)))
print("\n=== last 14 / 30 / 90 OOS days ===")
maxd = df.d.max()
for n in (14, 30, 90):
    t = R.stats(R.simulate(df[df.d > maxd - pd.Timedelta(days=n)]))
    print(f"last {n:3d} d: presisjon {t['pres']:>7} ({t['presisjon']:.0%})  dekning {t['dek']:>7}  pushes/day {t['pushes_per_day']:.2f}  expected {t['exp_presisjon']:.0%}")
print("\n=== alternative policies (all OOS days) ===")
pols = {
 "production 0.10/0.30": {s: (0.10, 0.30) for s in R.SLOTS},
 "yellow from 0.15": {s: (0.15, 0.30) for s in R.SLOTS},
 "yellow from 0.20": {s: (0.20, 0.30) for s in R.SLOTS},
 "yellow from 0.25": {s: (0.25, 0.30) for s in R.SLOTS},
 "red only (>=0.30)": {s: (0.30, 0.50) for s in R.SLOTS},
 "AM 0.10/0.30, PM 0.20/0.30": {"0630": (0.10,0.30), "0700": (0.10,0.30), "1200": (0.20,0.30), "1400": (0.20,0.30)},
 "AM 0.10/0.30, 1200 red only, 1400 0.20": {"0630": (0.10,0.30), "0700": (0.10,0.30), "1200": (0.30,0.50), "1400": (0.20,0.30)},
 "AM 0.15/0.30, 1200 red only, 1400 0.20": {"0630": (0.15,0.30), "0700": (0.15,0.30), "1200": (0.30,0.50), "1400": (0.20,0.30)},
 "AM 0.20/0.40, 1200 red only, 1400 0.20": {"0630": (0.20,0.40), "0700": (0.20,0.40), "1200": (0.30,0.50), "1400": (0.20,0.30)},
 "0630 red only, 0700 0.10/0.30, PM prod": {"0630": (0.30,0.50), "0700": (0.10,0.30), "1200": (0.10,0.30), "1400": (0.10,0.30)},
 "0630 red only, 0700 0.20/0.30, 1200 red only, 1400 0.20": {"0630": (0.30,0.50), "0700": (0.20,0.30), "1200": (0.30,0.50), "1400": (0.20,0.30)},
 "0630 0.20, 0700 0.20, 1200 red only, 1400 0.30": {"0630": (0.20,0.30), "0700": (0.20,0.30), "1200": (0.30,0.50), "1400": (0.30,0.50)},
}
for name, cuts in pols.items():
    t = R.table(R.simulate(df, cuts)); tot = t.iloc[0]
    per = " ".join(f"{r.slot}:{r.presisjon:.0%}/{r.dekning:.0%}" if (r.presisjon is not None and r.dekning is not None) else f"{r.slot}:–" for _, r in t.iloc[1:].iterrows())
    print(f"{name:55s} presisjon {tot['pres']:>8} ({tot['presisjon']:.0%})  dekning {tot['dek']:>8} ({tot['dekning']:.0%})  bad rushes warned {tot['rec']:>7} ({tot['recall_bad']:.0%})  pushes/day {tot['pushes_per_day']:.2f}  | {per}")
