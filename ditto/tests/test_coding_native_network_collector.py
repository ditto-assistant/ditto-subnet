"""Native network enforcement collector (B5 PR4) against a simulated host.

Nothing here touches a host, daemon, firewall or provider. The host is a
``FakeHost`` that models the worker unit, the deny guard, the connectivity
window and the agents; its nft listings are real kernel output recorded in a
throwaway user and network namespace (``unshare -rn``) from the reviewed
``connectivity-policy.py`` and ``host-policy.py``, with only the worker cgroup
path substituted back (the recording namespace cannot resolve it). The live
test at the end re-records them when ``unshare -rn nft`` works, and CI requires
it (``DITTOBENCH_REQUIRE_LIVE_NFT=1``).
"""

import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ditto.tests import test_coding_native_enforcement_evidence as base

ROOT = Path(__file__).parents[2]
COLLECTOR_PATH = ROOT / "infra/scripts/collect-coding-native-enforcement.py"
spec = importlib.util.spec_from_file_location(
    "native_network_collector", COLLECTOR_PATH
)
assert spec is not None and spec.loader is not None
COLLECTOR = importlib.util.module_from_spec(spec)
spec.loader.exec_module(COLLECTOR)
EVIDENCE = base.EVIDENCE
FIXTURES = Path(__file__).parent / "fixtures/coding_native_network"
POLICY_PATH = (
    ROOT / "infra/ansible/roles/coding_hosted_connectivity/files/connectivity-policy.py"
)
HOST_POLICY_PATH = ROOT / "infra/ansible/roles/coding_hosted/files/host-policy.py"

T0 = base.T0
UID = GID = 1001
PROFILE_RAW = (FIXTURES / "probe-profile.json").read_bytes()
PROFILE = json.loads(PROFILE_RAW)
ISSUED, EXPIRES = PROFILE["issued_at_unix"], PROFILE["expires_at_unix"]
NFT_SCOPED = (FIXTURES / "nft-scoped.json").read_bytes()
NFT_DENY = (FIXTURES / "nft-deny-after-scoped.json").read_bytes()
NFT_DENY_INITIAL = (FIXTURES / "nft-deny-initial.json").read_bytes()
NFT_DENY_AFTER_EXPIRY = (FIXTURES / "nft-deny-after-expiry.json").read_bytes()
NFT_FIXTURE_NAMES = (
    "nft-deny-initial.json",
    "nft-scoped.json",
    "nft-deny-after-scoped.json",
    "nft-deny-after-expiry.json",
)
PREFLIGHT_PATH = ROOT / "infra/scripts/inspect-coding-native-host.py"
RUNNER_SHA256 = base.PROBE_RUNNER_BINARY
RUNNER = (
    f"/opt/ditto-coding-hosted/{base.REVISION}/bin/dittobench-coding-enforcement-probe"
)
ROUTER = ("10.30.0.4", 18080)
PROXY = ("10.30.0.5", 3128)
TRUSTED = [("10.20.0.7", 5432), ("10.20.0.9", 443)]
CONFIG_PATH = Path("/etc/ditto-coding-native-collector/network.json")
PROFILE_PATH = Path("/etc/ditto-coding-native-collector/probe-profile.json")
PREREQUISITES = {
    "schema": "dittobench-coding-hosted-host-prerequisites-v2",
    "shadow_only": True,
    "weight_eligible": False,
    "router_listen": "10.30.0.4:18080",
    "egress_network": "ditto-coding-restricted",
    "egress_proxy": "http://10.30.0.5:3128",
    "candidate_uid": 10001,
    "candidate_gid": 10001,
}
WORKER_EXEC = (
    f"{{ path={RUNNER} ; argv[]={RUNNER} net-agent --unix "
    "/run/ditto-coding-hosted-enforcement/agent.sock ; ignore_errors=no ; "
    "start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }"
)
WORKER_STOP = (
    "{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 -I "
    "/usr/local/lib/ditto-coding-hosted/connectivity-policy.py revoke ; "
    "ignore_errors=no } ; "
    f"{{ path={RUNNER} ; argv[]={RUNNER} net-once --gate "
    "/run/ditto-coding-hosted-enforcement/gate --plan "
    "/run/ditto-coding-hosted-enforcement/plan.json --report "
    "/run/ditto-coding-hosted-enforcement/report.json ; ignore_errors=no }"
)
CONTAINER_IPS = {"candidate": "172.18.0.2", "sibling": "172.19.0.2", "executor": None}
CONTAINER_UIDS = {"candidate": 65532, "sibling": 65532, "executor": 10001}
DENIAL = "not_permitted"


class Scenario:
    """Knobs a refusal test turns; the defaults describe a correct host."""

    def __init__(self) -> None:
        self.overrides: dict[tuple, str] = {}
        self.router_remote: str | None = (
            None  # defaults to the candidate container address
        )
        self.tampered_exe: set[str] = set()
        self.lying: set[str] = set()
        self.listener_sees: set[str] = set()
        self.nft_scoped = NFT_SCOPED
        self.nft_deny = NFT_DENY
        self.worker_active_before = False
        self.stray_container = False
        self.proxy_active = True
        self.installed_runner_sha256 = RUNNER_SHA256
        self.daemon_info_after = None
        self.failed_start_fails = True
        self.preflight_age = 60
        self.profile_raw = PROFILE_RAW
        self.check_outcome = "timeout"


class FakeSession:
    def __init__(self, host: "FakeHost", role: str, pid: int) -> None:
        self.host, self.role, self.pid = host, role, pid
        self.listeners: dict[str, tuple[str, int]] = {}
        self.connections: dict[str, str] = {}
        self.counter = 0
        self.closed = False

    def handle(self, value: dict) -> dict:
        host, scenario = self.host, self.host.scenario
        op = value["op"]
        answer = {"schema": COLLECTOR.AGENT_SCHEMA, "op": op}
        host.requests.append((self.role, value))
        if op == "hello":
            uid = CONTAINER_UIDS.get(self.role, UID)
            answer.update(
                pid=self.pid if self.role in ("worker", "daemon") else 1,
                uid=uid,
                gid=uid,
                probe_runner_binary_sha256=(
                    base.digest("another binary")
                    if self.role in scenario.lying
                    else RUNNER_SHA256
                ),
            )
        elif op == "exit":
            if self.role == "worker":
                host.router_listening = False
        elif op == "listen":
            self.counter += 1
            listener = f"l{self.counter}"
            self.listeners[listener] = (value["address"], value["port"])
            if self.role == "worker" and (value["address"], value["port"]) == ROUTER:
                host.router_listening = True
                host.router_listener = (self, listener)
            answer.update(outcome="accepted", listener=listener)
        elif op == "accept":
            endpoint = self.listeners[value["listener"]]
            host.clock += 0 if host.pending.get(endpoint) else 1
            if host.pending.get(endpoint):
                host.pending[endpoint] -= 1
                self.counter += 1
                connection = f"c{self.counter}"
                self.connections[connection] = "router"
                remote = scenario.router_remote or CONTAINER_IPS["candidate"]
                answer.update(
                    outcome="accepted", connection=connection, remote_address=remote
                )
            elif endpoint[1] in (18100, 18101) and self.role in scenario.listener_sees:
                answer.update(
                    outcome="accepted", connection="c99", remote_address="10.0.2.100"
                )
            else:
                answer.update(outcome="timeout")
        elif op == "check":
            answer.update(
                outcome=scenario.check_outcome
                if host.clock >= EXPIRES
                else "connected",
                connection=value["connection"],
            )
        elif op == "establish":
            outcome = host.decide(self.role, "connect", value["address"], value["port"])
            answer.update(outcome=outcome)
            if outcome == "connected":
                self.counter += 1
                answer["connection"] = f"c{self.counter}"
                endpoint = (value["address"], value["port"])
                host.pending[endpoint] = host.pending.get(endpoint, 0) + 1
        elif op in ("connect", "handshake"):
            outcome = host.decide(self.role, op, value["address"], value["port"])
            if outcome == "connected" and op == "connect":
                endpoint = (value["address"], value["port"])
                if endpoint == ROUTER:
                    host.pending[endpoint] = host.pending.get(endpoint, 0) + 1
            answer.update(outcome=outcome)
        elif op == "proxy_forward":
            key = (self.role, op, value["address"], value["port"])
            active = host.scoped_active()
            answer.update(
                outcome=scenario.overrides.get(
                    key, "proxy_refused" if active else DENIAL
                )
            )
        elif op == "dns":
            answer.update(outcome=scenario.overrides.get((self.role, op), "refused"))
        else:
            raise AssertionError(op)
        return answer

    def request(self, value: dict, timeout: float) -> dict:
        assert not self.closed
        assert timeout > 0
        return json.loads(json.dumps(self.handle(json.loads(json.dumps(value)))))

    def close(self) -> None:
        self.closed = True


class FakeHost:
    def __init__(self, world, scenario: Scenario) -> None:
        self.world, self.scenario = world, scenario
        self.clock = T0 + scenario.preflight_age
        self.worker_state = "active" if scenario.worker_active_before else "inactive"
        self.worker_result = "success"
        self.nft = "deny"
        self.worker_pid = 4000
        self.router_listening = False
        self.router_listener: tuple[FakeSession, str] | None = None
        self.pending: dict[tuple, int] = {}
        self.requests: list[tuple] = []
        self.commands: list[tuple] = []
        self.containers: dict[str, dict] = {}
        self.networks: set[str] = set()
        if scenario.stray_container:
            self.containers["leftover"] = {"pid": 9000, "network": None, "role": "x"}
        self.agent_dir = False
        self.plan = None
        self.report = None
        self.once_pid: int | None = None
        self.gate_opened = 0
        self.info_calls = 0

    # enforcement model -------------------------------------------------

    def scoped_active(self) -> bool:
        return self.nft == "scoped" and self.clock < EXPIRES

    def decide(self, role: str, op: str, address: str, port: int) -> str:
        override = self.scenario.overrides.get((role, op, address, port))
        if override:
            return override
        active = self.scoped_active()
        endpoint = (address, port)
        if role in ("worker", "once"):
            if endpoint in TRUSTED and active:
                return "handshake_completed" if op == "handshake" else "connected"
            return "handshake_failed" if op == "handshake" else DENIAL
        if role == "candidate":
            if endpoint == ROUTER and active:
                return "connected" if self.router_listening else "refused"
            if endpoint == PROXY and active:
                return "connected"
            if ":" in address:
                return "unreachable"
        return DENIAL

    # Host protocol -----------------------------------------------------

    def now(self) -> int:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += max(1, int(seconds))

    def euid(self) -> int:
        return 0

    def hostname(self) -> str:
        return COLLECTOR.HOSTNAME

    def machine_id(self) -> bytes:
        return b"synthetic-machine-id"

    def boot_id(self) -> str:
        return base.BOOT

    def kernel(self) -> str:
        return base.KERNEL

    def identity(self) -> tuple[int, int]:
        return UID, GID

    def read(self, path: Path) -> bytes:
        files = {
            CONFIG_PATH: json.dumps(self.world.collector_config).encode(),
            PROFILE_PATH: PROFILE_RAW,
            COLLECTOR.CONNECTIVITY_PATH: self.scenario.profile_raw,
            COLLECTOR.PREREQUISITES_PATH: json.dumps(PREREQUISITES).encode(),
            Path("/srv/native-release/release.json"): base.RELEASE_INDEX_RAW,
            Path("/etc/subuid"): b"ditto-coding-hosted:100000:65536\n",
            Path("/etc/subgid"): b"ditto-coding-hosted:100000:65536\n",
        }
        return files[path]

    def file_sha256(self, path: Path) -> str:
        if str(path) == RUNNER:
            return self.scenario.installed_runner_sha256
        assert str(path) == COLLECTOR.PROXY_SCRIPT
        return base.digest("egress-proxy.py")

    def exe_sha256(self, pid: int) -> str:
        role = self.role_of(pid)
        return (
            base.digest("tampered")
            if role in self.scenario.tampered_exe
            else RUNNER_SHA256
        )

    def role_of(self, pid: int) -> str:
        if pid == self.worker_pid:
            return "worker"
        if pid == self.once_pid:
            return "once"
        if pid == 5001:
            return "daemon"
        for container in self.containers.values():
            if container["pid"] + 1 == pid:
                return container["role"]
        return "unknown"

    def proc(self, pid: int, name: str) -> bytes:
        role = self.role_of(pid)
        if pid == 3001:
            if name == "cgroup":
                return b"0::/system.slice/ditto-coding-hosted-egress-proxy.service\n"
            return b"\0".join(
                [
                    b"/usr/bin/python3",
                    b"-I",
                    b"-B",
                    COLLECTOR.PROXY_SCRIPT.encode(),
                    b"10.30.0.5",
                    b"3128",
                    b"",
                ]
            )
        if name == "cgroup":
            if role in ("worker", "once"):
                return b"0::/system.slice/ditto-coding-hosted-worker.service\n"
            if role == "daemon":
                return (
                    b"0::/user.slice/user-1001.slice/user@1001.service/app.slice/"
                    b"ditto-native-network-daemon-probe.service\n"
                )
        if name == "cmdline" and role == "once":
            return f"{RUNNER}\0net-once\0--gate\0x\0".encode()
        if name == "cmdline" and role == "worker":
            return f"{RUNNER}\0net-agent\0--unix\0x\0".encode()
        if name == "status":
            if role in CONTAINER_UIDS:
                host = 100000 + CONTAINER_UIDS[role] - 1
                ids = "\t".join([str(host)] * 4)
                return f"Name:\trunner\nUid:\t{ids}\nGid:\t{ids}\n".encode()
            return b"Uid:\t1001\t1001\t1001\t1001\nGid:\t1001\t1001\t1001\t1001\n"
        if name == "net/dev" and role == "executor":
            return (
                b"Inter-|   Receive\n face |bytes packets\n"
                b"    lo: 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
            )
        raise AssertionError((pid, name))

    def children(self, pid: int) -> list[int]:
        return [pid + 1]

    def cgroup_procs(self, cgroup: str) -> list[int]:
        if cgroup == COLLECTOR.WORKER_CGROUP:
            procs = [self.once_pid] if self.once_pid else []
            if self.worker_state == "active":
                procs.append(self.worker_pid)
            return procs
        return []

    def systemctl(self, *args: str) -> tuple[int, bytes]:
        self.commands.append(("systemctl", *args))
        if args[0] == "--user":
            return 0, b"MainPID=5001\n"
        if args[0] == "show":
            unit = args[1]
            values = {}
            if unit == COLLECTOR.WORKER_UNIT:
                values = {
                    "ActiveState": self.worker_state,
                    "Result": self.worker_result,
                    "MainPID": str(
                        self.worker_pid if self.worker_state == "active" else 0
                    ),
                    "ExecStart": WORKER_EXEC,
                    "ExecStopPost": WORKER_STOP,
                }
            elif unit == COLLECTOR.EGRESS_UNIT:
                values = {"ActiveState": "active"}
            elif unit == COLLECTOR.PROXY_UNIT:
                values = {
                    "ActiveState": "active"
                    if self.scenario.proxy_active
                    else "inactive",
                    "MainPID": "3001" if self.scenario.proxy_active else "0",
                }
            wanted = [item.split("=", 1)[1] for item in args[2:]]
            return 0, "".join(
                f"{key}={values.get(key, '')}\n" for key in wanted
            ).encode()
        if args[0] == "list-units":
            return 0, b""
        if args[0] == "reset-failed":
            if self.worker_state == "failed":
                self.worker_state = "inactive"
            return 0, b""
        if args[:2] == ("start", COLLECTOR.WORKER_UNIT):
            assert self.clock - ISSUED <= 300 and self.clock < EXPIRES
            self.worker_pid += 1
            self.worker_state, self.nft = "active", "scoped"
            return 0, b""
        if args[:2] == ("kill", "--signal=SIGKILL"):
            self.once_pid, self.plan = None, None
            return 0, b""
        if args[:2] == ("stop", COLLECTOR.WORKER_UNIT):
            assert self.plan is None and self.once_pid is None
            self.worker_state, self.nft, self.router_listening = (
                "inactive",
                "deny",
                False,
            )
            return 0, b""
        if args[:3] == ("stop", "--no-block", COLLECTOR.WORKER_UNIT):
            self.worker_state, self.nft, self.router_listening = (
                "deactivating",
                "deny",
                False,
            )
            self.once_pid = 4101
            return 0, b""
        if args[:3] == ("start", "--no-block", COLLECTOR.WORKER_UNIT):
            expired = self.clock >= ISSUED + 300 or self.clock >= EXPIRES
            if expired and self.scenario.failed_start_fails:
                self.worker_state, self.nft = "activating", "deny"
                self.once_pid = 4201
            else:
                self.worker_state, self.nft = "active", "scoped"
            return 0, b""
        raise AssertionError(args)

    def docker(self, *args: str) -> bytes:
        self.commands.append(("docker", *args))
        if args[0] == "info":
            self.info_calls += 1
            info = base.DAEMON_IDENTITY_VECTOR["info"]
            if self.info_calls > 1 and self.scenario.daemon_info_after is not None:
                info = self.scenario.daemon_info_after
            return json.dumps(info).encode()
        if args[:3] == ("ps", "--all", "--quiet"):
            return "\n".join(self.containers).encode()
        if args[:2] == ("network", "ls"):
            return "\n".join(sorted(self.networks)).encode()
        if args[:2] == ("volume", "ls"):
            return b""
        if args[:2] == ("network", "create"):
            self.networks.add(args[-1])
            return b"network-id\n"
        if args[:2] == ("network", "rm"):
            self.networks.discard(args[2])
            return b""
        if args[:2] == ("rm", "--force"):
            self.containers.pop(args[2], None)
            return b""
        if args[0] == "inspect":
            container = self.containers[args[1]]
            network = container["network"]
            return json.dumps(
                [
                    {
                        "State": {"Pid": container["pid"]},
                        "Mounts": [
                            {"Destination": COLLECTOR.CONTAINER_RUNNER, "RW": False}
                        ],
                        "NetworkSettings": {
                            "Networks": (
                                {
                                    network: {
                                        "IPAddress": CONTAINER_IPS[container["role"]]
                                    }
                                }
                                if network != "none"
                                else {}
                            )
                        },
                    }
                ]
            ).encode()
        raise AssertionError(args)

    def nft_table(self) -> bytes:
        return (
            self.scenario.nft_scoped if self.nft == "scoped" else self.scenario.nft_deny
        )

    def socket_present(self, directory: Path) -> bool:
        assert directory == COLLECTOR.CUSTODY_RUN
        return False

    def prepare_agent_dir(self, uid: int, gid: int) -> None:
        assert (uid, gid) == (UID, GID)
        self.agent_dir, self.plan, self.report = True, None, None

    def write_plan(self, raw: bytes, uid: int, gid: int) -> None:
        assert self.agent_dir and self.plan is None and (uid, gid) == (UID, GID)
        self.plan = json.loads(raw)

    def open_gate(self) -> None:
        assert self.once_pid and self.plan
        self.gate_opened += 1
        responses = [
            {
                "schema": COLLECTOR.AGENT_SCHEMA,
                "op": request["op"],
                "outcome": self.decide(
                    "once", request["op"], request["address"], request["port"]
                ),
            }
            for request in self.plan["requests"]
        ]
        self.report = json.dumps(
            {
                "schema": COLLECTOR.REPORT_SCHEMA,
                "pid": self.once_pid,
                "uid": UID,
                "gid": GID,
                "probe_runner_binary_sha256": RUNNER_SHA256,
                "responses": responses,
            }
        ).encode()
        self.worker_state = "failed" if self.once_pid == 4201 else "inactive"
        self.worker_result = "exit-code" if self.once_pid == 4201 else "success"
        self.once_pid, self.plan = None, None

    def read_report(self, uid: int) -> bytes:
        assert uid == UID and self.report is not None
        return self.report

    def remove_agent_dir(self) -> None:
        self.agent_dir, self.plan = False, None

    def worker_session(self):
        return FakeSession(self, "worker", self.worker_pid)

    def daemon_session(self, runner: Path, unit: str):
        assert str(runner) == RUNNER and unit.startswith(
            "ditto-native-network-daemon-probe-"
        )
        return FakeSession(self, "daemon", 5001)

    def container_session(self, arguments: list[str]):
        name = arguments[arguments.index("--name") + 1]
        role = name.split("-")[3]
        network = arguments[arguments.index("--network") + 1]
        assert (
            arguments[arguments.index("--user") + 1]
            == f"{CONTAINER_UIDS[role]}:{CONTAINER_UIDS[role]}"
        )
        assert (
            "--pull" in arguments
            and arguments[arguments.index("--pull") + 1] == "never"
        )
        assert (
            f"type=bind,src={RUNNER},dst={COLLECTOR.CONTAINER_RUNNER},readonly"
            in arguments
        )
        assert arguments[-2].startswith("coding-runtime.invalid/python/runtime@sha256:")
        pid = {"candidate": 6000, "sibling": 7000, "executor": 8000}[role]
        self.containers[name] = {"pid": pid, "network": network, "role": role}
        return FakeSession(self, role, pid + 1)


class CollectorWorld:
    def __init__(self, tmp_path: Path) -> None:
        self.world = base.World(tmp_path)
        targets = ROOT / COLLECTOR.TARGETS_FIXTURE
        destination = self.world.checkout / COLLECTOR.TARGETS_FIXTURE
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(targets, destination)
        for path in (destination.parent, destination):
            path.chmod(0o755 if path.is_dir() else 0o644)
        self.world.profile_paths["connectivity_profile_sha256"].write_bytes(PROFILE_RAW)
        # Preflights carry the semantic digests of recorded kernel listings: the
        # deny guard before any worker start, and after the collector's worker
        # cycles and the profile's expiry.
        self.world.pre_raw = base.stdout_bytes(
            base.preflight_value(
                self.world.preflight_tools, T0, nft_semantic_sha256(NFT_DENY_INITIAL)
            )
        )
        self.world.pre_sha = self.world.put(self.world.pre_raw)
        self.world.post_value = base.preflight_value(
            self.world.preflight_tools,
            T0 + 4000,
            nft_semantic_sha256(NFT_DENY_AFTER_EXPIRY),
        )
        self.collector_config = {
            "schema": COLLECTOR.CONFIG_SCHEMA,
            "source_revision": base.REVISION,
            "release_directory": "/srv/native-release",
            "release_manifest_sha256": base.MANIFEST,
            "machine_id_sha256": base.MACHINE,
            "boot_id": base.BOOT,
            "store": str(self.world.store),
            "pre_collection_preflight_sha256": self.world.pre_sha,
            "connectivity_profile": str(PROFILE_PATH),
            "candidate_image_language": "python",
            "shadow_only": True,
            "weight_eligible": False,
        }

    def host(self, scenario: Scenario | None = None) -> FakeHost:
        return FakeHost(self, scenario or Scenario())

    def collect(self, scenario: Scenario | None = None) -> tuple[dict, FakeHost]:
        host = self.host(scenario)
        collector = COLLECTOR.Collector(
            host,
            COLLECTOR.parse_config(json.dumps(self.collector_config).encode()),
            checkout=self.world.checkout,
        )
        try:
            return collector.collect(), host
        except COLLECTOR.Refusal:
            self.last_host = host
            raise

    def verify(self, record: dict) -> str | None:
        return self.world.verify_record(record)


def nft_semantic_sha256(raw: bytes) -> str:
    return COLLECTOR.PREFLIGHT.nft_ruleset_semantic_sha256(raw, COLLECTOR.TABLE)


@pytest.fixture
def cw(tmp_path):
    return CollectorWorld(tmp_path)


def probe(record: dict, probe_id: str) -> list[dict]:
    return [
        item
        for phase in record["phases"]
        for item in phase["probes"]
        if item["id"] == probe_id
    ]


def assert_cleaned(host: FakeHost) -> None:
    assert host.containers == {}
    assert host.networks == set()
    assert (
        host.worker_state in ("inactive", "failed") or host.worker_state == "inactive"
    )
    assert host.agent_dir is False


# ---------------------------------------------------------------------------
# Accept path


def test_collected_record_verifies_offline_and_binds_the_measured_host(cw):
    record, host = cw.collect()
    assert cw.verify(record) is None
    assert all(
        item["matched"] for phase in record["phases"] for item in phase["probes"]
    )
    assert record["kind"] == "network_enforcement"
    assert record["coverage"] == "same_boot"
    assert record["not_covered"] == ["daemon_restart_recovery", "reboot_recovery"]
    assert record["host"]["router_namespace"] == "host"
    assert record["tools"]["probe_runner_binary_sha256"] == RUNNER_SHA256
    binding = record["network_binding"]
    assert (
        binding["scoped_ruleset_sha256"]
        == COLLECTOR.scoped_ruleset(NFT_SCOPED, PROFILE, UID)[0]
    )
    assert binding["deny_ruleset_sha256"] == COLLECTOR.deny_ruleset(NFT_DENY, UID)
    assert (binding["scoped_output_rules"], binding["scoped_input_rules"]) == (11, 3)
    pre = json.loads(cw.world.pre_raw)
    assert pre["nft_ruleset_semantic_sha256"] == nft_semantic_sha256(NFT_DENY_INITIAL)
    assert (
        cw.world.post_value["nft_ruleset_semantic_sha256"]
        == pre["nft_ruleset_semantic_sha256"]
    )
    assert record["preconditions"] == EVIDENCE.PRECONDITIONS
    assert record["residue"] == EVIDENCE.RESIDUE
    phases = {phase["name"]: phase for phase in record["phases"]}
    assert phases["stop_rollback"]["completed_at_unix"] < EXPIRES
    assert phases["expiry"]["completed_at_unix"] >= EXPIRES
    # Positive trusted checks are handshakes; nothing the worker sent to a
    # trusted endpoint was anything but connect, handshake, establish or check.
    worker_ops = {value["op"] for role, value in host.requests if role == "worker"}
    assert worker_ops <= {
        "hello",
        "listen",
        "accept",
        "handshake",
        "connect",
        "establish",
        "check",
        "exit",
    }
    for role, value in host.requests:
        if value.get("op") == "proxy_forward":
            assert role == "candidate" and (value["address"], value["port"]) == PROXY
        if (value.get("address"), value.get("port")) in TRUSTED:
            assert value["op"] in ("handshake", "connect", "establish")
    # Two gated ExecStopPost probes, each measured before it ran.
    assert host.gate_opened == 2
    assert_cleaned(host)
    raw = json.dumps(record)
    for address in ("10.20.0.7", "10.30.0.4", "172.18.0.2", "1.1.1.1", "10.0.2.2"):
        assert address not in raw
    assert not [key for key in base.keys_of(record) if base.FORBIDDEN_KEY.search(key)]


def test_main_retains_the_record_and_prints_no_approval(cw, capsys):
    host = cw.host()
    code = COLLECTOR.main(
        ["network", "--config", str(CONFIG_PATH), "--confirm", COLLECTOR.CONFIRMATION],
        host_factory=lambda: host,
        checkout=cw.world.checkout,
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 0 and result["all_matched"] and result["approval_generated"] is False
    stored = (cw.world.store / result["record_sha256"]).read_bytes()
    assert EVIDENCE.parse_record_envelope(stored)["kind"] == "network_enforcement"
    assert (
        cw.world.verify([result["record_sha256"]])[0]["records"][0]["failure"] is None
    )


def test_missing_confirmation_is_refused():
    for kind in ("network", "resource", "preexec", "cleanup"):
        with pytest.raises(COLLECTOR.Refusal, match="exact confirmation"):
            COLLECTOR.main(
                [kind, "--config", str(CONFIG_PATH), "--confirm", "yes"],
                host_factory=pytest.fail,
            )
    # Each kind has its own confirmation.
    for kind, other in (
        ("resource", COLLECTOR.CONFIRMATION),
        ("preexec", COLLECTOR.RESOURCE_CONFIRMATION),
        ("cleanup", COLLECTOR.PREEXEC_CONFIRMATION),
    ):
        with pytest.raises(COLLECTOR.Refusal, match="exact confirmation"):
            COLLECTOR.main(
                [kind, "--config", str(CONFIG_PATH), "--confirm", other],
                host_factory=pytest.fail,
            )


# ---------------------------------------------------------------------------
# Enforcement failures produce an honest record the verifier refuses


@pytest.mark.parametrize(
    ("change", "probe_id", "observed"),
    [
        (
            lambda s: s.overrides.update(
                {("candidate", "connect", "1.1.1.1", 443): "connected"}
            ),
            "candidate.public",
            "connected",
        ),
        (
            lambda s: setattr(s, "router_remote", "10.30.0.4"),
            "candidate.router.source",
            "host_address",
        ),
        (
            lambda s: s.overrides.update(
                {("candidate", "proxy_forward", *PROXY): "proxy_forwarded"}
            ),
            "candidate.proxy.forward",
            "proxy_forwarded",
        ),
        (
            lambda s: s.overrides.update({("candidate", "dns"): "connected"}),
            "candidate.dns",
            "connected",
        ),
        (
            lambda s: s.overrides.update(
                {("daemon", "connect", "10.20.0.7", 5432): "connected"}
            ),
            "daemon.trusted",
            "connected",
        ),
        (
            lambda s: s.overrides.update(
                {("worker", "handshake", "10.20.0.9", 443): "handshake_failed"}
            ),
            "worker.trusted.handshake",
            "handshake_failed",
        ),
        # The candidate reports a timeout, but the sibling's own listener, outside
        # the candidate, saw the connection.
        (
            lambda s: s.listener_sees.add("sibling"),
            "candidate.sibling_attempt",
            "connected",
        ),
        (
            lambda s: s.listener_sees.add("worker"),
            "candidate.host_loopback",
            "connected",
        ),
        (
            lambda s: setattr(s, "check_outcome", "connected"),
            "worker.trusted.established_after_expiry",
            "connected",
        ),
        (
            lambda s: s.overrides.update(
                {("once", "connect", "10.20.0.7", 5432): "connected"}
            ),
            "worker.trusted.after_stop",
            "connected",
        ),
    ],
)
def test_each_enforcement_failure_is_recorded_and_refused(
    cw, change, probe_id, observed
):
    scenario = Scenario()
    change(scenario)
    record, host = cw.collect(scenario)
    entries = probe(record, probe_id)
    assert any(
        item["observed"] == {"outcome": observed} and item["matched"] is False
        for item in entries
    )
    failure = cw.verify(record)
    assert failure is not None and re.search("did not match", failure), failure
    assert_cleaned(host)


def test_main_exits_nonzero_for_an_unmatched_record(cw, capsys):
    scenario = Scenario()
    scenario.overrides[("candidate", "connect", "169.254.169.254", 80)] = "connected"
    code = COLLECTOR.main(
        ["network", "--config", str(CONFIG_PATH), "--confirm", COLLECTOR.CONFIRMATION],
        host_factory=lambda: cw.host(scenario),
        checkout=cw.world.checkout,
    )
    result = json.loads(capsys.readouterr().out)
    assert code == 3 and result["unmatched"] == ["candidate.metadata"]


# ---------------------------------------------------------------------------
# Refusals: no record at all


def tampered_scoped(port: int) -> bytes:
    return NFT_SCOPED.replace(b'"right": 5432', f'"right": {port}'.encode(), 1)


REFUSALS = [
    (
        lambda s: s.tampered_exe.add("candidate"),
        "candidate agent runs a binary other than",
    ),
    (lambda s: s.tampered_exe.add("worker"), "worker agent runs a binary other than"),
    (lambda s: s.tampered_exe.add("daemon"), "daemon agent runs a binary other than"),
    (
        lambda s: s.tampered_exe.add("once"),
        "worker stop probe runs a binary other than",
    ),
    (lambda s: s.lying.add("executor"), "executor agent self-report differs"),
    (
        lambda s: setattr(s, "nft_scoped", tampered_scoped(5433)),
        "differs from the connectivity profile's compiled policy",
    ),
    (
        lambda s: setattr(
            s,
            "nft_scoped",
            NFT_SCOPED.replace(
                b"ditto-coding-hosted-worker", b"ditto-coding-hosted-other"
            ),
        ),
        "compiled policy",
    ),
    (lambda s: setattr(s, "nft_deny", NFT_SCOPED), "not the deny guard"),
    (
        lambda s: setattr(
            s,
            "daemon_info_after",
            {
                **base.DAEMON_IDENTITY_VECTOR["info"],
                "ID": "5f0c2a8e-5b7d-4e19-9a61-2c4d8b0e7f15",
            },
        ),
        "daemon identity changed",
    ),
    (
        lambda s: setattr(s, "failed_start_fails", False),
        "worker stop probe did not run",
    ),
]


@pytest.mark.parametrize(("change", "reason"), REFUSALS)
def test_collection_refusals_leave_no_candidate_state(cw, change, reason):
    scenario = Scenario()
    change(scenario)
    with pytest.raises(COLLECTOR.Refusal, match=reason):
        cw.collect(scenario)
    assert_cleaned(cw.last_host)


PRESTART_REFUSALS = [
    (
        lambda s: setattr(s, "installed_runner_sha256", base.digest("other")),
        "installed probe runner differs",
    ),
    (lambda s: setattr(s, "worker_active_before", True), "worker is active"),
    (lambda s: setattr(s, "stray_container", True), "preconditions do not hold"),
    (lambda s: setattr(s, "proxy_active", False), "refusing proxy is not running"),
    (lambda s: setattr(s, "preflight_age", 901), "stale"),
    (
        lambda s: setattr(s, "profile_raw", PROFILE_RAW.replace(b"5432", b"5433")),
        "installed connectivity profile differs",
    ),
]


@pytest.mark.parametrize(("change", "reason"), PRESTART_REFUSALS)
def test_prestart_refusals_start_nothing(cw, change, reason):
    scenario = Scenario()
    change(scenario)
    with pytest.raises(COLLECTOR.Refusal, match=reason):
        cw.collect(scenario)
    started = [
        command
        for command in cw.last_host.commands
        if command[:2] == ("systemctl", "start")
        or command[:3] == ("docker", "network", "create")
    ]
    assert started == [] and cw.last_host.containers == (
        {"leftover": cw.last_host.containers["leftover"]}
        if "leftover" in cw.last_host.containers
        else {}
    )


def test_profile_window_and_prerequisites_are_bound(cw):
    long = json.dumps({**PROFILE, "expires_at_unix": ISSUED + 600}).encode()
    scenario = Scenario()
    scenario.profile_raw = long
    host = cw.host(scenario)
    read = host.read
    host.read = lambda path: long if path == PROFILE_PATH else read(path)
    collector = COLLECTOR.Collector(
        host, cw.collector_config, checkout=cw.world.checkout
    )
    with pytest.raises(COLLECTOR.Refusal, match="window does not fit"):
        collector.collect()
    host = cw.host()
    read = host.read
    other = json.dumps(
        {**PREREQUISITES, "egress_proxy": "http://10.30.0.5:3129"}
    ).encode()
    host.read = lambda path: (
        other if path == COLLECTOR.PREREQUISITES_PATH else read(path)
    )
    collector = COLLECTOR.Collector(
        host, cw.collector_config, checkout=cw.world.checkout
    )
    with pytest.raises(
        COLLECTOR.Refusal, match="not exactly the router and refusing proxy"
    ):
        collector.collect()


def test_config_is_closed_and_shadow_only(cw):
    good = json.dumps(cw.collector_config).encode()
    COLLECTOR.parse_config(good)
    for change in (
        {"shadow_only": False},
        {"approved": True},
        {"store": "relative/store"},
        {"candidate_image_language": "java"},
        {"boot_id": "not-a-boot"},
    ):
        with pytest.raises(COLLECTOR.Refusal):
            COLLECTOR.parse_config(
                json.dumps({**cw.collector_config, **change}).encode()
            )


# ---------------------------------------------------------------------------
# Verifier binding and tamper refusals


VERIFIER_TAMPERS = [
    (
        lambda r: r["network_binding"].update(scoped_output_rules=12),
        "scoped_output_rules differs",
    ),
    (
        lambda r: r["network_binding"].update(scoped_input_rules=2),
        "scoped_input_rules differs",
    ),
    (
        lambda r: r["network_binding"].update(
            deny_ruleset_sha256=r["network_binding"]["scoped_ruleset_sha256"]
        ),
        "scoped and deny rulesets are the same",
    ),
    (
        lambda r: r["network_binding"].update(
            worker_cgroup="system.slice/other.service"
        ),
        "another cgroup",
    ),
    (
        lambda r: r["network_binding"].update(nft_table="inet filter"),
        "another cgroup, table or proxy",
    ),
    (
        lambda r: r["network_binding"].update(refusing_proxy_sha256="0" * 63),
        "refusing_proxy_sha256 is malformed",
    ),
    (lambda r: r["network_binding"].pop("refusing_proxy_unit"), "network binding keys"),
    (lambda r: r.pop("network_binding"), "closed set"),
    (
        lambda r: r["tools"].update(probe_runner_binary_sha256=base.digest("x")),
        "probe runner binary differs",
    ),
    (lambda r: r["host"].update(boot_id=base.OTHER_BOOT), "boot differs"),
    (lambda r: r.update(not_covered=["reboot_recovery"]), "beyond the same boot"),
]


@pytest.mark.parametrize(("change", "reason"), VERIFIER_TAMPERS)
def test_verifier_refuses_a_tampered_collected_record(cw, change, reason):
    record, _ = cw.collect()
    tampered = copy.deepcopy(record)
    change(tampered)
    failure = cw.verify(tampered)
    assert failure is not None and reason in failure, failure


def test_verifier_refuses_a_misreported_match_and_a_bound_resource_record(cw):
    record, _ = cw.collect()
    tampered = copy.deepcopy(record)
    entry = probe(tampered, "candidate.public")[0]
    entry["observed"] = {"outcome": "connected"}
    failure = cw.verify(tampered)
    assert failure is not None and "misreported" in failure
    resource = copy.deepcopy(cw.world.records["resource_enforcement"])
    resource["network_binding"] = copy.deepcopy(record["network_binding"])
    failure = cw.verify(resource)
    assert failure is not None and "closed set" in failure


# ---------------------------------------------------------------------------
# nft analysis on recorded kernel listings


def test_compiled_policy_equals_the_recorded_kernel_listing():
    assert COLLECTOR.normalize_ruleset(NFT_SCOPED) == COLLECTOR.compiled_rules(
        PROFILE, UID
    )
    for mutate in (
        lambda p: p.update(trusted_loopback_tcp=False),
        lambda p: p["trusted_tcp"].append({"address": "10.20.0.8", "port": 5432}),
        lambda p: p.update(expires_at_unix=p["expires_at_unix"] + 1),
    ):
        other = copy.deepcopy(PROFILE)
        mutate(other)
        with pytest.raises(COLLECTOR.Refusal, match="compiled policy"):
            COLLECTOR.scoped_ruleset(NFT_SCOPED, other, UID)
    with pytest.raises(COLLECTOR.Refusal, match="compiled policy"):
        COLLECTOR.scoped_ruleset(NFT_SCOPED, PROFILE, 1002)


def test_deny_digest_is_stable_across_a_worker_cycle_but_raw_listings_are_not():
    # Handles change and the scoped chains and sets survive a flush, so a raw
    # listing digest (preflight v3's nft_snapshot_sha256) differs after any
    # worker start; the normalized deny digest and, once the elements expire,
    # the preflight's semantic digest do not.
    assert (
        hashlib.sha256(NFT_DENY_INITIAL).digest() != hashlib.sha256(NFT_DENY).digest()
    )
    assert nft_semantic_sha256(NFT_DENY_INITIAL) == nft_semantic_sha256(
        NFT_DENY_AFTER_EXPIRY
    )
    assert COLLECTOR.deny_ruleset(NFT_DENY_INITIAL, UID) == COLLECTOR.deny_ruleset(
        NFT_DENY, UID
    )
    with pytest.raises(COLLECTOR.Refusal, match="not the deny guard"):
        COLLECTOR.deny_ruleset(NFT_DENY, 1002)
    with pytest.raises(
        COLLECTOR.Refusal, match="exactly the ditto_coding_hosted table"
    ):
        COLLECTOR.normalize_ruleset(json.dumps({"nftables": []}).encode())


def test_collector_and_preflight_share_one_nft_normalization():
    assert str(PREFLIGHT_PATH.relative_to(ROOT)) == COLLECTOR.PREFLIGHT_TOOL
    assert Path(COLLECTOR.PREFLIGHT.__file__).resolve() == PREFLIGHT_PATH
    assert "def _strip" not in COLLECTOR_PATH.read_text()
    for name in NFT_FIXTURE_NAMES:
        raw = (FIXTURES / name).read_bytes()
        assert COLLECTOR.normalize_ruleset(raw) == COLLECTOR.PREFLIGHT.nft_entries(
            json.loads(raw)
        ), name
        # The semantic digest is a pruned, sorted view of those same entries.
        assert (
            nft_semantic_sha256(raw)
            == hashlib.sha256(
                json.dumps(
                    COLLECTOR.PREFLIGHT.nft_semantic_entries(
                        COLLECTOR.normalize_ruleset(raw)
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        ), name


def test_verifier_accepts_the_recorded_cycle_and_refuses_a_ruleset_difference(cw):
    record, _host = cw.collect()
    assert cw.verify(record) is None

    def post_with(raw: bytes) -> str | None:
        value = base.preflight_value(
            cw.world.preflight_tools, T0 + 4000, nft_semantic_sha256(raw)
        )
        sha = cw.world.put(base.stdout_bytes(value))
        result, _ok = cw.world.verify([cw.world.put(base.canonical(record))], sha)
        return result["records"][0]["failure"]

    assert post_with(NFT_DENY_INITIAL) is None
    listing = json.loads(NFT_DENY_AFTER_EXPIRY)
    listing["nftables"].append(
        {
            "rule": {
                "family": "inet",
                "table": COLLECTOR.TABLE,
                "chain": "scoped_output",
                "handle": 40,
                "expr": [
                    {
                        "match": {
                            "op": "==",
                            "left": {"meta": {"key": "skuid"}},
                            "right": UID,
                        }
                    },
                    {"accept": None},
                ],
            }
        }
    )
    drifted = {
        "added accept rule": json.dumps(listing).encode(),
        "unexpired scoped sets": NFT_DENY,
        "scoped policy still loaded": NFT_SCOPED,
    }
    for label, raw in drifted.items():
        failure = post_with(raw)
        assert failure is not None, label
        assert "nft_ruleset_semantic_sha256 differs from the host preflight" in failure


def test_verifier_rule_counts_mirror_the_compiled_policy():
    parsed = EVIDENCE.parse_connectivity_profile(PROFILE_RAW)
    _, output, inputs = COLLECTOR.scoped_ruleset(NFT_SCOPED, PROFILE, UID)
    assert (parsed["scoped_output_rules"], parsed["scoped_input_rules"]) == (
        output,
        inputs,
    )


def test_proc_and_systemd_parsers():
    assert COLLECTOR.parse_net_dev(b"h\nh\n  lo: 1 2\n eth0: 3\n") == ["eth0", "lo"]
    with pytest.raises(COLLECTOR.Refusal):
        COLLECTOR.parse_net_dev(b"h\nh\n 10.0.0.1: 3\n")
    assert COLLECTOR.parse_status_ids(b"Uid:\t5\t5\t5\t5\nGid:\t6\t6\t6\t6\n") == (5, 6)
    with pytest.raises(COLLECTOR.Refusal, match="uids differ"):
        COLLECTOR.parse_status_ids(b"Uid:\t5\t0\t5\t5\nGid:\t6\t6\t6\t6\n")
    assert (
        COLLECTOR.parse_cgroup(b"0::/system.slice/x.service\n")
        == "system.slice/x.service"
    )
    ids = {
        "uid_start": 100000,
        "uid_count": 65536,
        "gid_start": 100000,
        "gid_count": 65536,
    }
    assert COLLECTOR.container_ids((165531, 165531), ids) == {
        "uid": 65532,
        "gid": 65532,
    }
    with pytest.raises(COLLECTOR.Refusal):
        COLLECTOR.container_ids((1001, 1001), ids)
    assert (
        COLLECTOR.classify_source("172.18.0.2", "172.18.0.2", {"10.30.0.4"})
        == "container_address"
    )
    assert (
        COLLECTOR.classify_source("10.30.0.4", "172.18.0.2", {"10.30.0.4"})
        == "host_address"
    )
    assert (
        COLLECTOR.classify_source("10.0.2.100", "172.18.0.2", {"10.30.0.4"})
        == "other_address"
    )


def test_targets_fixture_is_bound_into_the_fixture_tree():
    raw = (ROOT / COLLECTOR.TARGETS_FIXTURE).read_bytes()
    targets = COLLECTOR.parse_targets(raw)
    assert COLLECTOR.TARGETS_FIXTURE.startswith(EVIDENCE.FIXTURE_ROOT + "/")
    for change in (
        {"public": {"address": "10.0.0.1", "port": 443}},
        {"metadata": {"address": "1.1.1.1", "port": 80}},
        {"extra": 1},
    ):
        with pytest.raises((COLLECTOR.Refusal, ValueError)):
            COLLECTOR.parse_targets(json.dumps({**targets, **change}).encode())


def test_collector_uses_only_fixed_binaries_and_never_approves():
    source = COLLECTOR_PATH.read_text()
    binaries = set(re.findall(r'"(/usr/(?:s?bin)/[a-z0-9-]+)"', source))
    assert binaries == {
        "/usr/bin/docker",
        "/usr/bin/systemctl",
        "/usr/bin/systemd-run",
        "/usr/sbin/nft",
        "/usr/bin/python3",
    }
    assert "shell=True" not in source
    for forbidden in ("native_approval", "curator", "gcloud", "ssh "):
        assert forbidden not in source
    workflow = ROOT / ".github/workflows/coding-hosted-operate.yml"
    if workflow.exists():
        assert "collect-coding-native-enforcement" not in workflow.read_text()


# ---------------------------------------------------------------------------
# Live kernel listing (throwaway user and network namespace; CI requires it)


def _live_nft_available() -> bool:
    if not shutil.which("unshare") or not Path("/usr/sbin/nft").exists():
        return False
    result = subprocess.run(
        ["unshare", "-rn", "/usr/sbin/nft", "list", "ruleset"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def test_live_kernel_listing_matches_the_compiled_policy(tmp_path):
    if not _live_nft_available():
        if os.environ.get("DITTOBENCH_REQUIRE_LIVE_NFT") == "1":
            pytest.fail("unshare -rn nft is required in CI")
        pytest.skip("unprivileged user and network namespaces are unavailable")
    policy = base_load("live_policy", POLICY_PATH)
    host_policy = base_load("live_host_policy", HOST_POLICY_PATH)
    cgroup = Path("/proc/self/cgroup").read_text().strip().removeprefix("0::/")
    if not cgroup:
        pytest.skip("the test runs in the root cgroup")
    parts = cgroup.split("/")
    worker = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
    uid = 1001
    daemon = cgroup
    profile = copy.deepcopy(PROFILE)
    scoped = policy.policy(profile, uid, ISSUED).replace(policy.WORKER, worker)
    scoped = scoped.replace(f"user.slice/user-{uid}.slice/user@{uid}.service", daemon)
    (tmp_path / "deny.nft").write_text(host_policy.nft_policy(uid))
    (tmp_path / "scoped.nft").write_text(scoped)
    # A second scoped load whose elements expire after one second, so the
    # final listing is the post-expiry deny guard the post-collection
    # preflight sees.
    expiring = policy.policy(profile, uid, EXPIRES - 1).replace(policy.WORKER, worker)
    expiring = expiring.replace(
        f"user.slice/user-{uid}.slice/user@{uid}.service", daemon
    )
    (tmp_path / "expiring.nft").write_text(expiring)
    script = (
        "set -e; list='nft -j list table inet ditto_coding_hosted'; "
        "nft -f deny.nft; $list > d0.json; nft -f scoped.nft; $list > s.json; "
        "nft -f deny.nft; $list > d1.json"
    )
    # A fresh namespace: re-adding an existing element keeps its first timeout.
    cycle = (
        "set -e; nft -f deny.nft; nft -f expiring.nft; nft -f deny.nft; sleep 2; "
        "nft -j list table inet ditto_coding_hosted > d2.json"
    )
    for commands in (script, cycle):
        subprocess.run(
            ["unshare", "-rn", "sh", "-c", commands],
            cwd=tmp_path,
            check=True,
            env={"PATH": "/usr/sbin:/usr/bin:/bin", "TZ": "UTC"},
        )
    listing = (tmp_path / "s.json").read_bytes()
    digest, output, inputs = COLLECTOR.scoped_ruleset(
        listing, profile, uid, worker_cgroup=worker, daemon_cgroup=daemon
    )
    assert (output, inputs) == (11, 3)
    assert COLLECTOR.deny_ruleset(
        (tmp_path / "d0.json").read_bytes(), uid
    ) == COLLECTOR.deny_ruleset((tmp_path / "d1.json").read_bytes(), uid)
    assert (tmp_path / "d0.json").read_bytes() != (tmp_path / "d1.json").read_bytes()
    d0, d2 = (tmp_path / "d0.json").read_bytes(), (tmp_path / "d2.json").read_bytes()
    assert d0 != d2
    assert nft_semantic_sha256(d0) == nft_semantic_sha256(d2)
    assert nft_semantic_sha256(d0) == nft_semantic_sha256(NFT_DENY_INITIAL)
    assert nft_semantic_sha256(d2) == nft_semantic_sha256(NFT_DENY_AFTER_EXPIRY)
    assert nft_semantic_sha256((tmp_path / "d1.json").read_bytes()) != (
        nft_semantic_sha256(d0)
    )


def base_load(name: str, path: Path):
    loader = importlib.util.spec_from_file_location(name, path)
    assert loader is not None and loader.loader is not None
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    return module
