"""Live OpenRouter proofs for grandmaster-like chat shapes.

Grandmaster v37 sends ``reasoning_effort`` on every chat and echoes
OpenRouter ``tool_calls`` including ``index``. These tests hit the real
API so we know which shapes 400 and heal those shapes before the provider,
instead of waiting for a 400 retry.

Not collected in CI: ``needs_openrouter`` is deselected by default.

Run:

    cd apps/platform
    uv run pytest ditto/tests/integration/test_openrouter_grandmaster_healing.py \\
        -o addopts= -m needs_openrouter -n0
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from ditto.api_server.endpoints.inference import (
    _locked_upstream_payload,
    _validate_request_schema,
)

pytestmark = pytest.mark.needs_openrouter

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_MODEL = "openai/gpt-oss-20b"
_TIMEOUT = 60.0


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        pytest.skip("OPENROUTER_API_KEY is required")
    return key


def _grandmaster_messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "You are a retrieval agent. Reply with ok."},
        {"role": "user", "content": "Search memory for ok, then reply with ok."},
        {
            "role": "assistant",
            "content": "",
            "reasoning": "I should call search_memory.",
            "tool_calls": [
                {
                    "id": "call_search_memory_1",
                    "type": "function",
                    "index": 0,
                    "function": {
                        "name": "search_memory",
                        "arguments": '{"query":"ok"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_search_memory_1",
            "name": "search_memory",
            "content": "ok",
        },
        {"role": "user", "content": "Reply with the single word ok."},
    ]


def _grandmaster_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "search_memory",
                "description": "Search long-term memory.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]


def _grandmaster_payload(**extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": _MODEL,
        "messages": _grandmaster_messages(),
        "tools": _grandmaster_tools(),
        "temperature": 0,
        "max_tokens": 8,
        "reasoning_effort": "medium",
    }
    payload.update(extra)
    return payload


def _post_openrouter(payload: dict[str, Any]) -> tuple[int, str]:
    """Return (status, error message). Never include the API key in output."""
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://heyditto.ai/",
        "X-OpenRouter-Title": "Ditto",
    }
    with httpx.Client(timeout=_TIMEOUT) as client:
        response = client.post(_OPENROUTER_URL, headers=headers, json=payload)
    message = ""
    try:
        decoded = response.json()
    except ValueError:
        decoded = None
    if isinstance(decoded, dict):
        error = decoded.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(decoded.get("message"), str):
            message = decoded["message"]
    return response.status_code, message


def test_openrouter_rejects_conflicting_reasoning_aliases() -> None:
    """The only proven-bad grandmaster-adjacent shape: both aliases disagree."""
    status, message = _post_openrouter(
        {
            "model": _MODEL,
            "messages": [{"role": "user", "content": "Reply with ok."}],
            "temperature": 0,
            "max_tokens": 8,
            "reasoning": {"effort": "high"},
            "reasoning_effort": "low",
        }
    )
    assert status == 400, message
    assert "reasoning_effort" in message
    assert "reasoning.effort" in message
    assert "conflicting" in message.lower()


def test_openrouter_accepts_grandmaster_echo_payload() -> None:
    """Index, extra assistant reasoning, and flat effort are not 400s."""
    status, message = _post_openrouter(_grandmaster_payload())
    assert status == 200, message


def test_openrouter_accepts_locked_nested_reasoning() -> None:
    """The scoring-lane pin (nested effort + exclude) is a valid provider shape."""
    status, message = _post_openrouter(
        {
            "model": _MODEL,
            "messages": [{"role": "user", "content": "Reply with ok."}],
            "temperature": 0,
            "max_tokens": 8,
            "n": 1,
            "stream": False,
            "reasoning": {"effort": "medium", "exclude": True},
        }
    )
    assert status == 200, message


def test_healed_conflict_is_accepted_by_openrouter() -> None:
    """Detect the bad shape, collapse to nested, and skip the provider 400."""
    payload = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": "Reply with ok."}],
        "temperature": 0,
        "max_tokens": 8,
        "reasoning": {"effort": "high"},
        "reasoning_effort": "low",
    }
    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model=_MODEL, max_tokens=8, bench_version=9
    )
    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": "high", "exclude": True}
    status, message = _post_openrouter(upstream)
    assert status == 200, message


def test_healed_grandmaster_payload_is_accepted_by_openrouter() -> None:
    payload = _grandmaster_payload()
    _validate_request_schema(payload)
    upstream = _locked_upstream_payload(
        payload, model=_MODEL, max_tokens=8, bench_version=9
    )
    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": "medium", "exclude": True}
    assert upstream["messages"][2]["tool_calls"][0]["index"] == 0
    assert upstream["messages"][2]["reasoning"] == "I should call search_memory."
    assert "name" not in upstream["messages"][3]
    status, message = _post_openrouter(upstream)
    assert status == 200, message
