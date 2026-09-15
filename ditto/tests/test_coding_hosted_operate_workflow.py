import ast
import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/coding-hosted-operate.yml"
VERIFIER = ROOT / "infra/scripts/coding-hosted-verify.py"
STACK = ROOT / "infra/terraform/stacks/gcp-platform/coding-hosted-operate.tf"
WIRING = ROOT / "infra/terraform/stacks/gcp-platform/coding-hosted.tf"
ENVIRONMENT = ROOT / "infra/github/coding-hosted-operate-environment.json"
RULESET = ROOT / "infra/github/coding-hosted-operate-ruleset.json"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _step(steps: list[dict], name: str) -> dict:
    return next(step for step in steps if step.get("name") == name)


def test_operate_is_manual_main_only_with_fixed_operations() -> None:
    workflow = _workflow()
    triggers = workflow.get("on", workflow[True])
    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"operation", "confirmation"}
    assert inputs["operation"]["type"] == "choice"
    assert inputs["operation"]["options"] == ["verify"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert set(workflow["jobs"]) == {"verify"}
    job = workflow["jobs"]["verify"]
    assert job["environment"] == "coding-hosted-operate"
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "inputs.operation == 'verify'" in job["if"]
    assert "inputs.confirmation == 'OPERATE CODING HOST verify'" in job["if"]
    assert job["permissions"] == {"contents": "read", "id-token": "write"}


def test_every_root_capable_job_requires_the_protected_environment() -> None:
    for job in _workflow()["jobs"].values():
        assert job["environment"] == "coding-hosted-operate"
    environment = json.loads(ENVIRONMENT.read_text())
    assert environment["prevent_self_review"] is True
    assert {reviewer["id"] for reviewer in environment["reviewers"]} == {
        6766068,
        170978465,
    }
    assert len(environment["reviewers"]) == 2
    assert environment["deployment_branch_policy"] == {
        "protected_branches": False,
        "custom_branch_policies": True,
    }


def test_operate_uses_exact_clean_source_before_authentication() -> None:
    steps = _workflow()["jobs"]["verify"]["steps"]
    assert steps[0]["with"] == {
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
    }
    clean = _step(steps, "Verify exact clean source before authentication")
    auth = next(
        step
        for step in steps
        if step.get("uses", "").startswith("google-github-actions/auth@")
    )
    assert steps.index(clean) < steps.index(auth)
    assert 'test -z "$(git status --porcelain)"' in clean["run"]
    assert auth["with"] == {
        "workload_identity_provider": (
            "${{ vars.GCP_CODING_HOSTED_OPERATE_WIF_PROVIDER }}"
        ),
        "service_account": "${{ vars.GCP_CODING_HOSTED_OPERATE_SA }}",
    }


def test_operate_has_no_command_inputs_secrets_or_broad_authority() -> None:
    text = WORKFLOW.read_text()
    for step in _workflow()["jobs"]["verify"]["steps"]:
        command = step.get("run", "")
        assert "${{" not in command
        assert "set -x" not in command
    for forbidden in (
        "secrets.",
        "gcloud secrets",
        "terraform",
        "GCP_TF_APPLY_SA",
        "upload-artifact",
        "GITHUB_ENV",
        "GITHUB_OUTPUT",
    ):
        assert forbidden not in text
    run = _step(
        _workflow()["jobs"]["verify"]["steps"],
        "Run the fixed read-only host verifier",
    )["run"]
    assert "gcloud compute ssh ditto-coding-hosted-v2 \\" in run
    assert "--project=ditto-app-dev" in run
    assert "--zone=us-central1-a" in run
    assert "--tunnel-through-iap" in run
    assert "--command='sudo -n /usr/bin/python3 -I -'" in run
    assert "< infra/scripts/coding-hosted-verify.py" in run
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in run
    # The only retried failure is runner-side key propagation, never a verdict.
    assert "@compute\\.[0-9]+: Permission denied \\(publickey\\)" in run
    assert '[ "$attempt" -ge 3 ]' in run


def test_operate_key_is_short_lived_and_always_removed() -> None:
    steps = _workflow()["jobs"]["verify"]["steps"]
    add = _step(steps, "Register a short-lived OS Login key")["run"]
    assert "--ttl 30m" in add
    remove = _step(steps, "Remove the short-lived OS Login key")
    assert remove["if"] == "always()"
    assert "gcloud compute os-login ssh-keys remove" in remove["run"]


def _verifier_calls() -> list[list[object]]:
    tree = ast.parse(VERIFIER.read_text())
    argvs: list[list[object]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run":
            argument = node.args[0]
            assert isinstance(argument, ast.List)
            argv: list[object] = [
                element.value if isinstance(element, ast.Constant) else None
                for element in argument.elts
            ]
            argvs.append(argv)
    return argvs


def test_verifier_runs_only_fixed_read_only_commands() -> None:
    source = VERIFIER.read_text()
    assert "shell=True" not in source
    assert "open(" not in source
    assert "write_text" not in source and "write_bytes" not in source
    assert "os.remove" not in source and "unlink" not in source
    assert "subprocess.run(" in source and source.count("subprocess.run(") == 1
    commands = _verifier_calls()
    assert commands, "verifier must run fixed commands"
    for argv in commands:
        head = argv[0]
        if head == "systemctl":
            assert argv[1] == "is-active"
        elif head == "openssl":
            assert argv[:3] == ["openssl", "pkey", "-pubin"]
        elif head == "runuser":
            # argv[2] is the RUNTIME_USER constant; the tail is one fixed check.
            assert argv[1] == "-u" and argv[2] is None and argv[3] == "--"
            assert argv[-4:] in (
                ["systemctl", "--user", "is-active", "coding-hosted-docker.service"],
                ["/usr/bin/python3", "-I", None, "verify"],
            )
        else:
            raise AssertionError(f"unexpected verifier command: {head}")


def test_verifier_never_opens_private_material() -> None:
    source = VERIFIER.read_text()
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    private = {
        "CUSTODY_PRIVATE_KEY",
        "CUSTODY_RECEIPT",
        "CUSTODY_POSTGRES_ENVIRONMENT",
        "WORKER_POSTGRES_ENVIRONMENT",
    }
    seen = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id in private):
            continue
        if isinstance(node.ctx, ast.Store):
            continue
        seen.add(node.id)
        parent = parents[node]
        # Private paths may only reach lstat metadata or a report label.
        assert (
            isinstance(parent, ast.Call)
            and getattr(parent.func, "id", None) == "metadata"
        ) or isinstance(parent, (ast.Tuple, ast.Dict)), ast.unparse(parent)
        assert not any(
            isinstance(call, ast.Call) and getattr(call.func, "id", None) == "run"
            for call in ast.walk(parent)
        )
    assert seen == private
    assert "gcloud" not in source and "secretmanager" not in source.lower()


def test_verifier_off_host_reports_nothing_about_the_machine() -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(VERIFIER)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["schema"] == "ditto-coding-host-verify-v1"
    assert report["ok"] is False
    assert report["reads_secrets"] is False
    assert report["mutates"] is False
    assert [check["name"] for check in report["checks"]] == ["host identity"]
    assert socket.gethostname() not in result.stdout


def test_workflow_identity_is_pinned_to_this_main_workflow() -> None:
    text = STACK.read_text()
    for clause in (
        "assertion.repository_id == '1224630318'",
        "assertion.repository_owner_id == '148669063'",
        "assertion.ref == 'refs/heads/main'",
        "assertion.event_name == 'workflow_dispatch'",
        "assertion.workflow_ref == 'ditto-assistant/ditto-subnet/.github/workflows/"
        "coding-hosted-operate.yml@refs/heads/main'",
        "assertion.actor_id in ['6766068', '170978465']",
    ):
        assert clause in text
    assert (
        '"repo:ditto-assistant/ditto-subnet:environment:coding-hosted-operate"' in text
    )
    for forbidden in (
        "google_project_iam_member",
        "google_project_iam_custom_role",
        "secret_manager",
        "storage_bucket_iam",
        "roles/owner",
        "roles/editor",
    ):
        assert forbidden not in text
    assert "default     = false" in text
    wiring = WIRING.read_text()
    assert "var.enable_coding_hosted_operate_workflow" in wiring
    prod = (ROOT / "infra/terraform/stacks/gcp-platform/prod.auto.tfvars").read_text()
    assert re.search(
        r"(?m)^enable_coding_hosted_operate_workflow\s*=\s*false\s*$", prod
    )
    assert (
        '"serviceAccount:${google_service_account.coding_hosted_operate.email}"'
        in wiring
    )


def test_review_ruleset_covers_every_root_capable_surface() -> None:
    ruleset = json.loads(RULESET.read_text())
    parameters = ruleset["rules"][0]["parameters"]
    assert parameters["require_last_push_approval"] is True
    (reviewer,) = parameters["required_reviewers"]
    assert reviewer["minimum_approvals"] == 1
    assert set(reviewer["file_patterns"]) == {
        ".github/workflows/coding-hosted-operate.yml",
        "infra/scripts/coding-hosted-verify.py",
        "infra/github/coding-hosted-operate-*.json",
        "infra/terraform/stacks/gcp-platform/coding-hosted*.tf",
        "infra/terraform/modules/coding-hosted-host/**",
    }


def _simulated_host(
    monkeypatch,
    *,
    receipt_owner: str | None,
    policy_exit: int,
    receipt_mode: str = "0600",
    socket_owner: str = "ditto-coding-hosted",
):
    """Load the verifier against an in-memory healthy host."""
    import importlib.util
    import types

    spec = importlib.util.spec_from_file_location("coding_hosted_verify", VERIFIER)
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)

    def entry(kind: str, owner: str, mode: str) -> dict:
        return {"exists": True, "type": kind, "owner": owner, "mode": mode, "links": 1}

    daemon, custody = verifier.RUNTIME_USER, verifier.CUSTODY_USER
    image = verifier.IMAGE_ROOT + "/" + "a" * 64
    tree = {
        verifier.DAEMON_HOME: entry("directory", daemon, "0700"),
        "/run/ditto-coding-hosted": entry("directory", daemon, "0700"),
        verifier.DAEMON_SOCKET: entry("socket", socket_owner, "0600"),
        verifier.CUSTODY_PUBLIC_KEY: entry("file", custody, "0644"),
        verifier.CUSTODY_PRIVATE_KEY: entry("file", custody, "0600"),
        verifier.CUSTODY_RECEIPT: entry("file", custody, "0600"),
        verifier.RUNTIME_ROOT: entry("directory", "root", "0755"),
        verifier.RUNTIME_BUNDLE: entry("file", "root", "0444"),
        verifier.IMAGE_ROOT: entry("directory", "root", "0755"),
        image: entry("directory", "root", "0755"),
    }
    if receipt_owner is not None:
        tree[image + "/import-receipt.json"] = entry(
            "file", receipt_owner, receipt_mode
        )
    calls: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[bytes]:
        calls.append(argv)
        if argv[:2] == ["systemctl", "is-active"] and len(argv) == 3:
            return subprocess.CompletedProcess(argv, 0, b"active\n", b"")
        if argv[:2] == ["systemctl", "is-active"]:
            return subprocess.CompletedProcess(argv, 3, b"inactive\ninactive\n", b"")
        if argv[0] == "openssl":
            return subprocess.CompletedProcess(argv, 0, b"der", b"")
        if argv[-1] == "verify":
            body = json.dumps({"schema": "dittobench-coding-hosted-daemon-check-v2"})
            return subprocess.CompletedProcess(argv, policy_exit, body.encode(), b"")
        return subprocess.CompletedProcess(argv, 0, b"active\n", b"")

    monkeypatch.setattr(
        verifier, "metadata", lambda path: tree.get(path, {"exists": False})
    )
    monkeypatch.setattr(verifier, "run", run)
    monkeypatch.setattr(
        verifier.socket, "gethostname", lambda: verifier.EXPECTED_HOSTNAME
    )
    monkeypatch.setattr(
        verifier.pwd, "getpwnam", lambda _name: types.SimpleNamespace(pw_uid=1001)
    )
    monkeypatch.setattr(
        verifier.hashlib,
        "sha256",
        lambda _body: types.SimpleNamespace(
            hexdigest=lambda: verifier.EXPECTED_CUSTODY_SPKI_SHA256
        ),
    )
    monkeypatch.setattr(
        verifier.os,
        "scandir",
        lambda path: (
            [types.SimpleNamespace(name="a" * 64, path=image)]
            if path == verifier.IMAGE_ROOT
            else []
        ),
    )
    monkeypatch.setattr(verifier.sys, "stdout", __import__("io").StringIO())
    status = verifier.main()
    report = json.loads(verifier.sys.stdout.getvalue())
    return status, report, calls


def _check(report: dict, name: str) -> dict:
    return next(check for check in report["checks"] if check["name"] == name)


POLICY_CHECK = "preinstalled non-root host policy verify (socket, paths, empty daemon)"


def test_verify_requires_the_empty_daemon_check_before_image_import(monkeypatch):
    status, report, calls = _simulated_host(
        monkeypatch, receipt_owner=None, policy_exit=0
    )
    assert status == 0 and report["ok"] is True
    assert _check(report, POLICY_CHECK)["required"] is True
    assert any(argv[-1] == "verify" for argv in calls)
    # The bootstrap verifier failing (e.g. an unexpected image) fails verify.
    status, report, _ = _simulated_host(monkeypatch, receipt_owner=None, policy_exit=1)
    assert status == 1 and report["ok"] is False


def test_verify_stays_usable_after_a_root_sealed_image_import(monkeypatch):
    status, report, calls = _simulated_host(
        monkeypatch, receipt_owner="root", policy_exit=1
    )
    assert status == 0 and report["ok"] is True
    skipped = _check(report, POLICY_CHECK)
    assert skipped["required"] is False and skipped["ok"] is False
    assert skipped["detail"] == {"applicable": False, "verified_imports": 1}
    assert not any(argv[-1] == "verify" for argv in calls)
    for name in (
        "rootless daemon home is private to the daemon account",
        "rootless daemon socket directory is private to the daemon account",
        "rootless daemon socket is private to the daemon account",
    ):
        assert _check(report, name)["required"] is True
        assert _check(report, name)["ok"] is True


def test_unsealed_import_receipt_never_relaxes_the_empty_daemon_check(monkeypatch):
    status, report, calls = _simulated_host(
        monkeypatch, receipt_owner="ditto-coding-hosted", policy_exit=1
    )
    assert status == 1 and report["ok"] is False
    assert _check(report, POLICY_CHECK)["required"] is True
    assert _check(report, "verified image imports")["detail"] == {"verified_imports": 0}
    assert any(argv[-1] == "verify" for argv in calls)


def test_import_phase_still_fails_closed_on_daemon_paths_and_loose_receipts(
    monkeypatch,
):
    # A socket not owned by the daemon account fails verify even after import.
    status, report, _ = _simulated_host(
        monkeypatch, receipt_owner="root", policy_exit=1, socket_owner="root"
    )
    assert status == 1 and report["ok"] is False
    assert (
        _check(report, "rootless daemon socket is private to the daemon account")["ok"]
        is False
    )
    # A root-owned receipt that is not mode 0600 does not count as an import.
    status, report, calls = _simulated_host(
        monkeypatch, receipt_owner="root", policy_exit=1, receipt_mode="0644"
    )
    assert status == 1 and _check(report, POLICY_CHECK)["required"] is True
    assert any(argv[-1] == "verify" for argv in calls)
