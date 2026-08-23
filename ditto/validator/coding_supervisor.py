"""Private, currently unwired client for the shadow coding attempt supervisor."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ditto.api_models.coding import (
    CodingAuthoringEvidence,
    CodingAuthoringLeaseResponse,
    CodingGradingLeaseResponse,
    CodingTaskEvidence,
)
from ditto.validator.coding_attempt import (
    CodingAttemptIntegrityError,
    CodingAuthoringOutcome,
    CodingGradingOutcome,
)
from ditto.validator.config import ValidatorConfig
from ditto.validator.errors import ValidatorInfrastructureError

_REQUEST_SCHEMA = "dittobench-coding-attempt-supervisor-request-v1"
_RESPONSE_SCHEMA = "dittobench-coding-attempt-supervisor-response-v1"
_MAX_BODY_BYTES = 8 << 20

type SupervisorOperation = Literal[
    "author", "grade", "abort_authoring", "abort_grading", "recover"
]


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _AuthoringWire(_WireModel):
    evidence: dict[str, Any]
    authoring_transcript_object_key: str
    authoring_transcript_bytes: int = Field(gt=0, le=512 << 20)
    authoring_event_count: int = Field(gt=0, le=1_000)
    frozen_submission_object_key: str
    capabilities_revoked: Literal[True]
    authoring_environment_destroyed: Literal[True]


class _GradingWire(_WireModel):
    task_evidence: list[dict[str, Any]] = Field(min_length=1, max_length=100)
    grading_environment_destroyed: Literal[True]


class CodingSupervisorRecovery(_WireModel):
    state: Literal[
        "none",
        "authoring_pending",
        "terminal_pending",
        "released",
        "ambiguous",
        "expired",
    ]
    publication_stage: Literal["authoring_freeze", "terminal_result"] | None
    request_sha256: str | None

    @model_validator(mode="after")
    def pending_shape_is_coherent(self) -> CodingSupervisorRecovery:
        pending = self.state in {"authoring_pending", "terminal_pending"}
        if pending != (
            self.publication_stage is not None and self.request_sha256 is not None
        ):
            raise ValueError("coding supervisor recovery shape is invalid")
        if (
            self.state == "authoring_pending"
            and self.publication_stage != "authoring_freeze"
        ) or (
            self.state == "terminal_pending"
            and self.publication_stage != "terminal_result"
        ):
            raise ValueError("coding supervisor recovery stage is invalid")
        if self.request_sha256 is not None and (
            len(self.request_sha256) != 64
            or self.request_sha256 != self.request_sha256.lower()
            or any(
                character not in "0123456789abcdef" for character in self.request_sha256
            )
        ):
            raise ValueError("coding supervisor recovery digest is invalid")
        return self


class _SupervisorResponse(_WireModel):
    schema_name: Literal["dittobench-coding-attempt-supervisor-response-v1"] = Field(
        alias="schema"
    )
    operation: SupervisorOperation
    operation_id: UUID
    ticket_id: UUID
    coding_run_id: str = Field(min_length=1, max_length=256)
    authoring: _AuthoringWire | None
    grading: _GradingWire | None
    recovery: CodingSupervisorRecovery | None
    aborted: bool

    @model_validator(mode="after")
    def operation_shape_is_coherent(self) -> _SupervisorResponse:
        valid = False
        if self.operation == "author":
            valid = (
                self.authoring is not None
                and self.grading is None
                and self.recovery is None
                and not self.aborted
            )
        elif self.operation == "grade":
            valid = (
                self.authoring is None
                and self.grading is not None
                and self.recovery is None
                and not self.aborted
            )
        elif self.operation in {"abort_authoring", "abort_grading"}:
            valid = (
                self.authoring is None
                and self.grading is None
                and self.recovery is None
                and self.aborted
            )
        elif self.operation == "recover":
            valid = (
                self.authoring is None
                and self.grading is None
                and self.recovery is not None
                and not self.aborted
            )
        if not valid:
            raise ValueError("coding supervisor response operation is invalid")
        return self


class CodingSupervisorRuntime:
    """Implement ``CodingAttemptRuntime`` through the protected scorer control plane."""

    def __init__(self, config: ValidatorConfig, client: httpx.AsyncClient) -> None:
        parsed = urlsplit(config.dittobench_api_url)
        token = config.dittobench_control_token
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not 32 <= len(token) <= 256
            or any(character.isspace() or ord(character) < 32 for character in token)
        ):
            raise ValueError("coding supervisor configuration is invalid")
        self._base = config.dittobench_api_url.rstrip("/")
        self._token = token
        self._client = client

    async def author(
        self,
        lease: CodingAuthoringLeaseResponse,
    ) -> CodingAuthoringOutcome:
        response = await self._call(
            operation="author",
            ticket_id=lease.ticket_id,
            coding_run_id=lease.coding_run_id,
            deadline=lease.ticket_deadline,
            lease=lease.model_dump(mode="json", by_alias=True),
            authoring=None,
        )
        if response.authoring is None:
            raise CodingAttemptIntegrityError("coding supervisor omitted authoring")
        try:
            evidence = CodingAuthoringEvidence.model_validate_json(
                json.dumps(response.authoring.evidence, separators=(",", ":"))
            )
            return CodingAuthoringOutcome(
                evidence=evidence,
                authoring_transcript_object_key=(
                    response.authoring.authoring_transcript_object_key
                ),
                authoring_transcript_bytes=(
                    response.authoring.authoring_transcript_bytes
                ),
                authoring_event_count=response.authoring.authoring_event_count,
                frozen_submission_object_key=(
                    response.authoring.frozen_submission_object_key
                ),
                capabilities_revoked=True,
                authoring_environment_destroyed=True,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise CodingAttemptIntegrityError(
                "coding supervisor authoring result is invalid"
            ) from error

    async def grade(
        self,
        lease: CodingGradingLeaseResponse,
        authoring: CodingAuthoringOutcome,
    ) -> CodingGradingOutcome:
        response = await self._call(
            operation="grade",
            ticket_id=lease.ticket_id,
            coding_run_id=lease.coding_run_id,
            deadline=lease.ticket_deadline,
            lease=lease.model_dump(mode="json", by_alias=True),
            authoring=_authoring_payload(authoring),
        )
        if response.grading is None:
            raise CodingAttemptIntegrityError("coding supervisor omitted grading")
        try:
            evidence = tuple(
                CodingTaskEvidence.model_validate_json(
                    json.dumps(value, separators=(",", ":"))
                )
                for value in response.grading.task_evidence
            )
            identities = [
                (item.task.case_id, item.task.variant_id) for item in evidence
            ]
            if (
                identities != sorted(identities)
                or len(set(identities)) != len(identities)
                or any(
                    item.coding_run_id != lease.coding_run_id
                    or item.validator_ticket_id != str(lease.ticket_id)
                    for item in evidence
                )
            ):
                raise ValueError("coding supervisor task evidence authority drifted")
            return CodingGradingOutcome(
                task_evidence=evidence,
                grading_environment_destroyed=True,
            )
        except (TypeError, ValueError, ValidationError) as error:
            raise CodingAttemptIntegrityError(
                "coding supervisor grading result is invalid"
            ) from error

    async def abort_authoring(self, lease: CodingAuthoringLeaseResponse) -> None:
        response = await self._call(
            operation="abort_authoring",
            ticket_id=lease.ticket_id,
            coding_run_id=lease.coding_run_id,
            deadline=lease.ticket_deadline,
            lease=lease.model_dump(mode="json", by_alias=True),
            authoring=None,
        )
        if not response.aborted:
            raise CodingAttemptIntegrityError(
                "coding supervisor did not abort authoring"
            )

    async def abort_grading(self, lease: CodingGradingLeaseResponse) -> None:
        response = await self._call(
            operation="abort_grading",
            ticket_id=lease.ticket_id,
            coding_run_id=lease.coding_run_id,
            deadline=lease.ticket_deadline,
            lease=lease.model_dump(mode="json", by_alias=True),
            authoring=None,
        )
        if not response.aborted:
            raise CodingAttemptIntegrityError("coding supervisor did not abort grading")

    async def recover(
        self,
        *,
        ticket_id: UUID,
        coding_run_id: str,
        deadline: datetime,
    ) -> CodingSupervisorRecovery:
        response = await self._call(
            operation="recover",
            ticket_id=ticket_id,
            coding_run_id=coding_run_id,
            deadline=deadline,
            lease=None,
            authoring=None,
        )
        if response.recovery is None:
            raise CodingAttemptIntegrityError("coding supervisor omitted recovery")
        return response.recovery

    async def _call(
        self,
        *,
        operation: SupervisorOperation,
        ticket_id: UUID,
        coding_run_id: str,
        deadline: datetime,
        lease: dict[str, Any] | None,
        authoring: dict[str, Any] | None,
    ) -> _SupervisorResponse:
        if (
            ticket_id.int == 0
            or not coding_run_id
            or len(coding_run_id) > 256
            or any(
                character.isspace() or ord(character) < 32
                for character in coding_run_id
            )
            or deadline.tzinfo is None
            or deadline.utcoffset() is None
        ):
            raise CodingAttemptIntegrityError(
                "coding supervisor request authority is invalid"
            )
        operation_id = uuid4()
        payload = {
            "schema": _REQUEST_SCHEMA,
            "operation": operation,
            "operation_id": str(operation_id),
            "ticket_id": str(ticket_id),
            "coding_run_id": coding_run_id,
            "deadline": deadline.isoformat().replace("+00:00", "Z"),
            "lease": lease,
            "authoring": authoring,
        }
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError, UnicodeEncodeError) as error:
            raise CodingAttemptIntegrityError(
                "coding supervisor request JSON is invalid"
            ) from error
        if len(body) > _MAX_BODY_BYTES:
            raise CodingAttemptIntegrityError("coding supervisor request is too large")
        endpoint = operation.replace("_", "-")
        received = bytearray()
        try:
            async with self._client.stream(
                "POST",
                f"{self._base}/v1/coding/supervisor/{endpoint}",
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "Cache-Control": "no-store",
                },
                content=body,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    if response.status_code in {400, 409}:
                        raise CodingAttemptIntegrityError(
                            f"coding supervisor rejected {operation}"
                        )
                    raise ValidatorInfrastructureError(
                        f"coding supervisor unavailable for {operation}"
                    )
                if response.headers.get("cache-control") != "no-store":
                    raise ValidatorInfrastructureError(
                        "coding supervisor response cache policy is invalid"
                    )
                media_type = response.headers.get("content-type", "").split(";", 1)[0]
                if media_type != "application/json" or response.headers.get(
                    "content-encoding"
                ):
                    raise ValidatorInfrastructureError(
                        "coding supervisor response encoding is invalid"
                    )
                async for chunk in response.aiter_bytes(chunk_size=64 << 10):
                    if len(received) + len(chunk) > _MAX_BODY_BYTES:
                        raise ValidatorInfrastructureError(
                            "coding supervisor response is too large"
                        )
                    received.extend(chunk)
        except httpx.HTTPError as error:
            raise ValidatorInfrastructureError(
                f"coding supervisor transport failed for {operation}"
            ) from error
        try:
            result = _SupervisorResponse.model_validate_json(received)
        except ValidationError as error:
            raise CodingAttemptIntegrityError(
                "coding supervisor response is invalid"
            ) from error
        if (
            result.operation != operation
            or result.operation_id != operation_id
            or result.ticket_id != ticket_id
            or result.coding_run_id != coding_run_id
        ):
            raise CodingAttemptIntegrityError(
                "coding supervisor response authority is invalid"
            )
        return result

    def __repr__(self) -> str:
        return "CodingSupervisorRuntime(private=True)"


def _authoring_payload(authoring: CodingAuthoringOutcome) -> dict[str, Any]:
    return {
        "evidence": authoring.evidence.model_dump(mode="json", by_alias=True),
        "authoring_transcript_object_key": (authoring.authoring_transcript_object_key),
        "authoring_transcript_bytes": authoring.authoring_transcript_bytes,
        "authoring_event_count": authoring.authoring_event_count,
        "frozen_submission_object_key": authoring.frozen_submission_object_key,
        "capabilities_revoked": authoring.capabilities_revoked,
        "authoring_environment_destroyed": (authoring.authoring_environment_destroyed),
    }


__all__ = ["CodingSupervisorRecovery", "CodingSupervisorRuntime"]
