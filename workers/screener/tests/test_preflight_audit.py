"""Safety and concurrency tests for the private static-preflight audit journal."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ditto_screener.preflight_audit import (
    StaticPreflightAuditError,
    StaticPreflightAuditJournal,
)

_AGENT = UUID("550e8400-e29b-41d4-a716-446655440000")
_ATTEMPT = UUID("7c5df3f9-3ea7-47ba-92d1-1bbcf4c5f300")
_DIGEST = "a" * 64


def _record(journal: StaticPreflightAuditJournal, **payload: object) -> None:
    journal.record(
        agent_id=_AGENT,
        attempt_id=_ATTEMPT,
        artifact_sha256=_DIGEST,
        payload=payload,
    )


def test_unconfigured_journal_is_a_noop() -> None:
    _record(StaticPreflightAuditJournal(None), mode="shadow")


def test_journal_creates_private_directory_and_file(tmp_path: Path) -> None:
    path = tmp_path / "private" / "preflight.jsonl"

    _record(StaticPreflightAuditJournal(str(path)), mode="shadow")

    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["mode"] == "shadow"


def test_journal_repairs_overly_broad_existing_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o777)
    path = parent / "preflight.jsonl"
    path.write_text("")
    os.chmod(parent, 0o777)
    os.chmod(path, 0o666)

    _record(StaticPreflightAuditJournal(str(path)), mode="shadow")

    assert parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_trusted_bindings_cannot_be_overridden_by_payload(tmp_path: Path) -> None:
    path = tmp_path / "preflight.jsonl"

    _record(
        StaticPreflightAuditJournal(str(path)),
        agent_id="attacker",
        attempt_id="attacker",
        artifact_sha256="attacker",
    )

    record = json.loads(path.read_text())
    assert record["agent_id"] == str(_AGENT)
    assert record["attempt_id"] == str(_ATTEMPT)
    assert record["artifact_sha256"] == _DIGEST


def test_concurrent_appends_remain_intact_json_lines(tmp_path: Path) -> None:
    path = tmp_path / "preflight.jsonl"
    journal = StaticPreflightAuditJournal(str(path))
    count = 96

    def append(index: int) -> None:
        journal.record(
            agent_id=uuid4(),
            attempt_id=uuid4(),
            artifact_sha256=f"{index:064x}",
            payload={"index": index, "padding": "x" * 8192},
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(append, range(count)))

    lines = path.read_text().splitlines()
    assert len(lines) == count
    records = [json.loads(line) for line in lines]
    assert {record["index"] for record in records} == set(range(count))
    assert all(record["padding"] == "x" * 8192 for record in records)


def test_oversize_record_is_rejected_without_creating_file(tmp_path: Path) -> None:
    path = tmp_path / "preflight.jsonl"

    with pytest.raises(StaticPreflightAuditError, match="exceeds safety bound"):
        _record(StaticPreflightAuditJournal(str(path)), padding="x" * (32 * 1024))

    assert not path.exists()


def test_symlink_target_is_rejected_without_modifying_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "destination"
    destination.write_text("unchanged\n")
    path = tmp_path / "preflight.jsonl"
    path.symlink_to(destination)

    with pytest.raises(StaticPreflightAuditError, match="append failed"):
        _record(StaticPreflightAuditJournal(str(path)), mode="shadow")

    assert destination.read_text() == "unchanged\n"
