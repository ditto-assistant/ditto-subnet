#!/usr/bin/env python3
"""Prove the native retry preserved every judged original row, including wrongs."""
import argparse
import json
from pathlib import Path
from analyze_slices_pilot import digest, require
from audit_backend_run import validate_hydration_preflight
from resume_slices_full_tls import FAILED, JUDGE_FAILED, SOURCE, INPUTS, ORIGINAL_RUN


def audit(original, final):
    a = {r["case_id"]: r for r in original["per_case"]}
    b = {r["case_id"]: r for r in final["per_case"]}
    require(len(a) == len(b) == len(original["per_case"]) == len(final["per_case"]) == 500 and a.keys() == b.keys(), "coverage differs")
    complete = {i for i, r in a.items() if type(r.get("lme_correct")) is bool}
    require(len(complete) == 496 and set(a)-complete == FAILED, "failure complement differs")
    require(all(a[i] == b[i] for i in complete), "judged original row altered")
    require(all(type(r.get("lme_correct")) is bool and r["data"].get("query_status") == r["data"].get("judge_status") == "ok" for r in b.values()), "final incomplete")
    require(original["git_sha"] == final["git_sha"] == SOURCE and final["meta"]["lme_complete"] == "true", "source/status differs")
    require(original["meta"]["lme_condition_sha256"] == final["meta"]["lme_condition_sha256"], "condition changed")
    require(original["meta"].get("lme_hydration_preflight_users") == "500" and original["meta"].get("lme_subject_graph_failures") == "0" and original["meta"].get("lme_subject_graph_discovery_complete") == "true", "original native gates failed")
    # Failed reader rows lack per-case hydration fields; original native meta
    # still records the complete preflight and graph counters, audited at retry.
    return {"preserved_judged_rows": len(complete), "preserved_wrong_answers": sum(not a[i]["lme_correct"] for i in complete),
            "newly_judged_case_ids": sorted(FAILED), "first_invocation_native_meta": {k:v for k,v in original["meta"].items() if k.startswith("lme_subject_graph_") and "fixture_builds" not in k},
            "final_hydration_and_graph": validate_hydration_preflight(final, list(b.values()), 500, True),
            "judge_failed_case_regenerated": {"case_id": JUDGE_FAILED, "original_answer": a[JUDGE_FAILED].get("hypothesis"),
                                              "final_answer": b[JUDGE_FAILED].get("hypothesis"), "final_correct": b[JUDGE_FAILED]["lme_correct"]},
            "scope": "Original496 judged rows preserved byte-semantically; exact4 unjudged cases recovered once. Original graph counters and final counters are invocation-specific, not duplicates to subtract from cost."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("original", "final", "output"):
        parser.add_argument("--"+key, required=True, type=Path)
    args = parser.parse_args()
    require(digest(args.original) == INPUTS["runs/"+ORIGINAL_RUN], "original artifact drift")
    result = audit(json.loads(args.original.read_text()), json.loads(args.final.read_text()))
    result.update(original_sha256=digest(args.original), final_sha256=digest(args.final), helper_sha256=digest(__file__))
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({k: result[k] for k in ("preserved_judged_rows", "preserved_wrong_answers", "newly_judged_case_ids")}))


if __name__ == "__main__":
    main()
