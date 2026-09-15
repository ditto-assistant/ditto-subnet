"""Static guards: the certification service role only renders review files."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_certification_service"
ALLOWED_MODULES = {
    "ansible.builtin.meta",
    "ansible.builtin.include_tasks",
    "ansible.builtin.assert",
    "ansible.builtin.file",
    "ansible.builtin.template",
}
WRITING_MODULES = {"ansible.builtin.file", "ansible.builtin.template"}
TASK_KEYS = {
    "name",
    "vars",
    "when",
    "loop",
    "loop_control",
    "delegate_to",
    "become",
    "register",
}


def _tasks() -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for path in sorted((ROLE / "tasks").glob("*.yml")):
        tasks.extend(yaml.safe_load(path.read_text()) or [])
    return tasks


def test_role_uses_only_controller_local_render_modules() -> None:
    tasks = _tasks()
    assert tasks
    for task in tasks:
        modules = set(task) - TASK_KEYS
        assert len(modules) == 1, task
        (module,) = modules
        assert module in ALLOWED_MODULES, module
        if module in WRITING_MODULES:
            assert task.get("delegate_to") == "localhost", task["name"]
            assert task.get("become") is False, task["name"]


def test_role_is_default_off_and_ends_before_any_write() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults["coding_certification_service_render_enabled"] is False
    assert defaults["coding_certification_service_render_root"] == ""
    main = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    first = main[0]
    assert first["ansible.builtin.meta"] == "end_role"
    assert "is sameas true" in first["when"]


def test_rendered_units_never_install_start_or_carry_secrets() -> None:
    text = "\n".join(
        path.read_text() for path in sorted((ROLE / "templates").glob("*.j2"))
    )
    assert "[Install]" not in text
    assert "WantedBy" not in text
    assert "systemctl" not in text
    assert "Restart=no" in text
    assert "DITTOBENCH_CODING_CERTIFICATION_SERVICE_ENABLED=false" in text
    for forbidden in (
        "DITTOBENCH_SANDBOX_",
        "DOCKER_HOST=",
        "HTTPS_PROXY",
        "SSL_CERT_FILE",
        "CA_BUNDLE",
        "tcp://",
        "vault_",
        "lookup(",
    ):
        assert forbidden not in text, forbidden
    # The bearer is referenced by path only.
    assert "LoadCredential=control-token:/etc/ditto-coding-certification/" in text


def test_no_playbook_applies_the_role() -> None:
    for path in (ROOT / "infra/ansible/playbooks").glob("*.yml"):
        assert "coding_certification_service" not in path.read_text(), path.name
