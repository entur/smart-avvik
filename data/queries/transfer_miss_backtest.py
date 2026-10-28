"""Backtest and fit of multi-leg KTD7's P(miss) (`trips.p_miss`) on historical Vy-to-Vy changes.

Reads the changes transfer_miss_backtest.sql builds and asks each one at lead L in {30, 60, 120, 240}
min before the arriving call, with what the serving would have said then:
  arriving call  -- the arrival heads re-scored LEAK-FREE (exp_etcmp6_fair_scored: et_expected_eval.sql
                    statements 1-2 run 2026-10-24 over Apr 1 - Oct 12 2026), calibrated as served
                    (the per-horizon Platt layers, then predict_calls.sql's monotone stack);
  connecting call -- the departure heads as leadtime_scored built them. Their only use is `_late_by`'s
                    shift, whose weight in the fit is ~0, so the leak there moves nothing.
A flagged row counts only once its situation exists at the ask (min_from_onset >= L); a flagged
arriving call with none is left out at that lead, a flagged connecting call with none shifts 0.
P(miss) is the shipped code's: `_read_off` at `connection_slack_min`'s slack with walk 0, then the
correction. Both are vectorized here and checked against `trips` first, so a script that drifts from
the code stops. Missed = actual arrival after actual departure, the labeller's rule with walk 0.

MEASURED 2026-10-28 (3 388 785 asks; flagged = an operator message names the arriving call; bar 0.65
flagged, 0.85 not). At 30 min, precision / recall at the bar:
  as first shipped (JP's 120 s taken off the slack, leadtime_scored's leaky heads): 0.70 / 0.55 and
    0.75 / 0.24 -- a figure of 0.65-0.85 was missed 39-43 % of the time, 0.85+ 75-82 %. The same with
    a 120 s walk. Two causes, both measured: the 120 s (kept in as margin, 0.76 / 0.51 and 0.82 /
    0.20), and the connecting train's own late departure, which saved 44 % of the arrivals that would
    have missed a punctual one.
  read-off with the margin kept, leak-free heads: 0.76 / 0.31 and 0.80 / 0.12.
  corrected, fit Apr 1 - Aug 15, tested Aug 16 - Oct 12: 0.84 / 0.27 and 0.87 / 0.08 (60 min: 0.78 /
    0.13 and 0.92 / 0.01); reliability on the diagonal within ~0.07. From Sep 18, Vy's reduced plan:
    0.79 and 0.90. Lead terms came out -0.04..-0.06 and left precision unchanged, so the correction
    carries none (the verdict has no ask time to give it).
The constants trips.py ships are the fit over the whole window, printed last. Refit after the arrival
heads are retrained, with exp_etcmp6_fair_scored rebuilt first. ~3.7 GB billed per run (~$0.02).

    cd disruptions/api && PYTHONPATH=. uv run --with pandas --with db-dtypes --with scikit-learn \
        --with google-cloud-bigquery-storage python ../../data/queries/transfer_miss_backtest.py
"""

import types
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd
import trips
from google.cloud import bigquery
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score

DS = "ent-kixavvik-dev.disruptions"
LEADS = (30, 60, 120, 240)
BAR = {True: 0.65, False: 0.85}  # alert_policy: Vy with an operator message, and without
SPLIT = date(2026, 8, 16)  # the out-of-time check: fit before, test from
KEY = ["jkey", "stop_order", "call_date", "lead_min"]

Q_PAIRS = f"""
SELECT call_date, a_jkey, a_stop_order, b_jkey, b_stop_order,
       TIMESTAMP_DIFF(aimed_dep, aimed_arr, SECOND) AS gap_s, actual_arr > actual_dep AS missed
FROM `{DS}.exp_transfer_pairs`
"""
_CALLS = f"""
  SELECT a_jkey AS jkey, a_stop_order AS stop_order, call_date FROM `{DS}.exp_transfer_pairs`
  UNION DISTINCT
  SELECT b_jkey, b_stop_order, call_date FROM `{DS}.exp_transfer_pairs`
"""
Q_SCORES = f"""
WITH calls AS ({_CALLS}),
onset AS (
  SELECT IFNULL(situationNumber, '') AS sid, jkey, stop_order, call_date, lead_min, min_from_onset
  FROM `{DS}.leadtime_eval_calls`
  WHERE call_date BETWEEN '2026-04-01' AND '2026-10-12' AND lead_min IN {LEADS}
)
SELECT IFNULL(s.situationNumber, '') AS sid, s.jkey, s.stop_order, s.call_date, s.lead_min,
       s.has_situation, o.min_from_onset,
       s.p_dep5_raw, s.p_dep10_raw, s.p_dep20_raw, s.p_dep40_raw,
       f.f_arr5_raw, f.f_arr10_raw, f.f_arr20_raw, f.f_arr40_raw
FROM `{DS}.leadtime_scored` AS s
JOIN calls USING (jkey, stop_order, call_date)
LEFT JOIN onset AS o
  ON o.sid = IFNULL(s.situationNumber, '') AND o.jkey = s.jkey AND o.stop_order = s.stop_order
 AND o.call_date = s.call_date AND o.lead_min = s.lead_min
LEFT JOIN `{DS}.exp_etcmp6_fair_scored` AS f
  ON f.sid = IFNULL(s.situationNumber, '') AND f.jkey = s.jkey AND f.stop_order = s.stop_order
 AND f.call_date = s.call_date AND f.lead_min = s.lead_min
WHERE s.call_date BETWEEN '2026-04-01' AND '2026-10-12' AND s.lead_min IN {LEADS}
"""
_LAYERS = {"30": "", "60": "_lead60", "120": "_lead120", "240": "_lead240"}
Q_WEIGHTS = (
    "\nUNION ALL\n".join(
        f"SELECT '{side}_ge{t}' AS head, '{lb}' AS lead_bucket, TRUE AS has_situation,"
        " MAX(IF(processed_input = '__INTERCEPT__', weight, NULL)) AS b,"
        " MAX(IF(processed_input != '__INTERCEPT__', weight, NULL)) AS a"
        f" FROM ML.WEIGHTS(MODEL `{DS}.{side}_ge{t}_platt{suffix}`)"
        for side in ("arr", "dep")
        for t in (5, 10, 20, 40)
        for lb, suffix in _LAYERS.items()
    )
    + f"\nUNION ALL\nSELECT head, lead_bucket, FALSE, b, a FROM `{DS}.nosx_platt_weights`"
    " WHERE head != 'cancel' AND lead_bucket != 'plan'"
)


def stack(df, w, side, prefix):
    """predict_calls.sql's calibrated, monotone survival points at 5, 10 and 20 min, as served."""
    cal = {}
    for t in (5, 10, 20, 40):
        p = np.clip(df[f"{prefix}{t}_raw"].to_numpy(float), 1e-6, 1 - 1e-6)
        a, b = np.full(len(df), np.nan), np.full(len(df), np.nan)
        for r in w[w["head"] == f"{side}_ge{t}"].itertuples():
            m = ((df.lead_min.astype(str) == r.lead_bucket) & (df.has_situation == r.has_situation)).to_numpy()
            a[m], b[m] = r.a, r.b
        cal[t] = 1 / (1 + np.exp(-(b + a * np.log(p / (1 - p)))))
    s20 = np.maximum(cal[20], cal[40])
    s10 = np.maximum(cal[10], s20)
    return np.round(np.maximum(cal[5], s10), 4), np.round(s10, 4), np.round(s20, 4)


def read_off(s5, s10, s20, slack):
    """trips._read_off with all three points present."""
    return np.select(
        [slack <= 5, slack < 10, slack < 20],
        [s5, s5 + (s10 - s5) * (slack - 5) / 5, s10 + (s20 - s10) * (slack - 10) / 10],
        s20,
    )


def late_by(d5, d10, d20):
    """trips._late_by."""
    return np.select([d20 >= 0.5, d10 >= 0.5, d5 >= 0.5], [20, 10, 5], 0)


def features(d):
    p = np.clip(d.read_off, 1e-4, 1 - 1e-4)
    return pd.DataFrame({"logit": np.log(p / (1 - p)), "flagged": d.flagged.astype(float),
                         "slack": d.slack})


def corrected(d, coef):
    """trips.p_miss's correction with (intercept, logit, flagged, slack) = `coef`."""
    z = coef[0] + features(d).to_numpy(float) @ np.asarray(coef[1:])
    return 1 / (1 + np.exp(-z))


SHIPPED = (trips._MISS_B, trips._MISS_A, trips._MISS_FLAGGED, trips._MISS_SLACK)


def check_against_trips(rng, n=20000):
    s = np.sort(rng.random((n, 3)), axis=1)[:, ::-1]  # s5 >= s10 >= s20
    slack = rng.uniform(-5, 40, n)
    flagged = rng.random(n) < 0.5
    mine = read_off(s[:, 0], s[:, 1], s[:, 2], slack)
    fixed = corrected(pd.DataFrame({"read_off": mine, "flagged": flagged, "slack": slack}), SHIPPED)
    for i in range(n):
        call = types.SimpleNamespace(p_arr_ge5=s[i, 0], p_arr_ge10=s[i, 1], p_arr_ge20=s[i, 2],
                                     has_situation=bool(flagged[i]))
        assert abs(trips._read_off(call, slack[i]) - mine[i]) < 1e-12, (s[i], slack[i])
        assert abs(trips.p_miss(call, slack[i]) - fixed[i]) < 1e-12, (s[i], slack[i])
        dep = types.SimpleNamespace(p_dep_ge5=s[i, 0], p_dep_ge10=s[i, 1], p_dep_ge20=s[i, 2])
        assert trips._late_by(dep) == late_by(s[i, 0], s[i, 1], s[i, 2]), s[i]
    at = datetime(2026, 10, 1, 8, tzinfo=UTC)
    for gap in (120, 300, 777, 1200):
        assert abs(trips.connection_slack_min(at, 0, at + timedelta(seconds=gap), []) - gap / 60) < 1e-12
    print(f"read-off, correction, shift and slack == trips' on {n:,} samples")


def asks(pairs, scores):
    """One row per (change, lead): the read-off (the highest over the arriving call's situations, as
    the verdict's `_best` takes it), the arriving call's population, the slack, the outcome."""
    shift = (
        scores.assign(late=late_by(scores.d5, scores.d10, scores.d20))
        .groupby(KEY, as_index=False).late.max()
        .rename(columns={"jkey": "b_jkey", "stop_order": "b_stop_order"})
    )
    arriving = scores[scores.f5.notna()][KEY + ["has_situation", "f5", "f10", "f20"]].rename(
        columns={"jkey": "a_jkey", "stop_order": "a_stop_order"})
    rows = pairs.merge(arriving, on=["a_jkey", "a_stop_order", "call_date"])
    rows = rows.merge(shift, on=["b_jkey", "b_stop_order", "call_date", "lead_min"], how="left")
    rows["slack"] = rows.gap_s / 60 + rows.late.fillna(0)  # walk 0; JP's 120 s stays in
    rows["read_off"] = read_off(rows.f5, rows.f10, rows.f20, rows.slack)
    return rows.groupby(["a_jkey", "a_stop_order", "call_date", "b_jkey", "lead_min"], as_index=False).agg(
        read_off=("read_off", "max"), flagged=("has_situation", "max"), slack=("slack", "first"),
        missed=("missed", "first"))


def summary(d, p):
    alert = d[p] >= d.bar
    n, m, al, tp = len(d), int(d.missed.sum()), int(alert.sum()), int((alert & d.missed).sum())
    return pd.Series({
        "asks": n, "missed": m, "mean_p": d[p].mean(), "alerts": al,
        "precision": tp / al if al else np.nan, "recall": tp / m if m else np.nan,
        "auc": roc_auc_score(d.missed, d[p]) if 0 < m < n else np.nan,
    })


def table(d, p):
    by = d.assign(bar=d.flagged.map(BAR)).groupby(["flagged", "lead_min"])
    return by.apply(summary, p, include_groups=False)


def reliability(d, p):
    g = d.groupby(pd.cut(d[p], [0, 0.05, 0.1, 0.2, 0.35, 0.5, 0.65, 0.85, 1.0001], right=False),
                  observed=True)
    return pd.DataFrame({"asks": g.size(), "mean_p": g[p].mean(), "missed_rate": g.missed.mean()})


def fit(d):
    m = LogisticRegression(max_iter=1000).fit(features(d).to_numpy(float), d.missed.astype(bool))
    return (m.intercept_[0], *m.coef_[0])


def main():
    check_against_trips(np.random.default_rng(7))
    bq = bigquery.Client(project="ent-kixavvik-dev")
    w = bq.query(Q_WEIGHTS).to_dataframe()
    w["has_situation"] = w.has_situation.astype(bool)
    pairs = bq.query(Q_PAIRS).to_dataframe()
    pairs["missed"] = pairs.missed.astype(bool)
    scores = bq.query(Q_SCORES).to_dataframe()
    scores["has_situation"] = scores.has_situation.astype(bool)
    scores = scores[~scores.has_situation | (scores.min_from_onset >= scores.lead_min)]
    scores["d5"], scores["d10"], scores["d20"] = stack(scores, w, "dep", "p_dep")
    fair = scores.f_arr5_raw.notna()
    scores["f5"] = scores["f10"] = scores["f20"] = np.nan
    f5, f10, f20 = stack(scores[fair], w, "arr", "f_arr")
    scores.loc[fair, "f5"], scores.loc[fair, "f10"], scores.loc[fair, "f20"] = f5, f10, f20

    d = asks(pairs, scores)
    d["shipped"] = corrected(d, SHIPPED)
    pd.set_option("display.width", 200, "display.max_columns", 20, "display.float_format", "{:.3f}".format)
    print(f"{len(pairs):,} changes, {len(d):,} asks")
    print("\n== the read-off alone, whole window ==")
    print(table(d, "read_off"))
    print("\n== P(miss) as shipped, whole window ==")
    print(table(d, "shipped"))

    train, test = d[d.call_date < SPLIT].copy(), d[d.call_date >= SPLIT].copy()
    test["oot"] = corrected(test, fit(train))
    print(f"\n== out of time: fit before {SPLIT}, tested from it ==")
    print(table(test, "oot"))
    for flagged in (True, False):
        print(f"-- reliability at 30 min, flagged={flagged} --")
        print(reliability(test[(test.flagged == flagged) & (test.lead_min == 30)], "oot"))

    whole = fit(d)
    print("\n== the whole window's fit, what trips.py ships (_MISS_B, _MISS_A, _MISS_FLAGGED, _MISS_SLACK) ==")
    print(", ".join(f"{c:.4f}" for c in whole), "  shipped:", ", ".join(f"{c:.4f}" for c in SHIPPED))


if __name__ == "__main__":
    main()
