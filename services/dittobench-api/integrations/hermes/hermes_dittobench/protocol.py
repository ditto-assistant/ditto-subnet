"""Small, dependency-free representations of the public DittoBench wire types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ProtocolError(ValueError):
    """The validator sent a malformed request."""


def _string(value: Any, field_name: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value):
        raise ProtocolError(f"{field_name} must be a non-empty string")
    return value


def _list(value: Any, field_name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ProtocolError(f"{field_name} must be an array")
    return value


@dataclass(frozen=True)
class MemoryPair:
    pair_id: str
    session_id: str
    timestamp: str
    prompt: str
    response: str

    @classmethod
    def from_json(cls, value: Any) -> "MemoryPair":
        if not isinstance(value, dict):
            raise ProtocolError("pairs entries must be objects")
        return cls(
            pair_id=_string(value.get("pair_id"), "pair_id", required=True),
            session_id=_string(value.get("session_id"), "session_id"),
            timestamp=_string(value.get("timestamp"), "timestamp"),
            prompt=_string(value.get("prompt"), "prompt"),
            response=_string(value.get("response"), "response"),
        )


@dataclass(frozen=True)
class SeedRequest:
    user_id: str = "miner"
    wave: int = 0
    pairs: tuple[MemoryPair, ...] = ()
    subjects: tuple[dict[str, Any], ...] = ()
    links: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_json(cls, value: Any) -> "SeedRequest":
        if not isinstance(value, dict):
            raise ProtocolError("seed request must be an object")
        wave = value.get("wave", 0)
        if not isinstance(wave, int) or isinstance(wave, bool) or wave < 0:
            raise ProtocolError("wave must be a non-negative integer")
        raw_subjects = _list(value.get("subjects"), "subjects")
        raw_links = _list(value.get("links"), "links")
        if not all(isinstance(item, dict) for item in raw_subjects):
            raise ProtocolError("subjects entries must be objects")
        if not all(isinstance(item, dict) for item in raw_links):
            raise ProtocolError("links entries must be objects")
        return cls(
            user_id=_string(value.get("user_id") or "miner", "user_id", required=True),
            wave=wave,
            pairs=tuple(MemoryPair.from_json(item) for item in _list(value.get("pairs"), "pairs")),
            subjects=tuple(raw_subjects),
            links=tuple(raw_links),
        )


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, value: Any) -> "ToolDefinition":
        if not isinstance(value, dict):
            raise ProtocolError("tools entries must be objects")
        parameters = value.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(parameters, dict):
            raise ProtocolError("tool parameters must be an object")
        return cls(
            name=_string(value.get("name"), "tool name", required=True),
            description=_string(value.get("description"), "tool description"),
            parameters=parameters,
        )

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class RunRequest:
    case_id: str
    system_prompt: str
    user_input: str
    tools: tuple[ToolDefinition, ...]
    tool_endpoint: str = ""
    user_id: str = "miner"

    @classmethod
    def from_json(cls, value: Any) -> "RunRequest":
        if not isinstance(value, dict):
            raise ProtocolError("run request must be an object")
        return cls(
            case_id=_string(value.get("case_id"), "case_id", required=True),
            system_prompt=_string(value.get("system_prompt"), "system_prompt"),
            user_input=_string(value.get("user_input"), "user_input"),
            tools=tuple(ToolDefinition.from_json(item) for item in _list(value.get("tools"), "tools")),
            tool_endpoint=_string(value.get("tool_endpoint"), "tool_endpoint"),
            user_id=_string(value.get("user_id") or "miner", "user_id", required=True),
        )
