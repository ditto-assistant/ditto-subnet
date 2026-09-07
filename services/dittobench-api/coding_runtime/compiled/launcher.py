"""Trusted container-side compiled-program launch; no grading or private inputs.

The sole CONTINUE response belongs to the immutable, single-threaded bootstrap,
not candidate code. The listener closes immediately afterward. Kernel filters
then deny future exec, fork, group escape and replacement filter installation.
"""

import array
import ctypes
import fcntl
import hashlib
import os
import platform
import re
import selectors
import socket
import stat
import struct
import subprocess
import time
from pathlib import Path

BOOTSTRAP = Path("/usr/local/libexec/dittobench-compiled-bootstrap")
MAX_BINARY = 256 << 20
MAX_STARTUP_SECONDS = 30
HANDOFF = struct.Struct("<5I")
NOTIFICATION = struct.Struct("<QIIiIQ6Q")
RESPONSE = struct.Struct("<QqiI")
# Linux amd64 UAPI encodings from <linux/seccomp.h>, checked by the native probe.
NOTIF_RECV = 0xC0502100
NOTIF_SEND = 0xC0182101
NOTIF_ID_VALID = 0x40082102
ARCH = 0xC000003E
SYS_EXECVEAT = 322
AT_EMPTY_PATH = 0x1000


class BootstrapError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise BootstrapError(message)


def validate_parent_status(source):
    required, allowed = 0xE0, 0xE3  # KILL/SETGID/SETUID; optional CHOWN/DAC_OVERRIDE
    caps = {}
    for field in ("CapEff", "CapPrm", "CapInh", "CapAmb"):
        match = re.search(r"^" + field + r":\s+([0-9a-fA-F]+)$", source, re.MULTILINE)
        require(match is not None, "parent capability evidence missing")
        assert match is not None
        caps[field] = int(match[1], 16)
    require(
        caps["CapEff"] & required == required
        and not caps["CapEff"] & ~allowed
        and not caps["CapPrm"] & ~allowed
        and caps["CapInh"] == caps["CapAmb"] == 0
        and re.search(r"^NoNewPrivs:\s+1$", source, re.MULTILINE),
        "parent lacks bounded identity and termination authority",
    )


def protected_parents(path):
    require(path.is_absolute() and path.resolve() == path, "noncanonical program path")
    for parent in path.parents:
        info = parent.lstat()
        require(
            stat.S_ISDIR(info.st_mode)
            and info.st_uid == 0
            and not info.st_mode & 0o022,
            "unprotected program directory",
        )


def readonly_program(path):
    protected_parents(path)
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o555
            and info.st_uid == 0
            and info.st_nlink == 1
            and 64 <= info.st_size <= MAX_BINARY,
            "program is not a sealed root-owned regular file",
        )
    except BaseException:
        os.close(fd)
        raise
    return fd


def verify_program(fd, expected_sha256):
    require(re.fullmatch(r"[0-9a-f]{64}", expected_sha256), "invalid program digest")
    before = os.fstat(fd)
    header = os.pread(fd, 64, 0)
    require(
        len(header) == 64
        and header[:6] == b"\x7fELF\x02\x01"
        and struct.unpack_from("<H", header, 18)[0] == 62
        and struct.unpack_from("<H", header, 16)[0] in (2, 3),
        "Linux amd64 ELF program required",
    )
    digest, total = hashlib.sha256(), 0
    os.lseek(fd, 0, os.SEEK_SET)
    while chunk := os.read(fd, 1 << 20):
        total += len(chunk)
        require(total <= MAX_BINARY, "program exceeds size bound")
        digest.update(chunk)
    after = os.fstat(fd)
    require(
        digest.hexdigest() == expected_sha256
        and total == before.st_size == after.st_size
        and before.st_mtime_ns == after.st_mtime_ns
        and before.st_ctime_ns == after.st_ctime_ns,
        "program digest or metadata changed",
    )
    os.lseek(fd, 0, os.SEEK_SET)


def notification_sizes():
    require(
        platform.system() == "Linux" and platform.machine() == "x86_64",
        "Linux amd64 notification ABI required",
    )
    sizes = (ctypes.c_ushort * 3)()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    require(
        libc.syscall(317, 3, 0, ctypes.byref(sizes)) == 0,
        "seccomp notification ABI unavailable",
    )
    notification_size, response_size, data_size = sizes
    require(
        NOTIFICATION.size <= notification_size <= 4096
        and RESPONSE.size <= response_size <= 4096
        and 64 <= data_size <= 4096,
        "unsupported seccomp notification ABI",
    )
    return notification_size, response_size


def wait_readable(fd, deadline):
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        remaining = deadline - time.monotonic()
        require(
            remaining > 0 and selector.select(remaining), "bootstrap deadline exceeded"
        )


def receive_listener(control, process, binary_fd, uid, gid, deadline):
    wait_readable(control, deadline)
    message, ancillary, flags, _ = control.recvmsg(
        HANDOFF.size + 1, socket.CMSG_SPACE(4 * 4), socket.MSG_CMSG_CLOEXEC
    )
    received: list[int] = []
    valid_ancillary = len(ancillary) == 1
    for level, kind, data in ancillary:
        if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
            valid_ancillary = False
            continue
        descriptors = array.array("i")
        if len(data) % descriptors.itemsize:
            valid_ancillary = False
        descriptors.frombytes(data[: len(data) - len(data) % descriptors.itemsize])
        received.extend(descriptors)
    try:
        require(
            not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC)
            and valid_ancillary
            and len(received) == 1
            and len(message) == HANDOFF.size
            and HANDOFF.unpack(message) == (1, process.pid, binary_fd, uid, gid),
            "invalid trusted bootstrap handoff",
        )
        require(process.poll() is None, "bootstrap terminated before handoff")
    except BaseException:
        for fd in received:
            os.close(fd)
        raise
    return received[0]


def continue_initial_exec(listener, process, binary_fd, sizes, deadline):
    wait_readable(listener, deadline)
    notification = bytearray(sizes[0])
    fcntl.ioctl(listener, NOTIF_RECV, notification, True)
    identity, pid, flags, nr, arch, _ip, *args = NOTIFICATION.unpack_from(notification)
    require(
        process.poll() is None
        and pid == process.pid
        and flags == 0
        and nr == SYS_EXECVEAT
        and arch == ARCH
        and args[0] == binary_fd
        and args[4] == AT_EMPTY_PATH,
        "unexpected initial execution notification",
    )
    # Do not read target memory, resolve target-supplied paths, emulate syscalls,
    # or service another request. Only trusted bootstrap code has run so far;
    # it holds the verified FD, has one thread and is nondumpable/non-root.
    fcntl.ioctl(listener, NOTIF_ID_VALID, struct.pack("<Q", identity))
    response = bytearray(sizes[1])
    RESPONSE.pack_into(response, 0, identity, 0, 0, 1)  # USER_NOTIF_FLAG_CONTINUE
    fcntl.ioctl(listener, NOTIF_SEND, response, True)


def terminate(process):
    try:
        try:
            if process.poll() is None:
                process.kill()
        finally:
            process.wait(timeout=5)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def launch(binary, expected_sha256, uid, gid, startup_timeout=5):
    """Return a child whose initial exec was authorized under an installed filter.

    Success is NOT a language or test success. Caller must own API assertions,
    runtime/output deadlines and terminate()/reap before any grading receipt.
    Use only inside the separately qualified isolated executor container.
    """
    require(
        os.geteuid() == 0
        and platform.system() == "Linux"
        and platform.machine() == "x86_64",
        "trusted Linux amd64 container parent required",
    )
    require(
        type(uid) is int
        and type(gid) is int
        and 0 < uid < 2**32 - 1
        and 0 < gid < 2**32 - 1,
        "invalid candidate identity",
    )
    require(
        type(startup_timeout) in (int, float)
        and 0 < startup_timeout <= MAX_STARTUP_SECONDS,
        "invalid startup deadline",
    )
    validate_parent_status(Path("/proc/self/status").read_text())
    bootstrap_fd = readonly_program(BOOTSTRAP)
    os.close(bootstrap_fd)
    fd = readonly_program(binary)
    process = None
    try:
        verify_program(fd, expected_sha256)
        sizes = notification_sizes()
        deadline = time.monotonic() + startup_timeout
        control, child_control = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_SEQPACKET
        )
        with control, child_control:
            require(
                fd < 1024 and child_control.fileno() < 1024,
                "bootstrap descriptor bound",
            )
            process = subprocess.Popen(
                [
                    str(BOOTSTRAP),
                    str(fd),
                    str(child_control.fileno()),
                    str(uid),
                    str(gid),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd="/workspace",
                env={"PATH": "/usr/local/bin:/usr/bin:/bin"},
                close_fds=True,
                pass_fds=(fd, child_control.fileno()),
            )
            child_control.close()
            listener = receive_listener(control, process, fd, uid, gid, deadline)
            try:
                continue_initial_exec(listener, process, fd, sizes, deadline)
            finally:
                # The stub closed its descriptor before attempting exec. Closing
                # our last copy makes every subsequent execveat return ENOSYS.
                os.close(listener)
        return process
    except BaseException:
        if process is not None:
            terminate(process)
        raise
    finally:
        os.close(fd)
