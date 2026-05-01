"""One-shot RSA-2048 keypair generator for the OAuth JWT-bearer flow.

Outputs the PKCS8 PEM private key to a file (default
``./agent-service-private-key.pem``) and prints the JWK form of the public
key to stdout. The private PEM goes into Railway's ``OAUTH_PRIVATE_KEY_PEM``
env var; the JWK is what ``register_oauth_client.py`` posts to OpenEMR's
``/oauth2/default/registration`` endpoint as ``jwks.keys[0]``.

A run looks like::

    uv run python scripts/generate_client_keypair.py \\
        --kid agent-service-2026-05 \\
        --out agent-service-private-key.pem \\
        > public-jwk.json

The ``kid`` is shared between the JWK posted at registration and the
``OAUTH_KEY_ID`` env var; ``client_assertion.py`` embeds it in every minted
JWT header so OpenEMR can resolve the public key (RsaSha384Signer.php:106).
Pick something stable and human-recognizable so log lines are diagnosable
during incident response — but unique enough that a key rotation gets a new
``kid``.

The private key is written with mode 0600 to make it harder to commit by
accident; ``.gitignore`` should still exclude ``*.pem`` to be safe.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

KEY_SIZE = 2048
PUBLIC_EXPONENT = 65537


def _b64url_uint(value: int) -> str:
    """Encode a non-negative integer as base64url with no padding (RFC 7518 §6.3)."""
    if value < 0:
        raise ValueError("value must be non-negative")
    byte_length = (value.bit_length() + 7) // 8 or 1
    encoded = base64.urlsafe_b64encode(value.to_bytes(byte_length, "big"))
    return encoded.rstrip(b"=").decode("ascii")


def public_key_to_jwk(public_key: RSAPublicKey, kid: str) -> dict[str, str]:
    """Return the JWK form OpenEMR expects in the registration ``jwks`` payload.

    Fields are in the order the JWK spec lists them so a human reading the
    JSON has an easier time spotting drift; OpenEMR ignores order.
    """
    numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "alg": "RS384",
        "use": "sig",
        "kid": kid,
        "n": _b64url_uint(numbers.n),
        "e": _b64url_uint(numbers.e),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--kid",
        required=True,
        help="Stable key id; same value used for OAUTH_KEY_ID and the registered JWK.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("agent-service-private-key.pem"),
        help="Where to write the PKCS8 PEM private key (default: ./agent-service-private-key.pem).",
    )
    args = parser.parse_args(argv)

    if args.out.exists():
        print(
            f"refusing to overwrite existing key file: {args.out}",
            file=sys.stderr,
        )
        return 2

    private_key = rsa.generate_private_key(
        public_exponent=PUBLIC_EXPONENT, key_size=KEY_SIZE
    )
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    # Write 0600 to keep the file out of group/world reads even if the
    # umask is permissive. Doesn't help against a committed .pem — still
    # add it to .gitignore.
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, pem)
    finally:
        os.close(fd)

    jwk = public_key_to_jwk(private_key.public_key(), args.kid)
    json.dump(jwk, sys.stdout, indent=2)
    sys.stdout.write("\n")

    print(
        f"\nWrote private key to {args.out} (mode 0600).",
        file=sys.stderr,
    )
    print(
        "Set OAUTH_PRIVATE_KEY_PEM to the file's contents on the deployed agent-service.",
        file=sys.stderr,
    )
    print(
        f"Set OAUTH_KEY_ID to {args.kid!r}.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
