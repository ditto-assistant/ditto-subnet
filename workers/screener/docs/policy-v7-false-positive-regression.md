# Policy v7 false-positive regression

> Historical note: the v2 prompt introduced here used an overly broad
> public-benchmark allowlist. The v3 prompt added opaque-blob and build-path
> handling. The v4 boundary preserves generic optimization while treating
> generator/scorer/audit fingerprints and deterministic runtime shortcuts as
> benchmark emulation. See
> [source-review-policy.md](source-review-policy.md).

## Production evidence

The reviewed batch contained five unique source-safe patterns across six held
submissions. Four unique artifacts were released after manual source review.
The fifth was source-safe but appeared as two active copies under different
miner hotkeys and remained held for originality review.

Every submission persisted the same `audit-awaiting-private-challenge` reason
under one v7 manifest. The source reviewer produced a category tuple, but the
policy engine appended a generic reason and the worker persisted only that last
code plus a finding digest. Exact historical reviewer categories therefore
cannot be reconstructed. The measurable old behavior was:

| Measure | Old v7 | Updated synthetic replay |
| --- | ---: | ---: |
| Source-safe unique patterns held for source safety | 5 / 5 | 0 / 5 |
| Source-safe submissions with a generic source-safety hold | 6 / 6 | 0 / 6 |
| Cross-miner duplicate copies retained as originality risk | 2 / 2 | 2 / 2 |
| Adversarial safety fixtures retained | 6 / 6 | 6 / 6 |

This is a contract replay over synthetic patterns, not a claim that a live
model is perfectly deterministic. A canary rollout must measure actual
quarantine and operator-release rates before full activation.

## False-positive mechanism

The v1 prompt asked whether source implemented general model-backed behavior or
benchmark-specific emulation but did not name allowed documented mechanisms.
Legitimate generic lexical retrieval, user-scoped retrieval, faithful
answer-slot serialization, seed-subject construction, and prompt-injection
defenses could therefore look like shortcuts. Large official seed fixtures and
model binaries had no provenance marker and could look like suspicious static
tables. Finally, all medium/high findings collapsed to one generic reason,
obscuring whether the risk was source safety, private-challenge leakage, or
originality.

## Updated boundary

- The prompt names allowed generic optimization mechanisms and requires causal
  runtime evidence before reporting benchmark emulation. Exact generator,
  scorer, canary, challenge, and audit fingerprints are not covered by the
  allowlist merely because their implementation is public.
- A pinned manifest recognizes only byte-identical official fixture/model files
  at exact paths. Modified or derivative files receive no trust.
- Source-safety private-challenge, malicious-source, behavioral, and
  originality duplicate risks use distinct reason codes.
- A low-risk label cannot clear a malicious category.
- Exact cross-miner duplicates remain a separate durable admission concern; the
  source reviewer never infers ownership from one archive.

## Rollout

Deploy the durable exact-duplicate guard first or in the same maintenance
window. Then canary the source-review revision on a bounded share of new
submissions. Track source-safety quarantine rate, operator release rate by
reason code, originality holds, and reviewer infrastructure failures. Roll back
the screener commit if adversarial canaries clear or malicious-source hold rate
drops unexpectedly; no policy-version or signing change is required.
