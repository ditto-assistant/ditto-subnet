"""Phase-bound grading authority and verified terminal publication."""

from __future__ import annotations

import asyncio
import base64
import re
import time
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ditto.api_models.coding_hosted_control import RetentionHeader, SourceBinding
from ditto.api_models.coding_hosted_grading import HostedTerminalIdentity
from ditto.api_models.coding_inference import _decode_json_document
from ditto.api_models.coding_private_catalog_v2 import CodingPrivateCatalogV2Task
from ditto.api_server.coding_hippius_evidence import HippiusSealedEvidenceNotFound
from ditto.api_server.coding_hosted_authoring_evidence import (
    HostedAuthoringEvidencePublisher,
    canonical,
    check_blob,
    owned_source,
    projection,
    sha,
)
from ditto.api_server.coding_hosted_evidence_spool import HostedEvidenceError
from ditto.api_server.coding_private_v2_retrieval import PrivateV2InputRetriever
from ditto.coding_hosted_private import GRADING_ROLES
from ditto.db.models import (
    CodingHostedAssignment,
    CodingHostedGradingClaim,
    CodingHostedTerminalFinalization,
    CodingHostedTerminalReservation,
)
from ditto.db.queries.coding_hosted_admission import _locked_assignment, _now
from ditto.db.queries.coding_hosted_private import (
    _selection_matches,
    _task,
    close_hosted_private_task,
)


def _hash(value) -> str:
    return sha(canonical(value, 65536))


def _command(value: dict) -> dict:
    return {
        "id": value["ID"],
        "argv": value["Argv"],
        "timeout_milliseconds": value["Timeout"] // 1_000_000,
    }


def _resource(value: dict) -> dict:
    names = {
        "MaxBundleBytes": "max_bundle_bytes",
        "MaxWorkspaceBytes": "max_workspace_bytes",
        "MaxFileBytes": "max_file_bytes",
        "MaxPatchBytes": "max_patch_bytes",
        "MaxEntries": "max_entries",
        "MaxToolCalls": "max_tool_calls",
        "MaxReadBytes": "max_read_bytes",
        "MaxResponseBytes": "max_response_bytes",
        "MaxSearchResults": "max_search_results",
        "MaxReplayCacheBytes": "max_replay_cache_bytes",
        "MaxTranscriptBytes": "max_transcript_bytes",
    }
    return {
        "schema": "dittobench-coding-grader-resource-v2",
        "candidate_limits": {b: value["CandidateLimits"][a] for a, b in names.items()},
        "protected_limits": {b: value["ProtectedLimits"][a] for a, b in names.items()},
        "max_combined_disk_bytes": value["MaxCombinedDiskBytes"],
        "memory_limit_bytes": value["MemoryLimitBytes"],
        "scratch_limit_bytes": value["ScratchLimitBytes"],
        "pids_limit": value["PidsLimit"],
        "cpu_quota_millis": value["CPUQuotaMillis"],
    }


def expected_grading(profile: dict, source: SourceBinding, submission: dict) -> dict:
    groups = [
        {
            "group": g["Group"],
            "command": _command(g["Command"]),
            "expected_total": g["ExpectedTotal"],
        }
        for g in profile["test_groups"]
    ]
    if [g["group"] for g in groups] != ["hidden", "visible"] or any(
        type(g["expected_total"]) is not int
        or not 1 <= g["expected_total"] <= 1_000_000
        for g in groups
    ):
        raise HostedEvidenceError("grading groups invalid")
    resource_sha = _hash(_resource(profile["resource_policy"]))
    plan = {
        "schema": "dittobench-coding-grader-plan-v2",
        "coding_contract_version": 2,
        "case_id": str(source.attempt_id),
        "variant_id": "hosted-private",
        "visible_bundle_sha256": submission["visible_bundle_sha256"],
        "base_tree_sha256": submission["base_tree_sha256"],
        "grader_contract_sha256": profile["grader_contract_sha256"],
        "grader_bundle_sha256": profile["grader_bundle_sha256"],
        "grader_image_digest": profile["image_digest"],
        "grader_platform": "linux/amd64",
        "test_manifest_sha256": profile["test_manifest_sha256"],
        "resource_profile_sha256": resource_sha,
        "execution_timeout_milliseconds": profile["execution_timeout"] // 1_000_000,
        "build_required": profile["build"]["Required"],
        "build_command": _command(profile["build"]["Command"]),
        "test_groups": groups,
        "execution_order": ["visible", "hidden"],
    }
    return {
        "grader_plan_sha256": _hash(plan),
        "resource_profile_sha256": resource_sha,
        "grader_contract_sha256": profile["grader_contract_sha256"],
        "grader_bundle_sha256": profile["grader_bundle_sha256"],
        "grader_image_digest": profile["image_digest"],
        "test_manifest_sha256": profile["test_manifest_sha256"],
        "final_tree_sha256": submission["final_tree_sha256"],
        "build_required": profile["build"]["Required"],
        "build_command_id": profile["build"]["Command"]["ID"],
        "build_command_sha256": _hash(_command(profile["build"]["Command"])),
        "groups": [
            {
                "group": g["group"],
                "total": g["expected_total"],
                "command_id": g["command"]["id"],
                "command_sha256": _hash(g["command"]),
            }
            for g in groups
        ],
    }


def validate_grading_result(value: dict, binding: dict) -> str:
    result = value.get("grader_result", {})
    if (
        value.get("schema") != "dittobench-coding-hosted-terminal-v2"
        or value.get("authoring_evidence_sha256")
        != binding["authoring_evidence_sha256"]
        or value.get("grading_profile_sha256") != binding["grading_profile_sha256"]
        or result.get("schema") != "dittobench-coding-hosted-grading-result-v2"
        or type(result.get("coding_contract_version")) is not int
        or result["coding_contract_version"] != 2
        or result.get("shadow_only") is not True
        or result.get("weight_eligible") is not False
        or result.get("assignment_sha256") != binding["source"]["assignment_sha256"]
        or result.get("frozen_patch_sha256") != binding["frozen_patch_sha256"]
    ):
        raise HostedEvidenceError("terminal grading identity differs")
    result = result["result"]
    domain = result.get("terminal_domain")
    outcomes = {
        "resolved": "completed",
        "repair_failure": "candidate_failure",
        "candidate_integrity": "integrity_failure",
        "control_plane_integrity": "integrity_failure",
        "validator_infrastructure": "infrastructure_failure",
        "task_invalid": "infrastructure_failure",
    }
    if (
        domain not in outcomes
        or type(result.get("repair_score_micros")) is not int
        or result["repair_score_micros"] != (1_000_000 if domain == "resolved" else 0)
    ):
        raise HostedEvidenceError("terminal outcome invalid")
    evidence = result.get("grader")
    if evidence is None:
        if domain in {"resolved", "repair_failure", "candidate_integrity"}:
            raise HostedEvidenceError("terminal grading evidence missing")
        return outcomes[domain]
    for key in (
        "grader_contract_sha256",
        "grader_bundle_sha256",
        "grader_image_digest",
        "test_manifest_sha256",
        "grader_plan_sha256",
        "resource_profile_sha256",
    ):
        if evidence.get(key) != binding[key]:
            raise HostedEvidenceError("terminal grader profile differs")
    if (
        result.get("replayed_final_tree_sha256") != binding["final_tree_sha256"]
        or evidence.get("grader_platform") != "linux/amd64"
    ):
        raise HostedEvidenceError("terminal replay differs")
    for value in (
        result.get("protected_grader_tree_sha256"),
        evidence.get("grader_integrity_before_sha256"),
        evidence.get("grader_integrity_after_sha256"),
    ):
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise HostedEvidenceError("terminal integrity commitment missing")
    before = sha(
        b"dittobench-coding-grader-integrity-v1\0"
        + binding["final_tree_sha256"].encode()
        + b"\0"
        + result["protected_grader_tree_sha256"].encode()
    )
    if evidence["grader_integrity_before_sha256"] != before:
        raise HostedEvidenceError("terminal integrity commitment differs")
    groups = {g["group"]: g for g in binding["groups"]}
    expected_order = ([None] if binding["build_required"] else []) + [
        "visible",
        "hidden",
    ]
    receipts = result.get("execution_receipts")
    if not isinstance(receipts, list) or not 1 <= len(receipts) <= len(expected_order):
        raise HostedEvidenceError("terminal receipt count invalid")
    root = "0" * 64
    for i, receipt in enumerate(receipts):
        instance = receipt.get("executor_instance_id")
        if (
            not isinstance(instance, str)
            or not 1 <= len(instance) <= 256
            or any(c.isspace() or ord(c) < 32 for c in instance)
            or instance != receipts[0].get("executor_instance_id")
        ):
            raise HostedEvidenceError("terminal executor identity differs")
        group = expected_order[i]
        command_id = (
            binding["build_command_id"]
            if group is None
            else groups[group]["command_id"]
        )
        command_sha = (
            binding["build_command_sha256"]
            if group is None
            else groups[group]["command_sha256"]
        )
        if (
            receipt.get("schema") != "dittobench-coding-grader-receipt-v2"
            or any(
                type(receipt.get(k)) is not int
                for k in ("sequence", "returncode", "passed", "total")
            )
            or any(type(receipt.get(k)) is not bool for k in ("completed", "timed_out"))
            or receipt.get("sequence") != i + 1
            or receipt.get("previous_receipt_sha256") != root
            or receipt.get("group") != group
            or receipt.get("phase") != ("build" if group is None else "test")
            or receipt.get("command_id") != command_id
            or receipt.get("command_sha256") != command_sha
        ):
            raise HostedEvidenceError("terminal execution receipt differs")
        if group is not None and (
            receipt.get("total") != groups[group]["total"]
            or type(receipt.get("passed")) is not int
            or not 0 <= receipt["passed"] <= receipt["total"]
        ):
            raise HostedEvidenceError("terminal test count differs")
        root = _hash(receipt)
    if (
        root != result.get("execution_receipt_root_sha256")
        or root != evidence.get("execution_receipt_root_sha256")
        or evidence.get("execution_receipt_count") != len(receipts)
        or type(evidence.get("execution_receipt_count")) is not int
    ):
        raise HostedEvidenceError("terminal receipt root differs")
    if domain == "resolved":
        if (
            len(receipts) != len(expected_order)
            or result.get("failure_code") is not None
            or evidence.get("grader_integrity_before_sha256")
            != evidence.get("grader_integrity_after_sha256")
            or evidence.get("build", {}).get("passed") is not True
        ):
            raise HostedEvidenceError("terminal success proof incomplete")
        for r in receipts:
            if (
                r.get("completed") is not True
                or r.get("timed_out") is not False
                or r.get("returncode") != 0
                or r.get("passed") != r.get("total")
            ):
                raise HostedEvidenceError("terminal success receipt failed")
        if [g.get("group") for g in evidence.get("test_groups", [])] != [
            "hidden",
            "visible",
        ]:
            raise HostedEvidenceError("terminal groups missing")
        for g in evidence["test_groups"]:
            if (
                g.get("passed") != groups[g["group"]]["total"]
                or g.get("total") != groups[g["group"]]["total"]
            ):
                raise HostedEvidenceError("terminal success counts differ")
    return outcomes[domain]


class HostedGradingControl:
    def __init__(
        self,
        *,
        sessions: async_sessionmaker[AsyncSession],
        worker_id: UUID,
        retriever: PrivateV2InputRetriever,
        authoring_evidence: HostedAuthoringEvidencePublisher,
        cipher_store: HostedAuthoringEvidencePublisher,
        profile: bytes,
    ):
        self._sessions, self._worker, self._retriever, self._authoring, self._cipher = (
            sessions,
            worker_id,
            retriever,
            authoring_evidence,
            cipher_store,
        )
        value = _decode_json_document(profile, maximum_bytes=65536)
        if (
            not isinstance(value, dict)
            or value.get("schema") != "dittobench-coding-hosted-grading-profile-v2"
            or canonical(value, 65536) != profile
        ):
            raise HostedEvidenceError("grading profile invalid")
        self._profile, self._profile_sha = value, sha(profile)
        self._locks: dict[UUID, asyncio.Lock] = {}

    async def _active(self, session: AsyncSession, source: SourceBinding):
        await owned_source(session, source, self._worker)
        row = await _locked_assignment(session, source.evaluation_id)
        task = await _task(session, source.evaluation_id)
        if (
            task is None
            or task.frozen_at is None
            or task.closed_at is not None
            or not _selection_matches(task, row)
            or row.expires_at <= await _now(session)
            or row.authority["grading_profile_sha256"] != self._profile_sha
        ):
            raise HostedEvidenceError("grading authority unavailable")
        return row, task

    async def check(self, source: SourceBinding) -> None:
        async with asyncio.timeout(5), self._sessions() as session, session.begin():
            await self._active(session, source)

    async def claim(
        self,
        source: SourceBinding,
        evidence_sha: str,
        header: RetentionHeader,
        freeze: bytes,
    ):
        if sha(freeze) != header.freeze_sha256 or len(freeze) != header.freeze_size:
            raise HostedEvidenceError("grading freeze bytes differ")
        value = _decode_json_document(freeze, maximum_bytes=520 << 20)
        submission = value.get("submission") if isinstance(value, dict) else None
        if not isinstance(submission, dict):
            raise HostedEvidenceError("grading requires a successful freeze")
        expected = expected_grading(self._profile, source, submission)
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            _, task = await self._active(session, source)
            retained = await self._authoring.require_retained(
                session, source, evidence_sha
            )
            if (
                retained.header != header
                or retained.patch_sha256 != task.frozen_patch_sha256
                or sha(base64.b64decode(submission["patch"], validate=True))
                != task.frozen_patch_sha256
            ):
                raise HostedEvidenceError("grading freeze authority differs")
            if (
                await session.get(CodingHostedGradingClaim, source.evaluation_id)
                is not None
            ):
                raise HostedEvidenceError("grading was already claimed")
            claim = uuid4()
            binding = dict(
                expected,
                source=projection(source),
                grading_profile_sha256=self._profile_sha,
                authoring_evidence_sha256=evidence_sha,
                frozen_patch_sha256=task.frozen_patch_sha256,
            )
            session.add(
                CodingHostedGradingClaim(
                    evaluation_id=source.evaluation_id, claim_id=claim, binding=binding
                )
            )
            grant = task.grading_grant_id
        return claim, binding, grant

    async def inputs(
        self,
        source: SourceBinding,
        evidence_sha: str,
        header: RetentionHeader,
        freeze: bytes,
    ):
        claim, binding, grant = await self.claim(source, evidence_sha, header, freeze)
        description = await self._retriever.describe_grading(grant)
        if description.grant.frozen_patch_sha256 != binding["frozen_patch_sha256"]:
            raise HostedEvidenceError("grading grant freeze differs")
        objects = []
        entries = []
        for role, digest, size in description.objects:
            maximum = {
                "catalog_record": 64 << 10,
                "visible_bundle": 128 << 20,
                "grader_bundle": 64 << 20,
            }.get(role, 4 << 20)
            if not 0 < size <= maximum:
                raise HostedEvidenceError("grading object bound exceeded")
            entries.append({"role": role, "sha256": digest, "size_bytes": size})
            if role == "grader_bundle":
                continue
            body = await self._retriever.read(grant_id=grant, role=role)
            if len(body) != size or sha(body) != digest:
                raise HostedEvidenceError("grading object differs")
            objects.append(body)
        if (
            tuple(e["role"] for e in entries) != GRADING_ROLES
            or entries[2]["sha256"] != self._profile["grader_bundle_sha256"]
        ):
            raise HostedEvidenceError("grading payload profile differs")
        record = CodingPrivateCatalogV2Task.model_validate_json(objects[0])
        if (
            record.task_commitment_sha256 != description.task_commitment_sha256
            or record.catalog_index != description.grant.catalog_index
        ):
            raise HostedEvidenceError("grading catalog differs")
        await self.check(source)
        return {
            "schema": "dittobench-coding-hosted-grading-inputs-v2",
            "claim_id": str(claim),
            "grading_profile_sha256": self._profile_sha,
            "frozen_patch_sha256": binding["frozen_patch_sha256"],
            "grader_plan_sha256": binding["grader_plan_sha256"],
            "objects": entries,
        }, objects

    async def protected(self, source: SourceBinding, claim_id: UUID) -> bytes:
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            _, task = await self._active(session, source)
            claim = await session.get(CodingHostedGradingClaim, source.evaluation_id)
            if (
                claim is None
                or claim.claim_id != claim_id
                or claim.binding["source"] != projection(source)
                or claim.binding["frozen_patch_sha256"] != task.frozen_patch_sha256
            ):
                raise HostedEvidenceError("protected grader claim differs")
            grant = task.grading_grant_id
        body = await self._retriever.read(grant_id=grant, role="grader_bundle")
        if sha(body) != self._profile["grader_bundle_sha256"] or len(body) > min(
            64 << 20,
            self._profile["resource_policy"]["ProtectedLimits"]["MaxBundleBytes"],
        ):
            raise HostedEvidenceError("protected grader bytes differ")
        await self.check(source)
        return body

    def _fresh(self, identity: HostedTerminalIdentity) -> None:
        checked = datetime.fromisoformat(
            self._cipher._probe.checked_at.replace("Z", "+00:00")
        )
        if (
            not 0 <= (datetime.now(UTC) - checked).total_seconds() < 86400
            or time.time() >= identity.publication_deadline_unix
            or identity.blob.storage_domain_sha256 != self._cipher._domain
        ):
            raise HostedEvidenceError("terminal publication expired")

    async def terminal(self, source: SourceBinding, body: bytes) -> str:
        value = _decode_json_document(body, maximum_bytes=4 << 20)
        if not isinstance(value, dict):
            raise HostedEvidenceError("terminal result invalid")
        async with asyncio.timeout(20), self._sessions() as session, session.begin():
            await owned_source(session, source, self._worker)
            claim = await session.get(CodingHostedGradingClaim, source.evaluation_id)
            if (
                claim is None
                or value.get("claim_id") != str(claim.claim_id)
                or claim.binding["source"] != projection(source)
            ):
                raise HostedEvidenceError("terminal claim differs")
            binding = claim.binding
        outcome = validate_grading_result(value, binding)
        lock = self._locks.setdefault(source.evaluation_id, asyncio.Lock())
        async with lock:
            key = uuid5(source.attempt_id, "hosted-terminal-record-v2")
            existing = self._cipher._existing(key)
            if existing is None:
                try:
                    await self.check(source)
                except Exception:
                    outcome = "infrastructure_failure"
                payload = canonical(
                    {
                        "schema": "dittobench-coding-sealed-terminal-v2",
                        "source": projection(source),
                        "platform_outcome": outcome,
                        "result": value,
                    },
                    4 << 20,
                )
                async with self._cipher._spool.lock:
                    blob, sealed = await self._cipher._prepare(
                        uuid5(source.attempt_id, "hosted-terminal-blob-v2"),
                        source.digest(),
                        sha(body),
                        -1,
                        payload,
                    )
                    identity = HostedTerminalIdentity.model_validate_json(
                        canonical(
                            {
                                "schema": "dittobench-coding-terminal-evidence-v2",
                                "source": projection(source),
                                "claim_id": str(claim.claim_id),
                                "authoring_evidence_sha256": binding[
                                    "authoring_evidence_sha256"
                                ],
                                "grading_profile_sha256": binding[
                                    "grading_profile_sha256"
                                ],
                                "result_sha256": sha(body),
                                "outcome": outcome,
                                "blob": projection(blob),
                                "publication_deadline_unix": source.deadline_unix
                                + 86400,
                                "weight_eligible": False,
                            }
                        )
                    )
                    self._cipher._spool.store(
                        key, canonical(projection(identity)), sealed
                    )
            else:
                identity = HostedTerminalIdentity.model_validate_json(
                    canonical(existing[0])
                )
                sealed = existing[1]
                if (
                    identity.source != source
                    or identity.result_sha256 != sha(body)
                    or identity.claim_id != claim.claim_id
                ):
                    raise HostedEvidenceError("terminal replay conflicts")
            check_blob(identity.blob, sealed)
            async with (
                asyncio.timeout(20),
                self._sessions() as session,
                session.begin(),
            ):
                await owned_source(session, source, self._worker)
                await session.get(
                    CodingHostedAssignment, source.evaluation_id, with_for_update=True
                )
                row = await session.get(
                    CodingHostedTerminalReservation, source.evaluation_id
                )
                if row is None:
                    session.add(
                        CodingHostedTerminalReservation(
                            evaluation_id=source.evaluation_id,
                            identity_sha256=identity.digest(),
                            identity=projection(identity),
                        )
                    )
                elif (
                    row.identity_sha256 != identity.digest()
                    or row.identity != projection(identity)
                ):
                    raise HostedEvidenceError("terminal reservation conflicts")
                if (
                    await session.get(
                        CodingHostedTerminalFinalization, source.evaluation_id
                    )
                    is not None
                ):
                    return identity.digest()
            self._fresh(identity)
            remote = (
                f"coding-hosted-terminal/v2/{identity.blob.object_id.hex}/"
                f"{identity.blob.ciphertext_sha256}.bin"
            )
            try:
                async with asyncio.timeout(20):
                    found = await self._cipher._transport.get_object(
                        key=remote, max_bytes=len(sealed)
                    )
            except HippiusSealedEvidenceNotFound:
                self._fresh(identity)
                async with asyncio.timeout(20):
                    await self._cipher._transport.put_object(
                        key=remote, body=sealed, metadata={}
                    )
            else:
                check_blob(identity.blob, found)
            self._fresh(identity)
            async with asyncio.timeout(20):
                found = await self._cipher._transport.get_object(
                    key=remote, max_bytes=len(sealed)
                )
            check_blob(identity.blob, found)
            self._fresh(identity)
            async with (
                asyncio.timeout(20),
                self._sessions() as session,
                session.begin(),
            ):
                await owned_source(session, source, self._worker)
                await session.get(
                    CodingHostedAssignment, source.evaluation_id, with_for_update=True
                )
                if (
                    await session.get(
                        CodingHostedTerminalFinalization, source.evaluation_id
                    )
                    is None
                ):
                    session.add(
                        CodingHostedTerminalFinalization(
                            evaluation_id=source.evaluation_id,
                            probe_sha256=self._cipher._probe_sha,
                        )
                    )
                await close_hosted_private_task(
                    session,
                    evaluation_id=source.evaluation_id,
                    attempt_id=source.attempt_id,
                    worker_id=self._worker,
                    reason="completed" if identity.outcome == "completed" else "failed",
                )
            return identity.digest()

    def __repr__(self) -> str:
        return "HostedGradingControl(private=True)"
