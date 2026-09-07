# One-attempt Platform startup and launch handoff

`ditto.coding_hosted_worker` assembles the private control service and invokes the
[native Go launcher](../../../services/dittobench-api/docs/coding-hosted-runtime-v2.md)
for one already-approved, admitted assignment. It is an explicit standalone
Platform process, not an API lifespan hook, validator worker, queue or scheduler:

```text
<protected-platform-python> -I -m ditto.coding_hosted_worker \
  --private-shadow-once --config <absolute-protected-config.json>
```

The installed Python package and Go executable must be approved together from
the intended source revision. A development checkout/virtualenv with writable
shared ancestors is not an approved installation. No command, service unit,
Compose setting, public control signer or scoring gate is enabled by this PR.

## Pre-start authority

The configuration identifies one evaluation UUID, attempt UUID, assignment
digest and worker UUID. Startup reconstructs the complete approved assignment
from migrated PostgreSQL and checks the active registered release, current
screened artifact and policy, admission, deadline, selected task, and execution,
grading and inference-policy digests. Started, closed, frozen or mismatched
attempts are refused. It does not create an assignment, select a task, admit a
validator request or commit a start.

`PrivateV2InputRetriever.describe_selection` provides metadata from its verified
signed payload, with catalog Merkle-root verification. The index comes only from
the locked selection row. This method reads no remote object, unwraps no key and
issues no grant. Actual authoring retrieval continues to require a committed
start; hidden retrieval additionally requires the existing grading phase/freeze.

The screened-image URL is minted for the existing immutable upload-ID key with
at most 300 seconds of lifetime, bounded by the assignment deadline. Startup
rechecks the entire projection after URL minting. An image-store call never runs
while holding the assignment transaction. Retirement or identity drift during
that call cannot produce a launch handoff.

## Protected configuration

The configuration is `HostedPlatformRuntimeInput` in
`ditto/api_models/coding_hosted_runtime.py`, with schema
`dittobench-coding-hosted-platform-runtime-v2`, explicit `shadow_only=true` and
`weight_eligible=false`. Known fields are validated strictly; unknown fields are
ignored and cannot override the generated Go authority. No private configuration
model is exposed through public OpenAPI.

| Fields | Required input |
| --- | --- |
| `worker_id`, `evaluation_id`, `attempt_id`, `assignment_sha256` | Exact intended approved execution; no discovery or selection fallback |
| `runtime_root` | Fresh persistent owner-only directory dedicated to this invocation |
| `worker_executable`, `python_executable` | Protected installed Go launcher and Platform interpreter; the Go validation step checks the interpreter before execution |
| `unwrap_executable`, `unwrap_work_root` | Separately provisioned, approved native v2 key-custody helper and private working directory |
| `postgres_environment_file` | JSON array of start-helper `POSTGRES_*=value` entries |
| `hippius_environment_file` | JSON object with only the supported `DITTO_CODING_HIPPIUS_*` reader/evidence settings |
| `image_storage_file` | JSON object with `endpoint_url`, `bucket`, `access_key`, `secret_key`, `region`; HTTPS is required |
| `provider_key_file` | Raw nonempty printable-ASCII provider key, without a trailing newline |
| `execution_profile_file`, `grading_profile_file` | Exact canonical approved native profile bytes |
| `budget_profile_file`, `policy_file` | Reviewed runtime budget profile and native inference policy bound to it |
| `transport_manifest_file`, `payload_authority_file`, `publication_receipt_file`, `curator_public_key_file` | Existing verified encrypted-release authorities; no plaintext corpus import |
| `evidence_public_key_file`, `evidence_wrapping_key_sha256` | RSA public wrapping key and its independently configured fingerprint |
| `probe_receipt_file` | Matching Hippius reader/evidence authority receipt, less than 24 hours old; checking a receipt is not a new live provider probe |
| `host` | Existing Go host settings: `docker_executable`, `docker_socket`, `router_listen`, `egress_network`, `egress_proxy`, `executor_repository`, `candidate_uid`, `candidate_gid`, optional `seccomp_profile` and `apparmor_profile` |
| `spool_max_bytes`, `spool_max_objects` | Per-spool capacity; defaults 2 GiB / 4096 objects, subject to explicit bounds |

All configuration, profile, credential, authority and public-key files are
bounded regular single-link mode-0600 files owned by the worker, below canonical
mode-0700 directories. Symlinks, FIFOs/devices, shared writable non-sticky
ancestors and permissive files fail closed. Helpers/Go executable must be
owner-owned mode 0500 or 0700 in protected private directories and must be
distinct files. Protect their interpreters, installed dependencies and custody
configuration too: permission checks do not attest arbitrary helper code.

PostgreSQL permits only `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_COMMAND_TIMEOUT`,
`POSTGRES_POOL_MIN_SIZE` and `POSTGRES_POOL_MAX_SIZE`. The first five are mandatory;
duplicates, injected environment names and invalid numeric bounds are refused.

The Hippius object permits the `DITTO_CODING_HIPPIUS_` suffixes `ENDPOINT_URL`,
`PRIVATE_INPUT_BUCKET`, `PRIVATE_INPUT_CURATOR_ACCESS_KEY`,
`PRIVATE_INPUT_READER_ACCESS_KEY`, `PRIVATE_INPUT_READER_SECRET_KEY`,
`SEALED_EVIDENCE_BUCKET`, `EVIDENCE_MEDIATOR_ACCESS_KEY`,
`EVIDENCE_MEDIATOR_SECRET_KEY`, optional `REGION` and `TIMEOUT_SECONDS`.
Reader, curator and evidence identities must differ; private-input and evidence
buckets must differ. The existing Hippius-only endpoint checks remain in force.
The separate image store serves already-screened miner images, not private
Coding inputs or evidence, and is not an alternate private-data store.

## External key custody

`ProcessPrivateV2Unwrapper` is a concrete client to an approved external-custody
executable. It does not implement a KMS server, load a private RSA key, reuse a
legacy v1 ticket, or authorize arbitrary decryption. Production must provision
and review the helper and its independently enforced grant/key policy.

The [native custody service and Unix proxy](coding-private-v2-custody.md) now
provide a concrete, separately owned implementation of this protocol. They are
explicit private processes, not automatically provisioned or started here.

The helper receives canonical JSON for `PrivateV2UnwrapRequest`, schema
`dittobench-coding-private-v2-unwrap-v1`. This is the first unwrap-message version
for native private v2, not the old v1 ticket protocol. It binds grant, evaluation,
attempt, registration, transport, plaintext/ciphertext/wrapping-key/AAD digests,
wrapped data key, phase, role, audience, expiry and committed frozen patch.

The response is a bounded JSON object containing:

```text
schema: dittobench-coding-private-v2-unwrap-result-v1
request_sha256: SHA-256 of the canonical request including its newline
data_key_b64: base64 encoding of exactly 32 bytes
weight_eligible: false
```

Unknown response fields are non-authoritative. The helper must independently
verify current grant/phase/expiry and exact registered wrapped-key/AAD authority;
echoing a request digest alone is not authorization. A hardware/remote custody
service and its credentials remain outside the worker's configuration and the
candidate. Never implement a production helper that blindly unwraps any
caller-supplied ciphertext.

Each call is a fixed executable, without a shell, in a fresh process group with
only `PATH`, `LANG` and `LC_ALL`. No ambient provider, storage, database or Python
override is inherited. Requests are at most 16 KiB; responses at most 1 KiB;
the timeout is the smaller of 20 seconds and the grant lifetime. Stderr is
discarded. Wrong response binding, malformed keys and late replies fail closed.
The retriever still rechecks the durable grant before unwrap and after decryption.

## Startup, handoff and completion

1. Load/validate protected configuration and inspect the unstarted assignment.
2. Exclusively write and fsync `platform-consumed` beneath `runtime_root`.
   Partial or existing markers refuse reuse before another service/worker starts.
3. Construct distinct authoring/grading grant stores and retrievers, and three
   independent inference/authoring/terminal spools. Verify the evidence public-key
   fingerprint and provider-probe linkage. Start one evaluation-bound control
   socket with a fresh local token.
4. Generate the Go configuration from verified metadata and the rechecked image
   handoff. Write only its control token, canonical execution/grading profiles,
   start-helper database allowlist and protected references. Provider, Hippius,
   custody-helper and image-store credentials are absent from the Go document.
5. Invoke Go with `--validate-only --config ...`. This checks files and profiles
   without changing its environment, contacting Docker/Platform, consuming its
   own state directory, or executing code. Recheck the assignment again.
6. Invoke Go once with `--private-shadow-once`. The existing PostgreSQL start
   helper commits before candidate execution. Local or host restart never
   changes an already-started assignment into a fresh run.
7. Accept completion only after parsing the bounded Go receipt and independently
   matching its terminal identity/finalization and source/profile commitments in
   PostgreSQL. A zero exit code or self-reported digest is insufficient.
8. Drain the private control server, revoke/close every bound inference/private
   grant, then close all spool and reader handles. Cleanup failure is not success.

If control drain fails, a provider retention task may still be active. Its
spools, reader and database are not explicitly disposed behind it; the process
fails with the private state intact. Disposal failures after verified drain
still attempt every remaining resource and never produce successful completion.

The control server stops accepting, cancels/joins active handlers, and only then
waits for socket closure. Python's `wait_closed()` includes active clients;
waiting first would delay cancellation behind a long-running request. A server
instance cannot start twice, including concurrently or after a partial failure.

SIGINT/SIGTERM first give the Go process its own cleanup opportunity. The parent
allows up to 30 minutes for this conservative drain before forced group stop;
the overall invocation also has a bounded post-deadline finalization window.
Operators must not configure a short service-manager kill timeout. Forced stop,
unknown cleanup or failed evidence retention still requires exact-resource
reconciliation; killing a trusted process does not prove Docker cleanup.

No state directory, evidence spool, tombstone or stale container is automatically
deleted. Spool handles close, but encrypted files remain. Process loss before
evidence capture remains non-rerunnable. This is not a durable recovery reader,
host-crash reconciler, bulk retry mechanism or scoring-readiness certification.

## Verification and operational boundary

Integration tests generate the real Go configuration, exercise its validation-only
command, commit the real PostgreSQL start, and use the newly constructed service
through native Go authoring/grading and terminal verification. The external helper
decrypts synthetic fixture keys in a separate process. Candidate/executor,
provider and remote-storage transports remain synthetic; no production private
run or production key-custody approval is claimed. Other tests cover retirement
during image minting, pre-start privacy, replay refusal, injected credentials,
malformed helper responses, bounded I/O, cancellation and cleanup failures.

Remaining operational work includes approved installation/production runtime
images and real test drivers, custody-helper/KMS provisioning, isolated host and
network policy, encrypted release publication/readback/registration, and a
deployed private shadow canary. Public signed admission/result delivery retains
its separate default-off `HostedCodingControl` signer configuration; this worker
does not enable it or load a public control signing key. Scoring, weights and
emissions remain separate activation decisions.
