"""Guard: the CLI pre-flight archive limits match the screener's contract.

``ditto verify`` and the upload pre-flight mirror the screener's archive
contract so a miner learns about a rejection before paying the upload fee.
The screener worker is a separate package, so this reads its source text,
like ``test_bench_version_pins``, and fails the pull request that moves
either side alone.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ditto.miner_cli import tar_validator
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


_SCR_CODE = re.compile(r'"(SCR-[A-Z]+-\d+)"')


def test_cli_emits_every_screener_contract_code() -> None:
    # ditto-subnet#2968: SCR-CONTRACT-004 was left out of the port, so
    # `ditto verify` passed archives the screener rejected after the fee.
    screener_codes = set(_SCR_CODE.findall(SCREENER_GATE.read_text()))
    cli_codes = set(_SCR_CODE.findall(Path(tar_validator.__file__).read_text()))
    assert "SCR-CONTRACT-004" in screener_codes
    assert screener_codes <= cli_codes, sorted(screener_codes - cli_codes)


def _function_body(source: str, name: str) -> str:
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                body = body[1:]  # docstrings may differ
            return "\n".join(ast.dump(statement) for statement in body)
    raise AssertionError(f"{name} not found")


def test_cli_parses_the_dockerfile_exactly_as_the_screener() -> None:
    assert _function_body(
        Path(tar_validator.__file__).read_text(), "_dockerfile_instructions"
    ) == _function_body(SCREENER_GATE.read_text(), "_dockerfile_instructions")


def test_published_envelope_states_the_archive_cap() -> None:
    from ditto_screening_protocol.runtime_envelope import runtime_envelope

    assert runtime_envelope().source_bytes == MAX_UNPACKED_BYTES
