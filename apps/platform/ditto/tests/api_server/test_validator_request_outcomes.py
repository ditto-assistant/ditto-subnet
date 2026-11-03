from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ditto.api_server import validator_request_outcomes
from ditto.api_server.validator_request_outcomes import RequestOutcomeTally


def test_tally_windows_recent_counts_and_evicts_the_quietest_validator(
    monkeypatch,
) -> None:
    now = [datetime(2026, 10, 28, tzinfo=UTC)]
    tally = RequestOutcomeTally(clock=lambda: now[0])
    monkeypatch.setattr(validator_request_outcomes, "MAX_VALIDATORS", 2)

    tally.record("5Old", "job", "allocator_busy", slot_id="slot-0")
    now[0] += timedelta(minutes=10)
    tally.record("5Mid", "job", "issued")
    now[0] += timedelta(minutes=10)
    tally.record("5Old", "job", "allocator_busy", slot_id="slot-1")
    now[0] += timedelta(seconds=1)
    tally.record("5Old", "job", "http_428", detail="x" * 500)
    now[0] += timedelta(minutes=1)
    tally.record("5New", "job", "issued")

    rows = {row["validator_hotkey"]: row for row in tally.snapshot()["validators"]}
    assert set(rows) == {"5Old", "5New"}
    old = {entry["outcome"]: entry for entry in rows["5Old"]["outcomes"]}
    assert rows["5Old"]["last_outcome"] == "http_428"
    assert (old["allocator_busy"]["total"], old["allocator_busy"]["recent_15m"]) == (
        2,
        1,
    )
    assert old["allocator_busy"]["last_slot_id"] == "slot-1"
    assert (
        len(old["http_428"]["last_detail"])
        == validator_request_outcomes.MAX_DETAIL_CHARS
    )
