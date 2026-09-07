"""Public fixture only. Do not copy this into a selectable production image."""

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "compiled_launcher", "/opt/compiled/launcher.py"
)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

for directory in (Path("/run/dittobench-grader"), Path("/run/dittobench-control")):
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
Path("/run/dittobench-grader/secret").write_text("public synthetic marker")
Path("/run/dittobench-grader/secret").chmod(0o400)
assert os.geteuid() == 0
abi = subprocess.check_output(["/opt/compiled/abi"], timeout=5, text=True).split()
assert [int(value) for value in abi] == [
    launcher.NOTIF_RECV,
    launcher.NOTIF_SEND,
    launcher.NOTIF_ID_VALID,
    launcher.NOTIFICATION.size,
    launcher.RESPONSE.size,
]
print("native seccomp notification ABI verified", flush=True)

marker = Path("/tmp/compiled-candidate-started")
candidate = Path("/opt/compiled/candidates/c-init")
candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
if sys.argv[1:] == ["parent-refusal"]:
    try:
        launcher.launch(candidate, candidate_sha, 10001, 10001)
    except launcher.BootstrapError:
        assert not marker.exists()
        print("missing parent termination authority rejected", flush=True)
        raise SystemExit(0) from None
    raise AssertionError("unsafe parent accepted")
original_continue = launcher.continue_initial_exec
original_receive = launcher.receive_listener
for failure in ("wrong-digest", "wrong-fd", "wrong-handoff", "aborted-grant"):
    assert not marker.exists()
    if failure == "wrong-fd":

        def wrong_fd(listener, process, fd, sizes, deadline):
            return original_continue(listener, process, fd + 1, sizes, deadline)

        launcher.continue_initial_exec = wrong_fd
    elif failure == "wrong-handoff":
        launcher.receive_listener = lambda control, process, fd, uid, gid, deadline: (
            original_receive(control, process, fd, uid + 1, gid, deadline)
        )
    elif failure == "aborted-grant":

        def abort(*_args):
            raise launcher.BootstrapError("synthetic parent cancellation")

        launcher.continue_initial_exec = abort
    try:
        launcher.launch(
            candidate,
            "0" * 64 if failure == "wrong-digest" else candidate_sha,
            10001,
            10001,
        )
    except launcher.BootstrapError:
        pass
    else:
        raise AssertionError("failed launch was accepted: " + failure)
    finally:
        launcher.continue_initial_exec = original_continue
        launcher.receive_listener = original_receive
    assert not marker.exists(), failure
    # This fixture owns every child in its container; failed launches must reap.
    try:
        os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        pass
    else:
        raise AssertionError("failed launch retained a child")
    print("failed launch never entered candidate: " + failure, flush=True)

for name, expected in (
    ("c-init", "compiled C constructors and thread isolation passed"),
    ("go-init", "compiled Go init and runtime preemption passed"),
    ("rust-init", "compiled Rust constructor and thread isolation passed"),
):
    path = Path("/opt/compiled/candidates") / name
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    process = launcher.launch(path, digest, 10001, 10001)
    try:
        output, _ = process.communicate(timeout=5)
        assert process.returncode == 0, (name, process.returncode)
        assert output.decode().strip() == expected, (name, output)
    finally:
        launcher.terminate(process)
    print(expected, flush=True)
    if name == "c-init":
        assert marker.exists()
        marker.unlink()
print("compiled pre-exec qualification probes passed", flush=True)
