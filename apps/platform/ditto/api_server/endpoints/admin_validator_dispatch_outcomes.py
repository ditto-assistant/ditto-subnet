"""Backroom read of how each validator's job polls ended.

The Prometheus decline counter has no hotkey label by design. This read gives
the per-validator view an operator needs when one validator sits idle: was it
told 428 (stale heartbeat), 204 allocator_busy, slot_not_healthy, or did it
stop polling? Read-only; nothing here participates in dispatch.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from ditto.api_models.validator_dispatch_outcomes import (
    ValidatorDispatchOutcomeSummary,
)
from ditto.api_server import dispatch_outcomes
from ditto.api_server.endpoints.admin_quarantine import require_admin

router = APIRouter(tags=["admin"])
AdminDep = Annotated[None, Depends(require_admin)]


@router.get(
    "/admin/validator-dispatch-outcomes",
    response_model=ValidatorDispatchOutcomeSummary,
)
async def get_validator_dispatch_outcomes(
    _admin: AdminDep,
    validator_hotkey: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
) -> ValidatorDispatchOutcomeSummary:
    """Per-validator job-poll outcomes since this API process started."""
    return ValidatorDispatchOutcomeSummary.model_validate(
        dispatch_outcomes.DISPATCH_OUTCOMES.snapshot(validator_hotkey)
    )
