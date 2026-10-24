"""Unit tests for the generator competition service decisions."""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

import ditto.api_server.generator_competition as service
from ditto.api_models.generator import (
    GeneratorBuildResult,
    GeneratorResultRequest,
    GeneratorSeedOutput,
    GeneratorSeedResult,
    ValidationReport,
)
from ditto.api_server.generator_competition import (
    DEFAULT_SETTINGS,
    UploadRejected,
    inspect_upload,
    settings_from_row,
)


def _archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue(), mtime=0)


_TREE = {"go.mod": b"module example.com/g\n\ngo 1.23\n", "main.go": b"package main\n"}


def _encode(raw: bytes) -> tuple[str, str]:
    return base64.b64encode(raw).decode(), hashlib.sha256(raw).hexdigest()


def test_inspect_upload_accepts_a_buildable_tree() -> None:
    b64, sha = _encode(_archive(_TREE))
    upload = inspect_upload(source_base64=b64, source_sha256=sha)
    assert upload.info.module == "example.com/g" and upload.info.files == 2


def test_inspect_upload_rejects_bad_bytes() -> None:
    b64, _ = _encode(_archive(_TREE))
    with pytest.raises(UploadRejected, match="does not match"):
        inspect_upload(source_base64=b64, source_sha256="0" * 64)
    with pytest.raises(UploadRejected, match="base64"):
        inspect_upload(source_base64="!!!", source_sha256="0" * 64)
    b64, sha = _encode(_archive({"main.go": b"package main\n"}))
    with pytest.raises(UploadRejected, match="go.mod"):
        inspect_upload(source_base64=b64, source_sha256=sha)


def _payload(**overrides: object) -> GeneratorResultRequest:
    seed = GeneratorSeedResult(
        seed="ab" * 32, validation=ValidationReport(valid=True, case_count=8)
    )
    base: dict[str, object] = {
        "lease_id": uuid4(),
        "run_id": uuid4(),
        "submission_id": uuid4(),
        "source_sha256": "cd" * 32,
        "scorer_version": "challenge-scorer/2",
        "build": GeneratorBuildResult(ok=True),
        "deterministic": True,
        "seeds": [seed],
        "probe": GeneratorSeedOutput(
            seed="ef" * 32, validation=ValidationReport(valid=True)
        ),
    }
    base.update(overrides)
    return GeneratorResultRequest(**base)  # type: ignore[arg-type]


def test_generator_failure_classification() -> None:
    assert service._generator_failure(_payload()) is None
    assert service._generator_failure(
        _payload(build=GeneratorBuildResult(ok=False, error="compile_failed: x"))
    ) == ("build_failed", "compile_failed: x")
    crashed = GeneratorSeedResult(seed="ab" * 32, error="generator generate: timeout")
    assert service._generator_failure(_payload(seeds=[crashed]))[0] == (  # type: ignore[index]
        "generation_failed"
    )
    bad_probe = GeneratorSeedOutput(seed="ef" * 32, validation=ValidationReport())
    assert service._generator_failure(_payload(probe=bad_probe))[0] == "invalid_probe"  # type: ignore[index]
    assert service._generator_failure(_payload(deterministic=False))[0] == (  # type: ignore[index]
        "nondeterministic"
    )


def test_corrupt_settings_close_the_lane() -> None:
    row = SimpleNamespace(revision=4, settings={"mode": "shadow", "panel_size": 999})
    settings, valid = settings_from_row(row)  # type: ignore[arg-type]
    assert settings == DEFAULT_SETTINGS and settings.mode == "off" and not valid


@dataclass
class _Row:
    agent_id: UUID
    miner_hotkey: str
    miner_coldkey: str | None
    composite: float
    eligible: bool = True
    first_seen: datetime = datetime(2026, 1, 1, tzinfo=UTC)


async def test_select_panel_takes_top_owners_and_marks_champion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _Row(uuid4(), "hk-low", "ck-low", 0.40),
        _Row(uuid4(), "hk-top", "ck-top", 0.90),
        _Row(uuid4(), "hk-mid", None, 0.70),
        _Row(uuid4(), "hk-held", "ck-held", 0.99, eligible=False),
    ]

    async def _ledger(_session: object, **_kwargs: object) -> list[_Row]:
        return rows

    monkeypatch.setattr(service, "list_eligible_ledger", _ledger)
    panel = await service.select_panel(object(), size=2)  # type: ignore[arg-type]
    assert panel == [
        {
            "agent_id": str(rows[1].agent_id),
            "owner_key": "coldkey:ck-top",
            "champion": True,
        },
        {
            "agent_id": str(rows[2].agent_id),
            "owner_key": "hotkey:hk-mid",
            "champion": False,
        },
    ]
