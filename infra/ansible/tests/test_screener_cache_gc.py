#!/usr/bin/env python3
"""Behavior tests for the screener BuildKit cache cleanup script.

Uses a fake ``docker`` on PATH that records every invocation, so the tests prove
exactly what the script will and will not ask Docker to delete.
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "roles" / "screener_worker"
SCRIPT = ROOT / "files" / "ditto-screener-cache-gc.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
echo "$*" >> "$FAKE_DOCKER_LOG"
case "$1" in
  version) [[ "${FAKE_DOCKER_VERSION_FAIL:-}" == 1 ]] && { echo "cannot connect" >&2; exit 1; }
           echo 28.0.0 ;;
  info) echo /nonexistent ;;
  system) [[ "${FAKE_DOCKER_DF_FAIL:-}" == 1 ]] && { echo "df boom" >&2; exit 1; }
          echo "Build Cache 12 3 57.3GB 41.38GB" ;;
  builder) [[ "${FAKE_DOCKER_PRUNE_FAIL:-}" == 1 ]] && { echo "boom" >&2; exit 1; }
           echo "Total: 1GB" ;;
  *) echo "unexpected docker call: $*" >&2; exit 99 ;;
esac
"""


class CacheGcTest(unittest.TestCase):
    def run_gc(self, **overrides):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            docker = bindir / "docker"
            docker.write_text(FAKE_DOCKER)
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            log = Path(tmp) / "docker.log"
            env = {
                "PATH": f"{bindir}:{os.environ['PATH']}",
                "FAKE_DOCKER_LOG": str(log),
                "DOCKER_HOST": "unix:///run/ditto-screener-docker/docker.sock",
                "SCREENER_CACHE_GC_KEEP_STORAGE": "40GB",
                "SCREENER_CACHE_GC_MIN_AGE": "1h",
            }
            env.update(overrides)
            proc = subprocess.run(
                ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
            )
            calls = log.read_text().splitlines() if log.exists() else []
            return proc, calls

    def test_prunes_only_build_cache_with_budget_and_age(self):
        proc, calls = self.run_gc()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        prunes = [c for c in calls if c.startswith("builder prune")]
        self.assertEqual(
            prunes, ["builder prune --force --keep-storage 40GB --filter until=1h"]
        )
        # Nothing else may mutate the daemon: no system/image/container/volume
        # prune, and never --all.
        for call in calls:
            self.assertNotIn("--all", call)
            self.assertFalse(call.startswith(("system prune", "image", "container", "volume", "rm")), call)

    def test_empty_min_age_means_no_age_floor(self):
        proc, calls = self.run_gc(SCREENER_CACHE_GC_MIN_AGE="")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        prunes = [c for c in calls if c.startswith("builder prune")]
        self.assertEqual(prunes, ["builder prune --force --keep-storage 40GB"])
        self.assertIn("min-age=none", proc.stdout)

    def test_unreachable_executor_fails_before_any_prune(self):
        proc, calls = self.run_gc(FAKE_DOCKER_VERSION_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not reachable", proc.stderr)
        self.assertFalse([c for c in calls if c.startswith("builder prune")])

    def test_docker_system_df_failure_fails_before_prune(self):
        proc, calls = self.run_gc(FAKE_DOCKER_DF_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("docker system df failed", proc.stderr)
        self.assertFalse([c for c in calls if c.startswith("builder prune")])

    def test_df_logs_a_stat_able_path_even_if_docker_root_is_unreadable(self):
        # The fake daemon reports a nonexistent DockerRootDir (as the unit user
        # cannot see beneath the 0700 executor home); disk usage must still be
        # logged via the configured df path.
        proc, _ = self.run_gc(SCREENER_CACHE_GC_DF_PATH="/")
        self.assertIn("before: df -h /", proc.stdout)
        self.assertIn("Filesystem", proc.stdout)

    def test_df_failure_warns_instead_of_skipping_silently(self):
        proc, _ = self.run_gc(SCREENER_CACHE_GC_DF_PATH="/nonexistent/path")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("WARNING: df -h /nonexistent/path failed (before)", proc.stdout)

    def test_logs_before_and_after_state_and_policy(self):
        proc, _ = self.run_gc()
        for needle in ("policy keep-storage=40GB min-age=1h", "before: docker system df", "after: docker system df", "done"):
            self.assertIn(needle, proc.stdout)

    def test_dry_run_never_prunes(self):
        proc, calls = self.run_gc(SCREENER_CACHE_GC_DRY_RUN="true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse([c for c in calls if c.startswith("builder prune")])
        self.assertIn("dry-run: would run docker builder prune", proc.stdout)

    def test_prune_failure_is_a_failed_run(self):
        proc, _ = self.run_gc(FAKE_DOCKER_PRUNE_FAIL="1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("ERROR: docker builder prune failed", proc.stderr)

    def test_invalid_policy_fails_closed_before_touching_docker(self):
        for name, value in (
            ("SCREENER_CACHE_GC_KEEP_STORAGE", "40GB --all"),
            ("SCREENER_CACHE_GC_KEEP_STORAGE", ""),
            ("SCREENER_CACHE_GC_MIN_AGE", "yesterday"),
            ("SCREENER_CACHE_GC_MIN_AGE", "6h;rm"),
            ("SCREENER_CACHE_GC_DRY_RUN", "maybe"),
        ):
            with self.subTest(name=name, value=value):
                proc, calls = self.run_gc(**{name: value})
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(calls, [])

    def test_requires_explicit_rootless_docker_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"PATH": os.environ["PATH"], "HOME": tmp,
                   "SCREENER_CACHE_GC_KEEP_STORAGE": "40GB",
                   "SCREENER_CACHE_GC_MIN_AGE": "1h"}
            proc = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DOCKER_HOST", proc.stderr)


if __name__ == "__main__":
    unittest.main()
