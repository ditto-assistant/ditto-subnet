# Untrusted execution containment

This runbook covers the SN118 screener and validator execution path. It contains
no miner source, credentials, or runnable exploit payloads.

## Required production invariants

- Validators load a screener-built image whose archive digest, image ID,
  source artifact, screening attempt, and lease are bound by the platform.
- Benchmarks v7 and v8 accept only a platform-verified screener image; validators
  never build miner source.
- Miner containers run non-root with a read-only root filesystem, ephemeral
  no-exec scratch, all capabilities dropped, no-new-privileges, resource and
  time limits, and request-scoped cleanup.
- The `ditto-sandbox` bridge denies forwarding by default. Only the
  source-bound ticket broker is admitted; chat and embedding provider access
  remains platform-owned. Denials are logged with the `ditto-sandbox-deny`
  prefix.
- No wallet, `.env`, cloud credential, Docker control socket, or host directory
  is mounted into a miner container. Host network, PID, IPC, and other namespaces
  are not shared or joined with miner containers.

## Executor boundary and residual risk

The compatibility executor is a privileged outer container running a rootful
nested Docker daemon. This deliberately restores the v0.41 deployment contract
while rootless DinD, reviewed AppArmor/seccomp profiles, and portable aggregate
cgroup defaults are redesigned behind an explicit migration gate. The
validator host socket is never mounted, and miner containers still run non-root
with the per-run restrictions above.

The nested daemon is reachable only inside the executor namespace. Its
`ditto-sandbox` bridge permits the source-bound ticket broker on port 11436 and
rejects all other miner egress. Operators must dedicate the validator host to
this workload and treat the privileged executor as a residual risk until the
deferred isolation work lands without breaking existing managed validators.

The scorer may hold the nested executor's socket because it is trusted
control-plane code. The socket must never be mounted into, proxied to, or made
network-reachable from a miner container. A host-root Docker socket must never
be exposed to a miner container. Backroom must route work from the signed
capability response rather than merely because the installed scorer binary
contains a dataset contract.

Heartbeat protocol 18 reports screened-image mode, executor isolation, and the
four component identities. This is signed routing and observability data. It is
not remote attestation: a compromised host can still lie using its validator
wallet, so the platform must not treat a heartbeat as proof of host integrity.

## Pre-activation checks

1. Confirm the deployed screener version contains static malicious-source
   preflight and that its canary quarantines before any Docker build event.
2. Confirm the platform assigns v7/v8 only to validators whose signed capability
   heartbeat advertises screened-image support and a freshly verified scorer.
3. Record that the temporary compatibility executor is privileged rootful DinD
   and confirm the host is dedicated to validator work.
4. Confirm each miner run receives the scorer's memory, CPU, PID, filesystem,
   capability, and deadline limits.
5. Verify the executor account cannot read validator wallet paths, service
   `.env` files, SSH/cloud configuration, or other users' homes.
6. Run the inert canary suite. It must show Docker control absent, host-root and
   credential paths unreadable, host writes impossible, metadata blocked, and
   outbound connections denied except the ticket broker.
7. Confirm `ditto-sandbox-deny` events reach the operator log/alert sink without
   including request bodies, credentials, or private source.
8. Confirm the required `dittobench-api` change is merged and the deployed
   scorer checksum identifies its actual post-merge commit in `main` history.
9. Confirm `DITTOBENCH_ALLOW_SCREENED_IMAGES=1` is enabled while the broader
   `DITTOBENCH_ALLOW_PRIVATE_HARNESS` bypass remains disabled.

## Emergency containment

If a malicious-source quarantine or runtime deny alert fires:

1. Drain validator lease claims and stop only the dedicated executor boundary.
2. Preserve the attempt ID, artifact SHA-256, image digest/ID, timestamps,
   sanitized category finding, executor logs, and network-deny counters.
3. Do not release, reject, rescreen, rotate credentials, or delete evidence
   until an authorized operator approves the action.
4. Check whether a build event, container start, metadata attempt, Docker API
   attempt, host-path denial, or outbound denial occurred. Absence of a score is
   not by itself proof that screener-side build code never ran.
5. If the dedicated boundary invariant was broken, treat potentially reachable
   credentials as exposed and escalate for an approved rotation plan. Do not
   perform rotation from this runbook automatically.
6. Restore service only from a reviewed immutable executor image/config and
   rerun the inert canaries before undraining.

## Rollback

Rollback is code/config rollback only. Use the updater's cooperative drain,
whole-stack health checks, and fresh platform-accepted heartbeat before
resuming. A fallback-capable prior release may be restored during the staged
rollout; its heartbeat must accurately report that capability and executor
boundary so the platform routes compatible work.
