# One-attempt hosted worker launcher

The [Platform startup companion](../../../apps/platform/docs/coding-hosted-platform-runtime-v2.md)
now constructs the private control service and this launcher's approved input.
`--validate-only --config <private-file>` checks that input without starting work;
it is mutually exclusive with `--private-shadow-once` and is not runtime readiness.

`cmd/dittobench-coding-hosted-worker` connects the native authoring/grading worker
to its concrete local control client, PostgreSQL start helper, screened-harness
sandbox, source router and phase-separated executors. It runs only on trusted
Platform infrastructure. It is not installed in the validator/scorer service,
Docker Compose, a scheduler or a deployment. All persistent scoring gates remain
unchanged; the command requires an explicit subprocess-only opt-in:

```text
dittobench-coding-hosted-worker --private-shadow-once --config <absolute-private-file>
```

Build from `services/dittobench-api` with
`go build ./cmd/dittobench-coding-hosted-worker`. The installed executable and
Platform Python environment must come from an approved source revision.

## Protected startup input

The owner provisions the configuration outside Git from the committed assignment,
verified private payload descriptor and approved profiles. The launcher does not
select tasks, approve profiles, mint image URLs or treat its local config as a
replacement for database authority. No validator/miner supplies this file.
The existing start helper rechecks current authority before committing a start;
the private control service independently verifies each subsequent operation.

Configuration and referenced JSON/token files must be regular, single-link,
owner-owned mode-0600 files in canonical, non-symlink mode-0700 directories.
Shared writable non-sticky ancestors are rejected. Reads are bounded and reject
duplicate JSON keys. Unknown fields remain non-authoritative. Secret values and
private filenames are never printed. Files must not change during a launch;
same-UID/root compromise is outside these file-permission checks.

| Configuration field | Required value or authority |
| --- | --- |
| `schema` | `dittobench-coding-hosted-runtime-v2` |
| `shadow_only`, `weight_eligible` | Explicit `true`, `false`; neither can be omitted |
| `expected` | Existing `codinghostedinput.Expected` JSON from the approved assignment and verified payload descriptor |
| `harness` | Existing `codingharness.HostedBinding`, using its Go field names listed below |
| `authoring_profile_file` | Approved authoring profile; `ProfileDigest` must equal `expected.execution_profile_sha256` |
| `grading_profile_file`, `grading_profile_sha256` | Exact canonical approved grading template and its SHA-256; no inferred counts, fixture driver or fabricated plan |
| `control_socket`, `control_token_file` | Already-running Platform control service's owner-only Unix socket and raw 32-byte nonzero token (not base64 or newline-terminated) |
| `python_executable`, `postgres_environment_file` | Approved installed Platform interpreter; JSON array containing only the start helper's allowed `POSTGRES_*=value` entries |
| `state_root` | New pre-provisioned mode-0700 persistent directory dedicated to this invocation; never recycle it for recovery |
| `docker_executable`, `docker_socket` | Protected absolute executable named `docker`; explicit owner-only local Unix socket in a private directory |
| `router_listen` | Explicit private IPv4 host-gateway address and port 1024–65535; wildcard, loopback and public binds fail |
| `egress_network`, `egress_proxy` | Provisioned restricted Docker network and credential-free `http://<private-IP>:<port>` allowlisting proxy |
| `executor_repository` | Approved repository used with each profile's immutable image digest |
| `candidate_uid`, `candidate_gid` | Explicit nonzero executor identity |
| `seccomp_profile`, `apparmor_profile` | Optional approved policy; `unconfined` is refused. Empty seccomp retains Docker's built-in policy |

`expected` requires the existing fields `evaluation_id`, `attempt_id`, `worker_id`,
`assignment_sha256`, `registration_sha256`, `execution_profile_sha256`,
`task_commitment_sha256`, `deadline_unix`, `max_patch_bytes`, `catalog_index`,
`corpus_release_id` and `private_release_sha256`.

`harness` uses `EvaluationID`, `AttemptID`, `WorkerID`, `AssignmentSHA256`,
`AgentID`, `AgentArtifactSHA256`, `ProfileCapabilityID`, `Deadline`,
`ScreenedImageSHA256`, `ScreenedImageID`, `ScreenedImageRef`, `ScreenedImageSize`,
`ScreeningPolicyVersion`, `ImageURL` and `ImageExpiresAt`. Times are JSON RFC3339;
the assignment deadline is whole-second and at most one hour away. The image
capability expires within six minutes and no later than the assignment. Its
profile capability is exactly `hosted-<AttemptID>`.

The loader checks matching source fields, authoring profile/budgets/patch bounds,
canonical grading digest, native grader contract, resource policy, unique command
IDs, sorted required groups, required test-driver command and bounded expected counts
before consuming the local invocation. Template validation deliberately does not
invent a snapshot, patch or per-attempt plan. Full grading validation still runs
after the committed freeze. Local validation is not proof that approved images
or the network policy actually work; runtime preflight retains those checks.

## Environment and credential boundary

This is a dedicated process: startup replaces its environment with exactly
`PATH` (the approved Docker executable directory), `DOCKER_HOST` (the explicit
Unix socket), `DOCKER_CONFIG` (a newly created empty private directory), and
`TMPDIR` (new private workspace storage beneath `state_root`). No ambient Docker
context, credential helper, registry credential, proxy, Python override, provider
key or sandbox-debug flag survives. Executables and their resolved ancestors
must be root/worker-owned and not group/world writable. Installed code approval
and protection are still operator responsibilities, not binary attestation.

The start helper receives only its separately loaded PostgreSQL allowlist through
the existing isolated `python -I` invocation. No database value enters the Docker
environment or candidate. The local control token stays in the trusted client;
provider, Hippius and unwrap/signing keys stay in Platform's separate service.

The sandbox is constructed explicitly, without environment-derived defaults:
rootless and isolated-daemon checks, hardening, restricted egress, fixed harness
port 8080, and resource caps from the approved authoring profile. No private-URL
fetch bypass, repository credential, provider shim or host socket mount is
enabled. The existing executor verifies pinned production images and rejects
certification fixtures when commands/grading run. The operator must provision
the daemon's network/firewall/proxy and shared workspace visibility beforehand.

## Single use, cancellation and failure

Before Docker activity, the launcher exclusively creates and fsyncs `consumed`
and its parent directory. Any existing marker, even an empty/partial one, refuses
another invocation using that directory. This conservative local tombstone is
not execution authority: PostgreSQL's irreversible start also prevents a second
candidate launch if someone changes directories or restarts on another host.

The same in-memory attempt runs authoring once. Up to two additional authoring
finalization calls can resend only captured evidence/freeze acknowledgements.
Grading can make up to three publication calls; its existing one-shot state
permits only the first grading execution, then exact captured result bytes.
There is no candidate, inference or grading re-execution loop.

SIGINT/SIGTERM cancel the attempt context. Cleanup runs independently of that
cancelled context, with the existing per-operation timeouts and up to three
cleanup calls on the same handles. Each call attempts revocations and container
shutdown even if another boundary fails. The source router closes only after
its routes have been released. Cleanup can take several minutes; the service
manager must allow that drain time. A forced kill remains an unconfirmed cleanup.

After success, stdout contains only a local completion schema, terminal evidence
SHA-256, `shadow_only=true` and `weight_eligible=false`. This is neither a signed
validator response nor a claim that the candidate passed its tests. Public
signed result delivery continues through Platform's existing endpoint.

On failure, the process exits nonzero with one fixed redacted diagnostic.
Unconfirmed cleanup requires operator recovery of the exact retained resources;
no broad Docker prune or cross-worker stale cleanup is run. The launcher does not
delete the state directory or tombstone. Failed retention may leave a frozen
workspace there. Do not publish, automatically purge or reuse these private
files. Keep both this directory and Platform's encrypted evidence spools until
the exact ledger/evidence/container state is reconciled.

Process loss before durable evidence, or after a grading claim but before result
capture, remains non-rerunnable. This launcher provides refusal, not automatic
host-crash reconciliation or an encrypted evidence recovery reader. On restart
it does not turn an ambiguous attempt into a fresh run or a successful terminal.

## Verification and remaining deployment work

Tests cover configuration/hash/authority rejection, protected files, concurrent
single-use consumption, partial markers, environment replacement in a subprocess,
bounded finalization retries, cancellation, cleanup failure and command-output
redaction. Existing native worker/input/grader and Go/Python control tests remain
the composition tests; the new launcher tests do not claim real Docker/private
provider execution or a live canary.

The Platform companion now supplies private control startup and the assignment
projection. Still required: approved installations and custody provisioning,
reviewed production runtime images/test drivers, encrypted private-release
publication/readback/registration, operational reconciliation, and a deployed
private shadow canary. Public signed control retains separate default-off signer
configuration. No deployment, release registration, private-data upload, scoring,
weight or emission is enabled by building or merging these commands.
