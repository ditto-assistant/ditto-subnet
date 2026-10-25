"""Build the lineage-scoped review packet for one screening attempt.

A resubmission's lineage parent is the same hotkey's prior version of the same
agent name (highest lower ``version``), chosen when the attempt is claimed. The
packet the worker fetches carries three things, all bounded:

* the source diff from the parent (per-file unified diffs);
* every prior ruling on the agent's earlier versions (quarantine resolutions,
  ATH actions with their batch evidence references, resolved disputes);
* the parent's automated review outcomes (finding summary, cited locations and
  the review notes ledger).

``scope`` is ``diff`` unless a change cannot inherit the parent's review: the
Dockerfile, build manifest or entrypoint changed, the diff is too large, an
opaque or unread file changed, the parent was reviewed under another policy, or
the lineage has no review history at all. The model review runs either way;
the scope only tells it where to spend its budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ditto.api_server.source_diff import (
    build_source_diff_manifest,
    unified_diff_for_file,
)
from ditto.api_server.source_inspect import (
    MAX_TARBALL_BYTES,
    SourceInspectError,
    TarSourceInspector,
)
from ditto.api_server.storage import ObjectDownloadFailedError, S3StorageClient
from ditto.db.models import (
    Agent,
    AthReview,
    AthReviewAction,
    ScreeningAttempt,
    ScreeningDispute,
    ScreeningQuarantine,
    ScreeningQuarantineResolution,
    ScreeningReviewEvent,
)
from ditto_screening_protocol import (
    ReviewLineageFullReviewReason,
    ReviewLineagePacket,
    ReviewLineageScope,
    seal_review_lineage_packet,
)
from ditto_screening_protocol.review_lineage import (
    MAX_LINEAGE_CHANGES,
    MAX_LINEAGE_DIFF_LINES,
    MAX_LINEAGE_FINDINGS,
    MAX_LINEAGE_HISTORY,
    MAX_LINEAGE_RULINGS,
    MAX_LINEAGE_TEXT,
)

# Above this many changed lines the parent's review no longer scopes much.
MAX_SCOPED_CHANGED_LINES = 2500
# Per-file cap inside the packet's shared ``MAX_LINEAGE_DIFF_LINES`` budget.
MAX_FILE_DIFF_LINES = 1500
MAX_NOTES_PER_FINDING = 16

# Files whose change rewrites what is built or served, so the decision path
# itself may have moved. Lockfiles stay out: a dependency bump is in the diff
# for the reviewer, but it does not by itself invalidate the parent's trace.
_BUILD_SURFACE_NAMES = frozenset(
    {
        "dockerfile",
        "containerfile",
        ".dockerignore",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "cargo.toml",
        "build.rs",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "go.mod",
        "makefile",
        "procfile",
    }
)


class ReviewLineageUnavailableError(Exception):
    """The parent or candidate artifact cannot be read for a lineage diff."""


def is_build_surface(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return (
        name in _BUILD_SURFACE_NAMES
        or name.startswith(("dockerfile", "entrypoint"))
        or name.endswith(".dockerfile")
        or (name.startswith("requirements") and name.endswith(".txt"))
    )


def is_dockerfile(path: str) -> bool:
    name = PurePosixPath(path).name.lower()
    return name.startswith(("dockerfile", "containerfile")) or name.endswith(
        ".dockerfile"
    )


# Dockerfile instructions that only set metadata or the served process's
# environment. A miner that flips feature switches through ``ENV`` on every
# upload still gets a diff-scoped review: the switch and the code that reads
# it are both in the diff, and the reviewer reads the changed Dockerfile in
# full. ``ARG`` stays build surface because ``FROM ${base}`` reads it.
_DOCKERFILE_RUNTIME_INSTRUCTIONS = frozenset({"env", "label"})
# ``ENV`` names that steer the build steps themselves (package indexes,
# compilers, proxies, lookup paths); changing one can change what ``RUN``
# installs even when no ``RUN`` line moved.
_BUILD_ENV_PREFIXES = (
    "pip_",
    "uv_",
    "python",
    "npm_",
    "node_",
    "yarn_",
    "pnpm_",
    "cargo_",
    "rust",
    "go",
    "cc",
    "cxx",
    "cflags",
    "cppflags",
    "ldflags",
    "ld_",
    "path",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    "debian_",
    "apt_",
    "pkg_",
    "make",
)


def _dockerfile_instructions(text: str) -> list[str]:
    """Logical instructions: comments and blank lines dropped, continuation
    lines folded, surrounding whitespace collapsed."""
    out: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        continued = line.endswith("\\")
        if continued:
            line = line[:-1].rstrip()
        pending = f"{pending} {line}".strip() if pending else line
        if not continued:
            out.append(" ".join(pending.split()))
            pending = ""
    if pending:
        out.append(" ".join(pending.split()))
    return out


def _env_names(instruction: str) -> list[str]:
    """Variable names an ``ENV`` instruction sets, in both ``K=V`` and the
    legacy ``K V`` form."""
    body = instruction.split(None, 1)[1] if " " in instruction else ""
    tokens = body.split()
    if not tokens:
        return []
    if "=" not in tokens[0]:
        return [tokens[0]]
    return [token.split("=", 1)[0] for token in tokens if "=" in token]


def _steers_build(instruction: str) -> bool:
    return any(
        name.lower().startswith(_BUILD_ENV_PREFIXES) for name in _env_names(instruction)
    )


def dockerfile_build_surface_changed(
    reference: str | None, candidate: str | None
) -> bool:
    """Whether a Dockerfile change moves the build surface.

    ``False`` only when every changed logical instruction is ``ENV`` or
    ``LABEL`` and none of the changed ``ENV`` names steers a build tool. A
    Dockerfile that was added, removed, or cannot be read as text is a build
    surface change.
    """
    if reference is None or candidate is None:
        return True
    before = _dockerfile_instructions(reference)
    after = _dockerfile_instructions(candidate)

    def built(instructions: list[str]) -> list[str]:
        return [
            line
            for line in instructions
            if line.split(None, 1)[0].lower() not in _DOCKERFILE_RUNTIME_INSTRUCTIONS
        ]

    if built(before) != built(after):
        return True
    # A changed ENV that a RUN step reads ("$NAME", "${NAME}") can change what
    # the image builds even though no RUN line moved.
    run_text = " ".join(
        line for line in (*before, *after) if line.split(None, 1)[0].lower() == "run"
    )

    def read_by_run(name: str) -> bool:
        return f"${name}" in run_text or "${" + name in run_text

    changed = set(before).symmetric_difference(after)
    return any(
        line.split(None, 1)[0].lower() == "env"
        and (_steers_build(line) or any(read_by_run(n) for n in _env_names(line)))
        for line in changed
    )


def _clip(value: object, limit: int = MAX_LINEAGE_TEXT) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class _ArtifactRef:
    """What opening an artifact needs, detached from the ORM row."""

    agent_id: UUID
    sha256: str


async def _open(agent: _ArtifactRef, storage: S3StorageClient) -> TarSourceInspector:
    try:
        tar_bytes = await storage.get_object(
            key=f"{agent.agent_id}/agent.tar.gz", max_bytes=MAX_TARBALL_BYTES
        )
    except ObjectDownloadFailedError as error:
        raise ReviewLineageUnavailableError("artifact is unavailable") from error

    def _verify() -> TarSourceInspector:
        if hashlib.sha256(tar_bytes).hexdigest() != agent.sha256:
            raise ReviewLineageUnavailableError("artifact does not match its digest")
        return TarSourceInspector(tar_bytes)

    try:
        return await asyncio.to_thread(_verify)
    except SourceInspectError as error:
        raise ReviewLineageUnavailableError(str(error)) from error


@dataclass(frozen=True)
class _DiffResult:
    changes: list[dict[str, Any]]
    changed_file_count: int
    unchanged_file_count: int
    changed_line_count: int
    reasons: set[ReviewLineageFullReviewReason]


def _build_surface_moved(
    path: str,
    from_path: str | None,
    candidate_texts: Mapping[str, str],
    reference_texts: Mapping[str, str],
) -> bool:
    """A changed build-surface file forces a full review, except a Dockerfile
    kept in place whose only changed instructions are runtime ``ENV``/``LABEL``
    lines (see ``dockerfile_build_surface_changed``)."""
    if not any(is_build_surface(p) for p in (path, from_path) if p):
        return False
    if from_path or not is_dockerfile(path):
        return True
    return dockerfile_build_surface_changed(
        reference_texts.get(path), candidate_texts.get(path)
    )


def _diff(candidate: TarSourceInspector, reference: TarSourceInspector) -> _DiffResult:
    """Text diff plus digest comparison for the members text diffing skipped."""
    cand_snapshot = candidate.read_text_snapshot()
    ref_snapshot = reference.read_text_snapshot()
    omitted = {*cand_snapshot.omitted_paths, *ref_snapshot.omitted_paths}
    manifest = build_source_diff_manifest(
        cand_snapshot.texts,
        ref_snapshot.texts,
        omitted=omitted,
        max_files=10_000,
    )
    reasons: set[ReviewLineageFullReviewReason] = set()
    cand_digests = candidate.member_digests()
    ref_digests = reference.member_digests()
    opaque = candidate.opaque_paths() | reference.opaque_paths()
    compared = set(cand_snapshot.texts) | set(ref_snapshot.texts)
    for path in sorted(set(cand_digests) | set(ref_digests)):
        if path in compared or cand_digests.get(path) == ref_digests.get(path):
            continue
        reasons.add(
            ReviewLineageFullReviewReason.OPAQUE_FILE_CHANGED
            if path in opaque
            else ReviewLineageFullReviewReason.SOURCE_NOT_COMPARED
        )
        if is_build_surface(path):
            reasons.add(ReviewLineageFullReviewReason.BUILD_SURFACE_CHANGED)

    changed = [entry for entry in manifest["files"] if entry["status"] != "identical"]
    changed_lines = sum(
        int(entry["added_lines"]) + int(entry["removed_lines"]) for entry in changed
    )
    if changed_lines > MAX_SCOPED_CHANGED_LINES:
        reasons.add(ReviewLineageFullReviewReason.DIFF_TOO_LARGE)
    if len(changed) > MAX_LINEAGE_CHANGES:
        reasons.add(ReviewLineageFullReviewReason.TOO_MANY_FILES_CHANGED)

    changes: list[dict[str, Any]] = []
    budget = MAX_LINEAGE_DIFF_LINES
    # Smallest first: the packet keeps the most whole files; a cut file is
    # marked ``truncated`` and the reviewer reads it from the archive.
    for entry in sorted(
        changed, key=lambda item: int(item["added_lines"]) + int(item["removed_lines"])
    )[:MAX_LINEAGE_CHANGES]:
        path = str(entry.get("to_path") or entry["path"])
        from_path = entry.get("from_path")
        if _build_surface_moved(
            path, from_path, cand_snapshot.texts, ref_snapshot.texts
        ):
            reasons.add(ReviewLineageFullReviewReason.BUILD_SURFACE_CHANGED)
        limit = min(MAX_FILE_DIFF_LINES, budget)
        detail: dict[str, Any] = (
            unified_diff_for_file(
                path, cand_snapshot.texts, ref_snapshot.texts, max_lines=limit
            )
            if limit > 0
            else {"diff_lines": [], "truncated": True}
        )
        lines = list(detail["diff_lines"])
        budget -= len(lines)
        changes.append(
            {
                "path": path,
                "change": entry["status"],
                "from_path": from_path,
                "added_lines": int(entry["added_lines"]),
                "removed_lines": int(entry["removed_lines"]),
                "diff_lines": lines,
                "truncated": bool(detail["truncated"]),
            }
        )
    return _DiffResult(
        changes=sorted(changes, key=lambda item: item["path"]),
        changed_file_count=len(changed),
        unchanged_file_count=int(manifest.get("identical_count", 0)),
        changed_line_count=changed_lines,
        reasons=reasons,
    )


def _history_row(agent: Agent) -> dict[str, Any]:
    return {
        "agent_id": agent.agent_id,
        "version": agent.version,
        "artifact_sha256": agent.sha256,
        "status": str(getattr(agent.status, "value", agent.status)),
        "screening_policy_version": agent.screening_policy_version or None,
        "submitted_at": agent.created_at,
    }


async def _rulings(
    session: AsyncSession, versions: dict[Any, int]
) -> list[dict[str, Any]]:
    ids = list(versions)
    rows: list[dict[str, Any]] = []
    for resolution, quarantine in (
        await session.execute(
            select(ScreeningQuarantineResolution, ScreeningQuarantine)
            .join(
                ScreeningQuarantine,
                ScreeningQuarantine.quarantine_id
                == ScreeningQuarantineResolution.quarantine_id,
            )
            .where(ScreeningQuarantine.agent_id.in_(ids))
            .order_by(ScreeningQuarantineResolution.created_at.desc())
            .limit(MAX_LINEAGE_RULINGS)
        )
    ).all():
        rows.append(
            {
                "agent_id": quarantine.agent_id,
                "version": versions[quarantine.agent_id],
                "court": "quarantine",
                "resolution": resolution.resolution,
                "reason": _clip(resolution.reason),
                "evidence_references": [],
                "policy_version": quarantine.policy_version,
                "decided_at": resolution.created_at,
            }
        )
    for action, review in (
        await session.execute(
            select(AthReviewAction, AthReview)
            .join(AthReview, AthReview.review_id == AthReviewAction.review_id)
            .where(AthReview.agent_id.in_(ids))
            .order_by(AthReviewAction.created_at.desc())
            .limit(MAX_LINEAGE_RULINGS)
        )
    ).all():
        batch = (action.evidence or {}).get("batch_ruling") or {}
        references = [
            str(ref)[:512] for ref in (batch.get("evidence_references") or [])
        ][:64]
        rows.append(
            {
                "agent_id": review.agent_id,
                "version": versions[review.agent_id],
                "court": "ath",
                "resolution": action.action,
                "reason": _clip(action.reason),
                "evidence_references": references,
                "policy_version": review.original_policy_version,
                "decided_at": action.created_at,
            }
        )
    for dispute in (
        await session.scalars(
            select(ScreeningDispute)
            .where(
                ScreeningDispute.agent_id.in_(ids),
                ScreeningDispute.resolution.is_not(None),
                ScreeningDispute.resolved_at.is_not(None),
            )
            .order_by(ScreeningDispute.resolved_at.desc())
            .limit(MAX_LINEAGE_RULINGS)
        )
    ).all():
        rows.append(
            {
                "agent_id": dispute.agent_id,
                "version": versions[dispute.agent_id],
                "court": "dispute",
                "resolution": dispute.resolution,
                "reason": _clip(
                    f"miner: {dispute.message}\noperator: {dispute.resolution_reason}"
                ),
                "evidence_references": [],
                "policy_version": None,
                "decided_at": dispute.resolved_at,
            }
        )
    # Each source is already its newest MAX_LINEAGE_RULINGS; the global newest
    # MAX_LINEAGE_RULINGS are among them.
    rows.sort(key=lambda item: item["decided_at"], reverse=True)
    return rows[:MAX_LINEAGE_RULINGS]


def _finding_text(evidence: dict[str, Any]) -> str:
    """Compact finding summary, cited locations and the notes ledger."""
    parts: list[str] = []
    finding = evidence.get("finding") or {}
    if finding:
        categories = ",".join(str(c) for c in finding.get("categories") or [])
        parts.append(
            f"finding [{finding.get('risk_level')}; {categories}]: "
            f"{finding.get('summary', '')}"
        )
        for item in (finding.get("evidence") or [])[:12]:
            parts.append(
                f"  at {item.get('path')}:{item.get('line')} ({item.get('category')})"
            )
    for note in (evidence.get("review_notes") or [])[:MAX_NOTES_PER_FINDING]:
        where = f" {note.get('path')}:{note.get('line')}" if note.get("path") else ""
        parts.append(
            f"note {note.get('stage', 'l1')}/{note.get('kind')}"
            f"[{note.get('category', 'none')}]{where}: {note.get('summary', '')}"
        )
    return _clip("\n".join(parts))


async def _findings(session: AsyncSession, parent: Agent) -> list[dict[str, Any]]:
    events = (
        await session.scalars(
            select(ScreeningReviewEvent)
            .where(
                ScreeningReviewEvent.agent_id == parent.agent_id,
                ScreeningReviewEvent.event_kind == "automated",
            )
            .order_by(ScreeningReviewEvent.created_at.desc())
            .limit(MAX_LINEAGE_FINDINGS)
        )
    ).all()
    return [
        {
            "agent_id": parent.agent_id,
            "version": parent.version,
            "attempt_id": event.attempt_id,
            "policy_version": event.policy_version,
            "reviewer": event.reviewer_model,
            "outcome": event.outcome,
            "reason_code": event.reason_code,
            "reason": _clip(event.reason),
            "finding": _finding_text(event.evidence or {}),
            "recorded_at": event.created_at,
        }
        for event in events
    ]


@dataclass(frozen=True)
class ReviewLineageContext:
    """Everything the packet needs from the database, read in one transaction.

    The artifact reads and the diff (``assemble_review_lineage_packet``) run
    on this detached snapshot so no database connection is held across
    storage I/O and archive parsing.
    """

    agent_id: UUID
    attempt_id: UUID
    artifact_sha256: str
    policy_version: int
    candidate: _ArtifactRef
    parent: _ArtifactRef
    parent_row: dict[str, Any]
    history: list[dict[str, Any]]
    rulings: list[dict[str, Any]]
    findings: list[dict[str, Any]]

    def history_digest(self) -> str:
        """Digest of the database half of the packet, so a caller can re-read
        the history under a lock and refuse to bind a packet built before a
        ruling landed."""
        payload = {
            "parent": self.parent_row,
            "history": self.history,
            "rulings": self.rulings,
            "findings": self.findings,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode()
        ).hexdigest()


async def load_review_lineage(
    session: AsyncSession, *, agent: Agent, attempt: ScreeningAttempt
) -> ReviewLineageContext:
    """Read the lineage parent, history, rulings and findings for an attempt."""
    if attempt.lineage_parent_agent_id is None:
        raise ReviewLineageUnavailableError("attempt has no lineage parent")
    parent = await session.get(Agent, attempt.lineage_parent_agent_id)
    # A parent must be an earlier version of this same hotkey's agent; any other
    # row would put unrelated review context and a foreign artifact in the
    # packet.
    if (
        parent is None
        or parent.version is None
        or agent.version is None
        or parent.miner_hotkey != agent.miner_hotkey
        or parent.name != agent.name
        or parent.version >= agent.version
    ):
        raise ReviewLineageUnavailableError(
            "lineage parent is not a prior version of this agent"
        )
    history = (
        await session.scalars(
            select(Agent)
            .where(
                Agent.miner_hotkey == agent.miner_hotkey,
                Agent.name == agent.name,
                Agent.version.is_not(None),
                Agent.version <= parent.version,
            )
            .order_by(Agent.version.desc())
            .limit(MAX_LINEAGE_HISTORY)
        )
    ).all()
    versions = {row.agent_id: int(row.version or 0) for row in history}
    return ReviewLineageContext(
        agent_id=agent.agent_id,
        attempt_id=attempt.attempt_id,
        artifact_sha256=agent.sha256,
        policy_version=attempt.policy_version,
        candidate=_ArtifactRef(agent.agent_id, agent.sha256),
        parent=_ArtifactRef(parent.agent_id, parent.sha256),
        parent_row=_history_row(parent),
        history=[_history_row(row) for row in history],
        rulings=await _rulings(session, versions),
        findings=await _findings(session, parent),
    )


async def assemble_review_lineage_packet(
    context: ReviewLineageContext, storage: S3StorageClient
) -> ReviewLineagePacket:
    """Open both artifacts, diff them and seal the packet. No database."""
    candidate = await _open(context.candidate, storage)
    reference = await _open(context.parent, storage)
    diff = await asyncio.to_thread(_diff, candidate, reference)

    reasons = set(diff.reasons)
    if not context.rulings and not context.findings:
        reasons.add(ReviewLineageFullReviewReason.NO_REVIEW_HISTORY)
    elif not any(
        row["policy_version"] == context.policy_version
        for row in (*context.rulings, *context.findings)
    ):
        # History under an older policy is still context, but it cannot settle
        # unchanged code against the policy this attempt enforces.
        reasons.add(ReviewLineageFullReviewReason.PARENT_POLICY_DIFFERS)

    return seal_review_lineage_packet(
        {
            "agent_id": context.agent_id,
            "attempt_id": context.attempt_id,
            "artifact_sha256": context.artifact_sha256,
            "policy_version": context.policy_version,
            "parent": context.parent_row,
            "history": context.history,
            "scope": (ReviewLineageScope.FULL if reasons else ReviewLineageScope.DIFF),
            "full_review_reasons": sorted(reasons),
            "changes": diff.changes,
            "changed_file_count": diff.changed_file_count,
            "unchanged_file_count": diff.unchanged_file_count,
            "changed_line_count": diff.changed_line_count,
            "rulings": context.rulings,
            "findings": context.findings,
        }
    )


async def build_review_lineage_packet(
    session: AsyncSession,
    storage: S3StorageClient,
    *,
    agent: Agent,
    attempt: ScreeningAttempt,
) -> ReviewLineagePacket:
    """Assemble and seal the packet for an attempt that names a lineage parent.

    Callers that hold a transaction should ``load_review_lineage`` inside it
    and ``assemble_review_lineage_packet`` after it ends.
    """
    context = await load_review_lineage(session, agent=agent, attempt=attempt)
    return await assemble_review_lineage_packet(context, storage)


__all__ = [
    "MAX_SCOPED_CHANGED_LINES",
    "ReviewLineageContext",
    "ReviewLineageUnavailableError",
    "assemble_review_lineage_packet",
    "build_review_lineage_packet",
    "dockerfile_build_surface_changed",
    "is_build_surface",
    "is_dockerfile",
    "load_review_lineage",
]
