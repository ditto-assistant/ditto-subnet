"""Read-only aggregate adapter for one anti-copy candidate/reference pair.

The durable ATH review API calls :func:`compare_anti_copy_pair` after loading two
ledger rows. The adapter delegates the decision to the score-path gate and emits
only bounded aggregate evidence and public algorithm provenance. It never returns
hashes, sketches, source, paths, artifact locations, or credentials, and it has no
database or storage dependency.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ditto.api_server.fingerprint import (
    _FP_VERSION,
    _NSH_VERSION,
    _PROMPT_VERSION,
    content_similarity,
    reference_corpus_provenance,
)
from ditto.api_server.scoring_gate import (
    _DEFAULT_CONTAINMENT_TOL,
    _DEFAULT_JACCARD_TOL,
    _DEFAULT_STRUCTURAL_CONTAINMENT_TOL,
    _DEFAULT_STRUCTURAL_JACCARD_TOL,
    _PROMPT_ADVISORY_TOL,
    _fingerprint_versions_incompatible,
    _utc,
    evaluate_duplicate_signals,
)
from ditto.db.queries.scores import LedgerRow

ANTI_COPY_ALGORITHM_VERSION = "reference-aware-v2"


@dataclass(frozen=True)
class SimilarityEvidence:
    """Wire-safe aggregate evidence for one fingerprint channel."""

    candidate_version: int | str | None
    reference_version: int | str | None
    compatible: bool
    applicable: bool
    candidate_cardinality: int | None
    reference_cardinality: int | None
    jaccard: float | None
    containment: float | None
    above_threshold: bool
    decision_role: str


@dataclass(frozen=True)
class AntiCopyComparison:
    """Wire-ready, aggregate-only result of a pure pair comparison."""

    availability: str
    bulk_eligible: bool
    algorithm_version: str
    lexical_fingerprint_version: int
    normalized_source_fingerprint_version: str
    prompt_fingerprint_version: str
    canonical_reference_revision: str
    reference_corpus_id: str
    reference_exclusion_mode: str
    miner_exclusion_mode: str
    same_miner_excluded: bool
    chronology_direction: str
    chronology_eligible: bool
    exact_byte_match: bool
    normalized_source_match: bool
    lexical: SimilarityEvidence
    structural: SimilarityEvidence
    prompt: SimilarityEvidence
    triggered: bool
    triggered_signal: str | None
    current_decision: str

    def to_wire(self) -> dict[str, Any]:
        """Return JSON-ready primitives without any submitted fingerprint data."""
        return asdict(self)


def _cardinality(fingerprint: dict | None) -> int | None:
    if not fingerprint or "card" not in fingerprint:
        return None
    try:
        return max(0, min(int(fingerprint["card"]), 500_000))
    except (TypeError, ValueError):
        return None


def _similarity(
    candidate: dict | None,
    reference: dict | None,
    *,
    jaccard_threshold: float,
    containment_threshold: float,
    decision_role: str,
) -> SimilarityEvidence:
    compatible = bool(
        candidate
        and reference
        and not _fingerprint_versions_incompatible(candidate, reference)
    )
    applicable = bool(
        compatible
        and candidate is not None
        and reference is not None
        and candidate.get("m")
        and reference.get("m")
    )
    jaccard, containment = content_similarity(candidate, reference)
    return SimilarityEvidence(
        candidate_version=candidate.get("v") if candidate else None,
        reference_version=reference.get("v") if reference else None,
        compatible=compatible,
        applicable=applicable,
        candidate_cardinality=_cardinality(candidate),
        reference_cardinality=_cardinality(reference),
        jaccard=round(max(0.0, min(jaccard, 1.0)), 6) if applicable else None,
        containment=(round(max(0.0, min(containment, 1.0)), 6) if applicable else None),
        above_threshold=bool(
            applicable
            and (jaccard >= jaccard_threshold or containment >= containment_threshold)
        ),
        decision_role=decision_role,
    )


def _chronology(candidate: LedgerRow, reference: LedgerRow) -> tuple[str, bool]:
    candidate_time = _utc(candidate.first_seen)
    reference_time = _utc(reference.first_seen)
    if reference_time < candidate_time:
        return ("reference_earlier", True)
    if reference_time > candidate_time:
        return ("candidate_earlier", False)
    if reference.agent_id.int < candidate.agent_id.int:
        return ("equal_time_reference_uuid_first", True)
    if reference.agent_id.int > candidate.agent_id.int:
        return ("equal_time_candidate_uuid_first", False)
    return ("same_submission", False)


def _decision_fields(held: bool, reason: str | None) -> tuple[str | None, str]:
    if not held:
        return (None, "clear")
    if reason and reason.startswith("anti-copy comparison inconclusive"):
        return ("incompatible_fingerprint", "inconclusive_review")
    if reason and reason.startswith("exact sha256"):
        return ("exact_byte", "hold")
    if reason and reason.startswith("normalized-source"):
        return ("normalized_source", "hold")
    if reason and reason.startswith("content near-duplicate"):
        return ("lexical", "hold")
    if reason and reason.startswith("near-duplicate"):
        return ("size_fallback", "hold")
    return ("other", "hold")


def compare_anti_copy_pair(
    *, candidate: LedgerRow, reference: LedgerRow
) -> AntiCopyComparison:
    """Recompute the authoritative aggregate decision for one candidate pair.

    This function is pure and read-only. The reference row is passed as the sole
    eligible row to :func:`evaluate_duplicate_signals`, so decision precedence,
    chronology, same-miner exclusion, thresholds, and fallback behavior remain
    identical to the score-write path.
    """
    direction, chronology_eligible = _chronology(candidate, reference)
    same_miner = candidate.miner_hotkey == reference.miner_hotkey or bool(
        candidate.miner_coldkey is not None
        and candidate.miner_coldkey == reference.miner_coldkey
    )
    decision = evaluate_duplicate_signals(
        agent_id=candidate.agent_id,
        miner_hotkey=candidate.miner_hotkey,
        miner_coldkey=candidate.miner_coldkey,
        submitted_at=candidate.first_seen,
        sha256=candidate.sha256,
        composite=candidate.composite,
        size_bytes=candidate.size_bytes,
        eligible=(reference,),
        normalized_source_hash=candidate.normalized_source_hash,
        content_fingerprint=candidate.content_fingerprint,
        structural_fingerprint=candidate.structural_fingerprint,
        prompt_fingerprint=candidate.prompt_fingerprint,
    )
    signal, current_decision = _decision_fields(decision.held, decision.reason)
    if same_miner:
        current_decision = "excluded"
        signal = "same_miner"

    provenance = reference_corpus_provenance()
    lexical = _similarity(
        candidate.content_fingerprint,
        reference.content_fingerprint,
        jaccard_threshold=_DEFAULT_JACCARD_TOL,
        containment_threshold=_DEFAULT_CONTAINMENT_TOL,
        decision_role="trigger",
    )
    structural = _similarity(
        candidate.structural_fingerprint,
        reference.structural_fingerprint,
        jaccard_threshold=_DEFAULT_STRUCTURAL_JACCARD_TOL,
        containment_threshold=_DEFAULT_STRUCTURAL_CONTAINMENT_TOL,
        decision_role="advisory",
    )
    prompt = _similarity(
        candidate.prompt_fingerprint,
        reference.prompt_fingerprint,
        jaccard_threshold=_PROMPT_ADVISORY_TOL,
        containment_threshold=_PROMPT_ADVISORY_TOL,
        decision_role="advisory",
    )
    canonical_lexical_pair = bool(
        lexical.compatible
        and candidate.content_fingerprint
        and reference.content_fingerprint
        and candidate.content_fingerprint.get("v") == _FP_VERSION
        and reference.content_fingerprint.get("v") == _FP_VERSION
        and candidate.content_fingerprint.get("corpus") == provenance["corpus_id"]
        and reference.content_fingerprint.get("corpus") == provenance["corpus_id"]
        and "m" in candidate.content_fingerprint
        and "m" in reference.content_fingerprint
        and lexical.candidate_cardinality is not None
        and lexical.reference_cardinality is not None
    )
    return AntiCopyComparison(
        availability="available",
        bulk_eligible=bool(
            current_decision == "clear"
            and chronology_eligible
            and not same_miner
            and canonical_lexical_pair
        ),
        algorithm_version=ANTI_COPY_ALGORITHM_VERSION,
        lexical_fingerprint_version=_FP_VERSION,
        normalized_source_fingerprint_version=f"nsh{_NSH_VERSION}",
        prompt_fingerprint_version=_PROMPT_VERSION,
        canonical_reference_revision=provenance["revision"],
        reference_corpus_id=provenance["corpus_id"],
        reference_exclusion_mode=provenance["exclusion_mode"],
        miner_exclusion_mode="same-payment-coldkey-excluded-hotkey-fallback",
        same_miner_excluded=same_miner,
        chronology_direction=direction,
        chronology_eligible=chronology_eligible and not same_miner,
        exact_byte_match=(not same_miner and candidate.sha256 == reference.sha256),
        normalized_source_match=bool(
            not same_miner
            and chronology_eligible
            and candidate.normalized_source_hash is not None
            and candidate.normalized_source_hash == reference.normalized_source_hash
        ),
        lexical=lexical,
        structural=structural,
        prompt=prompt,
        triggered=decision.held,
        triggered_signal=signal,
        current_decision=current_decision,
    )
