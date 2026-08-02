# DittoBench v5 coverage map — production capabilities → benchmark

Goal: a high leaderboard score must mean the agent behaves like a strong Ditto
agent in real chat. This maps each production capability (anchored in the `backend`
repo) to how the benchmark exercises it, and marks the remaining gaps as Phase B/C.

Legend: ✅ covered (pre-v5) · 🆕 added in v5 Phase A · ⏳ Phase B/C.

## Tool-calling capabilities (tool suite: `datagen/datagen.go`, `catalog/catalog.go`)

| Production capability | backend anchor | Benchmark coverage |
|---|---|---|
| Image gen / edit (`create_image`, `edit_image`) | `chatv2-tools.go:178,187` | ✅ `image_create`, `multi_image_edit` |
| Web search (`search_web`) | `chatv2-tools.go:206` | ✅ `web_search`, `route_web_not_memory`, result-usage |
| Link reading (`read_links`) | `chatv2-tools.go:198` | ✅ `link_read`, `multi_web_read` |
| **Ditto Code — coding harness (`execute_agent_job`)** | `chatv2-tools.go:312` | ✅ `agent_job`, `agent_run_not_read`, `job_chain_result_usage` |
| Multi-agent workflow (`execute_agent_workflow`) | `chatv2-tools.go:353` | ✅ `agent_workflow`, `workflow_not_job` |
| **Code Mode — JS compute/orchestration (`run_code`)** | `pkg/codemode/meta.go:19`, `chatv2-tools.go:1398` | 🆕 `code_compute`, `code_compute_not_agent_job` |
| **Code Mode — tool discovery (`search_tools`)** | `pkg/codemode/meta.go:46` | 🆕 `tool_discovery` |
| Capability discovery (`discover_capabilities`) | `chatv2-tools.go:250` | ✅ `capability_discovery` |
| Settings / NL preferences (`set_theme`, `set_reasoning_effort`, …) | `chatv2-settings-tools.go` | ✅ `settings` (+ intent variants) |
| Automations / recipes | `chatv2-automation-tool.go`, `chatv2-recipe-tool.go` | ✅ in `catalog`; ⏳ dedicated routing cases |
| Abstain / no-tool on small talk | `system-prompt.af:8-19` (no forced search) | ✅ `abstention`, `arg_hallucination`; 🆕 conversational-sanity |

Code Mode is the meta-tool mode (activated at >40 MCP tools) where the model writes
JavaScript calling `mcp.<tool>()` bindings. v5 represents it two ways: a pure
in-context calculation routes to `run_code` (`code_compute`), and a task phrased to
tempt the heavy coding harness must still route to `run_code`, not
`execute_agent_job`, when it needs no fs/network/repo (`code_compute_not_agent_job`)
— mirroring `RunCodeComputeToolDef` vs Ditto Code (`pkg/codemode/meta.go:73`).

## Memory capabilities (memory suite: `persona/`, `gen/`, `grade/`)

| Production capability | backend anchor | Benchmark coverage |
|---|---|---|
| Semantic recall, single & cross-session | `pkg/mcp/list_memories.go` | ✅ single-session-recall, multi-session |
| Multi-query fan-out (`search_memories` `queries[]`) | `chatv2-tools.go:218`, `precursorprime.go` | ✅ (implicit in multi-session); ⏳ 4.6 explicit multi-query-only cases |
| Knowledge update / recency over staleness | passive capture, no supersede on passive path | ✅ knowledge-update, contradiction, point-in-time |
| Temporal reasoning over the timeline | multi-session day offsets | ✅ temporal-reasoning, ordering, reversal |
| Passive capture of plain statements (no save verb) | `chatv2.go:1924` (every turn captured) | 🆕 declarative-write → persistence-read → behavior-change |
| Memory writes (`save_memory`/`update_memory`/`delete_memory`) | `pkg/mcp/memory_*.go` | ✅ lifecycle write/read chains |
| Multi-graph isolation (own KG + subscribed + app KGs) | `pkg/services/kgauth/`, `pkg/mcp/dedicated_graphs.go` | ✅ isolation cases; ⏳ 4.6 cross-graph provenance depth |
| Memory-as-data vs instructions (indirect injection) | attacker-influenceable store | ✅ injection-resistance; ⏳ 4.8 extend to stored-instruction payloads |
| Subject/KG promotion, multi-hop relational | `pkg/services/sync/kg.go:149` | ⏳ 4.9 multi-hop relational retrieval |
| Integrity / anti-exfil | canary + bait | ✅ canary (nonce + bait), dump guard |

## Chat-experience (the "respond to 'hi' correctly" requirement)

| Behavior | backend anchor | Benchmark coverage |
|---|---|---|
| Greeting / small talk → natural reply, no memory dump | `system-prompt.af:2`, no forced search | 🆕 conversational-chitchat (non-leak floor) |
| Plain declarative → acknowledge (echo the value) | onboarding "acknowledge you'll remember it" | 🆕 conversational-declarative (echo) |
| Never-stated attribute → abstain, not confabulate | "Do not skip search and guess" | 🆕 conversational-abstention (decline over neighbor) |
| Behavior change from a stored preference | passive capture + application | 🆕 declarative-behavior |
| First-class conversational-sanity metric (weakest link) | — | 🆕 `conversational_sanity` + bounded factor (floor 0.5) |
| Warmth / concision (positive quality) | `realtime-system-prompt_af.go:60` | ⏳ out of scope for a judge-free grader (plan §7) |
| Proposal-gated actions ("make it dark mode" → a proposal, not a silent apply) | `system-prompt.af:28-33` | ✅ `settings` routing; ⏳ explicit proposal-vs-apply grading |

## On-model validation (this session)

The frozen reference model Qwen3-32B (`qwen/qwen3-32b`, the locked harness model)
was run as a full-context honest reader against real generated v5 datasets via
`cmd/v5onmodel` (validator OpenRouter key from gcloud). Two seeds: overall
model-mean 0.68–0.77 vs grep-parser 0.48 (gap +0.20 to +0.29), with the
conversational classes at the intended +1.00 gap (chitchat, declarative echo,
declarative write). See `docs/BASELINES-v5-phaseA.md`. This confirms v5 winnability
(non-degenerate ceiling) and a positive honest-minus-parser gap on-model.

## Data volume / seed variety (audit + v5 scale-up)

- v4 full: 60 tool / 50 mem / 3 waves / ~199 haystack pairs.
- v5 full: **110 tool / 85 mem** / 4 waves / 0.4 raw-pairs / 6 isolation / ~238
  haystack pairs, with a larger persona (`personaOptsFor` n>55 tier) so distractor
  density rises (plan 4.3). v2/v3/v4 sizes are frozen (`ProfileForVersion`).
- Seed variety: every submission draws a fresh crypto-random seed
  (`gen.FreshSeed`), folded per bench version (`RotateSeedForVersion`), so the
  surface rotates per run and per version; generation stays byte-reproducible from
  `(seed, bench_version)`. v5 additionally **doubles the thin tool content pools**
  (topics, URLs, image prompts, agent tasks, workflow goals, artifact kinds,
  calendar titles — `poolV5`/`fillerForVersion`) and expands the conversational
  greeting / declarative / abstention / preference-domain surfaces, cutting the
  per-seed memorization surface without changing routing difficulty.

### Seed-difficulty variance guardrail (measured, `cmd/benchcal --version 5`)

The constraint on this pass was to add cases and variety WITHOUT widening
between-seed difficulty variance. Result — it narrowed it, because more cases at
the fixed stratified mix average out per-category noise:

| config | tool_mean σ (60 seeds) | memory_mean σ |
|---|---|---|
| v4 mix, n=60 | 0.0429 | — |
| v5 mix, n=60 | 0.0375 | — |
| v5 full, OLD n=80 | 0.0372 | ~0.010 |
| **v5 full, NEW n=110/85** | **0.0300** | **0.0101** |

Adding the Code Mode categories did not raise variance (v5 mix < v4 mix at equal
n), and the entity-pool expansion is difficulty-neutral (a different URL is the
same routing difficulty). Memory σ stays ~0.01 (stratification). Artifact:
`docs/benchcal-v5-toolsuite.json`. benchcal is now version-aware (`--version`).

## Remaining Phase B/C (tracked in BENCHMARK-V5-PLAN.md)

4.3 non-verbatim answer rendering (accept-set primitive already landed) · 4.4 live
passive-learning multi-turn with a deterministic sync barrier · 4.6 explicit
multi-query + temporal-depth + cross-graph provenance · 4.7 efficiency waste factor
· 4.8 stored-instruction injection · 4.9 multi-hop relational KG · dedicated
automation/recipe routing and proposal-vs-apply grading.
