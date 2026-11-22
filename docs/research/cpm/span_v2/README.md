# CPM v2 span extraction: Mac mini PoC

Research code for the span-extraction proof of concept in
[`../span-v2.md`](../span-v2.md). Nothing here touches a score path: it lives
under `docs/` (release-ignored) like `export_cpm.py`, uses only public
`cmd/generate` seeds, and runs in the `services/cpm-scorer` environment.

| File | Role |
| --- | --- |
| `span_data.py` | Builds the raw-text span dataset from generator seeds (deterministic; split by seed) |
| `span_model.py` | Template, encoding, span head, decoding, metrics and bootstrap summary |
| `train_span.py` | LoRA plus span head (or `--head-only`), gradient checkpointing, resumable checkpoints |
| `eval_span.py` | `span`, `zeroshot`, `chunk` (v1 top pair) and `packall` predictors, same metrics |

```bash
R="uv run --project services/cpm-scorer python docs/research/cpm/span_v2"
$R/span_data.py --seeds 101,102,103,104,105,106 --variants 2 --out /tmp/span/train.jsonl
$R/span_data.py --seeds 42,7 --out /tmp/span/heldout.jsonl
$R/train_span.py --data /tmp/span/train.jsonl --device mps --epochs 2 --out /tmp/span/lora
$R/train_span.py --data /tmp/span/train.jsonl --device mps --epochs 2 --head-only --out /tmp/span/probe
$R/eval_span.py span --model /tmp/span/lora/final --data /tmp/span/heldout.jsonl --device mps --out /tmp/span/eval/span-lora.json
$R/eval_span.py chunk --model base --data /tmp/span/heldout.jsonl --device mps --out /tmp/span/eval/chunk-v1-base.json
$R/eval_span.py zeroshot --data /tmp/span/heldout.jsonl --device mps --out /tmp/span/eval/zeroshot-base.json
$R/eval_span.py packall --data /tmp/span/heldout.jsonl --out /tmp/span/eval/packall.json
```

`--device cuda` works the same way. On Apple silicon, the trainer keeps
gradient checkpointing on: without it, one step of about 1.6k tokens used
44.6 GB of MPS memory on an M4 Pro and macOS killed the process.

Run long jobs detached (`nohup … &`). A rerun of `train_span.py` with the same
arguments resumes from `OUT/ckpt.pt`.
