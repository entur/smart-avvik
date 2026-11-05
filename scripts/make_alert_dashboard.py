#!/usr/bin/env python3
"""Generate a Grafana dashboard from the alert-rule runbook.

The runbook (docs/observability/02-alert-rules-2026-09-11.md) is already the
reviewable definition the rules are created from, so it is the source here too:
one timeseries panel per rule, the query byte-identical, the alert threshold
drawn as a line. A panel showing "No data" is the failure that whole file is
written against — a wrong selector — made visible.

The dev/prd pair is collapsed onto an `env` dashboard variable, which the
runbook says is the only difference between the two copies.

    python3 scripts/make_alert_dashboard.py            # writes the JSON
    python3 scripts/make_alert_dashboard.py --check    # self-check only

Import: Grafana -> Dashboards -> New -> Import -> paste the JSON.
"""

import argparse
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
RUNBOOK = ROOT / "docs/observability/02-alert-rules-2026-09-11.md"
OUT = ROOT / "docs/observability/alert-dashboard.json"

HEADING = re.compile(r"^### (\S+) — (.+?) · (critical|warning)\s*$", re.MULTILINE)
PROMQL = re.compile(r"```promql\n(.*?)```", re.DOTALL)
CONDITION = re.compile(
    r"IS (ABOVE|BELOW) `([\d.]+)`.*?pending\*{0,2} `(\d+)m`.*?no data → (\w+)", re.DOTALL
)
DESCRIPTION = re.compile(r"^- `description`: `(.+)`\s*$", re.MULTILINE)
# The runbook writes every query for dev; prd differs only in this selector.
GROUP = re.compile(r'prometheus_group="kub-ent-dev-001"')


def rules(text):
    """Yield (number, title, severity, expr, op, threshold, pending, nodata, description)."""
    heads = list(HEADING.finditer(text))
    for head, nxt in zip(heads, heads[1:] + [None]):
        body = text[head.end(): nxt.start() if nxt else len(text)]
        query, cond, desc = PROMQL.search(body), CONDITION.search(body), DESCRIPTION.search(body)
        if not (query and cond):
            continue  # a section with no creatable rule
        yield (
            head.group(1),
            head.group(2),
            head.group(3),
            GROUP.sub('prometheus_group="$env"', query.group(1).strip()),
            cond.group(1),
            cond.group(2),
            int(cond.group(3)),
            cond.group(4),
            desc.group(1) if desc else "",
        )


def targets(expr, nodata):
    """Target A is the alert expression verbatim; target B is the readable one.

    Two shapes make a healthy panel read "No data", and neither is a broken
    selector — the failure this dashboard is meant to surface.

    1. A labelled counter child does not exist until its first increment, so
       `rate(kix_http_requests_total{status="5xx"}[5m])` is ABSENT rather than 0
       in an environment that has served no 5xx, and the ratio built on it is
       empty. Same for rule 6's ERROR level. The runbook records this as rule 6's
       known gap; it applies to every `kix_*` counter.
    2. Rules 7-9 carry a volume guard (`and on() ... > N`) that empties the whole
       expression whenever the guard is unsatisfied — dev's push volume makes
       that the normal state.

    Target B strips the guard, and zero-fills where absence really does mean
    zero: a counter the runbook gives `no data → Normal`. Not the gauge-age rules
    (1, 2, 2b) — there absence means the pod is gone, which is why the runbook
    gives them `no data → Alerting`, and a flat 0 would read as "just ticked".
    """
    out = [{"expr": expr, "legendFormat": "alert expression", "refId": "A"}]
    display = expr.split("\nand on()\n")[0].strip()
    if nodata == "Normal" and ("rate(" in display or "increase(" in display):
        display += " or vector(0)"
    if display != expr:
        out.append({"expr": display, "legendFormat": "readable", "refId": "B"})
    # Rules 1a/1b: the gauge is exported by every pod (followers and the worker at 0), so the sum
    # is how many lead and the count how many export it. Drawn side by side, a sum of 0 beside a
    # count of 0 is "no pods", beside a count of 3 it is "pods, but nobody leads".
    if expr.startswith("sum(kix_poller_leader"):
        out[0]["legendFormat"] = "leaders (sum)"
        out.append({"expr": "count" + expr[3:], "legendFormat": "pods exporting (count)", "refId": "B"})
    return out


def panel(i, rule):
    number, title, severity, expr, op, threshold, pending, nodata, desc = rule
    base, over = ("green", "red") if op == "ABOVE" else ("red", "green")
    return {
        "type": "timeseries",
        "title": f"{number} — {title}",
        "description": f"{severity} · fires {op.lower()} {threshold} for {pending}m\n\n{desc}",
        "datasource": {"type": "prometheus", "uid": "$datasource"},
        "gridPos": {"h": 8, "w": 12, "x": (i % 2) * 12, "y": (i // 2) * 8},
        "targets": targets(expr, nodata),
        "fieldConfig": {
            "defaults": {
                "custom": {"thresholdsStyle": {"mode": "line"}},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": base, "value": None},
                        {"color": over, "value": float(threshold)},
                    ],
                },
            },
            "overrides": [],
        },
    }


def dashboard(text):
    panels = [panel(i, r) for i, r in enumerate(rules(text))]
    return {
        "title": "smart-avvik — alert rules",
        "uid": "smart-avvik-alerts",
        "tags": ["smart-avvik", "alerts"],
        "timezone": "browser",
        "time": {"from": "now-6h", "to": "now"},
        "refresh": "1m",
        "schemaVersion": 39,
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {"text": "prometheus-thanos", "value": "prometheus-thanos"},
                },
                {
                    "name": "env",
                    "type": "custom",
                    "label": "Environment",
                    "query": "kub-ent-dev-001,kub-ent-prd-001",
                    "options": [
                        {"text": e, "value": e, "selected": i == 0}
                        for i, e in enumerate(("kub-ent-dev-001", "kub-ent-prd-001"))
                    ],
                    "current": {"text": "kub-ent-dev-001", "value": "kub-ent-dev-001"},
                },
            ]
        },
        "panels": panels,
    }


def check(dash):
    """The parse is regex over prose — assert it actually caught the rules."""
    assert len(dash["panels"]) >= 13, f"only {len(dash['panels'])} rules parsed"
    for p in dash["panels"]:
        expr = p["targets"][0]["expr"]
        assert expr, f"{p['title']}: empty query"
        assert "kub-ent-dev-001" not in expr, f"{p['title']}: env not parameterised"
        steps = p["fieldConfig"]["defaults"]["thresholds"]["steps"]
        assert steps[1]["value"] is not None, f"{p['title']}: no threshold"
    titles = [p["title"] for p in dash["panels"]]
    assert any("Labelling stale" in t for t in titles), "rule 9 missing"
    assert any("mirror stale" in t for t in titles), "rule 2a missing"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="parse and verify, write nothing")
    args = ap.parse_args()
    dash = dashboard(RUNBOOK.read_text())
    check(dash)
    if not args.check:
        OUT.write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(dash['panels'])} panels" + ("" if args.check else f" -> {OUT.relative_to(ROOT)}"))
