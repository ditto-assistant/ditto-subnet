# What the 474/500 training overlap means

**The overlap is in Ditto's small learned memory-ranking model, not a claim
about Luna's training.** Giving a memory system the benchmark's saved
conversations is expected: those are the memories it must search. Separately,
Ditto's repository contains supervised ranking examples for 474 of the same
500 examination questions, with labels identifying relevant memories. That
second use means the complete system was not evaluated on wholly held-out
questions.

The distinction is:

- Expected input: saved conversations → memories and subjects → retrieval.
- Learned component: question embeddings + candidate relevance labels →
  Ditto's ranking-weight MLP → which memories retrieval prioritizes.
- Reader: retrieved context + question → hosted `openai/gpt-5.6-luna` → answer.

This audit did not inspect Luna's provider training corpus and supplies no
evidence that Luna was trained or fine-tuned on these questions. It also did
not train or tune any model. The ranking MLP has 216,905 float32 parameters
including an unused scale-mode sentinel, not a language-model answer head.
Training uses question embeddings, auxiliary features, candidate signals and
relevance grades; it is not a table of final answers or an operation that
fine-tunes Luna.

## What is directly verified

The [reproducible evidence audit](results/2026-09-13-ditto-retrieval-training-lineage.json)
compares `pkg/services/retrieval/training/training_set.jsonl` against the pinned
cleaned LongMemEval-S file. It finds:

| Observation | Count |
| --- | ---: |
| Evaluation questions | 500 unique IDs |
| Ranking-training input rows | 474 unique IDs |
| Training IDs found in evaluation | 474/474 |
| Training query text exactly equal to corresponding evaluation question | 474/474 |
| Training IDs absent from evaluation | 0 |
| Evaluation IDs absent from this training input file | 26 |

All 474 rows record `request_path=synthesize_oracle`. Across their candidate
lists there are 888 positive relevance grades (`2`) and 4,332 zero grades.
The checked-in `synthesize_from_oracle.py` derives positives from the oracle's
`has_answer` annotations, embeds the examination question, and computes the
candidate signals. `train.py` then optimizes an ApproxNDCG relevance-ranking
loss. This is supervised use of evaluation questions/labels, not merely loading
the conversations that the memory benchmark requires at inference.

The overlap by category is 72/78 knowledge-update, 125/133 multi-session,
51/56 single-session-assistant, 30/30 preference, 64/70 single-session-user,
and 132/133 temporal-reasoning questions.

## The model.bin lineage is now stronger than the earlier caveat

The previous retest documentation established training-file overlap but left
the checkpoint-to-export link unverified. This audit closes that specific gap:

1. Backend commit `20545326e7b29e7b279e3a6923e186c8b0629dcf`
   ([PR #1086](https://github.com/ditto-assistant/backend/pull/1086), May 14)
   introduced the training input, `model.pt`, metrics, exporter and updated
   `model.bin` together. Its commit description explicitly records training the
   v2 weights on the synthesized 474-example LongMemEval ranking set.
2. The current training input, checkpoint and model bytes still match that
   introducing commit. The metadata sidecar names `model.pt` as the source.
3. An independent restricted checkpoint reader reconstructs all 15 float32
   tensors and applies the documented exporter ordering/format entirely in
   memory. Its result is **byte-for-byte identical** to `model.bin`, including
   SHA-256. No PyTorch installation, `torch.load`, training, forward inference,
   or backend exporter execution was needed.
4. Checkpoint epoch 47 and validation NDCG@10 `0.8222300727316674` exactly match
   the best entry in the checked-in 50-epoch metrics. This is ranking-validation
   NDCG, not the end-to-end QA score.
5. `mlp.go` embeds `model.bin`; the memory store uses `PredictV2` to obtain
   ranking weights. Both retained September 13 reader logs record successful
   loading with 15 tensors, 17 auxiliary inputs and 8 outputs. The frozen ON
   report records `weights_sha=learned:46d34091333706c841b6e0f6b35b2bf9f5f89ac0fca692720ce9c22cc795138c`.

Key artifact hashes:

| Artifact | SHA-256 |
| --- | --- |
| Ranking input `training_set.jsonl` | `b3c02a73429175ce1b6d07c5f1a5972c8ced4f49bc9d6431831eb59a5cc6cb7d` |
| Checkpoint `model.pt` | `00b2a1301635a8f5eae61a5831244602b23387f933c4dca476f3f7838a4e51b9` |
| Embedded/reconstructed `model.bin` | `46d34091333706c841b6e0f6b35b2bf9f5f89ac0fca692720ce9c22cc795138c` |
| Original ON report | `859a118af975b219de3b73809efd648832291dadae590fbaf129e0f172028c18` |

The ON run is
`20260913-055526-1b5755609b3ea95660fdba289e6a747adb8c5dae`, with all 500 per-case
answer models recorded as `openai/gpt-5.6-luna`. Its report hash identifies the
unchanged original artifact. One nuance: `WeightsSHA()` hashes a relative file,
not live predictor memory. The practical runtime binding therefore rests on
that field **together with** the frozen clean-build/source provenance,
`go:embed`, and successful-load logs—not that field alone.

This is strong repository and byte-level evidence tying the evaluated ranker
to the documented LongMemEval-trained checkpoint. It is more than a coincidental
file sitting beside unrelated production weights.

## What remains unknown

The historical training report says **80/20** train/validation, but the committed
`train.py` calls `split(full.rows)` with `val_frac=0.1` and shuffle seed 1234:
its default is **427 training rows / 47 validation rows**. There is no CLI split
override. The checkpoint records architecture, selected epoch and validation
metric, but no training-input hash, actual split IDs, optimizer state, run
command, or complete random-state provenance. We have not replayed the training.

Consequently, 474 is the proven **training-workflow input overlap**, not a claim
that gradients were taken on every one of those 474 questions. The exact
historical gradient-training versus validation partition cannot be established
from the retained artifacts alone. Validation examples also influenced best
checkpoint selection under the committed script; they are not an untouched
final test set. The contradictory split documentation should not be silently
resolved by choosing whichever number sounds better.

## What this does—and does not—change about the scores

The recorded **isolated LongMemEval-S, fully seeded and dreamed** results—
67.2% OFF and 72.4% ON, with `openai/gpt-5.6-luna` at `medium` reasoning and
`google/gemini-3.1-flash-lite` as the built-in QA judge—remain mechanically
audited observations. The [frozen final results](ditto-full-memory-results-2026-09-13.md)
record both run IDs and complete conditions. Overlap does not make the observed answers disappear,
and it does not show that the measured difference was caused by memorization.
It does prevent describing these as clean held-out generalization estimates
for the complete memory system. The prior flawed/unblinded historical condition has
additional, separate limitations. No old source, fixture, result or score is
rewritten by this clarification.

Likewise, the 26 IDs absent from the input file are not automatically a fair new
test set: 21 are abstention IDs, so the remainder is highly selected. All 26
full evaluation haystacks share some source sessions with the 474 matched
evaluation cases (420 shared source session IDs in total). That comparison is
between evaluation haystacks; it does **not** prove every shared session entered
the smaller oracle training fixture. It does show why a question-ID subtraction
alone is not a sufficient history-disjoint holdout design.

## A fair next evaluation design

Keep the existing 500-case set as an explicitly exposed engineering regression
set. Do not retrain on its current failures and relabel the next score held-out.
For a generalization claim, predeclare a fresh test split with conversation/user
groups and source-session lineage separated from ranking training and model
selection. Reused sessions should stay within one split, not leak across
different question IDs. Check ID overlap, normalized question text and source
history hashes before freezing the experiment.

Either train the small ranker only on independently sourced development data,
or use a clearly named fixed/heuristic-ranking ablation. Neither changes what
the hosted reader was trained on. Record training-input hashes, exact split IDs,
all training/model-selection commands and seeds, selected checkpoint, export
hash and artifact ancestry. Freeze code, weights, prepared memories, reader,
judge and evaluation policy before revealing test judgments. Use repeated,
matched runs to characterize provider variability; retain failures and all
variants, and keep tuning/model-selection results separate from the final test.
No such retraining or fresh paid evaluation was performed by this audit.

## Performance RCA evidence

The [separate graph RCA](ditto-subject-graph-rca-2026-09-13.md) records the
retained timeout evidence, failed intermediate prototype, scoped-query rewrite
and database-only 500-user parity/performance probes. Those measurements are
not a new accuracy run and do not resolve this training-overlap limitation.

## Reproduce this audit

Use the source containing the retained artifacts above and the pinned cleaned
dataset. The helper does only local reads plus creation of a new mode-0600
aggregate evidence file. It emits no question text, candidate IDs, embeddings,
checkpoint weights, provider IDs or credentials.

```sh
python3 services/dittobench-api/integrations/longmemeval/audit_retrieval_training_lineage.py \
  --backend /path/to/backend \
  --dataset /path/to/longmemeval_s_cleaned.json \
  --output /new/path/retrieval-training-lineage.json
```

The helper requires the pinned dataset digest and frozen retest model digest,
rejects duplicate IDs and unsupported checkpoint globals/storage layouts, and
compares the reconstructed export bytes before reporting a match. Its evidence
records script/artifact hashes and the distinction between default split counts
and proven historical partitioning. The old task workspace remains read-only;
this RCA work does not alter production miner scoring or perform inference.
