# Architecture

DittoBench has two implementations of the scoring core and one of (almost)
everything else. This document is the contract that explains the split so
contributors know where to land changes and which side wins when they
disagree.

## TL;DR

| Concern | Source of truth | Notes |
|---|---|---|
| On-chain weight setting | **Go** (`go/cmd/validator`) | The binary subnet validators run in production |
| Score & anti-gaming math | **Go** (`go/bittensor/`) | Python mirrors it; parity tests are the contract |
| Pylon / chain client | **Go** (`go/chain/`) | Python kept for the contributor runner; SCALE gap documented |
| Fixture corpus | **Python tree** (`ditto/bench/data/`) | Language-agnostic JSONL, single copy |
| Schemas & docs | **Python tree** (`ditto/bench/schemas/`, `ditto/bench/docs/`) | Public miner spec |
| Loader / taxonomy | **Python** (`ditto/bench/loader/`) | Go consumes JSONL directly via flat structs |
| Contributor runner | **Python** (`ditto/bench/runner/`) | Debugger / notebook surface only — not authoritative |
| Tests, dashboards, notebooks | **Python** | pytest + pandas |
| Miner harness contract | Language-agnostic stdio JSON | Miners can write any language |

**Rule of thumb:** if Go and Python disagree on a number, **Go is right** and
Python is the bug. Parity tests in `ditto/tests/bench/` and
`go/bittensor/*_test.go` plus the `parity-smoke` binary in CI enforce this.

## Why Go is canonical

1. **Deterministic on-chain weights.** Validators committing weights have to
   agree to ~5 decimal places. A static Go binary with a pinned `go.sum` is
   easier to reproduce across operator machines than a Python venv whose
   NumPy / Pylon / async-substrate-interface versions drift.
2. **Performance budget.** A validator scores thousands of challenges per
   epoch under a deadline. Go's startup, GC, and concurrency primitives
   beat Python without us reaching for asyncio + uvloop + processpool.
3. **Deployment surface.** Distroless Docker image, no Python runtime, no
   `pip install` at startup. Validator operators on Hetzner / bare metal
   don't need a Python stack on the host.
4. **Reviewer load.** The rest of `ditto-assistant/backend` is Go;
   safety-critical scoring code lives where the eyes are.

## Why Python is still here

1. **Bittensor ecosystem is Python-first.** Pylon SDK,
   `async-substrate-interface`, every reference subnet implementation, and
   every dashboard tutorial are Python.
2. **Fixture authoring lives in Python.** `ditto/bench/loader/` is the
   dataclass + JSONL spec; `ditto/bench/data/` is the case corpus. Rewriting
   the data-engineering side in Go would be a step backwards.
3. **Schemas + docs colocate with Python.** `schemas/*.json` and
   `docs/*.md` are the public spec miners read. Python is the natural host
   because dataclasses + jsonschema generation are mature there.
4. **Parity tests are the moat.** `tests/bench/test_antigaming.py` and
   `go/bittensor/antigaming_test.go` assert value equality on the same
   vectors; `parity-smoke` does a third deterministic JSON envelope diff.
   If anyone touches one side without the other, CI breaks on the same PR.

## Where new work lands

| Change type | Land first in | Port to |
|---|---|---|
| New scorer component, new anti-gaming control | `go/bittensor/` | `ditto/bench/runner/` (parity test required) |
| New validator subcommand / pipeline stage | `go/cmd/validator/` | — (do **not** mirror to Python runner) |
| New chain RPC call | `go/chain/` | `ditto/chain/` only if the Python runner needs it |
| New fixture, new taxonomy entry, new schema field | `ditto/bench/` | Go consumes via flat structs; bump `SchemaVersion` on both sides |
| New miner-facing doc | `ditto/bench/docs/` | — |
| New test corpus / notebook / dashboard | `ditto/` (Python) | — |

If you're tempted to add a third implementation of anything in the
"canonical" rows, stop and write a parity test against the existing
canonical implementation instead.

## What we are explicitly *not* duplicating

- The validator binary. There is one validator, written in Go. The Python
  `ditto/bench/runner/__main__.py` is a *debugger* for contributors poking
  at fixtures and scorers; it is not what validators run in production and
  does not commit weights to chain.
- Fixtures. `ditto/bench/data/` is the only copy; the previous
  `dittobench-testdata/` mirror in `ditto-backend-ops-log` has been
  removed.
- Schemas. `ditto/bench/schemas/*.json` is the only copy; Go side reads it
  directly when validating envelopes.

## The substrate gap

`go/chain/substrate.go::CheckExtrinsicSuccess` returns `ErrNotImplemented`.
The Python side implements this via `async-substrate-interface`; the Go
side does not yet ship a SCALE decoder. Validators that need the success
bit today have three options, documented inline in that file. Closing the
gap is tracked separately and is not blocking the foundation milestone.

## Could we go pure Go? Or pure Python?

Both are possible; both cost us things that matter today.

- **Pure Go** would require porting the fixture loader, schema validator,
  LongMemEval seeding, response-quality grader, and corpus authoring flow
  to Go, plus finishing the SCALE decoder. Contributors writing new cases
  or scorer variants would write Go — a higher bar than `pytest`.
- **Pure Python** is what most subnets do. The validator binary would
  become a Python process with NumPy + Pylon + asyncio: bigger blast
  radius, softer determinism guarantees, and a Python runtime on every
  validator host.

The current split is a deliberate compromise: Go where it has to be right
on-chain, Python where the contributor and data-engineering surface is.
Add to one side; mirror via parity test, not by copying.
