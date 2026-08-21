from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_preview_workflow_never_publishes_compat_or_prod() -> None:
    text = (ROOT / ".github/workflows/preview.yml").read_text()
    workflow = yaml.safe_load(text)
    assert workflow["name"] == "Preview controls"
    assert "--tag" not in text
    assert "compat-2" not in text
    assert "environment: prod" not in text
    triggers = workflow.get("on", workflow[True])
    dispatch = triggers["workflow_dispatch"]["inputs"]
    assert "profiles" in dispatch
    assert "cheatcodes" in workflow["jobs"]
    assert "teardown" not in workflow["jobs"]
    assert "uv run pytest ditto/tests/preview -q" in text
    assert "uv run python -m ditto.preview compose" in text
    assert "pull-requests: read" in text
    assert "pull-requests: write" not in text
    assert "gh api --paginate" in text
    assert "ref: ${{ needs.plan.outputs.sha }}" in text
