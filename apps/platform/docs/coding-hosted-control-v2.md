# Native Platform control adapters

`HostedAuthoringControl` and `HostedControlServer` implement the Platform side of
the native authoring worker's control interface. Go's `NewControlClient` supplies
the concrete `codinghostedworker.Control` implementation. No public HTTP route,
startup factory, worker command, deployment or scoring setting is added.

The integration now connects private retrieval, native inference, encrypted
patch/transcript retention, PostgreSQL freeze acknowledgement and access removal.
Production configuration, protected key/secret loading, grading integration and
signed terminal results remain separate work. Private tasks stay on trusted
Platform infrastructure; validators and miners receive no control socket token,
Hippius credential, unwrap key or private evidence.

## Runtime composition

The Platform process constructs the existing registered-release retriever,
authoring assembler, native inference evidence publisher and the new authoring
evidence publisher. Give `HostedAuthoringControl` the fixed worker UUID, approved
execution and budget profiles, native inference policy, provider credential and
a pre-provisioned private bridge directory. Invalid provider credentials and
budget profiles reject construction. The publishers use distinct protected
spools and the existing dedicated Hippius evidence identity/probe.

Start `HostedControlServer` on a new owner-only Unix socket with a random 32-byte
local token. The Go runtime constructs `ControlClient` using that socket/token,
the independently approved execution profile and expected input authority, then
passes it to the authoring worker's public constructor. Never infer expected
authority from the incoming private input frame. None of this configuration goes
inside the candidate container or validator process.

The control service pins one source binding per evaluation and retains partial
inference state before awaiting grant issuance. It supports at most 64 bound
attempts per service incarnation; it is not a persistent multi-release scheduler.
The worker remains responsible for its irreversible start through the merged
PostgreSQL start adapter. Restarting this control service never grants a new
candidate start.

## Private transport

Each request uses `DITTO-HOSTED-CONTROL-V2\n`, the 32-byte token, a big-endian
uint32 header length and a bounded JSON command. Authentication precedes body
parsing. At most eight connections are admitted. The command commits a request
UUID, operation and native source binding. Responses echo the exact request,
operation and canonical source digest. There is no caller-supplied URL or
arbitrary method/path routing.

Small operations end after their command. `freeze` adds the declared patch
bytes. `retain` adds an explicit private freeze JSON document followed by the
raw transcript. The Go client includes the runner's otherwise non-serialized
patch only in this private retention document. It hashes/counts the transcript
before sending, streams it again and verifies that both observations agree.
The client half-closes its write side; trailing or truncated input fails closed.
Errors return EOF without private exception details.

`authoring` returns the existing verified private frame after its response
header. Platform rechecks authority between objects and before the completion
marker; Go verifies the frame against its independent expected authority. Normal
operations have 30-second client timeouts, authoring 180 seconds, and retention
600 seconds, all bounded by the caller's context. The worker's shorter contexts
remain authoritative. Large freeze JSON is bounded by four times the approved
patch limit plus 8 MiB; transcripts are bounded by the approved profile up to
512 MiB. Freeze JSON is parsed in memory; transcript bytes stream into bounded
encryption chunks. This is not a constant-memory claim for the whole request.

## Inference lifecycle and expiry

The adapter issues a native grant, constructs the policy-bound budget estimator,
provider adapter, durable evidence callback and local relay bridge. A grant can
expire before the assignment because its lifetime starts at committed execution
start. Its exact whole-second expiry is now passed separately to the Go relay.
The source registry retains the original assignment binding; the provider bridge
and model request deadline use the shorter grant expiry. A later or already
expired override is rejected.

Lost issuance acknowledgements do not lose the grant: revocation looks up the
committed evaluation-owned grant when partial local state has no returned UUID.
Repeated inference creation is refused. `revoke` cancels/awaits provider work and
verifies ledger drain while retaining the command socket. `close` disposes the
socket after the worker has completed all revocations and container shutdown.
`abort` closes private object grants even after expiry or retirement.

## Authoring evidence

The publisher retains a frozen patch/transcript as 8 MiB AES-256-GCM chunks with
independent random data keys/nonces wrapped by the existing RSA-OAEP-SHA256 public
key boundary. Each chunk binds its source, payload, ordinal, storage domain,
plaintext/ciphertext identities and wrapping-key identity. A separately encrypted
manifest retains all chunk identities. PostgreSQL keeps the aggregate identity
and manifest commitment, without plaintext evidence or storage URLs.

The exact ciphertext, envelope and canonical metadata are durably written to the
existing protected spool before any provider operation. The table
`coding_hosted_authoring_reservations` commits one immutable identity per evaluation. The publisher
reuses only byte-identical objects, refuses conflicting stored bytes, downloads
every uploaded/reused chunk and the manifest completely, and verifies hashes
before appending `coding_hosted_authoring_finalizations`. Both tables reject
updates/deletes and validate their source/lifetime on insert.

Interrupted upload retries the same prepared bytes. Rotation cannot rewrap
already prepared chunks or switch their storage domain. Partial local entries
remain fail-closed; no automatic garbage collector removes unpublished evidence.
Provision adequate spool capacity. The publisher permits new publication only
within 24 hours of the assignment deadline and with a probe less than 24 hours
old. An already finalized identity can replay its historical acknowledgement
without another provider operation; that is not fresh storage-availability proof.

Completed authoring requires the finalized inference-evidence set. Failure
evidence can be retained with incomplete inference, but cannot authorize a patch
freeze. `freeze` verifies the retained source, successful-run flag, inference
commitment and exact patch digest/size before committing the existing PostgreSQL
freeze transaction. The returned acknowledgement is independently reconstructed
from the committed state. It is not a grading or scoring result.

## Verification

The Go/Python integration test uses the real control client/server, registered
encrypted-input retrieval, native runner, source router and relay, both evidence
publishers and migrated PostgreSQL. A synthetic harness edits an actual workspace
file, and a synthetic provider response produces a real native reservation and
settlement. The test verifies encrypted chunk decryption, the patch commitment,
the shorter inference expiry and final access removal. Candidate transport,
provider responses and Hippius storage are synthetic; no production canary or
private grader is exercised.

Additional tests cover corrupt readback, exact replay, wrapping-key rotation,
missing inference evidence, lost grant acknowledgements, source drift and socket
authentication. The next operational milestone remains a complete private shadow
execution including pristine grading, terminal evidence and a signed result.
