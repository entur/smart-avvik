"""Cross-cutting guards on the webhook channel (plan 2026-09-08-001, U8).

Everything here belongs to no single unit, which is why it exists as its own suite rather than as
three more tests in `test_notification_store.py`. Two questions run through all of it.

**AE11: is client data absent from every public API, page, export and routine log?** Each unit
proves its own half — `test_notification_api` proves one client cannot read another's records,
`test_varsel_api` proves the GCS state blob is byte-identical to a cycle with no webhook recipients,
`test_historikk` and `test_meldinger` each grep their own SQL. What none of them can prove is the
*negative over the whole repository*: that no query, no front-end bundle, no decision-log column and
no structured log field anywhere names a client field. That is a whole-tree question and it is
asked here.

**R19: does the published retention text still match the code?** The clause-by-clause check was a
reading, done once, recorded in `docs/operations/notification-webhooks.md`. A reading decays. So that
document carries a machine-readable table of every published number beside the constant that defines
it, and this suite parses the table and compares it against the modules — which turns the one-off
check into something that fails when either side moves. The same shape as
`test_personvern_texts.py`, and for the same reason: a published claim nothing verifies becomes a
published claim that is false.

Three things this suite deliberately does NOT do:

* **It does not import `main`.** Doing so needs a suite's BigQuery-stub preamble, and every question
  here about `main` is answerable from its source text — whether a flag is read at import or inside
  a function is a fact about the AST, not about a running process.
* **It does not re-prove what a unit suite already proves.** Cross-client refusal, the state-blob
  round trip, the wire projection's allow-list and the SSO prefix tuple are each pinned where they
  belong; duplicating them here would make a change fail in two places and be fixed in one.
* **It does not check log VALUES, only log field NAMES.** A field named `reason` can still carry a
  refused destination's hostname, and one does — `webhook_sender`'s `webhook_delivery_refused`.
  That is published in the operations document rather than asserted away, because the value is
  built at the raise site and no static check can bound it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import notification_api
import notification_auth
import notification_events
import notification_store
import webhook_delivery
import webhook_sender
import webhook_signing

API_DIR = Path(__file__).resolve().parent
REPO_ROOT = API_DIR.parent.parent
DOCS = REPO_ROOT / "docs"
OPERATIONS_DOC = DOCS / "operations" / "notification-webhooks.md"
API_DOC = DOCS / "integrations" / "notification-api.md"
BFF_DOC = DOCS / "integrations" / "firebase-bff.md"

#: The modules the channel is made of. `notification_wording` is included even though it also serves
#: the browser: it is the one module both surfaces write through, so a leak added there would reach
#: the notification API path by default rather than by intent.
NOTIFICATION_MODULES = (
    "notification_store.py",
    "notification_auth.py",
    "notification_api.py",
    "notification_events.py",
    "webhook_delivery.py",
    "webhook_signing.py",
    "webhook_sender.py",
    "notification_wording.py",
)

MODULES_BY_NAME = {
    "notification_store": notification_store,
    "notification_auth": notification_auth,
    "notification_api": notification_api,
    "notification_events": notification_events,
    "webhook_delivery": webhook_delivery,
    "webhook_signing": webhook_signing,
    "webhook_sender": webhook_sender,
}


def _source(name: str) -> str:
    return (API_DIR / name).read_text(encoding="utf-8")


def _tree(name: str) -> ast.Module:
    return ast.parse(_source(name), filename=name)


# ---- R19: the published numbers ----------------------------------------------------------------

_NUMBER_ROW = re.compile(r"^\|\s*`([a-z_]+)\.([A-Za-z_][A-Za-z0-9_]*)`\s*\|\s*([0-9.]+)\s*\|\s*$")


def _published_numbers() -> list[tuple[str, str, str]]:
    """Every row of the operations document's constants table, as (module, name, printed value)."""
    rows = []
    for line in OPERATIONS_DOC.read_text(encoding="utf-8").splitlines():
        match = _NUMBER_ROW.match(line.strip())
        if match:
            rows.append((match.group(1), match.group(2), match.group(3)))
    return rows


def test_the_published_numbers_table_matches_the_constants():
    """R19's clause-by-clause check, made durable.

    The check itself is a reading: each published clause beside the function that makes it true. A
    reading is only ever true on the day it is done, so the numbers half of it is pinned here. The
    table is parsed rather than hand-listed in this file for the same reason the constants are not
    copied into the prose: a second copy is a second thing to forget.
    """
    rows = _published_numbers()
    assert len(rows) >= 35, (
        f"the operations document's constants table parsed to only {len(rows)} rows. Either the "
        "table lost most of its content or its formatting changed — a parser that silently matches "
        "nothing is how a guard becomes decoration."
    )
    for module_name, constant, printed in rows:
        module = MODULES_BY_NAME.get(module_name)
        assert module is not None, (
            f"the constants table names module {module_name!r}, which this suite does not import. "
            "Add it to MODULES_BY_NAME, or the row is unchecked."
        )
        assert hasattr(module, constant), (
            f"{module_name}.{constant} is published in docs/operations/notification-webhooks.md and "
            "does not exist. A renamed constant leaves the published number describing nothing."
        )
        actual = getattr(module, constant)
        expected = float(printed) if "." in printed else int(printed)
        assert actual == expected, (
            f"{module_name}.{constant} is {actual!r} and the operations document publishes "
            f"{expected!r}. Change the document in the same commit as the constant: every number "
            "there is quoted to a client, and several of them are retention promises."
        )


def test_every_retention_constant_is_published():
    """The other direction: a retention number that exists and is published nowhere.

    R19 is a claim about what is PUBLISHED matching the code, so a constant nobody published cannot
    make the published text false — but it can make it incomplete, which is the same failure one
    step earlier. These six are the ones a client's own retention question is answered from.
    """
    published = {f"{module}.{name}" for module, name, _ in _published_numbers()}
    for constant in (
        "HISTORY_DAYS",
        "SUBSCRIPTION_MAX_LIFETIME_DAYS",
        "SUBSCRIPTION_INACTIVITY_DAYS",
        "EVENT_VALIDITY_MINUTES",
        "TTL_RESIDUAL_HOURS",
        "DELIVERED_MARK_TTL_H",
    ):
        assert f"notification_store.{constant}" in published, (
            f"notification_store.{constant} bounds how long client data survives and is published "
            "nowhere. Add it to the constants table in docs/operations/notification-webhooks.md."
        )


def test_the_published_worst_case_residual_is_the_sum_and_not_the_ttl_lag():
    """The number a reader gets wrong, so it is stated and checked.

    `TTL_RESIDUAL_HOURS` (24) is the TTL process's LAG, and it is what the DELETE response returns.
    It is not the residual for a cleanup that never completes: those documents keep their own
    `HISTORY_DAYS` window first, and the tombstone keeps its route and the client's reference until
    `_finish_cleanup_body(done=True)` redacts them. So the worst case is 7 days plus the lag — about
    eight — and publishing the 24 alone would understate it by a week.
    """
    assert notification_store.TTL_RESIDUAL_HOURS == 24
    days = notification_store.HISTORY_DAYS + notification_store.TTL_RESIDUAL_HOURS / 24
    assert days == 8, f"the published residual is stated as about 8 days; the code now gives {days}"
    text = OPERATIONS_DOC.read_text(encoding="utf-8")
    assert "about **8 days**" in text or "about 8 days" in text, (
        "the operations document no longer publishes the ~8-day worst-case residual. It is the "
        "number R19's own verification line asks for, and it is not the 24 hours beside it."
    )
    assert "about **8 days**" in API_DOC.read_text(encoding="utf-8"), (
        "the client-facing document no longer publishes the ~8-day worst-case residual"
    )


def test_the_receivers_two_numbers_match_the_typescript_source():
    """The receiver's ceiling and cutoff are TypeScript constants and are published in prose here.

    Neither is in the plan, and the cutoff in particular is a product choice a client has to be told
    about rather than discover: it cannot tell a bug from an outage, so a store outage longer than
    fifteen minutes expires every event accepted during it, `valid_until` notwithstanding.
    """
    client = (
        REPO_ROOT / "integrations" / "notifications-client" / "src" / "client.ts"
    ).read_text(encoding="utf-8")
    body_bytes = re.search(r"DEFAULT_MAX_BODY_BYTES\s*=\s*([0-9]+)\s*\*\s*([0-9]+)", client)
    cutoff = re.search(r"DEFAULT_PROCESSING_CUTOFF_MS\s*=\s*([0-9]+)\s*\*\s*([0-9]+)", client)
    assert body_bytes and cutoff, (
        "could not read DEFAULT_MAX_BODY_BYTES / DEFAULT_PROCESSING_CUTOFF_MS out of client.ts. "
        "A regex that matches nothing passes every assertion below it; fix the regex rather than "
        "deleting the check."
    )
    assert int(body_bytes.group(1)) * int(body_bytes.group(2)) == 64 * 1024, (
        "the receiver's inbound ceiling is no longer 64 KiB; the published documents say it is"
    )
    assert int(cutoff.group(1)) * int(cutoff.group(2)) == 15 * 60, (
        "the receiver's processing cutoff is no longer 15 minutes; both published documents say it "
        "is, and say what it costs during an outage"
    )
    for doc in (API_DOC, BFF_DOC):
        text = doc.read_text(encoding="utf-8")
        assert "64 KiB" in text, f"{doc.name} no longer publishes the 64 KiB ceiling"
        assert "15-minute" in text or "fifteen minutes" in text, (
            f"{doc.name} no longer publishes the 15-minute processing cutoff"
        )


# ---- AE11: no client data in a routine log ----------------------------------------------------

#: Field names that would put a route, a callback, a credential or prose into Cloud Logging. Chosen
#: as names rather than as a value scan because a name is what a later edit adds: `extra={"fields":
#: {..., "callback_url": url}}` is one plausible line away at every site that already logs a
#: `destination_id`.
FORBIDDEN_LOG_FIELDS = frozenset(
    {
        "url",
        "callback_url",
        "callback",
        "endpoint",
        "from_stop",
        "to_stop",
        "from_name",
        "to_name",
        "external_ref",
        "begin",
        "end",
        "days",
        "departure_time",
        "title",
        "body",
        "message",
        "prose",
        "notification",
        "payload",
        "secret",
        "pepper",
        "signing_root",
        "signature",
        "authorization",
        "bearer",
        "journey_digest",
    }
)


def _log_field_keys(name: str) -> set[str]:
    """Every key of every `extra={"fields": {...}}` dict literal in one module."""
    keys: set[str] = set()
    for node in ast.walk(_tree(name)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                continue
            for outer_key, outer_value in zip(keyword.value.keys, keyword.value.values):
                if (
                    isinstance(outer_key, ast.Constant)
                    and outer_key.value == "fields"
                    and isinstance(outer_value, ast.Dict)
                ):
                    for field_key in outer_value.keys:
                        if isinstance(field_key, ast.Constant) and isinstance(field_key.value, str):
                            keys.add(field_key.value)
    return keys


def test_no_notification_module_logs_a_route_a_callback_a_credential_or_prose():
    """R16 over the structured log fields, statically.

    The units each argued this in prose at their own call sites. Prose is what a later edit does not
    read, and a log field is exactly the kind of thing added while debugging and never removed —
    which is how a traveller's origin and destination end up in a retention policy nobody chose.
    """
    found: dict[str, set[str]] = {}
    seen: set[str] = set()
    for module in NOTIFICATION_MODULES:
        keys = _log_field_keys(module)
        seen |= keys
        offending = keys & FORBIDDEN_LOG_FIELDS
        if offending:
            found[module] = offending
    # Non-vacuity, because an AST walk that stops matching passes silently and forever. 51 keys on
    # the day this was written; the floor is deliberately well under that, since the point is to
    # catch a walk that broke rather than to freeze the field set.
    assert len(seen) > 20 and {"event", "result", "client_id"} <= seen, (
        f"the log-field walk found only {len(seen)} keys ({sorted(seen)}). It has stopped seeing "
        "`extra={'fields': {...}}` literals, so every assertion below it is vacuous."
    )
    assert not found, (
        f"client log records now carry forbidden fields: {found}. R16 keeps client records, "
        "credentials, callback URLs and generated prose out of routine logs; app logs live in the "
        "cluster project with its own retention, which is not a place a traveller's route belongs. "
        "If a field is genuinely needed, publish it in docs/operations/notification-webhooks.md's "
        "log table first and then narrow FORBIDDEN_LOG_FIELDS deliberately."
    )


def test_the_operations_document_names_every_notification_log_event():
    """A log event nobody documented is one nobody will look for during an incident.

    The failure this closes is the one the `/meldinger` rename already cost once: an event name that
    exists in the code and in no query, dashboard or runbook returns nothing rather than erroring.
    """
    emitted: set[str] = set()
    for module in (*NOTIFICATION_MODULES, "varsel.py"):
        for node in ast.walk(_tree(module)):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "event"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and (
                        value.value.startswith(("notification_", "webhook_"))
                        or value.value == "varsel_webhook_load"
                    )
                ):
                    emitted.add(value.value)
    # A floor, not just non-emptiness: after the 2026-09-09 rename this filter still said
    # `partner_` and matched only `varsel_webhook_load`, so the test passed while checking one
    # event of nineteen. The number is the size of the log table this test exists to keep whole.
    assert len(emitted) >= 18, f"only {len(emitted)} client log events were found: {sorted(emitted)}"
    text = OPERATIONS_DOC.read_text(encoding="utf-8")
    missing = sorted(name for name in emitted if f"`{name}`" not in text)
    assert not missing, (
        f"these client log events are emitted and documented nowhere: {missing}. Add them to the "
        "log table in docs/operations/notification-webhooks.md, with what each field carries — an "
        "operator reads that table during an incident, not the source."
    )


# ---- The dependency direction ------------------------------------------------------------------


def test_the_store_and_delivery_modules_import_no_poller_state():
    """The dependency runs one way, and the reason is R16 rather than tidiness.

    `varsel` carries ~147 KB of poller state, the device registry, the VAPID keypair and the
    decision-log writer, none of which a notification event may reach. `events` already pins
    this for itself; the same claim is what makes AE10 true by construction for `webhook_delivery`
    — "events are still delivered" in a deployment with no keypair — which nothing else asserts.
    """
    forbidden = {"varsel", "main", "impact", "llm_message", "decisions", "historikk", "meldinger"}
    for module in (
        "notification_store.py",
        "notification_auth.py",
        "notification_events.py",
        "webhook_delivery.py",
        "webhook_signing.py",
        "webhook_sender.py",
        "notification_wording.py",
    ):
        imported: set[str] = set()
        for node in _tree(module).body:  # module level only, deliberately
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                imported.add(node.module.split(".")[0])
        offending = imported & forbidden
        assert not offending, (
            f"{module} imports {sorted(offending)} at module level. A notification event may not reach "
            "the device registry, the VAPID keypair or the decision-log writer, and the import "
            "direction is what enforces it — `notification_wording` imports `llm_message` INSIDE "
            "functions for exactly this reason, so that a wording is still produced when that "
            "module has failed."
        )


# ---- AE11: no client data in a query, an export or a page -------------------------------------

#: Identifiers that would mean notification state had reached a surface it must not. Deliberately
#: the store's FIELD names and the API prefix rather than its collection names or the words "client"
#: and "webhook": the collections are unprefixed (`subscriptions`, `events`, `credentials` ...) and
#: those words appear innocently in a browser bundle and in comments about the /demo audience, so a
#: token here has to be one that only this channel writes.
NOTIFICATION_TOKENS = (
    "client_id",
    "external_ref",
    "destination_id",
    "callback_url",
    "alert_subjects",
    "admin_audit",
    "notification-api",
)


def _hits(text: str) -> list[str]:
    return [token for token in NOTIFICATION_TOKENS if token in text]


def test_no_query_in_data_queries_names_a_notification_store_field_or_collection():
    """KTD10 as a property of the whole export surface, not of two pages' own SQL.

    `test_historikk` and `test_meldinger` each grep their own file. What neither can see is the
    nightly labelling job, the demo rebuilds, the togvarsel chain or a query added next month — and
    an export is exactly where a "just join it in for one report" edit lands.
    """
    offenders = {}
    for path in sorted((REPO_ROOT / "data" / "queries").rglob("*.sql")):
        hits = _hits(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        f"client identifiers reached checked-in SQL: {offenders}. Notification state is Firestore's "
        "(KTD10): `varsel_decisions` is written for every match including sub-threshold ones and "
        "/historikk and /meldinger publish it on public hosts, and its `pushed` column cannot tell "
        "a webhook from a Web Push, so a client column there corrupts both pages' meaning as well "
        "as their privacy claim."
    )


def test_no_front_end_bundle_names_a_notification_store_field_or_collection():
    """AE11's page half: no client route, reference or prose on any of the six front-ends."""
    offenders = {}
    for path in sorted((REPO_ROOT / "demo").rglob("*.ts*")):
        if "node_modules" in path.parts or "dist" in path.parts:
            continue
        hits = _hits(path.read_text(encoding="utf-8"))
        if hits:
            offenders[str(path.relative_to(REPO_ROOT))] = hits
    assert not offenders, (
        f"client identifiers reached a front-end bundle: {offenders}. /demo, /varsel, /historikk, "
        "/meldinger, /togmelding and /rushhistorikk are public pages; none of them has a client."
    )


def test_the_decision_log_names_no_notification_store_field():
    """The decision log and its outcomes table stay exactly as they were.

    Two reasons, and the second is operational rather than about privacy: adding a column to
    `varsel_decision_outcomes` breaks the registered scheduled query until it is re-registered in
    both environments, and the config holds a COPY of the SQL.
    """
    for relative in (
        "disruptions/api/decisions.py",
        "disruptions/api/create_varsel_decisions.sql",
        "data/queries/label_varsel_decisions.sql",
    ):
        path = REPO_ROOT / relative
        assert path.exists(), f"{relative} has moved; this guard now checks nothing"
        hits = _hits(path.read_text(encoding="utf-8"))
        assert not hits, (
            f"{relative} now names {hits}. Client decisions and deliveries are recorded in "
            "Firestore and never in `varsel_decisions` (KTD10) — and a new column in "
            "`varsel_decision_outcomes` also breaks the registered scheduled query in both "
            "environments until it is re-registered."
        )


# ---- The flags, and which of them needs a restart ----------------------------------------------


def test_the_channel_gate_is_read_at_call_time_and_the_wording_flag_at_import():
    """The published difference between the four flags, pinned where it is decided.

    The operations document tells an operator that `NOTIFICATION_WEBHOOKS_ENABLED` takes effect
    without a restart and that `NOTIFICATION_WEBHOOK_LLM_ENABLED` does not. Both halves are
    properties of WHERE the environment read sits, so both are checked as such: an import-time read
    of the gate would silently make "flip it and wait a cycle" wrong, and a call-time read of the
    wording flag would make the restart instruction merely superfluous — the harmless direction, but
    still a published claim that had stopped being true.
    """
    module_level_reads: set[str] = set()
    for node in ast.parse(_source("notification_wording.py")).body:
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Constant)
                and child.value == "NOTIFICATION_WEBHOOK_LLM_ENABLED"
                and isinstance(node, ast.Assign)
            ):
                module_level_reads.add("NOTIFICATION_WEBHOOK_LLM_ENABLED")
    assert module_level_reads == {"NOTIFICATION_WEBHOOK_LLM_ENABLED"}, (
        "NOTIFICATION_WEBHOOK_LLM_ENABLED is no longer read in a module-level assignment in "
        "notification_wording.py. The published rollout note says this flag needs a restart "
        "*because* it is read at import; move the note in the same change as the read."
    )

    main_source = _source("main.py")
    tree = ast.parse(main_source)
    inside_function = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if (
                    isinstance(child, ast.Constant)
                    and child.value == "NOTIFICATION_WEBHOOKS_ENABLED"
                ):
                    inside_function = True
    assert inside_function, (
        "NOTIFICATION_WEBHOOKS_ENABLED is not read inside a function in main.py. It is the channel "
        "gate and the operations document says it takes effect without a restart; an import-time "
        "read makes that instruction false and the failure is silent — the flag flips, the cycle "
        "logs nothing new, and the channel stays off."
    )


def test_the_named_database_never_falls_back_to_the_default_one():
    """The safe direction, and the reason the base Helm value must stay where it is.

    `notification_store._firestore_client()` raises rather than opening `(default)`. That is what makes an empty
    `NOTIFICATION_FIRESTORE_DATABASE` a loud failure instead of a silent one — and it is also what makes
    removing the base chart value dangerous, since a live flag flip would then become a no-op that
    logs one warning per cycle and reads exactly like a healthy deployment with nothing registered.
    """
    notification_store.reset_for_tests()
    assert notification_store.database_id() is None
    assert notification_store.enabled() is False
    try:
        notification_store._firestore_client()
    except notification_store.NotConfigured:
        pass
    else:  # pragma: no cover - the assertion below is the report
        raise AssertionError(
            "notification_store._firestore_client() no longer raises NotConfigured with no database configured. "
            "A fallback to `(default)` would write client records into a database no Terraform "
            "manages, with no TTL field config and no composite indexes."
        )


# ---- The published documents themselves --------------------------------------------------------


def test_the_three_published_documents_exist_and_point_at_each_other():
    """A document set whose cross-references rot is one nobody navigates.

    Cheap, and it is the check that fails when a file is renamed or moved — which is the one edit
    that breaks every link at once and none of the prose.
    """
    for doc in (API_DOC, BFF_DOC, OPERATIONS_DOC):
        assert doc.exists(), f"{doc} is missing"
    api = API_DOC.read_text(encoding="utf-8")
    bff = BFF_DOC.read_text(encoding="utf-8")
    operations = OPERATIONS_DOC.read_text(encoding="utf-8")
    assert "firebase-bff.md" in api and "notification-webhooks.md" in api
    assert "notification-api.md" in bff and "notification-webhooks.md" in bff
    assert "notification-api.md" in operations and "firebase-bff.md" in operations
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for name in ("notification-api.md", "firebase-bff.md", "notification-webhooks.md"):
        assert name in readme, f"README.md no longer signposts {name}"


def test_the_retention_table_names_only_symbols_that_exist():
    """R19's clause table cites code. A citation that no longer resolves is worse than none.

    Parsed rather than hand-listed: the value of the table is that a reader can go from a published
    clause to the function that keeps it, and a renamed function turns that into a dead end which
    reads exactly like a correct citation.
    """
    text = OPERATIONS_DOC.read_text(encoding="utf-8")
    section = text.split("## Retention and deletion, clause by clause")[1]
    section = section.split("### The two things deletion does not reach")[0]
    cited = set(re.findall(r"`(_?[a-z][a-z0-9_]*)\(\)`", section))
    cited |= set(re.findall(r"`(_[a-z][a-z0-9_]*)`(?! =)", section))
    assert len(cited) >= 6 and "_delete_body" in cited, (
        f"only {len(cited)} function names parsed out of the retention table ({sorted(cited)}). "
        "The table cites nine; a regex that has stopped matching turns this guard into decoration."
    )
    store_source = _source("notification_store.py")
    api_source = _source("notification_api.py")
    for name in sorted(cited):
        assert f"def {name}(" in store_source or f"def {name}(" in api_source, (
            f"the retention table cites `{name}`, which is defined in neither notification_store.py nor "
            "notification_api.py. Re-read the clause against the code that implements it now — a stale "
            "citation is how a published retention promise quietly stops being checked."
        )


def test_the_cap_exemptions_product_decision_is_published_beside_the_wider_code():
    """The exemption is wider than the requirement's wording, and that is a decision, not a detail.

    R11 exempts "a confirmation that replaces an earlier forecast about the same train". The store
    exempts any replacement of a DELIVERED revision, which includes a withdrawal — defensible (a
    retraction the cap suppressed leaves the traveller holding a warning we have withdrawn) and
    still not what was asked for. It was published as an open product-owner question with the pull
    request and DECIDED on 2026-09-09 (the wider rule stands), so what this pins now is that the
    operations document records that decision, dated, for as long as the code is the wider one —
    a narrowing of the code without a re-decision, or a re-decision without the code moving, is
    exactly the drift this exists to catch.
    """
    exempt = re.search(
        r"cap_exempt\s*=\s*(.+)", _source("notification_store.py")
    )
    assert exempt and "delivered_revision" in exempt.group(1), (
        "the cap exemption is no longer keyed on `delivered_revision`. If it has been narrowed to "
        "a confirmation, the 2026-09-09 decision in docs/operations/notification-webhooks.md has been "
        "reversed and the document must say so rather than keep recording the wider rule as decided."
    )
    text = OPERATIONS_DOC.read_text(encoding="utf-8")
    assert "withdrawal" in text, "the operations document no longer names the withdrawal case"
    assert "Decided 2026-09-09: the wider rule stands." in text, (
        "the operations document no longer records the product owner's decision on the generalised "
        "cap exemption beside the wider code"
    )
    assert "product-owner question" not in text, (
        "the operations document still flags the cap exemption as an OPEN product-owner question, "
        "which it stopped being on 2026-09-09"
    )


def test_the_two_halves_of_the_prompt_record_claim_are_both_published():
    """R16's prompt-record claim is true of webhook-only routes, and the docs must say which.

    A shared definition stays on the browser wording policy — one match and one wording between the
    channels means one call and it is the browser's, and that record would exist with no client
    channel at all. Publishing "no client route appears in the prompt log" would be false, and
    false in the direction that matters, so both halves are stated.
    """
    varsel_source = _source("varsel.py")
    assert "notification_wording.WEBHOOK" in varsel_source, (
        "varsel no longer routes any subject through notification_wording.WEBHOOK; the closed half "
        "of R16's prompt-record claim is what that policy is for"
    )
    assert "key not in by_journey_key" in varsel_source, (
        "the webhook-only discriminator has changed. It is `by_journey_key`, which is browser-only "
        "by construction (KTD10), so absence from it is exactly 'no browser registration holds "
        "this route'."
    )
    operations = OPERATIONS_DOC.read_text(encoding="utf-8")
    assert "VARSEL_LLM_LOG_PROMPTS" in operations, (
        "the operations document no longer names the flag under which the shared-route prompt "
        "record carries origin, destination and departure time"
    )
    flat = " ".join(operations.replace("**", "").split())
    assert 'It is not "no client route ever appears in the prompt log."' in flat, (
        "the operations document no longer states the half of R16 that is NOT closed. Publishing "
        "the closed half alone is the failure this check exists for: a shared route's prompt "
        "record exists, and a reader told only about `notification_wording.WEBHOOK` would conclude "
        "otherwise."
    )


def _run():
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} client-contract checks passed.")


if __name__ == "__main__":
    _run()
