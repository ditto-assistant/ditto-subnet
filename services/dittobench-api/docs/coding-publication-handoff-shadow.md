# Shadow coding durable publication handoff

`internal/codingpublication` exposes the existing durable evidence-outbox
publication state machine to the trusted Python validator over five private
control-token-protected local operations:

- `prepare` stores exact signed authoring-freeze or terminal-result request
  bytes before any Platform transmission;
- `acknowledge` stores the exact verified Platform response and binds it to the
  prepared request digest without releasing terminal evidence;
- `release` accepts only Platform's finalized identity for the exact stored
  terminal acknowledgement and then advances local retention;
- `pending` returns only bounded metadata for the next replayable publication
  per ticket;
- `open` streams the exact content-addressed request or acknowledgement bytes
  for crash recovery.

The Go service independently re-parses request and acknowledgement bytes,
cross-checks them against the durable transcript, frozen patch, ticket, run,
artifact, screened image, manifest, and evidence digest, and rejects changed
replays. Release additionally binds the ticket, record, claim generation,
upload ID, terminal-acknowledgement digest and size, finalized timestamp, and
terminal evidence digest. It never signs, contacts Platform, executes candidate
code, or returns a score.

`ditto.validator.coding_publication.CodingPublicationClient` is the bounded,
no-redirect loopback client. Bodies cross the local JSON wire as strict base64
so the exact signed Platform bytes are not reserialized. Responses are streamed
under a hard size limit and must be JSON plus `Cache-Control: no-store`.

`PlatformClient.prepare_coding_authoring_freeze` and
`prepare_coding_shadow_result` construct the signed immutable request and its
outbox authority. `publish_prepared_coding_publication` sends those same bytes
and returns the exact verified acknowledgement bytes. The final worker wiring
must order them as prepare -> Platform publish -> acknowledge -> upload and
finalize the terminal acknowledgement -> release, and replay only durable bytes
after restart.

`internal/codinghost` mounts the local service only behind the scorer shadow
gate, and `CodingShadowWorker` uses it only behind the separate validator gate.
Both committed defaults are false. Coding remains shadow-only and
`weight_eligible=false`.
