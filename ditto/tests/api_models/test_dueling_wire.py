"""Bench >= 16 (dueling) lease and ledger fields are additive on the wire."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ditto.api_models.validator import (
    ConfirmationScoreRecord,
    JobResponse,
    LedgerEntry,
)

_DUELING_LEASE_KEYS = {
    "dueling_slice_mode",
    "dueling_slice_bytes",
    "expected_slice_sha256",
    "dueling_owner_key",
    "dueling_season_id",
}


def _lease(**extra: object) -> dict[str, object]:
    return {
        "agent_id": str(uuid4()),
        "miner_hotkey": "5" + "F" * 47,
        "sha256": "ab" * 32,
        "deadline": "2026-11-03T00:00:00Z",
        **extra,
    }


def _dueling(season_id: str = "s16-1") -> dict[str, object]:
    return {
        "season_id": season_id,
        "season_index": 1,
        "slice_sha256": "cd" * 32,
        "owner_key": "coldkey:a",
        "datagen_raw": 0.8,
        "datagen_gate": 1.0,
        "column_weight": 0.1,
        "columns": [
            {
                "column_id": f"c{i}",
                "owner_key": "" if i == 4 else f"coldkey:o{i}",
                "n": 35,
                "sum": 28.0,
                "sum_sq": 24.0,
            }
            for i in range(5)
        ],
        "composite_stderr": 0.01,
    }


def test_pre_dueling_lease_serializes_without_dueling_keys() -> None:
    lease = JobResponse.model_validate(_lease(bench_version=15))
    assert not _DUELING_LEASE_KEYS & set(lease.model_dump(mode="json"))


def test_dueling_lease_round_trips() -> None:
    raw = b"slice-bytes"
    lease = JobResponse.model_validate(
        _lease(
            bench_version=16,
            dueling_slice_mode="dueling-slice-v1",
            dueling_slice_bytes=base64.b64encode(raw).decode(),
            expected_slice_sha256=hashlib.sha256(raw).hexdigest(),
            dueling_owner_key="coldkey:a",
            dueling_season_id="s16-1",
        )
    )
    dumped = lease.model_dump(mode="json")
    assert set(dumped) >= _DUELING_LEASE_KEYS
    assert JobResponse.model_validate(dumped) == lease


def test_dueling_lease_rejects_an_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        JobResponse.model_validate(_lease(dueling_slice_mode="dueling-slice-v2"))


def test_ledger_rows_carry_optional_dueling_aggregates() -> None:
    base = {
        "miner_hotkey": "5" + "F" * 47,
        "agent_id": str(uuid4()),
        "composite": 0.8,
        "n": 350,
        "first_seen": datetime(2026, 11, 1, tzinfo=UTC).isoformat(),
        "sha256": "ab" * 32,
        "run_id": "run",
        "seed": 1,
        "validator_hotkey": "5" + "G" * 47,
        "signature": None,
        "status": "scored",
    }
    legacy = LedgerEntry.model_validate(base)
    dumped = legacy.model_dump(mode="json")
    assert "dueling" not in dumped
    assert "dueling_season_id" not in dumped

    record = {
        "seed": 7,
        "composite": 0.8,
        "validator_hotkey": "5" + "G" * 47,
        "bench_version": 16,
        "dueling": _dueling(),
    }
    entry = LedgerEntry.model_validate(
        base
        | {
            "bench_version": 16,
            "dueling_season_id": "s16-1",
            "dueling": _dueling(),
            "confirmation_history": [record],
        }
    )
    assert entry.dueling is not None
    assert entry.dueling.season_id == entry.dueling_season_id
    assert entry.confirmation_history is not None
    assert entry.confirmation_history[0].dueling == entry.dueling
    assert "dueling" not in ConfirmationScoreRecord.model_validate(
        {k: v for k, v in record.items() if k != "dueling"}
    ).model_dump(mode="json")
