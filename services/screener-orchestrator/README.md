# Screener fleet and capacity

The enrolled Hetzner workers claim screening attempts, run builds, runtime
checks, and source reviews, then send signed terminal verdicts to Platform.
The separate capacity controller owns only the GCE managed instance group's
bounded backlog and outage fallback. Platform remains the queue and audit
store; the controller never reads miner artifacts or model credentials.

`python -m screener_capacity.controller` runs as a systemd service on the
private capacity VM. It reads the current provider-routing revision and node
health, calculates the GCE target, acquires a fenced controller lease, and
changes only that target. If a stored routing revision still selects the
retired provider, it routes demand through GCE until an operator updates the
revision. New routing writes cannot select that provider.

The GCE autoscaler stays in `ONLY_SCALE_OUT`, including at a zero target. The
controller pauses it only for a fenced manual resize and restores it even if
the resize fails. The independent queue metric publishes zero while Platform
reports a fresh, ready controller. Missing, expired or unready controllers
activate the metric and permit authenticated legacy GCP claims through the
same fallback predicate. Current provider policy still disables overflow for
a closed or unknown Hetzner primary; explicit GCP-first routing is the operator
override. A fresh controller retains authority over the bounded GCE target.

After deploying Platform and the controller together, verify the safety net in
staging or a controlled production window: with an open primary and backlog,
stop controller reconciliation for longer than its 180-second lease, observe
MIG scale-out, a GCE worker heartbeat and a successful legacy claim, then resume
the controller and verify the target returns to zero. Unit tests do not prove
worker bootstrap or this deployment drill.

Release images are built on the trusted GitHub runner from the exact release
commit, pushed under a SHA tag, and registered with Platform by digest. The
Platform records that immutable image for compatibility and audit. Submission
builds remain inside the enrolled worker's isolated build environment.

For focused tests, run
`uv run --project services/screener-orchestrator pytest services/screener-orchestrator/tests -q`.
Production capacity and worker revisions are read through public Backroom.
