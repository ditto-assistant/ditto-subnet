"""Local preparation of a Season 2 reranker artifact for upload (SN-207).

Runs the same static checks Platform runs, so an artifact the server would
reject fails here without a request. The ``model.onnx`` check needs the
``onnx`` package; the base miner install does not carry it, so without it the
model is checked only by Platform (the starter kit's environment has it).
"""

from __future__ import annotations

import hashlib
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from ditto.miner_cli.errors import RerankerArtifactError

# Mirrors MAX_RERANKER_UPLOAD_BYTES in
# apps/platform/ditto/api_server/endpoints/reranker_upload.py.
_TAR_OVERHEAD_BYTES = 1024 * 1024


@dataclass(frozen=True)
class PreparedRerankerArtifact:
    """The exact bytes to upload plus what was checked locally."""

    data: bytes
    sha256: str
    """Digest of ``data`` (what the hotkey signs)."""
    artifact_sha256: str
    """Season digest over the three files (duplicate detection keys on it)."""
    model_checked: bool
    """False when ``onnx`` is not installed and only Platform checks the model."""
    warnings: tuple[str, ...]


def max_upload_bytes() -> int:
    from dittobench_reranker.contract import CONTRACT

    limits = CONTRACT.artifact
    return (
        limits.max_model_bytes
        + limits.max_vocab_bytes
        + limits.max_manifest_bytes
        + _TAR_OVERHEAD_BYTES
    )


def _onnx_available() -> bool:
    try:
        import onnx  # noqa: F401
    except ImportError:
        return False
    return True


def prepare_reranker_artifact(path: Path) -> PreparedRerankerArtifact:
    """Validate an artifact directory or tarball and return upload bytes.

    A directory is packed with the deterministic ``pack()`` (fixed order,
    zero mtimes), so the same files always upload as the same bytes. A
    tarball is uploaded as is.

    Raises:
        RerankerArtifactError: the artifact violates the season contract or
            cannot be read.
    """
    from dittobench_reranker.artifact import (
        MANIFEST_FILE,
        MODEL_FILE,
        VOCAB_FILE,
        ArtifactError,
        artifact_digest,
        pack,
        read_files,
        validate_manifest,
        validate_model,
        validate_vocab,
    )

    try:
        files = read_files(path)
        validate_manifest(files[MANIFEST_FILE])
        validate_vocab(files[VOCAB_FILE])
        model_checked = _onnx_available()
        warnings = validate_model(files[MODEL_FILE]) if model_checked else []
        data = pack(path) if path.is_dir() else path.read_bytes()
    except ArtifactError as exc:
        raise RerankerArtifactError(f"artifact rejected: {exc}") from exc
    except (tarfile.TarError, EOFError, zlib.error, OSError) as exc:
        raise RerankerArtifactError(
            f"cannot read {path} as an artifact directory or tar/tar.gz: {exc}"
        ) from exc

    cap = max_upload_bytes()
    if len(data) > cap:
        raise RerankerArtifactError(f"artifact upload exceeds {cap} bytes")
    return PreparedRerankerArtifact(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        artifact_sha256=artifact_digest(
            files[MODEL_FILE], files[VOCAB_FILE], files[MANIFEST_FILE]
        ),
        model_checked=model_checked,
        warnings=tuple(warnings),
    )
