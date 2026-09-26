"""Fail-closed agent-attributable exhaustion classification."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from ditto.api_models.agent_status import AgentStatus
from ditto.api_models.retry_state import RetryDisposition, RetryState
from ditto.api_models.screener import SCREENING_POLICY_VERSION
from ditto.api_models.ticket_status import TicketStatus
from ditto.db.models import Agent, ValidatorTicket
from ditto.db.queries.retry_state import (
    AGENT_ATTRIBUTABLE_WITHDRAW_REASON,
    agreed_failure_detail,
    dominant_agent_failure_detail,
    is_agent_attributable_exhaustion,
    recommended_retry_action,
    recovery_gate,
    retry_disposition,
)
from ditto.db.queries.tickets import MAX_ATTEMPTS_PER_VERSION

_NOW = datetime(2026, 8, 21, 15, tzinfo=UTC)
_ISSUED = _NOW - timedelta(hours=2)
_FAILED = _NOW - timedelta(hours=1)
_AGENT_ID = UUID("90cb5697-cbc1-40f4-a27e-439a7986a054")


def _ticket(**overrides: object) -> ValidatorTicket:
    values: dict[str, object] = {
        "agent_id": _AGENT_ID,
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
    return ValidatorTicket(**values)


def _agent() -> Agent:
    return Agent(
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


def test_dominant_code_requires_complete_attributable_exhaustion() -> None:
    tickets = [
        _ticket(validator_hotkey="validator-0"),
        _ticket(validator_hotkey="validator-1"),
        _ticket(
            validator_hotkey="validator-2",
            status=TicketStatus.ISSUED,
            failure_detail=None,
            failed_at=None,
        ),
    ]
    assert dominant_agent_failure_detail(scores=[], tickets=tickets) is None


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


def _disposition(
    tickets: list[ValidatorTicket], *, state: RetryState = "exhausted"
) -> RetryDisposition | None:
    """The disposition a bulk classification would publish for these tickets."""
    _, allowed, _, _ = recovery_gate(
        agent=_agent(),
        scores=[],
        tickets=tickets,
        now=_NOW,
        bench_version=11,
    )
    return retry_disposition(
        state=state,
        scores=[],
        tickets=tickets,
        recovery_allowed=allowed,
    )


def test_named_agent_failures_read_as_a_terminal_artifact_failure() -> None:
    tickets = [_ticket(validator_hotkey=f"validator-{index}") for index in range(3)]
    assert _disposition(tickets) == "terminal_artifact_failure"


def test_infrastructure_exhaustion_reads_as_an_operator_hold() -> None:
    tickets = [
        _ticket(
            validator_hotkey=f"validator-{index}",
            failure_detail="provider_outage_parked",
            failure_reason="infrastructure",
        )
        for index in range(3)
    ]
    assert _disposition(tickets) == "operator_hold"


def test_a_mixed_remaining_set_never_blames_the_submission() -> None:
    tickets = [
        _ticket(validator_hotkey="validator-0"),
        _ticket(
            validator_hotkey="validator-1",
            failure_detail="invalid screened image archive: Docker manifest",
        ),
        _ticket(validator_hotkey="validator-2"),
    ]
    assert _disposition(tickets) == "operator_hold"


def test_missing_and_stale_details_never_blame_the_submission() -> None:
    missing = [
        _ticket(validator_hotkey="validator-0", failure_detail=None, failed_at=None),
        _ticket(validator_hotkey="validator-1"),
        _ticket(validator_hotkey="validator-2"),
    ]
    stale = [
        _ticket(
            validator_hotkey=f"validator-{index}",
            failed_at=_ISSUED - timedelta(minutes=1),
        )
        for index in range(3)
    ]
    assert _disposition(missing) == "operator_hold"
    assert _disposition(stale) == "operator_hold"


def test_an_unnameable_next_step_still_reads_as_an_operator_hold() -> None:
    """An exhausted row the gate will not act on is a hold, not a miner failure.

    The gate declines to recommend anything for a submission that is no longer
    waiting for scores, so ``recommended_retry_action`` is ``None`` even though
    the leases really did exhaust. Declining to name a remedy is not evidence
    about the artifact, and the miner-facing reading has to stay on the safe
    side of that silence.
    """
    agent = _agent()
    agent.status = AgentStatus.SCORED
    tickets = [
        _ticket(
            validator_hotkey=f"validator-{index}",
            failure_detail="provider_outage_parked",
            failure_reason="infrastructure",
        )
        for index in range(3)
    ]
    _, allowed, reason, _ = recovery_gate(
        agent=agent,
        scores=[],
        tickets=tickets,
        now=_NOW,
        bench_version=11,
    )
    assert allowed is False
    assert reason == "submission is not waiting for validator scores"
    assert (
        recommended_retry_action(scores=[], tickets=tickets, recovery_allowed=allowed)
        is None
    )
    assert (
        retry_disposition(
            state="exhausted",
            scores=[],
            tickets=tickets,
            recovery_allowed=allowed,
        )
        == "operator_hold"
    )


def test_two_different_named_codes_do_not_publish_a_terminal_failure() -> None:
    """A withdraw verdict the public surface cannot name stays a hold.

    ``is_agent_attributable_exhaustion`` is satisfied by any mix of named agent
    codes, but a terminal row has to show the code it is telling the miner to
    fix. When the remaining slots name different ones there is nothing to show,
    so the public reading falls back rather than asserting a failure it cannot
    attribute. The operator verdict is unchanged.
    """
    tickets = [
        _ticket(
            validator_hotkey="validator-0", failure_detail="inference_request_rejected"
        ),
        _ticket(
            validator_hotkey="validator-1",
            failure_detail="inference_allowance_exhausted",
        ),
        _ticket(
            validator_hotkey="validator-2", failure_detail="model_inference_required"
        ),
    ]
    assert is_agent_attributable_exhaustion(scores=[], tickets=tickets) is True
    assert (
        recommended_retry_action(scores=[], tickets=tickets, recovery_allowed=False)
        == "withdraw"
    )
    assert dominant_agent_failure_detail(scores=[], tickets=tickets) is None
    assert _disposition(tickets) == "operator_hold"


def test_an_agreed_cause_is_reported_for_a_hold() -> None:
    """A hold every slot agrees on can be attributed; a mixed one cannot."""
    agreed = [
        _ticket(
            validator_hotkey=f"validator-{index}",
            failure_detail="provider_outage_parked",
            failure_reason="infrastructure",
        )
        for index in range(3)
    ]
    assert agreed_failure_detail(scores=[], tickets=agreed) == "provider_outage_parked"

    mixed = [
        _ticket(
            validator_hotkey="validator-0",
            failure_detail="provider_outage_parked",
            failure_reason="infrastructure",
        ),
        _ticket(
            validator_hotkey="validator-1",
            failure_detail=(
                "DittobenchError: run deadbeef did not finish within 6600.0s"
            ),
            failure_reason="infrastructure",
        ),
        _ticket(
            validator_hotkey="validator-2",
            failure_detail=None,
            failed_at=None,
        ),
    ]
    assert agreed_failure_detail(scores=[], tickets=mixed) is None
    assert _disposition(mixed) == "operator_hold"


def test_an_advancing_row_has_no_disposition() -> None:
    """Only a parked row carries one; a live or cooling lease is still moving."""
    tickets = [_ticket(validator_hotkey=f"validator-{index}") for index in range(3)]
    for state in ("running", "retry_available", "cooling_down", "queued"):
        assert _disposition(tickets, state=state) is None
