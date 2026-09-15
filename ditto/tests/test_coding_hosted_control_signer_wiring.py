"""Default-off host wiring for the hosted-v2 Platform control signer.

Platform converge may only stat-verify a seed placed by a separate protected
ceremony; validators register trust in one public address. Real Ansible
rendering and the stat guard's negative cases run in
infra/ansible/tests/coding-hosted-control-signer.yml (Infrastructure CI). This
root suite runs on every pull request, including infra-only changes.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from ditto.validator import coding_hosted_control

ROOT = Path(__file__).parents[2]
ANSIBLE = ROOT / "infra/ansible"
PLATFORM_ROLE = ANSIBLE / "roles/platform_app"
VALIDATOR_ROLE = ANSIBLE / "roles/validator_stack"
SEED_DIRECTORY = "/etc/ditto-platform/coding-hosted-signer"
SEED_FILE = f"{SEED_DIRECTORY}/seed"
SEED_ANCESTORS = ["/etc/ditto-platform", "/etc", "/"]
GUARD_VARS = {
    "platform_coding_hosted_signer_guard_ancestors": SEED_ANCESTORS,
    "platform_coding_hosted_signer_guard_directory": SEED_DIRECTORY,
    "platform_coding_hosted_signer_guard_seed": SEED_FILE,
}
ACTIVATION_FLAGS = (
    "platform_coding_hosted_control_enabled",
    "validator_stack_coding_hosted_control_enabled",
    # The signer's isolation prerequisites are reviewed activations too.
    "platform_api_service_identity_enabled",
    "platform_pylon_root_unit_enabled",
)
# Activation is a reviewed change: a host_vars (or workflow) file that sets an
# activation flag truthy must be listed here, in the same reviewed pull request.
# Staging a hotkey, or setting a flag false while revoking, needs no entry.
REVIEWED_ACTIVATIONS: dict[str, frozenset[str]] = {}
FALSY = (False, None, 0, "", "false", "False", "no", "off", "0", "n", "f")
ANSIBLE_HOTKEY_PATTERN = "^5[1-9A-HJ-NP-Za-km-z]{47}$"
SEED_MARKERS = (
    "coding-hosted-signer",
    "coding_hosted_signer_seed",
    "coding_hosted_signer_guard",
    "DITTO_CODING_HOSTED_SIGNER_SEED_FILE",
)
# Modules that can read, create, move, hash or transport file contents.
CONTENT_MODULES = {
    "archive",
    "assemble",
    "blockinfile",
    "command",
    "copy",
    "fetch",
    "file",
    "get_url",
    "lineinfile",
    "raw",
    "replace",
    "script",
    "shell",
    "slurp",
    "synchronize",
    "template",
    "unarchive",
    "uri",
}
TASK_KEYWORDS = {
    "args",
    "become",
    "become_user",
    "block",
    "always",
    "changed_when",
    "environment",
    "failed_when",
    "loop",
    "loop_control",
    "name",
    "no_log",
    "notify",
    "register",
    "rescue",
    "vars",
    "when",
    "tags",
    "delegate_to",
    "run_once",
    "until",
    "retries",
    "delay",
    "ignore_errors",
    "check_mode",
    "listen",
}


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _tasks(items: Any) -> Iterator[dict[str, Any]]:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        yield item
        for section in ("block", "rescue", "always", "tasks", "pre_tasks"):
            yield from _tasks(item.get(section))
        yield from _tasks(item.get("post_tasks"))
        yield from _tasks(item.get("handlers"))


def _module(task: dict[str, Any]) -> str:
    names = [key for key in task if key not in TASK_KEYWORDS]
    assert len(names) == 1, task
    return names[0].rsplit(".", 1)[-1]


def _converge_task_files() -> list[Path]:
    """Every task/handler/playbook YAML a real converge can execute."""
    files = sorted((ANSIBLE / "playbooks").glob("*.yml"))
    for role in sorted((ANSIBLE / "roles").iterdir()):
        for section in ("tasks", "handlers"):
            files.extend(sorted((role / section).glob("*.yml")))
    return files


def _text(task: dict[str, Any]) -> str:
    return yaml.safe_dump(task, sort_keys=True)


def test_platform_signer_wiring_ships_off_with_one_fixed_path() -> None:
    defaults = _load(PLATFORM_ROLE / "defaults/main.yml")
    template = (PLATFORM_ROLE / "templates/platform.env.j2").read_text()

    assert defaults["platform_coding_hosted_control_enabled"] is False
    assert defaults["platform_coding_hosted_signer_hotkey"] == ""
    # The seed path is not configurable: no role default, inventory or workflow
    # names a seed-path or guard variable. Only the entry point's import vars
    # and the synthetic fixture pass the guard its paths.
    signer_vars = {
        key for key in defaults if key.startswith("platform_coding_hosted_signer")
    }
    assert signer_vars == {"platform_coding_hosted_signer_hotkey"}
    for path in [
        *sorted(ANSIBLE.rglob("*.yml")),
        *sorted((ROOT / ".github/workflows").glob("*.yml")),
    ]:
        if path.is_relative_to(ANSIBLE / "tests") or path.name in {
            "coding_hosted_signer.yml",
            "coding_hosted_signer_seed_stat.yml",
        }:
            continue
        text = path.read_text()
        for name in ("platform_coding_hosted_signer_seed", *GUARD_VARS):
            assert name not in text, (path, name)

    block = template[template.index("# --- Hosted-v2 native control signer") :]
    block = block[: block.index("{% endif %}") + len("{% endif %}")]
    enabled, disabled = block.split("{% else %}")
    assert "{% if platform_coding_hosted_control_enabled | bool %}" in enabled
    assert [
        line for line in enabled.splitlines() if "=" in line and "#" not in line
    ] == [
        "DITTO_CODING_HOSTED_CONTROL_ENABLED=true",
        f"DITTO_CODING_HOSTED_SIGNER_SEED_FILE={SEED_FILE}",
        "DITTO_CODING_HOSTED_SIGNER_HOTKEY="
        "{{ platform_coding_hosted_signer_hotkey | quote }}",
    ]
    assert [line for line in disabled.splitlines() if "=" in line] == [
        "DITTO_CODING_HOSTED_CONTROL_ENABLED=false"
    ]
    # The rendered path is a literal, never an overridable variable, and no
    # secret material or seed variable feeds the block.
    assert "platform_coding_hosted_signer_seed" not in template
    assert "platform_secrets" not in block
    assert template.count("DITTO_CODING_HOSTED_") == 4


def test_no_converge_task_reads_creates_copies_or_hashes_the_seed() -> None:
    seen_stats = 0
    for path in _converge_task_files():
        for task in _tasks(_load(path)):
            if "hosts" in task or {"block", "rescue", "always"} & task.keys():
                continue
            text = _text(task)
            if not any(marker in text for marker in SEED_MARKERS):
                continue
            module = _module(task)
            assert module not in CONTENT_MODULES, (path, task)
            assert module in {"assert", "stat", "import_tasks", "debug"}, (path, task)
            if module == "stat":
                seen_stats += 1
                arguments = task[next(key for key in task if key.endswith("stat"))]
                assert arguments["follow"] is False, (path, task)
                assert arguments["get_checksum"] is False, (path, task)
                assert arguments["get_mime"] is False, (path, task)
                assert arguments["get_attributes"] is False, (path, task)
                assert "checksum_algorithm" not in arguments, (path, task)
                assert path.parent == PLATFORM_ROLE / "tasks", path
    assert seen_stats == 3

    stat_file = (PLATFORM_ROLE / "tasks/coding_hosted_signer_seed_stat.yml").read_text()
    for forbidden in ("slurp", "copy", "template", "fetch", "get_checksum: true"):
        assert forbidden not in stat_file


def test_disabled_platform_converge_never_reaches_the_seed_path() -> None:
    main = _load(PLATFORM_ROLE / "tasks/main.yml")
    names = [task.get("name", "") for task in main]
    references = [
        task
        for task in main
        if any(
            marker in _text(task)
            for marker in (
                *SEED_MARKERS,
                "coding_hosted_signer",
                "coding_hosted_control",
            )
        )
    ]
    # Static import, so `ansible-playbook --syntax-check gcp-platform-app.yml`
    # parses the whole guard; the condition skips every imported task.
    assert references == [
        {
            "name": "Verify the pre-placed hosted-v2 control signer without reading it",
            "ansible.builtin.import_tasks": "coding_hosted_signer.yml",
            "when": "platform_coding_hosted_control_enabled | bool",
        }
    ]
    # Runs straight after preflight and the fact naming the API user: before
    # any other platform_app task. The playbook's base role runs before this
    # role, so it is not "first on the host".
    include = names.index(references[0]["name"])
    assert names[include - 2 : include] == [
        "Validate env-sourced configuration before rendering anything",
        "Resolve the user that runs ditto-api",
    ]
    assert include < names.index("Render .env")
    playbook = _load(ANSIBLE / "playbooks/gcp-platform-app.yml")[0]
    assert playbook["roles"][:2] == ["base", "platform_app"]

    # The stat guard is reachable only through the profile guard.
    includers = {
        path.name
        for path in _converge_task_files()
        if "coding_hosted_signer_seed_stat.yml" in path.read_text()
    }
    assert includers == {"coding_hosted_signer.yml"}
    signer = _load(PLATFORM_ROLE / "tasks/coding_hosted_signer.yml")
    assert [_module(task) for task in signer] == [
        "assert",
        "import_tasks",
        "import_tasks",
    ]
    # Both imports run unconditionally inside the enabled guard: no `when`,
    # loop or tag may skip the stat guard or the live Docker check.
    for imported in signer[1:]:
        assert set(imported) == {"name", "ansible.builtin.import_tasks", "vars"}
    # The live Docker check runs after the stat guard with literal inputs.
    assert signer[2]["ansible.builtin.import_tasks"] == "deploy_docker_access.yml"
    assert signer[2]["vars"] == {
        "platform_deploy_docker_probe_group": "docker",
        "platform_deploy_docker_probe_proc": "/proc",
        "platform_deploy_docker_probe_socket": "/run/docker.sock",
    }
    docker = _load(PLATFORM_ROLE / "tasks/deploy_docker_access.yml")
    assert [_module(task) for task in docker] == ["script", "assert"]
    assert docker[0]["ansible.builtin.script"]["executable"] == "/usr/bin/python3"
    script = docker[0]["ansible.builtin.script"]["cmd"].split()[0]
    assert script == "../files/deploy-docker-access.py"
    assert (PLATFORM_ROLE / "tasks" / script).resolve() == (
        PLATFORM_ROLE / "files/deploy-docker-access.py"
    )
    assert docker[1]["ansible.builtin.assert"]["that"] == [
        "platform_deploy_docker_access.rc == 0"
    ]
    for name in (
        "platform_deploy_docker_probe_group",
        "platform_deploy_docker_probe_proc",
    ):
        for path in _converge_task_files():
            if path.name not in {
                "coding_hosted_signer.yml",
                "deploy_docker_access.yml",
            }:
                assert name not in path.read_text(), (path, name)
    profile = signer[0]["ansible.builtin.assert"]["that"]
    assert (
        f"platform_coding_hosted_signer_hotkey is match('{ANSIBLE_HOTKEY_PATTERN}')"
        in profile
    )
    assert "platform_coding_hosted_signer_hotkey | length == 48" in profile
    assert (
        "platform_coding_hosted_signer_hotkey != "
        "(platform_screener_hotkey | default('', true) | string | trim)"
    ) in profile
    # Only ditto-api may read the seed: the dedicated identity and the root
    # Pylon unit (deploy outside the docker group) are both required.
    assert {
        "platform_api_service_identity_enabled | bool",
        "platform_pylon_root_unit_enabled | bool",
        "platform_api_process_user == 'ditto-api'",
        "platform_owner not in ['root', 'ditto-api']",
    } <= set(profile)

    # The guard checks exactly the literal path platform.env.j2 renders.
    assert signer[1]["ansible.builtin.import_tasks"] == (
        "coding_hosted_signer_seed_stat.yml"
    )
    assert signer[1]["vars"] == GUARD_VARS
    template = (PLATFORM_ROLE / "templates/platform.env.j2").read_text()
    assert f"DITTO_CODING_HOSTED_SIGNER_SEED_FILE={SEED_FILE}\n" in template
    assert [str(parent) for parent in Path(SEED_DIRECTORY).parents] == SEED_ANCESTORS
    assert str(Path(SEED_FILE).parent) == SEED_DIRECTORY


def test_seed_guard_mirrors_the_platform_private_file_contract() -> None:
    guard = _load(PLATFORM_ROLE / "tasks/coding_hosted_signer_seed_stat.yml")
    stats = [
        task["ansible.builtin.stat"] for task in guard if "ansible.builtin.stat" in task
    ]
    assert [task.get("loop") for task in guard if "ansible.builtin.stat" in task][
        0
    ] == ("{{ platform_coding_hosted_signer_guard_ancestors }}")
    assert [arguments["path"] for arguments in stats] == [
        "{{ item }}",
        "{{ platform_coding_hosted_signer_guard_directory }}",
        "{{ platform_coding_hosted_signer_guard_seed }}",
    ]
    conditions = {
        task["name"]: task["ansible.builtin.assert"]["that"]
        for task in guard
        if "ansible.builtin.assert" in task
    }
    # Owner rules name the ditto-api process user, never platform_owner:
    # a deploy-owned directory, seed or ancestor fails the guard.
    assert conditions["Require safe hosted-v2 control signer seed ancestors"] == [
        "item.stat.exists",
        "item.stat.isdir",
        "not item.stat.islnk",
        "item.stat.pw_name | default('') in ['root', platform_api_process_user]",
        "not item.stat.wgrp",
        "not item.stat.woth",
    ]
    directory = "platform_coding_hosted_signer_directory_stat.stat"
    assert conditions["Require the ditto-api-owned 0700 seed directory"] == [
        f"{directory}.exists",
        f"{directory}.isdir",
        f"not {directory}.islnk",
        f"{directory}.pw_name | default('') == platform_api_process_user",
        f"{directory}.mode == '0700'",
    ]
    seed = "platform_coding_hosted_signer_seed_stat.stat"
    seed_guard = (
        "Require the pre-placed single-link 0600 32-byte seed owned by ditto-api"
    )
    assert conditions[seed_guard] == [
        f"{seed}.exists",
        f"{seed}.isreg",
        f"not {seed}.islnk",
        f"{seed}.pw_name | default('') == platform_api_process_user",
        f"{seed}.mode == '0600'",
        f"{seed}.nlink == 1",
        f"{seed}.size == 32",
    ]
    assert (
        "platform_owner"
        not in (PLATFORM_ROLE / "tasks/coding_hosted_signer_seed_stat.yml").read_text()
    )


def test_deploy_runs_pm2_and_ditto_api_runs_as_itself_when_isolated() -> None:
    defaults = _load(PLATFORM_ROLE / "defaults/main.yml")
    all_vars = _load(ANSIBLE / "group_vars/all.yml")
    main = (PLATFORM_ROLE / "tasks/main.yml").read_text()
    deploy = (ROOT / ".github/workflows/platform-deploy.yml").read_text()
    unit = (PLATFORM_ROLE / "templates/ditto-platform-api.service.j2").read_text()

    # deploy still owns pm2 (relays, cleanup, and ditto-api by default) and
    # runs update.sh; with the identity enabled ditto-api runs as ditto-api.
    assert defaults["platform_owner"] == "{{ deploy_user | default('deploy') }}"
    assert all_vars["deploy_user"] == "deploy"
    assert "pm2 startup systemd -u {{ platform_owner }}" in main
    assert "sudo -iu deploy bash -lc" in deploy
    assert "\nUser=ditto-api\nGroup=ditto-api\n" in unit
    assert "platform_owner" not in unit


def test_validator_trust_registration_is_default_off_and_distinct() -> None:
    defaults = _load(VALIDATOR_ROLE / "defaults/main.yml")
    template = (VALIDATOR_ROLE / "templates/validator.env.j2").read_text()
    main = _load(VALIDATOR_ROLE / "tasks/main.yml")
    trust = _load(VALIDATOR_ROLE / "tasks/coding_hosted_trust.yml")

    assert defaults["validator_stack_coding_hosted_control_enabled"] is False
    assert defaults["validator_stack_coding_hosted_platform_hotkey"] == ""
    assert (
        "VALIDATOR_CODING_HOSTED_CONTROL_ENABLED={{ 'true' if "
        "validator_stack_coding_hosted_control_enabled | bool else 'false' }}"
    ) in template
    assert (
        "VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY={{ "
        "validator_stack_coding_hosted_platform_hotkey if "
        "validator_stack_coding_hosted_control_enabled | bool else '' }}"
    ) in template

    assert len(trust) == 1
    assert trust[0]["when"] == "validator_stack_coding_hosted_control_enabled | bool"
    conditions = trust[0]["ansible.builtin.assert"]["that"]
    assert (
        "validator_stack_coding_hosted_platform_hotkey is match("
        f"'{ANSIBLE_HOTKEY_PATTERN}')"
    ) in conditions
    assert "validator_stack_coding_hosted_platform_hotkey | length == 48" in conditions
    # Both sides must be exact SS58 strings before they are compared: `match`
    # alone accepts a trailing newline, which would make equal keys unequal.
    comparison = conditions.index(
        "validator_stack_coding_hosted_platform_hotkey != validator_stack_hotkey"
    )
    assert comparison == len(conditions) - 1
    assert {
        "validator_stack_hotkey is string",
        "validator_stack_hotkey | length == 48",
        f"validator_stack_hotkey is match('{ANSIBLE_HOTKEY_PATTERN}')",
    } <= set(conditions[:comparison])

    names = [task.get("name", "") for task in main]
    guard = names.index(
        "Validate hosted-v2 Platform control signer trust before any other "
        "validator_stack task"
    )
    assert main[guard]["ansible.builtin.import_tasks"] == "coding_hosted_trust.yml"
    assert names[guard - 1] == "Validate production validator inputs"
    assert guard < names.index("Render the production validator environment")


def test_env_keys_and_hotkey_shape_match_the_control_command() -> None:
    template = (VALIDATOR_ROLE / "templates/validator.env.j2").read_text()
    compose = _load(ROOT / "docker-compose.yml")
    environment = compose["services"]["ditto-subnet"]["environment"]
    keys = {
        coding_hosted_control.ENABLED_ENV,
        coding_hosted_control.PLATFORM_HOTKEY_ENV,
    }

    rendered = set(re.findall(r"^(VALIDATOR_CODING_HOSTED_[A-Z_]+)=", template, re.M))
    passed = {key for key in environment if key.startswith("VALIDATOR_CODING_HOSTED_")}
    assert rendered == passed == keys
    synthetic = "5DtDLm5rQHShDqojQpsvcN8tRXHVFaecfDoRet1SU6BFD9Fi"
    assert re.fullmatch(ANSIBLE_HOTKEY_PATTERN.strip("^$"), synthetic)
    assert coding_hosted_control._HOTKEY.fullmatch(synthetic)


def test_compose_passes_hosted_control_trust_through_off_and_empty() -> None:
    compose = _load(ROOT / "docker-compose.yml")
    environment = compose["services"]["ditto-subnet"]["environment"]
    assert environment["VALIDATOR_CODING_HOSTED_CONTROL_ENABLED"] == (
        "${VALIDATOR_CODING_HOSTED_CONTROL_ENABLED:-false}"
    )
    assert environment["VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY"] == (
        "${VALIDATOR_CODING_HOSTED_PLATFORM_HOTKEY:-}"
    )
    for name, service in compose["services"].items():
        if name == "ditto-subnet":
            continue
        assert "VALIDATOR_CODING_HOSTED_" not in yaml.safe_dump(service), name


def test_only_the_control_command_reads_validator_trust() -> None:
    readers = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "ditto").rglob("*.py")
        if "VALIDATOR_CODING_HOSTED_" in path.read_text()
        and "tests" not in path.relative_to(ROOT).parts
    }
    assert readers == {"ditto/validator/coding_hosted_control.py"}


def _activations(node: Any) -> Iterator[tuple[str, Any]]:
    """Every activation flag assignment in parsed YAML, including inside strings."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ACTIVATION_FLAGS:
                yield key, value
            yield from _activations(value)
    elif isinstance(node, list):
        for item in node:
            yield from _activations(item)
    elif isinstance(node, str):
        # `-e flag=true`, `flag: true` in an inline document, or JSON extra vars.
        for flag in ACTIVATION_FLAGS:
            for match in re.finditer(
                rf"{flag}[\"']?\s*[:=]\s*[\"']?([^\s\"',}}]*)", node
            ):
                yield flag, match.group(1)


def _truthy(value: Any) -> bool:
    return value not in FALSY


def test_inventory_and_workflows_never_enable_the_signer_or_trust() -> None:
    """Staging a hotkey or setting a flag false is allowed; enabling is reviewed.

    Setting either activation flag truthy outside role defaults and synthetic
    fixtures fails unless the file is listed in REVIEWED_ACTIVATIONS by the same
    reviewed change (infra/docs/coding-hosted-control-signer-v2.md).
    """
    paths = [
        *sorted(ANSIBLE.rglob("*.yml")),
        *sorted(ANSIBLE.rglob("*.yaml")),
        *sorted((ROOT / ".github/workflows").glob("*.yml")),
    ]
    enabled: dict[str, set[str]] = {}
    for path in paths:
        if path.is_relative_to(ANSIBLE / "tests") or path.is_relative_to(
            ANSIBLE / "roles"
        ):
            continue
        for document in yaml.safe_load_all(path.read_text()):
            for flag, value in _activations(document):
                if _truthy(value):
                    enabled.setdefault(path.relative_to(ROOT).as_posix(), set()).add(
                        flag
                    )
    assert enabled == {path: set(flags) for path, flags in REVIEWED_ACTIVATIONS.items()}


def test_activation_guard_parses_values_instead_of_names() -> None:
    staged = yaml.safe_load(
        "platform_coding_hosted_control_enabled: false\n"
        "platform_coding_hosted_signer_hotkey: "
        "5DtDLm5rQHShDqojQpsvcN8tRXHVFaecfDoRet1SU6BFD9Fi\n"
        "validator_stack_coding_hosted_control_enabled: 'no'\n"
        "validator_stack_coding_hosted_platform_hotkey: ''\n"
    )
    assert not any(_truthy(value) for _, value in _activations(staged))
    for document in (
        {"platform_coding_hosted_control_enabled": True},
        {"validator_stack_coding_hosted_control_enabled": "yes"},
        {"platform_coding_hosted_control_enabled": "{{ lookup('env', 'X') }}"},
        {
            "steps": [
                {"run": "ansible-playbook -e platform_coding_hosted_control_enabled=1"}
            ]
        },
        {
            "run": "ansible-playbook -e "
            """'{"validator_stack_coding_hosted_control_enabled": true}'"""
        },
    ):
        assert any(_truthy(value) for _, value in _activations(document)), document


def test_infra_ci_runs_the_rendering_and_stat_guard_fixture() -> None:
    workflow = _load(ROOT / ".github/workflows/infra-ci.yml")
    steps = workflow["jobs"]["ansible"]["steps"]
    commands = [step.get("run", "") for step in steps]
    fixture = [
        command for command in commands if "coding-hosted-control-signer.yml" in command
    ]
    assert fixture == [
        "uvx --from ansible-core==2.21.2 ansible-playbook "
        "-i localhost, tests/coding-hosted-control-signer.yml"
    ]
    play = _load(ANSIBLE / "tests/coding-hosted-control-signer.yml")[0]
    assert play["hosts"] == "localhost"
    assert play["connection"] == "local"
    assert play["become"] is False
    assert "roles" not in play
    fixture_text = "\n".join(
        path.read_text()
        for path in sorted((ANSIBLE / "tests").glob("coding-hosted-control-*.yml"))
    )
    assert "become: true" not in fixture_text


def test_platform_ci_runs_the_loader_cross_checks_on_their_infra_inputs() -> None:
    """The Platform host-wiring test reads two infra files; infra-only edits to
    either must still run it. Every other infra assertion lives in this suite."""
    workflow = _load(ROOT / ".github/workflows/platform-ci.yml")
    triggers = workflow.get("on", workflow.get(True))
    paths = set(triggers["pull_request"]["paths"])
    platform_test = (
        ROOT / "apps/platform/ditto/tests/api_server/"
        "test_coding_hosted_signer_host_wiring.py"
    ).read_text()
    read = {
        "infra/ansible/roles/platform_app/templates/platform.env.j2",
        "infra/ansible/roles/platform_app/tasks/coding_hosted_signer.yml",
    }
    assert read <= paths
    assert '"templates" / "platform.env.j2"' in platform_test
    assert '"tasks" / "coding_hosted_signer.yml"' in platform_test
    assert platform_test.count('ROLE / "') == 2
    assert "defaults" not in platform_test


def test_ceremony_doc_keeps_key_custody_out_of_automation() -> None:
    doc = (ROOT / "infra/docs/coding-hosted-control-signer-v2.md").read_text()
    for required in (
        SEED_FILE,
        "protected ceremony",
        "curator",
        "hosted_control_configured",
        "Rotation",
        "Revocation",
        "Residual risks",
        "REVIEWED_ACTIVATIONS",
        "coding_hosted_signer_preflight --check-metadata",
        # Peyton's 2026-09-15 decision, stated in the doc it governs.
        "The only non-root identity that can open it",
        "completely separate from the offline curator Ed25519",
        "Neither seed, online or curator, may ever be placed in CI, Git, Telegram,\n"
        "workflow artifacts, command arguments or logs.",
        "owner `ditto-api`",
        "Immediate",
        "Reviewed activation",
        "the CI identity is\n  root on the host",
    ):
        assert required in doc, required
    # Root, CI and deploy tooling are named as residual paths; nothing claims
    # an absolute boundary against them.
    for claim in ("cannot read", "cannot reach", "can't read", "before anything"):
        assert claim not in doc, claim
    platform_doc = (
        ROOT / "apps/platform/docs/coding-hosted-control-startup-v2.md"
    ).read_text()
    assert "infra/docs/coding-hosted-control-signer-v2.md" in platform_doc
