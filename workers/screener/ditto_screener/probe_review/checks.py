"""Deterministic predicates over a probe record.

A probe verdict is only as strong as its predicates. Every predicate here reads
the served ``/run`` response, the authoritative tool-endpoint ledger, or the
recorded model requests; none consults a model. Replay re-executes the same
stimulus and re-evaluates the same predicate list, so a verdict that cites
predicates is verifiable without another review layer.
"""

from __future__ import annotations

import json
import math
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


_COMPARISONS: dict[str, Callable[[float, float], bool]] = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
    "!=": lambda a, b: a != b,
}


def _compare(actual: Any, op: Any, expected: Any) -> bool:
    """A malformed comparison is a failed predicate, not an evaluation crash."""
    numeric = (int, float)
    if (
        not isinstance(actual, numeric)
        or not isinstance(expected, numeric)
        or isinstance(actual, bool)
        or isinstance(expected, bool)
        # Huge ints are exact and finite; isfinite would overflow on them.
        or (not isinstance(actual, int) and not math.isfinite(actual))
        or (not isinstance(expected, int) and not math.isfinite(expected))
        or not isinstance(op, str)
    ):
        return False
    compare = _COMPARISONS.get(op)
    return compare(actual, expected) if compare is not None else False


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
        if isinstance(choice, str) and choice not in {"auto", "none"}:
            # Legacy ``function_call: "<name>"`` forces that function.
            choice = {"type": "function", "function": {"name": choice}}
        elif isinstance(choice, dict) and choice.get("name"):
            choice = {"type": "function", "function": choice}
    return {
        "text": json.dumps(
            request.get("messages") or request.get("input") or "", ensure_ascii=False
        ),
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
    if not isinstance(check, Mapping):
        return False, f"malformed predicate: {type(check).__name__}"
    op = check.get("op")
    served = served_answer(record)
    if isinstance(op, str) and op.startswith("answer_") and served is None:
        # No served answer: no answer-content predicate can hold, not even
        # answer_lacks or answer_not_equals.
        return False, "answer=None"
    answer = served or ""
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
        "model_requests_matching": lambda: (
            _compare(
                _count_matching(record, check.get("where") or {}),
                check["cmp"],
                check["n"],
            ),
            f"matching_requests={_count_matching(record, check.get('where') or {})}",
        ),
        "model_request_after_tool": lambda: _model_request_after_tool(
            record, check.get("name")
        ),
    }
    handler = handlers.get(str(op))
    if handler is None:
        return False, f"unknown predicate {op!r}"
    try:
        return handler()
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as error:
        # A malformed check is a failed predicate, never an evaluation crash.
        return False, f"malformed predicate {op!r}: {type(error).__name__}"


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
    reported = []
    for c in calls if isinstance(calls, list) else []:
        if not isinstance(c, dict):
            continue
        # Flat {name, args} or OpenAI-nested {function: {name, arguments}}.
        call = as_dict(c.get("function")) or c
        args = call.get("arguments", c.get("args"))
        reported.append({"name": call.get("name"), "args": _parse_args(args)})
    return reported


def _count_matching(record: ProbeRecord, where: Mapping[str, Any]) -> int:
    return sum(
        _request_matches(_model_request_view(e), where) for e in record.model_requests
    )


def _model_request_after_tool(
    record: ProbeRecord, name: str | None
) -> tuple[bool, str]:
    """A model request follows the first (named) tool execution in the transcript."""
    tool_seqs = [
        int(e.get("seq") or 0)
        for e in record.tool_executions
        if name is None or as_dict(e.get("request")).get("name") == name
    ]
    if not tool_seqs:
        return False, "no matching tool execution"
    first = min(tool_seqs)
    later = [int(e.get("seq") or 0) for e in record.model_requests]
    after = [seq for seq in later if seq > first]
    return bool(after), f"tool_seq={first} later_model_seqs={after[:8]}"


def _unmatched(
    calls: list[dict[str, Any]], against: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Calls with no counterpart, consuming one counterpart per call."""
    remaining = list(against)
    missing = []
    for call in calls:
        if call in remaining:
            remaining.remove(call)
        else:
            missing.append(call)
    return missing


def _reported_not_executed(record: ProbeRecord) -> tuple[bool, str]:
    missing = _unmatched(_reported(record), executions(record))
    return bool(missing), f"reported_not_executed={json.dumps(missing)[:400]}"


def _executed_not_reported(record: ProbeRecord) -> tuple[bool, str]:
    missing = _unmatched(executions(record), _reported(record))
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
        shown = dict(check) if isinstance(check, Mapping) else {"malformed": check}
        results.append({"check": shown, "holds": holds, "observed": observed})
    return results
