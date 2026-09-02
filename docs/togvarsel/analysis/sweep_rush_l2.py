"""Regularisation grid + minimal PM sets, rolling OOS, paired bootstrap vs production C."""
from sweep_rush_lib import *
print()
for mname, base in BASE.items():
    label = LABEL[mname]; y = df[label].values; C0 = 1.0 / L2[mname]
    p0 = roll(base, label, lambda: lr(C0)); m0 = metrics(p0, y)
    print(f"##### {mname}: production C={C0:.3f} auc={m0['auc']:.3f} ll={m0['ll']:.3f}")
    for Cv in (0.003, 0.01, 0.02, 0.03, 0.05, 0.1):
        p = roll(base, label, lambda: lr(Cv)); m = metrics(p, y); lo, hi = paired_boot(p0, p, y, 400)
        print(f"   C={Cv:<6} auc={m['auc']:.3f} ({m['auc']-m0['auc']:+.3f} CI[{lo:+.3f},{hi:+.3f}]) ll={m['ll']:.3f} prec@0.10={m['prec@0.10']:.2f} push@0.10={m['push@0.10']:.2f} prec@0.30={m['prec@0.30']:.2f} push@0.30={m['push@0.30']:.2f} prec_top20={m['prec_top20']:.2f}")
# minimal PM sets
MIN = {
 "pm1200": ['et_t5_last_1200','et_cx_1200','prev_pm_t5','low_season','dow_6','am_t10_known'],
 "pm1400": ['et_t5_last_1400','et_cx_1400','et_t5_1200_1350','prev_pm_t5','low_season','dow_6','am_t10_known','sx_pmj_inn_1400'],
}
for mname, cols in MIN.items():
    label = LABEL[mname]; y = df[label].values; C0 = 1.0 / L2[mname]
    p0 = roll(BASE[mname], label, lambda: lr(C0)); m0 = metrics(p0, y)
    for Cv in (C0, 0.03):
        p = roll(cols, label, lambda: lr(Cv)); m = metrics(p, y); lo, hi = paired_boot(p0, p, y, 400)
        print(f"{mname} minimal({len(cols)}) C={Cv:.3f}: auc={m['auc']:.3f} ({m['auc']-m0['auc']:+.3f} CI[{lo:+.3f},{hi:+.3f}]) ll={m['ll']:.3f} prec@0.10={m['prec@0.10']:.2f} push@0.10={m['push@0.10']:.2f}")
# seasonal split of the production recipe OOS
print("\n##### production recipe OOS AUC by half-year")
for mname, base in BASE.items():
    label = LABEL[mname]; y = df[label].values; p = roll(base, label, lambda: lr(1.0 / L2[mname]))
    for name, mask in [("Oct25-Mar26", (df.d >= "2025-10-01") & (df.d < "2026-04-01")), ("Apr26-Aug26", df.d >= "2026-04-01")]:
        mm = mask.values & ~np.isnan(p)
        print(f"   {mname} {name}: n={mm.sum()} bad={y[mm].mean():.2f} auc={roc_auc_score(y[mm], p[mm]):.3f}")
