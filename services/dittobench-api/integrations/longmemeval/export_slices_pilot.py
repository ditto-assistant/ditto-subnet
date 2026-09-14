#!/usr/bin/env python3
"""Publish replayable public-fixture evidence, excluding provider IDs/reasoning.

Output is derived evidence, not byte-identical native reports. Original source
hashes are retained. Only explicit whitelists are exported; private final-cost
receipts stay local. Run with the same --control/--treatment/--launch-spec inputs
as analyze_slices_pilot, and --output for a new JSON bundle.
"""
import argparse
import json
from pathlib import Path
from analyze_slices_pilot import analyze, digest, require


def public_report(report):
    top = ("run_id", "git_sha", "prompt_sha", "tools_sha", "weights_sha", "judge_model")
    meta = ("lme_complete", "lme_subject_graph", "lme_require_graph", "lme_prepared_snapshot_unchanged",
            "lme_seed_context_mode", "lme_hydration_preflight", "lme_hydration_preflight_users", "lme_reasoning_effort",
            "lme_prompt_clock", "lme_prepared_snapshot_sha256", "lme_prepared_snapshot_after_sha256",
            "lme_cases_sha256", "lme_manifest_sha256", "lme_subject_graph_version", "lme_condition_sha256",
            "lme_subject_graph_calls", "lme_subject_graph_failures", "lme_subject_graph_discovery_complete",
            "lme_subject_graph_failure_counts_consistent", "lme_subject_graph_failure_schema")
    meta += tuple("lme_subject_graph_failures_" + reason for reason in
                  ("context_deadline", "context_canceled", "postgres_query_canceled", "graph_unavailable", "other"))
    data = ("query_status", "judge_status", "seed_context_mode", "seed_pair_count", "seed_pair_ids",
            "seed_context", "fixture_user", "prompt_tokens", "session_recall", "tools_called")
    result = {key: report[key] for key in top}
    result["meta"] = {key: report["meta"][key] for key in meta}
    result["per_case"] = []
    for row in report["per_case"]:
        clean = {key: row[key] for key in ("case_id", "query", "gold_answer", "hypothesis", "category", "model", "lme_correct")}
        clean["data"] = {key: row["data"].get(key) for key in data}
        result["per_case"].append(clean)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("control", "treatment", "launch-spec", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    inputs = [args.control, args.treatment, args.launch_spec]
    hashes = [digest(path) for path in inputs]
    spec = json.loads(args.launch_spec.read_text())
    control, treatment = (json.loads(path.read_text()) for path in inputs[:2])
    original = analyze(control, treatment, spec["case_ids"])
    a, b = public_report(control), public_report(treatment)
    require(analyze(a, b, spec["case_ids"]) == original, "export changed audited result")
    require(hashes == [digest(path) for path in inputs], "inputs changed during export")
    result = {"schema": "lme-slices-public-fixture-evidence-v1", "original_sha256": dict(zip(("control", "treatment", "launch_spec"), hashes)),
              "source_sha": spec["source_sha"], "binary_sha256": spec["binary_sha256"], "case_ids": spec["case_ids"],
              "control": a, "treatment": b,
              "scope": "Derived public dataset evidence. Excludes provider IDs, raw tool traces, reasoning and private receipt details. Local native reports are separately hash-pinned; run IDs collide and are not unique arm identifiers."}
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({"bytes": args.output.stat().st_size, "sha256": digest(args.output)}))


if __name__ == "__main__":
    main()
