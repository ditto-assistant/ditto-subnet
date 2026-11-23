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
| `multi_data.py` | Multi-span dataset: answer, absence (grounding) and null documents, gold at three widths |
| `multi_model.py` | Fixed buffer of K spans: `slots` and `pointer` heads, losses, decoding, set metrics |
| `train_multi.py` | LoRA plus a K-span head at one gold width, resumable |
| `eval_multi.py` | `multi`, `single` (the one-span model), `chunk --top N` and `packall`, set metrics |

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

Multi-span (span-v2.md §7):

```bash
$R/multi_data.py --seeds 101,102,103,104,105,106 --variants 2 --null-rate 0.35 --out /tmp/span2/train.jsonl
$R/multi_data.py --seeds 42,7 --out /tmp/span2/heldout.jsonl
$R/train_multi.py --head slots --level needle --k 12 --data /tmp/span2/train.jsonl --device mps --out /tmp/span2/slots-needle
$R/train_multi.py --head slots --level anchored --k 16 --data /tmp/span2/train.jsonl --device mps --out /tmp/span2/slots-anchored
$R/train_multi.py --head slots --level clause --k 12 --max-span 96 --data /tmp/span2/train.jsonl --device mps --out /tmp/span2/slots-clause
$R/eval_multi.py multi --model /tmp/span2/slots-needle/final --data /tmp/span2/heldout.jsonl --device mps --out /tmp/span2/eval/slots-needle.json
$R/eval_multi.py chunk --model base --top 3 --data /tmp/span2/heldout.jsonl --device mps --out /tmp/span2/eval/chunk-top3.json
```

`--device cuda` works the same way. On Apple silicon, the trainer keeps
gradient checkpointing on: without it, one step of about 1.6k tokens used
44.6 GB of MPS memory on an M4 Pro and macOS killed the process.

Run long jobs detached (`nohup … &`). A rerun of `train_span.py` or `train_multi.py` with the same
arguments resumes from `OUT/ckpt.pt`.
