"""Protocol and isolation checks for the report-only Sol candidate."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from ditto_screener.sol_report_candidate import Identity, run_report_candidate


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
    assert "read_l1_notes" in l2_tools
    assert "submit_candidate_review" in l2_tools
    assert "Entrypoint inspected" in json.dumps(requests[-1]["input"])


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
