#!/usr/bin/env python3
"""Load harness: put N synthetic webhook subscriptions and M synthetic browser registrations into
dev, then take them out again (U1 of the 100k plan, R16).

Every capacity claim in `docs/plans/2026-09-11-002-feat-scale-to-100k-commuters-plan.md` is gated
on a measurement from this tool plus `scripts/bench_cycle.py`. The point is not that the numbers
are pretty; it is that a later unit's "this fits in the cycle" is a reading rather than an
assertion.

    # What would be created, with no credentials and no network:
    python3 scripts/loadgen_subscriptions.py --environment dev plan --count 1000 --ratio prd

    # Create them (direct-store seeding; see "Two ways in" below):
    uv run --with google-cloud-firestore python scripts/loadgen_subscriptions.py \\
        --environment dev seed --count 1000 --destination loadtest-receiver

    # And take them out, including every collection they touched:
    uv run --with google-cloud-firestore python scripts/loadgen_subscriptions.py \\
        --environment dev teardown

**Two ways in, and the default is not the interesting one.**

``--via api`` drives ``POST /notification-api/v1/subscriptions`` with a real client credential, so
the create path itself — the spec validator, the quota check, the idempotent re-PUT — is exercised.
It is also the path that CANNOT reach the numbers this plan is about: ``_create_body`` counts a
client's live subscriptions by READING them inside the create transaction (which is why
``QUOTA_BOUNDS`` caps ``max_active_subscriptions`` at 10 000 and why U7 replaces it with a
``count()`` aggregation), and the per-client rate quota tops out at 10 000 a minute. At 100 000 the
seeding would take longer than the measurement and the ceiling would refuse it outright, so
Phase B's gate would be unrunnable.

``--via store`` therefore writes the subscription documents directly, through
``notification_store``'s OWN document builder (:func:`notification_store._new_subscription_doc`)
after :meth:`SubscriptionSpec.validate`, so a seeded document is byte-identical to one the API
would have written: same ``channel``, same ``daily_cap`` read off the client record, the same two
expiry horizons, the same ``definition_digest``. What it does NOT reproduce is the create path's
side effects — the in-transaction count, the ``notification_subscription`` log line, the inline
servability ask — and that is the whole trade. A benchmark row must therefore say which mode it
used, which is what :func:`Plan.summary` prints and what the operations table's "Mode" column is
for.

``--via auto`` (the default) picks ``api`` at or below :data:`MAX_VIA_API` and ``store`` above it,
so a small run still exercises the create path.

**The receiver must be a real public HTTPS endpoint.** ``webhook_sender`` requires ``https``, port
443 and a globally routable resolved address on EVERY attempt, and there is deliberately no flag,
environment variable or configuration that relaxes any of it — a bypass would then exist in the
code production runs, one variable away from turning webhook delivery into an SSRF primitive
(``test_webhook_sender.test_no_flag_or_environment_variable_bypasses_the_address_validation``
pins its absence). So ``scripts/loadgen_receiver.py`` is meant to be run behind a tunnel that
terminates TLS on a public name (``cloudflared tunnel``, ``ngrok http``, an ingress in the
cluster), and the destination is provisioned with THAT hostname. A ``localhost`` destination is
refused by the sender, correctly, and the refusal is not a bug to work around.

**It refuses production.** ``prd`` is not in :data:`PROJECTS`, and beyond that
:func:`resolve_target` exits non-zero BEFORE any request when the resolved project id ends in
``-prd``, when an exported ``GOOGLE_CLOUD_PROJECT`` disagrees with ``--environment`` (the
2026-09-09 lesson from ``manage_notification_client``: a stale shell variable is one deploy away
from being the target), or when a ``--base-url`` names a host that is not an explicitly known
non-production one. Synthetic registrations in prd would mean synthetic pushes to real devices and
synthetic rows in the decision log the pages publish.

**What the shape of the load is for.** Pairs are drawn from the picker's own stop list weighted by
scheduled calls, so the pair distribution resembles real commutes rather than a uniform draw over
every rural halt — matching cost follows which stops carry scored calls. Windows cluster on a few
commuter bands (:data:`WINDOW_CLUSTERS`) because KTD8's definition-half memo pays only if windows
cluster, and a uniform draw would answer that question with a "no" the load itself invented.
``--ratio`` picks how many subscriptions share a definition and how many definitions share a pair:
``plan`` is the first draft's ~2.9 and ~4.4 ("35 000 definitions on 8 000 pairs") and ``prd`` is
what prd's registrations actually measure, 1.22 and 1.41. R1 is sized on the range between them,
so a run that quotes one ratio and not the other has measured half the question.

``--seed`` makes a run reproducible: the same seed draws the same definitions, so two runs either
side of a code change differ by the code rather than by the draw.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import random
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass

# scripts/ drives modules that live in disruptions/api/, which is a flat import root rather than a
# package. manage_notification_client.py's own note applies verbatim.
_API = pathlib.Path(__file__).resolve().parents[1] / "disruptions" / "api"
if str(_API) not in sys.path:
    sys.path.insert(0, str(_API))

logger = logging.getLogger("loadgen_subscriptions")

#: The environments this tool may target. **`prd` is deliberately absent**, which is the cheapest
#: half of the refusal: an operator cannot even spell the production environment here. The other
#: half is :func:`resolve_target`, for the ways prd can arrive without being named.
ENVIRONMENTS = ("dev", "tst")
PROJECTS = {environment: f"ent-kixavvik-{environment}" for environment in ENVIRONMENTS}
BASE_URLS = {
    "dev": "https://kix-avvik.dev.entur.io",
    "tst": "https://kix-avvik.staging.entur.io",
}

#: The client every synthetic subscription belongs to. One client id, so teardown is a query rather
#: than a list somebody has to keep, and so a real client's rows can never be caught by it.
LOADTEST_CLIENT_ID = "loadtest"
#: The prefix every synthetic subscription id carries, for the same reason one layer down: even
#: inside the loadtest client, teardown deletes only what this tool made.
SUBSCRIPTION_PREFIX = "loadgen-"
#: And the device-id prefix on the browser side, which `scripts/make_device_report.py` renders and
#: which teardown matches on.
DEVICE_PREFIX = "loadgen-device-"

DATABASE_ENV = "NOTIFICATION_FIRESTORE_DATABASE"
DEFAULT_DATABASE = "notifications"

#: Above this, ``--via auto`` seeds through the store. The number is not a tuning knob: it is well
#: under the create path's own ceilings (10 000 active subscriptions counted inside the create
#: transaction, 10 000 requests a minute) so that an ``api`` run finishes in a couple of minutes
#: and stays a check of the create path rather than a load test of it.
MAX_VIA_API = 2000

#: How long a seeded subscription must stay alive for a measurement to run against it. Nothing
#: here shortens the store's own horizons -- a seeded document carries exactly the horizons the API
#: would have written -- this is only what the tool warns about.
EXPECTED_RUN_MINUTES = 180


@dataclass(frozen=True)
class Ratio:
    """How the load clusters. See the module docstring."""

    journeys_per_definition: float
    definitions_per_pair: float


#: prd's are measured (2026-09-11); the plan's are the first draft's assumption, kept because R1 is
#: sized on the RANGE between them and a harness that could only run one of them would leave half
#: of every capacity claim unmeasured.
RATIOS = {
    "plan": Ratio(2.9, 4.4),
    "prd": Ratio(1.22, 1.41),
}

@dataclass(frozen=True)
class Band:
    """One commuter window band, and how many 15-minute variants of it exist.

    Two axes, because the load has to answer two different questions. The BAND is the clustering:
    KTD8's definition-half memo is keyed on (pair, window tuple, weekday set) and pays only if
    windows cluster, so a uniform draw over the day would answer that with a "no" the generator
    invented. The VARIANTS are the granularity the plan's own Open Question is about — "the app's
    window granularity decides it: free minutes, or a picker rounded to 15 minutes" — and they are
    also what makes distinct definitions possible at all: six fixed bands over 200 pairs is 1 200
    definitions, so a run asking for more than that could not have them.
    """

    begin: str
    end: str
    days: tuple[int, ...]
    weight: int
    variants: int


#: The step a variant moves the band by: 15 minutes, the picker granularity the Open Question names
#: as one of its two candidates. The other candidate, free minutes, is `--window-step 1`.
WINDOW_STEP_MIN = 15
_LAST_MINUTE = 23 * 60 + 59

WINDOW_CLUSTERS: tuple[Band, ...] = (
    Band("06:30", "08:30", (0, 1, 2, 3, 4), 30, 8),  # the morning commute
    Band("07:00", "09:00", (0, 1, 2, 3, 4), 25, 8),
    Band("15:00", "17:00", (0, 1, 2, 3, 4), 20, 8),  # the afternoon one
    Band("16:00", "18:00", (0, 1, 2, 3, 4), 15, 8),
    Band("00:00", "23:59", (0, 1, 2, 3, 4, 5, 6), 7, 1),  # "tell me about anything"
    Band("09:00", "14:00", (1, 3), 3, 4),  # the long tail
)


def _hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def window_variants(band: Band, *, step: int = WINDOW_STEP_MIN) -> list[tuple[str, str, tuple[int, ...]]]:
    """Every window this band can produce, centred on it and keeping its duration.

    A variant that would run past midnight or start before it is dropped rather than clamped:
    `SubscriptionSpec.validate` refuses an overnight window and a clamped one would silently
    collapse two variants onto one, which would show up as a smaller definition count than the
    benchmark row claims.
    """
    begin, end = _minutes(band.begin), _minutes(band.end)
    out: list[tuple[str, str, tuple[int, ...]]] = []
    for variant in range(band.variants):
        offset = (variant - band.variants // 2) * step
        moved_begin, moved_end = begin + offset, end + offset
        if 0 <= moved_begin < moved_end <= _LAST_MINUTE:
            out.append((_hhmm(moved_begin), _hhmm(moved_end), band.days))
    return out


class Refused(RuntimeError):
    """The tool refuses to act. Exit code 2, and always before any request."""


@dataclass(frozen=True)
class Target:
    environment: str
    project: str
    base_url: str
    database: str


def resolve_target(
    environment: str,
    *,
    project: str | None = None,
    base_url: str | None = None,
    env_project: str | None = None,
    database: str | None = None,
) -> Target:
    """Where this run acts, or :class:`Refused` before anything has been sent.

    Four refusals, each for a way production can arrive without anybody typing "prd":

    * an ``--environment`` outside :data:`ENVIRONMENTS`;
    * a resolved project id ending in ``-prd`` — the belt to the missing-entry braces, and what
      the plan's test scenario names;
    * an exported ``GOOGLE_CLOUD_PROJECT`` that disagrees with ``--environment``. The Firestore
      client takes its project from the ambient environment, so without this check the flag picks
      the refusal and the shell variable picks the target — exactly the split
      ``manage_notification_client`` closed on 2026-09-09;
    * a ``--base-url`` whose host is not the one this environment publishes. ``--via api`` has a
      URL and may have no project at all, so the project checks above do not reach it.
    """
    if environment not in ENVIRONMENTS:
        raise Refused(
            f"--environment must be one of {', '.join(ENVIRONMENTS)}; this tool cannot target "
            "production, and that is not a flag to add: synthetic registrations in prd mean "
            "synthetic pushes to real devices and synthetic rows in the published decision log"
        )
    resolved = (project or PROJECTS[environment]).strip()
    if resolved.endswith("-prd"):
        raise Refused(f"refusing to act on a production project ({resolved})")
    ambient = (env_project or "").strip()
    if ambient and ambient != resolved:
        raise Refused(
            f"GOOGLE_CLOUD_PROJECT is {ambient!r} but --environment {environment} means "
            f"{resolved!r}; unset the variable or pass --project {ambient} deliberately"
        )
    url = (base_url or BASE_URLS[environment]).rstrip("/")
    if url not in {b.rstrip("/") for b in BASE_URLS.values()}:
        raise Refused(
            f"--base-url {url!r} is not one of this tool's known non-production hosts "
            f"({', '.join(sorted(BASE_URLS.values()))})"
        )
    if url != BASE_URLS[environment].rstrip("/"):
        raise Refused(f"--base-url {url!r} is not {environment}'s host")
    return Target(
        environment=environment,
        project=resolved,
        base_url=url,
        database=(database or os.environ.get(DATABASE_ENV) or DEFAULT_DATABASE).strip(),
    )


@dataclass(frozen=True)
class Definition:
    """One journey definition: the five fields both channels key on."""

    from_stop: str
    to_stop: str
    begin: str
    end: str
    days: tuple[int, ...]


@dataclass(frozen=True)
class Plan:
    """What a run would create. Produced without credentials, so ``plan`` is a real dry run."""

    definitions: tuple[Definition, ...]
    subscriptions: tuple[tuple[str, Definition], ...]
    ratio: str
    seed: int
    via: str = "auto"
    shape: str = "evening"

    @property
    def pairs(self) -> set[tuple[str, str]]:
        return {(d.from_stop, d.to_stop) for d in self.definitions}

    def summary(self) -> str:
        """The line a benchmark row is tagged with. Mode and ratio are ON it deliberately: a
        capacity number from a store-seeded run and one from an API-seeded run are not the same
        measurement, and neither is a `plan`-ratio run next to a `prd`-ratio one."""
        return (
            f"{len(self.subscriptions)} subscriptions / {len(self.definitions)} definitions / "
            f"{len(self.pairs)} pairs  (ratio={self.ratio}, via={self.via}, "
            f"shape={self.shape}, seed={self.seed})"
        )


def _weighted_places(stops: Sequence[dict]) -> list[tuple[str, int]]:
    """One (ref, weight) entry per physical place, weight = scheduled calls today.

    Collapsed on `place_id` keeping the busiest ref, which is `/varsel-api/stops`' own rule --
    the picker has already done this, and drawing two refs of one place would create definitions
    `varsel.create_journey` canonicalizes onto one, quietly halving the pair count a run reports.
    """
    best: dict[str, tuple[str, int]] = {}
    for row in stops:
        ref = row.get("stop_place")
        if not ref:
            continue
        calls = int(row.get("n_calls") or 0)
        place = row.get("place_id") or ref
        if place not in best or calls > best[place][1]:
            best[place] = (ref, calls)
    # +1 so a stop with no scheduled calls today can still be drawn rather than being impossible.
    return sorted((ref, calls + 1) for ref, calls in best.values())


def draw_definitions(
    stops: Sequence[dict],
    *,
    count: int,
    pairs: int | None = None,
    ratio: str = "prd",
    seed: int = 0,
    window_step: int = WINDOW_STEP_MIN,
) -> Plan:
    """`count` subscriptions over the drawn definitions and pairs. Pure, seeded, no I/O.

    Two ways to say how much clustering there is, and they do not mix:

    * ``--pairs N`` is the explicit form: exactly N distinct canonical pairs, one subscription per
      definition. It is what a test can assert on and what a sweep over pair counts uses.
    * ``--ratio`` derives both from the count, which is how a run reproduces prd's measured shape
      or the first draft's assumed one.
    """
    if count < 1:
        raise Refused("--count must be at least 1")
    if ratio not in RATIOS:
        raise Refused(f"--ratio must be one of {', '.join(sorted(RATIOS))}")
    places = _weighted_places(stops)
    if len(places) < 2:
        raise Refused(
            f"need at least two stop places to draw a pair, got {len(places)}; the stop list is "
            "read from /varsel-api/stops and is empty until the poller's first refresh"
        )
    shape = RATIOS[ratio]
    if pairs is None:
        n_definitions = max(1, round(count / shape.journeys_per_definition))
        n_pairs = max(1, round(n_definitions / shape.definitions_per_pair))
    else:
        if pairs < 1:
            raise Refused("--pairs must be at least 1")
        n_definitions, n_pairs = count, pairs
    n_pairs = min(n_pairs, n_definitions)

    rng = random.Random(seed)
    refs = [ref for ref, _ in places]
    weights = [weight for _, weight in places]
    drawn_pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    # Bounded, because a weighted draw over a small stop list can stop producing NEW pairs long
    # before the target: without the bound a 200-pair request against a 10-stop dev timetable
    # spins forever, which reads as a hung tool rather than as "ask for fewer pairs".
    for _ in range(n_pairs * 200):
        if len(drawn_pairs) >= n_pairs:
            break
        a, b = rng.choices(refs, weights=weights, k=2)
        if a == b or (a, b) in seen:
            continue
        seen.add((a, b))
        drawn_pairs.append((a, b))
    if len(drawn_pairs) < n_pairs:
        raise Refused(
            f"could only draw {len(drawn_pairs)} distinct pairs from {len(places)} places; ask "
            f"for at most {len(drawn_pairs)} with --pairs"
        )

    band_weights = [band.weight for band in WINDOW_CLUSTERS]
    variants = {band: window_variants(band, step=window_step) for band in WINDOW_CLUSTERS}
    capacity = len(drawn_pairs) * sum(len(v) for v in variants.values())
    if n_definitions > capacity:
        raise Refused(
            f"{n_definitions} distinct definitions cannot exist over {len(drawn_pairs)} pairs and "
            f"{capacity // max(1, len(drawn_pairs))} windows ({capacity} in all); ask for more "
            "pairs, or widen --window-step's variants"
        )
    # DISTINCT by construction, not by deduplication afterwards. A repeated (pair, window, days)
    # draw is a collision rather than a definition, and silently keeping the survivors would make
    # the run smaller than the number the benchmark row quotes -- which is the denominator R1's
    # per-definition budget is divided by, so the error would flatter every per-definition figure.
    definitions: list[Definition] = []
    seen_definitions: set[Definition] = set()
    attempts = 0
    while len(definitions) < n_definitions and attempts < n_definitions * 200:
        attempts += 1
        from_stop, to_stop = drawn_pairs[len(definitions) % len(drawn_pairs)]
        band = rng.choices(WINDOW_CLUSTERS, weights=band_weights, k=1)[0]
        options = variants[band]
        if not options:
            continue
        begin, end, days = options[rng.randrange(len(options))]
        candidate = Definition(from_stop, to_stop, begin, end, days)
        if candidate in seen_definitions:
            continue
        seen_definitions.add(candidate)
        definitions.append(candidate)
    if len(definitions) < n_definitions:
        raise Refused(
            f"drew only {len(definitions)} of {n_definitions} distinct definitions; the window "
            "draw is weighted, so a request close to the theoretical capacity starves -- ask for "
            "more pairs"
        )

    subscriptions = [
        (f"{SUBSCRIPTION_PREFIX}{i:06d}", definitions[i % len(definitions)]) for i in range(count)
    ]
    return Plan(
        definitions=tuple(definitions),
        subscriptions=tuple(subscriptions),
        ratio=ratio,
        seed=seed,
    )


# ---- Reading the stop list ---------------------------------------------------------------------


def fetch_stops(base_url: str, *, timeout: float = 30.0) -> list[dict]:
    """The picker's own stop list. Public and unauthenticated, like the picker's request."""
    with urllib.request.urlopen(f"{base_url}/varsel-api/stops", timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# ---- Seeding: through the API ------------------------------------------------------------------


def _api_call(
    base_url: str, method: str, path: str, *, key: str | None = None, body: dict | None = None
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{base_url}{path}", data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            payload = response.read().decode("utf-8")
            return response.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(payload)
            parsed = parsed if isinstance(parsed, dict) else {"detail": parsed}
        except ValueError:
            parsed = {"detail": payload[:200]}
        # `Retry-After` is a HEADER, not a body field, and a caller that has to honour it would
        # otherwise have to re-open this function to reach it. Lifted onto the dict rather than
        # widening the return shape, so every existing call site is unchanged.
        if exc.headers.get("Retry-After"):
            parsed.setdefault("retry_after", exc.headers["Retry-After"])
        return exc.code, parsed


#: The per-client request rate to assume when the client's own quota cannot be read. It is the
#: module default (`notification_auth.PER_CLIENT_PER_MIN`), not a guess: a tool that hammered past
#: it would spend the run collecting 429s.
DEFAULT_REQUESTS_PER_MIN = 120


def seed_via_api(
    plan: Plan,
    target: Target,
    *,
    destination_id: str,
    key: str,
    requests_per_min: int = DEFAULT_REQUESTS_PER_MIN,
    progress_every: int = 200,
    sleeper=time.sleep,
) -> dict[str, int]:
    """Create every subscription through the public create path, at the client's own rate.

    Bounded by construction and not by patience: the create path counts a client's live
    subscriptions inside its own transaction, so this mode is for exercising the path, never for
    reaching the plan's numbers. See the module docstring.

    **Throttled to the client's `requests_per_minute` quota.** A tight loop at `--count 1000`
    against the default 120 a minute collects 429s for most of the run: the outcomes dict would
    say so, but the run would have created a few dozen subscriptions and measured a rate limiter.
    A 429 is slept off once (honouring `Retry-After` when the server sends one) and retried, which
    is what a client's own integration would do.

    **An API-seeded subscription may well be unservable, and that is not a failure.** The create
    path asks BigQuery inline whether any planned trip connects the pair, under
    `main._NOTIFICATION_API_CONNECTIVITY_MAX_PER_MIN` (15) — well below this rate — so past the
    first fifteen a minute the answer is simply not established and `servable` stays null. The
    contract already tolerates that (null means "could not be established", never "no"), and it is
    a property of every API-seeded row rather than something a retry would fix.
    """
    outcomes: dict[str, int] = {}
    interval = 60.0 / max(1, requests_per_min)

    def _create(subscription_id: str, definition: Definition, index: int):
        return _api_call(
            target.base_url,
            "POST",
            "/notification-api/v1/subscriptions",
            key=key,
            body={
                "subscription_id": subscription_id,
                "destination_id": destination_id,
                "from_stop": definition.from_stop,
                "to_stop": definition.to_stop,
                "begin": definition.begin,
                "end": definition.end,
                "days": list(definition.days),
                "external_ref": f"loadgen/{index}",
                "test": True,
            },
        )

    for i, (subscription_id, definition) in enumerate(plan.subscriptions, start=1):
        status, body = _create(subscription_id, definition, i)
        if status == 429:
            # Once, not in a loop: a second 429 means the rate assumption is wrong, and grinding
            # against it would hide that behind a long run.
            outcomes["429_retried"] = outcomes.get("429_retried", 0) + 1
            sleeper(float(body.get("retry_after") or 60.0))
            status, body = _create(subscription_id, definition, i)
        outcomes[str(status)] = outcomes.get(str(status), 0) + 1
        if i % progress_every == 0:
            print(f"  ... {i}/{len(plan.subscriptions)} {outcomes}", file=sys.stderr)
        if i < len(plan.subscriptions):
            sleeper(interval)
    return outcomes


# ---- Seeding: straight into the store -----------------------------------------------------------


def store_documents(plan: Plan, *, destination_id: str, daily_cap: int, now) -> list[tuple[str, dict]]:
    """(id, document) for every subscription, built by the store's OWN builder.

    ``spec.validate()`` first, then :func:`notification_store._new_subscription_doc`, so what lands
    is what the API would have written — ``channel``, ``daily_cap``, both horizons, the definition
    digest — rather than this tool's idea of the schema. A hand-rolled document would drift the day
    a field is added, and it would drift SILENTLY: the pass reads documents through
    ``_subscription_from_doc``, whose per-document guard turns an unreadable one into a skipped
    one, so a seeded load would simply be smaller than the number in the benchmark row.
    """
    import notification_store

    built: list[tuple[str, dict]] = []
    for subscription_id, definition in plan.subscriptions:
        spec = notification_store.SubscriptionSpec(
            subscription_id=subscription_id,
            client_id=LOADTEST_CLIENT_ID,
            destination_id=destination_id,
            from_stop=definition.from_stop,
            to_stop=definition.to_stop,
            begin=definition.begin,
            end=definition.end,
            days=tuple(definition.days),
            external_ref=f"loadgen/{subscription_id}",
            test=True,
        )
        spec.validate()
        built.append(
            (subscription_id, notification_store._new_subscription_doc(spec, now, daily_cap=daily_cap))
        )
    return built


def bind_store(target: Target) -> None:
    """Point the store at the RESOLVED project, not at whatever ADC happens to name.

    `resolve_target` refuses production four ways, and every one of them judges an ARGUMENT. None of
    them reaches the Firestore client: `notification_store.firestore_client()` builds
    `firestore.Client(database=...)` with no project, so the project comes from ambient ADC — which
    means an exported `GOOGLE_CLOUD_PROJECT=ent-kixavvik-prd` would have this tool print that it is
    seeding dev while writing synthetic subscriptions into PRODUCTION, where they become real push
    notifications and real webhook deliveries to a real client's BFF.

    `scripts/manage_notification_client.py` already documents exactly this trap -- "GOOGLE_CLOUD_PROJECT
    would be one stale shell variable away from provisioning the wrong environment" -- and binds the
    variable before configuring the store. This does the same, and additionally hands the store a
    client built on the resolved project, so a caller that reaches `seed_via_store` or
    `teardown_store` without going through `main()` is bound too rather than relying on call order.
    """
    import notification_store

    os.environ["GOOGLE_CLOUD_PROJECT"] = target.project
    from google.cloud import firestore

    notification_store.configure(
        client=firestore.Client(project=target.project, database=target.database),
        database=target.database,
    )


def seed_via_store(plan: Plan, target: Target, *, destination_id: str) -> dict[str, int]:
    """Write the documents directly, in bulk.

    ``BulkWriter`` rather than a loop of ``set()`` calls, and not only for speed: Firestore ramps a
    collection's sustained write rate (500 a second, +50 % every five minutes), so a 100 000-document
    burst through individual writes spends most of its time being throttled with no backpressure
    anyone can see. The writer handles the retries.
    """
    import notification_auth
    import notification_store

    bind_store(target)
    client = notification_store.firestore_client()
    account = notification_auth.get_account(LOADTEST_CLIENT_ID)
    if account is None:
        raise Refused(
            f"client {LOADTEST_CLIENT_ID!r} does not exist in {target.project}; create it first "
            "with scripts/manage_notification_client.py create-client"
        )
    # The cap the API would have copied onto the document: the CLIENT's own, not the module
    # constant. Read here rather than defaulted so a seeded document and an API-written one agree
    # on the field `cap_remaining` is computed from -- the point of seeding through the store's own
    # builder is that the two are indistinguishable, and a defaulted cap would be the one field
    # that gave it away, silently, on the number the delivery ceiling is judged by.
    # And the destination must belong to the loadtest client. The API path cannot get this wrong --
    # `get_destination(..., client_id=...)` raises Forbidden for another client's -- but seeding
    # writes the document directly, so nothing would otherwise stop a mistyped `--destination` from
    # pointing 100 000 synthetic subscriptions at ANOTHER client's receiver, which then takes a load
    # test it never agreed to. Checked through the same tenancy-aware accessor rather than by
    # comparing a field here, so there is one definition of "owns".
    destination = notification_auth.get_destination(destination_id, client_id=LOADTEST_CLIENT_ID)
    if destination is None:
        raise Refused(
            f"destination {destination_id!r} does not exist under client {LOADTEST_CLIENT_ID!r} in "
            f"{target.project}; create it with scripts/manage_notification_client.py add-destination"
        )
    daily_cap = int(account.quotas.daily_cap)
    documents = store_documents(
        plan, destination_id=destination_id, daily_cap=daily_cap, now=notification_store._now()
    )
    writer = client.bulk_writer()
    collection = client.collection(notification_store.SUBSCRIPTIONS)
    for i, (subscription_id, document) in enumerate(documents, start=1):
        writer.set(collection.document(subscription_id), document)
        if i % 5000 == 0:
            print(f"  ... queued {i}/{len(documents)}", file=sys.stderr)
    writer.close()
    return {"written": len(documents), "daily_cap": daily_cap}


# ---- Browser registrations ----------------------------------------------------------------------

#: Registrations a minute on the browser side. `POST /varsel-api/journeys` asks BigQuery whether
#: any planned trip connects the pair (`main._CONNECTIVITY_MAX_PER_MIN`, 30) and then rewrites and
#: re-uploads the WHOLE `varsel_state.json` blob under the lock the poller needs — so M registrations
#: are M uploads of a growing object, serialized against the cycle. That cost is not an accident of
#: this tool: it is the defect R13 exists to remove, and the harness names it rather than hiding it
#: behind a fan-out that would simply stall the poller it is trying to measure.
BROWSER_REGISTRATIONS_PER_MIN = 30


def seed_browser_registrations(
    plan: Plan, target: Target, *, devices: int, sleeper=time.sleep
) -> dict[str, int]:
    """`devices` synthetic registrations: one journey each, under a generated device id.

    **No device record is created, and that is not a shortcut.** `POST /varsel-api/devices` requires
    a real `PushSubscription` with an endpoint, so registering one would mean either inventing an
    endpoint (which the poller would then try to POST to) or holding a real browser's — neither of
    which a load harness may do. `POST /varsel-api/journeys` does not require the device to exist,
    so a journey under an unknown device id is a legal registration and is exactly the shape the
    device report renders as "registered a journey and never turned notifications on": 79 of prd's
    186 journeys on 2026-09-04 were in it. Such a journey is matched like every other — only the
    delivery loop skips it — so it loads the pass that this harness is measuring, while sending
    nothing to any push service.

    What that means for a benchmark row: the browser side loads passes 1a, 1b, 1c and the decision
    rows, and NOT loop 2's fan-out. Loop 2 at scale is `_PUSH_WORKERS` against real push services
    and is out of this harness's reach by design.
    """
    outcomes: dict[str, int] = {}
    interval = 60.0 / max(1, BROWSER_REGISTRATIONS_PER_MIN)
    for i in range(devices):
        device_id = f"{DEVICE_PREFIX}{i:06d}"
        definition = plan.definitions[i % len(plan.definitions)]
        status, _ = _api_call(
            target.base_url,
            "POST",
            "/varsel-api/journeys",
            body={
                "device_id": device_id,
                "from_stop": definition.from_stop,
                "from_name": "Loadgen fra",
                "to_stop": definition.to_stop,
                "to_name": "Loadgen til",
                "begin": definition.begin,
                "end": definition.end,
                "days": list(definition.days),
            },
        )
        outcomes[f"journey:{status}"] = outcomes.get(f"journey:{status}", 0) + 1
        if i + 1 < devices:
            sleeper(interval)
    return outcomes


# ---- Teardown ------------------------------------------------------------------------------------

#: Every collection a synthetic subscription leaves rows in. All four are TOP LEVEL carrying a
#: subscription id -- which is the store's own design, because a document delete removes no
#: subcollection and a TTL field cascades into none -- so a subscriptions-only sweep would leave
#: events, deliveries and subjects behind, and the delivery query would keep returning loadtest rows
#: for as long as their TTL lasts.
TEARDOWN_COLLECTIONS = ("subscriptions", "events", "deliveries", "subjects")


def teardown_store(target: Target, *, batch: int = 400) -> dict[str, int]:
    """Delete every row this tool created, in all four collections."""
    import notification_store
    from google.cloud.firestore_v1.base_query import FieldFilter

    bind_store(target)
    client = notification_store.firestore_client()
    names = {
        "subscriptions": notification_store.SUBSCRIPTIONS,
        "events": notification_store.EVENTS,
        "deliveries": notification_store.DELIVERIES,
        "subjects": notification_store.SUBJECTS,
    }
    removed: dict[str, int] = {}
    for label in TEARDOWN_COLLECTIONS:
        collection = client.collection(names[label])
        total = 0
        cursor = None
        while True:
            # `filter=FieldFilter(...)`, the form `notification_store._query` uses: the
            # positional `where("client_id", "==", ...)` is deprecated and warns on every call.
            query = (
                collection.where(filter=FieldFilter("client_id", "==", LOADTEST_CLIENT_ID))
                .order_by("__name__")
                .limit(batch)
            )
            if cursor is not None:
                query = query.start_after(cursor)
            page = list(query.stream())
            if not page:
                break
            cursor = page[-1]
            # The client id narrows the read; the PREFIX decides the delete. SUBSCRIPTION_PREFIX
            # promises that "even inside the loadtest client, teardown deletes only what this tool
            # made", and until 2026-09-12 this swept the whole client instead -- so a subscription
            # someone had created by hand under `loadtest` to reproduce a bug was collateral. The
            # page is walked with a cursor rather than re-read from the top precisely because the
            # filter is applied in Python: a page where nothing matches must advance, not repeat.
            doomed = [
                snapshot
                for snapshot in page
                if str(snapshot.id).startswith(SUBSCRIPTION_PREFIX)
                or str(snapshot.to_dict().get("subscription_id", "")).startswith(
                    SUBSCRIPTION_PREFIX
                )
            ]
            if doomed:
                writer = client.batch()
                for snapshot in doomed:
                    writer.delete(snapshot.reference)
                writer.commit()
                total += len(doomed)
        removed[label] = total
    return removed


def teardown_browser(target: Target, *, devices: int) -> dict[str, int]:
    """«Slett alt» for each synthetic device: the endpoint's own erasure, not a partial delete."""
    outcomes: dict[str, int] = {}
    for i in range(devices):
        status, _ = _api_call(
            target.base_url, "DELETE", f"/varsel-api/devices/{DEVICE_PREFIX}{i:06d}"
        )
        outcomes[str(status)] = outcomes.get(str(status), 0) + 1
    return outcomes


# ---- CLI -----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--environment", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--project", help="Override the environment's project id (never prd).")
    parser.add_argument("--base-url", help="Override the environment's host.")
    parser.add_argument("--database", help=f"Firestore database (default ${DATABASE_ENV}).")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("plan", "Print what a run would create. No credentials, no network beyond the stop list."),
        ("seed", "Create the synthetic subscriptions and registrations."),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--count", type=int, default=1000, help="Subscriptions to create.")
        p.add_argument("--pairs", type=int, help="Exactly this many distinct pairs (overrides --ratio's derivation).")
        p.add_argument("--ratio", choices=sorted(RATIOS), default="prd")
        p.add_argument("--seed", type=int, default=0)
        p.add_argument("--window-step", type=int, default=WINDOW_STEP_MIN,
                       help="Minutes between window variants. 15 is a rounded picker, 1 is free "
                            "minutes -- which is the plan's open question about how much "
                            "definitions actually cluster.")
        p.add_argument("--shape", choices=("evening", "morning"), default="evening",
                       help="Which load shape this run is tagged with in the benchmark row.")
        p.add_argument("--devices", type=int, default=0, help="Synthetic browser registrations.")
        p.add_argument("--stops-file", type=pathlib.Path,
                       help="Read the stop list from a file instead of /varsel-api/stops.")
        if name == "seed":
            p.add_argument("--via", choices=("auto", "api", "store"), default="auto")
            p.add_argument("--destination", required=True,
                           help="A provisioned destination id whose callback is a PUBLIC https:443 host.")
            p.add_argument("--key", help="Client credential, for --via api (else $LOADGEN_API_KEY).")
            p.add_argument("--requests-per-min", type=int, default=DEFAULT_REQUESTS_PER_MIN,
                           help="The client's own requests_per_minute quota, which --via api is "
                                "throttled to. Raise it only after set-quotas has.")

    t = sub.add_parser("teardown", help="Delete everything this tool created.")
    t.add_argument("--devices", type=int, default=0, help="How many synthetic devices to erase.")
    t.add_argument("--skip-store", action="store_true", help="Only erase the browser side.")
    return parser


def _load_stops(args, target: Target) -> list[dict]:
    if getattr(args, "stops_file", None):
        return json.loads(args.stops_file.read_text())
    return fetch_stops(target.base_url)


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)
    try:
        target = resolve_target(
            args.environment,
            project=args.project,
            base_url=args.base_url,
            env_project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            database=args.database,
        )
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if args.command == "teardown":
        print(f"Tearing down in {target.environment} ({target.project})", file=sys.stderr)
        if not args.skip_store:
            print(json.dumps(teardown_store(target), indent=2))
        if args.devices:
            print(json.dumps(teardown_browser(target, devices=args.devices), indent=2))
        return 0

    try:
        plan = draw_definitions(
            _load_stops(args, target),
            count=args.count,
            pairs=args.pairs,
            ratio=args.ratio,
            seed=args.seed,
            window_step=args.window_step,
        )
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    via = getattr(args, "via", "auto")
    if via == "auto":
        via = "api" if args.count <= MAX_VIA_API else "store"
    plan = Plan(
        definitions=plan.definitions,
        subscriptions=plan.subscriptions,
        ratio=plan.ratio,
        seed=args.seed,
        via=via,
        shape=args.shape,
    )
    print(plan.summary())

    if args.command == "plan":
        window_mix: dict[str, int] = {}
        for definition in plan.definitions:
            label = f"{definition.begin}-{definition.end} {definition.days}"
            window_mix[label] = window_mix.get(label, 0) + 1
        print(json.dumps({"windows": window_mix}, indent=2))
        return 0

    print(
        f"Seeding {target.environment} via {via}; expect the run to hold for "
        f"~{EXPECTED_RUN_MINUTES} min",
        file=sys.stderr,
    )
    if via == "api":
        key = args.key or os.environ.get("LOADGEN_API_KEY")
        if not key:
            print("REFUSED: --via api needs --key or $LOADGEN_API_KEY", file=sys.stderr)
            return 2
        print(
            json.dumps(
                seed_via_api(
                    plan,
                    target,
                    destination_id=args.destination,
                    key=key,
                    requests_per_min=args.requests_per_min,
                ),
                indent=2,
            )
        )
    else:
        print(json.dumps(seed_via_store(plan, target, destination_id=args.destination), indent=2))
    if args.devices:
        print(json.dumps(seed_browser_registrations(plan, target, devices=args.devices), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
