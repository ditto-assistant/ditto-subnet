# Trusted Go oracle (runtime integration in progress)

This package parses and type-checks protected Go tests without compiling or
executing them. Candidate source is type-checked only; its initializers do not
run. Both internal-package tests and external `_test` packages are supported.

The caller supplies authenticated, build-selected candidate files, independently
approved function/method names, an expected test count, and a hermetic importer
for preverified offline type data. There is no implicit importer, command,
network lookup or candidate execution fallback in the compiler.

Protected support files may supply private helpers. Their tests are not selected
unless they are the primary suite. Private helpers are tracked separately from
candidate APIs, checked as a closed instruction set and rejected if recursive.
They must remain in the trusted parent; their code and expected values must not
be included in a generated candidate bridge.

The frontend currently admits typed local assignments and values, candidate API
calls, fatal/nonfatal assertion branches, data-only helper loops/returns, and
explicit deterministic standard-library helpers. Corpus-driven additions include
fixed time constructors and `Time.Add`/`Time.In`; ambient clocks are not admitted.
Package globals and candidate-defined constants cannot silently become oracle
inputs. Generic diagnostics do not include private source or type-error details.
Source/configuration/suite objects reject JSON serialization and redact ordinary
debug formatting.

Local public tests:

```bash
go test -race ./internal/codinggooracle
go vet ./internal/codinggooracle
```

An optional, read-only test uses `DITTO_CODING_PRIVATE_AUDIT_ROOT` to audit a
protected local corpus. It is skipped by default. It reads private files only
from that root, performs no candidate execution, and reports aggregate admission
counts plus a suite-set commitment without names, literals or source. Keep that
output in the private work context, not public PR logs or release artifacts.

The parent-side evaluator runs admitted assignments, comparisons, private helpers,
time operations and assertion branches. It owns pass counts, requires a fresh
candidate client per test, and waits for verified client closure before completion.
Synthetic clients exercise the evaluator without importing candidate code. Values
preserve Go integer widths, nil/interface distinctions and scalar comparison
semantics; bounded evaluation prevents empty helper loops from escaping deadlines.
Candidate transport failures and cleanup failures cannot become successful reports.

This is not a completed Go grading runtime. The non-root fixed compiler workflow,
generated API bridge, pre-exec launcher integration, trusted
supervisor reports and actual unchanged base/reference executions remain to be
implemented and verified. Structural/type admission is not runtime qualification,
catalog approval or activation.
