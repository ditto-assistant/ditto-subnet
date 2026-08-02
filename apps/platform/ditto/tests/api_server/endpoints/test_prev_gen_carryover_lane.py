"""The cohort-lane gate on previous-generation carryover work.

Mirrors the existing ``_issue_source_backfill_ticket`` tests: the helper is
driven directly with mocks, because what is under test is the gate order, not
the allocator underneath it (which ``ditto/tests/db/queries`` covers against a
real database).

Requirement nine -- "the fresh lane is not diluted" -- has two halves. The half
that matters most is structural and is pinned in
``ditto/tests/db/queries/test_prev_gen_carryover.py``: a fresh-lane issuance
filters on ``Agent.created_at >= rollout.created_at``, so it can never select a
carryover agent even if one were named. The half pinned here is that the helper
itself refuses to run unless its own gates pass, and the endpoint only calls it
on a non-fresh lane slot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from ditto.api_models.queue_policy_settings import PrevGenCarryoverSettings
from ditto.api_server.endpoints.validator import (
    _issue_prev_gen_carryover_ticket,
    _prev_gen_carryover_precedes_desired_era,
)

_NOW = datetime.now(UTC)
_ENABLED = PrevGenCarryoverSettings(enabled=True)
_ADOPTED = [uuid4(), uuid4()]


@pytest.mark.parametrize(
    ("fresh_lane_due", "settings", "expected"),
    [
        (
            False,
            PrevGenCarryoverSettings(enabled=True, require_desired_era_drained=False),
            True,
        ),
        (
            True,
            PrevGenCarryoverSettings(enabled=True, require_desired_era_drained=False),
            False,
        ),
        (False, PrevGenCarryoverSettings(enabled=True), False),
        (
            False,
            PrevGenCarryoverSettings(require_desired_era_drained=False),
            False,
        ),
    ],
)
def test_only_explicitly_relaxed_cohort_slots_interleave_carryover_first(
    fresh_lane_due: bool,
    settings: PrevGenCarryoverSettings,
    expected: bool,
) -> None:
    assert (
        _prev_gen_carryover_precedes_desired_era(
            fresh_lane_due=fresh_lane_due, settings=settings
        )
        is expected
    )


def _rollout() -> MagicMock:
    return MagicMock(
        rollout_id=uuid4(), from_version=6, desired_version=7, cohort_size=10
    )


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    *,
    supports: bool = True,
    cohort_complete: bool = True,
    adopted: list | None = None,
    desired_era_outstanding: bool = False,
) -> tuple[AsyncMock, AsyncMock, MagicMock, AsyncMock]:
    issue = AsyncMock(return_value=MagicMock(name="ticket"))
    complete = AsyncMock(return_value=cohort_complete)
    ids = AsyncMock(return_value=_ADOPTED if adopted is None else adopted)
    outstanding = AsyncMock(return_value=desired_era_outstanding)
    supports_version = MagicMock(return_value=supports)
    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.heartbeat_supports_version",
        supports_version,
    )
    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.rollout_cohort_complete", complete
    )
    monkeypatch.setattr("ditto.api_server.endpoints.validator.carryover_agent_ids", ids)
    monkeypatch.setattr("ditto.api_server.endpoints.validator.issue_ticket", issue)
    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator.desired_era_work_outstanding", outstanding
    )
    monkeypatch.setattr(
        "ditto.api_server.endpoints.validator._desired_era_capable_hotkeys",
        AsyncMock(return_value={"5Validator"}),
    )
    return issue, complete, ids, outstanding


async def test_disabled_policy_issues_nothing_and_queries_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shipped default must not even look at the carryover table."""
    issue, complete, ids, _outstanding = _patch(monkeypatch)
    ticket = await _issue_prev_gen_carryover_ticket(
        AsyncMock(),
        rollout=_rollout(),
        heartbeat=MagicMock(),
        validator_hotkey="5Validator",
        now=_NOW,
        settings=PrevGenCarryoverSettings(),
        target_inference_ready=True,
        validator_running_benchmark=False,
        slot_id="slot-0",
    )
    assert ticket is None
    issue.assert_not_awaited()
    complete.assert_not_awaited()
    ids.assert_not_awaited()


@pytest.mark.parametrize(
    ("inference_ready", "heartbeat_present", "supports"),
    [(False, True, True), (True, False, True), (True, True, False)],
)
async def test_capability_gates_refuse_before_touching_the_queue(
    monkeypatch: pytest.MonkeyPatch,
    inference_ready: bool,
    heartbeat_present: bool,
    supports: bool,
) -> None:
    """A validator that cannot run the new era is never handed new-era work."""
    issue, _complete, ids, _outstanding = _patch(monkeypatch, supports=supports)
    ticket = await _issue_prev_gen_carryover_ticket(
        AsyncMock(),
        rollout=_rollout(),
        heartbeat=MagicMock() if heartbeat_present else None,
        validator_hotkey="5Validator",
        now=_NOW,
        settings=_ENABLED,
        target_inference_ready=inference_ready,
        validator_running_benchmark=False,
        slot_id="slot-0",
    )
    assert ticket is None
    issue.assert_not_awaited()
    ids.assert_not_awaited()


async def test_waits_for_the_inherited_cohort_then_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default policy: spare capacity only, on the source-backfill precedent."""
    rollout = _rollout()
    issue, complete, _ids, _outstanding = _patch(monkeypatch, cohort_complete=False)

    blocked = await _issue_prev_gen_carryover_ticket(
        AsyncMock(),
        rollout=rollout,
        heartbeat=MagicMock(),
        validator_hotkey="5Validator",
        now=_NOW,
        settings=_ENABLED,
        target_inference_ready=True,
        validator_running_benchmark=False,
        slot_id="slot-0",
    )
    assert blocked is None
    issue.assert_not_awaited()

    complete.return_value = True
    ticket = await _issue_prev_gen_carryover_ticket(
        AsyncMock(),
        rollout=rollout,
        heartbeat=MagicMock(),
        validator_hotkey="5Validator",
        now=_NOW,
        settings=_ENABLED,
        target_inference_ready=True,
        validator_running_benchmark=False,
        slot_id="slot-0",
    )
    assert ticket is not None
    assert issue.await_args is not None
    kwargs = issue.await_args.kwargs
    assert kwargs["bench_version"] == 7
    assert kwargs["artifact_mode"] == "screened_only"
    assert kwargs["only_agent_ids"] == _ADOPTED
    # The arrival filter is deliberately absent: it is the one thing that makes
    # every other desired-version path unable to reach a carryover agent.
    assert "submitted_at_or_after" not in kwargs


async def test_interleaving_policy_does_not_wait_for_the_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issue, complete, _ids, _outstanding = _patch(monkeypatch, cohort_complete=False)
    ticket = await _issue_prev_gen_carryover_ticket(
        AsyncMock(),
        rollout=_rollout(),
        heartbeat=MagicMock(),
        validator_hotkey="5Validator",
        now=_NOW,
        settings=PrevGenCarryoverSettings(enabled=True, require_cohort_complete=False),
        target_inference_ready=True,
        validator_running_benchmark=False,
        slot_id="slot-0",
    )
    assert ticket is not None
    complete.assert_not_awaited()


async def test_no_adopted_agents_issues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An enabled policy with an empty carryover table is still a no-op."""
    issue, _complete, _ids, _outstanding = _patch(monkeypatch, adopted=[])
    ticket = await _issue_prev_gen_carryover_ticket(
        AsyncMock(),
        rollout=_rollout(),
        heartbeat=MagicMock(),
        validator_hotkey="5Validator",
        now=_NOW,
        settings=_ENABLED,
        target_inference_ready=True,
        validator_running_benchmark=False,
        slot_id="slot-0",
    )
    assert ticket is None
    issue.assert_not_awaited()


async def test_strict_priority_blocks_while_the_new_era_still_has_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lane is last, not proportional: any leasable new-era row wins.

    Reaching this helper only proves that THIS validator's desired-era lanes
    came back empty, which they do constantly while the queue is deep. The
    fleet-wide question is the one that decides.
    """
    issue, _complete, ids, outstanding = _patch(
        monkeypatch, desired_era_outstanding=True
    )
    ticket = await _issue_prev_gen_carryover_ticket(
        AsyncMock(),
        rollout=_rollout(),
        heartbeat=MagicMock(),
        validator_hotkey="5Validator",
        now=_NOW,
        settings=_ENABLED,
        target_inference_ready=True,
        validator_running_benchmark=False,
        slot_id="slot-0",
    )
    assert ticket is None
    issue.assert_not_awaited()
    ids.assert_not_awaited()
    outstanding.assert_awaited()


async def test_operator_can_relax_strict_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The knob lives on the #429 board, so the gate is not hardcoded."""
    issue, _complete, _ids, outstanding = _patch(
        monkeypatch, desired_era_outstanding=True
    )
    ticket = await _issue_prev_gen_carryover_ticket(
        AsyncMock(),
        rollout=_rollout(),
        heartbeat=MagicMock(),
        validator_hotkey="5Validator",
        now=_NOW,
        settings=PrevGenCarryoverSettings(
            enabled=True, require_desired_era_drained=False
        ),
        target_inference_ready=True,
        validator_running_benchmark=False,
        slot_id="slot-0",
    )
    assert ticket is not None
    issue.assert_awaited()
    outstanding.assert_not_awaited()
