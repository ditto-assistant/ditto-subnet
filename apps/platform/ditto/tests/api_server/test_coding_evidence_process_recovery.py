"""Real process-death spool drills with public synthetic bytes, never private runs."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import sys
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from ditto.api_server.coding_hosted_evidence_spool import (
    HostedEvidenceError,
    HostedEvidenceSpool,
)

# No database, provider, key service or plaintext capture is involved. The spool
# owns byte durability; the recovery service separately authenticates identities
# against real PostgreSQL reservations in test_coding_evidence_recovery.py.
CHILD = """
import os
import sys
from pathlib import Path
from uuid import UUID
from ditto.api_server import coding_hosted_evidence_spool as module

root, request, stage = Path(sys.argv[1]), UUID(sys.argv[2]), sys.argv[3]
spool = module.HostedEvidenceSpool(root, max_bytes=65536, max_objects=4)

def pause():
    os.write(1, b'ready\\n')
    # Parent owns this pipe and kills this exact child; no timer/retry loop.
    os.read(0, 1)
    raise SystemExit('unexpected resume')

original = module._write
def intercepted(directory, name, body):
    if stage == 'before-sealed' and name == 'sealed.bin':
        pause()
    original(directory, name, body)
    if stage == 'after-sealed' and name == 'sealed.bin':
        pause()
module._write = intercepted
if stage == 'before-capture':
    pause()
spool.store(request, b'{"public":"synthetic identity"}', b'synthetic sealed bytes')
pause()
"""


def reader(root: Path) -> HostedEvidenceSpool:
    return HostedEvidenceSpool(root, max_bytes=65536, max_objects=4, read_only=True)


def selected_bytes(root: Path, request: UUID) -> dict[str, bytes]:
    entry = root / request.hex
    if not entry.exists():
        return {}
    return {path.name: path.read_bytes() for path in entry.iterdir()}


@pytest.mark.parametrize(
    "stage", ["before-capture", "before-sealed", "after-sealed", "committed"]
)
def test_process_death_preserves_capture_boundary_and_releases_lock(tmp_path, stage):
    root = tmp_path / "synthetic-spool"
    root.mkdir(mode=0o700)
    request = uuid4()
    child = subprocess.Popen(
        [sys.executable, "-B", "-I", "-c", CHILD, str(root), str(request), stage],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env={"PATH": os.defpath},
    )
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(15), "synthetic child missed its checkpoint"
            assert child.stdout.readline(16) == b"ready\n"
        assert child.poll() is None
        # A separate, live process owns the original inode/lock, not a Python
        # mock or a second in-process object that forgot to release a handle.
        with pytest.raises(BlockingIOError):
            reader(root)
        lock = (root / ".lock").stat()
        before = selected_bytes(root, request)
        child.kill()
        assert child.wait(timeout=10) == -signal.SIGKILL

        after = (root / ".lock").stat()
        assert (after.st_dev, after.st_ino) == (lock.st_dev, lock.st_ino)
        assert selected_bytes(root, request) == before
        spool = reader(root)
        try:
            if stage == "before-capture":
                assert spool.load(request) is None
                assert not (root / request.hex).exists()
            elif stage in {"before-sealed", "after-sealed"}:
                with pytest.raises(FileNotFoundError):
                    spool.load(request)
                assert "identity.json" not in before
                if stage == "after-sealed":
                    assert before == {"sealed.bin": b"synthetic sealed bytes"}
            else:
                assert spool.load(request) == (
                    b'{"public":"synthetic identity"}',
                    b"synthetic sealed bytes",
                )
                assert set(before) == {"identity.json", "sealed.bin"}
                assert all(
                    (root / request.hex / name).stat().st_mode & 0o777 == 0o400
                    for name in before
                )
            with pytest.raises(HostedEvidenceError, match="read-only"):
                spool.store(uuid4(), b"{}", b"must-not-create")
        finally:
            spool.close()
        assert selected_bytes(root, request) == before
        if stage in {"before-sealed", "after-sealed"}:
            # A writer restart cannot recycle partial state into a new capture.
            with pytest.raises(HostedEvidenceError):
                HostedEvidenceSpool(root, max_bytes=65536, max_objects=4)
            assert selected_bytes(root, request) == before
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            if stream is not None:
                stream.close()
