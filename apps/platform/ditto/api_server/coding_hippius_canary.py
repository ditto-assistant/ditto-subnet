"""Synthetic single-validator canary for the Hippius Coding data plane."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID

from ditto.api_models.coding_artifacts import CodingArtifactDeliveryPhase
from ditto.api_models.coding_canonical import coding_canonical_json_bytes
from ditto.api_models.coding_evidence import CodingSealedEvidenceKind
from ditto.api_models.coding_selection import (
    CodingCatalogBudgets,
    CodingCatalogGraderPlan,
    CodingCatalogIssue,
    CodingCatalogResourceProfile,
    CodingCatalogRunnerPlan,
    CodingCatalogRuntimePolicy,
    CodingPrivateCatalogRecord,
)
from ditto.api_server.coding_hippius_custody import (
    HippiusEvidenceCustodyReadiness,
)
from ditto.api_server.coding_hippius_evidence import (
    HippiusSealedEvidenceReceipt,
    HippiusSealedEvidenceSourceAuthority,
)
from ditto.api_server.coding_hippius_retrieval import (
    HippiusPrivateInputTicketAuthority,
)

HIPPIUS_SHADOW_CANARY_CONFIRMATION = "RUN HIPPIUS CODING SHADOW CANARY"
HIPPIUS_SHADOW_CANARY_CORPUS_PREFIX = "hippius-synthetic-canary-"

_RECEIPT_SCHEMA = "dittobench-coding-hippius-shadow-canary-receipt-v1"
_RUN_SCHEMA = "dittobench-coding-hippius-shadow-canary-run-v1"
_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_RECORD_BYTES = 2 << 20
_MAX_RECEIPT_BYTES = 64 << 10
_MAX_SYNTHETIC_EVIDENCE_BYTES = 8 << 20


class HippiusShadowCanaryError(RuntimeError):
    """Canary configuration, authority, or execution failed without secrets."""


class HippiusShadowCanaryIntegrity(HippiusShadowCanaryError):
    """The synthetic run disagrees with its immutable authority."""


class HippiusShadowCanaryUnavailable(HippiusShadowCanaryError):
    """A trusted canary dependency is temporarily unavailable."""


@dataclass(frozen=True, repr=False)
class HippiusShadowCanaryPlan:
    canary_id: UUID
    source_sha: str
    synthetic_corpus_release_id: str
    synthetic_record_sha256: str
    private_input: HippiusPrivateInputTicketAuthority
    sealed_evidence: HippiusSealedEvidenceSourceAuthority
    synthetic_only: Literal[True] = True
    single_validator: Literal[True] = True
    weight_eligible: Literal[False] = False

    def __repr__(self) -> str:
        return (
            "HippiusShadowCanaryPlan(synthetic_only=True, "
            "single_validator=True, weight_eligible=False)"
        )


@dataclass(frozen=True, repr=False)
class HippiusShadowCanaryAuthoringMaterial:
    execution_authority_sha256: str
    task_commitment_sha256: str
    ticket_deadline: datetime
    issue: CodingCatalogIssue
    runtime_policy: CodingCatalogRuntimePolicy
    budgets: CodingCatalogBudgets
    runner_plan: CodingCatalogRunnerPlan

    def __repr__(self) -> str:
        return "HippiusShadowCanaryAuthoringMaterial(phase='authoring')"


@dataclass(frozen=True, repr=False)
class HippiusShadowCanaryAuthoringOutcome:
    execution_authority_sha256: str
    task_commitment_sha256: str
    transcript: bytes
    frozen_submission: bytes
    resolved: bool

    def __repr__(self) -> str:
        return "HippiusShadowCanaryAuthoringOutcome(sealed=True)"


@dataclass(frozen=True, repr=False)
class HippiusShadowCanaryGradingMaterial:
    execution_authority_sha256: str
    task_commitment_sha256: str
    ticket_deadline: datetime
    frozen_submission: bytes
    frozen_submission_sha256: str
    grader_plan: CodingCatalogGraderPlan
    resource_profile: CodingCatalogResourceProfile

    def __repr__(self) -> str:
        return "HippiusShadowCanaryGradingMaterial(phase='grading')"


@dataclass(frozen=True, repr=False)
class HippiusShadowCanaryGradingOutcome:
    execution_authority_sha256: str
    task_commitment_sha256: str
    frozen_submission_sha256: str
    terminal_evidence: bytes
    resolved: bool
    pristine: bool

    def __repr__(self) -> str:
        return "HippiusShadowCanaryGradingOutcome(sealed=True)"


@dataclass(frozen=True)
class HippiusShadowCanaryEvidenceReceipt:
    evidence_kind: str
    identity_sha256: str
    object_key_sha256: str
    ciphertext_sha256: str
    ciphertext_size_bytes: int
    envelope_sha256: str
    status: str
    finalized_at: str


@dataclass(frozen=True)
class HippiusShadowCanaryReceipt:
    schema: str
    source_sha: str
    checked_at: str
    canary_id_sha256: str
    ticket_id_sha256: str
    validator_hotkey_sha256: str
    synthetic_corpus_release_sha256: str
    synthetic_record_sha256: str
    task_commitment_sha256: str
    execution_authority_sha256: str
    private_input_authority_sha256: str
    sealed_evidence_authority_sha256: str
    probe_receipt_payload_sha256: str
    wrapping_key_sha256: str
    authoring_transcript_sha256: str
    frozen_submission_sha256: str
    terminal_evidence_sha256: str
    evidence: tuple[HippiusShadowCanaryEvidenceReceipt, ...]
    canary_run_sha256: str
    synthetic_only: bool
    single_validator: bool
    ready: bool
    worker_active: bool
    weight_eligible: bool


class HippiusShadowCanaryPrivateInput(Protocol):
    @property
    def authority_sha256(self) -> str: ...

    async def get_task_material(
        self,
        *,
        authority: HippiusPrivateInputTicketAuthority,
        now: datetime | None = None,
    ) -> CodingPrivateCatalogRecord: ...


class HippiusShadowCanaryEvidence(Protocol):
    readiness: HippiusEvidenceCustodyReadiness

    async def prepare_and_store(
        self,
        *,
        authority: HippiusSealedEvidenceSourceAuthority,
        plaintext: bytes,
    ) -> str: ...

    async def publish(self, identity_sha256: str) -> HippiusSealedEvidenceReceipt: ...


class HippiusShadowCanaryAuthoringExecutor(Protocol):
    async def execute_authoring(
        self,
        *,
        material: HippiusShadowCanaryAuthoringMaterial,
    ) -> HippiusShadowCanaryAuthoringOutcome: ...


class HippiusShadowCanaryGradingExecutor(Protocol):
    async def execute_grading(
        self,
        *,
        material: HippiusShadowCanaryGradingMaterial,
    ) -> HippiusShadowCanaryGradingOutcome: ...


async def run_hippius_shadow_canary(
    *,
    plan: HippiusShadowCanaryPlan,
    private_input: HippiusShadowCanaryPrivateInput,
    evidence: HippiusShadowCanaryEvidence,
    authoring: HippiusShadowCanaryAuthoringExecutor,
    grading: HippiusShadowCanaryGradingExecutor,
    confirmation: str,
    deployed_source_sha: str,
    now: datetime | None = None,
) -> HippiusShadowCanaryReceipt:
    """Run one synthetic phase-separated canary and seal its exact evidence."""

    checked_at = _utc_now(now)
    _validate_plan(
        plan,
        checked_at=checked_at,
        deployed_source_sha=deployed_source_sha,
    )
    if confirmation != HIPPIUS_SHADOW_CANARY_CONFIRMATION:
        raise HippiusShadowCanaryIntegrity("Hippius shadow canary is not confirmed")
    readiness = _validate_readiness(evidence.readiness)
    if private_input.authority_sha256 != readiness.private_input_authority_sha256:
        raise HippiusShadowCanaryIntegrity(
            "Hippius canary private-input authority is inconsistent"
        )

    authoring_record = await _retrieve(
        private_input,
        authority=plan.private_input,
        now=checked_at,
    )
    record_sha256 = _record_sha256(authoring_record)
    _validate_synthetic_record(plan, authoring_record, record_sha256=record_sha256)
    task_commitment_sha256 = authoring_record.task_version.task_commitment_sha256
    execution_authority_sha256 = _execution_authority_sha256(plan)
    authoring_material = HippiusShadowCanaryAuthoringMaterial(
        execution_authority_sha256=execution_authority_sha256,
        task_commitment_sha256=task_commitment_sha256,
        ticket_deadline=plan.private_input.ticket_deadline,
        issue=authoring_record.issue,
        runtime_policy=authoring_record.runtime_policy,
        budgets=authoring_record.budgets,
        runner_plan=authoring_record.runner_plan,
    )
    try:
        authoring_outcome = await authoring.execute_authoring(
            material=authoring_material
        )
    except Exception as error:
        raise HippiusShadowCanaryUnavailable(
            "Hippius shadow authoring execution failed"
        ) from error
    _validate_authoring(authoring_outcome, material=authoring_material)

    sealed_receipts = [
        await _seal(
            evidence,
            authority=plan.sealed_evidence,
            kind=CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
            plaintext=authoring_outcome.transcript,
        ),
        await _seal(
            evidence,
            authority=plan.sealed_evidence,
            kind=CodingSealedEvidenceKind.FROZEN_SUBMISSION,
            plaintext=authoring_outcome.frozen_submission,
        ),
    ]

    grading_authority = replace(
        plan.private_input,
        delivery_phase=CodingArtifactDeliveryPhase.GRADING,
    )
    grading_record = await _retrieve(
        private_input,
        authority=grading_authority,
        now=checked_at,
    )
    if _record_sha256(grading_record) != record_sha256:
        raise HippiusShadowCanaryIntegrity(
            "Hippius canary material changed between execution phases"
        )
    frozen_submission_sha256 = hashlib.sha256(
        authoring_outcome.frozen_submission
    ).hexdigest()
    grading_material = HippiusShadowCanaryGradingMaterial(
        execution_authority_sha256=execution_authority_sha256,
        task_commitment_sha256=task_commitment_sha256,
        ticket_deadline=plan.private_input.ticket_deadline,
        frozen_submission=authoring_outcome.frozen_submission,
        frozen_submission_sha256=frozen_submission_sha256,
        grader_plan=grading_record.grader_plan,
        resource_profile=grading_record.grader_resource_profile,
    )
    try:
        grading_outcome = await grading.execute_grading(material=grading_material)
    except Exception as error:
        raise HippiusShadowCanaryUnavailable(
            "Hippius shadow grading execution failed"
        ) from error
    _validate_grading(grading_outcome, material=grading_material)
    sealed_receipts.append(
        await _seal(
            evidence,
            authority=plan.sealed_evidence,
            kind=CodingSealedEvidenceKind.TERMINAL_PUBLICATION_REQUEST,
            plaintext=grading_outcome.terminal_evidence,
        )
    )

    evidence_receipts = tuple(
        _redacted_evidence(item, kind=kind)
        for item, kind in zip(
            sealed_receipts,
            (
                CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT,
                CodingSealedEvidenceKind.FROZEN_SUBMISSION,
                CodingSealedEvidenceKind.TERMINAL_PUBLICATION_REQUEST,
            ),
            strict=True,
        )
    )
    receipt_values: dict[str, object] = {
        "schema": _RECEIPT_SCHEMA,
        "source_sha": plan.source_sha,
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
        "canary_id_sha256": _scalar_sha256(str(plan.canary_id)),
        "ticket_id_sha256": _scalar_sha256(str(plan.private_input.ticket_id)),
        "validator_hotkey_sha256": _scalar_sha256(plan.private_input.validator_hotkey),
        "synthetic_corpus_release_sha256": _scalar_sha256(
            plan.synthetic_corpus_release_id
        ),
        "synthetic_record_sha256": record_sha256,
        "task_commitment_sha256": task_commitment_sha256,
        "execution_authority_sha256": execution_authority_sha256,
        "private_input_authority_sha256": readiness.private_input_authority_sha256,
        "sealed_evidence_authority_sha256": (
            readiness.sealed_evidence_authority_sha256
        ),
        "probe_receipt_payload_sha256": readiness.probe_receipt_payload_sha256,
        "wrapping_key_sha256": readiness.wrapping_key_sha256,
        "authoring_transcript_sha256": hashlib.sha256(
            authoring_outcome.transcript
        ).hexdigest(),
        "frozen_submission_sha256": frozen_submission_sha256,
        "terminal_evidence_sha256": hashlib.sha256(
            grading_outcome.terminal_evidence
        ).hexdigest(),
        "evidence": evidence_receipts,
        "synthetic_only": True,
        "single_validator": True,
        "ready": True,
        "worker_active": False,
        "weight_eligible": False,
    }
    canary_run_sha256 = hashlib.sha256(
        coding_canonical_json_bytes(
            {
                **receipt_values,
                "evidence": [asdict(item) for item in evidence_receipts],
                "schema": _RUN_SCHEMA,
            },
            maximum_bytes=_MAX_RECEIPT_BYTES,
            label="Hippius shadow canary run",
        )
    ).hexdigest()
    receipt = HippiusShadowCanaryReceipt(
        schema=_RECEIPT_SCHEMA,
        source_sha=plan.source_sha,
        checked_at=str(receipt_values["checked_at"]),
        canary_id_sha256=str(receipt_values["canary_id_sha256"]),
        ticket_id_sha256=str(receipt_values["ticket_id_sha256"]),
        validator_hotkey_sha256=str(receipt_values["validator_hotkey_sha256"]),
        synthetic_corpus_release_sha256=str(
            receipt_values["synthetic_corpus_release_sha256"]
        ),
        synthetic_record_sha256=record_sha256,
        task_commitment_sha256=task_commitment_sha256,
        execution_authority_sha256=execution_authority_sha256,
        private_input_authority_sha256=(readiness.private_input_authority_sha256),
        sealed_evidence_authority_sha256=(readiness.sealed_evidence_authority_sha256),
        probe_receipt_payload_sha256=readiness.probe_receipt_payload_sha256,
        wrapping_key_sha256=readiness.wrapping_key_sha256,
        authoring_transcript_sha256=str(receipt_values["authoring_transcript_sha256"]),
        frozen_submission_sha256=frozen_submission_sha256,
        terminal_evidence_sha256=str(receipt_values["terminal_evidence_sha256"]),
        evidence=evidence_receipts,
        canary_run_sha256=canary_run_sha256,
        synthetic_only=True,
        single_validator=True,
        ready=True,
        worker_active=False,
        weight_eligible=False,
    )
    _validate_receipt(receipt)
    return receipt


def write_hippius_shadow_canary_receipt(
    *,
    receipt: HippiusShadowCanaryReceipt,
    output: Path,
) -> str:
    """Write one new mode-0600 content-addressed redacted canary receipt."""

    _validate_receipt(receipt)
    if not output.is_absolute():
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary receipt path must be absolute"
        )
    payload = _receipt_projection(receipt)
    payload_body = coding_canonical_json_bytes(
        payload,
        maximum_bytes=_MAX_RECEIPT_BYTES,
        label="Hippius shadow canary receipt payload",
    )
    payload_sha256 = hashlib.sha256(payload_body).hexdigest()
    body = coding_canonical_json_bytes(
        {**payload, "receipt_payload_sha256": payload_sha256},
        maximum_bytes=_MAX_RECEIPT_BYTES,
        label="Hippius shadow canary receipt",
    )
    _write_exclusive(output, body)
    return payload_sha256


def load_hippius_shadow_canary_receipt(
    path: Path,
) -> tuple[HippiusShadowCanaryReceipt, str]:
    """Load and revalidate one canonical ready receipt."""

    body = _read_regular_file(path)
    try:
        raw = json.loads(body, object_pairs_hook=_unique_object)
        if not isinstance(raw, dict):
            raise ValueError("receipt root is invalid")
        payload_sha256 = str(raw.pop("receipt_payload_sha256"))
        raw_evidence = raw.pop("evidence")
        if not isinstance(raw_evidence, list):
            raise ValueError("receipt evidence is invalid")
        evidence = tuple(
            HippiusShadowCanaryEvidenceReceipt(**item)
            for item in raw_evidence
            if isinstance(item, dict)
        )
        if len(evidence) != len(raw_evidence):
            raise ValueError("receipt evidence shape is invalid")
        receipt = HippiusShadowCanaryReceipt(evidence=evidence, **raw)
        _validate_receipt(receipt)
        payload = _receipt_projection(receipt)
        payload_body = coding_canonical_json_bytes(
            payload,
            maximum_bytes=_MAX_RECEIPT_BYTES,
            label="Hippius shadow canary receipt payload",
        )
        expected = coding_canonical_json_bytes(
            {**payload, "receipt_payload_sha256": payload_sha256},
            maximum_bytes=_MAX_RECEIPT_BYTES,
            label="Hippius shadow canary receipt",
        )
        if (
            _SHA256.fullmatch(payload_sha256) is None
            or hashlib.sha256(payload_body).hexdigest() != payload_sha256
            or body != expected
        ):
            raise ValueError("receipt digest is invalid")
    except (KeyError, TypeError, ValueError) as error:
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary receipt is invalid"
        ) from error
    return receipt, payload_sha256


async def _retrieve(
    private_input: HippiusShadowCanaryPrivateInput,
    *,
    authority: HippiusPrivateInputTicketAuthority,
    now: datetime,
) -> CodingPrivateCatalogRecord:
    try:
        return await private_input.get_task_material(authority=authority, now=now)
    except Exception as error:
        raise HippiusShadowCanaryUnavailable(
            "Hippius shadow private-input retrieval failed"
        ) from error


async def _seal(
    evidence: HippiusShadowCanaryEvidence,
    *,
    authority: HippiusSealedEvidenceSourceAuthority,
    kind: CodingSealedEvidenceKind,
    plaintext: bytes,
) -> HippiusSealedEvidenceReceipt:
    resolved = replace(authority, evidence_kind=kind)
    try:
        identity_sha256 = await evidence.prepare_and_store(
            authority=resolved,
            plaintext=plaintext,
        )
        receipt = await evidence.publish(identity_sha256)
    except Exception as error:
        raise HippiusShadowCanaryUnavailable(
            "Hippius shadow evidence publication failed"
        ) from error
    if (
        receipt.identity_sha256 != identity_sha256
        or receipt.probe_receipt_payload_sha256
        != evidence.readiness.probe_receipt_payload_sha256
        or receipt.ready is not True
        or receipt.weight_eligible is not False
    ):
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow evidence receipt is inconsistent"
        )
    return receipt


def _validate_plan(
    plan: HippiusShadowCanaryPlan,
    *,
    checked_at: datetime,
    deployed_source_sha: str,
) -> None:
    if (
        not isinstance(plan, HippiusShadowCanaryPlan)
        or not isinstance(plan.canary_id, UUID)
        or not isinstance(plan.private_input, HippiusPrivateInputTicketAuthority)
        or not isinstance(
            plan.sealed_evidence,
            HippiusSealedEvidenceSourceAuthority,
        )
    ):
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary plan is invalid or not single-validator"
        )
    private = plan.private_input
    sealed = plan.sealed_evidence
    if (
        plan.canary_id.int == 0
        or _SOURCE_SHA.fullmatch(plan.source_sha) is None
        or _SOURCE_SHA.fullmatch(deployed_source_sha) is None
        or plan.source_sha != deployed_source_sha
        or not plan.synthetic_corpus_release_id.startswith(
            HIPPIUS_SHADOW_CANARY_CORPUS_PREFIX
        )
        or not _safe_scalar(plan.synthetic_corpus_release_id, maximum_bytes=128)
        or _SHA256.fullmatch(plan.synthetic_record_sha256) is None
        or plan.synthetic_only is not True
        or plan.single_validator is not True
        or plan.weight_eligible is not False
        or private.delivery_phase is not CodingArtifactDeliveryPhase.AUTHORING
        or private.ticket_id != sealed.ticket_id
        or private.validator_hotkey != sealed.validator_hotkey
        or private.ticket_deadline != sealed.ticket_deadline
        or private.weight_eligible is not False
        or sealed.weight_eligible is not False
        or sealed.claim_generation != 1
        or not sealed.instance_id.startswith("hippius-canary-")
        or private.commitment.corpus_release_id != plan.synthetic_corpus_release_id
        or checked_at >= private.ticket_deadline
        or private.ticket_deadline - checked_at > timedelta(hours=2)
    ):
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary plan is invalid or not single-validator"
        )


def _validate_readiness(
    readiness: HippiusEvidenceCustodyReadiness,
) -> HippiusEvidenceCustodyReadiness:
    if (
        readiness.configured is not True
        or readiness.provider != "hippius"
        or _SHA256.fullmatch(readiness.private_input_authority_sha256) is None
        or _SHA256.fullmatch(readiness.sealed_evidence_authority_sha256) is None
        or _SHA256.fullmatch(readiness.probe_receipt_payload_sha256) is None
        or _SHA256.fullmatch(readiness.wrapping_key_sha256) is None
        or readiness.spool_ready is not True
        or readiness.runtime_wired is not True
        or readiness.worker_active is not False
        or readiness.weight_eligible is not False
    ):
        raise HippiusShadowCanaryIntegrity("Hippius shadow canary custody is not ready")
    return readiness


def _validate_synthetic_record(
    plan: HippiusShadowCanaryPlan,
    record: CodingPrivateCatalogRecord,
    *,
    record_sha256: str,
) -> None:
    if (
        record_sha256 != plan.synthetic_record_sha256
        or record.catalog_commitment_sha256
        != plan.private_input.commitment.commitment_sha256
        or record.task_version.payload.corpus_release_id
        != plan.synthetic_corpus_release_id
        or record.task_version.payload.weight_eligible is not False
    ):
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary record is not the committed synthetic input"
        )


def _validate_authoring(
    outcome: HippiusShadowCanaryAuthoringOutcome,
    *,
    material: HippiusShadowCanaryAuthoringMaterial,
) -> None:
    if (
        not isinstance(outcome, HippiusShadowCanaryAuthoringOutcome)
        or outcome.execution_authority_sha256 != material.execution_authority_sha256
        or outcome.task_commitment_sha256 != material.task_commitment_sha256
        or outcome.resolved is not True
        or not _bounded_bytes(outcome.transcript)
        or not _bounded_bytes(outcome.frozen_submission)
    ):
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow authoring outcome is invalid"
        )


def _validate_grading(
    outcome: HippiusShadowCanaryGradingOutcome,
    *,
    material: HippiusShadowCanaryGradingMaterial,
) -> None:
    if (
        not isinstance(outcome, HippiusShadowCanaryGradingOutcome)
        or outcome.execution_authority_sha256 != material.execution_authority_sha256
        or outcome.task_commitment_sha256 != material.task_commitment_sha256
        or outcome.frozen_submission_sha256 != material.frozen_submission_sha256
        or outcome.resolved is not True
        or outcome.pristine is not True
        or not _bounded_bytes(outcome.terminal_evidence)
    ):
        raise HippiusShadowCanaryIntegrity("Hippius shadow grading outcome is invalid")


def _validate_receipt(receipt: HippiusShadowCanaryReceipt) -> None:
    try:
        checked_at = datetime.fromisoformat(receipt.checked_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary receipt time is invalid"
        ) from error
    if (
        receipt.schema != _RECEIPT_SCHEMA
        or not receipt.checked_at.endswith("Z")
        or checked_at.tzinfo is None
        or checked_at.utcoffset() is None
        or _SOURCE_SHA.fullmatch(receipt.source_sha) is None
        or any(
            _SHA256.fullmatch(value) is None
            for value in (
                receipt.canary_id_sha256,
                receipt.ticket_id_sha256,
                receipt.validator_hotkey_sha256,
                receipt.synthetic_corpus_release_sha256,
                receipt.synthetic_record_sha256,
                receipt.task_commitment_sha256,
                receipt.execution_authority_sha256,
                receipt.private_input_authority_sha256,
                receipt.sealed_evidence_authority_sha256,
                receipt.probe_receipt_payload_sha256,
                receipt.wrapping_key_sha256,
                receipt.authoring_transcript_sha256,
                receipt.frozen_submission_sha256,
                receipt.terminal_evidence_sha256,
                receipt.canary_run_sha256,
            )
        )
        or tuple(item.evidence_kind for item in receipt.evidence)
        != (
            CodingSealedEvidenceKind.AUTHORING_TRANSCRIPT.value,
            CodingSealedEvidenceKind.FROZEN_SUBMISSION.value,
            CodingSealedEvidenceKind.TERMINAL_PUBLICATION_REQUEST.value,
        )
        or any(
            _SHA256.fullmatch(value) is None
            for item in receipt.evidence
            for value in (
                item.identity_sha256,
                item.object_key_sha256,
                item.ciphertext_sha256,
                item.envelope_sha256,
            )
        )
        or any(
            not isinstance(item.ciphertext_size_bytes, int)
            or isinstance(item.ciphertext_size_bytes, bool)
            or item.ciphertext_size_bytes < 17
            or not _is_utc_timestamp(item.finalized_at)
            for item in receipt.evidence
        )
        or any(item.status not in {"uploaded", "reused"} for item in receipt.evidence)
        or receipt.synthetic_only is not True
        or receipt.single_validator is not True
        or receipt.ready is not True
        or receipt.worker_active is not False
        or receipt.weight_eligible is not False
        or _canary_run_sha256(receipt) != receipt.canary_run_sha256
    ):
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary receipt is inconsistent"
        )


def _redacted_evidence(
    receipt: HippiusSealedEvidenceReceipt,
    *,
    kind: CodingSealedEvidenceKind,
) -> HippiusShadowCanaryEvidenceReceipt:
    return HippiusShadowCanaryEvidenceReceipt(
        evidence_kind=kind.value,
        identity_sha256=receipt.identity_sha256,
        object_key_sha256=receipt.object_key_sha256,
        ciphertext_sha256=receipt.ciphertext_sha256,
        ciphertext_size_bytes=receipt.ciphertext_size_bytes,
        envelope_sha256=receipt.envelope_sha256,
        status=receipt.status.value,
        finalized_at=receipt.finalized_at,
    )


def _receipt_projection(receipt: HippiusShadowCanaryReceipt) -> dict[str, object]:
    projection = asdict(receipt)
    projection["evidence"] = [asdict(item) for item in receipt.evidence]
    return projection


def _canary_run_sha256(receipt: HippiusShadowCanaryReceipt) -> str:
    projection = _receipt_projection(receipt)
    projection.pop("canary_run_sha256")
    projection["schema"] = _RUN_SCHEMA
    return hashlib.sha256(
        coding_canonical_json_bytes(
            projection,
            maximum_bytes=_MAX_RECEIPT_BYTES,
            label="Hippius shadow canary run",
        )
    ).hexdigest()


def _record_sha256(record: CodingPrivateCatalogRecord) -> str:
    return hashlib.sha256(
        coding_canonical_json_bytes(
            record.model_dump(mode="json", by_alias=True),
            maximum_bytes=_MAX_RECORD_BYTES,
            label="Hippius shadow canary private record",
        )
    ).hexdigest()


def _execution_authority_sha256(plan: HippiusShadowCanaryPlan) -> str:
    private = plan.private_input
    return hashlib.sha256(
        coding_canonical_json_bytes(
            {
                "assignment_sha256": private.assignment_sha256,
                "canary_id_sha256": _scalar_sha256(str(plan.canary_id)),
                "catalog_commitment_sha256": (private.commitment.commitment_sha256),
                "catalog_index": private.catalog_index,
                "publication_receipt_payload_sha256": (
                    private.publication_receipt_payload_sha256
                ),
                "run_manifest_sha256": private.run_manifest_sha256,
                "schema": "dittobench-coding-hippius-shadow-canary-authority-v1",
                "source_sha": plan.source_sha,
                "synthetic_record_sha256": plan.synthetic_record_sha256,
                "ticket_deadline": private.ticket_deadline.isoformat().replace(
                    "+00:00", "Z"
                ),
                "ticket_id_sha256": _scalar_sha256(str(private.ticket_id)),
                "transport_manifest_sha256": private.transport_manifest_sha256,
                "validator_hotkey_sha256": _scalar_sha256(private.validator_hotkey),
                "weight_eligible": False,
            },
            maximum_bytes=_MAX_RECEIPT_BYTES,
            label="Hippius shadow canary execution authority",
        )
    ).hexdigest()


def _scalar_sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _bounded_bytes(value: object) -> bool:
    return isinstance(value, bytes) and 1 <= len(value) <= _MAX_SYNTHETIC_EVIDENCE_BYTES


def _safe_scalar(value: str, *, maximum_bytes: int) -> bool:
    return (
        bool(value)
        and len(value.encode()) <= maximum_bytes
        and all(
            character.isprintable() and not character.isspace() for character in value
        )
    )


def _is_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _utc_now(value: datetime | None) -> datetime:
    resolved = datetime.now(UTC) if value is None else value
    if resolved.tzinfo is None or resolved.utcoffset() is None:
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary clock must be timezone-aware"
        )
    return resolved.astimezone(UTC)


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary receipt is unreadable"
        ) from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or not 1 <= info.st_size <= _MAX_RECEIPT_BYTES
        ):
            raise HippiusShadowCanaryIntegrity(
                "Hippius shadow canary receipt is unsafe"
            )
        chunks: list[bytes] = []
        size = 0
        while size < _MAX_RECEIPT_BYTES + 1:
            chunk = os.read(descriptor, _MAX_RECEIPT_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        body = b"".join(chunks)
    finally:
        os.close(descriptor)
    if not body or len(body) > _MAX_RECEIPT_BYTES:
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary receipt exceeds bounds"
        )
    return body


def _write_exclusive(path: Path, body: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise HippiusShadowCanaryIntegrity(
            "Hippius shadow canary receipt output is unsafe"
        ) from error
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise HippiusShadowCanaryIntegrity(
                "Hippius shadow canary receipt output is invalid"
            )
        view = memoryview(body)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise HippiusShadowCanaryIntegrity(
                    "Hippius shadow canary receipt write made no progress"
                )
            view = view[written:]
        os.fsync(descriptor)
    except Exception:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


__all__ = [
    "HIPPIUS_SHADOW_CANARY_CONFIRMATION",
    "HIPPIUS_SHADOW_CANARY_CORPUS_PREFIX",
    "HippiusShadowCanaryAuthoringExecutor",
    "HippiusShadowCanaryAuthoringMaterial",
    "HippiusShadowCanaryAuthoringOutcome",
    "HippiusShadowCanaryError",
    "HippiusShadowCanaryEvidence",
    "HippiusShadowCanaryEvidenceReceipt",
    "HippiusShadowCanaryGradingExecutor",
    "HippiusShadowCanaryGradingMaterial",
    "HippiusShadowCanaryGradingOutcome",
    "HippiusShadowCanaryIntegrity",
    "HippiusShadowCanaryPlan",
    "HippiusShadowCanaryPrivateInput",
    "HippiusShadowCanaryReceipt",
    "HippiusShadowCanaryUnavailable",
    "load_hippius_shadow_canary_receipt",
    "run_hippius_shadow_canary",
    "write_hippius_shadow_canary_receipt",
]
