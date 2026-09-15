# Hosted-v2 execution and grading profile derivation

`dittobench-coding-hosted-profiles` derives the two task-bound documents an
assignment commits to: the authoring (execution) profile and the grading
profile of one private catalog index. It proves both against the worker's own
launch-time checks before anyone reviews their digests. The inference policy and
budget profile are separate, time-bound documents and are not produced here.

Run it only on an owner-controlled machine that already holds the verified
private payload. Outputs are private review inputs. They are not an approval,
an assignment, a published object or evidence that the task runs on the native
host.

```text
go build -o /ABS/PRIVATE/dittobench-coding-hosted-profiles ./cmd/dittobench-coding-hosted-profiles
/ABS/PRIVATE/dittobench-coding-hosted-profiles \
  --request /ABS/PRIVATE/request.json \
  --payload-authority /ABS/PRIVATE/payload/payload-authority.json \
  --objects /ABS/PRIVATE/payload/objects \
  --output /ABS/PRIVATE/NEW-DIRECTORY
```

## Request

The closed `dittobench-coding-hosted-profile-request-v1` document rejects
unknown fields and trailing data. It carries only reviewed choices:

| Field | Meaning |
|---|---|
| `catalog_index` | The selected task in the verified payload authority |
| `image_digest` | Approved immutable runtime image manifest digest, shared by authoring and grading |
| `candidate_limits`, `protected_limits` | Full `codingrunner.Limits`, using its exported PascalCase field names |
| `max_combined_disk_bytes` | Sandbox disk envelope |
| `budgets` | Model tokens, workspace tool calls (must equal `MaxToolCalls`) and wall time (at most the task's wall) |
| `build` | Must restate the task runtime policy's single build command exactly, or be `required: false` when it declares none |
| `test_groups` | `hidden` then `visible` driver commands with curator-approved expected counts |
| `execution_timeout_milliseconds` | Whole grading lifetime, at most one hour |
| `shadow_only`, `weight_eligible` | Explicit `true`, `false` |

CPU, PID, memory and scratch limits are **not** request fields. They are copied
from the task's private resource profile, which the worker requires them to
equal. The grader contract digest is the compiled
`HostedGraderContractSHA256`. The grader bundle digest comes from the payload
authority.

Hosted v2 binds no `test_manifest_sha256`. Nothing in the private task or
release authority enumerates tests, and a grader archive is not a test manifest,
so no other digest stands in for one. The grading profile, grader plan, hosted
grader contract field lists and grader evidence all omit the key. The worker,
executor and Platform reject a profile, manifest or evidence that carries one.
v1 still requires its curator test manifest unchanged.

What hosted v2 does bind is the grader bundle digest, whose extracted tree must
equal the task's `hidden_grader_tree_sha256`, and the reviewed `test_groups`
commands and expected counts inside `grading_profile_sha256`. A canonical test
manifest committed into the private task authority needs a new private corpus
release. It remains a prerequisite for weighted activation
(`docs/coding-memory-v2-weighted-activation.md`).

## Checks

1. Every task object is read from `<sha256>.bin`, and its size and digest must
   match the payload authority. The catalog record must agree with the payload
   task entry.
2. The execution profile is hashed with `ProfileDigest`. Its bytes must be exact
   canonical JSON with that hash.
3. `CheckTaskProfile` runs the worker's unexported task compilation with a
   throwaway authority. That covers the catalog record, object digests,
   resource equality, patch limit, visible snapshot compilation under the
   candidate limits, the hosted manifest, memory seed projection and run request.
4. `GradingProfileBytes` validates the grading profile and produces its exact
   canonical bytes.
5. `CheckGradingProfile` verifies the following against the objects:
   - the catalog record binding;
   - the runtime policy and resource digests;
   - the grader bundle digest, size limit and hidden grader tree;
   - the visible snapshot;
   - the hosted grading plan construction.
6. Each driver command must name exactly its own `--group`. When it has a
   `--suite`, the suite must be a clean relative regular file in the bundle that
   group reads: the grader bundle for `hidden`, the pristine workspace for
   `visible`.

A rejected request prints a fixed message, exits 70 and writes nothing. On
success the helper creates the new mode-`0700` output directory and exclusively
writes three mode-`0600` files: `execution-profile.json`,
`grading-profile.json` and `receipt.json`. It prints only the receipt.

The receipt records the task identity, payload and request digests, both profile
digests, `max_patch_bytes`, image, grader bundle and grader contract digests. It also sets `launch_checks_passed=true` and `approved=false`.
It contains no argv, suite path, source or private object bytes.

## What this does not establish

- **Driver compatibility.** It does not prove the private suite runs under the
  selected driver image. Expected counts and driver arguments must come from the
  private compatibility matrix, and that matrix must be repeated against the
  imported native images before a canary (see `coding_runtime/qualification`).
- **Approval.** Launch checks passing is not approval. The profile digests,
  limits, budgets, driver commands and expected counts still need independent
  review.
  The approved digests must then be bound together with a current inference
  policy and budget profile.
- **Activation.** It changes no Platform, host, release, assignment or reward
  state.
