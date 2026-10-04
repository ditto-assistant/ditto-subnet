#!/usr/bin/env python3
"""Exercise low-space refusal, encryption failure, and exact retention scopes."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "backup", ROOT / "ansible/roles/postgres_backup/files/backup.py"
)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
spec = importlib.util.spec_from_file_location(
    "drill", ROOT / "scripts/platform-pg-restore-drill.py"
)
drill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(drill)


def keys(prefix, instant):
    base = prefix + "/" + instant.strftime("%Y/%m/%d/")
    stamp = instant.strftime("%Y%m%dT%H%M%SZ")
    return [
        base + f"ditto_platform_prod-{stamp}.dump.age",
        base + f"globals-{stamp}.sql.age",
        base + f"manifest-{stamp}.json",
    ]


class BackupTest(unittest.TestCase):
    def test_real_df_stub_refuses_before_any_export(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            stub = staging / "df"
            stub.write_text(
                "#!/bin/sh\n"
                "echo 'Filesystem 1024-blocks Used Available Capacity Mounted'\n"
                "echo 'disk 104857600 94371840 10485760 90% /var/tmp'\n"
            )
            stub.chmod(0o755)
            with (
                patch.dict(
                    os.environ, {"PATH": str(staging) + ":" + os.environ["PATH"]}
                ),
                self.assertRaisesRegex(RuntimeError, "insufficient"),
            ):
                backup.guard_space(1024**3, staging)

    def test_space_guard_accepts_boundary_and_rejects_one_byte_under(self):
        with patch.object(
            subprocess,
            "check_output",
            return_value="header\ndisk 104857600 0 22020096 0% disk\n",
        ):
            backup.guard_space(1024**3, Path("/var/tmp"))
            with self.assertRaises(RuntimeError):
                backup.guard_space(1024**3 + 1, Path("/var/tmp"))

    def test_retention_never_deletes_other_keys_or_incomplete_groups(self):
        now = datetime(2026, 10, 4, tzinfo=UTC)
        runs = [keys("daily", now - timedelta(days=i)) for i in range(32)]
        unexpected = [
            "daily/old.dump",
            "daily/2026/09/03/globals-20260903T000000Z.dump.age",
            "monthly/2026/09/02/manifest-20260902T000000Z.json",
            "daily/2026/09/04/manifest-20260903T000000Z.json",
        ]
        incomplete = keys("daily", now - timedelta(days=100))[:1]
        victims = backup.retention_keys(sum(runs, []) + unexpected + incomplete)
        self.assertEqual(set(victims), set(runs[30] + runs[31]))
        self.assertEqual(len(victims), 6)
        self.assertTrue(all("/manifest-" in victims[index] for index in (0, 3)))

    def test_monthly_retention_keeps_twelve_distinct_months(self):
        months = [
            datetime(2025 + (i // 12), 1 + i % 12, 1, tzinfo=UTC) for i in range(14)
        ]
        duplicate = months[-1] + timedelta(hours=1)
        all_keys = sum(
            [keys("monthly", instant) for instant in months + [duplicate]], []
        )
        victims = backup.retention_keys(all_keys)
        self.assertEqual(
            set(victims),
            set(
                keys("monthly", months[0])
                + keys("monthly", months[1])
                + keys("monthly", months[-1])
            ),
        )

    def test_pg_dump_failure_is_not_hidden_by_a_successful_encryptor(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "encrypted.age"
            age = Path(directory) / "age"
            age.write_text(
                "#!/bin/sh\ncat >/dev/null\nprintf 'age-encryption.org/v1\\nfixture'\n"
            )
            age.chmod(0o755)
            with (
                patch.dict(os.environ, {"PATH": directory + ":" + os.environ["PATH"]}),
                self.assertRaisesRegex(RuntimeError, "export failed"),
            ):
                backup.encrypted_dump(
                    ["sh", "-c", "printf PGDMP; exit 9"],
                    Path("recipient"),
                    destination,
                )

    def test_secret_file_mode_and_symlink_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base / "credential"
            target.write_text("synthetic-only")
            target.chmod(0o644)
            with self.assertRaises(RuntimeError):
                backup.protected_file(base, target.name)
            target.chmod(0o600)
            self.assertEqual(backup.protected_file(base, target.name), target)
            (base / "link").symlink_to(target)
            with self.assertRaises(RuntimeError):
                backup.protected_file(base, "link")

    def test_upload_refuses_plaintext_before_creating_any_remote_object(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "archive.dump.age"
            file.write_bytes(b"PGDMP" + b"unencrypted database" * 5)
            client = object.__new__(backup.S3)
            with self.assertRaisesRegex(RuntimeError, "non-age"):
                client.upload("daily/archive.dump.age", file)

    def manifest(self):
        return {
            "format_version": 1,
            "database": "ditto_platform_prod",
            "started_at": "2026-10-04T06:30:00+00:00",
            "completed_at": "2026-10-04T06:40:00+00:00",
            "server_version_num": 170004,
            "alembic_version": "abcdef",
            "row_counts": dict.fromkeys(backup.TABLES, 100),
            "objects": [
                {
                    "name": "ditto_platform_prod-20261004T063000Z.dump.age",
                    "sha256": "a" * 64,
                    "size": 100,
                },
                {
                    "name": "globals-20261004T063000Z.sql.age",
                    "sha256": "b" * 64,
                    "size": 100,
                },
            ],
        }

    def test_drill_rejects_stale_future_and_substituted_manifests(self):
        manifest = self.manifest()
        now = datetime(2026, 10, 4, 9, tzinfo=UTC)
        self.assertEqual(drill.validate_manifest(manifest, "20261004T063000Z", now), 17)
        for wrong_now in (now + timedelta(days=2), now - timedelta(days=1)):
            with self.assertRaises(ValueError):
                drill.validate_manifest(manifest, "20261004T063000Z", wrong_now)
        manifest["objects"][0]["name"] = "../source.dump"
        with self.assertRaises(ValueError):
            drill.validate_manifest(manifest, "20261004T063000Z", now)

    def test_drill_count_tolerance_and_empty_restore(self):
        drill.compare_counts({"agents": 100}, {"agents": 105})
        for actual in ({"agents": 106}, {"agents": 0}, {"unknown": 100}):
            with self.assertRaises(ValueError):
                drill.compare_counts({"agents": 100}, actual)


if __name__ == "__main__":
    unittest.main()
