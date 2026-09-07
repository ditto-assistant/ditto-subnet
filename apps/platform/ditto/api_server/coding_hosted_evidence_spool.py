"""Bounded owner-only native ciphertext spool. No plaintext or garbage collector."""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import stat
from pathlib import Path
from uuid import UUID

from ditto.api_models.coding_hosted_evidence import MAX_PLAINTEXT


class HostedEvidenceError(RuntimeError):
    """Safe evidence failure without private paths, payloads or credentials."""


def _root(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts:
        raise HostedEvidenceError("evidence root must be absolute")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
            try:
                os.stat(".git", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise HostedEvidenceError("evidence cannot be stored in a Git tree")
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise HostedEvidenceError("evidence root is not private")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(directory: int, name: str, maximum: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o400
            or info.st_nlink != 1
            or not 0 < info.st_size <= maximum
        ):
            raise HostedEvidenceError("evidence file is unsafe")
        body = bytearray()
        while len(body) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(body)))
            if not chunk:
                break
            body.extend(chunk)
        if len(body) != info.st_size or len(body) > maximum:
            raise HostedEvidenceError("evidence file size drifted")
        return bytes(body)
    finally:
        os.close(fd)


def _write(directory: int, name: str, body: bytes) -> None:
    fd = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        view = memoryview(body)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise HostedEvidenceError("evidence write made no progress")
            view = view[count:]
        os.fchmod(fd, 0o400)
        os.fsync(fd)
    finally:
        os.close(fd)


class HostedEvidenceSpool:
    def __init__(
        self, root: Path, *, max_bytes: int, max_objects: int, read_only: bool = False
    ):
        if (
            type(read_only) is not bool
            or type(max_bytes) is not int
            or not 16384 <= max_bytes <= 1 << 40
            or type(max_objects) is not int
            or not 1 <= max_objects <= 65536
        ):
            raise HostedEvidenceError("evidence capacity is invalid")
        self._path = root
        self._read_only = read_only
        self._root = _root(root)
        self._lock = -1
        self._closed = False
        self._broken = False
        self.lock = asyncio.Lock()
        self._max_bytes, self._max_objects = max_bytes, max_objects
        try:
            self._lock = os.open(
                ".lock",
                os.O_RDWR | os.O_NOFOLLOW | (0 if read_only else os.O_CREAT),
                0o600,
                dir_fd=self._root,
            )
            info = os.fstat(self._lock)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise HostedEvidenceError("evidence process lock is unsafe")
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not read_only:
                self._usage()
        except BaseException:
            self.close()
            raise

    def _check(self) -> None:
        if self._closed or self._broken:
            raise HostedEvidenceError("evidence spool is closed or incomplete")
        observed = _root(self._path)
        try:
            before, now = os.fstat(self._root), os.fstat(observed)
            if (before.st_dev, before.st_ino) != (now.st_dev, now.st_ino):
                raise HostedEvidenceError("evidence root changed")
            held = os.fstat(self._lock)
            linked = os.stat(".lock", dir_fd=self._root, follow_symlinks=False)
            if (held.st_dev, held.st_ino) != (
                linked.st_dev,
                linked.st_ino,
            ) or linked.st_nlink != 1:
                raise HostedEvidenceError("evidence process lock changed")
        finally:
            os.close(observed)

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def bounds(self) -> tuple[int, int]:
        return self._max_bytes, self._max_objects

    def _directory(self, request_id: UUID) -> int:
        fd = os.open(
            request_id.hex,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=self._root,
        )
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(fd)
            raise HostedEvidenceError("evidence entry is unsafe")
        return fd

    def load(self, request_id: UUID) -> tuple[bytes, bytes] | None:
        self._check()
        try:
            directory = self._directory(request_id)
        except FileNotFoundError:
            return None
        try:
            return _read(directory, "identity.json", 16384), _read(
                directory, "sealed.bin", MAX_PLAINTEXT + 2048
            )
        finally:
            os.close(directory)

    def _usage(self) -> tuple[int, int]:
        self._check()
        count, size = 0, 0
        with os.scandir(self._root) as entries:
            for entry in entries:
                if entry.name == ".lock":
                    continue
                count += 1
                if (
                    count > self._max_objects
                    or re.fullmatch(r"[0-9a-f]{32}", entry.name) is None
                ):
                    raise HostedEvidenceError(
                        "evidence object capacity or layout invalid"
                    )
                directory = self._directory(UUID(hex=entry.name))
                try:
                    names = set()
                    with os.scandir(directory) as children:
                        for child in children:
                            names.add(child.name)
                            if len(names) > 2:
                                raise HostedEvidenceError("unexpected evidence files")
                    if names != {"identity.json", "sealed.bin"}:
                        raise HostedEvidenceError("incomplete evidence entry")
                    for name, maximum in (
                        ("identity.json", 16384),
                        ("sealed.bin", MAX_PLAINTEXT + 2048),
                    ):
                        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                        if (
                            not stat.S_ISREG(info.st_mode)
                            or info.st_uid != os.geteuid()
                            or stat.S_IMODE(info.st_mode) != 0o400
                            or info.st_nlink != 1
                            or not 0 < info.st_size <= maximum
                        ):
                            raise HostedEvidenceError("unsafe evidence capacity record")
                        size += info.st_size
                finally:
                    os.close(directory)
                if size > self._max_bytes:
                    raise HostedEvidenceError("evidence byte capacity exceeded")
        return count, size

    def store(self, request_id: UUID, identity: bytes, sealed: bytes) -> None:
        self._check()
        if self.read_only:
            raise HostedEvidenceError("recovery spool is read-only")
        count, size = self._usage()
        if (
            count >= self._max_objects
            or size + len(identity) + len(sealed) > self._max_bytes
        ):
            raise HostedEvidenceError("evidence spool capacity exhausted")
        if (
            not 0 < len(identity) <= 16384
            or not 0 < len(sealed) <= MAX_PLAINTEXT + 2048
        ):
            raise HostedEvidenceError("evidence object exceeds bound")
        try:
            os.mkdir(request_id.hex, mode=0o700, dir_fd=self._root)
            os.fsync(self._root)
            directory = self._directory(request_id)
            try:
                _write(directory, "sealed.bin", sealed)
                os.fsync(directory)
                _write(directory, "identity.json", identity)  # Commit marker last.
                os.fsync(directory)
            finally:
                os.close(directory)
            os.fsync(self._root)
            self._check()
        except BaseException:
            self._broken = True  # Preserve partial bytes; no automatic rebuild.
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._lock >= 0:
            os.close(self._lock)
        os.close(self._root)

    def __repr__(self) -> str:
        return "HostedEvidenceSpool(private=True)"
