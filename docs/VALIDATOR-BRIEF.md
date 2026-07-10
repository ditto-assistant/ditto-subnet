# SN118 validator brief (2026-07-10)

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

## Validator protocol

Scoring role, every sweep (default `VALIDATOR_SWEEP_SECONDS=120`):

1. `POST /api/v1/validator/job` → `204` (no work) or one ticket:
   `{agent_id, run_id, seed, dataset_sha256, run_size, deadline}`. The
   platform leases at most 3 tickets per agent, to distinct validators.
2. `GET /api/v1/validator/agent/{id}/artifact` → presigned tarball URL +
   sha256. Verify the hash before building.
3. `POST localhost:8080/v1/score` on the co-located dittobench-api with
   `{tarball_url, tarball_sha256, seed, dataset_sha256, run_size}` → `202` +
   run id; poll `GET /v1/runs/{id}` to `done`/`failed`. The engine regenerates
   the dataset from `seed` and fails the run on a `dataset_sha256` mismatch.
4. Sign sr25519 over `{validator_hotkey}:{agent_id}:{run_id}:{composite!r}:{seed}`
   (`!r` = Python shortest-round-trip float repr) and
   `POST /api/v1/validator/agent/{id}/score`.

Weights role, at most every `VALIDATOR_EPOCH_SECONDS=3600`, stretched to the
chain's `WeightsSetRateLimit`: `GET /api/v1/scoring/scores` → deterministic
KOTH fold → `put_weights` via Pylon. Under commit-reveal v3 the sink makes the
timelock commit and the chain auto-reveals; there is no separate reveal call.

Harness wire timeouts the scoring engine enforces per run: `/health` 10 s,
`/seed` 5 min per wave, `/run` 60 s per case. Docker build cap: 2 GB memory,
20 min.

## System requirements, cost, latency

| Role | Host | GPU | Extra |
|---|---|---|---|
| Weights-only | 1-2 vCPU, 2-4 GB RAM, Linux, Python 3.11+ | none | outbound HTTPS to platform + chain ws |
| Scoring (FP8 standard) | 4 vCPU, 16 GB RAM, 80 GB disk (reference: GCE `e2-standard-4`, ~$100/mo on-demand) | none | Docker, Ollama (embeddinggemma, CPU), model-relay, Chutes key |
| Scoring (fallback A) | same, plus one 24 GB card | 1x 3090/4090/L4 | Ollama serving `qwen3:32b-q4_K_M` |

Inference cost (FP8 standard): Chutes Qwen3-32B is $0.104/M input, $0.416/M
output; a full run's 10^5-10^6 tokens costs under $0.50, and the sandbox never
holds the key.

Latency per scored run: docker build 2-5 min, then seeding plus 110+ cases run
sequentially. Measured on the localnet proof with a Chutes-hosted harness:
median 13.6 s per case, which puts a full run at roughly 30-40 min wall-clock.
The ticket `deadline` bounds it; a run that cannot finish in time is simply
re-leased.

Chain-side: a registered hotkey with a validator permit and the stake finney
requires for one; the same hotkey signs scores (and screener verdicts, which
is safe: verdict and weight signatures have disjoint formats).

Ledger: `GET /api/v1/scoring/scores`, self-verifying per the signature above.

## Infrastructure state (dev VM, ditto-validator-dev)

Live now:

- Sandbox egress enforcement: each untrusted miner container runs on the
  isolated `ditto-sandbox` docker network (172.31.240.0/24) whose only egress
  is a fail-closed CONNECT proxy, with a DOCKER-USER firewall dropping direct
  dials. Verified active 2026-07-10.
- dittobench-api on the judge-free build (converged from main).
- Validator worker, screener, and Pylon identity sidecar on dev localnet;
  full pipeline runs unattended; champion selected by Yuma consensus.

Staged in infra (`feat/validator-role-split` branch), flips on at the first
converge after our Chutes key exists:

- `dittobench_model_lock: true`: sandbox scores against `Qwen/Qwen3-32B-TEE`
  only, egress allowlist derives to empty (deny-all CONNECT), no key in any
  run.
- `ditto-model-relay` unit on :11435: pins the model field, injects the Chutes
  key from Secret Manager, forwards to `llm.chutes.ai`. Embeddings stay on the
  VM's Ollama at :11434 (`HARNESS_EMBED_URL`).

**TODO (Nick):** create a Chutes API key and store it as the
`validator-chutes-key` Secret Manager value in `ditto-app-dev`:
`printf '%s' 'cpk_...' | gcloud secrets create validator-chutes-key
--data-file=- --project ditto-app-dev`, then re-converge
(`ansible-playbook -i ansible/inventory/validator-static.yml
ansible/playbooks/gcp-validator.yml` in the infra repo); the lock and relay
activate themselves.

This key blocks only OUR dev validator's lock flip and testing. It is not a
blocker for independent validators: weights-only validators need no key at
all, and an independent scoring validator brings their own Chutes key (or
their own GPU on the fallback path) when they stand up their host.

## Remaining gates to finney (validator-visible)

Each item names the repo and the concrete change so anyone can pick it up.

1. Lock flip on dev. Blocked on the Chutes key TODO above. Then run the
   enforcement smoke test from infra `docs/validator-deploy.md` (proxy must
   deny every CONNECT; `curl localhost:11435/health` on the relay; a scored
   run against a harness that requests a different model still gets
   `Qwen/Qwen3-32B-TEE` served).
2. Noise-floor calibration at Qwen3-32B. Submit the unmodified starter-kit
   baseline through `POST /v1/score` on the dev VM for 30 distinct seeds at
   `run_size=full`, then compute the between-seed composite stddev. It must
   clear the 0.05 relative KOTH margin (`VALIDATOR_KOTH_MARGIN`,
   `ditto/validator/config.py`); if it does not, widen the margin or grow the
   full profile. Grading contributes zero variance now, so the number is pure
   dataset + harness-execution spread. Also refreshes the 13.6 s/case latency
   figure for 32B.
3. Platform (ditto-platform repo): finish the `/validator/queue` to
   `/validator/job` ticket migration (contract in this repo's
   `ditto/api_models/validator.py`); add `composite_stderr` per ledger entry
   on `GET /api/v1/scoring/scores` (this repo's SE dethroning band and CRN
   re-score code are already wired and inert until it appears); publish each
   run's transcript + dataset artifact to a public GCS bucket (dittobench-api
   writes artifacts wherever `DITTOBENCH_ARTIFACT_DIR` points; the bucket is
   the drop-in replacement).
4. Median-of-3 proof: stand up two more scoring validators (any mix of
   relay-backed hosts; `VALIDATOR_ENABLE_SCORING=true`,
   `VALIDATOR_ENABLE_WEIGHTS=false`), let the platform lease all three
   tickets per agent, and confirm identical composites in the ledger. Any
   spread beyond gate 2's band means a gateway mismatch.
5. Finney cutover per infra `docs/cutover-runbook.md` (no testnet):
   register on netuid 118, re-enable commit-reveal, verify the Pylon
   identity-write path sets weights on chain.

Not gating, open for pickup:

- Starter kit parity: populate `answer`/`abstain` in `src/baseline.rs`, and
  replace the local LLM judge (`src/judge.rs`, used by `evaluate`/`practice`
  in `src/eval.rs`) with the deterministic rules from
  `dittobench-datagen/grade`, so local scores match on-chain scores exactly.
  Add the two `RunResponse` fields to the kit's `PROTOCOL.md` and
  `src/protocol.rs`.
- Doc drift: MINER-FAQ still cites the 1% margin and judge-based grading;
  code is authoritative (margin 0.05, judge-free).
- 17 open dependabot findings in this repo (3 high).

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
