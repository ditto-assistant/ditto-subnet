"""Admin wire contracts for the shadow-only private Coding v2 registry."""

from __future__ import annotations

import base64
import hashlib
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from ditto.api_models.coding_canonical import coding_canonical_sha256
from ditto.api_models.coding_evaluation import CodingEvaluationModel, OpaqueId, Sha256

_MAX_CANONICAL_BYTES = 16 << 20
_MAX_CIPHERTEXT_BYTES = (2 << 20) + 16
_MAX_PUBLICATION_OBJECTS = 10_000
_SOURCE_SHA_PATTERN = r"^[0-9a-f]{40}$"

SourceSha = Annotated[str, Field(pattern=_SOURCE_SHA_PATTERN)]


class CodingPrivateV2RegistrationAuthority(CodingEvaluationModel):
    schema_name: Literal["dittobench-coding-private-v2-registration-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[2]
    weight_eligible: Literal[False]
    shadow_only: Literal[True]
    corpus_release_id: OpaqueId
    private_release_sha256: Sha256
    catalog_sha256: Sha256
    catalog_merkle_root: Sha256
    payload_sha256: Sha256
    transport_sha256: Sha256
    wrapping_key_sha256: Sha256
    publication_receipt_sha256: Sha256
    previous_registration_sha256: Sha256 | None
    registration_sha256: Sha256

    @model_validator(mode="after")
    def digest_matches_known_fields(self) -> CodingPrivateV2RegistrationAuthority:
        if private_v2_registration_digest(self) != self.registration_sha256:
            raise ValueError("registration_sha256 does not match known fields")
        return self


class CodingPrivateV2PublicationObject(CodingEvaluationModel):
    object_index: Annotated[int, Field(ge=0, le=999_999)]
    remote_object_key_sha256: Sha256
    ciphertext_sha256: Sha256
    ciphertext_size_bytes: Annotated[int, Field(ge=17, le=_MAX_CIPHERTEXT_BYTES)]
    status: Literal["uploaded", "reused"]


class CodingPrivateV2PublicationReceipt(CodingEvaluationModel):
    schema_name: Literal["dittobench-coding-private-v2-publication-v1"] = Field(
        alias="schema"
    )
    source_sha: SourceSha
    checked_at: Annotated[str, Field(min_length=20, max_length=40)]
    provider: Literal["hippius"]
    probe_receipt_payload_sha256: Sha256
    private_input_authority_sha256: Sha256
    transport_sha256: Sha256
    payload_sha256: Sha256
    catalog_sha256: Sha256
    catalog_merkle_root: Sha256
    wrapping_key_sha256: Sha256
    curator_signing_key_sha256: Sha256
    curator_signature_b64: Annotated[str, Field(min_length=88, max_length=88)]
    object_count: Annotated[int, Field(ge=1, le=_MAX_PUBLICATION_OBJECTS)]
    objects: Annotated[
        list[CodingPrivateV2PublicationObject],
        Field(min_length=1, max_length=_MAX_PUBLICATION_OBJECTS),
    ]
    ready: Literal[True]
    shadow_only: Literal[True]
    weight_eligible: Literal[False]
    receipt_payload_sha256: Sha256

    @field_validator("checked_at")
    @classmethod
    def timestamp_is_canonical_utc(cls, value: str) -> str:
        if not value.endswith("Z"):
            raise ValueError("checked_at must use canonical UTC Z form")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("checked_at is invalid") from error
        offset = parsed.utcoffset()
        if offset is None or offset.total_seconds() != 0:
            raise ValueError("checked_at must be UTC")
        return value

    @field_validator("curator_signature_b64")
    @classmethod
    def signature_is_64_bytes(cls, value: str) -> str:
        try:
            signature = base64.b64decode(value, validate=True)
        except ValueError as error:
            raise ValueError("curator signature is invalid base64") from error
        if len(signature) != 64:
            raise ValueError("curator signature must be 64 bytes")
        return value

    @model_validator(mode="after")
    def receipt_is_complete_and_canonical(self) -> CodingPrivateV2PublicationReceipt:
        if self.object_count != len(self.objects):
            raise ValueError("object_count does not match objects")
        remote_keys: set[str] = set()
        ciphertexts: set[str] = set()
        for expected_index, item in enumerate(self.objects):
            if (
                item.object_index != expected_index
                or item.remote_object_key_sha256 in remote_keys
                or item.ciphertext_sha256 in ciphertexts
            ):
                raise ValueError("publication objects are not unique and ordered")
            remote_keys.add(item.remote_object_key_sha256)
            ciphertexts.add(item.ciphertext_sha256)
        if private_v2_publication_receipt_digest(self) != self.receipt_payload_sha256:
            raise ValueError("receipt_payload_sha256 does not match known fields")
        return self


class AdminRegisterCodingPrivateV2ReleaseRequest(CodingEvaluationModel):
    registration: CodingPrivateV2RegistrationAuthority
    publication_receipt: CodingPrivateV2PublicationReceipt
    curator_public_key_pem: Annotated[str, Field(min_length=1, max_length=65_536)]
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @field_validator("reason")
    @classmethod
    def reason_is_substantive(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 8:
            raise ValueError("reason must contain at least 8 characters")
        return value

    @field_validator("actor")
    @classmethod
    def actor_is_substantive(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be blank")
        return value


class AdminTransitionCodingPrivateV2ReleaseRequest(CodingEvaluationModel):
    corpus_release_id: OpaqueId
    expected_registration_sha256: Sha256
    reason: Annotated[str, Field(min_length=8)]
    actor: Annotated[str, Field(min_length=1, max_length=120)] = "admin_api"
    confirmation: str

    @field_validator("reason")
    @classmethod
    def reason_is_substantive(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 8:
            raise ValueError("reason must contain at least 8 characters")
        return value

    @field_validator("actor")
    @classmethod
    def actor_is_substantive(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("actor must not be blank")
        return value


class CodingPrivateV2ReleaseRecord(CodingEvaluationModel):
    release_row_id: UUID
    registration: CodingPrivateV2RegistrationAuthority
    publication_source_sha: SourceSha
    provider_probe_receipt_sha256: Sha256
    private_input_authority_sha256: Sha256
    curator_signing_key_sha256: Sha256
    publication_object_count: Annotated[int, Field(ge=1, le=_MAX_PUBLICATION_OBJECTS)]
    status: Literal["registered", "quarantined", "retired"]
    registered_reason: str
    registered_actor: str
    registered_at: datetime
    lifecycle_event_count: Annotated[int, Field(ge=0, le=2)]
    latest_event_reason: str | None
    latest_event_actor: str | None
    latest_event_at: datetime | None
    shadow_only: Literal[True]
    selectable: Literal[False]
    weight_eligible: Literal[False]


class AdminCodingPrivateV2ReleaseResponse(CodingEvaluationModel):
    total: Annotated[int, Field(ge=0)]
    releases: list[CodingPrivateV2ReleaseRecord]
    shadow_only: Literal[True]
    selectable: Literal[False]
    weight_eligible: Literal[False]


def private_v2_registration_digest(
    authority: CodingPrivateV2RegistrationAuthority,
) -> str:
    projection = authority.model_dump(mode="json", by_alias=True)
    projection.pop("registration_sha256")
    return coding_canonical_sha256(
        projection,
        maximum_bytes=64 << 10,
        label="private v2 registration authority",
    )


def private_v2_publication_receipt_digest(
    receipt: CodingPrivateV2PublicationReceipt,
) -> str:
    projection = receipt.model_dump(mode="json", by_alias=True)
    projection.pop("receipt_payload_sha256")
    return coding_canonical_sha256(
        projection,
        maximum_bytes=_MAX_CANONICAL_BYTES,
        label="private v2 publication receipt",
    )


def private_v2_release_event_digest(
    *,
    registration_sha256: str,
    action: Literal["quarantined", "retired"],
    reason: str,
    actor: str,
) -> str:
    return coding_canonical_sha256(
        {
            "action": action,
            "actor": actor.strip(),
            "reason_sha256": hashlib.sha256(reason.strip().encode("utf-8")).hexdigest(),
            "registration_sha256": registration_sha256,
            "schema": "dittobench-coding-private-v2-release-event-v1",
            "selectable": False,
            "shadow_only": True,
            "weight_eligible": False,
        },
        maximum_bytes=16 << 10,
        label="private v2 release event",
    )
