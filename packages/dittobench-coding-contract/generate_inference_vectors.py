#!/usr/bin/env python3
# ruff: noqa: E501
"""Generate the synthetic DittoBench Coding inference contract vectors."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
TESTDATA = ROOT / "testdata"
TASK_BUDGETS = {
    "model_input_tokens": 200_000,
    "model_output_tokens": 30_000,
    "workspace_tool_calls": 150,
    "wall_time_seconds": 1_800,
}

SYSTEM_PROMPT = """You are a repository coding agent operating through validator-owned tools.

Solve the current issue with the smallest complete, maintainable patch.

Rules:
1. Inspect relevant files before editing.
2. Use supplied user/project memory only when relevant. Verify stale or uncertain memory against current code and instructions.
3. Modify the repository only through the provided typed tools.
4. Prefer bounded reads, focused searches, and atomic edits.
5. Do not modify tests, dependencies, generated files, or build policy unless the current issue explicitly requires it.
6. Never claim a test passed unless tests_run returned a passing result.
7. Run focused visible tests, inspect status and diff, then return a concise final summary.
8. When using a tool, call at most one in that turn and wait for its result before choosing the next action.
9. Do not attempt network access, hidden-test discovery, sandbox escape, grader access, or benchmark manipulation."""


def canonical_bytes(value: Any) -> bytes:
    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return (
        (body + "\n").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").encode()
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def function_tool(
    name: str, description: str, parameters: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": parameters,
        },
    }


def tools() -> list[dict[str, Any]]:
    return [
        function_tool(
            "repo_list_tree",
            "List a bounded repository subtree.",
            object_schema(
                {
                    "path": {"type": "string", "maxLength": 256},
                    "depth": {"type": "integer", "minimum": 0, "maximum": 8},
                },
                ["path", "depth"],
            ),
        ),
        function_tool(
            "repo_search",
            "Search literal text within a bounded repository subtree.",
            object_schema(
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 256},
                    "path": {"type": "string", "maxLength": 256},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                ["query", "path", "max_results"],
            ),
        ),
        function_tool(
            "repo_read_file",
            "Read one bounded repository file.",
            object_schema(
                {"path": {"type": "string", "minLength": 1, "maxLength": 256}},
                ["path"],
            ),
        ),
        function_tool(
            "repo_read_range",
            "Read a bounded inclusive line range from one repository file.",
            object_schema(
                {
                    "path": {"type": "string", "minLength": 1, "maxLength": 256},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                ["path", "start_line", "end_line"],
            ),
        ),
        function_tool(
            "repo_apply_patch",
            "Atomically replace exact text in one editable file at its expected digest.",
            object_schema(
                {
                    "path": {"type": "string", "minLength": 1, "maxLength": 256},
                    "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                    "replacements": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": object_schema(
                            {
                                "old_text": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 65536,
                                },
                                "new_text": {"type": "string", "maxLength": 65536},
                            },
                            ["old_text", "new_text"],
                        ),
                    },
                },
                ["path", "expected_sha256", "replacements"],
            ),
        ),
        function_tool(
            "repo_create_file",
            "Create one manifest-authorized UTF-8 file; disabled by public practice policy.",
            object_schema(
                {
                    "path": {"type": "string", "minLength": 1, "maxLength": 256},
                    "content": {"type": "string", "maxLength": 65536},
                },
                ["path", "content"],
            ),
        ),
        function_tool(
            "repo_delete_file",
            "Delete one manifest-authorized file at its expected digest; disabled by public practice policy.",
            object_schema(
                {
                    "path": {"type": "string", "minLength": 1, "maxLength": 256},
                    "expected_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                },
                ["path", "expected_sha256"],
            ),
        ),
        function_tool(
            "tests_run",
            "Run a task-manifest test command by opaque command ID.",
            object_schema(
                {"command_id": {"type": "string", "minLength": 1, "maxLength": 80}},
                ["command_id"],
            ),
        ),
        function_tool(
            "build_run",
            "Run a task-manifest build command by opaque command ID.",
            object_schema(
                {"command_id": {"type": "string", "minLength": 1, "maxLength": 80}},
                ["command_id"],
            ),
        ),
        function_tool(
            "git_status",
            "Return the validator-owned workspace change status.",
            object_schema({}, []),
        ),
        function_tool(
            "git_diff",
            "Return the bounded current workspace diff for review.",
            object_schema({}, []),
        ),
    ]


def miner_request(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "model": "openai/gpt-5.6-luna",
        "messages": messages,
        "tools": tools(),
        "tool_choice": "auto",
        "reasoning": {"effort": "medium"},
        "max_completion_tokens": 32768,
        "parallel_tool_calls": False,
    }


def locked_request(messages: list[dict[str, Any]]) -> dict[str, Any]:
    request = miner_request(messages)
    request["reasoning"] = {"effort": "medium", "exclude": True}
    request["n"] = 1
    request["stream"] = False
    request["store"] = False
    request["usage"] = {"include": True}
    request["provider"] = {
        "only": ["azure/eu"],
        "order": ["azure/eu"],
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
    }
    return request


def response_projection(raw: dict[str, Any], cost_usd_micros: int) -> dict[str, Any]:
    return {
        "schema": "dittobench-coding-inference-response-v1",
        "id": raw["id"],
        "model": raw["model"],
        "provider": raw["provider"],
        "choices": raw["choices"],
        "usage": {
            "prompt_tokens": raw["usage"]["prompt_tokens"],
            "completion_tokens": raw["usage"]["completion_tokens"],
            "total_tokens": raw["usage"]["total_tokens"],
            "cost_usd_micros": cost_usd_micros,
        },
    }


def receipt_set(
    *, policy_sha256: str, receipts: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema": "dittobench-coding-inference-receipt-set-v1",
        "coding_contract_version": 1,
        "ticket_id": "33333333-3333-4333-8333-333333333333",
        "case_id": "case-inference-001",
        "profile_capability_id": "profile-inference-001",
        "inference_grant_sha256": policy_sha256,
        "grant_id": "44444444-4444-4444-8444-444444444444",
        "generation": 1,
        "request_budget": min(TASK_BUDGETS["workspace_tool_calls"] + 16, 256),
        "prompt_token_budget": TASK_BUDGETS["model_input_tokens"],
        "completion_token_budget": TASK_BUDGETS["model_output_tokens"],
        "receipts": receipts,
    }


def receipt(
    *,
    sequence: int,
    request_sequence: int,
    attempt: int,
    request_id: str,
    locked_request_sha256: str,
    prompt_sha256: str,
    tool_schema_sha256: str,
    outcome: str,
    failure_code: str | None,
    http_status: int,
    response_sha256: str | None,
    provider_generation_id: str | None,
    provider_selected: bool,
    receipt_provider: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd_micros: int,
    timed_out: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "dittobench-coding-inference-receipt-v1",
        "sequence": sequence,
        "request_sequence": request_sequence,
        "attempt": attempt,
        "request_id": request_id,
        "locked_request_sha256": locked_request_sha256,
        "prompt_sha256": prompt_sha256,
        "tool_schema_sha256": tool_schema_sha256,
        "outcome": outcome,
        "failure_code": failure_code,
        "http_status": http_status,
        "response_sha256": response_sha256,
        "response_digest_kind": (
            "none"
            if response_sha256 is None
            else "normalized_v1"
            if outcome == "complete"
            else "canonical_json_v1"
        ),
        "provider_generation_id": provider_generation_id,
        "model": "openai/gpt-5.6-luna",
        "provider_route": "azure/eu",
        "provider_route_profile": "luna-azure-eu-zdr-v1",
        "provider_selected": provider_selected,
        "receipt_provider": receipt_provider,
        "fallback_used": False,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd_micros": cost_usd_micros,
        "timed_out": timed_out,
    }


def provider_settlement(value: dict[str, Any], policy_sha256: str) -> dict[str, Any]:
    provider_accounting_available = value["provider_selected"]
    return {
        "schema": "dittobench-coding-provider-settlement-v1",
        "coding_contract_version": 1,
        "ticket_id": "33333333-3333-4333-8333-333333333333",
        "case_id": "case-inference-001",
        "profile_capability_id": "profile-inference-001",
        "inference_grant_sha256": policy_sha256,
        "grant_id": "44444444-4444-4444-8444-444444444444",
        "generation": 1,
        "request_id": value["request_id"],
        "request_sequence": value["request_sequence"],
        "attempt": value["attempt"],
        "locked_request_sha256": value["locked_request_sha256"],
        "outcome": value["outcome"],
        "terminal_error_code": value["failure_code"],
        "http_status": value["http_status"],
        "response_sha256": value["response_sha256"],
        "response_digest_kind": value["response_digest_kind"],
        "provider_generation_id": value["provider_generation_id"],
        "model": value["model"],
        "provider_api": "openrouter",
        "provider_route": value["provider_route"],
        "receipt_provider": value["receipt_provider"],
        "provider_route_profile": value["provider_route_profile"],
        "provider_account_guardrail": "openrouter_private_account_v1",
        "provider_pipeline_policy": "no_plugins_no_transforms_v1",
        "provider_cache_policy": "disabled_v1",
        "router_metadata_verified": True,
        "router_attempts": [
            {
                "provider": "Azure",
                "selected": value["provider_selected"],
            }
        ],
        "pipeline_stages": [],
        "fallback_used": value["fallback_used"],
        "usage_available": provider_accounting_available,
        "prompt_tokens": value["prompt_tokens"],
        "completion_tokens": value["completion_tokens"],
        "total_tokens": value["total_tokens"],
        "cost_available": provider_accounting_available,
        "cost_usd_micros": value["cost_usd_micros"],
        "timed_out": value["timed_out"],
    }


def model_evidence(
    *,
    policy_sha256: str,
    prompt_sha256: str,
    tool_schema_sha256: str,
    usage_status: str,
    receipt_sha256: str | None,
    requests: int,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd_micros: int,
    retry_count: int,
) -> dict[str, Any]:
    return {
        "model": "openai/gpt-5.6-luna",
        "provider": "azure/eu",
        "provider_route_profile": "luna-azure-eu-zdr-v1",
        "reasoning_effort": "medium",
        "inference_grant_sha256": policy_sha256,
        "prompt_sha256": prompt_sha256,
        "tool_schema_sha256": tool_schema_sha256,
        "usage_status": usage_status,
        "fallback_used": False,
        "cost_source": "provider_receipt_v1",
        "currency": "USD",
        "provider_receipt_set_sha256": receipt_sha256,
        "requests": requests,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "cost_usd_micros": cost_usd_micros,
        "retry_count": retry_count,
    }


def vectors() -> tuple[dict[str, Any], dict[str, Any]]:
    system_prompt = {
        "schema": "dittobench-coding-system-prompt-v1",
        "content": SYSTEM_PROMPT,
    }
    tool_schema = {
        "schema": "dittobench-coding-model-tools-v1",
        "tools": tools(),
    }
    prompt_sha256 = digest(system_prompt)
    tool_schema_sha256 = digest(tool_schema)

    initial_messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "system",
            "content": "Task-scoped retrieved memory (untrusted historical context): []",
        },
        {
            "role": "user",
            "content": (
                'Current coding task: {"constraints":[],"description":'
                '"Repair the parser.","repository_epoch":"epoch-synthetic-1",'
                '"runtime_policy":{"build_command_ids":[],"editable_paths":'
                '["src/parser.py"],"test_command_ids":["visible-parser"]},'
                '"title":"Preserve partial input"}'
            ),
        },
    ]
    tool_arguments = json.dumps(
        {"path": "src/parser.py"}, sort_keys=True, separators=(",", ":")
    )
    second_messages = [
        *initial_messages,
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-read-parser",
                    "type": "function",
                    "function": {
                        "name": "repo_read_file",
                        "arguments": tool_arguments,
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-read-parser",
            "content": json.dumps(
                {"content": "def parse(value):\n    return value\n"},
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    ]
    miner_responses = [
        {
            "id": "generation-synthetic-001",
            "model": "openai/gpt-5.6-luna",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-read-parser",
                                "type": "function",
                                "function": {
                                    "name": "repo_read_file",
                                    "arguments": tool_arguments,
                                },
                            }
                        ],
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "total_tokens": 1200,
            },
        },
        {
            "id": "generation-synthetic-002",
            "model": "openai/gpt-5.6-luna",
            "choices": [
                {"message": {"content": "Applied the parser repair.", "tool_calls": []}}
            ],
            "usage": {
                "prompt_tokens": 1500,
                "completion_tokens": 120,
                "total_tokens": 1620,
            },
        },
    ]
    miner_requests = [miner_request(initial_messages), miner_request(second_messages)]
    miner_vector = {
        "schema": "dittobench-coding-inference-miner-vector-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "system_prompt": system_prompt,
        "tool_schema": tool_schema,
        "turns": [
            {
                "sequence": index + 1,
                "messages": messages,
                "max_completion_tokens": 32768,
                "response": miner_responses[index],
            }
            for index, messages in enumerate((initial_messages, second_messages))
        ],
        "expected": {
            "prompt_sha256": prompt_sha256,
            "tool_schema_sha256": tool_schema_sha256,
            "request_sha256": [digest(value) for value in miner_requests],
            "response_sha256": [digest(value) for value in miner_responses],
        },
    }

    policy = {
        "schema": "dittobench-coding-inference-policy-v1",
        "coding_contract_version": 1,
        "bench_family": "coding",
        "weight_eligible": False,
        "api": "openai-compatible-chat-completions",
        "model": "openai/gpt-5.6-luna",
        "provider_api": "openrouter",
        "provider_route": "azure/eu",
        "receipt_provider": "Azure",
        "provider_receipt_source": "platform_settlement_v1",
        "provider_account_guardrail": "openrouter_private_account_v1",
        "provider_pipeline_policy": "no_plugins_no_transforms_v1",
        "provider_cache_policy": "disabled_v1",
        "router_metadata_required": True,
        "provider_route_profile": "luna-azure-eu-zdr-v1",
        "prompt_sha256": prompt_sha256,
        "tool_schema_sha256": tool_schema_sha256,
        "reasoning_effort": "medium",
        "reasoning_excluded": True,
        "stream": False,
        "store": False,
        "n": 1,
        "parallel_tool_calls": False,
        "max_tool_calls_per_response": 1,
        "usage_included": True,
        "allow_fallbacks": False,
        "require_parameters": True,
        "data_collection": "deny",
        "zdr": True,
        "max_requests": 256,
        "max_prompt_tokens": 2_000_000,
        "max_completion_tokens": 250_000,
        "max_total_tokens": 2_250_000,
        "max_completion_tokens_per_request": 32_768,
        "max_cost_usd_micros": 10_000_000,
        "max_request_bytes": 4 << 20,
        "max_response_bytes": 8 << 20,
        "request_timeout_milliseconds": 300_000,
        "retry_policy": "receipt_free_pre_provider_v1",
        "max_attempts_per_request": 3,
        "max_retries": 100,
        "cost_source": "provider_receipt_v1",
        "currency": "USD",
    }
    policy_sha256 = digest(policy)
    locked_requests = [
        locked_request(initial_messages),
        locked_request(second_messages),
    ]
    request_sha256 = [digest(value) for value in locked_requests]
    provider_responses = [
        {
            **miner_responses[0],
            "provider": "Azure",
            "usage": {**miner_responses[0]["usage"], "cost": 0.001234},
        },
        {
            **miner_responses[1],
            "provider": "Azure",
            "usage": {**miner_responses[1]["usage"], "cost": 0.000456},
        },
    ]
    response_projections = [
        response_projection(provider_responses[0], 1234),
        response_projection(provider_responses[1], 456),
    ]
    response_sha256 = [digest(value) for value in response_projections]
    invalid_provider_response = {
        "id": "generation-synthetic-invalid",
        "model": "openai/other-model",
        "provider": "Azure",
        "choices": [],
        "usage": None,
    }
    invalid_provider_response_sha256 = digest(invalid_provider_response)

    first_id = "55555555-5555-4555-8555-555555555555"
    second_id = "66666666-6666-4666-8666-666666666666"
    complete = receipt_set(
        policy_sha256=policy_sha256,
        receipts=[
            receipt(
                sequence=1,
                request_sequence=1,
                attempt=1,
                request_id=first_id,
                locked_request_sha256=request_sha256[0],
                prompt_sha256=prompt_sha256,
                tool_schema_sha256=tool_schema_sha256,
                outcome="complete",
                failure_code=None,
                http_status=200,
                response_sha256=response_sha256[0],
                provider_generation_id="generation-synthetic-001",
                provider_selected=True,
                receipt_provider="Azure",
                prompt_tokens=1000,
                completion_tokens=200,
                cost_usd_micros=1234,
            ),
            receipt(
                sequence=2,
                request_sequence=2,
                attempt=1,
                request_id=second_id,
                locked_request_sha256=request_sha256[1],
                prompt_sha256=prompt_sha256,
                tool_schema_sha256=tool_schema_sha256,
                outcome="complete",
                failure_code=None,
                http_status=200,
                response_sha256=response_sha256[1],
                provider_generation_id="generation-synthetic-002",
                provider_selected=True,
                receipt_provider="Azure",
                prompt_tokens=1500,
                completion_tokens=120,
                cost_usd_micros=456,
            ),
        ],
    )
    retry_complete = receipt_set(
        policy_sha256=policy_sha256,
        receipts=[
            receipt(
                sequence=1,
                request_sequence=1,
                attempt=1,
                request_id=first_id,
                locked_request_sha256=request_sha256[0],
                prompt_sha256=prompt_sha256,
                tool_schema_sha256=tool_schema_sha256,
                outcome="receipt_free_retry",
                failure_code="pre_provider_unavailable",
                http_status=503,
                response_sha256=None,
                provider_generation_id=None,
                provider_selected=False,
                receipt_provider=None,
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd_micros=0,
            ),
            receipt(
                sequence=2,
                request_sequence=1,
                attempt=2,
                request_id=first_id,
                locked_request_sha256=request_sha256[0],
                prompt_sha256=prompt_sha256,
                tool_schema_sha256=tool_schema_sha256,
                outcome="complete",
                failure_code=None,
                http_status=200,
                response_sha256=response_sha256[0],
                provider_generation_id="generation-synthetic-001",
                provider_selected=True,
                receipt_provider="Azure",
                prompt_tokens=1000,
                completion_tokens=200,
                cost_usd_micros=1234,
            ),
        ],
    )
    provider_failure = receipt_set(
        policy_sha256=policy_sha256,
        receipts=[
            receipt(
                sequence=1,
                request_sequence=1,
                attempt=1,
                request_id=first_id,
                locked_request_sha256=request_sha256[0],
                prompt_sha256=prompt_sha256,
                tool_schema_sha256=tool_schema_sha256,
                outcome="provider_failure",
                failure_code="provider_timeout",
                http_status=504,
                response_sha256=None,
                provider_generation_id=None,
                provider_selected=True,
                receipt_provider="Azure",
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd_micros=0,
                timed_out=True,
            )
        ],
    )
    response_invalid = receipt_set(
        policy_sha256=policy_sha256,
        receipts=[
            receipt(
                sequence=1,
                request_sequence=1,
                attempt=1,
                request_id=first_id,
                locked_request_sha256=request_sha256[0],
                prompt_sha256=prompt_sha256,
                tool_schema_sha256=tool_schema_sha256,
                outcome="provider_failure",
                failure_code="provider_response_invalid",
                http_status=200,
                response_sha256=invalid_provider_response_sha256,
                provider_generation_id="generation-synthetic-invalid",
                provider_selected=True,
                receipt_provider="Azure",
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd_micros=0,
            )
        ],
    )
    receipt_sets = {
        "complete": complete,
        "retry_complete": retry_complete,
        "provider_failure": provider_failure,
        "response_invalid": response_invalid,
    }
    provider_settlements: dict[str, list[dict[str, Any]]] = {}
    settlement_roots: dict[str, list[str]] = {}
    for name, receipt_set_value in receipt_sets.items():
        settlements = [
            provider_settlement(value, policy_sha256)
            for value in receipt_set_value["receipts"]
        ]
        provider_settlements[name] = settlements
        settlement_roots[name] = [digest(value) for value in settlements]
        for receipt_value, settlement_sha256 in zip(
            receipt_set_value["receipts"], settlement_roots[name], strict=True
        ):
            receipt_value["provider_settlement_sha256"] = settlement_sha256
    receipt_roots = {
        "complete": digest(complete),
        "retry_complete": digest(retry_complete),
        "provider_failure": digest(provider_failure),
        "response_invalid": digest(response_invalid),
    }
    evidence = {
        "not_invoked": model_evidence(
            policy_sha256=policy_sha256,
            prompt_sha256=prompt_sha256,
            tool_schema_sha256=tool_schema_sha256,
            usage_status="not_invoked",
            receipt_sha256=None,
            requests=0,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd_micros=0,
            retry_count=0,
        ),
        "complete": model_evidence(
            policy_sha256=policy_sha256,
            prompt_sha256=prompt_sha256,
            tool_schema_sha256=tool_schema_sha256,
            usage_status="complete",
            receipt_sha256=receipt_roots["complete"],
            requests=2,
            prompt_tokens=2500,
            completion_tokens=320,
            cost_usd_micros=1690,
            retry_count=0,
        ),
        "retry_complete": model_evidence(
            policy_sha256=policy_sha256,
            prompt_sha256=prompt_sha256,
            tool_schema_sha256=tool_schema_sha256,
            usage_status="complete",
            receipt_sha256=receipt_roots["retry_complete"],
            requests=1,
            prompt_tokens=1000,
            completion_tokens=200,
            cost_usd_micros=1234,
            retry_count=1,
        ),
        "provider_failure": model_evidence(
            policy_sha256=policy_sha256,
            prompt_sha256=prompt_sha256,
            tool_schema_sha256=tool_schema_sha256,
            usage_status="provider_failure",
            receipt_sha256=receipt_roots["provider_failure"],
            requests=1,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd_micros=0,
            retry_count=0,
        ),
        "response_invalid": model_evidence(
            policy_sha256=policy_sha256,
            prompt_sha256=prompt_sha256,
            tool_schema_sha256=tool_schema_sha256,
            usage_status="provider_failure",
            receipt_sha256=receipt_roots["response_invalid"],
            requests=1,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd_micros=0,
            retry_count=0,
        ),
    }
    policy_vector = {
        "schema": "dittobench-coding-inference-policy-vector-v1",
        "coding_contract_version": 1,
        "weight_eligible": False,
        "policy": policy,
        "task_budgets": TASK_BUDGETS,
        "locked_requests": locked_requests,
        "provider_responses": provider_responses,
        "invalid_provider_response_projections": {
            "response_invalid": invalid_provider_response,
        },
        "normalized_provider_responses": response_projections,
        "provider_settlements": provider_settlements,
        "receipt_sets": receipt_sets,
        "model_evidence": evidence,
        "expected": {
            "prompt_sha256": prompt_sha256,
            "tool_schema_sha256": tool_schema_sha256,
            "inference_grant_sha256": policy_sha256,
            "locked_request_sha256": request_sha256,
            "normalized_response_sha256": response_sha256,
            "invalid_provider_response_sha256": {
                "response_invalid": invalid_provider_response_sha256,
            },
            "provider_settlement_sha256": settlement_roots,
            "provider_receipt_set_sha256": receipt_roots,
            "model_evidence_sha256": {
                name: digest(value) for name, value in evidence.items()
            },
        },
    }
    return miner_vector, policy_vector


def rendered(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    miner, policy = vectors()
    outputs = {
        TESTDATA / "coding_inference_miner_v1.json": rendered(miner),
        TESTDATA / "coding_inference_policy_v1.json": rendered(policy),
        TESTDATA / "coding_inference_policy_locked_v1.json": rendered(policy["policy"]),
    }
    if args.check:
        drift = [
            path
            for path, body in outputs.items()
            if not path.exists() or path.read_text(encoding="utf-8") != body
        ]
        if drift:
            for path in drift:
                print(f"drift: {path.relative_to(ROOT)}")
            return 1
        print("coding inference vectors are current")
        return 0
    for path, body in outputs.items():
        path.write_text(body, encoding="utf-8")
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
