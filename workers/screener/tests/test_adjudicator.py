"""Automated clear/reject adjudication of a held source review."""

from __future__ import annotations

import io
import json
import os
import tarfile
from pathlib import Path

import httpx
import pytest

from ditto_screener.adjudicator import (
    ADJUDICATOR_PROMPT_REVISION,
    SourceReviewAdjudicator,
    build_adjudicator,
)

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


_CONCERN = {
    "kind": "concern",
    "category": "benchmark_emulation",
    "path": "src/main.rs",
    "line": 10,
    "summary": "reranking looked like an answer-family engine",
}


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


async def test_a_citation_the_court_never_read_escalates(tmp_path: Path) -> None:
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
    assert result.escalation_code == "cited-unread-source"


async def test_a_hallucinated_path_escalates(tmp_path: Path) -> None:
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
    assert result.escalation_code == "cited-unknown-member"


async def test_a_line_past_the_end_of_the_file_escalates(tmp_path: Path) -> None:
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
    assert result.escalation_code == "cited-unread-source"


async def test_only_inert_citations_escalate(tmp_path: Path) -> None:
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
    assert result.escalation_code == "inadmissible-citations"


async def test_a_decision_without_a_published_basis_escalates(tmp_path: Path) -> None:
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
    assert result.escalation_code == "verdict-contract-failed"


async def test_an_uncited_decision_escalates(tmp_path: Path) -> None:
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
    assert result.escalation_code == "uncited-decision"


async def test_an_exhausted_step_budget_escalates(tmp_path: Path) -> None:
    """Running out of turns holds the submission; it never decides it."""

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
    assert result.escalation_code == "adjudicator-failed"


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


async def test_a_missing_key_escalates_rather_than_raising(tmp_path: Path) -> None:
    result = await SourceReviewAdjudicator(
        api_key_file=None, base_url="https://openrouter.test/api/v1"
    ).adjudicate(_archive(tmp_path), notes=[])

    assert result.decision == "escalate"
    assert result.escalation_code == "adjudicator-unavailable"


@pytest.mark.parametrize("mode", ("off", "shadow", "enforce"))
def test_the_court_is_only_built_when_an_operator_turns_it_on(
    make_config, mode: str
) -> None:
    built = build_adjudicator(make_config(adjudicator_mode=mode))

    assert (built is None) == (mode == "off")
