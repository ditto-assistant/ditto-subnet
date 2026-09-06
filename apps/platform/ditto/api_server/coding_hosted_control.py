"""Concrete Platform operations for the native authoring coordinator."""

from __future__ import annotations

import asyncio
import base64
import secrets
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_hosted_control import RetentionHeader, SourceBinding
from ditto.api_models.coding_hosted_inference import HostedInferencePolicy
from ditto.api_models.coding_hosted_relay import HostedRelayBinding
from ditto.api_models.coding_inference import _decode_json_document
from ditto.api_server.coding_hosted_authoring_evidence import (
    HostedAuthoringEvidencePublisher,
    canonical,
    owned_source,
    sha,
)
from ditto.api_server.coding_hosted_budget import ProfiledBudgetEstimator
from ditto.api_server.coding_hosted_evidence import HostedInferenceEvidencePublisher
from ditto.api_server.coding_hosted_evidence_spool import HostedEvidenceError
from ditto.api_server.coding_hosted_grading import HostedGradingControl
from ditto.api_server.coding_hosted_inference import HostedInferenceLedger
from ditto.api_server.coding_hosted_inputs import (
    HostedAuthoringInputAssembler,
    HostedAuthoringInputs,
)
from ditto.api_server.coding_hosted_provider import HostedProviderAdapter
from ditto.api_server.coding_hosted_relay import HostedRelayBridge
from ditto.db.models import CodingHostedInferenceGrant
from ditto.db.queries.coding_hosted_admission import _locked_assignment, _now
from ditto.db.queries.coding_hosted_private import (
    _require_worker,
    _selection_matches,
    _task,
    close_hosted_private_task,
    freeze_hosted_private_patch,
)


@dataclass(repr=False)
class _Bridge:
    source: SourceBinding
    grant: UUID | None = None
    binding: HostedRelayBinding | None = None
    bridge: HostedRelayBridge | None = None
    server: asyncio.AbstractServer | None = None
    path: Path | None = None
    token: bytes = b""
    closed: bool = False


class HostedAuthoringControl:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        worker_id: UUID,
        assembler: HostedAuthoringInputAssembler,
        policy: HostedInferencePolicy,
        execution_profile: bytes,
        budget_profile: bytes,
        api_key: str,
        inference_evidence: HostedInferenceEvidencePublisher,
        authoring_evidence: HostedAuthoringEvidencePublisher,
        bridge_root: Path,
        grading: HostedGradingControl | None = None,
        evaluation_id: UUID | None = None,
        _test_transport: httpx.MockTransport | None = None,
    ):
        import os

        if (
            not isinstance(api_key, str)
            or not 1 <= len(api_key) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in api_key)
        ):
            raise HostedEvidenceError("control provider credential is invalid")
        ProfiledBudgetEstimator(profile_bytes=budget_profile, policy=policy)
        info = bridge_root.lstat()
        if (
            not bridge_root.is_absolute()
            or bridge_root.resolve() != bridge_root
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.geteuid()
        ):
            raise HostedEvidenceError("control bridge root is not private")
        profile = _decode_json_document(execution_profile, maximum_bytes=16384)
        if not isinstance(profile, dict) or canonical(profile) != execution_profile:
            raise HostedEvidenceError("control execution profile is invalid")
        limits = profile.get("resource_policy", {}).get("CandidateLimits", {})
        self._patch_limit = limits.get("MaxPatchBytes", 0)
        self._transcript_limit = limits.get("MaxTranscriptBytes", 0)
        if (
            type(self._patch_limit) is not int
            or not 0 < self._patch_limit <= 128 << 20
            or type(self._transcript_limit) is not int
            or not 0 < self._transcript_limit <= 512 << 20
        ):
            raise HostedEvidenceError("control evidence limits are invalid")
        self._sessions, self._worker, self._assembler = sessions, worker_id, assembler
        self._policy, self._execution, self._budget = (
            policy,
            bytes(execution_profile),
            bytes(budget_profile),
        )
        self._key, self._inference, self._evidence = (
            api_key,
            inference_evidence,
            authoring_evidence,
        )
        self._root, self._test_transport = bridge_root, _test_transport
        self._grading = grading
        self._ledger = HostedInferenceLedger(sessions=sessions, worker_id=worker_id)
        self._bridges: dict[UUID, _Bridge] = {}
        self._bound: dict[UUID, SourceBinding] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}
        if evaluation_id is not None and (
            not isinstance(evaluation_id, UUID) or not evaluation_id.int
        ):
            raise HostedEvidenceError("control evaluation is invalid")
        self._evaluation_id = evaluation_id

    async def _owner(self, source: SourceBinding, session: AsyncSession) -> None:
        if (
            self._evaluation_id is not None
            and source.evaluation_id != self._evaluation_id
        ):
            raise HostedEvidenceError("control evaluation differs")
        await owned_source(session, source, self._worker)
        previous = self._bound.get(source.evaluation_id)
        if previous is not None and previous != source:
            raise HostedEvidenceError("control source changed")
        if previous is None:
            if len(self._bound) >= 64:
                raise HostedEvidenceError("control attempt capacity exhausted")
            self._bound[source.evaluation_id] = source

    async def check(self, source: SourceBinding) -> None:
        async with asyncio.timeout(5), self._sessions() as session, session.begin():
            await self._owner(source, session)
            row = await _locked_assignment(session, source.evaluation_id)
            _require_worker(row, source.attempt_id, self._worker)
            task = await _task(session, source.evaluation_id)
            if (
                row.assignment_sha256 != source.assignment_sha256
                or row.authority["execution_profile_sha256"] != sha(self._execution)
                or row.authority["policy_sha256"] != self._policy.digest()
                or task is None
                or task.closed_at is not None
                or task.frozen_at is not None
                or not _selection_matches(task, row)
                or row.expires_at <= await _now(session)
            ):
                raise HostedEvidenceError("control authoring authority unavailable")

    async def authoring(self, source: SourceBinding) -> HostedAuthoringInputs:
        await self.check(source)
        result = await self._assembler.assemble(
            evaluation_id=source.evaluation_id,
            attempt_id=source.attempt_id,
            assignment_sha256=source.assignment_sha256,
        )
        await self.check(source)
        return result

    async def inference(self, source: SourceBinding) -> dict:
        await self.check(source)
        if source.evaluation_id in self._bridges:
            raise HostedEvidenceError("control inference already attempted")
        state = _Bridge(source)
        self._bridges[source.evaluation_id] = state
        # Claim before any await, retaining partial state for revoke/close.
        estimator = ProfiledBudgetEstimator(
            profile_bytes=self._budget, policy=self._policy
        )
        state.grant = await self._ledger.issue(
            evaluation_id=source.evaluation_id,
            attempt_id=source.attempt_id,
            assignment_sha256=source.assignment_sha256,
            policy=self._policy,
            execution_profile=self._execution,
        )
        async with self._sessions() as session:
            grant = await session.get(CodingHostedInferenceGrant, state.grant)
            assert grant is not None
            expiry = int(grant.expires_at.timestamp())
        state.binding = HostedRelayBinding(
            schema="dittobench-coding-hosted-relay-binding-v2",
            evaluation_id=source.evaluation_id,
            attempt_id=source.attempt_id,
            worker_id=self._worker,
            grant_id=state.grant,
            assignment_sha256=source.assignment_sha256,
            policy_sha256=self._policy.digest(),
            artifact_sha256=source.artifact_sha256,
            harness_instance_id=source.harness_instance_id,
            profile_capability_id=source.profile_capability_id,
            expires_at_unix=expiry,
        )
        adapter = HostedProviderAdapter(
            ledger=self._ledger,
            grant_id=state.grant,
            policy=self._policy,
            estimator=estimator,
            api_key=self._key,
            _test_transport=self._test_transport,
        )
        state.token = secrets.token_bytes(32)
        state.path = self._root / f"{source.attempt_id.hex}.sock"
        state.bridge = HostedRelayBridge(
            adapter=adapter,
            binding=state.binding,
            token=state.token,
            retain_evidence=self._inference,
        )
        state.server = await state.bridge.start(state.path)
        await self.check(source)
        return {
            "grant_id": str(state.grant),
            "policy_sha256": self._policy.digest(),
            "socket_path": str(state.path),
            "token_base64": base64.b64encode(state.token).decode(),
            "expires_at_unix": expiry,
        }

    async def revoke(self, source: SourceBinding) -> None:
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            await self._owner(source, session)
        state = self._bridges.get(source.evaluation_id)
        if state is not None and state.source != source:
            raise HostedEvidenceError("control bridge source differs")
        if state is not None and state.bridge is not None:
            if not await state.bridge.revoke():
                raise HostedEvidenceError("control inference is not drained")
        else:
            # A grant commit may have succeeded while its acknowledgement was lost.
            async with self._sessions() as session:
                grant = await session.scalar(
                    select(CodingHostedInferenceGrant.grant_id).where(
                        CodingHostedInferenceGrant.evaluation_id == source.evaluation_id
                    )
                )
            if grant is not None:
                await self._ledger.revoke(grant)
                if not (await self._ledger.accounting(grant)).verified:
                    raise HostedEvidenceError("control inference is not drained")

    async def close(self, source: SourceBinding) -> None:
        await self.revoke(source)
        state = self._bridges.get(source.evaluation_id)
        if state is not None and not state.closed:
            if state.server is not None:
                state.server.close()
                await state.server.wait_closed()
            state.closed = True

    def retention_bounds(self, header: RetentionHeader) -> None:
        if (
            header.freeze_size > 4 * self._patch_limit + (8 << 20)
            or header.transcript_size > self._transcript_limit
        ):
            raise HostedEvidenceError("control retention exceeds approved profile")

    async def retain(
        self,
        source: SourceBinding,
        header: RetentionHeader,
        freeze: bytes,
        transcript: AsyncIterator[bytes],
    ) -> str:
        self.retention_bounds(header)
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            await self._owner(source, session)
        lock = self._locks.setdefault(source.evaluation_id, asyncio.Lock())
        async with lock:
            value = _decode_json_document(freeze, maximum_bytes=header.freeze_size)
            if not isinstance(value, dict) or sha(freeze) != header.freeze_sha256:
                raise HostedEvidenceError("control freeze payload differs")
            submission = value.get("submission")
            patch_sha = None
            patch_size = 0
            if submission is not None:
                if not isinstance(submission, dict) or value.get("failure") is not None:
                    raise HostedEvidenceError("control freeze shape differs")
                patch = base64.b64decode(submission["patch"], validate=True)
                if (
                    len(patch) > self._patch_limit
                    or sha(patch) != submission["frozen_patch_sha256"]
                    or submission["coding_contract_version"] != 2
                    or submission["case_id"] != str(source.attempt_id)
                ):
                    raise HostedEvidenceError("control patch differs")
                document = _decode_json_document(patch, maximum_bytes=self._patch_limit)
                if (
                    not isinstance(document, dict)
                    or document.get("schema") != "dittobench-coding-frozen-patch-v2"
                    or document.get("evaluation_id") != str(source.evaluation_id)
                    or document.get("case_id") != str(source.attempt_id)
                    or document.get("assignment_sha256") != source.assignment_sha256
                    or document.get("coding_contract_version") != 2
                ):
                    raise HostedEvidenceError("control patch authority differs")
                if submission.get("protected_paths_intact") is not True or any(
                    document.get(name) != submission.get(name)
                    for name in ("base_tree_sha256", "visible_bundle_sha256", "changes")
                ):
                    raise HostedEvidenceError("control freeze metadata differs")
                if (
                    submission["authoring_transcript_sha256"]
                    != header.transcript_sha256
                    or submission["authoring_transcript_bytes"]
                    != header.transcript_size
                ):
                    raise HostedEvidenceError("control transcript commitment differs")
                patch_sha, patch_size = sha(patch), len(patch)
                del patch
            elif not isinstance(value.get("failure"), dict):
                raise HostedEvidenceError("control freeze failure missing")
            inference_sha = None
            async with self._sessions() as session:
                grant = await session.scalar(
                    select(CodingHostedInferenceGrant.grant_id).where(
                        CodingHostedInferenceGrant.evaluation_id == source.evaluation_id
                    )
                )
            if header.completed_run:
                if grant is None:
                    raise HostedEvidenceError("control inference grant missing")
                inference_sha = await self._inference.require_complete(grant)
            await self._evidence.prepare(
                source=source,
                header=header,
                freeze=freeze,
                transcript=transcript,
                patch_sha256=patch_sha,
                patch_size=patch_size,
                inference_evidence_sha256=inference_sha,
            )
            return await self._evidence.resume(source)

    async def freeze(
        self, source: SourceBinding, patch: bytes, evidence_sha: str
    ) -> dict:
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            await self._owner(source, session)
            retained = await self._evidence.require_retained(
                session, source, evidence_sha
            )
            if (
                not retained.header.completed_run
                or retained.inference_evidence_sha256 is None
                or retained.patch_sha256 != sha(patch)
                or retained.patch_size != len(patch)
            ):
                raise HostedEvidenceError("control retained patch differs")
            result = await freeze_hosted_private_patch(
                session,
                evaluation_id=source.evaluation_id,
                attempt_id=source.attempt_id,
                worker_id=self._worker,
                patch=patch,
            )
            response = {
                "evaluation_id": str(source.evaluation_id),
                "attempt_id": str(source.attempt_id),
                "assignment_sha256": source.assignment_sha256,
                "frozen_patch_sha256": result.patch_sha256,
            }
        return response

    async def abort(self, source: SourceBinding) -> None:
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            await self._owner(source, session)
            await close_hosted_private_task(
                session,
                evaluation_id=source.evaluation_id,
                attempt_id=source.attempt_id,
                worker_id=self._worker,
                reason="aborted",
            )

    def __repr__(self) -> str:
        return "HostedAuthoringControl(private=True)"

    async def shutdown(self) -> None:
        """Call after the Go worker exits and control handlers have drained.

        Attempt every grant boundary even when another fails. Retain all handles
        and spooled evidence on failure; this does not certify container cleanup.
        """
        failed = False
        for source in tuple(self._bound.values()):
            for operation in (self.close, self.abort):
                try:
                    async with asyncio.timeout(30):
                        await operation(source)
                except Exception:
                    failed = True
        if failed:
            raise HostedEvidenceError("control shutdown is unconfirmed")
