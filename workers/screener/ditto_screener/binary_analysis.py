"""Bounded, non-executing inspection of opaque submission artifacts."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from typing import IO

_MAX_ANALYZED_BYTES = 8 * 1024 * 1024
_MAX_HASHED_BYTES = 32 * 1024 * 1024
_MAX_STRINGS = 80
_MAX_STRING_LENGTH = 160
_MAX_PROTO_FIELDS = 4096
_MAX_ARCHIVE_ENTRIES = 128
_BENCHMARK_SCHEMA_MARKERS = (
    "answer_items",
    "expected_answer",
    "forbidden_answer",
    "injection_attempt",
    "memory_cases",
    "run_after_wave",
    "tool_cases",
)

# Bounded, advisory-only saturation signal for opaque model/vocab blobs. It is
# NEVER a tripwire or a finding on its own: a genuine TF-IDF vocabulary is
# dominated by word tokens and an opaque reranker exposes no vocab, so both stay
# low. It reports only bucketed counts/ratios and never the token values,
# preserving the locations/ratios-only boundary. The reviewer must still prove
# runtime reachability AND that the saturated literals are consumed as a served
# answer (not retrieval features / user-scoped values) before it can matter.
_ANSWER_SHAPED_TOKEN_SCAN_CAP = 40_000
_TOKEN_PATTERN = re.compile(rb"[A-Za-z0-9][A-Za-z0-9-]{2,31}")
# Bare 4-6 digit codes: the challenge verification-code / count shape.
_BARE_CODE_SHAPE = re.compile(r"^\d{4,6}$")
# Coined needle shapes (persona.CoinShaped): VK-ABCDEFGH23, GAVOTU-8842,
# 84-GAVO-TUKE. A consonant/digit-heavy segmented token, not a dictionary word.
_COINED_NEEDLE_SHAPE = re.compile(
    r"^(?:VK-[A-Z0-9]{6,}|[A-Z]{4,8}-\d{3,4}|\d{2}-[A-Z]{4}-[A-Z]{4})$"
)


@dataclass(frozen=True)
class BinarySample:
    """A bounded prefix plus immutable whole-file metadata."""

    data: bytes
    size: int
    sha256: str
    hashed_bytes: int

    @property
    def truncated(self) -> bool:
        return self.size > len(self.data)

    @property
    def hash_complete(self) -> bool:
        return self.hashed_bytes >= self.size


@dataclass(frozen=True)
class _ProtoField:
    number: int
    wire_type: int
    value: int | bytes
    declared_bytes: int | None = None
    complete: bool = True


@dataclass(frozen=True)
class _ProtoParse:
    fields: list[_ProtoField]
    status: str

    @property
    def complete(self) -> bool:
        return self.status == "complete"


def analyze_binary(sample: BinarySample, *, path: str) -> dict[str, object]:
    """Return bounded structural facts without executing or deserializing code."""

    data = sample.data
    strings = _printable_strings(data)
    lowered = data.lower()
    markers = [item for item in _BENCHMARK_SCHEMA_MARKERS if item.encode() in lowered]
    detected_format, confidence, details = _detect_format(data, sample.truncated)
    return {
        "path": path,
        "bytes": sample.size,
        "sha256": sample.sha256,
        "sha256_bytes": sample.hashed_bytes,
        "sha256_complete": sample.hash_complete,
        "analyzed_bytes": len(data),
        "analysis_truncated": sample.truncated,
        "format": detected_format,
        "format_confidence": confidence,
        "entropy_bits_per_byte": _entropy(data),
        "benchmark_schema_markers": markers,
        "answer_shaped_tokens": _answer_shaped_token_density(data),
        "printable_strings": strings,
        "details": details,
        "safety": {
            "executed": False,
            "decompressed_payloads": False,
            "external_data_loaded": False,
        },
    }


def _answer_shaped_token_density(data: bytes) -> dict[str, object]:
    """Bounded, values-free saturation stat for opaque model/vocab blobs.

    Tokenizes a bounded prefix and reports how many tokens are answer-shaped
    (bare verification codes / coined needle shapes) versus total tokens. Only
    counts and a coarse ratio bucket leave this module; the matched tokens
    never do. This is advisory context for the reviewer, not a tripwire.
    """
    bare_codes = 0
    coined_needles = 0
    total = 0
    for match in _TOKEN_PATTERN.finditer(data):
        if total >= _ANSWER_SHAPED_TOKEN_SCAN_CAP:
            break
        total += 1
        token = match.group().decode("ascii", errors="replace")
        if _BARE_CODE_SHAPE.match(token):
            bare_codes += 1
        elif _COINED_NEEDLE_SHAPE.match(token.upper()):
            coined_needles += 1
    answer_shaped = bare_codes + coined_needles
    ratio = (answer_shaped / total) if total else 0.0
    if ratio >= 0.4:
        bucket = "high"
    elif ratio >= 0.15:
        bucket = "elevated"
    elif ratio > 0.0:
        bucket = "low"
    else:
        bucket = "none"
    return {
        "total_tokens": total,
        "bare_code_tokens": bare_codes,
        "coined_needle_tokens": coined_needles,
        "answer_shaped_ratio_bucket": bucket,
        "scan_capped": total >= _ANSWER_SHAPED_TOKEN_SCAN_CAP,
    }


def compact_binary_analysis(value: dict[str, object]) -> dict[str, object]:
    """Trim a full analysis for the reviewer's initial inventory."""

    detected_format = value.get("format")
    details = value.get("details")
    strings = value.get("printable_strings")
    compact_details: object = details
    if detected_format == "safetensors" and isinstance(details, dict):
        tensors = details.get("tensors")
        compact_details = {
            key: item for key, item in details.items() if key != "tensors"
        }
        if isinstance(tensors, list):
            compact_details["tensor_names"] = [
                item.get("name")
                for item in tensors[:8]
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            ]
    elif detected_format == "zip" and isinstance(details, dict):
        entries = details.get("entries")
        compact_details = {
            key: item for key, item in details.items() if key != "entries"
        }
        if isinstance(entries, list):
            compact_details["entry_paths"] = [
                item.get("path")
                for item in entries[:12]
                if isinstance(item, dict) and isinstance(item.get("path"), str)
            ]

    return {
        "path": value.get("path"),
        "bytes": value.get("bytes"),
        "sha256": value.get("sha256"),
        "sha256_bytes": value.get("sha256_bytes"),
        "sha256_complete": value.get("sha256_complete"),
        "analyzed_bytes": value.get("analyzed_bytes"),
        "analysis_truncated": value.get("analysis_truncated"),
        "format": detected_format,
        "format_confidence": value.get("format_confidence"),
        "entropy_bits_per_byte": value.get("entropy_bits_per_byte"),
        "benchmark_schema_markers": value.get("benchmark_schema_markers"),
        "answer_shaped_tokens": value.get("answer_shaped_tokens"),
        "printable_strings": strings[:6] if isinstance(strings, list) else [],
        "details": compact_details,
    }


def sample_stream(stream: IO[bytes], *, size: int) -> BinarySample:
    """Read a bounded prefix while hashing the complete member."""

    digest = hashlib.sha256()
    prefix = bytearray()
    hashed_bytes = 0
    while hashed_bytes < min(size, _MAX_HASHED_BYTES):
        chunk = stream.read(min(1024 * 1024, _MAX_HASHED_BYTES - hashed_bytes))
        if not chunk:
            break
        digest.update(chunk)
        hashed_bytes += len(chunk)
        remaining = _MAX_ANALYZED_BYTES - len(prefix)
        if remaining > 0:
            prefix.extend(chunk[:remaining])
    return BinarySample(
        data=bytes(prefix),
        size=size,
        sha256=digest.hexdigest(),
        hashed_bytes=hashed_bytes,
    )


def _detect_format(data: bytes, truncated: bool) -> tuple[str, str, dict[str, object]]:
    if data.startswith(b"\x7fELF"):
        return "elf", "high", _elf_details(data)
    if data.startswith(b"MZ"):
        return "pe", "high", _pe_details(data)
    if data[:4] in {
        b"\xfe\xed\xfa\xce",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xcf\xfa\xed\xfe",
        b"\xca\xfe\xba\xbe",
    }:
        return "mach-o", "high", {"magic": data[:4].hex()}
    if data.startswith(b"\x00asm"):
        wasm_version = int.from_bytes(data[4:8], "little") if len(data) >= 8 else None
        return "webassembly", "high", {"version": wasm_version}
    if data.startswith(b"SQLite format 3\x00"):
        page_size = int.from_bytes(data[16:18], "big") if len(data) >= 18 else None
        if page_size == 1:
            page_size = 65536
        return "sqlite3", "high", {"page_size": page_size}
    if data.startswith(b"PK\x03\x04"):
        return "zip", "high", _zip_details(data, truncated)
    if data.startswith(b"\x1f\x8b"):
        return (
            "gzip",
            "high",
            {"compression_method": data[2] if len(data) > 2 else None},
        )
    if data.startswith(b"%PDF-"):
        pdf_version = data[5:8].decode("ascii", errors="replace")
        return "pdf", "high", {"version": pdf_version}
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "high", _png_details(data)
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg", "high", {}

    safetensors = _safetensors_details(data, truncated)
    if safetensors is not None:
        confidence = "high" if safetensors.get("payload_available") else "medium"
        return "safetensors", confidence, safetensors

    onnx = _onnx_details(data)
    if onnx is not None:
        confidence = (
            "high"
            if onnx.get("graph_complete") and onnx.get("metadata_complete")
            else "medium"
        )
        return "onnx", confidence, onnx

    protobuf = _protobuf_summary(data)
    if protobuf is not None:
        return "protobuf", "low", protobuf
    return "binary-data", "low", {}


def _entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    return round(
        -sum((count / total) * math.log2(count / total) for count in counts.values()),
        3,
    )


def _printable_strings(data: bytes) -> list[str]:
    strings: list[str] = []
    current = bytearray()
    for value in data:
        if 32 <= value <= 126 or value in {9}:
            current.append(value)
            if len(current) >= _MAX_STRING_LENGTH:
                strings.append(current.decode("ascii", errors="replace"))
                current.clear()
        else:
            if len(current) >= 4:
                strings.append(current.decode("ascii", errors="replace"))
            current.clear()
        if len(strings) >= _MAX_STRINGS:
            break
    if len(strings) < _MAX_STRINGS and len(current) >= 4:
        strings.append(current.decode("ascii", errors="replace"))
    return strings[:_MAX_STRINGS]


def _elf_details(data: bytes) -> dict[str, object]:
    if len(data) < 20:
        return {"truncated_header": True}
    bits = {1: 32, 2: 64}.get(data[4])
    byte_order = "little" if data[5] == 1 else "big" if data[5] == 2 else None
    details: dict[str, object] = {
        "bits": bits,
        "byte_order": byte_order,
        "os_abi": data[7],
    }
    if byte_order is not None:
        details["type"] = int.from_bytes(
            data[16:18], "little" if byte_order == "little" else "big"
        )
        details["machine"] = int.from_bytes(
            data[18:20], "little" if byte_order == "little" else "big"
        )
        entry_end = 32 if bits == 64 else 28
        if len(data) >= entry_end:
            details["entrypoint"] = int.from_bytes(
                data[24:entry_end], "little" if byte_order == "little" else "big"
            )
    return details


def _pe_details(data: bytes) -> dict[str, object]:
    if len(data) < 64:
        return {"truncated_header": True}
    offset = int.from_bytes(data[60:64], "little")
    if offset + 24 > len(data) or data[offset : offset + 4] != b"PE\x00\x00":
        return {"valid_pe_signature": False}
    return {
        "valid_pe_signature": True,
        "machine": int.from_bytes(data[offset + 4 : offset + 6], "little"),
        "section_count": int.from_bytes(data[offset + 6 : offset + 8], "little"),
        "characteristics": int.from_bytes(data[offset + 22 : offset + 24], "little"),
    }


def _png_details(data: bytes) -> dict[str, object]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return {"truncated_header": True}
    return {
        "width": int.from_bytes(data[16:20], "big"),
        "height": int.from_bytes(data[20:24], "big"),
    }


def _zip_details(data: bytes, truncated: bool) -> dict[str, object]:
    if truncated:
        return {"entries_available": False, "reason": "analysis-prefix-only"}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = [
                {
                    "path": item.filename[:240],
                    "compressed_bytes": item.compress_size,
                    "uncompressed_bytes": item.file_size,
                    "encrypted": bool(item.flag_bits & 0x1),
                }
                for item in archive.infolist()[:_MAX_ARCHIVE_ENTRIES]
            ]
            return {
                "entries": entries,
                "entry_count": len(archive.infolist()),
                "entries_truncated": len(archive.infolist()) > len(entries),
            }
    except (OSError, ValueError, zipfile.BadZipFile):
        return {"entries_available": False, "reason": "invalid-central-directory"}


def _safetensors_details(data: bytes, truncated: bool) -> dict[str, object] | None:
    if len(data) < 10:
        return None
    header_bytes = int.from_bytes(data[:8], "little")
    if not 2 <= header_bytes <= 8 * 1024 * 1024 or 8 + header_bytes > len(data):
        return None
    try:
        header = json.loads(data[8 : 8 + header_bytes])
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(header, dict):
        return None
    payload_bytes = len(data) - 8 - header_bytes
    candidates: list[tuple[int, int, str, dict[str, object]]] = []
    invalid_ranges = 0
    for name, value in header.items():
        if name == "__metadata__" or not isinstance(value, dict):
            continue
        offsets = value.get("data_offsets")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or not all(
                isinstance(item, int) and not isinstance(item, bool) for item in offsets
            )
        ):
            invalid_ranges += 1
            continue
        start, end = offsets
        if start < 0 or end < start or end > payload_bytes:
            invalid_ranges += 1
            continue
        candidates.append((start, end, str(name)[:160], value))

    tensors: list[dict[str, object]] = []
    tensor_count = 0
    total_tensor_bytes = 0
    previous_end = 0
    for start, end, name, value in sorted(candidates):
        if start < previous_end:
            invalid_ranges += 1
            continue
        previous_end = end
        tensor_count += 1
        total_tensor_bytes += end - start
        if len(tensors) < 64:
            tensors.append(
                {
                    "name": name,
                    "dtype": value.get("dtype"),
                    "shape": value.get("shape"),
                    "bytes": end - start,
                }
            )
    if tensor_count == 0:
        return None
    return {
        "header_bytes": header_bytes,
        "tensor_count": tensor_count,
        "tensors": tensors,
        "tensor_bytes": total_tensor_bytes,
        "invalid_tensor_ranges": invalid_ranges,
        "payload_available": not truncated and invalid_ranges == 0,
    }


def _read_varint(data: bytes, offset: int) -> tuple[int, int] | None:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            return None
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
    return None


def _protobuf_fields(data: bytes) -> _ProtoParse | None:
    fields: list[_ProtoField] = []
    offset = 0
    while offset < len(data):
        if len(fields) >= _MAX_PROTO_FIELDS:
            return _ProtoParse(fields, "field_limit")
        key_result = _read_varint(data, offset)
        if key_result is None:
            return _ProtoParse(fields, "truncated") if fields else None
        key, offset = key_result
        number, wire_type = key >> 3, key & 0x07
        if number < 1 or wire_type not in {0, 1, 2, 5}:
            return _ProtoParse(fields, "malformed") if fields else None
        if wire_type == 0:
            result = _read_varint(data, offset)
            if result is None:
                return _ProtoParse(fields, "truncated")
            value, offset = result
            fields.append(_ProtoField(number, wire_type, value))
        elif wire_type == 1:
            if offset + 8 > len(data):
                return _ProtoParse(fields, "truncated")
            fields.append(_ProtoField(number, wire_type, data[offset : offset + 8]))
            offset += 8
        elif wire_type == 5:
            if offset + 4 > len(data):
                return _ProtoParse(fields, "truncated")
            fields.append(_ProtoField(number, wire_type, data[offset : offset + 4]))
            offset += 4
        else:
            result = _read_varint(data, offset)
            if result is None:
                return _ProtoParse(fields, "truncated")
            length, offset = result
            end = offset + length
            complete = end <= len(data)
            fields.append(
                _ProtoField(
                    number,
                    wire_type,
                    data[offset:end] if complete else data[offset:],
                    declared_bytes=length,
                    complete=complete,
                )
            )
            if not complete:
                return _ProtoParse(fields, "truncated")
            offset = end
    return _ProtoParse(fields, "complete") if fields else None


def _onnx_details(data: bytes) -> dict[str, object] | None:
    model_parse = _protobuf_fields(data)
    if model_parse is None:
        return None
    fields = model_parse.fields
    ir = next(
        (field.value for field in fields if field.number == 1 and field.wire_type == 0),
        None,
    )
    graph = next(
        (field for field in fields if field.number == 7 and field.wire_type == 2),
        None,
    )
    if not isinstance(ir, int) or not 1 <= ir <= 100 or graph is None:
        return None
    details: dict[str, object] = {
        "ir_version": ir,
        "graph_bytes": graph.declared_bytes,
        "model_parse_status": model_parse.status,
        "graph_complete": False,
    }
    metadata_complete = model_parse.complete
    opsets: list[dict[str, object]] = []
    for field in fields:
        if field.number != 8 or field.wire_type != 2 or not field.complete:
            continue
        assert isinstance(field.value, bytes)
        nested_parse = _protobuf_fields(field.value)
        metadata_complete = metadata_complete and bool(
            nested_parse and nested_parse.complete
        )
        nested = nested_parse.fields if nested_parse else []
        domain = _proto_text(nested, 1)
        version = _proto_int(nested, 2)
        opsets.append({"domain": domain, "version": version})
    details["opsets"] = opsets[:32]
    if not graph.complete:
        details["metadata_complete"] = False
        return details
    assert isinstance(graph.value, bytes)
    graph_parse = _protobuf_fields(graph.value)
    graph_fields = graph_parse.fields if graph_parse else []
    graph_complete = bool(graph_parse and graph_parse.complete)
    details["graph_parse_status"] = graph_parse.status if graph_parse else "malformed"
    details["graph_complete"] = graph_complete
    metadata_complete = metadata_complete and graph_complete
    operator_types, operators_complete = _onnx_operator_types(graph_fields)
    initializer_bytes, external_data, initializers_complete = _onnx_initializer_details(
        graph_fields
    )
    metadata_complete = (
        metadata_complete and operators_complete and initializers_complete
    )
    details.update(
        {
            "graph_name": _proto_text(graph_fields, 2),
            "node_count": sum(field.number == 1 for field in graph_fields),
            "initializer_count": sum(field.number == 5 for field in graph_fields),
            "input_count": sum(field.number == 11 for field in graph_fields),
            "output_count": sum(field.number == 12 for field in graph_fields),
            "operator_types": operator_types,
            "initializer_bytes": initializer_bytes,
            "external_data_references": external_data,
            "metadata_complete": metadata_complete,
        }
    )
    return details


def _onnx_operator_types(fields: list[_ProtoField]) -> tuple[list[str], bool]:
    operators: list[str] = []
    complete = True
    for field in fields:
        if field.number != 1 or field.wire_type != 2 or not field.complete:
            continue
        assert isinstance(field.value, bytes)
        node_parse = _protobuf_fields(field.value)
        complete = complete and bool(node_parse and node_parse.complete)
        operator = _proto_text(node_parse.fields if node_parse else [], 4)
        if operator and operator not in operators:
            operators.append(operator)
    return operators[:64], complete


def _onnx_initializer_details(fields: list[_ProtoField]) -> tuple[int, int, bool]:
    total = 0
    external_data = 0
    complete = True
    for field in fields:
        if field.number != 5 or field.wire_type != 2 or not field.complete:
            continue
        assert isinstance(field.value, bytes)
        tensor_parse = _protobuf_fields(field.value)
        complete = complete and bool(tensor_parse and tensor_parse.complete)
        tensor_fields = tensor_parse.fields if tensor_parse else []
        for tensor_field in tensor_fields:
            if tensor_field.number == 9 and tensor_field.declared_bytes is not None:
                total += tensor_field.declared_bytes
        external_data += sum(item.number == 13 for item in tensor_fields)
    return total, external_data, complete


def _proto_text(fields: list[_ProtoField], number: int) -> str | None:
    for field in fields:
        if field.number == number and field.wire_type == 2 and field.complete:
            assert isinstance(field.value, bytes)
            try:
                return field.value.decode("utf-8")[:160]
            except UnicodeDecodeError:
                return None
    return None


def _proto_int(fields: list[_ProtoField], number: int) -> int | None:
    for field in fields:
        if field.number == number and field.wire_type == 0:
            assert isinstance(field.value, int)
            return field.value
    return None


def _protobuf_summary(data: bytes) -> dict[str, object] | None:
    parsed = _protobuf_fields(data)
    if parsed is None or len(parsed.fields) < 2:
        return None
    fields = parsed.fields
    counts = Counter(field.number for field in fields)
    return {
        "top_level_field_counts": {
            str(number): count for number, count in sorted(counts.items())[:64]
        },
        "parsed_fields": len(fields),
        "parse_complete": parsed.complete,
        "parse_status": parsed.status,
    }
