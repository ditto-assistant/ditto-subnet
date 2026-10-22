"""Deterministic predicates over a probe record.

A probe verdict is only as strong as its predicates. Every predicate here reads
the served ``/run`` response, the authoritative tool-endpoint ledger, or the
recorded model requests; none consults a model. Replay re-executes the same
stimulus and re-evaluates the same predicate list, so a verdict that cites
predicates is verifiable without another review layer.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ditto_screener.probe_review.sandbox import ProbeRecord

Check = Mapping[str, Any]


def served_answer(record: ProbeRecord) -> str | None:
    response = record.response if isinstance(record.response, dict) else {}
    answer = response.get("answer")
    # The v13 scorer's AssertedAnswer: the slot when it is non-blank, else
    # the prose (services/dittobench-api/internal/scorer/twins_v13.go).
    if isinstance(answer, str) and answer.strip():
        return answer
    final = response.get("final_text")
    return final if isinstance(final, str) else None


def _field(value: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    return value


def _compare(actual: float, op: str, expected: float) -> bool:
    return {
        ">=": actual >= expected,
        "<=": actual <= expected,
        "==": actual == expected,
        ">": actual > expected,
        "<": actual < expected,
        "!=": actual != expected,
    }[op]


def _parse_args(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _subset(expected: Any, actual: Any) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            k in actual and _subset(v, actual[k]) for k, v in expected.items()
        )
    return expected == actual


def executions(record: ProbeRecord, name: str | None = None) -> list[dict[str, Any]]:
    out = []
    for entry in record.tool_executions:
        request = as_dict(entry.get("request"))
        if name is None or request.get("name") == name:
            out.append(
                {"name": request.get("name"), "args": _parse_args(request.get("args"))}
            )
    return out


def _model_request_view(entry: dict[str, Any]) -> dict[str, Any]:
    request = as_dict(entry.get("request"))
    # Legacy function calling: ``functions`` + ``function_call``.
    tools = request.get("tools") or request.get("functions") or []
    names = []
    for tool in tools if isinstance(tools, list) else []:
        if isinstance(tool, dict):
            fn = as_dict(tool.get("function")) or tool
            names.append(fn.get("name"))
    choice = request.get("tool_choice")
    if choice is None:
        choice = request.get("function_call")
        if isinstance(choice, dict) and choice.get("name"):
            choice = {"type": "function", "function": choice}
    return {
        "text": json.dumps(request.get("messages") or request.get("input") or ""),
        "tool_names": names,
        "tool_choice": choice,
        "pinned": _choice_pinned(choice),
    }


def _choice_pinned(choice: Any) -> bool:
    """Whether a tool_choice removes the model's discretion (any wire format)."""
    if isinstance(choice, str):
        return choice in {"required", "any"}
    if not isinstance(choice, dict):
        return False
    kind = choice.get("type")
    if kind == "any":
        return True
    named = as_dict(choice.get("function")).get("name") or choice.get("name")
    return kind in {"function", "tool"} and bool(named)


def _search(pattern: str, text: str) -> bool:
    """A malformed pattern is a failed predicate, not an evaluation crash."""
    try:
        return re.search(pattern, text) is not None
    except re.error:
        return False


def _request_matches(view: dict[str, Any], spec: Mapping[str, Any]) -> bool:
    if "prompt_contains" in spec and str(spec["prompt_contains"]) not in view["text"]:
        return False
    if "prompt_lacks" in spec and str(spec["prompt_lacks"]) in view["text"]:
        return False
    if "tool_choice_pinned" in spec and view["pinned"] != bool(
        spec["tool_choice_pinned"]
    ):
        return False
    if "tools_count" in spec:
        op, n = spec["tools_count"]
        if not _compare(len(view["tool_names"]), op, n):
            return False
    if "offers_tool" in spec and spec["offers_tool"] not in view["tool_names"]:
        return False
    return not ("lacks_tool" in spec and spec["lacks_tool"] in view["tool_names"])


def evaluate(record: ProbeRecord, check: Check) -> tuple[bool, str]:
    """Return ``(holds, observed)`` for one predicate."""
    op = check.get("op")
    answer = served_answer(record) or ""
    response = record.response if isinstance(record.response, dict) else {}
    value = check.get("value")
    handlers: dict[str, Callable[[], tuple[bool, str]]] = {
        "http_ok": lambda: (
            record.http_status is not None and 200 <= record.http_status < 300,
            f"http_status={record.http_status}",
        ),
        "answer_equals": lambda: (
            answer.strip() == str(value),
            f"answer={answer[:200]!r}",
        ),
        "answer_not_equals": lambda: (
            answer.strip() != str(value),
            f"answer={answer[:200]!r}",
        ),
        "answer_contains": lambda: (str(value) in answer, f"answer={answer[:200]!r}"),
        "answer_lacks": lambda: (str(value) not in answer, f"answer={answer[:200]!r}"),
        "answer_matches": lambda: (
            _search(str(value), answer),
            f"answer={answer[:200]!r}",
        ),
        "final_text_contains": lambda: (
            str(value) in str(response.get("final_text", "")),
            f"final_text={str(response.get('final_text', ''))[:200]!r}",
        ),
        "final_text_lacks": lambda: (
            str(value) not in str(response.get("final_text", "")),
            f"final_text={str(response.get('final_text', ''))[:200]!r}",
        ),
        "field_equals": lambda: (
            _field(response, str(check.get("path"))) == value,
            f"{check.get('path')}={_field(response, str(check.get('path')))!r}",
        ),
        "model_calls": lambda: (
            _compare(len(record.model_requests), check["cmp"], check["n"]),
            f"model_calls={len(record.model_requests)}",
        ),
        "tool_executed": lambda: _tool_executed(record, check),
        "tool_not_executed": lambda: _negate(_tool_executed(record, check)),
        "tool_execution_count": lambda: (
            _compare(
                len(executions(record, check.get("name"))), check["cmp"], check["n"]
            ),
            f"executions={[e['name'] for e in executions(record)]}",
        ),
        "reported_not_executed": lambda: _reported_not_executed(record),
        "executed_not_reported": lambda: _executed_not_reported(record),
        "any_model_request": lambda: _any_request(record, check.get("where") or {}),
        "no_model_request": lambda: _negate(
            _any_request(record, check.get("where") or {})
        ),
        "last_model_request": lambda: _last_request(record, check.get("where") or {}),
    }
    handler = handlers.get(str(op))
    if handler is None:
        return False, f"unknown predicate {op!r}"
    return handler()


def _negate(result: tuple[bool, str]) -> tuple[bool, str]:
    return not result[0], result[1]


def _tool_executed(record: ProbeRecord, check: Check) -> tuple[bool, str]:
    seen = executions(record, check.get("name"))
    if "args" in check:
        hit = any(_subset(check["args"], e["args"]) for e in seen)
    elif "args_exact" in check:
        hit = any(e["args"] == check["args_exact"] for e in seen)
    else:
        hit = bool(seen)
    return hit, f"executions={json.dumps(executions(record))[:400]}"


def _reported(record: ProbeRecord) -> list[dict[str, Any]]:
    response = record.response if isinstance(record.response, dict) else {}
    calls = response.get("tool_calls") or []
    return [
        {"name": c.get("name"), "args": _parse_args(c.get("args"))}
        for c in calls
        if isinstance(c, dict)
    ]


def _reported_not_executed(record: ProbeRecord) -> tuple[bool, str]:
    executed = executions(record)
    missing = [c for c in _reported(record) if c not in executed]
    return bool(missing), f"reported_not_executed={json.dumps(missing)[:400]}"


def _executed_not_reported(record: ProbeRecord) -> tuple[bool, str]:
    reported = _reported(record)
    missing = [c for c in executions(record) if c not in reported]
    return bool(missing), f"executed_not_reported={json.dumps(missing)[:400]}"


def _any_request(record: ProbeRecord, where: Mapping[str, Any]) -> tuple[bool, str]:
    views = [_model_request_view(e) for e in record.model_requests]
    hits = [i for i, v in enumerate(views) if _request_matches(v, where)]
    summary = [
        {"i": i, "tools": len(v["tool_names"]), "tool_choice": v["tool_choice"]}
        for i, v in enumerate(views)
    ]
    return bool(hits), f"matching_requests={hits} of {json.dumps(summary)[:400]}"


def _last_request(record: ProbeRecord, where: Mapping[str, Any]) -> tuple[bool, str]:
    views = [_model_request_view(e) for e in record.model_requests]
    if not views:
        return False, "no model requests"
    return _request_matches(
        views[-1], where
    ), f"last_tool_choice={views[-1]['tool_choice']}"


def evaluate_all(record: ProbeRecord, checks: Sequence[Check]) -> list[dict[str, Any]]:
    results = []
    for check in checks:
        holds, observed = evaluate(record, check)
        results.append({"check": dict(check), "holds": holds, "observed": observed})
    return results
