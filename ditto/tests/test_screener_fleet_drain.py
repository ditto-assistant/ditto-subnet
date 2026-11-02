"""Lease-aware release drain decisions."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

_PATH = Path(__file__).parents[2] / "scripts" / "screener-fleet-drain.py"
_SPEC = importlib.util.spec_from_file_location("screener_fleet_drain", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
_DRAIN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_DRAIN)


def test_idle_worker_is_ready_to_stop() -> None:
    assert _DRAIN.classify(None, now=1_000) == "ready"


def test_open_lease_with_fresh_progress_waits() -> None:
    assert (
        _DRAIN.classify(
            {"lease_deadline": 2_000, "progress_at": 900},
            now=1_000,
        )
        == "wait"
    )


def test_quiet_stretch_inside_the_lease_is_not_an_orphan() -> None:
    assert (
        _DRAIN.classify(
            {"lease_deadline": 5_000, "progress_at": 1},
            now=1_000,
        )
        == "wait"
    )


def test_expired_lease_without_fresh_progress_is_held_not_killed() -> None:
    assert (
        _DRAIN.classify(
            {"lease_deadline": 1_000, "progress_at": 1},
            now=2_000,
        )
        == "held"
    )


def test_deployed_layout_reads_the_worker_journal_directory(tmp_path: Path) -> None:
    """Match the systemd unit: journal and lease sit under the fleet state root.

    The updater state directory is ``<state>/updater``. The worker unit writes
    ``<state>/workers/%i/review.jsonl``, and the lease file is the sibling
    ``active-lease.json``.
    """
    role = Path(__file__).parents[2] / "infra/ansible/roles/hetzner_screener_fleet"
    worker_unit = (role / "templates/ditto-screener-worker@.service.j2").read_text()
    updater_unit = (
        role / "templates/ditto-screener-fleet-auto-update.service.j2"
    ).read_text()
    tasks = (role / "tasks/main.yml").read_text()
    updater = (
        Path(__file__).parents[2] / "scripts/screener-fleet-auto-update.sh"
    ).read_text()
    assert (
        "SCREENER_REVIEW_JOURNAL_FILE="
        "{{ screener_fleet_state_dir }}/workers/%i/review.jsonl" in worker_unit
    )
    assert "SCREENER_FLEET_STATE_DIR={{ screener_fleet_state_dir }}" in updater_unit
    assert "screener-fleet-drain.py" in tasks
    assert '"$FLEET_STATE_DIR/workers/$index/active-lease.json"' in updater

    fleet_state = tmp_path / "var/lib/ditto-screener-fleet"
    updater_dir = fleet_state / "updater"
    worker_dir = fleet_state / "workers/1"
    updater_dir.mkdir(parents=True)
    worker_dir.mkdir(parents=True)
    shutil.copy(_PATH, updater_dir / "screener-fleet-drain.py")
    (worker_dir / "review.jsonl").write_text("", encoding="utf-8")
    lease = worker_dir / "active-lease.json"
    lease.write_text(
        json.dumps({"lease_deadline": 5_000, "progress_at": 1_000}),
        encoding="utf-8",
    )
    wrong = updater_dir / "workers/1/active-lease.json"
    assert not wrong.exists()
    result = subprocess.run(
        [
            "python3",
            str(updater_dir / "screener-fleet-drain.py"),
            "--lease",
            str(lease),
            "--now",
            "1500",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "wait"


def test_progress_past_the_deadline_keeps_waiting() -> None:
    assert (
        _DRAIN.classify(
            {"lease_deadline": 1_000, "progress_at": 1_900},
            now=2_000,
        )
        == "wait"
    )
