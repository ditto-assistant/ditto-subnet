"""Minimal single-agent loop for the probe reviewer (shadow only).

One model, one transcript, the probe tool surface, and a hard request/token/
cost ledger. The loop never signs or settles anything: its product is a
``verdict.json`` that ``tools.replay_verdict`` re-executes deterministically.
The client is any OpenAI-compatible ``/chat/completions`` endpoint (Ditto
Router in production shadow); the API key is read from a file and never logged.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from ditto_screener.probe_review import tools
from ditto_screener.probe_review.sandbox import Session
from ditto_screener.review_provider import review_gateway_headers

PROMPT = (Path(__file__).with_name("reviewer_prompt.md")).read_text()
_OBS_LIMIT = 12_000


def _fn(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_OBJ = {"type": "object"}
_STR = {"type": "string"}
_INT = {"type": "integer"}
TOOL_SCHEMAS = [
    _fn(
        "run_case",
        "POST /run (optionally /seed first) with a dictated model script.",
        {
            "request": _OBJ,
            "script": _OBJ,
            "seed": _OBJ,
            "checks": {"type": "array"},
            "label": _STR,
        },
        ["request"],
    ),
    _fn(
        "show",
        "Exact model requests a probe produced.",
        {"probe_id": _STR, "entry": _INT},
        ["probe_id"],
    ),
    _fn(
        "batch",
        "Code mode: many probes with foreach templating.",
        {"plan": _OBJ},
        ["plan"],
    ),
    _fn(
        "mutate",
        "Re-run a probe under a metamorphic transform and diff.",
        {"probe_id": _STR, "transform": _STR, "value": {}, "path": _STR},
        ["probe_id", "transform"],
    ),
    _fn("diff", "Diff two probes.", {"a": _STR, "b": _STR}, ["a", "b"]),
    _fn(
        "search",
        "Regex search the submitted source.",
        {"pattern": _STR, "glob": _STR},
        ["pattern"],
    ),
    _fn(
        "read_file",
        "Read source lines.",
        {"path": _STR, "start": _INT, "end": _INT},
        ["path"],
    ),
    _fn(
        "submit_verdict",
        "Write the verdict and run deterministic replay. Ends the review.",
        {"verdict": _OBJ},
        ["verdict"],
    ),
]


@dataclass
class Ledger:
    max_requests: int = 48
    max_total_tokens: int = 1_500_000
    max_cost_usd: float = 3.0
    # The reviewer contract's cumulative limits (reviewer_prompt.md).
    fallback_input_usd_per_mtok: float = 5.0
    fallback_output_usd_per_mtok: float = 25.0
    max_tool_calls: int = 40
    max_probes: int = 30
    tool_calls: int = 0
    untracked: bool = False
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)

    def admit(self) -> bool:
        return (
            not self.untracked
            and self.requests < self.max_requests
            and self.prompt_tokens + self.completion_tokens < self.max_total_tokens
            and self.cost_usd < self.max_cost_usd
        )

    def over_budget(self) -> bool:
        """A settled response went past a token or cost limit: do not act on it."""
        return self.untracked or (
            self.prompt_tokens + self.completion_tokens > self.max_total_tokens
            or self.cost_usd > self.max_cost_usd
        )

    def exhausted(self) -> bool:
        """Any limit reached, so ``admit`` refuses another request."""
        return not self.admit()

    def settle(self, usage: dict[str, Any]) -> None:
        self.requests += 1
        prompt_raw = usage.get("prompt_tokens")
        completion_raw = usage.get("completion_tokens")
        if not all(
            isinstance(v, int) and not isinstance(v, bool) and v >= 0
            for v in (prompt_raw, completion_raw)
        ):
            # Untracked spend fails closed: the response is not acted on and
            # no further request is admitted.
            self.untracked = True
            return
        prompt = int(prompt_raw)  # type: ignore[arg-type]
        completion = int(completion_raw)  # type: ignore[arg-type]
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        cost = usage.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            if not math.isfinite(cost) or cost < 0:
                # A negative or non-finite cost would loosen the cap.
                self.untracked = True
                return
            self.cost_usd += float(cost)
        else:
            # Endpoints that omit the cost field are charged at a deliberately
            # high list price so the cost cap still binds.
            self.cost_usd += (
                prompt * self.fallback_input_usd_per_mtok
                + completion * self.fallback_output_usd_per_mtok
            ) / 1e6


_PROBING_TOOLS = frozenset({"run_case", "batch", "mutate"})


def _observation(result: Any) -> str:
    """Serialize a tool result; say so when the reviewer sees only a prefix."""
    text = json.dumps(result, default=str)
    if len(text) <= _OBS_LIMIT:
        return text
    return text[:_OBS_LIMIT] + f"...[truncated {len(text) - _OBS_LIMIT} chars]"


def dispatch(
    session: Session,
    name: str,
    args: dict[str, Any],
    *,
    max_probes: int | None = None,
) -> Any:
    if name == "run_case":
        # A seeded case costs two probes (/seed then /run).
        cost = 2 if args.get("seed") is not None else 1
        if max_probes is not None and cost > max_probes:
            return {"error": "probe budget spent; call submit_verdict"}
        return tools.run_case(
            session,
            args.get("request") or {},
            args.get("script"),
            label=args.get("label", ""),
            seed=args.get("seed"),
            checks=args.get("checks"),
        )
    if name == "show":
        return tools.show(session, args["probe_id"], entry=args.get("entry"))
    if name == "batch":
        return tools.batch(session, args.get("plan") or {}, max_probes=max_probes)
    if name == "mutate":
        return tools.mutate(
            session,
            args["probe_id"],
            args["transform"],
            args.get("value"),
            args.get("path"),
        )
    if name == "diff":
        return tools.diff(session, args["a"], args["b"])
    if name == "search":
        return tools.search(session, args["pattern"], args.get("glob", "*"))
    if name == "read_file":
        return tools.read_file(
            session, args["path"], int(args.get("start", 1)), args.get("end")
        )
    if name == "submit_verdict":
        verdict = args.get("verdict") or {}
        Path(session.root, "verdict.json").write_text(json.dumps(verdict, indent=1))
        result = tools.replay_verdict(session, verdict)
        Path(session.root, "replay.json").write_text(json.dumps(result, indent=1))
        return result
    raise ValueError(f"unknown tool {name}")


def review(
    session: Session,
    *,
    complete: Callable[[list[dict[str, Any]], list[dict[str, Any]]], dict[str, Any]],
    ledger: Ledger | None = None,
    deadline_seconds: float = 1800.0,
) -> dict[str, Any]:
    """Run the reviewer until it submits a verdict or exhausts its ledger."""
    ledger = ledger or Ledger()
    prepare = json.loads(Path(session.root, "prepare.json").read_text())
    brief = {
        "artifact_sha256": prepare["artifact_sha256"],
        "battery_format": prepare["battery"]["format"],
        "battery_items": [
            {
                k: i.get(k)
                for k in (
                    "item_id",
                    "invariant",
                    "lead",
                    "applicable",
                    "probe_id",
                    "observed",
                    "note",
                )
            }
            for i in prepare["battery"]["items"]
        ],
        "triage": prepare["triage"].get("leads", []),
    }
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": "Session prepared. Brief:\n" + json.dumps(brief)},
    ]
    started = time.monotonic()
    first_probe = session.probe_count
    replay: dict[str, Any] | None = None
    while (
        replay is None
        and ledger.admit()
        and time.monotonic() - started < deadline_seconds
    ):
        remaining = deadline_seconds - (time.monotonic() - started)
        # A pooled completer is bounded by the review's remaining wall time.
        response = (
            complete(messages, TOOL_SCHEMAS, timeout=max(1.0, remaining))
            if isinstance(complete, HttpCompleter)
            else complete(messages, TOOL_SCHEMAS)
        )
        ledger.settle(response.get("usage") or {})
        if ledger.over_budget():
            # The response that crossed a budget is not acted on.
            break
        message = response["choices"][0]["message"]
        messages.append(
            {k: v for k, v in message.items() if k in {"role", "content", "tool_calls"}}
        )
        calls = message.get("tool_calls") or []
        if not calls:
            messages.append(
                {
                    "role": "user",
                    "content": "Continue with a tool call; finish with submit_verdict.",
                }
            )
            continue
        for call in calls:
            if replay is not None:
                # Nothing may touch the session after a verified replay.
                break
            name = call["function"]["name"]
            probes_left = ledger.max_probes - (session.probe_count - first_probe)
            result: Any
            if name != "submit_verdict" and ledger.tool_calls >= ledger.max_tool_calls:
                result = {"error": "tool-call budget spent; call submit_verdict"}
            elif name in _PROBING_TOOLS and probes_left <= 0:
                result = {"error": "probe budget spent; call submit_verdict"}
            else:
                ledger.tool_calls += 1
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    result = dispatch(session, name, args, max_probes=probes_left)
                except Exception as error:  # noqa: BLE001 - tool errors go back to the reviewer
                    result = {"error": f"{type(error).__name__}: {error}"[:500]}
            ledger.events.append(
                {"tool": name, "t": round(time.monotonic() - started, 1)}
            )
            if (
                name == "submit_verdict"
                and isinstance(result, dict)
                and "error" not in result
                and not result.get("problems")
            ):
                replay = result
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": _observation(result),
                }
            )
    return {
        "replay": replay,
        "complete": replay is not None,
        "budget_exhausted": ledger.exhausted()
        or ledger.tool_calls >= ledger.max_tool_calls
        or session.probe_count - first_probe >= ledger.max_probes,
        "requests": ledger.requests,
        "prompt_tokens": ledger.prompt_tokens,
        "completion_tokens": ledger.completion_tokens,
        "cost_usd": round(ledger.cost_usd, 4),
        "wall_s": round(time.monotonic() - started, 1),
        "tool_events": ledger.events,
    }


class HttpCompleter:
    """OpenAI-compatible completer over one pooled client; close it when done."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key_file: str,
        *,
        provider: str = "ditto",
        timeout: float = 120.0,
    ) -> None:
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._model = model
        self._key = Path(api_key_file).read_text().strip()
        self._headers = review_gateway_headers(provider)
        self._client = httpx.Client(timeout=timeout)

    def __call__(
        self,
        messages: list[dict[str, Any]],
        schemas: list[dict[str, Any]],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        response = self._client.post(
            self._url,
            timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            headers={"Authorization": f"Bearer {self._key}", **self._headers},
            json={
                "model": self._model,
                "messages": messages,
                "tools": schemas,
                "tool_choice": "auto",
                "max_tokens": 8000,
            },
        )
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpCompleter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def http_completer(
    base_url: str,
    model: str,
    api_key_file: str,
    *,
    provider: str = "ditto",
    timeout: float = 120.0,
) -> HttpCompleter:
    """Use as a context manager so the connection pool is released."""
    return HttpCompleter(
        base_url, model, api_key_file, provider=provider, timeout=timeout
    )
