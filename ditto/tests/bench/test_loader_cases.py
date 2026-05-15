"""Unit tests for ditto.bench.loader.cases.

Covers JSONL parsing, default-value handling, duplicate-id rejection, and a
sanity check that every shipped public fixture file parses without errors.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ditto.bench.loader.cases import (
    ArgMatcher,
    ArgMatcherKind,
    DuplicateCaseIDError,
    ExpectedToolCall,
    RetrievalCase,
    ToolCallCase,
    load_retrieval_cases,
    load_toolcall_cases,
)
from ditto.bench.loader.taxonomy import (
    is_known_core_domain,
    is_known_retrieval_category,
)

PUBLIC_FIXTURES = Path(__file__).resolve().parents[2] / "bench" / "fixtures"


def _write_jsonl(directory: Path, name: str, records: list[dict]) -> Path:
    """Write a JSONL file under ``directory``; return the file path."""
    path = directory / name
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def test_arg_matcher_round_trips_dict() -> None:
    """ArgMatcher.from_dict accepts the canonical fixture shape."""
    m = ArgMatcher.from_dict(
        {
            "kind": "url_list",
            "key": "urls",
            "any_of": ["https://example.com/a"],
            "weight": 2.0,
        }
    )
    assert m.kind is ArgMatcherKind.URL_LIST
    assert m.key == "urls"
    assert m.any_of == ["https://example.com/a"]
    assert m.weight == 2.0


def test_expected_tool_call_parses_arg_matchers() -> None:
    """ExpectedToolCall.from_dict reads both required_args and arg_matchers."""
    t = ExpectedToolCall.from_dict(
        {
            "name": "search_memories",
            "required_args": {"q": "kubernetes"},
            "arg_matchers": [{"kind": "present", "key": "queries"}],
            "forbidden_args": ["password"],
            "forbidden_values": {"queries": ["ssn", "credit card"]},
        }
    )
    assert t.name == "search_memories"
    assert t.required_args == {"q": "kubernetes"}
    assert t.arg_matchers[0].kind is ArgMatcherKind.PRESENT
    assert t.forbidden_args == ["password"]
    assert t.forbidden_values == {"queries": ["ssn", "credit card"]}


def test_toolcall_case_is_public_default_true() -> None:
    """An empty/unset visibility means the case ships in the public split."""
    c = ToolCallCase(id="x", category="memory_lookup", prompt="...")
    assert c.is_public
    c.visibility = "private"
    assert not c.is_public


def test_retrieval_case_is_stm_when_context_present() -> None:
    """is_stm reflects stm_context, not difficulty."""
    c = RetrievalCase.from_dict(
        {
            "id": "x",
            "category": "stm_only",
            "query": "what did I just say?",
            "user_fixture_id": "fixture",
            "stm_context": [{"role": "user", "content": "hi"}],
        }
    )
    assert c.is_stm
    assert c.stm_context[0].role == "user"


def test_load_toolcall_cases_merges_every_jsonl(tmp_path: Path) -> None:
    """The loader merges every *.jsonl in the directory."""
    _write_jsonl(
        tmp_path,
        "a.jsonl",
        [{"id": "a1", "category": "memory_lookup", "prompt": "hi"}],
    )
    _write_jsonl(
        tmp_path,
        "b.jsonl",
        [{"id": "b1", "category": "memory_lookup", "prompt": "bye"}],
    )
    cases = load_toolcall_cases(tmp_path)
    assert {c.id for c in cases} == {"a1", "b1"}


def test_load_toolcall_cases_rejects_duplicate_ids(tmp_path: Path) -> None:
    """Two cases with the same id across two files must fail loudly."""
    _write_jsonl(
        tmp_path,
        "a.jsonl",
        [{"id": "dup", "category": "memory_lookup", "prompt": "hi"}],
    )
    _write_jsonl(
        tmp_path,
        "b.jsonl",
        [{"id": "dup", "category": "memory_lookup", "prompt": "bye"}],
    )
    with pytest.raises(DuplicateCaseIDError):
        load_toolcall_cases(tmp_path)


def test_load_toolcall_cases_skips_comments_and_blank_lines(tmp_path: Path) -> None:
    """Empty lines and ``//`` / ``#`` prefixes are ignored like the Go loader."""
    (tmp_path / "x.jsonl").write_text(
        "\n# comment\n// also comment\n"
        + json.dumps({"id": "x1", "category": "x", "prompt": "y"})
        + "\n",
        encoding="utf-8",
    )
    cases = load_toolcall_cases(tmp_path)
    assert [c.id for c in cases] == ["x1"]


def test_load_retrieval_cases_parses_full_record(tmp_path: Path) -> None:
    """Every retrieval JSONL field round-trips through the dataclass."""
    record = {
        "id": "r1",
        "category": "single_needle_recent",
        "query": "what?",
        "user_fixture_id": "u1",
        "expected_pair_ids": ["p1", "p2"],
        "k": 10,
        "in_window": True,
        "difficulty": "easy",
    }
    _write_jsonl(tmp_path, "r.jsonl", [record])
    cases = load_retrieval_cases(tmp_path)
    assert len(cases) == 1
    c = cases[0]
    assert c.id == "r1"
    assert c.expected_pair_ids == ["p1", "p2"]
    assert c.k == 10
    assert c.in_window
    assert c.difficulty == "easy"


def test_load_toolcall_cases_rejects_invalid_json(tmp_path: Path) -> None:
    """A malformed JSON line surfaces the file path and line number."""
    (tmp_path / "x.jsonl").write_text("{not json\n", encoding="utf-8")
    with pytest.raises(ValueError, match="x.jsonl:1"):
        load_toolcall_cases(tmp_path)


def test_public_toolcall_fixtures_parse() -> None:
    """Every shipped public toolcall fixture parses and uses known categories."""
    cases = load_toolcall_cases(PUBLIC_FIXTURES / "toolcall")
    assert len(cases) > 0, "expected at least one toolcall fixture"
    for c in cases:
        if c.domain:
            assert is_known_core_domain(c.domain), (
                f"unknown domain {c.domain!r} in case {c.id!r}"
            )


def test_public_retrieval_fixtures_parse() -> None:
    """Every shipped public retrieval fixture parses and uses known categories."""
    cases = load_retrieval_cases(PUBLIC_FIXTURES / "retrieval")
    assert len(cases) > 0, "expected at least one retrieval fixture"
    for c in cases:
        assert is_known_retrieval_category(c.category), (
            f"unknown category {c.category!r} in case {c.id!r}"
        )
