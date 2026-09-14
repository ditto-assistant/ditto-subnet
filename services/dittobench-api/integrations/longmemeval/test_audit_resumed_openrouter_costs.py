import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import audit_resumed_openrouter_costs as union
import audit_openrouter_costs as costs


class ResumedCostsTests(unittest.TestCase):
    def journal(self, directory, name, generation, attempt="first", case="q"):
        path = Path(directory) / name
        path.write_text(json.dumps(dict(case_id=case, stage="reader", generation_id=generation,
                                       attempt_id=attempt, model="m", cost_status="missing")) + "\n")
        return path

    def test_failed_first_call_retained_and_only_new_call_fetched(self):
        with TemporaryDirectory() as directory:
            first = self.journal(directory, "first", "gen-failed")
            retry = self.journal(directory, "retry", "gen-success", "second")
            report = {"per_case": [{"case_id": "q", "model": "m", "data": {"provider_responses": [{"ID": "gen-success", "Model": "m"}]}}]}
            old = costs.sanitize_receipt("gen-failed", {"data": {"id": "gen-failed", "model": "m", "total_cost": "0.4", "is_byok": False}})
            requests, receipts, _ = union.prepare(report, [first, retry], [{"receipts": [old]}])
            seen = []
            def fetch(generation, key):
                seen.append(generation)
                return costs.sanitize_receipt(generation, {"data": {"id": generation, "model": "m", "total_cost": "0.6", "is_byok": False}})
            reconciled = costs.reconcile_receipts(requests, receipts, "synthetic", fetch)
            self.assertEqual(seen, ["gen-success"])
            summary = costs.summarize(requests, reconciled)
            self.assertEqual(summary["recorded_cost_usd"], "1.0")
            self.assertEqual(summary["priced_generations"], 2)

    def test_reject_cross_attempt_or_case_reassignment(self):
        for second in ({"attempt": "changed"}, {"case": "other"}):
            with self.subTest(second=second), TemporaryDirectory() as directory:
                first = self.journal(directory, "first", "gen-one")
                retry = self.journal(directory, "retry", "gen-one", **second)
                with self.assertRaisesRegex(ValueError, "ownership"):
                    union.union_journals([first, retry])

    def test_duplicate_journal_rejected(self):
        with TemporaryDirectory() as directory:
            first = self.journal(directory, "first", "gen-one")
            with self.assertRaisesRegex(ValueError, "duplicate journal"):
                union.union_journals([first, first])

    def test_unknown_id_first_attempt_not_resolved_by_retry(self):
        with TemporaryDirectory() as directory:
            first = self.journal(directory, "first", None)
            retry = self.journal(directory, "retry", "gen-new", "second")
            requests, _, _ = union.union_journals([first, retry])
            self.assertEqual(len(requests), 2)
            self.assertEqual(sum(row["generation_id"] is None for row in requests), 1)

    def test_unrelated_recovery_rejected(self):
        with TemporaryDirectory() as directory:
            first = self.journal(directory, "first", "gen-one")
            report = {"per_case": [{"case_id": "q", "model": "m", "data": {}}]}
            with self.assertRaisesRegex(ValueError, "outside selected"):
                union.prepare(report, [first], [{"receipts": [{"generation_id": "gen-unrelated"}]}])


if __name__ == "__main__":
    unittest.main()
