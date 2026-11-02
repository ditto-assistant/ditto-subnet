"""Controller lease boundaries shared by the watchdog and GCP claim gate."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ditto.db.models import ScreenerCapacitySnapshot
from ditto.db.queries.screener_capacity import screener_fallback_active

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def test_missing_controller_activates_fallback() -> None:
    assert screener_fallback_active(None, NOW) == (True, "controller_missing")


@pytest.mark.parametrize("naive", [False, True])
@pytest.mark.parametrize(
    ("remaining_seconds", "ready", "expected"),
    [
        (-1, True, (True, "controller_stale")),
        (0, True, (True, "controller_stale")),
        (0, False, (True, "controller_stale")),
        (1, False, (True, "provider_not_ready")),
        (1, True, (False, "controller_fresh")),
    ],
)
def test_controller_lease_boundary(
    naive: bool, remaining_seconds: int, ready: bool, expected: tuple[bool, str]
) -> None:
    expiry = NOW + timedelta(seconds=remaining_seconds)
    if naive:
        expiry = expiry.replace(tzinfo=None)
    snapshot = ScreenerCapacitySnapshot(
        controller_lease_expires_at=expiry, provider_ready=ready
    )
    assert screener_fallback_active(snapshot, NOW) == expected


def test_aware_expiry_uses_absolute_time() -> None:
    snapshot = ScreenerCapacitySnapshot(
        controller_lease_expires_at=NOW.astimezone(timezone(timedelta(hours=3))),
        provider_ready=True,
    )
    assert screener_fallback_active(snapshot, NOW) == (True, "controller_stale")
