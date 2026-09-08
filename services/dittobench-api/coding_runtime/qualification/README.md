# Private four-language compatibility and native qualification

This is the final planned engineering layer, not permission to import images,
provision hosts/keys, activate a release, run a production canary or change rewards.
Private inputs and sealed production evidence remain Hippius-only. Local private
control receipts are diagnostic operator artifacts, not catalog or runtime approval.

## Private compatibility matrix

Use an isolated clean checkout of the exact revision under review. Build the
`runtime` target (not `test`) of `Dockerfile.coding-python`, `Dockerfile.coding-node`,
`Dockerfile.coding-go` and `Dockerfile.coding-rust`, stamping that exact revision
with `DITTOBENCH_SOURCE_SHA`. Do not include private inputs in any build context.
Provide their local references in a mode-0600 JSON file keyed by `python`, `node`,
`go`, and `rust`. The runner inspects all four, rejects fixture/default/identity
mismatches, and executes by immutable local image ID.

Create a new private operator directory outside Git, then build the static public
helper and Rust's parse-only snapshot inventory:

```sh
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go -C services/dittobench-api build -o /ABS/PRIVATE/operator ./cmd/dittobench-coding-private-control
# From coding_runtime/rust:
cargo build --locked --features driver --bin inventory
```

The Go helper runs candidate code only through the installed trusted supervisor
inside a container. Its `--inspect-go <snapshot> <suite>` mode parses source
without execution. Rust's `inventory --snapshot <snapshot>` and
`inventory --count-suite <suite>` also only parse source. Inventories are based on
pristine implementation exports/signatures, never patched source or test imports,
and explicitly mark `production_api_approval=false`. This is a closed subset;
unsupported types/structure require review rather than a Cargo/test-harness fallback.

`prepare.py` supports the staged curator layout (`groups/*/snapshot/workspace`,
`grader`, `curator/gold-workspace`). It copies only snapshot-selected file names;
visible tests always come from the pristine snapshot, never an old gold tree's
injected tests. Python/Go/Rust expected counts come from non-executing syntax
inspection. Node counts must be supplied separately in a private source-hash-bound
inventory; a stale suite hash is rejected rather than guessing counts.

```sh
python3 prepare.py --corpus /ABS/PRIVATE/corpus \
  --rust-inventory /ABS/CHECKOUT/services/dittobench-api/coding_runtime/rust/target/debug/inventory \
  --go-helper /ABS/PRIVATE/operator --node-counts /ABS/PRIVATE/node-counts.json \
  --source-sha REVIEWED_40_HEX_SHA --output /ABS/PRIVATE/plan.json
python3 run.py --plan /ABS/PRIVATE/plan.json --images /ABS/PRIVATE/images.json \
  --corpus /ABS/PRIVATE/corpus --helper /ABS/PRIVATE/operator \
  --checkout /ABS/CHECKOUT --output /ABS/PRIVATE/new-matrix --jobs 2
```

Concurrency is bounded to 1, 2 or 4 containers. Use 4 only on an operator host
with capacity for the corresponding aggregate CPU/RAM limits; per-container
limits and private-data boundaries do not change.

The runner requires all four languages and complete base/reference × visible/
hidden coverage for each group, with two repeats. Reference and base-visible
controls must pass; base-hidden controls must expose at least one failing test.
Every copied byte is hash-checked. Each control has a fresh networkless read-only
container, capped CPU/RAM/PIDs/scratch, private grader/control roots, a source-free
supervisor result, and checked removal. Only the helper and private data are
mounted at run time; no Docker socket, credentials or provider key enter it.

Every observation and full supervisor response is retained privately, together
with exact source, plan, helper, runner, image and kernel commitments. Semantic
results/input commitments must agree across repeats; nonce-bound response bytes
and per-execution runtime receipts are retained but not expected to be identical.
Files are exclusive mode 0600 beneath a fresh mode-0700 output directory. A failure
does not overwrite old receipts or synthesize a passing summary.
Use `--collect-failures` for a diagnostic sweep: every safely cleaned-up control
is recorded, a failing summary remains failing, and the command exits nonzero.
It does not skip tests, relax expectations, or continue after unconfirmed cleanup.

The local image binding is explicitly a config ID, not an approved native OCI
manifest. Source labels and these operator-generated receipts are not independent
build attestations. They cannot substitute for custodian review, native import,
key custody, recovery or production execution evidence. All readiness/approval
flags remain false even when every compatibility control passes.

Local controls pin a local Unix engine for the invocation: the default is
`unix:///var/run/docker.sock`, or an explicit local `DOCKER_HOST`. `DOCKER_CONTEXT`
and remote engines are refused. The native host/name/data root is also refused
by this diagnostic path; omitting native approval flags must not bypass its gates.

## Separately approved native controls

Native mode is an explicit, single-use compatibility invocation on the dedicated
`ditto-coding-hosted-v2` Linux amd64 host, as the non-root `ditto-coding-hosted`
daemon principal. It is not a private competition attempt or an automatic host
qualification decision. The local diagnostic mode above remains separate.

Before authorization, the custodian must review the exact release set, imported
images, real host enforcement and private-input/key/recovery evidence. The
post-import preflight alone is insufficient: its pending network, resource,
pre-exec and cleanup checks must not be relabelled as passing evidence. This tool
neither generates an approval nor contacts a key, provider, catalog or admission
service. Only an independently communicated approval-file SHA authorizes a run.

The closed approval record uses schema
`dittobench-coding-native-controls-approval-v2`, purpose
`private-compatibility-once`, and these required fields:

| Field | Binding |
|---|---|
| `source_revision` | Exact clean checkout shared by runner, binding module and all four production images |
| `release_manifest_sha256` | Independently reviewed release-set index SHA |
| `plan_sha256`, `helper_sha256` | Exact private plan and public static linux/amd64 helper bytes |
| `runner_sha256`, `binding_sha256` | Exact `run.py` and `native.py` bytes |
| `machine_id_sha256`, `boot_id` | Intended host's stripped machine-ID hash and current boot UUID |
| `issued_at_unix`, `expires_at_unix` | Current validity window, at most 24 hours |
| `controls`, `max_jobs` | Exact plan case count times two; at most 1024 controls and 1, 2 or 4 parallel jobs |
| `images` | Exactly `python`, `node`, `go`, `rust`, each with approved `image_ref`, `config_digest`, `approval_sha256`, `driver_profile` matching the release index |
| `evidence_sha256` | Nonzero digests for `host_preflight`, `network_enforcement`, `resource_enforcement`, `preexec_confinement`, `cleanup_recovery`, `private_input_custody` |
| `shadow_only`, `weight_eligible` | Explicit `true`, `false` |

Evidence digests identify the custodian's separately reviewed records. This
consumer does not parse or independently attest their contents; a fabricated
record or a self-computed hash is not approval. Preserve the actual underlying
evidence and its approval audit outside Git. These references do not constitute
hardware attestation or protection against the trusted host principal/root.

The plan, image-reference map, approval and release index must be worker-owned
mode-0600 regular single-link files beneath protected canonical directories.
The corpus and output parent must be worker-owned mode 0700 outside the checkout.
The helper must be a protected single-link mode-0555 amd64 ELF. No shared writable
ancestors or symlinks are accepted. The existing empty Docker client directory
and private native socket must already be provisioned; the runner does not create
or repair them. Pre-provision a dedicated worker-owned mode-0700
`/var/lib/ditto-coding-hosted/qualification` directory for durable consumed markers.

```text
python3 -B -I run.py --plan /ABS/PRIVATE/plan.json --images /ABS/PRIVATE/images.json \
  --corpus /ABS/PRIVATE/corpus --helper /ABS/PUBLIC/operator \
  --checkout /ABS/REVIEWED/CHECKOUT --output /ABS/PRIVATE/new-native-matrix --jobs 2 \
  --private-native-controls-once --native-approval /ABS/PRIVATE/approval.json \
  --native-approval-sha256 INDEPENDENTLY_APPROVED_64_HEX_SHA \
  --native-release-index /ABS/PRIVATE/release.json
```

Every native engine call uses `/usr/bin/docker`, the fixed owner-only native
socket and a clean environment, never an ambient context. Images are selected by
approved repository/manifest references with `--pull=never`; the case authority
records the OCI manifest digest, not a local config ID. Rootless/containerd/cgroup
metadata and zero containers are checked before and after the matrix. Host boot
and expiry are rechecked before controls; per-control deadlines reserve cleanup
time and cannot be extended by moving the wall clock backward.

Before any container starts, the approval SHA is consumed through an exclusive,
fsynced marker in the fixed qualification directory. A fresh output directory
cannot reuse it; empty/partial markers also refuse retry. Do not delete markers,
restore old state, change the approval or silently retry after interruption.
This is operator-local one-shot state, not the competition's PostgreSQL start
ledger. Root/same-UID state tampering is outside the boundary. Expired or failed
runs require exact-resource reconciliation and a separately approved new action.

Both modes now retain per-case input directories alongside observations, including
when removal is unconfirmed. All files remain private operator artifacts; none
are cleaned automatically. A native summary records `native_controls_passed` and
`image_binding_kind=approved_native_oci_manifest` plus host/approval commitments.
`runtime_qualification`, `production_api_approval`, `native_host_ready`,
`canary_completed` and `weight_eligible` remain false: the control result is
evidence for independent acceptance, not authorization to activate the competition.

## Native acceptance gates, in order

| Gate | Required evidence before proceeding |
|---|---|
| Source | Six intended PRs reviewed and merged in dependency order; required checks on exact heads; immutable integrated release revision. |
| Images | All four production targets rebuilt from that revision; public OCI graphs/configs/layers verified; independent approval SHA for each exact profile/image; no fixture/probe/default hooks. |
| Host | Reviewed native host provisioning; dedicated principal/socket/data root; isolated rootless containerd daemon; delegated cgroup v2 limits; mount, network, pre-exec filter and cleanup controls on the actual host. |
| Inputs and keys | Hippius-only exact private reads; authenticated manifests; Platform-only private material; approved unwrap/signing services and key identities; no validator/candidate key access. |
| Recovery | Actual encrypted spool/append-only ledger/Hippius readback recovery; one-shot consumption and ambiguous-run refusal; retained resources reconciled after interruption. |
| Private runtime | Repeat the complete private control matrix against the imported exact native images/profile, retaining private source/image/host commitments and complete cleanup evidence. |
| Shadow canary | Separately authorized single private shadow attempt; authoritative grading, signed/sealed evidence and exact readback; no duplicate execution; validate calibration and recovery/rollback. |
| Rollout | Review the above receipts; explicitly authorize a bounded shadow rollout. Competitive weights/emissions remain separately gated. |

Use the existing [native OCI importer](../../../../infra/docs/coding-hosted-image-v2.md),
[hosted worker](../../docs/coding-hosted-runtime-v2.md), and
[Platform runtime companion](../../../../apps/platform/docs/coding-hosted-platform-runtime-v2.md)
for the operational procedures. The image importer now recognizes closed Python,
Node, Go and Rust profiles, but remains default-off and reports qualification
required/private execution not ready after import. Its ability to validate a
profile is not approval to import it.

Do not claim final completion from PR count, green CI, local image builds, metadata
validation, or a local control matrix. Missing approval, host access, custodian
coordination, recovery evidence or a real canary is a named operational blocker,
not a reason to flip readiness flags or bypass a gate.
