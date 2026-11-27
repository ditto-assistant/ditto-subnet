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
- **Multi-span buffer (§7).** The span head generalises to a fixed buffer of
  K `(start, end)` tuples, padded with `(-1, -1)`. An all-empty buffer means
  "no answer": nothing in the document is relevant. The PoC's winning form is
  `Linear(hidden, 2K)` (K = 12 "slots"). Each slot scores the document tokens
  plus a null position, the last input token, which has read everything. The
  head file is then `weight [2K, hidden]`, `bias [2K]`, and the manifest states
  K. The best form so far (§9) adds a count head: K + 1 more columns at the
  null position that predict how many spans to keep, so "no answer" is a
  count of 0 (`weight [3K + 1, hidden]`).
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

**Multi-span decoding (slots).**

1. Slot k takes the best span that starts after slot k−1's end.
2. If the null score (start and end logits at the null position) is at least
   as high as that span's score, slot k is null. The first null slot ends the
   buffer.
3. The result is in document order and has no overlaps.

In the set-matched variant (§8), every slot whose best span beats null
proposes it, and proposals fill the buffer by score, without overlaps. In both
variants a span may not cross a memory boundary. The null margin is a fixed
decoding constant, published with the season.

**Per case.** Treat the predicted and gold spans as sets of token indices `P`
and `G`. For a buffer, `P` and `G` are the unions over spans. Add span-level
exact F1, the share of gold spans contained, and, on documents with empty
gold, whether the buffer is empty.

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

Questions 1 to 3 have first answers from the PoC in §7.

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

**Next, before any spend:** multi-span, a null output and wider gold are in
§7. The same evaluation in bf16 on one CUDA host (phase B0 of
[bittensor-compute.md](bittensor-compute.md)) waits for a CUDA host.

## 7. Multi-span, "no answer" and wider gold (Mac mini PoC, part 2)

Code: `span_v2/multi_*.py`. Same caveats as §6: fp32 on Apple MPS, small n,
not a season score.

**Task.** Spans are relevance, not only the needle. Given the query and a raw
document, return every span that is relevant to the query, as a fixed buffer
of K = 12 `(start, end)` tuples. Return an empty buffer only when nothing in
the document is relevant.

**Data** (`multi_data.py`, bench v18, deterministic). There are three kinds of
document:

| Kind | Gold |
| --- | --- |
| `answer` | Every `answer_value` occurrence inside the case's evidence pairs. List and ordered answers give 2–8 spans. |
| `absence` | Every occurrence of the case's `grounding_tokens` inside evidence pairs: the facts a grounded decline should cite. These are relevant spans, not "no answer". |
| `null` | Empty. The case's query over a same-seed document with all of the case's evidence removed, plus any pair holding an answer, distractor, forbidden or grounding string, or a rare query term (document frequency < 2%). |

- **Train:** seeds 101–106, 2 variants per case, null documents at a rate of
  0.35. That gives 1,605 documents (950 answer, 144 absence, 511 null),
  sha256 `649913ab…`.
- **Held out:** seeds 42 and 7, one document per case plus one null document
  per case. That gives 427 documents (sha256 `28e89b5c…`):
  - 107 single-answer;
  - 46 multi-answer;
  - 24 absence;
  - 250 null.
- **Skipped:** 60 held-out answer cases whose answer never appears verbatim in
  evidence (computed numbers and sums), 7 that overflow the budget or K, and
  6 absence cases without grounding hits.

**Models.**
- Qwen3.5-0.8B with a LoRA (as in §6), 1 epoch, 402 optimiser steps of 4
  documents.
- About 2.7 h each, with two runs sharing the GPU, at 5.2 GB each.
- Gold spans are assigned to slots in document order.

**Two ways to fill the buffer, one forward pass each:**

- **`slots`**: `Linear(hidden, 2K)`. Each slot has start and end logits over
  document tokens plus the null position. Decoding is as in §3.
- **`pointer`**: `Linear(hidden, 1 + 32)`. Every token gets an "a relevant span
  ends here" logit (> 0 to fire) and a pointer back to the start. The best K
  non-overlapping spans fill the buffer. It is meant to sidestep the causal
  limit (§5.5) by locating a span at its end token, where the whole span has
  been read.

**Held-out results, needle gold.** "Positive" is the 177 documents with gold
spans; null is the 250 without. Brackets are 95% bootstrap intervals over
documents. "Single EM" is the share of the 107 single-answer documents where
the buffer is exactly the gold span.

| Predictor | Token F1 (positive) | Span F1 | Gold spans contained | Single EM | Multi F1 (n 46) | Absence F1 (n 24) | Null correct (n 250) | Tokens |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **slots, K = 12** | **0.644** [0.58, 0.70] | **0.612** | **0.620** [0.56, 0.68] | **0.692** | **0.595** | **0.413** | 0.876 [0.83, 0.92] | 7.2 |
| pointer | 0.389 [0.32, 0.45] | 0.257 | 0.289 | 0.215 | 0.464 | 0.224 | 0.996 | 3.8 |
| §6 single-span model | 0.528 [0.46, 0.59] | 0.454 | 0.476 | 0.673 | 0.278 | 0.100 | 0.000 | 5.0 |
| v1 top-1 memory (v1 LoRA) | 0.050 | 0 | 0.507 | 0 | 0.021 | 0.082 | 0.000 | 218 |
| v1 top-3 memories (v1 LoRA) | 0.035 | 0 | 0.774 [0.72, 0.83] | 0 | 0.022 | 0.089 | 0.000 | 446 |
| Pack the whole document | 0.010 | 0 | 1.000 | 0 | 0.012 | 0.024 | 0.000 | 1,500 |

Per seed, the slots model's token F1 is 0.648 on seed 42 and 0.640 on seed 7,
and its null-correct rate is 0.888 and 0.864. Re-running both models gave
identical predicted buffers (`preds_sha256` `33e736737be9…` for slots).

**Reading the numbers:**

- **The fixed buffer of slots works, and adding spans costs nothing on single
  answers.**
  - On the 107 single-answer documents, its exact match (0.692) equals the §6
    model's (0.673): the paired difference is +0.02 [−0.07, 0.12].
  - Over all positive documents it beats the §6 model by +0.116 token F1
    [0.055, 0.177] and +0.144 gold spans contained [0.077, 0.211].
  - The gain comes from multi-answer (0.595 against 0.278) and absence
    documents (0.413 against 0.100).
- **"No answer" is learnable.** On documents with nothing relevant, the slots
  model returns an empty buffer 87.6% of the time, leaking 0.8 tokens on
  average. Every other predictor always returns something. Of its 31 false
  positives, 13 are absence cases where it picked a near-miss person or
  company. The null filter is a heuristic, so some of those documents may
  really hold a related fact.
- **The pointer head lost.** It returned an empty buffer on 43.5% of positive
  documents: the fixed 0 threshold on a rare-positive BCE is too conservative.
  It was also less exact when it fired. A tuned threshold, or a focal loss,
  might close part of the gap. Its null rate (0.996) is the other side of the
  same calibration.
- **The weak spot is counting.**
  - On 70 documents with two or more gold spans, the slots head returned
    fewer spans than gold 57 times, the same number 13 times, and more never.
  - Its share of documents with all gold contained is 0.196 on multi-answer
    documents.
  - Ordered slots stop at the first null. Hungarian (set) matching, or
    decoding each slot independently, are the next things to try.
- **Against v1.** v1's top 3 memories contain 77% of gold spans at about 446
  tokens, which is 62 times the slots buffer. v1's top 1 contains 51% at
  218 tokens. The span head trades some recall for a packet of about 7
  tokens. With B = 2,048, that leaves room to return spans and whole
  memories together.

**Wider gold (open question 2): the "as of" date and the clause.** The same
slots model was trained at two more gold widths, on the same documents:

- `anchored`: each needle plus its memory's `[timestamp]` as a separate span,
  with K = 16;
- `clause`: the sentence of the memory holding the needle, with spans of up to
  96 tokens.

Every width is scored on whether the packet still holds the needle:

| Gold width | Needle contained | All needles contained | Null correct | Tokens | Own-level token F1 |
| --- | --- | --- | --- | --- | --- |
| needle | 0.620 [0.56, 0.68] | 0.497 | 0.876 [0.83, 0.92] | 7.2 | 0.644 |
| anchored (needle + timestamp) | 0.496 [0.43, 0.56] | 0.384 | 0.976 [0.96, 0.99] | 26.3 | 0.491 |
| clause | 0.618 [0.55, 0.68] | 0.503 | 0.972 [0.95, 0.99] | 37.2 | 0.641 |

- **The as-of anchor as a span hurts.** Needle containment falls by 0.124
  [0.068, 0.182].
  - An ISO timestamp costs 20 tokens.
  - It sits at the start of a memory, before the content that makes the memory
    relevant, which is the causal limit (§5.5) in its purest form.
  - Doubling the spans also doubles the under-counting: the model predicted
    400 spans against 693 gold. It got 40% of timestamps exact and 32% of
    needles.
- **Why the date mattered in the first place.** Of the §6 model's 13 misses
  that landed in another evidence memory, 10 picked an older, stale value and
  3 a newer one. Almost all of them were v13 supersession programs.
  `point-in-time-contact` stays at 0.38 needle containment at every width.
  Emitting the date as a span does not fix temporal resolution. Better
  options:
  - render a compact date into the memory text;
  - add the superseded values with their dates as relevant spans (a "history"
    width built from distractor strings inside evidence pairs), so the reader
    gets the timeline.
- **The clause width is the better widening.**
  - It keeps needle containment the same (paired −0.002 [−0.069, 0.067]).
  - It hands the reader the whole statement at about 37 tokens (1.8% of B).
  - It cuts false "relevant" outputs on null documents from 12.4% to 2.8%.
  - Families move both ways: `world-story-next-action` goes from 0.48 to 0.87
    needle containment and `world-story-x-owner-email` from 0.86 to 0.50.
  - Some clause spans run across a memory boundary into the next memory.

**Caveats, beyond §6's:**
- Gold strings are string-matched. For example, a channel answer `message`
  also matches "prompted by a message from …".
- The null documents are built by filtering, so "nothing relevant" is
  approximate.
- Answers that are computed (60 held-out cases) have no extractable span.
- User-profile relevance ("who the user is") has no generator label yet;
  grounding tokens cover only part of it.

**Next on the Mac mini** (done in §8):
1. set matching for slots;
2. a "history" gold width with compact dates;
3. clause spans clipped at memory boundaries;
4. a calibrated pointer threshold chosen on a train split.

## 8. Set matching, the history width and calibration (Mac mini PoC, part 3)

The same documents, model recipe and caveats as §7. The data file gains a
fourth gold width (`DATA_VERSION = 3`, sha256 `f279e697…` held out,
`62e904e2…` train). Its first three widths, and every document, are
byte-identical to §7's.

**What changed:**

- **Set matching** (`train_multi.py --matching set`). Gold spans go to the
  slots that already score them best. The assignment is an exact minimum-cost
  one: a DP over subsets of gold, which matched brute force on 300 random
  cases. Each slot decodes on its own, and the highest-scoring proposals that
  don't overlap fill the buffer in document order.
- **`history` width.** The `clause` gold, plus the sentence of every
  distractor or forbidden string inside an evidence memory: the older,
  superseded or other values. The packet carries the timeline, for example
  every dated milestone for "counting from the earliest", or the old and new
  email for "as of June 22". It adds spans to 48 of the 177 positive held-out
  documents.
- **Dates from the packer, not the model.** `dated_tokens` is the packet cost
  when the packer prefixes each quoted memory with its compact date
  (`[YYYY-MM-DD] `, from the memory's metadata). The reader always gets the
  as-of date, and the model never has to point at a timestamp (§7 showed that
  hurts).
- **Decoding variants, no retraining.** These re-decode saved logits on the
  CPU (`eval_multi.py redecode`):
  - `--clip-pairs`: no span may cross a memory boundary;
  - `--no-stop`: ordered slots skip an empty slot instead of stopping;
  - a null margin for slots and a fire threshold for the pointer. Each is
    picked on **seed 108**, a calibration seed in neither the training nor the
    held-out split (214 documents), by `calibrate.py`. It maximises
    `0.5 · positive token F1 + 0.5 · null correct`.

**Held-out results** (n = 177 positive and 250 null documents, seeds 42 and 7):

| Model and decoding | Token F1 | Needle contained | All needles contained | Multi F1 (n 46) | Absence F1 (n 24) | Null correct | Tokens | Tokens with packer dates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| §7 slots, needle | 0.644 | 0.620 | 0.497 | 0.595 | 0.413 | 0.876 | 7.2 | 23.3 |
| set matching, needle | 0.672 [0.61, 0.73] | 0.642 | 0.475 | 0.685 | 0.603 | 0.848 | 7.2 | 23.9 |
| set matching + margin −2 | 0.686 | 0.669 | 0.531 | 0.713 | 0.682 | 0.800 | 8.0 | 26.1 |
| pointer + threshold −3 | 0.652 | 0.615 | 0.497 | 0.756 | 0.536 | 0.912 | 10.5 | 32.4 |
| §7 slots, clause | 0.641 | 0.618 | 0.503 | 0.660 | 0.461 | 0.972 | 37.2 | 51.4 |
| clause + margin −4 + clip | 0.676 | 0.682 | 0.565 | 0.674 | 0.681 | 0.944 | 43.4 | 59.8 |
| **history** | 0.692 | 0.711 [0.65, 0.77] | 0.582 | 0.702 | 0.781 | **0.988** | 52.8 | 71.3 |
| **history + margin −4 + clip** | **0.721** [0.67, 0.76] | **0.783** [0.73, 0.83] | **0.655** | **0.733** | **0.797** | 0.976 [0.96, 0.99] | 64.5 | 87.1 (4.3% of B) |
| v1 top 3 memories (§7) | 0.035 | 0.774 | 0.701 | 0.022 | 0.089 | 0.000 | 446 | — |

- Token F1 is against each model's own gold width. Needle containment is
  comparable across rows.
- "Multi" and "Absence" are F1 against the model's own width.
- The ordered `--no-stop` variant changed nothing (0.645 F1). Clipping at
  memory boundaries never hurt and saved 1–5 tokens. Neither is listed.
- The two new models were evaluated once. Every re-decode of saved logits is
  deterministic, and the three §7 models reproduced their §7 predictions
  byte for byte through the new eval path.

**Reading the numbers:**

- **The history width is the win.** It is trained on the timeline, not just
  the answer.
  - It finds the answer more often: needle containment is +0.091 [0.021,
    0.162] against the needle-trained model and +0.092 [0.042, 0.143] against
    the clause model.
  - Its false-positive rate on null documents drops to 1.2%.
  - The as-of failures it was aimed at move the most (needle contained,
    needle model → history):

    | Family | n | Needle model | History |
    | --- | --- | --- | --- |
    | `point-in-time-contact` | 8 | 0.38 | 0.88 |
    | `world-story-owner-current` | 14 | 0.49 | 0.85 |
    | `absence-false-premise` | 6 | 0.18 | 0.73 |

  - With the seed-108 margin and memory clipping, needle containment is 0.783.
    That matches v1's top 3 whole memories (0.774) at a seventh of the tokens
    (64.5 against 446, or 87 with dates), and it still says "nothing relevant"
    correctly 97.6% of the time.
  - Against the §7 needle model, that is +0.163 [0.100, 0.228] needle
    containment and +0.100 [0.064, 0.144] null correct.
  - Per seed, token F1 is 0.758 on seed 42 and 0.683 on seed 7, and null
    correct is 0.968 and 0.984.
- **Set matching helps multi-answer documents but does not fix counting.**
  - Multi-answer token F1 rises by +0.124 [0.063, 0.186] and absence F1 from
    0.413 to 0.603.
  - Overall, token F1 rises by +0.028 [−0.031, 0.084] and null correct falls
    by −0.028 [−0.060, 0.004].
  - It still returns too few spans on 51 of 70 documents with two or more gold
    spans (ordered slots: 57). All needles contained does not move
    (−0.014 [−0.100, 0.071]).
  - Counting is the open problem. A learned count, or a per-slot calibrated
    margin, comes next.
- **The pointer head was a calibration problem.**
  - With the fire threshold picked on seed 108 (−3.0), its token F1 goes from
    0.389 to 0.652, level with the slots (+0.007 [−0.052, 0.069]).
  - It is best on multi-answer documents (0.756).
  - Its span-level exact F1 (0.419 against 0.612) shows looser boundaries.
- **Calibration trades null precision for recall, as it should.** The margin
  the balanced objective picks is −2 to −4 for every slots model: it fires
  more readily.
- **The v13 programs stay the weak spot** at 0.49–0.65 needle contained, even
  with history. They need the struck or "decision so far" statements, which
  carry no distractor string, so the history gold misses them. A generator
  `supersedes` label (§5.2) would cover them.

**What this means for the spec.** The strongest form so far is:

- a K = 12 slot head trained on the history width;
- the null margin calibrated on a calibration seed;
- spans clipped to one memory each;
- a compact date per memory added by the packer.

Two things would turn into contract items: the margin (a fixed decoding
constant, published with the season), and the history width (gold labels the
generator would export natively, §4.1). Nothing here changes live scoring.

**Next on the Mac mini** (done in §9):
1. history width with set matching;
2. a span count head, or a per-slot margin, for multi-answer documents;
3. a `supersedes` label for the v13 programs;
4. a reader test: answer accuracy of a small local model given each packet,
   rather than span overlap alone.

## 9. A count head, the evidence width and a reader test (Mac mini PoC, part 4)

The same documents, recipe and caveats as §7 and §8.

**Data.** `DATA_VERSION = 4` adds a fifth width (sha256 `ee887c22…` held out,
`58897997…` train, `1cb75e5b…` calibration seed 108). Every document and the
four earlier widths are byte-identical to version 3. The new width:

- **`evidence`**: `history`, plus, for every other evidence memory in the
  document, its whole user statement when it is at most 96 tokens.
- These memories hold no labelled string, but the generator marks them as
  needed:
  - who a person is to the user ("Shanna Garner is my physiotherapist");
  - where they work now;
  - a glossary line;
  - a "that entry was struck" correction.

  That is the "who the user is, in relation to the question" relevance, and
  the closest exporter-side stand-in for a `supersedes` label.
- It adds spans to 89 of the 177 positive held-out documents.

**Models.** Three LoRA plus slots runs (K = 12, spans up to 96 tokens, 1 epoch,
set matching):

1. history width;
2. history width plus a **count head**. K + 1 more columns at the null
   position predict how many spans the document holds (0..K). Decoding keeps
   the top n̂ non-overlapping slot proposals by margin over null, and n̂ = 0 is
   "no answer". There is no threshold to calibrate.
3. evidence width.

Margins were again picked on seed 108, and every span is clipped to one
memory.

**Span results** (n = 177 positive and 250 null documents, seeds 42 and 7).
Needle containment is comparable across widths:

| Model and decoding | Needle contained | All needles contained | Absence F1 | Null correct | Tokens (dated) |
| --- | --- | --- | --- | --- | --- |
| §8 ordered, history, default decoding | 0.711 | 0.582 | 0.781 | 0.988 | 53 (71) |
| §8 ordered, history, margin −4 + clip | 0.783 | 0.655 | 0.797 | 0.976 | 65 (87) |
| set, history, margin −2.5 + clip | 0.729 | 0.644 | 0.876 | 0.964 | 57 (79) |
| **set + count head, history, clip (no calibration)** | **0.784** [0.73, 0.84] | **0.695** | 0.808 | 0.964 [0.94, 0.98] | 60 (80) |
| **set, evidence, margin −4.5 + clip** | **0.832** [0.78, 0.88] | **0.763** | **0.906** | 0.916 [0.88, 0.95] | 110 (145) |

- **The count head fixes most of the counting.**
  - On the 99 documents with two or more history spans, it returns the right
    number of spans 65 times. Ordered slots return the right number 36 times,
    or 53 with the calibrated margin. Under-counts fall from 57 to 25.
  - Against ordered slots at default decoding, all needles contained is +0.113
    [0.045, 0.181] and needle containment +0.073 [0.008, 0.137], for −0.024
    null correct.
  - It matches the calibrated ordered model (+0.001 needle containment
    [−0.061, 0.062]) without needing a calibration seed. That matters for a
    contract: there is no published margin to tune against.
  - Per seed, token F1 is 0.704 on seed 42 and 0.676 on seed 7.
- **Set matching alone does not help at the history width.** It is −0.051
  [−0.114, 0.012] needle containment against ordered slots.
- **The evidence width finds the most.**
  - It reaches 0.832 needle containment, 0.763 all needles contained and
    0.906 absence F1 (per seed, token F1 0.845 and 0.835).
  - It pays about twice the tokens, and null correct falls by −0.060 [−0.092,
    −0.032] against the §8 best.

**Reader test** (`reader_eval.py`). A small local reader, Qwen3.5-2B (bf16,
greedy, thinking off, told to answer only from the excerpts or reply "I don't
know"), answers each query from one packet. Packets are dated per memory by
the packer.

- **Answer** documents (n = 153) are correct when the reply contains the
  generator's answer: every `answer_items` string, or else any expected or
  accepted string.
- **Absence** (n = 24) and **null** documents (n = 92, a deterministic
  subsample of the 250) are correct when the reply declines. That uses a
  keyword heuristic.

| Packet | Answer correct | Absence declined | Null declined | Tokens |
| --- | --- | --- | --- | --- |
| No memory | 0.000 | 1.000 | 1.000 | 0 |
| Oracle needle gold | 0.183 [0.12, 0.24] | 1.000 | 1.000 | 21 |
| Oracle history gold | **0.627** [0.55, 0.70] | 0.792 | 1.000 | 77 |
| Oracle evidence gold | 0.562 | 0.750 | 1.000 | 153 |
| §7 slots, needle | 0.111 | 0.958 | 1.000 | 20 |
| §8 ordered, history, margin + clip | 0.497 [0.42, 0.58] | 0.625 | 1.000 | 81 |
| set, history, margin + clip | 0.471 | 0.833 | 0.989 | 70 |
| **set + count head, history** | **0.503** [0.42, 0.58] | 0.750 | 0.978 | **73** |
| set, evidence, margin + clip | 0.490 | 0.708 | 0.989 | 147 |
| v1 top 3 memories (§7) | 0.523 [0.44, 0.60] | 0.833 | 0.957 | 512 |
| Pack the whole document | 0.510 | 0.583 | 0.935 | 1,498 |

- **Bare answers do not help a reader.** Given exactly the gold answer values,
  the reader is right 18% of the time. 118 of its 125 misses are "I don't
  know": a value with no context does not answer anything. The history gold
  lifts the same reader to 63% (+0.444 [0.353, 0.529]). **Grading v2 on
  needle overlap would reward packets the reader cannot use.**
- **A ~73-token history packet reads as well as 512 or 1,500 tokens.** The
  count-head packet is level with v1's top 3 memories (−0.020 [−0.092, 0.046])
  and with the whole document (−0.007 [−0.092, 0.072]). It uses a seventh and
  a twentieth of the tokens. On "nothing relevant" documents it declines 97.8%
  of the time, against 95.7% and 93.5%.
- **More context is not better for a 2B reader.** Oracle evidence (153
  tokens) reads worse than oracle history (77 tokens), and the evidence model's
  packet equals the history ones. A stronger reader may use the extra
  relevance. This one does not.
- **Absence questions are where focused packets cost.** Near-miss facts make
  the reader guess a little more often than v1's top 3 do (0.750 against
  0.833 declined, n = 24, so wide intervals). The whole document is worst
  (0.583).
- **Headroom.** The best predicted packet (0.503) is 0.124 below the oracle
  history packet (0.627). The span model, not the width, is now the limit.
- **v13 programs stay hard for every packet** (the oracle history reads
  0.38 on `v13-open-program`). The reader has to compose a glossary with
  events. That is a reader-capacity question as much as a span one.

**What this means for the spec:**

1. **Grade on the history width, not the needle.**
   - Gold is the answer's sentence plus every superseded or other value's
     sentence inside evidence, with exported `answer_span` and `history_span`
     labels (§4.1).
   - `span_f1` and needle containment against that gold track reader accuracy
     here; needle-only overlap does not.
2. **Use a count head.** "No answer" becomes n̂ = 0, and decoding needs no
   season-published threshold.
3. **The packer adds a compact date per memory,** from metadata.
4. **Keep a reader check** (a pinned small reader, answer and decline
   accuracy) as a screening or calibration signal beside span metrics. It
   should not replace them: it adds generation and its own variance.

**Next on the Mac mini:**
1. evidence width with a count head (done in §10: answer contained 0.790,
   but null correct falls to 0.748, so the count head suits the history width
   better);
2. a second reader size (0.8B), done in §10;
3. a pinned reader-accuracy number per packet in `eval_multi.py` output.

A bf16 CUDA check still waits for a CUDA host.

## 10. Scaling, the plateau, and whether to rent GPUs or launch an IM

The question: how far does the §9 recipe improve with more compute? Is
scaling it worth renting GPUs? Is it worth putting a new incentive mechanism
(IM) on the subnet?

**Recipe, held fixed.**
- Qwen3.5-0.8B with a LoRA (r 16) and a K = 12 slots head.
- History gold, set matching and the count head.
- Memory clipping, and no calibration.
- 1 epoch unless stated.

**Axes:**
- **Data:** nested training sets of 2, 4, 6, 12 and 24 generator seeds. Each
  smaller set is a prefix of the larger, and seed 108 stays reserved.
- **Training seed:** seeds 0 and 1 at 6 and 12 seeds.
- **Epochs:** 2 epochs at 6 seeds.
- **Model size:** Qwen3.5-2B at 6 seeds.

**Eval.** A wider held-out set, **seeds 1–5** (456 positive and 171 null
documents; sha256 `605ee3b0…`), in no split used before.
- Span metrics as in §8.
- The 2B reader as in §9, with its null documents subsampled as there.
- v1's top 3 memories (the v1 PoC LoRA), the oracle history gold and the 0.8B
  reader as references.

**Results on the wide set** (95% bootstrap intervals over documents; reader
accuracy over 396 answer documents):

| Run | Training documents | Answer (needle) contained | All needles | Null correct | Tokens | Reader accuracy (2B) |
| --- | --- | --- | --- | --- | --- | --- |
| 2 seeds | 548 | 0.538 [0.50, 0.58] | 0.436 | 0.708 | 50 | 0.424 [0.38, 0.47] |
| 4 seeds | 1,065 | 0.617 [0.58, 0.66] | 0.498 | 0.994 | 62 | 0.444 [0.39, 0.49] |
| 6 seeds, seed 0 | 1,605 | 0.690 [0.65, 0.73] | 0.566 | 0.982 | 60 | 0.505 [0.46, 0.56] |
| 6 seeds, seed 1 | 1,605 | 0.601 [0.56, 0.64] | 0.469 | 0.924 | 44 | 0.462 [0.41, 0.51] |
| 6 seeds × 2 epochs | 3,210 steps | 0.642 [0.60, 0.68] | 0.550 | 0.971 | 48 | 0.490 [0.44, 0.54] |
| 6 seeds, 2B base | 1,605 | 0.709 [0.67, 0.75] | 0.592 | 0.942 | 62 | 0.503 [0.45, 0.55] |
| 12 seeds, seed 0 | 3,220 | 0.776 [0.74, 0.81] | 0.671 | 0.988 | 72 | 0.535 [0.49, 0.59] |
| 12 seeds, seed 1 | 3,220 | 0.691 [0.65, 0.73] | 0.607 | 0.982 | 52 | 0.495 [0.44, 0.55] |
| **24 seeds** | 6,447 | **0.798** [0.76, 0.83] | **0.702** | 0.982 | 60 | **0.566** [0.52, 0.62] |
| v1 top 3 memories | – | 0.779 [0.74, 0.81] | 0.702 | 0.000 | 467 | 0.558 [0.51, 0.61] |
| Oracle history gold | – | – | – | – | – | 0.677 |

Paired differences on the same documents (needle contained | reader accuracy):

| Comparison | Needle contained | Reader accuracy |
| --- | --- | --- |
| 4 − 2 seeds | +0.079 [0.031, 0.125] | +0.020 [−0.025, 0.066] |
| 6 − 4 seeds | +0.073 [0.029, 0.114] | +0.061 [0.018, 0.104] |
| 12 − 6 seeds, seed 0 | +0.086 [0.045, 0.125] | +0.030 [−0.015, 0.076] |
| 12 − 6 seeds, seed 1 | +0.090 [0.048, 0.132] | +0.033 [−0.015, 0.078] |
| **24 − 12 seeds, seed 0** | **+0.022** [−0.015, 0.058] | **+0.030** [−0.013, 0.071] |
| 24 seeds − v1 top 3 | +0.019 [−0.027, 0.066] | +0.008 [−0.043, 0.056] |
| Seed 1 − seed 0, at 6 seeds | −0.089 [−0.128, −0.048] | −0.043 [−0.086, −0.003] |
| Seed 1 − seed 0, at 12 seeds | −0.085 [−0.121, −0.050] | −0.040 [−0.083, 0.003] |
| 2 epochs − 1 epoch, at 6 seeds | −0.048 [−0.079, −0.017] | −0.015 [−0.056, 0.025] |
| 12 seeds − 6 seeds × 2 epochs (equal compute) | +0.134 [0.090, 0.178] | +0.045 [0.000, 0.091] |
| 2B − 0.8B, at 6 seeds | +0.020 [−0.017, 0.060] | −0.003 [−0.045, 0.038] |
| 12 seeds − v1 top 3 | −0.003 [−0.050, 0.043] | −0.023 [−0.071, 0.025] |

**Side checks** (on the §7–§9 held-out set):
- **0.8B reader:** the 73-token count packet (0.464) reads as well as v1's top
  3 (0.471) and better than the whole document (0.418). Oracle history is
  0.556. A weaker reader gains more from a short packet.
- **Evidence width with the count head:** answer contained 0.790, but null
  correct is 0.748. That is worse than the evidence model with a calibrated
  margin (0.916).

**What the curves say:**

1. **Fresh data is the only axis that pays, up to a point.**
   - From 2 to 12 seeds, every doubling of generator seeds adds about +0.08
     to +0.09 answer containment, on both training seeds: log-linear until it
     bends (item 4).
   - Reader accuracy rises only about +0.02 to +0.06 per doubling, and most of
     those intervals include 0.
   - A second epoch hurts (−0.048). At equal compute, fresh seeds beat
     repetition by +0.134.
   - A 2.6× larger base buys nothing measurable at this data size (+0.020
     [−0.017, 0.060]).
2. **Training-seed noise equals one data doubling.** Re-running the identical
   recipe on another training seed moves answer containment by −0.085 to
   −0.089 and reader accuracy by about −0.04, at both data sizes. Only
   eval-seed variance is small: per-eval-seed containment has a standard
   deviation of 0.02–0.05.
3. **At 12 seeds the span packet is level with v1's top 3 memories.** That
   holds on both answer containment and reader accuracy, at 72 tokens against
   467, and it adds a working "nothing relevant" answer (0.988 against 0).
4. **The plateau is at about 12–24 seeds** (about 3,000–6,500 training
   documents) for this recipe (0.8B, LoRA r 16, 1 epoch).
   - Up to 12 seeds every doubling paid about +0.08 to +0.09 answer
     containment. 12 → 24 seeds pays +0.022 [−0.015, 0.058] on the wide set
     and −0.014 [−0.067, 0.039] on the §7 held-out set: about a quarter of the
     earlier slope, and inside noise.
   - Reader accuracy keeps creeping (+0.030 [−0.013, 0.071]). The best packet,
     at 24 seeds, reads 0.566 at 60 tokens against v1's top 3 at 0.558 with
     467 tokens, and the 2B reader's oracle ceiling is 0.677.
   - What remains is capacity and labels, not data:
     - 2B was not better at 6 seeds, but it was never tried at 24;
     - a higher LoRA rank or a full fine-tune was not tried;
     - the history gold misses the v13 "struck" statements (§8).

**Cost of scaling further.**

Measured on the Mac:
- 1,579 input tokens per training document;
- 0.30 documents/s for one job alone (fp32, MPS, gradient checkpointing),
  about 0.10–0.16 per job with four jobs sharing the GPU;
- about 2 days of continuous Mac mini time for this whole PoC, with up to
  five jobs sharing the GPU.

Estimated for one L40S (bf16, LoRA, checkpointing):
- 6 × 0.87 B FLOPs per token at 15–35% of 181 TFLOPS gives 5,200–12,100
  tokens/s, or 3.3–7.7 documents/s (11–26× the Mac). That is an estimate;
  B0 measures it.

| Training set | Documents | L40S hours | Lium ($0.38–0.73/h) | AWS g6e ($1.86/h) |
| --- | --- | --- | --- | --- |
| 24 seeds | 6,440 | 0.2–0.5 | $0.09–0.40 | $0.43–1.01 |
| 48 seeds | 12,880 | 0.5–1.1 | $0.18–0.79 | $0.87–2.02 |
| 96 seeds | 25,760 | 0.9–2.2 | $0.35–1.59 | $1.73–4.04 |
| 192 seeds | 51,520 | 1.9–4.3 | $0.71–3.17 | $3.46–8.08 |

- **Scoring one submission** is a forward pass over about 600–1,000 documents
  (1–1.6M tokens): minutes on an L40S, under $0.05.
- **A reader check** adds about 600 short generations.
- The data curve has already bent at 24 seeds. The cheap regime, about
  3,000–6,500 documents, costs under $1 of L40S time per training run.

**Is renting GPUs worth it?** Yes, but only a small, targeted spend that fits
inside the already-approved P0 budget (about $300–1,000; Lium first).

1. **Test whether the plateau is a capacity limit.** Train 2B, a higher
   LoRA rank and a full fine-tune of 0.8B, at 24–48 seeds, with 3 training
   seeds each, on one L40S. That is about $5–30 at Lium prices; the Mac would
   need about 1–2 days per configuration. More data alone is not worth
   renting for: 12 → 24 seeds already stopped paying.
2. **Test with a production-class reader** (Haiku 5.5 or GLM-5.3-Flash, the
   readers the product and DittoBench use) on the same packets. This is the
   one result that changes the product case: a 2B reader caps at 0.677 even
   with oracle packets, so the packet's real value at production quality is
   unmeasured. It costs inference, not training: tens of dollars.
3. **Run B0's bf16 determinism check** on the same node (bittensor-compute.md).

Not worth renting:
- **more data alone:** past about 12–24 seeds it stopped paying;
- **bigger bases without more data:** 2B gave nothing at 6 seeds;
- **longer training:** a second epoch hurt;
- **H100-class hardware:** the whole curve fits in a few L40S-hours.

**Is it worth putting a new IM on the subnet?** Not as a new mechanism, and
not yet as a paid track. Measured against what an IM needs:

| What an IM needs | Evidence here |
| --- | --- |
| A spread larger than noise | Yes on span metrics. 2 against 12 seeds is +0.24 containment, and 5 eval seeds resolve about ±0.04. Reader accuracy spreads only about 0.14 across 12× data (0.424 to 0.566), so it cannot rank miners alone. |
| Rankings that are not luck | No. Re-rolling the training seed of one recipe moves containment by 0.09, as much as doubling the data. A KOTH board would churn on seed lottery unless the dethrone margin is at least 0.09 containment, or a miner must submit several seeds. |
| Headroom that needs ideas, not just spend | Weak. The recipe is simple and public, and it plateaus at about 12–24 generator seeds: under $1 of L40S time per training run. A miner market would reach that ceiling within days, then compete on seed re-rolls and copies (open questions Q1 and Q3). Emissions would buy little that about $10–50 of in-house GPU time does not. |
| A test that transfers to the product | Unknown. Everything here is the public generator, and the score measures fidelity to it. The approved private team-trace eval (P0-T) is what would make a competition test something the generator does not. |
| Low operating cost | Mixed. Scoring is cheap (under $0.05 per submission). But v2 needs native generator span labels, a new scorer path, commit-reveal seeds (Q1) and near-duplicate rules (Q3). v1 itself has not yet run its shadow season or P0-S. |

Recommendation:

- **Keep span extraction inside the existing CPM track**, as the
  `cpm_score_version = 2` candidate in §4, in shadow at 0 bps. A second
  mechanism would halve capacity.
- **Make v2 the product's own CPM path**: Ditto trains and hosts the span
  model from the generator for about $10–50 of GPU time. Doing that in-house
  is cheaper than paying emissions for it today.
- **Revisit a paid v2 track only when all three hold:**
  1. a production-class reader shows packet quality matters beyond token
     savings;
  2. P0-T gives a private real-trace eval, so the competition cannot be won by
     generator volume alone;
  3. v1's shadow season has settled Q1 to Q3.

  Then set the dethrone margin from these noise numbers: at least about 0.09
  containment on at least 5 eval seeds, or a mean over several training seeds.

## 11. Long haystacks (4–8k tokens): does harder data make a bigger model pay?

§10 left one question open: the plateau might be a property of easy data, so
harder documents might open a gap that rewards a bigger model. Real memory
corpora are 4–50k tokens. This tests the low end, 4–8k.

**Data (DATA_VERSION 5, `multi_data.py --doc-tokens 4096,8192`).**
- Each document draws its own budget uniformly from 4,096–8,192 tokens: p10
  about 4,500, median about 6,150, p90 about 7,750, with a median of 64
  memories.
- The case's candidates are filled first. v4 had been cutting them at 1.5k
  tokens; now they mostly fit. Only after that is a document padded with the
  seed's other memories.
- A padding memory never holds a needle, history or grounding string of the
  case, and shares no query word rarer than `PAD_DF` = 0.35 of the seed's
  memories. Padding ends up as only 2–3% of memories. The extra length is
  almost entirely the generator's own candidate pool: denser, same-topic
  distractors, not random history.
- Same splits and mix as §10: training is seeds 101–113 without 108 (3,263
  documents, sha256 `3602775f…`). The held-out set is seeds 1–5 (469 positive
  and 183 null, `0abd8715…`); calibration is seed 108 (121, `f3d262ba…`).
  Gold strings are identical to v4 on every shared case.
- Without the flag, the builder's output is still byte-identical to
  DATA_VERSION 4.

**Runs.** The §10 recipe (history gold, set matching, count head, K = 12,
LoRA r 16, 1 epoch, memory clipping) trained on the long set, for 0.8B and
2B. The §10 12-seed model, trained on short documents, serves as the
baseline. Each model is decoded two ways: with the count head, and with no
count and a null margin calibrated on seed 108 (`calibrate.py`), so that no
model is judged at an operating point it was not tuned for.

**Results on the 4–8k held-out set** (needle contained over 469 positive
documents, 95% bootstrap; reader is Qwen3.5-2B over 409 answer documents):

| Packet | Answer contained | All needles | Null correct | Tokens | Reader accuracy | Absence declined | Null declined |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Short-trained 0.8B, count | **0.711** [0.67, 0.75] | 0.599 | 0.918 | 98 | **0.491** | 0.817 | 0.944 |
| Short-trained 0.8B, calibrated (−3.5) | 0.702 [0.67, 0.74] | 0.586 | 0.896 | 86 | 0.487 | 0.783 | 0.922 |
| Long-trained 0.8B, count | 0.570 [0.53, 0.61] | 0.478 | 0.978 | 61 | 0.423 | 0.817 | 0.956 |
| Long-trained 0.8B, calibrated (−7.0) | 0.642 [0.60, 0.68] | 0.520 | 0.907 | 70 | 0.450 | 0.800 | 0.933 |
| Long-trained 2B, count | 0.511 [0.47, 0.55] | 0.452 | 0.885 | 46 | 0.381 | 0.883 | 0.933 |
| Long-trained 2B, calibrated (−3.0) | 0.618 [0.58, 0.66] | 0.516 | 0.918 | 59 | 0.474 | 0.767 | 0.922 |
| v1 top 3 memories | 0.717 [0.68, 0.75] | 0.638 | – | 609 | 0.501 | 0.733 | 0.878 |
| Whole document | – | – | – | 6,150 | **0.550** | 0.300 | 0.778 |
| Oracle history gold | – | – | – | 76 | 0.638 | 0.800 | 0.967 |

Paired differences on the same documents (needle contained | reader accuracy):

| Comparison | Needle contained | Reader accuracy |
| --- | --- | --- |
| 2B − 0.8B, both long-trained, calibrated | −0.024 [−0.062, 0.014] | +0.024 [−0.017, 0.066] |
| Long − short training, 0.8B, calibrated | −0.060 [−0.099, −0.020] | −0.037 [−0.078, 0.007] |
| Long-trained 2B − short-trained 0.8B, calibrated | −0.083 [−0.126, −0.044] | −0.012 [−0.054, 0.029] |
| Short-trained 0.8B − v1 top 3 | −0.007 [−0.052, 0.041] | −0.010 [−0.061, 0.039] |
| Short-trained 0.8B − whole document (reader) | – | −0.059 [−0.108, −0.005] |

**An earlier attempt, and why it was rebuilt.** The first long build padded
with a looser filter: it dropped only gold strings and query words under 2%
of the seed. The short-trained model then put 13% of its spans in memories
that no v4 document held, and some of those were plainly relevant (another
line of the same v13 case file). So the 2B run was stopped at about step 230
and the data rebuilt with the filter above. The direction did not change:
- that build's long-trained 0.8B reached 0.614 contained (count head);
- a curriculum run (the short model fine-tuned on a quarter epoch of long
  documents) reached 0.676, against 0.723 for the short model on the same set.

Padding noise was not the cause.

**What the runs say:**

1. **A short-trained model carries to 4–8k.** Training on 1.5k-token
   documents and running on 4–8k loses about 0.065 containment against the
   short held-out set (0.776 → 0.711). It still matches v1's top 3 on both
   containment and reader accuracy, at a sixth of the tokens.
2. **Training directly on long documents is worse, not better,** at equal
   document count and 1 epoch. This held in three long runs (two data builds
   and the curriculum).
   - The long-trained model becomes over-conservative: its count head
     under-predicts and its calibrated margin is −7, twice the short model's
     −3.5.
   - Calibration recovers part of the gap, not all of it (−0.060).
   - Each long document also costs about 6× as much Mac time. On MPS,
     Qwen3.5's linear-attention layers fall back to a reference PyTorch
     kernel.
3. **The bigger model does not pay on harder data at this size.** The 2B is
   level with the 0.8B on long documents (−0.024 [−0.062, 0.014]). Both end
   training at the same loss, about 1.0 per document, against 0.6 for short
   training. A 2.6× larger model that fits its training data no better points
   to a floor in the labels, not to capacity.
4. **The floor is unlabelled relevance.** The long-trained losses concentrate
   in the families where the candidate pool holds many same-topic memories
   that the history gold does not mark:
   - `v13-open-program`: 0.58 → 0.43 for the 0.8B;
   - `world-story-owner-current`: 0.79 → 0.60;
   - `v13-personal-program`: 0.39 → 0.28.

   These memories are struck statements, proposals, and older owners. Labelled
   negative, they teach the model to hold back. In a 1.5k-token document few of
   them fit, which is why short training learns a looser policy that happens to
   score better.
5. **At 4–8k the whole document answers best but declines worst.** The 2B
   reader gets 0.550 from 6,150 tokens against 0.491 from 98, but it declines
   only 30% of absence questions and 78% of nulls. The span packets decline
   77–88% and 92–96%. The oracle packet (0.638) shows the headroom is still in
   what the packet selects.

**What this means for data design.** More length only helps once the gold
labels every memory that matters among the candidates, not only the answer
clause and its distractor strings. Before going past 8k:
- label struck, proposed and superseded statements, and same-entity history,
  as relevant (the graded-relevance labels: answer, supporting, identity,
  irrelevant);
- or train only on the reader-verified minimal packet, and leave the remaining
  candidates unlabelled (no loss), rather than negative.

Until then, the efficient recipe is to train on short documents and run on
long ones.

**What this means for the GPU verdict in §10.** It stands, with one change.
The capacity test (2B, higher rank, full fine-tune at 24–48 seeds) is worth
renting only on relabelled data: on current labels the 2B did not beat the
0.8B at either length.

**Caveats.**
- One training seed per configuration. §10 measured training-seed noise at
  0.085–0.089, but the direction of item 2 replicated three times.
- One epoch, LoRA r 16, Mac fp32.
- 4–8k only: 50k is untested.
- Reader accuracy comes from the 2B reader.

## 12. A hard-case objective for the generator competition (proposal, design only)

Proposal: make the existing data-generation competition produce the cases the
CPM still fails. It needs no new mechanism, only a different objective for
the generator competition. Nothing here changes the live competition, a
validator or a `score_version`.

**Objective.** Score a generated case only when it is both of these:
- **valid:** production-class readers answer it correctly from the case's own
  gold packet;
- **hard:** reference CPMs fail it.

As the CPM improves, the data gets harder: the moving ceiling a model
competition lacks (§10).

**Prototype on existing results** (wide-v4 held-out, 2B reader). "Valid"
means the reader is right from the oracle history packet. "Hard" means the
24-seed model's packet fails. "Robust" means all five reference models fail
(24 seeds, 12 seeds × 2 training seeds, 6 seeds, 6 seeds 2B).

| Family | Answer cases | Valid | Hard | Robust |
| --- | --- | --- | --- | --- |
| v13-personal-program | 40 | 38 | 23 | 5 |
| v13-open-program | 60 | 44 | 13 | 4 |
| world-story-owner-current | 35 | 31 | 9 | 3 |
| world-story-order | 31 | 17 | 5 | 0 |
| injection-classic | 5 | 5 | 4 | 2 |
| world-story-next-action | 32 | 7 | 3 | 1 |
| world-story-x-owner-email | 35 | 11 | 3 | 0 |
| 21 other families | 158 | 115 | 1 | 0 |
| **Total** | 396 | 268 | 61 | 15 |

**Design rules this suggests:**
- **Judge hardness against an ensemble, not the champion.** Only 15 of the 61
  cases the 24-seed model misses are missed by every reference model. The
  rest are mostly training-seed luck (§10), and paying for them would reward
  generators for noise.
- **Use production readers for validity.** With the 2B reader, 128 of 396
  cases (32%) are not answerable even from the oracle packet. A weak validity
  reader throws away good cases and keeps cases it happens to read.
- **Require complete labels.** §11 shows that a case can be "hard" because its
  gold leaves relevant memories unlabelled. A generator could farm that, so a
  valid case must label every memory that matters (graded relevance), and an
  audit should check that the gold packet is sufficient.
- **Cap the share of any one family**, so the objective cannot be met by
  flooding a single weakness. Today the yield sits almost entirely in v13
  programs and owner-current.
- **Run it in shadow first**, as its own season, before any weight.

Open questions:
- how to verify "every relevant memory is labelled" without a human;
- whether the ensemble should include each season's top miner models (open
  weights at submit);
- how to keep generators from tuning cases to the reference models' known
  quirks.
