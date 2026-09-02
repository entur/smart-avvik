"""Rolling-origin sweep of candidate feature additions with placebo arms (2026-09-02).
Run from a directory holding training.csv + features3.csv (see sweep_rush_lib.py)."""
from sweep_rush_lib import *

RESULTS = {}
OOS = {}
for mname, base in BASE.items():
    label = LABEL[mname]; y = df[label].values
    C = 1.0 / L2[mname]  # BQML l2_reg ~ 1/C on standardized features (study: C=0.1 <-> strong penalty)
    print(f"\n##### {mname} (label {label}, base rate {y.mean():.2f}, C={C:.3f}) — OOS Oct 2025..Aug 2026")
    p_base = roll(base, label, lambda: lr(C)); mb = metrics(p_base, y); OOS[(mname, "base")] = p_base
    rows = [dict(variant="base (production list)", **mb, net=0.0, z=np.nan, ci="")]
    # regularisation variants on the base set
    for Cv in (0.03, 0.3, 1.0):
        p = roll(base, label, lambda: lr(Cv)); m = metrics(p, y); lo, hi = paired_boot(p_base, p, y, 300)
        rows.append(dict(variant=f"base, C={Cv}", **m, net=m["auc"] - mb["auc"], z=np.nan, ci=f"[{lo:+.3f},{hi:+.3f}]"))
    p = roll(base, label, lambda: HistGradientBoostingClassifier(max_depth=2, learning_rate=0.05, max_iter=150, min_samples_leaf=10, l2_regularization=1.0))
    m = metrics(p, y); lo, hi = paired_boot(p_base, p, y, 300)
    rows.append(dict(variant="base, HistGB depth2", **m, net=m["auc"] - mb["auc"], z=np.nan, ci=f"[{lo:+.3f},{hi:+.3f}]"))
    all_add = []
    for name, add in ADD[mname].items():
        all_add += add
        p = roll(base + add, label, lambda: lr(C)); m = metrics(p, y); OOS[(mname, name)] = p
        pm, ps = placebo(base, add, label, C); lo, hi = paired_boot(p_base, p, y, 300)
        rows.append(dict(variant=f"+ {name} ({len(add)})", **m, net=m["auc"] - pm, z=(m["auc"] - pm) / ps if ps > 0 else np.nan, ci=f"[{lo:+.3f},{hi:+.3f}]"))
    p = roll(base + all_add, label, lambda: lr(C)); m = metrics(p, y); OOS[(mname, "all")] = p
    pm, ps = placebo(base, all_add, label, C, n_perm=10); lo, hi = paired_boot(p_base, p, y, 300)
    rows.append(dict(variant=f"+ all ({len(all_add)})", **m, net=m["auc"] - pm, z=(m["auc"] - pm) / ps if ps > 0 else np.nan, ci=f"[{lo:+.3f},{hi:+.3f}]"))
    R = pd.DataFrame(rows)
    RESULTS[mname] = R
    with pd.option_context("display.width", 250, "display.max_columns", 30, "display.float_format", "{:.3f}".format):
        print(R[["variant","n","auc","net","z","ci","ll","prec@0.10","push@0.10","prec@0.30","push@0.30","prec_top20","rec_top20","prec_top40","rec_top40"]].to_string(index=False))
    print(f"[{time.time()-T0:.0f}s]")

# calibration OOS of the base recipe per level
print("\n##### OOS calibration of the production recipe (rolling), observed bad rate per level")
for mname in BASE:
    p = OOS[(mname, "base")]; y = df[LABEL[mname]].values; m = ~np.isnan(p)
    lv = np.where(p[m] < 0.10, "green", np.where(p[m] < 0.30, "yellow", "red"))
    s = pd.DataFrame({"lv": lv, "y": y[m], "p": p[m]}).groupby("lv").agg(n=("y","size"), bad=("y","mean"), pbar=("p","mean")).reindex(["green","yellow","red"])
    print(mname, " | ".join(f"{i}: n={int(r.n) if not np.isnan(r.n) else 0} bad={r.bad:.2f} p̄={r.pbar:.2f}" for i, r in s.iterrows()))

# save OOS predictions for the policy simulator
out = df[["d","dow","low_season","am_t10","am_t10_rest0700","pm_t10","y_am10","y_amrest10","y_pm10"]].copy()
for (mname, var), p in OOS.items():
    out[f"p_{mname[2:]}__{var}"] = p
out.to_csv("oos_local.csv", index=False)
print("saved oos_local.csv", f"[{time.time()-T0:.0f}s]")
