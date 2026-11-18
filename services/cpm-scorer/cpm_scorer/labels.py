"""CPM labels from a public `cmd/generate` artifact.

This is a copy of the label logic in `docs/research/cpm/export_cpm.py`, so
local rehearsal (the exporter) and the scorer read identical labels (contract
§4.2). The copy is pure: `export_records` returns the records the exporter
writes to pairs.jsonl, catalog.jsonl and cases.jsonl instead of writing them.

`tests/test_labels.py` runs both on the same artifact and asserts identical
output. Change the two together.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

DEFAULT_USER = "miner"  # protocol.go: UserID defaults to "miner" if empty
ABSTAIN_KINDS = {"absence", "chitchat"}
FIELDS = ("prompt", "response")


def norm_strings(values: Any) -> list[str]:
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
    """Strings whose presence in a candidate marks an answer span."""
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


def export_records(art: dict, seed_key: str) -> dict[str, Any]:
    """Return {"pairs", "catalog", "cases", "stats"} exactly as the exporter
    would write them for one artifact (pairs.jsonl, catalog.jsonl,
    cases.jsonl rows, in order)."""
    stats: Counter = Counter()
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
    stats["pairs"] += len(pairs)

    catalog = art.get("catalog") or []
    catalog_rows = [{"seed_key": seed_key, **tool} for tool in catalog]
    stats["catalog_tools"] += len(catalog)

    cases: list[dict] = []
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
        cases.append(
            {
                "seed_key": seed_key,
                "kind": "memory",
                "case_id": case["id"],
                "family": case.get("question_type"),
                "query": case.get("question"),
                "user_id": user,
                "run_after_wave": after,
                "candidate_pair_ids": candidates,
                "relevant_pair_ids": evidence,
                "decision": {
                    "answer_kind": case.get("answer_kind"),
                    "abstain": abstain,
                },
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
        )
        stats["memory_cases"] += 1

    needles = {f["case_id"]: f.get("needle") for f in art.get("tool_fixtures") or []}
    for tc in art.get("tool_cases") or []:
        expected = [
            t.get("name") for t in tc.get("expected_tools") or [] if t.get("name")
        ]
        cases.append(
            {
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
        )
        stats["tool_cases"] += 1

    return {
        "pairs": list(pairs.values()),
        "catalog": catalog_rows,
        "cases": cases,
        "stats": dict(stats),
    }
