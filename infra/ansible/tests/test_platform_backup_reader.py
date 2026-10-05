"""Render actual Platform configuration for every reader/avatar combination."""

import subprocess
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

import yaml
from jinja2 import Environment, meta

ROOT = Path(__file__).resolve().parents[3]


def render_case(reader_enabled, avatars_enabled):
    source = (
        ROOT / "infra/ansible/roles/platform_app/templates/platform.env.j2"
    ).read_text()
    env = Environment()
    env.filters.update(bool=bool, quote=str)
    context: dict[str, object] = dict.fromkeys(
        meta.find_undeclared_variables(env.parse(source)), ""
    )
    context.update(
        platform_database_backup_reader_enabled=reader_enabled,
        platform_hippius_bucket="synthetic-avatars" if avatars_enabled else "",
        platform_secrets=defaultdict(
            str,
            {
                "hippius_access_key_id": "hip_synthetic_avatar"
                if avatars_enabled
                else "",
                "database_backup_reader_access": "hip_synthetic_reader",
                "database_backup_reader_secret": "synthetic_reader_secret",
            },
        ),
    )
    rendered = env.from_string(source).render(context)
    assert (
        "DATABASE_BACKUP_READER_ACCESS_KEY_ID=hip_synthetic_reader" in rendered
    ) == reader_enabled
    assert (
        "DATABASE_BACKUP_READER_SECRET_ACCESS_KEY=synthetic_reader_secret" in rendered
    ) == reader_enabled
    assert ("HIPPIUS_BUCKET=synthetic-avatars" in rendered) == avatars_enabled


class ReaderEnvironmentTest(unittest.TestCase):
    def test_enabled_reader_rejects_empty_or_missing_fetch_results(self):
        tasks = yaml.safe_load(
            (ROOT / "infra/ansible/roles/platform_app/tasks/main.yml").read_text()
        )
        validation = next(
            task
            for task in tasks
            if task["name"]
            == "Validate the enabled backup metadata reader before rendering"
        )
        fold = next(
            task
            for task in tasks
            if task["name"]
            == "Fold only the read-only backup pair into Platform config"
        )
        references = next(
            task
            for task in tasks
            if task["name"]
            == "Validate backup reader secret references before fetching"
        )
        names = [task["name"] for task in tasks]
        self.assertLess(
            names.index(references["name"]),
            names.index("Read the separate backup metadata reader access key"),
        )
        self.assertLess(
            names.index("Fold Hippius credentials into the secrets map"),
            names.index(validation["name"]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "reader.yml"

            def run(task, enabled, access, secret, overrides=None):
                config = {
                    "platform_database_backup_secret_project": "ditto-subnet",
                    "secret_database_backup_reader_access_key_id": (
                        "platform-pg-backup-reader-access-key-id"
                    ),
                    "secret_database_backup_reader_secret_access_key": (
                        "platform-pg-backup-reader-secret-access-key"
                    ),
                    **(overrides or {}),
                }
                fixture.write_text(
                    yaml.safe_dump(
                        [
                            {
                                "hosts": "localhost",
                                "gather_facts": False,
                                "connection": "local",
                                "vars": {
                                    "platform_database_backup_reader_enabled": enabled,
                                    "platform_backup_reader_access": access,
                                    "platform_backup_reader_secret": secret,
                                    "platform_secrets": {
                                        "hippius_access_key_id": "avatar-access",
                                        "hippius_secret_access_key": "avatar-secret",
                                    },
                                    **config,
                                },
                                "tasks": [task],
                            }
                        ]
                    )
                )
                return subprocess.run(
                    ["ansible-playbook", "-i", "localhost,", str(fixture)],
                    capture_output=True,
                    text=True,
                    check=False,
                ).returncode

            good = {"stdout": "synthetic-reader"}
            good_secret = {"stdout": "synthetic-reader-secret"}
            self.assertEqual(run(validation, True, good, good_secret), 0)
            for invalid in ({"stdout": ""}, {"stdout": "   "}, {}):
                for access, secret in ((invalid, good), (good, invalid)):
                    with self.subTest(access=access, secret=secret):
                        self.assertNotEqual(run(validation, True, access, secret), 0)
            self.assertEqual(run(validation, False, {}, {}), 0)
            self.assertEqual(run(references, True, good, good_secret), 0)
            for field in (
                "secret_database_backup_reader_access_key_id",
                "secret_database_backup_reader_secret_access_key",
            ):
                for writer in (
                    "platform-pg-backup-hippius-access-key-id",
                    "platform-pg-backup-hippius-secret-access-key",
                ):
                    with self.subTest(field=field, writer=writer):
                        self.assertNotEqual(
                            run(references, True, good, good_secret, {field: writer}), 0
                        )
            self.assertNotEqual(
                run(
                    references,
                    True,
                    good,
                    good_secret,
                    {"platform_database_backup_secret_project": "ditto-app-dev"},
                ),
                0,
            )
            for shared in ("avatar-access", "avatar-secret"):
                for access, secret in (
                    ({"stdout": shared}, good_secret),
                    (good, {"stdout": shared}),
                ):
                    with self.subTest(shared=shared, access=access, secret=secret):
                        self.assertNotEqual(run(validation, True, access, secret), 0)
            self.assertNotEqual(run(validation, True, good, good), 0)
            former_validation = {
                **validation,
                "ansible.builtin.assert": {
                    "that": validation["ansible.builtin.assert"]["that"][:2]
                },
            }
            self.assertEqual(
                run(former_validation, True, {"stdout": "avatar-access"}, good_secret),
                0,
            )
            # The former fold-only path accepts the empty pair: this is a real
            # negative control using the unchanged materialization task.
            self.assertEqual(run(fold, True, {"stdout": ""}, {"stdout": ""}), 0)

    def test_reader_and_avatar_configuration_are_independent(self):
        for reader in (True, False):
            for avatars in (True, False):
                with self.subTest(reader=reader, avatars=avatars):
                    render_case(reader, avatars)


if __name__ == "__main__":
    unittest.main()
