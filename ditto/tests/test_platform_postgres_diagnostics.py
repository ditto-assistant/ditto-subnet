import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]


def test_database_diagnostic_stays_manual_protected_and_read_only() -> None:
    workflow = yaml.load(
        (ROOT / ".github/workflows/platform-postgres-diagnostics.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert not workflow["on"]["workflow_dispatch"]
    job = workflow["jobs"]["diagnose"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "prod"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    commands = [step["run"] for step in job["steps"] if "run" in step]
    assert commands == [
        ".agents/skills/gcloud-ditto-readonly/scripts/inspect_platform_postgres.sh"
    ]
    source = (ROOT / commands[0]).read_text()
    assert "[[ $# == 0 ]]" in source
    assert "gcloud compute ssh ditto-pg-platform" in source
    assert "4*1024*1024" in source
    assert "[-60:]" in source
    assert "STATEMENT:" in source
    for forbidden in [
        "systemctl restart",
        "pg_resetwal",
        "pg_ctl promote",
        ".env",
        "psql ",
    ]:
        assert forbidden not in source

    assert re.search(r"\brm\s+-", source) is None
