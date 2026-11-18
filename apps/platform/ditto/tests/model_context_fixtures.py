"""Builders for model_context tests: safetensors bytes, repos, a fake fetcher."""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import AsyncIterator, Mapping
from typing import Any

from ditto.model_context_artifacts import ArtifactRejected, ArtifactUnavailable

_DTYPE_BYTES = {"F16": 2, "BF16": 2, "F32": 4}


def safetensors_bytes(
    tensors: Mapping[str, tuple[str, list[int], bytes | None]],
    *,
    metadata: dict[str, str] | None = None,
    order: list[str] | None = None,
) -> bytes:
    """A valid safetensors file. ``bytes`` of ``None`` fills with a pattern."""
    header: dict[str, Any] = {}
    if metadata is not None:
        header["__metadata__"] = metadata
    data = b""
    for name in order or list(tensors):
        dtype, shape, raw = tensors[name]
        size = _DTYPE_BYTES[dtype]
        for dim in shape:
            size *= dim
        if raw is None:
            raw = pattern(name, size)
        assert len(raw) == size
        header[name] = {
            "dtype": dtype,
            "shape": shape,
            "data_offsets": [len(data), len(data) + size],
        }
        data += raw
    encoded = json.dumps(header).encode()
    return struct.pack("<Q", len(encoded)) + encoded + data


def pattern(label: str, size: int) -> bytes:
    seed = hashlib.sha256(label.encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


def lora_tensors(
    rank: int = 4, modules: tuple[str, ...] = ("q_proj", "v_proj"), salt: str = ""
) -> dict[str, tuple[str, list[int], bytes | None]]:
    out: dict[str, tuple[str, list[int], bytes | None]] = {}
    for module in modules:
        prefix = f"base_model.model.model.layers.0.self_attn.{module}"
        a, b = f"{prefix}.lora_A.weight", f"{prefix}.lora_B.weight"
        out[a] = ("BF16", [rank, 8], pattern(salt + a, rank * 8 * 2))
        out[b] = ("BF16", [8, rank], pattern(salt + b, rank * 8 * 2))
    return out


def lora_repo(
    *,
    rank: int = 4,
    target_modules: list[str] | None = None,
    tensor_modules: tuple[str, ...] = ("q_proj", "v_proj"),
    salt: str = "",
    base_id: str = "qwen3.5-0.8b",
    license: str = "apache-2.0",
    extra_config: dict[str, Any] | None = None,
) -> tuple[str, dict[str, bytes]]:
    """``(manifest_text, files)`` for a LoRA repo."""
    config = {
        "peft_type": "LORA",
        "r": rank,
        "lora_alpha": 16,
        "target_modules": target_modules or list(tensor_modules),
        **(extra_config or {}),
    }
    files = {
        "adapter_config.json": json.dumps(config).encode(),
        "adapter_model.safetensors": safetensors_bytes(
            lora_tensors(rank, tensor_modules, salt)
        ),
    }
    return manifest_for(files, base_id=base_id, kind="lora", license=license), files


def manifest_for(
    files: Mapping[str, bytes],
    *,
    base_id: str = "qwen3.5-0.8b",
    kind: str = "lora",
    license: str = "apache-2.0",
) -> str:
    return json.dumps(
        {
            "schema": "ditto-cpm-manifest-v1",
            "track": "model_context",
            "base_id": base_id,
            "kind": kind,
            "files": [
                {
                    "path": path,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                }
                for path, raw in sorted(files.items())
            ],
            "license": license,
            "training_data_attestation": "trained on exporter data only",
        },
        indent=2,
    )


class FakeFetcher:
    """Serves ``{(repo, revision): {path: bytes}}`` in small chunks."""

    def __init__(self) -> None:
        self.repos: dict[tuple[str, str], dict[str, bytes]] = {}
        self.unavailable = False
        self.requests: list[tuple[str, str, str]] = []

    def put(self, repo: str, revision: str, files: Mapping[str, bytes]) -> None:
        self.repos[(repo, revision)] = dict(files)

    async def stream(self, repo: str, revision: str, path: str) -> AsyncIterator[bytes]:
        self.requests.append((repo, revision, path))
        if self.unavailable:
            raise ArtifactUnavailable("fake outage")
        files = self.repos.get((repo, revision))
        if files is None or path not in files:
            raise ArtifactRejected(f"{path} is not public at revision {revision}")
        raw = files[path]
        for start in range(0, len(raw), 7):
            yield raw[start : start + 7]
