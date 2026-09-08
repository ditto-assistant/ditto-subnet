"""Approved finite shadow rollout on one pinned native host; never a scheduler."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import pwd
import re
import stat
import sys
import time
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse

from ditto.api_models.coding_bounded_rollout import BoundedRolloutApproval
from ditto.api_models.coding_hosted_grading import HostedTerminalIdentity
from ditto.api_server.coding_hosted_authoring_evidence import canonical, sha
from ditto.api_server.coding_hosted_installed_worker import (
    read_root_file,
    require_installed_worker,
)
from ditto.api_server.coding_hosted_runtime import _projection, run_loaded_runtime
from ditto.api_server.coding_hosted_runtime_config import (
    HostedRuntimeConfig,
    load_runtime_config,
)
from ditto.api_server.coding_hosted_runtime_io import (
    private_directory,
    read_json,
    read_private,
)
from ditto.api_server.coding_rollout_governor import (
    AttemptOutcome,
    RolloutError,
    RolloutState,
    govern,
)
from ditto.db.factory import create_db_engine, create_session_maker
from ditto.db.models import CodingHostedTerminalReservation

STATE = Path("/var/lib/ditto-coding-hosted/rollout")
HOME_DIR = Path("/var/lib/ditto-coding-hosted")
SOCKET = Path("/run/ditto-coding-hosted/docker.sock")
CGROUP = "0::/system.slice/ditto-coding-hosted-worker.service"


def require(condition: bool) -> None:
    if not condition:
        raise RolloutError("bounded rollout prerequisite rejected")


def host(approval: BoundedRolloutApproval) -> None:
    user = pwd.getpwnam("ditto-coding-hosted")
    require(os.getuid() == os.geteuid() == user.pw_uid >= 1000)
    require(os.getgid() == os.getegid() == user.pw_gid >= 1000)
    require(set(os.getgroups()) <= {user.pw_gid})
    require(
        platform.node() == "ditto-coding-hosted-v2" and platform.system() == "Linux"
    )
    require(Path("/proc/self/cgroup").read_text().strip() == CGROUP)
    require(
        Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        == str(approval.boot_id)
    )
    machine = Path("/etc/machine-id").read_bytes().strip()
    require(hashlib.sha256(machine).hexdigest() == approval.machine_id_sha256)
    for path in (STATE, HOME_DIR, HOME_DIR / "empty-client", SOCKET.parent):
        private_directory(path)
    require(not list((HOME_DIR / "empty-client").iterdir()))
    info = SOCKET.lstat()
    require(SOCKET.resolve() == SOCKET and stat.S_ISSOCK(info.st_mode))
    require(info.st_uid == user.pw_uid and stat.S_IMODE(info.st_mode) == 0o600)
    prefix = Path("/opt/ditto-coding-hosted") / approval.runtime_revision
    require(Path(sys.prefix) == prefix / "apps/platform/.venv")
    require(Path(__file__).resolve().is_relative_to(prefix))
    for path in (Path(__file__).resolve(), Path("/usr/bin/docker")):
        for part in (path, *path.parents):
            item = part.lstat()
            require(item.st_uid == 0 and not item.st_mode & 0o022)
    require(sha(Path(__file__).read_bytes()) == approval.controller_sha256)
    require_installed_worker(prefix / "bin/dittobench-coding-hosted-worker")
    receipt, _ = read_root_file(prefix / "bundle-receipt.json", 4096, 0o444)
    require(json.loads(receipt)["archive_sha256"] == approval.runtime_archive_sha256)


async def empty_daemon() -> None:
    env = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "DOCKER_HOST": f"unix://{SOCKET}",
        "DOCKER_CONFIG": str(HOME_DIR / "empty-client"),
    }
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/docker",
        "info",
        "--format",
        "{{json .}}",
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(30):
            assert process.stdout is not None
            body = bytearray()
            while chunk := await process.stdout.read(65536):
                body.extend(chunk)
                require(len(body) <= 1 << 20)
            require(await process.wait() == 0)
        info = json.loads(body)
        require(
            type(info) is dict
            and type(info.get("Containers")) is int
            and info["Containers"] == 0
        )
        require(info.get("DockerRootDir") == str(HOME_DIR / "docker"))
        require(info.get("OSType") == "linux" and info.get("Architecture") == "x86_64")
        require(
            info.get("CgroupVersion") == "2" and info.get("CgroupDriver") == "systemd"
        )
        require("io.heyditto.dittobench.isolated=true" in info.get("Labels", []))
        require(
            any(
                value in ("rootless", "name=rootless")
                or value.startswith("name=rootless,")
                for value in info.get("SecurityOptions", [])
                if isinstance(value, str)
            )
        )
        require(
            ["driver-type", "io.containerd.snapshotter.v1"]
            in info.get("DriverStatus", [])
        )
        require(
            all(
                info.get(field) is True
                for field in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit")
            )
        )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def configurations(
    approval: BoundedRolloutApproval, connectivity_sha: str
) -> list[HostedRuntimeConfig]:
    require(connectivity_sha == approval.connectivity_sha256)
    network = read_json(Path(approval.connectivity_file), 16384)
    encoded = json.dumps(network, sort_keys=True, separators=(",", ":")).encode()
    require(sha(encoded) == connectivity_sha)
    require(network["schema"] == "dittobench-coding-hosted-connectivity-v3")
    require(network["expires_at_unix"] >= approval.expires_at_unix + 3600)
    # The root ExecStartPre separately verifies/installs these same bytes.
    destinations = {
        (value["address"], value["port"]) for value in network["candidate_tcp"]
    }
    configs: list[HostedRuntimeConfig] = []
    root = Path("/opt/ditto-coding-hosted") / approval.runtime_revision
    for item in approval.attempts:
        config = load_runtime_config(
            Path(item.config_file), expected_sha256=item.config_sha256
        )
        wire = config.wire
        require(
            (
                wire.evaluation_id,
                wire.attempt_id,
                wire.worker_id,
                wire.assignment_sha256,
            )
            == (
                item.evaluation_id,
                item.attempt_id,
                item.worker_id,
                item.assignment_sha256,
            )
        )
        require(config.policy.digest() == item.policy_sha256)
        require(config.policy.max_cost_usd_micros == item.cost_ceiling_usd_micros)
        require(
            wire.worker_executable == str(root / "bin/dittobench-coding-hosted-worker")
        )
        require(wire.python_executable == str(root / "apps/platform/.venv/bin/python"))
        require(
            wire.host.docker_executable == "/usr/bin/docker"
            and wire.host.docker_socket == str(SOCKET)
        )
        address, port = wire.host.router_listen.rsplit(":", 1)
        require(f"{address}:{int(port)}" == wire.host.router_listen)
        proxy = urlparse(wire.host.egress_proxy)
        require(
            proxy.scheme == "http"
            and proxy.username is None
            and proxy.password is None
            and proxy.path in {"", "/"}
            and not proxy.query
            and not proxy.fragment
        )
        require(
            (address, int(port)) in destinations
            and (proxy.hostname, proxy.port) in destinations
        )
        if configs:
            require(config.postgres == configs[0].postgres)
        configs.append(config)
    require(
        len({Path(config.wire.runtime_root).resolve() for config in configs})
        == len(configs)
    )
    # The governor excludes overlapping use of a router; later assignments may
    # reuse it only after the previous runtime has fully returned from cleanup.
    return configs


async def run(
    path: Path, approval_sha: str, connectivity_sha: str, stop: asyncio.Event
) -> dict:
    raw = read_private(path, 65536)
    require(
        re.fullmatch(r"[0-9a-f]{64}", approval_sha) is not None
        and sha(raw) == approval_sha
    )
    approval = BoundedRolloutApproval.model_validate_json(raw)
    require(0 <= time.time() - approval.issued_at_unix <= 300)
    host(approval)
    state = RolloutState(STATE, approval_sha)
    engine = None
    try:
        configs = configurations(approval, connectivity_sha)
        engine = create_db_engine(configs[0].postgres)
        sessions = create_session_maker(engine)
        for item, config in zip(approval.attempts, configs, strict=True):
            require(not stop.is_set() and time.time() < approval.expires_at_unix)
            projection = await _projection(config, sessions)
            require(projection.authority.deadline_unix == item.deadline_unix)
        await empty_daemon()
        require(not stop.is_set())
        state.consume(
            canonical(
                {
                    "max_attempts": approval.max_attempts,
                    "max_parallel": approval.max_parallel,
                    "expires_at_unix": approval.expires_at_unix,
                    "reserved_cost_ceiling_usd_micros": sum(
                        item.cost_ceiling_usd_micros for item in approval.attempts
                    ),
                }
            )
        )

        async def execute(index: int) -> AttemptOutcome:
            host(approval)
            config = configs[index]
            progress = {
                "evaluation_id": str(config.wire.evaluation_id),
                "attempt_id": str(config.wire.attempt_id),
                "weight_eligible": False,
            }
            state.record(index, "launch", canonical(progress))
            try:
                digest = await run_loaded_runtime(config)
                async with sessions() as session:
                    row = await session.get(
                        CodingHostedTerminalReservation, config.wire.evaluation_id
                    )
                    require(row is not None)
                    assert row is not None
                    identity = HostedTerminalIdentity.model_validate_json(
                        canonical(row.identity)
                    )
                    require(identity.digest() == digest == row.identity_sha256)
                outcome = AttemptOutcome(digest, identity.outcome)
                state.record(
                    index, "finish", canonical({**progress, **asdict(outcome)})
                )
                return outcome
            except BaseException:
                state.record(
                    index,
                    "failure",
                    canonical({**progress, "reconciliation_required": True}),
                )
                raise

        outcomes = await govern(
            approval,
            execute,
            stop,
            resources=tuple(config.wire.host.router_listen for config in configs),
        )
        host(approval)
        await empty_daemon()
        require(not stop.is_set())
        await engine.dispose()
        engine = None
        await asyncio.sleep(0)
        require(
            not any(
                task is not asyncio.current_task() and not task.done()
                for task in asyncio.all_tasks()
            )
        )
        result = {
            "schema": "dittobench-coding-bounded-rollout-result-v2",
            "approval_sha256": approval_sha,
            "runtime_revision": approval.runtime_revision,
            "attempts": [
                {"evaluation_id": str(item.evaluation_id), **asdict(outcome)}
                for item, outcome in zip(approval.attempts, outcomes, strict=True)
            ],
            "reserved_cost_ceiling_usd_micros": sum(
                item.cost_ceiling_usd_micros for item in approval.attempts
            ),
            "shadow_only": True,
            "weight_eligible": False,
            "reexecuted": False,
        }
        state.finish(canonical(result, 65536))
        return result
    finally:
        try:
            if engine is not None:
                await engine.dispose()
        finally:
            state.close()
