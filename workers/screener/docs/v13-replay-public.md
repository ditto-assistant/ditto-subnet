# Independent V13 public replay runner

`ditto-screener-replay` is an opt-in, report-only process for the replay lease
API. It runs the existing rootless local build gate under a separate node and
records only the seven public observations defined by the replay transport.
`reported` means those observations were recorded; it does not mean the
submission passed V13 or that its quarantine may be cleared.

The process refuses to start unless `SCREENER_REPLAY_WORKER_ENABLED=1`,
`SCREENER_NODE_ID=subnet-screener-2`,
`SCREENER_INSTANCE_ID=subnet-screener-2-worker-1`, rootless Docker is required,
remote build is off, `SCREENER_V13_RUNTIME_RECEIPTS_MODE=shadow`, and
`SCREENER_REPLAY_PROCESS_KEY_FILE` names a private regular file containing a
raw 32-byte Ed25519 seed. The host must also supply an enrolled node credential,
the matching screener hotkey signer, and an attested release version. The
process signs its heartbeat and every replay lease request with the dedicated
process key. No private seed is sent to Platform.

The runner rebuilds a replay with no pre-existing verified image. It verifies
the uploaded image bytes through Platform before recording runtime observations.
For an existing verified image, it downloads the exact Platform-pinned tar,
checks its byte size and SHA-256, checks the portable tar config against the
pinned Docker image ID, then loads that image into the isolated rootless Docker
executor. It never republishes the existing image. Receipts retain its original
image upload ID. It fails the lease when any public observation is missing.
The benchmark version in replay inputs is resolved
from the submission's arrival era when inputs are fetched; a durable
source-attempt benchmark pin remains future work.

This path does not produce the other mandatory V13 checks, the private
metamorphic profile, or a terminal adjudication. The Platform runner release
minimum remains unset and replay capacity remains zero. Activation requires
those separate review and release steps; do not run held-submission waves from
these public receipts.
