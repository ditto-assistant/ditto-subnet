# SN118 bounty claims, reservations, and contributor identity

Status: **proposed**. This is the claim contract for the maintenance treasury in
[maintenance-treasury.md](maintenance-treasury.md). The parent epic is #2054.
#2046 owns acceptance and payout, and #2047 owns the board and contributor
guide.

A claim does one job: it binds a piece of scoped work to a verifiable subnet
identity and a payment destination, for a limited time, under rules that were
public before the work began. A claim does not approve work, promise payment,
or move funds.

## Principles

1. **The hotkey decides who you are; GitHub is only a label.** Every claim
   action is signed by a hotkey or by the coldkey that owns it on chain. A
   GitHub login is recorded for display and for linking PRs, but it can never
   choose, change, or approve a payment destination.
2. **Payment goes to the payee recorded on the claim, and only that payee can
   move it.** At claim time the payee coldkey must be the chain owner of the
   hotkey, and it is signed into the claim. After that, whoever owns the hotkey
   later can't redirect payment for work already claimed. The only ways to
   change the payee are the recorded payee's own signature or an audited,
   non-conflicted reassignment.
3. **Every signature is used once, for one purpose.** Each signature binds a
   domain tag, the netuid, the repository, the issue, the bounty revision, the
   contributor, a nonce, the time it was issued, and an expiry.
4. **The rules are fixed before work starts.** Concurrency mode, reservation
   length, renewal limits, and reward range are part of the signed bounty
   revision. Changing them creates a new revision, and existing claims keep the
   terms they signed.
5. **The Platform API is the only authority.** Claims are submitted to the
   Platform API and appended to the treasury ledger. GitHub comments and labels
   mirror that state. A signature pasted into a GitHub comment is not a claim.

These rules reuse the signed-action pattern already used for owner links
(`apps/platform/ditto/api_server/attestation.py`) and handle claims
(`apps/platform/ditto/api_server/name_claim.py`).

## The bounty revision

A bounty is identified as `{owner}/{repo}#{issue}`, for example
`ditto-assistant/ditto-subnet#2045`. Before it can be claimed, a maintainer
publishes a **bounty revision** through Platform. The revision is an integer
that starts at 1, plus a `spec_digest`: the SHA-256 of the canonical JSON of
these fields.

| Field | Meaning |
|---|---|
| `scope` | What is in and out of scope |
| `acceptance_evidence` | The objective evidence the reviewer will check (tests, receipts, live behavior) |
| `reward_min_tao`, `reward_max_tao` | The reward range, in TAO rao |
| `reviewer` | The named reviewer; must not be conflicted (see the treasury contract) |
| `dependencies` | Bounties or PRs that must land first |
| `bounty_expires_at` | After this time, no new claims or renewals |
| `concurrency` | `exclusive` or `open` (see [Concurrency](#concurrency)) |
| `max_open_claims` | For `open` only: the maximum number of simultaneous reservations |
| `reservation_days` | Length of the first reservation and of each renewal (default 7) |
| `max_renewals` | Renewals allowed without reviewer approval (default 2) |
| `review_days`, `rework_days`, `max_change_rounds` | Submission review deadline, rework window, and rework round limit (defaults 7, 7, 2) |
| `policy_revision` | The treasury policy revision this bounty is governed by |

The board (#2047) shows the revision and the digest on the issue. Editing the
issue text does not change the terms: only a new Platform revision does. That
new revision applies to new claims, and to existing claimants only when they
opt in by renewing against it.

## Identity

- **Claimant hotkey.** Any hotkey with an on-chain owner. At a finalized block,
  `SubtensorModule.Owner(hotkey)` must resolve to a coldkey. An SN118 UID is
  **not** required, so outside contributors don't need to buy a registration
  (decision C1).
- **Payee coldkey.** It must equal `Owner(claimant_hotkey)` at the claim block,
  and it is signed into the claim. From then on, that **payee of record** is
  what establishes who did the work. The current chain owner does not. A later
  owner of the hotkey may be a buyer or an attacker, not the contributor, so a
  change of `Owner` never moves payment for existing work. See
  [Payee changes](#payee-changes).
- **Key kind.** The claim itself is proved with the hotkey or with its on-chain
  owner coldkey. Handle claims use the payment-record coldkey instead, but
  outside contributors have no payment record, and at claim time the chain owner
  is authoritative. Once the claim exists, any action that affects payment
  (payee rebind, and the `from` half of a handoff) must be signed by the
  **recorded payee coldkey**. A hotkey signature can't change where money goes,
  and neither can the hotkey's current owner.
- **GitHub login.** It is signed into the claim so reviewers can match PRs to
  claimants. A PR opened by another account is still linkable through a signed
  `submit` action. A PR opened by the claimed login but never signed-linked is
  not submitted work.
- **Teams.** A team claim names one payee coldkey and lists its members. Each
  member signs a membership half over the team digest. The payee and the members
  are public. Working as a team is allowed and expected; an undisclosed payee is
  what counts as a conflict.

## Signed actions

Each action signs the exact UTF-8 bytes of its fields joined by `:`. The
format matches `claim_message` in `name_claim.py`:

- `issued_at` and the expiry timestamps are UTC ISO-8601 with microseconds.
- `nonce` is a UUIDv4.
- `key_kind` is `hotkey` or `coldkey`.
- `signer` is the SS58 address that signed.
- `repo` is `owner/name`. GitHub logins and repository names can't contain `:`.

Platform never parses a message it receives. It rebuilds the bytes from the
structured request fields and checks the signature against them.

| Action | Domain | Fields after the domain |
|---|---|---|
| Claim | `ditto-bounty-claim:v1` | `netuid:repo:issue:bounty_revision:spec_digest:claimant_hotkey:payee_coldkey:github_login:team_digest:reservation_expires_at:nonce:issued_at:key_kind:signer` |
| Team member | `ditto-bounty-member:v1` | `netuid:repo:issue:bounty_revision:team_digest:member_hotkey:nonce:issued_at:key_kind:signer` |
| Renew | `ditto-bounty-renew:v1` | `netuid:claim_id:claimant_hotkey:bounty_revision:spec_digest:reservation_expires_at:nonce:issued_at:key_kind:signer` |
| Submit (link PR) | `ditto-bounty-submit:v1` | `netuid:claim_id:claimant_hotkey:repo:pr_number:head_sha:nonce:issued_at:key_kind:signer` |
| Handoff half | `ditto-bounty-handoff:v1` | `netuid:claim_id:from_hotkey:to_hotkey:to_payee_coldkey:nonce:issued_at:side:key_kind:signer` |
| Withdraw | `ditto-bounty-withdraw:v1` | `netuid:claim_id:claimant_hotkey:nonce:issued_at:key_kind:signer` |
| Payee rebind half | `ditto-bounty-payee:v1` | `netuid:claim_id:claimant_hotkey:old_payee_coldkey:new_payee_coldkey:nonce:issued_at:side:signer` |
| Appeal | `ditto-bounty-appeal:v1` | `netuid:claim_id:claimant_hotkey:subject_entry_hash:reason_digest:nonce:issued_at:key_kind:signer` |

Notes:

- `team_digest` is the SHA-256 of canonical JSON
  `{"payee_coldkey": ..., "members": [{"hotkey": ..., "github_login": ...}, ...]}`,
  with members sorted by hotkey. A solo claim uses the digest of a one-member
  team, so every claim has the same shape.
- `claim_id` is the server-issued UUID returned by the claim. Every follow-up
  action binds it, so a signature for one claim can't be reused on another.
  This is the same approach as `ditto-name-endorse:v1` and
  `ditto-name-withdraw:v1`.
- `head_sha` binds the exact commit submitted for review. #2046 approves that
  commit, not a branch name that can be rewritten.
- For a handoff, `side` is `from` or `to`. A handoff needs **both** halves, just
  as an owner link needs both endpoints. That way no one can hand themselves
  someone else's reservation, and no one can be handed work they didn't accept.
  The `from` half is always signed by the recorded payee coldkey.
- For a payee rebind, `side` is `old` or `new`. Both halves are coldkey
  signatures. The `old` half is signed by the recorded payee and the `new` half
  by the new payee. A rebind where `old_payee_coldkey` equals
  `new_payee_coldkey` is a *confirmation*: it needs only the one signature, and
  it keeps the payee of record after the hotkey's owner has changed.
- `subject_entry_hash` is the treasury ledger entry being appealed, for example
  a revocation. `reason_digest` is the SHA-256 of the appeal text, which is
  stored beside the entry.

### Replay protection

| Guard | Rule |
|---|---|
| Domain tag and version | A signature can't be replayed into the upload, owner-link, name-claim, or validator lanes |
| `netuid` | A signature from another deployment is rejected |
| Repository, issue, bounty revision, `spec_digest` | A claim can't be moved to another bounty or to changed terms |
| `claim_id` | Follow-up actions can't be moved to another claim |
| `nonce` | Unique across **all** bounty actions (one table with a unique constraint). A second submission gets `409` |
| `issued_at` | Uses the owner-link window: at most `MAX_ISSUED_AT_SKEW` (5 minutes) in the future, and at most `MAX_ATTESTATION_AGE` (24 hours) old |
| `reservation_expires_at` | Must be at most `reservation_days` after the server's accept time and no later than `bounty_expires_at`. The server stores the earlier of the signed and computed expiry |

## Reservations

### States

```
active ──submit──▶ submitted ──accept──▶ (acceptance and payout: #2046)
  │  ▲                │
  │  ├──renew         ├──changes_requested──▶ active (bounded rework window)
  │  └────────────────┼──not_a_submission / head moved──▶ active (clock resumes)
  │                   ├──rejected──▶ released (appealable)
  │                   └──stale_submission──▶ released (automatic, audited)
  ├──▶ expired
  ├──▶ withdrawn
  ├──▶ revoked (audited reason; appealable)
  └──▶ handed_off (a new active claim for the recipient)
```

Every transition out of `submitted` appends a ledger entry. The entry records
the actor (the reviewer, or `system` for automatic releases), the reason, and
the PR state that justified it.

- **Accepted claim.** The server verifies the signature, identity, freshness,
  nonce, and concurrency rules. It then appends a `reservation` entry to the
  treasury ledger and returns `claim_id`. The entry records the bounty revision
  and digest, both keys, the GitHub login, the team, and the expiry.
- **Renew.** Allowed only in the last 48 hours before expiry, at most
  `max_renewals` times. More renewals need reviewer approval, recorded with the
  reviewer as actor. Renewing against a newer bounty revision is how a claimant
  accepts its terms.
- **Submit.** Links a PR and its exact head commit, and pauses the expiry
  clock while the work is under review. The pause is bounded; see
  [Submission review](#submission-review).
- **Expire.** Handled by the server clock. The reservation is released and the
  ledger records `reservation_release` with reason `expired`. The same payee
  coldkey can't claim the same bounty again for 72 hours, so a contributor
  can't squat on a bounty by cycling hotkeys.
- **Withdraw.** Signed by the claimant, and takes effect immediately.
- **Revoke.** Done by a maintainer, with a reason from a fixed set and free
  text. The reasons are `inactive`, `scope_violation`, `undisclosed_conflict`,
  `misconduct`, `bounty_cancelled`, and `superseded_by_revision`. Revocation
  appends a ledger entry with actor and reason and can be appealed. A GitHub
  label change is never a revocation.

### Concurrency

The bounty revision shows its mode on the board before anyone starts work.

- **`exclusive`** (default): one `active` or `submitted` reservation per bounty.
  A partial unique index in the database enforces this, the same way active
  handle claims are enforced, so the rule doesn't depend on application logic.
  A second claim gets `409` with the holder's public reservation and its expiry.
- **`open`**: up to `max_open_claims` concurrent reservations. Proposals compete.
  The reviewer accepts one, or makes partial or shared awards under #2046. The
  other reservations are released with reason `superseded`. That is not a
  rejection of the contributor.
- **Per-payee limit:** at most two `exclusive` reservations per payee coldkey,
  counting both `active` and `submitted`. Submitting doesn't free a slot. A
  slot is freed only when the reservation leaves both states: it is accepted
  into #2046, released, expired, withdrawn, revoked, or handed off. `open`
  claims don't count. Team claims count against the team's payee coldkey.

### Submission review

A submission pauses the contributor's clock, so it has to be real work, and
the pause has to end. Otherwise placeholder PRs could hold exclusive bounties
forever.

- **What counts as a submission.** Platform accepts `submit` only if the PR:
  - is open and not a draft;
  - targets the bounty repository's default branch;
  - has the signed `head_sha` as its current head;
  - references the bounty issue.

  Anything else is refused and the clock keeps running.
- **The reviewer decides within a deadline.** Within `review_days` (default 7)
  of a submission, the reviewer records one of four outcomes:
  - `accept`: the work goes on to #2046.
  - `changes_requested`: see the next point.
  - `rejected`: the reservation is released and the decision can be appealed.
  - `not_a_submission`: the reservation goes back to `active` with the clock
    resuming from where it paused, not a fresh window. A second
    `not_a_submission` on the same claim may be followed by revocation with
    reason `inactive`.

  If the deadline passes without a decision, the reservation stays held, since
  the contributor isn't at fault. Platform appends a `review_overdue` ledger
  entry, flags it on the board and in Backroom, and reassigns the reviewer. The
  reassigned reviewer has one more `review_days`. After that, the reservation
  escalates to every non-conflicted maintainer, and the board and Backroom
  report it on every read until someone records a decision. A held submission
  can therefore outlast the deadline only through visible maintainer inaction,
  never through anything the claimant does. It still counts toward the
  claimant's per-payee limit.
- **Rework is limited.** `changes_requested` sends the claim back to `active`
  with a rework window of `rework_days` (default 7). This window is not a new
  `reservation_days` period and not a renewal. If the rework window ends
  without a new signed `submit`, the reservation is released with reason
  `expired`, recorded in the ledger. After `max_change_rounds` (default 2)
  rounds, the reviewer must accept or reject.
- **Stale submissions are released automatically.** Platform re-reads the PR,
  both on a schedule and when it receives webhooks. The reservation is released
  with reason `stale_submission`, with actor `system` and the PR state recorded
  as evidence, when either:
  - the PR is closed without merging, or turned back into a draft, and stays
    that way for 72 hours;
  - the head moves away from the signed `head_sha` and no new `submit` arrives
    within 72 hours.

  Until then, a moved head returns the claim to `active` with the clock
  running, so unsigned commits never extend a pause. The claimant can appeal a
  stale release like any other release.

### Payee changes

Changing the payee is never self-service for whoever controls the hotkey now.
Platform compares `Owner(claimant_hotkey)` with the payee of record whenever a
claim changes state, and again at payout (#2046). If they differ, payout holds
until one of these three paths is completed.

| Situation | What resolves it | Who signs or decides |
|---|---|---|
| The contributor rotated keys and still holds the old payee coldkey | Two-half rebind (`old` → `new`) | The recorded payee coldkey **and** the new coldkey |
| The contributor sold or moved the hotkey but keeps the payee coldkey | Confirmation rebind (`old` = `new`) | The recorded payee coldkey alone. Payment stays with the contributor, and the buyer gets nothing for this work |
| The recorded payee coldkey is lost or compromised | `payee_reassignment` dispute | Decided by non-conflicted operators at the treasury contract's large-award threshold, whatever the amount. The decision and evidence are published, then a 14-day objection window runs before any payment. The new payee must sign an acceptance half |

A new owner of the hotkey can't start any of these paths. They can make new
claims with that hotkey, and those claims record the new owner as payee. They
never inherit existing work. Evidence for a reassignment can include earlier
owner-link attestations between the old and new keys, commit history, and
review threads. None of it is sufficient on its own, and GitHub identity never
is.

### Public visibility

The bounty's public reservation view lists:

- claim ID, state, and expiry;
- claimant hotkey, payee coldkey, and GitHub login;
- team members;
- the bounty revision;
- the linked PR and its head commit.

These are already public on chain or on GitHub. The board (#2047) reads this
view to answer "stale" and "blocked" queries. Appeal text is published only if
the appellant chooses to.

## Examples

The CLI surface proposed below follows `ditto name`. It prints every key and
digest before asking for confirmation, and signing never transfers TAO. The
addresses are Substrate development keys used for illustration only.

### Example: claim

```sh
uv run ditto --network finney bounty claim \
  --repo ditto-assistant/ditto-subnet --issue 2045 --revision 1 \
  --coldkey alice --hotkey default --github-login alice-dev
```

Signed bytes (one line):

```
ditto-bounty-claim:v1:118:ditto-assistant/ditto-subnet:2045:1:<spec_digest>:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY:5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty:alice-dev:<team_digest>:2026-09-28T12:00:00.000000+00:00:1b4e28ba-2fa1-4d3b-a3f5-ef19b5a7633b:2026-09-21T12:00:00.000000+00:00:hotkey:5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY
```

Platform then checks these, in order:

1. `Owner(5Grw…)` equals `5FHn…` at a finalized block.
2. The bounty revision 1 digest matches.
3. The bounty is `exclusive` and has no live reservation.
4. The nonce is unused.

It returns `claim_id` and the ledger entry hash. The board mirrors the issue to
**Claimed**.

### Example: renew

Within 48 hours of expiry:

```sh
uv run ditto --network finney bounty renew --claim-id <claim_id> \
  --coldkey alice --hotkey default
```

This signs `ditto-bounty-renew:v1:118:<claim_id>:5Grw…:1:<spec_digest>:<new_expires_at>:…`.
The third renewal is refused with `reviewer approval required` unless the
reviewer has recorded an extension.

### Example: submit a PR

```sh
uv run ditto --network finney bounty submit --claim-id <claim_id> \
  --pr 2061 --head-sha 3f9c2a1… --coldkey alice --hotkey default
```

The reservation moves to `submitted`. If a new commit is pushed after
submitting, the contributor must submit again with the new `head_sha`.
Otherwise the reviewer reviews the commit that was signed.

### Example: handoff

Alice can't finish, and Bob agrees to take over:

```sh
# Alice signs the "from" half with her recorded payee coldkey and shares the
# printed JSON with Bob.
uv run ditto --network finney bounty handoff --claim-id <claim_id> --side from \
  --to-hotkey 5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y \
  --coldkey alice --hotkey default --key-kind coldkey
# Bob signs the "to" half and submits both halves.
uv run ditto --network finney bounty handoff --claim-id <claim_id> --side to \
  --from-hotkey 5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY \
  --coldkey bob --hotkey default --submit
```

Alice's claim becomes `handed_off`. Bob gets a new `active` claim with a fresh
expiry, on the same bounty revision. Alice's work before the handoff is not
paid automatically. The reviewer may propose a shared award under #2046, and
both parties see that decision in the ledger.

### Example: appeal a revocation

```sh
uv run ditto --network finney bounty appeal --claim-id <claim_id> \
  --entry <revocation_entry_hash> --reason-file appeal.md \
  --coldkey alice --hotkey default
```

The appeal is assigned to a maintainer who didn't make the revocation and isn't
conflicted, and it must be decided within 14 days. The decision is a
`dispute_resolution` ledger entry that references the revocation. If upheld,
the reservation is restored with its remaining time plus the time the appeal
took.

### Example: key rotation (payee rebind)

Alice moves her hotkey to a new coldkey, so `Owner` has changed and payout is
held. Owning the hotkey now proves nothing about who did the work, so Alice
proves that the rotation was her own. She signs with **both** the recorded
payee coldkey and the new one:

```sh
uv run ditto --network finney bounty rebind-payee --claim-id <claim_id> \
  --old-coldkey alice --new-coldkey alice-new --hotkey default
```

Platform accepts the rebind only if both of these hold:

- the `old` half is signed by the payee of record, and the `old_payee_coldkey`
  in the message matches it;
- the `new` half is signed by `new_payee_coldkey`.

If Alice sold the hotkey but kept her coldkey, she signs a confirmation with
`--new-coldkey alice` (the same coldkey), and payment stays with her. A buyer
signing with the new owner coldkey alone is refused. If Alice lost the old
coldkey, she opens a `payee_reassignment` dispute
([Payee changes](#payee-changes)). If she rotated the *hotkey* instead, the old
claim is handed off to the new hotkey, and the `from` half is signed by the
recorded payee coldkey.

## Threat model

| Threat | Mitigation |
|---|---|
| GitHub account takeover redirects payment | The payee of record comes from the signed claim. GitHub can't sign a submit, handoff, or rebind |
| Replay of a captured claim or follow-up action | Domain, netuid, bounty revision, digest, claim ID, a nonce that can't be reused, and a 24-hour freshness window |
| Terms changed after work began | The claim binds `spec_digest`. New revisions apply only when the claimant opts in by renewing |
| Squatting on bounties | Limited reservation length and renewals, a per-payee cap that counts `submitted` claims, and a 72-hour cooldown keyed on the payee coldkey |
| Placeholder PRs holding bounties indefinitely | Submission validity checks, a `review_days` deadline, `not_a_submission` resuming the paused clock, limited rework rounds, and automatic audited `stale_submission` release |
| Someone takes over another person's reservation | A handoff needs both halves. Revocation needs an audited maintainer action and can be appealed |
| A PR is rewritten after review | `submit` binds `head_sha`. New commits need a new signed submit |
| A hotkey leaks, is sold, or changes owner | The new holder can make new claims but can't move the payee of existing work. Payout holds on an owner mismatch until the recorded payee signs a rebind or confirmation, or an audited reassignment completes |
| The payee coldkey is lost or compromised | Non-conflicted `payee_reassignment` at the large-award threshold, with public evidence and a 14-day objection window |
| Undisclosed shared payee | The team digest and payee are public, and conflict findings are ledger entries |

## Acceptance mapping (#2045)

| Acceptance item | Where it is satisfied |
|---|---|
| Claims can't be replayed and are bound to repository, issue, contributor, revision, and expiry | [Signed actions](#signed-actions), [Replay protection](#replay-protection) |
| GitHub identity alone can't redirect payment | Principles 1–2, [Identity](#identity), [Payee changes](#payee-changes), threat model |
| Reservation and concurrency rules are visible before work begins | [The bounty revision](#the-bounty-revision), [Concurrency](#concurrency), [Submission review](#submission-review), [Public visibility](#public-visibility) |
| Claim, renew, handoff, and appeal examples are documented | [Examples](#examples) |

## Decisions that need approval

- **C1:** whether any chain-owned hotkey may claim, or only SN118-registered
  hotkeys. The first is recommended, so outside contributors can take part.
- **C2:** defaults for `reservation_days` (7), `max_renewals` (2), the per-payee
  limit on exclusive claims (2, counting `submitted`), `review_days` (7),
  `rework_days` (7), `max_change_rounds` (2), the stale-submission grace (72
  hours), and the cooldown after expiry (72 hours).
- **C3:** whether `exclusive` is the default mode, or `open` for documentation
  and incident bounties.

## Implementation handoff

A single Platform and CLI stack implements this contract:

- a `bounty_claims` table with a status `CHECK`, paired timestamps, and a
  partial unique index for `exclusive` bounties;
- a nonce table shared by every bounty action;
- message builders and verification, reusing `verify_signature` and the
  owner-link freshness constants;
- the `ditto bounty` CLI;
- ledger appends for each action;
- a submission watcher that checks PR validity at submit, the review deadline,
  and stale-submission release from GitHub webhooks and a scheduled re-read;
- a public reservation read;
- Backroom MCP read tools for reservations, overdue reviews, stale releases,
  payee holds and reassignments, revocations, and appeals.

Acceptance lives in #2046, and so does the payout-time check that the payee of
record still matches `Owner(hotkey)` (or has been rebound or reassigned).
