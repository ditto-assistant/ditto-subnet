"""Structural contract of the worker-facing wire models.

The validator models under ``ditto/api_models/`` remain hand-maintained copies
of the platform's. This module reduces those models to their *structure* —
field names, types, required-ness —
dropping prose (``title`` / ``description`` / ``example(s)``) so a docstring
edit on one side does not look like a contract break, while a renamed, retyped,
added, or removed field does.

The committed ``validator_contract.json`` golden is generated from the
**platform** models (the source of truth). The validator contract test
recomputes the same structure from this repo's models and asserts equality, so
the worker client cannot silently drift from the API it calls. Regenerate it
with ``scripts/gen_validator_contract.py``.
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
    # The ticket hand-back. ditto-subnet added these to *its* copy of this list
    # when `failure_detail` landed (#282); this side never did, so the golden
    # ditto-subnet checks itself against was generated from ditto-subnet's own
    # models rather than from the platform -- a golden with no authority behind
    # it, which is the one thing this file exists to prevent. `failure_detail`
    # is precisely the field that needs it: its shape (optional, defaulted,
    # length-bounded) *is* the backward-compatibility guarantee, and the length
    # bound is a number the two repos must agree on exactly. Widening it 200 ->
    # 4096 on one side only is now a failing contract test instead of a silent
    # 422 in production against a mixed-version fleet.
    "FailJobRequest",
    "FailJobResponse",
]

CONFIRMATION_MODELS = [
    "ConfirmationExecutionProfile",
    "V9ConfirmationClaimRequest",
    "V9ConfirmationJobResponse",
    "V9ConfirmationPrepareRequest",
    "V9ConfirmationPreparedReport",
    "V9ConfirmationSubmitRequest",
    "V9ConfirmationSubmitResponse",
    "V9ConfirmationFailRequest",
    "V9ConfirmationFailResponse",
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


def compute_confirmation_contract() -> dict[str, Any]:
    """Return the exact private v9 confirmation transport contract."""
    return compute_contract(
        CONFIRMATION_MODELS, module="ditto.api_models.validator_confirmation"
    )
