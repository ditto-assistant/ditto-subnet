"""Structural contract of the validator wire models.

There is no shared package between ``ditto-subnet`` and ``ditto-platform``: the
``ditto/api_models/validator.py`` here is a hand-maintained copy of the
platform's, and the **platform's OpenAPI schema is the contract**. This module
reduces those models to their *structure* — field names, types, required-ness —
dropping prose (``title`` / ``description`` / ``example(s)``) so a docstring
edit on one side does not look like a contract break, while a renamed, retyped,
added, or removed field does.

The committed golden (``validator_contract.json``) is generated from the
**platform** models (the source of truth). :mod:`test_validator_contract`
recomputes the same structure from *this* repo's models and asserts equality, so
the validator client cannot silently drift from the API it calls. Regenerate the
golden with ``scripts/gen_validator_contract.py`` (see that file's header for
how to point it at a ditto-platform checkout).
"""

from __future__ import annotations

from typing import Any

# The validator request/response models that cross the platform <-> validator
# HTTP boundary. Both repos must keep their copies structurally identical.
SHARED_MODELS = [
    "ValidatorQueueItem",
    "ValidatorQueueResponse",
    "ArtifactResponse",
    "CaseScore",
    "ScoreReport",
    "SubmitScoreRequest",
    "SubmitScoreResponse",
]

# Cosmetic JSON-Schema keys that carry prose/illustration, not structure.
_STRIP_KEYS = {"title", "description", "examples", "example"}


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip(v) for k, v in sorted(node.items()) if k not in _STRIP_KEYS}
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node


def compute_contract() -> dict[str, Any]:
    """Return the normalized structural schema for each shared validator model.

    Imports ``ditto.api_models.validator`` from whichever repo this runs in, so
    the same function generates the golden (run inside ditto-platform) and
    checks against it (run inside ditto-subnet).
    """
    from ditto.api_models import validator as v

    return {
        name: _strip(getattr(v, name).model_json_schema()) for name in SHARED_MODELS
    }
