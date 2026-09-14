import unittest
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import audit_openrouter_costs as audit
from inventory_preparation_cost_evidence import inventory


class CostAuditTests(unittest.TestCase):
    def test_missing_is_not_zero_and_zero_receipt_is_real(self):
        requests = [{"case_id": "a", "stage": "reader", "model": "m", "generation_id": "gen-a"},
                    {"case_id": "b", "stage": "reader", "model": "m", "generation_id": "gen-b"}]
        receipt = audit.sanitize_receipt("gen-a", {"data": {"id": "gen-a", "total_cost": 0, "model": "m"}})
        summary = audit.summarize(requests, [receipt])
        self.assertEqual(summary["priced_generations"], 1)
        self.assertEqual(summary["missing_generations"], 1)
        self.assertFalse(summary["saved_generation_coverage_complete"])
        self.assertIsNone(summary["full_lifecycle_cost_usd"])
        self.assertFalse(summary["all_attempts_coverage_proven"])

    def test_decimal_sum_and_no_upstream_double_charge(self):
        receipts = [audit.sanitize_receipt("gen-" + c, {"data": {
            "id": "gen-" + c, "model": "m", "total_cost": charge,
            "upstream_inference_cost": 200, "usage": 300, "prompt": "private"}})
            for c, charge in [("a", Decimal("0.1")), ("b", Decimal("0.2"))]]
        requests = [{"case_id": "a", "stage": "reader", "model": "m", "generation_id": row["generation_id"]}
                    for row in receipts]
        self.assertEqual(audit.summarize(requests, receipts)["recorded_cost_usd"], "0.3")
        self.assertNotIn("prompt", receipts[0])
        self.assertNotIn("upstream_inference_cost", receipts[0])

    def test_invalid_or_missing_cost_rejected(self):
        for value in [None, True, -1, "NaN", "Infinity", "garbage"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                audit.money(value)
        with self.assertRaises(ValueError):
            audit.sanitize_receipt("gen-a", {"data": {"id": "gen-other", "total_cost": 1}})

    def test_stream_id_dedup_but_cross_case_attribution_rejected(self):
        case = {"case_id": "q", "model": "m", "data": {"provider_responses": [
            {"ID": "gen-a", "Model": "m"}, {"ID": "gen-a", "Model": "m"}]}}
        self.assertEqual(len(audit.report_requests({"per_case": [case]})), 1)
        with self.assertRaises(ValueError):
            audit.report_requests({"per_case": [case, dict(case, case_id="other")]})
        with self.assertRaises(ValueError):
            audit.report_requests({"per_case": [case, case]})

    def test_missing_reader_refs_remain_missing(self):
        requests = audit.report_requests({"per_case": [{"case_id": "q", "model": "m"}]})
        self.assertEqual(audit.summarize(requests, [])["missing_generations"], 1)

    def test_model_conflict_duplicate_receipt_fail(self):
        request = {"case_id": "q", "stage": "reader", "model": "m", "generation_id": "gen-a"}
        receipt = {"generation_id": "gen-a", "status": "ok", "model": "other", "total_cost_usd": "1"}
        with self.assertRaises(ValueError):
            audit.summarize([request], [receipt])
        with self.assertRaises(ValueError):
            audit.summarize([request], [receipt, receipt])

    def test_output_exclusive_private(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            audit.write_new(path, {})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                audit.write_new(path, {})

    def test_network_endpoint_identity_guard(self):
        with self.assertRaises(ValueError):
            audit.fetch_receipt("https://attacker.invalid", "never-transmitted")

    def test_journal_uses_latest_cumulative_not_sum_stream_chunks(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            row = {"case_id": "q", "stage": "judge", "model": "j", "attempt_id": "a",
                   "generation_id": "gen-j", "cost_status": "reported_usage_cost", "cost_credits": "0.1"}
            path.write_text(json.dumps(row) + "\n" + json.dumps(dict(row, cost_credits="0.2")) + "\n")
            requests, receipts = audit.journal_evidence(path)
            summary = audit.summarize(requests, receipts)
            self.assertEqual(summary["recorded_cost_by_stage_usd"], {"judge": "0"})
            self.assertEqual(summary["provisional_observed_cost_usd"], "0.2")
            self.assertEqual(summary["priced_generations"], 0)
            self.assertFalse(summary["saved_generation_coverage_complete"])
            self.assertIsNone(summary["captured_generation_cost_per_question_stats_usd"])
            self.assertIsNone(summary["full_lifecycle_cost_usd"])

    def test_journal_unknown_id_is_not_removed_by_other_success(self):
        good = {"case_id": "q", "stage": "reader", "model": "m", "generation_id": "gen-a"}
        unknown = dict(good, generation_id=None)
        requests = audit.combine_requests([good], [good, unknown])
        self.assertEqual(len(requests), 2)
        self.assertIn(unknown, requests)

    def test_fetch_reconciles_missing_but_skips_priced(self):
        requests = [{"generation_id": "gen-a"}, {"generation_id": "gen-b"}]
        receipts = [{"generation_id": "gen-a", "status": "cost_missing"},
                    {"generation_id": "gen-b", "status": "ok", "total_cost_usd": "0",
                     "basis": "openrouter_generation.total_cost"}]
        called = []
        def fetch(generation, key):
            called.append(generation)
            return {"generation_id": generation, "status": "ok", "total_cost_usd": "1",
                    "basis": "openrouter_generation.total_cost"}
        result = audit.reconcile_receipts(requests, receipts, "unused", fetch)
        self.assertEqual(called, ["gen-a"])
        self.assertEqual(len(result), 2)
        self.assertTrue(all(row["status"] == "ok" for row in result))

    def test_measured_query_stats_and_judge_subtotal_not_lifecycle(self):
        requests, receipts = [], []
        for case, stage, charge in [("a", "reader", "1"), ("a", "judge", "0.1"),
                                    ("b", "reader", "2"), ("b", "judge", "0.1")]:
            generation = "gen-" + case + stage
            requests.append({"case_id": case, "stage": stage, "model": "m", "generation_id": generation})
            receipts.append({"generation_id": generation, "status": "ok", "model": "m", "total_cost_usd": charge,
                             "basis": "openrouter_generation.total_cost"})
        summary = audit.summarize(requests, receipts)
        self.assertEqual(summary["question_denominator"], 2)
        self.assertEqual(summary["recorded_judge_cost_subtotal_usd"], "0.2")
        self.assertEqual(summary["captured_generation_cost_per_question_stats_usd"],
                         {"mean": "1.6", "median": "1.6", "p95_nearest_rank": "2.1"})
        self.assertIsNone(summary["full_judge_cost_usd"])
        self.assertIsNone(summary["full_lifecycle_cost_usd"])
        self.assertIsNone(audit.summarize(requests, receipts[:-1])["captured_generation_cost_per_question_stats_usd"])

    def test_partial_stream_cost_get_reconciliation_can_increase(self):
        request = {"case_id": "q", "stage": "reader", "model": "m", "generation_id": "gen-a"}
        partial = {"generation_id": "gen-a", "model": "m", "status": "ok",
                   "total_cost_usd": "0.1", "basis": "openrouter_response.usage.cost"}
        before = audit.summarize([request], [partial])
        self.assertFalse(before["saved_generation_coverage_complete"])
        self.assertEqual(before["provisional_observed_cost_usd"], "0.1")
        called = []
        def fetch(generation, key):
            called.append(generation)
            return audit.sanitize_receipt(generation, {"data": {"id": generation, "model": "m", "total_cost": "0.3"}})
        receipts = audit.reconcile_receipts([request], [partial], "unused", fetch)
        after = audit.summarize([request], receipts)
        self.assertEqual(called, ["gen-a"])
        self.assertTrue(after["saved_generation_coverage_complete"])
        self.assertEqual(after["recorded_cost_usd"], "0.3")
        self.assertEqual(after["provisional_observed_cost_usd"], "0.1")
        merged = audit.merge_receipts(receipts, [partial])
        self.assertEqual(audit.summarize([request], merged)["recorded_cost_usd"], "0.3")

    def test_journal_model_fills_after_initial_blank_and_conflicts_fail(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "usage.jsonl"
            row = {"case_id": "q", "stage": "reader", "model": "", "attempt_id": "a",
                   "generation_id": "gen-a", "cost_status": "missing"}
            path.write_text(json.dumps(row) + "\n" + json.dumps(dict(row, model="m")) + "\n")
            requests, _ = audit.journal_evidence(path)
            self.assertEqual(requests[0]["model"], "m")
            with path.open("a") as stream:
                stream.write(json.dumps(dict(row, model="different")) + "\n")
            with self.assertRaisesRegex(ValueError, "model identity changed"):
                audit.journal_evidence(path)
            path.write_text(json.dumps(row) + "\n")
            with self.assertRaisesRegex(ValueError, "unresolved"):
                audit.journal_evidence(path)

    def test_journal_merge_rejects_duplicate_saved_receipts(self):
        row = {"generation_id": "gen-a", "status": "cost_missing"}
        with self.assertRaisesRegex(ValueError, "duplicate generation receipt"):
            audit.merge_receipts([row, row], [row])

    def test_byok_zero_router_charge_preserves_nonzero_upstream_estimate(self):
        request = {"case_id": "q", "stage": "reader", "model": "openai/gpt-5.6-luna", "generation_id": "gen-a"}
        receipt = audit.sanitize_receipt("gen-a", {"data": {"id": "gen-a", "model": "openai/gpt-5.6-luna-20260709",
                                          "total_cost": 0, "is_byok": True, "upstream_inference_cost": "0.0017014"}})
        summary = audit.summarize([request], [receipt])
        self.assertEqual(summary["recorded_cost_usd"], "0")
        self.assertEqual(summary["byok_upstream_estimate_usd"], "0.0017014")
        self.assertEqual(summary["selected_generation_estimated_cost_usd"], "0.0017014")
        self.assertEqual(summary["selected_generation_estimated_cost_per_question_usd"],
                         [{"case_id": "q", "estimated_cost_usd": "0.0017014"}])
        self.assertEqual(summary["selected_generation_estimated_cost_per_question_stats_usd"]["median"], "0.0017014")
        self.assertTrue(summary["requested_resolved_models"][0]["exact_alias_mapping_used"])
        self.assertIsNone(summary["full_lifecycle_cost_usd"])
        self.assertEqual(receipt["upstream_cost_classification"], "provider_reported_byok_estimate_not_vendor_invoice")

    def test_byok_missing_upstream_does_not_estimate_free(self):
        request = {"case_id": "q", "stage": "reader", "model": "m", "generation_id": "gen-a"}
        receipt = audit.sanitize_receipt("gen-a", {"data": {"id": "gen-a", "model": "m", "total_cost": 0, "is_byok": True}})
        summary = audit.summarize([request], [receipt])
        self.assertEqual(summary["missing_byok_upstream_costs"], 1)
        self.assertIsNone(summary["selected_generation_estimated_cost_usd"])
        self.assertIsNone(summary["selected_generation_estimated_cost_per_question_usd"][0]["estimated_cost_usd"])

    def test_alias_mapping_exact_not_any_date_or_prefix(self):
        self.assertTrue(audit.model_matches("openai/gpt-5.6-luna", "openai/gpt-5.6-luna-20260709"))
        self.assertTrue(audit.model_matches("google/gemini-3.1-flash-lite", "google/gemini-3.1-flash-lite-20260507"))
        self.assertFalse(audit.model_matches("google/gemini-3.1-flash-lite", "google/gemini-3.1-flash-lite-20260508"))
        for resolved in ["openai/gpt-5.6-luna-20260809", "openai/gpt-5.6-luna-other", "openai/gpt-5.6-sol-20260709"]:
            self.assertFalse(audit.model_matches("openai/gpt-5.6-luna", resolved))
            request = {"case_id": "q", "stage": "reader", "model": "openai/gpt-5.6-luna", "generation_id": "gen-a"}
            receipt = audit.sanitize_receipt("gen-a", {"data": {"id": "gen-a", "model": resolved, "total_cost": 0}})
            with self.assertRaises(ValueError):
                audit.summarize([request], [receipt])

    def test_non_byok_upstream_not_added_twice_and_unknown_route_incomplete(self):
        request = {"case_id": "q", "stage": "reader", "model": "m", "generation_id": "gen-a"}
        raw = {"id": "gen-a", "model": "m", "total_cost": "1", "is_byok": False, "upstream_inference_cost": "0.8"}
        receipt = audit.sanitize_receipt("gen-a", {"data": raw})
        self.assertEqual(audit.summarize([request], [receipt])["selected_generation_estimated_cost_usd"], "1")
        del receipt["is_byok"]
        self.assertIsNone(audit.summarize([request], [receipt])["selected_generation_estimated_cost_usd"])

    def test_invalid_attempt_keeps_spend_excludes_valid_question_means(self):
        request = {"case_id": "q", "stage": "reader", "model": "m", "generation_id": "gen-a"}
        receipt = audit.sanitize_receipt("gen-a", {"data": {"id": "gen-a", "model": "m", "total_cost": "1", "is_byok": False}})
        summary = audit.classify_attempt(audit.summarize([request], [receipt]), "invalid_attempt")
        self.assertEqual(summary["recorded_cost_usd"], "1")
        self.assertEqual(summary["selected_generation_estimated_cost_usd"], "1")
        self.assertTrue(summary["include_in_campaign_spend"])
        self.assertFalse(summary["include_in_valid_run_metrics"])
        self.assertIsNone(summary["captured_generation_cost_per_question_stats_usd"])
        self.assertIsNone(summary["selected_generation_estimated_mean_per_question_usd"])
        self.assertIsNone(summary["selected_generation_estimated_cost_per_question_stats_usd"])

    def test_preparation_inventory_scoped_no_raw_content(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dream-test.log").write_text('secret prompt gen-abcde "cost_credits": "1"\n')
            (root / "qa-test.log").write_text('gen-ignore')
            result = inventory(root)
            self.assertEqual(result["file_count"], 1)
            self.assertEqual(result["files_with_generation_ids"], 1)
            self.assertNotIn("secret prompt", json.dumps(result))
            self.assertNotIn("gen-abcde", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
