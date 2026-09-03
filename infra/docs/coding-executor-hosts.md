# Dedicated shadow-coding executor hosts

The optional GCP coding-executor cohort is the physical boundary for a future
shadow coding canary. It is absent by default:

```hcl
coding_executor_host_count = 0
```

The only nonzero value is `3`, matching the future k=3 validator quorum. A
protected Terraform apply may create three private Debian hosts in their own
subnet. They have no public IP, use secure boot/vTPM/integrity monitoring, and
receive a runtime service account with logging and metric permissions only.
That identity has no Secret Manager, Platform, provider, storage, Artifact
Registry, or impersonation permission. IAP/OS Login is scoped to the explicit
executor-operator set and exact VM instances.

The VM-level firewall blocks egress to the Platform and production-validator
subnets. Future rootless candidate execution requires a second, narrower
candidate egress/proxy policy; it is deliberately not implied by this host
foundation.

This Terraform layer does not install Docker, create a rootless daemon, mount a
socket, preload an image, place a wallet, read a secret, start DittoBench, run a
validator, issue a ticket, or enable a coding gate. A later Ansible role must
install the dedicated rootless daemon under an empty local identity, prove its
isolated-daemon label and socket ownership, then keep the worker disabled until
the full Platform/validator/scorer canary is explicitly approved.

## Rootless daemon role

`infra/ansible/playbooks/gcp-coding-executor.yml` targets only hosts labelled
`role=coding_executor`. Its `coding_executor_daemon_enabled` default is false,
so a normal converge is a no-op. When a protected host configuration explicitly
sets it true, the role installs a rootless Docker daemon under the empty
`ditto-coding-executor` local identity. It creates a socket owned by the empty
future-client group `ditto-coding-client`, labels the daemon
`io.heyditto.dittobench.isolated=true`, enforces bounded logs/GC, and blocks
that daemon identity from private and metadata egress except DNS.

The role intentionally installs neither a client service nor a candidate image.
No user belongs to `ditto-coding-client` at this stage, so the rootless socket
has no trusted scorer or worker consumer. A future slice must add the dedicated
scorer process, join only that process to the client group, prove the complete
candidate proxy policy, and retain every coding feature gate false until a
reviewed canary activation.

## Production runtime-bundle staging

The public `Dockerfile.coding-supervisor` builds a synthetic certification
fixture. Its fixture label is rejected by normal executor preflight, so it is
never a production runtime input. A repository-specific supervisor and trusted
test driver remain a private-artifact deliverable; this repository does not
invent a production image digest or driver identity.

After that artifact has an approved immutable manifest, an operator may use the
separate `coding_executor_runtime_bundle_enabled=true` control. It stays false
by default and requires a root-owned `0600` (or stricter) manifest and OCI
archive at the fixed staging paths below, plus the complete raw manifest
SHA-256 in protected host configuration:

```text
/var/lib/ditto-coding-executor/staged/runtime-manifest.json
/var/lib/ditto-coding-executor/staged/supervisor.oci.tar
```

The exact v1 manifest field set is `schema`, `source_revision`, `platform`,
`supervisor_contract`, `image_repository`, `image_digest`,
`trusted_test_driver_digest`, `archive_sha256`, and `fixture`. It fixes the
schema to `dittobench-coding-runtime-manifest-v1`, platform to `linux/amd64`,
supervisor contract to `1`, and `fixture` to JSON `false`; the source revision
is a full lowercase Git SHA and every artifact field is a lowercase SHA-256
pin. Unknown or duplicate fields are rejected rather than becoming future
authority by accident.

The verifier rejects symlinks, non-root-readable files, mutable/missing pins,
fixture declarations, non-`linux/amd64` manifests, incorrect supervisor
contract, archive drift, and malformed or duplicate-key JSON. The raw manifest
hash is the root of trust for this staging layer; it binds the source revision,
exact image and trusted-test-driver digests, and archive SHA-256. The host does
not receive Artifact Registry, Secret Manager, provider, Platform, wallet, or
validator access to fetch the bundle itself.

An operator transfers the manifest/archive pair through the protected IAP or
release path, then runs the root-only `stage-runtime-bundle.sh` helper with
absolute source paths and the approved manifest SHA-256. The helper copies to a
root-owned temporary directory, verifies the copied pair before it is
published to the fixed paths, and never contacts a registry or Docker. It does
not accept a caller-selected destination or manifest bytes through Ansible.

Bundle verification alone does **not** load the archive into Docker, inspect an
image, install a scorer, add a socket consumer, or start any candidate process.
The separate optional load control below must prove the loaded image's exact
digest, platform, labels—including rejection of the certification fixture—and
driver identity before the dedicated scorer client can receive socket access.
All coding gates remain false through these staging steps.

## Runtime-image load attestation

`coding_executor_runtime_image_load_enabled` is a second default-off control.
It may be true only when the runtime-bundle control is also true. The loader
re-verifies the staged files, contacts only the fixed rootless Unix socket, and
checks the archive has only regular tar members with at most 16 GiB of unpacked
content before `docker image load`. It does not run or create a container. It
then requires exactly the manifest's `image_repository@image_digest`, an image
ID, `linux/amd64`, no volumes or credential-shaped environment, the fixed
supervisor entrypoint, and these labels:

- `io.heyditto.dittobench.coding-supervisor-contract=1`
- no `io.heyditto.dittobench.coding-supervisor-fixture=true`
- `io.heyditto.dittobench.trusted-test-driver-sha256` matching the manifest
- `io.heyditto.dittobench.trusted-test-driver-name=dittobench-test-driver`
- `org.opencontainers.image.revision` matching the manifest source revision

Only after all checks does it atomically write the root-owned `0640`
`runtime-image-attestation.json` beside the staged inputs. Its only non-root
reader is the otherwise-empty `ditto-coding-client` group. A bundle that does
not restore the exact repository digest fails closed; no fallback pull, local
tag, registry credential, task-serving scorer, or candidate process is
introduced. The client guard below consumes and revalidates this attestation;
future scorer code must do the same rather than treating image presence as
authority.

## Attestation-bound client guard

`coding_executor_client_guard_enabled` is a third default-off control. It may
be true only after runtime-image loading is enabled and has written the fixed
attestation. It creates `ditto-coding-scorer` as the **only** member of
`ditto-coding-client`, then runs a hardened systemd guard with no capabilities,
no network address family, no listener, and no writable state.

The guard can issue only Docker `info` and `image inspect` calls against the
fixed rootless socket. On every check it proves the daemon label/rootless state
and compares image ID, repository digest, platform, entrypoint, labels,
volumes, and environment with the root-owned attestation. It cannot pull, load,
create, start, run, remove, publish, or expose an image. A mismatch exits the
guard; it does not attempt remediation or candidate execution.

This is not yet a task-serving scorer: it receives no validator or Platform
request path, wallet, Platform/provider/registry credential, secret, ticket,
or network listener. The next functional scorer slice needs its own immutable
release artifact and a separately reviewed paired mTLS validator-to-executor
transport.

## Dedicated scorer artifact

`Dockerfile` now has a separately built `coding-executor-scorer` target. Its
`dittobench-coding-executor-scorer` binary is provenance-stamped and stays
disabled until a future deployment profile supplies the fixed rootless Docker
socket, locked policy, root-owned control-token file, private state root, and
attested runtime image identity. Only then does it compose the reviewed coding
host behind its fixed Unix socket, exposing constant health plus the existing
supervisor/publication handlers. It has no TCP listener, canary route, ordinary
scorer route, broker, ticket, wallet, secret in the artifact, or
provider/Platform authority. CI builds this artifact without publishing or
deploying it. A later signed release/staging slice must add the real scorer
configuration and another later paired mTLS slice may connect a validator.

The release boundary is frozen by
`scripts/render-coding-executor-scorer-manifest.py`. It emits only the exact
image repository/digest, source revision, `linux/amd64` platform, scorer
contract, and locked-policy digest. It rejects a tag or any malformed digest;
a later protected release job must sign both the exact image and this canonical
manifest before an operator can transfer an OCI archive through IAP.

`scripts/export-coding-executor-scorer-bundle.sh` is the protected-operator
export step. It verifies the release workflow's Cosign identity, pulls only the
digest reference, refuses output overwrite, saves an OCI archive, and renders a
bundle manifest binding both archive and signed release manifests. It does not
contact an executor host or grant it registry access.

Destroying a created cohort is intentionally not a routine rollback: the shared
compute module has deletion protection. Rollback during the shadow phase means
leave the hosts present and disable the later daemon/worker configuration; any
infrastructure teardown requires its own reviewed protected plan.
