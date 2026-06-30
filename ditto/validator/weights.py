"""Map per-miner composite scores to a chain weight vector.

WIP / subnet-economics decision: this is a placeholder reward curve — weight
proportional to composite. Pylon normalizes the vector, so absolute magnitudes
don't matter, only the ratios. The real curve (winner-take-most, burn floor,
exponential emphasis, EMA across epochs to resist single-run variance) is a
tokenomics decision for the team; it lives here so it's a one-function change.
"""

from __future__ import annotations


def compute_weights(scores: dict[str, float]) -> dict[str, float]:
    """Return ``{miner_hotkey: weight}`` from ``{miner_hotkey: composite}``.

    Drops non-positive composites (a zero-scoring miner earns nothing) and
    passes the rest through proportionally. Returns an empty dict when no miner
    scored above zero, in which case the caller should skip ``put_weights``.
    """
    return {hotkey: c for hotkey, c in scores.items() if c > 0.0}
