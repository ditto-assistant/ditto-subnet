from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ditto.api_server.coding_hosted_runtime import HostedRuntimeServices
from ditto.api_server.coding_hosted_runtime_config import postgres_config
from ditto.api_server.coding_hosted_runtime_io import (
    HostedRuntimeError,
    private_directory,
    read_json,
    read_private,
    run_private_process,
    write_private,
)
from ditto.api_server.coding_private_v2_retrieval import PrivateV2UnwrapRequest
from ditto.api_server.coding_private_v2_unwrap import ProcessPrivateV2Unwrapper


def helper(tmp_path, program):
    tmp_path.chmod(0o700)
    path = tmp_path / "helper"
    path.write_text(f"#!{sys.executable} -I\n" + program)
    path.chmod(0o700)
    return path


@pytest.mark.parametrize(
    "fault",
    [
        "symlink",
        "hardlink",
        "fifo",
        "mode",
        "directory",
        "oversize",
        "empty",
        "shared_parent",
    ],
)
def test_private_inputs_reject_unsafe_files(tmp_path, fault):
    tmp_path.chmod(0o700)
    path = tmp_path / "input"
    write_private(path, b"synthetic-private")
    if fault == "symlink":
        linked = tmp_path / "link"
        linked.symlink_to(path)
        path = linked
    elif fault == "hardlink":
        os.link(path, tmp_path / "link")
    elif fault == "fifo":
        path = tmp_path / "fifo"
        os.mkfifo(path, 0o600)
    elif fault == "mode":
        path.chmod(0o644)
    elif fault == "directory":
        path = tmp_path
    elif fault == "oversize":
        path.write_bytes(b"x" * 33)
    elif fault == "empty":
        path.write_bytes(b"")
    else:
        tmp_path.chmod(0o777)
    with pytest.raises((HostedRuntimeError, OSError)):
        read_private(path, 32)


def test_exclusive_files_keep_partial_tombstones_and_reject_duplicate_json(tmp_path):
    tmp_path.chmod(0o700)
    path = tmp_path / "consumed"
    write_private(path, b"")
    with pytest.raises(FileExistsError):
        write_private(path, b"rerun")
    assert path.read_bytes() == b""
    bad = tmp_path / "bad.json"
    write_private(bad, b'{"schema":1,"schema":2}')
    with pytest.raises(ValueError):
        read_json(bad)
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    private = shared / "private"
    private.mkdir(mode=0o700)
    with pytest.raises(HostedRuntimeError):
        private_directory(private)


async def test_helper_environment_has_no_ambient_credentials(tmp_path, monkeypatch):
    path = helper(tmp_path, "import json, os\nprint(json.dumps(dict(os.environ)))\n")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-marker")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret-marker")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret-marker")
    monkeypatch.setenv("PYTHONPATH", "secret-marker")
    body = await run_private_process((str(path),), root=tmp_path, timeout=10)
    assert json.loads(body) == {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


async def test_helper_output_and_time_are_bounded(tmp_path):
    path = helper(tmp_path, "print('x' * 5000)\n")
    with pytest.raises(HostedRuntimeError, match="exceeded"):
        await run_private_process(
            (str(path),), root=tmp_path, timeout=10, maximum_output=1024
        )
    path.write_text(f"#!{sys.executable} -I\nimport time\ntime.sleep(60)\n")
    with pytest.raises(TimeoutError):
        await run_private_process(
            (str(path),), root=tmp_path, timeout=0.1, shutdown_grace=0.1
        )


async def test_cancellation_gives_worker_time_to_drain(tmp_path):
    ready = tmp_path / "ready"
    clean = tmp_path / "clean"
    path = helper(
        tmp_path,
        f"""import signal, time
from pathlib import Path
def stop(*_args):
    time.sleep(0.05)
    Path({str(clean)!r}).write_text("drained")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
Path({str(ready)!r}).write_text("ready")
while True:
    time.sleep(1)
""",
    )
    task = asyncio.create_task(
        run_private_process((str(path),), root=tmp_path, timeout=30, shutdown_grace=2)
    )
    async with asyncio.timeout(10):
        while not ready.exists():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert clean.read_text() == "drained"


@pytest.mark.parametrize(
    "entry",
    [
        "PYTHONPATH=/untrusted",
        "POSTGRES_PASSWORD=other",
        "POSTGRES_PORT=bad",
        "POSTGRES_COMMAND_TIMEOUT=nan",
        "POSTGRES_POOL_MAX_SIZE=0",
    ],
)
def test_database_settings_are_explicit_and_allowlisted(entry):
    values = [
        "POSTGRES_HOST=localhost",
        "POSTGRES_PORT=5432",
        "POSTGRES_USER=synthetic",
        "POSTGRES_PASSWORD=synthetic",
        "POSTGRES_DB=synthetic",
    ]
    with pytest.raises(ValueError):
        postgres_config([*values, entry])


def test_command_requires_explicit_opt_in_and_redacts_arguments():
    for args in (
        [],
        ["--config", "/secret-marker"],
        ["--bad-secret-marker"],
        ["--private-shadow-once", "--config", "/secret-marker"],
    ):
        result = subprocess.run(
            [sys.executable, "-I", "-m", "ditto.coding_hosted_worker", *args],
            capture_output=True,
            timeout=30,
        )
        assert result.returncode != 0
        assert not result.stdout
        assert b"secret-marker" not in result.stderr


@pytest.mark.parametrize("fault", ["hash", "key", "eligibility", "schema"])
async def test_native_unwrap_rejects_wrong_response(tmp_path, fault):
    path = helper(
        tmp_path,
        f"""import base64, hashlib, json, sys
r = json.load(sys.stdin)
body = (json.dumps(r, sort_keys=True, separators=(",", ":")) + "\\n").encode()
v = {{"schema": "dittobench-coding-private-v2-unwrap-result-v1",
     "request_sha256": hashlib.sha256(body).hexdigest(),
     "data_key_b64": base64.b64encode(b"a" * 32).decode(),
     "weight_eligible": False}}
fault = {fault!r}
if fault == "hash": v["request_sha256"] = "f" * 64
if fault == "key": v["data_key_b64"] = "bad"
if fault == "eligibility": v["weight_eligible"] = 0
if fault == "schema": v["schema"] = "legacy"
print(json.dumps(v))
""",
    )
    request = PrivateV2UnwrapRequest(
        schema="dittobench-coding-private-v2-unwrap-v1",
        grant_id="10000000-0000-4000-8000-000000000001",
        evaluation_id="20000000-0000-4000-8000-000000000002",
        attempt_id="30000000-0000-4000-8000-000000000003",
        registration_sha256="a" * 64,
        transport_sha256="b" * 64,
        plaintext_sha256="c" * 64,
        ciphertext_sha256="d" * 64,
        wrapping_key_sha256="e" * 64,
        wrapped_data_key_b64="synthetic",
        aad_sha256="f" * 64,
        phase="authoring",
        role="issue",
        audience="platform-authoring",
        expires_at_unix=int(time.time()) + 60,
        frozen_patch_sha256=None,
    )
    client = ProcessPrivateV2Unwrapper(executable=path, work_root=tmp_path)
    with pytest.raises(ValueError):
        await client.unwrap(request)


async def test_service_cleanup_attempts_all_dependencies_after_failures():
    events = []

    async def server_close():
        events.append("server")

    async def control_close():
        events.append("control")

    async def reader_close(*_args):
        events.append("reader")

    def failed_spool():
        events.append("spool1")
        raise ValueError("synthetic")

    service = HostedRuntimeServices(
        reader=SimpleNamespace(__aexit__=reader_close),
        retriever=None,
        control=SimpleNamespace(shutdown=control_close),
        server=SimpleNamespace(shutdown=server_close),
        spools=[
            SimpleNamespace(close=failed_spool),
            SimpleNamespace(close=lambda: events.append("spool2")),
        ],
        socket=Path("/unused"),
        token=b"a" * 32,
    )
    with pytest.raises(HostedRuntimeError, match="unconfirmed"):
        await service.shutdown()
    assert events == ["server", "control", "spool1", "spool2", "reader"]


async def test_service_keeps_evidence_dependencies_when_control_is_not_drained():
    events = []

    async def server_close():
        events.append("server")

    async def control_close():
        events.append("control")
        raise ValueError("synthetic outstanding provider retention")

    async def reader_close(*_args):
        events.append("reader")

    service = HostedRuntimeServices(
        reader=SimpleNamespace(__aexit__=reader_close),
        retriever=None,
        control=SimpleNamespace(shutdown=control_close),
        server=SimpleNamespace(shutdown=server_close),
        spools=[SimpleNamespace(close=lambda: events.append("spool"))],
        socket=Path("/unused"),
        token=b"a" * 32,
    )
    with pytest.raises(HostedRuntimeError, match="drain is unconfirmed"):
        await service.shutdown()
    assert not service.drained
    assert events == ["server", "control"]
