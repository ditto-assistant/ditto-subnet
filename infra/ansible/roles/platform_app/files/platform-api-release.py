#!/usr/bin/python3 -I
"""Sealed releases for ditto-api under its dedicated service identity.

Installed by infra/ansible/roles/platform_app (tasks/api_service_identity.yml)
as /usr/local/sbin/ditto-platform-api-release, owned by root. The deploy user
may run exactly these four argument vectors through sudo
(/etc/sudoers.d/ditto-platform-api), and nothing else as root:

  install    stdin: ``revision=<sha>`` then the deploy-owned .env.deploy lines
  activate   stdin: ``revision=<sha>``
  stop       no stdin
  logs       no stdin

``install`` fetches the revision from GitHub into fresh root-only Git
directories, refuses it unless it is reachable from ``main``, extracts
``git archive`` output, copies the deploy-built dashboard as plain data, builds
the Python environment as the unprivileged ``ditto-api-build`` user inside
sandboxed transient units (hash-pinned build backends, no build isolation),
then seals the tree root-owned and records a manifest digest. Finally it runs
the hosted signer metadata preflight from that release as ``ditto-api``.
``activate`` re-verifies the digest, points ``current`` at the release and
restarts ditto-platform-api.service. While the root-owned environment enables
the control signer, both refuse unless the deploy user has no live route to the
Docker daemon.

Nothing here opens, stats, hashes or copies the control-signer seed. Diagnostics
never echo a deploy-supplied value.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import hashlib
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

COMMANDS = ("install", "activate", "stop", "logs")
BRANCH = "main"
REVISION = re.compile(r"[0-9a-f]{40}")
# Exactly the keys scripts/update.sh owns in .env.deploy. A value may hold any
# printable ASCII character that stays literal inside the single quotes this
# installer writes, which covers every URL update.sh's upsert_env accepts
# (`&`, `|`, `?`, `~` ...). Refused: whitespace, control characters (newline,
# NUL), quotes, backquotes, `$` and backslashes, so nothing can expand or
# end the quoting.
DEPLOY_KEYS = frozenset(
    {
        "DITTO_UPLOAD_PAYMENT_ADDRESS",
        "DITTO_DASHBOARD_WANDB_URL",
        "DITTO_TAOSTATS_API_KEY",
        "DITTO_TAOSTATS_VALIDATOR_NAMES_URL",
        "SUBTENSOR_ARCHIVE_RPC_API_KEY",
        "SUBTENSOR_ARCHIVE_RPC_AUTH_MODE",
        "SUBTENSOR_ARCHIVE_RPC_URL",
    }
)
DEPLOY_VALUE = re.compile(r"[!#%&()*+,./0-9:;<=>?@A-Z\[\]^_a-z{|}~-]{1,1024}")
PYTHON = re.compile(r"/usr/bin/python3\.[0-9]{1,2}")
ACCOUNT = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
DASHBOARD_NAME = re.compile(r"[A-Za-z0-9_@+-][A-Za-z0-9._@+-]{0,254}")
MAX_REQUEST = 16 << 10
MAX_DASHBOARD_FILES = 4096
MAX_DASHBOARD_BYTES = 256 << 20
MAX_DASHBOARD_DEPTH = 8
RECEIPT = ".ditto-platform-api-release.json"
RECEIPT_SCHEMA = "ditto-platform-api-release-v1"
# The transient units that build the environment and run the preflight get the
# same filesystem and privilege boundary as ditto-platform-api.service, minus
# anything the step does not need.
SANDBOX = (
    "NoNewPrivileges=yes",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
    "ProtectSystem=strict",
    "ProtectHome=yes",
    "PrivateTmp=yes",
    "PrivateDevices=yes",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "ProtectProc=invisible",
    "RestrictSUIDSGID=yes",
    "RestrictNamespaces=yes",
    "LockPersonality=yes",
    "UMask=0022",
)


class ReleaseError(RuntimeError):
    """A fixed diagnostic: never a deploy-supplied value or file content."""


@dataclasses.dataclass(frozen=True)
class Host:
    """Every path and account the installer touches. Tests substitute a tree."""

    api_root: Path = Path("/opt/ditto-platform-api")
    config_dir: Path = Path("/etc/ditto-platform/api")
    work_root: Path = Path("/var/lib/ditto-platform-api-release")
    builder_home: Path = Path("/var/lib/ditto-platform-api-build")
    dashboard_source: Path = Path("/opt/ditto-subnet/apps/platform/dashboard/dist")
    launcher: Path = Path("/usr/local/libexec/ditto-platform-api/launch")
    docker_probe: Path = Path(
        "/usr/local/libexec/ditto-platform-api/deploy-docker-access"
    )
    proc_root: Path = Path("/proc")
    docker_socket: Path = Path("/run/docker.sock")
    repository: str = "git@github.com:ditto-assistant/ditto-subnet.git"
    git_protocols: str = "ssh"
    unit: str = "ditto-platform-api.service"
    service_user: str = "ditto-api"
    builder_user: str = "ditto-api-build"
    trusted_uid: int = 0
    trusted_gid: int = 0
    # Ownership checks stop at this directory (inclusive). Production is "/".
    anchor: Path = Path("/")
    git: str = "/usr/bin/git"
    ssh: str = "/usr/bin/ssh"
    uv: str = "/usr/local/bin/uv"
    systemd_run: str = "/usr/bin/systemd-run"
    systemctl: str = "/usr/bin/systemctl"
    journalctl: str = "/usr/bin/journalctl"

    @property
    def releases(self) -> Path:
        return self.api_root / "releases"

    @property
    def current(self) -> Path:
        return self.api_root / "current"

    @property
    def github_key(self) -> Path:
        return self.config_dir / "github-deploy-key"

    @property
    def known_hosts(self) -> Path:
        return self.config_dir / "github-known-hosts"

    @property
    def settings_file(self) -> Path:
        return self.config_dir / "release.json"

    @property
    def platform_env(self) -> Path:
        return self.config_dir / "platform.env"

    @property
    def deploy_env(self) -> Path:
        return self.config_dir / "deploy.env"

    def staged_deploy_env(self, revision: str) -> Path:
        return self.config_dir / f"deploy-{revision}.env"


@dataclasses.dataclass(frozen=True)
class Settings:
    python: Path
    platform_owner: str


@dataclasses.dataclass(frozen=True)
class Request:
    revision: str
    values: tuple[tuple[str, str], ...] = ()


Runner = Callable[..., subprocess.CompletedProcess[bytes]]


def run_command(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    stdout: IO[bytes] | int | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """No shell, no inherited environment, no stdin."""
    return subprocess.run(
        list(argv),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        cwd=cwd,
        check=False,
    )


# --- Requests ---------------------------------------------------------------


def parse_request(data: bytes, *, with_values: bool) -> Request:
    if len(data) > MAX_REQUEST:
        raise ReleaseError("request is larger than its bound")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ReleaseError("request is not ASCII") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines or not lines[0].startswith("revision="):
        raise ReleaseError("request must start with revision=<40-character sha>")
    revision = lines[0].removeprefix("revision=")
    if not REVISION.fullmatch(revision):
        raise ReleaseError("request must start with revision=<40-character sha>")
    values: dict[str, str] = {}
    for number, line in enumerate(lines[1:], start=2):
        key, separator, value = line.partition("=")
        if not separator or key not in DEPLOY_KEYS:
            # Never echo the line: it is not a known key, so it may be a value.
            raise ReleaseError(f"request line {number} is not an allowed deploy key")
        if not with_values:
            raise ReleaseError(f"{key}: activate requests carry no deploy values")
        if key in values:
            raise ReleaseError(f"{key} appears more than once")
        if not DEPLOY_VALUE.fullmatch(value):
            raise ReleaseError(
                f"{key} is empty, longer than 1024 characters, or holds whitespace, "
                "a control character, a quote, a backquote, `$` or a backslash"
            )
        values[key] = value
    return Request(revision, tuple(sorted(values.items())))


# --- Trusted host inputs ----------------------------------------------------


def _chain(path: Path, host: Host) -> Iterator[Path]:
    yield path
    for parent in path.parents:
        yield parent
        if parent == host.anchor:
            return


def _lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError:
        raise ReleaseError(f"{path} is missing or unreadable") from None


def require_trusted_directory(path: Path, host: Host) -> None:
    for part in _chain(path, host):
        info = _lstat(part)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != host.trusted_uid
            or info.st_mode & 0o022
        ):
            raise ReleaseError(
                f"{part} must be a real directory owned by root without group "
                "or world write"
            )


def require_trusted_file(path: Path, host: Host, *, modes: set[int]) -> None:
    require_trusted_directory(path.parent, host)
    info = _lstat(path)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != host.trusted_uid
        or stat.S_IMODE(info.st_mode) not in modes
        or info.st_nlink != 1
    ):
        raise ReleaseError(f"{path} must be a single-link root-owned file")


def load_settings(host: Host) -> Settings:
    require_trusted_file(host.settings_file, host, modes={0o644, 0o444})
    try:
        document = json.loads(host.settings_file.read_bytes())
    except (OSError, ValueError):
        raise ReleaseError(f"{host.settings_file} is not valid JSON") from None
    if (
        not isinstance(document, dict)
        or set(document) != {"python", "platform_owner"}
        or not isinstance(document["python"], str)
        or not PYTHON.fullmatch(document["python"])
        or not isinstance(document["platform_owner"], str)
        or not ACCOUNT.fullmatch(document["platform_owner"])
        or document["platform_owner"] in {"root", host.service_user, host.builder_user}
    ):
        raise ReleaseError(f"{host.settings_file} has unexpected settings")
    return Settings(Path(document["python"]), document["platform_owner"])


def account(name: str) -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        raise ReleaseError(f"account {name} does not exist") from None


def require_distinct_accounts(host: Host, settings: Settings) -> None:
    """ditto-api, the builder, deploy and root must be four different UIDs."""
    uids = [
        account(host.service_user).pw_uid,
        account(host.builder_user).pw_uid,
        account(settings.platform_owner).pw_uid,
        host.trusted_uid,
    ]
    if len(set(uids)) != len(uids):
        raise ReleaseError(
            "ditto-api, ditto-api-build, deploy and root must be distinct"
        )


def require_host(host: Host, settings: Settings) -> None:
    for directory in (host.api_root, host.releases, host.work_root):
        require_trusted_directory(directory, host)
    if stat.S_IMODE(_lstat(host.work_root).st_mode) != 0o700:
        raise ReleaseError(f"{host.work_root} must have mode 0700")
    require_trusted_file(host.github_key, host, modes={0o600, 0o400})
    require_trusted_file(host.known_hosts, host, modes={0o644, 0o444})
    for executable in (
        settings.python,
        Path(host.git),
        Path(host.ssh),
        Path(host.uv),
        Path(host.systemd_run),
        Path(host.systemctl),
    ):
        require_trusted_file(executable, host, modes={0o755, 0o555})
    require_trusted_file(host.launcher, host, modes={0o755, 0o555})
    require_trusted_file(host.docker_probe, host, modes={0o755, 0o555})
    require_distinct_accounts(host, settings)
    builder = account(host.builder_user)
    info = _lstat(host.builder_home)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != builder.pw_uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise ReleaseError(f"{host.builder_home} must be a 0700 builder directory")
    require_trusted_directory(host.builder_home.parent, host)


def signer_enabled(host: Host) -> bool:
    """Read the activation flag the unit will start with, the way the loader does.

    Only an explicit false or 0 disables; any other value, including one the
    loader would reject, counts as enabled so the Docker check still runs.
    """
    require_trusted_file(host.platform_env, host, modes={0o640})
    value = "false"
    for line in host.platform_env.read_text().splitlines():
        if line.startswith("DITTO_CODING_HOSTED_CONTROL_ENABLED="):
            value = line.split("=", 1)[1].strip().strip("'\"").lower()
    return value not in {"false", "0"}


def require_deploy_without_docker(host: Host, settings: Settings, run: Runner) -> None:
    """Refuse while any live deploy process could reach the root Docker daemon.

    Docker access is root-equivalent, so it would reach the seed. Group files
    alone are not enough: a running pm2 daemon keeps its old groups.
    """
    result = run(
        [
            str(settings.python),
            "-I",
            str(host.docker_probe),
            f"--user={settings.platform_owner}",
            "--group=docker",
            f"--proc={host.proc_root}",
            f"--socket={host.docker_socket}",
        ],
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
    )
    if result.returncode != 0:
        raise ReleaseError(
            f"{settings.platform_owner} can still reach the Docker daemon (see the "
            "lines above), so the hosted control signer may not run. Enable "
            "platform_pylon_root_unit_enabled, converge, restart pm2-deploy.service, "
            "then redeploy"
        )


# --- Reviewed source --------------------------------------------------------


def git_environment(host: Host) -> dict[str, str]:
    ssh = shlex.join(
        [
            host.ssh,
            "-F",
            "/dev/null",
            "-i",
            str(host.github_key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            f"UserKnownHostsFile={host.known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "UpdateHostKeys=no",
            "-o",
            "BatchMode=yes",
        ]
    )
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(host.work_root),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": host.git_protocols,
        "GIT_SSH_COMMAND": ssh,
    }


def _checked(result: subprocess.CompletedProcess[bytes], failure: str) -> None:
    if result.returncode != 0:
        raise ReleaseError(failure)


def fetch_reviewed_archive(
    host: Host, revision: str, workspace: Path, run: Runner
) -> Path:
    """Write ``git archive`` of a revision reachable from main to a root file."""
    env = git_environment(host)
    history = workspace / "history.git"
    _checked(
        run(
            [
                host.git,
                "clone",
                "--quiet",
                "--bare",
                "--no-tags",
                "--single-branch",
                f"--branch={BRANCH}",
                "--filter=tree:0",
                "--",
                host.repository,
                str(history),
            ],
            env=env,
        ),
        f"could not fetch {BRANCH} history",
    )
    ancestry = run(
        [
            host.git,
            f"--git-dir={history}",
            "merge-base",
            "--is-ancestor",
            revision,
            f"refs/heads/{BRANCH}",
        ],
        env=env,
    )
    if ancestry.returncode != 0:
        raise ReleaseError(
            f"revision {revision} is not reachable from {BRANCH}; only merged "
            "revisions run as ditto-api"
        )
    tree = workspace / "tree.git"
    _checked(
        run([host.git, "init", "--quiet", "--bare", str(tree)], env=env),
        "could not create the source repository",
    )
    _checked(
        run(
            [
                host.git,
                f"--git-dir={tree}",
                "fetch",
                "--quiet",
                "--no-tags",
                "--depth=1",
                "--",
                host.repository,
                revision,
            ],
            env=env,
        ),
        f"could not fetch revision {revision}",
    )
    with open(workspace / "resolved", "xb") as resolved:
        _checked(
            run(
                [
                    host.git,
                    f"--git-dir={tree}",
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{revision}^{{commit}}",
                ],
                env=env,
                stdout=resolved,
            ),
            f"revision {revision} is not a commit",
        )
    if (workspace / "resolved").read_bytes().strip() != revision.encode():
        raise ReleaseError(f"revision {revision} did not resolve to itself")
    archive = workspace / "source.tar"
    with open(archive, "xb") as output:
        _checked(
            run(
                [
                    host.git,
                    f"--git-dir={tree}",
                    "archive",
                    "--format=tar",
                    revision,
                ],
                env=env,
                stdout=output,
            ),
            f"could not export revision {revision}",
        )
    return archive


def extract_source(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, mode="r:") as bundle:
        for member in bundle.getmembers():
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ReleaseError("source archive holds an unsupported entry")
        # The data filter refuses absolute or escaping names and links, device
        # files and set-id bits, and drops group/other write.
        bundle.extractall(destination, filter="data")


def copy_dashboard(source: Path, destination: Path, owner_uid: int) -> int:
    """Copy the deploy-built SPA as plain data. Returns the file count.

    Every path component is opened without following links, and every copied
    entry must belong to the deploy user. A link or a file deploy does not own
    (for example the seed, which belongs to ditto-api) fails the install
    instead of being copied into a world-readable release.
    """
    parts = source.parts
    if not source.is_absolute() or ".." in parts:
        raise ReleaseError("dashboard source must be an absolute path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, name in enumerate(parts[1:], start=1):
            try:
                child = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                if index == len(parts) - 1:
                    # No build output: the release serves the API only, which
                    # is what the factory does for a checkout without dist/.
                    return 0
                raise ReleaseError("dashboard source path is missing") from None
            except OSError:
                raise ReleaseError(
                    "dashboard source path is not a chain of real directories"
                ) from None
            os.close(fd)
            fd = child
        counter = {"files": 0, "bytes": 0}
        _copy_tree(fd, destination, owner_uid, counter, depth=0)
        return counter["files"]
    finally:
        os.close(fd)


def _copy_tree(
    fd: int, destination: Path, owner_uid: int, counter: dict[str, int], depth: int
) -> None:
    if depth > MAX_DASHBOARD_DEPTH:
        raise ReleaseError("dashboard is nested too deeply")
    if os.fstat(fd).st_uid != owner_uid:
        raise ReleaseError("dashboard directory is not owned by the deploy user")
    destination.mkdir(mode=0o755)
    for name in sorted(os.listdir(fd)):
        if not DASHBOARD_NAME.fullmatch(name):
            raise ReleaseError("dashboard holds an unexpected file name")
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            try:
                _copy_tree(child, destination / name, owner_uid, counter, depth + 1)
            finally:
                os.close(child)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ReleaseError("dashboard holds a link or special file")
        source = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd
        )
        with os.fdopen(source, "rb") as reader:
            opened = os.fstat(reader.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or opened.st_uid != owner_uid
                or opened.st_nlink != 1
            ):
                raise ReleaseError("dashboard file is not a deploy-owned single link")
            counter["files"] += 1
            counter["bytes"] += opened.st_size
            if (
                counter["files"] > MAX_DASHBOARD_FILES
                or counter["bytes"] > MAX_DASHBOARD_BYTES
            ):
                raise ReleaseError("dashboard exceeds its size bound")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            target = os.open(destination / name, flags | os.O_CLOEXEC, 0o644)
            with os.fdopen(target, "wb") as writer:
                remaining = opened.st_size
                while remaining > 0:
                    chunk = reader.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise ReleaseError("dashboard file changed while copying")
                    writer.write(chunk)
                    remaining -= len(chunk)
                if reader.read(1):
                    raise ReleaseError("dashboard file changed while copying")


# --- Build, seal and verify -------------------------------------------------


def sandbox_arguments(unit: str, user: str) -> list[str]:
    return [
        "--quiet",
        "--wait",
        "--pipe",
        "--collect",
        f"--unit={unit}",
        f"--uid={user}",
        f"--gid={user}",
        *(f"--property={item}" for item in SANDBOX),
    ]


def build_environment(
    host: Host, settings: Settings, release: Path, revision: str, run: Runner
) -> None:
    """Build .venv as ditto-api-build without resolving any build backend.

    Every build dependency (hatchling, hatch-vcs, editables and their
    requirements) comes from the reviewed release-build-requirements.txt,
    installed as wheels with every hash required. `uv sync --no-build-isolation`
    then builds the project, the shared protocol and the pinned Git dependency
    with exactly those backends and removes them again; runtime packages are
    verified against uv.lock.
    """
    platform = release / "apps" / "platform"
    venv = platform / ".venv"
    requirements = platform / "release-build-requirements.txt"
    require_trusted_file(requirements, host, modes={0o644})
    builder = account(host.builder_user)
    venv.mkdir(mode=0o755)
    os.chown(venv, builder.pw_uid, builder.pw_gid)
    cache = host.builder_home / "uv-cache"
    steps = (
        ("venv", ["venv", "--quiet", str(venv)]),
        (
            "build-backends",
            [
                "pip",
                "install",
                "--quiet",
                "--require-hashes",
                "--no-build",
                f"--python={venv}/bin/python",
                f"--requirements={requirements}",
            ],
        ),
        (
            "sync",
            [
                "sync",
                "--frozen",
                "--no-dev",
                "--no-build-isolation",
                "--no-progress",
                f"--project={platform}",
            ],
        ),
    )
    for step, arguments in steps:
        argv = [
            host.systemd_run,
            *sandbox_arguments(
                f"ditto-platform-api-build-{step}-{revision[:12]}", host.builder_user
            ),
            f"--property=ReadWritePaths={venv} {host.builder_home}",
            "--property=InaccessiblePaths=-/etc/ditto-platform -/opt/ditto-subnet "
            f"-{host.work_root} -/run/docker.sock",
            f"--working-directory={platform}",
            "--setenv=PATH=/usr/bin:/bin",
            f"--setenv=HOME={host.builder_home}",
            f"--setenv=UV_CACHE_DIR={cache}",
            f"--setenv=UV_PYTHON={settings.python}",
            "--setenv=UV_PYTHON_DOWNLOADS=never",
            "--setenv=UV_PYTHON_PREFERENCE=only-system",
            "--setenv=UV_NO_CONFIG=1",
            # Copies, never hard links into the builder-owned cache.
            "--setenv=UV_LINK_MODE=copy",
            # The sealed tree is read-only at run time, so compile bytecode now.
            "--setenv=UV_COMPILE_BYTECODE=1",
            f"--setenv=UV_PROJECT_ENVIRONMENT={venv}",
            "--",
            host.uv,
            *arguments,
        ]
        # Each transient unit's cgroup is stopped when uv exits, so no builder
        # process survives to write into the tree after it is sealed.
        _checked(
            run(argv, env={"PATH": "/usr/bin:/bin", "LANG": "C"}),
            f"the Python environment build failed at {step}",
        )


MAX_LINK_HOPS = 40


def _allowed_link(release: Path, path: Path, settings: Settings) -> bool:
    """Resolve the link hop by hop the way the kernel would.

    Every intermediate step must stay inside the release, so a chain of
    relative links cannot escape even when each target looks harmless on its
    own. The one exception is a .venv/bin/python* link whose resolution ends at
    the pinned root-owned interpreter.
    """
    relative = path.relative_to(release)
    python_link = relative.parts[:-1] == (
        "apps",
        "platform",
        ".venv",
        "bin",
    ) and relative.name.startswith("python")
    pending = list(relative.parts)
    current = release
    hops = 0
    while pending:
        part = pending.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            if current == release:
                return False
            current = current.parent
            continue
        candidate = current / part
        if not candidate.is_symlink():
            current = candidate
            continue
        hops += 1
        if hops > MAX_LINK_HOPS:
            return False
        target = os.readlink(candidate)
        if os.path.isabs(target):
            return python_link and target == str(settings.python) and not pending
        pending = [*Path(target).parts, *pending]
    return True


def seal(host: Host, settings: Settings, release: Path) -> None:
    """Root-own the tree, drop write for everyone else and refuse odd entries."""
    for directory, names, files in os.walk(release, followlinks=False):
        for name in (*names, *files):
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if not _allowed_link(release, path, settings):
                    raise ReleaseError(
                        f"release link {path.relative_to(release)} leaves the release"
                    )
                os.lchown(path, host.trusted_uid, host.trusted_gid)
            elif stat.S_ISDIR(info.st_mode):
                os.lchown(path, host.trusted_uid, host.trusted_gid)
                os.chmod(path, 0o755)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise ReleaseError(
                        f"release file {path.relative_to(release)} is hard-linked"
                    )
                os.lchown(path, host.trusted_uid, host.trusted_gid)
                os.chmod(path, 0o755 if info.st_mode & 0o111 else 0o644)
            else:
                raise ReleaseError(
                    f"release entry {path.relative_to(release)} is not a file"
                )
    os.lchown(release, host.trusted_uid, host.trusted_gid)
    os.chmod(release, 0o755)


def manifest_digest(release: Path) -> str:
    """SHA-256 over every entry's path, owner, mode and content or link target."""
    digest = hashlib.sha256()
    entries: list[str] = []
    for directory, names, files in os.walk(release, followlinks=False):
        for name in (*names, *files):
            path = Path(directory) / name
            relative = path.relative_to(release).as_posix()
            if relative == RECEIPT:
                continue
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                entries.append(f"l {relative} {info.st_uid} {os.readlink(path)}")
            elif stat.S_ISDIR(info.st_mode):
                entries.append(f"d {relative} {info.st_uid} {mode:o}")
            elif stat.S_ISREG(info.st_mode):
                with open(path, "rb") as reader:
                    content = hashlib.file_digest(reader, "sha256").hexdigest()
                entries.append(f"f {relative} {info.st_uid} {mode:o} {content}")
            else:
                entries.append(f"x {relative}")
    for entry in sorted(entries):
        digest.update(entry.encode("utf-8", "surrogateescape") + b"\n")
    return digest.hexdigest()


def write_receipt(host: Host, settings: Settings, release: Path, revision: str) -> None:
    body = json.dumps(
        {
            "schema": RECEIPT_SCHEMA,
            "revision": revision,
            "python": str(settings.python),
            "manifest_sha256": manifest_digest(release),
        },
        sort_keys=True,
    ).encode()
    path = release / RECEIPT
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o444)
    with os.fdopen(fd, "wb") as output:
        output.write(body + b"\n")
        output.flush()
        os.fsync(output.fileno())
    os.lchown(path, host.trusted_uid, host.trusted_gid)
    os.chmod(path, 0o444)


def verify_release(host: Host, settings: Settings, revision: str) -> Path:
    release = host.releases / revision
    require_trusted_directory(release, host)
    require_trusted_file(release / RECEIPT, host, modes={0o444})
    try:
        receipt: Any = json.loads((release / RECEIPT).read_bytes())
    except (OSError, ValueError):
        raise ReleaseError(f"release {revision} has an unreadable receipt") from None
    expected = {"schema", "revision", "python", "manifest_sha256"}
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected
        or receipt["schema"] != RECEIPT_SCHEMA
        or receipt["revision"] != revision
        or receipt["python"] != str(settings.python)
        or receipt["manifest_sha256"] != manifest_digest(release)
    ):
        raise ReleaseError(f"release {revision} does not match its sealed manifest")
    return release


def run_preflight(host: Host, release: Path, revision: str, run: Runner) -> None:
    argv = [
        host.systemd_run,
        *sandbox_arguments(
            f"ditto-platform-api-preflight-{revision[:12]}", host.service_user
        ),
        "--property=InaccessiblePaths=-/opt/ditto-subnet "
        f"-{host.work_root} -{host.builder_home} -/run/docker.sock",
        "--",
        str(host.launcher),
        "preflight",
        str(release),
    ]
    _checked(
        run(argv, env={"PATH": "/usr/bin:/bin", "LANG": "C"}),
        "the hosted signer preflight failed as ditto-api; ditto-api was not restarted",
    )


def stage_deploy_env(host: Host, request: Request) -> None:
    service = account(host.service_user)
    require_trusted_directory(host.config_dir, host)
    body = "".join(f"{key}={shlex.quote(value)}\n" for key, value in request.values)
    target = host.staged_deploy_env(request.revision)
    temporary = host.config_dir / f".deploy-{secrets.token_hex(8)}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        with os.fdopen(fd, "w", encoding="ascii") as output:
            output.write(body)
            output.flush()
            os.fchown(output.fileno(), host.trusted_uid, service.pw_gid)
            os.fchmod(output.fileno(), 0o640)
            os.fsync(output.fileno())
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


# --- Commands ---------------------------------------------------------------


@contextlib.contextmanager
def locked(host: Host) -> Iterator[None]:
    require_trusted_directory(host.work_root, host)
    fd = os.open(
        host.work_root / "lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def install(host: Host, request: Request, run: Runner) -> str:
    settings = load_settings(host)
    require_host(host, settings)
    if signer_enabled(host):
        require_deploy_without_docker(host, settings, run)
    owner = account(settings.platform_owner)
    release = host.releases / request.revision
    reused = False
    if release.exists() or release.is_symlink():
        try:
            verify_release(host, settings, request.revision)
            reused = True
        except ReleaseError:
            if _current_revision(host) == request.revision:
                # Never delete the tree the running process was loaded from.
                raise
            # A crashed attempt. Its parent is root-only, so this is our own
            # tree, and rmtree does not follow links inside it.
            shutil.rmtree(release)
    if not reused:
        workspace = host.work_root / f"fetch-{secrets.token_hex(8)}"
        workspace.mkdir(mode=0o700)
        try:
            archive = fetch_reviewed_archive(host, request.revision, workspace, run)
            release.mkdir(mode=0o700)
            try:
                extract_source(archive, release)
                archive.unlink()
                # Readable (never writable) by the builder, which must reach
                # the .venv it owns below the root-owned source tree.
                os.chmod(release, 0o755)
                dashboard = release / "apps" / "platform" / "dashboard"
                dashboard.mkdir(mode=0o755, parents=True, exist_ok=True)
                copy_dashboard(host.dashboard_source, dashboard / "dist", owner.pw_uid)
                build_environment(host, settings, release, request.revision, run)
                seal(host, settings, release)
                # The receipt hashes the tree once; activate verifies it again
                # at the moment it switches, so no second pass here.
                write_receipt(host, settings, release, request.revision)
            except BaseException:
                shutil.rmtree(release, ignore_errors=True)
                raise
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
    run_preflight(host, release, request.revision, run)
    stage_deploy_env(host, request)
    return "reused" if reused else "installed"


def _current_revision(host: Host) -> str | None:
    try:
        target = os.readlink(host.current)
    except OSError:
        return None
    name = Path(target).name
    return name if target == f"releases/{name}" and REVISION.fullmatch(name) else None


def activate(host: Host, request: Request, run: Runner) -> None:
    settings = load_settings(host)
    require_host(host, settings)
    if signer_enabled(host):
        require_deploy_without_docker(host, settings, run)
    verify_release(host, settings, request.revision)
    staged = host.staged_deploy_env(request.revision)
    if staged.exists():
        require_trusted_file(staged, host, modes={0o640})
        os.replace(staged, host.deploy_env)
    require_trusted_file(host.deploy_env, host, modes={0o640})
    previous = _current_revision(host)
    link = host.api_root / f".current-{secrets.token_hex(8)}"
    os.symlink(f"releases/{request.revision}", link)
    os.replace(link, host.current)
    _checked(
        run(
            [host.systemctl, "enable", "--quiet", host.unit],
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ),
        f"could not enable {host.unit}",
    )
    # A unit that hit its start limit refuses to start again for the limit
    # interval, which would block a rollback deploy; clear that first.
    _checked(
        run(
            [host.systemctl, "reset-failed", host.unit],
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ),
        f"could not reset {host.unit}",
    )
    _checked(
        run(
            [host.systemctl, "restart", host.unit],
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ),
        f"{host.unit} did not start; read `sudo -n ditto-platform-api-release logs`",
    )
    prune(host, keep={request.revision, previous})


def prune(host: Host, keep: set[str | None]) -> None:
    """Keep the running and previous release; remove older sealed trees."""
    for entry in sorted(host.releases.iterdir()):
        if REVISION.fullmatch(entry.name) and entry.name not in keep:
            shutil.rmtree(entry)
    for entry in sorted(host.config_dir.glob("deploy-*.env")):
        revision = entry.name.removeprefix("deploy-").removesuffix(".env")
        if REVISION.fullmatch(revision) and revision not in keep:
            entry.unlink()


def stop(host: Host, run: Runner) -> None:
    _checked(
        run(
            [host.systemctl, "disable", "--now", "--quiet", host.unit],
            env={"PATH": "/usr/bin:/bin", "LANG": "C"},
        ),
        f"could not stop {host.unit}",
    )


def logs(host: Host, run: Runner) -> None:
    _checked(
        run(
            [
                host.journalctl,
                "--no-pager",
                "--quiet",
                "--output=short-iso",
                "--lines=80",
                f"--unit={host.unit}",
            ],
            env={
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "PAGER": "cat",
                "SYSTEMD_PAGER": "cat",
                "SYSTEMD_PAGERSECURE": "1",
                "SYSTEMD_COLORS": "0",
            },
        ),
        f"could not read {host.unit} logs",
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: IO[bytes] | None = None,
    host: Host | None = None,
    run: Runner = run_command,
) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    host = host or Host()
    if len(arguments) != 1 or arguments[0] not in COMMANDS:
        print(
            f"usage: ditto-platform-api-release {'|'.join(COMMANDS)}", file=sys.stderr
        )
        return 64
    command = arguments[0]
    if os.geteuid() != host.trusted_uid:
        print("ditto-platform-api-release must run as root", file=sys.stderr)
        return 77
    os.umask(0o022)
    os.chdir(host.anchor)
    reader = stdin if stdin is not None else sys.stdin.buffer
    try:
        if command == "install":
            request = parse_request(reader.read(MAX_REQUEST + 1), with_values=True)
            with locked(host):
                outcome = install(host, request, run)
            print(f"ditto-api release {request.revision} {outcome} and sealed")
        elif command == "activate":
            request = parse_request(reader.read(MAX_REQUEST + 1), with_values=False)
            with locked(host):
                activate(host, request, run)
            print(f"ditto-api is running release {request.revision}")
        elif command == "stop":
            stop(host, run)
            print(f"{host.unit} stopped and disabled")
        else:
            logs(host, run)
    except ReleaseError as error:
        print(f"ditto-platform-api-release {command} failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
