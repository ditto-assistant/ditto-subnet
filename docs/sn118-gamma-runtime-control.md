# Guarded Gamma producer control

Public Backroom `get_treasury_runtime` reads the append-only producer control.
`record_treasury_runtime` requires live write access, expected revision, public
emission approval, both immutable digests, an explicit managed-validator roster, a reason and the exact confirmation
`GAMMA <OBSERVE|ENFORCE|PAUSE> <emission-policy-digest>`.

This control does not load keys, set weights itself, register a collector,
change custody limits, enable timers or transfer funds. It does not replace
the offline-signed policy or confer control over independent validator keys.

## Modes and gates

- Observe verifies the public coldkey signature and current finalized collector
  identity, then configures the ledger observer. It cannot enable weight routing.
- Enforce requires an existing identical public approval, matching public bucket
  settings, epoch-pinned ledgers and the explicit managed-fleet gate. Every
  configured managed hotkey must retain current finalized chain permission and
  prove the exact queued V2 capability and policy digests. Independent validators
  do not block activation. The roster is stored with the audited runtime revision;
  missing or stale managed heartbeats cannot silently shrink it. Historical matching weight
  vectors do not establish future copy behavior. The requested activation epoch
  must be the next independently observed epoch; old pins are never rewritten.
- Pause retains the current signed policy and managed roster and stops serving new V2 dispatch
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
wire boundary and cannot enter signed output. PostgreSQL rejects history updates,
deletes and truncation, enforces one child per parent revision, and refuses a downgrade
which would delete retained controls.
These triggers protect against accidental mutation; database owners can still
disable triggers or drop tables and must remain trusted.

Each producer and ledger authorization reads durable control independently;
no mutable process-local config serves as authority. A shared transaction lock
and revision comparison fence epoch insertion against a concurrent policy
change. Stored epoch pins remain immutable. Corrupt history or unavailable
runtime verification cannot fall back to a cached legacy ledger. Dispatch still
rechecks current finalized collector, epoch and full configured managed fleet, and the transport
retains its own queued-dispatch fence.
Once any durable control exists, a later database failure refuses cached legacy
fallback even when an earlier read saw observe or pause: another process could
have activated Gamma after that read. This intentionally trades availability
for reliable cross-process control.

Configuration and successful control writes are not evidence of a produced
enforcing epoch, chain-accepted weights, collector earnings or finalized
transfer. Use current ledger readiness and independent finalized chain receipts.
The separate one-transfer journal remains capped at 0.01 SN118 alpha; an original
bucket exceeding the ceiling remains held. Recurring transfers are not enabled
by this control. The public Gamma funding view remains a shadow forecast until
its independent live-state integration is completed.

Preflight reports fixed chain failure stage/kind labels (identity vs setter
roster, timeout/connection/invalid evidence/missing reader/unavailable). These
labels expose neither raw exceptions nor credentials and never weaken refusal.

A roster change requires an observe revision before enforcement, and an active
control must first be paused. Stack labels and heartbeat freshness are evidence
for operator selection, never automatic membership authority. Current permission
is checked for each managed member at the finalized identity hash; additional
independent permitted validators do not expand the gate. The public preflight
reports managed scope and the separate chain-permitted total. This changes no
offline allocation signature and makes no prospective copy-weight guarantee.
