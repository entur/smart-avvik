"""Checks for notification_api.py: the authenticated client surface at /notification-api/v1.

Hermetic -- a ``TestClient`` over a FastAPI app carrying the real router, no emulator, no socket, no
credentials -- and deliberately backed by the **real** ``notification_store`` running against an
in-memory fake client rather than by a hand-written fake store. That choice is the point: the
interesting behaviour on this surface is the mapping of the store's own verdicts onto status codes,
so a fake store would be a second statement of the rules being checked, and every one of them
(a tombstone is not resurrected, a version mismatch is a conflict, the test marker is immutable, the
active-subscription ceiling is refused before any write) would then be true by construction of the
fixture. The credential plane is the real ``notification_auth`` over its four-method document seam, so
the two-tier limiter, the negative cache and the 401/403/503 split are exercised end to end through
HTTP.

The one thing genuinely stubbed is the outbound call: ``webhook_sender.deliver`` opens a socket and
refuses to do so from a test process at all, so ``/test-events`` gets an injected transport. The
signing is real -- the synthetic event is signed by ``webhook_signing.sign`` over the exact bytes
the route built, and one check verifies it the way a client's receiver would.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import alert_policy
import notification_api
import notification_auth
import notification_pull
import notification_store
import sso
import webhook_sender
import webhook_signing
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
API_SOURCE = (HERE / "notification_api.py").read_text()
VARSEL_SOURCE = (HERE / "varsel.py").read_text()
CONTRACTS = REPO / "contracts" / "notifications" / "v1"
OPENAPI = json.loads((CONTRACTS / "openapi.json").read_text())
SUBSCRIPTION_SCHEMA = json.loads((CONTRACTS / "subscription.schema.json").read_text())
EVENT_SCHEMA = json.loads((CONTRACTS / "event.schema.json").read_text())

NOW = datetime(2026, 9, 8, 6, 30, tzinfo=UTC)
CLIENT = "acme"
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

    Needed for the negative greps below, and the reason is `test_notification_store._uncommented`'s in
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
    """One alert-subject document, in the shape `notification_store._mint` writes.

    Written straight into the fake rather than by running an emit: the emit path is U6's and needs
    a whole cycle's inputs, while what this suite is checking is that the API SERVES what the pass
    has already committed. `test_notification_store` pins the writer against the reader on its side.
    """
    doc: dict[str, Any] = {
        "alert_id": alert_id,
        "subscription_id": document_id,
        "client_id": CLIENT,
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
        notification_store.TTL_FIELD: NOW + timedelta(days=notification_store.HISTORY_DAYS),
    }
    doc.update(over)
    _fake.put(notification_store.SUBJECTS, alert_id, doc)
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
        "external_ref": "commuter-42",
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

    def get(self, transaction: Any = None, timeout: float | None = None) -> _Snap:
        # `timeout` is accepted and ignored: every read in `notification_store` now carries its own
        # Firestore deadline (KTD6 -- an abandoned worker thread with no call deadline delays
        # pod termination via `concurrent.futures`' atexit join). `test_notification_store` is where
        # the deadline is asserted; this double only has to tolerate the kwarg.
        del transaction, timeout
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

    def stream(self, timeout: float | None = None) -> list[_Snap]:
        del timeout
        # Counted per DOCUMENT, not per query: `reads` above answers "how many queries", and the
        # cost a paginated endpoint is judged on is how many documents each of them streams. A
        # `?limit=1` page that reads five hundred rows makes exactly two queries.
        self.client.documents_read += len(self.rows)
        return [_Snap(path, data, self.client) for path, data in self.rows]

    def count(self, alias: str) -> _Aggregation:
        assert alias == "n"
        return _Aggregation(self)


class _AggregationValue:
    def __init__(self, value: int) -> None:
        self.value = value


class _Aggregation:
    """The create-time ceiling count (U7), which is an aggregation rather than a page of rows.

    It bills ONE document read however many index entries it scans, which is why the count moved
    off the rows -- so it adds one to `documents_read`, not `len(rows)`. An endpoint's cost is what
    this suite judges, and charging the aggregation per row would report the old price for the new
    query.
    """

    def __init__(self, page: _Page) -> None:
        self._page = page

    def get(self, transaction: Any = None, timeout: float | None = None) -> list[list[Any]]:
        del transaction, timeout
        self._page.client.documents_read += 1
        return [[_AggregationValue(len(self._page.rows))]]


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

    def get(self, query: Any, timeout: float | None = None) -> list[_Snap]:
        del timeout
        return list(query.stream())


class _FakeFirestore:
    """Enough Firestore for the real ``notification_store`` bodies, plus an ordered write log.

    ``log`` is what makes "the tombstone lands before anything is deleted" checkable rather than
    asserted: every transactional write and every batch delete appends to it in order, so R13's
    ordering is read off the sequence instead of inferred from the end state -- which is identical
    whichever order the two halves ran in.
    """

    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}
        self.log: list[str] = []
        self.reads = 0
        self.documents_read = 0
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
    """``notification_auth._Docs`` over a dict, counting reads."""

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


def _install(*, pepper: str | None = PEPPER) -> _FakeFirestore:
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
            "client": notification_store._firestore_client,
            "run": notification_store._run_transaction,
            "query": notification_store._query,
        }
    )
    notification_store._firestore_client = lambda: client  # type: ignore[assignment]

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

    def _query(
        name: str,
        *,
        equality: Any = None,
        inequality: Any = None,
        limit: int,
        start_after: Any = None,
    ) -> _Page:
        if client.fail_with is not None:
            raise client.fail_with
        client.reads += 1
        spec = notification_store._SPECS_BY_NAME[name]
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
        if start_after is not None:
            # Positional, as Firestore's own cursor is: everything strictly after the snapshot in
            # THIS ordering. Matched on the document path, which is what `__name__` breaks a tie on
            # when two rows share their ordering field.
            paths = [path for path, _ in rows]
            if start_after._path in paths:
                rows = rows[paths.index(start_after._path) + 1 :]
        return _Page(client, rows[:limit])

    notification_store._run_transaction = _run  # type: ignore[assignment]
    notification_store._query = _query  # type: ignore[assignment]
    notification_store.configure(client=client, clock=lambda: NOW)

    if pepper is None:
        os.environ.pop(notification_auth.PEPPER_ENV, None)
    else:
        os.environ[notification_auth.PEPPER_ENV] = pepper
    os.environ[webhook_signing.SIGNING_ROOT_ENV] = SIGNING_ROOT
    notification_auth.configure(docs=_auth_docs, clock=lambda: NOW)

    def _deliver(url: str, *, body: bytes, headers: Any, **_kw: Any) -> Any:
        _deliveries.append({"url": url, "body": body, "headers": dict(headers)})
        return webhook_sender.DeliveryAttempt(**_delivery_result)

    notification_api.configure(
        store=notification_store,
        auth=notification_auth,
        canonicalize=lambda ref: FROM if ref == COLLAPSED else ref,
        deliver=_deliver,
    )
    return client


def _uninstall() -> None:
    global _fake, _auth_docs
    if _saved:
        notification_store._firestore_client = _saved["client"]  # type: ignore[assignment]
        notification_store._run_transaction = _saved["run"]  # type: ignore[assignment]
        notification_store._query = _saved["query"]  # type: ignore[assignment]
        _saved.clear()
    notification_store.reset_for_tests()
    notification_auth.reset_for_tests()
    notification_api.configure()
    os.environ.pop(notification_auth.PEPPER_ENV, None)
    os.environ.pop(webhook_signing.SIGNING_ROOT_ENV, None)
    _fake = None
    _auth_docs = None


def _provision(client: str = CLIENT, destination: str = DEST) -> str:
    notification_auth.create_client(client, name=client.title(), actor="op@entur.org")
    notification_auth.create_destination(
        destination, client_id=client, callback_url=GOOD_URL, actor="op@entur.org"
    )
    _record, bearer = notification_auth.issue_credential(client, actor="op@entur.org")
    return bearer


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(notification_api.router)
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
        notification_auth.revoke_credential(bearer.split(".")[0], actor="op@entur.org")
        assert client.get("/notification-api/v1/subscriptions", headers=_headers(bearer)).status_code == 401
    finally:
        _uninstall()


def test_a_suspended_client_gets_403_so_it_can_tell_the_two_apart():
    _install()
    bearer = _provision()
    try:
        notification_auth.set_client_suspended(CLIENT, suspended=True, actor="op@entur.org")
        response = _client().get("/notification-api/v1/subscriptions", headers=_headers(bearer))
        assert response.status_code == 403
        assert response.json() == {"detail": "client_suspended"}
    finally:
        _uninstall()


def test_one_client_cannot_reach_anothers_records_and_the_body_names_nothing():
    """AE11's second half. Ownership comes from the credential, so the reference is namespaced.

    Three probes, one answer. Client A asking for B's own reference gets A's namespace and finds
    nothing; A asking with B's namespaced id gets it treated as a reference (the prefix stripped is
    only ever A's own) and finds nothing; and a record that somehow carries another client_id
    raises Forbidden inside the store and is collapsed to 404 here.
    """
    _install()
    a_bearer = _provision(CLIENT, DEST)
    b_bearer = _provision(OTHER, OTHER_DEST)
    try:
        client = _client()
        created = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="shared-ref", destination_id=OTHER_DEST),
            headers=_headers(b_bearer),
        )
        assert created.status_code == 201, created.text
        # Both clients may use the same reference; they are different records.
        mine = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="shared-ref"),
            headers=_headers(a_bearer),
        )
        assert mine.status_code == 201
        assert mine.json()["client_id"] == CLIENT
        assert mine.json()["subscription_id"] == "shared-ref"

        for probe in (f"{OTHER}~shared-ref", "unknown-ref"):
            response = client.get(
                f"/notification-api/v1/subscriptions/{probe}", headers=_headers(a_bearer)
            )
            assert response.status_code == 404, probe
            assert response.json() == {"detail": "not_found"}
            assert OTHER not in response.text

        # A stored record carrying a foreign client_id: Forbidden inside the store, 404 here.
        _fake.docs[f"{notification_store.SUBSCRIPTIONS}/{CLIENT}~squatted"] = {
            **notification_store._new_subscription_doc(
                notification_store.SubscriptionSpec(
                    subscription_id=f"{CLIENT}~squatted",
                    client_id=OTHER,
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


def test_a_client_id_in_the_body_is_rejected_rather_than_honoured():
    """Ownership is a property of the credential, never a request-shaped choice."""
    _install()
    bearer = _provision()
    try:
        response = _client().post(
            "/notification-api/v1/subscriptions",
            json={**_body(), "client_id": OTHER},
            headers=_headers(bearer),
        )
        assert response.status_code == 422
    finally:
        _uninstall()


def test_an_unknown_or_foreign_destination_is_one_answer():
    """The same collapse as a foreign record: a client must not learn another's destination ids."""
    _install()
    a_bearer = _provision(CLIENT, DEST)
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


def test_a_create_asks_nothing_and_answers_that_it_has_not_established_servability():
    """AE13's first half, and the whole of U10's step 3.

    A create and a replace used to ask /varsel's 20-second BigQuery question in a second
    transaction after the write. On a channel whose purpose is onboarding an existing commuter base
    that put a BigQuery job on the registration path: a client may issue
    `notification_auth.PER_CLIENT_PER_MIN` (120) writes a minute, every one drawing on a shared
    per-minute ceiling, and the degradation past it was silent -- `None` reads as "could not
    establish", so the caveat simply did not appear.

    `servable: null` is what the contract already defines as "not established", and it must never
    render as an all-clear. `notification_servability.recheck` answers it from one job per cycle.

    Never refused either way: weekend-only and seasonal routes are why refusing would be wrong, and
    an unregisterable journey is worse than a registered one carrying a caveat.
    """
    _install()
    bearer = _provision()
    try:
        response = _client().post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["servable"] is None
        assert payload["servable_checked_at"] is None
    finally:
        _uninstall()


def test_a_replace_also_answers_null_rather_than_carrying_the_old_verdict_forward():
    """R6: a stale answer must never read as an all-clear, so an amendment retires it.

    The store already retires both fields on a replace; what changed in U10 is that nothing
    re-establishes them on the request path, so the response says "not established" until the
    sweep's next cycle rather than either stalling on BigQuery or carrying a verdict about the
    route the client has just stopped watching.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        first = client.post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        ).json()
        assert first["servable"] is None
        second = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(to_stop="NSR:StopPlace:591"),
            headers=_headers(bearer),
        ).json()
        assert second["servable"] is None
        assert second["servable_checked_at"] is None
        assert second["version"] == 2
    finally:
        _uninstall()


def test_no_write_path_reaches_a_connectivity_seam_at_all():
    """The falsification of the two above, and it is a property of the MODULE rather than of one
    route: a seam re-introduced for some third write path would put a BigQuery job back on the
    registration path with nothing here to notice.
    """
    source = pathlib.Path(notification_api.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    used = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    }
    used |= {node.arg for node in ast.walk(tree) if isinstance(node, ast.keyword) and node.arg}
    reintroduced = used & {"_connectivity", "connectivity", "_record_servability"}
    assert not reintroduced, reintroduced


# ---- Scenario 3: retry, conflict, tombstone ----------------------------------------------------


def test_an_identical_retry_is_stable():
    """R5's safe retry: same reference, same definition, same record and no new version.

    The store's create path performs no write at all for this, so a retry cannot bump a version,
    reset a horizon or clear a spent cap slot. What the route adds is the inactivity touch -- a
    client re-POSTing its watch IS the "still interested" signal R5's horizon is measured from.
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
        assert len(_fake.in_collection(notification_store.SUBSCRIPTIONS)) == 1
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
        document_id = f"{CLIENT}~commuter-42-morning"
        for index in range(3):
            _fake.put(
                notification_store.EVENTS,
                f"e{index}",
                {"event_id": f"e{index}", "subscription_id": document_id},
            )
            _fake.put(
                notification_store.DELIVERIES,
                f"d{index}",
                {"delivery_id": f"d{index}", "subscription_id": document_id},
            )
        _fake.put(
            notification_store.SUBJECTS, "s0", {"alert_id": "s0", "subscription_id": document_id}
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
        assert payload["cleanup"]["residual_hours"] == notification_store.TTL_RESIDUAL_HOURS

        tombstone_at = next(
            i for i, entry in enumerate(_fake.log) if entry.endswith(f"/{document_id}")
        )
        deletes = [i for i, entry in enumerate(_fake.log) if entry.startswith("delete:")]
        assert deletes, "the cleanup must actually have deleted something"
        assert tombstone_at < min(deletes), _fake.log

        assert _fake.in_collection(notification_store.EVENTS) == []
        assert _fake.in_collection(notification_store.DELIVERIES) == []
        assert _fake.in_collection(notification_store.SUBJECTS) == []
        # The tombstone's route is redacted the moment cleanup completes.
        tombstone = _fake.doc(notification_store.SUBSCRIPTIONS, document_id)
        assert tombstone["status"] == "deleted"
        assert tombstone["from_stop"] == "" and tombstone["external_ref"] == ""
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
    """Scenario 4. An empty window, a bad weekday and from == to, refused as /varsel refuses them --
    and a window over midnight accepted as /varsel accepts it (KIX-22).

    The point is not that a 422 comes back -- it is that the SAME sentence does. R2's shape lives in
    `notification_store.SubscriptionSpec.validate`, which restates /varsel's three rules and is pinned
    against them by that module's own suite; this route reaches it rather than adding a fourth copy,
    and the strings below are checked against varsel.py so a divergence fails here too.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        cases = [
            (_body(days=[0, 9]), "days must be a non-empty subset of 0..6"),
            (_body(days=[]), "days must be a non-empty subset of 0..6"),
            (_body(begin="08:00", end="08:00"), "begin and end must differ"),
            (_body(to_stop=FROM), "from_stop and to_stop must differ"),
        ]
        for payload, fragment in cases:
            response = client.post(
                "/notification-api/v1/subscriptions", json=payload, headers=_headers(bearer)
            )
            assert response.status_code == 422, (payload, response.text)
            assert fragment in response.json()["detail"], (payload, response.text)
        overnight = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(begin="22:00", end="06:00"),
            headers=_headers(bearer),
        )
        assert overnight.status_code in (200, 201), overnight.text
        assert (overnight.json()["begin"], overnight.json()["end"]) == ("22:00", "06:00")
        # The verdicts are /varsel's, not this surface's invention.
        assert "begin and end must differ" in VARSEL_SOURCE
        assert "days must be a non-empty subset of 0..6 (0=Monday)" in VARSEL_SOURCE
        assert "from_stop and to_stop must differ" in VARSEL_SOURCE
    finally:
        _uninstall()


def test_the_router_does_not_restate_the_window_rule():
    """The fourth copy is the failure mode, so its absence is checked rather than trusted.

    `notification_store.SubscriptionSpec.validate` is statement three (JourneyIn and
    `varsel._parse_webhook_recipient` are one and two). A comparison of begin and end anywhere in
    this module would be a fourth, and the two surfaces would then drift silently.
    """
    # `spec.begin == sub.begin` is the idempotency comparison, not a window rule, so the equality
    # shapes named here are the ones that compare a definition's begin with its OWN end.
    for forbidden in ("begin >= ", "begin >=", "begin == end", ".begin == body.end", "_parse_hhmm", "_HHMM_RE", "range(0, 7)"):
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
        assert _fake.doc(notification_store.SUBSCRIPTIONS, f"{CLIENT}~live")["test"] is False
    finally:
        _uninstall()


def test_a_test_event_is_signed_over_the_exact_bytes_and_stores_nothing():
    """R15, and the single-writer claim the whole store rests on.

    Nothing is written: no event, no delivery, no alert subject. That is not tidiness --
    `notification_store`'s pending-event ceiling is exact rather than best-effort BECAUSE the emit path
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

        assert _fake.in_collection(notification_store.EVENTS) == []
        assert _fake.in_collection(notification_store.DELIVERIES) == []
        assert _fake.in_collection(notification_store.SUBJECTS) == []
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
    conform teaches a client the wrong shape at exactly the moment they are learning it."""
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
        notification_auth.pause_destination(DEST, reason="410 Gone")
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


def test_a_test_event_reports_a_clients_own_failure_as_the_diagnosis():
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
            {"ok": False, "status": 500, "retryable": True, "error": "client returned 500"}
        )
        response = client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": "probe"},
            headers=_headers(bearer),
        )
        assert response.status_code == 200, "the request succeeded; the delivery did not"
        assert response.json()["delivered"] is False
        assert response.json()["status"] == 500
        assert response.json()["error"] == "client returned 500"
    finally:
        _uninstall()


def test_a_test_event_delivery_that_never_returns_gives_the_worker_back():
    """DNS is not bounded by a socket timeout, and this is the one route that dials on request.

    `webhook_sender`'s ceilings are `socket.settimeout` values on an already-open connection, and
    `_default_resolver` calls `socket.getaddrinfo` BEFORE any of them apply -- a blocking C call
    with no deadline the Python layer can express and no way to interrupt it. Every other caller of
    `deliver` is the poller's delivery pass, which contains that inside its own bounded wait; this
    one runs on a plain `def` route sharing the ~40-slot anyio worker pool with `/varsel-api` and
    `/impact`. So a destination whose resolver stalls held one of those workers indefinitely, and
    `_TEST_EVENTS_PER_MIN` (6) per client is a ceiling on REQUESTS, not on workers held.

    Bounded by running the whole delivery -- resolution included -- on a one-wide pool that is
    ABANDONED on timeout: `notification_events.emit`'s shape, with the same honest residual, and here the
    per-client ceiling is also what bounds how many threads can be leaked.

    Asserted with a real clock and a real thread, like `test_webhook_delivery`'s two timeout tests:
    the property is that the request thread comes back, and a mocked clock cannot fail that way.
    """
    # The real wait must EXCEED one whole attempt, or this backstop would fire on an ordinary slow
    # client and report `timeout` where their own status is the diagnosis. Asserted here and then
    # shortened, so the property is checked without the suite paying eight seconds for it.
    assert (
        notification_api._TEST_EVENT_WAIT_S
        > webhook_sender.DEFAULT_TIMEOUT_S + webhook_sender.CONNECT_TIMEOUT_S
    )
    _install()
    bearer = _provision()
    release = threading.Event()
    saved_wait = notification_api._TEST_EVENT_WAIT_S
    try:
        client = _client()
        client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="probe", test=True),
            headers=_headers(bearer),
        )

        def hang(url: str, *, body: bytes, headers: Any, **_kw: Any) -> Any:
            release.wait(30)  # bounded, so a failing assertion cannot wedge the suite
            return webhook_sender.DeliveryAttempt(ok=True, status=200)

        notification_api.configure(deliver=hang)
        notification_api._TEST_EVENT_WAIT_S = 0.15  # type: ignore[assignment]
        started = time.monotonic()
        response = client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": "probe"},
            headers=_headers(bearer),
        )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"a stalled destination must not hold a worker; took {elapsed:.1f}s"
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["delivered"] is False
        assert payload["status"] is None
        assert payload["error"] == "timeout"
    finally:
        notification_api._TEST_EVENT_WAIT_S = saved_wait  # type: ignore[assignment]
        release.set()
        _uninstall()


def test_the_test_event_route_is_rate_limited_on_its_own():
    """The one route that makes an outbound call, so it carries its own tight ceiling: this app must
    not become a request amplifier against a client-supplied host."""
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
            for _ in range(notification_api._TEST_EVENTS_PER_MIN + 2)
        ]
        assert statuses.count(200) == notification_api._TEST_EVENTS_PER_MIN
        assert statuses[-1] == 429
        assert len(_deliveries) == notification_api._TEST_EVENTS_PER_MIN
    finally:
        _uninstall()


def test_signing_that_is_not_configured_is_503_rather_than_an_unsigned_request():
    """Fail closed and fail visible: a client endpoint that accepts an unsigned request is a hole
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
    """Scenario 6 through the surface a client actually cuts over on."""
    _install()
    retiring = _provision()
    try:
        client = _client()
        _record, replacement = notification_auth.issue_credential(CLIENT, actor="op@entur.org")
        for bearer in (retiring, replacement):
            assert client.get(
                "/notification-api/v1/subscriptions", headers=_headers(bearer)
            ).status_code == 200
        notification_auth.revoke_credential(retiring.split(".")[0], actor="op@entur.org")
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
        principal = notification_auth.authenticate(f"Bearer {bearer}")
        for _ in range(notification_auth.PER_CLIENT_PER_MIN):
            notification_auth.claim_client_slot(principal.client_id)
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


# ---- Scenario 8 is notification_auth's; scenario 9: the SSO gate -----------------------------------


def test_the_notification_api_prefix_is_deliberately_not_behind_the_browser_sso_gate():
    """Scenario 9. That gate answers an unconfigured deployment with 503 and an unauthenticated
    BROWSER with a 302 -- and a machine caller following a redirect into an Entra login screen gets
    an HTML page where it expected JSON. The credential dependency is the gate here."""
    assert len(sso.PROTECTED_PREFIXES) == 8
    assert not any(p.startswith("/notification") for p in sso.PROTECTED_PREFIXES)
    assert sso._is_protected(notification_api.PREFIX) is False
    assert sso._is_protected("/notification-api/v1/subscriptions") is False


# ---- Scenario 10: a store outage leaves nothing behind ----------------------------------------


def test_a_store_outage_leaves_no_partial_subscription_and_no_event():
    """Scenario 10's first half. R20 at the API edge: 503, and nothing written.

    503 rather than 500 because it is retryable, and the traceback stays in the log rather than in
    the response -- a client debugging their own integration must not be reading our stack.
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
        assert _fake.in_collection(notification_store.SUBSCRIPTIONS) == []
        assert _fake.in_collection(notification_store.EVENTS) == []
        assert _fake.in_collection(notification_store.DELIVERIES) == []
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
        for index in range(notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT):
            _fake.put(
                notification_store.SUBSCRIPTIONS,
                f"{CLIENT}~filler-{index}",
                notification_store._new_subscription_doc(
                    notification_store.SubscriptionSpec(
                        subscription_id=f"{CLIENT}~filler-{index}",
                        client_id=CLIENT,
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
        assert int(response.headers["retry-after"]) == notification_store.RETRY_AFTER_CAP_S
        assert len(_fake.docs) == before, "refused before any write"
        # R21's "a breach is visible to an operator". The ceiling is enforced inside a transaction
        # body, which logs nothing by design (a body may run more than once), so the route is the
        # only place it can be recorded once per request.
        records: list[Any] = []
        handler = logging.Handler()
        handler.emit = records.append  # type: ignore[method-assign]
        notification_api.logger.addHandler(handler)
        try:
            client.post(
                "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
            )
        finally:
            notification_api.logger.removeHandler(handler)
        breaches = [
            r
            for r in records
            if getattr(r, "fields", {}).get("event") == "notification_quota"
            and getattr(r, "fields", {}).get("result") == "subscriptions"
        ]
        assert len(breaches) == 1, records
        assert breaches[0].fields["client_id"] == CLIENT
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
            f"/notification-api/v1/subscriptions?limit={notification_api._LIST_MAX + 1}",
            headers=_headers(bearer),
        ).status_code == 422
    finally:
        _uninstall()


def test_a_small_page_reads_a_small_page():
    """The caller's `limit` reaches the QUERY, and did not until 2026-09-09.

    `list_subscriptions` streamed up to `MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT` (500) subscription
    documents and sliced in Python, so `?limit=1` cost the same as `?limit=500`. Against
    `notification_auth.PER_CLIENT_PER_MIN` (120) that is a client near the ceiling polling small pages
    at up to 120 x 500 subscription reads a minute from one endpoint on one credential -- a read
    amplification driven by their polling shape rather than by what they asked for.

    Asserted as `limit + 1` and not "small": the extra one is what detects `next_cursor` without a
    second query, and asserting the exact number is what makes the fix visible if the cursor is ever
    dropped. The ALERT-state read stays client-scoped and is excluded here on purpose -- one query
    for every row's alert state is `test_the_list_page_reads_every_rows_alert_state_in_one_query`'s
    deliberate trade, and narrowing it per page would trade it back for N+1 queries.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        for index in range(8):
            client.post(
                "/notification-api/v1/subscriptions",
                json=_body(subscription_id=f"ref-{index}"),
                headers=_headers(bearer),
            )
        assert _fake is not None
        # A page with a cursor: one document for the cursor, then `limit + 1` rows.
        first = client.get(
            "/notification-api/v1/subscriptions?limit=2", headers=_headers(bearer)
        ).json()
        _fake.documents_read = 0
        second = client.get(
            f"/notification-api/v1/subscriptions?limit=2&after={first['next_cursor']}",
            headers=_headers(bearer),
        ).json()
        # 1 cursor resolution + 3 subscription rows + the client's 0 alert subjects.
        assert _fake.documents_read <= 4, _fake.documents_read
        assert [s["subscription_id"] for s in second["subscriptions"]] == ["ref-2", "ref-3"]
        assert second["next_cursor"] == "ref-3"
    finally:
        _uninstall()


def test_a_page_of_expired_rows_does_not_end_the_listing_early():
    """The cost of testing expiry in Python, and why the paged read fills rather than truncates.

    `subscription_is_live` cannot be a query -- it tests BOTH of R5's horizons plus the tombstone --
    and Firestore deletes an expired document only "typically within 24 hours", which the store's
    own comments call an ordinary condition rather than a fault. So a page of `limit` documents can
    come back with fewer LIVE rows, and serving those alone would tell the client the listing had
    ended while live watches remained behind them: the client stops, and a watch it owns is simply
    invisible until something else re-pages.

    Two dead rows at the FRONT, because ordering is by `created_at` and that is where an aged-out
    watch actually sits.
    """
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
        assert _fake is not None
        for index in (0, 1):
            _fake.docs[f"{notification_store.SUBSCRIPTIONS}/{CLIENT}~ref-{index}"][
                "inactivity_expires_at"
            ] = NOW - timedelta(days=1)
        page = client.get(
            "/notification-api/v1/subscriptions?limit=2", headers=_headers(bearer)
        ).json()
        assert [s["subscription_id"] for s in page["subscriptions"]] == ["ref-2", "ref-3"], page
        assert page["next_cursor"] is None, "four rows, two dead, one page: there is no next"
    finally:
        _uninstall()


def test_a_raised_quota_can_page_past_the_default_ceiling():
    """The list bound is the CLIENT's ceiling, not the module constant.

    `list_subscriptions` used to clamp both its page and its scan at
    `MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT` (500) and the route clamped `limit + 1` to the same number,
    so a client whose quota had been raised past 500 got a full first page with no `next_cursor`
    -- the page equalled the read -- and could never list its remaining watches. The second half is
    the same clamp on the scan: 500 expired rows ahead of the live ones ended the scan on an empty
    page, which reads as "you hold nothing" to a client that holds a watch.
    """
    _install()
    bearer = _provision()
    try:
        notification_auth.set_client_quotas(CLIENT, actor="op@entur.org", max_active_subscriptions=600)
        assert _fake is not None
        n = notification_store.MAX_ACTIVE_SUBSCRIPTIONS_PER_CLIENT + 1
        for index in range(n):
            _fake.put(
                notification_store.SUBSCRIPTIONS,
                f"{CLIENT}~bulk-{index:04d}",
                notification_store._new_subscription_doc(
                    notification_store.SubscriptionSpec(
                        subscription_id=f"{CLIENT}~bulk-{index:04d}",
                        client_id=CLIENT,
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
        client = _client()
        first = client.get(
            f"/notification-api/v1/subscriptions?limit={notification_api._LIST_MAX}",
            headers=_headers(bearer),
        ).json()
        assert first["count"] == notification_api._LIST_MAX
        assert first["next_cursor"] is not None, "501 live rows and a page of 500: there IS a next"
        rest = client.get(
            f"/notification-api/v1/subscriptions?limit={notification_api._LIST_MAX}"
            f"&after={first['next_cursor']}",
            headers=_headers(bearer),
        ).json()
        assert rest["count"] == 1 and rest["next_cursor"] is None
        # And the scan bound: every row but the last expired, the last one still listed.
        for index in range(n - 1):
            _fake.docs[f"{notification_store.SUBSCRIPTIONS}/{CLIENT}~bulk-{index:04d}"][
                "inactivity_expires_at"
            ] = NOW - timedelta(days=1)
        page = client.get(
            "/notification-api/v1/subscriptions?limit=2", headers=_headers(bearer)
        ).json()
        assert [s["subscription_id"] for s in page["subscriptions"]] == [f"bulk-{n - 1:04d}"], page
        assert page["next_cursor"] is None
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

        notification_auth.rotate_signing_generation(DEST, actor="op@entur.org")
        rotated = client.get(
            "/notification-api/v1/destinations/current", headers=_headers(bearer)
        ).json()
        assert rotated["signing_generations"] == {"active": 2, "retiring": 1}
        assert webhook_signing.SECRET_PREFIX not in json.dumps(rotated)

        notification_auth.pause_destination(DEST, reason="410 Gone")
        paused = client.get(
            "/notification-api/v1/destinations/current", headers=_headers(bearer)
        ).json()
        assert paused["paused"] is True
        assert paused["ready"] is False
        assert paused["paused_reason"] == "410 Gone"
    finally:
        _uninstall()


def test_a_client_with_no_destination_gets_404_rather_than_an_empty_record():
    _install()
    try:
        notification_auth.create_client(CLIENT, name="Acme", actor="op@entur.org")
        _record, bearer = notification_auth.issue_credential(CLIENT, actor="op@entur.org")
        response = _client().get(
            "/notification-api/v1/destinations/current", headers=_headers(bearer)
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "no_destination"}
    finally:
        _uninstall()


def test_the_committed_test_event_sample_still_matches_the_producer():
    """The sample a client's fixture reads must be what `_synthetic_event` actually emits.

    `contracts/notifications/v1/test-event-sample.json` exists because the vendored TypeScript
    fixture was a HAND transcription of this producer and drifted from it in eight fields -- while
    its own docstring claimed to be a copy "field for field" and its end-to-end test passed green
    against a shape no producer sends. The sample removes the transcription; this check removes the
    rot, by rebuilding the event from the inputs the file itself records and comparing.

    Structure and non-volatile values, not bytes: `event_id`, `alert_id`, `journey_ref`, `revision`
    and the three instants are derived per call and are meant to differ. Everything else is the
    contract, and `payload`'s sub-objects carry no property list in the schema -- so a wrongly named
    key inside them is schema-VALID and wrong, which is exactly why this compares producers rather
    than validating against the schema.
    """
    import dataclasses

    sample = json.loads((CONTRACTS / "test-event-sample.json").read_text())
    recorded = sample["how_to_regenerate"]["inputs"]
    event = sample["event"]

    # Built generically from the recorded fields so a new `Subscription` field does not fail this
    # for a reason that has nothing to do with the event.
    names = {f.name for f in dataclasses.fields(notification_store.Subscription)}
    kwargs = {k: v for k, v in recorded["subscription"].items() if k in names}
    instant = datetime.fromisoformat(recorded["now"])
    far = datetime(2027, 1, 1, tzinfo=UTC)
    for name, value in (
        ("created_at", instant), ("updated_at", instant), ("last_seen_at", instant),
        ("lifetime_expires_at", far), ("inactivity_expires_at", far), ("expires_at", far),
    ):
        if name in names:
            kwargs.setdefault(name, value)
    for field in dataclasses.fields(notification_store.Subscription):
        if (
            field.name not in kwargs
            and field.default is dataclasses.MISSING
            and field.default_factory is dataclasses.MISSING
        ):
            kwargs[field.name] = None

    live = notification_api._synthetic_event(
        notification_store.Subscription(**kwargs), now=instant, nonce=recorded["nonce"]
    )

    def paths(node, prefix=""):
        out = {}
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{prefix}.{key}" if prefix else key
                out[here] = type(value).__name__
                out.update(paths(value, here))
        return out

    in_sample, in_live = paths(event), paths(live)
    assert not set(in_sample) - set(in_live), sorted(set(in_sample) - set(in_live))
    assert not set(in_live) - set(in_sample), sorted(set(in_live) - set(in_sample))
    assert not [k for k in in_sample if in_sample[k] != in_live[k]], "a leaf changed type"

    derived = {"event_id", "alert_id", "journey_ref", "revision", "occurred_at", "valid_until"}
    for key, value in event.items():
        if key in derived or isinstance(value, (dict, list)):
            continue
        assert value == live[key], f"{key}: sample {value!r} != producer {live[key]!r}"
    # The two the drift actually turned on, named so a failure reads without a diff.
    assert event["payload"]["probabilities"] == live["payload"]["probabilities"]
    assert event["payload"]["call"] == live["payload"]["call"]
    # `situations` is a LIST, and `paths()` above deliberately descends into dicts only -- so until
    # 2026-09-10 its items were never compared at all, and they are hand-built on both sides
    # (`_synthetic_event` here, `varsel._webhook_situations` on the live path): exactly the drift
    # class this check exists for. Whole items, because the recorded nonce makes every value
    # reproducible, and the top-level list of numbers beside them.
    assert event["payload"]["situations"] == live["payload"]["situations"]
    assert event["payload"]["situations"], "the sample carries at least one situation"
    assert event["situations"] == live["situations"]


def test_the_test_event_request_schema_accepts_the_id_the_route_accepts():
    """`TestEventIn` widened to the namespaced form; the published schema has to agree.

    It did not: `TestEventRequest.subscription_id` `$ref`ed `subscription.schema.json`'s CREATE-side
    definition, which is correctly strict and is the wrong constraint here -- so the contract still
    forbade the id `GET` hands back and an event carries. Pinning both bounds is what stops the
    widening being applied to one artefact and not the other a second time.
    """
    published = OPENAPI["components"]["schemas"]["TestEventRequest"]["properties"]["subscription_id"]
    assert "$ref" not in published, "the create-side definition is the wrong constraint here"
    field = notification_api.TestEventIn.model_fields["subscription_id"]
    bounds = {
        "maxLength": next(
            (m.max_length for m in field.metadata if getattr(m, "max_length", None) is not None),
            None,
        ),
        "pattern": next(
            (m.pattern for m in field.metadata if getattr(m, "pattern", None) is not None), None
        ),
    }
    assert published["maxLength"] == bounds["maxLength"], (published, bounds)
    assert published["pattern"] == bounds["pattern"], (published, bounds)
    # And the create-side definition stays strict -- widening THAT would be the wrong fix.
    create = SUBSCRIPTION_SCHEMA["properties"]["subscription_id"]
    assert "~" not in create["pattern"], "the create body must keep refusing a namespaced id"


def test_the_response_model_matches_the_published_schema_in_both_directions():
    """`subscription.schema.json` is `additionalProperties: false` and U7 generates types from it,
    so a field added on one side and not the other breaks a client's build silently."""
    served = set(notification_api.SubscriptionOut.model_fields)
    published = set(SUBSCRIPTION_SCHEMA["properties"])
    assert served == published, (served - published, published - served)
    # Every property the published schema REQUIRES is served non-optionally in spirit: the writable
    # ones are all present on the response.
    for key in SUBSCRIPTION_SCHEMA["required"]:
        assert key in served, key
    # And the input model accepts exactly the writable half.
    writable = set(OPENAPI["components"]["schemas"]["SubscriptionCreate"]["properties"])
    assert set(notification_api.SubscriptionIn.model_fields) == writable
    assert "alerts" not in writable, "alert state is read-only; a request may not carry it"

    # The nested alert state, both directions too. It is two levels deep and
    # `additionalProperties: false` at each, so a field added on one side and not the other breaks
    # a client's generated types exactly as a top-level one would -- and more quietly.
    alerts = published_schema = SUBSCRIPTION_SCHEMA["properties"]["alerts"]
    assert set(notification_api.AlertStateOut.model_fields) == set(alerts["properties"])
    assert set(alerts["required"]) == set(alerts["properties"])
    subject = alerts["properties"]["subjects"]["items"]
    assert set(notification_api.AlertSubjectOut.model_fields) == set(subject["properties"])
    assert set(subject["required"]) == set(subject["properties"])
    # The published bound is the LARGEST cap an operator can set, never the default: `_served()`
    # enumerates up to the client's own daily cap, so a bound equal to the default would be
    # violated by the first raised cap -- a client's generated types would then refuse a response
    # this API sent on purpose.
    assert alerts["properties"]["subjects"]["maxItems"] == notification_auth.QUOTA_BOUNDS["daily_cap"][1]
    assert alerts["properties"]["subjects"]["maxItems"] >= notification_store.MAX_ALERTS_SERVED
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
    # The Standard Webhooks headers a receiver must verify are named where a client will look.
    declared = {p["name"] for p in webhook["parameters"]}
    assert declared == {
        webhook_signing.HEADER_ID,
        webhook_signing.HEADER_TIMESTAMP,
        webhook_signing.HEADER_SIGNATURE,
    }
    # Bearer, never a cookie: one global CORS policy allows every origin.
    scheme = OPENAPI["components"]["securitySchemes"]["clientKey"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer"
    # Checked on the DECLARATION rather than by searching the text: the description says the word
    # "cookie" in order to rule it out, so a substring search would fail on the explanation.
    for declared in OPENAPI["components"]["securitySchemes"].values():
        assert declared.get("in") != "cookie", declared
        assert declared["type"] != "apiKey", declared
    assert OPENAPI["security"] == [{"clientKey": []}]


def test_the_openapi_paths_are_exactly_the_routers_routes():
    """A published contract that has drifted from the router is worse than none: a client writes a
    client against it. Checked in both directions, over the UNION of the two routers that share the
    prefix (KTD12, 2026-09-10): the six credentialed routes are the origin plan's own API table, the
    seventh is the public journey-keyed read on `notification_pull.router`. The pull router's own
    pins -- no dependency, exactly that one path, a coroutine -- live in `test_notification_pull`
    and are deliberately not repeated here."""
    routes = {
        (route.path[len(notification_api.PREFIX) :] or "/", method.lower())
        for router in (notification_api.router, notification_pull.router)
        for route in router.routes
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
    assert len(published) == 8
    for server in OPENAPI["servers"]:
        assert server["url"].endswith(notification_api.PREFIX)


def test_every_route_is_gated_at_router_level_as_well_as_by_its_parameter():
    """Defence in depth, in the shape `deployment.require_rushvarsel` uses one surface over: a route
    added later is gated even if its author forgets the principal parameter. FastAPI caches an
    identical dependency within a request, so the credential is still verified once."""
    dependencies = [d.dependency for d in notification_api.router.dependencies]
    assert notification_api._authenticated in dependencies
    assert notification_api._throttled in dependencies, (
        "tier two is spent by a router-level dependency, so 'before anything is created' is a "
        "property of the wiring rather than of whoever wrote the newest handler"
    )
    for route in notification_api.router.routes:
        signature = getattr(route, "endpoint", None)
        assert signature is not None
        assert "principal" in signature.__code__.co_varnames


def test_every_route_is_a_plain_def():
    """They touch Firestore, so they belong in the anyio worker pool like every other route here.

    `/meldinger`'s `async def` inversion is the precedent only for a handler that would otherwise
    hold one of those slots for its whole deadline, which none of these does.
    """
    import inspect

    for route in notification_api.router.routes:
        assert not inspect.iscoroutinefunction(route.endpoint), route.path


def test_the_route_bodies_forbid_unknown_fields():
    """`extra="forbid"` on both sides: on the way in it stops a typo being silently ignored, and on
    the way out it is the drift guard against the published schema."""
    for model in (
        notification_api.SubscriptionIn,
        notification_api.SubscriptionOut,
        notification_api.TestEventIn,
        notification_api.TestEventOut,
        notification_api.DestinationOut,
        notification_api.DeleteResult,
        notification_api.CleanupState,
        notification_api.SubscriptionList,
        notification_api.SigningGenerations,
    ):
        assert model.model_config.get("extra") == "forbid", model.__name__


def test_the_module_never_reaches_the_decision_log_or_the_device_registry():
    """KTD10 and R16: notification state is Firestore's. `varsel_decisions` is written for every match
    including sub-threshold ones and is published on public hosts, and its `pushed` column cannot
    tell a webhook from a Web Push -- so writing webhook deliveries there would corrupt both
    /historikk's and /meldinger's meaning as well as their privacy claim."""
    for forbidden in ("decisions", "varsel_decisions", "import varsel", "journey_digest"):
        assert forbidden not in API_CODE, forbidden
    assert "commit_event" not in API_CODE, (
        "the emit path has exactly one writer; a route calling it falsifies every quota check in "
        "notification_store"
    )


# ---- Current alert state on both GET routes ----------------------------------------------------


def test_the_get_routes_serve_the_current_alert_state():
    """The plan's API table promises servability AND current alert state on both GETs.

    A read of what the notification pass has already committed -- one document per (watch, train)
    with its revision and phase -- so nothing here consults the matcher or `/impact`. The train's
    six fields are the ones an event carries, which is what lets a client join a card to a
    notification.
    """
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer))
        document_id = f"{CLIENT}~commuter-42-morning"
        _put_subject(document_id, "alert-1", notification_store.PHASE_PREDICTED)
        _put_subject(
            document_id, "alert-2", notification_store.PHASE_CONFIRMED, delivered_revision=7
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
                f"{CLIENT}~ref-{index}", f"alert-{index}", notification_store.PHASE_PREDICTED
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

        original = notification_store.subscription_alerts
        notification_store.subscription_alerts = _boom  # type: ignore[assignment]
        try:
            degraded = client.get(
                "/notification-api/v1/subscriptions/commuter-42-morning",
                headers=_headers(bearer),
            )
            assert degraded.status_code == 200, "the subscription itself was read fine"
            assert degraded.json()["alerts"]["truncated"] is True
            assert degraded.json()["alerts"]["n_active"] == 0
        finally:
            notification_store.subscription_alerts = original  # type: ignore[assignment]

        original_many = notification_store.client_alerts
        notification_store.client_alerts = _boom  # type: ignore[assignment]
        try:
            page = client.get("/notification-api/v1/subscriptions", headers=_headers(bearer))
            assert page.status_code == 200
            assert all(r["alerts"]["truncated"] is True for r in page.json()["subscriptions"])
        finally:
            notification_store.client_alerts = original_many  # type: ignore[assignment]
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
        document_id = f"{CLIENT}~commuter-42-morning"
        for index in range(13):
            _put_subject(
                document_id,
                f"alert-{index:02d}",
                notification_store.PHASE_CONFIRMED,
                updated_at=NOW - timedelta(minutes=index),
            )
        alerts = client.get(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(bearer)
        ).json()["alerts"]
        assert alerts["n_active"] == 13
        assert alerts["n_confirmed"] == 13
        assert len(alerts["subjects"]) == notification_store.MAX_ALERTS_SERVED
        assert notification_store.MAX_ALERTS_SERVED == notification_store.MAX_ALERT_DELIVERIES_PER_DAY
        assert alerts["truncated"] is False, "capped for display is not a bounded read"
    finally:
        _uninstall()


def test_a_withdrawn_alert_is_not_served_as_standing():
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer))
        document_id = f"{CLIENT}~commuter-42-morning"
        _put_subject(document_id, "gone", notification_store.PHASE_WITHDRAWN)
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
    """The subject carries its own client_id, and a mismatch is Forbidden in the store -- served
    here as the same 404 every other tenancy check collapses to."""
    _install()
    a_bearer = _provision(CLIENT, DEST)
    _provision(OTHER, OTHER_DEST)
    try:
        client = _client()
        client.post("/notification-api/v1/subscriptions", json=_body(), headers=_headers(a_bearer))
        document_id = f"{CLIENT}~commuter-42-morning"
        _put_subject(document_id, "foreign", notification_store.PHASE_CONFIRMED, client_id=OTHER)
        response = client.get(
            "/notification-api/v1/subscriptions/commuter-42-morning", headers=_headers(a_bearer)
        )
        # The subscription is served; its alert state degrades to unknown rather than leaking.
        assert response.status_code == 200
        assert response.json()["alerts"]["truncated"] is True
        assert OTHER not in response.text
    finally:
        _uninstall()



# ---- The synthetic event is the real shape, not a lookalike ------------------------------------


def _real_payload() -> dict[str, Any]:
    """One payload from the REAL producer, `varsel._webhook_payload`.

    Imported here rather than mirrored: a fixture that restated the keys would agree with itself
    forever. `varsel` is heavy but importable (it logs "no VAPID keypair" and does nothing else at
    import), and this suite runs in its own process like every other.
    """
    import varsel

    alert = varsel.Alert(
        situation_number="ENT:SituationNumber:1",
        summary="Innstilt",
        line="VYG:Line:L1",
        operator="VYG",
        jkey="VYG:DatedServiceJourney:1234",
        stop_place=FROM,
        stop_order=1,
        departure_time=NOW,
        cancel_probability=1.0,
        delay_p50_min=None,
        delay_p90_min=None,
        impact_probability=1.0,
        alert_threshold=0.65,
        alertworthy=True,
        cancelled=True,
    )
    return varsel._webhook_payload(
        alert, [alert], message_input={"i_morgen": False}, title="t", body="b"
    )


def _shape(value: Any) -> Any:
    """A value's KEY structure, with the leaves erased.

    Compares what a client types against -- which keys exist and how they nest -- rather than what
    a particular disruption happened to contain. A list collapses to the shape of its first entry,
    because the schema's arrays are homogeneous and a client writes one row renderer.
    """
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    # Leaves are erased, not typed. A nullable figure IS nullable in the contract -- `delay_ge10` is
    # null for a cancellation and a float for a delay, and a situation carries a description only
    # when an operator wrote one -- so comparing leaf types would fail on per-disruption variation
    # while catching nothing a client types against.
    return "leaf"


def test_the_synthetic_payload_has_the_same_shape_as_a_real_one():
    """A client types their receiver against a test event -- it is the only body they can see
    before a real disruption -- so a synthetic payload with its own key names teaches a shape that
    breaks on the first real event, in production, at go-live.

    Compared PRODUCER TO PRODUCER, in both directions, because the schema cannot catch this:
    `payload` declares no property list and its sub-objects are bare `type: object`, so
    `probabilities.cancel` instead of `probabilities.cancellation` is schema-VALID and wrong. Same
    class as the wire-envelope defect on the sending side, where a deny-list projection would have
    shipped two fields our own verifier refuses -- and was caught only by comparing the projection
    with the schema in both directions.
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
        client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": "probe"},
            headers=_headers(bearer),
        )
        synthetic = json.loads(_deliveries[0]["body"])["payload"]
        real = _real_payload()
        assert set(synthetic) == set(real), (set(synthetic) ^ set(real))
        for key in ("notification", "call", "probabilities"):
            assert set(synthetic[key]) == set(real[key]), (key, set(synthetic[key]) ^ set(real[key]))
        assert set(synthetic["situations"][0]) == set(real["situations"][0])
        # And the whole nested structure, which is what actually catches a renamed key three levels
        # down. `delay` and `alternative_journey` are null on both sides here by construction: this
        # synthetic event is a confirmed cancellation, and every delay figure meant "if it runs".
        assert _shape(synthetic) == _shape(real), (_shape(synthetic), _shape(real))
        # THE PROBABILITY LEAVES' NULLABILITY, which `_shape` erases on purpose -- and the gap that
        # erasure left is not hypothetical: the synthetic event stated `delay_ge10: 1.0` under
        # `phase: confirmed` while every real one carries `null` there, because
        # `impact._cancelled_prediction` drops every delay figure ("if it runs"). A client typing
        # that field as a number, or rendering it with no null guard, breaks on their first genuine
        # cancelled train, in production, at go-live -- which is the exact failure this function was
        # already fixed once to stop teaching. Compared for BOTH producers being null-or-not, never
        # for equal values: the two are different disruptions and only the shape has to agree.
        nullability = {k: v is None for k, v in synthetic["probabilities"].items()}
        assert nullability == {k: v is None for k, v in real["probabilities"].items()}, (
            nullability,
            real["probabilities"],
        )
        assert nullability["delay_ge10"] is True, "a confirmed cancellation states no delay figure"
    finally:
        _uninstall()


def test_the_synthetic_event_is_a_coherent_confirmed_cancellation():
    """The schema's own rule: both probabilities are 1.0 and the delay figures null when `phase` is
    `confirmed`. A synthetic event stating 1.0 under `predicted` with a null delay would teach a
    combination no real event produces -- which is what this one did before."""
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
        assert event["phase"] == "confirmed"
        assert event["payload"]["probabilities"]["impact"] == 1.0
        assert event["payload"]["probabilities"]["cancellation"] == 1.0
        # BOTH delay figures null, not just the one under `delay`: `probabilities.delay_ge10` is a
        # delay figure too, and `impact._cancelled_prediction` drops it for the same stated reason.
        assert event["payload"]["probabilities"]["delay_ge10"] is None
        assert event["payload"]["delay"] is None
        assert event["payload"]["effect"] == "cancelled"
        assert "innstilt" in event["payload"]["notification"]["title"].lower()
        # The top level carries situation NUMBERS and the payload the rich objects -- the store's
        # own split, not two ways of saying one thing.
        assert event["situations"] == [event["payload"]["situations"][0]["situation_number"]]
        assert event["payload"]["situations"][0]["has_situation"] is False
        # And the threshold agrees with that flag. A real event's `alert_threshold` is the gate's
        # own, resolved with the situation's `has_situation` -- so an unnamed train carries the
        # no-SX cut, never the published one. The shape test above cannot catch this: both are
        # floats under the same key.
        assert event["payload"]["probabilities"]["alert_threshold"] == alert_policy.threshold_for(
            "VYG", has_situation=False
        )
        assert event["payload"]["probabilities"]["alert_threshold"] != alert_policy.threshold_for(
            "VYG"
        ), "the two cuts must differ, or this assertion proves nothing"
    finally:
        _uninstall()


def test_the_test_event_is_projected_and_serialized_by_the_delivery_pass():
    """One producer of wire bytes, not two.

    The allow-list projection is what decides what leaves this process, and it is
    `additionalProperties: false` on the other side -- so a field the store gains, or a property the
    schema gains, has to change both paths at once rather than leaving the test route shipping last
    month's envelope. The serializer is the same one for the same reason: sorted keys, UTF-8, one
    timestamp rendering.
    """
    import webhook_delivery

    assert "webhook_delivery.event_body(webhook_delivery.wire_event(" in API_CODE
    assert "json.dumps" not in API_CODE, "there is one serializer, and it is not here"
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
        raw = _deliveries[0]["body"]
        event = json.loads(raw)
        # Exactly the published envelope: nothing the schema forbids, and every key it requires.
        assert set(event) <= set(EVENT_SCHEMA["properties"])
        assert set(EVENT_SCHEMA["required"]) <= set(event)
        # Byte-identical to what the delivery pass would send for the same document, which is the
        # property "one producer" actually means.
        assert raw == webhook_delivery.event_body(webhook_delivery.wire_event(event))
        assert list(event) == sorted(event), "sorted keys, so two attempts are byte-identical"
    finally:
        _uninstall()


def test_a_test_event_accepts_the_same_ids_the_path_routes_do():
    """A client takes the id from a GET response -- or from the event their receiver just handled
    -- and posts it here. Rejecting the namespaced form was a 422 on a value this API had itself
    just served."""
    _install()
    bearer = _provision()
    try:
        client = _client()
        client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="probe", test=True),
            headers=_headers(bearer),
        )
        namespaced = f"{CLIENT}~probe"
        for reference in ("probe", namespaced):
            response = client.post(
                "/notification-api/v1/test-events",
                json={"subscription_id": reference},
                headers=_headers(bearer),
            )
            assert response.status_code == 200, (reference, response.text)
            assert response.json()["subscription_id"] == "probe"
        # The namespaced form the GET route serves in an event body is the same one accepted here.
        stored = json.loads(_deliveries[0]["body"])["subscription_id"]
        assert stored == namespaced
        assert client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": stored},
            headers=_headers(bearer),
        ).status_code == 200
        # Still bounded, and still no path to another client: the prefix stripped is the caller's.
        assert client.post(
            "/notification-api/v1/test-events",
            json={"subscription_id": f"{OTHER}~probe"},
            headers=_headers(bearer),
        ).status_code == 404
    finally:
        _uninstall()


def test_the_openapi_tags_are_the_routers_own():
    """The published document is what U7 generates from and what a client reads; this app's /docs
    is what an operator reads. Two groupings for one surface is a difference somebody reconciles by
    hand, so they are one vocabulary, pinned in both directions."""
    published = {tag["name"] for tag in OPENAPI["tags"]}
    used = {
        tag
        for operations in OPENAPI["paths"].values()
        for method, op in operations.items()
        if method != "parameters"
        for tag in op["tags"]
    }
    router_tags = {
        tag
        for router in (notification_api.router, notification_pull.router)
        for route in router.routes
        for tag in (route.tags or [])
    }
    assert published == used == router_tags
    assert router_tags == {
        notification_api.TAG_SUBSCRIPTIONS,
        notification_api.TAG_DESTINATIONS,
        notification_api.TAG_TEST,
        # The public read's own tag (KTD12, 2026-09-10): a fourth grouping, because the tag is
        # what a generated client and this app's /docs both group on, and "public, no credential"
        # is worth a heading of its own beside three credentialed ones.
        notification_pull.TAG,
    }
    # Prefixed, because these eight routes sit among three dozen paths in this app.
    assert all(tag.startswith("notification-") for tag in router_tags)


def test_the_public_read_overrides_the_global_security_per_operation():
    """KTD12's third scenario: the document keeps its global `clientKey` requirement, and the ONE
    public operation says `security: []` on itself rather than the file dropping the global.

    Both directions matter. A global that went away would publish six credentialed routes as open;
    a pull operation without its own override would tell a generated client to send a bearer it
    does not have -- and the read would still answer 200, so nothing at runtime would notice.
    """
    assert OPENAPI["security"] == [{"clientKey": []}]
    public = ("/journey-alerts", "/journey-status")  # the second since plan 2026-09-22-001
    for path in public:
        operation = OPENAPI["paths"][path]["get"]
        assert operation["security"] == [], path
        assert operation["tags"] == [notification_pull.TAG], path
    # Every credentialed operation inherits the global and declares nothing of its own.
    for path, operations in OPENAPI["paths"].items():
        for method, op in operations.items():
            if method == "parameters" or path in public:
                continue
            assert "security" not in op, (path, method)


def test_the_journey_status_schema_matches_the_response_models_in_both_directions():
    """`additionalProperties: false` at every level, so a field added on one side and not the other
    is a client's generated types refusing a response this API sent."""
    import journey_status

    schemas = OPENAPI["components"]["schemas"]
    top = schemas["JourneyStatus"]
    assert set(notification_pull.JourneyStatusResponse.model_fields) == set(top["properties"])
    assert set(top["required"]) == set(top["properties"])
    train = schemas["TrainStatus"]
    assert set(journey_status.TrainStatus.model_fields) == set(train["properties"])
    assert set(train["required"]) == set(train["properties"])
    as_of = top["properties"]["as_of"]
    assert set(journey_status.AsOf.model_fields) == set(as_of["properties"]) == set(as_of["required"])
    assert train["properties"]["state"]["enum"] == ["confirmed", "predicted", "unknown"]
    assert train["properties"]["level"]["enum"] == ["red", "yellow", "green", "grey"]
    assert top["properties"]["reason"]["enum"] == ["not_a_travel_day", None]
    # The echoed definition and the coverage block are the alerts read's own, not restatements.
    alerts = schemas["JourneyAlerts"]["properties"]
    assert top["properties"]["journey"] == alerts["journey"]
    assert set(top["properties"]["coverage"]["properties"]) == set(alerts["coverage"]["properties"])


def test_the_call_payload_promises_only_what_both_producers_emit():
    """A schema describing a richer document than exists is worse than one describing less: a
    client writes a renderer for the fields it names.

    Read off the SCHEMA and the two producers, so re-adding the promise without emitting the fields
    fails here rather than at a client's first integration.
    """
    description = EVENT_SCHEMA["properties"]["payload"]["properties"]["call"]["description"]
    emitted = set(_real_payload()["call"])
    assert emitted == {
        "stop_place",
        "stop_order",
        "aimed_departure_time",
        "departs_tomorrow",
    }, emitted
    for field in sorted(emitted):
        assert field in description, field
    # The two it used to promise and neither producer sends.
    assert "expected times" not in description
    assert "boarding_allowed" not in description



def _run_all() -> None:
    tests = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in tests:
        fn()
        print(f"  ok  {name}")
    print(f"\n{len(tests)} notification API checks passed.")



# ---- Per-client quotas (2026-09-09) ------------------------------------------------------------


def test_a_clients_own_quotas_govern_its_ceiling_its_rate_and_its_subscriptions_cap():
    """The three pilot constants are per-client DEFAULTS: an operator raises or lowers them on the
    account, the router reads them off the Principal, and nothing needs a deploy. The daily cap is
    copied onto the subscription document, which is where the delivery pass reads it."""
    _install()
    bearer = _provision()
    try:
        notification_auth.set_client_quotas(
            CLIENT,
            actor="op@entur.org",
            max_active_subscriptions=1,
            requests_per_minute=4,
            daily_cap=9,
        )
        client = _client()
        first = client.post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert first.status_code == 201, first.text
        stored = _fake.doc(
            notification_store.SUBSCRIPTIONS, f"{CLIENT}~{_body()['subscription_id']}"
        )
        assert stored["daily_cap"] == 9 and stored["channel"] == "webhook"
        second = client.post(
            "/notification-api/v1/subscriptions",
            json=_body(subscription_id="another"),
            headers=_headers(bearer),
        )
        assert second.status_code == 429 and second.json() == {"detail": "quota_exceeded"}
        # Two requests spent against a per-minute allowance of four: the fifth is refused.
        codes = [
            client.get("/notification-api/v1/subscriptions", headers=_headers(bearer)).status_code
            for _ in range(3)
        ]
        assert codes == [200, 200, 429], codes
        # The idempotent re-PUT is the write that carries a CHANGED cap to an existing subscription
        # (the rate is raised alongside, since this client has just spent its four).
        notification_auth.set_client_quotas(
            CLIENT, actor="op@entur.org", requests_per_minute=100, daily_cap=5
        )
        again = client.post(
            "/notification-api/v1/subscriptions", json=_body(), headers=_headers(bearer)
        )
        assert again.status_code == 200, again.text
        refreshed = _fake.doc(
            notification_store.SUBSCRIPTIONS, f"{CLIENT}~{_body()['subscription_id']}"
        )
        assert refreshed["daily_cap"] == 5 and refreshed["version"] == stored["version"]
    finally:
        _uninstall()

if __name__ == "__main__":
    _run_all()
