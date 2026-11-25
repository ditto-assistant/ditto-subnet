# CPM: open questions for ML review

Status: open, 2026-11-20. The CPM (`model_context`) track is built and
deployed with `mode = off`. It pays nothing until an owner-approved step after
P0-S ([competition-v1.md](competition-v1.md) §7, §9). These are the questions
where ML judgement changes the design. Each lists the evidence, the current
behaviour and the options we see. Please comment inline on this file.

Background: [competition-v1.md](competition-v1.md) (the contract),
[bittensor-compute.md](bittensor-compute.md) (where the scorer runs),
`services/cpm-scorer/`, `miners/cpm-starter-kit/`, and memo Part 0 in
[../sn118-model-competition.md](../sn118-model-competition.md).

## Q1. Within-season train-on-test (highest priority)

**Evidence.** Season seeds are
`sha256("dcpm-season:{season}:{k}:{season_open_block_hash}")` (§4.1). The
generator and the label exporter are public (§4.2). A submission screened
mid-season is queued straight into the current season. Platform's
`accept_screened` calls `enqueue_submission_run(session, row,
season.season)`, and nothing compares `commitment_block` with the season's
`open_block` (`apps/platform/ditto/api_server/model_context_competition.py`).

**Consequence.** Once a season opens, anyone can generate its exact cases and
gold labels, train on them, and submit. That model is scored on the data it
was trained on until the next rotation re-scores it on fresh seeds (§4.1).
During a season, the board and the ledger can reward memorising the test
set, not generalisation. This is distinct from the policy-legal overfit to the
generator distribution: here the test cases themselves are known.

**Options.**
1. Score a submission only on seasons that open after its commitment block.
   A mid-season submission waits for the next rotation. This costs up to
   `season_days` of latency.
2. Derive the seeds from a block hash after a commitment cutoff
   (commit-then-reveal). Every submission committed before the cutoff is
   scored together, and seeds reveal only after that.
3. Keep public seeds for practice, and add a private held-out seed per season
   that counts toward the score and is revealed at season close.

**Ask.** Which option, and whether three seeds per season is enough once the
seeds are hidden (see Q2).

## Q2. Score noise against spread (P0-S not run)

- **Score.** The mean over 3 seeds of
  `0.6 · recall@2048 · (1 − 0.5 · distractor_rate) + 0.4 · tool_recall@8`
  (§4.4).
- **Floor.** The unmodified base + `floor_margin` 0.01 (§4.5).
- **Payment gate.** None of the spread is measured. Emissions wait on P0-S
  showing that the spread is larger than the noise.

**Ask:**
- Is 3 seeds enough?
- Is a 0.01 margin above the base meaningful, or inside seed noise?
- What dethrone margin should rank 1 need?

The board currently ranks by score, then by commitment block, with no margin.

## Q3. Copying and leader fine-tunes

- **The gap.** Weights are public at submit (§3.1), and only exact
  `weights_sha256` duplicates are rejected (§3.3). Near-duplicate detection
  (behavioural and ΔW distance) is v2 and is only logged in shadow.
- **The attack.** A miner can take the public leader, fine-tune it lightly
  and outrank it on score.

**Ask:**
- Which near-duplicate measure is robust enough to enforce before the track
  pays: ΔW norm per layer, LoRA subspace overlap, or behavioural agreement on
  held-out cases?
- Alternatively, should rank 1 need a margin over any model it descends from
  (the same idea as #3309 for the harness track)?

## Q4. The v1 task and metric

- **Interface.** A pointwise yes/no relevance logit (§2.4), on Qwen3.5
  0.8B/2B (vision-language checkpoints used text-only). Candidates are
  truncated to 768 tokens and queries to 256. Packing is greedy to 2,048
  tokens of the base tokenizer.
- **Gold labels.** `v10_evidence_pair_ids` from the public generator
  (`export_cpm.py`).
- **Tool prerequisites.** The tool cases' `prerequisite_pairs` hold 56% of the
  relevant memory ids on v18 seed 42 (§4.2).

**Ask:**
- Does a pointwise interface cap what miners can show compared with a
  listwise one? The starter kit already trains listwise
  (`miners/cpm-starter-kit/train_lora.py`).
- Are the 0.6/0.4 and 0.5 weights sensible?
- Does the 56% prerequisite share let a model score by spotting the
  prerequisite pattern rather than by judging relevance?
- Does the 768-token cut hide evidence in long pairs?

## Q5. Determinism and portability of the scorer

The scorer needs bitwise-equal scores for the same weights, seeds and GPU SKU
(§2.4). Quorum in [bittensor-compute.md](bittensor-compute.md) §4.2 depends
on that holding across machines, not just across re-runs on one host.

**What the scorer pins today** (`services/cpm-scorer/cpm_scorer/model.py`):
- `torch.use_deterministic_algorithms(True)` and
  `CUBLAS_WORKSPACE_CONFIG=:4096:8`;
- SDPA's math backend, in bf16;
- a pure-torch Gated DeltaNet path. `fla`, `causal_conv1d` and `kernels` are
  refused because they change numerics, and the short convolution runs
  through `shift_causal_conv1d`.

**Ask:**
1. Will bits match across two L40S hosts with different drivers? The release
   image needs CUDA 13.2 (driver r595), but every L40S on Lium runs r580 (CUDA
   13.0). That means forward-compat libraries, or a rebuild against CUDA 13.0
   wheels. Either way, phase B0 must measure it.
2. Is the pure-torch DeltaNet path fast enough? The run time per submission is
   unmeasured, and it sets the compute cost.
3. Audits on another SKU compare scores within `audit_tolerance` (proposed
   1e-3). Recall moves in discrete steps: one packing flip changes
   recall by `1/|relevant|` for that case, divided by the number of cases. So
   1e-3 may be too tight for cross-SKU bf16. What tolerance, or which
   per-component check, would you use?

## Q6. Starter kit and the floor

The starter kit trains a LoRA with a listwise softmax loss on exporter data
(`miners/cpm-starter-kit/train_lora.py`). The contract also promises a
baseline adapter (§6.3), but **the kit does not ship one yet**: it holds only
the README and the trainer. Nobody has trained the kit on a GPU, so we do not
know whether it clears `floor(base) + 0.01` on fresh seeds.

**Ask:**
- Should we train and publish the baseline before season 1?
- Should that baseline, rather than the unmodified base, be the "beat this"
  floor?
- Are the trainer's defaults sensible: the listwise loss, the
  gradient-accumulation windows and the LoRA targets?
