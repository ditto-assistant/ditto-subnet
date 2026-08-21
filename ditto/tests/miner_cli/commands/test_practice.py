"""Tests for the one-command local DittoBench practice wrapper."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from ditto.miner_cli.commands import practice
from ditto.miner_cli.errors import MinerCliError


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "starter_kit": None,
        "run_size": "small",
        "bench_version": None,
        "seed": None,
        "timeout": 7200,
        "report": None,
        "longmem_eval": False,
        "longmem_limit": None,
        "longmem_shards": 5,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_rehearsal_argv_defaults_leave_live_bench_version_to_the_runner() -> None:
    command = practice.rehearsal_argv(_args())
    assert command[:2] == [sys.executable, str(practice.REHEARSAL_SCRIPT)]
    assert command[command.index("--run-size") + 1] == "small"
    assert command[command.index("--timeout") + 1] == "7200"
    assert "--seed" not in command
    assert "--bench-version" not in command
    assert "--longmem-eval" not in command


def test_rehearsal_argv_forwards_every_reproducibility_option(tmp_path: Path) -> None:
    command = practice.rehearsal_argv(
        _args(
            starter_kit=tmp_path / "kit",
            run_size="full",
            bench_version=11,
            seed=42,
            timeout=99,
            report=tmp_path / "report.json",
            longmem_eval=True,
            longmem_limit=12,
            longmem_shards=3,
        )
    )
    for flag, value in (
        ("--starter-kit", str(tmp_path / "kit")),
        ("--run-size", "full"),
        ("--bench-version", "11"),
        ("--seed", "42"),
        ("--timeout", "99"),
        ("--report", str(tmp_path / "report.json")),
        ("--longmem-limit", "12"),
        ("--longmem-shards", "3"),
    ):
        assert command[command.index(flag) + 1] == value
    assert command.count("--longmem-eval") == 1


def test_run_returns_child_status_without_shell_interpolation() -> None:
    completed: subprocess.CompletedProcess[bytes] = subprocess.CompletedProcess(
        ["practice"], 7
    )
    with (
        patch.object(Path, "is_file", return_value=True),
        patch.object(practice.subprocess, "run", return_value=completed) as run,
    ):
        assert practice.run(_args()) == 7
    called = run.call_args.args[0]
    assert isinstance(called, list)
    assert run.call_args.kwargs == {"check": False}


def test_run_fails_actionably_without_source_assets() -> None:
    with (
        patch.object(Path, "is_file", return_value=False),
        pytest.raises(MinerCliError, match="ditto-subnet source checkout"),
    ):
        practice.run(_args())


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["practice"],
            {"run_size": "small", "bench_version": None, "longmem_eval": False},
        ),
        (
            ["practice", "--bench-version", "11", "--run-size", "full"],
            {"bench_version": 11, "run_size": "full"},
        ),
        (
            ["practice", "--longmem-eval", "--longmem-limit", "25"],
            {"longmem_eval": True, "longmem_limit": 25},
        ),
        (
            ["practice", "--run-size", "medium", "--longmem-shards", "7"],
            {"run_size": "medium", "longmem_shards": 7},
        ),
    ],
)
def test_parser_exposes_practice_options(
    argv: list[str], expected: dict[str, object]
) -> None:
    from ditto.miner_cli.__main__ import _build_parser

    args = _build_parser().parse_args(argv)
    for name, value in expected.items():
        assert getattr(args, name) == value


def test_help_states_longmem_is_separate(capsys: pytest.CaptureFixture[str]) -> None:
    from ditto.miner_cli.__main__ import main

    with pytest.raises(SystemExit) as error:
        main(["practice", "--help"])
    assert error.value.code == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "live SN118 scoring contract" in output
    assert "separate score" in output
    assert "Nothing must be exposed to the public internet" in output
