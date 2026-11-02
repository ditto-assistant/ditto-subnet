"""Upload retry guidance follows the server clock and minute boundaries."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ditto.api_server.upload_feedback import submission_cooldown_message


@pytest.mark.parametrize(
    ("seconds", "wait"),
    [
        (-1, "0 hours and 0 minutes"),
        (0, "0 hours and 0 minutes"),
        (1, "0 hours and 1 minute"),
        (60, "0 hours and 1 minute"),
        (61, "0 hours and 2 minutes"),
        (3599, "1 hour and 0 minutes"),
        (3600, "1 hour and 0 minutes"),
        (3601, "1 hour and 1 minute"),
        (9000, "2 hours and 30 minutes"),
        (86399, "24 hours and 0 minutes"),
    ],
)
def test_countdown_keeps_timestamp_and_rounds_up(seconds: int, wait: str) -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    retry_at = now + timedelta(seconds=seconds)

    message = submission_cooldown_message(retry_at, now=now)

    assert retry_at.isoformat() in message
    assert message.endswith(f"Please try again in {wait}.")


@pytest.mark.parametrize("naive", [False, True])
def test_countdown_uses_the_same_instant_across_time_zones(naive: bool) -> None:
    now = datetime(2026, 9, 28, 12, tzinfo=UTC)
    retry_at = datetime(2026, 9, 28, 16, tzinfo=timezone(timedelta(hours=3)))
    if naive:
        now = now.replace(tzinfo=None)
        retry_at = retry_at.astimezone(UTC).replace(tzinfo=None)

    assert submission_cooldown_message(retry_at, now=now).endswith(
        "Please try again in 1 hour and 0 minutes."
    )
