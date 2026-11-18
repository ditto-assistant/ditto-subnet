"""Static screening for model_context submissions (contract §2.2, §2.3, §3.3).

Pure functions. Platform runs the same checks before queueing a run, and the
scorer re-runs them on the files it downloads. Every failure raises
`ScreeningError` with a stable `reason` code.

Canonical `weights_sha256` (contract §3.3): SHA-256 over every tensor in every
safetensors file of the submission, sorted by tensor name, each written as

    u32le(len(name)) name_utf8
    u32le(len(dtype)) dtype_ascii      # safetensors dtype string, e.g. "BF16"
    u32le(ndim) u64le(dim) * ndim
    u64le(len(data)) data              # raw little-endian bytes

The stream is independent of shard layout, file names, header order and
`__metadata__`, so resharding the same weights keeps the hash.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from cpm_scorer.bases import ALLOWED_LORA_TARGETS, BASES

MANIFEST_NAME = "cpm-manifest.json"
MANIFEST_SCHEMA = "ditto-cpm-manifest-v1"
TRACK = "model_context"
KINDS = ("lora", "full")
ALLOWED_LICENSES = frozenset({"apache-2.0", "mit"})
MAX_TOTAL_BYTES = 6 * 1024**3
MAX_LORA_RANK = 64
PARAM_TOLERANCE = 0.01
MAX_ATTESTATION_CHARS = 4000
# Executable code and pickle-based weight formats are never accepted anywhere
# in the repo, listed in the manifest or not.
FORBIDDEN_SUFFIXES = frozenset(
    {
        ".py",
        ".pyc",
        ".pyo",
        ".pyd",
        ".so",
        ".dylib",
        ".pkl",
        ".pickle",
        ".bin",
        ".pt",
        ".pth",
        ".ckpt",
        ".joblib",
        ".npy",
        ".npz",
        ".h5",
        ".msgpack",
    }
)
MANIFEST_SUFFIXES = frozenset({".json", ".safetensors"})
LORA_REQUIRED = ("adapter_config.json", "adapter_model.safetensors")
FULL_SHARD_RE = re.compile(r"^model[\w.-]*\.safetensors$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_LORA_KEY = re.compile(r"^(?P<module>.+)\.lora_(?P<ab>[AB])(?:\.default)?\.weight$")


class ScreeningError(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ManifestFile:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Manifest:
    base_id: str
    kind: str
    files: tuple[ManifestFile, ...]
    license: str
    training_data_attestation: str

    @property
    def total_bytes(self) -> int:
        return sum(f.bytes for f in self.files)

    def paths(self) -> list[str]:
        return [f.path for f in self.files]


def manifest_sha256(raw: bytes) -> str:
    """`manifest_sha256` is the SHA-256 of the manifest file's exact bytes."""
    return hashlib.sha256(raw).hexdigest()


def _safe_relpath(p: Any) -> str:
    if not isinstance(p, str) or not p:
        raise ScreeningError("invalid_manifest", f"bad file path {p!r}")
    pp = PurePosixPath(p)
    if pp.is_absolute() or ".." in pp.parts or "\\" in p or str(pp) != p:
        raise ScreeningError("invalid_manifest", f"unsafe file path {p!r}")
    return p


def validate_manifest(
    obj: Any,
    *,
    allowed_bases: Iterable[str] = BASES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
) -> Manifest:
    """Validate a parsed `cpm-manifest.json` (contract §2.3)."""
    if not isinstance(obj, dict):
        raise ScreeningError("invalid_manifest", "manifest is not an object")
    known = {
        "schema",
        "track",
        "base_id",
        "kind",
        "files",
        "license",
        "training_data_attestation",
    }
    extra = sorted(set(obj) - known)
    if extra:
        raise ScreeningError("invalid_manifest", f"unknown keys {extra}")
    if obj.get("schema") != MANIFEST_SCHEMA:
        raise ScreeningError("invalid_manifest", f"schema must be {MANIFEST_SCHEMA}")
    if obj.get("track") != TRACK:
        raise ScreeningError("invalid_manifest", f"track must be {TRACK}")
    base_id = obj.get("base_id")
    if not isinstance(base_id, str) or base_id not in set(allowed_bases):
        raise ScreeningError("base_not_allowed", f"base_id {base_id!r}")
    kind = obj.get("kind")
    if kind not in KINDS:
        raise ScreeningError("invalid_manifest", f"kind must be one of {KINDS}")
    lic = obj.get("license")
    if not isinstance(lic, str) or lic not in ALLOWED_LICENSES:
        raise ScreeningError(
            "license", f"license must be one of {sorted(ALLOWED_LICENSES)}"
        )
    att = obj.get("training_data_attestation")
    if not isinstance(att, str) or not att.strip() or len(att) > MAX_ATTESTATION_CHARS:
        raise ScreeningError(
            "invalid_manifest",
            f"training_data_attestation must be 1..{MAX_ATTESTATION_CHARS} chars",
        )
    raw_files = obj.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ScreeningError("invalid_manifest", "files must be a non-empty list")
    files: list[ManifestFile] = []
    seen: set[str] = set()
    for f in raw_files:
        if not isinstance(f, dict) or set(f) != {"path", "sha256", "bytes"}:
            raise ScreeningError("invalid_manifest", f"bad file entry {f!r}")
        path = _safe_relpath(f["path"])
        if path in seen:
            raise ScreeningError("invalid_manifest", f"duplicate path {path}")
        seen.add(path)
        if path == MANIFEST_NAME:
            raise ScreeningError("invalid_manifest", "manifest cannot list itself")
        if PurePosixPath(path).suffix not in MANIFEST_SUFFIXES:
            raise ScreeningError("forbidden_file", f"{path}: only json/safetensors")
        sha = f["sha256"]
        if not isinstance(sha, str) or not _HEX64.match(sha):
            raise ScreeningError("invalid_manifest", f"{path}: sha256 must be 64 hex")
        size = f["bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ScreeningError("invalid_manifest", f"{path}: bad bytes")
        files.append(ManifestFile(path, sha, size))
    m = Manifest(base_id, kind, tuple(files), lic, att)
    _check_kind_files(m)
    if m.total_bytes > max_total_bytes:
        raise ScreeningError(
            "too_large", f"{m.total_bytes} bytes > cap {max_total_bytes}"
        )
    return m


def _check_kind_files(m: Manifest) -> None:
    paths = set(m.paths())
    weights = {p for p in paths if p.endswith(".safetensors")}
    if m.kind == "lora":
        missing = [p for p in LORA_REQUIRED if p not in paths]
        if missing:
            raise ScreeningError("invalid_manifest", f"lora needs {missing}")
        if weights != {"adapter_model.safetensors"}:
            raise ScreeningError(
                "invalid_manifest", "lora weights must be adapter_model.safetensors"
            )
    else:
        if "config.json" not in paths:
            raise ScreeningError("invalid_manifest", "full needs config.json")
        if not weights:
            raise ScreeningError("invalid_manifest", "full needs model*.safetensors")
        bad = sorted(p for p in weights if not FULL_SHARD_RE.match(p))
        if bad:
            raise ScreeningError("invalid_manifest", f"unexpected weight files {bad}")


def scan_repo_files(paths: Iterable[str]) -> None:
    """Reject executable code and pickle formats anywhere in the repo."""
    for p in paths:
        name = PurePosixPath(p).name.lower()
        if any(name.endswith(s) for s in FORBIDDEN_SUFFIXES):
            raise ScreeningError("forbidden_file", p)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_files(root: Path, manifest: Manifest) -> None:
    """Per-file size and SHA-256 check of downloaded files."""
    for f in manifest.files:
        p = root / f.path
        if not p.is_file():
            raise ScreeningError("missing_file", f.path)
        size = p.stat().st_size
        if size != f.bytes:
            raise ScreeningError(
                "hash_mismatch", f"{f.path}: {size} != {f.bytes} bytes"
            )
        if sha256_file(p) != f.sha256:
            raise ScreeningError("hash_mismatch", f.path)


def check_config_no_remote_code(cfg: Mapping[str, Any], name: str) -> None:
    if "auto_map" in cfg or cfg.get("trust_remote_code"):
        raise ScreeningError("remote_code", f"{name} requests remote code")


# --- safetensors -----------------------------------------------------------

_DTYPE_BYTES = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E4M3": 1,
    "F8_E5M2": 1,
    "I16": 2,
    "U16": 2,
    "F16": 2,
    "BF16": 2,
    "I32": 4,
    "U32": 4,
    "F32": 4,
    "I64": 8,
    "U64": 8,
    "F64": 8,
}
MAX_HEADER_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class TensorInfo:
    name: str
    dtype: str
    shape: tuple[int, ...]
    begin: int  # absolute file offset
    end: int


def read_safetensors_header(path: Path) -> dict[str, TensorInfo]:
    """Parse and validate a safetensors header without loading tensors."""
    size = path.stat().st_size
    with path.open("rb") as f:
        raw = f.read(8)
        if len(raw) != 8:
            raise ScreeningError("bad_safetensors", f"{path.name}: truncated")
        (hlen,) = struct.unpack("<Q", raw)
        if hlen > MAX_HEADER_BYTES or 8 + hlen > size:
            raise ScreeningError("bad_safetensors", f"{path.name}: header size")
        try:
            header = json.loads(f.read(hlen))
        except ValueError as e:
            raise ScreeningError("bad_safetensors", f"{path.name}: {e}") from None
    if not isinstance(header, dict):
        raise ScreeningError("bad_safetensors", f"{path.name}: header not an object")
    base = 8 + hlen
    out: dict[str, TensorInfo] = {}
    spans: list[tuple[int, int]] = []
    for name, meta in header.items():
        if name == "__metadata__":
            continue
        try:
            dtype = meta["dtype"]
            shape = tuple(int(d) for d in meta["shape"])
            a, b = (int(x) for x in meta["data_offsets"])
        except (KeyError, TypeError, ValueError):
            raise ScreeningError("bad_safetensors", f"{path.name}: {name}") from None
        if dtype not in _DTYPE_BYTES or any(d < 0 for d in shape):
            raise ScreeningError("bad_safetensors", f"{path.name}: {name} dtype/shape")
        want = _DTYPE_BYTES[dtype] * math.prod(shape)
        if a < 0 or b - a != want or base + b > size:
            raise ScreeningError("bad_safetensors", f"{path.name}: {name} offsets")
        out[name] = TensorInfo(name, dtype, shape, base + a, base + b)
        spans.append((a, b))
    spans.sort()
    for (_, b0), (a1, _) in zip(spans, spans[1:], strict=False):
        if a1 < b0:
            raise ScreeningError("bad_safetensors", f"{path.name}: overlapping tensors")
    return out


def read_headers(paths: Sequence[Path]) -> dict[str, tuple[Path, TensorInfo]]:
    """Merge headers across shards; a tensor name may appear only once."""
    merged: dict[str, tuple[Path, TensorInfo]] = {}
    for p in paths:
        for name, info in read_safetensors_header(p).items():
            if name in merged:
                raise ScreeningError("bad_safetensors", f"duplicate tensor {name}")
            merged[name] = (p, info)
    return merged


def weights_sha256(paths: Sequence[Path]) -> str:
    """Canonical tensor-stream SHA-256 (module docstring, contract §3.3)."""
    merged = read_headers(paths)
    h = hashlib.sha256()
    handles: dict[Path, Any] = {}
    try:
        for name in sorted(merged):
            p, info = merged[name]
            nb = name.encode("utf-8")
            db = info.dtype.encode("ascii")
            h.update(struct.pack("<I", len(nb)) + nb)
            h.update(struct.pack("<I", len(db)) + db)
            h.update(struct.pack("<I", len(info.shape)))
            for d in info.shape:
                h.update(struct.pack("<Q", d))
            h.update(struct.pack("<Q", info.end - info.begin))
            f = handles.get(p)
            if f is None:
                f = handles[p] = p.open("rb")
            f.seek(info.begin)
            left = info.end - info.begin
            while left:
                chunk = f.read(min(left, 1 << 20))
                if not chunk:
                    raise ScreeningError("bad_safetensors", f"{name}: short read")
                h.update(chunk)
                left -= len(chunk)
    finally:
        for f in handles.values():
            f.close()
    return h.hexdigest()


def check_finite(paths: Sequence[Path]) -> None:
    """Reject NaN or Inf in any floating-point tensor."""
    import torch
    from safetensors import safe_open

    for p in paths:
        with safe_open(str(p), framework="pt") as f:
            for name in f.keys():  # noqa: SIM118 - safe_open is not a Mapping
                t = f.get_tensor(name)
                if t.is_floating_point() and not bool(torch.isfinite(t).all()):
                    raise ScreeningError("non_finite", f"{p.name}: {name}")


# --- LoRA and full checks ----------------------------------------------------


def check_lora(
    adapter_config: Mapping[str, Any],
    tensors: Mapping[str, TensorInfo],
    *,
    max_rank: int = MAX_LORA_RANK,
    allowed_targets: frozenset[str] = ALLOWED_LORA_TARGETS,
) -> None:
    """LoRA rank and target-module limits, checked on config and tensors."""
    check_config_no_remote_code(adapter_config, "adapter_config.json")
    if adapter_config.get("peft_type") != "LORA":
        raise ScreeningError("lora_config", "peft_type must be LORA")
    r = adapter_config.get("r")
    if not isinstance(r, int) or isinstance(r, bool) or not 1 <= r <= max_rank:
        raise ScreeningError("lora_rank", f"r={r!r} (max {max_rank})")
    rank_pattern = adapter_config.get("rank_pattern")
    if rank_pattern is None:
        rank_pattern = {}
    if not isinstance(rank_pattern, Mapping):
        raise ScreeningError("lora_rank", "rank_pattern must be a mapping")
    for k, v in rank_pattern.items():
        if not isinstance(v, int) or not 1 <= v <= max_rank:
            raise ScreeningError("lora_rank", f"rank_pattern[{k}]={v!r}")
    targets = adapter_config.get("target_modules")
    if isinstance(targets, str):
        targets = [targets]
    if not isinstance(targets, list) or not targets:
        raise ScreeningError("lora_target", "target_modules must be a list of names")
    bad = sorted(set(map(str, targets)) - allowed_targets)
    if bad:
        raise ScreeningError("lora_target", f"not allowed: {bad}")
    for key in ("modules_to_save", "trainable_token_indices", "layer_replication"):
        if adapter_config.get(key):
            raise ScreeningError("lora_config", f"{key} is not allowed")
    if adapter_config.get("use_dora"):
        raise ScreeningError("lora_config", "use_dora is not allowed in v1")
    if not tensors:
        raise ScreeningError("lora_config", "adapter has no tensors")
    for name, info in tensors.items():
        m = _LORA_KEY.match(name)
        if not m:
            raise ScreeningError("lora_target", f"unexpected tensor {name}")
        leaf = m.group("module").rsplit(".", 1)[-1]
        if leaf not in allowed_targets:
            raise ScreeningError("lora_target", f"tensor targets {leaf}")
        if len(info.shape) != 2:
            raise ScreeningError("lora_rank", f"{name}: shape {info.shape}")
        rank = info.shape[0] if m.group("ab") == "A" else info.shape[1]
        if rank > max_rank:
            raise ScreeningError("lora_rank", f"{name}: rank {rank} > {max_rank}")


def param_count(tensors: Mapping[str, TensorInfo]) -> int:
    return sum(math.prod(t.shape) for t in tensors.values())


def check_full(
    tensors: Mapping[str, TensorInfo],
    base_shapes: Mapping[str, tuple[int, ...]],
    *,
    tolerance: float = PARAM_TOLERANCE,
) -> None:
    """Full fine-tune: same tensor names and shapes as the text-only base,
    and a parameter count within ±tolerance of it."""
    base_n = sum(math.prod(s) for s in base_shapes.values())
    n = param_count(tensors)
    if base_n == 0 or abs(n - base_n) > tolerance * base_n:
        raise ScreeningError("param_count", f"{n} vs base {base_n}")
    missing = sorted(set(base_shapes) - set(tensors))
    extra = sorted(set(tensors) - set(base_shapes))
    if missing or extra:
        raise ScreeningError(
            "architecture_mismatch", f"missing {missing[:5]} extra {extra[:5]}"
        )
    for name, shape in base_shapes.items():
        if tensors[name].shape != tuple(shape):
            raise ScreeningError(
                "architecture_mismatch",
                f"{name}: {tensors[name].shape} != {tuple(shape)}",
            )


def check_full_config(cfg: Mapping[str, Any], base_cfg: Mapping[str, Any]) -> None:
    check_config_no_remote_code(cfg, "config.json")
    for key in ("model_type", "architectures"):
        if key in cfg and key in base_cfg and cfg[key] != base_cfg[key]:
            raise ScreeningError(
                "architecture_mismatch",
                f"config {key} {cfg[key]!r} != {base_cfg[key]!r}",
            )
