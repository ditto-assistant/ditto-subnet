"""Signed, restricted native results after sealed evidence finalization."""

import bittensor
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_models.coding_hosted import (
    HostedCodingRequest,
    HostedCodingResult,
    hosted_message_digest,
    hosted_signing_bytes,
)
from ditto.api_models.coding_hosted_grading import HostedTerminalIdentity
from ditto.api_server.coding_hosted_authoring_evidence import canonical
from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedResultDelivery,
    CodingHostedTerminalFinalization,
    CodingHostedTerminalReservation,
)


async def signed_terminal_result(
    session: AsyncSession,
    row: CodingHostedAssignment,
    request: HostedCodingRequest,
    signer,
    issued: int,
) -> bytes | None:
    if await session.get(CodingHostedTerminalFinalization, row.evaluation_id) is None:
        return None
    reserved = await session.get(CodingHostedTerminalReservation, row.evaluation_id)
    if reserved is None:
        raise ValueError("hosted terminal unavailable")
    identity = HostedTerminalIdentity.model_validate_json(canonical(reserved.identity))
    if (
        identity.digest() != reserved.identity_sha256
        or identity.source.evaluation_id != row.evaluation_id
        or identity.source.worker_id != row.worker_id
        or identity.source.artifact_sha256 != row.artifact_sha256
        or identity.grading_profile_sha256 != row.authority["grading_profile_sha256"]
        or identity.source.attempt_id != row.attempt_id
        or identity.source.assignment_sha256 != row.assignment_sha256
    ):
        raise ValueError("hosted terminal differs")
    result = HostedCodingResult.model_validate(
        {
            "schema": "dittobench-coding-hosted-result-v2",
            "coding_contract_version": 2,
            "shadow_only": True,
            "weight_eligible": False,
            "evaluation_id": row.evaluation_id,
            "attempt_id": row.attempt_id,
            "validator_hotkey": row.validator_hotkey,
            "platform_hotkey": signer.ss58_address,
            "request_sha256": hosted_message_digest(request),
            "artifact_sha256": row.artifact_sha256,
            "assignment_sha256": row.assignment_sha256,
            "policy_sha256": row.authority["policy_sha256"],
            "execution_profile_sha256": row.authority["execution_profile_sha256"],
            "grading_profile_sha256": row.authority["grading_profile_sha256"],
            "evidence_sha256": identity.digest(),
            "outcome": identity.outcome,
            "issued_at_unix": issued,
            "expires_at_unix": issued + 3600,
            "signature": "0" * 128,
        }
    )
    message = hosted_signing_bytes(result)
    signature = signer.sign(message)
    if not bittensor.Keypair(ss58_address=signer.ss58_address).verify(
        message, signature
    ):
        raise ValueError("hosted result signer unavailable")
    result = HostedCodingResult.model_validate(
        {**result.model_dump(mode="json", by_alias=True), "signature": signature.hex()}
    )
    digest = hosted_message_digest(result)
    body = result.model_dump(mode="json", by_alias=True)
    if await session.get(CodingHostedResultDelivery, digest) is None:
        session.add(
            CodingHostedResultDelivery(
                result_sha256=digest,
                evaluation_id=row.evaluation_id,
                validator_hotkey=row.validator_hotkey,
                body=body,
            )
        )
    return canonical(body, 8192)
