from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
GCP_ROOT = ROOT / "infra" / "terraform" / "stacks" / "gcp-platform"
PREVIEW_ROOT = "infra/terraform/stacks/gcp-preview"


def test_preview_root_is_validated_and_has_protected_plan_apply_routing() -> None:
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "infra-ci.yml").read_text())
    roots = ci["jobs"]["terraform"]["strategy"]["matrix"]["root"]
    assert PREVIEW_ROOT in roots

    delivery_path = ROOT / ".github" / "workflows" / "infra-plan-apply.yml"
    delivery_text = delivery_path.read_text()
    delivery = yaml.safe_load(delivery_text)
    root_options = delivery[True]["workflow_dispatch"]["inputs"]["root"]["options"]
    assert "gcp-preview" in root_options
    assert delivery["jobs"]["apply"]["environment"] == "infra-apply"
    assert (
        delivery_text.count("gcp-preview) root=infra/terraform/stacks/gcp-preview") == 2
    )
    assert 'if [ "$TF_ROOT" = gcp-platform ]; then' in delivery_text


def test_preview_state_bootstrap_is_exact_object_only() -> None:
    lock_grant = (GCP_ROOT / "terraform-state-locks.tf").read_text()
    assert 'role   = "roles/storage.objectAdmin"' in lock_grant
    assert 'role   = "roles/storage.objectCreator"' in lock_grant
    assert (
        'resource.name == \\"projects/_/buckets/ditto-app-dev-tfstate/'
        'objects/gcp-preview/default.tflock\\"' in lock_grant
    )
    assert (
        'resource.name == \\"projects/_/buckets/ditto-app-dev-tfstate/'
        'objects/gcp-preview/default.tfstate\\"' in lock_grant
    )
    assert "startsWith" not in lock_grant


def test_production_intent_explicitly_sets_every_live_resource_toggle() -> None:
    intent = (GCP_ROOT / "prod.auto.tfvars").read_text()
    declared = set(
        re.findall(
            r'^variable "(enable_[a-z0-9_]+|manage_dns)"',
            "\n".join(path.read_text() for path in GCP_ROOT.glob("*.tf")),
            re.MULTILINE,
        )
    )
    assigned = set(
        re.findall(r"^(enable_[a-z0-9_]+|manage_dns)\s*=", intent, re.MULTILINE)
    )
    assert declared <= assigned


def test_fleet_boot_is_bound_to_a_protected_release_sha() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "infra-plan-apply.yml").read_text()
    )
    checkout = workflow["jobs"]["plan"]["steps"][0]
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["fetch-depth"] == 0
    bind_step = next(
        step
        for step in workflow["jobs"]["plan"]["steps"]
        if step.get("name") == "Bind the screener fleet to the latest semantic release"
    )
    assert bind_step["if"] == "inputs.root == 'gcp-platform'"
    assert "gh release view" in bind_step["run"]
    assert "git rev-list -n 1" in bind_step["run"]
    assert "git merge-base --is-ancestor" in bind_step["run"]
    assert "TF_VAR_screener_fleet_release_sha=$revision" in bind_step["run"]
    fleet = (GCP_ROOT / "screener-fleet.tf").read_text()
    startup = (GCP_ROOT / "files" / "screener-fleet-startup.sh.tpl").read_text()
    bootstrap = (
        ROOT / "workers" / "screener" / "scripts" / "bootstrap-screener.sh"
    ).read_text()
    assert "git_revision      = var.screener_fleet_release_sha" in fleet
    assert 'git_ref           = "main"' not in fleet
    assert (
        'git -C /opt/ditto/bootstrap-src fetch --depth 1 origin "${git_revision}"'
        in startup
    )
    assert 'SCREENER_EXPECTED_SHA="${git_revision}"' in startup
    assert 'test "$target_sha" = "$SCREENER_EXPECTED_SHA"' in bootstrap


def test_controller_deploy_proves_exact_fresh_controller_heartbeat() -> None:
    updater = (
        ROOT / "services" / "screener-orchestrator" / "scripts" / "update-controller.sh"
    ).read_text()
    for contract in (
        ".controller_stale == false",
        ".controller_source_sha == $revision",
        ".controller_epoch != $prior_epoch",
    ):
        assert contract in updater
    assert ".activate_fallback == false" not in updater
    assert ".provider_ready == true" not in updater
    assert '$(git -C "$CONTROLLER_ROOT"' not in updater
    assert (
        'previous_sha="$(as_deploy git -C "$CONTROLLER_ROOT" rev-parse HEAD)"'
        in updater
    )
    assert (
        'test "$(as_deploy git -C "$CONTROLLER_ROOT" rev-parse HEAD)" = "$revision"'
        in updater
    )
    assert 'as_deploy git -C "$CONTROLLER_ROOT" cat-file -e' in updater


def test_controller_deploy_identity_has_no_gce_worker_access() -> None:
    terraform = (GCP_ROOT / "screener-deploy.tf").read_text()

    assert "screener_deploy_actas_worker" not in terraform
    assert 'for_each = toset(["roles/compute.viewer"])' in terraform
    assert 'resource "google_compute_instance_iam_member"' in terraform
    assert 'resource "google_iap_tunnel_instance_iam_member"' in terraform
    assert "module.screener_capacity_controller_vm[0].hostname" in terraform
    assert '"iap.tunnelInstances.getIamPolicy"' in terraform
    assert '"iap.tunnelInstances.setIamPolicy"' in terraform
    assert (
        "github-actions-terraform-apply@${var.project}.iam.gserviceaccount.com"
        in terraform
    )
    assert "google_project_iam_member.terraform_screener_iap_policy_admin" in terraform


def test_controller_deploy_releases_only_the_stopped_writer_epoch() -> None:
    updater = (
        ROOT / "services" / "screener-orchestrator" / "scripts" / "update-controller.sh"
    ).read_text()

    stop = 'systemctl stop "$CONTROLLER_UNIT"'
    release = 'release_lease "$prior_epoch"'
    start = 'systemctl start "$CONTROLLER_UNIT"'
    assert updater.index(stop) < updater.index(release) < updater.index(start)
    assert "/api/v1/screener/controller/release" in updater
    assert '"environment": os.environ["SCREENER_CONTROLLER_ENVIRONMENT"]' in updater
    assert '"controller_epoch": os.environ["SCREENER_CONTROLLER_EPOCH"]' in updater
    assert "retaining expiry-based handoff" in updater
    assert '"Authorization": f"Bearer {token}"' in updater
    assert f"{stop} || true" not in updater


# Issue #395: every applyable revision keeps the provider mutator on its
# dedicated roles, so a broad-to-custom migration cannot land in two layers.
CAPACITY_CONTROLLER = re.compile(
    r"google_service_account\.screener_capacity_controller\b"
    r"|ditto-screener-capacity@"
)
BROAD_ROLES = {
    "roles/owner",
    "roles/editor",
    "roles/compute.admin",
    "roles/compute.instanceAdmin",
    "roles/compute.instanceAdmin.v1",
}
# Custom role -> (exact permissions, required binding condition suffix).
CAPACITY_CONTROLLER_ROLES = {
    "screener_controller_fleet_reconciler": (
        {
            "compute.instanceGroupManagers.get",
            "compute.instanceGroupManagers.update",
            "compute.instanceGroupManagers.use",
        },
        "/instanceGroupManagers/ditto-screener-fleet')",
    ),
    "screener_controller_autoscaler_reader": ({"compute.autoscalers.list"}, None),
    "screener_controller_autoscaler_updater": (
        {"compute.autoscalers.get", "compute.autoscalers.update"},
        "/autoscalers/ditto-screener-fleet')",
    ),
}
RESOURCE = re.compile(
    r'^resource "(\w+)" "(\w+)" \{\n(.*?)^\}', re.MULTILINE | re.DOTALL
)


def _terraform_resources() -> list[tuple[str, str, str]]:
    return [
        (match[1], match[2], match[3])
        for path in sorted((ROOT / "infra" / "terraform").rglob("*.tf"))
        for match in RESOURCE.finditer(path.read_text())
    ]


def _attribute(body: str, name: str) -> str | None:
    match = re.search(
        rf"^\s*{name}\s*=\s*(\[.*?\]|[^\n]+)$", body, re.MULTILINE | re.DOTALL
    )
    return match.group(1).strip() if match else None


def test_capacity_controller_holds_no_broad_or_project_wide_predefined_role() -> None:
    project_roles: set[str] = set()
    for kind, name, body in _terraform_resources():
        members = _attribute(body, "members") or _attribute(body, "member")
        if not kind.endswith(("_iam_member", "_iam_binding")) or not (
            members and CAPACITY_CONTROLLER.search(members)
        ):
            continue
        role = _attribute(body, "role")
        custom = re.fullmatch(
            r"google_project_iam_custom_role\.(\w+)\[0\]\.name", role or ""
        )
        literal = re.fullmatch(r'"(roles/[\w.]+)"', role or "")
        assert custom or literal, f"{kind}.{name} binds an unresolvable role {role}"
        assert not literal or literal.group(1) not in BROAD_ROLES, (
            f"{kind}.{name} grants broad {role}"
        )
        if kind.startswith("google_project_iam_"):
            assert custom, f"{kind}.{name} grants predefined {role} project-wide"
            project_roles.add(custom.group(1))
    assert project_roles == set(CAPACITY_CONTROLLER_ROLES)


def test_capacity_controller_custom_roles_stay_exact_and_scoped() -> None:
    resources = {(kind, name): body for kind, name, body in _terraform_resources()}
    for name, (permissions, condition) in CAPACITY_CONTROLLER_ROLES.items():
        role = resources["google_project_iam_custom_role", name]
        assert (
            set(re.findall(r'"([\w.]+)"', _attribute(role, "permissions") or ""))
            == permissions
        )
        binding = resources["google_project_iam_member", name]
        expression = _attribute(binding, "expression") or ""
        if condition is None:
            assert "condition" not in binding
        else:
            assert "resource.name.endsWith('/regions/${var.region}" in expression
            assert expression.endswith(condition + '"')


def test_infra_apply_requires_current_main_and_sealed_plan() -> None:
    delivery = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "infra-plan-apply.yml").read_text()
    )
    plan_checkout = next(
        step
        for step in delivery["jobs"]["plan"]["steps"]
        if "checkout" in step.get("uses", "")
    )
    assert plan_checkout["with"]["ref"] == "main"
    apply = {step.get("name"): step for step in delivery["jobs"]["apply"]["steps"]}
    assert (
        'test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"'
        in apply["Require plan commit to remain current main"]["run"]
    )
    assert (
        "sha256sum -c tfplan.sha256"
        in apply["Fetch and verify exact private plan"]["run"]
    )


def test_capacity_controller_retires_targon_builder_and_credential() -> None:
    intent = (GCP_ROOT / "prod.auto.tfvars").read_text()
    assert re.search(
        r"^enable_screener_capacity_controller\s*=\s*true$", intent, re.MULTILINE
    )
    role = ROOT / "infra" / "ansible" / "roles" / "screener_capacity_controller"
    tasks = (role / "tasks" / "main.yml").read_text()
    controller_unit = (
        role / "templates" / "ditto-screener-capacity.service.j2"
    ).read_text()
    assert not (role / "templates" / "ditto-image-builder.service.j2").exists()
    assert "ditto-image-builder" in tasks
    assert "targon-api-key" in tasks
    assert "--targon-" not in controller_unit
    updater = (
        ROOT / "services" / "screener-orchestrator" / "scripts" / "update-controller.sh"
    ).read_text()
    assert 'systemctl start "$BUILDER_UNIT"' not in updater
    assert 'systemctl stop "$RETIRED_BUILDER_UNIT" || return 1' in updater
    assert 'systemctl disable "$RETIRED_BUILDER_UNIT"' in updater
    assert 'rm -f -- "$RETIRED_BUILDER_UNIT_FILE" "$RETIRED_TARGON_KEY_FILE"' in updater
    assert updater.index("retire_builder\n") < updater.index(
        'if [[ "$previous_sha" == "$CONTROLLER_EXPECTED_SHA" ]]'
    )

    platform_prod = (
        ROOT / "infra" / "ansible" / "host_vars" / "ditto-platform-prod.yml"
    ).read_text()
    assert (
        'secret_screener_controller_api_token: "screener-controller-api-token-prod"'
        in platform_prod
    )
