"""Checks for partner_api.py: the authenticated partner surface at /notification-api/v1.

Hermetic -- a ``TestClient`` over a FastAPI app carrying the real router, no emulator, no socket, no
credentials -- and deliberately backed by the **real** ``partner_store`` running against an
in-memory fake client rather than by a hand-written fake store. That choice is the point: the
interesting behaviour on this surface is the mapping of the store's own verdicts onto status codes,
so a fake store would be a second statement of the rules being checked, and every one of them
(a tombstone is not resurrected, a version mismatch is a conflict, the test marker is immutable, the
active-subscription ceiling is refused before any write) would then be true by construction of the
fixture. The credential plane is the real ``partner_auth`` over its four-method document seam, so
the two-tier limiter, the negative cache and the 401/403/503 split are exercised end to end through
HTTP.

The one thing genuinely stubbed is the outbound call: ``webhook_client.deliver`` opens a socket and
refuses to do so from a test process at all, so ``/test-events`` gets an injected transport. The
signing is real -- the synthetic event is signed by ``webhook_signing.sign`` over the exact bytes
the route built, and one check verifies it the way a partner's receiver would.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
from datetime import UTC, datetime, timedelta
from typing import Any

import partner_api
import partner_auth
import partner_store
import sso
import webhook_client
import webhook_signing
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
API_SOURCE = (HERE / "partner_api.py").read_text()
VARSEL_SOURCE = (HERE / "varsel.py").read_text()
CONTRACTS = REPO / "contracts" / "notifications" / "v1"
OPENAPI = json.loads((CONTRACTS / "openapi.json").read_text())
SUBSCRIPTION_SCHEMA = json.loads((CONTRACTS / "subscription.schema.json").read_text())
EVENT_SCHEMA = json.loads((CONTRACTS / "event.schema.json").read_text())

NOW = datetime(2026, 9, 8, 6, 30, tzinfo=UTC)
PARTNER = "acme"
OTHER = "beta"
DEST = "acme-prod"
OTHER_DEST = "beta-prod"
PEPPER = "p" * 32
SIGNING_ROOT = "r" * 40
GOOD_URL = "https://hooks.acme.example.com/kix"

FROM = "NSR:StopPlace:337"
TO = "NSR:StopPlace:418"
#: A ref the stop registry has collapsed onto FROM. /varsel canonicalises a submitted ref for
#: exactly this case: a client holding a stale stop list names the absorbed side of a station, and
#: storing it recreates the silent no-match the collapse exists to remove.
COLLAPSED = "NSR:StopPlace:3247"


def _code_only(source: str) -> str:
    """The executable half of a module: no comments, no docstrings.

    Needed for the negative greps below, and the reason is `test_partner_store._uncommented`'s in
    the other direction: this module's docstrings deliberately NAME what the code must not do
    ("a route that called `commit_event` would falsify every quota check"), so a bare substring
    search over the whole file matches the explanation and fails for the wrong reason.
    """
    tree = ast.parse(source)
    skip: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            skip.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return "\n".join(
        line
        for number, line in enumerate(source.splitlines(), start=1)
        if number not in skip and not line.strip().startswith("#")
    )


API_CODE = _code_only(API_SOURCE)


def _put_subject(document_id: str, alert_id: str, phase: str, **over: Any) -> dict[str, Any]:
    """One alert-subject document, in the shape `partner_store._mint` writes.

    Written straight into the fake rather than by running an emit: the emit path is U6's and needs
    a whole cycle's inputs, while what this suite is checking is that the API SERVES what the pass
    has already committed. `test_partner_store` pins the writer against the reader on its side.
    """
    doc: dict[str, Any] = {
        "alert_id": alert_id,
        "subscription_id": document_id,
        "partner_id": PARTNER,
        "destination_id": DEST,
        "revision": 7,
        "phase": phase,
        "delivered_revision": None,
        "operator": "VYG",
        "line": "VYG:Line:L1",
        "journey_ref": "VYG:DatedServiceJourney:1234",
        "call_date": "2026-09-08",
        "stop_place": FROM,
        "stop_order": 3,
        "created_at": NOW,
        "updated_at": NOW,
        partner_store.TTL_FIELD: NOW + timedelta(days=partner_store.HISTORY_DAYS),
    }
    doc.update(over)
    _fake.put(partner_store.SUBJECTS, alert_id, doc)
    return doc


def _body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "subscription_id": "commuter-42-morning",
        "destination_id": DEST,
        "from_stop": FROM,
        "to_stop": TO,
        "begin": "06:45",
        "end": "08:30",
        "days": [0, 1, 2, 3, 4],
        "partner_ref": "commuter-42",
        "test": False,
    }
    base.update(over)
    return base


# ---- The in-memory Firestore the REAL store runs against --------------------------------------


class _Snap:
    def __init__(self, path: str, data: dict[str, Any] | None, client: Any) -> None:
        self._path = path
        self._data = data
        self._client = client

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None

    @property
    def reference(self) -> Any:
        return _Ref(self._client, self._path)


class _Ref:
    def __init__(self, client: Any, path: str) -> None:
        self.client = client
        self.path = path

    def get(self, transaction: Any = None) -> _Snap:
        del transaction
        return _Snap(self.path, self.client.docs.get(self.path), self.client)


class _Collection:
    def __init__(self, client: Any, name: str) -> None:
        self.client = client
        self.name = name

    def document(self, doc_id: str) -> _Ref:
        return _Ref(self.client, f"{self.name}/{doc_id}")


class _Batch:
    def __init__(self, client: Any) -> None:
        self.client = client
        self._deletes: list[str] = []

    def delete(self, ref: _Ref) -> None:
        self._deletes.append(ref.path)

    def commit(self) -> None:
        for path in self._deletes:
            self.client.docs.pop(path, None)
            self.client.log.append(f"delete:{path}")
        self._deletes = []


class _Page:
    def __init__(self, client: Any, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self.client = client
        self.rows = rows

    def stream(self) -> list[_Snap]:
        return [_Snap(path, data, self.client) for path, data in self.rows]


class _Txn:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.writes: list[tuple[str, str, dict[str, Any] | None]] = []

    def set(self, ref: _Ref, data: dict[str, Any]) -> None:
        self.writes.append(("set", ref.path, dict(data)))

    def update(self, ref: _Ref, data: dict[str, Any]) -> None:
        self.writes.append(("update", ref.path, dict(data)))

    def delete(self, ref: _Ref) -> None:
        self.writes.append(("delete", ref.path, None))

    def get(self, query: Any) -> list[_Snap]:
        return list(query.stream())


class _FakeFirestore:
    """Enough Firestore for the real ``partner_store`` bodies, plus an ordered write log.

    ``log`` is what makes "the tombstone lands before anything is deleted" checkable rather than
    asserted: every transactional write and every batch delete appends to it in order, so R13's
    ordering is read off the sequence instead of inferred from the end state -- which is identical
    whichever order the two halves ran in.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.log: list[str] = []
        self.reads = 0
        self.fail_with: BaseException | None = None

    def collection(self, name: str) -> _Collection:
        return _Collection(self, name)

    def batch(self) -> _Batch:
        return _Batch(self)

    def put(self, collection: str, doc_id: str, data: dict[str, Any]) -> None:
        self.docs[f"{collection}/{doc_id}"] = dict(data)

    def doc(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        stored = self.docs.get(f"{collection}/{doc_id}")
        return dict(stored) if stored is not None else None

    def in_collection(self, collection: str) -> list[dict[str, Any]]:
        return [
            dict(data)
            for path, data in self.docs.items()
            if path.split("/", 1)[0] == collection
        ]


_fake: _FakeFirestore | None = None
_auth_docs: Any = None
_saved: dict[str, Any] = {}
_deliveries: list[dict[str, Any]] = []
_delivery_result: dict[str, Any] = {}


class _MemoryDocs:
    """``partner_auth._Docs`` over a dict, counting reads."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], dict[str, Any]] = {}
        self.reads = 0
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
        self.data[(collection, doc_id)] = dict(doc)

    def update(self, collection: str, doc_id: str, patch: Any) -> None:
        self._check()
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


def _sortable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value
    return datetime.min.replace(tzinfo=UTC)


def _install(*, pepper: str | None = PEPPER, connectivity: Any = None) -> _FakeFirestore:
    """Point both modules at in-memory state, running every real body they own."""
    global _fake, _auth_docs
    client = _FakeFirestore()
    _fake = client
    _auth_docs = _MemoryDocs()
    _deliveries.clear()
    _delivery_result.clear()
    _delivery_result.update({"ok": True, "status": 200})

    _saved.update(
        {
            "client": partner_store._client,
            "run": partner_store._run_transaction,
            "query": partner_store._query,
        }
    )
    partner_store._client = lambda: client  # type: ignore[assignment]

    def _run(body: Any) -> Any:
        if client.fail_with is not None:
            raise client.fail_with
        txn = _Txn(client)
        result = body(txn)
        for kind, path, data in txn.writes:
            if kind == "set":
                client.docs[path] = dict(data or {})
            elif kind == "update":
                client.docs.setdefault(path, {}).update(data or {})
            else:
                client.docs.pop(path, None)
            client.log.append(f"{kind}:{path}")
        return result

    def _query(name: str, *, equality: Any = None, inequality: Any = None, limit: int) -> _Page:
        if client.fail_with is not None:
            raise client.fail_with
        client.reads += 1
        spec = partner_store._SPECS_BY_NAME[name]
        rows: list[tuple[str, dict[str, Any]]] = []
        for path, data in client.docs.items():
            if path.split("/", 1)[0] != spec.collection:
                continue
            if any(data.get(f) != (equality or {})[f] for f in spec.equality):
                continue
            keep = True
            for field_path, op in spec.inequality:
                value = data.get(field_path)
                bound = (inequality or {})[field_path]
                if op == "<=" and not (value is not None and value <= bound):
                    keep = False
            if keep:
                rows.append((path, dict(data)))
        for field_path, direction in reversed(spec.order_by):
            rows.sort(
                key=lambda row, f=field_path: _sortable(row[1].get(f)),
                reverse=direction == "DESCENDING",
            )
        return _Page(client, rows[:limit])

    partner_store._run_transaction = _run  # type: ignore[assignment]
    partner_store._query = _query  # type: ignore[assignment]
    partner_store.configure(client=client, clock=lambda: NOW)

    if pepper is None:
        os.environ.pop(partner_auth.PEPPER_ENV, None)
    else:
        os.environ[partner_auth.PEPPER_ENV] = pepper
    os.environ[webhook_signing.SIGNING_ROOT_ENV] = SIGNING_ROOT
    partner_auth.configure(docs=_auth_docs, clock=lambda: NOW)

    def _deliver(url: str, *, body: bytes, headers: Any, **_kw: Any) -> Any:
        _deliveries.append({"url": url, "body": body, "headers": dict(headers)})
        return webhook_client.DeliveryAttempt(**_delivery_result)

    partner_api.configure(
        store=partner_store,
        auth=partner_auth,
        canonicalize=lambda ref: FROM if ref == COLLAPSED else ref,
        connectivity=connectivity if connectivity is not None else (lambda a, b: (True, True)),
        deliver=_deliver,
    )
    return client


def _uninstall() -> None:
    global _fake, _auth_docs
    if _saved:
        partner_store._client = _saved["client"]  # type: ignore[assignment]
        partner_store._run_transaction = _saved["run"]  # type: ignore[assignment]
        partner_store._query = _saved["query"]  # type: ignore[assignment]
        _saved.clear()
    partner_store.reset_for_tests()
    partner_auth.reset_for_tests()
    partner_api.configure()
    os.environ.pop(partner_auth.PEPPER_ENV, None)
    os.environ.pop(webhook_signing.SIGNING_ROOT_ENV, None)
    _fake = None
    _auth_docs = None


def _provision(partner: str = PARTNER, destination: str = DEST) -> str:
    partner_auth.create_partner(partner, name=partner.title(), actor="op@entur.org")
    partner_auth.create_destination(
        destination, partner_id=partner, callback_url=GOOD_URL, actor="op@entur.org"
    )
    _record, bearer = partner_auth.issue_credential(partner, actor="op@entur.org")
    return bearer


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(partner_api.router)
    return TestClient(app)


def _headers(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}"}


# ---- Scenario 1 / AE11: credentials and tenancy -----------------------------------------------


def test_a_missing_or_wrong_credential_reads_nothing():
    """AE11's first half, and the body says which it is not.

    "Unknown key id" and "wrong secret" are different facts about somebody's credential, so the
    response carries neither -- the reason is logged instead, against the key id, which is the half
    an operator can act on.
    """
    _install()
    bearer = _provision()
    key_id = bearer.split(".")[0]
    try:
        client = _client()
        for headers in (
            {},
            {"Authorization": "Bearer nonsense"},
            {"Authorization": f"Bearer {key_id}.wrong-secret"},
            {"Authorization": f"Bearer kixn_{'0' * 20}.whatever"},
        ):
            response = client.get("/notification-api/v1/subscriptions", headers=headers)
            assert response.status_code == 401, headers
            assert response.json() == {"detail": "unauthorized"}
            assert response.headers.get("www-authenticate") == "Bearer"
        # A cookie is never an authenticator here: the app allows every CORS origin, so a cookie
        # would be sent from all of them.
        client.cookies.set("kix_sso", "anything")
        assert client.get("/notification-api/v1/subscriptions").status_code == 401
    finally:
        _uninstall()


def test_a_revoked_credential_stops_working():
    _install()
    bearer = _provision()
    try:
        client = _client()
        assert client.get("/notification-api/v1/subscriptions", headers=_headers(bearer)).status_code == 200
        partner_auth.revoke_credential(bearer.split(".")[0], actor="op@entur.org")
        assert client.get("/notification-api/v1/subscriptions", headers=_headers(bearer)).status_code == 401
    finally:
        _uninstall()


def test_a_suspended_partner_gets_403_so_it_can_tell_the_two_apart():
    _install()
    bearer = _provision()
    try:
        partner_auth.set_partner_suspended(PARTNER, suspended=True, actor="op@entur.org")
        response = _client().get("/notification-api/v1/subscriptions", headers=_headers(bearer))
        assert response.status_code == 403
        assert response.json() == {"detail": "partner_suspended"}
    finally:
        _uninstall()


def test_one_partner_cannot_reach_anothers_records_and_the_body_names_nothing():
    """AE11's second half. Ownership comes from the credential, so the reference is namespaced.

    Three probes, one answer. Partner A asking for B's own reference gets A's namespace and finds
    nothing; A asking with B's namespaced id gets it treated as a reference (the prefix stripped is
    only ever A's own) and finds nothing; and a record that somehow carries another partner_id
    raises Forbidden inside the store and is collapsed to 404 here.
    """
    _install()
    a_bearer = _provision(PARTNER, DEST)
    b_bearer = _provision(OTHER, OTHER_DEST)
    try:
        client = _client()
        created = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="shared-ref", destination_id=OTHER_DEST),
            headers=_headers(b_bearer),
        )
        assert created.status_code == 201, created.text
        # Both partners may use the same reference; they are different records.
        mine = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="shared-ref"),
            headers=_headers(a_bearer),
        )
        assert mine.status_code == 201
        assert mine.json()["partner_id"] == PARTNER
        assert mine.json()["subscription_id"] == "shared-ref"

        for probe in (f"{OTHER}~shared-ref", "unknown-ref"):
            response = client.get(
                f"/notification-api/v1/subscriptions/{probe}", headers=_headers(a_bearer)
            )
            assert response.status_code == 404, probe
            assert response.json() == {"detail": "not_found"}
            assert OTHER not in response.text

        # A stored record carrying a foreign partner_id: Forbidden inside the store, 404 here.
        _fake.docs[f"{partner_store.SUBSCRIPTIONS}/{PARTNER}~squatted"] = {
            **partner_store._new_subscription_doc(
                partner_store.SubscriptionSpec(
                    subscription_id=f"{PARTNER}~squatted",
                    partner_id=OTHER,
                    destination_id=OTHER_DEST,
                    from_stop=FROM,
                    to_stop=TO,
                    begin="06:45",
                    end="08:30",
                    days=(0,),
                ),
                NOW,
            )
        }
        response = client.get(
            "/notification-api/v1/subscriptions/squatted", headers=_headers(a_bearer)
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "not_found"}
        assert OTHER not in response.text
    finally:
        _uninstall()


def test_a_partner_id_in_the_body_is_rejected_rather_than_honoured():
    """Ownership is a property of the credential, never a request-shaped choice."""
    _install()
    bearer = _provision()
    try:
        response = _client().post(
            "/notification-api/v1/subscriptions",
            json={**_body(), "partner_id": OTHER},
            headers=_headers(bearer),
        )
        assert response.status_code == 422
    finally:
        _uninstall()


def test_an_unknown_or_foreign_destination_is_one_answer():
    """The same collapse as a foreign record: a partner must not learn another's destination ids."""
    _install()
    a_bearer = _provision(PARTNER, DEST)
    _provision(OTHER, OTHER_DEST)
    try:
        client = _client()
        for destination in (OTHER_DEST, "no-such-destination"):
            response = client.post(
                "/notification-api/v1/subscriptions",
                json=_body(destination_id=destination),
                headers=_headers(a_bearer),
            )
            assert response.status_code == 422, destination
            assert response.json() == {"detail": "unknown_destination"}
            assert OTHER not in response.text
    finally:
        _uninstall()


# ---- Scenario 2 / AE13: servability ------------------------------------------------------------


def test_an_unservable_pair_is_stored_and_carries_the_caveat():
    """AE13's first half. Never refused: weekend-only and seasonal routes are why that would be
    wrong, and an unregisterable journey is worse than a registered one carrying a caveat."""
    _install(connectivity=lambda a, b: (False, True))
    bearer = _provision()
    try:
        response = _client().post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["servable"] is False
        assert payload["servable_checked_at"] is not None
    finally:
        _uninstall()


def test_a_provider_timeout_stores_no_caveat_rather_than_a_false_one():
    """AE13's second half. `None` means "could not be established" and must never read as an
    all-clear -- nor as "no trips run", which storing False would have said."""
    _install(connectivity=lambda a, b: None)
    bearer = _provision()
    try:
        payload = _client().post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        ).json()
        assert payload["servable"] is None
        assert payload["servable_checked_at"] is None
    finally:
        _uninstall()


def test_a_raising_connectivity_provider_never_fails_the_registration():
    _install(connectivity=lambda a, b: (_ for _ in ()).throw(RuntimeError("bigquery down")))
    bearer = _provision()
    try:
        response = _client().post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert response.status_code == 201
        assert response.json()["servable"] is None
    finally:
        _uninstall()


def test_a_changed_route_retires_the_old_servability_answer():
    """R6: a stale answer must never read as an all-clear, so an amendment re-asks."""
    answers = iter([(True, True), (False, False)])
    _install(connectivity=lambda a, b: next(answers))
    bearer = _provision()
    try:
        client = _client()
        first = client.post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        ).json()
        assert first["servable"] is True
        second = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(to_stop="NSR:StopPlace:591"),
            headers=_headers(bearer),
        ).json()
        assert second["servable"] is False
        assert second["version"] == 2
    finally:
        _uninstall()


# ---- Scenario 3: retry, conflict, tombstone ----------------------------------------------------


def test_an_identical_retry_is_stable():
    """R5's safe retry: same reference, same definition, same record and no new version.

    The store's create path performs no write at all for this, so a retry cannot bump a version,
    reset a horizon or clear a spent cap slot. What the route adds is the inactivity touch -- a
    partner re-POSTing its watch IS the "still interested" signal R5's horizon is measured from.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        first = client.post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert first.status_code == 201
        second = client.post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert second.status_code == 200
        assert second.json()["version"] == first.json()["version"] == 1
        assert second.json()["created_at"] == first.json()["created_at"]
        assert len(_fake.in_collection(partner_store.SUBSCRIPTIONS)) == 1
    finally:
        _uninstall()


def test_a_conflicting_update_fails_rather_than_overwriting():
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer))
        stale = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(begin="07:00"),
            headers={**_headers(bearer), "If-Match": "99"},
        )
        assert stale.status_code == 409
        assert stale.json() == {"detail": "version_conflict"}
        fresh = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(begin="07:00"),
            headers={**_headers(bearer), "If-Match": "1"},
        )
        assert fresh.status_code == 200
        assert fresh.json()["version"] == 2
        assert fresh.json()["begin"] == "07:00"
        # An If-Match on a reference that does not exist cannot be satisfied.
        missing = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="brand-new"),
            headers={**_headers(bearer), "If-Match": "1"},
        )
        assert missing.status_code == 409
        # And a malformed one is a 400 rather than being silently ignored.
        malformed = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(),
            headers={**_headers(bearer), "If-Match": "not-a-number"},
        )
        assert malformed.status_code == 400
    finally:
        _uninstall()


def test_a_tombstoned_reference_cannot_be_resurrected():
    """The id is spent and its record is the audit trail; a re-registration is a NEW subscription.

    The window this closes is the one between the tombstone and the cleanup completing, where a
    same-definition re-POST looks exactly like a safe retry -- which is precisely where nobody is
    looking.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer))
        assert client.delete(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(bearer)
        ).status_code == 200
        again = client.post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert again.status_code == 409
        assert "new id" in again.json()["detail"]
        assert client.get(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(bearer)
        ).status_code == 404
    finally:
        _uninstall()


def test_deletion_is_ordered_idempotent_and_reports_its_cleanup():
    """R13, read off the write LOG rather than off the end state.

    The end state is identical whichever half ran first, so ordering has to be checked as a
    sequence: the tombstone -- one transactional write that stops routing, because the poller's
    load query filters status == "active" -- must precede every document delete. Reversed, a partial
    failure leaves a routable subscription whose events are already gone.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer))
        document_id = f"{PARTNER}~commuter-42-morning"
        for index in range(3):
            _fake.put(
                partner_store.EVENTS,
                f"e{index}",
                {"event_id": f"e{index}", "subscription_id": document_id},
            )
            _fake.put(
                partner_store.DELIVERIES,
                f"d{index}",
                {"delivery_id": f"d{index}", "subscription_id": document_id},
            )
        _fake.put(
            partner_store.SUBJECTS, "s0", {"alert_id": "s0", "subscription_id": document_id}
        )
        _fake.log.clear()

        response = client.delete(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(bearer)
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["existed"] is True
        assert payload["status"] == "deleted"
        assert payload["cleanup"]["state"] == "done"
        assert payload["cleanup"]["deleted_events"] == 3
        assert payload["cleanup"]["deleted_deliveries"] == 3
        assert payload["cleanup"]["deleted_subjects"] == 1
        assert payload["cleanup"]["residual_hours"] == partner_store.TTL_RESIDUAL_HOURS

        tombstone_at = next(
            i for i, entry in enumerate(_fake.log) if entry.endswith(f"/{document_id}")
        )
        deletes = [i for i, entry in enumerate(_fake.log) if entry.startswith("delete:")]
        assert deletes, "the cleanup must actually have deleted something"
        assert tombstone_at < min(deletes), _fake.log

        assert _fake.in_collection(partner_store.EVENTS) == []
        assert _fake.in_collection(partner_store.DELIVERIES) == []
        assert _fake.in_collection(partner_store.SUBJECTS) == []
        # The tombstone's route is redacted the moment cleanup completes.
        tombstone = _fake.doc(partner_store.SUBSCRIPTIONS, document_id)
        assert tombstone["status"] == "deleted"
        assert tombstone["from_stop"] == "" and tombstone["partner_ref"] == ""
        assert tombstone["cleanup"]["state"] == "done"

        # Repeated deletion succeeds, and so does deleting something that never existed.
        assert client.delete(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(bearer)
        ).status_code == 200
        nothing = client.delete(
            "/notification-api/v1/subscriptions/never-existed", headers=_headers(bearer)
        )
        assert nothing.status_code == 200
        assert nothing.json()["existed"] is False
        assert nothing.json()["cleanup"] is None
    finally:
        _uninstall()


# ---- Scenario 4: /varsel's own verdicts --------------------------------------------------------


def test_the_window_and_weekday_verdicts_are_varsels_own_word_for_word():
    """Scenario 4. Overnight windows, a bad weekday and from == to, refused as /varsel refuses them.

    The point is not that a 422 comes back -- it is that the SAME sentence does. R2's shape lives in
    `partner_store.SubscriptionSpec.validate`, which restates /varsel's three rules and is pinned
    against them by that module's own suite; this route reaches it rather than adding a fourth copy,
    and the strings below are checked against varsel.py so a divergence fails here too.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        cases = [
            (_body(begin="22:00", end="06:00"), "overnight windows are not supported"),
            (_body(days=[0, 9]), "days must be a non-empty subset of 0..6"),
            (_body(days=[]), "days must be a non-empty subset of 0..6"),
            (_body(begin="08:00", end="08:00"), "begin must be before end"),
            (_body(to_stop=FROM), "from_stop and to_stop must differ"),
        ]
        for payload, fragment in cases:
            response = client.post(
                "/notification-api/v1/subscriptions", json=payload, headers=_headers(bearer)
            )
            assert response.status_code == 422, (payload, response.text)
            assert fragment in response.json()["detail"], (payload, response.text)
        # The verdicts are /varsel's, not this surface's invention.
        assert "overnight windows are not supported" in VARSEL_SOURCE
        assert "days must be a non-empty subset of 0..6 (0=Monday)" in VARSEL_SOURCE
        assert "from_stop and to_stop must differ" in VARSEL_SOURCE
    finally:
        _uninstall()


def test_the_router_does_not_restate_the_window_rule():
    """The fourth copy is the failure mode, so its absence is checked rather than trusted.

    `partner_store.SubscriptionSpec.validate` is statement three (JourneyIn and
    `varsel._parse_partner_recipient` are one and two). A comparison of begin and end anywhere in
    this module would be a fourth, and the two surfaces would then drift silently.
    """
    for forbidden in ("begin >= ", "begin >=", "_parse_hhmm", "_HHMM_RE", "range(0, 7)"):
        assert forbidden not in API_CODE, forbidden
    assert "spec.validate()" in API_CODE


def test_a_collapsed_stop_ref_is_canonicalised_before_it_is_stored():
    """/varsel's rule, applied on the way in: a client holding a stale stop list names the absorbed
    side of a station, and storing it would recreate the silent no-match the collapse removes."""
    _install()
    bearer = _provision()
    try:
        payload = _client().post(
            "/notification-api/v1/subscriptions",
            json=_body(from_stop=COLLAPSED),
            headers=_headers(bearer),
        ).json()
        assert payload["from_stop"] == FROM
    finally:
        _uninstall()


def test_two_refs_that_collapse_to_one_place_are_refused():
    """Reachable without the client doing anything odd: two refs for one physical place look like
    two stops in an older list, so the from != to check has to run AFTER canonicalising."""
    _install()
    bearer = _provision()
    try:
        response = _client().post(
            "/notification-api/v1/subscriptions",
            json=_body(from_stop=COLLAPSED, to_stop=FROM),
            headers=_headers(bearer),
        )
        assert response.status_code == 422
        assert "must differ" in response.json()["detail"]
    finally:
        _uninstall()


def test_a_non_nsr_stop_ref_is_refused_by_the_published_shape():
    _install()
    bearer = _provision()
    try:
        response = _client().post(
            "/notification-api/v1/subscriptions",
            json=_body(from_stop="Oslo S"),
            headers=_headers(bearer),
        )
        assert response.status_code == 422
    finally:
        _uninstall()


# ---- Scenario 5 / AE18: the test-only marker ---------------------------------------------------


def test_a_synthetic_event_is_refused_for_a_live_subscription():
    """AE18's first half. The marker is set at creation and immutable, so the only way to a test
    event is a watch that was always one."""
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer))
        response = client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": "commuter-42-morning"},
            headers=_headers(bearer),
        )
        assert response.status_code == 409
        assert response.json() == {"detail": "not_a_test_subscription"}
        assert _deliveries == [], "nothing may be dialled for a live subscription"
    finally:
        _uninstall()


def test_a_replace_cannot_clear_or_set_the_test_marker():
    """AE18's second half, both directions. A subscription that could be flipped live would turn
    every previously-signed test event into a real notification."""
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="probe", test=True),
            headers=_headers(bearer),
        )
        cleared = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="probe", test=False),
            headers=_headers(bearer),
        )
        assert cleared.status_code == 409
        assert "immutable" in cleared.json()["detail"]

        client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="live"),
            headers=_headers(bearer),
        )
        promoted = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="live", test=True),
            headers=_headers(bearer),
        )
        assert promoted.status_code == 409
        assert "immutable" in promoted.json()["detail"]
        assert _fake.doc(partner_store.SUBSCRIPTIONS, f"{PARTNER}~live")["test"] is False
    finally:
        _uninstall()


def test_a_test_event_is_signed_over_the_exact_bytes_and_stores_nothing():
    """R15, and the single-writer claim the whole store rests on.

    Nothing is written: no event, no delivery, no alert subject. That is not tidiness --
    `partner_store`'s pending-event ceiling is exact rather than best-effort BECAUSE the emit path
    has exactly one writer, so a route calling `commit_event` would falsify every quota check in
    that module.

    The signature is verified the way a receiver would: over the raw body, under a secret derived
    from the destination and generation, with the `test` marker INSIDE those bytes rather than in a
    header a proxy could strip.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="probe", test=True),
            headers=_headers(bearer),
        )
        before = dict(_fake.docs)
        response = client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": "probe"},
            headers=_headers(bearer),
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["delivered"] is True
        assert payload["status"] == 200
        assert payload["signed_generations"] == [1]
        assert payload["subscription_id"] == "probe"

        assert _fake.in_collection(partner_store.EVENTS) == []
        assert _fake.in_collection(partner_store.DELIVERIES) == []
        assert _fake.in_collection(partner_store.SUBJECTS) == []
        assert _fake.docs == before, "a test event writes nothing at all"

        assert len(_deliveries) == 1
        attempt = _deliveries[0]
        assert attempt["url"] == GOOD_URL
        event = json.loads(attempt["body"])
        assert event["test"] is True
        assert event["event_id"] == payload["event_id"]
        verification = webhook_signing.verify(
            secret=webhook_signing.derive_secret(DEST, 1),
            webhook_id=attempt["headers"][webhook_signing.HEADER_ID],
            timestamp=attempt["headers"][webhook_signing.HEADER_TIMESTAMP],
            body=attempt["body"],
            header=attempt["headers"][webhook_signing.HEADER_SIGNATURE],
        )
        assert verification.ok, verification
        # The webhook id IS the event id, so a receiver can dedupe on either.
        assert attempt["headers"][webhook_signing.HEADER_ID] == payload["event_id"]
        # Two calls, two events: a receiver's duplicate rule must not swallow the second.
        second = client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": "probe"},
            headers=_headers(bearer),
        )
        assert second.json()["event_id"] != payload["event_id"]
    finally:
        _uninstall()


def test_the_synthetic_event_conforms_to_the_published_event_schema():
    """U7 generates its receiver types from `event.schema.json`, so a test event that does not
    conform teaches a partner the wrong shape at exactly the moment they are learning it."""
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="probe", test=True),
            headers=_headers(bearer),
        )
        client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": "probe"},
            headers=_headers(bearer),
        )
        event = json.loads(_deliveries[0]["body"])
        for key in EVENT_SCHEMA["required"]:
            assert key in event, key
        assert set(event) <= set(EVENT_SCHEMA["properties"]), set(event) - set(
            EVENT_SCHEMA["properties"]
        )
        assert event["schema_version"] == EVENT_SCHEMA["properties"]["schema_version"]["const"]
        assert event["type"] in EVENT_SCHEMA["properties"]["type"]["enum"]
        assert event["phase"] in EVENT_SCHEMA["properties"]["phase"]["enum"]
        assert event["revision"] >= EVENT_SCHEMA["properties"]["revision"]["minimum"]
        assert event["subscription_version"] >= 1
        notification = event["payload"]["notification"]
        assert notification["title"] and notification["body"]
        # Norwegian, and marked as a test in the TEXT: a notification that reaches a real lock
        # screen during integration has to say so on the screen, not only in a field.
        assert "Testvarsel" in notification["title"]
        assert "testvarsel" in notification["body"].lower()
    finally:
        _uninstall()


def test_a_paused_destination_refuses_a_test_event():
    """R12: while a destination is paused nothing is dialled, and that includes a test."""
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="probe", test=True),
            headers=_headers(bearer),
        )
        partner_auth.pause_destination(DEST, reason="410 Gone")
        response = client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": "probe"},
            headers=_headers(bearer),
        )
        assert response.status_code == 409
        assert response.json() == {"detail": "destination_paused"}
        assert _deliveries == []
    finally:
        _uninstall()


def test_a_test_event_reports_a_partners_own_failure_as_the_diagnosis():
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="probe", test=True),
            headers=_headers(bearer),
        )
        _delivery_result.clear()
        _delivery_result.update(
            {"ok": False, "status": 500, "retryable": True, "error": "partner returned 500"}
        )
        response = client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": "probe"},
            headers=_headers(bearer),
        )
        assert response.status_code == 200, "the request succeeded; the delivery did not"
        assert response.json()["delivered"] is False
        assert response.json()["status"] == 500
        assert response.json()["error"] == "partner returned 500"
    finally:
        _uninstall()


def test_the_test_event_route_is_rate_limited_on_its_own():
    """The one route that makes an outbound call, so it carries its own tight ceiling: this app must
    not become a request amplifier against a partner-supplied host."""
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="probe", test=True),
            headers=_headers(bearer),
        )
        statuses = [
            client.post(
                "/notification-api/v1/test-events",
                json={"subscription_id": "probe"},
                headers=_headers(bearer),
            ).status_code
            for _ in range(partner_api._TEST_EVENTS_PER_MIN + 2)
        ]
        assert statuses.count(200) == partner_api._TEST_EVENTS_PER_MIN
        assert statuses[-1] == 429
        assert len(_deliveries) == partner_api._TEST_EVENTS_PER_MIN
    finally:
        _uninstall()


def test_signing_that_is_not_configured_is_503_rather_than_an_unsigned_request():
    """Fail closed and fail visible: a partner endpoint that accepts an unsigned request is a hole
    on their side, and one that rejects it is a retry storm on ours."""
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="probe", test=True),
            headers=_headers(bearer),
        )
        os.environ.pop(webhook_signing.SIGNING_ROOT_ENV, None)
        response = client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": "probe"},
            headers=_headers(bearer),
        )
        assert response.status_code == 503
        assert response.json() == {"detail": "signing_not_configured"}
        assert _deliveries == []
    finally:
        _uninstall()


# ---- Scenario 6: rotation over HTTP -----------------------------------------------------------


def test_both_credentials_authenticate_during_a_rotation():
    """Scenario 6 through the surface a partner actually cuts over on."""
    _install()
    retiring = _provision()
    try:
        client = _client()
        _record, replacement = partner_auth.issue_credential(PARTNER, actor="op@entur.org")
        for bearer in (retiring, replacement):
            assert client.get(
                "/notification-api/v1/subscriptions", headers=_headers(bearer)
            ).status_code == 200
        partner_auth.revoke_credential(retiring.split(".")[0], actor="op@entur.org")
        assert client.get(
            "/notification-api/v1/subscriptions", headers=_headers(retiring)
        ).status_code == 401
        assert client.get(
            "/notification-api/v1/subscriptions", headers=_headers(replacement)
        ).status_code == 200
    finally:
        _uninstall()


# ---- Scenario 7: the two-tier limiter over HTTP ------------------------------------------------


def test_a_repeated_unknown_credential_costs_no_credential_lookup():
    """Scenario 7 at the edge: the negative cache is what makes a bad credential cheap."""
    _install()
    _provision()
    try:
        client = _client()
        headers = {"Authorization": f"Bearer kixn_{'a' * 20}.nope"}
        before = _auth_docs.reads
        for _ in range(15):
            assert client.get(
                "/notification-api/v1/subscriptions", headers=headers
            ).status_code == 401
        assert _auth_docs.reads - before == 1, _auth_docs.reads - before
    finally:
        _uninstall()


def test_a_limiter_rejection_carries_a_bounded_retry_after():
    _install()
    bearer = _provision()
    try:
        client = _client()
        principal = partner_auth.authenticate(f"Bearer {bearer}")
        for _ in range(partner_auth.PER_PARTNER_PER_MIN):
            partner_auth.claim_partner_slot(principal.partner_id)
        response = client.get("/notification-api/v1/subscriptions", headers=_headers(bearer))
        assert response.status_code == 429
        assert response.json() == {"detail": "rate_limited"}
        assert 0 < int(response.headers["retry-after"]) <= 60
        # Scenario 10's other half, literally: a rate-limited WRITE leaves nothing behind. The tier
        # is a router-level dependency, so the handler never runs.
        before = dict(_fake.docs)
        rejected = client.post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert rejected.status_code == 429
        assert _fake.docs == before
    finally:
        _uninstall()


# ---- Scenario 8 is partner_auth's; scenario 9: the SSO gate -----------------------------------


def test_the_partner_prefix_is_deliberately_not_behind_the_browser_sso_gate():
    """Scenario 9. That gate answers an unconfigured deployment with 503 and an unauthenticated
    BROWSER with a 302 -- and a machine caller following a redirect into a Google login screen gets
    an HTML page where it expected JSON. The credential dependency is the gate here."""
    assert len(sso.PROTECTED_PREFIXES) == 8
    assert not any(p.startswith("/notification") for p in sso.PROTECTED_PREFIXES)
    assert sso._is_protected(partner_api.PREFIX) is False
    assert sso._is_protected("/notification-api/v1/subscriptions") is False


# ---- Scenario 10: a store outage leaves nothing behind ----------------------------------------


def test_a_store_outage_leaves_no_partial_subscription_and_no_event():
    """Scenario 10's first half. R20 at the API edge: 503, and nothing written.

    503 rather than 500 because it is retryable, and the traceback stays in the log rather than in
    the response -- a partner debugging their own integration must not be reading our stack.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        _fake.fail_with = RuntimeError("firestore unavailable")
        response = client.post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert response.status_code == 503
        assert response.json() == {"detail": "store_unavailable"}
        assert int(response.headers["retry-after"]) > 0
        _fake.fail_with = None
        assert _fake.in_collection(partner_store.SUBSCRIPTIONS) == []
        assert _fake.in_collection(partner_store.EVENTS) == []
        assert _fake.in_collection(partner_store.DELIVERIES) == []
    finally:
        _uninstall()


def test_an_unconfigured_deployment_answers_503_on_every_route():
    """tst has neither a database nor a pepper today, so it serves 503 -- honest, and never a 401
    about a credential that is fine."""
    _install(pepper=None)
    try:
        client = _client()
        for method, path in (
            ("get", "/notification-api/v1/subscriptions"),
            ("get", "/notification-api/v1/subscriptions/x"),
            ("delete", "/notification-api/v1/subscriptions/x"),
            ("get", "/notification-api/v1/destinations/current"),
        ):
            response = getattr(client, method)(path, headers=_headers("kixn_x.y"))
            assert response.status_code == 503, (method, path)
            assert response.json() == {"detail": "channel_not_configured"}
    finally:
        _uninstall()


def test_the_active_subscription_ceiling_is_refused_before_any_write():
    """R21: refused before any store write, with a bounded Retry-After, and the ceiling is a number.

    Longer than the limiter's 60 s deliberately: a subscription-count ceiling does not clear when a
    window rolls over, so a one-minute hint would invite the retry loop the ceiling exists to stop.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        for index in range(partner_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_PARTNER):
            _fake.put(
                partner_store.SUBSCRIPTIONS,
                f"{PARTNER}~filler-{index}",
                partner_store._new_subscription_doc(
                    partner_store.SubscriptionSpec(
                        subscription_id=f"{PARTNER}~filler-{index}",
                        partner_id=PARTNER,
                        destination_id=DEST,
                        from_stop=FROM,
                        to_stop=TO,
                        begin="06:45",
                        end="08:30",
                        days=(0,),
                    ),
                    NOW,
                ),
            )
        before = len(_fake.docs)
        response = client.post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert response.status_code == 429
        assert response.json() == {"detail": "quota_exceeded"}
        assert int(response.headers["retry-after"]) == partner_store.RETRY_AFTER_CAP_S
        assert len(_fake.docs) == before, "refused before any write"
        # R21's "a breach is visible to an operator". The ceiling is enforced inside a transaction
        # body, which logs nothing by design (a body may run more than once), so the route is the
        # only place it can be recorded once per request.
        records: list[Any] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        partner_api.logger.addHandler(handler)
        try:
            client.post(
                "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
            )
        finally:
            partner_api.logger.removeHandler(handler)
        breaches = [
            r
            for r in records
            if getattr(r, "fields", {}).get("event") == "partner_quota"
            and getattr(r, "fields", {}).get("result") == "subscriptions"
        ]
        assert len(breaches) == 1, records
        assert breaches[0].fields["partner_id"] == PARTNER
    finally:
        _uninstall()


# ---- Listing, the destination, and the published contract -------------------------------------


def test_listing_is_a_slice_of_a_bounded_read():
    _install()
    bearer = _provision()
    try:
        client = _client()
        for index in range(5):
            client.post(
                "/notification-api/v1/subscriptions",
                json=_body(subscription_id=f"ref-{index}"),
                headers=_headers(bearer),
            )
        first = client.get(
            "/notification-api/v1/subscriptions?limit=2", headers=_headers(bearer)
        ).json()
        assert first["count"] == 2
        assert first["limit"] == 2
        assert first["next_cursor"] == first["subscriptions"][-1]["subscription_id"]
        second = client.get(
            f"/notification-api/v1/subscriptions?limit=2&after={first['next_cursor']}",
            headers=_headers(bearer),
        ).json()
        assert [s["subscription_id"] for s in second["subscriptions"]] != [
            s["subscription_id"] for s in first["subscriptions"]
        ]
        last = client.get(
            "/notification-api/v1/subscriptions?limit=100", headers=_headers(bearer)
        ).json()
        assert last["count"] == 5
        assert last["next_cursor"] is None
        # A cursor the current set no longer contains is a 400, not an empty page: "that watch is
        # gone" and "nothing matched" are different answers.
        unknown = client.get(
            "/notification-api/v1/subscriptions?after=no-such-ref", headers=_headers(bearer)
        )
        assert unknown.status_code == 400
        assert unknown.json() == {"detail": "unknown_cursor"}
        # And the page size is bounded by R21's own ceiling.
        assert client.get(
            f"/notification-api/v1/subscriptions?limit={partner_api._LIST_MAX + 1}",
            headers=_headers(bearer),
        ).status_code == 422
    finally:
        _uninstall()


def test_the_destination_response_carries_generation_numbers_and_never_a_secret():
    """"We do not store it" and "we do not serve it" are different claims, so both are checked."""
    _install()
    bearer = _provision()
    try:
        client = _client()
        response = client.get(
            "/notification-api/v1/destinations/current", headers=_headers(bearer)
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["destination_id"] == DEST
        assert payload["callback_url"] == GOOD_URL
        assert payload["paused"] is False
        assert payload["ready"] is True
        assert payload["signing_generations"] == {"active": 1, "retiring": None}
        assert webhook_signing.SECRET_PREFIX not in response.text
        assert SIGNING_ROOT not in response.text

        partner_auth.rotate_signing_generation(DEST, actor="op@entur.org")
        rotated = client.get(
            "/notification-api/v1/destinations/current", headers=_headers(bearer)
        ).json()
        assert rotated["signing_generations"] == {"active": 2, "retiring": 1}
        assert webhook_signing.SECRET_PREFIX not in json.dumps(rotated)

        partner_auth.pause_destination(DEST, reason="410 Gone")
        paused = client.get(
            "/notification-api/v1/destinations/current", headers=_headers(bearer)
        ).json()
        assert paused["paused"] is True
        assert paused["ready"] is False
        assert paused["paused_reason"] == "410 Gone"
    finally:
        _uninstall()


def test_a_partner_with_no_destination_gets_404_rather_than_an_empty_record():
    _install()
    try:
        partner_auth.create_partner(PARTNER, name="Acme", actor="op@entur.org")
        _record, bearer = partner_auth.issue_credential(PARTNER, actor="op@entur.org")
        response = _client().get(
            "/notification-api/v1/destinations/current", headers=_headers(bearer)
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "no_destination"}
    finally:
        _uninstall()


def test_the_response_model_matches_the_published_schema_in_both_directions():
    """`subscription.schema.json` is `additionalProperties: false` and U7 generates types from it,
    so a field added on one side and not the other breaks a partner's build silently."""
    served = set(partner_api.SubscriptionOut.model_fields)
    published = set(SUBSCRIPTION_SCHEMA["properties"])
    assert served == published, (served - published, published - served)
    # Every property the published schema REQUIRES is served non-optionally in spirit: the writable
    # ones are all present on the response.
    for key in SUBSCRIPTION_SCHEMA["required"]:
        assert key in served, key
    # And the input model accepts exactly the writable half.
    writable = set(OPENAPI["components"]["schemas"]["SubscriptionCreate"]["properties"])
    assert set(partner_api.SubscriptionIn.model_fields) == writable
    assert "alerts" not in writable, "alert state is read-only; a request may not carry it"

    # The nested alert state, both directions too. It is two levels deep and
    # `additionalProperties: false` at each, so a field added on one side and not the other breaks
    # a partner's generated types exactly as a top-level one would -- and more quietly.
    alerts = published_schema = SUBSCRIPTION_SCHEMA["properties"]["alerts"]
    assert set(partner_api.AlertStateOut.model_fields) == set(alerts["properties"])
    assert set(alerts["required"]) == set(alerts["properties"])
    subject = alerts["properties"]["subjects"]["items"]
    assert set(partner_api.AlertSubjectOut.model_fields) == set(subject["properties"])
    assert set(subject["required"]) == set(subject["properties"])
    # The cap is published, not implied, and it is the store's own number.
    assert alerts["properties"]["subjects"]["maxItems"] == partner_store.MAX_ALERTS_SERVED
    # `null` is a legal value: a create response does not read the alert state.
    assert "null" in published_schema["type"]
    # The train's fields are REFERENCED from the event schema rather than restated, so a card and a
    # notification cannot come to describe one train in two vocabularies.
    for key in ("alert_id", "revision", "operator", "line", "journey_ref", "call_date",
                "stop_place", "stop_order"):
        assert subject["properties"][key] == {
            "$ref": f"./event.schema.json#/properties/{key}"
        }, key


def test_the_openapi_document_renders_the_authored_schemas_rather_than_restating_them():
    """Two definitions of one payload is how they drift, and this document is what U7 generates
    from -- so every subscription property here is a `$ref` into the authored file, and the outbound
    event contract is published as a 3.1 `webhooks` entry referencing the event schema."""
    assert OPENAPI["openapi"].startswith("3.1")
    text = json.dumps(OPENAPI)
    assert "./subscription.schema.json" in text
    assert OPENAPI["components"]["schemas"]["Subscription"] == {
        "$ref": "./subscription.schema.json"
    }
    create = OPENAPI["components"]["schemas"]["SubscriptionCreate"]
    for key, value in create["properties"].items():
        assert value == {"$ref": f"./subscription.schema.json#/properties/{key}"}, key
        assert key in SUBSCRIPTION_SCHEMA["properties"], key
    assert create["additionalProperties"] is False
    webhook = OPENAPI["webhooks"]["journeyAlert"]["post"]
    assert webhook["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "./event.schema.json"
    }
    # The Standard Webhooks headers a receiver must verify are named where a partner will look.
    declared = {p["name"] for p in webhook["parameters"]}
    assert declared == {
        webhook_signing.HEADER_ID,
        webhook_signing.HEADER_TIMESTAMP,
        webhook_signing.HEADER_SIGNATURE,
    }
    # Bearer, never a cookie: one global CORS policy allows every origin.
    scheme = OPENAPI["components"]["securitySchemes"]["partnerKey"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer"
    # Checked on the DECLARATION rather than by searching the text: the description says the word
    # "cookie" in order to rule it out, so a substring search would fail on the explanation.
    for declared in OPENAPI["components"]["securitySchemes"].values():
        assert declared.get("in") != "cookie", declared
        assert declared["type"] != "apiKey", declared
    assert OPENAPI["security"] == [{"partnerKey": []}]


def test_the_openapi_paths_are_exactly_the_routers_routes():
    """A published contract that has drifted from the router is worse than none: a partner writes a
    client against it. Checked in both directions, and the six are the plan's own API table."""
    routes = {
        (route.path[len(partner_api.PREFIX) :] or "/", method.lower())
        for route in partner_api.router.routes
        for method in route.methods
        if method not in ("HEAD", "OPTIONS")
    }
    published = {
        (path, method)
        for path, operations in OPENAPI["paths"].items()
        for method in operations
        if method in ("get", "post", "put", "delete", "patch")
    }
    assert routes == published, (routes - published, published - routes)
    assert len(published) == 6
    for server in OPENAPI["servers"]:
        assert server["url"].endswith(partner_api.PREFIX)


def test_every_route_is_gated_at_router_level_as_well_as_by_its_parameter():
    """Defence in depth, in the shape `deployment.require_rushvarsel` uses one surface over: a route
    added later is gated even if its author forgets the principal parameter. FastAPI caches an
    identical dependency within a request, so the credential is still verified once."""
    dependencies = [d.dependency for d in partner_api.router.dependencies]
    assert partner_api._authenticated in dependencies
    assert partner_api._throttled in dependencies, (
        "tier two is spent by a router-level dependency, so 'before anything is created' is a "
        "property of the wiring rather than of whoever wrote the newest handler"
    )
    for route in partner_api.router.routes:
        signature = getattr(route, "endpoint", None)
        assert signature is not None
        assert "principal" in signature.__code__.co_varnames


def test_every_route_is_a_plain_def():
    """They touch Firestore, so they belong in the anyio worker pool like every other route here.

    `/meldinger`'s `async def` inversion is the precedent only for a handler that would otherwise
    hold one of those slots for its whole deadline, which none of these does.
    """
    import inspect

    for route in partner_api.router.routes:
        assert not inspect.iscoroutinefunction(route.endpoint), route.path


def test_the_route_bodies_forbid_unknown_fields():
    """`extra="forbid"` on both sides: on the way in it stops a typo being silently ignored, and on
    the way out it is the drift guard against the published schema."""
    for model in (
        partner_api.SubscriptionIn,
        partner_api.SubscriptionOut,
        partner_api.TestEventIn,
        partner_api.TestEventOut,
        partner_api.DestinationOut,
        partner_api.DeleteResult,
        partner_api.CleanupState,
        partner_api.SubscriptionList,
        partner_api.SigningGenerations,
    ):
        assert model.model_config.get("extra") == "forbid", model.__name__


def test_the_module_never_reaches_the_decision_log_or_the_device_registry():
    """KTD10 and R16: partner state is Firestore's. `varsel_decisions` is written for every match
    including sub-threshold ones and is published on public hosts, and its `pushed` column cannot
    tell a webhook from a Web Push -- so writing partner deliveries there would corrupt both
    /historikk's and /meldinger's meaning as well as their privacy claim."""
    for forbidden in ("decisions", "varsel_decisions", "import varsel", "journey_digest"):
        assert forbidden not in API_CODE, forbidden
    assert "commit_event" not in API_CODE, (
        "the emit path has exactly one writer; a route calling it falsifies every quota check in "
        "partner_store"
    )


# ---- Current alert state on both GET routes ----------------------------------------------------


def test_the_get_routes_serve_the_current_alert_state():
    """The plan's API table promises servability AND current alert state on both GETs.

    A read of what the notification pass has already committed -- one document per (watch, train)
    with its revision and phase -- so nothing here consults the matcher or `/impact`. The train's
    six fields are the ones an event carries, which is what lets a partner join a card to a
    notification.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer))
        document_id = f"{PARTNER}~commuter-42-morning"
        _put_subject(document_id, "alert-1", partner_store.PHASE_PREDICTED)
        _put_subject(
            document_id, "alert-2", partner_store.PHASE_CONFIRMED, delivered_revision=7
        )

        one = client.get(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(bearer)
        ).json()
        alerts = one["alerts"]
        assert alerts["n_active"] == 2
        assert alerts["n_confirmed"] == 1
        assert alerts["truncated"] is False
        assert alerts["as_of"]
        # Worst first: the confirmed cancellation, whatever changed most recently.
        assert [s["alert_id"] for s in alerts["subjects"]] == ["alert-2", "alert-1"]
        first = alerts["subjects"][0]
        assert first["phase"] == "confirmed"
        assert first["revision"] == 7
        assert first["delivered_revision"] == 7
        assert first["operator"] == "VYG"
        assert first["line"] == "VYG:Line:L1"
        assert first["journey_ref"] == "VYG:DatedServiceJourney:1234"
        assert first["call_date"] == "2026-09-08"
        assert first["stop_place"] == FROM
        assert first["stop_order"] == 3
        assert alerts["subjects"][1]["delivered_revision"] is None

        listed = client.get("/notification-api/v1/subscriptions", headers=_headers(bearer)).json()
        assert listed["subscriptions"][0]["alerts"]["n_active"] == 2
    finally:
        _uninstall()


def test_the_list_page_reads_every_rows_alert_state_in_one_query():
    """One query per REQUEST, not one per row: a page can hold hundreds of watches."""
    _install()
    bearer = _provision()
    try:
        client = _client()
        for index in range(4):
            client.post(
                "/notification-api/v1/subscriptions",
                json=_body(subscription_id=f"ref-{index}"),
                headers=_headers(bearer),
            )
            _put_subject(
                f"{PARTNER}~ref-{index}", f"alert-{index}", partner_store.PHASE_PREDICTED
            )
        before = _fake.reads
        page = client.get("/notification-api/v1/subscriptions", headers=_headers(bearer)).json()
        # Two queries: the subscriptions themselves and their alert state. Not one per row.
        assert _fake.reads - before == 2, _fake.reads - before
        assert all(row["alerts"]["n_active"] == 1 for row in page["subscriptions"])
    finally:
        _uninstall()


def test_a_quiet_watch_says_so_and_a_bounded_read_does_not():
    """The two must not render the same. R6's rule applied to a second question.

    `n_active: 0` with `truncated: false` is a CLAIM -- no standing alert. The same zero with
    `truncated: true` is the absence of a claim, and a client is told to read it as "we do not
    know". On a list page a bounded read marks every row, including the ones whose own subjects
    fitted, because a watch crowded out of the page would otherwise be indistinguishable from calm.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer))
        quiet = client.get(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(bearer)
        ).json()["alerts"]
        assert quiet == {
            **quiet,
            "n_active": 0,
            "n_confirmed": 0,
            "truncated": False,
            "subjects": [],
        }

        # A failed alert-state read never fails the request and never reads as calm.
        def _boom(*_a: Any, **_k: Any):
            raise RuntimeError("firestore unavailable")

        original = partner_store.subscription_alerts
        partner_store.subscription_alerts = _boom  # type: ignore[assignment]
        try:
            degraded = client.get(
                "/notification-api/v1/subscriptions/commuter-42-morning",
                headers=_headers(bearer),
            )
            assert degraded.status_code == 200, "the subscription itself was read fine"
            assert degraded.json()["alerts"]["truncated"] is True
            assert degraded.json()["alerts"]["n_active"] == 0
        finally:
            partner_store.subscription_alerts = original  # type: ignore[assignment]

        original_many = partner_store.partner_alerts
        partner_store.partner_alerts = _boom  # type: ignore[assignment]
        try:
            page = client.get("/notification-api/v1/subscriptions", headers=_headers(bearer))
            assert page.status_code == 200
            assert all(r["alerts"]["truncated"] is True for r in page.json()["subscriptions"])
        finally:
            partner_store.partner_alerts = original_many  # type: ignore[assignment]
    finally:
        _uninstall()


def test_the_served_list_of_subjects_is_capped_and_the_counts_are_not():
    """AE3 is why: one line suspension put thirteen cancelled departures in one window.

    The cap is the daily notification allowance, so a fifth subject is not something anybody can be
    told about today -- and the counts carry the rest, so nothing is hidden, only unenumerated.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer))
        document_id = f"{PARTNER}~commuter-42-morning"
        for index in range(13):
            _put_subject(
                document_id,
                f"alert-{index:02d}",
                partner_store.PHASE_CONFIRMED,
                updated_at=NOW - timedelta(minutes=index),
            )
        alerts = client.get(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(bearer)
        ).json()["alerts"]
        assert alerts["n_active"] == 13
        assert alerts["n_confirmed"] == 13
        assert len(alerts["subjects"]) == partner_store.MAX_ALERTS_SERVED
        assert partner_store.MAX_ALERTS_SERVED == partner_store.MAX_ALERT_DELIVERIES_PER_DAY
        assert alerts["truncated"] is False, "capped for display is not a bounded read"
    finally:
        _uninstall()


def test_a_withdrawn_alert_is_not_served_as_standing():
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer))
        document_id = f"{PARTNER}~commuter-42-morning"
        _put_subject(document_id, "gone", partner_store.PHASE_WITHDRAWN)
        alerts = client.get(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(bearer)
        ).json()["alerts"]
        assert alerts["n_active"] == 0 and alerts["subjects"] == []
        assert alerts["truncated"] is False
    finally:
        _uninstall()


def test_a_create_response_carries_no_alert_state_and_says_so_with_null():
    """`null` means "not included in this response", never "no standing alerts" -- which is why the
    quiet case is `n_active: 0` with `truncated: false` and not an absent field."""
    _install()
    bearer = _provision()
    try:
        created = _client().post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        ).json()
        assert created["alerts"] is None
    finally:
        _uninstall()


def test_a_watch_cannot_be_shown_anothers_alert_state():
    """The subject carries its own partner_id, and a mismatch is Forbidden in the store -- served
    here as the same 404 every other tenancy check collapses to."""
    _install()
    a_bearer = _provision(PARTNER, DEST)
    _provision(OTHER, OTHER_DEST)
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(a_bearer))
        document_id = f"{PARTNER}~commuter-42-morning"
        _put_subject(document_id, "foreign", partner_store.PHASE_CONFIRMED, partner_id=OTHER)
        response = client.get(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(a_bearer)
        )
        # The subscription is served; its alert state degrades to unknown rather than leaking.
        assert response.status_code == 200
        assert response.json()["alerts"]["truncated"] is True
        assert OTHER not in response.text
    finally:
        _uninstall()



def _run_all() -> None:
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} partner API checks passed.")


if __name__ == "__main__":
    _run_all()
