"""The live Docker-access probe that gates the hosted control signer.

Synthetic process tables and account databases only; nothing reads the real
host's /proc or group files, and no Docker daemon is involved.
"""

from __future__ import annotations

import grp
import importlib.util
import os
import pwd
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
PROBE = ROOT / "infra/ansible/roles/platform_app/files/deploy-docker-access.py"
spec = importlib.util.spec_from_file_location("deploy_docker_access", PROBE)
assert spec is not None and spec.loader is not None
DOCKER = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DOCKER)

DEPLOY_UID = 1000
DEPLOY_GID = 1000
DOCKER_GID = 998


def _account(name: str, gid: int = DEPLOY_GID) -> pwd.struct_passwd:
    if name != "deploy":
        raise KeyError(name)
    return pwd.struct_passwd(("deploy", "x", DEPLOY_UID, gid, "", "/home/deploy", ""))


def _groups(members: list[str]) -> dict[int, grp.struct_group]:
    return {
        DOCKER_GID: grp.struct_group(("docker", "x", DOCKER_GID, members)),
        0: grp.struct_group(("root", "x", 0, [])),
        DEPLOY_GID: grp.struct_group(("deploy", "x", DEPLOY_GID, [])),
    }


def _process(
    proc: Path,
    pid: int,
    *,
    uids: tuple[int, int, int, int] = (DEPLOY_UID,) * 4,
    gids: tuple[int, int, int, int] = (DEPLOY_GID,) * 4,
    groups: tuple[int, ...] = (),
    name: str = "PM2 v7.0.3: God Daemon",
) -> None:
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    (directory / "status").write_text(
        f"Name:\t{name}\n"
        f"Uid:\t{' '.join(map(str, uids))}\n"
        f"Gid:\t{' '.join(map(str, gids))}\n"
        f"Groups:\t{' '.join(map(str, groups))}\n"
    )


def _check(
    proc: Path,
    *,
    members: list[str] | None = None,
    primary: int = DEPLOY_GID,
    sock: Path | None = None,
    xattrs: list[str] | None = None,
) -> list[str]:
    groups = _groups(members or [])
    return DOCKER.check(
        "deploy",
        "docker",
        proc,
        sock or proc.parent / "no-docker.sock",
        getpwnam=lambda name: _account(name, primary),
        getgrnam=lambda name: next(g for g in groups.values() if g.gr_name == name),
        getgrgid=lambda gid: groups[gid],
        listxattr=lambda _path: xattrs or [],
    )


def test_a_clean_host_passes(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    _process(proc, 100, groups=(DEPLOY_GID,))
    # Other users may hold the Docker group; only the deploy UID matters.
    _process(proc, 101, uids=(0, 0, 0, 0), gids=(0, 0, 0, 0), groups=(DOCKER_GID,))
    (proc / "self").mkdir()
    assert _check(proc) == []


def test_group_membership_or_primary_group_is_refused(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    assert _check(proc, members=["deploy"]) == [
        "deploy is a member of docker, which opens the Docker daemon"
    ]
    assert _check(proc, primary=DOCKER_GID) == [
        "deploy's primary group docker opens the Docker daemon"
    ]


@pytest.mark.parametrize(
    "shape",
    ["supplementary", "effective-gid", "saved-uid", "filesystem-uid"],
)
def test_a_running_process_that_kept_the_group_is_refused(
    tmp_path: Path, shape: str
) -> None:
    """The pm2 daemon keeps the groups it started with after `gpasswd -d`."""
    proc = tmp_path / "proc"
    if shape == "supplementary":
        _process(proc, 4242, groups=(DEPLOY_GID, DOCKER_GID))
    elif shape == "effective-gid":
        _process(proc, 4242, gids=(DEPLOY_GID, DOCKER_GID, DEPLOY_GID, DEPLOY_GID))
    elif shape == "saved-uid":
        _process(proc, 4242, uids=(0, 0, DEPLOY_UID, 0), groups=(DOCKER_GID,))
    else:
        _process(proc, 4242, uids=(0, 0, 0, DEPLOY_UID), groups=(DOCKER_GID,))
    (problem,) = _check(proc)
    assert problem.startswith("process 4242 (PM2 v7.0.3: God Daemon) runs as the user")


def test_the_socket_group_counts_even_when_the_docker_group_is_missing(
    tmp_path: Path,
) -> None:
    """A renamed or removed docker group does not hide the socket's real group."""
    proc = tmp_path / "proc"
    socket_gid = os.getgid()
    _process(proc, 7, uids=(os.getuid(),) * 4, gids=(1,) * 4, groups=(socket_gid,))
    listener = socket.socket(socket.AF_UNIX)
    sock = tmp_path / "docker.sock"
    listener.bind(str(sock))
    sock.chmod(0o660)

    def missing(name: str) -> grp.struct_group:
        raise KeyError(name)

    try:
        problems = DOCKER.check(
            "deploy",
            "docker",
            proc,
            sock,
            getpwnam=lambda name: pwd.struct_passwd(
                (name, "x", os.getuid(), 1, "", "/", "")
            ),
            getgrnam=missing,
            getgrgid=lambda gid: grp.struct_group(("socket-group", "x", gid, [])),
            listxattr=lambda _path: [],
        )
    finally:
        listener.close()
    assert any(
        problem.startswith("process 7 (PM2 v7.0.3: God Daemon) runs as the user")
        for problem in problems
    ), problems


@pytest.mark.parametrize("fault", ["other-access", "acl", "not-a-socket"])
def test_socket_permissions_that_bypass_the_group_are_refused(
    tmp_path: Path, fault: str
) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    sock = tmp_path / "docker.sock"
    listener = socket.socket(socket.AF_UNIX)
    xattrs: list[str] = []
    if fault == "not-a-socket":
        sock.write_text("")
    else:
        listener.bind(str(sock))
    if fault == "other-access":
        sock.chmod(0o666)
    elif fault == "acl":
        sock.chmod(0o660)
        xattrs = ["system.posix_acl_access"]
    try:
        problems = _check(proc, sock=sock, xattrs=xattrs)
    finally:
        listener.close()
    expected = {
        "other-access": f"{sock} grants access to other users",
        "acl": f"{sock} carries a POSIX ACL",
        "not-a-socket": f"{sock} is not a socket",
    }[fault]
    assert expected in problems
    if os.geteuid() != 0:
        assert f"{sock} is not owned by root" in problems


def test_command_line_exit_codes_and_bounded_output(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    user = pwd.getpwuid(os.getuid())
    primary = grp.getgrgid(user.pw_gid).gr_name

    def run(group: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                str(PROBE),
                f"--user={user.pw_name}",
                f"--group={group}",
                f"--proc={proc}",
                f"--socket={tmp_path / 'absent.sock'}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    for pid in range(30):
        _process(proc, 1000 + pid, uids=(os.getuid(),) * 4, groups=(user.pw_gid,))
    refused = run(primary)
    assert refused.returncode == 1
    lines = refused.stdout.splitlines()
    assert (
        lines[0] == f"{user.pw_name}'s primary group {primary} opens the Docker daemon"
    )
    assert len(lines) == 21 and lines[-1] == "... and 11 more"

    if user.pw_gid != 0 and os.getuid() != 0:
        for entry in proc.iterdir():
            (entry / "status").write_text(
                f"Uid:\t{os.getuid()} {os.getuid()} {os.getuid()} {os.getuid()}\n"
                f"Gid:\t{user.pw_gid} {user.pw_gid} {user.pw_gid} {user.pw_gid}\n"
                f"Groups:\t{user.pw_gid}\n"
            )
        accepted = run("root")
        if user.pw_name not in grp.getgrnam("root").gr_mem:
            assert accepted.returncode == 0, accepted.stdout
            assert "has no Docker group, process or socket access" in accepted.stdout

    broken = proc / "9999"
    broken.mkdir()
    (broken / "status").write_text("Name:\tbroken\n")
    unreadable = run(primary)
    assert unreadable.returncode == 2
    assert unreadable.stdout == "docker access check could not run: ValueError\n"
