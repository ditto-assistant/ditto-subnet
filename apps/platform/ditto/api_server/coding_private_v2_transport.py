"""Offline AES-GCM/RSA-OAEP transport preparation for a v2 private payload."""

from __future__ import annotations

import base64
import hashlib
import json
import os
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
PRIVATE_V2_MAX_PLAINTEXT_BYTES = 128 << 20
PRIVATE_V2_MAX_CIPHERTEXT_BYTES = PRIVATE_V2_MAX_PLAINTEXT_BYTES + 16
_MAX_MANIFEST_BYTES = 16 << 20
_MIN_WRAPPED_KEY_BYTES = 3072 // 8
_MAX_WRAPPED_KEY_BYTES = 8192 // 8


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
        plaintext = _read(
            payload_directory / "objects" / f"{digest}.bin",
            maximum_bytes=PRIVATE_V2_MAX_PLAINTEXT_BYTES,
        )
        if (
            len(plaintext) != item["size_bytes"]
            or hashlib.sha256(plaintext).hexdigest() != digest
        ):
            raise PrivateV2TransportError("private v2 payload object drifted")
        key = secrets.token_bytes(_KEY_BYTES)
        nonce = secrets.token_bytes(_NONCE_BYTES)
        aad = _aad_bytes(
            payload_sha256=payload["payload_sha256"],
            catalog_sha256=payload["catalog_sha256"],
            plaintext_sha256=digest,
            plaintext_size_bytes=len(plaintext),
            wrapping_key_sha256=wrapping_sha,
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

    if (
        directory.is_symlink()
        or not directory.is_dir()
        or stat.S_IMODE(directory.stat().st_mode) & 0o077
    ):
        raise PrivateV2TransportError("private v2 transport directory is not protected")
    manifest = load_private_v2_transport_manifest(directory / "manifest.json")
    objects_dir = directory / "objects"
    if objects_dir.is_symlink() or not objects_dir.is_dir():
        raise PrivateV2TransportError("private v2 transport objects are invalid")
    on_disk: set[str] = set()
    for path in objects_dir.iterdir():
        if path.is_symlink() or not path.is_file() or not path.name.endswith(".bin"):
            raise PrivateV2TransportError("private v2 transport objects drifted")
        on_disk.add(path.name[: -len(".bin")])
    if on_disk != {item["plaintext_sha256"] for item in manifest["objects"]}:
        raise PrivateV2TransportError("private v2 transport objects drifted")
    for item in manifest["objects"]:
        read_private_v2_transport_ciphertext(directory=directory, item=item)
    return manifest


def load_private_v2_transport_manifest(path: Path) -> dict[str, Any]:
    """Validate transport metadata without requiring local ciphertext replicas."""

    manifest = _canonical_object(path)
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
        or not 1 <= len(manifest["objects"]) <= 1_000_000
        or any(
            not _sha256(manifest.get(name))
            for name in (
                "payload_sha256",
                "catalog_sha256",
                "catalog_merkle_root",
                "wrapping_key_sha256",
                "transport_sha256",
            )
        )
    ):
        raise PrivateV2TransportError("private v2 transport manifest is invalid")
    seen: set[str] = set()
    seen_ciphertexts: set[str] = set()
    previous_digest: str | None = None
    for item in manifest["objects"]:
        expected_fields = {
            "plaintext_sha256",
            "plaintext_size_bytes",
            "ciphertext_relative_path",
            "ciphertext_sha256",
            "ciphertext_size_bytes",
            "nonce_b64",
            "wrapped_data_key_b64",
            "aad_sha256",
        }
        if not isinstance(item, dict) or set(item) != expected_fields:
            raise PrivateV2TransportError("private v2 transport object is invalid")
        digest = item["plaintext_sha256"]
        relative = item["ciphertext_relative_path"]
        nonce = item["nonce_b64"]
        wrapped = item["wrapped_data_key_b64"]
        aad_sha = item["aad_sha256"]
        ciphertext_sha = item["ciphertext_sha256"]
        plaintext_size = item["plaintext_size_bytes"]
        ciphertext_size = item["ciphertext_size_bytes"]
        if (
            not isinstance(digest, str)
            or not isinstance(relative, str)
            or not isinstance(nonce, str)
            or not isinstance(wrapped, str)
            or not isinstance(aad_sha, str)
            or not isinstance(ciphertext_sha, str)
        ):
            raise PrivateV2TransportError("private v2 transport object is invalid")
        try:
            decoded_nonce = base64.b64decode(nonce, validate=True)
            decoded_wrapped = base64.b64decode(wrapped, validate=True)
        except (TypeError, ValueError) as error:
            raise PrivateV2TransportError(
                "private v2 transport object is invalid"
            ) from error
        if (
            not _sha256(digest)
            or digest in seen
            or (previous_digest is not None and digest <= previous_digest)
            or relative != f"objects/{digest}.bin"
            or type(plaintext_size) is not int
            or not 1 <= plaintext_size <= PRIVATE_V2_MAX_PLAINTEXT_BYTES
            or type(ciphertext_size) is not int
            or ciphertext_size != plaintext_size + 16
            or ciphertext_size > PRIVATE_V2_MAX_CIPHERTEXT_BYTES
            or not _sha256(ciphertext_sha)
            or ciphertext_sha in seen_ciphertexts
            or len(decoded_nonce) != _NONCE_BYTES
            or not _MIN_WRAPPED_KEY_BYTES
            <= len(decoded_wrapped)
            <= _MAX_WRAPPED_KEY_BYTES
            or not _sha256(aad_sha)
            or hashlib.sha256(
                _aad_bytes(
                    payload_sha256=manifest["payload_sha256"],
                    catalog_sha256=manifest["catalog_sha256"],
                    plaintext_sha256=digest,
                    plaintext_size_bytes=plaintext_size,
                    wrapping_key_sha256=manifest["wrapping_key_sha256"],
                )
            ).hexdigest()
            != aad_sha
        ):
            raise PrivateV2TransportError("private v2 transport object is invalid")
        seen.add(digest)
        seen_ciphertexts.add(ciphertext_sha)
        previous_digest = digest
    return manifest


def read_private_v2_transport_ciphertext(
    *, directory: Path, item: dict[str, Any]
) -> bytes:
    """Read one verified v2 ciphertext without following a replaced symlink."""

    digest = item.get("plaintext_sha256")
    relative = item.get("ciphertext_relative_path")
    expected_size = item.get("ciphertext_size_bytes")
    expected_sha = item.get("ciphertext_sha256")
    if (
        not _sha256(digest)
        or relative != f"objects/{digest}.bin"
        or type(expected_size) is not int
        or not 17 <= expected_size <= PRIVATE_V2_MAX_CIPHERTEXT_BYTES
        or not _sha256(expected_sha)
    ):
        raise PrivateV2TransportError("private v2 transport object is invalid")
    ciphertext = _read(
        directory / relative,
        maximum_bytes=PRIVATE_V2_MAX_CIPHERTEXT_BYTES,
    )
    if (
        len(ciphertext) != expected_size
        or hashlib.sha256(ciphertext).hexdigest() != expected_sha
    ):
        raise PrivateV2TransportError("private v2 transport ciphertext drifted")
    return ciphertext


def _aad_bytes(
    *,
    payload_sha256: str,
    catalog_sha256: str,
    plaintext_sha256: str,
    plaintext_size_bytes: int,
    wrapping_key_sha256: str,
) -> bytes:
    return coding_canonical_json_bytes(
        {
            "schema": "dittobench-coding-private-v2-transport-aad-v1",
            "payload_sha256": payload_sha256,
            "catalog_sha256": catalog_sha256,
            "plaintext_sha256": plaintext_sha256,
            "plaintext_size_bytes": plaintext_size_bytes,
            "wrapping_key_sha256": wrapping_key_sha256,
        },
        maximum_bytes=16 << 10,
        label="private v2 transport AAD",
    )


def _canonical_object(path: Path) -> dict[str, Any]:
    body = _read(path, maximum_bytes=_MAX_MANIFEST_BYTES)
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


def _read(path: Path, *, maximum_bytes: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PrivateV2TransportError(
            "private v2 transport artifact is unreadable"
        ) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= maximum_bytes:
            raise PrivateV2TransportError("private v2 transport artifact is invalid")
        body = bytearray()
        while len(body) < maximum_bytes + 1:
            chunk = os.read(descriptor, maximum_bytes + 1 - len(body))
            if not chunk:
                break
            body.extend(chunk)
    except PrivateV2TransportError:
        raise
    except OSError as error:
        raise PrivateV2TransportError(
            "private v2 transport artifact is unreadable"
        ) from error
    finally:
        os.close(descriptor)
    if not body or len(body) > maximum_bytes:
        raise PrivateV2TransportError("private v2 transport artifact is invalid")
    return bytes(body)


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


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
