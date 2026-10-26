"""Render a lineage-scoped review packet into L1/L2 model context.

The packet arrives only when Platform names a same-hotkey prior version of the
submission. It scopes where the review spends its budget; it never decides the
verdict. Prior rulings and findings are history, not evidence about this
artifact: every concern or clearance must still cite this archive's lines.
"""

from __future__ import annotations

import json

from ditto_screening_protocol import ReviewLineagePacket, ReviewLineageScope

LINEAGE_REVIEW_INSTRUCTIONS = (
    "This submission is a new version of an agent this subnet has already "
    "reviewed. review_lineage carries the source diff from the prior version, "
    "every prior operator ruling on the agent's versions, and the prior "
    "version's automated findings. Use it this way: (1) Read every changed "
    "file in full and trace each change on the served decision path. (2) For "
    "each prior reject or concern, decide from this archive whether the cited "
    "pattern is still reachable, removed, or moved; cite the current lines. "
    "(3) For each prior clear or release, check whether the diff touches the "
    "path it cleared; if it does, re-review that path. (4) Unchanged files may "
    "rely on the history only where a ruling under this policy settled them; "
    "read them when a changed file now calls into them. You may read any file. "
    "History is context, never a verdict: a cleared parent does not clear this "
    "artifact, and a rejected parent does not reject it. "
)
FULL_SCOPE_INSTRUCTIONS = (
    "Scope is full: the reasons in full_review_reasons mean the prior review "
    "cannot carry, so review the whole served path as for a new submission, "
    "using the history only to prioritize leads."
)
DIFF_SCOPE_INSTRUCTIONS = (
    "Scope is diff: focus the budget on the changed files, their callers on "
    "the decision path, and the open leads in the history."
)


def lineage_dossier(packet: ReviewLineagePacket) -> dict[str, object]:
    """Bounded dossier section for the L2/L3 trajectories."""
    return {
        "instructions": LINEAGE_REVIEW_INSTRUCTIONS
        + (
            FULL_SCOPE_INSTRUCTIONS
            if packet.scope == ReviewLineageScope.FULL
            else DIFF_SCOPE_INSTRUCTIONS
        ),
        "packet_sha256": packet.packet_sha256,
        "scope": packet.scope.value,
        "full_review_reasons": [reason.value for reason in packet.full_review_reasons],
        "parent": packet.parent.model_dump(mode="json"),
        "history": [row.model_dump(mode="json") for row in packet.history],
        "changed_file_count": packet.changed_file_count,
        "unchanged_file_count": packet.unchanged_file_count,
        "changed_line_count": packet.changed_line_count,
        "changes": [change.model_dump(mode="json") for change in packet.changes],
        "rulings": [ruling.model_dump(mode="json") for ruling in packet.rulings],
        "prior_findings": [
            finding.model_dump(mode="json") for finding in packet.findings
        ],
    }


def lineage_l1_text(packet: ReviewLineagePacket) -> str:
    """The same context as one appended block of the L1 opening message."""
    return "\nReview lineage (prior versions, rulings and diff):\n" + json.dumps(
        {"review_lineage": lineage_dossier(packet)},
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "LINEAGE_REVIEW_INSTRUCTIONS",
    "lineage_dossier",
    "lineage_l1_text",
]
