# CPM starter kit (model_context track, shadow)

Reference tooling for the SN118 `model_context` track: you submit a small
open-weight model that ranks what goes into Ditto's context packet. The
contract is `docs/research/cpm/competition-v1.md` (PR #3572). The track runs
**in shadow at 0 bps**: it is scored and visible, and pays nothing.

The scorer is `services/cpm-scorer`. Everything here runs in its environment,
so practice and training use the exact template, truncation and score the
scorer uses.

```bash
cd services/cpm-scorer
uv sync --extra cpu        # laptops and CI; the GPU host uses --extra gpu
```

## The task in one paragraph

Your model is a pointwise relevance scorer. For each (query, candidate) pair
the scorer renders a fixed chat prompt (thinking disabled) and reads
`logit("yes") - logit("no")` at the first assistant position. Memory
candidates are ranked and packed into a 2,048-token budget; tools are ranked
and the top 8 kept. The score per seed is

```text
0.6 · memory_recall@B · (1 − 0.5 · distractor_rate) + 0.4 · tool_recall@8
```

averaged over the season's three seeds. Bases: `qwen3.5-0.8b`
(`Qwen/Qwen3.5-0.8B`) and `qwen3.5-2b` (`Qwen/Qwen3.5-2B`), LoRA (rank ≤ 64,
attention, MLP and Gated DeltaNet projections) or a full fine-tune.

## Practice

`cpm-scorer score` is the scorer's own code path, offline. It builds
`cmd/generate` from `research/dittobench-datagen` (needs Go), generates the
seeds, labels them with the exporter logic, and scores.

```bash
cd services/cpm-scorer
# the unmodified base, a quick look (first 3 memory and 3 tool cases)
uv run cpm-scorer score --model base --base qwen3.5-0.8b \
    --seeds 42 --limit-cases 3 --device cpu
# your adapter or full model directory
uv run cpm-scorer score --model ../../miners/cpm-starter-kit/adapters/my-lora \
    --base qwen3.5-0.8b --seeds 42,7,1234 --device cuda
# a pushed Hugging Face repo at a pinned commit
uv run cpm-scorer score --model you/your-cpm@<40-hex-commit> --base qwen3.5-0.8b
```

The output is the same JSON the scorer posts to Platform: `score`,
`components`, `per_seed`, `tokens`, `cpm_score_version` and `determinism`.

- **Full runs need a GPU.** One full seed is about 122,000 (query, candidate)
  pairs on v18: 107 memory cases × about 505 candidates (54,051 pairs) plus
  47 tool cases × 1,454 tools. On CPU, use `--limit-cases`.
- **Scores are only comparable at the same settings.** A `--limit-cases` run
  is a smoke test, not a season score.
- **Determinism.** The scorer re-runs the first seed and fails a run whose
  scores are not bitwise identical. `--skip-determinism` halves practice time.

## Train a LoRA

`train_lora.py` trains on exporter data with a listwise softmax loss over each
case's candidates. Each group holds the positives and hard negatives first
(distractor-span memories, and same-integration tools), then random
negatives.

```bash
cd miners/cpm-starter-kit
# tiny CPU smoke run: two optimiser steps
uv run --project ../../services/cpm-scorer python train_lora.py \
    --seeds 101 --max-steps 2 --group-size 4 --out /tmp/cpm-lora
# a real run (GPU)
uv run --project ../../services/cpm-scorer python train_lora.py \
    --seeds 101,102,103,104,105 --device cuda --out adapters/my-lora
```

Or train from exporter output (`docs/research/cpm/README.md`):

```bash
uv run --project ../../services/cpm-scorer python train_lora.py \
    --data /tmp/cpm/out --device cuda --out adapters/my-lora
```

**Hold out whole seeds.** Cases from one seed share a haystack, so a
case-level split leaks evidence. Do not practise on the seeds you trained on.

The trainer writes `adapter_config.json`, `adapter_model.safetensors` and
`cpm-manifest.json` (contract §2.3) with per-file SHA-256. The manifest's
`training_data_attestation` is shown to reviewers; set it with
`--attestation`.

## Submit

Submission goes through `ditto model-context {manifest, commit, submit}`
(contract §6.2), which lands with the miner CLI PR. The order is: push to a
private Hugging Face repo, write the on-chain commitment
`dcpm1:<manifest_sha256>`, make the repo public, then submit the full 40-hex
revision. Until then, the scorer screens a local directory exactly as Platform
will: no `.py` or pickle files, safetensors only, LoRA rank and targets,
NaN/Inf, size cap, and the manifest hashes.
