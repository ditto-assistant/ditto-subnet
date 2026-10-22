"""Execute prepared official-control pairs without inventing miner identities.

The caller owns authenticated registry, qualified process and fresh sandbox
admission. This primitive only verifies commitments and reports observations;
it cannot approve a control, complete unrelated policy checks or change scores.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid4

from ditto_screening_protocol.v13_official_execution import (
    ExecutionSubjectV2,
    OfficialPrivateResultV2,
    OfficialPrivateRunSummaryV2,
    PreparedOfficialPackageV2,
    Role,
    prepare_official_package_v2,
)
from ditto_screening_protocol.v13_private_execute import (
    IsolatedCaseObservation,
    PrivateExecutionUnavailable,
    _execute_private_inventory,
)
from ditto_screening_protocol.v13_private_package import SealedPackageStore


@dataclass(frozen=True)
class OfficialRunContextV2:
    task_id: UUID
    group_id: UUID
    role: Role
    subject: ExecutionSubjectV2
    runtime_configuration_sha256: str
    manifest_sha256: str
    pair_inventory_sha256: str


@dataclass(frozen=True)
class OfficialCaseContextV2(OfficialRunContextV2):
    execution_id: UUID
    request_sha256: str


@dataclass(frozen=True)
class OfficialIsolatedCaseObservationV2:
    context: OfficialCaseContextV2
    observation: IsolatedCaseObservation


class FreshOfficialCaseExecutorV2(Protocol):
    """Trusted adapter starts a fresh container, network and broker per call.

    The adapter verifies both archive and config identity from context.subject
    and enforces the approved runtime configuration. Returned context comes
    from actual sandbox admission, rather than echoing unverified inputs.
    """

    async def run_case(
        self,
        *,
        context: OfficialCaseContextV2,
        seed_envelope: Mapping[str, object] | None,
        run_envelope: Mapping[str, object],
        timeout_seconds: float,
    ) -> OfficialIsolatedCaseObservationV2: ...


@dataclass(frozen=True)
class _BoundOfficialExecutor:
    context: OfficialRunContextV2
    executor: FreshOfficialCaseExecutorV2

    async def run_case(
        self,
        *,
        image_sha256: str,
        seed_envelope: Mapping[str, object] | None,
        run_envelope: Mapping[str, object],
        timeout_seconds: float,
    ) -> IsolatedCaseObservation:
        if image_sha256 != self.context.subject.image_sha256:
            raise PrivateExecutionUnavailable("private execution image mismatch")
        if run_envelope.get("tools"):
            raise PrivateExecutionUnavailable("private tool execution unavailable")
        request_bytes = json.dumps(
            {"seed": seed_envelope, "run": run_envelope},
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        context = OfficialCaseContextV2(
            task_id=self.context.task_id,
            group_id=self.context.group_id,
            role=self.context.role,
            subject=self.context.subject,
            runtime_configuration_sha256=self.context.runtime_configuration_sha256,
            manifest_sha256=self.context.manifest_sha256,
            pair_inventory_sha256=self.context.pair_inventory_sha256,
            execution_id=uuid4(),
            request_sha256=hashlib.sha256(request_bytes).hexdigest(),
        )
        async with asyncio.timeout(timeout_seconds):
            result = await self.executor.run_case(
                context=context,
                seed_envelope=seed_envelope,
                run_envelope=run_envelope,
                timeout_seconds=timeout_seconds,
            )
        if result.context != context:
            raise PrivateExecutionUnavailable("private execution context mismatch")
        return result.observation


async def execute_official_private_pairs_v2(
    *,
    prepared: PreparedOfficialPackageV2,
    store: SealedPackageStore,
    executor: FreshOfficialCaseExecutorV2,
    runner_hotkey: str,
) -> OfficialPrivateResultV2:
    """Revalidate sealed state, execute every pair, return digest-only evidence.

    This recall/answer primitive does not validate tool effects or dynamic
    catalog/revocation behavior. Those require trusted endpoint observations
    and separate verification; a completed result cannot fill those checks.
    """
    try:
        async with asyncio.timeout(7200):
            current = await prepare_official_package_v2(
                group=prepared.group,
                registration=prepared.registration,
                store=store,
            )
            if current != prepared:
                raise PrivateExecutionUnavailable("private prepared package mismatch")
            if prepared.manifest.tool_catalog_applicable:
                raise PrivateExecutionUnavailable("private tool execution unavailable")
            context = OfficialRunContextV2(
                task_id=prepared.group.task_id,
                group_id=prepared.group.group_id,
                role=prepared.manifest.role,
                subject=prepared.manifest.subject,
                runtime_configuration_sha256=(
                    prepared.group.approval.runtime_configuration_sha256
                ),
                manifest_sha256=prepared.registration.manifest_sha256,
                pair_inventory_sha256=prepared.registration.pair_inventory_sha256,
            )
            aggregates, evidence_sha256 = await _execute_private_inventory(
                pairs=prepared.manifest.pairs,
                image_sha256=context.subject.image_sha256,
                store=store,
                executor=_BoundOfficialExecutor(context, executor),
            )
            return OfficialPrivateResultV2(
                summary=OfficialPrivateRunSummaryV2(
                    task_id=context.task_id,
                    group_id=context.group_id,
                    role=context.role,
                    subject=context.subject,
                    profile_sha256=prepared.registration.profile_sha256,
                    manifest_sha256=context.manifest_sha256,
                    pair_inventory_sha256=context.pair_inventory_sha256,
                    runner_hotkey=runner_hotkey,
                    status="completed",
                    completed_pairs=len(prepared.manifest.pairs),
                    evidence_sha256=evidence_sha256,
                ),
                aggregates=aggregates,
            )
    except PrivateExecutionUnavailable:
        raise
    except Exception:
        raise PrivateExecutionUnavailable("private execution unavailable") from None
