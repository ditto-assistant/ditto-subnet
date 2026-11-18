"""Contracts that keep the CPM scorer deploy path inert until activated.

The GCP bearer and federation, the Ansible role, the Platform env wiring and the
release image name the same secret, pool, role and channel in several places.
These checks fail when one drifts. The AWS side (aws-bootstrap's
ditto-cpm-scorer role and the aws-cpm-scorer root) carries the same role name
and is checked by its own tests.
"""

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
GCP = REPO / "infra" / "terraform" / "stacks" / "gcp-platform"
CPM_TF = (GCP / "cpm-scorer.tf").read_text()
X509_TF = (GCP / "screener-fleet-x509.tf").read_text()
GCP_TFVARS = (GCP / "prod.auto.tfvars").read_text()
ANSIBLE = REPO / "infra" / "ansible"
ROLE = ANSIBLE / "roles" / "cpm_scorer"
DEFAULTS = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
UNIT = (ROLE / "templates" / "ditto-cpm-scorer.service.j2").read_text()
PLATFORM_ROLE = ANSIBLE / "roles" / "platform_app"
DOCKERFILE = (REPO / "Dockerfile.cpm-scorer-release").read_text()
UPDATER = (REPO / "scripts" / "cpm-scorer-auto-update.sh").read_text()
SECRET = "model-context-scorer-token-prod"
ROLE_NAME = "ditto-cpm-scorer"


def _search(pattern: str, text: str, flags: int = 0) -> re.Match[str]:
    match = re.search(pattern, text, flags)
    assert match, pattern
    return match


def _default(text: str, variable: str) -> str:
    block = _search(rf'variable "{variable}" \{{(.*?)\n\}}', text, re.S).group(1)
    return _search(r"default\s*=\s*(\S+)", block).group(1).strip('"')


def test_gcp_side_defaults_to_creating_nothing() -> None:
    assert _default(CPM_TF, "enable_model_context_scorer_secret") == "false"
    assert _default(CPM_TF, "cpm_scorer_aws_account_id") == ""
    # The reviewed production inputs leave both halves off, explicitly.
    assert re.findall(
        r"^enable_model_context_scorer_secret\s*=\s*(\S+)", GCP_TFVARS, re.M
    ) == ["false"]
    assert not re.search(r"^cpm_scorer_aws_account_id\s*=", GCP_TFVARS, re.M)
    for resource in (
        'resource "google_secret_manager_secret" "model_context_scorer_token"',
        'resource "google_secret_manager_secret_iam_member" '
        '"model_context_scorer_token_platform_access"',
        'resource "google_iam_workload_identity_pool" "cpm_scorer_aws"',
        'resource "google_iam_workload_identity_pool_provider" "cpm_scorer_aws"',
        'resource "google_secret_manager_secret_iam_member" '
        '"model_context_scorer_token_aws_access"',
    ):
        block = _search(re.escape(resource) + r" \{(.*?)\n\}", CPM_TF, re.S).group(1)
        assert re.search(r"^\s*count\s*=\s*local\.", block, re.M), resource


def test_one_container_and_no_value_in_terraform() -> None:
    assert re.findall(r'secret_id = "([^"]+)"', CPM_TF) == [SECRET]
    # Values are added out of band and never enter state.
    assert "google_secret_manager_secret_version" not in CPM_TF
    assert "prevent_destroy = true" in CPM_TF


def test_federation_reads_exactly_one_secret_for_one_role() -> None:
    assert _default(CPM_TF, "cpm_scorer_aws_role_name") == ROLE_NAME
    assert (
        "assertion.arn.startsWith('arn:aws:sts::${var.cpm_scorer_aws_account_id}"
        ":assumed-role/${var.cpm_scorer_aws_role_name}/')" in CPM_TF
    )
    grants = re.findall(r"principalSet://[^\"]+", CPM_TF)
    assert len(grants) == 1
    assert grants[0].endswith("/attribute.aws_role/${var.cpm_scorer_aws_role_name}")
    block = _search(
        r'"model_context_scorer_token_aws_access" \{(.*?)\n\}', CPM_TF, re.S
    ).group(1)
    assert "google_secret_manager_secret.model_context_scorer_token[0]" in block
    assert '"roles/secretmanager.secretAccessor"' in block
    assert "google_service_account" not in CPM_TF
    # The CI apply identity can create the pool: the shared grant counts it.
    pool_admin = _search(
        r'resource "google_project_iam_member" "screener_fleet_x509_pool_admin" '
        r"\{(.*?)\n\}",
        X509_TF,
        re.S,
    ).group(1)
    assert 'var.cpm_scorer_aws_account_id != ""' in pool_admin


def test_ansible_audience_and_secret_match_terraform() -> None:
    pool = _search(r'workload_identity_pool_id = "([^"]+)"', CPM_TF).group(1)
    provider = _search(r'workload_identity_pool_provider_id = "([^"]+)"', CPM_TF)
    assert DEFAULTS["cpm_scorer_gcp_wif_pool"] == pool
    assert DEFAULTS["cpm_scorer_gcp_wif_provider"] == provider.group(1)
    assert DEFAULTS["cpm_scorer_token_secret"] == SECRET
    assert DEFAULTS["cpm_scorer_token_source"] == "aws-wif"
    tasks = (ROLE / "tasks" / "main.yml").read_text()
    assert "cpm_scorer_token_source in ['aws-wif', 'file']" in tasks
    for source in ("aws-wif", "file"):
        assert (ROLE / "tasks" / f"token-{source}.yml").is_file()


def test_bearer_reaches_the_scorer_only_as_a_private_file() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    write = next(t for t in tasks if t["name"] == "Write the Platform bearer file")
    copy = write["ansible.builtin.copy"]
    assert copy["mode"] == "0400"
    assert copy["owner"] == "{{ cpm_scorer_user }}"
    assert write["no_log"] is True
    # The unit mounts the file read-only and names it; the value itself is
    # never in the unit, the environment file, or the updater.
    assert ",readonly" in UNIT
    assert "DITTO_MODEL_CONTEXT_SCORER_TOKEN_FILE=" in UNIT
    assert "DITTO_MODEL_CONTEXT_SCORER_TOKEN=" not in UNIT
    assert "MODEL_CONTEXT_SCORER_TOKEN" not in UPDATER


def test_unit_runs_only_the_verified_image_on_the_gpu() -> None:
    assert "EnvironmentFile={{ cpm_scorer_release_root }}/current.env" in UNIT
    assert "${CPM_SCORER_IMAGE}" in UNIT
    assert "--pull never" in UNIT and "--runtime=nvidia --gpus all" in UNIT
    assert "--user {{ cpm_scorer_uid }}:{{ cpm_scorer_gid }}" in UNIT
    assert "--read-only" in UNIT and "--cap-drop ALL" in UNIT
    assert "Restart=always" in UNIT and "StartLimitBurst=" in UNIT
    assert "serve --platform {{ cpm_scorer_platform_url }} --device cuda" in UNIT
    assert "--generate-bin /opt/cpm-scorer/bin/generate" in UNIT
    assert "COPY --from=generate /out/generate /opt/cpm-scorer/bin/generate" in (
        DOCKERFILE
    )
    # The model cache is on the root volume and owned by the scorer account.
    assert DEFAULTS["cpm_scorer_cache_dir"].startswith("/var/lib/")


def test_gpu_probe_and_release_image_share_one_cuda_base() -> None:
    base = _search(r"^FROM (docker\.io/nvidia/cuda:\S+)$", DOCKERFILE, re.M).group(1)
    assert DEFAULTS["cpm_scorer_gpu_probe_image"] == base
    assert "@sha256:" in base
    # torch 2.15 is CUDA 13.2; the r595 driver is CUDA 13.2's branch.
    assert "cuda:13.2" in base
    assert DEFAULTS["cpm_scorer_nvidia_driver_version"].startswith("595.")
    assert DEFAULTS["cpm_scorer_nvidia_driver_package_version"].startswith(
        DEFAULTS["cpm_scorer_nvidia_driver_version"] + "-"
    )


def test_bootstrap_pins_an_immutable_release() -> None:
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    validate = tasks[0]["ansible.builtin.assert"]["that"]
    assert "cpm_scorer_revision is match('^[0-9a-f]{40}$')" in validate
    activate = next(t for t in tasks if t["name"] == "Activate the pinned release")
    env = activate["environment"]
    assert env["CPM_SCORER_REF"].endswith(":cpm-scorer-sha-{{ cpm_scorer_revision }}")
    assert env["CPM_SCORER_EXPECTED_REVISION"] == "{{ cpm_scorer_revision }}"
    assert DEFAULTS["cpm_scorer_image_repository"] == (
        "ghcr.io/ditto-assistant/ditto-subnet-stack"
    )
    assert "cpm-scorer-sha-[0-9a-f]{40}$" in UPDATER


def test_platform_env_wiring_is_present_but_off() -> None:
    defaults = yaml.safe_load((PLATFORM_ROLE / "defaults" / "main.yml").read_text())
    assert defaults["secret_model_context_scorer_token"] == ""
    template = (PLATFORM_ROLE / "templates" / "platform.env.j2").read_text()
    assert (
        "{% if secret_model_context_scorer_token | default('') | length > 0 %}\n"
        "DITTO_MODEL_CONTEXT_SCORER_TOKEN="
        "{{ platform_secrets.model_context_scorer_token }}\n"
        "{% endif %}"
    ) in template
    tasks = (PLATFORM_ROLE / "tasks" / "main.yml").read_text()
    assert "--secret={{ secret_model_context_scorer_token }}" in tasks
    host_vars = yaml.safe_load(
        (ANSIBLE / "host_vars" / "ditto-platform-prod.yml").read_text()
    )
    # Naming the secret is the activation step; it is not taken here.
    assert "secret_model_context_scorer_token" not in host_vars
    # Platform reads the same name the scorer presents.
    config = (
        REPO / "apps" / "platform" / "ditto" / "api_server" / "config.py"
    ).read_text()
    assert 'os.environ.get("DITTO_MODEL_CONTEXT_SCORER_TOKEN")' in config
