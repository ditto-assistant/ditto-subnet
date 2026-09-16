"""Resource samplers against a real kernel and Docker daemon, small limits.

The collector's outside samplers (``sample_*``) and ``SystemHost`` readers run
unchanged against throwaway containers started here with the measured probe
runner's ``workload`` helper as entrypoint. Containers run as the test user's
own uid, so ``/proc/<pid>`` and ``/proc/<pid>/root`` are readable without root;
on the native host the collector reads them as root instead.

This is a mechanics test, not evidence: the containers are started by this test,
not by the production launch, and no record is written. It needs a rootful Linux
cgroup v2 Docker daemon, a built runner and a local image holding the runner's
shared libraries, which is never pulled (CI imports one from its own VM):

    DITTOBENCH_LOCAL_WORKLOAD_RUNNER=/abs/dittobench-coding-enforcement-probe
    DITTOBENCH_LOCAL_WORKLOAD_IMAGE=<local image with glibc>

Every container gets a unique name and is removed by name.
"""

import contextlib
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import pytest

from ditto.tests import test_coding_native_enforcement_evidence as base
from ditto.tests.test_coding_native_network_collector import COLLECTOR

EVIDENCE = base.EVIDENCE
RUNNER = os.environ.get("DITTOBENCH_LOCAL_WORKLOAD_RUNNER", "")
IMAGE = os.environ.get("DITTOBENCH_LOCAL_WORKLOAD_IMAGE", "")
MOUNT = "/opt/dittobench-probe/runner"

AVAILABLE = bool(
    RUNNER and IMAGE and Path("/sys/fs/cgroup/cgroup.controllers").exists()
)
# CI sets DITTOBENCH_REQUIRE_LOCAL_WORKLOADS=1, so these tests cannot skip there.
if os.environ.get("DITTOBENCH_REQUIRE_LOCAL_WORKLOADS") == "1":
    assert AVAILABLE, "local workload prerequisites are required"
pytestmark = pytest.mark.skipif(
    not AVAILABLE,
    reason="needs DITTOBENCH_LOCAL_WORKLOAD_RUNNER, _IMAGE and cgroup v2",
)


def expectation(probe_id: str) -> dict:
    kinds = base.catalog()["kinds"]["resource_enforcement"]["probes"]
    return next(item["expect"] for item in kinds if item["id"] == probe_id)


def matched(probe_id: str, observed: dict) -> bool:
    return EVIDENCE.evaluate(
        expectation(probe_id),
        observed,
        base.SUBORDINATE,
        set(base.catalog()["outcomes"]),
    )


def host():
    # The real readers; SystemHost's constructor only resolves the native
    # daemon user, which none of these readers use.
    return object.__new__(COLLECTOR.SystemHost)


class Workload:
    def __init__(self, mode: str, *docker: str, **flags) -> None:
        self.nonce = secrets.token_hex(8)
        self.name = f"dittobench-pr5-kernel-{self.nonce}"
        self.argv = COLLECTOR.workload_args(mode, self.nonce, **flags)
        self.docker = list(docker)

    def __enter__(self) -> "Workload":
        uid, gid = os.getuid(), os.getgid()
        subprocess.run(
            [
                "docker",
                "run",
                "--detach",
                "--pull",
                "never",
                "--name",
                self.name,
                "--user",
                f"{uid}:{gid}",
                "--init",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--network",
                "none",
                "--mount",
                f"type=bind,src={RUNNER},dst={MOUNT},readonly",
                "--entrypoint",
                MOUNT,
                *self.docker,
                IMAGE,
                *self.argv[:1],
                *self.argv[1:],
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        inspected = json.loads(
            subprocess.run(
                ["docker", "inspect", self.name], check=True, capture_output=True
            ).stdout
        )[0]
        self.id = inspected["Id"]
        self.init = inspected["State"]["Pid"]
        reader = host()
        self.cgroup = COLLECTOR.parse_cgroup(reader.proc(self.init, "cgroup"))
        expected = [MOUNT.encode(), *(item.encode() for item in self.argv)]
        deadline = time.monotonic() + 20
        while True:
            children = Path(f"/proc/{self.init}/task/{self.init}/children").read_text()
            for item in children.split():
                with contextlib.suppress(OSError):
                    if reader.proc(int(item), "cmdline").split(b"\0")[:-1] == expected:
                        self.pid = int(item)
                        return self
            assert time.monotonic() < deadline, "workload did not start"
            time.sleep(0.01)

    def __exit__(self, *_exc) -> None:
        subprocess.run(
            ["docker", "rm", "--force", self.name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


MIB = 1 << 20
READ_ONLY = ("--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=67108864")


def test_cgroup_files_equal_the_requested_limits():
    with Workload(
        "hold",
        *READ_ONLY,
        "--memory",
        "64m",
        "--memory-swap",
        "64m",
        "--cpus",
        "0.5",
        "--pids-limit",
        "64",
        hold_ms=3000,
    ) as run:
        values = COLLECTOR.sample_cgroup_limits(host(), run.cgroup)
    assert values == {
        "memory_max": 64 * MIB,
        "memory_swap_max": 0,
        "cpu_quota": 500,
        "pids_max": 64,
    }
    assert matched("harness.memory_swap_max", {"swap_max_bytes": 0})
    # Without --memory-swap Docker grants swap equal to memory: the finding
    # the hosted-v2 harness fixes by passing --memory-swap equal to --memory.
    with Workload("hold", "--memory", "64m", hold_ms=2000) as run:
        swap = COLLECTOR.sample_cgroup_limits(host(), run.cgroup)["memory_swap_max"]
    assert swap == 64 * MIB
    assert not matched("harness.memory_swap_max", {"swap_max_bytes": swap})


def test_memory_oom_is_seen_in_the_cgroup_at_the_limit():
    with Workload(
        "memory", "--memory", "64m", "--memory-swap", "64m", hold_ms=3000
    ) as run:
        observed = COLLECTOR.sample_memory_oom(host(), run.cgroup)
    observed["limit"] = 64 * MIB
    assert observed["enforced"] is True
    # memory.peak can pass memory.max by a forced charge of one page, which
    # tolerances v2 allows in the recorded host page size.
    page = observed["page_bytes"]
    assert page == os.sysconf("SC_PAGE_SIZE")
    assert 64 * MIB * 9 // 10 <= observed["measured"] <= 64 * MIB + page
    assert matched("harness.memory_oom", observed), observed


def test_cpu_quota_is_throttled_near_the_quota():
    with Workload(
        "cpu", "--cpus", "0.5", threads=COLLECTOR.cpu_burners(500), seconds=9
    ) as run:
        observed = COLLECTOR.sample_cpu_throttle(host(), run.cgroup)
    observed["limit"] = 500
    assert observed["enforced"] is True
    assert matched("harness.cpu_throttle", observed), observed


def test_pids_cap_is_reached_exactly():
    with Workload("pids", "--pids-limit", "64", hold_ms=3000) as run:
        observed = COLLECTOR.sample_pids_cap(host(), run.cgroup)
    observed["limit"] = 64
    assert matched("harness.pids_cap", observed), observed


def test_scratch_enospc_fills_the_tmpfs_from_outside():
    with Workload(
        "scratch", *READ_ONLY, "--memory", "256m", hold_ms=3000, dir="/tmp"
    ) as run:
        observed = COLLECTOR.sample_scratch_enospc(host(), run.pid)
    observed["limit"] = 64 * MIB
    assert matched("harness.scratch_enospc", observed), observed


def test_rootfs_is_read_only_only_when_mounted_read_only():
    with Workload("rootfs", *READ_ONLY, hold_ms=3000) as run:
        assert COLLECTOR.sample_rootfs(host(), run.pid, run.nonce) == {
            "outcome": "read_only"
        }
    with Workload("rootfs", hold_ms=3000) as run:
        observed = COLLECTOR.sample_rootfs(host(), run.pid, run.nonce)
    assert observed == {"outcome": "writable"}
    assert not matched("harness.rootfs_read_only", observed)


def test_nofile_cap_counts_the_full_descriptor_table():
    with Workload("nofile", "--ulimit", "nofile=1024:1024", hold_ms=3000) as run:
        observed = COLLECTOR.sample_nofile_cap(host(), run.pid, 1024)
    observed["limit"] = 1024
    assert observed["measured"] == 1024 and matched("harness.nofile_cap", observed)
    with Workload("nofile", "--ulimit", "nofile=2048:2048", hold_ms=3000) as run:
        loose = COLLECTOR.sample_nofile_cap(host(), run.pid, 1024)
    loose["limit"] = 1024
    assert not matched("harness.nofile_cap", loose)


def test_local_log_driver_retains_a_bounded_tail():
    limit = 8 * MIB
    with Workload(
        "log",
        "--log-driver",
        "local",
        "--log-opt",
        "max-size=8m",
        "--log-opt",
        "max-file=1",
        "--log-opt",
        "compress=false",
        hold_ms=3000,
        bytes=limit * 7 // 4,
    ) as run:
        emitted = COLLECTOR.sample_emitted_bytes(host(), run.pid, limit * 7 // 4)
        logs = subprocess.run(
            ["docker", "logs", run.name], check=True, capture_output=True
        )
    retained = len(logs.stdout) + len(logs.stderr)
    observed = {"enforced": emitted > limit, "limit": limit, "measured": retained}
    assert emitted == limit * 7 // 4
    assert matched("harness.log_bound", observed), observed


def test_elapsed_time_is_measured_from_the_kernel_start_time():
    with Workload("hang", seconds=600) as run:
        killer = subprocess.Popen(
            ["sh", "-c", f"sleep 3; docker kill {run.name} >/dev/null"]
        )
        uid = os.getuid()
        observed = COLLECTOR.sample_timeout(host(), run.cgroup, run.pid, 3000, uid)
        killer.wait()
    # The container was killed about three seconds after the workload started;
    # nothing of it survives.
    assert 2900 <= observed["elapsed_ms"] <= 4500, observed
    assert observed["live_processes"] == 0
