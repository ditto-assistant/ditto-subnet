# Shadow core qualification with hysteresis

Core qualification is a separate, diagnostic fact derived from the existing
tool-and-memory score pipeline. It does not modify a score and is not read by
coding admission, queueing, ranking, validator weights, or emissions.

## Policy authority

No policy exists by default. An operator may write one append-only policy for a
specific benchmark version through Backroom. Every revision is
`weight_eligible=false` and declares:

- entry floors for quorum-median composite, tool mean, and memory mean;
- lower-or-equal retention/exit floors for the same dimensions;
- consecutive evidence snapshots required to enter and exit.

The write uses optimistic concurrency, an audit actor and reason, and exact
confirmation `APPLY SHADOW CORE QUALIFICATION V{bench_version}`. Changing the
policy revision starts a fresh observation sequence; it never re-labels old
evidence under new thresholds.

## Evidence and hysteresis

After an ordinary score commits, Platform best-effort reconstructs the current
validator score set. Fewer than three scores produces no observation. Every
other observation binds:

```text
agent UUID
source artifact SHA-256
screened image SHA-256
benchmark version
policy revision and checksum
sorted validator hotkeys, run IDs, seeds, aggregates, sizes, timestamps,
and signatures
```

The score projection is content-addressed. Re-observing identical evidence is
idempotent and cannot advance a streak. After the first snapshot, a streak
advances only when every validator run ID in the quorum changed; a partial
replacement is visible as `partial_wave` but cannot qualify or de-qualify the
artifact. Runs below the full benchmark case floor are recorded for diagnosis
but fail both entry and retention. A database-assigned monotonic sequence, not
timestamps or UUID ordering, determines the latest observation.

An unqualified artifact enters only after `enter_observations` consecutive
snapshots clear all three entry floors. A qualified artifact remains qualified
while all three exit floors hold, and exits only after `exit_observations`
consecutive snapshots fail at least one. A source upload, screened-image
rebuild, benchmark change, or policy revision makes previous observations
stale instead of rewriting or deleting them.

## Failure isolation

Observation runs in a new transaction after the score and ticket commit. A
missing policy, rolling migration, malformed shadow row, or observer failure is
logged and cannot reject or roll back the ordinary score. The shadow ledger is
therefore auditable without becoming a second scoring authority.

## Operator visibility

Backroom exposes:

- the current policy and revision history;
- an append-only policy write;
- one agent's current exact-bound status and stale observation history;
- an idempotent one-agent refresh for existing scores or recovery after a
  transient shadow-write failure.

This package does not issue a coding task. A separate reviewed change must add a
shadow coding job/score ledger, and coding contract v2 plus calibration and
owner approval remain prerequisites for any emissions allocation.
