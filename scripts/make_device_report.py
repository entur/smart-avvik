"""Render the Avviksvarsel device report from the varsel state blob and open it in a browser.

Downloads gs://<bucket>/varsel_state.json with `gcloud storage cp` (so an active gcloud login is
needed), renders one HTML page — every device with its signup time and the journeys it watches,
newest signup first — and opens it.

    python3 scripts/make_device_report.py              # prd (the default)
    python3 scripts/make_device_report.py --env dev
    python3 scripts/make_device_report.py --no-download  # re-render an already downloaded blob
"""
import argparse
import html
import json
import subprocess
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

OSLO = ZoneInfo("Europe/Oslo")
DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# The VARSEL_STATE_BUCKET of each environment (helm/kix-avvik/env/values-kub-ent-*.yaml).
BUCKETS = {"prd": "ent-kixavvik-prd-varsel-state", "dev": "ent-kixavvik-dev-varsel-state-gke"}

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--env", choices=sorted(BUCKETS), default="prd", help="Environment to read (default: prd).")
parser.add_argument("--state", type=Path, help="Where to store the downloaded blob (default: /tmp/varsel_state_<env>.json).")
parser.add_argument("--out", type=Path, help="HTML file to write (default: /tmp/avviksvarsel_device_report_<env>.html).")
parser.add_argument("--no-download", action="store_true", help="Skip the download and render the existing --state file.")
parser.add_argument("--no-open", action="store_true", help="Do not open the report in a browser.")
args = parser.parse_args()

env = args.env
blob_uri = f"gs://{BUCKETS[env]}/varsel_state.json"
state_path = args.state or Path(f"/tmp/varsel_state_{env}.json")
out_path = args.out or Path(f"/tmp/avviksvarsel_device_report_{env}.html")

if not args.no_download:
    print(f"Downloading {blob_uri} -> {state_path}", file=sys.stderr)
    subprocess.run(["gcloud", "storage", "cp", blob_uri, str(state_path)], check=True)
elif not state_path.exists():
    sys.exit(f"{state_path} does not exist; run without --no-download first")

state = json.loads(state_path.read_text())
devices, journeys = state["devices"], state["journeys"]
# Why a device record went away while its journeys stayed (varsel._device_tombstones, written since
# 2026-09-04): device_id -> {reason: "expired" | "turned_off", ts, created_ts, journeys}. A journey
# whose device has neither a record nor a tombstone was registered by a device that never turned
# notifications on — or lost its record before the tombstones existed.
tombstones = state.get("device_tombstones", {})
REASONS = {
    "turned_off": ("notifications turned off", "Turned off"),
    "expired": ("subscription expired", "Expired"),
}
snapshot = datetime.now(UTC).astimezone(OSLO)


def ts(iso):
    return datetime.fromisoformat(iso).astimezone(OSLO)


def fmt(dt):
    return dt.strftime("%Y-%m-%d %H:%M")


def fmt_days(days):
    days = sorted(set(days))
    if days == list(range(7)):
        return "Every day"
    runs, start, prev = [], days[0], days[0]
    for d in days[1:]:
        if d == prev + 1:
            prev = d
            continue
        runs.append((start, prev)); start = prev = d
    runs.append((start, prev))
    parts = []
    for a, b in runs:
        parts.append(DAY[a] if a == b else (f"{DAY[a]}–{DAY[b]}" if b > a + 1 else f"{DAY[a]}, {DAY[b]}"))
    return ", ".join(parts)


def name(rec, side):
    return rec.get(f"{side}_name") or rec[f"{side}_stop"]


by_device = {}
for j in journeys.values():
    by_device.setdefault(j["device_id"], []).append(j)
for lst in by_device.values():
    lst.sort(key=lambda j: (j["begin"], j["created_ts"]))

active = sorted(devices.items(), key=lambda kv: kv[1]["created_ts"], reverse=True)
orphans = {d: js for d, js in by_device.items() if d not in devices}


def orphan_signup(did, js):
    """The tombstone's original signup when there is one, else the earliest journey."""
    tomb = tombstones.get(did) or {}
    return tomb.get("created_ts") or min(j["created_ts"] for j in js)


orphan_rows = sorted(orphans.items(), key=lambda kv: orphan_signup(*kv), reverse=True)
n_orphan_by_reason = {}
for did in orphans:
    reason = (tombstones.get(did) or {}).get("reason", "never_enabled")
    n_orphan_by_reason[reason] = n_orphan_by_reason.get(reason, 0) + 1

n_active = len(active)
n_active_journeys = sum(len(by_device.get(d, [])) for d, _ in active)
n_empty = sum(1 for d, _ in active if not by_device.get(d))
n_orphan_j = sum(len(v) for v in orphans.values())
e = html.escape


def journey_table(js):
    if not js:
        return '<p class="empty">No journeys registered on this device.</p>'
    rows = "".join(
        f'<tr><td class="route"><span>{e(name(j,"from"))}</span><span class="arrow" aria-hidden="true">→</span><span>{e(name(j,"to"))}</span></td>'
        f'<td class="days">{e(fmt_days(j["days"]))}</td>'
        f'<td class="win"><span class="mono">{e(j["begin"])}–{e(j["end"])}</span></td>'
        f'<td class="reg"><span class="mono">{fmt(ts(j["created_ts"]))}</span></td></tr>'
        for j in js
    )
    return (
        '<div class="tablewrap"><table><thead><tr><th>Journey</th><th>Days</th><th>Window</th><th>Registered</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
    )


def device_block(did, signup_dt, chips, js, signup_label="Signed up"):
    n = len(js)
    count = f'{n} journey{"s" if n != 1 else ""}'
    return (
        f'<article class="device">'
        f'<header><div class="when"><span class="lbl">{signup_label}</span>'
        f'<time class="mono" datetime="{signup_dt.isoformat()}">{fmt(signup_dt)}</time></div>'
        f'<div class="meta"><span class="count">{count}</span>{chips}'
        f'<span class="id mono" title="{e(did)}">{e(did[:8])}…{e(did[-4:])}</span></div></header>'
        f"{journey_table(js)}</article>"
    )


active_html = []
for did, rec in active:
    brand = rec.get("brand") or "entur"
    chips = f'<span class="chip brand-{e(brand)}">{e(brand)}</span>'
    active_html.append(device_block(did, ts(rec["created_ts"]), chips, by_device.get(did, [])))

orphan_html = []
for did, js in orphan_rows:
    tomb = tombstones.get(did)
    if tomb:
        label, chip = REASONS.get(tomb["reason"], (tomb["reason"], tomb["reason"]))
        chips = (
            f'<span class="chip gone" title="{e(label)} {fmt(ts(tomb["ts"]))}">{e(chip)} '
            f'{fmt(ts(tomb["ts"]))}</span>'
        )
        signup_label = "Signed up"
    else:
        chips = '<span class="chip gone">never turned on</span>'
        signup_label = "First journey"
    orphan_html.append(
        device_block(did, ts(orphan_signup(did, js)), chips, js, signup_label=signup_label)
    )


def reason_summary():
    parts = []
    for reason, n in sorted(n_orphan_by_reason.items(), key=lambda kv: -kv[1]):
        text = {
            "never_enabled": "never turned notifications on (or lost the record before 2026-09-04, when the reason started being recorded)",
            "turned_off": "turned notifications off («Skru av»)",
            "expired": "had their subscription retired by the push service (404/410)",
        }.get(reason, reason)
        parts.append(f"<strong>{n}</strong> {text}")
    return "; ".join(parts) if parts else "none"

page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Avviksvarsel Device Report ({e(env)})</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
:root {{
  --ground:#F5F7FA; --surface:#FFFFFF; --ink:#172033; --muted:#5F6B7E; --line:#D8DEE8; --line-soft:#E9EDF3;
  --accent:#1F5AA8; --accent-soft:#E4ECF8; --accent-ink:#173F76;
  --warn:#8A5A00; --warn-soft:#FBF1DC; --zebra:#F9FAFC;
  --shadow:0 1px 2px rgba(23,32,51,.06), 0 4px 14px rgba(23,32,51,.05);
  color-scheme: light;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground:#0F141B; --surface:#161C26; --ink:#E5EAF2; --muted:#98A4B8; --line:#2B3442; --line-soft:#222A36;
    --accent:#83ADF0; --accent-soft:#1B2A44; --accent-ink:#B8D0F7;
    --warn:#E6B95E; --warn-soft:#2D2510; --zebra:#1A212C;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 4px 14px rgba(0,0,0,.35);
    color-scheme: dark;
  }}
}}
:root[data-theme="dark"] {{
  --ground:#0F141B; --surface:#161C26; --ink:#E5EAF2; --muted:#98A4B8; --line:#2B3442; --line-soft:#222A36;
  --accent:#83ADF0; --accent-soft:#1B2A44; --accent-ink:#B8D0F7;
  --warn:#E6B95E; --warn-soft:#2D2510; --zebra:#1A212C;
  --shadow:0 1px 2px rgba(0,0,0,.4), 0 4px 14px rgba(0,0,0,.35);
  color-scheme: dark;
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--ground); color:var(--ink); font:15px/1.5 "Archivo", system-ui, -apple-system, "Segoe UI", sans-serif; }}
.mono {{ font-family:"IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, monospace; font-variant-numeric: tabular-nums; }}
main {{ max-width: 1040px; margin: 0 auto; padding: 40px 24px 72px; display:flex; flex-direction:column; gap:32px; }}
.masthead {{ display:flex; flex-direction:column; gap:10px; }}
.eyebrow {{ font-size:12px; letter-spacing:.08em; text-transform:uppercase; color:var(--accent); font-weight:600; }}
h1 {{ margin:0; font-size:30px; line-height:1.15; font-weight:600; letter-spacing:-.01em; text-wrap:balance; }}
.lede {{ margin:0; color:var(--muted); max-width:66ch; }}
.lede .mono {{ font-size:13.5px; color:var(--ink); }}
.summary {{ display:grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap:1px; background:var(--line); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
.summary > div {{ background:var(--surface); padding:16px 18px; display:flex; flex-direction:column; gap:2px; }}
.summary .n {{ font-size:28px; font-weight:600; letter-spacing:-.02em; line-height:1.1; }}
.summary .l {{ font-size:13px; color:var(--muted); }}
h2 {{ margin:0; font-size:20px; font-weight:600; letter-spacing:-.01em; }}
.section {{ display:flex; flex-direction:column; gap:14px; }}
.section > p {{ margin:0; color:var(--muted); max-width:70ch; }}
.devices {{ display:flex; flex-direction:column; gap:14px; }}
.device {{ background:var(--surface); border:1px solid var(--line); border-radius:10px; box-shadow:var(--shadow); overflow:hidden; }}
.device header {{ display:flex; justify-content:space-between; align-items:center; gap:16px; padding:14px 18px; border-bottom:1px solid var(--line-soft); flex-wrap:wrap; }}
.when {{ display:flex; align-items:baseline; gap:10px; }}
.when .lbl {{ font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); font-weight:500; }}
.when time {{ font-size:17px; font-weight:500; }}
.meta {{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; }}
.count {{ font-weight:600; }}
.chip {{ font-size:12px; font-weight:500; padding:2px 9px; border-radius:999px; background:var(--accent-soft); color:var(--accent-ink); letter-spacing:.01em; }}
.chip.brand-none {{ background:var(--line-soft); color:var(--muted); }}
.chip.gone {{ background:var(--warn-soft); color:var(--warn); }}
.id {{ font-size:12.5px; color:var(--muted); }}
.tablewrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; font-size:14.5px; }}
th {{ text-align:left; font-size:12px; letter-spacing:.06em; text-transform:uppercase; color:var(--muted); font-weight:500; padding:8px 18px; border-bottom:1px solid var(--line-soft); white-space:nowrap; }}
td {{ padding:9px 18px; vertical-align:top; border-bottom:1px solid var(--line-soft); }}
tbody tr:nth-child(even) td {{ background:var(--zebra); }}
tbody tr:last-child td {{ border-bottom:0; }}
td.route {{ display:flex; gap:8px; align-items:baseline; flex-wrap:wrap; min-width:260px; }}
td.route .arrow {{ color:var(--accent); }}
td.days {{ white-space:nowrap; color:var(--ink); }}
td.win, td.reg {{ white-space:nowrap; }}
td.win .mono {{ font-weight:500; }}
td.reg {{ color:var(--muted); }}
.empty {{ margin:0; padding:14px 18px; color:var(--muted); font-style:italic; }}
@media (max-width: 720px) {{
  .summary {{ grid-template-columns: repeat(2, minmax(0,1fr)); }}
  h1 {{ font-size:25px; }}
  td.route {{ min-width:200px; }}
}}
</style>
</head>
<body>
<main>
  <div class="masthead">
    <div class="eyebrow">Avviksvarsel · {e(env)}</div>
    <h1>Registered devices and their journeys</h1>
    <p class="lede">Read from <span class="mono">{e(blob_uri)}</span> on
    <span class="mono">{fmt(snapshot)}</span>. All times are Europe/Oslo. Devices are listed newest signup first;
    a device's signup is when it first registered its push subscription.</p>
  </div>

  <div class="summary">
    <div><span class="n">{n_active}</span><span class="l">devices with a push subscription</span></div>
    <div><span class="n">{n_active_journeys}</span><span class="l">journeys on those devices</span></div>
    <div><span class="n">{n_empty}</span><span class="l">subscribed devices with no journeys</span></div>
    <div><span class="n">{n_orphan_j}</span><span class="l">journeys no push can reach</span></div>
  </div>

  <section class="section">
    <h2>Devices with a push subscription</h2>
    <div class="devices">
      {"".join(active_html)}
    </div>
  </section>

  <section class="section">
    <h2>Journeys that no push can reach</h2>
    <p>These {n_orphan_j} journeys belong to {len(orphans)} device ids without a push subscription. Registering a
    journey never required notifications to be on, and most of these were registered by devices that never turned
    them on — typically an iPhone that opened the link in Safari without installing the app. Of the
    {len(orphans)} devices: {reason_summary()}. Since 2026-09-04 the server records why a record went away, and the
    app warns on the journey list until notifications are on. The journeys are kept on purpose: the page still shows
    live disruptions for them whenever the app is opened, and they reattach the moment notifications are turned on.
    Where the reason is recorded the device's original signup is shown; otherwise the earliest journey registration.</p>
    <div class="devices">
      {"".join(orphan_html)}
    </div>
  </section>
</main>
</body>
</html>
"""
out_path.write_text(page)
print(
    f"{env}: devices={n_active} journeys={n_active_journeys} empty={n_empty} "
    f"orphan_devices={len(orphans)} orphan_journeys={n_orphan_j} -> {out_path}"
)
if not args.no_open:
    webbrowser.open(out_path.resolve().as_uri())
