#!/usr/bin/env python3
"""Fail-closed, offline audit of private backend LongMemEval research evidence.

Never publishes answers, questions, provider generation IDs, or local paths.
Does not call a provider or change DittoBench production scoring.
"""

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys

from longmemeval_adapter import iter_json_array, normalize_timestamp

DATASET_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
DATASET_REVISION = "98d7416c24c778c2fee6e6f3006e7a073259d48f"
EVALUATOR_REVISION = "9e0b455f4ef0e2ab8f2e582289761153549043fc"
OPAQUE_ID_SCHEME = "lme-opaque-sha256-v1"
REFINEMENT_RECEIPT_VERSION = "native-semantic-save-v1"


class AuditError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise AuditError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_dataset(path):
    require(digest(path) == DATASET_SHA256, "dataset digest differs from pinned cleaned LongMemEval-S")
    result = {}
    for row in iter_json_array(Path(path)):
        qid = row["question_id"]
        require(qid not in result, "duplicate dataset question ID")
        result[qid] = {"category": row["question_type"], "question": row["question"],
                       "prompt_time": normalize_timestamp(row["question_date"])}
    require(len(result) == 500, "dataset must contain exactly 500 unique questions")
    return result


def load_run(path):
    """A final JSON report, or a JSONL checkpoint with per-case provenance."""
    source = Path(path).read_text()
    try:
        doc = json.loads(source)
    except json.JSONDecodeError:
        try:
            rows = [json.loads(line) for line in source.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise AuditError("malformed or interrupted JSONL evidence") from exc
        return {}, rows
    require(isinstance(doc, dict), "run must be an object or JSONL objects")
    if "per_case" in doc:
        return doc, doc["per_case"]
    return {}, [doc]


def wilson(correct, total):
    z = 1.959963984540054
    p = correct / total
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return [max(0.0, center - half), min(1.0, center + half)]


def validate_provenance(report):
    require(bool(report), "a final JSON report is required; checkpoint alone cannot attest source provenance")
    require(isinstance(report.get("run_id"), str) and report["run_id"], "missing run ID")
    require(re.fullmatch(r"[0-9a-f]{40}", report.get("git_sha", "")), "git_sha must be the full source commit")
    for field in ("prompt_sha", "tools_sha"):
        require(re.fullmatch(r"[0-9a-f]{64}", report.get(field, "")), f"{field} must be a SHA-256 digest")
    require(re.fullmatch(r"(?:learned:)?[0-9a-f]{64}", report.get("weights_sha", "")), "weights_sha must identify the actual learned-weight binary")
    for field in ("started_at", "finished_at"):
        require(isinstance(report.get(field), str) and report[field] and not report[field].startswith("0001-"), f"missing {field}")
    meta = report.get("meta", {})
    require(meta.get("lme_id_blinding_scheme") == OPAQUE_ID_SCHEME,
            "missing or incompatible opaque-ID blinding scheme; legacy labeled IDs are not clean accuracy evidence")
    for field in ("lme_manifest_sha256", "lme_cases_sha256", "lme_prepared_snapshot_sha256", "lme_prepared_snapshot_after_sha256"):
        require(re.fullmatch(r"[0-9a-f]{64}", meta.get(field, "")), f"missing or invalid {field}")
    require(meta.get("lme_prepared_snapshot_unchanged") == "true" and
            meta["lme_prepared_snapshot_sha256"] == meta["lme_prepared_snapshot_after_sha256"] and
            not meta.get("lme_prepared_snapshot_error"), "prepared fixture changed or could not be verified after answering")
    validate_refinement_completion(meta)


def validate_refinement_completion(meta):
    require(meta.get("lme_refinement_receipt_version") == REFINEMENT_RECEIPT_VERSION,
            "missing or incompatible native refinement receipt version")
    require(re.fullmatch(r"[0-9a-f]{64}", meta.get("lme_refinement_receipts_sha256", "")),
            "missing or invalid refinement receipt-set digest")
    counts = {}
    for field in ("lme_raw_pending_refinement", "lme_accepted_refinement_receipts"):
        value = meta.get(field)
        require(isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value),
                f"missing or invalid nonnegative {field}")
        counts[field] = int(value)
    raw, matched = counts["lme_raw_pending_refinement"], counts["lme_accepted_refinement_receipts"]
    require(matched <= raw, "matched refinement receipts exceed raw candidates")
    require(raw == matched, "native refinement remains pending after current-content receipt matching")
    return {"receipt_version": REFINEMENT_RECEIPT_VERSION,
            "receipts_sha256": meta["lme_refinement_receipts_sha256"],
            "raw_pending_refinement": raw, "accepted_refinement_receipts": matched,
            "remaining_pending_refinement": raw - matched,
            "evidence_boundary": "Backend matches native-save receipts to current scoped semantic content; offline counts do not independently query the database."}


def aggregate(rows):
    correct = sum(row["lme_correct"] for row in rows)
    return {"count": len(rows), "correct": correct, "qa_accuracy": correct / len(rows),
            "wilson_95_interval": wilson(correct, len(rows)),
            "empty_answers": sum(not row.get("hypothesis", "").strip() for row in rows)}


def operational_diagnostics(report, rows):
    """Count observed graph use separately from its flag and grade outcomes."""
    meta = report.get("meta", {})
    counters = {}
    names = ("calls", "failures", "candidates", "total_ms")
    available = ["lme_subject_graph_" + name in meta for name in names]
    require(not any(available) or all(available), "partial graph discovery counters")
    if all(available):
        for name in names:
            value = meta["lme_subject_graph_" + name]
            require(isinstance(value, str) and re.fullmatch(r"\d+", value), "invalid graph discovery counter")
            counters[name] = int(value)
        require(counters["failures"] <= counters["calls"], "graph failures exceed calls")
    graph = {"discovery_counters_available": all(available),
             "discovery_calls": counters.get("calls"),
             "discovery_failures": counters.get("failures"),
             "discovery_failure_fallback_rate": counters["failures"] / counters["calls"] if counters.get("calls") else None,
             "discovered_candidate_occurrences_not_unique": counters.get("candidates"),
             "discovery_total_ms": counters.get("total_ms"),
             "graph_seed_recorded_cases": 0, "cases_with_graph_seed_ids": 0,
             "graph_seed_id_occurrences": 0, "tool_trace_recorded_cases": 0,
             "neighbor_tool_trace_calls": 0, "cases_with_neighbor_tool_traces": 0,
             "neighbor_tool_traces_with_truncated_text": 0,
             "scope": "Discovery counters cover this invocation only, including seed-context preparation; not necessarily all resumed reader attempts. Candidate counts are occurrences, not globally unique IDs. Explicit neighbor-tool traces are separate and are not included in discovery counters. Graph seed IDs may also be found by stock retrieval, not graph-only additions. Enabled does not mean effective graph coverage."}
    metrics = {}
    for row in rows:
        data = row["data"]
        if "graph_seed_pair_ids" in data:
            ids = data["graph_seed_pair_ids"] or []
            require(isinstance(ids, list) and all(isinstance(i, str) and i for i in ids), "invalid graph seed IDs")
            graph["graph_seed_recorded_cases"] += 1
            graph["cases_with_graph_seed_ids"] += bool(ids)
            graph["graph_seed_id_occurrences"] += len(ids)
        if "tool_trace" in data:
            traces = data["tool_trace"] or []
            require(isinstance(traces, list) and all(isinstance(t, dict) for t in traces), "invalid tool trace records")
            graph["tool_trace_recorded_cases"] += 1
            neighbors = [t for t in traces if t.get("name") == "explore_subject_neighbors"]
            graph["neighbor_tool_trace_calls"] += len(neighbors)
            graph["cases_with_neighbor_tool_traces"] += bool(neighbors)
            graph["neighbor_tool_traces_with_truncated_text"] += sum(
                any(isinstance(t.get(part), dict) and t[part].get("truncated") is True
                    for part in ("arguments", "result")) for t in neighbors)
    for field in ("latency_ms", "prompt_tokens", "output_tokens"):
        values = [row["data"][field] for row in rows if field in row["data"]]
        require(all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values), "invalid per-case latency/token metric")
        metrics[field] = {"recorded_cases": len(values), "complete_case_coverage": len(values) == len(rows)}
        if values and len(values) == len(rows):
            metrics[field].update(sum=sum(values), mean=statistics.mean(values), median=statistics.median(values),
                                  p95_nearest_rank=sorted(values)[math.ceil(0.95 * len(values)) - 1])
    return {"graph_utilization": graph, "successful_per_case_metrics": metrics,
            "metric_scope": "Recomputed from all recorded selected per-case observations, never resumed Standard/Speed aggregates. QueryCase latency starts after seed-context preparation, so it excludes serial graph seed discovery. Also excludes failed attempts, separate judge/preparation calls and seed-context re-preparation; sums are not campaign wall time or monetary spend. Compare whole invocation intervals separately."}


def audit_rows(report, rows, dataset, condition):
    require(isinstance(rows, list), "per_case must be an array")
    require(len(rows) == len(dataset), "incomplete or overfull evidence: row count differs from dataset")
    indexed, fixture_users, response_ids = {}, set(), set()
    providers, tool_calls = Counter(), Counter()
    condition_sha = report.get("meta", {}).get("lme_condition_sha256")
    if report:
        require(isinstance(condition_sha, str) and re.fullmatch(r"[0-9a-f]{64}", condition_sha), "missing immutable condition digest")
    for row in rows:
        qid = row.get("case_id")
        require(qid in dataset, "unexpected question ID in evidence")
        require(qid not in indexed, "duplicate question ID; reruns must not be silently deduplicated")
        require(row.get("suite") == "longmemeval", "unexpected suite")
        require(row.get("model") == condition["answer_model"], "answer model differs from declared condition")
        require(row.get("category") == dataset[qid]["category"], "question type differs from dataset")
        require(row.get("query") == dataset[qid]["question"], "question text differs from pinned dataset")
        require(type(row.get("lme_correct")) is bool, "missing or non-boolean judge verdict")
        require(isinstance(row.get("hypothesis", ""), str), "hypothesis must be text")
        require(row.get("hypothesis", "").strip(), "empty final answer is incomplete under this strict condition")
        data = row.get("data")
        require(isinstance(data, dict), "per-case data/provenance is required")
        if report:
            require(data.get("condition_sha256") == condition_sha, "per-case condition digest differs from report")
        require(data.get("judge_status") == "ok", "missing/failed judge status; false is not proof of successful judging")
        require(data.get("query_status") == "ok", "missing/failed query status")
        require(data.get("answer_source") == "final", "answer must come from final text, not reasoning fallback")
        require(not data.get("judge_error") and not data.get("error"), "case contains an operational error")
        require(data.get("judge_model", report.get("judge_model")) == condition["judge_model"], "judge model differs from declared condition")
        require(type(data.get("qa_correct")) is bool and data["qa_correct"] == row["lme_correct"], "inconsistent judge verdicts")
        require(data.get("prompt_clock") == condition["prompt_clock"], "prompt clock differs from declared condition")
        require(data.get("prompt_time") == dataset[qid]["prompt_time"], "prompt time differs from dataset question date")
        require(data.get("reasoning_effort") == condition["reasoning_effort"], "reasoning effort differs from declared condition")
        for flag in ("require_graph", "graph_retrieval"):
            require(type(data.get(flag)) is bool and data[flag] == condition[flag], f"missing or mismatched {flag}")
        user = data.get("fixture_user", "")
        require(user == "lme_s_" + qid and user not in fixture_users, "fixture user must match its exact question ID")
        fixture_users.add(user)
        identities = data.get("provider_responses")
        require(isinstance(identities, list) and len(identities) > 0, "missing answer-provider identities")
        for identity in identities:
            require(isinstance(identity, dict), "malformed provider identity")
            # Go's pre-existing exported identity struct uses capitalized keys.
            rid = identity.get("id", identity.get("ID"))
            model = identity.get("model", identity.get("Model"))
            provider = identity.get("provider", identity.get("Provider"))
            require(isinstance(rid, str) and rid.strip(), "missing provider response ID")
            require(rid not in response_ids, "duplicate provider response ID")
            require(model == condition["answer_model"], "observed provider model differs from requested model")
            require(provider == condition["answer_provider"], "observed provider differs from declared provider")
            response_ids.add(rid)
            providers[provider] += 1
        called = data.get("tools_called") or []
        require(isinstance(called, list) and all(isinstance(name, str) for name in called), "malformed tool call names")
        tool_calls.update(called)
        indexed[qid] = row
    require(set(indexed) == set(dataset), "question-ID coverage differs from dataset")
    overall = aggregate(rows)
    if report:
        require(report.get("judge_model") == condition["judge_model"], "report judge identity mismatch")
        metadata = report.get("meta", {})
        require(metadata.get("lme_cases_completed") == f"{len(dataset)}/{len(dataset)}", "report completion differs from audited coverage")
        suite = report.get("suites", {}).get("longmemeval", {})
        value = suite.get("overall_accuracy")
        require(type(value) in (int, float) and math.isfinite(value) and abs(value - overall["qa_accuracy"]) < 1e-9, "reported headline differs from audited QA accuracy")
    summary = {"status": "complete_audited", "condition": condition, "overall": overall,
               "by_question_type": {category: aggregate([r for r in rows if r["category"] == category])
                                    for category in sorted({r["category"] for r in rows})},
               "answer_provider_turns": dict(providers), "tool_call_counts": dict(sorted(tool_calls.items())),
               "isolated_fixture_users": len(fixture_users), "unique_answer_provider_response_ids": len(response_ids)}
    summary["operational_diagnostics"] = operational_diagnostics(report, rows)
    return summary, indexed


def validate_paired_provenance(left, right):
    """Graph implementation/tool hashes may differ; common inputs must not."""
    checked, unavailable = [], []
    for field in ("weights_sha", "prompt_sha", "judge_model"):
        first, second = left.get(field), right.get(field)
        if field == "weights_sha":
            first = first.removeprefix("learned:") if isinstance(first, str) else first
            second = second.removeprefix("learned:") if isinstance(second, str) else second
        require(first and first == second, f"paired {field} differs or is missing")
        checked.append(field)
    # The combined condition hash includes source identity, so it legitimately
    # differs between stock and graph implementations. Compare input hashes
    # individually, never condition hashes or machine-local manifest paths.
    for field in ("lme_dataset_sha256", "lme_manifest_sha256", "lme_cases_sha256", "lme_prepared_snapshot_sha256", "lme_refinement_receipts_sha256"):
        first, second = left.get("meta", {}).get(field), right.get("meta", {}).get(field)
        if first is None and second is None:
            unavailable.append(field)
            continue
        require(isinstance(first, str) and re.fullmatch(r"[0-9a-f]{64}", first) and first == second,
                f"paired {field} differs, is malformed, or is available on only one side")
        if field == "lme_dataset_sha256":
            require(first == DATASET_SHA256, "paired dataset digest differs from pinned dataset")
        checked.append(field)
    return {"matched_provenance_fields": checked, "unavailable_provenance_fields": unavailable,
            "fixture_snapshot_verified": "lme_prepared_snapshot_sha256" in checked,
            "limitation": "Missing snapshot evidence is not equivalence; graph attribution still requires a frozen prepared-fixture audit."}


def compare(left, right):
    require(set(left) == set(right), "paired comparison requires identical complete question IDs")
    both_correct = sum(left[q]["lme_correct"] and right[q]["lme_correct"] for q in left)
    left_only = sum(left[q]["lme_correct"] and not right[q]["lme_correct"] for q in left)
    right_only = sum(not left[q]["lme_correct"] and right[q]["lme_correct"] for q in left)
    discordant = left_only + right_only
    p = min(1.0, 2 * sum(math.comb(discordant, k) for k in range(min(left_only, right_only) + 1)) / (2 ** discordant)) if discordant else 1.0
    return {"count": len(left), "both_correct": both_correct, "left_only_correct": left_only,
            "right_only_correct": right_only, "both_incorrect": len(left) - both_correct - discordant,
            "right_minus_left_accuracy": (right_only - left_only) / len(left),
            "exact_mcnemar_two_sided_p": p,
            "interpretation": "paired descriptive evidence; no repeated-run variance or causal guarantee"}


def compare_graph_seeds(left, right):
    require(set(left) == set(right), "paired graph seeds require identical question IDs")
    result = {"compared_cases": 0, "unavailable_cases": 0,
              "cases_with_graph_marked_seeds": 0, "graph_marked_seed_occurrences": 0,
              "cases_with_graph_marked_seeds_absent_from_off_seed": 0,
              "graph_marked_seed_occurrences_absent_from_off_seed": 0,
              "graph_marked_seed_occurrences_also_in_off_seed": 0,
              "all_seed_sets_compared_cases": 0, "cases_with_different_seed_id_sets": 0,
              "graph_marked_seed_cases_with_verdict_change": 0,
              "graph_marked_seed_cases_both_correct": 0, "graph_marked_seed_cases_both_incorrect": 0,
              "interpretation": "Per-case ON graph-marked IDs minus corresponding OFF seed IDs, plus full seed-set comparison ignoring order. This is observed seed-set novelty, not causal attribution or proof that stock retrieval could never find the same memory. A frozen database does not establish identical rendered seed prompts or identify why seed sets differ."}
    for qid in left:
        first, second = left[qid]["data"], right[qid]["data"]
        require(type(first.get("graph_retrieval")) is bool and type(second.get("graph_retrieval")) is bool
                and first["graph_retrieval"] != second["graph_retrieval"], "paired seed comparison requires opposite graph flags")
        on, off = (first, second) if first["graph_retrieval"] else (second, first)
        if "graph_seed_pair_ids" not in on or "seed_pair_ids" not in off:
            result["unavailable_cases"] += 1
            continue
        marked, stock = on["graph_seed_pair_ids"] or [], off["seed_pair_ids"] or []
        require(all(isinstance(ids, list) and all(isinstance(i, str) and i for i in ids) for ids in (marked, stock)), "invalid paired seed ID lists")
        marked, stock = set(marked), set(stock)
        novel = marked - stock
        result["compared_cases"] += 1
        result["cases_with_graph_marked_seeds"] += bool(marked)
        result["graph_marked_seed_occurrences"] += len(marked)
        result["cases_with_graph_marked_seeds_absent_from_off_seed"] += bool(novel)
        result["graph_marked_seed_occurrences_absent_from_off_seed"] += len(novel)
        result["graph_marked_seed_occurrences_also_in_off_seed"] += len(marked & stock)
        if "seed_pair_ids" in on:
            on_seeds = on["seed_pair_ids"] or []
            require(isinstance(on_seeds, list) and all(isinstance(i, str) and i for i in on_seeds), "invalid ON seed IDs")
            result["all_seed_sets_compared_cases"] += 1
            result["cases_with_different_seed_id_sets"] += set(on_seeds) != stock
        if marked:
            first_correct, second_correct = left[qid]["lme_correct"], right[qid]["lme_correct"]
            result["graph_marked_seed_cases_with_verdict_change"] += first_correct != second_correct
            result["graph_marked_seed_cases_both_correct"] += first_correct and second_correct
            result["graph_marked_seed_cases_both_incorrect"] += not first_correct and not second_correct
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--condition", required=True, help="immutable, distinct research condition label")
    parser.add_argument("--answer-model", default="openai/gpt-5.6-luna")
    parser.add_argument("--answer-provider", default="OpenAI")
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--reasoning-effort", default="medium")
    parser.add_argument("--graph-retrieval", action="store_true")
    parser.add_argument("--paired-run", type=Path, help="same model/judge/clock; opposite graph_retrieval flag")
    parser.add_argument("--export-hypotheses", type=Path, help="PRIVATE official question_id/hypothesis JSONL")
    args = parser.parse_args(argv)
    inputs = {p.resolve() for p in (args.dataset, args.run, args.paired_run) if p}
    outputs = [p for p in (args.output, args.export_hypotheses) if p]
    require(len({p.resolve() for p in outputs}) == len(outputs), "output paths must be distinct")
    require(all(p.resolve() not in inputs and not p.exists() for p in outputs), "outputs must be new files, never input evidence")
    dataset = load_dataset(args.dataset)
    condition = {"label": args.condition, "answer_model": args.answer_model, "answer_provider": args.answer_provider,
                 "judge_model": args.judge_model, "reasoning_effort": args.reasoning_effort,
                 "prompt_clock": "question-date", "require_graph": True, "graph_retrieval": args.graph_retrieval}
    report, rows = load_run(args.run)
    validate_provenance(report)
    summary, indexed = audit_rows(report, rows, dataset, condition)
    summary["dataset"] = {"sha256": DATASET_SHA256, "revision": DATASET_REVISION, "questions": 500}
    summary["evidence_sha256"] = digest(args.run)
    summary["backend_provenance"] = {key: report.get(key) for key in ("run_id", "git_sha", "prompt_sha", "tools_sha", "weights_sha", "started_at", "finished_at")}
    summary["backend_provenance"]["condition_sha256"] = report["meta"]["lme_condition_sha256"]
    for field in ("lme_manifest_sha256", "lme_cases_sha256", "lme_prepared_snapshot_sha256", "lme_prepared_snapshot_after_sha256", "lme_prepared_snapshot_unchanged", "lme_id_blinding_scheme"):
        summary["backend_provenance"][field] = report["meta"][field]
    summary["backend_provenance"]["lme_source_evidence"] = report["meta"].get("lme_source_evidence")
    summary["refinement_completion"] = validate_refinement_completion(report["meta"])
    summary["cost_reporting"] = {
        "backend_estimate_valid": report.get("meta", {}).get("lme_cost_estimate_valid") == "true",
        "backend_estimate_invalid_reason": report.get("meta", {}).get("lme_cost_estimate_invalid_reason"),
        "policy": "No monetary amounts exported. Invalid/missing model prices must not be quoted as spend; provider billing is separate."}
    summary["limitations"] = ["Research only; not a production DittoBench score or a held-out leaderboard claim.",
                             "Observed answer-provider identities do not attest judge or dreaming provider identities.",
                             "Requires separate fixture completeness, dreaming stage, and trained-retriever provenance evidence.",
                             "Wilson interval assumes independent Bernoulli cases; not a model stochasticity interval."]
    if args.paired_run:
        other_condition = dict(condition, graph_retrieval=not args.graph_retrieval, label="paired-opposite-graph-retrieval")
        other_report, other_rows = load_run(args.paired_run)
        validate_provenance(other_report)
        other_summary, other_indexed = audit_rows(other_report, other_rows, dataset, other_condition)
        pairing = validate_paired_provenance(report, other_report)
        summary["paired_comparison"] = compare(indexed, other_indexed)
        summary["paired_comparison"]["provenance_validation"] = pairing
        summary["paired_comparison"]["right_evidence_sha256"] = digest(args.paired_run)
        summary["paired_comparison"]["right_condition"] = other_summary["condition"]
        summary["paired_comparison"]["right_operational_diagnostics"] = other_summary["operational_diagnostics"]
        summary["paired_comparison"]["graph_seed_comparison"] = compare_graph_seeds(indexed, other_indexed)
    # No output is written until every requested evidence set passes.
    with args.output.open("x") as out:
        out.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    if args.export_hypotheses:
        fd = os.open(args.export_hypotheses, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            for qid in sorted(indexed):
                out.write(json.dumps({"question_id": qid, "hypothesis": indexed[qid].get("hypothesis", "")}) + "\n")
    print(json.dumps({"status": summary["status"], "count": summary["overall"]["count"], "qa_accuracy": summary["overall"]["qa_accuracy"]}))


if __name__ == "__main__":
    try:
        main()
    except (AuditError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        sys.exit(1)
