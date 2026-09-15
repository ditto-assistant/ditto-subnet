"""Native PostgreSQL environment materialization stays default-off and silent."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/coding_hosted_postgres_environment"
TASKS = (ROLE / "tasks/main.yml").read_text()


PASSWORD_LOOKUP = "lookup('env', 'DITTO_CODING_PG_PASSWORD')"


def _block() -> list[dict]:
    return yaml.safe_load(TASKS)[1]["block"]


def _task(name: str) -> dict:
    return next(task for task in _block() if task["name"] == name)


def test_default_off_and_password_only_from_the_controller_environment() -> None:
    defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
    assert defaults == {
        "coding_hosted_postgres_environment_enabled": False,
        "coding_hosted_postgres_environment_confirmation": "",
        "coding_hosted_postgres_environment_source_revision": "",
        "coding_hosted_postgres_environment_host": "",
    }
    assert "MATERIALIZE NATIVE CODING POSTGRES ENVIRONMENT" in TASKS
    assert "ansible_facts['hostname'] == 'ditto-coding-hosted-v2'" in TASKS
    # No variable can carry the password; one supplied with -e is refused first.
    names = [task["name"] for task in _block()]
    refuse = "Refuse a password supplied as an Ansible variable"
    require = "Require one bounded single-line password from the controller environment"
    assert names.index(refuse) < names.index(require)
    assert _task(refuse)["ansible.builtin.assert"]["that"] == [
        "coding_hosted_postgres_environment_password is not defined"
    ]
    outer = yaml.safe_load(TASKS)[1]
    assert set(outer["vars"]) == {
        "coding_hosted_postgres_environment_entries",
        "coding_hosted_postgres_environment_document",
    }
    assert not any(
        "PASSWORD" in entry
        for entry in outer["vars"]["coding_hosted_postgres_environment_entries"]
    )
    assert (
        PASSWORD_LOOKUP in outer["vars"]["coding_hosted_postgres_environment_document"]
    )
    assert "coding_hosted_postgres_environment_password:" not in TASKS


def test_credentials_are_never_logged_read_back_or_diffed() -> None:
    write = _task(
        "Write each reader's own PostgreSQL environment copy without printing it"
    )
    assert write["no_log"] is True and write["diff"] is False
    for forbidden in (
        "slurp",
        "fetch",
        "set -x",
        "gcloud secrets",
        "extra_vars",
    ):
        assert forbidden not in TASKS
    report = _task("Report only that the copies exist")["ansible.builtin.debug"]["msg"]
    assert "password" not in report.lower()
    assert "{{" not in report
    secret_uses = [
        task["name"]
        for task in _block()
        if PASSWORD_LOOKUP in yaml.safe_dump(task, width=10_000)
        or "coding_hosted_postgres_environment_document" in yaml.safe_dump(task)
        or "coding_hosted_postgres_environment_files" in yaml.safe_dump(task)
    ]
    assert secret_uses == [
        "Require one bounded single-line password from the controller environment",
        "Write each reader's own PostgreSQL environment copy without printing it",
        "Reinspect both copies as ownership, mode and digest metadata only",
        "Require owner-only regular single-link copies",
        "Require both copies to hold exactly the rendered document",
    ]
    for name in secret_uses:
        task = _task(name)
        # The owner/mode assert prints only the path label and never the digest.
        if name == "Require owner-only regular single-link copies":
            assert "checksum" not in yaml.safe_dump(task)
            assert task["loop_control"]["label"] == "{{ item.item }}"
            continue
        assert task.get("no_log") is True, name


def test_copies_are_verified_by_owner_mode_and_digest_only() -> None:
    stat = _task("Reinspect both copies as ownership, mode and digest metadata only")
    assert stat["ansible.builtin.stat"] == {
        "path": "{{ item }}",
        "follow": False,
        "get_checksum": True,
        "checksum_algorithm": "sha256",
        "get_mime": False,
    }
    digest = _task("Require both copies to hold exactly the rendered document")
    assert digest["ansible.builtin.assert"]["that"] == [
        "item.stat.checksum == coding_hosted_postgres_environment_document"
        " | hash('sha256')"
    ]
    assert digest["loop"] == "{{ coding_hosted_postgres_environment_files.results }}"
    sealed = _task("Require owner-only regular single-link copies")
    that = sealed["ansible.builtin.assert"]["that"]
    assert "item.stat.mode == '0600'" in that
    assert "item.stat.nlink == 1" in that
    assert "not item.stat.islnk" in that


def test_each_reader_gets_its_own_owner_only_copy_for_the_admitted_principal() -> None:
    write = _task(
        "Write each reader's own PostgreSQL environment copy without printing it"
    )
    assert write["ansible.builtin.copy"]["mode"] == "0600"
    assert write["loop"] == [
        {
            "path": "/var/lib/ditto-coding-custody/private/postgres-environment.json",
            "owner": "ditto-coding-custody",
        },
        {
            "path": "/var/lib/ditto-coding-hosted/private/postgres-environment.json",
            "owner": "ditto-coding-hosted",
        },
    ]
    assert write["ansible.builtin.copy"]["content"] == (
        "{{ coding_hosted_postgres_environment_document }}"
    )
    entries = yaml.safe_load(TASKS)[1]["vars"][
        "coding_hosted_postgres_environment_entries"
    ]
    assert "POSTGRES_USER=ditto" in entries
    assert "POSTGRES_DB=ditto_platform_prod" in entries
    directories = _task("Create owner-only credential directories for each reader")
    assert directories["ansible.builtin.file"]["mode"] == "0700"
    assert {item["owner"] for item in directories["loop"]} == {
        "ditto-coding-custody",
        "ditto-coding-hosted",
    }
    refuse = _task(
        "Refuse to replace credentials under a live worker or custody instance"
    )
    assert "active|activating|deactivating|reloading" in yaml.safe_dump(refuse)
    listing = _task("List live worker and custody units")["ansible.builtin.command"]
    assert "ditto-coding-hosted-worker.service" in listing["argv"]
    assert "ditto-coding-custody@*.service" in listing["argv"]


def test_playbook_group_connection_and_ci_registration() -> None:
    (play,) = yaml.safe_load(
        (
            ROOT / "infra/ansible/playbooks/gcp-coding-hosted-postgres-environment.yml"
        ).read_text()
    )
    assert play["hosts"] == "role_coding_hosted"
    assert play["roles"] == ["coding_hosted_postgres_environment"]
    group = yaml.safe_load(
        (ROOT / "infra/ansible/group_vars/role_coding_hosted.yml").read_text()
    )
    assert set(group) == {
        "gcp_project",
        "gcp_region",
        "gcp_zone",
        "ansible_user",
        "ansible_ssh_common_args",
    }
    assert "gcloud compute start-iap-tunnel" in group["ansible_ssh_common_args"]
    workflow = (ROOT / ".github/workflows/infra-ci.yml").read_text()
    assert "playbooks/gcp-coding-hosted-postgres-environment.yml" in workflow
    assert "tests/coding-hosted-postgres-environment.yml" in workflow
