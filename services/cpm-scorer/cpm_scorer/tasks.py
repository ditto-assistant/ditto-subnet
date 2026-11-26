"""Scoring cases for one seed (contract §4.2) and candidate text (§2.4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MemoryTask:
    case_id: str
    query: str
    candidates: tuple[str, ...]  # pair ids, sorted, unique
    relevant: tuple[str, ...]
    spans: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolTask:
    case_id: str
    query: str
    target: tuple[str, ...]


@dataclass(frozen=True)
class SeedTasks:
    memory: tuple[MemoryTask, ...]
    tools: tuple[ToolTask, ...]
    pair_text: Mapping[str, str]  # pair id -> candidate text
    tool_text: Mapping[str, str]  # tool name -> candidate text
    tool_names: tuple[str, ...]  # whole catalog, sorted


def memory_text(pair: Mapping[str, Any]) -> str:
    """`[{timestamp}] User: {prompt}\\nAssistant: {response}`; the timestamp
    prefix is omitted when the pair has none."""
    body = f"User: {pair.get('prompt') or ''}\nAssistant: {pair.get('response') or ''}"
    ts = pair.get("timestamp")
    return f"[{ts}] {body}" if ts else body


def tool_text(tool: Mapping[str, Any]) -> str:
    return f"{tool['name']}: {tool.get('description') or ''}"


def build_tasks(
    records: Mapping[str, Any], limit_cases: int | None = None
) -> SeedTasks:
    """Select v1 cases from exporter records, in the fixed scoring order:
    memory cases then tool cases, each by case id; candidates by id.

    - memory: not abstain and at least one relevant pair id;
    - tool: non-empty shortlist_target; candidates are the whole catalog.

    `limit_cases` keeps the first N of each kind (practice runs only).
    """
    pairs = {p["pair_id"]: p for p in records["pairs"]}
    catalog: dict[str, Mapping[str, Any]] = {}
    for tool in records["catalog"]:
        catalog.setdefault(tool["name"], tool)
    memory: list[MemoryTask] = []
    tools: list[ToolTask] = []
    for case in records["cases"]:
        if case["kind"] == "memory":
            if case["decision"]["abstain"] or not case["relevant_pair_ids"]:
                continue
            memory.append(
                MemoryTask(
                    case_id=case["case_id"],
                    query=case["query"] or "",
                    candidates=tuple(sorted(set(case["candidate_pair_ids"]))),
                    relevant=tuple(case["relevant_pair_ids"]),
                    spans=case.get("spans") or {},
                )
            )
        elif case["kind"] == "tool":
            if not case["shortlist_target"]:
                continue
            tools.append(
                ToolTask(
                    case_id=case["case_id"],
                    query=case["query"] or "",
                    target=tuple(case["shortlist_target"]),
                )
            )
    memory.sort(key=lambda t: t.case_id)
    tools.sort(key=lambda t: t.case_id)
    if limit_cases is not None:
        memory, tools = memory[:limit_cases], tools[:limit_cases]
    needed = {pid for t in memory for pid in t.candidates}
    return SeedTasks(
        memory=tuple(memory),
        tools=tuple(tools),
        pair_text={pid: memory_text(pairs[pid]) for pid in sorted(needed)},
        tool_text={name: tool_text(t) for name, t in sorted(catalog.items())},
        tool_names=tuple(sorted(catalog)),
    )
