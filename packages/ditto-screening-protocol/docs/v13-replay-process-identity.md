# V13 replay process identity (inactive contract)

`V13ReplayProcessProof` defines canonical bytes for a future, independent
replay worker's Ed25519 signature. It grants no replay authority by itself.
Node credentials and the ordinary screener hotkey are shared among local
workers, so a node bearer token plus a claimed `instance_id` cannot prove
which process made a replay request.

Before enabling replay capacity, Platform and the node-2 runner must complete
the following contract:

1. On the separate physical `subnet-screener-2` host, generate a dedicated
   Ed25519 key for `subnet-screener-2-worker-1`. Keep its private key readable
   only by that supervised worker; never reuse the node hotkey, node bearer,
   node-1 key, or a key from an untrusted build or smoke guest.
2. Register its public key through a guarded operator action while replay
   capacity is zero. Bind it to the exact enrolled node, hotkey, instance ID,
   provider resource, and a revision. A node bearer alone must not register or
   replace a replay-process key. Rotation drains active leases first.
3. Sign both the heartbeat and claim proof with that process key using the
   domain-separated canonical bytes in `v13_replay_process_identity.py`.
   Platform verifies the pinned public key, authenticated node, exact request
   path and body hash, a 30-second age bound, and a fresh nonce consumed once
   in durable storage. The heartbeat must record verified key identity, not
   merely a caller-supplied instance string.
4. At claim time, require that **the same registered process key** has a fresh
   signed V13 heartbeat from the minimum activated replay-runner release.
   A healthy sibling's heartbeat never qualifies a stale claimant. Missing,
   rotated, cross-node, replayed, or wrong-release proofs fail closed.
5. Only after the runner and verification paths are tested and released may
   Platform set `_MIN_VERIFICATION_REPLAY_RUNNER_RELEASE`. Keep node-2 replay
   capacity at zero until live Backroom shows its exact process/key, release,
   policy, heartbeat age, and independent resource ID. The first canary uses
   capacity one and produces report-only receipts, never an automatic CLEAR.

This is an activation checklist, not an instruction to provision the host or
change production settings. A production read must come from Backroom; merge
and release alone do not prove node adoption.
