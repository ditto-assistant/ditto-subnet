"""Run one DittoBench case through the unmodified Hermes agent loop."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.request
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable

from .protocol import RunRequest, ToolDefinition
from .state import HermesStateStore


MEMORY_READ_TOOLS = frozenset(
    {
        "search_memories",
        "fetch_memories",
        "search_subjects",
        "search_memories_in_subjects",
    }
)
MEMORY_WRITE_TOOLS = frozenset({"save_memory", "update_memory", "delete_memory"})
WIRE_MEMORY_TOOLS = MEMORY_READ_TOOLS | MEMORY_WRITE_TOOLS

BASELINE_PROFILE = "baseline"
FAVORABLE_PROFILE = "favorable"
NATIVE_SESSION_PROFILE = "native-session"
FAVORABLE_RECALL_GUIDANCE = (
    "When the user references something from a past conversation or relevant "
    "cross-session context may exist, use the supplied memory search tools to "
    "recall it before asking the user to repeat themselves or concluding that "
    "the information is unavailable."
)


@dataclass
class _RunContext:
    request: RunRequest
    db: Any


def _default_post_json(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(1 << 20)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("tool endpoint returned a non-object")
    return value


class EvalAgentMixin:
    """Keep benchmark questions out of the seeded recall corpus."""

    def _ensure_db_session(self) -> None:  # pragma: no cover - exercised with Hermes
        return None

    def _flush_messages_to_session_db(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        return None

    def _persist_session(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        return None


class HermesRunner:
    def __init__(
        self,
        state: HermesStateStore,
        *,
        agent_factory: Callable[..., Any] | None = None,
        registry: Any | None = None,
        session_search: Callable[..., str] | None = None,
        post_json: Callable[[str, dict[str, Any], float], dict[str, Any]] = _default_post_json,
    ) -> None:
        self.state = state
        self._agent_factory = agent_factory
        self._registry = registry
        self._session_search = session_search
        self._post_json = post_json
        self._local = threading.local()

    @staticmethod
    def _profile() -> str:
        value = os.environ.get("HERMES_DITTOBENCH_PROFILE", BASELINE_PROFILE).strip().lower()
        if value not in {BASELINE_PROFILE, FAVORABLE_PROFILE, NATIVE_SESSION_PROFILE}:
            raise ValueError(
                "HERMES_DITTOBENCH_PROFILE must be baseline, favorable, or native-session"
            )
        return value

    @staticmethod
    def _positive_int(name: str, default: int) -> int:
        value = int(os.environ.get(name, str(default)))
        if value <= 0:
            raise ValueError(f"{name} must be positive")
        return value

    def _search_limit(self) -> int:
        # Hermes clamps native session_search discovery to ten results. Keep
        # the alias profile honest about that effective upstream ceiling.
        default = 10 if self._profile() == FAVORABLE_PROFILE else 5
        return self._positive_int("HERMES_DITTOBENCH_SEARCH_LIMIT", default)

    def _max_iterations(self) -> int:
        default = 90 if self._profile() in {FAVORABLE_PROFILE, NATIVE_SESSION_PROFILE} else 8
        return self._positive_int("HERMES_DITTOBENCH_MAX_ITERATIONS", default)

    def _max_tokens(self) -> int:
        # Hermes defaults to a 65,536-token output cap. That exceeds the pinned
        # OpenRouter/Nebius route's 40,960-token context before ordinary prompt
        # and tool-schema tokens are counted, producing avoidable HTTP 400s.
        # Match the other reference adapter's generous 8,192-token cap while
        # retaining enough room for every benchmark response and tool loop.
        return self._positive_int("HERMES_DITTOBENCH_MAX_TOKENS", 8192)

    def _system_prompt(self, request: RunRequest) -> str:
        if self._profile() != FAVORABLE_PROFILE:
            return request.system_prompt
        return "\n\n".join(part for part in (request.system_prompt, FAVORABLE_RECALL_GUIDANCE) if part)

    def _hermes(self) -> tuple[Callable[..., Any], Any, Callable[..., str]]:
        agent_factory = self._agent_factory
        if agent_factory is None:
            from run_agent import AIAgent

            if self._profile() == NATIVE_SESSION_PROFILE:
                # This condition deliberately keeps Hermes' ordinary session
                # persistence. Later cases can recall earlier native sessions,
                # including explicit remember/update/forget conversations.
                agent_factory = AIAgent
            else:
                class EvalAgent(EvalAgentMixin, AIAgent):
                    pass

                agent_factory = EvalAgent
        if self._registry is None:
            from tools.registry import registry

            self._registry = registry
        if self._session_search is None:
            from tools.session_search_tool import session_search

            self._session_search = session_search
        return agent_factory, self._registry, self._session_search

    @staticmethod
    def _queries(args: dict[str, Any]) -> str:
        raw = args.get("queries") or args.get("query") or args.get("pairIds") or args.get("subject_id") or ""
        if isinstance(raw, list):
            return " OR ".join(str(item) for item in raw if str(item).strip())
        return str(raw)

    def _memory_read(self, name: str, args: dict[str, Any]) -> str:
        _, _, session_search = self._hermes()
        context: _RunContext = self._local.context
        query = self._queries(args)
        if not query:
            return session_search(limit=self._search_limit(), db=context.db)
        # These aliases deliberately retain Hermes' native FTS5 behavior. The
        # adapter translates the catalog name, not the retrieval algorithm.
        return session_search(query=query, limit=self._search_limit(), db=context.db)

    def _dispatch_wire_tool(self, name: str, args: dict[str, Any]) -> str:
        context: _RunContext = self._local.context
        if name in MEMORY_READ_TOOLS:
            return self._memory_read(name, args)
        endpoint = context.request.tool_endpoint
        if not endpoint:
            return json.dumps({"error": "no validator tool_endpoint was provided"})
        result = self._post_json(
            endpoint,
            {
                "case_id": context.request.case_id,
                "user_id": context.request.user_id,
                "name": name,
                "args": args,
                "hop": int(getattr(self._local, "hop", 0)),
            },
            15.0,
        )
        if result.get("error"):
            return json.dumps({"error": result["error"]})
        return str(result.get("result", ""))

    def _register_tools(self, tools: tuple[ToolDefinition, ...]) -> None:
        _, registry, _ = self._hermes()
        for tool in tools:
            if self._profile() == NATIVE_SESSION_PROFILE and tool.name in WIRE_MEMORY_TOOLS:
                # The native condition must not teach Hermes DittoBench's
                # memory dialect. Hermes sees its own session_search tool and
                # emits that exact tool name in the retained trace.
                continue

            def handler(args: dict[str, Any], _name: str = tool.name, **_: Any) -> str:
                return self._dispatch_wire_tool(_name, args)

            registry.register(
                name=tool.name,
                toolset="dittobench-wire",
                schema={
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
                handler=handler,
                override=True,
            )

    def _preflight(self, request: RunRequest) -> dict[str, Any]:
        started = time.monotonic()
        if not request.tool_endpoint:
            raise ValueError("preflight requires tool_endpoint")
        self._post_json(
            request.tool_endpoint,
            {
                "case_id": request.case_id,
                "user_id": request.user_id,
                "name": "search_web",
                "args": {"queries": ["dittobench reachability preflight"]},
                "hop": 0,
            },
            15.0,
        )
        return {
            "final_text": "DittoBench tool endpoint is reachable.",
            "tool_calls": [
                {
                    "name": "search_web",
                    "args": {"queries": ["dittobench reachability preflight"]},
                    "hop": 0,
                }
            ],
            "prompt_tokens": 0,
            "output_tokens": 0,
            "latency_ms": int((time.monotonic() - started) * 1000),
        }

    def run(self, request: RunRequest) -> dict[str, Any]:
        if request.case_id.startswith("preflight:"):
            return self._preflight(request)

        # Hermes SessionDB explicitly supports multiple reader threads and the
        # benchmark never seeds while cases are running. Serializing every case
        # behind the user lock would defeat the scorer's bounded tool-case
        # concurrency without improving isolation; physical per-user databases
        # remain the isolation boundary.
        with nullcontext():
            db = self.state.db_for(request.user_id)
            self._register_tools(request.tools)
            agent_factory, _, _ = self._hermes()
            observed: list[dict[str, Any]] = []

            def on_tool_start(_call_id: str, name: str, args: dict[str, Any]) -> None:
                hop = len(observed)
                self._local.hop = hop
                observed.append({"name": name, "args": args or {}, "hop": hop})

            native_session = self._profile() == NATIVE_SESSION_PROFILE
            agent = agent_factory(
                base_url=os.environ.get("CHUTES_BASE_URL") or os.environ.get("OPENAI_BASE_URL"),
                api_key=os.environ.get("CHUTES_API_KEY") or os.environ.get("OPENAI_API_KEY") or "relay",
                provider="custom",
                api_mode="chat_completions",
                model=os.environ.get("DITTOBENCH_MODEL", "qwen/qwen3-32b"),
                max_iterations=self._max_iterations(),
                max_tokens=self._max_tokens(),
                tool_delay=0,
                enabled_toolsets=(
                    ["dittobench-wire", "session_search"] if native_session else []
                ),
                quiet_mode=True,
                ephemeral_system_prompt=self._system_prompt(request),
                session_id="dittobench_eval_"
                + hashlib.sha256(request.case_id.encode("utf-8")).hexdigest()[:32],
                session_db=db,
                skip_context_files=True,
                load_soul_identity=False,
                # Native-session keeps Hermes' ordinary (empty, container-local)
                # curated-memory prompt path. Seeded benchmark history remains
                # in the native SessionDB, not in adapter-authored MEMORY.md.
                skip_memory=not native_session,
                tool_start_callback=on_tool_start,
                reasoning_config={"enabled": False},
            )
            # The validator-style model relay deliberately returns ordinary
            # Chat Completions JSON (streaming is disabled for deterministic
            # accounting). Tell Hermes to request that shape; otherwise its
            # streaming consumer treats the valid non-stream response as an
            # empty SSE stream and never executes the returned tool call.
            agent._disable_streaming = True
            if not native_session:
                # Alias profiles intentionally expose exactly the validator
                # catalog and route its memory names onto native FTS.
                agent.tools = [tool.openai_schema() for tool in request.tools]
                agent.valid_tool_names = {tool.name for tool in request.tools}
            # The native condition leaves the tool surface AIAgent built from
            # Hermes' own registry untouched: session_search plus the ordinary
            # non-memory tools registered above.
            agent._end_session_on_close = False

            started = time.monotonic()
            self._local.context = _RunContext(request=request, db=db)
            self._local.hop = 0
            try:
                result = agent.run_conversation(request.user_input)
            finally:
                self._local.context = None
                try:
                    agent.shutdown_memory_provider()
                except Exception:
                    pass
                try:
                    agent.release_clients()
                except Exception:
                    pass

            return {
                "final_text": str(result.get("final_response") or ""),
                "tool_calls": observed,
                "prompt_tokens": int(result.get("input_tokens") or result.get("prompt_tokens") or 0),
                "output_tokens": int(result.get("output_tokens") or result.get("completion_tokens") or 0),
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
