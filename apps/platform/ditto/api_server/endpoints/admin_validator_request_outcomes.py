"""Backroom read of how each validator's job polls and ledger reads ended.

The Prometheus decline counter has no hotkey label by design. This read gives
the per-validator view an operator needs when one validator sits idle (was it
told 428 stale heartbeat, 204 allocator_busy, slot_not_healthy, or did it stop
polling?) or skips setting weights (did its scoring-ledger read get a 428 for
treasury fleet readiness?). Read-only; nothing here participates in dispatch.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ditto.api_models.validator_request_outcomes import (
    ValidatorRequestOutcomeSummary,
)
from ditto.api_server import validator_request_outcomes
from ditto.api_server.endpoints.admin_quarantine import require_admin

router = APIRouter(tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]


@router.get(
    "/admin/validator-request-outcomes",
    response_model=ValidatorRequestOutcomeSummary,
)
async def get_validator_request_outcomes(
    _admin: AdminDep,
    validator_hotkey: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> ValidatorRequestOutcomeSummary:
    """Per-validator request outcomes since this API process started."""
    return ValidatorRequestOutcomeSummary.model_validate(
        validator_request_outcomes.REQUEST_OUTCOMES.snapshot(validator_hotkey)
    )
