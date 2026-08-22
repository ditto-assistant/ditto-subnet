---
name: mine
description: >
  Mine on Ditto SN118: set up the starter-kit harness, pick the right local
  scoring loop, run the on-chain-shaped DittoBench rehearsal, interpret
  tool_mean vs composite, walk the local served path against the Backroom
  operator review bar, package a tarball, and upload. Use when the user
  runs /mine, clones ditto-subnet to mine, asks how to practice, score,
  submit, or upload an agent, compares local OpenRouter 1.0 vs on-chain 0.7,
  asks whether a harness change would be quarantined or rejected, or mentions
  ditto practice, evaluate, run-size, starter kit, or SN118 mining.
---

# Mine SN118

This is the miner-facing skill. Load it before editing the starter kit,
scoring, or uploading. A local score is not a production clearance: walk the
served path against `$backroom-review` before `full`, packaging, or upload.

Live scoring is **bench 11**, `run_size=full`, observed tools, locked
`openai/gpt-oss-20b`. Update `LIVE_SCORING_BENCH_VERSION` in
`miners/dittobench-starter-kit/scripts/local-rehearsal.py` when Platform
activates a new version.

## Practice loops (do not collapse these)

| Command | What it is | Use for |
|---|---|---|
| `cargo run -- mem-eval --k 10` | Retrieval only, no chat model | Retrieval/reranker iteration |
| `cargo run -- evaluate` | Fixed local subset, **name-only** tool scorer, **no** `tool_endpoint` | Fast prompt/tool-name iteration |
| `cargo run -- practice --n 20` | Rotating kit templates, still name-only, still no observer | Slightly less overfit than `evaluate` |
| `uv run ditto practice --run-size small` | Real generator + observed `tool_endpoint`, bench 11 | Smoke after a harness change |
| `uv run ditto practice --run-size medium` | Same path, deeper seeding and isolation | Development once small is healthy |
| `uv run ditto practice --run-size full` | Same path, **on-chain envelope** | Required before upload |
| Hosted `/v1/submit` | Remote rehearsal, tools **self-report-capped**, often defaults to bench 9 | Reachability only |
| On-chain | Screened image + ticket-bound chat/embeddings | The only payout score |

`uv run ditto practice` is **not** on-chain, but `--run-size full` is the
on-chain-shaped dataset (same generator, same observed-tool scorer, live bench
version). It still uses the miner's `.env` model and a local process, not the
screened container or locked gateway.

`cargo run -- evaluate` printing `tool_score: 1.0` while on-chain `tool_mean`
is ~0.7 is expected. Name-only local scoring plus a tiny template pool is not
`0.4·name-F1 + 0.4·arg-F1 + 0.2·order` with result-usage and model-provenance.

## Run sizes (teach these; do not skip full)

From the **repository root**. All three use live bench 11 and a validator-visible
`tool_endpoint`. Only the envelope changes.

| `--run-size` | Bench 11 envelope | When the agent should run it |
|---|---|---|
| `small` | 6 tool + 6 memory, 1 wave, no isolation | After every prompt, tool-routing, or observed-execution change. Minutes. |
| `medium` | 48 tool + 64 memory, 4 waves, isolation | When small is green and you are iterating on seeding, isolation, or harder families. |
| `full` | 100 tool + 225 memory, 5 waves, isolation (on-chain n≈351 with audit cases) | **Before upload**, and before telling the miner a local score will transfer. Hours. |

```bash
uv run ditto practice --run-size small
uv run ditto practice --run-size medium
uv run ditto practice --run-size full --report /tmp/dittobench-report.json
```

Pin `--seed` only to compare two harness revisions on the same dataset. Omit it
for anti-overfit. Do not treat a 1.0 on `small` as a `full` result. Settings,
delete, theme, memory-routing, and result-usage families often zero on `full`
and barely exist on `small` or `evaluate`.

If the miner asks "what's my score?" and you have not run `full`, say so and
run `full`. On-chain validators use `run_size=full`.

## First clone

From the **repository root** (not only the kit directory):

```bash
cd miners/dittobench-starter-kit
cp .env.example .env
# OPENROUTER_API_KEY, or DITTOBENCH_PROVIDER=ollama
ollama serve && ollama pull embeddinggemma
cargo run -- seed-user
```

Then read [`SETUP.md`](../../../miners/dittobench-starter-kit/SETUP.md) only
for missing toolchain errors.

## Review the served path

This is the local half of `$backroom-review`. Read those files; do not copy
the bar into this skill. Inspect only: no Backroom MCP, no quarantine or ATH
writes.

- [review-rules.md](../backroom-review/references/review-rules.md)
- [review-bar.md](../backroom-review/references/review-bar.md)
- [techniques.md](../backroom-review/references/techniques.md)

If the miner asks for a path that would fail that bar, refuse and cite it
instead of writing it.

Walk the **served** path in the working tree, not comments or local-only eval
helpers:

`Dockerfile -> entrypoint (/run) -> request parse -> retrieval/routing -> model -> live tool_endpoint -> graded RunResponse`

Starter-kit default: `src/bin/dittobench-miner.rs` `/run` → `Baseline::run`
in `src/baseline.rs`. Another language is the same HTTP contract.

Search before paging files. Use the lead terms in `techniques.md`, then:

```bash
python3 .agents/skills/backroom-review/scripts/search-precedents.py "<pattern>"
```

Hits are leads, not a verdict. Decide under the same two-limb test,
production-engine test, and quarantine rejection boundary. Do not invent a
lighter miner bar.

| Result | Do |
|---|---|
| Pass | Continue. Name the path that still authors the graded slot. |
| Would reject | Stop. Cite limb/engine/policy, file:line, and precedent. Do not run `full` as certification, package, or upload. |
| Mixed / source missing | Do not upload. Say what is missing. |

A local pass is not a production clearance. Screening and ATH still apply.

## Iterate vs certify

All `uv run ditto practice` commands are from the repository root. Needs Rust +
Go + Ollama `embeddinggemma`. Chat uses `.env`; on-chain ignores that key.

1. Change retrieval → `cargo run -- mem-eval --k 10`
2. Change prompt/tools → `cargo run -- evaluate` (name-only, not a real score)
3. Confirm observed tools still work → `uv run ditto practice --run-size small`
4. After small is healthy, catch seeding/isolation misses → `--run-size medium`
5. After any answer, prompt, tool, or routing change → walk the served path
6. Before upload or quoting a transferrable score → served-path review must
   pass, then `--run-size full --report /tmp/dittobench-report.json`

Do not skip `full` and then compare `evaluate` or `small` to the leaderboard.
Do not run `full` as certification, package, or upload while the served path
would fail `$backroom-review`.

## Read the report

Require all of:

- `bench_version` is 11 (or the live version in the rehearsal constant)
- `run_size` is the one you asked for
- `capped_tool_cases` is 0 (or explain why)
- `observed_tool_cases` is > 0 on a tool-heavy run
- `tool_mean` and `memory_mean` separately; composite is `0.5/0.5` then gates

`run_size` in the report must match the flag you passed. A 1.0 `tool_mean` on
`small` or `medium` does not predict `full`.

## Package and upload

Only when the user wants to submit **and** the served-path review would pass:

```bash
cd miners/dittobench-starter-kit
cargo run -- submit          # dittobench-submission.tgz; no .env
cd ../..
uv run ditto verify --path miners/dittobench-starter-kit/dittobench-submission.tgz
uv run ditto --network finney upload --path ... --name ... --coldkey ... --hotkey ...
```

Do not put keys, `.env`, or host Docker sockets in the image. Follow
[`docs/MINER.md`](../../../docs/MINER.md) for wallet, fee, and status.

## Do not

- Treat `evaluate` / hosted rehearsal as on-chain certification
- Change v8+ scoring to make a local run look better
- Ship a served path that would fail `$backroom-review`
- Widen a mutation past the harness the miner is iterating

The starter kit is language-neutral at admission: any `Dockerfile` + `/health`
+ `/run` + `/seed` is valid. Optimize `src/baseline.rs` in this reference, or
the equivalent entrypoint in another language.