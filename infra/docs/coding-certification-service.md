# Host coding certification service

The contract-v1 public certification canary runs in a host-level service beside
a dedicated rootless Docker daemon, not in the Compose scorer. The validator
reaches it only over one Unix socket at a fixed path. Everything here is
default-off. This document describes the reviewed shape; it authorizes no
installation, start, certification run or activation.

## Topology

```text
validator container (root)                 host
  CodingCanaryRuntime                        ditto-coding-cert (uid U, non-root)
    verified Unix socket client  ── bind ──>   /run/ditto-coding-certification/control.sock
                                                 dittobench-coding-certification-service
                                                   ├─ canary + readiness routes only
                                                   ├─ router listener on <bip gateway>:11438,
                                                   │  created inside RootlessKit's netns (#1919)
                                                   └─ Docker CLI ──> /run/ditto-coding-certification-docker/docker.sock
                                                                      dockerd-rootless.sh (same uid U,
                                                                      DETACH_NETNS=false, pinned bip,
                                                                      io.heyditto.dittobench.isolated=true)
```

- **Service.** `dittobench-coding-certification-service` runs as the dedicated
  user `ditto-coding-cert`. It refuses to run as root and refuses unless
  `DITTOBENCH_CODING_CERTIFICATION_SERVICE_ENABLED` is exactly `true`
  (exit 3). It takes no arguments.
- **Daemon.** A rootless dockerd for the same user. RootlessKit stays in
  non-detached mode (`DOCKERD_ROOTLESS_ROOTLESSKIT_DETACH_NETNS=false`). The
  default bridge `bip` is pinned, so the router address (the bridge gateway) is
  known before the daemon starts.
- **Router.** The source router listener is created inside RootlessKit's
  network namespace with #1919's `rootlessnetns` helper, so candidate traffic
  keeps its container source address. The harness is published on host
  loopback, which the service reaches directly because it runs on the host.
- **No TCP control listener.** The only control surface is the Unix socket.

## Fixed paths, owners and modes

| Object | Path | Owner | Group | Mode |
| --- | --- | --- | --- | --- |
| Control socket directory | `/run/ditto-coding-certification` | `ditto-coding-cert` | `ditto-coding-cert-clients` | `0750` |
| Control socket | `/run/ditto-coding-certification/control.sock` | `ditto-coding-cert` | `ditto-coding-cert-clients` | `0660` |
| Daemon socket directory | `/run/ditto-coding-certification-docker` | `ditto-coding-cert` | (any) | `0700` |
| Daemon socket | `/run/ditto-coding-certification-docker/docker.sock` | `ditto-coding-cert` | (any) | `0600` |
| Router helper | `/usr/local/lib/ditto-coding-certification/dittobench-coding-router-listener` | root | (any) | not group/other writable; pinned SHA-256 |
| `nsenter` | `/usr/bin/nsenter` | root | (any) | not group/other writable |
| Certification pack | `/usr/local/lib/ditto-coding-certification/certification-root` | root | root | read-only |
| Locked inference policy | `/usr/local/lib/ditto-coding-certification/coding_inference_policy_locked_v1.json` | root | root | read-only |
| Private state | `/var/lib/ditto-coding-certification/private` | `ditto-coding-cert` | | `0700` |
| Control token | `$CREDENTIALS_DIRECTORY/control-token` (systemd `LoadCredential`) | | | |

None of these paths is configurable. Every ancestor of both socket paths must be
a real directory, owned by root or the service user, and not writable by group
or others (a root-owned sticky directory is accepted).

## Configuration

Service environment (all required when enabled; nothing else is read, including
no `DITTOBENCH_SANDBOX_*`, `DOCKER_HOST`, proxy or CA variable):

| Variable | Shape |
| --- | --- |
| `DITTOBENCH_CODING_CERTIFICATION_SERVICE_ENABLED` | exactly `true`; anything else is disabled |
| `DITTOBENCH_CODING_CERTIFICATION_SERVICE_UID` | the service user's uid; must equal the process euid; not 0 |
| `DITTOBENCH_CODING_CERTIFICATION_CONTROL_GID` | the client group gid; not 0 |
| `DITTOBENCH_CODING_CERTIFICATION_ROUTER_LISTEN` | `<bip gateway>:<port>`, private IPv4, port ≥ 1024 |
| `DITTOBENCH_CODING_CERTIFICATION_EGRESS_NETWORK` | the dedicated daemon's egress network |
| `DITTOBENCH_CODING_CERTIFICATION_EGRESS_PROXY` | `http://<private IPv4>:<port>` |
| `DITTOBENCH_CODING_CERTIFICATION_SECCOMP_PROFILE`, `..._APPARMOR_PROFILE` | optional; `unconfined` refused |
| `DITTOBENCH_CODING_CERTIFICATION_RUNTIME_IMAGE_REPOSITORY` | Docker repository grammar, no tag |
| `DITTOBENCH_CODING_CERTIFICATION_RUNTIME_IMAGE_DIGEST` | `sha256:<64 hex>` |
| `DITTOBENCH_CODING_CERTIFICATION_PACK_MANIFEST_SHA256` | the pinned canary manifest digest; startup refuses another pack |
| `DITTOBENCH_CODING_CERTIFICATION_ROUTER_HELPER_SHA256` | SHA-256 of the installed router helper; startup refuses another helper |

The coding harness gets no CA bundle, no GitHub token and fixed limits (3g
memory, 512m tmpfs, 2 CPUs, 512 pids). The Docker CLI drops every inherited
`DOCKER_*` selector and proxy variable.

Validator environment (read only when `VALIDATOR_CODING_CANARY_ENABLED=true`):

| Variable | Shape |
| --- | --- |
| `VALIDATOR_CODING_CERTIFICATION_SOCKET_UID` | pinned socket owner uid (the service user); not 0 |
| `VALIDATOR_CODING_CERTIFICATION_SOCKET_GID` | pinned socket group gid (the client group); not 0 |
| `VALIDATOR_CODING_CERTIFICATION_CONTROL_TOKEN` | 32–256 URL-safe characters; must differ from `VALIDATOR_DITTOBENCH_CONTROL_TOKEN` |
| `VALIDATOR_CODING_CERTIFICATION_RUNTIME_IMAGE_DIGEST` | must equal the service's pinned digest |
| `VALIDATOR_CODING_CERTIFICATION_PACK_MANIFEST_SHA256` | must equal the service's pinned manifest digest |

The canary no longer uses `VALIDATOR_DITTOBENCH_API_URL`, the scorer bearer or
the shared scorer client.

## Socket verification (validator side)

Before every connection (the transport keeps no idle connection), the client:

1. walks from `/` to the socket directory with `O_NOFOLLOW` directory opens,
   refusing any link, any ancestor not owned by root or the pinned uid, and any
   ancestor writable by group or others (root-owned sticky excepted);
2. requires the directory to be exactly uid/gid/`0750`, and the socket entry
   (`follow_symlinks=False`) to be a socket with exactly uid/gid/`0660`;
3. connects, then requires the kernel peer credential (`SO_PEERCRED`) uid to be
   the pinned uid, and repeats steps 1–2 to require the same socket inode.

Any mismatch closes the connection before a request byte is sent. The client
never dials TCP, never follows redirects and reads no proxy, TLS or socket
setting from the environment.

## Readiness (before any lease claim)

`GET /v1/coding/certifier/canary/readiness` (schema
`dittobench-coding-certification-canary-readiness-v2`) runs these checks in
order. Each later flag requires every earlier one, and the first failure is
reported as `failure`:

| Order | `failure` | Proof |
| --- | --- | --- |
| 1 | `pack` | the loaded public pack re-verifies on disk |
| 2 | `rootless_topology` | euid is the configured non-root user; the daemon socket and directory have the pinned owner and modes (no links); the daemon's default bridge gateway equals the router address; RootlessKit's child and the daemon behind the socket share the pinned, non-detached user and network namespaces owned by this user (`rootlessnetns.Precheck`) |
| 3 | `listener_namespace` | the served router listener is a listening IPv4 TCP socket bound exactly to the router address, without `SO_REUSEPORT`, in the current RootlessKit network namespace (`rootlessnetns.VerifyListener`, `SIOCGSKNS`); a daemon restart strands the old listener and fails here |
| 4 | `control_socket` | the control socket path still names the served inode, in the same directory inode, with the pinned owner, group and modes, reached without links |
| 5 | `executor_daemon` | the dedicated daemon reports rootless and carries the isolated-daemon label |
| 6 | `runtime_image` | the pinned `sha256` runtime image digest is present locally with the supervisor contract |

Startup verifies the helper (root-owned, unwritable by others, pinned SHA-256,
root-owned ancestors, no links) and `nsenter`, then applies the topology check
before creating private state, the router listener or the control socket, re-verifies the listener after construction,
and creates the control socket last.

`POST /v1/coding/certifier/canary` re-runs checks 2–4 before touching the
backend and answers `503 placement` if any fails, so a stale listener or swapped
socket after claim is an infrastructure refusal, never a failed certification
attributed to the candidate.

The validator worker calls readiness before issuing a lease and again between
issue and claim. It refuses (and aborts an issued lease) unless every flag is
true, the reported runtime image digest and canary manifest digest equal its
own pins, and the lease's five pack digests match. Refusals are logged with the
failure code only; no path, uid, token or digest value appears in errors.

## Rendering (review only)

The `coding_certification_service` role renders review files on the Ansible
controller only: the rootless dockerd unit, `daemon.json`, the service unit and
a default-off environment file. It is off by default, refuses system render
locations, and no playbook applies it. It installs, enables and starts nothing,
and it never reads or renders the control token.

```bash
cd infra/ansible
uvx --from ansible-core==2.21.2 ansible-playbook --check -i localhost, tests/coding-certification-service.yml
```

## Tests

- Go: `./internal/codingcertservice ./cmd/dittobench-coding-certification-service
  ./internal/rootlessnetns ./internal/codingcanary ./internal/codinghost ./internal/sandbox`.
- Python: `ditto/tests/validator/test_coding_certification_socket.py`,
  `test_coding_canary.py`, `test_coding_runtime_wiring.py`, `test_config.py`.
- CI: `coding-rootless-router.yml` runs
  `TestCertificationServiceSocketAndReadinessUnderRootlessDocker` against a real
  labelled rootless daemon with synthetic data only. It proves the topology
  gate, the in-namespace listener, the fixed-mode control socket and readiness,
  and drives the validator's own client through ready, relaxed socket (no request
  sent), host-namespace listener (`listener_namespace`) and socket swap cases.
  Runtime image presence is synthetic there and covered by unit tests.

## Explicitly not covered

- Installing or starting anything: the users and groups, subuid/subgid, the
  rootless Docker packages, the units, the pack, the policy, the helper or the
  binary. These are separate, approved operator steps.
- The host deny-all egress guard for the daemon user, the egress proxy itself,
  and the egress network on the dedicated daemon.
- Materializing the control token on the host or in the validator environment.
- Mounting `/run/ditto-coding-certification` into the validator container and
  rendering the new `VALIDATOR_CODING_CERTIFICATION_*` variables in
  `validator_stack`. Until then `validator_stack` refuses
  `validator_stack_coding_canary_enabled: true` during input validation,
  before any host change. Without that refusal the validator would exit at
  startup and stop ordinary scoring too.
- Preloading the reviewed runtime image, and stale-resource cleanup on the
  dedicated daemon.
- A time-bounded authority window on the router listener: unlike the one-shot
  hosted worker, the service is long-lived, so admission relies on source
  binding plus route tokens while it runs.
- Client authentication beyond the socket permissions and the certification
  bearer. The validator container runs as root, so the socket mode identifies
  the service to the validator rather than restricting the validator.
- Any certification run, admin bypass, Platform change or activation.
