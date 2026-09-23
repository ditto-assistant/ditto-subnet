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
The binary classification summary also keeps unclassified safe and violation
cases separate from true negatives and false negatives. Violation recall counts
all labeled violations in its denominator, including incomplete reviews;
classification coverage exposes non-completion rather than crediting it as a
pass. False-positive rate uses only terminally classified safe controls.
The private per-artifact result also retains the validated, sanitized I1–I8
assessment (disposition, pass clause, summary digest, and cited path/line),
without source text or prompts. S1–S3 do not have an equivalent structured
per-clause result in this reviewer; categories or an absent finding must not be
reported as proof that those clauses were covered or passed.

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

## Exact private handoff to paired L4 replay

`--handoff-file` is an opt-in private sidecar for the report-only paired L4
replay. It requires policy v13 and, on **every selected case**, the exact
Backroom `agent_id`, `attempt_id`, artifact SHA-256, manifest digest, and
positive review-settings revision. The complete selected manifest is checked
before the first model call; an agent UUID or SHA alone is not an attempt.
The operator must obtain those identifiers from the exact attempt and freeze
the matching archive. The exporter does not query or authenticate Backroom.

```json
{
  "revision": "private-v13-corpus-v1",
  "items": [
    {
      "agent_id": "11111111-1111-4111-8111-111111111111",
      "attempt_id": "22222222-2222-4222-8222-222222222222",
      "artifact_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "manifest_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "review_settings_revision": 119,
      "archive": "relative/path/agent.tar.gz",
      "expected_disposition": "safe"
    }
  ]
}
```

Add `--handoff-file /private/results/single-sol-handoff.json` to a future
bounded calibration run. Use a fresh output path; the runner refuses an
existing handoff. This does not itself run the L4 model. The handoff
sidecar is atomically written mode 0600 under a mode-0700 directory and
contains only exact identity, the 0–48 bounded typed notes, their canonical
JSON digest, the validated canonical finding and digest (or both null),
error code, elapsed time, model/prompt revision, compaction count, and reported
cost. It never writes source files, prompts, tool arguments, or model
transcripts. Notes and findings may still describe private miner code, so
keep both this file and the original results private; do not commit or paste
them. Unmetered responses, failed requests, or a served-model mismatch set
handoff cost to null, which makes the two-layer arm incomplete rather than
free. Historical result files containing only a finding digest cannot be
expanded into a canonical finding without a new review.

The separate **offline** attachment step makes a new private copy of an
independently labeled, frozen [paired L4 manifest](https://github.com/ditto-assistant/ditto-subnet/pull/2164)
with the exact `case.sol_investigator` object expected by PR #2164:

```bash
uv run --project workers/screener \
  python workers/screener/scripts/attach_single_sol_handoff.py \
  --l4-manifest /private/l4-corpus.json \
  --handoff-file /private/results/single-sol-handoff.json \
  --output /private/results/l4-two-layer-corpus.json
```

Both inputs must be non-symlink mode-0600 files. Every L4 case and Sol
handoff must match one-to-one on UUID, attempt, SHA, policy, manifest digest,
and settings revision; a preexisting handoff is never overwritten. The
attachment step neither invents an independent label nor supplies missing
mandatory host evidence. The L4 runner's dry run must validate the new
manifest before any separately authorized paid replay. These files are
calibration artifacts, never screening decisions or V13 CLEAR authority.
Recommend one or two model layers only from those paired results. Issues
#2094 and #2117 track current adjudicator reliability and artifact-bound
verification gaps; neither is resolved merely by a larger model context.
