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
| `model.onnx` | Sequence-classification cross-encoder. Inputs `input_ids`, `attention_mask`, `token_type_ids` (int64, `[batch, seq]`). One output of shape `[batch, 1]` (float32 relevance logit). At most 100 MiB. No external data files. Only the default ONNX domain and `com.microsoft` operators. |
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

- Pools come from fresh DittoBench datagen universes. The generator records the
  pairs it planted as evidence for each case (`RequiredPairIDs`), so gold labels
  are exact, not inferred from answer strings.
- Candidate text is `"User: " + prompt + "\nDitto: " + response`, the same
  shape the product hydrates.
- Base order is a deterministic BM25 ranking over the case's memory graph. The
  product's base order is its composite retriever. Matching it exactly needs
  embeddings and is deferred.
- Only pools with at least one gold memory in the top 20 are kept, because a
  reranker cannot move what is not in the pool.
- Scored pools are generated from a seed fixed only after the submission is
  committed, so they cannot be memorised. A public practice generator uses a
  disjoint seed space.

## Score

For each pool:

1. Tokenize each `(question, candidate)` pair with the product tokenizer,
   truncating longest-first to **256** tokens.
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
| Latency | p50 time for one 20-candidate pool, 4 intra-op threads, after warm-up, **≤ 50 ms** on the reference CPU. Measured once by the trusted screener, not by every validator, because validator hardware differs. |
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
