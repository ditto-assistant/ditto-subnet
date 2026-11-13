"""Contracts that keep the AWS generator scorer portable and inert until enabled.

The AWS host, its GCP federation, the bootstrap CI roles and the Ansible role
name the same identities in four places. These checks fail when one drifts.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TF = REPO / "infra" / "terraform"
GCP_SCORER = (TF / "stacks" / "gcp-platform" / "generator-scorer.tf").read_text()
GCP_TFVARS = (TF / "stacks" / "gcp-platform" / "prod.auto.tfvars").read_text()
AWS_ROOT = TF / "stacks" / "aws-generator-scorer"
AWS_MAIN = (AWS_ROOT / "main.tf").read_text()
AWS_VARS = (AWS_ROOT / "variables.tf").read_text()
BOOTSTRAP = (TF / "stacks" / "aws-bootstrap" / "main.tf").read_text()
BOOTSTRAP_IDENTITY = (
    TF / "stacks" / "aws-bootstrap" / "generator-scorer-identity.tf"
).read_text()
ROLE_DEFAULTS = (
    REPO / "infra" / "ansible" / "roles" / "generator_scorer" / "defaults" / "main.yml"
).read_text()


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
    assert _default(BOOTSTRAP_IDENTITY, "enable_generator_scorer_identity") == "false"
    assert _default(GCP_SCORER, "generator_scorer_aws_account_id") == ""
    assert "generator_scorer_aws_account_id" not in GCP_TFVARS
    assert _default(GCP_SCORER, "generator_scorer_gce_deletion_protection") == "true"


def test_one_role_name_everywhere() -> None:
    role = _search(r'scorer_role_name = "([^"]+)"', AWS_MAIN).group(1)
    assert _default(GCP_SCORER, "generator_scorer_aws_role_name") == role
    assert re.search(rf'scorer_role_name\s*=\s*"{role}"', BOOTSTRAP)
    assert _default(AWS_VARS, "instance_profile_name") == role


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


def test_ci_roles_trust_only_protected_environments() -> None:
    assert '"repo:${local.repository}:environment:infra-plan"' in BOOTSTRAP
    assert '"repo:${local.repository}:environment:infra-apply"' in BOOTSTRAP
    assert "aws_iam_access_key" not in BOOTSTRAP
    # Wiring the AWS roots into infra-plan-apply.yml is a separate change that
    # needs the independent admin review its ruleset requires.


def _apply_statements() -> list[str]:
    start = BOOTSTRAP.index('data "aws_iam_policy_document" "apply" {')
    end = BOOTSTRAP.index('resource "aws_iam_role_policy" "github_plan"')
    return re.split(
        r"\n  (?:statement|dynamic \"statement\") \{", BOOTSTRAP[start:end]
    )[1:]


def _sid(statement: str) -> str:
    return _search(r'sid\s+= "([^"]+)"', statement).group(1)


def _actions(statement: str) -> set[str]:
    lists = re.findall(r"actions\s*=\s*\[(.*?)\]", statement, re.S)
    return {a for block in lists for a in re.findall(r'"([\w*:]+)"', block)}


def _is_deny(statement: str) -> bool:
    return re.search(r'effect\s*=\s*"Deny"', statement) is not None


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
        '"aws:ResourceAccount"',
    )
    unbounded = {_sid(s) for s in ec2 if not any(bound in s for bound in bounds)}
    assert unbounded == {"RevokeUntaggedDefaultRules", "LaunchNetworkInterface"}
    for statement in ec2:
        if 'resources = ["*"]' in statement:
            assert '"aws:ResourceTag/stack"' in statement, statement
    # The scorer has no ingress, so nothing may grant it anywhere.
    assert "AuthorizeSecurityGroupIngress" not in BOOTSTRAP
    assert "UpdateSecurityGroupRuleDescriptionsIngress" not in BOOTSTRAP


def test_apply_role_cannot_bring_an_existing_security_group_into_scope() -> None:
    statements = _apply_statements()
    # The only way to tag a security group is while creating it: no statement
    # grants CreateTags on security-group/* except the create-action-scoped
    # TagOnCreate (whose wildcard resource also covers groups), and the
    # tag-scoped mutate statement needs the tag to be there already.
    taggers = [s for s in statements if '"ec2:CreateTags"' in s]
    for statement in taggers:
        sid = _sid(statement)
        if sid == "TagOnCreate":
            assert '"ec2:CreateAction"' in statement
        else:
            assert "security-group/*" not in statement, sid
            assert '"aws:ResourceTag/stack"' in statement, sid
    assert "TagScorerDefaultSecurityGroup" not in BOOTSTRAP
    assert "aws_default_security_group" not in AWS_MAIN
    # ModifySecurityGroupRules could widen a rule, so only groups (and rules)
    # that already carry the stack tag may be modified.
    modify = [s for s in statements if "ec2:ModifySecurityGroupRules" in s]
    assert [_sid(s) for s in modify] == ["MutateTaggedStackResources"]
    assert '"aws:ResourceTag/stack"' in modify[0]
    # Revoke on the untagged default rule also authorizes its group, which the
    # tag-scoped statement must cover.
    for action in ("RevokeSecurityGroupEgress", "RevokeSecurityGroupIngress"):
        scoped = [
            s
            for s in statements
            if f'"ec2:{action}"' in s and '"aws:ResourceTag/stack"' in s
        ]
        assert [_sid(s) for s in scoped] == ["MutateTaggedStackResources"], action
        assert 'resources = ["*"]' in scoped[0]


SCORER_IAM_WRITES = {
    "iam:CreateRole",
    "iam:DeleteRole",
    "iam:UpdateRole",
    "iam:UpdateRoleDescription",
    "iam:UpdateAssumeRolePolicy",
    "iam:TagRole",
    "iam:UntagRole",
    "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
    "iam:AttachRolePolicy",
    "iam:DetachRolePolicy",
    "iam:PutRolePermissionsBoundary",
    "iam:DeleteRolePermissionsBoundary",
    "iam:CreateInstanceProfile",
    "iam:DeleteInstanceProfile",
    "iam:AddRoleToInstanceProfile",
    "iam:RemoveRoleFromInstanceProfile",
    "iam:TagInstanceProfile",
    "iam:UntagInstanceProfile",
}


def test_apply_role_cannot_write_the_federated_scorer_identity() -> None:
    # GCP grants the bearer to every session of this role name, so the CI
    # apply role must not create, delete, retrust or re-permission the role or
    # its profile. aws-bootstrap creates them as an administrator.
    statements = _apply_statements()
    allows = [s for s in statements if not _is_deny(s)]
    iam_allows = {
        (_sid(s), a) for s in allows for a in _actions(s) if a.startswith("iam:")
    }
    assert iam_allows == {("PassHostRolesToEc2", "iam:PassRole")}
    # The plan policy, which apply inherits, only reads IAM.
    plan = BOOTSTRAP[
        BOOTSTRAP.index('data "aws_iam_policy_document" "plan" {') : BOOTSTRAP.index(
            'data "aws_iam_policy_document" "apply" {'
        )
    ]
    plan_iam = {a for a in _actions(plan) if a.startswith("iam:")}
    assert plan_iam
    assert all(a.startswith(("iam:Get", "iam:List")) for a in plan_iam), plan_iam
    passes = [s for s in allows if _sid(s) == "PassHostRolesToEc2"]
    assert '"iam:PassedToService"' in passes[0]
    assert '"ec2.amazonaws.com"' in passes[0]
    denies = [s for s in statements if _sid(s) == "NeverWriteScorerIdentity"]
    assert len(denies) == 1
    assert _is_deny(denies[0])
    assert "resources = local.scorer_iam" in denies[0]
    assert _actions(denies[0]) == SCORER_IAM_WRITES
    # The role and profile live in the admin-applied bootstrap, never the CI root.
    assert 'resource "aws_iam_role" "scorer"' in BOOTSTRAP_IDENTITY
    assert 'resource "aws_iam_instance_profile" "scorer"' in BOOTSTRAP_IDENTITY
    assert 'resource "aws_iam_' not in AWS_MAIN
    assert 'data "aws_iam_instance_profile" "scorer"' in AWS_MAIN
    assert "self.role_name == local.scorer_role_name" in AWS_MAIN


def test_gcloud_probe_runs_in_check_mode() -> None:
    tasks = yaml.safe_load(
        (
            REPO / "infra/ansible/roles/generator_scorer/tasks/token-aws-wif.yml"
        ).read_text()
    )
    probe = next(
        task for task in tasks if task["name"] == "Check for the Google Cloud CLI"
    )
    # The install guard reads its rc, which a skipped check-mode task lacks.
    assert probe["check_mode"] is False
    assert probe["failed_when"] is False


@pytest.mark.parametrize("arguments", [["--aply"], ["--apply", "extra"], ["apply"]])
def test_state_bucket_script_rejects_unknown_arguments(arguments: list[str]) -> None:
    script = REPO / "infra/terraform/stacks/aws-bootstrap/create-state-bucket.sh"
    # Only rejected argument lists are run here, so no AWS command executes.
    result = subprocess.run(
        ["bash", str(script), *arguments], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert result.stdout == ""
