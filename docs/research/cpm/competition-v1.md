# CPM competition v1: contract

Status: **build contract, 2026-11-18.** This is the `model_context` track:
miners submit small open-weight models that build Ditto's context packet. The
track ships **in shadow at 0 bps**. It is scored and visible but pays nothing.
Emissions start only after P0-S shows the score spread is larger than the
noise (memo §I.10).

Peyton's calls behind it (memo Part 0, 2026-11-18):

- Build the track in parallel with P0, shadow first.
- Bases: **Qwen3.5-0.8B and Qwen3.5-2B** (Apache-2.0), LoRA or full
  fine-tune.
- Weights are **public at submit**, after an on-chain commitment.
- Ditto mirrors accepted models into a **Ditto Hugging Face organization**.

Each component PR implements its section of this document and links back to
it. A change to anything marked **(contract)** needs an edit here in the same
PR.

## 1. Architecture

The track mirrors the **router track**: one trusted Ditto scorer publishes a
ledger, Platform relays it, and validators only fold it in (shadow,
`weight_eligible = False`). Validators never load a model or a GPU. The data
flow is:

1. **Miner.** Pushes weights to their own Hugging Face repo, which stays
   private.
2. **Miner.** Writes the on-chain commitment `dcpm1:<manifest_sha256>`, then
   makes the repo public.
3. **Miner.** Runs `ditto model-context submit`. The CLI sends the repo,
   revision, manifest and a hotkey signature to Platform.
4. **Platform.** Checks the signature and the commitment, fetches the
   manifest and files at the pinned revision, verifies their hashes, and runs
   static screening.
5. **Platform.** Queues `model_context_runs`.
6. **CPM scorer** (an AWS GPU host). Leases work, scores the model on the
   season's seeds (§4), and posts the result.
7. **Platform.** Builds the ledger and serves the board at
   `/model-context/board` and the ledger at `/scoring/model-context-ledger`.
8. **Validators.** Fold the ledger in shadow (§7).
9. **Platform mirror job.** Copies each accepted model to the Ditto Hugging
   Face organization (§6.4).

## 2. Model contract (contract)

### 2.1 Bases

| `base_id` | Hugging Face repo | Pinned revision | Parameters |
| --- | --- | --- | --- |
| `qwen3.5-0.8b` | `Qwen/Qwen3.5-0.8B` | `2fc06364715b…` (resolve to the full SHA at implementation) | 0.87B |
| `qwen3.5-2b` | `Qwen/Qwen3.5-2B` | `15852e8c1636…` | 2.27B |

- Both are Apache-2.0.
- Both are vision-language checkpoints, and the scorer uses them **text-only**.
- The allowlist lives in Platform settings. Adding a base means a contract
  edit and a new `cpm_score_version`.

### 2.2 Submission kinds

- **`lora`:**
  - files: `adapter_config.json` plus `adapter_model.safetensors`;
  - rank ≤ 64;
  - target modules only from the attention and MLP projections of the base
    (`q_proj k_proj v_proj o_proj gate_proj up_proj down_proj`, plus the
    base's linear-attention projections if it has them).
- **`full`:**
  - files: `config.json` plus `model*.safetensors` shards;
  - parameter count within ±1% of the base;
  - architecture identical to the base.
- **Both:**
  - `safetensors` only;
  - no pickle, no `*.py`, no `trust_remote_code`;
  - the tokenizer is always the base's, and tokenizer files in the repo are
    ignored;
  - total size ≤ 6 GiB.

### 2.3 Manifest (`cpm-manifest.json`, repo root)

```json
{
  "schema": "ditto-cpm-manifest-v1",
  "track": "model_context",
  "base_id": "qwen3.5-0.8b",
  "kind": "lora",
  "files": [
    {"path": "adapter_config.json", "sha256": "<hex>", "bytes": 812},
    {"path": "adapter_model.safetensors", "sha256": "<hex>", "bytes": 12345678}
  ],
  "license": "apache-2.0",
  "training_data_attestation": "free text, shown to reviewers"
}
```

- `manifest_sha256` is the SHA-256 of the manifest file's exact bytes.
- `license` must be `apache-2.0` or `mit`.

### 2.4 Inference interface (what the scorer runs)

The model is a **pointwise relevance scorer**. For each (query, candidate)
pair, the scorer renders this fixed template with the base's chat template,
with thinking disabled:

```text
system: Judge whether the Document is needed to answer the Query for this user. Answer only "yes" or "no".
user:   <Query>{query}</Query>
        <Document>{candidate}</Document>
assistant: 
```

- The relevance score is `logit("yes") − logit("no")` at the first assistant
  position. `"yes"` and `"no"` are the base tokenizer's single tokens.
- The candidate text is truncated to 768 tokens. The query is truncated to
  256.
- Candidate text by kind:
  - memory pair: `User: {prompt}\nAssistant: {response}`, with
    `[{timestamp}]` prepended;
  - tool: `{name}: {description}`.
- Execution: bf16, batch size 32, a fixed order (by case, then candidate id),
  `torch.use_deterministic_algorithms(True)` and SDPA math attention. One GPU
  SKU per season.
- Same weights, seed and SKU must give **bitwise-identical scores**. The
  scorer re-runs one seed and fails the run on any mismatch
  (`nondeterministic`).

This interface is deliberately the smallest model that does Ditto's job:
ranking and pruning under a budget. Span extraction and a decide head come in
v2, under a new `cpm_score_version`.

## 3. Submission and priority (contract)

### 3.1 Commitment

The commitment is the subtensor `Commitments` pallet value for the miner's
hotkey on netuid 118, with the exact text `dcpm1:<manifest_sha256>`.

- Platform reads the commitment and its block. A submission is accepted only
  when the commitment matches its `manifest_sha256`.
- **Priority = the commitment block.**

### 3.2 Signed payload

```text
ditto-model-context-v1:{hotkey}:{hf_repo}:{revision}:{manifest_sha256}:{ts}:{nonce}
```

The payload is signed with the hotkey. `revision` is a full 40-hex commit SHA,
not a branch name.

### 3.3 Duplicates

- **Exact duplicates:**
  - `weights_sha256` is the SHA-256 over every tensor in every
    `safetensors` file of the submission. Tensors are sorted by name, and
    each is written as

    ```text
    u32le(len(name)) name_utf8
    u32le(len(dtype)) dtype_ascii      # safetensors dtype string, e.g. "BF16"
    u32le(ndim) u64le(dim) * ndim
    u64le(len(data)) data              # raw little-endian bytes
    ```

    The hash does not depend on shard layout, file names, header order or
    `__metadata__`, so resharding the same weights keeps it.
  - When two submissions share a `weights_sha256`, the one with the earlier
    commitment block owns it, and the later one is rejected
    (`duplicate_weights`).
- **Near duplicates** (behavioral and ΔW distance, memo §II.6.3 layers 3–4)
  are v2. In shadow they are logged, not enforced.

### 3.4 Limits

- One active submission per hotkey. A new one supersedes the old.
- Cooldown: `upload_cooldown_seconds`, 6 h by default.
- No fee in shadow (`upload_fee_required = false`). The fee path reuses
  `competition_fees["model_context"]` when enforced.

## 4. Task and score, `cpm_score_version = 1` (contract)

### 4.1 Seasons and seeds

- A season lasts `season_days` (7 days by default).
- Season seeds: `seed_k = uint64(sha256("dcpm-season:{season}:{k}:{season_open_block_hash}")[:8])`
  for `k ∈ {0, 1, 2}`.
- `cmd/generate -seed` takes a signed int64. Platform stores `seed_k` as a
  uint64 and passes the generator its int64 two's-complement value (a value
  x ≥ 2^63 becomes x − 2^64).
- Every submission in a season is scored on the same three seeds, so scores
  within a season are directly comparable.
- At rotation, the scorer re-scores every submission still on the board.

### 4.2 Cases

- For each seed, the scorer runs the public DittoBench generator:
  `cmd/generate -bench-version {bench_version} -seed {seed_k} -run-size full`.
  `bench_version` is pinned per season; v18 at launch.
- Candidates and gold come from `docs/research/cpm/export_cpm.py`, so local
  rehearsal and the scorer read identical labels.
- **Memory cases:**
  - Only cases with at least one `relevant_pair_id` that are not `abstain`.
    Absence cases are left out of v1.
  - The candidates are the case's `candidate_pair_ids`: every pair its user
    holds when the question runs. For the primary user (`miner`), that
    includes the tool cases' `prerequisite_pairs`, which are seeded before
    the tool phase and kept into the memory phase. On v18 seed 42 they hold
    56% of the relevant ids.
- **Tool cases:**
  - Only cases with a non-empty `shortlist_target`.
  - The candidates are the seed's whole catalog.

### 4.3 Metrics per seed

- **Memory recall@B** (`B = 2,048` tokens of the base tokenizer):
  1. Sort candidates by score, descending; ties go to the lexically smaller
     `pair_id`.
  2. Pack whole candidates while the running token total stays ≤ B. Skip a
     candidate that doesn't fit and keep going.
  3. `recall = |packed ∩ relevant| / |relevant|`, averaged over cases.
- **Distractor rate:** the share of packed candidates that contain a
  `distractor` or `forbidden` span (export labels) and are not relevant,
  averaged over cases.
- **Tool recall@8:** `|top-8 tool names ∩ shortlist_target| / |shortlist_target|`,
  averaged over cases.

### 4.4 Score

```text
seed_score = 0.6 · memory_recall@B · (1 − 0.5 · distractor_rate) + 0.4 · tool_recall@8
score      = mean over the 3 seeds
```

- The score lies in [0, 1].
- The run also reports every component, the per-seed scores, and the token
  counts.

### 4.5 Floor

- Each season, the scorer scores each **unmodified base**, through the
  template above, as a reference.
- A submission is **eligible** only if its score is at least
  `floor(base) + floor_margin` (0.01 by default).
- When no submission clears the floor, the track folds nothing (§7).

### 4.6 Why this v1

- Every number is deterministic and needs no reader model, so no provider
  calls and no judge.
- It measures the two jobs from Peyton's thesis: retrieval, with recall of
  needed facts, and pruning, under a budget and with a distractor penalty.
- Both parts prune hard: about 507 memory candidates per case into 2,048
  tokens, and a 1,454-tool catalog down to 8.
- The answer-quality-per-token score (Q@B through a reader, memo §I.7) is v2.
  It is added once P0-A confirms which reader to use.

## 5. Platform (contract for wire shapes)

The pattern follows the generator and router competitions; anchors are in the
component PRs.

### 5.1 Tables

| Table | Purpose |
| --- | --- |
| `model_context_settings_revisions` | Settings |
| `model_context_submissions` | `hotkey`, `hf_repo`, `revision`, `manifest` (jsonb), `manifest_sha256`, `weights_sha256`, `base_id`, `kind`, `commitment_block`, `status` (`screening`, `queued`, `scored`, `rejected`, `superseded`), `reject_reason`, `mirror_repo`, `created_at` |
| `model_context_runs` | `submission_id`, `season`, `cpm_score_version`, `status`, `score`, `components` (jsonb), `per_seed` (jsonb), `scorer_id`, `gpu_sku`, `started_at`, `finished_at`, `fault` |
| `model_context_seasons` | `season`, `open_block`, `open_block_hash`, `seeds`, `bench_version`, `base_floors` (jsonb), `opened_at` |
| `model_context_scorer_heartbeats` | Scorer heartbeats |

### 5.2 Settings

| Setting | Default |
| --- | --- |
| `mode` | `off` (values: `off`, `shadow`, `enforce`) |
| `base_allowlist` | §2.1 |
| `max_lora_rank` | 64 |
| `max_total_bytes` | 6 GiB |
| `upload_cooldown_seconds` | 21,600 |
| `upload_fee_required` | false |
| `season_days` | 7 |
| `floor_margin` | 0.01 |
| `budget_tokens` | 2,048 |
| `tool_k` | 8 |
| `rank_shares` | `[0.50, 0.20, 0.13, 0.10, 0.07]` |

### 5.3 Public endpoints

- `GET /model-context/upload-terms`
- `POST /model-context/submissions`, with body `{hotkey, hf_repo, revision, manifest, signature, ts, nonce}`
- `GET /model-context/submissions?hotkey=`
- `GET /model-context/board`
- `GET /model-context/seasons`

### 5.4 Scorer endpoints

These require the bearer `DITTO_MODEL_CONTEXT_SCORER_TOKEN`. The shapes are
Platform's models in `apps/platform/ditto/api_models/model_context.py`.

- **`GET /model-context-scorer/work?scorer_id=&scorer_version=&gpu_sku=`**
  leases the next run, or answers 204. It returns a `ModelContextWorkItem`:

  ```text
  {run_id: UUID, lease_id: UUID, lease_expires_at,
   kind: "submission" | "reference",
   submission: {submission_id, hotkey, hf_repo, revision, manifest,
                manifest_sha256, weights_sha256, base_id, kind} | null,
   base: {base_id, hf_repo, revision, parameters, architectures,
          lora_target_modules},
   season, seeds: [uint64], generate_seeds: [int64], bench_version,
   cpm_score_version, gpu_sku: str | null,
   settings: {budget_tokens, tool_k, floor_margin}}
  ```

  - `kind = "reference"` means: score the unmodified `base` for the season
    floor (§4.5). `submission` is then null. Reference runs come only through
    `/work`.
  - `generate_seeds` are the same seeds as `cmd/generate -seed` takes them
    (two's-complement int64), in order. The scorer passes them to the
    generator.
  - `gpu_sku`, once set, is the season's SKU. A scorer on a different SKU
    posts an `infra` fault instead of scoring.
- **`POST /model-context-scorer/results`** takes a
  `ModelContextResultRequest`:

  ```text
  {run_id, lease_id, cpm_score_version, score,
   components: {memory_recall, distractor_rate, tool_recall,
                memory_cases, tool_cases, packed_tokens?},
   per_seed: [{seed, score, memory_recall, distractor_rate, tool_recall,
               memory_cases, tool_cases, packed_tokens?}],   # 1..8 items
   scorer_id, scorer_version (1..64 chars), gpu_sku (1..64 chars)}
  ```

  Platform checks that `per_seed` is on the season's seeds in order, that
  each seed score matches its components (§4.4), and that `score` is their
  mean. It ignores extra keys.
- **`POST /model-context-scorer/fault`** takes a `ModelContextFaultRequest`:
  `{run_id, lease_id, error_kind, detail (≤ 1,982 chars; longer is a 422), scorer_id,
  scorer_version}`. The `error_kind` values are:
  - `invalid_model`: screening failures and bad weights;
  - `model_load`: download and load failures;
  - `nondeterministic`: the re-run seed differed;
  - `infra`: anything host-side or unexpected, including a malformed work
    item.
- **Once per lease.** Results and faults are accepted once per lease, and a
  repeat gets 409.
- **`GET /model-context-scorer/reference-work`** is a status summary: the
  season's base floors and the bases still pending. It is not a queue.

### 5.5 Ledger and board

- **Validator ledger:** `GET /scoring/model-context-ledger`, behind the same
  ledger-request proof as `/scoring/router-ledger`. Entries are `{hotkey,
  submission_id, score, eligible, commitment_block, season}` for the current
  season.
- **Board:** ranked by score, then by commitment block (earlier first).

## 6. Scorer, miner tooling and mirror

### 6.1 `services/cpm-scorer`

- A Python package (torch, transformers, peft, safetensors), with one entry
  point:
  - `cpm-scorer serve --platform URL`: lease, score, post.
  - `cpm-scorer score --model DIR --seeds 1,2,3`: the same scoring path,
    run offline.
- It shells out to a pinned `cmd/generate` binary built from
  `research/dittobench-datagen`, and imports the exporter's label logic.
- It runs on an AWS GPU host (a g6e.xlarge, L40S).

### 6.2 Miner CLI

`ditto model-context {manifest, commit, submit, status, board, practice}`:

- `manifest` writes `cpm-manifest.json` with hashes.
- `commit` sets the on-chain commitment.
- `practice` runs `cpm-scorer score` locally on public seeds. That makes local
  rehearsal the same code path as the scorer.

### 6.3 Starter kit, `miners/cpm-starter-kit`

- A reference LoRA training script on exporter data, with a pairwise and
  listwise loss over each case's candidates.
- A baseline adapter.
- The practice loop.

### 6.4 Hugging Face mirror

- Platform copies each accepted submission at its pinned revision to
  `<ditto-org>/<base_id>-<hotkey[:8]>-<manifest_sha256[:8]>`.
- It stays disabled until the owner creates the organization and stores a
  write token in Secret Manager. That is an owner step; agents never create
  or move credentials.
- Until the mirror is enabled, the board links each miner's own repo at the
  pinned revision. Hugging Face keeps revisions, so the evidence persists as
  long as the miner doesn't delete the repo.

## 7. Validator (shadow)

- `TRACK_MODEL_CONTEXT = "model_context"` in `ditto/validator/tracks.py`:
  - state `SHADOW`;
  - `weight_eligible = False`;
  - `emission_share_bps = 0`;
  - `unclaimed_share_to = memory`.
- **Fold:** rank the eligible entries by score, then by commitment block.
  Apply `rank_shares` from the ledger, or fall back to the default.
- **Failure handling:** if the ledger is missing or unreadable, the track
  folds to nothing. In shadow that changes no weights.
- **Paying the track later:**
  - add `model_context` to `PAYABLE_TRACKS`;
  - bump `HEARTBEAT_PROTOCOL_VERSION` and add a
    `MODEL_CONTEXT_TRACK_PROTOCOL` gate, mirroring
    `TRACK_SHARES_PROTOCOL = 33`;
  - set a share through `set_emission_allocation`.

  That is a separate, owner-approved step.

## 8. Operator surface

- **Backroom MCP tools:**
  - `get_model_context_settings`, `set_model_context_settings`;
  - `list_model_context_submissions`, `get_model_context_submission`;
  - `get_model_context_run`;
  - `rescore_model_context_submission`, `reject_model_context_submission`;
  - `get_model_context_board`, `get_model_context_scorer`;
  - `rotate_model_context_season`.
- **Dashboard:** a "Context models" page beside "Generators".

## 9. Delivery

Each PR goes through ditto-code review and green CI, and merges with a squash.

| # | Change | Depends on |
| --- | --- | --- |
| 1 | This contract (docs only) | — |
| 2 | `services/cpm-scorer` and the starter kit's training and practice scripts, proved locally on CPU with a tiny run | 1 |
| 3 | Platform: tables and migration, settings, submission endpoints, scorer endpoints, seasons, ledger, generated clients. `mode = off` on merge | 1 |
| 4 | Backroom MCP tools and dashboard page | 3 |
| 5 | Validator shadow track and miner CLI `model-context` commands | 3 |
| 6 | Infra: the `aws-cpm-scorer` Terraform stack (GPU, inactive), the aws-bootstrap identity and grants, the Ansible role, and the scorer release channel | 2 |
| 7 | Activation, in order: apply bootstrap as an SSO admin, apply the stack once GPU quota lands, set `mode = shadow`, open season 1 | 2–6 |

Nothing in this plan pays emissions. Turning on a share is a separate owner
call after P0-S.
