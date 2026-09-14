import copy
import unittest
from analyze_slices_full import CATEGORIES, full_analysis, wilson
from test_analyze_slices_pilot import report


def full_report(mode):
    result = report(mode)
    template = result["per_case"][0]
    result["per_case"] = []
    result["meta"]["lme_hydration_preflight_users"] = "500"
    for category, count in CATEGORIES.items():
        for _ in range(count):
            row = copy.deepcopy(template)
            row["case_id"] = row["data"]["fixture_user"] = str(len(result["per_case"]))
            row["category"] = category
            result["per_case"].append(row)
    return result


class FullAuditTests(unittest.TestCase):
    def test_full500_and_pilot_subsets(self):
        a, b = full_report("summary"), full_report("source-slices-v1")
        ids = [str(i) for i in range(500)]
        result = full_analysis(a, b, ids, ids[:60])
        self.assertEqual(result["cases"], 500)
        self.assertEqual(result["remaining_440"]["cases"], 440)
        self.assertEqual(result["baseline_payload_preserved_cases"], 500)

    def test_rejects_partial_duplicate_and_colliding_runs(self):
        a, b = full_report("summary"), full_report("source-slices-v1")
        ids = [str(i) for i in range(500)]
        for mutation in (lambda r: r["per_case"].pop(), lambda r: r.update(run_id=a["run_id"]),
                         lambda r: r["per_case"][0].update(category="wrong")):
            bad = copy.deepcopy(b)
            mutation(bad)
            with self.assertRaises(ValueError):
                full_analysis(a, bad, ids, ids[:60])

    def test_wilson_bounds(self):
        lo, hi = wilson(250, 500)
        self.assertLess(lo, .5)
        self.assertGreater(hi, .5)
        self.assertAlmostEqual(lo+hi, 1)


if __name__ == "__main__":
    unittest.main()
