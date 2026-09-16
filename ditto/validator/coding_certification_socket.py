"""Verified Unix-socket transport to the host coding certification service.

The validator reaches the certification service only through one Unix socket at
a fixed path. Before every connection it proves, without following links, that
each ancestor directory is a real directory that only root or the service user
can modify, that the socket directory has the pinned owner, group and mode
0750, and that the socket has the pinned owner, group and mode 0660. After
connecting it requires the peer process to run as the pinned owner and the path
to still name the same socket inode. Anything else closes the connection before
a request byte is sent. There is no TCP fallback.
"""

from __future__ import annotations

import os
import socket
import stat
import struct
import typing
from dataclasses import dataclass

import httpcore
import httpx

CERTIFICATION_SOCKET_DIRECTORY = "/run/ditto-coding-certification"
CERTIFICATION_SOCKET_PATH = f"{CERTIFICATION_SOCKET_DIRECTORY}/control.sock"
CERTIFICATION_SOCKET_MODE = 0o660
CERTIFICATION_DIRECTORY_MODE = 0o750
# The request authority for the socket transport. It is never resolved.
CERTIFICATION_ORIGIN = "http://coding-certification.invalid"
_MAX_ID = (1 << 31) - 2
_SUN_PATH_MAX = 107


class CertificationSocketError(OSError):
    """The certification socket failed verification. Carries no path or id."""

    def __init__(self) -> None:
        super().__init__("coding certification socket refused")


@dataclass(frozen=True)
class CertificationSocketIdentity:
    """The pinned owner and group of the fixed certification socket."""

    uid: int
    gid: int
    path: str = CERTIFICATION_SOCKET_PATH

    def __post_init__(self) -> None:
        if (
            type(self.uid) is not int
            or type(self.gid) is not int
            or not 1 <= self.uid <= _MAX_ID
            or not 1 <= self.gid <= _MAX_ID
            or not _clean_socket_path(self.path)
        ):
            raise ValueError("coding certification socket identity is invalid")


def _clean_socket_path(path: str) -> bool:
    return (
        isinstance(path, str)
        and path.startswith("/")
        and os.path.normpath(path) == path
        and not path.startswith("//")
        and os.path.dirname(path) != "/"
        and "\x00" not in path
        and len(path.encode()) <= _SUN_PATH_MAX
    )


def verify_certification_socket(
    identity: CertificationSocketIdentity,
) -> tuple[int, int]:
    """Return the socket's ``(st_dev, st_ino)`` or raise CertificationSocketError.

    The walk opens each directory relative to its parent with ``O_NOFOLLOW`` and
    inspects the final entry with ``follow_symlinks=False``, so a link anywhere
    on the path is refused rather than followed.
    """

    directory, name = os.path.split(identity.path)
    components = [part for part in directory.split("/") if part]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    try:
        fd = os.open("/", flags)
    except OSError as error:
        raise CertificationSocketError() from error
    try:
        for index, component in enumerate(components):
            current = os.fstat(fd)
            if not _safe_ancestor(current, identity.uid):
                raise CertificationSocketError()
            try:
                child = os.open(component, flags | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as error:
                raise CertificationSocketError() from error
            os.close(fd)
            fd = child
            if index == len(components) - 1:
                break
        parent = os.fstat(fd)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != identity.uid
            or parent.st_gid != identity.gid
            or stat.S_IMODE(parent.st_mode) != CERTIFICATION_DIRECTORY_MODE
        ):
            raise CertificationSocketError()
        try:
            entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError as error:
            raise CertificationSocketError() from error
        if (
            not stat.S_ISSOCK(entry.st_mode)
            or entry.st_uid != identity.uid
            or entry.st_gid != identity.gid
            or stat.S_IMODE(entry.st_mode) != CERTIFICATION_SOCKET_MODE
        ):
            raise CertificationSocketError()
        return entry.st_dev, entry.st_ino
    finally:
        os.close(fd)


def _safe_ancestor(value: os.stat_result, uid: int) -> bool:
    if not stat.S_ISDIR(value.st_mode) or value.st_uid not in (0, uid):
        return False
    writable_by_others = value.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    sticky_root = value.st_uid == 0 and value.st_mode & stat.S_ISVTX
    return not writable_by_others or bool(sticky_root)


def peer_uid(connection: socket.socket) -> int:
    """The connected peer's uid from the kernel (``SO_PEERCRED``)."""

    raw = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
    )
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


class _VerifiedUnixBackend(httpcore.AsyncNetworkBackend):
    """Connects only to the verified certification socket."""

    def __init__(
        self,
        identity: CertificationSocketIdentity,
        inner: httpcore.AsyncNetworkBackend | None = None,
        peer: typing.Callable[[socket.socket], int] = peer_uid,
    ) -> None:
        self._identity = identity
        self._inner = inner or httpcore.AnyIOBackend()
        self._peer = peer

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        # The certification service has no TCP listener; never dial one.
        del host, port, timeout, local_address, socket_options
        raise httpcore.ConnectError("coding certification socket refused")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if path != self._identity.path:
            raise httpcore.ConnectError("coding certification socket refused")
        try:
            before = verify_certification_socket(self._identity)
        except OSError as error:
            # CertificationSocketError, or any other failure of the walk (such
            # as fstat), is a refused socket rather than an unmapped error.
            raise httpcore.ConnectError(
                "coding certification socket refused"
            ) from error
        stream = await self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )
        try:
            connection = stream.get_extra_info("socket")
            if (
                not isinstance(connection, socket.socket)
                or connection.family != socket.AF_UNIX
                or self._peer(connection) != self._identity.uid
                or verify_certification_socket(self._identity) != before
            ):
                raise CertificationSocketError()
        except (OSError, struct.error) as error:
            await stream.aclose()
            raise httpcore.ConnectError(
                "coding certification socket refused"
            ) from error
        return stream

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class CertificationSocketTransport(httpx.AsyncHTTPTransport):
    """HTTP/1.1 over the verified socket; one fresh, verified connection per request.

    It never reads proxy, TLS or socket settings from the environment, keeps no
    idle connection (so every request re-verifies the socket and its peer), and
    never retries.
    """

    def __init__(
        self,
        identity: CertificationSocketIdentity,
        *,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        super().__init__(
            uds=identity.path, trust_env=False, retries=0, http1=True, http2=False
        )
        self._pool = httpcore.AsyncConnectionPool(
            uds=identity.path,
            max_connections=4,
            max_keepalive_connections=0,
            keepalive_expiry=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=backend or _VerifiedUnixBackend(identity),
        )
