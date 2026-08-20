"""Live OpenRouter proofs for Crown-like ``reasoning_content`` traces.

Crown-v4's CompletionsClient recall emits ``reasoning_content`` on assistant
messages (rig-core ``AssistantContent::Reasoning``). These tests hit the real
API so we know the field is a 200, and that the scoring-lane lock still heals
request-level ``reasoning_effort`` without stripping the traces.

Not collected in CI: ``needs_openrouter`` is deselected by default.

Run:

    cd apps/platform
    uv run pytest ditto/tests/integration/test_openrouter_reasoning_content.py \\
        -o addopts= -m needs_openrouter -n0
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from ditto.tests.integration.harnesses.reasoning_trace_agent import (
    DEFAULT_TRACE,
    ReasoningTraceAgent,
)

pytestmark = pytest.mark.needs_openrouter

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_TIMEOUT = 60.0


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        pytest.skip("OPENROUTER_API_KEY is required")
    return key


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


def test_openrouter_accepts_crown_reasoning_content_payload() -> None:
    """rig-core's ``reasoning_content`` alias is a valid provider history field."""
    payload = ReasoningTraceAgent().recall_after_tool_call()
    status, message = _post_openrouter(payload)
    assert status == 200, message


def test_openrouter_accepts_reasoning_and_reasoning_content_aliases() -> None:
    """OpenRouter documents the two string fields as identical aliases."""
    payload = ReasoningTraceAgent().recall_after_tool_call(
        include_openrouter_alias=True
    )
    status, message = _post_openrouter(payload)
    assert status == 200, message


def test_healed_crown_payload_is_accepted_by_openrouter() -> None:
    """Lock heals ``reasoning_effort`` and still forwards the traces."""
    agent = ReasoningTraceAgent()
    payload = agent.recall_after_tool_call()
    upstream = agent.heal_for_openrouter(payload)
    assistant = upstream["messages"][2]
    tool = upstream["messages"][3]
    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": "medium", "exclude": True}
    assert assistant["reasoning_content"] == DEFAULT_TRACE
    assert assistant["tool_calls"][0]["index"] == 0
    assert "name" not in tool
    status, message = _post_openrouter(upstream)
    assert status == 200, message


def test_healed_conflicting_aliases_keep_reasoning_content() -> None:
    """Request-level nested-vs-flat heal must not wipe message traces."""
    agent = ReasoningTraceAgent()
    payload = agent.recall_after_tool_call()
    payload["reasoning"] = {"effort": "high"}
    payload["reasoning_effort"] = "low"
    upstream = agent.heal_for_openrouter(payload)
    assert "reasoning_effort" not in upstream
    assert upstream["reasoning"] == {"effort": "high", "exclude": True}
    assert upstream["messages"][2]["reasoning_content"] == DEFAULT_TRACE
    status, message = _post_openrouter(upstream)
    assert status == 200, message
