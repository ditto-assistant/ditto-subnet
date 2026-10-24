"""Exercise the shipped Hetzner logs CLI through a recorded SSH transport."""

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents/skills/ditto-subnet-release-ops/scripts/read_screener_logs.sh"


class ScreenerLogsHelperTest(unittest.TestCase):
    def invoke(self, *args, overrides=None, failure=False):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            key = tmp / "approved-key"
            key.write_text("fixture key; SSH transport is stubbed")
            calls = tmp / "calls.jsonl"
            ssh = tmp / "ssh"
            ssh.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['SSH_CALLS'], 'a') as stream:
    stream.write(json.dumps(sys.argv[1:]) + '\\n')
if os.environ.get('SSH_FAILURE') == 'yes':
    sys.exit(255)
print('{"MESSAGE":"fixture journal record"}')
""")
            ssh.chmod(0o755)
            env = dict(os.environ)
            env.update(
                PATH=str(tmp) + os.pathsep + env["PATH"],
                SSH_CALLS=str(calls),
                SSH_FAILURE="yes" if failure else "no",
                DITTOBOX_LOGS_USER="logs",
                DITTOBOX_SSH_IDENTITY=str(key),
            )
            env.update(overrides or {})
            result = subprocess.run(
                ["bash", str(SCRIPT), *args], env=env, text=True, capture_output=True
            )
            recorded = (
                [json.loads(row) for row in calls.read_text().splitlines()]
                if calls.exists()
                else []
            )
            return result, recorded

    def test_default_is_logs_on_company_tailnet_with_one_pinned_identity(self):
        result, calls = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 1)
        args = calls[0]
        self.assertIn("logs@100.79.84.29", args)
        self.assertIn("StrictHostKeyChecking=yes", args)
        self.assertIn("BatchMode=yes", args)
        self.assertIn("IdentitiesOnly=yes", args)
        self.assertIn(
            "UserKnownHostsFile=" + str(SCRIPT.parent / "screener_known_hosts"), args
        )
        self.assertEqual(args[args.index("-F") + 1], "/dev/null")
        self.assertEqual(args.count("-i"), 1)
        command = shlex.split(args[-1])
        self.assertEqual(command[0], "journalctl")
        self.assertNotIn("sudo", command)
        self.assertIn("ditto-screener-worker@*.service", command)
        self.assertIn("3600 seconds ago", command)
        self.assertIn("200", command)

    def test_worker_time_and_json_arguments_are_passed_as_data(self):
        result, calls = self.invoke(
            "--worker", "12", "--since", "-15m", "--lines", "3", "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        command = shlex.split(calls[0][-1])
        self.assertIn("ditto-screener-worker@12.service", command)
        self.assertIn("900 seconds ago", command)
        self.assertEqual(command[-1], "json")
        self.assertIn("3", command)

    def test_only_known_read_only_service_units_are_available(self):
        for name, unit in (
            ("agent", "ditto-screener-fleet-agent.service"),
            ("updater", "ditto-screener-fleet-auto-update.service"),
            ("enroll", "ditto-screener-enroll.service"),
            ("egress", "ditto-screener-egress-guard.service"),
        ):
            with self.subTest(name=name):
                result, calls = self.invoke("--service", name)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(unit, shlex.split(calls[0][-1]))

    def test_invalid_or_injected_arguments_never_open_a_connection(self):
        for args in (
            ("--worker", "1; touch /tmp/unwanted"),
            ("--service", "ssh"),
            ("--since", "1h; whoami"),
            ("--lines", "0"),
            ("--lines", "10001"),
            ("--service", "agent", "--worker", "1"),
            ("--lines",),
            ("restart",),
        ):
            with self.subTest(args=args):
                result, calls = self.invoke(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(calls, [])

    def test_missing_identity_and_privileged_username_are_not_fallbacks(self):
        for override in (
            {"DITTOBOX_SSH_IDENTITY": ""},
            {"DITTOBOX_SSH_IDENTITY": "/nonexistent/approved-key"},
            {"DITTOBOX_LOGS_USER": "peyton"},
        ):
            result, calls = self.invoke(overrides=override)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(calls, [])

    def test_failure_preserves_exit_status_and_never_tries_another_host_or_user(self):
        result, calls = self.invoke(failure=True)
        self.assertEqual(result.returncode, 255)
        self.assertEqual(len(calls), 1)
        self.assertIn("omniaura.ai", result.stderr)
        self.assertIn("logs account", result.stderr)
        self.assertIn("No public-host or privileged-user fallback", result.stderr)

    def test_operator_identity_does_not_change_log_user(self):
        result, calls = self.invoke(overrides={"DITTOBOX_USER": "peyton"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("logs@100.79.84.29", calls[0])

    def test_help_needs_no_credentials(self):
        result, calls = self.invoke("--help", overrides={"DITTOBOX_SSH_IDENTITY": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
