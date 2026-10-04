"""Keep recovery credential custody out of ordinary application workflows."""

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
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "GCP_PG_RESTORE_SERVICE_ACCOUNT" in text
    assert "platform-pg-backup-age-identity" in text
    assert "platform-pg-backup-reader-access-key-id" in text
    assert "platform-pg-backup-hippius-access-key-id" not in text
    assert "upload-artifact" not in text
    cleanup = job["steps"][-1]
    assert cleanup["if"] == "always()"
    assert "shred -u" in cleanup["run"]


def test_db_secret_fetch_uses_controller_without_private_identity_or_vm_iam():
    role = ROOT / "infra/ansible/roles/postgres_backup"
    defaults = yaml.safe_load((role / "defaults/main.yml").read_text())
    assert defaults["postgres_backup_enabled"] is False
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
        ROOT / "infra/terraform/stacks/gcp-platform/postgres-backup.tf"
    ).read_text()
    assert '"pg_backup_vm"' not in terraform
    assert "google_secret_manager_secret_version" not in terraform
    assert "assertion.workflow_ref" in terraform
    assert "assertion.repository_id" in terraform
    assert "assertion.event_name in ['schedule', 'workflow_dispatch']" in terraform
