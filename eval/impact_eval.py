import marimo

__generated_with = "0.23.13"
app = marimo.App(width="medium")


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # Impact-evaluering

    **Kort fortalt:** mens en avviksmelding er aktiv — hvor godt klarer vi å forutsi hvilke
    stopp passasjerene faktisk vil merke det på: et innstilt tog, eller et tog som er
    10+ minutter forsinket?

    Evaluerer de serverte sannsynlighetene **`p_impact`** / **`p_impact_dep`** (innstilling +
    ≥10 min forsinkelse) mot faktisk utfall på det temporale holdout-settet (`is_eval = TRUE`,
    onset ≥ 2026-04-01) i `disruptions.training_calls`.

    Skåringen speiler `disruptions/api/predict_calls.sql`: rå `ML.PREDICT`-sannsynligheter
    platt-rekalibreres (`cancel_platt`, `arr_ge10_platt`, `dep_ge10_platt`) og settes sammen som
    `p_impact = p_cancel + (1 − p_cancel) · P(≥10)`.

    Fasit: **ankomstsiden** = innstilt ELLER ankomstforsinkelse ≥ 10 (avstigningsstopp);
    **avgangssiden** = innstilt ELLER avgangsforsinkelse ≥ 10 (påstigningsstopp).
    Bare rader der siden finnes (ikke origin/terminal) og utfallet er observert, telles med.
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
    _q = """
    WITH ev AS (
      SELECT * FROM `ent-kixavvik-dev.disruptions.training_calls` WHERE is_eval
    ),
    clf AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`, (SELECT * FROM ev))
    ),
    a10 AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_clf`, (SELECT * FROM ev))
    ),
    d10 AS (
      SELECT situationNumber, jkey, stop_order,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`, (SELECT * FROM ev))
    ),
    wc AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt`)
    ),
    wa AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.arr_ge10_platt`)
    ),
    wd AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt`)
    ),
    cal AS (
      SELECT c.situationNumber, c.jkey, c.stop_order,
        1/(1+EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)))))) AS p_cancel,
        1/(1+EXP(-(wa.b + wa.a * LN(GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(a.p_raw,1-1e-6),1e-6)))))) AS p_a10,
        1/(1+EXP(-(wd.b + wd.a * LN(GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)))))) AS p_d10
      FROM clf AS c
      JOIN a10 AS a USING (situationNumber, jkey, stop_order)
      JOIN d10 AS d USING (situationNumber, jkey, stop_order)
      CROSS JOIN wc CROSS JOIN wa CROSS JOIN wd
    )
    SELECT
      e.situationNumber, e.call_date, e.category, e.lineRef, e.min_from_onset,
      e.call_cancelled,
      (NOT e.is_origin   AND (e.call_cancelled OR e.arr_delay_pos IS NOT NULL)) AS eligible_arr,
      (NOT e.is_terminal AND (e.call_cancelled OR e.dep_delay_pos IS NOT NULL)) AS eligible_dep,
      (e.call_cancelled OR COALESCE(e.arr_delay_pos, 0) >= 10) AS label_arr,
      (e.call_cancelled OR COALESCE(e.dep_delay_pos, 0) >= 10) AS label_dep,
      ROUND(cal.p_cancel, 4) AS p_cancel,
      ROUND(cal.p_a10, 4)    AS p_arr_ge10,
      ROUND(cal.p_d10, 4)    AS p_dep_ge10,
      ROUND(cal.p_cancel + (1 - cal.p_cancel) * cal.p_a10, 4) AS p_impact,
      ROUND(cal.p_cancel + (1 - cal.p_cancel) * cal.p_d10, 4) AS p_impact_dep
    FROM ev AS e
    JOIN cal USING (situationNumber, jkey, stop_order)
    """
    df_impact = client.query(_q).to_dataframe()
    df_impact.shape
    return (df_impact,)


@app.cell
def metrics_helpers(np, pd):
    def auc_score(y, p):
        """Rank-statistic (Mann-Whitney) AUC with tie handling."""
        _r = pd.Series(p).rank(method="average").to_numpy()
        _pos = int(y.sum())
        _neg = len(y) - _pos
        return (_r[y].sum() - _pos * (_pos + 1) / 2) / (_pos * _neg)


    def pr_curve(y, p):
        """Precision/recall over descending thresholds, one point per distinct score."""
        _ord = np.argsort(-p, kind="mergesort")
        _y = y[_ord].astype(float)
        _tp = np.cumsum(_y)
        _prec = _tp / np.arange(1, len(_y) + 1)
        _rec = _tp / _y.sum()
        _keep = np.r_[np.diff(p[_ord]) != 0, True]
        return _prec[_keep], _rec[_keep], p[_ord][_keep]


    def pr_sweep(y, p, thresholds):
        """Alert operating points: fire when p >= threshold."""
        _rows = []
        for _t in thresholds:
            _hit = p >= _t
            _n = int(_hit.sum())
            _rows.append({
                "threshold": round(float(_t), 2),
                "alerts": _n,
                "precision": float(y[_hit].mean()) if _n else float("nan"),
                "recall": float(y[_hit].sum() / y.sum()),
            })
        return pd.DataFrame(_rows)

    def recall_at_precision(y, p, target=0.90):
        """Highest recall achievable at >= target precision; returns (recall, threshold)."""
        _prec, _rec, _thr = pr_curve(y, p)
        _ok = _prec >= target
        if not _ok.any():
            return float("nan"), float("nan")
        _i = np.argmax(_rec * _ok)
        return float(_rec[_i]), float(_thr[_i])

    return auc_score, pr_curve, pr_sweep, recall_at_precision


@app.cell
def eval_frames(df_impact):
    arr_eval = df_impact[df_impact.eligible_arr.fillna(False).astype(bool)].copy()
    arr_eval["y"] = arr_eval.label_arr.astype(bool)
    arr_eval["p"] = arr_eval.p_impact.astype(float)
    dep_eval = df_impact[df_impact.eligible_dep.fillna(False).astype(bool)].copy()
    dep_eval["y"] = dep_eval.label_dep.astype(bool)
    dep_eval["p"] = dep_eval.p_impact_dep.astype(float)
    f"ankomst: {len(arr_eval):,} rader | avgang: {len(dep_eval):,} rader"
    return arr_eval, dep_eval


@app.cell(hide_code=True)
def explain_numbers(mo):
    mo.md("""
    ## Slik leser du tallene

    Hver rad i dataene er ett **togstopp** — ett tog på ett stopp — som ligger innenfor vinduet
    til en aktiv avviksmelding. For hvert stopp gir modellen en sannsynlighet for at en passasjer
    på stoppet blir **rammet**: toget er innstilt, eller det er ≥ 10 minutter forsinket.

    - **andel rammet** — hvor ofte det faktisk skjedde (rundt 19 % av stoppene i denne perioden).
    - **snittprediksjon** — gjennomsnittssannsynligheten modellen ga. Når den treffer andelen
      rammet, er modellen ærlig *i snitt*: verken alarmistisk eller sovende.
    - **AUC** — trekk ett rammet og ett urammet stopp tilfeldig; AUC er sjansen for at modellen
      ga det rammede stoppet høyest sannsynlighet. 0,5 er myntkast, 1,0 er perfekt.
    """)
    return


@app.cell(hide_code=True)
def headline(arr_eval, auc_score, dep_eval, pd):
    impact_headline = pd.DataFrame([
        {"side": "ankomst (p_impact)", "n": len(arr_eval),
         "base_rate": arr_eval.y.mean(), "mean_pred": arr_eval.p.mean(),
         "auc": auc_score(arr_eval.y.to_numpy(), arr_eval.p.to_numpy())},
        {"side": "avgang (p_impact_dep)", "n": len(dep_eval),
         "base_rate": dep_eval.y.mean(), "mean_pred": dep_eval.p.mean(),
         "auc": auc_score(dep_eval.y.to_numpy(), dep_eval.p.to_numpy())},
    ]).round(4)
    impact_headline.rename(columns={
        "n": "antall stopp", "base_rate": "andel rammet",
        "mean_pred": "snittprediksjon", "auc": "AUC",
    })
    return


@app.cell(hide_code=True)
def explain_threshold(mo):
    mo.md("""
    ## Valg av varslingsterskel

    Et varsel utløses når sannsynligheten passerer en valgt terskel. Terskelen er et ratt
    mellom to hensyn:

    - **presisjon** — av varslene vi sendte, hvor mange var reelle problemer. Lav presisjon
      betyr falske alarmer, som undergraver tilliten til varslene.
    - **dekning (recall)** — av de reelle problemene, hvor mange varslet vi om. Lav dekning
      betyr at passasjerer blir overrasket av innstillinger og forsinkelser vi tidde om.

    Høyere terskel gir færre, men sikrere varsler. Tabellene under viser avveiningen for begge
    sider: for eksempel ved terskel **0,65** er ankomstvarselet riktig **91 %** av gangene og
    fanger fortsatt **59 %** av alle rammede stopp.
    """)
    return


@app.cell(hide_code=True)
def threshold_sweep(arr_eval, dep_eval, mo, np, pr_sweep):
    _ts = np.round(np.arange(0.30, 0.96, 0.05), 2)
    sweep_arr = pr_sweep(arr_eval.y.to_numpy(), arr_eval.p.to_numpy(), _ts)
    sweep_dep = pr_sweep(dep_eval.y.to_numpy(), dep_eval.p.to_numpy(), _ts)
    _no = {"threshold": "terskel", "alerts": "varsler", "precision": "presisjon", "recall": "dekning"}
    mo.hstack([
        mo.vstack([mo.md("**Ankomst — `p_impact`**"), sweep_arr.round(3).rename(columns=_no)]),
        mo.vstack([mo.md("**Avgang — `p_impact_dep`**"), sweep_dep.round(3).rename(columns=_no)]),
    ], gap=2)
    return sweep_arr, sweep_dep


@app.cell(hide_code=True)
def explain_charts(mo):
    mo.md("""
    ## De to figurene

    **Venstre — varslingskvalitet for hver terskel.** Hvert punkt på kurven er én mulig
    terskelinnstilling. Beveger man seg mot høyre, fanges flere av de reelle problemene (dekning),
    men andelen riktige varsler (presisjon) faller. De markerte punktene viser terskelen 0,65 fra
    tabellene over.

    **Høyre — betyr «70 %» faktisk 70 %?** Stoppene grupperes etter predikert sannsynlighet, og
    hvert punkt sammenlikner hva modellen *sa* med hvor ofte passasjerer *faktisk* ble rammet i
    gruppen. En ærlig modell ligger på den stiplede diagonalen. (En klype salt: kalibreringslaget
    ble tilpasset på den samme evalueringsperioden, så figuren ser litt bedre ut enn den ville
    gjort på helt ferske data — se forbeholdene nederst.)
    """)
    return


@app.cell(hide_code=True)
def pr_and_reliability(
    arr_eval,
    dep_eval,
    np,
    pd,
    plt,
    pr_curve,
    sweep_arr,
    sweep_dep,
):
    _fig, (_ax1, _ax2) = plt.subplots(1, 2, figsize=(11.5, 4.8))
    _colors = {"ankomst": "#2a78d6", "avgang": "#1baf7a"}

    for _name, _ev in [("ankomst", arr_eval), ("avgang", dep_eval)]:
        _prec, _rec, _ = pr_curve(_ev.y.to_numpy(), _ev.p.to_numpy())
        _ax1.plot(_rec, _prec, color=_colors[_name], lw=2, label=_name)
    for _name, _sw in [("ankomst", sweep_arr), ("avgang", sweep_dep)]:
        _pt = _sw.loc[np.isclose(_sw.threshold, 0.65)].iloc[0]
        _ax1.plot(_pt.recall, _pt.precision, "o", color=_colors[_name], ms=8,
                  mec="white", mew=1.5, zorder=5)
    _pt = sweep_arr.loc[np.isclose(sweep_arr.threshold, 0.65)].iloc[0]
    _ax1.annotate(
        "terskel 0,65 (ankomst):\n91 % riktige varsler,\n59 % av problemene fanget",
        xy=(float(_pt.recall), float(_pt.precision)), xytext=(0.16, 0.52),
        fontsize=9, color="#444444",
        arrowprops=dict(arrowstyle="-", color="#9a9a92", lw=0.8),
    )
    _ax1.set_xlabel("andel reelle problemer vi varsler om (dekning)")
    _ax1.set_ylabel("andel varsler som er riktige (presisjon)")
    _ax1.set_title("Varslingskvalitet for hver mulige terskel", fontsize=11)
    _ax1.set_xlim(0, 1)
    _ax1.set_ylim(0, 1.02)

    for _name, _ev in [("ankomst", arr_eval), ("avgang", dep_eval)]:
        _bins = pd.cut(_ev.p, np.linspace(0, 1, 21))
        _g = _ev.groupby(_bins, observed=True).agg(pred=("p", "mean"), real=("y", "mean"), n=("y", "size"))
        _g = _g[_g.n >= 50]
        _ax2.plot(_g.pred, _g.real, color=_colors[_name], lw=2, marker="o", ms=5, label=_name)
    _ax2.plot([0, 1], [0, 1], color="#9a9a92", lw=1, ls="--", zorder=0)
    _ax2.annotate(
        "en ærlig modell ligger\npå denne diagonalen",
        xy=(0.72, 0.72), xytext=(0.52, 0.18),
        fontsize=9, color="#444444",
        arrowprops=dict(arrowstyle="-", color="#9a9a92", lw=0.8),
    )
    _ax2.set_xlabel("modellens predikerte sannsynlighet (gruppert)")
    _ax2.set_ylabel("hvor ofte passasjerer faktisk ble rammet")
    _ax2.set_title("Predikert sannsynlighet mot virkeligheten", fontsize=11)
    _ax2.set_xlim(0, 1)
    _ax2.set_ylim(0, 1.02)

    for _ax in (_ax1, _ax2):
        _ax.grid(True, color="#e8e7e0", lw=0.7)
        _ax.set_axisbelow(True)
        _ax.spines["top"].set_visible(False)
        _ax.spines["right"].set_visible(False)
        _ax.legend(frameon=False, loc="lower left" if _ax is _ax1 else "upper left")
    plt.tight_layout()
    _fig
    return


@app.cell(hide_code=True)
def findings(mo):
    mo.md("""
    ## Funn (2026-07-03)

    | side | antall stopp | andel rammet | snittprediksjon | AUC |
    |---|---|---|---|---|
    | ankomst `p_impact` | 250 697 | 0,187 | 0,187 | **0,930** |
    | avgang `p_impact_dep` | 245 176 | 0,189 | 0,190 | **0,923** |

    - Ved terskel **0,65** gir ankomstvarselet **91,2 % presisjon @ 58,9 % dekning** — det tidligere
      dokumenterte driftspunktet (91 % @ 32 %, før promoteringen) har nesten doblet dekningen ved
      samme presisjon, i tråd med den tunede `call_cancel_clf` promotert 2026-07-03
      (AUC 0,885 → 0,960).
    - Avgangssiden ligger ~2 prosentpoeng bak ankomstsiden gjennom hele terskelsveipet.

    **Forbehold**

    - Platt-lagene (`cancel_platt`, `*_ge10_platt`) ble **tilpasset på det samme holdout-settet**
      (`train_call_models.sql`), så kalibreringsfiguren er in-sample for kalibreringslaget og leser
      optimistisk. Rangeringsmål (AUC, PR-kurvens form) påvirkes i praksis ikke; terskel-forankret
      presisjon/dekning kan flytte seg noe på helt ferske data.
    - Stopp uten observert utfall (ingen registrert forsinkelse, ikke innstilt) er utelatt; hvis
      manglende data korrelerer med påvirkning, er de realiserte ratene en nedre grense.
    """)
    return


@app.cell(hide_code=True)
def lead_header(mo):
    mo.md("""
    ## Hvor tidlig kan vi varsle?

    Spørsmålet over evaluerer varsling ~30 minutter før avgang (slik treningsdataene er forankret:
    togets egen forsinkelse måles ved siste stopp ≥ 30 min før, linjetilstanden i timen rundt
    60 min før). Her spør vi i stedet: **hvor mange minutter før planlagt avgang vet vi at avgangen
    blir innstilt eller ≥ 10 min forsinket?**

    To ting begrenser hvor tidlig vi kan varsle:

    1. **Taket:** avviksmeldingen må finnes. Halvparten av de rammede avgangene har meldingen
       mindre enn ~60 minutter før avgang — de kan ingen modell varsle tidligere om.
    2. **Informasjonsforfall:** jo tidligere vi spør, jo mindre vet vi om togets og linjens
       faktiske tilstand. Modellen faller gradvis tilbake til «planlegging framover»-regimet.

    Metode: holdout-settet re-skåres med de samme modellene, men med de to sanntidsforankrede
    egenskapene (`own_prev_delay`, `line_now_*`) beregnet som de ville sett ut **L minutter før
    avgang**, L ∈ {30, 60, 120, 240}, pluss en «ved melding»-variant uten observerbar tog-/
    linjetilstand (ren planlegging). L = 30 reproduserer treningsforankringen eksakt (verifisert:
    0 avvik mot `training_calls`). Tabellen bygges av `data/queries/leadtime_eval.sql`.
    """)
    return


@app.cell
def lead_scoring(client):
    _lq = """
    WITH inp AS (
      SELECT * FROM `ent-kixavvik-dev.disruptions.leadtime_eval_calls`
    ),
    clf AS (
      SELECT situationNumber, jkey, stop_order, lead_min,
             (SELECT prob FROM UNNEST(predicted_call_cancelled_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.call_cancel_clf`, (SELECT * FROM inp))
    ),
    d10 AS (
      SELECT situationNumber, jkey, stop_order, lead_min,
             (SELECT prob FROM UNNEST(predicted_y_ge_probs) WHERE label) AS p_raw
      FROM ML.PREDICT(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_clf`, (SELECT * FROM inp))
    ),
    wc AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.cancel_platt`)
    ),
    wd AS (
      SELECT MAX(IF(processed_input='__INTERCEPT__', weight, NULL)) AS b,
             MAX(IF(processed_input!='__INTERCEPT__', weight, NULL)) AS a
      FROM ML.WEIGHTS(MODEL `ent-kixavvik-dev.disruptions.dep_ge10_platt`)
    )
    SELECT
      e.lead_min, e.min_from_onset,
      (e.call_cancelled OR COALESCE(e.dep_delay_pos, 0) >= 10) AS label_dep,
      ROUND(
        1/(1+EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_raw,1-1e-6),1e-6))))))
        + (1 - 1/(1+EXP(-(wc.b + wc.a * LN(GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(c.p_raw,1-1e-6),1e-6)))))))
          * 1/(1+EXP(-(wd.b + wd.a * LN(GREATEST(LEAST(d.p_raw,1-1e-6),1e-6)/(1-GREATEST(LEAST(d.p_raw,1-1e-6),1e-6))))))
      , 4) AS p_impact_dep
    FROM inp AS e
    JOIN clf AS c USING (situationNumber, jkey, stop_order, lead_min)
    JOIN d10 AS d USING (situationNumber, jkey, stop_order, lead_min)
    CROSS JOIN wc CROSS JOIN wd
    WHERE NOT e.is_terminal AND (e.call_cancelled OR e.dep_delay_pos IS NOT NULL)
    """
    df_lead = client.query(_lq).to_dataframe()
    df_lead["y"] = df_lead.label_dep.astype(bool)
    df_lead["p"] = df_lead.p_impact_dep.astype(float)
    f"{len(df_lead):,} rader ({df_lead.lead_min.nunique()} lead-varianter)"
    return (df_lead,)


@app.cell(hide_code=True)
def lead_metrics_cell(auc_score, dep_eval, df_lead, pd, recall_at_precision):
    _all_impacted = dep_eval.y.sum()
    _rows = []
    for _L, _navn in [(30, "30 min før avgang"), (60, "60 min før avgang"),
                      (120, "120 min før avgang"), (240, "240 min før avgang"),
                      (-1, "ved melding (plan)")]:
        _v = df_lead[df_lead.lead_min == _L]
        _v = _v[_v.min_from_onset >= _L] if _L > 0 else _v
        _rec90, _thr90 = recall_at_precision(_v.y.to_numpy(), _v.p.to_numpy(), 0.90)
        _tak = (dep_eval[dep_eval.y].min_from_onset >= _L).mean() if _L > 0 else 1.0
        _flagget = (_v.y & (_v.p >= _thr90)).sum() / _all_impacted
        _rows.append({
            "spørretidspunkt": _navn,
            "avganger": len(_v),
            "AUC": auc_score(_v.y.to_numpy(), _v.p.to_numpy()),
            "dekning @90 % presisjon": _rec90,
            "terskel": _thr90,
            "tak (meldingen finnes)": _tak,
            "andel av ALLE rammede varslet": _flagget,
        })
    lead_metrics = pd.DataFrame(_rows).round(3)
    lead_metrics
    return


@app.cell(hide_code=True)
def lead_figure(dep_eval, df_lead, plt, recall_at_precision):
    _leads = [30, 60, 120, 240]
    _tak = [(dep_eval[dep_eval.y].min_from_onset >= _L).mean() for _L in _leads]

    # self-contained: compute "share of ALL impacted departures alerted" directly from df_lead
    _varslet = []
    for _L in _leads:
        _v = df_lead[(df_lead.lead_min == _L) & (df_lead.min_from_onset >= _L)]
        _rec, _thr = recall_at_precision(_v.y.to_numpy(), _v.p.to_numpy(), 0.90)
        _varslet.append(float((_v.y & (_v.p >= _thr)).sum() / dep_eval.y.sum()))

    _dekning_fast = []
    for _L in _leads:
        _v = df_lead[(df_lead.lead_min == _L) & (df_lead.min_from_onset >= 240)]
        _dekning_fast.append(recall_at_precision(_v.y.to_numpy(), _v.p.to_numpy(), 0.90)[0])
    _v_onset = df_lead[(df_lead.lead_min == -1) & (df_lead.min_from_onset >= 240)]
    _gulv = recall_at_precision(_v_onset.y.to_numpy(), _v_onset.p.to_numpy(), 0.90)[0]

    _fig, (_ax1, _ax2) = plt.subplots(1, 2, figsize=(11.5, 4.8))

    _ax1.plot(_leads, _tak, color="#9a9a92", lw=2, ls="--", marker="o", ms=5, label="taket: meldingen finnes så tidlig")
    _ax1.plot(_leads, _varslet, color="#2a78d6", lw=2, marker="o", ms=6, label="varslet @ 90 % presisjon")
    _ax1.annotate("38 % av rammede avganger\nkan varsles ≥ 30 min før",
                  xy=(30, _varslet[0]), xytext=(75, 0.55),
                  fontsize=9, color="#444444",
                  arrowprops=dict(arrowstyle="-", color="#9a9a92", lw=0.8))
    _ax1.set_xlabel("minutter før planlagt avgang")
    _ax1.set_ylabel("andel av alle rammede avganger")
    _ax1.set_title("Hvor tidlig kan vi varsle? (hele holdout)", fontsize=11)
    _ax1.set_xticks(_leads)
    _ax1.set_ylim(0, 1.0)

    _ax2.plot(_leads, _dekning_fast, color="#2a78d6", lw=2, marker="o", ms=6, label="spurt L min før avgang")
    _ax2.axhline(_gulv, color="#9a9a92", lw=1.5, ls="--")
    _ax2.annotate("gulvet: kun meldingsinfo,\ningen tog-/linjetilstand",
                  xy=(150, _gulv), xytext=(120, _gulv + 0.12),
                  fontsize=9, color="#444444",
                  arrowprops=dict(arrowstyle="-", color="#9a9a92", lw=0.8))
    _ax2.set_xlabel("spørretidspunkt: minutter før avgang")
    _ax2.set_ylabel("dekning ved 90 % presisjon")
    _ax2.set_title("Informasjonsforfall (samme avganger, tak ≥ 4 t)", fontsize=11)
    _ax2.set_xticks(_leads)
    _ax2.set_ylim(0, 0.6)

    for _ax in (_ax1, _ax2):
        _ax.grid(True, color="#e8e7e0", lw=0.7)
        _ax.set_axisbelow(True)
        _ax.spines["top"].set_visible(False)
        _ax.spines["right"].set_visible(False)
        _ax.legend(frameon=False, loc="upper right")
    plt.tight_layout()
    _fig
    return


@app.cell(hide_code=True)
def lead_quality_expl(mo):
    mo.md("""
    ### Varslingskvalitet per spørretidspunkt

    Samme to figurtyper som lenger opp, men nå for tre spørretidspunkter på **avgangssiden**:
    idet avviksmeldingen kommer, 60 minutter før avgang, og 30 minutter før avgang. Hver kurve
    bruker avgangene som er nåbare på sitt tidspunkt (meldingen må finnes).

    To ting å se etter:

    - **Venstre:** kurven ligger lavere jo tidligere vi spør — med mindre informasjon må vi ofre
      mer dekning for å holde samme presisjon.
    - **Høyre:** sannsynlighetene er kalibrert for ~30-minuttersregimet. Spør vi tidligere, blir
      modellen **overkonfident** — punktene faller under diagonalen: når den sier «80 %», rammes
      færre enn 80 %. Det er derfor terskelen må heves (0,62 → 0,86–0,88) for å holde 90 %
      presisjon ved tidlig varsling.
    """)
    return


@app.cell
def lead_quality_fig(df_lead, np, pd, plt, pr_curve):
    _varianter = [("ved melding", -1, "#eda100"), ("60 min før", 60, "#1baf7a"), ("30 min før", 30, "#2a78d6")]
    _fig, (_ax1, _ax2) = plt.subplots(1, 2, figsize=(11.5, 4.8))

    for _navn, _L, _farge in _varianter:
        _v = df_lead[df_lead.lead_min == _L]
        if _L > 0:
            _v = _v[_v.min_from_onset >= _L]
        _prec, _rec, _ = pr_curve(_v.y.to_numpy(), _v.p.to_numpy())
        _ax1.plot(_rec, _prec, color=_farge, lw=2, label=_navn)
        _bins = pd.cut(_v.p, np.linspace(0, 1, 21))
        _g = _v.groupby(_bins, observed=True).agg(pred=("p", "mean"), real=("y", "mean"), n=("y", "size"))
        _g = _g[_g.n >= 50]
        _ax2.plot(_g.pred, _g.real, color=_farge, lw=2, marker="o", ms=4, label=_navn)

    _ax1.set_xlabel("andel reelle problemer vi varsler om (dekning)")
    _ax1.set_ylabel("andel varsler som er riktige (presisjon)")
    _ax1.set_title("Varslingskvalitet per spørretidspunkt", fontsize=11)
    _ax1.set_xlim(0, 1)
    _ax1.set_ylim(0, 1.02)

    _ax2.plot([0, 1], [0, 1], color="#9a9a92", lw=1, ls="--", zorder=0)
    _ax2.annotate(
        "under diagonalen =\noverkonfident modell",
        xy=(0.62, 0.25), xytext=(0.50, 0.05),
        fontsize=9, color="#444444",
        arrowprops=dict(arrowstyle="-", color="#9a9a92", lw=0.8),
    )
    _ax2.set_xlabel("modellens predikerte sannsynlighet (gruppert)")
    _ax2.set_ylabel("hvor ofte avgangen faktisk ble rammet")
    _ax2.set_title("Predikert sannsynlighet mot virkeligheten", fontsize=11)
    _ax2.set_xlim(0, 1)
    _ax2.set_ylim(0, 1.02)

    for _ax in (_ax1, _ax2):
        _ax.grid(True, color="#e8e7e0", lw=0.7)
        _ax.set_axisbelow(True)
        _ax.spines["top"].set_visible(False)
        _ax.spines["right"].set_visible(False)
    _ax1.legend(frameon=False, loc="lower left")
    _ax2.legend(frameon=False, loc="upper left")
    plt.tight_layout()
    _fig
    return


@app.cell(hide_code=True)
def lead_findings(mo):
    mo.md("""
    ## Funn: hvor tidlig vet vi det? (2026-07-03)

    **Kort svar: i praksis ~30–60 minutter før planlagt avgang.** Ved 90 % presisjon:

    | varslingshorisont | dekning blant nåbare | andel av *alle* rammede |
    |---|---|---|
    | 30 min før avgang | 55 % | **38 %** |
    | 60 min før avgang | 31 % | 15 % |
    | 2 t før avgang | 19 % | 5 % |
    | 4 t før avgang | 17 % | 1,7 % |
    | ved melding (plan-regime) | 27 % | **27 %** |

    To ting begrenser tidlig varsling, omtrent like mye:

    1. **Meldingen kommer sent.** Median rammet avgang har avviksmeldingen bare ~57 min før
       planlagt avgang; kun 26 % har den ≥ 2 t før. Ingen modell kan varsle før meldingen finnes.
    2. **Informasjonen forfaller.** På samme avganger (tak ≥ 4 t) faller dekningen ved 90 %
       presisjon fra 51 % (spurt 30 min før) til 17 % (spurt 4 t før) — nesten ned til gulvet på
       19 % med kun meldingsinfo. Modellens sterkeste signal er togets egen observerte forsinkelse
       og linjetilstanden siste time, og de finnes ikke lenger enn ~1 t fram i tid.

    **Produktimplikasjon:** en to-trinns strategi ser riktig ut — varsle straks meldingen kommer
    med høy terskel (0,86; fanger 27 % med 90 % presisjon, typisk planlagte innstillinger), og
    re-vurder alle avganger løpende slik at de resterende fanges ~30–60 min før avgang (terskel
    ~0,62). Dagens serving gjør i praksis det siste allerede.

    **Forbehold:** tersklene per horisont er valgt på samme data (optimistisk); `line_now`-bøtta
    arver ~30 min slingring fra treningen; gjentatte varsler per avgang gir samlet sett litt
    lavere presisjon enn 90 % per spørring. Tabellen bygges av `data/queries/leadtime_eval.sql`
    (lead 30 verifisert identisk med treningsforankringen).
    """)
    return


if __name__ == "__main__":
    app.run()
