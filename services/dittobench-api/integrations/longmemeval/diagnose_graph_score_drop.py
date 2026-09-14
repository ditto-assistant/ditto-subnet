#!/usr/bin/env python3
"""Offline descriptive diagnosis of two pinned LME reports; never regrades.

Optional fixture probes use a read-only transaction on a loopback PostgreSQL.
Output contains aggregate metrics and public case IDs, never raw answers/keys.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics
import subprocess
from urllib.parse import urlsplit

OLD_SHA = "859a118af975b219de3b73809efd648832291dadae590fbaf129e0f172028c18"
NEW_SHA = "d55d4d81af4c85ae9f41eb97f0d6446ae887ea281d8a411166f6caf51b59c541"
PROBES = {"3c1045c8": "29.5", "gpt4_372c3eed": "Associate", "gpt4_cd90e484": "three weeks"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def load_report(path, expected_sha):
    raw = Path(path).read_bytes()
    require(hashlib.sha256(raw).hexdigest() == expected_sha, "report hash mismatch")
    report = json.loads(raw)
    require(report["meta"]["lme_complete"] == "true", "report incomplete")
    return report


def index(report):
    rows = report["per_case"]
    indexed = {row["case_id"]: row for row in rows}
    require(len(rows) == len(indexed) == 500, "expected 500 unique cases")
    for row in rows:
        require(type(row["lme_correct"]) is bool, "verdict must be boolean")
        require(row["data"].get("query_status") == row["data"].get("judge_status") == "ok", "non-successful case")
    return indexed


def tools(row):
    return row["data"].get("tools_called") or []


def recall(row):
    # The top-level omitempty field disappears at zero; data retains zero.
    value = row["data"]["session_recall"]
    require(type(value) in (int, float) and 0 <= value <= 1, "invalid session recall")
    require(row.get("lme_session_recall", value) == value, "inconsistent session recall")
    return value


def group_metrics(old, new, ids):
    regress = [i for i in ids if old[i]["lme_correct"] and not new[i]["lme_correct"]]
    improve = [i for i in ids if not old[i]["lme_correct"] and new[i]["lme_correct"]]
    return {"cases": len(ids), "old_correct": sum(old[i]["lme_correct"] for i in ids),
            "new_correct": sum(new[i]["lme_correct"] for i in ids),
            "regressions": len(regress), "improvements": len(improve),
            "regression_case_ids": sorted(regress), "improvement_case_ids": sorted(improve)}


def analyze(old_report, new_report):
    old, new = index(old_report), index(new_report)
    require(old.keys() == new.keys(), "case coverage mismatch")
    for i in old:
        for key in ("query", "gold_answer", "category", "model"):
            require(old[i][key] == new[i][key], "case identity/condition mismatch")
    ids = sorted(old)
    result = group_metrics(old, new, ids)
    regress = result["regression_case_ids"]
    result["by_category"] = {c: group_metrics(old, new, [i for i in ids if old[i]["category"] == c])
                             for c in sorted({row["category"] for row in old.values()})}
    same = [i for i in ids if set(old[i]["data"]["seed_pair_ids"]) == set(new[i]["data"]["seed_pair_ids"])]
    result["same_seed_set"] = group_metrics(old, new, same)
    result["different_seed_set"] = group_metrics(old, new, sorted(set(ids) - set(same)))
    result["regression_diagnostics"] = {
        "new_full_session_recall": sum(recall(new[i]) == 1 for i in regress),
        "old_no_tool_calls": sum(not tools(old[i]) for i in regress),
        "new_no_tool_calls": sum(not tools(new[i]) for i in regress),
        "identical_answer_verdict_flips": sum(old[i]["hypothesis"] == new[i]["hypothesis"] for i in regress),
        "same_ordered_seed_ids": sum(old[i]["data"]["seed_pair_ids"] == new[i]["data"]["seed_pair_ids"] for i in regress)}
    result["runs"] = {}
    for name, report, rows in (("old", old_report, old), ("new", new_report, new)):
        result["runs"][name] = {key: report[key] for key in ("run_id", "git_sha", "prompt_sha", "tools_sha", "weights_sha", "judge_model")}
        result["runs"][name].update(
            mean_session_recall=statistics.mean(recall(row) for row in rows.values()),
            mean_prompt_tokens=statistics.mean(row["data"]["prompt_tokens"] for row in rows.values()),
            final_answer_source_cases=sum(row["data"]["answer_source"] == "final" for row in rows.values()),
            graph_seed_cases=sum(bool(row["data"]["graph_seed_pair_ids"]) for row in rows.values()))
    result["scope"] = "Descriptive paired records, not a controlled graph treatment effect. Session recall is not fact visibility. No regrading or new inference."
    return result


def fixture_probe(uri, psql, old_report, new_report):
    target = urlsplit(uri)
    require(target.scheme in ("postgres", "postgresql") and target.hostname in ("127.0.0.1", "localhost"), "fixture must be loopback PostgreSQL")
    require(not target.query and not target.fragment, "connection options are not accepted")
    old, new = index(old_report), index(new_report)
    result = {}
    for cid, fact in PROBES.items():
        ids = sorted(set(old[cid]["data"]["seed_pair_ids"]) | set(new[cid]["data"]["seed_pair_ids"]))
        require(all(re.fullmatch(r"lme_mem_[a-f0-9]{64}", pid) for pid in ids), "unexpected pair ID")
        literals = ",".join("'" + pid + "'" for pid in ids)
        sql = ("BEGIN READ ONLY; SET LOCAL statement_timeout='5s'; "
               "SELECT json_build_object('id',firestore_pair_id,'prompt',prompt,'summary',description) "
               "FROM memory_pairs WHERE user_id='lme_s_" + cid + "' AND firestore_pair_id IN (" + literals + ") ORDER BY firestore_pair_id; ROLLBACK;")
        raw = subprocess.check_output([psql, uri, "-X", "-v", "ON_ERROR_STOP=1", "-At", "-c", sql], text=True)
        memories = [json.loads(line) for line in raw.splitlines() if line.startswith("{")]
        require({m["id"] for m in memories} == set(ids), "fixture is missing selected memories")
        matches = [m for m in memories if fact.lower() in (m["prompt"] or "").lower()]
        require(matches, "expected example fact absent")
        result[cid] = {"fact_marker": fact, "selected_memory_rows": len(memories),
                       "selected_rows_digest": hashlib.sha256(json.dumps(memories, sort_keys=True).encode()).hexdigest(),
                       "matching_fact_rows": [{"in_old_seed": m["id"] in old[cid]["data"]["seed_pair_ids"],
                                               "in_new_seed": m["id"] in new[cid]["data"]["seed_pair_ids"],
                                               "fact_in_summary": fact.lower() in (m["summary"] or "").lower()} for m in matches],
                       "old_tools": tools(old[cid]), "new_tools": tools(new[cid])}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", required=True, type=Path)
    parser.add_argument("--new", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--fixture-db")
    parser.add_argument("--psql", default="psql")
    args = parser.parse_args()
    require(not args.output.exists(), "output already exists")
    old, new = load_report(args.old, OLD_SHA), load_report(args.new, NEW_SHA)
    result = analyze(old, new)
    result["report_sha256"] = {"old": OLD_SHA, "new": NEW_SHA}
    result["helper_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if args.fixture_db:
        result["readonly_fixture_examples"] = fixture_probe(args.fixture_db, args.psql, old, new)
    # Never overwrite a previous result or either input.
    with args.output.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps({key: result[key] for key in ("cases", "old_correct", "new_correct", "regressions", "improvements")}))


if __name__ == "__main__":
    main()
