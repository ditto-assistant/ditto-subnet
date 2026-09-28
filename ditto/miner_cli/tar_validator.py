"""Pre-flight validation for a miner's harness tarball.

Runs every check that does not require external infrastructure. Returns
a :class:`PreflightResult` aggregating per-check pass/fail/deferred
status so both ``ditto verify`` and ``ditto upload`` consume the same
data shape.

Several checks the design spec calls for (manifest format validation,
dependency allowlist scan, schema diff against the reference harness
schema) depend on artifacts the harness team still owns. Those checks
ship as ``deferred=True`` stubs that log a warning and surface in the
result table; they do not gate uploads. Once the harness team locks
the submission spec, the stubs become real implementations without
changing the public API of this module.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import tarfile
from pathlib import Path, PurePosixPath

from ditto.miner_cli.errors import TarStructureError
from ditto.miner_cli.models import PreflightCheckResult, PreflightResult

logger = logging.getLogger(__name__)


# Must match the platform's upload cap
# (apps/platform/ditto/api_server/endpoints/upload.py:DEFAULT_MAX_TARBALL_SIZE_BYTES,
# env-overridable there via DITTO_MAX_TARBALL_SIZE_BYTES). Duplicated here so
# the CLI can reject oversize tars before bothering the API; if either side
# changes, both must change. A larger local value makes `ditto verify` pass a
# tarball the server then rejects at /upload/check.
MAX_TARBALL_SIZE_BYTES = 20 * 1024 * 1024

# Must match the screener's archive contract (workers/screener/ditto_screener/
# gate.py: _MAX_ARCHIVE_MEMBERS, _MAX_UNPACKED_BYTES, _contract_error). The
# screener rejects a violating archive only after the upload fee is paid, so
# checking the same rules here lets `ditto verify` and the upload preflight
# fail first. Never make these stricter than the screener: a stricter local
# rule would block archives the screener accepts.
# ditto/tests/miner_cli/test_archive_contract_pins.py keeps the limits aligned.
MAX_ARCHIVE_MEMBERS = 20_000
MAX_UNPACKED_BYTES = 64 * 1024 * 1024


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
    tar_opens = _check_tar_opens(tar_path)
    checks.append(tar_opens)
    if tar_opens.passed:
        checks.append(_check_archive_contract(tar_path))

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


def _contract_failure(code: str, problem: str, fix: str) -> PreflightCheckResult:
    return PreflightCheckResult(
        name="archive_contract",
        passed=False,
        detail=f"{code}: {problem}; {fix}",
    )


def _check_archive_contract(tar_path: Path) -> PreflightCheckResult:
    """Apply the screener's archive and root-Dockerfile contract locally."""
    try:
        with tarfile.open(tar_path, "r:gz") as tar:
            members: dict[str, tarfile.TarInfo] = {}
            unpacked = 0
            for member_count, member in enumerate(tar, start=1):
                if member_count > MAX_ARCHIVE_MEMBERS:
                    return _contract_failure(
                        "SCR-ARCHIVE-005",
                        "archive contains too many members",
                        "remove generated directories and package only the harness",
                    )
                name = member.name.removeprefix("./")
                if not name and member.isdir():
                    continue
                path = PurePosixPath(name)
                if (
                    not name
                    or name.startswith("/")
                    or "\\" in name
                    or (path.parts and path.parts[0].endswith(":"))
                    or ".." in path.parts
                ):
                    return _contract_failure(
                        "SCR-ARCHIVE-001",
                        f"archive contains an unsafe path ({member.name!r})",
                        "remove absolute paths, parent traversals, backslashes, "
                        "and drive-prefixed entries",
                    )
                if str(path) != name:
                    return _contract_failure(
                        "SCR-ARCHIVE-001",
                        f"archive contains a non-canonical path ({member.name!r})",
                        "remove redundant path separators and dot components",
                    )
                if name in members:
                    return _contract_failure(
                        "SCR-ARCHIVE-002",
                        f"archive contains a duplicate path ({name!r})",
                        "package each path exactly once",
                    )
                if not (member.isfile() or member.isdir()):
                    return _contract_failure(
                        "SCR-ARCHIVE-003",
                        f"archive contains a link or special file ({name!r})",
                        "package only regular files and directories",
                    )
                unpacked += member.size
                if unpacked > MAX_UNPACKED_BYTES:
                    return _contract_failure(
                        "SCR-ARCHIVE-004",
                        "archive expands beyond the safety limit",
                        "remove generated assets and build output before packaging",
                    )
                members[name] = member
            dockerfile = members.get("Dockerfile")
            if dockerfile is None or not dockerfile.isfile():
                return _contract_failure(
                    "SCR-CONTRACT-001",
                    "Dockerfile is missing from the archive root",
                    "package the harness contents so Dockerfile is at the top level",
                )
            handle = tar.extractfile(dockerfile)
            if handle is None:
                return _contract_failure(
                    "SCR-CONTRACT-002",
                    "Dockerfile could not be read",
                    "recreate the archive from readable regular files",
                )
            try:
                handle.read().decode("utf-8")
            except UnicodeDecodeError:
                return _contract_failure(
                    "SCR-CONTRACT-003",
                    "Dockerfile is not valid UTF-8 text",
                    "commit a readable UTF-8 Dockerfile that builds the harness",
                )
    except (tarfile.TarError, OSError):
        return _contract_failure(
            "SCR-ARCHIVE-006",
            "archive is not a readable gzip-compressed tar",
            "recreate it as a .tar.gz archive and retry",
        )
    return PreflightCheckResult(
        name="archive_contract",
        passed=True,
        detail=f"{len(members)} paths; root Dockerfile present",
    )


def _deferred_checks() -> list[PreflightCheckResult]:
    """Stubs for validators that depend on external artifacts.

    Each emits a debug log so a verbose run shows what is not yet
    enforced; the returned ``PreflightCheckResult.deferred=True``
    flag tells the result table renderer to print these distinctly.
    """
    deferred_names = (
        ("manifest_present", "manifest spec owned by the harness team (TBD)"),
        (
            "dependency_allowlist",
            "approved-dependency list owned by the harness team (TBD)",
        ),
        ("schema_diff", "reference harness schema owned by the harness team (TBD)"),
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
