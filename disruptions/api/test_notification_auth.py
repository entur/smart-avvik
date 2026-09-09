"""Checks for notification_auth.py and for the operator tool that drives it.

Hermetic: no emulator, no socket, no credentials. Every store access in ``notification_auth`` goes
through the four-method :class:`notification_auth._Docs` seam, so an in-memory implementation exercises
the real hashing, the real two-tier limiter, the real negative cache and the real audit path -- and
the operator tool takes its authorizer and its store by injection, which is what lets scenario 10
run with no Google group and no GCP project.

Two things this file deliberately checks by GREPPING source rather than by behaviour, because no
runtime test can establish either. That ``notification_api`` never names the audit collection is R22's
"no client credential can read it", and it is a projection argument -- Firestore has no
per-collection IAM (KTD5), so nothing at runtime distinguishes "the route cannot read it" from
"this request happened not to". And that the audit collection is append-only is a property of every
write site in the repo, not of a document.
"""

from __future__ import annotations

import inspect
import io
import json
import logging
import os
import pathlib
import re
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import notification_auth
import notification_store
import webhook_signing

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
AUTH_SOURCE = (HERE / "notification_auth.py").read_text()
API_SOURCE = (HERE / "notification_api.py").read_text()
TOOL_PATH = REPO / "scripts" / "manage_notification_client.py"
TOOL_SOURCE = TOOL_PATH.read_text()

if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import manage_notification_client as tool

NOW = datetime(2026, 9, 8, 6, 30, tzinfo=UTC)
CLIENT = "acme"
DEST = "acme-prod"
#: 32 bytes exactly, which is `MIN_PEPPER_BYTES`: a shorter one must be refused, so the fixture
#: sits on the bar rather than comfortably above it.
PEPPER = "p" * 32
#: A globally routable literal. `webhook_sender.is_safe_address` refuses the RFC 5737
#: documentation ranges, so "example" addresses do not work here -- its docstring says so.
GOOD_URL = "https://hooks.acme.example.com/kix"
SIGNING_ROOT = "r" * 40


# ---- The in-memory document seam --------------------------------------------------------------


class _MemoryDocs:
    """:class:`notification_auth._Docs` over a dict, counting reads so "costs no store read" is checkable.

    ``fail_with`` is the store-outage injection: R20 wants a store failure to suppress the request
    and nothing wider, and the only way to check that at this layer is to make the store raise.
    """

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict[str, Any]] = {}
        self.reads = 0
        self.writes: list[tuple[str, str]] = []
        self.fail_with: BaseException | None = None

    def _check(self) -> None:
        if self.fail_with is not None:
            raise self.fail_with

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        self._check()
        self.reads += 1
        stored = self.data.get((collection, doc_id))
        return dict(stored) if stored is not None else None

    def set(self, collection: str, doc_id: str, doc: Any) -> None:
        self._check()
        self.writes.append((collection, doc_id))
        self.data[(collection, doc_id)] = dict(doc)

    def update(self, collection: str, doc_id: str, patch: Any) -> None:
        self._check()
        self.writes.append((collection, doc_id))
        self.data.setdefault((collection, doc_id), {}).update(dict(patch))

    def list_by(self, collection: str, field: str, value: Any, *, limit: int) -> list[dict]:
        self._check()
        self.reads += 1
        rows = [
            dict(doc)
            for (coll, _), doc in self.data.items()
            if coll == collection and doc.get(field) == value
        ]
        return rows[:limit]

    def audit(self) -> list[dict[str, Any]]:
        return [
            doc
            for (coll, _), doc in sorted(self.data.items())
            if coll == notification_auth.ADMIN_AUDIT
        ]


def _install(docs: _MemoryDocs | None = None, *, pepper: str | None = PEPPER) -> _MemoryDocs:
    store = docs or _MemoryDocs()
    if pepper is None:
        os.environ.pop(notification_auth.PEPPER_ENV, None)
    else:
        os.environ[notification_auth.PEPPER_ENV] = pepper
    notification_auth.configure(docs=store, clock=lambda: NOW)
    return store


def _uninstall() -> None:
    notification_auth.reset_for_tests()
    os.environ.pop(notification_auth.PEPPER_ENV, None)


def _provision(store: _MemoryDocs, *, url: str = GOOD_URL) -> str:
    notification_auth.create_client(CLIENT, name="Acme Reise AS", actor="operator@entur.org")
    notification_auth.create_destination(
        DEST, client_id=CLIENT, callback_url=url, actor="operator@entur.org"
    )
    _credential, bearer = notification_auth.issue_credential(CLIENT, actor="operator@entur.org")
    return bearer


# ---- Configuration and the pepper -------------------------------------------------------------


def test_an_unconfigured_deployment_is_not_a_bad_credential():
    """No store or no pepper is a fact about THIS deployment, and must not read as a 401.

    `authenticate` raises NotConfigured, which `notification_api._http` turns into 503 with a
    Retry-After. The failure direction matters: a 401 here sends a client hunting for a credential
    problem that is ours.
    """
    _install(pepper=None)
    try:
        assert notification_auth.store_ready() is True
        assert notification_auth.pepper_ready() is False
        assert notification_auth.available() is False
        raised = False
        try:
            notification_auth.authenticate("Bearer kixn_00000000000000000000.x")
        except notification_auth.NotConfigured:
            raised = True
        assert raised, "an unusable pepper must raise NotConfigured, not AuthFailed"
    finally:
        _uninstall()


def test_a_pepper_one_byte_short_is_refused():
    _install(pepper="p" * (notification_auth.MIN_PEPPER_BYTES - 1))
    try:
        assert notification_auth.pepper_ready() is False
    finally:
        _uninstall()


def test_a_pepper_written_with_echo_is_stripped_and_reported_once():
    """A trailing newline re-hashes every credential at once and reports nothing on its own.

    `feedback._env`'s lesson, and worse here: the eight silently dropped feedback submissions were
    one endpoint, while this byte would 401 every client simultaneously. Stripped rather than
    refused (whitespace is a transport artefact of how the payload was written), and WARNED once so
    the provisioning defect is findable.
    """
    _install(pepper=PEPPER + "\n")
    try:
        records: list[logging.LogRecord] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        notification_auth.logger.addHandler(handler)
        try:
            assert notification_auth.pepper_ready() is True
            first = notification_auth.secret_hash("kixn_0", "s")
            second = notification_auth.secret_hash("kixn_0", "s")
        finally:
            notification_auth.logger.removeHandler(handler)
        assert first == second
        # The stripped value is what hashes, so the newline cannot change a single hash.
        os.environ[notification_auth.PEPPER_ENV] = PEPPER
        assert notification_auth.secret_hash("kixn_0", "s") == first
        warnings = [r for r in records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1, "warned once per process, not per read"
    finally:
        _uninstall()


# ---- Hashing ----------------------------------------------------------------------------------


def test_the_hash_is_bound_to_its_key_id():
    """A hash lifted from one credential document must not verify under another `key-id`.

    Without the id in the preimage, one stolen row authenticates as every client whose row it is
    pasted into -- and nothing about the store would look wrong.
    """
    _install()
    try:
        secret = "the-same-secret"
        assert notification_auth.secret_hash("kixn_aaaaaaaaaaaaaaaaaaaa", secret) != (
            notification_auth.secret_hash("kixn_bbbbbbbbbbbbbbbbbbbb", secret)
        )
    finally:
        _uninstall()


def test_the_hash_is_hmac_sha256_and_never_bcrypt_or_argon2():
    """KTD8: HMAC-SHA256 under a pepper, because ~100 ms per comparison would be paid per request.

    Checked structurally rather than by timing: the module must import `hmac` and `hashlib` and
    must not name a password-hashing library at all.
    """
    assert "hmac.new(_pepper()" in AUTH_SOURCE
    assert "hmac.compare_digest" in AUTH_SOURCE
    # Over the IMPORT lines only. A bare substring search over the whole file matches the docstring
    # that explains why none of these is used -- test_notification_store's `_uncommented` lesson, in the
    # other direction: a negative grep that hits the explanation passes (or fails) for the wrong
    # reason.
    imports = [
        line
        for line in AUTH_SOURCE.splitlines()
        if line.startswith(("import ", "from ")) or " import " in line
    ]
    for forbidden in ("bcrypt", "argon2", "passlib", "scrypt", "pbkdf2", "hashlib.scrypt"):
        assert not any(forbidden in line.lower() for line in imports), forbidden
    assert len(notification_auth.secret_hash.__doc__ or "") > 100


def test_a_minted_credential_has_the_documented_shape():
    key_id, secret = notification_auth.mint_credential()
    assert notification_auth.KEY_ID_RE.match(key_id), key_id
    assert key_id.startswith(notification_auth.KEY_ID_PREFIX)
    assert len(secret) >= 40, "32 random bytes, urlsafe-base64 without padding"
    assert notification_auth.bearer_credential(key_id, secret) == f"{key_id}.{secret}"


# ---- The two-tier limiter and the negative cache ----------------------------------------------


def test_a_malformed_header_costs_no_store_read_and_no_global_slot():
    """The cheapest refusal in the path, and deliberately BEFORE the global tier.

    A flood of garbage headers then cannot spend the budget that protects the credential lookup;
    only a well-formed unknown key id reaches the tier designed to bound it.
    """
    store = _install()
    try:
        for header in (None, "", "Basic abc", "Bearer", "Bearer nodot", "Bearer bad.secret"):
            failed = False
            try:
                notification_auth.authenticate(header)
            except notification_auth.AuthFailed:
                failed = True
            assert failed, header
        assert store.reads == 0
        assert len(notification_auth._global_calls) == 0
    finally:
        _uninstall()


def test_a_repeated_unknown_key_id_costs_one_store_read():
    """Scenario 7, first half: the negative cache is what makes a bad credential cheap.

    One read establishes that the key id is unknown; the next twenty are answered from memory. The
    entry is short-lived (60 s) precisely because it is a negative cache on a security decision --
    that bounds how long a freshly issued credential could be refused by a stale entry.
    """
    store = _install()
    try:
        header = "Bearer kixn_0123456789abcdef0123.whatever"
        for _ in range(20):
            try:
                notification_auth.authenticate(header)
            except notification_auth.AuthFailed:
                pass
        assert store.reads == 1, store.reads
    finally:
        _uninstall()


def test_a_wrong_secret_for_a_known_key_is_never_cached():
    """The other direction, and it must not be folded in with the one above.

    Caching a wrong-secret refusal would let one bad attempt mask the RIGHT secret for a minute --
    a self-inflicted outage triggered by anybody who can guess a key id.
    """
    store = _install()
    bearer = _provision(store)
    key_id = bearer.split(".")[0]
    try:
        before = store.reads
        for _ in range(3):
            try:
                notification_auth.authenticate(f"Bearer {key_id}.wrong")
            except notification_auth.AuthFailed as exc:
                assert exc.reason == "secret"
        assert store.reads - before == 3, "each wrong-secret attempt re-reads the credential"
        # And the right secret still works immediately afterwards.
        assert notification_auth.authenticate(f"Bearer {bearer}").client_id == CLIENT
    finally:
        _uninstall()


def test_an_anonymous_flood_cannot_spend_a_clients_own_budget():
    """Scenario 7, second half. Two tiers, and the second is only reachable after authentication.

    The flood exhausts the GLOBAL pre-lookup tier -- the accepted trade, which `/impact`'s limiter
    already makes -- but spends none of the per-client allowance, so the client's budget is intact
    the moment the global window rolls over.
    """
    store = _install()
    bearer = _provision(store)
    try:
        seen_rate_limit = False
        for n in range(notification_auth.GLOBAL_PRELOOKUP_PER_MIN + 5):
            header = f"Bearer kixn_{n:020x}.nope"
            try:
                notification_auth.authenticate(header)
            except notification_auth.RateLimited as exc:
                seen_rate_limit = True
                assert exc.tier == "global"
                assert 0 < exc.retry_after_s <= 60
            except notification_auth.AuthFailed:
                pass
        assert seen_rate_limit, "the global tier must bind"
        assert notification_auth._client_calls == {}, "no client slot was spent by anonymous traffic"
        # Roll the global window over; the client still has its whole allowance.
        notification_auth._global_calls.clear()
        principal = notification_auth.authenticate(f"Bearer {bearer}")
        for _ in range(notification_auth.PER_CLIENT_PER_MIN):
            notification_auth.claim_client_slot(principal.client_id)
        limited = False
        try:
            notification_auth.claim_client_slot(principal.client_id)
        except notification_auth.RateLimited as exc:
            limited = exc.tier == "client"
        assert limited
    finally:
        _uninstall()


# ---- Credentials: rotation, revocation, suspension --------------------------------------------


def test_several_credentials_live_at_once_and_revoking_one_leaves_the_other(
):
    """Scenario 6. KTD8's whole rotation story: a client is never taken down to rotate a key."""
    store = _install()
    retiring = _provision(store)
    try:
        _new_record, replacement = notification_auth.issue_credential(
            CLIENT, actor="operator@entur.org", label="rotation 2026-09"
        )
        assert retiring.split(".")[0] != replacement.split(".")[0]
        assert notification_auth.authenticate(f"Bearer {retiring}").client_id == CLIENT
        assert notification_auth.authenticate(f"Bearer {replacement}").client_id == CLIENT

        notification_auth.revoke_credential(
            retiring.split(".")[0], actor="operator@entur.org", reason="cutover complete"
        )
        revoked = False
        try:
            notification_auth.authenticate(f"Bearer {retiring}")
        except notification_auth.AuthFailed as exc:
            revoked = exc.reason == "revoked"
        assert revoked
        assert notification_auth.authenticate(f"Bearer {replacement}").client_id == CLIENT

        listed = notification_auth.list_credentials(CLIENT)
        assert {c.status for c in listed} == {"active", "revoked"}
        # An operator listing credentials never sees the material.
        assert not any("secret" in c.__dataclass_fields__ for c in listed)
    finally:
        _uninstall()


def test_a_suspended_client_gets_403_not_401():
    """A client must be able to tell "my key is wrong" from "we have been switched off"."""
    store = _install()
    bearer = _provision(store)
    try:
        assert notification_auth.authenticate(f"Bearer {bearer}").client_id == CLIENT
        notification_auth.set_client_suspended(
            CLIENT, suspended=True, actor="operator@entur.org", reason="billing"
        )
        suspended = False
        try:
            notification_auth.authenticate(f"Bearer {bearer}")
        except notification_auth.AuthSuspended:
            suspended = True
        assert suspended, "the account cache must be cleared by the suspension itself"
        notification_auth.set_client_suspended(CLIENT, suspended=False, actor="operator@entur.org")
        assert notification_auth.authenticate(f"Bearer {bearer}").client_id == CLIENT
    finally:
        _uninstall()


def test_an_expired_credential_is_refused():
    _install()
    try:
        notification_auth.create_client(CLIENT, name="Acme", actor="op@entur.org")
        _record, bearer = notification_auth.issue_credential(
            CLIENT, actor="op@entur.org", expires_at=NOW - timedelta(seconds=1)
        )
        expired = False
        try:
            notification_auth.authenticate(f"Bearer {bearer}")
        except notification_auth.AuthFailed as exc:
            expired = exc.reason == "expired"
        assert expired
    finally:
        _uninstall()


def test_the_credential_ceiling_is_enforced_before_a_write():
    store = _install()
    try:
        notification_auth.create_client(CLIENT, name="Acme", actor="op@entur.org")
        for _ in range(notification_auth.MAX_CREDENTIALS_PER_CLIENT):
            notification_auth.issue_credential(CLIENT, actor="op@entur.org")
        before = len(store.writes)
        refused = False
        try:
            notification_auth.issue_credential(CLIENT, actor="op@entur.org")
        except notification_auth.Conflict:
            refused = True
        assert refused
        assert len(store.writes) == before, "the ceiling is checked before any write"
    finally:
        _uninstall()


def test_the_secret_is_never_logged_and_the_key_id_always_is():
    """KTD8's log rule, checked by capturing what a refusal actually emits."""
    store = _install()
    bearer = _provision(store)
    key_id, secret = bearer.split(".", 1)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        try:
            notification_auth.authenticate(f"Bearer {key_id}.{secret}x")
        except notification_auth.AuthFailed:
            pass
        rendered = "\n".join(
            r.getMessage() + repr(getattr(r, "fields", {})) for r in records
        )
        assert key_id in rendered, "the key id is what an operator can act on"
        assert secret not in rendered
        assert secret[:16] not in rendered
    finally:
        root.setLevel(previous)
        root.removeHandler(handler)
        _uninstall()


# ---- Destinations -----------------------------------------------------------------------------


def test_an_unsafe_callback_url_is_refused_at_provisioning_and_audited():
    """Scenario 8 / AE9. KTD9's syntactic gate runs while a human is present to fix it.

    A refusal that leaves no trace is indistinguishable from an operator who never tried, so the
    attempt, the actor and the reason land in the audit collection.
    """
    store = _install()
    try:
        notification_auth.create_client(CLIENT, name="Acme", actor="op@entur.org")
        for bad in (
            "http://hooks.acme.example.com/kix",  # not HTTPS
            "https://good.example@evil.example/kix",  # userinfo
            "https://hooks.acme.example.com:8443/kix",  # non-default port
            "https://hooks.acme.example.com/kix#frag",  # fragment
            # The address test, on the half of it that needs no DNS. These are the URLs an operator
            # actually pastes by mistake, and every one of them passes the syntactic checks above.
            "https://169.254.169.254/kix",  # the metadata address
            "https://10.0.0.5/kix",  # RFC 1918
            "https://127.0.0.1/kix",  # loopback
            "https://[::1]/kix",  # loopback, v6
            # NAT64 and 6to4: global and non-private by every named range, while encoding
            # 169.254.169.254. A deny-list is the wrong SHAPE for the claim, which is why
            # webhook_sender re-runs its positive test on what a prefix embeds.
            "https://[64:ff9b::a9fe:a9fe]/kix",
            "https://[2002:a9fe:a9fe::]/kix",
        ):
            refused = False
            try:
                notification_auth.create_destination(
                    DEST, client_id=CLIENT, callback_url=bad, actor="op@entur.org"
                )
            except notification_auth.Conflict as exc:
                refused = "unsafe callback URL" in str(exc)
            assert refused, bad
            assert store.get(notification_auth.DESTINATIONS, DEST) is None, bad
        refusals = [r for r in store.audit() if r["result"] == "refused"]
        assert len(refusals) == 10
        assert {r["action"] for r in refusals} == {"create_destination"}
        assert all(r["actor"] == "op@entur.org" for r in refusals)
        assert all(r["destination_id"] == DEST for r in refusals)
    finally:
        _uninstall()


def test_a_destination_is_paused_on_410_and_only_an_operator_resumes_it():
    """R12, and the field U3 left as an open question for U4.

    `mark_delivery_failed(terminal_state="cancelled")` records what happened to the DELIVERY. The
    pause is about the destination, lives here, and is what U6 reads before attempting one.
    """
    store = _install()
    _provision(store)
    try:
        destination = notification_auth.destination_for_delivery(DEST)
        assert destination is not None and destination.deliverable is True
        assert destination.signing_generations == (1,)

        paused = notification_auth.pause_destination(DEST, reason="410 Gone")
        assert paused.paused is True and paused.deliverable is False
        assert paused.paused_reason == "410 Gone"
        assert notification_auth.destination_for_delivery(DEST).deliverable is False

        # Idempotent: a backlog of pending deliveries against a gone endpoint must not fill the
        # audit collection with one row per attempt.
        audit_before = len(store.audit())
        notification_auth.pause_destination(DEST, reason="410 Gone")
        assert len(store.audit()) == audit_before

        resumed = notification_auth.resume_destination(DEST, actor="op@entur.org")
        assert resumed.paused is False and resumed.deliverable is True
        actions = [r["action"] for r in store.audit()]
        assert "pause_destination" in actions and "resume_destination" in actions
    finally:
        _uninstall()


def test_two_signing_generations_are_live_across_a_rotation():
    """R10: two valid at once so one can be rotated without a flag day and revoked alone."""
    store = _install()
    _provision(store)
    try:
        rotated = notification_auth.rotate_signing_generation(DEST, actor="op@entur.org")
        assert rotated.active_generation == 2
        assert rotated.retiring_generation == 1
        assert rotated.signing_generations == (2, 1), "active first, as webhook_signing documents"
        retired = notification_auth.retire_signing_generation(DEST, actor="op@entur.org")
        assert retired.signing_generations == (2,)
        # Idempotent once nothing is retiring.
        assert notification_auth.retire_signing_generation(DEST, actor="op@entur.org").signing_generations == (2,)
    finally:
        _uninstall()


def test_a_destination_belonging_to_another_client_is_forbidden_inside_the_module():
    """Kept apart HERE and collapsed into "missing" at the API edge, exactly as notification_store does.

    The store's caller needs to tell a tenancy violation from a typo in order to log the former; a
    client must not be able to probe another's destination ids by the difference.
    """
    store = _install()
    _provision(store)
    try:
        assert notification_auth.get_destination(DEST, client_id=CLIENT) is not None
        forbidden = False
        try:
            notification_auth.get_destination(DEST, client_id="someone-else")
        except notification_auth.Forbidden:
            forbidden = True
        assert forbidden
        assert notification_auth.current_destination("someone-else") is None
    finally:
        _uninstall()


def test_a_paused_destination_is_still_the_current_one():
    """Pausing stops delivery, not ownership -- and a client whose callback is paused most needs
    to see it on /destinations/current."""
    store = _install()
    _provision(store)
    try:
        notification_auth.pause_destination(DEST, reason="410 Gone")
        current = notification_auth.current_destination(CLIENT)
        assert current is not None and current.destination_id == DEST and current.paused is True
    finally:
        _uninstall()


def test_a_document_written_before_the_paused_field_existed_is_not_paused():
    """The failure direction here is the OPPOSITE of notification_store's expiry test, on purpose.

    A missing `paused` key means a document written by an older binary, and reading that as paused
    would take every existing destination out of service on the deploy that added the field.
    """
    store = _install()
    store.set(
        notification_auth.DESTINATIONS,
        DEST,
        {
            "destination_id": DEST,
            "client_id": CLIENT,
            "callback_url": GOOD_URL,
            "status": "active",
            "generations": {"active": 1},
        },
    )
    try:
        destination = notification_auth.get_destination(DEST)
        assert destination is not None
        assert destination.paused is False and destination.deliverable is True
        assert destination.retiring_generation is None
    finally:
        _uninstall()


# ---- The audit collection ---------------------------------------------------------------------


def test_every_operator_action_lands_in_the_audit_record():
    """R22: provisioning, credential, destination and suspension actions, with actor and timestamp."""
    store = _install()
    try:
        notification_auth.create_client(CLIENT, name="Acme", actor="a@entur.org")
        notification_auth.create_destination(
            DEST, client_id=CLIENT, callback_url=GOOD_URL, actor="a@entur.org"
        )
        _c, bearer = notification_auth.issue_credential(CLIENT, actor="a@entur.org")
        notification_auth.revoke_credential(bearer.split(".")[0], actor="a@entur.org")
        notification_auth.set_client_suspended(CLIENT, suspended=True, actor="a@entur.org")
        notification_auth.rotate_signing_generation(DEST, actor="a@entur.org")
        notification_auth.amend_destination(
            DEST, callback_url="https://hooks2.acme.example.com/kix", actor="a@entur.org"
        )
        rows = store.audit()
        assert {r["action"] for r in rows} >= {
            "create_client",
            "create_destination",
            "issue_credential",
            "revoke_credential",
            "suspend_client",
            "rotate_signing_generation",
            "amend_destination",
        }
        for row in rows:
            assert row["actor"] == "a@entur.org"
            assert isinstance(row["at"], datetime)
            assert row["audit_id"]
        # Chronological by document id, which is what makes a plain collection listing readable.
        ids = [r["audit_id"] for r in rows]
        assert ids == sorted(ids)
        # And no secret material anywhere in it.
        assert "secret_hash" not in {k for r in rows for k in r}
        assert bearer.split(".", 1)[1] not in repr(rows)
    finally:
        _uninstall()


def test_the_audit_collection_is_append_only_and_no_route_can_read_it():
    """R22's second half, established by grepping both files -- see the module docstring.

    Firestore has no per-collection IAM (KTD5 records the limit), so "no client credential can
    read it" cannot be a runtime property. What can be established is that the request-path module
    never names the collection, which is `meldinger_decisions.sql`'s own argument: a query that
    cannot name a column cannot leak it however the code above drifts.
    """
    assert notification_auth.ADMIN_AUDIT == "admin_audit"
    assert notification_auth.ADMIN_AUDIT not in API_SOURCE
    assert "ADMIN_AUDIT" not in API_SOURCE
    # Append-only: the audit collection is written by `set` and by nothing else.
    for forbidden in (
        f"update({notification_auth.ADMIN_AUDIT}",
        "update(ADMIN_AUDIT",
        "delete(ADMIN_AUDIT",
    ):
        assert forbidden not in AUTH_SOURCE, forbidden
    assert AUTH_SOURCE.count("_store().set(ADMIN_AUDIT") == 1
    assert "record_admin_action" not in API_SOURCE


def test_a_store_outage_suppresses_the_action_and_nothing_wider():
    """R20 at this layer: the failure propagates as itself, and no half-written record survives."""
    store = _install()
    try:
        store.fail_with = RuntimeError("firestore unavailable")
        raised = False
        try:
            notification_auth.create_client("beta", name="Beta", actor="op@entur.org")
        except RuntimeError:
            raised = True
        assert raised
        store.fail_with = None
        assert store.data == {}
    finally:
        _uninstall()


# ---- The operator tool ------------------------------------------------------------------------


def _allow(actor: str = "operator@entur.org"):
    def authorizer(environment: str) -> tool.Authorization:
        return tool.Authorization(
            actor=actor, group=tool.OPERATOR_GROUPS[environment], allowed=True
        )

    return authorizer


def _deny(reason: str = "not_a_member", actor: str = "outsider@entur.org"):
    def authorizer(environment: str) -> tool.Authorization:
        return tool.Authorization(
            actor=actor, group=tool.OPERATOR_GROUPS[environment], allowed=False, reason=reason
        )

    return authorizer


def _run(args: list[str], *, authorizer, store: _MemoryDocs) -> tuple[int, str]:
    out = io.StringIO()
    # GOOGLE_CLOUD_PROJECT is POPPED for the run, and that is not tidiness. The tool refuses a
    # project that is not `--environment`'s own (exit 2), and a developer reproducing prd behaviour
    # locally has `GOOGLE_CLOUD_PROJECT=ent-kixavvik-dev` exported -- which would fail every
    # `--environment prd` case here for a reason about their shell. `test_varsel_api` pins
    # `KIX_ENVIRONMENT` at import for the same class of leak.
    saved = os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
    try:
        code = tool.main(args, authorizer=authorizer, store=store, out=out)
    finally:
        if saved is None:
            os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        else:
            os.environ["GOOGLE_CLOUD_PROJECT"] = saved
    return code, out.getvalue()


def test_an_operator_action_without_the_group_is_refused_and_audited():
    """Scenario 10, second half. Refused with a non-zero exit, nothing written but the audit row.

    The limitation is stated in the tool's docstring rather than hidden: if the group IS the IAM
    grant, a non-member may also be unable to write this row, so it is best effort and a structured
    stderr line always goes out. What the row proves is what was attempted and by whom it claimed to
    be -- and it cannot be erased, because nothing updates or deletes this collection.
    """
    store = _install()
    try:
        code, _out = _run(
            ["--environment", "prd", "create-client", "--client-id", "beta", "--name", "Beta"],
            authorizer=_deny(),
            store=store,
        )
        assert code == 3
        assert store.get(notification_auth.ACCOUNTS, "beta") is None, "nothing was provisioned"
        rows = store.audit()
        assert len(rows) == 1
        assert rows[0]["result"] == "refused"
        assert rows[0]["action"] == "create-client"
        assert rows[0]["actor"] == "outsider@entur.org"
        assert rows[0]["client_id"] == "beta"
        assert rows[0]["reason"] == "not_a_member"
    finally:
        _uninstall()


def test_every_subcommand_is_authorized_including_the_reads():
    """A destination's callback URL and a client's key ids are exactly what a forger wants first."""
    store = _install()
    _provision(store)
    try:
        for args in (
            ["show-client", "--client-id", CLIENT],
            ["list-keys", "--client-id", CLIENT],
        ):
            code, _out = _run(["--environment", "dev", *args], authorizer=_deny(), store=store)
            assert code == 3, args
    finally:
        _uninstall()


def test_the_tool_provisions_a_client_a_destination_and_a_working_credential():
    """The operator runbook, executed: create-client, create-destination, issue-key, and the
    credential the last one prints authenticates against the same store."""
    store = _install()
    try:
        allow = _allow()
        code, _ = _run(
            ["--environment", "dev", "create-client", "--client-id", CLIENT, "--name", "Acme"],
            authorizer=allow,
            store=store,
        )
        assert code == 0
        code, _ = _run(
            [
                "--environment",
                "dev",
                "create-destination",
                "--client-id",
                CLIENT,
                "--destination-id",
                DEST,
                "--url",
                GOOD_URL,
            ],
            authorizer=allow,
            store=store,
        )
        assert code == 0
        code, printed = _run(
            ["--environment", "dev", "issue-key", "--client-id", CLIENT],
            authorizer=allow,
            store=store,
        )
        assert code == 0
        import json

        result = json.loads(printed)
        bearer = result["bearer"]
        assert "Shown once" in result["note"]
        # Re-configure exactly as the app does, then authenticate the printed credential.
        notification_auth.configure(docs=store, clock=lambda: NOW)
        assert notification_auth.authenticate(f"Bearer {bearer}").client_id == CLIENT

        code, _ = _run(
            ["--environment", "dev", "revoke-key", "--key-id", bearer.split(".")[0]],
            authorizer=allow,
            store=store,
        )
        assert code == 0
        notification_auth.configure(docs=store, clock=lambda: NOW)
        revoked = False
        try:
            notification_auth.authenticate(f"Bearer {bearer}")
        except notification_auth.AuthFailed:
            revoked = True
        assert revoked
    finally:
        _uninstall()


def test_the_tool_reports_a_refused_url_as_a_failure_rather_than_a_crash():
    """An operator pasting the metadata address gets exit code 2 and an audited refusal.

    Not a traceback, and not a stored destination: the refusal is the useful outcome, and the audit
    row is what makes "somebody tried to point a client at 169.254.169.254" visible later.
    """
    store = _install()
    try:
        allow = _allow()
        _run(
            ["--environment", "dev", "create-client", "--client-id", CLIENT, "--name", "Acme"],
            authorizer=allow,
            store=store,
        )
        code, _ = _run(
            [
                "--environment",
                "dev",
                "create-destination",
                "--client-id",
                CLIENT,
                "--destination-id",
                DEST,
                "--url",
                "https://169.254.169.254/kix",
            ],
            authorizer=allow,
            store=store,
        )
        assert code == 2
        assert store.get(notification_auth.DESTINATIONS, DEST) is None
        refusals = [r for r in store.audit() if r["result"] == "refused"]
        assert len(refusals) == 1
        assert refusals[0]["action"] == "create_destination"
        assert "not a globally routable address" in refusals[0]["reason"]
    finally:
        _uninstall()


def test_production_has_its_own_operator_group():
    """R22: "separately for production". Two groups, and the prd one is not the other."""
    assert set(tool.OPERATOR_GROUPS) == set(tool.ENVIRONMENTS)
    assert tool.OPERATOR_GROUPS["prd"] != tool.OPERATOR_GROUPS["dev"]
    assert tool.OPERATOR_GROUPS["prd"].endswith("@entur.org")
    assert "prd" in tool.OPERATOR_GROUPS["prd"]


def test_the_environment_is_never_inferred():
    """A tool that guessed prd from an exported GOOGLE_CLOUD_PROJECT would be one stale shell
    variable away from provisioning the wrong environment."""
    assert "--environment" in TOOL_SOURCE
    assert 'required=True, choices=ENVIRONMENTS' in TOOL_SOURCE.replace("\n", " ")
    failed = False
    stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        tool._parser().parse_args(["create-client", "--client-id", "x", "--name", "y"])
    except SystemExit:
        failed = True
    finally:
        sys.stderr = stderr
    assert failed


def test_the_gate_and_the_target_cannot_be_chosen_separately():
    """R22's separate prd group was bypassable by a flag, and only prd IAM stopped it.

    `--environment` picks the group membership is checked against; `--project` picked which project
    was then read and written, with no comparison between the two. So `--environment dev --project
    ent-kixavvik-prd` was authorized against the dev group and acted on prd -- `show-signing-secret`
    printing a live prd receiver credential, `suspend-client` and `revoke-key` silencing a real
    client. The audit row lands in the project that was targeted, so the action IS recorded; it is
    recorded as authorized.

    Refused BEFORE the store is configured and before the group is checked, so nothing is opened and
    nothing is read on the way to the refusal. Exit 2, the tool's "your invocation is wrong" code,
    distinct from 3 ("you are not in the group") -- an operator who IS a member and typed the wrong
    project must not be told they lack membership.
    """
    store = _MemoryDocs()
    saved = os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
    stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        code = tool.main(
            [
                "--environment",
                "dev",
                "--project",
                "ent-kixavvik-prd",
                "create-client",
                "--client-id",
                "acme",
                "--name",
                "Acme",
            ],
            authorizer=_allow(),
            store=store,
            out=io.StringIO(),
        )
        message = sys.stderr.getvalue()
    finally:
        sys.stderr = stderr
        if saved is not None:
            os.environ["GOOGLE_CLOUD_PROJECT"] = saved
    assert code == 2, code
    assert store.data == {}, "refused before anything was written, audit row included"
    # The message names both halves, because the operator has to know which one to change.
    assert tool.OPERATOR_GROUPS["dev"] in message and "ent-kixavvik-dev" in message
    assert "ent-kixavvik-prd" in message
    # And a stale exported variable is refused the same way rather than silently redirecting the
    # run -- which is the failure `--project` was added for in the first place.
    saved = os.environ.get("GOOGLE_CLOUD_PROJECT")
    os.environ["GOOGLE_CLOUD_PROJECT"] = "ent-kixavvik-prd"
    stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        code = tool.main(
            ["--environment", "dev", "create-client", "--client-id", "acme", "--name", "Acme"],
            authorizer=_allow(),
            store=_MemoryDocs(),
            out=io.StringIO(),
        )
    finally:
        sys.stderr = stderr
        if saved is None:
            os.environ.pop("GOOGLE_CLOUD_PROJECT", None)
        else:
            os.environ["GOOGLE_CLOUD_PROJECT"] = saved
    assert code == 2, code
    # Every environment has a project and the map is the one place the naming lives.
    assert set(tool.PROJECTS) == set(tool.ENVIRONMENTS)
    assert tool.PROJECTS["prd"] != tool.PROJECTS["dev"]


def test_the_group_authorizer_fails_closed():
    """Every failure path in the real authorizer answers "not allowed", never "allowed".

    At Entur `groups.search` is denied to ordinary developers while `checkTransitiveMembership`
    works, so an exception is at least as likely to mean "not a member" as "API problem" -- and
    guessing "member" from an unreadable answer is the one guess that must not be made. Exercised
    for real: with no googleapiclient/ADC reachable in this process, the call must still return a
    refusal rather than raise.
    """
    verdict = tool.google_group_authorizer("prd")
    assert verdict.allowed is False
    assert verdict.group == tool.OPERATOR_GROUPS["prd"]
    assert verdict.reason
    assert re.search(r"membership_check_failed|not_a_member", verdict.reason)


def test_the_tool_never_reaches_the_notification_api_or_a_client_credential():
    """R22's separation, structurally: the operator boundary is not a client-authenticated route.

    A client credential authenticates a machine against /notification-api/v1 and can do none of
    this; a group membership authorizes a human against the store and cannot register a
    subscription.
    """
    assert "notification_api" not in TOOL_SOURCE
    assert "authenticate(" not in TOOL_SOURCE
    assert "Bearer" not in TOOL_SOURCE.split('"""', 2)[2]


def test_the_operator_can_hand_a_client_its_verification_secret():
    """There is otherwise NO path to it, and the quickstart's promise depends on one.

    `webhook_signing.derive_secret` is called only by the signer, and `GET /destinations/current`
    serves generation NUMBERS and never derives -- deliberately, because a secret fetchable over the
    client's own credential is a secret their credential can be traded for. So the disclosure is an
    OPERATOR action: same group, same audit, and the audit row is the point rather than a formality.
    """
    store = _install()
    _provision(store)
    os.environ[webhook_signing.SIGNING_ROOT_ENV] = SIGNING_ROOT
    try:
        code, printed = _run(
            ["--environment", "dev", "show-signing-secret", "--destination-id", DEST],
            authorizer=_allow(),
            store=store,
        )
        assert code == 0
        result = json.loads(printed)
        assert result["destination_id"] == DEST
        assert result["active_generation"] == 1
        assert result["retiring_generation"] is None
        assert [s["generation"] for s in result["secrets"]] == [1]
        assert result["secrets"][0]["role"] == "active"
        # The value a receiver verifies with, derived rather than stored -- so it is exactly what
        # `webhook_signing.sign` will use, not a copy that can drift.
        assert result["secrets"][0]["secret"] == webhook_signing.derive_secret(DEST, 1)
        assert result["secrets"][0]["secret"].startswith(webhook_signing.SECRET_PREFIX)
        assert "LIVE CREDENTIAL" in result["note"]

        rows = [r for r in store.audit() if r["action"] == "show_signing_secret"]
        assert len(rows) == 1
        assert rows[0]["result"] == "disclosed"
        assert rows[0]["actor"] == "operator@entur.org"
        assert rows[0]["destination_id"] == DEST
        # The audit row records the disclosure, never the disclosed value.
        assert webhook_signing.derive_secret(DEST, 1) not in repr(rows)
        assert SIGNING_ROOT not in repr(rows)
    finally:
        os.environ.pop(webhook_signing.SIGNING_ROOT_ENV, None)
        _uninstall()


def test_both_live_generations_are_printed_during_a_rotation():
    """"Which one do I install" is the first question a client asks, and during a rotation the
    answer is "the active one, and keep accepting the retiring one"."""
    store = _install()
    _provision(store)
    os.environ[webhook_signing.SIGNING_ROOT_ENV] = SIGNING_ROOT
    try:
        notification_auth.rotate_signing_generation(DEST, actor="op@entur.org")
        code, printed = _run(
            ["--environment", "dev", "show-signing-secret", "--destination-id", DEST],
            authorizer=_allow(),
            store=store,
        )
        assert code == 0
        result = json.loads(printed)
        assert [(s["generation"], s["role"]) for s in result["secrets"]] == [
            (2, "active"),
            (1, "retiring"),
        ]
        assert result["secrets"][0]["secret"] != result["secrets"][1]["secret"]
        for entry in result["secrets"]:
            assert entry["secret"] == webhook_signing.derive_secret(DEST, entry["generation"])
    finally:
        os.environ.pop(webhook_signing.SIGNING_ROOT_ENV, None)
        _uninstall()


def test_the_signing_secret_needs_the_group_like_every_other_action():
    """A credential disclosure without the group would be the one action worth having a group for."""
    store = _install()
    _provision(store)
    os.environ[webhook_signing.SIGNING_ROOT_ENV] = SIGNING_ROOT
    try:
        code, printed = _run(
            ["--environment", "prd", "show-signing-secret", "--destination-id", DEST],
            authorizer=_deny(),
            store=store,
        )
        assert code == 3
        assert SIGNING_ROOT not in printed
        refusals = [r for r in store.audit() if r["result"] == "refused"]
        assert len(refusals) == 1
        assert refusals[0]["action"] == "show-signing-secret"
        assert refusals[0]["destination_id"] == DEST
    finally:
        os.environ.pop(webhook_signing.SIGNING_ROOT_ENV, None)
        _uninstall()


def test_a_missing_signing_root_is_one_line_rather_than_a_provisioning_lecture():
    """`issue-key`'s pre-check, for the other secret. Deeper in, the failure's message is about
    Secret Manager and a Helm secret group -- the wrong hint for a human who has simply not
    exported it, and one that reads as "this environment is broken"."""
    store = _install()
    _provision(store)
    os.environ.pop(webhook_signing.SIGNING_ROOT_ENV, None)
    try:
        code, printed = _run(
            ["--environment", "dev", "show-signing-secret", "--destination-id", DEST],
            authorizer=_allow(),
            store=store,
        )
        assert code == 2
        assert printed == "", "nothing is printed on stdout when nothing could be derived"
        # Refused BEFORE the group check, so a missing variable is not reported as an authorization
        # problem -- and nothing was disclosed or audited as disclosed.
        assert [r for r in store.audit() if r["action"] == "show_signing_secret"] == []
        # AND the message distinguishes the two reasons the root can be unusable, because they need
        # different actions. THIS command is where the second one happens: the runbook tells an
        # operator to export the variable by hand for it, which is exactly the moment the example
        # root published in `signing-vectors.json` gets pasted -- and "is unset (or under 32 bytes)"
        # would tell them the variable they have plainly set is missing.
        os.environ[webhook_signing.SIGNING_ROOT_ENV] = webhook_signing._PUBLISHED_TEST_ROOT.decode()
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            code, printed = _run(
                ["--environment", "dev", "show-signing-secret", "--destination-id", DEST],
                authorizer=_allow(),
                store=store,
            )
            message = sys.stderr.getvalue()
        finally:
            sys.stderr = stderr
            os.environ.pop(webhook_signing.SIGNING_ROOT_ENV, None)
        assert code == 2
        assert printed == ""
        assert "signing-vectors" in message, message
        assert [r for r in store.audit() if r["action"] == "show_signing_secret"] == []
    finally:
        _uninstall()


def test_the_disclosure_command_says_what_it_prints():
    """The one command whose output must not go into a ticket, and the docstring has to say so
    where somebody reading `--help` or the file will see it."""
    assert "show-signing-secret" in TOOL_SOURCE
    assert "PRINTS A LIVE CREDENTIAL" in TOOL_SOURCE
    assert "must\nnot go into a ticket" in TOOL_SOURCE or "not go into a ticket" in TOOL_SOURCE
    assert "rotate-signing" in TOOL_SOURCE



def test_every_identity_plane_firestore_call_carries_its_own_deadline():
    """KTD6's per-CALL deadline, on the plane it was missing from.

    ``notification_store`` bounds every read at ``_CALL_DEADLINE_S`` and
    ``test_notification_store.test_every_firestore_call_carries_its_own_deadline`` pins it. The identity
    plane inherited none of that: all four ``_FirestoreDocs`` operations passed no ``timeout=``, and
    the one at :meth:`notification_auth._FirestoreDocs.get` is the credential lookup ``authenticate``
    performs on EVERY authenticated request -- on a plain ``def`` route sharing the anyio worker pool
    with ``/varsel-api`` and ``/impact``. A Firestore stall therefore parked one shared worker per
    request, behind nothing but a 300/min pre-lookup tier, until the gRPC layer gave up on its own.

    Checked DYNAMICALLY as well as statically, and the dynamic half is the one that matters: this
    class is the production seam, so every suite in this directory injects
    :class:`_MemoryDocs` instead and no behavioural test reaches it at all. The fake below asserts
    the kwarg the way ``test_notification_store``'s does, for the same stated reason -- a double that
    accepted ``**kwargs`` would let a path silently lose its deadline.
    """
    seen: list[str] = []

    class _Ref:
        def get(self, timeout: float | None = None) -> Any:
            assert timeout == notification_store._CALL_DEADLINE_S, "get without a deadline"
            seen.append("get")
            return _Snap()

        def set(self, doc: Any, timeout: float | None = None) -> None:
            assert timeout == notification_store._CALL_DEADLINE_S, "set without a deadline"
            seen.append("set")

        def update(self, patch: Any, timeout: float | None = None) -> None:
            assert timeout == notification_store._CALL_DEADLINE_S, "update without a deadline"
            seen.append("update")

    class _Snap:
        exists = True

        def to_dict(self) -> dict[str, Any]:
            return {"client_id": CLIENT}

    class _Query:
        def where(self, **_kwargs: Any) -> Any:
            return self

        def limit(self, _n: int) -> Any:
            return self

        def stream(self, timeout: float | None = None) -> list[Any]:
            assert timeout == notification_store._CALL_DEADLINE_S, "stream without a deadline"
            seen.append("stream")
            return [_Snap()]

    class _Collection:
        def document(self, _doc_id: str) -> Any:
            return _Ref()

        def where(self, **kwargs: Any) -> Any:
            return _Query().where(**kwargs)

    class _Client:
        def collection(self, _name: str) -> Any:
            return _Collection()

    saved = notification_store.firestore_client
    notification_store.firestore_client = lambda: _Client()  # type: ignore[assignment]
    try:
        docs = notification_auth._FirestoreDocs()
        assert docs.get(notification_auth.ACCOUNTS, CLIENT) == {"client_id": CLIENT}
        docs.set(notification_auth.ACCOUNTS, CLIENT, {"client_id": CLIENT})
        docs.update(notification_auth.ACCOUNTS, CLIENT, {"suspended": True})
        assert docs.list_by(notification_auth.CREDENTIALS, "client_id", CLIENT, limit=5) == [
            {"client_id": CLIENT}
        ]
    finally:
        notification_store.firestore_client = saved  # type: ignore[assignment]
    assert seen == ["get", "set", "update", "stream"], seen
    # And statically, over the seam's own source, so a FIFTH operation cannot dodge it: every call
    # this class makes on the client is one of the four names above, and each must name the
    # constant. Scoped to the class rather than grepped over the module, because `_Docs`' own
    # `get`/`set`/`update`/`list_by` are called all over `notification_auth` and carry no timeout by
    # design -- the seam is the abstraction, and only its Firestore implementation dials anything.
    seam = inspect.getsource(notification_auth._FirestoreDocs)
    assert seam.count("timeout=notification_store._CALL_DEADLINE_S") == 4, seam
    assert len(re.findall(r"self\._c\(\)", seam)) == 4, "one client call per operation, no more"
    assert "_CALL_DEADLINE_S" not in AUTH_SOURCE.replace(seam, ""), (
        "the deadline belongs to the seam's four calls; a fifth mention elsewhere means a client "
        "call escaped the class"
    )


def _run_all() -> None:
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} client auth checks passed.")


if __name__ == "__main__":
    _run_all()
