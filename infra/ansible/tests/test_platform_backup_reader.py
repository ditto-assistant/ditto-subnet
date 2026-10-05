"""Render actual Platform configuration for every reader/avatar combination."""

import unittest
from collections import defaultdict
from pathlib import Path

from jinja2 import Environment, meta

ROOT = Path(__file__).resolve().parents[3]


def render_case(reader_enabled, avatars_enabled):
    source = (
        ROOT / "infra/ansible/roles/platform_app/templates/platform.env.j2"
    ).read_text()
    env = Environment()
    env.filters.update(bool=bool, quote=str)
    context = dict.fromkeys(meta.find_undeclared_variables(env.parse(source)), "")
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
    def test_reader_and_avatar_configuration_are_independent(self):
        for reader in (True, False):
            for avatars in (True, False):
                with self.subTest(reader=reader, avatars=avatars):
                    render_case(reader, avatars)


if __name__ == "__main__":
    unittest.main()
