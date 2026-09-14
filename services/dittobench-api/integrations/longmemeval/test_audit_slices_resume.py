import copy
import unittest
from audit_slices_resume import audit
from resume_slices_full_tls import FAILED, SOURCE
from test_analyze_slices_full import full_report


class ResumeAuditTests(unittest.TestCase):
    def test_preservation_and_tampering(self):
        final = full_report("summary")
        final["git_sha"] = SOURCE
        for row, cid in zip(final["per_case"][-4:], sorted(FAILED)):
            row["case_id"] = cid
        original = copy.deepcopy(final)
        original["meta"]["lme_complete"] = "false"
        for row in original["per_case"][-4:]:
            row["lme_correct"] = None
        result = audit(original, final)
        self.assertEqual(result["preserved_judged_rows"], 496)
        final["per_case"][0]["lme_correct"] = False
        with self.assertRaises(ValueError):
            audit(original, final)


if __name__ == "__main__":
    unittest.main()
