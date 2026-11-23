#!/usr/bin/env python3
"""Evaluate fixed-buffer span predictors on the held-out multi-span set.

Every predictor returns a list of document-token ranges. The metrics in
`multi_model.example_metrics` are taken twice: against the gold of `--level`
(default: the level the model trained on), and against the `needle` gold, so
wider gold widths can be compared on "does the packet still hold the answer".

Modes:

- `multi --model DIR`: a K-span head from train_multi.py (slots or pointer);
  `--dump-logits FILE` also saves every document's logits;
- `redecode --model DIR --logits FILE`: decode saved logits again on the CPU,
  for decoding variants without another forward pass;
- `single --model DIR`: the single-span head from train_span.py; always one
  span, so it can never say "no answer";
- `chunk --model base|DIR --top N`: the v1 pointwise relevance scorer ranks the
  document's pairs and its top N pairs, whole, are the prediction;
- `packall`: the whole document.

Decoding variants (multi, redecode): `--matching`, `--no-stop`,
`--null-margin`, `--end-threshold`, `--clip-pairs` (no span crosses a
memory boundary) and `--no-count` (decode a count-head model by null scores);
see `multi_model.decode`.

`dated_tokens` is what the packet costs when the packer prefixes each memory
it quotes with that memory's compact date (`[YYYY-MM-DD] `, from metadata), so
the reader always gets the "as of" date without the model predicting it.

    uv run --project services/cpm-scorer python \\
        docs/research/cpm/span_v2/eval_multi.py multi --model runs/slots/final \\
        --data heldout.jsonl --device mps --out eval-slots.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import multi_model as mm  # noqa: E402
import span_model as sm  # noqa: E402
from cpm_scorer import model as m  # noqa: E402
from cpm_scorer.bases import get_base  # noqa: E402

HEAD_META = "head_meta.json"
TIMESTAMP = re.compile(r"^\[(\d{4}-\d{2}-\d{2})")


def log(msg: str) -> None:
    print(
        f"[eval_multi {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True
    )


def load_net(base_dir: Path, model_dir: Path | None):
    net = m.load_base_model(base_dir)
    if model_dir is not None and (model_dir / "adapter_config.json").is_file():
        net = m.apply_lora(net, model_dir)
    return net


def decode_opts(args, meta: dict) -> dict:
    return {
        "matching": args.matching or meta.get("matching", "ordered"),
        "stop_at_null": not args.no_stop,
        "null_margin": args.null_margin,
        "end_threshold": args.end_threshold,
        "use_count": bool(meta.get("count")) and not args.no_count,
    }


def date_tokens(tok, r: dict, e, seg: list[int], pred) -> int:
    """Tokens of one compact date prefix per distinct memory quoted."""
    first = e.doc_tokens[0]
    mems = sorted({seg[a - first] for a, _ in pred} - {-1})
    total = 0
    for idx in mems:
        p = r["pairs"][idx]
        hit = TIMESTAMP.match(r["doc"][p["start"] : p["end"]])
        if hit:
            ids = tok(f"[{hit.group(1)}] ", add_special_tokens=False)["input_ids"]
            total += len(ids)
    return total


def score_rows(tok, rows, enc, enc_needle, segs, preds) -> list[dict]:
    out_rows = []
    for r, e, en, seg, pred in zip(rows, enc, enc_needle, segs, preds, strict=True):
        mets = mm.example_metrics(pred, e.gold)
        nmets = mm.example_metrics(pred, en.gold)
        texts = []
        for a, b in pred:
            t = r["doc"][max(e.offsets[a][0], 0) : e.offsets[b][1]]
            texts.append(t if len(t) <= 300 else t[:300] + "…")
        out_rows.append(
            {
                "seed_key": r["seed_key"],
                "case_id": r["case_id"],
                "kind": r["kind"],
                "family": r["family"],
                "gold": [list(x) for x in e.gold],
                "gold_text": r["gold_text"],
                "pred": [list(x) for x in pred],
                "pred_text": texts,
                **mets,
                "dated_tokens": mets["tokens"] + date_tokens(tok, r, e, seg, pred),
                **{
                    f"needle_{k}": v
                    for k, v in nmets.items()
                    if k in ("gold_contained", "all_contained", "f1", "exact_set")
                },
            }
        )
    return out_rows


def summarize(out_rows: list[dict], extra: dict, raw: bytes) -> dict:
    preds_sha = hashlib.sha256(
        json.dumps([x["pred"] for x in out_rows]).encode()
    ).hexdigest()
    pos = [x for x in out_rows if x["n_gold"]]
    return {
        **extra,
        "dtype": "float32",
        "data_sha256": hashlib.sha256(raw).hexdigest(),
        "preds_sha256": preds_sha,
        "overall": mm.summarize(out_rows),
        "needle": mm.boot(
            pos,
            (
                "needle_gold_contained",
                "needle_all_contained",
                "needle_f1",
                "dated_tokens",
            ),
        ),
        "per_seed": {
            k: mm.summarize([x for x in out_rows if x["seed_key"] == k])
            for k in sorted({x["seed_key"] for x in out_rows})
        },
    }


def write(out: Path, summary: dict, out_rows: list[dict]) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, indent=2) + "\n")
    out.with_suffix(".preds.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in out_rows)
    )
    o, nl = summary["overall"]["positive"], summary["overall"]["null"]
    log(
        f"{summary['mode']}: pos n={o['n']} f1={o['f1']['mean']:.3f} "
        f"span_f1={o['span_f1']['mean']:.3f} "
        f"contained={o['gold_contained']['mean']:.3f} "
        f"tokens={o['tokens']['mean']:.1f} | null n={nl['n']} "
        f"correct={nl['null_correct']['mean']:.3f}"
    )


def load_data(path: Path, limit: int = 0):
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.decode().splitlines() if line]
    return raw, rows[:limit] if limit else rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["multi", "redecode", "single", "chunk", "packall"])
    ap.add_argument("--model")
    ap.add_argument("--top", type=int, default=1, help="chunk: pairs to return")
    ap.add_argument("--level", help="gold width (default: the model's, else needle)")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--base", default="qwen3.5-0.8b")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--limit", type=int, default=0, help="first N docs (smoke)")
    ap.add_argument("--logits", type=Path, help="redecode: saved logits")
    ap.add_argument("--dump-logits", type=Path, help="multi: save logits here")
    ap.add_argument("--matching", choices=mm.MATCHINGS)
    ap.add_argument("--no-stop", action="store_true")
    ap.add_argument("--null-margin", type=float, default=0.0)
    ap.add_argument("--end-threshold", type=float, default=0.0)
    ap.add_argument("--clip-pairs", action="store_true")
    ap.add_argument("--no-count", action="store_true", help="ignore a count head")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    import torch
    from safetensors.torch import load_file, save_file
    from torch.nn.attention import SDPBackend, sdpa_kernel

    torch.manual_seed(0)
    torch.use_deterministic_algorithms(True, warn_only=True)
    raw, rows = load_data(args.data, args.limit)
    base_dir = m.download_base(get_base(args.base))
    tok = m.load_tokenizer(base_dir)
    mdir = Path(args.model) if args.model not in (None, "base") else None
    meta = {"k": mm.K_SLOTS, "max_span": 96, "level": "needle"}
    if args.mode in ("multi", "redecode"):
        meta = json.loads((mdir / HEAD_META).read_text())
    level = args.level or meta["level"]
    enc = [mm.encode(tok, r, level, 96) for r in rows]
    enc_needle = [mm.encode(tok, r, "needle", 96) for r in rows]
    segs = [mm.segments(e, r["pairs"]) for r, e in zip(rows, enc, strict=True)]
    opts = decode_opts(args, meta)
    dumped: dict = {}

    def buffer(i: int, lg):
        seg = segs[i] if args.clip_pairs else None
        buf = mm.decode(
            meta["head"], lg, enc[i], meta["k"], meta["max_span"], seg=seg, **opts
        )
        return mm.live(buf)

    if args.mode == "redecode":
        saved = load_file(str(args.logits))

        def predict(i, _r, _e):
            return buffer(i, saved[str(i)])

    elif args.mode in ("multi", "single"):
        net = load_net(base_dir, mdir).to(device=args.device, dtype=torch.float32)
        net.eval()
        state = load_file(str(mdir / sm.HEAD_FILE))
        hidden = net.config.hidden_size
        if args.mode == "multi":
            head = mm.build_head(
                meta["head"],
                hidden,
                meta["k"],
                meta["max_span"],
                count=bool(meta.get("count")),
            )
        else:
            head = sm.SpanHead.build(hidden)
        head.load_state_dict(state)
        head.to(args.device)

        def predict(i, _r, e):
            with torch.inference_mode(), sdpa_kernel(SDPBackend.MATH):
                if args.mode == "single":
                    s, t = sm.span_logits(net.model, head, e.input_ids, args.device)
                    return [sm.decode(s, t, e)]
                lg = mm.head_logits(net.model, head, e.input_ids, args.device)
            if args.dump_logits:
                dumped[str(i)] = lg.detach().cpu().contiguous()
            return buffer(i, lg)

    elif args.mode == "chunk":
        scorer = m.RelevanceScorer(load_net(base_dir, mdir), tok, args.device)

        def predict(_i, r, e):
            pairs = r["pairs"]
            texts = [r["doc"][p["start"] : p["end"]] for p in pairs]
            scores, _ = scorer.score([(r["query"], t) for t in texts])
            order = sorted(range(len(pairs)), key=lambda i: (-scores[i], i))
            spans = [
                sm.char_to_tokens(e, pairs[i]["start"], pairs[i]["end"])
                for i in order[: args.top]
            ]
            return sorted(s for s in spans if s)

    else:

        def predict(_i, _r, e):
            return [e.doc_tokens]

    preds = []
    t0 = time.time()
    for i, (r, e) in enumerate(zip(rows, enc, strict=True)):
        preds.append(predict(i, r, e))
        if (i + 1) % 50 == 0:
            log(f"{i + 1}/{len(rows)} ({time.time() - t0:.0f}s)")
    if dumped:
        args.dump_logits.parent.mkdir(parents=True, exist_ok=True)
        save_file(dumped, str(args.dump_logits))

    out_rows = score_rows(tok, rows, enc, enc_needle, segs, preds)
    extra = {
        "mode": args.mode,
        "model": args.model,
        "top": args.top if args.mode == "chunk" else None,
        "head": meta if args.mode in ("multi", "redecode") else None,
        "decode": {**opts, "clip_pairs": args.clip_pairs},
        "level": level,
        "device": args.device,
    }
    write(args.out, summarize(out_rows, extra, raw), out_rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
