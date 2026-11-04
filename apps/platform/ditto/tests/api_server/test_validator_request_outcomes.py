from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from ditto.api_server import validator_request_outcomes
from ditto.api_server.validator_request_outcomes import (
    RequestOutcomeTally,
    track_request_outcomes,
)


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


async def test_lease_lane_503_with_retry_after_tallies_as_retry_later(
    monkeypatch,
) -> None:
    tally = RequestOutcomeTally()
    monkeypatch.setattr(validator_request_outcomes, "REQUEST_OUTCOMES", tally)

    def endpoint(lane, headers):
        @track_request_outcomes(lane)
        async def handler(*, payload):  # noqa: ARG001 - the tally reads it
            raise HTTPException(
                503, "dueling slice preparation pending", headers=headers
            )

        return handler

    payload = SimpleNamespace(validator_hotkey="5Val", slot_id="slot-2")
    for lane, headers in (
        ("job", {"Retry-After": "15"}),
        ("top5_confirmation", {"Retry-After": "15"}),
        ("scoring_ledger", {"Retry-After": "5"}),
        ("job", None),
    ):
        with pytest.raises(HTTPException):
            await endpoint(lane, headers)(payload=payload)

    rows = {row["lane"]: row for row in tally.snapshot()["validators"]}
    job = {entry["outcome"]: entry for entry in rows["job"]["outcomes"]}
    assert set(job) == {"retry_later", "http_503"}
    assert job["retry_later"]["last_detail"] == "dueling slice preparation pending"
    assert job["retry_later"]["last_slot_id"] == "slot-2"
    assert rows["top5_confirmation"]["last_outcome"] == "retry_later"
    # The ledger lane is not a lease: its 503 keeps meaning "ledger unavailable".
    assert rows["scoring_ledger"]["last_outcome"] == "http_503"
