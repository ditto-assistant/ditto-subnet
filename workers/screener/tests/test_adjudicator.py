"""Automated clear/reject adjudication of a held source review."""

from __future__ import annotations

import asyncio
import io
import json
import os
import tarfile
from pathlib import Path

import httpx
import pytest

import ditto_screener.adjudicator as adjudicator_module
from ditto_screener.adjudicator import (
    ADJUDICATOR_PROMPT_REVISION,
    SourceReviewAdjudicator,
    _adjudicator_tools_for_policy,
    _compacted_adjudicator_messages,
    _system_prompt,
    adjudicator_prompt_revision,
    build_adjudicator,
)


def test_miner_reason_preserves_complete_explanation_and_paragraphs() -> None:
    reason = (
        "The served path bypasses the deciding model.\n\n"
        + "Relevant source detail. " * 80
    )
    verdict = adjudicator_module._verdict_from({"decision": "reject", "reason": reason})
    assert verdict.reason == reason.strip()
    assert len(verdict.reason) > 600


def test_oversized_reason_is_refused_instead_of_silently_truncated() -> None:
    with pytest.raises(ValueError, match="exceeds 8000"):
        adjudicator_module._verdict_from({"decision": "reject", "reason": "x" * 8001})


_SOURCE = "\n".join(
    [
        "// leading comment",
        "use std::collections::HashMap;",
        "",
        "fn serve(request: &Request) -> String {",
        "    let records = retrieve(request);",
        "    let reply = call_model(request, &records);",
        "    reply.text",
        "}",
        "",
        "fn rerank(candidates: Vec<Doc>) -> Vec<Doc> {",
        "    candidates.into_iter().take(8).collect()",
        "}",
    ]
)


def _archive(tmp_path: Path) -> str:
    path = tmp_path / "agent.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, value in {
            "Cargo.toml": '[package]\nname="agent"\nversion="0.1.0"\n',
            "Dockerfile": "FROM scratch\n",
            "src/main.rs": _SOURCE,
        }.items():
            raw = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return str(path)


def _key(tmp_path: Path) -> Path:
    key = tmp_path / "key"
    key.write_text("sk-test-private-adjudicator")
    os.chmod(key, 0o600)
    return key


def _call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "id": f"{name}-1",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _transport(scripted: list[list[dict[str, object]]]) -> httpx.MockTransport:
    turns = iter(scripted)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": next(turns),
                        }
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


def _adjudicator(key: Path, transport: httpx.MockTransport) -> SourceReviewAdjudicator:
    return SourceReviewAdjudicator(
        api_key_file=str(key),
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=10,
        max_steps=6,
        transport=transport,
    )


def test_adjudicator_compacts_old_tool_turns_but_keeps_the_case_brief() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "court-policy"},
        {"role": "user", "content": "case-brief"},
    ]
    for turn in range(6):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [_call("search", {"query": f"needle-{turn}"})],
                },
                {
                    "role": "tool",
                    "tool_call_id": "search-1",
                    "content": f"large-output-{turn}",
                },
            ]
        )

    compacted = _compacted_adjudicator_messages(messages)

    assert compacted[:2] == messages[:2]
    assert "Earlier inspection turns were compacted" in str(compacted[2]["content"])
    assert "large-output-0" not in json.dumps(compacted)
    assert "large-output-5" in json.dumps(compacted)
    assert sum(row.get("role") == "assistant" for row in compacted) == 3


async def test_request_uses_provider_supported_completion_parameter(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                _call(
                                    "submit_adjudication",
                                    {
                                        "decision": "clear",
                                        "clear_clause": (
                                            "retrieval_ranking_not_family_engine"
                                        ),
                                        "reason": "bounded request contract test",
                                        "citations": [],
                                    },
                                )
                            ],
                        }
                    }
                ]
            },
        )

    await _adjudicator(_key(tmp_path), httpx.MockTransport(handler)).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )

    assert len(requests) == 1
    assert requests[0]["max_tokens"] == 6_000
    assert "max_completion_tokens" not in requests[0]
    assert requests[0]["tool_choice"] == "required"
    assert requests[0]["stream"] is True
    assert requests[0]["provider"] == {
        "allow_fallbacks": True,
        "data_collection": "deny",
        "require_parameters": True,
    }


async def test_streamed_tool_call_is_assembled_before_verdict(tmp_path: Path) -> None:
    arguments = json.dumps(
        {
            "decision": "clear",
            "clear_clause": ("retrieval_ranking_not_family_engine"),
            "reason": "The served path keeps model authority.",
            "citations": [],
        }
    )
    fragments = [arguments[:20], arguments[20:]]
    events = [
        {
            "model": "z-ai/glm-5.3-flash",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "verdict-1",
                                "type": "function",
                                "function": {
                                    "name": "submit_",
                                    "arguments": fragments[0],
                                },
                            }
                        ]
                    }
                }
            ],
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "adjudication",
                                    "arguments": fragments[1],
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        },
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    body += "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        message = await _adjudicator(
            _key(tmp_path), httpx.MockTransport(handler)
        )._completion_message(client, "sk-test", [], timeout=10)
    assert message["tool_calls"] == [
        _call("submit_adjudication", json.loads(arguments)) | {"id": "verdict-1"}
    ]


@pytest.mark.parametrize("streamed", [False, True])
async def test_truncated_completion_cannot_settle_a_verdict(
    tmp_path: Path, streamed: bool
) -> None:
    call = _call(
        "submit_adjudication",
        {
            "decision": "clear",
            "clear_clause": "model_authors_graded_slot",
            "reason": "syntactically complete but provider reports truncation",
            "citations": [{"path": "src/main.rs", "line": 6}],
        },
    )
    if streamed:
        event = {
            "choices": [
                {
                    "delta": {"tool_calls": [{"index": 0, **call}]},
                    "finish_reason": "length",
                }
            ]
        }
        response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
        )
    else:
        response = httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"role": "assistant", "tool_calls": [call]},
                    }
                ]
            },
        )
    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(lambda _request: response)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN], ledger_final=True)
    assert result.decision == "escalate"
    assert result.escalation_code == "adjudicator-failed"


async def test_truncated_stream_cannot_clear(tmp_path: Path) -> None:
    attempts = 0
    body = (
        "data: "
        + json.dumps(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "x",
                                    "function": {
                                        "name": "submit_adjudication",
                                        "arguments": '{"decision":"clear"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        )
        + "\n\n"
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])
    assert result.decision == "escalate"
    assert result.escalation_code == "adjudicator-failed"
    assert attempts == 2


@pytest.mark.parametrize("streamed", [False, True])
async def test_large_wire_with_small_tool_call_keeps_verdict(
    tmp_path: Path, streamed: bool
) -> None:
    call = _call("submit_adjudication", {"decision": "clear", "reason": "valid"})
    if streamed:
        noise = json.dumps({"choices": [{"delta": {"content": "x" * 500}}]})
        tool = json.dumps(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, **call}]}}]}
        )
        body = (f"data: {noise}\n\n" * 1100) + f"data: {tool}\n\ndata: [DONE]\n\n"
        assert 512_000 < len(body.encode()) < 2_000_000
        response = httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=body
        )
    else:
        response = httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "x" * 550_000, "tool_calls": [call]}}
                ]
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        message = await _adjudicator(
            _key(tmp_path), httpx.MockTransport(lambda _request: response)
        )._completion_message(client, "sk-test", [], timeout=10)
    assert message["tool_calls"] == [call]
    assert message.get("content") is None


@pytest.mark.parametrize("streamed", [False, True])
async def test_oversized_tool_arguments_still_fail_closed(
    tmp_path: Path, streamed: bool
) -> None:
    call = _call("submit_adjudication", {"decision": "clear", "reason": "x" * 520_000})
    if streamed:
        event = {"choices": [{"delta": {"tool_calls": [{"index": 0, **call}]}}]}
        response = httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
        )
    else:
        response = httpx.Response(
            200, json={"choices": [{"message": {"tool_calls": [call]}}]}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        with pytest.raises(ValueError, match="exceeded response bound"):
            await _adjudicator(
                _key(tmp_path), httpx.MockTransport(lambda _request: response)
            )._completion_message(client, "sk-test", [], timeout=10)


async def test_stream_wire_limit_still_fails_closed(tmp_path: Path) -> None:
    noise = json.dumps({"choices": [{"delta": {"content": "x" * 1_000}}]})
    body = f"data: {noise}\n\n" * 2_000
    assert len(body.encode()) > adjudicator_module._MAX_COMPLETION_STREAM_BYTES
    response = httpx.Response(
        200, headers={"content-type": "text/event-stream"}, text=body
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response)
    ) as client:
        with pytest.raises(ValueError, match="exceeded response bound"):
            await _adjudicator(
                _key(tmp_path), httpx.MockTransport(lambda _request: response)
            )._completion_message(client, "sk-test", [], timeout=10)


async def test_gateway_rejecting_stream_uses_one_buffered_attempt(
    tmp_path: Path,
) -> None:
    modes: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        streaming = json.loads(request.content)["stream"]
        modes.append(streaming)
        if streaming:
            return httpx.Response(400, json={"error": "stream not supported"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                _call(
                                    "submit_adjudication",
                                    {
                                        "decision": "clear",
                                        "clear_clause": (
                                            "retrieval_ranking_not_family_engine"
                                        ),
                                        "reason": "fallback contract",
                                        "citations": [],
                                    },
                                )
                            ],
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        message = await _adjudicator(
            _key(tmp_path), httpx.MockTransport(handler)
        )._completion_message(client, "sk-test", [], timeout=10)
    assert modes == [True, False]
    assert message["tool_calls"][0]["function"]["name"] == "submit_adjudication"


async def test_deadline_bounds_a_completion_and_its_retry(tmp_path: Path) -> None:
    """A slow provider cannot spend past the court's reserved lease window."""
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={})

    deadline = asyncio.get_running_loop().time() + 0.03
    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN], deadline=deadline)

    assert result.decision == "escalate"
    assert result.clear_clause is None
    assert result.escalation_code == "adjudicator-failed"
    assert result.run_diagnostic is not None
    assert result.run_diagnostic.error_class == "TimeoutError"
    assert result.run_diagnostic.timeout_stage == "completion"
    assert result.run_diagnostic.http_status is None
    assert result.run_diagnostic.final_tool_call_returned is None
    assert result.run_diagnostic.model == "z-ai/glm-5.3-flash"
    assert result.run_diagnostic.provider == "openrouter"
    assert (
        result.canonical_digest()
        == result.model_copy(update={"run_diagnostic": None}).canonical_digest()
    )
    assert requests == 1


def test_tool_call_rejects_parsed_object_arguments() -> None:
    with pytest.raises(ValueError, match="arguments are invalid"):
        adjudicator_module._tool_call(
            {
                "id": "submit-1",
                "function": {
                    "name": "submit_adjudication",
                    "arguments": {
                        "decision": "clear",
                        "reason": "model text that must not be stored",
                    },
                },
            }
        )


async def test_object_tool_arguments_stay_fail_closed_without_their_text(
    tmp_path: Path,
) -> None:
    """A parsed object is recorded as a contract failure, not stored or settled."""
    secret = "model text that must not be stored"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "usage": {"prompt_tokens": 11, "completion_tokens": 4},
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": secret,
                            "tool_calls": [
                                {
                                    "id": "submit-1",
                                    "type": "function",
                                    "function": {
                                        "name": "submit_adjudication",
                                        "arguments": {
                                            "decision": "clear",
                                            "reason": secret,
                                        },
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])

    assert result.decision == "escalate"
    assert result.escalation_code == "adjudicator-failed"
    diagnostic = result.run_diagnostic
    assert diagnostic is not None
    assert diagnostic.error_class == "ValueError"
    assert diagnostic.timeout_stage == "response"
    assert diagnostic.http_status is None
    assert diagnostic.elapsed_ms >= 0
    assert diagnostic.prompt_tokens == 11
    assert diagnostic.completion_tokens == 4
    assert diagnostic.final_tool_call_returned is True
    assert secret not in result.model_dump_json()
    assert secret not in diagnostic.model_dump_json()


async def test_the_serving_upstream_is_recorded_on_a_failed_court_run(
    tmp_path: Path,
) -> None:
    """One model is routed across many upstreams; the trace has to name one.

    The gateway reports the upstream that served each call and may fail over
    between them per request, so without this a burst of court failures cannot
    be attributed to a fleet route rather than to the artifacts.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "provider": "Sail Research",
                "usage": {"prompt_tokens": 11, "completion_tokens": 4},
                "choices": [{"message": {"content": "not a tool call"}}],
            },
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])

    assert result.decision == "escalate"
    diagnostic = result.run_diagnostic
    assert diagnostic is not None
    # Normalized to the same bounded slug every other identifier here uses.
    assert diagnostic.upstream == "sail-research"
    # The gateway stays its own field; one is the route, the other is the door.
    assert diagnostic.provider == "openrouter"


async def test_an_earlier_step_does_not_own_a_later_timeout(
    tmp_path: Path,
) -> None:
    """A run that answered once and then hung must not blame the first upstream.

    The trace spans the whole court run, so without a per-request reset the
    upstream that served a completed step would be named as the one that served
    the call which actually failed. A timeout has no response body, so the
    honest answer is that the upstream is unknown.
    """
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                200,
                json={
                    "provider": "Together",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [_call("list_files", {"prefix": ""})],
                            }
                        }
                    ],
                },
            )
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={})

    deadline = asyncio.get_running_loop().time() + 0.15
    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN], deadline=deadline)

    assert requests >= 2
    assert result.decision == "escalate"
    assert result.escalation_code == "adjudicator-failed"
    diagnostic = result.run_diagnostic
    assert diagnostic is not None
    assert diagnostic.error_class == "TimeoutError"
    # The first step was served by Together; the failing one was served by
    # nobody that answered, so the field stays unknown rather than inheriting.
    assert diagnostic.upstream is None
    assert diagnostic.prompt_tokens is None
    assert diagnostic.completion_tokens is None
    assert diagnostic.final_tool_call_returned is None


async def test_stream_without_tool_records_safe_contract_diagnostic(
    tmp_path: Path,
) -> None:
    secret = "private model text"
    event = {
        "provider": "Together",
        "usage": {"prompt_tokens": 17, "completion_tokens": 2},
        "choices": [{"delta": {"content": secret}, "finish_reason": "stop"}],
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])
    assert result.decision == "escalate"
    assert result.run_diagnostic is not None
    assert result.run_diagnostic.failure_code == "stream-no-tool-call"
    assert result.run_diagnostic.final_tool_call_returned is False
    assert result.run_diagnostic.prompt_tokens == 17
    assert result.run_diagnostic.completion_tokens == 2
    assert secret not in result.model_dump_json()


async def test_a_provider_fault_inside_a_200_still_names_its_upstream(
    tmp_path: Path,
) -> None:
    """The body is rejected, but the upstream that sent it is the point.

    A relayed ``provider_error`` is a fault of the upstream that served the
    call, so the trace has to keep the name even though the body itself is
    thrown away.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "provider": "Io Net",
                "error": {"code": "provider_error", "message": "upstream failed"},
            },
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])

    assert result.decision == "escalate"
    diagnostic = result.run_diagnostic
    assert diagnostic is not None
    assert diagnostic.upstream == "io-net"
    assert diagnostic.failure_code == "provider-body-error"
    assert "upstream failed" not in diagnostic.model_dump_json()


async def test_stream_provider_error_retries_and_accepts_only_complete_tool_call(
    tmp_path: Path,
) -> None:
    attempts = 0
    arguments = {"decision": "clear", "reason": "Model authority is retained."}

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            event = {
                "provider": "Together",
                "error": {"message": "private provider detail"},
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
            )
        event = {
            "provider": "Friendli",
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "verdict-1",
                                "type": "function",
                                "function": {
                                    "name": "submit_adjudication",
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        message = await _adjudicator(
            _key(tmp_path), httpx.MockTransport(handler)
        )._completion_message(client, "sk-test", [], timeout=10)
    assert attempts == 2
    assert message["tool_calls"] == [
        _call("submit_adjudication", arguments) | {"id": "verdict-1"}
    ]


async def test_two_stream_provider_errors_hold_with_safe_subtype(
    tmp_path: Path,
) -> None:
    attempts = 0
    secret = "private provider detail"

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        event = {"provider": "Together", "error": {"message": secret}}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])
    assert attempts == 2
    assert result.decision == "escalate"
    assert result.run_diagnostic is not None
    assert result.run_diagnostic.failure_code == "provider-stream-error"
    assert result.run_diagnostic.upstream == "together"
    assert secret not in result.model_dump_json()


async def test_an_unusable_upstream_name_is_dropped_rather_than_stored(
    tmp_path: Path,
) -> None:
    """The name arrives in a provider response, so it is never free text."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "provider": "Sail Research <script>alert(1)</script>",
                "choices": [{"message": {"content": "not a tool call"}}],
            },
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])

    diagnostic = result.run_diagnostic
    assert diagnostic is not None
    assert diagnostic.upstream is None
    assert "script" not in diagnostic.model_dump_json()


async def test_a_failure_with_no_response_leaves_the_upstream_unknown(
    tmp_path: Path,
) -> None:
    """A request that never produced a body cannot name an upstream."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": "upstream down"}})

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])

    diagnostic = result.run_diagnostic
    assert diagnostic is not None
    assert diagnostic.http_status == 503
    assert diagnostic.upstream is None


async def test_provider_status_is_recorded_without_the_response_body(
    tmp_path: Path,
) -> None:
    secret = "prompt text that must not be stored"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"message": secret}})

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])

    assert result.decision == "escalate"
    assert result.escalation_code == "adjudicator-failed"
    assert result.reason == (
        "Automated adjudication did not complete; held for operator review"
    )
    diagnostic = result.run_diagnostic
    assert diagnostic is not None
    assert diagnostic.error_class == "HTTPStatusError"
    assert diagnostic.timeout_stage == "response"
    assert diagnostic.http_status == 503
    assert diagnostic.final_tool_call_returned is None
    assert diagnostic.prompt_tokens is None
    assert secret not in result.model_dump_json()
    assert "error" not in diagnostic.model_dump(mode="json")


@pytest.mark.parametrize(
    ("error_code", "ledger_final"),
    [
        ("source-review-lease-budget-exhausted", False),
        ("source-review-inconsistent-verdict", False),
        (None, True),
    ],
)
async def test_evidence_bearing_ledger_uses_one_preloaded_final_turn(
    tmp_path: Path,
    error_code: str | None,
    ledger_final: bool,
) -> None:
    """L4 decides retained evidence, regardless of why upstream stopped."""
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                _call(
                                    "submit_adjudication",
                                    {
                                        "decision": "clear",
                                        "clear_clause": "model_authors_graded_slot",
                                        "reason": "the served model writes the reply",
                                        "citations": [
                                            {"path": "src/main.rs", "line": 6}
                                        ],
                                    },
                                )
                            ],
                        }
                    }
                ]
            },
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(
        _archive(tmp_path),
        notes=[
            {
                "kind": "concern",
                "category": "benchmark_emulation",
                "path": "src/main.rs",
                "line": 6,
                "summary": "review the served response assembly",
            }
        ],
        error_code=error_code,
        ledger_final=ledger_final,
    )

    assert result.decision == "clear"
    assert result.citations[0].path == "src/main.rs"
    assert len(requests) == 1
    assert [tool["function"]["name"] for tool in requests[0]["tools"]] == [
        "submit_adjudication"
    ]
    assert "Preloaded source evidence" in str(requests[0]["messages"])


@pytest.mark.parametrize("decision", ["clear", "reject"])
async def test_later_unread_concern_cannot_be_silently_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    """One-turn L4 may preload fewer locations than the retained ledger."""
    monkeypatch.setattr(adjudicator_module, "_MAX_PRELOADED_LEDGER_LOCATIONS", 1)
    arguments: dict[str, object] = {
        "decision": decision,
        "reason": "The first source excerpt supports this verdict.",
        "citations": [{"path": "src/main.rs", "line": 6}],
    }
    if decision == "clear":
        arguments["clear_clause"] = "model_authors_graded_slot"
    else:
        arguments["reject_invariant"] = "i5_production_engine"

    def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert "src/main.rs" in str(sent["messages"])
        evidence = str(sent["messages"][1]["content"]).split(
            "Preloaded source evidence:\n", 1
        )[1]
        assert '"path": "Dockerfile"' not in evidence
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [_call("submit_adjudication", arguments)],
                        }
                    }
                ]
            },
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(
        _archive(tmp_path),
        notes=[
            {"kind": "observation", "path": "src/main.rs", "line": 6},
            {"kind": "concern", "path": "Dockerfile", "line": 1},
        ],
        ledger_final=True,
    )
    if decision == "clear":
        assert result.decision == "escalate"
        assert result.escalation_code == "adjudicator-evidence-incomplete"
    else:
        assert result.decision == "reject"


@pytest.mark.parametrize(
    "concern",
    [
        {"kind": "concern", "summary": "path omitted"},
        {"kind": "concern", "path": "src/main.rs", "line": "6"},
        {"kind": "concern", "path": "src/main.rs", "line": True},
    ],
)
def test_malformed_concern_is_not_mistaken_for_preloaded_source(
    concern: dict[str, object],
) -> None:
    assert adjudicator_module._has_unreviewed_lead(
        [concern], None, {("src/main.rs", 6)}
    )


def test_finding_evidence_not_in_preloaded_ledger_blocks_clear() -> None:
    assert adjudicator_module._has_unreviewed_lead(
        [{"kind": "observation", "path": "src/main.rs", "line": 6}],
        {"evidence": [{"path": "Dockerfile", "line": 1}]},
        {("src/main.rs", 6)},
    )


async def test_budget_terminated_review_without_evidence_settles_immediately(
    tmp_path: Path,
) -> None:
    """No retained evidence means no model rediscovery or miner-facing retry."""
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(500)

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(
        _archive(tmp_path),
        notes=[],
        error_code="source-review-lease-budget-exhausted",
    )

    assert result.decision == "escalate"
    assert result.clear_clause is None
    assert result.escalation_code == "adjudicator-no-evidence"
    assert requests == 0


_CONCERN = {
    "kind": "concern",
    "category": "benchmark_emulation",
    "path": "src/main.rs",
    "line": 10,
    "summary": "reranking looked like an answer-family engine",
}


def test_adjudicator_prompt_treats_forced_choice_as_i7() -> None:
    policy_v10 = _system_prompt(10)
    assert "required_*tool" in policy_v10
    assert "ForcedChoiceModel" in policy_v10
    assert "forbidding every other tool" in policy_v10
    assert adjudicator_prompt_revision(10) == "adjudicator-v4-policy-v10"
    assert adjudicator_prompt_revision(11) == "adjudicator-v4-policy-v11"
    assert adjudicator_prompt_revision(12) == "adjudicator-v4-policy-v12"
    assert adjudicator_prompt_revision(13) == "adjudicator-v6-policy-v13"
    assert ADJUDICATOR_PROMPT_REVISION == "adjudicator-v6-policy-v13"


def test_adjudicator_policy_v12_narrows_plain_normalization() -> None:
    policy_v11 = _system_prompt(11)
    policy_v12 = _system_prompt(12)
    assert policy_v12.startswith(policy_v11)
    assert "plain_answer_normalization false-positive class is NARROWED" in policy_v12
    assert "scorer_field_rewritten" in policy_v12
    assert "format-only model" in policy_v12
    assert "NARROWED" not in policy_v11
    assert "NARROWED" not in _system_prompt(10)
    with pytest.raises(ValueError, match="not implemented by this build"):
        adjudicator_prompt_revision(14)


def test_adjudicator_policy_v13_adds_i8_and_incomplete_review_boundary() -> None:
    policy_v12 = _system_prompt(12)
    policy_v13 = _system_prompt(13)

    assert policy_v13.startswith(policy_v12)
    assert "I8 is evaluation independence" in policy_v13
    assert "always-on benchmark-specific recipe is activated on every request" in (
        policy_v13
    )
    assert "withhold submit_adjudication" in policy_v13
    assert "`bench_version` alter substantive retrieval" in policy_v13
    assert "path-and-digest provenance" in policy_v13
    assert "omission of its duplicate README" in policy_v13
    assert "null compact score" in policy_v13

    legacy_submit = _adjudicator_tools_for_policy(12, decision_only=True)[0]
    current_submit = _adjudicator_tools_for_policy(13, decision_only=True)[0]
    legacy_invariants = legacy_submit["function"]["parameters"]["properties"][
        "reject_invariant"
    ]["enum"]
    current_invariants = current_submit["function"]["parameters"]["properties"][
        "reject_invariant"
    ]["enum"]
    assert "i8_evaluation_independence" not in legacy_invariants
    assert "i8_evaluation_independence" in current_invariants


async def test_legacy_policy_refuses_a_v13_only_adjudication_basis(
    tmp_path: Path,
) -> None:
    result = await _adjudicator(
        _key(tmp_path),
        _transport(
            [
                [
                    _call(
                        "submit_adjudication",
                        {
                            "decision": "reject",
                            "reject_invariant": "i8_evaluation_independence",
                            "reason": "provider returned a newer-policy basis",
                            "citations": [{"path": "src/main.rs", "line": 10}],
                        },
                    )
                ]
            ]
        ),
    ).adjudicate(
        _archive(tmp_path),
        notes=[_CONCERN],
        policy_version=12,
        ledger_final=True,
    )

    assert result.decision == "escalate"
    assert result.escalation_code == "verdict-contract-failed"


async def test_v11_court_request_and_signed_verdict_bind_policy_version(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                _call(
                                    "submit_adjudication",
                                    {
                                        "decision": "clear",
                                        "clear_clause": "model_authors_graded_slot",
                                        "reason": "no executable source conclusion",
                                        "citations": [],
                                    },
                                )
                            ],
                        }
                    }
                ]
            },
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN], policy_version=11)

    assert "Policy v11 additions" in str(requests[0]["messages"])
    assert (
        "planner authorship does not save a forced executor"
        in str(requests[0]["messages"]).lower()
    )
    assert result.policy_version == 11
    assert result.prompt_revision == "adjudicator-v4-policy-v11"


async def test_clear_names_a_published_clause_and_read_lines(tmp_path: Path) -> None:
    transport = _transport(
        [
            [
                _call(
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 12},
                )
            ],
            [
                _call(
                    "submit_adjudication",
                    {
                        "decision": "clear",
                        "clear_clause": "retrieval_ranking_not_family_engine",
                        "reason": (
                            "rerank only truncates a candidate list; the model "
                            "still authors the served reply from this user's "
                            "records at src/main.rs:6"
                        ),
                        "citations": [
                            {"path": "src/main.rs", "line": 6},
                            {"path": "src/main.rs", "line": 11},
                        ],
                    },
                )
            ],
        ]
    )

    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )

    assert result.decision == "clear"
    assert result.clear_clause == "retrieval_ranking_not_family_engine"
    assert result.reject_invariant is None
    assert [(item.path, item.line) for item in result.citations] == [
        ("src/main.rs", 6),
        ("src/main.rs", 11),
    ]
    assert result.notes_considered == 1
    assert result.prompt_revision == ADJUDICATOR_PROMPT_REVISION


async def test_reject_names_the_breached_invariant(tmp_path: Path) -> None:
    transport = _transport(
        [
            [
                _call(
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 12},
                )
            ],
            [
                _call(
                    "submit_adjudication",
                    {
                        "decision": "reject",
                        "reject_invariant": "i5_production_engine",
                        "reason": "closed answer-family table at src/main.rs:11",
                        "citations": [{"path": "src/main.rs", "line": 11}],
                    },
                )
            ],
        ]
    )

    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )

    assert result.decision == "reject"
    assert result.reject_invariant == "i5_production_engine"
    assert result.clear_clause is None


async def test_a_citation_the_court_never_read_clears_without_proof(
    tmp_path: Path,
) -> None:
    """The strongest guard: a decision may only rest on lines it was shown."""
    transport = _transport(
        [
            [
                _call(
                    "submit_adjudication",
                    {
                        "decision": "reject",
                        "reject_invariant": "i4_derived_value_authority",
                        "reason": "asserted without reading anything",
                        "citations": [{"path": "src/main.rs", "line": 6}],
                    },
                )
            ]
        ]
    )

    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )

    assert result.decision == "escalate"
    assert result.clear_clause is None


async def test_same_turn_read_cannot_certify_a_verdict(tmp_path: Path) -> None:
    """The model chose both calls before it could see the read_file result."""
    transport = _transport(
        [
            [
                _call(
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 12},
                ),
                _call(
                    "submit_adjudication",
                    {
                        "decision": "clear",
                        "clear_clause": "model_authors_graded_slot",
                        "reason": "asserted in the same turn as the source read",
                        "citations": [{"path": "src/main.rs", "line": 6}],
                    },
                ),
            ]
        ]
    )
    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )
    assert result.decision == "escalate"
    assert result.escalation_code == "adjudicator-failed"


async def test_decision_only_rejects_duplicate_verdicts(tmp_path: Path) -> None:
    transport = _transport(
        [
            [
                _call(
                    "submit_adjudication",
                    {
                        "decision": "clear",
                        "clear_clause": "model_authors_graded_slot",
                        "reason": "first verdict",
                        "citations": [{"path": "src/main.rs", "line": 6}],
                    },
                ),
                _call(
                    "submit_adjudication",
                    {
                        "decision": "reject",
                        "reject_invariant": "i5_production_engine",
                        "reason": "contradictory second verdict",
                        "citations": [{"path": "src/main.rs", "line": 11}],
                    },
                ),
            ]
        ]
    )
    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN], ledger_final=True
    )
    assert result.decision == "escalate"
    assert result.escalation_code == "adjudicator-failed"


async def test_a_hallucinated_path_clears_without_proof(tmp_path: Path) -> None:
    transport = _transport(
        [
            [
                _call(
                    "submit_adjudication",
                    {
                        "decision": "reject",
                        "reject_invariant": "i5_production_engine",
                        "reason": "table in a file that does not exist",
                        "citations": [{"path": "src/families.rs", "line": 4}],
                    },
                )
            ]
        ]
    )

    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )

    assert result.decision == "escalate"
    assert result.clear_clause is None


async def test_a_line_past_the_end_clears_without_proof(tmp_path: Path) -> None:
    """The tools only serve real lines, so this lands on the unread guard."""
    transport = _transport(
        [
            [
                _call(
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 12},
                )
            ],
            [
                _call(
                    "submit_adjudication",
                    {
                        "decision": "reject",
                        "reject_invariant": "i5_production_engine",
                        "reason": "cites past the end",
                        "citations": [{"path": "src/main.rs", "line": 4000}],
                    },
                )
            ],
        ]
    )

    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )

    assert result.decision == "escalate"
    assert result.clear_clause is None


async def test_only_inert_citations_clear_without_proof(tmp_path: Path) -> None:
    """A comment and an import cannot carry a behaviour, so they prove nothing."""
    transport = _transport(
        [
            [
                _call(
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 12},
                )
            ],
            [
                _call(
                    "submit_adjudication",
                    {
                        "decision": "reject",
                        "reject_invariant": "i5_production_engine",
                        "reason": "cites a comment and an import",
                        "citations": [
                            {"path": "src/main.rs", "line": 1},
                            {"path": "src/main.rs", "line": 2},
                        ],
                    },
                )
            ],
        ]
    )

    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )

    assert result.decision == "escalate"
    assert result.clear_clause is None


async def test_a_decision_without_a_published_basis_clears_without_proof(
    tmp_path: Path,
) -> None:
    transport = _transport(
        [
            [
                _call(
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 12},
                )
            ],
            [
                _call(
                    "submit_adjudication",
                    {
                        "decision": "clear",
                        "reason": "looks fine to me",
                        "citations": [{"path": "src/main.rs", "line": 6}],
                    },
                )
            ],
        ]
    )

    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )

    assert result.decision == "escalate"
    assert result.clear_clause is None


async def test_an_uncited_decision_clears_without_proof(tmp_path: Path) -> None:
    transport = _transport(
        [
            [
                _call(
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 12},
                )
            ],
            [
                _call(
                    "submit_adjudication",
                    {
                        "decision": "clear",
                        "clear_clause": "model_authors_graded_slot",
                        "reason": "no citations at all",
                        "citations": [],
                    },
                )
            ],
        ]
    )

    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )

    assert result.decision == "escalate"
    assert result.clear_clause is None


async def test_an_exhausted_step_budget_clears_without_proof(tmp_path: Path) -> None:
    """Running out of turns settles fairly instead of holding forever."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                _call(
                                    "read_file",
                                    {
                                        "path": "src/main.rs",
                                        "start_line": 1,
                                        "end_line": 12,
                                    },
                                )
                            ],
                        }
                    }
                ]
            },
        )

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])

    assert result.decision == "escalate"
    assert result.clear_clause is None
    assert result.notes_considered == 1


async def test_a_search_hit_counts_as_reading_that_line(tmp_path: Path) -> None:
    transport = _transport(
        [
            [_call("search", {"query": "call_model"})],
            [
                _call(
                    "submit_adjudication",
                    {
                        "decision": "clear",
                        "clear_clause": "model_authors_graded_slot",
                        "reason": "the model authors the served reply",
                        "citations": [{"path": "src/main.rs", "line": 6}],
                    },
                )
            ],
        ]
    )

    result = await _adjudicator(_key(tmp_path), transport).adjudicate(
        _archive(tmp_path), notes=[_CONCERN]
    )

    assert result.decision == "clear"


async def test_a_missing_key_clears_rather_than_punishing_the_miner(
    tmp_path: Path,
) -> None:
    result = await SourceReviewAdjudicator(
        api_key_file=None, base_url="https://openrouter.test/api/v1"
    ).adjudicate(_archive(tmp_path), notes=[])

    assert result.decision == "escalate"
    assert result.clear_clause is None


async def test_a_wall_clock_timeout_clears_rather_than_holding(
    tmp_path: Path,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(
        _archive(tmp_path),
        notes=[_CONCERN],
        deadline=asyncio.get_running_loop().time() + 0.01,
    )

    assert result.decision == "escalate"
    assert result.clear_clause is None


async def test_a_stalled_completion_retries_once_then_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung model request cannot spend the entire screening lease."""
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    monkeypatch.setattr(adjudicator_module, "_MAX_COMPLETION_REQUEST_SECONDS", 0.01)
    result = await _adjudicator(
        _key(tmp_path), httpx.MockTransport(handler)
    ).adjudicate(_archive(tmp_path), notes=[_CONCERN])

    assert attempts == 2
    assert result.decision == "escalate"
    assert result.clear_clause is None


@pytest.mark.parametrize("mode", ("off", "shadow", "enforce"))
def test_the_court_is_only_built_when_an_operator_turns_it_on(
    make_config, mode: str
) -> None:
    built = build_adjudicator(make_config(adjudicator_mode=mode))

    assert (built is None) == (mode == "off")


def test_the_court_uses_the_audited_deep_review_completion_budget(make_config) -> None:
    built = build_adjudicator(
        make_config(
            adjudicator_mode="enforce",
            l2_max_completion_tokens=16_384,
        )
    )

    assert built is not None
    assert built._max_completion_tokens == 16_384
