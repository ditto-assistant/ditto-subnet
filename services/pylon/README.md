# Pylon stateful epoch adaptation

The pinned Pylon image shipped `bittensor-drand==1.0.1`. Its reveal calculation
uses `(block + netuid + 1) // (tempo + 1)`. Finney's stateful scheduler instead
uses `LastEpochBlock`, `PendingEpochAt`, `SubnetEpochIndex`, `Tempo`, and
`BlocksSinceLastStep`. Pylon's background weight-task expiry also used the retired
formula. An HTTP acceptance or even a successful commit does not prove that the
weights revealed in time for Yuma.

On 2026-09-09, archived SN118 reads established this concrete incident:

| Yuma block | UID 139 vTrust | UID 0 vTrust |
| --- | --- | --- |
| 9028789 | 0.999985 | 0.999985 |
| 9029149 | 0.999985 | 0.999985 |
| 9029509 | 0.929992 | 0.999985 |
| 9029869 | 0.999985 | 0.999985 |
| 9030229 | 0.959991 | 0.999985 |

The bad block is
`0x248b9da8ae60ed5f3a133121f8c555f6bc254738542fec0c5adde4b6eb7affd8`.
The fleet-reported Pylon artifact
`ghcr.io/ditto-assistant/ditto-subnet-pylon@sha256:a80c2ba192eebfdaa525391c953a0cc0206b59cb77edcf35e8c25787dd101306`
was inspected locally without a wallet or network access. It contains drand
1.0.1 and the legacy TurboBT call (source SHA-256
`840418d3d294558943584a08b24be9b144916b6bcd24e4bc5ec6eba4cd70c6bd`).
Both rows gave UID 43 approximately 65%, UID 104 14%, and UID 134 10%.
UID 139 still gave UID 16 7% and UID 180 4%; consensus gave UID 251 7% and
UID 16 4%. The excess 3% on UID 16 plus 4% on UID 180 explains the roughly
7% clipping. Burn was not a destination in either vector.

The recurrence at block 9030229
(`0xfb4820285596749c96209c5ee752f433d88aed10c10e77781b6b18be4a1758a4`)
had the same shape with a smaller transition:
UID 139 still gave the 4% tail to UID 180 while consensus had moved it to UID 16.
That exact 4% excess accounts for vTrust 0.959991. This rules out a one-off WSL
host failure and confirms that tail changes plus reveal timing reproduce the
incident. Immediately before that Yuma block, UID 0's block-9029921 commitment
for round 32051145 was still recorded; it cleared at 9030229. UID 139's later
block-9030170 commitment for round 32052592 remained pending through 9030232.

The public commitment records at blocks 9029508 and 9029509 provide the causal
timing evidence. UID 0 committed at 9029197 for round 32048254 and revealed by
9029509. UID 139 committed at 9029448 for round 32049696 and remained pending at
9029509 and 9029539. By 9029579 it had revealed the same vector as UID 0, but the
Yuma result would not update until the next epoch. Both commits were in chain
epoch 25016. The old formula selected target blocks 9029216 and 9029577:
two different reveal windows inside one actual epoch ending at 9029509.

## Implementation

`Dockerfile.pylon` installs the checksum-pinned drand 2.0.0 CPython 3.13 wheel.
`patch_epoch_schedule.py` accepts only the exact source hashes from the pinned
Pylon 2.3.2 amd64 base image
`sha256:d7389e6132dac49cb1acad68184fd6c3759592e2e494afdb732fd8f7a3f6349a`
and TurboBT revision
`d36d1a28e761ff9d743bbcc83d091309c4d3599e`. It adapts the installed source at build
time; changes to those dependencies require an explicit patch review. That
TurboBT revision is byte-identical to the official 1.3.1 package code and is
installed with version 1.3.1; its local commit adds the same transaction and
string-RPC compatibility changes as the upstream release.
Pylon 2.3.2 also retains the upstream 2.3.1 weight-submission repair and adds
recovery when a partially initialized TurboBT client starts raising `TypeError`.

- TurboBT reads all five schedule fields at the encryption block's hash and
  calls `get_encrypted_commit_v2`. Mechanism IDs remain attached to the submitted
  extrinsic; schedule state is read using the base subnet ID.
- Missing, malformed, or inconsistent schedule state fails closed. There is no
  fallback to the retired formula.
- Pylon records the request's original epoch counter and expires retries when
  that counter changes, including after a restart or manual early epoch.
  A scheduled boundary that the chain defers does not expire the task before
  the epoch counter actually advances. Its weight-status endpoint uses the same
  stateful epoch window and includes the deferred boundary block.
- Upstream encryption, commit-reveal version 4, weight normalization, wallet
  handling, inclusion delay, and the three-block security offset are preserved.
  The patch does not copy consensus or change miner allocations or burn.

The fix removes the obsolete formula's phase-dependent reveal window. It does
not promise immediate vTrust=1: ledger changes during an epoch, mixed fleet
versions, delayed inclusion, chain deferral, and already-persisted commitments
remain distinct operational facts. Drand's security offset also means a reveal
can occur after a scoring boundary; a current row cannot be compared to an old
Yuma result as if they were contemporaneous.

## Verification and rollout

Run `uv run pytest ditto/tests/test_pylon_epoch_schedule.py` and build Pylon with
the exact named TurboBT context used by Compose/release. Then run
`bash scripts/test-pylon-epoch-schedule.sh <image>`: the installed-image tests
exercise real encryption and mocked submission/task lifecycles with networking
disabled and no wallet. The release workflow runs the same checks.

The separate read-only Backroom tool `get_validator_weight_diagnostics` exposes
block/hash-bound trust, revealed weights, pending rounds, and epoch counters.
It reports missing chain evidence as an error and never publishes ciphertext.
Its vTrust is the last Yuma result, not a fresh recomputation against later rows.

Release the signed Pylon stack through the normal updater, verify the exact
image digest across the fleet, and observe several Yuma boundaries and tail
changes. Existing ciphertext cannot be retimed by installing new code. Preserve
pending task/commit evidence and allow the old commitments to drain; do not
delete Pylon's database or wallet. Local tests and builds do not establish live
fleet convergence or delegator return recovery.
