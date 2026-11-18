#!/usr/bin/env python3
"""Export DittoBench datasets as context-packet model (CPM) training data.

Input: one or more `cmd/generate` JSON artifacts, for example

    go run ./cmd/generate -bench-version 18 -seed 42 -run-size full \
        -out /tmp/cpm/v18-42.json

Output, written to --out-dir:
- pairs.jsonl: every seeded memory pair, once per (seed, pair_id);
- cases.jsonl: one record per memory or tool case, with its gold labels;
- catalog.jsonl: each seed's pinned tool catalog;
- manifest.json: inputs, their SHA-256, counts and label-coverage stats.

Every label comes from the public artifact; no grader-only field is used. The
labels are:
- memory cases: relevant pair ids (`v10_evidence_pair_ids`), answer spans
  (expected answer and accepted variants located in candidate text),
  distractor and forbidden spans, and the abstain decision;
- tool cases: the expected tool names (the shortlist target), prerequisite
  pairs and the fixture needle.

See docs/research/sn118-model-competition.md §I.2 and §I.4 (source 1).
Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from collections import Counter

DEFAULT_USER = "miner"  # protocol.go: UserID defaults to "miner" if empty
ABSTAIN_KINDS = {"absence", "chitchat"}
FIELDS = ("prompt", "response")


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_strings(values) -> list[str]:
    """Flatten str / list / nested list answer fields into distinct strings."""
    out: list[str] = []
    stack = [values]
    while stack:
        v = stack.pop()
        if v is None:
            continue
        if isinstance(v, str):
            s = v.strip()
            if s and s not in out:
                out.append(s)
        elif isinstance(v, (list, tuple)):
            stack.extend(reversed(v))
    return out


def answer_strings(case: dict) -> list[str]:
    """Strings whose presence in a candidate marks an answer span.

    List answers ("A; B") are split into items so each item can be located on
    its own; the full string is kept too.
    """
    vals = norm_strings(
        [
            case.get("expected_answer"),
            case.get("accept_any"),
            case.get("answer_items"),
            case.get("answer_item_accept_any"),
        ]
    )
    expanded: list[str] = []
    for s in vals:
        if s not in expanded:
            expanded.append(s)
        if ";" in s:
            for part in s.split(";"):
                p = part.strip()
                if p and p not in expanded:
                    expanded.append(p)
    return expanded


def find_spans(text: str, needles: list[str], label: str) -> list[dict]:
    """Case-insensitive, word-bounded, non-overlapping matches (longest first)."""
    spans: list[dict] = []
    taken: list[tuple[int, int]] = []
    for needle in sorted(needles, key=len, reverse=True):
        if len(needle) < 2:
            continue
        pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            a, b = m.span()
            if any(a < tb and ta < b for ta, tb in taken):
                continue
            taken.append((a, b))
            spans.append({"start": a, "end": b, "type": label, "text": text[a:b]})
    return spans


def pair_spans(pair: dict, groups: list[tuple[list[str], str]]) -> list[dict]:
    out: list[dict] = []
    for field in FIELDS:
        text = pair.get(field) or ""
        for needles, label in groups:
            for s in find_spans(text, needles, label):
                s["field"] = field
                out.append(s)
    return out


def export_artifact(path: pathlib.Path, files: dict, stats: Counter) -> dict:
    art = json.loads(path.read_text())
    digest = sha256_file(path)
    seed_key = path.stem  # e.g. v18-42

    # Haystack: (user, wave) -> pairs. Absent user means the default user and
    # absent wave means 0 (SeedRequest is additive-optional).
    pairs: dict[str, dict] = {}
    by_user_wave: dict[tuple[str, int], list[str]] = {}
    for wave in art.get("memory_waves") or []:
        user = wave.get("user_id") or DEFAULT_USER
        w = wave.get("wave") or 0
        for p in wave.get("pairs") or []:
            rec = {
                "seed_key": seed_key,
                "pair_id": p["pair_id"],
                "user_id": user,
                "wave": w,
                "session_id": p.get("session_id"),
                "timestamp": p.get("timestamp"),
                "prompt": p.get("prompt") or "",
                "response": p.get("response") or "",
                "source": "memory_wave",
            }
            pairs[p["pair_id"]] = rec
            by_user_wave.setdefault((user, w), []).append(p["pair_id"])
    for tc in art.get("tool_cases") or []:
        for p in tc.get("prerequisite_pairs") or []:
            pairs.setdefault(
                p["pair_id"],
                {
                    "seed_key": seed_key,
                    "pair_id": p["pair_id"],
                    "user_id": DEFAULT_USER,
                    "wave": 0,
                    "session_id": p.get("session_id"),
                    "timestamp": p.get("timestamp"),
                    "prompt": p.get("prompt") or "",
                    "response": p.get("response") or "",
                    "source": f"tool_prereq:{tc['id']}",
                },
            )
    for rec in pairs.values():
        files["pairs"].write(json.dumps(rec, ensure_ascii=False) + "\n")
    stats["pairs"] += len(pairs)

    catalog = art.get("catalog") or []
    for tool in catalog:
        files["catalog"].write(
            json.dumps({"seed_key": seed_key, **tool}, ensure_ascii=False) + "\n"
        )
    stats["catalog_tools"] += len(catalog)

    for case in art.get("memory_cases") or []:
        user = case.get("user_id") or DEFAULT_USER
        after = case.get("run_after_wave") or 0
        candidates = [
            pid
            for (u, w), ids in sorted(by_user_wave.items(), key=lambda kv: kv[0][1])
            if u == user and w <= after
            for pid in ids
        ]
        evidence = case.get("v10_evidence_pair_ids") or []
        abstain = (case.get("answer_kind") or "") in ABSTAIN_KINDS
        answers = [] if abstain else answer_strings(case)
        distractors = norm_strings(case.get("distractor_answers"))
        forbidden = norm_strings(case.get("forbidden_answer"))
        groups = [
            (answers, "answer_value"),
            (distractors, "distractor"),
            (forbidden, "forbidden"),
        ]
        spans = {}
        for pid in candidates:
            s = pair_spans(pairs[pid], groups)
            if s:
                spans[pid] = s
        answer_in_evidence = any(
            sp["type"] == "answer_value"
            for pid in evidence
            for sp in spans.get(pid, [])
        )
        missing_evidence = [pid for pid in evidence if pid not in pairs]
        rec = {
            "seed_key": seed_key,
            "kind": "memory",
            "case_id": case["id"],
            "family": case.get("question_type"),
            "query": case.get("question"),
            "user_id": user,
            "run_after_wave": after,
            "candidate_pair_ids": candidates,
            "relevant_pair_ids": evidence,
            "decision": {"answer_kind": case.get("answer_kind"), "abstain": abstain},
            "expected_answer": case.get("expected_answer"),
            "accept_any": case.get("accept_any"),
            "answer_items": case.get("answer_items"),
            "distractor_answers": distractors,
            "forbidden_answer": case.get("forbidden_answer"),
            "spans": spans,
            "label_quality": {
                "answer_span_in_evidence": answer_in_evidence,
                "missing_evidence_ids": missing_evidence,
            },
        }
        files["cases"].write(json.dumps(rec, ensure_ascii=False) + "\n")
        stats["memory_cases"] += 1
        stats[f"memory_abstain={abstain}"] += 1
        if not abstain and evidence:
            stats["answerable_with_evidence"] += 1
            stats["answer_span_found_in_evidence"] += int(answer_in_evidence)
        if missing_evidence:
            stats["cases_with_missing_evidence_ids"] += 1
        stats["candidate_pairs_total"] += len(candidates)

    needles = {f["case_id"]: f.get("needle") for f in art.get("tool_fixtures") or []}
    for tc in art.get("tool_cases") or []:
        expected = [
            t.get("name") for t in tc.get("expected_tools") or [] if t.get("name")
        ]
        rec = {
            "seed_key": seed_key,
            "kind": "tool",
            "case_id": tc["id"],
            "family": tc.get("category"),
            "query": tc.get("prompt"),
            "expected_tools": tc.get("expected_tools") or [],
            "shortlist_target": expected,
            "max_tool_calls": tc.get("max_tool_calls"),
            "expected_behavior": tc.get("expected_behavior"),
            "prerequisite_pair_ids": [
                p["pair_id"] for p in tc.get("prerequisite_pairs") or []
            ],
            "needle": needles.get(tc["id"]),
            "decision": {"abstain": not expected},
        }
        files["cases"].write(json.dumps(rec, ensure_ascii=False) + "\n")
        stats["tool_cases"] += 1
        stats[f"tool_expects_tools={bool(expected)}"] += 1
        names = {t.get("name") for t in catalog}
        stats["tool_targets_in_catalog"] += sum(1 for n in expected if n in names)
        stats["tool_targets_total"] += len(expected)

    return {"path": str(path), "seed_key": seed_key, "sha256": digest}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("artifacts", nargs="+", type=pathlib.Path)
    ap.add_argument("--out-dir", type=pathlib.Path, required=True)
    args = ap.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stats: Counter = Counter()
    inputs = []
    with (
        (args.out_dir / "pairs.jsonl").open("w") as fp,
        (args.out_dir / "cases.jsonl").open("w") as fc,
        (args.out_dir / "catalog.jsonl").open("w") as fk,
    ):
        files = {"pairs": fp, "cases": fc, "catalog": fk}
        for path in args.artifacts:
            inputs.append(export_artifact(path, files, stats))

    answerable = stats["answerable_with_evidence"]
    manifest = {
        "inputs": inputs,
        "counts": dict(sorted(stats.items())),
        "answer_span_coverage": (
            round(stats["answer_span_found_in_evidence"] / answerable, 4)
            if answerable
            else None
        ),
        "tool_target_catalog_coverage": (
            round(stats["tool_targets_in_catalog"] / stats["tool_targets_total"], 4)
            if stats["tool_targets_total"]
            else None
        ),
    }
    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    json.dump(manifest, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
