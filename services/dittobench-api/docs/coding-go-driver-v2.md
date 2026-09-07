# Trusted Go test-driver candidate

`Dockerfile.coding-go` packages the `go-call-ast-v1` driver, fixed Go compiler,
reviewed pre-exec bootstrap and existing Go supervisor. The parent interprets
protected test assertions. Hidden tests and private helpers are never compiler
inputs or linked into the candidate program.

This is a runtime candidate, not universal `go test` compatibility or production
approval. Qualify unchanged suites, source, reference patches, image digest,
resource limits and provider/kernel behavior before selecting it.

## Command authority

```text
dittobench-test-driver --group hidden --suite hidden_test.go --support visible_test.go --package-path example.invalid/subject --function Add --function Parse --candidate-timeout-ms 10000 --build-timeout-ms 120000
```

Functions must be independently approved candidate APIs, not inferred from hidden
test names. Support files are protected oracle dependencies in the pristine
workspace: their helpers remain parent-owned and their tests are not counted as
selected tests. The supervisor appends the report path, nonce, expected count and
candidate UID/GID. Duplicate or unknown authority flags are rejected.

The driver checks canonical root-owned mode-0700 control/grader directories and
exclusive report creation. It discovers the selected Go test count and requires
the approved total. Workspace files are bounded, regular and traversal-contained;
test files are excluded from compiler inputs. Build results, errors and private
AST/type objects are not public wire objects or diagnostics.

## Compile, execute, report

1. Build only candidate-authorized implementation files plus a public API bridge
   with the fixed non-root toolchain. cgo, external linking, Go environment hooks,
   VCS stamping, toolchain download and module/VCS network access are disabled.
2. Kill/reap compiler process groups, then seal the executable in an immutable
   root-owned memory file. Writable scratch remains `noexec`. Explicit executable
   memory-file support is required; no mount-policy or execution fallback exists.
3. Type-check the unchanged private suite and protected helper files in the trusted
   parent, preserving internal/external package visibility and Go constant types.
4. Launch a fresh confined process for each test. Confinement precedes package
   initialization. Only API calls and arguments cross the bounded, correlated
   reply channel; candidate stdout, stderr and claimed test counts are ignored.
5. Evaluate assertions and private helpers in the parent. Preserve scalar types,
   nil/interface distinctions, explicit fixed-time operations and supported slice
   effects. Every candidate must be terminated before its test completes.
6. Write the exclusive mode-0600 nonce-bound trusted report only after cleanup.
   The supervisor independently verifies the report and process-group termination.

Compilation/type incompatibility is a candidate failure with zero passed tests;
missing runtime authority, unsupported oracle policy or unverified cleanup creates
no successful authoritative report. The initial frontend/type and value-language
boundaries are documented in [codinggooracle](../internal/codinggooracle/README.md).
The [compiler and launcher](../internal/codinggobuild/README.md) describe kernel,
memory-file and transport requirements, including remaining alias-graph boundaries.

The compile deadline is bounded independently and cannot extend the supervisor's
outer command deadline. Profiles need enough scratch and memory for a cold fixed
Go compilation; the synthetic integration fixture uses 512 MiB scratch, 1 GiB RAM,
256 PIDs and two CPUs. Those fixture numbers are not automatic production approval.

## Validation and remaining gates

Public container scenarios cover successful and incorrect results, typed nil,
ordinary/forged output, early exit, hangs, hostile initialization, private file
access, report forgery, malformed/oversized API frames, compilation failure,
compiler attempts to read protected data, unsupported suites and count mismatch.

```bash
cd services/dittobench-api
go test -race ./internal/codinggodriver ./internal/codinggooracle ./internal/codinggobuild
```

From the repository root:

```bash
docker build -f services/dittobench-api/Dockerfile.coding-go --target test -t coding-go:synthetic .
bash scripts/test-coding-go-driver.sh coding-go:synthetic
```

The final runtime target excludes synthetic probes, private inputs and build-time
source/cache state. Source revision and profile labels are explicit. Private
control runs mount protected data only at runtime and retain their output outside
public CI and Git. Initial successful controls do not replace repeat runs,
adversarial evaluation, exact runtime import approval or a live native-host canary.
This source layer does not register a catalog release or enable rewards.

The test target also includes `/opt/private-probe`, a generic operator-only probe
without embedded private data. `--group <opaque-id> --variant base|reference
--supervisor-phase visible|hidden` runs one unchanged suite through the actual
supervisor in a fresh container. Mount the protected corpus read-only at
`/private-input`, with the same resource and empty tmpfs settings as the public
probe. It selects reference implementation files only through snapshot-authorized
filenames; old injected tests in a reference workspace are never copied. This
initial corpus reader admits only flat libraries with one suite per phase.

Private output binds input, suite, helper set, command, request and response
hashes and requires authoritative counts, suppressed output and confirmed process
cleanup. The operator receipt must additionally bind the exact fixture/runtime
image identities, clean source revision, resource envelope and repeat index.
`RuntimeQualification` remains false: these controls are evidence for a separate
qualification decision, never an automatic catalog or runtime approval.
