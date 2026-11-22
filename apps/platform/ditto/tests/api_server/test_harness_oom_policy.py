"""The SN-488 effective time for charging a proven harness OOM."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ditto.api_server.harness_oom_policy import (
    HARNESS_OOM_CHARGE_EFFECTIVE_AT,
    HARNESS_OOM_GRACE_FAILURE_DETAIL,
    harness_oom_charged,
)
from ditto.db.queries.retry_state import AGENT_ATTRIBUTABLE_FAILURE_DETAIL_VALUES


def test_effective_time_is_noon_wednesday_in_toronto() -> None:
    assert (
        datetime(2026, 11, 25, 12, 0, tzinfo=ZoneInfo("America/Toronto"))
        == HARNESS_OOM_CHARGE_EFFECTIVE_AT
    )
    assert HARNESS_OOM_CHARGE_EFFECTIVE_AT.weekday() == 2


def test_charge_is_keyed_on_the_lease_issue_time() -> None:
    at = HARNESS_OOM_CHARGE_EFFECTIVE_AT
    assert not harness_oom_charged(at - timedelta(microseconds=1))
    assert harness_oom_charged(at)
    assert harness_oom_charged(at + timedelta(days=1))
    # A naive database timestamp is UTC.
    assert harness_oom_charged(at.astimezone(UTC).replace(tzinfo=None))
    assert not harness_oom_charged(datetime(2026, 11, 22, 18, 34))


def test_grace_detail_is_never_agent_attributable() -> None:
    assert HARNESS_OOM_GRACE_FAILURE_DETAIL not in (
        AGENT_ATTRIBUTABLE_FAILURE_DETAIL_VALUES
    )
