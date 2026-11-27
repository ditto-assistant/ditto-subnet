#!/usr/bin/env python3
"""Pick one decoding threshold on a calibration seed (never the held-out set).

Reads logits saved by `eval_multi.py multi --dump-logits` on a calibration
set, a seed in neither the training nor the held-out split. It tries a grid
of values for `--param` and prints the best. The objective is balanced, so the
mix of null and positive documents does not decide it:

    0.5 * mean token F1 over positive documents
  + 0.5 * share of null documents with an empty buffer

Ties go to the value closest to 0, the uncalibrated default.

    uv run --project services/cpm-scorer python \\
        docs/research/cpm/span_v2/calibrate.py --model runs/pointer/final \\
        --data calib.jsonl --logits calib-pointer.safetensors --param end_threshold
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import multi_model as mm  # noqa: E402
from cpm_scorer import model as m  # noqa: E402
from cpm_scorer.bases import get_base  # noqa: E402

GRID = [x / 2 for x in range(-16, 9)]  # -8.0 .. 4.0 in steps of 0.5


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--logits", type=Path, required=True)
    ap.add_argument("--param", choices=["null_margin", "end_threshold"], required=True)
    ap.add_argument("--matching", choices=mm.MATCHINGS)
    ap.add_argument("--no-stop", action="store_true")
    ap.add_argument("--clip-pairs", action="store_true")
    ap.add_argument("--base", default="qwen3.5-0.8b")
    args = ap.parse_args(argv)

    from safetensors.torch import load_file

    meta = json.loads((args.model / "head_meta.json").read_text())
    rows = [json.loads(x) for x in args.data.read_text().splitlines() if x]
    tok = m.load_tokenizer(m.download_base(get_base(args.base)))
    enc = [mm.encode(tok, r, meta["level"], 96) for r in rows]
    segs = [mm.segments(e, r["pairs"]) for r, e in zip(rows, enc, strict=True)]
    saved = load_file(str(args.logits))
    results = []
    for v in GRID:
        pos, null = [], []
        for i, e in enumerate(enc):
            buf = mm.decode(
                meta["head"],
                saved[str(i)],
                e,
                meta["k"],
                meta["max_span"],
                matching=args.matching or meta.get("matching", "ordered"),
                stop_at_null=not args.no_stop,
                seg=segs[i] if args.clip_pairs else None,
                **{args.param: v},
            )
            met = mm.example_metrics(mm.live(buf), e.gold)
            (pos if e.gold else null).append(
                met["f1"] if e.gold else float(met["null_correct"])
            )
        f1, nc = sum(pos) / len(pos), sum(null) / len(null)
        results.append((0.5 * f1 + 0.5 * nc, -abs(v), v, f1, nc))
        print(f"{args.param}={v:+.1f} pos_f1={f1:.3f} null={nc:.3f}", file=sys.stderr)
    best = max(results)
    print(
        json.dumps(
            {
                "param": args.param,
                "value": best[2],
                "objective": best[0],
                "pos_f1": best[3],
                "null_correct": best[4],
                "n_pos": sum(bool(e.gold) for e in enc),
                "n_null": sum(not e.gold for e in enc),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
