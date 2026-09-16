# Native enforcement evidence v1

This is the B5 evidence format for the hosted-v2 coding canary. It covers the
four `evidence_sha256` entries that the host preflight marks as pending:
network, resource, pre-exec and cleanup. This layer has three parts:

- the record format;
- the probe catalog;
- an offline tool, `infra/scripts/coding-native-evidence.py`.

The verifier runs no probe, reaches no host or daemon, reads no custody path
and creates no approval. The PR2 probe runner (below) writes no evidence
record either. Three collectors write records: the PR4 network collector
(`network_enforcement`), and the PR5 resource (`resource_enforcement`) and
cleanup (`cleanup_recovery`) collectors. Pre-exec collection refuses before any
host effect and lists the catalog probes it does not collect, with the reason;
see [pre-exec and cleanup](#pre-exec-and-cleanup-b5-pr5).
Collectors never run from `coding-hosted-operate`, and a test checks that only
the offline regression job and the disposable rootless probe-runner CI job name
these tools. The collector may appear there only as a path filter and a lint
target; no workflow step executes it.

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
| `inputs` | Per kind: the connectivity profile digest (network) or the execution/grading profile digests, plus the enforcement-images and pre-exec-fixtures digests (pre-exec). Each must equal the document supplied to the verifier |
| `endpoints` | Network only. Roles `router` and `refusing_proxy` (one each, distinct), `trusted` (1 to 32) and `trusted_dns` (0 to 2), each as `endpoint_sha256`. The set must equal the hashes derived from the connectivity profile |
| `tools` | `catalog_sha256`, `collector_sha256`, `evidence_tool_sha256`, `fixtures_sha256`, `probe_runner_source_sha256` (supporting provenance: the canonical hash of the Go runner command, probe library and catalog package trees in the reviewed checkout) and `probe_runner_binary_sha256` (the probe runner binary that actually ran, measured on the host; it must equal the release index's `runtime.probe_runner_sha256`) |
| `preconditions`, `residue` | Worker and custody inactive, no custody socket, zero containers, job networks, volumes and processes |
| `phases` | Catalog phases in order, each with timestamps and its probes |
| `coverage`, `not_covered` | `same_boot`, and exactly `daemon_restart_recovery`, `reboot_recovery` |
| `tolerances_version`, `started_at_unix`, `completed_at_unix` | |
| `network_binding` | Network records only, and required there. `worker_cgroup` (`system.slice/ditto-coding-hosted-worker.service`), `nft_table` (`inet ditto_coding_hosted`), `scoped_ruleset_sha256` and `deny_ruleset_sha256` (normalized loaded rulesets, see PR4), `scoped_output_rules` and `scoped_input_rules`, `refusing_proxy_unit` and `refusing_proxy_sha256` (the proxy script the unit runs) |

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
`--execution-profile`, `--grading-profile`, `--connectivity-profile` and
`--enforcement-images`, plus the release index with `--release-index`.

### Per-language probe images (B5 PR 3b)

Peyton (2026-09-15): "every language image" means every image the approved
profile names, using the same approved limits and timeouts, but each with that
language's canonical, explicitly recorded test command. A common argv is never
forced.

`dittobench-coding-native-enforcement-images-v1` is canonical JSON with closed
keys:

- `grading_profile_sha256`: the approved grading profile whose limits, timeouts,
  command IDs and expected totals every language uses.
- `images`: exactly `go`, `node`, `python` and `rust`. Each has a distinct
  `image_digest` (OCI manifest digest), its own `build_argv`, and its own
  `test_argv` for `hidden` and `visible`.
  - Every argv is 1 to 64 printable arguments with a bare non-shell executable.
  - Test commands must use `dittobench-test-driver`.
  - Rust-specific arguments are allowed only because they are recorded here,
    and they are verified: each Rust test command must be the Rust driver's
    authority command, `dittobench-test-driver --group <its own group>
    --authority <relative .json path> --authority-sha256 <hex>`. This mirrors
    `codingexecutor.rustCommand`, which refuses any other Rust test command,
    so a Rust entry the executor could never run is refused offline. A Go test
    checks the offline check against `rustCommand` on the shared vector.

Resource and pre-exec records carry the set's digest as
`inputs.enforcement_images_sha256`. The verifier requires:

- the set names the supplied grading profile;
- each language's `image_digest` is that language's released image (the
  release index's `image_ref` digest);
- the grading profile's own image is exactly one released language, whose
  recorded build and test commands equal the profile's.

The signed approval pins the set in `profile_pins.enforcement_images_sha256`.
The pre-exec fixtures recorded against these images are pinned alongside it in
`profile_pins.preexec_fixtures_sha256`; see
[pre-exec confinement](#pre-exec-confinement-fixtures-landed-host-wiring-remains).

The probe runner takes image digests and commands only from this set
(`--enforcement-images`; `--image` is gone). It builds each language's hosted
grading manifest from the approved profile with that language's commands, and
refuses a set for another profile. It also refuses a manifest or resolved
repository digest other than the pinned one. `--language` narrows which
pinned languages it observes. Go and Python parse one shared vector
(`catalog/testdata/enforcement-images-vector-v1.json`).

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
(`dittobench-coding-native-enforcement-tolerances-v2`), repeated in the catalog,
the Go package and the verifier. Every limit event must happen at or near its
limit, so an idle or crashed burner fails:

| Probe | Floor (per mille of limit) | Ceiling |
|---|---|---|
| `cpu_throttle` (usage against quota) | 750 | 1150 |
| `memory_oom` (peak before the kill) | 900 | 1000, plus one page of the recorded `page_bytes` (4096, 16384 or 65536) |
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
- `verify --store DIR --checkout DIR --release-index FILE --host-preflight SHA --record SHA...`
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
   release, and they have the same semantic nft ruleset
   (`nft_ruleset_semantic_sha256`) and config digests.
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
  `--curator-signing-key-sha256`;
- that same key identity pinned as `CURATOR_SIGNING_KEY_SHA256` in the reviewed
  `native.py` the approval binds, the only key the host accepts. An approval
  checked against any other key would pass here and still be refused on-host.

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
  `--enforcement-images-sha256`, `--preexec-fixtures-sha256`,
  `--connectivity-endpoint-set-sha256`), for
  example from the signed profile approval and the canary's own connectivity
  profile.
  - The signed approval also carries the same pins in `profile_pins` (PR 3a).
  - The probe profile's full digest stays in the review; it is not a pin.
- The approval's `evidence_sha256`, machine, boot, source revision, release
  manifest and image approvals equal the review.
- Each approval image (`image_ref`, `config_digest`, `approval_sha256`,
  `driver_profile`) equals the release index entry, as `native.release_policy`
  requires on the host.
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
  - `observe-requested-config --grading-profile FILE --enforcement-images FILE
    --executor-repository REPO [--language LANGUAGE]...` emits a
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

`observe-requested-config` itself satisfies no catalog probe. What each kind
collects now (nothing has been collected on a host yet):

| Probe | Status |
|---|---|
| `network_enforcement` (all) | Collected by the [PR4 network collector](#network-collector-b5-pr4) |
| `resource_enforcement` (all 35 probes × 4 language images) | Collected by the [PR5 resource collector](#resource-collector-b5-pr5) from started containers, measured from outside |
| `preexec_confinement` (all) | Public fixtures are authored, recorded and pinned, and a preexec record built from them verifies offline; the host driver-run collector wiring remains. See [pre-exec and cleanup](#pre-exec-and-cleanup-b5-pr5) |
| `cleanup_recovery` (all 10 probes) | Collected by the [PR5 cleanup collector](#cleanup-recovery-collected), including SIGKILL reconciliation from the hosted runtime's launch journal and the consumed-attempt rerun |

The PR4 network collector measures preconditions and residue for network
records (worker and custody state, custody socket, all daemon containers,
`ditto-job-` networks, volumes and worker-cgroup processes). The PR5 collectors
must measure them with the labels production containers carry:
`io.heyditto.dittobench.coding-executor` and `io.heyditto.dittobench.run`.

### Tool binding

Peyton (2026-09-15): runtime acceptance pins the binary that actually ran. The
source tree and revision remain as supporting provenance.

- **Binary (acceptance).** `tools.probe_runner_binary_sha256` is measured on
  the host from the running process. The runner hashes `/proc/self/exe` and
  reports `probe_runner_binary_sha256`. It must equal
  `runtime.probe_runner_sha256` in the release index (`--release-index`, schema
  `dittobench-coding-native-release-set-v3`).
  - The native runtime bundle builds and smoke-checks
    `bin/dittobench-coding-enforcement-probe` and requires it as an executable
    amd64 ELF. The release builder copies its manifest digest into the index.
  - A record must name that exact index: `release_manifest_sha256` is the
    index's digest. Its `source_revision`, `runtime_archive_sha256` and image
    approvals must equal the index.
  - The rootless CI job checks that the reported digest equals an independent
    `sha256sum` of the binary it built.
  - The runner's own `/proc/self/exe` digest is a self-report: a modified
    binary can print the released digest. The record field is therefore only
    as trustworthy as the collector that writes it. The collector (PR4/PR5)
    must measure the file it executes from outside the runner, for example by
    hashing an open descriptor and executing that same descriptor, or by
    hashing `/proc/<pid>/exe` of the child. It must refuse a runner whose
    self-report differs. The verifier cannot tell the two apart.
- **Sources (provenance).** `tools.probe_runner_source_sha256` is the canonical
  hash of the reviewed checkout's `cmd/dittobench-coding-enforcement-probe`,
  `internal/codingenforcement/catalog` and `internal/codingenforcement/probe`
  trees (`RUNNER_ROOTS`). It is still computed from the checkout, so a source
  change after review fails the record. It is never accepted in place of the
  binary digest.
- **Naming.** The old `tools.runner_sha256` is refused. `approval.runner_sha256`
  names only `run.py`.

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
  limit. Fixed in PR5 for hosted-v2 only (Peyton, 2026-09-15):
  `sandbox.NewHostedHarnessDocker` passes `--memory-swap` equal to `--memory`
  and `--pull never`; the v8 sandbox is unchanged.
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
2. ~~Harness swap?~~ Answered: `--memory-swap` equal to memory in the hosted-v2
   harness constructor only (PR5).
3. ~~`--pull never` for the hosted harness?~~ Answered: yes (PR5).
4. ~~Every language image?~~ Answered: every image the approved profile names,
   with the same limits and timeouts, each with its own recorded commands
   (PR 3b).
5. ~~Source trees or binary?~~ Answered: the release-recorded binary digest is
   binding, and the source trees are provenance (PR 3b).

## Network collector (B5 PR4)

`infra/scripts/collect-coding-native-enforcement.py network --config FILE
--confirm "COLLECT NATIVE NETWORK ENFORCEMENT EVIDENCE"` is default-off and
root-only on `ditto-coding-hosted-v2`. It retains one record in the store and prints
`approval_generated: false`; it exits 3 if any probe did not match or residue
remains, so a failing record is kept for review but the verifier refuses it.
Nothing in this PR has run on a host.

### Host setup it requires (operator steps, not automated)

- The `coding_hosted_connectivity` role with
  `coding_hosted_worker_mode: network_enforcement` and
  `coding_hosted_probe_runner: /opt/ditto-coding-hosted/<revision>/bin/dittobench-coding-enforcement-probe`.
  The unit then runs `net-agent --unix /run/ditto-coding-hosted-enforcement/agent.sock`
  in the exact worker cgroup, and after `revoke` a second `ExecStopPost` runs
  `net-once` as the worker user in the same cgroup. This replaces the canary
  worker unit; reinstall `single` mode afterwards. The mode needs a v2 probe
  profile whose window is at most 280 seconds.
- A probe connectivity profile installed by that role: v2, `candidate_tcp`
  exactly the #1899 router listener and refusing proxy, issued at most 60 seconds
  before collection starts, expiring 120 to 280 seconds after issuance. It is
  never the canary profile.
- The #1899 refusing proxy started (`ditto-coding-hosted-egress-proxy.service`),
  the deny guard active, worker and custody stopped, zero containers and job
  networks, and a retained pre-collection preflight at most 15 minutes old.

### Binding

- **Probe runner.** The collector hashes
  `/opt/ditto-coding-hosted/<revision>/bin/dittobench-coding-enforcement-probe`
  and refuses unless it equals the release index `probe_runner_sha256`. It then
  hashes `/proc/<pid>/exe` of every agent it drives, from outside: the worker
  unit's main process, the daemon-cgroup agent, the agent inside each container
  (the child of `docker-init`), and each `net-once` stop probe before opening
  its gate. It measures again after each `hello` and refuses any self-reported
  digest, PID or identity that differs.
- **Docker.** Every Docker call uses `/usr/bin/docker` as the daemon user with
  `DOCKER_HOST=unix:///run/ditto-coding-hosted/docker.sock` and the empty
  `DOCKER_CONFIG`. The daemon identity digest (the preflight's
  `daemon_identity`) must equal the pre-collection preflight's before and after
  collection. Containers run the approved release image with `--pull never`,
  the hosted harness hardening, one read-only bind mount (the measured runner)
  and no other mount.
- **nft.** `nft -j list table inet ditto_coding_hosted` (TZ=UTC) is normalized
  by the preflight's own `nft_entries` (the collector loads
  `inspect-coding-native-host.py` from the same reviewed checkout, so there is
  one implementation): metainfo, handles and counter values dropped, and named
  set elements lose their kernel `timeout` and `expires`. While the worker runs, the listing must equal, entry for entry, what
  `connectivity-policy.py policy` compiles for the installed profile and the
  daemon UID (the collector's `compiled_rules`; a test loads the real policy
  into a kernel namespace and compares). Its digest is
  `scoped_ruleset_sha256`, and the expiry phase's reload must reproduce it.
  After stop and after the failed start, the listing must be the deny guard:
  one UID reject in `output` and no rule in any other chain. That digest
  (chains and rules only) is `deny_ruleset_sha256`, identical for both.
- **Worker cgroup.** The agent's `/proc/<pid>/cgroup` must be
  `system.slice/ditto-coding-hosted-worker.service`, and the stop probes must
  run there as the worker user.
- **Refusing proxy.** The unit must be active, its main process in its own
  cgroup and running exactly `/usr/bin/python3 -I -B egress-proxy.py <address>
  <port>` for the listed proxy endpoint. The record carries the script digest.
- **Endpoints.** Addresses, source addresses and container addresses stay in the
  collector. The record carries only the verifier's endpoint hashes.
- **Targets.** Public, IPv6, metadata, DNS name, proxy CONNECT authority and
  ports come from `internal/codingenforcement/fixtures/network/targets.json`,
  covered by `fixtures_sha256`.

### Phases

1. **active** (worker started): `candidate.router.connect` counts only if the
   worker-cgroup router listener accepted it; `candidate.router.source` is the
   listener's view of the source address (`container_address` only when it is
   the candidate container's address). Proxy connect and CONNECT forward, DNS
   (one question to the container's resolver), metadata, public, IPv6, trusted
   endpoints, Docker API port, host loopback alias and a sibling container on
   its own ICC-disabled bridge. Host loopback and sibling denials count only if
   their outside listeners (worker agent, sibling agent) saw nothing.
   `candidate.identity` and `candidate.host_ids` come from
   `/proc/<pid>/status` and the subordinate range; `executor.interfaces` from
   `/proc/<pid>/net/dev` of a `--network none` container as uid 10001.
   Worker trusted checks are `handshake` (TCP connect and close, no byte sent);
   worker public/metadata and daemon checks are connect attempts.
2. **stop_rollback**: `systemctl stop`; after `revoke`, the gated `net-once`
   attempts every trusted endpoint from the worker cgroup; the candidate
   attempts the router.
3. **expiry**: the worker is started again before expiry; the candidate
   establishes a router flow and the worker establishes one flow per trusted
   endpoint (handshake only). After `expires_at_unix` + 3 s each flow is checked
   with TCP keepalive and `TCP_USER_TIMEOUT` (no payload). The router flow is
   checked on the listener side, outside the candidate. New worker and candidate
   attempts follow.
4. **failed_start**: with the profile expired, `systemctl start` must fail
   (`Result=exit-code`); the gated stop probe attempts every trusted endpoint.

Any refusal kills a waiting stop probe, removes the containers and networks by
name, stops the worker and removes the agent directory before exiting.

### What it does not show

- **Router namespace `host` only.** `rootless-netns` (the in-namespace router
  fd hand-off) is not supported. With slirp4netns the router sees the host
  address, so `candidate.router.source` records `host_address` and the verifier
  refuses the record, until the router source fix lands.
- **Candidate-side denials are self-reports** of the measured binary running as
  the candidate. Only the router, host loopback and sibling probes have an
  outside listener. DNS, metadata, public, IPv6, trusted and Docker API denials
  are not corroborated by nft counters.
- **Not the production harness launch.** The candidate container mirrors
  `sandbox.runArgsForNetwork` hardening but is started by the collector, not by
  `sandbox.LocalDocker`, and uses a released language image rather than a
  screened miner image.
- **`closed` is ambiguous** for `established_after_expiry`: a trusted server
  closing an idle unauthenticated connection within the few seconds after expiry
  looks the same as a cut flow.
- **Host loopback** is tested through the slirp4netns alias `10.0.2.2` only.
- **The verifier cannot recompute the ruleset digests.** It checks their form,
  that scoped and deny differ, and that the scoped rule counts equal what the
  profile compiles to.
- **Pre-/post-collection preflight nft digests.** Preflight v3's
  `nft_snapshot_sha256` hashed the raw listing, which a worker start and stop
  always changes (new handles, leftover scoped chains and sets), so rule 8
  refused every real network record. Preflight v4 instead records
  `nft_ruleset_semantic_sha256`, and the verifier refuses v3. See
  [the semantic ruleset digest](#semantic-nft-ruleset-digest-preflight-v4).
- Reboot and daemon-restart recovery, as for every record.

## Resource collector (B5 PR5)

`infra/scripts/collect-coding-native-enforcement.py resource --config FILE
--confirm "COLLECT NATIVE RESOURCE ENFORCEMENT EVIDENCE"` is default-off and
root-only on `ditto-coding-hosted-v2`. Like the network collector it retains one
record, prints `approval_generated: false` and exits 3 when a probe did not
match or residue remains. Nothing in this PR has run on a host.

### Host setup it requires (operator steps, not automated)

- The release installed, worker and custody stopped, zero containers and job
  networks, and a retained pre-collection preflight at most 15 minutes old.
- A root-protected config (`dittobench-coding-native-resource-collection-config-v1`):
  the network config's host, release, store and preflight fields, plus the
  exact approved `execution_profile`, `grading_profile` and
  `enforcement_images` files, and the `seccomp_profile` and `apparmor_profile`
  names the hosted runtime passes (empty for Docker's defaults; `unconfined` is
  refused).
- `/var/lib/ditto-coding-hosted` on a filesystem that allows exec: the collector
  creates `native-enforcement/` there for the agent, and executor probe
  workspaces (bind-mounted into containers) hold the runner copy the workload
  executes.
- The kernel exposes cgroup v2 `memory.peak` (Linux 5.19 or later) and the
  daemon user's systemd manager delegates cpu, memory and pids.

### Launch paths

Workloads start only through production launch code, driven by the measured
runner's `resource-agent` (a transient `systemd-run --user` unit of the daemon
user, `DOCKER_HOST` pinned to the native socket, the empty client config):

| Class | Production path | Limits from |
|---|---|---|
| `harness` | `sandbox.NewHostedHarnessDocker` (now shared with `codinghostedruntime`) then `LocalDocker.RunEnforcementWorkload`: the same job network, run arguments and retained failed start as `RunRetainingFailedHandle`, with the runner bind-mounted read-only as entrypoint; removal by `StopRetainingImage` | execution profile |
| `executor_authoring` | `PhaseFactory.Authoring` then `Executor.Execute` (image preflight, `createArgs`, policy inspection, supervisor, receipt, exact-id cleanup) | execution profile |
| `executor_grading` | `PhaseFactory.HostedGrading` with `GradingProfile.EnforcementProbeManifest` for each pinned image, then `Executor.RunEnforcementWorkload`: the hosted executor's own `execute` in build mode, accepting only the fixed runner workload argv | grading profile |

Executor commands must be bare executables, so a workload command is
`nice -n 0 /workspace/dittobench-coding-enforcement-probe workload ...`
(coreutils `nice` replaces itself with the runner). The agent copies its own
`/proc/self/exe` into a fresh workspace and checks the copy's digest.

### Binding

- **Runner.** The installed runner must be the release-recorded binary. The
  agent's `/proc/<pid>/exe` is hashed before and after `hello`, and so is every
  workload process inside every container. Its self-reported digest, PID, user
  and the digests of the three documents it read must match.
- **Inputs.** The collector parses the documents with the verifier's own
  parsers and `_enforcement_images` check (released images, the grading
  profile's own language and commands). Each language's repository comes from
  the release index, and its digest must be the pinned one.
- **Container.** Each started container is bound by `docker inspect` from
  outside, or collection refuses:
  - image id equal to the release `config_digest`, read-only rootfs, memory and
    `MemorySwap` equal to the profile, `NanoCpus`, `PidsLimit`, the exact `/tmp`
    tmpfs size (Rust executor carve-out included), not privileged, `CapDrop`
    `ALL`;
  - harness: user `65532:65532`, `local` log driver, a `ditto-job-` network, the
    run label, one read-only runner mount as entrypoint;
  - executor: user `0:0`, `none` log driver and network, the executor instance
    and run labels;
  - the init process's cgroup is under the daemon user's
    `user@<uid>.service` and ends in `docker-<id>.scope`.
- **Workload.** The process is found under `docker-init` (and the supervisor)
  by its exact argv, which carries a fresh 16-hex nonce, and must run as the
  candidate's subordinate host id (65532 or 10001).
- **Cleanup.** After each production receipt the container must be gone.
  Preconditions, residue (including the agent unit's processes), daemon
  identity and boot are checked as for network records.

### What each probe measures (from outside the candidate)

Every probe runs for every class and every language image, one workload per
probe, sequentially.

| Probe | Workload | Observation |
|---|---|---|
| `*.memory_max`, `*.memory_swap_max`, `*.cpu_quota`, `*.pids_max` | `hold` | The container cgroup's `memory.max`, `memory.swap.max`, `cpu.max` (quota × 1000 / period) and `pids.max` (`max` is recorded as 2^63−1 or 0 CPU quota, which never match) |
| `*.memory_oom` | `memory`: a re-executed child maps and touches memory until killed; the parent holds | `memory.events` `oom_kill` ≥ 1 is `enforced`; `measured` is `memory.peak`; `page_bytes` is the collector's `os.sysconf("SC_PAGE_SIZE")` |
| `*.cpu_throttle` | `cpu`: quota/1000 + 1 locked burner threads for 9 s | After a 1.5 s warm-up, `cpu.stat` twice 4 s apart on the collector's monotonic clock: `measured` is usage millis per wall second, `enforced` is a rising `nr_throttled` |
| `*.pids_cap` | `pids`: raw single-thread forks until `EAGAIN`, then hold | `pids.events max` ≥ 1 is `enforced`; `measured` is the highest `pids.current` over 5 reads |
| `*.scratch_enospc` | `scratch`: 1 MiB writes to `/tmp` until `ENOSPC`, then hold | `statvfs` of `/proc/<pid>/root/tmp`: `enforced` when no block is available, `measured` is used blocks × frame size |
| `*.rootfs_read_only` | `rootfs`: tries to create `/.dittobench-rootfs-probe-<nonce>` | `read_only` only if `/proc/<pid>/mountinfo` mounts `/` `ro` and the file is absent through `/proc/<pid>/root`; otherwise `writable` |
| `*.nofile_cap` | `nofile`: opens `/dev/null` until `EMFILE`, then holds | Entries of `/proc/<pid>/fd` once stable; `enforced` when `/proc/<pid>/limits` soft and hard equal 1024 and the table is full |
| `harness.log_bound` | `log`: 1.75 × 8 MiB of lines, then hold | `wchar` from `/proc/<pid>/io` must exceed the bound (`enforced`); `measured` is the byte count of `docker logs` |
| `executor_authoring.log_bound` | `log`: 96 KiB | `wchar` exceeds 24 KiB; `measured` is the production receipt's retained stdout and stderr length |
| `executor_grading.log_bound` | `log`: 96 KiB | `emitted_bytes` from `wchar`; `retained_bytes` is the receipt's retained output (build receipts with output are refused by production validation) under the bound `none` log driver |
| `executor_grading.supervisor_timeout.{hidden,visible}` | `hang` with a same-process-group child, command timeout taken by the agent from that group of the approved grading profile | `elapsed_ms` from the workload's kernel start time (`/proc/<pid>/stat`, clock ticks) to the first 10 ms poll finding it gone or a zombie, on `CLOCK_BOOTTIME`, rounded up; `live_processes` are live candidate-uid processes left in the container cgroup at that moment; `exit_code` is the supervisor receipt's return code |

### Tests

- `ditto/tests/test_coding_native_resource_collector.py`: a simulated host
  whose correct collection verifies offline, 22 enforcement failures that are
  recorded and refused by the verifier, 26 binding refusals that retain nothing,
  residue, config and verifier tamper paths, the memory page tolerance, and
  cleanup collection end to end (retained and verified), with each SIGKILL and
  rerun failure recorded as unmatched and the launch journal parser.
- `ditto/tests/test_coding_native_resource_kernel.py`: the collector's own
  samplers and `SystemHost` readers against a real kernel, with the built
  runner's workloads in throwaway containers under small limits (64 MiB memory,
  0.5 CPU, 64 pids, 64 MiB tmpfs, nofile 1024, the local log driver), including
  negative controls (Docker's default swap, a writable root, a loose nofile
  limit). They ran locally (Docker 29.1.3, cgroup v2, Linux 6.8), and the
  rootless probe-runner CI job runs them on its VM's own Docker daemon with an
  image imported from the runner's shared libraries. They are mechanics, not
  evidence: those containers are not the production launch.
- Go: the workload argument grammar, the resource agent (classes, approved group
  timeouts, refusals, SIGTERM cancelling runs), `RunEnforcementWorkload` in the
  executor and the harness launch splice.

### Findings

- **`memory.peak` can pass `memory.max` by one page.** In 1 of 5 local 64 MiB
  runs `memory.peak` was 67112960 (limit 67108864) after a genuine cgroup OOM
  kill; forced kernel charges are not limited. Decided (Peyton, 2026-09-16) and
  implemented as tolerances v2:
  - each `*.memory_oom` observation carries `page_bytes`, the host page size
    the collector read with `os.sysconf("SC_PAGE_SIZE")`;
  - the verifier (Python and Go) accepts only 4096, 16384 or 65536, and
    otherwise refuses the record, so a collection that sees another size
    retains nothing;
  - the ceiling is `measured <= limit + memory_peak_overshoot_max_pages ×
    page_bytes` with `memory_peak_overshoot_max_pages` = 1. One byte more is
    unmatched, and the 900 per-mille floor is unchanged.

  The shared expectation vectors cover exactly one page, one page plus one
  byte, two pages, and page sizes outside the set.
- **Harness swap and pull.** Fixed for hosted-v2 as decided: without
  `--memory-swap` the local test shows `memory.swap.max` equal to the memory
  limit.
- **The pids cap applies to the supervisor too.** At the cap every clone in the
  container fails. The workload keeps idle runtime threads and holds only
  briefly, and the observation comes from the cgroup either way, but a Go
  supervisor needing a new thread at that moment would abort and lose its
  receipt. Watch for this on the host.
- **Scratch is charged to memory.** tmpfs pages count against the memory cgroup,
  so a profile whose `ScratchLimitBytes` reaches `MemoryLimitBytes` OOM-kills
  before `ENOSPC`, and the scratch probe records it as not enforced.

### What it does not show

- **Grading workloads run in build mode.** The hosted Build and Test paths admit
  only the recorded commands, and the trusted test driver refuses exec, so the
  grading-class workload uses the hosted grading executor's launch in build mode
  (which, like test mode, discards candidate output). The supervisor timeout
  uses each group's approved timeout, but not the test driver's own handling.
- **The harness is not the screened harness server.** The runner replaces the
  image entrypoint, the environment is empty (no egress proxy variables), and
  the image is the released language image, not a screened miner image.
- **Security profile names** come from the collector config, not from the hosted
  runtime's private configuration.
- **Timing.** CPU usage is a 4 s window; host contention can push it below 750
  per mille, which fails closed. Elapsed time has 10 ms granularity at both ends,
  so a deadline killed within milliseconds can read below the deadline, which
  also fails closed.
- **Harness log retention** after rotation may be anything below 8 MiB (still the
  least certain floor).
- Reboot and daemon-restart recovery, as for every record.

## Pre-exec and cleanup (B5 PR5)

`preexec` exits 2 before reading a config or touching the host, and prints every
catalog probe it does not collect with its reason (`NOT_COLLECTED` in the
collector). A record missing a catalog probe never verifies, and no collector
claims a probe it did not measure. A test checks that this list, the catalog
and the tables below agree. `cleanup` collects every `cleanup_recovery` probe.

### Pre-exec confinement (fixtures landed; host wiring remains)

The public synthetic fixtures now exist, are recorded, and a preexec record
built from them is accepted by the offline verifier. What is not yet run on a
host is the driver-run collector wiring that produces such a record.

**Fixtures (B5 PR6).** `internal/codingenforcement/fixtures/preexec/` holds tiny,
deterministic, public fixtures for every released language, recorded in
`fixtures.json` (`dittobench-coding-native-preexec-fixtures-v1`, canonical JSON,
closed keys). Per language it pins:

- a shared `pass`/`wrong`/`hang` controls suite and each control's candidate,
  by path and sha256, with the expected total;
- a hostile candidate for every `hostile.*` probe, each of which attempts the
  forbidden operation before its API is called and, when the pre-exec
  confinement denies it, still returns the correct value so the recorded test
  command passes (`denied`; `credential_env` asserts the secret is `absent`). If
  the confinement failed, the operation would succeed, the fixture would return
  a sentinel and the suite would fail, which the verifier records as unmatched;
- for Rust, a public authority file (`hidden-authority.json`) by path and
  sha256.

Each hostile fixture attempts its probe's own operation and accepts only the
denial that confinement produces (any other error or success is the sentinel):

| Probe | Python, Go, Rust | Node (no syscall API) |
|---|---|---|
| `fork_exec` | fork and exec: `EPERM` (Go: exec only) | `spawnSync`: `EPERM` |
| `process_group_escape` | `setsid`: `EPERM` | detached `spawnSync`: `EPERM` |
| `setuid` | `setuid(0)`: `EPERM` | `process.setuid(0)`: `EPERM`/`EACCES` |
| `signal_supervisor` | `kill(1, SIGKILL)`: `EPERM` | `process.kill(1)`: `EPERM`/`EACCES` |
| `capability_use` | `CapEff` of `/proc/self/status` is zero | same |
| `grader_mount_read`, `control_file_forge` | read or write under the 0700 directory: `EACCES` | same |
| `network` | TCP connect to `10.0.0.1:80`, 2 s: `ENETUNREACH`/`EPERM`/`EACCES` | same, at module load |
| `scratch_exec` | `mmap(PROT_EXEC)` of a file written to `/tmp`: `EPERM` (noexec) | `process.dlopen` of a copied shared object from `/tmp`: segment map failure |
| `unshare` | `unshare(CLONE_NEWUSER)`: `EPERM` | `unshare --user` helper: spawn `EPERM` |
| `mount` | `mount(2)` tmpfs on `/tmp`: `EPERM` | `mount` helper: spawn `EPERM` |
| `ptrace` | raw `ptrace` `PTRACE_TRACEME`, then `PTRACE_ATTACH` to the parent: `EPERM` | open `/proc/<ppid>/mem` (ptrace attach check): `EPERM`/`EACCES` |
| `load_time_escape` | fork at import / in Go `init` / in a Rust `.init_array` constructor: `EPERM` | `spawnSync` at module load: `EPERM` |
| `credential_env` | `DITTOBENCH_FIXTURE_SECRET` is absent | same |

The whole tree is already covered by `tools.fixtures_sha256`; the manifest is
additionally pinned as `preexec_fixtures_sha256` (see below).

**Recording and pinning.** `preexec_fixtures_sha256` is a preexec-record input
and a signed-approval pin (`profile_pins`, `native.PROFILE_PINS`), pinned exactly
where `enforcement_images_sha256` is. The verifier binds a preexec record to it:

- every fixture file is the reviewed checkout's exact bytes;
- the fixtures cover every catalog `hostile.*` probe;
- each `control.{pass,wrong,hang}` observation carries the recorded public
  controls-suite digest;
- Rust is public only when it is **not** the grading profile's own language, so
  its pinned fixture authority can never be private grading material, and that
  authority equals the enforcement image set's recorded Rust command authority.

| Probes | Remaining host wiring |
|---|---|
| `control.pass`, `control.wrong`, `control.hang` | Stage each language's controls suite and candidate and run the recorded test command on the real hosted grading launch; map the receipt to `all_pass`/`some_fail`/`timeout` |
| `identity.candidate`, `identity.host_ids`, `identity.capabilities`, `identity.no_new_privs`, `identity.seccomp` | Read the live hang-fixture candidate's `/proc/<pid>/status` from outside; only the hang fixture provides one |
| `hostile.fork_exec`, `hostile.process_group_escape`, `hostile.setuid`, `hostile.signal_supervisor`, `hostile.capability_use`, `hostile.grader_mount_read`, `hostile.control_file_forge`, `hostile.network`, `hostile.scratch_exec`, `hostile.unshare`, `hostile.mount`, `hostile.ptrace`, `hostile.load_time_escape`, `hostile.credential_env` | Run the per-language hostile fixture and map its receipt (`denied`/`absent`, else unmatched) |

This host path needs the released driver images and the rootless native host to
validate its per-language staging, so it lands with the driver-run collector.
Until then, `preexec` refuses before any host effect, listing every catalog
probe it does not yet collect with this reason, and no record can claim a probe
it did not measure.

### Cleanup recovery (collected)

`collect-coding-native-enforcement.py cleanup --config FILE --confirm "COLLECT
NATIVE CLEANUP RECOVERY EVIDENCE"` is default-off and root-only on
`ditto-coding-hosted-v2`, and takes the resource collector's config, host setup
and bindings. It retains one record, prints `approval_generated: false` and
exits 3 when a probe did not match or residue remains. Nothing has run on a
host.

Every agent session runs with the hosted runtime's
[launch intent journal](../../services/dittobench-api/docs/coding-hosted-runtime-v2.md#launch-intent-journal-and-sigkill-recovery-b5):
`resource-agent --launch-journal native-enforcement/launch-journal
--attempt-state native-enforcement/attempt-N`. The collector creates both
directories (mode 0700, owned by the daemon user). The agent first consumes the
attempt with the runtime's own `ConsumeAttempt` (the function `Run` calls), then
reconciles the journal. It runs each workload as one journaled attempt, one at a
time: it journals and creates a sentinel network, launches only through the
journal hook, and reconciles when the run's production cleanup is done.

Each counting scenario then counts, from outside: all daemon containers,
`ditto-job-` networks (sentinels included), volumes, and processes owned by
subordinate ids plus processes in the daemon user's `docker-*.scope` cgroups.

| Probe | Scenario |
|---|---|
| `cleanup.normal_stop.absent` | An authoring `hold` workload completes through `Execute` |
| `cleanup.partial_start.absent` | A harness start from a never-released digest fails after the production run created its job network; the retained handle is stopped |
| `cleanup.timeout.absent` | An authoring `hang` workload exceeds a 3 s command timeout |
| `cleanup.oom.absent` | An authoring `memory` workload is OOM-killed |
| `cleanup.escaped_setsid.absent` | An authoring `hang` workload whose child calls `setsid` times out |
| `cleanup.runner_sigterm.absent` | The resource agent gets SIGTERM while a workload runs; it cancels the run, waits for production cleanup, reconciles and exits |
| `cleanup.runner_sigkill.reconciled_absent` | A new agent session (a fresh attempt) starts an authoring `hang` workload. The collector binds the container and workload, then SIGKILLs the agent and waits for its cgroup to empty. It runs the runtime's reconciler as a transient unit of the daemon user (`dittobench-coding-enforcement-probe reconcile-launch-journal`, the same `ReconcileLaunchJournal` as `dittobench-coding-hosted-worker --reconcile-launch-journal`, with the pinned socket and `/usr/bin/docker`), then counts |
| `cleanup.runner_sigkill.journal_ids_only` | Read by root after the kill, before reconciling, without following links: the journal must be the daemon user's mode-0600 single-link file of at most 1 MiB. Every complete line must be exactly the runtime's encoding of `schema`, `attempt`, `worker`, `run`, `containers`, `networks`, in that order, with closed identifier values. `journal_ids_only` if so; `permitted` if anything else is present or the file is not private; `absent` if there is no journal or no entry |
| `cleanup.runner_sigkill.sentinel_network` | Before the kill the collector creates a decoy: an internal network named and labelled exactly like a sentinel (`ditto-job-sentinel-<16 hex>`, ownership label, sentinel label) but never journaled. `present` only if all of these hold: after the kill the journal names the workload container and exactly one sentinel, and both still exist; after reconciliation that sentinel is gone; and the decoy is still there. `extra_ids_touched` if the decoy was removed; `absent` if the kill left nothing journaled to reconcile; `probe_error` if the sentinel survived. The collector then removes its decoy before counting |
| `cleanup.rerun.consumed_marker` | The collector reads the killed attempt's `consumed` marker (the daemon user's 0600 file, exact v2 content), then starts the agent again on the same `--attempt-state`. `refused` only if the marker was exact and the agent's only output is the fixed consumed refusal before it exits; otherwise `accepted` |

On an interrupted collection, the collector removes its decoy and, if a journal
is pending, runs the reconciler before removing the work directory.

### What cleanup collection does not show

- **Not a `Run`.** The workloads are started by the probe runner's agent through
  the production launch paths and the runtime's journal, marker and reconciler
  functions, not by `codinghostedruntime.Run`, which needs private Platform
  control and custody inputs. Run's own order (consume, environment, reconcile,
  sentinel, launch through the hook, reconcile after confirmed cleanup) is
  covered by Go tests only.
- **The rerun proves the marker, not Platform.** PostgreSQL's irreversible start
  remains the cross-host authority.
- **One class.** The SIGKILL scenario kills an authoring executor workload; the
  harness path journals its job network too, and a real-daemon test covers that
  only in CI (`TestLaunchJournalReconcilesAKilledOwnerOnRealDocker`, created
  containers only).
- **Sentinel outcome vocabulary.** The catalog's `present` outcome is kept. It
  now means the journaled sentinel was left by the kill and removed by
  reconciliation while an unjournaled look-alike was spared. Removal of the
  journaled sentinel is also counted by `reconciled_absent`.
- Reboot and daemon-restart recovery, as for every record.

## Semantic nft ruleset digest (preflight v4)

`inspect-coding-native-host.py` records `nft_ruleset_semantic_sha256`: the
SHA-256 of the compact sorted-key JSON of `nft_semantic_entries` over the
stripped `inet ditto_coding_hosted` listing. It must be stable across a clean
worker start, stop and profile expiry, and change on any difference that can
affect a packet verdict.

- **Stripped** (`nft_entries`): `metainfo`; every `handle`; counter `packets`
  and `bytes` (set to 0, the counter statement stays); `timeout` and `expires`
  of named set and map elements. The listing must hold exactly one table,
  `inet ditto_coding_hosted`, and no duplicate keys.
- **Pruned**, only where no verdict can depend on the object:
  - a regular chain with no rules that no `jump` or `goto` (in a rule or a map
    element) names;
  - a named set or map with no elements that nothing references as `@name`;
  - a base chain with exactly `family`, `table`, `name`, `type`, `hook`, `prio`
    and `policy`, `type` `filter`, policy `accept`, and no rules. An accept
    verdict from one base chain does not end evaluation of the hook's other
    base chains, so such a chain is a no-op. A rule-less base chain with policy
    `drop`, another type or any other attribute is kept.
- **Kept:** every rule, every referenced or rule-bearing chain, every set or map
  that has elements or is referenced (even when empty), and every other object.
- **Order:** tables, chains, sets, maps, then other objects, each sorted by
  canonical JSON; rules last, grouped by chain with their kernel order preserved,
  since the first match decides. Element lists of named and anonymous sets are
  sorted.

The recorded listings `nft-deny-initial.json` (before any worker start) and
`nft-deny-after-expiry.json` (deny guard, scoped policy, deny guard, element
expiry) have different raw bytes and the same semantic digest. Before expiry
(`nft-deny-after-scoped.json`) the unreferenced sets still hold elements and
are kept, so the digest differs; the post-collection preflight is taken after
the expiry phase, so an early one refuses rather than passes. The verifier
cannot recompute either digest; it requires both preflights to carry the same
one.

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

- **Memory peak ceiling (PR5). Decided 2026-09-16:** allow one page of the
  recorded host page size (tolerances v2; see the resource findings).
- **Pre-exec fixtures (PR5).** Should the enforcement image set record public
  fixture test commands for the languages other than the grading profile's own?
  That includes a public Rust authority when Rust is not the profile's language.
- **Cleanup SIGKILL (PR5). Decided 2026-09-16:** build the journal and the
  reconciler in the hosted runtime; implemented, and the SIGKILL probes are
  collected (see [cleanup recovery](#cleanup-recovery-collected)).
- **Consumed-marker rerun (PR5). Decided 2026-09-16:** observe it without
  private configuration. The marker write and check stay one function, which
  `Run` still calls first. It is exported as `ConsumeAttempt`, and the probe
  agent drives it with a public attempt directory. Nothing in the runtime's own
  check changed.
- **Harness and authoring log floors.** These keep the 500 per-mille floor.
  Docker's local log driver with `max-file=1` may keep any amount under 8 MiB
  after rotation, so this floor is still the least certain.
- **Custody binding and the other tolerances** from the first round remain
  open.

## Host approval verification (B5 PR 3a)

Peyton (2026-09-15): before any host collection can count as approval
evidence, the host verifies the detached curator signature and the Docker daemon
identity is bound into the signed approval and the evidence.

- **On-host signature.** `native.py` no longer accepts an approval digest.
  - `run.py` takes `--native-approval` and `--native-approval-signature`.
    `--native-approval-sha256` is refused before any input is read.
  - The host compiles this tool from one read of its bytes and runs its own
    `verify_ed25519` over the exact stored approval bytes, with the curator key
    pinned in `native.py` source. Only after that does it parse those same
    bytes with `parse_strict`.
  - The approval (`dittobench-coding-native-controls-approval-v3`) names
    `curator_signing_key_sha256` and `evidence_tool_sha256`. The host refuses a
    different key or verifier. `check-approval` refuses them offline too.
  - Peyton (2026-09-16): the signature covers the exact stored bytes, not a
    re-serialization, so the approval need not be canonical JSON. Both the host
    and `check-approval` parse it strictly (no duplicate keys, trailing data or
    non-integer numbers) and bind `sha256` of those exact bytes.
- **Daemon identity.** `dittobench-coding-native-daemon-identity-v1` is a
  closed object; see the qualification README for its fields and why each is
  included.
  - The host preflight (schema `dittobench-coding-native-host-preflight-v4`)
    records it and its canonical digest. The verifier refuses a digest that
    does not match the object, a non-rootless identity, or a preflight v2 or
    v3.
  - Records carry that digest as `host.daemon_identity_sha256`, and every record
    and both preflights must agree.
  - `check-approval` requires `sha256(canonical(approval.daemon_identity))` to
    equal the evidence daemon. `native.policy` fixes the socket and data root.
  - On the host, `docker info` must reproduce the approved identity before and
    after the run, served over the fixed socket by the native principal.
    `server_version` is recorded but not a hard key (Peyton, 2026-09-16): a
    different version is reported in `daemon_identity_observations`, not
    refused. Every other field, including `engine_id`, must match.
- **Endpoint set in the signed document.** `approval.profile_pins` carries the
  endpoint-set, execution-profile, grading-profile, enforcement-images and
  pre-exec-fixtures pins. They must equal the reviewed pins given to
  `check-approval`, and `native.PROFILE_PINS` fixes the same closed key set.
- **Boot and replay.** The approval pins machine and boot. The host rechecks
  both before every step, and the single-use marker refuses a second run on the
  same boot. An identical daemon after a reboot is still refused.
- **Go parity.** `catalog.DaemonIdentityFromInfo` and
  `catalog.ApprovalDaemonIdentitySHA256` share golden vectors with the Python
  side (`daemon-identity-vector-v1.json`, `approval-vector-v3.json`). Go can
  read an approval's daemon digest to refuse a different daemon. It verifies no
  signature and can't create an approval.
- **Validity.** Peyton (2026-09-16): an approval is valid for at most 24 hours
  (`expires_at_unix - issued_at_unix <= 86400`); expired and not-yet-valid
  approvals are refused. This is separate from evidence freshness: every
  collection record must still fall within six hours of `issued_at_unix`.

## Not covered

- Daemon restart and reboot recovery. Every record and review says so, and any
  claim of more than same-boot coverage is refused.
- This layer cannot tell whether a record is true. It checks internal
  consistency and binding only. A fabricated record made with the reviewed
  tools still needs Peyton's review.
- It cannot confirm that `--checkout` is at `source_revision`.
- The router and proxy roles are the record's own split of the two candidate
  endpoints; only the pair itself is derived from the profile.
- Pre-exec confinement host collection: the fixtures are recorded, pinned and
  verifier-accepted, but the driver-run collector that produces a record on a
  host is not wired yet; see
  [pre-exec and cleanup](#pre-exec-and-cleanup-b5-pr5). Cleanup records do not
  exercise `codinghostedruntime.Run` itself; see
  [what cleanup collection does not show](#what-cleanup-collection-does-not-show).
- Endpoint hashes hide raw addresses from the record, but IPv4 addresses are
  few enough to guess a hash by brute force. They are labels, not secrets.
