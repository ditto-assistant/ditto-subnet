"""Guard: the CLI pre-flight archive limits match the screener's contract.

``ditto verify`` and the upload pre-flight mirror the screener's archive
contract so a miner learns about a rejection before paying the upload fee.
The screener worker is a separate package, so this reads its source text,
like ``test_bench_version_pins``, and fails the pull request that moves
either side alone.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ditto.miner_cli.tar_validator import MAX_ARCHIVE_MEMBERS, MAX_UNPACKED_BYTES

SCREENER_GATE = (
    Path(__file__).resolve().parents[3] / "workers/screener/ditto_screener/gate.py"
)


def _screener_constant(name: str) -> object:
    for node in ast.parse(SCREENER_GATE.read_text()).body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            expression = ast.Expression(node.value)
            # Only integer arithmetic such as ``64 * 1024 * 1024`` is evaluated.
            assert all(
                isinstance(
                    part, (ast.Expression, ast.BinOp, ast.Constant, ast.operator)
                )
                for part in ast.walk(expression)
            ), f"{name} is not a constant arithmetic expression"
            return eval(compile(expression, name, "eval"), {"__builtins__": {}})
    raise AssertionError(f"{name} not found in {SCREENER_GATE}")


def test_cli_archive_limits_match_the_screener() -> None:
    assert _screener_constant("_MAX_ARCHIVE_MEMBERS") == MAX_ARCHIVE_MEMBERS
    assert _screener_constant("_MAX_UNPACKED_BYTES") == MAX_UNPACKED_BYTES
