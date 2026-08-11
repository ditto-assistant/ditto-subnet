# SN118 quarantine decision rules

## Evidence priority

1. Served runtime dispatch and answer construction.
2. Exact reachable source locations.
3. Validator/inference-broker observations and behavioral findings.
4. Artifact hashes, ownership, duplicate manifests, and normalized diffs.
5. Reproducible build/runtime behavior.
6. Score shape and miner claims only as investigation leads.

## Release boundary

Release when suspicious code still uses the current request, current user's
memories, live schemas, a real model, and real endpoint execution. Public-bench
prompt specialization, broad family classifiers, reranking, schema-derived
arguments, context expansion, and replacement harnesses are not violations by
themselves. Bugs should score poorly; they are not anti-cheat findings.

Exact or near-identical source is not automatically theft. Check payment-time
owner/coldkey lineage and attestations first. Same-owner revisions and shared
starter code are legitimate unless another independent violation exists.

## Rejection boundary

Reject when a reachable scored path:

- maps finite requests, phrases, IDs, seeds, canaries, or hidden facts to
  benchmark answers;
- replaces model output with deterministic expected-answer or grader-matching
  logic;
- manipulates answer, abstention, or trace fields solely to satisfy scoring;
- fabricates, inserts, removes, or rewrites tool calls without executing the
  live tool selected from the current request and schema;
- embeds stolen credentials, private challenge material, or another unrelated
  miner's distinctive artifact; or
- loads opaque executable data proven to contain one of those bypasses.

For static logic, ask whether it generalizes to renamed/paraphrased requests,
whether the real model makes the substantive choice, and whether a live tool is
actually invoked. Reject only when exact evidence answers those questions
adversely under the quarantine's policy version.

## Failure handling

Infrastructure outages, registry/DNS faults, daemon failures, lease expiry,
resource exhaustion, and deploy interruption are not miner violations. Leave
the quarantine unchanged unless rescreening was separately authorized.

## Reason quality

- Release: explain why the flagged evidence does not bypass the model/tool path.
- Reject: name the active policy category, cite the minimal decisive file/line
  set, and describe the deterministic or fabricated path without disclosing
  challenge values.
- Escalate: state the missing or contradictory evidence and do not mutate.
