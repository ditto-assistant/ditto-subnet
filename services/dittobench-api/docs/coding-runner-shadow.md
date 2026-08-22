# Coding-runner shadow core

`internal/codingrunner` is the shadow-only authoring workspace and freezer for
DittoBench Coding contract v1. It implements step 2 of the
[private execution protocol](../../../research/dittobench-coding-datagen/docs/PRIVATE-EXECUTION-PROTOCOL.md).

It does not register a scorer route, start a sidecar, load a private catalog,
run hidden tests, submit evidence, change a `bench_version`, or affect weights.
Those remain separate reviewed changes.

## Authority boundary

One `Session` receives:

- one task-scoped runner manifest;
- a stream containing one exact visible tar or gzip-tar capsule;
- an optional trusted `CommandExecutor` supplied by the later sandbox layer.

The runner streams the capsule into a mode-0600 bounded temporary artifact while
computing its raw SHA-256, then extracts from that file. It rejects traversal,
links, special files, unsafe modes, duplicate paths, `.git`, archive bombs, and
base-tree disagreement, and materializes into a private temporary directory.
The workspace path is never returned by the API. Capsule extraction and tree
identity include regular files and directories, so empty untracked directories
cannot disappear from frozen evidence.

Tree snapshots retain paths, modes, sizes, and digests rather than every file's
contents. Freeze reads only added or modified bytes, bounded by the signed patch
profile. This prevents repository size from becoming equivalent process-memory
use.

The core deliberately has no default subprocess executor. `tests.run` and
`build.run` resolve only manifest-owned command IDs and fail closed until a
trusted executor is injected. An executor must provide network denial, process
group cleanup, bounded scratch space, and filesystem confinement. This prevents
an incomplete integration from running candidate code directly on the scorer
host. It must mount the supplied host directory at a fixed candidate-visible
path such as `/workspace`; it may not use the random host path as the candidate
process working directory.

## Tool and event contract

The embeddable handler serves only `POST /tool`; its caller must mount that
handler behind an unguessable, source-bound outer capability. It supports:

```text
repo.list_tree
repo.search
repo.read_file
repo.read_range
repo.apply_patch
repo.create_file
repo.delete_file
tests.run
build.run
git.status
git.diff
```

Paths and command IDs are enforced from the runner manifest, not trusted from
the model-facing runtime policy. File edits use an exact current SHA-256 and
atomic replacement. Create and delete require separately enumerated paths.
There is no shell tool or caller-selected executable.

Calls are serialized. The first accepted call consumes the next sequence and
extends a SHA-256 event chain. Replaying the same typed request with the same
`call_id` returns the cached response and sequence; reusing the ID for different
known fields latches candidate integrity without consuming an event. Malformed, cross-case,
cross-profile, expired, oversized, and post-freeze requests fail before event
creation.

The signed response limit covers the complete encoded HTTP response. The core
reserves 2 KiB while constructing results and then checks the exact serialized
receipt before persistence. Default profiles cap responses at 256 KiB and 256
calls, and require enough aggregate replay-cache budget to retain the worst-case
response for every accepted call. Replays do not consume additional budget.

Every canonical event is appended and synced to a mode-0600 JSONL transcript
before the response is acknowledged. The default transcript and replay-cache
budgets are 128 MiB and 64 MiB respectively; manifest validation rejects any
combination of calls/request/response sizes that those aggregate budgets cannot
retain. After freeze, `WriteTranscript` streams the exact bytes with their
SHA-256, size, and event count.

Each session owns a deadline context. Freeze and Close cancel that context
before waiting for serialized state, and repository walks, reads, searches, and
the injected executor observe cancellation. This lets deadline revocation
preempt an active operation instead of waiting behind it indefinitely.

Command output is bounded and replaces the private workspace prefix with
`<workspace>`. Any command-created filesystem change permanently latches a
candidate-integrity freeze failure, even if the modified path was otherwise
editable.

## Freeze format

`Freeze` revokes mutation before inspecting the tree and caches exactly one
result. Success emits a canonical, newline-terminated structured patch:

```json
{
  "base_tree_sha256": "...",
  "case_id": "opaque-case",
  "changes": [
    {
      "after_content": "base64 bytes or null",
      "after_sha256": "digest or null",
      "before_sha256": "digest or null",
      "kind": "added | modified | deleted",
      "mode": 420,
      "path": "src/example.py"
    }
  ],
  "coding_contract_version": 1,
  "schema": "dittobench-coding-frozen-patch-v1",
  "visible_bundle_sha256": "..."
}
```

The result binds the raw visible bundle, base tree, final tree, structured
patch, sorted changed-path root, final authoring event root, and complete
authoring-transcript digest/byte count. The later
pristine grader must replay these exact full-file transitions and verify every
digest; it must not fuzzy-apply a miner-provided diff.

If freeze detects a protected path, undeclared add/delete, directory/mode
change, symlink, special file, resource overflow, or previously latched command
mutation, it returns bounded failure identity instead of a partial submission.
Repeated freeze calls return a deep copy of the same cached result.

## Validation

```bash
cd services/dittobench-api
go test -race ./internal/codingrunner ./internal/codingcontract
go vet ./internal/codingrunner ./internal/codingcontract
go test ./...
```

The tests cover tar/gzip streaming and verification, traversal, `.git`, symlinks, duplicate
entries, manifest immutability, typed editing, create/delete, protected paths,
empty-directory capture, umask-independent modes, command mutation latching,
output redaction, cancellation-before-freeze, bounded replay/transcript
retention, transcript replay, concurrent idempotency, conflicting call IDs,
request bounds, Unicode rejection, deadlines, cached freeze, patch replay, and
fixed identity roots.
