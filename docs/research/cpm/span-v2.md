# CPM v2: span extraction (draft)

Status: **draft for ML review, 2026-11-22. Experimental, 0 bps.** Nothing in
this document changes the validator, Platform, the scorer's
`cpm_score_version = 1` path, or any live setting. The v1 contract stays
[competition-v1.md](competition-v1.md). It names span extraction as v2 under a
new `cpm_score_version` (§2.4).

v1 ranks whole memories and packs them. v2 reads **raw text** and points at
the start and end of the part that answers the query, so the packet carries
that slice instead of the whole memory. This draft gives the interface, the
deterministic grading rules and the version plan. It also reports a Mac mini
proof of concept (§6), run before any spend on compute.

## 1. Task

- **Input:** the query and a raw document: memory pairs rendered as v1
  candidate text (`[{timestamp}] User: …\nAssistant: …`) and joined with blank
  lines. The document carries no pair ids and no chunk markers.
- **Output:** one span `[start, end]` of document tokens (base tokenizer),
  with at most 32 tokens. The scorer decodes the span from the model's logits,
  so a prediction is always a verbatim slice of the input.
- **Gold:** the generator's `answer_value` span (export labels,
  competition-v1 §4.2), moved to character offsets in the document, then to the
  document tokens that overlap it.

## 2. Interface (proposal)

The template is v1's with a span instruction. The query comes before the
document, so a causal decoder has read the query at every document token.

```text
system: Find the exact span of the Document that answers the Query for this user.
user:   <Query>{query}</Query>
        <Document>{document}</Document>
assistant: 
```

- **Span head.** `Linear(hidden, 2)` on the final hidden state (after the
  model's last norm) at each document token. Column 0 is the start logit and
  column 1 the end logit, computed in float32.
- **Submission.** A LoRA or a full fine-tune as in v1 §2.2, plus
  `span_head.safetensors` holding `weight [2, hidden]` and `bias [2]`. The
  manifest lists the head with its SHA-256 like any weight file. Screening
  checks the tensor names, shapes and dtype, and rejects NaN or Inf.
- **Why a head, not generation.** A head makes every prediction a verbatim
  token range, needs one forward pass per document, and is deterministic
  under the v1 rules. Generating the quote can paraphrase, so it needs fuzzy
  matching, and it costs a decode loop. The zero-shot baseline in §6 shows the
  failure.

## 3. Grading (deterministic)

**Decoding.** The scorer, not the miner, turns logits into a span:

1. Take start logits `s` and end logits `e` over document tokens only.
2. Pick `argmax over (i, j)` of `s[i] + e[j]`, with `i ≤ j < i + 32`.
3. Compute in float64 on the CPU from the float32 logits. Ties go to the
   smallest `i`, then the smallest `j`.

**Per case.** Treat the predicted and gold spans as sets of token indices `P`
and `G`.

| Metric | Rule |
| --- | --- |
| Exact match | `P == G` |
| Token precision, recall, F1 | `\|P∩G\|/\|P\|`, `\|P∩G\|/\|G\|`, harmonic mean |
| IoU | `\|P∩G\| / \|P∪G\|` |
| Contained | `G ⊆ P` (the packet holds the whole answer) |
| Tokens | `\|P\|`, charged against the budget `B = 2,048` |

**Seed score, proposed.** v2 keeps v1's components and adds span quality:

```text
seed_score_v2 = 0.45 · memory_recall@B · (1 − 0.5 · distractor_rate)
              + 0.25 · span_f1
              + 0.30 · tool_recall@8
```

`span_f1` is the mean token F1 over the seed's span cases.

**Why F1 is the main metric:**
- Exact match alone fails to separate "one token too long" from a miss.
- `contained` alone rewards returning the whole document, which `packall` in
  §6 does with a containment of 1.0 and an F1 near 0.
- F1 punishes both padding and misses.

Exact match, IoU, containment and tokens are still reported for review.

**Determinism.**
- Same rules as v1 §2.4: bf16 on the season SKU, batch 1 per document in a
  fixed order, deterministic algorithms, SDPA math.
- A re-run of one seed must give bitwise-identical predicted spans.
- Because decoding is an argmax, small numeric drift changes a span only when
  two candidate spans are near a tie, which is rarer than a ranking flip in v1.

**Anti-gaming:**
- The 32-token cap stops "return everything".
- The scorer decodes, so the miner never supplies offsets.
- Non-finite logits fail the run (`invalid_output`).
- Season seeds carry the same train-on-test exposure as v1, which is
  open-questions Q1, and the fix is the same.

## 4. Version plan (draft; nothing here is scheduled)

1. **Labels.** The generator exports gold spans natively: `answer_span`
   offsets per evidence pair, plus `support` and `temporal_anchor` spans where
   a family has them. Today's `answer_value` spans are found by string
   matching in the exporter (competition-v1 §4.2), which is good enough for
   this PoC. It is not a contract.
2. **Scorer.**
   - A `cpm_scorer/span.py` path runs under `cpm_score_version = 2` and writes
     `components.span_f1` and the other span metrics.
   - The v1 path is untouched.
   - A run row already carries `cpm_score_version` (competition-v1 §5.1).
3. **Screening.** Accept `span_head.safetensors` (names, shapes, dtype, NaN
   and Inf checks). A v1 submission without a head scores 0 on `span_f1` and
   stays valid on v1.
4. **Platform.**
   - Store v2 runs beside v1 runs, and show a separate v2 board.
   - Add a `score_version` setting: `1` (default), `dual` (score both), `2`.
   - Before any switch, run at least one full season in `dual`, in shadow.
5. **Gate.**
   - v2 replaces v1 only after a P0-S-style check: score spread larger than
     seed noise across ≥ 3 seeds.
   - It also needs an owner decision.
   - The track stays at 0 bps throughout, and the validator does not change
     until then.

## 5. Open questions for ML review

1. **Multiple spans.** Most memory cases have 3–4 evidence pairs, and many
   need two facts. Options:
   - k spans with a packing budget, graded as needed-fact recall@B over spans;
   - one span per evidence pair.

   The PoC covers single-span cases only.
2. **What "useful" means.** The bare answer value (`erin@natco.com`) is the
   minimum. A reader often also needs the anchor ("as of June 22") or the
   superseding statement. Should gold widen to the support clause, or should v2
   add the `temporal_anchor` and `supersedes` span types from memo §I.2?
3. **No answer.** Absence cases (left out of v1 and of this PoC) need a null
   span, for example a no-answer logit at the assistant position, or the
   decide head.
4. **Weights.** Are `0.45 / 0.25 / 0.30` sensible, or should span F1 replace
   memory recall once spans are packed under B?
5. **Decoder limits.** A causal model's start logit at token i cannot see
   tokens after i. A bidirectional pass, or a second pass over a short window,
   may be needed for longer answers.

## 6. Mac mini proof of concept

Code: [`span_v2/`](span_v2/). Unofficial: fp32 on Apple MPS (M4 Pro), small
n, not a season score.

**Setup.**

- **Data** (`span_data.py`, bench v18, deterministic):
  - Train: seeds 101–106, 330 cases × 2 documents = 660 documents
    (sha256 `e356a7a5…`).
  - Held out: seeds 42 and 7, 1 document per case, **n = 107**
    (`d7fbeb65…`; 55 + 52).
  - Documents have a median of 16 pairs and about 1,500 document tokens, and
    78% contain a hard-distractor pair. Gold spans are a median of 5 tokens.
- **Span model:**
  - Qwen3.5-0.8B with a LoRA (r 16, α 32, every allowed target) and the
    span head.
  - lr 1e-4 for the LoRA and 1e-3 for the head, 4 documents per step,
    2 epochs, 330 steps.
  - 67 min at 5.2 GB, with gradient checkpointing. The loss went from 7.7 to
    0.006.
- **Head-only probe:** the same, with the base frozen; 13 min.
- **Precision and statistics:**
  - float32 throughout; decoding in float64 on the CPU.
  - Intervals are 95% percentile bootstrap over cases (2,000 resamples, seed
    20261122).

**Held-out results** (n = 107; tokens are charged against `B = 2,048`):

| Predictor | Exact match | Token F1 | IoU | Contained | Tokens (share of B) |
| --- | --- | --- | --- | --- | --- |
| **LoRA + span head** | **0.701** [0.617, 0.776] | **0.747** [0.669, 0.825] | **0.738** [0.658, 0.815] | **0.720** [0.636, 0.804] | **5.3 (0.26%)** |
| Head-only probe (base frozen) | 0.505 [0.411, 0.598] | 0.541 [0.448, 0.634] | 0.536 [0.442, 0.627] | 0.514 [0.421, 0.607] | 5.2 (0.25%) |
| Base, zero-shot quote | 0.000 | 0.041 [0.018, 0.068] | 0.027 [0.011, 0.046] | 0.103 [0.047, 0.159] | 14.7 (0.72%) |
| v1 top chunk, base | 0.000 | 0.052 [0.039, 0.067] | 0.028 [0.021, 0.037] | 0.486 [0.383, 0.579] | 134.5 (6.6%) |
| v1 top chunk, v1 LoRA | 0.000 | 0.055 [0.041, 0.069] | 0.030 [0.022, 0.038] | 0.533 [0.439, 0.626] | 137.4 (6.7%) |
| Pack the whole document | 0.000 | 0.007 [0.006, 0.008] | 0.003 [0.003, 0.004] | 1.000 | 1,493 (72.9%) |

Per seed, the LoRA's exact match is 0.727 on seed 42 (n 55) and 0.673 on
seed 7 (n 52).

The v1 rows grade v1's own interface fairly. v1 picks whole memories, so it
never isolates the answer: its top memory holds the answer about half the
time at about 135 tokens. The "v1 LoRA" is the 0.778 v1 PoC adapter.

**Reading the numbers:**

- **The span head works.** It isolates the exact answer in 70% of held-out
  documents with about 5 tokens. The best v1 top chunk holds the answer in
  53% of documents at about 26× the tokens.
- **The LoRA matters.** Against the frozen-base probe, compared case by case:
  - the LoRA alone is exact in 25 cases, the probe alone in 4, both in 50 and
    neither in 28;
  - the exact-match gain is +0.196 [0.103, 0.290].

  The frozen base already gets half the answers from a trained linear head.
- **Zero-shot quoting fails.** The unmodified base copied a findable quote in
  57% of cases and never the exact answer. It tends to quote whole sentences,
  sometimes from the wrong memory. This is why §2 uses a head and not
  generation.
- **Where the LoRA misses** (32 cases):

  | Prediction landed in | Cases |
  | --- | --- |
  | Another evidence pair (often the stale or superseded value) | 13 |
  | Partial overlap with the gold span | 8 |
  | A hard-distractor pair | 7 |
  | The answer pair, but the wrong words | 3 |
  | An unrelated pair | 1 |

  The weakest families are the program and supersession ones:
  `v13-personal-program` 0.30 (n 10) and `v13-open-program` 0.50 (n 20). That
  is where open question 2 (temporal anchor and supersedes spans) bites.
- **Determinism.** Re-running the LoRA eval gave identical predicted spans:
  `preds_sha256` `0639a44e35db…` both times. That is on one MPS host in fp32.
  Cross-host, bf16 CUDA determinism is still untested
  ([open-questions.md](open-questions.md) Q5).

**Caveats:**

- This is not a season score. n = 107 single-span cases from 2 seeds, fp32 on
  Apple MPS.
- The data comes from the same public generator the training seeds came from
  (Q1 applies).
- The documents are about 1,500 tokens, not a user's full history.
- It covers single-span, answerable cases only; multi-span and abstain cases
  are §5.
- Gold labels come from the exporter's string matching, not from native
  generator spans (§4.1).

**Next, before any spend:**

- multi-span packing under B;
- a null span for absence;
- widening gold to the support clause;
- the same evaluation in bf16 on one CUDA host (phase B0 of
  [bittensor-compute.md](bittensor-compute.md)).
