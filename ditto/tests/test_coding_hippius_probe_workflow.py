from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW = ROOT / ".github/workflows/coding-hippius-probe.yml"


def _triggers(workflow: dict) -> dict:
    return workflow.get("on", workflow[True])


def test_live_probe_is_manual_main_only_and_peyton_gated() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    triggers = _triggers(workflow)
    assert set(triggers) == {"workflow_dispatch"}
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"confirmation"}
    probe = workflow["jobs"]["probe"]
    assert probe["environment"] == "infra-apply"
    assert "github.ref == 'refs/heads/main'" in probe["if"]
    assert "PROBE HIPPIUS CODING STORAGE" in probe["if"]
    assert probe["permissions"] == {"contents": "read", "id-token": "write"}


def test_live_probe_uses_exact_source_and_fixed_hippius_authorities() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["probe"]["steps"]
    checkout = steps[0]
    assert checkout["with"] == {
        "ref": "${{ github.sha }}",
        "persist-credentials": False,
    }
    clean = next(
        step
        for step in steps
        if step.get("name") == "Verify exact clean source before authentication"
    )
    auth = next(
        step
        for step in steps
        if step.get("uses", "").startswith("google-github-actions/auth@")
    )
    assert steps.index(clean) < steps.index(auth)
    assert 'test -z "$(git status --porcelain)"' in clean["run"]
    probe = next(
        step
        for step in steps
        if step.get("name") == "Run the exact-source protected capability probe"
    )
    assert probe["env"] == {
        "DITTO_CODING_HIPPIUS_ENDPOINT_URL": "https://s3.hippius.com",
        "DITTO_CODING_HIPPIUS_REGION": "decentralized",
        "DITTO_CODING_HIPPIUS_PRIVATE_INPUT_BUCKET": (
            "ditto-subnet-coding-private-input"
        ),
        "DITTO_CODING_HIPPIUS_SEALED_EVIDENCE_BUCKET": (
            "ditto-subnet-coding-sealed-evidence"
        ),
    }
    command = probe["run"]
    assert 'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"' in command
    assert "git diff --quiet" in command
    assert "git diff --cached --quiet" in command
    assert "git status --porcelain" not in command
    assert "--confirm 'PROBE HIPPIUS CODING STORAGE'" in command
    assert "--confirm 'PROFILE HIPPIUS CODING STORAGE'" in command
    assert "weight_eligible" in command


def test_live_probe_binds_exact_secret_versions_without_logging_values() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["probe"]["steps"]
    auth = next(
        step
        for step in steps
        if step.get("uses", "").startswith("google-github-actions/auth@")
    )
    assert auth["with"]["service_account"] == "${{ secrets.GCP_TF_APPLY_SA }}"
    command = next(
        step
        for step in steps
        if step.get("name") == "Run the exact-source protected capability probe"
    )["run"]
    for secret in (
        "platform-coding-catalog-access-key",
        "platform-coding-catalog-secret-key",
        "platform-coding-catalog-curator-access-key",
        "platform-coding-catalog-curator-secret-key",
        "platform-coding-hippius-evidence-access-key",
        "platform-coding-hippius-evidence-secret-key",
    ):
        assert secret in command
    assert "gcloud secrets versions list" in command
    assert 'gcloud secrets versions access "$version"' in command
    assert "set -x" not in command
    assert "GITHUB_ENV" not in command
    assert "GITHUB_OUTPUT" not in command
    assert "redacted probe output contains forbidden authority" in command


def test_live_probe_artifact_contains_only_redacted_evidence() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    upload = next(
        step
        for step in workflow["jobs"]["probe"]["steps"]
        if "upload-artifact@" in step.get("uses", "")
    )
    assert upload["name"] == "Retain only redacted probe evidence"
    assert upload["if"] == "always()"
    assert upload["with"]["retention-days"] == 7
    assert upload["with"]["if-no-files-found"] == "warn"
    paths = set(upload["with"]["path"].splitlines())
    assert paths == {
        "${{ runner.temp }}/hippius-coding-probe.json",
        "${{ runner.temp }}/hippius-coding-provider-profile.json",
        "${{ runner.temp }}/hippius-coding-secret-metadata.tsv",
    }
