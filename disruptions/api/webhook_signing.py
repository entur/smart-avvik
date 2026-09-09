"""Standard Webhooks signing for the webhook channel (KTD7, R10).

HMAC-SHA256 over ``webhook-id.webhook-timestamp.<raw body>``, emitted as space-delimited
``v1,<base64>`` entries in one ``webhook-signature`` header. The shape is the Standard Webhooks
one deliberately: the client verifies with the ``standardwebhooks`` npm package rather than
hand-rolled HMAC (KTD11), so every framing detail here is a wire contract with a library we do not
own. The four that are easy to get wrong, and that
``contracts/notifications/v1/signing-vectors.json`` pins in both languages:

* **The body bytes are signed exactly as sent.** The caller stores the serialized bytes once and
  passes the same object here and to the transport. Re-serializing between signing and sending is
  the canonical webhook bug -- every provider's documentation carries the warning, and the
  signature then covers a document the receiver did not get.
* **``webhook-id`` is stable across attempts; the timestamp and signature are regenerated per
  attempt.** A retry that reuses the first attempt's timestamp lands outside the receiver's
  ~5-minute replay window, so a *correct* receiver drops our own retry as an attack. This is why
  :func:`sign` takes the id from the caller (it is the durable delivery id) and mints the timestamp
  itself.
* **Standard base64, padded, for both the secret body and the signature.** npm's
  ``digest('base64')`` emits exactly that, and U7's clock-free check compares our header entry to
  its ``sign()`` output byte for byte. Do NOT reach for :func:`base64.urlsafe_b64encode` or strip
  the padding the way :mod:`sso` does for its cookie -- that is a different contract with only one
  implementation.
* **The signed content is a UTF-8 byte string.** npm hashes ``update(str)`` as UTF-8, so a body
  carrying Norwegian text agrees only if both sides encode it the same way. The wording IS
  Norwegian, so a UTF-8/UTF-16 length disagreement is a live failure mode rather than a
  hypothetical -- hence the non-ASCII vector, and hence the vectors file stating byte lengths.

**Per-destination derivation.** Each destination's secret is HMAC-SHA256 of a root secret over a
length-framed ``(destination_id, generation)`` material, so the store holds no signing material at
all and one client's leaked receiver secret forges nothing for another. The framing is
length-prefixed rather than delimiter-joined because a destination id is an operator-supplied
string: ``("a", 11)`` and ``("a1", 1)`` must not produce one message, and a bare separator only
holds while no id contains that separator. Same idea as :mod:`sso`'s purpose tag one level up --
one key, disjoint messages -- and the tag is here too, so a future second use of the same root
cannot collide with this one.

**Two generations are live at once.** :func:`sign` emits one entry per generation it is given, so a
rotation overlaps instead of cutting over on a flag day, and each generation is revocable alone by
dropping it from the destination record (U4 owns that record; this module looks nothing up). The
alternative -- retrofitting multi-signature later -- means either a flag-day cutover or building
this during an active leak.

This module holds no destination state, opens no socket and reads no store. It is the bytes and the
headers, and nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---- Wire constants ----------------------------------------------------------------------------
#
# Header names are lowercase because Standard Webhooks specifies them lowercase and HTTP header
# names are case-insensitive on the wire; the npm verifier indexes its headers object by these
# exact strings, so a receiver behind a proxy that does not normalize case would miss a
# `Webhook-Id`.

HEADER_ID = "webhook-id"
HEADER_TIMESTAMP = "webhook-timestamp"
HEADER_SIGNATURE = "webhook-signature"

#: The algorithm tag on each header entry. `v1` is symmetric HMAC-SHA256; `v1a` in the same spec is
#: the ED25519 *algorithm*, not a rotation generation -- rotation is several full `v1` entries.
ALGORITHM_TAG = "v1"

#: What the receiver's replay window is, and therefore what we publish. Both the npm verifier and
#: :func:`verify` use it. It is why the timestamp is regenerated per attempt.
TOLERANCE_S = 300

#: The `whsec_` prefix is the spec's, and a recognizable prefix is what lets secret scanning find a
#: leaked key with ~0.5 % false positives (GitHub's own number) instead of drowning in base64.
SECRET_PREFIX = "whsec_"

#: 32 raw bytes: one full HMAC-SHA256 output, so derivation neither truncates nor stretches.
SECRET_BYTES = 32

# ---- Root secret -------------------------------------------------------------------------------
#
# The name is fixed by the Helm chart's commented `client:` secret group, which also records why
# the value is written with `printf '%s'`: a trailing newline in a 49-byte token silently dropped
# eight of this app's feedback submissions, and every hash check missed it because they were all
# written `| tr -d '\n' | shasum`, which deletes the byte at fault. Here the same byte would
# re-derive EVERY destination's secret, so every client's verification fails at once and nothing
# on our side reports a configuration fault -- just a wall of rejected deliveries. So the read
# strips and warns once per process, exactly as `feedback._env` does.

SIGNING_ROOT_ENV = "WEBHOOK_SIGNING_ROOT"

#: A root shorter than one HMAC block's worth of entropy is a provisioning mistake, not a policy
#: choice; refusing it is the only moment anybody can notice.
MIN_ROOT_BYTES = 32

_PURPOSE = b"kix.webhook.signing.v1"

_WHITESPACE_REPORTED: set[str] = set()


class SigningNotConfigured(RuntimeError):
    """No usable root secret. Raised loudly rather than degrading to an unsigned request.

    Fail closed and fail *visible*: a client endpoint that accepts an unsigned or wrongly-signed
    request is a security hole on their side, and one that rejects it produces a retry storm on
    ours. Neither is a state to reach by omission.
    """


def _env(name: str) -> str:
    """The variable, stripped, warning once per process when stripping changed it.

    `feedback._env`'s shape and `feedback._env`'s reason. Stripping rather than rejecting is
    deliberate: whitespace is a transport artefact of how the payload was written, not a wrong
    value, and refusing it would trade a silent wrong-key for a silent absence. The WARNING is what
    keeps the provisioning defect findable.
    """
    raw = os.environ.get(name) or ""
    value = raw.strip()
    if value != raw and name not in _WHITESPACE_REPORTED:
        _WHITESPACE_REPORTED.add(name)
        logger.warning(
            "%s carried surrounding whitespace and was stripped before use; fix it at the source "
            "(a Secret Manager payload written with `echo` rather than `printf '%%s'` is the usual "
            "cause). Unstripped, it re-derives every destination's signing secret and every "
            "client's verification fails at once.",
            name,
            extra={"fields": {"event": "webhook_signing_config", "variable": name}},
        )
    return value


#: The root `contracts/notifications/v1/signing-vectors.json` publishes so a client can reproduce a
#: signature by hand. It is 48 bytes, so the length test alone ADMITTED it -- `configured()` answered
#: True, `/destinations/current` reported ready, `/test-events` answered 200, and every delivery
#: signed and verified normally, while every `whsec_` secret that root derives is committed in the
#: same file and vendored again under `integrations/`. The signature is the only authentication a
#: client receiver has, so a deployment carrying this value would accept injected cancellations and
#: withdrawals about real trains with nothing on our side reporting a fault.
#:
#: A plausible slip rather than a hypothetical: the runbook tells an operator to export
#: `WEBHOOK_SIGNING_ROOT` by hand for `show-signing-secret`, which is exactly the moment a documented
#: example gets pasted. Restated here rather than read from the contract at runtime, because
#: `contracts/` is not in the image; `test_webhook_signing` reads the literal OUT of the JSON and
#: compares, so the two cannot drift.
_PUBLISHED_TEST_ROOT = b"kix-test-signing-root-not-used-in-any-deployment"


def configured() -> bool:
    """Is there a usable root secret? A cheap probe that never raises and never logs the value."""
    value = _env(SIGNING_ROOT_ENV).encode()
    return len(value) >= MIN_ROOT_BYTES and value != _PUBLISHED_TEST_ROOT


def signing_root() -> bytes:
    """The root secret, read lazily.

    Lazily, and never at import time: a deployment with the channel switched off must import this
    module without needing the secret, and `main` imports every module it might route to. An
    import-time read would also freeze the value against a restart-free secret rotation.
    """
    value = _env(SIGNING_ROOT_ENV).encode()
    if value == _PUBLISHED_TEST_ROOT:
        # Named separately from "unset or too short", because the two need different actions and the
        # wrong message sends an operator hunting for a missing secret they have plainly set.
        raise SigningNotConfigured(
            f"{SIGNING_ROOT_ENV} is set to the example root published in "
            "contracts/notifications/v1/signing-vectors.json. Every secret it derives is committed "
            "in that file, so it signs forgeable webhooks; provision a real root in Secret Manager."
        )
    if len(value) < MIN_ROOT_BYTES:
        raise SigningNotConfigured(
            f"{SIGNING_ROOT_ENV} is unset or shorter than {MIN_ROOT_BYTES} bytes; webhook "
            "signing cannot proceed. Provision it in Secret Manager and uncomment the chart's "
            "`client:` secret group."
        )
    return value


def reset_for_tests() -> None:
    """Forget the once-per-process whitespace warning so a suite can assert it fires."""
    _WHITESPACE_REPORTED.clear()


# ---- Derivation --------------------------------------------------------------------------------


def derivation_material(destination_id: str, generation: int) -> bytes:
    """The exact preimage :func:`derive_secret` signs. Public so the vectors can state it.

    Length-framed on the destination id, so no pair of ``(destination_id, generation)`` values can
    share a preimage. ``generation`` needs no length because it is decimal digits and terminates the
    message.
    """
    if not destination_id:
        raise ValueError("destination_id must not be empty")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError("generation must be a non-negative int")
    encoded = destination_id.encode("utf-8")
    return b"|".join(
        (_PURPOSE, str(len(encoded)).encode("ascii"), encoded, str(generation).encode("ascii"))
    )


def derive_secret(destination_id: str, generation: int, *, root: bytes | None = None) -> str:
    """This destination's signing secret for one generation, as ``whsec_<base64>``.

    Deterministic, so no secret is ever stored: U4's destination record carries which generations
    are live and nothing else, and rotating the root rotates every destination at once -- which is
    precisely why generations overlap.

    ``root`` is injectable for the vectors and for tests. Production passes nothing and the
    environment decides.
    """
    key = root if root is not None else signing_root()
    raw = hmac.new(key, derivation_material(destination_id, generation), hashlib.sha256).digest()
    return SECRET_PREFIX + base64.b64encode(raw).decode("ascii")


def secret_key(secret: str) -> bytes:
    """The raw HMAC key inside a ``whsec_`` secret.

    The prefix is stripped and the remainder base64-decoded, which is what the npm package does to
    the same string -- so a secret handed to a client works in their verifier unchanged.
    """
    if not secret.startswith(SECRET_PREFIX):
        raise ValueError(f"a signing secret must start with {SECRET_PREFIX!r}")
    body = secret[len(SECRET_PREFIX) :]
    try:
        key = base64.b64decode(body, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("a signing secret's body must be standard padded base64") from exc
    if not key:
        raise ValueError("a signing secret's body must decode to at least one byte")
    return key


# ---- Signing -----------------------------------------------------------------------------------


def signed_content(webhook_id: str, timestamp: int, body: bytes) -> bytes:
    """``webhook-id.webhook-timestamp.<raw body>``, as bytes.

    The id is refused if it carries a ``.`` or whitespace. Nothing in the spec forbids it, and the
    receiver would still verify -- but the framing would be ambiguous, and we are the side that
    chooses ids, so the cheap guarantee is worth having. In practice the id is the delivery id.
    """
    if not webhook_id:
        raise ValueError("webhook_id must not be empty")
    if "." in webhook_id or any(ch.isspace() for ch in webhook_id):
        raise ValueError("webhook_id must contain no '.' and no whitespace: the framing needs it")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        # A str here is the mistake worth catching: `str(int(time.time()))` would sign the same
        # digits and produce a valid signature, so nothing would fail -- until a caller passed a
        # formatted timestamp and the two sides framed it differently.
        raise TypeError("timestamp must be an int of whole seconds since the epoch")
    return f"{webhook_id}.{timestamp}.".encode() + body


def _digest(secret: str, *, webhook_id: str, timestamp: int, body: bytes) -> bytes:
    """The 32 raw HMAC bytes. Separate from :func:`sign_entry` so verification can compare BYTES.

    Comparing the encoded entries instead would look constant-time and not be: ``compare_digest``
    on two ``str`` arguments returns early on a length mismatch, and an attacker-supplied entry can
    be any length while ours is always 47 characters. Decoding both sides to a fixed 32 bytes is
    also exactly what the npm verifier does (``timingSafeEqual`` over two base64-decoded buffers),
    so the two implementations agree on the comparison as well as on the arithmetic.
    """
    return hmac.new(
        secret_key(secret), signed_content(webhook_id, timestamp, body), hashlib.sha256
    ).digest()


def sign_entry(secret: str, *, webhook_id: str, timestamp: int, body: bytes) -> str:
    """One ``v1,<base64>`` header entry under one secret."""
    digest = _digest(secret, webhook_id=webhook_id, timestamp=timestamp, body=body)
    return f"{ALGORITHM_TAG},{base64.b64encode(digest).decode('ascii')}"


def signature_header(
    secrets: Sequence[str], *, webhook_id: str, timestamp: int, body: bytes
) -> str:
    """The whole ``webhook-signature`` value: one entry per secret, space-delimited.

    Order is the caller's -- active generation first by convention, so a receiver that stops at the
    first match does the least work in the common case. Order carries no meaning: the npm verifier
    accepts a match on any entry, which is what makes each generation independently revocable.
    """
    if not secrets:
        raise ValueError("at least one secret is needed to sign")
    return " ".join(
        sign_entry(secret, webhook_id=webhook_id, timestamp=timestamp, body=body)
        for secret in secrets
    )


@dataclass(frozen=True)
class SignedRequest:
    """What to send, and what the caller must persist.

    ``webhook_id`` and ``body`` are the caller's own objects handed back unchanged, so there is one
    obvious way to send exactly what was signed. ``timestamp`` is per attempt and deliberately NOT
    something to persist: the next attempt mints a new one.
    """

    webhook_id: str
    timestamp: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


def sign(
    *,
    destination_id: str,
    generations: Sequence[int],
    webhook_id: str,
    body: bytes,
    timestamp: int | None = None,
    root: bytes | None = None,
) -> SignedRequest:
    """Sign one attempt for one destination.

    ``webhook_id`` is stable across attempts and is the delivery id: the receiver dedupes on it, and
    minting a fresh one per attempt is the one thing every provider studied agrees not to do.
    ``timestamp`` defaults to now and must be re-minted per attempt -- pass it only to reproduce a
    vector.

    ``generations`` is the destination's live set (active, then retiring). This function looks
    nothing up: the destination record is U4's, and a signer that read it would need the store on
    the delivery path.
    """
    if not generations:
        raise ValueError("a destination must have at least one live generation to sign under")
    key = root if root is not None else signing_root()
    stamp = int(time.time()) if timestamp is None else timestamp
    secrets = [derive_secret(destination_id, generation, root=key) for generation in generations]
    return SignedRequest(
        webhook_id=webhook_id,
        timestamp=stamp,
        body=body,
        headers={
            HEADER_ID: webhook_id,
            HEADER_TIMESTAMP: str(stamp),
            HEADER_SIGNATURE: signature_header(
                secrets, webhook_id=webhook_id, timestamp=stamp, body=body
            ),
        },
    )


# ---- Verification ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verification:
    """Why a verification failed, not just that it did.

    A boolean cannot tell a stale timestamp from a wrong key, and those are different faults: the
    first is our retry regenerating its timestamp incorrectly, the second is a rotation gone wrong.
    The vectors carry the reason so the two languages agree about which one each vector is.
    """

    ok: bool
    reason: str | None = None

    def __bool__(self) -> bool:
        return self.ok


def verify(
    *,
    secret: str,
    webhook_id: str,
    timestamp: str | int,
    body: bytes,
    header: str,
    now: int | None = None,
    tolerance_s: int = TOLERANCE_S,
) -> Verification:
    """Verify one header under one secret, the way a receiver would.

    Ours to *own*, not ours to ship: the client verifies with the npm package. It exists so the
    vectors can be asserted against the implementation that produced them, and so a rotation can be
    checked from this side.

    ``now`` is injectable, which the npm package's ``verify`` is not -- it reads the wall clock. That
    asymmetry is why the vectors are cross-checked through ``sign()`` rather than ``verify()``; see
    the vectors file's ``how_to_consume``.
    """
    try:
        stamp = int(timestamp)
    except (TypeError, ValueError):
        return Verification(False, "malformed_timestamp")
    instant = int(time.time()) if now is None else now
    if abs(instant - stamp) > tolerance_s:
        return Verification(False, "stale_timestamp")
    try:
        expected = _digest(secret, webhook_id=webhook_id, timestamp=stamp, body=body)
    except ValueError:
        return Verification(False, "malformed_secret")
    entries = [entry for entry in header.split(" ") if entry]
    if not entries:
        return Verification(False, "malformed_header")
    matched = False
    for entry in entries:
        tag, separator, encoded = entry.partition(",")
        if tag != ALGORITHM_TAG or not separator:
            continue
        try:
            candidate = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            # A malformed entry is skipped, not raised on: a header is attacker-controlled input,
            # and one bad entry beside a good one must not stop the good one being found.
            continue
        # Fixed-length raw digests, and compared for EVERY candidate rather than breaking on the
        # first match: the loop is at most a handful of entries, and a short-circuit leaks which
        # generation matched through timing. Cheap here, so there is no reason to accept the leak.
        matched = hmac.compare_digest(candidate, expected) or matched
    return Verification(True) if matched else Verification(False, "no_matching_signature")


__all__ = [
    "ALGORITHM_TAG",
    "HEADER_ID",
    "HEADER_SIGNATURE",
    "HEADER_TIMESTAMP",
    "MIN_ROOT_BYTES",
    "SECRET_BYTES",
    "SECRET_PREFIX",
    "SIGNING_ROOT_ENV",
    "TOLERANCE_S",
    "SignedRequest",
    "SigningNotConfigured",
    "Verification",
    "configured",
    "derivation_material",
    "derive_secret",
    "reset_for_tests",
    "secret_key",
    "sign",
    "sign_entry",
    "signature_header",
    "signed_content",
    "signing_root",
    "verify",
]
