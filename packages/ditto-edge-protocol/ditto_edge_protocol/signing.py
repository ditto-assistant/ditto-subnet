"""Device keys and request signatures for edge/v1.

A node's only credential is an Ed25519 device key it generates itself and never
uploads. Every request is signed over its method, path, a timestamp, a random
nonce and the SHA-256 of its body, so a captured request cannot be replayed,
re-pointed, or edited. Ed25519 signatures are deterministic (RFC 8032), which is
what lets the Desktop helper prove byte-for-byte agreement with this module
through the conformance vectors.

The device key is deliberately separate from any SN118 hotkey: a hotkey may
vouch for a device (:func:`binding_message`) so one wallet can stand behind one
Mac or a whole pool, but a node needs no wallet to run the shadow.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from ditto_edge_protocol.limits import NONCE_BYTES, REQUEST_SKEW_SECONDS

REQUEST_DOMAIN = "ditto-edge-request:v1"
BIND_DOMAIN = "ditto-edge-bind:v1"

HEADER_NODE = "X-Ditto-Edge-Node"
HEADER_TIMESTAMP = "X-Ditto-Edge-Timestamp"
HEADER_NONCE = "X-Ditto-Edge-Nonce"
HEADER_SIGNATURE = "X-Ditto-Edge-Signature"

_HEX_KEY = re.compile(r"[0-9a-f]{64}\Z")
_HEX_SIGNATURE = re.compile(r"[0-9a-f]{128}\Z")
_HEX_NONCE = re.compile(rf"[0-9a-f]{{{NONCE_BYTES * 2}}}\Z")
_PATH = re.compile(r"/[\x21-\x7e]*\Z")


class SignatureError(ValueError):
    """A request signature, nonce, timestamp, or key failed verification."""


def node_id_for(public_key_hex: str) -> str:
    """``edn_`` + the first 32 hex characters of SHA-256(raw public key)."""
    if _HEX_KEY.fullmatch(public_key_hex) is None:
        raise SignatureError("public key must be 32 bytes of lowercase hex")
    digest = hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()
    return f"edn_{digest[:32]}"


def canonical_request(
    *, method: str, path: str, timestamp: int, nonce_hex: str, body: bytes
) -> bytes:
    """The exact bytes a request signature covers.

    ``path`` is the request path as sent, without a query string; edge/v1
    endpoints take none, so a query is refused rather than left unsigned.
    """
    if _PATH.fullmatch(path) is None or "?" in path:
        raise SignatureError("path must be an absolute path without a query")
    if _HEX_NONCE.fullmatch(nonce_hex) is None:
        raise SignatureError(f"nonce must be {NONCE_BYTES} bytes of lowercase hex")
    if timestamp < 0:
        raise SignatureError("timestamp must be a non-negative unix time")
    lines = (
        REQUEST_DOMAIN,
        method.upper(),
        path,
        str(timestamp),
        nonce_hex,
        hashlib.sha256(body).hexdigest(),
    )
    return "\n".join(lines).encode("ascii")


def new_nonce() -> str:
    return secrets.token_hex(NONCE_BYTES)


@dataclass(frozen=True)
class SignedHeaders:
    node_id: str
    timestamp: int
    nonce_hex: str
    signature_hex: str

    def as_dict(self) -> dict[str, str]:
        return {
            HEADER_NODE: self.node_id,
            HEADER_TIMESTAMP: str(self.timestamp),
            HEADER_NONCE: self.nonce_hex,
            HEADER_SIGNATURE: self.signature_hex,
        }


class DeviceKey:
    """An Ed25519 device key held only on the node."""

    def __init__(self, private_key: Ed25519PrivateKey) -> None:
        self._key = private_key
        raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.public_key_hex = raw.hex()
        self.node_id = node_id_for(self.public_key_hex)

    @classmethod
    def generate(cls) -> DeviceKey:
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> DeviceKey:
        """Load a key from its 32-byte seed (the RFC 8032 private key)."""
        if len(seed) != 32:
            raise SignatureError("an Ed25519 seed is exactly 32 bytes")
        return cls(Ed25519PrivateKey.from_private_bytes(seed))

    def seed(self) -> bytes:
        return self._key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())

    @classmethod
    def load(cls, path: Path) -> DeviceKey:
        """Read the hex seed at ``path``; a malformed file is a ``SignatureError``."""
        try:
            text = path.read_text(encoding="ascii").strip()
        except UnicodeDecodeError as exc:
            raise SignatureError(f"{path} does not hold a 32-byte hex seed") from exc
        if _HEX_KEY.fullmatch(text) is None:
            raise SignatureError(f"{path} does not hold a 32-byte hex seed")
        return cls.from_seed(bytes.fromhex(text))

    @classmethod
    def load_or_create(cls, path: Path) -> DeviceKey:
        """Read the seed at ``path``, creating it (mode 0600) if absent.

        The file holds the raw seed as hex and never leaves the machine. A new
        seed is written and fsynced to a private temporary file first, then
        published with a hard link that never overwrites. A crash therefore
        never leaves a half-written seed, and when two processes race to
        create one, both end up with the winner's key.
        """
        if path.exists():
            return cls.load(path)
        key = cls.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(key.seed().hex() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temp, path)
            except FileExistsError:
                return cls.load(path)
        finally:
            temp.unlink(missing_ok=True)
        return key

    def sign(self, message: bytes) -> str:
        return self._key.sign(message).hex()

    def sign_request(
        self,
        *,
        method: str,
        path: str,
        body: bytes,
        timestamp: int,
        nonce_hex: str | None = None,
    ) -> SignedHeaders:
        nonce = nonce_hex if nonce_hex is not None else new_nonce()
        message = canonical_request(
            method=method, path=path, timestamp=timestamp, nonce_hex=nonce, body=body
        )
        return SignedHeaders(
            node_id=self.node_id,
            timestamp=timestamp,
            nonce_hex=nonce,
            signature_hex=self.sign(message),
        )


def verify_request(
    *,
    public_key_hex: str,
    signature_hex: str,
    method: str,
    path: str,
    timestamp: int,
    nonce_hex: str,
    body: bytes,
    now: int,
    skew_seconds: int = REQUEST_SKEW_SECONDS,
) -> None:
    """Raise :class:`SignatureError` unless the request is fresh and signed.

    Freshness is checked here; single use of the nonce is the caller's job and
    must be an atomic insert into durable storage, never a read-then-write.
    """
    if abs(now - timestamp) > skew_seconds:
        raise SignatureError("request timestamp is outside the allowed clock skew")
    if _HEX_KEY.fullmatch(public_key_hex) is None:
        raise SignatureError("public key must be 32 bytes of lowercase hex")
    if _HEX_SIGNATURE.fullmatch(signature_hex) is None:
        raise SignatureError("signature must be 64 bytes of lowercase hex")
    message = canonical_request(
        method=method, path=path, timestamp=timestamp, nonce_hex=nonce_hex, body=body
    )
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(
            bytes.fromhex(signature_hex), message
        )
    except InvalidSignature as exc:
        raise SignatureError("request signature does not verify") from exc


def binding_message(*, node_id: str, hotkey_ss58: str, issued_at: int) -> bytes:
    """What an SN118 hotkey signs (sr25519) to vouch for a device key."""
    if not node_id.startswith("edn_") or ":" in hotkey_ss58:
        raise SignatureError("malformed node id or hotkey")
    if not node_id.isascii() or not hotkey_ss58.isascii():
        raise SignatureError("node id and hotkey must be ascii")
    return f"{BIND_DOMAIN}:{node_id}:{hotkey_ss58}:{issued_at}".encode("ascii")
