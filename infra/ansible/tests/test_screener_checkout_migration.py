#!/usr/bin/env python3
"""The screener checkout is replaced only for a different repository, safely.

An SSH origin of the configured HTTPS repository once looked "retired" to a
literal URL comparison, so the role deleted the live checkout (and its in-tree
venv) and a later failure left systemd with no entrypoint. Remotes must compare
by repository identity, and a genuine migration must stage, verify, and swap
rather than delete the service path.
"""

import importlib.util
import re
import unittest
from pathlib import Path

ROLE = Path(__file__).resolve().parents[1] / "roles" / "screener_worker"
TASKS = (ROLE / "tasks" / "main.yml").read_text()

_spec = importlib.util.spec_from_file_location(
    "git_repo_identity", ROLE / "filter_plugins" / "git_repo_identity.py"
)
_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plugin)
git_repo_identity = _plugin.git_repo_identity

REPO = "https://github.com/ditto-assistant/ditto-subnet.git"


class RepoIdentityTest(unittest.TestCase):
    def test_equivalent_remotes_share_one_identity(self) -> None:
        for remote in (
            REPO,
            "https://github.com/ditto-assistant/ditto-subnet",
            "https://github.com/ditto-assistant/ditto-subnet/",
            "https://github.com/ditto-assistant/ditto-subnet.git/",
            "https://GitHub.com/Ditto-Assistant/Ditto-Subnet.git",
            "https://x-access-token@github.com/ditto-assistant/ditto-subnet.git",
            "git@github.com:ditto-assistant/ditto-subnet.git",
            "git@github.com:ditto-assistant/ditto-subnet",
            "ssh://git@github.com/ditto-assistant/ditto-subnet.git",
            "ssh://git@github.com:22/ditto-assistant/ditto-subnet.git",
            f"  {REPO}\n",
        ):
            with self.subTest(remote=remote):
                self.assertEqual(
                    git_repo_identity(remote), "github.com/ditto-assistant/ditto-subnet"
                )

    def test_a_different_repository_differs(self) -> None:
        for remote in (
            "git@github.com:ditto-assistant/ditto-screener.git",
            "https://github.com/ditto-assistant/ditto-screener.git",
            "https://github.com/someone-else/ditto-subnet.git",
            "https://gitlab.com/ditto-assistant/ditto-subnet.git",
        ):
            with self.subTest(remote=remote):
                self.assertNotEqual(git_repo_identity(remote), git_repo_identity(REPO))

    def test_registered_as_an_ansible_filter(self) -> None:
        filters = _plugin.FilterModule().filters()
        self.assertIs(filters["git_repo_identity"], git_repo_identity)


class CheckoutMigrationTasksTest(unittest.TestCase):
    def test_origins_compare_by_identity_on_both_sides(self) -> None:
        self.assertIn(
            "screener_existing_origin.stdout | git_repo_identity"
            " != screener_repo_url | git_repo_identity",
            TASKS,
        )
        self.assertNotIn("stdout | trim != screener_repo_url", TASKS)

    def test_live_checkout_is_never_deleted(self) -> None:
        live = re.compile(r'path: "\{\{ screener_root \}\}/src"\s+state: absent')
        self.assertIsNone(live.search(TASKS))

    def test_migration_stages_verifies_carries_venv_then_swaps(self) -> None:
        steps = [
            'dest: "{{ screener_root }}/src.next"',
            '"{{ screener_staged_source_dir }}/uv.lock"',
            '"{{ screener_staged_source_dir }}/scripts/install-rootless-docker.sh"',
            '"{{ screener_staged_venv }}"',
            "mv -T src src.prev && { mv -T src.next src || { mv -T src.prev src;",
            'dest: "{{ screener_root }}/src"',
        ]
        offsets = [TASKS.index(step) for step in steps]
        self.assertEqual(offsets, sorted(offsets))

    def test_a_migration_still_triggers_executor_install_and_venv_sync(self) -> None:
        self.assertIn(
            "changed_when: screener_clone.changed"
            " or screener_migrated_clone is changed",
            TASKS,
        )
        self.assertIn("screener_clone.changed or not screener_venv_before_sync", TASKS)


if __name__ == "__main__":
    unittest.main()
