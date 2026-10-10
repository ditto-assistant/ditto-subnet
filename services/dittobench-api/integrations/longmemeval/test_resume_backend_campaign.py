import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import resume_backend_campaign as resume


class ResumeGuardTests(unittest.TestCase):
    def test_exact_committed_cohort_and_mutation_rejection(self):
        path = Path(__file__).resolve().parents[2] / "docs/longmemeval-benchmark/results/2026-09-12-ditto-resource-pause-221310.json"
        record = json.loads(path.read_text())
        self.assertEqual(len(resume.checked_users(record)), 169)
        record["remaining_native_users"][0] = "lme_s_different"
        with self.assertRaisesRegex(ValueError, "frozen selection"):
            resume.checked_users(record)

    def test_literal_config_preserves_process_and_local_precedence(self):
        runtime = {"EXISTING": "operator", "EMPTY": ""}
        resume.inject_config(runtime, "# ignored\nEXISTING=file\nEMPTY=file\nA = local=literal\nQUOTED='keep quotes'\n")
        resume.inject_config(runtime, "A=common\nB=common\n")
        self.assertEqual(runtime, {"EXISTING": "operator", "EMPTY": "", "A": "local=literal", "B": "common", "QUOTED": "'keep quotes'"})

    def test_low_headroom_never_sleeps_or_executes(self):
        with patch.object(resume.subprocess, "check_output", return_value="Filesystem Used Available\ndisk 1 2 100\n"), patch.object(resume.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "headroom"):
                resume.require_stable_headroom(Path("."))
            sleep.assert_not_called()

    def test_optimized_python_does_not_disable_checks(self):
        result = subprocess.run([sys.executable, "-O", "-c", "from resume_backend_campaign import require; require(False, 'guard active')"],
                                cwd=Path(__file__).parent, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("guard active", result.stderr)


if __name__ == "__main__":
    unittest.main()
