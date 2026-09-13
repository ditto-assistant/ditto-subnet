"""Sign in with Ditto for miners: the Platform as an OIDC relying party.

A miner proves control of a hotkey by holding a live miner session (minted by
``ditto login`` with a hotkey signature). To attach a Ditto product account
to that hotkey the Platform runs a standard authorization-code + PKCE flow
against Ditto's OpenID provider using the Omni Aura-owned DittoBench app as
the client. The Ditto user id is read **only** from the id_token this module
verifies against the provider's JWKS; a caller can never assert it.

Nothing here touches TAO, emissions or scoring. The link is identity
plumbing: it lets the relay attribute inference to a consenting Ditto
account and lets the Feedback Track name a hotkey for a Ditto user.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ATTEMPT_TTL_SECONDS: Final = 10 * 60
JWKS_CACHE_SECONDS: Final = 10 * 60
DEFAULT_SCOPES: Final = "openid email"
CLOCK_SKEW_SECONDS: Final = 60


class DittoLinkRejected(Exception):
    """The Ditto sign-in could not be completed or verified."""


@dataclass(frozen=True)
class DittoLinkConfig:
    """Relying-party settings for Sign in with Ditto (``DITTO_LINK_*``).

    Disabled by default so the Platform boots unchanged until an operator
    registers the DittoBench app and points these at it.
    """

    enabled: bool
    issuer: str
    """Ditto OpenID issuer, e.g. ``https://api.heyditto.ai``."""
    client_id: str | None
    """The DittoBench ``app_id`` (OIDC client_id)."""
    client_secret: str | None
    """The app secret; in-memory only, never logged or served."""
    redirect_url: str
    """Exact callback registered on the app, e.g. ``https://dittobench.ai/api/v1/miner-auth/ditto/callback``."""
    return_url: str
    """Where the browser lands after linking (the dashboard sign-in page)."""
    scopes: str = DEFAULT_SCOPES
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class DittoIdentity:
    """Verified id_token claims the Platform keeps."""

    user_id: str
    email: str | None
    email_verified: bool


def new_state() -> str:
    return secrets.token_urlsafe(32)


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def code_challenge_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url(digest)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _int_from_b64url(raw: str) -> int:
    return int.from_bytes(_b64url_decode(raw), "big")


def authorize_url(
    config: DittoLinkConfig, *, state: str, nonce: str, code_challenge: str
) -> str:
    if not config.client_id:
        raise DittoLinkRejected("Ditto account linking is not configured")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_url,
            "scope": config.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{config.issuer.rstrip('/')}/authorize?{query}"


def same_issuer(expected: str, actual: object) -> bool:
    return isinstance(actual, str) and actual.rstrip("/") == expected.rstrip("/")


def verify_id_token(
    token: str,
    *,
    jwks: dict[str, Any],
    issuer: str,
    client_id: str,
    nonce: str,
    now: float | None = None,
) -> DittoIdentity:
    """Verify an RS256 id_token against ``jwks`` and return the claims kept.

    Checks signature, ``iss``, ``aud`` (string or list), ``exp``/``iat`` with
    a minute of skew, and the ``nonce`` bound to this attempt. Any failure is
    a :class:`DittoLinkRejected`; the message never echoes the token.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise DittoLinkRejected("id_token is malformed")
    header_raw, payload_raw, signature_raw = parts
    try:
        header = json.loads(_b64url_decode(header_raw))
        payload = json.loads(_b64url_decode(payload_raw))
        signature = _b64url_decode(signature_raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise DittoLinkRejected("id_token is malformed") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise DittoLinkRejected("id_token is malformed")
    if header.get("alg") != "RS256":
        raise DittoLinkRejected("id_token uses an unsupported algorithm")
    key = _select_key(jwks, header.get("kid"))
    try:
        key.verify(
            signature,
            f"{header_raw}.{payload_raw}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except InvalidSignature as exc:
        raise DittoLinkRejected("id_token signature did not verify") from exc

    if not same_issuer(issuer, payload.get("iss")):
        raise DittoLinkRejected("id_token issuer mismatch")
    audience = payload.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if client_id not in audiences:
        raise DittoLinkRejected("id_token audience mismatch")
    moment = time.time() if now is None else now
    exp = payload.get("exp")
    if not isinstance(exp, int | float) or exp + CLOCK_SKEW_SECONDS < moment:
        raise DittoLinkRejected("id_token has expired")
    iat = payload.get("iat")
    if isinstance(iat, int | float) and iat - CLOCK_SKEW_SECONDS > moment:
        raise DittoLinkRejected("id_token is not valid yet")
    if payload.get("nonce") != nonce:
        raise DittoLinkRejected("id_token nonce mismatch")
    subject = payload.get("sub")
    if not isinstance(subject, str) or not (1 <= len(subject) <= 128):
        raise DittoLinkRejected("id_token has no usable subject")
    email = payload.get("email")
    return DittoIdentity(
        user_id=subject,
        email=email if isinstance(email, str) and email else None,
        email_verified=bool(payload.get("email_verified", False)),
    )


def _select_key(jwks: dict[str, Any], kid: object) -> rsa.RSAPublicKey:
    keys = jwks.get("keys")
    if not isinstance(keys, list) or not keys:
        raise DittoLinkRejected("provider JWKS has no keys")
    candidates = [k for k in keys if isinstance(k, dict) and k.get("kty") == "RSA"]
    if isinstance(kid, str):
        matched = [k for k in candidates if k.get("kid") == kid]
        if matched:
            candidates = matched
    if not candidates:
        raise DittoLinkRejected("no RSA key matches the id_token")
    jwk = candidates[0]
    try:
        numbers = rsa.RSAPublicNumbers(
            e=_int_from_b64url(str(jwk["e"])), n=_int_from_b64url(str(jwk["n"]))
        )
        return numbers.public_key()
    except (KeyError, ValueError) as exc:
        raise DittoLinkRejected("provider JWKS key is malformed") from exc


class DittoLinkClient:
    """Outbound calls to the Ditto OpenID provider, with a cached JWKS.

    One instance per process on ``app.state.ditto_link``. Tests inject an
    ``httpx.MockTransport``; production uses a plain ``AsyncClient``.
    """

    def __init__(
        self,
        config: DittoLinkConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self._client = httpx.AsyncClient(
            timeout=config.timeout_seconds, transport=transport
        )
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(
            self.config.enabled and self.config.client_id and self.config.client_secret
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        return authorize_url(
            self.config, state=state, nonce=nonce, code_challenge=code_challenge
        )

    async def jwks(self, *, force: bool = False) -> dict[str, Any]:
        moment = time.time()
        if (
            not force
            and self._jwks is not None
            and moment - self._jwks_fetched_at < JWKS_CACHE_SECONDS
        ):
            return self._jwks
        url = f"{self.config.issuer.rstrip('/')}/.well-known/jwks.json"
        try:
            response = await self._client.get(
                url, headers={"accept": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise DittoLinkRejected(
                "could not reach the Ditto sign-in provider"
            ) from exc
        if response.status_code != 200:
            raise DittoLinkRejected("the Ditto sign-in provider did not serve its keys")
        try:
            document = response.json()
        except ValueError as exc:
            raise DittoLinkRejected("provider JWKS is not JSON") from exc
        if not isinstance(document, dict):
            raise DittoLinkRejected("provider JWKS is malformed")
        self._jwks = document
        self._jwks_fetched_at = moment
        return document

    async def exchange_code(
        self, *, code: str, code_verifier: str, nonce: str
    ) -> DittoIdentity:
        """Redeem ``code`` at the token endpoint and verify the id_token.

        The client secret travels only here, as ``client_secret`` in the
        form body over HTTPS to the configured issuer.
        """
        if (
            not self.enabled
            or not self.config.client_id
            or not self.config.client_secret
        ):
            raise DittoLinkRejected("Ditto account linking is not configured")
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.config.redirect_url,
            "client_id": self.config.client_id,
            "client_secret": self.config.client_secret,
            "code_verifier": code_verifier,
        }
        try:
            response = await self._client.post(
                f"{self.config.issuer.rstrip('/')}/token",
                data=form,
                headers={"accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise DittoLinkRejected(
                "could not reach the Ditto sign-in provider"
            ) from exc
        if response.status_code != 200:
            raise DittoLinkRejected(_token_error(response))
        try:
            body = response.json()
        except ValueError as exc:
            raise DittoLinkRejected("token response is not JSON") from exc
        token = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise DittoLinkRejected("token response has no id_token")
        keys = await self.jwks()
        try:
            return verify_id_token(
                token,
                jwks=keys,
                issuer=self.config.issuer,
                client_id=self.config.client_id,
                nonce=nonce,
            )
        except DittoLinkRejected:
            # A key rotation between fetch and verify is the one legitimate
            # reason a fresh token fails; retry once with fresh keys.
            keys = await self.jwks(force=True)
            return verify_id_token(
                token,
                jwks=keys,
                issuer=self.config.issuer,
                client_id=self.config.client_id,
                nonce=nonce,
            )


def _token_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        code = str(payload.get("error") or "").strip()
        description = str(payload.get("error_description") or "").strip()
        if code or description:
            detail = " ".join(part for part in (code, description) if part)[:160]
            return f"Ditto sign-in was refused: {detail}"
    return f"Ditto sign-in was refused (HTTP {response.status_code})"


RESULT_PARAMS: Final = ("ditto", "reason", "attempt")


def _strip_result_params(url: str) -> str:
    """Drop our own result parameters from a URL so they cannot be pre-seeded."""
    base, hash_sep, fragment = url.partition("#")
    path, q_sep, query = base.partition("?")
    kept = [
        pair
        for pair in query.split("&")
        if pair and pair.split("=", 1)[0] not in RESULT_PARAMS
    ]
    base = path + ("?" + "&".join(kept) if kept else "")
    if hash_sep:
        route, fq_sep, fquery = fragment.partition("?")
        fkept = [
            pair
            for pair in fquery.split("&")
            if pair and pair.split("=", 1)[0] not in RESULT_PARAMS
        ]
        fragment = route + ("?" + "&".join(fkept) if fkept else "")
        return base + "#" + fragment
    return base


def safe_return_to(config: DittoLinkConfig, requested: str | None) -> str:
    """Only the configured dashboard origin may receive the browser back.

    A ``return_to`` on another origin is dropped, not honoured, so the
    callback can never be turned into an open redirect. The candidate is
    re-serialised from the parsed URL (never echoed raw), backslashes and
    control characters are refused outright, and our own result parameters
    are stripped so a caller cannot pre-seed ``ditto=linked``.
    """
    default = config.return_url
    if not requested:
        return default
    if any(ch in requested for ch in "\\\x00\r\n\t ") or any(
        ord(ch) < 0x20 or ord(ch) == 0x7F for ch in requested
    ):
        return default
    allowed = httpx.URL(default)
    try:
        candidate = httpx.URL(requested)
    except (httpx.InvalidURL, TypeError, ValueError):
        return default
    if (
        candidate.scheme != allowed.scheme
        or candidate.host != allowed.host
        or candidate.port != allowed.port
        or candidate.userinfo
    ):
        return default
    normalized = str(candidate)
    origin = f"{allowed.scheme}://{allowed.netloc.decode()}"
    if not (normalized == origin or normalized.startswith(origin + "/")):
        return default
    return _strip_result_params(normalized)


def with_result(
    url: str, *, outcome: str, reason: str | None = None, attempt: str | None = None
) -> str:
    """Append ``ditto=<outcome>`` (and a bounded ``reason``) to a dashboard URL.

    Dashboard routes live in the hash (``/#/reviews``), so the query goes
    after the hash route when one is present.
    """
    params: dict[str, str] = {"ditto": outcome}
    if reason:
        params["reason"] = reason[:120]
    if attempt:
        params["attempt"] = attempt
    suffix = urlencode(params)
    if "#" in url:
        base, _, fragment = url.partition("#")
        joiner = "&" if "?" in fragment else "?"
        return f"{base}#{fragment}{joiner}{suffix}"
    joiner = "&" if "?" in url else "?"
    return f"{url}{joiner}{suffix}"
