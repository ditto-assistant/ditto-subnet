"""Native resource and cleanup collectors (B5 PR5) against a simulated host.

Nothing here touches a host or daemon. ``ResourceFakeHost`` models the resource
agent, the containers the production launch paths start, their cgroup v2 files
and ``/proc`` of each workload, from recorded kernel semantics (see
``test_coding_native_resource_kernel.py`` for the same samplers on a real
kernel). A correct host yields a record the offline verifier accepts; each knob
turns one outside observation or binding and must either make the verifier
refuse the record or refuse collection outright.
"""

import copy
import functools
import json
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ditto.tests import test_coding_native_enforcement_evidence as base
from ditto.tests.test_coding_native_network_collector import (
    COLLECTOR,
    COLLECTOR_PATH,
    ROOT,
    RUNNER,
    RUNNER_SHA256,
)

EVIDENCE = base.EVIDENCE
T0 = base.T0
UID = GID = 1001
AGENT_PID = 5001
CONFIG_PATH = Path("/etc/ditto-coding-native-collector/resource.json")
PROFILE_DIR = Path("/etc/ditto-coding-native-collector")
SUBUID = 100000
DOC = ROOT / "infra/docs/coding-native-enforcement-evidence-v1.md"


class Scenario:
    """Knobs a refusal test turns; the defaults describe a correct host."""

    def __init__(self) -> None:
        self.inspect: Any = None  # callable(inspected, run) mutating the inspection
        self.cgroup: dict[tuple[str, str], bytes | None] = {}  # (class, file)
        self.cgroup_path: Any = None
        self.tampered_exe: set[str] = set()
        self.cmdline_nonce: str | None = None
        self.workload_uid: dict[str, int] = {}
        self.agent_digest = RUNNER_SHA256
        self.agent_inputs: dict | None = None
        self.agent_uid = UID
        self.installed_runner = RUNNER_SHA256
        self.mount_read_only = True
        self.rootfs_created = False
        self.fds: dict[str, int] = {}
        self.nofile_soft = 1024
        self.statvfs_free = 0
        self.wchar: dict[str, int] = {}
        self.docker_logs: int | None = None
        self.receipt: dict[str, dict] = {}  # (class) -> receipt overrides
        self.run_failed: set[str] = set()
        self.container_remains: set[str] = set()
        self.timeout_extra_ms = 150
        self.timeout_live = 0
        self.stray_container = False
        self.residue_process = False
        self.daemon_info_after: dict | None = None
        self.reboot = False
        self.preflight_age = 60
        self.leftover_after: dict[str, dict[str, int]] = {}
        self.agent_survives_sigterm = False
        self.page_bytes = 4096
        # cleanup_recovery launch journal and rerun knobs
        self.journal_tamper: Any = None  # callable(bytes) -> bytes
        self.journal_not_private = False
        self.journal_omits_container = False
        self.kill_leaves_nothing = False
        self.reconciler_removes_decoy = False
        self.reconciler_skips_sentinel = False
        self.rerun_accepted = False
        self.marker_missing = False


@functools.cache
def resource_limits(container: str, language: str) -> dict[str, int]:
    def limit(source: str) -> int:
        return base.expected_limit(source, container, language)

    return {
        "memory": limit("memory_limit_bytes"),
        "cpu": limit("cpu_quota_millis"),
        "pids": limit("pids_limit"),
        "scratch": limit("scratch_limit_bytes"),
    }


class FakeAgent:
    def __init__(self, host: "ResourceFakeHost", arguments: list[str]) -> None:
        self.host, self.arguments = host, arguments
        self.closed = False
        self.counter = 0

    def read(self, timeout: float) -> dict:
        assert timeout > 0
        raise COLLECTOR.Refusal("agent did not answer in time")

    def request(self, value: dict, timeout: float) -> dict:
        assert timeout > 0
        host, scenario = self.host, self.host.scenario
        assert not host.agent_terminated, "request to a terminated agent"
        assert not host.agent_killed, "request to a killed agent"
        host.requests.append(value)
        op = value["op"]
        answer: dict[str, Any] = {"schema": COLLECTOR.RESOURCE_AGENT_SCHEMA, "op": op}
        if op == "hello":
            answer.update(
                pid=AGENT_PID,
                uid=scenario.agent_uid,
                gid=GID,
                probe_runner_binary_sha256=scenario.agent_digest,
                inputs=scenario.agent_inputs or host.world.inputs(),
            )
        elif op == "start":
            answer.update(host.start(value, f"r{self.counter}"))
            self.counter += 1
        elif op == "wait":
            answer.update(host.wait(value["run"]))
        elif op == "exit":
            host.agent_exited = True
        return answer

    def close(self) -> None:
        self.closed = True
        # End of input ends the agent.
        self.host.agent_exited = True


class RefusingAgent:
    """An agent started on a consumed attempt: one line, then it exits."""

    def __init__(self) -> None:
        self.closed = False

    def read(self, timeout: float) -> dict:
        assert timeout > 0
        return {
            "schema": COLLECTOR.RESOURCE_AGENT_SCHEMA,
            "op": "attempt",
            "error": "probe: attempt state consumed",
        }

    def request(self, value: dict, timeout: float) -> dict:
        assert value and timeout > 0
        raise COLLECTOR.Refusal("agent closed its session")

    def close(self) -> None:
        self.closed = True


def journal_line(run: str, containers: list[str], networks: list[str]) -> bytes:
    entry = {
        "schema": COLLECTOR.JOURNAL_ENTRY_SCHEMA,
        "attempt": "native-enforcement-0123456789abcdef",
        "worker": "native-enforcement-agent",
        "run": run,
        "containers": containers,
        "networks": networks,
    }
    return json.dumps(entry, separators=(",", ":")).encode() + b"\n"


class ResourceFakeHost:
    def __init__(self, world: "ResourceWorld", scenario: Scenario) -> None:
        self.world, self.scenario = world, scenario
        self.mono = 0.0
        self.base = T0 + scenario.preflight_age
        self.requests: list[dict] = []
        self.runs: dict[str, dict] = {}
        self.pids: dict[int, tuple[dict, str]] = {}
        self.containers: dict[str, dict] = {}
        self.networks: set[str] = set()
        self.next_pid = 20000
        self.info_calls = 0
        self.work_dir: dict[str, bytes] | None = None
        self.work_dir_removed = False
        self.agent_exited = False
        self.agent_terminated = False
        self.agent_killed = False
        self.journaled = False
        self.journal: list[bytes] = []
        self.consumed: set[str] = set()
        self.private_dirs: list[Path] = []
        self.one_shots: list[tuple[str, list[str]]] = []
        self.decoys: set[str] = set()
        self.commands: list[tuple] = []
        if scenario.stray_container:
            self.containers["f" * 64] = {"run": None}

    # clocks ------------------------------------------------------------

    def now(self) -> int:
        return self.base + int(self.mono)

    def monotonic(self) -> float:
        return self.mono

    def sleep(self, seconds: float) -> None:
        self.mono += seconds

    def boottime_ns(self) -> int:
        return int(self.mono * 1e9) + 10**12

    def clock_ticks(self) -> int:
        return 100

    def page_size(self) -> int:
        return self.scenario.page_bytes

    # identity ------------------------------------------------------------

    def euid(self) -> int:
        return 0

    def hostname(self) -> str:
        return COLLECTOR.HOSTNAME

    def machine_id(self) -> bytes:
        return b"synthetic-machine-id"

    def boot_id(self) -> str:
        if self.scenario.reboot and self.mono > 0:
            return base.OTHER_BOOT
        return base.BOOT

    def kernel(self) -> str:
        return base.KERNEL

    def identity(self) -> tuple[int, int]:
        return UID, GID

    def read(self, path: Path) -> bytes:
        files = {
            CONFIG_PATH: json.dumps(self.world.config).encode(),
            Path("/srv/native-release/release.json"): base.RELEASE_INDEX_RAW,
            Path("/etc/subuid"): b"ditto-coding-hosted:100000:65536\n",
            Path("/etc/subgid"): b"ditto-coding-hosted:100000:65536\n",
            **{
                PROFILE_DIR / file: raw
                for file, raw in self.world.profile_bytes().items()
            },
        }
        return files[path]

    def file_sha256(self, path: Path) -> str:
        assert str(path) == RUNNER
        return self.scenario.installed_runner

    # runs ------------------------------------------------------------------

    def start(self, value: dict, run_id: str) -> dict:
        container, language = value["class"], value["language"]
        argv = ["workload", *value["workload"]]
        assert value["repository"] == f"coding-runtime.invalid/{language}/runtime"
        mode = argv[1]
        nonce = argv[argv.index("--nonce") + 1]
        flags = dict(zip(argv[4::2], argv[5::2], strict=False))
        container_id = f"{self.next_pid:064x}"
        init = self.next_pid
        self.next_pid += 10
        timeout_ms = value.get("timeout_ms")
        if value.get("test_group"):
            timeout_ms = base.GROUP_TIMEOUTS_MS[value["test_group"]]
        run = {
            "id": run_id,
            "class": container,
            "language": language,
            "argv": argv,
            "mode": mode,
            "nonce": nonce,
            "flags": flags,
            "container_id": container_id,
            "init": init,
            "supervisor": init + 1,
            "workload": init + 2,
            "child": init + 3,
            "started": self.mono,
            "timeout_ms": timeout_ms,
            "fail_start": value.get("fail_start", False),
            "instance": "coding-executor-" + f"{init:032x}",
            "network": f"ditto-job-{init:016x}",
        }
        self.runs[run_id] = run
        for role in ("init", "supervisor", "workload", "child"):
            self.pids[run[role]] = (run, role)
        answer: dict[str, Any] = {"run": run_id}
        if container == "harness":
            answer["container_name"] = (
                f"dittobench-{init:016x}" if run["fail_start"] else container_id
            )
            self.networks.add(run["network"])
        else:
            answer["executor_instance"] = run["instance"]
        if self.journaled:
            run["sentinel"] = f"ditto-job-sentinel-{init:016x}"
            self.networks.add(run["sentinel"])
            self.journal.append(
                journal_line(f"sentinel-{init:016x}", [], [run["sentinel"]])
            )
            name = self.container_name(run)
            networks = [run["network"]] if container == "harness" else []
            if not self.scenario.journal_omits_container:
                self.journal.append(journal_line(name, [name], networks))
        if not run["fail_start"]:
            self.containers[container_id] = {"run": run_id}
        return answer

    def container_name(self, run: dict) -> str:
        if run["class"] == "harness":
            return f"dittobench-{run['init']:016x}"
        return f"dittobench-coding-{run['init']:012x}-{run['init']:012x}"

    def reconcile_journal(
        self, *, decoys: bool = False, skip_sentinel: bool = False
    ) -> None:
        for line in self.journal:
            entry = json.loads(line)
            for name in entry["containers"]:
                for container_id, item in list(self.containers.items()):
                    run = self.runs.get(item["run"]) if item["run"] else None
                    if run and self.container_name(run) == name:
                        del self.containers[container_id]
            for name in entry["networks"]:
                if name.startswith("ditto-job-sentinel-") and skip_sentinel:
                    continue
                self.networks.discard(name)
        if decoys:
            for name in list(self.networks):
                if name.startswith("ditto-job-sentinel-"):
                    self.networks.discard(name)
        self.journal = []

    def run_of_container(self, target: str) -> dict | None:
        entry = self.containers.get(target)
        return self.runs.get(entry["run"]) if entry and entry["run"] else None

    def run_of_pid(self, pid: int) -> tuple[dict | None, str]:
        run, role = self.pids.get(pid, (None, ""))
        if run is None or run["container_id"] not in self.containers:
            return None, ""
        return run, role

    def duration(self, run: dict) -> float:
        flags = run["flags"]
        if run["mode"] == "hang":
            return run["timeout_ms"] / 1000 + self.scenario.timeout_extra_ms / 1000
        if run["mode"] == "cpu":
            return float(flags["--seconds"])
        return int(flags.get("--hold-ms", "1000")) / 1000

    def wait(self, run_id: str) -> dict:
        run = self.runs[run_id]
        end = run["started"] + self.duration(run)
        if self.mono < end:
            self.mono = end
        container = run["class"]
        if container not in self.scenario.container_remains:
            self.containers.pop(run["container_id"], None)
            self.networks.discard(run["network"])
            if self.journaled:
                self.reconcile_journal()
        answer: dict[str, Any] = {"run": run_id, "done": True}
        leftovers = self.scenario.leftover_after.get(run["mode"])
        if leftovers:
            self.scenario.cleanup_leftover = leftovers  # type: ignore[attr-defined]
        if run["fail_start"] or container in self.scenario.run_failed:
            answer["run_failed"] = True
            return answer
        receipt = {"return_code": 0, "retained_output_bytes": 0, "completed": True}
        if run["mode"] == "hang":
            receipt = {
                "return_code": 124,
                "retained_output_bytes": 0,
                "timed_out": True,
            }
        if run["mode"] == "log" and container == "executor_authoring":
            receipt["retained_output_bytes"] = 24576
        receipt.update(self.scenario.receipt.get(container, {}))
        answer.update(receipt)
        return answer

    # docker ------------------------------------------------------------------

    def docker(self, *args: str) -> bytes:
        self.commands.append(("docker", *args))
        if args == ("ps", "--all", "--format", "{{.Names}}"):
            names = []
            for item in self.containers.values():
                run = self.runs.get(item["run"]) if item["run"] else None
                if run is not None:
                    names.append(self.container_name(run))
            return "\n".join(names).encode()
        if args == ("network", "ls", "--format", "{{.Name}}"):
            return "\n".join(sorted(self.networks)).encode()
        if args[:2] == ("network", "create"):
            self.networks.add(args[-1])
            self.decoys.add(args[-1])
            return b"id\n"
        if args[:2] == ("network", "rm"):
            self.networks.discard(args[-1])
            return args[-1].encode()
        if args[0] == "info":
            self.info_calls += 1
            info = base.DAEMON_IDENTITY_VECTOR["info"]
            if self.info_calls > 1 and self.scenario.daemon_info_after is not None:
                info = self.scenario.daemon_info_after
            return json.dumps(info).encode()
        if args[0] == "ps":
            filters = [item for item in args if "=" in item]
            ids = []
            for container_id, entry in self.containers.items():
                run = self.runs.get(entry["run"]) if entry["run"] else None
                if filters:
                    key, _, wanted = filters[0].partition("=")
                    if run is None:
                        continue
                    if key == "label" and wanted != (
                        f"{COLLECTOR.EXECUTOR_LABEL}={run['instance']}"
                    ):
                        continue
                    if key in ("id", "name") and wanted not in (
                        container_id,
                        f"dittobench-{run['init']:016x}",
                    ):
                        continue
                ids.append(container_id)
            return "\n".join(ids).encode()
        if args[:2] == ("network", "ls"):
            return "\n".join(sorted(self.networks)).encode()
        if args[:2] == ("volume", "ls"):
            return b""
        if args[0] == "inspect":
            run = self.run_of_container(args[1])
            if run is None:
                raise COLLECTOR.Refusal("docker inspect failed")
            return json.dumps([self.inspection(run)]).encode()
        raise AssertionError(args)

    def inspection(self, run: dict) -> dict:
        container, language = run["class"], run["language"]
        limits = resource_limits(container, language)
        harness = container == "harness"
        name = (
            f"dittobench-{run['init']:016x}"
            if harness
            else f"dittobench-coding-{run['init']:012x}-{run['init']:012x}"
        )
        value = {
            "Id": run["container_id"],
            "Name": "/" + name,
            "Image": "sha256:" + base.digest(language + "-config"),
            "State": {"Running": True, "Pid": run["init"]},
            "Config": {
                "User": "65532:65532" if harness else "0:0",
                "Labels": {COLLECTOR.RUN_LABEL: name}
                if harness
                else {
                    COLLECTOR.RUN_LABEL: name,
                    COLLECTOR.EXECUTOR_LABEL: run["instance"],
                },
                "Entrypoint": [COLLECTOR.CONTAINER_RUNNER]
                if harness
                else ["/usr/local/bin/dittobench-coding-supervisor"],
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Memory": limits["memory"],
                "MemorySwap": limits["memory"],
                "NanoCpus": limits["cpu"] * 1_000_000,
                "PidsLimit": limits["pids"],
                "Tmpfs": {"/tmp": f"rw,noexec,nosuid,nodev,size={limits['scratch']}"},
                "Privileged": False,
                "CapDrop": ["CAP_ALL"] if harness else ["ALL"],
                "LogConfig": {"Type": "local" if harness else "none"},
                "NetworkMode": run["network"] if harness else "none",
            },
            "Mounts": [{"Destination": COLLECTOR.CONTAINER_RUNNER, "RW": False}]
            if harness
            else [
                {"Destination": "/workspace", "RW": False},
                {"Destination": "/run/dittobench-control", "RW": True},
            ],
        }
        if self.scenario.inspect is not None:
            self.scenario.inspect(value, run)
        return value

    def docker_output_bytes(self, *args: str) -> int:
        assert args[0] == "logs" and self.run_of_container(args[1]) is not None
        if self.scenario.docker_logs is not None:
            return self.scenario.docker_logs
        return 8388608 * 3 // 4

    # /proc -----------------------------------------------------------------

    def exe_sha256(self, pid: int) -> str:
        if pid == AGENT_PID:
            return RUNNER_SHA256
        run, _ = self.run_of_pid(pid)
        assert run is not None, pid
        if run["class"] in self.scenario.tampered_exe:
            return base.digest("tampered runner")
        return RUNNER_SHA256

    def children(self, pid: int) -> list[int]:
        run, role = self.run_of_pid(pid)
        if run is None:
            return []
        if role == "init":
            return [run["workload"] if run["class"] == "harness" else run["supervisor"]]
        if role == "supervisor":
            return [run["workload"]]
        if role == "workload" and run["mode"] == "hang":
            return [run["child"]]
        return []

    def proc(self, pid: int, name: str) -> bytes:
        if pid == AGENT_PID:
            if name == "cgroup":
                return (
                    b"0::/user.slice/user-1001.slice/user@1001.service/app.slice/"
                    b"ditto-native-resource-agent.service\n"
                )
            raise AssertionError(name)
        run, role = self.run_of_pid(pid)
        if run is None:
            raise FileNotFoundError(pid)
        scenario, container = self.scenario, run["class"]
        if name == "cgroup":
            path = (
                f"user.slice/user-{UID}.slice/user@{UID}.service/user.slice/"
                f"docker-{run['container_id']}.scope"
            )
            if scenario.cgroup_path is not None:
                path = scenario.cgroup_path(path)
            return f"0::/{path}\n".encode()
        if name == "cmdline":
            runner = (
                COLLECTOR.CONTAINER_RUNNER
                if container == "harness"
                else COLLECTOR.EXECUTOR_RUNNER
            )
            argv = list(run["argv"])
            if scenario.cmdline_nonce is not None:
                argv[argv.index("--nonce") + 1] = scenario.cmdline_nonce
            if role != "workload":
                return b"/usr/local/bin/dittobench-coding-supervisor\0"
            return b"\0".join(item.encode() for item in [runner, *argv]) + b"\0"
        if name == "status":
            uid = scenario.workload_uid.get(
                container, COLLECTOR.CANDIDATE_UIDS[container]
            )
            host = SUBUID + uid - 1
            ids = "\t".join([str(host)] * 4)
            return f"Uid:\t{ids}\nGid:\t{ids}\n".encode()
        if name == "stat":
            start_ticks = int(run["started"] * 100) + 100_000
            end = run["started"] + self.duration(run)
            state = "Z" if self.mono >= end else "S"
            if role == "child" and scenario.timeout_live:
                state = "S"
            return f"{pid} (runner) {state} ".encode() + b" ".join(
                [b"0"] * 18 + [str(start_ticks).encode(), b"0"]
            )
        if name == "mountinfo":
            options = "ro,relatime" if scenario.mount_read_only else "rw,relatime"
            return f"877 831 0:57 / / {options} - overlay overlay rw\n".encode()
        if name == "limits":
            soft = scenario.nofile_soft
            return (
                "Limit Soft Limit Hard Limit Units\n"
                f"Max open files            {soft}                 {soft}"
                "                 files\n"
            ).encode()
        if name == "io":
            wchar = scenario.wchar.get(
                container,
                8388608 * 7 // 4 if container == "harness" else 4 * 24576,
            )
            return f"rchar: 10\nwchar: {wchar}\nsyscr: 1\n".encode()
        raise AssertionError((pid, name))

    def cgroup_read(self, cgroup: str, name: str) -> bytes | None:
        match = re.search(r"docker-([0-9a-f]{64})\.scope$", cgroup)
        assert match, cgroup
        run = self.run_of_container(match.group(1))
        if run is None:
            return None
        container = run["class"]
        if (container, name) in self.scenario.cgroup:
            return self.scenario.cgroup[(container, name)]
        limits = resource_limits(container, run["language"])
        elapsed = self.mono - run["started"]
        values = {
            "memory.max": str(limits["memory"]),
            "memory.swap.max": "0",
            "cpu.max": f"{limits['cpu'] * 100} 100000",
            "pids.max": str(limits["pids"]),
            "memory.events": (
                "low 0\nhigh 0\nmax 12\noom 1\n"
                f"oom_kill {int(run['mode'] == 'memory')}\n"
            ),
            "memory.peak": str(limits["memory"] * 99 // 100),
            "pids.events": f"max {int(run['mode'] == 'pids')}\n",
            "pids.current": str(limits["pids"]),
            "cpu.stat": (
                f"usage_usec {int(elapsed * limits['cpu'] * 1000)}\n"
                f"user_usec 0\nsystem_usec 0\nnr_periods {int(elapsed * 10)}\n"
                f"nr_throttled {int(elapsed * 10)}\nthrottled_usec 0\n"
            ),
            "cgroup.procs": (
                f"{run['init']}\n{run['supervisor']}\n"
                + "".join(
                    f"{run['child']}\n" for _ in range(self.scenario.timeout_live)
                )
            ),
        }
        return values[name].encode()

    def fd_count(self, pid: int) -> int:
        run, _ = self.run_of_pid(pid)
        assert run is not None
        return self.scenario.fds.get(run["class"], 1024)

    def root_statvfs(self, pid: int, path: str) -> tuple[int, int, int, int]:
        run, _ = self.run_of_pid(pid)
        assert run is not None and path == "/tmp"
        scratch = resource_limits(run["class"], run["language"])["scratch"]
        free = self.scenario.statvfs_free
        return scratch // 4096, free, free, 4096

    def root_lexists(self, pid: int, path: str) -> bool:
        run, _ = self.run_of_pid(pid)
        assert run is not None
        assert path == f"/.dittobench-rootfs-probe-{run['nonce']}"
        return self.scenario.rootfs_created

    # units, sessions, residue ------------------------------------------------

    def systemctl(self, *args: str) -> tuple[int, bytes]:
        self.commands.append(("systemctl", *args))
        if args[0] == "--user":
            return 0, f"MainPID={AGENT_PID}\n".encode()
        if args[0] == "show":
            wanted = [item.split("=", 1)[1] for item in args[2:]]
            return 0, "".join(f"{key}=inactive\n" for key in wanted).encode()
        if args[0] == "list-units":
            return 0, b""
        raise AssertionError(args)

    def socket_present(self, directory: Path) -> bool:
        assert directory == COLLECTOR.CUSTODY_RUN
        return False

    def cgroup_procs(self, cgroup: str) -> list[int]:
        if cgroup.endswith(".service") and "ditto-native-resource-agent" in cgroup:
            alive = not (
                self.agent_exited or self.agent_terminated or self.agent_killed
            )
            if self.agent_terminated and self.scenario.agent_survives_sigterm:
                alive = True
            return [AGENT_PID] if alive else []
        if self.scenario.residue_process and cgroup == COLLECTOR.WORKER_CGROUP:
            return [4242]
        return []

    def prepare_work_dir(self, uid: int, gid: int, files: dict[str, bytes]) -> None:
        assert (uid, gid) == (UID, GID)
        self.work_dir = dict(files)

    def remove_work_dir(self) -> None:
        self.work_dir_removed = True

    def resource_session(self, unit: str, arguments: list[str]):
        self.agent_unit, self.agent_arguments = unit, arguments
        self.agent_exited = self.agent_terminated = self.agent_killed = False
        self.journaled = "--launch-journal" in arguments
        if "--attempt-state" in arguments:
            state = arguments[arguments.index("--attempt-state") + 1]
            if state in self.consumed and not self.scenario.rerun_accepted:
                self.agent_exited = True
                return RefusingAgent()
            if not self.scenario.marker_missing:
                self.consumed.add(state)
        self.agent = FakeAgent(self, arguments)
        return self.agent

    def terminate(self, pid: int) -> None:
        assert pid == AGENT_PID
        self.agent_terminated = True
        if not self.scenario.agent_survives_sigterm:
            for run in self.runs.values():
                self.containers.pop(run["container_id"], None)
            if self.journaled:
                self.reconcile_journal()

    def kill(self, pid: int) -> None:
        assert pid == AGENT_PID
        self.agent_killed = True
        if self.scenario.kill_leaves_nothing:
            self.containers.clear()
            self.networks = {
                name
                for name in self.networks
                if not name.startswith("ditto-job-sentinel-") or name in self.decoys
            }

    def make_private_dir(self, path: Path, uid: int, gid: int) -> None:
        assert (uid, gid) == (UID, GID) and path.parent == COLLECTOR.WORK_DIR
        assert path not in self.private_dirs
        self.private_dirs.append(path)

    def read_private_file(self, path: Path, uid: int, maximum: int) -> bytes | None:
        assert uid == UID and maximum > 0
        if path == COLLECTOR.JOURNAL_DIR / COLLECTOR.JOURNAL_FILE:
            if self.scenario.journal_not_private:
                raise COLLECTOR.Refusal("private file is not the daemon user's")
            if not self.journal:
                return None
            raw = b"".join(self.journal)
            if self.scenario.journal_tamper is not None:
                raw = self.scenario.journal_tamper(raw)
            return raw
        assert path.name == "consumed"
        if str(path.parent) in self.consumed:
            return COLLECTOR.CONSUMED_MARKER
        return None

    def one_shot(self, unit: str, arguments: list[str]) -> tuple[int, bytes]:
        self.one_shots.append((unit, arguments))
        assert arguments[:2] == [RUNNER, "reconcile-launch-journal"]
        assert arguments[2:] == [
            "--launch-journal",
            str(COLLECTOR.JOURNAL_DIR),
            "--docker-executable",
            "/usr/bin/docker",
            "--docker-socket",
            COLLECTOR.SOCKET,
        ]
        entries = len(self.journal)
        self.reconcile_journal(
            decoys=self.scenario.reconciler_removes_decoy,
            skip_sentinel=self.scenario.reconciler_skips_sentinel,
        )
        report = {"schema": COLLECTOR.RECONCILE_SCHEMA, "entries": entries}
        return 0, json.dumps(report).encode()

    def subordinate_processes(self, start: int, count: int) -> int:
        assert (start, count) == (SUBUID, 65536)
        leftover = getattr(self.scenario, "cleanup_leftover", {})
        return leftover.get("processes", 0)

    def docker_scope_processes(self, uid: int) -> int:
        assert uid == UID
        return 0


class ResourceWorld:
    def __init__(self, tmp_path: Path) -> None:
        self.world = base.World(tmp_path)
        targets = ROOT / COLLECTOR.TARGETS_FIXTURE
        destination = self.world.checkout / COLLECTOR.TARGETS_FIXTURE
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(targets.read_bytes())
        for path in (destination.parent, destination):
            path.chmod(0o755 if path.is_dir() else 0o644)
        self.config = {
            "schema": COLLECTOR.RESOURCE_CONFIG_SCHEMA,
            "source_revision": base.REVISION,
            "release_directory": "/srv/native-release",
            "release_manifest_sha256": base.MANIFEST,
            "machine_id_sha256": base.MACHINE,
            "boot_id": base.BOOT,
            "store": str(self.world.store),
            "pre_collection_preflight_sha256": self.world.pre_sha,
            "execution_profile": str(PROFILE_DIR / "execution-profile.json"),
            "grading_profile": str(PROFILE_DIR / "grading-profile.json"),
            "enforcement_images": str(PROFILE_DIR / "enforcement-images.json"),
            "seccomp_profile": "",
            "apparmor_profile": "",
            "shadow_only": True,
            "weight_eligible": False,
        }

    def profile_bytes(self) -> dict[str, bytes]:
        paths = self.world.profile_paths
        return {
            "execution-profile.json": paths["execution_profile_sha256"].read_bytes(),
            "grading-profile.json": paths["grading_profile_sha256"].read_bytes(),
            "enforcement-images.json": paths["enforcement_images_sha256"].read_bytes(),
        }

    def inputs(self) -> dict[str, str]:
        return {
            name: base.INPUTS[name]
            for name in (
                "enforcement_images_sha256",
                "execution_profile_sha256",
                "grading_profile_sha256",
            )
        }

    def host(self, scenario: Scenario | None = None) -> ResourceFakeHost:
        return ResourceFakeHost(self, scenario or Scenario())

    def collector(self, host: ResourceFakeHost, kind: str = "resource"):
        config = COLLECTOR.parse_resource_config(json.dumps(self.config).encode())
        factory = (
            COLLECTOR.ResourceCollector
            if kind == "resource"
            else COLLECTOR.CleanupCollector
        )
        return factory(host, config, checkout=self.world.checkout)

    def collect(
        self, scenario: Scenario | None = None
    ) -> tuple[dict, ResourceFakeHost]:
        host = self.host(scenario)
        return self.collector(host).collect(), host

    def verify(self, record: dict) -> str | None:
        # The post-collection preflight is taken after the record.
        self.world.post_value = base.preflight_value(
            self.world.preflight_tools, record["completed_at_unix"] + 30
        )
        return self.world.verify_record(record)


@pytest.fixture
def rw(tmp_path):
    # main() installs a SIGTERM handler for collection; restore the default.
    previous = signal.getsignal(signal.SIGTERM)
    yield ResourceWorld(tmp_path)
    signal.signal(signal.SIGTERM, previous)


def observed(record: dict, probe_id: str, language: str = "python") -> dict:
    return base.find_probe(record, probe_id, language)["observed"]


# ---------------------------------------------------------------------------
# Accept path


def test_collected_resource_record_verifies_offline(rw):
    record, host = rw.collect()
    assert rw.verify(record) is None
    assert all(
        item["matched"] for phase in record["phases"] for item in phase["probes"]
    )
    assert record["kind"] == "resource_enforcement" and record["endpoints"] == []
    assert record["inputs"] == rw.inputs()
    assert record["tools"]["probe_runner_binary_sha256"] == RUNNER_SHA256
    assert record["preconditions"] == EVIDENCE.PRECONDITIONS
    assert record["residue"] == EVIDENCE.RESIDUE
    # Every probe observed for every language image, through every class.
    starts = [item for item in host.requests if item["op"] == "start"]
    assert {(item["class"], item["language"]) for item in starts} == {
        (container, language)
        for container in COLLECTOR.CLASSES
        for language in EVIDENCE.LANGUAGES
    }
    # Grading supervisor timeouts use the approved group timeout, never a
    # collector value.
    timeouts = [item for item in starts if item.get("test_group")]
    assert sorted({item["test_group"] for item in timeouts}) == ["hidden", "visible"]
    assert all("timeout_ms" not in item for item in timeouts)
    # The agent received exactly the approved documents, and was measured.
    assert host.work_dir == rw.profile_bytes()
    assert host.agent_arguments[:2] == [RUNNER, "resource-agent"]
    assert "--runner" in host.agent_arguments and host.work_dir_removed
    assert host.agent_exited and host.containers == {} and host.networks == set()
    rust = observed(record, "executor_grading.scratch_enospc", "rust")
    assert rust["limit"] == base.expected_limit(
        "scratch_limit_bytes", "executor_grading", "rust"
    )
    assert observed(record, "executor_grading.log_bound") == {
        "emitted_bytes": 4 * 24576,
        "limit": 24576,
        "retained_bytes": 0,
    }
    hidden = observed(record, "executor_grading.supervisor_timeout.hidden")
    assert hidden["exit_code"] == 124 and hidden["live_processes"] == 0
    assert 300000 <= hidden["elapsed_ms"] <= 300000 * 11 // 10
    assert not [key for key in base.keys_of(record) if base.FORBIDDEN_KEY.search(key)]


def test_main_retains_the_resource_record_and_prints_no_approval(rw, capsys):
    host = rw.host()
    code = COLLECTOR.main(
        [
            "resource",
            "--config",
            str(CONFIG_PATH),
            "--confirm",
            COLLECTOR.RESOURCE_CONFIRMATION,
        ],
        host_factory=lambda: host,
        checkout=rw.world.checkout,
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["all_matched"] and result["approval_generated"] is False
    assert result["schema"] == "dittobench-coding-native-resource-collection-result-v1"
    stored = (rw.world.store / result["record_sha256"]).read_bytes()
    assert EVIDENCE.parse_record_envelope(stored)["kind"] == "resource_enforcement"


# ---------------------------------------------------------------------------
# Enforcement failures: an honest record the verifier refuses


def cgroup_file(container: str, name: str, value: bytes | None):
    def change(scenario: Scenario) -> None:
        scenario.cgroup[(container, name)] = value

    return change


def setting(name: str, value: Any):
    def change(scenario: Scenario) -> None:
        setattr(scenario, name, value)

    return change


def keyed(name: str, key: str, value: Any):
    def change(scenario: Scenario) -> None:
        getattr(scenario, name)[key] = value

    return change


ENFORCEMENT_FAILURES = [
    (
        "harness swap allowance",
        cgroup_file("harness", "memory.swap.max", b"4294967296"),
        "harness.memory_swap_max",
    ),
    (
        "no memory limit",
        cgroup_file("executor_authoring", "memory.max", b"max"),
        "executor_authoring.memory_max",
    ),
    (
        "no cpu quota",
        cgroup_file("executor_grading", "cpu.max", b"max 100000"),
        "executor_grading.cpu_quota",
    ),
    (
        "pids limit differs",
        cgroup_file("harness", "pids.max", b"4096"),
        "harness.pids_max",
    ),
    (
        "no oom kill",
        cgroup_file("executor_authoring", "memory.events", b"oom_kill 0\n"),
        "executor_authoring.memory_oom",
    ),
    (
        "oom far below the limit",
        cgroup_file("harness", "memory.peak", b"1048576"),
        "harness.memory_oom",
    ),
    (
        "cpu above tolerance",
        cgroup_file(
            "harness", "cpu.stat", b"usage_usec 999999999999\nnr_throttled 5\n"
        ),
        "harness.cpu_throttle",
    ),
    (
        "fork never refused",
        cgroup_file("executor_grading", "pids.events", b"max 0\n"),
        "executor_grading.pids_cap",
    ),
    (
        "fewer pids than the cap",
        cgroup_file("harness", "pids.current", b"3"),
        "harness.pids_cap",
    ),
    ("scratch never fills", setting("statvfs_free", 1000), "harness.scratch_enospc"),
    (
        "root mounted writable",
        setting("mount_read_only", False),
        "harness.rootfs_read_only",
    ),
    (
        "probe file created",
        setting("rootfs_created", True),
        "executor_grading.rootfs_read_only",
    ),
    (
        "descriptor table not full",
        keyed("fds", "executor_authoring", 600),
        "executor_authoring.nofile_cap",
    ),
    ("loose nofile limit", setting("nofile_soft", 4096), "harness.nofile_cap"),
    ("harness keeps no log", setting("docker_logs", 0), "harness.log_bound"),
    (
        "harness keeps too much log",
        setting("docker_logs", 9 << 20),
        "harness.log_bound",
    ),
    (
        "writer never exceeded the bound",
        keyed("wchar", "executor_authoring", 100),
        "executor_authoring.log_bound",
    ),
    (
        "grading retained a byte",
        keyed("receipt", "executor_grading", {"retained_output_bytes": 1}),
        "executor_grading.log_bound",
    ),
    (
        "grading writer idle",
        keyed("wchar", "executor_grading", 10),
        "executor_grading.log_bound",
    ),
    (
        "supervisor exit code",
        keyed("receipt", "executor_grading", {"return_code": 0}),
        "executor_grading.supervisor_timeout.hidden",
    ),
    (
        "supervisor late",
        setting("timeout_extra_ms", 90000),
        "executor_grading.supervisor_timeout.visible",
    ),
    (
        "process survives the timeout",
        setting("timeout_live", 1),
        "executor_grading.supervisor_timeout.hidden",
    ),
]


@pytest.mark.parametrize(
    ("change", "probe_id"),
    [item[1:] for item in ENFORCEMENT_FAILURES],
    ids=[item[0] for item in ENFORCEMENT_FAILURES],
)
def test_each_enforcement_failure_is_recorded_and_refused(rw, change, probe_id):
    scenario = Scenario()
    change(scenario)
    record, _ = rw.collect(scenario)
    unmatched = {
        item["id"]
        for phase in record["phases"]
        for item in phase["probes"]
        if not item["matched"]
    }
    assert probe_id in unmatched
    failure = rw.verify(record)
    assert failure is not None and "did not match" in failure


def memory_peak(container: str, language_limit_offset: int):
    def change(scenario: Scenario) -> None:
        limit = resource_limits(container, "go")["memory"]
        scenario.cgroup[(container, "memory.peak")] = str(
            limit + language_limit_offset
        ).encode()

    return change


@pytest.mark.parametrize("page", [4096, 16384, 65536])
def test_memory_peak_one_page_over_the_limit_verifies(rw, page):
    scenario = Scenario()
    scenario.page_bytes = page
    memory_peak("harness", page)(scenario)
    record, _ = rw.collect(scenario)
    item = base.find_probe(record, "harness.memory_oom", "go")
    assert item["matched"] is True
    assert item["observed"]["page_bytes"] == page
    assert item["observed"]["measured"] == item["observed"]["limit"] + page
    assert rw.verify(record) is None


def test_memory_peak_one_page_and_a_byte_over_the_limit_is_refused(rw):
    scenario = Scenario()
    memory_peak("harness", 4097)(scenario)
    record, _ = rw.collect(scenario)
    assert base.find_probe(record, "harness.memory_oom", "go")["matched"] is False
    failure = rw.verify(record)
    assert failure is not None and "harness.memory_oom did not match" in failure


@pytest.mark.parametrize("page", [0, 1, 4095, 8192, 2097152])
def test_memory_page_size_outside_the_accepted_set_is_refused(rw, page):
    scenario = Scenario()
    scenario.page_bytes = page
    # The verifier's own evaluation refuses while the record is assembled, so
    # nothing is retained.
    with pytest.raises(Exception, match="not an accepted page size") as caught:
        rw.collect(scenario)
    assert type(caught.value).__name__ == "Refusal"


def test_the_verifier_refuses_an_edited_page_size(rw):
    record, _ = rw.collect()
    base.edit_observed("harness.memory_oom", "go", page_bytes=8192)(record)
    failure = rw.verify(record)
    assert failure is not None and "not an accepted page size" in failure
    record, _ = rw.collect()
    item = base.find_probe(record, "harness.memory_oom", "go")
    del item["observed"]["page_bytes"]
    failure = rw.verify(record)
    assert failure is not None and "keys are not the closed set" in failure


def test_main_exits_nonzero_for_an_unmatched_resource_record(rw, capsys):
    scenario = Scenario()
    scenario.cgroup[("harness", "memory.swap.max")] = b"max"
    code = COLLECTOR.main(
        [
            "resource",
            "--config",
            str(CONFIG_PATH),
            "--confirm",
            COLLECTOR.RESOURCE_CONFIRMATION,
        ],
        host_factory=lambda: rw.host(scenario),
        checkout=rw.world.checkout,
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 3 and not result["all_matched"]
    assert "harness.memory_swap_max" in result["unmatched"]


# ---------------------------------------------------------------------------
# Binding refusals: no record at all


def inspect_change(container: str, mutate):
    def change(scenario: Scenario) -> None:
        def apply(value: dict, run: dict) -> None:
            if run["class"] == container:
                mutate(value)

        scenario.inspect = apply

    return change


def nested(*path: str, value: Any):
    def mutate(inspected: dict) -> None:
        target = inspected
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


REFUSALS = [
    (
        "installed runner",
        setting("installed_runner", base.digest("other")),
        "release-recorded binary",
    ),
    (
        "agent self report",
        setting("agent_digest", base.digest("other")),
        "self-report differs",
    ),
    (
        "agent inputs",
        setting("agent_inputs", {"execution_profile_sha256": "0" * 64}),
        "other profile documents",
    ),
    ("agent user", setting("agent_uid", 0), "runs as another user"),
    (
        "workload binary",
        setting("tampered_exe", {"executor_grading"}),
        "binary other than",
    ),
    ("workload identity", keyed("workload_uid", "harness", 1000), "another identity"),
    ("workload nonce", setting("cmdline_nonce", "f" * 16), "did not start"),
    (
        "image",
        inspect_change("harness", nested("Image", value="sha256:" + "0" * 64)),
        "another image",
    ),
    (
        "memory",
        inspect_change("executor_authoring", nested("HostConfig", "Memory", value=1)),
        "limits differ",
    ),
    (
        "swap",
        inspect_change("harness", nested("HostConfig", "MemorySwap", value=-1)),
        "limits differ",
    ),
    (
        "tmpfs",
        inspect_change("executor_grading", nested("HostConfig", "Tmpfs", value={})),
        "limits differ",
    ),
    (
        "capabilities",
        inspect_change("harness", nested("HostConfig", "CapDrop", value=[])),
        "limits differ",
    ),
    (
        "rootfs flag",
        inspect_change("harness", nested("HostConfig", "ReadonlyRootfs", value=False)),
        "limits differ",
    ),
    (
        "grading log driver",
        inspect_change(
            "executor_grading",
            nested("HostConfig", "LogConfig", value={"Type": "json-file"}),
        ),
        "not the production executor launch",
    ),
    (
        "executor network",
        inspect_change(
            "executor_authoring", nested("HostConfig", "NetworkMode", value="bridge")
        ),
        "not the production executor launch",
    ),
    (
        "executor user",
        inspect_change(
            "executor_grading", nested("Config", "User", value="10001:10001")
        ),
        "not the production executor launch",
    ),
    (
        "harness mounts",
        inspect_change("harness", nested("Mounts", value=[])),
        "not the hosted harness launch",
    ),
    (
        "harness network",
        inspect_change("harness", nested("HostConfig", "NetworkMode", value="bridge")),
        "not the hosted harness launch",
    ),
    (
        "cgroup outside daemon",
        setting("cgroup_path", lambda _path: "system.slice/docker-x.scope"),
        "outside the daemon user's",
    ),
    (
        "memory.peak missing",
        cgroup_file("harness", "memory.peak", None),
        "lacks memory.peak",
    ),
    (
        "container remains",
        setting("container_remains", {"executor_authoring"}),
        "container remains",
    ),
    (
        "no receipt",
        setting("run_failed", {"executor_grading"}),
        "no production receipt",
    ),
    ("stray container", setting("stray_container", True), "preconditions do not hold"),
    (
        "daemon change",
        setting(
            "daemon_info_after",
            {**base.DAEMON_IDENTITY_VECTOR["info"], "ID": "other-engine"},
        ),
        "daemon identity",
    ),
    ("reboot", setting("reboot", True), "rebooted"),
    ("stale preflight", setting("preflight_age", 1000), "stale"),
]


@pytest.mark.parametrize(
    ("change", "reason"),
    [item[1:] for item in REFUSALS],
    ids=[item[0] for item in REFUSALS],
)
def test_resource_collection_refusals(rw, change, reason):
    scenario = Scenario()
    change(scenario)
    host = rw.host(scenario)
    with pytest.raises(COLLECTOR.Refusal, match=reason):
        rw.collector(host).collect()
    # A refusal after the agent started still stops it and removes its inputs.
    if host.work_dir is not None:
        assert host.work_dir_removed


def test_residue_is_recorded_and_refused(rw):
    scenario = Scenario()
    scenario.residue_process = True
    record, _ = rw.collect(scenario)
    assert record["residue"]["processes"] == 1
    assert "residue is not empty" in rw.verify(record)


def test_inputs_must_be_the_released_pinned_image_set(rw):
    images = base.enforcement_images_value()
    images["images"]["go"]["image_digest"] = "sha256:" + base.digest("unreleased")
    rw.world.profile_paths["enforcement_images_sha256"].write_bytes(
        EVIDENCE.canonical_bytes(images)
    )
    with pytest.raises(COLLECTOR.Refusal, match="not the released image"):
        rw.collector(rw.host()).collect()


def test_resource_config_is_closed_and_shadow_only(rw):
    good = dict(rw.config)
    COLLECTOR.parse_resource_config(json.dumps(good).encode())
    for change in (
        {"shadow_only": False},
        {"weight_eligible": True},
        {"extra": 1},
        {"execution_profile": "relative/profile.json"},
        {"grading_profile": "/etc/../tmp/grading.json"},
        {"seccomp_profile": "unconfined"},
        {"apparmor_profile": "bad profile"},
        {"schema": COLLECTOR.CONFIG_SCHEMA},
    ):
        with pytest.raises(COLLECTOR.Refusal):
            COLLECTOR.parse_resource_config(json.dumps({**good, **change}).encode())
    rw.config["seccomp_profile"] = "ditto-coding"
    record, host = rw.collect()
    assert host.agent_arguments[-2:] == ["--seccomp-profile", "ditto-coding"]
    assert rw.verify(record) is None


# ---------------------------------------------------------------------------
# Verifier tamper paths on a collected record


def tamper_observed(probe_id: str, field: str, value: Any):
    def change(record: dict) -> None:
        base.find_probe(record, probe_id, "go")["observed"][field] = value

    return change


VERIFIER_TAMPERS = [
    (
        "limit raised to fit",
        tamper_observed("harness.pids_cap", "limit", 1),
        "differs from the approved profile",
    ),
    (
        "profile echo",
        tamper_observed("executor_grading.memory_max", "profile", 7),
        "differs from the approved profile",
    ),
    (
        "deadline",
        tamper_observed(
            "executor_grading.supervisor_timeout.visible", "deadline_ms", 1
        ),
        "differs from the approved profile",
    ),
    (
        "grading log limit",
        tamper_observed("executor_grading.log_bound", "limit", 0),
        "differs from the approved profile",
    ),
    (
        "matched flipped",
        lambda record: base.find_probe(record, "harness.cpu_throttle", "go").update(
            matched=False
        ),
        "misreported",
    ),
    (
        "inputs",
        lambda record: record["inputs"].update(
            execution_profile_sha256=base.digest("other")
        ),
        "differs from the supplied document",
    ),
    ("network binding", lambda record: record.update(network_binding={}), "closed set"),
    (
        "endpoint",
        lambda record: record["endpoints"].append(
            {"role": "router", "endpoint_sha256": "0" * 64}
        ),
        "not allowed",
    ),
    (
        "runner",
        lambda record: record["tools"].update(probe_runner_binary_sha256="0" * 64),
        "release-recorded binary",
    ),
]


@pytest.mark.parametrize(
    ("change", "reason"),
    [item[1:] for item in VERIFIER_TAMPERS],
    ids=[item[0] for item in VERIFIER_TAMPERS],
)
def test_verifier_refuses_a_tampered_resource_record(rw, change, reason):
    record, _ = rw.collect()
    tampered = copy.deepcopy(record)
    change(tampered)
    failure = rw.verify(tampered)
    assert failure is not None and reason in failure


# ---------------------------------------------------------------------------
# Cleanup scenarios (implemented) and the refused kinds


def cleanup_collector(rw, scenario: Scenario | None = None):
    host = rw.host(scenario)
    collector = rw.collector(host, "cleanup")
    collector.bind_host()
    collector.bind_release()
    collector.bind_preflight()
    collector.bind_daemon()
    collector.bind_inputs()
    collector.start_agent()
    return collector, host


def test_cleanup_scenarios_observe_absence_from_outside(rw):
    collector, host = cleanup_collector(rw)
    collector.phases()
    implemented = {
        probe["id"] for probe in base.catalog()["kinds"]["cleanup_recovery"]["probes"]
    } - set(COLLECTOR.NOT_COLLECTED["cleanup_recovery"])
    assert {key[0] for key in collector.observed} == implemented
    for (probe_id, language), value in collector.observed.items():
        assert language is None
        assert EVIDENCE.evaluate(
            base.catalog_probe("cleanup_recovery", probe_id)["expect"]
            if hasattr(base, "catalog_probe")
            else next(
                item["expect"]
                for item in base.catalog()["kinds"]["cleanup_recovery"]["probes"]
                if item["id"] == probe_id
            ),
            value,
            base.SUBORDINATE,
            set(base.catalog()["outcomes"]),
        )
    starts = [item for item in host.requests if item["op"] == "start"]
    assert [item.get("fail_start", False) for item in starts].count(True) == 1
    # Nothing any scenario launched remains, and the journal is reconciled.
    assert host.containers == {} and host.networks == set() and host.journal == []


def test_cleanup_records_leftovers_and_refuses_an_agent_that_ignores_sigterm(rw):
    scenario = Scenario()
    scenario.leftover_after = {"hang": {"processes": 1}}
    collector, _ = cleanup_collector(rw, scenario)
    collector.scenario("timeout", "cleanup.timeout.absent", collector.scenario_timeout)
    assert collector.observed[("cleanup.timeout.absent", None)]["processes"] == 1

    scenario = Scenario()
    scenario.agent_survives_sigterm = True
    collector, _ = cleanup_collector(rw, scenario)
    with pytest.raises(COLLECTOR.Refusal, match="did not stop after SIGTERM"):
        collector.scenario_runner_sigterm()


def test_uncollectable_kinds_refuse_before_any_host_effect(capsys):
    assert COLLECTOR.main(["preexec"], host_factory=pytest.fail) == 2
    err = capsys.readouterr().err
    for probe in COLLECTOR.NOT_COLLECTED["preexec_confinement"]:
        assert probe in err
    assert COLLECTOR.NOT_COLLECTED["cleanup_recovery"] == {}


def test_not_collected_probes_are_catalog_probes_and_documented():
    catalog = base.catalog()
    text = DOC.read_text()
    for kind, missing in COLLECTOR.NOT_COLLECTED.items():
        ids = {probe["id"] for probe in catalog["kinds"][kind]["probes"]}
        assert set(missing) <= ids
        for probe_id in missing:
            assert f"`{probe_id}`" in text, probe_id
    assert COLLECTOR.NOT_COLLECTED["resource_enforcement"] == {}
    assert set(COLLECTOR.NOT_COLLECTED["preexec_confinement"]) == {
        probe["id"] for probe in catalog["kinds"]["preexec_confinement"]["probes"]
    }


def test_samplers_parse_cgroup_and_proc_formats():
    assert COLLECTOR.parse_cgroup_limit(b"max\n", "x") == COLLECTOR.INT64_MAX
    assert COLLECTOR.parse_cgroup_limit(b"1024\n", "x") == 1024
    assert COLLECTOR.parse_cpu_max(b"150000 100000\n") == 1500
    assert COLLECTOR.parse_cpu_max(b"max 100000\n") == 0
    assert COLLECTOR.parse_keyed(b"rchar: 1\nwchar: 99\n", "io")["wchar"] == 99
    assert COLLECTOR.parse_nofile_limits(
        b"Max open files            1024                 2048                 files\n"
    ) == (1024, 2048)
    assert COLLECTOR.root_mount_read_only(
        b"1 0 0:1 / / rw - overlay overlay rw\n2 1 0:2 / / ro,relatime - x x rw\n"
    )
    assert COLLECTOR.parse_stat(
        b"42 (a b) c) S " + b" ".join([b"0"] * 18 + [b"777", b"0"])
    ) == ("S", 777)
    for raw, call in (
        (b"-1", lambda value: COLLECTOR.parse_cgroup_limit(value, "x")),
        (None, lambda value: COLLECTOR.parse_cgroup_limit(value, "x")),
        (b"100000", COLLECTOR.parse_cpu_max),
        (b"a b c", lambda value: COLLECTOR.parse_keyed(value, "x")),
        (b"max 1\nmax 2\n", lambda value: COLLECTOR.parse_keyed(value, "x")),
    ):
        with pytest.raises(COLLECTOR.Refusal):
            call(raw)
    assert COLLECTOR.workload_args(
        "log", "0123456789abcdef", hold_ms=10, bytes=5, setsid_child=False
    ) == [
        "workload",
        "log",
        "--nonce",
        "0123456789abcdef",
        "--hold-ms",
        "10",
        "--bytes",
        "5",
    ]
    assert COLLECTOR.cpu_burners(1500) == 3 and COLLECTOR.cpu_burners(2000) == 3


def test_collector_source_names_no_approval_and_no_workflow():
    source = COLLECTOR_PATH.read_text()
    for forbidden in ("native_approval", "curator", "gcloud", "ssh ", "shell=True"):
        assert forbidden not in source
    workflow = ROOT / ".github/workflows/coding-hosted-operate.yml"
    if workflow.exists():
        assert "collect-coding-native-enforcement" not in workflow.read_text()


# ---------------------------------------------------------------------------
# Cleanup recovery: SIGKILL journal reconciliation and the consumed rerun


def cleanup_main(rw, scenario: Scenario | None = None, confirm: str | None = None):
    host = rw.host(scenario)
    code = COLLECTOR.main(
        [
            "cleanup",
            "--config",
            str(CONFIG_PATH),
            "--confirm",
            confirm or COLLECTOR.CLEANUP_CONFIRMATION,
        ],
        host_factory=lambda: host,
        checkout=rw.world.checkout,
    )
    return code, host


def test_cleanup_record_is_collected_retained_and_verifies_offline(rw, capsys):
    code, host = cleanup_main(rw)
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["all_matched"] and result["approval_generated"] is False
    assert result["schema"] == "dittobench-coding-native-cleanup-collection-result-v1"
    stored = (rw.world.store / result["record_sha256"]).read_bytes()
    record = json.loads(stored)
    assert record["kind"] == "cleanup_recovery"
    assert rw.verify(record) is None
    observed_ids = {
        (item["id"], json.dumps(item["observed"], sort_keys=True))
        for phase in record["phases"]
        for item in phase["probes"]
    }
    assert (
        "cleanup.runner_sigkill.sentinel_network",
        '{"outcome": "present"}',
    ) in observed_ids
    assert (
        "cleanup.rerun.consumed_marker",
        '{"outcome": "refused"}',
    ) in observed_ids
    # Every session had a fresh attempt directory in the private work dir;
    # the rerun reused the killed attempt's.
    work = COLLECTOR.WORK_DIR
    assert host.private_dirs == [
        COLLECTOR.JOURNAL_DIR,
        work / "attempt-1",
        work / "attempt-2",
    ]
    assert host.agent_arguments[-1] == str(work / "attempt-2")
    # The runtime's reconciler ran once, as the daemon user's transient unit,
    # and the collector's decoy network is gone again.
    assert len(host.one_shots) == 1
    assert host.networks == set() and host.containers == {} and host.journal == []
    decoys = [
        command
        for command in host.commands
        if command[:3] == ("docker", "network", "create")
    ]
    assert (
        len(decoys) == 1
        and COLLECTOR.SENTINEL_LABEL + "=collector-decoy" in (decoys[0])
    )


def test_cleanup_needs_the_exact_confirmation_and_root(rw, monkeypatch):
    with pytest.raises(COLLECTOR.Refusal, match="exact confirmation"):
        cleanup_main(rw, confirm=COLLECTOR.RESOURCE_CONFIRMATION)
    monkeypatch.setattr(ResourceFakeHost, "euid", lambda _self: 1001)
    with pytest.raises(COLLECTOR.Refusal, match="needs root"):
        cleanup_main(rw)


def test_cleanup_refuses_another_hostname(rw, monkeypatch):
    monkeypatch.setattr(ResourceFakeHost, "hostname", lambda _self: "other-host")
    with pytest.raises(COLLECTOR.Refusal):
        cleanup_main(rw)


def add_env_field(raw: bytes) -> bytes:
    return raw.replace(b'"networks":', b'"env":["OPENROUTER_API_KEY=x"],"networks":', 1)


CLEANUP_FAILURES = [
    (
        "journal carries a non-identifier field",
        setting("journal_tamper", add_env_field),
        {"cleanup.runner_sigkill.journal_ids_only": "permitted"},
    ),
    (
        "journal value is a path",
        setting(
            "journal_tamper",
            lambda raw: raw.replace(b'"worker":"native', b'"worker":"/home/native', 1),
        ),
        {"cleanup.runner_sigkill.journal_ids_only": "permitted"},
    ),
    (
        "journal is not the owner-only single-link file",
        setting("journal_not_private", True),
        {"cleanup.runner_sigkill.journal_ids_only": "permitted"},
    ),
    (
        "workload container never journaled",
        setting("journal_omits_container", True),
        {
            "cleanup.runner_sigkill.sentinel_network": "absent",
            "cleanup.runner_sigkill.reconciled_absent": {
                "containers": 1,
                "networks": 0,
                "processes": 0,
                "volumes": 0,
            },
        },
    ),
    (
        "kill left nothing to reconcile",
        setting("kill_leaves_nothing", True),
        {"cleanup.runner_sigkill.sentinel_network": "absent"},
    ),
    (
        "reconciler removed an unjournaled network",
        setting("reconciler_removes_decoy", True),
        {"cleanup.runner_sigkill.sentinel_network": "extra_ids_touched"},
    ),
    (
        "reconciler left the sentinel",
        setting("reconciler_skips_sentinel", True),
        {
            "cleanup.runner_sigkill.sentinel_network": "probe_error",
            "cleanup.runner_sigkill.reconciled_absent": {
                "containers": 0,
                "networks": 1,
                "processes": 0,
                "volumes": 0,
            },
        },
    ),
    (
        "consumed attempt started again",
        setting("rerun_accepted", True),
        {"cleanup.rerun.consumed_marker": "accepted"},
    ),
    (
        "no consumed marker on disk",
        setting("marker_missing", True),
        {"cleanup.rerun.consumed_marker": "accepted"},
    ),
]


@pytest.mark.parametrize(
    ("change", "expected"),
    [item[1:] for item in CLEANUP_FAILURES],
    ids=[item[0] for item in CLEANUP_FAILURES],
)
def test_each_cleanup_recovery_failure_is_recorded_and_refused(rw, change, expected):
    scenario = Scenario()
    change(scenario)
    host = rw.host(scenario)
    record = rw.collector(host, "cleanup").collect()
    probes = {
        item["id"]: item for phase in record["phases"] for item in phase["probes"]
    }
    for probe_id, value in expected.items():
        item = probes[probe_id]
        observed_value = value if isinstance(value, dict) else {"outcome": value}
        assert item["observed"] == observed_value, probe_id
        assert item["matched"] is False
    failure = rw.verify(record)
    # A leftover the reconciler never removed is also collection residue.
    assert failure is not None and (
        "did not match" in failure or "residue is not empty" in failure
    )
    # Whatever the failure, the collector's own decoy never outlives it.
    assert not host.decoys & host.networks


def test_launch_journal_parser_accepts_only_the_runtime_encoding():
    good = journal_line("abc", ["dittobench-abc"], ["ditto-job-abc"])
    assert COLLECTOR.parse_launch_journal(good + good)[0]["run"] == "abc"
    assert COLLECTOR.parse_launch_journal(b"") == []
    assert COLLECTOR.parse_launch_journal(None) is None
    # A torn final append is ignored; other trailing bytes are not.
    assert len(COLLECTOR.parse_launch_journal(good + b'{"schema":"ditto')) == 1
    for raw in (
        good + b"garbage",
        good + b'{"schema":"x"}',
        good.replace(b'{"schema"', b'{ "schema"'),
        good.replace(b'"attempt":', b'"run":"x","attempt":'),
        add_env_field(good),
        good.replace(b"dittobench-abc", b"/var/lib/secret"),
        good.replace(
            b'"containers":["dittobench-abc"],"networks":["ditto-job-abc"]',
            b'"containers":[],"networks":[]',
        ),
        journal_line("abc", ["dittobench-abc"], ["bridge"]),
        b"\xff\n",
        good * (COLLECTOR.JOURNAL_MAX_ENTRIES + 1),
    ):
        assert COLLECTOR.parse_launch_journal(raw) is None, raw[:80]


def test_an_interrupted_sigkill_scenario_unwinds_the_decoy_and_the_journal(rw):
    scenario = Scenario()
    collector, host = cleanup_collector(rw, scenario)
    collector.stop_agent()

    def refuse(_pid: int) -> None:
        raise COLLECTOR.Refusal("kill failed")

    host.kill = refuse  # type: ignore[method-assign]
    with pytest.raises(COLLECTOR.Refusal, match="kill failed"):
        collector.scenario_runner_sigkill()
    assert any(name.startswith("ditto-job-sentinel-") for name in host.networks)
    collector.stop_agent()
    collector.unwind()
    assert host.networks == set() and host.journal == []


@pytest.fixture
def spawned(monkeypatch):
    """Records every process the output counter starts."""

    processes = []
    real = COLLECTOR.subprocess.Popen

    def popen(*args, **kwargs):
        process = real(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(COLLECTOR.subprocess, "Popen", popen)
    return processes


def test_process_output_counter_counts_combined_output(spawned):
    script = "import sys; sys.stdout.write('abc'); sys.stderr.write('de')"
    total = COLLECTOR.count_process_output(
        [sys.executable, "-c", script], 30, "fake docker"
    )
    assert total == 5
    assert spawned[0].returncode == 0


@pytest.mark.parametrize(
    "script",
    [
        # hangs without writing or closing the pipe
        "import time; time.sleep(60)",
        # writes a little, then holds the pipe open without filling a buffer
        "import sys, time; sys.stdout.write('x'); sys.stdout.flush(); time.sleep(60)",
    ],
)
def test_process_output_counter_kills_a_hung_command_at_its_deadline(spawned, script):
    started = time.monotonic()
    with pytest.raises(COLLECTOR.Refusal, match="fake docker output took too long"):
        COLLECTOR.count_process_output(
            [sys.executable, "-c", script], 0.5, "fake docker"
        )
    assert time.monotonic() - started < 10
    assert len(spawned) == 1 and spawned[0].returncode is not None  # killed and reaped


def test_process_output_counter_refuses_a_pipe_held_by_a_leftover_child(spawned):
    # The command exits at once but leaves a child holding its output pipe.
    script = (
        "import subprocess, sys; "
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])"
    )
    started = time.monotonic()
    with pytest.raises(COLLECTOR.Refusal, match="took too long"):
        COLLECTOR.count_process_output(
            [sys.executable, "-c", script], 0.5, "fake docker"
        )
    assert time.monotonic() - started < 4
    assert spawned[0].returncode is not None


def test_process_output_counter_refuses_a_failed_command(spawned):
    with pytest.raises(COLLECTOR.Refusal, match="fake docker failed"):
        COLLECTOR.count_process_output(
            [sys.executable, "-c", "raise SystemExit(3)"], 30, "fake docker"
        )
    assert spawned[0].returncode == 3
