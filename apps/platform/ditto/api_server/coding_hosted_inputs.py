"""Assemble one verified authoring handoff inside the trusted Platform boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import BinaryIO
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_private_catalog_v2 import CodingPrivateCatalogV2Task
from ditto.api_server.coding_private_v2_retrieval import PrivateV2InputRetriever
from ditto.coding_hosted_private import AUTHORING_ROLES
from ditto.db.queries.coding_hosted_admission import _locked_assignment, _now
from ditto.db.queries.coding_hosted_private import (
    _require_worker,
    _selection_matches,
    _task,
)

_MAXIMUMS = dict.fromkeys(AUTHORING_ROLES, 4 << 20)
_MAXIMUMS["visible_bundle"] = 128 << 20
_MAXIMUMS["catalog_record"] = 64 << 10


class HostedInputError(ValueError):
    """Safe failure without private input bytes or storage identities."""


@dataclass(frozen=True, repr=False)
class HostedAuthoringJob:
    evaluation_id: UUID
    attempt_id: UUID
    worker_id: UUID
    assignment_sha256: str
    registration_sha256: str
    execution_profile_sha256: str
    deadline_unix: int
    max_patch_bytes: int
    catalog_index: int
    grant_id: UUID


@dataclass(frozen=True, repr=False)
class HostedAuthoringInputs:
    job: HostedAuthoringJob
    header: bytes
    objects: tuple[bytes, ...]
    recheck: Callable[[], Awaitable[None]]

    async def write_private_frame(self, destination: BinaryIO) -> None:
        """Explicit private worker pipe only, never a miner/validator response.

        The caller owns a bounded, cancellable transport and must not persist
        this plaintext in Git, logs, public receipts or a validator filesystem.
        """
        if int(time.time()) >= self.job.deadline_unix:
            raise HostedInputError("hosted authoring handoff expired")
        await self.recheck()
        for body in (struct.pack(">I", len(self.header)), self.header, *self.objects):
            await self.recheck()
            if destination.write(body) != len(body):
                raise HostedInputError("hosted authoring handoff failed")
        await self.recheck()
        marker = b"DITTO-AUTHORING-READY-V2\n"
        if destination.write(marker) != len(marker):
            raise HostedInputError("hosted authoring handoff failed")


class HostedAuthoringInputAssembler:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        worker_id: UUID,
        retriever: PrivateV2InputRetriever,
    ):
        if not isinstance(worker_id, UUID) or not worker_id.int:
            raise HostedInputError("hosted assembler configuration is invalid")
        self._sessions, self._worker, self._retriever = sessions, worker_id, retriever

    async def _job(
        self, evaluation_id: UUID, attempt_id: UUID, assignment_sha256: str
    ) -> HostedAuthoringJob:
        async with self._sessions() as session, session.begin():
            assignment = await _locked_assignment(session, evaluation_id)
            _require_worker(assignment, attempt_id, self._worker)
            task = await _task(session, evaluation_id)
            if (
                task is None
                or task.closed_at is not None
                or task.frozen_at is not None
                or assignment.expires_at <= await _now(session)
                or assignment.assignment_sha256 != assignment_sha256
                or not _selection_matches(task, assignment)
            ):
                raise HostedInputError("hosted authoring assignment is unavailable")
            return HostedAuthoringJob(
                evaluation_id,
                attempt_id,
                self._worker,
                assignment_sha256,
                assignment.registration_sha256,
                assignment.authority["execution_profile_sha256"],
                int(assignment.expires_at.timestamp()),
                task.max_patch_bytes,
                task.catalog_index,
                task.authoring_grant_id,
            )

    async def assemble(
        self, *, evaluation_id: UUID, attempt_id: UUID, assignment_sha256: str
    ) -> HostedAuthoringInputs:
        try:
            async with asyncio.timeout(180):
                job = await self._job(evaluation_id, attempt_id, assignment_sha256)
                description = await self._retriever.describe_authoring(job.grant_id)
                grant = description.grant
                if (
                    grant.evaluation_id != job.evaluation_id
                    or grant.attempt_id != job.attempt_id
                    or grant.registration_sha256 != job.registration_sha256
                    or grant.catalog_index != job.catalog_index
                    or grant.expires_at_unix != job.deadline_unix
                ):
                    raise HostedInputError("hosted authoring grant does not match")
                objects = []
                entries = []
                for role, digest, size in description.objects:
                    if (
                        role not in _MAXIMUMS
                        or type(size) is not int
                        or not 0 < size <= _MAXIMUMS[role]
                    ):
                        raise HostedInputError("hosted authoring object exceeds bounds")
                    body = await self._retriever.read(grant_id=job.grant_id, role=role)
                    if (
                        type(body) is not bytes
                        or len(body) != size
                        or hashlib.sha256(body).hexdigest() != digest
                    ):
                        raise HostedInputError("hosted authoring object does not match")
                    objects.append(body)
                    entries.append({"role": role, "sha256": digest, "size_bytes": size})
                if tuple(item["role"] for item in entries) != AUTHORING_ROLES:
                    raise HostedInputError("hosted authoring roles are invalid")
                record = CodingPrivateCatalogV2Task.model_validate(
                    json.loads(objects[0])
                )
                if (
                    record.catalog_index != job.catalog_index
                    or record.task_version_id != description.task_version_id
                    or record.task_commitment_sha256
                    != description.task_commitment_sha256
                    or record.corpus_release_id != description.corpus_release_id
                    or record.private_release_sha256
                    != description.private_release_sha256
                ):
                    raise HostedInputError("hosted catalog binding does not match")
                for role, name in (
                    ("issue", "visible_issue_sha256"),
                    ("memory_bundle", "memory_bundle_sha256"),
                    ("runtime_policy", "runtime_policy_sha256"),
                    ("resource_profile", "resource_profile_sha256"),
                ):
                    if getattr(record, name) != next(
                        item["sha256"] for item in entries if item["role"] == role
                    ):
                        raise HostedInputError(
                            "hosted catalog object binding does not match"
                        )
                if (
                    await self._job(evaluation_id, attempt_id, assignment_sha256) != job
                    or await self._retriever.describe_authoring(job.grant_id)
                    != description
                ):
                    raise HostedInputError("hosted authoring grant changed")
                header = coding_canonical_json_bytes(
                    {
                        "schema": "dittobench-coding-hosted-authoring-inputs-v2",
                        "evaluation_id": str(job.evaluation_id),
                        "attempt_id": str(job.attempt_id),
                        "worker_id": str(job.worker_id),
                        "assignment_sha256": job.assignment_sha256,
                        "registration_sha256": job.registration_sha256,
                        "execution_profile_sha256": job.execution_profile_sha256,
                        "task_commitment_sha256": description.task_commitment_sha256,
                        "deadline_unix": job.deadline_unix,
                        "max_patch_bytes": job.max_patch_bytes,
                        "catalog_index": job.catalog_index,
                        "corpus_release_id": description.corpus_release_id,
                        "private_release_sha256": description.private_release_sha256,
                        "objects": entries,
                    },
                    maximum_bytes=16384,
                    label="hosted authoring inputs",
                )

                async def recheck() -> None:
                    try:
                        if (
                            await self._job(
                                evaluation_id, attempt_id, assignment_sha256
                            )
                            != job
                        ):
                            raise HostedInputError("hosted authoring handoff expired")
                    except Exception:
                        raise HostedInputError(
                            "hosted authoring handoff is unavailable"
                        ) from None

                return HostedAuthoringInputs(job, header, tuple(objects), recheck)
        except Exception:
            raise HostedInputError("hosted authoring input assembly failed") from None
