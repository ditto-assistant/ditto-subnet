"""Fusion and metrics, exactly as the product ranks a reranked pool."""

from __future__ import annotations

import math
from collections.abc import Sequence


def fuse_rrf(ce_scores: Sequence[float], rrf_k: float, ce_weight: float) -> list[int]:
    """Port of backend crossencoder ``fuseRRF``.

    ``ce_scores[i]`` is the cross-encoder score of the candidate at base rank
    ``i``. Returns candidate indices best first. Both sorts are stable, so ties
    keep base order and a correct top hit is never demoted by a tie.
    """
    n = len(ce_scores)
    by_ce = sorted(range(n), key=lambda i: -ce_scores[i])
    ce_rank = [0] * n
    for rank, idx in enumerate(by_ce):
        ce_rank[idx] = rank
    fused = [
        ce_weight * (1.0 / (rrf_k + ce_rank[i] + 1))
        + (1.0 - ce_weight) * (1.0 / (rrf_k + i + 1))
        for i in range(n)
    ]
    return sorted(range(n), key=lambda i: -fused[i])


def ndcg_at_k(order: Sequence[str], gold: set[str], k: int) -> float:
    """Binary-gain NDCG@k with log2 discount."""
    if not gold:
        raise ValueError("a pool needs at least one gold candidate")
    dcg = sum(1.0 / math.log2(i + 2) for i, c in enumerate(order[:k]) if c in gold)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(k, len(gold))))
    return dcg / ideal


def recall_at_k(order: Sequence[str], gold: set[str], k: int) -> float:
    if not gold:
        raise ValueError("a pool needs at least one gold candidate")
    return sum(1 for c in order[:k] if c in gold) / len(gold)
