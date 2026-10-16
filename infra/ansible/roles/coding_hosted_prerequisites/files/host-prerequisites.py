#!/usr/bin/python3
"""Check the fixed native-v2 host record and proxy unit paths. Read-only."""

import ipaddress
import json
import os
import re
import runpy
import socket
import stat
import subprocess
import sys
from pathlib import Path

SCHEMA = "dittobench-coding-hosted-host-prerequisites-v2"
USER = "ditto-coding-hosted"
HOST_POLICY = Path("/usr/local/lib/ditto-coding-hosted/host-policy.py")
PORT_RANGE = Path("/proc/sys/net/ipv4/ip_local_port_range")
KEYS = {
    "schema",
    "shadow_only",
    "weight_eligible",
    "router_listen",
    "egress_network",
    "egress_proxy",
    "candidate_uid",
    "candidate_gid",
}
# Go net.IP.IsPrivate for IPv4; connectivity candidate_tcp uses the same set.
PRIVATE = tuple(
    ipaddress.IPv4Network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
PORT = re.compile(r"[1-9][0-9]{3,4}")
# codinghostedruntime identifier(): 1-128 of [a-z0-9_-], no leading dash.
NETWORK = re.compile(r"[a-z0-9_][a-z0-9_-]{0,127}")
# host-policy.py admits exactly one 65,536-ID range. Rootless Docker maps
# container ID 0 to the daemon account and IDs 1..65536 into that range.
SUBORDINATE_IDS = 65536
UNIT = "ditto-coding-hosted-egress-proxy.service"
# The role installs and byte-compares this fragment; nothing else may name the unit.
FRAGMENT = Path("/etc/systemd/system") / UNIT
# System manager unit load path for systemd 257 on Debian 13, highest priority
# first: systemd.unit(5) "Unit File Load Path", equal to `systemd-analyze
# unit-paths` there. A host that reports any other list is refused.
UNIT_PATHS = tuple(
    Path(path)
    for path in (
        "/etc/systemd/system.control",
        "/run/systemd/system.control",
        "/run/systemd/transient",
        "/run/systemd/generator.early",
        "/etc/systemd/system",
        "/etc/systemd/system.attached",
        "/run/systemd/system",
        "/run/systemd/system.attached",
        "/run/systemd/generator",
        "/usr/local/lib/systemd/system",
        "/usr/lib/systemd/system",
        "/run/systemd/generator.late",
    )
)
SYSTEMD_ANALYZE = ("/usr/bin/systemd-analyze", "unit-paths")
DEPENDENCY_DIRECTORIES = (".wants", ".requires", ".upholds")


def require(condition):
    if not condition:
        raise ValueError("host prerequisites rejected")


def endpoint(text):
    """Canonical private IPv4 and unprivileged port; no names, IPv6 or wildcards."""
    require(type(text) is str and text.count(":") == 1)
    address, port = text.split(":")
    require(PORT.fullmatch(port) is not None and 1024 <= int(port) <= 65535)
    ip = ipaddress.IPv4Address(address)
    require(str(ip) == address and any(ip in network for network in PRIVATE))
    return address, int(port)


def settings(document):
    require(type(document) is dict and set(document) == KEYS)
    require(document["schema"] == SCHEMA)
    require(document["shadow_only"] is True and document["weight_eligible"] is False)
    router = endpoint(document["router_listen"])
    proxy = document["egress_proxy"]
    require(type(proxy) is str and proxy.startswith("http://"))
    proxy = endpoint(proxy.removeprefix("http://"))
    # The router address is also the sandbox host gateway; the refusing proxy
    # listens beside it so one candidate_tcp address covers both listeners.
    require(router[0] == proxy[0] and router[1] != proxy[1])
    network = document["egress_network"]
    require(type(network) is str and NETWORK.fullmatch(network) is not None)
    # The runtime creates and removes its own ditto-job-<id> bridge per start.
    require(not network.startswith("ditto-job-"))
    for key in ("candidate_uid", "candidate_gid"):
        value = document[key]
        require(type(value) is int and 1 <= value <= SUBORDINATE_IDS)
    return router, proxy


def protected(path):
    # Mirrors connectivity-policy.py root_path(); sharing one copy across the
    # native roles is a separate follow-up.
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1)
    require(not info.st_mode & 0o022)
    for parent in path.parents:
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0)
        require(not info.st_mode & 0o022)


def host_policy():
    protected(HOST_POLICY)
    return runpy.run_path(str(HOST_POLICY))


def ephemeral_range():
    low, high = (int(value) for value in PORT_RANGE.read_text().split())
    require(1 <= low <= high <= 65535)
    return low, high


def bindable(address, port):
    """Prove the address is local and the exact listener is free, then release it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Match Go net.Listen and the proxy: TIME_WAIT from an earlier attempt is
        # free, while any live listener still makes bind fail with EADDRINUSE.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((address, port))


def unit_names():
    """Names systemd 257 reads for the unit besides its fragment.

    Drop-in and dependency directories use the full name, each dash-truncated
    prefix and the bare type (systemd.unit(5)); same-name socket, timer and path
    units would activate it.
    """
    stem, kind = UNIT.rsplit(".", 1)
    parts = stem.split("-")
    truncated = [f"{'-'.join(parts[:n])}-.{kind}" for n in range(len(parts) - 1, 0, -1)]
    suffixes = (".d", *DEPENDENCY_DIRECTORIES)
    names = [name + suffix for name in (UNIT, *truncated, kind) for suffix in suffixes]
    return names + [f"{stem}.{trigger}" for trigger in ("socket", "timer", "path")]


def search_path():
    output = subprocess.run(
        SYSTEMD_ANALYZE,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        timeout=30,
        check=True,
    ).stdout
    require(output.decode("ascii").splitlines() == [str(path) for path in UNIT_PATHS])
    return UNIT_PATHS


def reverse_links(directory):
    """Any other unit's .wants, .requires or .upholds entry naming the unit."""
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return False
    # lexists, not glob: a dangling link still pulls the unit in once installed.
    return any(
        entry.name.endswith(DEPENDENCY_DIRECTORIES)
        and os.path.lexists(os.path.join(entry.path, UNIT))
        for entry in entries
    )


def unit_overrides(paths):
    """Refuse any other fragment, alias, mask, drop-in, dependency or trigger."""
    for directory in paths:
        unit = directory / UNIT
        if unit == FRAGMENT and os.path.lexists(unit):
            protected(unit)
        else:
            require(not os.path.lexists(unit))
        for name in unit_names():
            require(not os.path.lexists(directory / name))
        require(not reverse_links(directory))


def check(document):
    require(os.geteuid() == 0)
    router, proxy = settings(document)
    host = host_policy()
    # Exact account, no host account inside either range, no overlap.
    host["identity"]()
    mapped = []
    for source, key in (
        ("/etc/subuid", "candidate_uid"),
        ("/etc/subgid", "candidate_gid"),
    ):
        start, end = host["mapping"](Path(source).read_text(), USER, [])
        # settings() bounds the ID to 1..SUBORDINATE_IDS, so this keeps it mapped.
        require(end - start == SUBORDINATE_IDS)
        mapped.append(start + document[key] - 1)
    low, high = ephemeral_range()
    for address, port in (router, proxy):
        require(port < low or port > high)
        bindable(address, port)
    unit_overrides(search_path())
    return {
        "schema": "dittobench-coding-hosted-host-prerequisites-check-v2",
        "shadow_only": True,
        "weight_eligible": False,
        "router_listen": document["router_listen"],
        "egress_proxy": document["egress_proxy"],
        "egress_network": document["egress_network"],
        "candidate_host_uid": mapped[0],
        "candidate_host_gid": mapped[1],
        "services_started": False,
        "private_execution_ready": False,
    }


def unique(entries):
    result = {}
    for key, value in entries:
        require(key not in result)
        result[key] = value
    return result


def main(argv, stream):
    require(argv == ["check"])
    body = stream.read(4097)
    require(0 < len(body) <= 4096)
    document = json.loads(body, object_pairs_hook=unique)
    print(json.dumps(check(document), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    try:
        main(sys.argv[1:], sys.stdin.buffer)
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("coding hosted host prerequisites check failed", file=sys.stderr)
        raise SystemExit(1) from None
