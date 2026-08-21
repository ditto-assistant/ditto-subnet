"""Signed shadow coding-catalog commitments and exposure contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ditto.api_models.coding_evaluation import (
    BlockHash,
    CodingEvaluationModel,
    OpaqueId,
    Sha256,
    ShortName,
)

_OCI_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_SS58_PATTERN = r"^[1-9A-HJ-NP-Za-km-z]{47,48}$"
_SIGNATURE_PATTERN = r"^[0-9a-fA-F]{128}$"
_MAX_CANONICAL_JSON_BYTES = 1 << 20

OciDigest = Annotated[str, Field(pattern=_OCI_DIGEST_PATTERN)]
Ss58Hotkey = Annotated[str, Field(pattern=_SS58_PATTERN)]
Signature = Annotated[str, Field(pattern=_SIGNATURE_PATTERN)]


class CodingCatalogCommitment(CodingEvaluationModel):
    schema_name: Literal["dittobench-coding-catalog-commitment-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    corpus_release_id: OpaqueId
    catalog_merkle_root: Sha256
    selection_derivation_id: ShortName
    selection_chain_genesis_hash: BlockHash
    grader_contract_sha256: Sha256
    inference_grant_sha256: Sha256
    task_version_count: Annotated[int, Field(ge=1, le=1_000_000)]
    curator_hotkey: Ss58Hotkey
    committed_at_unix: Annotated[int, Field(ge=1, le=(1 << 63) - 1)]
    commitment_sha256: Sha256

    @field_validator("committed_at_unix")
    @classmethod
    def committed_at_is_representable(cls, value: int) -> int:
        try:
            datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError) as error:
            raise ValueError("committed_at_unix is outside supported bounds") from error
        return value

    @model_validator(mode="after")
    def digest_matches_known_fields(self) -> CodingCatalogCommitment:
        if coding_catalog_commitment_digest(self) != self.commitment_sha256:
            raise ValueError("commitment_sha256 does not match known fields")
        return self


class CodingCatalogTaskExposure(CodingEvaluationModel):
    """Private task-version projection consumed before any lease is issued."""

    manifest_index: Annotated[int, Field(ge=0, le=99)]
    task_version_id: OpaqueId
    task_commitment_sha256: Sha256
    selection_proof_sha256: Sha256
    catalog_membership_proof_sha256: Sha256
    visible_bundle_sha256: Sha256
    base_tree_sha256: Sha256
    memory_bundle_sha256: Sha256
    environment_image_digest: OciDigest
    resource_profile_sha256: Sha256
    grader_bundle_sha256: Sha256
    grader_image_digest: OciDigest
    test_manifest_sha256: Sha256
    grader_plan_sha256: Sha256


class AdminRegisterCodingCatalogRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    commitment: CodingCatalogCommitment
    signature: Signature
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @field_validator("reason")
    @classmethod
    def reason_is_substantive(cls, value: str) -> str:
        if len(value.strip()) < 8:
            raise ValueError("reason must contain at least 8 non-whitespace characters")
        return value.strip()

    @field_validator("actor")
    @classmethod
    def actor_is_substantive(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be blank")
        return value


class AdminRetireCodingCatalogRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    corpus_release_id: OpaqueId
    expected_commitment_sha256: Sha256
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @field_validator("reason")
    @classmethod
    def reason_is_substantive(cls, value: str) -> str:
        if len(value.strip()) < 8:
            raise ValueError("reason must contain at least 8 non-whitespace characters")
        return value.strip()

    @field_validator("actor")
    @classmethod
    def actor_is_substantive(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be blank")
        return value


class CodingCatalogReleaseRecord(CodingEvaluationModel):
    release_row_id: UUID
    commitment: CodingCatalogCommitment
    signature: str
    registered_reason: str
    registered_actor: str
    registered_at: datetime
    retired: bool
    retired_reason: str | None
    retired_actor: str | None
    retired_at: datetime | None
    exposure_count: Annotated[int, Field(ge=0)]
    exposed_run_count: Annotated[int, Field(ge=0)]
    shadow_only: Literal[True] = True


class AdminCodingCatalogResponse(CodingEvaluationModel):
    total: Annotated[int, Field(ge=0)]
    releases: list[CodingCatalogReleaseRecord]
    shadow_only: Literal[True] = True


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    body = (
        (
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        )
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
        .encode()
    )
    if len(body) > _MAX_CANONICAL_JSON_BYTES:
        raise ValueError("canonical catalog commitment exceeds 1 MiB")
    return body


def coding_catalog_commitment_digest(commitment: CodingCatalogCommitment) -> str:
    projection = commitment.model_dump(mode="json", by_alias=True)
    projection.pop("commitment_sha256")
    return hashlib.sha256(_canonical_json_bytes(projection)).hexdigest()


def coding_catalog_commitment_signing_message(
    commitment: CodingCatalogCommitment,
) -> bytes:
    committed_at = datetime.fromtimestamp(commitment.committed_at_unix, UTC).isoformat(
        timespec="microseconds"
    )
    return "\x00".join(
        (
            "dittobench-coding-catalog-commitment:v1",
            commitment.curator_hotkey,
            commitment.corpus_release_id,
            str(commitment.coding_contract_version),
            committed_at,
            commitment.commitment_sha256,
        )
    ).encode()
