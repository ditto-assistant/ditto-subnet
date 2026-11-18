"""Static screening of model_context submissions (CPM contract sections 2-3).

Everything here runs before a model is ever loaded: manifest rules, streaming
download with per-file SHA-256, ``safetensors`` header sanity, LoRA rank and
target modules, full fine-tune architecture and parameter count, and the
canonical ``weights_sha256``.

``weights_sha256`` (contract section 3.3) is SHA-256 over the canonical tensor
stream. Tensors from every ``.safetensors`` file in the manifest are sorted by
name and each is written as::

    u32le(len(name)) || name (UTF-8)
    u32le(len(dtype)) || dtype (ASCII, the safetensors dtype tag, e.g. BF16)
    u32le(ndim) || u64le(dim) for each dim
    u64le(nbytes) || raw tensor bytes (safetensors stores little-endian)

This is byte-identical to ``weights_sha256`` in ``services/cpm-scorer``
(``cpm_scorer/screening.py``); ``ditto/tests/test_model_context_artifacts.py``
pins a shared test vector.

so re-sharding, reordering, or changing ``__metadata__`` cannot make a copy
look new, and the hash is computed from the header offsets without loading a
tensor into memory.

Nothing here holds a weight file in memory: downloads stream to disk in
``CHUNK_BYTES`` pieces and hashing reads back in the same pieces.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from ditto.api_models.model_context import (
    ALLOWED_LICENSES,
    MANIFEST_FILENAME,
    MAX_MANIFEST_BYTES,
    CpmManifest,
    ModelContextBase,
    ModelContextSettings,
)

CHUNK_BYTES = 1 << 20
MAX_HEADER_BYTES = 100 << 20
MAX_CONFIG_BYTES = 1 << 20
PARAMETER_TOLERANCE = 0.01

LORA_FILES = frozenset({"adapter_config.json", "adapter_model.safetensors"})
FULL_CONFIG = "config.json"
FULL_INDEX = "model.safetensors.index.json"
_FULL_SHARD = re.compile(r"^model[A-Za-z0-9._-]*\.safetensors$")
_ROOT_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

DTYPE_BYTES = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E5M2": 1,
    "F8_E4M3": 1,
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "F64": 8,
    "I64": 8,
    "U64": 8,
}
WEIGHT_DTYPES = frozenset({"F16", "BF16", "F32"})
_FORBIDDEN_CONFIG_KEYS = ("auto_map", "trust_remote_code")


class ArtifactRejected(ValueError):
    """The submission's artifacts break the model contract (the miner's fault)."""


class ArtifactUnavailable(RuntimeError):
    """The artifacts could not be fetched (retryable, not the miner's fault yet)."""


# --- manifest ----------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ArtifactRejected("manifest JSON repeats a key")
    return dict(pairs)


def parse_manifest(text: str, settings: ModelContextSettings) -> CpmManifest:
    """Parse and check ``cpm-manifest.json`` (contract sections 2.2-2.3)."""
    raw = text.encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ArtifactRejected(f"manifest is larger than {MAX_MANIFEST_BYTES} bytes")
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except json.JSONDecodeError as exc:
        raise ArtifactRejected(f"manifest is not valid JSON: {exc.msg}") from exc
    except RecursionError as exc:
        # Adversarially deep nesting is the submission's fault: reject it
        # instead of letting it escape as a host error and be retried.
        raise ArtifactRejected("manifest JSON is nested too deeply") from exc
    if not isinstance(data, dict):
        raise ArtifactRejected("manifest must be a JSON object")
    try:
        manifest = CpmManifest.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ())) or "manifest"
        raise ArtifactRejected(f"manifest {where}: {first.get('msg')}") from exc
    if manifest.license not in ALLOWED_LICENSES:
        raise ArtifactRejected(
            f"license must be one of {list(ALLOWED_LICENSES)}, not {manifest.license!r}"
        )
    if settings.base(manifest.base_id) is None:
        allowed = [b.base_id for b in settings.base_allowlist]
        raise ArtifactRejected(
            f"base_id {manifest.base_id!r} is not allowlisted; allowed: {allowed}"
        )
    check_file_set(manifest)
    if manifest.total_bytes > settings.max_total_bytes:
        raise ArtifactRejected(
            f"files total {manifest.total_bytes} bytes, above the "
            f"{settings.max_total_bytes}-byte cap"
        )
    return manifest


def check_file_set(manifest: CpmManifest) -> None:
    """Only the files the kind needs; no code, pickle, or subdirectory."""
    paths = [f.path for f in manifest.files]
    if len(set(paths)) != len(paths):
        raise ArtifactRejected("manifest lists a file twice")
    for path in paths:
        if not _ROOT_FILE.fullmatch(path) or ".." in path:
            raise ArtifactRejected(f"file path {path!r} must be a plain root file name")
        if path == MANIFEST_FILENAME:
            raise ArtifactRejected("the manifest must not list itself")
    if manifest.kind == "lora":
        if set(paths) != LORA_FILES:
            raise ArtifactRejected(
                f"a lora submission lists exactly {sorted(LORA_FILES)}"
            )
        return
    if FULL_CONFIG not in paths:
        raise ArtifactRejected("a full submission lists config.json")
    shards = [p for p in paths if _FULL_SHARD.fullmatch(p)]
    if not shards:
        raise ArtifactRejected("a full submission lists model*.safetensors shards")
    extra = sorted(set(paths) - {FULL_CONFIG, FULL_INDEX, *shards})
    if extra:
        raise ArtifactRejected(
            f"a full submission may list only config.json, {FULL_INDEX} and "
            f"model*.safetensors; also listed {extra}"
        )


# --- fetching ----------------------------------------------------------------


class ArtifactFetcher(Protocol):
    def stream(self, repo: str, revision: str, path: str) -> AsyncIterator[bytes]:
        """Yield the file's bytes at ``revision``; raise ArtifactUnavailable."""
        ...


class HuggingFaceFetcher:
    """Plain-HTTPS ``resolve`` reads of public repos; never sends a token."""

    def __init__(
        self,
        *,
        base_url: str = "https://huggingface.co",
        timeout_seconds: float = 120.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds, connect=20.0)

    def url(self, repo: str, revision: str, path: str) -> str:
        return (
            f"{self._base}/{quote(repo, safe='/')}/resolve/"
            f"{quote(revision, safe='')}/{quote(path, safe='')}"
        )

    async def stream(self, repo: str, revision: str, path: str) -> AsyncIterator[bytes]:
        url = self.url(repo, revision, path)
        try:
            async with (
                httpx.AsyncClient(
                    timeout=self._timeout, follow_redirects=True, max_redirects=5
                ) as client,
                client.stream("GET", url) as response,
            ):
                if response.status_code in (401, 403, 404):
                    raise ArtifactRejected(
                        f"{path} is not public at revision {revision} "
                        f"(HTTP {response.status_code})"
                    )
                if response.status_code != 200:
                    raise ArtifactUnavailable(
                        f"fetching {path} returned HTTP {response.status_code}"
                    )
                async for chunk in response.aiter_bytes(CHUNK_BYTES):
                    yield chunk
        except httpx.HTTPError as exc:
            raise ArtifactUnavailable(
                f"fetching {path} failed: {type(exc).__name__}"
            ) from exc


@dataclass(frozen=True)
class Downloaded:
    path: Path
    sha256: str
    size: int


async def download(
    fetcher: ArtifactFetcher,
    *,
    repo: str,
    revision: str,
    name: str,
    dest: Path,
    max_bytes: int,
) -> Downloaded:
    """Stream one file to ``dest``, hashing as it goes; refuse past ``max_bytes``."""
    digest = hashlib.sha256()
    size = 0
    with dest.open("wb") as out:
        async for chunk in fetcher.stream(repo, revision, name):
            size += len(chunk)
            if size > max_bytes:
                raise ArtifactRejected(
                    f"{name} is larger than its {max_bytes} declared bytes"
                )
            digest.update(chunk)
            out.write(chunk)
    return Downloaded(path=dest, sha256=digest.hexdigest(), size=size)


# --- safetensors -------------------------------------------------------------


@dataclass(frozen=True)
class TensorInfo:
    name: str
    dtype: str
    shape: tuple[int, ...]
    start: int
    end: int

    @property
    def numel(self) -> int:
        return math.prod(self.shape)


@dataclass(frozen=True)
class SafetensorsFile:
    path: Path
    data_start: int
    tensors: tuple[TensorInfo, ...]


def read_safetensors(path: Path) -> SafetensorsFile:
    """Parse and sanity-check one ``safetensors`` header (never the data)."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        prefix = handle.read(8)
        if len(prefix) != 8:
            raise ArtifactRejected(f"{path.name} is too short to be safetensors")
        (header_len,) = struct.unpack("<Q", prefix)
        if header_len < 2 or header_len > MAX_HEADER_BYTES or 8 + header_len > size:
            raise ArtifactRejected(f"{path.name} has an invalid header length")
        raw = handle.read(header_len)
    try:
        header = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ArtifactRejected(f"{path.name} header is not JSON") from exc
    if not isinstance(header, dict):
        raise ArtifactRejected(f"{path.name} header must be a JSON object")
    metadata = header.pop("__metadata__", None)
    if metadata is not None and not (
        isinstance(metadata, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())
    ):
        raise ArtifactRejected(f"{path.name} __metadata__ must map strings to strings")
    data_start = 8 + header_len
    data_len = size - data_start
    tensors: list[TensorInfo] = []
    for name, entry in header.items():
        tensors.append(_tensor(path.name, name, entry))
    if not tensors:
        raise ArtifactRejected(f"{path.name} holds no tensors")
    cursor = 0
    for tensor in sorted(tensors, key=lambda t: (t.start, t.end)):
        if tensor.start != cursor:
            raise ArtifactRejected(
                f"{path.name} tensor data is not contiguous at {tensor.name!r}"
            )
        cursor = tensor.end
    if cursor != data_len:
        raise ArtifactRejected(
            f"{path.name} tensor data covers {cursor} of {data_len} bytes"
        )
    return SafetensorsFile(path=path, data_start=data_start, tensors=tuple(tensors))


def _tensor(file: str, name: str, entry: Any) -> TensorInfo:
    if not name or len(name) > 512 or not isinstance(entry, dict):
        raise ArtifactRejected(f"{file} has a malformed tensor entry {name[:80]!r}")
    dtype = entry.get("dtype")
    shape = entry.get("shape")
    offsets = entry.get("data_offsets")
    if dtype not in WEIGHT_DTYPES:
        raise ArtifactRejected(
            f"{file} tensor {name!r} has dtype {dtype!r}; allowed: "
            f"{sorted(WEIGHT_DTYPES)}"
        )
    if (
        not isinstance(shape, list)
        or len(shape) > 8
        or any(type(d) is not int or d < 0 for d in shape)
    ):
        raise ArtifactRejected(f"{file} tensor {name!r} has an invalid shape")
    if (
        not isinstance(offsets, list)
        or len(offsets) != 2
        or any(type(o) is not int or o < 0 for o in offsets)
        or offsets[1] < offsets[0]
    ):
        raise ArtifactRejected(f"{file} tensor {name!r} has invalid data_offsets")
    info = TensorInfo(
        name=name, dtype=dtype, shape=tuple(shape), start=offsets[0], end=offsets[1]
    )
    if info.end - info.start != info.numel * DTYPE_BYTES[dtype]:
        raise ArtifactRejected(
            f"{file} tensor {name!r} byte size does not match its dtype and shape"
        )
    return info


def canonical_weights_sha256(files: Iterable[SafetensorsFile]) -> str:
    """The canonical tensor-stream digest (see the module docstring)."""
    located: dict[str, tuple[SafetensorsFile, TensorInfo]] = {}
    for file in files:
        for tensor in file.tensors:
            if tensor.name in located:
                raise ArtifactRejected(f"tensor {tensor.name!r} appears in two files")
            located[tensor.name] = (file, tensor)
    digest = hashlib.sha256()
    u32 = struct.Struct("<I")
    u64 = struct.Struct("<Q")
    for name in sorted(located):
        file, tensor = located[name]
        encoded = name.encode("utf-8")
        digest.update(u32.pack(len(encoded)))
        digest.update(encoded)
        digest.update(u32.pack(len(tensor.dtype)))
        digest.update(tensor.dtype.encode("ascii"))
        digest.update(u32.pack(len(tensor.shape)))
        for dim in tensor.shape:
            digest.update(u64.pack(dim))
        digest.update(u64.pack(tensor.end - tensor.start))
        with file.path.open("rb") as handle:
            handle.seek(file.data_start + tensor.start)
            remaining = tensor.end - tensor.start
            while remaining:
                chunk = handle.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ArtifactRejected(f"{file.path.name} ended inside {name!r}")
                digest.update(chunk)
                remaining -= len(chunk)
    return digest.hexdigest()


# --- kind checks -------------------------------------------------------------


def _json_file(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ArtifactRejected(f"{path.name} is larger than {MAX_CONFIG_BYTES} bytes")
    try:
        data = json.loads(path.read_text("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ArtifactRejected(f"{path.name} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ArtifactRejected(f"{path.name} must be a JSON object")
    for key in _FORBIDDEN_CONFIG_KEYS:
        if data.get(key):
            raise ArtifactRejected(f"{path.name} sets {key}; remote code is refused")
    return data


_LORA_TENSOR = re.compile(
    r"^(?P<module>.+)\.lora_(?P<side>[AB])(?:\.[A-Za-z0-9_]+)?\.weight$"
)


def check_lora(
    config_path: Path,
    adapter: SafetensorsFile,
    *,
    base: ModelContextBase,
    max_rank: int,
) -> dict[str, Any]:
    """Rank and target modules from ``adapter_config.json`` and the tensors."""
    config = _json_file(config_path)
    peft_type = config.get("peft_type", "LORA")
    if peft_type != "LORA":
        raise ArtifactRejected(f"adapter peft_type must be LORA, not {peft_type!r}")
    rank = config.get("r")
    if type(rank) is not int or rank < 1:
        raise ArtifactRejected("adapter_config.json r must be a positive integer")
    if rank > max_rank:
        raise ArtifactRejected(f"LoRA rank {rank} is above the cap of {max_rank}")
    for key in ("rank_pattern", "alpha_pattern"):
        pattern = config.get(key) or {}
        if not isinstance(pattern, dict):
            raise ArtifactRejected(f"adapter_config.json {key} must be an object")
        if key == "rank_pattern" and any(
            type(v) is not int or v < 1 or v > max_rank for v in pattern.values()
        ):
            raise ArtifactRejected(
                f"a rank_pattern rank is above the cap of {max_rank}"
            )
    if config.get("modules_to_save"):
        raise ArtifactRejected(
            "modules_to_save is refused; a LoRA trains adapters only"
        )
    allowed = set(base.lora_target_modules)
    targets = config.get("target_modules")
    if isinstance(targets, str):
        targets = [targets]
    if (
        not isinstance(targets, list)
        or not targets
        or any(not isinstance(t, str) for t in targets)
    ):
        raise ArtifactRejected("adapter_config.json target_modules must list names")
    outside = sorted(set(targets) - allowed)
    if outside:
        raise ArtifactRejected(
            f"target_modules {outside} are not allowed; allowed: {sorted(allowed)}"
        )
    max_seen = 0
    for tensor in adapter.tensors:
        match = _LORA_TENSOR.fullmatch(tensor.name)
        if match is None:
            raise ArtifactRejected(
                f"adapter tensor {tensor.name!r} is not a lora_A/lora_B weight"
            )
        module = match.group("module").rsplit(".", 1)[-1]
        if module not in allowed:
            raise ArtifactRejected(
                f"adapter tensor {tensor.name!r} targets {module!r}, not allowed"
            )
        if len(tensor.shape) != 2:
            raise ArtifactRejected(f"adapter tensor {tensor.name!r} is not 2-D")
        tensor_rank = tensor.shape[0] if match.group("side") == "A" else tensor.shape[1]
        if tensor_rank > max_rank:
            raise ArtifactRejected(
                f"adapter tensor {tensor.name!r} has rank {tensor_rank}, above "
                f"the cap of {max_rank}"
            )
        max_seen = max(max_seen, tensor_rank)
    return {
        "rank": rank,
        "max_tensor_rank": max_seen,
        "target_modules": sorted(set(targets)),
        "tensors": len(adapter.tensors),
        "parameters": sum(t.numel for t in adapter.tensors),
    }


def check_full(
    config_path: Path,
    shards: list[SafetensorsFile],
    *,
    index_path: Path | None,
    base: ModelContextBase,
) -> dict[str, Any]:
    """Architecture and parameter count against the base."""
    config = _json_file(config_path)
    architectures = config.get("architectures")
    if architectures != base.architectures:
        raise ArtifactRejected(
            f"config.json architectures {architectures!r} differ from the base's "
            f"{base.architectures}"
        )
    parameters = sum(t.numel for shard in shards for t in shard.tensors)
    if abs(parameters - base.parameters) > PARAMETER_TOLERANCE * base.parameters:
        raise ArtifactRejected(
            f"{parameters} parameters is not within 1% of the base's {base.parameters}"
        )
    if index_path is not None:
        index = _json_file(index_path)
        weight_map = index.get("weight_map")
        names = {t.name: s.path.name for s in shards for t in s.tensors}
        if not isinstance(weight_map, dict) or weight_map != names:
            raise ArtifactRejected(
                f"{FULL_INDEX} weight_map does not match the shards' tensors"
            )
    return {
        "parameters": parameters,
        "tensors": sum(len(s.tensors) for s in shards),
        "architectures": architectures,
    }


# --- the whole screen --------------------------------------------------------


@dataclass(frozen=True)
class ScreenResult:
    weights_sha256: str
    detail: dict[str, Any] = field(default_factory=dict)


async def fetch_and_verify(
    fetcher: ArtifactFetcher,
    *,
    hf_repo: str,
    revision: str,
    manifest_sha256: str,
    manifest: CpmManifest,
    workdir: Path,
) -> dict[str, Path]:
    """Download the manifest and every file it lists; verify bytes and digests.

    The repo's ``cpm-manifest.json`` must hash to the committed
    ``manifest_sha256``, so the files verified are the ones committed to."""
    fetched = await download(
        fetcher,
        repo=hf_repo,
        revision=revision,
        name=MANIFEST_FILENAME,
        dest=workdir / MANIFEST_FILENAME,
        max_bytes=MAX_MANIFEST_BYTES,
    )
    if fetched.sha256 != manifest_sha256:
        raise ArtifactRejected(
            f"{MANIFEST_FILENAME} at revision {revision} does not match the "
            "submitted and committed manifest"
        )
    paths: dict[str, Path] = {}
    for entry in manifest.files:
        got = await download(
            fetcher,
            repo=hf_repo,
            revision=revision,
            name=entry.path,
            dest=workdir / entry.path,
            max_bytes=entry.bytes,
        )
        if got.size != entry.bytes:
            raise ArtifactRejected(
                f"{entry.path} is {got.size} bytes; the manifest says {entry.bytes}"
            )
        if got.sha256 != entry.sha256:
            raise ArtifactRejected(f"{entry.path} sha256 does not match the manifest")
        paths[entry.path] = got.path
    return paths


def screen_files(
    manifest: CpmManifest,
    paths: dict[str, Path],
    *,
    base: ModelContextBase,
    max_lora_rank: int,
) -> ScreenResult:
    """Static screening of verified local files (blocking; run in a thread)."""
    tensor_files = sorted(p for p in paths if p.endswith(".safetensors"))
    parsed = [read_safetensors(paths[p]) for p in tensor_files]
    if manifest.kind == "lora":
        detail = check_lora(
            paths["adapter_config.json"],
            parsed[0],
            base=base,
            max_rank=max_lora_rank,
        )
    else:
        detail = check_full(
            paths[FULL_CONFIG],
            parsed,
            index_path=paths.get(FULL_INDEX),
            base=base,
        )
    detail["files"] = len(paths)
    detail["total_bytes"] = manifest.total_bytes
    return ScreenResult(weights_sha256=canonical_weights_sha256(parsed), detail=detail)
