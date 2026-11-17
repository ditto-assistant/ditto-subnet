# Readmission fee (SN-485)

Status: building, 2026-11-14. Spec: Jira SN-485. This revises the scoping draft
(`readmission-fee-scope.md`, written on another branch) with the owner's
decisions. Line references are to `main` at `f7ac949a4` and were re-checked
against it.

## What it is

A **readmission** is the owner of an existing agent lineage asking Ditto to
score it again. Identity is the lineage plus the owner (payment-time) coldkey,
not the hotkey or the upload id. There are two kinds:

- **(a) era readmission.** The owner opts an existing agent into a new benchmark
  era. Today the only way in is the operator's free `admit_stranded_submission`.
- **(b) re-run.** The owner re-runs an unchanged agent after one of its
  evaluations completed. Today that is `ditto upload --allow-identical-rescore`.

A byte-identical (`sha256`) or near-identical (same `normalized_source_hash`)
re-upload of the owner's own lineage is also classified as a readmission. It is
the same price, so classification changes labels, accounting and the section 3
and 4 handling, not the amount. Nothing is charged or readmitted automatically.

## Decisions

| Topic | Decision |
|---|---|
| Activation (section 7) | New operator setting `submission_settings_revisions.readmission_announced_at` (timestamptz, nullable, **unset by default**). Readmission is active only when `active_bench_version >= 19` **and** `now >= readmission_announced_at + 7 days`. Unset means inactive: nothing is charged as a readmission and self-serve readmission is unavailable. The announcement date is the kill switch |
| Retroactivity | Never. An upload or identical rescore made before `effective_at` keeps today's behaviour |
| Who sets it | An operator, through the Platform admin API. Backroom shows it read-only. The agent that builds this never sets it |
| Price (O6) | Always exactly the resolved upload (default) fee, `fee_amount_rao`. No separate override key, so nothing can make it more than the upload fee |
| Within-era re-run (O1, 1b) | Paid at the readmission fee once active. Today's `--allow-identical-rescore` already pays the upload fee (verified, below). Once active it is labelled and accounted as a readmission and stays under the cooldown. A failure the agent caused is remedied by a paid re-run or a resubmit, never a free retry |
| Dedupe (section 1) | Same-owner exact and near-duplicate uploads are classified with the existing machinery (`get_same_owner_agent_by_sha`, `Agent.normalized_source_hash`). No new detector |
| Cooldown (O4) | A readmission counts toward, and starts, the 3600 s owner-coldkey cooldown |
| Eligibility (O2) | Any stranded row of the owner. At most one paid readmission per agent per era. The check response shows `owner_best_stranded` and `cohort_sibling` |
| Partial rows (O3) | Not eligible in v1. 2-of-3 rows stay resubmit-only or follow Ditto's free carryover |
| Refusal (O5, section 4) | Refuse before payment whenever the check can know (slot, cohort cap, queue policy, guards, open rollout, policy floor). If refusal is only found after payment, bank an automatic credit through the existing credit mechanism, valid at least 30 days for readmission-origin credits, and record `outcome = credited`. No on-chain refunds |
| Free retries (section 3) | Audit every free within-era path, cite whether it is bounded per agent, and add a per-agent cap only where a miner can drive an unbounded one. Eligibility comes from recorded error codes, never miner input. Operator-only tools are not capped |
| Cost basis (section 2) | Publish the measured per-agent cost on the active bench in the memory submission terms: a cached rolling median of the inference ledger over agents that reached quorum, `null` without enough data. No hardcoded figure, nothing on the leaderboard hot path |
| Free operator path (O7) | `admit_stranded_submission` is unchanged, except that its audit payload carries an `origin` and a `category` |
| Out of scope | Where the proceeds go; copying priority across eras; scoring several runs (latest or mean, separate PR); the "era changed, resubmit" terminal status (separate PR); the per-lineage free resubmission credit (on hold) |

## Verified: today's identical rescore pays the upload fee

`/upload/check` skips the same-owner duplicate refusal (1104) only when
`allow_identical_rescore` is set (`apps/platform/ditto/api_server/endpoints/upload.py:336-352`),
reserves a slot at the snapshot fee (`:479-507`, `reserve_upload_admission`
`db/queries/submission_settings.py:249`) and enforces the coldkey cooldown
(`:457-476`). `/upload/agent` verifies the proof against that snapshot
(`:731-791`). With the flag set, the duplicate branch that would bank a
`reusable_credit` is skipped (`:803`), so the request inserts a new agent and an
`evaluation_payments` row for the fresh proof in one transaction (`:944-985`)
after consuming the reservation (`:927`). It therefore pays exactly the upload
fee and starts the cooldown like any upload. The Go upload relay's check
(`services/model-relay/internal/upload/handlers.go:471`) matches, and it is off
by default (`infra/ansible/roles/platform_app/defaults/main.yml:114`).

## Activation gate

`apps/platform/ditto/db/queries/readmission_gate.py` holds the single predicate,
`readmission_activation`, with `READMISSION_MIN_BENCH_VERSION = 19` and
`READMISSION_NOTICE_PERIOD = 7 days`. It reports `inactive_reasons`
(`announcement_unset`, `bench_version_below_minimum`, `notice_period`) and
`effective_at`. Every consumer (the admin settings view, the public memory
terms, `/readmission/check`, upload classification) calls it.

The admin write carries the announcement forward unless the request names it,
and an explicit `null` clears it. A newly written date may be at most one hour
in the past, so a backdate cannot shorten the notice miners actually got, and
at most 30 days ahead. Sending the field appends `; READMISSION ANNOUNCED
<UTC>Z` (or `; READMISSION ANNOUNCEMENT CLEARED`) to the confirmation phrase.

## Upload-fee path: hook points

| Step | Where | Today | Readmission |
|---|---|---|---|
| CLI transfer | `ditto/miner_cli/payment.py:90` `submit_eval_payment` | Transfer, returns the proof tuple | Reused unchanged |
| CLI flow | `ditto/miner_cli/commands/upload.py:424`, `:520`, `:586`, `:633` | check, pay, POST, with saved-proof recovery | New `ditto readmit <agent_id>` with the same shape. Not part of `upload` |
| CLI proof store | `ditto/miner_cli/preferences.py:36-39` | One bucket per lane | New `pending_readmission_payments` bucket. `upload`'s `saved_reassigned` reuse (`upload.py:404-415`) never sees it |
| Fee | `db/queries/submission_settings.py:133` `competition_fee` | Memory lane charges the default | `readmission_fee_rao(settings) = settings.fee_amount_rao`. No override key |
| Reservation | `submission_settings.py:249`; model `db/models.py:7110` (PK `miner_coldkey`) | One slot per coldkey, fee and address snapshot, 24 h / 15 min | New `readmission_reservations`, PK `(agent_id, bench_version)`, same TTLs. It takes `lock_upload_admission_coldkey` (`:223`) so a readmission and an upload from one coldkey serialize. The coldkey keeps one submission slot across both lanes: one live readmission reservation per coldkey, and neither lane reserves while the other's reservation is inside its 15-minute block window. Consuming any token rechecks the cooldown against submissions recorded after the reservation was issued |
| Amount and chain proof | `api_server/payment_verifier/verifier.py:35-60`, `:106`, `:182` | Exact rao against the snapshot; signer coldkey owns the hotkey at the payment block | Reused unchanged |
| Single use | `db/queries/payments.py:117` `lock_payment_proof`, `:176`, `:284` | Two lanes check each other under one advisory lock | **Three lanes.** Upload, generator and readmission inserts each check both other tables under the same lock. The upload pre-checks (`upload.py:419` check, `:702` commit) also refuse a readmission proof |
| Recovery window | `upload.py:193` | 24 h | Same for a readmission proof. A readmission-origin credit lasts 30 days |
| Lookup and accounting | `endpoints/admin_upload_payments.py:40`, `endpoints/admin_miner_fees.py` | Upload and generator lanes | Readmission lane added |

`verify_payment` binds hotkey, coldkey, amount, destination and time, not an
artifact, so it can attest a payment for an existing `agent_id`. Three things
bind a readmission instead of a tarball:

1. **A signature domain.** `ditto-readmit-v1:{operation}:{hotkey}:{agent_id}:{bench_version}:{ts}:{nonce}`,
   with `operation` one of `check`, `reserve` (a check with `reserve_slot`) or
   `commit`. It differs from the upload domain, so neither signature can stand
   in for the other, and a signature for one operation never authorizes
   another.
2. **`readmission_payments`**, not `evaluation_payments`, whose `UNIQUE(agent_id)`
   (`models.py:2039`) and every reader assume the row is the agent's upload
   payment. PK `(block_hash, extrinsic_index)`, plus `agent_id`, `bench_version`,
   `rollout_id`, `miner_hotkey`, `miner_coldkey`, `amount_rao`, `tao_usd_rate`,
   `dest_address`, `timestamp`, `outcome` (`admitted` | `credited`), and a partial
   unique index on `(agent_id, bench_version) WHERE outcome = 'admitted'`.
3. **`readmission_reservations`** (above).

## Era gate

| Gate | Where | Readmission |
|---|---|---|
| `benchmark_admission_predicate` | `db/queries/benchmark_admission.py:215` | Unchanged. A paid readmission writes a `BenchmarkRolloutMember`, which the existing member disjunct admits, so leaderboard reads stay as fast as they are |
| Stranded set | `db/queries/benchmark_era_stranded.py:92` | The eligibility set: offered only for a row this read returns |
| Guards | `db/queries/stranded_admission.py:115` | Reused whole: active version unchanged, no rollout open, an activated rollout with a previous era, SHA pin, previous score count, not withdrawn (`:170`), policy floor (`:176`), complete screened image (`:181`) |
| Dataset pin | `api_server/benchmark_rollout.py:790` | Reused: resolve, render outside the transaction, re-resolve and write |
| Write | `stranded_admission.py:210` | `origin` (`operator_stranded_admission` or `paid_readmission`), `category` and `payment_ref` in the audit payload; the `readmission_payments` insert in the same transaction |
| Issuance | `db/queries/benchmark_rollout.py:2022` | Unchanged: leases from scored/live under the distinct-validator, withdrawal, retirement and owner-concurrency rails |

**What the check refuses before payment** (section 4): gate inactive; no
activated era or a rollout open; not a stranded row of this owner; withdrawn,
below the policy floor, no complete screened image, or retired; already
admitted or already paid this era; a partial (2-of-3) row; banned hotkey, CLA,
unregistered hotkey, or a new on-chain owner of the hotkey; cooldown; and any
queue-policy exclusion it can read for one agent (queue withdrawal,
retirement). Owner concurrency and the similarity budget depend on live leases,
so they delay issuance but never exclude the row: they do not refuse.

## Free retries (section 3)

| Path | Where | Trigger | Per-agent bound | Miner-drivable |
|---|---|---|---|---|
| Same-validator retry | `db/queries/tickets.py:110` `MAX_ATTEMPTS_PER_VERSION = 1`; `db/queries/retry_budget.py:25,27` (both 0) | Automatic | One attempt per validator per bench version | No |
| First assignment on each further validator after a failure | `db/queries/queue_order.py:253` (counts no failures); close-out only on one agent-attributable code, `db/queries/terminal_failure.py:86-101` with `retry_state.py:56` | Automatic | **No constant: the capable fleet size per era** | **Yes**: a harness that hangs to the lease deadline or fails with an unnamed code keeps being leased |
| Operator retry grants | `endpoints/admin_validation_retry.py:1301` (`max_operator_recoveries=None`), `:976` | Operator only | Unbounded | No (not capped, by decision) |
| Operator infra void | `endpoints/admin_confirmation_score_voids.py:44` | Operator only | Unbounded | No |
| Automatic outage-sweep void | `db/queries/confirmation_score_voids.py:45-54` | Automatic, needs recorded upstream 402/429/503 on the run's own grant | Unbounded | Not shown: the evidence is provider failures on Ditto's keys |
| Screening infra auto-retry | `db/queries/screening_infra_retry.py` `INFRA_AUTO_RETRY_MAX_STREAK = 8` | Automatic, `FLEET_INFRA` codes | 8 per agent | Bounded already |
| Provider-outage parked resume | `db/queries/provider_outages.py:197-208`, `tickets.py:1113-1144` | Platform-written | One per ticket | No |
| Continual retest | `api_server/endpoints/validator.py:6940` | Automatic, top five only | 6 h cooldown, no attempt cap | Only a top-five agent; a failure cannot raise its score |

The one path a miner can drive without a constant bound is the fleet-wide
first assignment. PR 4 adds a per-agent cap: the terminal-failure sweeper
withdraws an agent from the active era once `FREE_RETRY_CAP_FAILED_LEASES`
failed leases on distinct validators have no score and no agent-attributable
close-out, counting only recorded ticket outcomes and excluding
`provider_outage_parked`. Like the fee, it is active only when readmission is.
An operator reinstatement is the free remedy for a Ditto fault; the miner's
remedy is a paid re-run.

**Platform-caused re-tests are always exempt (SN-486).** Season rotation,
budgeted catch-up overflow, off-board catch-up and the ATH-clear re-test are
score re-tests with basis `dueling_season_rotation` (`catch_up_source` one of
`board`, `board_overflow`, `off_board`, `ath_clear`). They never pass through
`/upload` or a payment proof. No check, cap, label or classification in this
stack counts them. The cap skips any lease on a validator that ran a score
re-test of any basis for the agent in that era: rotation, the
`provider_outage_void` re-run of an outage-tainted canonical score (#3566),
v9 contract repair, and operator re-tests. It also skips any agent already at
quorum. Upload classification keys only on the owner's own uploads. Tests
assert each of these.

## Edge cases

| Case | Handling |
|---|---|
| Our fault | Free: within-era paths never meet the fee path. If Ditto's fault kept a row out of the cohort, the operator's `admit_stranded_submission` with category `ditto_fault` is the free remedy |
| Cohort member | Already admitted: `/readmission/check` answers `already_admitted` |
| Payment lands, admission refused at commit | `outcome = credited` and an upload credit with `credit_for_agent_id = agent_id`, valid 30 days; the next readmission or upload spends it with no transfer |
| Admitted, scores low | No refund: the fee bought a run |
| Exact retry of a commit | Returns the original admission (same proof, agent, version) |
| Second proof for an admitted row | Banked as a credit |
| Ownership | The hotkey signature plus the current on-chain coldkey equal to the payment-time owner key; a re-registered UID held by someone else is refused |
| Old CLI | New route and domain. An old CLI that sends a readmission proof to `/upload/agent` gets 3207. A new CLI against an old Platform gets a 404 and says readmission is not available yet |
| Price snapshot | Taken at `/readmission/check`, honoured 24 h |
| Public status copy | When the honest-status PR (SN-483) lands, its "resubmit" copy points at `ditto readmit` only when the gate is active and the check would pass |
| Emissions | Unchanged; owner-best and KOTH apply as usual. Fee accounting includes the new lane |

## Stack

1. `feat(platform,backroom)`: `readmission_announced_at`, the activation
   predicate, read-only exposure in `get_submission_cooldown` and the memory
   submission terms, this document. Charges nothing.
2. `feat(platform)`: `readmission_payments` and `readmission_reservations`, the
   three-lane single-use cross-checks, tests that no lane spends another's proof.
3. `feat(platform)`: signed `/readmission/check` and `/readmission/commit`, the
   `ditto-readmit-v1` domain, pre-payment refusals and credit-on-refusal, the
   origin and category on stranded-admission audits.
4. `feat(platform)`: classify identical rescores and same-owner duplicates as
   readmissions (labels, accounting, cooldown) and the free-retry cap.
5. `feat(backroom)`: `list_readmissions`, the lane in `get_upload_payment_proof`,
   origin in `get_stranded_admissions`, fee accounting, the measured cost basis.
6. `feat(cli)`: `ditto readmit` with its own proof bucket, `docs/MINER.md`, the
   terms text, the mine skill.

Activation is an operator write after release and deploy. Verify source, CI,
merge, release, deploy, migration and one live readmission on a test hotkey as
separate facts.
