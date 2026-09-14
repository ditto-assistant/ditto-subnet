"""Build, project and key the bench v13 gate evidence on an accepted score.

See :mod:`ditto.api_models.gate_evidence` for the wire contract. This module is
the one place the platform reads the scorer's advisory ``details.gate_evidence``
object and the per-case ``notes`` -- everything downstream (the public
aggregate, the owner-only notes, the dispute link) consumes the stored
projection it builds, so a scorer-side reshaping has exactly one seam to cross.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from ditto.api_models.gate_evidence import (
    GATE_EVIDENCE_CONTRACT_VERSION,
    GATE_EVIDENCE_MIN_BENCH_VERSION,
    GATE_NOTE_VOCABULARY,
    MinerGateNote,
    MinerGateNoteCase,
    MinerGateNotesResponse,
    MinerGateNotesRun,
    PublicGateEvidence,
    ReportedGateCase,
    ReportedGateEvidence,
    StoredGateCase,
    StoredGateEvidence,
)
from ditto.api_models.validator import ScoreReport

logger = logging.getLogger(__name__)

# Statuses whose dataset seed is already published on the public record; only
# then does the owner view carry the seed-derived ``case_id``.
_CASE_ID_PUBLIC_STATUSES = frozenset({"scored", "live"})


def carries_gate_evidence(bench_version: int | None) -> bool:
    """Whether ``bench_version`` is in the gate-evidence era (a floor, not a set)."""
    return (
        bench_version is not None and bench_version >= GATE_EVIDENCE_MIN_BENCH_VERSION
    )


def _gate_notes(raw: object) -> list[str]:
    """Keep only notes byte-identical to the closed vocabulary, in report order."""
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for note in raw:
        if isinstance(note, str) and note in GATE_NOTE_VOCABULARY and note not in seen:
            seen.add(note)
            out.append(note)
    return out


def _reported(details: dict[str, Any]) -> ReportedGateEvidence:
    raw = details.get("gate_evidence")
    if not isinstance(raw, dict):
        return ReportedGateEvidence()
    try:
        return ReportedGateEvidence.model_validate(raw)
    except ValidationError as exc:
        # Advisory telemetry must never turn a valid score into a 4xx. Drop the
        # malformed object -- the per-case notes still project -- and say so.
        logger.warning("dropping malformed details.gate_evidence: %s", exc)
        return ReportedGateEvidence()


def _has_gate_content(case: StoredGateCase) -> bool:
    return bool(case.notes) or any(
        value is not None
        for value in (
            case.relation_outcome,
            case.cost_factor,
            case.tools_offered,
            case.score_with_gates,
        )
    )


def _merge_case(
    stored: StoredGateCase, extra: ReportedGateCase | None
) -> StoredGateCase:
    if extra is None:
        return stored
    notes = list(stored.notes)
    for note in _gate_notes(extra.notes):
        if note not in notes:
            notes.append(note)
    return stored.model_copy(
        update={
            "notes": notes,
            "relation": extra.relation
            if extra.relation is not None
            else stored.relation,
            "relation_outcome": (
                extra.relation_outcome
                if extra.relation_outcome is not None
                else stored.relation_outcome
            ),
            "cost_factor": (
                extra.cost_factor
                if extra.cost_factor is not None
                else stored.cost_factor
            ),
            "tools_offered": (
                extra.tools_offered
                if extra.tools_offered is not None
                else stored.tools_offered
            ),
            "score_with_gates": (
                extra.score_with_gates
                if extra.score_with_gates is not None
                else stored.score_with_gates
            ),
            "score_without_gates": (
                extra.score_without_gates
                if extra.score_without_gates is not None
                else stored.score_without_gates
            ),
        }
    )


def build_gate_evidence(
    report: ScoreReport, *, bench_version: int
) -> dict[str, Any] | None:
    """Project a v13+ report's gate telemetry into the stored shape, or ``None``.

    ``None`` for every report below the floor and for a v13+ report that carries
    no gate telemetry at all (an older scorer), so a row without evidence is
    indistinguishable from one that predates it -- there is nothing to show
    either way. Never raises on malformed advisory input.
    """
    if not carries_gate_evidence(bench_version):
        return None
    details = report.details if isinstance(report.details, dict) else {}
    reported = _reported(details)
    extras: dict[str, ReportedGateCase] = {c.case_id: c for c in reported.cases}

    cases: list[StoredGateCase] = []
    for index, case in enumerate(report.per_case):
        stored = StoredGateCase(
            case_index=index,
            case_id=case.case_id[:200] or None,
            category=case.category[:200] or None,
            kind=case.kind[:32] or None,
            score=case.score,
            notes=_gate_notes(case.notes),
        )
        stored = _merge_case(stored, extras.pop(case.case_id, None))
        if _has_gate_content(stored):
            cases.append(stored)
    # Gate outcomes the scorer reported for cases the breakdown did not carry
    # (a daemon that posts the aggregate only) still count.
    for extra in extras.values():
        stored = _merge_case(StoredGateCase(case_id=extra.case_id), extra)
        if _has_gate_content(stored):
            cases.append(stored)

    run_level = (
        reported.posture,
        reported.composite_with_gates,
        reported.composite_without_gates,
        reported.catalog_suppression_rate,
    )
    if not cases and all(value is None for value in run_level):
        return None

    gate_counts = Counter(note for case in cases for note in case.notes)
    outcome_counts = Counter(
        case.relation_outcome for case in cases if case.relation_outcome is not None
    )
    loss: float | None = None
    if (
        reported.composite_with_gates is not None
        and reported.composite_without_gates is not None
    ):
        loss = max(
            0.0, reported.composite_without_gates - reported.composite_with_gates
        )
    evidence = StoredGateEvidence(
        contract_version=GATE_EVIDENCE_CONTRACT_VERSION,
        bench_version=bench_version,
        posture=reported.posture,
        composite_with_gates=reported.composite_with_gates,
        composite_without_gates=reported.composite_without_gates,
        gate_induced_loss=loss,
        catalog_suppression_rate=reported.catalog_suppression_rate,
        gate_counts=dict(sorted(gate_counts.items())),
        relation_outcome_counts=dict(sorted(outcome_counts.items())),
        flagged_case_count=sum(
            1 for case in cases if case.notes or case.relation_outcome is not None
        ),
        cases=cases,
    )
    return evidence.model_dump(mode="json")


def stored_gate_evidence(raw: object) -> StoredGateEvidence | None:
    """Parse a ``scores.gate_evidence`` cell; ``None`` when absent or malformed.

    A projection bug must never take a public read down, so a cell that fails
    validation reads as "no evidence" rather than raising.
    """
    if not isinstance(raw, dict):
        return None
    try:
        return StoredGateEvidence.model_validate(raw)
    except ValidationError:
        logger.warning("ignoring malformed stored gate_evidence")
        return None


def public_gate_evidence(raw: object) -> PublicGateEvidence | None:
    """Project a stored cell onto the public aggregate (never per-case)."""
    stored = stored_gate_evidence(raw)
    if stored is None:
        return None
    return PublicGateEvidence(
        bench_version=stored.bench_version,
        posture=stored.posture,
        composite_with_gates=stored.composite_with_gates,
        composite_without_gates=stored.composite_without_gates,
        gate_induced_loss=stored.gate_induced_loss,
        catalog_suppression_rate=stored.catalog_suppression_rate,
        flagged_case_count=stored.flagged_case_count,
        gate_counts=dict(stored.gate_counts),
        relation_outcome_counts=dict(stored.relation_outcome_counts),
    )


def gate_note_id(
    *,
    agent_id: UUID,
    bench_version: int,
    validator_hotkey: str,
    run_id: str,
    case_index: int | None,
    case_id: str | None,
    gate: str,
) -> str:
    """The stable id a dispute cites for one gate note on one case.

    Derived, not stored: it is a function of the score's identity and the
    note, so it survives a re-projection and can be re-derived to check that a
    cited id belongs to the submission it is filed against.
    """
    key = (
        f"{agent_id}:{bench_version}:{validator_hotkey}:{run_id}:"
        f"{case_index if case_index is not None else '-'}:{case_id or '-'}:{gate}"
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _run_notes(
    score: Any,
    *,
    agent_id: UUID,
    include_case_ids: bool,
) -> MinerGateNotesRun | None:
    stored = stored_gate_evidence(getattr(score, "gate_evidence", None))
    if stored is None:
        return None
    cases: list[MinerGateNoteCase] = []
    for case in stored.cases:
        cases.append(
            MinerGateNoteCase(
                case_index=case.case_index,
                case_id=case.case_id if include_case_ids else None,
                category=case.category,
                kind=case.kind,
                score=case.score,
                score_with_gates=case.score_with_gates,
                score_without_gates=case.score_without_gates,
                notes=[
                    MinerGateNote(
                        note_id=gate_note_id(
                            agent_id=agent_id,
                            bench_version=score.bench_version,
                            validator_hotkey=score.validator_hotkey,
                            run_id=score.run_id,
                            case_index=case.case_index,
                            case_id=case.case_id,
                            gate=note,
                        ),
                        gate=note,
                    )
                    for note in case.notes
                ],
                relation=case.relation,
                relation_outcome=case.relation_outcome,
                cost_factor=case.cost_factor,
                tools_offered=case.tools_offered,
            )
        )
    return MinerGateNotesRun(
        validator_hotkey=score.validator_hotkey,
        run_id=score.run_id,
        bench_version=score.bench_version,
        composite=score.composite,
        generated_at=score.generated_at,
        posture=stored.posture,
        composite_with_gates=stored.composite_with_gates,
        composite_without_gates=stored.composite_without_gates,
        gate_induced_loss=stored.gate_induced_loss,
        catalog_suppression_rate=stored.catalog_suppression_rate,
        flagged_case_count=stored.flagged_case_count,
        gate_counts=dict(stored.gate_counts),
        relation_outcome_counts=dict(stored.relation_outcome_counts),
        cases=cases,
    )


def owner_gate_notes(
    *,
    agent_id: UUID,
    miner_hotkey: str,
    agent_status: str,
    scores: list[Any],
) -> MinerGateNotesResponse:
    """The owner-only view over every accepted run that carries evidence."""
    include_case_ids = agent_status in _CASE_ID_PUBLIC_STATUSES
    runs = [
        run
        for run in (
            _run_notes(score, agent_id=agent_id, include_case_ids=include_case_ids)
            for score in sorted(
                scores, key=lambda s: (s.bench_version, s.validator_hotkey)
            )
        )
        if run is not None
    ]
    return MinerGateNotesResponse(
        agent_id=agent_id,
        miner_hotkey=miner_hotkey,
        agent_status=agent_status,
        runs=runs,
    )


def gate_note_ids_for(*, agent_id: UUID, scores: list[Any]) -> frozenset[str]:
    """Every note id that exists on ``agent_id``'s accepted scores."""
    ids: set[str] = set()
    for score in scores:
        stored = stored_gate_evidence(getattr(score, "gate_evidence", None))
        if stored is None:
            continue
        for case in stored.cases:
            for note in case.notes:
                ids.add(
                    gate_note_id(
                        agent_id=agent_id,
                        bench_version=score.bench_version,
                        validator_hotkey=score.validator_hotkey,
                        run_id=score.run_id,
                        case_index=case.case_index,
                        case_id=case.case_id,
                        gate=note,
                    )
                )
    return frozenset(ids)
