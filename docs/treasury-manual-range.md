# Manual transfer approval and source aggregation (SN-66)

The separate `ditto-treasury-manual-limit-v1` cold approval permits positive
manual amounts up to 2,500 SN118 alpha, inclusive, to the existing signed service
destinations. It binds the exact base custody digest and collector coldkey.
It does not change emission allocation, registration, fee limits or recurring
distribution authority. The original custody signature/digest remain intact.

One version-2 manual envelope identifies up to 512 ordered, unique earning
sources and exact amounts summing to the single transfer. Custody re-proves each
source and its unconsumed bucket share before arming and before signing. The
existing journal retains one operation and the exact signed transaction before
dispatch. Unknown delivery only reconciles that transaction. The original
canary, all previous operations, source remainders and the one-active-claim fence
remain. A 2,500 ceiling does not authorize withdrawing unproved principal.

Platform verifies cold approval in the readiness report, previews the same
ordered claims, and rechecks freshness and exact confirmation at submission.
After finalization it independently verifies every source against its immutable
epoch approval/settings and the one actual chain effect. Stable epoch lock order
and cumulative consumption checks cover both legacy and aggregate receipts.
One public receipt and transaction link are published atomically; source claims
are included in the public feed. A vendor credit is still not proven by this
transfer.

## Offline signing

Prepare a new, exclusive directory using public inputs only:

```sh
PYTHONPATH=packages/ditto-screening-protocol .venv/bin/python \
  scripts/treasury_manual_packet.py \
  --collector-approval /private/public-collector-approval.json \
  --collector-policy-digest EXACT_CURRENT_DIGEST \
  --output /private/new-manual-range-packet
```

Copy the generated directory to the USB. On the existing offline Ubuntu 24.04 /
Python 3.12 laptop, run `bash /media/USER/USB/PACKET/sign.sh`, type the displayed
manual-range confirmation, and unlock the existing encrypted collector coldkey.
Only `public/manual-limit-approval.json` and sanitized status return on USB.
No chain transaction, new key, wallet export or journal migration is involved.
The signed maximum is reusable for manual amounts within the range; no cold
laptop run per transfer is needed. Revoking the active approval disables new
aggregate claims, while embedded historical signatures remain audit evidence.

## Activation and rollback

After verifying the returned public signature and reviewed runtime adoption,
install the approval root-owned/read-only. Add paired `--manual-approval FILE`
and `--manual-approval-sha256 DIGEST` to the manual worker; keep automatic
transfer units masked. No unsigned approval may be installed. Public Backroom
must show the exact approval, effective ceiling and fresh source inventory.

Do not reset, replace or restore an older financial journal. After a version-2
intent exists, rollback must retain a batch-aware reconciler and all attribution
history. Removing the active manual approval stops new batch authority; it
does not erase an armed or delivered transfer. A pending request must reconcile
with its original bytes, never be replaced. Old version-1 envelope digests remain
unchanged. A future custody digest requires a new bound manual approval.

Max is the minimum of the signed ceiling, the destination's verified remaining
mature earnings, and current stake minus the requested reserve. Until receipt
catch-up proves sufficient earnings, the spendable amount can be below 2,500.

With manual approval deployed, up to eight recent payout candidates are checked
per iteration using the finalized chain's last-step/tempo only as search hints.
Each candidate still undergoes the full source proof. These probes share the
32-block iteration budget with the contiguous scanner and publish readiness
between proofs. They neither skip nor advance that scanner's cursor; when the
scanner later reaches a proved source it must match exactly and cannot re-credit
or reset consumption. Schedule changes can miss a candidate, never invent
earnings. No recent-source probe runs a transfer tick.
