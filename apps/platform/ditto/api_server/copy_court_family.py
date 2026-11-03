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
   unit an I1-I8/S1-S3 read has to cover. A fork of released source is
   eligible only with its own code changes (operator ruling 2026-10-29): a
   held agent whose runtime files are byte-identical to its released primary
   base, with only declaration or attribution prose changed, is an exact
   duplicate with no contribution and never gets a CLEAR.

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

TRIAGE_REVISION = "family-diff-v3"
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
# A present distinctive line this close to a line of the same file in a public
# base is a small edit of released code (one renamed variable, one added
# argument) that two authors can make independently. A few of them are
# reported apart as convergent edits; this many or more are a pattern and
# count as present distinctive code.
CONVERGENT_EDIT_MIN_RATIO = 0.85
CONVERGENT_EDIT_MAX = 2
CONVERGENT_SCAN_MAX_HITS = 24
MAX_EVIDENCE_ROWS = 64

_METADATA_DOC = re.compile(
    r"(?i)(^|/)(declaration|base|attribution|readme|notice|credits|lineage|"
    r"changelog|changes|upstream|fork|provenance|license)[^/]*\.(md|txt|rst)$"
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


def is_runtime_file(path: str) -> bool:
    """A member that can change what the agent does: not prose, not a lockfile."""
    return not is_metadata_doc(path) and PurePosixPath(path).name not in (
        _GENERATED_LOCKFILES
    )


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
    convergent_edits: list[dict[str, Any]] = field(default_factory=list)
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
            "convergent_edits": self.convergent_edits[:MAX_EVIDENCE_ROWS],
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


def _closest_base_ratio(key: str, path: str, bases: Sequence[PublicBase]) -> float:
    """Best similarity of ``key`` to any line of ``path`` in a public base."""
    best = 0.0
    for base in bases:
        text = base.view.texts.get(path)
        if text is None:
            continue
        matcher = difflib.SequenceMatcher(autojunk=False)
        matcher.set_seq2(key)
        for raw in text.splitlines():
            other = line_key(raw)
            if not other or abs(len(other) - len(key)) > len(key) // 2:
                continue
            matcher.set_seq1(other)
            if matcher.real_quick_ratio() <= best or matcher.quick_ratio() <= best:
                continue
            best = max(best, matcher.ratio())
    return best


def _all_keys(views: Iterable[ArtifactView]) -> set[str]:
    keys: set[str] = set()
    for view in views:
        for text in view.texts.values():
            keys.update(line_key(raw) for raw in text.splitlines())
    keys.discard("")
    return keys


def _whitespace_only(before: str | None, after: str | None) -> bool:
    """True when two texts differ only in whitespace or blank lines."""
    if before is None or after is None:
        return False
    keys = [
        [key for key in (line_key(raw) for raw in text.splitlines()) if key]
        for text in (before, after)
    ]
    return keys[0] == keys[1]


def _held_delta(held: ArtifactView, base: ArtifactView | None) -> dict[str, Any]:
    """The held agent's own changes from its primary base, by path.

    ``runtime_changed_count`` counts added or content-modified runtime files
    (not declaration prose, lockfiles or whitespace-only edits): the held
    agent's own contribution. ``runtime_removed_count`` counts runtime files
    the base has and the held agent dropped.
    """
    if base is None:
        return {
            "base": None,
            "added": [],
            "modified": [],
            "removed": [],
            "runtime_changed": [],
            "runtime_changed_count": None,
            "runtime_removed_count": None,
        }
    added: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    removed = sorted(set(base.digests) - set(held.digests))
    runtime_changed: list[str] = []
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
            if is_runtime_file(path):
                runtime_changed.append(path)
        elif held.digests[path] != base.digests[path]:
            before, after = base.texts.get(path), held.texts.get(path)
            if is_runtime_file(path) and not _whitespace_only(before, after):
                runtime_changed.append(path)
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
        "runtime_changed": runtime_changed[:MAX_EVIDENCE_ROWS],
        "runtime_changed_count": len(runtime_changed),
        "runtime_removed_count": sum(1 for path in removed if is_runtime_file(path)),
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
    convergent: list[dict[str, Any]] = []
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
            row = {
                "matched_location": f"{path}:{number}",
                "held_location": f"{hit[0]}:{hit[1]}",
                "line_sha256": line_digest(key),
            }
            # Past a few dozen hits the overlap is a pattern, not a
            # coincidence; skip the similarity scan (it is the costly part).
            ratio = (
                _closest_base_ratio(key, path, bases)
                if len(present) + len(convergent) < CONVERGENT_SCAN_MAX_HITS
                else 0.0
            )
            if ratio >= CONVERGENT_EDIT_MIN_RATIO:
                convergent.append({**row, "base_similarity": round(ratio, 3)})
            else:
                present.append(row)
    if len(convergent) > CONVERGENT_EDIT_MAX:
        present.extend(
            {key: value for key, value in row.items() if key != "base_similarity"}
            for row in convergent
        )
        convergent = []

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
        convergent_edits=convergent,
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


def _own_delta_gate(
    context: FamilyContext,
    facts: FamilyFacts,
    labels: Mapping[str, str],
    delta_text: str,
    screening_text: str,
) -> FamilyVerdict | None:
    """Downgrade a released-base CLEAR that carries no own runtime change.

    Operator ruling 2026-10-29: a fork of a publicly released agent is eligible
    only with its own code changes. Returns ``None`` when the held agent has a
    non-empty runtime delta from its primary base (the CLEAR may stand).
    """
    held = context.held
    delta = facts.held_delta
    changed = delta.get("runtime_changed_count")
    removed = delta.get("runtime_removed_count") or 0
    if changed:
        return None
    base_id = delta.get("base")
    base_label = (
        f"{labels.get(base_id, base_id[:8])} ({base_id[:8]})"
        if base_id
        else "no primary base"
    )
    if base_id is None or changed is None:
        return FamilyVerdict(
            verdict="escalate",
            basis="own_delta_unknown",
            reason=(
                f"HOLD for operator review: the overlap with released source is "
                f"explained, but {_label(held)} has no primary base to measure its "
                "own runtime changes against. A fork of released source is "
                "eligible only with its own code changes. "
                f"{delta_text}. {screening_text}."
            ),
            citations=[],
            unresolved=["held agent's own runtime delta could not be measured"],
            auto_clear_blockers=["escalations are never applied"],
        )
    if removed:
        return FamilyVerdict(
            verdict="escalate",
            basis="own_delta_removal_only",
            reason=(
                f"HOLD for operator review: {_label(held)} adds or changes no "
                f"runtime file relative to its released primary base {base_label}; "
                f"it only removes {removed} runtime file(s). A fork of released "
                "source is eligible only with its own code changes; decide whether "
                f"the removal is a contribution. {delta_text}. {screening_text}."
            ),
            citations=list(context.reject_precedents[:1]),
            unresolved=[
                f"only {removed} runtime file removal(s) from the released base; "
                "no added or modified runtime file"
            ],
            auto_clear_blockers=["escalations are never applied"],
        )
    return FamilyVerdict(
        verdict="reject",
        basis="unmodified_fork_of_released_base",
        reason=(
            f"Recommend REJECT for operator confirmation: every runtime file of "
            f"{_label(held)} is byte-identical to its released primary base "
            f"{base_label} ({delta.get('identical_count', 0)} identical file(s)); "
            "only declaration or attribution prose differs. A fork of a publicly "
            "released agent is eligible only with its own code changes; an "
            "unmodified fork is an exact duplicate with no contribution "
            f"(operator ruling 2026-10-29). {delta_text}. {screening_text}. "
            "The court never applies a reject."
        ),
        citations=list(context.reject_precedents),
        unresolved=["no own runtime change from the released primary base"],
        auto_clear_blockers=["reject is never applied automatically"],
    )


def family_verdict(context: FamilyContext, facts: FamilyFacts) -> FamilyVerdict:
    """Map attribution facts onto the published copy-hold rule.

    CLEAR when the overlap is fully explained by proven-public bases, none
    of the unreleased matched agent's distinctive lines are present, and the
    held agent changes at least one runtime file of its primary base. A fork
    with no own runtime change is a REJECT recommendation. REJECT
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
        own_delta = _own_delta_gate(context, facts, labels, delta_text, screening_text)
        if own_delta is not None:
            return own_delta
        base_note = (
            "the matched agent itself was public before this upload"
            if context.matched_public
            else f"attribution by proven-public base: {_base_summary(facts, labels)}"
        )
        if context.matched_public:
            blockers.append(
                "matched agent public: clear rests on the matched agent, not a base"
            )
        if facts.convergent_edits:
            blockers.append(
                f"{len(facts.convergent_edits)} matched-agent line(s) in the held "
                "agent are small edits of a public base line (convergent edits); "
                "an operator should glance at them"
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
                f"appear in the held agent"
                + (
                    f" beyond {len(facts.convergent_edits)} convergent one-line "
                    "edit(s) of a public base line ("
                    + ", ".join(row["held_location"] for row in facts.convergent_edits)
                    + ")"
                    if facts.convergent_edits
                    else ""
                )
                + ". Publication: "
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
    "is_runtime_file",
    "line_digest",
    "line_key",
]
