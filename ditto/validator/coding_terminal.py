"""Deterministic validator-owned aggregation for shadow coding outcomes."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from ditto.api_models.coding import (
    REPAIR_SCORE_RESOLVED_MICROS,
    CodingRunEvidence,
    CodingRunManifest,
    CodingTaskEvidence,
    CodingTaskResult,
    CodingTerminalDomain,
    canonical_digest,
    task_evidence_digest,
    validate_run_evidence_against_manifest,
)


class CodingTerminalEvidenceError(ValueError):
    """Per-task evidence cannot reproduce one manifest-bound run aggregate."""


def build_coding_run_evidence(
    manifest: CodingRunManifest,
    validator_ticket_id: str,
    task_evidence: Sequence[CodingTaskEvidence],
) -> CodingRunEvidence:
    """Build counts, roots, and binary repair mean from authoritative tasks."""

    try:
        manifest = CodingRunManifest.model_validate_json(
            manifest.model_dump_json(by_alias=True)
        )
        if not 1 <= len(task_evidence) <= 100:
            raise ValueError("task evidence cardinality is outside bounds")
        tasks = tuple(
            CodingTaskEvidence.model_validate_json(item.model_dump_json(by_alias=True))
            for item in task_evidence
        )
        if not tasks or len(tasks) != len(manifest.tasks):
            raise ValueError("task evidence cardinality disagrees with manifest")

        by_identity: dict[tuple[str, str], CodingTaskEvidence] = {}
        for item in tasks:
            identity = (item.task.case_id, item.task.variant_id)
            if identity in by_identity:
                raise ValueError("duplicate task evidence identity")
            by_identity[identity] = item

        ordered: list[CodingTaskEvidence] = []
        results: list[CodingTaskResult] = []
        for selected in manifest.tasks:
            identity = (selected.case_id, selected.variant_id)
            matched = by_identity.get(identity)
            if matched is None:
                raise ValueError("selected task evidence is missing")
            digest = task_evidence_digest(manifest, validator_ticket_id, matched)
            ordered.append(matched)
            results.append(
                CodingTaskResult(
                    case_id=selected.case_id,
                    variant_id=selected.variant_id,
                    task_evidence_sha256=digest,
                    terminal_domain=matched.terminal_domain,
                    repair_score_micros=matched.repair_score_micros,
                )
            )

        counts = Counter(item.terminal_domain for item in ordered)
        resolved = counts[CodingTerminalDomain.RESOLVED]
        repair_failure = counts[CodingTerminalDomain.REPAIR_FAILURE]
        candidate_integrity = counts[CodingTerminalDomain.CANDIDATE_INTEGRITY]
        scoreable = resolved + repair_failure + candidate_integrity
        repair_mean = (
            (resolved * REPAIR_SCORE_RESOLVED_MICROS) // scoreable if scoreable else 0
        )
        evidence = CodingRunEvidence(
            schema="dittobench-coding-run-evidence-v1",
            coding_contract_version=1,
            weight_eligible=False,
            coding_run_id=manifest.coding_run_id,
            validator_ticket_id=validator_ticket_id,
            run_manifest_sha256=canonical_digest(manifest),
            task_set_manifest_sha256=manifest.task_set_manifest_sha256,
            tasks=results,
            resolved_count=resolved,
            repair_failure_count=repair_failure,
            infrastructure_count=counts[CodingTerminalDomain.VALIDATOR_INFRASTRUCTURE],
            invalid_count=counts[CodingTerminalDomain.TASK_INVALID],
            candidate_integrity_count=candidate_integrity,
            control_plane_integrity_count=counts[
                CodingTerminalDomain.CONTROL_PLANE_INTEGRITY
            ],
            scoreable_task_count=scoreable,
            repair_mean_micros=repair_mean,
        )
        validate_run_evidence_against_manifest(
            manifest,
            validator_ticket_id,
            evidence,
            ordered,
        )
        return evidence
    except (AttributeError, TypeError, ValueError) as error:
        raise CodingTerminalEvidenceError(
            "coding terminal evidence disagrees with immutable run authority"
        ) from error
