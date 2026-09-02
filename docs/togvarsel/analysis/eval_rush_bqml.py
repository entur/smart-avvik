import numpy as np, pandas as pd
from sklearn.metrics import roc_auc_score, log_loss
import rushsim as R
RNG = np.random.default_rng(3)
b = pd.read_csv("bq_roll_pred.csv", parse_dates=["d"]); b["label"] = b.label.astype(str).str.lower().eq("true")
tr = pd.read_csv("training.csv", parse_dates=["d"])[["d","am_t10","am_t10_rest0700","pm_t10","y_am10","y_amrest10","y_pm10"]]
for c in ("y_am10","y_amrest10","y_pm10"): tr[c] = tr[c].astype(str).str.lower().eq("true")
MODELS = ["am0630","am0700","pm1200","pm1400"]
def boot(pa, pb, y, B=1000):
    idx_all = np.arange(len(y)); out = []
    for _ in range(B):
        i = RNG.choice(idx_all, len(y), replace=True)
        if y[i].sum() in (0, len(i)): continue
        out.append(roc_auc_score(y[i], pb[i]) - roc_auc_score(y[i], pa[i]))
    return np.percentile(out, 2.5), np.percentile(out, 97.5)
print("## AUC per variant (rolling OOS, BQML)")
print("| variant | model | n | AUC | log-loss | ΔAUC vs baseline [95 % CI] | green n→bad | yellow n→bad | red n→bad (p̄) |")
print("|---|---|---|---|---|---|---|---|---|")
for mname in MODELS:
    base = b[(b.variant=="baseline")&(b.mname==mname)].set_index("d")
    for variant in ["baseline","l2x10","l2x30","pmmin"]:
        x = b[(b.variant==variant)&(b.mname==mname)].set_index("d").sort_index()
        if len(x)==0: continue
        y = x.label.values.astype(int); p = x.p.values
        common = x.index.intersection(base.index)
        yb = base.loc[common].label.values.astype(int); pb = base.loc[common].p.values; px = x.loc[common].p.values
        d = "" if variant=="baseline" else "%+.3f [%+.3f, %+.3f]" % ((roc_auc_score(yb, px) - roc_auc_score(yb, pb)), *boot(pb, px, yb))
        cal = []
        for lo, hi in [(0,.1),(.1,.3),(.3,1.01)]:
            s = x[(x.p>=lo)&(x.p<hi)]; cal.append(f"{len(s)} → {s.label.mean():.0%} (p̄ {s.p.mean():.2f})" if len(s) else "0")
        print(f"| {variant} | {mname} | {len(x)} | {roc_auc_score(y,p):.3f} | {log_loss(y,p):.3f} | {d} | " + " | ".join(cal) + " |")
# seasonal split, baseline
print("\n## baseline OOS AUC by half-year")
for mname in MODELS:
    x = b[(b.variant=="baseline")&(b.mname==mname)]
    for name, m in [("Oct25–Mar26", (x.d >= "2025-10-01") & (x.d < "2026-04-01")), ("Apr–Aug26", x.d >= "2026-04-01")]:
        s = x[m]; print(f"{mname} {name}: n={len(s)} bad={s.label.mean():.2f} auc={roc_auc_score(s.label.astype(int), s.p):.3f}")
def wide(variant_by_model):
    w = tr.copy()
    for mname, variant in variant_by_model.items():
        x = b[(b.variant==variant)&(b.mname==mname)][["d","p"]].rename(columns={"p": f"p_{mname[2:]}"})
        w = w.merge(x, on="d", how="inner")
    return w.sort_values("d").reset_index(drop=True)
def rates(w, cuts, title):
    sim = R.simulate(w, cuts); t = R.table(sim); warn = sim[sim.sent & (sim.level != "green")]; allc = sim[sim.sent & (sim.level=="green")]
    tot = t.iloc[0]
    per = " ".join(f"{r.slot} {r.presisjon:.0%}" if r.presisjon is not None else f"{r.slot} –" for _, r in t.iloc[1:].iterrows())
    print(f"| {title} | {tot['presisjon']:.0%} ({tot['pres']}) | {warn.justified.mean():.0%} ({int(warn.justified.sum())}/{len(warn)}) | {tot['dekning']:.0%} | {tot['recall_bad']:.0%} ({tot['rec']}) | {tot['pushes_per_day']:.2f} | {tot['exp_presisjon']:.0%} | {per} |")
    return sim
w = wide({m: "baseline" for m in MODELS})
print(f"\n## page rates on the BQML baseline: OOS {w.d.min().date()}..{w.d.max().date()} n={len(w)}")
print("| policy | Presisjon (all pushes) | warnings only | Dekning | bad rushes warned | pushes/day | expected Presisjon | per slot |")
print("|---|---|---|---|---|---|---|---|")
rates(w, None, "production 0.10 / 0.30")
rates(w, {s: (0.15, 0.30) for s in R.SLOTS}, "yellow from 0.15")
rates(w, {s: (0.20, 0.30) for s in R.SLOTS}, "yellow from 0.20")
rates(w, {s: (0.30, 1.01) for s in R.SLOTS}, "red only (p ≥ 0.30)")
rates(w, {"0630": (0.30,1.01), "0700": (0.30,1.01), "1200": (1.01,1.02), "1400": (1.01,1.02)}, "red only, morning issues only")
rates(w, {s: (0.40, 1.01) for s in R.SLOTS}, "push at p ≥ 0.40")
rates(w, {s: (0.50, 1.01) for s in R.SLOTS}, "push at p ≥ 0.50")
rates(w, {"0630": (0.10,0.30), "0700": (0.10,0.30), "1200": (0.30,1.01), "1400": (0.20,0.30)}, "AM as production, 12:00 red only, 14:00 from 0.20")
print("\n## windows, production policy, BQML baseline")
maxd = w.d.max()
for n in (14, 30, 90, 180):
    t = R.stats(R.simulate(w[w.d > maxd - pd.Timedelta(days=n)]))
    print(f"last {n:3d} d: presisjon {t['pres']:>7} ({t['presisjon']:.0%})  dekning {t['dek']:>7}  bad rushes warned {t['rec']}  pushes/day {t['pushes_per_day']:.2f}  expected {t['exp_presisjon']:.0%}")
print("\n## red-only by half-year, BQML baseline")
for name, m in [("Oct25–Mar26", (w.d >= "2025-10-01") & (w.d < "2026-04-01")), ("Apr–Aug26", w.d >= "2026-04-01")]:
    sim = R.simulate(w[m], {s: (0.30, 1.01) for s in R.SLOTS}); t = R.stats(sim); warn = sim[sim.sent & (sim.level != "green")]
    print(f"{name}: n={m.sum()} presisjon {t['pres']} ({t['presisjon']:.0%}) warnings-only {int(warn.justified.sum())}/{len(warn)} ({warn.justified.mean():.0%}) bad rushes warned {t['rec']} pushes/day {t['pushes_per_day']:.2f}")
    simp = R.simulate(w[m]); tp = R.stats(simp); print(f"   production: presisjon {tp['pres']} ({tp['presisjon']:.0%}) bad rushes warned {tp['rec']} pushes/day {tp['pushes_per_day']:.2f}")
# best-variant combo
w2 = wide({"am0630": "l2x10", "am0700": "l2x10", "pm1200": "pmmin", "pm1400": "pmmin"})
print(f"\n## combo l2x10 (AM) + pmmin (PM): n={len(w2)}")
print("| policy | Presisjon (all pushes) | warnings only | Dekning | bad rushes warned | pushes/day | expected Presisjon | per slot |")
print("|---|---|---|---|---|---|---|---|")
rates(w2, None, "production 0.10 / 0.30")
rates(w2, {s: (0.30, 1.01) for s in R.SLOTS}, "red only (p ≥ 0.30)")
w.to_csv("oos_bqml_baseline.csv", index=False)
