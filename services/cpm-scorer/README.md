# cpm-scorer

Scorer for the SN118 `model_context` (CPM) track, `cpm_score_version = 1`.
Contract: `docs/research/cpm/competition-v1.md` §2, §4 and §6.1. Shadow only:
no validator or Platform image imports this package.

```bash
uv sync --extra cpu    # CPU build of torch (CI, laptops)
uv sync --extra gpu    # default PyPI torch, the CUDA build on Linux (scorer host)
uv run pytest -q
```

## Commands

- `cpm-scorer score --model base|DIR|HF_REPO@REVISION --base qwen3.5-0.8b
  --seeds 42,7,1234 [--bench-version 18] [--device cpu|cuda] [--limit-cases N]`
  prints the run JSON.
- `cpm-scorer serve --platform URL` leases `GET /model-context-scorer/reference-work`
  then `/work`, scores, and posts `/results` or `/fault`. The bearer comes from
  `DITTO_MODEL_CONTEXT_SCORER_TOKEN` or the file named by
  `DITTO_MODEL_CONTEXT_SCORER_TOKEN_FILE`, and is never logged.

`--generate-bin` pins the `cmd/generate` binary. Without it the scorer builds
`research/dittobench-datagen` with `go build` into `~/.cache/cpm-scorer`,
keyed by the generator source digest.

## Modules

| Module | Role |
| --- | --- |
| `bases.py` | allowed bases at full revision SHAs, allowed LoRA targets |
| `labels.py` | copy of `docs/research/cpm/export_cpm.py` label logic; `tests/test_labels.py` asserts identical output |
| `tasks.py` | v1 case selection, fixed order, candidate text |
| `model.py` | text-only Qwen3.5 load, LoRA merge, full weights, relevance score, determinism |
| `metrics.py` | recall@B with skip-and-continue packing, distractor rate, tool recall@8, seed score |
| `screening.py` | manifest schema, per-file SHA-256, canonical `weights_sha256`, LoRA limits, NaN/Inf, size cap, forbidden files |
| `serve.py` | Platform client and lease loop |

## Implementation notes

- **Text-only base.** `Qwen3_5ForCausalLM.from_pretrained` on the
  vision-language checkpoint loads the 320 text tensors
  (`model.language_model.*`) with no missing keys, and drops the vision tower
  and the MTP head. `"yes"` (9405) and `"no"` (2083) are single tokens, and
  the chat template's `enable_thinking=False` emits an empty think block.
- **Pinned kernels.** The Gated DeltaNet short convolution runs through
  `model.shift_causal_conv1d` on every device. On CPU, torch's depthwise
  `conv1d` is about 200 times slower. The scorer refuses to run when `fla`,
  `causal_conv1d` or `kernels` is installed, because those change numerics.
- **Determinism.** Batch 32, fixed order, right padding, deterministic
  algorithms, SDPA math backend; bf16 on CUDA and float32 on CPU. LoRA is
  merged in float32 before the cast.
- **`weights_sha256`** framing is in the `screening.py` docstring.
- **Season seeds** are uint64; `cmd/generate -seed` is int64, so seeds
  ≥ 2^63 pass as their two's-complement value.
