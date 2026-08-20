"""Fake retrieval agent that echoes assistant reasoning traces.

Crown-v4 (ditto-harness ``ba5b463`` / rig-core 0.38 CompletionsClient)
serializes recall as ``{model, messages, seed, temperature}``. When history
contains ``AssistantContent::Reasoning``, the next request emits
``reasoning_content`` on assistant messages — the DeepSeek/OpenAI-compat
alias of OpenRouter's ``message.reasoning``. OpenRouter also accepts the
structured ``reasoning_details`` block.

This harness reproduces that recall shape so Platform / model-relay can
heal request-level aliases without stripping the traces, and so live
OpenRouter tests can prove the healed payload is a 200.

Not collected in CI. Nightly / operator:

    cd apps/platform
    uv run python -m ditto.tests.integration.harnesses.reasoning_trace_agent
    uv run pytest ditto/tests/integration/test_openrouter_reasoning_content.py \\
        -o addopts= -m needs_openrouter -n0
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Literal

from ditto.api_server.endpoints.inference import (
    _locked_upstream_payload,
    _validate_request_schema,
)

TraceField = Literal["reasoning", "reasoning_content"]

DEFAULT_MODEL = "openai/gpt-oss-20b"
DEFAULT_TRACE = "I should call search_memory."
_SEARCH_CALL_ID = "call_search_memory_1"


def search_memory_tools() -> list[dict[str, Any]]:
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


def reasoning_details_for(text: str) -> list[dict[str, Any]]:
    """Minimal OpenRouter ``reasoning.text`` block. Not a live model echo."""
    return [
        {
            "type": "reasoning.text",
            "text": text,
            "format": "unknown",
            "index": 0,
        }
    ]


@dataclass(frozen=True)
class ReasoningTraceAgent:
    """Crown-like recall: assistant turns keep their reasoning traces."""

    model: str = DEFAULT_MODEL
    seed: int = 1
    temperature: float = 0.0
    max_tokens: int = 8
    reasoning_effort: str = "medium"
    trace: str = DEFAULT_TRACE

    def recall_after_tool_call(
        self,
        *,
        trace_field: TraceField = "reasoning_content",
        include_openrouter_alias: bool = False,
        include_details: bool = False,
        include_legacy_tool_name: bool = True,
    ) -> dict[str, Any]:
        """Next chat after the agent decided to ``search_memory``.

        rig-core emits ``reasoning_content``. OpenRouter documents ``reasoning``
        as the canonical string field and ``reasoning_content`` as its alias.
        Grandmaster-style harnesses also echo ``tool_calls[].index`` and the
        legacy tool-role ``name``.
        """
        assistant: dict[str, Any] = {
            "role": "assistant",
            "content": "",
            trace_field: self.trace,
            "tool_calls": [
                {
                    "id": _SEARCH_CALL_ID,
                    "type": "function",
                    "index": 0,
                    "function": {
                        "name": "search_memory",
                        "arguments": '{"query":"ok"}',
                    },
                }
            ],
        }
        if include_openrouter_alias and trace_field != "reasoning":
            assistant["reasoning"] = self.trace
        if include_details:
            assistant["reasoning_details"] = reasoning_details_for(self.trace)

        tool: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": _SEARCH_CALL_ID,
            "content": "ok",
        }
        if include_legacy_tool_name:
            tool["name"] = "search_memory"

        return {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a retrieval agent. Reply with ok.",
                },
                {
                    "role": "user",
                    "content": "Search memory for ok, then reply with ok.",
                },
                assistant,
                tool,
                {"role": "user", "content": "Reply with the single word ok."},
            ],
            "tools": search_memory_tools(),
            "seed": self.seed,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
        }

    def heal_for_openrouter(
        self, payload: dict[str, Any] | None = None, *, bench_version: int = 9
    ) -> dict[str, Any]:
        """Validate and lock the recall payload the way the scoring lane does."""
        if payload is None:
            payload = self.recall_after_tool_call()
        _validate_request_schema(payload)
        return _locked_upstream_payload(
            payload,
            model=self.model,
            max_tokens=self.max_tokens,
            bench_version=bench_version,
        )


def main() -> None:
    agent = ReasoningTraceAgent()
    payload = agent.recall_after_tool_call()
    upstream = agent.heal_for_openrouter(payload)
    json.dump({"payload": payload, "upstream": upstream}, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
