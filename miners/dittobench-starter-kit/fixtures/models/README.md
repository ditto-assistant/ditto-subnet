# Shipped ranking models

The retrieval pipeline ships two trained models as weights:

| File | Model | Role |
| --- | --- | --- |
| `mlp-weights.bin` | weight-predictor MLP (~217K params) | predicts the 7 composite fusion weights + scale from the query embedding + 17 aux features |
| `cross-encoder.onnx` + `cross-encoder-vocab.txt` | TinyBERT-L2 INT8 cross-encoder | reranks the composite candidate pool (RRF fusion) |

## `mlp-weights.bin`: retrained on embeddinggemma

The kit embeds with local Ollama `embeddinggemma` (768-dim), so this MLP is
retrained on embeddinggemma embeddings (production training pipeline, on
LongMemEval) to be calibrated to that space. On the bundled seed user this
improves retrieval hit@10 from 0.90 to 0.96 and recall@10 from 0.415 to 0.454
versus the Vertex-`text-embedding-005`-trained weights production uses.

Same binary format and shape as production (`aux_dim=17, output_dim=8`), so it
loads via the harness `MlpPredictor` with no code changes. The production
training pipeline is not distributed, but the artifact format is fully
documented (this file plus the harness's `mlp.rs` loader), so you can retrain a
replacement for any embedder with your own pipeline and drop the binary in.

## `cross-encoder.onnx`: identical to production

The cross-encoder scores raw `(query, memory)` text, so it is
embedder-independent and byte-identical to the production model.
