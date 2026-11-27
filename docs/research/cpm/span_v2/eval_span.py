#!/usr/bin/env python3
"""Evaluate span predictors on the held-out span set.

Modes (all predictions become a token range of the document, then the same
metrics in `span_model.example_metrics` apply):

- `span --model DIR`: span head (plus a LoRA when DIR has adapter_config.json);
- `zeroshot`: the unmodified base is asked to quote the answer verbatim
  (greedy, 32 new tokens); the quote is located in the document
  case-insensitively, first occurrence; no match is an empty prediction;
- `chunk --model base|DIR`: the v1 pointwise relevance scorer ranks the
  document's pairs and its top pair, whole, is the prediction (ties go to the
  earlier pair);
- `packall`: the whole document, which is what v1 packs when everything fits
  the 2,048-token budget.

    uv run --project services/cpm-scorer python \\
        docs/research/cpm/span_v2/eval_span.py span --model runs/lora/final \\
        --data heldout.jsonl --device mps --out eval-lora.json
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

import span_model as sm  # noqa: E402
from cpm_scorer import model as m  # noqa: E402
from cpm_scorer.bases import get_base  # noqa: E402

ZEROSHOT_SYSTEM = (
    "Quote the exact words from the Document that answer the Query. Reply with "
    "only the quote, copied verbatim."
)
QUOTES = "\"'`“”‘’"


def log(msg: str) -> None:
    print(f"[eval_span {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def load_net(base_dir: Path, model_dir: Path | None):
    net = m.load_base_model(base_dir)
    if model_dir is not None and (model_dir / "adapter_config.json").is_file():
        net = m.apply_lora(net, model_dir)
    return net


def locate(quote: str, doc: str) -> tuple[int, int] | None:
    q = quote.strip().strip(QUOTES).strip()
    for cand in (q, q.rstrip(".")):
        if len(cand) >= 1:
            hit = re.search(re.escape(cand), doc, flags=re.IGNORECASE)
            if hit:
                return hit.span()
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("mode", choices=["span", "zeroshot", "chunk", "packall"])
    ap.add_argument("--model", help="span: run dir; chunk: base or a v1 LoRA dir")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--base", default="qwen3.5-0.8b")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    import torch

    torch.manual_seed(0)
    torch.use_deterministic_algorithms(True, warn_only=True)
    raw = args.data.read_bytes()
    rows = [json.loads(line) for line in raw.decode().splitlines() if line]
    base_dir = m.download_base(get_base(args.base))
    tok = m.load_tokenizer(base_dir)
    encoded = [sm.encode(tok, r) for r in rows]

    predict = None
    if args.mode == "span":
        from safetensors.torch import load_file

        mdir = Path(args.model)
        net = load_net(base_dir, mdir).to(device=args.device, dtype=torch.float32)
        net.eval()
        head = sm.SpanHead.build(net.config.hidden_size)
        head.load_state_dict(load_file(str(mdir / sm.HEAD_FILE)))
        head.to(args.device)

        def predict(_r, e):
            from torch.nn.attention import SDPBackend, sdpa_kernel

            with torch.inference_mode(), sdpa_kernel(SDPBackend.MATH):
                s, t = sm.span_logits(net.model, head, e.input_ids, args.device)
            return sm.decode(s, t, e), {}

    elif args.mode == "zeroshot":
        net = load_net(base_dir, None).to(device=args.device, dtype=torch.float32)
        net.eval()

        def predict(r, e):
            text, _ = sm.render(tok, r["query"], r["doc"], ZEROSHOT_SYSTEM)
            ids = tok(text, add_special_tokens=False, return_tensors="pt")
            ids = {k: v.to(args.device) for k, v in ids.items()}
            with torch.inference_mode():
                out = net.generate(
                    **ids,
                    max_new_tokens=32,
                    do_sample=False,
                    pad_token_id=tok.eos_token_id,
                )
            quote = tok.decode(
                out[0, ids["input_ids"].shape[1] :], skip_special_tokens=True
            )
            hit = locate(quote, r["doc"])
            pred = sm.char_to_tokens(e, *hit) if hit else None
            return pred, {"quote": quote, "found": hit is not None}

    elif args.mode == "chunk":
        src = None if args.model in (None, "base") else Path(args.model)
        scorer = m.RelevanceScorer(load_net(base_dir, src), tok, args.device)

        def predict(r, e):
            pairs = r["pairs"]
            texts = [r["doc"][p["start"] : p["end"]] for p in pairs]
            scores, _ = scorer.score([(r["query"], t) for t in texts])
            best = max(range(len(pairs)), key=lambda i: (scores[i], -i))
            p = pairs[best]
            return sm.char_to_tokens(e, p["start"], p["end"]), {
                "pair_hit": bool(p["answer"])
            }

    else:

        def predict(_r, e):
            return e.doc_tokens, {}

    out_rows = []
    t0 = time.time()
    for n, (r, e) in enumerate(zip(rows, encoded, strict=True), 1):
        pred, extra = predict(r, e)
        mets = sm.example_metrics(pred, e)
        text = ""
        if pred:
            a, b = e.offsets[pred[0]][0], e.offsets[pred[1]][1]
            text = r["doc"][max(a, 0) : b]
        out_rows.append(
            {
                "seed_key": r["seed_key"],
                "case_id": r["case_id"],
                "family": r["family"],
                "gold": r["answer_text"],
                "pred": list(pred) if pred else None,
                "pred_text": text if len(text) <= 400 else text[:400] + "…",
                **mets,
                **extra,
            }
        )
        if n % 10 == 0:
            log(f"{n}/{len(rows)} ({time.time() - t0:.0f}s)")

    preds_sha = hashlib.sha256(
        json.dumps([x["pred"] for x in out_rows]).encode()
    ).hexdigest()
    summary = {
        "mode": args.mode,
        "model": args.model,
        "device": args.device,
        "dtype": "float32",
        "data_sha256": hashlib.sha256(raw).hexdigest(),
        "preds_sha256": preds_sha,
        "overall": sm.summarize(out_rows),
        "per_seed": {
            k: sm.summarize([x for x in out_rows if x["seed_key"] == k])
            for k in sorted({x["seed_key"] for x in out_rows})
        },
    }
    if args.mode == "zeroshot":
        summary["verbatim_found"] = sum(x["found"] for x in out_rows) / len(out_rows)
    if args.mode == "chunk":
        summary["pair_hit"] = sum(x["pair_hit"] for x in out_rows) / len(out_rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    args.out.with_suffix(".preds.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in out_rows)
    )
    o = summary["overall"]
    log(
        f"{args.mode}: n={o['n']} exact={o['exact']['mean']:.3f} "
        f"f1={o['f1']['mean']:.3f} iou={o['iou']['mean']:.3f} "
        f"contained={o['contained']['mean']:.3f} tokens={o['tokens']['mean']:.1f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
