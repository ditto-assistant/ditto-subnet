# Four-language native release set

This is the public-software handoff for operational gate 1. It groups four
executor OCI images and the Platform/native-worker bundle from one exact Git
revision. It does not approve, install, import or execute them on a native host.

## Build and reverify

Use an isolated checkout of a reviewed commit, with no tracked changes. Choose
a new canonical absolute output directory outside that checkout. The command
refuses existing output, exports tracked source via `git archive` for every
build, and never sends untracked files, `.env`, private corpus or Git credentials
as build context. Public package/base-image resolution still requires network;
this is not a claim of hermetic builds or independent build attestation.

```text
python3 -I infra/scripts/build-coding-native-release.py build \
  --revision <exact-40-character-commit> --output <new-absolute-directory>
```

The existing runtime builder builds and smoke-tests the native bundle. Each
production language target is exported as a single linux/amd64 OCI graph and
normalized by the existing image tool without changing image blobs. The fixed
profiles are Python v2, Node v2, Go v1 and Rust v1; profiles cannot be substituted.
No language image is loaded into a daemon or run by this wrapper. The runtime
builder's existing synthetic install/import smoke checks run inside BuildKit,
not on the operator's host. No private evaluation or provider call is made.

`release.json` is written only after full verification of all five artifacts.
It binds the exact source, per-image archive/approval/config/manifest identities,
and native archive/manifest/worker/Python/package identities. It has no absolute
operator paths. The printed release-manifest SHA is a local content identity,
not a custodian's approval. Reverification accepts that independently retained
identity and rehashes/reparses every artifact without extraction or subprocesses:

```text
python3 -I infra/scripts/build-coding-native-release.py verify \
  --revision <same-commit> --directory <absolute-release-directory> \
  --manifest-sha256 <independently-retained-64-character-SHA>
```

Keep the files under operator-controlled storage, with no concurrent writer.
Symlinks, hard links, special files, group/world-writable files, malformed
archives, mismatched revisions/profiles, noncanonical/duplicate JSON keys and
altered readiness fields are rejected. Metadata rechecks catch ordinary input
drift; a malicious same-UID/root writer is outside this offline tool's boundary.
Neither a self-generated index nor matching source labels authenticate a build.

Build failures retain partial public artifacts and never overwrite them on
retry. A missing index means no complete set was produced. Review the exact
directory; retry into a new output path. There is no automatic deletion/prune.
`source.oci.tar` files are retained build intermediates, not part of the indexed
handoff. Retain `release.json`, `native/runtime.tar`, and each language's
`runtime.oci.tar` and `approval.json`; CI retains these for seven days only.

## Independent approval and operational handoff

The custodian reviews the source, build provenance and exact bytes, then pins
the index SHA, each image approval SHA and native runtime archive SHA through
an independent channel. Pass those approved hashes to the existing
[native image importer](coding-hosted-image-v2.md) and
[runtime installer](coding-hosted-runtime-install-v2.md). Do not derive deployment
approval from newly received sidecars. Public software artifacts are not private
inputs/evidence; private Coding storage remains Hippius-only.

Every release index permanently records `independent_approval_required=true`,
`native_imported=false`, `runtime_qualification=false`, `canary_completed=false`,
`shadow_only=true`, and `weight_eligible=false`. Do not edit it to record later
progress: separate operational receipts must refer back to this immutable set.

The remaining gates are native host qualification, private release/key readiness,
recovery drills, the complete native control matrix, one authorized shadow canary,
and a separately authorized bounded shadow rollout. Public CI cannot complete
those gates. The `Coding native release set` workflow runs synthetic regressions
on PRs; only explicit dispatch builds the complete public artifact set, with
read-only GitHub permissions and no production environment or credentials.
