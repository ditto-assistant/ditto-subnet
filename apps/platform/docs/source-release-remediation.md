# Source disclosure remediation, 2026-09-17

Live containment: Backroom source policy revision 6 sets disclosure=never while retaining embargo_hours=48. Readback verified. This prevents new public URL grants; it cannot recall already downloaded source or invalidate previously issued URLs before their TTL expires.

The code patch replaces weight_confirmed_at with emission_confirmed_at on every new disclosure path. Legacy weights are not backfilled into earnings. The 48-hour clock is anchored to a verified payout, not crown time, score quorum, or weight observation.

No automatic confirmer is installed in this patch. This is deliberate and visible as automatic_confirmation_enabled=false in Backroom. Chain evidence can prove a finalized successful miner-incentive distribution to a hotkey, but current signed folds and Pylon acknowledgments cannot bind identical weight vectors to distinct submissions. LastUpdate is updated on commit, and can describe a newer pending vector while Weights still describes an older revealed vector. Choosing the newest signed fold before LastUpdate is unsafe; that proposed collector was removed before publication.

Primary runtime reference: opentensor/subtensor commit 7c732d8d3eb7f8d736bf64d5809c48cbf2e4f028, pallets/subtensor/src/subnets/weights.rs (commit updates LastUpdate; revealed weights do not), and coinbase/reveal_commits.rs (successful event has subnet and hotkey, not commit identity). Epoch counters also advance for skipped payouts; LastMechansimStepBlock plus the finalized miner-emission event is needed for completed hotkey earnings. Owner and owner-associated recycled incentives are excluded.

Remaining automatic publication work requires a Pylon task/fold reference, persisted normalized weights and ciphertext hash, finalized commit identity, a signed validator receipt, and verified successful reveal provenance before the completed payout can be attributed. The pinned Pylon/TurboBT adapter currently discards this information and requires an API/fleet rollout. Until that exists, keep disclosure paused. Do not substitute timestamp or hotkey guesses.
