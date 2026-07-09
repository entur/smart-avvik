"""Generate a VAPID keypair for Avviksvarsel Web Push.

Prints the three env vars to set on the deployment (and for local testing):

    VAPID_PRIVATE_KEY   raw base64url of the P-256 private scalar (loaded via Vapid02.from_raw)
    VAPID_PUBLIC_KEY    base64url uncompressed public point — the browser applicationServerKey
    VAPID_SUBJECT       mailto: contact for push services

Run:
    uv run --with pywebpush python disruptions/api/gen_vapid.py [mailto:you@entur.org]

Generate ONCE and keep the keypair stable: the public key is baked into every browser's push
subscription, so rotating it invalidates all existing subscriptions. Treat the private key as a
secret (it is the only thing needed to send push).
"""

import base64
import sys

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def main() -> None:
    subject = sys.argv[1] if len(sys.argv) > 1 else "mailto:kixavvik@entur.org"
    vapid = Vapid02()
    vapid.generate_keys()
    private_b64 = _b64url(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))
    public_b64 = _b64url(
        vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )
    print(f"VAPID_PRIVATE_KEY={private_b64}")
    print(f"VAPID_PUBLIC_KEY={public_b64}")
    print(f"VAPID_SUBJECT={subject}")


if __name__ == "__main__":
    main()
