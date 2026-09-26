"""The postgres role must prune its own collector logs.

On 2026-09-08 the production database crash-looped for five hours because
12 GB of daily slow-query logs filled the 30 GB boot disk; nothing rotated
them out. These tests pin the retention timer and the bounded parameter
logging so a future role edit cannot silently drop them.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
ROLE = ROOT / "infra/ansible/roles/postgres"


def _defaults() -> dict:
    return yaml.safe_load((ROLE / "defaults/main.yml").read_text())


def test_retention_window_is_bounded_and_default_on() -> None:
    defaults = _defaults()
    assert 1 <= int(defaults["postgres_log_retention_days"]) <= 30
    assert (
        0
        <= int(defaults["postgres_log_compress_after_days"])
        <= int(defaults["postgres_log_retention_days"])
    )
    assert defaults["postgres_tuning"]["log_parameter_max_length"] <= 4096


def test_prune_units_are_installed_enabled_and_run_once() -> None:
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
    names = [task.get("name", "") for task in tasks]
    assert "Install the collector-log retention script" in names
    assert "Enable the collector-log retention timer" in names
    enable = next(
        t for t in tasks if t["name"] == "Enable the collector-log retention timer"
    )
    assert enable["ansible.builtin.systemd"]["enabled"] is True
    assert enable["ansible.builtin.systemd"]["state"] == "started"
    assert any(n.startswith("Prune collector logs once now") for n in names)
    units = (
        "ditto-pg-log-prune.sh",
        "ditto-pg-log-prune.service",
        "ditto-pg-log-prune.timer",
    )
    for unit in units:
        assert (ROLE / "templates" / f"{unit}.j2").is_file()


def test_prune_script_never_touches_the_open_log_and_keeps_within_retention() -> None:
    script = (ROLE / "templates/ditto-pg-log-prune.sh.j2").read_text()
    assert '! -name "$today"' in script
    assert '-mtime +"$RETENTION_DAYS"' in script
    assert "-delete" in script and "gzip" in script
    assert "set -euo pipefail" in script
    service = (ROLE / "templates/ditto-pg-log-prune.service.j2").read_text()
    assert "User=postgres" in service
    assert "ProtectSystem=strict" in service
