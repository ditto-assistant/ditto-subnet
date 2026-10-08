"""Bounded completed-read evidence for logs and authenticated Backroom reads."""

import logging
from datetime import UTC, datetime
from socket import gaierror
from typing import Any, Literal

from ditto.api_models.treasury_readiness import TreasuryChainReadDiagnostic
from ditto.chain.errors import ChainConnectionError, ChainTimeoutError, TreasuryReadStep

logger = logging.getLogger(__name__)


def treasury_read_failure_kind(
    cause: BaseException,
) -> Literal["timeout", "connection", "invalid_evidence", "unavailable"]:
    if isinstance(cause, (ChainTimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(cause, (ChainConnectionError, ConnectionError, gaierror)):
        return "connection"
    if isinstance(cause, ValueError):
        return "invalid_evidence"
    return "unavailable"


def record_treasury_read(
    state: Any,
    operation: Literal["epoch_schedule", "requester_activation"],
    *,
    elapsed: float,
    failure_kind: Literal["timeout", "connection", "invalid_evidence", "unavailable"]
    | None = None,
    failure_stage: Literal["epoch_schedule", "identity", "setter_roster"] | None = None,
    failure_step: TreasuryReadStep | None = None,
) -> None:
    # The middleware package imports endpoint routers. Resolve its context only
    # at call time so a producer can import this recorder without a router cycle.
    from ditto.api_server.middleware.request_id import request_id_var

    # This synchronous update cannot interleave with another asyncio task. Two
    # fixed operation keys bound retention; no provider text or request body.
    reads = getattr(state, "treasury_chain_read_diagnostics", None)
    if reads is None:
        reads = {}
        if state is not None:
            state.treasury_chain_read_diagnostics = reads
    previous = reads.get(operation)
    now = datetime.now(UTC)
    values = previous.model_dump() if previous is not None else {}
    values.update(
        operation=operation,
        attempts=values.get("attempts", 0) + 1,
        successes=values.get("successes", 0) + int(failure_kind is None),
        failures=values.get("failures", 0) + int(failure_kind is not None),
        last_finished_at=now,
        last_elapsed_seconds=max(0.0, elapsed),
    )
    if failure_kind is not None:
        values.update(
            last_failure_at=now,
            last_failure_elapsed_seconds=max(0.0, elapsed),
            last_failure_stage=failure_stage,
            last_failure_step=failure_step,
            last_failure_kind=failure_kind,
            last_failure_request_id=request_id_var.get(),
        )
    reads[operation] = TreasuryChainReadDiagnostic.model_validate(values)
    log = logger.warning if failure_kind is not None else logger.info
    log(
        "treasury_chain_read operation=%s outcome=%s stage=%s step=%s "
        "kind=%s elapsed_seconds=%.3f",
        operation,
        "failed" if failure_kind is not None else "success",
        failure_stage,
        failure_step,
        failure_kind,
        elapsed,
    )
