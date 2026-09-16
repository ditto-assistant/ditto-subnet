from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ditto.validator import coding_certification_socket
from ditto.validator.coding_canary_runtime import CodingCanaryRuntime
from ditto.validator.coding_certification_socket import (
    CERTIFICATION_ORIGIN,
    CERTIFICATION_SOCKET_PATH,
    CertificationSocketError,
    CertificationSocketIdentity,
    CertificationSocketTransport,
    _safe_ancestor,
    _VerifiedUnixBackend,
    verify_certification_socket,
)

pytestmark = pytest.mark.skipif(
    os.geteuid() == 0, reason="ownership pins are meaningless for root"
)

_UID = os.geteuid()
_GID = os.getegid()
_READY_VECTOR = json.loads(
    (
        Path(__file__).parents[3]
        / "packages"
        / "dittobench-coding-contract"
        / "testdata"
        / "coding_certification_canary_readiness_v2.json"
    ).read_text(encoding="utf-8")
)["ready"]


@dataclass
class _Server:
    directory: Path
    path: Path
    received: list[bytes] = field(default_factory=list)
    connections: int = 0

    def identity(self, **updates: object) -> CertificationSocketIdentity:
        values: dict[str, object] = {"uid": _UID, "gid": _GID, "path": str(self.path)}
        values.update(updates)
        return CertificationSocketIdentity(**values)  # type: ignore[arg-type]


@pytest.fixture
def short_root() -> Iterator[Path]:
    # A short private root keeps socket paths under sun_path's limit and passes
    # the ancestor check (/tmp is root-owned and sticky).
    root = Path(tempfile.mkdtemp(prefix="ccs", dir="/tmp"))
    root.chmod(0o700)
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@asynccontextmanager
async def _serve(root: Path, body: bytes = b'{"ok":true}') -> AsyncIterator[_Server]:
    directory = root / "control"
    directory.mkdir(mode=0o700)
    directory.chmod(0o750)
    path = directory / "control.sock"
    server_state = _Server(directory=directory, path=path)

    async def handle(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        server_state.connections += 1
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=2)
                if not chunk:
                    break
                data += chunk
        except TimeoutError:
            pass
        server_state.received.append(data)
        if data:
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Cache-Control: no-store\r\nConnection: close\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(path))
    path.chmod(0o660)
    try:
        yield server_state
    finally:
        server.close()
        await server.wait_closed()


async def _get(transport: httpx.AsyncBaseTransport) -> httpx.Response:
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        return await client.get(
            f"{CERTIFICATION_ORIGIN}/v1/coding/certifier/canary/readiness", timeout=5
        )


async def test_verified_socket_serves_a_request(short_root: Path) -> None:
    async with _serve(short_root) as server:
        response = await _get(CertificationSocketTransport(server.identity()))
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        # A second request opens a second connection, which is re-verified.
        await _get(CertificationSocketTransport(server.identity()))
    assert server.connections == 2
    assert all(data.startswith(b"GET /v1/coding/") for data in server.received)


def _chmod(relative: str, mode: int) -> Callable[[_Server], None]:
    def mutate(server: _Server) -> None:
        target = server.path if relative == "socket" else server.directory
        if relative == "ancestor":
            target = server.directory.parent
        target.chmod(mode)

    return mutate


def _symlink_socket(server: _Server) -> None:
    moved = server.directory / "moved.sock"
    server.path.rename(moved)
    server.path.symlink_to(moved)


def _symlink_directory(server: _Server) -> None:
    moved = server.directory.parent / "real"
    server.directory.rename(moved)
    server.directory.symlink_to(moved)


def _regular_file(server: _Server) -> None:
    server.path.unlink()
    server.path.write_bytes(b"")
    server.path.chmod(0o660)


def _missing(server: _Server) -> None:
    server.path.unlink()


@pytest.mark.parametrize(
    ("name", "mutate", "identity"),
    [
        ("socket mode relaxed", _chmod("socket", 0o666), {}),
        ("socket mode narrowed", _chmod("socket", 0o600), {}),
        ("directory traversable by others", _chmod("directory", 0o755), {}),
        ("directory writable by group", _chmod("directory", 0o770), {}),
        ("ancestor writable by group", _chmod("ancestor", 0o770), {}),
        ("socket is a symlink", _symlink_socket, {}),
        ("socket directory is a symlink", _symlink_directory, {}),
        ("regular file at the socket path", _regular_file, {}),
        ("socket missing", _missing, {}),
        ("another pinned owner", None, {"uid": _UID + 1}),
        ("another pinned group", None, {"gid": _GID + 1}),
    ],
)
async def test_unverified_socket_is_refused_before_any_byte(
    short_root: Path,
    name: str,
    mutate: Callable[[_Server], None] | None,
    identity: dict[str, object],
) -> None:
    del name
    async with _serve(short_root) as server:
        pinned = server.identity(**identity)
        if mutate is not None:
            mutate(server)
        with pytest.raises(CertificationSocketError):
            verify_certification_socket(pinned)
        with pytest.raises(httpx.ConnectError, match="socket refused"):
            await _get(CertificationSocketTransport(pinned))
        await asyncio.sleep(0.05)
    assert all(data == b"" for data in server.received)


async def test_peer_running_as_another_user_is_refused_before_any_byte(
    short_root: Path,
) -> None:
    async with _serve(short_root) as server:
        backend = _VerifiedUnixBackend(server.identity(), peer=lambda _: _UID + 1)
        transport = CertificationSocketTransport(server.identity(), backend=backend)
        with pytest.raises(httpx.ConnectError, match="socket refused"):
            await _get(transport)
        await asyncio.sleep(0.05)
    assert server.received == [b""]


async def test_socket_swapped_during_connect_is_refused_before_any_byte(
    short_root: Path,
) -> None:
    async with _serve(short_root) as server:
        other_path = server.directory / "other.sock"
        other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        other.bind(str(other_path))
        other.listen()
        other_path.chmod(0o660)

        def swap_then_report_owner(_connection: socket.socket) -> int:
            # Between the pre-connect check and the post-connect check the path
            # is replaced by another socket with identical owner, group and mode.
            os.replace(other_path, server.path)
            return _UID

        backend = _VerifiedUnixBackend(server.identity(), peer=swap_then_report_owner)
        transport = CertificationSocketTransport(server.identity(), backend=backend)
        try:
            with pytest.raises(httpx.ConnectError, match="socket refused"):
                await _get(transport)
            await asyncio.sleep(0.05)
        finally:
            other.close()
    assert server.received == [b""]


async def test_unexpected_walk_failure_is_a_refused_socket(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failure of the walk other than CertificationSocketError (for example
    # fstat) must still surface as a refused connection, never an unmapped error.
    def failing_walk(_identity: CertificationSocketIdentity) -> tuple[int, int]:
        raise OSError(5, "Input/output error")

    async with _serve(short_root) as server:
        monkeypatch.setattr(
            coding_certification_socket, "verify_certification_socket", failing_walk
        )
        with pytest.raises(httpx.ConnectError, match="socket refused"):
            await _get(CertificationSocketTransport(server.identity()))
    assert server.connections == 0


def _stat(uid: int, mode: int) -> os.stat_result:
    return os.stat_result((mode, 1, 1, 2, uid, 0, 0, 0, 0, 0))


def test_ancestor_must_be_owned_by_root_or_the_pinned_uid() -> None:
    directory = 0o040755
    assert _safe_ancestor(_stat(0, directory), _UID)
    assert _safe_ancestor(_stat(_UID, directory), _UID)
    # Another user's directory, even when not group or other writable, could be
    # renamed or replaced by that user.
    assert not _safe_ancestor(_stat(_UID + 1, directory), _UID)
    assert not _safe_ancestor(_stat(_UID, 0o040775), _UID)
    assert _safe_ancestor(_stat(0, 0o041777), _UID)
    assert not _safe_ancestor(_stat(_UID, 0o041777), _UID)
    assert not _safe_ancestor(_stat(0, 0o100755), _UID)


async def test_socket_directory_group_is_pinned_independently(
    short_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _serve(short_root) as server:
        target = server.directory.stat().st_ino
        real_fstat = os.fstat

        def other_group_directory(fd: int) -> os.stat_result:
            value = real_fstat(fd)
            if value.st_ino != target:
                return value
            fields = list(value[:10])
            fields[5] = _GID + 1
            return os.stat_result(fields)

        # Only the directory's group differs; the socket entry still matches.
        monkeypatch.setattr(os, "fstat", other_group_directory)
        with pytest.raises(CertificationSocketError):
            verify_certification_socket(server.identity())
        monkeypatch.undo()
        verify_certification_socket(server.identity())


async def test_backend_never_dials_tcp_or_another_path(short_root: Path) -> None:
    async with _serve(short_root) as server:
        backend = _VerifiedUnixBackend(server.identity())
        with pytest.raises(Exception, match="socket refused"):
            await backend.connect_tcp("127.0.0.1", 8000)
        with pytest.raises(Exception, match="socket refused"):
            await backend.connect_unix_socket(str(server.directory / "other.sock"))
    assert server.connections == 0


@pytest.mark.parametrize(
    "values",
    [
        {"uid": 0, "gid": _GID},
        {"uid": _UID, "gid": 0},
        {"uid": -1, "gid": _GID},
        {"uid": True, "gid": _GID},
        {"uid": str(_UID), "gid": _GID},
        {"uid": 1 << 31, "gid": _GID},
        {"uid": _UID, "gid": _GID, "path": "control.sock"},
        {"uid": _UID, "gid": _GID, "path": "/run/../run/x/control.sock"},
        {"uid": _UID, "gid": _GID, "path": "/control.sock"},
        {"uid": _UID, "gid": _GID, "path": "/run/" + "x" * 120 + "/c.sock"},
    ],
)
def test_socket_identity_refuses_root_or_malformed_pins(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="identity is invalid"):
        CertificationSocketIdentity(**values)  # type: ignore[arg-type]


def test_socket_identity_defaults_to_the_fixed_path() -> None:
    identity = CertificationSocketIdentity(_UID, _GID)
    assert identity.path == CERTIFICATION_SOCKET_PATH
    assert CERTIFICATION_SOCKET_PATH == "/run/ditto-coding-certification/control.sock"


async def test_runtime_readiness_end_to_end_over_the_verified_socket(
    short_root: Path,
) -> None:
    body = json.dumps(_READY_VECTOR).encode()
    async with _serve(short_root, body=body) as server:
        config = SimpleNamespace(
            dittobench_control_token="scorer-control-token-000000000000000001",
            coding_certification_control_token="coding-certification-token-00000001",
            coding_certification_socket_uid=_UID,
            coding_certification_socket_gid=_GID,
            coding_certification_runtime_image_digest=_READY_VECTOR[
                "runtime_image_digest"
            ],
            coding_certification_pack_manifest_sha256=_READY_VECTOR[
                "canary_manifest_sha256"
            ],
        )
        runtime = CodingCanaryRuntime(
            config,  # type: ignore[arg-type]
            transport=CertificationSocketTransport(server.identity()),
        )
        try:
            readiness = await runtime.require_ready()
        finally:
            await runtime.aclose()
    assert readiness.canary_manifest_sha256 == _READY_VECTOR["canary_manifest_sha256"]
    (request,) = server.received
    assert b"Authorization: Bearer coding-certification-token-00000001" in request
    assert b"scorer-control-token" not in request
