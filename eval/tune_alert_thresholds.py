# Tune the per-operator varsel alert thresholds (varsel.py _OPERATOR_ALERT_THRESHOLD).
#
# The alerting score is p_impact_dep = p_cancel + (1 - p_cancel) * p_dep_ge10, calibrated
# EXACTLY like live serving (predict_calls_bus.sql): operator-aware AND ask-horizon-aware
# platts read from disruptions.pooled_all_platt_weights by (head, lead_bucket, operator).
# The eval notebook's lead-time table used the base platts only; this script is the
# serving-matched version, which is what a deployed threshold actually sees.
#
# Data: disruptions.leadtime_scored_pooled_all — the pooled holdout re-scored at ask times
# L in {30, 60, 120, 240} min before departure plus "at message arrival" (lead_min = -1).
# Rows with L > 0 are kept only when the message existed by then (min_from_onset >= L),
# mirroring when varsel could actually have asked. All ask variants are pooled: varsel
# re-checks every poll cycle until departure, so each feasible ask point is a real
# alerting opportunity.
#
# Policy: pick the lowest threshold on a 0.05 grid whose pooled precision >= 0.90 (the bar
# the rail default 0.65 = ~91 % was set at), then sanity-check per-ask-variant stability.
# Keep the default instead when the evidence is thin (few impacted departures, or too few
# alerts at the pick to estimate precision) or the pick lands within one grid step of the
# default (not worth a per-operator override).
#
# Run:  uv run --with google-cloud-bigquery,pandas,db-dtypes python eval/tune_alert_thresholds.py
# Re-run after any pooled retrain / platt refit (rebuild pooled_all_platt_weights first).

import numpy as np
import pandas as pd
from google.cloud import bigquery

DEFAULT_THRESHOLD = 0.65
TARGET_PRECISION = 0.90
GRID = np.round(np.arange(0.30, 0.95, 0.05), 2)
MIN_IMPACTED = 200  # pooled positive rows below this -> too noisy to override the default
MIN_ALERTS = 50  # fewer alerts than this at the pick -> precision estimate is noise
MIN_STEPS = 2  # picks within one grid step of the default keep the default

QUERY = """
WITH s AS (
  SELECT operator, lead_min, min_from_onset,
         (call_cancelled OR COALESCE(dep_delay_pos, 0) >= 10) AS label_dep,
         IF(lead_min = -1, 'plan', CAST(lead_min AS STRING)) AS lead_bucket,
         p_cancel_raw, p_dep10_raw
  FROM `ent-smartavvik-dev.disruptions.leadtime_scored_pooled_all`
  WHERE NOT is_terminal
    AND (call_cancelled OR dep_delay_pos IS NOT NULL)
    AND (lead_min = -1 OR min_from_onset >= lead_min)
)
SELECT s.operator, s.lead_min, s.label_dep,
       ROUND(pc + (1 - pc) * pd, 4) AS p_impact_dep
FROM (
  SELECT s.*,
         1 / (1 + EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(s.p_cancel_raw, 1 - 1e-6), 1e-6)
                / (1 - GREATEST(LEAST(s.p_cancel_raw, 1 - 1e-6), 1e-6))) + IFNULL(wc.w, 0)))) AS pc,
         1 / (1 + EXP(-(wd.b + wd.a * LN(GREATEST(LEAST(s.p_dep10_raw, 1 - 1e-6), 1e-6)
                / (1 - GREATEST(LEAST(s.p_dep10_raw, 1 - 1e-6), 1e-6))) + IFNULL(wd.w, 0)))) AS pd
  FROM s
  JOIN `ent-smartavvik-dev.disruptions.pooled_all_platt_weights` AS wc
    ON wc.head = 'cancel' AND wc.lead_bucket = s.lead_bucket AND wc.operator = s.operator
  JOIN `ent-smartavvik-dev.disruptions.pooled_all_platt_weights` AS wd
    ON wd.head = 'dep_ge10' AND wd.lead_bucket = s.lead_bucket AND wd.operator = s.operator
) AS s
"""


def prec_rec(y: np.ndarray, p: np.ndarray, thr: float) -> tuple[float, float, int]:
    alert = p >= thr
    n_alert = int(alert.sum())
    if n_alert == 0:
        return float("nan"), 0.0, 0
    hit = int((y & alert).sum())
    return hit / n_alert, hit / int(y.sum()), n_alert


def main() -> None:
    df = bigquery.Client(project="ent-smartavvik-dev").query(QUERY).to_dataframe()
    df["y"] = df.label_dep.astype(bool)
    df["p"] = df.p_impact_dep.astype(float)
    print(f"{len(df):,} ask-variant rows, {df.operator.nunique()} operators\n")

    picks: dict[str, float] = {}
    rows = []
    for op, d in df.groupby("operator"):
        y, p = d.y.to_numpy(), d.p.to_numpy()
        n_pos = int(y.sum())
        base_prec, base_rec, _ = prec_rec(y, p, DEFAULT_THRESHOLD)
        grid = [(t, *prec_rec(y, p, t)) for t in GRID]
        ok = [(t, pr, rc, na) for t, pr, rc, na in grid if pr >= TARGET_PRECISION]
        pick = ok[0][0] if ok else float("nan")
        pick_alerts = ok[0][3] if ok else 0
        best_prec = max(
            (pr for _, pr, _, na in grid if pd.notna(pr) and na >= MIN_ALERTS), default=float("nan")
        )
        steps = round(abs(pick - DEFAULT_THRESHOLD) * 20) if pd.notna(pick) else 0  # 0.05 grid units
        reason = ""
        if n_pos < MIN_IMPACTED:
            final, reason = DEFAULT_THRESHOLD, f"too few impacted ({n_pos})"
        elif not ok:
            final, reason = DEFAULT_THRESHOLD, f"90% unreachable (best {best_prec:.2f})"
        elif pick_alerts < MIN_ALERTS:
            final, reason = DEFAULT_THRESHOLD, f"only {pick_alerts} alerts at pick"
        elif steps < MIN_STEPS:
            final, reason = DEFAULT_THRESHOLD, f"pick {pick:.2f} ~ default"
        else:
            final, reason = float(pick), "override"
            picks[op] = final
        f_prec, f_rec, f_alerts = prec_rec(y, p, final)
        rows.append({
            "operator": op, "impacted": n_pos,
            "prec@0.65": base_prec, "rec@0.65": base_rec,
            "pick@90%": pick, "final": final,
            "prec@final": f_prec, "rec@final": f_rec, "alerts@final": f_alerts,
            "why": reason,
        })

    out = pd.DataFrame(rows).sort_values("impacted", ascending=False)
    print(out.to_string(index=False, float_format=lambda v: f"{v:.3f}"))

    print("\nPer-ask-variant precision at the final thresholds (stability check):")
    for op, d in df.groupby("operator"):
        thr = picks.get(op, DEFAULT_THRESHOLD)
        parts = []
        for lead, v in d.groupby("lead_min"):
            pr, rc, na = prec_rec(v.y.to_numpy(), v.p.to_numpy(), thr)
            name = "melding" if lead == -1 else f"{lead}min"
            parts.append(f"{name} {pr:.2f}/{rc:.2f} (n={na})")
        print(f"  {op} @ {thr:.2f}: " + "; ".join(parts))

    print("\n_OPERATOR_ALERT_THRESHOLD = " + repr({k: picks[k] for k in sorted(picks)}))


if __name__ == "__main__":
    main()
