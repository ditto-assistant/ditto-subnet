from __future__ import annotations

import json
from pathlib import Path

import pytest

from ditto_edge_protocol.cli import main
from ditto_edge_protocol.signing import DeviceKey


def test_keygen_creates_once_and_prints_the_node_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_path = tmp_path / "device.key"
    assert main(["keygen", "--key", str(key_path)]) == 0
    first = capsys.readouterr().out.strip()
    assert main(["keygen", "--key", str(key_path)]) == 0
    assert (
        capsys.readouterr().out.strip()
        == first
        == DeviceKey.load_or_create(key_path).node_id
    )


def test_dry_run_writes_a_passing_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.json"
    code = main(["dry-run", "--windows", "3", "--synthetic", "20", "--out", str(out)])
    summary = json.loads(capsys.readouterr().out)
    report = json.loads(out.read_text())
    assert code == 0
    assert summary["verdict"]["pass"] is True
    assert report["payouts"] == "none"


def test_dry_run_rejects_unknown_adversaries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["dry-run", "--adversaries", "lazy,wizard"]) == 2
    assert "wizard" in capsys.readouterr().err
