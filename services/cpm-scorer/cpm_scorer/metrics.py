"""Metrics and score for cpm_score_version 1 (contract §4.3–4.4).

Pure functions over relevance scores, so they are tested without a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

CPM_SCORE_VERSION = 1
BUDGET_TOKENS = 2048
TOOL_K = 8
MEMORY_WEIGHT = 0.6
TOOL_WEIGHT = 0.4
DISTRACTOR_PENALTY = 0.5
DISTRACTOR_SPAN_TYPES = frozenset({"distractor", "forbidden"})


def rank(scores: Mapping[str, float]) -> list[str]:
    """Candidate ids by score descending; ties go to the lexically smaller id."""
    return sorted(scores, key=lambda cid: (-scores[cid], cid))


def pack(
    ranked: Sequence[str], token_counts: Mapping[str, int], budget: int
) -> list[str]:
    """Pack whole candidates in rank order while the running total stays
    <= budget. A candidate that does not fit is skipped and packing continues.
    """
    packed: list[str] = []
    used = 0
    for cid in ranked:
        n = token_counts[cid]
        if used + n <= budget:
            packed.append(cid)
            used += n
    return packed


def recall(selected: Iterable[str], relevant: Sequence[str]) -> float:
    rel = set(relevant)
    if not rel:
        raise ValueError("recall needs at least one relevant id")
    return len(rel.intersection(selected)) / len(rel)


def distractor_share(
    packed: Sequence[str], relevant: Sequence[str], distractor_ids: Iterable[str]
) -> float:
    """Share of packed candidates that carry a distractor or forbidden span and
    are not relevant. An empty packet has no distractors (0.0)."""
    if not packed:
        return 0.0
    rel = set(relevant)
    bad = set(distractor_ids)
    return sum(1 for cid in packed if cid in bad and cid not in rel) / len(packed)


def distractor_ids(spans: Mapping[str, Sequence[Mapping]]) -> set[str]:
    """Candidate ids with a `distractor` or `forbidden` span in the export."""
    return {
        cid
        for cid, ss in spans.items()
        if any(s.get("type") in DISTRACTOR_SPAN_TYPES for s in ss)
    }


@dataclass(frozen=True)
class MemoryCaseResult:
    case_id: str
    recall: float
    distractor_rate: float
    packed: int
    packed_tokens: int


@dataclass(frozen=True)
class ToolCaseResult:
    case_id: str
    recall: float


def memory_case(
    case_id: str,
    scores: Mapping[str, float],
    token_counts: Mapping[str, int],
    relevant: Sequence[str],
    spans: Mapping[str, Sequence[Mapping]],
    budget: int = BUDGET_TOKENS,
) -> MemoryCaseResult:
    packed = pack(rank(scores), token_counts, budget)
    return MemoryCaseResult(
        case_id=case_id,
        recall=recall(packed, relevant),
        distractor_rate=distractor_share(packed, relevant, distractor_ids(spans)),
        packed=len(packed),
        packed_tokens=sum(token_counts[c] for c in packed),
    )


def tool_case(
    case_id: str, scores: Mapping[str, float], target: Sequence[str], k: int = TOOL_K
) -> ToolCaseResult:
    return ToolCaseResult(case_id=case_id, recall=recall(rank(scores)[:k], target))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def seed_components(
    memory: Sequence[MemoryCaseResult], tools: Sequence[ToolCaseResult]
) -> dict[str, float]:
    return {
        "memory_recall_at_b": _mean([r.recall for r in memory]),
        "distractor_rate": _mean([r.distractor_rate for r in memory]),
        "tool_recall_at_k": _mean([r.recall for r in tools]),
    }


def seed_score(components: Mapping[str, float]) -> float:
    return (
        MEMORY_WEIGHT
        * components["memory_recall_at_b"]
        * (1.0 - DISTRACTOR_PENALTY * components["distractor_rate"])
        + TOOL_WEIGHT * components["tool_recall_at_k"]
    )


def aggregate(per_seed: Sequence[Mapping[str, float]]) -> dict[str, float]:
    """Mean of each component and of the seed score over the seeds."""
    if not per_seed:
        raise ValueError("no seeds")
    keys = ("memory_recall_at_b", "distractor_rate", "tool_recall_at_k")
    out = {k: _mean([s[k] for s in per_seed]) for k in keys}
    out["score"] = _mean([s["score"] for s in per_seed])
    return out
