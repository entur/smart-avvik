"""Turn out/bqml_eval.json + out/bqml_run.log into the §14j parity table (doc + artifact)."""
import json, re, sys
import pandas as pd
S = sys.argv[1]; DOC = sys.argv[2]; ART = sys.argv[3]
rows = json.load(open(f"{S}/out/bqml_eval.json"))
df = pd.DataFrame(rows)
for c in ["n", "n_pos"]: df[c] = df[c].astype(int)
for c in ["auc", "brier", "share_green", "bad_green", "share_red", "bad_red"]: df[c] = pd.to_numeric(df[c], errors="coerce")
# wall clock per model
wall = {}
for line in open(f"{S}/out/bqml_run.log"):
    m = re.search(r"(?:RERUN )?(exp_togvarsel_\w+) rc=\d+ (\d+)s", line)
    if m: wall[m.group(1)] = int(m.group(2))
df["wall_s"] = df.apply(lambda r: wall.get(f"exp_togvarsel_{r.kind}_{r.cfg}_{r.fold}"), axis=1)
# local sweep numbers for the same cells
loc = pd.read_csv(f"{S}/out_sweep/results.csv")
MAP = {"am5_f15": ("am5", "F15"), "am5_f15cat": ("am5", "F15+CAT"), "am10_f15": ("am10", "F15"), "am10_f15cat": ("am10", "F15+CAT"),
       "pm5_pm1400": ("pm5", "PM1400"), "pm5_pm1400cat": ("pm5", "PM1400+CAT")}
def local(cfg, fold, kind):
    t, s = MAP[cfg]; k = "lr" if kind == "lr" else "xgb"
    r = loc[(loc.target == t) & (loc.set == s) & (loc.model == k) & (loc.proto == fold)]
    return float(r.auc.iloc[0]) if len(r) else float("nan")
LABEL = {"am5_f15": "AM trouble5, 15 feat.", "am5_f15cat": "AM trouble5, 15 + categories", "am10_f15": "AM trouble10, 15 feat.",
         "am10_f15cat": "AM trouble10, 15 + categories", "pm5_pm1400": "PM @ 14:00", "pm5_pm1400cat": "PM @ 14:00 + categories"}
md = ["| configuration | fold | BQML LOGISTIC_REG | local LR | BQML BOOSTED_TREE | local xgb | BT wall-clock | LR green→bad / red→bad | BT green→bad / red→bad |",
      "|---|---|---|---|---|---|---|---|---|"]
html = ['<div class="tbl"><table><tr><th>configuration</th><th>fold</th><th class="num">BQML logistic</th><th class="num">local LR</th><th class="num">BQML boosted tree</th><th class="num">local xgb</th><th class="num">tree wall-clock</th></tr>']
for cfg in MAP:
    for fold in ("A", "B"):
        lr = df[(df.cfg == cfg) & (df.fold == fold) & (df.kind == "lr")]; bt = df[(df.cfg == cfg) & (df.fold == fold) & (df.kind == "bt")]
        if not len(lr) or not len(bt): continue
        lr = lr.iloc[0]; bt = bt.iloc[0]
        md.append(f"| {LABEL[cfg]} | {fold} | **{lr.auc:.2f}** | {local(cfg, fold, 'lr'):.2f} | {bt.auc:.2f} | {local(cfg, fold, 'bt'):.2f} | {bt.wall_s/60:.0f} min | "
                  f"{lr.share_green*100:.0f} %→{lr.bad_green*100:.0f} % / {lr.share_red*100:.0f} %→{lr.bad_red*100:.0f} % | "
                  f"{bt.share_green*100:.0f} %→{bt.bad_green*100:.0f} % / {bt.share_red*100:.0f} %→{bt.bad_red*100:.0f} % |")
        html.append(f'<tr><td>{LABEL[cfg]}</td><td>{fold}</td><td class="num"><strong>{lr.auc:.2f}</strong></td><td class="num">{local(cfg, fold, "lr"):.2f}</td><td class="num">{bt.auc:.2f}</td><td class="num">{local(cfg, fold, "bt"):.2f}</td><td class="num">{bt.wall_s/60:.0f} min</td></tr>')
html.append("</table></div>")
lr_mean = df[df.kind == "lr"].auc.mean(); bt_mean = df[df.kind == "bt"].auc.mean()
lr_wall = df[df.kind == "lr"].wall_s.mean(); bt_wall = df[df.kind == "bt"].wall_s.mean()
wins_lr = int(sum(df[(df.kind == "lr")].set_index(["cfg", "fold"]).auc > df[(df.kind == "bt")].set_index(["cfg", "fold"]).auc))
ncells = len(df[df.kind == "lr"])
summary = (f"Across the {ncells} (configuration × fold) cells BQML `LOGISTIC_REG` averages AUC {lr_mean:.3f} against {bt_mean:.3f} for "
           f"`BOOSTED_TREE_CLASSIFIER` (same hyper-parameters as the local trees), and the logistic model wins {wins_lr} of {ncells}. "
           f"Wall-clock: {lr_wall:.0f} s per logistic model vs {bt_wall/60:.0f} min per boosted tree — BQML bills a boosted tree per iteration, "
           f"so 200 iterations on 250 rows bill ~15 GB each (the whole 24-model batch ≈ $1). The BQML logistic numbers sit within a few "
           f"hundredths of the local CV-tuned LR (raw counts and a fixed `l2_reg=0.5` instead of log1p and a CV-chosen C), so the production "
           f"path is the plain one: `LOGISTIC_REG` on the 15 + extra-ET-state columns, retrained monthly.")
doc = open(DOC).read()
i0 = doc.find("**BQML parity** — _interim state"); i1 = doc.find("spend.", i0) + len("spend.")
assert i0 > 0 and i1 > i0
doc = doc[:i0] + "**BQML parity.** " + summary + "\n\n" + "\n".join(md) + "\n\nGreen/red = share of test days below 10 % / at or above 30 %, and the observed bad-day rate inside each." + doc[i1:]
open(DOC, "w").write(doc)
art = open(ART).read()
j0 = art.find('  <div class="callout"><div class="k">BigQuery ML parity — pending one job</div>'); j1 = art.find("</div>\n\n", j0) + len("</div>\n\n")
assert j0 > 0 and j1 > j0
art = art[:j0] + art[j1:]
anchor = "  <h3>What this changes in the recommendation</h3>"
assert anchor in art
block = ('  <h3>BigQuery ML parity</h3>\n  <div class="prose"><p>' + summary.replace("`", "<code>", 1).replace("`", "</code>", 1)
         .replace("`BOOSTED_TREE_CLASSIFIER`", "<code>BOOSTED_TREE_CLASSIFIER</code>").replace("`l2_reg=0.5`", "<code>l2_reg=0.5</code>").replace("`LOGISTIC_REG`", "<code>LOGISTIC_REG</code>")
         + "</p></div>\n" + "\n".join(html) + "\n\n")
art = art.replace(anchor, block + anchor)
open(ART, "w").write(art)
print(summary); print("\n".join(md))
