# Shadow coding durable publication handoff

`internal/codingpublication` exposes the existing durable evidence-outbox
publication state machine to the trusted Python validator over five private
operations. The existing loopback path is control-token protected; the dormant
dedicated-executor path uses a validator-signed control envelope over mTLS:

- `prepare` stores exact signed authoring-freeze or terminal-result request
  bytes before any Platform transmission;
- `acknowledge` stores the exact verified Platform response, binds it to the
  prepared request digest, and releases terminal evidence only after that
  acknowledgement is durable;
- `pending` returns bounded replay metadata locally; the remote form is a
  non-enumerating, ticket-authorized readiness probe;
- `open` streams the exact content-addressed request or acknowledgement bytes
  for crash recovery;
- `lookup` resolves one ticket and stage to its durable publication identity.

The Go service independently re-parses request and acknowledgement bytes,
cross-checks them against the durable transcript, frozen patch, ticket, run,
artifact, screened image, manifest, and evidence digest, and rejects changed
replays. It never signs, contacts Platform, executes candidate code, returns a
score, or releases evidence.

`ditto.validator.coding_publication.CodingPublicationClient` is bounded and
no-redirect. Bodies cross either JSON wire as strict base64 so the exact signed
Platform bytes are not reserialized. In remote mode, the command itself is
canonicalized once, SHA-256 bound into a fresh short-lived executor envelope,
and sent without the local bearer. Responses are streamed under a hard size
limit and must be unencoded JSON plus `Cache-Control: no-store`.

`PlatformClient.prepare_coding_authoring_freeze` and
`prepare_coding_shadow_result` construct the signed immutable request and its
outbox authority. `publish_prepared_coding_publication` sends those same bytes
and returns the exact verified acknowledgement bytes. The final worker wiring
must order them as prepare -> Platform publish -> acknowledge and recover only
the exact bytes returned by ticket-bound `lookup` and `open`.

`internal/codinghost` mounts the local service only behind the scorer shadow
gate, and `CodingShadowWorker` uses it only behind the separate validator gate.
Both committed defaults are false. Coding remains shadow-only and
`weight_eligible=false`.
