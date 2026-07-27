import marimo

__generated_with = "0.23.13"
app = marimo.App(width="medium")


@app.cell(hide_code=True)
def header(mo):
    mo.md("""
    # Hvor bra er varslene? (Vy og Go-Ahead)

    **Kort fortalt:** forrige notatbok (`impact_eval.py`) evaluerte varslene for Vy tog (NSB),
    familien som serverer i dag. Her spør vi: kan vi ta inn flere operatører ved å trene **én
    felles modellfamilie**, uten at det koster noe for NSB? Første kandidat er Go-Ahead (GOA,
    Sørlandsbanen/Jærbanen) — også tog, men bare ~2 % av treningsradene.

    Eksperimentet (docs/pooled-models/LEARNINGS.md, 2026-07-03): tren `*_pooled`-familien på
    NSB + GOA med `operator` som egenskap, evaluer per operatør på det temporale holdout-settet
    (`is_eval = TRUE`, onset ≥ 2026-04-01) i `disruptions.training_calls_pooled`, og **gate på
    NSB-paritet**: ingen serveringsendring hvis NSB taper målbart på poolingen.

    Resultat: **gaten PASSERTE.** NSB-differansene er ≤ 0,0004 AUC på alle hovedmetrikker
    (statistisk uavgjort), og GOA får sterke modeller umiddelbart (innstillings-AUC 0,986,
    ~95 % presisjon ved 91–95 % dekning på serveringsterskelen 0,65). Neste notatbok
    (`pooled-model-bus-eval.py`) tester samme idé utvidet til buss — der **feiler** gaten,
    og arkitekturen ender som to familier.
    """)


@app.cell
def imports():
    import marimo as mo
    import matplotlib.pyplot as plt
    import numpy as np
    import pandas as pd
    from google.cloud import bigquery


    return bigquery, mo, np, pd, plt


@app.cell
def bq_client(bigquery):
    client = bigquery.Client(project="ent-kixavvik-dev")
    return (client,)


@app.cell(hide_code=True)
def build_md(mo):
    mo.md("""
    ## Hva ble bygget

    | Artefakt | Skript | Notat |
    |---|---|---|
    | `disruptions.goa_calls` (833k stopp, 60k avganger) | `data/queries/create_goa_calls.sql` | rail_calls-formet, fra `last_recorded` (`dataSource='GOA' AND vehicleMode='rail'`), vindu 2025-02-25 → 2026-06-20 |
    | `disruptions.training_calls_pooled` (1,34M rader) | `data/queries/create_training_calls_pooled.sql` | training_calls-logikken + `operator`-egenskap; per-operatør calls-join |
    | 11 `*_pooled`-modeller | `data/queries/train_call_models_pooled.sql` | identiske kontrakter + `operator`; tunede cancel-opsjoner |
    | 9 `*_platt_pooled`-lag | `data/queries/create_pooled_calibration.sql` | ett delt fitt på det poolede holdout-settet |
    | Eval | `data/queries/eval_pooled_models.sql` | 3 familier × 3 metrikker × operatør |

    Nøkkelfakta fra byggingen:

    - **GOA finnes bare fra mars 2025** i `last_recorded` (`GOA:DatedServiceJourney:`-refer) —
      ingenting merket eksisterer tidligere, i noen kilde (census-funn). Erstatningsbusser
      skiller seg rent ut som `ServiceJourney` + `bus`.
    - **Joinen er skopet `(operator, jkey)`** — DatedServiceJourney-numeriske id-er er ikke
      garantert unike på tvers av kodespaces, og kryss-operatør-lekkasje av fasit må være umulig.
    - **`category` trengte null arbeid**: `disruptions.categories` dekker allerede 99,7 % av
      GOA-onset-tekstene (delte meldingsmaler).
    - **Totalkostnad ≈ $1,6** (trening $1,40, GOA-tabellen $0,10) og ~40 min vegg-tid.
    """)


@app.cell(hide_code=True)
def families_md(mo):
    mo.md("""
    ## Tre familier å sammenlikne

    Skåringen speiler `data/queries/eval_pooled_models.sql` — samme metrikk-pipeline som
    `impact_eval.py`: rå `ML.PREDICT`-sannsynligheter platt-rekalibreres (lukket form fra
    `ML.WEIGHTS`) og settes sammen som `p_impact = p_cancel + (1 − p_cancel) · P(≥10)`.

    - **pooled** — `*_pooled`-modellene + `*_platt_pooled`-lagene på det poolede holdout-settet,
      evaluert per operatør. Dette er kandidaten.
    - **prod** — dagens NSB-modeller på NSB-holdouten: **paritets-baselinen**, regnet på nytt i
      nøyaktig samme pipeline (ikke sitert fra dokumentasjon).
    - **zeroshot** — dagens NSB-modeller anvendt *blindt* på GOA-radene: hva GOA ville fått
      gratis uten pooling (ukjente GOA-linjer/stopp faller til trærnes default-grener).
      Gapet pooled − zeroshot er verdien av selve poolingen.

    NSB-radene i den poolede tabellen er bit-identiske med `training_calls` (1 055 196 trenings- /
    262 647 holdout-rader), så pooled-vs-prod-differansen på NSB måler modellene, ikke dataene.
    Platt-laget for den poolede familien er **ett delt fitt** på det poolede holdout-settet
    (~95 % NSB-rader) — kalibreringsavsnittet under sjekker om GOA rir den kurven akseptabelt.
    """)


@app.cell
def score_holdout(client):
    # Radnivå-skåring av alle tre familiene (samme pipeline som eval_pooled_models.sql).
    # 9 ML.PREDICTs: 3 _pooled-modeller på pooled-holdouten, 3 prod-modeller på NSB-holdouten,
    # 3 prod-modeller blindt på GOA-radene (zeroshot). ~$0.1-klasse skann.
    _q = """
    WITH pooled_ev AS (
      SELECT * FROM `ent-kixavvik-dev.disruptions.training_calls_pooled` WHERE is_eval
    ),
    prod_ev AS (
      SELECT * FROM `ent-kixavvik-dev.disruptions.training_calls` WHERE is_eval
    ),
    zs_ev AS (
      SELECT * FROM pooled_ev WHERE operator = 'GOA'
    ),
    pl_clf AS (
      SELECT operator, situationNumber, jkey, stop_order, call_cancelled,
             is_origin, is_terminal, dep_delay_pos, arr_delay_pos,
             (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf_pooled`, (SELECT * FROM pooled_ev))
    ),
    pl_a10 AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf_pooled`, (SELECT * FROM pooled_ev))
    ),
    pl_d10 AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf_pooled`, (SELECT * FROM pooled_ev))
    ),
    pl_wc AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt_pooled`)
    ),
    pl_wa AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt_pooled`)
    ),
    pl_wd AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt_pooled`)
    ),
    pr_clf AS (
      SELECT situationNumber, jkey, stop_order, call_cancelled,
             is_origin, is_terminal, dep_delay_pos, arr_delay_pos,
             (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`, (SELECT * FROM prod_ev))
    ),
    pr_a10 AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf`, (SELECT * FROM prod_ev))
    ),
    pr_d10 AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`, (SELECT * FROM prod_ev))
    ),
    pr_wc AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt`)
    ),
    pr_wa AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt`)
    ),
    pr_wd AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt`)
    ),
    zs_clf AS (
      SELECT situationNumber, jkey, stop_order, call_cancelled,
             is_origin, is_terminal, dep_delay_pos, arr_delay_pos,
             (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`, (SELECT * FROM zs_ev))
    ),
    zs_a10 AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf`, (SELECT * FROM zs_ev))
    ),
    zs_d10 AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`, (SELECT * FROM zs_ev))
    )
    SELECT 'pooled' AS family, c.operator, c.call_cancelled,
      (NOT c.is_origin   AND (c.call_cancelled OR c.arr_delay_pos IS NOT NULL)) AS eligible_arr,
      (NOT c.is_terminal AND (c.call_cancelled OR c.dep_delay_pos IS NOT NULL)) AS eligible_dep,
      (c.call_cancelled OR COALESCE(c.arr_delay_pos, 0) >= 10) AS label_arr,
      (c.call_cancelled OR COALESCE(c.dep_delay_pos, 0) >= 10) AS label_dep,
      1/(1+EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)))))) AS p_cancel,
      1/(1+EXP(-(wa.b + wa.a * LN(GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)))))) AS p_a10,
      1/(1+EXP(-(wd.b + wd.a * LN(GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)))))) AS p_d10
    FROM pl_clf AS c
    JOIN pl_a10 AS a USING (situationNumber, jkey, stop_order)
    JOIN pl_d10 AS d USING (situationNumber, jkey, stop_order)
    CROSS JOIN pl_wc AS wc CROSS JOIN pl_wa AS wa CROSS JOIN pl_wd AS wd
    UNION ALL
    SELECT 'prod', 'NSB', c.call_cancelled,
      (NOT c.is_origin   AND (c.call_cancelled OR c.arr_delay_pos IS NOT NULL)),
      (NOT c.is_terminal AND (c.call_cancelled OR c.dep_delay_pos IS NOT NULL)),
      (c.call_cancelled OR COALESCE(c.arr_delay_pos, 0) >= 10),
      (c.call_cancelled OR COALESCE(c.dep_delay_pos, 0) >= 10),
      1/(1+EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wa.b + wa.a * LN(GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd.b + wd.a * LN(GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(d.p_raw,1-1e-6),1e-6))))))
    FROM pr_clf AS c
    JOIN pr_a10 AS a USING (situationNumber, jkey, stop_order)
    JOIN pr_d10 AS d USING (situationNumber, jkey, stop_order)
    CROSS JOIN pr_wc AS wc CROSS JOIN pr_wa AS wa CROSS JOIN pr_wd AS wd
    UNION ALL
    SELECT 'zeroshot', 'GOA', c.call_cancelled,
      (NOT c.is_origin   AND (c.call_cancelled OR c.arr_delay_pos IS NOT NULL)),
      (NOT c.is_terminal AND (c.call_cancelled OR c.dep_delay_pos IS NOT NULL)),
      (c.call_cancelled OR COALESCE(c.arr_delay_pos, 0) >= 10),
      (c.call_cancelled OR COALESCE(c.dep_delay_pos, 0) >= 10),
      1/(1+EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wa.b + wa.a * LN(GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)))))),
      1/(1+EXP(-(wd.b + wd.a * LN(GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(d.p_raw,1-1e-6),1e-6))))))
    FROM zs_clf AS c
    JOIN zs_a10 AS a USING (situationNumber, jkey, stop_order)
    JOIN zs_d10 AS d USING (situationNumber, jkey, stop_order)
    CROSS JOIN pr_wc AS wc CROSS JOIN pr_wa AS wa CROSS JOIN pr_wd AS wd
    """
    scored = client.query(_q).to_dataframe()
    scored["p_impact_arr"] = scored["p_cancel"] + (1 - scored["p_cancel"]) * scored["p_a10"]
    scored["p_impact_dep"] = scored["p_cancel"] + (1 - scored["p_cancel"]) * scored["p_d10"]
    scored.groupby(["family", "operator"]).size().rename("holdout-rader").to_frame()
    return (scored,)


@app.cell
def helpers(np, pd):
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


@app.cell(hide_code=True)
def headline_md(mo):
    mo.md("""
    ## Hovedtabellen: tre familier, to operatører, tre metrikker

    Samme måltall som i forrige notatbok (AUC, presisjon og dekning ved serveringsterskelen
    0,65), nå per **familie × operatør**. `cancel` = P(innstilt) mot faktisk innstilling;
    `impact_*` = sammensatt p_impact mot innstilt ELLER ≥ 10 min forsinkelse, på kvalifiserte
    stopp (ankomst: ikke-origin; avgang: ikke-terminal).

    Les tabellen som to spørsmål:

    1. **Koster poolingen noe for NSB?** Sammenlikn `NSB/pooled` mot `NSB/prod` — radene skal
       være like.
    2. **Hva vinner GOA på pooling?** Sammenlikn `GOA/pooled` mot `GOA/zeroshot` — gapet er
       verdien av å trene med GOA-radene inne.
    """)


@app.cell(hide_code=True)
def headline(THR, auc_score, metric_slice, pd, scored):
    _rows = []
    for (_fam, _op), _g in scored.groupby(["family", "operator"]):
        for _metric in ["cancel", "impact_arr", "impact_dep"]:
            _y, _p = metric_slice(_g, _metric)
            _alert = _p >= THR
            _hit = int((_y & _alert).sum())
            _rows.append({
                "operator": _op, "family": _fam, "metric": _metric,
                "n": len(_y), "base": _y.mean(), "auc": auc_score(_y, _p),
                "prec": _hit / _alert.sum() if _alert.any() else float("nan"),
                "rec": _hit / int(_y.sum()) if _y.any() else float("nan"),
            })
    fam_metrics = pd.DataFrame(_rows)

    _wide = fam_metrics.pivot(index=["operator", "family"], columns="metric", values=["n", "base", "auc", "prec", "rec"])
    _wide = _wide.swaplevel(axis=1).sort_index(axis=1, level=0, sort_remaining=False)
    _wide = _wide.reindex(columns=["cancel", "impact_arr", "impact_dep"], level=0)
    _wide = _wide.reindex(columns=["n", "base", "auc", "prec", "rec"], level=1)
    _wide = _wide.reindex([("NSB", "prod"), ("NSB", "pooled"), ("GOA", "zeroshot"), ("GOA", "pooled")])

    _fmt = {c: ("{:,.0f}" if c[1] == "n" else "{:.3f}") for c in _wide.columns}
    _wide.style.format(_fmt).background_gradient(
        subset=[c for c in _wide.columns if c[1] == "auc"], cmap="RdYlGn", vmin=0.8, vmax=1.0
    ).background_gradient(
        subset=[c for c in _wide.columns if c[1] in ("prec", "rec")], cmap="RdYlGn", vmin=0.0, vmax=1.0
    )
    return (fam_metrics,)


@app.cell(hide_code=True)
def gate_md(mo):
    mo.md("""
    ## Gaten: NSB-paritet — og GOA-gevinsten

    Differansetabellene under svarer på de to spørsmålene direkte. Til venstre paritets-gaten:
    Δ = pooled − prod på NSB-holdouten (262 647 stopp; radene er bit-identiske mellom tabellene,
    så all differanse er modellene). Til høyre pooling-gevinsten: Δ = pooled − zeroshot på
    GOA-holdouten.

    **Gaten passerer** når NSB-differansene er støy (± noen få tusendeler); GOA-gevinsten viser
    hva selve poolingen kjøper utover ren egenskaps-overføring.
    """)


@app.cell(hide_code=True)
def gate_tables(fam_metrics, mo, pd):
    _m = fam_metrics.set_index(["operator", "family", "metric"])


    def _delta_table(op, fam_new, fam_base):
        _rows = []
        for _metric in ["cancel", "impact_arr", "impact_dep"]:
            _new = _m.loc[(op, fam_new, _metric)]
            _base = _m.loc[(op, fam_base, _metric)]
            _rows.append({
                "metrikk": _metric,
                f"AUC {fam_base}": _base["auc"], f"AUC {fam_new}": _new["auc"],
                "Δ AUC": _new["auc"] - _base["auc"],
                f"presisjon {fam_base}": _base["prec"], f"presisjon {fam_new}": _new["prec"],
                "Δ presisjon": _new["prec"] - _base["prec"],
                "Δ dekning": _new["rec"] - _base["rec"],
            })
        return pd.DataFrame(_rows).set_index("metrikk")


    _nsb = _delta_table("NSB", "pooled", "prod")
    _goa = _delta_table("GOA", "pooled", "zeroshot")

    def _sty(t, lim):
        return t.style.format("{:+.4f}", subset=[c for c in t.columns if c.startswith("Δ")]) \
            .format("{:.4f}", subset=[c for c in t.columns if not c.startswith("Δ")]) \
            .background_gradient(subset=[c for c in t.columns if c.startswith("Δ")], cmap="RdYlGn", vmin=-lim, vmax=lim)
    mo.vstack([
        mo.md("**NSB: paritets-gaten** (Δ = pooled − prod; skal være ~0)"),
        _sty(_nsb, 0.02),
        mo.md("**GOA: pooling-gevinsten** (Δ = pooled − zeroshot)"),
        _sty(_goa, 0.05),
    ])


@app.cell(hide_code=True)
def pr_fig_md(mo):
    mo.md("""
    ## Varslingskvalitet: de samme to spørsmålene som kurver

    Presisjon/dekning-kurver for `impact_arr` (ankomstsiden), som i forrige notatbok. Punktene
    markerer serveringsterskelen 0,65.

    - **Venstre (GOA):** zero-shot-kurven ligger systematisk under den poolede — for samme
      dekning må man akseptere flere falske alarmer uten pooling. De sterkeste egenskapene
      (linjetilstand, togets egen forsinkelse, status, kategori, timing) er operatør-generiske,
      så zero-shot fungerer overraskende bra; poolingen henter de siste ~3 AUC-poengene som
      ukjente GOA-linjer/stopp koster.
    - **Høyre (NSB):** pooled- og prod-kurvene ligger oppå hverandre — poolingen er gratis
      for NSB. Det er hele gaten.
    """)


@app.cell(hide_code=True)
def pr_figure(THR, metric_slice, plt, pr_curve, scored):
    _fig, (_ax1, _ax2) = plt.subplots(1, 2, figsize=(11.5, 4.8), sharey=True)

    _panels = [
        (_ax1, "GOA", [("pooled", "#2a78d6", "-", "pooled (med GOA i treningen)"),
                       ("zeroshot", "#eda100", "-", "zeroshot (dagens NSB-modeller blindt)")]),
        (_ax2, "NSB", [("pooled", "#2a78d6", "-", "pooled"),
                       ("prod", "#9a9a92", "--", "prod (serverer i dag)")]),
    ]
    for _ax, _op, _fams in _panels:
        for _fam, _farge, _ls, _navn in _fams:
            _g = scored[(scored.family == _fam) & (scored.operator == _op)]
            _y, _p = metric_slice(_g, "impact_arr")
            _thr, _prec, _rec = pr_curve(_y, _p)
            _ax.plot(_rec, _prec, color=_farge, lw=2, ls=_ls, label=_navn)
            _alert = _p >= THR
            _ax.plot((_y & _alert).sum() / _y.sum(), _y[_alert].mean(), "o",
                     color=_farge, ms=8, mec="white", mew=1.5, zorder=5)
        _ax.set_xlabel("andel reelle problemer vi varsler om (dekning)")
        _ax.set_title(f"{_op} — impact_arr", fontsize=11)
        _ax.set_xlim(0, 1)
        _ax.set_ylim(0, 1.02)
        _ax.grid(True, color="#e8e7e0", lw=0.7)
        _ax.set_axisbelow(True)
        _ax.spines["top"].set_visible(False)
        _ax.spines["right"].set_visible(False)
        _ax.legend(frameon=False, loc="lower left")
    _ax1.set_ylabel("andel varsler som er riktige (presisjon)")
    _ax1.annotate("terskel 0,65: pooling gir\n+5 pp presisjon ved litt\nlavere dekning",
                  xy=(0.92, 0.95), xytext=(0.35, 0.55),
                  fontsize=9, color="#444444",
                  arrowprops={"arrowstyle": "-", "color": "#9a9a92", "lw": 0.8})
    _ax2.annotate("kurvene ligger oppå hverandre:\npooling er gratis for NSB",
                  xy=(0.59, 0.91), xytext=(0.12, 0.35),
                  fontsize=9, color="#444444",
                  arrowprops={"arrowstyle": "-", "color": "#9a9a92", "lw": 0.8})
    plt.tight_layout()
    _fig


@app.cell(hide_code=True)
def calib_md(mo):
    mo.md("""
    ## Tåler GOA et delt kalibreringslag?

    Platt-laget for den poolede familien er **ett delt fitt** på det poolede holdout-settet —
    som er ~95 % NSB-rader. Koeffisientene er altså i praksis NSB-tilpasset, og spørsmålet er
    om GOA-sannsynlighetene likevel kan tas bokstavelig.

    Kurvene under (samme lesemåte som i forrige notatbok: predikert sannsynlighet gruppert i
    binger mot faktisk andel rammet; ærlig modell = diagonalen) viser svaret: **ja, med et mildt
    overskudd for GOA** — snittprediksjonen for innstilling er 0,519 mot realisert 0,505
    (+1,4 pp). Godt nok til å dele lag. Hvis GOA-reliabiliteten noen gang må strammes, er et
    **per-operatør-intercept i platt-laget** første ratt å vri på — det er nøyaktig det den
    all-operatør-poolede familien i neste notatbok gjør (`sigmoid(b + a·logit + w[operatør])`).
    """)


@app.cell(hide_code=True)
def calib_figure(metric_slice, np, pd, plt, scored):
    _fig, (_ax1, _ax2) = plt.subplots(1, 2, figsize=(11.5, 4.8), sharey=True)
    _metrics = [("cancel", "#2a78d6", "innstilling (p_cancel)"),
                ("impact_arr", "#1baf7a", "ankomst (p_impact)"),
                ("impact_dep", "#eda100", "avgang (p_impact_dep)")]

    # NSB: 20 binger (262k stopp); GOA: 10 binger (bare ~5,4k stopp -- 20 blir stoy)
    for _ax, _op, _nbins, _minn in [(_ax1, "NSB", 20, 50), (_ax2, "GOA", 10, 50)]:
        _g = scored[(scored.family == "pooled") & (scored.operator == _op)]
        for _metric, _farge, _navn in _metrics:
            _y, _p = metric_slice(_g, _metric)
            _bins = pd.cut(pd.Series(_p), np.linspace(0, 1, _nbins + 1))
            _df = pd.DataFrame({"p": _p, "y": _y}).groupby(_bins, observed=True).agg(
                pred=("p", "mean"), real=("y", "mean"), n=("y", "size"))
            _df = _df[_df.n >= _minn]
            _ax.plot(_df.pred, _df.real, color=_farge, lw=2, marker="o", ms=4, label=_navn)
        _ax.plot([0, 1], [0, 1], color="#9a9a92", lw=1, ls="--", zorder=0)
        _ax.set_title(f"{_op} — pooled familie, delt platt-lag", fontsize=11)
        _ax.set_xlabel("modellens predikerte sannsynlighet (gruppert)")
        _ax.set_xlim(0, 1)
        _ax.set_ylim(0, 1.02)
        _ax.grid(True, color="#e8e7e0", lw=0.7)
        _ax.set_axisbelow(True)
        _ax.spines["top"].set_visible(False)
        _ax.spines["right"].set_visible(False)
        _ax.legend(frameon=False, loc="upper left")
    _ax1.set_ylabel("hvor ofte stoppet faktisk ble rammet")
    _ax2.annotate("støyete med ~5,4k stopp, men følger\ndiagonalen; i snitt +1,4 pp overskudd\npå innstilling — greit for et delt lag",
                  xy=(0.62, 0.55), xytext=(0.40, 0.13),
                  fontsize=9, color="#444444",
                  arrowprops={"arrowstyle": "-", "color": "#9a9a92", "lw": 0.8})
    plt.tight_layout()
    _fig


@app.cell(hide_code=True)
def findings(mo):
    mo.md("""
    ## Funn (2026-07-03)

    | metrikk | familie | operatør | n | andel rammet | AUC | presisjon @0,65 | dekning @0,65 |
    |---|---|---|---|---|---|---|---|
    | cancel | **pooled** | GOA | 5 420 | 0,505 | **0,986** | 0,947 | 0,950 |
    | cancel | zeroshot | GOA | 5 420 | 0,505 | 0,955 | 0,858 | 0,946 |
    | cancel | **pooled** | NSB | 262 647 | 0,103 | **0,961** | 0,934 | 0,589 |
    | cancel | prod | NSB | 262 647 | 0,103 | 0,960 | 0,933 | 0,587 |
    | impact_arr | **pooled** | GOA | 5 039 | 0,655 | **0,969** | 0,951 | 0,918 |
    | impact_arr | zeroshot | GOA | 5 039 | 0,655 | 0,936 | 0,901 | 0,926 |
    | impact_arr | **pooled** | NSB | 250 697 | 0,187 | **0,931** | 0,913 | 0,592 |
    | impact_arr | prod | NSB | 250 697 | 0,187 | 0,930 | 0,912 | 0,589 |
    | impact_dep | **pooled** | GOA | 4 973 | 0,649 | **0,967** | 0,948 | 0,911 |
    | impact_dep | zeroshot | GOA | 4 973 | 0,649 | 0,930 | 0,888 | 0,924 |
    | impact_dep | **pooled** | NSB | 245 176 | 0,189 | **0,923** | 0,909 | 0,567 |
    | impact_dep | prod | NSB | 245 176 | 0,189 | 0,923 | 0,911 | 0,567 |

    - **NSB-paritet er eksakt — gaten passerte.** Differansene pooled − prod er ≤ 0,0004 AUC på
      alle tre hovedmetrikker (statistisk uavgjort). Å legge GOA (2 % av radene) i treningen
      endrer ingenting for NSB.
    - **GOA får sterke modeller umiddelbart.** Innstillings-AUC 0,986, p_impact-AUC ~0,97 begge
      sider, og på serveringsterskelen ~95 % presisjon ved 91–95 % dekning. GOA leser *lettere*
      enn NSB fordi situasjonene er journey-skopede innstillingsmeldinger: 51 % av berørte stopp
      innstilt og 65 % rammet, mot NSBs 10 %/19 % — modellen trenger mest å lese
      `status`/journey-skopet riktig.
    - **Pooling slår zero-shot klart.** Dagens NSB-modeller anvendt blindt på GOA klarer
      0,93–0,94 impact-AUC (egenskapene overføres!), men poolingen legger på ~+3 AUC-poeng og
      +5–9 pp presisjon ved terskelen — det ukjente GOA-linjer/stopp koster i default-grener.
    - **NSB-tallene her matcher forrige notatbok** (0,960 / 0,930 / 0,923, ~91 % presisjon) —
      metrikk-pipelinen er konsistent på tvers av evalueringene.

    **Forbehold**

    - GOA-holdouten er liten: 461 situasjoner / ~5,4k stopp → AUC ± ~0,005.
    - Platt-lagene er tilpasset på samme holdout som evalueres (leser optimistisk) — men likt
      for begge familier, så sammenlikningene er epler mot epler.
    - Januar 2026 er en produsentgap-måned for GOA (normale avgangstall, ~halvparten av
      call-radene) — etikettene undertelles der.
    - Forsinkelses-regressorene ble trent for familie-kompletthet, men ikke evaluert separat
      (svake hoder, R² ~0,13 hos NSB); impact-stacken er gaten.

    **Ikke gjort — porter før noen serveringsendring**

    1. **Per-horisont-platt-lag** (36 `*_platt_{lead60,120,240,plan}`-modeller) finnes bare for
       prod-familien; den poolede familien trenger samme refitt ved promotering.
    2. **Serveringsstien er NSB/VYG-hardkodet**: `disruptions_today.sql`
       (`participantRef='NSB'`), `rail_calls_*_today.sql` (`lineRef LIKE 'VYG:%'`),
       `predict_calls*.sql` (modellnavn), `main.py` (`VYG:Line:`-prefiks). Parameterisering er
       en egen fase.
    3. **GOA-ledetid** besvares i neste notatbok: GOA har faktisk den beste meldingskulturen av
       alle operatørene (median ledetid +77 min; meldingen finnes ≥ 30 min før avgang for ~64 %
       av avgangene).
    4. **Promoteringsbeslutningen**: pooled er uavgjort-eller-bedre for NSB, så én felles familie
       fristet — men **buss-utvidelsen i neste notatbok feilet NSB-gaten** (63 % buss-rader
       fortynner skinnesignalet), så arkitekturen endte som **to familier**: denne
       skinnefamilien (NSB + GOA) og en egen buss/lokal-familie.

    **Repro** (~$1,6 totalt): `create_goa_calls.sql` → `create_training_calls_pooled.sql` →
    `train_call_models_pooled.sql` → `create_pooled_calibration.sql` → `eval_pooled_models.sql`,
    alle i `data/queries/`.
    """)


if __name__ == "__main__":
    app.run()
