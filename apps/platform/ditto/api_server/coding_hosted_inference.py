"""Private worker inference ledger; no public route or provider key handling."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_hosted_inference import (
    HostedInferencePolicy,
    HostedInferenceSettlement,
)
from ditto.api_models.coding_inference import (
    _decode_json_document,
    effective_inference_request_budget,
)
from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedInferenceGrant,
    CodingHostedInferenceRequest,
)
from ditto.db.queries.coding_hosted_admission import _locked_assignment, _now
from ditto.db.queries.coding_hosted_private import (
    _require_worker,
    _selection_matches,
    _task,
)


class HostedInferenceError(ValueError):
    """Safe refusal without model text, provider keys or database details."""


@dataclass(frozen=True, repr=False)
class DispatchReservation:
    grant_id: UUID
    request_id: UUID
    sequence: int
    locked_request_sha256: str
    newly_reserved: bool
    expires_at_unix: int
    evaluation_id: UUID
    attempt_id: UUID
    policy_sha256: str


@dataclass(frozen=True, repr=False)
class ReservationCeilings:
    """Required trusted adapter estimates, NEVER miner-reported token counts."""

    prompt_tokens: int
    completion_tokens: int
    cost_usd_micros: int

    def validate(self) -> None:
        for value, maximum in (
            (self.prompt_tokens, 2_250_000),
            (self.completion_tokens, 250_000),
            (self.cost_usd_micros, 100_000_000),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise HostedInferenceError("inference reservation bounds are invalid")


@dataclass(frozen=True, repr=False)
class InferenceAccounting:
    """Private accounting status, not a signed task result or reward authority."""

    verified: bool
    request_count: int
    pending_count: int
    uncertain_count: int
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd_micros: int | None


class HostedInferenceLedger:
    """A trusted Platform worker principal; grant UUIDs alone are not credentials.

    Each mutating method owns and commits its transaction before returning.
    Provider transport, source authentication and verified usage ingestion must
    be supplied by a reviewed native adapter; none is enabled by this class.
    """

    def __init__(self, *, sessions: async_sessionmaker[AsyncSession], worker_id: UUID):
        if not isinstance(worker_id, UUID) or not worker_id.int:
            raise HostedInferenceError("hosted inference worker is invalid")
        self._sessions, self._worker = sessions, worker_id

    async def issue(
        self,
        *,
        evaluation_id: UUID,
        attempt_id: UUID,
        assignment_sha256: str,
        policy: HostedInferencePolicy,
        execution_profile: bytes,
    ) -> UUID:
        policy = HostedInferencePolicy.model_validate(
            policy.model_dump(mode="json", by_alias=True)
        )
        profile = _decode_json_document(execution_profile, maximum_bytes=16384)
        if (
            not isinstance(profile, dict)
            or profile.get("schema") != "dittobench-coding-hosted-authoring-profile-v2"
            or coding_canonical_json_bytes(
                profile, maximum_bytes=16384, label="execution profile"
            )
            != execution_profile
        ):
            raise HostedInferenceError("approved execution profile is invalid")
        budgets = profile.get("budgets", {})
        if (
            not isinstance(budgets, dict)
            or any(
                type(budgets.get(name)) is not int or budgets[name] <= 0
                for name in (
                    "model_input_tokens",
                    "model_output_tokens",
                    "workspace_tool_calls",
                    "wall_time_seconds",
                )
            )
            or budgets["wall_time_seconds"] > 3600
        ):
            raise HostedInferenceError("approved inference budgets are invalid")
        profile_sha = hashlib.sha256(execution_profile).hexdigest()
        limits = (
            min(
                policy.max_requests,
                effective_inference_request_budget(budgets["workspace_tool_calls"]),
            ),
            min(policy.max_prompt_tokens, budgets["model_input_tokens"]),
            min(policy.max_completion_tokens, budgets["model_output_tokens"]),
            policy.max_cost_usd_micros,
        )
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            assignment = await _locked_assignment(session, evaluation_id)
            _require_worker(assignment, attempt_id, self._worker)
            task = await _task(session, evaluation_id)
            if (
                assignment.assignment_sha256 != assignment_sha256
                or assignment.authority["policy_sha256"] != policy.digest()
                or assignment.authority["execution_profile_sha256"] != profile_sha
                or task is None
                or not _selection_matches(task, assignment)
                or task.closed_at is not None
                or task.frozen_at is not None
                or assignment.expires_at <= await _now(session)
            ):
                raise HostedInferenceError("hosted inference assignment is unavailable")
            grant = await session.scalar(
                select(CodingHostedInferenceGrant)
                .where(CodingHostedInferenceGrant.evaluation_id == evaluation_id)
                .with_for_update()
            )
            if grant is not None:
                if (
                    grant.worker_id != self._worker
                    or grant.policy_sha256 != policy.digest()
                    or grant.revoked_at is not None
                    or grant.expires_at <= await _now(session)
                ):
                    raise HostedInferenceError("hosted inference grant conflicts")
                result = grant.grant_id
            else:
                if assignment.started_at is None:
                    raise HostedInferenceError("hosted attempt has not started")
                expiry = min(
                    assignment.expires_at,
                    assignment.started_at
                    + timedelta(seconds=budgets["wall_time_seconds"]),
                )
                if expiry <= await _now(session):
                    raise HostedInferenceError("hosted inference lifetime expired")
                result = uuid4()
                session.add(
                    CodingHostedInferenceGrant(
                        grant_id=result,
                        evaluation_id=evaluation_id,
                        attempt_id=attempt_id,
                        worker_id=self._worker,
                        assignment_sha256=assignment_sha256,
                        policy_sha256=policy.digest(),
                        execution_profile_sha256=profile_sha,
                        policy=policy.model_dump(mode="json", by_alias=True),
                        request_limit=limits[0],
                        prompt_limit=limits[1],
                        completion_limit=limits[2],
                        cost_limit=limits[3],
                        expires_at=expiry,
                        shadow_only=True,
                        weight_eligible=False,
                    )
                )
                await session.flush()
        return result

    async def _grant(
        self, session: AsyncSession, grant_id: UUID, *, active: bool
    ) -> CodingHostedInferenceGrant:
        snapshot = await session.get(CodingHostedInferenceGrant, grant_id)
        if snapshot is None:
            raise HostedInferenceError("hosted inference grant is unavailable")
        assignment = (
            (await _locked_assignment(session, snapshot.evaluation_id))
            if active
            else await session.get(
                CodingHostedAssignment,
                snapshot.evaluation_id,
                with_for_update=True,
                populate_existing=True,
            )
        )
        if assignment is None:
            raise HostedInferenceError("hosted inference assignment is unavailable")
        _require_worker(assignment, snapshot.attempt_id, self._worker)
        task = await _task(session, snapshot.evaluation_id)
        grant = await session.get(
            CodingHostedInferenceGrant,
            grant_id,
            with_for_update=True,
            populate_existing=True,
        )
        if (
            grant is None
            or grant.worker_id != self._worker
            or grant.assignment_sha256 != assignment.assignment_sha256
            or grant.policy_sha256 != assignment.authority["policy_sha256"]
        ):
            raise HostedInferenceError("hosted inference identity does not match")
        if active and (
            grant.revoked_at is not None
            or task is None
            or not _selection_matches(task, assignment)
            or task.frozen_at is not None
            or task.closed_at is not None
            or grant.expires_at <= await _now(session)
        ):
            raise HostedInferenceError("hosted inference authoring phase is closed")
        return grant

    async def reserve(
        self,
        *,
        grant_id: UUID,
        request_id: UUID,
        locked_request: bytes,
        ceilings: ReservationCeilings,
    ) -> DispatchReservation:
        ceilings.validate()
        if not isinstance(request_id, UUID) or not request_id.int:
            raise HostedInferenceError("hosted request identity is invalid")
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            grant = await self._grant(session, grant_id, active=True)
            policy = HostedInferencePolicy.model_validate(grant.policy)
            if policy.digest() != grant.policy_sha256:
                raise HostedInferenceError("hosted inference policy drifted")
            try:
                request, digest = policy.locked_request(locked_request)
            except ValueError:
                raise HostedInferenceError(
                    "hosted model request violates policy"
                ) from None
            if ceilings.completion_tokens != request.max_completion_tokens:
                raise HostedInferenceError("output reservation does not match request")
            existing = await session.get(CodingHostedInferenceRequest, request_id)
            if existing is not None:
                if (
                    existing.grant_id != grant_id
                    or existing.locked_request_sha256 != digest
                    or (
                        existing.prompt_ceiling,
                        existing.completion_ceiling,
                        existing.cost_ceiling,
                    )
                    != (
                        ceilings.prompt_tokens,
                        ceilings.completion_tokens,
                        ceilings.cost_usd_micros,
                    )
                ):
                    raise HostedInferenceError(
                        "request identity was reused with different authority"
                    )
                result = DispatchReservation(
                    grant_id,
                    request_id,
                    existing.sequence,
                    digest,
                    False,
                    int(grant.expires_at.timestamp()),
                    grant.evaluation_id,
                    grant.attempt_id,
                    grant.policy_sha256,
                )
            else:
                rows = list(
                    (
                        await session.scalars(
                            select(CodingHostedInferenceRequest).where(
                                CodingHostedInferenceRequest.grant_id == grant_id
                            )
                        )
                    ).all()
                )
                used_prompt = _charged(rows, "prompt_tokens", "prompt_ceiling")
                used_completion = _charged(
                    rows, "completion_tokens", "completion_ceiling"
                )
                used_cost = _charged(rows, "cost_usd_micros", "cost_ceiling")
                if (
                    any(row.state == "reserved" for row in rows)
                    or len(rows) >= grant.request_limit
                    or used_prompt + ceilings.prompt_tokens > grant.prompt_limit
                    or used_completion + ceilings.completion_tokens
                    > grant.completion_limit
                    or used_cost + ceilings.cost_usd_micros > grant.cost_limit
                ):
                    raise HostedInferenceError(
                        "hosted inference budget or serial dispatch limit reached"
                    )
                sequence = len(rows) + 1
                session.add(
                    CodingHostedInferenceRequest(
                        request_id=request_id,
                        grant_id=grant_id,
                        sequence=sequence,
                        locked_request_sha256=digest,
                        prompt_ceiling=ceilings.prompt_tokens,
                        completion_ceiling=ceilings.completion_tokens,
                        cost_ceiling=ceilings.cost_usd_micros,
                        state="reserved",
                    )
                )
                await session.flush()
                result = DispatchReservation(
                    grant_id,
                    request_id,
                    sequence,
                    digest,
                    True,
                    int(grant.expires_at.timestamp()),
                    grant.evaluation_id,
                    grant.attempt_id,
                    grant.policy_sha256,
                )
        return result

    async def settle(self, settlement: HostedInferenceSettlement) -> bool:
        settlement = HostedInferenceSettlement.model_validate(
            settlement.model_dump(mode="json", by_alias=True)
        )
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            grant = await self._grant(session, settlement.grant_id, active=False)
            request = await session.get(
                CodingHostedInferenceRequest,
                settlement.request_id,
                with_for_update=True,
            )
            if (
                request is None
                or request.grant_id != grant.grant_id
                or settlement.evaluation_id != grant.evaluation_id
                or settlement.attempt_id != grant.attempt_id
                or settlement.policy_sha256 != grant.policy_sha256
                or settlement.sequence != request.sequence
                or settlement.locked_request_sha256 != request.locked_request_sha256
                or settlement.prompt_tokens > request.prompt_ceiling
                or settlement.completion_tokens > request.completion_ceiling
                or settlement.cost_usd_micros > request.cost_ceiling
            ):
                raise HostedInferenceError("hosted provider settlement does not match")
            digest = settlement.digest()
            if request.state != "reserved":
                if request.state != "settled" or request.settlement_sha256 != digest:
                    raise HostedInferenceError("hosted provider settlement conflicts")
                fresh = False
            else:
                request.state = "settled"
                request.finalized_at = await _now(session)
                request.prompt_tokens = settlement.prompt_tokens
                request.completion_tokens = settlement.completion_tokens
                request.cost_usd_micros = settlement.cost_usd_micros
                request.settlement_sha256 = digest
                request.provider_receipt_sha256 = settlement.provider_receipt_sha256
                request.settlement = settlement.model_dump(mode="json", by_alias=True)
                await session.flush()
                fresh = True
        return fresh

    async def require_active(self, grant_id: UUID) -> None:
        """Recheck authority before releasing a settled response to the worker."""
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            await self._grant(session, grant_id, active=True)

    async def revoke(self, grant_id: UUID) -> bool:
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            grant = await self._grant(session, grant_id, active=False)
            if grant.revoked_at is None:
                grant.revoked_at = await _now(session)
                await session.flush()
            pending = await session.scalar(
                select(CodingHostedInferenceRequest.request_id)
                .where(
                    CodingHostedInferenceRequest.grant_id == grant_id,
                    CodingHostedInferenceRequest.state == "reserved",
                )
                .limit(1)
            )
        return pending is None

    async def accounting(self, grant_id: UUID) -> InferenceAccounting:
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            grant = await self._grant(session, grant_id, active=False)
            rows = list(
                (
                    await session.scalars(
                        select(CodingHostedInferenceRequest).where(
                            CodingHostedInferenceRequest.grant_id == grant_id
                        )
                    )
                ).all()
            )
            pending = sum(row.state == "reserved" for row in rows)
            uncertain = sum(row.state == "uncertain" for row in rows)
            verified = grant.revoked_at is not None and pending == 0 and uncertain == 0
            result = InferenceAccounting(
                verified,
                len(rows),
                pending,
                uncertain,
                _charged(rows, "prompt_tokens", "prompt_ceiling") if verified else None,
                _charged(rows, "completion_tokens", "completion_ceiling")
                if verified
                else None,
                _charged(rows, "cost_usd_micros", "cost_ceiling") if verified else None,
            )
        return result

    async def mark_uncertain(self, *, grant_id: UUID, request_id: UUID) -> None:
        """After transport quiescence, retain full ceilings and disable the grant."""
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            await self._grant(session, grant_id, active=False)
            request = await session.get(
                CodingHostedInferenceRequest, request_id, with_for_update=True
            )
            if (
                request is None
                or request.grant_id != grant_id
                or request.state == "settled"
            ):
                raise HostedInferenceError("hosted uncertain request does not match")
            if request.state == "reserved":
                request.state = "uncertain"
                request.finalized_at = await _now(session)
                await session.flush()


def _charged(
    rows: list[CodingHostedInferenceRequest], actual: str, ceiling: str
) -> int:
    total = 0
    for row in rows:
        value = getattr(row, actual if row.state == "settled" else ceiling)
        if type(value) is not int or value < 0:
            raise HostedInferenceError("hosted accounting is inconsistent")
        total += value
    return total
