# Parallel GLM screening prototype

This offline experiment asks whether cheap independent trajectories improve
candidate recall. It is not wired into worker claims, verdicts, signing, caches,
Platform settings or deployment. It cannot clear, quarantine, reject or ban.

The default hybrid plan combines five GLM 5.3 Flash trajectories (generalist,
answer authority, benchmark engines, tool fidelity, evasion/scope) with up to eight
independent file-group trajectories over the same SHA-bound archive.
Each has the complete versioned production policy, its own transcript and notes,
and the existing inert archive tools and host-side citation validation. Specialists
prioritize different surfaces but still review all policy obligations. No source
is extracted or executed. No analyst sees another analyst's output.

This adapts the file-focused discovery and independent verification scaffold in
Anthropic's [Mythos launch research](https://www.anthropic.com/research/mythos-preview)
(April 7, 2026, "Our scaffold"). Anthropic describes model-ranked file priorities,
parallel agents starting at different files, and a separate verification agent.
Here the ordering is deterministic runtime/build path priority, not an LLM risk
ranker. The application to screening is an experiment; the article does not
establish that GLM can match Mythos or that static policy review can match a
reproducible vulnerability proof.

`--partition files --files-per-group 1` assigns one file to each pass plus a
whole-archive generalist. `--files-per-group 4` uses small groups bounded by
`--group-bytes 64000`; oversized files get their own group. `--partition hybrid`
adds all four specialists, while `--partition specialists` reproduces the initial
prototype. Each group is a starting point: reviewers can inspect every file and
must trace cross-file callers, imports, build wiring and answer/tool sinks.

The planner enumerates the full validated regular-member map, including docs,
configuration and opaque files; it does not infer safety from an extension or
silently rely on the sampled inventory tool. Source priorities order the work;
the count/byte limits then partition that order, without dependency clustering.
`--max-groups` defaults to eight and permits up to 64. Reports retain assigned
paths, total/scheduled/omitted counts, oversized-file count and truncation. An
unopened assigned file or truncated plan prevents an overall `no_findings`.
`opened_assigned_paths` records successful nonempty tool reads, not proof that
all lines or semantics were reviewed. Empty/opaque/unreadable files conservatively
remain incomplete unless a candidate is found. Large files are not line-sharded.

The union of medium/high findings goes to a fresh GLM critic with original-source
access. A minority candidate is retained even if everyone else reports no findings.
`critic_also_flagged` means the critic independently returned a candidate, not that
it proved each analyst allegation or agreed on the exact mechanism.
`unresolved_candidate` retains disagreement and critic failure for human inspection.
`no_findings` requires completed, certified low-risk observations from every pass;
it is an experiment outcome, never a production clearance. Partial results remain
`incomplete`. Exhausted-ledger pass shortcuts do not become no-findings results.

Run from `workers/screener`:

```bash
uv run python scripts/run_fanout_calibration.py \
  --manifest /private/calibration/gold.json \
  --artifact-root /private/calibration/artifacts \
  --api-key-file /private/calibration/openrouter-key \
  --results-file /private/calibration/fanout-results.json
```

The key must have mode 0600. Use already verified private artifacts. Manifest shape:

```json
{"items":[{"archive":"case.tar.gz","artifact_sha256":"FULL_SHA256",
           "expected_disposition":"violation"}]}
```

Labels are `safe` or `violation` and never enter a model prompt. Each case is run
once, with a private atomic checkpoint after each case. Rerunning starts over and
incurs fresh calls; there is no implicit retry/resume. Keep artifacts, labels and
results outside Git. Reports retain sanitized findings and metadata, never source,
prompts, raw responses or model-authored notes.

Defaults: five concurrent passes, 12 turns per pass, 2,400 output tokens per
request, 180,000 tool characters per pass and a 300-second deadline per pass.
The conditional critic has a separate equal budget. The default hybrid plan has
at most 14 passes (five whole-archive, eight groups, one critic), 168 model requests
and 403,200 requested output tokens per artifact. Specialist-only mode retains
the original six-pass/72-request ceiling. Concurrency is configurable from 1 to 32.
Lower concurrency queues passes; the deadline starts when a pass acquires a slot.
There are no automatic transport retries. These are work limits, **not a dollar
cap**: growing input contexts are also billed. Reports sum provider-reported cost
and count unmetered requests; a partial cost must not be read as a total bill.
Provider routing preserves ZDR, denied collection, and required parameter support.
Unsupported model/provider contracts stay incomplete; no privacy relaxation or
model substitution occurs. `--model` explicitly permits another comparison model.

The model ID is `z-ai/glm-5.3-flash` ([OpenRouter catalog](https://openrouter.ai/z-ai/glm-5.3-flash)).
Availability, current prices and provider compatibility must be checked for a live
run; mocked tests do not establish that a ZDR provider serves this configuration.

The report compares the generalist alone, the union of all scheduled passes, and critic
positives against adjudicated labels. This is a matched single-GLM baseline, **not
the production Luna/Terra/SOL chain**. Precision/recall treat incomplete outcomes
as negatives; inspect incomplete rate separately using per-pass outcomes. Inspect
incremental candidates, false positives, causal evidence, actual spend and latency
across a representative private gold corpus, including benign retrieval and tool
controls. Repeat trials to measure variance. Shared-model mistakes are correlated;
parallelism alone does not establish improved detection. No accuracy improvement
has been demonstrated by building the prototype.
