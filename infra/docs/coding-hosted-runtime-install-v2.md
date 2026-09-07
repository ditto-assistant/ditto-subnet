# Pinned native host runtime bundle

This installs public Platform software and its native Go worker, not a service,
private dataset, credential, executor image or evaluation. The
`coding_hosted_runtime` role defaults off and never starts/enables a worker,
changes an active-version pointer, downloads packages or removes old versions.

## Build and provenance

From a clean tracked checkout, build one exact commit into a new output directory
outside the repository:

```text
python3 infra/scripts/build-coding-hosted-runtime.py \
  --revision <40-character-reviewed-commit> --output <new-absolute-output-directory>
```

The wrapper checks the commit and feeds `git archive` to BuildKit. It does not
send the working directory, untracked files, `.env`, Git credentials, provider
keys or private corpus to the build. Go, Python base and uv images are digest
pinned. Go uses the in-tree generator and verified module sums; Platform uses
`uv sync --frozen --no-dev`, the committed lock and shared package at the same
monorepo revision. Wheel files are copied, not hard-linked to builder caches.

The environment is built at its final
`/opt/ditto-coding-hosted/<revision>/apps/platform` prefix, preserving editable
source references. The archive contains Platform source, its environment and
lock, the shared protocol source, and a static linux/amd64 Go worker. Python
points to `/usr/bin/python3.13`; the manifest pins that interpreter hash and
the Debian Python/glibc package versions. The target must be Debian 13 amd64
with that baseline already installed. The installer will not update it for you.

Runtime dependency versions and resulting artifact bytes are pinned. Debian
package and build-backend resolution happens during the public software build;
this is not a claim of a fully hermetic/reproducible toolchain or OS attestation.
Review the exact CI provenance, source, base packages and artifact bytes before
approval. A manifest or local installation receipt is not a signature.

The workflow builds and tests the exact PR/requested SHA without production
credentials, then uploads a short-lived public-software artifact. It does not
publish a release, deploy a host, register a private release or enable scoring.
Hippius remains the sole store for private Coding inputs and sealed evidence;
this public software artifact contains neither.

## Offline installation

Stage the independently approved `runtime.tar` in a protected root-owned
directory, outside Git and outside world-writable staging. Supply its expected
SHA-256 and source revision from the reviewed provenance, not merely a sidecar
obtained from an untrusted archive sender. The role inputs are:

- `coding_hosted_runtime_enabled: true` after separate approval;
- `coding_hosted_runtime_revision`: exact lowercase 40-character source commit;
- `coding_hosted_runtime_archive`: absolute protected local archive path;
- `coding_hosted_runtime_sha256`: independently approved 64-character digest.

The role validates the dedicated host, installs the public verifier and invokes
it through `gcp-coding-hosted-runtime.yml`. A direct installation requires the
same checks and explicit confirmation:

```text
/usr/bin/python3.13 -I /opt/ditto-coding-hosted/runtime-bundle.py install \
  --revision <commit> --archive <protected-runtime.tar> --sha256 <approved-digest> \
  --confirm 'INSTALL VERIFIED CODING RUNTIME'
```

`inspect` validates an archive read-only without requiring host compatibility or
loading credentials. `verify` checks the installed version, original archive,
Python baseline, every file digest/mode, symlink targets and receipt without
starting anything. Retain the exact archive for verification; no network fetch
or dependency resolution occurs in these commands.

All archive bytes and entries are validated before installation. The parser
accepts bounded uncompressed USTAR regular files/symlinks only, with canonical
paths, a closed manifest, explicit file modes and bounded sizes/counts. It
rejects traversal, duplicate members, hard links, special devices, extension
headers, undeclared files, corruption and external symlinks other than the pinned
system interpreter. Installation reserves a new prefix exclusively, rechecks
copied bytes, fsyncs files/directories and seals the root-owned tree read-only.
It refuses to overwrite an existing version. A partial prefix is retained and
must be reconciled explicitly; no recursive deletion, blind repair or downgrade
is attempted. Existing installations are verified, not assumed valid.

## Runtime handoff

The approved paths are:

- Platform interpreter and Go start-helper interpreter:
  `/opt/ditto-coding-hosted/<revision>/apps/platform/.venv/bin/python`.
- Native worker:
  `/opt/ditto-coding-hosted/<revision>/bin/dittobench-coding-hosted-worker`.

Startup retains its existing owner-only private-helper rules. A separate narrow
path admits only that exact root-owned Go worker layout: root-only write
authority throughout the parent chain, sealed modes, canonical root receipt,
matching source revision, linux/amd64 ELF header and a fresh hash matching the
receipt's worker digest. Arbitrary root-owned executables do not gain admission.
Private unwrap helpers keep their existing private-owner checks.

The build smoke test installs into a fresh Debian filesystem, verifies the whole
tree, imports the real Platform runtime under an unprivileged UID, checks the
installed-worker admission path, and executes the Go binary's no-argument refusal.
It then corrupts the installed worker copy and requires admission to reject it;
the exported archive remains unchanged. No private flag/configuration, Docker
daemon, provider call, database, key or evaluation is used by these probes.

Changing a configured runtime remains an explicit stopped/drained-worker
operation. Reverify the bundle and host baseline after maintenance and before
approving an invocation. This installer does not establish immutable hardware
attestation, base-OS integrity against a trusted administrator, or live private
runtime readiness.

Still required: offline import and qualification of the separate supervisor/test
driver images, actual daemon/network/resource/cleanup checks, protected custody
and runtime configuration, private PostgreSQL access, encrypted Hippius release
publication/readback/registration, unchanged private-suite qualification and the
private shadow canary. No competitive scoring, weights or emissions are enabled.
