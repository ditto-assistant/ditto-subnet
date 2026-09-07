#!/usr/bin/python3
"""Explicit, expiring cgroup-bound network authority for one trusted worker."""

import ipaddress
import json
import os
import runpy
import stat
import subprocess
import sys
import time
from pathlib import Path

TABLE = "ditto_coding_hosted"
CHAIN = "scoped_output"
WORKER = "system.slice/ditto-coding-hosted-worker.service"
CONFIG = Path("/etc/ditto-coding-hosted/connectivity.json")
HOST_POLICY = Path("/usr/local/lib/ditto-coding-hosted/host-policy.py")


def require(condition):
    if not condition:
        raise ValueError("native connectivity policy rejected")


def pairs(items, *, candidate=False, dns=False):
    require(type(items) is list and len(items) <= (2 if candidate or dns else 32))
    result = []
    for item in items:
        require(type(item) is dict and set(item) == {"address", "port"})
        address, port = item["address"], item["port"]
        require(type(address) is str and type(port) is int and 1 <= port <= 65535)
        ip = ipaddress.IPv4Address(address)
        require(str(ip) == address)
        require(
            not (
                ip.is_multicast
                or ip.is_unspecified
                or ip.is_link_local
                or ip.is_reserved
            )
        )
        if candidate:
            require(
                any(
                    ip in network
                    for network in (
                        ipaddress.IPv4Network("10.0.0.0/8"),
                        ipaddress.IPv4Network("172.16.0.0/12"),
                        ipaddress.IPv4Network("192.168.0.0/16"),
                    )
                )
                and port >= 1024
            )
        if dns:
            require(port == 53)
        pair = (address, port)
        require(pair not in result)
        result.append(pair)
    return sorted(result)


def policy(config, uid, now):
    """Compile only fixed paths, numeric endpoints and kernel-expiring authority."""
    require(type(uid) is int and 1000 <= uid < 2**32 - 1)
    require(type(now) is int and now > 0)
    require(
        type(config) is dict
        and set(config)
        == {
            "schema",
            "shadow_only",
            "weight_eligible",
            "issued_at_unix",
            "expires_at_unix",
            "trusted_tcp",
            "trusted_dns",
            "trusted_loopback_tcp",
            "candidate_tcp",
        }
    )
    require(config["schema"] == "dittobench-coding-hosted-connectivity-v2")
    require(config["shadow_only"] is True and config["weight_eligible"] is False)
    require(type(config["trusted_loopback_tcp"]) is bool)
    issued, expires = config["issued_at_unix"], config["expires_at_unix"]
    require(type(issued) is int and type(expires) is int)
    require(0 <= now - issued <= 300 and 0 < expires - now <= 86400)
    require(expires - issued <= 86400 and expires < 2**32)
    tcp = pairs(config["trusted_tcp"])
    dns = pairs(config["trusted_dns"], dns=True)
    candidate = pairs(config["candidate_tcp"], candidate=True)
    require(tcp and candidate)
    daemon = f"user.slice/user-{uid}.slice/user@{uid}.service"
    lines = [
        f"add table inet {TABLE}",
        f"flush table inet {TABLE}",
        f"add chain inet {TABLE} {CHAIN} {{ type filter hook output "
        "priority -150; policy accept; }",
    ]
    # Every accept depends on a timed cgroup element. Expiry also cuts existing
    # connections; there is deliberately no blanket established/related bypass.
    for name, path in (("worker", WORKER), ("daemon", daemon)):
        lines.extend(
            [
                f"add set inet {TABLE} {name} {{ type cgroupv2; flags timeout; }}",
                f'add element inet {TABLE} {name} {{ "{path}" '
                f"timeout {expires - now}s }}",
            ]
        )
    prefix = f"add rule inet {TABLE} {CHAIN} meta skuid {uid}"
    window = f"meta time >= {issued} meta time < {expires}"
    worker = f"{prefix} {window} socket cgroupv2 level 2 @worker"
    daemon_rule = f"{prefix} {window} socket cgroupv2 level 3 @daemon"
    if config["trusted_loopback_tcp"]:
        lines.append(f"{worker} ip daddr 127.0.0.1 meta l4proto tcp counter accept")
    for address, port in tcp:
        lines.append(f"{worker} ip daddr {address} tcp dport {port} counter accept")
    for address, port in dns:
        for protocol in ("tcp", "udp"):
            lines.append(
                f"{worker} ip daddr {address} {protocol} dport {port} counter accept"
            )
    for address, port in candidate:
        lines.append(
            f"{daemon_rule} ip daddr {address} tcp dport {port} counter accept"
        )
        # Only replies from these scoped local listeners, not arbitrary inbound
        # connections, gain a worker response path.
        lines.append(
            f"{worker} ip saddr {address} tcp sport {port} ct direction reply "
            "ct state established counter accept"
        )
    # The trusted worker reaches Docker-published harness ports via loopback.
    # Daemon replies do not authorize a new outbound candidate connection.
    lines.append(
        f"{daemon_rule} ip daddr 127.0.0.1 meta l4proto tcp ct direction reply "
        "ct state established counter accept"
    )
    lines.append(f"{prefix} counter reject with icmpx type admin-prohibited")
    return "\n".join(lines) + "\n"


def root_path(path, *, directory=False, private=False):
    require(path.is_absolute() and path.resolve() == path)
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o022)
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    if not directory:
        require(info.st_nlink == 1)
    if private:
        require(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600))
    for parent in path.parents:
        info = parent.lstat()
        require(
            stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
        )


def unique(entries):
    result = {}
    for key, value in entries:
        require(key not in result)
        result[key] = value
    return result


def configuration():
    root_path(CONFIG.parent, directory=True, private=True)
    root_path(CONFIG, private=True)
    fd = os.open(CONFIG, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0)
        require(stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)
        require(0 < info.st_size <= 16384)
        body = stream.read(16385)
        require(len(body) == info.st_size)
    return json.loads(body, object_pairs_hook=unique)


def worker_cgroup():
    root = Path("/sys/fs/cgroup")
    require((root / "cgroup.controllers").is_file())
    require(Path("/proc/self/cgroup").read_text().strip() == f"0::/{WORKER}")
    path = root / WORKER
    root_path(path, directory=True)
    for filename in ("cgroup.procs", "cgroup.threads", "cgroup.subtree_control"):
        root_path(path / filename)
    require(not (path / "cgroup.subtree_control").read_text().strip())
    return path.stat().st_ino


def execute(payload, *, check=False):
    command = ["/usr/sbin/nft"]
    if check:
        command.append("--check")
    command.extend(["--file", "-"])
    subprocess.run(
        command,
        input=payload.encode(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
        timeout=20,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )


def main(argv):
    require(argv in (["validate"], ["install"], ["revoke"]))
    require(os.geteuid() == 0)
    root_path(HOST_POLICY)
    host = runpy.run_path(str(HOST_POLICY))
    uid = host["identity"]().pw_uid
    deny = host["nft_policy"](uid)
    if argv == ["validate"]:
        policy(configuration(), uid, int(time.time()))
        return
    if argv == ["revoke"]:
        execute(deny)
        return
    inode = worker_cgroup()
    # Fail closed before processing fresh authority, including malformed config.
    execute(deny)
    try:
        config = configuration()
        compiled = policy(config, uid, int(time.time()))
        execute(compiled, check=True)
        require(worker_cgroup() == inode)
        # Recompute the timeout after syntax/kernel checks so their elapsed time
        # cannot extend the approved window.
        execute(policy(config, uid, int(time.time())))
        require(worker_cgroup() == inode)
    except BaseException:
        execute(deny)
        raise


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("coding hosted connectivity unavailable", file=sys.stderr)
        raise SystemExit(1) from None
