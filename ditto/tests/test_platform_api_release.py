"""Root release installer for the dedicated ditto-api identity.

Synthetic trees only: no root, no systemd, no network, no production key. A real
local Git repository stands in for GitHub; systemd-run, systemctl and journalctl
are recording fakes. The "seed" below is a synthetic placeholder file used only
to prove the installer never opens it.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import os
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/platform_app"
INSTALLER = ROLE / "files/platform-api-release.py"
spec = importlib.util.spec_from_file_location("platform_api_release", INSTALLER)
assert spec is not None and spec.loader is not None
RELEASE = importlib.util.module_from_spec(spec)
sys.modules["platform_api_release"] = RELEASE
spec.loader.exec_module(RELEASE)

PAYMENT = "5G6fGXnXFYdLM3ZyAm9whUbCY4ziQzcbMiTEqZB5c9KekTtR"
NAMES_URL = "https://api.taostats.io/api/dtao/validator/available/v1?netuid=118"
UID = os.getuid()
GID = os.getgid()


def _executable(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\nset -eu\n{body}")
    path.chmod(0o755)
    return path


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    ).stdout.strip()


@dataclasses.dataclass
class Tree:
    root: Path
    host: Any
    python: Path
    source: Path
    mains: list[str]
    side: str
    seed: Path
    control: Path

    def log(self, name: str) -> list[list[str]]:
        path = self.control / f"{name}.log"
        if not path.exists():
            return []
        return [shlex.split(line) for line in path.read_text().splitlines()]

    def request(self, revision: str, *lines: str) -> io.BytesIO:
        return io.BytesIO("".join([f"revision={revision}\n", *lines]).encode())


def _source_repository(path: Path) -> tuple[list[str], str]:
    path.mkdir()
    _git(path, "init", "--quiet", "--initial-branch=main")
    _git(path, "config", "user.email", "fixture@example.invalid")
    _git(path, "config", "user.name", "fixture")
    files = {
        "apps/platform/pyproject.toml": "[project]\nname = 'synthetic'\n",
        "apps/platform/release-build-requirements.txt": "hatchling==1\n",
        "apps/platform/ditto/__init__.py": "",
        "apps/platform/scripts/run.sh": "#!/bin/sh\n",
        "apps/platform/dashboard/package.json": "{}\n",
        "packages/ditto-screening-protocol/pyproject.toml": "[project]\n",
        ".agents/skills/example/SKILL.md": "synthetic\n",
    }
    for name, body in files.items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(body)
    (path / "apps/platform/scripts/run.sh").chmod(0o755)
    link = path / "apps/platform/.agents/skills/example"
    link.parent.mkdir(parents=True)
    link.symlink_to("../../../../.agents/skills/example")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "one")
    mains = [_git(path, "rev-parse", "HEAD")]
    _git(path, "checkout", "--quiet", "-b", "side")
    (path / "unreviewed.py").write_text("print('not on main')\n")
    _git(path, "add", "-A")
    _git(path, "commit", "--quiet", "-m", "side")
    side = _git(path, "rev-parse", "HEAD")
    _git(path, "checkout", "--quiet", "main")
    for number in (2, 3):
        (path / f"apps/platform/ditto/change_{number}.py").write_text(f"N = {number}\n")
        _git(path, "add", "-A")
        _git(path, "commit", "--quiet", "-m", f"main {number}")
        mains.append(_git(path, "rev-parse", "HEAD"))
    _git(path, "config", "uploadpack.allowFilter", "true")
    _git(path, "config", "uploadpack.allowAnySHA1InWant", "true")
    return mains, side


def _account(name: str) -> pwd.struct_passwd:
    return pwd.struct_passwd((name, "x", UID, GID, "", "/nonexistent", "/bin/false"))


def build_tree(tmp_path: Path) -> Tree:
    root = tmp_path / "host"
    control = tmp_path / "control"
    control.mkdir()
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    mains, side = _source_repository(tmp_path / "source")

    def directory(relative: str, mode: int = 0o755) -> Path:
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        for part in (path, *path.parents):
            if part == root:
                break
            part.chmod(0o755)
        path.chmod(mode)
        return path

    api_root = directory("opt/ditto-platform-api")
    directory("opt/ditto-platform-api/releases")
    config_dir = directory("etc/ditto-platform/api", 0o750)
    work_root = directory("var/lib/ditto-platform-api-release", 0o700)
    builder_home = directory("var/lib/ditto-platform-api-build", 0o700)
    bin_dir = directory("usr/bin")

    # Stands in for the pinned interpreter: a real Python for the probe, and a
    # fixed link target for the synthetic environment.
    python = _executable(bin_dir / "python3.13", 'exec /usr/bin/python3 "$@"\n')
    git = _executable(
        bin_dir / "git",
        f'printf "%s\\n" "$(printf "%q " "$@")" >> "{control}/git.log"\n'
        'exec /usr/bin/git "$@"\n',
    )
    ssh = _executable(bin_dir / "ssh", "exit 99\n")
    uv = _executable(bin_dir / "uv", "exit 99\n")
    systemd_run = _executable(
        bin_dir / "systemd-run",
        f'printf "%s\\n" "$(printf "%q " "$@")" >> "{control}/systemd-run.log"\n'
        'args=("$@")\n'
        'for i in "${!args[@]}"; do\n'
        '  if [ "${args[$i]}" = -- ]; then\n'
        '    command=("${args[@]:$((i + 1))}")\n'
        "    break\n"
        "  fi\n"
        "done\n"
        'case "${command[0]}" in\n'
        "  */uv)\n"
        f'    if [ -e "{control}/uv-${{command[1]}}-exit" ]; then\n'
        f'      exit "$(cat "{control}/uv-${{command[1]}}-exit")"\n'
        "    fi\n"
        '    for arg in "${args[@]}"; do\n'
        '      case "$arg" in\n'
        '        --setenv=UV_PROJECT_ENVIRONMENT=*) venv="${arg#*=*=}" ;;\n'
        '        --setenv=UV_PYTHON=*) interpreter="${arg#*=*=}" ;;\n'
        "      esac\n"
        "    done\n"
        '    case "${command[1]}" in\n'
        "      venv)\n"
        '        mkdir -p "$venv/bin" "$venv/lib/python3.13/site-packages"\n'
        '        ln -s "$interpreter" "$venv/bin/python"\n'
        '        ln -s python "$venv/bin/python3"\n'
        '        ln -s lib "$venv/lib64"\n'
        "        ;;\n"
        "      sync)\n"
        '        pth="$venv/lib/python3.13/site-packages/_synthetic.pth"\n'
        '        printf "%s\\n" "${venv%/.venv}" > "$pth"\n'
        '        chmod 0666 "$pth"\n'
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *)\n"
        f'    if [ -e "{control}/preflight-exit" ]; then\n'
        f'      exit "$(cat "{control}/preflight-exit")"\n'
        "    fi\n"
        '    "${command[@]}"\n'
        "    ;;\n"
        "esac\n",
    )
    systemctl = _executable(
        bin_dir / "systemctl",
        f'printf "%s\\n" "$(printf "%q " "$@")" >> "{control}/systemctl.log"\n'
        f'if [ "$1" = restart ] && [ -e "{control}/restart-exit" ]; then\n'
        f'  exit "$(cat "{control}/restart-exit")"\n'
        "fi\n",
    )
    journalctl = _executable(
        bin_dir / "journalctl",
        f'printf "%s\\n" "$(printf "%q " "$@")" >> "{control}/journalctl.log"\n'
        'echo "synthetic journal line"\n',
    )
    launcher = _executable(
        root / "usr/local/libexec/ditto-platform-api/launch",
        'echo "synthetic preflight $1 $2"\n',
    )
    # Records its arguments; exits with control/docker-exit (default 0).
    docker_probe = root / "usr/local/libexec/ditto-platform-api/deploy-docker-access"
    docker_probe.write_text(
        "import pathlib, sys\n"
        f"control = pathlib.Path({str(control)!r})\n"
        "with open(control / 'docker-probe.log', 'a') as log:\n"
        "    log.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "code = control / 'docker-exit'\n"
        "if code.exists():\n"
        "    print('synthetic: deploy still holds the docker group')\n"
        "    sys.exit(int(code.read_text()))\n"
    )
    docker_probe.chmod(0o755)
    directory("usr/local/libexec/ditto-platform-api")

    platform_env = config_dir / "platform.env"
    platform_env.write_text("DITTO_CODING_HOSTED_CONTROL_ENABLED=false\n")
    platform_env.chmod(0o640)
    settings = config_dir / "release.json"
    settings.write_text(json.dumps({"python": str(python), "platform_owner": "deploy"}))
    settings.chmod(0o644)
    key = config_dir / "github-deploy-key"
    key.write_text("synthetic, never used by file:// fetches\n")
    key.chmod(0o600)
    known_hosts = config_dir / "github-known-hosts"
    known_hosts.write_text("")
    known_hosts.chmod(0o644)

    dist = root / "opt/ditto-subnet/apps/platform/dashboard/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>\n")
    (dist / "assets/app-abcdef12.js").write_text("console.log(1)\n")

    seed_dir = directory("etc/ditto-platform/coding-hosted-signer", 0o700)
    seed = seed_dir / "seed"
    seed.write_bytes(b"synthetic-placeholder-not-a-key!")
    seed.chmod(0o600)

    host = RELEASE.Host(
        api_root=api_root,
        config_dir=config_dir,
        work_root=work_root,
        builder_home=builder_home,
        dashboard_source=dist,
        launcher=launcher,
        docker_probe=docker_probe,
        proc_root=tmp_path / "proc",
        docker_socket=tmp_path / "docker.sock",
        repository=f"file://{tmp_path / 'source'}",
        git_protocols="file",
        trusted_uid=UID,
        trusted_gid=GID,
        anchor=root,
        git=str(git),
        ssh=str(ssh),
        uv=str(uv),
        systemd_run=str(systemd_run),
        systemctl=str(systemctl),
        journalctl=str(journalctl),
    )
    return Tree(root, host, python, tmp_path / "source", mains, side, seed, control)


def patch_accounts(monkeypatch: pytest.MonkeyPatch, python: Path) -> None:
    # Every account is the test user here, so only the production distinctness
    # check is replaced; test_distinct_accounts_are_required covers it.
    monkeypatch.setattr(RELEASE, "PYTHON", re.compile(re.escape(str(python))))
    monkeypatch.setattr(RELEASE, "account", _account)
    monkeypatch.setattr(RELEASE, "require_distinct_accounts", lambda *_: None)


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Tree]:
    if UID == 0:
        pytest.skip("synthetic trees model a non-root trusted owner")
    monkeypatch.chdir(tmp_path)
    # main() sets the installer's umask; give the test process its own back.
    umask = os.umask(0o022)
    os.umask(umask)
    built = build_tree(tmp_path)
    patch_accounts(monkeypatch, built.python)
    yield built
    os.umask(umask)


def _install(tree: Tree, revision: str, *lines: str) -> int:
    return RELEASE.main(
        ["install"], stdin=tree.request(revision, *lines), host=tree.host
    )


# --- Production literals ----------------------------------------------------


def test_production_host_is_fixed_and_matches_the_role() -> None:
    host = RELEASE.Host()
    assert host.api_root == Path("/opt/ditto-platform-api")
    assert host.current == Path("/opt/ditto-platform-api/current")
    assert host.config_dir == Path("/etc/ditto-platform/api")
    assert host.work_root == Path("/var/lib/ditto-platform-api-release")
    assert host.builder_home == Path("/var/lib/ditto-platform-api-build")
    assert host.dashboard_source == Path(
        "/opt/ditto-subnet/apps/platform/dashboard/dist"
    )
    assert host.launcher == Path("/usr/local/libexec/ditto-platform-api/launch")
    assert host.docker_probe == Path(
        "/usr/local/libexec/ditto-platform-api/deploy-docker-access"
    )
    assert (host.proc_root, host.docker_socket) == (
        Path("/proc"),
        Path("/run/docker.sock"),
    )
    assert host.repository == "git@github.com:ditto-assistant/ditto-subnet.git"
    assert host.git_protocols == "ssh"
    assert (host.unit, host.service_user, host.builder_user) == (
        "ditto-platform-api.service",
        "ditto-api",
        "ditto-api-build",
    )
    assert (host.trusted_uid, host.trusted_gid, host.anchor) == (0, 0, Path("/"))
    assert RELEASE.BRANCH == "main"
    assert RELEASE.COMMANDS == ("install", "activate", "stop", "logs")
    assert RELEASE.PYTHON.fullmatch("/usr/bin/python3.13")
    for unsafe in ("/usr/local/bin/python3.13", "/home/deploy/python3", "python3"):
        assert not RELEASE.PYTHON.fullmatch(unsafe)

    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["platform_repo_url"] == host.repository
    assert defaults["platform_api_release_python"] == "/usr/bin/python3.13"

    # The installer forwards exactly the keys update.sh owns in .env.deploy.
    updater = (ROOT / "apps/platform/scripts/update.sh").read_text()
    block = updater[updater.index("deploy_owned_keys=(") :]
    keys = block[len("deploy_owned_keys=(") : block.index(")")].split()
    assert frozenset(keys) == RELEASE.DEPLOY_KEYS


def test_environment_for_git_pins_transport_and_ignores_user_config() -> None:
    host = RELEASE.Host()
    env = RELEASE.git_environment(host)
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_ALLOW_PROTOCOL"] == "ssh"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["HOME"] == "/var/lib/ditto-platform-api-release"
    assert shlex.split(env["GIT_SSH_COMMAND"]) == [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-i",
        "/etc/ditto-platform/api/github-deploy-key",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "UserKnownHostsFile=/etc/ditto-platform/api/github-known-hosts",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "BatchMode=yes",
    ]
    assert "/home/" not in json.dumps(env)


# --- Requests ---------------------------------------------------------------


def test_request_accepts_the_revision_and_allowed_deploy_values() -> None:
    revision = "a" * 40
    request = RELEASE.parse_request(
        (
            f"revision={revision}\n"
            f"DITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n"
            f"DITTO_TAOSTATS_VALIDATOR_NAMES_URL={NAMES_URL}\n"
        ).encode(),
        with_values=True,
    )
    assert request.revision == revision
    assert dict(request.values) == {
        "DITTO_UPLOAD_PAYMENT_ADDRESS": PAYMENT,
        "DITTO_TAOSTATS_VALIDATOR_NAMES_URL": NAMES_URL,
    }
    assert RELEASE.parse_request(f"revision={revision}".encode(), with_values=False)


SECRET = "tao-secret-value"


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"revision=" + b"A" * 40 + b"\n",
        b"revision=" + b"a" * 39 + b"\n",
        b"DITTO_UPLOAD_PAYMENT_ADDRESS=x\nrevision=" + b"a" * 40 + b"\n",
        b"revision=" + b"a" * 40 + b"\nPYTHONPATH=/tmp/evil\n",
        b"revision=" + b"a" * 40 + b"\nLD_PRELOAD=/tmp/evil.so\n",
        b"revision="
        + b"a" * 40
        + b"\nDITTO_TAOSTATS_API_KEY="
        + SECRET.encode()
        + b"$x\n",
        b"revision="
        + b"a" * 40
        + b"\nDITTO_TAOSTATS_API_KEY="
        + SECRET.encode()
        + b" x\n",
        b"revision="
        + b"a" * 40
        + b"\nDITTO_TAOSTATS_API_KEY='"
        + SECRET.encode()
        + b"'\n",
        b"revision="
        + b"a" * 40
        + b"\nDITTO_TAOSTATS_API_KEY="
        + SECRET.encode()
        + b"`id`\n",
        b"revision="
        + b"a" * 40
        + b"\nDITTO_TAOSTATS_API_KEY="
        + SECRET.encode()
        + b"\\\n",
        b"revision="
        + b"a" * 40
        + b"\nDITTO_TAOSTATS_API_KEY="
        + SECRET.encode()
        + b"\x00\n",
        b"revision=" + b"a" * 40 + b"\n" + SECRET.encode() + b"\n",
        b"revision=" + b"a" * 40 + b"\nDITTO_TAOSTATS_API_KEY=\n",
        b"revision="
        + b"a" * 40
        + b"\nDITTO_TAOSTATS_API_KEY=a\nDITTO_TAOSTATS_API_KEY=b\n",
        b"revision=" + b"a" * 40 + b"\nDITTO_TAOSTATS_API_KEY=\xc3\xa9\n",
        b"revision=" + b"a" * 40 + b"\n" + b"DITTO_TAOSTATS_API_KEY=" + b"a" * 20000,
    ],
)
def test_request_refuses_anything_else_without_echoing_values(body: bytes) -> None:
    with pytest.raises(RELEASE.ReleaseError) as error:
        RELEASE.parse_request(body, with_values=True)
    assert SECRET not in str(error.value)
    assert "/tmp/evil" not in str(error.value)


def test_url_characters_update_sh_accepts_stay_literal() -> None:
    """`&`, `|`, `;`, `~` and friends are single-quoted in deploy.env."""
    url = "wss://archive.example/v1?key=a&mode=b|c;d~e(f)<g>{h}[i]^j!"
    request = RELEASE.parse_request(
        f"revision={'a' * 40}\nSUBTENSOR_ARCHIVE_RPC_URL={url}\n".encode(),
        with_values=True,
    )
    assert dict(request.values) == {"SUBTENSOR_ARCHIVE_RPC_URL": url}
    assert shlex.split(f"X={shlex.quote(url)}") == [f"X={url}"]


def test_refusals_name_a_known_key_but_never_a_value() -> None:
    with pytest.raises(RELEASE.ReleaseError) as bad_value:
        RELEASE.parse_request(
            f"revision={'a' * 40}\nDITTO_TAOSTATS_API_KEY={SECRET}$HOME\n".encode(),
            with_values=True,
        )
    assert str(bad_value.value).startswith("DITTO_TAOSTATS_API_KEY ")
    assert SECRET not in str(bad_value.value)
    with pytest.raises(RELEASE.ReleaseError) as unknown:
        RELEASE.parse_request(
            f"revision={'a' * 40}\n{SECRET}=1\n".encode(), with_values=True
        )
    assert str(unknown.value) == "request line 2 is not an allowed deploy key"


def test_activate_requests_carry_no_values() -> None:
    with pytest.raises(RELEASE.ReleaseError):
        RELEASE.parse_request(
            f"revision={'a' * 40}\nDITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n".encode(),
            with_values=False,
        )


@pytest.mark.parametrize(
    "argv", [[], ["install", "extra"], ["shell"], ["--help"], ["install", "a" * 40]]
)
def test_only_the_four_fixed_argument_vectors_run(argv, capsys) -> None:
    assert RELEASE.main(argv, stdin=io.BytesIO(b""), host=RELEASE.Host()) == 64
    assert "usage" in capsys.readouterr().err


def test_refuses_to_run_without_root(capsys) -> None:
    if UID == 0:
        pytest.skip("the non-root refusal needs a non-root test user")
    assert RELEASE.main(["stop"], stdin=io.BytesIO(b""), host=RELEASE.Host()) == 77
    assert "must run as root" in capsys.readouterr().err


def test_distinct_accounts_are_required(monkeypatch) -> None:
    uids = {"ditto-api": 900, "ditto-api-build": 901, "deploy": 1000}
    monkeypatch.setattr(
        RELEASE,
        "account",
        lambda name: pwd.struct_passwd((name, "x", uids[name], 1, "", "/", "")),
    )
    settings = RELEASE.Settings(Path("/usr/bin/python3.13"), "deploy")
    RELEASE.require_distinct_accounts(RELEASE.Host(), settings)
    for name in uids:
        for other in (*uids, "root"):
            if other == name:
                continue
            clash = dict(uids)
            clash[name] = 0 if other == "root" else uids[other]
            monkeypatch.setattr(
                RELEASE,
                "account",
                lambda account, clash=clash: pwd.struct_passwd(
                    (account, "x", clash[account], 1, "", "/", "")
                ),
            )
            with pytest.raises(RELEASE.ReleaseError):
                RELEASE.require_distinct_accounts(RELEASE.Host(), settings)


# --- End to end on a synthetic host ----------------------------------------


def test_install_seals_a_main_revision_and_activate_switches_current(
    tree: Tree, capsys
) -> None:
    revision = tree.mains[-1]
    assert (
        _install(
            tree,
            revision,
            f"DITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n",
            f"DITTO_TAOSTATS_VALIDATOR_NAMES_URL={NAMES_URL}\n",
        )
        == 0
    ), capsys.readouterr().err
    assert f"release {revision} installed and sealed" in capsys.readouterr().out

    release = tree.host.releases / revision
    # Exactly the reviewed tree: no .git, no side-branch file, plus the copied
    # dashboard data and the builder's environment.
    assert not (release / ".git").exists()
    assert not (release / "unreviewed.py").exists()
    assert (release / "apps/platform/ditto/change_3.py").read_text() == "N = 3\n"
    assert (release / "apps/platform/dashboard/dist/assets/app-abcdef12.js").exists()
    assert os.readlink(release / "apps/platform/.venv/bin/python") == str(tree.python)
    assert os.readlink(release / "apps/platform/.agents/skills/example") == (
        "../../../../.agents/skills/example"
    )
    for directory, names, files in os.walk(release):
        for name in (*names, *files):
            info = (Path(directory) / name).lstat()
            assert info.st_uid == UID
            if not stat.S_ISLNK(info.st_mode):
                assert not info.st_mode & 0o7022, (directory, name)
    assert stat.S_IMODE((release / "apps/platform/scripts/run.sh").stat().st_mode) == (
        0o755
    )
    pth = release / "apps/platform/.venv/lib/python3.13/site-packages/_synthetic.pth"
    assert stat.S_IMODE(pth.stat().st_mode) == 0o644

    # The environment was built as ditto-api-build in three sandboxed steps:
    # the venv, the hash-pinned build backends as wheels only, then the locked
    # sync that builds local packages without isolation. Then the metadata
    # preflight ran as ditto-api from the sealed tree.
    venv, backends, sync, preflight = tree.log("systemd-run")
    platform = f"{release}/apps/platform"
    assert venv[venv.index("--") + 1 :] == [
        tree.host.uv,
        "venv",
        "--quiet",
        f"{platform}/.venv",
    ]
    assert backends[backends.index("--") + 1 :] == [
        tree.host.uv,
        "pip",
        "install",
        "--quiet",
        "--require-hashes",
        "--no-build",
        f"--python={platform}/.venv/bin/python",
        f"--requirements={platform}/release-build-requirements.txt",
    ]
    assert sync[sync.index("--") + 1 :] == [
        tree.host.uv,
        "sync",
        "--frozen",
        "--no-dev",
        "--no-build-isolation",
        "--no-progress",
        f"--project={platform}",
    ]
    for build in (venv, backends, sync):
        assert "--uid=ditto-api-build" in build and "--gid=ditto-api-build" in build
        for expected in (
            "--wait",
            "--pipe",
            "--collect",
            "--property=NoNewPrivileges=yes",
            "--property=ProtectSystem=strict",
            "--property=CapabilityBoundingSet=",
            f"--property=ReadWritePaths={release}/apps/platform/.venv "
            f"{tree.host.builder_home}",
            "--setenv=UV_PYTHON_DOWNLOADS=never",
            "--setenv=UV_LINK_MODE=copy",
            "--setenv=UV_COMPILE_BYTECODE=1",
            f"--setenv=UV_PROJECT_ENVIRONMENT={release}/apps/platform/.venv",
            f"--setenv=UV_PYTHON={tree.python}",
            "--setenv=UV_PYTHON_PREFERENCE=only-system",
            "--setenv=UV_NO_CONFIG=1",
            f"--working-directory={release}/apps/platform",
        ):
            assert expected in build, expected
    assert "--uid=ditto-api" in preflight
    assert preflight[preflight.index("--") + 1 :] == [
        str(tree.host.launcher),
        "preflight",
        str(release),
    ]

    staged = tree.host.staged_deploy_env(revision)
    assert stat.S_IMODE(staged.stat().st_mode) == 0o640
    assert staged.read_text() == (
        f"DITTO_TAOSTATS_VALIDATOR_NAMES_URL={shlex.quote(NAMES_URL)}\n"
        f"DITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n"
    )
    assert not tree.host.current.exists()
    assert tree.log("systemctl") == []

    staged_body = staged.read_text()
    assert RELEASE.main(["activate"], stdin=tree.request(revision), host=tree.host) == 0
    assert os.readlink(tree.host.current) == f"releases/{revision}"
    assert tree.host.deploy_env.read_text() == staged_body
    assert stat.S_IMODE(tree.host.deploy_env.stat().st_mode) == 0o640
    assert not staged.exists()
    assert tree.log("systemctl") == [
        ["enable", "--quiet", "ditto-platform-api.service"],
        ["reset-failed", "ditto-platform-api.service"],
        ["restart", "ditto-platform-api.service"],
    ]
    assert list(tree.host.work_root.iterdir()) == [tree.host.work_root / "lock"]
    # The signer is disabled in platform.env, so no Docker probe ran.
    assert tree.log("docker-probe") == []


def test_an_unmerged_revision_never_becomes_a_release(tree: Tree, capsys) -> None:
    assert _install(tree, tree.side) == 1
    error = capsys.readouterr().err
    assert f"revision {tree.side} is not reachable from main" in error
    assert not (tree.host.releases / tree.side).exists()
    assert tree.log("systemd-run") == []
    assert list(tree.host.work_root.iterdir()) == [tree.host.work_root / "lock"]


def test_unknown_revision_is_refused(tree: Tree, capsys) -> None:
    assert _install(tree, "0" * 40) == 1
    assert "not reachable from main" in capsys.readouterr().err


def test_a_sealed_release_is_reused_without_refetching(tree: Tree, capsys) -> None:
    revision = tree.mains[0]
    assert _install(tree, revision) == 0
    clones = len(tree.log("git"))
    assert _install(tree, revision) == 0
    assert "reused and sealed" in capsys.readouterr().out
    assert len(tree.log("git")) == clones
    # The preflight still runs for every deploy.
    assert len(tree.log("systemd-run")) == 5


@pytest.mark.parametrize("step", ["venv", "pip", "sync"])
def test_a_failed_build_leaves_no_release(tree: Tree, capsys, step: str) -> None:
    (tree.control / f"uv-{step}-exit").write_text("3")
    assert _install(tree, tree.mains[0]) == 1
    assert "Python environment build failed at" in capsys.readouterr().err
    assert list(tree.host.releases.iterdir()) == []
    assert not tree.host.staged_deploy_env(tree.mains[0]).exists()


def test_a_failed_preflight_stages_nothing_and_touches_no_unit(
    tree: Tree, capsys
) -> None:
    (tree.control / "preflight-exit").write_text("1")
    revision = tree.mains[0]
    assert _install(tree, revision, f"DITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n") == 1
    assert "hosted signer preflight failed as ditto-api" in capsys.readouterr().err
    assert not tree.host.staged_deploy_env(revision).exists()
    assert tree.log("systemctl") == []


def test_tampering_after_sealing_blocks_activation(tree: Tree, capsys) -> None:
    revision = tree.mains[0]
    assert _install(tree, revision) == 0
    target = tree.host.releases / revision / "apps/platform/ditto/__init__.py"
    target.write_text("import os  # tampered\n")
    assert RELEASE.main(["activate"], stdin=tree.request(revision), host=tree.host) == 1
    assert "does not match its sealed manifest" in capsys.readouterr().err
    assert not tree.host.current.exists()
    assert tree.log("systemctl") == []


@pytest.mark.parametrize("change", ["mode", "link", "extra"])
def test_manifest_covers_modes_links_and_new_files(tree: Tree, change: str) -> None:
    revision = tree.mains[0]
    assert _install(tree, revision) == 0
    release = tree.host.releases / revision
    settings = RELEASE.load_settings(tree.host)
    RELEASE.verify_release(tree.host, settings, revision)
    if change == "mode":
        (release / "apps/platform/ditto/__init__.py").chmod(0o755)
    elif change == "link":
        link = release / "apps/platform/.venv/bin/python3"
        link.unlink()
        link.symlink_to("python3.13")
    else:
        (release / "apps/platform/ditto/sitecustomize.py").write_text("")
    with pytest.raises(RELEASE.ReleaseError):
        RELEASE.verify_release(tree.host, settings, revision)


def test_running_release_is_never_deleted_on_a_bad_manifest(tree: Tree, capsys) -> None:
    revision = tree.mains[0]
    assert _install(tree, revision) == 0
    assert RELEASE.main(["activate"], stdin=tree.request(revision), host=tree.host) == 0
    (tree.host.releases / revision / "apps/platform/ditto/__init__.py").write_text("x")
    assert _install(tree, revision) == 1
    assert "does not match its sealed manifest" in capsys.readouterr().err
    assert (tree.host.releases / revision / "apps/platform/ditto").is_dir()


def test_activation_keeps_the_previous_release_and_prunes_older(tree: Tree) -> None:
    for revision in tree.mains:
        assert (
            _install(tree, revision, f"DITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n") == 0
        )
        assert (
            RELEASE.main(["activate"], stdin=tree.request(revision), host=tree.host)
            == 0
        )
    assert sorted(path.name for path in tree.host.releases.iterdir()) == sorted(
        tree.mains[1:]
    )
    assert sorted(path.name for path in tree.host.config_dir.glob("deploy*.env")) == [
        "deploy.env"
    ]


def test_restart_failure_reports_how_to_read_logs(tree: Tree, capsys) -> None:
    revision = tree.mains[0]
    assert _install(tree, revision, f"DITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n") == 0
    (tree.control / "restart-exit").write_text("1")
    assert RELEASE.main(["activate"], stdin=tree.request(revision), host=tree.host) == 1
    assert "ditto-platform-api-release logs" in capsys.readouterr().err


def test_activation_requires_a_deploy_environment(tree: Tree, capsys) -> None:
    revision = tree.mains[0]
    assert _install(tree, revision) == 0
    tree.host.staged_deploy_env(revision).unlink()
    assert RELEASE.main(["activate"], stdin=tree.request(revision), host=tree.host) == 1
    assert "deploy.env is missing" in capsys.readouterr().err


def test_stop_and_logs_run_fixed_commands(tree: Tree) -> None:
    assert RELEASE.main(["stop"], stdin=io.BytesIO(b""), host=tree.host) == 0
    assert RELEASE.main(["logs"], stdin=io.BytesIO(b""), host=tree.host) == 0
    assert tree.log("systemctl") == [
        ["disable", "--now", "--quiet", "ditto-platform-api.service"]
    ]
    assert tree.log("journalctl") == [
        [
            "--no-pager",
            "--quiet",
            "--output=short-iso",
            "--lines=80",
            "--unit=ditto-platform-api.service",
        ]
    ]


@pytest.mark.parametrize(
    "fault",
    ["group-writable-root", "foreign-settings", "linked-key", "writable-launcher"],
)
def test_untrusted_host_inputs_stop_the_install(tree: Tree, fault: str, capsys) -> None:
    if fault == "group-writable-root":
        tree.host.api_root.chmod(0o775)
    elif fault == "foreign-settings":
        settings = json.loads(tree.host.settings_file.read_text())
        settings["platform_owner"] = "ditto-api"
        tree.host.settings_file.write_text(json.dumps(settings))
    elif fault == "linked-key":
        real = tree.host.config_dir / "real-key"
        tree.host.github_key.rename(real)
        tree.host.github_key.symlink_to(real.name)
    else:
        tree.host.launcher.chmod(0o775)
    assert _install(tree, tree.mains[0]) == 1
    assert "failed:" in capsys.readouterr().err
    assert tree.log("git") == []


@pytest.mark.parametrize("value", ["true", "1", "yes", "'true'", "maybe"])
def test_an_enabled_signer_requires_deploy_without_docker(
    tree: Tree, capfd, value: str
) -> None:
    tree.host.platform_env.write_text(
        "DITTO_CODING_HOSTED_CONTROL_ENABLED=false\n"
        f"DITTO_CODING_HOSTED_CONTROL_ENABLED={value}\n"
    )
    (tree.control / "docker-exit").write_text("1")
    revision = tree.mains[0]
    assert _install(tree, revision) == 1
    error = capfd.readouterr()
    assert "deploy can still reach the Docker daemon" in error.err
    assert "synthetic: deploy still holds the docker group" in error.out
    assert tree.log("git") == []
    assert tree.log("docker-probe") == [
        [
            "--user=deploy",
            "--group=docker",
            f"--proc={tree.host.proc_root}",
            f"--socket={tree.host.docker_socket}",
        ]
    ]


def test_activate_rechecks_docker_access_before_switching(tree: Tree, capsys) -> None:
    revision = tree.mains[0]
    assert _install(tree, revision, f"DITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n") == 0
    tree.host.platform_env.write_text("DITTO_CODING_HOSTED_CONTROL_ENABLED=true\n")
    (tree.control / "docker-exit").write_text("1")
    assert RELEASE.main(["activate"], stdin=tree.request(revision), host=tree.host) == 1
    assert "can still reach the Docker daemon" in capsys.readouterr().err
    assert not tree.host.current.exists()
    assert tree.log("systemctl") == []


def test_a_clean_docker_probe_lets_an_enabled_signer_deploy(tree: Tree) -> None:
    tree.host.platform_env.write_text("DITTO_CODING_HOSTED_CONTROL_ENABLED=true\n")
    revision = tree.mains[0]
    assert _install(tree, revision, f"DITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n") == 0
    assert RELEASE.main(["activate"], stdin=tree.request(revision), host=tree.host) == 0
    assert len(tree.log("docker-probe")) == 2


def test_each_deploy_hashes_the_tree_once_per_step(
    tree: Tree, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    real = RELEASE.manifest_digest

    def counting(release: Path) -> str:
        calls.append(release)
        return real(release)

    monkeypatch.setattr(RELEASE, "manifest_digest", counting)
    revision = tree.mains[0]
    request = f"DITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n"
    assert _install(tree, revision, request) == 0
    assert RELEASE.main(["activate"], stdin=tree.request(revision), host=tree.host) == 0
    # Receipt at install, verification at activate.
    assert len(calls) == 2
    assert _install(tree, revision, request) == 0
    assert RELEASE.main(["activate"], stdin=tree.request(revision), host=tree.host) == 0
    # A reused release: verification at install and at activate.
    assert len(calls) == 4


# --- Dashboard data ---------------------------------------------------------


def _dist(tmp_path: Path) -> Path:
    dist = tmp_path / "checkout/apps/platform/dashboard/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>\n")
    (dist / "assets/app-abcdef12.js").write_text("js\n")
    return dist


def test_dashboard_copy_is_plain_deploy_owned_data(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    (dist / "assets/app-abcdef12.js").chmod(0o777)
    target = tmp_path / "release/dist"
    target.parent.mkdir()
    assert RELEASE.copy_dashboard(dist, target, UID) == 2
    assert (target / "assets/app-abcdef12.js").read_text() == "js\n"
    assert stat.S_IMODE((target / "assets/app-abcdef12.js").stat().st_mode) == 0o644
    # No build output means an API-only release; a missing parent is an error.
    assert RELEASE.copy_dashboard(dist.parent / "never-built", target, UID) == 0
    with pytest.raises(RELEASE.ReleaseError):
        RELEASE.copy_dashboard(tmp_path / "checkout/missing/dist", target, UID)


def _plant(dist: Path, tmp_path: Path, fault: str) -> None:
    seed = tmp_path / "signer/seed"
    seed.parent.mkdir(mode=0o700)
    seed.write_bytes(b"synthetic-placeholder-not-a-key!")
    if fault == "link-to-seed":
        (dist / "assets/seed.js").symlink_to(seed)
    elif fault == "linked-directory":
        (dist / "assets/signer").symlink_to(seed.parent)
    elif fault == "linked-component":
        checkout = dist.parent
        real = checkout.parent / "real-dashboard"
        checkout.rename(real)
        checkout.symlink_to(real.name)
    elif fault == "hard-link":
        os.link(dist / "index.html", dist / "assets/copy.html")
    elif fault == "fifo":
        os.mkfifo(dist / "assets/pipe")
    elif fault == "name":
        (dist / "assets/has space.js").write_text("")
    else:
        raise AssertionError(fault)


@pytest.mark.parametrize(
    "fault",
    [
        "link-to-seed",
        "linked-directory",
        "linked-component",
        "hard-link",
        "fifo",
        "name",
    ],
)
def test_dashboard_links_and_special_files_are_refused(
    tmp_path: Path, fault: str
) -> None:
    dist = _dist(tmp_path)
    _plant(dist, tmp_path, fault)
    if fault == "link-to-seed":
        # Control: a following copy would have published the seed bytes.
        naive = tmp_path / "naive"
        shutil.copytree(dist, naive)
        assert (naive / "assets/seed.js").read_bytes().startswith(b"synthetic")
    target = tmp_path / "release/dist"
    target.parent.mkdir()
    with pytest.raises(RELEASE.ReleaseError) as error:
        RELEASE.copy_dashboard(dist, target, UID)
    assert "synthetic-placeholder" not in str(error.value)
    copied = [path.read_bytes() for path in target.rglob("*") if path.is_file()]
    assert all(b"synthetic-placeholder" not in body for body in copied)


def test_dashboard_files_owned_by_anyone_but_deploy_are_refused(tmp_path: Path) -> None:
    dist = _dist(tmp_path)
    target = tmp_path / "release/dist"
    target.parent.mkdir()
    with pytest.raises(RELEASE.ReleaseError):
        RELEASE.copy_dashboard(dist, target, UID + 1)


def test_a_foreign_file_inside_a_deploy_owned_directory_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    """The per-file owner check, separately from the directory check.

    Creating another user's file needs root, so the opened file's owner is
    reported as foreign while every directory keeps its real owner.
    """
    dist = _dist(tmp_path)
    real_fstat = os.fstat

    def foreign_files(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return info
        fields = list(info[:10])
        fields[4] = UID + 1
        return os.stat_result(fields)

    monkeypatch.setattr(RELEASE.os, "fstat", foreign_files)
    target = tmp_path / "release/dist"
    target.parent.mkdir()
    with pytest.raises(RELEASE.ReleaseError) as error:
        RELEASE.copy_dashboard(dist, target, UID)
    assert "deploy-owned single link" in str(error.value)
    assert not (target / "index.html").exists()


def test_dashboard_size_is_bounded(tmp_path: Path, monkeypatch) -> None:
    dist = _dist(tmp_path)
    monkeypatch.setattr(RELEASE, "MAX_DASHBOARD_FILES", 1)
    target = tmp_path / "release/dist"
    target.parent.mkdir()
    with pytest.raises(RELEASE.ReleaseError):
        RELEASE.copy_dashboard(dist, target, UID)


# --- Sealing ----------------------------------------------------------------


def _release(tmp_path: Path) -> tuple[Any, Any, Path]:
    host = RELEASE.Host(trusted_uid=UID, trusted_gid=GID, anchor=tmp_path)
    settings = RELEASE.Settings(Path("/usr/bin/python3.13"), "deploy")
    release = tmp_path / "release"
    (release / "apps/platform/.venv/bin").mkdir(parents=True)
    (release / "apps/platform/.venv/bin/python").symlink_to("/usr/bin/python3.13")
    (release / "apps/platform/.venv/bin/python3").symlink_to("python")
    (release / "apps/platform/code.py").write_text("")
    return host, settings, release


@pytest.mark.parametrize(
    "fault",
    ["escaping-link", "absolute-link", "python-link-elsewhere", "hard-link", "fifo"],
)
def test_seal_refuses_links_out_of_the_release_and_special_files(
    tmp_path: Path, fault: str
) -> None:
    host, settings, release = _release(tmp_path)
    platform = release / "apps/platform"
    if fault == "escaping-link":
        (platform / "outside").symlink_to("../../../etc")
    elif fault == "absolute-link":
        (platform / "signer").symlink_to("/etc/ditto-platform/coding-hosted-signer")
    elif fault == "python-link-elsewhere":
        (platform / "python").symlink_to("/usr/bin/python3.13")
    elif fault == "hard-link":
        os.link(platform / "code.py", platform / "code2.py")
    else:
        os.mkfifo(platform / "pipe")
    with pytest.raises(RELEASE.ReleaseError):
        RELEASE.seal(host, settings, release)


def test_seal_refuses_a_chain_of_relative_links_that_escapes(tmp_path: Path) -> None:
    """Each target looks harmless alone; resolved hop by hop it leaves.

    `sub/hop -> ../..` stays inside (it names release/apps). `esc ->
    sub/hop/../..` also normalizes to a path inside, but the kernel resolves
    `hop` first and then climbs two levels above release/apps.
    """
    host, settings, release = _release(tmp_path)
    platform = release / "apps/platform"
    (platform / "sub").mkdir()
    (platform / "sub/hop").symlink_to("../..")
    (platform / "esc").symlink_to("sub/hop/../..")
    # Control: the old textual rule accepted it; the kernel escapes.
    textual = os.path.normpath(os.path.join(platform, "sub/hop/../.."))
    assert textual.startswith(f"{release}{os.sep}")
    assert not Path(os.path.realpath(platform / "esc")).is_relative_to(release)
    assert RELEASE._allowed_link(release, platform / "sub/hop", settings)
    assert not RELEASE._allowed_link(release, platform / "esc", settings)
    with pytest.raises(RELEASE.ReleaseError, match="leaves the release"):
        RELEASE.seal(host, settings, release)


def test_seal_refuses_a_link_loop(tmp_path: Path) -> None:
    host, settings, release = _release(tmp_path)
    platform = release / "apps/platform"
    (platform / "a").symlink_to("b")
    (platform / "b").symlink_to("a")
    with pytest.raises(RELEASE.ReleaseError):
        RELEASE.seal(host, settings, release)


def test_seal_accepts_the_venv_interpreter_and_internal_links(tmp_path: Path) -> None:
    host, settings, release = _release(tmp_path)
    (release / "apps/platform/.venv/lib").mkdir()
    (release / "apps/platform/.venv/lib64").symlink_to("lib")
    (release / "apps/platform/code.py").chmod(0o666)
    RELEASE.seal(host, settings, release)
    assert stat.S_IMODE((release / "apps/platform/code.py").stat().st_mode) == 0o644


# --- The seed is never opened ----------------------------------------------

AUDITED_INSTALL = """
import importlib.util, io, json, os, pwd, re, sys
from pathlib import Path
config = json.loads(sys.argv[1])
seed_dir = config["seed_dir"]
def audit(event, args):
    if event == "subprocess.Popen" and seed_dir in repr(args):
        os._exit(96)
    if event in {"open", "os.listdir", "os.scandir", "os.chmod", "os.chown",
                 "shutil.copyfile", "shutil.rmtree", "os.rename", "os.remove"}:
        target = args[0] if args else None
        if not isinstance(target, (str, bytes, os.PathLike)):
            return
        if seed_dir in os.fsdecode(target):
            os._exit(97)
sys.addaudithook(audit)
if config.get("control"):
    open(Path(seed_dir) / "seed", "rb").close()
spec = importlib.util.spec_from_file_location("installer", config["installer"])
installer = importlib.util.module_from_spec(spec)
sys.modules["installer"] = installer
spec.loader.exec_module(installer)
uid, gid = os.getuid(), os.getgid()
installer.PYTHON = re.compile(re.escape(config["python"]))
def account(name):
    return pwd.struct_passwd((name, "x", uid, gid, "", "/", ""))
installer.account = account
installer.require_distinct_accounts = lambda *_: None
fields = {
    k: (Path(v) if k in config["paths"] else v) for k, v in config["host"].items()
}
host = installer.Host(**fields)
for command, body in config["steps"]:
    code = installer.main([command], stdin=io.BytesIO(body.encode()), host=host)
    if code != 0:
        os._exit(10 + code)
os._exit(0)
"""


def test_install_activate_stop_and_logs_never_touch_the_seed(tmp_path: Path) -> None:
    if UID == 0:
        pytest.skip("synthetic trees model a non-root trusted owner")
    tree = build_tree(tmp_path)
    host = tree.host
    # Enabled, so the Docker probe runs inside the audited process tree too.
    host.platform_env.write_text("DITTO_CODING_HOSTED_CONTROL_ENABLED=true\n")
    fields = {
        field.name: getattr(host, field.name) for field in dataclasses.fields(host)
    }
    paths = [name for name, value in fields.items() if isinstance(value, Path)]
    revision = tree.mains[-1]
    config = {
        "installer": str(INSTALLER),
        "python": str(tree.python),
        "seed_dir": str(tree.seed.parent),
        "paths": paths,
        "host": {
            name: str(value) if isinstance(value, Path) else value
            for name, value in fields.items()
        },
        "steps": [
            [
                "install",
                f"revision={revision}\nDITTO_UPLOAD_PAYMENT_ADDRESS={PAYMENT}\n",
            ],
            ["activate", f"revision={revision}\n"],
            ["logs", ""],
            ["stop", ""],
        ],
    }

    def run(**extra: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                AUDITED_INSTALL,
                json.dumps({**config, **extra}),
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    # Control: the hook does catch a real open of the synthetic seed.
    assert run(control=True).returncode == 97
    result = run()
    assert result.returncode == 0, result.stderr
    assert len(tree.log("docker-probe")) == 2
    assert os.readlink(host.current) == f"releases/{revision}"
