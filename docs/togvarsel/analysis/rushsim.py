"""Replays togvarsel_notify.decide_issue + rushhistorikk_verdicts over per-day predictions.

Input: a DataFrame with d, y_am10, y_amrest10, y_pm10, am_t10, am_t10_rest0700, pm_t10 and
probabilities p_0630, p_0700, p_1200, p_1400. Output: the stats table the page shows
(Presisjon / Dekning per slot + total, pushes, matrix), under a chosen level cut.
"""
import numpy as np, pandas as pd

BAR = {"0630": 0.10, "0700": 0.10, "1200": 0.15, "1400": 0.15}
SCOPE = {"0630": "am_t10", "0700": "am_t10_rest0700", "1200": "pm_t10", "1400": "pm_t10"}
LABEL = {"0630": "y_am10", "0700": "y_amrest10", "1200": "y_pm10", "1400": "y_pm10"}
BASE = {"0700": "0630", "1400": "1200"}
RUSH = {"0630": "am", "0700": "am", "1200": "pm", "1400": "pm"}
SLOTS = ["0630", "0700", "1200", "1400"]

def level(p, green_max=0.10, red_min=0.30):
    return np.where(p < green_max, "green", np.where(p < red_min, "yellow", "red"))

def realized(t, bar):
    return np.where(t < bar, "green", np.where(t < 2 * bar, "yellow", "red"))

def simulate(df, cuts=None, push_levels=("yellow", "red")):
    """cuts: {slot: (green_max, red_min)}; default production 0.10/0.30 everywhere.
    push_levels: the levels a push can SAY besides green — the launch rule ("yellow", "red") or
    the 2026-09-02 red-only rule ("red",), mirroring togvarsel_notify.decide_issue: a baseline
    pushes iff its level is one of them; a follow-up pushes iff its level differs from the
    effective baseline AND is sayable (a fresh warning with nothing standing, an all_clear at green
    after a pushed baseline, and under the launch rule the yellow<->red update).
    Returns a long DataFrame: one row per (d, slot) with level, sent, kind, base, actual, justified, due."""
    cuts = cuts or {}
    push_levels = list(push_levels)
    rows = []
    lv = {}
    for s in SLOTS:
        g, r = cuts.get(s, (0.10, 0.30))
        lv[s] = level(df[f"p_{s}"].values, g, r)
    for s in SLOTS:
        lab = df[LABEL[s]].values.astype(bool)
        act = realized(df[SCOPE[s]].values, BAR[s])
        L = lv[s]
        if s in BASE:
            b = BASE[s]
            base_pushed = np.isin(lv[b], push_levels)
            base = np.where(base_pushed, lv[b], "green")
            # a follow-up pushes on a level change it can SAY: a push-level forecast with nothing
            # standing (warning), a green after a pushed baseline (all_clear); under the launch rule
            # yellow<->red against a pushed baseline was an "update", under red-only yellow never pushes
            sayable = np.isin(L, push_levels) | (L == "green")
            sent = (L != base) & sayable & ~((base == "green") & (L == "green"))
            kind = np.where(L == "green", "all_clear", np.where(base == "green", "warning", "update"))
            standing = np.isin(base, ["yellow", "red"])
        else:
            base = np.array([None] * len(df))
            sent = np.isin(L, push_levels)
            kind = np.where(sent, "warning", None)
            standing = np.zeros(len(df), bool)
        justified = (L != "green") == lab
        due = standing != lab
        rows.append(pd.DataFrame({"d": df["d"].values, "slot": s, "rush": RUSH[s], "p": df[f"p_{s}"].values,
                                  "level": L, "sent": sent, "kind": np.where(sent, kind, None), "base": base,
                                  "actual": act, "label": lab, "justified": justified, "due": due}))
    return pd.concat(rows, ignore_index=True)

def stats(sim, slot=None):
    x = sim if slot is None else sim[sim.slot == slot]
    pushes = x[x.sent]
    pres_num, pres_den = int(pushes.justified.sum()), len(pushes)
    owed = x[x.due]
    if slot is None:
        g = owed.groupby(["d", "rush"])
        dek_den = g.ngroups
        dek_num = int(g.apply(lambda grp: bool((grp.sent & grp.justified).any())).sum()) if dek_den else 0
    else:
        dek_den = len(owed)
        dek_num = int((owed.sent & owed.justified).sum())
    n_days = x.d.nunique()
    # traveller-centric recall: of the BAD rushes (label true at the rush's own issues), the share where
    # a yellow/red warning was pushed at either issue (baseline warning, or follow-up warning/update)
    bad = x[x.label]
    warned = bad[bad.sent & (bad.level != "green")]
    if slot is None:
        rec_den = bad.groupby(["d", "rush"]).ngroups; rec_num = warned.groupby(["d", "rush"]).ngroups
    else:
        rec_den = len(bad); rec_num = len(warned)
    return dict(recall_bad=rec_num / rec_den if rec_den else None, rec=f"{rec_num}/{rec_den}",slot=slot or "total", presisjon=pres_num / pres_den if pres_den else None, pres=f"{pres_num}/{pres_den}",
                dekning=dek_num / dek_den if dek_den else None, dek=f"{dek_num}/{dek_den}",
                pushes=pres_den, pushes_per_day=pres_den / n_days if n_days else None, n_days=n_days,
                exp_presisjon=float(np.where(pushes.level == "green", 1 - pushes.p, pushes.p).mean()) if pres_den else None)

def table(sim):
    return pd.DataFrame([stats(sim)] + [stats(sim, s) for s in SLOTS])

def matrix(sim, slot=None):
    x = sim if slot is None else sim[sim.slot == slot]
    return pd.crosstab(x.level, x.actual).reindex(index=["green", "yellow", "red"], columns=["green", "yellow", "red"], fill_value=0)

def fmt(t):
    out = t.copy()
    for c in ("presisjon", "dekning", "exp_presisjon", "recall_bad"):
        out[c] = out[c].map(lambda v: "–" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:.0%}")
    out["pushes_per_day"] = out["pushes_per_day"].map(lambda v: f"{v:.2f}")
    return out.to_string(index=False)

if __name__ == "__main__":
    import sys
    df = pd.read_csv(sys.argv[1], parse_dates=["d"])
    for c in ("y_am10", "y_amrest10", "y_pm10"):
        df[c] = df[c].astype(str).str.lower().eq("true")
    df = df.sort_values("d").reset_index(drop=True)
    maxd = df.d.max()
    print(f"days {df.d.min().date()}..{maxd.date()}  n={len(df)}  base rates am10={df.y_am10.mean():.2f} amrest10={df.y_amrest10.mean():.2f} pm10={df.y_pm10.mean():.2f}")
    sim_all = simulate(df)
    for name, mask in [("last 14 d", df.d > maxd - pd.Timedelta(days=14)), ("last 30 d", df.d > maxd - pd.Timedelta(days=30)),
                       ("last 90 d", df.d > maxd - pd.Timedelta(days=90)), ("last 180 d", df.d > maxd - pd.Timedelta(days=180)),
                       ("all (Apr 2025–Aug 2026)", df.d > pd.Timestamp("2000-01-01"))]:
        sub = df[mask]
        sim = simulate(sub)
        print(f"\n=== {name}: {sub.d.min().date()}..{sub.d.max().date()} ({len(sub)} weekdays) ===")
        print(fmt(table(sim)))
    print("\n=== by quarter (production cuts) ===")
    q = df.d.dt.to_period("Q")
    for per, sub in df.groupby(q):
        t = stats(simulate(sub))
        print(f"{per}: n={len(sub):3d}  presisjon {t['pres']:>7} = {t['presisjon'] if t['presisjon'] is None else round(t['presisjon'],2)}   dekning {t['dek']:>7}   pushes/day {t['pushes_per_day']:.2f}   exp {t['exp_presisjon']:.2f}")
    print("\n=== calibration of the LIVE models in-sample: observed bad rate per forecast level ===")
    for s in SLOTS:
        x = sim_all[sim_all.slot == s]
        g = x.groupby("level").agg(n=("label", "size"), bad=("label", "mean"), mean_p=("p", "mean")).reindex(["green", "yellow", "red"])
        print(s, " ".join(f"{lvl}: n={int(r.n) if not np.isnan(r.n) else 0} bad={r.bad:.2f} p̄={r.mean_p:.2f}" for lvl, r in g.iterrows()))
    print("\n=== matrix (all, total) ===")
    print(matrix(sim_all))
    print("\n=== alternative cuts (all days, in-sample): green_max varied, red_min 0.30 unless noted ===")
    for g, r in [(0.10, 0.30), (0.15, 0.30), (0.20, 0.30), (0.25, 0.30), (0.30, 0.50), (0.30, 0.60)]:
        cuts = {s: (g, r) for s in SLOTS}
        t = table(simulate(df, cuts))
        tot = t.iloc[0]
        print(f"green<{g:.2f} red>={r:.2f}: presisjon {tot['pres']:>8} ({tot['presisjon']:.0%})  dekning {tot['dek']:>7} ({tot['dekning']:.0%})  pushes/day {tot['pushes_per_day']:.2f}  | per slot pres: " +
              " ".join(f"{row.slot}={row.presisjon:.0%}" if row.presisjon is not None else f"{row.slot}=–" for _, row in t.iloc[1:].iterrows()))
