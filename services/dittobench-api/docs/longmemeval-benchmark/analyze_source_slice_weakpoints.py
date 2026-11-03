"""Audit the pinned public 500-case evidence; no inference or private traces."""

import argparse
import collections
import hashlib
import json
from pathlib import Path


EXPECTED_SHA256 = "4dbce2f6b28bf717b0bccf36f845491d9cfc6520dfaa10442a2d1326d7e15ceb"


def analyze(path):
    raw = Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != EXPECTED_SHA256:
        raise ValueError("Evidence hash differs from the registered public bundle")
    evidence = json.loads(raw)
    control = {c["case_id"]: c for c in evidence["control"]["per_case"]}
    treatment = {c["case_id"]: c for c in evidence["treatment"]["per_case"]}
    if len(control) != 500 or control.keys() != treatment.keys():
        raise ValueError("Expected 500 matched cases")
    failures = [c for c in treatment.values() if not c["lme_correct"]]
    updates = [c for c in failures if c["category"] == "knowledge-update"]
    return {
        "evidence_sha256": EXPECTED_SHA256,
        "n": len(treatment),
        "control_correct": sum(bool(c["lme_correct"]) for c in control.values()),
        "slices_correct": sum(bool(c["lme_correct"]) for c in treatment.values()),
        "wins": sum(bool(c["lme_correct"]) and not control[k]["lme_correct"]
                    for k, c in treatment.items()),
        "regressions": [k for k, c in treatment.items()
                        if not c["lme_correct"] and control[k]["lme_correct"]],
        "remaining_errors_by_category": dict(sorted(collections.Counter(
            c["category"] for c in failures).items())),
        "knowledge_update_failures_without_tools": [
            c["case_id"] for c in updates if not c["data"]["tools_called"]],
        "prompt_tokens_mean": {
            label: sum(c["data"]["prompt_tokens"] for c in cases.values()) / 500
            for label, cases in (("control", control), ("slices", treatment))
        },
        "audited_cases": [
            {key: treatment[case_id][key] for key in
             ("case_id", "query", "gold_answer", "hypothesis")}
            for case_id in ("2698e78f", "852ce960")
        ],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.evidence), indent=2))
