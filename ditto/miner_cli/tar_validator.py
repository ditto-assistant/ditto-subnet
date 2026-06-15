"""Pre-flight validation for a miner's harness tarball.

Runs every check that does not require external infrastructure. Returns
a :class:`PreflightResult` aggregating per-check pass/fail/deferred
status so both ``ditto verify`` and ``ditto upload`` consume the same
data shape.

Several checks the design spec calls for (manifest format validation,
Go import allowlist scan, sqlite schema diff against
``schema/initial_harness.sql``) depend on artifacts that have not yet
landed in this repo. Those checks ship as ``deferred=True`` stubs that
log a warning and surface in the result table; they do not gate
uploads. Once the harness interface repo lands, the stubs become real
implementations without changing the public API of this module.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import tarfile
from pathlib import Path

from ditto.miner_cli.errors import TarStructureError
from ditto.miner_cli.models import PreflightCheckResult, PreflightResult

logger = logging.getLogger(__name__)


# Must match the server constant at
# ditto/api_server/endpoints/upload.py:78. Duplicated here so the CLI
# can reject oversize tars before bothering the API; if either side
# changes, both should change.
MAX_TARBALL_SIZE_BYTES = 2 * 1024 * 1024


def run_preflight(tar_path: Path) -> PreflightResult:
    """Run every pre-flight check and aggregate results.

    Always returns a :class:`PreflightResult`; per-check failures are
    surfaced via ``PreflightCheckResult.passed=False`` rather than by
    raising. Callers (``verify``, ``upload``) decide what to do with
    the aggregate (``.passed`` boolean ignores deferred checks).

    Raises:
        TarStructureError: only when the path itself cannot be opened
            for a sha256 + size read (file missing, unreadable). Every
            other failure is reported as a non-passed check.
    """
    if not tar_path.exists():
        raise TarStructureError(f"file not found: {tar_path}")
    if not tar_path.is_file():
        raise TarStructureError(f"not a regular file: {tar_path}")

    checks: list[PreflightCheckResult] = []

    # Real checks (run today)
    size = tar_path.stat().st_size
    checks.append(_check_file_size(size))
    checks.append(_check_gzip_valid(tar_path))
    checks.append(_check_tar_opens(tar_path))

    # Deferred checks (logged, not gating, pending external artifacts)
    checks.extend(_deferred_checks())

    sha = _compute_sha256(tar_path)

    return PreflightResult(
        sha256=sha,
        file_size_bytes=size,
        checks=tuple(checks),
    )


def _check_file_size(size_bytes: int) -> PreflightCheckResult:
    if size_bytes > MAX_TARBALL_SIZE_BYTES:
        return PreflightCheckResult(
            name="file_size",
            passed=False,
            detail=(
                f"tarball is {size_bytes} bytes; "
                f"server caps at {MAX_TARBALL_SIZE_BYTES}"
            ),
        )
    return PreflightCheckResult(
        name="file_size",
        passed=True,
        detail=f"{size_bytes} bytes (cap {MAX_TARBALL_SIZE_BYTES})",
    )


def _check_gzip_valid(tar_path: Path) -> PreflightCheckResult:
    """Open via gzip and read a small chunk; bad gzip raises early."""
    try:
        with gzip.open(tar_path, "rb") as gz:
            gz.read(1024)
    except (OSError, gzip.BadGzipFile) as e:
        return PreflightCheckResult(
            name="gzip_valid",
            passed=False,
            detail=f"not a valid gzip stream: {e}",
        )
    return PreflightCheckResult(
        name="gzip_valid",
        passed=True,
        detail="gzip header parsed",
    )


def _check_tar_opens(tar_path: Path) -> PreflightCheckResult:
    """Open via tarfile and read the member list."""
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            names = tar.getnames()
    except tarfile.TarError as e:
        return PreflightCheckResult(
            name="tar_opens",
            passed=False,
            detail=f"tarfile.open failed: {e}",
        )
    return PreflightCheckResult(
        name="tar_opens",
        passed=True,
        detail=f"{len(names)} entries",
    )


def _deferred_checks() -> list[PreflightCheckResult]:
    """Stubs for validators that depend on external artifacts.

    Each emits a debug log so a verbose run shows what is not yet
    enforced; the returned ``PreflightCheckResult.deferred=True``
    flag tells the result table renderer to print these distinctly.
    """
    deferred_names = (
        ("manifest_present", "manifest spec lives in ditto-harness/interface/ (TBD)"),
        ("go_import_allowlist", "allowlist file lives in ditto-harness (TBD)"),
        ("schema_diff", "reference schema lives at schema/initial_harness.sql (TBD)"),
    )
    out: list[PreflightCheckResult] = []
    for name, hint in deferred_names:
        logger.debug(f"preflight {name} deferred: {hint}")
        out.append(
            PreflightCheckResult(
                name=name,
                passed=True,
                detail=f"deferred: {hint}",
                deferred=True,
            )
        )
    return out


def _compute_sha256(tar_path: Path) -> str:
    """Lowercase hex SHA-256 of the tarball, streamed in 1 MiB chunks."""
    h = hashlib.sha256()
    with tar_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
