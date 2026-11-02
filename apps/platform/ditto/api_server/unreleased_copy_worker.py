"""CPU side of the unreleased-copy signal, run in the sentinel's worker process.

Everything CPU-bound lives here so the API process only does storage and SQL:
extracting items, scoring a candidate, the fuzzy released-content pass and
assembling ``path:line`` evidence. Each function is top-level and takes and
returns picklable values. Module state is a per-process cache: released
artifacts' text fingerprints (compact sorted ``array('Q')``, released source is
immutable) and the candidate whose evidence is being assembled.

Called inline (a thread) when no worker process is configured, as in tests.
"""

from __future__ import annotations

from array import array
from bisect import bisect_left
from collections.abc import Sequence
from typing import Any

import numpy as np

from ditto.api_server.distinctive_content import Item, extract_items
from ditto.api_server.fingerprint import _excluded_from_fingerprints
from ditto.api_server.unreleased_copy import (
    MAX_MATCHES,
    CopySignal,
    IndexedAgent,
    attach_evidence,
    evaluate,
    refine_released,
    released_shingles,
)
from ditto.db.queries.unreleased_copy import index_keys_from

_RELEASED: dict[str, array] = {}
# The whole distinctive-item index (agent id -> sorted int64 keys), streamed in
# by the sentinel; about 8 bytes a key.
_INDEX: dict[str, Any] = {}
_CANDIDATE: tuple[str, dict[int, Item]] | None = None


def extract(raw: bytes) -> dict[int, Item] | None:
    extracted = extract_items(raw, excluded=_excluded_from_fingerprints)
    return None if extracted is None else dict(extracted.items)


def index_keys(raw: bytes) -> list[int] | None:
    """Sorted, public-subtracted item keys of one artifact (the index row)."""
    items = extract(raw)
    return None if items is None else index_keys_from(items)


def missing_released(agent_ids: Sequence[str]) -> list[str]:
    return [a for a in agent_ids if a not in _RELEASED]


def add_released(agent_id: str, raw: bytes) -> None:
    items = extract(raw) or {}
    _RELEASED[agent_id] = array("Q", sorted(released_shingles(items.values())))


class _Released:
    """Membership over several released artifacts' sorted fingerprints."""

    def __init__(self, arrays: list[array]) -> None:
        self._arrays = [a for a in arrays if len(a)]

    def __contains__(self, value: object) -> bool:
        if not isinstance(value, int):
            return False
        for values in self._arrays:
            i = bisect_left(values, value)
            if i < len(values) and values[i] == value:
                return True
        return False


def score(
    candidate: IndexedAgent,
    others: Sequence[IndexedAgent],
    overlaps: dict,
    threshold: float,
) -> CopySignal:
    """Evaluate with each other agent's overlap materialized here, not in the API."""
    from dataclasses import replace

    full = [replace(o, keys=frozenset(overlaps.get(o.agent_id, ()))) for o in others]
    return evaluate(candidate, full, threshold=threshold, max_matches=MAX_MATCHES * 4)


def refine(
    signal: CopySignal,
    candidate_raw: bytes,
    released_ids: Sequence[str],
    owner_names: Sequence[str],
) -> CopySignal | None:
    """Fuzzy released pass with the candidate's text; ``None`` if unreadable."""
    global _CANDIDATE
    items = extract(candidate_raw)
    if items is None:
        return None
    _CANDIDATE = (str(signal.candidate_agent_id), items)
    released = _Released([_RELEASED[a] for a in released_ids if a in _RELEASED])
    refined = refine_released(signal, items, released, owner_names=owner_names)
    return _truncate(refined)


def attach(signal: CopySignal, matched: dict[str, bytes]) -> CopySignal:
    """``path:line`` evidence for the matches, using the cached candidate."""
    if _CANDIDATE is None or _CANDIDATE[0] != str(signal.candidate_agent_id):
        return signal
    sources = {
        match.matched_agent_id: extract(matched[str(match.matched_agent_id)]) or {}
        for match in signal.matches
        if str(match.matched_agent_id) in matched
    }
    return attach_evidence(signal, _CANDIDATE[1], sources)


def _truncate(signal: CopySignal) -> CopySignal:
    from dataclasses import replace

    return replace(signal, matches=signal.matches[:MAX_MATCHES])


def truncate(signal: CopySignal) -> CopySignal:
    return _truncate(signal)


def index_put(rows: Sequence[tuple[str, bytes]]) -> int:
    """Add or replace index rows (keys as ``array('q')`` bytes, sorted)."""
    for agent_id, raw in rows:
        _INDEX[agent_id] = np.frombuffer(raw, dtype=np.int64).copy()
    return len(_INDEX)


def index_count() -> int:
    return len(_INDEX)


def overlap(keys: bytes, eligible: Sequence[str]) -> tuple[dict[str, array], list[str]]:
    """Each eligible agent's overlap with ``keys``; ids missing from the cache.

    One vectorized membership pass per row (``searchsorted`` against the sorted
    candidate keys) instead of a full-index scan in Postgres.
    """
    candidate = np.unique(np.frombuffer(keys, dtype=np.int64))
    out: dict[str, array] = {}
    missing: list[str] = []
    if not len(candidate):
        return out, missing
    last = len(candidate) - 1
    for agent_id in eligible:
        row = _INDEX.get(agent_id)
        if row is None:
            missing.append(agent_id)
            continue
        if not len(row):
            continue
        position = np.minimum(np.searchsorted(candidate, row), last)
        hit = row[candidate[position] == row]
        if len(hit):
            values = array("q")
            values.frombytes(hit.tobytes())
            out[agent_id] = values
    return out, missing
