#!/usr/bin/env python3
"""Build the CPM v2 span-extraction PoC dataset from public generator seeds.

Each example is one memory case rendered as RAW text: the answer-bearing
evidence pair, the case's other evidence pairs, hard distractors (pairs with a
`distractor` or `forbidden` export span) and random candidates, shuffled and
joined as plain text with no ids or chunk markers. The gold span is the
generator's own `answer_value` span (export labels, contract §4.2), moved to
character offsets inside the document.

Inclusion (all deterministic):
- memory case, not abstain;
- exactly one `answer_value` span across the case's evidence pairs;
- the answer pair fits the document budget;
- the answer string occurs exactly once in the document (case-insensitive,
  word-bounded, as `labels.find_spans` matches). Non-evidence pairs that also
  contain it are left out, so the gold offsets are unambiguous.

Everything is a pure function of (bench_version, seed, case_id): pair order and
sampling use `random.Random` seeded from sha256 of the seed key and case id.

Run in the scorer environment:

    uv run --project services/cpm-scorer python \\
        docs/research/cpm/span_v2/span_data.py --seeds 101,102 --out train.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from pathlib import Path

from cpm_scorer import model as m
from cpm_scorer.bases import get_base
from cpm_scorer.generate import (
    build_generate,
    default_cache_dir,
    find_datagen_dir,
    generate,
)
from cpm_scorer.labels import export_records
from cpm_scorer.metrics import distractor_ids
from cpm_scorer.tasks import memory_text

DATA_VERSION = 1
DOC_TOKEN_BUDGET = 1536  # document tokens; query + template stay under 2048
MAX_PAIR_TOKENS = 384  # skip one very long non-answer pair rather than cut it
MAX_HARD = 4
SEPARATOR = "\n\n"


def answer_offset(pair: dict, span: dict) -> int:
    """Offset of an export span (per field) inside `tasks.memory_text`."""
    ts = pair.get("timestamp")
    prefix = (f"[{ts}] " if ts else "") + "User: "
    if span["field"] == "prompt":
        return len(prefix) + span["start"]
    return (
        len(prefix)
        + len(pair.get("prompt") or "")
        + len("\nAssistant: ")
        + span["start"]
    )


def occurrences(text: str, needle: str) -> list[int]:
    pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
    return [mm.start() for mm in re.finditer(pattern, text, flags=re.IGNORECASE)]


def case_rng(seed_key: str, case_id: str, variant: int) -> random.Random:
    h = hashlib.sha256(f"cpm-span-v2:{seed_key}:{case_id}:{variant}".encode())
    return random.Random(int.from_bytes(h.digest()[:8], "big"))


def build_examples(records: dict, seed_key: str, tok, variants: int = 1) -> list:
    pairs = {p["pair_id"]: p for p in records["pairs"]}
    text = {pid: memory_text(p) for pid, p in pairs.items()}
    ntok = {}

    def tokens(pid: str) -> int:
        if pid not in ntok:
            ntok[pid] = len(tok(text[pid], add_special_tokens=False)["input_ids"])
        return ntok[pid]

    sep_tokens = len(tok(SEPARATOR, add_special_tokens=False)["input_ids"])
    out = []
    cases = sorted(
        (c for c in records["cases"] if c["kind"] == "memory"),
        key=lambda c: c["case_id"],
    )
    for case in cases:
        if case["decision"]["abstain"]:
            continue
        evidence = [pid for pid in case["relevant_pair_ids"] if pid in pairs]
        answers = [
            (pid, s)
            for pid in evidence
            for s in case["spans"].get(pid, [])
            if s["type"] == "answer_value"
        ]
        if len(answers) != 1:
            continue
        apid, aspan = answers[0]
        aoff = answer_offset(pairs[apid], aspan)
        atext = text[apid][aoff : aoff + len(aspan["text"])]
        if atext != aspan["text"]:
            raise RuntimeError(f"offset mismatch in {case['case_id']}")
        if tokens(apid) > DOC_TOKEN_BUDGET:
            continue
        others = sorted(set(case["candidate_pair_ids"]) - {apid})
        # Pairs that repeat the answer string would make the gold ambiguous.
        others = [pid for pid in others if not occurrences(text[pid], atext)]
        rel = set(evidence)
        hard_ids = distractor_ids(case["spans"]) - rel
        for v in range(variants):
            rng = case_rng(seed_key, case["case_id"], v)
            support = [pid for pid in others if pid in rel]
            hard = [pid for pid in others if pid in hard_ids]
            rand = [pid for pid in others if pid not in rel and pid not in hard_ids]
            rng.shuffle(support)
            rng.shuffle(hard)
            rng.shuffle(rand)
            order = [*support, *hard[:MAX_HARD], *rand]
            chosen, used = [apid], tokens(apid)
            for pid in order:
                n = tokens(pid)
                if n > MAX_PAIR_TOKENS:
                    continue
                if used + sep_tokens + n > DOC_TOKEN_BUDGET:
                    continue
                chosen.append(pid)
                used += sep_tokens + n
            rng.shuffle(chosen)
            doc, bounds, cursor = [], [], 0
            for pid in chosen:
                if doc:
                    doc.append(SEPARATOR)
                    cursor += len(SEPARATOR)
                doc.append(text[pid])
                bounds.append(
                    {
                        "start": cursor,
                        "end": cursor + len(text[pid]),
                        "relevant": pid in rel,
                        "answer": pid == apid,
                        "distractor": pid in hard_ids,
                    }
                )
                cursor += len(text[pid])
            doc_text = "".join(doc)
            abound = next(b for b in bounds if b["answer"])
            cstart = abound["start"] + aoff
            hits = occurrences(doc_text, atext)
            if hits != [cstart]:
                raise RuntimeError(f"answer not unique in {case['case_id']}: {hits}")
            out.append(
                {
                    "data_version": DATA_VERSION,
                    "seed_key": seed_key,
                    "case_id": case["case_id"],
                    "variant": v,
                    "family": case.get("family"),
                    "query": case["query"] or "",
                    "doc": doc_text,
                    "answer_text": atext,
                    "char_start": cstart,
                    "char_end": cstart + len(atext),
                    "pairs": bounds,
                }
            )
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--bench-version", type=int, default=18)
    ap.add_argument("--base", default="qwen3.5-0.8b")
    ap.add_argument("--variants", type=int, default=1, help="documents per case")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    tok = m.load_tokenizer(m.download_base(get_base(args.base)))
    cache = default_cache_dir()
    gen = build_generate(find_datagen_dir(), cache)
    rows = []
    for seed in [int(s) for s in args.seeds.split(",") if s.strip()]:
        art = generate(gen, args.bench_version, seed, cache)
        key = f"v{args.bench_version}-{seed}"
        recs = export_records(json.loads(art.read_text()), key)
        ex = build_examples(recs, key, tok, args.variants)
        print(f"{key}: {len(ex)} examples", file=sys.stderr)
        rows.extend(ex)
    raw = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    print(f"wrote {len(rows)} examples to {args.out} sha256 {digest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
