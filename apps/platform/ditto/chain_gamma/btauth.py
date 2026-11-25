"""btauth/1: verify a hotkey-signed request to a chain gamma service.

The wire format is the signed-requests guide's, and the order follows the
reference ``bittensor.http_auth.verify`` (subtensor v493) with the standard's
extra step for chain-protected senders:

1. headers present and well formed, and the scheme is supported;
2. ``X-Bittensor-Receiver`` is one of the service's publisher hotkeys;
3. the nonce is at most ``max_age`` old and at most ``allowed_skew`` ahead;
4. an sr25519 or ed25519 sender the chain protects (``HashedAccounts``
   entries) is refused, so a forged header cannot weaken that account;
5. the signature verifies over the canonical payload under that scheme only;
6. ``(hotkey, nonce)`` is new. It is recorded only after the signature
   verifies, so unauthenticated traffic cannot poison the replay store.

V1 verifies sr25519 and ed25519. The ``hashed`` and ``hashed-ed25519`` schemes
need the RaoFoundation ``bittensor`` 11 SDK and are refused with 401 until it is
adopted (design question 19). Every failure is an :class:`AuthError` (HTTP 401).
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Protocol

import bittensor
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROTOCOL = "btauth/1"
VERSION = "1"

HEADER_VERSION = "X-Bittensor-Version"
HEADER_HOTKEY = "X-Bittensor-Hotkey"
HEADER_RECEIVER = "X-Bittensor-Receiver"
HEADER_NONCE = "X-Bittensor-Nonce"
HEADER_CRYPTO = "X-Bittensor-Crypto"
HEADER_SIGNATURE = "X-Bittensor-Signature"

SCHEME_SR25519 = "sr25519"
SCHEME_ED25519 = "ed25519"
SUPPORTED_SCHEMES = frozenset({SCHEME_SR25519, SCHEME_ED25519})
KNOWN_SCHEMES = SUPPORTED_SCHEMES | {"hashed", "hashed-ed25519"}

DEFAULT_MAX_AGE_S = 10.0
DEFAULT_ALLOWED_SKEW_S = 2.0
_NS = 1_000_000_000
_SIGNATURE_BYTES = 64


class AuthError(Exception):
    """A request failed btauth/1 verification; answer 401 with ``code``."""

    code = "unauthorized"
    http_status = 401


class MalformedAuth(AuthError):
    code = "malformed_auth"


class UnsupportedScheme(AuthError):
    code = "unsupported_scheme"


class WrongReceiver(AuthError):
    code = "wrong_receiver"


class StaleRequest(AuthError):
    code = "stale_request"


class ProtectedSender(AuthError):
    code = "protected_sender"


class BadSignature(AuthError):
    code = "bad_signature"


class ReplayedRequest(AuthError):
    code = "replayed_request"


class NonceStore(Protocol):
    """Shared replay state. ``True`` records a new pair, ``False`` means seen.

    Entries must outlive ``max_age + allowed_skew``. Production uses one store
    for every process (a Postgres unique insert, as ``validator_request_nonces``).
    """

    async def check_and_store(self, hotkey: str, nonce_ns: int) -> bool: ...


ProtectedSenderCheck = Callable[[str], Awaitable[bool]]
"""``True`` when the finalized chain protects the sender from sr25519/ed25519."""


@dataclass(frozen=True)
class VerifiedCaller:
    hotkey: str
    receiver: str
    nonce_ns: int
    scheme: str


def build_payload(
    *,
    scheme: str,
    method: str,
    target: str,
    body: bytes,
    nonce_ns: int,
    sender: str,
    receiver: str,
) -> bytes:
    """The exact signed bytes: eight lines joined by ``\\n``, no trailing newline.

    ``target`` is the percent-encoded path plus ``?query`` exactly as received.
    """
    return "\n".join(
        (
            PROTOCOL,
            scheme,
            method.upper(),
            target,
            hashlib.sha256(body).hexdigest(),
            str(nonce_ns),
            sender,
            receiver,
        )
    ).encode()


def _verify_signature(scheme: str, sender: str, payload: bytes, sig: bytes) -> bool:
    try:
        keypair = bittensor.Keypair(ss58_address=sender)
    except (TypeError, ValueError):
        raise MalformedAuth("invalid sender hotkey address") from None
    if scheme == SCHEME_SR25519:
        # The substrate signing context; also accepts a <Bytes>-wrapped payload.
        try:
            return bool(keypair.verify(payload, sig))
        except (TypeError, ValueError):
            raise MalformedAuth("invalid signature encoding") from None
    public_key = keypair.public_key
    if public_key is None:
        raise MalformedAuth("invalid sender hotkey address")
    public = Ed25519PublicKey.from_public_bytes(bytes(public_key))
    for message in (payload, b"<Bytes>" + payload + b"</Bytes>"):
        try:
            public.verify(sig, message)
            return True
        except InvalidSignature:
            continue
    return False


async def verify_request(
    headers: Mapping[str, str],
    body: bytes,
    *,
    method: str,
    target: str,
    receivers: Collection[str],
    nonce_store: NonceStore,
    is_protected_sender: ProtectedSenderCheck,
    now_ns: int | None = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    allowed_skew_s: float = DEFAULT_ALLOWED_SKEW_S,
) -> VerifiedCaller:
    """Authenticate one request or raise :class:`AuthError`.

    ``body`` is the raw received bytes, hashed before any parsing. ``receivers``
    are the service's publisher hotkeys. Nothing integrity-relevant is taken
    from the headers unverified.
    """
    lowered = {key.lower(): value for key, value in headers.items()}

    if lowered.get(HEADER_VERSION.lower()) != VERSION:
        raise MalformedAuth(f"missing or unsupported {HEADER_VERSION}")
    sender = lowered.get(HEADER_HOTKEY.lower())
    if not sender:
        raise MalformedAuth(f"missing {HEADER_HOTKEY}")
    raw_nonce = lowered.get(HEADER_NONCE.lower(), "")
    if not raw_nonce.isascii() or not raw_nonce.isdigit():
        raise MalformedAuth(f"{HEADER_NONCE} is not a decimal integer")
    nonce = int(raw_nonce)
    scheme = lowered.get(HEADER_CRYPTO.lower(), SCHEME_SR25519).strip().lower()
    if scheme not in KNOWN_SCHEMES:
        raise MalformedAuth(f"unknown {HEADER_CRYPTO}")
    if scheme not in SUPPORTED_SCHEMES:
        raise UnsupportedScheme(f"{scheme} signatures are not accepted yet")
    raw_signature = lowered.get(HEADER_SIGNATURE.lower())
    if raw_signature is None:
        raise MalformedAuth(f"missing {HEADER_SIGNATURE}")
    try:
        signature = bytes.fromhex(raw_signature.removeprefix("0x"))
    except ValueError:
        raise MalformedAuth(f"{HEADER_SIGNATURE} is not hex") from None
    if len(signature) != _SIGNATURE_BYTES:
        raise MalformedAuth(f"{HEADER_SIGNATURE} must be 64 bytes")

    receiver = lowered.get(HEADER_RECEIVER.lower())
    if not receiver:
        raise WrongReceiver(f"missing {HEADER_RECEIVER}")
    if receiver not in receivers:
        raise WrongReceiver("request was signed for a different receiving hotkey")

    now = time.time_ns() if now_ns is None else now_ns
    if now - nonce > max_age_s * _NS:
        raise StaleRequest("nonce is older than the freshness window")
    if nonce - now > allowed_skew_s * _NS:
        raise StaleRequest("nonce is too far in the future")

    if await is_protected_sender(sender):
        raise ProtectedSender("the chain protects this account from this scheme")

    payload = build_payload(
        scheme=scheme,
        method=method,
        target=target,
        body=body,
        nonce_ns=nonce,
        sender=sender,
        receiver=receiver,
    )
    if not _verify_signature(scheme, sender, payload, signature):
        raise BadSignature("signature does not verify against the sender hotkey")

    if not await nonce_store.check_and_store(sender, nonce):
        raise ReplayedRequest("this nonce was already accepted from this hotkey")

    return VerifiedCaller(
        hotkey=sender, receiver=receiver, nonce_ns=nonce, scheme=scheme
    )


class InMemoryNonceStore:
    """A single-process :class:`NonceStore` for tests; never for production."""

    def __init__(self, retention_s: float = 60.0) -> None:
        if retention_s < DEFAULT_MAX_AGE_S + DEFAULT_ALLOWED_SKEW_S:
            raise ValueError("retention must cover the freshness window")
        self._retention_ns = int(retention_s * _NS)
        self._seen: dict[tuple[str, int], int] = {}

    async def check_and_store(self, hotkey: str, nonce_ns: int) -> bool:
        now = time.monotonic_ns()
        self._seen = {k: exp for k, exp in self._seen.items() if exp > now}
        key = (hotkey, nonce_ns)
        if key in self._seen:
            return False
        self._seen[key] = now + self._retention_ns
        return True
