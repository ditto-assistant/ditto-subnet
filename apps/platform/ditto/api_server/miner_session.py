"""Miner dashboard/MCP session proofs, scopes, and bearer tokens.

A session is minted when a miner signs ``ditto-miner-login:v1`` over a
device grant (user code + scopes + TTL). The raw bearer is returned once
and stored only as a SHA-256 digest. Session-scoped writes (profile
socials, avatar upload) do not re-ask for a hotkey signature. Upload,
handle reservation, and other one-shot signed actions stay CLI-signed;
the hosted MCP returns those commands instead of performing them.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from ditto.api_server.attestation import (
    MAX_ATTESTATION_AGE,
    MAX_ISSUED_AT_SKEW,
    verify_signature,
)
from ditto.api_server.miner_avatar import MinerAvatarRejected
from ditto.api_server.name_claim import KeyKind

LOGIN_DOMAIN: Final = "ditto-miner-login:v1"
TOKEN_PREFIX: Final = "ditto_ms_"
MIN_TTL_SECONDS: Final = 3600
MAX_TTL_SECONDS: Final = 30 * 24 * 3600
DEFAULT_TTL_SECONDS: Final = 24 * 3600
GRANT_TTL: Final = timedelta(minutes=15)
OAUTH_CODE_TTL: Final = timedelta(minutes=5)
USER_CODE_ALPHABET: Final = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

Scope = Literal["read", "profile", "download", "upload", "handle", "challenges"]
ALL_SCOPES: Final[tuple[Scope, ...]] = (
    "read",
    "profile",
    "download",
    "upload",
    "handle",
    "challenges",
)
DEFAULT_SCOPES: Final[tuple[Scope, ...]] = ("read",)
OAUTH_DEFAULT_SCOPES: Final[tuple[Scope, ...]] = ("read",)
SessionLabel = Literal["dashboard", "mcp", "cli"]

_X_HOSTS: Final = ("x.com", "www.x.com", "twitter.com", "www.twitter.com")
_GITHUB_HOSTS: Final = ("github.com", "www.github.com")
_DISCORD_RE = re.compile(r"^[A-Za-z0-9._]{2,32}$")
_USER_CODE_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}$")
_PKCE_RE = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
_CUSTOM_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]{1,32}$")
_BLOCKED_REDIRECT_SCHEMES: Final = frozenset(
    {"javascript", "data", "file", "vbscript", "about"}
)
MAX_PROFILE_URL_LEN: Final = 200


class MinerSessionRejected(MinerAvatarRejected):
    """A miner login, session, or profile mutation failed policy."""


def normalize_scopes(raw: list[str] | tuple[str, ...] | str) -> tuple[Scope, ...]:
    """Return a sorted, de-duplicated, known scope tuple."""
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        parts = [str(part).strip() for part in raw if str(part).strip()]
    unknown = sorted({part for part in parts if part not in ALL_SCOPES})
    if unknown:
        raise MinerSessionRejected("unknown miner session scope: " + ", ".join(unknown))
    if not parts:
        raise MinerSessionRejected("at least one miner session scope is required")
    ordered = tuple(scope for scope in ALL_SCOPES if scope in set(parts))
    return ordered


def scopes_csv(scopes: tuple[Scope, ...] | list[str] | str) -> str:
    return ",".join(normalize_scopes(scopes))


def parse_scopes(csv: str) -> tuple[Scope, ...]:
    return normalize_scopes(csv)


def has_scope(scopes: tuple[str, ...] | str, needed: Scope) -> bool:
    have = parse_scopes(scopes) if isinstance(scopes, str) else tuple(scopes)
    return needed in have


def clamp_ttl_seconds(value: int) -> int:
    if value < MIN_TTL_SECONDS or value > MAX_TTL_SECONDS:
        raise MinerSessionRejected(
            f"session ttl must be between {MIN_TTL_SECONDS} and "
            f"{MAX_TTL_SECONDS} seconds"
        )
    return value


def new_user_code() -> str:
    chars = [secrets.choice(USER_CODE_ALPHABET) for _ in range(8)]
    return f"{''.join(chars[:4])}-{''.join(chars[4:])}"


def normalize_user_code(raw: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if len(compact) != 8:
        raise MinerSessionRejected("user code must look like ABCD-EFGH")
    formatted = f"{compact[:4]}-{compact[4:]}"
    if not _USER_CODE_RE.match(formatted):
        raise MinerSessionRejected("user code must look like ABCD-EFGH")
    return formatted


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_hex(32)


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _issued_stamp(issued_at: datetime) -> str:
    return issued_at.astimezone(UTC).isoformat(timespec="microseconds")


def login_message(
    *,
    netuid: int,
    miner_hotkey: str,
    user_code: str,
    grant_id: UUID,
    ttl_seconds: int,
    scopes: tuple[Scope, ...] | str,
    nonce: UUID,
    issued_at: datetime,
    key_kind: KeyKind,
    signer: str,
    oauth_client_id: str | None = None,
    redirect_uri: str | None = None,
) -> bytes:
    issued = _issued_stamp(issued_at)
    csv = scopes_csv(scopes)
    code = normalize_user_code(user_code)
    client = oauth_client_id or "-"
    redirect = redirect_uri or "-"
    return (
        f"{LOGIN_DOMAIN}:{netuid}:{miner_hotkey}:{code}:{grant_id}:{ttl_seconds}"
        f":{csv}:{nonce}:{issued}:{key_kind}:{signer}:{client}:{redirect}"
    ).encode()


def validate_pkce(value: str, *, label: str = "PKCE value") -> str:
    if not _PKCE_RE.fullmatch(value):
        raise MinerSessionRejected(
            f"{label} must be 43-128 unreserved characters (A-Z, a-z, 0-9, -._~)"
        )
    return value


def validate_redirect_uri(raw: str) -> str:
    value = raw.strip()
    if not value or any(ch in value for ch in "\r\n\x00"):
        raise MinerSessionRejected("redirect_uri is invalid")
    from urllib.parse import urlparse

    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    if scheme in _BLOCKED_REDIRECT_SCHEMES:
        raise MinerSessionRejected("redirect_uri scheme is not allowed")
    if scheme == "https":
        return value
    if scheme == "http":
        host = (parsed.hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return value
        raise MinerSessionRejected("http redirect_uri must target loopback")
    if scheme and _CUSTOM_SCHEME_RE.fullmatch(scheme):
        return value
    raise MinerSessionRejected("redirect_uri scheme is not allowed")


def check_freshness(*, issued_at: datetime, now: datetime) -> None:
    issued = issued_at.astimezone(UTC)
    if issued > now + MAX_ISSUED_AT_SKEW:
        raise MinerSessionRejected(
            "login issued_at is in the future beyond the allowed skew"
        )
    if issued < now - MAX_ATTESTATION_AGE:
        raise MinerSessionRejected(
            "login signature has expired; mint a fresh one and submit it again"
        )


def verify_signed_action(
    *,
    payload: bytes,
    hotkey: str,
    key_kind: KeyKind,
    signer: str,
    signature: str,
    bound_coldkey: str | None,
) -> None:
    if key_kind == "hotkey":
        if signer != hotkey:
            raise MinerSessionRejected(
                "hotkey proof signer must be the named hotkey itself"
            )
    elif key_kind == "coldkey":
        if bound_coldkey is None:
            raise MinerSessionRejected(
                "coldkey proof requires a payment record binding that hotkey"
            )
        if signer != bound_coldkey:
            raise MinerSessionRejected(
                "coldkey proof signer is not the payment-bound coldkey"
            )
    else:
        raise MinerSessionRejected(f"unknown key_kind {key_kind!r}")
    if not verify_signature(signer=signer, payload=payload, signature_hex=signature):
        raise MinerSessionRejected("signature did not verify")


DITTO_GIT_FROM = "git+https://github.com/ditto-assistant/ditto-subnet.git"
DITTO_CLONE_URL = "https://github.com/ditto-assistant/ditto-subnet.git"


def ditto_uvx_command(*, network: str, argv: str) -> str:
    """One-shot installer command. ``uvx`` caches the git checkout."""
    return f"uvx --from {DITTO_GIT_FROM} ditto --network {network} {argv}"


def ditto_clone_command(*, network: str, argv: str) -> str:
    """Fallback when the miner already wants a working tree."""
    return (
        f"git clone {DITTO_CLONE_URL}\n"
        "cd ditto-subnet && uv sync\n"
        f"uv run ditto --network {network} {argv}"
    )


def login_command(*, user_code: str, network: str = "finney") -> str:
    code = normalize_user_code(user_code)
    return ditto_uvx_command(network=network, argv=f"login --code {code}")


def login_clone_command(*, user_code: str, network: str = "finney") -> str:
    code = normalize_user_code(user_code)
    return ditto_clone_command(network=network, argv=f"login --code {code}")


def normalize_x_url(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    from urllib.parse import urlparse

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _X_HOSTS:
        raise MinerSessionRejected("x_url must be an x.com or twitter.com profile URL")
    path = (parsed.path or "").rstrip("/")
    if not path or path == "/":
        raise MinerSessionRejected("x_url must include a profile path")
    normalized = f"https://x.com{path}"
    if len(normalized) > MAX_PROFILE_URL_LEN:
        raise MinerSessionRejected("x_url is too long")
    return normalized


def normalize_github_url(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if "://" not in value:
        value = "https://" + value
    from urllib.parse import urlparse

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or host not in _GITHUB_HOSTS:
        raise MinerSessionRejected("github_url must be a github.com profile URL")
    path = (parsed.path or "").rstrip("/")
    if not path or path == "/":
        raise MinerSessionRejected("github_url must include a username path")
    normalized = f"https://github.com{path}"
    if len(normalized) > MAX_PROFILE_URL_LEN:
        raise MinerSessionRejected("github_url is too long")
    return normalized


def normalize_discord_handle(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip().lstrip("@")
    if not value:
        return None
    if not _DISCORD_RE.match(value):
        raise MinerSessionRejected(
            "discord_handle must be 2-32 letters, numbers, dots, or underscores"
        )
    return value
