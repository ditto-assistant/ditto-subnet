from __future__ import annotations

import stat
from pathlib import Path

import pytest

from ditto_edge_protocol.proofs import digest_of
from ditto_edge_protocol.store import DirectoryStore, MemoryStore, StoreError

DATA = b"public bytes"
DIGEST = digest_of(DATA)


@pytest.fixture(params=["memory", "directory"])
def store(
    request: pytest.FixtureRequest, tmp_path: Path
) -> MemoryStore | DirectoryStore:
    return (
        MemoryStore()
        if request.param == "memory"
        else DirectoryStore(tmp_path / "objects")
    )


def test_round_trip_and_accounting(store: MemoryStore | DirectoryStore) -> None:
    assert not store.has(DIGEST)
    store.put(DIGEST, DATA)
    assert store.has(DIGEST)
    assert store.read(DIGEST) == DATA
    assert store.digests() == {DIGEST}
    assert store.used_bytes() == len(DATA)
    assert store.delete(DIGEST)
    assert not store.delete(DIGEST)
    assert store.read(DIGEST) is None
    assert store.used_bytes() == 0


def test_refuses_bytes_under_the_wrong_name(
    store: MemoryStore | DirectoryStore,
) -> None:
    with pytest.raises(StoreError):
        store.put(DIGEST, DATA + b"!")
    with pytest.raises(StoreError):
        store.put("not-a-digest", DATA)
    assert not store.has(DIGEST)


def test_directory_store_files_are_private_and_writes_leave_no_temp(
    tmp_path: Path,
) -> None:
    store = DirectoryStore(tmp_path / "objects")
    store.put(DIGEST, DATA)
    path = tmp_path / "objects" / DIGEST[:2] / DIGEST
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == [DIGEST]


def test_directory_store_ignores_foreign_files_and_wipes(tmp_path: Path) -> None:
    store = DirectoryStore(tmp_path / "objects")
    store.put(DIGEST, DATA)
    (tmp_path / "objects" / "notes.txt").write_text("not an object")
    (tmp_path / "objects" / DIGEST[:2] / "junk").write_text("x")
    assert store.digests() == {DIGEST}
    store.wipe()
    assert store.digests() == set()


def test_reopening_sweeps_only_stale_fill_temporaries(tmp_path: Path) -> None:
    import os
    import time

    root = tmp_path / "objects"
    store = DirectoryStore(root)
    data = b"public bytes"
    digest = digest_of(data)
    store.put(digest, data)
    shard = store.root / digest[:2]
    crashed = shard / ".fill-crashed"
    in_flight = shard / ".fill-in-flight"
    crashed.write_bytes(b"x" * 10)
    in_flight.write_bytes(b"y" * 10)
    old = time.time() - 2 * 3600
    os.utime(crashed, (old, old))
    reopened = DirectoryStore(root)
    assert not crashed.exists()
    assert in_flight.exists()
    assert reopened.read(digest) == data
