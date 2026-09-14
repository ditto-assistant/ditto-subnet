#!/usr/bin/env python3
"""Offline full500 paired audit and whitelist evidence export; never regrades."""
import argparse
import json
import math
from pathlib import Path
from analyze_slices_pilot import analyze, digest, require, metrics
from export_slices_pilot import public_report

CATEGORIES = {"single-session-user": 70, "single-session-assistant": 56,
              "single-session-preference": 30, "multi-session": 133,
              "knowledge-update": 78, "temporal-reasoning": 133}


def wilson(correct, total):
    z = 1.959963984540054
    p = correct / total
    center = (p + z*z/(2*total))/(1+z*z/total)
    width = z*math.sqrt(p*(1-p)/total + z*z/(4*total*total))/(1+z*z/total)
    return [center-width, center+width]


def full_analysis(control, treatment, ids, pilot_ids):
    require(len(set(pilot_ids)) == len(pilot_ids) == 60 and set(pilot_ids) <= set(ids), "invalid prior pilot subset")
    result = analyze(control, treatment, ids, CATEGORIES)
    require(control["run_id"] != treatment["run_id"], "run ID collision")
    a = {r["case_id"]: r for r in control["per_case"]}
    b = {r["case_id"]: r for r in treatment["per_case"]}
    result["previously_piloted_60"] = metrics(a, b, pilot_ids)
    result["remaining_440"] = metrics(a, b, sorted(set(ids)-set(pilot_ids)))
    result["wilson_95"] = {name: wilson(result[name+"_correct"], 500) for name in ("summary", "slices")}
    result["scope"] = "Full500 isolated LongMemEval-S matched presentation experiment, graph ON both arms. One first-pass run per arm plus explicitly documented failure-only recovery, not repeated stochastic trials. No clean held-out or graph-treatment claim. Prior60 and remaining440 shown separately; ranker training overlap remains."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("control", "treatment", "launch-spec", "pilot-evidence", "output", "public-output"):
        parser.add_argument("--"+key, required=True, type=Path)
    args = parser.parse_args()
    inputs = [args.control, args.treatment, args.launch_spec, args.pilot_evidence]
    hashes = {str(p): digest(p) for p in inputs}
    a, b, spec, pilot = (json.loads(p.read_text()) for p in inputs)
    require(a["git_sha"] == b["git_sha"] == spec["source_sha"], "native reports differ from source freeze")
    require(a["meta"]["lme_manifest_sha256"] == "a232c2b7ee77f06597535416682c00187f9c44bb2a324de7ccd5dce3fa13ccef", "prepared manifest differs")
    result = full_analysis(a, b, spec["case_ids"], pilot["case_ids"])
    clean_a, clean_b = public_report(a), public_report(b)
    require(full_analysis(clean_a, clean_b, spec["case_ids"], pilot["case_ids"]) == result, "export changed audit")
    require(hashes == {str(p): digest(p) for p in inputs}, "input changed")
    outputs = [args.output, args.public_output]
    require(len({p.resolve() for p in outputs}) == 2 and not any(p.exists() for p in outputs), "outputs must be distinct new files")
    result.update(evidence_sha256=hashes, helper_sha256=digest(__file__), shared_helper_sha256=digest(Path(__file__).with_name("analyze_slices_pilot.py")))
    bundle = {"schema": "lme-slices-full-public-fixture-evidence-v1", "original_sha256": hashes,
              "source_sha": spec["source_sha"], "binary_sha256": spec["binary_sha256"], "case_ids": spec["case_ids"],
              "pilot_case_ids": pilot["case_ids"], "control": clean_a, "treatment": clean_b,
              "scope": "Derived public fixture evidence. Provider IDs, private cost receipts, raw tool traces and reasoning excluded."}
    for path, value in zip(outputs, [result, bundle]):
        with path.open("x") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
    print(json.dumps({k: result[k] for k in ("cases", "summary_correct", "slices_correct", "ordered_seed_parity_cases", "baseline_payload_preserved_cases")}))


if __name__ == "__main__":
    main()
