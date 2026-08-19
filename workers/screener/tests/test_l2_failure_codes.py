"""Named L2 failure codes must stay attached to every static ValueError."""

from __future__ import annotations

import ast
import inspect

import httpx
import pytest

from ditto_screener import l2_review as l2_review_module


def test_every_static_l2_valueerror_maps_to_a_named_code() -> None:
    """No L2 ValueError may silently collapse to a bare exception name.

    Collapsing every ``ValueError`` into ``l2-valueerror`` is what made the
    operator-visible screening pipeline undiagnosable. If a new
    ``raise ValueError("...")`` is added to the L2 path, classify it.
    """
    source = inspect.getsource(l2_review_module)
    unmapped: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        call = node.exc
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        if call.func.id != "ValueError" or not call.args:
            continue
        literal = call.args[0]
        if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
            continue
        if literal.value not in l2_review_module._L2_FAILURE_CODES:
            unmapped.add(literal.value)

    assert not unmapped, (
        "add these to _L2_FAILURE_CODES so the failure keeps a cause: "
        f"{sorted(unmapped)}"
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ValueError("L2 analyzer timed out"), "l2-analyzer-timeout"),
        (
            ValueError("L2 analyzer exited with code 125"),
            "l2-analyzer-exited-125",
        ),
        (
            ValueError("L2 did not analyze every L1 evidence file"),
            "l2-l1-evidence-unanalyzed",
        ),
        (
            ValueError("L2 result has unexpected fields"),
            "l2-inconsistent-verdict",
        ),
        (
            ValueError("L2 model exceeded token or cost budget raw_input=1"),
            "l2-model-budget-exhausted",
        ),
        (ValueError("brand new unmapped failure"), "l2-valueerror"),
        (OSError("docker missing"), "l2-oserror"),
    ],
)
def test_l2_failure_codes_name_the_cause(error: Exception, expected: str) -> None:
    assert l2_review_module._error_code("l2", error) == expected


def test_l3_prefix_reuses_the_same_classification() -> None:
    assert (
        l2_review_module._error_code(
            "l3-critic", ValueError("L2 analyzer exited with code 127")
        )
        == "l3-critic-analyzer-exited-127"
    )


def test_http_status_codes_stay_provider_specific() -> None:
    request = httpx.Request("POST", "https://openrouter.example/api")
    response = httpx.Response(429, request=request, json={"error": {}})
    error = httpx.HTTPStatusError("rate limited", request=request, response=response)
    assert l2_review_module._error_code("l2", error) == "l2-http-429"
