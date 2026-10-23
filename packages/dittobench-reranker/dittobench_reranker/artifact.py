"""Structural validation of a reranker submission (SN-206).

A submission is a directory, or a tarball of one, holding ``model.onnx``,
``vocab.txt`` and ``manifest.json``. Everything checked here is static: no
model is executed. The checks reject anything the product could not run, plus
anything that would make the score depend on files outside the artifact.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from dittobench_reranker.contract import (
    ALLOWED_BASE_MODEL_LICENSES,
    ALLOWED_OPSET_DOMAINS,
    CONTRACT,
    CONTRACT_ID,
    MODEL_INPUT_NAMES,
    MODEL_OUTPUT_NAME,
    SPECIAL_TOKEN_IDS,
    SeasonContract,
)

MODEL_FILE = "model.onnx"
VOCAB_FILE = "vocab.txt"
MANIFEST_FILE = "manifest.json"
REQUIRED_FILES = (MODEL_FILE, VOCAB_FILE, MANIFEST_FILE)

# ONNX TensorProto element types.
_INT64 = 7
_FLOAT = 1


class ArtifactError(ValueError):
    """The artifact violates the season contract."""


@dataclass(frozen=True)
class Artifact:
    model: bytes
    vocab: bytes
    manifest: dict
    # sha256 over the three files in a fixed order: the identity a submission
    # commits to and duplicate detection keys on.
    sha256: str
    warnings: tuple[str, ...] = field(default=())


def artifact_digest(model: bytes, vocab: bytes, manifest: bytes) -> str:
    h = hashlib.sha256()
    for name, data in (
        (MODEL_FILE, model),
        (VOCAB_FILE, vocab),
        (MANIFEST_FILE, manifest),
    ):
        h.update(name.encode())
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def read_files(
    path: str | Path, contract: SeasonContract = CONTRACT
) -> dict[str, bytes]:
    """Read the three artifact files from a directory or a tarball."""
    p = Path(path)
    limits = contract.artifact
    caps = {
        MODEL_FILE: limits.max_model_bytes,
        VOCAB_FILE: limits.max_vocab_bytes,
        MANIFEST_FILE: limits.max_manifest_bytes,
    }
    files: dict[str, bytes] = {}
    if p.is_dir():
        extra = sorted(x.name for x in p.iterdir() if x.name not in REQUIRED_FILES)
        if extra:
            raise ArtifactError(f"unexpected files in artifact: {extra}")
        for name in REQUIRED_FILES:
            f = p / name
            if not f.is_file():
                raise ArtifactError(f"missing {name}")
            if f.stat().st_size > caps[name]:
                raise ArtifactError(f"{name} exceeds {caps[name]} bytes")
            files[name] = f.read_bytes()
        return files
    if not p.is_file():
        raise ArtifactError(f"no artifact at {p}")
    with tarfile.open(p, "r:*") as tar:
        for member in tar.getmembers():
            name = PurePosixPath(member.name)
            if member.isdir() and name.name in ("", "."):
                continue
            if not member.isfile():
                raise ArtifactError(
                    f"tarball may only hold regular files: {member.name}"
                )
            base = name.name
            if base not in REQUIRED_FILES or len(name.parts) > 2 or ".." in name.parts:
                raise ArtifactError(f"unexpected tarball entry: {member.name}")
            if base in files:
                raise ArtifactError(f"duplicate tarball entry: {base}")
            if member.size > caps[base]:
                raise ArtifactError(f"{base} exceeds {caps[base]} bytes")
            handle = tar.extractfile(member)
            assert handle is not None
            files[base] = handle.read()
    missing = [n for n in REQUIRED_FILES if n not in files]
    if missing:
        raise ArtifactError(f"missing {missing}")
    return files


def validate_manifest(raw: bytes) -> dict:
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"manifest.json is not JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ArtifactError("manifest.json must be an object")
    if manifest.get("contract") != CONTRACT_ID:
        raise ArtifactError(f"manifest contract must be {CONTRACT_ID!r}")
    base = manifest.get("base_model")
    if not isinstance(base, str) or not base.strip():
        raise ArtifactError(
            "manifest base_model is required (use 'none' for from-scratch)"
        )
    license_id = manifest.get("base_model_license")
    if (
        not isinstance(license_id, str)
        or license_id.lower() not in ALLOWED_BASE_MODEL_LICENSES
    ):
        raise ArtifactError(
            f"base_model_license must be one of {sorted(ALLOWED_BASE_MODEL_LICENSES)}"
        )
    return manifest


def validate_vocab(raw: bytes, contract: SeasonContract = CONTRACT) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError("vocab.txt must be UTF-8") from exc
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) > contract.artifact.max_vocab_tokens:
        raise ArtifactError("vocab.txt has too many tokens")
    tokens = [line.rstrip("\r") for line in lines]
    for token, want in SPECIAL_TOKEN_IDS.items():
        if want >= len(tokens) or tokens[want] != token:
            raise ArtifactError(f"vocab.txt must put {token} at id {want}")
        # A later duplicate would win in the product's map and move the id.
        if tokens.count(token) != 1:
            raise ArtifactError(f"vocab.txt repeats {token}")


def validate_model(raw: bytes) -> list[str]:
    """Static ONNX checks. Returns warnings; raises on violations."""
    import onnx  # imported lazily: contract-only consumers need no onnx

    try:
        model = onnx.load_model_from_string(raw)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a rejection
        raise ArtifactError(f"model.onnx does not parse: {exc}") from exc
    try:
        onnx.checker.check_model(model, full_check=False)
    except Exception as exc:  # noqa: BLE001
        raise ArtifactError(f"model.onnx fails the ONNX checker: {exc}") from exc

    graph = model.graph
    domains = {op.domain for op in model.opset_import}
    domains |= {node.domain for node in graph.node}
    bad = sorted(d for d in domains if d not in ALLOWED_OPSET_DOMAINS)
    if bad:
        raise ArtifactError(f"operators from unsupported domains: {bad}")
    if model.functions:
        raise ArtifactError("model-local functions are not allowed")
    for tensor in graph.initializer:
        if tensor.data_location == onnx.TensorProto.EXTERNAL:
            raise ArtifactError("external tensor data is not allowed")

    initializer_names = {t.name for t in graph.initializer}
    inputs = [i for i in graph.input if i.name not in initializer_names]
    names = tuple(i.name for i in inputs)
    if sorted(names) != sorted(MODEL_INPUT_NAMES):
        raise ArtifactError(
            f"model inputs must be {list(MODEL_INPUT_NAMES)}, got {list(names)}"
        )
    for i in inputs:
        tt = i.type.tensor_type
        if tt.elem_type != _INT64:
            raise ArtifactError(f"input {i.name} must be int64")
        if len(tt.shape.dim) != 2:
            raise ArtifactError(f"input {i.name} must be rank 2 [batch, seq]")

    outputs = [o.name for o in graph.output]
    if MODEL_OUTPUT_NAME not in outputs:
        raise ArtifactError(f"model must have an output named {MODEL_OUTPUT_NAME!r}")
    out = next(o for o in graph.output if o.name == MODEL_OUTPUT_NAME)
    tt = out.type.tensor_type
    if tt.elem_type != _FLOAT:
        raise ArtifactError("logits must be float32")
    dims = tt.shape.dim
    if len(dims) != 2:
        raise ArtifactError("logits must be rank 2 [batch, 1]")
    if dims[1].HasField("dim_value") and dims[1].dim_value != 1:
        raise ArtifactError("logits must have exactly one column")
    warnings = []
    if len(outputs) > 1:
        warnings.append(
            f"extra outputs ignored: {[o for o in outputs if o != MODEL_OUTPUT_NAME]}"
        )
    return warnings


def load_artifact(path: str | Path, contract: SeasonContract = CONTRACT) -> Artifact:
    """Read and statically validate an artifact. Raises ArtifactError."""
    files = read_files(path, contract)
    manifest = validate_manifest(files[MANIFEST_FILE])
    validate_vocab(files[VOCAB_FILE], contract)
    warnings = validate_model(files[MODEL_FILE])
    return Artifact(
        model=files[MODEL_FILE],
        vocab=files[VOCAB_FILE],
        manifest=manifest,
        sha256=artifact_digest(
            files[MODEL_FILE], files[VOCAB_FILE], files[MANIFEST_FILE]
        ),
        warnings=tuple(warnings),
    )


def pack(directory: str | Path) -> bytes:
    """Deterministic tarball of an artifact directory (for upload)."""
    d = Path(directory)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in REQUIRED_FILES:
            data = (d / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue(), mtime=0)
