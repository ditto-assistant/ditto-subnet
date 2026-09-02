"""Offline client-side envelope encryption for Hippius private Coding inputs."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_catalog_publication import (
    CodingCatalogPublicationObject,
    CodingCatalogPublicationPlan,
    plan_private_catalog_publication,
    read_private_catalog_publication_object,
)

HIPPIUS_PRIVATE_INPUT_ENCRYPTION_CONFIRMATION = "ENCRYPT HIPPIUS CODING PRIVATE INPUTS"
_ENCRYPTION_SCHEMA = "dittobench-coding-hippius-private-input-transport-v1"
_AAD_SCHEMA = "dittobench-coding-hippius-private-input-aad-v1"
_ALGORITHM = "AES-256-GCM"
_WRAPPING_ALGORITHM = "RSA-OAEP-SHA256"
_DATA_KEY_BYTES = 32
_NONCE_BYTES = 12
_MIN_RSA_BITS = 3072
_MAX_RSA_BITS = 8192
_MAX_PUBLIC_KEY_BYTES = 64 << 10
_MAX_MANIFEST_BYTES = (1_000_000 * (12 << 10)) + (1 << 20)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HippiusPrivateInputEncryptionError(ValueError):
    """Protected curator input cannot become a safe encrypted transport."""


@dataclass(frozen=True)
class HippiusEncryptedPrivateInputObject:
    catalog_index: int
    logical_object_key: str
    ciphertext_relative_path: str
    plaintext_sha256: str
    plaintext_size_bytes: int
    ciphertext_sha256: str
    ciphertext_size_bytes: int
    nonce_b64: str
    wrapped_data_key_b64: str
    aad_sha256: str
    task_commitment_sha256: str
    task_version_id: str

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HippiusPrivateInputTransportManifest:
    catalog_commitment_sha256: str
    coding_contract_version: int
    corpus_release_id: str
    publication_sha256: str
    wrapping_key_sha256: str
    objects: tuple[HippiusEncryptedPrivateInputObject, ...]
    transport_manifest_sha256: str

    def as_json(self) -> dict[str, Any]:
        return {
            **_manifest_projection(self),
            "transport_manifest_sha256": self.transport_manifest_sha256,
        }


def prepare_hippius_private_input_transport(
    *,
    commitment_path: Path,
    records_dir: Path,
    wrapping_public_key_path: Path,
    output_dir: Path,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> HippiusPrivateInputTransportManifest:
    """Encrypt a verified catalog into one new local transport directory."""

    plan = plan_private_catalog_publication(
        commitment_path=commitment_path,
        records_dir=records_dir,
    )
    public_key, wrapping_key_sha256 = load_hippius_wrapping_public_key(
        wrapping_public_key_path
    )
    output_dir = _validate_new_output_directory(output_dir)
    try:
        output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
        os.chmod(output_dir, 0o700)
        objects_dir = output_dir / "objects"
        objects_dir.mkdir(mode=0o700)
        encrypted: list[HippiusEncryptedPrivateInputObject] = []
        for item in plan.objects:
            plaintext = read_private_catalog_publication_object(
                records_dir=records_dir,
                item=item,
            )
            data_key = random_bytes(_DATA_KEY_BYTES)
            nonce = random_bytes(_NONCE_BYTES)
            if len(data_key) != _DATA_KEY_BYTES or len(nonce) != _NONCE_BYTES:
                raise HippiusPrivateInputEncryptionError(
                    "private-input entropy source returned the wrong byte count"
                )
            aad = hippius_private_input_aad_bytes(
                plan=plan,
                item=item,
                wrapping_key_sha256=wrapping_key_sha256,
            )
            aad_sha256 = hashlib.sha256(aad).hexdigest()
            ciphertext = AESGCM(data_key).encrypt(nonce, plaintext, aad)
            wrapped_data_key = public_key.encrypt(
                data_key,
                padding.OAEP(
                    mgf=padding.MGF1(algorithm=hashes.SHA256()),
                    algorithm=hashes.SHA256(),
                    label=bytes.fromhex(aad_sha256),
                ),
            )
            relative_path = f"objects/{item.catalog_index:06d}.bin"
            _write_exclusive_file(
                path=output_dir / relative_path,
                body=ciphertext,
            )
            encrypted.append(
                HippiusEncryptedPrivateInputObject(
                    catalog_index=item.catalog_index,
                    logical_object_key=item.object_key,
                    ciphertext_relative_path=relative_path,
                    plaintext_sha256=item.record_sha256,
                    plaintext_size_bytes=item.record_size_bytes,
                    ciphertext_sha256=hashlib.sha256(ciphertext).hexdigest(),
                    ciphertext_size_bytes=len(ciphertext),
                    nonce_b64=base64.b64encode(nonce).decode("ascii"),
                    wrapped_data_key_b64=base64.b64encode(wrapped_data_key).decode(
                        "ascii"
                    ),
                    aad_sha256=aad_sha256,
                    task_commitment_sha256=item.task_commitment_sha256,
                    task_version_id=item.task_version_id,
                )
            )
        manifest = _build_manifest(
            plan=plan,
            wrapping_key_sha256=wrapping_key_sha256,
            objects=tuple(encrypted),
        )
        _sync_directory(objects_dir)
        _write_exclusive_file(
            path=output_dir / "manifest.json",
            body=_manifest_bytes(manifest),
        )
        _sync_directory(output_dir)
        _sync_directory(output_dir.parent)
        return manifest
    except FileExistsError as error:
        raise HippiusPrivateInputEncryptionError(
            "private-input transport output already exists"
        ) from error


def load_hippius_wrapping_public_key(
    path: Path,
) -> tuple[rsa.RSAPublicKey, str]:
    """Load a bounded RSA public key and return its stable SPKI digest."""

    body = _read_bounded_regular_file(
        path,
        maximum_bytes=_MAX_PUBLIC_KEY_BYTES,
        label="wrapping public key",
    )
    try:
        loaded = serialization.load_pem_public_key(body)
    except (TypeError, ValueError) as error:
        raise HippiusPrivateInputEncryptionError(
            "wrapping public key is invalid"
        ) from error
    if (
        not isinstance(loaded, rsa.RSAPublicKey)
        or not _MIN_RSA_BITS <= loaded.key_size <= _MAX_RSA_BITS
        or loaded.public_numbers().e != 65_537
    ):
        raise HippiusPrivateInputEncryptionError(
            "wrapping public key must be RSA-3072 through RSA-8192 with exponent 65537"
        )
    der = loaded.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return loaded, hashlib.sha256(der).hexdigest()


def hippius_private_input_aad_bytes(
    *,
    plan: CodingCatalogPublicationPlan,
    item: CodingCatalogPublicationObject,
    wrapping_key_sha256: str,
) -> bytes:
    """Return the canonical public preimage authenticated by AES-GCM."""

    if _SHA256.fullmatch(wrapping_key_sha256) is None:
        raise HippiusPrivateInputEncryptionError(
            "wrapping public key identity is invalid"
        )
    projection = {
        "aad_schema": _AAD_SCHEMA,
        "catalog_commitment_sha256": plan.commitment.commitment_sha256,
        "catalog_index": item.catalog_index,
        "logical_object_key": item.object_key,
        "plaintext_sha256": item.record_sha256,
        "plaintext_size_bytes": item.record_size_bytes,
        "publication_sha256": plan.publication_sha256,
        "task_commitment_sha256": item.task_commitment_sha256,
        "task_version_id": item.task_version_id,
        "wrapping_key_sha256": wrapping_key_sha256,
    }
    try:
        return coding_canonical_json_bytes(
            projection,
            maximum_bytes=16 << 10,
            label="Hippius private-input AAD",
        )
    except ValueError as error:
        raise HippiusPrivateInputEncryptionError(
            "private-input AAD is outside canonical bounds"
        ) from error


def _build_manifest(
    *,
    plan: CodingCatalogPublicationPlan,
    wrapping_key_sha256: str,
    objects: tuple[HippiusEncryptedPrivateInputObject, ...],
) -> HippiusPrivateInputTransportManifest:
    draft = HippiusPrivateInputTransportManifest(
        catalog_commitment_sha256=plan.commitment.commitment_sha256,
        coding_contract_version=plan.commitment.coding_contract_version,
        corpus_release_id=plan.commitment.corpus_release_id,
        publication_sha256=plan.publication_sha256,
        wrapping_key_sha256=wrapping_key_sha256,
        objects=objects,
        transport_manifest_sha256="0" * 64,
    )
    digest = hashlib.sha256(
        coding_canonical_json_bytes(
            _manifest_projection(draft),
            maximum_bytes=_MAX_MANIFEST_BYTES,
            label="Hippius private-input transport manifest",
        )
    ).hexdigest()
    return replace(draft, transport_manifest_sha256=digest)


def _manifest_projection(
    manifest: HippiusPrivateInputTransportManifest,
) -> dict[str, Any]:
    return {
        "algorithm": _ALGORITHM,
        "catalog_commitment_sha256": manifest.catalog_commitment_sha256,
        "coding_contract_version": manifest.coding_contract_version,
        "corpus_release_id": manifest.corpus_release_id,
        "objects": [item.as_json() for item in manifest.objects],
        "publication_sha256": manifest.publication_sha256,
        "schema": _ENCRYPTION_SCHEMA,
        "weight_eligible": False,
        "wrapping_algorithm": _WRAPPING_ALGORITHM,
        "wrapping_key_sha256": manifest.wrapping_key_sha256,
    }


def _manifest_bytes(manifest: HippiusPrivateInputTransportManifest) -> bytes:
    projection = _manifest_projection(manifest)
    try:
        expected = hashlib.sha256(
            coding_canonical_json_bytes(
                projection,
                maximum_bytes=_MAX_MANIFEST_BYTES,
                label="Hippius private-input transport manifest",
            )
        ).hexdigest()
        if manifest.transport_manifest_sha256 != expected:
            raise HippiusPrivateInputEncryptionError(
                "private-input transport manifest digest is invalid"
            )
        return coding_canonical_json_bytes(
            {
                **projection,
                "transport_manifest_sha256": manifest.transport_manifest_sha256,
            },
            maximum_bytes=_MAX_MANIFEST_BYTES,
            label="Hippius private-input transport manifest",
        )
    except ValueError as error:
        raise HippiusPrivateInputEncryptionError(
            "private-input transport manifest exceeds bounds"
        ) from error


def _validate_new_output_directory(output_dir: Path) -> Path:
    if not output_dir.is_absolute():
        raise HippiusPrivateInputEncryptionError(
            "private-input transport output must be absolute"
        )
    if output_dir.exists() or output_dir.is_symlink():
        raise HippiusPrivateInputEncryptionError(
            "private-input transport output must not exist"
        )
    parent = output_dir.parent
    if parent.is_symlink() or not parent.is_dir():
        raise HippiusPrivateInputEncryptionError(
            "private-input transport parent is invalid"
        )
    return output_dir


def _read_bounded_regular_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HippiusPrivateInputEncryptionError(f"{label} is unreadable") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= maximum_bytes:
            raise HippiusPrivateInputEncryptionError(f"{label} is invalid")
        body = bytearray()
        while len(body) < maximum_bytes + 1:
            chunk = os.read(descriptor, maximum_bytes + 1 - len(body))
            if not chunk:
                break
            body.extend(chunk)
    except HippiusPrivateInputEncryptionError:
        raise
    except OSError as error:
        raise HippiusPrivateInputEncryptionError(f"{label} is unreadable") from error
    finally:
        os.close(descriptor)
    if not body or len(body) > maximum_bytes:
        raise HippiusPrivateInputEncryptionError(f"{label} exceeds bounds")
    return bytes(body)


def _write_exclusive_file(*, path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise HippiusPrivateInputEncryptionError(
            "private-input transport file cannot be created"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise HippiusPrivateInputEncryptionError(
                    "private-input transport write made no progress"
                )
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise HippiusPrivateInputEncryptionError(
            "private-input transport directory sync failed"
        ) from error


__all__ = [
    "HIPPIUS_PRIVATE_INPUT_ENCRYPTION_CONFIRMATION",
    "HippiusEncryptedPrivateInputObject",
    "HippiusPrivateInputEncryptionError",
    "HippiusPrivateInputTransportManifest",
    "hippius_private_input_aad_bytes",
    "load_hippius_wrapping_public_key",
    "prepare_hippius_private_input_transport",
]
