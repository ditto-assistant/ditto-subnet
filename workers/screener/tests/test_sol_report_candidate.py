"""Protocol and isolation checks for the report-only Sol candidate."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import httpx
import pytest

from ditto_screener import sol_report_candidate
from ditto_screener.sol_report_candidate import (
    Identity,
    _Workspace,
    run_report_candidate,
)


def _archive(path: Path, member: str = "src/main.go") -> str:
    content = b"package main\nfunc main() {}\n"
    with tarfile.open(path, "w:gz") as archive:
        info = tarfile.TarInfo(member)
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _response(
    call_id: str, name: str, arguments: dict[str, object], *, cost: float | None = 0.001
) -> dict[str, object]:
    usage: dict[str, object] = {"input_tokens": 50, "output_tokens": 20}
    if cost is not None:
        usage["cost"] = cost
    return {
        "status": "completed",
        "model": "openai/gpt-6-sol",
        "usage": usage,
        "output": [
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments),
            }
        ],
    }


@pytest.mark.asyncio
async def test_l1_notes_reach_l2_and_report_stays_non_authoritative(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "source.tar.gz"
    sha = _archive(archive)
    identity = Identity(sha, 13, "agent-1", "attempt-1")
    sequence = iter(
        [
            _response("a", "list_files", {"prefix": ""}),
            _response(
                "b",
                "record_note",
                {
                    "kind": "cleared",
                    "path": "src/main.go",
                    "line": 2,
                    "summary": "Entrypoint inspected",
                },
            ),
            _response("c", "finish_notes", {"summary": "Review notes complete"}),
            _response("d", "read_l1_notes", {"offset": 0}),
            _response(
                "e",
                "submit_candidate_review",
                {
                    "disposition": "HOLD",
                    "summary": "A tiny sample cannot establish the full served path",
                    "citations": [
                        {"path": "src/main.go", "line": 2, "reason": "Entrypoint"}
                    ],
                },
            ),
        ]
    )
    requests: list[dict[str, object]] = []
    progress: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/responses"
        payload = json.loads(request.content)
        requests.append(payload)
        assert payload["model"] == "openai/gpt-6-sol"
        assert payload["store"] is False
        assert payload["provider"]["data_collection"] == "deny"
        return httpx.Response(200, json=next(sequence))

    report = await run_report_candidate(
        archive,
        identity=identity,
        api_key="test-only",
        transport=httpx.MockTransport(respond),
        on_progress=progress.append,
    )
    assert report["authority"] == "none"
    assert report["l1"]["result"]["note_count"] == 1
    assert report["l2"]["result"]["disposition"] == "HOLD"
    assert report["notes_sha256"]
    l1_tools = {tool["name"] for tool in requests[0]["tools"]}
    l2_tools = {tool["name"] for tool in requests[-1]["tools"]}
    assert "submit_review" not in l1_tools
    assert "analyze_binary" not in l1_tools
    assert "record_note" in l1_tools
    assert "verify_syntax" in l1_tools
    assert "verify_syntax" in l2_tools
    assert "read_l1_notes" in l2_tools
    assert "submit_candidate_review" in l2_tools
    assert "Entrypoint inspected" in json.dumps(requests[-1]["input"])
    assert [
        snapshot["phase"] for snapshot in progress if snapshot["event"] == "started"
    ] == ["l1", "l2"]
    assert progress[-1]["tool_counts"]["submit_candidate_review"] == 1
    assert "Entrypoint inspected" not in json.dumps(progress)


@pytest.mark.asyncio
async def test_l1_step_limit_hands_partial_notes_to_l2_without_clearance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "source.tar.gz"
    sha = _archive(archive)
    monkeypatch.setattr(sol_report_candidate, "_MAX_STEPS", 1)
    responses = iter(
        [
            _response(
                "l1",
                "record_note",
                {
                    "kind": "context",
                    "path": "src/main.go",
                    "line": 1,
                    "summary": "Go source inspected",
                },
            ),
            _response(
                "l2",
                "submit_candidate_review",
                {
                    "disposition": "HOLD",
                    "summary": "Partial L1 coverage needs independent review",
                    "citations": [
                        {"path": "src/main.go", "line": 1, "reason": "Source path"}
                    ],
                },
            ),
        ]
    )
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    report = await run_report_candidate(
        archive,
        identity=Identity(sha, 13, "agent-1", "attempt-1"),
        api_key="test-only",
        transport=httpx.MockTransport(respond),
    )
    assert report["authority"] == "none"
    assert report["l1"]["result"]["complete"] is False
    assert report["l1"]["result"]["note_count"] == 1
    l2_task = json.loads(requests[1]["input"][0]["content"])
    assert l2_task["l1_complete"] is False
    assert report["l2"]["result"]["disposition"] == "HOLD"


@pytest.mark.asyncio
async def test_l2_step_limit_returns_host_hold_with_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "source.tar.gz"
    sha = _archive(archive)
    monkeypatch.setattr(sol_report_candidate, "_MAX_STEPS", 1)
    responses = iter(
        [
            _response(
                "l1",
                "record_note",
                {
                    "kind": "context",
                    "path": "src/main.go",
                    "line": 1,
                    "summary": "Go source",
                },
            ),
            _response("l2", "list_files", {"prefix": ""}),
        ]
    )

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    report = await run_report_candidate(
        archive,
        identity=Identity(sha, 13, "agent-1", "attempt-1"),
        api_key="test-only",
        transport=httpx.MockTransport(respond),
    )
    assert report["authority"] == "none"
    assert report["l2"]["result"] == {
        "disposition": "HOLD",
        "summary": "L2 bounded review ended without a submitted conclusion",
        "citations": [],
        "origin": "host_budget_hold",
        "reason_code": "step_cap",
    }
    assert report["l2"]["usage"]["accounted_cost_usd"] == 0.001


@pytest.mark.asyncio
async def test_l2_deadline_returns_host_hold_without_model_call(tmp_path: Path) -> None:
    workspace = _Workspace(tmp_path)
    notes_path = tmp_path / "l1-notes.md"
    notes_path.write_text("# notes\n", encoding="utf-8")
    workspace.attach_notes(notes_path)
    total_usage = dict.fromkeys(
        (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reported_cost_usd",
            "estimated_cost_usd",
            "accounted_cost_usd",
        ),
        0.0,
    )

    def unexpected(_request: httpx.Request) -> httpx.Response:
        pytest.fail("expired L2 deadline must not call the model")

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(unexpected),
    ) as client:
        result = await sol_report_candidate._phase(
            client,
            key="test-only",
            workspace=workspace,
            identity=Identity("a" * 64, 13, "agent-1", "attempt-1"),
            phase="l2",
            notes_path=notes_path,
            deadline=0,
            total_usage=total_usage,
            l1_notes=[],
            l1_complete=False,
        )
    assert result["result"]["disposition"] == "HOLD"
    assert result["result"]["origin"] == "host_budget_hold"
    assert result["result"]["reason_code"] == "deadline_exceeded"


@pytest.mark.parametrize(
    ("path", "valid", "invalid", "parser"),
    [
        (
            "app/service.py",
            'x = """closed\ntext"""\n',
            'x = """unclosed\n',
            "python-ast",
        ),
        (
            "src/main.go",
            "package main\nfunc main() {}\n",
            "package main\nfunc main( {\n",
            "gofmt",
        ),
        ("src/main.rs", "fn main() {}\n", "fn main( {\n", "rustfmt"),
    ],
)
def test_syntax_receipts_parse_source_without_executing_it(
    tmp_path: Path, path: str, valid: str, invalid: str, parser: str
) -> None:
    if parser != "python-ast" and shutil.which(parser) is None:
        pytest.skip(f"{parser} unavailable")
    target = tmp_path / path
    target.parent.mkdir(parents=True)
    target.write_text(valid, encoding="utf-8")
    workspace = _Workspace(tmp_path)
    good = workspace.call("verify_syntax", {"path": path})
    assert good["parser"] == parser
    assert good["syntax_valid"] is True
    target.write_text(invalid, encoding="utf-8")
    bad = workspace.call("verify_syntax", {"path": path})
    assert bad["parser"] == parser
    assert bad["syntax_valid"] is False


def test_source_navigation_can_page_past_first_files_and_hits(tmp_path: Path) -> None:
    for index in range(260):
        (tmp_path / f"{index:03}.go").write_text(
            f"package main\n// located needle {index}\n", encoding="utf-8"
        )
    workspace = _Workspace(tmp_path)
    first = workspace.call("list_files", {"prefix": "", "offset": 0})
    second = workspace.call("list_files", {"prefix": "", "offset": 256})
    assert first["next_offset"] == 256
    assert second["paths"] == [f"{index:03}.go" for index in range(256, 260)]
    hits = workspace.call("search", {"query": "needle", "offset": 80})
    assert hits["hits"][0]["path"] == "080.go"
    assert hits["next_offset"] == 160


def test_host_notes_are_searchable_but_cannot_be_cited_as_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
    notes = tmp_path / "l1-notes.md"
    notes.write_text("# Host notes\nlocated finding\n", encoding="utf-8")
    workspace = _Workspace(source)
    workspace.attach_notes(notes)
    assert (
        "review/l1-notes.md"
        in workspace.call("list_files", {"prefix": "review/", "offset": 0})["paths"]
    )
    assert (
        workspace.call("search", {"query": "located", "offset": 0})["hits"][0]["path"]
        == "review/l1-notes.md"
    )
    assert not workspace.has_line("review/l1-notes.md", 2)
    assert workspace.has_line("main.py", 1)


@pytest.mark.asyncio
async def test_archive_path_traversal_is_rejected_before_model_call(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    sha = _archive(archive, "../outside.go")
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    with pytest.raises(ValueError, match="unsafe"):
        await run_report_candidate(
            archive,
            identity=Identity(sha, 13, "agent-1", "attempt-1"),
            api_key="test-only",
            transport=httpx.MockTransport(respond),
        )
    assert not called


@pytest.mark.asyncio
async def test_cross_phase_cost_cap_is_shared(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    sha = _archive(archive)
    sequence = iter(
        [
            _response(
                "a",
                "record_note",
                {
                    "kind": "context",
                    "path": "src/main.go",
                    "line": 1,
                    "summary": "Go source",
                },
                cost=13,
            ),
            _response("b", "finish_notes", {"summary": "Done"}, cost=0),
            _response("c", "list_l1_notes", {}, cost=13),
        ]
    )
    with pytest.raises(ValueError, match="cost or output cap"):
        await run_report_candidate(
            archive,
            identity=Identity(sha, 13, "agent-1", "attempt-1"),
            api_key="test-only",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=next(sequence))
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_cost", [None, 0.0])
async def test_unmetered_cost_uses_conservative_estimate(
    tmp_path: Path, reported_cost: float | None
) -> None:
    archive = tmp_path / "source.tar.gz"
    sha = _archive(archive)
    response = _response("a", "list_files", {"prefix": ""}, cost=reported_cost)
    response["usage"]["input_tokens"] = 6_000_000
    with pytest.raises(ValueError, match="cost or output cap"):
        await run_report_candidate(
            archive,
            identity=Identity(sha, 13, "agent-1", "attempt-1"),
            api_key="test-only",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=response)
            ),
        )


@pytest.mark.asyncio
async def test_notes_search_and_invalid_citation_are_bounded(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    sha = _archive(archive)
    sequence = iter(
        [
            _response(
                "a",
                "record_note",
                {
                    "kind": "context",
                    "path": "src/main.go",
                    "line": 1,
                    "summary": "Go package",
                },
            ),
            _response(
                "b",
                "record_note",
                {
                    "kind": "concern",
                    "path": "src/main.go",
                    "line": 2,
                    "summary": "Inspect entrypoint",
                },
            ),
            _response("c", "finish_notes", {"summary": "Done"}),
            _response("d", "list_l1_notes", {}),
            _response("e", "search_l1_notes", {"query": "entrypoint"}),
            _response("f", "read_l1_notes", {"offset": 1}),
            _response(
                "g",
                "submit_candidate_review",
                {
                    "disposition": "REJECT",
                    "summary": "Invalid",
                    "citations": [
                        {"path": "../outside.go", "line": 1, "reason": "Not source"}
                    ],
                },
            ),
            _response(
                "h",
                "submit_candidate_review",
                {
                    "disposition": "HOLD",
                    "summary": "Needs more evidence",
                    "citations": [
                        {"path": "src/main.go", "line": 2, "reason": "Entrypoint"}
                    ],
                },
            ),
        ]
    )
    requests: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(sequence))

    report = await run_report_candidate(
        archive,
        identity=Identity(sha, 13, "agent-1", "attempt-1"),
        api_key="test-only",
        transport=httpx.MockTransport(respond),
    )
    assert report["l2"]["result"]["disposition"] == "HOLD"
    assert json.loads(requests[5]["input"][-1]["output"])["hits"][0]["index"] == 1
    assert len(json.loads(requests[6]["input"][-1]["output"])["notes"]) == 1
    assert (
        json.loads(requests[7]["input"][-1]["output"])["error"]
        == "invalid-review-or-citations"
    )


@pytest.mark.asyncio
async def test_archive_symlink_is_rejected_before_model_call(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        link = tarfile.TarInfo("src/main.go")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../private"
        handle.addfile(link)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    called = False

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    with pytest.raises(ValueError, match="link or special file"):
        await run_report_candidate(
            archive,
            identity=Identity(sha, 13, "agent-1", "attempt-1"),
            api_key="test-only",
            transport=httpx.MockTransport(respond),
        )
    assert not called
