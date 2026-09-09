# Pylon stateful epoch adaptation

The pinned Pylon image shipped `bittensor-drand==1.0.1`. Its reveal calculation
uses `(block + netuid + 1) // (tempo + 1)`. Finney's stateful scheduler instead
uses `LastEpochBlock`, `PendingEpochAt`, `SubnetEpochIndex`, `Tempo`, and
`BlocksSinceLastStep`. Pylon's background weight-task expiry also used the retired
formula. An HTTP acceptance or even a successful commit does not prove that the
weights revealed in time for Yuma.

## What this removes: the phase-dependent reveal lane

Under the retired formula a commit's reveal window depended on *where in the
epoch* it was made. The legacy period is 361 blocks against a real tempo of 360,
so the fake boundary drifts one block per epoch and lane membership rotates
through the fleet. Two archived SN118 commits from chain epoch 25016 show the
mechanism: UID 0 committed at 9029197 and the formula targeted 9029216, inside
the same epoch; UID 139 committed at 9029448 and it targeted 9029577, 68 blocks
past the real boundary at 9029509. Both belonged to one chain epoch, yet one
revealed in the next fold and one in the fold after. At head 9032496 the same
split was live: UID 0's pending commit (block 9032460, round 32061278) implied
a reveal at 9032465 in its own epoch, while UID 45, already on the stateful
schedule, committed at 9032400 for round 32062423, the next boundary 9032749
plus drand's three-block security offset.

The fleet-reported Pylon artifact
`ghcr.io/ditto-assistant/ditto-subnet-pylon@sha256:a80c2ba192eebfdaa525391c953a0cc0206b59cb77edcf35e8c25787dd101306`
was inspected locally without a wallet or network access. It contains drand
1.0.1 and the legacy TurboBT call (source SHA-256
`840418d3d294558943584a08b24be9b144916b6bcd24e4bc5ec6eba4cd70c6bd`).

With drand 2.0 every commit made in epoch N encrypts for the boundary that ends
N plus three blocks, so it enters fold N+2 together with every other epoch-N
commit regardless of phase. That is a real defect fixed, and it is a
prerequisite for anything that pins the ledger per epoch. It is not, on its own,
the cause of the vTrust dips.

## What this does not fix: the ledger-sampling gap

The archived incident at Yuma block 9029509 was not one validator. Eight
validators (UIDs 45, 54, 70, 71, 72, 92, 139, 187) dipped to about 0.93, four
of them not on our stack, all having committed at phases 299 to 351 of epoch
25016. The three at phases 48, 130, and 162 (UIDs 0, 34, 28) carried the new
7%/4% tail on UIDs 251 and 16; the late committers still carried the old tail on
UIDs 16 and 180. The split is by *ledger read time*: the Platform ledger moved
the tail between roughly 9029140 and 9029197, and everyone who read before
that carried the old tail into the fold. Fold 9030949 is clearer still: UIDs
54, 70, 71, and 139 revealed UID 134 as champion at 65% while UID 0, on the
same code roughly 100 blocks later, revealed UID 43 with 134 second; the next
fold everyone agreed on 43 again, and those four took vTrust 0.49 for reading
the ledger during a twenty-minute crown flip. Across 60 sampled folds, 48 had
at least one permitted validator below 0.99.

No reveal schedule reconciles validators that read different ledgers. What the
uniform schedule does is make a ledger change at phase p split "read before p"
from "read after p" for exactly one fold instead of scattering the split across
lanes. Closing the gap needs, in order:

1. one shared commit phase for the managed fleet, so every validator samples the
   ledger at the same point in the epoch (shipped here in the validator worker,
   see below);
2. an epoch-pinned ledger served by Platform so every epoch-N commit is
   byte-identical, at the cost of up to one epoch of latency for a new score
   (follow-up);
3. ranking hysteresis for the crown and tail, a separate Platform question that
   stops costing vTrust once the snapshot is pinned.

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
- `ditto_pylon_epoch.py` ports drand 2.0.0's `should_run_epoch`,
  `current_epoch_pre_run_coinbase`, `simulate_run_coinbase`, and
  `predict_first_reveal_block` line for line, and derives Pylon's task window
  (`next_epoch_block`) from that same simulation. A two-rule shortcut of
  `min(LastEpochBlock + tempo, PendingEpochAt)` disagrees with the chain after a
  deferred pending epoch or the `BlocksSinceLastStep` safety net; the port and
  the Rust crate are checked against each other with real encryption in
  `test_image.py`.
- Pylon records the request's original epoch counter and expires retries when
  that counter changes, including after a restart or manual early epoch.
  A scheduled boundary that the chain defers does not expire the task before
  the epoch counter actually advances. Its weight-status endpoint uses the same
  stateful epoch window and includes the deferred boundary block. This expiry
  is stricter than Subtensor's own `is_commit_expired`; the validator therefore
  never creates a task within the last six blocks of an epoch.
- Upstream encryption, commit-reveal version 4, weight normalization, wallet
  handling, inclusion delay, and the three-block security offset are preserved.
  The patch does not copy consensus or change miner allocations or burn.

### Validator commit phase

Subtensor stores the *commit* block as `LastUpdate`, and the worker used to gate
its next commit on `LastUpdate + tempo + 1`. Inclusion delay makes each commit
land a block or two after the gate opened, so that phase precessed about two
blocks per epoch (UID 139: 299, 305, 314 over eight epochs; UIDs 72 and 92 sat
at 349 and 351). Once a phase reaches the boundary the commit slips into the
next epoch, the validator makes no commit for one epoch, and its stale vector
survives two folds. The stateful expiry above would additionally expire a task
created at phase 358 before it commits.

The worker now anchors on `LastEpochBlock`: it commits once per chain epoch at
`LastEpochBlock + 270` (of a 360-block tempo), never inside the last six blocks
of an epoch, and never below the chain's `WeightsSetRateLimit` (100 blocks on
SN118). The offset is a fleet constant in `ditto/validator/worker.py`, not an
operator setting: a per-host value would reintroduce exactly the ledger-read
skew the shared phase exists to remove. A late phase is deliberate: the
commit-to-boundary span is where wall-clock drift against `block_time=12`
accumulates, and a span above about 36 s of drift would pull a commit's pulse on
chain before the boundary and reveal it one fold early. When the anchor is
unreadable the worker keeps the previous `LastUpdate` cadence.

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
For each pending commit it derives `implied_reveal_block` from the drand round
and the commit block's timestamp, plus `implied_reveal_offset_blocks` relative
to the boundary that ends the commit's epoch; `next_epoch_block` comes from the
same simulation as the image. About `+3` is the stateful schedule, a large
negative offset is the legacy lane. This is the fleet-wide observable,
including for the validators that are not on our stack and will not move with
this release.

Release the signed Pylon stack through the normal updater, verify the exact
image digest across the fleet, and observe several Yuma boundaries and tail
changes. Existing ciphertext cannot be retimed by installing new code. Preserve
pending task/commit evidence and allow the old commitments to drain; do not
delete Pylon's database or wallet. After rollout confirm through the diagnostic
that every managed validator's `implied_reveal_offset_blocks` is `+3` (not `0`,
which would be the boundary-fold fast lane) and that their commit blocks share
one phase after `LastEpochBlock`. Expect dips to continue while the ledger read
is not epoch-deterministic; a dip whose split follows commit phase rather than
Pylon version is the ledger-sampling gap, not a scheduling regression. Local
tests and builds do not establish live fleet convergence or delegator return
recovery.
