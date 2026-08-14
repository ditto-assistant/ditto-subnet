# Private Bench v10 qualification

`cmd/v10-qualify` evaluates an external, content-addressed manifest and result
file. The repository contains no fixture identities, miner source, excerpts,
prompts, challenge values, or private evidence. The CLI requires the exact
manifest SHA-256 approved through the operator rollout boundary and performs no
activation or external write.

The manifest must cover every compiler, router, model-retry, deterministic
repair, preselected-tool, semantic-agent, lineage, and uncertainty class. Each
complete fixture carries paired mutation measurements for the tool and memory
axes. Semantic credit separately gates renderer invariance, causal sensitivity,
counterfactual model dependence, model tool selection, endpoint execution,
result use, and post-model insertion. Transport failures and inconclusive
results are rendered as `uncertain`; they never become evidence of guilt or a
benchmark pass. Any uncertainty on a non-control fixture blocks readiness.

The retained evaluator remains useful for independently rechecking the private
content-addressed qualification evidence and performs no scorer configuration,
release, deployment, or activation step. Runtime releases may advertise and
execute v10 after that gate passes; capability advertisement still does not
open or activate a Platform rollout.
