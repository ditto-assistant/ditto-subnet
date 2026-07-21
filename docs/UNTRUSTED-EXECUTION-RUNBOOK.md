# Untrusted execution containment

This runbook covers the SN118 screener and validator execution path. It contains
no miner source, credentials, or runnable exploit payloads.

## Required production invariants

- Validators load a screener-built image whose archive digest, image ID,
  source artifact, screening attempt, and lease are bound by the platform.
- Benchmark v2 preserves source-build fallback for legacy records and mixed
  validator versions. Benchmark v3 is issued only after policy-9 screening and
  a platform-verified image; this is a versioned contract, not a fleet-wide
  environment switch.
- Miner containers run non-root with a read-only root filesystem, ephemeral
  no-exec scratch, all capabilities dropped, no-new-privileges, resource and
  time limits, and request-scoped cleanup.
- The `ditto-sandbox` bridge denies forwarding by default. Only local
  embeddings on 11434 and the source-bound ticket broker on 11436 are admitted;
  the deprecated process-wide relay on 11435 is denied. Denials are logged with
  the `ditto-sandbox-deny` prefix.
- No wallet, `.env`, cloud credential, Docker control socket, or host directory
  is mounted into a miner container. Host network, PID, IPC, and other namespaces
  are not shared or joined with miner containers.

## Residual risk and target executor boundary

The current `sandbox-docker` service is a privileged rootful Docker-in-Docker
container. It avoids mounting the validator host socket, but privileged mode is
still host-sensitive and does not meet the target production boundary. The
screened-image rollout removes most validator-side builds and restricts egress,
but does not remove this pre-existing executor risk. Track and migrate to one of:

1. a dedicated rootless Docker daemon owned by a locked-down OS account with no
   wallet, repository, cloud, or operator-home access; or
2. an ephemeral VM/microVM executor with an independently restricted service
   identity and network policy.

The scorer may hold that dedicated executor's socket because it is trusted
control-plane code. The socket must never be mounted into, proxied to, or made
network-reachable from a miner container. A host-root Docker socket must never
be exposed to a miner container. Privileged DinD is an explicitly reported
interim boundary, not the target architecture.

Heartbeat protocol v7 reports screened-image mode, executor isolation, and the
six component identities. This is signed routing and observability data. It is
not remote attestation: a compromised host can still lie using its validator
wallet, so the platform must not treat a heartbeat as proof of host integrity.

## Pre-activation checks

1. Confirm the deployed screener version contains static malicious-source
   preflight and that its canary quarantines before any Docker build event.
2. Confirm the platform assigns v3 only to validators whose signed v8 capability
   heartbeat advertises screened-image support and a freshly verified v3 scorer.
   Source-capable validators may continue receiving v2 records.
3. Inspect and record the executor daemon security options. Enable the reviewed
   seccomp and AppArmor profiles where the host supports them.
4. Verify the executor account cannot read validator wallet paths, service
   `.env` files, SSH/cloud configuration, or other users' homes.
5. Run the inert canary suite. It must show Docker control absent, host-root and
   credential paths unreadable, host writes impossible, metadata blocked, and
   outbound connections denied except the two inference relays.
6. Confirm `ditto-sandbox-deny` events reach the operator log/alert sink without
   including request bodies, credentials, or private source.
7. Confirm the required `dittobench-api` change is merged and the deployed
   scorer checksum identifies its actual post-merge commit in `main` history.
8. Confirm `DITTOBENCH_ALLOW_SCREENED_IMAGES=1` is enabled while the broader
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
