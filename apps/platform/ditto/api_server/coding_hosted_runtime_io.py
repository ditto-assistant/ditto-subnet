"""Owner-only runtime files and bounded, credential-empty child processes."""

from __future__ import annotations

import asyncio
import os
import signal
import stat
from contextlib import suppress
from pathlib import Path

from ditto.api_models.coding_inference import _decode_json_document


class HostedRuntimeError(ValueError):
    """Fixed safe diagnostic, never includes a path, URL or child output."""


def private_directory(path: Path) -> None:
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise HostedRuntimeError("hosted runtime directory is unsafe")
    for parent in path.parents:
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or (
                info.st_mode & 0o022
                and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
            )
        ):
            raise HostedRuntimeError("hosted runtime directory ancestor is unsafe")


def read_private(path: Path, maximum: int) -> bytes:
    private_directory(path.parent)
    if not path.is_absolute() or path.parent.resolve() / path.name != path:
        raise HostedRuntimeError("hosted runtime file is unsafe")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or not 0 < info.st_size <= maximum
        ):
            raise HostedRuntimeError("hosted runtime file is unsafe")
        body = source.read(maximum + 1)
        if len(body) != info.st_size:
            raise HostedRuntimeError("hosted runtime file changed")
        return body


def read_json(path: Path, maximum: int = 65536):
    body = read_private(path, maximum)
    return _decode_json_document(body, maximum_bytes=maximum)


def write_private(path: Path, body: bytes) -> None:
    private_directory(path.parent)
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    # Partial files are intentionally retained and never treated as replayable.
    with os.fdopen(fd, "wb") as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def protected_helper(path: Path) -> None:
    from ditto.api_server.coding_hosted_installed_worker import (
        installed_worker_path,
        require_installed_worker,
    )

    if installed_worker_path(path):
        try:
            require_installed_worker(path)
            return
        except Exception:
            raise HostedRuntimeError("installed hosted worker is unsafe") from None
    private_directory(path.parent)
    info = path.lstat()
    if (
        not path.is_absolute()
        or path.resolve() != path
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) not in {0o500, 0o700}
        or info.st_nlink != 1
    ):
        raise HostedRuntimeError("hosted runtime executable is unsafe")


async def run_private_process(
    argv: tuple[str, ...],
    *,
    root: Path,
    body: bytes = b"",
    timeout: float,
    maximum_output: int = 4096,
    shutdown_grace: float = 5,
) -> bytes:
    """A fixed trusted executable, no shell or inherited keys. Cancellation
    gives the Go worker time for its own cleanup before forced process-group stop.
    """
    private_directory(root)
    protected_helper(Path(argv[0]))
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=root,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )

    async def exchange() -> bytes:
        assert process.stdin is not None and process.stdout is not None
        if body:
            process.stdin.write(body)
            await process.stdin.drain()
        process.stdin.close()
        chunks = bytearray()
        while chunk := await process.stdout.read(4096):
            chunks.extend(chunk)
            if len(chunks) > maximum_output:
                raise HostedRuntimeError(
                    "hosted runtime child output exceeded its bound"
                )
        if await process.wait() != 0:
            raise HostedRuntimeError("hosted runtime child failed")
        return bytes(chunks)

    def send(sig: int) -> None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, sig)

    task = asyncio.create_task(exchange())
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.shield(task)
    except BaseException:
        send(signal.SIGTERM)
        try:
            async with asyncio.timeout(shutdown_grace):
                await asyncio.shield(task)
        except BaseException:
            send(signal.SIGKILL)
        raise
    finally:
        # Stop surviving children in this invocation's process group as well.
        send(signal.SIGKILL)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await process.wait()
