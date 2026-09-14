import copy
import json
from pathlib import Path
import tempfile
import unittest

import resume_fixed_backend_tls as resume


class ResumeTests(unittest.TestCase):
    def fixture(self):
        cohort = {f"synthetic-{i}" for i in range(495)} | resume.FAILED
        rows = [{"case_id": case, "model": resume.MODEL, "lme_correct": False,
                 "data": {"query_status": "ok", "judge_status": "ok", "condition_sha256": resume.CONDITION,
                          "seed_pair_count": 12, "graph_seed_pair_ids": ["opaque"]}}
                for case in sorted(cohort - resume.FAILED)]
        errors = [{"case_id": case, "model": resume.MODEL, "notes": [resume.TLS_ERROR],
                   "data": {"query_status": "error"}} for case in resume.FAILED]
        report = {"per_case": copy.deepcopy(rows) + errors, "meta": {
            "lme_complete": "false", "lme_judged_cases": "495",
            "lme_prepared_snapshot_sha256": resume.PREPARED,
            "lme_prepared_snapshot_after_sha256": resume.PREPARED,
            "lme_subject_graph_discovery_complete": "true", "lme_subject_graph_failures": "0",
            "lme_hydration_preflight_users": "500"}}
        return report, rows, cohort

    def test_exact_five_failure_complement_passes(self):
        resume.validate_rows(*self.fixture())

    def test_duplicate_missing_changed_or_failed_checkpoint_rejected(self):
        for mutation in ("duplicate", "missing", "changed", "unjudged", "no_seed", "wrong_condition"):
            with self.subTest(mutation=mutation):
                report, rows, cohort = self.fixture()
                if mutation == "duplicate":
                    rows[0] = rows[1]
                elif mutation == "missing":
                    rows.pop()
                elif mutation == "changed":
                    rows[0]["model"] = "different"
                elif mutation == "unjudged":
                    rows[0]["lme_correct"] = None
                    report["per_case"][0] = copy.deepcopy(rows[0])
                elif mutation == "no_seed":
                    rows[0]["data"]["graph_seed_pair_ids"] = []
                    report["per_case"][0] = copy.deepcopy(rows[0])
                else:
                    rows[0]["data"]["condition_sha256"] = "changed"
                    report["per_case"][0] = copy.deepcopy(rows[0])
                with self.assertRaises(ValueError):
                    resume.validate_rows(report, rows, cohort)

    def test_retry_cannot_select_wrong_answers_or_other_errors(self):
        report, rows, cohort = self.fixture()
        report["per_case"][-1]["notes"] = ["non-transport failure"]
        with self.assertRaises(ValueError):
            resume.validate_rows(report, rows, cohort)

    def test_snapshot_or_graph_failure_rejected(self):
        for field in ("lme_prepared_snapshot_after_sha256", "lme_subject_graph_failures", "lme_hydration_preflight_users"):
            report, rows, cohort = self.fixture()
            report["meta"][field] = "different"
            with self.assertRaises(ValueError):
                resume.validate_rows(report, rows, cohort)

    def test_exclusive_copy_and_claim_never_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "receipt.json"
            body = resume.encoded({"original": True})
            resume.exclusive_bytes(path, body)
            with self.assertRaises(FileExistsError):
                resume.exclusive_bytes(path, b"overwrite")
            self.assertEqual(path.read_bytes(), body)
            self.assertEqual(json.loads(path.read_text()), {"original": True})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
