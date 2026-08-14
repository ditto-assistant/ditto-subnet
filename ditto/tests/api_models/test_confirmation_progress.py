from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from ditto.api_models.confirmation_progress import (
    ConfirmationProgress,
    confirmation_progress_signing_token,
)

_BUNDLE = UUID("11111111-2222-4333-8444-555555555555")
_TICKET = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
_AGENT = UUID("99999999-8888-4777-8666-555555555555")
_DEADLINE = datetime(2030, 1, 1, tzinfo=UTC)


def _progress(slot_id: str = "longmem-0", **updates: object) -> ConfirmationProgress:
    values: dict[str, object] = {
        "bundle_id": _BUNDLE,
        "ticket_id": _TICKET,
        "agent_id": _AGENT,
        "slot_id": slot_id,
        "stage": "running_confirmation",
        "completed": 17,
        "total": 500,
        "ticket_deadline": _DEADLINE,
    }
    values.update(updates)
    return ConfirmationProgress.model_validate(values)


def test_confirmation_progress_token_is_slot_ordered_and_privacy_safe() -> None:
    later = _progress(
        "longmem-1",
        ticket_id=UUID("bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
        completed=3,
    )
    token = confirmation_progress_signing_token([later, _progress()])

    assert token.split(";")[0].startswith("longmem-0,")
    assert token.split(";")[1].startswith("longmem-1,")
    assert "running_confirmation,17,500" in token
    assert "score" not in token
    assert "prompt" not in token
    assert "response" not in token


@pytest.mark.parametrize(
    "updates",
    [
        {"completed": 1, "total": None},
        {"completed": 501, "total": 500},
        {"stage": "finalizing", "completed": 499, "total": 500},
        {"ticket_deadline": datetime(2030, 1, 1)},
        {"slot_id": "slot-0"},
    ],
)
def test_confirmation_progress_rejects_inconsistent_or_ambiguous_work(
    updates: dict[str, object],
) -> None:
    values = _progress().model_dump()
    values.update(updates)
    with pytest.raises(ValidationError):
        ConfirmationProgress.model_validate(values)


def test_confirmation_progress_normalizes_deadline_and_accepts_terminal_total() -> None:
    progress = _progress(
        stage="submitting_result",
        completed=500,
        total=500,
        ticket_deadline=_DEADLINE + timedelta(hours=5),
    )
    assert progress.completed == progress.total == 500
    assert progress.ticket_deadline.tzinfo == UTC


def test_confirmation_progress_token_rejects_duplicate_slots() -> None:
    with pytest.raises(ValueError, match="duplicate slots"):
        confirmation_progress_signing_token([_progress(), _progress()])
