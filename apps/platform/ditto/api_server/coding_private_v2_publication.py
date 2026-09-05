"""Signed exact-object Hippius publication for a private Coding v2 payload."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_hippius_probe import (
    HIPPIUS_PROVIDER_PROFILE_MAX_AGE,
    HippiusProbeReceiptError,
    load_hippius_probe_receipt,
)
from ditto.api_server.coding_hippius_publication import (
    HippiusPrivateInputConflict,
    HippiusPrivateInputNotFound,
    HippiusPrivateInputPublicationConfig,
    HippiusPrivateInputPublicationError,
    HippiusPrivateInputPublicationStatus,
    HippiusPrivateInputPublicationTransport,
    load_curator_signing_public_key,
)
from ditto.api_server.coding_private_v2_transport import (
    PrivateV2TransportError,
    read_private_v2_transport_ciphertext,
    verify_private_v2_transport,
)

PRIVATE_V2_PUBLICATION_CONFIRMATION = "PUBLISH HIPPIUS CODING PRIVATE V2 PAYLOAD"
PRIVATE_V2_PUBLICATION_RECEIPT_SCHEMA = "dittobench-coding-private-v2-publication-v1"
PRIVATE_V2_PUBLICATION_SIGNING_SCHEMA = (
    "dittobench-coding-private-v2-publication-signing-v1"
)
PRIVATE_V2_PUBLICATION_MAX_CIPHERTEXT_BYTES = (2 << 20) + 16
_REMOTE_PREFIX = "coding-private-inputs/v2"
_MAX_SIGNATURE_BYTES = 64
_MAX_RECEIPT_BYTES = 16 << 20
_MAX_OBJECTS = 1_000_000
_CLOCK_SKEW = timedelta(minutes=5)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")


class PrivateV2PublicationError(HippiusPrivateInputPublicationError):
    """A private v2 transport cannot be safely published or attested."""


@dataclass(frozen=True)
class PrivateV2PublicationObject:
    object_index: int
    remote_object_key_sha256: str
    ciphertext_sha256: str
    ciphertext_size_bytes: int
    status: HippiusPrivateInputPublicationStatus


@dataclass(frozen=True)
class PrivateV2PublicationReceipt:
    schema: str
    source_sha: str
    checked_at: str
    provider: str
    probe_receipt_payload_sha256: str
    private_input_authority_sha256: str
    transport_sha256: str
    payload_sha256: str
    catalog_sha256: str
    catalog_merkle_root: str
    wrapping_key_sha256: str
    curator_signing_key_sha256: str
    curator_signature_b64: str
    object_count: int
    objects: tuple[PrivateV2PublicationObject, ...]
    ready: bool
    shadow_only: bool
    weight_eligible: bool


async def publish_private_v2_to_hippius(
    *,
    config: HippiusPrivateInputPublicationConfig,
    transport: HippiusPrivateInputPublicationTransport,
    transport_directory: Path,
    probe_receipt_path: Path,
    curator_public_key_path: Path,
    curator_signature_path: Path,
    source_sha: str,
    now: datetime | None = None,
) -> PrivateV2PublicationReceipt:
    """Publish one signed v2 transport and verify every byte via the reader."""

    if not _source_sha(source_sha):
        raise PrivateV2PublicationError(
            "private v2 publication source SHA must be 40 lowercase hex"
        )
    checked_at = datetime.now(UTC) if now is None else now
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise PrivateV2PublicationError(
            "private v2 publication clock must be timezone-aware"
        )
    checked_at = checked_at.astimezone(UTC)
    try:
        probe, probe_sha256 = load_hippius_probe_receipt(probe_receipt_path)
        manifest = verify_private_v2_transport(transport_directory)
        public_key, signing_key_sha256 = load_curator_signing_public_key(
            curator_public_key_path
        )
    except (
        HippiusProbeReceiptError,
        HippiusPrivateInputPublicationError,
        PrivateV2TransportError,
    ) as error:
        raise PrivateV2PublicationError(
            "private v2 publication authorities are invalid"
        ) from error
    if any(
        item["ciphertext_size_bytes"] > PRIVATE_V2_PUBLICATION_MAX_CIPHERTEXT_BYTES
        for item in manifest["objects"]
    ):
        raise PrivateV2PublicationError(
            "private v2 ciphertext exceeds the reviewed provider profile"
        )
    probe_checked_at = datetime.fromisoformat(
        probe.checked_at.replace("Z", "+00:00")
    ).astimezone(UTC)
    if (
        probe_checked_at > checked_at + _CLOCK_SKEW
        or checked_at - probe_checked_at > HIPPIUS_PROVIDER_PROFILE_MAX_AGE
    ):
        raise PrivateV2PublicationError(
            "private v2 provider probe is outside the publication freshness window"
        )
    if probe.private_input_authority_sha256 != config.authority_sha256:
        raise PrivateV2PublicationError(
            "private v2 provider probe does not bind the current authority"
        )
    signature = _read_bounded_regular_file(
        curator_signature_path,
        maximum_bytes=_MAX_SIGNATURE_BYTES,
        label="private v2 curator signature",
    )
    if len(signature) != _MAX_SIGNATURE_BYTES:
        raise PrivateV2PublicationError("private v2 curator signature is invalid")
    message = private_v2_publication_signing_message(
        manifest=manifest,
        source_sha=source_sha,
        probe_receipt_payload_sha256=probe_sha256,
        private_input_authority_sha256=config.authority_sha256,
        curator_signing_key_sha256=signing_key_sha256,
    )
    try:
        public_key.verify(signature, message)
    except InvalidSignature as error:
        raise PrivateV2PublicationError(
            "private v2 curator signature does not verify"
        ) from error

    published: list[PrivateV2PublicationObject] = []
    for object_index, item in enumerate(manifest["objects"]):
        ciphertext = read_private_v2_transport_ciphertext(
            directory=transport_directory,
            item=item,
        )
        remote_key = private_v2_remote_object_key(
            transport_sha256=manifest["transport_sha256"],
            object_index=object_index,
        )
        try:
            existing = await transport.get_object(
                key=remote_key,
                max_bytes=item["ciphertext_size_bytes"],
            )
        except HippiusPrivateInputNotFound:
            await transport.put_object(
                key=remote_key,
                body=ciphertext,
                metadata={
                    "ciphertext-sha256": item["ciphertext_sha256"],
                    "object-index": str(object_index),
                    "transport-sha256": manifest["transport_sha256"],
                },
            )
            status = HippiusPrivateInputPublicationStatus.UPLOADED
        else:
            _verify_remote_ciphertext(existing=existing, expected_item=item)
            status = HippiusPrivateInputPublicationStatus.REUSED
        verified = await transport.get_object(
            key=remote_key,
            max_bytes=item["ciphertext_size_bytes"],
        )
        _verify_remote_ciphertext(existing=verified, expected_item=item)
        published.append(
            PrivateV2PublicationObject(
                object_index=object_index,
                remote_object_key_sha256=hashlib.sha256(
                    remote_key.encode("utf-8")
                ).hexdigest(),
                ciphertext_sha256=item["ciphertext_sha256"],
                ciphertext_size_bytes=item["ciphertext_size_bytes"],
                status=status,
            )
        )
    return _validated_receipt(
        PrivateV2PublicationReceipt(
            schema=PRIVATE_V2_PUBLICATION_RECEIPT_SCHEMA,
            source_sha=source_sha,
            checked_at=checked_at.isoformat().replace("+00:00", "Z"),
            provider="hippius",
            probe_receipt_payload_sha256=probe_sha256,
            private_input_authority_sha256=config.authority_sha256,
            transport_sha256=manifest["transport_sha256"],
            payload_sha256=manifest["payload_sha256"],
            catalog_sha256=manifest["catalog_sha256"],
            catalog_merkle_root=manifest["catalog_merkle_root"],
            wrapping_key_sha256=manifest["wrapping_key_sha256"],
            curator_signing_key_sha256=signing_key_sha256,
            curator_signature_b64=base64.b64encode(signature).decode("ascii"),
            object_count=len(published),
            objects=tuple(published),
            ready=True,
            shadow_only=True,
            weight_eligible=False,
        )
    )


def private_v2_publication_signing_message(
    *,
    manifest: Mapping[str, Any],
    source_sha: str,
    probe_receipt_payload_sha256: str,
    private_input_authority_sha256: str,
    curator_signing_key_sha256: str,
) -> bytes:
    """Return the exact canonical bytes an offline curator must sign."""

    objects = manifest.get("objects")
    if (
        not _source_sha(source_sha)
        or not isinstance(objects, list)
        or not 1 <= len(objects) <= _MAX_OBJECTS
        or any(
            not _sha256(value)
            for value in (
                probe_receipt_payload_sha256,
                private_input_authority_sha256,
                curator_signing_key_sha256,
                manifest.get("transport_sha256"),
                manifest.get("payload_sha256"),
                manifest.get("catalog_sha256"),
                manifest.get("catalog_merkle_root"),
                manifest.get("wrapping_key_sha256"),
            )
        )
        or manifest.get("schema") != "dittobench-coding-private-v2-transport-v1"
        or manifest.get("coding_contract_version") != 2
        or manifest.get("weight_eligible") is not False
    ):
        raise PrivateV2PublicationError(
            "private v2 publication signing authority is invalid"
        )
    try:
        return coding_canonical_json_bytes(
            {
                "catalog_merkle_root": manifest["catalog_merkle_root"],
                "catalog_sha256": manifest["catalog_sha256"],
                "curator_signing_key_sha256": curator_signing_key_sha256,
                "object_count": len(objects),
                "payload_sha256": manifest["payload_sha256"],
                "private_input_authority_sha256": private_input_authority_sha256,
                "probe_receipt_payload_sha256": probe_receipt_payload_sha256,
                "schema": PRIVATE_V2_PUBLICATION_SIGNING_SCHEMA,
                "shadow_only": True,
                "source_sha": source_sha,
                "transport_sha256": manifest["transport_sha256"],
                "weight_eligible": False,
                "wrapping_key_sha256": manifest["wrapping_key_sha256"],
            },
            maximum_bytes=16 << 10,
            label="private v2 publication signing message",
        )
    except ValueError as error:
        raise PrivateV2PublicationError(
            "private v2 publication signing message exceeds bounds"
        ) from error


def private_v2_remote_object_key(*, transport_sha256: str, object_index: int) -> str:
    """Derive an opaque, immutable remote identity without plaintext hashes."""

    if (
        not _sha256(transport_sha256)
        or type(object_index) is not int
        or not 0 <= object_index < _MAX_OBJECTS
    ):
        raise PrivateV2PublicationError("private v2 remote object identity is invalid")
    return f"{_REMOTE_PREFIX}/{transport_sha256}/objects/{object_index:06d}.bin"


def write_private_v2_publication_signing_message(
    *, message: bytes, output: Path
) -> str:
    """Write one create-only message for an external signing boundary."""

    if not isinstance(message, bytes) or not 1 <= len(message) <= 16 << 10:
        raise PrivateV2PublicationError(
            "private v2 publication signing message is invalid"
        )
    _write_exclusive_file(path=output, body=message)
    return hashlib.sha256(message).hexdigest()


def write_private_v2_publication_receipt(
    *, receipt: PrivateV2PublicationReceipt, output: Path
) -> str:
    """Write one canonical redacted receipt and return its payload digest."""

    receipt = _validated_receipt(receipt)
    payload = asdict(receipt)
    try:
        payload_bytes = coding_canonical_json_bytes(
            payload,
            maximum_bytes=_MAX_RECEIPT_BYTES,
            label="private v2 publication receipt",
        )
        payload_sha256 = hashlib.sha256(payload_bytes).hexdigest()
        document = coding_canonical_json_bytes(
            {**payload, "receipt_payload_sha256": payload_sha256},
            maximum_bytes=_MAX_RECEIPT_BYTES,
            label="private v2 publication receipt",
        )
    except ValueError as error:
        raise PrivateV2PublicationError(
            "private v2 publication receipt exceeds bounds"
        ) from error
    _write_exclusive_file(path=output, body=document)
    return payload_sha256


def load_private_v2_publication_receipt(
    path: Path,
    *,
    curator_public_key_path: Path,
) -> tuple[PrivateV2PublicationReceipt, str]:
    """Load one canonical receipt and verify the curator signature and keys."""

    body = _read_bounded_regular_file(
        path,
        maximum_bytes=_MAX_RECEIPT_BYTES,
        label="private v2 publication receipt",
    )
    try:
        raw = json.loads(body, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict):
            raise ValueError("receipt root is not an object")
        payload_sha256 = raw.pop("receipt_payload_sha256")
        raw_objects = raw.pop("objects")
        if not isinstance(raw_objects, list):
            raise ValueError("receipt objects are not a list")
        objects = tuple(_parse_receipt_object(item) for item in raw_objects)
        receipt = _validated_receipt(
            PrivateV2PublicationReceipt(objects=objects, **raw)
        )
        payload = asdict(receipt)
        payload_bytes = coding_canonical_json_bytes(
            payload,
            maximum_bytes=_MAX_RECEIPT_BYTES,
            label="private v2 publication receipt",
        )
        if (
            not _sha256(payload_sha256)
            or hashlib.sha256(payload_bytes).hexdigest() != payload_sha256
            or coding_canonical_json_bytes(
                {**payload, "receipt_payload_sha256": payload_sha256},
                maximum_bytes=_MAX_RECEIPT_BYTES,
                label="private v2 publication receipt",
            )
            != body
        ):
            raise ValueError("receipt digest or canonical bytes are invalid")
        public_key, signing_key_sha256 = load_curator_signing_public_key(
            curator_public_key_path
        )
        if signing_key_sha256 != receipt.curator_signing_key_sha256:
            raise ValueError("curator signing key drifted")
        message = private_v2_publication_signing_message(
            manifest={
                "schema": "dittobench-coding-private-v2-transport-v1",
                "coding_contract_version": 2,
                "weight_eligible": False,
                "transport_sha256": receipt.transport_sha256,
                "payload_sha256": receipt.payload_sha256,
                "catalog_sha256": receipt.catalog_sha256,
                "catalog_merkle_root": receipt.catalog_merkle_root,
                "wrapping_key_sha256": receipt.wrapping_key_sha256,
                "objects": [{}] * receipt.object_count,
            },
            source_sha=receipt.source_sha,
            probe_receipt_payload_sha256=receipt.probe_receipt_payload_sha256,
            private_input_authority_sha256=receipt.private_input_authority_sha256,
            curator_signing_key_sha256=signing_key_sha256,
        )
        public_key.verify(
            base64.b64decode(receipt.curator_signature_b64, validate=True),
            message,
        )
    except (
        KeyError,
        TypeError,
        ValueError,
        InvalidSignature,
        HippiusPrivateInputPublicationError,
        PrivateV2PublicationError,
    ) as error:
        raise PrivateV2PublicationError(
            "private v2 publication receipt is invalid or incomplete"
        ) from error
    return receipt, payload_sha256


def _validated_receipt(
    receipt: PrivateV2PublicationReceipt,
) -> PrivateV2PublicationReceipt:
    try:
        signature = base64.b64decode(receipt.curator_signature_b64, validate=True)
        checked_at = datetime.fromisoformat(receipt.checked_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise PrivateV2PublicationError(
            "private v2 publication receipt is inconsistent"
        ) from error
    if (
        receipt.schema != PRIVATE_V2_PUBLICATION_RECEIPT_SCHEMA
        or not _source_sha(receipt.source_sha)
        or not receipt.checked_at.endswith("Z")
        or checked_at.tzinfo is None
        or checked_at.utcoffset() is None
        or checked_at.utcoffset() != timedelta(0)
        or receipt.provider != "hippius"
        or any(
            not _sha256(value)
            for value in (
                receipt.probe_receipt_payload_sha256,
                receipt.private_input_authority_sha256,
                receipt.transport_sha256,
                receipt.payload_sha256,
                receipt.catalog_sha256,
                receipt.catalog_merkle_root,
                receipt.wrapping_key_sha256,
                receipt.curator_signing_key_sha256,
            )
        )
        or len(signature) != _MAX_SIGNATURE_BYTES
        or type(receipt.object_count) is not int
        or receipt.object_count != len(receipt.objects)
        or not 1 <= receipt.object_count <= _MAX_OBJECTS
        or receipt.ready is not True
        or receipt.shadow_only is not True
        or receipt.weight_eligible is not False
    ):
        raise PrivateV2PublicationError(
            "private v2 publication receipt is inconsistent"
        )
    remote_keys: set[str] = set()
    ciphertexts: set[str] = set()
    for expected_index, item in enumerate(receipt.objects):
        expected_remote = hashlib.sha256(
            private_v2_remote_object_key(
                transport_sha256=receipt.transport_sha256,
                object_index=expected_index,
            ).encode("utf-8")
        ).hexdigest()
        if (
            item.object_index != expected_index
            or item.remote_object_key_sha256 != expected_remote
            or not _sha256(item.ciphertext_sha256)
            or type(item.ciphertext_size_bytes) is not int
            or not 17
            <= item.ciphertext_size_bytes
            <= PRIVATE_V2_PUBLICATION_MAX_CIPHERTEXT_BYTES
            or item.status
            not in {
                HippiusPrivateInputPublicationStatus.UPLOADED,
                HippiusPrivateInputPublicationStatus.REUSED,
            }
            or item.remote_object_key_sha256 in remote_keys
            or item.ciphertext_sha256 in ciphertexts
        ):
            raise PrivateV2PublicationError(
                "private v2 publication receipt object is inconsistent"
            )
        remote_keys.add(item.remote_object_key_sha256)
        ciphertexts.add(item.ciphertext_sha256)
    return receipt


def _parse_receipt_object(raw: object) -> PrivateV2PublicationObject:
    if not isinstance(raw, dict) or set(raw) != {
        "object_index",
        "remote_object_key_sha256",
        "ciphertext_sha256",
        "ciphertext_size_bytes",
        "status",
    }:
        raise ValueError("receipt object fields are invalid")
    return PrivateV2PublicationObject(
        object_index=raw["object_index"],
        remote_object_key_sha256=raw["remote_object_key_sha256"],
        ciphertext_sha256=raw["ciphertext_sha256"],
        ciphertext_size_bytes=raw["ciphertext_size_bytes"],
        status=HippiusPrivateInputPublicationStatus(raw["status"]),
    )


def _verify_remote_ciphertext(
    *, existing: bytes, expected_item: Mapping[str, Any]
) -> None:
    if (
        len(existing) != expected_item["ciphertext_size_bytes"]
        or hashlib.sha256(existing).hexdigest() != expected_item["ciphertext_sha256"]
    ):
        raise HippiusPrivateInputConflict(
            "Hippius private v2 identity contains different bytes"
        )


def _read_bounded_regular_file(path: Path, *, maximum_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PrivateV2PublicationError(f"{label} is unreadable") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= maximum_bytes:
            raise PrivateV2PublicationError(f"{label} is invalid")
        body = bytearray()
        while len(body) < maximum_bytes + 1:
            chunk = os.read(descriptor, maximum_bytes + 1 - len(body))
            if not chunk:
                break
            body.extend(chunk)
    except PrivateV2PublicationError:
        raise
    except OSError as error:
        raise PrivateV2PublicationError(f"{label} is unreadable") from error
    finally:
        os.close(descriptor)
    if not body or len(body) > maximum_bytes:
        raise PrivateV2PublicationError(f"{label} exceeds bounds")
    return bytes(body)


def _write_exclusive_file(*, path: Path, body: bytes) -> None:
    parent = path.parent
    if (
        not path.is_absolute()
        or path.exists()
        or path.is_symlink()
        or parent.is_symlink()
        or not parent.is_dir()
        or stat.S_IMODE(parent.stat().st_mode) & 0o077
    ):
        raise PrivateV2PublicationError(
            "private v2 publication output must be new in a protected directory"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise PrivateV2PublicationError(
            "private v2 publication output cannot be created"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise PrivateV2PublicationError(
                    "private v2 publication output write made no progress"
                )
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _source_sha(value: object) -> bool:
    return isinstance(value, str) and _SOURCE_SHA.fullmatch(value) is not None


__all__ = [
    "PRIVATE_V2_PUBLICATION_CONFIRMATION",
    "PRIVATE_V2_PUBLICATION_MAX_CIPHERTEXT_BYTES",
    "PRIVATE_V2_PUBLICATION_RECEIPT_SCHEMA",
    "PRIVATE_V2_PUBLICATION_SIGNING_SCHEMA",
    "PrivateV2PublicationError",
    "PrivateV2PublicationObject",
    "PrivateV2PublicationReceipt",
    "load_private_v2_publication_receipt",
    "private_v2_publication_signing_message",
    "private_v2_remote_object_key",
    "publish_private_v2_to_hippius",
    "write_private_v2_publication_receipt",
    "write_private_v2_publication_signing_message",
]
