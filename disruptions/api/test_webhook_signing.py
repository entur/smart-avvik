"""Checks for webhook_signing.py -- KTD7's signing, R10's rotation and freshness.

**The vectors file is re-derived here, not merely read.** Every secret, signature and header value in
``contracts/notifications/v1/signing-vectors.json`` is recomputed from this module and compared, so
the file cannot rot away from the code that produced it -- which is the only thing that makes it
usable as a cross-language contract. U7's TypeScript verifier consumes the same file through the
``standardwebhooks`` npm package; if these checks pass and that one fails, the disagreement is in
the framing rather than in either implementation's arithmetic.

The asymmetry worth knowing before reading the vector checks: npm's ``Webhook.verify()`` consults
``Date.now()`` and takes no injectable clock, while :func:`webhook_signing.verify` accepts ``now``.
So the vectors' fixed timestamps are cross-checked through ``sign()`` on the TypeScript side and
through ``verify(now=...)`` here. The file says so in ``how_to_consume``, and
``test_the_vectors_file_is_self_describing`` keeps it saying so.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import pathlib
import re
import time

import webhook_signing as ws

MODULE_SOURCE = pathlib.Path(ws.__file__).read_text()
REPO = pathlib.Path(__file__).resolve().parents[2]
VECTORS_PATH = REPO / "contracts" / "notifications" / "v1" / "signing-vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text())

ROOT = b"smart-avvik-test-signing-root-not-used-in-any-deployment"
OTHER_ROOT = b"a-completely-different-root-secret-for-comparison"
DEST_A = "dst_client_a_prod"
DEST_B = "dst_client_b_prod"
BODY = b'{"event_id":"7f3c1a","revision":351}'


def _vector(name: str) -> dict:
    for vector in VECTORS["vectors"]:
        if vector["name"] == name:
            return vector
    raise AssertionError(f"no vector named {name!r}")


def _reset() -> None:
    os.environ.pop(ws.SIGNING_ROOT_ENV, None)
    ws.reset_for_tests()


# --- The root secret ----------------------------------------------------------------------------


def test_the_root_is_read_lazily_and_never_at_import_time():
    # `main` imports every module it might route to, and a deployment with the channel switched off
    # must import this one without the secret. An import-time read would also freeze the value
    # against a restart-free rotation.
    # Column 0 is module scope, so any un-indented statement touching the environment is an
    # import-time read. (The `SIGNING_ROOT_ENV = "..."` constant only NAMES the variable, which is
    # exactly the line a laxer grep for the variable's name would flag instead.)
    module_level = [
        line
        for line in MODULE_SOURCE.splitlines()
        if line[:1] not in ("", " ", "\t", "#") and "os.environ" in line
    ]
    assert module_level == [], module_level
    assert ws.configured() is False, "unset must read as not configured, not as an ephemeral key"


def test_an_absent_or_short_root_fails_closed_and_loudly():
    try:
        ws.signing_root()
    except ws.SigningNotConfigured as exc:
        assert ws.SIGNING_ROOT_ENV in str(exc)
    else:
        raise AssertionError("an unset root must raise")
    # Short is a provisioning mistake, and refusing it is the only moment anybody notices. There is
    # deliberately no ephemeral fallback here (unlike sso.py's session key, where a wrong-but-
    # working key only logs somebody out): an unsigned or wrongly-signed client request is a hole
    # on their side and a retry storm on ours.
    os.environ[ws.SIGNING_ROOT_ENV] = "x" * (ws.MIN_ROOT_BYTES - 1)
    try:
        ws.signing_root()
    except ws.SigningNotConfigured:
        pass
    else:
        raise AssertionError("a short root must raise")
    os.environ[ws.SIGNING_ROOT_ENV] = "x" * ws.MIN_ROOT_BYTES
    assert ws.configured() is True
    assert ws.signing_root() == b"x" * ws.MIN_ROOT_BYTES


def test_the_published_test_root_is_refused_by_name():
    """A root anyone can read out of this repository must not be a root any deployment can use.

    `contracts/notifications/v1/signing-vectors.json` publishes `test_root` so a client can
    reproduce a signature by hand -- which is what the file is for -- and it is 48 bytes, so it
    cleared `MIN_ROOT_BYTES` and every readiness check passed for it: `configured()` answered True,
    `/destinations/current` reported `ready: true`, `/test-events` answered 200, and every delivery
    signed and verified normally. The secrets that root derives are committed in the same file and
    vendored again under `integrations/`, so anyone could forge a cancellation or a withdrawal about
    a real train against a receiver whose only authentication IS the signature.

    The runbook tells an operator to export `WEBHOOK_SIGNING_ROOT` by hand for `show-signing-secret`,
    which is precisely the moment a documented example value gets pasted -- so this is a plausible
    provisioning slip rather than a hypothetical, and it produced no fault report anywhere.

    The literal is read OUT of the vectors file rather than restated here, so the module's refusal
    and the published value cannot drift apart. Regenerating the vectors under a different root is
    fine; regenerating them under one this refuses is what must stay impossible.
    """
    _reset()
    published = VECTORS["derivation"]["test_root"].encode()
    assert published == ws._PUBLISHED_TEST_ROOT, (
        "the module refuses one literal and the contract publishes another"
    )
    assert len(published) >= ws.MIN_ROOT_BYTES, (
        "the length test alone admits it -- which is why a rejected-value check is needed at all"
    )
    os.environ[ws.SIGNING_ROOT_ENV] = published.decode()
    try:
        assert ws.configured() is False
        try:
            ws.signing_root()
        except ws.SigningNotConfigured as exc:
            # Naming the file is the whole value of the message: an operator who pasted it needs to
            # recognise WHERE from, not be told the variable is unset when it plainly is not.
            assert "signing-vectors" in str(exc), str(exc)
        else:
            raise AssertionError("the published example root must raise")
    finally:
        _reset()
    # And the injection path is untouched: the vectors are still reproducible with `root=`, which is
    # what the file is published for.
    assert ws.sign(
        destination_id=DEST_A, generations=[1], webhook_id="dlv_1", body=BODY, root=published
    ).headers[ws.HEADER_SIGNATURE]



def test_an_earlier_published_root_is_refused_too():
    """The vectors used to publish kix-avvik's example root. The current file no longer carries it,
    but this repository's public history does, with every secret it derives, so it must be refused
    as firmly as the current one rather than slip back in as an ordinary 48-byte root."""
    _reset()
    for retired in ws._RETIRED_TEST_ROOTS:
        assert len(retired) >= ws.MIN_ROOT_BYTES, "the length test alone would admit it"
        os.environ[ws.SIGNING_ROOT_ENV] = retired.decode()
        try:
            assert ws.configured() is False
            try:
                ws.signing_root()
            except ws.SigningNotConfigured as exc:
                assert "signing-vectors" in str(exc), str(exc)
            else:
                raise AssertionError(f"{retired!r} must raise")
        finally:
            _reset()

def test_surrounding_whitespace_is_stripped_and_warned_about_once(caplog=None):
    # The chart's own note: a payload written with `echo` rather than `printf '%s'` carries a
    # trailing newline, which here would re-derive EVERY destination's secret -- so every client's
    # verification fails at once and nothing on our side reports a configuration fault.
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    logger = logging.getLogger("webhook_signing")
    logger.addHandler(handler)
    try:
        os.environ[ws.SIGNING_ROOT_ENV] = " " + "x" * ws.MIN_ROOT_BYTES + "\n"
        assert ws.signing_root() == b"x" * ws.MIN_ROOT_BYTES
        assert len(records) == 1
        assert ws.SIGNING_ROOT_ENV in records[0].getMessage() % records[0].args
        ws.signing_root()
        assert len(records) == 1, "the warning must fire once per process, not per read"
    finally:
        logger.removeHandler(handler)


# --- Derivation ---------------------------------------------------------------------------------


def test_a_destinations_secret_is_derived_so_the_store_holds_no_signing_material():
    secret = ws.derive_secret(DEST_A, 2, root=ROOT)
    assert secret.startswith(ws.SECRET_PREFIX)
    expected = hmac.new(ROOT, ws.derivation_material(DEST_A, 2), hashlib.sha256).digest()
    assert ws.secret_key(secret) == expected
    assert len(ws.secret_key(secret)) == ws.SECRET_BYTES == 32
    # Deterministic: nothing needs to be stored for it to be reproducible.
    assert ws.derive_secret(DEST_A, 2, root=ROOT) == secret


def test_one_destinations_secret_forges_nothing_for_another():
    assert ws.derive_secret(DEST_A, 2, root=ROOT) != ws.derive_secret(DEST_B, 2, root=ROOT)
    assert ws.derive_secret(DEST_A, 2, root=ROOT) != ws.derive_secret(DEST_A, 3, root=ROOT)
    # Rotating the root rotates every destination at once, which is exactly why generations overlap.
    assert ws.derive_secret(DEST_A, 2, root=ROOT) != ws.derive_secret(DEST_A, 2, root=OTHER_ROOT)


def test_the_derivation_material_is_length_framed_so_no_two_pairs_can_collide():
    # ("a", 11) and ("a1", 1) are the pair that a bare concatenation merges, and a bare separator
    # only holds while no destination id contains that separator -- which an operator-supplied
    # string cannot promise.
    assert ws.derivation_material("a", 11) != ws.derivation_material("a1", 1)
    assert ws.derivation_material("a|1", 1) != ws.derivation_material("a", 11)
    # Every pair whose naive concatenation collides, checked pairwise: an id carrying the separator
    # is the case a delimiter-only framing gets wrong.
    pairs = [("a|1", 2), ("a", 12), ("a|", 12), ("|a", 1), ("a", 1), ("a1", 2)]
    materials = [ws.derivation_material(d, g) for d, g in pairs]
    assert len(set(materials)) == len(pairs), pairs
    # And an id that ENDS with the join character cannot borrow its neighbour's field, which is the
    # case a delimiter-only framing gets wrong even when both ids are distinct.
    for left, right in ((("a|", 1), ("a", 1)), (("|a", 1), ("a", 1)), (("a", 1), ("a", 10))):
        assert ws.derivation_material(*left) != ws.derivation_material(*right), (left, right)


def test_the_material_carries_a_purpose_tag_like_ssos_tokens():
    # One key, disjoint messages: a future second use of the same root cannot collide with this one.
    material = ws.derivation_material(DEST_A, 2)
    assert material.startswith(b"smart-avvik.webhook.signing.v1|")
    assert b"|17|dst_client_a_prod|2" in material


def test_a_bad_destination_id_or_generation_is_refused():
    for bad_args in (("", 1), (DEST_A, -1), (DEST_A, True)):
        try:
            ws.derivation_material(*bad_args)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"{bad_args!r} should have been refused")
    # Generation 0 is legal: nothing in this module assumes where U4 starts counting.
    assert ws.derivation_material(DEST_A, 0)


def test_a_secret_that_is_not_a_whsec_value_is_refused():
    for bad in ("", "nope", "whsec_", "whsec_!!!not-base64!!!", base64.b64encode(b"x").decode()):
        try:
            ws.secret_key(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been refused")


def test_the_secret_body_is_standard_padded_base64_not_the_urlsafe_form():
    # A different contract from sso.py's cookie: the npm package base64-decodes this string with the
    # standard alphabet and expects padding. Reaching for urlsafe_b64encode or stripping '=' here
    # would hand a client a secret their own library cannot read.
    for generation in range(64):
        body = ws.derive_secret(DEST_A, generation, root=ROOT)[len(ws.SECRET_PREFIX) :]
        assert body.endswith("="), body
        assert re.fullmatch(r"[A-Za-z0-9+/]+=*", body), body
    # Matched as a CALL, so the docstring naming it as the wrong choice does not satisfy the check.
    assert not re.search(r"urlsafe_b64(en|de)code\s*\(", MODULE_SOURCE)
    assert not re.search(r"\.rstrip\(\s*b?[\"']=", MODULE_SOURCE), "padding must not be stripped"


# --- Signing ------------------------------------------------------------------------------------


def test_the_signed_content_is_the_standard_webhooks_shape_over_the_exact_body_bytes():
    content = ws.signed_content("dlv_1", 1789197300, BODY)
    assert content == b"dlv_1.1789197300." + BODY
    # Bytes, not a decoded string: a body carrying Norwegian text must not be re-encoded on the way
    # in, and a body that is not valid UTF-8 must still sign.
    assert ws.signed_content("dlv_1", 1, b"\xff\xfe") == b"dlv_1.1.\xff\xfe"


def test_the_webhook_id_framing_is_guarded():
    # Nothing in the spec forbids a '.' in the id and a receiver would still verify -- but the
    # framing would be ambiguous, and we are the side that chooses ids.
    for bad in ("", "dlv.1", "dlv 1", "dlv\n1", "dlv\t1"):
        try:
            ws.signed_content(bad, 1, BODY)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} should have been refused as a webhook_id")
    try:
        ws.signed_content("dlv_1", "1789197300", BODY)  # type: ignore[arg-type]
    except TypeError:
        pass
    else:
        # It would have produced a VALID signature over the same digits, so nothing would fail
        # until a caller passed a differently-formatted timestamp.
        raise AssertionError("a string timestamp should have been refused")


def test_an_entry_is_the_tagged_base64_hmac_of_the_signed_content():
    secret = ws.derive_secret(DEST_A, 2, root=ROOT)
    entry = ws.sign_entry(secret, webhook_id="dlv_1", timestamp=1789197300, body=BODY)
    tag, _, signature = entry.partition(",")
    assert tag == ws.ALGORITHM_TAG == "v1"
    expected = hmac.new(
        ws.secret_key(secret), b"dlv_1.1789197300." + BODY, hashlib.sha256
    ).digest()
    assert base64.b64decode(signature) == expected
    assert signature.endswith("=")  # padded, standard alphabet


def test_the_header_carries_one_space_delimited_entry_per_generation_in_the_given_order():
    signed = ws.sign(
        destination_id=DEST_A,
        generations=[3, 2],
        webhook_id="dlv_1",
        body=BODY,
        timestamp=1789197300,
        root=ROOT,
    )
    entries = signed.headers[ws.HEADER_SIGNATURE].split(" ")
    assert len(entries) == 2
    for entry, generation in zip(entries, (3, 2), strict=True):
        assert entry == ws.sign_entry(
            ws.derive_secret(DEST_A, generation, root=ROOT),
            webhook_id="dlv_1",
            timestamp=1789197300,
            body=BODY,
        )
    assert set(signed.headers) == {ws.HEADER_ID, ws.HEADER_TIMESTAMP, ws.HEADER_SIGNATURE}
    assert signed.headers[ws.HEADER_ID] == "dlv_1"
    assert signed.headers[ws.HEADER_TIMESTAMP] == "1789197300"
    # Lowercase, because the npm verifier indexes its headers object by these exact strings.
    assert list(signed.headers) == [h.lower() for h in signed.headers]


def test_sign_hands_back_the_same_body_object_it_signed():
    # The canonical webhook bug is signing one serialization and sending another. Returning the
    # caller's own bytes leaves one obvious way to send exactly what was signed.
    signed = ws.sign(
        destination_id=DEST_A, generations=[1], webhook_id="dlv_1", body=BODY, root=ROOT
    )
    assert signed.body is BODY


def test_signing_needs_at_least_one_live_generation():
    for generations in ([], ()):
        try:
            ws.sign(
                destination_id=DEST_A,
                generations=generations,
                webhook_id="dlv_1",
                body=BODY,
                root=ROOT,
            )
        except ValueError:
            continue
        raise AssertionError("a destination with no live generation cannot be signed for")


def test_sign_mints_a_fresh_timestamp_when_none_is_given():
    before = int(time.time())
    signed = ws.sign(
        destination_id=DEST_A, generations=[1], webhook_id="dlv_1", body=BODY, root=ROOT
    )
    assert before <= signed.timestamp <= int(time.time())
    assert signed.headers[ws.HEADER_TIMESTAMP] == str(signed.timestamp)


def test_sign_reads_the_environment_when_no_root_is_passed():
    # NOT `ROOT`: that is the value the contract publishes and `signing_root()` now refuses it by
    # name, so an environment read of it raises rather than signing. The other suites' fixtures
    # ('r' * 40 in test_notification_auth and test_notification_api, 'r' * 48 in test_webhook_delivery) are
    # arbitrary for the same reason. `ROOT` stays as the module-level constant for the `root=`
    # injection path, which is what re-derives the published vectors.
    env_root = b"e" * 40
    os.environ[ws.SIGNING_ROOT_ENV] = env_root.decode()
    signed = ws.sign(
        destination_id=DEST_A,
        generations=[2],
        webhook_id="dlv_1",
        body=BODY,
        timestamp=1789197300,
    )
    assert signed.headers[ws.HEADER_SIGNATURE] == ws.sign(
        destination_id=DEST_A,
        generations=[2],
        webhook_id="dlv_1",
        body=BODY,
        timestamp=1789197300,
        root=env_root,
    ).headers[ws.HEADER_SIGNATURE]


# --- Scenario 1 / AE8: fresh per attempt, stable id ---------------------------------------------


def test_two_attempts_share_the_webhook_id_and_differ_in_timestamp_and_signature():
    """Scenario 1 / AE8.

    The failure this prevents is subtle enough to be worth spelling out: reusing the first
    attempt's timestamp on a retry that crosses a poller cycle (~296 s measured) puts it outside the
    receiver's 300 s replay window, so a CORRECT receiver drops our own retry as an attack -- and
    the traveller is never told, while our logs record a delivered attempt refused for no visible
    reason.
    """
    first = ws.sign(
        destination_id=DEST_A,
        generations=[2],
        webhook_id="dlv_stable",
        body=BODY,
        timestamp=1789197300,
        root=ROOT,
    )
    second = ws.sign(
        destination_id=DEST_A,
        generations=[2],
        webhook_id="dlv_stable",
        body=BODY,
        timestamp=1789197300 + 297,
        root=ROOT,
    )
    assert first.headers[ws.HEADER_ID] == second.headers[ws.HEADER_ID] == "dlv_stable"
    assert first.headers[ws.HEADER_TIMESTAMP] != second.headers[ws.HEADER_TIMESTAMP]
    assert first.headers[ws.HEADER_SIGNATURE] != second.headers[ws.HEADER_SIGNATURE]
    secret = ws.derive_secret(DEST_A, 2, root=ROOT)
    for attempt in (first, second):
        assert ws.verify(
            secret=secret,
            webhook_id=attempt.webhook_id,
            timestamp=attempt.timestamp,
            body=attempt.body,
            header=attempt.headers[ws.HEADER_SIGNATURE],
            now=attempt.timestamp,
        ), "each attempt must verify against its own timestamp"
    # And the first attempt's header does NOT verify under the second attempt's timestamp, which is
    # what makes the regeneration necessary rather than cosmetic.
    assert not ws.verify(
        secret=secret,
        webhook_id="dlv_stable",
        timestamp=second.timestamp,
        body=BODY,
        header=first.headers[ws.HEADER_SIGNATURE],
        now=second.timestamp,
    )


# --- Scenario 5: tampering, staleness, rotation -------------------------------------------------


def test_tampered_bytes_do_not_verify():
    secret = ws.derive_secret(DEST_A, 2, root=ROOT)
    header = ws.signature_header([secret], webhook_id="dlv_1", timestamp=1789197300, body=BODY)
    for tampered in (BODY + b" ", BODY.replace(b"351", b"352"), BODY[:-1], b""):
        result = ws.verify(
            secret=secret,
            webhook_id="dlv_1",
            timestamp=1789197300,
            body=tampered,
            header=header,
            now=1789197300,
        )
        assert not result and result.reason == "no_matching_signature", tampered


def test_a_stale_timestamp_is_refused_for_the_timestamp_and_not_for_the_mac():
    secret = ws.derive_secret(DEST_A, 2, root=ROOT)
    stamp = 1767225600
    header = ws.signature_header([secret], webhook_id="dlv_1", timestamp=stamp, body=BODY)

    def check(now: int):
        return ws.verify(
            secret=secret,
            webhook_id="dlv_1",
            timestamp=stamp,
            body=BODY,
            header=header,
            now=now,
        )

    assert check(stamp).ok is True
    assert check(stamp + ws.TOLERANCE_S).ok is True
    stale = check(stamp + ws.TOLERANCE_S + 1)
    assert not stale and stale.reason == "stale_timestamp"
    # Tolerance is symmetric: a timestamp from the future is as suspect as one from the past.
    ahead = check(stamp - ws.TOLERANCE_S - 1)
    assert not ahead and ahead.reason == "stale_timestamp"
    assert ws.TOLERANCE_S == 300


def test_both_generations_verify_during_an_overlap_and_each_is_revocable_alone():
    active = ws.derive_secret(DEST_A, 3, root=ROOT)
    retiring = ws.derive_secret(DEST_A, 2, root=ROOT)
    revoked = ws.derive_secret(DEST_A, 1, root=ROOT)
    header = ws.sign(
        destination_id=DEST_A,
        generations=[3, 2],
        webhook_id="dlv_1",
        body=BODY,
        timestamp=1789197300,
        root=ROOT,
    ).headers[ws.HEADER_SIGNATURE]

    def check(secret: str):
        return ws.verify(
            secret=secret,
            webhook_id="dlv_1",
            timestamp=1789197300,
            body=BODY,
            header=header,
            now=1789197300,
        )

    assert check(active).ok is True
    assert check(retiring).ok is True, "a receiver still holding the old secret must keep working"
    assert not check(revoked)
    # Dropping generation 2 from the destination record revokes it and leaves 3 working: the header
    # is simply re-signed with one entry. No flag day, and nothing to coordinate with the client.
    single = ws.sign(
        destination_id=DEST_A,
        generations=[3],
        webhook_id="dlv_1",
        body=BODY,
        timestamp=1789197300,
        root=ROOT,
    ).headers[ws.HEADER_SIGNATURE]
    assert (
        ws.verify(
            secret=active,
            webhook_id="dlv_1",
            timestamp=1789197300,
            body=BODY,
            header=single,
            now=1789197300,
        ).ok
        is True
    )
    assert not ws.verify(
        secret=retiring,
        webhook_id="dlv_1",
        timestamp=1789197300,
        body=BODY,
        header=single,
        now=1789197300,
    )


def test_revoking_one_destinations_key_leaves_another_working():
    header_a = ws.sign(
        destination_id=DEST_A,
        generations=[2],
        webhook_id="dlv_a",
        body=BODY,
        timestamp=1789197300,
        root=ROOT,
    ).headers[ws.HEADER_SIGNATURE]
    header_b = ws.sign(
        destination_id=DEST_B,
        generations=[2],
        webhook_id="dlv_b",
        body=BODY,
        timestamp=1789197300,
        root=ROOT,
    ).headers[ws.HEADER_SIGNATURE]

    def check(secret_dest: str, webhook_id: str, header: str):
        return ws.verify(
            secret=ws.derive_secret(secret_dest, 2, root=ROOT),
            webhook_id=webhook_id,
            timestamp=1789197300,
            body=BODY,
            header=header,
            now=1789197300,
        )

    assert check(DEST_A, "dlv_a", header_a).ok is True
    assert check(DEST_B, "dlv_b", header_b).ok is True
    # A leaked receiver secret is one destination's problem and nobody else's.
    assert not check(DEST_B, "dlv_a", header_a)
    assert not check(DEST_A, "dlv_b", header_b)


def test_verification_reports_why_rather_than_only_that_it_failed():
    secret = ws.derive_secret(DEST_A, 2, root=ROOT)
    header = ws.signature_header([secret], webhook_id="dlv_1", timestamp=1789197300, body=BODY)
    common = {"webhook_id": "dlv_1", "body": BODY, "now": 1789197300}
    assert ws.verify(secret=secret, timestamp="not-a-number", header=header, **common).reason == (
        "malformed_timestamp"
    )
    assert ws.verify(secret=secret, timestamp=1789197300, header="", **common).reason == (
        "malformed_header"
    )
    assert ws.verify(secret="not-a-secret", timestamp=1789197300, header=header, **common).reason == (
        "malformed_secret"
    )
    assert ws.verify(secret=secret, timestamp=1789197300, header="v9,abc", **common).reason == (
        "no_matching_signature"
    ), "an unknown algorithm tag is not a match"
    assert ws.verify(secret=secret, timestamp=1789197300, header=header, **common).reason is None


def test_a_matching_entry_anywhere_in_the_header_verifies():
    # Entry order carries no meaning -- that is what makes each generation independently revocable.
    secret = ws.derive_secret(DEST_A, 2, root=ROOT)
    mine = ws.sign_entry(secret, webhook_id="dlv_1", timestamp=1789197300, body=BODY)
    for header in (mine, f"v1,ZmFrZQ== {mine}", f"{mine} v1,ZmFrZQ==", f"v9,x {mine}"):
        assert ws.verify(
            secret=secret,
            webhook_id="dlv_1",
            timestamp=1789197300,
            body=BODY,
            header=header,
            now=1789197300,
        ).ok is True, header


# --- The vectors file ---------------------------------------------------------------------------


def test_every_vector_is_re_derived_from_this_module():
    """The file cannot drift: every value in it is recomputed here."""
    root = VECTORS["derivation"]["test_root"].encode()
    assert root == ROOT
    checked = 0
    for vector in VECTORS["vectors"]:
        body = vector["body"].encode(vector["body_encoding"])
        assert len(body) == vector["body_utf8_byte_length"], vector["name"]
        assert hashlib.sha256(body).hexdigest() == vector["body_sha256_hex"], vector["name"]
        stamp = int(vector["webhook_timestamp"])
        for entry in vector["secrets"]:
            secret = ws.derive_secret(entry["destination_id"], entry["generation"], root=root)
            assert secret == entry["secret"], (vector["name"], entry["generation"])
            assert entry["signature"] == ws.sign_entry(
                secret, webhook_id=vector["webhook_id"], timestamp=stamp, body=body
            ), (vector["name"], entry["generation"])
            checked += 1
    assert checked >= 12, checked


def test_every_vector_verifies_exactly_as_it_says_it_does():
    root = VECTORS["derivation"]["test_root"].encode()
    for vector in VECTORS["vectors"]:
        body = vector["body"].encode(vector["body_encoding"])
        stamp = int(vector["webhook_timestamp"])
        # Every `verifies` flag is a claim at the vector's own `verify_at_unix_seconds`, never at
        # "now" -- which is what keeps the file timeless. A field meaning "fresh today" would read
        # as a lie the day after this file was generated.
        now = int(vector["verify_at_unix_seconds"])
        for entry in vector["secrets"]:
            result = ws.verify(
                secret=ws.derive_secret(entry["destination_id"], entry["generation"], root=root),
                webhook_id=vector["webhook_id"],
                timestamp=stamp,
                body=body,
                header=vector["signature_header"],
                now=now,
            )
            assert result.ok is entry["verifies"], (
                vector["name"],
                entry["destination_id"],
                entry["generation"],
                result.reason,
            )


def test_the_stale_vector_carries_a_correct_mac_so_only_the_timestamp_is_wrong():
    """Otherwise it would test two things at once and isolate neither."""
    vector = _vector("stale-timestamp")
    root = VECTORS["derivation"]["test_root"].encode()
    stamp = int(vector["webhook_timestamp"])
    assert vector["mac_is_correct"] is True
    assert int(vector["verify_at_unix_seconds"]) == stamp + ws.TOLERANCE_S + 1
    entry = vector["secrets"][0]
    secret = ws.derive_secret(entry["destination_id"], entry["generation"], root=root)
    common = {
        "secret": secret,
        "webhook_id": vector["webhook_id"],
        "timestamp": stamp,
        "body": vector["body"].encode("utf-8"),
        "header": vector["signature_header"],
    }
    assert ws.verify(now=stamp, **common).ok is True, "the MAC itself must be correct"
    assert ws.verify(now=stamp + ws.TOLERANCE_S + 1, **common).reason == "stale_timestamp"
    # And it stays stale for good, which is what lets a wall-clock verifier consume it unchanged.
    assert stamp < int(time.time()) - ws.TOLERANCE_S


def test_the_header_is_the_verifying_entries_joined_by_one_space():
    for vector in VECTORS["vectors"]:
        if vector["name"] == "tampered-body":
            # Its header is a valid signature over DIFFERENT bytes, so no entry over this vector's
            # own content can equal it -- that is the whole vector.
            assert vector["signature_covers_body_sha256_hex"] != vector["body_sha256_hex"]
            continue
        if vector["name"] == "stale-timestamp":
            # One correct entry, outside its window: the header is that entry.
            assert vector["signature_header"] == vector["secrets"][0]["signature"]
            continue
        expected = [e["signature"] for e in vector["secrets"] if e["verifies"]]
        assert vector["signature_header"] == " ".join(expected), vector["name"]


def test_the_file_covers_the_five_cases_the_contract_names():
    names = [v["name"] for v in VECTORS["vectors"]]
    for required in (
        "plain",
        "rotated-pair",
        "tampered-body",
        "stale-timestamp",
        "norwegian-body",
    ):
        assert required in names, required
    # Plus the two the plan's scenarios add: a retry sharing the id, and cross-destination isolation.
    assert _vector("retry-same-id-new-timestamp")["webhook_id"] == _vector("plain")["webhook_id"]
    assert _vector("retry-same-id-new-timestamp")["webhook_timestamp"] != _vector("plain")[
        "webhook_timestamp"
    ]
    assert _vector("retry-same-id-new-timestamp")["signature_header"] != _vector("plain")[
        "signature_header"
    ]
    isolation = _vector("cross-destination-isolation")
    assert {e["destination_id"] for e in isolation["secrets"]} == {DEST_A, DEST_B}
    assert [e["verifies"] for e in isolation["secrets"]] == [True, False]


def test_the_non_ascii_vector_actually_disagrees_about_length():
    """Otherwise it proves nothing.

    The first draft of this file wrote the Norwegian text as ``\\uXXXX`` escapes, so the body was
    pure ASCII and the two length fields were equal -- a vector that would have passed in both
    languages while testing nothing. Hence this check on the vector itself.
    """
    vector = _vector("norwegian-body")
    text = vector["body"]
    assert vector["body_utf8_byte_length"] > vector["body_utf16_code_unit_length"], (
        "the point of this vector is that the two encodings measure differently"
    )
    assert vector["body_utf16_code_unit_length"] == len(text.encode("utf-16-le")) // 2
    assert any(ord(ch) > 127 for ch in text)
    # Literal characters, NOT \uXXXX escapes -- which is what made the first draft ASCII.
    for expected in ("Å", "æ", "ø", "å", "→", "—", "–"):
        assert expected in text, expected
    assert "\\u" not in text, "escaped non-ASCII would make this vector pure ASCII again"
    # It is real JSON carrying a real notification, not a blob of accents.
    payload = json.loads(text)
    assert "innstilt" in payload["notification"]["body"]


def test_the_vectors_file_is_self_describing():
    """A TypeScript reader must need no Python. These are the fields U7 reads."""
    scheme = VECTORS["scheme"]
    assert scheme["algorithm"] == "HMAC-SHA256"
    assert scheme["algorithm_tag"] == ws.ALGORITHM_TAG
    assert scheme["timestamp_tolerance_seconds"] == ws.TOLERANCE_S
    assert scheme["header_names"] == {
        "id": ws.HEADER_ID,
        "timestamp": ws.HEADER_TIMESTAMP,
        "signature": ws.HEADER_SIGNATURE,
    }
    assert "base64" in scheme["signature_encoding"] and "padded" in scheme["signature_encoding"]
    assert "webhook_id" in scheme["signed_content"] and "body" in scheme["signed_content"]
    ts = VECTORS["how_to_consume"]["typescript_standardwebhooks"]
    assert ts["package"] == "standardwebhooks"
    # The two facts that make the fixed timestamps consumable from TypeScript at all: sign() takes a
    # Date, and verify() reads the wall clock.
    assert "new Date(" in ts["clock_free_check"] and "1000" in ts["clock_free_check"]
    assert "Date.now()" in ts["verify_reads_the_wall_clock"]
    # Nothing in the file may be phrased relative to "today": a vector generated on Tuesday must
    # still read correctly on Wednesday.
    for field in ("verify_at_unix_seconds", "mac_is_correct"):
        assert VECTORS["how_to_consume"][field].strip(), field
    derivation = VECTORS["derivation"]
    assert derivation["root_env_var"] == ws.SIGNING_ROOT_ENV
    for example in derivation["examples"]:
        assert example["material"] == ws.derivation_material(
            example["destination_id"], example["generation"]
        ).decode()
    for vector in VECTORS["vectors"]:
        assert vector["why"].strip(), vector["name"]
        assert vector["body_encoding"] == "utf-8", vector["name"]
        for entry in vector["secrets"]:
            assert set(entry) == {
                "destination_id",
                "generation",
                "secret",
                "signature",
                "verifies",
            }, vector["name"]


def test_every_secret_in_the_file_is_derivable_from_the_declared_test_root():
    """The checkable form of "this committed, world-readable file holds no real key".

    Every ``whsec_`` value in the file -- not only the ones the vector loop happens to visit --
    must be reproducible from the declared test root. A secret that is not is one derived from some
    other root, which is the only way real signing material could get in here.
    """
    root = VECTORS["derivation"]["test_root"].encode()
    assert "not-used-in-any-deployment" in root.decode()
    text = VECTORS_PATH.read_text()
    found = set(re.findall(r"whsec_[A-Za-z0-9+/=]+", text))
    assert len(found) >= 4, found
    derivable = {
        ws.derive_secret(destination, generation, root=root)
        for destination in (DEST_A, DEST_B)
        for generation in range(8)
    }
    assert found <= derivable, found - derivable


# --- Structure ----------------------------------------------------------------------------------


def test_the_comparison_is_over_fixed_length_bytes_and_not_over_the_encoded_entries():
    """A grep for `compare_digest` certifies the intent, not the property.

    ``hmac.compare_digest`` on two ``str`` arguments returns early on a length mismatch, and an
    attacker-supplied header entry can be any length while ours is always 47 characters -- so
    comparing the ENCODED entries would look constant-time and not be. The comparison is therefore
    over two 32-byte digests, which is also what the npm verifier does. What is asserted here is
    behavioural: every shape a hostile header can take must reach the comparison (or be skipped)
    rather than raise, and a good entry beside a bad one must still be found.
    """
    assert re.search(r"compare_digest\(\s*candidate,\s*expected\s*\)", MODULE_SOURCE)
    assert "b64decode(encoded, validate=True)" in MODULE_SOURCE
    assert not re.search(r"^\s*if\s+\w*mac\w*\s*==", MODULE_SOURCE, re.MULTILINE)

    secret = ws.derive_secret(DEST_A, 2, root=ROOT)
    good = ws.sign_entry(secret, webhook_id="dlv_1", timestamp=1789197300, body=BODY)
    assert len(good) == 47, good

    def check(header: str):
        return ws.verify(
            secret=secret,
            webhook_id="dlv_1",
            timestamp=1789197300,
            body=BODY,
            header=header,
            now=1789197300,
        )

    for hostile in (
        "v1,",  # empty signature
        "v1,!!!not-base64!!!",  # undecodable
        "v1,YQ==",  # decodes to ONE byte, not 32
        "v1," + "A" * 4000,  # far longer than ours
        "v1",  # no comma at all
        ",v1",
        "v1,x" * 50,
    ):
        result = check(hostile)
        assert not result and result.reason == "no_matching_signature", hostile
        # ...and a valid entry sitting beside it is still found.
        assert check(f"{hostile} {good}").ok is True, hostile
        assert check(f"{good} {hostile}").ok is True, hostile


def test_no_new_dependency_is_introduced():
    imports = set(re.findall(r"^(?:import|from)\s+([\w.]+)", MODULE_SOURCE, re.MULTILINE))
    for module in imports - {"__future__"}:
        root = module.split(".")[0]
        assert root in set(__import__("sys").stdlib_module_names), f"{module} is not stdlib"
    assert "cryptography" not in MODULE_SOURCE


def test_the_module_looks_nothing_up():
    # The destination record is U4's. A signer that read it would need the store on the delivery
    # path, and R18 is about that path not depending on anything slow.
    for forbidden in ("notification_store", "firestore", "requests", "urllib"):
        assert forbidden not in MODULE_SOURCE, forbidden


def _run() -> None:
    checks = sorted(
        (name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)
    )
    for name, fn in checks:
        _reset()
        try:
            fn()
        finally:
            _reset()
        print(f"  ok  {name}")
    print(f"\n{len(checks)} webhook-signing checks passed.")


if __name__ == "__main__":
    _run()
