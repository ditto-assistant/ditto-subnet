"""Offline AES-GCM/RSA-OAEP transport preparation for a v2 private payload."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import stat
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_hippius_encryption import (
    HippiusPrivateInputEncryptionError,
    load_hippius_wrapping_public_key,
)
from ditto.api_server.coding_private_v2_payload import (
    PrivateV2PayloadError,
    verify_private_v2_payload,
)

PRIVATE_V2_TRANSPORT_CONFIRMATION = "ENCRYPT HIPPIUS CODING PRIVATE V2 PAYLOAD"
_KEY_BYTES = 32
_NONCE_BYTES = 12


class PrivateV2TransportError(ValueError):
    """A protected v2 payload cannot become a transport directory."""


def prepare_private_v2_transport(
    *, payload_directory: Path, wrapping_public_key: Path, output: Path
) -> dict[str, Any]:
    """Envelope-encrypt every deduplicated payload object without provider I/O."""

    try:
        payload = verify_private_v2_payload(payload_directory)
        public_key, wrapping_sha = load_hippius_wrapping_public_key(wrapping_public_key)
    except (PrivateV2PayloadError, HippiusPrivateInputEncryptionError) as error:
        raise PrivateV2TransportError(
            "private v2 transport inputs are invalid"
        ) from error
    _new_directory(output)
    objects_dir = output / "objects"
    objects_dir.mkdir(mode=0o700)
    encrypted: list[dict[str, Any]] = []
    for item in payload["objects"]:
        digest = item["sha256"]
        plaintext = _read(payload_directory / "objects" / f"{digest}.bin")
        if (
            len(plaintext) != item["size_bytes"]
            or hashlib.sha256(plaintext).hexdigest() != digest
        ):
            raise PrivateV2TransportError("private v2 payload object drifted")
        key = secrets.token_bytes(_KEY_BYTES)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        aad = coding_canonical_json_bytes(
            {
                "schema": "dittobench-coding-private-v2-transport-aad-v1",
                "payload_sha256": payload["payload_sha256"],
                "catalog_sha256": payload["catalog_sha256"],
                "plaintext_sha256": digest,
                "plaintext_size_bytes": len(plaintext),
                "wrapping_key_sha256": wrapping_sha,
            },
            maximum_bytes=16 << 10,
            label="private v2 transport AAD",
        )
        aad_sha = hashlib.sha256(aad).hexdigest()
        ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)
        wrapped = public_key.encrypt(
            key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=bytes.fromhex(aad_sha),
            ),
        )
        _write_new(objects_dir / f"{digest}.bin", ciphertext)
        encrypted.append(
            {
                "plaintext_sha256": digest,
                "plaintext_size_bytes": len(plaintext),
                "ciphertext_relative_path": f"objects/{digest}.bin",
                "ciphertext_sha256": hashlib.sha256(ciphertext).hexdigest(),
                "ciphertext_size_bytes": len(ciphertext),
                "nonce_b64": base64.b64encode(nonce).decode("ascii"),
                "wrapped_data_key_b64": base64.b64encode(wrapped).decode("ascii"),
                "aad_sha256": aad_sha,
            }
        )
    projection = {
        "schema": "dittobench-coding-private-v2-transport-v1",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "payload_sha256": payload["payload_sha256"],
        "catalog_sha256": payload["catalog_sha256"],
        "catalog_merkle_root": payload["catalog_merkle_root"],
        "wrapping_key_sha256": wrapping_sha,
        "objects": encrypted,
    }
    manifest = {**projection, "transport_sha256": _digest(projection)}
    _write_new(
        output / "manifest.json",
        coding_canonical_json_bytes(
            manifest, maximum_bytes=16 << 20, label="private v2 transport manifest"
        ),
    )
    return manifest


def verify_private_v2_transport(directory: Path) -> dict[str, Any]:
    """Verify manifest, ciphertext bytes, and content-addressed object paths."""

    manifest = _canonical_object(directory / "manifest.json")
    projection = dict(manifest)
    transport_sha = projection.pop("transport_sha256", None)
    expected = {
        "schema",
        "coding_contract_version",
        "weight_eligible",
        "payload_sha256",
        "catalog_sha256",
        "catalog_merkle_root",
        "wrapping_key_sha256",
        "objects",
        "transport_sha256",
    }
    if (
        set(manifest) != expected
        or manifest["schema"] != "dittobench-coding-private-v2-transport-v1"
        or manifest["coding_contract_version"] != 2
        or manifest["weight_eligible"] is not False
        or not isinstance(transport_sha, str)
        or _digest(projection) != transport_sha
        or not isinstance(manifest["objects"], list)
    ):
        raise PrivateV2TransportError("private v2 transport manifest is invalid")
    seen: set[str] = set()
    for item in manifest["objects"]:
        if not isinstance(item, dict):
            raise PrivateV2TransportError("private v2 transport object is invalid")
        digest = item.get("plaintext_sha256")
        relative = item.get("ciphertext_relative_path")
        if (
            not isinstance(digest, str)
            or digest in seen
            or relative != f"objects/{digest}.bin"
            or not isinstance(item.get("ciphertext_size_bytes"), int)
            or item["ciphertext_size_bytes"] < 17
        ):
            raise PrivateV2TransportError("private v2 transport object is invalid")
        ciphertext = _read(directory / relative)
        if len(ciphertext) != item["ciphertext_size_bytes"] or hashlib.sha256(
            ciphertext
        ).hexdigest() != item.get("ciphertext_sha256"):
            raise PrivateV2TransportError("private v2 transport ciphertext drifted")
        seen.add(digest)
    return manifest


def _canonical_object(path: Path) -> dict[str, Any]:
    body = _read(path)
    try:
        value: Any = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrivateV2TransportError("private v2 transport JSON is invalid") from error
    if (
        not isinstance(value, dict)
        or coding_canonical_json_bytes(
            value, maximum_bytes=16 << 20, label="private v2 transport manifest"
        )
        != body
    ):
        raise PrivateV2TransportError("private v2 transport manifest is not canonical")
    return value


def _new_directory(path: Path) -> None:
    if (
        path.exists()
        or path.is_symlink()
        or path.parent.is_symlink()
        or not path.parent.is_dir()
        or stat.S_IMODE(path.parent.stat().st_mode) & 0o077
    ):
        raise PrivateV2TransportError("private v2 transport output is unsafe")
    path.mkdir(mode=0o700)


def _read(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 << 20:
        raise PrivateV2TransportError("private v2 transport artifact is invalid")
    return path.read_bytes()


def _write_new(path: Path, body: bytes) -> None:
    with path.open("xb") as output:
        output.write(body)
    path.chmod(0o600)


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        coding_canonical_json_bytes(
            value, maximum_bytes=16 << 20, label="private v2 transport manifest"
        )
    ).hexdigest()
