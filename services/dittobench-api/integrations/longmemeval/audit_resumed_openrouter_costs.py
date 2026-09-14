#!/usr/bin/env python3
"""Union closed attempt journals without dropping charged failed-reader calls.

Offline unless --fetch is explicit. Private receipts never enter the public
summary. This audits captured spend, not answer correctness or full lifecycle.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import audit_openrouter_costs as costs


def union_journals(paths):
    owners, requests, receipts, hashes, unknown = {}, [], {}, [], []
    for path in paths:
        sha = costs.digest(path)
        if sha in hashes:
            raise ValueError("duplicate journal evidence")
        hashes.append(sha)
        for line in Path(path).read_text().splitlines():
            row = json.loads(line)
            generation = row.get("generation_id")
            if not generation:
                continue
            owner = (row.get("case_id"), row.get("stage"), row.get("attempt_id"))
            if generation in owners and owners[generation] != owner:
                raise ValueError("cross-journal generation ownership changed")
            owners[generation] = owner
        current_requests, current_receipts = costs.journal_evidence(path)
        unknown.extend(row for row in current_requests if not row.get("generation_id"))
        requests = costs.combine_requests(requests, [row for row in current_requests if row.get("generation_id")])
        for receipt in current_receipts:
            receipts[receipt["generation_id"]] = receipt
    return requests + unknown, list(receipts.values()), hashes


def prepare(report, journals, recoveries):
    journal_requests, observed, hashes = union_journals(journals)
    requests = costs.combine_requests(costs.report_requests(report), journal_requests)
    indexed = costs.receipt_index(observed)
    prior_receipts = [row for recovery in recoveries for row in recovery["receipts"]]
    prior = costs.receipt_index(prior_receipts)
    captured = {row["generation_id"] for row in requests if row["generation_id"]}
    if set(prior) - captured:
        raise ValueError("prior receipt is outside selected journal/report generation set")
    for generation, receipt in prior.items():
        if costs.final_receipt(receipt):
            indexed[generation] = receipt
    return requests, list(indexed.values()), hashes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--journal", required=True, action="append", type=Path)
    parser.add_argument("--recovery", action="append", default=[], type=Path)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument("--private-output", required=True, type=Path)
    parser.add_argument("--summary-output", required=True, type=Path)
    args = parser.parse_args()
    inputs = [args.report, *args.journal, *args.recovery, Path(__file__), Path(costs.__file__)]
    before = {path.resolve(): costs.digest(path) for path in inputs}
    outputs = [args.private_output, args.summary_output]
    if len({path.resolve() for path in outputs}) != 2 or any(path.exists() or path.resolve() in before for path in outputs):
        raise ValueError("outputs must be distinct new files")
    report = json.loads(args.report.read_text())
    requests, receipts, journal_hashes = prepare(report, args.journal, [json.loads(p.read_text()) for p in args.recovery])
    reused = sum(costs.final_receipt(row) for row in receipts)
    if args.fetch:
        key = os.environ.get("LOCAL_OPENROUTER_API_KEY", "")
        if not key:
            raise ValueError("LOCAL_OPENROUTER_API_KEY is required for --fetch")
        receipts = costs.reconcile_receipts(requests, receipts, key)
    summary = costs.summarize(requests, receipts)
    if any(costs.digest(path) != sha for path, sha in before.items()):
        raise ValueError("input changed during reconciliation")
    result = {"schema": "resumed-captured-cost-union-v1", "captured_at": datetime.now(timezone.utc).isoformat(),
              "report_run_id": report.get("run_id"), "report_sha256": costs.digest(args.report),
              "journal_sha256": journal_hashes, "recovery_sha256": [costs.digest(p) for p in args.recovery],
              "union_helper_sha256": costs.digest(__file__), "cost_helper_sha256": costs.digest(costs.__file__),
              "reused_final_receipts": reused, "summary": summary,
              "scope": "Union of captured reader/judge calls across supplied attempts, including failed calls. No inference. Original prep, query embeddings and calls without captured IDs are not established. Not an invoice or accuracy audit."}
    costs.write_new(args.private_output, dict(result, receipts=receipts))
    result["private_receipt_evidence_sha256"] = costs.digest(args.private_output)
    costs.write_new(args.summary_output, result)
    print(json.dumps({"priced_generations": summary["priced_generations"], "missing_generations": summary["missing_generations"],
                      "reused_final_receipts": reused, "estimated_captured_cost_usd": summary["selected_generation_estimated_cost_usd"],
                      "summary_sha256": costs.digest(args.summary_output)}))


if __name__ == "__main__":
    main()
