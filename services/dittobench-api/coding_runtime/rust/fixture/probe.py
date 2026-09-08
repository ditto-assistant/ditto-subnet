"""Public synthetic compiler/bridge control, not a production grading adapter."""

import contextlib
import fcntl
import hashlib
import importlib.util
import os
import signal
import socket
import stat
import subprocess
import tempfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("compiled_launcher", "/opt/launcher.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

grader = Path("/run/dittobench-grader")
grader.mkdir(exist_ok=True)
grader.chmod(0o700)
(grader / "secret").write_text("public synthetic private-file marker")
(grader / "secret").chmod(0o400)
assert os.geteuid() == 0
compiler = subprocess.check_output(
    ["/opt/bridge-probe", "compiler-program"], env={}, timeout=5, text=True
).strip()
tool_env = dict(
    line.split("=", 1)
    for line in subprocess.check_output(
        ["/opt/bridge-probe", "compiler-environment"], env={}, timeout=5, text=True
    ).splitlines()
)
assert compiler == "/opt/rustc" and tool_env == {"PATH": "/usr/bin:/bin"}
marker = Path("/tmp/rust-bridge-entered")
assert not marker.exists()


def compile_source(arguments, directory, *, fixed_recipe=False):
    process = subprocess.Popen(
        [
            compiler,
            *([] if fixed_recipe else ["--edition=2021", "-C", "overflow-checks=yes"]),
            *arguments,
        ],
        cwd=directory,
        env=tool_env,
        user=10001,
        group=10001,
        extra_groups=(),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        return process.wait(timeout=60)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


with tempfile.TemporaryDirectory(prefix="rust-bridge-", dir="/scratch") as directory:
    directory = Path(directory)
    directory.chmod(0o700)
    os.chown(directory, 10001, 10001)
    # A compiler running under the candidate identity must not read grader data.
    source = directory / "private_read.rs"
    source.write_text(
        'pub const SECRET: &str = include_str!("/run/dittobench-grader/secret");'
    )
    source.chmod(0o444)
    assert (
        compile_source(
            ["--crate-type=rlib", str(source), "-o", str(directory / "forbidden.rlib")],
            directory,
        )
        != 0
    )
    print("non-root compiler denied protected grader read", flush=True)

    control = Path("/scratch/input-control")
    control.mkdir(mode=0o755)
    frozen = control / "frozen"
    frozen.mkdir(mode=0o700)
    (frozen / "src").mkdir(mode=0o755)
    (frozen / "src/lib.rs").write_bytes(Path("/opt/fixture/candidate.rs").read_bytes())
    (frozen / "src/lib.rs").chmod(0o600)
    (frozen / "tests").mkdir(mode=0o700)
    (frozen / "tests/hidden.rs").write_text(
        "public synthetic oracle; never a compiler input"
    )
    staged = Path(
        subprocess.check_output(
            ["/opt/bridge-probe", "stage", str(frozen), str(control)],
            env={},
            timeout=5,
            text=True,
        ).strip()
    )
    assert staged.parent == control
    assert {str(p.relative_to(staged)) for p in staged.rglob("*") if p.is_file()} == {
        "src/lib.rs",
        "bridge.rs",
    }
    assert stat.S_IMODE(staged.stat().st_mode) == 0o555
    assert stat.S_IMODE((staged / "src/lib.rs").stat().st_mode) == 0o444
    # Mutation of the original tree after capture cannot alter staged bytes.
    (frozen / "src/lib.rs").write_text("changed after freeze capture")
    assert (staged / "src/lib.rs").read_bytes() == Path(
        "/opt/fixture/candidate.rs"
    ).read_bytes()
    for mode in ("library-args", "bridge-args"):
        arguments = subprocess.check_output(
            ["/opt/bridge-probe", mode], env={}, timeout=5, text=True
        ).splitlines()
        assert compile_source(arguments, staged, fixed_recipe=True) == 0
    binary = Path("/out/candidate")
    print("manifest-only frozen inputs and fixed compiler recipe verified", flush=True)
    assert not marker.exists()
    fd = os.open(binary, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    sealed = os.memfd_create(
        "public-rust-bridge", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING | 0x10
    )  # MFD_EXEC; no fallback
    try:
        info = os.fstat(fd)
        assert (
            stat.S_ISREG(info.st_mode) and info.st_uid == 10001 and info.st_nlink == 1
        )
        assert 64 <= info.st_size <= 256 << 20
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(fd, 1 << 20):
            total += len(chunk)
            assert total <= info.st_size
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(sealed, view)
                assert written > 0
                view = view[written:]
        assert total == info.st_size
        os.fchmod(sealed, 0o555)
        fcntl.fcntl(
            sealed,
            fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_WRITE
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_SEAL,
        )
        for invalid_digest in ["0" * 64]:
            try:
                launcher.launch(sealed, invalid_digest, 10001, 10001)
            except launcher.BootstrapError:
                pass
            else:
                raise AssertionError("wrong digest accepted")
            assert not marker.exists()
        parent, child = socket.socketpair()
        with parent, child:
            candidate = launcher.launch(
                sealed, digest.hexdigest(), 10001, 10001, api_socket=child
            )
            child.close()
            try:
                client = subprocess.run(
                    ["/opt/bridge-probe", "check"],
                    stdin=parent,
                    capture_output=True,
                    env={},
                    timeout=15,
                )
                assert (
                    client.returncode == 0
                    and client.stdout == b"public bridge calls verified\n"
                ), client.stderr[:4096].decode("utf-8", errors="replace")
            finally:
                launcher.terminate(candidate)
            assert candidate.poll() is not None
            assert marker.read_bytes() == b"public constructor"
        print(
            "compiled Rust API bridge and pre-exec constructor confinement verified",
            flush=True,
        )
        print("candidate termination and reap verified", flush=True)
    finally:
        os.close(fd)
        os.close(sealed)

try:
    os.waitpid(-1, os.WNOHANG)
except ChildProcessError:
    pass
else:
    raise AssertionError("fixture retained an unreaped child")

# An implementation signature mismatch must fail compilation, never change the
# controller-approved schema to fit miner code.
with tempfile.TemporaryDirectory(prefix="rust-wrong-api-", dir="/scratch") as directory:
    directory = Path(directory)
    directory.chmod(0o700)
    os.chown(directory, 10001, 10001)
    source = directory / "wrong.rs"
    source.write_text(
        Path("/opt/fixture/candidate.rs")
        .read_text()
        .replace(
            "pub fn add(a:i64,b:i64)->i64{a+b}", "pub fn add(a:i64,b:i64)->bool{a==b}"
        )
    )
    source.chmod(0o444)
    library = directory / "libcandidate.rlib"
    assert (
        compile_source(
            [
                "--crate-name=candidate",
                "--crate-type=rlib",
                str(source),
                "-o",
                str(library),
            ],
            directory,
        )
        == 0
    )
    assert (
        compile_source(
            [
                "/opt/fixture/bridge.rs",
                "--extern",
                "candidate=" + str(library),
                "--extern",
                "coding_rust_suite=/opt/deps/libcoding_rust_suite.rlib",
                "-L",
                "dependency=/opt/deps",
                "-o",
                str(directory / "wrong"),
            ],
            directory,
        )
        != 0
    )
    print("candidate API signature mismatch rejected", flush=True)
