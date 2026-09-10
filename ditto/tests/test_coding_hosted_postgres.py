"""Network/guest configuration contracts; no live database or cloud writes."""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def read(path):
    return (ROOT / path).read_text()


def test_private_database_path_requires_explicit_production_intent():
    module = read("infra/terraform/modules/coding-hosted-host/postgres.tf")
    stack = read("infra/terraform/stacks/gcp-platform/coding-hosted.tf")
    intent = read("infra/terraform/stacks/gcp-platform/prod.auto.tfvars")
    assert 'variable "postgres_peer"' in module and "default = null" in module
    assert 'variable "enable_coding_hosted_postgres"' in stack
    assert (
        "!var.enable_coding_hosted_postgres || var.enable_coding_hosted_host" in stack
    )
    assert any(
        line.split("=") == ["enable_coding_hosted_postgres", "true"]
        for line in ("".join(line.split()) for line in intent.splitlines())
    )
    assert "module.pg_vm.internal_ip" in stack
    assert "module.network.network_self_link" in stack
    assert 'source_ranges = ["${module.host[0].internal_ip}/32"]' in module
    assert 'ports    = ["5432"]' in module
    assert "database_login_ready = false" in module


def test_guest_gate_admits_only_the_reviewed_host_and_creates_no_credentials():
    config = yaml.safe_load(read("infra/ansible/group_vars/role_platform_postgres.yml"))
    assert config["coding_hosted_postgres_enabled"] is True
    assert config["coding_hosted_postgres_client_ip"] == "10.33.0.2"
    assert "ditto_platform_prod" in config["postgres_hba_hosts"]
    assert "scram-sha-256" in config["postgres_hba_hosts"]
    assert (
        "coding_hosted_postgres_enabled | bool else []" in config["postgres_hba_hosts"]
    )
    assert "coding_hosted_postgres_client_ip + '/32'" in config["base_ufw_extra_rules"]


def test_admission_guard_precedes_base_role_and_has_role_level_backstop():
    play = yaml.safe_load(read("infra/ansible/playbooks/gcp-platform-pg.yml"))[0]
    assert (
        "coding-hosted-access.yml"
        in play["pre_tasks"][0]["ansible.builtin.import_tasks"]
    )
    password_stat = play["pre_tasks"][1]
    assert password_stat["ansible.builtin.stat"]["path"] == (
        "/opt/ditto/secrets/postgres-ditto.password"
    )
    assert password_stat["ansible.builtin.stat"]["follow"] is False
    assert password_stat["ansible.builtin.stat"]["get_checksum"] is False
    password_guard = play["pre_tasks"][2]["ansible.builtin.assert"]
    password_condition = "\n".join(password_guard["that"])
    assert "DITTO_PG_PASSWORD" in password_condition
    assert "platform_pg_password_file.stat.isreg" in password_condition
    assert "platform_pg_password_file.stat.pw_name" in password_condition
    assert "platform_pg_password_file.stat.gr_name" in password_condition
    assert "platform_pg_password_file.stat.mode" in password_condition
    assert "platform_pg_password_file.stat.nlink" in password_condition
    assert "platform_pg_password_file.stat.size" in password_condition
    assert "slurp" not in str(play["pre_tasks"])
    tasks = yaml.safe_load(read("infra/ansible/roles/postgres/tasks/main.yml"))
    assert tasks[0]["ansible.builtin.import_tasks"] == "coding-hosted-access.yml"
    guard = yaml.safe_load(
        read("infra/ansible/roles/postgres/tasks/coding-hosted-access.yml")
    )[0]
    checks = guard["ansible.builtin.assert"]["that"]
    assert "inventory_hostname == 'ditto-pg-platform'" in checks
    assert (
        "coding_hosted_postgres_client_ip | trim == coding_hosted_postgres_client_ip"
        in checks
    )
    assert "postgres_app_user | default('ditto') == 'ditto'" in checks


def test_hba_changes_reload_while_restart_only_settings_keep_restart():
    tasks = yaml.safe_load(read("infra/ansible/roles/postgres/tasks/main.yml"))
    by_name = {task["name"]: task for task in tasks}
    assert by_name["Render pg_hba.conf"]["notify"] == "reload postgresql"
    assert (
        by_name["Render tuning drop-in (idempotent — Ansible-managed block)"]["notify"]
        == "restart postgresql"
    )
    handlers = yaml.safe_load(read("infra/ansible/roles/postgres/handlers/main.yml"))
    assert handlers[0]["ansible.builtin.systemd"]["state"] == "reloaded"


def test_narrow_guest_admission_changes_only_ufw_hba_and_reload():
    play = yaml.safe_load(
        read("infra/ansible/playbooks/gcp-coding-hosted-postgres-admission.yml")
    )[0]
    assert play["hosts"] == "role_platform_postgres"
    assert play["become"] is True and play["gather_facts"] is False
    assert "roles" not in play
    include = play["tasks"][0]["ansible.builtin.include_role"]
    assert include == {
        "name": "postgres",
        "tasks_from": "coding-hosted-converge.yml",
    }
    tasks = yaml.safe_load(
        read("infra/ansible/roles/postgres/tasks/coding-hosted-converge.yml")
    )
    rendered = str(tasks)
    assert tasks[0]["ansible.builtin.import_tasks"] == "coding-hosted-access.yml"
    assert "community.general.ufw" in rendered
    assert "10.33.0.2" in rendered and "ditto_platform_prod" in rendered
    assert "pg_hba_file_rules" in rendered and "scram-sha-256" in rendered
    assert "flush_handlers" in rendered and "pg_isready" in rendered
    assert rendered.count("not ansible_check_mode") == 4
    for forbidden in (
        "postgres_app_password",
        "ansible.builtin.slurp",
        "ansible.builtin.apt",
        "roles/base",
    ):
        assert forbidden not in rendered


def test_full_and_narrow_convergence_share_one_hba_template():
    tasks = yaml.safe_load(read("infra/ansible/roles/postgres/tasks/main.yml"))
    by_name = {task["name"]: task for task in tasks}
    render = by_name["Render pg_hba.conf"]["ansible.builtin.template"]
    assert render["src"] == "pg_hba.conf.j2"
    template = read("infra/ansible/roles/postgres/templates/pg_hba.conf.j2")
    assert "postgres_hba_hosts" in template
    assert "scram-sha-256" in template


def test_real_ansible_fixture_is_check_only_and_never_converges_roles():
    play = yaml.safe_load(read("infra/ansible/tests/coding-hosted-postgres.yml"))[0]
    assert play["connection"] == "local" and play["become"] is False
    assert play["gather_facts"] is False and "roles" not in play
    workflow = read(".github/workflows/infra-ci.yml")
    assert "ansible-playbook --check" in workflow
    assert "tests/coding-hosted-postgres-inventory.yml" in workflow
    assert "playbooks/gcp-coding-hosted-postgres-admission.yml" in workflow
