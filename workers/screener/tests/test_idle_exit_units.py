"""Every shipped screener unit leaves an idle-exited worker down."""

from __future__ import annotations

from pathlib import Path

import pytest

from ditto_screener.__main__ import IDLE_EXIT_STATUS

ROOT = Path(__file__).resolve().parents[3]
UNITS = (
    ROOT / "workers/screener/deploy/ditto-screener.service",
    ROOT / "infra/ansible/roles/screener_worker/templates/ditto-screener.service.j2",
    ROOT
    / "infra/ansible/roles/hetzner_screener_fleet/templates"
    / "ditto-screener-worker@.service.j2",
)


@pytest.mark.parametrize("unit", UNITS, ids=lambda path: path.name)
def test_unit_restarts_everything_but_an_idle_exit(unit: Path) -> None:
    lines = unit.read_text().splitlines()

    assert "Restart=always" in lines
    assert f"RestartPreventExitStatus={IDLE_EXIT_STATUS}" in lines
    assert f"SuccessExitStatus={IDLE_EXIT_STATUS}" in lines
