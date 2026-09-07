#!/usr/bin/python3
"""Fixed-host qualification checks. Only the root-only guard mode changes state."""

import grp
import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

USER = "ditto-coding-hosted"
DAEMON_HOME = Path("/var/lib/ditto-coding-hosted")
SOCKET = Path("/run/ditto-coding-hosted/docker.sock")
TABLE = "ditto_coding_hosted"


def require(condition):
    if not condition:
        raise ValueError("host policy rejected")


def mapping(source, name, host_ids):
    """Accept one complete, nonoverlapping subordinate range, never allocate it."""
    rows = []
    for line in source.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        owner, start, count = line.split(":")
        require(start.isdecimal() and count.isdecimal())
        start, count = int(start), int(count)
        require(start >= 1 and count >= 1 and start + count < 2**32)
        rows.append((owner, start, start + count))
    selected = [row for row in rows if row[0] == name]
    require(len(selected) == 1)
    _, start, end = selected[0]
    require(start >= 100000 and end - start == 65536)
    require(all(not start <= host_id < end for host_id in host_ids))
    require(
        all(owner == name or end <= low or high <= start for owner, low, high in rows)
    )
    return start, end


def identity():
    user = pwd.getpwnam(USER)
    group = grp.getgrnam(USER)
    require(user.pw_uid >= 1000 and user.pw_gid >= 1000 and user.pw_gid == group.gr_gid)
    require(user.pw_dir == str(DAEMON_HOME) and user.pw_shell == "/usr/sbin/nologin")
    require(set(os.getgrouplist(USER, user.pw_gid)) == {user.pw_gid})
    require(set(group.gr_mem) <= {USER})
    require(
        all(
            item.pw_name == USER
            or (item.pw_gid != user.pw_gid and item.pw_uid != user.pw_uid)
            for item in pwd.getpwall()
        )
    )
    subuid = Path("/etc/subuid").read_text()
    subgid = Path("/etc/subgid").read_text()
    require(
        not any(
            line.startswith(f"{user.pw_uid}:")
            for line in (subuid + "\n" + subgid).splitlines()
        )
    )
    mapping(subuid, USER, [p.pw_uid for p in pwd.getpwall()])
    mapping(subgid, USER, [g.gr_gid for g in grp.getgrall()])
    return user


def private_path(path, uid, mode, socket=False):
    info = path.lstat()
    require(path.resolve() == path and info.st_uid == uid)
    require(stat.S_IMODE(info.st_mode) == mode)
    require(stat.S_ISSOCK(info.st_mode) if socket else stat.S_ISDIR(info.st_mode))


def nft_policy(uid):
    require(type(uid) is int and 1000 <= uid < 2**32 - 1)
    # add (not create) permits the table to exist. The flush and replacement
    # commit in one nft transaction, never a transient accept-all window.
    return (
        f"add table inet {TABLE}\n"
        f"flush table inet {TABLE}\n"
        f"add chain inet {TABLE} output {{ type filter hook output "
        "priority -310; policy accept; }\n"
        f"add rule inet {TABLE} output meta skuid {uid} "
        "counter reject with icmpx type admin-prohibited\n"
    )


def execute(args, *, payload=None, uid=None):
    env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    if uid is not None:
        env.update(
            {
                "DOCKER_HOST": f"unix://{SOCKET}",
                "DOCKER_CONFIG": str(DAEMON_HOME / "empty-client"),
            }
        )
    result = subprocess.run(
        args,
        input=payload,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        timeout=30,
        check=True,
    )
    require(len(result.stdout) <= 1024 * 1024)
    return result.stdout


def validate_info(info):
    require(isinstance(info, dict))
    require(info.get("OSType") == "linux" and info.get("Architecture") == "x86_64")
    require(info.get("DockerRootDir") == str(DAEMON_HOME / "docker"))
    require(
        any(
            value == "name=rootless"
            or value == "rootless"
            or value.startswith("name=rootless,")
            for value in info.get("SecurityOptions", [])
            if isinstance(value, str)
        )
    )
    require(isinstance(info.get("Labels"), list))
    require("io.heyditto.dittobench.isolated=true" in info["Labels"])
    require(info.get("CgroupDriver") == "systemd" and info.get("CgroupVersion") == "2")
    for field in ("Containers", "Images"):
        require(type(info.get(field)) is int and info[field] == 0)
    for field in ("MemoryLimit", "SwapLimit", "CpuCfsQuota", "PidsLimit"):
        require(info.get(field) is True)


def main(argv):
    require(argv in (["identity"], ["guard"], ["verify"]))
    user = identity()
    if argv == ["identity"]:
        print(user.pw_uid)
        return
    if argv == ["guard"]:
        require(os.geteuid() == 0)
        execute(["/usr/sbin/nft", "-f", "-"], payload=nft_policy(user.pw_uid))
        return
    require(os.geteuid() == user.pw_uid)
    private_path(DAEMON_HOME, user.pw_uid, 0o700)
    private_path(SOCKET.parent, user.pw_uid, 0o700)
    private_path(SOCKET, user.pw_uid, 0o600, socket=True)
    private_path(DAEMON_HOME / "empty-client", user.pw_uid, 0o700)
    require(not list((DAEMON_HOME / "empty-client").iterdir()))
    validate_info(
        json.loads(
            execute(
                ["/usr/bin/docker", "info", "--format", "{{json .}}"], uid=user.pw_uid
            )
        )
    )
    print(
        json.dumps(
            {
                "schema": "dittobench-coding-hosted-daemon-check-v2",
                "shadow_only": True,
                "weight_eligible": False,
                "private_execution_ready": False,
            }
        )
    )


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("coding hosted daemon check failed", file=sys.stderr)
        raise SystemExit(1) from None
