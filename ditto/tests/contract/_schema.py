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
    # The ticket hand-back. Added when ``failure_detail`` did: it is the first
    # field on this route whose shape (optional, defaulted, length-bounded) is
    # itself the backward-compatibility guarantee, and a golden is the only
    # thing that notices if one repo quietly drops the default.
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

# The miner-CLI request/response models that cross the same boundary, keyed by
# the module each lives in. Guarded for the same reason as the validator models
# and learned the same way: the platform added ``payment_required``,
# ``identical_agent_*``, ``payment_disposition``, ``credit_for_agent_id`` and
# ``allow_identical_rescore`` to the payment path, this repo's copies kept the
# older shape, and pydantic's default ``extra='ignore'`` dropped every one of
# them without raising. A miner was told an ordinary upload had succeeded when
# the platform had actually banked their payment as a reusable credit against a
# submission they already owned.
MINER_MODELS = {
    "ditto.api_models.upload": [
        "EvalPricingResponse",
        "UploadCheckRequest",
        "UploadCheckResponse",
        "UploadAgentResponse",
    ],
    "ditto.api_models.retrieval": [
        "AgentResponse",
        "AgentStatusResponse",
    ],
}

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


def compute_miner_contract(
    modules: dict[str, list[str]] = MINER_MODELS,
) -> dict[str, Any]:
    """Return the normalized structure of the miner-facing wire models.

    Same mechanism as :func:`compute_contract`, but the models span several
    modules, so entries are keyed ``"<module tail>.<model>"``.
    """
    contract: dict[str, Any] = {}
    for module, names in modules.items():
        mod = importlib.import_module(module)
        tail = module.rsplit(".", 1)[-1]
        for name in names:
            contract[f"{tail}.{name}"] = _strip(getattr(mod, name).model_json_schema())
    return contract
