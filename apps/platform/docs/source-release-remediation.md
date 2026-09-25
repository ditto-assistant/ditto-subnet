# Source disclosure remediation, 2026-09-17

Source disclosure now requires evidence that the exact submission earned a
completed winning tempo. Positive revealed weights, a brief off-chain crown,
score quorum, and a Pylon scheduling acknowledgement do not establish that.
The 48-hour embargo starts at the first verified paid-winning distribution.

## Containment and deployment state

The recorded live containment is Backroom source policy revision 6:
`disclosure=never`, `embargo_hours=48`. Keep disclosure paused until the new
provenance chain has been validated on the deployed fleet and live chain.
Merging the implementation or passing CI is not evidence that this validation
has happened. Pausing prevents new public URL grants; it cannot recall source
already downloaded or invalidate issued URLs before their TTL expires.

## Evidence required before disclosure

1. Platform serves an immutable ledger snapshot ID, epoch, digest and benchmark
   version. The validator binds its champion UUID and artifact SHA-256, the
   complete weight vector and its digest to that snapshot.
2. Pylon durably links that request to a task before acknowledging it. It records
   the normalized UID vector and ciphertext hash before transmission, then the
   finalized commit block/hash, extrinsic identity and matching successful
   commit event. Idempotent retries cannot turn an identical vector for
   submission A into evidence for submission B.
3. The validator signs the immutable finalized receipt and submits it to
   Platform. Pylon evidence is acknowledged only after Platform returns the
   exact receipt digest. Restart recovery uses the durable Pylon queue.
4. Platform verifies commit and successful reveal provenance on finalized chain
   state. A singleton pending commit and a unique successful initialization
   reveal establish which commitment became the vector. Ambiguous or unrelated
   weight-setting events invalidate the old binding even when vector values
   happen to be identical. `LastUpdate` is not used as submission provenance:
   it advances on commit and can refer to a newer pending vector.
5. At a successful distribution, more than two thirds of effective active,
   permitted validator stake must support the same exact submission. The
   comparison uses conservative bounds for the chain's quantized stake values;
   unknown or conflicting provenance cannot count as supporting stake. The
   miner must actually receive positive incentive equal to the greatest miner
   payout that tempo. Exact payout ties qualify; a positive tail allocation
   alone does not. Owner and owner-associated recycled/burned incentives do
   not count as paid miner earnings.
6. Every public source path uses the verified emission timestamp plus the
   current 48-hour policy delay. Existing score-quorum and eligible-status
   checks still apply. Legacy `weight_confirmed_at` rows are never converted
   into earnings proof.

The collector is forward-only from its initial finalized cursor: historical
crowns or old signed folds are not retroactively granted eligibility. It stores
payout snapshots and retries their resolution when exact receipts arrive later,
so a delayed receipt is matched to the consumed vector at that payout rather
than today's crown. Collection can run while disclosure remains paused.
`DITTO_SOURCE_EMISSION_CONFIRMATION_ENABLED=false` separately prevents new
confirmations while preserving the evidence collection path.

## Runtime and failure boundaries

The chain reader accepts only explicitly audited WASM code hashes. The current
allowlist covers the reviewed v459/v464 compressed and compact artifacts; the
release artifacts' hashes were checked independently of their source commit.
An unknown runtime, runtime transition, incomplete or pruned chain read,
ambiguous reveal, missing receipt, or insufficient attributable stake withholds
eligibility. An epoch counter advance by itself is insufficient because skipped
payouts also advance that counter.

The runtime semantics are documented in
[RaoFoundation/subtensor](https://github.com/RaoFoundation/subtensor/tree/7c732d8d3eb7f8d736bf64d5809c48cbf2e4f028):
`subnets/weights.rs`, `coinbase/reveal_commits.rs`, `coinbase/block_step.rs`, and
`epoch/run_epoch.rs`. Source inspection alone does not prove the live runtime
matches; the observed chain code hash must match the audited allowlist.

Old Pylon versions can continue ordinary weights without creating source-release
proof. An explicit unsupported response or rejection before task creation permits
that fallback. A timeout or lost acknowledgement is ambiguous and must retry the
same deterministic receipt request rather than submit a duplicate legacy job.
Prepared-but-unconfirmed transmissions remain unusable evidence; a fresh epoch
may submit a new request, so old uncertainty does not permanently stop weights.

## Rollout, validation and rollback

Deploy the Platform schema/ingestion and collector, the pinned receipt-capable
Pylon image, and the validator relay. Confirm the actual served commits and
fleet image digests, then observe receipt ingestion, exact successful reveal
binding, a qualifying finalized payout, and the resulting embargo timestamp
through Backroom. Verify that A/B submissions with identical vectors remain
separate and that incomplete evidence remains private before restoring public
disclosure. Collection or confirmation being enabled does not itself restore
public disclosure.

Backroom must report release gate `completed-winner-emission-v2`, an advancing
finalized cursor and receipt/payout coverage from the deployed collector. An
older gate version or missing coverage fields means this path is not yet served.

Local tests cover pinned-source refusal, real isolated Pylon SQLite durability,
identity guards, installed TurboBT finalization capture, shared receipt digest
agreement, worker restart/unknown-outcome handling, chain verification and
collector/public-path behavior. These are implementation evidence, not proof
that live commits have revealed, miners have been paid, or disclosure can safely
be resumed.

Before any code rollback, set the Backroom source policy to `disclosure=never`
and verify readback. Older code can trust legacy weight confirmations, so
reverting code while disclosure is public can reopen source prematurely. Retain
receipt databases and collector evidence; do not delete them to clear a failure.

Pylon database rollback is separate from the source-policy rollback. Stop the new
Pylon process and back up its database, then use the new image to downgrade the
Alembic version marker to `daab35a40458` before launching an older image. The new
receipt migration deliberately retains its evidence table and linked task rows;
re-upgrade recognizes that retained table. An old image cannot resolve the new
revision identifier on its own. See the Pylon README for this reviewed procedure.
