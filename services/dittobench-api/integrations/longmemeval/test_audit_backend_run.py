import copy
import json
from pathlib import Path
import tempfile
import unittest

from audit_backend_run import AuditError, aggregate, audit_rows, compare, load_run, validate_provenance, wilson


def fixture():
    condition = {"label": "test", "answer_model": "openai/gpt-5.6-luna", "answer_provider": "OpenAI",
                 "judge_model": "judge-model", "reasoning_effort": "medium", "prompt_clock": "question-date",
                 "require_graph": True, "graph_retrieval": False}
    dataset, rows = {}, []
    for i in range(2):
        qid = f"q{i}"
        dataset[qid] = {"category": "multi-session", "prompt_time": "2024-01-01T10:00:00Z"}
        rows.append({"suite": "longmemeval", "case_id": qid, "model": condition["answer_model"],
                     "category": "multi-session", "lme_correct": i == 0, "hypothesis": "answer" if i == 0 else "wrong answer",
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
                  "started_at": "2026-09-12T12:00:00Z", "finished_at": "2026-09-12T13:00:00Z"}
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
