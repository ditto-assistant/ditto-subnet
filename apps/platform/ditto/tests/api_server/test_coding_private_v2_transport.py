from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_private_v2_transport import (
    PrivateV2TransportError,
    verify_private_v2_transport,
)


def _write_transport(
    root: Path,
    *,
    include_nonce: bool = True,
    digest: str = "a" * 64,
    ciphertext: bytes = b"\x00" * 17,
) -> None:
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    objects = root / "objects"
    objects.mkdir(mode=0o700)
    (objects / f"{digest}.bin").write_bytes(ciphertext)
    (objects / f"{digest}.bin").chmod(0o600)
    item = {
        "aad_sha256": "b" * 64,
        "ciphertext_relative_path": f"objects/{digest}.bin",
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
        "ciphertext_size_bytes": len(ciphertext),
        "nonce_b64": base64.b64encode(b"\x00" * 12).decode("ascii"),
        "plaintext_sha256": digest,
        "plaintext_size_bytes": len(ciphertext) - 16,
        "wrapped_data_key_b64": base64.b64encode(b"\x01" * (3072 // 8)).decode("ascii"),
    }
    if not include_nonce:
        item.pop("nonce_b64")
    projection = {
        "catalog_merkle_root": "c" * 64,
        "catalog_sha256": "d" * 64,
        "coding_contract_version": 2,
        "objects": [item],
        "payload_sha256": "e" * 64,
        "schema": "dittobench-coding-private-v2-transport-v1",
        "weight_eligible": False,
        "wrapping_key_sha256": "f" * 64,
    }
    authority = {
        **projection,
        "transport_sha256": hashlib.sha256(
            coding_canonical_json_bytes(
                projection,
                maximum_bytes=16 << 20,
                label="private v2 transport manifest",
            )
        ).hexdigest(),
    }
    body = coding_canonical_json_bytes(
        authority,
        maximum_bytes=16 << 20,
        label="private v2 transport manifest",
    )
    (root / "manifest.json").write_bytes(body)
    (root / "manifest.json").chmod(0o600)


def test_transport_verify_requires_unwrap_fields(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    _write_transport(missing, include_nonce=False)
    with pytest.raises(PrivateV2TransportError, match="object is invalid"):
        verify_private_v2_transport(missing)
