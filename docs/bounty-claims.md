# SN118 bounty claims, reservations, and contributor identity

Status: proposed. This is the claim contract for the SN118 maintenance treasury specified in [maintenance-treasury.md](maintenance-treasury.md). Parent epic: #2054. Related issues: #2044 (treasury governance and accounting), #2046 (acceptance, payout proof, disputes), and #2047 (bounty board and contributor guide).

A claim binds a scoped unit of work to a verifiable subnet identity and payment destination for a bounded duration under public rules established prior to work commencement. A claim does not approve deliverables, promise payment, or transfer funds.

## Core principles

1. **The hotkey determines identity; GitHub is a display label.** Every claim action is cryptographically signed by a registered hotkey or its on-chain owner coldkey. A GitHub username is recorded for attribution and pull request matching only. A GitHub profile cannot choose, modify, or approve a payment destination.
2. **Payout routes strictly to the on-chain coldkey owner.** The payee coldkey is signed into the claim payload and validated against `SubtensorModule.Owner(hotkey)` at claim registration and payout execution. Payment destination cannot be asserted via request parameters or profile fields.
3. **Single-use replay-resistant signatures.** Every signature binds a unique domain tag, netuid, repository, issue number, bounty spec revision, spec digest, claimant hotkey, payee coldkey, nonce, timestamp, and reservation expiry.
4. **Immutable terms prior to work start.** Concurrency rules, reservation duration, renewal ceilings, and reward ranges are immutable properties of a published bounty revision. Altering terms requires a new revision; existing active claims preserve the terms under which they were signed.
5. **Platform API as authoritative state machine.** Claims are evaluated and recorded by the Platform API and appended to the treasury ledger. GitHub comments and labels reflect platform state; a signature pasted in a comment has no legal or financial effect.

## Bounty specification and revision model

A bounty is identified by its repository target and issue identifier (`{owner}/{repo}#{issue}`, e.g. `ditto-assistant/ditto-subnet#2045`). Prior to claiming, maintainers publish a bounty specification with an integer revision starting at 1 and a cryptographic `spec_digest` computed as the SHA-256 hash of the canonical sorted JSON payload of the terms.

| Field | Description |
|---|---|
| `scope` | Explicit boundary of work, deliverables, and out-of-scope items |
| `acceptance_evidence` | Objective verification criteria (test suites, benchmarks, deployment receipts) |
| `reward_min_tao`, `reward_max_tao` | Authorized reward range denominated in TAO rao |
| `reviewer` | Assigned non-conflicted maintainer or operator |
| `dependencies` | Prerequisite issues or pull requests that must merge first |
| `bounty_expires_at` | UTC timestamp after which no claims or renewals are accepted |
| `concurrency` | Concurrency policy: `exclusive` or `open` |
| `max_open_claims` | Integer ceiling on simultaneous reservations under `open` policy |
| `reservation_days` | Time-to-live of initial reservation and subsequent renewals (default: 7 days) |
| `max_renewals` | Maximum allowed self-serve renewals without maintainer intervention (default: 2) |
| `policy_revision` | Treasury governance policy revision in force |

Editing issue descriptions on GitHub does not modify active bounty terms. Any amendment requires publishing a new Platform revision with an updated `spec_digest`. Claimants may choose to opt into a newer revision upon renewal or retain their original agreement.

## Contributor identity and payout routing

- **Claimant hotkey**: Any Substrate keypair with an on-chain registration on Bittensor. `SubtensorModule.Owner(claimant_hotkey)` must resolve to a valid coldkey at a finalized block. Holding an active SN118 miner UID is not required, enabling contributions from outside developers.
- **Payee coldkey**: Must match `SubtensorModule.Owner(claimant_hotkey)` at registration. Verified during claim creation and again at payout execution. If coldkey ownership rotates during development, the contributor must submit a signed payee rebind action prior to disbursement.
- **Signing key kinds**: Actions accept proofs signed with either `hotkey` or `coldkey`. `hotkey` proofs require `signer == claimant_hotkey`. `coldkey` proofs require `signer == payee_coldkey` where `payee_coldkey == SubtensorModule.Owner(claimant_hotkey)`.
- **GitHub identity**: Recorded strictly for pull request discovery and reviewer convenience. A pull request opened by a different GitHub user can still be linked through a valid hotkey-signed submit action.
- **Team claims and collaboration**: Teams designate a single payee coldkey and publish a `team_digest` computed over the lexicographically sorted list of member hotkeys and designated payee. Each member signs a team attestation. Collaborations with disclosed structures are valid and do not constitute undisclosed conflicts.

## Signed actions and wire formats

Signed payloads follow domain-separated canonical serialization using colon-delimited UTF-8 values, mirroring `ditto-owner-link:v1` and `ditto-name-claim:v1`. Timestamps are serialized as UTC ISO-8601 with microsecond precision. Nonces are UUIDv4 strings. Signatures are 64-byte sr25519 signatures encoded as 128 hexadecimal characters.

### 1. Bounty claim (`ditto-bounty-claim:v1`)
Initiates an active reservation on a bounty.
```
ditto-bounty-claim:v1:{netuid}:{repo}:{issue}:{bounty_revision}:{spec_digest}:{claimant_hotkey}:{payee_coldkey}:{github_login}:{team_digest}:{expires_at}:{nonce}:{issued_at}:{key_kind}:{signer}
```

### 2. Bounty renew (`ditto-bounty-renew:v1`)
Extends an active reservation prior to expiration.
```
ditto-bounty-renew:v1:{netuid}:{claim_id}:{claimant_hotkey}:{bounty_revision}:{spec_digest}:{new_expires_at}:{nonce}:{issued_at}:{key_kind}:{signer}
```

### 3. Bounty submit (`ditto-bounty-submit:v1`)
Links deliverable code to the reservation, locking the exact commit hash for review.
```
ditto-bounty-submit:v1:{netuid}:{claim_id}:{claimant_hotkey}:{pr_number}:{head_sha}:{nonce}:{issued_at}:{key_kind}:{signer}
```

### 4. Bounty handoff (`ditto-bounty-handoff:v1`)
Transfers an active reservation to a successor contributor. Requires two matching signed halves (`from` and `to`).
```
ditto-bounty-handoff:v1:{netuid}:{claim_id}:{side}:{from_hotkey}:{to_hotkey}:{nonce}:{issued_at}:{key_kind}:{signer}
```

### 5. Bounty withdraw (`ditto-bounty-withdraw:v1`)
Voluntarily releases a reservation without penalty.
```
ditto-bounty-withdraw:v1:{netuid}:{claim_id}:{claimant_hotkey}:{nonce}:{issued_at}:{key_kind}:{signer}
```

### 6. Payee rebind (`ditto-bounty-rebind-payee:v1`)
Updates payment destination following on-chain hotkey ownership rotation. Must be signed by the new owner coldkey.
```
ditto-bounty-rebind-payee:v1:{netuid}:{claim_id}:{claimant_hotkey}:{old_payee_coldkey}:{new_payee_coldkey}:{nonce}:{issued_at}:{key_kind}:{signer}
```

### 7. Bounty appeal (`ditto-bounty-appeal:v1`)
Appeals an administrative revocation to an independent reviewer.
```
ditto-bounty-appeal:v1:{netuid}:{claim_id}:{revocation_entry_hash}:{reason_digest}:{nonce}:{issued_at}:{key_kind}:{signer}
```

### 8. Team membership (`ditto-bounty-team:v1`)
Attestation signed by a team collaborator binding them to the team digest.
```
ditto-bounty-team:v1:{netuid}:{team_digest}:{member_hotkey}:{nonce}:{issued_at}:{key_kind}:{signer}
```

## Replay protection and freshness constraints

1. **Clock skew (`MAX_ISSUED_AT_SKEW = 5 minutes`)**: Rejects actions with `issued_at` timestamps located in the future beyond acceptable NTP drift.
2. **Signature freshness (`MAX_ATTESTATION_AGE = 24 hours`)**: Rejects actions minted more than 24 hours prior to submission.
3. **Nonce deduplication**: Nonces must be unique across all signed actions within the platform database. Replaying a recorded nonce returns HTTP 409 Conflict.
4. **Domain separation**: Distinct domain prefixes prevent cross-protocol signature substitution across miner attestations, handle claims, and bounty operations.

## Reservation lifecycle and concurrency

```
       [Open Bounty]
             │
      (claim action)
             ▼
        [Active] ◄──────┐
         │    │         │ (renew)
(submit) │    │ (expire/revoke/withdraw)
         ▼    ▼         │
    [Submitted] ────────┘
         │
    (acceptance)
         ▼
     [Accepted]
```

### Concurrency modes
- **`exclusive` (default)**: Exactly one concurrent reservation across `active` and `submitted` states. Enforced at the database level via a partial unique index on active reservations. Colliding claim attempts receive HTTP 409 Conflict with the active reservation details and expiration timestamp.
- **`open`**: Permits up to `max_open_claims` parallel reservations. Reviewers may accept the highest quality submission or grant partial split awards. Remaining concurrent submissions transition to `superseded`.
- **Anti-squatting constraints**:
  - Maximum 2 active `exclusive` reservations per payee coldkey across the entire subnet.
  - A 72-hour cooldown period on the bounty for the same payee coldkey if a reservation expires without submission.
- **Revocation**: Maintainers can revoke reservations for documented cause using one of six enumerated reasons: `inactive`, `scope_violation`, `undisclosed_conflict`, `misconduct`, `bounty_cancelled`, or `superseded_by_revision`. Revocations produce auditable ledger records and support administrative appeal.

## Contributor workflow examples

### 1. Claiming a bounty
The contributor signs the claim payload via the CLI:
```sh
uv run ditto --network finney bounty claim \
  --repo ditto-assistant/ditto-subnet --issue 2045 --revision 1 \
  --coldkey default --hotkey miner1 --github-login contributor-a
```
The platform validates:
1. `SubtensorModule.Owner(miner1)` equals the designated payee coldkey.
2. Bounty revision 1 `spec_digest` matches platform records.
3. Concurrency constraints are satisfied.
4. Signature is valid and nonce is fresh.
Returns `claim_id` and records the reservation.

### 2. Renewing a reservation
Within 48 hours of expiration, a contributor extends their reservation:
```sh
uv run ditto --network finney bounty renew \
  --claim-id 1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b \
  --coldkey default --hotkey miner1
```

### 3. Submitting pull request deliverables
When work is ready for review, the contributor binds their pull request and exact commit hash:
```sh
uv run ditto --network finney bounty submit \
  --claim-id 1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b \
  --pr 2080 --head-sha 7c1e8b4f2a9d0e1c3b5a7f9e8d1c2b3a4f5e6d7a \
  --coldkey default --hotkey miner1
```
The reservation transitions to `submitted`, halting the expiration timer.

### 4. Voluntary handoff
Contributor A transfers work to Contributor B:
```sh
# Contributor A signs the sender half
uv run ditto --network finney bounty handoff \
  --claim-id 1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b --side from \
  --to-hotkey 5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y \
  --coldkey default --hotkey miner1

# Contributor B signs the receiver half and submits both proofs
uv run ditto --network finney bounty handoff \
  --claim-id 1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b --side to \
  --from-hotkey 5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY \
  --coldkey dev-wallet --hotkey dev-hotkey --submit
```

### 5. Appealing a revocation
A contributor submits a structured appeal against a revocation:
```sh
uv run ditto --network finney bounty appeal \
  --claim-id 1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b \
  --revocation-entry-hash 3a8f... \
  --reason "Delivered blocking dependency PR #2075 before deadline" \
  --coldkey default --hotkey miner1
```

## Threat model and mitigations

| Vector | Consequence | Mitigation |
|---|---|---|
| Compromised GitHub account | Attacker attempts to change payout address in PR or comment | Payout destination is derived from on-chain Subtensor ownership of the signing hotkey. GitHub account metadata is ignored for financial routing. |
| Signature replay attack | Attacker resubmits recorded signature on another bounty or network | Signatures bind domain tag, netuid, repository, issue number, revision, spec digest, and single-use UUIDv4 nonce. |
| Terms alteration during work | Maintainer edits issue requirements mid-flight | The claim payload binds `spec_digest`. Any specification modification generates a new revision without altering existing active claim terms. |
| Bounty squatting | Bad actor claims bounties to block competitors without intending to deliver | Maximum 2 active exclusive claims per payee, mandatory 7-day TTL with maximum 2 self-renewals, and 72-hour cooldown upon expiration. |
| Post-review commit rewrite | Submitter swaps reviewed code for unauthorized payload | `submit` binds the exact `head_sha`. Review and payout verification check the signed commit hash. |
| Unauthorized handoff | Third party attempts to seize reservation | Handoff requires cryptographic signatures from both current holder and new recipient. |
| Key theft or rotation | Contributor rotates coldkey on chain | Contributor submits `rebind-payee` signed with new on-chain owner coldkey. Payout verifier asserts current on-chain owner equals payee coldkey. |

## Acceptance mapping (#2045)

| Requirement | Implementation and Specification |
|---|---|
| Replay-resistant claims bound to repo, issue, contributor, revision, expiry | Bound in `bounty_claim_message` payload format with UUID nonces, netuid, and freshness checks |
| GitHub identity alone cannot redirect payment | Payout target derived from `SubtensorModule.Owner(claimant_hotkey)` and verified against `payee_coldkey` |
| Reservation and concurrency rules visible before work begins | Specified in `BountySpec` with immutable `spec_digest`, partial unique index for `exclusive`, and explicit caps for `open` |
| Claim, renew, handoff, and appeal examples documented | Fully detailed in Contributor Workflow Examples with exact CLI commands and signed payloads |
| Team disclosure and conflict mitigation | Formalized via `team_digest`, individual member attestations, and single designated payee |
