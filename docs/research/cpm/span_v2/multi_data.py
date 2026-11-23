#!/usr/bin/env python3
"""Build the CPM v2 multi-span PoC dataset from public generator seeds.

Each example is a query and a RAW document (memory pairs as plain text, no ids
or chunk markers) with a SET of gold spans, which can be empty. Three kinds:

- `answer`: a non-abstain memory case. Gold is every `answer_value` export
  span (contract §4.2) inside the case's evidence pairs, so list and ordered
  answers give several spans. Cases whose answer never appears verbatim in
  evidence (computed numbers, sums) have no extractable gold and are skipped.
- `absence`: an abstain case (absence / chitchat). Gold is every occurrence of
  the generator's `grounding_tokens` inside evidence pairs: the facts a
  grounded decline should cite. These are relevant spans, not "no answer".
- `null`: "nothing relevant in the corpus". The case's query over a document
  from the same seed with the case's evidence pairs removed, plus every pair
  that holds an answer, distractor, forbidden or grounding string, or a rare
  query term (document frequency under 2% of the seed's pairs). Gold is empty.

Gold comes at five widths (`gold.<level>`, character offsets into `doc`):

- `needle`: the strings above;
- `anchored`: each needle plus the `[timestamp]` of the memory that holds it,
  as a separate span (the "as of" date);
- `clause`: the sentence (break at `.!?` + space, or a newline) of the memory
  that holds each needle, merged when two needles share one;
- `history`: `clause`, plus the sentence of every distractor or forbidden
  string (an older, superseded or other value) inside an evidence pair, so the
  packet carries the timeline the reader has to resolve;
- `evidence`: `history`, plus, for every other evidence pair in the document,
  its whole user statement when it is at most MAX_CLAUSE_TOKENS tokens. These
  memories hold no labelled string but the generator marks them as needed:
  who a person is to the user, where they work now, a glossary line, a
  "that entry was struck" correction.

The first three widths are byte-identical to DATA_VERSION 2, and `history` to
DATA_VERSION 3.

Documents keep every gold-bearing evidence pair, then the case's other evidence
pairs, up to 4 hard distractors and random candidates, up to DOC_TOKEN_BUDGET.
Non-evidence pairs that also hold a gold string are left out, so the gold is
unambiguous. Docs with more than MAX_GOLD needle spans are skipped.

Everything is a pure function of (bench_version, seed, case_id, variant).

    uv run --project services/cpm-scorer python \\
        docs/research/cpm/span_v2/multi_data.py --seeds 101,102 --out train.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from cpm_scorer import model as m  # noqa: E402
from cpm_scorer.bases import get_base  # noqa: E402
from cpm_scorer.generate import (  # noqa: E402
    build_generate,
    default_cache_dir,
    find_datagen_dir,
    generate,
)
from cpm_scorer.labels import export_records, find_spans, norm_strings  # noqa: E402
from cpm_scorer.metrics import distractor_ids  # noqa: E402
from cpm_scorer.tasks import memory_text  # noqa: E402
from span_data import (  # noqa: E402
    DOC_TOKEN_BUDGET,
    MAX_HARD,
    MAX_PAIR_TOKENS,
    SEPARATOR,
    answer_offset,
    case_rng,
)

DATA_VERSION = 4
MAX_GOLD = 12  # needle spans per document; the fixed output buffer is K = 12
MAX_CLAUSE_TOKENS = 96
RARE_DF = 0.02
LEVELS = ("needle", "anchored", "clause", "history", "evidence")
HISTORY_TYPES = ("distractor", "forbidden")
WORD = re.compile(r"[a-z0-9][a-z0-9@._'-]*[a-z0-9]")


def words(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


def clause_bounds(text: str, a: int, b: int) -> tuple[int, int]:
    """Sentence of `text` around [a, b): breaks at `.!?` + whitespace or `\\n`."""
    start = 0
    for mm in re.finditer(r"[.!?](?=\s)|\n", text[:a]):
        start = mm.end()
    while start < a and text[start].isspace():
        start += 1
    mm = re.search(r"[.!?](?=\s|$)|\n", text[b:])
    end = b + mm.end() if mm else len(text)
    if text[end - 1] == "\n":
        end -= 1
    return start, end


def merge(spans: list[tuple[int, int]]) -> list[list[int]]:
    out: list[list[int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def pair_gold(pair: dict, spans: list[dict]) -> list[dict]:
    """Gold spans inside one pair's memory_text (needle, anchor, clause)."""
    out = []
    ts = pair.get("timestamp")
    for s in spans:
        off = answer_offset(pair, s)
        field = pair.get(s["field"]) or ""
        ca, cb = clause_bounds(field, s["start"], s["end"])
        out.append(
            {
                "needle": (off, off + s["end"] - s["start"]),
                "anchor": (1, 1 + len(ts)) if ts else None,
                "clause": (off - s["start"] + ca, off - s["start"] + cb),
                "text": s["text"],
            }
        )
    return out


def build_examples(
    records: dict,
    raw_cases: dict,
    seed_key: str,
    tok,
    variants: int,
    null_rate: float,
    stats: Counter,
) -> list:
    pairs = {p["pair_id"]: p for p in records["pairs"]}
    text = {pid: memory_text(p) for pid, p in pairs.items()}
    pair_words = {pid: words(t) for pid, t in text.items()}
    df: Counter = Counter(w for ws in pair_words.values() for w in ws)
    ntok: dict[str, int] = {}

    def tokens(s: str) -> int:
        return len(tok(s, add_special_tokens=False)["input_ids"])

    def pair_tokens(pid: str) -> int:
        if pid not in ntok:
            ntok[pid] = tokens(text[pid])
        return ntok[pid]

    sep_tokens = tokens(SEPARATOR)
    out = []
    cases = sorted(
        (c for c in records["cases"] if c["kind"] == "memory"),
        key=lambda c: c["case_id"],
    )
    for case in cases:
        cid = case["case_id"]
        abstain = case["decision"]["abstain"]
        evidence = [pid for pid in case["relevant_pair_ids"] if pid in pairs]
        rel = set(evidence)
        hard_ids = distractor_ids(case["spans"]) - rel
        if abstain:
            ground = norm_strings(raw_cases[cid].get("grounding_tokens"))
            gold_spans = {
                pid: [
                    {**s, "field": f}
                    for f in ("prompt", "response")
                    for s in find_spans(pairs[pid][f] or "", ground, "grounding")
                ]
                for pid in pairs
            }
            gold_spans = {pid: v for pid, v in gold_spans.items() if v}
        else:
            ground = []
            gold_spans = {
                pid: [s for s in sp if s["type"] == "answer_value"]
                for pid, sp in case["spans"].items()
            }
            gold_spans = {pid: v for pid, v in gold_spans.items() if v}
        must = sorted(pid for pid in evidence if pid in gold_spans)
        # Non-evidence pairs holding a gold string would make the gold ambiguous.
        clean = [
            pid
            for pid in sorted(set(case["candidate_pair_ids"]) - set(must))
            if pid not in gold_spans
        ]
        kind = "absence" if abstain else "answer"
        hist_spans = {
            pid: [s for s in case["spans"].get(pid, []) if s["type"] in HISTORY_TYPES]
            for pid in evidence
        }

        # Null pool: nothing relevant to this case at all.
        q_rare = {
            w
            for w in words(case["query"] or "")
            if len(w) >= 4 and df[w] < RARE_DF * len(pairs)
        }
        null_pool = [
            pid
            for pid in sorted(case["candidate_pair_ids"])
            if pid not in rel
            and pid not in case["spans"]
            and pid not in gold_spans
            and not (pair_words[pid] & q_rare)
        ]

        for v in range(variants):
            rng = case_rng(seed_key, cid, v)
            docs = []
            n_gold = sum(len(gold_spans[pid]) for pid in must)
            used = sum(pair_tokens(pid) for pid in must) + sep_tokens * len(must)
            if not must:
                stats[f"{kind}:no_gold_in_evidence"] += 1
            elif n_gold > MAX_GOLD:
                stats[f"{kind}:too_many_gold"] += 1
            elif used > DOC_TOKEN_BUDGET:
                stats[f"{kind}:gold_pairs_over_budget"] += 1
            else:
                support = [pid for pid in clean if pid in rel]
                hard = [pid for pid in clean if pid in hard_ids]
                rand = [pid for pid in clean if pid not in rel and pid not in hard_ids]
                for lst in (support, hard, rand):
                    rng.shuffle(lst)
                docs.append((kind, must, [*support, *hard[:MAX_HARD], *rand], used))
            if rng.random() < null_rate:
                pool = list(null_pool)
                rng.shuffle(pool)
                docs.append(("null", [], pool, 0))
            for dkind, fixed, order, used in docs:
                chosen = list(fixed)
                for pid in order:
                    n = pair_tokens(pid)
                    if n > MAX_PAIR_TOKENS or used + sep_tokens + n > DOC_TOKEN_BUDGET:
                        continue
                    chosen.append(pid)
                    used += sep_tokens + n
                if dkind == "null" and used < DOC_TOKEN_BUDGET // 2:
                    stats["null:too_small"] += 1
                    continue
                rng.shuffle(chosen)
                parts, bounds, cursor = [], [], 0
                gold: dict[str, list] = {lv: [] for lv in LEVELS}
                for pid in chosen:
                    if parts:
                        parts.append(SEPARATOR)
                        cursor += len(SEPARATOR)
                    parts.append(text[pid])
                    bounds.append(
                        {
                            "start": cursor,
                            "end": cursor + len(text[pid]),
                            "relevant": pid in rel,
                            "gold": dkind != "null" and pid in gold_spans,
                            "distractor": pid in hard_ids,
                        }
                    )
                    if dkind != "null" and pid in gold_spans:
                        for g in pair_gold(pairs[pid], gold_spans[pid]):
                            a, b = g["needle"]
                            if text[pid][a:b] != g["text"]:
                                raise RuntimeError(f"offset mismatch in {cid}")
                            gold["needle"].append((cursor + a, cursor + b))
                            gold["anchored"].append((cursor + a, cursor + b))
                            if g["anchor"]:
                                ta, tb = g["anchor"]
                                gold["anchored"].append((cursor + ta, cursor + tb))
                            ca, cb = g["clause"]
                            if tokens(text[pid][ca:cb]) > MAX_CLAUSE_TOKENS:
                                stats["clause:fallback_to_needle"] += 1
                                ca, cb = a, b
                            gold["clause"].append((cursor + ca, cursor + cb))
                            gold["history"].append((cursor + ca, cursor + cb))
                            gold["evidence"].append((cursor + ca, cursor + cb))
                    if dkind != "null" and hist_spans.get(pid):
                        for g in pair_gold(pairs[pid], hist_spans[pid]):
                            ca, cb = g["clause"]
                            if tokens(text[pid][ca:cb]) > MAX_CLAUSE_TOKENS:
                                ca, cb = g["needle"]
                            gold["history"].append((cursor + ca, cursor + cb))
                            gold["evidence"].append((cursor + ca, cursor + cb))
                    labelled = pid in gold_spans or hist_spans.get(pid)
                    if dkind != "null" and pid in rel and not labelled:
                        prompt = pairs[pid].get("prompt") or ""
                        a = answer_offset(pairs[pid], {"field": "prompt", "start": 0})
                        if prompt.strip() and tokens(prompt) <= MAX_CLAUSE_TOKENS:
                            gold["evidence"].append(
                                (cursor + a, cursor + a + len(prompt.rstrip()))
                            )
                    cursor += len(text[pid])
                doc = "".join(parts)
                rec = {
                    "data_version": DATA_VERSION,
                    "seed_key": seed_key,
                    "case_id": cid,
                    "variant": v,
                    "kind": dkind,
                    "family": case.get("family"),
                    "query": case["query"] or "",
                    "doc": doc,
                    "gold": {lv: merge(gold[lv]) for lv in LEVELS},
                    "pairs": bounds,
                }
                rec["gold_text"] = [doc[a:b] for a, b in rec["gold"]["needle"]]
                n = len(rec["gold"]["needle"])
                stats[f"{dkind}:docs"] += 1
                stats[f"{dkind}:gold={min(n, 4)}{'+' if n >= 4 else ''}"] += 1
                out.append(rec)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seeds", required=True)
    ap.add_argument("--bench-version", type=int, default=18)
    ap.add_argument("--base", default="qwen3.5-0.8b")
    ap.add_argument("--variants", type=int, default=1, help="documents per case")
    ap.add_argument("--null-rate", type=float, default=1.0)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    tok = m.load_tokenizer(m.download_base(get_base(args.base)))
    cache = default_cache_dir()
    gen = build_generate(find_datagen_dir(), cache)
    rows = []
    stats: Counter = Counter()
    for seed in [int(s) for s in args.seeds.split(",") if s.strip()]:
        art = json.loads(generate(gen, args.bench_version, seed, cache).read_text())
        key = f"v{args.bench_version}-{seed}"
        recs = export_records(art, key)
        raw_cases = {c["id"]: c for c in art.get("memory_cases") or []}
        ex = build_examples(
            recs, raw_cases, key, tok, args.variants, args.null_rate, stats
        )
        print(f"{key}: {len(ex)} examples", file=sys.stderr)
        rows.extend(ex)
    raw = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(raw)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    print(json.dumps(dict(sorted(stats.items()))), file=sys.stderr)
    print(f"wrote {len(rows)} examples to {args.out} sha256 {digest}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
