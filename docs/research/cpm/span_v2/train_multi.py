#!/usr/bin/env python3
"""Train a CPM v2 multi-span PoC model: a LoRA on the base plus a K-span head.

`--head slots|pointer` picks how the fixed buffer is filled (multi_model.py),
`--matching ordered|set` how gold spans are assigned to slots, and
`--level needle|anchored|clause|history` which gold width is learned. Documents
with more gold spans than `--k` are skipped and counted.

Gradient checkpointing is always on (see train_span.py for the memory numbers).
Resumable: every `--ckpt-every` optimiser steps the trainable tensors, the
optimiser state and the position go atomically to OUT/ckpt.pt, and a rerun
with the same arguments continues from there.

    uv run --project services/cpm-scorer python \\
        docs/research/cpm/span_v2/train_multi.py --head pointer \\
        --data train.jsonl --device mps --out runs/pointer
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import multi_model as mm  # noqa: E402
import span_model as sm  # noqa: E402
from cpm_scorer import model as m  # noqa: E402
from cpm_scorer.bases import ALLOWED_LORA_TARGETS, get_base  # noqa: E402

HEAD_META = "head_meta.json"


def log(msg: str) -> None:
    print(
        f"[train_multi {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--head", choices=mm.HEADS, required=True)
    ap.add_argument("--level", default="needle")
    ap.add_argument("--matching", choices=mm.MATCHINGS, default="ordered")
    ap.add_argument("--count", action="store_true", help="slots: add a count head")
    ap.add_argument("--k", type=int, default=mm.K_SLOTS)
    ap.add_argument("--max-span", type=int, default=sm.MAX_SPAN_TOKENS)
    ap.add_argument("--base", default="qwen3.5-0.8b")
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--lr", type=float, default=1e-4, help="LoRA learning rate")
    ap.add_argument("--head-lr", type=float, default=1e-3)
    ap.add_argument("--accum", type=int, default=4)
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--warmup", type=int, default=10, help="optimiser steps")
    ap.add_argument("--ckpt-every", type=int, default=10)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument(
        "--init",
        type=Path,
        help="start from another run's ckpt.pt (head + LoRA weights, fresh optimiser)",
    )
    args = ap.parse_args(argv)

    import torch
    from peft import LoraConfig, get_peft_model
    from safetensors.torch import save_file

    torch.manual_seed(args.seed)
    raw = args.data.read_bytes()
    data_sha = hashlib.sha256(raw).hexdigest()
    rows = [json.loads(line) for line in raw.decode().splitlines() if line]
    base = get_base(args.base)
    base_dir = m.download_base(base)
    tok = m.load_tokenizer(base_dir)
    encoded = [mm.encode(tok, r, args.level, args.max_span) for r in rows]
    skipped = sum(len(e.gold) > args.k for e in encoded)
    encoded = [e for e in encoded if len(e.gold) <= args.k]
    log(
        f"{len(encoded)} examples, {skipped} skipped over k={args.k} "
        f"(data sha256 {data_sha[:16]}, level {args.level}, head {args.head})"
    )

    net = m.load_base_model(base_dir)
    net.config.use_cache = False
    net.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False}
    )
    net.enable_input_require_grads()
    net = get_peft_model(
        net,
        LoraConfig(
            r=args.rank,
            lora_alpha=args.alpha,
            lora_dropout=0.0,
            target_modules=sorted(ALLOWED_LORA_TARGETS),
        ),
    )
    inner = net.get_base_model()
    head = mm.build_head(
        args.head,
        net.config.hidden_size,
        args.k,
        args.max_span,
        args.seed,
        count=args.count,
    )
    net.to(device=args.device, dtype=torch.float32)
    head.to(device=args.device)
    net.train()
    lora = [p for p in net.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(
        [
            {"params": list(head.parameters()), "lr": args.head_lr},
            {"params": lora, "lr": args.lr},
        ],
        weight_decay=0.0,
    )
    base_lrs = [g["lr"] for g in opt.param_groups]

    per_epoch = len(encoded)
    total_items = per_epoch * args.epochs
    total_steps = -(-per_epoch // args.accum) * args.epochs
    args.out.mkdir(parents=True, exist_ok=True)
    ckpt = args.out / "ckpt.pt"
    done_items, step, history = 0, 0, []
    if ckpt.exists():
        state = torch.load(ckpt, map_location=args.device, weights_only=True)
        head.load_state_dict(state["head"])
        names = {n for n, p in net.named_parameters() if p.requires_grad}
        missing = names - set(state["lora"])
        if missing:
            raise RuntimeError(f"checkpoint lacks {len(missing)} LoRA tensors")
        net.load_state_dict(state["lora"], strict=False)
        opt.load_state_dict(state["opt"])
        done_items, step, history = state["items"], state["step"], state["history"]
        log(f"resumed at item {done_items} step {step}")
    elif args.init:
        state = torch.load(args.init, map_location=args.device, weights_only=True)
        head.load_state_dict(state["head"])
        names = {n for n, p in net.named_parameters() if p.requires_grad}
        missing = names - set(state["lora"])
        if missing:
            raise RuntimeError(f"init checkpoint lacks {len(missing)} LoRA tensors")
        net.load_state_dict(state["lora"], strict=False)
        log(f"initialised from {args.init} (its step {state['step']})")

    def save_ckpt() -> None:
        tmp = ckpt.with_suffix(".tmp")
        torch.save(
            {
                "head": {k: v.cpu() for k, v in head.state_dict().items()},
                "lora": {
                    n: p.detach().cpu()
                    for n, p in net.named_parameters()
                    if p.requires_grad
                },
                "opt": opt.state_dict(),
                "items": done_items,
                "step": step,
                "history": history,
            },
            tmp,
        )
        tmp.replace(ckpt)

    decoder = inner.model
    window: list[float] = []
    for epoch in range(args.epochs):
        order = list(range(per_epoch))
        random.Random(args.seed * 1000 + epoch).shuffle(order)
        for i, idx in enumerate(order, 1):
            item = epoch * per_epoch + i
            if item <= done_items:
                continue
            e = encoded[idx]
            logits = mm.head_logits(decoder, head, e.input_ids, args.device)
            loss = mm.loss(args.head, logits, e, args.k, args.max_span, args.matching)
            start = ((i - 1) // args.accum) * args.accum
            size = min(args.accum, per_epoch - start)
            (loss / size).backward()
            window.append(float(loss.item()))
            done_items = item
            if i % args.accum == 0 or i == per_epoch:
                scale = min(1.0, (step + 1) / max(1, args.warmup))
                for g, lr in zip(opt.param_groups, base_lrs, strict=True):
                    g["lr"] = lr * scale
                opt.step()
                opt.zero_grad(set_to_none=True)
                step += 1
                mean = sum(window) / len(window)
                history.append({"step": step, "loss": mean, "items": item})
                window = []
                extra = ""
                if args.device == "mps":
                    extra = f" mps_gb {torch.mps.driver_allocated_memory() / 2**30:.1f}"
                    torch.mps.empty_cache()
                log(f"epoch {epoch} step {step}/{total_steps} loss {mean:.4f}{extra}")
                if step % args.ckpt_every == 0:
                    save_ckpt()
    if done_items < total_items:
        raise RuntimeError("training stopped early")

    final = args.out / "final"
    final.mkdir(exist_ok=True)
    save_file(
        {k: v.detach().cpu().contiguous() for k, v in head.state_dict().items()},
        str(final / sm.HEAD_FILE),
    )
    (final / HEAD_META).write_text(
        json.dumps(
            {
                "head": args.head,
                "k": args.k,
                "max_span": args.max_span,
                "level": args.level,
                "matching": args.matching,
                "count": args.count,
            },
            indent=2,
        )
        + "\n"
    )
    net.peft_config["default"].base_model_name_or_path = base.repo
    net.peft_config["default"].revision = base.revision
    inner.name_or_path = base.repo
    inner.config._name_or_path = base.repo
    net.save_pretrained(final)
    card = final / "README.md"
    if card.is_file():
        card.unlink()
    meta = {
        "base": base.base_id,
        "data_sha256": data_sha,
        "examples": per_epoch,
        "skipped_over_k": skipped,
        "epochs": args.epochs,
        "steps": step,
        "args": {k: str(v) for k, v in vars(args).items() if k not in ("data", "out")},
        "history": history,
    }
    (final / "train_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    save_ckpt()
    log(f"saved {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
