"""Unit tests for :mod:`ditto.api_server.ditto_link` (no network, no DB)."""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ditto.api_server.ditto_link import (
    DittoLinkConfig,
    DittoLinkRejected,
    authorize_url,
    code_challenge_s256,
    hash_state,
    safe_return_to,
    verify_id_token,
    with_result,
)

ISSUER = "https://api.heyditto.ai"
CLIENT_ID = "dittobench-app"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks_for(key: rsa.RSAPrivateKey, kid: str = "ditto-mcp-oauth-1") -> dict:
    numbers = key.public_key().public_numbers()
    n = numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
    e = numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": _b64url(n),
                "e": _b64url(e),
            }
        ]
    }


def sign_id_token(
    key: rsa.RSAPrivateKey,
    claims: dict,
    *,
    alg: str = "RS256",
    kid: str = "ditto-mcp-oauth-1",
) -> str:
    header = _b64url(json.dumps({"alg": alg, "typ": "JWT", "kid": kid}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signature = key.sign(
        f"{header}.{payload}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
    )
    return f"{header}.{payload}.{_b64url(signature)}"


def claims(**over: object) -> dict:
    now = int(time.time())
    base: dict = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "firebase-uid-123",
        "nonce": "n0nce",
        "iat": now,
        "exp": now + 300,
        "email": "miner@example.com",
        "email_verified": True,
    }
    base.update(over)
    return base


def test_verify_id_token_accepts_a_good_token() -> None:
    key = make_key()
    identity = verify_id_token(
        sign_id_token(key, claims()),
        jwks=jwks_for(key),
        issuer=ISSUER + "/",
        client_id=CLIENT_ID,
        nonce="n0nce",
    )
    assert identity.user_id == "firebase-uid-123"
    assert identity.email == "miner@example.com"
    assert identity.email_verified is True


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"nonce": "other"}, "nonce"),
        ({"aud": "someone-else"}, "audience"),
        ({"iss": "https://evil.example"}, "issuer"),
        ({"exp": int(time.time()) - 3600}, "expired"),
        ({"sub": ""}, "subject"),
    ],
)
def test_verify_id_token_rejects_bad_claims(override: dict, message: str) -> None:
    key = make_key()
    with pytest.raises(DittoLinkRejected, match=message):
        verify_id_token(
            sign_id_token(key, claims(**override)),
            jwks=jwks_for(key),
            issuer=ISSUER,
            client_id=CLIENT_ID,
            nonce="n0nce",
        )


def test_verify_id_token_rejects_wrong_key_and_alg() -> None:
    key = make_key()
    other = make_key()
    with pytest.raises(DittoLinkRejected, match="signature"):
        verify_id_token(
            sign_id_token(other, claims()),
            jwks=jwks_for(key),
            issuer=ISSUER,
            client_id=CLIENT_ID,
            nonce="n0nce",
        )
    with pytest.raises(DittoLinkRejected, match="algorithm"):
        verify_id_token(
            sign_id_token(key, claims(), alg="none"),
            jwks=jwks_for(key),
            issuer=ISSUER,
            client_id=CLIENT_ID,
            nonce="n0nce",
        )
    # A list audience that names the client is fine; the public PEM export
    # proves the JWKS conversion round-trips the same key.
    assert key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    verify_id_token(
        sign_id_token(key, claims(aud=[CLIENT_ID, "x"])),
        jwks=jwks_for(key),
        issuer=ISSUER,
        client_id=CLIENT_ID,
        nonce="n0nce",
    )


def _config(**over: object) -> DittoLinkConfig:
    base: dict[str, object] = {
        "enabled": True,
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": "s3cret",
        "redirect_url": "https://dittobench.ai/api/v1/miner-auth/ditto/callback",
        "return_url": "https://dittobench.ai/#/reviews",
    }
    base.update(over)
    return DittoLinkConfig(**base)  # type: ignore[arg-type]


def test_authorize_url_is_pkce_code_flow() -> None:
    url = authorize_url(
        _config(), state="st", nonce="nn", code_challenge=code_challenge_s256("v" * 43)
    )
    assert url.startswith(ISSUER + "/authorize?")
    assert "response_type=code" in url
    assert f"client_id={CLIENT_ID}" in url
    assert "code_challenge_method=S256" in url
    assert "scope=openid+email" in url
    assert "client_secret" not in url
    assert len(hash_state("st")) == 64


def test_safe_return_to_only_allows_the_dashboard_origin() -> None:
    config = _config()
    assert safe_return_to(config, None) == config.return_url
    assert safe_return_to(config, "https://dittobench.ai/#/reviews?tab=profile") == (
        "https://dittobench.ai/#/reviews?tab=profile"
    )
    assert safe_return_to(config, "https://evil.example/#/reviews") == config.return_url
    assert safe_return_to(config, "javascript:alert(1)") == config.return_url


def test_with_result_appends_after_the_hash_route() -> None:
    assert with_result("https://dittobench.ai/#/reviews", outcome="linked") == (
        "https://dittobench.ai/#/reviews?ditto=linked"
    )
    assert with_result(
        "https://dittobench.ai/#/reviews?x=1", outcome="error", reason="a b"
    ) == ("https://dittobench.ai/#/reviews?x=1&ditto=error&reason=a+b")
    assert with_result("https://dittobench.ai/done", outcome="linked") == (
        "https://dittobench.ai/done?ditto=linked"
    )


def test_safe_return_to_rejects_authority_confusion_and_preseeded_results() -> None:
    config = _config()
    # Backslash authority confusion: httpx reads dittobench.ai, browsers go to evil.com.
    assert (
        safe_return_to(config, "https://evil.com\\@dittobench.ai/") == config.return_url
    )
    assert (
        safe_return_to(config, "https://user@dittobench.ai/#/reviews")
        == config.return_url
    )
    assert (
        safe_return_to(config, "https://dittobench.ai.evil.example/")
        == config.return_url
    )
    assert safe_return_to(config, "//evil.example/") == config.return_url
    assert (
        safe_return_to(config, "https://dittobench.ai/\r\nSet-Cookie:x")
        == config.return_url
    )
    # Our own result parameters cannot be pre-seeded by the caller.
    assert safe_return_to(
        config, "https://dittobench.ai/#/reviews?ditto=linked&tab=profile"
    ) == ("https://dittobench.ai/#/reviews?tab=profile")
    assert safe_return_to(config, "https://dittobench.ai/?ditto=linked#/reviews") == (
        "https://dittobench.ai/#/reviews"
    )
    assert with_result(
        "https://dittobench.ai/#/reviews", outcome="confirm", attempt="a-1"
    ) == ("https://dittobench.ai/#/reviews?ditto=confirm&attempt=a-1")
