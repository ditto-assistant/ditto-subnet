#!/usr/bin/env python3
"""Reference LoRA trainer for the SN118 model_context (CPM) track.

Trains a LoRA adapter on the scorer's own interface: the same template,
truncation and logit("yes") - logit("no") relevance score that `cpm-scorer`
uses, so what you optimise is what is scored.

Loss: a listwise softmax per training group. Each group is one case's query
with its positives (relevant memory pairs, or the expected tools) and up to
`--group-size` candidates in all. Hard negatives come first: memory pairs
carrying a distractor or forbidden span, and tools from the same integration
as a target (same name prefix). The rest are sampled at random.

    loss = logsumexp(scores) - logsumexp(scores[positives])

Data: exporter output (`docs/research/cpm/export_cpm.py --out-dir DIR`) via
`--data DIR`, or `--seeds` to generate it here. Hold out whole seeds; cases of
one seed share a haystack.

Run inside the scorer's environment:

    uv run --project ../../services/cpm-scorer python train_lora.py \\
        --seeds 101,102,103 --out adapters/my-lora

Tiny CPU smoke run (two optimiser steps):

    uv run --project ../../services/cpm-scorer python train_lora.py \\
        --seeds 101 --max-steps 2 --group-size 4 --out /tmp/cpm-lora
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from cpm_scorer import model as m
from cpm_scorer.bases import ALLOWED_LORA_TARGETS, get_base
from cpm_scorer.generate import (
    build_generate,
    default_cache_dir,
    find_datagen_dir,
    generate,
)
from cpm_scorer.labels import export_records
from cpm_scorer.metrics import distractor_ids
from cpm_scorer.screening import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA,
    MAX_LORA_RANK,
    TRACK,
    sha256_file,
    validate_manifest,
)
from cpm_scorer.tasks import SeedTasks, build_tasks

DEFAULT_TARGETS = sorted(ALLOWED_LORA_TARGETS)


@dataclass(frozen=True)
class Group:
    query: str
    texts: tuple[str, ...]
    labels: tuple[bool, ...]


def log(msg: str) -> None:
    print(
        f"[train_lora {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True
    )


def load_export(data_dir: Path) -> dict[str, dict]:
    """Exporter jsonl files grouped by seed_key into `export_records` shape."""
    by_seed: dict[str, dict] = defaultdict(
        lambda: {"pairs": [], "catalog": [], "cases": []}
    )
    for name in ("pairs", "catalog", "cases"):
        with (data_dir / f"{name}.jsonl").open() as f:
            for line in f:
                row = json.loads(line)
                by_seed[row["seed_key"]][name].append(row)
    return dict(by_seed)


def generate_records(seeds: Iterable[int], bench_version: int) -> dict[str, dict]:
    cache = default_cache_dir()
    gen = build_generate(find_datagen_dir(), cache)
    out = {}
    for seed in seeds:
        art = generate(gen, bench_version, seed, cache)
        key = f"v{bench_version}-{seed}"
        out[key] = export_records(json.loads(art.read_text()), key)
    return out


def _fill(
    rng: random.Random, chosen: list[str], hard: list[str], pool: list[str], n: int
) -> list[str]:
    seen = set(chosen)
    for cid in [*hard, *rng.sample(pool, len(pool))]:
        if len(chosen) >= n:
            break
        if cid not in seen:
            chosen.append(cid)
            seen.add(cid)
    return chosen


def groups_for_seed(
    tasks: SeedTasks, rng: random.Random, group_size: int
) -> list[Group]:
    groups: list[Group] = []
    for t in tasks.memory:
        cands = list(t.candidates)
        pos = [c for c in t.relevant if c in set(cands)]
        if not pos:
            continue
        pos = pos[: max(1, group_size // 2)]
        hard = sorted(distractor_ids(t.spans) - set(t.relevant))
        rng.shuffle(hard)
        negatives = [c for c in cands if c not in set(t.relevant)]
        chosen = _fill(rng, list(pos), hard, negatives, group_size)
        groups.append(
            Group(
                t.query,
                tuple(tasks.pair_text[c] for c in chosen),
                tuple(c in set(pos) for c in chosen),
            )
        )
    names = list(tasks.tool_names)
    for t in tasks.tools:
        pos = [n for n in t.target if n in tasks.tool_text][: max(1, group_size // 2)]
        if not pos:
            continue
        prefixes = {n.split("_", 1)[0] for n in pos}
        hard = [
            n for n in names if n.split("_", 1)[0] in prefixes and n not in t.target
        ]
        rng.shuffle(hard)
        negatives = [n for n in names if n not in set(t.target)]
        chosen = _fill(rng, list(pos), hard, negatives, group_size)
        groups.append(
            Group(
                t.query,
                tuple(tasks.tool_text[n] for n in chosen),
                tuple(n in set(pos) for n in chosen),
            )
        )
    return groups


def group_scores(net, tok, render, group: Group, device: str):
    """Relevance scores with gradients, exactly as the scorer computes them."""
    import torch

    texts = [render(group.query, text) for text in group.texts]
    ids = tok(texts, add_special_tokens=False)["input_ids"]
    lengths = [len(x) for x in ids]
    pad = tok.pad_token_id if tok.pad_token_id is not None else tok.eos_token_id
    input_ids = torch.full((len(ids), max(lengths)), pad, dtype=torch.long)
    mask = torch.zeros_like(input_ids)
    for i, row in enumerate(ids):
        input_ids[i, : len(row)] = torch.tensor(row)
        mask[i, : len(row)] = 1
    hidden = net.model(
        input_ids=input_ids.to(device), attention_mask=mask.to(device)
    ).last_hidden_state
    rows = torch.arange(len(ids), device=hidden.device)
    h = hidden[rows, torch.tensor(lengths, device=hidden.device) - 1]
    (yes,) = tok.encode(m.YES, add_special_tokens=False)
    (no,) = tok.encode(m.NO, add_special_tokens=False)
    head = net.get_output_embeddings().weight[[yes, no]]
    logits = h.float() @ head.float().T
    return logits[:, 0] - logits[:, 1]


def accum_window_size(i: int, n: int, accum: int) -> int:
    """Size of the gradient-accumulation window holding 1-based item `i` of
    `n`. Every window holds `accum` items except a shorter final one, and each
    loss is divided by its own window's size so every optimiser step averages
    its groups equally."""
    if not 1 <= i <= n or accum < 1:
        raise ValueError(f"item {i} of {n} with accum {accum}")
    start = ((i - 1) // accum) * accum
    return min(accum, n - start)


def check_lora_args(rank: int, targets: list[str]) -> str | None:
    """The scorer's LoRA limits (contract §2.2), checked before any work."""
    if not 1 <= rank <= MAX_LORA_RANK:
        return f"--rank must be in 1..{MAX_LORA_RANK}, not {rank}"
    if not targets:
        return "--targets must name at least one module"
    bad = sorted(set(targets) - ALLOWED_LORA_TARGETS)
    if bad:
        return f"--targets not allowed: {bad}; allowed: {sorted(ALLOWED_LORA_TARGETS)}"
    return None


def listwise_loss(scores, labels: Iterable[bool]):
    import torch

    pos = torch.tensor(list(labels), device=scores.device)
    return torch.logsumexp(scores, 0) - torch.logsumexp(scores[pos], 0)


def write_manifest(out: Path, base_id: str, license_id: str, attestation: str) -> str:
    files = [
        {"path": p, "sha256": sha256_file(out / p), "bytes": (out / p).stat().st_size}
        for p in ("adapter_config.json", "adapter_model.safetensors")
    ]
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "track": TRACK,
        "base_id": base_id,
        "kind": "lora",
        "files": files,
        "license": license_id,
        "training_data_attestation": attestation,
    }
    validate_manifest(manifest)
    raw = (json.dumps(manifest, indent=2) + "\n").encode()
    (out / MANIFEST_NAME).write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--data", type=Path, help="exporter --out-dir")
    src.add_argument("--seeds", help="comma-separated seeds to generate")
    ap.add_argument("--bench-version", type=int, default=18)
    ap.add_argument("--base", default="qwen3.5-0.8b")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=int, default=32)
    ap.add_argument("--targets", default=",".join(DEFAULT_TARGETS))
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--group-size", type=int, default=16)
    ap.add_argument("--accum", type=int, default=4, help="groups per optimiser step")
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--max-steps", type=int, help="stop after N optimiser steps")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--license", default="apache-2.0", choices=["apache-2.0", "mit"])
    ap.add_argument(
        "--attestation",
        default="Trained with miners/cpm-starter-kit/train_lora.py on public "
        "DittoBench cmd/generate seeds only.",
    )
    args = ap.parse_args(argv)
    targets = [t for t in args.targets.split(",") if t.strip()]
    targets = [t.strip() for t in targets]
    problem = check_lora_args(args.rank, targets)
    if problem:
        ap.error(problem)
    if args.accum < 1:
        ap.error("--accum must be at least 1")

    import torch
    from peft import LoraConfig, get_peft_model

    rng = random.Random(args.seed)
    torch.manual_seed(args.seed)
    if args.data:
        records = load_export(args.data)
    else:
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]
        records = generate_records(seeds, args.bench_version)
    groups: list[Group] = []
    for key in sorted(records):
        groups.extend(groups_for_seed(build_tasks(records[key]), rng, args.group_size))
    log(f"{len(groups)} training groups from {len(records)} seed(s)")

    base = get_base(args.base)
    base_dir = m.download_base(base)
    tok = m.load_tokenizer(base_dir)
    net = m.load_base_model(base_dir)
    render = m.RelevanceScorer(net, tok, "cpu").render  # template + truncation
    net = get_peft_model(
        net,
        LoraConfig(
            r=args.rank, lora_alpha=args.alpha, lora_dropout=0.0, target_modules=targets
        ),
    )
    dtype = torch.bfloat16 if args.device.startswith("cuda") else torch.float32
    net.to(device=args.device, dtype=dtype)
    net.train()
    inner = net.get_base_model()
    opt = torch.optim.AdamW(
        [p for p in net.parameters() if p.requires_grad], lr=args.lr
    )

    step = 0
    done = False
    for epoch in range(args.epochs):
        order = list(range(len(groups)))
        rng.shuffle(order)
        for i, gi in enumerate(order, 1):
            g = groups[gi]
            loss = listwise_loss(
                group_scores(inner, tok, render, g, args.device), g.labels
            )
            (loss / accum_window_size(i, len(order), args.accum)).backward()
            if i % args.accum == 0 or i == len(order):
                opt.step()
                opt.zero_grad(set_to_none=True)
                step += 1
                log(f"epoch {epoch} step {step} loss {loss.item():.4f}")
                if args.max_steps and step >= args.max_steps:
                    done = True
                    break
        if done:
            break

    args.out.mkdir(parents=True, exist_ok=True)
    # Record the public base, not this machine's cache path: peft writes it
    # into adapter_config.json and the README model card of a public repo.
    net.peft_config["default"].base_model_name_or_path = base.repo
    net.peft_config["default"].revision = base.revision
    inner.name_or_path = base.repo
    inner.config._name_or_path = base.repo
    net.save_pretrained(args.out)
    card = args.out / "README.md"
    if card.is_file() and str(base_dir) in card.read_text():
        card.write_text(card.read_text().replace(str(base_dir), base.repo))
    msha = write_manifest(args.out, base.base_id, args.license, args.attestation)
    log(f"saved {args.out} (manifest_sha256 {msha})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
