#!/usr/bin/env python3
"""Evaluate fixed-buffer span predictors on the held-out multi-span set.

Every predictor returns a list of document-token ranges. The metrics in
`multi_model.example_metrics` are taken twice: against the gold of `--level`
(default: the level the model trained on), and against the `needle` gold, so
wider gold widths can be compared on "does the packet still hold the answer".

Modes:

- `multi --model DIR`: a K-span head from train_multi.py (slots or pointer);
- `single --model DIR`: the single-span head from train_span.py; always one
  span, so it can never say "no answer";
- `chunk --model base|DIR --top N`: the v1 pointwise relevance scorer ranks the
  document's pairs and its top N pairs, whole, are the prediction;
- `packall`: the whole document.

    uv run --project services/cpm-scorer python \\
        docs/research/cpm/span_v2/eval_multi.py multi --model runs/pointer/final \\
        --data heldout.jsonl --device mps --out eval-pointer.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
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


def log(msg: str) -> None:
    print(
        f"[eval_multi {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True
    )


def load_net(base_dir: Path, model_dir: Path | None):
    net = m.load_base_model(base_dir)
    if model_dir is not None and (model_dir / "adapter_config.json").is_file():
        net = m.apply_lora(net, model_dir)
    return net


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["multi", "single", "chunk", "packall"])
    ap.add_argument("--model")
    ap.add_argument("--top", type=int, default=1, help="chunk: pairs to return")
    ap.add_argument("--level", help="gold width (default: the model's, else needle)")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--base", default="qwen3.5-0.8b")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--limit", type=int, default=0, help="first N docs (smoke)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    import torch
    from torch.nn.attention import SDPBackend, sdpa_kernel

    torch.manual_seed(0)
    torch.use_deterministic_algorithms(True, warn_only=True)
    raw = args.data.read_bytes()
    rows = [json.loads(line) for line in raw.decode().splitlines() if line]
    if args.limit:
        rows = rows[: args.limit]
    base_dir = m.download_base(get_base(args.base))
    tok = m.load_tokenizer(base_dir)
    mdir = Path(args.model) if args.model not in (None, "base") else None
    meta = {"k": mm.K_SLOTS, "max_span": 96, "level": "needle"}
    if args.mode == "multi":
        meta = json.loads((mdir / HEAD_META).read_text())
    level = args.level or meta["level"]
    enc = [mm.encode(tok, r, level, 96) for r in rows]
    enc_needle = [mm.encode(tok, r, "needle", 96) for r in rows]

    if args.mode in ("multi", "single"):
        from safetensors.torch import load_file

        net = load_net(base_dir, mdir).to(device=args.device, dtype=torch.float32)
        net.eval()
        state = load_file(str(mdir / sm.HEAD_FILE))
        hidden = net.config.hidden_size
        if args.mode == "multi":
            head = mm.build_head(meta["head"], hidden, meta["k"], meta["max_span"])
        else:
            head = sm.SpanHead.build(hidden)
        head.load_state_dict(state)
        head.to(args.device)

        def predict(_r, e):
            with torch.inference_mode(), sdpa_kernel(SDPBackend.MATH):
                if args.mode == "single":
                    s, t = sm.span_logits(net.model, head, e.input_ids, args.device)
                    return [sm.decode(s, t, e)]
                lg = mm.head_logits(net.model, head, e.input_ids, args.device)
            buf = mm.decode(meta["head"], lg, e, meta["k"], meta["max_span"])
            return mm.live(buf)

    elif args.mode == "chunk":
        scorer = m.RelevanceScorer(load_net(base_dir, mdir), tok, args.device)

        def predict(r, e):
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

        def predict(_r, e):
            return [e.doc_tokens]

    out_rows = []
    t0 = time.time()
    for n, (r, e, en) in enumerate(zip(rows, enc, enc_needle, strict=True), 1):
        pred = predict(r, e)
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
                **{
                    f"needle_{k}": v
                    for k, v in nmets.items()
                    if k in ("gold_contained", "all_contained", "f1", "exact_set")
                },
            }
        )
        if n % 25 == 0:
            log(f"{n}/{len(rows)} ({time.time() - t0:.0f}s)")

    preds_sha = hashlib.sha256(
        json.dumps([x["pred"] for x in out_rows]).encode()
    ).hexdigest()
    pos = [x for x in out_rows if x["n_gold"]]
    summary = {
        "mode": args.mode,
        "model": args.model,
        "top": args.top if args.mode == "chunk" else None,
        "head": meta if args.mode == "multi" else None,
        "level": level,
        "device": args.device,
        "dtype": "float32",
        "data_sha256": hashlib.sha256(raw).hexdigest(),
        "preds_sha256": preds_sha,
        "overall": mm.summarize(out_rows),
        "needle": mm.boot(
            pos,
            ("needle_gold_contained", "needle_all_contained", "needle_f1"),
        ),
        "per_seed": {
            k: mm.summarize([x for x in out_rows if x["seed_key"] == k])
            for k in sorted({x["seed_key"] for x in out_rows})
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    args.out.with_suffix(".preds.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in out_rows)
    )
    o, nl = summary["overall"]["positive"], summary["overall"]["null"]
    log(
        f"{args.mode}: pos n={o['n']} f1={o['f1']['mean']:.3f} "
        f"span_f1={o['span_f1']['mean']:.3f} "
        f"contained={o['gold_contained']['mean']:.3f} "
        f"tokens={o['tokens']['mean']:.1f} | null n={nl['n']} "
        f"correct={nl['null_correct']['mean']:.3f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
