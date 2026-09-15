"""Deploy access narrowing and the dedicated ditto-api service identity.

Static and synthetic checks of the base and platform_app roles, the launcher,
the units and scripts/update.sh. Real Ansible rendering of the env, unit and
Pylon templates runs in infra/ansible/tests/coding-hosted-control-signer.yml;
the root release installer has its own suite (test_platform_api_release.py).
Nothing here needs root, systemd, Docker or a key.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
ANSIBLE = ROOT / "infra/ansible"
BASE = ANSIBLE / "roles/base"
ROLE = ANSIBLE / "roles/platform_app"
UPDATER = ROOT / "apps/platform/scripts/update.sh"
INSTALLER_DEST = "/usr/local/sbin/ditto-platform-api-release"
LAUNCHER_DEST = "/usr/local/libexec/ditto-platform-api/launch"
UNIT = "ditto-platform-api.service"
PYLON_UNIT = "ditto-platform-pylon.service"


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _task(path: Path, name: str) -> dict[str, Any]:
    (task,) = [task for task in _load(path) if task.get("name") == name]
    return task


def _content_lines(task: dict[str, Any]) -> list[str]:
    content = task["ansible.builtin.copy"]["content"]
    return [line for line in content.splitlines() if line and not line.startswith("#")]


def _visudo(tmp_path: Path, content: str) -> None:
    visudo = shutil.which("visudo") or "/usr/sbin/visudo"
    if not Path(visudo).exists():
        pytest.skip("visudo is not installed")
    rules = tmp_path / "rules"
    rules.write_text(content)
    result = subprocess.run(
        [visudo, "-cf", str(rules)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


# --- base role: journal access without root ---------------------------------


TEAM_SERVICES = {
    "ditto-image-builder",
    "ditto-platform-api",
    "ditto-platform-pylon",
    "ditto-pylon",
    "ditto-screener",
    "ditto-screener-capacity",
    "ditto-screener-enroll",
    "ditto-screener-fleet-agent",
    "ditto-screener-fleet-auto-update",
    "ditto-screener-source-review-secret",
    "ditto-validator",
    "ditto-validator-stack-auto-update",
    "ditto-validator-stack-prefetch",
    "ditto-validator-stack-updater-refresh",
    "ditto-worker",
}
TEAM_TIMERS = {
    "ditto-screener-fleet-auto-update",
    "ditto-screener-source-review-secret",
    "ditto-validator-auto-update",
    "ditto-validator-stack-auto-update",
    "ditto-validator-stack-prefetch",
    "ditto-validator-stack-updater-refresh",
}
# Units whose stop would weaken isolation. No team or deploy rule may name them.
GUARD_UNITS = (
    "ditto-coding-executor",
    "ditto-coding-hosted",
    "ditto-sandbox-firewall",
    "ditto-imds-guard",
    "ditto-egress-proxy",
    "ditto-screener-docker",
)
PYLON_JOURNAL = (
    "/usr/bin/journalctl --no-pager --quiet --output=short-iso --lines=80 "
    "--unit=ditto-platform-pylon.service"
)


def _team_rules() -> tuple[dict[str, Any], list[str], list[str]]:
    task = _task(BASE / "tasks/users.yml", "Ensure team sudoers grant")
    content = task["ansible.builtin.copy"]["content"]
    logical = re.sub(r"\\\n\s*", "", content)
    lines = [
        line.strip()
        for line in logical.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    (alias,) = [line for line in lines if line.startswith("Cmnd_Alias ")]
    name, commands = alias.removeprefix("Cmnd_Alias ").split(" = ", 1)
    assert name == "DITTO_TEAM_UNITS"
    rules = [line for line in lines if line != alias]
    return task, [command.strip() for command in commands.split(",")], rules


def test_team_sudoers_is_exact_root_only_and_excludes_deploy(tmp_path: Path) -> None:
    task, commands, rules = _team_rules()
    copy = task["ansible.builtin.copy"]
    assert copy["dest"] == "/etc/sudoers.d/ditto-team"
    assert (copy["owner"], copy["group"], copy["mode"]) == ("root", "root", "0440")
    assert copy["validate"] == "/usr/sbin/visudo -cf %s"
    assert rules == [
        "%ditto,!deploy ALL=(root) NOPASSWD: DITTO_TEAM_UNITS",
        "%ditto,!deploy ALL=(root) NOPASSWD: /bin/systemctl daemon-reload, "
        "/bin/systemctl reload caddy",
        "%ditto ALL=(deploy) NOPASSWD: ALL",
    ]
    expected = set()
    for service in TEAM_SERVICES:
        expected |= {service, f"{service}.service"}
    expected |= {f"{timer}.timer" for timer in TEAM_TIMERS}
    exact = re.compile(
        r"/bin/systemctl (start|stop|restart) (ditto-[a-z0-9-]+(\.service|\.timer)?)"
    )
    seen: dict[str, set[str]] = {}
    for command in commands:
        match = exact.fullmatch(command)
        assert match, command
        seen.setdefault(match.group(2), set()).add(match.group(1))
    assert set(seen) == expected
    assert all(verbs == {"start", "stop", "restart"} for verbs in seen.values())
    assert len(commands) == len(set(commands)) == 3 * len(expected)
    text = copy["content"]
    assert "*" not in text.replace("# ", "")
    assert "(ALL)" not in text and "journalctl" not in "".join(rules + commands)
    assert "status" not in "".join(rules + commands)
    for guard in GUARD_UNITS:
        assert guard not in "".join(commands), guard
    _visudo(tmp_path, text)


def test_team_members_read_the_journal_without_sudo_and_deploy_does_not() -> None:
    users = _load(BASE / "tasks/users.yml")
    (team,) = [task for task in users if "team members exist" in task.get("name", "")]
    assert team["ansible.builtin.user"]["groups"] == (
        "{{ [deploy_group | default('ditto'), 'sudo', 'systemd-journal'] }}"
    )
    (deploy,) = [
        task
        for task in users
        if task.get("name") == "Ensure 'deploy' service account exists"
    ]
    assert (
        deploy["ansible.builtin.user"]["groups"]
        == "{{ deploy_group | default('ditto') }}"
    )
    # The only journalctl rule in any role is the exact, pager-free Pylon read.
    for role in (ANSIBLE / "roles").iterdir():
        for path in sorted(role.rglob("*.yml")):
            for line in _strings(_load(path)):
                rule = line.strip()
                if "NOPASSWD" in rule and not rule.startswith("#"):
                    assert "*" not in rule, (path, rule)
                    if "journalctl" in rule:
                        owner = "{{ platform_owner }}"
                        expected = f"{owner} ALL=(root) NOPASSWD: {PYLON_JOURNAL}"
                        assert rule == expected, (path, rule)


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return node.splitlines()
    if isinstance(node, dict):
        return [line for value in node.values() for line in _strings(value)]
    if isinstance(node, list):
        return [line for value in node for line in _strings(value)]
    return []


# --- platform_app: Pylon without the docker group ----------------------------


def test_switches_ship_off_and_are_reviewed_activations() -> None:
    defaults = _load(ROLE / "defaults/main.yml")
    assert defaults["platform_pylon_root_unit_enabled"] is False
    assert defaults["platform_api_service_identity_enabled"] is False
    assert defaults["platform_coding_hosted_control_enabled"] is False

    main = _load(ROLE / "tasks/main.yml")
    by_name = {task.get("name"): task for task in main}
    docker = by_name["Add the deploy user to the docker group"]
    assert docker["when"] == "not (platform_pylon_root_unit_enabled | bool)"
    assert docker["ansible.builtin.user"] == {
        "name": "{{ platform_owner }}",
        "groups": "docker",
        "append": True,
    }
    pylon = by_name[
        "Run Pylon from a root-owned unit and take deploy out of the docker group"
    ]
    identity = by_name[
        "Prepare the dedicated ditto-api identity and its sealed releases"
    ]
    assert pylon == {
        "name": pylon["name"],
        "ansible.builtin.import_tasks": "pylon_root_unit.yml",
        "when": "platform_pylon_root_unit_enabled | bool",
    }
    assert identity == {
        "name": identity["name"],
        "ansible.builtin.import_tasks": "api_service_identity.yml",
        "when": "platform_api_service_identity_enabled | bool",
    }
    names = [task.get("name") for task in main]
    accounts = by_name["Create the dedicated ditto-api and ditto-api-build accounts"]
    assert accounts == {
        "name": accounts["name"],
        "ansible.builtin.import_tasks": "api_service_accounts.yml",
        "when": "platform_api_service_identity_enabled | bool",
    }
    # The users exist before anything chowns to ditto-api, so one converge with
    # the identity and Hippius evidence both on succeeds.
    signer_guard = "Verify the pre-placed hosted-v2 control signer without reading it"
    assert names.index(signer_guard) + 1 == names.index(accounts["name"])
    for owner_task in (
        "Validate protected Hippius Coding evidence authority files",
        "Ensure protected Hippius Coding evidence spool",
    ):
        assert names.index(accounts["name"]) < names.index(owner_task)
        assert "platform_api_process_user" in yaml.safe_dump(by_name[owner_task])
    guard = "Guard the rendered .env against unresolved placeholders"
    assert (
        names.index(guard) < names.index(pylon["name"]) < names.index(identity["name"])
    )
    assert names.index("Resolve the user that runs ditto-api") < names.index(
        "Verify the pre-placed hosted-v2 control signer without reading it"
    )
    resolve = by_name["Resolve the user that runs ditto-api"][
        "ansible.builtin.set_fact"
    ]
    assert resolve["platform_api_process_user"].strip() == (
        "{{ 'ditto-api' if platform_api_service_identity_enabled | bool "
        "else platform_owner }}"
    )


def test_pylon_compose_copy_is_identical_to_the_platform_service() -> None:
    platform = _load(ROOT / "apps/platform/docker-compose.yml")["services"]["pylon"]
    copy = _load(ROLE / "files/pylon-compose.yml")
    assert list(copy) == ["services"]
    assert copy["services"] == {"pylon": platform}


def test_pylon_unit_reads_only_root_owned_inputs() -> None:
    unit = (ROLE / "templates/ditto-platform-pylon.service.j2").read_text().splitlines()
    assert (
        "ExecStart=/usr/bin/docker compose --project-name "
        "{{ platform_compose_project_name }} "
        "--project-directory /etc/ditto-platform/pylon "
        "--file /etc/ditto-platform/pylon/compose.yml up --detach --wait pylon"
    ) in unit
    assert "EnvironmentFile=/etc/ditto-platform/pylon/pylon.env" in unit
    assert "Environment=DOCKER_CONFIG=/etc/ditto-platform/pylon/docker-client" in unit
    assert "Type=oneshot" in unit
    assert "[Install]" not in unit
    env = [
        line
        for line in (ROLE / "templates/pylon.env.j2").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert env == [
        "SUBTENSOR_NETWORK={{ platform_subtensor_network }}",
        "PYLON_OPEN_ACCESS_TOKEN={{ platform_secrets.pylon_token }}",
        # Root-owned and empty: dockerd resolves the bind source as root, so a
        # path in deploy's home could be redirected by a symlink.
        "BITTENSOR_WALLET_PATH=/etc/ditto-platform/pylon/wallets",
    ]
    assert "/home/" not in "\n".join(env)
    # Every variable the compose file interpolates is either supplied above or
    # keeps the default update.sh's exported .env left it at.
    compose = (ROLE / "files/pylon-compose.yml").read_text()
    interpolated = set(re.findall(r"\$\{([A-Z_]+)", compose))
    assert interpolated == {
        "PYLON_OPEN_ACCESS_TOKEN",
        "SUBTENSOR_NETWORK",
        "PYLON_IDENTITIES",
        "PYLON_ID_VALIDATOR_WALLET_NAME",
        "PYLON_ID_VALIDATOR_HOTKEY_NAME",
        "PYLON_TOKEN",
        "BITTENSOR_WALLET_PATH",
        "HOME",
    }
    rendered = (ROLE / "templates/platform.env.j2").read_text()
    for name in interpolated - {"PYLON_OPEN_ACCESS_TOKEN", "SUBTENSOR_NETWORK", "HOME"}:
        assert f"\n{name}=" not in rendered, name


def test_pylon_tasks_install_one_exact_rule_and_remove_docker_membership(
    tmp_path: Path,
) -> None:
    path = ROLE / "tasks/pylon_root_unit.yml"
    tasks = _load(path)
    rule = _task(path, "Allow the deploy user to re-run and read only the Pylon unit")
    copy = rule["ansible.builtin.copy"]
    assert (copy["dest"], copy["mode"], copy["validate"]) == (
        "/etc/sudoers.d/ditto-platform-pylon",
        "0440",
        "/usr/sbin/visudo -cf %s",
    )
    assert _content_lines(rule) == [
        "{{ platform_owner }} ALL=(root) NOPASSWD: "
        "/usr/bin/systemctl restart ditto-platform-pylon.service",
        f"{{{{ platform_owner }}}} ALL=(root) NOPASSWD: {PYLON_JOURNAL}",
    ]
    # update.sh prints exactly that journal read when the unit fails.
    assert PYLON_JOURNAL.replace(" --unit=", " \\\n        --unit=") in (
        UPDATER.read_text()
    )

    wallets = _task(
        path,
        "Inspect the deploy wallet directory Pylon used to mount, "
        "without following links",
    )["ansible.builtin.stat"]
    assert wallets["path"] == "/home/{{ platform_owner }}/.bittensor/wallets"
    assert wallets["follow"] is False
    listing = _task(path, "List the deploy wallet directory without following links")
    assert listing["ansible.builtin.find"]["follow"] is False
    assert "not platform_pylon_deploy_wallets.stat.islnk" in listing["when"]
    refusal = _task(path, "Refuse a Pylon signing identity or a redirected wallet path")
    (condition,) = refusal["ansible.builtin.assert"]["that"]
    assert "platform_pylon_deploy_wallet_entries.matched == 0" in condition
    assert "not platform_pylon_deploy_wallets.stat.islnk" in condition
    directories = _task(path, "Ensure the root-only Pylon unit directories")
    assert "/etc/ditto-platform/pylon/wallets" in directories["loop"]
    _visudo(tmp_path, copy["content"].replace("{{ platform_owner }}", "deploy"))

    removal = _task(
        path, "Take the deploy user out of the root-equivalent docker group"
    )
    assert removal["ansible.builtin.command"]["argv"] == [
        "/usr/sbin/gpasswd",
        "--delete",
        "{{ platform_owner }}",
        "docker",
    ]
    assert "getent_group" in removal["when"]
    for task in tasks:
        module = task.get("ansible.builtin.systemd")
        if module is not None:
            assert module == {"daemon_reload": True}, task
    for name in ("compose.yml", "pylon.env"):
        (installed,) = [
            task
            for task in tasks
            if str(
                (
                    task.get("ansible.builtin.copy")
                    or task.get("ansible.builtin.template")
                    or {}
                ).get("dest", "")
            ).endswith(name)
        ]
        module = (
            installed.get("ansible.builtin.copy")
            or installed["ansible.builtin.template"]
        )
        assert (module["owner"], module["group"], module["mode"]) == (
            "root",
            "root",
            "0600",
        )


# --- platform_app: the dedicated ditto-api identity -------------------------

API_SUDOERS = [
    f"{{{{ platform_owner }}}} ALL=(root) NOPASSWD: {INSTALLER_DEST} {command}"
    for command in ("install", "activate", "stop", "logs")
]


def test_identity_sudoers_allows_exactly_four_installer_commands(
    tmp_path: Path,
) -> None:
    path = ROLE / "tasks/api_service_identity.yml"
    rule = _task(
        path, "Allow the deploy user exactly the four ditto-api release commands"
    )
    copy = rule["ansible.builtin.copy"]
    assert (copy["dest"], copy["owner"], copy["group"], copy["mode"]) == (
        "/etc/sudoers.d/ditto-platform-api",
        "root",
        "root",
        "0440",
    )
    assert copy["validate"] == "/usr/sbin/visudo -cf %s"
    assert _content_lines(rule) == API_SUDOERS
    for line in _content_lines(rule):
        assert "*" not in line and "ALL=(ALL)" not in line and "^" not in line
    _visudo(tmp_path, copy["content"].replace("{{ platform_owner }}", "deploy"))


def test_identity_users_are_locked_down_and_outside_privileged_groups() -> None:
    path = ROLE / "tasks/api_service_accounts.yml"
    users = _task(path, "Ensure the locked-down ditto-api and ditto-api-build users")
    module = users["ansible.builtin.user"]
    assert module["groups"] == [] and module["append"] is False
    assert module["system"] is True
    assert module["shell"] == "/usr/sbin/nologin"
    assert module["password"] == "!"
    assert module["create_home"] is False
    assert [item["name"] for item in users["loop"]] == ["ditto-api", "ditto-api-build"]

    refusal = _task(path, "Refuse privileged group membership for the dedicated users")
    loop = refusal["loop"]
    for group in ("docker", "sudo", "adm", "systemd-journal", "google-sudoers"):
        assert f"'{group}'" in loop
    assert "deploy_group" in loop and "platform_owner" in loop

    first = _load(path)[0]
    assert (
        first["name"]
        == "Require distinct deploy and dedicated accounts before creating them"
    )
    assert (
        "platform_owner not in ['root', 'ditto-api', 'ditto-api-build']"
        in (first["ansible.builtin.assert"]["that"])
    )
    identity = _task(
        ROLE / "tasks/api_service_identity.yml",
        "Require a supported host and a pinned release interpreter",
    )
    that = identity["ansible.builtin.assert"]["that"]
    assert "platform_api_process_user == 'ditto-api'" in that
    assert "platform_owner not in ['root', 'ditto-api', 'ditto-api-build']" in that
    assert (
        "platform_repo_url == 'git@github.com:ditto-assistant/ditto-subnet.git'" in that
    )


def test_identity_tasks_install_root_owned_code_and_never_start_a_unit() -> None:
    path = ROLE / "tasks/api_service_identity.yml"
    tasks = _load(path)
    installer = _task(path, "Install the root release installer")[
        "ansible.builtin.copy"
    ]
    launcher = _task(path, "Install the ditto-api launcher")["ansible.builtin.copy"]
    assert installer == {
        "src": "platform-api-release.py",
        "dest": INSTALLER_DEST,
        "owner": "root",
        "group": "root",
        "mode": "0755",
    }
    assert launcher == {
        "src": "platform-api-launch",
        "dest": LAUNCHER_DEST,
        "owner": "root",
        "group": "root",
        "mode": "0755",
    }
    probe = _task(
        path, "Install the live Docker-access probe the installer runs for the signer"
    )
    assert probe["ansible.builtin.copy"] == {
        "src": "deploy-docker-access.py",
        "dest": "/usr/local/libexec/ditto-platform-api/deploy-docker-access",
        "owner": "root",
        "group": "root",
        "mode": "0755",
    }
    assert not any(
        "ansible.builtin.user" in task or "ansible.builtin.group" in task
        for task in tasks
    )
    env = _task(path, "Render the root-owned ditto-api environment")
    assert env["ansible.builtin.template"] == {
        "src": "platform.env.j2",
        "dest": "/etc/ditto-platform/api/platform.env",
        "owner": "root",
        "group": "ditto-api",
        "mode": "0640",
    }
    assert env["vars"] == {"platform_checkout_root": "/opt/ditto-platform-api/current"}
    assert env["no_log"] is True
    key = _task(path, "Install a root-only copy of the read-only GitHub deploy key")
    assert key["no_log"] is True
    assert key["ansible.builtin.copy"]["mode"] == "0600"
    directories = _task(
        path, "Ensure the root-owned release and configuration directories"
    )
    assert {
        item["path"]: (item["group"], item["mode"]) for item in directories["loop"]
    } == {
        "/etc/ditto-platform": ("root", "0755"),
        "/etc/ditto-platform/api": ("ditto-api", "0750"),
        "/opt/ditto-platform-api": ("root", "0755"),
        "/opt/ditto-platform-api/releases": ("root", "0755"),
        "/usr/local/libexec/ditto-platform-api": ("root", "0755"),
        "/var/lib/ditto-platform-api-release": ("root", "0700"),
    }
    for task in tasks:
        systemd = task.get("ansible.builtin.systemd")
        if systemd is not None:
            assert systemd == {"daemon_reload": True}, task
        assert "ansible.builtin.service" not in task
        assert "coding-hosted-signer" not in yaml.safe_dump(task)


UNIT_HARDENING = [
    "User=ditto-api",
    "Group=ditto-api",
    "SupplementaryGroups=",
    f"ExecStart={LAUNCHER_DEST} serve",
    "NoNewPrivileges=yes",
    "CapabilityBoundingSet=",
    "AmbientCapabilities=",
    "ProtectSystem=strict",
    "ReadOnlyPaths=/opt/ditto-platform-api /etc/ditto-platform",
    "InaccessiblePaths=-/opt/ditto-subnet -/opt/ditto-platform "
    "-/opt/ditto-platform-relay -/var/lib/ditto-platform-api-release "
    "-/var/lib/ditto-platform-api-build -/run/docker.sock",
    "ProtectHome=yes",
    "PrivateTmp=yes",
    "PrivateDevices=yes",
    "RestrictSUIDSGID=yes",
    "RemoveIPC=yes",
    "ProtectKernelTunables=yes",
    "ProtectKernelModules=yes",
    "ProtectKernelLogs=yes",
    "ProtectControlGroups=yes",
    "ProtectClock=yes",
    "ProtectHostname=yes",
    "ProtectProc=invisible",
    "RestrictNamespaces=yes",
    "RestrictRealtime=yes",
    "LockPersonality=yes",
    "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK",
    "SystemCallArchitectures=native",
    "SystemCallFilter=@system-service",
    "SystemCallFilter=~@privileged",
    "UMask=0077",
    "MemoryMax=3072M",
    "KillSignal=SIGINT",
    "TimeoutStopSec=35",
]


def test_api_unit_is_hardened_and_only_the_spool_is_writable() -> None:
    template = (ROLE / "templates/ditto-platform-api.service.j2").read_text()
    lines = template.splitlines()
    for directive in UNIT_HARDENING:
        assert directive in lines, directive
    keys = [
        line.split("=", 1)[0]
        for line in lines
        if "=" in line and not line.startswith("#")
    ]
    # Nothing weakens the sandbox or runs a step with elevated rights.
    for forbidden in (
        "PermissionsStartOnly",
        "AmbientCapabilities=CAP",
        "ReadWriteDirectories",
    ):
        assert forbidden not in template
    assert not any(
        line.startswith(("ExecStart=+", "ExecStart=!", "ExecStartPre=+"))
        for line in lines
    )
    assert keys.count("ReadWritePaths") == 1
    # One launcher run resolves the release for both the preflight and the
    # server; a separate ExecStartPre would resolve `current` again.
    assert "ExecStartPre" not in keys and keys.count("ExecStart") == 1
    block = template[
        template.index("{% if platform_coding_hippius_evidence_enabled | bool %}") :
    ]
    assert block.splitlines()[1] == (
        "ReadWritePaths={{ platform_coding_hippius_evidence_spool_root }}"
    )
    assert "EnvironmentFile" not in template  # only the root-owned launcher sources env


def test_launcher_runs_only_as_ditto_api_from_a_sealed_release() -> None:
    launcher = ROLE / "files/platform-api-launch"
    text = launcher.read_text()
    subprocess.run(["bash", "-n", str(launcher)], check=True)
    assert "readonly api_root=/opt/ditto-platform-api\n" in text
    assert "readonly config_dir=/etc/ditto-platform/api\n" in text
    assert '[ "$(id -un)" = ditto-api ]' in text
    assert '. "$config_dir/platform.env"' in text
    assert '. "$config_dir/deploy.env"' in text
    assert re.findall(r"^\s*(?:exec|\.)\s.*$", text, re.M) == [
        '. "$config_dir/platform.env"',
        '  . "$config_dir/deploy.env"',
        "  exec ./.venv/bin/python -I -m "
        "ditto.api_server.coding_hosted_signer_preflight --check-metadata",
        "exec ./.venv/bin/python -I -m ditto.api_server",
    ]
    assert 'export DITTO_BUILD_COMMIT="$revision"' in text
    assert text.count("readlink -e") == 1
    code = [line.strip() for line in text.splitlines() if line.strip()]
    preflight = (
        "./.venv/bin/python -I -m ditto.api_server.coding_hosted_signer_preflight "
        "--check-metadata"
    )
    # serve: the preflight runs (and `set -e` stops on failure), then the server
    # execs, both from the single resolved release.
    assert code.index(preflight) + 1 == code.index(
        "exec ./.venv/bin/python -I -m ditto.api_server"
    )
    assert "set -euo pipefail" in code
    assert "seed" not in re.sub(r"#.*", "", text)

    # As any user but ditto-api it stops before sourcing or running anything.
    if subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip() == (
        "ditto-api"
    ):
        pytest.skip("the refusal needs a user other than ditto-api")
    refused = subprocess.run(
        ["bash", str(launcher), "serve"], capture_output=True, text=True, check=False
    )
    assert refused.returncode == 77
    assert "runs only as ditto-api" in refused.stderr
    assert (
        subprocess.run(
            ["bash", str(launcher), "shell"],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 64
    )


def test_update_script_commands_match_sudoers_exactly() -> None:
    updater = UPDATER.read_text()
    assert f"readonly api_release_command={INSTALLER_DEST}\n" in updater
    assert f"readonly api_unit={UNIT}\n" in updater
    assert f"readonly pylon_unit={PYLON_UNIT}\n" in updater
    used = set(re.findall(r'sudo -n "\$api_release_command" (\w+)', updater))
    assert used == {"install", "activate", "stop", "logs"}
    assert {line.rsplit(" ", 1)[-1] for line in API_SUDOERS} == used
    assert 'sudo -n /usr/bin/systemctl restart "$pylon_unit"' in updater


def test_release_build_backends_are_pinned_with_hashes() -> None:
    requirements = (ROOT / "apps/platform/release-build-requirements.txt").read_text()
    logical = re.sub(r"\\\n\s*", " ", requirements)
    pins: dict[str, list[str]] = {}
    for line in logical.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        spec, *hashes = line.split(" --hash=")
        name, _, version = spec.strip().partition("==")
        assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name) and version, line
        assert hashes and all(
            re.fullmatch(r"sha256:[0-9a-f]{64}", h.strip()) for h in hashes
        )
        pins[name] = hashes
    # Backends of the project and the shared protocol, hatch-vcs for the pinned
    # bittensor-pylon-client Git dependency, editables for the editable build.
    assert {"hatchling", "hatch-vcs", "editables"} <= set(pins)
    for project in ("apps/platform", "packages/ditto-screening-protocol"):
        pyproject = (ROOT / project / "pyproject.toml").read_text()
        requires = re.search(r"requires = \[([^\]]*)\]", pyproject)
        assert requires
        for requirement in re.findall(r'"([A-Za-z0-9_.-]+)', requires.group(1)):
            assert requirement.lower() in pins, (project, requirement)
    lock = (ROOT / "apps/platform/uv.lock").read_text()
    assert (
        'source = { git = "https://github.com/bittensor-church/bittensor-pylon' in lock
    )


def test_stop_and_profile_scripts_follow_the_supervisor() -> None:
    stop = (ROOT / "apps/platform/scripts/stop.sh").read_text()
    assert "sudo -n /usr/local/sbin/ditto-platform-api-release stop" in stop
    assert "pm2 stop ditto-api || true" in stop
    profile = (ROOT / "apps/platform/scripts/profile-python.sh").read_text()
    assert (
        'pid="$(systemctl show --property=MainPID --value ditto-platform-api.service)"'
        in profile
    )
    subprocess.run(
        ["bash", "-n", str(ROOT / "apps/platform/scripts/stop.sh")], check=True
    )
    subprocess.run(
        ["bash", "-n", str(ROOT / "apps/platform/scripts/profile-python.sh")],
        check=True,
    )


def test_start_script_leaves_ditto_api_to_the_unit_in_systemd_mode() -> None:
    start = (ROOT / "apps/platform/scripts/start.sh").read_text()
    assert 'process.env.DITTO_PLATFORM_API_SUPERVISOR === "systemd"' in start
    assert '.filter((name) => !(systemd && name === "ditto-api"))' in start


def test_env_template_renders_supervision_defaults() -> None:
    template = (ROLE / "templates/platform.env.j2").read_text()
    assert (
        "DITTO_PLATFORM_API_SUPERVISOR={{ 'systemd' if "
        "platform_api_service_identity_enabled | bool else 'pm2' }}\n"
    ) in template
    assert (
        "DITTO_PLATFORM_PYLON_UNIT={{ 'ditto-platform-pylon.service' if "
        "platform_pylon_root_unit_enabled | bool else '' }}\n"
    ) in template
