from __future__ import annotations

import base64
import hashlib
import json
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
    plaintext_size = len(ciphertext) - 16
    aad = coding_canonical_json_bytes(
        {
            "schema": "dittobench-coding-private-v2-transport-aad-v1",
            "payload_sha256": "e" * 64,
            "catalog_sha256": "d" * 64,
            "plaintext_sha256": digest,
            "plaintext_size_bytes": plaintext_size,
            "wrapping_key_sha256": "f" * 64,
        },
        maximum_bytes=16 << 10,
        label="private v2 transport AAD",
    )
    item = {
        "aad_sha256": hashlib.sha256(aad).hexdigest(),
        "ciphertext_relative_path": f"objects/{digest}.bin",
        "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
        "ciphertext_size_bytes": len(ciphertext),
        "nonce_b64": base64.b64encode(b"\x00" * 12).decode("ascii"),
        "plaintext_sha256": digest,
        "plaintext_size_bytes": plaintext_size,
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


def test_transport_verify_rejects_leftover_objects_and_aad_drift(
    tmp_path: Path,
) -> None:
    leftover = tmp_path / "leftover"
    _write_transport(leftover)
    extra = leftover / "objects" / f"{'0' * 64}.bin"
    extra.write_bytes(b"\x00" * 17)
    extra.chmod(0o600)
    with pytest.raises(PrivateV2TransportError, match="objects drifted"):
        verify_private_v2_transport(leftover)

    drifted = tmp_path / "aad-drift"
    _write_transport(drifted)
    manifest_path = drifted / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["objects"][0]["aad_sha256"] = "b" * 64
    projection = {
        key: value for key, value in manifest.items() if key != "transport_sha256"
    }
    manifest["transport_sha256"] = hashlib.sha256(
        coding_canonical_json_bytes(
            projection,
            maximum_bytes=16 << 20,
            label="private v2 transport manifest",
        )
    ).hexdigest()
    manifest_path.write_bytes(
        coding_canonical_json_bytes(
            manifest,
            maximum_bytes=16 << 20,
            label="private v2 transport manifest",
        )
    )
    with pytest.raises(PrivateV2TransportError, match="object is invalid"):
        verify_private_v2_transport(drifted)


def test_transport_rejects_symlinked_object_directory(tmp_path: Path) -> None:
    root = tmp_path / "transport"
    _write_transport(root)
    (root / "objects").rename(root / "actual-objects")
    (root / "objects").symlink_to(root / "actual-objects", target_is_directory=True)
    with pytest.raises(PrivateV2TransportError, match="objects are invalid"):
        verify_private_v2_transport(root)
