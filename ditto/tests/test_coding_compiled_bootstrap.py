"""Read-only unit tests; native launch probes run only in the synthetic image."""

import array
import ctypes
import fcntl
import hashlib
import importlib.util
import os
import socket
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[2]
SOURCE = ROOT / "services/dittobench-api/coding_runtime/compiled/launcher.py"
spec = importlib.util.spec_from_file_location("compiled_launcher_test", SOURCE)
assert spec is not None and spec.loader is not None
LAUNCHER = importlib.util.module_from_spec(spec)
spec.loader.exec_module(LAUNCHER)


def elf_bytes():
    raw = bytearray(64)
    raw[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", raw, 16, 2, 62)
    return bytes(raw)


def parent_status():
    return (
        "CapEff:\t00000000000000e0\nCapPrm:\t00000000000000e0\n"
        "CapInh:\t0\nCapAmb:\t0\nNoNewPrivs:\t1\n"
    )


def test_bounded_parent_termination_authority():
    LAUNCHER.validate_parent_status(parent_status())
    LAUNCHER.validate_parent_status(parent_status().replace("00e0", "00e3"))


def test_api_socket_requires_parent_owned_unix_stream():
    parent, peer = socket.socketpair()
    with parent, peer:
        LAUNCHER.validate_api_socket(peer)
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as wrong,
        pytest.raises(LAUNCHER.BootstrapError),
    ):
        LAUNCHER.validate_api_socket(wrong)
    with pytest.raises(LAUNCHER.BootstrapError):
        LAUNCHER.validate_api_socket(0)


@pytest.mark.parametrize("sealed", [False, True])
def test_sealed_descriptor_is_duplicated_and_rechecked(monkeypatch, sealed):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.memfd_create.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    libc.memfd_create.restype = ctypes.c_int
    fd = libc.memfd_create(b"public-elf-probe", 0x13)  # CLOEXEC | ALLOW_SEALING | EXEC
    assert fd >= 0
    try:
        raw = elf_bytes()
        os.write(fd, raw)
        os.fchmod(fd, 0o555)
        # Unit test checks descriptor mechanics without root or execution.
        original = LAUNCHER.os.fstat

        def root_stat(value):
            stat = original(value)
            return SimpleNamespace(
                st_mode=stat.st_mode,
                st_uid=0,
                st_nlink=stat.st_nlink,
                st_size=stat.st_size,
            )

        monkeypatch.setattr(
            LAUNCHER, "os", SimpleNamespace(fstat=root_stat, close=os.close)
        )
        if sealed:
            fcntl.fcntl(
                fd,
                1033,  # Linux F_ADD_SEALS
                LAUNCHER.REQUIRED_SEALS,
            )
            duplicate = LAUNCHER.sealed_program_fd(fd)
            try:
                assert duplicate != fd and not os.get_inheritable(duplicate)
            finally:
                os.close(duplicate)
        else:
            with pytest.raises(LAUNCHER.BootstrapError):
                LAUNCHER.sealed_program_fd(fd)
        assert os.pread(fd, len(raw), 0) == raw
    finally:
        os.close(fd)


@pytest.mark.parametrize(
    "old,new",
    [
        ("00e0", "00c0"),
        ("00e0", "20e0"),
        ("CapInh:\t0", "CapInh:\t1"),
        ("CapAmb:\t0", "CapAmb:\t1"),
        ("NoNewPrivs:\t1", "NoNewPrivs:\t0"),
    ],
)
def test_rejects_missing_kill_or_extra_parent_privileges(old, new):
    with pytest.raises(LAUNCHER.BootstrapError):
        LAUNCHER.validate_parent_status(parent_status().replace(old, new))


def test_verifies_exact_elf_bytes_without_running_them(tmp_path):
    raw = elf_bytes()
    path = tmp_path / "fixture"
    path.write_bytes(raw)
    with path.open("rb") as stream:
        LAUNCHER.verify_program(stream.fileno(), hashlib.sha256(raw).hexdigest())
        assert stream.tell() == 0


def test_digest_verification_does_not_move_shared_descriptor_offset(tmp_path):
    raw = elf_bytes()
    path = tmp_path / "fixture"
    path.write_bytes(raw)
    with path.open("rb") as stream:
        stream.seek(17)
        duplicate = os.dup(stream.fileno())
        try:
            LAUNCHER.verify_program(duplicate, hashlib.sha256(raw).hexdigest())
            assert stream.tell() == 17
        finally:
            os.close(duplicate)


@pytest.mark.parametrize(
    "raw", [b"#! /bin/sh\n" + bytes(64), bytes(64), elf_bytes()[:32]]
)
def test_refuses_non_elf_or_truncated_headers(tmp_path, raw):
    path = tmp_path / "fixture"
    path.write_bytes(raw)
    with path.open("rb") as stream, pytest.raises(LAUNCHER.BootstrapError):
        LAUNCHER.verify_program(stream.fileno(), hashlib.sha256(raw).hexdigest())


def test_refuses_digest_mismatch_before_spawn(tmp_path):
    path = tmp_path / "fixture"
    path.write_bytes(elf_bytes())
    with path.open("rb") as stream, pytest.raises(LAUNCHER.BootstrapError):
        LAUNCHER.verify_program(stream.fileno(), "0" * 64)


@pytest.mark.parametrize(
    "uid,gid,timeout",
    [
        (0, 1, 1),
        (1, 0, 1),
        (True, 1, 1),
        (1, 2**32 - 1, 1),
        (1, 1, 0),
        (1, 1, 31),
        (1, 1, float("nan")),
    ],
)
def test_bad_authority_rejected_before_any_program_open(monkeypatch, uid, gid, timeout):
    monkeypatch.setattr(LAUNCHER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(LAUNCHER.platform, "system", lambda: "Linux")
    monkeypatch.setattr(LAUNCHER.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        LAUNCHER, "readonly_program", lambda *_a: pytest.fail("program opened")
    )
    with pytest.raises(LAUNCHER.BootstrapError):
        LAUNCHER.launch(Path("/unused"), "0" * 64, uid, gid, timeout)


def test_wrong_parent_identity_rejected_before_program_open(monkeypatch):
    monkeypatch.setattr(LAUNCHER.os, "geteuid", lambda: 1000)
    with pytest.raises(LAUNCHER.BootstrapError):
        LAUNCHER.launch(Path("/unused"), "0" * 64, 10001, 10001)


def test_readiness_wait_is_bounded():
    read, write = os.pipe()
    try:
        with pytest.raises(LAUNCHER.BootstrapError):
            LAUNCHER.wait_readable(read, time.monotonic() + 0.01)
        os.write(write, b"x")
        LAUNCHER.wait_readable(read, time.monotonic() + 1)
    finally:
        os.close(read)
        os.close(write)


@pytest.mark.parametrize("mismatch", [False, True])
def test_scm_handoff_binds_pid_fd_uid_and_closes_rejected_descriptors(mismatch):
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    read, write = os.pipe()
    process = SimpleNamespace(pid=1234, poll=lambda: None)
    with parent, child:
        try:
            body = LAUNCHER.HANDOFF.pack(1, 1234, 42, 10001, 10001)
            child.sendmsg(
                [body],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [read]))],
            )
            if mismatch:
                before = len(list(Path("/proc/self/fd").iterdir()))
                with pytest.raises(LAUNCHER.BootstrapError):
                    LAUNCHER.receive_listener(
                        parent, process, 43, 10001, 10001, time.monotonic() + 1
                    )
                assert len(list(Path("/proc/self/fd").iterdir())) == before
            else:
                received = LAUNCHER.receive_listener(
                    parent, process, 42, 10001, 10001, time.monotonic() + 1
                )
                assert not os.get_inheritable(received)
                os.close(received)
        finally:
            os.close(read)
            os.close(write)


@pytest.mark.parametrize(
    "field,value",
    [
        ("pid", 999),
        ("flags", 1),
        ("nr", 59),
        ("arch", 0x40000003),
        ("fd", 43),
        ("exec_flags", 0),
    ],
)
def test_unexpected_notification_is_never_continued(monkeypatch, field, value):
    record = {
        "pid": 1234,
        "flags": 0,
        "nr": 322,
        "arch": LAUNCHER.ARCH,
        "fd": 42,
        "exec_flags": 0x1000,
    }
    record[field] = value
    calls = []

    def ioctl(_fd, command, buffer, *_args):
        calls.append(command)
        LAUNCHER.NOTIFICATION.pack_into(
            buffer,
            0,
            55,
            record["pid"],
            record["flags"],
            record["nr"],
            record["arch"],
            0,
            record["fd"],
            0,
            0,
            0,
            record["exec_flags"],
            0,
        )

    monkeypatch.setattr(LAUNCHER, "wait_readable", lambda *_a: None)
    monkeypatch.setattr(LAUNCHER.fcntl, "ioctl", ioctl)
    with pytest.raises(LAUNCHER.BootstrapError):
        LAUNCHER.continue_initial_exec(
            8,
            SimpleNamespace(pid=1234, poll=lambda: None),
            42,
            (80, 24),
            time.monotonic() + 1,
        )
    assert calls == [LAUNCHER.NOTIF_RECV]


def test_one_initial_continue_uses_kernel_cookie_without_reading_target_memory(
    monkeypatch,
):
    calls = []

    def ioctl(_fd, command, buffer, *_args):
        calls.append(command)
        if command == LAUNCHER.NOTIF_RECV:
            LAUNCHER.NOTIFICATION.pack_into(
                buffer,
                0,
                55,
                1234,
                0,
                322,
                LAUNCHER.ARCH,
                123,
                42,
                111,
                222,
                333,
                0x1000,
                0,
            )
        elif command == LAUNCHER.NOTIF_ID_VALID:
            assert struct.unpack("<Q", buffer) == (55,)
        else:
            assert LAUNCHER.RESPONSE.unpack(buffer) == (55, 0, 0, 1)

    monkeypatch.setattr(LAUNCHER, "wait_readable", lambda *_a: None)
    monkeypatch.setattr(LAUNCHER.fcntl, "ioctl", ioctl)
    LAUNCHER.continue_initial_exec(
        8,
        SimpleNamespace(pid=1234, poll=lambda: None),
        42,
        (80, 24),
        time.monotonic() + 1,
    )
    assert calls == [LAUNCHER.NOTIF_RECV, LAUNCHER.NOTIF_ID_VALID, LAUNCHER.NOTIF_SEND]


def test_filter_replacement_apis_and_foreign_signals_are_explicitly_fenced():
    source = SOURCE.with_name("bootstrap.c").read_text()
    assert "DENY(SYS_seccomp)" in source and "PR_SET_SECCOMP" in source
    assert "PR_SET_DUMPABLE, 0" in source and "count == 1" in source
    assert "SELF_SIGNAL(SYS_tgkill, self)" in source
    assert "DENY(SYS_execve)" in source and "SECCOMP_RET_USER_NOTIF" in source
    assert source.index("close(listener)") < source.index("syscall(SYS_execveat")
    assert SOURCE.with_name("probe.go").read_text().startswith("//go:build ignore\n")
