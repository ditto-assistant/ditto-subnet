"""Validator-runner reference implementation.

Drives a Go harness OCI image over stdio against the public fixture set and
emits a JSON report compatible with the on-chain :class:`Score` schema. The
canonical Go scorer lives in ``go/bittensor/`` (open-source, sibling of
this package) and :mod:`ditto.bench.runner.scoring` is held in lockstep
via parity tests in ``ditto/tests/bench/test_scoring_*``.

Anti-gaming helpers (hidden split, paraphrase seed, memorisation discount,
distractor pool) are re-exported from :mod:`ditto.bench.runner.antigaming`.
"""

from __future__ import annotations

from ditto.bench.runner.antigaming import (
    CanaryIdenticalError,
    HiddenSet,
    distractor_bundle_for,
    ensure_paraphrase_changed,
    memorisation_discount,
    normalise_prompt_for_canary_check,
    paraphrase_seed,
    partition_fixture,
)
from ditto.bench.runner.scoring import (
    CoreScoreInputs,
    RetrievalScore,
    RetrievalScoreInputs,
    Score,
    ToolCallScore,
    latency_component,
    score_core,
    score_retrieval,
)

__all__ = [
    "CoreScoreInputs",
    "RetrievalScoreInputs",
    "Score",
    "ToolCallScore",
    "RetrievalScore",
    "latency_component",
    "score_core",
    "score_retrieval",
    "CanaryIdenticalError",
    "HiddenSet",
    "distractor_bundle_for",
    "ensure_paraphrase_changed",
    "memorisation_discount",
    "normalise_prompt_for_canary_check",
    "paraphrase_seed",
    "partition_fixture",
]
