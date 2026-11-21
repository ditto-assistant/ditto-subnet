"""Contracts that keep the AWS CPM scorer GPU host inert, isolated and keyless.

The aws-cpm-scorer root, its identity in aws-bootstrap, the CI roles and the
shared compute module name the same role, bucket, tag and size in several
places. These checks fail when one drifts or a grant widens.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TF = REPO / "infra" / "terraform"
ROOT = TF / "stacks" / "aws-cpm-scorer"
MAIN = (ROOT / "main.tf").read_text()
VARS = (ROOT / "variables.tf").read_text()
VERSIONS = (ROOT / "versions.tf").read_text()
BACKEND = (ROOT / "backend.tf").read_text()
USER_DATA = (ROOT / "user-data.sh.tftpl").read_text()
BOOTSTRAP_MAIN = (TF / "stacks" / "aws-bootstrap" / "main.tf").read_text()
BOOTSTRAP_IDENTITY = (
    TF / "stacks" / "aws-bootstrap" / "cpm-scorer-identity.tf"
).read_text()
AWS_LOCALS = (TF / "modules" / "compute" / "aws" / "locals.tf").read_text()
GCP_LOCALS = (TF / "modules" / "compute" / "gcp" / "locals.tf").read_text()
GCP_VARS = (TF / "modules" / "compute" / "gcp" / "variables.tf").read_text()
INTERFACE = (TF / "modules" / "compute" / "interface.md").read_text()
CI = (REPO / ".github" / "workflows" / "infra-ci.yml").read_text()

ROLE = "ditto-cpm-scorer"
STACK = "aws-cpm-scorer"

IDENTITY_WRITES = {
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


def _search(pattern: str, text: str, flags: int = 0) -> re.Match[str]:
    match = re.search(pattern, text, flags)
    assert match, pattern
    return match


def _default(text: str, variable: str) -> str:
    block = _search(rf'variable "{variable}" \{{(.*?)\n\}}', text, re.S).group(1)
    return _search(r"default\s*=\s*(\S+)", block).group(1).strip('"')


def _statements(document: str) -> dict[str, str]:
    parts = re.split(r"\n  (?:statement|dynamic \"statement\") \{", document)[1:]
    return {_search(r'sid\s+= "([\w${}.]+)"', part).group(1): part for part in parts}


def _policy(name: str, end: str) -> dict[str, str]:
    start = BOOTSTRAP_MAIN.index(f'data "aws_iam_policy_document" "{name}" {{')
    return _statements(BOOTSTRAP_MAIN[start : BOOTSTRAP_MAIN.index(end, start)])


def _actions(statement: str) -> set[str]:
    lists = re.findall(r"actions\s*=\s*\[(.*?)\]", statement, re.S)
    return {a for block in lists for a in re.findall(r'"([\w*:]+)"', block)}


# --- root: inert, isolated, egress only ---------------------------------------


def test_both_sides_default_to_creating_nothing() -> None:
    assert _default(VARS, "enable_cpm_scorer") == "false"
    assert _default(BOOTSTRAP_IDENTITY, "enable_cpm_scorer_identity") == "false"
    assert _default(VARS, "deletion_protection") == "true"


def test_root_state_and_tag_are_its_own() -> None:
    assert f'key          = "{STACK}/terraform.tfstate"' in BACKEND
    assert f'stack  = "{STACK}"' in VERSIONS
    assert "allowed_account_ids = [var.aws_account_id]" in VERSIONS


def test_one_role_name_everywhere() -> None:
    assert f'scorer_role_name = "{ROLE}"' in MAIN
    assert f'cpm_scorer_role_name  = "{ROLE}"' in BOOTSTRAP_MAIN
    assert _default(VARS, "instance_profile_name") == ROLE
    assert "self.role_name == local.scorer_role_name" in MAIN


def test_root_creates_no_iam_and_no_ingress() -> None:
    assert 'resource "aws_iam_' not in MAIN
    assert 'data "aws_iam_instance_profile" "scorer"' in MAIN
    assert "ingress" not in MAIN.lower().replace("no ingress", "")
    assert 'toset(["80", "443"])' in MAIN
    assert "aws_default_security_group" not in MAIN


def test_transfer_bucket_matches_the_ci_grant() -> None:
    assert (
        '"ditto-cpm-scorer-ssm-${data.aws_caller_identity.current.account_id}"' in MAIN
    )
    assert (
        'cpm_scorer_ssm_bucket = "ditto-cpm-scorer-ssm-${local.account_id}"'
        in BOOTSTRAP_MAIN
    )
    assert "aws_s3_bucket_public_access_block" in MAIN
    assert "DenyInsecureTransport" in MAIN


def test_gpu_host_shape() -> None:
    assert _default(VARS, "size") == "gpu-l40s"
    assert _default(VARS, "image") == "ubuntu-24"
    assert _default(VARS, "availability_zone") == "us-east-1a"
    assert int(_default(VARS, "boot_disk_gb")) >= 200
    assert "var.boot_disk_gb >= 200" in VARS
    assert 'role               = "cpm-scorer"' in MAIN
    # The VPC does not overlap the generator scorer's or the screener's.
    assert _default(VARS, "vpc_cidr") == "10.118.2.0/24"


def test_user_data_only_pins_the_ssm_agent() -> None:
    assert "sha256sum --check --strict" in USER_DATA
    assert "snap remove --purge amazon-ssm-agent" in USER_DATA
    # The pinned package is verified before the stock agent is removed.
    assert USER_DATA.index("sha256sum --check") < USER_DATA.index("snap remove")
    # The GPU runtime and the scorer belong to the Ansible role.
    code = "\n".join(
        line
        for line in USER_DATA.lower().splitlines()
        if not line.lstrip().startswith("#")
    )
    for later in ("nvidia", "cuda", "pip ", "docker", "git "):
        assert later not in code, later


# --- compute module: AWS-only GPU size -------------------------------------------


def test_gpu_size_is_aws_only_and_gcp_refuses_it() -> None:
    assert '"gpu-l40s" = "g6e.xlarge"' in AWS_LOCALS
    assert '"gpu-l40s"' not in GCP_LOCALS
    assert '!contains(["gpu-l40s"], var.size)' in GCP_VARS
    assert "### AWS-only sizes" in INTERFACE
    assert "| `gpu-l40s` | g6e.xlarge" in INTERFACE


# --- bootstrap identity and CI grants -------------------------------------------


def test_identity_is_ssm_only_and_not_federated_yet() -> None:
    assert 'resource "aws_iam_role" "cpm_scorer"' in BOOTSTRAP_IDENTITY
    assert 'resource "aws_iam_instance_profile" "cpm_scorer"' in BOOTSTRAP_IDENTITY
    attachments = re.findall(r'policy_arn = "([^"]+)"', BOOTSTRAP_IDENTITY)
    assert attachments == ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
    assert 'aws_iam_role_policy"' not in BOOTSTRAP_IDENTITY
    assert 'identifiers = ["ec2.amazonaws.com"]' in BOOTSTRAP_IDENTITY
    assert "max_session_duration = 3600" in BOOTSTRAP_IDENTITY
    assert "aws_iam_access_key" not in BOOTSTRAP_IDENTITY
    assert "google" not in BOOTSTRAP_IDENTITY.split("####\n", 2)[-1]


def test_ci_roles_cover_the_root_by_state_and_tag() -> None:
    assert f'"{STACK}"' in _search(r"ci_roots = \[([^\]]*)\]", BOOTSTRAP_MAIN).group(1)
    assert "ci_stack_tags = local.ci_roots" in BOOTSTRAP_MAIN


def test_plan_reads_and_apply_passes_but_never_writes_the_identity() -> None:
    plan = _policy("plan", 'data "aws_iam_policy_document" "apply" {')
    assert "local.cpm_scorer_iam" in plan["ReadHostIdentities"]
    assert "local.cpm_scorer_ssm_bucket" in plan["ReadCpmScorerTransferBucket"]

    apply = _policy("apply", 'resource "aws_iam_role_policy" "github_plan"')
    passes = apply["PassHostRolesToEc2"]
    assert "role/${local.cpm_scorer_role_name}" in passes
    assert '"iam:PassedToService"' in passes and '"ec2.amazonaws.com"' in passes
    deny = apply["NeverWriteCpmScorerIdentity"]
    assert 'effect = "Deny"' in deny
    assert "resources = local.cpm_scorer_iam" in deny
    assert _actions(deny) == IDENTITY_WRITES
    # The deny mirrors the generator scorer's exactly.
    assert _actions(apply["NeverWriteScorerIdentity"]) == IDENTITY_WRITES
    # No allow statement other than PassRole and the read names the identity.
    for sid, body in apply.items():
        if 'effect = "Deny"' in body or 'effect    = "Deny"' in body:
            continue
        if "cpm_scorer_role_name" in body or "cpm_scorer_iam" in body:
            assert sid == "PassHostRolesToEc2", sid
    bucket = apply["ManageCpmScorerTransferBucket"]
    assert "arn:aws:s3:::${local.cpm_scorer_ssm_bucket}" in bucket


def test_infra_ci_validates_and_tests_the_root() -> None:
    assert f"- infra/terraform/stacks/{STACK}\n" in CI
    assert f"if: matrix.root == 'infra/terraform/stacks/{STACK}'" in CI
