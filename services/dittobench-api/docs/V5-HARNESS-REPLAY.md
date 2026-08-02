# v5 ship-gate replay against real leaderboard harnesses (4.11)

The current top leaderboard harnesses were run, **unmodified**, against `bench_version`
5 (full) and scored by the real dittobench-api pipeline. Harnesses were fetched from
the platform (backroom `get_screening_artifact`), built from source (`docker build`),
and driven locally: memory embeddings via Ollama `embeddinggemma`, chat via the locked
model **Qwen3-32B** (OpenRouter `qwen/qwen3-32b`, the validator key), memory **and**
tool cases observed through the validator's mock tool endpoint.

Raw numbers: `docs/v5-harness-replay.json`.

## Result — v5 SEPARATES grounded champions from routing exploiters

The full current top-of-leaderboard cohort, run unmodified against v5 (Qwen3-32B):

| harness | v4 (leaderboard) | v4-view here | **v5 composite** | conv-sanity | verdict |
|---|---|---|---|---|---|
| **mnemo-v6** (champion, `d2a307a4`) | 0.988 | 0.97 | **0.87** ✓ | **1.00** | grounded — **stays champion** |
| ditto-scratch (`1fddf196`) | 0.949 | 0.92 | 0.46 | 0.00 | router — drops |
| ditto-fan-v0 (`dea14c11`) | 0.953 | 0.94 | 0.47 | 0.00 | router — drops |
| pandas (`2b260c75`) | ~0.91 | 0.91 | 0.45 | 0.00 | router — drops |
| zeus_v8 (`977782fa`) | 0.988 | 0.88 | 0.43 | 0.00 | router — drops |
| gggggggg v3 (`81266c17`) | ~0.86 | 0.86 | 0.42 | 0.00 | router — drops |
| tada (`2b870216`) | 0.944 | 0.75 | 0.37 | 0.00 | router — drops |

"v4-view" is the ungated `0.5·tool + 0.5·memory` (≈ the v4 composite). The result is
NOT a blanket difficulty hike — it is a **separation**:

- **mnemo-v6, the #1 champion, is a genuinely grounded harness** (reads memory, handles
  greetings/declaratives correctly): `conversational_sanity = 1.0`, so it PASSES the v5
  gate and stays a champion at **0.87**.
- **Every other top harness is a phrase-list router**: `conversational_sanity = 0`
  (leaks on greetings, never captures a no-save-verb declarative), so its composite is
  halved to **0.37–0.47** — all below 0.85. Both 4.11 tiers hold for the exploiter
  cohort while the honest champion is preserved (the counter-guard).

### Running these harnesses locally (reproducibility)

Faithful reproduction requires matching each harness's runtime. Two chat patterns
exist: DIRECT (gggggggg/pandas read `OPENROUTER_API_KEY` and call OpenRouter) and
GATEWAY (mnemo-v6/zeus read `HARNESS_GATEWAY_URL` — the validator's model-relay).
For the gateway harnesses, run `cmd/model-relay` (`RELAY_PROVIDER=openrouter`) and point
`HARNESS_GATEWAY_URL` at it. **On Docker Desktop / WSL2, the relay must NOT use port 9100
(Docker Desktop intercepts it and returns HTTP 426) and must bind IPv4** — mnemo-v6 (the
champion) scored a broken 0.09 until both were fixed (its chat calls all failed with 426);
with the working relay it scores its true 0.87.

## Raising the ceiling — hardening even the grounded champion (v5 4.9 / 4.6)

Because mnemo-v6 saturates almost every category at 1.0, two new capability
dimensions were added to keep a capability gradient at the champion boundary:

- **multi-hop relational retrieval (KG join, 4.9):** answerable only by joining two
  memories across sessions (`sister → Dana → puppy <coined>`), with a wrong-relative
  decoy. mnemo-v6 scores **0.75** here (fails 1 of 4) — winnable but hard, the single
  strongest discriminator between shallow and real retrieval.
- **temporal-depth (4.6):** the SECOND-most-recent value in an update chain ("what was
  my color before I changed it to X"), with the latest and oldest as distractors.

Adding these dropped mnemo-v6 from 0.872 to **0.854** and exposed multi-session
synthesis as its genuine high-variance weak spot (0.48–0.90 across seeds). Both new
classes clear the 4.10 winnability precheck against the strongest harness (mnemo-v6
scores in a band, not 0).

## Why they drop — the phrase-list-router signature, confirmed

The drop is entirely the conversational-sanity gate: `conversational_sanity = 0` for
every champion triggers the factor's 0.5 floor (verified: gggggggg's post-gate composite
0.4218 / pre-gate 0.8605 = **0.490** gate). The drop is *specific*, not a blanket
difficulty hike — the counter-guard holds:

| category | gggggggg | pandas | reading |
|---|---|---|---|
| `single-session-recall` | 1.00 | 1.00 | strong ordinary recall |
| `multi-session` | 0.80 | — | strong retrieval |
| `memory-write-read` (EXPLICIT save verb) | **1.00** | **1.00** | captures explicit "remember/save" |
| `declarative-write-read` (NO save verb) | **0.00** | **0.00** | never captures a plain statement |
| `declarative-behavior` (apply the preference) | **0.00** | **0.00** | can't apply what it never stored |
| `conversational-chitchat` (greeting non-leak) | 0.50 | 1.00 | leaks stored values on a greeting |
| `injection-resistance` | 1.00 | 1.00 | resists injection (retrieval-as-data) |

The tell is unambiguous: both champions **ace the explicit-save-verb lifecycle (1.00)**
yet score **exactly 0.00 on the no-save-verb declarative write** — they recognize memory
writes only through the closed cue family and ignore ordinary declarative statements,
exactly the v4 exploit v5's 4.1/4.2 workstreams target.

Concrete "Aurora-9" evidence (gggggggg per-case notes):
- greeting → `"answer dump: surfaced 4 off-answer self values (scored 0)"` — a plain
  greeting force-routed into recall, dumping stored memory.
- declarative statement → `"abstained on an answerable question (scored 0)"` — the user
  states a fact; the router doesn't capture it and later abstains.

mnemo-v6 (a "pure-LLM" harness with no retrieval stack) is simply a weak harness across
the board (memory 0.16), low on v4 and v5 alike.

## How the run was optimized

Each case is a real Qwen3-32B call (~13s with reasoning), so throughput is the
constraint. Two env knobs (no code change) drove ~4× speedup and parallel harnesses:
- `DITTOBENCH_CASE_CONCURRENCY=8` — cases run concurrently within a run (wave barrier
  preserved).
- `DITTOBENCH_MAX_CONCURRENT_RUNS=3` — all three harnesses scored in parallel.

All three full runs (206 cases each) completed together in **~5 minutes** vs ~2+ hours
sequential, no rate limits.

## Local-run fixes (this session)

Two blockers were fixed to score a containerized harness via `harness_url` locally:
- The v5 screened-image contract wrongly gated `harness_url` direct runs, which never
  build source — exempted in local-dev mode (`allowPrivate`).
- The tool endpoint bound dual-stack `[::]`, unreachable from a container via Docker
  Desktop's IPv4 host-gateway — forced IPv4, plus a `DITTOBENCH_TOOL_HOST` override so a
  containerized local harness can reach it.
