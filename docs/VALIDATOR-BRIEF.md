# SN118 validator brief (2026-07-10)

For the lead independent validator, to relay to other validators. Engineering
critical path: [ROAD-TO-PRODUCTION.md](ROAD-TO-PRODUCTION.md). Doc index at the
end.

## Facts to relay

1. Scoring is deterministic. No LLM judge, no validator-side API key. A score
   is a pure function of (seed, transcript); anyone can re-grade a published
   transcript offline with the public `dittobench-datagen` module (v0.4.0).
2. The fleet standard for the locked model is **Chutes FP8**:
   `Qwen/Qwen3-32B-TEE`, served in attested Intel TDX with per-token model
   verification, reached through the local `model-relay`. A scoring validator
   needs zero GPUs; at Chutes' Qwen3-32B pricing ($0.104/M input, $0.416/M
   output) a full run's 10^5-10^6 tokens costs under $0.50. Local Ollama/vLLM
   remains a supported fallback but does not bit-match FP8, so it must not mix
   with relay-backed validators in the same k=3 set.
3. Weights-only validators need no GPU, no key, no benchmark data. Env:
   [RUNNING-A-VALIDATOR.md](RUNNING-A-VALIDATOR.md). KOTH knobs are consensus
   parameters (margin 0.05, champion share 0.9, tail 4); run defaults.
4. bench_version stays 2 until after launch. Dataset hashes moved with datagen
   v0.4.0 on 2026-07-10; older cached hashes are stale.

## Pipeline

```
miner: starter-kit fork -> local eval -> hosted practice (keyless)
  -> ditto upload (signed tarball + on-chain fee)
platform: payment verify -> screener (docker build + /health)
  -> k=3 tickets pinning (seed, dataset_sha256, run_size, deadline);
     seed from an on-chain block hash fixed after the miner commits
validator (x3): dittobench-api /v1/score
  -> regenerate dataset (hash mismatch fails loudly) -> build crate in sandbox
  -> run cases (observed execution) -> deterministic grade -> signed report
platform: median of 3 -> ledger -> weights-only validators fold KOTH
  -> put_weights via Pylon -> chain
```

## Scoring, concretely

- Tool cases: 0.4 name-F1 + 0.4 arg-F1 + 0.2 order/extra-call discipline, on
  the validator-observed trajectory. Unobserved observable cases cap at 0.5.
  Result-usage cases also require the served needle value in the answer.
- Memory cases: graded per `answer_kind` (value, number, list, ordered_list,
  duration, reversal, decline) against the response's `answer` slot with
  `final_text` fallback. Zeroed by: any forbidden value (isolation leak,
  injection payload, canary bait), any distractor value (wrong same-attribute
  value, or a pool value on a decline case), or abstaining on an answerable
  case.
- Composite: 0.5 tool_mean + 0.5 memory_mean, times the observed
  tool-efficiency factor (≤1). Latency is measured and advisory.
- Signature: sr25519 over `hotkey:agent_id:run_id:composite:seed`, verified by
  the platform at write time. Ledger: `GET /api/v1/scoring/scores`.

## Infrastructure state (dev VM, ditto-validator-dev)

Live now:

- C-ISO egress enforcement: isolated `ditto-sandbox` network
  (172.31.240.0/24), fail-closed CONNECT proxy, DOCKER-USER firewall.
  Verified active 2026-07-10.
- dittobench-api on the judge-free build (converged from main).
- Validator worker, screener, and Pylon identity sidecar on dev localnet;
  full pipeline runs unattended; champion selected by Yuma consensus.

Staged in infra (`feat/validator-role-split`), flips on at the first converge
after the Chutes key exists:

- `dittobench_model_lock: true`: sandbox scores against `Qwen/Qwen3-32B-TEE`
  only, egress allowlist derives to empty (deny-all CONNECT), no key in any
  run.
- `ditto-model-relay` unit on :11435: pins the model field, injects the Chutes
  key from Secret Manager, forwards to `llm.chutes.ai`. Embeddings stay on the
  VM's Ollama at :11434 (`HARNESS_EMBED_URL`).

**TODO (Nick):** create the Chutes API key and store it as the
`validator-chutes-key` Secret Manager value in `ditto-app-dev`:
`printf '%s' 'cpk_...' | gcloud secrets create validator-chutes-key
--data-file=- --project ditto-app-dev`. Then re-converge
(`ansible/playbooks/gcp-validator.yml`); the lock and relay activate
themselves.

## Remaining gates to finney (validator-visible)

1. Chutes key + lock flip on dev (above), then the enforcement smoke test in
   infra `docs/validator-deploy.md`.
2. Noise-floor calibration at Qwen3-32B: 30 seeds, one real harness, confirm
   between-seed composite sigma clears the 0.05 KOTH margin. Grading
   contributes zero; what remains is dataset + harness-execution variance.
3. Platform: `/validator/job` ticket migration complete, `composite_stderr`
   in the ledger (activates the SE dethroning band + CRN re-scores already in
   this repo), transcripts + artifacts published to the public bucket for
   third-party re-grading.
4. Median-of-3 convergence proof with three validators (F-MV).
5. Finney cutover per the runbook (no testnet), commit-reveal re-enabled,
   verified Pylon identity-write.

Not gating: starter kit adopting the `answer`/`abstain` slot and the public
grader for local eval parity; MINER-FAQ still citing the 1% margin and judged
grading (code is authoritative); 17 dependabot findings in this repo (3 high).

## Watch items

- Cross-validator noise is now harness execution only. k=3 disagreement beyond
  the calibrated band means a gateway mismatch (backend, quantization), not
  grading.
- Chutes is a third-party dependency under the FP8 standard: catalog churn,
  serving upgrades, uptime. TEE attestation makes serving observable; the
  relay makes the backend swappable to local GPUs with no other change.

## Doc index

| Question | Doc |
|---|---|
| Run a weights-only validator | [RUNNING-A-VALIDATOR.md](RUNNING-A-VALIDATOR.md) |
| Host the locked model / hardware | [VALIDATOR-MODEL-HOSTING.md](VALIDATOR-MODEL-HOSTING.md) |
| Exact grading rules | dittobench-api docs/judge-determinism.md + PROTOCOL.md |
| Model lock enforcement | dittobench-api docs/model-lock.md + docs/sandbox-egress-hardening.md |
| Provisioning runbook | infra docs/validator-deploy.md |
| Reproduce a dataset / re-grade a run | dittobench-datagen README (public) |
| What miners build | dittobench-starter-kit README + PROTOCOL.md |
| Incentives, KOTH, anti-copy | [incentive-mechanism.md](incentive-mechanism.md) + [MINER-FAQ.md](MINER-FAQ.md) |
| Engineering critical path | [ROAD-TO-PRODUCTION.md](ROAD-TO-PRODUCTION.md) |
| Live status | `GET /api/v1/public/leaderboard`, `/public/health`, `GET /api/v1/scoring/scores` |
