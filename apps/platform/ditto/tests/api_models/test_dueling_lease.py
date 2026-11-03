"""Platform never serves a partial or pre-16 dueling slice on a lease."""

from __future__ import annotations

import base64
import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto.api_models.validator import JobResponse

_SLICE = b'{"slice":"fixture"}\n'
_DUELING = {
    "dueling_slice_mode": "dueling-slice-v1",
    "dueling_slice_bytes": base64.b64encode(_SLICE).decode(),
    "expected_slice_sha256": hashlib.sha256(_SLICE).hexdigest(),
    "dueling_owner_key": "coldkey:a",
    "dueling_season_id": "s16-0001",
}


def _lease(**extra: object) -> dict[str, object]:
    return {
        "agent_id": str(uuid4()),
        "miner_hotkey": "5" + "F" * 47,
        "sha256": "ab" * 32,
        "deadline": "2026-11-03T00:00:00Z",
        **extra,
    }


def test_a_whole_slice_on_a_dueling_lease_is_served() -> None:
    lease = JobResponse.model_validate(_lease(bench_version=16, **_DUELING))
    assert lease.dueling_season_id == "s16-0001"
    assert (
        JobResponse.model_validate(_lease(bench_version=16)).dueling_slice_mode is None
    )
    assert (
        JobResponse.model_validate(_lease(bench_version=15)).dueling_slice_mode is None
    )


@pytest.mark.parametrize("missing", sorted(_DUELING))
def test_a_partial_slice_is_refused(missing: str) -> None:
    fields = {key: value for key, value in _DUELING.items() if key != missing}
    with pytest.raises(ValidationError, match=f"missing {missing}"):
        JobResponse.model_validate(_lease(bench_version=16, **fields))


@pytest.mark.parametrize("bench_version", [None, 15])
def test_a_slice_below_the_dueling_floor_is_refused(bench_version: int | None) -> None:
    with pytest.raises(ValidationError, match="only valid on a bench_version >= 16"):
        JobResponse.model_validate(_lease(bench_version=bench_version, **_DUELING))
