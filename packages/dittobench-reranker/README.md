# dittobench-reranker

SN118 Season 2: the memory reranker competition. Miners submit an ONNX
cross-encoder; validators score how well it reorders Ditto memory-retrieval
pools, using the same tokenizer, fusion and metric as the product.

- Contract: [`docs/reranker-season.md`](../../docs/reranker-season.md), with the
  constants in `dittobench_reranker/contract.py`.
- Jira: SN-202 (epic), SN-203 (contract).

```bash
uv run --group dev pytest -q
```
