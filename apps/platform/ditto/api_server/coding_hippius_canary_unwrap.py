"""Prepare the exact two-request authority for the isolated canary unwrap service."""

from __future__ import annotations

import base64
import hashlib
import os
import stat
from dataclasses import replace
from pathlib import Path

from ditto.api_models.coding_artifacts import CodingArtifactDeliveryPhase
from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_server.coding_hippius_canary import (
    HIPPIUS_SHADOW_CANARY_CORPUS_PREFIX,
)
from ditto.api_server.coding_hippius_canary_operator import (
    load_hippius_shadow_canary_plan,
)
from ditto.api_server.coding_hippius_encryption import (
    load_hippius_private_input_transport_manifest,
)
from ditto.api_server.coding_hippius_publication import (
    load_hippius_private_input_publication_receipt,
)
from ditto.api_server.coding_hippius_retrieval import (
    build_hippius_private_input_unwrap_request,
)

HIPPIUS_CANARY_UNWRAP_AUTHORITY_CONFIRMATION = "PREPARE HIPPIUS CANARY UNWRAP AUTHORITY"

_AUTHORITY_SCHEMA = "dittobench-coding-hippius-canary-unwrap-authority-v1"
_MAX_AUTHORITY_BYTES = 64 << 10


class HippiusCanaryUnwrapAuthorityError(RuntimeError):
    """The protected canary inputs cannot produce one exact unwrap authority."""


def prepare_hippius_canary_unwrap_authority(
    *,
    plan_path: Path,
    manifest_path: Path,
    publication_receipt_path: Path,
    confirmation: str,
) -> dict[str, object]:
    if confirmation != HIPPIUS_CANARY_UNWRAP_AUTHORITY_CONFIRMATION:
        raise HippiusCanaryUnwrapAuthorityError(
            "Hippius canary unwrap authority is not confirmed"
        )
    try:
        plan = load_hippius_shadow_canary_plan(plan_path)
        manifest = load_hippius_private_input_transport_manifest(manifest_path)
        receipt, receipt_payload_sha256 = (
            load_hippius_private_input_publication_receipt(publication_receipt_path)
        )
        private = plan.private_input
        if (
            plan.synthetic_only is not True
            or plan.single_validator is not True
            or plan.weight_eligible is not False
            or not plan.synthetic_corpus_release_id.startswith(
                HIPPIUS_SHADOW_CANARY_CORPUS_PREFIX
            )
            or plan.synthetic_corpus_release_id != private.commitment.corpus_release_id
            or private.delivery_phase is not CodingArtifactDeliveryPhase.AUTHORING
            or private.catalog_index >= len(manifest.objects)
            or private.commitment.commitment_sha256
            != manifest.catalog_commitment_sha256
            or private.transport_manifest_sha256 != manifest.transport_manifest_sha256
            or private.publication_receipt_payload_sha256 != receipt_payload_sha256
            or receipt.transport_manifest_sha256 != manifest.transport_manifest_sha256
            or receipt.catalog_commitment_sha256 != manifest.catalog_commitment_sha256
            or receipt_payload_sha256 != private.publication_receipt_payload_sha256
        ):
            raise ValueError("registered canary transport is inconsistent")
        item = manifest.objects[private.catalog_index]
        receipt_item = receipt.objects[private.catalog_index]
        if (
            item.catalog_index != private.catalog_index
            or receipt_item.catalog_index != private.catalog_index
            or receipt_item.ciphertext_sha256 != item.ciphertext_sha256
            or receipt_item.ciphertext_size_bytes != item.ciphertext_size_bytes
        ):
            raise ValueError("selected canary object is inconsistent")
        requests = []
        allowed_requests = []
        for phase in (
            CodingArtifactDeliveryPhase.AUTHORING,
            CodingArtifactDeliveryPhase.GRADING,
        ):
            request = build_hippius_private_input_unwrap_request(
                authority=replace(private, delivery_phase=phase),
                manifest=manifest,
                item=item,
                publication_receipt_payload_sha256=receipt_payload_sha256,
            )
            wrapped_sha256 = hashlib.sha256(request.wrapped_data_key).hexdigest()
            requests.append(request)
            allowed_requests.append(
                {
                    "aad_sha256": request.aad_sha256,
                    "ciphertext_sha256": request.ciphertext_sha256,
                    "delivery_phase": request.delivery_phase.value,
                    "request_sha256": request.request_sha256,
                    "wrapped_data_key_sha256": wrapped_sha256,
                }
            )
        if (
            requests[0].wrapped_data_key != requests[1].wrapped_data_key
            or requests[0].aad_sha256 != requests[1].aad_sha256
            or requests[0].ciphertext_sha256 != requests[1].ciphertext_sha256
            or base64.b64decode(item.wrapped_data_key_b64, validate=True)
            != requests[0].wrapped_data_key
        ):
            raise ValueError("canary unwrap phases do not bind one object")
        projection: dict[str, object] = {
            "allowed_requests": allowed_requests,
            "assignment_sha256": private.assignment_sha256,
            "catalog_commitment_sha256": private.commitment.commitment_sha256,
            "catalog_index": private.catalog_index,
            "coding_run_id": private.coding_run_id,
            "publication_receipt_payload_sha256": receipt_payload_sha256,
            "run_manifest_sha256": private.run_manifest_sha256,
            "run_row_id": str(private.run_row_id),
            "schema": _AUTHORITY_SCHEMA,
            "single_validator": True,
            "source_sha": plan.source_sha,
            "synthetic_only": True,
            "ticket_deadline": private.ticket_deadline.isoformat().replace(
                "+00:00", "Z"
            ),
            "ticket_id": str(private.ticket_id),
            "transport_manifest_sha256": manifest.transport_manifest_sha256,
            "validator_hotkey": private.validator_hotkey,
            "weight_eligible": False,
            "wrapping_key_sha256": manifest.wrapping_key_sha256,
        }
        authority_sha256 = hashlib.sha256(
            coding_canonical_json_bytes(
                projection,
                maximum_bytes=_MAX_AUTHORITY_BYTES,
                label="Hippius canary unwrap authority",
            )
        ).hexdigest()
        return {**projection, "authority_sha256": authority_sha256}
    except HippiusCanaryUnwrapAuthorityError:
        raise
    except Exception as error:
        raise HippiusCanaryUnwrapAuthorityError(
            "Hippius canary unwrap authority inputs are invalid"
        ) from error


def write_hippius_canary_unwrap_authority(
    *,
    authority: dict[str, object],
    output: Path,
) -> str:
    if not output.is_absolute():
        raise HippiusCanaryUnwrapAuthorityError(
            "Hippius canary unwrap authority output must be absolute"
        )
    body = coding_canonical_json_bytes(
        authority,
        maximum_bytes=_MAX_AUTHORITY_BYTES,
        label="Hippius canary unwrap authority",
    )
    authority_sha256 = str(authority.get("authority_sha256", ""))
    projection = dict(authority)
    projection.pop("authority_sha256", None)
    if (
        authority.get("schema") != _AUTHORITY_SCHEMA
        or authority.get("synthetic_only") is not True
        or authority.get("single_validator") is not True
        or authority.get("weight_eligible") is not False
        or hashlib.sha256(
            coding_canonical_json_bytes(
                projection,
                maximum_bytes=_MAX_AUTHORITY_BYTES,
                label="Hippius canary unwrap authority projection",
            )
        ).hexdigest()
        != authority_sha256
    ):
        raise HippiusCanaryUnwrapAuthorityError(
            "Hippius canary unwrap authority digest is invalid"
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output, flags, 0o600)
    except OSError as error:
        raise HippiusCanaryUnwrapAuthorityError(
            "Hippius canary unwrap authority output is unsafe"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HippiusCanaryUnwrapAuthorityError(
                "Hippius canary unwrap authority output is invalid"
            )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise HippiusCanaryUnwrapAuthorityError(
                    "Hippius canary unwrap authority write made no progress"
                )
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        output.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    return authority_sha256


__all__ = [
    "HIPPIUS_CANARY_UNWRAP_AUTHORITY_CONFIRMATION",
    "HippiusCanaryUnwrapAuthorityError",
    "prepare_hippius_canary_unwrap_authority",
    "write_hippius_canary_unwrap_authority",
]
