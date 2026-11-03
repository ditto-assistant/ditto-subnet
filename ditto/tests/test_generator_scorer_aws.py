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


def _search(pattern: str, text: str, flags: int = 0) -> re.Match[str]:
    match = re.search(pattern, text, flags)
    assert match, pattern
    return match


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
    role = _search(r'scorer_role_name = "([^"]+)"', AWS_MAIN).group(1)
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
    block = _search(
        r'resource "google_secret_manager_secret_iam_member" '
        r'"generator_scorer_token_aws_access" \{(.*?)\n\}',
        GCP_SCORER,
        re.S,
    ).group(1)
    assert "google_secret_manager_secret.generator_scorer_token.secret_id" in block
    assert '"roles/secretmanager.secretAccessor"' in block
    assert "google_service_account" not in block


def test_ansible_audience_matches_terraform() -> None:
    pool = _search(r'workload_identity_pool_id = "([^"]+)"', GCP_SCORER).group(1)
    provider = _search(
        r'workload_identity_pool_provider_id = "([^"]+)"', GCP_SCORER
    ).group(1)
    assert _yaml_value("generator_scorer_gcp_wif_pool") == pool
    assert _yaml_value("generator_scorer_gcp_wif_provider") == provider
    # The GCE host keeps its original source.
    assert _yaml_value("generator_scorer_token_source") == "gcloud"


def test_aws_roots_get_no_app_credentials() -> None:
    for name in ("Create exact private plan", "Apply the reviewed binary plan"):
        step = _search(rf"- name: {name}\n\s+if: ([^\n]+)", WORKFLOW).group(1)
        assert "!startsWith(inputs.root, 'aws-')" in step, name
    assert "arn:aws:iam::339713011188:role/ditto-subnet-infra-plan" in WORKFLOW
    assert "arn:aws:iam::339713011188:role/ditto-subnet-infra-apply" in WORKFLOW
    assert "aws-access-key-id" not in WORKFLOW
    assert "AWS_SECRET_ACCESS_KEY" not in WORKFLOW


def test_ci_roles_trust_only_protected_environments() -> None:
    assert '"repo:${local.repository}:environment:infra-plan"' in BOOTSTRAP
    assert '"repo:${local.repository}:environment:infra-apply"' in BOOTSTRAP
    assert "aws_iam_access_key" not in BOOTSTRAP
    options = _search(r"options: \[(gcp-platform[^\]]*)\]", WORKFLOW)
    roots = options.group(1).split(", ")
    assert "aws-generator-scorer" in roots
    # The bootstrap creates the workflow's own identity; it is never a root.
    assert "aws-bootstrap" not in roots


def _apply_statements() -> list[str]:
    start = BOOTSTRAP.index('data "aws_iam_policy_document" "apply" {')
    end = BOOTSTRAP.index('resource "aws_iam_role_policy" "github_plan"')
    return re.split(
        r"\n  (?:statement|dynamic \"statement\") \{", BOOTSTRAP[start:end]
    )[1:]


def test_apply_role_ec2_mutation_is_tag_scoped() -> None:
    statements = _apply_statements()
    ec2 = [s for s in statements if '"ec2:' in s]
    assert ec2
    # Every EC2 grant is bounded by the scorer stack tag, tag-on-create, or the
    # image publisher, except two named grants whose residual scope the policy
    # comments record: untagged AWS-created default rules, and the launch ENI.
    bounds = (
        '"aws:ResourceTag/stack"',
        '"aws:RequestTag/stack"',
        '"ec2:CreateAction"',
        '"ec2:Owner"',
    )
    unbounded = {
        _search(r'sid\s+= "(\w+)"', s).group(1)
        for s in ec2
        if not any(bound in s for bound in bounds)
    }
    assert unbounded == {"RevokeUntaggedDefaultRules", "LaunchNetworkInterface"}
    for statement in ec2:
        if 'resources = ["*"]' in statement:
            assert '"aws:ResourceTag/stack"' in statement, statement
    # The scorer has no ingress, so nothing may grant it anywhere.
    assert "AuthorizeSecurityGroupIngress" not in BOOTSTRAP
    assert "UpdateSecurityGroupRuleDescriptionsIngress" not in BOOTSTRAP


def test_apply_role_cannot_retrust_the_federated_scorer_role() -> None:
    # GCP grants the bearer to every session of this role, so its trust policy
    # must not be writable by the CI apply role.
    retrust = [s for s in _apply_statements() if "iam:UpdateAssumeRolePolicy" in s]
    assert len(retrust) == 1
    assert 'effect    = "Deny"' in retrust[0]
