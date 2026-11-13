from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from ditto_edge_protocol import paths
from ditto_edge_protocol.signing import (
    HEADER_NODE,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    DeviceKey,
    SignatureError,
    binding_message,
    canonical_request,
    node_id_for,
    verify_request,
)

NOW = 1_763_000_000
NONCE = "ab" * 16


def _key() -> DeviceKey:
    return DeviceKey.from_seed(bytes(range(32)))


def _signed(key: DeviceKey, body: bytes = b'{"a":1}') -> dict[str, object]:
    headers = key.sign_request(
        method="POST", path=paths.POLL, body=body, timestamp=NOW, nonce_hex=NONCE
    )
    return {
        "public_key_hex": key.public_key_hex,
        "signature_hex": headers.signature_hex,
        "method": "POST",
        "path": paths.POLL,
        "timestamp": NOW,
        "nonce_hex": NONCE,
        "body": body,
        "now": NOW,
    }


def test_node_id_is_prefixed_hash_of_raw_public_key() -> None:
    key = _key()
    expected = hashlib.sha256(bytes.fromhex(key.public_key_hex)).hexdigest()[:32]
    assert key.node_id == f"edn_{expected}"
    assert node_id_for(key.public_key_hex) == key.node_id


def test_node_id_rejects_malformed_keys() -> None:
    with pytest.raises(SignatureError):
        node_id_for("AB" * 32)
    with pytest.raises(SignatureError):
        node_id_for("ab" * 31)


def test_canonical_request_layout() -> None:
    body = b"hello"
    canonical = canonical_request(
        method="post", path="/api/v1/edge/poll", timestamp=7, nonce_hex=NONCE, body=body
    )
    assert canonical.decode().split("\n") == [
        "ditto-edge-request:v1",
        "POST",
        "/api/v1/edge/poll",
        "7",
        NONCE,
        hashlib.sha256(body).hexdigest(),
    ]


@pytest.mark.parametrize(
    "path", ["api/v1/edge/poll", "/api/v1/edge/poll?x=1", "/api/v1/edge poll", ""]
)
def test_canonical_request_refuses_unsigned_or_relative_paths(path: str) -> None:
    with pytest.raises(SignatureError):
        canonical_request(
            method="GET", path=path, timestamp=1, nonce_hex=NONCE, body=b""
        )


def test_canonical_request_refuses_bad_nonce_and_time() -> None:
    with pytest.raises(SignatureError):
        canonical_request(
            method="GET", path="/x", timestamp=1, nonce_hex="AB" * 16, body=b""
        )
    with pytest.raises(SignatureError):
        canonical_request(
            method="GET", path="/x", timestamp=-1, nonce_hex=NONCE, body=b""
        )


def test_signed_request_verifies() -> None:
    verify_request(**_signed(_key()))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("method", "GET"),
        ("path", paths.ACK),
        ("timestamp", NOW + 1),
        ("nonce_hex", "cd" * 16),
        ("body", b'{"a":2}'),
    ],
)
def test_any_edit_breaks_the_signature(field: str, value: object) -> None:
    request = _signed(_key())
    request[field] = value
    with pytest.raises(SignatureError, match="does not verify"):
        verify_request(**request)  # type: ignore[arg-type]


def test_another_key_cannot_verify() -> None:
    request = _signed(_key())
    request["public_key_hex"] = DeviceKey.generate().public_key_hex
    with pytest.raises(SignatureError, match="does not verify"):
        verify_request(**request)  # type: ignore[arg-type]


@pytest.mark.parametrize("drift", [-121, 121])
def test_stale_or_future_requests_are_refused(drift: int) -> None:
    request = _signed(_key())
    request["now"] = NOW + drift
    with pytest.raises(SignatureError, match="clock skew"):
        verify_request(**request)  # type: ignore[arg-type]


def test_signature_is_deterministic_and_headers_name_the_node() -> None:
    key = _key()
    first = key.sign_request(
        method="GET", path="/x", body=b"", timestamp=1, nonce_hex=NONCE
    )
    second = key.sign_request(
        method="GET", path="/x", body=b"", timestamp=1, nonce_hex=NONCE
    )
    assert first == second
    headers = first.as_dict()
    assert headers[HEADER_NODE] == key.node_id
    assert headers[HEADER_TIMESTAMP] == "1"
    assert headers[HEADER_NONCE] == NONCE
    assert headers[HEADER_SIGNATURE] == first.signature_hex


def test_fresh_nonce_when_not_given() -> None:
    key = _key()
    a = key.sign_request(method="GET", path="/x", body=b"", timestamp=1)
    b = key.sign_request(method="GET", path="/x", body=b"", timestamp=1)
    assert a.nonce_hex != b.nonce_hex
    assert len(a.nonce_hex) == 32


def test_load_or_create_writes_a_private_seed_and_reloads_it(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "device.key"
    created = DeviceKey.load_or_create(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    reloaded = DeviceKey.load_or_create(path)
    assert reloaded.node_id == created.node_id


def test_load_or_create_refuses_a_corrupt_seed(tmp_path: Path) -> None:
    path = tmp_path / "device.key"
    path.write_text("not a key\n")
    with pytest.raises(SignatureError):
        DeviceKey.load_or_create(path)


def test_from_seed_requires_32_bytes() -> None:
    with pytest.raises(SignatureError):
        DeviceKey.from_seed(b"short")


def test_binding_message_layout() -> None:
    message = binding_message(
        node_id="edn_" + "0" * 32, hotkey_ss58="5Abc", issued_at=9
    )
    assert message == b"ditto-edge-bind:v1:edn_00000000000000000000000000000000:5Abc:9"
    with pytest.raises(SignatureError):
        binding_message(node_id="node", hotkey_ss58="5Abc", issued_at=9)
    with pytest.raises(SignatureError):
        binding_message(node_id="edn_" + "0" * 32, hotkey_ss58="5A:bc", issued_at=9)
