"""Unreleased-copy signal: does a new artifact carry another owner's unreleased content?

The existing copy rules (:mod:`ditto.api_server.scoring_gate`) compare code
shape and run after scoring. They missed cross-language ports and partial
copies of agents that were never published (a Rust port of a Python agent
shared 0-4 fingerprint hashes with its source). This signal compares the
language-agnostic distinctive items of
:mod:`ditto.api_server.distinctive_content` from upload time, before an
artifact can score or go public.

For a candidate ``C`` uploaded at ``t`` and every other indexed agent ``S``:

* ``S`` must belong to a different owner (same hotkey, same payment coldkey and
  attested hotkey links are all one owner), be uploaded strictly before ``C``,
  and must **not** have been publicly released before ``t``: building on a
  released agent is permitted on SN118.
* A shared item counts only if it is not
  - in the packaged public corpus (removed when the index is built),
  - in **any** agent publicly released before ``t`` (a released base explains
    the overlap, whoever else also has it),
  - in any of ``C``'s owner's own agents uploaded before ``S`` (the owner held
    it first, so ``S`` cannot be where it came from),
  - common: held by at least :data:`COMMON_OWNER_FLOOR` distinct other owners
    among agents uploaded before ``t`` (shared idiom, not a secret).
* The pair score is the kind-weighted count of surviving items
  (:data:`KIND_WEIGHTS`); the candidate is flagged when its best pair reaches
  :data:`DEFAULT_THRESHOLD`.

Calibration (2026-11-01, 41 labeled artifacts, operator-private): every ruled
copy scored >= 119 against its ruled source and every cleared released-base
fork scored <= 3.25 (released bases <= 11.7), so 40 sits ~3x under the
smallest copy and ~3.4x over the largest negative. See
``apps/platform/docs/unreleased-copy-signal.md``.

Everything here is pure. Storage lives in :mod:`ditto.db.queries.unreleased_copy`
and the shadow loop in :mod:`ditto.api_server.unreleased_copy_sentinel`.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID

from ditto.api_server.distinctive_content import (
    KIND_IDENT,
    KIND_SEQ,
    KIND_SHORT,
    KIND_TEXT,
    KIND_WORD_SEQ,
    Item,
    key_kind,
)

SIGNAL_VERSION = "uc1"

KIND_WEIGHTS: Mapping[str, float] = {
    KIND_TEXT: 1.0,
    KIND_SEQ: 1.0,
    KIND_IDENT: 0.5,
    KIND_SHORT: 0.25,
    KIND_WORD_SEQ: 0.1,
}
DEFAULT_THRESHOLD = 40.0
COMMON_OWNER_FLOOR = 4
MAX_EVIDENCE_ITEMS = 60
MAX_MATCHES = 5


@dataclass(frozen=True)
class IndexedAgent:
    """One agent of the index, as of a candidate's upload."""

    agent_id: UUID
    owner: str
    """Canonical owner id: the same string for every agent of one owner."""
    uploaded_at: datetime
    released_at: datetime | None
    """Earliest publication evidence for this exact artifact, if any."""
    keys: frozenset[int]
    """Item keys; for a non-candidate, only its overlap with the candidate."""
    name: str = ""
    version: int | None = None
    hotkey: str = ""
    status: str = ""


@dataclass(frozen=True)
class SharedItem:
    kind: str
    preview: str
    candidate_path: str
    candidate_line: int
    matched_path: str
    matched_line: int


@dataclass(frozen=True)
class PairMatch:
    matched_agent_id: UUID
    matched_name: str
    matched_version: int | None
    matched_hotkey: str
    matched_status: str
    matched_uploaded_at: datetime
    matched_released_at: datetime | None
    score: float
    shared_count: int
    shared_by_kind: dict[str, int]
    shared_keys: frozenset[int] = field(repr=False)
    items: tuple[SharedItem, ...] = ()


@dataclass(frozen=True)
class CopySignal:
    version: str
    candidate_agent_id: UUID
    threshold: float
    flagged: bool
    top_score: float
    candidate_item_count: int
    compared_agent_count: int
    excluded: dict[str, int]
    """How many candidate items each filter removed (public is pre-removed)."""
    matches: tuple[PairMatch, ...]


def pair_score(keys: Iterable[int]) -> float:
    return round(sum(KIND_WEIGHTS[key_kind(k)] for k in keys), 2)


def _before(a: IndexedAgent, b: IndexedAgent) -> bool:
    """Upload chronology with the UUID as the deterministic tie-break."""
    return (a.uploaded_at, a.agent_id.int) < (b.uploaded_at, b.agent_id.int)


def evaluate(
    candidate: IndexedAgent,
    others: Sequence[IndexedAgent],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    common_owner_floor: int = COMMON_OWNER_FLOOR,
    max_matches: int = MAX_MATCHES,
) -> CopySignal:
    """Score ``candidate`` against every eligible earlier other-owner agent."""
    t = candidate.uploaded_at
    earlier = [
        o for o in others if o.agent_id != candidate.agent_id and _before(o, candidate)
    ]
    released: set[int] = set()
    owners_by_key: dict[int, set[str]] = {}
    for other in others:
        if other.agent_id == candidate.agent_id:
            continue
        if other.released_at is not None and other.released_at < t:
            released.update(other.keys & candidate.keys)
    for other in earlier:
        if other.owner != candidate.owner:
            for key in other.keys & candidate.keys:
                owners_by_key.setdefault(key, set()).add(other.owner)
    common = {k for k, o in owners_by_key.items() if len(o) >= common_owner_floor}
    base = candidate.keys - released - common
    own_history = [o for o in earlier if o.owner == candidate.owner]
    matches: list[PairMatch] = []
    compared = 0
    for source in earlier:
        if source.owner == candidate.owner or (
            source.released_at is not None and source.released_at < t
        ):
            continue
        compared += 1
        shared = base & source.keys
        for own in own_history:
            if shared and _before(own, source):
                shared = shared - own.keys
        if not shared:
            continue
        by_kind: dict[str, int] = {}
        for key in shared:
            kind = key_kind(key)
            by_kind[kind] = by_kind.get(kind, 0) + 1
        matches.append(
            PairMatch(
                matched_agent_id=source.agent_id,
                matched_name=source.name,
                matched_version=source.version,
                matched_hotkey=source.hotkey,
                matched_status=source.status,
                matched_uploaded_at=source.uploaded_at,
                matched_released_at=source.released_at,
                score=pair_score(shared),
                shared_count=len(shared),
                shared_by_kind=dict(sorted(by_kind.items())),
                shared_keys=frozenset(shared),
            )
        )
    matches.sort(
        key=lambda m: (-m.score, m.matched_uploaded_at, str(m.matched_agent_id))
    )
    top = matches[0].score if matches else 0.0
    return CopySignal(
        version=SIGNAL_VERSION,
        candidate_agent_id=candidate.agent_id,
        threshold=threshold,
        flagged=top >= threshold,
        top_score=top,
        candidate_item_count=len(base),
        compared_agent_count=compared,
        excluded={
            "released_base": len(candidate.keys & released),
            "common": len((candidate.keys - released) & common),
        },
        matches=tuple(matches[:max_matches]),
    )


def attach_evidence(
    signal: CopySignal,
    candidate_items: Mapping[int, Item],
    matched_items: Mapping[UUID, Mapping[int, Item]],
    *,
    max_items: int = MAX_EVIDENCE_ITEMS,
) -> CopySignal:
    """Fill each match with ``path:line`` pairs on both sides, heaviest first."""
    matches = []
    for match in signal.matches:
        source = matched_items.get(match.matched_agent_id, {})
        items = [
            SharedItem(
                kind=candidate_items[k].kind,
                preview=candidate_items[k].preview,
                candidate_path=candidate_items[k].path,
                candidate_line=candidate_items[k].line,
                matched_path=source[k].path,
                matched_line=source[k].line,
            )
            for k in match.shared_keys
            if k in candidate_items and k in source
        ]
        items.sort(
            key=lambda s: (-KIND_WEIGHTS[s.kind], s.candidate_path, s.candidate_line)
        )
        matches.append(replace(match, items=tuple(items[:max_items])))
    return replace(signal, matches=tuple(matches))


def canonical_owners(
    agents: Iterable[tuple[UUID, str, str | None]],
    links: Collection[tuple[str, str]] = (),
) -> dict[UUID, str]:
    """Union hotkeys, payment coldkeys and attested hotkey links into owners.

    ``agents`` yields ``(agent_id, hotkey, coldkey)``. Two agents are one owner
    when they share a hotkey, a payment coldkey, or a hotkey pair in ``links``.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    rows = list(agents)
    for _, hotkey, coldkey in rows:
        find(f"hk:{hotkey}")
        if coldkey:
            union(f"hk:{hotkey}", f"ck:{coldkey}")
    for a, b in links:
        union(f"hk:{a}", f"hk:{b}")
    return {agent_id: find(f"hk:{hotkey}") for agent_id, hotkey, _ in rows}
