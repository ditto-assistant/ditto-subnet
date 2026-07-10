# SN118 validator brief: current state and next steps (2026-07-10)

Audience: the lead independent validator, as the briefing to relay to other
independent validators. It covers what the pipeline is today, what changed this
week, what each validator role does now and at mainnet, and what is still in
flight. The engineering-facing critical path lives in
[ROAD-TO-PRODUCTION.md](ROAD-TO-PRODUCTION.md); this document is the
validator-facing view and defers to it where they overlap. Doc pointers are at
the end.

## TL;DR to relay

1. Scoring is now fully deterministic. There is no LLM judge, no validator-side
   API key, and no grading noise in the k=3 median. A score is a pure function
   of (seed, transcript), and anyone can re-grade a published transcript
   offline from the public generator module.
2. The hardware floor for scoring validators collapsed. The locked harness
   model is Qwen3-32B: one 24 GB GPU self-hosted, or zero GPUs via the
   model-relay backed by Chutes (SN64) TEE inference. Weights-only validators
   still need no GPU and no key.
3. The full loop runs unattended end to end on the dev localnet, and the
   migration target is finney netuid 118 directly (no testnet). The gates are
   listed below.
4. bench_version is 2 and does not move until after production launch; all
   pre-production hardening has shipped under it.

## The pipeline in one diagram

```
miner: fork starter-kit -> edit baseline.rs -> local eval -> hosted practice
   |
   |  ditto upload (signed tarball + on-chain eval fee)
   v
ditto-platform: payment verify -> store -> screener gate (docker build + /health)
   |  k=3 tickets, each pinning (seed, dataset_sha256, run_size, deadline);
   |  seed derived from an on-chain block hash fixed AFTER the miner commits
   v
3 scoring validators -> dittobench-api POST /v1/score on their own hosts:
   regenerate dataset from seed (hash must match, else fail loudly)
   -> docker-build the crate -> /seed -> /run per case (observed execution)
   -> deterministic grading -> signed ScoreReport
   v
platform ledger (median of 3) -> weights-only validators fold KOTH
   -> put_weights via Pylon -> Bittensor chain
```

Repos: `dittobench-datagen` (public generator + grader, single source of
truth), `dittobench-api` (private scoring engine; also the hosted practice
endpoint), `ditto-platform` (intake, queue, ledger, leaderboard),
`ditto-subnet` (this repo: miner CLI, validator and screener workers),
`ditto-harness` + `dittobench-starter-kit` (public miner stack).

## What changed this week (all merged and deployed)

Judge-free scoring (dittobench-api 782853c, dittobench-datagen v0.4.0):

- Memory cases carry typed grading data (`answer_kind`, `answer_items`,
  `distractor_answers`) and grade deterministically: value, number, list,
  ordered list, duration, reversal, and decline checks, with distractor and
  forbidden-value zeroing. The grader is the public
  `dittobench-datagen/grade` package.
- Tool cases score their deterministic trajectory accuracy (0.4 name-F1 +
  0.4 arg-F1 + 0.2 order and extra-call discipline, observed execution,
  needle checks). The judged quality half is gone: under the model lock every
  miner runs the same model, so it measured the model, not the miner.
- `RunResponse` gained an optional `answer` slot and `abstain` flag
  (additive; prose containment is the fallback, old harnesses keep scoring).
- Removed outright: judge prompts, judge models and the audit slice, the
  judge outage gate, judge prompt-injection as an attack class, and the
  per-request OpenRouter key. The composite stays
  0.5 tool + 0.5 memory, times the tool-efficiency factor.

Locked model shrink + Chutes gateway:

- `HARNESS_MODEL` default is now `qwen/qwen3-32b` (was Qwen2.5-72B).
  Judge-free scoring removed the last reason for a large model, and a smaller
  model differentiates retrieval quality at least as well.
- New `model-relay` binary (dittobench-api `cmd/model-relay`): a GPU-less
  gateway that forces the model field to the locked id and holds the
  operator's Chutes key outside the sandbox. Lock semantics identical to a
  local gateway. Chutes serves `Qwen/Qwen3-32B-TEE` in attested Intel TDX
  with per-token model verification, at well under $1 per full scoring run.
- The lock's owned env keys now include the Chutes and OpenAI provider
  selectors, so a crate cannot route around the locked model through any
  supported provider.
- Miner side: the starter kit gained `DITTOBENCH_PROVIDER=chutes`
  (starter-kit PR #11, rebased and merged).

Verification: keyless end-to-end runs (reference harness, small and medium
profiles, every case category) produced byte-identical per-case scores across
repeated runs of one seed. All suites green across the three Go/Rust repos.

## Current state by component

| Component | State |
|---|---|
| dittobench-datagen | Public, MIT, v0.4.0. Non-LLM, byte-reproducible from (seed, bench_version). Known vector pinned in CI. |
| dittobench-api | Judge-free scorer merged and auto-deployed; hosted practice endpoint live and keyless. Docker build path runs on validator hosts only. |
| ditto-platform | Intake, payment verify, screener, k=3 ticket leasing, signed score ledger, public leaderboard endpoints. |
| ditto-subnet worker | Role-split scoring/weights loops, KOTH fold (margin 0.05, champion 0.9, tail 4), Pylon weight sink, commit-reveal aware. |
| starter kit | v2 parity + Chutes provider. Local eval still uses its own local judge (parity follow-up below). |
| Chain | Dev localnet runs the full loop unattended; a champion has been selected by real Yuma consensus. Production netuid 118 not yet live. |

## What validators do now

Weights-only (the role most independents run):

- No GPU, no LLM key, no benchmark answers. Read the public ledger, fold
  weights deterministically, submit via Pylon. Env in
  [RUNNING-A-VALIDATOR.md](RUNNING-A-VALIDATOR.md).
- The KOTH knobs are consensus parameters; run defaults unless a change is
  announced. Deviation gets clipped by Yuma.
- Verify, don't trust: `GET /api/v1/scoring/scores` is self-verifying
  (sr25519 over `hotkey:agent_id:run_id:composite:seed`), and any published
  transcript can now be re-graded offline with dittobench-datagen. GPU-less
  spot-audits of grading are possible today; re-executing a run to audit the
  transcript itself needs one 24 GB GPU or a Chutes key.

Scoring validators (k=3 quorum members):

- Pick a gateway backend and standardize with the fleet:
  A) Ollama `qwen3:32b-q4_K_M` on one 24 GB card, digest-matched fleet-wide.
  B) vLLM with a pinned HF revision.
  C) model-relay + Chutes (no GPU).
  The quantization is part of the consensus: FP8 on Chutes and Q4 locally do
  not bit-match each other, so the fleet picks ONE option for scored runs.
  Details in [VALIDATOR-MODEL-HOSTING.md](VALIDATOR-MODEL-HOSTING.md).
- Do not flip `DITTOBENCH_MODEL_LOCK=1` until the egress firewall and gateway
  are provisioned together (dittobench-api docs/model-lock.md and
  docs/sandbox-egress-hardening.md).

## Next steps, in order

Validator-relevant gates to production (the full ordered checklist is
ROAD-TO-PRODUCTION.md §2; items here are the ones validators see or act on):

1. Fleet decision: one consensus gateway option (A local Q4 vs C Chutes FP8)
   and, for A/B, matching artifact digests across all scoring validators.
2. Provision the lock: gateway + egress firewall + `DITTOBENCH_MODEL_LOCK=1`
   on every scoring host, `openrouter.ai` dropped from the egress allowlist.
   This removes the last key from the pipeline.
3. Noise-floor calibration at Qwen3-32B: a 30-seed run with a real harness to
   reconfirm between-seed composite sigma against the 0.05 KOTH margin.
   Grading noise is now zero, so whatever remains is dataset plus
   harness-execution variance.
4. Platform: finish the queue-to-ticket (`/validator/job`) migration, surface
   `composite_stderr` in the ledger (activates the SE-aware dethroning band
   and CRN re-scores already wired in this repo), and publish per-run
   transcripts and dataset artifacts to the public bucket so third parties
   can exercise the offline re-grade path.
5. Median-of-3 proof: three validators converging on the KOTH champion
   (ROAD-TO-PRODUCTION F-MV), now with grading noise structurally at zero.
6. Finney migration per the cutover runbook (no testnet), commit-reveal
   re-enabled on production, verified Pylon identity-write.

Parity and cleanup (not launch-gating):

7. Starter kit: adopt the `answer`/`abstain` slot in baseline.rs and replace
   the kit's local LLM judge with the public deterministic grader, so local
   `evaluate`/`practice` matches on-chain grading exactly.
8. Doc drift sweep: MINER-FAQ still cites the 1% margin and judge-based
   grading in places; the starter kit's PROTOCOL.md copy needs the
   answer-slot fields. Code is authoritative until then.
9. Chutes hardening if option C is chosen: a deploy-our-own-chute guarantee
   for catalog stability, a scoped key, and periodic attestation checks. The
   relay makes the backend swappable back to local GPUs with no other change.
10. Repo hygiene: 17 open dependabot findings in this repo (3 high); recent
    doc pushes to main used admin bypass of the PR rule and can get
    retroactive review. Superseded: dittobench-api PR #11 (closed with a
    landing map) and the 2026-07-02 snapshot in NEXT-STEPS.md where this
    brief and ROAD-TO-PRODUCTION disagree with it.

## Watch items

- Chutes is a third-party dependency inside the scoring loop under option C:
  catalog churn, serving-stack upgrades, and uptime all matter. TEE
  attestation makes the serving observable; the relay makes it swappable.
- Harness-execution variance is now the ONLY cross-validator noise source. If
  k=3 medians disagree beyond the calibrated band, suspect a gateway config
  mismatch (quantization, serving stack), not grading.
- bench_version 2 dataset hashes moved with datagen v0.4.0 (grading fields are
  part of the artifact). Any cached hashes from before 2026-07-10 are stale.

## Doc index (what to send someone)

| Question | Doc |
|---|---|
| How do I run a weights-only validator | [RUNNING-A-VALIDATOR.md](RUNNING-A-VALIDATOR.md) |
| How do I host the locked model, what hardware | [VALIDATOR-MODEL-HOSTING.md](VALIDATOR-MODEL-HOSTING.md) |
| How is anything scored, exactly | dittobench-api docs/judge-determinism.md + PROTOCOL.md |
| What is the model lock and how is it enforced | dittobench-api docs/model-lock.md + docs/sandbox-egress-hardening.md |
| How do I reproduce a dataset or re-grade a run | dittobench-datagen README (public repo) |
| What do miners build | dittobench-starter-kit README + PROTOCOL.md |
| Incentives, KOTH, anti-copy | [incentive-mechanism.md](incentive-mechanism.md) + [MINER-FAQ.md](MINER-FAQ.md) |
| Scoring decentralization decision | dittobench-api docs/scoring-decentralization-brief.md |
| Engineering critical path | [ROAD-TO-PRODUCTION.md](ROAD-TO-PRODUCTION.md) |
| Live status endpoints | `GET /api/v1/public/leaderboard`, `/public/health`, `GET /api/v1/scoring/scores` (self-verifying ledger) |
