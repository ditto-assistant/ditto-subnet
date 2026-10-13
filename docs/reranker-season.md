# Season 2: memory reranker competition

Status: **shadow build** (SN-202). This document is the season contract
(SN-203). The machine-readable copy lives in
`packages/dittobench-reranker/dittobench_reranker/contract.py`; when the two
disagree, the code is authoritative and this document is the bug.

## What is optimised

Miners submit a **cross-encoder reranker model**. The score is how well that
model reorders a pool of 20 retrieved memories so that the memories which
answer the question rank first, measured exactly the way Ditto's product uses a
reranker. Nothing else is scored.

The product slot is `backend/pkg/services/retrieval/crossencoder`. It tokenizes
`(query, memory)` pairs with an uncased BERT WordPiece tokenizer, runs one
batched ONNX forward pass, and fuses the cross-encoder order with the original
retrieval order by weighted Reciprocal Rank Fusion. The champion of this
season ships into that slot (DITTO-737).

## The artifact

A submission is a directory, packed as a tarball:

| File | Requirement |
|---|---|
| `model.onnx` | Sequence-classification cross-encoder. Inputs `input_ids`, `attention_mask`, `token_type_ids` (int64, `[batch, seq]`). An output named `logits` of shape `[batch, 1]` (float32 relevance logit). At most 100 MiB. No external data files. Only the default ONNX domain and `com.microsoft` operators. |
| `vocab.txt` | Uncased BERT WordPiece vocabulary. Line N is token id N-1. `[PAD]`=0, `[UNK]`=100, `[CLS]`=101, `[SEP]`=102, which the product tokenizer hard-codes. At most 2 MiB. |
| `manifest.json` | `{"contract": "reranker-season-v1", "base_model": "<hf id or none>", "base_model_license": "<spdx id>"}`. The licence must be in the allow-list below. |

The artifact is a model, not code. There is no harness, no container and no
sandboxed execution.

**Base-model licence allow-list:** `apache-2.0`, `mit`, `bsd-2-clause`,
`bsd-3-clause`. A model derived from a non-commercial or share-alike base cannot
ship in the product and is rejected. Submitting also requires a hotkey-signed
contributor licence agreement (SN-211).

## Evaluation pools

A pool is one question plus 20 candidate memories in **base order** (the order
retrieval produced before reranking), with the subset that answers the
question marked gold.

- Pools come from fresh DittoBench datagen universes
  (`packages/dittobench-reranker/poolgen`: package `rerankpool`, CLI
  `cmd/rerank-pools`). The
  generator records the pairs it planted as evidence for each case
  (`RequiredPairIDs`), so gold labels are exact, not inferred from answer
  strings.
- A case's graph is what the scorer would have seeded for that user up to the
  case's wave: the tool-prerequisite world first, then the memory waves.
- Every gold memory is in the pool. The other slots go to the hardest
  negatives: the non-evidence memories BM25 ranks highest. The pool is then
  ordered by BM25. A lexically weak gold memory therefore sits low in base
  order, which is the product's real situation: the evidence was retrieved but
  ranked badly.
- The product's base order is its composite retriever. Matching that needs
  embeddings and is deferred; every candidate is scored on the same BM25 order.
- Cases whose evidence would fill more than half the pool are dropped.
- Candidate text is `"User: " + prompt + "\nDitto: " + response`, the shape the
  product hydrates, capped at 4,096 characters. The cap never changes what a
  256-token model sees.
- Scored pools are generated from a seed fixed only after the submission is
  committed, so they cannot be memorised. They are rendered under a held surface
  salt, so their wording differs from anything public. Any seed a miner picks is
  practice.

At bench version 13 (full profile), one seed yields 244 pools. Over seeds 1–4
the BM25 base order scores a mean NDCG@10 of 0.718, and gold is already first in
73% of pools. Most of the headroom is in ranking all of a case's evidence
highly, since most cases plant three or more memories.

## Uploading (shadow)

Submissions go to Platform with the miner CLI (SN-207):

```bash
uv run ditto --network finney upload --track reranker \
  --path <artifact directory or tar.gz> \
  --wallet.name <coldkey> --wallet.hotkey <hotkey>
```

The CLI runs the structural checks above locally (the `model.onnx` check only
when `onnx` is installed), packs a directory with the deterministic `pack()`,
and calls `POST /api/v1/upload/reranker`. There is no fee, no payment and no
admission token, and an accepted submission earns no weight: the track is in
shadow.

| Rule | Behaviour |
|---|---|
| Signature | The hotkey signs `reranker-season-v1:upload:{hotkey}:{sha256 of the upload}:{unix time}:{uuid4}` within five minutes. The prefix separates it from a memory-track upload signature, so neither can be replayed as the other, and binds it to this season. |
| Eligibility | The hotkey must be registered on the subnet and not banned. |
| Size | At most the three file caps plus 1 MiB of tar overhead, enforced on the streamed bytes and again on the decompressed archive. At most eight tar entries. |
| Validation | Every check in "The artifact", run by Platform on the uploaded bytes. A violation is HTTP 422 with the reason. |
| Duplicates | The artifact digest covers the three files. An exact duplicate of an earlier submission is HTTP 409; the first submitter keeps it. |
| Rate | One accepted upload per owner coldkey per hour by default, matching the memory track's cooldown (HTTP 429 with `Retry-After`). A request refused for any other reason (signature, eligibility, size, validation, duplicate) does not use the slot. Operators set `DITTO_RERANKER_UPLOADS_PER_WINDOW` and `DITTO_RERANKER_UPLOAD_WINDOW_SECONDS`; Backroom `get_reranker_submissions` shows the effective values. |

An accepted submission is stored and recorded with status `accepted`. Its
`seed` and `seed_block_hash` stay null at intake. The trusted scorer worker
(SN-209) derives them after the commit from a finalized block, scores the
artifact, and moves it to `scored`, `invalid` (for example the latency gate)
or `failed`. Status and score are public at
`GET /api/v1/public/reranker/submissions?hotkey=…` and
`GET /api/v1/public/reranker/submissions/{id}`; no tarball URL is published.

Reranker submissions live in their own table. They never enter the memory
track's screener queue, validator allocation, leaderboard, anti-copy checks or
payments.

## Score

For each pool:

1. Tokenize each `(question, candidate)` pair with the product tokenizer,
   truncating longest-first to **128** tokens. The product reads
   `ONNX_RERANK_MAXLEN` (default 256); its README and latency table use 128,
   the incumbent scores higher at 128 on season pools (NDCG@10 0.792 vs 0.778)
   and runs 25% faster, so a champion ships with 128.
2. Run one batched forward pass. Scoring uses one intra-op thread so results
   are deterministic.
3. Fuse the cross-encoder ranking with the base ranking by weighted RRF,
   `k = 60`, cross-encoder weight `0.7`. Ties keep base order. This is the
   product's `fuseRRF`.
4. Compute NDCG@10 with binary gains over the gold set.

The **score** is the mean NDCG@10 over all pools. Reports also carry R@10, the
base (unreranked) NDCG@10, and the lift over base.

## Gates

| Gate | Rule |
|---|---|
| Structure | The artifact passes every check in "The artifact". |
| Latency | p50 time to tokenize and score one 20-candidate pool, 4 intra-op threads, after warm-up, at most **3.5×** the incumbent's p50 measured on the same machine in the same run (interleaved pool by pool). This is the 50 ms budget expressed against the incumbent's ~14 ms in the product README; a ratio does not depend on the CPU. Measured once by the trusted screener, not by every validator. |
| Overfit (before paying) | No regression against the incumbent on a LongMemEval-S retrieval slice. Required before the track pays; not required during shadow. |

## Winning

King of the hill, using the existing rank shares. A challenger dethrones the
champion only when, on the same pools, its mean NDCG@10 exceeds the champion's
by at least `max(0.005, 1.64 × standard error of the paired difference)`. That
is half an NDCG point or a one-sided 95% band, whichever is larger. The first
submitter wins ties. Exact-hash duplicates of an earlier submission are
rejected.

**Incumbent at launch:** the product's current model,
`cross-encoder/ms-marco-TinyBERT-L2-v2` INT8 (Apache-2.0). A miner earns
nothing for matching what Ditto already ships.

## Saturation and exit

The season is saturating when there has been no dethrone for 21 days and the
top five are within the dethrone margin of each other. At that point the burn
ramp starts (SN-180) and the next season enters shadow.

## Explicitly out of scope for v1

LLM rerankers, rerankers that need GPUs, listwise or multi-stage rerankers,
changes to the base retriever, training data supplied by Ditto beyond the public
practice generator, and learned retrieval weights (Season 3, SN-164).

## Measured baseline (2026-10-13)

The product's shipped TinyBERT-L2 INT8 (`model.onnx` sha256 `d9c3f25f…`),
scored with this package on 976 pools (bench v13 full, seeds 1–4), on debby
(16-core x86):

| max_len | NDCG@10 | Lift over BM25 base | p50 per pool (4 threads) |
|---|---|---|---|
| 256 | 0.778 | +0.060 | 43.4 ms |
| 128 | 0.792 | +0.074 | 32.7 ms |

The README's ~14 ms was measured on different hardware with shorter texts, so
absolute milliseconds are not portable. That is why the gate is a ratio.
The Python tokenizer reproduces the product's Go tokenizer exactly: 0
mismatches over 19,536 (query, memory) pairs, including Unicode edge cases.

### Starter-kit trial (2026-10-13)

On 976 pools from four unseen seeds, a one-epoch fine-tune of the incumbent on
ten practice seeds scored 0.831 (public wording) and 0.832 (held-salt wording),
against the incumbent's 0.792 and 0.789. MiniLM-L2 off the shelf scored
0.797/0.794 at 1.6× latency, which does not clear the dethrone margin. The salt
and post-commit seeds stop memorisation, but not learning the generator's
world; the LongMemEval-S transfer gate is what guards against that and must be
in force before the season pays.
