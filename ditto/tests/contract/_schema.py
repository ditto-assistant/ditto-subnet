"""Structural contract of the worker-facing wire models.

The validator models under ``ditto/api_models/`` remain hand-maintained copies
of the platform's. This module reduces those models to their *structure* —
field names, types, required-ness —
dropping prose (``title`` / ``description`` / ``example(s)``) so a docstring
edit on one side does not look like a contract break, while a renamed, retyped,
added, or removed field does.

The committed goldens (``validator_contract.json``, ``screener_contract.json``)
are generated from the **platform** models (the source of truth). The
``test_*_contract`` modules recompute the same structure from *this* repo's
models and assert equality, so a worker client cannot silently drift from the
API it calls. Regenerate a golden with the matching
``scripts/gen_*_contract.py`` (see those files' headers for how to point them
at a ditto-platform checkout).
"""

from __future__ import annotations

import importlib
from typing import Any

# The validator request/response models that cross the platform <-> validator
# HTTP boundary. Both repos must keep their copies structurally identical.
SHARED_MODELS = [
    "ArtifactResponse",
    "CaseScore",
    "ScoreReport",
    "SubmitScoreRequest",
    "SubmitScoreResponse",
    "ValidatorHeartbeatRequest",
    "ValidatorHeartbeatResponse",
    "LedgerEntry",
    "LedgerResponse",
]

SHARED_SCREENER_HEARTBEAT_MODELS = [
    "ScreenerHeartbeatRequest",
    "ScreenerHeartbeatResponse",
]

# Cosmetic JSON-Schema keys that carry prose/illustration, not structure.
_STRIP_KEYS = {"title", "description", "examples", "example"}


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in sorted(node.items()) if k not in _STRIP_KEYS}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def compute_contract(
    models: list[str] = SHARED_MODELS, module: str = "ditto.api_models.validator"
) -> dict[str, Any]:
    """Return the normalized structural schema for each shared wire model.

    Imports ``module`` from whichever repo this runs in, so the same function
    generates a golden (run inside ditto-platform) and checks against it (run
    inside ditto-subnet). Defaults preserve the original validator contract.
    """
    mod = importlib.import_module(module)

    return {name: _strip(getattr(mod, name).model_json_schema()) for name in models}
