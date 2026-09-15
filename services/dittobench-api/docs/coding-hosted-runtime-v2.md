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
| `router_listen` | Explicit private IPv4 address and port 1024–65535; wildcard, loopback and public binds fail. It is a host address in `host` mode and the rootless daemon's default bridge gateway in `rootless-netns` mode |
| `router_namespace` | Optional. Omitted or `host` keeps the existing listener in the worker's network namespace; `rootless-netns` creates it inside the rootless daemon's RootlessKit namespace (below). Any other value fails |
| `router_expires_at_unix` | Required only with `rootless-netns`, and must be omitted, null or 0 otherwise: the connectivity profile's `expires_at_unix`, in the future and at most 24 hours away. The worker ends candidate router access at this time or the assignment deadline, whichever is earlier (below) |
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

## Rootless-netns router listener

The source router admits a request only when its socket source equals the
harness container's Docker address. A `host`-mode listener cannot work with a
slirp4netns rootless daemon: slirp4netns reaches host addresses from a host
socket, so every candidate request arrives from the host's own address and every
route returns 404. This was reproduced with RootlessKit, slirp4netns and Docker
29.1.3: a listener on the RootlessKit bridge gateway saw the ICC-disabled job
container's address, while a host listener saw only the host address.

`router_namespace: rootless-netns` keeps per-container source binding.

Before the invocation is consumed, both `--validate-only` and
`--private-shadow-once` run a read-only precheck. It reads the default bridge
with one Engine API `GET /networks/bridge` on the configured socket (no Docker
CLI, configuration or credential helper) and requires its gateway to be
`router_listen`'s address. It then runs steps 2-4 below without starting nsenter
or creating a socket. Any failure is a configuration refusal that leaves the
state root unconsumed. In this mode `--validate-only` therefore contacts the
local daemon socket; host mode keeps the file-only validation.

After consuming the invocation and checking the rootless daemon, but before any
candidate starts, the worker repeats every check and:

1. requires `docker network inspect bridge` to report exactly one private IPv4
   default-bridge gateway equal to `router_listen`'s address. `host.docker.internal`
   maps to that same address (`HostGatewayIP`), never to eth0 or `host-gateway`;
2. reads RootlessKit's `child_pid` only from
   `/run/user/<worker-euid>/dockerd-rootless/child_pid`, the fixed
   `dockerd-rootless.sh` state path. It opens each component without following
   links. The UID runtime directory must be private, the state directory must not
   be writable by others, and the file must be a read-only, single-link decimal
   pid. A state-directory override is unsupported;
3. opens a pidfd, pins the child's user and network namespace descriptors, and
   requires the process to be alive after inspection. The user namespace must be
   owned by the worker UID, be a direct child of the worker's user namespace and
   differ from it. The network namespace must be owned by that user namespace and
   differ from the worker's. All of the child's UIDs must map to the worker UID;
4. takes the listening process of the configured Docker socket (`SO_PEERCRED`)
   and requires that daemon to be in the same pinned user and network namespaces;
5. runs `/usr/bin/nsenter --user=/proc/self/fd/4 --net=/proc/self/fd/5
   --preserve-credentials -- <bundle>/bin/dittobench-coding-router-listener
   --listen <router_listen>` with an empty environment. Descriptors 4 and 5 are
   the pinned namespaces, so a reused pid cannot redirect the join. The helper
   must sit beside the installed worker executable and both it and nsenter must
   pass the protected-executable check at configuration load;
6. receives exactly one `SOCK_SEQPACKET` message with the fixed protocol payload
   and exactly one `SCM_RIGHTS` descriptor, and requires a successful helper exit.
   Every other descriptor, message or truncation is closed and refused. The
   helper binds without `IP_FREEBIND` or `SO_REUSEPORT`, so the address must be
   local in the joined namespace and nothing else can share the port;
7. accepts the descriptor only if it is an `AF_INET`, `SOCK_STREAM`, TCP socket
   in listening state, without `SO_REUSEPORT`, bound exactly to `router_listen`,
   and `SIOCGSKNS` returns the pinned RootlessKit network namespace. The existing
   router then serves on it from the worker process.

Any mismatch fails the attempt. There is no fallback to a host-namespace
listener. Detached-netns RootlessKit is refused: the daemon then runs in the host
network namespace while RootlessKit's child holds the detached one, so step 4
fails. Docker 29.1.3's `dockerd-rootless.sh` does not use detached mode. Later
scripts default `DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS` to true; a local
Docker 29.8.0 / RootlessKit 3.1.0 rehearsal was refused this way. A Docker
upgrade must set it to false or wait for reviewed support.

### Router authority window

In `host` mode, host nftables bound candidate-to-router traffic. The rules
accept it only before the profile's `meta time < expires`, only from the timed
daemon cgroup, and replies need the timed UID lease. In `rootless-netns` mode
that traffic stays inside RootlessKit's network namespace, so none of those
kernel checks apply to it. The worker replaces them for the router listener:

- The listener ends at `min(router_expires_at_unix, assignment deadline)`, or
  as soon as the worker's run context ends. That happens on SIGTERM, which is how
  the worker learns that authority is being revoked: an operator stop, or systemd
  stopping the unit because the `ditto-coding-hosted-egress.service` guard it is
  `BindsTo=` stopped. `ExecStopPost` removes the nft grants only after the worker
  exits, so the worker has already closed the router by then.
- When the window ends, the worker closes the listening socket, so the kernel
  refuses new connections to the gateway port, and closes every accepted
  connection, including requests still in flight. Accepts that race the end are
  closed at once. The router then still shuts down cleanly.
- The source registry still refuses any request after its binding deadline.

What remains different from `host` mode:

- This is enforced by the worker process, not the kernel. A stopped or wedged
  worker cannot close the listener, whereas nft timeouts expire on their own. The
  worker cgroup is still killed by the unit's stop timeout.
- Within the window, anything that can route to the bridge gateway inside
  RootlessKit's namespace can open a TCP connection to the router: other
  containers of the same daemon, and daemon-UID processes in that namespace such
  as dockerd and containerd. Host nftables cannot restrict this. What still
  protects every route is unchanged: a request is served only if its socket
  source is the registered harness container's own address, and its path carries
  the unguessable route token. All other requests get 404.

Operational consequences:

- Candidate-to-router traffic stays inside RootlessKit's network namespace and
  never crosses host nftables. The restricted proxy is still reached through
  slirp4netns. A connectivity profile for this mode therefore lists only the
  proxy in `candidate_tcp`. Platform's bounded rollout and the connectivity role
  refuse a profile that also lists `router_listen`: that entry would grant
  daemon-UID traffic to a host address with no router behind it.
- The worker unit hides `/run/user`. The connectivity role's
  `coding_hosted_router_namespace: rootless-netns` switches to `ProtectHome=tmpfs`
  and bind-mounts only the `child_pid` file read-only. The default keeps
  `ProtectHome=yes`.
- The native host prerequisites record (open PR #1899) currently pins
  `router_listen` to `<host address>:18080` and checks that the address is local
  and the port free on the host. In this mode that record must instead carry the
  daemon's default bridge gateway and `router_namespace`, and its bind probe no
  longer applies on the host. Consider pinning the daemon's `bip` so the gateway
  is deterministic. That is a follow-up to #1899, not part of this change.

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
redaction. The rootless-netns listener has unit tests for descriptor passing and
socket verification over real socketpairs, child-pid and namespace refusals,
mode validation, the precheck refusing before consumption, and the authority
window closing established and new connections at expiry or cancellation. The `coding-rootless-router.yml` workflow starts a real rootless
Docker 29.1.3 daemon on a disposable runner and proves the in-namespace
admission and the host-address failure mode. It also checks that the read-only
precheck passes, and that a host-namespace socket is refused: SIOCGSKNS returns
EPERM for the non-root daemon user. A socket from another network namespace
owned by RootlessKit's user namespace is refused by the namespace identity
comparison itself. Finally, the authority window closes an admitted keep-alive
connection at its end, and the kernel refuses new connections after it. Existing native worker/input/grader and Go/Python control tests remain
the composition tests; the new launcher tests do not claim real Docker/private
provider execution or a live canary.

The Platform companion now supplies private control startup and the assignment
projection. Still required: approved installations and custody provisioning,
reviewed production runtime images/test drivers, encrypted private-release
publication/readback/registration, operational reconciliation, and a deployed
private shadow canary. Public signed control retains separate default-off signer
configuration. No deployment, release registration, private-data upload, scoring,
weight or emission is enabled by building or merging these commands.
