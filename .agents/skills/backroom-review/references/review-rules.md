# SN118 quarantine decision rules

## Evidence priority

1. Served runtime dispatch and answer construction.
2. Exact reachable source locations.
3. Validator/inference-broker observations and behavioral findings.
4. Artifact hashes, ownership, duplicate manifests, and normalized diffs.
5. Reproducible build/runtime behavior.
6. Score shape and miner claims only as investigation leads.

## Release boundary

For policy v9 and earlier, release when suspicious code still uses the current
request, current user's memories, live schemas, a real model, and real endpoint
execution under the historical two-limb plus production-engine bar.

For policy v10, those facts are necessary and not sufficient. Independently
apply I4 derived-value authority, I5 production generality, and I7 model tool
planning. An explicitly untrusted candidate beside complete records may pass;
an authoritative derived value, closed family/operand recipe, or host-predicted
exact tool count/set/order does not. Reranking, schema validation, context
expansion, safety/authorization filtering, and natural singleton capability
classes remain legitimate. Bugs should score poorly; they are not anti-cheat
findings.

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

Policy-v10 rejection additionally includes a causally proved I4, I5, or I7
breach, even when a real model call occurs and the historical two limbs pass.
The finding must name the policy-v10 invariant and the applicable pass clause it
considered and refuted.

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

## Adjudicating a finding-backed hold

The L2/L3 finding (categories, summary, invariant_assessment) is a LEAD, not
the verdict. It over-flags a recurring set of legitimate patterns, so the
`agentic-source-review-tripwire` reason code is never sufficient on its own.
Before you reject:

1. **Read every decisive cited line in source.** `get_screening_quarantine_context(s)`
   for the finding, then `read_screening_source_file` on the exact evidence
   `path:line`s (and their callers, via `search_screening_source`). A rejection
   quotes lines you actually read.
2. **Find the answer-authority question.** Under policy v10 the decisive breach
   is host authority over the graded slot: reject only when a reachable scored
   path (a) injects a host-derived value as an accept-until-match / reject-until-
   match gate over the model's answer (I4), (b) overwrites the scorer-visible
   `answer`/`final_text`/`tool_calls` with host-composed content (I3/I6), or (c)
   feeds a closed benchmark-family operand/answer recipe or an exact host tool
   plan to the deciding turn (I5/I7). If the finding's own assessment marks I4
   `no_derived_value` AND I5 `no_family_compiler`, a lone I2/I3/I6/I7 flag rarely
   clears the bar — read those lines specifically and expect a false positive.
3. **Check same-owner precedent.** `miner.recent_quarantines` carries prior
   release/reject reasons. The exact machinery is often already adjudicated;
   cite it. A resubmission that REMOVED the previously-rejected gate (e.g. I4
   flips to `no_derived_value`) is a good-faith fix, not the same violation.

### Known court false positives (release with a cited refutation)

- **Retrieval/ranking as an "answer-family engine."** A group ranker,
  cross-encoder rerank, candidate-pool/`long_term_limit` tuning, or fused vector
  retrieval flagged as I5 — legitimate context expansion. (But a static prompt
  that enumerates the benchmark's per-family operand formulas and output
  formats — "net effect = correction − charges + credits", exact money/direction
  rendering — IS a real I5 family compiler, even delivered as prose. Read the
  prompt text.)
- **Content-complete memoization cache flagged as cross-user (I2).** If the cache
  key includes the system prompt and that prompt already embeds the current
  user's retrieved records, the key is user-specific by construction; a hit
  returns a genuine prior answer to a byte-identical grounded request.
- **Standard broker/inference client flagged credential_access / provider_bypass
  / data_exfiltration (L3 safety).** Reading the harness's own
  `OPENROUTER_API_KEY` and preferring the injected `DITTOBENCH_INFERENCE_BASE_URL`
  broker is the intended audited path, not theft.
- **Empty self-reported `tool_calls` flagged fabricated_tool_trajectory (I6).**
  When calls are actually executed against the supplied endpoint and the harness
  reports `[]` so the OBSERVED broker trajectory stands, that is safe harbor —
  agent-returned traces are not proof; the observed trajectory is authoritative.
  Fabrication is reporting calls that did NOT execute, not declining to report.
- **Local practice-harness stubs.** A tool stub that only fires when no
  `tool_endpoint` is supplied is dev-only; the scored run supplies the endpoint,
  so the stub is unreachable on the graded path.
- **Grammar/intent routing or a precursor pass flagged as I7 by itself.** A
  deterministic grammar router, lightweight per-intent classifiers, advisory
  relevance signals shown to the model, or a pre-pass LLM that only informs is
  legitimate when the deciding model still sees the catalog and makes the
  calls. I7 fails only when an enforced plan predetermines the executed tool
  count/set/order against a deciding turn that cannot deviate, skip, add, or
  reorder (see the owner rulings in review-bar.md; live precedent
  `lets_v609`, rejected then reinstated, re-adjudication deferred to the
  scheduled policy-v11 rescreen). The I7 planner-forced letter applies to
  policy v11 only: a row screened under v10 is not retroactively judged by
  it.
- **`bench_version` branching flagged as benchmark fingerprinting.** Behavior
  that switches on `bench_version` is a warning sign warranting a second
  look, never an independent reject; require a companion violation on the
  served path.
- **Single-success duplicate suppression flagged as fabricated trajectory
  (I6).** Suppressing a repeat of a side-effecting tool after a genuine first
  successful execution (read-only tools exempt) is production idempotency,
  not telemetry fabrication.
- **Plain answer normalization flagged as scorer manipulation.** Unicode
  folding, slot-length caps, and whitespace normalization of the model's own
  output are scorer-fidelity gaps, not violations, unless normalization is so
  aggressive the served text no longer represents the model's answer.

When evidence is genuinely mixed (a lone borderline invariant, source ambiguous,
ownership unresolved), leave it held rather than forcing a reject or release, and
say what would resolve it.
