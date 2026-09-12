#!/usr/bin/env python3
"""Read the poller's cycle back: wall, CPU and RSS per phase, from Cloud Logging (U1, R16).

    # What the last two hours of dev cycles cost, per phase:
    python3 scripts/bench_cycle.py --environment dev --since 2h

    # Beside the harness receiver's own record:
    python3 scripts/bench_cycle.py --environment dev --since 2h \\
        --receiver /tmp/loadgen_receiver.json --shape evening --ratio prd --via store

    # Interpreter ms per definition, locally, with no cluster and no BigQuery:
    python3 scripts/bench_cycle.py profile --definitions 1000

**Where the numbers come from, and where they do not.**

The per-phase figures are read from the ``poller_phase`` and ``poller_cycle`` log records, in the
**cluster** project (``ent-kub-dev``, namespace ``kix-avvik``) — never the app project, where
``gcloud logging read --project=ent-kixavvik-dev`` returns nothing for them, silently. The filter
field is ``jsonPayload.event``, not ``jsonPayload.fields.event``: the logging setup lifts the
``fields`` dict to the top of the payload, and the nested form matches nothing while looking
perfectly plausible.

The same three figures are also Prometheus series — ``kix_poller_phase_duration_seconds``,
``kix_poller_phase_cpu_seconds``, ``kix_poller_cycle_duration_seconds`` — and **this script cannot
read them**. The ``kix_*`` series live in Thanos behind Grafana and are not in Google Managed
Prometheus (checked 2026-09-11), so there is no API to scrape them from here. Use them in Grafana
for rates and alerts; use this script for a run's numbers. That is not a redundancy to remove: the
histogram is what an alert rule can select on, the log record is what a benchmark row can quote.

**The trace has what the log cannot.** The five sub-pass spans under ``varsel_notify`` and the
per-Firestore-call spans (``firestore_transaction`` carries ``cpu_ms`` — KTD2 sizes the widened
emit pass in interpreter ms per transaction) are in Cloud Trace, not in any log line, deliberately:
R18 says per-event and per-attempt records get aggregated, so the place for per-call detail is the
trace. dev samples every trace. Read them with Kompass ``list_cloud_traces`` / ``get_cloud_trace``,
or the Cloud Trace console, and tag the row's source ``trace`` rather than ``log``.

**A row is only comparable to a row with the same tags.** ``--shape``, ``--ratio``, ``--via`` and
the three registration counts are recorded on the printed row because a store-seeded evening run at
prd's ratio and an API-seeded morning run at the plan's ratio are two different measurements of two
different things, and a phase timing without its registration counts has no denominator. Every tag
is passed in rather than inferred: nothing in a log record says which load produced it.

**What the shapes are, and which one is not a switch.** The EVENING shape is a live dev run between
22:00 and 01:00 Oslo, when the prediction cache is at its daily maximum and the next morning's
windows are inside the horizon — it is produced by running the harness at that hour, not by a flag.
The MORNING shape needs a prediction cache with many alert-worthy trains, which no script here can
synthesize: it needs a captured `call_predictions_today` from a real bad morning. Until that
capture exists, ``--shape morning`` is a label on a row somebody produced some other way, and
`profile` is a FLOOR rather than the peak (see :func:`run_profile`).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import statistics
import subprocess
import sys
from collections.abc import Iterable, Sequence

#: The GKE cluster project per environment. App logs are here, NOT in ent-kixavvik-*; a read
#: against the app project returns an empty result rather than an error, which is the whole reason
#: this map is in the code instead of in somebody's shell history.
CLUSTER_PROJECTS = {"dev": "ent-kub-dev", "tst": "ent-kub-dev", "prd": "ent-kub-prd"}
NAMESPACE = "kix-avvik"

#: `jsonPayload.event`, never `jsonPayload.fields.event`: logging_setup lifts `fields` onto the
#: payload root, so the nested form matches nothing and reports it as "no cycles in the window".
EVENT_FIELD = "jsonPayload.event"


#: The events a capacity row is read from. The first two are the cycle's own breakdown; the other
#: three are the three steps whose cost `poller_phase` cannot resolve on its own.
#:
#: `varsel_webhook_load` is NOT a `_phase` -- it happens inside `varsel_notify`, before pass 1a --
#: so its `ms` exists only on this record and appears in no phase breakdown. `notification_emit`
#: carries `created`/`deferred`/`capped`, which is KTD2's switch trigger stated as a number
#: ("`deferred` or `capped` above zero on two consecutive cycles"). `webhook_delivery_round`
#: carries `ready`/`sent`/`failed`/`retryable`, which is the delivery drain per cycle.
BENCH_EVENTS = (
    "poller_phase",
    "poller_cycle",
    "varsel_webhook_load",
    "notification_emit",
    "webhook_delivery_round",
)

#: Counter fields worth summing per event, and only counters: nothing here may name a stop ref, a
#: destination or a client (R18's redaction, restated at the reader).
EVENT_COUNTERS = {
    "varsel_webhook_load": ("n", "skipped"),
    "notification_emit": ("created", "deferred", "capped", "withdrawn", "unchanged"),
    "webhook_delivery_round": ("ready", "sent", "failed", "retryable", "capped"),
}


def log_filter(*, namespace: str = NAMESPACE, events: Sequence[str] = BENCH_EVENTS) -> str:
    clause = " OR ".join(f'{EVENT_FIELD}="{event}"' for event in events)
    return f'resource.labels.namespace_name="{namespace}" AND ({clause})'


def read_records(project: str, since: str, *, limit: int = 5000, runner=subprocess.run) -> list[dict]:
    """`gcloud logging read`, as JSON. One subprocess, because there is no client library here.

    `gcloud` rather than `google-cloud-logging`: this tool is run from a laptop that already has an
    authenticated gcloud, and adding a client library would put a dependency in `pyproject.toml`
    for a script CI never runs.
    """
    if shutil.which("gcloud") is None:
        raise RuntimeError("gcloud is not on PATH; this reads Cloud Logging in the cluster project")
    completed = runner(
        [
            "gcloud", "logging", "read", log_filter(),
            f"--project={project}",
            f"--freshness={since}",
            f"--limit={limit}",
            "--format=json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"gcloud logging read failed: {completed.stderr.strip()[:400]}")
    return json.loads(completed.stdout or "[]")


def _payloads(records: Iterable[dict]) -> list[dict]:
    return [r.get("jsonPayload") or {} for r in records]


def summarise(records: Iterable[dict]) -> dict:
    """Per-phase p50/max of wall, CPU and RSS, plus the cycle totals.

    `cpu_ms` is why this exists: wall clock cannot tell one CPython thread executing bytecode from
    a socket wait, and that distinction is the whole of KTD17 — pools and cores buy wall clock for
    waits and nothing at all for bytecode. `rss_delta_bytes` is optional in the record (it is read
    from /proc, which the container has and a laptop does not), so a phase without it reports
    `null` rather than 0.
    """
    phases: dict[str, dict[str, list[float]]] = {}
    cycles: list[float] = []
    steps: dict[str, dict[str, list[float]]] = {}
    for payload in _payloads(records):
        event = payload.get("event")
        if event == "poller_cycle":
            cycles.append(float(payload.get("ms") or 0) / 1000.0)
            continue
        if event in EVENT_COUNTERS:
            # The three steps that are not `_phase`es. Wall clock where the record carries it
            # (`varsel_webhook_load` does), and the counters that make the row readable -- an emit
            # pass that produced nothing and one that was capped both cost little wall clock.
            bucket = steps.setdefault(str(event), {})
            if payload.get("ms") is not None:
                bucket.setdefault("wall_ms", []).append(float(payload["ms"]))
            for field in EVENT_COUNTERS[str(event)]:
                if payload.get(field) is not None:
                    bucket.setdefault(field, []).append(float(payload[field]))
            continue
        if event != "poller_phase":
            continue
        name = str(payload.get("phase") or "?")
        bucket = phases.setdefault(name, {"wall_ms": [], "cpu_ms": [], "rss_delta_bytes": [], "errors": []})
        bucket["wall_ms"].append(float(payload.get("ms") or 0))
        if payload.get("cpu_ms") is not None:
            bucket["cpu_ms"].append(float(payload["cpu_ms"]))
        if payload.get("rss_delta_bytes") is not None:
            bucket["rss_delta_bytes"].append(float(payload["rss_delta_bytes"]))
        bucket["errors"].append(1.0 if payload.get("result") == "error" else 0.0)

    def stat(values: list[float]) -> dict:
        if not values:
            return {"n": 0, "p50": None, "max": None}
        return {"n": len(values), "p50": round(statistics.median(values), 1), "max": round(max(values), 1)}

    return {
        "cycles": {
            "n": len(cycles),
            "p50_s": round(statistics.median(cycles), 1) if cycles else None,
            "max_s": round(max(cycles), 1) if cycles else None,
        },
        "phases": {
            name: {
                "wall_ms": stat(b["wall_ms"]),
                "cpu_ms": stat(b["cpu_ms"]),
                "rss_delta_bytes": stat(b["rss_delta_bytes"]),
                "errors": int(sum(b["errors"])),
            }
            for name, b in sorted(phases.items())
        },
        "steps": {
            # `records`, not `n`: `varsel_webhook_load` has its OWN `n` field (the subscriptions
            # it loaded), and a key called `n` here silently overwrote it -- the count of records
            # and the count of subscriptions are two numbers and the row needs both.
            name: {
                "records": len(next(iter(b.values()), [])),
                **{field: {"total": int(sum(values)), **stat(values)}
                   for field, values in sorted(b.items())},
            }
            for name, b in sorted(steps.items())
        },
    }


def render(summary: dict, *, tags: dict[str, str], receiver: dict | None = None) -> str:
    """One table plus one tagged headline. Text, because it goes into a markdown table by hand."""
    lines: list[str] = []
    cycles = summary["cycles"]
    lines.append(
        f"cycles: n={cycles['n']} p50={cycles['p50_s']}s max={cycles['max_s']}s   "
        + "  ".join(f"{k}={v}" for k, v in sorted(tags.items()) if v)
    )
    lines.append(f"{'phase':<28}{'n':>5}{'wall p50':>10}{'wall max':>10}{'cpu p50':>9}{'cpu max':>9}{'rss p50':>12}{'err':>5}")
    for name, phase in summary["phases"].items():
        def cell(block: dict, key: str) -> str:
            value = block[key]
            return "-" if value is None else f"{value:,.0f}"
        lines.append(
            f"{name:<28}{phase['wall_ms']['n']:>5}"
            f"{cell(phase['wall_ms'], 'p50'):>10}{cell(phase['wall_ms'], 'max'):>10}"
            f"{cell(phase['cpu_ms'], 'p50'):>9}{cell(phase['cpu_ms'], 'max'):>9}"
            f"{cell(phase['rss_delta_bytes'], 'p50'):>12}{phase['errors']:>5}"
        )
    if summary.get("steps"):
        lines.append("")
        lines.append("steps that are not phases (their `ms` appears in no phase breakdown):")
        for name, step in summary["steps"].items():
            fields = "  ".join(
                f"{field}={block['total']} (p50 {block['p50']})"
                for field, block in step.items()
                if isinstance(block, dict)
            )
            lines.append(f"  {name:<24}records={step['records']}  {fields}")
    lines.append("")
    lines.append(
        "kix_poller_phase_cpu_seconds and its duration twin are in THANOS behind Grafana, not in "
        "Google Managed Prometheus -- no script can scrape them. The sub-pass and Firestore spans "
        "(firestore_transaction.cpu_ms is KTD2's number) are in Cloud Trace, not in any log line."
    )
    if receiver is not None:
        lines.append("")
        lines.append("receiver: " + json.dumps(receiver.get("configured", {})))
        lines.append(
            f"  accepted={receiver.get('accepted')} refused={receiver.get('refused')} "
            f"duplicates={receiver.get('duplicate_posts')} "
            f"distinct={receiver.get('distinct_deliveries')} "
            f"rate={receiver.get('posts_per_second')}/s over {receiver.get('span_seconds')}s"
        )
    return "\n".join(lines)


# ---- The local profile -------------------------------------------------------------------------


def profile_report(stats, *, definitions: int, top: int = 5) -> dict:
    """Interpreter ms per definition, and the five functions with the most self time.

    Self time (``tottime``), never cumulative: the question KTD17 asks is where the interpreter is,
    and cumulative time double-counts every caller of the function that actually holds it.
    """
    total_s = stats.total_tt
    rows = []
    for func, (_calls, primitive, tottime, cumtime, _callers) in stats.stats.items():
        rows.append((tottime, cumtime, primitive, func))
    rows.sort(reverse=True)
    return {
        "definitions": definitions,
        "total_interpreter_s": round(total_s, 3),
        "ms_per_definition": round(total_s * 1000 / definitions, 4) if definitions else None,
        "top_by_self_time": [
            {
                "function": f"{path.rsplit('/', 1)[-1]}:{line}({name})",
                "self_ms": round(tottime * 1000, 1),
                "cumulative_ms": round(cumtime * 1000, 1),
                "calls": primitive,
            }
            for tottime, cumtime, primitive, (path, line, name) in rows[:top]
        ],
    }


def run_profile(definitions: int, *, top: int = 5) -> dict:
    """Profile one pass over `definitions` synthetic registrations, in-process.

    Local and hermetic: the prediction cache is stubbed, nothing is pushed and nothing is written,
    so this measures the interpreter and only the interpreter — which is the point. It reuses the
    /varsel-api suite's own stubbing seams rather than inventing a second set, because a second set
    would drift from the pass it claims to measure.

    This deliberately does NOT stand in for a dev run, and the difference is worth stating rather
    than discovering: the prediction cache here is the test suite's one-situation snapshot, so the
    absolute ms-per-definition is a FLOOR and not the evening peak's figure. What it answers is
    "which functions hold the interpreter", which is the one question the cluster's per-phase
    timings cannot answer per definition. The evening-cache version of it needs a capture of a real
    `call_predictions_today` from a dev run; until that exists, quote this number as a floor and
    the cluster's `varsel_notify` cpu_ms as the real one.
    """
    import cProfile
    import os
    import pstats

    api = pathlib.Path(__file__).resolve().parents[1] / "disruptions" / "api"
    if str(api) not in sys.path:
        sys.path.insert(0, str(api))
    # **The network fences, set BEFORE the modules are imported.** `alternatives` and
    # `webhook_sender` both allow a real request unless the variable is falsy OR the process looks
    # like a test process -- and it decides that from `sys.argv[0]`, which here is `bench_cycle.py`.
    # So this profile, run unfenced, opened real Journey Planner and Vertex connections: measured
    # before the fences went in, four TLS handshakes and 1 616 socket reads inside a run that is
    # supposed to be measuring the interpreter. It made the report wrong (most of the self time was
    # in `_ssl` and `BufferedReader`) and it sent a laptop's traffic to a live upstream.
    os.environ.setdefault("KIX_ALLOW_JP_NETWORK", "0")
    os.environ.setdefault("KIX_ALLOW_WEBHOOK_NETWORK", "0")
    # Not a fence but the same intent: `off` is the kill switch, so no backend is even asked.
    os.environ["VARSEL_LLM_PROVIDER"] = "off"

    # The suite sets its VAPID and environment variables at import and stubs BigQuery before main;
    # importing it is what gives this profile the same pass the tests drive, rather than a second
    # set of stubs that would drift from it.
    import decisions
    import test_varsel_api as suite  # type: ignore[import-not-found]
    import varsel

    suite._reset_state()
    # The two remaining writes. A push goes to a real push service and a decision row to BigQuery;
    # neither is interpreter time this profile is asking about, and both would be real side effects
    # of a measurement.
    varsel._send_push = lambda subscription, payload: "sent"
    decisions.record = lambda rows: 0
    for i in range(definitions):
        device = f"profile-{i:06d}"
        varsel._devices[device] = {"subscription": None, "created_ts": "2026-01-01T00:00:00+00:00"}
        varsel._journeys[f"pj-{i:06d}"] = {
            "device_id": device,
            "from_stop": "A",
            "to_stop": "B",
            "begin": "00:00",
            "end": "23:59",
            "days": [0, 1, 2, 3, 4, 5, 6],
            "created_ts": "2026-01-01T00:00:00+00:00",
        }
    profiler = cProfile.Profile()
    profiler.enable()
    varsel.check_and_notify(suite.SNAPSHOT)
    profiler.disable()
    return profile_report(pstats.Stats(profiler), definitions=definitions, top=top)


# ---- CLI -----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command")

    read = sub.add_parser("read", help="Per-phase timings from Cloud Logging (the default).")
    read.add_argument("--environment", default="dev", choices=sorted(CLUSTER_PROJECTS))
    read.add_argument("--since", default="2h", help="gcloud --freshness window, e.g. 90m, 6h, 1d.")
    read.add_argument("--limit", type=int, default=5000)
    read.add_argument("--receiver", type=pathlib.Path, help="loadgen_receiver.py's record file.")
    read.add_argument("--shape", default="", choices=("", "evening", "morning"),
                      help="Which peak this run measured. The EVENING shape is a live dev run at "
                           "22:00-01:00 Oslo, when the prediction cache is at its daily maximum. "
                           "The MORNING shape needs a bad-morning prediction cache, which no "
                           "script here can synthesize -- it is a tag, not a switch.")
    read.add_argument("--ratio", default="", choices=("", "plan", "prd"))
    read.add_argument("--via", default="", choices=("", "api", "store"))
    read.add_argument("--subscriptions", type=int, help="Seeded subscriptions, for the row.")
    read.add_argument("--definitions", type=int, help="Distinct definitions, for the row.")
    read.add_argument("--pairs", type=int, help="Distinct canonical pairs, for the row.")
    read.add_argument("--json", action="store_true", help="Print the summary as JSON.")

    prof = sub.add_parser("profile", help="Interpreter ms per definition, locally.")
    prof.add_argument("--definitions", type=int, default=1000)
    prof.add_argument("--top", type=int, default=5)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "profile":
        print(json.dumps(run_profile(args.definitions, top=args.top), indent=2))
        return 0
    if args.command is None:
        build_parser().print_help()
        return 2
    project = CLUSTER_PROJECTS[args.environment]
    summary = summarise(read_records(project, args.since, limit=args.limit))
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0
    receiver = json.loads(args.receiver.read_text()) if args.receiver else None
    print(
        render(
            summary,
            tags={
                "env": args.environment,
                "shape": args.shape,
                "ratio": args.ratio,
                "via": args.via,
                "source": "log",
                # The load the phases were measured under. Passed in rather than inferred: nothing
                # in a log record says which load produced it, and a phase timing without its
                # registration counts is a number with no denominator.
                "subs": args.subscriptions or "",
                "defs": args.definitions or "",
                "pairs": args.pairs or "",
            },
            receiver=receiver,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
