#!/usr/bin/env python3
"""Prove the real copy task rejects skipped secret fetches before writing files."""

import copy
import subprocess
import tempfile
from pathlib import Path

import yaml

role = Path(__file__).resolve().parents[1] / "roles/postgres_backup/tasks/main.yml"
tasks = yaml.safe_load(role.read_text())[0]["block"]
materialize = next(task for task in tasks if task["name"].startswith("Materialize"))
with tempfile.TemporaryDirectory() as temporary:
    task = copy.deepcopy(materialize)
    # Only the destination is a fixture; the real loop/when/copy expressions are
    # unchanged. No secret-fetch task or host installation task enters this play.
    task["ansible.builtin.copy"]["dest"] = temporary + "/credential"
    fixture = Path(temporary) / "playbook.yml"

    def run(candidate):
        fixture.write_text(
            yaml.safe_dump(
                [
                    {
                        "name": "Missing secret-fetch prerequisite",
                        "hosts": "localhost",
                        "gather_facts": False,
                        "connection": "local",
                        "tasks": [candidate],
                    }
                ]
            )
        )
        return subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(fixture)],
            capture_output=True,
            text=True,
            check=False,
        )

    fixed = run(task)
    assert fixed.returncode != 0
    assert "postgres_backup_secret_reads" in fixed.stdout + fixed.stderr
    # Negative control: the former default([]) quietly skips this same task.
    task["loop"] = "{{ postgres_backup_secret_reads.results | default([]) }}"
    former = run(task)
    assert former.returncode == 0
    assert not (Path(temporary) / "credential").exists()
print("Missing fetch: fixed task rejected; former default skipped; no file writes.")
