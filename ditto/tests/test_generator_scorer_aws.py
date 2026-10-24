"""Contracts that keep the AWS generator scorer portable and inert until enabled.

The AWS host, its GCP federation, the bootstrap CI roles and the Ansible role
name the same identities in four places. These checks fail when one drifts.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TF = REPO / "infra" / "terraform"
GCP_SCORER = (TF / "stacks" / "gcp-platform" / "generator-scorer.tf").read_text()
GCP_TFVARS = (TF / "stacks" / "gcp-platform" / "prod.auto.tfvars").read_text()
AWS_ROOT = TF / "stacks" / "aws-generator-scorer"
AWS_MAIN = (AWS_ROOT / "main.tf").read_text()
AWS_VARS = (AWS_ROOT / "variables.tf").read_text()
BOOTSTRAP = (TF / "stacks" / "aws-bootstrap" / "main.tf").read_text()
ROLE_DEFAULTS = (
    REPO / "infra" / "ansible" / "roles" / "generator_scorer" / "defaults" / "main.yml"
).read_text()
WORKFLOW = (REPO / ".github" / "workflows" / "infra-plan-apply.yml").read_text()


def _default(text: str, variable: str) -> str:
    block = re.search(rf'variable "{variable}" \{{(.*?)\n\}}', text, re.S)
    assert block, variable
    match = re.search(r"default\s*=\s*(\S+)", block.group(1))
    assert match, variable
    return match.group(1).strip('"')


def _yaml_value(key: str) -> str:
    match = re.search(rf"^{key}:\s*\"?([^\"\n]+)\"?$", ROLE_DEFAULTS, re.M)
    assert match, key
    return match.group(1).strip()


def test_both_sides_default_to_creating_nothing() -> None:
    assert _default(AWS_VARS, "enable_generator_scorer") == "false"
    assert _default(GCP_SCORER, "generator_scorer_aws_account_id") == ""
    assert "generator_scorer_aws_account_id" not in GCP_TFVARS
    assert _default(GCP_SCORER, "generator_scorer_gce_deletion_protection") == "true"


def test_one_role_name_everywhere() -> None:
    role = re.search(r'scorer_role_name = "([^"]+)"', AWS_MAIN).group(1)
    assert _default(GCP_SCORER, "generator_scorer_aws_role_name") == role
    assert re.search(rf'scorer_role_name\s*=\s*"{role}"', BOOTSTRAP)


def test_federation_reads_exactly_one_secret_for_one_role() -> None:
    assert (
        "assertion.arn.startsWith('arn:aws:sts::${var.generator_scorer_aws_account_id}"
        ":assumed-role/${var.generator_scorer_aws_role_name}/')" in GCP_SCORER
    )
    grants = re.findall(r"principalSet://[^\"]+", GCP_SCORER)
    assert len(grants) == 1
    role_suffix = "/attribute.aws_role/${var.generator_scorer_aws_role_name}"
    assert grants[0].endswith(role_suffix)
    block = re.search(
        r'resource "google_secret_manager_secret_iam_member" '
        r'"generator_scorer_token_aws_access" \{(.*?)\n\}',
        GCP_SCORER,
        re.S,
    ).group(1)
    assert "google_secret_manager_secret.generator_scorer_token.secret_id" in block
    assert '"roles/secretmanager.secretAccessor"' in block
    assert "google_service_account" not in block


def test_ansible_audience_matches_terraform() -> None:
    pool = re.search(r'workload_identity_pool_id = "([^"]+)"', GCP_SCORER).group(1)
    provider = re.search(
        r'workload_identity_pool_provider_id = "([^"]+)"', GCP_SCORER
    ).group(1)
    assert _yaml_value("generator_scorer_gcp_wif_pool") == pool
    assert _yaml_value("generator_scorer_gcp_wif_provider") == provider
    # The GCE host keeps its original source.
    assert _yaml_value("generator_scorer_token_source") == "gcloud"


def test_aws_roots_get_no_app_credentials() -> None:
    for name in ("Create exact private plan", "Apply the reviewed binary plan"):
        step = re.search(rf"- name: {name}\n\s+if: ([^\n]+)", WORKFLOW).group(1)
        assert "!startsWith(inputs.root, 'aws-')" in step, name
    assert "arn:aws:iam::339713011188:role/ditto-subnet-infra-plan" in WORKFLOW
    assert "arn:aws:iam::339713011188:role/ditto-subnet-infra-apply" in WORKFLOW
    assert "aws-access-key-id" not in WORKFLOW
    assert "AWS_SECRET_ACCESS_KEY" not in WORKFLOW


def test_ci_roles_trust_only_protected_environments() -> None:
    assert '"repo:${local.repository}:environment:infra-plan"' in BOOTSTRAP
    assert '"repo:${local.repository}:environment:infra-apply"' in BOOTSTRAP
    assert "aws_iam_access_key" not in BOOTSTRAP
    options = re.search(r"options: \[(gcp-platform[^\]]*)\]", WORKFLOW)
    roots = options.group(1).split(", ")
    assert "aws-generator-scorer" in roots
    # The bootstrap creates the workflow's own identity; it is never a root.
    assert "aws-bootstrap" not in roots
