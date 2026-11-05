# /// script
# requires-python = ">=3.13"
# dependencies = ["google-cloud-bigquery[pandas]", "db-dtypes", "scikit-learn"]
# ///
"""Print the comparison of our arrival heads with SIRI-ET's expected arrival, asked at the same T.

Reads the three exp_etcmp_* tables et_expected_eval.sql builds (read its header first: it carries
the method, the window and the 2026-10-23 numbers). Our side is calibrated here with the serving's
per-horizon Platt layers -- ML.WEIGHTS for flagged calls, nosx_platt_weights for the rest -- and its
point estimate is the serving's survival-curve median (predict_calls.sql, arr_delay_p50_stack_min).

Two arms of ours are printed: `ours` (the leak-free re-score) and `ours_built` (leadtime_scored, the
numbers the lead-time work published). Only `ours` is a fair opponent for ET.

Delays are whole minutes the way rail_calls makes them (TIMESTAMP_DIFF truncates) and clamped at 0
the way arr_delay_pos is; ET's expected delay is put through the same two steps. Cancelled calls
are left out of every time metric -- neither side predicts a time for them -- and get a line of
their own at the end.

    uv run data/queries/et_expected_eval.py
"""

import numpy as np
import pandas as pd
from google.cloud import bigquery
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

DS = "ent-smartavvik-dev.disruptions"
FIT_UNTIL = "2026-09-06"  # combination model: fit on the first week, test on the second

PULL = f"""
SELECT a.sid, a.jkey, a.stop_order, a.call_date, a.lead_min, a.has_situation,
       a.call_cancelled, a.arr_delay_pos, a.own_prev_delay, a.aimed_arr, a.actual_arr,
       s.p_arr5_raw, s.p_arr10_raw, s.p_arr20_raw, s.p_arr40_raw,
       f.f_arr5_raw, f.f_arr10_raw, f.f_arr20_raw, f.f_arr40_raw,
       e.et.rec AS et_rec, e.et.src AS et_src, e.et.exp_arr AS et_exp_arr,
       e.et.c_cancel AS et_c_cancel, e.et.j_cancel AS et_j_cancel, e.et.n_rec AS et_n_rec
FROM `{DS}.exp_etcmp_asks` AS a
JOIN `{DS}.exp_etcmp_fair_scored` AS f USING (sid, jkey, stop_order, call_date, lead_min)
JOIN (SELECT *, IFNULL(situationNumber, '') AS sid FROM `{DS}.leadtime_scored`) AS s
  USING (sid, jkey, stop_order, call_date, lead_min)
LEFT JOIN `{DS}.exp_etcmp_et_asof` AS e USING (sid, jkey, stop_order, call_date, lead_min)
"""

_LAYERS = {"30": "", "60": "_lead60", "120": "_lead120", "240": "_lead240"}
WEIGHTS = (
    "\nUNION ALL\n".join(
        f"SELECT 'arr_ge{t}' AS head, '{lb}' AS lead_bucket, TRUE AS has_situation,"
        " MAX(IF(processed_input = '__INTERCEPT__', weight, NULL)) AS b,"
        " MAX(IF(processed_input != '__INTERCEPT__', weight, NULL)) AS a"
        f" FROM ML.WEIGHTS(MODEL `{DS}.arr_ge{t}_platt{suffix}`)"
        for t in (5, 10, 20, 40)
        for lb, suffix in _LAYERS.items()
    )
    + f"\nUNION ALL\nSELECT head, lead_bucket, FALSE, b, a FROM `{DS}.nosx_platt_weights`"
    " WHERE head LIKE 'arr_ge%' AND lead_bucket != 'plan'"
)


def calibrate(df, w, prefix):
    """Calibrated P(arr >= 10) and the served median, from one arm's four raw heads."""
    layer = {(r.head, r.lead_bucket, bool(r.has_situation)): (r.a, r.b) for r in w.itertuples()}
    cal = {}
    for t in (5, 10, 20, 40):
        key = list(zip([f"arr_ge{t}"] * len(df), df.lead_min.astype(str), df.has_situation, strict=True))
        a = np.array([layer[k][0] for k in key])
        b = np.array([layer[k][1] for k in key])
        p = np.clip(df[f"{prefix}{t}_raw"].to_numpy(float), 1e-6, 1 - 1e-6)
        cal[t] = 1 / (1 + np.exp(-(b + a * np.log(p / (1 - p)))))
    # the serving's monotone survival curve and its median (predict_calls.sql, s5..s40)
    s40 = cal[40]
    s20 = np.maximum(cal[20], s40)
    s10 = np.maximum(cal[10], s20)
    s5 = np.maximum(cal[5], s10)
    p50 = np.select(
        [s5 <= 0.5, s10 <= 0.5, s20 <= 0.5, s40 <= 0.5],
        [
            np.floor(5 * np.log(0.5) / np.log(np.maximum(s5, 1e-6))),  # log-linear, floored
            5 + (s5 - 0.5) / np.maximum(s5 - s10, 0.001) * 5,
            10 + (s10 - 0.5) / np.maximum(s10 - s20, 0.001) * 10,
            20 + (s20 - 0.5) / np.maximum(s20 - s40, 0.001) * 20,
        ],
        np.minimum(40 * np.sqrt(s40 / 0.5), 90),
    )
    return s10, np.floor(p50 + 0.5)  # BigQuery ROUND: half away from zero


def prepare(df, w):
    for col in ("et_c_cancel", "et_j_cancel", "call_cancelled"):
        df[col] = df[col].astype("boolean").fillna(False).astype(bool)
    df["has_situation"] = df.has_situation.astype(bool)
    df["et_n_rec"] = df.et_n_rec.astype("Float64").fillna(0).astype(float)
    df["ours_p10"], df["ours_p50"] = calibrate(df, w, "f_arr")
    df["built_p10"], df["built_p50"] = calibrate(df, w, "p_arr")
    df["stratum"] = np.select(
        [df.et_rec.isna(), df.et_src == "rec", df.et_c_cancel | df.et_j_cancel, df.et_n_rec > 0],
        ["no ET yet", "already arrived", "ET: cancelled", "running"],
        "not started",
    )
    sec = (pd.to_datetime(df.et_exp_arr) - pd.to_datetime(df.aimed_arr)).dt.total_seconds()
    df["et_delay"] = np.clip(np.trunc(sec.fillna(0) / 60), 0, None)
    df.loc[df.stratum == "no ET yet", "et_delay"] = 0  # nothing published yet: the timetable
    df["persist"] = np.clip(df.own_prev_delay.astype(float), 0, None)  # known delay carried forward
    return df


def point_error(d):
    big = d[d.y10]
    out = {"n": len(d), "late>=10": int(d.y10.sum())}
    for name, col in (("timetable", None), ("persist", "persist"), ("ET", "et_delay"),
                      ("ours", "ours_p50"), ("ours_built", "built_p50")):
        pred = 0 if col is None else d[col]
        out[f"MAE {name}"] = np.mean(np.abs(pred - d.y))
    for name, col in (("ET", "et_delay"), ("ours", "ours_p50")):
        out[f"bias {name}"] = np.mean(d[col] - d.y)
        out[f"MAE|late>=10 {name}"] = np.mean(np.abs(big[col] - big.y))
    return pd.Series(out)


def detection(d):
    out = {f"AUC {n}": roc_auc_score(d.y10, d[c]) for n, c in
           (("ET", "et_delay"), ("persist", "persist"), ("ours", "ours_p10"), ("ours_built", "built_p10"))}
    et_alert = d.et_delay >= 10
    for name, alert in (("ET>=10", et_alert), ("ours>=0.5", d.ours_p10 >= 0.5),
                        ("ours>=0.65", d.ours_p10 >= 0.65), ("persist>=10", d.persist >= 10)):
        out[f"{name} alerts"] = int(alert.sum())
        out[f"{name} prec"] = d.y10[alert].mean() if alert.any() else np.nan
        out[f"{name} recall"] = alert[d.y10].mean()
    hits = int((et_alert & d.y10).sum())  # our precision when we catch as many as ET does
    if hits:
        found = np.cumsum(d.y10.to_numpy()[np.argsort(-d.ours_p10.to_numpy())])
        out["ours prec @ ET recall"] = hits / (np.searchsorted(found, hits) + 1)
    return pd.Series(out)


def combination(ev):
    p = np.clip(ev.ours_p10, 1e-6, 1 - 1e-6)
    ev = ev.assign(lo=np.log(p / (1 - p)), etd=np.minimum(ev.et_delay, 60),
                   et10=(ev.et_delay >= 10).astype(float), run=(ev.stratum == "running").astype(float))
    arms = {"+started": ["lo", "run"], "+ET times": ["lo", "etd", "et10"], "+both": ["lo", "etd", "et10", "run"]}
    rows = []
    for (hs, lead), d in ev.groupby(["has_situation", "lead_min"]):
        fit = pd.to_datetime(d.call_date) <= FIT_UNTIL
        tr, te = d[fit], d[~fit]
        row = {"flagged": hs, "lead": lead, "n_test": len(te),
               "ET": roc_auc_score(te.y10, te.et_delay), "ours": roc_auc_score(te.y10, te.ours_p10)}
        for name, cols in arms.items():
            m = LogisticRegression(max_iter=1000).fit(tr[cols], tr.y10)
            row[name] = roc_auc_score(te.y10, m.predict_proba(te[cols])[:, 1])
        rows.append(row)
    return pd.DataFrame(rows)


def main():
    client = bigquery.Client(project="ent-smartavvik-dev")
    df = prepare(client.query(PULL).to_dataframe(), client.query(WEIGHTS).to_dataframe())
    print(f"{len(df):,} asks")

    print("\n== What ET had at T (share of all asks, %) ==")
    shares = df.groupby(["has_situation", "lead_min"]).stratum.value_counts(normalize=True)
    print((shares.unstack().fillna(0) * 100).round(1).to_string())

    ev = df[~df.call_cancelled & df.arr_delay_pos.notna() & (df.stratum != "already arrived")]
    ev = ev.assign(y=ev.arr_delay_pos.astype(float))
    ev = ev.assign(y10=ev.y >= 10)
    print("\n== ET's expected delay is >= 1 min (share of evaluated asks, %) ==")
    print((ev.groupby(["has_situation", "lead_min"]).et_delay.apply(lambda s: (s >= 1).mean()) * 100)
          .round(1).to_string())

    running = ev[ev.stratum == "running"]
    err = (pd.to_datetime(running.et_exp_arr) - pd.to_datetime(running.actual_arr)).dt.total_seconds()
    print("\n== Join check: ET expected - actual arrival, running trains (s; ~-70..-140 is right) ==")
    print(err.groupby(running.lead_min).agg(["median", lambda x: x.abs().median()]).round(0).to_string())

    for hs, label in ((True, "SX-flagged"), (False, "no SX")):
        sub = ev[ev.has_situation == hs]
        print(f"\n==================== {label} ====================")
        print("-- point error, minutes --")
        print(sub.groupby("lead_min").apply(point_error).round(2).to_string())
        print("-- detecting arrival delay >= 10 min --")
        print(sub.groupby("lead_min").apply(detection).round(3).T.to_string())
        print("-- same, trains already running at T --")
        print(sub[sub.stratum == "running"].groupby("lead_min").apply(detection).round(3).T.to_string())

    print(f"\n== ET on top of ours: logistic fit to {FIT_UNTIL}, test AUC after ==")
    print(combination(ev).round(3).to_string(index=False))

    cancelled = df[df.call_cancelled]
    print("\n== Cancelled calls ET had already declared cancelled at T (%) ==")
    print((cancelled.groupby(["has_situation", "lead_min"]).stratum.apply(lambda s: (s == "ET: cancelled").mean())
           * 100).round(1).to_string())


if __name__ == "__main__":
    main()
