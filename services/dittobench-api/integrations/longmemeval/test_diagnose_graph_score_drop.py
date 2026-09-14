import copy
import unittest

import diagnose_graph_score_drop as diagnosis


def report():
    return dict(run_id="test", git_sha="sha", prompt_sha="prompt", tools_sha="tools", weights_sha="weights", judge_model="judge",
                per_case=[dict(case_id=str(i), query="q", gold_answer="a", category="c", model="m", hypothesis="a",
                               lme_correct=True, lme_session_recall=1,
                               data=dict(query_status="ok", judge_status="ok", session_recall=1, seed_pair_ids=["a", "b"], graph_seed_pair_ids=[],
                                         tools_called=None, answer_source="final", prompt_tokens=10)) for i in range(500)])


class DiagnosisTests(unittest.TestCase):
    def test_zero_recall_is_not_missing(self):
        row = report()["per_case"][0]
        del row["lme_session_recall"]
        row["data"]["session_recall"] = 0
        self.assertEqual(diagnosis.recall(row), 0)

    def test_transitions_and_order_are_separate(self):
        old = report()
        new = copy.deepcopy(old)
        new["per_case"][0]["lme_correct"] = False
        new["per_case"][0]["data"]["seed_pair_ids"] = ["b", "a"]
        result = diagnosis.analyze(old, new)
        self.assertEqual(result["regressions"], 1)
        self.assertEqual(result["same_seed_set"]["cases"], 500)
        self.assertEqual(result["regression_diagnostics"]["same_ordered_seed_ids"], 0)

    def test_duplicate_is_rejected(self):
        data = report()
        data["per_case"][-1] = data["per_case"][0]
        with self.assertRaises(ValueError):
            diagnosis.index(data)

    def test_error_and_nonboolean_are_rejected(self):
        for key, value in (("query_status", "error"), ("judge_status", "error")):
            data = report()
            data["per_case"][0]["data"][key] = value
            with self.assertRaises(ValueError):
                diagnosis.index(data)
        data = report()
        data["per_case"][0]["lme_correct"] = 1
        with self.assertRaises(ValueError):
            diagnosis.index(data)

    def test_identity_drift_is_rejected(self):
        old, new = report(), report()
        new["per_case"][0]["gold_answer"] = "changed"
        with self.assertRaises(ValueError):
            diagnosis.analyze(old, new)

    def test_nonlocal_database_is_rejected_without_subprocess(self):
        with self.assertRaises(ValueError):
            diagnosis.fixture_probe("postgresql://example.org/db", "psql", {}, {})


if __name__ == "__main__":
    unittest.main()
