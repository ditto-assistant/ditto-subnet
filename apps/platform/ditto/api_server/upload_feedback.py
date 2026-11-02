"""Upload guidance calculated using Platform's clock."""

from datetime import UTC, datetime
from math import ceil


def submission_cooldown_message(
    retry_at: datetime, *, now: datetime | None = None
) -> str:
    """Keep the retry timestamp and round positive waits up to a minute."""
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    total_minutes = ceil(max(0, (retry_at - current).total_seconds()) / 60)
    hours, minutes = divmod(total_minutes, 60)
    hour_unit = "hour" if hours == 1 else "hours"
    minute_unit = "minute" if minutes == 1 else "minutes"
    return (
        f"owner coldkey may submit again at {retry_at.isoformat()}. "
        f"Please try again in {hours} {hour_unit} and {minutes} {minute_unit}."
    )
