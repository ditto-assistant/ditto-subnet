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


def test_dry_run_trims_adversary_names(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "report.json"
    code = main(
        [
            "dry-run",
            "--windows",
            "3",
            "--synthetic",
            "20",
            "--adversaries",
            "lazy, , tamper ",
            "--out",
            str(out),
        ]
    )
    capsys.readouterr()
    assert code == 0
    assert json.loads(out.read_text())["config"]["adversaries"] == ["lazy", "tamper"]


def test_recheck_public_without_share_ids_is_refused(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["dry-run", "--recheck-public"]) == 2
    assert "--share-id" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["status", "leave"])
def test_status_and_leave_never_create_a_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    key_path = tmp_path / "missing.key"
    code = main(
        [
            "node",
            command,
            "--key",
            str(key_path),
            "--store",
            str(tmp_path / "objects"),
            "--api-url",
            "http://127.0.0.1:9",
        ]
    )
    assert code == 2
    assert not key_path.exists()
    assert "ditto-edge keygen" in capsys.readouterr().err


def test_node_run_still_creates_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    from ditto_edge_protocol.node import EdgeNode

    def stop_before_enrolling(_node: EdgeNode) -> None:
        raise RuntimeError("stop before any request")

    monkeypatch.setattr(signal, "signal", lambda *_: None)
    monkeypatch.setattr(EdgeNode, "enroll", stop_before_enrolling)
    key_path = tmp_path / "device.key"
    with pytest.raises(RuntimeError, match="stop before any request"):
        main(
            [
                "node",
                "run",
                "--key",
                str(key_path),
                "--store",
                str(tmp_path / "objects"),
                "--api-url",
                "http://127.0.0.1:9",
            ]
        )
    assert key_path.is_file()


@pytest.mark.parametrize("quota", ["-1", "nan", "inf", "lots"])
def test_node_commands_refuse_an_invalid_quota(tmp_path: Path, quota: str) -> None:
    key_path = tmp_path / "device.key"
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "node",
                "run",
                "--key",
                str(key_path),
                "--store",
                str(tmp_path / "objects"),
                "--quota-gb",
                quota,
            ]
        )
    assert raised.value.code == 2
    assert not key_path.exists()
