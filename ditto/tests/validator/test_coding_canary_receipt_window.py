"""The validator never starts a receipt submission after Platform's window."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ditto.api_models.coding_certification_leases import (
    CODING_CERTIFICATION_RECEIPT_GRACE_SECONDS,
)
from ditto.tests.validator.test_coding_canary import (
    _AGENT,
    _NOW,
    _lease,
    _Platform,
    _Runtime,
)
from ditto.validator.coding_canary import (
    RECEIPT_SUBMISSION_ALLOWANCE_SECONDS,
    CodingCanaryWorker,
    coding_certification_receipt_submission_open,
)
from ditto.validator.errors import ValidatorInfrastructureError

_DEADLINE = datetime(2026, 9, 15, 12, tzinfo=UTC)
_CLOSES = _DEADLINE + timedelta(
    seconds=CODING_CERTIFICATION_RECEIPT_GRACE_SECONDS
    - RECEIPT_SUBMISSION_ALLOWANCE_SECONDS
)


def test_receipt_submission_closes_before_platform_window() -> None:
    assert (_CLOSES - _DEADLINE).total_seconds() == 105

    def open_at(now: datetime) -> bool:
        return coding_certification_receipt_submission_open(deadline=_DEADLINE, now=now)

    assert open_at(_DEADLINE - timedelta(minutes=1)) is True
    assert open_at(_DEADLINE + timedelta(seconds=30)) is True
    assert open_at(_CLOSES - timedelta(microseconds=1)) is True
    assert open_at(_CLOSES) is False
    assert open_at(_DEADLINE + timedelta(minutes=5)) is False
    with pytest.raises(ValidatorInfrastructureError):
        coding_certification_receipt_submission_open(
            deadline=_DEADLINE, now=datetime(2026, 9, 15, 12)
        )


@pytest.mark.asyncio
async def test_canary_worker_does_not_submit_after_the_receipt_window() -> None:
    platform = _Platform()
    runtime = _Runtime()
    deadline = _lease().authority.deadline
    times = iter(
        [_NOW, deadline + timedelta(seconds=CODING_CERTIFICATION_RECEIPT_GRACE_SECONDS)]
    )
    worker = CodingCanaryWorker(
        platform=platform,
        runtime=runtime,
        sign_receipt=lambda _lease, _receipt: "ab" * 64,
        clock=lambda: next(times),
    )
    worker.offer(_AGENT, 12)
    with pytest.raises(ValidatorInfrastructureError, match="receipt window"):
        await worker.run_once()
    # The run happened and its grant was revoked; only the late submit is withheld.
    assert len(runtime.certified) == 1
    assert platform.revokes == 1
    assert platform.submits == 0
