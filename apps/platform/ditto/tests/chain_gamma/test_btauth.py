"""btauth/1 verification against the signed-requests guide's vectors."""

from __future__ import annotations

import hashlib

import pytest

from ditto.chain_gamma.btauth import (
    HEADER_CRYPTO,
    HEADER_NONCE,
    HEADER_RECEIVER,
    HEADER_SIGNATURE,
    HEADER_VERSION,
    BadSignature,
    InMemoryNonceStore,
    MalformedAuth,
    ProtectedSender,
    ReplayedRequest,
    StaleRequest,
    UnsupportedScheme,
    WrongReceiver,
    build_payload,
    verify_request,
)
from ditto.tests.chain_gamma.helpers import (
    ALICE_DEV,
    ALICE_ED25519,
    ALICE_SR25519,
    BOB,
    CHARLIE,
    VECTOR_BODY,
    VECTOR_NONCE,
    VECTOR_TARGET,
    always_protected,
    ed25519_vector_headers,
    never_protected,
    sr25519_headers,
)

GUIDE_SR25519_PAYLOAD = (
    b"btauth/1\n"
    b"sr25519\n"
    b"POST\n"
    b"/generate?stream=false\n"
    b"341c57448e531310fbbe83f44cea2a5e838bd9e8a6b82b269f01d0dbbc23c3cc\n"
    b"1752076800000000000\n"
    b"5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY\n"
    b"5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
)


async def _verify(headers, body=VECTOR_BODY, **overrides):
    kwargs = {
        "method": "POST",
        "target": VECTOR_TARGET,
        "receivers": frozenset({BOB}),
        "nonce_store": InMemoryNonceStore(),
        "is_protected_sender": never_protected,
        "now_ns": VECTOR_NONCE,
    }
    kwargs.update(overrides)
    return await verify_request(headers, body, **kwargs)


def test_payload_matches_guide_vector_byte_for_byte() -> None:
    assert ALICE_DEV.ss58_address == ALICE_SR25519
    assert hashlib.sha256(VECTOR_BODY).hexdigest() == (
        "341c57448e531310fbbe83f44cea2a5e838bd9e8a6b82b269f01d0dbbc23c3cc"
    )
    payload = build_payload(
        scheme="sr25519",
        method="post",
        target=VECTOR_TARGET,
        body=VECTOR_BODY,
        nonce_ns=VECTOR_NONCE,
        sender=ALICE_SR25519,
        receiver=BOB,
    )
    assert payload == GUIDE_SR25519_PAYLOAD
    assert not payload.endswith(b"\n")


async def test_guide_ed25519_vector_verifies() -> None:
    caller = await _verify(ed25519_vector_headers())
    assert (caller.hotkey, caller.receiver, caller.scheme) == (
        ALICE_ED25519,
        BOB,
        "ed25519",
    )


async def test_ed25519_vector_rejects_an_edited_body() -> None:
    with pytest.raises(BadSignature):
        await _verify(ed25519_vector_headers(), body=b'{"prompt": "hellO"}')


async def test_ed25519_vector_rejects_an_edited_target() -> None:
    with pytest.raises(BadSignature):
        await _verify(ed25519_vector_headers(), target="/generate?stream=true")


async def test_sr25519_signature_verifies_and_header_names_are_case_free() -> None:
    headers = {k.lower(): v for k, v in sr25519_headers().items()}
    caller = await _verify(headers)
    assert caller.hotkey == ALICE_SR25519
    assert caller.scheme == "sr25519"


async def test_replay_is_refused() -> None:
    store = InMemoryNonceStore()
    headers = sr25519_headers()
    await _verify(headers, nonce_store=store)
    with pytest.raises(ReplayedRequest):
        await _verify(headers, nonce_store=store)


async def test_bad_signature_does_not_consume_the_nonce() -> None:
    store = InMemoryNonceStore()
    forged = sr25519_headers() | {HEADER_SIGNATURE: "0x" + "11" * 64}
    with pytest.raises(BadSignature):
        await _verify(forged, nonce_store=store)
    await _verify(sr25519_headers(), nonce_store=store)


@pytest.mark.parametrize(
    "receiver_headers",
    [
        {HEADER_RECEIVER: CHARLIE},
        {HEADER_RECEIVER: "-"},
    ],
)
async def test_receiver_must_be_a_publisher(receiver_headers) -> None:
    with pytest.raises(WrongReceiver):
        await _verify(sr25519_headers() | receiver_headers)


async def test_missing_receiver_is_refused() -> None:
    headers = sr25519_headers()
    del headers[HEADER_RECEIVER]
    with pytest.raises(WrongReceiver):
        await _verify(headers)


async def test_any_publisher_hotkey_is_an_accepted_receiver() -> None:
    headers = sr25519_headers(receiver=CHARLIE)
    caller = await _verify(headers, receivers=frozenset({BOB, CHARLIE}))
    assert caller.receiver == CHARLIE


@pytest.mark.parametrize(
    ("offset_s", "ok"),
    [(-10.0, True), (-10.001, False), (2.0, True), (2.001, False)],
)
async def test_freshness_window(offset_s: float, ok: bool) -> None:
    now = VECTOR_NONCE - int(offset_s * 1_000_000_000)
    if ok:
        await _verify(sr25519_headers(), now_ns=now)
    else:
        with pytest.raises(StaleRequest):
            await _verify(sr25519_headers(), now_ns=now)


async def test_protected_sender_is_refused_before_signature_work() -> None:
    with pytest.raises(ProtectedSender):
        await _verify(sr25519_headers(), is_protected_sender=always_protected)


@pytest.mark.parametrize("scheme", ["hashed", "hashed-ed25519"])
async def test_hashed_schemes_wait_for_the_new_sdk(scheme: str) -> None:
    with pytest.raises(UnsupportedScheme):
        await _verify(sr25519_headers() | {HEADER_CRYPTO: scheme})


@pytest.mark.parametrize(
    "broken",
    [
        {HEADER_VERSION: "2"},
        {HEADER_CRYPTO: "ml-dsa"},
        {HEADER_NONCE: "17e18"},
        {HEADER_NONCE: "-5"},
        {HEADER_SIGNATURE: "0xzz"},
        {HEADER_SIGNATURE: "0x" + "11" * 63},
    ],
)
async def test_malformed_headers(broken) -> None:
    with pytest.raises(MalformedAuth):
        await _verify(sr25519_headers() | broken)


async def test_declared_scheme_must_match_the_signature() -> None:
    # An sr25519 signature presented as ed25519 never verifies; no fallback.
    with pytest.raises(BadSignature):
        await _verify(sr25519_headers() | {HEADER_CRYPTO: "ed25519"})


def test_in_memory_store_refuses_a_retention_shorter_than_the_window() -> None:
    with pytest.raises(ValueError):
        InMemoryNonceStore(retention_s=5)
