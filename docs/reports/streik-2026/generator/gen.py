"""Build the strike-week reports (docs/reports/streik-2026/rapport-YYYY-MM-DD.html).

    python3 gen.py DATA_DIR     # DATA_DIR as filled by run.sh

Stdlib only. The report text is Norwegian; the per-day prose is in the intro_* functions below.
"""

import collections
import csv
import datetime as dt
import html
import os
import sys
from zoneinfo import ZoneInfo

OSLO = ZoneInfo("Europe/Oslo")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.dirname(HERE)
DATA = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data")
DAYS = ["2026-09-18", "2026-09-19", "2026-09-20", "2026-09-21", "2026-09-22", "2026-09-23"]
WEEKDAY = {"2026-09-18": "fredag", "2026-09-19": "lørdag", "2026-09-20": "søndag",
           "2026-09-21": "mandag", "2026-09-22": "tirsdag", "2026-09-23": "onsdag"}
NO_DOW = {"Mon": "man", "Tue": "tir", "Wed": "ons", "Thu": "tor", "Fri": "fre", "Sat": "lør",
          "Sun": "søn"}

e = html.escape


def read(path):
    with open(path) as f:
        return f.read()


def rd(name):
    """Rows of a CSV in DATA; bq writes nothing at all for an empty result."""
    path = os.path.join(DATA, name)
    if not os.path.exists(path) or not read(path).strip():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def ts(s):
    """A bq CSV timestamp (UTC, 'YYYY-MM-DD HH:MM:SS') as an Oslo datetime."""
    s = s.replace("Z", "+00:00")
    if "T" not in s:
        s = s.replace(" ", "T") + "+00:00"
    return dt.datetime.fromisoformat(s).astimezone(OSLO)


def short(s):
    return (s or "").replace(" stasjon", "").replace("Nationaltheatret", "Nationalth.")


def dm(d):
    return f"{int(d[8:])}.{d[5:7]}"


def minus_days(d, n):
    return (dt.date.fromisoformat(d) - dt.timedelta(days=n)).isoformat()


# ---- page ----------------------------------------------------------------------------------
CSS = """
:root{--bg:#fbfaf8;--fg:#1d1b18;--mut:#6b675f;--line:#e2ded6;--card:#fff;--acc:#b03a2e;--ok:#2e7d52}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#171614;--fg:#eceae6;--mut:#9a958c;--line:#33312d;--card:#201f1c;--acc:#e8776a;--ok:#6cc79a}}
:root[data-theme=dark]{--bg:#171614;--fg:#eceae6;--mut:#9a958c;--line:#33312d;--card:#201f1c;--acc:#e8776a;--ok:#6cc79a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:40px 16px 80px}
h1{font-size:1.75rem;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:1.1rem;margin:40px 0 10px;letter-spacing:-.01em}
.sub{color:var(--mut);margin:0 0 28px;font-size:.92rem}
.sub a{color:inherit}
.kpis{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;min-width:120px}
.kpi b{display:block;font-size:1.5rem;line-height:1.2;font-variant-numeric:tabular-nums}
.kpi span{color:var(--mut);font-size:.78rem;text-transform:uppercase;letter-spacing:.04em}
.scroll{overflow-x:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:.86rem}
th{text-align:left;font-weight:600;color:var(--mut);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.msg{font-size:.82rem;color:var(--fg);min-width:280px}
.tag{display:inline-block;font-size:.7rem;padding:1px 7px;border-radius:99px;border:1px solid var(--line);color:var(--mut)}
.full{color:var(--acc);font-weight:600}
p.note{color:var(--mut);font-size:.85rem;max-width:68ch}
"""


def page(title, subtitle, body):
    return f"""<!DOCTYPE html><html lang="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)}</title><style>{CSS}</style></head>
<body><div class="wrap"><h1>{e(title)}</h1><p class="sub">{subtitle}</p>{body}</div></body></html>"""


def table(cols, rows):
    """cols: (header, is_numeric) pairs; rows: lists of already-escaped cell HTML."""
    head = "".join(f'<th{" class=num" if num else ""}>{e(h)}</th>' for h, num in cols)
    body = "".join(
        "<tr>" + "".join(
            f'<td{" class=num" if num else (" class=msg" if h == "Melding" else "")}>{v}</td>'
            for (h, num), v in zip(cols, r)) + "</tr>"
        for r in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def kpis(items):
    return '<div class="kpis">' + "".join(
        f'<div class="kpi"><b>{e(str(v))}</b><span>{e(k)}</span></div>' for k, v in items) + "</div>"


def para(*parts):
    return '<p class="note">' + "".join(parts) + "</p>"


# ---- data ----------------------------------------------------------------------------------
PULLED = read(os.path.join(DATA, "pulled.txt")).strip()
days = rd("days.csv")
day = {r["call_date"]: r for r in days}
alerts = {d: rd(f"alerts_{d}.csv") for d in DAYS}
strike_sx = {d: rd(f"strike_sx_{d}.csv") for d in DAYS}
cap_spend = {d: rd(f"cap_spend_{d}.csv") for d in DAYS}
removed = rd("removed.csv")
cut = [r for r in removed if r["why"] == "plan_cut"]
net_cut = {r["d"]: int(r["cut"]) for r in rd("plan_cuts.csv")}
plan = {(r["d"], r["line"]): r for r in rd("lines_plan.csv")}
gaps = rd("gaps.csv")
sx_sits = rd("sx_situations.csv")

STRIKE_SITS = {r["situationNumber"] for rows in strike_sx.values() for r in rows}
STRIKE_SITS |= {r["situationNumber"] for r in sx_sits if "streik" in (r["descr"] or "").lower()}
LAST_STRIKE_SX = max(ts(r["first_v"]) for r in sx_sits if "streik" in (r["descr"] or "").lower())

caplog = collections.defaultdict(list)  # Oslo day -> [(time, withheld, registrations)]
with open(os.path.join(DATA, "push_capped.tsv")) as f:
    for ln in f:
        t, withheld, regs = ln.rstrip("\n").split("\t")[:3]
        when = ts(t)
        caplog[when.date().isoformat()].append((when, int(withheld), int(regs)))


def cut_trains(d):
    return {(r["line"], r["trainno"]) for r in cut if r["d"] == d}


def cut_regs(d):
    return {r["journey_digest"] for r in cut if r["d"] == d}


def n_why(d, why):
    return len({(r["line"], r["trainno"]) for r in removed if r["d"] == d and r["why"] == why})


def reason(g):
    if g["capped"] == "true":
        return "dagsgrense"
    if g["capped_any"] == "true":
        return "dagsgrense, gjentatt tog telt"
    if not g["last_push"]:
        return "aldri varslet"
    return "uavklart"


# ---- alert helpers -------------------------------------------------------------------------
# No SX named the strike after 21 Sept, so an alert is tagged with the cause the operator gave.
CAUSES = (("streik", "streik"), ("signalfeil", "signalfeil"), ("sporet", "feil på sporet"),
          ("togsett", "mangler togsett"), ("personell", "personell"), ("teknisk", "teknisk feil"),
          ("forsink", "forsinkelse"))


def tag(r):
    sn = r["situation_number"]
    if sn.startswith("KIX:ETCancellation:"):
        return "ET-innstilling"
    if sn.startswith("KIX:NoSituation:"):
        return "prediksjon"
    if sn in STRIKE_SITS:
        return "SX (streik)"
    text = ((r["descr"] or "") + " " + (r["summary"] or "")).lower()
    for word, label in CAUSES:
        if word in text:
            return f"SX ({label})"
    return "SX (annet)"


def line(r):
    return e(r["line"].split(":")[-1])


def pre(r):
    """Pushed before midnight of the departure day: the lead is longer than the clock time."""
    return int(r["lead_min"]) > int(r["avgang"][:2]) * 60 + int(r["avgang"][3:])


def leads(rows):
    return sorted(int(r["lead_min"]) for r in rows)


def med(values):
    return values[len(values) // 2]


def at_midnight(rows):
    return [r for r in rows if r["varslet"].startswith("00:0")]


# ---- tables --------------------------------------------------------------------------------
def alerts_table(rows):
    cols = [("Linje", 0), ("Fra", 0), ("Til", 0), ("Avgang", 0), ("Varslet", 0),
            ("Lead (min)", 1), ("Enheter", 1), ("Kilde", 0), ("Melding", 0)]
    return table(cols, [[line(r), e(short(r["fra"])), e(short(r["til"])), r["avgang"],
                         ("i går " if pre(r) else "") + r["varslet"][:5], r["lead_min"],
                         r["n_devices"], f'<span class="tag">{e(tag(r))}</span>',
                         e((r["message"] or "").replace("\n", " "))] for r in rows])


def gap_table(rows):
    cols = [("Linje", 0), ("Fra", 0), ("Til", 0), ("Avgang", 0), ("Først sett", 0),
            ("Status", 0), ("Enheter", 1), ("Sist varslet", 0), ("Trolig årsak", 0)]
    return table(cols, [[line(g), e(short(g["fra"])), e(short(g["til"])), g["avgang"],
                         g["first_seen"], f'<span class="tag">{e(g["st"])}</span>', g["dev"],
                         g["last_push"] or "—", f'<span class="tag">{e(reason(g))}</span>']
                        for g in rows])


def cap_table(groups):
    cols = [("Fra", 0), ("Til", 0), ("Enheter", 1), ("Ulike tog", 1), ("Tapte innstillinger", 1),
            ("De fire varslene", 0)]
    return table(cols, [[e(short(v[0]["fra"])), e(short(v[0]["til"])), v[0]["dev"],
                         v[0]["n_trains"], f'<b>{v[0]["n_missed"]}</b>',
                         "<br>".join(f'{x["varslet"]} · {e(x["line"].split(":")[-1])} '
                                     f'{x["avgang"]} · {e(x["om"])}' for x in v)]
                        for v in groups])


def base_table(hi):
    cols = [("Dato", 0), ("Dag", 0), ("Registrerte reiser", 1), ("Innstilte tog", 1),
            ("Varslet", 1), ("Fjernet fra planen", 1)]
    return table(cols, [[f'<b>{r["call_date"][5:]}</b>' if r["call_date"] == hi else r["call_date"][5:],
                         NO_DOW[r["dow"]], r["n_def"], r["canc"], r["et_pushed"],
                         len(cut_trains(r["call_date"]))] for r in days])


def sx_table(rows):
    groups = collections.OrderedDict()
    for r in sorted(rows, key=lambda r: r["first_versioned"]):
        groups.setdefault((r["summary"], r["descr"]), []).append(r)
    cols = [("Situasjon", 0), ("Tekst", 0), ("Tog", 1), ("Først meldt", 0)]
    return table(cols, [[e(s), e(t), len({x["ref"] for x in v}),
                         ts(v[0]["first_versioned"]).strftime("%d.%m %H:%M")]
                        for (s, t), v in sorted(groups.items(), key=lambda kv: -len(kv[1]))])


def per_line(d):
    """Line -> (removed train numbers, registrations hit), most trains first."""
    per = collections.defaultdict(lambda: (set(), set()))
    for r in cut:
        if r["d"] == d:
            per[r["line"]][0].add(r["trainno"])
            per[r["line"]][1].add(r["journey_digest"])
    return sorted(per.items(), key=lambda kv: -len(kv[1][0]))


def cut_line_table(d):
    ref = minus_days(d, 7)
    cols = [("Linje", 0), (f"Tog {dm(ref)}", 1), (f"Tog {dm(d)}", 1), ("Buss for tog", 1),
            ("Fjernede tog på registrerte reiser", 1), ("Reiser rammet", 1)]
    return table(cols, [[e(ln), plan.get((ref, ln), {}).get("tog", "—"),
                         plan.get((d, ln), {}).get("tog", "—"), plan.get((d, ln), {}).get("buss", "—"),
                         len(trains), len(regs)] for ln, (trains, regs) in per_line(d)])


def cut_reg_table(d):
    per = collections.OrderedDict()
    for r in sorted((r for r in cut if r["d"] == d), key=lambda r: r["avgang"]):
        per.setdefault(r["journey_digest"], []).append(r)
    rows = sorted(per.values(), key=lambda v: (-len(v), short(v[0]["fra"])))
    cols = [("Fra", 0), ("Til", 0), ("Enheter", 1), ("Fjernede avganger", 1), ("Avganger", 0)]
    return table(cols, [[e(short(v[0]["fra"])), e(short(v[0]["til"])), v[0]["dev"], len(v),
                         ", ".join(f'{x["line"]} {x["avgang"]}' for x in v)] for v in rows])


# ---- notes ---------------------------------------------------------------------------------
def and_join(parts):
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " og " + parts[-1]


def baseline():
    """Removed trains on registered journeys per day 14-18 Sept, before the cut, and their lines."""
    pre_days = ["2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"]
    counts = [len(cut_trains(x)) for x in pre_days]
    lines = sorted({r["line"] for r in cut if r["d"] in pre_days})
    n = str(counts[0]) if min(counts) == max(counts) else f"{min(counts)}–{max(counts)}"
    return f"14.–18. manglet {n} tog per dag på registrerte reiser, alle på {and_join(lines)}."


def cut_note(d):
    hist = [x for x in DAYS[1:] if x <= d]
    s = ("Standardplanen er tog som gikk minst 3 av de 4 foregående samme ukedagene. Et tog regnes "
         "ikke som fjernet hvis et nytt tog i dagens plan går samme strekning innen 15 minutter; da "
         "er det flyttet, ikke fjernet. Hver reises vindu er første til siste avgang reisen ble "
         "matchet mot den dagen, så tog i kanten av vinduet er ikke med, og tallene er et gulv. ")
    if d >= "2026-09-22":
        # Checked for 22 and 23 Sept only: L1/R13 bus-for-train trips run 21:16-02:31 from other
        # stop places (sql/lines_plan.sql counts them; the times came from a one-off query).
        s += ("Buss for tog på L1 og R13 går bare etter kl. 21, fra andre stoppesteder, og erstatter "
              "ikke togene som er fjernet om dagen. ")
    s += "Ingen SX-melding varsler et redusert rutetilbud, og årsaken står ikke i dataene. "
    if d == "2026-09-19":
        s += "Kuttet begynte denne dagen, mens streiken pågikk. "
    else:
        s += (f"Kuttet begynte lørdag 19.09, mens streiken pågikk (siste SX om streik kom "
              f"{LAST_STRIKE_SX:%d.%m kl. %H:%M}), og har ikke gitt seg. Per dag fra 19.09: "
              + ", ".join(str(len(cut_trains(x))) for x in hist) + " fjernede tog på registrerte "
              "reiser, " + ", ".join(str(net_cut[x]) for x in hist) + " i hele Vys plan. ")
    return para(s, baseline())


def week_note():
    extra = [f'{dm(r["call_date"])} {r["any_pushed"]} av {r["canc"]}' for r in days
             if r["any_pushed"] != r["et_pushed"]]
    et_miss = [n_why(r["call_date"], "et_cancel") for r in days if r["call_date"] < "2026-09-22"]
    return para("Innstilte tog som traff en registrert reise, per driftsdøgn. «Varslet» teller tog "
                "der ET-innstillingen selv ble varslet. Regnes SX-varsler om samme tog med, blir "
                f"{and_join(extra)}. «Fjernet fra planen» er tog som ville ha betjent en registrert "
                "reise, men manglet i dagens rutetabell; ingen av dem kunne varsles. "
                "ET-innstillinger /varsel ikke så fordi toget bare var meldt dagen før, var "
                f"{min(et_miss)}–{max(et_miss)} per dag fram til 21.09; den feilen er rettet i prd "
                f"fra 22.09 kl. 09:43. 23.09 er ikke omme (data hentet {PULLED}).")


def alerts_note(a, extra=""):
    lead = leads(a)
    src = collections.Counter(tag(r) for r in a)
    other_sx = len(a) - src["ET-innstilling"] - src["prediksjon"] - src["SX (streik)"]
    parts = [f"{n} {label}" for n, label in (
        (src["ET-innstilling"], "fra ET-innstillinger"),
        (src["SX (streik)"], "fra streikemeldinger i SX"),
        (other_sx, "fra andre SX-meldinger"),
        (src["prediksjon"], "fra prediksjon uten melding")) if n]
    n_pre, n_neg = sum(1 for r in a if pre(r)), sum(1 for x in lead if x < 0)
    s = (f'{len(a)} varsler til {sum(int(x["n_devices"]) for x in a)} enheter, lead '
         f"{lead[0]}–{lead[-1]} min (median {med(lead)}). Av dem kom {and_join(parts)}. ")
    s += f"{n_pre} gikk ut kvelden før og sier «i morgen». " if n_pre else "Ingen gikk ut kvelden før. "
    if n_neg:
        s += f"{n_neg} gikk ut etter planlagt avgang fra påstigningsstasjonen. "
    return para(s, extra)


def gap_note(g, extra=""):
    why = collections.Counter(reason(x) for x in g)
    s = f"{len(g)} innstillinger på registrerte reiser fikk ikke varsel. "
    if why["dagsgrense"]:
        s += (f'{why["dagsgrense"]} ligger på reiser som alt hadde fått fire varsler om fire ulike '
              "tog (<i>dagsgrense</i>). ")
    if why["dagsgrense, gjentatt tog telt"]:
        s += (f'{why["dagsgrense, gjentatt tog telt"]} ligger på reiser som hadde fått fire varsler '
              "der to gjaldt samme tog (<i>gjentatt tog telt</i>), se avsnittet om dagsgrensen. ")
    if why["aldri varslet"]:
        s += (f'{why["aldri varslet"]} ligger på reiser som ikke har fått et eneste varsel fra 4. til '
              "23. september, trolig registrert på enheter som aldri slo på varsler. ")
    if why["uavklart"]:
        s += f'{why["uavklart"]} kan ikke forklares fra BigQuery alene. '
    return para(s, extra)


def cap_note(groups):
    top = groups[0]
    n_short = sum(1 for v in groups if int(v[0]["n_trains"]) < 4)
    s = (f"{len(groups)} reiser fikk fire varsler og gikk deretter glipp av "
         f'{sum(int(v[0]["n_missed"]) for v in groups)} innstillinger. Flest tapte hadde '
         f'{short(top[0]["fra"])}–{short(top[0]["til"])}: alle fire brukt kl. {top[-1]["varslet"]}, '
         f'deretter {top[0]["n_missed"]} innstillinger uten varsel. Grensen er fire varsler per '
         "reise per døgn. ")
    if n_short:
        s += ("Et nytt varsel om et tog reisen allerede har fått varsel om, skal ifølge koden ikke "
              f"telle. {n_short} av reisene fikk likevel fire varsler om bare to eller tre ulike "
              "tog, og ble stille etterpå. Det tyder på at gjentakelsen ble telt. Det er ikke "
              "bekreftet.")
    return para(s)


def cap_log_text(d):
    lg = caplog.get(d)
    if not lg:
        return ""
    return (f"Dagsgrensen holdt tilbake varsler fra kl. {min(lg)[0]:%H:%M} til {max(lg)[0]:%H:%M}, "
            f"opptil {max(x[1] for x in lg)} i én pollersyklus og opptil {max(x[2] for x in lg)} "
            "reiser samtidig.")


def nav(d):
    return ('<p class="sub">Andre dager: ' + " · ".join(
        f"<b>{dm(x)}</b>" if x == d else f'<a href="rapport-{x}.html">{dm(x)}</a>' for x in DAYS)
        + "</p>")


SRC = ("Kilder: varsel_decisions (prd), SIRI-SX og SIRI-ET (ent-anshar-prd), GTFS-rutetabellen "
       "(timetable_gtfs), push_capped i poller-loggen. Alle klokkeslett Europe/Oslo. Én rad er én "
       "registrert reise × avgang. Bygget med generator/run.sh.")


# ---- per-day prose -------------------------------------------------------------------------
def intro_18(d, a, g, sx):
    k = day[d]
    why = collections.Counter(reason(x) for x in g)
    burst = collections.Counter(r["varslet"][:5] for r in a).most_common(1)[0]
    return ("<h2>Flest innstillinger i streikeuka, og dagsgrensen tok resten</h2>"
            + para(f'{k["canc"]} innstilte tog på registrerte reiser, mot {day[minus_days(d, 7)]["canc"]} '
                   f"fredagen før. Streiken ble meldt som innstillinger: {len(sx)} SX-meldinger om "
                   "dagens avganger sier «Dette skyldes streik», togene ble innstilt i ET, og "
                   f"rutetabellen var intakt ({len(cut_trains(d))} tog fjernet fra planen, som hver "
                   "dag før 19.09).")
            + para(f'Bare {k["et_pushed"]} av {k["canc"]} innstilte tog ble varslet. {burst[1]} av '
                   f"dagens varsler gikk ut i én og samme pollersyklus kl. {burst[0]}, og der brukte "
                   f"mange reiser opp dagsgrensen på fire. {cap_log_text(d)} "
                   f'{why["dagsgrense"]} innstillinger ligger på reiser som alt hadde fått sine fire, '
                   f'{why["aldri varslet"]} på reiser som aldri har fått et varsel.'))


def intro_19(d, a, g, sx):
    k = day[d]
    why = collections.Counter(reason(x) for x in g)
    return ("<h2>Streiken på L2, og de første togene borte fra planen</h2>"
            + para(f'{k["canc"]} innstilte tog på registrerte reiser, mot {day[minus_days(d, 7)]["canc"]} '
                   f'lørdagen før, og {k["et_pushed"]} av dem ble varslet. {len(sx)} SX-meldinger om '
                   "dagens avganger nevner streik, de fleste om L2 (Høvik/Stabekk–Ski), "
                   "Drammen–Lillehammer og «Færre vogner». Av innstillingene uten varsel skyldes "
                   f'{why["dagsgrense"]} dagsgrensen, og {why["aldri varslet"]} ligger på reiser som '
                   "aldri har fått varsel.")
            + para(f"Samme dag begynte Vy å fjerne tog fra rutetabellen: {len(cut_trains(d))} tog som "
                   f"går en vanlig lørdag, manglet i planen og ville ha betjent {len(cut_regs(d))} av "
                   f'de {k["n_def"]} registrerte reisene; i hele planen manglet {net_cut[d]}. De er '
                   "ikke innstilt i ET og ikke nevnt i SX, så ingen av dem ble varslet."))


def intro_20(d, a, g, sx):
    k = day[d]
    lg = caplog[d]
    released = at_midnight(alerts["2026-09-21"])
    return (f"<h2>Ingen streikemelding, men {len(cut_trains(d))} tog borte fra planen</h2>"
            + para(f"Ingen SX-melding nevner streik for søndagens avganger. {k['canc']} tog ble "
                   f"innstilt i ET, mot {day[minus_days(d, 7)]['canc']} forrige søndag, og alle "
                   f"{k['et_pushed']} ble varslet. Men {len(cut_trains(d))} tog som går en vanlig "
                   f"søndag, manglet i rutetabellen og ville ha betjent {len(cut_regs(d))} av de "
                   f"{k['n_def']} registrerte reisene; i hele planen manglet {net_cut[d]}. Ingen av "
                   "dem ble varslet. Den opprinnelige rapporten sa «ingen hull denne dagen»; det "
                   "gjaldt bare innstillingene i ET, som alle ble varslet.")
            + para(f"Fra kl. {min(lg)[0]:%H:%M} holdt dagsgrensen tilbake opptil {max(x[1] for x in lg)} "
                   f"varsler om gangen, fordelt på opptil {max(x[2] for x in lg)} reiser, mens ET "
                   f"meldte mandagens innstillinger. {len(released)} varsler gikk ut kl. "
                   f'{released[0]["varslet"][:5]} 21.09, i første syklus etter at grensen ble '
                   "nullstilt ved midnatt; trolig de samme."))


def intro_21(d, a, g, sx):
    k = day[d]
    src = collections.Counter(tag(r) for r in a)
    vogner = sum(1 for x in sx if "vogner" in (x["summary"] or ""))
    # The 38 registrations / 8 trains / 0.59 come from a one-off query of varsel_decisions for the
    # day's strike situations (all rows alertworthy = FALSE, message_status not_alertworthy).
    return (f"<h2>Tolv ganger en vanlig mandag, og {len(cut_trains(d))} tog borte fra planen</h2>"
            + para(f'{k["canc"]} innstilte tog på registrerte reiser, mot {day[minus_days(d, 7)]["canc"]} '
                   f'forrige mandag, og {k["et_pushed"]} av dem ble varslet. SIRI-SX knytter ingen av '
                   f"innstillingene til streiken: {len(sx)} meldinger om dagens avganger nevner "
                   f"streik, {vogner} av dem om «Færre vogner». De traff 38 registrerte reiser på 8 "
                   "tog, men lå under varslingsgrensen (høyest 0,59). "
                   f'{src["ET-innstilling"]} av dagens {len(a)} varsler kom fra ET-innstillinger '
                   "operatøren ikke skrev noe om.")
            + para(f"I tillegg manglet {len(cut_trains(d))} tog som går en vanlig mandag, i "
                   f"rutetabellen. De ville ha betjent {len(cut_regs(d))} registrerte reiser; i hele "
                   f"planen manglet {net_cut[d]}. Ingen av dem ble varslet, og den opprinnelige "
                   "rapporten tok dem ikke med."))


def intro_22(d, a, g, sx):
    k = day[d]
    lines = per_line(d)
    by_trains = [ln for ln, _ in lines[:2]]
    by_regs = [ln for ln, _ in sorted(lines, key=lambda kv: -len(kv[1][1]))[:3]]
    mon = "2026-09-21"
    return (f"<h2>Signalfeil, dagsgrensen og {len(cut_trains(d))} tog borte fra planen</h2>"
            + para(f"{len(cut_trains(d))} tog som går en vanlig tirsdag, manglet i dagens rutetabell "
                   f"og ville ha betjent en registrert reise, fordelt på {len(cut_regs(d))} reiser. De "
                   "er ikke innstilt i ET og ikke nevnt i SX. For /varsel finnes ikke toget, så ingen "
                   "av dem ble varslet, og de er heller ikke med i «Innstilte tog». Flest fjernede "
                   f"tog på {and_join(by_trains)}; flest rammede reiser på {and_join(by_regs)}.")
            + para(f'{k["canc"]} tog ble innstilt i ET, omtrent like mange som første streikedag, '
                   f'tirsdagen før ({day[minus_days(d, 7)]["canc"]}). Men ingen SX-melding nevner '
                   "streik. Operatøren oppga signalfeil (Oslo S–Nationaltheatret, Bryn–Grorud) og "
                   "feil på sporet (Skøyen–Oslo S), og innstilte L1 mellom Lillestrøm og Asker. I "
                   "tillegg gikk flere avganger med «Færre vogner» fordi et togsett manglet. "
                   "Innstillingene kom i løpet av dagen, ikke kvelden før: median lead er "
                   f"{med(leads(a))} min, mot {med(leads(alerts[mon]))} mandag.")
            + para(f'Bare {k["et_pushed"]} av {k["canc"]} innstilte tog ble varslet, mot '
                   f'{day[mon]["et_pushed"]} av {day[mon]["canc"]} mandag. Forskjellen er '
                   f"dagsgrensen på fire varsler per registrert reise. {cap_log_text(d)} De fire gikk "
                   "som regel til de første innstilte togene, meldt i SX mellom kl. 10 og 11. "
                   "Innstillingene av senere tog nådde ikke fram."))


def intro_23(d, a, g, sx):
    k = day[d]
    wed = minus_days(d, 7)
    rx = [r for r in a if r["line"].endswith("RX11") and r["avgang"] == "08:06"]
    n_l1_sx = sum(1 for r in a if r["line"].endswith(":L1") and tag(r) == "SX (mangler togsett)")
    capped = cap_log_text(d)

    def tog(ln, x):
        return plan[(x, ln)]["tog"]

    return ("<h2>Få innstillinger, mange fjernede tog</h2>"
            + para(f'{k["canc"]} innstilte tog i ET på registrerte reiser så langt, mot '
                   f'{day[wed]["canc"]} onsdagen før. Men {len(cut_trains(d))} tog som går en vanlig '
                   f"onsdag, mangler i dagens rutetabell og ville ha betjent {len(cut_regs(d))} "
                   f'registrerte reiser, flere enn i går ({len(cut_trains("2026-09-22"))}). De er ikke '
                   "innstilt i ET og ikke nevnt i SX, så ingen av dem ble varslet. L1 har "
                   f'{tog("L1", d)} tog i planen mot {tog("L1", wed)}, R13 {tog("R13", d)} mot '
                   f'{tog("R13", wed)} og R23 {tog("R23", d)} mot {tog("R23", wed)}.')
            + para(f"Ingen SX-melding nevner streik, for andre dag på rad. {len(rx)} av dagens "
                   f"{len(a)} varsler gjelder ett tog: RX11 08:06 fra Asker, innstilt fordi «vi "
                   f"mangler personell». {n_l1_sx} av de andre er L1-avganger om morgenen som fikk en "
                   "sannsynlighet for innstilling etter «Færre vogner» fordi et togsett manglet. "
                   + (capped if capped else "Ingen reise har nådd dagsgrensen så langt.")))


def extra_notes(d, a, g):
    """Day-specific sentences appended to the alerts and gap notes."""
    if d == "2026-09-21":
        eve = sorted(r["varslet"][:5] for r in a if pre(r))
        mid = at_midnight(a)
        return (f"De {len(eve)} varslene kvelden før gikk ut kl. {eve[0]}–{eve[-1]}, da ET meldte "
                f"morgendagens innstillinger. De {len(mid)} kl. {mid[0]['varslet'][:5]} gikk ut i "
                "første syklus etter at dagsgrensen ble nullstilt; se 20.09."), ""
    if d == "2026-09-23":
        rx = sum(1 for x in g if x["line"].endswith("RX11") and x["avgang"] == "08:06")
        on_alerts = ("RX11 08:06 ble først varslet fra SX kl. 20:47, og deretter igjen kl. 02:01 "
                     "da ET bekreftet innstillingen. Det andre varselet erstatter det første på "
                     "låseskjermen.")
        on_gaps = (f"{rx} av dem er RX11 08:06: reiser som først ble matchet mot toget kl. 05:01, "
                   "lenge etter at de andre hadde fått varsel, og som aldri fikk et. Mandag var "
                   "hullet på det samme toget, men hos andre reiser: fire av dagens fikk varsel om "
                   "RX11 08:06 mandag kl. 06:42, mens fem andre reiser på toget ikke fikk noe. Ingen "
                   "av dem forklares av dagsgrensen.")
        return on_alerts, on_gaps
    return "", ""


INTRO = {"2026-09-18": intro_18, "2026-09-19": intro_19, "2026-09-20": intro_20,
         "2026-09-21": intro_21, "2026-09-22": intro_22, "2026-09-23": intro_23}
SUB = {
    "2026-09-18": "Streiken meldt i SX og ET. Tabellen over L1-avganger er fra den opprinnelige "
                  "rapporten, hentet 19.09. ",
    "2026-09-19": "Streiken fortsetter på L2; de første togene fjernet fra planen. L1- og "
                  "L2-tabellene er fra den opprinnelige rapporten, hentet 19.09 kl. 18:30. ",
    "2026-09-20": "Ingen SX-melding nevner streik, men tog på registrerte reiser er fjernet fra planen. ",
    "2026-09-21": "SIRI-SX knytter ingen av innstillingene til streiken, og tog på registrerte "
                  "reiser er fjernet fra planen. ",
    "2026-09-22": "Ingen SX-melding nevner streik; de innstilte togene skyldes signal- og sporfeil "
                  "rundt Oslo. ",
    "2026-09-23": "Ingen SX-melding nevner streik. ",
}


def carried(d):
    """The SX-vs-ET line tables of the original 18/19 Sept reports, kept verbatim."""
    path = os.path.join(HERE, "carried", f"rapport-{d}.html")
    return read(path) if os.path.exists(path) else ""


def report(d):
    a = alerts[d]
    g = [x for x in gaps if x["call_date"] == d]
    why = collections.Counter(reason(x) for x in g)
    n_cap, n_rep = why["dagsgrense"], why["dagsgrense, gjentatt tog telt"]
    sx = strike_sx[d]
    caps = collections.OrderedDict()
    for x in cap_spend[d]:
        caps.setdefault(x["journey_digest"], []).append(x)
    caps = list(caps.values())
    k = day[d]
    items = [("Varsler sendt", len(a)), ("Innstilte tog", k["canc"]),
             ("Fjernet fra planen", len(cut_trains(d))), ("Varslet", f'{k["et_pushed"]} av {k["canc"]}'),
             ("Median lead", f"{med(leads(a))} min")]
    if n_cap:
        items.append(("Stoppet av dagsgrensen", f"{n_cap}–{n_cap + n_rep}" if n_rep else n_cap))
    items.append(("Streik-SX", len(sx)))
    alerts_extra, gap_extra = extra_notes(d, a, g)
    has_cut = len(cut_trains(d)) > 5

    body = nav(d) + kpis(items) + INTRO[d](d, a, g, sx) + carried(d)
    if has_cut:
        body += "<h2>Tog som forsvant fra planen</h2>" + cut_line_table(d) + cut_note(d)
    body += "<h2>Innstillinger per dag</h2>" + base_table(d) + week_note()
    if sx:
        body += ("<h2>Det streiken ble meldt som</h2>" + sx_table(sx)
                 + para(f"{len(sx)} SX-meldinger om dagens avganger nevner streik, om "
                        f'{len({x["ref"] for x in sx})} tog.'
                        + (" Ingen av dem førte til et varsel." if d == "2026-09-21" else "")))
    if caps:
        body += "<h2>Reiser som brukte opp dagsgrensen</h2>" + cap_table(caps) + cap_note(caps)
    body += "<h2>Varsler vi sendte</h2>" + alerts_table(a) + alerts_note(a, alerts_extra)
    if g:
        body += "<h2>Innstillinger uten varsel</h2>" + gap_table(g) + gap_note(g, gap_extra)
    if has_cut:
        body += ("<h2>Reiser som mistet tog fra planen</h2>" + cut_reg_table(d)
                 + para(f"{len(cut_regs(d))} registrerte reiser, "
                        f'{sum(1 for r in cut if r["d"] == d)} avganger. I tillegg var '
                        f'{n_why(d, "no_et")} tog i dagens plan ikke publisert i ET.'))

    title = f"Innstillinger og varsler — {WEEKDAY[d]} {int(d[8:])}. september 2026"
    sub = ("Innstillinger, tog fjernet fra planen og varsler på registrerte reiser. "
           f"{SUB[d]}Data hentet {PULLED}. " + SRC)
    with open(os.path.join(OUT, f"rapport-{d}.html"), "w") as f:
        f.write(page(title, sub, body))


if __name__ == "__main__":
    for d in DAYS:
        report(d)
    print(f"wrote {len(DAYS)} reports to {OUT}")
