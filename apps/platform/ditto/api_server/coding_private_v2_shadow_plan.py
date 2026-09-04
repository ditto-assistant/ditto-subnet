"""Create immutable v2 registration and shadow-canary authorities offline."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from typing import Any

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_private_catalog_v2_compile import (
    PrivateCatalogV2CompileError,
    verify_private_catalog_v2,
)
from ditto.api_server.coding_private_v2_payload import (
    PrivateV2PayloadError,
    verify_private_v2_payload,
)
from ditto.api_server.coding_private_v2_transport import (
    PrivateV2TransportError,
    verify_private_v2_transport,
)


class PrivateV2ShadowPlanError(ValueError):
    """Private v2 authorities cannot become a registration or canary plan."""


def build_private_v2_registration_authority(
    *,
    catalog_directory: Path,
    payload_directory: Path,
    transport_directory: Path,
    publication_receipt_sha256: str,
    output: Path,
) -> dict[str, Any]:
    """Bind independently verified artifacts into one append-only-ready record."""

    if not _sha256(publication_receipt_sha256):
        raise PrivateV2ShadowPlanError("publication receipt identity is invalid")
    try:
        catalog = verify_private_catalog_v2(catalog_directory)
        payload = verify_private_v2_payload(payload_directory)
        transport = verify_private_v2_transport(transport_directory)
    except (
        PrivateCatalogV2CompileError,
        PrivateV2PayloadError,
        PrivateV2TransportError,
    ) as error:
        raise PrivateV2ShadowPlanError("private v2 inputs are invalid") from error
    if (
        payload["catalog_sha256"] != catalog["catalog_sha256"]
        or transport["catalog_sha256"] != catalog["catalog_sha256"]
        or transport["payload_sha256"] != payload["payload_sha256"]
    ):
        raise PrivateV2ShadowPlanError("private v2 authority linkage drifted")
    projection = {
        "schema": "dittobench-coding-private-v2-registration-v1",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "shadow_only": True,
        "corpus_release_id": catalog["corpus_release_id"],
        "private_release_sha256": catalog["private_release_sha256"],
        "catalog_sha256": catalog["catalog_sha256"],
        "catalog_merkle_root": catalog["catalog_merkle_root"],
        "payload_sha256": payload["payload_sha256"],
        "transport_sha256": transport["transport_sha256"],
        "wrapping_key_sha256": transport["wrapping_key_sha256"],
        "publication_receipt_sha256": publication_receipt_sha256,
        "previous_registration_sha256": None,
    }
    authority = {**projection, "registration_sha256": _digest(projection)}
    _write_new(
        output,
        coding_canonical_json_bytes(
            authority, maximum_bytes=64 << 10, label="private v2 registration authority"
        ),
    )
    return authority


def build_private_v2_shadow_canary(
    *,
    registration_authority: Path,
    catalog_directory: Path,
    catalog_index: int,
    output: Path,
) -> dict[str, Any]:
    """Create a single-leaf default-off canary authority; no lease is issued."""

    registration = _canonical_object(registration_authority, "private v2 registration")
    try:
        catalog = verify_private_catalog_v2(catalog_directory)
    except PrivateCatalogV2CompileError as error:
        raise PrivateV2ShadowPlanError("private v2 catalog is invalid") from error
    expected = {
        "schema",
        "coding_contract_version",
        "weight_eligible",
        "shadow_only",
        "corpus_release_id",
        "private_release_sha256",
        "catalog_sha256",
        "catalog_merkle_root",
        "payload_sha256",
        "transport_sha256",
        "wrapping_key_sha256",
        "publication_receipt_sha256",
        "previous_registration_sha256",
        "registration_sha256",
    }
    if (
        set(registration) != expected
        or registration.get("schema")
        != "dittobench-coding-private-v2-registration-v1"
        or registration.get("coding_contract_version") != 2
        or registration.get("registration_sha256")
        != _digest(
            {
                key: value
                for key, value in registration.items()
                if key != "registration_sha256"
            }
        )
        or registration.get("shadow_only") is not True
        or registration.get("weight_eligible") is not False
        or registration.get("catalog_sha256") != catalog["catalog_sha256"]
        or not 0 <= catalog_index < catalog["task_version_count"]
    ):
        raise PrivateV2ShadowPlanError("private v2 registration authority is invalid")
    record = catalog["records"][catalog_index]
    projection = {
        "schema": "dittobench-coding-private-v2-shadow-canary-v1",
        "coding_contract_version": 2,
        "weight_eligible": False,
        "shadow_only": True,
        "registration_sha256": registration["registration_sha256"],
        "catalog_sha256": catalog["catalog_sha256"],
        "catalog_index": catalog_index,
        "task_version_id": record["task_version_id"],
        "task_commitment_sha256": record["task_commitment_sha256"],
        "record_sha256": record["record_sha256"],
        "proof_sha256": record["proof_sha256"],
    }
    canary = {**projection, "canary_sha256": _digest(projection)}
    _write_new(
        output,
        coding_canonical_json_bytes(
            canary, maximum_bytes=64 << 10, label="private v2 shadow canary"
        ),
    )
    return canary


def _canonical_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise PrivateV2ShadowPlanError(f"{label} is unavailable")
    try:
        body = path.read_bytes()
        value: Any = json.loads(body)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PrivateV2ShadowPlanError(f"{label} is invalid") from error
    if (
        not isinstance(value, dict)
        or coding_canonical_json_bytes(value, maximum_bytes=64 << 10, label=label)
        != body
    ):
        raise PrivateV2ShadowPlanError(f"{label} is not canonical")
    return value


def _write_new(path: Path, body: bytes) -> None:
    if (
        path.exists()
        or path.is_symlink()
        or path.parent.is_symlink()
        or not path.parent.is_dir()
        or stat.S_IMODE(path.parent.stat().st_mode) & 0o077
    ):
        raise PrivateV2ShadowPlanError("private v2 output is unsafe")
    with path.open("xb") as file:
        file.write(body)
    path.chmod(0o600)


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        coding_canonical_json_bytes(
            value, maximum_bytes=64 << 10, label="private v2 authority"
        )
    ).hexdigest()
