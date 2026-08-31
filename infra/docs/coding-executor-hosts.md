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

Only after all checks does it atomically write the root-owned `0600`
`runtime-image-attestation.json` beside the staged inputs. A bundle that does
not restore the exact repository digest fails closed; no fallback pull, local
tag, registry credential, scorer, client-group member, or candidate process is
introduced. The future scorer-client role must consume and revalidate this
attestation, not treat mere image presence as authority.

Destroying a created cohort is intentionally not a routine rollback: the shared
compute module has deletion protection. Rollback during the shadow phase means
leave the hosts present and disable the later daemon/worker configuration; any
infrastructure teardown requires its own reviewed protected plan.
