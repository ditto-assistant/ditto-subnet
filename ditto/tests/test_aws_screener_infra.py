"""Security contracts for the on-demand AWS screener node.

The EC2 host, its AWS identities (aws-bootstrap), its GCP federation
(gcp-platform/screener-aws.tf) and the protected workflow name the same roles,
tags and vars in several places. These checks fail when one drifts or when a
grant widens past what the design needs.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TF = REPO / "infra" / "terraform"
ROOT = TF / "stacks" / "aws-screener"
MAIN = (ROOT / "main.tf").read_text()
VARS = (ROOT / "variables.tf").read_text()
VERSIONS = (ROOT / "versions.tf").read_text()
BACKEND = (ROOT / "backend.tf").read_text()
USER_DATA = (ROOT / "user-data.sh.tftpl").read_text()
BOOTSTRAP_MAIN = (TF / "stacks" / "aws-bootstrap" / "main.tf").read_text()
BOOTSTRAP_SCREENER = (TF / "stacks" / "aws-bootstrap" / "screener.tf").read_text()
GCP_AWS = (TF / "stacks" / "gcp-platform" / "screener-aws.tf").read_text()
GCP_TFVARS = (TF / "stacks" / "gcp-platform" / "prod.auto.tfvars").read_text()
MODULE_MAIN = (TF / "modules" / "compute" / "aws" / "main.tf").read_text()
MODULE_VARS = (TF / "modules" / "compute" / "aws" / "variables.tf").read_text()
MODULE_LOCALS = (TF / "modules" / "compute" / "aws" / "locals.tf").read_text()
GCP_LOCALS = (TF / "modules" / "compute" / "gcp" / "locals.tf").read_text()
WORKFLOW = (REPO / ".github" / "workflows" / "infra-plan-apply.yml").read_text()
CI = (REPO / ".github" / "workflows" / "infra-ci.yml").read_text()

NODE_ROLE = "ditto-screener-node"
WAKE_ROLE = "ditto-screener-capacity-wake"
STACK = "aws-screener"

# The vars file Ansible's aws-screener-node playbook consumes. Do not rename.
BOOTSTRAP_VAR_KEYS = {
    "screener_fleet_node_id",
    "screener_fleet_provider",
    "screener_fleet_provider_resource_id",
    "screener_fleet_release_sha",
    "screener_fleet_builder_image",
    "screener_fleet_google_identity",
    "screener_fleet_aws_wif_provider",
    "screener_fleet_gcp_project",
    "screener_fleet_registration_token_secret",
    "screener_fleet_kvm_enabled",
    "screener_fleet_idle_exit_seconds",
    "screener_fleet_worker_processes",
    "screener_fleet_runtime_enabled",
    "screener_fleet_platform_url",
}


def _search(pattern: str, text: str, flags: int = 0) -> re.Match[str]:
    match = re.search(pattern, text, flags)
    assert match, pattern
    return match


def _block(text: str, header: str) -> str:
    """Body of a top-level HCL block whose opening line starts with header."""
    start = text.index(header)
    end = text.index("\n}\n", start)
    return text[start:end]


def _default(text: str, variable: str) -> str:
    body = _block(text, f'variable "{variable}" {{')
    return _search(r"default\s*=\s*(\S+)", body).group(1).strip('"')


def _statements(document: str) -> dict[str, str]:
    parts = re.split(r"\n  (?:statement|dynamic \"statement\") \{", document)[1:]
    return {_search(r'sid\s+= "([\w${}.]+)"', part).group(1): part for part in parts}


def _apply_statements() -> dict[str, str]:
    start = BOOTSTRAP_MAIN.index('data "aws_iam_policy_document" "apply" {')
    end = BOOTSTRAP_MAIN.index('resource "aws_iam_role_policy" "github_plan"')
    return _statements(BOOTSTRAP_MAIN[start:end])


# --- root: inert, isolated, egress only ---------------------------------------


def test_root_is_inert_and_isolated() -> None:
    assert _default(VARS, "enable_aws_screener") == "false"
    assert 'key          = "aws-screener/terraform.tfstate"' in BACKEND
    assert f'stack  = "{STACK}"' in VERSIONS
    assert _default(VARS, "vpc_cidr") == "10.118.1.0/24"
    scorer_vars = (TF / "stacks" / "aws-generator-scorer" / "variables.tf").read_text()
    assert _default(scorer_vars, "vpc_cidr") != _default(VARS, "vpc_cidr")
    assert "enable_screener_aws_identity = false" in GCP_TFVARS


def test_root_creates_no_iam_and_names_the_bootstrap_profile() -> None:
    assert not re.search(r'resource "aws_iam_', MAIN)
    assert _default(VARS, "instance_profile_name") == NODE_ROLE
    assert f'self.role_name == "{NODE_ROLE}"' in MAIN
    assert "instance_profile_name = data.aws_iam_instance_profile.node[0].name" in MAIN


def test_no_ingress_and_only_http_https_egress() -> None:
    assert "ingress" not in MAIN.lower().replace("no ingress", "")
    assert "aws_vpc_security_group_ingress_rule" not in MAIN
    egress = _block(MAIN, 'resource "aws_vpc_security_group_egress_rule" "screener"')
    assert 'toset(["80", "443"])' in egress
    assert 'ip_protocol       = "tcp"' in egress
    # Managing the VPC default group would need a grant that tags any existing
    # group into CI scope; the instance uses its own group instead.
    assert "aws_default_security_group" not in MAIN
    assert "firewall_id      = aws_security_group.screener[0].id" in MAIN
    assert "aws_nat_gateway" not in MAIN


def test_instance_stops_itself_and_hides_credentials_from_containers() -> None:
    host = _block(MAIN, 'module "screener"')
    assert 'instance_initiated_shutdown_behavior = "stop"' in host
    assert "metadata_hop_limit = 1" in host
    assert "assign_public_ip = true" in host
    assert "size     = var.size" in host
    assert _default(VARS, "size") == "screener-on-demand"
    assert _default(VARS, "image") == "debian-12"
    assert _default(VARS, "boot_disk_gb") == "200"
    assert _default(VARS, "deletion_protection") == "true"
    # The module wires the variable, defaults to stop, and keeps IMDSv2 only.
    assert (
        "instance_initiated_shutdown_behavior = "
        "var.instance_initiated_shutdown_behavior" in MODULE_MAIN
    )
    assert _default(MODULE_VARS, "instance_initiated_shutdown_behavior") == "stop"
    assert 'http_tokens                 = "required"' in MODULE_MAIN
    assert '"screener-on-demand" = "c7a.2xlarge"' in MODULE_LOCALS
    assert '"screener-on-demand" = "c3d-highcpu-8"' in GCP_LOCALS


# --- first boot ------------------------------------------------------------------


def test_bootstrap_vars_contract_is_exact() -> None:
    rendered = set(
        re.findall(
            r"^\s+(screener_fleet_\w+)\s+=",
            _block(MAIN, "locals {"),
            re.M,
        )
    )
    # The instance id is the only key added on the host.
    assert rendered | {"screener_fleet_provider_resource_id"} == BOOTSTRAP_VAR_KEYS
    assert "screener_fleet_provider_resource_id" not in rendered
    assert 'data["screener_fleet_provider_resource_id"] = instance_id' in USER_DATA
    for literal in (
        'screener_fleet_provider                  = "aws"',
        'screener_fleet_google_identity           = "aws"',
        "screener_fleet_kvm_enabled               = false",
        "screener_fleet_runtime_enabled           = true",
    ):
        assert literal in MAIN, literal
    assert _default(VARS, "screener_fleet_node_id") == "subnet-screener-3"
    # No default: enabling requires an explicit value (plan precondition), and
    # the first-boot playbook checks the release worker supports it.
    assert _default(VARS, "screener_fleet_idle_exit_seconds") == "null"
    assert "var.screener_fleet_idle_exit_seconds != null" in MAIN
    assert _default(VARS, "screener_fleet_worker_processes") == "1"
    assert _default(VARS, "screener_fleet_gcp_project") == "ditto-app-dev"
    assert (
        _default(VARS, "screener_fleet_registration_token_secret")
        == "screener-aws-registration-token"
    )
    assert (
        _default(VARS, "screener_fleet_platform_url")
        == "https://platform-api.heyditto.ai"
    )
    assert "^subnet-screener-[1-9][0-9]*$" in VARS
    assert "@sha256:[0-9a-f]{64}$" in VARS
    assert (
        "bootstrap_vars_b64   = base64encode(jsonencode(local.bootstrap_vars))" in MAIN
    )


def test_user_data_checks_out_one_exact_release_and_converges_locally() -> None:
    assert USER_DATA.startswith("#!/bin/bash\n")
    assert "set -Eeuo pipefail" in USER_DATA
    assert '[[ "$release_sha" =~ ^[0-9a-f]{40}$ ]]' in USER_DATA
    assert (
        "git clone --filter=blob:none --no-checkout "
        '"$repository_url" "$repo"' in USER_DATA
    )
    assert (
        "repository_url='https://github.com/ditto-assistant/ditto-subnet.git'"
        in USER_DATA
    )
    assert 'checkout --detach "$release_sha"' in USER_DATA
    assert 'head="$(git -C "$repo" rev-parse HEAD)"' in USER_DATA
    assert 'if [ "$head" != "$release_sha" ]; then' in USER_DATA
    assert '"ansible-core==$ansible_core_version"' in USER_DATA
    assert re.fullmatch(r"\d+\.\d+\.\d+", _default(VARS, "ansible_core_version"))
    assert (
        'ansible-galaxy" collection install -r requirements.yml -p "$boot/collections"'
        in USER_DATA
    )
    assert 'export ANSIBLE_COLLECTIONS_PATH="$boot/collections"' in USER_DATA
    assert "-i localhost, -c local" in USER_DATA
    assert 'playbooks/aws-screener-node.yml -e "@$vars_file"' in USER_DATA
    assert 'vars_file="$config_dir/bootstrap-vars.json"' in USER_DATA
    assert "config_dir=/etc/ditto-screener-fleet" in USER_DATA
    assert 'chmod 0600 "$vars_file.tmp"' in USER_DATA
    assert "log=/var/log/ditto-screener-bootstrap.log" in USER_DATA


def test_user_data_uses_imdsv2_and_powers_off_only_an_unenrolled_host() -> None:
    assert "X-aws-ec2-metadata-token-ttl-seconds" in USER_DATA
    assert "-X PUT" in USER_DATA
    assert "latest/meta-data/instance-id" in USER_DATA
    # A failed converge stays up for SSM inspection; the only poweroff is the
    # cost guard for a host that never enrolled.
    assert USER_DATA.count("exec /usr/bin/systemctl poweroff") == 1
    assert "ConditionPathExists=!/etc/ditto-screener-fleet/node.json" in USER_DATA
    assert "OnBootSec=2h" in USER_DATA
    assert "OnUnitActiveSec=15min" in USER_DATA
    guard = USER_DATA.index("ditto-screener-unenrolled-poweroff.timer")
    assert guard < USER_DATA.index("amazon-ssm-agent.deb")
    code = "\n".join(
        line for line in USER_DATA.splitlines() if not line.lstrip().startswith("#")
    )
    stripped = code.replace("exec /usr/bin/systemctl poweroff", "")
    stripped = stripped.replace("unenrolled-poweroff", "").replace("powering off", "")
    for forbidden in ("poweroff", "shutdown", "halt", "systemctl stop", "reboot"):
        assert forbidden not in stripped, forbidden


def test_cost_guard_defers_to_a_live_first_converge_that_is_bounded() -> None:
    # The guard defers only for the exact first-boot process: pid plus kernel
    # start time (field 22, parsed after the last ") "), so a reused pid after a
    # SIGKILL cannot defer it.
    assert "rest=$${stat##*) }" in USER_DATA
    assert "shift 19" in USER_DATA
    assert '[ "$(start_time "$pid")" = "$recorded" ]' in USER_DATA
    assert "kill -0" not in USER_DATA
    assert 'read -r -a bootstrap_fields <<<"$${bootstrap_stat##*) }"' in USER_DATA
    assert (
        'printf \'%s %s\\n\' "$$" "$${bootstrap_fields[19]}" >"$bootstrap_pid_file"'
        in USER_DATA
    )
    assert 'rm -f "$bootstrap_pid_file"' in USER_DATA
    # The whole first boot, not just Ansible, runs under one deadline.
    reexec = USER_DATA.index(
        'exec timeout --kill-after=5m "$DITTO_SCREENER_BOOTSTRAP_DEADLINE" /bin/bash'
    )
    assert "export DITTO_SCREENER_BOOTSTRAP_DEADLINE=4h" in USER_DATA
    assert reexec < USER_DATA.index("set -Eeuo pipefail")
    assert reexec < USER_DATA.index("amazon-ssm-agent.deb")
    assert "timeout --kill-after=5m 3h" in USER_DATA
    pid_written = USER_DATA.index('>"$bootstrap_pid_file"')
    assert pid_written < USER_DATA.index("ansible-playbook")


def test_user_data_installs_the_exact_ssm_agent() -> None:
    # Same exact-bytes SSM agent install as the generator scorer.
    assert (
        'echo "$ssm_sha256  $workdir/amazon-ssm-agent.deb" | sha256sum --check --strict'
        in USER_DATA
    )
    # Nothing secret is rendered: no token values, only secret names.
    assert not re.search(r"(?i)(api[_-]?key|password|bearer)\s*=", USER_DATA)


def test_rendered_vars_are_base64_wrapped_for_the_shell() -> None:
    # The JSON is passed as base64 inside single quotes, so no Terraform value
    # can break out of the shell string.
    assert "printf '%s' '${bootstrap_vars_b64}' | base64 -d" in USER_DATA
    # Every other Terraform value is format-validated before it is quoted.
    rendered = set(re.findall(r"\$\{(\w+)\}", USER_DATA))
    assert rendered == {
        "region",
        "ssm_agent_version",
        "ssm_agent_sha256",
        "ansible_core_version",
        "release_sha",
        "bootstrap_vars_b64",
    }


# --- AWS identities (aws-bootstrap) ----------------------------------------


def test_screener_identities_live_only_in_the_sso_bootstrap() -> None:
    assert f'screener_node_role_name = "{NODE_ROLE}"' in BOOTSTRAP_MAIN
    assert f'screener_wake_role_name = "{WAKE_ROLE}"' in BOOTSTRAP_MAIN
    assert _default(BOOTSTRAP_SCREENER, "enable_aws_screener") == "false"
    assert _default(BOOTSTRAP_SCREENER, "screener_capacity_controller_unique_id") == ""
    node = _block(BOOTSTRAP_SCREENER, 'resource "aws_iam_role" "screener_node"')
    assert "name                 = local.screener_node_role_name" in node
    attachments = re.findall(r"policy_arn = \"([^\"]+)\"", BOOTSTRAP_SCREENER)
    assert attachments == ["arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"]
    assert 'resource "aws_iam_role_policy" "screener_node' not in BOOTSTRAP_SCREENER
    assume = _block(
        BOOTSTRAP_SCREENER, 'data "aws_iam_policy_document" "screener_node_assume"'
    )
    assert 'identifiers = ["ec2.amazonaws.com"]' in assume


def test_wake_role_trusts_one_google_identity_and_audience() -> None:
    trust = _block(
        BOOTSTRAP_SCREENER, 'data "aws_iam_policy_document" "screener_wake_assume"'
    )
    assert 'actions = ["sts:AssumeRoleWithWebIdentity"]' in trust
    assert 'identifiers = ["accounts.google.com"]' in trust
    conditions = re.findall(
        r'test\s+= "(\w+)"\s+variable = "([^"]+)"\s+values\s+= \[([^\]]+)\]', trust
    )
    assert sorted(conditions) == sorted(
        [
            (
                "StringEquals",
                "accounts.google.com:aud",
                "var.screener_capacity_controller_unique_id",
            ),
            (
                "StringEquals",
                "accounts.google.com:sub",
                "var.screener_capacity_controller_unique_id",
            ),
            (
                "StringEquals",
                "accounts.google.com:oaud",
                "var.screener_capacity_wake_audience",
            ),
        ]
    )
    assert (
        _default(BOOTSTRAP_SCREENER, "screener_capacity_wake_audience")
        == "ditto-screener-capacity"
    )
    role = _block(BOOTSTRAP_SCREENER, 'resource "aws_iam_role" "screener_wake"')
    assert "max_session_duration = 3600" in role


def test_wake_role_can_only_describe_and_start_tagged_screeners() -> None:
    policy = _block(
        BOOTSTRAP_SCREENER, 'data "aws_iam_policy_document" "screener_wake" {'
    )
    statements = _statements(policy)
    assert set(statements) == {"FindScreener", "StartTaggedScreener"}
    granted = set(re.findall(r"actions\s+= \[([^\]]+)\]", policy))
    assert granted == {'"ec2:DescribeInstances"', '"ec2:StartInstances"'}
    assert 'actions   = ["ec2:DescribeInstances"]' in statements["FindScreener"]
    assert '"aws:RequestedRegion"' in statements["FindScreener"]
    start = statements["StartTaggedScreener"]
    assert 'actions   = ["ec2:StartInstances"]' in start
    assert ':instance/*"]' in start
    assert '"aws:ResourceTag/stack"' in start and "local.screener_stack_tag" in start
    assert '"aws:RequestedRegion"' in start
    assert f'screener_stack_tag      = "{STACK}"' in BOOTSTRAP_MAIN
    for forbidden in (
        "StopInstances",
        "TerminateInstances",
        "ModifyInstance",
        "iam:",
        "ssm:",
    ):
        assert forbidden not in policy, forbidden


# --- CI roles ------------------------------------------------------------------


def test_ci_roots_include_the_screener_with_its_own_state_and_tag() -> None:
    assert 'ci_roots = ["aws-generator-scorer", "aws-screener"]' in BOOTSTRAP_MAIN
    assert '"${aws_s3_bucket.state.arn}/${root}/terraform.tfstate"' in BOOTSTRAP_MAIN
    assert "ci_stack_tags = local.ci_roots" in BOOTSTRAP_MAIN
    statements = _apply_statements()
    assert "local.ci_stack_tags" in statements["MutateTaggedStackResources"]
    # No CI root may tag an existing security group into scope.
    assert "TagStackDefaultSecurityGroup" not in statements


def test_ci_cannot_manage_or_retrust_screener_identities() -> None:
    statements = _apply_statements()
    retrust = statements["NeverRetrustFederatedRoles"]
    assert 'effect    = "Deny"' in retrust
    assert 'actions   = ["iam:UpdateAssumeRolePolicy"]' in retrust
    assert "resources = local.federated_role_arns" in retrust
    federated = _search(r"federated_role_arns = \[(.*?)\]", BOOTSTRAP_MAIN, re.S).group(
        1
    )
    for role in (
        "scorer_role_name",
        "screener_node_role_name",
        "screener_wake_role_name",
    ):
        assert f"role/${{local.{role}}}" in federated, role
    deny = statements["NeverManageScreenerIdentity"]
    assert 'effect = "Deny"' in deny
    for action in (
        "iam:Create*",
        "iam:Delete*",
        "iam:Put*",
        "iam:Attach*",
        "iam:Update*",
    ):
        assert f'"{action}"' in deny, action
    assert "local.screener_node_iam" in deny and "screener_wake_role_name" in deny
    # No allow statement names a screener identity except PassRole of the node
    # role to EC2.
    for sid, body in statements.items():
        if "effect" in body and '"Deny"' in body:
            continue
        if "screener_node_role_name" in body or "screener_node_iam" in body:
            assert sid == "PassHostRolesToEc2", sid
        assert "screener_wake_role_name" not in body, sid
    passrole = statements["PassHostRolesToEc2"]
    assert '"iam:PassedToService"' in passrole and '"ec2.amazonaws.com"' in passrole
    # The scorer identity also lives in the bootstrap; CI writes neither.
    assert "ManageScorerIdentity" not in statements
    assert "AttachOnlySsmCore" not in statements
    scorer = statements["NeverWriteScorerIdentity"]
    assert 'effect = "Deny"' in scorer
    assert "resources = local.scorer_iam" in scorer


def test_plan_role_reads_screener_identity_read_only() -> None:
    start = BOOTSTRAP_MAIN.index('data "aws_iam_policy_document" "plan" {')
    end = BOOTSTRAP_MAIN.index('data "aws_iam_policy_document" "apply" {')
    plan = _statements(BOOTSTRAP_MAIN[start:end])
    read = plan["ReadHostIdentities"]
    assert "concat(local.scorer_iam, local.screener_node_iam)" in read
    actions = set(re.findall(r'"(iam:\w+)"', read))
    assert all(a.split(":")[1].startswith(("Get", "List")) for a in actions), actions


# --- GCP federation ------------------------------------------------------------


def test_gcp_federation_is_default_off_and_pins_the_node_role() -> None:
    assert _default(GCP_AWS, "enable_screener_aws_identity") == "false"
    assert f'screener_aws_role_name = "{NODE_ROLE}"' in GCP_AWS
    assert 'workload_identity_pool_id = "screener-aws"' in GCP_AWS
    assert 'workload_identity_pool_provider_id = "aws-ec2"' in GCP_AWS
    assert (
        '"attribute.aws_role" = "assertion.arn.extract(\'assumed-role/{role}/\')"'
        in GCP_AWS
    )
    assert (
        "assertion.arn.startsWith('arn:aws:sts::${var.screener_aws_account_id}"
        ":assumed-role/${local.screener_aws_role_name}/')" in GCP_AWS
    )
    grants = dict(
        re.findall(
            r'resource "google_secret_manager_secret_iam_member" "([^"]+)" \{(.*?)\n\}',
            GCP_AWS,
            re.S,
        )
    )
    secrets = {
        _search(r"secret_id = ([^\n]+)", body).group(1).strip('"')
        for body in grants.values()
    }
    assert secrets == {
        "validator-openrouter-key",
        "screener-review-ditto-inference-key",
        "screener-probe-review-router-key",
        "google_secret_manager_secret.screener_aws_registration_token[0].secret_id",
    }
    for body in grants.values():
        assert 'role      = "roles/secretmanager.secretAccessor"' in body
        assert "member    = local.screener_aws_principal_set" in body
    assert "/attribute.aws_role/${local.screener_aws_role_name}" in GCP_AWS
    assert "google_project_iam_member" not in GCP_AWS.replace(
        "depends_on = [google_project_iam_member.screener_fleet_x509_pool_admin]", ""
    )
    # Terraform owns the grant container only; the operator adds the version.
    assert 'secret_id = "screener-aws-registration-token"' in GCP_AWS
    assert "google_secret_manager_secret_version" not in GCP_AWS
    assert "unique_id" in _block(
        GCP_AWS, 'output "screener_capacity_controller_unique_id"'
    )


# --- workflows -------------------------------------------------------------------


def test_workflow_plans_the_screener_with_aws_oidc_only() -> None:
    options = (
        _search(r"options: \[(gcp-platform[^\]]*)\]", WORKFLOW).group(1).split(", ")
    )
    assert "aws-screener" in options
    assert "aws-bootstrap" not in options
    assert WORKFLOW.count("aws-generator-scorer|aws-screener)") == 2
    # aws-* roots take the AWS OIDC role steps and skip every app-credential step.
    assert WORKFLOW.count("if: startsWith(inputs.root, 'aws-')") >= 4
    assert "- infra/terraform/stacks/aws-screener\n" in CI
    assert "if: matrix.root == 'infra/terraform/stacks/aws-screener'" in CI
