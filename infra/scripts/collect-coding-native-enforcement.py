#!/usr/bin/env python3
"""Default-off root collector for native enforcement evidence (B5 PR4: network).

Only ``network`` is implemented. Resource, pre-exec and cleanup collection are
refused (PR5). Nothing here mints approval: the collector retains one
``network_enforcement`` record in the evidence store, and Peyton reviews it with
``coding-native-evidence.py verify`` against a post-collection preflight.

Run only on ``ditto-coding-hosted-v2``, as root, from the reviewed release
checkout, with the worker unit installed in ``network_enforcement`` mode, the
refusing proxy running and an expiring probe connectivity profile installed.
It is never invoked from ``coding-hosted-operate`` or any CI workflow.

What it measures from outside the candidate container:

- the probe runner file it will execute (release-recorded digest), and the
  ``/proc/<pid>/exe`` of every agent process it drives (worker cgroup, daemon
  cgroup, candidate, sibling and executor containers, and the ExecStopPost
  one-shot probes), refusing any self-report that differs;
- the loaded ``inet ditto_coding_hosted`` nft table, compared rule for rule with
  the profile's compiled policy and hashed after normalization;
- the worker cgroup of the router listener, which also records every candidate
  connection's source address (never written to the record);
- candidate identity, host ids and executor interfaces from ``/proc``;
- the refusing proxy unit, its cgroup and the script it executes;
- the Docker daemon identity through the pinned socket and empty client
  configuration, before and after collection.

Candidate-side denials are attempted by the same measured binary running as
the candidate identity inside the container; where a listener exists outside
(router, host loopback, sibling) the collector also requires that the listener
saw the connection, or did not.

Coverage is the same boot only. Daemon restart and reboot recovery are not
covered, and every record says so.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import selectors
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_TOOL = "infra/scripts/coding-native-evidence.py"
PREFLIGHT_TOOL = "infra/scripts/inspect-coding-native-host.py"
HOST_POLICY_TOOL = "infra/ansible/roles/coding_hosted/files/host-policy.py"
TARGETS_FIXTURE = (
    "services/dittobench-api/internal/codingenforcement/fixtures/network/targets.json"
)

CONFIRMATION = "COLLECT NATIVE NETWORK ENFORCEMENT EVIDENCE"
CONFIG_SCHEMA = "dittobench-coding-native-network-collection-config-v1"
TARGETS_SCHEMA = "dittobench-coding-native-network-targets-v1"
AGENT_SCHEMA = "dittobench-coding-native-net-agent-v1"
PLAN_SCHEMA = "dittobench-coding-native-net-plan-v1"
REPORT_SCHEMA = "dittobench-coding-native-net-report-v1"
PREREQUISITES_SCHEMA = "dittobench-coding-hosted-host-prerequisites-v2"

HOSTNAME = "ditto-coding-hosted-v2"
USER = "ditto-coding-hosted"
WORKER_UNIT = "ditto-coding-hosted-worker.service"
WORKER_CGROUP = "system.slice/ditto-coding-hosted-worker.service"
EGRESS_UNIT = "ditto-coding-hosted-egress.service"
PROXY_UNIT = "ditto-coding-hosted-egress-proxy.service"
PROXY_SCRIPT = "/usr/local/lib/ditto-coding-hosted/egress-proxy.py"
TABLE = "ditto_coding_hosted"
SOCKET = "/run/ditto-coding-hosted/docker.sock"
DOCKER_CONFIG = "/var/lib/ditto-coding-hosted/empty-client"
RUNTIME_BASE = Path("/opt/ditto-coding-hosted")
RUNNER_RELATIVE = "bin/dittobench-coding-enforcement-probe"
CONTAINER_RUNNER = "/opt/dittobench-probe/runner"
AGENT_DIR = Path("/run/ditto-coding-hosted-enforcement")
AGENT_SOCKET = AGENT_DIR / "agent.sock"
PLAN = AGENT_DIR / "plan.json"
REPORT = AGENT_DIR / "report.json"
GATE = AGENT_DIR / "gate"
CONNECTIVITY_PATH = Path("/etc/ditto-coding-hosted/connectivity.json")
PREREQUISITES_PATH = Path("/usr/local/lib/ditto-coding-hosted/host-prerequisites.json")
CUSTODY_RUN = Path("/run/ditto-coding-custody")
RUN_LABEL = "io.heyditto.dittobench.run"
HARNESS_UID = 65532
EXECUTOR_UID = 10001
ROUTER_NAMESPACE = "host"

# Timing. The probe profile must be issued just before collection, expire after
# the stop phase, and be re-installable (issued at most 300 s earlier) at the
# expiry phase's worker start.
PROFILE_START_MAX_AGE = 60
PROFILE_MIN_WINDOW = 120
PROFILE_MAX_WINDOW = 280
INSTALL_MAX_AGE = 300
EXPIRY_LEAD = 20
EXPIRY_GRACE = 3
POLL_SECONDS = 0.2
UNIT_WAIT_SECONDS = 60
MAX_LINE = 4096
MAX_FILE = 1 << 20

HEX64 = re.compile(r"[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")
BOOT_ID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
INTERFACE = re.compile(r"[a-z][a-z0-9_-]{0,14}")
DENIAL_OUTCOMES = ("not_permitted", "refused", "reset", "timeout", "unreachable")


class Refusal(ValueError):
    """Collection refused; no record is written."""


def require(condition: object, reason: str) -> None:
    if not condition:
        raise Refusal(reason)


def load_module(name: str, relative: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The reviewed preflight from this same checkout: daemon identity and nft
# listing normalization are shared with it, not reimplemented.
PREFLIGHT = load_module("native_preflight_for_collector", PREFLIGHT_TOOL)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def parse_unique(raw: bytes, label: str) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            require(key not in result, f"{label} repeats a key")
            result[key] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeDecodeError):
        raise Refusal(f"{label} is not JSON") from None


def closed(value: object, keys: set[str], label: str) -> dict[str, Any]:
    require(type(value) is dict and set(value) == keys, f"{label} keys differ")
    assert isinstance(value, dict)
    return value


# ---------------------------------------------------------------------------
# Inputs


def parse_config(raw: bytes) -> dict[str, Any]:
    keys = {
        "schema",
        "source_revision",
        "release_directory",
        "release_manifest_sha256",
        "machine_id_sha256",
        "boot_id",
        "store",
        "pre_collection_preflight_sha256",
        "connectivity_profile",
        "candidate_image_language",
        "shadow_only",
        "weight_eligible",
    }
    value = closed(parse_unique(raw, "collector config"), keys, "collector config")
    require(value["schema"] == CONFIG_SCHEMA, "collector config schema differs")
    require(
        value["shadow_only"] is True and value["weight_eligible"] is False,
        "collector config must be shadow-only",
    )
    require(
        type(value["source_revision"]) is str
        and REVISION.fullmatch(value["source_revision"]),
        "collector source revision is malformed",
    )
    for name in (
        "release_manifest_sha256",
        "machine_id_sha256",
        "pre_collection_preflight_sha256",
    ):
        require(
            type(value[name]) is str and HEX64.fullmatch(value[name]),
            f"collector {name} is malformed",
        )
    require(
        type(value["boot_id"]) is str and BOOT_ID.fullmatch(value["boot_id"]),
        "collector boot id is malformed",
    )
    for name in ("release_directory", "store", "connectivity_profile"):
        path = value[name]
        require(
            type(path) is str
            and path.startswith("/")
            and os.path.normpath(path) == path
            and ".." not in path.split("/"),
            f"collector {name} is not a clean absolute path",
        )
    require(
        value["candidate_image_language"] in ("go", "node", "python", "rust"),
        "collector candidate image language is unknown",
    )
    return value


def parse_targets(raw: bytes) -> dict[str, Any]:
    """Fixed reviewed probe destinations; hashed into ``fixtures_sha256``."""

    keys = {
        "schema",
        "public",
        "ipv6",
        "metadata",
        "dns_name",
        "proxy_target",
        "docker_api_port",
        "host_loopback_alias",
        "host_loopback_port",
        "sibling_port",
        "timeout_ms",
        "check_timeout_ms",
    }
    value = closed(parse_unique(raw, "network targets"), keys, "network targets")
    require(value["schema"] == TARGETS_SCHEMA, "network targets schema differs")
    for name, version in (("public", 4), ("ipv6", 6), ("metadata", 4)):
        endpoint = closed(value[name], {"address", "port"}, f"target {name}")
        ip = ipaddress.ip_address(endpoint["address"])
        require(
            ip.version == version
            and str(ip) == endpoint["address"]
            and type(endpoint["port"]) is int
            and 1 <= endpoint["port"] <= 65535,
            f"target {name} is malformed",
        )
        require(
            ip.is_link_local if name == "metadata" else ip.is_global,
            f"target {name} is not a {name} address",
        )
    require(
        type(value["dns_name"]) is str
        and re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", value["dns_name"]),
        "target dns name is malformed",
    )
    require(
        type(value["proxy_target"]) is str
        and re.fullmatch(r"[a-z0-9.-]+:[1-9][0-9]{0,4}", value["proxy_target"]),
        "target proxy authority is malformed",
    )
    alias = ipaddress.ip_address(value["host_loopback_alias"])
    require(
        alias.version == 4 and str(alias) == value["host_loopback_alias"],
        "host loopback alias is malformed",
    )
    for name in ("docker_api_port", "host_loopback_port", "sibling_port"):
        require(
            type(value[name]) is int and 1024 <= value[name] <= 65535,
            f"target {name} is malformed",
        )
    for name in ("timeout_ms", "check_timeout_ms"):
        require(
            type(value[name]) is int and 100 <= value[name] <= 30000,
            f"target {name} is malformed",
        )
    return value


def endpoint_text(text: object, label: str) -> tuple[str, int]:
    require(type(text) is str and text.count(":") == 1, f"{label} is malformed")
    assert isinstance(text, str)
    address, port = text.split(":")
    ip = ipaddress.IPv4Address(address)
    require(
        str(ip) == address and port.isdecimal() and 1024 <= int(port) <= 65535,
        f"{label} is malformed",
    )
    return address, int(port)


def parse_prerequisites(raw: bytes) -> tuple[tuple[str, int], tuple[str, int]]:
    keys = {
        "schema",
        "shadow_only",
        "weight_eligible",
        "router_listen",
        "egress_network",
        "egress_proxy",
        "candidate_uid",
        "candidate_gid",
    }
    value = closed(parse_unique(raw, "host prerequisites"), keys, "host prerequisites")
    require(value["schema"] == PREREQUISITES_SCHEMA, "host prerequisites differ")
    require(
        type(value["egress_proxy"]) is str
        and value["egress_proxy"].startswith("http://"),
        "egress proxy is malformed",
    )
    router = endpoint_text(value["router_listen"], "router listener")
    proxy = endpoint_text(value["egress_proxy"].removeprefix("http://"), "proxy")
    require(router != proxy, "router and proxy are the same listener")
    return router, proxy


# ---------------------------------------------------------------------------
# nft ruleset: normalization, exact compiled-policy comparison and digests


def normalize_ruleset(raw: bytes) -> list[dict[str, Any]]:
    """``nft -j list table`` without metainfo, handles, counters or timeouts.

    Stripping is the preflight's own (``inspect-coding-native-host.py``
    ``nft_entries``), kept in kernel order for the exact compiled-policy
    comparison; the preflight additionally prunes and sorts for its semantic
    digest.
    """

    entries = PREFLIGHT.nft_entries(parse_unique(raw, "nft listing"))
    require(entries is not None, "nft listing is malformed")
    tables = [entry["table"] for entry in entries if "table" in entry]
    require(
        len(tables) == 1 and tables[0] == {"family": "inet", "name": TABLE},
        "nft listing is not exactly the ditto_coding_hosted table",
    )
    return entries


def _match(left: dict[str, Any], right: Any, op: str = "==") -> dict[str, Any]:
    return {"match": {"op": op, "left": left, "right": right}}


def _meta(key: str) -> dict[str, Any]:
    return {"meta": {"key": key}}


def _payload(protocol: str, field: str) -> dict[str, Any]:
    return {"payload": {"protocol": protocol, "field": field}}


def _ct(key: str) -> dict[str, Any]:
    return {"ct": {"key": key}}


COUNTER: dict[str, Any] = {"counter": {"packets": 0, "bytes": 0}}
REJECT: dict[str, Any] = {"reject": {"type": "icmpx", "expr": "admin-prohibited"}}


def _utc(moment: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(moment))


def compiled_rules(
    profile: dict[str, Any],
    uid: int,
    *,
    worker_cgroup: str = WORKER_CGROUP,
    daemon_cgroup: str | None = None,
) -> list[dict[str, Any]]:
    """The normalized entries ``connectivity-policy.py policy`` loads (nft TZ=UTC).

    Mirrors that function statement by statement; a test loads its real output
    into a kernel namespace and compares.
    """

    daemon_cgroup = daemon_cgroup or f"user.slice/user-{uid}.slice/user@{uid}.service"
    issued, expires = profile["issued_at_unix"], profile["expires_at_unix"]
    encoded = json.dumps(profile, sort_keys=True, separators=(",", ":")).encode()
    worker_mark = int.from_bytes(hashlib.sha256(encoded).digest()[:4], "big")
    worker_mark = (worker_mark & 0x7FFFFFFF) or 1
    daemon_mark = worker_mark | 0x80000000

    def pairs(name: str) -> list[tuple[str, int]]:
        return sorted((item["address"], item["port"]) for item in profile[name])

    def chain(name: str, hook: str, prio: int) -> dict[str, Any]:
        return {
            "chain": {
                "family": "inet",
                "table": TABLE,
                "name": name,
                "type": "filter",
                "hook": hook,
                "prio": prio,
                "policy": "accept",
            }
        }

    def timed_set(name: str, kind: str, element: Any) -> dict[str, Any]:
        return {
            "set": {
                "family": "inet",
                "name": name,
                "table": TABLE,
                "type": kind,
                "flags": ["timeout"],
                "elem": [{"elem": {"val": element}}],
            }
        }

    def rule(chain_name: str, expr: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "rule": {
                "family": "inet",
                "table": TABLE,
                "chain": chain_name,
                "expr": expr,
            }
        }

    skuid = _match(_meta("skuid"), uid)
    window = [
        _match(_meta("time"), _utc(issued), ">="),
        _match(_meta("time"), _utc(expires), "<"),
    ]
    worker = [skuid, *window, _match({"socket": {"key": "cgroupv2"}}, "@worker")]
    daemon = [skuid, *window, _match({"socket": {"key": "cgroupv2"}}, "@daemon")]
    reply = [skuid, *window, _match(_meta("skuid"), "@lease")]
    accept: list[dict[str, Any]] = [COUNTER, {"accept": None}]
    established: list[dict[str, Any]] = [
        _match(_ct("direction"), "reply"),
        _match(_ct("state"), "established", "in"),
    ]

    def mark_new(mark: int) -> list[dict[str, Any]]:
        return [
            _match(_ct("direction"), "original"),
            _match(_ct("state"), "new", "in"),
            _match(_ct("mark"), 0),
            {"mangle": {"key": _ct("mark"), "value": mark}},
        ]

    output: list[dict[str, Any]] = []
    loopback = _match(_payload("ip", "daddr"), "127.0.0.1")
    tcp_proto = _match(_meta("l4proto"), "tcp")
    if profile["trusted_loopback_tcp"]:
        output.append(rule("scoped_output", [*worker, loopback, tcp_proto, *accept]))
    for address, port in pairs("trusted_tcp"):
        output.append(
            rule(
                "scoped_output",
                [
                    *worker,
                    _match(_payload("ip", "daddr"), address),
                    _match(_payload("tcp", "dport"), port),
                    *accept,
                ],
            )
        )
    for address, port in pairs("trusted_dns"):
        for protocol in ("tcp", "udp"):
            output.append(
                rule(
                    "scoped_output",
                    [
                        *worker,
                        _match(_payload("ip", "daddr"), address),
                        _match(_payload(protocol, "dport"), port),
                        *accept,
                    ],
                )
            )
    inputs: list[dict[str, Any]] = []
    for address, port in pairs("candidate_tcp"):
        output.append(
            rule(
                "scoped_output",
                [
                    *daemon,
                    _match(_payload("ip", "daddr"), address),
                    _match(_payload("tcp", "dport"), port),
                    *accept,
                ],
            )
        )
        inputs.append(
            rule(
                "scoped_input",
                [
                    *window,
                    _match(_payload("ip", "daddr"), address),
                    _match(_payload("tcp", "dport"), port),
                    _match({"socket": {"key": "cgroupv2"}}, "@worker"),
                    *mark_new(worker_mark),
                ],
            )
        )
        output.append(
            rule(
                "scoped_output",
                [
                    *reply,
                    _match(_payload("ip", "saddr"), address),
                    _match(_payload("tcp", "sport"), port),
                    *established,
                    _match(_ct("mark"), worker_mark),
                    *accept,
                ],
            )
        )
    inputs.append(
        rule(
            "scoped_input",
            [
                *window,
                loopback,
                tcp_proto,
                _match({"socket": {"key": "cgroupv2"}}, "@daemon"),
                *mark_new(daemon_mark),
            ],
        )
    )
    output.append(
        rule(
            "scoped_output",
            [
                *reply,
                loopback,
                tcp_proto,
                *established,
                _match(_ct("mark"), daemon_mark),
                *accept,
            ],
        )
    )
    output.append(rule("scoped_output", [skuid, COUNTER, REJECT]))
    return [
        {"table": {"family": "inet", "name": TABLE}},
        timed_set("lease", "uid", uid),
        timed_set("worker", "cgroupsv2", worker_cgroup),
        timed_set("daemon", "cgroupsv2", daemon_cgroup),
        chain("output", "output", -310),
        chain("scoped_output", "output", -150),
        chain("scoped_input", "input", -150),
        *output,
        *inputs,
    ]


def _entries_by(entries: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [entry for entry in entries if kind in entry]


def scoped_ruleset(
    raw: bytes, profile: dict[str, Any], uid: int, **cgroups: str
) -> tuple[str, int, int]:
    """Refuse unless the loaded table is exactly the profile's compiled policy."""

    entries = normalize_ruleset(raw)
    expected = compiled_rules(profile, uid, **cgroups)
    require(
        entries == expected,
        "loaded nft ruleset differs from the connectivity profile's compiled policy",
    )
    rules = _entries_by(entries, "rule")
    output = sum(1 for entry in rules if entry["rule"]["chain"] == "scoped_output")
    inputs = sum(1 for entry in rules if entry["rule"]["chain"] == "scoped_input")
    return canonical_digest(entries), output, inputs


def deny_ruleset(raw: bytes, uid: int) -> str:
    """The restored deny guard: one UID reject in ``output``; other chains empty.

    Scoped chains and sets survive a ``flush table`` but are inert without
    rules, so the digest covers the chains and rules only.
    """

    entries = normalize_ruleset(raw)
    rules = _entries_by(entries, "rule")
    require(
        rules
        == [
            {
                "rule": {
                    "family": "inet",
                    "table": TABLE,
                    "chain": "output",
                    "expr": [_match(_meta("skuid"), uid), COUNTER, REJECT],
                }
            }
        ],
        "restored nft ruleset is not the deny guard",
    )
    chains = _entries_by(entries, "chain")
    require(
        {
            "chain": {
                "family": "inet",
                "table": TABLE,
                "name": "output",
                "type": "filter",
                "hook": "output",
                "prio": -310,
                "policy": "accept",
            }
        }
        in chains,
        "restored nft ruleset lacks the deny chain",
    )
    kept = [
        entry
        for entry in entries
        if "set" not in entry
        and ("chain" not in entry or entry["chain"]["name"] == "output")
    ]
    return canonical_digest(kept)


def canonical_digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


# ---------------------------------------------------------------------------
# /proc and systemd parsing (pure)


def parse_status_ids(raw: bytes) -> tuple[int, int]:
    uid = gid = None
    for line in raw.decode("ascii", "replace").splitlines():
        fields = line.split()
        if fields[:1] == ["Uid:"] and len(fields) == 5:
            require(len(set(fields[1:])) == 1, "process uids differ")
            uid = int(fields[1])
        if fields[:1] == ["Gid:"] and len(fields) == 5:
            require(len(set(fields[1:])) == 1, "process gids differ")
            gid = int(fields[1])
    require(uid is not None and gid is not None, "process status lacks ids")
    assert uid is not None and gid is not None
    return uid, gid


def parse_net_dev(raw: bytes) -> list[str]:
    names = []
    for line in raw.decode("ascii").splitlines()[2:]:
        name = line.split(":", 1)[0].strip()
        require(INTERFACE.fullmatch(name) is not None, "interface name is malformed")
        names.append(name)
    return sorted(names)


def parse_cgroup(raw: bytes) -> str:
    lines = raw.decode("ascii").strip().splitlines()
    require(
        len(lines) == 1 and lines[0].startswith("0::/"), "process is not in cgroup v2"
    )
    return lines[0][4:]


def parse_show(raw: bytes) -> dict[str, str]:
    result = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def container_ids(host_ids: tuple[int, int], ids: dict[str, int]) -> dict[str, int]:
    """Rootless maps container id c >= 1 to subordinate start + c - 1."""

    uid = host_ids[0] - ids["uid_start"] + 1
    gid = host_ids[1] - ids["gid_start"] + 1
    require(
        1 <= uid <= ids["uid_count"] and 1 <= gid <= ids["gid_count"],
        "container process is outside the subordinate range",
    )
    return {"uid": uid, "gid": gid}


def classify_source(remote: str, container: str, host_addresses: set[str]) -> str:
    if remote == container:
        return "container_address"
    if remote in host_addresses:
        return "host_address"
    return "other_address"


# ---------------------------------------------------------------------------
# Host adapter


class Session(Protocol):
    def request(self, value: dict[str, Any], timeout: float) -> dict[str, Any]: ...

    def close(self) -> None: ...


class Host(Protocol):
    """Every outside effect. Tests replace it with recorded observations."""

    def now(self) -> int: ...
    def sleep(self, seconds: float) -> None: ...
    def euid(self) -> int: ...
    def hostname(self) -> str: ...
    def machine_id(self) -> bytes: ...
    def boot_id(self) -> str: ...
    def kernel(self) -> str: ...
    def identity(self) -> tuple[int, int]: ...
    def read(self, path: Path) -> bytes: ...
    def file_sha256(self, path: Path) -> str: ...
    def exe_sha256(self, pid: int) -> str: ...
    def proc(self, pid: int, name: str) -> bytes: ...
    def children(self, pid: int) -> list[int]: ...
    def cgroup_procs(self, cgroup: str) -> list[int]: ...
    def systemctl(self, *args: str) -> tuple[int, bytes]: ...
    def docker(self, *args: str) -> bytes: ...
    def nft_table(self) -> bytes: ...
    def socket_present(self, directory: Path) -> bool: ...
    def prepare_agent_dir(self, uid: int, gid: int) -> None: ...
    def write_plan(self, raw: bytes, uid: int, gid: int) -> None: ...
    def open_gate(self) -> None: ...
    def read_report(self, uid: int) -> bytes: ...
    def remove_agent_dir(self) -> None: ...
    def worker_session(self) -> Session: ...
    def daemon_session(self, runner: Path, unit: str) -> Session: ...
    def container_session(self, arguments: list[str]) -> Session: ...


class ProcessSession:
    def __init__(self, arguments: list[str], **kwargs: Any) -> None:
        self.process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
        self.buffer = b""

    def request(self, value: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        self.process.stdin.flush()
        return _read_line(self, self.process.stdout.fileno(), timeout)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            if self.process.stdin is not None:
                self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


class SocketSession:
    def __init__(self, path: Path, deadline: float) -> None:
        self.buffer = b""
        while True:
            try:
                self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.socket.connect(str(path))
                break
            except OSError:
                self.socket.close()
                require(time.monotonic() < deadline, "worker agent socket unavailable")
                time.sleep(POLL_SECONDS)

    def request(self, value: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.socket.sendall(json.dumps(value).encode() + b"\n")
        return _read_line(self, self.socket.fileno(), timeout)

    def close(self) -> None:
        self.socket.close()


def _read_line(session: Any, fd: int, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while b"\n" not in session.buffer:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "agent did not answer in time")
            if selector.select(remaining):
                chunk = os.read(fd, 65536)
                require(chunk, "agent closed its session")
                session.buffer += chunk
                require(len(session.buffer) <= MAX_LINE * 4, "agent answer too long")
    line, session.buffer = session.buffer.split(b"\n", 1)
    value = parse_unique(line, "agent answer")
    require(type(value) is dict, "agent answer is malformed")
    return value


class SystemHost:
    """The real host. Fixed binaries, pinned Docker socket, bounded commands."""

    def __init__(self) -> None:
        import pwd

        self.user = pwd.getpwnam(USER)

    def now(self) -> int:
        return int(time.time())

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def euid(self) -> int:
        return os.geteuid()

    def hostname(self) -> str:
        return socket.gethostname()

    def machine_id(self) -> bytes:
        return Path("/etc/machine-id").read_bytes().strip()

    def boot_id(self) -> str:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    def kernel(self) -> str:
        return os.uname().release

    def identity(self) -> tuple[int, int]:
        return self.user.pw_uid, self.user.pw_gid

    def read(self, path: Path) -> bytes:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode), f"{path} is not a file")
            require(
                info.st_uid == 0 and not info.st_mode & 0o022,
                f"{path} is not root-protected",
            )
            raw = stream.read(MAX_FILE + 1)
        require(len(raw) <= MAX_FILE, f"{path} is too large")
        return raw

    def file_sha256(self, path: Path) -> str:
        return sha256(self.read(path))

    def exe_sha256(self, pid: int) -> str:
        # /proc/<pid>/exe opens the mapped inode even if the path was replaced.
        with open(f"/proc/{pid}/exe", "rb") as stream:
            digest = hashlib.sha256()
            while chunk := stream.read(1 << 20):
                digest.update(chunk)
        return digest.hexdigest()

    def proc(self, pid: int, name: str) -> bytes:
        return Path(f"/proc/{pid}/{name}").read_bytes()[:MAX_FILE]

    def children(self, pid: int) -> list[int]:
        text = Path(f"/proc/{pid}/task/{pid}/children").read_text()
        return [int(item) for item in text.split()]

    def cgroup_procs(self, cgroup: str) -> list[int]:
        try:
            text = (Path("/sys/fs/cgroup") / cgroup / "cgroup.procs").read_text()
        except FileNotFoundError:
            return []
        return [int(item) for item in text.split()]

    def _run(self, arguments: list[str], **kwargs: Any) -> tuple[int, bytes]:
        result = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=90,
            check=False,
            cwd="/",
            **kwargs,
        )
        require(len(result.stdout) <= MAX_FILE, "command output too long")
        return result.returncode, result.stdout

    def systemctl(self, *args: str) -> tuple[int, bytes]:
        return self._run(
            ["/usr/bin/systemctl", *args],
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )

    def _docker_env(self) -> dict[str, Any]:
        return {
            "env": {
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "DOCKER_HOST": f"unix://{SOCKET}",
                "DOCKER_CONFIG": DOCKER_CONFIG,
            },
            "user": self.user.pw_uid,
            "group": self.user.pw_gid,
            "extra_groups": [],
        }

    def docker(self, *args: str) -> bytes:
        code, output = self._run(["/usr/bin/docker", *args], **self._docker_env())
        require(code == 0, f"docker {args[0]} failed")
        return output

    def nft_table(self) -> bytes:
        code, output = self._run(
            ["/usr/sbin/nft", "-j", "list", "table", "inet", TABLE],
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C", "TZ": "UTC"},
        )
        require(code == 0, "nft listing failed")
        return output

    def socket_present(self, directory: Path) -> bool:
        try:
            entries = list(os.scandir(directory))
        except FileNotFoundError:
            return False
        return any(
            stat.S_ISSOCK(entry.stat(follow_symlinks=False).st_mode)
            for entry in entries
        )

    def prepare_agent_dir(self, uid: int, gid: int) -> None:
        self.remove_agent_dir()
        os.mkdir(AGENT_DIR, 0o700)
        os.chown(AGENT_DIR, uid, gid)
        os.mkfifo(GATE, 0o600)
        os.chown(GATE, uid, gid)

    def write_plan(self, raw: bytes, uid: int, gid: int) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(REPORT)
        fd = os.open(PLAN, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            os.fchown(stream.fileno(), uid, gid)

    def open_gate(self) -> None:
        # The measured one-shot probe may not have opened its end yet (ENXIO).
        deadline = time.monotonic() + 10
        while True:
            try:
                fd = os.open(GATE, os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                break
            except OSError as error:
                require(
                    error.errno == 6 and time.monotonic() < deadline,
                    "stop probe gate unavailable",
                )
                time.sleep(POLL_SECONDS)
        try:
            os.write(fd, b"1")
        finally:
            os.close(fd)

    def read_report(self, uid: int) -> bytes:
        fd = os.open(REPORT, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == uid
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600,
                "stop probe report is not the worker's private file",
            )
            return stream.read(MAX_FILE)

    def remove_agent_dir(self) -> None:
        if not os.path.lexists(AGENT_DIR):
            return
        info = os.lstat(AGENT_DIR)
        require(stat.S_ISDIR(info.st_mode), "agent directory is not a directory")
        for entry in os.scandir(AGENT_DIR):
            os.unlink(entry.path)
        os.rmdir(AGENT_DIR)

    def worker_session(self) -> Session:
        return SocketSession(AGENT_SOCKET, time.monotonic() + UNIT_WAIT_SECONDS)

    def daemon_session(self, runner: Path, unit: str) -> Session:
        return ProcessSession(
            [
                "/usr/bin/systemd-run",
                "--user",
                f"--machine={USER}@.host",
                "--pipe",
                "--quiet",
                "--wait",
                "--collect",
                f"--unit={unit}",
                "--",
                str(runner),
                "net-agent",
            ],
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            cwd="/",
        )

    def container_session(self, arguments: list[str]) -> Session:
        return ProcessSession(
            ["/usr/bin/docker", *arguments], cwd="/", **self._docker_env()
        )


# ---------------------------------------------------------------------------
# Collection


class Collector:
    def __init__(
        self, host: Host, config: dict[str, Any], checkout: Path = ROOT
    ) -> None:
        self.host = host
        self.config = config
        self.evidence = load_module("native_evidence_for_collector", EVIDENCE_TOOL)
        self.preflight_tool = PREFLIGHT
        self.host_policy = load_module("host_policy_for_collector", HOST_POLICY_TOOL)
        self.checkout = self.evidence.Checkout(checkout)
        self.catalog = self.evidence.load_catalog(
            self.checkout.read(self.evidence.CATALOG_FILE)
        )
        self.targets = parse_targets(self.checkout.read(TARGETS_FIXTURE))
        self.sessions: list[Session] = []
        self.containers: list[str] = []
        self.networks: list[str] = []
        self.observed: dict[tuple[str, str | None], dict[str, Any]] = {}
        self.phase_times: dict[str, list[int]] = {}
        self.nonce = secrets.token_hex(8)
        self.daemon_unit = f"ditto-native-network-daemon-probe-{self.nonce}.service"

    # -- small helpers -----------------------------------------------------

    def timeout(self) -> int:
        return self.targets["timeout_ms"]

    def ask(
        self, session: Session, value: dict[str, Any], extra: float = 5
    ) -> dict[str, Any]:
        timeout_ms = value.get("timeout_ms", 0)
        answer = session.request(value, timeout_ms / 1000 + extra)
        require(
            answer.get("schema") == AGENT_SCHEMA and answer.get("op") == value["op"],
            "agent answer does not match its request",
        )
        require(not answer.get("error"), f"agent refused {value['op']}")
        return answer

    def outcome(self, session: Session, op: str, address: str, port: int) -> str:
        answer = self.ask(
            session,
            {"op": op, "address": address, "port": port, "timeout_ms": self.timeout()},
        )
        require(
            answer.get("outcome") in self.catalog["outcomes"],
            "agent reported an unknown outcome",
        )
        return answer["outcome"]

    def record(
        self, probe: str, endpoint: str | None, observed: dict[str, Any]
    ) -> None:
        key = (probe, endpoint)
        require(key not in self.observed, f"probe {probe} observed twice")
        self.observed[key] = observed

    def measure(self, pid: int, label: str) -> None:
        require(type(pid) is int and pid > 1, f"{label} process is unknown")
        require(
            self.host.exe_sha256(pid) == self.runner_sha256,
            f"{label} runs a binary other than the release-recorded probe runner",
        )

    def hello(
        self, session: Session, pid: int, label: str, *, in_host_pid_ns: bool
    ) -> dict[str, Any]:
        self.measure(pid, label)
        answer = self.ask(session, {"op": "hello"})
        require(
            answer.get("probe_runner_binary_sha256") == self.runner_sha256,
            f"{label} self-report differs from its measured binary",
        )
        if in_host_pid_ns:
            require(answer.get("pid") == pid, f"{label} self-reported another process")
        # Measure again after it answered: the answering process is still the
        # measured image.
        self.measure(pid, label)
        return answer

    def unit_show(self, unit: str, *properties: str) -> dict[str, str]:
        code, output = self.host.systemctl(
            "show", unit, *(f"--property={name}" for name in properties)
        )
        require(code == 0, f"systemctl show {unit} failed")
        return parse_show(output)

    def wait_state(self, unit: str, states: set[str]) -> dict[str, str]:
        for _ in range(int(UNIT_WAIT_SECONDS / POLL_SECONDS)):
            shown = self.unit_show(unit, "ActiveState", "Result", "MainPID")
            if shown.get("ActiveState") in states:
                return shown
            self.host.sleep(POLL_SECONDS)
        raise Refusal(f"{unit} did not reach {sorted(states)}")

    def phase(self, name: str) -> None:
        self.phase_times[name] = [self.host.now()]

    def end_phase(self, name: str) -> None:
        self.phase_times[name].append(self.host.now())

    # -- bindings ----------------------------------------------------------

    def bind_host(self) -> None:
        host, config = self.host, self.config
        require(host.euid() == 0, "collection needs root")
        require(host.hostname() == HOSTNAME, "collection runs only on the native host")
        machine = host.machine_id()
        # Format is the preflight's check; here only its digest binds.
        self.machine_sha256 = sha256(machine)
        require(
            self.machine_sha256 == config["machine_id_sha256"]
            and host.boot_id() == config["boot_id"],
            "collection host or boot differs from the config",
        )
        self.kernel = host.kernel()
        self.uid, self.gid = host.identity()

    def bind_release(self) -> None:
        directory = Path(self.config["release_directory"])
        raw = self.host.read(directory / "release.json")
        require(
            sha256(raw) == self.config["release_manifest_sha256"],
            "release index differs from the config",
        )
        self.release_index = self.evidence.parse_release_index(raw)
        self.release_value = json.loads(raw)
        require(
            self.release_index["source_revision"] == self.config["source_revision"],
            "release index names another revision",
        )
        self.runner = RUNTIME_BASE / self.config["source_revision"] / RUNNER_RELATIVE
        self.runner_sha256 = self.host.file_sha256(self.runner)
        require(
            self.runner_sha256 == self.release_index["probe_runner_sha256"],
            "installed probe runner differs from the release-recorded binary",
        )
        language = self.config["candidate_image_language"]
        self.image = self.release_value["images"][language]["image_ref"]

    def bind_preflight(self) -> None:
        store = self.evidence.Store(Path(self.config["store"]))
        try:
            self.pre = self.evidence.parse_preflight(
                store.get(self.config["pre_collection_preflight_sha256"]), self.checkout
            )
        finally:
            store.close()
        pre, index = self.pre, self.release_index
        require(
            pre["host"]["machine_id_sha256"] == self.machine_sha256
            and pre["host"]["boot_id"] == self.config["boot_id"]
            and pre["host"]["kernel_release"] == self.kernel,
            "pre-collection preflight names another host or boot",
        )
        require(
            pre["release_manifest_sha256"] == index["sha256"]
            and pre["source_revision"] == index["source_revision"]
            and pre["runtime_archive_sha256"] == index["runtime_archive_sha256"]
            and pre["image_approval_sha256"] == index["image_approval_sha256"],
            "pre-collection preflight names another release",
        )
        age = self.host.now() - pre["checked_at_unix"]
        require(
            0 <= age <= self.evidence.PRE_COLLECTION_PREFLIGHT_MAX_AGE_SECONDS,
            "pre-collection preflight is stale",
        )

    def daemon_identity_sha256(self) -> str:
        info = parse_unique(
            self.host.docker("info", "--format", "{{json .}}"), "docker info"
        )
        identity = self.preflight_tool.daemon_identity(info, SOCKET)
        return canonical_digest(identity)

    def bind_daemon(self) -> None:
        self.daemon_sha256 = self.daemon_identity_sha256()
        require(
            self.daemon_sha256 == self.pre["daemon_identity_sha256"],
            "Docker daemon identity differs from the pre-collection preflight",
        )
        subuid = self.host.read(Path("/etc/subuid")).decode()
        subgid = self.host.read(Path("/etc/subgid")).decode()
        uid_start, uid_end = self.host_policy.mapping(subuid, USER, [])
        gid_start, gid_end = self.host_policy.mapping(subgid, USER, [])
        self.subordinate = {
            "uid_start": uid_start,
            "uid_count": uid_end - uid_start,
            "gid_start": gid_start,
            "gid_count": gid_end - gid_start,
        }

    def bind_profile(self) -> None:
        installed = self.host.read(CONNECTIVITY_PATH)
        supplied = self.host.read(Path(self.config["connectivity_profile"]))
        require(installed == supplied, "installed connectivity profile differs")
        self.profile = parse_unique(installed, "connectivity profile")
        self.connectivity = self.evidence.parse_connectivity_profile(installed)
        require(
            self.profile["schema"] == "dittobench-coding-hosted-connectivity-v2",
            "network collection needs a single-worker v2 probe profile",
        )
        issued, expires = (
            self.profile["issued_at_unix"],
            self.profile["expires_at_unix"],
        )
        now = self.host.now()
        require(
            0 <= now - issued <= PROFILE_START_MAX_AGE
            and PROFILE_MIN_WINDOW <= expires - issued <= PROFILE_MAX_WINDOW,
            "probe profile window does not fit one collection",
        )
        router, proxy = parse_prerequisites(self.host.read(PREREQUISITES_PATH))
        candidates = sorted(
            (item["address"], item["port"]) for item in self.profile["candidate_tcp"]
        )
        require(
            candidates == sorted([router, proxy]),
            "probe profile candidates are not exactly the router and refusing proxy",
        )
        self.router, self.proxy = router, proxy
        endpoint_set = self.connectivity["endpoint_set_sha256"]

        def label(name: str, address: str, port: int) -> str:
            return self.evidence._endpoint_sha256(endpoint_set, name, address, port)

        self.router_sha256 = label("candidate_tcp", *router)
        self.proxy_sha256 = label("candidate_tcp", *proxy)
        self.trusted = sorted(
            (
                label("trusted_tcp", item["address"], item["port"]),
                item["address"],
                item["port"],
            )
            for item in self.profile["trusted_tcp"]
        )
        require(
            [item[0] for item in self.trusted]
            == self.connectivity["endpoints"]["trusted"],
            "trusted endpoint labels differ from the verifier's",
        )

    def bind_units(self) -> None:
        worker = self.unit_show(WORKER_UNIT, "ActiveState", "ExecStart", "ExecStopPost")
        require(worker.get("ActiveState") in ("inactive", "failed"), "worker is active")
        require(
            f"path={self.runner} ;" in worker.get("ExecStart", "")
            and f" net-agent --unix {AGENT_SOCKET} " in worker.get("ExecStart", ""),
            "worker unit is not in network_enforcement mode for this release",
        )
        require(
            " net-once " in worker.get("ExecStopPost", ""),
            "worker unit lacks the stop probe",
        )
        egress = self.unit_show(EGRESS_UNIT, "ActiveState")
        require(egress.get("ActiveState") == "active", "deny guard is not active")
        proxy = self.unit_show(PROXY_UNIT, "ActiveState", "MainPID")
        require(proxy.get("ActiveState") == "active", "refusing proxy is not running")
        pid = int(proxy.get("MainPID", "0"))
        require(pid > 1, "refusing proxy has no main process")
        require(
            parse_cgroup(self.host.proc(pid, "cgroup")) == f"system.slice/{PROXY_UNIT}",
            "refusing proxy runs outside its unit cgroup",
        )
        argv = self.host.proc(pid, "cmdline").split(b"\0")
        require(
            argv[:4] == [b"/usr/bin/python3", b"-I", b"-B", PROXY_SCRIPT.encode()]
            and argv[4:6] == [self.proxy[0].encode(), str(self.proxy[1]).encode()],
            "refusing proxy is not the reviewed script on the listed endpoint",
        )
        self.proxy_script_sha256 = self.host.file_sha256(Path(PROXY_SCRIPT))

    def conditions(self, *, residue: bool) -> dict[str, Any]:
        worker = self.unit_show(WORKER_UNIT, "ActiveState").get("ActiveState")
        _, custody = self.host.systemctl(
            "list-units",
            "--state=active",
            "--plain",
            "--no-legend",
            "ditto-coding-custody@*",
        )
        containers = len(self.host.docker("ps", "--all", "--quiet").split())
        networks = len(
            self.host.docker(
                "network", "ls", "--quiet", "--filter", "name=ditto-job-"
            ).split()
        )
        socket_present = self.host.socket_present(CUSTODY_RUN)
        if not residue:
            return {
                "custody_active": bool(custody.strip()),
                "custody_socket_present": socket_present,
                "daemon_containers": containers,
                "daemon_job_networks": networks,
                "worker_active": worker not in ("inactive", "failed"),
            }
        daemon_unit = (
            f"user.slice/user-{self.uid}.slice/user@{self.uid}.service/app.slice/"
            f"{self.daemon_unit}"
        )
        processes = len(self.host.cgroup_procs(WORKER_CGROUP)) + len(
            self.host.cgroup_procs(daemon_unit)
        )
        return {
            "containers": containers,
            "custody_socket_present": socket_present,
            "job_networks": networks,
            "processes": processes,
            "volumes": len(self.host.docker("volume", "ls", "--quiet").split()),
            "worker_active": worker not in ("inactive", "failed"),
        }

    # -- agents ------------------------------------------------------------

    def start_worker(self) -> Session:
        self.host.prepare_agent_dir(self.uid, self.gid)
        code, _ = self.host.systemctl("start", WORKER_UNIT)
        require(code == 0, "worker unit did not start")
        shown = self.wait_state(WORKER_UNIT, {"active"})
        pid = int(shown.get("MainPID", "0"))
        require(
            parse_cgroup(self.host.proc(pid, "cgroup")) == WORKER_CGROUP,
            "worker agent is not in the worker cgroup",
        )
        session = self.host.worker_session()
        self.sessions.append(session)
        answer = self.hello(session, pid, "worker agent", in_host_pid_ns=True)
        require(
            answer.get("uid") == self.uid and answer.get("gid") == self.gid,
            "worker agent runs as another user",
        )
        return session

    def start_daemon_agent(self) -> Session:
        unit = self.daemon_unit
        session = self.host.daemon_session(self.runner, unit)
        self.sessions.append(session)
        pid = 0
        for _ in range(int(UNIT_WAIT_SECONDS / POLL_SECONDS)):
            code, output = self.host.systemctl(
                "--user", f"--machine={USER}@.host", "show", unit, "--property=MainPID"
            )
            pid = int(parse_show(output).get("MainPID", "0") or 0) if code == 0 else 0
            if pid > 1:
                break
            self.host.sleep(POLL_SECONDS)
        require(pid > 1, "daemon agent process is unknown")
        cgroup = parse_cgroup(self.host.proc(pid, "cgroup"))
        daemon = f"user.slice/user-{self.uid}.slice/user@{self.uid}.service/"
        require(cgroup.startswith(daemon), "daemon agent is not in the daemon cgroup")
        answer = self.hello(session, pid, "daemon agent", in_host_pid_ns=True)
        require(answer.get("uid") == self.uid, "daemon agent runs as another user")
        return session

    def start_container(
        self, role: str, *, network: str | None, user: int
    ) -> tuple[Session, dict[str, Any]]:
        name = f"dittobench-native-network-{role}-{self.nonce}"
        if network is not None:
            self.host.docker(
                "network",
                "create",
                "--driver",
                "bridge",
                "--opt",
                f"com.docker.network.bridge.name=dtn{role[:3]}{self.nonce[:8]}",
                "--opt",
                "com.docker.network.bridge.enable_icc=false",
                "--label",
                f"{RUN_LABEL}={self.nonce}-{role}",
                network,
            )
            self.networks.append(network)
        # The hosted harness's hardening (sandbox.runArgsForNetwork) with the
        # measured probe runner mounted read-only as the entrypoint.
        arguments = [
            "run",
            "--interactive",
            "--init",
            "--pull",
            "never",
            "--name",
            name,
            "--hostname",
            "harness",
            "--label",
            f"{RUN_LABEL}={self.nonce}-{role}",
            "--user",
            f"{user}:{user}",
            "--read-only",
            "--ipc",
            "none",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m",
            "--memory",
            "256m",
            "--pids-limit",
            "64",
            "--ulimit",
            "nofile=1024:1024",
            "--security-opt",
            "no-new-privileges",
            "--cap-drop",
            "ALL",
            "--log-driver",
            "none",
            "--network",
            network or "none",
            "--mount",
            f"type=bind,src={self.runner},dst={CONTAINER_RUNNER},readonly",
            "--entrypoint",
            CONTAINER_RUNNER,
        ]
        if network is not None:
            arguments += ["--add-host", "host.docker.internal:host-gateway"]
        arguments += [self.image, "net-agent"]
        session = self.host.container_session(arguments)
        self.sessions.append(session)
        self.containers.append(name)
        inspected = None
        for _ in range(int(UNIT_WAIT_SECONDS / POLL_SECONDS)):
            with contextlib.suppress(Refusal):
                value = parse_unique(self.host.docker("inspect", name), "inspect")
                if (
                    type(value) is list
                    and len(value) == 1
                    and value[0]["State"]["Pid"] > 1
                ):
                    inspected = value[0]
                    break
            self.host.sleep(POLL_SECONDS)
        require(inspected is not None, f"{role} container did not start")
        assert inspected is not None
        mounts = inspected.get("Mounts") or []
        require(
            len(mounts) == 1
            and mounts[0].get("Destination") == CONTAINER_RUNNER
            and mounts[0].get("RW") is False,
            f"{role} container has another mount",
        )
        init = inspected["State"]["Pid"]
        children = self.host.children(init)
        require(len(children) == 1, f"{role} container has no single agent process")
        pid = children[0]
        answer = self.hello(session, pid, f"{role} agent", in_host_pid_ns=False)
        host_ids = parse_status_ids(self.host.proc(pid, "status"))
        ids = container_ids(host_ids, self.subordinate)
        require(
            answer.get("uid") == ids["uid"] and answer.get("gid") == ids["gid"],
            f"{role} agent self-reported another identity",
        )
        address = None
        if network is not None:
            settings = inspected["NetworkSettings"]["Networks"]
            require(list(settings) == [network], f"{role} container has other networks")
            address = settings[network]["IPAddress"]
            ipaddress.IPv4Address(address)
        return session, {
            "pid": pid,
            "host_ids": host_ids,
            "ids": ids,
            "address": address,
        }

    def remove_containers(self) -> None:
        for name in reversed(self.containers):
            with contextlib.suppress(Refusal):
                self.host.docker("rm", "--force", name)
        for network in reversed(self.networks):
            with contextlib.suppress(Refusal):
                self.host.docker("network", "rm", network)
        self.containers, self.networks = [], []

    def stop_probe(self, trusted_probe: str, *, failed_start: bool) -> None:
        """ExecStopPost one-shot attempts in the worker cgroup, after revoke."""

        plan = {
            "schema": PLAN_SCHEMA,
            "requests": [
                {
                    "op": "connect",
                    "address": address,
                    "port": port,
                    "timeout_ms": self.timeout(),
                }
                for _, address, port in self.trusted
            ],
        }
        self.host.write_plan(json.dumps(plan).encode(), self.uid, self.gid)
        verb = "start" if failed_start else "stop"
        code, _ = self.host.systemctl(verb, "--no-block", WORKER_UNIT)
        require(code == 0, f"worker {verb} was not queued")
        pid = None
        for _ in range(int(UNIT_WAIT_SECONDS / POLL_SECONDS)):
            for candidate in self.host.cgroup_procs(WORKER_CGROUP):
                with contextlib.suppress(OSError, Refusal):
                    argv = self.host.proc(candidate, "cmdline").split(b"\0")
                    if argv[:2] == [str(self.runner).encode(), b"net-once"]:
                        pid = candidate
            if pid is not None:
                break
            self.host.sleep(POLL_SECONDS)
        require(pid is not None, "worker stop probe did not run")
        assert pid is not None
        self.measure(pid, "worker stop probe")
        require(
            parse_cgroup(self.host.proc(pid, "cgroup")) == WORKER_CGROUP,
            "worker stop probe is not in the worker cgroup",
        )
        require(
            parse_status_ids(self.host.proc(pid, "status")) == (self.uid, self.gid),
            "worker stop probe runs as another user",
        )
        self.host.open_gate()
        shown = self.wait_state(WORKER_UNIT, {"inactive", "failed"})
        if failed_start:
            require(
                shown.get("ActiveState") == "failed"
                and shown.get("Result") == "exit-code",
                "an expired profile did not fail the worker start",
            )
            self.host.systemctl("reset-failed", WORKER_UNIT)
        report = parse_unique(self.host.read_report(self.uid), "stop probe report")
        require(
            type(report) is dict
            and report.get("schema") == REPORT_SCHEMA
            and report.get("pid") == pid
            and report.get("uid") == self.uid
            and report.get("probe_runner_binary_sha256") == self.runner_sha256
            and type(report.get("responses")) is list
            and len(report["responses"]) == len(self.trusted),
            "stop probe report does not match the measured process",
        )
        for (label, _, _), answer in zip(
            self.trusted, report["responses"], strict=True
        ):
            require(
                type(answer) is dict
                and answer.get("op") == "connect"
                and answer.get("outcome") in self.catalog["outcomes"],
                "stop probe answer is malformed",
            )
            self.record(trusted_probe, label, {"outcome": answer["outcome"]})

    # -- phases ------------------------------------------------------------

    def active(self) -> tuple[Session, Session]:
        self.phase("active")
        targets, timeout = self.targets, self.timeout()
        worker = self.start_worker()
        self.scoped_sha256, self.scoped_output, self.scoped_input = scoped_ruleset(
            self.host.nft_table(), self.profile, self.uid
        )
        router_listener = self.ask(
            worker, {"op": "listen", "address": self.router[0], "port": self.router[1]}
        )["listener"]
        loopback_listener = self.ask(
            worker,
            {
                "op": "listen",
                "address": "127.0.0.1",
                "port": targets["host_loopback_port"],
            },
        )["listener"]
        daemon = self.start_daemon_agent()
        candidate, facts = self.start_container(
            "candidate", network=f"ditto-job-{self.nonce}c", user=HARNESS_UID
        )
        sibling, sibling_facts = self.start_container(
            "sibling", network=f"ditto-job-{self.nonce}s", user=HARNESS_UID
        )
        executor, executor_facts = self.start_container(
            "executor", network=None, user=EXECUTOR_UID
        )
        self.record("candidate.identity", None, dict(facts["ids"]))
        self.record(
            "candidate.host_ids",
            None,
            {"host_uid": facts["host_ids"][0], "host_gid": facts["host_ids"][1]},
        )
        require(
            executor_facts["ids"]["uid"] == EXECUTOR_UID, "executor identity differs"
        )
        self.record(
            "executor.interfaces",
            None,
            {
                "interfaces": parse_net_dev(
                    self.host.proc(executor_facts["pid"], "net/dev")
                )
            },
        )

        # Router: the candidate's connection must reach the worker-cgroup
        # listener, which records its source address outside the candidate.
        connect = self.outcome(candidate, "establish", *self.router)
        accepted = self.ask(
            worker, {"op": "accept", "listener": router_listener, "timeout_ms": timeout}
        )
        if connect == "connected" and accepted.get("outcome") == "accepted":
            source = classify_source(
                accepted.get("remote_address", ""), facts["address"], {self.router[0]}
            )
        else:
            connect = connect if connect != "connected" else "probe_error"
            source = "probe_error"
        self.record(
            "candidate.router.connect", self.router_sha256, {"outcome": connect}
        )
        self.record("candidate.router.source", self.router_sha256, {"outcome": source})
        self.record(
            "candidate.proxy.connect",
            self.proxy_sha256,
            {"outcome": self.outcome(candidate, "connect", *self.proxy)},
        )
        forward = self.ask(
            candidate,
            {
                "op": "proxy_forward",
                "address": self.proxy[0],
                "port": self.proxy[1],
                "target": targets["proxy_target"],
                "timeout_ms": timeout,
            },
        )
        self.record(
            "candidate.proxy.forward",
            self.proxy_sha256,
            {"outcome": forward["outcome"]},
        )
        dns = self.ask(
            candidate,
            {"op": "dns", "target": targets["dns_name"], "timeout_ms": timeout},
        )
        self.record("candidate.dns", None, {"outcome": dns["outcome"]})
        for probe, name in (
            ("candidate.metadata", "metadata"),
            ("candidate.public", "public"),
            ("candidate.ipv6", "ipv6"),
        ):
            target = targets[name]
            self.record(
                probe,
                None,
                {
                    "outcome": self.outcome(
                        candidate, "connect", target["address"], target["port"]
                    )
                },
            )
        self.record(
            "candidate.host_loopback",
            None,
            {
                "outcome": self.corroborated(
                    candidate,
                    worker,
                    loopback_listener,
                    targets["host_loopback_alias"],
                    targets["host_loopback_port"],
                )
            },
        )
        sibling_listener = self.ask(
            sibling,
            {
                "op": "listen",
                "address": sibling_facts["address"],
                "port": targets["sibling_port"],
            },
        )["listener"]
        self.record(
            "candidate.sibling_attempt",
            None,
            {
                "outcome": self.corroborated(
                    candidate,
                    sibling,
                    sibling_listener,
                    sibling_facts["address"],
                    targets["sibling_port"],
                )
            },
        )
        self.record(
            "candidate.docker_api",
            None,
            {
                "outcome": self.outcome(
                    candidate, "connect", self.router[0], targets["docker_api_port"]
                )
            },
        )
        public, metadata = targets["public"], targets["metadata"]
        for label, address, port in self.trusted:
            self.record(
                "candidate.trusted",
                label,
                {"outcome": self.outcome(candidate, "connect", address, port)},
            )
            self.record(
                "worker.trusted.handshake",
                label,
                {"outcome": self.outcome(worker, "handshake", address, port)},
            )
            self.record(
                "daemon.trusted",
                label,
                {"outcome": self.outcome(daemon, "connect", address, port)},
            )
        self.record(
            "worker.public",
            None,
            {
                "outcome": self.outcome(
                    worker, "connect", public["address"], public["port"]
                )
            },
        )
        self.record(
            "worker.metadata",
            None,
            {
                "outcome": self.outcome(
                    worker, "connect", metadata["address"], metadata["port"]
                )
            },
        )
        self.record(
            "daemon.public",
            None,
            {
                "outcome": self.outcome(
                    daemon, "connect", public["address"], public["port"]
                )
            },
        )
        for session in (sibling, executor, daemon):
            with contextlib.suppress(Refusal):
                self.ask(session, {"op": "exit"})
        self.end_phase("active")
        return worker, candidate

    def corroborated(
        self,
        client: Session,
        listener_session: Session,
        listener: str,
        address: str,
        port: int,
    ) -> str:
        """A denial counts only if the outside listener also saw nothing."""

        outcome = self.outcome(client, "connect", address, port)
        seen = self.ask(
            listener_session, {"op": "accept", "listener": listener, "timeout_ms": 500}
        )
        if seen.get("outcome") != "timeout":
            return "connected"
        return outcome

    def stop_rollback(self, worker: Session, candidate: Session) -> None:
        self.phase("stop_rollback")
        with contextlib.suppress(Refusal):
            self.ask(worker, {"op": "exit"})
        self.stop_probe("worker.trusted.after_stop", failed_start=False)
        self.deny_sha256 = deny_ruleset(self.host.nft_table(), self.uid)
        self.record(
            "candidate.router.after_stop",
            self.router_sha256,
            {"outcome": self.outcome(candidate, "connect", *self.router)},
        )
        self.end_phase("stop_rollback")

    def expiry(self, candidate: Session) -> None:
        self.phase("expiry")
        issued, expires = (
            self.profile["issued_at_unix"],
            self.profile["expires_at_unix"],
        )
        now = self.host.now()
        require(
            now - issued <= INSTALL_MAX_AGE - 10 and now <= expires - EXPIRY_LEAD,
            "too late to re-install the probe profile before its expiry",
        )
        worker = self.start_worker()
        digest, _, _ = scoped_ruleset(self.host.nft_table(), self.profile, self.uid)
        require(digest == self.scoped_sha256, "the expiry phase loaded another ruleset")
        timeout = self.timeout()
        listener = self.ask(
            worker, {"op": "listen", "address": self.router[0], "port": self.router[1]}
        )["listener"]
        require(
            self.outcome(candidate, "establish", *self.router) == "connected",
            "router flow not established",
        )
        accepted = self.ask(
            worker, {"op": "accept", "listener": listener, "timeout_ms": timeout}
        )
        require(accepted.get("outcome") == "accepted", "router listener saw no flow")
        router_flow = accepted["connection"]
        flows = []
        for label, address, port in self.trusted:
            answer = self.ask(
                worker,
                {
                    "op": "establish",
                    "address": address,
                    "port": port,
                    "timeout_ms": timeout,
                },
            )
            require(
                answer.get("outcome") == "connected", "trusted flow not established"
            )
            flows.append((label, answer["connection"]))
        while self.host.now() < expires + EXPIRY_GRACE:
            self.host.sleep(1)
        check = self.targets["check_timeout_ms"]
        for label, flow in flows:
            answer = self.ask(
                worker, {"op": "check", "connection": flow, "timeout_ms": check}
            )
            self.record(
                "worker.trusted.established_after_expiry",
                label,
                {"outcome": answer["outcome"]},
            )
        # Measured on the router side of the candidate's flow, outside the
        # candidate: the listener's replies are what expiry must cut.
        answer = self.ask(
            worker, {"op": "check", "connection": router_flow, "timeout_ms": check}
        )
        self.record(
            "candidate.router.established_after_expiry",
            self.router_sha256,
            {"outcome": answer["outcome"]},
        )
        for label, address, port in self.trusted:
            self.record(
                "worker.trusted.new_after_expiry",
                label,
                {"outcome": self.outcome(worker, "connect", address, port)},
            )
        self.record(
            "candidate.router.new_after_expiry",
            self.router_sha256,
            {"outcome": self.corroborated(candidate, worker, listener, *self.router)},
        )
        with contextlib.suppress(Refusal):
            self.ask(worker, {"op": "exit"})
        code, _ = self.host.systemctl("stop", WORKER_UNIT)
        require(code == 0, "worker did not stop after expiry")
        self.end_phase("expiry")

    def failed_start(self) -> None:
        self.phase("failed_start")
        self.host.prepare_agent_dir(self.uid, self.gid)
        self.stop_probe("worker.trusted.after_failed_start", failed_start=True)
        require(
            deny_ruleset(self.host.nft_table(), self.uid) == self.deny_sha256,
            "a failed start left another ruleset than the stop",
        )
        self.end_phase("failed_start")

    # -- record ------------------------------------------------------------

    def probes_for(self, phase: str) -> list[dict[str, Any]]:
        entry = self.catalog["kinds"]["network_enforcement"]
        endpoints: dict[str, list[str | None]] = {
            "host": [None],
            "trusted_endpoint": [item[0] for item in self.trusted],
            "router_endpoint": [self.router_sha256],
            "proxy_endpoint": [self.proxy_sha256],
        }
        outcomes = set(self.catalog["outcomes"])
        result = []
        for probe in entry["probes"]:
            if probe["phase"] != phase:
                continue
            for endpoint in endpoints[probe["scope"]]:
                key = (probe["id"], endpoint)
                require(key in self.observed, f"probe {probe['id']} was not observed")
                observed = self.observed.pop(key)
                result.append(
                    {
                        "id": probe["id"],
                        "language": None,
                        "endpoint_sha256": endpoint,
                        "expect": probe["expect"],
                        "observed": observed,
                        "matched": self.evidence.evaluate(
                            probe["expect"], observed, self.subordinate, outcomes
                        ),
                    }
                )
        return sorted(
            result, key=lambda item: (item["id"], "", item["endpoint_sha256"] or "")
        )

    def assemble(
        self,
        started: int,
        completed: int,
        preconditions: dict[str, Any],
        residue: dict[str, Any],
        tools: dict[str, str],
    ) -> dict[str, Any]:
        entry = self.catalog["kinds"]["network_enforcement"]
        phases = [
            {
                "name": name,
                "started_at_unix": self.phase_times[name][0],
                "completed_at_unix": self.phase_times[name][1],
                "probes": self.probes_for(name),
            }
            for name in entry["phases"]
        ]
        require(not self.observed, "an observation belongs to no catalog probe")
        endpoints = sorted(
            [
                ("router", self.router_sha256),
                ("refusing_proxy", self.proxy_sha256),
                *(("trusted", item[0]) for item in self.trusted),
                *(
                    ("trusted_dns", item)
                    for item in self.connectivity["endpoints"]["trusted_dns"]
                ),
            ]
        )
        return {
            "schema": self.evidence.RECORD_SCHEMA,
            "kind": "network_enforcement",
            "coverage": self.evidence.COVERAGE,
            "not_covered": list(self.evidence.NOT_COVERED),
            "tolerances_version": self.evidence.TOLERANCES["version"],
            "host": {
                "machine_id_sha256": self.machine_sha256,
                "boot_id": self.config["boot_id"],
                "kernel_release": self.kernel,
                "daemon_identity_sha256": self.daemon_sha256,
                "subordinate_ids": dict(self.subordinate),
                "router_namespace": ROUTER_NAMESPACE,
            },
            "release": {
                "source_revision": self.release_index["source_revision"],
                "release_manifest_sha256": self.release_index["sha256"],
                "runtime_archive_sha256": self.release_index["runtime_archive_sha256"],
                "image_approval_sha256": dict(
                    self.release_index["image_approval_sha256"]
                ),
            },
            "pre_collection_preflight_sha256": self.config[
                "pre_collection_preflight_sha256"
            ],
            "inputs": {"connectivity_profile_sha256": self.connectivity["sha256"]},
            "endpoints": [
                {"role": role, "endpoint_sha256": digest} for role, digest in endpoints
            ],
            "network_binding": {
                "worker_cgroup": WORKER_CGROUP,
                "nft_table": f"inet {TABLE}",
                "scoped_ruleset_sha256": self.scoped_sha256,
                "deny_ruleset_sha256": self.deny_sha256,
                "scoped_output_rules": self.scoped_output,
                "scoped_input_rules": self.scoped_input,
                "refusing_proxy_unit": PROXY_UNIT,
                "refusing_proxy_sha256": self.proxy_script_sha256,
            },
            "tools": tools,
            "preconditions": preconditions,
            "phases": phases,
            "residue": residue,
            "started_at_unix": started,
            "completed_at_unix": completed,
        }

    def collect(self) -> dict[str, Any]:
        started = self.host.now()
        self.bind_host()
        self.bind_release()
        self.bind_preflight()
        self.bind_daemon()
        self.bind_profile()
        self.bind_units()
        tools = dict(self.checkout.tools())
        tools["probe_runner_binary_sha256"] = self.runner_sha256
        preconditions = self.conditions(residue=False)
        require(
            preconditions == self.evidence.PRECONDITIONS,
            "collection preconditions do not hold",
        )
        try:
            worker, candidate = self.active()
            self.stop_rollback(worker, candidate)
            self.expiry(candidate)
            self.failed_start()
        finally:
            for session in self.sessions:
                with contextlib.suppress(Exception):
                    session.close()
            self.remove_containers()
            shown = self.unit_show(WORKER_UNIT, "ActiveState")
            if shown.get("ActiveState") not in ("inactive", "failed"):
                # A refused collection may leave a stop probe waiting on its
                # gate; kill it rather than open the gate for it.
                self.host.systemctl("kill", "--signal=SIGKILL", WORKER_UNIT)
                self.host.systemctl("stop", WORKER_UNIT)
            self.host.systemctl("reset-failed", WORKER_UNIT)
            self.host.remove_agent_dir()
        residue = self.conditions(residue=True)
        require(
            self.daemon_identity_sha256() == self.daemon_sha256,
            "Docker daemon identity changed during collection",
        )
        require(self.host.boot_id() == self.config["boot_id"], "the host rebooted")
        completed = self.host.now()
        return self.assemble(started, completed, preconditions, residue, tools)


def retain(evidence: Any, store_path: Path, record: dict[str, Any]) -> str:
    raw = evidence.canonical_bytes(record)
    evidence.parse_record_envelope(raw)
    store = evidence.Store(store_path)
    try:
        return store.put(raw)
    finally:
        store.close()


def summary(record: dict[str, Any], digest: str) -> dict[str, Any]:
    unmatched = sorted(
        {
            probe["id"]
            for phase in record["phases"]
            for probe in phase["probes"]
            if probe["matched"] is not True
        }
    )
    return {
        "schema": "dittobench-coding-native-network-collection-result-v1",
        "record_sha256": digest,
        "all_matched": not unmatched,
        "unmatched": unmatched,
        "residue_clear": record["residue"]
        == {
            "containers": 0,
            "custody_socket_present": False,
            "job_networks": 0,
            "processes": 0,
            "volumes": 0,
            "worker_active": False,
        },
        "approval_generated": False,
        "next": (
            "run and retain inspect-coding-native-host.py, "
            "then coding-native-evidence.py verify"
        ),
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = result.add_subparsers(dest="kind", required=True)
    network = commands.add_parser("network", help="collect network_enforcement")
    network.add_argument("--config", required=True, type=Path)
    network.add_argument("--confirm", required=True)
    for name in ("resource", "preexec", "cleanup"):
        commands.add_parser(name, help="not implemented (B5 PR5)")
    return result


def terminated(_signum: int, _frame: object) -> None:
    # Unwind through collect()'s cleanup: containers, networks, worker, agent dir.
    raise Refusal("collection terminated")


def main(
    argv: list[str] | None = None,
    host_factory: Any = SystemHost,
    checkout: Path = ROOT,
) -> int:
    args = parser().parse_args(argv)
    if args.kind != "network":
        print(f"{args.kind} collection is not implemented (B5 PR5)", file=sys.stderr)
        return 2
    require(args.confirm == CONFIRMATION, "collection needs the exact confirmation")
    host = host_factory()
    require(host.euid() == 0, "collection needs root")
    config = parse_config(host.read(args.config))
    signal.signal(signal.SIGTERM, terminated)
    collector = Collector(host, config, checkout)
    record = collector.collect()
    digest = retain(collector.evidence, Path(config["store"]), record)
    result = summary(record, digest)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["all_matched"] and result["residue_clear"] else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as error:
        print(f"network collection refused: {error}", file=sys.stderr)
        raise SystemExit(1) from None
