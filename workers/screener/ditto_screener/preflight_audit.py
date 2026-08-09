"""Private bounded audit journal for static-preflight v1/v2 comparisons."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import threading
from collections.abc import Mapping
from pathlib import Path
from uuid import UUID

_MAX_RECORD_BYTES = 32 * 1024
_LOCAL_APPEND_LOCK = threading.Lock()


class StaticPreflightAuditError(RuntimeError):
    """The private rollout journal could not durably accept one record."""


class StaticPreflightAuditJournal:
    def __init__(self, path: str | None) -> None:
        self._path = Path(path) if path else None

    def record(
        self,
        *,
        agent_id: UUID,
        attempt_id: UUID,
        artifact_sha256: str,
        payload: Mapping[str, object],
    ) -> None:
        if self._path is None:
            return
        record = {
            **payload,
            "agent_id": str(agent_id),
            "attempt_id": str(attempt_id),
            "artifact_sha256": artifact_sha256,
        }
        encoded = (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        if len(encoded) > _MAX_RECORD_BYTES:
            raise StaticPreflightAuditError(
                "static preflight audit record exceeds safety bound"
            )
        try:
            # ``flock`` coordinates worker processes. The local lock also
            # serializes same-process threads during first-file creation on
            # platforms where concurrent O_CREAT + O_NOFOLLOW is racy.
            with _LOCAL_APPEND_LOCK:
                self._append(encoded)
        except OSError as error:
            raise StaticPreflightAuditError(
                f"static preflight audit append failed: {error}"
            ) from error

    def _append(self, encoded: bytes) -> None:
        assert self._path is not None
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_flags = os.O_RDONLY
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        directory_flags |= getattr(os, "O_CLOEXEC", 0)
        parent_fd = os.open(parent, directory_flags)
        try:
            if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
                raise OSError("static preflight audit parent is not a directory")
            os.fchmod(parent_fd, 0o700)
            file_flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
            file_flags |= getattr(os, "O_NOFOLLOW", 0)
            file_flags |= getattr(os, "O_CLOEXEC", 0)
            fd = os.open(self._path.name, file_flags, 0o600, dir_fd=parent_fd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError("static preflight audit target is not a regular file")
                os.fchmod(fd, 0o600)
                # JSONL records exceed PIPE_BUF on some hosts, so O_APPEND alone
                # cannot prevent interleaving across concurrent worker processes.
                fcntl.flock(fd, fcntl.LOCK_EX)
                view = memoryview(encoded)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("static preflight audit append made no progress")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)


__all__ = ["StaticPreflightAuditError", "StaticPreflightAuditJournal"]
