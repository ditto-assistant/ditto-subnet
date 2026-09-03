from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow[True])


def test_manual_platform_deploy_requires_release_tag_or_force() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/platform-deploy.yml").read_text()
    )
    dispatch = _triggers(workflow)["workflow_dispatch"]["inputs"]
    contract = workflow["jobs"]["changes"]["steps"][1]["run"]

    assert dispatch["force"]["default"] is False
    assert "git tag --points-at" in contract
    assert '"$FORCE_DEPLOY" != true' in contract
    assert "[0-9]+$'" in contract


def test_manual_screener_deploy_requires_release_tag_or_force() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/screener-deploy.yml").read_text()
    )
    dispatch = _triggers(workflow)["workflow_dispatch"]["inputs"]
    resolve = next(
        step
        for step in workflow["jobs"]["discover"]["steps"]
        if step.get("name") == "Resolve the immutable released revision"
    )

    assert dispatch["force"]["default"] is False
    assert "git tag --points-at" in resolve["run"]
    assert '"$FORCE_DEPLOY" != true' in resolve["run"]
    assert "[0-9]+$'" in resolve["run"]


def _platform_deploy_command() -> str:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/platform-deploy.yml").read_text()
    )
    step = next(
        step
        for step in workflow["jobs"]["deploy"]["steps"]
        if step.get("id") == "deploy"
    )
    return step["run"]


def _platform_checkout_root() -> str:
    defaults = yaml.safe_load(
        (ROOT / "infra/ansible/roles/platform_app/defaults/main.yml").read_text()
    )
    return defaults["platform_checkout_root"]


def test_platform_deploy_targets_the_ansible_provisioned_checkout() -> None:
    """The deploy path and the path Ansible provisions are one fact, not two.

    They drifted once already: the workflow shipped pointing at a directory the
    prod host did not have, so every `apps/platform` change merged here looked
    deployed and reached nothing. Pinning them together means a future move of
    `platform_checkout_root` fails here instead of in production.
    """
    root = _platform_checkout_root()
    assert root.startswith("/")
    assert f"cd {root} &&" in _platform_deploy_command()


def test_platform_deploy_preflights_the_checkout_before_touching_the_host() -> None:
    """An unconverged host must be diagnosable from the failure alone.

    A bare `cd` into a missing directory reports only that the directory is
    missing -- not that the host was never provisioned, not that it is still
    serving the pre-cutover checkout, and not which playbook fixes it.
    """
    command = _platform_deploy_command()
    root = _platform_checkout_root()

    # The guard runs BEFORE the cd it protects, or it protects nothing.
    assert command.index(f"[ ! -d {root}/.git ]") < command.index(f"cd {root} &&")
    # Names the pre-cutover checkout, so "merged but not live" is self-evident.
    assert "/opt/ditto-platform/.git" in command
    # Names the fix, not just the symptom.
    assert "gcp-platform-app.yml" in command
    # EX_CONFIG: a provisioning gap is not a deploy fault.
    assert "exit 78" in command


def test_platform_deploy_rejects_a_half_provisioned_checkout() -> None:
    """A cloned checkout with no rendered .env must not read as deployable.

    A converge that aborts between cloning and rendering leaves `.git` present
    and `.env` absent. Guarding only on `.git` waves that straight through into
    an app that cannot boot, and unlike "never provisioned" it looks ready.
    """
    command = _platform_deploy_command()
    root = _platform_checkout_root()

    assert f"[ ! -s {root}/apps/platform/.env ]" in command
    # -s, not -f: a zero-byte .env is as unbootable as a missing one.
    assert f"[ ! -f {root}/apps/platform/.env ]" not in command
    # Both guards run before the cd they protect.
    assert command.index(f"[ ! -s {root}/apps/platform/.env ]") < command.index(
        f"cd {root} &&"
    )
    assert "HALF provisioned" in command


def test_private_coding_catalog_deploy_is_default_off_and_relay_blind() -> None:
    """Catalog credentials stay with the Python API, not the shared relay pool."""
    defaults_path = ROOT / "infra/ansible/roles/platform_app/defaults/main.yml"
    defaults = yaml.safe_load(defaults_path.read_text())
    template = (
        ROOT / "infra/ansible/roles/platform_app/templates/platform.env.j2"
    ).read_text()
    tasks = (ROOT / "infra/ansible/roles/platform_app/tasks/main.yml").read_text()
    terraform = (ROOT / "infra/terraform/stacks/gcp-platform/main.tf").read_text()
    ecosystem = (ROOT / "apps/platform/scripts/ecosystem.config.js").read_text()

    assert defaults["platform_coding_catalog_enabled"] is False
    assert "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY=" in template
    assert "platform_secrets.coding_catalog_secret_key | quote" in template
    assert "platform_coding_catalog_use_tls | bool" in template
    assert "platform_coding_catalog_enabled | bool" in tasks
    assert "platform_coding_catalog_bucket != platform_bucket" in tasks
    assert "platform_coding_catalog_bucket != (platform_hippius_bucket" in tasks
    assert "secret_coding_catalog_access_key != secret_hippius_access_key_id" in tasks
    assert (
        "platform_coding_catalog_access_key_read.stdout | default('') | length > 0"
        in tasks
    )
    assert (
        "platform_coding_catalog_access_key_read.stdout != "
        "(platform_secrets.hippius_access_key_id | default(''))" in tasks
    )
    for secret in (
        "platform-coding-catalog-access-key",
        "platform-coding-catalog-secret-key",
    ):
        assert secret in terraform
    for curator_secret in (
        "platform-coding-catalog-curator-access-key",
        "platform-coding-catalog-curator-secret-key",
    ):
        assert curator_secret in terraform
    runtime_secret_locals = terraform[: terraform.index("# --- Network:")]
    assert "coding_catalog_curator_access_key.secret_id" not in runtime_secret_locals
    assert "coding_catalog_curator_secret_key.secret_id" not in runtime_secret_locals
    for key in (
        "DITTO_CODING_CATALOG_STORAGE_ACCESS_KEY",
        "DITTO_CODING_CATALOG_STORAGE_SECRET_KEY",
    ):
        assert f'{key}: ""' in ecosystem


def test_shadow_coding_deploy_controls_are_default_off_and_split_by_process() -> None:
    """A reviewed host profile cannot enable coding by accident or leak control.

    The Python API owns grant minting and confirmed admin actions. The Go relay
    needs only its narrow dispatch gate plus the provider credential; inheriting
    any Platform-side catalog, admin, or exchange authority would widen that
    process unnecessarily.
    """
    defaults = yaml.safe_load(
        (ROOT / "infra/ansible/roles/platform_app/defaults/main.yml").read_text()
    )
    template = (
        ROOT / "infra/ansible/roles/platform_app/templates/platform.env.j2"
    ).read_text()
    tasks = (ROOT / "infra/ansible/roles/platform_app/tasks/main.yml").read_text()
    ecosystem = (ROOT / "apps/platform/scripts/ecosystem.config.js").read_text()
    relay_release = (ROOT / "apps/platform/scripts/deploy-relay-release.sh").read_text()

    for key in (
        "platform_coding_shadow_enabled",
        "platform_coding_shadow_reconciliation_enabled",
        "platform_coding_shadow_ticket_set_enabled",
    ):
        assert defaults[key] is False
    assert defaults["platform_coding_shadow_inference_policy_sha256"] == (
        "6dd79225817b56ebf155f8344cd5faf752c8dd57802b21d6d2cbbae9cc2ff0b4"
    )
    assert "DITTO_CODING_SHADOW_ENABLED={{ 'true'" in template
    assert "DITTO_CODING_INFERENCE_ENABLED={{ 'true'" in template
    assert (
        "DITTO_CODING_INFERENCE_ACCOUNT_GUARDRAIL=openrouter_private_account_v1"
        in template
    )
    assert "platform_coding_shadow_inference_policy_file | quote" in template
    assert "platform_coding_shadow_reconciliation_enabled else 'false'" in template
    assert "platform_coding_shadow_ticket_set_enabled else 'false'" in template
    assert "platform_inference_relay_ports | sort == [8010, 8011]" in tasks
    assert "platform_coding_shadow_policy.stat.checksum" in tasks
    assert "platform_secrets.openrouter_api_key" in tasks
    assert "platform_coding_catalog_enabled | bool" in tasks
    for key in (
        "DITTO_ADMIN_API_TOKEN",
        "DITTO_CODING_SHADOW_ENABLED",
        "DITTO_CODING_SHADOW_RECONCILIATION_ENABLED",
        "DITTO_CODING_SHADOW_TICKET_SET_ENABLED",
        "DITTO_CODING_INFERENCE_POLICY_FILE",
        "DITTO_CODING_INFERENCE_EXCHANGE_URL",
        "DITTO_CODING_INFERENCE_PROXY_URL",
        "DITTO_CODING_INFERENCE_REVOKE_URL",
    ):
        assert f"{key}: " in ecosystem
        assert f"export {key}=" in relay_release
    assert (
        "const relayKillTimeout = codingInferenceEnabled ? 315000 : 135000;"
        in ecosystem
    )
    assert "kill_timeout: relayKillTimeout" in ecosystem


def test_hippius_coding_evidence_custody_is_default_off_and_relay_blind() -> None:
    defaults_path = ROOT / "infra/ansible/roles/platform_app/defaults/main.yml"
    defaults = yaml.safe_load(defaults_path.read_text())
    template = (
        ROOT / "infra/ansible/roles/platform_app/templates/platform.env.j2"
    ).read_text()
    tasks = (ROOT / "infra/ansible/roles/platform_app/tasks/main.yml").read_text()
    terraform = (ROOT / "infra/terraform/stacks/gcp-platform/main.tf").read_text()
    ecosystem = (ROOT / "apps/platform/scripts/ecosystem.config.js").read_text()

    assert defaults["platform_coding_hippius_evidence_enabled"] is False
    assert "DITTO_CODING_HIPPIUS_EVIDENCE_ENABLED=false" in template
    assert "platform_secrets.coding_hippius_evidence_secret_key | quote" in template
    assert "platform_coding_hippius_evidence_enabled | bool" in tasks
    assert "platform_coding_hippius_evidence_bucket != platform_bucket" in tasks
    assert (
        "platform_coding_hippius_evidence_bucket != (platform_coding_catalog_bucket"
        in tasks
    )
    assert (
        "secret_coding_hippius_evidence_access_key != secret_hippius_access_key_id"
        in tasks
    )
    assert "platform_coding_hippius_evidence_spool_root" in tasks
    assert 'mode: "0700"' in tasks
    for secret in (
        "platform-coding-hippius-evidence-access-key",
        "platform-coding-hippius-evidence-secret-key",
    ):
        assert secret in terraform
    for key in (
        "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_ACCESS_KEY",
        "DITTO_CODING_HIPPIUS_EVIDENCE_MEDIATOR_SECRET_KEY",
        "DITTO_CODING_HIPPIUS_PROBE_RECEIPT_PATH",
        "DITTO_CODING_HIPPIUS_EVIDENCE_SPOOL_ROOT",
        "DITTO_CODING_HIPPIUS_EVIDENCE_WRAPPING_PUBLIC_KEY_PATH",
    ):
        assert f'{key}: ""' in ecosystem


def test_hippius_canary_helpers_are_default_off_identity_bound_and_relay_blind() -> (
    None
):
    defaults = yaml.safe_load(
        (ROOT / "infra/ansible/roles/platform_app/defaults/main.yml").read_text()
    )
    tasks = (ROOT / "infra/ansible/roles/platform_app/tasks/main.yml").read_text()
    operator_template = (
        ROOT / "infra/ansible/roles/platform_app/templates/"
        "coding-hippius-canary-operator.env.j2"
    ).read_text()
    platform_template = (
        ROOT / "infra/ansible/roles/platform_app/templates/platform.env.j2"
    ).read_text()
    ecosystem = (ROOT / "apps/platform/scripts/ecosystem.config.js").read_text()
    proxy = (ROOT / "apps/platform/scripts/hippius_canary_helper_proxy.py").read_text()
    updater = (ROOT / "apps/platform/scripts/update.sh").read_text()

    assert defaults["platform_coding_hippius_canary_helpers_enabled"] is False
    assert "platform_coding_hippius_canary_helpers_enabled | bool" in tasks
    assert (
        "platform_coding_hippius_canary_config_root == "
        "'/etc/ditto-platform/coding/hippius-canary'" in tasks
    )
    assert "item.stat.issock" in tasks
    assert "item.stat.uid | int == item.item.uid | int" in tasks
    assert "item.stat.mode == '0660'" in tasks
    assert 'mode: "0550"' in tasks
    assert "state: absent" in tasks
    assert "DITTO_CODING_HIPPIUS_CANARY_ENABLED=true" in operator_template
    assert "platform_secrets.coding_catalog_secret_key" in operator_template
    assert "DITTO_CODING_HIPPIUS_CANARY_ENABLED" not in platform_template
    for key in (
        "DITTO_CODING_HIPPIUS_CANARY_ENABLED",
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_ACCESS_KEY",
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_READER_SECRET_KEY",
    ):
        assert f'{key}: ""' in ecosystem or f'{key}: "false"' in ecosystem
    assert "socket.SO_PEERCRED" in proxy
    assert "socket.AF_UNIX" in proxy
    assert 'deployed_source_file="logs/deployed-source.sha"' in updater
    assert 'chmod 0600 "$next_deployed_source"' in updater


def test_hippius_canary_unwrap_service_is_default_off_and_socket_activated() -> None:
    defaults = yaml.safe_load(
        (ROOT / "infra/ansible/roles/platform_app/defaults/main.yml").read_text()
    )
    tasks = (ROOT / "infra/ansible/roles/platform_app/tasks/main.yml").read_text()
    service_unit = (
        ROOT / "infra/ansible/roles/platform_app/templates/"
        "ditto-hippius-canary-unwrap.service.j2"
    ).read_text()
    socket_unit = (
        ROOT / "infra/ansible/roles/platform_app/templates/"
        "ditto-hippius-canary-unwrap.socket.j2"
    ).read_text()
    service_env = (
        ROOT / "infra/ansible/roles/platform_app/templates/"
        "hippius-canary-unwrap-service.env.j2"
    ).read_text()
    service = (
        ROOT / "apps/platform/scripts/hippius_canary_unwrap_service.py"
    ).read_text()

    assert defaults["platform_coding_hippius_canary_unwrap_installed"] is False
    assert defaults["platform_coding_hippius_canary_unwrap_service_enabled"] is False
    assert "platform_coding_hippius_canary_unwrap_user != platform_owner" in tasks
    assert "item.stat.mode == '0400'" in tasks
    assert "item.stat.nlink == 1" in tasks
    assert "platform_coding_hippius_canary_unwrap_service_enabled | bool" in tasks
    assert "PrivateNetwork=true" in service_unit
    assert "RestrictAddressFamilies=AF_UNIX" in service_unit
    assert "MemoryDenyWriteExecute=true" in service_unit
    assert "SocketMode=0660" in socket_unit
    assert "SocketGroup={{ platform_group }}" in socket_unit
    assert "DITTO_HIPPIUS_CANARY_UNWRAP_REQUIRE_SOCKET_ACTIVATION=true" in service_env
    for forbidden in ("ACCESS_KEY", "SECRET_KEY", "STORAGE_BUCKET", "PRIVATE_KEY="):
        assert forbidden not in service_env
    assert "rsa_padding_mode:oaep" in service
    assert "rsa_oaep_md:sha256" in service
    assert "rsa_mgf1_md:sha256" in service
    assert "dittobench-coding-hippius-private-input-unwrap-v1" in service
    assert "len(self._responses) >= 2" in service
