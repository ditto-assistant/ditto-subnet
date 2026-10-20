# Build bounties: contributor guide

Build bounties pay alpha for scoped work on Ditto repositories. Each bounty
is published on Platform as a signed revision of its terms, and every claim,
submission, review decision and payment is recorded publicly. The board is
at [dittobench.ai/bounties](https://dittobench.ai/bounties), and the miner CLI
reads the same API with `ditto bounty list` and `ditto bounty show`.

**v1 runs in rehearsal.** Entry fees and payments are simulated and no alpha
moves. The board, the CLI and the API mark every rehearsal fee, refund and
award as simulated. Live funding is a separate, later decision.

The rules behind this guide are in [bounty-claims.md](bounty-claims.md).

## What a bounty promises

A bounty's current revision fixes, before anyone starts work:

- the scope, the public interfaces it may change, and the acceptance evidence
  the reviewer checks;
- the entry fee and the reward pool, both in alpha;
- three paid slots per round;
- how long an unpaid hold lasts, the delivery deadline (counted from the
  moment the fee is confirmed), and the review target;
- the reviewer's GitHub login, any dependencies, and the fee escrow coldkey
  and hotkey.

The revision's `spec_digest` is the SHA-256 of those terms, and your claim
signs it. Editing the GitHub issue changes nothing; only a new revision on
Platform changes the terms, and it applies to new claims only.

## Before you claim

1. A hotkey whose owner coldkey is on chain. An SN118 UID is not required.
   The owner coldkey is your payee of record: any refund or award for this
   claim goes there. If the hotkey changes hands later, an award's payout
   waits until its owner is that coldkey again; refunds never wait.
2. A verified GitHub link for that hotkey, from `uv run ditto link-github`.
   See [GitHub links](GITHUB-LINKS.md).
3. A current contributor licence agreement signature, from
   `uv run ditto cla sign`. Bounty claims always need it, whatever the upload
   gate's mode. See [the miner guide](MINER.md#github-identity-and-contributor-agreement).

A GitHub account and a payee coldkey can each hold one live slot per bounty,
and the bounty's reviewer cannot claim it.

## Claim a slot

```bash
uv run ditto bounty show ditto-assistant/ditto-subnet#2942
uv run ditto bounty claim ditto-assistant/ditto-subnet#2942 \
  --github-login YOUR_LOGIN --wallet.name YOUR_WALLET --wallet.hotkey YOUR_HOTKEY
```

`claim` saves the exact terms under `~/.cache/ditto/bounty/`, prints the fee,
pool, deadlines and escrow keys, and signs only after you type
`CLAIM BOUNTY <owner/repo#issue> REVISION <n> <spec_digest>`. It then holds
the lowest open slot of the current round and pays and confirms the entry fee.
Your delivery deadline starts when the fee is confirmed.

If the command stops after the hold, finish with
`uv run ditto bounty confirm CLAIM_ID` on the same machine: the rehearsal fee
derives from your claim's signature, which `claim` saved there and nobody else
has. Check a claim at any time with `uv run ditto bounty status CLAIM_ID`.

You never lose a fee to timing. A fee that arrives after its hold lapsed,
after the slot went to someone else, after your link, CLA signature or owner
coldkey changed, or a second payment for the same claim, is recorded with a
full refund obligation to your payee coldkey.

## Deliver

Open a pull request against the bounty's repository, then submit its exact
commit:

```bash
uv run ditto bounty submit CLAIM_ID --pr 3001 --head-sha FULL_40_HEX_SHA \
  --wallet.name YOUR_WALLET --wallet.hotkey YOUR_HOTKEY
```

Type `SUBMIT BOUNTY CLAIM <claim id> PR <number> <sha>` to sign. You can
submit while the slot is claimed or in rework, before its deadline. The
reviewer decides on that commit; if you push again after a change request,
submit the new commit.

The reviewer then records one of three decisions, each with a public reason:

- **Accept.** The work is accepted with public credit. Your entry fee is
  refunded in full.
- **Request changes.** The slot moves to rework with a new deadline. Submit
  again before it ends.
- **Reject.** The slot closes as rejected.

The review target is set in the terms. A review that runs past it stays held,
since you are not at fault: the board flags it as overdue, and an operator can
reassign the reviewer, which starts a fresh review window.

## Withdraw or miss the deadline

`uv run ditto bounty withdraw CLAIM_ID` gives up a claimed or rework slot.
Type `WITHDRAW BOUNTY CLAIM <claim id>` to sign. If the delivery or rework
deadline passes without a submission, the slot expires. In both cases the
entry fee is retained.

## Appeal

You can appeal a rejection or a missed deadline once per decision:

```bash
uv run ditto bounty appeal CLAIM_ID --event EVENT_ID --reason-file appeal.md \
  --wallet.name YOUR_WALLET --wallet.hotkey YOUR_HOTKEY
```

The event id is listed next to the decision in the bounty's history on the
board, and in `uv run ditto bounty show <owner/repo#issue> --json`. Type
`APPEAL BOUNTY CLAIM <claim id> EVENT <id>` to sign. Your statement stays private; only its
SHA-256 is public. An operator upholds or denies the appeal, and a rejection
is never judged by the reviewer who rejected it. An upheld appeal restores
your claim with a new delivery deadline. While an appeal is pending, the slot
cannot be reopened for someone else.

## Fees, awards and payments

Every confirmed entry fee ends in exactly one outcome:

| What happened | The fee |
|---|---|
| Your work is accepted | Refunded in full |
| An operator cancels the slot | Refunded in full |
| You withdraw, miss the deadline, or the work is rejected | Retained once the slot is reopened for a new claim |

Accepted work can receive an award from the bounty's reward pool, with public
credit. Awards never exceed the pool in the revision you signed, and several
accepted claims can share it. A bounty's terms never promise a particular amount.

An award or refund is an obligation until it is paid. Payment needs approval
by as many distinct operators as the payout policy requires for its amount,
and whoever decided the award or accepted the work cannot approve it. An
award is held while your hotkey's owner is not your payee coldkey. Each
approved payment is sent once and reconciled against its chain receipt. The
board shows each obligation's state, its approvals and every transfer. In
rehearsal the transfer is simulated.

A merge, deployment or adoption of your work is recorded on the board as a
delivery fact. None of them is a condition for payment.

## What is public

The board shows a bounty's terms and revisions, every slot and its claimant's
GitHub login, hotkey and payee coldkey, each submission's pull request and
commit, review decisions with their reasons and credit, appeal outcomes,
obligations, transfers and delivery facts. Operator identities, appeal
statements, and the reasons operators give for pausing claims, cancelling or
reopening slots and deciding awards stay private.

## Signing keys

Every action is signed by the claim's hotkey by default. With
`--key-kind coldkey`, the claim's payee coldkey signs instead. The current
owner of a hotkey that has changed hands cannot act on your claim.

## When a request is refused

| Message | What to do |
|---|---|
| `bounty terms changed: review revision N` | A new revision was published. Run `claim` again to review and sign it |
| `new claims are paused for this bounty` | Wait for operators to resume claims. Existing holds and payments are unaffected |
| `every slot in round N is taken` | No slot in the current round is open |
| `this GitHub account or payee coldkey already holds a slot on this bounty` | One live slot per account and payee |
| `the bounty's reviewer cannot claim it` | The GitHub account named as the bounty's reviewer, in any letter case, cannot claim a slot on it |
| A CLA or GitHub link message | Run `ditto cla sign` or `ditto link-github`, then claim again |
| `bounty action nonce already used` | That signature was already used for a different request. An exact retry returns the original result |
