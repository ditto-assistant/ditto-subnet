# Report-only single-Sol review

This experiment tests whether one persistent GPT-5.6 Sol tool-using investigation
can replace the production Luna/Terra/Sol model-review path. It does not replace
the deterministic artifact, image, runtime, behavioral-oracle, or mandatory
policy-v13 private verification checks. It is calibration-only: it
does not lease Platform work, change signed screening verdicts, clear or reject
submissions, enable a release setting, or write to Backroom.

The reviewer uses the existing policy-versioned source-review tools and strict
result schema with a larger envelope: 240 model/tool steps, 16 MB of delivered
source, 32,000 completion tokens per turn, high reasoning, and a one-hour
deadline. Each case is SHA-256-bound to an immutable `safe` or `violation` gold
label. Results include classification metrics, latency, provider/model identity,
token usage, reported cost, typed notes, and finding metadata. A separate
disposition matrix keeps inconclusive and infrastructure failures out of the
certified-safe count and reports failure-code frequencies. Request attempts,
responses, and unmetered responses are counted separately; reported cost is
not a total bill when a response lacks provider cost data.

## Context compaction

The agent loop keeps the stable system prompt and artifact inventory plus the
three most recent assistant turns. Older turns are replaced with a host-built
checkpoint even if the model has not recorded a note yet. The checkpoint
contains:

- the complete bounded typed-notes ledger;
- bounded inspection provenance (tool and path, never source contents);
- served-path coverage obligations that remain open; and
- consumed step and read-byte budgets.

Dropped tool output is not converted into evidence. The checkpoint explicitly
requires the reviewer to re-read exact source before citing it. This prevents a
long exploratory sequence from growing without bound while keeping clearance
and finding evidence tied to the artifact.

## Run

```bash
uv run --project workers/screener \
  python workers/screener/scripts/run_single_sol_calibration.py \
  --manifest /private/gold/manifest.json \
  --artifact-root /private/gold/artifacts \
  --api-key-file /private/keys/openrouter \
  --results-file /private/results/single-sol.json
```

The manifest uses the report-only calibration shape:

```json
{
  "revision": "immutable-gold-v1",
  "items": [
    {
      "agent_id": "exact-agent-uuid",
      "archive": "relative/path/agent.tar.gz",
      "artifact_sha256": "64-lowercase-hex-characters",
      "expected_disposition": "safe"
    }
  ]
}
```

Use `--artifact-sha256` for a bounded retry and `--concurrency 1` (the default)
for attributable latency and cost. A replacement decision requires a measured
corpus comparison against the current chain: false clears, false holds, missed
rejections, inconclusive rate, evidence validity, latency, and cost. Unit tests
or a reader replay are not production-readiness evidence.

## Paired comparison gate

Freeze a private, SHA-bound corpus only after re-reading each exact Backroom
review. Exclude reopened or pending rulings and distinguish an operator release
from an independently certified policy-v13 clear. Stratify by policy invariant,
benign retrieval/tool controls, artifact size and language, and ambiguous
source. Reserve a holdout and repeat runs to measure variance. A purposive
pilot or a violation-heavy historical corpus cannot establish population
false-hold performance.

Run the current production model path and each candidate on the same frozen
artifacts and policy revision, with a matched confidential verification profile
and fresh hidden seeds where policy requires them. Compare terminal disposition,
the *causal* invariant, cited source locations,
served-path reachability, and evidence validity. Report false clears, false
holds, missed findings, inconclusive/timeout rate, required-tool-call failures,
stream-without-verdict rate, p50/p95 latency, metered and unmetered requests,
and billed cost per artifact. Incomplete verification is a hold, never a clear.
Historical Backroom outcomes are useful baselines but do not substitute for a
same-revision paired replay or provide full per-layer cost and finding traces.

The optional two-model candidate keeps the same persistent Sol investigation,
then invokes an independent, bounded adjudicator only for a proposed terminal
finding or unresolved hold. That second model must be free to request operator
review when evidence is incomplete; a forced CLEAR/REJECT contract is not
acceptable. Measure its incremental correction of Sol false clears/holds
against its own no-verdict, latency, and cost. Mandatory deterministic v13
verification remains a separate gate in both candidates, not a model layer.
The current runner implements only the one-model arm; no two-model result is
implied by its output.
Recommend one or two model layers only from those paired results. Issues
#2094 and #2117 track current adjudicator reliability and artifact-bound
verification gaps; neither is resolved merely by a larger model context.
