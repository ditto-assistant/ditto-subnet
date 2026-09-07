"""UID-authenticated, bounded, one-request Unix transport for private key custody."""

from __future__ import annotations

import asyncio
import os
import socket
import stat
import struct
from pathlib import Path

from ditto.api_models.coding_inference import _decode_json_document
from ditto.api_server.coding_private_v2_custody import (
    PrivateV2Custody,
    PrivateV2CustodyError,
)


def peer_uid(writer: asyncio.StreamWriter) -> int:
    sock = writer.get_extra_info("socket")
    if sock is None or sock.family != socket.AF_UNIX:
        raise PrivateV2CustodyError("native custody peer unavailable")
    return struct.unpack(
        "3i",
        sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
    )[1]


def socket_directory(path: Path, *, owner: int) -> None:
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner
        or stat.S_IMODE(info.st_mode) != 0o755
    ):
        raise PrivateV2CustodyError("native custody socket directory unsafe")
    for parent in path.parents:
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, owner}
            or (
                info.st_mode & 0o022
                and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            )
        ):
            raise PrivateV2CustodyError("native custody socket ancestor unsafe")


class PrivateV2CustodyServer:
    def __init__(
        self, *, path: Path, client_uid: int, services: dict[str, PrivateV2Custody]
    ):
        if (
            type(client_uid) is not int
            or client_uid <= 0
            or os.geteuid() <= 0
            or client_uid == os.geteuid()
            or set(services) != {"platform-authoring", "platform-grading"}
        ):
            raise PrivateV2CustodyError("native custody server configuration invalid")
        socket_directory(path.parent, owner=os.geteuid())
        self._path, self._uid, self._services = path, client_uid, dict(services)
        self._server: asyncio.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._inode: tuple[int, int] | None = None
        self._used = False

    async def start(self) -> None:
        if self._used or self._path.exists() or self._path.is_symlink():
            raise PrivateV2CustodyError("native custody socket already consumed")
        self._used = True
        # start_unix_server may unlink an existing path, so bind ourselves first.
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self._path))
            info = self._path.lstat()
            self._inode = (info.st_dev, info.st_ino)
            os.chmod(self._path, 0o666)
            sock.setblocking(False)
            self._server = await asyncio.start_unix_server(
                self._handle, sock=sock, limit=16384
            )
        except BaseException:
            sock.close()
            await self.close()
            raise

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        try:
            if (
                peer_uid(writer) != self._uid
                or len(self._tasks) >= 4
                or self._server is None
            ):
                return
            assert task is not None
            self._tasks.add(task)
            async with asyncio.timeout(20):
                body = await reader.readuntil(b"\n")
                if len(body) > 16384:
                    return
                raw = _decode_json_document(body, maximum_bytes=16384)
                if (
                    not isinstance(raw, dict)
                    or raw.get("audience") not in self._services
                ):
                    return
                result = await self._services[raw["audience"]].handle(body)
                if len(result) > 1024:
                    return
                writer.write(result)
                await writer.drain()
        except Exception:
            pass  # Close-only denial: no diagnostic, metadata, or key on errors.
        finally:
            if task is not None:
                self._tasks.discard(task)
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except Exception:
                pass

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        if self._inode is not None:
            owned_inode, self._inode = self._inode, None
            try:
                info = self._path.lstat()
                if (info.st_dev, info.st_ino) == owned_inode and stat.S_ISSOCK(
                    info.st_mode
                ):
                    self._path.unlink()
            except FileNotFoundError:
                pass


async def proxy_request(*, path: Path, expected_server_uid: int, body: bytes) -> bytes:
    if (
        type(expected_server_uid) is not int
        or expected_server_uid <= 0
        or expected_server_uid == os.geteuid()
        or not 0 < len(body) <= 16384
        or not body.endswith(b"\n")
    ):
        raise PrivateV2CustodyError("native custody proxy configuration invalid")
    socket_directory(path.parent, owner=expected_server_uid)
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != expected_server_uid:
        raise PrivateV2CustodyError("native custody socket invalid")
    async with asyncio.timeout(20):
        reader, writer = await asyncio.open_unix_connection(str(path), limit=1024)
        try:
            if peer_uid(writer) != expected_server_uid:
                raise PrivateV2CustodyError("native custody peer mismatch")
            writer.write(body)
            await writer.drain()
            result = await reader.readuntil(b"\n")
            if len(result) > 1024:
                raise PrivateV2CustodyError("native custody response invalid")
            return result
        finally:
            writer.close()
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except Exception:
                pass
