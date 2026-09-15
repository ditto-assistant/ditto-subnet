# Native enforcement evidence v1

This is the B5 evidence format for the hosted-v2 coding canary. It covers the
four `evidence_sha256` entries that the host preflight marks as pending:
network, resource, pre-exec and cleanup. This layer has three parts:

- the record format;
- the probe catalog;
- an offline tool, `infra/scripts/coding-native-evidence.py`.

The verifier runs no probe, reaches no host or daemon, reads no custody path
and creates no approval. The PR2 probe runner (below) writes no evidence
record either: it only reports requested configuration. Enforcement
measurement, bundle shipping and host collection come in later PRs.
Collectors never run from `coding-hosted-operate`, and a test checks that only
the offline regression job and the disposable rootless probe-runner CI job name
these tools.

`native.py` still checks the six evidence values only as nonzero digests. Their
content is guaranteed only by this verifier plus Peyton's own review and
signature. The curator signature is checked here, offline. The host does not
check it: see the host-side follow-up below.

## Record

Schema `dittobench-coding-native-enforcement-evidence-v1`. All keys are closed;
there is no approval or readiness key. One record covers one `kind`:
`network_enforcement`, `resource_enforcement`, `preexec_confinement` or
`cleanup_recovery`.

| Field | Content |
|---|---|
| `host` | `machine_id_sha256`, `boot_id`, `kernel_release`, `daemon_identity_sha256`, `subordinate_ids` (uid/gid start and count), `router_namespace` (`host` or `rootless-netns`) |
| `release` | `source_revision`, `release_manifest_sha256`, `runtime_archive_sha256`, `image_approval_sha256` for go, node, python and rust |
| `pre_collection_preflight_sha256` | The preflight stdout taken before collection, retained in the store |
| `inputs` | Per kind: the connectivity profile digest (network) or the execution/grading profile digests. Each must equal the document supplied to the verifier |
| `endpoints` | Network only. Roles `router` and `refusing_proxy` (one each, distinct), `trusted` (1 to 32) and `trusted_dns` (0 to 2), each as `endpoint_sha256`. The set must equal the hashes derived from the connectivity profile |
| `tools` | `catalog_sha256`, `collector_sha256`, `evidence_tool_sha256`, `fixtures_sha256`, `runner_sha256` (the canonical hash of the Go runner command, probe library and catalog package trees in the reviewed checkout) |
| `preconditions`, `residue` | Worker and custody inactive, no custody socket, zero containers, job networks, volumes and processes |
| `phases` | Catalog phases in order, each with timestamps and its probes |
| `coverage`, `not_covered` | `same_boot`, and exactly `daemon_restart_recovery`, `reboot_recovery` |
| `tolerances_version`, `started_at_unix`, `completed_at_unix` | |

A probe entry is `id`, `language` (or null), `endpoint_sha256` (or null),
`expect` (copied from the catalog), `observed` and `matched`. Trusted-endpoint
probes carry one trusted hash each. Every `candidate.router.*` probe carries the
router hash, and every `candidate.proxy.*` probe the proxy hash.

Records never hold a raw address, credential or private input. Observed values
are only integers, booleans, catalog outcome names and short lowercase names
such as `lo`. An endpoint is identified only by
`sha256("dittobench-coding-native-endpoint-v1\0" + endpoint_set_sha256 + "\0" + list + "\0" + "address:port")`,
where `list` is `trusted_tcp`, `trusted_dns` or `candidate_tcp`. The profile
itself never leaves the verifier.

Two connectivity digests are used:

- `connectivity_profile_sha256`, in the record's `inputs`, is the native
  canonical sha256 of the whole probe profile, the digest rollout connectivity
  profiles already use.
- `endpoint_set_sha256` is the canonical sha256 of
  `{"schema": "dittobench-coding-native-endpoint-set-v1", "trusted_tcp",
  "trusted_dns", "candidate_tcp", "trusted_loopback_tcp"}`, with each list
  sorted by address and port. It drops the per-issue fields (`issued_at_unix`,
  `expires_at_unix`) and the fields that do not change network authority
  (`schema` v2/v3, `shadow_only`, `weight_eligible`).

The probe profile must expire during collection, so it can never be the
canary's own profile. A reissued canary profile with the same endpoints
reproduces the endpoint-set digest, and the approval review pins that digest.
`endpoint-set --connectivity-profile FILE` prints both digests and the endpoint
counts for any profile, never its addresses.

## Approved profiles

`verify`, `review` and `check-approval` read the exact profile documents with
`--execution-profile`, `--grading-profile` and `--connectivity-profile`.

- The execution and grading profiles must be their exact Go canonical bytes
  (sorted, compact, newline), and their sha256 must equal the record's
  `inputs`. Grading test groups are `hidden` then `visible`.
- The connectivity profile is parsed with the deployer's exact rules from
  `connectivity-policy.py`, tested against it:
  - candidates are in 10/8, 172.16/12 or 192.168/16 on ports from 1024, at most
    2 (v2) or 8 (v3);
  - addresses are canonical IPv4, never multicast, unspecified, link-local or
    reserved;
  - DNS uses port 53;
  - `expires_at_unix` is below 2^32 and at most 24 hours after issuance.
- For evidence, `candidate_tcp` must hold exactly the router and the refusing
  proxy. The record's router and proxy hashes must be those two, and its
  trusted and DNS hashes must equal the profile's entries.
- The network record must fall inside the probe profile's window. The profile
  is issued no later than the record start. The `active` and `stop_rollback`
  phases end strictly before `expires_at_unix`, and the `expiry` phase ends at
  or after it.

Resource limits come only from these documents, never from the record:

| Container | Profile | Scratch | nofile | Log bound |
|---|---|---|---|---|
| `harness` | execution | `ScratchLimitBytes` | 1024 | 8 MiB (sandbox `max-size=8m`) |
| `executor_authoring` | execution | Rust: minus `min(scratch/2, 128 MiB)` | 1024 | 24 KiB model-visible output |
| `executor_grading` | grading | Rust: minus `min(scratch/2, 128 MiB)` | 1024 | 24 KiB model-visible output |

Memory, CPU quota (millis) and pids come from the container's profile
`resource_policy`. A resource probe's `profile`, `limit` or `deadline_ms` must
equal that value, so related probes (for example `memory_oom.limit` and
`memory_max.profile`) agree by construction. Only `executor_grading` has
supervisor timeout probes, one per grading test group
(`executor_grading.supervisor_timeout.hidden` and `.visible`). Each deadline is
that group's approved command timeout, and the observed `test_group` must name
the same group. Authoring command timeouts come from the private task runtime
policy, which the verifier never reads.

## Catalog

The catalog is
`services/dittobench-api/internal/codingenforcement/catalog/catalog-v1.json`.
It is the single source of truth: the Go package embeds it and the Python tool
reads the same file. It defines:

- the probe IDs for each kind and phase;
- each probe's scope: once per record, once per language image, once per trusted
  endpoint, or bound to the router or proxy endpoint;
- the accepted outcomes and tolerances;
- where each resource limit comes from.

The first network probe is `candidate.router.source`. Candidate identities follow
the runtime: the harness sandbox runs as `65532:65532` and the executor candidate
as `10001:10001` (required for the Rust driver). Rootless Docker maps container id
`c` to subordinate start `+ c - 1`, so each host id is checked exactly.

Expectation types:

| Type | Matched when |
|---|---|
| `outcome_in` | The observed outcome is in the accepted list. `probe_error` is never accepted, so a broken probe cannot count as a deny |
| `exact` | The observed object equals the catalog value |
| `profile_equal` | The cgroup value equals the profile value, and the profile value is at least 1 |
| `bounded` | Enforcement was seen, `limit` and `measured` are at least 1, and `limit * floor <= measured * 1000 <= limit * ceiling` |
| `zero_retained` | Exactly 0 bytes retained: `retained_bytes == 0`, with `limit >= 1` equal to the approved output limit and `emitted_bytes >= limit`. Only `executor_grading.log_bound` uses it |
| `supervisor_timeout` | Exit 124, no live processes, and elapsed time between the deadline and the tolerance, for a named `hidden` or `visible` test group |
| `control` | `all_pass`: at least two tests, all passed. `some_fail`: at least two tests, at least one passed and one failed. `timeout`: the run timed out. Pass, wrong and hang controls per language must share one `suite_sha256`, and pass and wrong must have the same total |
| `subordinate_ids` | Host uid and gid equal subordinate start `+ id - 1` for the catalog candidate ids |

Tolerances are versioned integer constants
(`dittobench-coding-native-enforcement-tolerances-v1`), repeated in the catalog,
the Go package and the verifier. Every limit event must happen at or near its
limit, so an idle or crashed burner fails:

| Probe | Floor (per mille of limit) | Ceiling |
|---|---|---|
| `cpu_throttle` (usage against quota) | 750 | 1150 |
| `memory_oom` (peak before the kill) | 900 | 1000 |
| `pids_cap` (pids when fork fails) | 1000 | 1000 |
| `scratch_enospc` (bytes written at ENOSPC) | 950 | 1000 |
| `nofile_cap` (fds open at EMFILE; 1014 of 1024 leaves a 10 fd baseline) | 990 | 1000 |
| `harness.log_bound`, `executor_authoring.log_bound` (bytes retained) | 500 | 1000 |

Supervisor elapsed time must fall between the deadline and 1100 per mille of it.

`executor_grading.log_bound` has no floor. Hosted grading intentionally keeps
none of the candidate's output, so Peyton decided (2026-09-15) that it is an
exact assertion that 0 bytes are retained. Its observation is
`{"emitted_bytes", "limit", "retained_bytes"}`. `limit` must equal the approved
24 KiB output limit, and the candidate must have written at least that much.
Otherwise an idle or crashed writer, which also retains nothing, would count.
One retained byte fails. Both verifiers refuse a catalog that puts this probe back
on a per-mille floor, turns it into a loose `exact` value, or uses
`zero_retained` for any other probe or limit.

## Canonical encoding

A record's digest is the sha256 of its canonical bytes. The canonical form is
the one native qualification already hashes in `run.py`, `prepare.py` and the
release tools: `json.dumps(value, sort_keys=True, separators=(",", ":"))`.

- Keys are sorted by code point, with no spaces and no trailing newline.
- Every non-ASCII character is escaped as lowercase `\uXXXX`, including U+2028
  and U+2029.
- Only int64 integers are allowed. Floats, NaN and duplicate keys are refused.

This is not Platform's `coding_canonical_json_bytes` form, which keeps UTF-8 and
adds a newline. A golden record and an escaping vector are pinned byte for byte
in both the Go and Python tests.

## Tool

- `retain --store DIR --type record|host-preflight|custody-binding --input FILE`
  retains one object.
  - The store is an existing mode-0700 directory owned by the invoking user
    (root on the host). Its ancestors must be owned by root or that user and
    not writable by others, except sticky directories such as `/tmp`.
  - Objects are named by their sha256 and written exclusively (`O_EXCL`), fsynced
    and made mode 0400. Existing objects are never replaced.
  - The host preflight is kept verbatim: the exact stdout of
    `inspect-coding-native-host.py`.
- `verify --store DIR --checkout DIR --host-preflight SHA --record SHA...`
  verifies records against a post-collection preflight. It needs the profile
  documents that the records name.
- `review --store DIR --checkout DIR` with all six evidence digests assembles
  `dittobench-coding-native-evidence-review-v1`.
  - The review has per-item verification, a consistency result and
    `approval_generated: false`.
  - The six-digest map, host, release, profile `inputs`, `endpoints`,
    `endpoint_counts`, `endpoint_set_sha256` and time window appear only if
    every item verified. A failed review prints no digests.
- `check-approval` checks Peyton's signed approval against a verified review.
- `endpoint-set --connectivity-profile FILE` prints a profile's full and
  endpoint-set digests.

`verify`, `review` and `check-approval` refuse to run unless the running
script's own bytes hash to the reviewed checkout's `evidence_tool_sha256`.

Every JSON comparison uses exact types, so `false` never equals `0`. Integers
longer than int64 are refused before conversion. Objects are renamed into place
with `renameat2(RENAME_NOREPLACE)` where available, otherwise linked and
unlinked; a crash between those steps is recovered on the next read or write.

`--checkout` must be a reviewed checkout of the release `source_revision`. Tool
hashes always come from that checkout, never from the record. The checkout, its
files and their directories must not be links or writable by group or others.

### Verification rules

A record is refused unless all of these hold:

1. It is canonical, uses closed keys, and has the right schema and kind.
2. `coverage` is `same_boot` and `not_covered` lists exactly the two uncovered
   items.
3. Every required probe appears exactly once, in the right phase and sorted
   order, with no extra IDs. Every language image and every trusted endpoint is
   covered, router and proxy probes carry their own distinct endpoint, and the
   refusing proxy is listed.
4. Each `expect` equals the catalog, each `observed` has the closed shape for
   its type, and `matched` equals the recomputed value. Every probe must match.
5. The tool, collector, catalog and fixture hashes equal the reviewed checkout.
   The preflight's own tool hashes must also match.
6. Preconditions held and the residue is empty.
7. Profile inputs, resource limits and floors, every grading test group's
   deadline, and endpoints match the supplied profile documents. The network
   record falls inside the probe profile's window, and controls come from one
   suite per language.
8. The pre-collection preflight and the post-collection `host_preflight` are
   separate objects. Both match the record's machine, boot, kernel, daemon and
   release, and they have the same nft snapshot and config digests.
9. Timestamps are in order: the pre-collection preflight (at most 15 minutes
   old), then the record start, the phases and the record end, then the
   post-collection preflight.
10. All records share one host (including `router_namespace` and subordinate
    IDs), release, tool set and shared input digests. No digest is reused.
11. Records run in the order network, resource, pre-exec, cleanup, and never
    overlap: each ends before the next starts.
12. The custody binding names the same machine and boot.
13. Records, the post-collection preflight and the custody binding all fall
    within six hours of each other, in `verify` as well as `review`.

`check-approval` then requires all of these:

- a 64-byte detached Ed25519 signature over the exact approval bytes;
- a PEM Ed25519 curator public key whose raw-key sha256 equals
  `--curator-signing-key-sha256`.

This is the algorithm and key identity that the profile approval and the
curator key loader use. The infra Python environment has neither `cryptography`
nor PyNaCl, so verification runs through OpenSSL 3 (`--openssl`, default
`/usr/bin/openssl`). OpenSSL must be a root-owned file with root-owned,
non-writable ancestors. The key, message and signature are written exclusively
into a fresh owner-only directory, and re-read unchanged after OpenSSL runs. The
tool never handles a private key. It then checks:

- The review re-verifies from the store and re-encodes to the exact supplied
  bytes.
- `native.policy` accepts the approval. `native.py` is read once, its bytes
  must hash to the approval's `binding_sha256`, and exactly those bytes are
  compiled and run. No import loader or `__pycache__` file is used.
- The approval's `runner_sha256` matches the checkout's `run.py`.
- The review's digests equal independently reviewed pins
  (`--execution-profile-sha256`, `--grading-profile-sha256`,
  `--connectivity-endpoint-set-sha256`), for example from the signed profile
  approval and the canary's own connectivity profile.
  - `native.policy`'s closed approval shape cannot carry these digests. The
    approval binds them through the record digests it names, and the pins
    make that binding visible.
  - The probe profile's full digest stays in the review; it is not a pin.
- The approval's `evidence_sha256`, machine, boot, source revision, release
  manifest and image approvals equal the review.
- `issued_at_unix` is no earlier than every record end, the post-collection
  preflight and the custody binding, and at most six hours after the earliest
  record start and the custody binding.

It prints a consistency result, never an approval.

## Probe runner (B5 PR2)

The rule is that evidence never claims more than was measured. In PR2 no
evidence kind is measured end to end, so no binary can write an evidence
record.

- **Library:** `services/dittobench-api/internal/codingenforcement/probe`.
- **Command:** `cmd/dittobench-coding-enforcement-probe`, default-off and never
  invoked from a host workflow. It has two subcommands, and neither assembles
  a record:
  - `resolve-images REF...` pins approved `repository@sha256` images to local
    content ids and refuses a missing image instead of pulling it.
  - `observe-requested-config --grading-profile FILE --executor-repository REPO
    --image LANGUAGE=sha256:...` emits a
    `dittobench-coding-native-probe-observations-v1` report with
    `"enforcement_measured": false`. The verifier refuses that schema as a
    record, and a test proves it.

### What it observes

For each approved image, `ObserveHostedGradingRequestedConfig` does the
following:

1. Refuses a daemon that is not rootless or lacks the isolated label.
2. Accepts only an exact canonical, valid approved grading profile.
3. Pre-checks the image without pulling.
4. Builds a hosted grading executor through the production conversion.
5. Reports `requested_config`: memory limit, swap allowance, CPU quota, pids,
   scratch and read-only rootfs. The source is
   `docker_inspect_created_unstarted_container`.

The container is created, inspected by the production policy check, read back
and removed by exact id. It is never started. That makes it requested
configuration, not enforcement: no process runs, no cgroup file is read and no
write is attempted. It is also circular by construction. The policy inspection
refuses any container whose configuration differs from the plan, so a
successful report can only echo the approved values. It shows the launch code
requests them, nothing more.

`ResolveApprovedImage` is a no-pull pre-check, not the launch guard, and its
resolved id is not passed into the launch. The executor launches by the same
`repository@sha256` reference with `docker create --pull never`, and its
policy check requires the container image to equal the id its own preflight
resolved.

### Production code reused

| Reuse | Where |
|---|---|
| Hosted profile to manifest conversion | `codinghostedworker/grading.go:88` `EnforcementProbeManifest`, which calls the worker's `manifest` (`:65`) |
| Hosted grading executor | `codingexecutor/hosted.go:19` `PhaseFactory.HostedGrading` (`NewHostedGrading`, `:14`) |
| Executor container spec | `codingexecutor/executor.go:439` `createArgs` |
| Create, policy inspection and exact-id cleanup | `codingexecutor/executor.go:96` `withProbeContainer` (shared with preflight), `codingexecutor/docker.go:219` `inspectContainerPolicy` |
| Requested-config read-back | `codingexecutor/resource_probe.go:33` `InspectRequestedResourceConfig` |

Record assembly exists only as test-only synthetic code
(`probe/synthetic_record_test.go`). It proves the Go canonical record form
equals the pinned golden bytes. Preconditions and residue are required
inputs, never defaulted to clear.

### Coverage by catalog probe

Nothing yet satisfies any catalog enforcement probe.

| Probe | Status |
|---|---|
| `executor_grading.{memory_max,memory_swap_max,cpu_quota,pids_max}` | Not satisfied. Only requested configuration is observed. Started-container cgroup reads are deferred to PR3 (in-container helper) and PR5 |
| `executor_grading.rootfs_read_only` | Not satisfied. Docker reports the `ReadonlyRootfs` flag, but no write is attempted. Deferred to PR3/PR5 |
| `executor_authoring.*` | Not observed. The inspection refuses authoring-only executors. Deferred to PR5 |
| `harness.*` | Not observed. The harness launch is not driven. Deferred to PR3/PR5 |
| `*.memory_oom`, `cpu_throttle`, `pids_cap`, `scratch_enospc`, `nofile_cap`, `log_bound`, `executor_grading.supervisor_timeout.*` | Not measured. These need the in-container workload helper and a host cgroup sampler. Deferred to PR3/PR5 |
| `preexec_confinement` (all) | Not implemented. Per-language hostile fixtures are needed. Deferred to PR5 |
| `cleanup_recovery` (all) | Not implemented. Needs the orchestrator's live scenarios, journal, sentinel network and consumed marker. Deferred to PR5 |
| `network_enforcement` (all) | Not implemented. Needs nft and the worker cgroup. Deferred to PR4 |

Preconditions and residue are not measured. A later collector must measure
them with the labels production containers carry:
`io.heyditto.dittobench.coding-executor` and `io.heyditto.dittobench.run`.

### Tool binding

Records carry `tools.runner_sha256`: the canonical hash of the reviewed
checkout's `cmd/dittobench-coding-enforcement-probe`,
`internal/codingenforcement/catalog` and `internal/codingenforcement/probe`
trees (`RUNNER_ROOTS` in the evidence tool). Like every tool hash it is
computed from the checkout, never taken from a record or its caller. A runner
source change after review fails the record.

### CI job

`.github/workflows/coding-native-enforcement-probe.yml` has pinned actions, a
20-minute limit, no secrets, environment or id-token, and never runs on a host.
Its path filters cover every monorepo package the runner and its tagged tests
import transitively (the static test derives this closure and it matches
`go list -deps -test`), plus the datagen replace target, `pyproject.toml` and
`uv.lock`.

1. Run Go vet (including `-tags native_probe_integration`) and unit tests.
2. Build the runner. It links cgo, like the hosted worker.
3. Start a pinned Docker 29.1.3 rootless daemon as a systemd user unit, with
   cpu/memory/pids delegation and the isolated label.
4. Tag a synthetic supervisor-labelled image locally (no registry).
5. Run the tagged integration test and the real `observe-requested-config`
   command against the committed approved profile
   (`probe/testdata/ci-grading-profile.json`), then confirm no executor
   container remains.
6. Run the Python tests with `DITTOBENCH_REQUIRE_PROBE_RUNNER` and
   `DITTOBENCH_REQUIRE_LIVE_PROBE_REPORT` set. They compare every live entry
   with limits the offline verifier parses independently from that profile.
   They also check that the report is refused as a record.

Root verification also builds the runner with `DITTOBENCH_REQUIRE_PROBE_RUNNER=1`,
so those tests cannot skip. The job was reproduced on a disposable local
rootless Docker 29.1.3 daemon. The report matched the profile, and a report
with a doubled pids limit failed the comparison.

### Findings

- **Fixed: Docker 29 capability names.** Docker 29 reports
  `HostConfig.CapAdd` as `CAP_CHOWN`. The executor compared against `CHOWN`,
  so on a real Docker 29 rootless daemon its policy inspection refused its own
  container, and hosted preflight could never pass. The comparison now drops
  the `CAP_` prefix, and an extra capability is still refused.
- **Harness swap.** Confirmed live: the sandbox passes `--memory` without
  `--memory-swap`, so the harness cgroup's `memory.swap.max` equals the memory
  limit. `harness.memory_swap_max` will fail until the runtime sets it.
- **Grading log bound.** `executor_grading.log_bound` cannot match. Grading
  containers use `--log-driver none`, and the supervisor discards candidate
  output, so retained output is 0. The `bounded` expectation requires at least
  1 byte and a 500 per mille floor.
- **Grading profiles are language specific.** The Rust driver requires
  `--group/--authority/--authority-sha256` argv, so one grading profile's test
  commands cannot run on every language image.
- **Repository digests.** A bare `docker import` has no `RepoDigests` on the
  rootless daemon. A local tag or an image-bundle load gives one.

### Residuals

- Requested configuration is not enforcement, and nothing in PR2 changes that.
- CI uses a synthetic supervisor-labelled image, not the approved language
  images.
- The runner command is not a static binary.
- The local reproduction gave RootlessKit ambient `CAP_SYS_ADMIN` inside a
  disposable container, because the dev kernel restricts unprivileged user
  namespaces. The CI VM relaxes the sysctl instead.
- The command inherits the caller's environment. Unlike the hosted runtime, it
  does not clear it to a private `DOCKER_CONFIG` and `TMPDIR`.

### Questions for Peyton

1. `executor_grading.log_bound`: change it to
   `exact {"retained_output_bytes": 0}`, with the grading log limit set to 0 in
   the catalog, Go and Python?
2. Harness swap: set `--memory-swap` equal to memory only in the hosted-v2
   harness constructor, leaving the shared v8 sandbox unchanged?
3. Should the hosted harness launch add `--pull never`?
4. Is "every language image" the approved profile's limits and group timeouts
   with each language's own fixture test argv?
5. Is `runner_sha256` over the source trees the right binding, or should the
   release record the built runner binary's digest instead (PR3)?

## Custody binding

Custody keeps its own digest, and this tool never opens custody paths. To bind
that digest to the machine and boot, the custodian retains a small canonical
`dittobench-coding-native-custody-binding-v1` object:
`custody_evidence_sha256`, `machine_id_sha256`, `boot_id`, `bound_at_unix`. The
approval's `private_input_custody` digest is the digest of that object.

## Peyton's decisions (2026-09-15)

- **Freshness:** one machine and one boot. Records fall within six hours of
  `issued_at`. `host_preflight` is a post-collection preflight, kept verbatim
  and taken after the last record.
- **Resources:** every language image the approval names.
- **Endpoints:** hashed endpoints plus the connectivity-profile digest only.
  Salting the hashes with the endpoint-set digest is accepted on three
  conditions, and each is tested:
  - **Domain separation.** Each endpoint hash is
    `sha256("dittobench-coding-native-endpoint-v1" \0 endpoint_set_sha256 \0 label \0 address:port)`.
    No other digest uses that tag, and the preimage can never be a canonical
    JSON document. Role labels and endpoint sets keep otherwise equal endpoints
    apart.
  - **Approval pin.** `check-approval` requires the reviewed
    `--connectivity-endpoint-set-sha256` pin and refuses a mismatch. PR 3a
    moves this pin into the signed approval document.
  - **Full digest retained.** Each network record keeps
    `inputs.connectivity_profile_sha256`, and the review and check output keep
    it next to the endpoint set. A reissued profile with the same endpoint set
    but a different full digest is refused.
- **Floors:** CPU, memory, pids, scratch and nofile are accepted as listed.
  Grading output retention is an exact zero-byte assertion, not a 500 per-mille
  floor.
- **Custody:** its digest is cross-bound to machine and boot.
- **Proxy:** the refusing proxy is part of network collection and must be
  running and listed.
- **Worker:** a handshake-only positive check to trusted endpoints. Only the
  handshake outcome is recorded.
- **Tolerances:** as above, versioned.
- **Timing:** B5 lands before the release cut, and collectors come from the
  reviewed release.
- **Signature:** a detached curator signature over the final approval is
  required.
- **Workflow:** collectors are never added to `coding-hosted-operate`.

## Questions for Peyton

- **Harness and authoring log floors.** These keep the 500 per-mille floor.
  Docker's local log driver with `max-file=1` may keep any amount under 8 MiB
  after rotation, so this floor is still the least certain.
- **Custody binding and the other tolerances** from the first round remain
  open.

## Host-side enforcement follow-up (B5 PR 3a)

Peyton (2026-09-15): this is not deferred beyond B5. No host collection can
count as approval evidence until the host verifies the detached curator signature
and the daemon identity is bound into the signed approval and evidence. This PR
does not change runtime code. The native consumer still has three gaps:

- `native.py` and `run.py` take `--native-approval-sha256` on the command line
  and never check the curator signature on the host. The signature is checked
  offline by `check-approval`, but the host run is authorized by the operator
  supplying the approval digest.
- `native.policy`'s closed approval shape has no `daemon_identity_sha256`, so a
  run cannot be tied to the daemon the evidence named.
- `expires_at_unix` may be up to 24 hours after `issued_at_unix`, although the
  evidence is only fresh for six hours at issuance.

## Not covered

- Daemon restart and reboot recovery. Every record and review says so, and any
  claim of more than same-boot coverage is refused.
- This layer cannot tell whether a record is true. It checks internal
  consistency and binding only. A fabricated record made with the reviewed
  tools still needs Peyton's review.
- It cannot confirm that `--checkout` is at `source_revision`.
- The router and proxy roles are the record's own split of the two candidate
  endpoints; only the pair itself is derived from the profile.
- The harness sandbox passes `--memory` without `--memory-swap`, so Docker's
  default likely gives the harness cgroup a swap allowance equal to its memory
  limit. `harness.memory_swap_max` would then fail until the runtime sets it.
- Endpoint hashes hide raw addresses from the record, but IPv4 addresses are
  few enough to guess a hash by brute force. They are labels, not secrets.
