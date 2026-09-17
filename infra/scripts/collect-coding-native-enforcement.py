#!/usr/bin/env python3
"""Default-off root collector for native enforcement evidence (B5 PR4, PR5).

``network`` (PR4), ``resource`` and ``cleanup`` (PR5) are collected.
``preexec`` refuses before any host effect, listing every catalog probe it does
not collect and why (``NOT_COLLECTED``). Nothing here
mints approval: the collector retains one record in the evidence store, and
Peyton reviews it with ``coding-native-evidence.py verify`` against a
post-collection preflight.

Run only on ``ditto-coding-hosted-v2``, as root, from the reviewed release
checkout. Network collection needs the worker unit installed in
``network_enforcement`` mode, the refusing proxy running and an expiring probe
connectivity profile installed; resource collection needs the worker and
custody stopped and the approved profile documents. It is never invoked from
``coding-hosted-operate`` or any CI workflow.

Resource collection starts every workload through the production launch paths
(hosted harness sandbox, authoring executor, hosted grading executor), driven by
the measured runner's resource agent as the daemon user, and measures each limit
from outside: the started container's cgroup v2 files and ``docker inspect``,
and ``/proc`` of the workload process, whose binary is hashed like every agent.

What network collection measures from outside the candidate container:

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
import shutil
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
CGROUP_ROOT = Path("/sys/fs/cgroup")
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

    def read(self, timeout: float) -> dict[str, Any]: ...

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

    def read(self, timeout: float) -> dict[str, Any]:
        """One line the process wrote without a request (a startup refusal)."""

        assert self.process.stdout is not None
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

    def read(self, timeout: float) -> dict[str, Any]:
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


DOCKER_OUTPUT_SECONDS = 90.0


def count_process_output(
    arguments: list[str], seconds: float, label: str, **kwargs: Any
) -> int:
    """Count a command's combined output within one deadline, without keeping it.

    The pipe is read only when the selector reports it readable, so a command
    that hangs or holds the pipe open without writing cannot outlive the
    deadline; on any exit path the process is killed and reaped.
    """

    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **kwargs,
    )
    assert process.stdout is not None
    deadline = time.monotonic() + seconds
    total = 0
    try:
        fd = process.stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                require(remaining > 0, f"{label} output took too long")
                if not selector.select(remaining):
                    continue
                chunk = os.read(fd, 1 << 20)
                if not chunk:
                    break
                total += len(chunk)
        remaining = deadline - time.monotonic()
        require(remaining > 0, f"{label} output took too long")
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise Refusal(f"{label} did not exit in time") from None
        require(code == 0, f"{label} failed")
        return total
    finally:
        if process.poll() is None:
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        process.stdout.close()


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

    # -- resource and cleanup collection (B5 PR5) ----------------------------

    def monotonic(self) -> float:
        return time.monotonic()

    def boottime_ns(self) -> int:
        return time.clock_gettime_ns(time.CLOCK_BOOTTIME)

    def clock_ticks(self) -> int:
        return os.sysconf("SC_CLK_TCK")

    def page_size(self) -> int:
        return os.sysconf("SC_PAGE_SIZE")

    def cgroup_read(self, cgroup: str, name: str) -> bytes | None:
        require(
            ".." not in cgroup.split("/") and "/" not in name,
            "cgroup path is malformed",
        )
        try:
            return (CGROUP_ROOT / cgroup / name).read_bytes()[:MAX_FILE]
        except FileNotFoundError:
            return None

    def fd_count(self, pid: int) -> int:
        return len(os.listdir(f"/proc/{pid}/fd"))

    def root_statvfs(self, pid: int, path: str) -> tuple[int, int, int, int]:
        # /proc/<pid>/root resolves inside the process's own mount namespace.
        info = os.statvfs(f"/proc/{pid}/root{path}")
        return info.f_blocks, info.f_bfree, info.f_bavail, info.f_frsize

    def root_lexists(self, pid: int, path: str) -> bool:
        return os.path.lexists(f"/proc/{pid}/root{path}")

    def docker_output_bytes(self, *args: str) -> int:
        """Count Docker's output (stdout and stderr) without keeping it."""

        return count_process_output(
            ["/usr/bin/docker", *args],
            DOCKER_OUTPUT_SECONDS,
            f"docker {args[0]}",
            cwd="/",
            **self._docker_env(),
        )

    def subordinate_processes(self, start: int, count: int) -> int:
        total = 0
        for entry in os.scandir("/proc"):
            if not entry.name.isdecimal():
                continue
            with contextlib.suppress(OSError, ValueError, IndexError):
                for line in Path(entry.path, "status").read_text().splitlines():
                    if line.startswith("Uid:"):
                        if start <= int(line.split()[1]) < start + count:
                            total += 1
                        break
        return total

    def docker_scope_processes(self, uid: int) -> int:
        base = CGROUP_ROOT / f"user.slice/user-{uid}.slice/user@{uid}.service"
        total = 0
        for procs in base.glob("**/docker-*.scope/cgroup.procs"):
            with contextlib.suppress(OSError):
                total += len(procs.read_text().split())
        return total

    def prepare_work_dir(self, uid: int, gid: int, files: dict[str, bytes]) -> None:
        self.remove_work_dir()
        os.mkdir(WORK_DIR, 0o700)
        os.chown(WORK_DIR, uid, gid)
        for name, raw in files.items():
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(WORK_DIR / name, flags, 0o400)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                os.fchown(stream.fileno(), uid, gid)

    def remove_work_dir(self) -> None:
        if not os.path.lexists(WORK_DIR):
            return
        require(stat.S_ISDIR(os.lstat(WORK_DIR).st_mode), "work directory is a link")
        shutil.rmtree(WORK_DIR)

    def resource_session(self, unit: str, arguments: list[str]) -> Session:
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
                f"--setenv=DOCKER_HOST=unix://{SOCKET}",
                f"--setenv=DOCKER_CONFIG={DOCKER_CONFIG}",
                f"--setenv=TMPDIR={WORK_DIR}",
                "--setenv=PATH=/usr/bin:/bin",
                "--",
                *arguments,
            ],
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            cwd="/",
        )

    def terminate(self, pid: int) -> None:
        os.kill(pid, signal.SIGTERM)

    # -- cleanup recovery (B5 PR5) ---------------------------------------------

    def kill(self, pid: int) -> None:
        os.kill(pid, signal.SIGKILL)

    def make_private_dir(self, path: Path, uid: int, gid: int) -> None:
        require(path.parent == WORK_DIR, "private directory is outside the work dir")
        os.mkdir(path, 0o700)
        os.chown(path, uid, gid, follow_symlinks=False)

    def read_private_file(self, path: Path, uid: int, maximum: int) -> bytes | None:
        """A file the daemon user owns, read without following a link."""

        require(path.parent.parent == WORK_DIR, "private file is outside the work dir")
        directory = os.lstat(path.parent)
        require(
            stat.S_ISDIR(directory.st_mode)
            and directory.st_uid == uid
            and stat.S_IMODE(directory.st_mode) == 0o700,
            "private directory is not the daemon user's own",
        )
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(fd)
            require(
                stat.S_ISREG(info.st_mode)
                and info.st_uid == uid
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_size <= maximum,
                "private file is not the daemon user's single-link owner-only file",
            )
            return stream.read(maximum + 1)

    def one_shot(self, unit: str, arguments: list[str]) -> tuple[int, bytes]:
        """One command as a transient unit of the daemon user, like the agent."""

        result = subprocess.run(
            [
                "/usr/bin/systemd-run",
                "--user",
                f"--machine={USER}@.host",
                "--pipe",
                "--quiet",
                "--wait",
                "--collect",
                f"--unit={unit}",
                f"--setenv=DOCKER_HOST=unix://{SOCKET}",
                f"--setenv=DOCKER_CONFIG={DOCKER_CONFIG}",
                f"--setenv=TMPDIR={WORK_DIR}",
                "--setenv=PATH=/usr/bin:/bin",
                "--",
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=300,
            check=False,
            cwd="/",
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        require(len(result.stdout) <= MAX_FILE, "command output too long")
        return result.returncode, result.stdout


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
        language = self.config.get("candidate_image_language")
        if language is not None:
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


# ---------------------------------------------------------------------------
# Resource and cleanup collection (B5 PR5)
#
# Workloads start only through the production launch paths, driven by the
# measured probe runner's resource agent running as the daemon user. Every
# limit is measured here, from outside the candidate: cgroup v2 files of the
# started container, docker inspect of that container, /proc of the workload
# process (whose binary is hashed through /proc/<pid>/exe), a statfs of its
# /tmp, and Docker's retained log. The agent's own reports are only the
# production receipts (return code, timeout, retained output length).

RESOURCE_CONFIRMATION = "COLLECT NATIVE RESOURCE ENFORCEMENT EVIDENCE"
RESOURCE_CONFIG_SCHEMA = "dittobench-coding-native-resource-collection-config-v1"
RESOURCE_AGENT_SCHEMA = "dittobench-coding-native-resource-agent-v1"
WORK_DIR = Path("/var/lib/ditto-coding-hosted/native-enforcement")
WORK_FILES = {
    "execution_profile": "execution-profile.json",
    "grading_profile": "grading-profile.json",
    "enforcement_images": "enforcement-images.json",
}
EXECUTOR_LABEL = "io.heyditto.dittobench.coding-executor"
CLEANUP_CONFIRMATION = "COLLECT NATIVE CLEANUP RECOVERY EVIDENCE"
PREEXEC_CONFIRMATION = "COLLECT NATIVE PREEXEC CONFINEMENT EVIDENCE"
PREEXEC_CONFIG_SCHEMA = "dittobench-coding-native-preexec-collection-config-v1"
PREEXEC_AGENT_SCHEMA = "dittobench-coding-native-preexec-agent-v1"
PREEXEC_FIXTURES_FILE = "preexec-fixtures.json"
PREEXEC_RUN_ID = re.compile(r"p[0-9]{1,3}")
# A hostile subject observes its own confinement: the suite passes when the
# kernel refused the action, and the subject panics when it did not. The
# recorded outcome is what a confined host produces; its complement is what an
# unconfined one produces, and the catalog expectation then does not match.
PREEXEC_UNCONFINED = {"denied": "permitted", "absent": "present"}
# The hosted runtime's launch journal (codinglaunchjournal) and single-use
# marker (codinghostedruntime.ConsumeAttempt), as the collector reads them.
JOURNAL_DIR = WORK_DIR / "launch-journal"
JOURNAL_FILE = "launch-journal"
JOURNAL_MAX_BYTES = 1 << 20
JOURNAL_MAX_ENTRIES = 4096
JOURNAL_ENTRY_SCHEMA = "dittobench-coding-launch-journal-entry-v1"
JOURNAL_KEYS = ["schema", "attempt", "worker", "run", "containers", "networks"]
JOURNAL_ATTEMPT = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
JOURNAL_RUN = re.compile(r"[a-z0-9][a-z0-9-]{0,127}")
JOURNAL_CONTAINER = re.compile(r"dittobench-[a-z0-9][a-z0-9-]{0,100}")
JOURNAL_NETWORK = re.compile(r"ditto-job-[a-z0-9][a-z0-9-]{0,100}")
SENTINEL_PREFIX = "ditto-job-sentinel-"
SENTINEL_LABEL = "io.heyditto.dittobench.launch-sentinel"
RECONCILE_SCHEMA = "dittobench-coding-launch-journal-reconcile-v1"
CONSUMED_MARKER = b"dittobench-coding-hosted-runtime-consumed-v2\n"


def parse_launch_journal(raw: bytes | None) -> list[dict[str, Any]] | None:
    """Entries of a launch journal that holds identifiers only, else None.

    Each complete line must be exactly the runtime's encoding of the closed
    entry: these keys in this order, every value a closed identifier. A torn
    final append (whose launch never ran) is ignored.
    """

    if raw is None or len(raw) > JOURNAL_MAX_BYTES:
        return None
    *lines, tail = raw.split(b"\n")
    if tail and not (
        tail.startswith(b"{")
        and not tail.endswith(b"}")
        and all(0x20 <= byte <= 0x7E for byte in tail)
    ):
        return None
    if len(lines) > JOURNAL_MAX_ENTRIES:
        return None
    entries = []
    for line in lines:
        try:
            pairs = json.loads(line, object_pairs_hook=list)
        except ValueError:
            return None
        if type(pairs) is not list or [key for key, _ in pairs] != JOURNAL_KEYS:
            return None
        entry = dict(pairs)
        valid = (
            entry["schema"] == JOURNAL_ENTRY_SCHEMA
            and type(entry["attempt"]) is str
            and JOURNAL_ATTEMPT.fullmatch(entry["attempt"]) is not None
            and type(entry["worker"]) is str
            and JOURNAL_ATTEMPT.fullmatch(entry["worker"]) is not None
            and type(entry["run"]) is str
            and JOURNAL_RUN.fullmatch(entry["run"]) is not None
            and type(entry["containers"]) is list
            and type(entry["networks"]) is list
            and all(
                type(name) is str and JOURNAL_CONTAINER.fullmatch(name)
                for name in entry["containers"]
            )
            and all(
                type(name) is str and JOURNAL_NETWORK.fullmatch(name)
                for name in entry["networks"]
            )
            and bool(entry["containers"] or entry["networks"])
        )
        if not valid or json.dumps(entry, separators=(",", ":")).encode() != line:
            return None
        entries.append(entry)
    return entries


EXECUTOR_RUNNER = "/workspace/dittobench-coding-enforcement-probe"
ROOTFS_PROBE_PREFIX = "/.dittobench-rootfs-probe-"
CLASSES = ("harness", "executor_authoring", "executor_grading")
CANDIDATE_UIDS = {
    "harness": 65532,
    "executor_authoring": 10001,
    "executor_grading": 10001,
}
INT64_MAX = (1 << 63) - 1
NONCE = re.compile(r"[0-9a-f]{16}")
RUN_ID = re.compile(r"r[0-9]{1,3}")
SECURITY_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
EXECUTOR_INSTANCE = re.compile(r"coding-executor-[0-9a-f]{32}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
HOLD_MS = 4000
RUN_TIMEOUT_MS = 180_000
LIMIT_WAIT_SECONDS = 90
SAMPLE_SECONDS = 0.01
CPU_SECONDS = 9
CPU_WARMUP_SECONDS = 1.5
CPU_WINDOW_SECONDS = 4.0
EXECUTOR_LOG_BYTES = 4 * 24576
HANG_GRACE_SECONDS = 120

# Probes each collector kind observes. Anything else in the catalog is listed
# in NOT_COLLECTED with the concrete reason, and a kind with any such probe
# refuses to run: a record missing a catalog probe can never verify, and no
# collector claims a probe it did not measure.
NOT_COLLECTED: dict[str, dict[str, str]] = {
    "network_enforcement": {},
    "resource_enforcement": {},
    "preexec_confinement": {},
    "cleanup_recovery": {},
}


def parse_resource_config(raw: bytes) -> dict[str, Any]:
    keys = {
        "schema",
        "source_revision",
        "release_directory",
        "release_manifest_sha256",
        "machine_id_sha256",
        "boot_id",
        "store",
        "pre_collection_preflight_sha256",
        "execution_profile",
        "grading_profile",
        "enforcement_images",
        "seccomp_profile",
        "apparmor_profile",
        "shadow_only",
        "weight_eligible",
    }
    value = closed(parse_unique(raw, "collector config"), keys, "collector config")
    require(
        value["schema"] == RESOURCE_CONFIG_SCHEMA, "collector config schema differs"
    )
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
    for name in (
        "release_directory",
        "store",
        "execution_profile",
        "grading_profile",
        "enforcement_images",
    ):
        path = value[name]
        require(
            type(path) is str
            and path.startswith("/")
            and os.path.normpath(path) == path
            and ".." not in path.split("/"),
            f"collector {name} is not a clean absolute path",
        )
    for name in ("seccomp_profile", "apparmor_profile"):
        require(
            value[name] == ""
            or (
                type(value[name]) is str
                and SECURITY_PROFILE.fullmatch(value[name])
                and value[name].lower() != "unconfined"
            ),
            f"collector {name} is malformed",
        )
    return value


def parse_preexec_config(raw: bytes) -> dict[str, Any]:
    """The resource collector's config plus the approval-pinned fixtures."""

    value = parse_unique(raw, "collector config")
    require(
        type(value) is dict and value.get("schema") == PREEXEC_CONFIG_SCHEMA,
        "collector config schema differs",
    )
    fixtures = value.pop("preexec_fixtures", None)
    require(
        type(fixtures) is str
        and fixtures.startswith("/")
        and os.path.normpath(fixtures) == fixtures
        and ".." not in fixtures.split("/"),
        "collector preexec_fixtures is not a clean absolute path",
    )
    # Reuse the resource config's own rules for every shared field.
    parsed = parse_resource_config(
        json.dumps({**value, "schema": RESOURCE_CONFIG_SCHEMA}).encode()
    )
    return {**parsed, "schema": PREEXEC_CONFIG_SCHEMA, "preexec_fixtures": fixtures}


def parse_status_confinement(raw: bytes) -> dict[str, Any]:
    """The candidate's own confinement, read from outside its container.

    ``/proc/<pid>/status`` is the kernel's account of the process: its
    capability sets, whether new privileges are refused, and whether seccomp
    filters it. The candidate cannot write any of these fields.
    """

    fields: dict[str, str] = {}
    for line in raw.splitlines():
        name, _, rest = line.decode().partition(":")
        fields[name.strip()] = rest.strip()
    capabilities = {}
    for key, name in (
        ("CapAmb", "ambient"),
        ("CapBnd", "bounding"),
        ("CapEff", "effective"),
        ("CapInh", "inheritable"),
        ("CapPrm", "permitted"),
    ):
        value = fields.get(key, "")
        require(
            re.fullmatch(r"[0-9a-f]{1,16}", value) is not None,
            f"candidate status lacks {key}",
        )
        capabilities[name] = int(value, 16)
    result: dict[str, Any] = {"capabilities": capabilities}
    for key, name in (("NoNewPrivs", "no_new_privs"), ("Seccomp", "seccomp_mode")):
        value = fields.get(key, "")
        require(
            re.fullmatch(r"[0-9]{1,3}", value) is not None,
            f"candidate status lacks {key}",
        )
        result[name] = int(value)
    return result


def parse_cgroup_limit(raw: bytes | None, label: str) -> int:
    """A cgroup v2 single-value limit; ``max`` is recorded as INT64_MAX."""

    require(raw is not None, f"container cgroup lacks {label}")
    assert raw is not None
    text = raw.decode("ascii", "replace").strip()
    if text == "max":
        return INT64_MAX
    require(text.isdecimal() and len(text) <= 19, f"{label} is malformed")
    return min(int(text), INT64_MAX)


def parse_cpu_max(raw: bytes | None) -> int:
    """``cpu.max`` as CPU millis per second; no quota is 0."""

    require(raw is not None, "container cgroup lacks cpu.max")
    assert raw is not None
    fields = raw.decode("ascii", "replace").split()
    require(
        len(fields) == 2
        and fields[1].isdecimal()
        and int(fields[1]) > 0
        and (fields[0] == "max" or fields[0].isdecimal()),
        "cpu.max is malformed",
    )
    if fields[0] == "max":
        return 0
    return int(fields[0]) * 1000 // int(fields[1])


def parse_keyed(raw: bytes | None, label: str) -> dict[str, int]:
    """``memory.events``, ``pids.events``, ``cpu.stat`` and ``/proc/<pid>/io``."""

    require(raw is not None, f"container cgroup lacks {label}")
    assert raw is not None
    result: dict[str, int] = {}
    for line in raw.decode("ascii", "replace").splitlines():
        fields = line.replace(":", " ").split()
        require(
            len(fields) == 2 and fields[1].isdecimal() and fields[0] not in result,
            f"{label} is malformed",
        )
        result[fields[0]] = int(fields[1])
    return result


def parse_nofile_limits(raw: bytes) -> tuple[int, int]:
    for line in raw.decode("ascii", "replace").splitlines():
        if line.startswith("Max open files"):
            fields = line[len("Max open files") :].split()
            require(
                len(fields) >= 2 and fields[0].isdecimal() and fields[1].isdecimal(),
                "open file limits are malformed",
            )
            return int(fields[0]), int(fields[1])
    raise Refusal("process limits lack open files")


def root_mount_read_only(raw: bytes) -> bool:
    """The last ``/`` mount in ``mountinfo`` (the visible one) is mounted ro."""

    options = None
    for line in raw.decode("utf-8", "replace").splitlines():
        fields = line.split()
        if len(fields) > 5 and fields[4] == "/":
            options = fields[5].split(",")
    require(options is not None, "mountinfo lacks the root mount")
    assert options is not None
    return "ro" in options


def parse_stat(raw: bytes) -> tuple[str, int]:
    """``/proc/<pid>/stat`` state and start time (clock ticks since boot)."""

    text = raw.decode("ascii", "replace")
    fields = text[text.rindex(")") + 2 :].split()
    require(len(fields) > 19 and fields[19].isdecimal(), "process stat is malformed")
    return fields[0], int(fields[19])


def workload_args(mode: str, nonce: str, **flags: int | str | bool) -> list[str]:
    """``probe.WorkloadArgs``: the exact order the runner renders and accepts."""

    require(NONCE.fullmatch(nonce) is not None, "workload nonce is malformed")
    argv = ["workload", mode, "--nonce", nonce]
    for name in ("hold_ms", "threads", "seconds", "dir", "bytes"):
        if flags.get(name):
            argv += ["--" + name.replace("_", "-"), str(flags[name])]
    if flags.get("setsid_child"):
        argv.append("--setsid-child")
    return argv


# Outside samplers. Each reads only the container's cgroup v2 files or /proc
# of the workload process through the host adapter, so the collector and the
# local kernel test share one implementation.


def poll(host: Any, condition: Any, seconds: float = LIMIT_WAIT_SECONDS) -> bool:
    deadline = host.monotonic() + seconds
    while True:
        if condition():
            return True
        if host.monotonic() >= deadline:
            return False
        host.sleep(SAMPLE_SECONDS)


def cpu_burners(quota_millis: int) -> int:
    """More burner threads than the quota allows, so the cgroup must throttle."""

    return -(-quota_millis // 1000) + 1


def sample_cgroup_limits(host: Any, cgroup: str) -> dict[str, int]:
    return {
        "memory_max": parse_cgroup_limit(
            host.cgroup_read(cgroup, "memory.max"), "memory.max"
        ),
        "memory_swap_max": parse_cgroup_limit(
            host.cgroup_read(cgroup, "memory.swap.max"), "memory.swap.max"
        ),
        "cpu_quota": parse_cpu_max(host.cgroup_read(cgroup, "cpu.max")),
        "pids_max": parse_cgroup_limit(
            host.cgroup_read(cgroup, "pids.max"), "pids.max"
        ),
    }


def sample_memory_oom(host: Any, cgroup: str) -> dict[str, Any]:
    """The cgroup's own OOM kill counter and its peak usage before the kill."""

    def killed() -> bool:
        events = parse_keyed(host.cgroup_read(cgroup, "memory.events"), "memory.events")
        return events.get("oom_kill", 0) >= 1

    enforced = poll(host, killed)
    peak = parse_cgroup_limit(host.cgroup_read(cgroup, "memory.peak"), "memory.peak")
    # memory.peak may pass memory.max by one forced page charge; the verifier
    # allows one page of the host page size recorded here (tolerances v2).
    return {"enforced": enforced, "measured": peak, "page_bytes": host.page_size()}


def sample_cpu_throttle(host: Any, cgroup: str) -> dict[str, Any]:
    """CPU millis used per wall second over a window, and throttling in it."""

    def read() -> tuple[float, dict[str, int]]:
        moment = host.monotonic()
        return moment, parse_keyed(host.cgroup_read(cgroup, "cpu.stat"), "cpu.stat")

    host.sleep(CPU_WARMUP_SECONDS)
    first, before = read()
    host.sleep(CPU_WINDOW_SECONDS)
    last, after = read()
    wall_usec = max(1, round((last - first) * 1_000_000))
    usage = after["usage_usec"] - before["usage_usec"]
    return {
        "enforced": after["nr_throttled"] - before["nr_throttled"] >= 1,
        "measured": usage * 1000 // wall_usec,
    }


def sample_pids_cap(host: Any, cgroup: str) -> dict[str, Any]:
    """The cgroup refused a fork (``pids.events max``) at its task count."""

    def capped() -> bool:
        events = parse_keyed(host.cgroup_read(cgroup, "pids.events"), "pids.events")
        return events.get("max", 0) >= 1

    enforced = poll(host, capped)
    current = 0
    for _ in range(5):
        current = max(
            current,
            parse_cgroup_limit(
                host.cgroup_read(cgroup, "pids.current"), "pids.current"
            ),
        )
        host.sleep(SAMPLE_SECONDS * 2)
    return {"enforced": enforced, "measured": current}


def sample_scratch_enospc(host: Any, pid: int) -> dict[str, Any]:
    """The workload's own /tmp, seen from outside: used bytes when it is full."""

    state: dict[str, tuple[int, int, int, int]] = {}

    def full() -> bool:
        state["fs"] = host.root_statvfs(pid, "/tmp")
        return state["fs"][2] == 0

    enforced = poll(host, full)
    blocks, free, _available, size = state["fs"]
    return {"enforced": enforced, "measured": (blocks - free) * size}


def sample_rootfs(host: Any, pid: int, nonce: str) -> dict[str, str]:
    """The root mount is read-only and the workload's create left nothing."""

    host.sleep(0.5)
    read_only = root_mount_read_only(host.proc(pid, "mountinfo"))
    created = host.root_lexists(pid, f"{ROOTFS_PROBE_PREFIX}{nonce}")
    return {"outcome": "read_only" if read_only and not created else "writable"}


def sample_nofile_cap(host: Any, pid: int, limit: int) -> dict[str, Any]:
    """Open descriptors once the table stopped growing, against the rlimit."""

    counts: list[int] = []

    def settled() -> bool:
        counts.append(host.fd_count(pid))
        return len(counts) > 5 and counts[-1] == counts[-5] and counts[-1] > 3

    poll(host, settled)
    soft, hard = parse_nofile_limits(host.proc(pid, "limits"))
    return {
        "enforced": soft == hard == limit and counts[-1] == soft,
        "measured": counts[-1],
    }


def sample_emitted_bytes(host: Any, pid: int, total: int) -> int:
    """Bytes the workload wrote (``/proc/<pid>/io`` wchar), once it wrote them."""

    emitted = [0]

    def written() -> bool:
        emitted[0] = parse_keyed(host.proc(pid, "io"), "io")["wchar"]
        return emitted[0] >= total

    poll(host, written)
    return emitted[0]


def sample_timeout(
    host: Any, cgroup: str, pid: int, deadline_ms: int, uid: int
) -> dict[str, int]:
    """From the workload's kernel start time to its disappearance, then what lives.

    Elapsed time starts at ``/proc/<pid>/stat`` starttime (clock ticks since
    boot) and ends at the first poll that finds the process gone or a zombie,
    on CLOCK_BOOTTIME, rounded up to a millisecond.
    """

    def alive(target: int) -> bool:
        try:
            return parse_stat(host.proc(target, "stat"))[0] != "Z"
        except (OSError, Refusal):
            return False

    ticks = host.clock_ticks()
    _, start = parse_stat(host.proc(pid, "stat"))
    poll(host, lambda: not alive(pid), deadline_ms / 1000 + HANG_GRACE_SECONDS)
    gone_ns = host.boottime_ns()
    elapsed = -(-(gone_ns - start * 1_000_000_000 // ticks) // 1_000_000)
    live = 0
    for item in (host.cgroup_read(cgroup, "cgroup.procs") or b"").split():
        with contextlib.suppress(OSError, Refusal, ValueError):
            other = int(item)
            if alive(other) and parse_status_ids(host.proc(other, "status"))[0] == uid:
                live += 1
    return {"elapsed_ms": max(0, elapsed), "live_processes": live}


class ResourceHost(Host, Protocol):
    """Outside reads resource and cleanup collection needs, beyond ``Host``."""

    def monotonic(self) -> float: ...
    def boottime_ns(self) -> int: ...
    def clock_ticks(self) -> int: ...
    def cgroup_read(self, cgroup: str, name: str) -> bytes | None: ...
    def fd_count(self, pid: int) -> int: ...
    def root_statvfs(self, pid: int, path: str) -> tuple[int, int, int, int]: ...
    def root_lexists(self, pid: int, path: str) -> bool: ...
    def docker_output_bytes(self, *args: str) -> int: ...
    def subordinate_processes(self, start: int, count: int) -> int: ...
    def docker_scope_processes(self, uid: int) -> int: ...
    def prepare_work_dir(self, uid: int, gid: int, files: dict[str, bytes]) -> None: ...
    def remove_work_dir(self) -> None: ...
    def resource_session(self, unit: str, arguments: list[str]) -> Session: ...
    def terminate(self, pid: int) -> None: ...
    def page_size(self) -> int: ...
    def kill(self, pid: int) -> None: ...
    def make_private_dir(self, path: Path, uid: int, gid: int) -> None: ...
    def read_private_file(self, path: Path, uid: int, maximum: int) -> bytes | None: ...
    def one_shot(self, unit: str, arguments: list[str]) -> tuple[int, bytes]: ...


class ResourceCollector(Collector):
    """``resource_enforcement``: every container class, every language image.

    It shares the network collector's host, release, preflight, daemon and
    residue bindings; ``daemon_unit`` names the resource agent's unit.
    """

    kind = "resource_enforcement"
    host: ResourceHost

    def __init__(
        self, host: ResourceHost, config: dict[str, Any], checkout: Path = ROOT
    ) -> None:
        super().__init__(host, config, checkout)
        self.daemon_unit = f"ditto-native-resource-agent-{self.nonce}.service"
        self.agent: Session | None = None
        self.agent_pid = 0

    # -- agent -------------------------------------------------------------

    def ask(
        self, session: Session, value: dict[str, Any], extra: float = 30
    ) -> dict[str, Any]:
        timeout_ms = value.get("timeout_ms", 0)
        answer = session.request(value, timeout_ms / 1000 + extra)
        require(
            answer.get("schema") == RESOURCE_AGENT_SCHEMA
            and answer.get("op") == value["op"],
            "resource agent answer does not match its request",
        )
        require(not answer.get("error"), f"resource agent refused {value['op']}")
        return answer

    def bind_inputs(self) -> None:
        try:
            self._bind_inputs()
        except self.evidence.Refusal as error:
            raise Refusal(str(error)) from None

    def _bind_inputs(self) -> None:
        raws = {name: self.host.read(Path(self.config[name])) for name in WORK_FILES}
        evidence = self.evidence
        self.input_raw = raws
        self.profiles = {
            "execution_profile_sha256": evidence.parse_execution_profile(
                raws["execution_profile"]
            ),
            "grading_profile_sha256": evidence.parse_grading_profile(
                raws["grading_profile"]
            ),
            "enforcement_images_sha256": evidence.parse_enforcement_images(
                raws["enforcement_images"]
            ),
        }
        # The verifier's own binding: released images, the grading profile's
        # own language and its exact commands.
        evidence._enforcement_images(self.profiles, self.release_index)
        self.inputs = {name: doc["sha256"] for name, doc in self.profiles.items()}
        self.repositories = {}
        for language in self.evidence.LANGUAGES:
            reference = self.release_value["images"][language]["image_ref"]
            repository, _, digest = reference.partition("@")
            require(
                "sha256:" + digest.removeprefix("sha256:")
                == self.profiles["enforcement_images_sha256"]["images"][language][
                    "image_digest"
                ],
                f"{language} release image is not the pinned enforcement image",
            )
            self.repositories[language] = repository
        grading_image = self.profiles["grading_profile_sha256"]["image_digest"]
        self.own_language = next(
            language
            for language in self.evidence.LANGUAGES
            if self.profiles["enforcement_images_sha256"]["images"][language][
                "image_digest"
            ]
            == grading_image
        )

    def limit(
        self, source: str, container: str, language: str, group: str | None = None
    ) -> int:
        value = self.evidence.resolve_bind(
            source, container, language, {"test_group": group}, self.profiles
        )
        assert isinstance(value, int)
        return value

    def start_agent(self) -> None:
        self.host.prepare_work_dir(
            self.uid,
            self.gid,
            {WORK_FILES[name]: raw for name, raw in self.input_raw.items()},
        )
        arguments = [str(self.runner), "resource-agent"]
        for name, file in WORK_FILES.items():
            arguments += ["--" + name.replace("_", "-"), str(WORK_DIR / file)]
        arguments += ["--runner", str(self.runner), "--work-dir", str(WORK_DIR)]
        for name in ("seccomp_profile", "apparmor_profile"):
            if self.config[name]:
                arguments += ["--" + name.replace("_", "-"), self.config[name]]
        arguments += self.agent_attempt_arguments()
        session = self.host.resource_session(self.daemon_unit, arguments)
        self.sessions.append(session)
        pid = 0
        for _ in range(int(UNIT_WAIT_SECONDS / POLL_SECONDS)):
            code, output = self.host.systemctl(
                "--user",
                f"--machine={USER}@.host",
                "show",
                self.daemon_unit,
                "--property=MainPID",
            )
            pid = int(parse_show(output).get("MainPID", "0") or 0) if code == 0 else 0
            if pid > 1:
                break
            self.host.sleep(POLL_SECONDS)
        require(pid > 1, "resource agent process is unknown")
        daemon = f"user.slice/user-{self.uid}.slice/user@{self.uid}.service/"
        require(
            parse_cgroup(self.host.proc(pid, "cgroup")).startswith(daemon),
            "resource agent is not in the daemon user's cgroup",
        )
        answer = self.hello(session, pid, "resource agent", in_host_pid_ns=True)
        require(
            answer.get("uid") == self.uid and answer.get("gid") == self.gid,
            "resource agent runs as another user",
        )
        require(
            answer.get("inputs") == self.inputs,
            "resource agent read other profile documents",
        )
        self.agent, self.agent_pid = session, pid

    def agent_attempt_arguments(self) -> list[str]:
        """Resource collection runs the agent without a launch journal."""

        return []

    def unwind(self) -> None:
        """Remove what an interrupted cleanup scenario left; nothing here."""

    def stop_agent(self) -> None:
        if self.agent is not None:
            with contextlib.suppress(Exception):
                self.ask(self.agent, {"op": "exit"}, extra=120)
            with contextlib.suppress(Exception):
                self.agent.close()
        self.agent = None

    # -- one workload --------------------------------------------------------

    def launch(
        self,
        container: str,
        language: str,
        argv: list[str],
        *,
        timeout_ms: int = RUN_TIMEOUT_MS,
        test_group: str | None = None,
        fail_start: bool = False,
    ) -> dict[str, Any]:
        assert self.agent is not None
        request: dict[str, Any] = {
            "op": "start",
            "class": container,
            "language": language,
            "repository": self.repositories[language],
            "workload": argv[1:],
        }
        if test_group is None:
            request["timeout_ms"] = timeout_ms
        else:
            request["test_group"] = test_group
        if fail_start:
            request["fail_start"] = True
        # A harness start runs `docker run` and may create its job network.
        answer = self.ask(self.agent, request, extra=150)
        require(
            type(answer.get("run")) is str and RUN_ID.fullmatch(answer["run"]),
            "resource agent run id is malformed",
        )
        if container == "harness":
            require(
                type(answer.get("container_name")) is str
                and (
                    CONTAINER_ID.fullmatch(answer["container_name"])
                    or re.fullmatch(
                        r"dittobench-[0-9a-f]{16}", answer["container_name"]
                    )
                ),
                "harness workload container is unnamed",
            )
        else:
            require(
                type(answer.get("executor_instance")) is str
                and EXECUTOR_INSTANCE.fullmatch(answer["executor_instance"]),
                "executor workload instance is malformed",
            )
        return answer

    def inspect(self, target: str) -> dict[str, Any] | None:
        try:
            value = parse_unique(self.host.docker("inspect", target), "inspect")
        except Refusal:
            return None
        require(type(value) is list and len(value) == 1, "inspect is malformed")
        return value[0]

    def container(
        self, started: dict[str, Any], container: str, language: str
    ) -> dict[str, Any]:
        """The running workload container, found and bound from outside."""

        for _ in range(int(UNIT_WAIT_SECONDS / POLL_SECONDS)):
            if container == "harness":
                targets = [started["container_name"]]
            else:
                label = f"{EXECUTOR_LABEL}={started['executor_instance']}"
                targets = self.host.docker(
                    "ps", "--all", "--no-trunc", "--quiet", "--filter", f"label={label}"
                ).split()
                targets = [item.decode() for item in targets]
            for target in targets:
                inspected = self.inspect(target)
                if inspected is None:
                    continue
                name = inspected.get("Name", "")
                if container != "harness" and name.startswith(
                    "/dittobench-coding-probe-"
                ):
                    continue
                state = inspected["State"]
                if state.get("Running") is True and state.get("Pid", 0) > 1:
                    return self.bind_container(inspected, container, language, started)
            self.host.sleep(POLL_SECONDS)
        raise Refusal(f"{container} {language} workload container did not start")

    def bind_container(
        self,
        inspected: dict[str, Any],
        container: str,
        language: str,
        started: dict[str, Any],
    ) -> dict[str, Any]:
        """The started container is the approved launch, or collection refuses."""

        host_config, config = inspected["HostConfig"], inspected["Config"]
        memory = self.limit("memory_limit_bytes", container, language)
        scratch = self.limit("scratch_limit_bytes", container, language)
        labels = config.get("Labels") or {}
        require(
            CONTAINER_ID.fullmatch(inspected.get("Id", "")) is not None,
            "workload container id is malformed",
        )
        require(
            inspected.get("Image")
            == self.release_value["images"][language]["config_digest"],
            f"{container} {language} container runs another image",
        )
        require(
            host_config.get("ReadonlyRootfs") is True
            and host_config.get("Memory") == memory
            and host_config.get("MemorySwap") == memory
            and host_config.get("NanoCpus")
            == self.limit("cpu_quota_millis", container, language) * 1_000_000
            and host_config.get("PidsLimit")
            == self.limit("pids_limit", container, language)
            and (host_config.get("Tmpfs") or {}).get("/tmp")
            == f"rw,noexec,nosuid,nodev,size={scratch}"
            and host_config.get("Privileged") is False
            and [
                item.upper().removeprefix("CAP_")
                for item in host_config.get("CapDrop") or []
            ]
            == ["ALL"],
            f"{container} {language} container limits differ from the approved profile",
        )
        uid = CANDIDATE_UIDS[container]
        if container == "harness":
            mounts = inspected.get("Mounts") or []
            require(
                config.get("User") == f"{uid}:{uid}"
                and host_config.get("LogConfig", {}).get("Type") == "local"
                and str(host_config.get("NetworkMode", "")).startswith("ditto-job-")
                and RUN_LABEL in labels
                and len(mounts) == 1
                and mounts[0].get("Destination") == CONTAINER_RUNNER
                and mounts[0].get("RW") is False
                and config.get("Entrypoint") == [CONTAINER_RUNNER],
                "harness workload container is not the hosted harness launch",
            )
        else:
            require(
                config.get("User") == "0:0"
                and host_config.get("LogConfig", {}).get("Type") == "none"
                and host_config.get("NetworkMode") == "none"
                and labels.get(EXECUTOR_LABEL) == started["executor_instance"]
                and labels.get(RUN_LABEL) == inspected.get("Name", "").lstrip("/"),
                "executor workload container is not the production executor launch",
            )
        init = inspected["State"]["Pid"]
        cgroup = parse_cgroup(self.host.proc(init, "cgroup"))
        require(
            cgroup.startswith(
                f"user.slice/user-{self.uid}.slice/user@{self.uid}.service/"
            )
            and cgroup.endswith(f"/docker-{inspected['Id']}.scope"),
            "workload container is outside the daemon user's container cgroup",
        )
        return {"id": inspected["Id"], "init": init, "cgroup": cgroup}

    def workload_process(
        self, found: dict[str, Any], container: str, argv: list[str]
    ) -> int:
        """The measured runner running exactly this workload, as the candidate."""

        runner = CONTAINER_RUNNER if container == "harness" else EXECUTOR_RUNNER
        expected = [runner.encode(), *(item.encode() for item in argv)]
        uid = CANDIDATE_UIDS[container]
        for _ in range(int(UNIT_WAIT_SECONDS / SAMPLE_SECONDS / 10)):
            found_pid = 0
            with contextlib.suppress(OSError, ValueError):
                children = self.host.children(found["init"])
                if container != "harness":
                    children = [
                        pid for child in children for pid in self.host.children(child)
                    ]
                for pid in children:
                    if self.host.proc(pid, "cmdline").split(b"\0")[:-1] == expected:
                        found_pid = pid
            if found_pid:
                self.measure(found_pid, f"{container} workload")
                host_ids = parse_status_ids(self.host.proc(found_pid, "status"))
                require(
                    container_ids(host_ids, self.subordinate)
                    == {"uid": uid, "gid": uid},
                    f"{container} workload runs as another identity",
                )
                return found_pid
            self.host.sleep(SAMPLE_SECONDS * 10)
        raise Refusal(f"{container} workload process did not start")

    def finish(
        self, started: dict[str, Any], container: str, *, receipt: bool = False
    ) -> dict[str, Any]:
        """Wait for the production receipt; the container must then be gone."""

        assert self.agent is not None
        deadline = self.host.monotonic() + RUN_TIMEOUT_MS / 1000 + 3600
        while True:
            answer = self.ask(
                self.agent, {"op": "wait", "run": started["run"], "timeout_ms": 30000}
            )
            if answer.get("done") is True:
                break
            require(self.host.monotonic() < deadline, "workload did not finish")
        if receipt:
            require(
                answer.get("run_failed") is not True
                and type(answer.get("return_code")) is int
                and type(answer.get("retained_output_bytes")) is int,
                f"{container} workload has no production receipt",
            )
        if container == "harness":
            name = started["container_name"]
            field = "id" if CONTAINER_ID.fullmatch(name) else "name"
            remaining = self.host.docker(
                "ps", "--all", "--quiet", "--filter", f"{field}={name}"
            )
        else:
            label = f"{EXECUTOR_LABEL}={started['executor_instance']}"
            remaining = self.host.docker(
                "ps", "--all", "--quiet", "--filter", f"label={label}"
            )
        require(not remaining.split(), f"{container} workload container remains")
        return answer

    def run(
        self,
        container: str,
        language: str,
        mode: str,
        sample: Any,
        *,
        receipt: bool = False,
        test_group: str | None = None,
        timeout_ms: int = RUN_TIMEOUT_MS,
        **flags: int | str | bool,
    ) -> tuple[Any, dict[str, Any]]:
        nonce = secrets.token_hex(8)
        argv = workload_args(mode, nonce, **flags)
        started = self.launch(
            container, language, argv, timeout_ms=timeout_ms, test_group=test_group
        )
        started = {**started, "nonce": nonce}
        found = self.container(started, container, language)
        pid = self.workload_process(found, container, argv)
        observed = sample(found, pid, started)
        return observed, self.finish(started, container, receipt=receipt)

    # -- phases --------------------------------------------------------------

    def cgroup_phase(self, container: str, language: str) -> None:
        values, _ = self.run(
            container,
            language,
            "hold",
            lambda found, _pid, _started: sample_cgroup_limits(
                self.host, found["cgroup"]
            ),
            hold_ms=HOLD_MS,
        )
        for probe, source in (
            ("memory_max", "memory_limit_bytes"),
            ("cpu_quota", "cpu_quota_millis"),
            ("pids_max", "pids_limit"),
        ):
            self.record(
                f"{container}.{probe}",
                language,
                {
                    "cgroup": values[probe],
                    "profile": self.limit(source, container, language),
                },
            )
        self.record(
            f"{container}.memory_swap_max",
            language,
            {"swap_max_bytes": values["memory_swap_max"]},
        )

    def measured_phase(self, container: str, language: str) -> None:
        bounded = {
            "memory_oom": self.memory_oom,
            "cpu_throttle": self.cpu_throttle,
            "pids_cap": self.pids_cap,
            "scratch_enospc": self.scratch_enospc,
            "rootfs_read_only": self.rootfs_read_only,
            "nofile_cap": self.nofile_cap,
            "log_bound": self.log_bound,
        }
        for probe, measure in bounded.items():
            self.record(f"{container}.{probe}", language, measure(container, language))
        if container == "executor_grading":
            for group in self.evidence.HOSTED_TEST_GROUPS:
                self.record(
                    f"executor_grading.supervisor_timeout.{group}",
                    language,
                    self.supervisor_timeout(language, group),
                )

    def memory_oom(self, container: str, language: str) -> dict[str, Any]:
        observed, _ = self.run(
            container,
            language,
            "memory",
            lambda found, _pid, _started: sample_memory_oom(self.host, found["cgroup"]),
            hold_ms=HOLD_MS,
        )
        return {
            **observed,
            "limit": self.limit("memory_limit_bytes", container, language),
        }

    def cpu_throttle(self, container: str, language: str) -> dict[str, Any]:
        quota = self.limit("cpu_quota_millis", container, language)
        observed, _ = self.run(
            container,
            language,
            "cpu",
            lambda found, _pid, _started: sample_cpu_throttle(
                self.host, found["cgroup"]
            ),
            threads=cpu_burners(quota),
            seconds=CPU_SECONDS,
        )
        return {**observed, "limit": quota}

    def pids_cap(self, container: str, language: str) -> dict[str, Any]:
        observed, _ = self.run(
            container,
            language,
            "pids",
            lambda found, _pid, _started: sample_pids_cap(self.host, found["cgroup"]),
            hold_ms=HOLD_MS,
        )
        return {**observed, "limit": self.limit("pids_limit", container, language)}

    def scratch_enospc(self, container: str, language: str) -> dict[str, Any]:
        observed, _ = self.run(
            container,
            language,
            "scratch",
            lambda _found, pid, _started: sample_scratch_enospc(self.host, pid),
            hold_ms=HOLD_MS,
            dir="/tmp",
        )
        return {
            **observed,
            "limit": self.limit("scratch_limit_bytes", container, language),
        }

    def rootfs_read_only(self, container: str, language: str) -> dict[str, Any]:
        observed, _ = self.run(
            container,
            language,
            "rootfs",
            lambda _found, pid, started: sample_rootfs(
                self.host, pid, started["nonce"]
            ),
            hold_ms=HOLD_MS,
        )
        return observed

    def nofile_cap(self, container: str, language: str) -> dict[str, Any]:
        limit = self.limit("nofile_limit", container, language)
        observed, _ = self.run(
            container,
            language,
            "nofile",
            lambda _found, pid, _started: sample_nofile_cap(self.host, pid, limit),
            hold_ms=HOLD_MS,
        )
        return {**observed, "limit": limit}

    def log_bound(self, container: str, language: str) -> dict[str, Any]:
        limit = self.limit("log_limit_bytes", container, language)
        total = limit * 7 // 4 if container == "harness" else EXECUTOR_LOG_BYTES

        def sample(found: dict[str, Any], pid: int, _started: object) -> dict:
            emitted = sample_emitted_bytes(self.host, pid, total)
            retained = 0
            if container == "harness":
                retained = self.host.docker_output_bytes("logs", found["id"])
            return {"emitted": emitted, "docker_retained": retained}

        observed, answer = self.run(
            container,
            language,
            "log",
            sample,
            receipt=container != "harness",
            hold_ms=HOLD_MS if container == "harness" else 1000,
            bytes=total,
        )
        if container == "executor_grading":
            # Grading containers use --log-driver none (bound in
            # bind_container), so the receipt is every retained byte.
            return {
                "emitted_bytes": observed["emitted"],
                "limit": limit,
                "retained_bytes": answer["retained_output_bytes"],
            }
        retained = (
            observed["docker_retained"]
            if container == "harness"
            else answer["retained_output_bytes"]
        )
        return {
            "enforced": observed["emitted"] > limit,
            "limit": limit,
            "measured": retained,
        }

    def supervisor_timeout(self, language: str, group: str) -> dict[str, Any]:
        deadline_ms = self.limit(
            f"{group}_command_timeout_ms", "executor_grading", language, group
        )
        uid = self.subordinate["uid_start"] + CANDIDATE_UIDS["executor_grading"] - 1
        observed, answer = self.run(
            "executor_grading",
            language,
            "hang",
            lambda found, pid, _started: sample_timeout(
                self.host, found["cgroup"], pid, deadline_ms, uid
            ),
            receipt=True,
            test_group=group,
            seconds=deadline_ms // 1000 + HANG_GRACE_SECONDS,
        )
        code = answer["return_code"]
        return {
            "deadline_ms": deadline_ms,
            "elapsed_ms": observed["elapsed_ms"],
            "exit_code": code if 0 <= code <= 255 else 255,
            "live_processes": observed["live_processes"],
            "test_group": group,
        }

    # -- record ------------------------------------------------------------

    def probes_for(self, phase: str) -> list[dict[str, Any]]:
        entry = self.catalog["kinds"][self.kind]
        outcomes = set(self.catalog["outcomes"])
        result = []
        for probe in entry["probes"]:
            if probe["phase"] != phase:
                continue
            scopes = (
                list(self.evidence.LANGUAGES)
                if probe["scope"] == "language"
                else [None]
            )
            for language in scopes:
                key = (probe["id"], language)
                require(key in self.observed, f"probe {probe['id']} was not observed")
                observed = self.observed.pop(key)
                result.append(
                    {
                        "id": probe["id"],
                        "language": language,
                        "endpoint_sha256": None,
                        "expect": probe["expect"],
                        "observed": observed,
                        "matched": self.evidence.evaluate(
                            probe["expect"], observed, self.subordinate, outcomes
                        ),
                    }
                )
        return sorted(result, key=lambda item: (item["id"], item["language"] or "", ""))

    def assemble_record(
        self,
        started: int,
        completed: int,
        preconditions: dict[str, Any],
        residue: dict[str, Any],
        tools: dict[str, str],
    ) -> dict[str, Any]:
        entry = self.catalog["kinds"][self.kind]
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
        return {
            "schema": self.evidence.RECORD_SCHEMA,
            "kind": self.kind,
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
            "inputs": {name: self.inputs[name] for name in entry["inputs"]},
            "endpoints": [],
            "tools": tools,
            "preconditions": preconditions,
            "phases": phases,
            "residue": residue,
            "started_at_unix": started,
            "completed_at_unix": completed,
        }

    def phases(self) -> None:
        for phase in ("cgroup", "measured"):
            self.phase(phase)
            for language in self.evidence.LANGUAGES:
                for container in CLASSES:
                    if phase == "cgroup":
                        self.cgroup_phase(container, language)
                    else:
                        self.measured_phase(container, language)
            self.end_phase(phase)

    def collect(self) -> dict[str, Any]:
        uncovered = sorted(NOT_COLLECTED[self.kind])
        require(not uncovered, f"{self.kind} cannot collect {', '.join(uncovered)}")
        started = self.host.now()
        self.bind_host()
        self.bind_release()
        self.bind_preflight()
        self.bind_daemon()
        self.bind_inputs()
        tools = dict(self.checkout.tools())
        tools["probe_runner_binary_sha256"] = self.runner_sha256
        preconditions = self.conditions(residue=False)
        require(
            preconditions == self.evidence.PRECONDITIONS,
            "collection preconditions do not hold",
        )
        try:
            self.start_agent()
            self.phases()
        finally:
            self.stop_agent()
            self.unwind()
            for session in self.sessions:
                with contextlib.suppress(Exception):
                    session.close()
            self.host.remove_work_dir()
        residue = self.conditions(residue=True)
        require(
            self.daemon_identity_sha256() == self.daemon_sha256,
            "Docker daemon identity changed during collection",
        )
        require(self.host.boot_id() == self.config["boot_id"], "the host rebooted")
        completed = self.host.now()
        return self.assemble_record(started, completed, preconditions, residue, tools)


class CleanupCollector(ResourceCollector):
    """``cleanup_recovery``: resources are gone after each production cleanup.

    Every agent session runs with the hosted runtime's launch journal and a
    fresh attempt state directory, so each workload is one journaled attempt.
    After the six in-process scenarios, a new session is killed with SIGKILL
    mid-attempt and the runtime's reconciler runs from outside; a last session
    reuses that attempt's state directory and must be refused.
    """

    kind = "cleanup_recovery"

    def __init__(
        self, host: ResourceHost, config: dict[str, Any], checkout: Path = ROOT
    ) -> None:
        super().__init__(host, config, checkout)
        self.sessions_started = 0
        self.attempt_state: Path | None = None
        self.decoy: str | None = None

    def agent_attempt_arguments(self) -> list[str]:
        if self.sessions_started == 0:
            self.host.make_private_dir(JOURNAL_DIR, self.uid, self.gid)
        self.sessions_started += 1
        self.attempt_state = WORK_DIR / f"attempt-{self.sessions_started}"
        self.host.make_private_dir(self.attempt_state, self.uid, self.gid)
        return [
            "--launch-journal",
            str(JOURNAL_DIR),
            "--attempt-state",
            str(self.attempt_state),
        ]

    def restart_agent(self) -> None:
        require(self.agent is None, "the previous resource agent is still running")
        self.daemon_unit = (
            f"ditto-native-resource-agent-{self.nonce}-{self.sessions_started}.service"
        )
        self.start_agent()

    def reconcile_arguments(self) -> list[str]:
        return [
            str(self.runner),
            "reconcile-launch-journal",
            "--launch-journal",
            str(JOURNAL_DIR),
            "--docker-executable",
            "/usr/bin/docker",
            "--docker-socket",
            SOCKET,
        ]

    def reconcile(self) -> tuple[int, dict[str, Any] | None]:
        self.sessions_started += 1
        unit = f"ditto-native-journal-reconcile-{self.nonce}-{self.sessions_started}"
        code, output = self.host.one_shot(unit + ".service", self.reconcile_arguments())
        try:
            report = parse_unique(output.strip(), "reconcile report")
        except Refusal:
            return code, None
        if type(report) is not dict or report.get("schema") != RECONCILE_SCHEMA:
            return code, None
        return code, report

    def unwind(self) -> None:
        """After an interrupted collection: the decoy, then the journal."""

        if self.decoy is not None:
            with contextlib.suppress(Exception):
                self.host.docker("network", "rm", self.decoy)
        with contextlib.suppress(Exception):
            journal = JOURNAL_DIR / JOURNAL_FILE
            pending = self.host.read_private_file(journal, self.uid, JOURNAL_MAX_BYTES)
            if self.sessions_started and pending is not None:
                self.reconcile()

    def present(self, kind: str, name: str) -> bool:
        if kind == "container":
            names = self.host.docker("ps", "--all", "--format", "{{.Names}}")
        else:
            names = self.host.docker("network", "ls", "--format", "{{.Name}}")
        return name.encode() in names.split()

    def counts(self) -> dict[str, int]:
        """What remains on the daemon and host, measured from outside."""

        def count(*args: str) -> int:
            return len(self.host.docker(*args).split())

        return {
            "containers": count("ps", "--all", "--quiet"),
            "networks": count(
                "network", "ls", "--quiet", "--filter", "name=ditto-job-"
            ),
            "processes": self.host.subordinate_processes(
                self.subordinate["uid_start"], self.subordinate["uid_count"]
            )
            + self.host.docker_scope_processes(self.uid),
            "volumes": count("volume", "ls", "--quiet"),
        }

    def settled_counts(self) -> dict[str, int]:
        state: dict[str, dict[str, int]] = {}

        def empty() -> bool:
            state["counts"] = self.counts()
            return not any(state["counts"].values())

        poll(self.host, empty, 10)
        return state["counts"]

    def scenario(self, name: str, probe: str, action: Any) -> None:
        self.phase(name)
        action()
        self.record(probe, None, self.settled_counts())
        self.end_phase(name)

    def scenario_normal_stop(self) -> None:
        language = self.own_language
        argv = workload_args("hold", secrets.token_hex(8), hold_ms=1000)
        started = self.launch("executor_authoring", language, argv)
        answer = self.finish(started, "executor_authoring", receipt=True)
        require(answer.get("return_code") == 0, "normal stop workload failed")

    def scenario_partial_start(self) -> None:
        argv = workload_args("hold", secrets.token_hex(8), hold_ms=1000)
        started = self.launch("harness", self.own_language, argv, fail_start=True)
        answer = self.finish(started, "harness")
        require(answer.get("run_failed") is True, "the partial start did not fail")

    def scenario_timeout(self) -> None:
        argv = workload_args("hang", secrets.token_hex(8), seconds=600)
        started = self.launch(
            "executor_authoring", self.own_language, argv, timeout_ms=3000
        )
        answer = self.finish(started, "executor_authoring")
        require(
            answer.get("timed_out") is True, "the timeout workload did not time out"
        )

    def scenario_oom(self) -> None:
        argv = workload_args("memory", secrets.token_hex(8), hold_ms=1000)
        started = self.launch("executor_authoring", self.own_language, argv)
        self.finish(started, "executor_authoring")

    def scenario_escaped_setsid(self) -> None:
        argv = workload_args(
            "hang", secrets.token_hex(8), seconds=600, setsid_child=True
        )
        started = self.launch(
            "executor_authoring", self.own_language, argv, timeout_ms=3000
        )
        found = self.container(started, "executor_authoring", self.own_language)
        pid = self.workload_process(found, "executor_authoring", argv)
        require(
            poll(self.host, lambda: len(self.host.children(pid)) == 1, 10),
            "the escaping child was not started",
        )
        self.finish(started, "executor_authoring")

    def scenario_runner_sigterm(self) -> None:
        argv = workload_args("hang", secrets.token_hex(8), seconds=600)
        started = self.launch(
            "executor_authoring", self.own_language, argv, timeout_ms=600_000
        )
        found = self.container(started, "executor_authoring", self.own_language)
        self.workload_process(found, "executor_authoring", argv)
        self.host.terminate(self.agent_pid)
        stopped = poll(
            self.host, lambda: not self.host.cgroup_procs(self.agent_cgroup()), 150
        )
        require(stopped, "the resource agent did not stop after SIGTERM")
        with contextlib.suppress(Exception):
            assert self.agent is not None
            self.agent.close()
        self.agent = None

    def scenario_runner_sigkill(self) -> None:
        """SIGKILL mid-attempt, leftovers seen, the runtime's reconciler run."""

        self.restart_agent()
        own = self.own_language
        argv = workload_args("hang", secrets.token_hex(8), seconds=600)
        started = self.launch("executor_authoring", own, argv, timeout_ms=600_000)
        found = self.container(started, "executor_authoring", own)
        self.workload_process(found, "executor_authoring", argv)
        inspected = self.inspect(found["id"])
        require(inspected is not None, "the workload container vanished")
        assert inspected is not None
        container_name = str(inspected.get("Name", "")).lstrip("/")
        # A network shaped and labelled exactly like a sentinel, never
        # journaled: reconciliation must leave it.
        suffix = secrets.token_hex(8)
        self.decoy = SENTINEL_PREFIX + suffix
        self.host.docker(
            "network",
            "create",
            "--driver",
            "bridge",
            "--internal",
            "--label",
            f"{RUN_LABEL}=sentinel-{suffix}",
            "--label",
            f"{SENTINEL_LABEL}=collector-decoy",
            self.decoy,
        )
        assert self.agent is not None
        self.host.kill(self.agent_pid)
        stopped = poll(
            self.host, lambda: not self.host.cgroup_procs(self.agent_cgroup()), 150
        )
        require(stopped, "the resource agent did not stop after SIGKILL")
        with contextlib.suppress(Exception):
            self.agent.close()
        self.agent = None

        try:
            raw = self.host.read_private_file(
                JOURNAL_DIR / JOURNAL_FILE, self.uid, JOURNAL_MAX_BYTES
            )
        except Refusal:
            # Not the runtime's owner-only single-link file: not ids-only.
            raw = b"\xff"
        entries = parse_launch_journal(raw)
        journaled_containers = {
            name for entry in entries or [] for name in entry["containers"]
        }
        sentinels = sorted(
            name
            for entry in entries or []
            for name in entry["networks"]
            if name.startswith(SENTINEL_PREFIX)
        )
        # Completeness is what reconciled_absent shows; this is the content.
        if raw is None or entries == []:
            ids_outcome = "absent"
        else:
            ids_outcome = "journal_ids_only" if entries is not None else "permitted"
        sentinel = sentinels[0] if len(sentinels) == 1 else None
        # Leftovers must exist, or the kill proved nothing.
        left = (
            sentinel is not None
            and container_name in journaled_containers
            and self.present("network", sentinel)
            and self.present("container", container_name)
        )

        self.reconcile()
        decoy_kept = self.present("network", self.decoy)
        sentinel_gone = sentinel is not None and not self.present("network", sentinel)
        with contextlib.suppress(Refusal):
            self.host.docker("network", "rm", self.decoy)
        require(
            not self.present("network", self.decoy),
            "the collector's decoy network could not be removed",
        )
        self.decoy = None
        if not decoy_kept:
            sentinel_outcome = "extra_ids_touched"
        elif not left:
            sentinel_outcome = "absent"
        elif not sentinel_gone:
            sentinel_outcome = "probe_error"
        else:
            sentinel_outcome = "present"
        self.record(
            "cleanup.runner_sigkill.journal_ids_only", None, {"outcome": ids_outcome}
        )
        self.record(
            "cleanup.runner_sigkill.sentinel_network",
            None,
            {"outcome": sentinel_outcome},
        )

    def scenario_rerun(self) -> None:
        """The killed attempt's state directory refuses a second start."""

        state = self.attempt_state
        require(state is not None and self.agent is None, "no killed attempt to rerun")
        assert state is not None
        marker = self.host.read_private_file(
            state / "consumed", self.uid, len(CONSUMED_MARKER)
        )
        arguments = [str(self.runner), "resource-agent"]
        for name, file in WORK_FILES.items():
            arguments += ["--" + name.replace("_", "-"), str(WORK_DIR / file)]
        arguments += ["--runner", str(self.runner), "--work-dir", str(WORK_DIR)]
        for name in ("seccomp_profile", "apparmor_profile"):
            if self.config[name]:
                arguments += ["--" + name.replace("_", "-"), self.config[name]]
        arguments += [
            "--launch-journal",
            str(JOURNAL_DIR),
            "--attempt-state",
            str(state),
        ]
        self.sessions_started += 1
        self.daemon_unit = (
            f"ditto-native-resource-agent-{self.nonce}-{self.sessions_started}.service"
        )
        session = self.host.resource_session(self.daemon_unit, arguments)
        self.sessions.append(session)
        try:
            answer: dict[str, Any] | None = session.read(30)
        except Refusal:
            answer = None
        finally:
            with contextlib.suppress(Exception):
                session.close()
        stopped = poll(
            self.host, lambda: not self.host.cgroup_procs(self.agent_cgroup()), 30
        )
        require(stopped, "the rerun resource agent did not exit")
        refused = marker == CONSUMED_MARKER and answer == {
            "schema": RESOURCE_AGENT_SCHEMA,
            "op": "attempt",
            "error": "probe: attempt state consumed",
        }
        self.record(
            "cleanup.rerun.consumed_marker",
            None,
            {"outcome": "refused" if refused else "accepted"},
        )

    def agent_cgroup(self) -> str:
        return (
            f"user.slice/user-{self.uid}.slice/user@{self.uid}.service/app.slice/"
            f"{self.daemon_unit}"
        )

    def phases(self) -> None:
        self.scenario(
            "normal_stop", "cleanup.normal_stop.absent", self.scenario_normal_stop
        )
        self.scenario(
            "partial_start", "cleanup.partial_start.absent", self.scenario_partial_start
        )
        self.scenario("timeout", "cleanup.timeout.absent", self.scenario_timeout)
        self.scenario("oom", "cleanup.oom.absent", self.scenario_oom)
        self.scenario(
            "escaped_setsid",
            "cleanup.escaped_setsid.absent",
            self.scenario_escaped_setsid,
        )
        self.scenario(
            "runner_sigterm",
            "cleanup.runner_sigterm.absent",
            self.scenario_runner_sigterm,
        )
        self.scenario(
            "runner_sigkill",
            "cleanup.runner_sigkill.reconciled_absent",
            self.scenario_runner_sigkill,
        )
        self.phase("rerun")
        self.scenario_rerun()
        self.end_phase("rerun")


class PreexecCollector(ResourceCollector):
    """``preexec_confinement``: the public fixtures, on the production launch.

    Every fixture is a subject compiled against the same hidden suite. A
    hostile subject panics when its action is not confined and observes the
    kernel's refusal when it is, so the suite's own pass/fail is the
    observation. The five identity probes are read from outside, from the
    candidate's ``/proc/<pid>/status`` while the hang control holds it live.

    The collector never decides what a fixture proves: the recorded outcome
    describes a confined host, its complement an unconfined one, and the
    catalog expectation is evaluated by the shared verifier either way.
    """

    kind = "preexec_confinement"

    def __init__(
        self, host: ResourceHost, config: dict[str, Any], checkout: Path = ROOT
    ) -> None:
        super().__init__(host, config, checkout)
        self.daemon_unit = f"ditto-native-preexec-agent-{self.nonce}.service"
        self.identity: dict[str, dict[str, Any]] = {}

    def ask(
        self, session: Session, value: dict[str, Any], extra: float = 30
    ) -> dict[str, Any]:
        timeout_ms = value.get("timeout_ms", 0)
        answer = session.request(value, timeout_ms / 1000 + extra)
        require(
            answer.get("schema") == PREEXEC_AGENT_SCHEMA
            and answer.get("op") == value["op"],
            "preexec agent answer does not match its request",
        )
        require(not answer.get("error"), f"preexec agent refused {value['op']}")
        return answer

    def _bind_inputs(self) -> None:
        super()._bind_inputs()
        raw = self.host.read(Path(self.config["preexec_fixtures"]))
        self.input_raw = {**self.input_raw, "preexec_fixtures": raw}
        self.fixtures = self.evidence.parse_preexec_fixtures(raw)
        self.profiles["preexec_fixtures_sha256"] = self.fixtures
        self.inputs = {name: doc["sha256"] for name, doc in self.profiles.items()}
        # The fixture suite is not the benchmark's suite, so the fixture runs
        # its own recorded command. What must agree between the two documents
        # is Rust's pinned authority, exactly as the offline verifier binds it.
        images = self.profiles["enforcement_images_sha256"]["images"]
        entry = self.fixtures["languages"]["rust"]
        recorded = images["rust"]["test_argv"][entry["test_group"]]
        fields = {recorded[i]: recorded[i + 1] for i in range(1, len(recorded) - 1, 2)}
        require(
            fields.get("--authority-sha256") == entry["authority_sha256"],
            "the rust fixture authority is not the recorded rust authority",
        )

    def start_agent(self) -> None:
        files = {
            WORK_FILES[name]: raw
            for name, raw in self.input_raw.items()
            if name in WORK_FILES
        }
        files[PREEXEC_FIXTURES_FILE] = self.input_raw["preexec_fixtures"]
        self.host.prepare_work_dir(self.uid, self.gid, files)
        arguments = [str(self.runner), "preexec-agent"]
        for name, file in WORK_FILES.items():
            arguments += ["--" + name.replace("_", "-"), str(WORK_DIR / file)]
        arguments += [
            "--preexec-fixtures",
            str(WORK_DIR / PREEXEC_FIXTURES_FILE),
            "--checkout",
            str(self.checkout.root),
            "--runner",
            str(self.runner),
            "--work-dir",
            str(WORK_DIR),
        ]
        for name in ("seccomp_profile", "apparmor_profile"):
            if self.config[name]:
                arguments += ["--" + name.replace("_", "-"), self.config[name]]
        session = self.host.resource_session(self.daemon_unit, arguments)
        self.sessions.append(session)
        pid = 0
        for _ in range(int(UNIT_WAIT_SECONDS / POLL_SECONDS)):
            code, output = self.host.systemctl(
                "--user",
                f"--machine={USER}@.host",
                "show",
                self.daemon_unit,
                "--property=MainPID",
            )
            pid = int(parse_show(output).get("MainPID", "0") or 0) if code == 0 else 0
            if pid > 1:
                break
            self.host.sleep(POLL_SECONDS)
        require(pid > 1, "preexec agent process is unknown")
        daemon = f"user.slice/user-{self.uid}.slice/user@{self.uid}.service/"
        require(
            parse_cgroup(self.host.proc(pid, "cgroup")).startswith(daemon),
            "preexec agent is not in the daemon user's cgroup",
        )
        answer = self.hello(session, pid, "preexec agent", in_host_pid_ns=True)
        require(
            answer.get("uid") == self.uid and answer.get("gid") == self.gid,
            "preexec agent runs as another user",
        )
        require(
            answer.get("inputs") == self.inputs,
            "preexec agent read other profile documents",
        )
        self.agent, self.agent_pid = session, pid

    # -- one fixture ---------------------------------------------------------

    def launch_fixture(self, language: str, fixture: str) -> dict[str, Any]:
        assert self.agent is not None
        answer = self.ask(
            self.agent,
            {
                "op": "start",
                "language": language,
                "repository": self.repositories[language],
                "fixture": fixture,
            },
            extra=150,
        )
        require(
            type(answer.get("run")) is str and PREEXEC_RUN_ID.fullmatch(answer["run"]),
            "preexec agent run id is malformed",
        )
        require(
            type(answer.get("executor_instance")) is str
            and EXECUTOR_INSTANCE.fullmatch(answer["executor_instance"]),
            "preexec fixture instance is malformed",
        )
        entry = self.fixtures["languages"][language]
        expected = self.fixture_subject(language, fixture)
        require(
            answer.get("subject_sha256") == expected
            and answer.get("suite_sha256") == entry["controls_suite_sha256"],
            f"{language} {fixture} staged other bytes than the pinned fixture",
        )
        return answer

    def fixture_subject(self, language: str, fixture: str) -> str:
        entry = self.fixtures["languages"][language]
        phase, _, name = fixture.partition(".")
        if phase == "control":
            return str(entry["controls"][name]["sha256"])
        return str(entry["hostile"][name]["subject"]["sha256"])

    def candidate(self, found: dict[str, Any]) -> int:
        """The candidate process, found from outside by its mapped identity.

        The executor's own supervisor is root in the container; only the
        candidate runs as the approved unprivileged identity, so a process that
        maps to it is the subject the fixture compiled.
        """

        uid = CANDIDATE_UIDS["executor_grading"]
        for _ in range(int(UNIT_WAIT_SECONDS / SAMPLE_SECONDS / 10)):
            descendants: list[int] = []
            with contextlib.suppress(OSError, ValueError):
                for child in self.host.children(found["init"]):
                    descendants += [child, *self.host.children(child)]
            for pid in descendants:
                # The supervisor is root in the container and every id outside
                # the mapping refuses; neither ends the scan for the candidate.
                with contextlib.suppress(OSError, ValueError):
                    host_ids = parse_status_ids(self.host.proc(pid, "status"))
                    if container_ids(host_ids, self.subordinate) == {
                        "uid": uid,
                        "gid": uid,
                    }:
                        return pid
            self.host.sleep(SAMPLE_SECONDS * 10)
        raise Refusal("preexec candidate process did not start")

    def sample_identity(self, language: str, found: dict[str, Any]) -> None:
        """The five identity observations, all from one live candidate."""

        pid = self.candidate(found)
        self.measure(pid, f"{language} candidate")
        host_uid, host_gid = parse_status_ids(self.host.proc(pid, "status"))
        confinement = parse_status_confinement(self.host.proc(pid, "status"))
        self.identity[language] = {
            "identity.candidate": container_ids((host_uid, host_gid), self.subordinate),
            "identity.host_ids": {"host_uid": host_uid, "host_gid": host_gid},
            "identity.capabilities": confinement["capabilities"],
            "identity.no_new_privs": {"no_new_privs": confinement["no_new_privs"]},
            "identity.seccomp": {"seccomp_mode": confinement["seccomp_mode"]},
        }

    def run_fixture(
        self, language: str, fixture: str, *, identity: bool = False
    ) -> dict[str, Any]:
        started = self.launch_fixture(language, fixture)
        if identity:
            found = self.container(started, "executor_grading", language)
            self.sample_identity(language, found)
        answer = self.finish(started, "executor_grading")
        require(
            answer.get("run_failed") is not True
            and type(answer.get("return_code")) is int
            and type(answer.get("passed")) is int
            and type(answer.get("total")) is int,
            f"{language} {fixture} has no production receipt",
        )
        require(
            answer.get("build_failed") is not True,
            f"{language} {fixture} never reached its suite",
        )
        return answer

    # -- phases --------------------------------------------------------------

    def controls_phase(self) -> None:
        self.phase("controls")
        for language in self.evidence.LANGUAGES:
            entry = self.fixtures["languages"][language]
            for name in self.evidence.PREEXEC_CONTROLS:
                answer = self.run_fixture(
                    language, f"control.{name}", identity=name == "hang"
                )
                self.observed[(f"control.{name}", language)] = {
                    "passed": answer["passed"],
                    "total": answer["total"],
                    "suite_sha256": entry["controls_suite_sha256"],
                    "timed_out": bool(answer.get("timed_out")),
                }
        self.end_phase("controls")

    def identity_phase(self) -> None:
        self.phase("identity")
        for language in self.evidence.LANGUAGES:
            require(
                language in self.identity,
                f"{language} candidate identity was not sampled",
            )
            for probe, observed in self.identity[language].items():
                self.observed[(probe, language)] = observed
        self.end_phase("identity")

    def hostile_phase(self) -> None:
        self.phase("hostile")
        for language in self.evidence.LANGUAGES:
            entry = self.fixtures["languages"][language]
            for name in sorted(entry["hostile"]):
                recorded = entry["hostile"][name]["outcome"]
                answer = self.run_fixture(language, f"hostile.{name}")
                if answer.get("timed_out"):
                    outcome = "probe_error"
                elif answer["passed"] == answer["total"] and answer["total"] >= 2:
                    outcome = recorded
                else:
                    outcome = PREEXEC_UNCONFINED[recorded]
                self.observed[(f"hostile.{name}", language)] = {"outcome": outcome}
        self.end_phase("hostile")

    def phases(self) -> None:
        self.controls_phase()
        self.identity_phase()
        self.hostile_phase()


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
    short = SUBCOMMANDS[record["kind"]]
    return {
        "schema": f"dittobench-coding-native-{short}-collection-result-v1",
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


SUBCOMMANDS = {
    "network_enforcement": "network",
    "resource_enforcement": "resource",
    "preexec_confinement": "preexec",
    "cleanup_recovery": "cleanup",
}
KIND_OF = {short: kind for kind, short in SUBCOMMANDS.items()}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = result.add_subparsers(dest="kind", required=True)
    for name in ("network", "resource", "preexec", "cleanup"):
        command = commands.add_parser(name, help=f"collect {KIND_OF[name]}")
        command.add_argument("--config", required=True, type=Path)
        command.add_argument("--confirm", required=True)
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
    uncovered = NOT_COLLECTED[KIND_OF[args.kind]]
    if uncovered:
        # Before any host effect: a record missing these probes never verifies.
        print(
            f"{args.kind} collection is refused; these catalog probes are not "
            "collected:",
            file=sys.stderr,
        )
        for probe, reason in sorted(uncovered.items()):
            print(f"  {probe}: {reason}", file=sys.stderr)
        return 2
    confirmation = {
        "network": CONFIRMATION,
        "resource": RESOURCE_CONFIRMATION,
        "preexec": PREEXEC_CONFIRMATION,
        "cleanup": CLEANUP_CONFIRMATION,
    }[args.kind]
    require(args.confirm == confirmation, "collection needs the exact confirmation")
    host = host_factory()
    require(host.euid() == 0, "collection needs root")
    raw = host.read(args.config)
    signal.signal(signal.SIGTERM, terminated)
    collector: Collector
    if args.kind == "network":
        config = parse_config(raw)
        collector = Collector(host, config, checkout)
    elif args.kind == "resource":
        config = parse_resource_config(raw)
        collector = ResourceCollector(host, config, checkout)
    elif args.kind == "preexec":
        config = parse_preexec_config(raw)
        collector = PreexecCollector(host, config, checkout)
    else:
        config = parse_resource_config(raw)
        collector = CleanupCollector(host, config, checkout)
    record = collector.collect()
    digest = retain(collector.evidence, Path(config["store"]), record)
    result = summary(record, digest)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["all_matched"] and result["residue_clear"] else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Refusal as error:
        print(f"collection refused: {error}", file=sys.stderr)
        raise SystemExit(1) from None
