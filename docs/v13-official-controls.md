# Retained V13 official control evidence

The screening private-audit activation program is retired. Official controls
are historical operator assets, separate from miner submissions, scores, fees
and emission entitlements. Staging, verification and approval writes have been
removed from Platform and Backroom. No private-case execution bridge is supplied.

`get_v13_official_controls` and `get_v13_official_control_approvals` retain
authenticated, digest-only history. `get_v13_official_control_artifact` retains
inspection of an already verified archive under artifact scope and a current
write-level account. Platform still requires its admin bearer and authenticated
operator proof, enforces the recorded reviewer identity when present, rechecks
the canonical source fixture and actual object bytes/metadata, and returns a
short-lived URL with `no-store`. An unverified staged image cannot be promoted
or downloaded through this read path.

Applied migrations, image and approval records, database immutability triggers
and stored objects remain intact. No existing review claim becomes a semantic
pass: `recorded_unverified`, `image_identity_execution_verified=false` and
`policy_verification_complete=false` preserve their original meaning.

See [the retirement boundary](screening-private-audit-retirement.md) for the
execution removal and retained source-screening protections.
