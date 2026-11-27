#!/usr/bin/env python3
"""Reader test: does a small local model answer better from a span packet?

Span overlap says where the packet points, not whether it helps. Here a reader
model (default Qwen3.5-2B, greedy, thinking off) answers each held-out query
from one packet, and the answer is graded against the generator's own answer
fields:

- `answer` documents: correct when the reply contains every `answer_items`
  string, or, without items, any expected / accepted answer string
  (case-insensitive, whitespace-collapsed);
- `absence` and `null` documents: correct when the reply declines ("I don't
  know" and close variants: a keyword heuristic, reported as such).

A packet is the predicted spans, grouped by memory in document order, each
memory prefixed by its compact date (`[YYYY-MM-DD]`) as the packer would.
Packets come from `eval_multi.py` / `eval_span.py` prediction files (token
ranges are mapped back to text through the same template encoding), from a
gold width (`gold:history`, an oracle), the whole document (`packall`) or no
memory at all (`closed`).

Null documents are subsampled deterministically (sha256 of the case id,
NULL_KEEP of NULL_MOD) to keep the run to a few hours on a Mac mini.

    uv run --project services/cpm-scorer python \\
        docs/research/cpm/span_v2/reader_eval.py --data heldout.jsonl \\
        --packet closed --packet packall --packet slots=eval/slots.preds.jsonl \\
        --device mps --out-dir reader/
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import multi_model as mm  # noqa: E402
from cpm_scorer import model as m  # noqa: E402
from cpm_scorer.bases import get_base  # noqa: E402
from cpm_scorer.generate import (  # noqa: E402
    build_generate,
    default_cache_dir,
    find_datagen_dir,
    generate,
)
from cpm_scorer.labels import answer_strings, export_records, norm_strings  # noqa: E402

READER_SYSTEM = (
    "You are the user's personal assistant. Answer the user's question using "
    "only the memory excerpts. Reply with just the answer, no explanation. If "
    "the excerpts do not contain the answer, reply exactly: I don't know."
)
DECLINE = re.compile(
    r"\b(i don'?t know|do not know|don'?t have|no record|not (?:mentioned|"
    r"stated|provided|available|specified|recorded|in the)|cannot (?:find|"
    r"determine)|unknown)\b",
    re.IGNORECASE,
)
TIMESTAMP = re.compile(r"^\[(\d{4}-\d{2}-\d{2})")
NULL_MOD, NULL_KEEP = 5, 2
MAX_NEW_TOKENS = 40


def log(msg: str) -> None:
    print(f"[reader {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def keep(r: dict) -> bool:
    if r["kind"] != "null":
        return True
    h = int(hashlib.sha256(r["case_id"].encode()).hexdigest(), 16)
    return h % NULL_MOD < NULL_KEEP


def answers(bench_version: int, seed_keys: set[str]) -> dict:
    cache = default_cache_dir()
    gen = build_generate(find_datagen_dir(), cache)
    out = {}
    for key in sorted(seed_keys):
        seed = int(key.split("-")[1])
        art = json.loads(generate(gen, bench_version, seed, cache).read_text())
        for c in export_records(art, key)["cases"]:
            if c["kind"] == "memory":
                items = norm_strings(c.get("answer_items"))
                out[(key, c["case_id"])] = {
                    "items": items,
                    "any": answer_strings(c),
                }
    return out


def correct(r: dict, reply: str, gold: dict) -> bool:
    if r["kind"] != "answer":
        return bool(DECLINE.search(reply))
    text = norm(reply)
    if gold["items"]:
        return all(norm(x) in text for x in gold["items"])
    return any(norm(x) in text for x in gold["any"])


def packet_text(r: dict, e, spans) -> str:
    """Spans grouped by memory, in document order, each memory dated."""
    groups: dict[int, list[str]] = {}
    for a, b in sorted(spans):
        lo, hi = e.offsets[a][0], e.offsets[b][1]
        idx = next(
            (i for i, p in enumerate(r["pairs"]) if p["start"] <= lo < p["end"]), -1
        )
        groups.setdefault(idx, []).append(r["doc"][max(lo, 0) : hi].strip())
    lines = []
    for idx in sorted(groups):
        date = ""
        if idx >= 0:
            p = r["pairs"][idx]
            hit = TIMESTAMP.match(r["doc"][p["start"] : p["end"]])
            date = f"[{hit.group(1)}] " if hit else ""
        lines.append(date + " … ".join(groups[idx]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument(
        "--packet",
        action="append",
        required=True,
        help="closed | packall | gold:LEVEL | NAME=PREDS.jsonl",
    )
    ap.add_argument("--reader", default="qwen3.5-2b")
    ap.add_argument("--bench-version", type=int, default=18)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--dtype", default="bfloat16")
    ap.add_argument("--out-dir", type=Path, required=True)
    args = ap.parse_args(argv)

    import torch

    torch.manual_seed(0)
    rows = [json.loads(x) for x in args.data.read_text().splitlines() if x]
    rows = [r for r in rows if keep(r)]
    gold = answers(args.bench_version, {r["seed_key"] for r in rows})
    span_tok = m.load_tokenizer(m.download_base(get_base("qwen3.5-0.8b")))
    enc = [mm.encode(span_tok, r, "needle", 96) for r in rows]
    rdir = m.download_base(get_base(args.reader))
    tok = m.load_tokenizer(rdir)
    net = m.load_base_model(rdir).to(
        device=args.device, dtype=getattr(torch, args.dtype)
    )
    net.eval()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    log(f"{len(rows)} documents; reader {args.reader} {args.dtype}")

    for spec in args.packet:
        name, _, src = spec.partition("=")
        name = name.replace(":", "-")
        out = args.out_dir / f"{name}.json"
        if out.exists():
            continue
        preds = {}
        if src:
            for line in Path(src).read_text().splitlines():
                x = json.loads(line)
                preds[(x["seed_key"], x["case_id"], x["kind"])] = x["pred"]
        results = []
        t0 = time.time()
        for n, (r, e) in enumerate(zip(rows, enc, strict=True), 1):
            if name == "closed":
                packet = ""
            elif name == "packall":
                packet = r["doc"]
            elif name.startswith("gold-"):
                level = name[len("gold-") :]
                ge = mm.encode(span_tok, r, level, 96)
                packet = packet_text(r, ge, ge.gold)
            else:
                packet = packet_text(
                    r, e, preds[(r["seed_key"], r["case_id"], r["kind"])]
                )
            user = f"Memory excerpts:\n{packet or '(none)'}\n\nQuestion: {r['query']}"
            text = tok.apply_chat_template(
                [
                    {"role": "system", "content": READER_SYSTEM},
                    {"role": "user", "content": user},
                ],
                tokenize=False,
                add_generation_prompt=True,
                enable_thinking=False,
            )
            ids = tok(text, add_special_tokens=False, return_tensors="pt")
            ids = {k: v.to(args.device) for k, v in ids.items()}
            with torch.inference_mode():
                gen = net.generate(
                    **ids,
                    max_new_tokens=MAX_NEW_TOKENS,
                    do_sample=False,
                    pad_token_id=tok.eos_token_id,
                )
            reply = tok.decode(
                gen[0, ids["input_ids"].shape[1] :], skip_special_tokens=True
            ).strip()
            ok = correct(r, reply, gold[(r["seed_key"], r["case_id"])])
            results.append(
                {
                    "seed_key": r["seed_key"],
                    "case_id": r["case_id"],
                    "kind": r["kind"],
                    "family": r["family"],
                    "packet_tokens": len(
                        span_tok(packet, add_special_tokens=False)["input_ids"]
                    )
                    if packet
                    else 0,
                    "reply": reply,
                    "correct": ok,
                }
            )
            if n % 50 == 0:
                log(f"{name}: {n}/{len(rows)} ({time.time() - t0:.0f}s)")
        summary = {"packet": spec, "reader": args.reader, "dtype": args.dtype}
        for kind in ("answer", "absence", "null"):
            sub = [x for x in results if x["kind"] == kind]
            summary[kind] = boot(sub)
        out.with_suffix(".jsonl").write_text(
            "".join(json.dumps(x) + "\n" for x in results)
        )
        out.write_text(json.dumps(summary, indent=2) + "\n")
        a = summary["answer"]
        log(
            f"{name}: answer acc {a['correct']['mean']:.3f} (n {a['n']}), "
            f"absence {summary['absence']['correct']['mean']:.3f}, "
            f"null {summary['null']['correct']['mean']:.3f}, "
            f"tokens {a['packet_tokens']['mean']:.0f}"
        )
    return 0


def boot(rows: list[dict], resamples: int = 2000, seed: int = 20261122) -> dict:
    n = len(rows)
    out: dict = {"n": n}
    if not n:
        return out
    rng = random.Random(seed)
    idx = [[rng.randrange(n) for _ in range(n)] for _ in range(resamples)]
    for key in ("correct", "packet_tokens"):
        vals = [float(r[key]) for r in rows]
        boots = sorted(sum(vals[i] for i in ix) / n for ix in idx)
        out[key] = {
            "mean": sum(vals) / n,
            "ci95": [boots[int(0.025 * resamples)], boots[int(0.975 * resamples) - 1]],
        }
    return out


if __name__ == "__main__":
    sys.exit(main())
