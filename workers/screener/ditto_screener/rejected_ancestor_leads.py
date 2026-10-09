"""Turn source-window matches into location-only L2 attention, never rejection."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from ditto_screener.policy import SourceReviewObservation
from ditto_screening_protocol import SourceReviewEvidenceItem, SourceReviewFinding
from ditto_screening_protocol.rejected_ancestor import (
    RejectedAncestorWindow,
    matching_windows,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ditto_screener.source_review import TarSourceRepository

_CATEGORY = "rejected-ancestor-mechanism"


def ancestor_lead(
    repository: TarSourceRepository,
    *,
    artifact_sha256: str,
    windows: Sequence[RejectedAncestorWindow],
    paths: Sequence[str],
    preflight: SourceReviewObservation | None,
) -> SourceReviewObservation | None:
    if not windows:
        return preflight
    evidence: list[SourceReviewEvidenceItem] = []
    # This is bounded supplemental attention. The ordinary review remains
    # responsible for every served path; a missing match is never a clearance.
    readable_paths = [path for path in paths if len(path) <= 240]
    for path, text in sorted(repository.attention_texts(readable_paths).items()):
        for line, _window in matching_windows(
            text, list(windows), limit=16 - len(evidence)
        ):
            item = SourceReviewEvidenceItem(path=path, line=line, category=_CATEGORY)
            if item not in evidence:
                evidence.append(item)
        if len(evidence) >= 16:
            break
    if not evidence:
        return preflight
    if preflight is None:
        finding = SourceReviewFinding(
            artifact_sha256=artifact_sha256,
            prompt_revision="rejected-ancestor-window-v1",
            risk_level="medium",
            confidence=0,
            categories=[_CATEGORY],
            evidence=evidence,
            summary=(
                "Normalized source matches an earlier rejected artifact. "
                "This is a search lead only; inspect reachable behavior and "
                "remediation before deciding."
            ),
        )
    else:
        # Preserve the static safety finding and its evidence. Do not let an
        # advisory ancestor match replace existing causal proof or certificates.
        if preflight.finding is None:
            return preflight
        finding = SourceReviewFinding.model_validate(preflight.finding)
        if (
            _CATEGORY not in finding.categories
            and len(finding.categories) < 8
            and len(finding.evidence) < 16
        ):
            finding = finding.model_copy(
                update={
                    "categories": [*finding.categories, _CATEGORY],
                    "evidence": [
                        *finding.evidence,
                        *evidence[: 16 - len(finding.evidence)],
                    ],
                }
            )
    base = preflight or SourceReviewObservation(
        ok=True, risk_level="medium", finding_digest=None, categories=()
    )
    return replace(
        base,
        finding=finding.model_dump(mode="json"),
        finding_digest=finding.canonical_digest(),
        categories=tuple(finding.categories),
    )
