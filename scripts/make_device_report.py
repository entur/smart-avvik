"""Render the Avviksvarsel device report from the notification STORE and open it in a browser.

Reads Firestore directly -- every `web_push` registration of the reserved `browser` client and
every device document -- and renders one HTML page: each device with its signup time, the journeys
it watches, and how close each registration is to the inactivity horizon. Needs an active gcloud
login whose ADC can read the environment's `notifications` database.

**It read the exported GCS blob until 2026-09-13 and moving it was a correctness fix, not a
freshness one.** Two things the blob cannot answer, and the second is why this could not wait:

1. `last_seen_at`, and therefore how close a registration is to the 180-day inactivity horizon
   (`WEB_PUSH_INACTIVITY_DAYS`). The exported journey record carries no such field and the blob
   backend has no concept of one, so under either backend the blob simply cannot say.
2. **Why a device stopped being pushable.** The blob has no shape for "a device with journeys and
   no push subscription" other than a tombstone, so `varsel_state.export_store_to_blob` writes one
   for every non-pushable device -- defaulting the reason to `expired` and the timestamp to the
   export instant when the store holds no tombstone at all. Measured on prd's first export
   (2026-09-13): `device_tombstones` went 1 -> 71, so 70 devices that had *never turned
   notifications on* were rendered here as "Expired" at the export's own clock, and the one real
   tombstone lost its original `ts`. That is precisely the «expired» vs «never turned on» split
   this report exists to measure -- the split that decided where the inactivity anchor is renewed
   -- so the artefact was not merely stale for this reader, it was wrong. The store holds the
   truth: a device with no tombstone has no tombstone.

    python3 scripts/make_device_report.py              # prd (the default)
    python3 scripts/make_device_report.py --env dev
"""
import argparse
import html
import os
import pathlib
import sys
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Same convention as scripts/manage_notification_client.py: disruptions/api is a flat import root
# rather than a package, so putting it on sys.path is what makes `import notification_store`
# resolve the way it does inside the app.
_API = pathlib.Path(__file__).resolve().parents[1] / "disruptions" / "api"
if str(_API) not in sys.path:
    sys.path.insert(0, str(_API))

import notification_store
import varsel_state

OSLO = ZoneInfo("Europe/Oslo")
DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
# Each environment is its own project with its own runtime identity, which is what keeps a dev
# credential from naming prd's data: the Firestore client takes its project from the ambient
# environment and never from an argument.
PROJECTS = {"prd": "ent-kixavvik-prd", "dev": "ent-kixavvik-dev"}
DATABASE = os.environ.get("NOTIFICATION_FIRESTORE_DATABASE") or "notifications"

parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("--env", choices=sorted(PROJECTS), default="prd", help="Environment to read (default: prd).")
parser.add_argument("--out", type=Path, help="HTML file to write (default: /tmp/avviksvarsel_device_report_<env>.html).")
parser.add_argument("--no-open", action="store_true", help="Do not open the report in a browser.")
args = parser.parse_args()

env = args.env
project = PROJECTS[env]
out_path = args.out or Path(f"/tmp/avviksvarsel_device_report_{env}.html")
source = f"{project}/{DATABASE}"

# Set BEFORE the client is built, for `manage_notification_client.py`'s reason: an exported
# GOOGLE_CLOUD_PROJECT naming another environment would otherwise decide which data this reads.
existing_project = os.environ.get("GOOGLE_CLOUD_PROJECT")
if existing_project and existing_project != project:
    print(
        f"GOOGLE_CLOUD_PROJECT is {existing_project} but --env {env} means {project}; using {project}",
        file=sys.stderr,
    )
os.environ["GOOGLE_CLOUD_PROJECT"] = project
notification_store.configure(database=DATABASE)

print(f"Reading {source}", file=sys.stderr)
_now = datetime.now(UTC)
_client = varsel_state.BROWSER_CLIENT_ID
_subs = notification_store.all_web_push_subscriptions(client_id=_client, now=_now)
_devices = notification_store.all_devices(client_id=_client)

# Rebuilt into the blob's own shapes so the rendering below is unchanged -- the report's question
# has not moved, only where the answer comes from. `_iso` keeps the renderer's `ts()` working.
def _iso(dt):
    return dt.astimezone(UTC).isoformat() if dt else ""


journeys = {
    sub.subscription_id: {
        "device_id": sub.device_id,
        "from_stop": sub.from_stop,
        "to_stop": sub.to_stop,
        "from_name": sub.from_name,
        "to_name": sub.to_name,
        "begin": sub.begin,
        "end": sub.end,
        "days": list(sub.days),
        "created_ts": _iso(sub.created_at),
        # The two the blob could never carry.
        "last_seen_ts": _iso(sub.last_seen_at),
        "inactivity_expires_ts": _iso(sub.inactivity_expires_at),
    }
    for sub in _subs
}
# A device is "active" here when it can actually be pushed to, which is what `pushable` means and
# what the blob's `devices` map used to hold. A non-pushable one falls through to the orphan
# section exactly as a missing blob record did.
devices = {
    device_id: {"created_ts": _iso(device.created_at), "brand": device.brand}
    for device_id, device in _devices.items()
    if device.pushable
}
# ONLY where the store actually holds one. The export fabricates `expired`/now for every
# non-pushable device; here an absent tombstone stays absent and renders as "never turned on",
# which is the distinction this report is read for.
tombstones = {
    device_id: {
        "reason": str(device.tombstone.get("reason") or "expired"),
        "ts": _iso(device.tombstone.get("at")) or _iso(device.updated_at),
        "created_ts": _iso(device.tombstone.get("original_created_at")) or _iso(device.created_at),
        "journeys": sum(1 for j in journeys.values() if j["device_id"] == device_id),
    }
    for device_id, device in _devices.items()
    if not device.pushable and device.tombstone
}
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


def horizon_cell(j):
    """How close this registration is to the 180-day inactivity horizon.

    The column the move to the store exists for: `last_seen_at` has no representation in the blob
    at all. The anchor is refreshed at most once per `WEB_PUSH_RENEW_AFTER_DAYS` (7) from the
    JOURNEY LIST -- opening the app -- so a push arriving renews nothing, and "days left" is
    therefore 173-180 for anyone who has opened it recently rather than a flat 180.
    """
    expires = j.get("inactivity_expires_ts")
    if not expires:
        # Absent rather than written far ahead: a horizon field that lies about its meaning is
        # worse than a missing one, and `subscription_is_live` reads the absence as expired.
        return '<td class="horizon"><span class="warn">no horizon</span></td>'
    left = (ts(expires) - snapshot).days
    seen = j.get("last_seen_ts")
    title = f"last seen {fmt(ts(seen))}" if seen else "never seen since the cutover"
    cls = "danger" if left <= 30 else ("warn" if left <= 60 else "ok")
    return f'<td class="horizon" title="{e(title)}"><span class="{cls}">{left}d</span></td>'


def journey_table(js):
    if not js:
        return '<p class="empty">No journeys registered on this device.</p>'
    rows = "".join(
        f'<tr><td class="route"><span>{e(name(j,"from"))}</span><span class="arrow" aria-hidden="true">→</span><span>{e(name(j,"to"))}</span></td>'
        f'<td class="days">{e(fmt_days(j["days"]))}</td>'
        f'<td class="win"><span class="mono">{e(j["begin"])}–{e(j["end"])}</span></td>'
        f'<td class="reg"><span class="mono">{fmt(ts(j["created_ts"]))}</span></td>'
        f'{horizon_cell(j)}</tr>'
        for j in js
    )
    return (
        '<div class="tablewrap"><table><thead><tr><th>Journey</th><th>Days</th><th>Window</th>'
        '<th>Registered</th><th title="Days until the 180-day inactivity horizon deletes it">'
        'Expires in</th></tr></thead>'
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
.horizon {{ text-align:right; white-space:nowrap; font-variant-numeric:tabular-nums; }}
.horizon .ok {{ color:var(--muted); }}
.horizon .warn {{ color:#8A5A00; font-weight:500; }}
.horizon .danger {{ color:#9B1C1C; font-weight:600; }}
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
    <p class="lede">Read from <span class="mono">{e(source)}</span> on
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
    app warns on the journey list until notifications are on. This split is read from the STORE, where a device that
    never turned notifications on simply has no tombstone; the exported blob has no shape for that state and writes
    every non-pushable device as «expired» at the export's own clock, which is why this report stopped reading it. The journeys are kept on purpose: the page still shows
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
