"""Ed25519 signing/verification + canonical request serialization.

Spec reference: SPEC.md §2.3
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

KEY_PREFIX: Final = "ed25519:"
TIMESTAMP_TOLERANCE_SECONDS: Final = 300
NONCE_TTL_SECONDS: Final = 600


@dataclass(frozen=True)
class KeyPair:
    """In-memory key pair; private key is bytes (32 raw seed)."""

    public_b64: str  # base64 of 32-byte public key, no prefix
    private_raw: bytes  # 32 raw seed bytes

    @classmethod
    def generate(cls) -> "KeyPair":
        priv = Ed25519PrivateKey.generate()
        pub = priv.public_key()
        return cls(
            public_b64=base64.b64encode(pub.public_bytes_raw()).decode("ascii"),
            private_raw=priv.private_bytes_raw(),
        )

    @classmethod
    def from_private_raw(cls, raw: bytes) -> "KeyPair":
        priv = Ed25519PrivateKey.from_private_bytes(raw)
        return cls(
            public_b64=base64.b64encode(priv.public_key().public_bytes_raw()).decode("ascii"),
            private_raw=raw,
        )

    @property
    def public_key_string(self) -> str:
        return KEY_PREFIX + self.public_b64

    def sign(self, message: bytes) -> str:
        priv = Ed25519PrivateKey.from_private_bytes(self.private_raw)
        sig = priv.sign(message)
        return base64.b64encode(sig).decode("ascii")

    def public_key_obj(self) -> Ed25519PublicKey:
        return Ed25519PrivateKey.from_private_bytes(self.private_raw).public_key()


def parse_public_key(public_key_string: str) -> Ed25519PublicKey:
    """Parse "ed25519:<b64>" → Ed25519PublicKey. Raises ValueError on bad format."""
    if not public_key_string.startswith(KEY_PREFIX):
        raise ValueError(f"public_key must start with {KEY_PREFIX!r}")
    raw_b64 = public_key_string[len(KEY_PREFIX) :]
    try:
        raw = base64.b64decode(raw_b64, validate=True)
    except Exception as e:
        raise ValueError(f"public_key base64 decode failed: {e}") from e
    if len(raw) != 32:
        raise ValueError(f"public_key must decode to 32 bytes, got {len(raw)}")
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_public_key(public_key_string: str) -> bool:
    try:
        parse_public_key(public_key_string)
    except ValueError:
        return False
    return True


def canonical_request(timestamp: int, method: str, path: str, body: bytes) -> bytes:
    """Build the bytes that the client signs and the server verifies.

    Layout (LF separated, no trailing newline):
        {TIMESTAMP}\\n{METHOD}\\n{REQUEST_PATH}\\n{HEX_LOWER(sha256(BODY_BYTES))}
    """
    body_digest = hashlib.sha256(body).hexdigest()
    canonical = f"{timestamp}\n{method.upper()}\n{path}\n{body_digest}"
    return canonical.encode("utf-8")


def verify_request(
    public_key_string: str,
    signature_b64: str,
    timestamp: int,
    method: str,
    path: str,
    body: bytes,
    now: int,
) -> None:
    """Verify signed request. Raises ValueError on any failure."""
    if abs(now - timestamp) > TIMESTAMP_TOLERANCE_SECONDS:
        raise ValueError(
            f"timestamp out of range: now={now}, ts={timestamp}, "
            f"tolerance=±{TIMESTAMP_TOLERANCE_SECONDS}s"
        )

    try:
        pub = parse_public_key(public_key_string)
    except ValueError as e:
        raise ValueError(f"bad public_key: {e}") from e

    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except Exception as e:
        raise ValueError(f"bad signature base64: {e}") from e

    msg = canonical_request(timestamp, method, path, body)
    try:
        pub.verify(sig, msg)
    except InvalidSignature as e:
        raise ValueError("signature does not verify") from e


# --- helpers used by examples ------------------------------------------------


def keypair_to_pem_pair(kp: KeyPair) -> tuple[bytes, bytes]:
    """Convert raw seed pair to PEM for backup/display."""
    priv = Ed25519PrivateKey.from_private_bytes(kp.private_raw)
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = kp.public_key_obj().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, pub_pem
