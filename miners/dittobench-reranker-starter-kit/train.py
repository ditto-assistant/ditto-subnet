#!/usr/bin/env python3
"""Fine-tune a cross-encoder on practice pools.

    python train.py --pools practice.json --base cross-encoder/ms-marco-TinyBERT-L2-v2 \
        --out ft/ --epochs 2

Every gold candidate becomes a positive pair and every other candidate in the
pool a negative (capped by --max-neg, hardest first: base order puts the
lexically closest negatives first). Then run ``export.py ft/ artifact/``.

Practice pools are generated from seeds you choose
(``poolgen/cmd/rerank-pools``). Scored pools use seeds fixed after you commit
and a held surface salt, so a model that memorises practice wording gains
nothing. Train for retrieval, not for the generator.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def load_pairs(
    path: Path, max_neg: int, text_chars: int
) -> list[tuple[str, str, float]]:
    bundle = json.loads(path.read_text())
    pairs: list[tuple[str, str, float]] = []
    for s in bundle["sets"]:
        for pool in s["pools"]:
            gold = set(pool["gold"])
            negatives = 0
            for cand in pool["candidates"]:
                text = cand["text"][:text_chars]
                if cand["id"] in gold:
                    pairs.append((pool["query"], text, 1.0))
                elif negatives < max_neg:
                    pairs.append((pool["query"], text, 0.0))
                    negatives += 1
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pools", type=Path, required=True)
    parser.add_argument("--base", default="cross-encoder/ms-marco-TinyBERT-L2-v2")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--max-neg", type=int, default=8)
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--text-chars", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    from sentence_transformers import InputExample
    from sentence_transformers.cross_encoder import CrossEncoder
    from torch.utils.data import DataLoader

    random.seed(args.seed)
    pairs = load_pairs(args.pools, args.max_neg, args.text_chars)
    random.shuffle(pairs)
    print(f"{len(pairs)} training pairs ({sum(p[2] for p in pairs):.0f} positive)")
    examples = [InputExample(texts=[q, d], label=y) for q, d, y in pairs]
    loader = DataLoader(examples, shuffle=True, batch_size=args.batch_size)
    model = CrossEncoder(args.base, num_labels=1, max_length=args.max_length)
    model.fit(
        train_dataloader=loader,
        epochs=args.epochs,
        warmup_steps=int(0.1 * len(loader) * args.epochs),
        show_progress_bar=True,
    )
    model.save(str(args.out))
    print(
        f"saved to {args.out}; next: python export.py {args.out} artifact/ "
        f"--base-model {args.base} --license <spdx>"
    )


if __name__ == "__main__":
    main()
