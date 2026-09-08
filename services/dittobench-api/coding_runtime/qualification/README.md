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
