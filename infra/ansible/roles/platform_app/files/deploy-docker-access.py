#!/usr/bin/python3 -I
"""Fail closed unless a user has no live route to the root Docker daemon.

Run as root by the hosted signer converge guard and by the root release
installer before the control signer may be used. Docker access is
root-equivalent: a user with it can mount /etc/ditto-platform into a container
and read the seed as root. Switches only change files on disk. A long-lived
process, the deploy user's pm2 daemon above all, keeps the supplementary groups
it started with. So this checks the state of the running host:

  * for the named Docker group and for whichever group owns the socket: it is
    not the account's primary group and the account is not a member;
  * no process whose real, effective, saved or filesystem UID is the account
    holds such a GID in any GID field or in its supplementary groups;
  * the Docker socket is owned by root, grants nothing to "other" and carries
    no POSIX ACL.

It reads only /proc/<pid>/status, the account and group databases and socket
metadata. Exit 0: no access found. Exit 1: access found (reasons on stdout).
Exit 2: the check itself could not run.
"""

from __future__ import annotations

import argparse
import contextlib
import grp
import os
import pwd
import stat
import sys
from collections.abc import Callable
from pathlib import Path

ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")
MAX_REPORTED = 20


def _ids(line: str) -> set[int]:
    return {int(value) for value in line.split(":", 1)[1].split()}


def process_problems(proc_root: Path, uid: int, gid: int) -> list[str]:
    problems: list[str] = []
    for entry in sorted(proc_root.iterdir(), key=lambda path: path.name):
        if not entry.name.isdigit():
            continue
        try:
            lines = (entry / "status").read_text().splitlines()
        except (FileNotFoundError, ProcessLookupError):
            continue  # exited while scanning
        fields = {line.split(":", 1)[0]: line for line in lines if ":" in line}
        if not {"Uid", "Gid", "Groups"} <= fields.keys():
            raise ValueError(f"process {entry.name} has no Uid/Gid/Groups")
        if uid not in _ids(fields["Uid"]):
            continue
        if gid in _ids(fields["Gid"]) | _ids(fields["Groups"]):
            name = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in lines
                    if line.startswith("Name:")
                ),
                "?",
            )
            problems.append(
                f"process {entry.name} ({name}) runs as the user and still holds "
                "the Docker group; restart it (for pm2: its systemd unit)"
            )
    return problems


def socket_problems(
    socket: Path, info: os.stat_result, listxattr: Callable[[Path], list[str]]
) -> list[str]:
    problems: list[str] = []
    if not stat.S_ISSOCK(info.st_mode):
        problems.append(f"{socket} is not a socket")
    if info.st_uid != 0:
        problems.append(f"{socket} is not owned by root")
    if info.st_mode & 0o007:
        problems.append(f"{socket} grants access to other users")
    try:
        names = listxattr(socket)
    except OSError:
        names = []
    if any(name in ACL_XATTRS for name in names):
        problems.append(f"{socket} carries a POSIX ACL")
    return problems


def check(
    user: str,
    group: str,
    proc_root: Path,
    socket: Path,
    *,
    getpwnam: Callable[[str], pwd.struct_passwd] = pwd.getpwnam,
    getgrnam: Callable[[str], grp.struct_group] = grp.getgrnam,
    getgrgid: Callable[[int], grp.struct_group] = grp.getgrgid,
    listxattr: Callable[[Path], list[str]] = os.listxattr,
) -> list[str]:
    account = getpwnam(user)
    problems: list[str] = []
    # Every group that opens the daemon: the named Docker group and whichever
    # group actually owns the socket.
    gids: set[int] = set()
    with contextlib.suppress(KeyError):
        gids.add(getgrnam(group).gr_gid)
    try:
        info = socket.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        gids.add(info.st_gid)
        problems.extend(socket_problems(socket, info, listxattr))
    for gid in sorted(gids):
        try:
            entry = getgrgid(gid)
            members, name = entry.gr_mem, entry.gr_name
        except KeyError:
            members, name = [], str(gid)
        if account.pw_gid == gid:
            problems.append(f"{user}'s primary group {name} opens the Docker daemon")
        if user in members:
            problems.append(
                f"{user} is a member of {name}, which opens the Docker daemon"
            )
        problems.extend(process_problems(proc_root, account.pw_uid, gid))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--proc", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        problems = check(
            arguments.user, arguments.group, arguments.proc, arguments.socket
        )
    except (KeyError, OSError, ValueError) as error:
        print(f"docker access check could not run: {type(error).__name__}")
        return 2
    for problem in problems[:MAX_REPORTED]:
        print(problem)
    if len(problems) > MAX_REPORTED:
        print(f"... and {len(problems) - MAX_REPORTED} more")
    if problems:
        return 1
    print(f"{arguments.user} has no Docker group, process or socket access")
    return 0


if __name__ == "__main__":
    sys.exit(main())
