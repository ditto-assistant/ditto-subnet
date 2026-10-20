"""Read a GCP converge playbook behind its empty-group guard."""

from pathlib import Path
from typing import Any

import yaml


def converge_play(path: Path) -> dict[str, Any]:
    """The playbook's one converge play, after asserting its guard.

    Every ``gcp-*.yml`` playbook first imports ``require-hosts.yml`` for the
    group its play targets, so a run that resolves no hosts fails instead of
    converging nothing.
    """
    guard, *plays = yaml.safe_load(path.read_text())
    (play,) = plays
    assert guard == {
        "ansible.builtin.import_playbook": "require-hosts.yml",
        "vars": {"required_group": play["hosts"]},
    }
    return play
