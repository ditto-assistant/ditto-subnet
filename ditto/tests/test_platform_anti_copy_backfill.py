import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def _workflow(name: str) -> dict:
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


def test_backfill_stays_manual_protected_and_fixed_command() -> None:
    workflow = _workflow("platform-anti-copy-backfill.yml")
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"mode"}
    assert inputs["mode"]["options"] == ["dry-run", "apply"]
    assert inputs["mode"]["default"] == "dry-run"
    job = workflow["jobs"]["backfill"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "prod"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    remote = [
        step["run"]
        for step in job["steps"]
        if "gcloud compute ssh" in step.get("run", "")
    ]
    assert len(remote) == 1
    # The only remote program is the deployed backfill; the mode is the only
    # interpolated value and it maps to exactly "" or "--apply".
    assert re.findall(r"python \S+", remote[0]) == [
        "python scripts/backfill_reference_fingerprints.py"
    ]
    assert re.findall(r"\$\{\{[^}]*\}\}", remote[0]) == []
    assert 'apply) flag="--apply"' in remote[0]
    assert 'dry-run) flag=""' in remote[0]


def test_reference_refresh_commits_every_loaded_bundle() -> None:
    fingerprint = (ROOT / "apps/platform/ditto/api_server/fingerprint.py").read_text()
    block = fingerprint.split("_REFERENCE_BUNDLES = {", 1)[1].split("}", 1)[0]
    bundles = set(re.findall(r'"(reference_\w+\.bin)"', block))
    assert "reference_line_v2.bin" in bundles
    refresh = (ROOT / ".github/workflows/platform-refresh-anti-copy.yml").read_text()
    committed = set(re.findall(r"ditto/anticopy/(reference_\w+\.bin)", refresh))
    assert bundles <= committed
