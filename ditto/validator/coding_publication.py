"""Private client for the durable Go coding-publication handoff."""

from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ditto.validator.errors import PlatformInfrastructureError

PublicationStage = Literal["authoring_freeze", "terminal_result"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RESPONSE_BYTES = 6 << 20


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class PublicationArtifact(_WireModel):
    object_key: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(strict=True, gt=0, le=4 << 20)

    @model_validator(mode="after")
    def object_key_matches_digest(self) -> PublicationArtifact:
        if self.object_key != f"sha256/{self.sha256}":
            raise ValueError("coding publication object key is invalid")
        return self


class PublicationAuthority(_WireModel):
    agent_id: UUID
    bench_version: int = Field(strict=True, ge=7, le=1_000_000)
    run_row_id: UUID
    coding_run_id: str = Field(min_length=1, max_length=256)
    screened_image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    task_set_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def identifiers_are_bounded(self) -> PublicationAuthority:
        if any(
            character.isspace() or ord(character) < 32
            for character in self.coding_run_id
        ):
            raise ValueError("coding publication run identity is invalid")
        return self


class PendingPublication(_WireModel):
    record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    ticket_id: UUID
    stage: PublicationStage
    authority: PublicationAuthority
    request: PublicationArtifact


class PublicationRecord(PendingPublication):
    acknowledgement: PublicationArtifact | None = None


@dataclass(frozen=True, repr=False)
class PreparedCodingPublication:
    stage: PublicationStage
    ticket_id: UUID
    agent_id: UUID
    authority: PublicationAuthority
    body: bytes

    def __post_init__(self) -> None:
        if (
            self.stage not in {"authoring_freeze", "terminal_result"}
            or self.ticket_id.int == 0
            or self.agent_id.int == 0
            or self.authority.agent_id != self.agent_id
            or not self.body
            or len(self.body) > 4 << 20
        ):
            raise ValueError("prepared coding publication body is outside bounds")


class _Result(_WireModel):
    schema_name: Literal["dittobench-coding-publication-result-v1"] = Field(
        alias="schema"
    )
    coding_contract_version: Literal[1]
    weight_eligible: Literal[False]
    operation: Literal["prepare", "acknowledge", "pending", "open", "lookup"]
    record_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    artifact: PublicationArtifact | None = None
    pending: list[PendingPublication] | None = None
    publication: PublicationRecord | None = None
    body_base64: str | None = None

    @model_validator(mode="after")
    def operation_shape_is_coherent(self) -> _Result:
        valid = {
            "prepare": self.record_id is not None and self.artifact is not None,
            "acknowledge": self.record_id is not None and self.artifact is not None,
            "pending": self.pending is not None,
            "open": self.record_id is not None and self.body_base64 is not None,
            "lookup": self.record_id is not None and self.publication is not None,
        }
        if not valid[self.operation]:
            raise ValueError("coding publication response shape is invalid")
        return self


@dataclass(frozen=True, repr=False)
class CodingPublicationClient:
    base_url: str
    control_token: str
    client: httpx.AsyncClient

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1", "sandbox-docker"}
            or not parsed.port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not _valid_control_token(self.control_token)
            or getattr(self.client, "trust_env", True)
        ):
            raise ValueError("coding publication client configuration is invalid")

    async def prepare(
        self,
        *,
        ticket_id: str,
        stage: PublicationStage,
        authority: PublicationAuthority,
        body: bytes,
    ) -> tuple[str, PublicationArtifact]:
        if not _canonical_uuid(ticket_id) or stage not in {
            "authoring_freeze",
            "terminal_result",
        }:
            raise ValueError("coding publication prepare authority is invalid")
        result = await self._call(
            "prepare",
            {
                "schema": "dittobench-coding-publication-command-v1",
                "ticket_id": ticket_id,
                "stage": stage,
                "authority": authority.model_dump(mode="json"),
                "body_base64": _encode_body(body, 4 << 20),
            },
        )
        if result.record_id is None or result.artifact is None:
            raise PlatformInfrastructureError(
                "coding publication prepare result is invalid"
            )
        return result.record_id, result.artifact

    async def acknowledge(
        self,
        *,
        ticket_id: str,
        stage: PublicationStage,
        request_sha256: str,
        body: bytes,
    ) -> PublicationArtifact:
        if (
            not _canonical_uuid(ticket_id)
            or stage not in {"authoring_freeze", "terminal_result"}
            or _SHA256.fullmatch(request_sha256) is None
        ):
            raise ValueError("coding publication request digest is invalid")
        result = await self._call(
            "acknowledge",
            {
                "schema": "dittobench-coding-publication-command-v1",
                "ticket_id": ticket_id,
                "stage": stage,
                "request_sha256": request_sha256,
                "body_base64": _encode_body(body, 1 << 20),
            },
        )
        if result.artifact is None:
            raise PlatformInfrastructureError(
                "coding publication acknowledgement result is invalid"
            )
        return result.artifact

    async def pending(self, *, limit: int = 100) -> list[PendingPublication]:
        if not 1 <= limit <= 10_000:
            raise ValueError("coding publication pending limit is invalid")
        result = await self._call(
            "pending",
            {"schema": "dittobench-coding-publication-command-v1", "limit": limit},
        )
        if result.pending is None:
            return []
        return result.pending

    async def open(
        self,
        *,
        record_id: str,
        stage: PublicationStage,
        expected: PublicationArtifact,
        acknowledgement: bool = False,
    ) -> bytes:
        if _SHA256.fullmatch(record_id) is None or stage not in {
            "authoring_freeze",
            "terminal_result",
        }:
            raise ValueError("coding publication replay authority is invalid")
        result = await self._call(
            "open",
            {
                "schema": "dittobench-coding-publication-command-v1",
                "record_id": record_id,
                "stage": stage,
                "acknowledgement": acknowledgement,
            },
        )
        if not result.body_base64:
            raise PlatformInfrastructureError(
                "coding publication replay body is invalid"
            )
        try:
            body = base64.b64decode(result.body_base64, validate=True)
        except ValueError:
            raise PlatformInfrastructureError(
                "coding publication replay body is invalid"
            ) from None
        if not body or len(body) > 4 << 20:
            raise PlatformInfrastructureError(
                "coding publication replay body is invalid"
            )
        if (
            result.record_id != record_id
            or len(body) != expected.size_bytes
            or hashlib.sha256(body).hexdigest() != expected.sha256
        ):
            raise PlatformInfrastructureError(
                "coding publication replay body identity is invalid"
            )
        return body

    async def lookup(
        self,
        *,
        ticket_id: str,
        stage: PublicationStage,
    ) -> PublicationRecord:
        if not _canonical_uuid(ticket_id) or stage not in {
            "authoring_freeze",
            "terminal_result",
        }:
            raise ValueError("coding publication lookup authority is invalid")
        result = await self._call(
            "lookup",
            {
                "schema": "dittobench-coding-publication-command-v1",
                "ticket_id": ticket_id,
                "stage": stage,
            },
        )
        if (
            result.publication is None
            or result.record_id != result.publication.record_id
            or result.publication.ticket_id != UUID(ticket_id)
            or result.publication.stage != stage
        ):
            raise PlatformInfrastructureError(
                "coding publication lookup result is invalid"
            )
        return result.publication

    async def _call(self, operation: str, payload: dict[str, object]) -> _Result:
        body = bytearray()
        try:
            async with self.client.stream(
                "POST",
                f"{self.base_url.rstrip('/')}/v1/coding/publications/{operation}",
                headers={
                    "Authorization": f"Bearer {self.control_token}",
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                },
                json=payload,
                follow_redirects=False,
            ) as response:
                if (
                    response.status_code != 200
                    or "no-store"
                    not in {
                        directive.strip().lower()
                        for directive in response.headers.get(
                            "Cache-Control", ""
                        ).split(",")
                    }
                    or not response.headers.get("Content-Type", "")
                    .lower()
                    .startswith("application/json")
                ):
                    raise PlatformInfrastructureError(
                        "coding publication handoff rejected"
                    )
                async for chunk in response.aiter_bytes(chunk_size=16 << 10):
                    if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                        raise PlatformInfrastructureError(
                            "coding publication handoff response is invalid"
                        )
                    body.extend(chunk)
        except httpx.HTTPError as error:
            raise PlatformInfrastructureError(
                "coding publication handoff request failed"
            ) from error
        if not body:
            raise PlatformInfrastructureError(
                "coding publication handoff response is invalid"
            )
        try:
            result = _Result.model_validate_json(body)
        except ValidationError:
            raise PlatformInfrastructureError(
                "coding publication handoff response is invalid"
            ) from None
        if result.operation != operation:
            raise PlatformInfrastructureError(
                "coding publication handoff response identity is invalid"
            )
        return result


def _encode_body(body: bytes, maximum: int) -> str:
    if not body or len(body) > maximum:
        raise ValueError("coding publication body is outside bounds")
    return base64.b64encode(body).decode("ascii")


def _canonical_uuid(value: str) -> bool:
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.int != 0 and str(parsed) == value


def _valid_control_token(value: str) -> bool:
    return 32 <= len(value) <= 256 and all(
        character.isascii() and (character.isalnum() or character in "_-")
        for character in value
    )


__all__ = [
    "CodingPublicationClient",
    "PendingPublication",
    "PreparedCodingPublication",
    "PublicationRecord",
    "PublicationArtifact",
    "PublicationAuthority",
    "PublicationStage",
]
