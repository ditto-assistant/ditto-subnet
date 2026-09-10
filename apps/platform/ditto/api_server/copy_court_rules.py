"""Pure classification and verdict rules for the copy-hold triage court.

The court classifies a copy-kind ATH hold from the hold's own recorded data and
produces a verdict where the class is mechanically decidable. Reason strings are
only the class hint: every identity claim a mechanical verdict rests on is
re-verified from stored artifact data, and anything the data does not prove
escalates with the evidence bundle instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

HOLD_CLASS_NEAR_DUPLICATE = "near_duplicate"
HOLD_CLASS_BYTE_IDENTICAL = "rejected_resubmission_byte_identical"
HOLD_CLASS_REPACK = "rejected_resubmission_repack"
HOLD_CLASS_CROSS_MINER = "rejected_resubmission_cross_miner"
HOLD_CLASS_UNKNOWN = "unknown"

ALL_HOLD_CLASSES = (
    HOLD_CLASS_NEAR_DUPLICATE,
    HOLD_CLASS_BYTE_IDENTICAL,
    HOLD_CLASS_REPACK,
    HOLD_CLASS_CROSS_MINER,
    HOLD_CLASS_UNKNOWN,
)

_RESUBMISSION_HINTS = (
    "previously rejected as",
    "Resubmission of a rejected artifact",
)
_NEAR_DUPLICATE_HINT = "content near-duplicate of agent"
_SIZE_HINT = "near-duplicate of agent"


@dataclass(frozen=True)
class AncestorIdentity:
    """The matched reference an operator already rejected."""

    agent_id: str
    name: str
    version: int | None
    miner_hotkey: str
    sha256: str
    normalized_source_hash: str | None
    reject_reason: str | None
    resolved_at: str | None


@dataclass(frozen=True)
class CandidateIdentity:
    agent_id: str
    miner_hotkey: str
    sha256: str
    normalized_source_hash: str | None


@dataclass(frozen=True)
class CourtVerdict:
    """One recommendation: verdict plus the miner-visible basis for it."""

    verdict: str  # clear | reject | escalate
    hold_class: str
    reason: str
    citations: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


def classify_hold(
    reason: str | None,
    candidate: CandidateIdentity,
    ancestor: AncestorIdentity | None,
) -> str:
    """Class a hold from its reason hint, confirmed against stored identity.

    Returns ``unknown`` when the reason text matches no known template or the
    stored artifact data contradicts the hint — an unknown class is always a
    bundle-and-escalate row, never a mechanical verdict.
    """
    text = reason or ""
    if not any(hint in text for hint in _RESUBMISSION_HINTS):
        if _NEAR_DUPLICATE_HINT in text:
            return HOLD_CLASS_NEAR_DUPLICATE
        if _SIZE_HINT in text:
            return HOLD_CLASS_NEAR_DUPLICATE
        return HOLD_CLASS_UNKNOWN
    if ancestor is None:
        # A resubmission hint with no loaded ancestor has nothing to verify
        # the identity claim against — the caller escalates it.
        return HOLD_CLASS_UNKNOWN
    same_miner = candidate.miner_hotkey == ancestor.miner_hotkey
    byte_identical = candidate.sha256 == ancestor.sha256
    repack = (
        not byte_identical
        and candidate.normalized_source_hash is not None
        and candidate.normalized_source_hash == ancestor.normalized_source_hash
    )
    if byte_identical:
        return HOLD_CLASS_BYTE_IDENTICAL if same_miner else HOLD_CLASS_CROSS_MINER
    if repack:
        return HOLD_CLASS_REPACK if same_miner else HOLD_CLASS_CROSS_MINER
    return HOLD_CLASS_UNKNOWN


def _ancestor_citation(ancestor: AncestorIdentity) -> dict:
    location = f"{ancestor.name}"
    if ancestor.version is not None:
        location += f" v{ancestor.version}"
    return {
        "kind": "resolved_ath_review",
        "location": location,
        "agent_id": ancestor.agent_id,
        "resolved_at": ancestor.resolved_at,
    }


def byte_identical_verdict(
    candidate: CandidateIdentity,
    ancestor: AncestorIdentity,
) -> CourtVerdict:
    """Mechanical reject for a byte-identical resubmission of a rejected artifact.

    The hold question — "check whether the cited behavior was removed" — is
    false by construction: the upload is the same sha256 as the artifact an
    operator already adjudicated against, so nothing was removed. The verdict
    cites the ancestor's own resolved reject reason.
    """
    return CourtVerdict(
        verdict="reject",
        hold_class=HOLD_CLASS_BYTE_IDENTICAL,
        reason=(
            f"Byte-identical resubmission of a rejected artifact: this upload "
            f"has the same sha256 as {ancestor.name}"
            + (f" v{ancestor.version}" if ancestor.version is not None else "")
            + f" (agent {ancestor.agent_id}), which an operator rejected before "
            "this artifact was uploaded. The hold question — whether the cited "
            "behavior was removed — is false by construction: nothing in the "
            "artifact changed. Cited operator decision: "
            + (ancestor.reject_reason or "reject reason unavailable")
        ),
        citations=[_ancestor_citation(ancestor)],
        evidence={
            "candidate_sha256": candidate.sha256,
            "ancestor_sha256": ancestor.sha256,
            "identity_basis": "exact_sha256",
            "same_miner": candidate.miner_hotkey == ancestor.miner_hotkey,
        },
    )


def repack_verdict(
    candidate: CandidateIdentity,
    ancestor: AncestorIdentity,
) -> CourtVerdict:
    """Mechanical reject for a canonicalized-identical resubmission."""
    return CourtVerdict(
        verdict="reject",
        hold_class=HOLD_CLASS_REPACK,
        reason=(
            f"Resubmission of a rejected artifact: this upload canonicalizes to "
            f"the same source as {ancestor.name}"
            + (f" v{ancestor.version}" if ancestor.version is not None else "")
            + f" (agent {ancestor.agent_id}), which an operator rejected before "
            "this artifact was uploaded. The hold question — whether the cited "
            "behavior was removed — is false by construction: the normalized "
            "source is identical. Cited operator decision: "
            + (ancestor.reject_reason or "reject reason unavailable")
        ),
        citations=[_ancestor_citation(ancestor)],
        evidence={
            "candidate_sha256": candidate.sha256,
            "ancestor_sha256": ancestor.sha256,
            "identity_basis": "normalized_source_hash",
            "same_miner": candidate.miner_hotkey == ancestor.miner_hotkey,
        },
    )


def escalate_verdict(
    hold_class: str,
    reason: str,
    evidence: dict,
    *,
    citations: list[dict] | None = None,
) -> CourtVerdict:
    """Fail-closed verdict: leave the hold for an operator, with evidence."""
    return CourtVerdict(
        verdict="escalate",
        hold_class=hold_class,
        reason=reason,
        citations=citations or [],
        evidence=evidence,
    )
