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

Local fixture execution may select `bench_version: 10` on the practice route
only when the scorer starts with `DITTOBENCH_PRIVATE_V10_QUALIFICATION=1`.
This opt-in does not add v10 to capability negotiation, the canonical score
route, release metadata, or activation state.
