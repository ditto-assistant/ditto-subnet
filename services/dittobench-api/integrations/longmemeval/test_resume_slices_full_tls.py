import copy
import unittest
import resume_slices_full_tls as resume


def evidence():
    rows = [dict(case_id=str(i), model="openai/gpt-5.6-luna", lme_correct=i % 2 == 0,
                 data=dict(query_status="ok", judge_status="ok", condition_sha256=resume.CONDITION)) for i in range(496)]
    bad = [dict(case_id=cid, notes=["agent loop: remote error: tls: bad record MAC"], data=dict(query_status="error")) for cid in sorted(resume.READER_FAILED)]
    bad.append(dict(case_id=resume.JUDGE_FAILED, data=dict(query_status="ok", judge_status="error",
               rationale="judge completion: error reading response body: remote error: tls: bad record MAC")))
    report = dict(per_case=rows+bad, meta=dict(lme_complete="false", lme_judged_cases="496", lme_prepared_snapshot_sha256=resume.PREPARED,
                  lme_prepared_snapshot_after_sha256=resume.PREPARED, lme_subject_graph_failures="0", lme_subject_graph_discovery_complete="true", lme_hydration_preflight_users="500"))
    return report, copy.deepcopy(rows), [r["case_id"] for r in report["per_case"]]


class ResumeTests(unittest.TestCase):
    def test_preserves_wrong_answers_and_retries_only_unjudged_tls(self):
        report, rows, ids = evidence()
        resume.validate_original(report, rows, ids)
        self.assertEqual(sum(not r["lme_correct"] for r in rows), 248)

    def test_rejects_wrong_retry_or_altered_success(self):
        for mutation in (lambda r, rows: rows[0].update(lme_correct=False),
                         lambda r, rows: r["per_case"][-1].update(lme_correct=False),
                         lambda r, rows: r["per_case"][-2].update(notes=["other error"])):
            report, rows, ids = evidence()
            mutation(report, rows)
            with self.assertRaises(ValueError):
                resume.validate_original(report, rows, ids)


if __name__ == "__main__":
    unittest.main()
