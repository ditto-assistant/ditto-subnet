# Guarded Gamma producer control

Public Backroom `get_treasury_runtime` reads the append-only producer control.
`record_treasury_runtime` requires live write access, expected revision, public
emission approval, both immutable digests, a reason and the exact confirmation
`GAMMA <OBSERVE|ENFORCE|PAUSE> <emission-policy-digest>`.

This control does not load keys, set weights itself, register a collector,
change custody limits, enable timers or transfer funds. It does not replace
the offline-signed policy or confer control over independent validator keys.

## Modes and gates

- Observe verifies the public coldkey signature and current finalized collector
  identity, then configures the ledger observer. It cannot enable weight routing.
- Enforce requires an existing identical public approval, matching public bucket
  settings, epoch-pinned ledgers and the existing complete fleet gate. Every
  finalized permitted setter, plus every fresh extra reporter, must have the
  exact queued V2 capability and policy digests. Historical matching weight
  vectors do not establish future copy behavior. The requested activation epoch
  must be the next independently observed epoch; old pins are never rewritten.
- Pause retains the current signed policy and stops serving new V2 dispatch
  authority. It does not cancel previously queued transactions, reverse finalized
  weights or disarm Pylon fences. Legacy weights may remain stopped until a
  reviewed epoch-boundary rollback and queue drain. Pause can be recorded during
  a chain-read outage, but invalid stored approval refuses.

Active controls cannot be replaced or rearmed until explicitly paused. An
already enforcing environment-only deployment must be migrated deliberately;
the database control cannot silently override it. No control is seeded by the
migration. Existing default-off environment configuration remains the fallback
only when no durable control exists.

## Persistence and race handling

Runtime revisions retain the full public approval, checksum, reason, timestamp
and authenticated Platform bearer principal. Backroom sends its signed-in email
in the existing actor header; Platform does not label that caller-controlled
header as independent human authentication. Unknown fields are ignored at the
wire boundary and cannot enter signed output. PostgreSQL forbids history updates
and deletes, enforces one child per parent revision, and refuses a downgrade
which would delete retained controls.

Each producer and ledger authorization reads durable control independently;
no mutable process-local config serves as authority. A shared transaction lock
and revision comparison fence epoch insertion against a concurrent policy
change. Stored epoch pins remain immutable. Corrupt history or unavailable
runtime verification cannot fall back to a cached legacy ledger. Dispatch still
rechecks current finalized collector, epoch and full fleet, and the transport
retains its own queued-dispatch fence.

Configuration and successful control writes are not evidence of a produced
enforcing epoch, chain-accepted weights, collector earnings or finalized
transfer. Use current ledger readiness and independent finalized chain receipts.
The separate one-transfer journal remains capped at 0.01 SN118 alpha; an original
bucket exceeding the ceiling remains held. Recurring transfers are not enabled
by this control. The public Gamma funding view remains a shadow forecast until
its independent live-state integration is completed.
