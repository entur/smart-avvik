import marimo

__generated_with = "0.23.13"
app = marimo.App(width="medium")


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # Pooled buss-modeller — evaluering

    **Kort fortalt:** den all-operatør-poolede modellfamilien (`*_pooled_all`, 13 operatører:
    NSB/GOA på skinner + KOL, SOF, NOR, SKY, ATB, OST, FIN, MOR, RUT, TRO, GCO på buss/lokal)
    evaluert per operatør på det temporale holdout-settet (`is_eval = TRUE`, onset ≥ 2026-04-01)
    i `disruptions.training_calls_pooled_all`.

    Skåringen speiler `data/queries/eval_pooled_all_models.sql`: rå `ML.PREDICT`-sannsynligheter
    rekalibreres med de **operatør-bevisste** platt-lagene (`sigmoid(b + a·logit + w[operatør])`)
    og settes sammen som `p_impact = p_cancel + (1 − p_cancel) · P(≥10)`.

    Hovedfunn fra byggingen (docs/pooled-models-bus/LEARNINGS.md): buss-operatørene får sterke
    modeller (AUC 0.88–0.997, presisjon 0.84–0.96 @ 0.65), men NSB taper 0.4–0.5 AUC-poeng på
    fortynning — paritets-gaten FEILET, så skinner og buss bør serveres fra hver sin familie.
    """)
    return


@app.cell
def imports():
    import marimo as mo
    import numpy as np
    import pandas as pd
    import seaborn as sns
    import matplotlib.pyplot as plt
    from google.cloud import bigquery

    return bigquery, mo, np, pd, plt


@app.cell
def bq_client(bigquery):
    client = bigquery.Client(project="ent-kixavvik-dev")
    return (client,)


@app.cell
def score_holdout(client):
    # Radnivå-skåring av pooled_all-familien på holdout (samme pipeline som
    # eval_pooled_all_models.sql, men returnerer rader i stedet for aggregater).
    _q = """
    WITH ev AS (
      SELECT * FROM `ent-kixavvik-dev.disruptions.training_calls_pooled_all` WHERE is_eval
    ),
    clf AS (
      SELECT operator, situationNumber, jkey, stop_order, call_cancelled,
             is_origin, is_terminal, dep_delay_pos, arr_delay_pos,
             (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf_pooled_all`,
                      (SELECT * FROM ev))
    ),
    a10 AS (
      SELECT operator, situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf_pooled_all`,
                      (SELECT * FROM ev))
    ),
    d10 AS (
      SELECT operator, situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf_pooled_all`,
                      (SELECT * FROM ev))
    ),
    wc AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input='raw_logit', weight, NULL))     AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt_pooled_all`)
    ),
    wc_op AS (
      SELECT cw.category AS operator, cw.weight AS w
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt_pooled_all`),
           UNNEST(category_weights) AS cw
      WHERE processed_input = 'operator'
    ),
    wa AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input='raw_logit', weight, NULL))     AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_pooled_all`)
    ),
    wa_op AS (
      SELECT cw.category AS operator, cw.weight AS w
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_pooled_all`),
           UNNEST(category_weights) AS cw
      WHERE processed_input = 'operator'
    ),
    wd AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input='raw_logit', weight, NULL))     AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_pooled_all`)
    ),
    wd_op AS (
      SELECT cw.category AS operator, cw.weight AS w
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_pooled_all`),
           UNNEST(category_weights) AS cw
      WHERE processed_input = 'operator'
    )
    SELECT
      c.operator,
      c.call_cancelled,
      (NOT c.is_origin   AND (c.call_cancelled OR c.arr_delay_pos IS NOT NULL)) AS eligible_arr,
      (NOT c.is_terminal AND (c.call_cancelled OR c.dep_delay_pos IS NOT NULL)) AS eligible_dep,
      (c.call_cancelled OR COALESCE(c.arr_delay_pos, 0) >= 10) AS label_arr,
      (c.call_cancelled OR COALESCE(c.dep_delay_pos, 0) >= 10) AS label_dep,
      1/(1+EXP(-(wc.b + IFNULL(oc.w,0) + wc.a * LN(GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)))))) AS p_cancel,
      1/(1+EXP(-(wa.b + IFNULL(oa.w,0) + wa.a * LN(GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)))))) AS p_a10,
      1/(1+EXP(-(wd.b + IFNULL(od.w,0) + wd.a * LN(GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)))))) AS p_d10
    FROM clf AS c
    JOIN a10 AS a USING (operator, situationNumber, jkey, stop_order)
    JOIN d10 AS d USING (operator, situationNumber, jkey, stop_order)
    CROSS JOIN wc CROSS JOIN wa CROSS JOIN wd
    LEFT JOIN wc_op AS oc ON oc.operator = c.operator
    LEFT JOIN wa_op AS oa ON oa.operator = c.operator
    LEFT JOIN wd_op AS od ON od.operator = c.operator
    """
    scored = client.query(_q).to_dataframe()
    scored["p_impact_arr"] = scored["p_cancel"] + (1 - scored["p_cancel"]) * scored["p_a10"]
    scored["p_impact_dep"] = scored["p_cancel"] + (1 - scored["p_cancel"]) * scored["p_d10"]
    scored.groupby("operator").size().rename("holdout-rader").to_frame()
    return (scored,)


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Per-operatør-metrikker (thr = 0.65)

    Samme tall som `eval_pooled_all_models.sql`, regnet i pandas fra `scored`.
    `cancel` = P(innstilt) mot faktisk innstilling; `impact_*` = sammensatt
    p_impact mot innstilt ELLER ≥10 min forsinkelse, på kvalifiserte stopp.
    """)
    return


@app.cell
def _(np, pd):
    THR = 0.65


    def auc_score(y, p):
        """Rank-statistic (Mann-Whitney) AUC with tie handling (same as impact_eval.py)."""
        _r = pd.Series(p).rank(method="average").to_numpy()
        _pos = int(y.sum())
        _neg = len(y) - _pos
        return (_r[y].sum() - _pos * (_pos + 1) / 2) / (_pos * _neg)


    def pr_curve(y, p):
        """(thresholds, precision, recall) over descending scores, one point per distinct score."""
        _ord = np.argsort(-p, kind="mergesort")
        _y = y[_ord].astype(float)
        _p = p[_ord]
        _tp = np.cumsum(_y)
        _prec = _tp / np.arange(1, len(_y) + 1)
        _rec = _tp / _y.sum()
        _keep = np.r_[np.diff(_p) != 0, True]
        return _p[_keep], _prec[_keep], _rec[_keep]


    def metric_slice(df, metric):
        """(y, p) arrays for one of the three eval metrics, on its eligible rows."""
        if metric == "cancel":
            _sub, _ycol, _pcol = df, "call_cancelled", "p_cancel"
        elif metric == "impact_arr":
            _sub, _ycol, _pcol = df[df["eligible_arr"].astype(bool)], "label_arr", "p_impact_arr"
        else:
            _sub, _ycol, _pcol = df[df["eligible_dep"].astype(bool)], "label_dep", "p_impact_dep"
        return _sub[_ycol].to_numpy(dtype=bool), _sub[_pcol].to_numpy(dtype=float)

    return THR, auc_score, metric_slice, pr_curve


@app.cell
def _(THR, auc_score, pd, scored):
    def _metrics(y, p):
        _n = len(y)
        _pos = int(y.sum())
        _alert = p >= THR
        _hit = int((y & _alert).sum())
        return {
            "n": _n,
            "base": _pos / _n if _n else float("nan"),
            "auc": auc_score(y, p) if 0 < _pos < _n else float("nan"),
            "prec": _hit / _alert.sum() if _alert.any() else float("nan"),
            "rec": _hit / _pos if _pos else float("nan"),
        }


    _rows = []
    for _op, _g in scored.groupby("operator"):
        for _metric, _sub, _ycol, _pcol in [
            ("cancel", _g, "call_cancelled", "p_cancel"),
            ("impact_arr", _g[_g["eligible_arr"].astype(bool)], "label_arr", "p_impact_arr"),
            ("impact_dep", _g[_g["eligible_dep"].astype(bool)], "label_dep", "p_impact_dep"),
        ]:
            _y = _sub[_ycol].to_numpy(dtype=bool)
            _p = _sub[_pcol].to_numpy(dtype=float)
            _rows.append({"operator": _op, "metric": _metric, **_metrics(_y, _p)})

    op_metrics = pd.DataFrame(_rows)

    _wide = op_metrics.pivot(index="operator", columns="metric", values=["n", "base", "auc", "prec", "rec"])
    _wide = _wide.reindex(columns=["cancel", "impact_arr", "impact_dep"], level=1)
    _wide = _wide.reindex(columns=["n", "base", "auc", "prec", "rec"], level=0)
    _wide = _wide.swaplevel(axis=1).sort_index(axis=1, level=0, sort_remaining=False)
    _wide = _wide.reindex(columns=["cancel", "impact_arr", "impact_dep"], level=0)
    _wide = _wide.loc[op_metrics[op_metrics.metric == "cancel"].sort_values("n", ascending=False).operator]

    _fmt = {c: ("{:,.0f}" if c[1] == "n" else "{:.3f}") for c in _wide.columns}
    _wide.style.format(_fmt).background_gradient(
        subset=[c for c in _wide.columns if c[1] == "auc"], cmap="RdYlGn", vmin=0.8, vmax=1.0
    ).background_gradient(
        subset=[c for c in _wide.columns if c[1] in ("prec", "rec")], cmap="RdYlGn", vmin=0.0, vmax=1.0
    )
    return (op_metrics,)


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Terskel-sveip per operatør

    Presisjon (heltrukket) og recall (stiplet) som funksjon av varslingsterskel, per operatør,
    for valgt metrikk. Grå vertikal linje = dagens faste terskel 0.65.

    Tabellen under viser den **operatør-spesifikke terskelen** som maksimerer recall under et
    presisjonskrav (≥ 0.85 / 0.90 / 0.95) — dvs. hva en per-operatør-terskel ville gitt i stedet
    for felles 0.65.
    """)
    return


@app.cell
def _(mo):
    metric_sel = mo.ui.dropdown(
        options=["cancel", "impact_arr", "impact_dep"], value="impact_dep", label="Metrikk"
    )
    metric_sel
    return (metric_sel,)


@app.cell
def _(THR, metric_sel, metric_slice, np, op_metrics, plt, scored):
    _ops = op_metrics[op_metrics.metric == "cancel"].sort_values("n", ascending=False).operator.tolist()
    _grid = np.linspace(0.01, 0.99, 99)
    _fig, _axes = plt.subplots(4, 4, figsize=(14, 11), sharex=True, sharey=True)
    for _ax, _op in zip(_axes.flat, _ops):
        _y, _p = metric_slice(scored[scored.operator == _op], metric_sel.value)
        _pos = int(_y.sum())
        if _pos == 0 or _pos == len(_y):
            _ax.set_title(f"{_op} (degenerert)", fontsize=9)
            continue
        _prec, _rec = [], []
        for _t in _grid:
            _a = _p >= _t
            _hit = int((_y & _a).sum())
            _prec.append(_hit / _a.sum() if _a.any() else np.nan)
            _rec.append(_hit / _pos)
        _ax.plot(_grid, _prec, color="tab:blue", lw=1.5, label="presisjon")
        _ax.plot(_grid, _rec, color="tab:orange", lw=1.5, ls="--", label="recall")
        _ax.axvline(THR, color="gray", ls=":", lw=1)
        _ax.set_title(f"{_op}  n={len(_y):,}", fontsize=9)
        _ax.set_ylim(0, 1.02)
    for _ax in _axes.flat[len(_ops):]:
        _ax.axis("off")
    _axes.flat[0].legend(fontsize=8, loc="lower left")
    _fig.suptitle(f"Presisjon/recall vs terskel — {metric_sel.value}", y=1.0)
    _fig.supxlabel("terskel")
    _fig.tight_layout()
    _fig
    return


@app.cell
def _(THR, metric_sel, metric_slice, np, op_metrics, pd, pr_curve, scored):
    _targets = [0.85, 0.90, 0.95]
    _ops = op_metrics[op_metrics.metric == "cancel"].sort_values("n", ascending=False).operator.tolist()
    _rows = []
    for _op in _ops:
        _y, _p = metric_slice(scored[scored.operator == _op], metric_sel.value)
        _pos = int(_y.sum())
        _row = {"operator": _op, "n": len(_y), "base": _pos / len(_y) if len(_y) else np.nan}
        if 0 < _pos < len(_y):
            _hit65 = int((_y & (_p >= THR)).sum())
            _a65 = int((_p >= THR).sum())
            _row["prec@0.65"] = _hit65 / _a65 if _a65 else np.nan
            _row["rec@0.65"] = _hit65 / _pos
            _thr, _prec, _rec = pr_curve(_y, _p)
            for _tgt in _targets:
                _ok = _prec >= _tgt
                if _ok.any():
                    _i = np.flatnonzero(_ok)[np.argmax(_rec[_ok])]
                    _row[f"thr(p>={_tgt})"] = _thr[_i]
                    _row[f"rec(p>={_tgt})"] = _rec[_i]
                else:
                    _row[f"thr(p>={_tgt})"] = np.nan
                    _row[f"rec(p>={_tgt})"] = np.nan
        _rows.append(_row)

    thr_targets = pd.DataFrame(_rows).set_index("operator")
    thr_targets.style.format("{:.3f}", na_rep="—").format({"n": "{:,.0f}"}).background_gradient(
        subset=[c for c in thr_targets.columns if c.startswith("rec")], cmap="RdYlGn", vmin=0, vmax=1
    )
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## Reliabilitetskurver (kalibrering)

    Per operatør, for valgt metrikk: prediksjonene deles i 10 sannsynlighetsbinger; hver
    prikk viser gjennomsnittlig predikert sannsynlighet (x) mot observert andel (y).
    Prikkestørrelse ∝ andel av radene i bingen; diagonalen = perfekt kalibrering.

    **ECE** (expected calibration error) = vektet gjennomsnittlig |observert − predikert|
    over bingene — 0 er perfekt. Platt-lagene er operatør-bevisste, så dette tester om
    per-operatør-intercept + felles helning holder over hele sannsynlighetsskalaen,
    ikke bare i snitt.
    """)
    return


@app.cell
def _(metric_sel, metric_slice, np, op_metrics, plt, scored):
    _ops = op_metrics[op_metrics.metric == "cancel"].sort_values("n", ascending=False).operator.tolist()
    _bins = np.linspace(0, 1, 11)
    _fig2, _axes2 = plt.subplots(4, 4, figsize=(14, 11), sharex=True, sharey=True)
    for _ax, _op in zip(_axes2.flat, _ops):
        _y, _p = metric_slice(scored[scored.operator == _op], metric_sel.value)
        if len(_y) == 0 or _y.all() or not _y.any():
            _ax.set_title(f"{_op} (degenerert)", fontsize=9)
            continue
        _bi = np.clip(np.digitize(_p, _bins) - 1, 0, 9)
        _conf, _acc, _share = [], [], []
        _ece = 0.0
        for _b in range(10):
            _m = _bi == _b
            _nb = int(_m.sum())
            if _nb == 0:
                continue
            _c, _a = _p[_m].mean(), _y[_m].mean()
            _conf.append(_c)
            _acc.append(_a)
            _share.append(_nb / len(_y))
            _ece += _nb / len(_y) * abs(_a - _c)
        _ax.plot([0, 1], [0, 1], color="gray", ls=":", lw=1)
        _ax.plot(_conf, _acc, color="tab:blue", lw=1, alpha=0.5, zorder=1)
        _ax.scatter(_conf, _acc, s=12 + 300 * np.sqrt(_share), color="tab:blue", alpha=0.8, zorder=2)
        _ax.set_title(f"{_op}  ECE={_ece:.3f}", fontsize=9)
        _ax.set_xlim(-0.02, 1.02)
        _ax.set_ylim(-0.02, 1.02)
    for _ax in _axes2.flat[len(_ops):]:
        _ax.axis("off")
    _fig2.suptitle(f"Reliabilitet — {metric_sel.value}", y=1.0)
    _fig2.supxlabel("predikert sannsynlighet")
    _fig2.supylabel("observert andel")
    _fig2.tight_layout()
    _fig2
    return


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    ## NSB-fortynning: hvor taper pooled_all mot prod?

    NSB-holdouten skåres av **begge** familiene (prod = NSB-only-modellene som serverer i dag;
    pooled_all = fellesfamilien der buss er 63 % av treningsradene), med hver families
    platt-lag. Radene er bit-identiske mellom tabellene, så all differanse er modellene.

    Tabellene under deler differansen (Δ = pooled_all − prod) etter kategori, status og
    tid-fra-onset; migrasjonscellen teller varsler som flytter seg over/under terskelen.
    """)
    return


@app.cell
def _(client):
    # NSB-holdout skåret av begge familier + slicing-kolonner. ~6 ML.PREDICTs over 262k rader.
    _q = """
    WITH ev AS (
      SELECT * FROM `ent-kixavvik-dev.disruptions.training_calls` WHERE is_eval
    ),
    ev_all AS (
      SELECT 'NSB' AS operator, * FROM ev
    ),
    pr_c AS (
      SELECT situationNumber, jkey, stop_order, call_cancelled, is_origin, is_terminal,
             dep_delay_pos, arr_delay_pos, category, status, planned, min_from_onset,
             (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`, (SELECT * FROM ev))
    ),
    pr_a AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf`, (SELECT * FROM ev))
    ),
    pr_d AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`, (SELECT * FROM ev))
    ),
    pa_c AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf_pooled_all`, (SELECT * FROM ev_all))
    ),
    pa_a AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf_pooled_all`, (SELECT * FROM ev_all))
    ),
    pa_d AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf_pooled_all`, (SELECT * FROM ev_all))
    ),
    w_pr_c AS (SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
                      MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
               FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt`)),
    w_pr_a AS (SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
                      MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
               FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt`)),
    w_pr_d AS (SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
                      MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
               FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt`)),
    w_pa_c AS (SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
                      MAX(IF(processed_input='raw_logit', weight, NULL))     AS a,
                      (SELECT cw.weight
                       FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt_pooled_all`),
                            UNNEST(category_weights) AS cw
                       WHERE processed_input='operator' AND cw.category='NSB') AS w_op
               FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt_pooled_all`)),
    w_pa_a AS (SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
                      MAX(IF(processed_input='raw_logit', weight, NULL))     AS a,
                      (SELECT cw.weight
                       FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_pooled_all`),
                            UNNEST(category_weights) AS cw
                       WHERE processed_input='operator' AND cw.category='NSB') AS w_op
               FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_pooled_all`)),
    w_pa_d AS (SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
                      MAX(IF(processed_input='raw_logit', weight, NULL))     AS a,
                      (SELECT cw.weight
                       FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_pooled_all`),
                            UNNEST(category_weights) AS cw
                       WHERE processed_input='operator' AND cw.category='NSB') AS w_op
               FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_pooled_all`))
    SELECT
      c.category, c.status, c.planned, c.min_from_onset,
      c.call_cancelled,
      (NOT c.is_origin   AND (c.call_cancelled OR c.arr_delay_pos IS NOT NULL)) AS eligible_arr,
      (NOT c.is_terminal AND (c.call_cancelled OR c.dep_delay_pos IS NOT NULL)) AS eligible_dep,
      (c.call_cancelled OR COALESCE(c.arr_delay_pos, 0) >= 10) AS label_arr,
      (c.call_cancelled OR COALESCE(c.dep_delay_pos, 0) >= 10) AS label_dep,
      1/(1+EXP(-(wpc.b + wpc.a * LN(GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)))))) AS p_cancel_prod,
      1/(1+EXP(-(wpa.b + wpa.a * LN(GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)))))) AS p_a10_prod,
      1/(1+EXP(-(wpd.b + wpd.a * LN(GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)))))) AS p_d10_prod,
      1/(1+EXP(-(wac.b + wac.w_op + wac.a * LN(GREATEST(LEAST(ac.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ac.p_raw,1-1e-6),1e-6)))))) AS p_cancel_pa,
      1/(1+EXP(-(waa.b + waa.w_op + waa.a * LN(GREATEST(LEAST(aa.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(aa.p_raw,1-1e-6),1e-6)))))) AS p_a10_pa,
      1/(1+EXP(-(wad.b + wad.w_op + wad.a * LN(GREATEST(LEAST(ad.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(ad.p_raw,1-1e-6),1e-6)))))) AS p_d10_pa
    FROM pr_c AS c
    JOIN pr_a AS a  USING (situationNumber, jkey, stop_order)
    JOIN pr_d AS d  USING (situationNumber, jkey, stop_order)
    JOIN pa_c AS ac USING (situationNumber, jkey, stop_order)
    JOIN pa_a AS aa USING (situationNumber, jkey, stop_order)
    JOIN pa_d AS ad USING (situationNumber, jkey, stop_order)
    CROSS JOIN w_pr_c AS wpc CROSS JOIN w_pr_a AS wpa CROSS JOIN w_pr_d AS wpd
    CROSS JOIN w_pa_c AS wac CROSS JOIN w_pa_a AS waa CROSS JOIN w_pa_d AS wad
    """
    nsb_cmp = client.query(_q).to_dataframe()
    for _fam in ("prod", "pa"):
        nsb_cmp[f"p_impact_arr_{_fam}"] = nsb_cmp[f"p_cancel_{_fam}"] + (1 - nsb_cmp[f"p_cancel_{_fam}"]) * nsb_cmp[f"p_a10_{_fam}"]
        nsb_cmp[f"p_impact_dep_{_fam}"] = nsb_cmp[f"p_cancel_{_fam}"] + (1 - nsb_cmp[f"p_cancel_{_fam}"]) * nsb_cmp[f"p_d10_{_fam}"]
    nsb_cmp.shape
    return (nsb_cmp,)


@app.cell
def _(THR, auc_score, metric_sel, mo, nsb_cmp, pd):
    _map = {
        "cancel": ("call_cancelled", None, "p_cancel_prod", "p_cancel_pa"),
        "impact_arr": ("label_arr", "eligible_arr", "p_impact_arr_prod", "p_impact_arr_pa"),
        "impact_dep": ("label_dep", "eligible_dep", "p_impact_dep_prod", "p_impact_dep_pa"),
    }
    _ycol, _elig, _pp, _ppa = _map[metric_sel.value]
    _df = nsb_cmp if _elig is None else nsb_cmp[nsb_cmp[_elig].astype(bool)]
    _df = _df.assign(
        onset_band=pd.cut(_df["min_from_onset"], [0, 60, 180, 720],
                          labels=["0–60 min", "60–180 min", "180–720 min"], include_lowest=True)
    )


    def _slice_table(df, by):
        _rows = []
        for _k, _g in df.groupby(by, observed=True):
            _y = _g[_ycol].to_numpy(dtype=bool)
            if len(_y) < 500 or not _y.any() or _y.all():
                continue
            _p1 = _g[_pp].to_numpy(dtype=float)
            _p2 = _g[_ppa].to_numpy(dtype=float)
            _rows.append({
                by: _k, "n": len(_y), "base": _y.mean(),
                "auc_prod": auc_score(_y, _p1), "auc_pool": auc_score(_y, _p2),
                "rec_prod": (_y & (_p1 >= THR)).sum() / _y.sum(),
                "rec_pool": (_y & (_p2 >= THR)).sum() / _y.sum(),
            })
        _t = pd.DataFrame(_rows)
        _t["d_auc"] = _t["auc_pool"] - _t["auc_prod"]
        _t["d_rec"] = _t["rec_pool"] - _t["rec_prod"]
        return _t.sort_values("n", ascending=False).set_index(by)


    def _style(t):
        return t.style.format({"n": "{:,.0f}"}, precision=3).background_gradient(
            subset=["d_auc", "d_rec"], cmap="RdYlGn", vmin=-0.08, vmax=0.08
        )


    mo.vstack([
        mo.md(f"### Δ etter kategori — {metric_sel.value} (pooled_all − prod; n ≥ 500)"),
        _style(_slice_table(_df, "category")),
        mo.md("### Δ etter status"),
        _style(_slice_table(_df, "status")),
        mo.md("### Δ etter tid fra onset"),
        _style(_slice_table(_df, "onset_band")),
    ])
    return


@app.cell
def _(THR, metric_sel, mo, nsb_cmp):
    _map2 = {
        "cancel": ("call_cancelled", None, "p_cancel_prod", "p_cancel_pa"),
        "impact_arr": ("label_arr", "eligible_arr", "p_impact_arr_prod", "p_impact_arr_pa"),
        "impact_dep": ("label_dep", "eligible_dep", "p_impact_dep_prod", "p_impact_dep_pa"),
    }
    _ycol2, _elig2, _pp2, _ppa2 = _map2[metric_sel.value]
    _d = nsb_cmp if _elig2 is None else nsb_cmp[nsb_cmp[_elig2].astype(bool)]
    _pos = _d[_d[_ycol2].astype(bool)]
    _neg = _d[~_d[_ycol2].astype(bool)]

    _lost = _pos[(_pos[_pp2] >= THR) & (_pos[_ppa2] < THR)]
    _gained = _pos[(_pos[_pp2] < THR) & (_pos[_ppa2] >= THR)]
    _fa_lost = _neg[(_neg[_pp2] >= THR) & (_neg[_ppa2] < THR)]
    _fa_gained = _neg[(_neg[_pp2] < THR) & (_neg[_ppa2] >= THR)]

    _top_lost = _lost.groupby("category").size().sort_values(ascending=False).head(5)
    _lines = "\n".join(f"    {_c}: {_n:,} ({_n / max(len(_lost), 1):.0%})" for _c, _n in _top_lost.items())

    mo.md(f"""
    ### Varselmigrasjon ved terskel {THR} — {metric_sel.value}

    Av **{len(_pos):,}** positive (faktisk rammede) stopp i NSB-holdouten:

    - **Tapte treff** (prod varsler, pooled_all ikke): **{len(_lost):,}** ({len(_pos) and len(_lost) / len(_pos):.1%})
    - **Nye treff** (pooled_all varsler, prod ikke): **{len(_gained):,}** ({len(_pos) and len(_gained) / len(_pos):.1%})
    - Falske alarmer som forsvinner / kommer til: {len(_fa_lost):,} / {len(_fa_gained):,}

    Tapte treff fordelt på kategori (topp 5):

    {_lines}
    """)
    return


if __name__ == "__main__":
    app.run()
