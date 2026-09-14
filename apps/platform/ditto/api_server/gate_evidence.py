"""Build, project and key the bench v13 gate evidence on an accepted score.

See :mod:`ditto.api_models.gate_evidence` for the wire contract. This module is
the one place the platform reads the scorer's v13 gate telemetry -- the
per-case ``catalog`` / ``claim_provenance`` / ``inference_cost`` records and
twin markers on ``per_case``, and the four run summaries under ``details`` --
and everything downstream (the public aggregate, the owner-only notes, the
dispute link) consumes the stored projection it builds, so a scorer-side
reshaping has exactly one seam to cross.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError

from ditto.api_models.agent_status import SCOREABLE_AGENT_STATUSES
from ditto.api_models.gate_evidence import (
    GATE_EVIDENCE_CONTRACT_VERSION,
    GATE_EVIDENCE_MIN_BENCH_VERSION,
    GATE_FINDINGS,
    GATE_NOTE_VOCABULARY,
    TWIN_NOTE_MARKERS,
    CatalogGateSummary,
    ClaimProvenanceSummary,
    GatePosture,
    InferenceCostSummary,
    MinerGateNote,
    MinerGateNoteCase,
    MinerGateNotesResponse,
    MinerGateNotesRun,
    PublicGateEvidence,
    StoredGateCase,
    StoredGateEvidence,
    TwinPostPassSummary,
)
from ditto.api_models.validator import CaseScore, ScoreReport

logger = logging.getLogger(__name__)

# Statuses whose owner may file the submission's one dispute against cited
# gate notes: every status an accepted (v13+) score can exist under. A
# rejected submission disputes its quarantine decision instead.
GATE_NOTE_DISPUTE_STATUSES = frozenset(SCOREABLE_AGENT_STATUSES)

# Per-case wire keys the platform used to strip at ingest (pydantic
# ``extra="ignore"``) before it mirrored them. Kept out of the persisted
# breakdown below the v13 floor so a v<=12 row's ``details`` stays
# byte-identical to what it was before this module existed.
_V13_ERA_CASE_KEYS = frozenset(
    {
        "audit_half",
        "undelivered",
        "validator_fault",
        "allow_extra_tools",
        "relation",
        "tool_provenance",
        "catalog",
        "claim_provenance",
        "inference_cost",
    }
)

# ``details`` key -> sanitised summary model, each validated on its own so one
# malformed block cannot erase the others.
_RUN_SUMMARIES: dict[str, type[BaseModel]] = {
    "catalog_gate": CatalogGateSummary,
    "claim_provenance": ClaimProvenanceSummary,
    "twin_post_pass": TwinPostPassSummary,
    "inference_cost": InferenceCostSummary,
}


def carries_gate_evidence(bench_version: int | None) -> bool:
    """Whether ``bench_version`` is in the gate-evidence era (a floor, not a set)."""
    return (
        bench_version is not None and bench_version >= GATE_EVIDENCE_MIN_BENCH_VERSION
    )


def persisted_case_dump(
    case: CaseScore, *, bench_version: int | None
) -> dict[str, Any]:
    """The per-case breakdown as it is persisted in ``scores.details``.

    Below the v13 floor this is exactly the pre-v13 shape: the v10/v13
    report-only keys the model now declares are removed again, because they
    used to be stripped at ingest and a re-scored v12 row must compare equal
    to the one before it. From v13 on the record is kept whole, minus the
    ``None`` values the Go engine omits (``omitempty`` / nil pointers).
    """
    if carries_gate_evidence(bench_version):
        return case.model_dump(mode="json", exclude_none=True)
    dumped = case.model_dump(mode="json")
    for key in _V13_ERA_CASE_KEYS:
        dumped.pop(key, None)
    return dumped


def _known_notes(raw: object) -> list[str]:
    """Keep only tokens byte-identical to the closed vocabulary, in order."""
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for note in raw:
        if isinstance(note, str) and note in GATE_NOTE_VOCABULARY and note not in out:
            out.append(note)
    return out


def _would_zero(notes: list[str]) -> bool:
    """Whether the case's findings zero it when their gate runs in enforce."""
    present = set(notes)
    for note in notes:
        finding = GATE_FINDINGS[note]
        if finding.zeroing and not (finding.waived_by & present):
            return True
    return False


def case_gate_notes(case: CaseScore) -> list[str]:
    """Every vocabulary finding the scorer recorded on one case, in report order.

    Catalog findings first, then claim-span findings, then the twin markers
    (and any other bare vocabulary token) in ``notes``. Free-text notes -- the
    ``v13 ... ; recorded only`` prose, the twin reasons -- never pass.
    """
    notes: list[str] = []
    if case.catalog is not None:
        notes.extend(_known_notes(case.catalog.findings))
    if case.claim_provenance is not None:
        for note in _known_notes(case.claim_provenance.findings):
            if note not in notes:
                notes.append(note)
    for note in _known_notes(case.notes):
        if note not in notes:
            notes.append(note)
    return notes


def _cost_factor(case: CaseScore) -> float | None:
    cost = case.inference_cost
    if cost is None or not cost.attributed:
        return None
    return round(cost.factor_bps / 10_000, 4)


def _run_summaries(details: dict[str, Any]) -> dict[str, BaseModel | None]:
    """Validate each ``details`` gate summary on its own.

    Advisory telemetry must never turn a valid score into a 4xx, and one
    malformed block must not take the others down: a bad ``twin_post_pass``
    drops only ``twin_post_pass``.
    """
    out: dict[str, BaseModel | None] = {}
    for key, model in _RUN_SUMMARIES.items():
        raw = details.get(key)
        if not isinstance(raw, dict):
            out[key] = None
            continue
        try:
            out[key] = model.model_validate(raw)
        except ValidationError as exc:
            logger.warning("dropping malformed details.%s: %s", key, exc)
            out[key] = None
    return out


def overall_posture(postures: list[GatePosture | None]) -> GatePosture | None:
    """Fold per-gate postures: ``enforce`` wins, else ``shadow``, else nothing.

    The twin post-pass calls its shadow ``observe``; the run-level posture
    normalises it so a consumer can ask one question -- did any gate move a
    score -- without knowing each gate's vocabulary.
    """
    present = [p for p in postures if p is not None]
    if not present:
        return None
    if "enforce" in present:
        return "enforce"
    if any(p in ("shadow", "observe") for p in present):
        return "shadow"
    return "off"


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
    summaries = _run_summaries(details)

    gate_counts: Counter[str] = Counter()
    cases: list[StoredGateCase] = []
    for index, case in enumerate(report.per_case):
        notes = case_gate_notes(case)
        gate_counts.update(notes)
        cost_factor = _cost_factor(case)
        discounted = cost_factor is not None and cost_factor < 1.0
        if not (_would_zero(notes) or discounted):
            continue
        cases.append(
            StoredGateCase(
                case_index=index,
                case_id=case.case_id[:200] or None,
                category=case.category[:200] or None,
                kind=case.kind[:32] or None,
                score=case.score,
                notes=notes[:32],
                relation=case.relation or None,
                cost_factor=cost_factor,
                tools_offered=(
                    len(case.catalog.tools_offered)
                    if case.catalog is not None
                    else None
                ),
                catalog_present=(
                    case.catalog.catalog_present if case.catalog is not None else None
                ),
            )
        )

    if not cases and not gate_counts and all(v is None for v in summaries.values()):
        return None

    catalog_gate = summaries["catalog_gate"]
    assert catalog_gate is None or isinstance(catalog_gate, CatalogGateSummary)
    postures: list[GatePosture | None] = [
        getattr(s, "posture", None) for s in summaries.values()
    ]
    if gate_counts and all(p is None for p in postures):
        # Findings without any summary: the scorer ran the gates but posted no
        # run block. Shadow is the only posture that can leave a score intact
        # while findings exist, which is what a bare finding list shows.
        postures.append("shadow")
    total = len(report.per_case)
    evidence = StoredGateEvidence(
        contract_version=GATE_EVIDENCE_CONTRACT_VERSION,
        bench_version=bench_version,
        posture=overall_posture(postures),
        catalog_gate=catalog_gate,
        claim_provenance=summaries["claim_provenance"],  # type: ignore[arg-type]
        twin_post_pass=summaries["twin_post_pass"],  # type: ignore[arg-type]
        inference_cost=summaries["inference_cost"],  # type: ignore[arg-type]
        catalog_suppression_rate=(
            catalog_gate.catalog_suppression_rate if catalog_gate is not None else None
        ),
        gate_counts=dict(sorted(gate_counts.items())),
        flagged_case_count=len(cases),
        flagged_case_share=(round(len(cases) / total, 6) if total else None),
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
        catalog_gate=stored.catalog_gate,
        claim_provenance=stored.claim_provenance,
        twin_post_pass=stored.twin_post_pass,
        inference_cost=stored.inference_cost,
        catalog_suppression_rate=stored.catalog_suppression_rate,
        flagged_case_count=stored.flagged_case_count,
        flagged_case_share=stored.flagged_case_share,
        gate_counts=dict(stored.gate_counts),
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


def _run_notes(score: Any, *, agent_id: UUID) -> MinerGateNotesRun | None:
    stored = stored_gate_evidence(getattr(score, "gate_evidence", None))
    if stored is None:
        return None
    cases: list[MinerGateNoteCase] = []
    for case in stored.cases:
        cases.append(
            MinerGateNoteCase(
                case_index=case.case_index,
                case_id=case.case_id,
                category=case.category,
                kind=case.kind,
                score=case.score,
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
                        zeroing=GATE_FINDINGS[note].zeroing
                        if note in GATE_FINDINGS
                        else False,
                    )
                    for note in case.notes
                ],
                relation=case.relation,
                cost_factor=case.cost_factor,
                tools_offered=case.tools_offered,
                catalog_present=case.catalog_present,
            )
        )
    return MinerGateNotesRun(
        validator_hotkey=score.validator_hotkey,
        run_id=score.run_id,
        bench_version=score.bench_version,
        composite=score.composite,
        generated_at=score.generated_at,
        posture=stored.posture,
        catalog_gate=stored.catalog_gate,
        claim_provenance=stored.claim_provenance,
        twin_post_pass=stored.twin_post_pass,
        inference_cost=stored.inference_cost,
        catalog_suppression_rate=stored.catalog_suppression_rate,
        flagged_case_count=stored.flagged_case_count,
        flagged_case_share=stored.flagged_case_share,
        gate_counts=dict(stored.gate_counts),
        cases=cases,
    )


def owner_gate_notes(
    *,
    agent_id: UUID,
    miner_hotkey: str,
    agent_status: str,
    scores: list[Any],
) -> MinerGateNotesResponse:
    """The owner-only view over every accepted run that carries evidence.

    Case ids are always included: the seed of every accepted score is already
    published on the submission's public pipeline record (provisional scores
    carry it before quorum, in every status), so the seed-derived id reveals
    nothing the owner could not already derive.
    """
    runs = [
        run
        for run in (
            _run_notes(score, agent_id=agent_id)
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


__all__ = [
    "GATE_NOTE_DISPUTE_STATUSES",
    "TWIN_NOTE_MARKERS",
    "build_gate_evidence",
    "carries_gate_evidence",
    "case_gate_notes",
    "gate_note_id",
    "gate_note_ids_for",
    "overall_posture",
    "owner_gate_notes",
    "persisted_case_dump",
    "public_gate_evidence",
    "stored_gate_evidence",
]
