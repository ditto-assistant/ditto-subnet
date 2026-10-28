"""Deterministic family-diff attribution for near-duplicate copy holds.

A near-duplicate copy hold says only that the held agent looks like a matched
agent. The operator question is narrower: is every byte the held agent shares
with the matched agent explained by source that was already public when the
held agent was uploaded, and is none of the matched agent's own, unreleased
work present? That is computable without a model:

1. **Attribution.** Every file the held agent shares byte-for-byte with the
   matched agent (by content digest, so a moved file still counts) must also be
   byte-identical to a proven-public base (or to published starter-kit
   content). A shared file no public base carries is unexplained.
2. **Distinctive lines.** The matched agent's own delta from the public bases
   is the set of its lines that appear in no public base (and not in the held
   owner's earlier versions that predate the matched agent: who came first
   wrote them). Each such line found in the held agent is a present
   distinctive line.
3. **Held delta.** The held agent's own changes from its primary base: the
   unit an I1-I8/S1-S3 read has to cover.

Everything here is pure: callers load the artifacts, publication proofs and
screening evidence, and this module turns them into facts and one verdict.
Source text never leaves this module; evidence carries paths, line numbers and
line digests only, so a recommendation is safe to show under the read scope.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

TRIAGE_REVISION = "family-diff-v2"
STARTER_KIT_BASE = "starter-kit"
HELD_OWNER_PRIOR = "held-owner-prior"

# A shared file this small carries no authorship signal (empty ``__init__``,
# one-line config) and never counts as unexplained.
TRIVIAL_FILE_MAX_BYTES = 64
TRIVIAL_FILE_MAX_LINES = 2
# A distinctive line must be long and wordy enough that two authors are
# unlikely to type it independently.
DISTINCTIVE_LINE_MIN_CHARS = 20
DISTINCTIVE_LINE_MIN_TOKENS = 3
# A reject recommendation needs at least this many present distinctive lines
# (or a non-trivial shared file no public base carries).
REJECT_MIN_DISTINCTIVE_LINES = 3
MAX_EVIDENCE_ROWS = 64

_METADATA_DOC = re.compile(
    r"(?i)(^|/)(declaration|base|attribution|readme|notice|credits|lineage|"
    r"changelog|license)[^/]*\.(md|txt|rst)$"
)
_SHA256 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])")
_UUID = re.compile(
    r"(?i)(?<![0-9a-f-])[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![0-9a-f-])"
)
_TOKEN = re.compile(r"[A-Za-z0-9_]+")
# "dan0l v21", "Hades v6": a named, versioned base in declaration prose.
_NAME_VERSION = re.compile(r"(?<![\w-])([A-Za-z][\w.-]{1,63}) v(\d{1,5})\b")
_GENERATED_LOCKFILES = frozenset(
    {
        "uv.lock",
        "poetry.lock",
        "Cargo.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "go.sum",
        "Pipfile.lock",
    }
)


def is_metadata_doc(path: str) -> bool:
    """Declaration / attribution / readme prose: provenance, not code."""
    return _METADATA_DOC.search(path) is not None


def declared_references(texts: Mapping[str, str]) -> tuple[set[str], set[str]]:
    """``(sha256s, agent_ids)`` an artifact's declaration documents name.

    Only metadata documents are scanned: a 64-hex constant inside code is a
    hash, not a base declaration.
    """
    shas: set[str] = set()
    ids: set[str] = set()
    for path, text in texts.items():
        if not is_metadata_doc(path):
            continue
        shas.update(match.lower() for match in _SHA256.findall(text))
        ids.update(match.lower() for match in _UUID.findall(text))
    return shas, ids


def declared_name_versions(texts: Mapping[str, str]) -> set[tuple[str, int]]:
    """``(name, version)`` pairs declaration documents name, e.g. ``base v25``."""
    pairs: set[tuple[str, int]] = set()
    for path, text in texts.items():
        if not is_metadata_doc(path):
            continue
        for name, version in _NAME_VERSION.findall(text):
            pairs.add((name, int(version)))
    return pairs


def line_key(line: str) -> str:
    """Whitespace-collapsed form a line is compared under."""
    return " ".join(line.split())


def _is_candidate_line(key: str) -> bool:
    return (
        len(key) >= DISTINCTIVE_LINE_MIN_CHARS
        and len(_TOKEN.findall(key)) >= DISTINCTIVE_LINE_MIN_TOKENS
    )


def line_digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class ArtifactView:
    """What attribution needs from one stored artifact."""

    agent_id: str
    label: str
    sha256: str
    created_at: datetime
    # path -> sha256 for every member, text or opaque.
    digests: Mapping[str, str]
    # path -> text for the members the bounded read loaded.
    texts: Mapping[str, str]
    # Text members the bounded read skipped: not compared line-by-line.
    omitted: frozenset[str] = frozenset()

    def digest_set(self) -> frozenset[str]:
        return frozenset(self.digests.values())


@dataclass(frozen=True)
class PublicBase:
    """A base whose exact SHA was public before the held agent's upload."""

    view: ArtifactView
    relation: str
    publication_at: datetime | None


@dataclass(frozen=True)
class FamilyFacts:
    shared_count: int
    explained_count: int
    trivial_count: int
    shared: list[dict[str, Any]]
    unexplained: list[str]
    base_explains: dict[str, int]
    primary_base: str | None
    distinctive_total: int
    distinctive_present: list[dict[str, Any]]
    distinctive_present_count: int
    distinctive_files: list[str]
    prior_suppressed: int
    held_delta: dict[str, Any]
    incomplete: list[str]
    prior_explained_count: int = 0
    # How many of the matched agent's files some proven base carries byte for
    # byte. Zero means no proven base belongs to the matched family, so its
    # "distinctive" lines are simply all of its lines and prove nothing.
    matched_base_overlap: int = 0

    def as_evidence(self) -> dict[str, Any]:
        return {
            "shared_count": self.shared_count,
            "explained_count": self.explained_count,
            "trivial_count": self.trivial_count,
            "shared": self.shared[:MAX_EVIDENCE_ROWS],
            "unexplained": self.unexplained[:MAX_EVIDENCE_ROWS],
            "base_explains": self.base_explains,
            "primary_base": self.primary_base,
            "distinctive_total": self.distinctive_total,
            "distinctive_present_count": self.distinctive_present_count,
            "distinctive_present": self.distinctive_present[:MAX_EVIDENCE_ROWS],
            "distinctive_files": self.distinctive_files[:MAX_EVIDENCE_ROWS],
            "prior_suppressed": self.prior_suppressed,
            "prior_explained_count": self.prior_explained_count,
            "matched_base_overlap": self.matched_base_overlap,
            "held_delta": self.held_delta,
            "incomplete": self.incomplete,
        }


def _is_trivial(path: str, view: ArtifactView) -> bool:
    name = PurePosixPath(path).name
    if name in _GENERATED_LOCKFILES:
        return True
    text = view.texts.get(path)
    if text is None:
        return False
    nonblank = [line for line in text.splitlines() if line.strip()]
    return (
        len(text.encode("utf-8")) <= TRIVIAL_FILE_MAX_BYTES
        or len(nonblank) <= TRIVIAL_FILE_MAX_LINES
    )


def _keys(view: ArtifactView, *, skip_metadata: bool) -> dict[str, tuple[str, int]]:
    """``line key -> (path, 1-based line)`` of each key's first occurrence."""
    out: dict[str, tuple[str, int]] = {}
    for path in sorted(view.texts):
        if skip_metadata and is_metadata_doc(path):
            continue
        for number, raw in enumerate(view.texts[path].splitlines(), start=1):
            key = line_key(raw)
            if key and key not in out:
                out[key] = (path, number)
    return out


def _all_keys(views: Iterable[ArtifactView]) -> set[str]:
    keys: set[str] = set()
    for view in views:
        for text in view.texts.values():
            keys.update(line_key(raw) for raw in text.splitlines())
    keys.discard("")
    return keys


def _held_delta(held: ArtifactView, base: ArtifactView | None) -> dict[str, Any]:
    """The held agent's own changes from its primary base, by path."""
    if base is None:
        return {"base": None, "added": [], "modified": [], "removed": []}
    added: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    removed = sorted(set(base.digests) - set(held.digests))
    identical = 0
    for path in sorted(held.digests):
        if path not in base.digests:
            text = held.texts.get(path)
            added.append(
                {
                    "path": path,
                    "lines": len(text.splitlines()) if text is not None else None,
                }
            )
        elif held.digests[path] != base.digests[path]:
            before, after = base.texts.get(path), held.texts.get(path)
            plus = minus = None
            if before is not None and after is not None:
                plus = minus = 0
                matcher = difflib.SequenceMatcher(
                    a=before.splitlines(), b=after.splitlines(), autojunk=False
                )
                for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                    if tag in ("replace", "delete"):
                        minus += i2 - i1
                    if tag in ("replace", "insert"):
                        plus += j2 - j1
            modified.append({"path": path, "added_lines": plus, "removed_lines": minus})
        else:
            identical += 1
    return {
        "base": base.agent_id,
        "identical_count": identical,
        "added": added[:MAX_EVIDENCE_ROWS],
        "modified": modified[:MAX_EVIDENCE_ROWS],
        "removed": removed[:MAX_EVIDENCE_ROWS],
        "added_count": len(added),
        "modified_count": len(modified),
        "removed_count": len(removed),
    }


def attribute_family(
    *,
    held: ArtifactView,
    matched: ArtifactView,
    bases: Sequence[PublicBase],
    held_prior: Sequence[ArtifactView] = (),
    kit_digests: frozenset[str] = frozenset(),
) -> FamilyFacts:
    """Attribute the held/matched overlap to proven-public bases.

    ``bases`` must already be filtered to proven publication before the held
    agent's upload; ``held_prior`` holds the held owner's earlier versions that
    predate the matched agent (their lines are the held owner's own).
    """
    matched_digests = matched.digest_set()
    base_sets = [(base.view.agent_id, base.view.digest_set()) for base in bases]
    explains: dict[str, int] = {base.view.agent_id: 0 for base in bases}
    shared: list[dict[str, Any]] = []
    unexplained: list[str] = []
    explained_count = trivial_count = 0
    for path in sorted(held.digests):
        digest = held.digests[path]
        if digest not in matched_digests:
            continue
        explained_by = next(
            (agent_id for agent_id, digests in base_sets if digest in digests), None
        )
        if explained_by is None and digest in kit_digests:
            explained_by = STARTER_KIT_BASE
        if explained_by is None:
            # The held owner's own earlier version, uploaded before the
            # matched agent, already carried these bytes: who came first.
            explained_by = next(
                (
                    f"{HELD_OWNER_PRIOR}:{prior.agent_id}"
                    for prior in held_prior
                    if digest in prior.digest_set()
                ),
                None,
            )
        trivial = _is_trivial(path, held)
        row = {
            "path": path,
            "sha256": digest[:16],
            "explained_by": explained_by,
            "trivial": trivial,
        }
        shared.append(row)
        if explained_by is not None:
            explained_count += 1
            if explained_by in explains:
                explains[explained_by] += 1
        elif trivial:
            trivial_count += 1
        else:
            unexplained.append(path)

    # Primary base: the public base sharing the most exact files with the held
    # agent overall (not only within the overlap) -- the base its delta is
    # read against.
    primary: PublicBase | None = None
    best = -1
    held_set = held.digest_set()
    for base in bases:
        overlap = len(held_set & base.view.digest_set())
        if overlap > best:
            primary, best = base, overlap

    base_keys = _all_keys(base.view for base in bases)
    prior_keys = _all_keys(held_prior)
    held_index = _keys(held, skip_metadata=True)
    distinctive_total = prior_suppressed = 0
    present: list[dict[str, Any]] = []
    distinctive_files: set[str] = set()
    for key, (path, number) in _keys(matched, skip_metadata=True).items():
        if not _is_candidate_line(key) or key in base_keys:
            continue
        if key in prior_keys:
            prior_suppressed += 1
            continue
        distinctive_total += 1
        distinctive_files.add(path)
        hit = held_index.get(key)
        if hit is not None:
            present.append(
                {
                    "matched_location": f"{path}:{number}",
                    "held_location": f"{hit[0]}:{hit[1]}",
                    "line_sha256": line_digest(key),
                }
            )

    incomplete: list[str] = []
    if held.omitted:
        incomplete.append(f"held text not fully read: {len(held.omitted)} file(s)")
    if matched.omitted:
        incomplete.append(
            f"matched text not fully read: {len(matched.omitted)} file(s)"
        )
    for base in bases:
        if base.view.omitted:
            incomplete.append(
                f"base {base.view.agent_id} text not fully read: "
                f"{len(base.view.omitted)} file(s)"
            )

    return FamilyFacts(
        shared_count=len(shared),
        explained_count=explained_count,
        trivial_count=trivial_count,
        shared=shared,
        unexplained=unexplained,
        base_explains=explains,
        primary_base=primary.view.agent_id if primary is not None else None,
        distinctive_total=distinctive_total,
        distinctive_present=present,
        distinctive_present_count=len(present),
        distinctive_files=sorted(distinctive_files),
        prior_suppressed=prior_suppressed,
        held_delta=_held_delta(held, primary.view if primary is not None else None),
        incomplete=incomplete,
        matched_base_overlap=sum(
            1
            for digest in matched.digests.values()
            if any(digest in digests for _agent_id, digests in base_sets)
        ),
        prior_explained_count=sum(
            1
            for row in shared
            if str(row["explained_by"] or "").startswith(HELD_OWNER_PRIOR)
        ),
    )


@dataclass(frozen=True)
class DisclosureProof:
    """One candidate base's publication evidence against the held upload."""

    agent_id: str
    label: str
    sha256: str
    relation: str
    publication_at: datetime | None
    handoff_count: int
    proven: bool

    def as_evidence(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "label": self.label,
            "sha256": self.sha256,
            "relation": self.relation,
            "earliest_exact_sha_public_handoff_at": (
                self.publication_at.isoformat() if self.publication_at else None
            ),
            "exact_sha_public_handoff_count": self.handoff_count,
            "publication_proven_by_reference_time": self.proven,
        }


@dataclass(frozen=True)
class ScreeningEvidence:
    """Whether the held agent's own delta was read under the current policy."""

    attempt_id: str | None
    policy_version: int | None
    agent_policy_version: int
    lineage_scope: str | None
    open_quarantines: int

    @property
    def delta_read(self) -> bool:
        return (
            self.attempt_id is not None
            and self.policy_version is not None
            and self.policy_version >= self.agent_policy_version
            and self.open_quarantines == 0
        )

    def as_evidence(self) -> dict[str, Any]:
        return {
            "passed_attempt_id": self.attempt_id,
            "passed_policy_version": self.policy_version,
            "agent_screening_policy_version": self.agent_policy_version,
            "lineage_scope": self.lineage_scope,
            "open_quarantines": self.open_quarantines,
            "delta_read": self.delta_read,
        }


@dataclass(frozen=True)
class FamilyContext:
    held: ArtifactView
    matched: ArtifactView
    matched_public: bool
    same_owner: bool
    matched_after_held: bool
    substituted_from: str | None
    disclosures: Sequence[DisclosureProof]
    screening: ScreeningEvidence
    clear_precedents: Sequence[dict[str, Any]] = field(default_factory=tuple)
    reject_precedents: Sequence[dict[str, Any]] = field(default_factory=tuple)


@dataclass(frozen=True)
class FamilyVerdict:
    verdict: str  # clear | reject | escalate
    basis: str
    reason: str
    citations: list[dict[str, Any]]
    unresolved: list[str]
    auto_clear_blockers: list[str]

    @property
    def auto_clear_eligible(self) -> bool:
        return self.verdict == "clear" and not self.auto_clear_blockers


def _label(view: ArtifactView) -> str:
    return f"{view.label} ({view.agent_id[:8]})"


def _base_summary(facts: FamilyFacts, labels: Mapping[str, str]) -> str:
    parts = [
        f"{labels.get(agent_id, agent_id[:8])}: {count}"
        for agent_id, count in facts.base_explains.items()
        if count
    ]
    return ", ".join(parts) or "none"


def family_verdict(context: FamilyContext, facts: FamilyFacts) -> FamilyVerdict:
    """Map attribution facts onto the published copy-hold rule.

    CLEAR when the overlap is fully explained by proven-public bases and none
    of the unreleased matched agent's distinctive lines are present. REJECT
    (recommendation only, never applied automatically) when the unreleased
    matched agent's distinctive code is present. Otherwise ESCALATE with the
    exact unresolved checks.
    """
    held, matched = context.held, context.matched
    labels = {proof.agent_id: proof.label for proof in context.disclosures}
    proven = [proof for proof in context.disclosures if proof.proven]
    unproven = [proof for proof in context.disclosures if not proof.proven]
    clear_cites = list(context.clear_precedents)
    reject_cites = list(context.reject_precedents)
    blockers: list[str] = []
    if not context.screening.delta_read:
        blockers.append(
            "held agent has no passed screening of this exact artifact under its "
            "current policy with no open quarantine (I1-I8 delta read missing)"
        )
    delta = facts.held_delta
    delta_text = (
        f"held delta vs primary base: {delta.get('added_count', 0)} added, "
        f"{delta.get('modified_count', 0)} modified, "
        f"{delta.get('removed_count', 0)} removed file(s)"
    )
    screening_text = (
        f"I1-I8 delta read: passed screening attempt "
        f"{context.screening.attempt_id} under policy v"
        f"{context.screening.policy_version}"
        + (
            f" (lineage scope {context.screening.lineage_scope})"
            if context.screening.lineage_scope
            else ""
        )
        if context.screening.delta_read
        else "I1-I8 delta read: not covered by a passed screening of this artifact"
    )

    if context.same_owner:
        return FamilyVerdict(
            verdict="clear",
            basis="same_owner",
            reason=(
                f"Recommend CLEAR: {_label(held)} and the matched agent "
                f"{_label(matched)} were paid for by the same coldkey; "
                "same-owner reuse is not unauthorized copying. "
                f"{screening_text}."
            ),
            citations=clear_cites,
            unresolved=[],
            auto_clear_blockers=[
                *blockers,
                "same-owner clears are outside the auto-apply scope",
            ],
        )
    if context.matched_after_held:
        return FamilyVerdict(
            verdict="clear",
            basis="matched_after_held",
            reason=(
                f"Recommend CLEAR: the matched agent {_label(matched)} was "
                f"uploaded after {_label(held)} and its owner has no earlier "
                "version before that upload, so the held agent cannot contain "
                f"its code. {screening_text}."
            ),
            citations=clear_cites,
            unresolved=[],
            auto_clear_blockers=[
                *blockers,
                "time-order clears are outside the auto-apply scope",
            ],
        )

    unresolved: list[str] = []
    if facts.unexplained:
        unresolved.append(
            f"{len(facts.unexplained)} non-trivial file(s) the held agent shares "
            "byte-for-byte with the matched agent are not byte-identical to any "
            "proven-public base: " + ", ".join(facts.unexplained[:12])
        )
    if facts.distinctive_present_count:
        unresolved.append(
            f"{facts.distinctive_present_count} of the matched agent's "
            f"{facts.distinctive_total} distinctive line(s) (absent from every "
            "proven-public base) appear in the held agent: "
            + ", ".join(row["held_location"] for row in facts.distinctive_present[:12])
        )
    if not proven and not context.matched_public:
        unresolved.append(
            "no candidate base has an exact-SHA public handoff before the held "
            "upload; a missing handoff does not prove privacy, but it cannot "
            "support a clear"
            + (
                " (unproven: "
                + ", ".join(f"{p.label} {p.agent_id[:8]}" for p in unproven[:6])
                + ")"
                if unproven
                else ""
            )
        )
    unresolved.extend(facts.incomplete)

    copied_signal = facts.distinctive_present_count >= REJECT_MIN_DISTINCTIVE_LINES or (
        facts.unexplained and facts.distinctive_present_count > 0
    )
    if (
        not context.matched_public
        and copied_signal
        and proven
        and (facts.matched_base_overlap == 0)
    ):
        unresolved.append(
            "no proven-public base shares a file with the matched agent, so its "
            "distinctive lines cannot be separated from inherited code; find the "
            "matched family's public base or the original author"
        )
    if (
        not context.matched_public
        and copied_signal
        and proven
        and facts.matched_base_overlap > 0
    ):
        return FamilyVerdict(
            verdict="reject",
            basis="unreleased_distinctive_code_present",
            reason=(
                f"Recommend REJECT for operator confirmation: {_label(held)} "
                f"contains code distinctive to the matched agent {_label(matched)}, "
                "which had no exact-SHA public handoff before this upload. "
                + " ".join(unresolved[:2])
                + ". Confirm no other public source carries these lines before "
                "ruling; the court never applies a reject."
            ),
            citations=reject_cites,
            unresolved=unresolved,
            auto_clear_blockers=["reject is never applied automatically"],
        )

    fully_explained = not facts.unexplained and facts.distinctive_present_count == 0
    if fully_explained and (proven or context.matched_public) and not facts.incomplete:
        base_note = (
            "the matched agent itself was public before this upload"
            if context.matched_public
            else f"attribution by proven-public base: {_base_summary(facts, labels)}"
        )
        if context.matched_public:
            blockers.append(
                "matched agent public: clear rests on the matched agent, not a base"
            )
        if facts.prior_explained_count:
            blockers.append(
                f"{facts.prior_explained_count} shared file(s) are explained only by "
                "the held owner's earlier version (who came first), not a public base"
            )
        return FamilyVerdict(
            verdict="clear",
            basis=(
                "matched_agent_public"
                if context.matched_public
                else "released_base_explains_overlap"
            ),
            reason=(
                f"Recommend CLEAR: every file {_label(held)} shares byte-for-byte "
                f"with {_label(matched)} ({facts.shared_count} shared, "
                f"{facts.explained_count} explained, {facts.trivial_count} trivial) "
                f"is explained by released source; {base_note}. None of the "
                f"matched agent's {facts.distinctive_total} distinctive line(s) "
                f"appear in the held agent. Publication: "
                + "; ".join(
                    f"{p.label} {p.agent_id[:8]} public by exact-SHA handoff at "
                    f"{p.publication_at.isoformat() if p.publication_at else '?'}"
                    for p in proven[:4]
                )
                + f". {delta_text}. {screening_text}. Released-source reuse is "
                "permitted; similarity alone is not unauthorized copying."
            ),
            citations=clear_cites,
            unresolved=[],
            auto_clear_blockers=blockers,
        )
    return FamilyVerdict(
        verdict="escalate",
        basis="unresolved",
        reason=(
            f"HOLD for operator review: the overlap between {_label(held)} and "
            f"{_label(matched)} is not fully attributed. Unresolved: "
            + ("; ".join(unresolved) or "attribution incomplete")
            + f". {delta_text}."
        ),
        citations=[*clear_cites[:1], *reject_cites[:1]],
        unresolved=unresolved or ["attribution incomplete"],
        auto_clear_blockers=["escalations are never applied"],
    )


__all__ = [
    "HELD_OWNER_PRIOR",
    "STARTER_KIT_BASE",
    "TRIAGE_REVISION",
    "ArtifactView",
    "DisclosureProof",
    "FamilyContext",
    "FamilyFacts",
    "FamilyVerdict",
    "PublicBase",
    "ScreeningEvidence",
    "attribute_family",
    "declared_name_versions",
    "declared_references",
    "family_verdict",
    "is_metadata_doc",
    "line_digest",
    "line_key",
]
