# DittoBench reranker starter kit (Season 2)

Season 2 of SN118 pays for one thing: a **cross-encoder reranker** that puts
the memories which answer a question at the top of a 20-memory pool, scored
exactly the way Ditto's product reranks. The contract is
[`docs/reranker-season.md`](../../docs/reranker-season.md).

You submit a model, not code: `model.onnx`, `vocab.txt` and `manifest.json`.

## The loop

```bash
# 0. Tools: Go 1.23+ for pools, Python 3.11+ for everything else.
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -e ../../packages/dittobench-reranker[score]

# 1. Practice pools. Pick any seeds; scored seeds are fixed after you commit.
(cd ../../packages/dittobench-reranker/poolgen && \
  go run ./cmd/rerank-pools -seed 100 -count 10 -out "$OLDPWD/practice.json")
(cd ../../packages/dittobench-reranker/poolgen && \
  go run ./cmd/rerank-pools -seed 900 -count 4 -out "$OLDPWD/eval.json")

# 2. A model. Start from any cross-encoder whose licence is in the allow-list,
#    or fine-tune one on practice pools.
python train.py --pools practice.json --out ft/ --epochs 1
python export.py ft/ artifact/ --base-model cross-encoder/ms-marco-TinyBERT-L2-v2 \
  --license apache-2.0

# 3. Score it the way validators will, and compare with the incumbent.
dittobench-reranker score --artifact incumbent --pools eval.json --out champ.json
dittobench-reranker score --artifact artifact --pools eval.json --out mine.json
dittobench-reranker compare champ.json mine.json      # "dethrones": true?
dittobench-reranker latency --artifact artifact --pools eval.json --incumbent incumbent

# 4. Package (optional: step 5 packs a directory the same way).
dittobench-reranker pack artifact artifact.tar.gz

# 5. Upload to the shadow track, from the repository root.
(cd ../.. && uv run ditto --network finney upload --track reranker \
  --path miners/dittobench-reranker-starter-kit/artifact \
  --wallet.name <coldkey> --wallet.hotkey <hotkey>)
```

## Uploading (shadow)

`ditto upload --track reranker` checks the artifact locally against the
contract, packs a directory deterministically, signs the upload with your
hotkey and sends it to Platform. It prints the submission id. There is no
fee and no payment prompt: the season is in **shadow**, so a submission is
stored and scored but earns no weight yet.

- The hotkey must be registered on the subnet and not banned.
- One accepted upload per owner coldkey per hour by default (HTTP 429 with
  `Retry-After` otherwise). A rejected upload (for example a 422 validation
  failure) does not use the slot, so fix the artifact and resubmit.
- An artifact that is byte-for-byte identical to an earlier submission (same
  three files) is rejected; the first submitter keeps it.
- The model check needs `onnx`. It is installed in this kit's environment; the
  base `ditto` install skips it and leaves it to Platform.
- Follow progress at
  `https://<api>/api/v1/public/reranker/submissions/<submission id>`. The score
  stays null until the trusted scorer has run, after it derives the scoring
  seed from a block finalized after your upload.

The full rules are in
[`docs/reranker-season.md`](../../docs/reranker-season.md#uploading-shadow).

## What wins

- **Score:** mean NDCG@10 over fresh pools, after the product's fusion: your
  ranking is blended 70/30 with the base order by reciprocal rank fusion, so a
  model only helps where it is confidently right.
- **Dethrone:** beat the champion on the same pools by at least
  `max(0.005, 1.64 × standard error)`.
- **Latency:** p50 at most 3.5× the incumbent's, timed on the same machine
  (4 threads, max length 128). MiniLM-L2-class models fit; L6-class do not.
- **Licence:** the base model must be Apache-2.0, MIT or BSD so a winner can
  ship in Ditto. You also sign the contributor licence agreement (SN-211).

## The incumbent

`incumbent/` is the exact model the Ditto product embeds today:
`cross-encoder/ms-marco-TinyBERT-L2-v2`, dynamic INT8, Apache-2.0
(sha256 pinned in the contract). A submission that only matches it earns
nothing.

## What the evaluation does and does not protect

Scored pools are generated after your commitment from seeds you cannot pick,
and their wording is re-rendered under a held salt, so memorising practice
pools gains nothing. It does **not** stop a model learning the generator's
synthetic world. On 2026-10-13 a one-epoch fine-tune of the incumbent on ten
practice seeds (about a minute of CPU) scored:

| Model (976 pools, 4 unseen seeds) | Public wording | Held-salt wording | Latency vs incumbent |
|---|---|---|---|
| Incumbent (product TinyBERT-L2 INT8) | 0.792 | 0.789 | 1.0× |
| MiniLM-L2 INT8, off the shelf | 0.797 | 0.794 | 1.6× |
| Incumbent fine-tuned 1 epoch on practice pools | **0.831** | **0.832** | 1.0× |

The fine-tune dethrones the incumbent (+0.039, threshold 0.006). Whether such
gains transfer to real conversations is what the overfit gate checks before
the season pays: a challenger must not regress against the incumbent on a
LongMemEval-S retrieval slice. Train for retrieval, not for the generator.
