# Official V13 control assets

An official control is an operator-owned verification asset, not a miner
submission. Its registry contains no Agent, ScreeningAttempt, score, fee or
emission entitlement. This path does not alter the existing miner-control
registry or reinterpret historical approvals.

The initial source kind is the exact canonical starter fixture. Its existing
authenticated source registration must be complete before an image is staged.
The independent served-path attestation and image approval remain mandatory,
but staging and sealing come first so the reviewer can inspect actual bytes.
The source archive, source tree, Dockerfile and reviewed image identity remain
pinned by that fixture.

## Operator workflow

1. Read `get_canonical_starter_fixture_preflight` and register the canonical
   source through its existing authenticated tool. A registrar cannot review
   their own fixture.
2. The same signed-in registrar calls `stage_v13_official_control_image` with
   a fresh request UUID, fixture UUID, image-tar SHA-256, byte size and exact
   candidate Docker image ID. Use the returned constrained PUT and headers.
   Use the config-digest identity verified from the image archive; a Docker
   build's image-index digest is a different identity.
   Reusing that request UUID may remint an unverified upload only when every
   bound field and the authenticated actor match. The tool never accepts a
   miner or attempt UUID.
3. The registrar calls `verify_v13_official_control_image` with the expected
   tar SHA and image ID. Platform hashes the actual staged object, copies it
   to a distinct protected key and hashes the copy. Each concurrent verifier
   uses a different destination; a late verifier cannot replace the key
   another request already committed. Storage I/O does not hold lifecycle
   locks, and the fixture, actor and image binding are checked again afterward.
4. The independent reviewer uses `get_v13_official_control_artifact` to inspect
   the sealed archive before source attestation. It requires artifact scope
   and a current write-level account with authenticated operator proof. Before
   source attestation, any such operator other than the registrar may inspect
   the candidate; afterward only the recorded reviewer may download it.
   Platform rechecks sealed bytes and metadata before issuing a URL. Verify
   the returned digest and review
   the candidate in isolation without credentials or host mounts. Then use
   `review_canonical_starter_fixture` to attest the exact source and config
   image identity. Downloading a candidate does not attest it.
5. The fixture's second authenticated reviewer calls
   `record_v13_official_control_approval` with the exact tar/image/profile
   identities, review-evidence digest and complete reason. The producer cannot
   self-approve, and an unrelated actor cannot substitute for that reviewer.
   An identical approval retry returns the original receipt; conflicting
   attestations require a new reviewed asset instead of overwriting history.
6. Read `get_v13_official_controls` and
   `get_v13_official_control_approvals`. These views expose identities and
   timestamps, not storage keys, URLs, credentials or private case bytes.

Every mutation requires both the service admin bearer and the authenticated
Backroom actor's short-lived, body-bound operator proof. A plain actor header
does not establish independent review. MCP writes additionally require the
write scope and the account's current write access. All three mutations have
distinct exact confirmation phrases.

## Evidence and limits

The database preserves image identity from insertion. Once verified, its
protected storage key and verification time cannot change. Images cannot be
deleted through ordinary SQL. Approval rows are append-only; updates and
deletes fail. The migration refuses rollback while any control or approval
exists. Evidence removal needs a separately reviewed data migration.

Object hashing proves archive bytes, not the Docker configuration inside the
archive. The API therefore always reports
`image_identity_execution_verified=false`. Before executing cases, an isolated
runner must load the archive and compare the actual image identity, then bind
its observations to that identity and the approved runtime configuration.

An approval records an independently authenticated operator claim with status
`recorded_unverified`. Its receipt must predate the private generation event
and challenge randomness. It neither proves semantic benignness nor supplies
missing model-authority, tool, isolation, opaque-role or policy checks.
`policy_verification_complete` remains false.

This asset path is preparation for a separately versioned private replay
contract that can name an official control explicitly. Existing v1 replay
groups and sealed packages still require miner/attempt identities; do not put
a fixture or control UUID in those fields. A typed replay-group, sealed
package, runner-input and signed-receipt integration is required before this
control can participate in a private run. No admission or adjudicator setting
is enabled by these tools.

## Validation

The endpoint tests use real Postgres migrations and a real S3-compatible
store. They cover byte tampering, actor/proof guards, request replay conflicts,
immutable evidence, separate review and absence of economic miner records.
Set `DITTO_REQUIRE_OBJECT_STORAGE=1` to make an unavailable store fail rather
than skip. `DITTO_TEST_MINIO_IMAGE` can point to an operator-built local image
when the harness's default registry image is unavailable. This does not change
production storage configuration.
