"""Reference adapter math and deterministic synthetic generators.

``apply`` reproduces ``QueryAdapter.apply`` from the Seed Memories v4 source
(hippocampus/train/query_adapter.py) operation for operation in float32:

    z     = x @ W1 + b1
    h     = 0.5 * z * (1 + tanh(0.79788456 * (z + 0.044715 * z**3)))   # GELU
    delta = h @ W2 + b2
    out   = x + alpha * delta
    out   = out / (||out|| + 1e-9)

``tests/test_reference.py`` pins this against golden vectors produced by that
source class on the synthetic weights below. Every weight, tenant and query
here is synthetic: they come from seeded RNGs, never from a trained adapter.
"""

from __future__ import annotations

import numpy as np

from .contract import DEFAULT_ALPHA, DIM, HIDDEN, NORM_EPS, AdapterWeights, F32Array

_GELU_C = np.float32(0.79788456)
_GELU_K = np.float32(0.044715)


def apply(weights: AdapterWeights, query: F32Array) -> F32Array:
    """Apply one adapter to one query vector (shape (DIM,), float32)."""
    x = np.asarray(query, dtype=np.float32)
    z = x @ weights.W1 + weights.b1
    h = np.float32(0.5) * z * (np.float32(1.0) + np.tanh(_GELU_C * (z + _GELU_K * z**3)))
    delta = h @ weights.W2 + weights.b2
    out = x + np.float32(weights.alpha) * delta
    out = out / (np.linalg.norm(out) + np.float32(NORM_EPS))
    return np.asarray(out, dtype=np.float32)


def identity(query: F32Array) -> F32Array:
    """Tier-0 fallback: the raw query, L2-normalized the same way."""
    x = np.asarray(query, dtype=np.float32)
    return np.asarray(x / (np.linalg.norm(x) + np.float32(NORM_EPS)), dtype=np.float32)


def synthetic_adapter(seed: int, alpha: float = DEFAULT_ALPHA) -> AdapterWeights:
    """Deterministic synthetic adapter. The up-projection is non-zero (a trained
    adapter starts at W2 = 0) so every synthetic version is distinguishable."""
    rng = np.random.default_rng(np.random.SeedSequence([0x51A7, seed]))
    return AdapterWeights(
        W1=(rng.standard_normal((DIM, HIDDEN)) / np.sqrt(DIM)).astype(np.float32),
        b1=(rng.standard_normal(HIDDEN) * 0.01).astype(np.float32),
        W2=(rng.standard_normal((HIDDEN, DIM)) * 0.05).astype(np.float32),
        b2=(rng.standard_normal(DIM) * 0.01).astype(np.float32),
        alpha=float(alpha),
    )


def synthetic_query(seed: int) -> F32Array:
    """Deterministic synthetic unit-norm 768-d query vector."""
    rng = np.random.default_rng(np.random.SeedSequence([0x9E3F, seed]))
    v = rng.standard_normal(DIM).astype(np.float32)
    return np.asarray(v / np.linalg.norm(v), dtype=np.float32)


def parity(a: F32Array, b: F32Array) -> tuple[float, float]:
    """Return ``(cosine, max_abs_diff)`` between two vectors, in float64."""
    a64, b64 = a.astype(np.float64), b.astype(np.float64)
    cos = float(a64 @ b64 / (np.linalg.norm(a64) * np.linalg.norm(b64) + 1e-300))
    return cos, float(np.max(np.abs(a64 - b64)))
