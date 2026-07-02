# Next Steps — Ditto SN118

**As of 2026-07-02.** Audience: leadership + team. Successor to
[`STATE-OF-THE-SUBNET.md`](STATE-OF-THE-SUBNET.md) (2026-06-30). It records what
moved since the walking-skeleton proof, the **current** state of the whole
miner→platform→validator→chain stack, the **bugs a full-stack review surfaced**,
and a prioritized path to a production launch.

> TL;DR — Since 2026-06-30 we turned several Phase-2 items from "planned" into
> "plumbed": mode-B tarball ingest is built, hardened, and **deployed**; the
> co-located validator (dittobench-api + worker) is **live on GCP** and
> configured for *real* (non-mock) scoring; the screener/promotion endpoints and
> the submission contract landed. A review then found **one critical
> incentive-correctness bug** (on-chain weights churn to only the last epoch's
> evaluated set) plus a cluster of signature/concurrency integrity gaps. Fixing
> the weight-ingestion bug is now the top priority — above any new feature.

---

## 1. What changed since 2026-06-30

All PRs below are merged. Nothing here was stubbed.

| Repo | Change | PR |
| --- | --- | --- |
| **dittobench-api** | **Mode B**: `tarball_url` ingest for the on-chain validator — in-process hardened extractor (zip-slip / gzip-bomb / symlink / size + file-count caps), SSRF-guarded fetch, SHA-256 verify, Dockerfile-context detection | #6 |
| | Deterministic reference harness + one-command single-run (capability A) | #7 |
| | Calibration harness + stratified sampling to tighten test-difficulty variance (capability B) | #5 |
| **ditto-platform** | **Screener/promotion** endpoints — `uploaded → evaluating` gate (`/screener/queue`, `/screener/agent/{id}/artifact`, `/screener/agent/{id}/result`), 5xxx error range, partial index on `uploaded` | #7 |
| | Widen validator `CaseScore` to preserve the memory signal | #6 |
| | Submission-contract doc (enforcement side) + corrected holistic architecture diagram | #9, #8 |
| **ditto-subnet** | Lock validator repo boundary + guard the API contract; sync `CaseScore`/`ScoreReport` with the platform | #16, #17 |
| **dittobench-starter-kit** | Make the submission contract explicit in the miner README (whole crate, fixed interface) | #5 |
| **infra** | Co-located validator VM (Terraform, gated `enable_validator`) + `dittobench` and `validator_worker` Ansible roles + converge fixes | #4, #5, #6 |

**The validator is now deployed and running.** `ditto-validator-dev` (private GCE
VM, IAP-only, no public IP) runs `dittobench-api` (loopback `:8080`, `/health`
green) and the `ditto-validator` worker as host systemd services. On-chain the
signing hotkey `5EexQS8U…dwgTY` is **uid 4 on netuid 3, validator-permitted, with
stake**. The worker is configured **`VALIDATOR_DITTOBENCH_MOCK=false`,
`RUN_SIZE=full`** — i.e. pointed at the *real* scorer, not the 6/30 mock — and is
polling the platform queue. It is idle only because nothing has been promoted to
`evaluating` yet. Runbook: `infra/docs/validator-deploy.md`.

---

## 2. Current state by goal area

Verdicts: **DONE / PARTIAL / DOC-ONLY / MISSING**. This supersedes the "what's
left" list in `STATE-OF-THE-SUBNET.md` §"What's left".

### 2.1 Real evaluation (DittoBench) — **PARTIAL** *(engine real + deployed; end-to-end real run not yet exercised)*
- **Engine is real, not a stub.** dittobench-api runs the full `run_size`
  pipeline (build → seeded anti-cheat datagen → seed haystack → tool + memory
  cases → LLM judge → score). Datagen (`internal/datagen`, `internal/gen`,
  full profile = 60 tools / 50 mem / 300 distractors), sandbox
  (`docker build` from clone/tarball, `--memory 2g`, no-new-privileges, private
  bridge net), scorer (deterministic tool accuracy + LongMemEval-style LLM judge
  at temp 0) are all implemented.
- **Mode-B tarball ingest is now wired** (PR #6): validator sends `tarball_url`,
  both platform endpoints hand back a presigned GCS URL, the engine fetches +
  safe-extracts + builds. The 6/30 contract mismatch (engine only accepted
  `git_url`) is **closed**.
- **Not yet run green end-to-end with real scoring.** The 6/30 E2E used the mock
  scorer; the deployed worker now has mock off but no agent has flowed through
  the real tarball→docker→judge path since deploy. **First real scoring run is
  the immediate validation milestone.**
- **Missing hardening:** OpenRouter **egress allowlist** and **cost cap** are not
  implemented (only a global concurrency bound + per-IP rate limit); the sandbox
  comment itself defers seccomp/gVisor/egress. The validator also does **not
  pass `tarball_sha256`**, so the engine can't integrity-check the blob it builds
  (see §3, tag-collision).

### 2.2 Pre-screen / screener — **PARTIAL** *(platform endpoints done; worker missing; promotion still manual)*
- Platform landing zone is **DONE** (PR #7): queue, artifact, signed result,
  `uploaded → evaluating` / `→ screening_failed`, idempotent, 409 on conflict.
- **The Rust lint/compile/build screener *worker* does not exist yet** — there is
  no screener process in `ditto-subnet`. So `uploaded → evaluating` is **still
  manual**. Owner: **Dan**.

### 2.3 Scoring + weights at scale — **PARTIAL / MISSING at scale**
- Single-validator straight-through works; signed score ledger is **DONE**.
- **k=3 sharding + median-of-3 finalization: MISSING.** The worker scores each
  queued agent once; the platform stores one score row per agent.
- **Weight function is a deliberate placeholder** (`validator/weights.py` — its
  own docstring says "WIP / placeholder reward curve"). The deterministic
  consensus curve every validator must agree on is not built.
- **Single validator today.** Owner: **Dan**.

### 2.4 Final incentive mechanism — **DOC-ONLY**
- `docs/incentive-mechanism.md` is thorough (Options A–E; recommends KOTH+ATH
  gate + participation tail, evolving toward Pareto).
- **No implementing code, and — critically — no plagiarism / first-seen /
  near-duplicate detection.** Since artifacts are downloadable, the current
  winner can be resubmitted verbatim. Owner: **team**.

### 2.5 Emission economics — **DOC-ONLY (config tuning, no code)**
- Unchanged from 6/30: consensus picks the winner (`Incentive[3]=65535`) but
  `SubnetTaoInEmission[3]=0`, so no alpha flows. Alpha-pool / `TaoWeight` tuning.
  Owner: **Ethan** (exact on-chain values in `STATE-OF-THE-SUBNET.md` §"For Ethan").

### 2.6 Productionization — **PARTIAL**
- **Deploy automation: DONE (dev), off by default.** Ansible
  (`base`/`dittobench`/`validator_worker`) + Terraform (`validator.tf`, gated
  `enable_validator`) + Secret Manager wiring; converged clean this session.
- **Commit-reveal: no first-class reveal step** in the worker (SDK path sets
  weights directly; Pylon path delegates reveal to Pylon). Dev netuid 3 has
  commit-reveal off; production needs a reveal step.
- **SDK vs Pylon weight path:** both implemented, switchable via
  `VALIDATOR_USE_SDK_WEIGHTS` (dev uses SDK; Pylon identity write-path not stood
  up on localnet).
- **Observability: minimal.** stdlib logging only; no W&B run logging, no winner
  dashboard, no metrics on the validator. Deployed unit is systemd (not pm2 +
  autoupdater as the older docs assumed).

### Submission contract — **DONE (documented + enforced)**, tarball-through-to-engine now closed
- Miners submit the **whole buildable crate** as one gzipped tarball (Dockerfile
  at root; `ditto-harness` is a pinned private git dep pulled at build via a
  `gh_token` secret) — **not** a single `baseline.rs`. Documented in
  `ditto-platform/docs/submission-contract.md` + starter-kit README; enforced at
  upload (on-chain fee, ≤2 MiB from streamed bytes, SHA-256 re-verify). Deferred
  checks (tar manifest, import allowlist, schema diff, banned-hotkey table)
  remain unenforced by design.

---

## 3. Bugs & integrity gaps found (full-stack review, 2026-07-02)

A four-part adversarial review of everything merged this session. Ranked; each is
substantiated with a file:line and a concrete trigger.

### 🔴 CRITICAL — On-chain weights cover only the *currently-evaluating* set; every previously-scored miner is zeroed each epoch
`ditto-subnet/ditto/validator/worker.py:53-77`. The worker builds the weight
vector from `queue.items` only, and `put_weights` **overwrites the entire**
on-chain vector. But the platform queue returns only `EVALUATING` agents, and a
successful score flips the agent to `SCORED` (drops it from the queue). So an
agent earns weight for **exactly the one epoch it is scored, then falls to zero
forever** (until the miner resubmits). This is the long-standing "ingestion gap":
the worker never reads a full score ledger — `PlatformClient` exposes
`get_queue`/`get_artifact`/`submit_score` but **no best-score-per-miner read**.
**This breaks sustained incentives and must be fixed before any scale work.**
- **Fix:** add a platform read endpoint that returns the current best score per
  active miner (the platform already keeps `SCORED`/`LIVE` rows for exactly this),
  and have the worker compute weights over that ledger — not the per-sweep queue.

### 🟠 HIGH — A transient `put_weights` / `submit_score` failure permanently loses that epoch's miners
`worker.py:73,85`. Composites live only in a local dict and are never persisted;
`put_weights` isn't wrapped, so on a chain blip the sweep is abandoned and
"retried next epoch" — but by then those agents are already `SCORED` and gone
from the queue. A single failed weight-set (or a `submit_score` hiccup) silently
drops those miners. Fix rides along with the CRITICAL ledger change (persist +
recompute from the ledger, retry weight submission).

### 🟡 MEDIUM — Signatures bind neither the decision nor the payload (screener *and* validator)
- **Screener** (`ditto-platform/.../screener.py:241`): the signed bytes are only
  `{screener_hotkey}:{agent_id}` — the **`passed` verdict is unsigned**. A
  captured/replayed signed result can be resubmitted with `passed=False` to grief
  a miner (or flip the outcome). Bound to `agent_id` (no cross-agent replay), but
  the actual decision has no integrity.
- **Validator** (`ditto-subnet/.../signing.py:50` ↔ `.../validator.py:281`):
  signs only `{validator_hotkey}:{run_id}` — the **score body and `agent_id` are
  unsigned**, no nonce/timestamp. Reported numbers carry no cryptographic
  integrity.
- **Fix:** sign a canonical payload that includes the agent id, the decision /
  score fields, and a nonce or expiry; verify the full payload server-side. (The
  wire docstrings still say verification is "deferred" though the endpoints verify
  live — reconcile the comments too.)

### 🟡 LOW–MEDIUM — Concurrency: lost-update on the promotion / score transactions
`screener.py:254` and `validator.py:292` read the agent with a plain SELECT (no
`FOR UPDATE`) and issue an **unconditional** UPDATE. Two concurrent conflicting
verdicts both pass the Python-side status guard and last-writer-wins (instead of
409); a slow txn can even demote an already-advanced agent. Small window in the
single-actor MVP, but a real state-corruption path once multiple
validators/screeners run. **Fix:** `with_for_update()` on the read *or* a
conditional `UPDATE … WHERE status IN (…)`.

### 🟡 LOW–MEDIUM — Presigned tarball URL (with its S3 signature) leaks into logs + pollable job status
`dittobench-api/internal/sandbox/tarball.go:63`. On a *transport-level* fetch
error the full credentialed URL is wrapped un-redacted and stored via
`store.Fail(...)` → returned by `GET /v1/runs/{id}` and logged (the git path
redacts; the tarball path doesn't). Non-200s are fine (status only). **Fix:**
apply the existing `redact()` to the tarball error path.

### 🟢 LOW — dittobench-api extractor: bounded CPU-DoS + docker tag collision
- Skipped tar entry types (symlink/device bodies) are decompressed but not
  counted against the 64 MiB uncompressed cap, and `extractTarGz` never checks
  `ctx` — a ~8 MiB gzip can burn GBs of discarded decompression (bounded by the
  compressed cap + 25-min build timeout). `tarball.go:155`.
- `safeTag` derives the image tag from the URL basename; two tarballs sharing a
  basename **with no `tarball_sha256` pin** can collide and, under concurrency,
  run the wrong image. `sandbox.go:safeTag`. The validator not sending
  `tarball_sha256` (§2.1) makes this reachable. **Fix:** count skipped-entry
  bytes / honor ctx; include a content hash in the tag (and have the validator
  pass the SHA it already has).

**Verified correct (no change needed):** SSRF parity incl. redirect re-checks,
SHA-256 + size caps computed on real streamed bytes, zip-slip/path-safety,
three-way source mutual-exclusivity, temp-dir cleanup (dittobench-api); migration
chain + index parity, status-guard ordering, 5xxx error mapping, additive
`CaseScore` widening (platform); empty-queue handling (no busy-loop, doesn't zero
the chain), one-bad-agent isolation, `compute_weights` per-input determinism, SDK
weight-path error handling, signing wire-format match + hotkey guard (validator).

---

## 4. Prioritized roadmap

Ordered by "what unblocks the most / what bites first."

1. **Fix the weight-ingestion bug** (§3 CRITICAL + HIGH). Platform: expose a
   best-score-per-active-miner ledger read. Validator: compute weights from that
   ledger, persist composites, retry weight submission. *Without this, incentives
   don't actually accrue past one epoch.* — **Nick / Dan**
2. **First real DittoBench E2E run** (§2.1). Promote one agent through the live
   non-mock path (screener/manual → validator → tarball → docker build pulling
   `ditto-harness` → judge → score → weights) and confirm a real composite lands.
   Then add the **OpenRouter egress allowlist + cost cap**. — **Nick + Omar**
3. **Screener worker** (§2.2). The Rust lint/compile/build gate that automates
   `uploaded → evaluating`. — **Dan**
4. **Harden the trust boundary** (§3 MEDIUM + LOW-MED). Sign canonical payloads
   (agent + body + nonce) on both the screener and validator paths; add row-lock /
   conditional UPDATE; redact the tarball error path; pin the tarball SHA. —
   **platform**
5. **Scoring at scale** (§2.3). k=3 sharded queue + median-of-3 finalization +
   the deterministic weight function, then move from one validator to the set. —
   **Dan**
6. **Incentive mechanism impl** (§2.4). KOTH+ATH gate + first-seen timestamps +
   plagiarism/near-dup detection (highest anti-copy risk). — **team**
7. **Emission tuning** (§2.5). Non-zero netuid-3 per-block emission. — **Ethan**
8. **Productionization** (§2.6). Commit-reveal reveal step; validator
   observability (W&B run logging + public winner dashboard); testnet → finney;
   deploy hardening.

---

## 5. Deploy receipts (this session)

- **VM:** `ditto-validator-dev`, private GCE (`e2-standard-4`), IAP SSH, internal
  `10.30.0.5`, `enable_validator=true` applied.
- **Services:** `dittobench-api` `active` (`/health` → `{"status":"ok"}`,
  loopback `:8080`); `ditto-validator` `active`, 0 restarts, polling
  `platform-api-dev.heyditto.ai`.
- **Chain (netuid 3, `ws://68.183.141.180:80`):** hotkey
  `5EexQS8UxChmkZ6vGeacAkwcf3TARR1Go5rd684Mf69dwgTY` = **uid 4**,
  `validator_permit=True`, staked. (Rotated from the retired `5CZq6Mdanx…`.)
- **Secrets** (Secret Manager, read via the VM SA): hotkey mnemonic, OpenRouter
  key, GitHub token (read on dittobench-api / ditto-subnet / ditto-harness).
- **Converge caveats:** the base-role Go install is broken repo-wide
  (`checksum: "sha256:auto"` is invalid `get_url` syntax) — disabled for the
  validator (dittobench role installs its own Go); worth a standalone base-role
  fix. Dynamic GCP inventory needs google-auth ADC (can't interactive-reauth) — a
  static-inventory fallback was added.

See also `STATE-OF-THE-SUBNET.md` (the 6/30 proof + receipts) and
`dev-e2e-handoff.md` (step-by-step runbook).
