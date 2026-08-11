# Kimi/GLM/SOL level-2 review with SOL critic and adjudicators

Level 2 is an optional escalation behind the bounded Luna source review. It is
disabled by default. A clean or advisory-only L1 result never pays its cost;
medium/high Luna findings and elevated static preflight matches become
artifact-bound L2 routing leads. Static leads are resolved before untrusted
Docker execution; they are no longer treated as terminal proof. A Kimi
violation retains quarantine. A primary-Kimi safe
result can clear directly only when a deterministic certificate verifies a
medium-risk L1 lead, confidence of at least 0.98, complete L1-file coverage, a
targeted analyzer call, no violation evidence, and a context-to-authoritative-
decision-to-answer-sink path. An obvious single-mechanism Kimi violation stops
at L2 only when Kimi has not narrowed away a distinct L1 mechanism family.
Benchmark-family or mixed benchmark, scorer, tool, provider, scope, credential,
or exfiltration leads use a bounded, medium-reasoning SOL cause adjudicator so
quarantine records the primary causal mechanism rather than whichever
downstream effect was easiest to name.
High-risk/static, fallback-model, incomplete, or
lower-confidence safe results require the independent GPT-5.6 SOL L3 critic. A
critic-safe result remains provisional and is independently checked against the
original L1 evidence by a second, low-reasoning SOL trajectory. A critic
challenge is checked by that same independently bounded trajectory against both
the clearance and challenge. Only a certificate-gated adjudicator-safe result
may clear either path. A served-runtime constellation that independently binds
generator-shaped request templates, retrieval/selection, and deterministic
answer authority cannot be released by model agreement alone; it remains a
non-terminal quarantine recommendation and skips needless SOL clearance calls.
When that deterministic hold contains multiple mechanism families, SOL may
adjudicate its cause for detailed feedback but cannot overturn the hold.
The same fail-closed hold applies when runtime source explicitly describes a
review channel and probe shape next to the model call that implements the
special route. Review terminology, retry logic, or comments alone are
insufficient: the three location-bound facts must co-locate in executable
source. This preserves ordinary reliability retries and ignores decoy docs
while preventing multiple agreeing models from clearing review-adaptive code.
Any inconclusive result or infrastructure failure cannot clear.
Neither layer can reject or ban.

## Model and budget contract

The OpenRouter catalog was verified on 2026-07-18:

- L2 ordered models: `moonshotai/kimi-k3` (canonical
  `moonshotai/kimi-k3-20260715`), then `z-ai/glm-5.2` (canonical
  `z-ai/glm-5.2-20260616`), then `openai/gpt-5.6-sol`;
- L3 critic and cause/safety adjudicators: exact model `openai/gpt-5.6-sol` (canonical
  `openai/gpt-5.6-sol-20260709`), with model fallback disabled;
- Kimi reasoning is mandatory and currently exposes only its `max`/model-default
  level. GLM supports `high` and `xhigh`; the shared failover request leaves each
  L2 model at its model default. SOL L3 uses `medium` reasoning for the
  clearance critic and violation-cause adjudicator, and `low` for the bounded
  safety-disagreement adjudicator. Mixed benchmark/scorer leads promote that
  final adjudicator to `medium` reasoning;
- privacy routing: ZDR required and data collection denied;
- Kimi context is 1,048,576 tokens and costs $3/M input, $0.30/M cached input,
  and $15/M output. GLM context is 1,048,576 tokens and currently costs
  $0.2674/M input, $0.04966/M cached input, and $0.8404/M output. SOL context
  is 1,050,000 tokens and
  costs $5/M input, $0.50/M cached input, and $30/M output below its
  272,000-token price tier.

The screener allows 425,000 cumulative effective input tokens (uncached plus
10% of cached input), 20,000 cumulative output tokens, 2,400 output tokens per
turn, 18 model turns and at most 36 analyzer calls per analyst/critic trajectory,
eight cause-adjudicator turns and at most 16 analyzer calls, six safety-
adjudicator turns and at most 12 analyzer calls, $2.00
total, and 900 seconds per escalation. The input budget is cumulative across
turns rather than a per-request context size; exact reported cost remains the
hard spend bound.
Raw, cached, cache-write, output, and reasoning tokens plus OpenRouter-reported
cost are recorded separately. The hard cost cap uses OpenRouter's exact reported
cost when present and falls back to the conservative SOL-priced estimate only
when the provider omits cost. A stable instructions/tool/dossier prefix,
`prompt_cache_key`, and artifact-scoped `session_id` maximize provider cache
reuse without enabling response replay caching. The result-cache key includes
all budgets, model/fallback/critic routing, reasoning settings, artifact/L1
digests, prompt revisions `l2-kimi-source-review-v23`,
`l3-sol-adversarial-critic-v15`, `l3-sol-violation-cause-v22`,
`l3-sol-cause-disagreement-v5`, `l3-sol-safety-adjudicator-v19`, and
`l2-integrity-static-hold-v3`, dossier revision
`l1-compressed-dossier-v8`, harness revision
`l2-isolated-coding-harness-v17`, and the supported canonical-starter revision
set. The inert analyzer chooses the canonical v2 or v3 baseline with the fewest
file-digest deltas and reports the selected exact revision in the dossier; this
keeps historical artifacts and current v3 starter submissions comparable
without trusting submission metadata.

Tool-fidelity review distinguishes model selection from transport. A
request-local Tool may return the exact memoized result of its first genuine
endpoint execution for later identical model-selected calls while the model
transcript faithfully records those invocations. Cross-request replay,
argument/result mutation, skipping the first execution, or reporting calls the
model never selected remains a violation.

The retired, non-scored DittoBench v3 reachability preflight has a narrower legacy authority
boundary: attempting the required POST to the request-supplied endpoint is the
harness action, while validator-side observation decides success. A handler may
discard the client response and return a matching self-reported call; that
untrusted report cannot make an unobserved probe pass or affect a score.

An earlier opt-in live acceptance on 2026-07-18 used the exact canonical starter
revision. Kimi made 14 targeted analyzer calls and SOL made 14 independent
critic calls; SOL confirmed safe. The run used 235,990 raw input tokens, of
which 161,152 were cached, 5,192 output tokens (2,609 reasoning), cost
$0.4643748, and took 205.821 seconds. Moonshot AI hosted Kimi and the ZDR Azure
endpoint hosted SOL. No source or transcript was retained.

Forced GLM calibration against the same starter confirmed Responses API and
isolated-tool compatibility, but GLM did not produce a valid clearance within
the bounded trials: separate attempts exhausted effective token, corrective
tool-call, malformed-argument, or step limits. Every outcome remained
`retryable_infra`; none cleared or rejected the artifact, and SOL L3 was not
silently substituted after a model-behavior failure. GLM therefore remains an
ordered availability fallback that must be measured in `shadow`, not a claimed
anti-cheat effectiveness result.

Catalog references:

- <https://openrouter.ai/provider/moonshotai>
- <https://openrouter.ai/z-ai/glm-5.2>
- <https://openrouter.ai/openai/gpt-5.6-sol/>
- <https://openrouter.ai/docs/api/reference/responses/overview>
- <https://openrouter.ai/docs/guides/best-practices/prompt-caching>
- <https://openrouter.ai/docs/guides/routing/provider-selection>
- <https://openrouter.ai/docs/guides/features/zdr>

## Coding harness boundary

Each model starts from a compressed deterministic L1 dossier: complete workspace
digests, canonical-starter diff, inert build structure, bounded binary/source
leads, exact L1 provenance, a conservative reachable-main graph with noisy
unresolved edges sampled and counted, and a snippet-free attention map covering
the answer contract, model/tool authority, service routes, user scope,
benchmark/score signals, a separately sampled generator-construction queue,
mutations, and host/secret access. The separate queue prevents scorer-heavy
files from hiding template, seeded-expansion, distribution, or expected-output
definitions behind the per-surface sample cap. The attention map
is never evidence; it keeps a wrong L1 diagnosis from narrowing the review and
points the agent to locations it must read and causally trace. It then invokes
only the targeted repository-owned, allowlisted analyzers it needs: workspace
index/digests, bounded reads, literal search, Tree-sitter Rust AST structure,
cross-file call graph, complete canonical-starter file and Rust-function AST/body
diffs, a snippet-free Rust scorer-field-flow map, and inert
Cargo/Docker build metadata. The scorer-field-flow analyzer
locates score/A-B-controlled clearing and the prior-population sites that must
be causally checked; it is an attention map rather than policy evidence. Any
nonempty scorer controls/populations/clears force cross-invariant inspection
and prevent direct clearance regardless of L1's category, because L1 can point
at the wrong interface while an independent response-authority violation exists.
Submission source is extracted without links or
path traversal, kept owner-readable only, mounted read-only, and removed after
the review. The analyzer container runs as the worker's non-root UID/GID (and
the image itself defaults to UID/GID 65532) with no network or root execution,
a read-only root filesystem,
all capabilities dropped, `no-new-privileges`, bounded CPU/memory/PIDs, and a
small no-exec scratch tmpfs.

The absolute screening lease deadline bounds every dossier and model-requested
analyzer process as well as every gateway call. Directory/header traversal,
analyzer calls, outputs, and file sizes are independently capped; any omitted
oversized content or traversal truncation makes clearance inconclusive.

Every file, output, AST, function, call, route, and graph bound carries an
explicit truncation marker. A sampled deterministic attention map may still
route the model to exact reads that prove a violation, but it marks the dossier
incomplete and can never support a safe clearance. Partial model-requested
evidence is withheld and cannot support a final claim. Qualified calls are
resolved only to one definition; ambiguous
short names and method calls are reported without manufacturing reachability.

The container receives no environment credentials, cloud paths, host paths, or
Docker socket. The model gateway remains in the trusted host process and is the
only component with approved egress. There is no shell, compiler, package
manager, arbitrary command, build, test, or submission-code execution tool.
Dynamic behavior remains the responsibility of the existing hardened screening
boundary, never a model-requested action.

Before clearing, every applicable trajectory independently closes four invariants: scored
answer-field authority, reported-versus-executed tool fidelity, user/host/secret
scope, and shipped-runtime reachability. A harness-owned memory tool is allowed
when the genuine model loop executes it and its result remains authoritative;
observing that real transcript is not fabrication. Generic user-scoped,
source-grounded timeout degradation is also allowed when it follows a real
provider failure and is not benchmark-, grader-, score-, or challenge-driven.

## Evidence and retention

Each trajectory must return `safe`, `violation`, or `inconclusive` plus one
bounded `resolution_basis`, analyzed file digests, exact path/line/category
evidence, and a trigger-to-effect causal path.
The host verifies every file digest and location against the original artifact.
Violation evidence requires both trigger and effect roles; multi-location policy
categories retain their stricter threshold. Model-authored summaries are
discarded and replaced with generic public-safe text.

The signed finding remains the existing platform-compatible payload. Its digest
is signed with the attempt-bound verdict. A separate private mode-0600 audit
journal binds attempt, artifact, L1/L2 finding digests, model/provider,
prompt/harness/starter revisions, selected response models and hosting providers,
separate analyst/critic/adjudicator tool names, critic and adjudicator
dispositions, clearance path, resolution basis, causal locations,
budgets/usage/cost/cache metrics, duration, result-cache status, and error code.
It never stores prompts, source snippets, tool output, private values, or model
transcripts and defaults to 30-day bounded retention. Cached records contain
only the same sanitized structured result and default to seven days. Complete-
write loops, file locks, and atomic replacement make identical concurrent reviews idempotent across
workers sharing the cache. A provisional-safe analyst stage and complete SOL
critic result are cached separately, so a retryable critic resumes without
paying for Kimi and a retryable adjudicator reruns only that final bounded
trajectory. Cache-hit stages contribute zero new usage to the retry audit.
The Platform-managed `l3_enabled` switch independently skips SOL critic and
adjudicator trajectories while preserving L1 routing, the paid L2 analyst,
budgets, caches, and audit records. With L3 disabled, the L2 analyst result is
authoritative and the audit records `critic_disposition=disabled` plus
`clearance_path=l2_only_l3_disabled`. Existing settings revisions predate the
switch and retain their original checksum while resolving to enabled.
Results produced after the local or platform lease
deadline are discarded as retryable infrastructure failures.

The backward-compatible heartbeat projection now reserves the first half of
source-review progress for L1 and the second half for L2/L3, including static
preflight escalation, so miners do not see a completed review while paid
adjudication is still running. It intentionally exposes no source paths or
private conclusions. Durable per-layer events, public finding cards, signed
packets, and miner-authenticated exhaustive feedback require the append-only
platform contract tracked in issue #224.

## Offline calibration

`scripts/run_l2_calibration.py` accepts a protected SHA-bound manifest plus a
directory of already verified artifacts. It rechecks every tarball digest,
runs the production reviewer and analyzer image with bounded concurrency, and
writes a mode-0600 checkpoint after each case. Retryable infrastructure or model
contract failures can get up to two bounded automatic retries; an L3 retry
resumes from the sanitized Kimi and, when available, SOL-critic stage caches.
Output contains
only disposition, resolution basis,
categories, routing/model/provider metadata, attempts, latency, usage, and
expected-vs-actual matches; it never writes source, prompts, signed URLs, or
transcripts. Duplicate artifacts use the same revisioned cache/lock path, which
also exercises horizontal idempotency. Gold manifests, artifacts, and result
files are operator-private and must never be committed.
The analyzer retains its 30-second, 0.5-CPU production defaults; recorded,
bounded calibration-only overrides up to 300 seconds and 2 CPUs are available
when unrelated local container load would otherwise invalidate the accuracy
measurement. These overrides are not production-latency evidence.

The protected 42-item frozen corpus has three independently adjudicated safe
controls and 39 violation controls. A current deterministic served-runtime
constellation check holds 15 of the 39 violations and zero of the three safe
controls. It is a false-release backstop, not a claim of complete classification
or exact causal attribution. In live model calibration, a generic fake-gateway
probe produced identical observable behavior for a matched benign scorer control
and a known scorer violation, and an earlier 16-item model run produced 15/16
disposition matches. This is why runtime-call presence and model consensus are
not release certificates by themselves. The full current-revision 42-item run
must pass disposition and causal-basis gates before enforcement can be enabled.

## Modes, rollout, and rollback

- `off` (default): L1 behavior and manifest digest are unchanged; no analyzer
  image or SOL request is needed.
- `shadow`: selected L1 findings run Kimi L2 and only safe results that lack the
  strict direct-clearance certificate run SOL L3; they write private audit/cost
  evidence, but the signed L1 result remains authoritative.
- `enforce`: a distinct manifest rotation is signed; L2 safe/violation/failure
  dispositions become authoritative within the quarantine-only policy boundary.

A bounded rollout is: merge without activation; deploy with `off`; durably set
one idle fleet worker to `shadow`; review false positives, inconclusive rate,
p95 latency, tokens, and cost; then enable `enforce` on one worker before
expanding. Do not mix enforcement manifests unintentionally across workers.
The screened-image path and policy 9 are already deployed. DittoBench v4 is
activated, while one of four online validators still reports legacy-v2-only
scorer capability. Rollout must preserve both versioned starter baselines and
current per-agent benchmark authority until that skew is resolved. Existing
submissions, scores, quarantine decisions, and attempts are not migrated or
rewritten. This private rotation affects new or explicitly rescreened attempts
only. Historical rescreening must use an explicit guarded operation, not an
automatic policy-bump side effect. Platform PR #222 is merged, so refused
artifacts remain duplicate owners and terminal rejection reasons survive later
policy-version changes. Durable signed miner-visible review packets and
enforcement-manifest binding are tracked in platform issue #224 and remain
prerequisites for automatic rejection, not for a bounded shadow canary.

Rollback is configuration-only: return workers to `shadow` or `off`, run the
exact-SHA updater, and verify the worker heartbeat/manifest. The updater rebuilds
the trusted analyzer only for `shadow`/`enforce` and rebuilds the prior analyzer
image if a health-checked deployment rolls back.

## Residual limits

Static review cannot prove that a built binary matches reviewed source, recover
all macro-generated/dynamic dispatch, explain opaque learned weights, or prove a
real provider/tool call occurred at runtime. Tree-sitter provides syntax and a
bounded conservative call graph, not whole-program Rust semantic resolution.
OpenRouter and the selected Moonshot/Azure hosting endpoints remain privacy/trust
dependencies even with ZDR routing.
Model review can still miss violations or produce inconclusive results; build and
health success are compatibility evidence, not anti-cheat proof.
