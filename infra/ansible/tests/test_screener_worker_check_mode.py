#!/usr/bin/env python3
"""Check-mode tests for the screener_worker role.

Runs the real role under ``ansible-playbook --check --diff`` against localhost
fixtures for a fresh, a legacy (retired checkout, no executor), and a converged
host, and proves check mode left each fixture untouched. Fake ``gcloud`` and
``uv`` stand in for Secret Manager and the uv install; the executor group and
units are deliberately absent. Needs the pinned Ansible on the interpreter:

  uvx --from ansible-core==2.21.2 python3 tests/test_screener_worker_check_mode.py
"""

import getpass
import grp
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROLES = Path(__file__).resolve().parents[1] / "roles"
ROLE = ROLES / "screener_worker"
ANSIBLE_PLAYBOOK = Path(sys.executable).with_name("ansible-playbook")
ABSENT = "ditto-check-mode-absent"
DEFERRED = f"CHECK MODE: {ABSENT}-docker was neither installed nor probed"

PLAYBOOK = """
- hosts: localhost
  connection: local
  gather_facts: false
  become: false
  roles:
    - screener_worker
"""


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else b""
        for path in sorted(root.rglob("*"))
    }


class ScreenerWorkerCheckModeTest(unittest.TestCase):
    def check(self, prepare, **overrides) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bindir = tmp / "bin"
            bindir.mkdir()
            for name, out in (("gcloud", "fixture-mnemonic"), ("uv", "uv 0.11.28")):
                (bindir / name).write_text(f"#!/bin/sh\necho {out}\n")
                (bindir / name).chmod(0o755)
            repo = tmp / "repo"
            git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
            subprocess.run([*git, "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(
                [
                    *git,
                    "-C",
                    str(repo),
                    "commit",
                    "-q",
                    "--allow-empty",
                    "-m",
                    "fixture",
                ],
                check=True,
            )
            root = tmp / "screener"
            root.mkdir()
            prepare(root, repo)
            before = _snapshot(root)

            source = root / "src" / "workers" / "screener"
            extra_vars = {
                "ansible_python_interpreter": sys.executable,
                "screener_user": getpass.getuser(),
                "screener_group": grp.getgrgid(os.getgid()).gr_name,
                "screener_root": str(root),
                "screener_source_dir": str(source),
                "screener_env_file": str(root / "screener.env"),
                "screener_logs_dir": str(root / "logs"),
                "screener_secrets_dir": str(root / "secrets"),
                "screener_uv_bin": str(bindir / "uv"),
                "screener_repo_url": str(repo),
                "screener_unit": ABSENT,
                "screener_executor_unit": f"{ABSENT}-docker",
                "screener_executor_group": ABSENT,
                "screener_cache_gc_unit": f"{ABSENT}-gc",
                "screener_cache_gc_bin": str(tmp / "cache-gc"),
                **overrides,
            }
            (tmp / "vars.json").write_text(json.dumps(extra_vars))
            (tmp / "play.yml").write_text(PLAYBOOK)
            env = {
                **os.environ,
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "ANSIBLE_ROLES_PATH": str(ROLES),
                "ANSIBLE_NOCOLOR": "1",
            }
            proc = subprocess.run(
                [
                    str(ANSIBLE_PLAYBOOK),
                    "--check",
                    "--diff",
                    "-i",
                    "localhost,",
                    "-e",
                    f"@{tmp / 'vars.json'}",
                    str(tmp / "play.yml"),
                ],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(_snapshot(root), before, "check mode wrote to the host")
            self.assertIn(DEFERRED, proc.stdout)

    def test_fresh_host(self) -> None:
        self.check(lambda *_: None)

    def test_legacy_host_with_retired_checkout(self) -> None:
        def prepare(root: Path, _repo: Path) -> None:
            src = root / "src"
            subprocess.run(["git", "init", "-q", str(src)], check=True)
            # Unresolvable: check mode must not fetch through the stale origin.
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(src),
                    "remote",
                    "add",
                    "origin",
                    "https://retired.invalid/ditto-subnet.git",
                ],
                check=True,
            )
            venv_bin = src / "workers" / "screener" / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / "ditto-screener").touch()
            (root / "screener.env").write_text("SCREENER_HOTKEY=legacy\n")

        self.check(prepare, screener_hotkey="5Fixture")

    def test_converged_host(self) -> None:
        def prepare(root: Path, repo: Path) -> None:
            subprocess.run(
                ["git", "clone", "-q", str(repo), str(root / "src")], check=True
            )
            venv_bin = root / "src" / "workers" / "screener" / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            (venv_bin / "ditto-screener").touch()
            (root / "screener.env").write_text("SCREENER_HOTKEY=converged\n")

        self.check(prepare, screener_hotkey="5Fixture")


class ScreenerWorkerCheckModeGuardsTest(unittest.TestCase):
    tasks = {
        t["name"]: t for t in yaml.safe_load((ROLE / "tasks" / "main.yml").read_text())
    }

    def test_only_read_only_probes_run_in_check_mode(self) -> None:
        probes = [
            name for name, task in self.tasks.items() if task.get("check_mode") is False
        ]
        self.assertEqual(len(probes), 4, probes)
        for name in probes:
            with self.subTest(name):
                self.assertIn("ansible.builtin.command", self.tasks[name])
                self.assertIs(self.tasks[name]["changed_when"], False)
                self.assertNotIn("notify", self.tasks[name])

    def test_runtime_steps_are_deferred_to_apply(self) -> None:
        for name in (
            "Install and verify the separate rootless build executor",
            "Assert the executor returned the pinned rootless socket",
            "Build the worker venv (uv sync)",
        ):
            with self.subTest(name):
                self.assertIn("not ansible_check_mode", str(self.tasks[name]["when"]))
        self.assertEqual(
            self.tasks[
                "Report that executor verification is deferred to apply in check mode"
            ]["when"],
            "ansible_check_mode",
        )


if __name__ == "__main__":
    unittest.main()
