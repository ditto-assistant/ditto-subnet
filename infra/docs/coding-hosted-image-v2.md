# Native-v2 public executor image import

The default-off `coding_hosted_image` role loads one independently approved
public executor image into the existing native daemon. It does not run a
container, create a worker, pull from a registry, touch private data, or change
catalog/scoring/weight gates. Public software OCI archives are not private
Coding inputs or evidence; those remain Hippius-only.

## Build and review

Use an isolated clean checkout of the exact reviewed integrated revision. The
importer supports Python `python-call-ast-v1`/`python-call-ast-v2`, Node
`node-call-ast-v2`, Go `go-call-ast-v1`, and Rust `rust-call-ast-v1`. The
profile is read from the verified image config, committed into the approval,
and checked again against the loaded image. Each image still requires its own
independently pinned approval SHA; an approval for v1 cannot authorize v2.
Each profile has a closed environment and working-directory policy. Go alone
adds `/usr/local/go/bin` to PATH; Go/Rust require `/workspace`, while Python/Node
retain their original root working directory. Profiles cannot borrow one another's
defaults or image approvals. Import still reports qualification required and
private execution not ready for every language.

```bash
revision="$(git rev-parse HEAD)"
docker buildx build --platform linux/amd64 --provenance=false \
  -f services/dittobench-api/Dockerfile.coding-python --target runtime \
  --build-arg DITTOBENCH_SOURCE_SHA="$revision" \
  --output type=oci,dest=source.oci.tar .
python3 infra/ansible/roles/coding_hosted_image/files/image-bundle.py prepare \
  --source source.oci.tar --archive runtime.oci.tar --approval approval.json \
  --repository coding-runtime.invalid/python/runtime --revision "$revision"
sha256sum approval.json runtime.oci.tar
```

The repository is a digest-qualified local name, not a registry to contact. The
single image manifest, config and layer blobs remain byte-identical; preparation
normalizes only outer transport metadata, including the containerd import name.
Tags, Docker-save archives, multi-platform indexes, provenance attestations,
external layers, links, extensions and unreferenced blobs are rejected. Build
attestations may be retained separately, not included in this single-image bundle.

Approval binds the outer archive SHA, OCI manifest digest, config digest, exact
40-character source revision, driver profile and permanent shadow/weight fields.
The tool checks every blob hash and descriptor size and streams decompression to
verify layer diff IDs with a 16 GiB combined expansion limit. The outer archive
is limited to 8 GiB, 512 entries and 1 MiB per JSON object. Layers are not executed
or extracted into the verifier's filesystem.

Have a custodian independently review the build source, image, approval contents
and approval-file SHA. A matching source label alone is not build provenance; a
generated manifest is not a signature or automatic operational approval. Supply
the reviewed approval SHA independently during apply, never derive it from a
freshly downloaded file in the deployment command.

## Import on the existing native host

First-provisioned native daemons explicitly enable the containerd image store.
Existing daemon backends must not be switched in place by this role: changing
stores can hide existing images and containers. See [Docker's containerd image
store documentation](https://docs.docker.com/engine/storage/containerd/).

The importer requires the dedicated non-root `ditto-coding-hosted` identity,
private `0600` socket under `/run/ditto-coding-hosted`, empty `0700` Docker client
directory, isolated rootless daemon, native data root, delegated systemd cgroup
v2 limits, containerd storage and no containers. Import is serialized by a local
lock; schedule it before workers and do not start workloads concurrently. The
socket owner remains the trusted host principal, not an untrusted candidate.

Set these variables only for a reviewed one-shot apply of
`infra/ansible/playbooks/gcp-coding-hosted-image.yml`:

```yaml
coding_hosted_image_import_enabled: true
coding_hosted_image_archive: /absolute/controller/path/runtime.oci.tar
coding_hosted_image_approval: /absolute/controller/path/approval.json
coding_hosted_image_approval_sha256: REVIEWED_64_HEX_SHA256
```

The separate playbook does not re-run first-provisioning or restart services.
It stages public files under root-owned
`/opt/ditto-coding-hosted-images/<approval-sha>/`, then invokes the importer as
the daemon user. A still-open verified archive is supplied to Docker on stdin.
No ambient Docker context, credential configuration or proxy environment is
inherited. After loading, the exact `RepoDigests` entry, manifest descriptor,
image ID, platform and supervisor configuration must match. Containerd-backed
Docker versions may expose either a manifest or config digest as image ID;
neither replaces the required repository digest lookup.

A root-owned `0600` `import-receipt.json` is written only after post-import
checks. It explicitly reports `qualification_required=true` and
`private_execution_ready=false`. No private-data authority is granted.

## Failure and qualification boundary

This role intentionally refuses existing staging directories. A failed or
interrupted import retains public staging and possibly Docker blobs; there is no
automatic retry, image prune, policy relaxation or cleanup. Review the exact
approval directory and digest before separately authorizing recovery. If a
receipt is missing, do not assume import failed before mutation. Re-running the
native importer manually after review may repeat an exact digest load; the
Ansible role itself will not overwrite retained state.

CI builds the exact checked-out source and verifies digest-preserving offline
import on a disposable containerd engine. It performs no container execution.
This proves the software transport and image metadata, not the native rootless
host. Host resource/network qualification, private lifecycle/evidence recovery,
other language runtimes and a shadow canary still need their own evidence.

Local regressions:

```bash
uv run --with pyyaml pytest -q ditto/tests/test_coding_hosted_image_import.py
# Optional: imports uniquely named tiny public images into the selected local
# Docker engine, without executing or removing anything. Not a host attestation.
CODING_OCI_IMPORT_TEST=1 uv run --with pyyaml pytest -q \
  ditto/tests/test_coding_hosted_image_import.py
```
