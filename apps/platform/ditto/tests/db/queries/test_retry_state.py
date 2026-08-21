"""Fail-closed agent-attributable exhaustion classification."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from ditto.api_models.ticket_status import TicketStatus
from ditto.db.queries.retry_state import (
    AGENT_ATTRIBUTABLE_WITHDRAW_REASON,
    is_agent_attributable_exhaustion,
    recommended_retry_action,
    recovery_gate,
)
from ditto.db.queries.tickets import MAX_ATTEMPTS_PER_VERSION

_NOW = datetime(2026, 8, 21, 15, tzinfo=UTC)
_ISSUED = _NOW - timedelta(hours=2)
_FAILED = _NOW - timedelta(hours=1)


def _ticket(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "status": TicketStatus.EXPIRED,
        "validator_hotkey": "validator-0",
        "attempt_count": MAX_ATTEMPTS_PER_VERSION,
        "manual_retry_grants": 0,
        "infra_retry_grants": 0,
        "issued_at": _ISSUED,
        "deadline": _FAILED,
        "failed_at": _FAILED,
        "failure_detail": "inference_request_rejected",
        "failure_reason": "scoring_error",
        "retry_after": _NOW - timedelta(minutes=1),
        "bench_version": 11,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _agent() -> SimpleNamespace:
    from ditto.api_models.agent_status import AgentStatus
    from ditto.api_models.screener import SCREENING_POLICY_VERSION

    return SimpleNamespace(
        status=AgentStatus.EVALUATING,
        screening_policy_version=SCREENING_POLICY_VERSION,
    )


def test_named_agent_failures_are_withdraw_not_retry() -> None:
    tickets = [_ticket(validator_hotkey=f"validator-{index}") for index in range(3)]
    assert is_agent_attributable_exhaustion(scores=[], tickets=tickets) is True
    assert (
        recommended_retry_action(scores=[], tickets=tickets, recovery_allowed=False)
        == "withdraw"
    )
    automatic, allowed, reason, selected = recovery_gate(
        agent=_agent(),
        scores=[],
        tickets=tickets,
        now=_NOW,
        bench_version=11,
    )
    assert automatic is False
    assert allowed is False
    assert reason == AGENT_ATTRIBUTABLE_WITHDRAW_REASON
    assert selected == []


def test_missing_detail_stays_on_the_retry_path() -> None:
    tickets = [
        _ticket(validator_hotkey="validator-0", failure_detail=None, failed_at=None),
        _ticket(validator_hotkey="validator-1"),
        _ticket(validator_hotkey="validator-2"),
    ]
    assert is_agent_attributable_exhaustion(scores=[], tickets=tickets) is False
    _, allowed, reason, selected = recovery_gate(
        agent=_agent(),
        scores=[],
        tickets=tickets,
        now=_NOW,
        bench_version=11,
    )
    assert allowed is True
    assert reason is None
    assert len(selected) == 3


def test_timeout_prose_is_not_agent_attributable() -> None:
    tickets = [
        _ticket(
            validator_hotkey=f"validator-{index}",
            failure_detail=(
                "DittobenchError: run deadbeef did not finish within 6600.0s"
            ),
        )
        for index in range(3)
    ]
    assert is_agent_attributable_exhaustion(scores=[], tickets=tickets) is False
    _, allowed, _, _ = recovery_gate(
        agent=_agent(),
        scores=[],
        tickets=tickets,
        now=_NOW,
        bench_version=11,
    )
    assert allowed is True


def test_stale_failure_detail_does_not_classify_the_current_lease() -> None:
    tickets = [
        _ticket(
            validator_hotkey=f"validator-{index}",
            failed_at=_ISSUED - timedelta(minutes=1),
        )
        for index in range(3)
    ]
    assert is_agent_attributable_exhaustion(scores=[], tickets=tickets) is False


def test_mixed_remaining_set_stays_retryable() -> None:
    tickets = [
        _ticket(validator_hotkey="validator-0"),
        _ticket(
            validator_hotkey="validator-1",
            failure_detail="invalid screened image archive: Docker manifest",
        ),
        _ticket(validator_hotkey="validator-2"),
    ]
    assert is_agent_attributable_exhaustion(scores=[], tickets=tickets) is False
    _, allowed, _, _ = recovery_gate(
        agent=_agent(),
        scores=[],
        tickets=tickets,
        now=_NOW,
        bench_version=11,
    )
    assert allowed is True
