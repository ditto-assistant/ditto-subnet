"""Where a node keeps the objects it was assigned.

Objects are stored by digest and re-verified on write, so a store never holds
bytes under the wrong name. The directory store writes atomically, keeps files
0600 in a 0700 tree, and touches nothing outside its root.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from pathlib import Path
from typing import Protocol

from ditto_edge_protocol.proofs import digest_of

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class StoreError(ValueError):
    """Bytes did not match their digest, or the digest is malformed."""


class ObjectStore(Protocol):
    def has(self, digest: str) -> bool: ...
    def read(self, digest: str) -> bytes | None: ...
    def put(self, digest: str, data: bytes) -> None: ...
    def delete(self, digest: str) -> bool: ...
    def digests(self) -> set[str]: ...
    def used_bytes(self) -> int: ...


def _check(digest: str, data: bytes) -> None:
    if _DIGEST.fullmatch(digest) is None:
        raise StoreError("malformed digest")
    if digest_of(data) != digest:
        raise StoreError("bytes do not match their digest")


class MemoryStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def has(self, digest: str) -> bool:
        return digest in self._objects

    def read(self, digest: str) -> bytes | None:
        return self._objects.get(digest)

    def put(self, digest: str, data: bytes) -> None:
        _check(digest, data)
        self._objects[digest] = data

    def delete(self, digest: str) -> bool:
        return self._objects.pop(digest, None) is not None

    def digests(self) -> set[str]:
        return set(self._objects)

    def used_bytes(self) -> int:
        return sum(len(data) for data in self._objects.values())


_TEMP_PREFIX = ".fill-"
STALE_TEMP_SECONDS = 3600.0
"""A fill writes for seconds. A temporary this old lost its writer to a crash."""


class DirectoryStore:
    """Objects at ``root/<first two hex>/<digest>``."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root = root.resolve()
        self.sweep_stale_temporaries()

    def sweep_stale_temporaries(self, *, older_than: float = STALE_TEMP_SECONDS) -> int:
        """Delete fill temporaries a crash left behind; return how many.

        Only old ones go: a young temporary may belong to a write in flight.
        """
        cutoff = time.time() - older_than
        removed = 0
        for shard in self.root.iterdir():
            if not shard.is_dir() or len(shard.name) != 2:
                continue
            for entry in shard.iterdir():
                if not entry.name.startswith(_TEMP_PREFIX):
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                        removed += 1
                except FileNotFoundError:
                    continue
        return removed

    def _path(self, digest: str) -> Path:
        if _DIGEST.fullmatch(digest) is None:
            raise StoreError("malformed digest")
        return self.root / digest[:2] / digest

    def has(self, digest: str) -> bool:
        return self._path(digest).is_file()

    def read(self, digest: str) -> bytes | None:
        try:
            return self._path(digest).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, digest: str, data: bytes) -> None:
        _check(digest, data)
        target = self._path(digest)
        target.parent.mkdir(exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(dir=target.parent, prefix=_TEMP_PREFIX)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp, 0o600)
            os.replace(temp, target)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def delete(self, digest: str) -> bool:
        try:
            self._path(digest).unlink()
        except FileNotFoundError:
            return False
        return True

    def digests(self) -> set[str]:
        found: set[str] = set()
        for shard in self.root.iterdir():
            if not shard.is_dir() or len(shard.name) != 2:
                continue
            for entry in shard.iterdir():
                if _DIGEST.fullmatch(entry.name) and entry.name[:2] == shard.name:
                    found.add(entry.name)
        return found

    def used_bytes(self) -> int:
        return sum(self._path(digest).stat().st_size for digest in self.digests())

    def wipe(self) -> None:
        """Delete every stored object (leaving the network)."""
        for digest in self.digests():
            self.delete(digest)
        self.sweep_stale_temporaries()
