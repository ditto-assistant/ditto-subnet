"""Keep recovery credential custody out of ordinary application workflows."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_restore_workflow_uses_main_prod_and_only_reader_credentials():
    text = (ROOT / ".github/workflows/platform-pg-restore-drill.yml").read_text()
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "17 8 * * 0"}]
    job = workflow["jobs"]["restore"]
    assert job["environment"] == "prod"
    assert "runner.temp" not in str(job.get("env", {}))
    assert "$RUNNER_TEMP/platform-pg-restore" in job["steps"][0]["run"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "GCP_PG_RESTORE_SERVICE_ACCOUNT" in text
    assert "platform-pg-backup-age-identity" in text
    assert "platform-pg-backup-reader-access-key-id" in text
    assert text.count("--project=ditto-subnet") == 3
    assert "platform-pg-backup-hippius-access-key-id" not in text
    assert "platform-pg-backup-hippius-secret-access-key" not in text
    assert "upload-artifact" not in text
    assert "--project apps/platform" not in text
    install = next(
        step
        for step in job["steps"]
        if step.get("name") == "Install only hash-locked restore dependencies"
    )
    assert "--require-hashes" in install["run"]
    assert "--only-binary=:all:" in install["run"]
    assert "uv --no-config" in install["run"]
    execute = next(
        step
        for step in job["steps"]
        if step.get("name")
        == "Restore verified encrypted daily backup in an isolated database"
    )
    assert 'bin/python" -I' in execute["run"]
    assert "platform-pg-restore-code/scripts/" in execute["run"]
    cleanup = job["steps"][-1]
    assert cleanup["if"] == "always()"
    assert "shred -u" in cleanup["run"]


def test_db_secret_fetch_uses_controller_without_private_identity_or_vm_iam():
    role = ROOT / "infra/ansible/roles/postgres_backup"
    defaults = yaml.safe_load((role / "defaults/main.yml").read_text())
    assert defaults["postgres_backup_enabled"] is False
    assert defaults["postgres_backup_secret_project"] == "ditto-subnet"
    assert set(defaults["postgres_backup_secret_ids"].values()) == {
        "platform-pg-backup-hippius-access-key-id",
        "platform-pg-backup-hippius-secret-access-key",
        "platform-pg-backup-age-recipient",
    }
    tasks = yaml.safe_load((role / "tasks/main.yml").read_text())[0]["block"]
    fetch = next(task for task in tasks if "Fetch only" in task["name"])
    assert fetch["delegate_to"] == "localhost"
    assert fetch["become"] is False
    assert fetch["no_log"] is True
    terraform = (
        ROOT / "infra/terraform/stacks/gcp-subnet-recovery/recovery.tf"
    ).read_text()
    assert '"pg_backup_vm"' not in terraform
    assert "google_secret_manager_secret_version" not in terraform
    assert "assertion.workflow_ref" in terraform
    assert "assertion.repository_id" in terraform
    assert "assertion.event_name in ['schedule', 'workflow_dispatch']" in terraform


def test_recovery_ci_has_no_application_credentials_or_owner_bootstrap_choice():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/infra-plan-apply.yml").read_text()
    )
    triggers = workflow.get("on", workflow.get(True))
    choices = triggers["workflow_dispatch"]["inputs"]["root"]["options"]
    assert "gcp-subnet-recovery" in choices
    assert "gcp-subnet-bootstrap" not in choices
    for job, command in (("plan", "plan -out=tfplan"), ("apply", "apply tfplan")):
        steps = workflow["jobs"][job]["steps"]
        dedicated = [
            step
            for step in steps
            if step.get("if") == "inputs.root == 'gcp-subnet-recovery'"
        ]
        execute = next(step for step in dedicated if command in step["run"])
        assert "env" not in execute
        assert any(
            "check-subnet-recovery-plan.py recovery" in step["run"]
            for step in dedicated
        )
        generic = next(
            step
            for step in steps
            if step.get("name")
            == (
                "Create exact private plan"
                if job == "plan"
                else "Apply the reviewed binary plan"
            )
        )
        assert "inputs.root != 'gcp-subnet-recovery'" in generic["if"]


def test_independent_review_covers_every_restore_import_and_dependency():
    rules = json.loads(
        (ROOT / "infra/github/platform-pg-backup-ruleset.json").read_text()
    )
    assert rules["enforcement"] == "active"
    assert rules["conditions"]["ref_name"]["include"] == ["refs/heads/main"]
    reviewer = rules["rules"][0]["parameters"]["required_reviewers"][0]
    assert reviewer["reviewer"] == {"id": 12645310, "type": "Team"}
    assert reviewer["minimum_approvals"] == 1
    assert set(reviewer["file_patterns"]) == {
        ".github/workflows/platform-pg-restore-drill.yml",
        ".github/workflows/infra-plan-apply.yml",
        "infra/scripts/platform-pg-restore-drill.py",
        "infra/scripts/pg-restore/**",
        "infra/scripts/check-subnet-recovery-plan.py",
        "infra/ansible/roles/postgres_backup/**",
        "infra/terraform/stacks/gcp-subnet-bootstrap/**",
        "infra/terraform/stacks/gcp-subnet-recovery/**",
        "infra/github/platform-pg-backup-ruleset.json",
    }
