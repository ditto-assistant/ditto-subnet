#!/usr/bin/env python3
"""Offline paired source-slices pilot audit. Never regrades or runs inference."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics
from audit_backend_run import validate_hydration_preflight


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def index(report, mode, case_ids, category_counts=None):
    expected = sum(category_counts.values()) if category_counts is not None else 60
    meta = report["meta"]
    for key in ("lme_complete", "lme_subject_graph", "lme_require_graph", "lme_prepared_snapshot_unchanged"):
        require(meta[key] == "true", f"failed report gate: {key}")
    require(meta["lme_seed_context_mode"] == mode, "wrong arm")
    require(meta["lme_hydration_preflight_users"] == str(expected), "hydration coverage differs")
    require(meta["lme_reasoning_effort"] == "medium" and meta["lme_prompt_clock"] == "question-date", "reader condition differs")
    require(meta["lme_prepared_snapshot_sha256"] == meta["lme_prepared_snapshot_after_sha256"], "fixture changed")
    rows = {row["case_id"]: row for row in report["per_case"]}
    require(len(rows) == len(report["per_case"]) == len(case_ids) == expected and set(rows) == set(case_ids), "cohort mismatch")
    counts = Counter(row["category"] for row in rows.values())
    require(counts == category_counts if category_counts is not None else sorted(counts.values()) == [10] * 6, "category imbalance")
    validate_hydration_preflight(report, list(rows.values()), expected, True)
    for row in rows.values():
        require(type(row["lme_correct"]) is bool, "missing verdict")
        require(row["model"] == "openai/gpt-5.6-luna", "reader differs")
        data = row["data"]
        require(data["query_status"] == data["judge_status"] == "ok", "failed case")
        require(data["seed_context_mode"] == mode and data["seed_pair_count"] > 0, "case mode/hydration differs")
        trace = data["seed_context"]
        raw = trace["text"].encode()
        require(not trace["truncated"] and trace["bytes"] == len(raw) and trace["sha256"] == hashlib.sha256(raw).hexdigest(), "seed trace incomplete/corrupt")
        require(json.loads(trace["text"])["memories"], "empty seed payload")
    return rows


def metrics(a, b, ids):
    wins = sorted(i for i in ids if not a[i]["lme_correct"] and b[i]["lme_correct"])
    losses = sorted(i for i in ids if a[i]["lme_correct"] and not b[i]["lme_correct"])
    return {"cases": len(ids), "summary_correct": sum(a[i]["lme_correct"] for i in ids),
            "slices_correct": sum(b[i]["lme_correct"] for i in ids), "wins": wins, "losses": losses}


def analyze(control, treatment, case_ids, category_counts=None):
    a, b = index(control, "summary", case_ids, category_counts), index(treatment, "source-slices-v1", case_ids, category_counts)
    for key in ("git_sha", "prompt_sha", "tools_sha", "weights_sha", "judge_model"):
        require(control[key] == treatment[key], f"cross-arm identity differs: {key}")
    require(control["judge_model"] == "google/gemini-3.1-flash-lite", "judge differs")
    for key in ("lme_cases_sha256", "lme_manifest_sha256", "lme_prepared_snapshot_sha256", "lme_subject_graph_version"):
        require(control["meta"][key] == treatment["meta"][key], f"cross-arm evidence differs: {key}")
    require(control["meta"]["lme_condition_sha256"] != treatment["meta"]["lme_condition_sha256"], "resume barrier absent")
    ordered, payload = [], []
    for cid in case_ids:
        for key in ("query", "gold_answer", "category", "model"):
            require(a[cid][key] == b[cid][key], "case identity differs")
        left, right = a[cid]["data"], b[cid]["data"]
        if left["seed_pair_ids"] == right["seed_pair_ids"]:
            ordered.append(cid)
        lm = json.loads(left["seed_context"]["text"])["memories"]
        rm = json.loads(right["seed_context"]["text"])["memories"]
        if len(lm) == len(rm) and all(all(right_mem.get(k) == v for k, v in left_mem.items()) for left_mem, right_mem in zip(lm, rm)):
            payload.append(cid)
    result = metrics(a, b, case_ids)
    result["ordered_seed_parity_cases"] = len(ordered)
    result["baseline_payload_preserved_cases"] = len(payload)
    matched = sorted(set(ordered) & set(payload))
    result["matched_presentation_subset"] = metrics(a, b, matched)
    result["retrieval_or_payload_drift_case_ids"] = sorted(set(case_ids) - set(matched))
    result["by_category"] = {category: metrics(a, b, [i for i in case_ids if a[i]["category"] == category])
                             for category in sorted({a[i]["category"] for i in case_ids})}
    result["arms"] = {}
    for name, report, rows in (("summary", control, a), ("source-slices-v1", treatment, b)):
        result["arms"][name] = {key: report[key] for key in ("run_id", "git_sha", "prompt_sha", "tools_sha", "weights_sha", "judge_model")}
        result["arms"][name].update(mean_seed_bytes=statistics.mean(r["data"]["seed_context"]["bytes"] for r in rows.values()),
                                   mean_prompt_tokens=statistics.mean(r["data"]["prompt_tokens"] for r in rows.values()),
                                   mean_session_recall=statistics.mean(r["data"]["session_recall"] for r in rows.values()),
                                   no_tool_cases=sum(not r["data"].get("tools_called") for r in rows.values()))
        result["arms"][name]["hydration_and_graph"] = validate_hydration_preflight(report, list(rows.values()), len(case_ids), True)
    result["scope"] = "Exploratory balanced 60-case paired pilot; not a full-500 score, held-out result or graph ablation. No regrading. Costs and graph SQL logs audited separately."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--treatment", required=True, type=Path)
    parser.add_argument("--launch-spec", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    paths = [args.control, args.treatment, args.launch_spec]
    before = {str(p): digest(p) for p in paths}
    result = analyze(json.loads(args.control.read_text()), json.loads(args.treatment.read_text()), json.loads(args.launch_spec.read_text())["case_ids"])
    require(before == {str(p): digest(p) for p in paths}, "input changed during audit")
    result["evidence_sha256"] = before
    result["helper_sha256"] = digest(__file__)
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({k: result[k] for k in ("cases", "summary_correct", "slices_correct", "ordered_seed_parity_cases", "baseline_payload_preserved_cases")}))


if __name__ == "__main__":
    main()
