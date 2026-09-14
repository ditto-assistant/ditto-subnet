import copy
import json
from pathlib import Path
import tempfile
import unittest

from audit_backend_run import DATASET_SHA256, AuditError, aggregate, audit_rows, compare, compare_graph_seeds, load_run, operational_diagnostics, validate_paired_provenance, validate_provenance, validate_refinement_completion, wilson


def fixture():
    condition = {"label": "test", "answer_model": "openai/gpt-5.6-luna", "answer_provider": "OpenAI",
                 "judge_model": "judge-model", "reasoning_effort": "medium", "prompt_clock": "question-date",
                 "require_graph": True, "graph_retrieval": False}
    dataset, rows = {}, []
    for i in range(2):
        qid = f"q{i}"
        dataset[qid] = {"category": "multi-session", "question": f"Question {i}?", "prompt_time": "2024-01-01T10:00:00Z"}
        rows.append({"suite": "longmemeval", "case_id": qid, "model": condition["answer_model"],
                     "category": "multi-session", "lme_correct": i == 0, "hypothesis": "answer" if i == 0 else "wrong answer",
                     "query": dataset[qid]["question"],
                     "data": {"qa_correct": i == 0, "judge_status": "ok", "judge_model": "judge-model",
                              "prompt_clock": "question-date", "prompt_time": dataset[qid]["prompt_time"],
                              "reasoning_effort": "medium", "require_graph": True, "graph_retrieval": False,
                              "query_status": "ok", "answer_source": "final",
                              "condition_sha256": "a" * 64,
                              "fixture_user": f"lme_s_{qid}", "tools_called": ["search_subjects"],
                              "provider_responses": [{"ID": f"gen-{i}", "Model": condition["answer_model"], "Provider": "OpenAI"}]}})
    report = {"judge_model": "judge-model", "meta": {"lme_cases_completed": "2/2", "lme_condition_sha256": "a" * 64},
              "suites": {"longmemeval": {"overall_accuracy": 0.5}}}
    return report, rows, dataset, condition


class BackendAuditTests(unittest.TestCase):
    def test_optional_hydration_gate_preserves_historical_default(self):
        report, rows, dataset, condition = fixture()
        historical, _ = audit_rows(report, rows, dataset, condition)
        self.assertNotIn("native_hydration_validation", historical)
        with self.assertRaisesRegex(AuditError, "native hydration"):
            audit_rows(report, rows, dataset, condition, True)
        report["meta"].update(lme_hydration_preflight="native-hydration-v1", lme_hydration_preflight_users="2")
        for row in rows:
            row["data"]["seed_pair_count"] = 1
        result, _ = audit_rows(report, rows, dataset, condition, True)
        self.assertEqual(result["native_hydration_validation"]["checked_users"], 2)
        for bad in (None, "500", "1", 2, "02"):
            changed = copy.deepcopy(report)
            changed["meta"]["lme_hydration_preflight_users"] = bad
            with self.subTest(users=bad), self.assertRaisesRegex(AuditError, "checked-user"):
                audit_rows(changed, rows, dataset, condition, True)
        for bad in (None, 0, -1, True, 1.0, "1"):
            changed = copy.deepcopy(rows)
            changed[1]["data"]["seed_pair_count"] = bad
            with self.subTest(seeds=bad), self.assertRaisesRegex(AuditError, "seed_pair_count"):
                audit_rows(report, changed, dataset, condition, True)

    def test_corrected_graph_hydration_gate_requires_runtime_discovery(self):
        report, rows, dataset, condition = fixture()
        condition["graph_retrieval"] = True
        for row in rows:
            row["data"].update(graph_retrieval=True, seed_pair_count=3)
        report["meta"].update(lme_hydration_preflight="native-hydration-v1", lme_hydration_preflight_users="2",
                              lme_subject_graph_calls="3", lme_subject_graph_failures="0",
                              lme_subject_graph_candidates="4", lme_subject_graph_total_ms="10",
                              lme_subject_graph_failure_schema="failure-reasons-v1",
                              lme_subject_graph_discovery_complete="true", lme_subject_graph_failure_counts_consistent="true")
        for reason in ("context_deadline", "context_canceled", "postgres_query_canceled", "graph_unavailable", "other"):
            report["meta"]["lme_subject_graph_failures_" + reason] = "0"
        result, _ = audit_rows(report, rows, dataset, condition, True)
        self.assertTrue(result["native_hydration_validation"]["graph_discovery"]["complete"])
        for field, value in (("lme_subject_graph_calls", "0"), ("lme_subject_graph_failures", "1"),
                             ("lme_subject_graph_discovery_complete", "false"),
                             ("lme_subject_graph_failure_counts_consistent", "false"),
                             ("lme_subject_graph_failures_other", "1"),
                             ("lme_subject_graph_failure_schema", None)):
            changed = copy.deepcopy(report)
            changed["meta"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(AuditError, "graph discovery"):
                audit_rows(changed, rows, dataset, condition, True)

    def test_graph_marked_seeds_not_necessarily_novel_against_off(self):
        _, rows, _, _ = fixture()
        off = {r["case_id"]: r for r in rows}
        on = copy.deepcopy(off)
        for qid in off:
            off[qid]["data"]["seed_pair_ids"] = ["stock"]
            on[qid]["data"].update(graph_retrieval=True, graph_seed_pair_ids=["stock"], seed_pair_ids=["stock"])
        on["q1"]["data"]["graph_seed_pair_ids"].append("novel")
        on["q1"]["data"]["seed_pair_ids"].append("novel")
        result = compare_graph_seeds(on, off)
        self.assertEqual(result["cases_with_graph_marked_seeds"], 2)
        self.assertEqual(result["cases_with_graph_marked_seeds_absent_from_off_seed"], 1)
        self.assertEqual(result["graph_marked_seed_occurrences_also_in_off_seed"], 2)
        self.assertEqual(result["cases_with_different_seed_id_sets"], 1)
        self.assertEqual(result["graph_marked_seed_cases_with_verdict_change"], 0)
        self.assertEqual(result, compare_graph_seeds(off, on))
        del off["q0"]["data"]["seed_pair_ids"]
        self.assertEqual(compare_graph_seeds(on, off)["unavailable_cases"], 1)

    def test_graph_fallback_candidates_and_neighbor_traces_are_separate(self):
        report, rows, _, _ = fixture()
        report["meta"].update(lme_subject_graph_calls="10", lme_subject_graph_failures="8",
                              lme_subject_graph_candidates="14", lme_subject_graph_total_ms="16000")
        rows[0]["data"].update(graph_seed_pair_ids=["private-id"], tool_trace=[{
            "name": "explore_subject_neighbors", "arguments": {"truncated": False}, "result": {"truncated": True}}])
        rows[1]["data"].update(graph_seed_pair_ids=[], tool_trace=[])
        result = operational_diagnostics(report, rows)
        graph = result["graph_utilization"]
        self.assertEqual(graph["discovery_failure_fallback_rate"], 0.8)
        self.assertEqual(graph["discovered_candidate_occurrences_not_unique"], 14)
        self.assertEqual(graph["cases_with_graph_seed_ids"], 1)
        self.assertEqual(graph["graph_seed_recorded_cases"], 2)
        self.assertEqual(graph["neighbor_tool_trace_calls"], 1)
        self.assertEqual(graph["neighbor_tool_traces_with_truncated_text"], 1)
        self.assertNotIn("private-id", json.dumps(result))
        report["meta"]["lme_subject_graph_failures"] = "11"
        with self.assertRaisesRegex(AuditError, "failures exceed"):
            operational_diagnostics(report, rows)

    def test_missing_graph_counters_are_unavailable_not_zero(self):
        report, rows, _, _ = fixture()
        graph = operational_diagnostics(report, rows)["graph_utilization"]
        self.assertFalse(graph["discovery_counters_available"])
        self.assertIsNone(graph["discovery_failure_fallback_rate"])
        self.assertEqual(graph["graph_seed_recorded_cases"], 0)
        report["meta"]["lme_subject_graph_calls"] = "0"
        with self.assertRaisesRegex(AuditError, "partial"):
            operational_diagnostics(report, rows)

    def test_latency_uses_every_case_not_resumed_suite(self):
        report, rows, _, _ = fixture()
        report["suites"]["standard"] = {"incorrect_resumed_sample": 99999}
        for value, row in zip([10, 30], rows):
            row["data"].update(latency_ms=value, prompt_tokens=value * 2, output_tokens=value * 3)
        result = operational_diagnostics(report, rows)["successful_per_case_metrics"]
        self.assertEqual(result["latency_ms"]["mean"], 20)
        self.assertEqual(result["latency_ms"]["p95_nearest_rank"], 30)
        self.assertEqual(result["prompt_tokens"]["sum"], 80)
        del rows[1]["data"]["latency_ms"]
        partial = operational_diagnostics(report, rows)["successful_per_case_metrics"]["latency_ms"]
        self.assertFalse(partial["complete_case_coverage"])
        self.assertNotIn("mean", partial)

    def test_complete_score_and_empty_answer_count(self):
        summary, indexed = audit_rows(*fixture())
        self.assertEqual(summary["overall"]["correct"], 1)
        self.assertEqual(summary["overall"]["empty_answers"], 0)
        self.assertEqual(summary["tool_call_counts"], {"search_subjects": 2})
        self.assertEqual(len(indexed), 2)
        encoded = json.dumps(summary)
        for private_value in ("gen-0", "lme_s_q0", '"hypothesis"'):
            self.assertNotIn(private_value, encoded)

    def test_lowercase_identity_keys_supported(self):
        report, rows, dataset, condition = fixture()
        for row in rows:
            identity = row["data"]["provider_responses"][0]
            row["data"]["provider_responses"] = [{key.lower(): value for key, value in identity.items()}]
        audit_rows(report, rows, dataset, condition)

    def test_jsonl_requires_per_case_judge_identity(self):
        _, rows, dataset, condition = fixture()
        audit_rows({}, rows, dataset, condition)
        del rows[0]["data"]["judge_model"]
        with self.assertRaisesRegex(AuditError, "judge model"):
            audit_rows({}, rows, dataset, condition)

    def test_duplicate_missing_unexpected_and_overfull_fail(self):
        for mutation in (lambda rows: rows.pop(), lambda rows: rows.append(copy.deepcopy(rows[0])),
                         lambda rows: rows[1].update(case_id="q0"), lambda rows: rows[1].update(case_id="unexpected")):
            with self.subTest(mutation=mutation):
                report, rows, dataset, condition = fixture()
                mutation(rows)
                with self.assertRaises(AuditError):
                    audit_rows(report, rows, dataset, condition)

    def test_case_contract_fail_closed(self):
        mutations = [
            lambda r: r.pop("lme_correct"), lambda r: r.update(lme_correct=0),
            lambda r: r.update(hypothesis=""), lambda r: r["data"].pop("query_status"),
            lambda r: r["data"].update(answer_source="reasoning"),
            lambda r: r["data"].update(condition_sha256="b" * 64),
            lambda r: r.update(category="wrong"), lambda r: r.update(model="fallback-model"),
            lambda r: r.update(query="a different question with the same ID"),
            lambda r: r["data"].pop("judge_status"), lambda r: r["data"].update(judge_status="error"),
            lambda r: r["data"].update(judge_error="timeout"), lambda r: r["data"].update(qa_correct=False),
            lambda r: r["data"].update(require_graph=1), lambda r: r["data"].pop("graph_retrieval"),
            lambda r: r["data"].update(reasoning_effort="low"), lambda r: r["data"].update(prompt_clock="wall"),
            lambda r: r["data"].update(prompt_time="2026-01-01T10:00:00Z"),
            lambda r: r["data"].update(fixture_user="production-user"),
            lambda r: r["data"].update(provider_responses=[]),
            lambda r: r["data"]["provider_responses"][0].update(Model="fallback-model"),
            lambda r: r["data"]["provider_responses"][0].update(Provider="fallback-provider"),
            lambda r: r["data"]["provider_responses"][0].pop("ID"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                report, rows, dataset, condition = fixture()
                mutation(rows[0])
                with self.assertRaises(AuditError):
                    audit_rows(report, rows, dataset, condition)

    def test_duplicate_provider_and_shared_fixture_fail(self):
        for key, value in (("provider_responses", [{"ID": "gen-0", "Model": "openai/gpt-5.6-luna", "Provider": "OpenAI"}]),
                           ("fixture_user", "lme_s_q0")):
            report, rows, dataset, condition = fixture()
            rows[1]["data"][key] = value
            with self.assertRaises(AuditError):
                audit_rows(report, rows, dataset, condition)

    def test_swapped_fixture_users_fail_even_when_unique(self):
        report, rows, dataset, condition = fixture()
        rows[0]["data"]["fixture_user"], rows[1]["data"]["fixture_user"] = rows[1]["data"]["fixture_user"], rows[0]["data"]["fixture_user"]
        with self.assertRaisesRegex(AuditError, "exact question"):
            audit_rows(report, rows, dataset, condition)

    def test_complete_source_provenance_is_required(self):
        report = {"run_id": "frozen-run", "git_sha": "a" * 40, "prompt_sha": "b" * 64,
                  "tools_sha": "c" * 64, "weights_sha": "learned:" + "d" * 64,
                  "started_at": "2026-09-12T12:00:00Z", "finished_at": "2026-09-12T13:00:00Z",
                  "meta": {"lme_manifest_sha256": "e" * 64, "lme_cases_sha256": "f" * 64,
                           "lme_prepared_snapshot_sha256": "1" * 64, "lme_prepared_snapshot_after_sha256": "1" * 64,
                           "lme_prepared_snapshot_unchanged": "true", "lme_id_blinding_scheme": "lme-opaque-sha256-v1",
                           "lme_refinement_receipt_version": "native-semantic-save-v1", "lme_refinement_receipts_sha256": "2" * 64,
                           "lme_raw_pending_refinement": "0", "lme_accepted_refinement_receipts": "0"}}
        validate_provenance(report)
        for field in report:
            bad = dict(report)
            del bad[field]
            with self.assertRaises(AuditError):
                validate_provenance(bad)
        for field, bad_value in (("git_sha", "abc1234"), ("weights_sha", "fixed"), ("prompt_sha", "unknown")):
            with self.assertRaises(AuditError):
                validate_provenance(dict(report, **{field: bad_value}))
        with self.assertRaisesRegex(AuditError, "checkpoint"):
            validate_provenance({})
        for value in (None, "legacy", "lme-opaque-sha256-v2", True):
            bad = copy.deepcopy(report)
            bad["meta"]["lme_id_blinding_scheme"] = value
            with self.subTest(blinding=value), self.assertRaisesRegex(AuditError, "opaque-ID"):
                validate_provenance(bad)
        for field, value in (("lme_prepared_snapshot_unchanged", "false"),
                             ("lme_prepared_snapshot_after_sha256", "2" * 64),
                             ("lme_prepared_snapshot_error", "timeout")):
            bad = copy.deepcopy(report)
            bad["meta"][field] = value
            with self.assertRaisesRegex(AuditError, "prepared fixture"):
                validate_provenance(bad)

    def test_report_cannot_replace_qa_with_composite(self):
        report, rows, dataset, condition = fixture()
        report["suites"]["longmemeval"]["overall_accuracy"] = 0.94
        with self.assertRaisesRegex(AuditError, "headline"):
            audit_rows(report, rows, dataset, condition)

    def test_wilson_boundaries_and_paired_disagreements(self):
        self.assertAlmostEqual(wilson(0, 500)[0], 0)
        self.assertAlmostEqual(wilson(500, 500)[1], 1)
        _, rows, _, _ = fixture()
        left = {r["case_id"]: r for r in rows}
        right = copy.deepcopy(left)
        right["q1"]["lme_correct"] = True
        result = compare(left, right)
        self.assertEqual(result["right_only_correct"], 1)
        self.assertEqual(result["right_minus_left_accuracy"], 0.5)
        self.assertEqual(result["exact_mcnemar_two_sided_p"], 1)

    def test_paired_provenance_rejects_changed_common_inputs(self):
        left = {"weights_sha": "learned:" + "a" * 64, "prompt_sha": "b" * 64, "judge_model": "judge",
                "git_sha": "c" * 40, "tools_sha": "d" * 64,
                "meta": {"lme_dataset_sha256": DATASET_SHA256, "lme_manifest_sha256": "e" * 64,
                         "lme_cases_sha256": "3" * 64, "lme_prepared_snapshot_sha256": "f" * 64,
                         "lme_refinement_receipts_sha256": "4" * 64}}
        right = copy.deepcopy(left)
        right.update(git_sha="1" * 40, tools_sha="2" * 64, weights_sha="a" * 64)
        self.assertTrue(validate_paired_provenance(left, right)["fixture_snapshot_verified"])
        for field in ("weights_sha", "prompt_sha", "judge_model"):
            with self.subTest(field=field), self.assertRaises(AuditError):
                validate_paired_provenance(left, dict(right, **{field: "different"}))
        for field in left["meta"]:
            for value in ("1" * 64, None):
                changed = copy.deepcopy(right)
                changed["meta"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(AuditError):
                    validate_paired_provenance(left, changed)

    def test_absent_snapshot_is_explicitly_unverified(self):
        report = {"weights_sha": "a" * 64, "prompt_sha": "b" * 64, "judge_model": "judge"}
        result = validate_paired_provenance(report, report)
        self.assertFalse(result["fixture_snapshot_verified"])
        self.assertIn("lme_prepared_snapshot_sha256", result["unavailable_provenance_fields"])

    def test_refinement_raw_and_matched_receipts_are_not_conflated(self):
        meta = {"lme_refinement_receipt_version": "native-semantic-save-v1", "lme_refinement_receipts_sha256": "a" * 64,
                "lme_raw_pending_refinement": "2", "lme_accepted_refinement_receipts": "2"}
        result = validate_refinement_completion(meta)
        self.assertEqual(result["raw_pending_refinement"], 2)
        self.assertEqual(result["accepted_refinement_receipts"], 2)
        self.assertEqual(result["remaining_pending_refinement"], 0)
        validate_refinement_completion(dict(meta, lme_raw_pending_refinement="0", lme_accepted_refinement_receipts="0"))
        for field, value in (("lme_refinement_receipt_version", "unknown"),
                             ("lme_refinement_receipts_sha256", "unknown"),
                             ("lme_raw_pending_refinement", "-1"),
                             ("lme_raw_pending_refinement", "2.0"),
                             ("lme_accepted_refinement_receipts", True),
                             ("lme_accepted_refinement_receipts", "1"),
                             ("lme_accepted_refinement_receipts", "3")):
            with self.subTest(field=field, value=value), self.assertRaises(AuditError):
                validate_refinement_completion(dict(meta, **{field: value}))
        for field in meta:
            bad = dict(meta)
            del bad[field]
            with self.subTest(missing=field), self.assertRaises(AuditError):
                validate_refinement_completion(bad)

    def test_report_and_checkpoint_loading_and_truncated_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "evidence.json"
            report, rows, _, _ = fixture()
            path.write_text(json.dumps(dict(report, per_case=rows)))
            self.assertEqual(load_run(path)[1], rows)
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
            self.assertEqual(load_run(path), ({}, rows))
            path.write_text(json.dumps(rows[0]) + '\n{"case_id":')
            with self.assertRaises(AuditError):
                load_run(path)


if __name__ == "__main__":
    unittest.main()
