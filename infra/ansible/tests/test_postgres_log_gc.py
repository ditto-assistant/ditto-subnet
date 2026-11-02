#!/usr/bin/env python3
"""Behavior tests for the PostgreSQL logging-collector retention script.

Runs the real script against a synthetic log directory, so the tests prove
exactly which files it deletes and — more importantly — which it never will:
the live collector file, anything outside the configured pattern, and anything
at all when the policy is malformed.
"""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "roles" / "postgres"
SCRIPT = ROOT / "files" / "ditto-postgres-log-gc.sh"

DAY = 86400


class PostgresLogGcTest(unittest.TestCase):
    def run_gc(self, tmp, **overrides):
        env = {
            "PATH": os.environ["PATH"],
            "POSTGRES_LOG_GC_DIR": str(tmp),
            "POSTGRES_LOG_GC_RETENTION_DAYS": "7",
            "POSTGRES_LOG_GC_MAX_TOTAL_MB": "6144",
        }
        env.update(overrides)
        return subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
        )

    def write_log(self, tmp, name, age_days=0.0, size=1024):
        path = Path(tmp) / name
        path.write_bytes(b"x" * size)
        when = time.time() - age_days * DAY
        os.utime(path, (when, when))
        return path

    def names(self, tmp):
        return sorted(p.name for p in Path(tmp).iterdir())

    def test_deletes_files_past_the_retention_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_log(tmp, "postgresql-2026-07-21.log", age_days=40)
            self.write_log(tmp, "postgresql-2026-08-30.log", age_days=20)
            self.write_log(tmp, "postgresql-2026-09-20.log", age_days=3)
            self.write_log(tmp, "postgresql-2026-09-23.log", age_days=0)
            proc = self.run_gc(tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                self.names(tmp),
                ["postgresql-2026-09-20.log", "postgresql-2026-09-23.log"],
            )
            self.assertIn("older than 7d", proc.stdout)

    def test_never_deletes_the_live_collector_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            # A stale mtime on the live file (an idle cluster that logged nothing
            # for weeks) must not make it a deletion candidate.
            live = self.write_log(tmp, "postgresql-2026-08-01.log", age_days=30)
            self.write_log(tmp, "postgresql-2026-07-01.log", age_days=60)
            current = Path(tmp) / "current_logfiles"
            current.write_text(f"stderr {live}\n")
            proc = self.run_gc(tmp, POSTGRES_LOG_GC_CURRENT_LOGFILES=str(current))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(live.name, self.names(tmp))
            self.assertNotIn("postgresql-2026-07-01.log", self.names(tmp))
            self.assertIn(f"protecting live collector file {live}", proc.stdout)

    def test_newest_file_survives_even_without_current_logfiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_log(tmp, "postgresql-2026-08-01.log", age_days=30)
            self.write_log(tmp, "postgresql-2026-08-10.log", age_days=25)
            proc = self.run_gc(tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.names(tmp), ["postgresql-2026-08-10.log"])

    def test_unreadable_current_logfiles_warns_and_still_protects_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_log(tmp, "postgresql-2026-08-10.log", age_days=25)
            proc = self.run_gc(
                tmp, POSTGRES_LOG_GC_CURRENT_LOGFILES=str(Path(tmp) / "absent")
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("is not readable", proc.stdout)
            self.assertEqual(self.names(tmp), ["postgresql-2026-08-10.log"])

    def test_size_ceiling_prunes_oldest_first_inside_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            mb = 1024 * 1024
            for day in range(5):
                self.write_log(
                    tmp,
                    f"postgresql-2026-09-1{day}.log",
                    age_days=4 - day,
                    size=mb,
                )
            proc = self.run_gc(tmp, POSTGRES_LOG_GC_MAX_TOTAL_MB="2")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            # Nothing was old enough for the age pass; the ceiling alone reclaimed
            # the three oldest files and stopped at the budget.
            self.assertEqual(
                self.names(tmp),
                ["postgresql-2026-09-13.log", "postgresql-2026-09-14.log"],
            )
            self.assertIn("over the 2MB ceiling", proc.stdout)

    def test_ceiling_keeps_the_live_file_even_when_it_alone_exceeds_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = self.write_log(
                tmp, "postgresql-2026-09-23.log", size=3 * 1024 * 1024
            )
            proc = self.run_gc(tmp, POSTGRES_LOG_GC_MAX_TOTAL_MB="1")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(self.names(tmp), [live.name])
            self.assertIn("only live collector files are left", proc.stdout)

    def test_dry_run_deletes_nothing_but_logs_the_whole_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            mb = 1024 * 1024
            for day in range(4):
                self.write_log(
                    tmp, f"postgresql-2026-09-1{day}.log", age_days=3 - day, size=mb
                )
            self.write_log(tmp, "postgresql-2026-07-01.log", age_days=60)
            proc = self.run_gc(
                tmp,
                POSTGRES_LOG_GC_DRY_RUN="true",
                POSTGRES_LOG_GC_MAX_TOTAL_MB="1",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(self.names(tmp)), 5)
            self.assertIn("dry-run: would delete", proc.stdout)
            # Both passes are rehearsed, not just the first deletion.
            self.assertGreaterEqual(proc.stdout.count("dry-run: would delete"), 3)
            self.assertIn("reclaimed 0 bytes", proc.stdout)

    def test_only_the_configured_pattern_in_the_top_level_is_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_log(tmp, "postgresql-2026-07-01.log", age_days=60)
            keep = self.write_log(tmp, "postgresql.conf.bak", age_days=60)
            nested = Path(tmp) / "archive"
            nested.mkdir()
            nested_log = nested / "postgresql-2026-07-01.log"
            nested_log.write_bytes(b"x")
            os.utime(nested_log, (time.time() - 60 * DAY,) * 2)
            self.write_log(tmp, "postgresql-2026-09-23.log")
            proc = self.run_gc(tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(keep.exists())
            self.assertTrue(nested_log.exists())
            self.assertNotIn("postgresql-2026-07-01.log", self.names(tmp))

    def test_malformed_policy_fails_closed_without_deleting(self):
        cases = (
            ("POSTGRES_LOG_GC_RETENTION_DAYS", "0"),
            ("POSTGRES_LOG_GC_RETENTION_DAYS", "forever"),
            ("POSTGRES_LOG_GC_RETENTION_DAYS", "7 -delete"),
            ("POSTGRES_LOG_GC_MAX_TOTAL_MB", "0"),
            ("POSTGRES_LOG_GC_MAX_TOTAL_MB", ""),
            ("POSTGRES_LOG_GC_MAX_TOTAL_MB", "6GB"),
            ("POSTGRES_LOG_GC_DRY_RUN", "maybe"),
            ("POSTGRES_LOG_GC_GLOB", "../*"),
            ("POSTGRES_LOG_GC_GLOB", "sub/*.log"),
        )
        for name, value in cases:
            with (
                self.subTest(name=name, value=value),
                tempfile.TemporaryDirectory() as tmp,
            ):
                self.write_log(tmp, "postgresql-2026-07-01.log", age_days=60)
                proc = self.run_gc(tmp, **{name: value})
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(
                    self.names(tmp), ["postgresql-2026-07-01.log"], proc.stdout
                )

    def test_missing_log_directory_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_gc(tmp, POSTGRES_LOG_GC_DIR=f"{tmp}/absent")
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("does not exist", proc.stderr)

    def test_requires_an_explicit_directory_and_bounds(self):
        for missing in (
            "POSTGRES_LOG_GC_DIR",
            "POSTGRES_LOG_GC_RETENTION_DAYS",
            "POSTGRES_LOG_GC_MAX_TOTAL_MB",
        ):
            with (
                self.subTest(missing=missing),
                tempfile.TemporaryDirectory() as tmp,
            ):
                env = {
                    "PATH": os.environ["PATH"],
                    "POSTGRES_LOG_GC_DIR": tmp,
                    "POSTGRES_LOG_GC_RETENTION_DAYS": "7",
                    "POSTGRES_LOG_GC_MAX_TOTAL_MB": "6144",
                }
                del env[missing]
                proc = subprocess.run(
                    ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn(missing, proc.stderr)

    def test_reports_policy_and_reclaimed_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_log(tmp, "postgresql-2026-07-01.log", age_days=60, size=4096)
            self.write_log(tmp, "postgresql-2026-09-23.log", size=1024)
            proc = self.run_gc(tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("retention-days=7 max-total-mb=6144", proc.stdout)
            self.assertIn("reclaimed 4096 bytes", proc.stdout)
            self.assertIn("done", proc.stdout)


if __name__ == "__main__":
    unittest.main()
