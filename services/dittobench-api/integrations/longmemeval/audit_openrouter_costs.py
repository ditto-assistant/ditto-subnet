#!/usr/bin/env python3
"""Read-only generation receipts; never infer missing charges as zero.

Only GET https://openrouter.ai/api/v1/generation is permitted. Credentials are
read from process environment, never persisted. Output excludes response text.
"""
import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import urllib.error
import urllib.parse
import urllib.request

GENERATION = re.compile(r"gen-[A-Za-z0-9_-]{1,180}\Z")
ENDPOINT = "https://openrouter.ai/api/v1/generation"
# Exact requested ID -> canonical_slug observed in OpenRouter /api/v1/models.
# No prefix/date stripping: unrelated dated variants must fail validation.
MODEL_ALIASES = {"openai/gpt-5.6-luna": "openai/gpt-5.6-luna-20260709",
                 "google/gemini-3.1-flash-lite": "google/gemini-3.1-flash-lite-20260507"}


def model_matches(requested, resolved):
    return requested == resolved or MODEL_ALIASES.get(requested) == resolved


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def money(value):
    if value is None or isinstance(value, bool):
        raise ValueError("missing or invalid charge")
    try:
        result = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("invalid charge") from error
    if not result.is_finite() or result < 0:
        raise ValueError("charge must be finite and nonnegative")
    return result


def decimal_stats(values):
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return None
    midpoint = count // 2
    median = ordered[midpoint] if count % 2 else (ordered[midpoint - 1] + ordered[midpoint]) / 2
    return {"mean": str(sum(ordered, Decimal(0)) / count), "median": str(median),
            "p95_nearest_rank": str(ordered[math.ceil(0.95 * count) - 1])}


def report_requests(report):
    """Final saved reader generations only; not proof all attempts were saved."""
    requests, seen_cases, owners = [], set(), {}
    for case in report.get("per_case", []):
        identity = (case.get("case_id"), case.get("model"))
        if not all(isinstance(v, str) and v for v in identity) or identity in seen_cases:
            raise ValueError("missing/duplicate case identity")
        seen_cases.add(identity)
        refs = case.get("data", {}).get("provider_responses", [])
        seen_ids = set()
        for ref in refs:
            generation = ref.get("ID")
            if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
                raise ValueError("invalid saved generation ID")
            if generation in seen_ids:
                continue  # Repeated stream ID is one charge, not another request.
            seen_ids.add(generation)
            if generation in owners and owners[generation] != identity:
                raise ValueError("generation attributed to multiple cases")
            owners[generation] = identity
            requests.append({"case_id": identity[0], "stage": "reader",
                             "model": ref.get("Model", identity[1]),
                             "generation_id": generation})
        if not seen_ids:
            requests.append({"case_id": identity[0], "stage": "reader",
                             "model": identity[1], "generation_id": None})
    if not seen_cases:
        raise ValueError("no cases")
    return requests


def journal_evidence(path):
    """Latest cumulative stream record per generation; retain failed-case calls.

    Input contract: passive backend checkpoint+'.provider-usage.jsonl'. It does
    not prove coverage of calls that failed before a provider ID was received.
    """
    latest, owners, models, unpriced = {}, {}, {}, []
    with Path(path).open() as stream:
        for line in stream:
            row = json.loads(line)
            case, stage, generation = row.get("case_id"), row.get("stage"), row.get("generation_id")
            if not isinstance(case, str) or not case or stage not in ("reader", "judge"):
                raise ValueError("invalid journal attribution")
            request = {"case_id": case, "stage": stage, "generation_id": generation,
                       "model": row.get("model")}
            if request["model"] is not None and not isinstance(request["model"], str):
                raise ValueError("invalid journal model")
            if not generation:
                unpriced.append(request)
                continue
            if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
                raise ValueError("invalid journal generation ID")
            owner = (case, stage, row.get("attempt_id"))
            if generation in owners and owners[generation] != owner:
                raise ValueError("journal generation ownership changed")
            owners[generation] = owner
            if request["model"]:
                if models.get(generation) and models[generation] != request["model"]:
                    raise ValueError("journal model identity changed")
                models[generation] = request["model"]
            request["model"] = models.get(generation)
            receipt = {"generation_id": generation, "status": "cost_missing", "model": request["model"]}
            if row.get("cost_status") == "reported_usage_cost":
                receipt.update(status="provisional", observed_cost_usd=str(money(row.get("cost_credits"))),
                               basis="openrouter_response.usage.cost", unit_conversion="USD-denominated OpenRouter credits")
            latest[generation] = (request, receipt)
    for request, receipt in latest.values():
        if not request["model"]:
            raise ValueError("journal model unresolved after aggregation")
    return [v[0] for v in latest.values()] + unpriced, [v[1] for v in latest.values()]


def combine_requests(saved, journal):
    combined = {}
    missing = []
    for request in saved + journal:
        generation = request.get("generation_id")
        if not generation:
            missing.append(request)
            continue
        if generation in combined and combined[generation] != request:
            raise ValueError("report/journal generation attribution mismatch")
        combined[generation] = request
    known = {(row["case_id"], row["stage"]) for row in combined.values()}
    # Saved-report placeholders are resolved by journal IDs; genuine unknown-ID
    # journal events must remain unpriced, even when other calls were captured.
    journal_missing = [row for row in journal if not row.get("generation_id")]
    missing_saved = [row for row in saved if not row.get("generation_id") and (row["case_id"], row["stage"]) not in known]
    return list(combined.values()) + missing_saved + journal_missing


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect rejected", headers, fp)


def sanitize_receipt(generation, raw):
    data = raw.get("data", {})
    if data.get("id") != generation:
        raise ValueError("generation receipt identity mismatch")
    charge = money(data.get("total_cost"))
    result = {"generation_id": generation, "status": "ok",
              "total_cost_usd": str(charge), "basis": "openrouter_generation.total_cost"}
    result["charge_scope"] = "OpenRouter account charge only; excludes separately billed BYOK vendor usage"
    upstream = data.get("upstream_inference_cost")
    result["upstream_inference_cost_usd"] = None if upstream is None else str(money(upstream))
    result["upstream_cost_classification"] = (
        "provider_reported_byok_estimate_not_vendor_invoice" if data.get("is_byok") is True
        else "not_added_for_non_byok_or_unknown_route")
    for key in ("model", "provider_name", "created_at", "is_byok", "cancelled",
                "native_tokens_prompt", "native_tokens_completion", "native_tokens_reasoning",
                "native_tokens_cached", "tokens_prompt", "tokens_completion"):
        value = data.get(key)
        if value is not None:
            result[key] = value
    return result


def fetch_receipt(generation, key, opener=None):
    if not GENERATION.fullmatch(generation):
        raise ValueError("invalid generation ID")
    opener = opener or urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(ENDPOINT + "?" + urllib.parse.urlencode({"id": generation}),
                                     headers={"Authorization": "Bearer " + key})
    try:
        with opener.open(request, timeout=30) as response:
            payload = response.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                raise ValueError("oversized generation response")
            return sanitize_receipt(generation, json.loads(payload, parse_float=Decimal))
    except urllib.error.HTTPError as error:
        return {"generation_id": generation, "status": "http_error", "http_status": error.code}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"generation_id": generation, "status": "transport_error"}
    except (ValueError, TypeError):
        return {"generation_id": generation, "status": "invalid_receipt"}


def final_receipt(receipt):
    return receipt.get("status") == "ok" and receipt.get("basis") == "openrouter_generation.total_cost"


def receipt_index(receipts):
    indexed = {}
    for receipt in receipts:
        generation = receipt.get("generation_id")
        if generation in indexed:
            raise ValueError("duplicate generation receipt")
        indexed[generation] = receipt
    return indexed


def observed_cost(receipt):
    value = receipt.get("observed_cost_usd")
    if value is None and receipt.get("basis") == "openrouter_response.usage.cost":
        value = receipt.get("total_cost_usd")  # Earlier audit format, still provisional.
    return None if value is None else str(money(value))


def merge_receipts(saved, journal):
    indexed = receipt_index(saved)
    for generation, row in receipt_index(journal).items():
        prior = indexed.get(generation)
        if prior and final_receipt(prior):
            # Final GET cost can legitimately differ from an earlier stream
            # snapshot. Retain both without summing or requiring equality.
            prior = dict(prior)
            if observed_cost(row) is not None:
                prior["observed_cost_usd"] = observed_cost(row)
            indexed[generation] = prior
        else:
            indexed[generation] = row
    return list(indexed.values())


def reconcile_receipts(requests, receipts, key, fetch=fetch_receipt):
    indexed = receipt_index(receipts)
    for generation in sorted({row["generation_id"] for row in requests if row["generation_id"]}):
        prior = indexed.get(generation)
        if prior and final_receipt(prior):
            money(prior.get("total_cost_usd"))
            continue
        receipt = fetch(generation, key)
        if prior and observed_cost(prior) is not None:
            receipt["observed_cost_usd"] = observed_cost(prior)
        indexed[generation] = receipt
        if receipt.get("http_status") in (401, 402, 403, 429):
            break
    return list(indexed.values())


def summarize(requests, receipts):
    indexed = receipt_index(receipts)
    totals, counts, missing = defaultdict(Decimal), defaultdict(int), defaultdict(int)
    provisional, observed_counts = defaultdict(Decimal), defaultdict(int)
    byok_estimates, byok_counts, byok_missing = defaultdict(Decimal), defaultdict(int), defaultdict(int)
    route_unknown = defaultdict(int)
    resolved_models = set()
    stages, seen = set(), set()
    for request in requests:
        key = (request["case_id"], request["stage"])
        stages.add(key)
        generation = request.get("generation_id")
        if generation:
            if generation in seen:
                raise ValueError("duplicate request attribution")
            seen.add(generation)
        receipt = indexed.get(generation, {})
        if receipt.get("model") and not model_matches(request["model"], receipt["model"]):
            raise ValueError("receipt model does not match attributed request")
        if receipt.get("model"):
            resolved_models.add((request["model"], receipt["model"]))
        if observed_cost(receipt) is not None:
            provisional[key] += money(observed_cost(receipt))
            observed_counts[key] += 1
        if not final_receipt(receipt):
            missing[key] += 1
            continue
        totals[key] += money(receipt.get("total_cost_usd"))
        counts[key] += 1
        if receipt.get("is_byok") is True:
            byok_counts[key] += 1
            if receipt.get("upstream_inference_cost_usd") is None:
                byok_missing[key] += 1
            else:
                byok_estimates[key] += money(receipt["upstream_inference_cost_usd"])
        elif receipt.get("is_byok") is not False:
            route_unknown[key] += 1
    rows = [{"case_id": case, "stage": stage,
             "recorded_cost_usd": str(totals[(case, stage)]),
             "provisional_observed_cost_usd": str(provisional[(case, stage)]),
             "generations_with_observed_cost": observed_counts[(case, stage)],
             "byok_generations": byok_counts[(case, stage)],
             "byok_upstream_estimate_usd": str(byok_estimates[(case, stage)]),
             "missing_byok_upstream_costs": byok_missing[(case, stage)],
             "unknown_route_generations": route_unknown[(case, stage)],
             "priced_generations": counts[(case, stage)],
             "missing_generations": missing[(case, stage)],
             "saved_generation_coverage_complete": missing[(case, stage)] == 0}
            for case, stage in sorted(stages)]
    stage_totals = {stage: str(sum((value for (case, name), value in totals.items() if name == stage), Decimal(0)))
                    for stage in sorted({stage for _, stage in stages})}
    case_totals = defaultdict(Decimal)
    for case, stage in stages:
        case_totals[case] += totals[(case, stage)]
    captured_stats = None
    if not any(missing.values()) and case_totals:
        captured_stats = decimal_stats(case_totals.values())
    known_route_costs = not any(missing.values()) and not any(byok_missing.values()) and not any(route_unknown.values())
    selected_estimate = sum(totals.values(), Decimal(0)) + sum(byok_estimates.values(), Decimal(0))
    estimated_questions = []
    for case in sorted(case_totals):
        keys = [key for key in stages if key[0] == case]
        known = all(not missing[key] and not byok_missing[key] and not route_unknown[key] for key in keys)
        value = sum((totals[key] + byok_estimates[key] for key in keys), Decimal(0))
        estimated_questions.append({"case_id": case, "estimated_cost_usd": str(value) if known else None})
    return {"per_case": rows, "recorded_cost_usd": str(sum(totals.values(), Decimal(0))),
            "recorded_cost_basis": "GET-generation-reconciled OpenRouter account charges ONLY; BYOK vendor usage excluded",
            "requested_resolved_models": [{"requested": a, "resolved": b, "exact_alias_mapping_used": a != b}
                                          for a, b in sorted(resolved_models)],
            "byok_generations": sum(byok_counts.values()),
            "byok_upstream_estimate_usd": str(sum(byok_estimates.values(), Decimal(0))),
            "missing_byok_upstream_costs": sum(byok_missing.values()),
            "unknown_route_generations": sum(route_unknown.values()),
            "selected_generation_estimated_cost_usd": str(selected_estimate) if known_route_costs else None,
            "selected_generation_estimated_mean_per_question_usd": str(selected_estimate / len(case_totals)) if known_route_costs and case_totals else None,
            "selected_generation_estimated_cost_per_question_usd": estimated_questions,
            "selected_generation_estimated_cost_per_question_stats_usd": decimal_stats(
                [money(row["estimated_cost_usd"]) for row in estimated_questions]) if known_route_costs else None,
            "selected_generation_estimate_scope": "OpenRouter charges plus provider-reported BYOK upstream estimates, not vendor invoice or full lifecycle. Null if any captured generation/route/upstream estimate is unknown.",
            "provisional_observed_cost_usd": str(sum(provisional.values(), Decimal(0))),
            "provisional_cost_note": "Observed stream/response snapshots without finality proof. Not additive to reconciled charges; may differ from final charges.",
            "recorded_cost_by_stage_usd": stage_totals,
            "question_denominator": len(case_totals),
            "captured_generation_cost_per_question_usd": [{"case_id": case, "recorded_cost_usd": str(value)}
                                                          for case, value in sorted(case_totals.items())],
            "captured_generation_cost_per_question_stats_usd": captured_stats,
            "stats_scope": "Captured OpenRouter account charges only; excludes BYOK vendor usage, uncaptured attempts and lifecycle costs. Stats omitted when any captured generation is unpriced.",
            "priced_generations": sum(counts.values()), "missing_generations": sum(missing.values()),
            "saved_generation_coverage_complete": not any(missing.values()),
            "all_attempts_coverage_proven": False,
            "historical_preparation_cost_usd": None,
            "recorded_judge_cost_subtotal_usd": stage_totals.get("judge"), "full_judge_cost_usd": None,
            "embedding_cost_usd": None, "full_lifecycle_cost_usd": None,
            "full_lifecycle_cost_per_question_usd": None,
            "limitation": "Captured generations only. Missing attempts, uncaptured judge calls, embeddings and original preparation are not zero. No full-lifecycle total is established."}


def write_new(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")


def classify_attempt(summary, classification):
    if classification not in ("unclassified", "invalid_attempt"):
        raise ValueError("unsupported cost attempt classification")
    result = dict(summary, attempt_classification=classification)
    if classification == "invalid_attempt":
        result.update(include_in_valid_run_metrics=False, include_in_campaign_spend=True,
                      captured_generation_cost_per_question_stats_usd=None,
                      selected_generation_estimated_cost_per_question_stats_usd=None,
                      selected_generation_estimated_mean_per_question_usd=None,
                      stats_scope="INVALID attempt: attributed spend only; excluded from valid-run cost/performance means. No accuracy result.")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--receipts", type=Path, help="Existing sanitized receipt JSON; offline by default")
    parser.add_argument("--journal", type=Path, help="Passive backend reader/judge usage journal")
    parser.add_argument("--fetch", action="store_true", help="Authorized read-only provider metadata lookup")
    parser.add_argument("--attempt-classification", choices=("unclassified", "invalid_attempt"), default="unclassified")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; preserve existing evidence")
    report = json.loads(args.report.read_text())
    requests = report_requests(report)
    receipts = json.loads(args.receipts.read_text())["receipts"] if args.receipts else []
    if args.journal:
        journal_requests, journal_receipts = journal_evidence(args.journal)
        requests = combine_requests(requests, journal_requests)
        receipts = merge_receipts(receipts, journal_receipts)
    if args.fetch:
        key = os.environ.get("LOCAL_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
        if not key:
            parser.error("key unavailable in process environment")
        receipts = reconcile_receipts(requests, receipts, key)
    result = {"schema": "openrouter-saved-reader-cost-audit-v1",
              "captured_at": datetime.now(timezone.utc).isoformat(),
              "report_sha256": digest(args.report), "report_run_id": report.get("run_id"),
              "helper_sha256": digest(__file__), "receipts": receipts,
              "summary": classify_attempt(summarize(requests, receipts), args.attempt_classification)}
    if args.journal:
        result["journal_sha256"] = digest(args.journal)
    write_new(args.output, result)
    print(json.dumps({"priced_generations": result["summary"]["priced_generations"],
                      "missing_generations": result["summary"]["missing_generations"],
                      "full_lifecycle_cost_usd": None}))


if __name__ == "__main__":
    main()
