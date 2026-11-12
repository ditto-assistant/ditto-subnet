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
    the overlap, whoever else also has it), exactly or, for text, fuzzily
    (:func:`refine_released`: a port re-wraps and re-interpolates prompts),
  - in any of ``C``'s owner's own agents uploaded before ``S`` (the owner held
    it first, so ``S`` cannot be where it came from),
  - common: held by at least :data:`COMMON_OWNER_FLOOR` distinct other owners
    among agents uploaded before ``t`` (shared idiom, not a secret).
* A pair that also shares a rare external namespace (a Hugging Face or GitHub
  account, a registry user) *named after the candidate's owner* (a namespace
  containing one of that owner's agent-name tokens) is marked
  ``probable_same_owner`` and is never flagged: one operator under two
  unlinked keys is not a copier. A namespace named after the source is what a
  copier carries over with the code, so it excuses nothing.
* The pair score is the kind-weighted count of surviving items
  (:data:`KIND_WEIGHTS`); the candidate is flagged when its best pair reaches
  the audited threshold (:data:`DEFAULT_THRESHOLD` by default).

Calibration lives in ``apps/platform/docs/unreleased-copy-signal.md``.

Everything here is pure. Storage lives in :mod:`ditto.db.queries.unreleased_copy`
and the shadow loop in :mod:`ditto.api_server.unreleased_copy_sentinel`.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Container, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from uuid import UUID

from ditto.api_server.distinctive_content import (
    KIND_IDENT,
    KIND_RESOURCE,
    KIND_SEQ,
    KIND_SHORT,
    KIND_TEXT,
    KIND_WORD_SEQ,
    Item,
    item_key,
    key_kind,
    public_item_keys,
)

SIGNAL_VERSION = "uc3"

KIND_WEIGHTS: Mapping[str, float] = {
    KIND_TEXT: 1.0,
    KIND_SEQ: 1.0,
    # A port names its constants after the messages it ports, public or not.
    KIND_IDENT: 0.25,
    KIND_SHORT: 0.25,
    KIND_WORD_SEQ: 0.1,
    KIND_RESOURCE: 0.0,
}
DEFAULT_THRESHOLD = 40.0
COMMON_OWNER_FLOOR = 4
MAX_EVIDENCE_ITEMS = 60
MAX_MATCHES = 5
# An item whose word 5-shingles are at least this contained in agents released
# before the upload is released content in a different surface form.
FUZZY_SHINGLE_WORDS = 5
FUZZY_RELEASED_CONTAINMENT = 0.9


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
    probable_same_owner: bool = False
    """Both artifacts reference a rare external namespace named after the
    candidate's owner. A match carrying it is listed but never flagged."""
    shared_resource_keys: frozenset[int] = field(default=frozenset(), repr=False)


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


def _with_keys(match: PairMatch, shared: frozenset[int]) -> PairMatch:
    scored = frozenset(k for k in shared if key_kind(k) != KIND_RESOURCE)
    by_kind: dict[str, int] = {}
    for key in scored:
        kind = key_kind(key)
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return replace(
        match,
        score=pair_score(scored),
        shared_count=len(scored),
        shared_by_kind=dict(sorted(by_kind.items())),
        shared_keys=scored,
        items=(),
    )


def _finish(
    signal: CopySignal, matches: Iterable[PairMatch], excluded: dict[str, int]
) -> CopySignal:
    kept = [m for m in matches if m.shared_keys]
    kept.sort(
        key=lambda m: (
            m.probable_same_owner,
            -m.score,
            m.matched_uploaded_at,
            str(m.matched_agent_id),
        )
    )
    top = max((m.score for m in kept if not m.probable_same_owner), default=0.0)
    return replace(
        signal,
        flagged=top >= signal.threshold,
        top_score=top,
        excluded=excluded,
        matches=tuple(kept),
    )


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
        resources = frozenset(k for k in shared if key_kind(k) == KIND_RESOURCE)
        template = PairMatch(
            matched_agent_id=source.agent_id,
            matched_name=source.name,
            matched_version=source.version,
            matched_hotkey=source.hotkey,
            matched_status=source.status,
            matched_uploaded_at=source.uploaded_at,
            matched_released_at=source.released_at,
            score=0.0,
            shared_count=0,
            shared_by_kind={},
            shared_keys=frozenset(),
            shared_resource_keys=resources,
        )
        matches.append(_with_keys(template, frozenset(shared)))
    scored_base = [k for k in base if key_kind(k) != KIND_RESOURCE]
    signal = CopySignal(
        version=SIGNAL_VERSION,
        candidate_agent_id=candidate.agent_id,
        threshold=threshold,
        flagged=False,
        top_score=0.0,
        candidate_item_count=len(scored_base),
        compared_agent_count=compared,
        excluded={},
        matches=(),
    )
    finished = _finish(
        signal,
        matches,
        {
            "released_base": len(candidate.keys & released),
            "common": len((candidate.keys - released) & common),
        },
    )
    return replace(finished, matches=finished.matches[:max_matches])


def _hash(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


def _shingles(text: str) -> list[int]:
    words = text.replace("|", " ").split()
    n = FUZZY_SHINGLE_WORDS
    return [_hash(" ".join(words[i : i + n])) for i in range(len(words) - n + 1)]


def _elements(item: Item) -> list[str]:
    text = item.text or item.preview
    if item.kind in (KIND_SEQ, KIND_WORD_SEQ):
        return [e.strip() for e in text.split(" | ") if e.strip()]
    return [text]


def released_shingles(items: Iterable[Item]) -> frozenset[int]:
    """Fuzzy fingerprint of one released artifact's literal text.

    Word 5-shingles of every text-bearing item, plus a hash of every whole
    literal (each element of a literal window, each short literal and each
    sentence). A port re-orders and re-wraps released literals; their elements
    and word runs survive.
    """
    out: set[int] = set()
    for item in items:
        if item.kind in (KIND_RESOURCE, KIND_IDENT):
            continue
        out.update(_shingles(item.text or item.preview))
        out.update(_hash("e:" + element) for element in _elements(item))
    return frozenset(out)


def _covered(element: str, released: Container[int], public: Container[int]) -> bool:
    """A literal element nobody needs to copy: one word, released, or public."""
    return (
        " " not in element
        or _hash("e:" + element) in released
        or item_key(KIND_SHORT, element) in public
        or item_key(KIND_TEXT, element) in public
    )


def _released(
    item: Item, released: Container[int], public: Container[int], containment: float
) -> bool:
    if item.kind == KIND_IDENT:
        return False
    if all(_covered(element, released, public) for element in _elements(item)):
        return True
    shingles = _shingles(item.text or item.preview)
    return bool(shingles) and sum(s in released for s in shingles) >= containment * len(
        shingles
    )


def _name_tokens(names: Iterable[str]) -> set[str]:
    return {
        token
        for name in names
        for token in re.findall(r"[a-z]+", name.lower())
        if len(token) >= 4
    }


def refine_released(
    signal: CopySignal,
    candidate_items: Mapping[int, Item],
    released: Container[int],
    *,
    owner_names: Iterable[str] = (),
    public: Container[int] | None = None,
    containment: float = FUZZY_RELEASED_CONTAINMENT,
) -> CopySignal:
    """Second pass with the candidate's text in hand.

    Drops shared items that are released content in another surface form
    (fuzzy), and marks a match ``probable_same_owner`` when the two share an
    external namespace named after the candidate's owner.
    """
    tokens = _name_tokens(owner_names)
    public = public_item_keys() if public is None else public
    dropped: set[int] = set()
    matches: list[PairMatch] = []
    for match in signal.matches:
        keep: set[int] = set()
        for key in match.shared_keys:
            item = candidate_items.get(key)
            if item is not None and _released(item, released, public, containment):
                dropped.add(key)
            else:
                keep.add(key)
        namespaces = [
            candidate_items[k].text.split(":", 1)[-1].lower()
            for k in match.shared_resource_keys
            if k in candidate_items
        ]
        same_owner = any(
            token in re.sub(r"[^a-z0-9]", "", ns)
            for ns in namespaces
            for token in tokens
        )
        matches.append(
            replace(_with_keys(match, frozenset(keep)), probable_same_owner=same_owner)
        )
    return _finish(signal, matches, {**signal.excluded, "released_fuzzy": len(dropped)})


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
