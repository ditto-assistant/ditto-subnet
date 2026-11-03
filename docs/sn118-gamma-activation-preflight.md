# Gamma proposed-policy preflight

`get_treasury_activation_preflight` is a read-only public Backroom tool available
with `backroom:read`. It inspects an exact signed public proposal before that
proposal is configured in Platform. It does not record settings, start the
observer, pin an epoch, set weights, sign a transfer, or enable a timer.

Inputs:

- `approvalJson`: JSON containing the public `TreasuryPolicyApproval` (`policy`
  and its coldkey `signature`), at most 8192 characters. Never provide secrets.
- `expectedPolicyDigest`: the exact approved emission policy digest.
- `expectedCollectorPolicyDigest`: the exact approved collector policy digest.

Platform verifies the signature and both digests before any chain read. It then
reads the finalized collector identity and the full chain-permitted validator
roster at the same block hash. The response binds that observation, its epoch,
and the check time to the requested policy. Invalid signatures refuse; chain
failures return a fixed blocker without exposing raw provider errors.

Every chain-permitted setter appears, including a setter with no heartbeat.
Required stale rows remain visible. All fresh extra authenticated reporters
also participate, matching the existing enforcing gate without a scorer filter.
Each row reports its signed capability and one status: ready, missing heartbeat,
outside the freshness window, invalid heartbeat, missing guard, unsupported
protocol, policy mismatch, or inventory not checked (a bounded query cannot
claim that an unread heartbeat is missing). The freshness window is the existing 15 minutes;
future heartbeats refuse. At most 512 rows are returned. Truncation, unavailable
chain evidence, or any unready row prevents proposed-fleet readiness.

`configured_policy_matches` requires both the configured proposal and approved
emission digest to match. `configured_collector_matches` compares the configured
collector digest. These describe configuration only; they cannot authorize an
epoch. `fleet_ready_for_proposed_policy` is prospective read evidence, while
`can_enforce_weights` and `copy_behavior_verified` are always false. Similar
historical weight vectors are not a substitute for exact-policy runtime proof.

After this read, rollout still needs the guarded production configuration path,
complete supported fleet, an independently bound immutable enforcing epoch,
and actual finalized weight/emission evidence. The collector transfer ceiling
is a separate journal guard. A source bucket exceeding the one-transfer ceiling
must remain held unless an independently reviewed partial-distribution contract
exists; this preflight cannot split or raise that amount.
