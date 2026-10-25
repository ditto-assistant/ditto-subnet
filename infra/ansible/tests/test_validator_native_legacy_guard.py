"""Prove gcp-validator.yml refuses the pre-monorepo converge before touching a host."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ANSIBLE = ROOT / "infra/ansible"
PLAYBOOK = ANSIBLE / "playbooks/gcp-validator.yml"

# TEST-NET-1 is unroutable, so any attempt to contact the host would surface as
# UNREACHABLE instead of the guard's refusal.
INVENTORY = """\
all:
  children:
    role_validator:
      hosts:
        ditto-validator-test:
          ansible_host: 192.0.2.1
"""


class ValidatorNativeLegacyGuardTests(unittest.TestCase):
    def run_playbook(self, *extra: str) -> subprocess.CompletedProcess[str]:
        executable = shutil.which("ansible-playbook")
        if executable is None:
            self.skipTest("ansible-playbook is required to exercise the guard")
        with tempfile.TemporaryDirectory() as tmp:
            inventory = Path(tmp) / "inventory.yml"
            inventory.write_text(INVENTORY)
            return subprocess.run(
                [executable, "-i", str(inventory), str(PLAYBOOK), *extra],
                cwd=ANSIBLE,
                capture_output=True,
                text=True,
                timeout=120,
            )

    def assert_refused(self, result: subprocess.CompletedProcess[str]) -> None:
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("pre-monorepo sources", output)
        self.assertIn("gcp-validator-prod.yml", output)
        self.assertNotIn("UNREACHABLE", output)

    def test_refuses_without_acknowledgement(self):
        self.assert_refused(self.run_playbook())

    def test_refuses_when_acknowledgement_is_false(self):
        self.assert_refused(self.run_playbook("-e", "validator_native_legacy_ack=false"))

    def test_refuses_under_tag_selection(self):
        self.assert_refused(self.run_playbook("--tags", "dittobench"))


if __name__ == "__main__":
    unittest.main()
