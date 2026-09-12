#!/usr/bin/env python3
"""The load harness's webhook receiver: configurable latency and refusals, and a record of what
arrived (U1 of the 100k plan, R16).

R2 states the drain time as a formula of receiver latency, per-replica concurrency and replica
count, so the harness has to be able to SET the receiver's latency rather than discover it — 0.2 s
is the healthy case the formula is quoted at, 2 s is the slow one that makes the delivery pool's
width visible. It also has to be able to refuse: a 429 with ``Retry-After`` is what exercises the
per-delivery ``retry_not_before`` and (from U7) the per-destination backoff, and "the backoff
works" is not a claim to make from reading the code.

    python3 scripts/loadgen_receiver.py --port 8080 --latency 0.2 \\
        --refuse-every 20 --refuse-status 429 --retry-after 30 \\
        --record /tmp/loadgen_receiver.json

`scripts/bench_cycle.py --receiver /tmp/loadgen_receiver.json` prints the record beside the phase
timings, which is the point: a cycle that looks fast because every delivery was refused is not a
fast cycle.

**It must be reachable on a PUBLIC https : 443 hostname.** ``webhook_sender`` validates the scheme,
the port and every resolved address on every attempt, and there is deliberately no flag,
environment variable or configuration anywhere that relaxes any of it — a bypass would then exist
in the code production runs, one variable away from turning webhook delivery into an SSRF
primitive against the metadata service. Its absence is pinned by
``test_webhook_sender.test_no_flag_or_environment_variable_bypasses_the_address_validation``, and
it is not a thing to add for the harness's convenience.

So this server speaks plain HTTP on a high port and something else terminates TLS on a public name
in front of it — a tunnel is the intended shape:

    cloudflared tunnel --url http://localhost:8080     # prints https://<name>.trycloudflare.com
    ngrok http 8080                                    # prints https://<name>.ngrok-free.app

Provision the destination with THAT hostname (``scripts/manage_notification_client.py
add-destination``). A ``localhost`` destination is refused by the sender, correctly; the refusal is
not a bug to work around.

**The signature is not verified here** and that is deliberate rather than lazy: verifying it needs
the destination's signing secret in the harness's environment, which is a live credential
(``show-signing-secret`` is the one command whose output must not go into a ticket), and what this
receiver measures is delivery throughput. Signature verification has its own suites on both sides
(``test_webhook_signing.py``, ``integrations/notifications-client``). The record keeps the
``webhook-id`` so a duplicate POST — the at-least-once contract's one guarantee, and what U8's
lease bounds — is countable.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: A body bigger than this is counted and dropped rather than read: the receiver must not become
#: the harness's own memory problem at twenty thousand events.
MAX_BODY_BYTES = 256 * 1024


@dataclass
class Record:
    """What arrived. Counts and ids only — never a body, and never a header beyond the delivery id.

    An event payload carries a traveller's origin, destination and departure time plus the wording;
    a record file sits on a laptop and gets pasted into a benchmark row. So the file holds what a
    capacity measurement actually needs and nothing that identifies anybody.
    """

    accepted: int = 0
    refused: int = 0
    duplicates: int = 0
    malformed: int = 0
    first_at: float | None = None
    last_at: float | None = None
    latency_s: float = 0.0
    refuse_every: int = 0
    refuse_status: int = 429
    retry_after: int | None = None
    delivery_ids: set[str] = field(default_factory=set)

    def note(self, delivery_id: str, *, refused: bool) -> None:
        now = time.time()
        if self.first_at is None:
            self.first_at = now
        self.last_at = now
        if delivery_id:
            if delivery_id in self.delivery_ids:
                self.duplicates += 1
            self.delivery_ids.add(delivery_id)
        else:
            self.malformed += 1
        if refused:
            self.refused += 1
        else:
            self.accepted += 1

    def as_dict(self) -> dict:
        total = self.accepted + self.refused
        span = (
            (self.last_at - self.first_at)
            if self.first_at is not None and self.last_at is not None
            else 0.0
        )
        return {
            "accepted": self.accepted,
            "refused": self.refused,
            # At-least-once is the published contract, so a duplicate is not a failure — it is the
            # number U8's lease has to keep to "one per worker crash" rather than per attempt.
            "duplicate_posts": self.duplicates,
            "distinct_deliveries": len(self.delivery_ids),
            "posts_without_a_delivery_id": self.malformed,
            "span_seconds": round(span, 3),
            "posts_per_second": round(total / span, 2) if span > 0 else None,
            "configured": {
                "latency_s": self.latency_s,
                "refuse_every": self.refuse_every,
                "refuse_status": self.refuse_status,
                "retry_after": self.retry_after,
            },
        }


def _write_atomically(path: pathlib.Path, payload: dict) -> None:
    """Write `payload` as JSON so no reader ever sees a partial file: temp file, then rename."""
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2))
    os.replace(temp, path)


def make_handler(record: Record, lock: threading.Lock, record_path: pathlib.Path | None):
    """A handler class closed over the run's record. A class, because that is `http.server`'s API."""

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "kix-loadgen-receiver/1"

        def log_message(self, *_args) -> None:
            """Silent: one line per POST at twenty thousand events is the harness's own noise, and
            the record file is the measurement."""

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= MAX_BODY_BYTES:
                self.rfile.read(length)
            elif length > MAX_BODY_BYTES:
                self.rfile.read(length)  # drained, never parsed
            delivery_id = self.headers.get("webhook-id") or ""
            if record.latency_s:
                time.sleep(record.latency_s)
            with lock:
                n = record.accepted + record.refused + 1
                refuse = bool(record.refuse_every) and n % record.refuse_every == 0
                record.note(delivery_id, refused=refuse)
                # Written INSIDE the lock, and atomically. This server is threading, so two
                # handlers reach here at once as soon as anything is in flight -- and the first
                # version wrote the file after sending the response, outside the lock, where two
                # `write_text` calls interleaved and produced a file that failed to parse
                # ("Extra data", seen in this suite before the fix). A capacity run's record is
                # also read WHILE the run is going, so the rename is what keeps a reader from
                # catching a half-written one.
                if record_path is not None:
                    _write_atomically(record_path, record.as_dict())
            if refuse:
                self.send_response(record.refuse_status)
                if record.retry_after is not None:
                    self.send_header("Retry-After", str(record.retry_after))
            else:
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            """`/record` so a run can be watched without stopping it."""
            body = json.dumps(record.as_dict(), indent=2).encode("utf-8")
            self.send_response(200 if self.path == "/record" else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return _Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1", help="Bind address; put a tunnel in front.")
    parser.add_argument("--latency", type=float, default=0.2,
                        help="Seconds per POST. R2's formula is quoted at 0.2; 2.0 is the slow case.")
    parser.add_argument("--refuse-every", type=int, default=0,
                        help="Refuse every Nth POST (0 = never).")
    parser.add_argument("--refuse-status", type=int, default=429)
    parser.add_argument("--retry-after", type=int, default=30,
                        help="Retry-After on a refusal; the store caps what it honours at 300 s.")
    parser.add_argument("--record", type=pathlib.Path,
                        help="Write the running record here; bench_cycle.py reads it.")
    return parser


def serve(args) -> int:
    record = Record(
        latency_s=args.latency,
        refuse_every=args.refuse_every,
        refuse_status=args.refuse_status,
        retry_after=args.retry_after,
    )
    lock = threading.Lock()
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(record, lock, args.record)
    )
    print(
        f"Receiver on http://{args.host}:{server.server_address[1]} "
        f"(latency {args.latency}s, refusing every {args.refuse_every or 'never'}). "
        "Put a PUBLIC https:443 tunnel in front of it -- the sender validates scheme, port and "
        "every resolved address, and nothing bypasses that.",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    print(json.dumps(record.as_dict(), indent=2))
    return 0


def main(argv=None) -> int:
    return serve(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
